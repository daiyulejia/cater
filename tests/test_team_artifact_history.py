"""Exact-filename publication history through offline native and external tools."""

import copy
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from cater.artifacts import content_metrics, normalize_deliverables
from cater.events import Actor
from cater.team import HOST
import test_team_external
import test_team_review_cycle


def text_version(seq, actor, content, filename="result.txt", source="team"):
    return {"seq": seq, "filename": filename, "actor": actor, "source": source,
            "format": "text", "content_bytes": len(content.encode("utf-8")),
            "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest()}


class TeamArtifactHistoryTests(unittest.TestCase):
    def setUp(self):
        self.f = test_team_review_cycle.TeamReviewCycleTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def read(self, seq, **arguments):
        result, = self.f.exchange(self.f.observer, ("read_artifact", {
            "artifact_seq": seq, **arguments}))
        return result

    def test_default_and_explicit_content_preserve_original_response_and_pages(self):
        content = "\u6708\U0001f680\u0301\r\n" * 2000
        seq = self.f.publish(content)
        artifact, = self.f.rt.task(self.f.task).deliverables
        for arguments in ({}, {"section": "content"}):
            with self.subTest(arguments=arguments):
                self.assertEqual(self.read(seq, **arguments), {"status": "ok", **artifact,
                    "content_metrics": content_metrics(content)})
        page = {"status": "ok", **artifact, "content_metrics": content_metrics(content),
            "content": content[2:9], "read_range": {
            "unit": "unicode_codepoints", "offset": 2, "returned_chars": 7,
            "total_chars": len(content), "has_more": True, "next_offset": 9,
            "content_complete": False}}
        self.assertEqual(self.read(seq, offset=2, length=7), page)
        self.assertEqual(self.read(seq, section="content", offset=2, length=7), page)

    def test_old_anchor_includes_all_publishers_equal_bytes_and_old_objectives(self):
        content = "PRIVATE_BODY_DO_NOT_RETURN\n"
        old = self.f.publish(content)
        changed, = self.f.exchange(self.f.manager, ("set_objective", {"goal": "New target"}))
        self.assertEqual(changed["status"], "ok")
        expected = [text_version(old, self.f.worker, content)]
        for role in [self.f.rt.organization.leader, *self.f.rt.roles]:
            seq = self.f.publish(content, author=role.id)
            expected.append(text_version(seq, role.id, content))
        payload, = normalize_deliverables([{"title": "PRIVATE_TITLE", "filename": "result.txt",
            "content": content, "summary": "PRIVATE_SUMMARY", "format": "text"}])
        legacy = self.f.rt.log.emit(type="deliverable.published", task_id=self.f.task,
            actor=Actor.agent("retired-publisher", "Historical author"),
            payload={**payload, "source": "legacy", "revision_source": {"artifact_seq": old},
                     "workspace_file": {"path": "PRIVATE_WORKSPACE_PATH"}})
        expected.append(text_version(legacy.seq, "retired-publisher", content, source="legacy"))
        for anchor in (old, legacy.seq):
            with self.subTest(anchor=anchor):
                result = self.read(anchor, section="versions")
                self.assertEqual(result, {"status": "ok", "section": "versions", "artifact_seq": anchor,
                    "filename": "result.txt", "scope": "same_filename_in_this_chat", "versions": expected,
                    "versions_total": len(expected), "has_more": False, "next_seq": legacy.seq})
                serialized = json.dumps(result)
                for secret in (content.strip(), "PRIVATE_TITLE", "PRIVATE_SUMMARY", "Fixture publication",
                               "PRIVATE_WORKSPACE_PATH", str(self.f.root)):
                    self.assertNotIn(secret, serialized)
        self.assertTrue(self.f.rt.verify_chain().ok)

    def test_pagination_default_limit_maximum_and_after_end(self):
        expected = []
        for i in range(22):
            content = f"Persisted version {i}"
            payload, = normalize_deliverables([{"title": "Version", "filename": "result.txt",
                "content": content, "summary": "Fixture", "format": "text"}])
            event = self.f.rt.log.emit(type="deliverable.published", task_id=self.f.task,
                actor=Actor.agent(self.f.worker, self.f.rt.team._role(self.f.worker).role),
                payload={**payload, "source": "team"})
            expected.append(text_version(event.seq, self.f.worker, content))
        anchor = expected[5]["seq"]
        cases = [({}, expected[:20], True, expected[19]["seq"]),
                 ({"after_seq": expected[19]["seq"], "limit": 1}, expected[20:21], True, expected[20]["seq"]),
                 ({"after_seq": expected[20]["seq"], "limit": 1}, expected[21:], False, expected[21]["seq"]),
                 ({"after_seq": expected[-1]["seq"]}, [], False, expected[-1]["seq"]),
                 ({"after_seq": expected[-1]["seq"] + 10000}, [], False, expected[-1]["seq"] + 10000),
                 ({"after_seq": 0, "limit": 50}, expected, False, expected[-1]["seq"])]
        for arguments, rows, more, cursor in cases:
            with self.subTest(arguments=arguments):
                self.assertEqual(self.read(anchor, section="versions", **arguments), {
                    "status": "ok", "section": "versions", "artifact_seq": anchor, "filename": "result.txt",
                    "scope": "same_filename_in_this_chat", "versions": rows,
                    "versions_total": 22, "has_more": more, "next_seq": cursor})

    def test_exact_case_unicode_and_filename_only_without_inferred_lineage(self):
        names = ["Report.txt", "report.txt", "caf\u00e9.txt", "cafe\u0301.txt", "renamed.txt"]
        seqs = [self.f.publish("same bytes", filename=name) for name in names]
        for name, seq in zip(names, seqs):
            with self.subTest(filename=name):
                self.assertEqual(self.read(seq, section="versions"), {
                    "status": "ok", "section": "versions", "artifact_seq": seq, "filename": name,
                    "scope": "same_filename_in_this_chat",
                    "versions": [text_version(seq, self.f.worker, "same bytes", filename=name)],
                    "versions_total": 1, "has_more": False, "next_seq": seq})

    def test_legacy_metadata_keeps_optional_fields_and_never_infers_missing_filename(self):
        event = self.f.rt.log.emit(type="deliverable.published", task_id=self.f.task,
            actor=Actor.system("historical-import"), payload={"filename": "result.txt",
                "title": "PRIVATE_TITLE", "summary": "PRIVATE_SUMMARY", "content_error": "invalid_content"})
        self.assertEqual(self.read(event.seq, section="versions"), {
            "status": "ok", "section": "versions", "artifact_seq": event.seq, "filename": "result.txt",
            "scope": "same_filename_in_this_chat", "versions": [{"seq": event.seq, "filename": "result.txt",
                "actor": "historical-import", "content_error": "invalid_content"}],
            "versions_total": 1, "has_more": False, "next_seq": event.seq})
        for content in (None, "legacy inline body"):
            payload, = normalize_deliverables([{"title": "result.txt", "summary": "Fixture",
                **({"content": content, "format": "text"} if content else {})}])
            unnamed = self.f.rt.log.emit(type="deliverable.published", task_id=self.f.task,
                actor=Actor.agent(self.f.worker, self.f.rt.team._role(self.f.worker).role), payload=payload)
            self.assertEqual(self.read(unnamed.seq, section="versions")["status"], "error")
            metrics = {"content_metrics": content_metrics(content)} if content is not None else {}
            self.assertEqual(self.read(unnamed.seq), {"status": "ok", **payload, **metrics,
                "seq": unnamed.seq, "actor": self.f.worker})

    def test_archive_history_and_workspace_publication_never_read_blobs_or_files(self):
        project = self.f.bind_workspace()
        (project / "result.txt").write_bytes(b"workspace body")
        inline = self.f.publish("inline body")
        workspace, archive = self.f.exchange(self.f.worker,
            ("publish_workspace_file", {"path": "result.txt", "summary": "PRIVATE_SUMMARY", "format": "text"}),
            ("publish_workspace_archive", {"paths": ["result.txt"], "filename": "project.zip",
                                           "summary": "PRIVATE_ARCHIVE_SUMMARY"}))
        self.assertEqual((workspace["status"], archive["status"]), ("ok", "ok"))
        artifact = self.f.rt.task(self.f.task).deliverables[-1]
        original = self.f.rt.team._execute

        def metadata_only(*args, **kwargs):
            with patch("cater.team_archive.read_blob", side_effect=AssertionError("Blob read")), \
                 patch("cater.artifact_blobs.read_blob", side_effect=AssertionError("Blob read")), \
                 patch("cater.team_archive.artifact_bytes", side_effect=AssertionError("Body read")), \
                 patch("pathlib.Path.open", side_effect=AssertionError("Filesystem read")), \
                 patch("builtins.open", side_effect=AssertionError("Filesystem read")):
                return original(*args, **kwargs)

        with patch.object(self.f.rt.team, "_execute", side_effect=metadata_only):
            text = self.read(inline, section="versions")
            result = self.read(archive["artifact_seq"], section="versions")
        self.assertEqual(text["versions"], [text_version(inline, self.f.worker, "inline body"),
            text_version(workspace["artifact_seq"], self.f.worker, "workspace body")])
        self.assertEqual(result, {"status": "ok", "section": "versions", "artifact_seq": archive["artifact_seq"],
            "filename": "project.zip", "scope": "same_filename_in_this_chat", "versions": [{
                "seq": archive["artifact_seq"], "filename": "project.zip", "actor": self.f.worker,
                "source": "team", "format": "archive", "content_bytes": archive["content_bytes"],
                "content_sha256": archive["content_sha256"]}],
            "versions_total": 1, "has_more": False, "next_seq": archive["artifact_seq"]})
        self.assertIn("archive", artifact)
        self.assertIn("local_path", artifact)

    def test_foreign_or_nonartifact_anchor_cannot_expose_same_filename_history(self):
        local = self.f.publish("LOCAL_PRIVATE_BODY")
        other = self.f.rt.create_task("Other private chat")
        self.f.rt.team.enable(other)
        foreign = self.f.publish("FOREIGN_PRIVATE_BODY", task_id=other)
        for seq in (foreign, self.f.events("team.started")[0].seq, foreign + 999999):
            with self.subTest(anchor=seq):
                self.assertEqual(self.read(seq, section="versions"), {
                    "status": "error", "message": "\u64cd\u4f5c\u672a\u5b8c\u6210\uff0c\u8bf7\u6838\u5bf9\u5de5\u5177\u53c2\u6570\u3001\u6743\u9650\u4e0e\u5df2\u6709\u8bb0\u5f55\u540e\u4fee\u6b63\u3002"})
        result, = self.f.exchange(self.f.observer, ("read_artifact", {
            "artifact_seq": local, "section": "versions"}), task_id=other)
        self.assertEqual(result["status"], "error")
        self.assertNotIn("versions", result)
        self.assertEqual(self.read(local, section="versions")["versions_total"], 1)

    def test_invalid_types_sections_and_mixed_content_arguments_are_settled_errors(self):
        seq = self.f.publish("body")
        cases = [{"section": value} for value in ("history", "", True, False, None, 1, [], {})]
        cases += [{"section": "versions", "after_seq": value} for value in (-1, True, False, "0", 1.0, None)]
        cases += [{"section": "versions", "limit": value} for value in (0, -1, 51, True, False, "1", 1.0, None)]
        cases += [{"section": "versions", key: value} for key, value in
                  (("offset", 0), ("length", 1), ("path", "result.txt"), ("offset", None), ("path", None))]
        cases += [{**section, key: value} for section in ({}, {"section": "content"})
                  for key, value in (("limit", 1), ("after_seq", 0))]
        cases += [{"artifact_seq": value, "section": "versions"} for value in (True, False, 0, -1, "1", 1.0, None)]
        for arguments in cases:
            with self.subTest(arguments=arguments):
                result, = self.f.exchange(self.f.observer, ("read_artifact", {"artifact_seq": seq, **arguments}))
                self.assertEqual(result["status"], "error")
                self.assertNotIn("outcome", result)
                self.assertNotIn("versions", result)
        self.assertEqual(self.f.rt.team._pending_operations(self.f.task), [])
        self.assertEqual(self.read(seq, section="versions")["versions_total"], 1)

    def test_native_schema_receipt_and_signed_transcript_preserve_exact_result(self):
        seq = self.f.publish("body")
        arguments = {"artifact_seq": seq, "section": "versions", "after_seq": 0, "limit": 1}
        result = self.read(seq, section="versions", after_seq=0, limit=1)
        last = self.f.driver.calls[-1]
        schema, = [tool["input_schema"] for tool in last["tools"] if tool["name"] == "read_artifact"]
        self.assertEqual(schema["required"], ["artifact_seq"])
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["properties"]["section"], {"type": "string", "enum": ["content", "versions"]})
        self.assertEqual({k: schema["properties"]["after_seq"][k] for k in ("type", "minimum")},
                         {"type": "integer", "minimum": 0})
        self.assertEqual({k: schema["properties"]["limit"][k] for k in ("type", "minimum", "maximum")},
                         {"type": "integer", "minimum": 1, "maximum": 50})
        receipt = [e for e in self.f.events("team.tool_finished") if e.payload["name"] == "read_artifact"][-1]
        started, = [e for e in self.f.events("team.tool_started") if e.seq == receipt.payload["started_seq"]]
        self.assertEqual(started.payload["arguments"], arguments)
        self.assertEqual((started.actor, receipt.actor), (HOST, HOST))
        self.assertEqual((started.payload["run_id"], started.payload["role"]),
                         (receipt.payload["run_id"], self.f.observer))
        replies = [row for row in last["history"] if row.get("tool_call_id") == started.payload["call_id"]]
        self.assertEqual(len(replies), 1)
        self.assertEqual(json.loads(replies[0]["content"]), result)
        self.assertEqual(receipt.payload["result"], result)
        signed, = [row for row in last["history"] if "provider_content" in row]
        self.assertEqual(signed["provider_content"][0]["type"], "thinking")
        self.assertTrue(signed["provider_content"][0]["signature"].startswith("private-signature-"))
        self.assertEqual(signed["tool_calls"], [{"id": started.payload["call_id"],
            "name": "read_artifact", "arguments": arguments}])
        self.assertEqual(self.f.protocol_errors, [])

    def test_unknown_effects_and_pause_preserve_reviews_work_roles_and_files(self):
        project = self.f.bind_workspace()
        seq = self.f.publish("frozen body")
        request = self.f.request([seq])
        self.assertEqual(request["status"], "ok")
        self.f.exchange(self.f.manager, ("delegate", {"recipient": self.f.worker, "instruction": "Keep work open"}))
        orphan, content = self.f.orphan_write(project)
        roles = copy.deepcopy((self.f.rt.organization, self.f.rt.roles))
        artifacts = self.f.rt.task(self.f.task).deliverables
        files = {Path(a["local_path"]): Path(a["local_path"]).read_bytes() for a in artifacts}
        files[project / "effect.txt"] = content.encode()
        before = self.f.rt.log.read(task_id=self.f.task)
        for paused in (False, True):
            if paused:
                self.f.rt.execution_control.change(self.f.task, "pause", request_id="pause-history-read")
            control = self.f.rt.execution_control.view(self.f.task)
            reviews = self.f.rt.team_reviews.view(self.f.task)
            self.assertEqual(reviews[0]["status"], "pending")
            self.assertEqual(reviews[0]["request_seq"], request["review_seq"])
            work = self.f.rt.team.work_control.view(self.f.task)
            assignments = self.f.rt.team.recovery.assignments(self.f.task)
            pending = self.f.rt.team._pending_operations(self.f.task)
            self.assertIn(orphan.seq, [row["started_seq"] for row in pending])
            self.assertEqual(self.read(seq, section="versions")["versions"],
                             [text_version(seq, self.f.worker, "frozen body")])
            self.assertEqual(self.read(seq)["content"], "frozen body")
            self.assertEqual(self.f.rt.execution_control.view(self.f.task), control)
            self.assertEqual(self.f.rt.team_reviews.view(self.f.task), reviews)
            self.assertEqual(self.f.rt.team.work_control.view(self.f.task), work)
            self.assertEqual(self.f.rt.team.recovery.assignments(self.f.task), assignments)
            self.assertEqual(self.f.rt.team._pending_operations(self.f.task), pending)
            if paused:
                names = {tool["name"] for tool in self.f.driver.calls[-1]["tools"]}
                self.assertIn("read_artifact", names)
                self.assertTrue(names.isdisjoint({"publish_artifact", "workspace_write", "workspace_run"}))
        self.assertEqual((self.f.rt.organization, self.f.rt.roles), roles)
        self.assertEqual(self.f.rt.task(self.f.task).deliverables, artifacts)
        self.assertEqual({path: path.read_bytes() for path in files}, files)
        after = self.f.rt.log.read(task_id=self.f.task)
        self.assertEqual(after[:len(before)], before)
        self.assertEqual({e.type for e in after[len(before):]} - {
            "task.execution_controlled", "team.message_received", "team.run_started", "team.run_finished",
            "team.model_called", "team.tool_started", "team.tool_finished"}, set())
        self.assertTrue(self.f.rt.verify_chain().ok)


