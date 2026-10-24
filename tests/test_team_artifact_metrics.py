"""Host read_artifact protocol tests, not model-quality acceptance.

Use real Runtime instances with offline native/external scripted drivers only.
The existing cater.artifacts.content_metrics is the counting contract.
"""

import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from cater.artifacts import content_metrics, normalize_deliverables
from cater.events import Actor
from cater.external_agent import QoderAcpDriver
from cater.tool_protocol import RecordedToolResult, ToolCall
import test_team_external
import test_team_review_cycle


class TeamArtifactMetricsTests(unittest.TestCase):
    def setUp(self):
        self.f = test_team_review_cycle.TeamReviewCycleTests()
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()

    def read(self, seq, **arguments):
        result, = self.f.exchange(self.f.worker, (
            "read_artifact", {"artifact_seq": seq, **arguments}))
        return result

    def legacy(self, **fields):
        return self.f.rt.log.emit(
            type="deliverable.published", task_id=self.f.task,
            actor=Actor.agent(self.f.worker, self.f.rt.team._role(self.f.worker).role),
            payload={"title": "Historical artifact", "summary": "Offline fixture",
                     "source": "team", **fields})

    def assert_metrics(self, result, body):
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["content_metrics"], content_metrics(body))

    def archive(self):
        project = self.f.bind_workspace()
        bodies = {"note.md": "# \u4e2d\u6587\nA\U00020000\U0001f680\n".encode("utf-8"),
                  "invalid.bin": b"\xff\xfe", "empty.txt": b""}
        for name, body in bodies.items():
            (project / name).write_bytes(body)
        result, = self.f.exchange(self.f.worker, ("publish_workspace_archive", {
            "paths": list(bodies), "filename": "project.zip", "summary": "Offline snapshot"}))
        self.assertEqual(result["status"], "ok", result)
        return result["artifact_seq"], bodies

    def test_complete_bodies_use_existing_metrics_and_preserve_all_other_fields(self):
        for format, body in (("text", "Complete original body\r\n"),
                             ("code", "print('hello')\n"),
                             ("markdown", "# Heading\n**bold** [link](https://example.invalid)\n")):
            with self.subTest(format=format):
                published, = self.f.exchange(self.f.worker, ("publish_artifact", {
                    "filename": f"result-{format}.txt", "content": body,
                    "summary": "Offline fixture", "format": format}))
                self.assertEqual(published["status"], "ok", published)
                seq = published["artifact_seq"]
                artifact = next(a for a in self.f.rt.task(self.f.task).deliverables if a["seq"] == seq)
                for arguments in ({}, {"section": "content"}):
                    result = self.read(seq, **arguments)
                    self.assertEqual(result, {"status": "ok", **artifact,
                                              "content_metrics": content_metrics(body)})
                    self.assertNotIn("read_range", result)
                self.assertEqual(Path(artifact["local_path"]).read_bytes(), body.encode("utf-8"))

    def test_historical_empty_string_has_real_zero_metrics(self):
        # Current publication rejects empty text; the event fold preserves legacy text.
        event = self.legacy(content="", format="text", filename="empty.txt")
        self.assertEqual(self.f.rt.task(self.f.task).deliverables[0]["content"], "")
        for arguments in ({}, {"offset": 0, "length": 1}):
            with self.subTest(arguments=arguments):
                result = self.read(event.seq, **arguments)
                self.assert_metrics(result, "")
                self.assertEqual(result["content"], "")
                self.assertEqual(result["content_metrics"], {
                    "basis": "raw_content_including_markdown", "utf8_bytes": 0,
                    "codepoints": 0, "non_whitespace_codepoints": 0, "cjk_ideographs": 0})

    def test_mixed_han_extension_b_compatibility_emoji_and_whitespace_keep_units(self):
        body = "# \u4e2d\u6587\n A\t\u6587\u3000\U00020000\uf900\U0001f680\u0301"
        result = self.read(self.f.publish(body))
        self.assert_metrics(result, body)
        self.assertEqual(result["content"], body)
        self.assertEqual(result["content_metrics"]["cjk_ideographs"], 5)
        self.assertEqual(result["content_metrics"]["codepoints"], 14)
        self.assertEqual(result["content_metrics"]["non_whitespace_codepoints"], 9)
        self.assertGreater(result["content_metrics"]["utf8_bytes"], 14)

    def test_middle_page_measures_full_saved_body_before_slicing(self):
        body = "# \u4e2d\u6587\nA\U00020000\U0001f680\n" * 2000
        seq = self.f.publish(body)
        with patch("cater.team.content_metrics", wraps=content_metrics) as measure:
            result = self.read(seq, offset=137, length=11)
        measure.assert_called_once_with(body)
        self.assert_metrics(result, body)
        self.assertEqual(result["content"], body[137:148])
        self.assertNotEqual(result["content_metrics"], content_metrics(result["content"]))
        self.assertEqual(result["read_range"]["returned_chars"], 11)
        self.assertEqual(result["read_range"]["total_chars"], len(body))
        self.assertFalse(result["read_range"]["content_complete"])

    def test_tail_and_empty_eof_page_keep_nonzero_full_body_metrics(self):
        body = "prefix \u4e2d\u6587\U00020000\U0001f680 tail"
        seq = self.f.publish(body)
        for offset in (len(body) - 2, len(body)):
            with self.subTest(offset=offset):
                result = self.read(seq, offset=offset, length=20)
                self.assert_metrics(result, body)
                self.assertEqual(result["content"], body[offset:])
                self.assertFalse(result["read_range"]["content_complete"])
                self.assertFalse(result["read_range"]["has_more"])
                self.assertGreater(result["content_metrics"]["codepoints"], 0)

    def test_old_and_new_exact_versions_ignore_later_disk_contents(self):
        old_body, new_body = "old \u4e2d\u6587\n", "new \U00020000\U0001f680\n" * 9
        old, new = self.f.publish(old_body), self.f.publish(new_body)
        for artifact in self.f.rt.task(self.f.task).deliverables:
            Path(artifact["local_path"]).write_bytes(b"changed on disk")
        for seq, body in ((old, old_body), (new, new_body)):
            with self.subTest(seq=seq):
                result = self.read(seq)
                self.assert_metrics(result, body)
                self.assertEqual(result["content"], body)
                self.assert_metrics(self.read(seq, offset=1, length=2), body)
        self.assertNotEqual(content_metrics(old_body), content_metrics(new_body))

    def test_forged_metrics_cannot_override_host_or_mutate_publication(self):
        body = "host \u4e2d\u6587\U00020000\n"
        payload, = normalize_deliverables([{"title": "Claim", "summary": "Offline fixture",
            "filename": "claim.txt", "format": "text", "content": body}])
        forged = {**content_metrics(body), "cjk_ideographs": 999999, "basis": "MODEL_CLAIM"}
        event = self.legacy(**payload, content_metrics=forged)
        before = copy.deepcopy(self.f.rt.log.read(task_id=self.f.task))
        projected = copy.deepcopy(self.f.rt.task(self.f.task).deliverables)
        for arguments in ({}, {"offset": 1, "length": 2}):
            self.assert_metrics(self.read(event.seq, **arguments), body)
        self.assertEqual(self.f.rt.task(self.f.task).deliverables, projected)
        after = self.f.rt.log.read(task_id=self.f.task)
        self.assertEqual(after[:len(before)], before)
        current = next(e for e in after if e.seq == event.seq)
        self.assertEqual(current, event)
        self.assertEqual(current.payload["content_metrics"], forged)
        self.assertTrue(self.f.rt.verify_chain().ok)

    def test_missing_and_nonstring_bodies_never_get_invented_zero_metrics(self):
        cases = ({}, {"content": None}, {"content": 0}, {"content": {"text": "not a body"}})
        for fields in cases:
            with self.subTest(fields=fields):
                event = self.legacy(**fields, content_metrics={"codepoints": 0})
                result = self.read(event.seq)
                self.assertEqual(result["status"], "ok", result)
                self.assertNotIn("content_metrics", result)
                if "content" in fields:
                    self.assertEqual(result["content"], fields["content"])
                else:
                    self.assertNotIn("content", result)
                page = self.read(event.seq, offset=0, length=1)
                self.assertEqual(page["status"], "error", page)
                self.assertNotIn("content_metrics", page)

    def test_invalid_utf8_normalization_preserves_error_without_fake_counts(self):
        # Raw surrogates cannot enter EventLog's UTF-8 fingerprint. Use the real
        # publication normalizer, which removes that body before persistence.
        payload, = normalize_deliverables([{"title": "Invalid historical text",
            "summary": "Offline fixture", "content": "before\ud800after", "format": "text"}])
        self.assertEqual(payload["content_error"], "invalid_encoding")
        self.assertNotIn("content", payload)
        event = self.legacy(**payload, content_metrics={"utf8_bytes": 0})
        result = self.read(event.seq)
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["content_error"], "invalid_encoding")
        self.assertNotIn("content", result)
        self.assertNotIn("content_metrics", result)
        self.assertEqual(self.read(event.seq, length=1)["status"], "error")

    def test_versions_catalog_contains_metadata_without_computing_counts(self):
        seqs = [self.f.publish(body) for body in ("old \u4e2d", "new \U00020000\U0001f680")]
        artifacts = self.f.rt.task(self.f.task).deliverables
        fields = ("seq", "filename", "actor", "source", "format", "content_bytes", "content_sha256")
        with patch("cater.team.content_metrics", side_effect=AssertionError("Metadata is not a body")) as measure:
            first = self.read(seqs[0], section="versions", limit=1)
            second = self.read(seqs[0], section="versions", after_seq=first["next_seq"])
        measure.assert_not_called()
        self.assertEqual(first["status"], "ok", first)
        self.assertEqual(second["status"], "ok", second)
        self.assertEqual(first["versions"] + second["versions"],
                         [{key: a[key] for key in fields} for a in artifacts])
        for result in (first, second):
            self.assertNotIn("content_metrics", result)
            self.assertTrue(all("content_metrics" not in row and "content" not in row
                                for row in result["versions"]))

    def test_archive_manifest_does_not_guess_text_counts_from_zip_bytes(self):
        seq, _ = self.archive()
        artifact, = self.f.rt.task(self.f.task).deliverables
        with patch("cater.team.content_metrics", side_effect=AssertionError("ZIP is not plain text")) as measure:
            result = self.read(seq)
        measure.assert_not_called()
        self.assertEqual(result, {"status": "ok", **artifact})
        self.assertNotIn("content_metrics", result)
        self.assertNotIn("content", result)
        self.assertTrue(all("content_metrics" not in row for row in result["archive"]["files"]))

    def test_zip_text_empty_and_binary_members_preserve_existing_read_contract(self):
        seq, bodies = self.archive()
        with patch("cater.team.content_metrics", side_effect=AssertionError("ZIP path returns first")) as measure:
            for name, body in bodies.items():
                with self.subTest(path=name):
                    result = self.read(seq, path=name, offset=0, length=4)
                    self.assertEqual(result["status"], "ok", result)
                    self.assertNotIn("content_metrics", result)
                    if name == "invalid.bin":
                        self.assertEqual(result["content_kind"], "binary")
                        self.assertIsNone(result["content"])
                    else:
                        self.assertEqual(result["content_kind"], "utf8")
                        self.assertEqual(result["content"], body.decode("utf-8")[:4])
        measure.assert_not_called()

    def test_cross_group_rejection_leaks_neither_body_metadata_nor_metrics(self):
        other = self.f.rt.create_task("PRIVATE_METRICS_GOAL")
        self.f.rt.team.enable(other)
        private = "PRIVATE_METRICS_BODY \u4e2d\u6587\U00020000\U0001f680"
        seq = self.f.publish(private, filename="private-metrics.txt", task_id=other)
        calls_before = len(self.f.driver.calls)
        with patch("cater.team.content_metrics", side_effect=AssertionError("Foreign body measured")) as measure:
            results = [self.read(seq, **arguments) for arguments in
                       ({}, {"offset": 0, "length": 1}, {"section": "versions"})]
        measure.assert_not_called()
        for result in results:
            self.assertEqual(result["status"], "error", result)
            self.assertNotIn("content_metrics", result)
        visible = json.dumps({"results": results, "calls": self.f.driver.calls[calls_before:]})
        for sentinel in ("PRIVATE_METRICS_GOAL", "PRIVATE_METRICS_BODY", "private-metrics.txt"):
            self.assertNotIn(sentinel, visible)

    def test_metrics_and_complete_page_do_not_approve_pending_review(self):
        body = "Review required: 1000 words; \u4e2d\u6587\n"
        seq = self.f.publish(body)
        request = self.f.request([seq])
        self.assertEqual(request["status"], "ok", request)
        before = copy.deepcopy(self.f.rt.team_reviews.view(self.f.task))
        decisions = self.f.events("team.review_recorded")
        result = self.read(seq, offset=0, length=8000)
        self.assert_metrics(result, body)
        self.assertTrue(result["read_range"]["content_complete"])
        self.assertEqual(self.f.rt.team_reviews.view(self.f.task), before)
        self.assertEqual(before[0]["status"], "pending")
        self.assertEqual(self.f.events("team.review_recorded"), decisions)
        self.assertEqual(set(result["content_metrics"]), set(content_metrics(body)))

    def test_native_continuation_and_durable_receipt_receive_same_host_metrics(self):
        body = "native \u4e2d\u6587\U00020000\U0001f680\n"
        seq = self.f.publish(body)
        for arguments in ({}, {"offset": 2, "length": 3}):
            with self.subTest(arguments=arguments):
                result = self.read(seq, **arguments)
                self.assert_metrics(result, body)
                history = self.f.driver.calls[-1]["history"]
                self.assertEqual(json.loads(history[-1]["content"]), result)
                receipt = self.f.results("read_artifact")[-1]
                self.assertEqual(receipt, result)
                signed = [row for row in history if "provider_content" in row]
                self.assertTrue(signed)
                self.assertTrue(all(row["provider_content"][0]["type"] == "thinking" for row in signed))
        self.assertGreater(self.f.validated_continuations, 0)
        self.assertEqual(self.f.protocol_errors, [])


