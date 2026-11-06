"""Windows long paths must retain immutable storage checks, not skip them."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import multiprocessing
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from cater.artifact_store import (ArtifactStoreError, _extended_windows_path, _legacy_directory,
                                  _lock, _prepare, _same_materialized_receipt, _validated_root,
                                  materialize, materialize_team_artifact)
from test_artifact_store import fixture, publish_process


def acquire_alias_lock(path, attempting, entered, errors):
    try:
        import msvcrt

        locking = msvcrt.locking

        def observe_lock(fd, mode, count):
            if mode == msvcrt.LK_LOCK:
                attempting.set()
            return locking(fd, mode, count)

        with patch.object(msvcrt, "locking", side_effect=observe_lock):
            with _lock(Path(path)):
                entered.set()
    except Exception as exc:
        errors.put((type(exc).__name__, str(exc)))


def assert_alias_lock_contends(test, first, second):
    test.assertTrue(os.path.samefile(first, second))
    context = multiprocessing.get_context("spawn")
    attempting, entered, errors = context.Event(), context.Event(), context.Queue()
    process = context.Process(target=acquire_alias_lock, args=(str(second), attempting, entered, errors))
    try:
        with _lock(first):
            process.start()
            test.assertTrue(attempting.wait(10), "child did not attempt lock")
            test.assertFalse(entered.wait(0.5), "alias bypassed the held file lock")
            test.assertTrue(process.is_alive(), "child exited instead of waiting for the lock")
        test.assertTrue(entered.wait(12), "child did not acquire after release")
        process.join(10)
        test.assertEqual(process.exitcode, 0)
        test.assertTrue(errors.empty())
    finally:
        if process.is_alive():
            process.terminate()
            process.join(5)
        errors.close()


@unittest.skipUnless(os.name == "nt", "Windows namespace regression")
class LegacyArtifactLongPathTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="cater-legacy-long-"))
        self.addCleanup(shutil.rmtree, _extended_windows_path(self.base))
        self.submission, self.artifacts = fixture(names=("exact.md",), content="Exact legacy bytes\n")
        self.task = "task"

    def long_root(self):
        task_dir = "task-" + hashlib.sha256(self.task.encode("utf-8")).hexdigest()
        suffix = Path(task_dir) / ".locks" / (self.submission["digest"] + ".lock")
        padding = 260 - len(str(self.base / suffix).encode("utf-16-le")) // 2 - 1
        self.assertGreater(padding, 0, "temporary base is too long for the boundary fixture")
        root = self.base / ("r" * padding)
        self.assertEqual(len(str(root / suffix).encode("utf-16-le")) // 2, 260)
        return root

    def test_exact_boundary_atomic_publish_read_and_repeat(self):
        root = self.long_root()
        rename = Path.rename
        observed = []

        def publish(staging, target):
            self.assertTrue(str(staging).startswith("\\\\?\\"))
            self.assertEqual(staging.parent, target.parent)
            self.assertFalse(os.path.lexists(target))
            self.assertEqual((staging / "files" / "exact.md").read_bytes(), b"Exact legacy bytes\n")
            self.assertTrue((staging / "manifest.json").is_file())
            observed.append(True)
            return rename(staging, target)

        with patch.object(Path, "rename", publish):
            manifest = materialize(root, self.task, self.submission, self.artifacts)
        self.assertEqual(observed, [True])
        directory = _legacy_directory(root, manifest)
        self.assertTrue(str(directory).startswith("\\\\?\\"))
        path = directory / manifest["files"][0]["path"]
        self.assertEqual(path.read_bytes(), b"Exact legacy bytes\n")
        before = path.stat().st_mtime_ns
        self.assertEqual(materialize(root, self.task, self.submission, self.artifacts), manifest)
        self.assertEqual(path.stat().st_mtime_ns, before)

    def test_short_root_retains_normal_public_path(self):
        root = self.base / "short"
        manifest = materialize(root, self.task, self.submission, self.artifacts)
        directory = _legacy_directory(root, manifest)
        self.assertFalse(str(directory).startswith("\\\\?\\"))
        self.assertEqual((directory / manifest["files"][0]["path"]).read_bytes(), b"Exact legacy bytes\n")

    def test_short_normal_and_extended_roots_share_file_lock_both_directions(self):
        root = self.base / "short"
        manifest = materialize(root, self.task, self.submission, self.artifacts)
        normal = _legacy_directory(root, manifest)
        extended = _legacy_directory(_extended_windows_path(root), manifest)
        self.assertFalse(str(normal).startswith("\\\\?\\"))
        self.assertEqual(normal, extended)
        self.assertTrue(os.path.samefile(normal, extended))
        lock_name = self.submission["digest"] + ".lock"
        plain_lock = normal.parent / ".locks" / lock_name
        extended_lock = _extended_windows_path(plain_lock)
        assert_alias_lock_contends(self, plain_lock, extended_lock)
        assert_alias_lock_contends(self, extended_lock, plain_lock)
        self.assertEqual(materialize(_extended_windows_path(root), self.task, self.submission, self.artifacts), manifest)

    def test_reopening_same_database_with_extended_path_does_not_duplicate_receipt(self):
        from cater.artifacts import normalize_deliverables
        from cater.events import Actor
        from cater.runtime import Runtime
        from test_delivery import DocumentWorkDriver
        from test_work import item

        driver = DocumentWorkDriver()
        drivers = {name: driver for name in ("product", "dev", "design", "test")}
        db = self.base / "work.db"
        with Runtime(db, drivers=drivers) as runtime:
            task = runtime.create_task("Persist an artifact")
            runtime.plan_work(task, [item("a")])
            claim = runtime.work.claim(task, "a")
            owner = next(row["owner"] for row in runtime.work.view(task)["items"] if row["id"] == "a")
            artifact = normalize_deliverables([{"title": "Note", "summary": "Stored", "content": "same bytes",
                                                "format": "markdown", "filename": "note.md"}])[0]
            event = runtime.log.emit(type="deliverable.published", task_id=task, actor=Actor.agent(owner, "worker"),
                payload={**artifact, "work_item_id": "a", "attempt_id": claim.payload["attempt_id"]})
            runtime.work.submit(task, claim, [event.seq])
            first, = runtime.persist_artifacts(task)
            self.assertEqual(first.type, "artifacts.materialized")
            self.assertFalse(first.payload["directory"].startswith("\\\\?\\"))
        with Runtime(_extended_windows_path(db), drivers=drivers) as runtime:
            second, = runtime.persist_artifacts(task)
            self.assertEqual(second, first)
            receipts = list(runtime.log.iter_events(task_id=task,
                event_types=("artifacts.materialized", "artifacts.materialization_failed")))
            self.assertEqual(len(receipts), 1)
            self.assertEqual(Path(second.payload["files"][0]["path"]).read_bytes(), b"same bytes")

    def test_upgrade_reuses_existing_short_extended_receipt(self):
        from cater.artifacts import normalize_deliverables
        from cater.events import Actor
        from cater.runtime import Runtime
        from test_delivery import DocumentWorkDriver
        from test_work import item

        driver = DocumentWorkDriver()
        drivers = {name: driver for name in ("product", "dev", "design", "test")}
        db = self.base / "upgrade.db"
        with Runtime(_extended_windows_path(db), drivers=drivers) as runtime:
            task = runtime.create_task("Persist an artifact")
            runtime.plan_work(task, [item("a")])
            claim = runtime.work.claim(task, "a")
            owner = next(row["owner"] for row in runtime.work.view(task)["items"] if row["id"] == "a")
            artifact = normalize_deliverables([{"title": "Note", "summary": "Stored", "content": "same bytes",
                                                "format": "markdown", "filename": "note.md"}])[0]
            event = runtime.log.emit(type="deliverable.published", task_id=task, actor=Actor.agent(owner, "worker"),
                payload={**artifact, "work_item_id": "a", "attempt_id": claim.payload["attempt_id"]})
            runtime.work.submit(task, claim, [event.seq])
            # Reproduce the former producer's root/manifest path construction.
            with patch("cater.artifact_store._legacy_directory", side_effect=lambda root, manifest:
                       Path(root) / manifest["directory"]):
                first, = runtime.persist_artifacts(task)
            self.assertEqual(first.type, "artifacts.materialized")
            self.assertTrue(first.payload["directory"].startswith("\\\\?\\"))
            self.assertTrue(Path(first.payload["files"][0]["path"]).is_file())
        with Runtime(db, drivers=drivers) as runtime:
            second, = runtime.persist_artifacts(task)
            self.assertEqual(second, first)
            receipts = list(runtime.log.iter_events(task_id=task,
                event_types=("artifacts.materialized", "artifacts.materialization_failed")))
            self.assertEqual(receipts, [first])
            self.assertEqual(Path(second.payload["files"][0]["path"]).read_bytes(), b"same bytes")

    def test_combined_emoji_in_long_root_for_legacy_and_team_artifacts(self):
        prefix = self.base / "👩‍💻" / "🧑‍🔬"
        prefix.mkdir(parents=True)
        self.assertTrue(prefix.is_dir())
        task_dir = "task-" + hashlib.sha256(self.task.encode("utf-8")).hexdigest()
        suffix = Path(task_dir) / ".locks" / (self.submission["digest"] + ".lock")
        padding = 260 - len(str(prefix / suffix).encode("utf-16-le")) // 2 - 1
        self.assertGreater(padding, 0)
        root = prefix / ("n" * padding)
        self.assertEqual(len(str(root / suffix).encode("utf-16-le")) // 2, 260)
        manifest = materialize(root, self.task, self.submission, self.artifacts)
        directory = _legacy_directory(root, manifest)
        self.assertTrue(str(directory).startswith("\\\\?\\"))
        self.assertEqual((directory / manifest["files"][0]["path"]).read_bytes(), b"Exact legacy bytes\n")
        self.assertEqual(materialize(_extended_windows_path(root), self.task, self.submission, self.artifacts), manifest)
        body = "Team content\n"
        artifact = {"seq": 123, "filename": "note.md", "content": body,
                    "content_sha256": hashlib.sha256(body.encode()).hexdigest(), "format": "markdown"}
        saved = materialize_team_artifact(root, self.task, artifact)
        self.assertEqual(Path(saved).read_bytes(), body.encode())
        self.assertEqual(materialize_team_artifact(_extended_windows_path(root), self.task, artifact), saved)

    def test_windows_valid_format_and_other_unicode_in_artifact_roots(self):
        names = ("می\u200cروم", "text\u200eexample", "a\u2060b", "a\u0085b", "a\ue000b", "a\ud800b")
        body = "Unicode root content\n"
        artifact = {"seq": 123, "filename": "note.md", "content": body,
                    "content_sha256": hashlib.sha256(body.encode()).hexdigest(), "format": "markdown"}
        for number, name in enumerate(names):
            with self.subTest(name=ascii(name)):
                prefix = self.base / ("case-" + str(number)) / name
                prefix.mkdir(parents=True)
                self.assertTrue(prefix.is_dir())
                # Exercise both normal I/O and the extended long-path namespace.
                for root in (prefix, prefix / ("a" * 70) / ("b" * 70)):
                    manifest = materialize(root, self.task, self.submission, self.artifacts)
                    directory = _legacy_directory(root, manifest)
                    self.assertEqual((directory / manifest["files"][0]["path"]).read_bytes(), b"Exact legacy bytes\n")
                    saved = materialize_team_artifact(root, self.task, artifact)
                    self.assertEqual(Path(saved).read_bytes(), body.encode())

    def test_nfkc_distinct_windows_root_names_are_usable(self):
        body = "Unicode root content\n"
        artifact = {"seq": 123, "filename": "note.md", "content": body,
                    "content_sha256": hashlib.sha256(body.encode()).hexdigest(), "format": "markdown"}
        for name in ("合法／目录", "ｃｏｎ", "fullwidth：name", "end．"):
            with self.subTest(name=name):
                prefix = self.base / name
                prefix.mkdir()
                for root in (prefix, prefix / ("a" * 70) / ("b" * 70)):
                    manifest = materialize(root, self.task, self.submission, self.artifacts)
                    path = _legacy_directory(root, manifest) / manifest["files"][0]["path"]
                    self.assertEqual(path.read_bytes(), b"Exact legacy bytes\n")
                    self.assertEqual(materialize(_extended_windows_path(root), self.task,
                                                 self.submission, self.artifacts), manifest)
                    self.assertEqual(Path(materialize_team_artifact(root, self.task, artifact)).read_bytes(), body.encode())

    def test_case_alias_existing_receipt_is_reused(self):
        from cater.artifacts import normalize_deliverables
        from cater.events import Actor
        from cater.runtime import Runtime
        from test_delivery import DocumentWorkDriver
        from test_work import item

        driver = DocumentWorkDriver()
        drivers = {name: driver for name in ("product", "dev", "design", "test")}
        db = self.base / "case.db"
        with Runtime(db, drivers=drivers) as runtime:
            task = runtime.create_task("Persist an artifact")
            runtime.plan_work(task, [item("a")])
            claim = runtime.work.claim(task, "a")
            owner = next(row["owner"] for row in runtime.work.view(task)["items"] if row["id"] == "a")
            artifact = normalize_deliverables([{"title": "Note", "summary": "Stored", "content": "same bytes",
                                                "format": "markdown", "filename": "note.md"}])[0]
            event = runtime.log.emit(type="deliverable.published", task_id=task, actor=Actor.agent(owner, "worker"),
                payload={**artifact, "work_item_id": "a", "attempt_id": claim.payload["attempt_id"]})
            runtime.work.submit(task, claim, [event.seq])
            first, = runtime.persist_artifacts(task)
        alias = Path(str(db).upper())
        if not os.path.samefile(db, alias):
            self.skipTest("Database path is case sensitive")
        with Runtime(alias, drivers=drivers) as runtime:
            def compare_without_transaction(left, right):
                self.assertFalse(runtime.log.db.in_transaction)
                return _same_materialized_receipt(left, right)
            with patch("cater.artifact_store._same_materialized_receipt", side_effect=compare_without_transaction) as compare:
                second, = runtime.persist_artifacts(task)
            self.assertEqual(compare.call_count, 1)
            self.assertEqual(second, first)
            receipts = list(runtime.log.iter_events(task_id=task,
                event_types=("artifacts.materialized", "artifacts.materialization_failed")))
            self.assertEqual(receipts, [first])
            self.assertEqual(Path(second.payload["files"][0]["path"]).read_bytes(), b"same bytes")
            alternate = {**first.payload, "directory": first.payload["directory"].upper(), "files": [
                {**entry, "path": entry["path"].upper()} for entry in first.payload["files"]]}
            self.assertTrue(_same_materialized_receipt(first.payload, alternate))
            with patch("cater.artifact_store._open", side_effect=PermissionError("unavailable")):
                self.assertFalse(_same_materialized_receipt(first.payload, alternate))
            changed = {**alternate, "submission_seq": alternate["submission_seq"] + 1}
            self.assertFalse(_same_materialized_receipt(first.payload, changed))
            Path(first.payload["files"][0]["path"]).write_bytes(b"tampered")
            failure, = runtime.persist_artifacts(task)
            self.assertEqual(failure.type, "artifacts.materialization_failed")
            self.assertIn("tampering", failure.payload["reason"])

    def test_old_receipt_namespace_equivalence_preserves_case_and_binding(self):
        directory = str(self.base / "CaseSensitive" / "version")
        receipt = {"submission_seq": 1, "submission_digest": "digest", "directory": directory,
                   "files": [{"seq": 2, "content_sha256": "hash", "path": directory + "\\files\\a.md"}]}
        old = {**receipt, "directory": "\\\\?\\" + directory,
               "files": [{**receipt["files"][0], "path": "\\\\?\\" + receipt["files"][0]["path"]}]}
        self.assertTrue(_same_materialized_receipt(old, receipt))
        self.assertFalse(_same_materialized_receipt({**old, "submission_seq": 3}, receipt))
        self.assertFalse(_same_materialized_receipt({**old, "files": [{**old["files"][0],
                                                                        "content_sha256": "other"}]}, receipt))
        self.assertFalse(_same_materialized_receipt({**old, "directory": old["directory"].replace(
            "CaseSensitive", "casesensitive")}, receipt))
        self.assertFalse(_same_materialized_receipt({**old, "files": [{**old["files"][0],
            "path": old["files"][0]["path"].replace("CaseSensitive", "casesensitive")}]}, receipt))
        self.assertFalse(_same_materialized_receipt({**old, "directory": old["directory"] + "\\..\\version"}, receipt))
        self.assertFalse(_same_materialized_receipt({**old, "unexpected": True}, receipt))

    def test_case_sensitive_roots_remain_distinct(self):
        parent = self.base / "case-sensitive"
        parent.mkdir()
        result = subprocess.run(["fsutil.exe", "file", "setCaseSensitiveInfo", str(parent), "enable"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            self.skipTest("Filesystem does not allow enabling case sensitivity")
        upper, lower = parent / "Root", parent / "root"
        upper_manifest = materialize(upper, self.task, self.submission, self.artifacts)
        lower_manifest = materialize(lower, self.task, self.submission, self.artifacts)
        upper_dir = _legacy_directory(upper, upper_manifest)
        lower_dir = _legacy_directory(lower, lower_manifest)
        self.assertFalse(os.path.samefile(upper_dir, lower_dir))
        def receipt(directory):
            return {"directory": str(directory), "files": [
                {"path": str(directory / upper_manifest["files"][0]["path"]), "content_sha256": "hash"}]}
        self.assertFalse(_same_materialized_receipt(receipt(upper_dir), receipt(lower_dir)))
        self.assertTrue(_same_materialized_receipt(receipt(_extended_windows_path(upper_dir)), receipt(upper_dir)))

    def test_case_alias_receipt_rejects_junction_and_hardlink(self):
        import _winapi

        parent = self.base / "case-sensitive-guard"
        parent.mkdir()
        result = subprocess.run(["fsutil.exe", "file", "setCaseSensitiveInfo", str(parent), "enable"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            self.skipTest("Filesystem does not allow enabling case sensitivity")
        upper, lower = parent / "Root", parent / "root"
        manifest = materialize(upper, self.task, self.submission, self.artifacts)
        directory = _legacy_directory(upper, manifest)
        path = directory / manifest["files"][0]["path"]
        new = {"directory": str(directory), "files": [{"path": str(path), "seq": 10}]}
        _winapi.CreateJunction(str(upper), str(lower))
        try:
            alias_dir = lower / directory.relative_to(upper)
            alias_file = alias_dir / manifest["files"][0]["path"]
            old = {"directory": str(alias_dir), "files": [{"path": str(alias_file), "seq": 10}]}
            self.assertTrue(os.path.samefile(alias_dir, directory))
            self.assertFalse(_same_materialized_receipt(old, new))
        finally:
            os.rmdir(lower)

        files = directory / "files"
        result = subprocess.run(["fsutil.exe", "file", "setCaseSensitiveInfo", str(files), "enable"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            self.skipTest("Filesystem does not allow enabling case sensitivity for files")
        hardlink = files / "EXACT.MD"
        os.link(path, hardlink)
        try:
            self.assertTrue(os.path.samefile(path, hardlink))
            old = {"directory": str(directory), "files": [{"path": str(hardlink), "seq": 10}]}
            self.assertFalse(_same_materialized_receipt(old, new))
        finally:
            hardlink.unlink()

    def test_old_unc_receipt_namespace_equivalence(self):
        directory = "\\\\server\\share\\version"
        plain = {"directory": directory, "files": [{"path": directory + "\\files\\a.md", "seq": 1}]}
        old = {"directory": "\\\\?\\UNC\\server\\share\\version", "files": [
            {"path": "\\\\?\\UNC\\server\\share\\version\\files\\a.md", "seq": 1}]}
        self.assertTrue(_same_materialized_receipt(old, plain))

    def test_case_alias_uses_same_lock_when_filesystem_is_case_insensitive(self):
        root = self.base / "case-root"
        manifest = materialize(root, self.task, self.submission, self.artifacts)
        alias = Path(str(root).upper())
        try:
            equivalent = os.path.samefile(root, alias)
        except FileNotFoundError:
            equivalent = False
        if not equivalent:
            self.skipTest("Directory uses case-sensitive names")
        normal = _legacy_directory(root, manifest)
        alternate = _legacy_directory(alias, manifest)
        lock_name = self.submission["digest"] + ".lock"
        assert_alias_lock_contends(self, normal.parent / ".locks" / lock_name,
                                   alternate.parent / ".locks" / lock_name)
        self.assertEqual(materialize(alias, self.task, self.submission, self.artifacts), manifest)

    def test_junction_root_is_rejected_in_both_namespaces(self):
        import _winapi

        root = self.base / "real-root"
        manifest = materialize(root, self.task, self.submission, self.artifacts)
        link = self.base / "linked-root"
        _winapi.CreateJunction(str(root), str(link))
        try:
            for alias in (link, _extended_windows_path(link)):
                with self.subTest(alias=str(alias)), self.assertRaisesRegex(ArtifactStoreError, "reparse"):
                    materialize(alias, self.task, self.submission, self.artifacts)
            self.assertEqual(materialize(root, self.task, self.submission, self.artifacts), manifest)
        finally:
            os.rmdir(link)

    def test_absolute_store_root_itself_can_exceed_max_path(self):
        root = self.base / ("a" * 120) / ("b" * 120) / "store"
        self.assertGreater(len(str(root).encode("utf-16-le")) // 2, 260)
        manifest = materialize(root, self.task, self.submission, self.artifacts)
        directory = _legacy_directory(root, manifest)
        self.assertEqual((directory / manifest["files"][0]["path"]).read_bytes(), b"Exact legacy bytes\n")
        self.assertEqual(materialize(root, self.task, self.submission, self.artifacts), manifest)

    def test_long_unc_path_uses_unc_namespace_without_network_access(self):
        manifest, _ = _prepare(self.task, self.submission, self.artifacts)
        root = Path("\\\\server\\share") / ("r" * 120)
        directory = _legacy_directory(root, manifest)
        self.assertTrue(str(directory).startswith("\\\\?\\UNC\\server\\share\\"))
        extended = Path("\\\\?\\UNC\\server\\share") / ("r" * 120)
        self.assertEqual(_legacy_directory(extended, manifest), directory)

    def test_legacy_length_counts_utf16_units(self):
        self.submission, self.artifacts = fixture(names=("🐈" * 20 + ".md",))
        manifest, _ = _prepare(self.task, self.submission, self.artifacts)
        suffix = Path(manifest["directory"]) / manifest["files"][0]["path"]
        padding = 250 - len(str(self.base / suffix)) - 1
        self.assertGreater(padding, 0)
        root = self.base / ("u" * padding)
        target = root / suffix
        self.assertEqual(len(str(target)), 250)
        self.assertGreaterEqual(len(str(target).encode("utf-16-le")) // 2, 260)
        result = materialize(root, self.task, self.submission, self.artifacts)
        directory = _legacy_directory(root, result)
        self.assertTrue(str(directory).startswith("\\\\?\\"))
        self.assertEqual((directory / result["files"][0]["path"]).read_bytes(),
                         self.artifacts[0]["content"].encode())

    def test_non_bmp_directory_segments_count_utf16_for_lock_and_version(self):
        segments = ("🐈" * 5, "🐾" * 5, "🧭" * 5)
        prefix = self.base.joinpath(*segments)
        task_dir = "task-" + hashlib.sha256(self.task.encode("utf-8")).hexdigest()
        suffix = Path(task_dir) / ".locks" / (self.submission["digest"] + ".lock")
        length = len(str(prefix / suffix).encode("utf-16-le")) // 2
        padding = 260 - length - 1
        self.assertGreater(padding, 0)
        root = prefix / ("n" * padding)
        plain_lock = root / suffix
        self.assertLess(len(str(plain_lock)), 260)
        self.assertEqual(len(str(plain_lock).encode("utf-16-le")) // 2, 260)
        self.assertTrue(all(len(part.encode("utf-8")) <= 240 for part in segments))

        manifest = materialize(root, self.task, self.submission, self.artifacts)
        directory = _legacy_directory(root, manifest)
        alias_directory = _legacy_directory(_extended_windows_path(root), manifest)
        self.assertEqual(directory, alias_directory)
        self.assertTrue(str(directory).startswith("\\\\?\\"))
        lock = directory.parent / ".locks" / (self.submission["digest"] + ".lock")
        self.assertEqual(lock, _extended_windows_path(plain_lock))
        self.assertTrue(lock.is_file())
        self.assertEqual((directory / manifest["files"][0]["path"]).read_bytes(), b"Exact legacy bytes\n")
        self.assertEqual(materialize(_extended_windows_path(root), self.task, self.submission, self.artifacts), manifest)
        assert_alias_lock_contends(self, lock, alias_directory.parent / ".locks" / lock.name)

    def test_long_root_rejects_junction_lock_directory_and_version_files(self):
        import _winapi

        root = self.long_root()
        outside = self.base / "outside"
        outside.mkdir()
        task_dir = root / ("task-" + hashlib.sha256(self.task.encode()).hexdigest())
        task_dir.mkdir(parents=True)
        locks = task_dir / ".locks"
        _winapi.CreateJunction(str(outside), str(locks))
        try:
            with self.assertRaisesRegex(ArtifactStoreError, "reparse"):
                materialize(root, self.task, self.submission, self.artifacts)
            self.assertEqual(list(outside.iterdir()), [])
        finally:
            os.rmdir(locks)

        manifest = materialize(root, self.task, self.submission, self.artifacts)
        directory = _legacy_directory(root, manifest)
        file_path = directory / manifest["files"][0]["path"]
        file_path.unlink()
        files = directory / "files"
        files.rmdir()
        _winapi.CreateJunction(str(outside), str(files))
        try:
            with self.assertRaisesRegex(ArtifactStoreError, "reparse"):
                materialize(root, self.task, self.submission, self.artifacts)
            self.assertEqual(list(outside.iterdir()), [])
        finally:
            os.rmdir(files)

    def test_long_root_rejects_hardlinked_lock_and_tampered_contents(self):
        root = self.long_root()
        manifest = materialize(root, self.task, self.submission, self.artifacts)
        directory = _legacy_directory(root, manifest)
        path = directory / manifest["files"][0]["path"]
        path.write_bytes(b"tampered")
        with self.assertRaisesRegex(ArtifactStoreError, "tampering"):
            materialize(root, self.task, self.submission, self.artifacts)
        path.write_bytes(b"Exact legacy bytes\n")
        lock = directory.parent / ".locks" / (self.submission["digest"] + ".lock")
        lock.unlink()
        outside = self.base / "outside-lock"
        outside.write_bytes(b"unchanged")
        os.link(outside, lock)
        with self.assertRaisesRegex(ArtifactStoreError, "hard-linked"):
            materialize(root, self.task, self.submission, self.artifacts)
        self.assertEqual(outside.read_bytes(), b"unchanged")

    def test_long_root_rejects_hardlinked_artifact(self):
        root = self.long_root()
        manifest = materialize(root, self.task, self.submission, self.artifacts)
        path = _legacy_directory(root, manifest) / manifest["files"][0]["path"]
        outside = self.base / "hardlinked-copy"
        os.link(path, outside)
        with self.assertRaisesRegex(ArtifactStoreError, "hard-linked"):
            materialize(root, self.task, self.submission, self.artifacts)
        self.assertEqual(outside.read_bytes(), b"Exact legacy bytes\n")

    def test_long_root_concurrent_publishers_share_one_version(self):
        root = self.long_root()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: materialize(root, self.task, self.submission, self.artifacts), range(4)))
        self.assertTrue(all(result == results[0] for result in results))
        directory = _legacy_directory(root, results[0])
        self.assertEqual((directory / results[0]["files"][0]["path"]).read_bytes(), b"Exact legacy bytes\n")
        self.assertEqual(list(directory.parent.glob(".staging-*")), [])

    def test_long_root_process_publishers_share_os_lock(self):
        root = self.long_root()
        context = multiprocessing.get_context("spawn")
        start, results = context.Event(), context.Queue()
        processes = [context.Process(target=publish_process,
                     args=(str(root), self.submission, self.artifacts, start, results)) for _ in range(3)]
        try:
            for process in processes:
                process.start()
            start.set()
            received = [results.get(timeout=30) for _ in processes]
            for process in processes:
                process.join(10)
                self.assertEqual(process.exitcode, 0)
            self.assertTrue(all(status == "ok" for status, _ in received), received)
            self.assertTrue(all(result == received[0][1] for _, result in received))
            directory = _legacy_directory(root, received[0][1])
            self.assertEqual(list(directory.parent.glob(".staging-*")), [])
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(5)
            results.close()

    def test_normalized_absolute_root_is_revalidated(self):
        invalid_absolute = self.base / "unsafe." / "store"
        with patch("cater.artifact_store.os.path.abspath", return_value=str(invalid_absolute)):
            with self.assertRaisesRegex(ArtifactStoreError, "unsafe artifact root"):
                _validated_root("relative-store")

    def test_unsafe_roots_are_rejected_before_conversion(self):
        for root in (self.base / "link" / ".." / "store", self.base / "trailing." / "store",
                     self.base / "trailing " / "store", self.base / "NUL" / "store",
                     self.base / "COM¹.txt" / "store", self.base / "LPT²" / "store",
                     self.base / "control\x01" / "store", self.base / "control\x1f" / "store",
                     Path("\\\\.\\C:\\store"), Path("\\\\?\\GLOBALROOT\\Device\\example"),
                     Path("C:relative"), Path("\\\\server.\\share\\store"),
                     Path("\\\\server\\share.\\store")):
            with self.subTest(root=str(root)), self.assertRaises(ArtifactStoreError):
                materialize(root, self.task, self.submission, self.artifacts)

    def test_extended_root_raw_syntax_rejected_before_path_normalization(self):
        prefix = str(_extended_windows_path(self.base))
        artifact = {"seq": 123, "filename": "note.md", "content": "test",
                    "content_sha256": hashlib.sha256(b"test").hexdigest(), "format": "markdown"}
        targets = ("dot-target", "slash-target", "duplicate-target", "parent-target")
        for target in targets:
            (self.base / target).mkdir()
        for raw in (prefix + "\\.\\dot-target", prefix + "/slash-target",
                    prefix + "\\\\duplicate-target", prefix + "\\missing\\..\\parent-target"):
            with self.subTest(root=raw):
                self.assertFalse(os.path.isdir(raw))
                with self.assertRaises(ArtifactStoreError):
                    materialize(raw, self.task, self.submission, self.artifacts)
                with self.assertRaises(ArtifactStoreError):
                    materialize_team_artifact(raw, self.task, artifact)
        self.assertTrue(all(not list((self.base / target).iterdir()) for target in targets))
        for raw in ("\\\\?\\UNC\\server\\share\\.\\root", "\\\\?\\UNC\\server\\share/root"):
            with self.subTest(root=raw), self.assertRaises(ArtifactStoreError):
                _validated_root(raw)
        class RawPath:
            def __fspath__(self):
                return prefix + "\\.\\dot-target"
        with self.assertRaises(ArtifactStoreError):
            materialize(RawPath(), self.task, self.submission, self.artifacts)

    def test_pathlike_is_converted_only_once(self):
        base = self.base
        class ChangingPath:
            calls = 0
            def __fspath__(self):
                self.calls += 1
                return str(base / ("first" if self.calls == 1 else "second"))
        root = ChangingPath()
        manifest = materialize(root, self.task, self.submission, self.artifacts)
        self.assertEqual(root.calls, 1)
        self.assertTrue((self.base / "first" / manifest["directory"]).exists())
        self.assertFalse((self.base / "second").exists())



@unittest.skipUnless(os.name == "nt", "Windows namespace regression")
class TeamArtifactLongPathTests(unittest.TestCase):
    def setUp(self):
        base = Path(tempfile.mkdtemp(prefix="cater-long-artifact-"))
        self.addCleanup(shutil.rmtree, _extended_windows_path(base))
        self.root = base / ("nested-" + "x" * 65)
        body = "Long path content\n"
        self.artifact = {"seq": 123, "filename": "CONTRIBUTING_CHECKLIST.md", "content": body,
                         "content_sha256": hashlib.sha256(body.encode()).hexdigest(), "format": "markdown"}

    def test_long_path_publication_read_and_idempotence(self):
        saved = materialize_team_artifact(self.root, "task", self.artifact)
        self.assertGreater(len(saved), 260)
        self.assertTrue(saved.startswith("\\\\?\\"))
        self.assertEqual(Path(saved).read_text(), self.artifact["content"])
        self.assertEqual(materialize_team_artifact(self.root, "task", self.artifact), saved)

    def test_long_path_modified_file_is_rejected(self):
        saved = materialize_team_artifact(self.root, "task", self.artifact)
        Path(saved).write_text("tampered")
        with self.assertRaises(ArtifactStoreError):
            materialize_team_artifact(self.root, "task", self.artifact)

    def test_parent_traversal_and_device_roots_rejected_before_writes(self):
        for root in (self.root.parent / "link" / ".." / "store",
                     self.root / "link" / ".." / "store",
                     Path("\\\\.\\C:\\store"), Path("\\\\.\\C:\\" + "x" * 160)):
            with self.subTest(root=str(root)), self.assertRaises(ArtifactStoreError):
                materialize_team_artifact(root, "task", self.artifact)
        self.assertEqual(list(self.root.parent.iterdir()), [])

    def test_unc_and_already_extended_path_conversion(self):
        for original, expected in (
            ("C:\\store", "\\\\?\\C:\\store"),
            ("\\\\server\\share\\store", "\\\\?\\UNC\\server\\share\\store"),
            ("\\\\?\\C:\\store", "\\\\?\\C:\\store"),
            ("\\\\?\\UNC\\server\\share\\store", "\\\\?\\UNC\\server\\share\\store"),
        ):
            self.assertEqual(str(_extended_windows_path(Path(original))), expected)

    def test_ambiguous_root_components_rejected_before_creation(self):
        for suffix in (".", " "):
            for padding in ("short", "long" * 30):
                root = self.root.parent / ("store" + suffix) / padding
                with self.subTest(root=str(root)), self.assertRaises(ArtifactStoreError):
                    materialize_team_artifact(root, "task", self.artifact)
        self.assertEqual(list(self.root.parent.iterdir()), [])

    def test_long_path_junction_ancestor_and_version_rejected(self):
        import _winapi

        outside = self.root.parent / "outside"
        outside.mkdir()
        link = self.root.parent / "junction"
        _winapi.CreateJunction(str(outside), str(link))
        self.addCleanup(os.rmdir, link)
        for root in (link / self.root.name, link):
            with self.assertRaises(ArtifactStoreError):
                materialize_team_artifact(root, "task", self.artifact)
        self.assertEqual(list(outside.iterdir()), [])
        saved = Path(materialize_team_artifact(self.root, "task", self.artifact))
        saved.unlink()
        saved.parent.rmdir()
        _winapi.CreateJunction(str(outside), str(saved.parent))
        self.addCleanup(os.rmdir, saved.parent)
        with self.assertRaises(ArtifactStoreError):
            materialize_team_artifact(self.root, "task", self.artifact)
        self.assertEqual(list(outside.iterdir()), [])

    def test_long_path_hard_link_is_rejected(self):
        saved = materialize_team_artifact(self.root, "task", self.artifact)
        os.link(saved, saved + ".link")
        with self.assertRaises(ArtifactStoreError):
            materialize_team_artifact(self.root, "task", self.artifact)

    def test_long_path_file_and_lock_symlinks_rejected(self):
        outside = self.root.parent / "outside.txt"
        outside.write_bytes(b"unchanged")
        probe = self.root.parent / "probe-link"
        try:
            probe.symlink_to(outside)
        except OSError as exc:
            if getattr(exc, "winerror", None) == 1314:
                self.skipTest("Windows symlink privilege unavailable")
            raise
        probe.unlink()
        saved = Path(materialize_team_artifact(self.root, "task", self.artifact))
        for target in (saved, saved.parent.parent / ".publish.lock"):
            original = target.read_bytes()
            target.unlink()
            try:
                for destination in (outside, outside.with_name("absent.txt")):
                    target.symlink_to(destination)
                    try:
                        with self.assertRaises((ArtifactStoreError, OSError)):
                            materialize_team_artifact(self.root, "task", self.artifact)
                        self.assertEqual(outside.read_bytes(), b"unchanged")
                        self.assertFalse(outside.with_name("absent.txt").exists())
                    finally:
                        target.unlink()
            finally:
                target.write_bytes(original)

    def test_short_paths_keep_existing_public_representation(self):
        with tempfile.TemporaryDirectory() as short:
            artifact = {**self.artifact, "filename": "a.txt"}
            saved = materialize_team_artifact(short, "task", artifact)
            self.assertFalse(saved.startswith("\\\\?\\"))

    def test_short_normal_and_extended_roots_share_publish_lock(self):
        root = self.root.parent / "short"
        saved = Path(materialize_team_artifact(root, "task", self.artifact))
        self.assertFalse(str(saved).startswith("\\\\?\\"))
        plain_lock = saved.parent.parent / ".publish.lock"
        extended_lock = _extended_windows_path(plain_lock)
        assert_alias_lock_contends(self, plain_lock, extended_lock)
        assert_alias_lock_contends(self, extended_lock, plain_lock)
        self.assertEqual(str(saved), materialize_team_artifact(
            _extended_windows_path(root), "task", self.artifact))

    def test_windows_limit_counts_utf16_units_not_python_characters(self):
        base = Path(tempfile.mkdtemp(prefix="c"))
        self.addCleanup(shutil.rmtree, _extended_windows_path(base))
        artifact = {**self.artifact, "filename": "\U0001f408" * 20 + ".md"}
        suffix = Path("team-" + hashlib.sha256(b"task").hexdigest()) / (
            "123-" + artifact["content_sha256"]) / artifact["filename"]
        padding = 247 - len(str(base / suffix)) - 1
        if padding < 1:
            self.skipTest("Temporary root too long for the UTF-16 boundary fixture")
        root = base / ("x" * padding)
        target = root / suffix
        self.assertEqual(len(str(target)), 247)
        self.assertGreater(len(str(target).encode("utf-16-le")) // 2, 260)
        saved = materialize_team_artifact(root, "task", artifact)
        self.assertTrue(saved.startswith("\\\\?\\"))
        self.assertEqual(Path(saved).read_text(), artifact["content"])

    def test_non_bmp_directory_segments_use_extended_namespace_for_team_lock(self):
        self.artifact = {**self.artifact, "filename": "a.md"}
        prefix = self.root.parent / ("🐈" * 5) / ("🐾" * 5) / ("🧭" * 5)
        suffix = Path("team-" + hashlib.sha256(b"task").hexdigest()) / (
            "123-" + self.artifact["content_sha256"]) / self.artifact["filename"]
        padding = 247 - len(str(prefix / suffix)) - 1
        self.assertGreater(padding, 0)
        root = prefix / ("t" * padding)
        plain_target = root / suffix
        self.assertEqual(len(str(plain_target)), 247)
        self.assertGreaterEqual(len(str(plain_target).encode("utf-16-le")) // 2, 260)
        saved = Path(materialize_team_artifact(root, "task", self.artifact))
        self.assertTrue(str(saved).startswith("\\\\?\\"))
        self.assertEqual(saved, _extended_windows_path(plain_target))
        self.assertEqual(saved.read_text(), self.artifact["content"])
        lock = saved.parent.parent / ".publish.lock"
        self.assertTrue(lock.is_file())
        self.assertEqual(str(saved), materialize_team_artifact(_extended_windows_path(root), "task", self.artifact))
        assert_alias_lock_contends(self, lock, _extended_windows_path(lock))