class ExternalArtifactHistoryTests(unittest.TestCase):
    def test_json_backed_external_callbacks_match_durable_history_receipts(self):
        f = test_team_external.TeamExternalTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        expected = []

        def publish_and_read(run):
            for _ in range(2):
                seq = f.publish(run, "external body")
                expected.append(text_version(seq, f.manager, "external body"))
            result = run.call("read_artifact", artifact_seq=expected[0]["seq"], section="versions", limit=1)
            self.assertEqual(result, {"status": "ok", "section": "versions", "artifact_seq": expected[0]["seq"],
                "filename": "result.txt", "scope": "same_filename_in_this_chat", "versions": expected[:1],
                "versions_total": 2, "has_more": True, "next_seq": expected[0]["seq"]})
            tail = run.call("read_artifact", artifact_seq=expected[0]["seq"], section="versions",
                            after_seq=result["next_seq"])
            self.assertEqual(tail, {**result, "versions": expected[1:], "has_more": False,
                                    "next_seq": expected[1]["seq"]})
            body = run.call("read_artifact", artifact_seq=expected[0]["seq"])
            self.assertEqual(body, {"status": "ok", **f.rt.task(f.task).deliverables[0],
                                   "content_metrics": content_metrics("external body")})

        f.driver.scripts = {f.manager: [publish_and_read]}
        f.receive("Inspect persisted versions through external callbacks")
        f.drain()
        run, = f.driver.runs
        self.assertEqual(run.context["you"]["id"], f.manager)
        self.assertEqual(len(f.results("read_artifact")), 3)
        self.assertEqual(f.results("read_artifact"), [result for call, result in run.receipts
                                                    if call.name == "read_artifact"])
        self.assertEqual(f.rt.team._pending_operations(f.task), [])


if __name__ == "__main__":
    unittest.main()
