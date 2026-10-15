from __future__ import annotations

import copy
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from cater.anthropic import AnthropicMessagesDriver
from cater.events import Actor
from cater.runtime import Runtime
from cater.review_contract import receipt_context
import test_browser_checks as browser_helpers
from test_work import item


class BrowserRepairPipelineTests(unittest.TestCase):
    # Reuse fixture operations, without inheriting/discovering its test methods.
    submit = browser_helpers.BrowserCheckTests.submit
    current = browser_helpers.BrowserCheckTests.current
    events = browser_helpers.BrowserCheckTests.events

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = self.root / "checker.json"
        self.command = [sys.executable, str(self.root / "mock-browser-runner.py")]
        self.config.write_text(json.dumps({"command": self.command, "timeout": 12}), encoding="utf-8")
        api = AnthropicMessagesDriver("fixture", endpoint="https://provider.test/fixture", model="fixture",
                                      api_key_env="CATER_BROWSER_PIPELINE_TEST_KEY")
        self.rt = Runtime(self.root / "pipeline.db", browser_checker_path=self.config,
                          drivers={name: api for name in ("dev", "test", "design", "product")})
        self.addCleanup(self.rt.close)
        self.task = self.rt.create_task("Independently check the page before using its accepted input")
        page = item("page", local=True)
        page["criteria"][-1]["text"] = "Click #save, then verify that #result reads Ready."
        self.rt.plan_work(self.task, [page, item("downstream", owner="design", deps=["page"])])
        self.calls, self.executions, self.trace, self.replies = [], [], [], []
        self.first_status = "unknown"
        self.second_status = "passed"
        self.request_repair = True
        self.stale_review = False
        self.identical_plan = False
        self.repair_outcome = "passed"
        self.initial_outcome = "failed"
        self.initial_phase = "execution"
        self.hook = None
        self.snapshots = []
        self.transport_faults = []
        self.enterContext(patch.dict(os.environ, {"CATER_BROWSER_PIPELINE_TEST_KEY": "fixture-only"}))
        self.transport = self.enterContext(patch("cater.anthropic._open_api_request", side_effect=self.transport_response))
        self.runner = self.enterContext(patch("cater.runtime.run_check", side_effect=self.execute))

    def rows(self):
        return {row["id"]: row for row in self.rt.work.view(self.task)["items"]}

    def calls_for(self, phase):
        return [context for label, context in self.calls if label == phase]

    def page_reviews(self):
        return [event for event in self.events("work.reviewed") if event.payload["work_item_id"] == "page"]

    def repair_starts(self):
        return [event for event in self.events("verification.planning_started")
                if "repair_of_receipt_seq" in event.payload]

    def collaborate(self):
        result = self.rt.collaborate(self.task, expected_scope=self.rt.work.scope(self.task))
        self.assertEqual(self.transport_faults, [], "Fixture errors must not masquerade as contained transport failures")
        return result

    def transport_response(self, request, **kwargs):
        try:
            return self.respond(request, **kwargs)
        except Exception as exc:
            self.transport_faults.append(repr(exc))
            raise

    def respond(self, request, **kwargs):
        self.assertEqual(request.full_url, "https://provider.test/fixture")
        body = json.loads(request.data)
        self.assertNotIn("tools", body)
        prompt = body["messages"][0]["content"]
        decoder = json.JSONDecoder()
        plan_marker = "Current check contract (untrusted reference data):\n"
        author_marker = "Current work contract and accepted inputs: "
        if plan_marker in prompt:
            context, _ = decoder.raw_decode(prompt.split(plan_marker, 1)[1])
            label = "repair_plan" if "test_diagnosis" in context else "plan"
            plan = copy.deepcopy(browser_helpers.PLAN)
            if label == "plan" and self.initial_outcome == "passed":
                plan["steps"] = plan["steps"][1:]
            elif label == "plan" or self.identical_plan:
                plan["steps"][0]["selector"] = "#missing-save"
            reply = {"status": "ready", "reason": "Check the submitted page with the corrected selector.", "plan": plan}
        elif author_marker in prompt:
            context, _ = decoder.raw_decode(prompt.split(author_marker, 1)[1])
            label = "author:" + context["work_item_id"]
            reply = {"stance": "assert", "claim": "Submitted exact body", "text": "Submitted exact body"}
            if label != "author:page" or not self.calls_for(label):
                page = context["work_item_id"] == "page"
                reply["deliverables"] = [{"title": context["work_item_id"], "summary": "Fixture output",
                    "content": browser_helpers.HTML if page else "Used the independently accepted page.",
                    "format": "code" if page else "text", "filename": "index.html" if page else "notes.txt"}]
        else:
            self.assertIn('"phase": "review"', prompt)
            context, _ = decoder.raw_decode(prompt[prompt.index('\n{"goal":') + 1:])
            label = "review:" + context["work_item_id"]
            reply = self.review_reply(context)
        self.calls.append((label, copy.deepcopy(context)))
        self.replies.append((label, copy.deepcopy(reply)))
        self.trace.append(label)
        self.snapshots.append((label, copy.deepcopy(self.rows())))
        if self.hook:
            self.hook(label, context)
        response = {"type": "message", "content": [{"type": "text", "text": json.dumps(reply)}],
                    "stop_reason": "end_turn", "usage": {"input_tokens": 20, "output_tokens": 5}}
        return io.BytesIO(json.dumps(response).encode())

    def review_reply(self, context):
        repaired = len(self.executions) > 1
        if not self.stale_review:
            if context["work_item_id"] != "page":
                return {"decision": "needs_information", "reason": "The downstream summary needs independent confirmation."}
            status = self.second_status if repaired and self.repair_outcome == "passed" else self.first_status
            decision = ("approve" if status == "passed" else "revise_artifact" if status == "failed" else
                        "revise_check" if self.request_repair and (not repaired or self.repair_outcome == "failed")
                        else "needs_information")
            reason = {
                "approve": "The current browser plan exercised Save and observed the required Ready state.",
                "revise_artifact": "The observed behavior violates the criterion; the source needs correction.",
                "revise_check": ("The smoke test omitted clicking Save; add that interaction before checking Ready."
                                 if self.initial_outcome == "passed" else
                                 "The generated #missing-save selector is absent; use #save on the unchanged source."),
                "needs_information": "The current execution does not resolve the remaining behavior question.",
            }[decision]
            return {"decision": decision, "reason": reason}

        # Keep one legacy citation regression: an old receipt cannot accept a new check.
        artifact = context["artifacts"][0]
        # Hold final delivery at the downstream review, keeping this fixture about scheduling.
        rows = [{"id": "meaning", "status": "passed" if context["work_item_id"] == "page" else "unknown",
                 "reason": "Read the exact submitted source.", "artifact_seq": artifact["seq"],
                 "quote": artifact["content"], "receipt_seq": None}]
        if context["work_item_id"] != "page":
            return {"checks": rows}
        receipt = context["local_checks"]["behavior"]
        if repaired and self.stale_review:
            receipt = self.calls_for("review:page")[0]["local_checks"]["behavior"]
        status = self.second_status if repaired and self.repair_outcome == "passed" else self.first_status
        row = {"id": "behavior", "status": status,
               "reason": "Correct the generated selector; this failure does not establish a source defect.",
               "artifact_seq": None, "quote": receipt["observed_quotes"][0],
               "receipt_seq": receipt["seq"]}
        if status == "failed":
            row["reason"] = "The observed behavior violates the criterion; the source needs correction."
        if self.request_repair and (not repaired or self.repair_outcome == "failed"):
            row["next_action"] = "revise_check"
        rows.append(row)
        return {"checks": rows}

    def execute(self, command, *, cwd, timeout, record_output):
        outcome = self.initial_outcome if not self.executions else self.repair_outcome
        self.browser_report = None
        self.exit_code = 0 if outcome == "passed" else 1
        if outcome != "passed":
            self.browser_report = {"outcome": outcome, "phase": self.initial_phase, "steps": [
                {"index": 1, "action": "click", "status": "failed", "error": "Wrong test selector"}]}
            if outcome == "unavailable":
                self.exit_code = 2
        self.trace.append("browser:" + outcome)
        return browser_helpers.BrowserCheckTests.execute(
            self, command, cwd=cwd, timeout=timeout, record_output=record_output)

    def assert_no_downstream(self):
        self.assertEqual(self.calls_for("author:downstream"), [])
        self.assertFalse(any(e.payload["work_item_id"] == "downstream" for e in self.events("work.claimed")))
        self.assertFalse(self.rt.work.view(self.task)["stage_accepted"])

    def test_one_collaborate_repairs_same_source_reviews_again_and_starts_successor(self):
        self.collaborate()
        self.assertEqual(self.trace, ["author:page", "plan", "browser:failed", "review:page", "repair_plan",
                                      "browser:passed", "review:page", "author:downstream", "review:downstream"])
        self.assertEqual(len(self.calls_for("author:page")), 1)
        self.assertEqual(len(self.executions), 2)
        original, corrected = self.events("verification.completed")
        reviews = self.page_reviews()
        self.assertEqual([r.payload["status"] for r in reviews], ["submitted", "accepted"])
        self.assertEqual([r.payload["review_protocol"] for r in reviews], ["decision_v1", "decision_v1"])
        self.assertEqual([r.payload["decision"] for r in reviews], ["revise_check", "approve"])
        for label, reply in self.replies:
            if label.startswith("review:"):
                self.assertEqual(set(reply), {"decision", "reason"})
        self.assertTrue(all("quote" not in check for review in reviews for check in review.payload["checks"]))
        self.assertEqual([r.actor.id for r in reviews], ["test", "test"])
        self.assertEqual([e.payload["outcome"] for e in (original, corrected)], ["failed", "passed"])
        self.assertEqual([next(c for c in r.payload["checks"] if c["id"] == "behavior")["receipt_seq"]
                          for r in reviews], [original.seq, corrected.seq])
        self.assertFalse(self.rt.work._receipt_passed(original))
        self.assertTrue(self.rt.work._receipt_passed(corrected))
        for key in ("submission_seq", "submission_digest", "plan_seq", "scope", "checked_artifacts"):
            self.assertEqual(original.payload[key], corrected.payload[key], key)
        self.assertEqual([run["files"] for run in self.executions], [{"index.html": browser_helpers.HTML}] * 2)
        self.assertNotEqual(self.executions[0]["plan"], self.executions[1]["plan"])
        self.assertEqual(len(self.events("work.submitted")), 2)
        self.assertEqual(self.events("work.invalidated"), [])
        self.assertEqual(self.events("work.repair_requested"), [])
        repair = self.repair_starts()[0]
        self.assertEqual(repair.payload["repair_of_receipt_seq"], original.seq)
        self.assertEqual(repair.payload["review_seq"], reviews[0].seq)
        diagnosis = self.calls_for("repair_plan")[0]
        self.assertEqual(diagnosis["previous_check"], receipt_context(original))
        self.assertEqual(diagnosis["previous_check"]["seq"], original.seq)
        self.assertEqual(diagnosis["previous_check"]["event_hash"], original.hash)
        self.assertEqual(diagnosis["previous_check"]["browser_report"], original.payload["browser_report"])
        self.assertIn("excerpt", original.payload["stdout"])
        self.assertNotIn("excerpt", diagnosis["previous_check"]["stdout"])
        self.assertEqual(diagnosis["test_diagnosis"]["next_action"], "revise_check")
        self.assertEqual(diagnosis["test_diagnosis"]["status"], "unknown")
        next_claim = next(e for e in self.events("work.claimed") if e.payload["work_item_id"] == "downstream")
        self.assertLess(original.seq, reviews[0].seq)
        self.assertLess(reviews[0].seq, repair.seq)
        self.assertLess(repair.seq, corrected.seq)
        self.assertLess(corrected.seq, reviews[1].seq)
        self.assertLess(reviews[1].seq, next_claim.seq)
        pending = [rows for label, rows in self.snapshots if label == "review:page"][1]
        self.assertEqual(pending["page"]["status"], "submitted")
        self.assertEqual(pending["downstream"]["status"], "blocked")
        accepted_input = self.calls_for("author:downstream")[0]["accepted_inputs"]["page"][0]
        self.assertEqual(accepted_input["seq"], original.payload["checked_artifacts"][0]["seq"])
        self.assertEqual(accepted_input["content"], browser_helpers.HTML)
        self.assertEqual(self.events("human.commanded"), [])
        self.assertTrue(self.rt.verify_chain().ok)

    def test_passed_but_incomplete_smoke_is_extended_and_handed_off_in_one_collaborate(self):
        self.initial_outcome = "passed"
        self.collaborate()
        self.assertEqual(self.trace, ["author:page", "plan", "browser:passed", "review:page", "repair_plan",
                                      "browser:passed", "review:page", "author:downstream", "review:downstream"])
        self.assertEqual(len(self.calls_for("author:page")), 1)
        receipts = self.events("verification.completed")
        self.assertEqual(len(receipts), 2)
        for key in ("submission_seq", "submission_digest", "checked_artifacts"):
            self.assertEqual(receipts[0].payload[key], receipts[1].payload[key])
        self.assertEqual([e.payload["outcome"] for e in receipts], ["passed", "passed"])
        self.assertEqual([step["action"] for step in self.executions[0]["plan"]["steps"]], ["assert_text"])
        self.assertEqual([step["action"] for step in self.executions[1]["plan"]["steps"]], ["click", "assert_text"])
        first, second = self.page_reviews()
        self.assertEqual([first.payload["status"], second.payload["status"]], ["submitted", "accepted"])
        behavior = next(c for c in first.payload["checks"] if c["id"] == "behavior")
        self.assertEqual(behavior["execution_status"], "passed")
        self.assertEqual(behavior["status"], "unknown")
        self.assertEqual(behavior["next_action"], "revise_check")
        before_second_review = [rows for label, rows in self.snapshots if label == "review:page"][1]
        self.assertEqual(before_second_review["page"]["status"], "submitted")
        self.assertEqual(before_second_review["downstream"]["status"], "blocked")
        self.assertEqual(self.repair_starts()[0].payload["repair_of_receipt_seq"], receipts[0].seq)
        self.assertEqual(self.events("work.repair_requested"), [])
        self.assertEqual(self.events("human.commanded"), [])

    def test_existing_submission_is_repaired_without_invoking_its_author(self):
        self.submit()
        original = copy.deepcopy(self.current()["submission"])
        self.collaborate()
        self.assertEqual(self.current()["submission"], original)
        self.assertEqual(self.calls_for("author:page"), [])
        self.assertEqual(len(self.calls_for("author:downstream")), 1)
        self.assertEqual(len(self.page_reviews()), 2)

    def test_unknown_second_review_keeps_downstream_blocked_despite_new_pass(self):
        self.second_status = "unknown"
        self.collaborate()
        self.assertEqual(len(self.executions), 2)
        self.assertEqual(len(self.page_reviews()), 2)
        self.assertEqual(self.current()["status"], "submitted")
        self.assert_no_downstream()

    def test_second_review_cannot_use_the_old_failed_receipt_to_accept_new_pass(self):
        self.stale_review = True
        self.collaborate()
        self.assertEqual(len(self.executions), 2)
        self.assertEqual(len(self.calls_for("review:page")), 3)  # One citation correction, no re-execution.
        behavior = next(c for c in self.current()["checks"] if c["id"] == "behavior")
        self.assertEqual(behavior["status"], "unknown")
        self.assertEqual(behavior["response_issue"], "invalid_receipt")
        self.assertEqual(behavior["receipt_seq"], self.events("verification.completed")[-1].seq)
        self.assert_no_downstream()

    def test_repeated_test_diagnosis_is_bounded_in_one_collaborate(self):
        self.repair_outcome = "failed"
        self.collaborate()
        self.assertEqual(len(self.executions), 2)
        self.assertEqual(len(self.calls_for("repair_plan")), 1)
        self.assertEqual(len(self.page_reviews()), 2)
        self.assertEqual(len(self.calls_for("author:page")), 1)
        self.assert_no_downstream()

    def test_identical_repair_plan_does_not_reexecute_old_failure(self):
        self.identical_plan = True
        self.collaborate()
        self.assertEqual(len(self.executions), 1)
        self.assertEqual(len(self.calls_for("repair_plan")), 1)
        self.assertEqual(len(self.page_reviews()), 1)
        self.assertEqual(self.events("verification.planned")[-1].payload["status"], "invalid")
        self.assert_no_downstream()

    def test_unknown_without_explicit_test_diagnosis_does_not_retest(self):
        self.request_repair = False
        self.collaborate()
        self.assertEqual(len(self.executions), 1)
        self.assertEqual(self.repair_starts(), [])
        self.assertEqual(len(self.calls_for("author:page")), 1)
        self.assert_no_downstream()

    def test_unavailable_browser_receipt_does_not_authorize_test_repair(self):
        self.initial_outcome = "unavailable"
        self.collaborate()
        self.assertEqual(len(self.executions), 1)
        self.assertEqual(self.events("verification.completed")[0].payload["outcome"], "unavailable")
        self.assertEqual(self.repair_starts(), [])
        self.assertEqual(self.calls_for("repair_plan"), [])
        self.assert_no_downstream()

    def test_validation_failure_is_not_a_completed_execution_to_repair(self):
        self.initial_phase = "validation"
        self.collaborate()
        self.assertEqual(len(self.executions), 1)
        self.assertEqual(self.events("verification.completed")[0].payload["outcome"], "unavailable")
        self.assertEqual(self.repair_starts(), [])
        self.assertEqual(self.calls_for("repair_plan"), [])
        self.assert_no_downstream()

    def test_claiming_old_failed_execution_passed_cannot_unlock_downstream(self):
        self.first_status = "passed"
        self.collaborate()
        self.assertEqual(len(self.executions), 1)
        self.assertFalse(self.rt.work._receipt_passed(self.events("verification.completed")[0]))
        self.assertEqual(self.current()["status"], "submitted")
        self.assertEqual(self.repair_starts(), [])
        self.assert_no_downstream()

    def test_artifact_failure_requests_source_revision_not_same_source_retest(self):
        self.first_status = "failed"
        self.collaborate()
        self.assertEqual(self.page_reviews()[0].payload["status"], "rework")
        self.assertEqual(len(self.events("work.repair_requested")), 1)
        self.assertEqual(len(self.calls_for("author:page")), 2)
        self.assertIn("previous_attempt", self.calls_for("author:page")[-1])
        self.assertEqual(len(self.executions), 1)
        self.assertEqual(self.repair_starts(), [])
        self.assert_no_downstream()

    def test_pause_after_diagnosis_starts_no_repair_model_runner_or_successor(self):
        def pause(label, context):
            if label == "review:page":
                self.rt.execution_control.change(self.task, "pause", request_id="pause-after-diagnosis")
        self.hook = pause
        self.collaborate()
        self.assertEqual(self.rt.execution_control.view(self.task)["state"], "paused")
        self.assertEqual(len(self.executions), 1)
        self.assertEqual(self.calls_for("repair_plan"), [])
        self.assert_no_downstream()

    def test_scope_change_after_diagnosis_starts_no_repair_or_successor(self):
        def change(label, context):
            if label == "review:page":
                self.rt.command(self.task, "Replace the page requirement", goal="A different page")
        self.hook = change
        self.collaborate()
        self.assertFalse(self.rt.work.view(self.task)["scope_current"])
        self.assertEqual(len(self.executions), 1)
        self.assertEqual(self.calls_for("repair_plan"), [])
        self.assert_no_downstream()

    def test_replacement_scope_removing_reviewed_item_stops_old_collaborate_cleanly(self):
        finish_review = self.rt.work.finish_review

        def replace_after_release(*args, **kwargs):
            event = finish_review(*args, **kwargs)
            self.rt.command(self.task, "Replace the entire plan", goal="New authorized work scope")
            contract = self.rt.work.check_contract(self.task)
            self.rt.revise_checks(self.task, [{"id": contract["obligations"][0]["id"], "action": "retire"}],
                                  reason="The replacement scope retires the old page check.",
                                  expected_revision=contract["revision"])
            self.rt.plan_work(self.task, [item("replacement")])
            return event

        # A new plan is legal only after the real review reservation is released.
        with patch.object(self.rt.work, "finish_review", side_effect=replace_after_release):
            self.collaborate()
        self.assertEqual(set(self.rows()), {"replacement"})
        self.assertEqual(self.rows()["replacement"]["status"], "ready")
        self.assertEqual(len(self.executions), 1)
        self.assertEqual(self.calls_for("repair_plan"), [])
        self.assertEqual(self.calls_for("author:replacement"), [])
        self.assert_no_downstream()

    def test_pause_during_repair_planning_prevents_reexecution_and_second_review(self):
        def pause(label, context):
            if label == "repair_plan":
                self.rt.execution_control.change(self.task, "pause", request_id="pause-during-repair")
        self.hook = pause
        self.collaborate()
        self.assertEqual(len(self.calls_for("repair_plan")), 1)
        self.assertEqual(len(self.executions), 1)
        self.assertEqual(len(self.calls_for("review:page")), 1)
        self.assert_no_downstream()

    def test_scope_change_during_repair_planning_prevents_reexecution(self):
        def change(label, context):
            if label == "repair_plan":
                self.rt.command(self.task, "Changed during test repair", goal="Different acceptance scope")
        self.hook = change
        self.collaborate()
        self.assertFalse(self.rt.work.view(self.task)["scope_current"])
        self.assertEqual(len(self.executions), 1)
        self.assertEqual(len(self.calls_for("review:page")), 1)
        self.assertEqual(self.events("verification.planned")[-1].payload["status"], "stale")
        self.assert_no_downstream()

    def failed_repair_planning(self, status="unavailable"):
        self.submit()
        self.rt.browser_checks.run(self.task, self.current())
        self.rt._review_work_item(self.rt.task(self.task), self.current())
        self.assertEqual(self.page_reviews()[-1].payload["decision"], "revise_check")

        def response(*args, **kwargs):
            if status == "unavailable":
                raise TimeoutError("Fixture repair planning timeout")
            reply = ("invalid plan" if status == "invalid" else json.dumps(
                {"status": "unsupported", "reason": "Fixture unsupported plan", "plan": None}))
            return io.BytesIO(json.dumps({"type": "message", "stop_reason": "end_turn",
                "content": [{"type": "text", "text": reply}],
                "usage": {"input_tokens": 20, "output_tokens": 5}}).encode())

        with patch("cater.anthropic._open_api_request", side_effect=response):
            self.assertFalse(self.rt.browser_checks.repair(self.task, self.current(), attempted=set()))
        self.assertEqual(self.events("verification.planned")[-1].payload["status"], status)
        self.assertEqual(len(self.repair_starts()), 1)
        self.assertEqual(len(self.executions), 1)
        return self.repair_starts()[0]

    def test_unavailable_repair_planning_resumes_with_current_review_and_same_source(self):
        first = self.failed_repair_planning()
        submission = copy.deepcopy(self.current()["submission"])
        authorization = self.rt.command(self.task, "Continue")
        self.assertEqual(authorization.payload["intent"], "resume")
        self.collaborate()
        starts = self.repair_starts()
        self.assertEqual(len(starts), 2)
        self.assertEqual(starts[-1].payload["retry_of"], first.seq)
        self.assertEqual(starts[-1].payload["authorization_seq"], authorization.seq)
        for key in ("work_item_id", "criterion_id", "plan_seq", "scope", "submission_seq",
                    "submission_digest", "repair_of_receipt_seq"):
            self.assertEqual(starts[-1].payload[key], first.payload[key], key)
        self.assertEqual(starts[-1].payload["review_seq"], self.page_reviews()[-2].seq)
        self.assertNotEqual(starts[-1].payload["review_seq"], first.payload["review_seq"])
        self.assertEqual(self.current()["submission"], submission)
        self.assertEqual(len(self.executions), 2)
        self.assertEqual(len(self.calls_for("author:page")), 0)
        self.assertEqual(self.current()["status"], "accepted")
        self.assertTrue(self.rt.verify_chain().ok)

    def assert_failed_repair_retries_after_resume(self, status):
        first = self.failed_repair_planning(status)
        authorization = self.rt.command(self.task, "Continue")
        self.assertTrue(self.rt.browser_checks.repair(self.task, self.current(), attempted=set()))
        retried = self.repair_starts()[-1]
        self.assertEqual(retried.payload["retry_of"], first.seq)
        self.assertEqual(retried.payload["authorization_seq"], authorization.seq)
        self.assertEqual(len(self.executions), 2)

    def test_invalid_repair_planning_can_retry_after_resume(self):
        self.assert_failed_repair_retries_after_resume("invalid")

    def test_unsupported_repair_planning_can_retry_after_resume(self):
        self.assert_failed_repair_retries_after_resume("unsupported")

    def test_failed_repair_planning_without_new_resume_does_not_retry(self):
        self.rt.command(self.task, "Continue")
        self.failed_repair_planning()
        self.collaborate()
        self.assertFalse(self.rt.browser_checks.repair(self.task, self.current(), attempted=set()))
        self.assertEqual(len(self.repair_starts()), 1)
        self.assertEqual(self.calls_for("repair_plan"), [])
        self.assertEqual(len(self.executions), 1)

    def test_resume_does_not_bypass_same_round_repair_attempt_limit(self):
        self.failed_repair_planning()
        self.rt.command(self.task, "Continue")
        attempted = {"behavior"}
        self.assertFalse(self.rt.browser_checks.repair(self.task, self.current(), attempted=attempted))
        self.assertEqual(len(self.repair_starts()), 1)
        self.assertEqual(len(self.executions), 1)

    def test_failed_retry_needs_another_resume_before_planning_again(self):
        first = self.failed_repair_planning()
        authorization = self.rt.command(self.task, "Continue")
        with patch("cater.anthropic._open_api_request", side_effect=TimeoutError("Fixture retry timeout")) as called:
            self.assertFalse(self.rt.browser_checks.repair(self.task, self.current(), attempted=set()))
            self.assertFalse(self.rt.browser_checks.repair(self.task, self.current(), attempted=set()))
            called.assert_called_once()
        retry = self.repair_starts()[-1]
        self.assertEqual(retry.payload["retry_of"], first.seq)
        self.assertEqual(retry.payload["authorization_seq"], authorization.seq)
        self.assertEqual(len(self.repair_starts()), 2)
        self.assertEqual(len(self.executions), 1)

    def test_repair_planning_retry_keeps_current_review_and_receipt_binding(self):
        self.failed_repair_planning()
        old = self.current()
        self.rt.command(self.task, "Continue")
        self.rt._review_work_item(self.rt.task(self.task), self.current())
        altered = copy.deepcopy(self.current())
        next(c for c in altered["checks"] if c["id"] == "behavior")["receipt_seq"] += 1
        self.assertFalse(self.rt.browser_checks.repair(self.task, old, attempted=set()))
        self.assertFalse(self.rt.browser_checks.repair(self.task, altered, attempted=set()))
        self.assertEqual(len(self.repair_starts()), 1)
        self.assertEqual(len(self.executions), 1)

    def test_repair_planning_retry_rejects_changed_scope(self):
        self.failed_repair_planning()
        old = self.current()
        self.rt.command(self.task, "Replace requirement", goal="Changed acceptance scope")
        self.rt.command(self.task, "Continue")
        self.assertFalse(self.rt.browser_checks.repair(self.task, old, attempted=set()))
        self.assertEqual(len(self.repair_starts()), 1)
        self.assertEqual(len(self.executions), 1)

    def test_executed_repair_cannot_be_replayed_after_unavailable_and_resume(self):
        self.failed_repair_planning()
        old = self.current()
        self.rt.command(self.task, "Continue")
        verify = self.rt.verify_work

        def execute_then_fail(*args, **kwargs):
            verify(*args, **kwargs)
            raise ValueError("Fixture failure after actual execution")

        with patch.object(self.rt, "verify_work", side_effect=execute_then_fail):
            self.assertFalse(self.rt.browser_checks.repair(self.task, old, attempted=set()))
        self.assertEqual(self.events("verification.planned")[-1].payload["status"], "unavailable")
        self.rt.command(self.task, "Continue")
        self.assertFalse(self.rt.browser_checks.repair(self.task, old, attempted=set()))
        self.assertFalse(self.rt.browser_checks.repair(self.task, self.current(), attempted=set()))
        self.assertEqual(len(self.repair_starts()), 2)
        self.assertEqual(len(self.calls_for("repair_plan")), 1)
        self.assertEqual(len(self.executions), 2)

    def test_pending_or_unknown_repair_planning_cannot_retry_after_resume(self):
        first = self.failed_repair_planning()
        pending = self.rt.log.emit(type="verification.planning_started", task_id=self.task,
            actor=Actor.system("local_verifier"), payload={**first.payload, "retry_of": first.seq})
        self.rt.command(self.task, "Continue")
        self.assertFalse(self.rt.browser_checks.repair(self.task, self.current(), attempted=set()))
        self.rt.log.emit(type="verification.planned", task_id=self.task, actor=pending.actor,
            payload={**pending.payload, "request_seq": pending.seq, "status": "unknown"})
        self.rt.command(self.task, "Continue")
        self.assertFalse(self.rt.browser_checks.repair(self.task, self.current(), attempted=set()))
        self.assertEqual(len(self.repair_starts()), 2)
        self.assertEqual(self.calls_for("repair_plan"), [])
        self.assertEqual(len(self.executions), 1)

    def test_pending_and_unknown_check_effects_block_repair_planning_retry(self):
        self.failed_repair_planning()
        old = self.current()
        started = self.rt.work.start_check(self.task, "page", "behavior")
        self.rt.command(self.task, "Continue")
        self.assertTrue(self.current()["check_in_flight"])
        self.assertFalse(self.rt.browser_checks.repair(self.task, old, attempted=set()))
        self.assertFalse(self.rt.browser_checks.repair(self.task, self.current(), attempted=set()))
        with patch("cater.work.time.time", return_value=started.payload["lease_until"] + 1):
            self.assertTrue(self.current()["check_uncertain"])
            self.assertFalse(self.rt.browser_checks.repair(self.task, old, attempted=set()))
            self.assertFalse(self.rt.browser_checks.repair(self.task, self.current(), attempted=set()))
        self.assertEqual(len(self.repair_starts()), 1)
        self.assertEqual(self.calls_for("repair_plan"), [])
        self.assertEqual(len(self.executions), 1)

    def test_pending_and_unknown_review_effects_block_retry_from_old_snapshot(self):
        self.failed_repair_planning()
        old = self.current()
        started = self.rt.work.start_review(self.task, "page", reviewer="test",
            submission_seq=old["submission"]["seq"], expected_review_seq=old["review_seq"],
            expected_review_attempt_seq=old["review_lifecycle"]["attempt_seq"],
            expected_check_seqs={key: event.seq for key, event in self.rt.work.checks(self.task, old).items()})
        self.rt.command(self.task, "Continue")
        self.assertTrue(self.current()["review_in_flight"])
        self.assertFalse(self.rt.browser_checks.repair(self.task, old, attempted=set()))
        self.rt.work.finish_review(self.task, started.seq, status="uncertain", reason="Fixture unknown effects")
        self.rt.command(self.task, "Continue")
        self.assertTrue(self.current()["review_uncertain"])
        self.assertFalse(self.rt.browser_checks.repair(self.task, old, attempted=set()))
        self.assertEqual(len(self.repair_starts()), 1)
        self.assertEqual(self.calls_for("repair_plan"), [])
        self.assertEqual(len(self.executions), 1)

    def test_concurrent_resume_claims_only_one_repair_planning_retry(self):
        self.failed_repair_planning()
        authorization = self.rt.command(self.task, "Continue")
        current = self.current()
        barrier = threading.Barrier(2)
        claim_barrier = threading.Barrier(2)
        loser_finished = threading.Event()
        results, errors, planned = [], [], []
        emit = type(self.rt.log).emit

        def competing_claim(log, **kwargs):
            if kwargs.get("type") == "verification.planning_started":
                claim_barrier.wait(timeout=10)
            return emit(log, **kwargs)

        def plan(service, task_id, started, *args):
            planned.append(started)
            if not loser_finished.wait(timeout=10):
                raise AssertionError("Concurrent claimant did not finish")
            service.rt.log.emit(type="verification.planned", task_id=task_id, actor=started.actor,
                payload={**started.payload, "request_seq": started.seq, "status": "unavailable"})
            return {"status": "unavailable"}

        def run():
            try:
                with Runtime(self.rt.log.path, browser_checker_path=self.config, drivers=self.rt.drivers,
                             close_drivers_on_close=False) as rt:
                    barrier.wait(timeout=10)
                    results.append(rt.browser_checks.repair(self.task, current, attempted=set()))
                    loser_finished.set()
            except Exception as exc:
                errors.append(repr(exc))
                loser_finished.set()

        with patch.object(type(self.rt.browser_checks), "_plan", autospec=True, side_effect=plan), \
                patch.object(type(self.rt.log), "emit", autospec=True, side_effect=competing_claim):
            threads = [threading.Thread(target=run) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=15)
                self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results, [False, False])
        self.assertEqual(len(planned), 1)
        self.assertEqual(planned[0].payload["authorization_seq"], authorization.seq)
        self.assertEqual(len(self.repair_starts()), 2)
        self.assertEqual(len(self.executions), 1)


if __name__ == "__main__":
    unittest.main()
