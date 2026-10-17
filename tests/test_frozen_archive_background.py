"""Frozen ZIP references through disposable hosts and native fake-model turns."""

from __future__ import annotations

import copy
import hashlib
import json
import unittest
from unittest.mock import patch

from cater import team_archive
from cater.conversations import capture, dialogue_background, read_background
from cater.events import Actor
from cater.runtime import Runtime
from cater.workspace_archive import MAX_ARCHIVE_BYTES, MAX_MEMBER_BYTES
import test_team_review_cycle as reviews


class FrozenArchiveBackgroundTests(unittest.TestCase):
    def setUp(self):
        self.f = reviews.TeamReviewCycleTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.project = self.f.bind_workspace()
        self.text = "FROZEN_SOURCE_ONLY\n" + "\u4e2d\U0001f680\r\n" * 5000
        self.files = {"src/main.py": b"def answer():\n    return 42\n",
                      "docs/notes.txt": self.text.encode("utf-8"),
                      "assets/data.bin": bytes(range(256)), "empty.txt": b""}
        for name, body in self.files.items():
            path = self.project / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
        self.original = self.publish()
        self.child = self.derive(self.f.task, "frozen-child-001")
        self.snapshot = self.f.rt.conversation_background(self.child)

    def tearDown(self):
        # Every native exchange must settle, including deliberately failed tools.
        events = self.f.rt.log.read()
        started = [e.payload["run_id"] for e in events if e.type == "team.run_started"]
        finished = [e.payload["run_id"] for e in events if e.type == "team.run_finished"]
        self.assertCountEqual(started, finished)
        for task in {e.task_id for e in events if e.task_id}:
            self.assertEqual(self.f.rt.team._pending_operations(task), [])

    def publish(self):
        result, = self.f.exchange(self.f.worker, ("publish_workspace_archive", {
            "paths": list(self.files), "filename": "project.zip", "summary": "Real local fixture ZIP"}))
        self.assertEqual(result["status"], "ok", result)
        return next(e for e in self.f.rt.log.iter_events(
            task_id=self.f.task, event_types=("deliverable.published",))
            if e.seq == result["artifact_seq"])

    def derive(self, source, request):
        child = self.f.rt.derive_task(source, "Inspect the frozen project", request_id=request)["task_id"]
        self.f.rt.team.enable(child)
        return child

    def read(self, snapshot=None, *, log=None, **arguments):
        return read_background(self.snapshot if snapshot is None else snapshot,
            {"section": "artifact", "artifact_seq": self.original.seq, **arguments},
            log=self.f.rt.log if log is None else log)

    def native_read(self, task=None, **arguments):
        result, = self.f.exchange(self.f.manager, ("read_background", {
            "section": "artifact", "artifact_seq": self.original.seq, **arguments}),
            task_id=task or self.child)
        return result

    def assert_envelope(self, result, snapshot=None, event=None):
        snapshot = self.snapshot if snapshot is None else snapshot
        event = self.original if event is None else event
        self.assertEqual(result["status"], "ok", result)
        self.assertIs(result["complete"], True)
        self.assertIs(result["historical_only"], True)
        self.assertEqual(result["section"], "artifact")
        self.assertEqual(result["snapshot"], {k: snapshot[k] for k in
            ("source_task_id", "source_seq", "source_hash", "captured_at")})
        self.assertEqual(result["source"], {
            "task_id": event.task_id, "seq": event.seq, "filename": event.payload["filename"],
            "format": "archive", "content_sha256": event.payload["content_sha256"],
            "verification": next(a["verification"] for a in snapshot["artifacts"] if a["seq"] == event.seq),
            "event_hash": event.hash})
        self.assertEqual(result["archive_read"]["status"], "ok")

    def assert_unavailable(self, result):
        self.assertEqual(result["status"], "error", result)
        self.assertEqual(result["code"], "frozen_archive_unavailable", result)
        self.assertNotIn("archive_read", result)
        self.assertNotIn("FROZEN_SOURCE_ONLY", json.dumps(result))
        self.assertNotIn(str(self.f.root), json.dumps(result))

    def corrupt_publication(self, **changes):
        """Fault-inject an immutable publication in this test's database only."""
        event = self.f.rt.log.emit(type="deliverable.published", task_id=self.f.task,
            actor=self.original.actor, payload={**copy.deepcopy(self.original.payload), **changes})
        return event, capture(self.f.rt, self.f.task)

    def test_native_manifest_member_and_schema_reach_the_model(self):
        manifest = self.native_read()
        self.assert_envelope(manifest)
        self.assertEqual(manifest["archive_read"], {"status": "ok", "seq": self.original.seq,
            **{k: self.original.payload[k] for k in
               ("filename", "format", "content_sha256", "content_bytes", "archive")}})
        member = self.native_read(path="src/main.py")
        self.assert_envelope(member)
        self.assertEqual(member["archive_read"]["content"], self.files["src/main.py"].decode())
        calls = [c for c in self.f.driver.calls if c["context"]["background"] is not None]
        self.assertTrue(calls)
        tool = next(t for t in calls[0]["tools"] if t["name"] == "read_background")
        self.assertEqual(tool["input_schema"]["properties"]["path"]["type"], "string")
        self.assertFalse(tool["input_schema"]["additionalProperties"])
        self.assertIn("path", tool["description"])
        self.assertIn("ZIP", tool["description"])
        for call in calls:
            self.assertNotIn("FROZEN_SOURCE_ONLY", json.dumps(call["context"]))
            self.assertNotIn("archive", call["context"]["background"]["artifacts"][0])
        histories = [json.loads(m["content"]) for c in calls for m in c["history"]
                     if m.get("role") == "tool"]
        self.assertIn(manifest, histories)
        self.assertIn(member, histories)
        self.assertTrue(self.f.rt.verify_chain().ok)

    def test_native_failure_is_visible_and_correction_reads_the_real_member(self):
        failed = self.native_read(path="missing.txt")
        self.assert_unavailable(failed)
        corrected = self.native_read(path="src/main.py")
        self.assert_envelope(corrected)
        self.assertEqual(corrected["archive_read"]["content"], self.files["src/main.py"].decode())
        tool_results = [e.payload["result"] for e in self.f.rt.log.read(task_id=self.child)
                       if e.type == "team.tool_finished" and e.payload["name"] == "read_background"]
        self.assertEqual(tool_results, [failed, corrected])
        self.assertFalse([r for r in self.f.rt.team.view(self.child)["runs"] if r["status"] == "failed"])
        self.assertEqual(self.f.rt.task(self.child).deliverables, [])

    def test_parent_new_version_goal_and_directory_do_not_replace_frozen_source(self):
        saved = copy.deepcopy(self.snapshot)
        (self.project / "src/main.py").write_bytes(b"LATER_PARENT_VERSION")
        latest = self.publish()
        self.assertGreater(latest.seq, self.snapshot["source_seq"])
        self.assertNotEqual(latest.payload["content_sha256"], self.original.payload["content_sha256"])
        result, = self.f.exchange(self.f.manager, ("set_objective", {"goal": "A different parent objective"}))
        self.assertEqual(result["status"], "ok", result)
        binding = self.f.rt.workspaces.view(self.f.task)
        self.f.rt.workspaces.bind(self.f.task, None, expected_revision=binding["revision"])
        self.project.rename(self.f.root / "moved-source-project")
        result = self.native_read(path="src/main.py")
        self.assert_envelope(result)
        self.assertEqual(result["archive_read"]["content"], self.files["src/main.py"].decode())
        self.assertEqual(self.f.rt.conversation_background(self.child), saved)
        self.assertEqual(self.f.rt.task(self.child).deliverables, [])
        self.assertEqual(self.f.rt.team_reviews.view(self.child), [])
        denied = self.native_read(artifact_seq=latest.seq, path="src/main.py")
        self.assertEqual(denied["status"], "error", denied)

    def test_descendant_restart_and_paused_native_read_keep_original_reference(self):
        grandchild = self.derive(self.child, "frozen-grandchild-001")
        frozen = copy.deepcopy(self.f.rt.conversation_background(grandchild))
        self.assertEqual(frozen["artifacts"], self.snapshot["artifacts"])
        self.assertEqual(frozen["source_task_id"], self.child)
        self.f.rt.execution_control.change(grandchild, "pause", request_id="frozen-pause-001")
        with Runtime(self.f.root / "test.db", roles_path=self.f.roles,
                     drivers={"team_test": self.f.driver}) as reopened:
            previous = self.f.rt
            self.f.rt = reopened
            try:
                result = self.native_read(grandchild, path="src/main.py")
                self.assert_envelope(result, frozen)
                self.assertEqual(result["archive_read"]["content"], self.files["src/main.py"].decode())
                self.assertEqual(reopened.conversation_background(grandchild), frozen)
                self.assertEqual(reopened.execution_control.view(grandchild)["state"], "paused")
                tools = {t["name"] for t in self.f.driver.calls[-1]["tools"]}
                self.assertIn("read_background", tools)
                self.assertNotIn("publish_workspace_archive", tools)
                self.assertNotIn("workspace_write", tools)
                self.assertTrue(reopened.verify_chain().ok)
            finally:
                self.f.rt = previous

    def test_legacy_references_missing_either_or_both_new_fields_still_read_exact_event(self):
        for absent in (("content_bytes",), ("event_hash",), ("content_bytes", "event_hash")):
            with self.subTest(absent=absent):
                frozen = copy.deepcopy(self.snapshot)
                for key in absent:
                    frozen["artifacts"][0].pop(key)
                before = copy.deepcopy(frozen)
                self.assert_envelope(self.read(frozen), frozen)
                result = self.read(frozen, path="src/main.py")
                self.assert_envelope(result, frozen)
                self.assertEqual(result["archive_read"]["content"], self.files["src/main.py"].decode())
                self.assertEqual(frozen, before)

    def test_metadata_does_not_read_blob_and_member_reuses_read_member_without_writes(self):
        frozen = copy.deepcopy(self.snapshot)
        head, count = self.f.rt.log.head_hash(), self.f.rt.log.count()
        changes = self.f.rt.log.db.total_changes
        queries = []
        self.f.rt.log.db.set_trace_callback(queries.append)
        try:
            with patch("cater.team_archive.read_blob", side_effect=AssertionError("metadata read BLOB")) as blob:
                self.assert_envelope(self.read())
                index = read_background(self.snapshot, {"section": "index"}, log=self.f.rt.log)
                self.assertEqual(index["entries"][-1]["format"], "archive")
                self.assertEqual(index["entries"][-1]["content_bytes"], self.original.payload["content_bytes"])
                self.assertNotIn("archive", index["entries"][-1])
                routing = dialogue_background(self.snapshot)
                self.assertNotIn("archive", routing["artifacts"][0])
                blob.assert_not_called()
            self.assertFalse(any("artifact_blobs" in q.lower() for q in queries), queries)
            artifact = {**self.original.payload, "seq": self.original.seq}
            expected = team_archive.read_member(self.f.rt.log, artifact,
                {"path": "docs/notes.txt", "offset": 2, "length": 9})
            with patch("cater.team_archive.read_member", wraps=team_archive.read_member) as member:
                result = self.read(path="docs/notes.txt", offset=2, limit=9)
                self.assert_envelope(result)
                self.assertEqual(result["archive_read"], expected)
                member.assert_called_once_with(self.f.rt.log, artifact,
                    {"path": "docs/notes.txt", "offset": 2, "length": 9})
            self.assert_unavailable(self.read(path="missing.txt"))
        finally:
            self.f.rt.log.db.set_trace_callback(None)
        self.assertEqual(self.snapshot, frozen)
        self.assertEqual((self.f.rt.log.head_hash(), self.f.rt.log.count()), (head, count))
        self.assertEqual(self.f.rt.log.db.total_changes, changes)

    def test_lookup_is_bounded_to_one_original_publication_and_never_latest(self):
        (self.project / "src/main.py").write_bytes(b"newer")
        self.publish()
        original_iter = self.f.rt.log.iter_events
        with patch.object(self.f.rt.log, "read", side_effect=AssertionError("unbounded stream")), \
                patch.object(self.f.rt, "task", side_effect=AssertionError("live task projection")), \
                patch.object(self.f.rt.log, "iter_events", wraps=original_iter) as lookup:
            result = self.read(path="src/main.py")
            self.assert_envelope(result)
            lookup.assert_called_once_with(task_id=self.original.task_id,
                event_types=("deliverable.published",), after_seq=self.original.seq - 1,
                before_seq=self.original.seq + 1)
        self.assertEqual(result["archive_read"]["content"], self.files["src/main.py"].decode())

    def test_member_unicode_pagination_binary_empty_and_exact_end(self):
        first = self.read(path="docs/notes.txt")["archive_read"]
        self.assertEqual(first["content"], self.text[:8000])
        self.assertEqual(first["read_range"]["next_offset"], 8000)
        second = self.read(path="docs/notes.txt", offset=8000, limit=16000)["archive_read"]
        self.assertEqual(first["content"] + second["content"], self.text)
        self.assertFalse(second["read_range"]["has_more"])
        self.assertEqual(second["read_range"]["unit"], "unicode_codepoints")
        end = self.read(path="docs/notes.txt", offset=len(self.text))["archive_read"]
        self.assertEqual(end["content"], "")
        self.assert_unavailable(self.read(path="docs/notes.txt", offset=len(self.text) + 1))
        empty = self.read(path="empty.txt")["archive_read"]
        self.assertEqual(empty["content"], "")
        self.assertTrue(empty["read_range"]["content_complete"])
        binary = self.read(path="assets/data.bin")["archive_read"]
        self.assertIsNone(binary["content"])
        self.assertEqual(binary["content_kind"], "binary")
        self.assertNotIn("read_range", binary)
        self.assertEqual(binary["bytes"], len(self.files["assets/data.bin"]))
        self.assertEqual(binary["sha256"], hashlib.sha256(self.files["assets/data.bin"]).hexdigest())

    def test_scope_cutoff_missing_and_ambiguous_references_never_fall_back(self):
        outsider = self.f.rt.create_task("Unrelated fixture task")
        for key, value in (("task_id", outsider), ("seq", self.snapshot["source_seq"] + 1),
                           ("filename", "other.zip"), ("actor", self.f.manager),
                           ("content_sha256", "a" * 64), ("event_hash", "b" * 64),
                           ("content_bytes", self.original.payload["content_bytes"] + 1),
                           ("content_bytes", True), ("content_bytes", None), ("event_hash", None)):
            with self.subTest(key=key, value=value):
                frozen = copy.deepcopy(self.snapshot)
                frozen["artifacts"][0][key] = value
                self.assert_unavailable(self.read(frozen, artifact_seq=frozen["artifacts"][0]["seq"]))
        for key in ("actor", "task_id", "filename", "content_sha256"):
            with self.subTest(missing=key):
                frozen = copy.deepcopy(self.snapshot)
                frozen["artifacts"][0].pop(key)
                self.assert_unavailable(self.read(frozen))
        frozen = copy.deepcopy(self.snapshot)
        frozen["source_seq"] = self.original.seq - 1
        self.assert_unavailable(self.read(frozen))
        for rows in ([], self.snapshot["artifacts"] * 2):
            result = self.read({**self.snapshot, "artifacts": rows}, path="src/main.py")
            self.assertEqual(result["status"], "error", result)
            self.assertNotIn("archive_read", result)
        result = read_background(self.snapshot,
            {"section": "artifact", "artifact_seq": self.original.seq})
        self.assert_unavailable(result)

    def test_exact_event_identity_type_and_ambiguity_are_checked(self):
        from dataclasses import replace

        for events in ([], [self.original, self.original],
                       [replace(self.original, seq=self.original.seq + 1)],
                       [replace(self.original, task_id=self.child)],
                       [replace(self.original, type="team.message_received")],
                       [replace(self.original, actor=Actor.human())]):
            with self.subTest(events=[(e.seq, e.task_id, e.type, e.actor) for e in events]):
                with patch.object(self.f.rt.log, "iter_events", return_value=iter(events)):
                    self.assert_unavailable(self.read())

    def test_bad_arguments_and_non_archive_path_are_rejected(self):
        base = {"section": "artifact", "artifact_seq": self.original.seq}
        bad = [None, [], {}, {**base, "task_id": self.f.task}, {**base, "source_id": "x"},
               {**base, "artifact_seq": True}, {**base, "artifact_seq": 0},
               {**base, "artifact_seq": "latest"}, {**base, "offset": 0}, {**base, "limit": 1}]
        bad.extend({**base, "path": p} for p in (None, True, "", "../x", "/x", "C:/x", "src\\main.py"))
        bad.extend({**base, "path": "src/main.py", **paging} for paging in
                   ({"offset": -1}, {"offset": True}, {"offset": 1.5},
                    {"limit": 0}, {"limit": 16001}, {"limit": False}, {"limit": None}))
        bad.extend({"section": s, "path": "src/main.py", **selector} for s, selector in
                   (("index", {}), ("goal", {}), ("source", {"source_id": "source"})))
        for arguments in bad:
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                read_background(self.snapshot, arguments, log=self.f.rt.log)
        seq = self.f.publish("Ordinary frozen text")
        frozen = capture(self.f.rt, self.f.task)
        with self.assertRaises(ValueError):
            self.read(frozen, artifact_seq=seq, path="src/main.py")
        self.assertEqual(self.read(frozen, artifact_seq=seq)["content"], "Ordinary frozen text")
        self.assert_unavailable(self.read(path="not-in-manifest.txt"))

    def test_missing_blob_table_still_allows_manifest_but_never_member_fallback(self):
        self.f.rt.log.db.execute("DROP TABLE artifact_blobs")
        self.assert_envelope(self.read())
        self.assert_unavailable(self.read(path="src/main.py"))
        self.assertIsNone(self.f.rt.log.db.execute(
            "SELECT name FROM sqlite_master WHERE name = 'artifact_blobs'").fetchone())

    def test_missing_blob_and_wrong_declared_blob_size_fail_closed(self):
        for changes in ({"content_sha256": "a" * 64},
                        {"content_bytes": self.original.payload["content_bytes"] + 1}):
            with self.subTest(changes=changes):
                event, frozen = self.corrupt_publication(**changes)
                metadata = self.read(frozen, artifact_seq=event.seq)
                self.assert_envelope(metadata, frozen, event)
                self.assert_unavailable(self.read(frozen, artifact_seq=event.seq, path="src/main.py"))

    def test_bad_original_manifest_format_and_size_return_no_partial_receipt(self):
        bad_manifests = [None, {}, {**self.original.payload["archive"], "version": 2}]
        oversized = copy.deepcopy(self.original.payload["archive"])
        oversized["files"][0]["bytes"] = MAX_MEMBER_BYTES + 1
        oversized["total_bytes"] = sum(e["bytes"] for e in oversized["files"])
        bad_manifests.append(oversized)
        cases = [{"archive": manifest} for manifest in bad_manifests]
        cases.extend(({"format": "text"}, {"content_bytes": MAX_ARCHIVE_BYTES + 1},
                      {"content_bytes": 0}))
        for changes in cases:
            with self.subTest(changes=changes):
                event, frozen = self.corrupt_publication(**changes)
                # Pin an archive reference even when the original event has a corrupt format.
                row = next(a for a in frozen["artifacts"] if a["seq"] == event.seq)
                row["format"] = "archive"
                self.assert_unavailable(self.read(frozen, artifact_seq=event.seq))

    def test_manifest_member_hash_and_size_must_match_actual_zip(self):
        for key, value in (("sha256", "f" * 64), ("bytes", 1)):
            with self.subTest(key=key):
                manifest = copy.deepcopy(self.original.payload["archive"])
                member = next(e for e in manifest["files"] if e["path"] == "src/main.py")
                member[key] = value
                manifest["total_bytes"] = sum(e["bytes"] for e in manifest["files"])
                event, frozen = self.corrupt_publication(archive=manifest)
                self.assert_envelope(self.read(frozen, artifact_seq=event.seq), frozen, event)
                self.assert_unavailable(self.read(frozen, artifact_seq=event.seq, path="src/main.py"))

    def test_mode_mismatch_is_rejected_by_native_and_frozen_member_reads(self):
        manifest = copy.deepcopy(self.original.payload["archive"])
        member = next(e for e in manifest["files"] if e["path"] == "src/main.py")
        member["executable"] = not member["executable"]
        event, frozen = self.corrupt_publication(archive=manifest)
        # A metadata-only read must not claim it verified the underlying ZIP.
        self.assert_envelope(self.read(frozen, artifact_seq=event.seq), frozen, event)
        with self.assertRaises(ValueError):
            team_archive.read_member(self.f.rt.log, {**event.payload, "seq": event.seq}, {"path": "src/main.py"})
        failed, = self.f.exchange(self.f.reviewer, ("read_artifact", {"artifact_seq": event.seq, "path": "src/main.py"}))
        self.assertEqual(failed["status"], "error", failed)
        self.assert_unavailable(self.read(frozen, artifact_seq=event.seq, path="src/main.py"))

    def test_shared_reader_retains_member_budget_above_browser_default(self):
        size = 17 * 1024 * 1024
        self.files["large.bin"] = b"\0" * size
        (self.project / "large.bin").write_bytes(self.files["large.bin"])
        event = self.publish()
        frozen = capture(self.f.rt, self.f.task)
        from cater.archive_members import read_members

        with patch("cater.archive_members.read_members", wraps=read_members) as reader:
            value = self.read(frozen, artifact_seq=event.seq, path="large.bin")
            self.assert_envelope(value, frozen, event)
            self.assertEqual(value["archive_read"]["bytes"], size)
            self.assertEqual(value["archive_read"]["content_kind"], "binary")
            self.assertIsNone(value["archive_read"]["content"])
            self.assertEqual(reader.call_args.kwargs, {"max_files": 1,
                "max_member_bytes": MAX_MEMBER_BYTES, "max_total_bytes": MAX_MEMBER_BYTES})

    def test_capture_stores_small_references_and_retains_only_three_artifacts(self):
        for number in range(3):
            (self.project / "src/main.py").write_bytes(f"version {number}".encode())
            self.publish()
        frozen = capture(self.f.rt, self.f.task)
        self.assertEqual(len(frozen["artifacts"]), 3)
        self.assertEqual(frozen["omitted_artifacts"], 1)
        self.assertNotIn(self.original.seq, [a["seq"] for a in frozen["artifacts"]])
        self.assertLess(len(json.dumps(frozen, ensure_ascii=False).encode()), 262144)
        for row in frozen["artifacts"]:
            self.assertNotIn("archive", row)
            self.assertNotIn("content", row)
            self.assertIsInstance(row["content_bytes"], int)
            self.assertEqual(len(row["event_hash"]), 64)
            self.assert_envelope(self.read(frozen, artifact_seq=row["seq"]), frozen,
                next(e for e in self.f.events("deliverable.published") if e.seq == row["seq"]))
        self.assertEqual(self.read(frozen)["status"], "error")


if __name__ == "__main__":
    unittest.main()
