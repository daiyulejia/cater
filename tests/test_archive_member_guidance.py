"""Corrective member guidance through native Team dispatch, with offline roles."""

import copy
import io
import json
import unittest
from unittest.mock import patch
import zipfile

from cater import archive_members, team_archive
from cater.artifact_blobs import put_blob, read_blob
from cater.workspace_archive import ArchiveError
import test_team_review_cycle as reviews


class ArchiveMemberGuidanceTests(unittest.TestCase):
    def setUp(self):
        self.f = reviews.TeamReviewCycleTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.project = self.f.bind_workspace()
        self.path = "docs/\u5b89\u88c5/\u8bf4\u660e.txt"
        self.content = "PUBLISHED_ONLY\n\u4e2d\U0001f680\u6587\r\n"
        self.files = {self.path: self.content.encode("utf-8"), "empty.txt": b""}
        for name, body in self.files.items():
            path = self.project / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
        result, = self.f.exchange(self.f.worker, ("publish_workspace_archive", {
            "paths": list(self.files), "filename": "project.zip", "summary": "Offline snapshot"}))
        self.assertEqual(result["status"], "ok", result)
        self.original, = self.f.events("deliverable.published")
        self.blob = read_blob(self.f.rt.log, self.original.payload["content_sha256"],
                              self.original.payload["content_bytes"])
        self.binding = copy.deepcopy(self.f.rt.workspaces.view(self.f.task))

    def tearDown(self):
        actual = {p.relative_to(self.project).as_posix(): p.read_bytes()
                  for p in self.project.rglob("*") if p.is_file()}
        self.assertEqual(actual, self.files)
        self.assertEqual(self.f.rt.workspaces.view(self.f.task), self.binding)
        self.assertEqual(self.f.rt.team._pending_operations(self.f.task), [])
        self.assertTrue(self.f.rt.verify_chain().ok)

    def read(self, seq=None, *, task_id=None, **arguments):
        before = self.f.rt.log.read()
        publications = copy.deepcopy(self.f.rt.task(self.f.task).deliverables)
        histories = copy.deepcopy(self.f.native_exchanges)
        result, = self.f.exchange(self.f.reviewer, ("read_artifact", {
            "artifact_seq": self.original.seq if seq is None else seq, **arguments}), task_id=task_id)
        self.assertEqual(self.f.rt.log.read()[:len(before)], before)
        self.assertEqual(self.f.rt.task(self.f.task).deliverables, publications)
        for key, history in histories.items():
            self.assertEqual(self.f.native_exchanges[key], history)
        replies = [json.loads(m["content"]) for m in self.f.driver.calls[-1]["history"]
                   if m.get("role") == "tool"]
        self.assertIn(result, replies)
        return result

    def assert_safe_error(self, result):
        self.assertEqual(result["status"], "error", result)
        rendered = json.dumps(result, ensure_ascii=False)
        for secret in ("PUBLISHED_ONLY", str(self.project), "INTERNAL_SECRET", "OUTSIDE_SECRET"):
            self.assertNotIn(secret, rendered)

    def fault_publication(self, **changes):
        return self.f.rt.log.emit(type="deliverable.published", task_id=self.f.task,
            actor=self.original.actor, payload={**copy.deepcopy(self.original.payload), **changes})

    def test_default_and_explicit_false_keep_actual_reader_exceptions(self):
        artifact = {**self.original.payload, "seq": self.original.seq}
        before = self.f.rt.log.read()
        with patch.object(archive_members, "read_members", wraps=archive_members.read_members) as reader, \
                patch.object(archive_members, "read_blob") as blob:
            for options in ({}, {"guide_missing": False}):
                for selected in (artifact, {**artifact, "archive": None}):
                    with self.subTest(options=options, manifest=selected["archive"]):
                        with self.assertRaises(ArchiveError):
                            team_archive.read_member(self.f.rt.log, selected,
                                {"path": "unlisted.txt"}, **options)
            self.assertEqual(reader.call_count, 4)
            self.assertTrue(all(call.args[2] == ["unlisted.txt"] for call in reader.call_args_list))
            blob.assert_not_called()
        self.assertEqual(self.f.rt.log.read(), before)

    def test_frozen_caller_keeps_original_unavailable_error(self):
        child = self.f.rt.derive_task(self.f.task, "Inspect frozen source",
                                     request_id="member-guidance-child")["task_id"]
        self.f.rt.team.enable(child)
        with patch.object(team_archive, "read_member", wraps=team_archive.read_member) as member, \
                patch.object(archive_members, "read_members", wraps=archive_members.read_members) as reader:
            result, = self.f.exchange(self.f.reviewer, ("read_background", {
                "section": "artifact", "artifact_seq": self.original.seq, "path": "unlisted.txt"}),
                task_id=child)
            member.assert_called_once()
            self.assertEqual(member.call_args.kwargs, {})
            reader.assert_called_once()
        self.assert_safe_error(result)
        self.assertEqual(result["code"], "frozen_archive_unavailable")
        self.assertNotIn("archive_read", result)
        self.assertNotIn("read_artifact", result["message"])

    def test_wrong_guesses_then_manifest_and_exact_unicode_member(self):
        with patch.object(archive_members, "read_members") as reader, \
                patch.object(archive_members, "read_blob") as blob, \
                patch("cater.workspace_tools._open_file", side_effect=AssertionError("outside read")), \
                patch("pathlib.Path.read_bytes", side_effect=AssertionError("disk fallback")):
            for path in ("\u8bf4\u660e.txt", "docs/\u8bf4\u660e.txt"):
                result = self.read(path=path)
                self.assert_safe_error(result)
                self.assertEqual(set(result), {"status", "code", "complete", "outcome",
                                               "artifact_seq", "message"})
                self.assertEqual((result["code"], result["complete"], result["outcome"],
                                  result["artifact_seq"]),
                                 ("archive_member_not_listed", True, "not_started", self.original.seq))
                for instruction in ("\u6240\u9009\u7248\u672c", "manifest", "\u6ca1\u6709\u8fd9\u4e2a\u6210\u5458",
                                    "\u4ec5\u4f20artifact_seq", "read_artifact", "\u7cbe\u786e\u76f8\u5bf9\u8def\u5f84"):
                    self.assertIn(instruction, result["message"])
            listing = self.read()
            self.assertEqual(listing, {"status": "ok", **self.f.rt.task(self.f.task).deliverables[0]})
            reader.assert_not_called()
            blob.assert_not_called()
        exact = next(e["path"] for e in listing["archive"]["files"] if e["path"] == self.path)
        with patch.object(archive_members, "read_members", wraps=archive_members.read_members) as reader:
            page = self.read(path=exact, offset=16, length=3)
            self.assertEqual(page["status"], "ok")
            self.assertEqual(page["path"], self.path)
            self.assertEqual(page["content"], self.content[16:19])
            self.assertEqual(page["read_range"]["returned_chars"], 3)
            self.assertTrue(page["read_range"]["has_more"])
            self.assertFalse(page["read_range"]["content_complete"])
            args, limits = reader.call_args
            self.assertEqual(args[2], [self.path])
            self.assertEqual(limits, {"max_files": 1, "max_member_bytes": 32 * 1024 * 1024,
                                      "max_total_bytes": 32 * 1024 * 1024})
        self.assertEqual(self.read(path=exact)["content"], self.content)

    def test_selection_stays_on_authorized_exact_version(self):
        newer, = self.f.exchange(self.f.worker, ("publish_workspace_archive", {
            "paths": ["empty.txt"], "filename": "project.zip", "summary": "New exact version"}))
        self.assertEqual(newer["status"], "ok", newer)
        self.assertEqual(self.read()["seq"], self.original.seq)
        self.assertEqual(self.read(path=self.path)["content"], self.content)
        self.assertEqual(self.read(newer["artifact_seq"], path=self.path)["code"], "archive_member_not_listed")
        other = self.f.rt.create_task("Separate offline group")
        self.f.rt.team.enable(other)
        with patch.object(archive_members, "read_members") as reader:
            for path in (self.path, "unlisted.txt"):
                result = self.read(task_id=other, path=path)
                self.assert_safe_error(result)
                self.assertNotEqual(result.get("code"), "archive_member_not_listed")
            reader.assert_not_called()

    def test_malformed_paths_and_pagination_are_not_absence(self):
        arguments = [{"path": p} for p in ("../OUTSIDE_SECRET", "/OUTSIDE_SECRET", "C:/OUTSIDE_SECRET",
            "docs\\OUTSIDE_SECRET", "docs//OUTSIDE_SECRET", ".env", "", None, 42, "bad\x00name")]
        arguments += [{"path": "unlisted.txt", **page} for page in
                      ({"offset": -1}, {"offset": True}, {"length": 0}, {"length": 16001})]
        with patch.object(archive_members, "read_members") as reader, \
                patch("cater.workspace_tools._open_file", side_effect=AssertionError("outside read")), \
                patch("pathlib.Path.read_bytes", side_effect=AssertionError("disk fallback")):
            for args in arguments:
                with self.subTest(arguments=args):
                    result = self.read(**args)
                    self.assert_safe_error(result)
                    self.assertNotEqual(result.get("code"), "archive_member_not_listed")
            reader.assert_not_called()

    def test_invalid_manifest_is_rejected_before_absence_or_blob_reads(self):
        invalid = [None, {**self.original.payload["archive"], "version": 2},
                   {**self.original.payload["archive"], "total_bytes": 0}]
        for field, value in (("path", "../INTERNAL_SECRET"), ("sha256", "INTERNAL_SECRET")):
            manifest = copy.deepcopy(self.original.payload["archive"])
            manifest["files"][-1][field] = value
            invalid.append(manifest)
        with patch.object(archive_members, "read_members") as reader, \
                patch.object(archive_members, "read_blob") as blob:
            for manifest in invalid:
                event = self.fault_publication(archive=manifest)
                for path in (self.path, "unlisted.txt"):
                    with self.subTest(manifest=manifest, path=path):
                        result = self.read(event.seq, path=path)
                        self.assert_safe_error(result)
                        self.assertNotEqual(result.get("code"), "archive_member_not_listed")
            reader.assert_not_called()
            blob.assert_not_called()

    def test_listed_member_integrity_failures_do_not_become_absence_or_disk_reads(self):
        wrong_sha = copy.deepcopy(self.original.payload["archive"])
        wrong_sha["files"][0]["sha256"] = "0" * 64
        metadata_mismatch = copy.deepcopy(self.original.payload["archive"])
        metadata_mismatch["files"][0]["executable"] = not metadata_mismatch["files"][0]["executable"]
        damaged = bytearray(self.blob)
        with zipfile.ZipFile(io.BytesIO(self.blob)) as archive:
            info = archive.getinfo(self.path)
            start = info.header_offset + 30 + len(info.filename.encode("utf-8")) + len(info.extra)
        damaged[start] ^= 1
        changes = [{"content_sha256": "0" * 64},
                   {"content_bytes": len(self.blob) + 1},
                   {"archive": wrong_sha}, {"archive": metadata_mismatch}]
        for body in (b"INTERNAL_SECRET", self.blob[:-10], bytes(damaged)):
            changes.append({"content_sha256": put_blob(self.f.rt.log, body), "content_bytes": len(body)})
        with patch("cater.workspace_tools._open_file", side_effect=AssertionError("outside read")), \
                patch("pathlib.Path.read_bytes", side_effect=AssertionError("disk fallback")), \
                patch.object(archive_members, "read_members", wraps=archive_members.read_members) as reader:
            for change in changes:
                with self.subTest(change=change):
                    event = self.fault_publication(**change)
                    result = self.read(event.seq, path=self.path)
                    self.assert_safe_error(result)
                    self.assertNotEqual(result.get("code"), "archive_member_not_listed")
            self.assertEqual(reader.call_count, len(changes))


if __name__ == "__main__":
    unittest.main()
