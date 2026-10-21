"""Bounded offline coverage of the external customer-answer host contract."""

import copy
from dataclasses import replace
import hashlib
import io
import json
import unittest
from unittest.mock import patch
import zipfile

from cater.events import Actor
from cater.team import HOST, LOCAL_EVENT_CONTRACT, LOCAL_EVENT_TOOLS, PAUSED_TOOLS
from cater import team_reply
from cater.work import WorkConflict
import test_team_external as external
import test_team_supervision as native


class ExternalCustomerReplyTests(unittest.TestCase):
    def setUp(self):
        self.f = external.TeamExternalTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.rt, self.task = self.f.rt, self.f.task
        self.manager, self.worker = self.f.manager, self.f.worker

    def events(self, kind):
        return self.f.events(kind)

    def customer_messages(self):
        return [e for e in self.events("msg.said") if e.payload.get("audience") == "customer"]

    def answer(self, run, text="The inspected answer is 42."):
        result = run.call("reply_to_customer", text=text)
        self.fail(f"Successful answer must yield immediately, got {result!r}")

    def assert_no_task_verdict(self):
        for kind in ("team.objective_updated", "team.objective_completed", "team.review_requested",
                     "team.review_recorded", "team.external_run_reconciled"):
            self.assertEqual(self.events(kind), [], kind)

    def helper_context(self, *, source_kind="human", recipient=None, waiting_assignment=False):
        """Persist a host run/start without manufacturing an external invocation."""
        task = self.rt.create_task("Isolated reply authority")
        self.rt.team.enable(task)
        role = self.rt.team._role(recipient or self.manager)
        if source_kind == "human":
            if waiting_assignment:
                assignment = self.rt.team._send(task, self.rt.team._role(self.manager), role.id,
                                               "The original delegated work remains open.", "assignment")
                self.rt.log.emit(type="team.questions_requested", task_id=task, actor=HOST,
                                 payload={"role_id": role.id, "assignment_seq": assignment.seq})
            source = self.rt.team.receive(task, f"@{role.name} Explain the current facts.")
        else:
            sender = self.rt.team._role(self.worker if role.id == self.manager else self.manager)
            source = self.rt.team._send(task, sender, role.id, "Explain to your sender.", source_kind)
        claimed, actual_role, run = self.rt.team._claim(task)
        self.assertEqual((claimed, actual_role), (source, role))
        start = self.rt.log.emit(type="team.tool_started", task_id=task, actor=HOST, payload={
            "run_id": run.payload["run_id"], "role": role.id, "name": "reply_to_customer",
            "call_id": "isolated-reply", "message_seq": source.seq,
            "revision": run.payload["revision"], "arguments": {"text": "A bounded answer."},
            "execution_contract": dict(LOCAL_EVENT_CONTRACT)})
        return task, role, source, run, start

    def execute_helper(self, task, role, start, *, assignment=None, text="A bounded answer."):
        return self.rt.team._execute(task, role, "reply_to_customer", {"text": text},
                                     None, {}, start, assignment)

    def test_direct_human_manager_and_at_worker_answer_once_without_sticky_goal(self):
        original = self.rt.team.view(self.task)
        for recipient in (self.manager, self.worker):
            with self.subTest(recipient=recipient):
                text = f"Answer from {recipient}."

                def script(run):
                    self.assertIsNone(run.context["active_assignment"])
                    offered = [t for t in run.tools if t["name"] == "reply_to_customer"]
                    self.assertEqual(offered, [team_reply.schema()])
                    self.answer(run, text)
                    run.call("publish_artifact", filename="must-not-exist.txt", content="late",
                             summary="Must not execute", format="text")

                self.f.driver.scripts = {recipient: [script]}
                source = self.f.receive("Explain only; do not adopt a new goal.",
                                        role=recipient if recipient != self.manager else None)
                self.f.drain()
                run = self.f.driver.runs[-1]
                self.assertIsInstance(run, external.ScriptedExternalRun)
                self.assertTrue(run.revoked)
                self.assertEqual(run.state.phase, "yielded")
                self.assertEqual([call.name for call, _ in run.receipts], ["reply_to_customer"])
                receipt = run.receipts[0][1]
                message = next(e for e in self.customer_messages() if e.seq == receipt["message_seq"])
                self.assertEqual(receipt, {"status": "ok", "message_seq": message.seq, "reply_to": source.seq})
                self.assertEqual(message.actor, Actor.agent(recipient, self.rt.team._role(recipient).role))
                for key, value in {"text": text, "recipient": "human", "reply_to": source.seq,
                                   "response_kind": "reply"}.items():
                    self.assertEqual(message.payload[key], value)
                self.assertEqual(message.payload["run_id"], self.events("team.run_started")[-1].payload["run_id"])
                self.assertEqual(len([e for e in self.customer_messages() if e.payload["reply_to"] == source.seq]), 1)
                self.assertEqual(self.events("team.run_finished")[-1].payload["status"], "completed")
                self.assertEqual(self.events("team.external_run_finished")[-1].payload["outcome"], "yielded")
        current = self.rt.team.view(self.task)
        self.assertEqual((current["objective"], current["revision"]),
                         (original["objective"], original["revision"]))
        self.assertEqual(current["pending_count"], 0)
        self.assertEqual(self.rt.task(self.task).deliverables, [])
        self.assert_no_task_verdict()

    def test_reply_schema_is_shared_local_and_pause_safe(self):
        self.assertIn("reply_to_customer", LOCAL_EVENT_TOOLS)
        self.assertIn("reply_to_customer", PAUSED_TOOLS)
        for role_id in (self.manager, self.worker):
            tools = self.rt.team._schemas(self.rt.team._role(role_id), None)
            self.assertEqual([t for t in tools if t["name"] == "reply_to_customer"], [team_reply.schema()])
        schema = team_reply.schema()["input_schema"]
        self.assertEqual(schema["required"], ["text"])
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["properties"], {"text": {
            "type": "string", "minLength": 1, "maxLength": 32000}})

    def test_done_followup_preserves_delivery_archive_hashes_and_objective(self):
        def deliver(run):
            self.assertEqual(run.call("set_objective", goal="Deliver the original exact file.")["status"], "ok")
            self.f.publish(run, "original delivered bytes\n")
            self.f.deliver(run)

        self.f.driver.scripts = {self.manager: [deliver]}
        self.f.receive("Publish and deliver the original file.")
        self.f.drain()
        before = copy.deepcopy(self.rt.team.view(self.task))
        published = copy.deepcopy(self.rt.task(self.task).deliverables)
        completed = list(self.events("team.objective_completed"))
        with zipfile.ZipFile(io.BytesIO(self.rt.team.archive(self.task, before["revision"]))) as archive:
            frozen = {name: archive.read(name) for name in archive.namelist()}
        for recipient in (self.manager, self.worker):
            def explain(run):
                artifact = run.call("read_artifact", artifact_seq=published[0]["seq"])
                self.assertEqual(artifact["content"], "original delivered bytes\n")
                self.answer(run, "That is the original published version; no new delivery was made.")

            self.f.driver.scripts = {recipient: [explain]}
            self.f.receive("Explain the existing delivery.", role=recipient if recipient != self.manager else None)
            self.f.drain()
        current = self.rt.team.view(self.task)
        self.assertEqual((current["status"], current["objective"], current["revision"]),
                         ("done", before["objective"], before["revision"]))
        self.assertEqual(self.rt.task(self.task).deliverables, published)
        self.assertEqual(self.events("team.objective_completed"), completed)
        self.assertEqual(len(self.events("team.objective_updated")), 1)
        self.assertEqual(len(self.f.results("finish_work")), 1)
        self.assertEqual(self.events("team.review_recorded"), [])
        with zipfile.ZipFile(io.BytesIO(self.rt.team.archive(self.task, before["revision"]))) as archive:
            self.assertEqual({name: archive.read(name) for name in archive.namelist()}, frozen)
            manifest = json.loads(archive.read("__cater__/manifest.json"))
            self.assertEqual(manifest["files"][0]["sha256"],
                             hashlib.sha256(archive.read("result.txt")).hexdigest())

    def test_cumulative_public_progress_is_separate_and_saved_before_dispatch(self):
        first, second = "Checking the saved receipts.\n", "The exact version is now verified.\n"

        def script(run):
            run.state = replace(run.state, text=first)
            result = run.call("reply_to_customer", text=" ")
            self.assertEqual(result["status"], "error")
            self.assertFalse(run.revoked)
            run.state = replace(run.state, text=first + second)
            self.answer(run, "Only this explicit answer belongs to the customer.")

        self.f.driver.scripts = {self.manager: [script]}
        source = self.f.receive()
        with patch.object(self.rt.team, "_finish_message", wraps=self.rt.team._finish_message) as finish:
            self.f.drain()
        self.assertEqual(finish.call_args.kwargs["final"], "Only this explicit answer belongs to the customer.")
        progress = [e for e in self.events("msg.said") if e.payload.get("response_kind") == "progress"]
        self.assertEqual([e.payload["text"] for e in progress], [first, second])
        starts = [e for e in self.events("team.tool_started") if e.payload["name"] == "reply_to_customer"]
        for event, start in zip(progress, starts):
            self.assertLess(event.seq, start.seq)
            self.assertEqual(event.payload["audience"], "team")
            self.assertEqual(event.payload["reply_to"], source.seq)
        answer, = self.customer_messages()
        self.assertEqual(answer.payload["text"], "Only this explicit answer belongs to the customer.")
        self.assertLess(starts[-1].seq, answer.seq)
        receipt = self.events("team.tool_finished")[-1]
        self.assertLess(answer.seq, receipt.seq)
        self.assertEqual(receipt.payload["result"]["message_seq"], answer.seq)
        self.assert_no_task_verdict()

    def test_invalid_arguments_are_correctable_and_32000_characters_are_accepted(self):
        invalid = [{}, {"text": "Valid but extra", "recipient": "human"}, {"text": None},
                   {"text": 7}, {"text": ""}, {"text": " \n\t"}, {"text": "x" * 32001}]

        def script(run):
            for arguments in invalid:
                result = run.call("reply_to_customer", **arguments)
                self.assertEqual(result["status"], "error", result)
                self.assertNotEqual(result.get("outcome"), "effects_unknown")
                self.assertFalse(run.revoked)
                self.assertEqual(self.customer_messages(), [])
            self.answer(run, "x" * 32000)

        self.f.driver.scripts = {self.manager: [script]}
        self.f.receive()
        self.f.drain()
        self.assertEqual([r["status"] for r in self.f.results("reply_to_customer")],
                         ["error"] * len(invalid) + ["ok"])
        answer, = self.customer_messages()
        self.assertEqual(len(answer.payload["text"]), 32000)
        self.assertEqual(self.events("team.run_finished")[-1].payload["status"], "completed")

    def test_agent_message_and_delegation_do_not_offer_reply(self):
        def wait(run):
            self.assertNotIn("reply_to_customer", {tool["name"] for tool in run.tools})
            run.call("wait_for_reply")
            self.fail("Wait must still yield")

        for kind in ("message", "assignment"):
            with self.subTest(kind=kind):
                self.rt.team._send(self.task, self.rt.team._role(self.manager), self.worker,
                                   "Wait for your sender; do not answer as the customer.", kind)
                self.f.driver.scripts = {self.worker: [wait]}
                if kind == "assignment":
                    self.f.driver.scripts[self.manager] = [wait]
                self.f.drain()
        self.assertEqual(self.customer_messages(), [])
        self.assert_no_task_verdict()

    def test_human_questionnaire_answer_with_active_assignment_does_not_offer_reply(self):
        def ask(run):
            self.assertIsNotNone(run.context["active_assignment"])
            run.call("request_questions", title="Clarify the delegated work", questions=[{
                "id": "scope", "question": "Which scope?", "selection": "single",
                "options": [{"id": "original", "label": "Original scope"}]}])
            self.fail("A question must yield")

        def resume(run):
            self.assertEqual(run.context["message"]["sender"], "human")
            self.assertIsNotNone(run.context["active_assignment"])
            self.assertNotIn("reply_to_customer", {t["name"] for t in run.tools})
            source = self.events("team.message_received")[-1]
            role = self.rt.team._role(self.worker)
            self.assertIsNotNone(self.rt.team._waiting_assignment(self.task, source, role))
            run.call("wait_for_reply")

        self.f.driver.scripts = {self.manager: [self.f.delegate_wait], self.worker: [ask]}
        self.f.receive()
        self.f.drain()
        self.f.driver.scripts = {self.worker: [resume], self.manager: [lambda run: run.call("wait_for_reply")]}
        self.f.receive("Use the original scope.", role=self.worker)
        self.f.drain()
        self.assertEqual(self.f.results("reply_to_customer"), [])
        self.assertEqual(self.events("team.objective_completed"), [])
        self.assertFalse([e for e in self.events("team.message_received") if e.payload["kind"] == "report"])

    def test_shared_helper_rejects_agent_delegation_and_explicit_assignment(self):
        for kind in ("message", "assignment", "human"):
            with self.subTest(kind=kind):
                task, role, source, _, start = self.helper_context(source_kind=kind)
                before = self.rt.log.read(task_id=task)
                with self.assertRaises(WorkConflict):
                    self.execute_helper(task, role, start, assignment=source if kind == "human" else None)
                self.assertEqual(self.rt.log.read(task_id=task), before)

    def test_shared_helper_requires_persisted_matching_host_start_and_active_run(self):
        task, role, source, run, start = self.helper_context()
        for forged in (replace(start, seq=start.seq + 100000),
                       replace(start, actor=Actor.human()),
                       replace(start, payload={**start.payload, "role": self.worker})):
            with self.subTest(forged=forged.seq):
                with self.assertRaises(WorkConflict):
                    self.execute_helper(task, role, forged)
        self.rt.log.emit(type="team.run_finished", task_id=task, actor=HOST, payload={
            "role": role.id, "run_id": run.payload["run_id"], "message_seq": source.seq,
            "status": "completed", "reason": "The prior run already ended."})
        with self.assertRaises(WorkConflict):
            self.execute_helper(task, role, start)
        self.assertFalse([e for e in self.rt.log.read(task_id=task) if e.type == "msg.said"])

    def test_shared_helper_rechecks_assignment_when_caller_passes_none(self):
        task, role, source, _, start = self.helper_context(recipient=self.worker, waiting_assignment=True)
        self.assertIsNotNone(self.rt.team._waiting_assignment(task, source, role))
        before = self.rt.log.read(task_id=task)
        with self.assertRaises(WorkConflict):
            self.execute_helper(task, role, start, assignment=None)
        self.assertEqual(self.rt.log.read(task_id=task), before)

    def test_shared_helper_rejects_newer_human_revision_and_control_changes(self):
        for change in ("human", "revision", "control"):
            with self.subTest(change=change):
                task, role, _, _, start = self.helper_context()
                if change == "human":
                    self.rt.team.receive(task, "Newer customer instruction takes priority.")
                elif change == "revision":
                    self.rt.log.emit(type="team.objective_updated", task_id=task, actor=HOST,
                                     payload={"goal": "New independently adopted goal."})
                else:
                    self.rt.execution_control.change(task, "pause", request_id="reply-control-pause")
                    self.rt.execution_control.change(task, "resume", request_id="reply-control-resume")
                before = self.rt.log.read(task_id=task)
                with self.assertRaises(WorkConflict):
                    self.execute_helper(task, role, start)
                self.assertEqual(self.rt.log.read(task_id=task), before)

    def test_atomic_receipt_failure_rolls_back_answer_then_retry_sends_one_copy(self):
        emitted = self.rt.log.emit
        rolled_back = []

        def fail_receipt(**kwargs):
            if (kwargs["type"] == "team.tool_finished"
                    and kwargs["payload"]["name"] == "reply_to_customer"
                    and kwargs["payload"]["result"].get("status") == "ok" and not rolled_back):
                rolled_back.extend(self.customer_messages())
                self.assertTrue(self.rt.log.db.in_transaction)
                raise OSError("Injected receipt write failure")
            return emitted(**kwargs)

        def script(run):
            with patch.object(self.rt.log, "emit", side_effect=fail_receipt):
                result = run.call("reply_to_customer", text="Retry this same answer.")
            self.assertEqual(result["status"], "error")
            self.assertEqual(len(rolled_back), 1)
            self.assertEqual(self.customer_messages(), [])
            self.assertFalse(run.revoked)
            self.answer(run, "Retry this same answer.")

        self.f.driver.scripts = {self.manager: [script]}
        self.f.receive()
        self.f.drain()
        answer, = self.customer_messages()
        results = self.f.results("reply_to_customer")
        self.assertEqual([r["status"] for r in results], ["error", "ok"])
        self.assertEqual(results[-1]["message_seq"], answer.seq)
        self.assertEqual(self.rt.team._pending_operations(self.task), [])
        self.assert_no_task_verdict()

    def test_same_run_helper_retry_is_idempotent_and_cannot_replace_sent_text(self):
        task, role, source, _, start = self.helper_context()
        first = self.execute_helper(task, role, start, text="  A bounded answer.  ")
        self.assertEqual(self.execute_helper(task, role, start), first)
        self.assertEqual(first["reply_to"], source.seq)
        with self.assertRaises(WorkConflict):
            self.execute_helper(task, role, start, text="A different answer.")
        answers = [e for e in self.rt.log.read(task_id=task) if e.type == "msg.said"]
        self.assertEqual(len(answers), 1)
        self.assertEqual(answers[0].payload["text"], "A bounded answer.")

    def test_paused_reply_preserves_unconfirmed_external_hold(self):
        def fail_cleanup(run):
            run.cleanup_complete = False
            raise RuntimeError("Offline invocation remains unconfirmed")

        self.f.driver.scripts = {self.manager: [fail_cleanup]}
        self.f.receive("Start an invocation whose cleanup cannot be confirmed.")
        self.f.drain()
        pending = copy.deepcopy(self.rt.team.external_runs.pending(self.task))
        self.assertEqual(len(pending), 1)
        self.rt.execution_control.change(self.task, "pause", request_id="reply-held-pause")
        control = self.rt.execution_control.view(self.task)

        def explain(run):
            names = {tool["name"] for tool in run.tools}
            self.assertIn("reply_to_customer", names)
            self.assertNotIn("finish_work", names)
            self.assertNotIn("delegate", names)
            self.answer(run, "The earlier invocation still needs inspection; execution remains paused.")

        self.f.driver.scripts = {self.manager: [explain]}
        self.f.receive("Explain the pending invocation without resuming it.")
        self.f.drain()
        self.assertEqual(self.rt.execution_control.view(self.task), control)
        self.assertEqual(self.rt.team.external_runs.pending(self.task), pending)
        self.assertEqual(len(self.customer_messages()), 1)
        self.assert_no_task_verdict()

    def test_unconfirmed_cleanup_keeps_durable_answer_and_pending_without_extra_chat(self):
        def script(run):
            run.cleanup_complete = False
            self.answer(run, "The saved file contains the requested value.")

        self.f.driver.scripts = {self.manager: [script]}
        self.f.receive()
        self.f.drain()
        answer, = self.customer_messages()
        self.assertEqual(answer.payload["text"], "The saved file contains the requested value.")
        self.assertEqual(len(self.events("msg.said")), 1)
        self.assertEqual(self.f.results("reply_to_customer"), [{
            "status": "ok", "message_seq": answer.seq, "reply_to": answer.payload["reply_to"]}])
        pending, = self.rt.team.external_runs.pending(self.task)
        self.assertEqual(pending["outcome"], "unknown")
        self.assertFalse(pending["replay_authorized"])
        self.assertEqual(self.events("team.run_finished")[-1].payload["status"], "interrupted")
        finished, = self.events("team.external_run_finished")
        self.assertFalse(finished.payload["cleanup_complete"])
        self.assertTrue(self.f.driver.runs[-1].revoked)
        self.assert_no_task_verdict()

    def test_native_direct_human_replies_keep_progress_internal_and_skip_later_calls(self):
        fixture = native.TeamSupervisionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        original = fixture.rt.team.view(fixture.task)
        for role_id in (fixture.manager, fixture.worker):
            with self.subTest(role=role_id):
                role = fixture.rt.team._role(role_id)

                def script(context, history, tools):
                    self.assertEqual(history, [])
                    self.assertIsNone(context["active_assignment"])
                    self.assertIn("reply_to_customer", {t["name"] for t in tools})
                    return fixture.native_turn("Checking the native receipts.",
                        ("reply_to_customer", {"text": "The native answer is explicit."}),
                        ("publish_artifact", {"filename": "must-not-exist.txt", "content": "late",
                                              "summary": "Must not execute", "format": "text"}))

                fixture.driver.steps[role_id] = [script,
                    AssertionError("Successful native reply must not ask the model for acknowledgment")]
                source = fixture.rt.team.receive(fixture.task, f"@{role.name} Explain only.")
                calls_before = len(fixture.driver.calls)
                fixture.run_team()
                self.assertEqual(len(fixture.driver.calls), calls_before + 1)
                self.assertEqual(len(fixture.driver.steps[role_id]), 1)
                said = [e for e in fixture.events("msg.said") if e.payload.get("reply_to") == source.seq]
                self.assertEqual([(e.payload["audience"], e.payload["response_kind"], e.payload["text"])
                                  for e in said], [
                    ("team", "progress", "Checking the native receipts."),
                    ("customer", "reply", "The native answer is explicit.")])
                self.assertEqual(said[-1].actor, Actor.agent(role_id, role.role))
                self.assertEqual(said[-1].payload["recipient"], "human")
                starts = [e for e in fixture.events("team.tool_started")
                          if e.payload["message_seq"] == source.seq]
                self.assertEqual([e.payload["name"] for e in starts], ["reply_to_customer"])
                receipt = fixture.events("team.tool_finished")[-1]
                self.assertEqual(receipt.payload["result"], {"status": "ok",
                    "message_seq": said[-1].seq, "reply_to": source.seq})
                self.assertLess(said[0].seq, starts[0].seq)
                self.assertLess(said[-1].seq, receipt.seq)
                finished = fixture.events("team.run_finished")[-1]
                self.assertLess(receipt.seq, finished.seq)
                self.assertEqual(finished.payload["status"], "completed")
        current = fixture.rt.team.view(fixture.task)
        self.assertEqual((current["objective"], current["revision"]),
                         (original["objective"], original["revision"]))
        self.assertEqual(fixture.rt.task(fixture.task).deliverables, [])
        self.assertEqual(fixture.events("team.objective_completed"), [])
        self.assertEqual(fixture.events("team.review_recorded"), [])

    def test_native_invalid_reply_can_be_corrected_without_premature_completion(self):
        fixture = native.TeamSupervisionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)

        def correct(context, history, tools):
            result = json.loads(history[-1]["content"])
            self.assertEqual(result["status"], "error")
            self.assertEqual(fixture.events("team.run_finished"), [])
            self.assertFalse([e for e in fixture.events("msg.said")
                              if e.payload.get("audience") == "customer"])
            return fixture.native_turn("", ("reply_to_customer", {"text": "Corrected native answer."}))

        fixture.driver.steps[fixture.manager] = [
            fixture.native_turn("Checking before answering.", ("reply_to_customer", {"text": " "})),
            correct, AssertionError("Corrected reply must terminate without another model call")]
        fixture.rt.team.receive(fixture.task, "Answer after correcting invalid arguments.")
        fixture.run_team()
        self.assertEqual(len(fixture.driver.calls), 2)
        self.assertEqual([r["status"] for r in fixture.results("reply_to_customer")], ["error", "ok"])
        self.assertEqual(fixture.events("team.run_finished")[-1].payload["status"], "completed")
        answers = [e for e in fixture.events("msg.said") if e.payload.get("audience") == "customer"]
        self.assertEqual([e.payload["text"] for e in answers], ["Corrected native answer."])

    def test_legacy_natural_text_reply_still_works_without_tool(self):
        self.f.driver.scripts = {self.manager: [lambda run: "Legacy natural-language answer."]}
        source = self.f.receive()
        self.f.drain()
        answer, = self.customer_messages()
        self.assertEqual(answer.payload["text"], "Legacy natural-language answer.")
        self.assertEqual(answer.payload["reply_to"], source.seq)
        self.assertEqual(self.f.results("reply_to_customer"), [])
        self.assertEqual(self.events("team.external_run_finished")[-1].payload["outcome"], "returned")
        self.assert_no_task_verdict()


if __name__ == "__main__":
    unittest.main()