class ExternalTeamArtifactMetricsTests(unittest.TestCase):
    def setUp(self):
        self.f = test_team_external.TeamExternalTests()
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()

    def assert_read_receipts(self, expected):
        run, = self.f.driver.runs
        results = [result for call, result in run.receipts if call.name == "read_artifact"]
        self.assertEqual(results, expected)
        self.assertEqual(self.f.results("read_artifact"), expected)
        for result in results:
            self.assertIsInstance(result, RecordedToolResult)
            receipt = next(e for e in self.f.events("team.tool_finished") if e.seq == result.receipt_seq)
            self.assertEqual(receipt.payload["result"], result)
            self.assertEqual(receipt.hash, result.receipt_hash)
        self.assertEqual(self.f.rt.team._pending_operations(self.f.task), [])

    def test_external_default_and_page_pass_complete_metrics_to_callback(self):
        body = "external \u4e2d\u6587\U00020000\U0001f680\n"
        expected = []

        def script(run):
            seq = self.f.publish(run, body)
            for arguments in ({}, {"offset": 3, "length": 2}):
                result = run.call("read_artifact", artifact_seq=seq, **arguments)
                self.assertEqual(result["status"], "ok", result)
                self.assertEqual(result["content_metrics"], content_metrics(body))
                self.assertEqual(result["content"], body if not arguments else body[3:5])
                expected.append(result)

        self.f.driver.scripts = {self.f.manager: [script]}
        self.f.receive("Read metrics through offline external callbacks")
        self.f.drain()
        self.assertEqual(len(expected), 2)
        self.assert_read_receipts(expected)

    def test_external_catalog_invocation_preserves_host_metrics_and_receipt_pointer(self):
        body = "catalog \u4e2d\u6587\U00020000\U0001f680\n"
        expected = []
        driver = QoderAcpDriver("offline-catalog", "never-launch", "offline", tool_surface="catalog")
        self.addCleanup(driver.close)

        def script(run):
            seq = self.f.publish(run, body)

            def execute(call):
                result = run.execute(call)
                run.receipts.append((call, result))
                return result

            # The production catalog adapter is lazy: call its callback only,
            # with process/session/bridge startup explicitly forbidden.
            with patch("subprocess.Popen", side_effect=AssertionError("No real process")), \
                 patch("cater.external_agent.AcpSession", side_effect=AssertionError("No real model")), \
                 patch("cater.external_agent.McpToolBridge", side_effect=AssertionError("No real bridge")):
                catalog = driver.agent_run(cwd=run.cwd, tools=run.tools, execute=execute,
                                           check_current=run.check_current, on_update=None)
                described = catalog._catalog_call(ToolCall("describe-metrics", "describe_tools",
                                                          {"names": ["read_artifact"]}))
                self.assertEqual(described["status"], "ok", described)
                self.assertEqual(len(self.f.results("read_artifact")), 0)
                for index, arguments in enumerate(({}, {"offset": 1, "length": 4})):
                    result = catalog._catalog_call(ToolCall(f"catalog-metrics-{index}", "invoke_tool", {
                        "name": "read_artifact", "arguments": {"artifact_seq": seq, **arguments}}))
                    self.assertEqual(result["status"], "ok", result)
                    self.assertEqual(result["content_metrics"], content_metrics(body))
                    self.assertEqual(result["content"], body if not arguments else body[1:5])
                    self.assertIs(result, run.receipts[-1][1])
                    self.assertEqual(run.receipts[-1][0].id, f"catalog-metrics-{index}")
                    self.assertEqual(run.receipts[-1][0].name, "read_artifact")
                    expected.append(result)

        self.f.driver.scripts = {self.f.manager: [script]}
        self.f.receive("Read metrics through offline catalog callbacks")
        self.f.drain()
        self.assertEqual(len(expected), 2)
        self.assert_read_receipts(expected)


if __name__ == "__main__":
    unittest.main()
