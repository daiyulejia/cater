"""Invalid optional explanations cannot block or fabricate a core delivery."""

import copy
import io
import json
import unittest
from unittest.mock import patch
import zipfile

from cater.events import Actor
from cater.team_delivery import HandoffError, HOST, REFERENCES_OMITTED_WARNING
import test_simple_team
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
    receipt = test_team_handoff.TeamHandoffTests.receipt

    def assert_omitted(self, value, *, secrets=(), references_only=False):
        original = copy.deepcopy(value)
        artifacts = self.refs()
        with self.assertRaises(HandoffError):
            self.normalize(value)
        expected = (self.normalize({key: item for key, item in value.items()
                    if key not in {"evidence", "command_receipts"}}) if references_only else None)
        result = self.finish(value)
        self.assertEqual(value, original)
        self.assertEqual(result["status"], "ok")
        completed, = self.events("team.objective_completed")
        payload = completed.payload
        for item in (result, payload):
            self.assertEqual(item["handoff_status"],
                             "references_omitted_invalid" if references_only else "omitted_invalid")
            self.assertTrue(item["handoff_warning"])
            self.assertLess(len(item["handoff_warning"]), 160)
            if references_only:
                self.assertEqual(item["handoff_warning"], REFERENCES_OMITTED_WARNING)
            for key in ("evidence", "command_receipts", "diagnostics", "available_sources"):
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
        delivery = self.rt.team.delivery.read(self.task, {})["delivery"]
        self.assertEqual(delivery["handoff_status"], payload["handoff_status"])
        self.assertEqual(delivery["handoff_warning"], payload["handoff_warning"])
        self.assertNotIn("handoff", result)
        if references_only:
            self.assertEqual(payload["handoff"], expected)
            self.assertEqual(delivery["handoff"], expected)
            self.assertEqual(expected["evidence"], [])
            self.assertNotIn("command_receipts", expected)
        else:
            self.assertNotIn("handoff", payload)
            self.assertNotIn("handoff", delivery)
        self.assertTrue(self.rt.verify_chain().ok)
        return completed

    def assert_references_omitted(self, value, *, secrets=()):
        return self.assert_omitted(value, secrets=secrets, references_only=True)

    def test_invalid_quote_preserves_prose_without_partial_evidence(self):
        artifact = self.artifact("actual source")
        self.assert_references_omitted(self.handoff([
            {"source_seq": artifact.seq, "quote": "actual source"},
            {"source_seq": artifact.seq, "quote": "UNTRUSTED_QUOTE"},
        ]), secrets=("UNTRUSTED_QUOTE", "actual source"))

    def test_foreign_source_is_not_copied_into_delivery_or_warning(self):
        self.artifact()
        foreign = self.artifact("FOREIGN_PRIVATE_BODY", task=self.rt.create_task("Foreign fixture"))
        self.assert_references_omitted(self.handoff([
            {"source_seq": foreign.seq, "quote": "FOREIGN_PRIVATE_BODY"},
        ]), secrets=("FOREIGN_PRIVATE_BODY",))

    def test_private_message_is_not_promoted_to_evidence(self):
        self.artifact()
        private = self.rt.log.emit(type="msg.said", task_id=self.task,
            actor=Actor.agent(self.worker, "worker"), payload={"text": "PRIVATE_SOURCE_BODY"})
        self.assert_references_omitted(self.handoff([
            {"source_seq": private.seq, "quote": "PRIVATE_SOURCE_BODY"},
        ]), secrets=("PRIVATE_SOURCE_BODY",))

    def test_missing_command_reference_does_not_create_execution_evidence(self):
        artifact = self.artifact("actual source")
        self.assert_references_omitted({**self.handoff([
            {"source_seq": artifact.seq, "quote": "actual source"},
        ]), "command_receipts": [999999]})
        self.assertEqual([e for e in self.events("team.tool_started")
                          if e.payload["name"] == "workspace_run"], [])

    def test_newline_mismatch_does_not_relax_strict_quote_contract(self):
        receipt = self.receipt(result={"status": "ok", "exit_code": 0,
            "stderr": {"excerpt": "Ran 9 tests\r\n\r\nOK"}})
        self.artifact()
        self.assert_references_omitted(self.handoff([
            {"source_seq": receipt.seq, "quote": "Ran 9 tests\n\nOK"},
        ]))

    def test_bad_quote_drops_otherwise_valid_command_records(self):
        receipt = self.receipt()
        artifact = self.artifact()
        commands = {**self.handoff(), "command_receipts": [receipt.seq]}
        self.assertEqual(len(self.normalize(commands)["command_receipts"]), 1)
        value = {**commands, "evidence": [{"source_seq": artifact.seq, "quote": "BAD_QUOTE_ONLY"}]}
        completed = self.assert_references_omitted(value, secrets=("BAD_QUOTE_ONLY",))
        note = self.rt.team.delivery.markdown(completed)
        self.assertIn(REFERENCES_OMITTED_WARNING, note)
        self.assertNotIn("Ran 5 tests", note)
        self.assertNotIn("### 命令回执", note)

    def test_bad_command_drops_valid_evidence_from_archive_and_read(self):
        artifact = self.artifact("VALID_REFERENCE_ONLY")
        value = {**self.handoff([{"source_seq": artifact.seq, "quote": "VALID_REFERENCE_ONLY"}]),
                 "command_receipts": [999999]}
        completed = self.assert_references_omitted(value, secrets=("VALID_REFERENCE_ONLY",))
        read = self.rt.team.delivery.read(self.task, {"delivery_seq": completed.seq})["delivery"]
        with zipfile.ZipFile(io.BytesIO(self.rt.team.archive(self.task,
                expected_delivery_seq=completed.seq))) as archive:
            self.assertEqual(archive.read("total.py").decode(), artifact.payload["content"])
            note = archive.read("__cater__/交付说明.md").decode()
            for text in (value["why"], value["reproduce"][0], value["limits"][0], read["handoff_warning"]):
                self.assertIn(text, note)
            for text in ("VALID_REFERENCE_ONLY", "来源事件哈希", "### 命令回执", "999999"):
                self.assertNotIn(text, note)
        saved = copy.deepcopy(read)
        self.rt.log.emit(type="team.objective_updated", task_id=self.task, actor=HOST,
                         payload={"goal": "Later work"})
        self.artifact("Changed file")
        historical = self.rt.team.delivery.read(self.task, {"delivery_seq": completed.seq})
        self.assertTrue(historical["historical"])
        self.assertEqual(historical["delivery"], saved)

    def test_read_delivery_tool_returns_preserved_prose_and_warning(self):
        artifact = self.artifact("actual source")
        completed = self.assert_references_omitted(self.handoff([
            {"source_seq": artifact.seq, "quote": "WRONG_QUOTE"},
        ]))

        def answer(context, history, tools):
            delivery = json.loads(history[-1]["content"])["delivery"]
            self.assertEqual(delivery["seq"], completed.seq)
            self.assertEqual(delivery["handoff_status"], "references_omitted_invalid")
            self.assertEqual(delivery["handoff_warning"], REFERENCES_OMITTED_WARNING)
            self.assertEqual(delivery["handoff"], completed.payload["handoff"])
            self.assertNotIn("WRONG_QUOTE", json.dumps(delivery))
            return test_simple_team.turn("The explanation was retained without verification evidence.")

        self.driver.steps[self.manager] = [test_simple_team.turn("", ("read_delivery", {
            "delivery_seq": completed.seq})), answer]
        message = self.rt.team.receive(self.task, "Read the saved explanation", request_id="read-salvaged-handoff")
        self.run_ok()
        self.assertEqual([e.payload["name"] for e in self.events("team.tool_started")
                          if e.payload["message_seq"] == message.seq], ["read_delivery"])
        self.assertEqual(self.events("team.objective_completed"), [completed])

    def test_invalid_body_is_not_salvaged_by_dropping_references(self):
        valid = self.handoff([{"source_seq": 999999, "quote": "BAD_REFERENCE_ONLY"}])
        cases = {
            "not_object": [],
            "missing_required": {key: item for key, item in valid.items() if key != "limits"},
            "empty_why": {**valid, "why": " "},
            "oversized_why": {**valid, "why": "x" * 4001},
            "list_type": {**valid, "changed": "not a list"},
            "list_item_type": {**valid, "limits": [True]},
            "empty_item": {**valid, "limits": [""]},
            "too_many_items": {**valid, "reproduce": ["step"] * 13},
            "long_item": {**valid, "changed": ["x" * 1001]},
            "unknown_field": {**valid, "thinking": "PRIVATE_UNKNOWN_FIELD"},
            "body_bytes": {**valid, "changed": ["中" * 1000] * 12},
        }
        for label, value in cases.items():
            with self.subTest(label=label):
                fixture = OptionalHandoffTests()
                fixture.setUp()
                try:
                    fixture.artifact()
                    fixture.assert_omitted(value, secrets=("BAD_REFERENCE_ONLY", "PRIVATE_UNKNOWN_FIELD"))
                finally:
                    fixture.doCleanups()

    def test_reference_size_overflow_keeps_body_within_its_existing_limit(self):
        artifact = self.artifact("q" * 1500)
        value = {**self.handoff([{"source_seq": artifact.seq, "quote": "q" * 1500}]),
                 "why": "w" * 3000, "changed": ["a" * 1000] * 12, "impact": ["b" * 1000] * 8}
        self.assert_references_omitted(value)

    def test_warning_does_not_copy_exception_diagnostics_or_source_catalog(self):
        self.artifact()
        error = HandoffError("PRIVATE_ERROR_BODY" * 1000,
            diagnostics=[{"quote": "PRIVATE_DIAGNOSTIC"}],
            source_catalog={"available_sources": ["PRIVATE_CATALOG"]})
        with patch.object(self.rt.team.delivery, "normalize", side_effect=error):
            self.assert_omitted(self.handoff(),
                secrets=("PRIVATE_ERROR_BODY", "PRIVATE_DIAGNOSTIC", "PRIVATE_CATALOG"))

    def test_preserved_prose_warning_does_not_echo_reference_diagnostics(self):
        artifact = self.artifact()
        normalize = self.rt.team.delivery.normalize

        def fail_references(task_id, value, **kwargs):
            if value.get("evidence"):
                raise HandoffError("PRIVATE_ERROR_BODY", diagnostics=[{"quote": "PRIVATE_DIAGNOSTIC"}],
                                   source_catalog={"available_sources": ["PRIVATE_CATALOG"]})
            return normalize(task_id, value, **kwargs)

        with patch.object(self.rt.team.delivery, "normalize", side_effect=fail_references):
            self.assert_references_omitted(self.handoff([{"source_seq": artifact.seq, "quote": "wrong"}]),
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

    def test_later_finish_cannot_replace_preserved_note_or_commit_again(self):
        artifact = self.artifact("actual source")
        completed = self.assert_references_omitted(self.handoff([{"source_seq": artifact.seq, "quote": "wrong"}]))
        saved = copy.deepcopy(completed.payload)
        with patch.object(self.rt.team.delivery, "normalize", side_effect=AssertionError("Unexpected retry")):
            result = self.finish(self.handoff([{"source_seq": artifact.seq, "quote": "actual source"}]))
        self.assertEqual((result["status"], result["code"], result["delivery_seq"]),
                         ("error", "delivery_already_completed", completed.seq))
        self.assertIsNot(result.get("already_completed"), True)
        self.assertEqual([e.payload for e in self.events("team.objective_completed")], [saved])
        self.assertEqual(self.rt.team.delivery.read(self.task, {})["delivery"]["seq"], completed.seq)

    def test_identical_repeat_uses_original_arguments_without_normalizing_again(self):
        artifact = self.artifact("actual source")
        value = self.handoff([{"source_seq": artifact.seq, "quote": "wrong"}])
        completed = self.assert_references_omitted(value)
        saved = copy.deepcopy(completed.payload)
        with zipfile.ZipFile(io.BytesIO(self.rt.team.archive(self.task, expected_delivery_seq=completed.seq))) as archive:
            before_files = {name: archive.read(name) for name in archive.namelist()}
        with patch.object(self.rt.team.delivery, "normalize", side_effect=AssertionError("Unexpected retry")):
            result = self.finish(value)
        self.assertIs(result.get("already_completed"), True)
        self.assertEqual(result["delivery_seq"], completed.seq)
        self.assertEqual([e.payload for e in self.events("team.objective_completed")], [saved])
        with zipfile.ZipFile(io.BytesIO(self.rt.team.archive(self.task, expected_delivery_seq=completed.seq))) as archive:
            self.assertEqual({name: archive.read(name) for name in archive.namelist()}, before_files)

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
