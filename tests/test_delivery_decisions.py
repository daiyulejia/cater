from __future__ import annotations

import copy
import hashlib
import io
import json
import unittest
import zipfile
from unittest.mock import patch

from cater.anthropic import AnthropicMessagesDriver
from cater.delivery import DeliveryService, delivery_decision_schema
from cater.drivers import DriverResponse, OpenAICompatibleDriver
from cater.events import Actor
import test_delivery_json_repair as repair_fixtures
import test_delivery_review_recovery as fixtures


def decision(choice="approve", **extra):
    return {"decision": choice, "reason": "已核对当前要求、正文及检查的适用范围。",
            "handoff": {"summary": "交付当前报告。", "why": "报告保留明确的复查依据。",
                        "reproduce": ["打开 report.md 核对实际内容。"], "limits": ["未执行外部 API 验证。"]},
            **extra}


class DecisionDriver:
    def __init__(self, raw=None):
        self.raw = raw or decision()
        self.calls = []
        self.after = None

    def complete(self, *, role, prompt, context):
        self.calls.append((prompt, copy.deepcopy(context["work"])))
        if self.after:
            self.after(context["work"])
        return DriverResponse(json.dumps(self.raw, ensure_ascii=False), 3, {})


class DeliveryDecisionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.DeliveryReviewRecoveryTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.driver = DecisionDriver()
        self.rt, self.task, _ = self.fixture.runtime(driver=self.driver)

    def context(self):
        return self.rt.delivery._start(self.task)[1]

    def test_approve_binds_all_host_inputs_without_manufactured_quotes(self):
        result = self.rt.delivery.run(self.task)
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["review_protocol"], "decision_v1")
        self.assertEqual(result["reason"], self.driver.raw["reason"])
        prompt, context = self.driver.calls[0]
        self.assertEqual([row["id"] for row in result["requirements"]],
                         [source["id"] for source in context["manifest"]["sources"]])
        self.assertEqual([row["id"] for row in result["checks"]],
                         [criterion["id"] for criterion in context["manifest"]["criteria"]])
        for row in result["requirements"] + result["checks"]:
            self.assertEqual(row["status"], "passed")
            self.assertNotIn("evidence", row)
            self.assertNotIn("quote", row)
        binding = result["evidence_binding"]
        self.assertEqual(binding["delivery_digest"], context["delivery_digest"])
        self.assertEqual(binding["plan_seq"], context["manifest"]["plan_seq"])
        self.assertEqual(binding["scope"], context["scope"])
        self.assertEqual(binding["sources"], context["manifest"]["sources"])
        self.assertEqual(binding["artifacts"], [
            {"seq": a["seq"], "sha256": hashlib.sha256(a["content"].encode()).hexdigest()}
            for a in context["artifacts"]])
        self.assertEqual(binding["local_checks"], context["local_checks"])
        self.assertNotIn('"quote":', json.dumps(binding))
        self.assertLess(len(prompt.split("\n", 1)[0]), 1100)
        envelope = json.loads(prompt.split("\n", 1)[1])
        self.assertEqual(envelope.pop("schema"), delivery_decision_schema(context))
        self.assertEqual(envelope, context)
        self.assertEqual(delivery_decision_schema(context)["required"], ["decision", "reason", "handoff"])
        self.assertEqual(delivery_decision_schema(context)["properties"]["affected"]["items"]["enum"], ["draft"])

    def test_missing_failed_or_stale_whole_receipt_cannot_approve(self):
        context = self.context()
        context["manifest"]["criteria"].append({"id": "integration_behavior", "kind": "local_check", "text": "Whole check"})
        receipt = {"seq": 999, "delivery_digest": context["delivery_digest"], "criterion_id": "integration_behavior",
                   "outcome": "passed", "exit_code": 0, "artifacts_unchanged": True,
                   "stdout": {"complete": True}, "stderr": {"complete": True}}
        variants = [{}, {"behavior": {**receipt, "criterion_id": "behavior"}},
                    {"integration_behavior": {**receipt, "outcome": "failed", "exit_code": 1}},
                    {"integration_behavior": {**receipt, "delivery_digest": "old"}},
                    {"integration_behavior": {**receipt, "artifacts_unchanged": False}}]
        for checks in variants:
            with self.subTest(checks=checks):
                context["local_checks"] = checks
                self.assertEqual(DeliveryService._verdict(decision(), context)["status"], "needs_evidence")
        context["local_checks"] = {"integration_behavior": receipt}
        result = DeliveryService._verdict(decision(), context)
        self.assertEqual(result["status"], "accepted")
        whole_check = next(row for row in result["checks"] if row["id"] == "integration_behavior")
        self.assertEqual(whole_check["receipt_seq"], 999)
        self.assertEqual(result["evidence_binding"]["local_checks"], context["local_checks"])
        receipt["exit_code"] = 1
        self.assertEqual(result["evidence_binding"]["local_checks"]["integration_behavior"]["exit_code"], 0)

    def test_host_gate_requires_whole_receipt_even_after_stage_passes(self):
        task = self.rt.create_task("Deliver with behavior evidence")
        with patch.object(self.fixture, "check"):
            self.fixture.prepare(self.rt, task, local=True)
        self.assertTrue(self.rt.work.view(task)["stage_accepted"])
        self.assertNotEqual(self.rt.delivery.run(task)["status"], "accepted")
        self.assertEqual(self.driver.calls, [])
        view = self.rt.delivery.view(task)
        started = self.rt.delivery._start_check(task, view, "integration_behavior", 60)
        self.rt.log.emit(type="verification.completed", task_id=task, actor=Actor.system("local_verifier"),
            payload={"delivery_digest": view["digest"], "criterion_id": "integration_behavior", "check_attempt_seq": started.seq,
                     "outcome": "failed", "exit_code": 1, "artifacts_unchanged": True,
                     "stdout": {"complete": True}, "stderr": {"complete": True}})
        self.assertNotEqual(self.rt.delivery.run(task)["status"], "accepted")
        self.assertEqual(self.driver.calls, [])
        self.fixture.check(self.rt, task)
        self.assertEqual(self.rt.delivery.run(task)["status"], "accepted")

    def test_revise_requires_explicit_unique_current_item_ids(self):
        context = self.context()
        for affected in (None, [], ["old"], ["draft", "draft"], [True], [["draft"]], "draft"):
            with self.subTest(affected=affected), self.assertRaises(ValueError):
                DeliveryService._verdict(decision("revise_artifact", affected=affected), context)
        with self.assertRaises(ValueError):
            DeliveryService._verdict(decision("revise_artifact"), context)
        with self.assertRaises(ValueError):
            DeliveryService._verdict(decision(affected=["draft"]), context)
        raw = decision("revise_artifact", affected=["draft"])
        result = DeliveryService._verdict(raw, context)
        self.assertEqual((result["status"], result["affected"]), ("rework", ["draft"]))
        self.assertEqual([row["id"] for row in result["requirements"]],
                         [source["id"] for source in context["manifest"]["sources"]])
        self.assertTrue(all(row["status"] == "failed" for row in result["requirements"] + result["checks"]))
        self.assertEqual(DeliveryService._verdict(decision("needs_evidence"), context)["status"], "needs_evidence")

    def test_revise_invalidates_named_item_without_citation_mapping(self):
        self.driver.raw = decision("revise_artifact", affected=["draft"])
        result = self.rt.delivery.run(self.task)
        self.assertEqual(result["status"], "stale")
        finish = self.fixture.events(self.rt, self.task, "delivery.review_finished")[-1]
        self.assertEqual(finish.payload["status"], "rework")
        self.assertEqual(finish.payload["affected"], ["draft"])
        self.assertEqual(finish.payload["review_protocol"], "decision_v1")
        self.assertIsNone(self.rt.work.view(self.task)["items"][0]["submission"])

    def test_later_human_direction_discards_decision(self):
        self.driver.after = lambda _: self.rt.command(self.task, "新的要求：添加后续修改说明")
        self.assertEqual(self.rt.delivery.run(self.task)["status"], "stale")
        self.assertNotEqual(self.rt.task(self.task).status, "done")

    def test_model_cannot_mutate_host_binding(self):
        def mutate(context):
            context["artifacts"][0]["content"] = "invented"
            context["manifest"]["sources"].clear()
        self.driver.after = mutate
        result = self.rt.delivery.run(self.task)
        self.assertEqual(result["status"], "accepted")
        original = self.driver.calls[0][1]
        self.assertEqual(result["evidence_binding"]["sources"], original["manifest"]["sources"])
        self.assertEqual(result["evidence_binding"]["artifacts"][0]["sha256"],
                         hashlib.sha256(original["artifacts"][0]["content"].encode()).hexdigest())

    def test_legacy_response_remains_accepted(self):
        context = self.context()
        raw = json.loads(fixtures.DeliveryDriver().complete(role="fixture", prompt="", context={"work": context}).text)
        result = DeliveryService._verdict(raw, context)
        self.assertEqual(result["status"], "accepted")
        self.assertTrue(result["requirements"][0]["evidence"])
        self.assertNotIn("review_protocol", result)

    def test_json_repair_cannot_upgrade_negative_decision(self):
        wire = repair_fixtures.DeliveryJsonRepairTests.wire
        for cls in (AnthropicMessagesDriver, OpenAICompatibleDriver):
            for syntax in ("delimiter", "closing_brace"):
                for choice in ("revise_artifact", "needs_evidence"):
                    for upgrade in (False, True):
                        with self.subTest(adapter=cls.__name__, syntax=syntax, choice=choice, upgrade=upgrade):
                            rt, task, _ = self.fixture.runtime(cls)
                            raw = decision(choice, **({"affected": ["draft"]} if choice == "revise_artifact" else {}))
                            original = json.dumps(raw, ensure_ascii=False)
                            bad = (original[:-1] if syntax == "closing_brace" else
                                   original.replace(', "reason":', ' "reason":', 1))
                            repaired = json.dumps(decision(), ensure_ascii=False) if upgrade else original
                            with patch(self.fixture.transport(cls), side_effect=[wire(bad, cls), wire(repaired, cls)]) as opened:
                                with patch.object(rt, "_complete_with_containment", wraps=rt._complete_with_containment) as gateway:
                                    rt.delivery.run(task)
                                self.assertEqual(opened.call_count, 2)
                                calls = gateway.call_args_list
                                self.assertEqual(calls[0].kwargs["work_context"], calls[1].kwargs["work_context"])
                                self.assertEqual(calls[0].kwargs["rule_snapshot"], calls[1].kwargs["rule_snapshot"])
                                self.assertIn("Repair ONLY JSON syntax", calls[1].kwargs["prompt"])
                            finishes = self.fixture.events(rt, task, "delivery.review_finished")
                            self.assertEqual(len(finishes), 1)
                            self.assertEqual(len(self.fixture.events(rt, task, "delivery.review_started")), 1)
                            expected = "invalid" if upgrade else "rework" if choice == "revise_artifact" else "needs_evidence"
                            self.assertEqual(finishes[0].payload["status"], expected)
                            self.assertNotEqual(rt.task(task).status, "done")
                            if not upgrade:
                                self.assertEqual(finishes[0].payload["decision"], choice)
                                self.assertEqual(finishes[0].payload["review_protocol"], "decision_v1")
                            else:
                                self.assertNotIn("decision", finishes[0].payload)

    def test_valid_needs_evidence_is_not_reconsidered_as_a_syntax_repair(self):
        wire = repair_fixtures.DeliveryJsonRepairTests.wire
        for cls in (AnthropicMessagesDriver, OpenAICompatibleDriver):
            with self.subTest(adapter=cls.__name__):
                rt, task, _ = self.fixture.runtime(cls)
                responses = [wire(json.dumps(decision("needs_evidence")), cls), wire(json.dumps(decision()), cls)]
                with patch(self.fixture.transport(cls), side_effect=responses) as opened:
                    self.assertEqual(rt.delivery.run(task)["status"], "needs_evidence")
                    self.assertEqual(rt.delivery.run(task)["status"], "needs_evidence")
                    opened.assert_called_once()
                self.assertNotEqual(rt.task(task).status, "done")
                finishes = self.fixture.events(rt, task, "delivery.review_finished")
                self.assertEqual(len(finishes), 1)
                self.assertEqual(finishes[0].payload["decision"], "needs_evidence")
                self.assertEqual(finishes[0].payload["review_protocol"], "decision_v1")

    def test_full_handoff_is_required_and_exported_with_the_delivery(self):
        context = self.context()
        handoff = decision()["handoff"]
        invalid = [None, {}, {**handoff, "extra": "unexpected"},
                   {**handoff, "summary": " "}, {**handoff, "why": ""},
                   {**handoff, "reproduce": []}, {**handoff, "reproduce": "open report.md"},
                   {**handoff, "limits": None}, {**handoff, "limits": [""]}]
        for value in invalid:
            with self.subTest(handoff=value), self.assertRaises(ValueError):
                DeliveryService._verdict(decision(handoff=value), context)
        with self.assertRaises(ValueError):
            raw = decision()
            del raw["handoff"]
            DeliveryService._verdict(raw, context)
        verdict = DeliveryService._verdict(decision(), context)
        started = self.fixture.events(self.rt, self.task, "delivery.review_started")[-1]
        self.rt.delivery._finish(self.task, started, context, verdict)
        with zipfile.ZipFile(io.BytesIO(self.rt.delivery.archive(self.task))) as archive:
            self.assertEqual(archive.read("work-1/report.md").decode(), context["artifacts"][0]["content"])
            exported = archive.read("handoff.md").decode()
            for value in (handoff["summary"], handoff["why"], *handoff["reproduce"], *handoff["limits"]):
                self.assertIn(value, exported)

    def test_host_maps_current_sources_and_criteria_for_every_decision(self):
        context = self.context()
        context["manifest"]["sources"].extend([
            {"id": "20:instruction:0", "seq": 20, "text": "Include the later direction."},
            {"id": "rule:quality:1", "seq": 21, "text": "Retain reproducibility.", "rule": True}])
        context["manifest"]["criteria"].append({"id": "compatibility", "kind": "review", "text": "Inspect compatibility."})
        for choice, status in (("approve", "passed"), ("revise_artifact", "failed"), ("needs_evidence", "unknown")):
            with self.subTest(decision=choice):
                raw = decision(choice, **({"affected": ["draft"]} if choice == "revise_artifact" else {}))
                result = DeliveryService._verdict(raw, context)
                self.assertEqual([row["id"] for row in result["requirements"]],
                                 [source["id"] for source in context["manifest"]["sources"]])
                self.assertEqual([row["id"] for row in result["checks"]], ["integration", "compatibility"])
                self.assertTrue(all(row["status"] == status for row in result["requirements"] + result["checks"]))
                self.assertTrue(all("quote" not in row and "evidence" not in row
                                    for row in result["requirements"] + result["checks"]))

    def test_real_anthropic_adapter_accepts_minimal_json_once_without_quotation_rows(self):
        rt, task, _ = self.fixture.runtime()
        response = {"type": "message", "stop_reason": "end_turn",
                    "content": [{"type": "text", "text": json.dumps(decision(), ensure_ascii=False)}],
                    "usage": {"input_tokens": 7, "output_tokens": 3}}
        with patch("cater.anthropic._open_api_request", return_value=io.BytesIO(json.dumps(response).encode())) as opened:
            result = rt.delivery.run(task)
        opened.assert_called_once()
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["review_protocol"], "decision_v1")
        request = json.loads(opened.call_args.args[0].data)
        self.assertNotIn("tools", request)
        self.assertEqual(result["handoff"], decision()["handoff"])
        self.assertTrue(rt.verify_chain().ok)

    def test_whole_check_changed_during_review_cannot_be_accepted(self):
        task = self.rt.create_task("Deliver with current whole-task checks")
        self.fixture.prepare(self.rt, task, local=True)
        self.driver.after = lambda _: self.fixture.check(self.rt, task)
        result = self.rt.delivery.run(task)
        self.assertNotEqual(result["status"], "accepted")
        self.assertNotEqual(self.rt.task(task).status, "done")
        self.assertEqual(len(self.driver.calls), 1)


if __name__ == "__main__":
    unittest.main()
