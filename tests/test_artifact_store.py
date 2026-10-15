from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import stat
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from cater import artifact_store as store
from cater.artifacts import normalize_deliverables
from cater.events import Actor, EventLog
from cater.rules import RuleBook
from cater.work import WorkLedger, digest


def fixture(names=("main.py",), content="print('stored, never executed')\r\n"):
    artifacts = []
    for seq, filename in enumerate(names, 10):
        artifacts.append({"seq": seq, "content": content, "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
                          "content_bytes": len(content.encode()), "filename": filename, "format": "code",
                          "work_item_id": "implementation", "attempt_id": "attempt-one"})
    submission = {"plan_seq": 2, "work_item_id": "implementation", "attempt_id": "attempt-one",
                  "artifact_seqs": [a["seq"] for a in artifacts],
                  "hashes": {str(a["seq"]): a["content_sha256"] for a in artifacts}, "inputs": {},
                  "scope": {"direction_seq": 1, "planning_seq": None, "rules": "rules-fingerprint"}}
    submission["digest"] = digest(submission)
    return submission, artifacts


def publish_process(root, submission, artifacts, start, results):
    start.wait(10)
    try:
        results.put(("ok", store.materialize(root, "task", submission, artifacts)))
    except Exception as exc:
        results.put(("error", repr(exc)))


class ArtifactStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "artifacts"
        self.submission, self.artifacts = fixture()

    def publish(self):
        return store.materialize(self.root, "task", self.submission, self.artifacts)

    def target(self):
        return self.root / f"task-{hashlib.sha256(b'task').hexdigest()}" / self.submission["digest"]

    def test_real_work_artifacts_and_submission_projection_are_accepted(self):
        log = EventLog(self.base / "work.db")
        self.addCleanup(log.close)
        work = WorkLedger(log, RuleBook(log))
        created = log.emit(type="task.created", actor=Actor.human(), task_id="task", payload={"goal": "Create source"})
        work.plan("task", [{"id": "implementation", "owner": "dev", "reviewer": "test", "objective": "Write source",
                            "depends_on": [], "criteria": [{"id": "body", "kind": "review", "text": "Inspect source"}]}],
                  role_ids={"dev", "test"}, source_seq=created.seq)
        claim = work.claim("task", "implementation")
        payload = normalize_deliverables([{"title": "Source", "summary": "Exact bytes", "filename": "main.py",
                                           "content": "# \u4e2d\u6587\r\nassert True\n", "format": "code"}])[0]
        published = log.emit(type="deliverable.published", actor=Actor.agent("dev", "worker"), task_id="task",
                             payload={**payload, "work_item_id": "implementation", "attempt_id": claim.payload["attempt_id"]})
        submitted = work.submit("task", claim, [published.seq])
        projection = work.view("task")["items"][0]["submission"]
        artifacts = work.artifacts("task", projection)
        manifest = store.materialize(self.root, "task", projection, artifacts)
        self.assertEqual(manifest, store.materialize(self.root, "task", submitted.payload, artifacts))
        version = self.root / manifest["directory"]
        self.assertEqual((version / manifest["files"][0]["path"]).read_bytes(), payload["content"].encode())
        self.assertEqual(json.loads((version / "manifest.json").read_text(encoding="utf-8")), manifest)

    def test_repeat_checks_all_bytes_without_rewriting_and_never_executes(self):
        self.submission, self.artifacts = fixture(content="raise RuntimeError('must not execute')\n")
        with patch("subprocess.Popen", side_effect=AssertionError("no commands")), patch("os.system", side_effect=AssertionError("no commands")):
            manifest = self.publish()
            version = self.root / manifest["directory"]
            paths = [version / "manifest.json", version / manifest["files"][0]["path"]]
            before = [(p.stat().st_mtime_ns, p.read_bytes()) for p in paths]
            self.assertEqual(manifest, self.publish())
            self.assertEqual(before, [(p.stat().st_mtime_ns, p.read_bytes()) for p in paths])
        self.assertFalse(Path(manifest["directory"]).is_absolute())
        self.assertEqual(manifest["files"][0]["seq"], 10)

    def test_versions_and_task_namespaces_do_not_overwrite(self):
        first = self.publish()
        second = store.materialize(self.root, "TASK", self.submission, self.artifacts)
        self.assertNotEqual(first["directory"], second["directory"])
        self.submission, self.artifacts = fixture(content="new revision\n")
        third = self.publish()
        self.assertNotEqual(first["directory"], third["directory"])
        self.assertTrue((self.root / first["directory"] / first["files"][0]["path"]).is_file())

    def test_long_root_with_writable_final_path_uses_short_staging_name(self):
        self.root = self.base
        padding = 252 - len(str(self.target() / "manifest.json")) - 1
        self.assertGreater(padding, 0, "test temporary base leaves no room below MAX_PATH")
        self.root = self.base / ("r" * padding)
        target = self.target()
        self.assertEqual(len(str(target / "manifest.json")), 252)
        self.assertLess(len(str(target / "files")), 248)
        old_staging = target.parent / f".staging-{target.name}-{'0' * 32}"
        self.assertGreater(len(str(old_staging)), 260)
        rename = Path.rename
        staged = []

        def inspect_publish(path, destination):
            self.assertRegex(path.name, r"^\.staging-[0-9a-f]{32}$")
            self.assertLess(len(str(path / "manifest.json")), 260)
            self.assertEqual(destination, target)
            self.assertFalse(target.exists())
            staged.append(path)
            return rename(path, destination)

        with patch.object(Path, "rename", inspect_publish):
            manifest = self.publish()
        self.assertEqual(len(staged), 1)
        self.assertFalse(staged[0].exists())
        self.assertEqual(self.root / manifest["directory"], target)
        body = target / manifest["files"][0]["path"]
        self.assertEqual(body.read_bytes(), self.artifacts[0]["content"].encode())
        before = (body.stat().st_mtime_ns, body.read_bytes())
        self.assertEqual(self.publish(), manifest)
        self.assertEqual((body.stat().st_mtime_ns, body.read_bytes()), before)

    def test_staging_uuid_collision_does_not_overwrite_existing_directory(self):
        staging = self.target().parent / f".staging-{'0' * 32}"
        staging.mkdir(parents=True)
        sentinel = staging / "keep.txt"
        sentinel.write_bytes(b"existing data")
        with patch.object(store.uuid, "uuid4", return_value=SimpleNamespace(hex="0" * 32)):
            with self.assertRaises(store.ArtifactStoreError):
                self.publish()
        self.assertFalse(self.target().exists())
        self.assertEqual(list(staging.iterdir()), [sentinel])
        self.assertEqual(sentinel.read_bytes(), b"existing data")
        self.publish()
        self.assertEqual(sentinel.read_bytes(), b"existing data")

    def test_missing_filename_has_deterministic_safe_fallback(self):
        del self.artifacts[0]["filename"]
        self.artifacts[0]["format"] = "markdown"
        self.assertEqual(self.publish()["files"][0]["path"], "files/artifact-10.md")

    def test_reused_submission_binds_prior_events_without_claiming_new_attempt_ownership(self):
        self.submission.update(attempt_id="attempt-two", reused_from=20)
        self.submission["digest"] = digest({k: v for k, v in self.submission.items() if k != "digest"})
        self.submission["seq"] = 30
        self.assertEqual(self.publish()["files"][0]["seq"], 10)
        self.artifacts[0]["work_item_id"] = "other-work"
        with self.assertRaisesRegex(store.ArtifactStoreError, "belong"):
            self.publish()

    def test_hash_membership_and_ownership_are_revalidated_before_any_writes(self):
        original = fixture()
        mutations = [
            lambda s, a: a[0].update(content="changed"),
            lambda s, a: a[0].update(content_sha256="0" * 64),
            lambda s, a: a[0].update(content_bytes=0),
            lambda s, a: a[0].update(work_item_id="other"),
            lambda s, a: a[0].update(attempt_id="other"),
            lambda s, a: a[0].update(task_id="other"),
            lambda s, a: s.update(task_id="other"),
            lambda s, a: s.update(digest="0" * 64),
            lambda s, a: a.append(copy.deepcopy(a[0])),
            lambda s, a: a.clear(),
            lambda s, a: a[0].update(seq=True),
            lambda s, a: s["hashes"].update({"10": "0" * 64}),
        ]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                self.submission, self.artifacts = copy.deepcopy(original)
                mutate(self.submission, self.artifacts)
                with self.assertRaises(store.ArtifactStoreError):
                    self.publish()
                self.assertFalse(self.root.exists())

    def test_unsafe_names_are_rejected_not_sanitized(self):
        for name in ("../escape.py", "sub/file.py", "sub\\file.py", "C:\\escape.py", "/absolute.py", "name:stream",
                     "CON", "nul.txt", "COM1.py", "LPT9", "COM\u00b9.txt", "CONIN$", "conout$.txt", "NUL .txt",
                     "trailing.", "trailing ", " leading", ".", "..", "a\0b", "a\nb", "\ud800", "a\uff0fb"):
            with self.subTest(name=name):
                self.artifacts[0]["filename"] = name
                with self.assertRaises(store.ArtifactStoreError):
                    self.publish()
        self.assertFalse(self.root.exists())
        self.assertFalse((self.base / "escape.py").exists())
        for task in ("../task", "CON", "a/b"):
            with self.assertRaises(store.ArtifactStoreError):
                store.materialize(self.root, task, *fixture())
        with self.assertRaises(store.ArtifactStoreError):
            store.materialize(self.root / ".." / "outside", "task", *fixture())

    def test_case_and_unicode_collisions_are_rejected_before_writing(self):
        for names in (("Readme.md", "README.md"), ("caf\u00e9.txt", "cafe\u0301.txt"),
                      ("Stra\u00dfe.txt", "STRASSE.txt"), ("\uff21.txt", "A.txt")):
            with self.subTest(names=names):
                self.submission, self.artifacts = fixture(names)
                with self.assertRaisesRegex(store.ArtifactStoreError, "collision"):
                    self.publish()
                self.assertFalse(self.root.exists())

    def test_existing_body_manifest_or_extra_entries_cannot_be_repaired(self):
        for damage in ("body", "manifest", "extra", "missing", "filename"):
            with self.subTest(damage=damage):
                self.root = self.base / damage
                self.submission, self.artifacts = fixture()
                manifest = self.publish()
                version = self.root / manifest["directory"]
                body = version / manifest["files"][0]["path"]
                if damage == "body": body.write_bytes(b"tampered")
                elif damage == "manifest": (version / "manifest.json").write_bytes(b"{}")
                elif damage == "extra": (version / "files" / "unmanaged.txt").write_bytes(b"user file")
                elif damage == "missing": body.unlink()
                else: self.artifacts[0]["filename"] = "renamed.py"
                before = {str(p): p.read_bytes() for p in version.rglob("*") if p.is_file()}
                with self.assertRaises(store.ArtifactStoreError): self.publish()
                self.assertEqual(before, {str(p): p.read_bytes() for p in version.rglob("*") if p.is_file()})

    def test_preexisting_half_version_or_file_is_not_overwritten_or_deleted(self):
        for kind in ("empty", "partial", "file"):
            with self.subTest(kind=kind):
                self.root = self.base / kind
                target = self.target()
                target.parent.mkdir(parents=True)
                if kind == "file": target.write_bytes(b"user data")
                else:
                    target.mkdir()
                    if kind == "partial": (target / "keep.txt").write_bytes(b"user data")
                with self.assertRaises(store.ArtifactStoreError): self.publish()
                self.assertTrue(target.exists())
                if kind == "file": self.assertEqual(target.read_bytes(), b"user data")
                if kind == "partial": self.assertEqual((target / "keep.txt").read_bytes(), b"user data")

    def test_interruption_keeps_only_staging_and_retry_publishes_new_complete_version(self):
        write = store._write
        def interrupt(path, body):
            if path.name == "manifest.json": raise KeyboardInterrupt()
            write(path, body)
        with patch.object(store, "_write", side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt): self.publish()
        self.assertFalse(self.target().exists())
        abandoned = list(self.target().parent.glob(".staging-*"))
        self.assertEqual(len(abandoned), 1)
        sentinel = abandoned[0] / "keep-user-data.txt"
        sentinel.write_bytes(b"do not remove")
        self.publish()
        self.assertEqual(sentinel.read_bytes(), b"do not remove")
        self.assertTrue((self.target() / "manifest.json").is_file())

    def test_atomic_publish_target_is_absent_until_complete_and_publish_failure_is_retryable(self):
        original = Path.rename
        def inspect_publish(path, target):
            self.assertFalse(Path(target).exists())
            self.assertTrue((path / "files" / "main.py").is_file())
            self.assertTrue((path / "manifest.json").is_file())
            raise OSError("injected publish failure")
        with patch.object(Path, "rename", inspect_publish):
            with self.assertRaisesRegex(store.ArtifactStoreError, "publish failure"): self.publish()
        self.assertFalse(self.target().exists())
        self.assertIs(Path.rename, original)
        self.publish()

    def test_thread_publishers_agree_and_conflicting_filenames_do_not_overwrite(self):
        barrier = threading.Barrier(4)
        def publish(_):
            barrier.wait(10)
            return self.publish()
        with ThreadPoolExecutor(max_workers=4) as pool:
            manifests = list(pool.map(publish, range(4)))
        self.assertTrue(all(m == manifests[0] for m in manifests))
        self.assertEqual(list(self.target().parent.glob(".staging-*")), [])
        self.artifacts[0]["filename"] = "different.py"
        with self.assertRaises(store.ArtifactStoreError): self.publish()
        self.assertTrue((self.target() / "files" / "main.py").is_file())

    def test_process_publishers_share_os_lock(self):
        context = multiprocessing.get_context("spawn")
        start, results = context.Event(), context.Queue()
        processes = [context.Process(target=publish_process, args=(str(self.root), self.submission, self.artifacts, start, results)) for _ in range(3)]
        try:
            for process in processes: process.start()
            start.set()
            received = [results.get(timeout=30) for _ in processes]
            for process in processes:
                process.join(10)
                self.assertEqual(process.exitcode, 0)
            self.assertTrue(all(state == "ok" for state, _ in received), received)
            self.assertTrue(all(value == received[0][1] for _, value in received))
        finally:
            for process in processes:
                if process.is_alive(): process.terminate(); process.join(5)
            results.close()

    def test_conflicting_concurrent_publishers_have_one_winner_without_replacement(self):
        barrier = threading.Barrier(2)
        def publish(filename):
            artifacts = copy.deepcopy(self.artifacts)
            artifacts[0]["filename"] = filename
            barrier.wait(10)
            try:
                return store.materialize(self.root, "task", self.submission, artifacts)
            except store.ArtifactStoreError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(publish, ("first.py", "second.py")))
        winners = [result for result in results if result is not None]
        self.assertEqual(len(winners), 1)
        manifest = winners[0]
        self.assertEqual(json.loads((self.target() / "manifest.json").read_text()), manifest)
        self.assertEqual({p.name for p in (self.target() / "files").iterdir()},
                         {Path(manifest["files"][0]["path"]).name})

    def make_symlink(self, link, target, directory):
        try: link.symlink_to(target, target_is_directory=directory)
        except OSError as exc: self.skipTest(f"symlink privilege unavailable: {exc}")

    def test_symlink_root_ancestor_and_version_are_rejected(self):
        outside = self.base / "outside"
        outside.mkdir()
        link = self.base / "link"
        self.make_symlink(link, outside, True)
        for root in (link, link / "nested"):
            with self.assertRaisesRegex(store.ArtifactStoreError, "symlink|reparse"):
                store.materialize(root, "task", self.submission, self.artifacts)
        self.target().parent.mkdir(parents=True)
        self.make_symlink(self.target(), outside, True)
        with self.assertRaisesRegex(store.ArtifactStoreError, "symlink|reparse"): self.publish()
        self.assertEqual(list(outside.iterdir()), [])

    def test_symlink_artifact_and_lock_are_rejected(self):
        manifest = self.publish()
        body = self.root / manifest["directory"] / manifest["files"][0]["path"]
        outside = self.base / "outside.py"
        outside.write_bytes(body.read_bytes())
        body.unlink()
        self.make_symlink(body, outside, False)
        with self.assertRaisesRegex(store.ArtifactStoreError, "symlink|reparse"): self.publish()
        lock = self.target().parent / ".locks" / f"{self.submission['digest']}.lock"
        lock.unlink()
        self.make_symlink(lock, outside, False)
        with self.assertRaisesRegex(store.ArtifactStoreError, "symlink|reparse"): self.publish()

    def test_hard_link_content_is_rejected(self):
        manifest = self.publish()
        body = self.root / manifest["directory"] / manifest["files"][0]["path"]
        os.link(body, self.base / "outside.py")
        with self.assertRaisesRegex(store.ArtifactStoreError, "hard-linked"): self.publish()

    def test_reparse_attribute_is_rejected_even_when_not_a_symlink(self):
        fake = SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_file_attributes=0x400)
        with patch.object(Path, "lstat", return_value=fake):
            with self.assertRaisesRegex(store.ArtifactStoreError, "reparse"):
                store._inspect(self.root, True)

    def test_symlink_mode_is_rejected_without_relying_on_creation_privileges(self):
        fake = SimpleNamespace(st_mode=stat.S_IFLNK | 0o700, st_file_attributes=0)
        with patch.object(Path, "lstat", return_value=fake):
            for directory in (True, False):
                with self.assertRaisesRegex(store.ArtifactStoreError, "symlink"):
                    store._inspect(self.root, directory)

    @unittest.skipUnless(os.name == "nt", "Windows junction test")
    def test_actual_windows_junction_root_and_files_directory_are_rejected(self):
        import _winapi
        outside = self.base / "junction-target"
        outside.mkdir()
        junction = self.base / "junction"
        _winapi.CreateJunction(str(outside), str(junction))
        with self.assertRaisesRegex(store.ArtifactStoreError, "reparse"):
            store.materialize(junction / "nested", "task", self.submission, self.artifacts)
        self.publish()
        files = self.target() / "files"
        (files / "main.py").unlink()
        files.rmdir()
        _winapi.CreateJunction(str(outside), str(files))
        with self.assertRaisesRegex(store.ArtifactStoreError, "reparse"): self.publish()
        self.assertEqual(list(outside.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
