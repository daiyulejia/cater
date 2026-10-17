"""Local scripted Runtime regressions; no real model, network, or processes."""

from __future__ import annotations

import copy
import hashlib
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import zipfile

from cater.artifact_blobs import put_blob
from cater.events import Actor
from cater.project_history import TOOL
from cater.runtime import Runtime
from cater.workspace_archive import MAX_ARCHIVE_BYTES, create_snapshot
from cater.workspace_tools import WorkspaceExecutor, WorkspacePolicy
from cater.workspaces import WorkspaceService
import test_project_history as fixtures


UNICODE_PATH = "docs/\u4f60\u597d.txt"
TEXT = "\u6c49\U0001f642abc\n" * 7
FILES = {UNICODE_PATH: TEXT.encode(), "empty.txt": b"", "binary.bin": bytes(range(256))}


class ProjectHistoryArchiveTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.ProjectHistoryTests()
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()
        self.workspace = self.f.root / "workspace"
        self.workspace.mkdir()
        for name, body in FILES.items():
            target = self.workspace / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
        self.config = self.f.root / "workspaces.json"
        self.config.write_text(json.dumps({"workspaces": [{"id": "snapshot", "title": "Local fixture",
            "root": str(self.workspace), "mode": "read_only"}]}), encoding="utf-8")
        self.f.rt.workspaces = WorkspaceService(self.f.rt, self.config)

    def publish(self, task):
        if not self.f.rt.workspaces.view(task)["available"]:
            self.f.rt.workspaces.bind(task, "snapshot", expected_revision=None)
        result, = self.f.exchange(task, ("publish_workspace_archive", {
            "filename": "project.zip", "summary": "Local scripted snapshot", "paths": list(FILES)}))
        self.assertEqual(result["status"], "ok", result)
        return next(e for e in self.f.events(task, "deliverable.published") if e.seq == result["artifact_seq"])

    def delivered(self, task=None):
        task = task or self.f.task(self.f.project)
        artifact = self.publish(task)
        return self.f.finish(task, artifacts=[artifact]), artifact

    def arguments(self, delivery, artifact, **changes):
        return {"section": "artifact", "delivery_seq": delivery.seq, "artifact_seq": artifact.seq, **changes}

    def read(self, delivery, artifact, *, native=False, task=None, **changes):
        task = task or self.f.target
        args = self.arguments(delivery, artifact, **changes)
        if native:
            result, = self.f.exchange(task, (TOOL, args))
            return result
        return self.f.history.read(task, args)

    def assert_rejected(self, value):
        self.f.assert_error(value)
        self.assertLessEqual(set(value), {"status", "code", "complete", "outcome", "message"})
        for fragment in (str(self.f.root), TEXT, "PRIVATE_EXCEPTION", "Traceback", "PK\\u0003"):
            self.assertNotIn(fragment, json.dumps(value, ensure_ascii=False))

    def assert_envelope(self, value, delivery, artifact, anchor=None, offset=0):
        self.assertEqual(value["status"], "ok", value)
        self.assertIs(value["complete"], True)
        self.assertIs(value["historical_only"], True)
        self.assertEqual(value["section"], "artifact")
        self.assertEqual(value["offset"], offset)
        self.assertTrue(value["note"])
        self.assertNotIn("content", value)
        self.assertEqual(value["source"], {"project_id": self.f.project, "task_id": delivery.task_id,
            "delivery_seq": delivery.seq, "delivery_hash": delivery.hash, "artifact_seq": artifact.seq,
            "content_sha256": artifact.payload["content_sha256"], "filename": "project.zip",
            "author": self.f.manager, "format": "archive", "event_hash": artifact.hash})
        if anchor:
            self.assertEqual((value["as_of_seq"], value["as_of_hash"]),
                             (anchor["as_of_seq"], anchor["as_of_hash"]))

    def test_native_manifest_validates_without_blob_or_member_access(self):
        delivery, artifact = self.delivered()
        anchor = self.f.history.index(self.f.target)
        with patch("cater.archive_members.read_blob", side_effect=AssertionError("blob accessed")) as blob, \
                patch("cater.team_archive.read_blob", side_effect=AssertionError("blob accessed")) as other, \
                patch.object(zipfile.ZipFile, "open", side_effect=AssertionError("member opened")) as member:
            result = self.read(delivery, artifact, native=True, as_of_seq=anchor["as_of_seq"])
        blob.assert_not_called()
        other.assert_not_called()
        member.assert_not_called()
        self.assert_envelope(result, delivery, artifact, anchor)
        self.assertEqual(result["archive_read"], {"status": "ok", "seq": artifact.seq,
            **{key: artifact.payload[key] for key in
               ("filename", "format", "content_sha256", "content_bytes", "archive")}})
        self.assertNotIn(str(self.workspace), json.dumps(result))

    def test_native_unicode_pages_empty_and_binary_match_current_artifact_reader(self):
        delivery, artifact = self.delivered()
        anchor = self.f.history.index(self.f.target)
        chunks, offset = [], 0
        while True:
            result = self.read(delivery, artifact, native=True, path=UNICODE_PATH, offset=offset,
                               length=11, as_of_seq=anchor["as_of_seq"])
            self.assert_envelope(result, delivery, artifact, anchor, offset)
            member = result["archive_read"]
            direct, = self.f.exchange(delivery.task_id, ("read_artifact", {
                "artifact_seq": artifact.seq, "path": UNICODE_PATH, "offset": offset, "length": 11}))
            self.assertEqual(member, direct)
            self.assertEqual(member["content"], TEXT[offset:offset + 11])
            span = member["read_range"]
            self.assertEqual(span["unit"], "unicode_codepoints")
            self.assertEqual(span["total_chars"], len(TEXT))
            self.assertEqual(span["returned_chars"], len(member["content"]))
            self.assertFalse(span["content_complete"])
            chunks.append(member["content"])
            if not span["has_more"]:
                self.assertIsNone(span["next_offset"])
                break
            self.assertEqual(span["next_offset"], offset + len(member["content"]))
            offset = span["next_offset"]
        self.assertEqual("".join(chunks), TEXT)
        eof = self.read(delivery, artifact, path=UNICODE_PATH, offset=len(TEXT))["archive_read"]
        self.assertEqual(eof["content"], "")
        self.assertFalse(eof["read_range"]["has_more"])
        empty = self.read(delivery, artifact, native=True, path="empty.txt")["archive_read"]
        self.assertEqual((empty["content"], empty["content_kind"], empty["bytes"]), ("", "utf8", 0))
        self.assertIs(empty["read_range"]["content_complete"], True)
        binary = self.read(delivery, artifact, native=True, path="binary.bin")["archive_read"]
        self.assertIsNone(binary["content"])
        self.assertEqual(binary["content_kind"], "binary")
        self.assertEqual(binary["bytes"], 256)
        self.assertEqual(binary["sha256"], hashlib.sha256(FILES["binary.bin"]).hexdigest())
        self.assertNotIn("read_range", binary)

    def test_old_cutoff_survives_source_workspace_and_delivery_changes_and_restart(self):
        delivery, artifact = self.delivered()
        anchor = self.f.history.index(self.f.target)
        args = {"path": UNICODE_PATH, "as_of_seq": anchor["as_of_seq"]}
        original = self.read(delivery, artifact, **args)
        self.assert_envelope(original, delivery, artifact, anchor)
        (self.workspace / UNICODE_PATH).write_text("Later workspace body", encoding="utf-8")
        self.f.update(delivery.task_id, "Replacement source goal")
        newer = self.publish(delivery.task_id)
        latest = self.f.finish(delivery.task_id, artifacts=[newer])
        self.assertEqual(self.f.history.index(self.f.target, as_of_seq=anchor["as_of_seq"]), anchor)
        self.assertEqual(self.read(delivery, artifact, **args), original)
        self.assert_rejected(self.read(delivery, newer, **args))
        self.assert_rejected(self.read(latest, newer, **args))
        self.assert_rejected(self.read(delivery, artifact, path=UNICODE_PATH))
        self.f.rt.close()
        self.f.rt = Runtime(self.f.database, roles_path=self.f.roles, workspaces_path=self.config,
                            drivers={"team_test": self.f.driver})
        self.assertEqual(self.read(delivery, artifact, **args), original)
        self.assertTrue(self.f.rt.verify_chain().ok)

    def test_private_derived_cross_project_and_current_group_are_rejected(self):
        other = self.f.rt.rulebook.create_project("Other scope")["id"]
        ordinary, archive = self.delivered()
        child = self.f.rt.derive_task(ordinary.task_id, "Derived independent scope",
                                     request_id="archive-derived")["task_id"]
        self.f.rt.team.enable(child)
        for task in (self.f.task(other), self.f.task(self.f.project, isolated=True), child, self.f.target):
            with self.subTest(source=task):
                delivery, artifact = self.delivered(task)
                self.assert_rejected(self.read(delivery, artifact, path=UNICODE_PATH))
        for target in (child, self.f.task(other), self.f.task(self.f.project, isolated=True), self.f.task()):
            with self.subTest(target=target):
                self.assert_rejected(self.read(ordinary, archive, task=target, path=UNICODE_PATH))

    def test_source_and_target_project_rebinding_revokes_even_old_cutoff(self):
        delivery, artifact = self.delivered()
        anchor = self.f.history.index(self.f.target)
        other = self.f.rt.rulebook.create_project("Rebound scope")["id"]
        for task in (delivery.task_id, self.f.target):
            with self.subTest(task=task):
                self.rebind(task, other)
                self.assert_rejected(self.read(delivery, artifact, path=UNICODE_PATH,
                                               as_of_seq=anchor["as_of_seq"]))
                self.rebind(task, self.f.project)
                self.assertEqual(self.read(delivery, artifact, path=UNICODE_PATH,
                    as_of_seq=anchor["as_of_seq"])["archive_read"]["content"], TEXT)

    def test_unselected_and_foreign_artifacts_and_wrong_delivery_do_not_resolve(self):
        source = self.f.task(self.f.project)
        chosen = self.publish(source)
        excluded = self.publish(source)
        delivery = self.f.finish(source, artifacts=[chosen])
        foreign_delivery, foreign = self.delivered()
        for d, a in ((delivery, excluded), (delivery, foreign), (foreign_delivery, chosen)):
            with self.subTest(delivery=d.seq, artifact=a.seq):
                self.assert_rejected(self.read(d, a, path=UNICODE_PATH))

    def test_invalid_path_range_and_manifest_pagination_are_rejected(self):
        delivery, artifact = self.delivered()
        invalid_paths = [None, True, 1, "", "../outside", "/absolute", "C:/host", "a\\b",
                         ".env", "a//b", "a/./b", "CON", "x\x00y", "\ud800", "x" * 2049]
        cases = [{"path": path} for path in invalid_paths]
        cases += [{"offset": 0}, {"length": 1}, {"offset": 1, "length": 1}, {"path": "missing.txt"}]
        for key, values in {"offset": [True, -1, "0", None, len(TEXT) + 1],
                            "length": [True, 0, -1, "1", None, 16001]}.items():
            cases.extend({"path": UNICODE_PATH, key: value} for value in values)
        for changes in cases:
            with self.subTest(changes=repr(changes)):
                self.assert_rejected(self.read(delivery, artifact, **changes))
        for section in ("index", "delivery"):
            arguments = {"section": section, "path": UNICODE_PATH}
            if section == "delivery":
                arguments["delivery_seq"] = delivery.seq
            self.assert_rejected(self.f.history.read(self.f.target, arguments))
        plain_delivery, plain = self.f.deliver(self.f.task(self.f.project))
        self.assert_rejected(self.read(plain_delivery, plain, path="result.txt", native=True))

    def adversarial(self, *, changes=None, transform=None, store=True):
        source = self.f.task(self.f.project)
        executor = WorkspaceExecutor(WorkspacePolicy.from_dict({"root": str(self.workspace), "mode": "read_only"}))
        body, manifest = create_snapshot(executor, list(FILES))
        if transform:
            body, manifest = transform(body, copy.deepcopy(manifest))
        digest = hashlib.sha256(body).hexdigest()
        if store:
            self.assertEqual(put_blob(self.f.rt.log, body), digest)
        payload = {"filename": "project.zip", "title": "project.zip", "summary": "Adversarial fixture",
                   "format": "archive", "archive": manifest, "content_sha256": digest,
                   "content_bytes": len(body), "source": "team", "phase": "team", **(changes or {})}
        artifact = self.f.rt.log.emit(type="deliverable.published", task_id=source,
            actor=Actor.agent(self.f.manager, "manager"), payload=payload)
        return self.f.finish(source, artifacts=[artifact]), artifact

    def test_bad_manifest_and_archive_size_reject_before_blob_access(self):
        manifests = [None, {}, {"version": 1, "files": [], "total_bytes": 0}]
        for change in ({"path": "../unsafe"}, {"sha256": "bad"}, {"bytes": -1}, {"executable": 1}):
            value = {"version": 1, "files": [{"path": "a", "sha256": "a" * 64,
                "bytes": 0, "executable": False, **change}], "total_bytes": 0}
            manifests.append(value)
        cases = [{"archive": value} for value in manifests]
        cases += [{"content_bytes": value} for value in (True, 0, -1, MAX_ARCHIVE_BYTES + 1, "5")]
        for changes in cases:
            with self.subTest(changes=changes):
                delivery, artifact = self.adversarial(changes=changes)
                with patch("cater.archive_members.read_blob", side_effect=AssertionError("blob read")) as blob:
                    self.assert_rejected(self.read(delivery, artifact))
                    self.assert_rejected(self.read(delivery, artifact, path=UNICODE_PATH))
                blob.assert_not_called()

    def test_missing_blob_wrong_digest_size_and_invalid_zip_are_safe_errors(self):
        cases = [{"store": False}, {"changes": {"content_sha256": "f" * 64}},
                 {"changes": {"content_bytes": 1}},
                 {"transform": lambda body, manifest: (b"PRIVATE_EXCEPTION not a ZIP", manifest)}]
        for kwargs in cases:
            with self.subTest(kwargs=kwargs):
                delivery, artifact = self.adversarial(**kwargs)
                self.assertEqual(self.read(delivery, artifact)["status"], "ok")
                self.assert_rejected(self.read(delivery, artifact, path=UNICODE_PATH))

    def test_member_hash_crc_and_unselected_metadata_are_verified(self):
        def wrong_sha(body, manifest):
            next(row for row in manifest["files"] if row["path"] == UNICODE_PATH)["sha256"] = "f" * 64
            return body, manifest

        def wrong_metadata(body, manifest):
            manifest["files"][0]["executable"] = not manifest["files"][0]["executable"]
            return body, manifest

        def wrong_crc(body, manifest):
            with zipfile.ZipFile(io.BytesIO(body)) as archive:
                info = archive.getinfo(UNICODE_PATH)
                start = info.header_offset + 30 + len(info.filename.encode()) + len(info.extra)
            damaged = bytearray(body)
            damaged[start] ^= 1
            return bytes(damaged), manifest

        for transform in (wrong_sha, wrong_metadata, wrong_crc):
            with self.subTest(transform=transform.__name__):
                delivery, artifact = self.adversarial(transform=transform)
                self.assertEqual(self.read(delivery, artifact)["status"], "ok")
                self.assert_rejected(self.read(delivery, artifact, path=UNICODE_PATH))

    def test_helper_exception_is_sanitized_without_partial_archive(self):
        delivery, artifact = self.delivered()
        for error in (OSError("PRIVATE_EXCEPTION host path"), ValueError("PRIVATE_EXCEPTION ZIP body")):
            with self.subTest(error=type(error).__name__), \
                    patch("cater.team_archive.read_member", side_effect=error):
                self.assert_rejected(self.read(delivery, artifact, path=UNICODE_PATH, native=True))

    def test_direct_archive_reads_are_pure_and_do_not_use_live_files(self):
        delivery, artifact = self.delivered()
        before = (self.f.rt.log.count(), self.f.rt.log.head_hash(), len(self.f.driver.calls))
        with patch("cater.team_archive.create_snapshot", side_effect=AssertionError("live snapshot")), \
                patch.object(Path, "read_bytes", side_effect=AssertionError("live file read")):
            manifest = self.read(delivery, artifact)
            member = self.read(delivery, artifact, path=UNICODE_PATH)
        self.assertEqual(manifest["status"], "ok")
        self.assertEqual(member["archive_read"]["content"], TEXT)
        self.assertEqual((self.f.rt.log.count(), self.f.rt.log.head_hash(), len(self.f.driver.calls)), before)
        self.assertEqual(self.f.rt.task(self.f.target).deliverables, [])

    def test_scope_changed_between_native_guard_and_archive_catalog(self):
        delivery, artifact = self.delivered()
        other = self.f.rt.rulebook.create_project("Changed after tool guard")["id"]
        original = self.f.rt.team.project_history.read

        def change(task, arguments, **kwargs):
            self.rebind(task, other)
            return original(task, arguments, **kwargs)

        with patch.object(self.f.rt.team.project_history, "read", side_effect=change):
            result = self.read(delivery, artifact, path=UNICODE_PATH, native=True)
        self.assert_rejected(result)
        self.assertEqual(result["code"], "project_history_scope_changed")
        started = self.f.events(self.f.target, "team.tool_started")[-1]
        self.assertEqual(started.payload["project_scope"], self.f.project)

    def rebind(self, task, project):
        # RuleBook forbids rebinding; model an adversarial trusted metadata change.
        self.f.rt.log.emit(type="task.project_bound", actor=Actor.human(), task_id=task,
                           payload={"project_id": project})

    def test_public_arguments_and_native_started_keep_only_safe_selectors(self):
        delivery, artifact = self.delivered()
        args = self.arguments(delivery, artifact, path=UNICODE_PATH, offset=1, length=3)
        self.assertEqual(self.f.history.public_arguments({**args, "content": "PRIVATE_EXCEPTION"}), args)
        for path in (None, True, "../private", "/private", "x" * 2049, "a\x00b", "\ud800"):
            with self.subTest(path=repr(path)):
                self.assertNotIn("path", self.f.history.public_arguments({**args, "path": path}))
        for section in ("delivery", "index"):
            self.assertNotIn("path", self.f.history.public_arguments({**args, "section": section}))
        self.assertEqual(self.f.history.public_arguments(None), {})
        self.read(delivery, artifact, path=UNICODE_PATH, offset=1, length=3, native=True)
        started = self.f.events(self.f.target, "team.tool_started")[-1]
        finished = self.f.events(self.f.target, "team.tool_finished")[-1]
        self.assertEqual(started.payload["arguments"], args)
        self.assertEqual(finished.payload["started_seq"], started.seq)
        self.assertEqual(finished.payload["result"]["archive_read"]["content"], TEXT[1:4])


if __name__ == "__main__":
    unittest.main()
