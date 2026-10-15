from __future__ import annotations

import copy
import io
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from cater.anthropic import AnthropicMessagesDriver
from cater.artifacts import normalize_deliverables
from cater.drivers import CliDriver, DriverResponse, OpenAICompatibleDriver
from cater.events import Actor
from cater.runtime import Runtime
from cater.work import WorkConflict
from test_work import WorkDriver, item


QUESTION = "Which accepted input specifies the interaction boundary?"
REPLY = "Use the accepted keyboard interaction boundary."
INPUT = "Keep keyboard focus on the primary input."


class RequestingOwner(WorkDriver):
    def __init__(self):
        super().__init__()
        self.proposal = {"recipient": "design", "question": QUESTION}
        self.mixed = None
        self.repeat = False
        self.enabled = True

    def complete(self, *, role, prompt, context):
        work = context["work"]
        if (self.enabled and work.get("work_item_id") == "draft" and work.get("phase") != "review"
                and ("consultation" not in work or self.repeat)):
            self.calls.append(copy.deepcopy(work))
            payload = {"claim": "Need a bounded consultation", "text": QUESTION, "stance": "assert",
                       "option_id": "A", "collaboration_request": self.proposal}
            if self.mixed == "deliverables":
                payload["deliverables"] = [{"title": "Must not publish", "summary": "Invalid mixed response",
                    "content": "UNAUTHORIZED_ARTIFACT", "format": "text", "filename": "invalid.txt"}]
            elif self.mixed == "clarification":
                payload["clarification"] = {"question": "Which scope should I use?", "options": []}
            return DriverResponse(json.dumps(payload), 7, {"token_usage": "reported"})
        return super().complete(role=role, prompt=prompt, context=context)


class CollaborationRequestTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.db = Path(temporary.name) / "consultation.db"
        self.owner, self.reviewer, self.unrelated = RequestingOwner(), WorkDriver(), WorkDriver()
        self.api = self.api_driver(AnthropicMessagesDriver)
        self.rt = Runtime(self.db, drivers={"dev": self.owner, "test": self.reviewer,
                                         "design": self.api, "product": self.unrelated})
        self.addCleanup(self.rt.close)
        self.task = self.rt.create_task("Implement the accepted interaction boundary")
        self.rt.plan_work(self.task, [item("input", owner="product"), item("draft", deps=["input"])])
        self.input_seq = self.accept_input(self.task, "input", INPUT)
        self.reply = {"status": "answered", "reply": REPLY,
                      "evidence": [{"artifact_seq": self.input_seq, "quote": INPUT}]}
        self.requests = []
        self.transport_hook = None
        self.enterContext(patch.dict(os.environ, {"CATER_CONSULTATION_TEST_KEY": "fixture-only"}))
        self.anthropic = self.enterContext(patch("cater.anthropic._open_api_request", side_effect=self.transport))
        self.openai = self.enterContext(patch("cater.drivers._open_api_request", side_effect=self.transport))

    def api_driver(self, driver_type, **fields):
        return driver_type("design", endpoint="https://provider.test/fixture", model="fixture",
                           api_key_env="CATER_CONSULTATION_TEST_KEY", **fields)

    def transport(self, request, **options):
        body = json.loads(request.data)
        self.requests.append(body)
        if self.transport_hook:
            self.transport_hook()
        text = json.dumps(self.reply)
        if "system" in body:
            data = {"type": "message", "content": [{"type": "text", "text": text}],
                    "stop_reason": "end_turn", "usage": {"input_tokens": 20, "output_tokens": 5}}
        else:
            data = {"choices": [{"message": {"content": text}, "finish_reason": "stop"}],
                    "usage": {"total_tokens": 25}}
        return io.BytesIO(json.dumps(data).encode())

    def accept_input(self, task_id, item_id, content):
        claim = self.rt.work.claim(task_id, item_id)
        artifact = normalize_deliverables([{"title": item_id, "summary": "Accepted input",
                    "content": content, "format": "text", "filename": f"{item_id}.txt"}])[0]
        published = self.rt.log.emit(type="deliverable.published", task_id=task_id,
            actor=Actor.agent("product", "worker"), payload={**artifact, "work_item_id": item_id,
                "attempt_id": claim.payload["attempt_id"]})
        submission = self.rt.work.submit(task_id, claim, [published.seq])
        self.rt.work.review(task_id, item_id, reviewer="test", submission_seq=submission.seq,
            results=[{"id": "meaning", "status": "passed", "reason": "Read the accepted input",
                      "artifact_seq": published.seq, "quote": content, "receipt_seq": None}])
        return published.seq

    def events(self, kind):
        return list(self.rt.log.iter_events(task_id=self.task, event_types=(kind,)))

    def draft(self):
        return next(row for row in self.rt.work.view(self.task)["items"] if row["id"] == "draft")

    def publications(self):
        return [event for event in self.events("deliverable.published") if event.payload.get("work_item_id") == "draft"]

    def claim_context(self):
        claim = self.rt.work.claim(self.task, "draft")
        view = self.rt.work.view(self.task)
        accepted = next(row for row in view["items"] if row["id"] == "input")
        context = {**claim.payload, "contract": self.draft(),
                   "accepted_inputs": {"input": self.rt.work.artifacts(self.task, accepted["submission"])},
                   "customer_directions": view["customer_directions"]}
        message = self.rt.log.emit(type="msg.said", task_id=self.task, actor=Actor.agent("dev", "worker"),
            payload={"work_item_id": "draft", "attempt_id": claim.payload["attempt_id"],
                     "plan_seq": claim.payload["plan_seq"], "text": QUESTION,
                     "phase": "collaboration_request",
                     "collaboration_request": self.owner.proposal})
        return claim, message.seq, context

    def consult(self, claim, message_seq, context, proposal=None):
        from cater.collaboration import ConsultationService
        return ConsultationService(self.rt).run(self.task, claim,
            self.owner.proposal if proposal is None else proposal, message_seq, context)

    def reject_consult(self, claim, message_seq, context, proposal=None):
        try:
            self.consult(claim, message_seq, context, proposal)
        except (ValueError, WorkConflict):
            pass
        self.assertEqual(self.requests, [])
        self.assertEqual(self.publications(), [])
        self.assertNotEqual(self.draft()["status"], "accepted")

    def test_targeted_reply_returns_to_original_owner_and_is_submitted_once(self):
        scope = self.rt.work.scope(self.task)
        plan = self.rt.work.view(self.task)["plan_seq"]
        self.rt.collaborate(self.task)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.unrelated.calls, [])
        self.assertEqual(len(self.owner.calls), 2)
        first, resumed = self.owner.calls
        self.assertNotIn("consultation", first)
        self.assertIn(REPLY, json.dumps(resumed["consultation"]))
        for key in ("work_item_id", "attempt_id", "plan_seq", "scope"):
            self.assertEqual(first[key], resumed[key])
        self.assertEqual(self.draft()["status"], "accepted")
        self.assertEqual(len(self.publications()), 1)
        self.assertEqual(len([e for e in self.events("work.submitted") if e.payload["work_item_id"] == "draft"]), 1)
        self.assertEqual(len(self.events("collaboration.requested")), 1)
        finished = self.events("collaboration.finished")[-1]
        self.assertEqual(finished.actor, Actor.system("collaboration"))
        self.assertEqual(finished.payload["status"], "answered")
        self.assertEqual(self.rt.work.scope(self.task), scope)
        self.assertEqual(self.rt.work.view(self.task)["plan_seq"], plan)
        self.assertEqual(self.rt._reported_driver_tokens("design"), 25)
        self.assertEqual(self.rt.task(self.task).tokens, 46)
        self.assertEqual(finished.payload["tokens"], 25)
        self.assertEqual(finished.payload["requester"], "dev")
        self.assertEqual(finished.payload["recipient"], "design")
        requested = self.events("collaboration.requested")[0]
        self.assertEqual(finished.payload["request_seq"], requested.seq)
        for key in ("work_item_id", "attempt_id", "plan_seq", "scope"):
            self.assertEqual(requested.payload[key], first[key])
            self.assertEqual(finished.payload[key], first[key])
        for body in self.requests:
            self.assertNotIn("tools", body)
            self.assertIn("consultation", json.dumps(body))
        calls = len(self.owner.calls)
        self.rt.collaborate(self.task)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(len(self.owner.calls), calls)
        self.assertEqual(len(self.publications()), 1)

    def test_ordinary_work_has_no_helper_call(self):
        self.owner.enabled = False
        self.rt.collaborate(self.task)
        self.assertEqual(self.draft()["status"], "accepted")
        self.assertEqual(self.requests, [])
        self.assertEqual(self.events("collaboration.requested"), [])
        self.assertEqual(self.events("collaboration.finished"), [])

    def test_invalid_or_mixed_requests_fail_explicitly_without_publishing(self):
        cases = [({"recipient": "dev", "question": QUESTION}, None),
                 ({"recipient": "missing-role", "question": QUESTION}, None),
                 ({"recipient": "design", "question": ""}, None),
                 ({"recipient": "design", "question": "x" * 4001}, None),
                 ({"recipient": "design", "question": QUESTION}, "deliverables"),
                 ({"recipient": "design", "question": QUESTION}, "clarification")]
        for proposal, mixed in cases:
            with self.subTest(recipient=proposal["recipient"], mixed=mixed, length=len(proposal["question"])):
                self.owner.proposal, self.owner.mixed = proposal, mixed
                before = len(self.events("work.failed"))
                self.rt.collaborate(self.task)
                self.assertGreater(len(self.events("work.failed")), before)
                self.assertEqual(self.publications(), [])
                self.assertEqual(self.requests, [])
                self.assertEqual(self.rt.clarifications.pending(self.task), None)

    def test_second_request_in_one_attempt_fails_instead_of_reinvoking_helper(self):
        self.owner.repeat = True
        self.rt.collaborate(self.task)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(len(self.events("collaboration.requested")), 1)
        self.assertEqual(self.publications(), [])
        self.assertTrue(self.events("work.failed"))
        self.assertFalse(self.draft()["in_flight"])

    def test_declined_reply_is_returned_to_owner_without_becoming_a_deliverable(self):
        self.reply = {"status": "declined", "reply": "The accepted input does not resolve this detail.", "evidence": []}
        self.rt.collaborate(self.task)
        self.assertEqual(len(self.requests), 1)
        resumed = [call for call in self.owner.calls if "consultation" in call]
        self.assertEqual(len(resumed), 1)
        self.assertIn("declined", json.dumps(resumed[0]["consultation"]))
        self.assertEqual(self.events("collaboration.finished")[-1].payload["status"], "declined")
        self.assertEqual(len(self.publications()), 1)
        self.assertNotEqual(self.publications()[0].payload["content"], self.reply["reply"])

    def test_exact_openai_adapter_can_consult_with_original_method(self):
        claim, message_seq, context = self.claim_context()
        self.rt.drivers["design"] = self.api_driver(OpenAICompatibleDriver)
        self.consult(claim, message_seq, context)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.events("collaboration.finished")[-1].payload["status"], "answered")
        self.assertEqual(self.publications(), [])
        self.assertEqual(self.draft()["status"], "running")

    def test_cli_subclass_and_replaced_complete_are_not_trusted(self):
        class DerivedAnthropic(AnthropicMessagesDriver):
            pass
        cli = CliDriver("design", ["this-command-must-never-start"])
        self.addCleanup(cli.close)
        drivers = [cli, self.api_driver(DerivedAnthropic), self.api_driver(AnthropicMessagesDriver)]
        for driver in drivers:
            with self.subTest(driver=type(driver).__name__):
                claim, message_seq, context = self.claim_context()
                self.rt.drivers["design"] = driver
                if isinstance(driver, CliDriver):
                    with patch.object(driver, "_complete", side_effect=AssertionError("CLI helper invoked")) as complete:
                        self.reject_consult(claim, message_seq, context)
                        complete.assert_not_called()
                elif type(driver) is not AnthropicMessagesDriver:
                    self.reject_consult(claim, message_seq, context)
                else:
                    with patch.object(driver, "complete", side_effect=AssertionError("replaced method invoked")) as complete:
                        self.reject_consult(claim, message_seq, context)
                        complete.assert_not_called()
                self.rt.work.fail(self.task, claim, "Close unsupported fixture", uncertain=False)

    def test_self_consultation_is_rejected_without_helper_invocation(self):
        claim, message_seq, context = self.claim_context()
        self.reject_consult(claim, message_seq, context, {"recipient": "dev", "question": QUESTION})

    def test_scope_change_before_consultation_cannot_call_helper(self):
        claim, message_seq, context = self.claim_context()
        self.rt.command(self.task, "New unapproved direction", goal="Different scope")
        self.reject_consult(claim, message_seq, context)

    def test_mismatched_attempt_or_plan_cannot_request_consultation(self):
        claim, message_seq, context = self.claim_context()
        for override in ({"attempt_id": "different-attempt"}, {"plan_seq": claim.payload["plan_seq"] + 1}):
            with self.subTest(override=override):
                invalid_claim = replace(claim, payload={**claim.payload, **override})
                self.reject_consult(invalid_claim, message_seq, context)
        self.assertEqual(self.events("collaboration.requested"), [])

    def test_reentry_while_helper_is_running_cannot_issue_a_second_call(self):
        claim, message_seq, context = self.claim_context()
        reentries = []
        def reenter():
            with self.assertRaises(WorkConflict):
                self.consult(claim, message_seq, context)
            reentries.append(True)
        self.transport_hook = reenter
        self.consult(claim, message_seq, context)
        self.assertEqual(reentries, [True])
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(len(self.events("collaboration.requested")), 1)
        self.assertEqual(len(self.events("collaboration.finished")), 1)
        self.assertEqual(self.events("collaboration.finished")[0].payload["status"], "answered")
        self.assertEqual(self.publications(), [])

    def test_scope_change_during_helper_response_is_stale_and_owner_does_not_resume(self):
        self.transport_hook = lambda: self.rt.command(self.task, "Changed during helper", goal="Different scope")
        self.rt.collaborate(self.task)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(len(self.owner.calls), 1)
        self.assertEqual(self.publications(), [])
        self.assertEqual(self.events("collaboration.finished")[-1].payload["status"], "stale")
        self.assertFalse(self.rt.work.view(self.task)["scope_current"])

    def test_cross_task_and_unmatched_quotes_are_not_accepted_as_evidence(self):
        foreign = self.rt.create_task("Unrelated private task")
        self.rt.plan_work(foreign, [item("foreign", owner="product")])
        foreign_seq = self.accept_input(foreign, "foreign", "PRIVATE_FOREIGN_INPUT")
        claim, message_seq, context = self.claim_context()
        self.reply["evidence"] = [{"artifact_seq": foreign_seq, "quote": "PRIVATE_FOREIGN_INPUT"}]
        self.consult(claim, message_seq, context)
        self.assertNotEqual(self.events("collaboration.finished")[-1].payload["status"], "answered")
        self.assertNotIn("PRIVATE_FOREIGN_INPUT", json.dumps(self.requests))
        self.assertEqual(self.publications(), [])

    def test_invalid_quote_is_not_adopted_as_a_valid_answer(self):
        self.reply["evidence"] = [{"artifact_seq": self.input_seq, "quote": "This text is absent"}]
        self.rt.collaborate(self.task)
        self.assertEqual(len(self.requests), 1)
        result = self.events("collaboration.finished")[-1].payload
        self.assertEqual(result["status"], "invalid")
        self.assertNotIn("evidence", result)
        for call in self.owner.calls:
            if "consultation" in call:
                self.assertEqual(call["consultation"]["status"], "invalid")
                self.assertNotIn(REPLY, json.dumps(call["consultation"]))

    def test_exhausted_helper_budget_does_not_invoke_or_report_fake_usage(self):
        self.api.token_budget = 0
        self.rt.collaborate(self.task)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.rt._reported_driver_tokens("design"), 0)
        self.assertEqual(self.events("collaboration.finished")[-1].payload["status"], "unavailable")
        self.assertEqual(self.events("collaboration.finished")[-1].payload["tokens"], 0)

    def test_requested_survives_crash_and_same_attempt_is_never_replayed(self):
        claim, message_seq, context = self.claim_context()
        def crash():
            raise KeyboardInterrupt("simulated process interruption")
        self.transport_hook = crash
        with self.assertRaises(KeyboardInterrupt):
            self.consult(claim, message_seq, context)
        self.assertEqual(len(self.events("collaboration.requested")), 1)
        self.assertEqual(self.events("collaboration.finished"), [])
        self.transport_hook = None
        with Runtime(self.db, drivers={"dev": self.owner, "test": self.reviewer,
                "design": self.api, "product": self.unrelated}, close_drivers_on_close=False) as restarted:
            from cater.collaboration import ConsultationService
            try:
                ConsultationService(restarted).run(self.task, claim, self.owner.proposal, message_seq, context)
            except (ValueError, WorkConflict):
                pass
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(len(self.events("collaboration.requested")), 1)
        self.assertEqual(self.publications(), [])


if __name__ == "__main__":
    unittest.main()
