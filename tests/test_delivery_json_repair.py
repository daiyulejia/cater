from __future__ import annotations

import copy
import io
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from cater.anthropic import AnthropicMessagesDriver
from cater.delivery import _review_state
from cater.drivers import CliDriver, DriverResponse, OpenAICompatibleDriver
from test_delivery import DeliveryDriver
import test_delivery_review_recovery as recovery_fixtures


class DeliveryJsonRepairTests(unittest.TestCase):
    def setUp(self):
        self.fixture = recovery_fixtures.DeliveryReviewRecoveryTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()

    def raw(self, rt, task):
        body = json.loads(self.fixture.response(rt, task).getvalue())
        return json.loads(body["content"][0]["text"])

    @staticmethod
    def wire(text, cls=AnthropicMessagesDriver):
        body = ({"type": "message", "content": [{"type": "text", "text": text}], "stop_reason": "end_turn",
                 "usage": {"input_tokens": 7, "output_tokens": 3}} if cls is AnthropicMessagesDriver else
                {"choices": [{"message": {"content": text}, "finish_reason": "stop"}],
                 "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}})
        return io.BytesIO(json.dumps(body).encode())

    @staticmethod
    def malformed(raw):
        return json.dumps(raw, ensure_ascii=False).replace(', "checks":', ' "checks":', 1)

    def finish(self, rt, task):
        return self.fixture.events(rt, task, "delivery.review_finished")[-1]

    def receipts(self, rt, task):
        return [e for e in self.fixture.events(rt, task, "msg.said") if e.payload.get("phase") == "delivery_receipt"]

    def assert_terminal(self, rt, task, status):
        self.assertEqual(self.finish(rt, task).payload["status"], status)
        self.assertEqual(_review_state(rt.log.read(task_id=task))[2], [])

    def test_malformed_then_valid_uses_same_claim_inputs_and_accounts_both_calls(self):
        for cls in (AnthropicMessagesDriver, OpenAICompatibleDriver):
            for syntax in ("delimiter", "closing_brace", "fenced"):
                with self.subTest(adapter=cls.__name__, syntax=syntax):
                    rt, task, _ = self.fixture.runtime(cls)
                    raw = self.raw(rt, task)
                    valid = json.dumps(raw, ensure_ascii=False)
                    bad = valid[:-1] if syntax == "closing_brace" else self.malformed(raw)
                    if syntax == "fenced":
                        bad = "```json\n" + bad + "\n```"
                    artifacts = self.fixture.events(rt, task, "deliverable.published")
                    with patch(self.fixture.transport(cls), side_effect=[self.wire(bad, cls), self.wire(valid, cls)]) as opened:
                        with patch.object(rt, "_complete_with_containment", wraps=rt._complete_with_containment) as gateway:
                            self.assertEqual(rt.delivery.run(task)["status"], "accepted")
                        self.assertEqual(opened.call_count, 2)
                    calls = gateway.call_args_list
                    self.assertEqual(calls[0].kwargs["work_context"], calls[1].kwargs["work_context"])
                    self.assertEqual(calls[0].kwargs["rule_snapshot"], calls[1].kwargs["rule_snapshot"])
                    self.assertIn("Repair ONLY JSON syntax", calls[1].kwargs["prompt"])
                    self.assertIn("negative or unknown judgments", calls[1].kwargs["prompt"])
                    self.assertEqual(len(self.fixture.events(rt, task, "delivery.review_started")), 1)
                    self.assertEqual(len(self.fixture.events(rt, task, "delivery.review_finished")), 1)
                    self.assertEqual(self.fixture.events(rt, task, "deliverable.published"), artifacts)
                    rows = self.receipts(rt, task)
                    self.assertEqual([e.payload["correction_index"] for e in rows], [0, 1])
                    self.assertEqual([e.payload["tokens"] for e in rows], [10, 10])
                    self.assertEqual(len({e.payload["attempt_seq"] for e in rows}), 1)
                    self.assertEqual(rt._reported_driver_tokens("final"), 20)
                    self.assert_terminal(rt, task, "accepted")

    def test_two_malformed_responses_stop_without_third_call_or_automatic_replay(self):
        rt, task, _ = self.fixture.runtime()
        bad = self.malformed(self.raw(rt, task))
        with patch(self.fixture.transport(AnthropicMessagesDriver), side_effect=[self.wire(bad), self.wire(bad)]) as opened:
            self.assertEqual(rt.delivery.run(task)["status"], "invalid")
            self.assertEqual(rt.delivery.run(task)["status"], "invalid")
            self.assertEqual(opened.call_count, 2)
        self.assert_terminal(rt, task, "invalid")
        self.assertEqual(rt._reported_driver_tokens("final"), 20)

    def test_http_and_transport_failures_are_not_json_repair_candidates(self):
        for correction in (False, True):
            for failure in ("429", "timeout"):
                with self.subTest(correction=correction, failure=failure):
                    rt, task, _ = self.fixture.runtime()
                    error = (HTTPError("https://fixture.invalid", 429, "fixture", {}, io.BytesIO())
                             if failure == "429" else TimeoutError("fixture"))
                    responses = ([self.wire(self.malformed(self.raw(rt, task)))] if correction else []) + [error]
                    with patch(self.fixture.transport(AnthropicMessagesDriver), side_effect=responses) as opened:
                        self.assertEqual(rt.delivery.run(task)["status"], "interrupted")
                        self.assertEqual(opened.call_count, 2 if correction else 1)
                    self.assert_terminal(rt, task, "interrupted")
                    self.assertEqual(rt._reported_driver_tokens("final"), 10 if correction else 0)
                    self.assertEqual(self.receipts(rt, task)[-1].payload["token_usage"], "unavailable")

    def test_cli_custom_and_replaced_api_methods_cannot_opt_in_to_repair(self):
        drivers = [CliDriver("final", ["never-executed"]), DeliveryDriver(),
                   AnthropicMessagesDriver("final", endpoint="https://fixture.invalid", model="fixture",
                                           api_key_env="CATER_DELIVERY_FIXTURE")]
        for driver in drivers:
            rt, task, _ = self.fixture.runtime(driver=driver)
            driver.review_response_repair = True
            with patch.object(driver, "complete", return_value=DriverResponse(self.malformed(self.raw(rt, task)))) as called:
                self.assertEqual(rt.delivery.run(task)["status"], "invalid")
                called.assert_called_once()
            self.assert_terminal(rt, task, "invalid")

    def test_unknown_cli_failure_remains_uncertain_not_blocked(self):
        rt, task, driver = self.fixture.runtime(driver=CliDriver("final", ["never-executed"]))
        driver.review_response_repair = True
        with patch.object(driver, "_request", side_effect=TimeoutError("lost CLI result")) as called:
            self.assertEqual(rt.delivery.run(task)["status"], "uncertain")
            called.assert_called_once()
        self.assertEqual(self.finish(rt, task).payload["status"], "uncertain")
        self.assertEqual(len(_review_state(rt.log.read(task_id=task))[2]), 1)

    def test_disabled_switch_does_not_repair(self):
        rt, task, driver = self.fixture.runtime()
        driver.review_response_repair = False
        with patch(self.fixture.transport(AnthropicMessagesDriver), return_value=self.wire(self.malformed(self.raw(rt, task)))) as opened:
            self.assertEqual(rt.delivery.run(task)["status"], "invalid")
            opened.assert_called_once()
        self.assert_terminal(rt, task, "invalid")

    def test_valid_json_evidence_schema_and_semantic_failures_are_not_rewritten(self):
        for failure in ("quote", "schema", "measurement", "failed", "unknown", "duplicate_key"):
            with self.subTest(failure=failure):
                rt, task, _ = self.fixture.runtime()
                raw = self.raw(rt, task)
                row = raw["requirements"][0]
                expected = "invalid"
                if failure == "quote":
                    row["evidence"][0]["quote"] = "Invented evidence"
                elif failure == "schema":
                    del raw["handoff"]
                elif failure == "measurement":
                    row["measurements"] = [{"observed": 0}]
                elif failure in {"failed", "unknown"}:
                    row["status"] = failure
                    expected = "rework" if failure == "failed" else "needs_evidence"
                text = json.dumps(raw)
                if failure == "duplicate_key":
                    text = text.replace('"requirements":', '"requirements": [], "requirements":', 1)
                with patch(self.fixture.transport(AnthropicMessagesDriver), return_value=self.wire(text)) as opened:
                    rt.delivery.run(task)
                    opened.assert_called_once()
                self.assert_terminal(rt, task, expected)

    def test_syntax_correction_preserves_negative_and_unknown_judgments(self):
        for status in ("failed", "unknown"):
            rt, task, _ = self.fixture.runtime()
            raw = self.raw(rt, task)
            raw["requirements"][0].update(status=status, reason="Original negative assessment")
            with patch(self.fixture.transport(AnthropicMessagesDriver), side_effect=[
                    self.wire(self.malformed(raw)), self.wire(json.dumps(raw))]) as opened:
                rt.delivery.run(task)
                self.assertEqual(opened.call_count, 2)
            self.assert_terminal(rt, task, "rework" if status == "failed" else "needs_evidence")
            self.assertEqual(self.finish(rt, task).payload["requirements"][0]["reason"], "Original negative assessment")

    def test_repair_cannot_change_judgment_reason_quote_or_measurement(self):
        for field in ("status", "reason", "quote", "limit", "structure"):
            with self.subTest(field=field):
                rt, task, _ = self.fixture.runtime()
                raw = self.raw(rt, task)
                row = raw["requirements"][0]
                row.update(status="failed", reason="Original negative assessment", measurements=[{
                    "artifact_seq": row["evidence"][0]["artifact_seq"], "metric": "codepoints", "operator": "le",
                    "limit": 0, "source_quote": rt.delivery.view(task)["manifest"]["sources"][0]["text"]}])
                corrected = copy.deepcopy(raw)
                new = corrected["requirements"][0]
                if field == "status":
                    new["status"] = "passed"
                elif field == "reason":
                    new["reason"] = "Rewritten opinion"
                elif field == "quote":
                    new["evidence"][0]["quote"] = "Report"
                elif field == "limit":
                    new["measurements"][0]["limit"] = 1000
                else:
                    corrected["requirements"] = {"rows": corrected["requirements"]}
                with patch(self.fixture.transport(AnthropicMessagesDriver), side_effect=[
                        self.wire(self.malformed(raw)), self.wire(json.dumps(corrected))]):
                    self.assertEqual(rt.delivery.run(task)["status"], "invalid")
                self.assert_terminal(rt, task, "invalid")
                self.assertTrue(rt.work.view(task)["stage_accepted"])

    def test_repaired_false_measurement_still_fails_host_comparison(self):
        rt, task, _ = self.fixture.runtime()
        raw = self.raw(rt, task)
        row = raw["requirements"][0]
        row["measurements"] = [{"artifact_seq": row["evidence"][0]["artifact_seq"], "metric": "codepoints",
            "operator": "le", "limit": 0, "source_quote": rt.delivery.view(task)["manifest"]["sources"][0]["text"]}]
        with patch(self.fixture.transport(AnthropicMessagesDriver), side_effect=[
                self.wire(self.malformed(raw)), self.wire(json.dumps(raw))]):
            rt.delivery.run(task)
        self.assert_terminal(rt, task, "rework")
        self.assertEqual(self.finish(rt, task).payload["requirements"][0]["measurements"][0]["status"], "failed")

    def test_pause_cancel_or_stale_after_first_response_closes_claim_without_second_call(self):
        for action in ("pause", "cancel", "stale"):
            with self.subTest(action=action):
                rt, task, _ = self.fixture.runtime()
                bad = self.malformed(self.raw(rt, task))

                def returning(*args, **kwargs):
                    if action == "stale":
                        rt.command(task, "Change the requirements")
                    else:
                        rt.execution_control.change(task, action, request_id="json-repair-control")
                    return self.wire(bad)

                with patch(self.fixture.transport(AnthropicMessagesDriver), side_effect=returning) as opened:
                    rt.delivery.run(task)
                    opened.assert_called_once()
                self.assert_terminal(rt, task, "stale" if action == "stale" else "blocked")
                self.assertEqual(len(self.receipts(rt, task)), 1)
                self.assertEqual(rt._reported_driver_tokens("final"), 10)

    def test_gateway_blocks_pause_or_scope_change_after_repair_preflight(self):
        for action in ("pause", "stale"):
            rt, task, _ = self.fixture.runtime()
            original, calls = rt._complete_with_containment, 0

            def gateway(**kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    if action == "pause":
                        rt.execution_control.change(task, "pause", request_id="json-repair-control")
                    else:
                        rt.command(task, "Change the requirements")
                return original(**kwargs)

            with patch.object(rt, "_complete_with_containment", side_effect=gateway):
                with patch(self.fixture.transport(AnthropicMessagesDriver), return_value=self.wire(self.malformed(self.raw(rt, task)))) as opened:
                    rt.delivery.run(task)
                    opened.assert_called_once()
            self.assert_terminal(rt, task, "blocked" if action == "pause" else "stale")
            self.assertEqual([e.payload["correction_index"] for e in self.receipts(rt, task)], [0, 1])
            self.assertEqual(rt._reported_driver_tokens("final"), 10)

    def test_pause_stale_or_recovery_during_second_response_cannot_accept(self):
        for action in ("pause", "stale", "recover"):
            rt, task, _ = self.fixture.runtime()
            raw, calls = self.raw(rt, task), 0

            def returning(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 1:
                    return self.wire(self.malformed(raw))
                if action == "pause":
                    rt.execution_control.change(task, "pause", request_id="json-repair-control")
                elif action == "stale":
                    rt.command(task, "Change the requirements")
                else:
                    self.fixture.now += 2
                    rt.delivery.recover(task)
                return self.wire(json.dumps(raw))

            with patch(self.fixture.transport(AnthropicMessagesDriver), side_effect=returning) as opened:
                rt.delivery.run(task, lease_seconds=1)
                self.assertEqual(opened.call_count, 2)
            self.assert_terminal(rt, task, {"pause": "blocked", "stale": "stale", "recover": "interrupted"}[action])
            self.assertEqual(rt._reported_driver_tokens("final"), 20)
            self.assertEqual(len(self.fixture.events(rt, task, "delivery.review_finished")), 1)

    def test_expired_lease_or_changed_contract_does_not_continue_correction(self):
        for action in ("expired", "contract"):
            rt, task, driver = self.fixture.runtime()
            raw = self.raw(rt, task)

            def returning(*args, **kwargs):
                if action == "expired":
                    self.fixture.now += 2
                else:
                    driver.complete = lambda **kw: self.fail("changed driver must not be called")
                return self.wire(self.malformed(raw))

            with patch(self.fixture.transport(AnthropicMessagesDriver), side_effect=returning) as opened:
                rt.delivery.run(task, lease_seconds=1)
                opened.assert_called_once()
            self.assert_terminal(rt, task, "interrupted" if action == "expired" else "invalid")

    def test_overlapping_run_cannot_claim_or_spend_an_extra_correction(self):
        rt, task, _ = self.fixture.runtime()
        raw, calls = self.raw(rt, task), 0

        def returning(*args, **kwargs):
            nonlocal calls
            calls += 1
            self.assertEqual(rt.delivery.run(task)["status"], "reviewing")
            return self.wire(self.malformed(raw) if calls == 1 else json.dumps(raw))

        with patch(self.fixture.transport(AnthropicMessagesDriver), side_effect=returning) as opened:
            self.assertEqual(rt.delivery.run(task)["status"], "accepted")
            self.assertEqual(opened.call_count, 2)
        self.assertEqual(len(self.fixture.events(rt, task, "delivery.review_started")), 1)

    def test_response_and_correction_prompt_limits_do_not_truncate_or_loop(self):
        for oversized in ("first_response", "repair_prompt", "second_response"):
            rt, task, _ = self.fixture.runtime()
            raw = self.raw(rt, task)
            if oversized == "repair_prompt":
                raw["handoff"]["why"] = "x" * 132000
            bad = "x" * 262145 if oversized == "first_response" else self.malformed(raw)
            responses = [self.wire(bad)]
            if oversized == "second_response":
                responses.append(self.wire("x" * 262145))
            with patch(self.fixture.transport(AnthropicMessagesDriver), side_effect=responses) as opened:
                self.assertEqual(rt.delivery.run(task)["status"], "invalid")
                self.assertEqual(opened.call_count, len(responses))
            self.assert_terminal(rt, task, "invalid")

    def test_ambiguous_string_syntax_cannot_be_repaired_by_rewriting_content(self):
        rt, task, _ = self.fixture.runtime()
        with patch(self.fixture.transport(AnthropicMessagesDriver), return_value=self.wire('{"requirements": "unterminated')) as opened:
            self.assertEqual(rt.delivery.run(task)["status"], "invalid")
            opened.assert_called_once()
        self.assert_terminal(rt, task, "invalid")


if __name__ == "__main__":
    unittest.main()
