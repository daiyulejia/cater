"""Invalid optional explanations cannot block or fabricate a core delivery."""

import copy
import json
import unittest
from unittest.mock import patch

from cater.events import Actor
from cater.team_delivery import HandoffError, HOST
import test_team_handoff
import test_team_review_cycle


class OptionalHandoffTests(unittest.TestCase):
    setUp = test_team_handoff.TeamHandoffTests.setUp
    events = test_team_handoff.TeamHandoffTests.events
    run_ok = test_team_handoff.TeamHandoffTests.run_ok
    artifact = test_team_handoff.TeamHandoffTests.artifact
    refs = test_team_handoff.TeamHandoffTests.refs
    handoff = test_team_handoff.TeamHandoffTests.handoff
    normalize = test_team_handoff.TeamHandoffTests.normalize
    finish = test_team_handoff.TeamHandoffTests.finish

    def assert_omitted(self, value, *, secrets=()):
        original = copy.deepcopy(value)
        artifacts = self.refs()
        with self.assertRaises(HandoffError):
            self.normalize(value)
        result = self.finish(value)
        self.assertEqual(value, original)
        self.assertEqual(result["status"], "ok")
        completed, = self.events("team.objective_completed")
        payload = completed.payload
        for item in (result, payload):
            self.assertEqual(item["handoff_status"], "omitted_invalid")
            self.assertTrue(item["handoff_warning"])
            self.assertLess(len(item["handoff_warning"]), 160)
            for key in ("handoff", "evidence", "command_receipts", "diagnostics", "available_sources"):
                self.assertNotIn(key, item)
            for secret in secrets:
                self.assertNotIn(secret, json.dumps(item, ensure_ascii=False))
        self.assertEqual(result["handoff_warning"], payload["handoff_warning"])
        self.assertEqual(payload["artifacts"], artifacts)
        started, = [e for e in self.events("team.tool_started") if e.payload["name"] == "finish_work"]
        self.assertEqual(started.payload["arguments"]["handoff"], original)
        for key in ("summary", "verification"):
            self.assertEqual(payload[key], started.payload["arguments"][key])
        receipt, = [e for e in self.events("team.tool_finished") if e.payload["name"] == "finish_work"]
        self.assertEqual(receipt.payload["result"], result)
        self.assertEqual(completed.actor, HOST)
        self.assertLess(started.seq, completed.seq)
        self.assertLess(completed.seq, receipt.seq)
        self.assertEqual(len(self.driver.calls), 1)
        self.assertEqual(self.rt.team.view(self.task)["status"], "done")
        self.assertNotIn("handoff", self.rt.team.delivery.read(self.task, {})["delivery"])
        self.assertTrue(self.rt.verify_chain().ok)
        return completed

    def test_invalid_quote_discards_entire_handoff_without_partial_evidence(self):
        artifact = self.artifact("actual source")
        self.assert_omitted(self.handoff([
            {"source_seq": artifact.seq, "quote": "actual source"},
            {"source_seq": artifact.seq, "quote": "UNTRUSTED_QUOTE"},
        ]), secrets=("UNTRUSTED_QUOTE", "actual source"))

    def test_foreign_source_is_not_copied_into_delivery_or_warning(self):
        self.artifact()
        foreign = self.artifact("FOREIGN_PRIVATE_BODY", task=self.rt.create_task("Foreign fixture"))
        self.assert_omitted(self.handoff([
            {"source_seq": foreign.seq, "quote": "FOREIGN_PRIVATE_BODY"},
        ]), secrets=("FOREIGN_PRIVATE_BODY",))

    def test_private_message_is_not_promoted_to_evidence(self):
        self.artifact()
        private = self.rt.log.emit(type="msg.said", task_id=self.task,
            actor=Actor.agent(self.worker, "worker"), payload={"text": "PRIVATE_SOURCE_BODY"})
        self.assert_omitted(self.handoff([
            {"source_seq": private.seq, "quote": "PRIVATE_SOURCE_BODY"},
        ]), secrets=("PRIVATE_SOURCE_BODY",))

    def test_missing_command_reference_does_not_create_execution_evidence(self):
        artifact = self.artifact("actual source")
        self.assert_omitted({**self.handoff([
            {"source_seq": artifact.seq, "quote": "actual source"},
        ]), "command_receipts": [999999]})
        self.assertEqual([e for e in self.events("team.tool_started")
                          if e.payload["name"] == "workspace_run"], [])

    def test_warning_does_not_copy_exception_diagnostics_or_source_catalog(self):
        self.artifact()
        error = HandoffError("PRIVATE_ERROR_BODY" * 1000,
            diagnostics=[{"quote": "PRIVATE_DIAGNOSTIC"}],
            source_catalog={"available_sources": ["PRIVATE_CATALOG"]})
        with patch.object(self.rt.team.delivery, "normalize", side_effect=error):
            self.assert_omitted(self.handoff(),
                secrets=("PRIVATE_ERROR_BODY", "PRIVATE_DIAGNOSTIC", "PRIVATE_CATALOG"))

    def test_valid_handoff_keeps_existing_payload_and_tool_result_shape(self):
        artifact = self.artifact("actual source")
        value = self.handoff([{"source_seq": artifact.seq, "quote": "actual source"}])
        expected = self.normalize(value)
        result = self.finish(value)
        completed, = self.events("team.objective_completed")
        self.assertEqual(completed.payload["handoff"], expected)
        self.assertEqual(set(result), {"status", "note"})
        self.assertEqual(set(completed.payload), {
            "summary", "verification", "run_id", "objective_seq", "artifact_selection", "artifacts", "handoff",
        })

    def test_later_finish_cannot_replace_omitted_note_or_commit_again(self):
        artifact = self.artifact("actual source")
        completed = self.assert_omitted(self.handoff([{"source_seq": artifact.seq, "quote": "wrong"}]))
        saved = copy.deepcopy(completed.payload)
        with patch.object(self.rt.team.delivery, "normalize", side_effect=AssertionError("Unexpected retry")):
            result = self.finish(self.handoff([{"source_seq": artifact.seq, "quote": "actual source"}]))
        self.assertEqual((result["status"], result["code"], result["delivery_seq"]),
                         ("error", "delivery_already_completed", completed.seq))
        self.assertIsNot(result.get("already_completed"), True)
        self.assertEqual([e.payload for e in self.events("team.objective_completed")], [saved])
        self.assertEqual(self.rt.team.delivery.read(self.task, {})["delivery"]["seq"], completed.seq)

    def test_unexpected_normalizer_failure_still_does_not_commit(self):
        self.artifact()
        with patch.object(self.rt.team.delivery, "normalize", side_effect=ValueError("unexpected failure")):
            result = self.finish(self.handoff())
        self.assertEqual(result["status"], "error")
        self.assertEqual(self.events("team.objective_completed"), [])


class OptionalHandoffGateTests(unittest.TestCase):
    def setUp(self):
        self.f = test_team_review_cycle.TeamReviewCycleTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def assert_blocked(self, code, **overrides):
        f = self.f
        arguments = {"summary": "Core result", "verification": "Fixture checks only",
            "handoff": {"why": "Optional note", "reproduce": [], "limits": [],
                        "evidence": [{"source_seq": 999999, "quote": "wrong"}]}, **overrides}
        with patch.object(f.rt.team.delivery, "normalize", side_effect=AssertionError("Core gate bypassed")) as normalize:
            result, = f.exchange(f.manager, ("finish_work", arguments))
        normalize.assert_not_called()
        self.assertEqual(result["status"], "error")
        if code is not None:
            self.assertEqual(result["code"], code)
        self.assertNotIn("handoff_status", result)
        self.assertEqual(f.events("team.objective_completed"), [])

    def test_pending_review_still_blocks_delivery(self):
        f = self.f
        f.request([f.publish()])
        self.assert_blocked("review_pending")

    def test_delegated_work_still_blocks_delivery(self):
        f = self.f
        f.exchange(f.manager, ("delegate", {"recipient": f.worker, "instruction": "Check core result"}))
        self.assert_blocked("work_pending")

    def test_invalid_selected_artifact_still_blocks_delivery(self):
        self.assert_blocked("invalid_delivery_files", artifact_seqs=[999999])

    def test_unapproved_version_still_blocks_delivery(self):
        f = self.f
        old = f.publish("old version")
        current = f.publish("current version")
        request = f.request([current])
        f.record(request["review_seq"])
        f.acknowledge_latest_review()
        self.assert_blocked("delivery_review_mismatch", artifact_seqs=[old])

    def test_empty_core_verification_still_blocks_delivery(self):
        self.assert_blocked(None, verification="")


if __name__ == "__main__":
    unittest.main()
