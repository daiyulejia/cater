"""Addressed outputs must become actionable inputs without losing their delegation."""

import json
import unittest

from cater.drivers import ToolRequestRejected
from cater.team import HOST
from test_team_supervision import TeamSupervisionTests


class TeamRelayTests(TeamSupervisionTests):
    def reports_to(self, recipient):
        return [e for e in self.events("team.message_received")
                if e.payload["kind"] == "report" and e.payload["recipient"] == recipient]

    def test_child_progress_resumes_the_original_delegation(self):
        helper = next(r.id for r in self.rt.worker_roles if r.id != self.worker)
        self.driver.steps = {
            self.manager: [
                self.native_turn("", ("delegate", {"recipient": self.worker, "instruction": "Prepare the reviewed guide"})),
                self.native_turn("", ("wait_for_reply", {})),
                self.native_turn("Progress received.", ("wait_for_reply", {})),
                self.native_turn("Final guide received."),
            ],
            self.worker: [
                self.native_turn("", ("delegate", {"recipient": helper, "instruction": "Prepare the evidence"})),
                self.native_turn("", ("wait_for_reply", {})),
                self.native_turn("Evidence inspected; the original guide is now ready."),
            ],
            helper: [self.native_turn("", ("publish_artifact", {
                "filename": "evidence.txt", "content": "Evidence body", "summary": "Prepared evidence", "format": "text"}),
                ("wait_for_reply", {}))],
        }
        self.rt.team.receive(self.task, "Prepare the guide", request_id="relay-progress")
        self.run_team()
        reports = self.reports_to(self.manager)
        self.assertEqual(len(reports), 1)
        self.assertIn("original guide is now ready", reports[0].payload["text"])
        resumed, = [c["context"] for c in self.driver.calls
                     if c["context"]["you"]["id"] == self.worker and c["context"]["message"]["kind"] == "progress"]
        self.assertEqual(resumed["active_assignment"]["sender"], self.manager)
        self.assertEqual(self.rt.team.view(self.task)["pending_count"], 0)
        self.assert_one_run_per_role()

    def test_information_reply_restores_the_questioners_work(self):
        helper = next(r.id for r in self.rt.worker_roles if r.id != self.worker)

        def answer(context, history, tools):
            return self.native_turn("", ("send_message", {"recipient": self.worker,
                "text": "UTF-8 text is available.", "reply_to": context["message"]["seq"]}))

        self.driver.steps = {
            self.manager: [
                self.native_turn("", ("delegate", {"recipient": self.worker, "instruction": "Prepare the delivery with advice"})),
                self.native_turn("", ("wait_for_reply", {})),
                self.native_turn("Progress noted.", ("wait_for_reply", {})),
                self.native_turn("Delivery received."),
            ],
            self.worker: [
                self.native_turn("", ("send_message", {"recipient": helper, "text": "Which input format is available?"})),
                self.native_turn("", ("wait_for_reply", {})),
                self.native_turn("The advice arrived and the delivery is ready."),
            ],
            helper: [
                answer,
                self.native_turn("", ("wait_for_reply", {})),
            ],
        }
        self.rt.team.receive(self.task, "Prepare the delivery", request_id="relay-information")
        self.run_team()
        question, = [e for e in self.events("team.message_received") if e.payload["kind"] == "message"
                     and e.actor.id == self.worker]
        response, = [e for e in self.events("team.message_received") if e.payload["kind"] == "report"
                     and e.actor.id == helper]
        self.assertEqual(response.payload["reply_to"], question.seq)
        self.assertTrue(response.payload["visible"])
        resumed, = [c["context"] for c in self.driver.calls
                     if c["context"]["message"]["seq"] == response.seq]
        self.assertEqual(resumed["active_assignment"]["sender"], self.manager)
        self.assertEqual(len(self.reports_to(self.manager)), 1)
        self.assert_one_run_per_role()

    def test_explicit_reply_is_not_automatically_delivered_again(self):
        def answer(context, history, tools):
            return self.native_turn("", ("send_message", {"recipient": self.manager,
                "text": "The requested result.", "reply_to": context["message"]["seq"]}))

        self.driver.steps = {
            self.manager: [self.native_turn("", ("delegate", {
                "recipient": self.worker, "instruction": "Check and report"})),
                self.native_turn("", ("wait_for_reply", {})), self.native_turn("Received.", ("wait_for_reply", {}))],
            self.worker: [answer, self.native_turn("The result was sent.")],
        }
        self.rt.team.receive(self.task, "Check", request_id="relay-explicit-once")
        self.run_team()
        reports = self.reports_to(self.manager)
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0].payload["text"], "The requested result.")

    def test_author_reviewer_correction_recheck_returns_to_original_manager(self):
        reviewer = next(r.id for r in self.rt.worker_roles if r.id != self.worker)

        def review_artifact(context, history, tools):
            artifact = self.rt.task(self.task).deliverables[-1]
            return self.native_turn("Reading the actual submitted body.", ("read_artifact", {"artifact_seq": artifact["seq"]}))

        def inspect_bad(context, history, tools):
            result = json.loads(history[-1]["content"])
            self.assertIn("BUG: negative input rejected", result["content"])
            return self.native_turn("Negative input must be supported.", ("delegate", {
                "recipient": self.worker, "instruction": "Fix negative input; preserve the original requirements, return the corrected file."}))

        def inspect_fixed(context, history, tools):
            result = json.loads(history[-1]["content"])
            self.assertIn("FIXED: negative input supported", result["content"])
            self.assertEqual(context["active_assignment"]["sender"], self.worker)
            return self.native_turn("Recheck complete: the corrected file satisfies the requested behavior.")

        def manager(context, history, tools):
            if context["message"]["sender"] == "human":
                return (self.native_turn("", ("wait_for_reply", {})) if history else
                        self.native_turn("", ("delegate", {"recipient": self.worker, "instruction": "Implement and obtain independent review"})))
            if context["message"]["kind"] != "report":
                return self.native_turn("Progress received.", ("wait_for_reply", {}))
            if not history:
                return self.native_turn("", ("finish_work", {"summary": "Reviewed delivery ready",
                    "verification": "Fixture body review only, no real model or runtime test"}))
            return self.native_turn("Reviewed delivery is ready.")

        def author_resumes(context, history, tools):
            self.assertEqual(context["active_assignment"]["sender"], self.manager)
            if context["message"]["kind"] == "progress":
                return self.native_turn("Progress understood.", ("wait_for_reply", {}))
            return self.native_turn("Implementation corrected and independently rechecked.")

        self.driver.steps = {
            self.manager: [manager] * 12,
            self.worker: [
                self.native_turn("", ("publish_artifact", {"filename": "result.txt", "content": "BUG: negative input rejected",
                    "summary": "Initial implementation", "format": "text"})),
                self.native_turn("", ("delegate", {"recipient": reviewer, "instruction": "Read result.txt and review negative input against the original requirements"})),
                self.native_turn("", ("wait_for_reply", {})),
                self.native_turn("", ("publish_artifact", {"filename": "result.txt", "content": "FIXED: negative input supported",
                    "summary": "Corrected implementation", "format": "text"})),
                self.native_turn("The requested correction is ready for recheck."),
            ] + [author_resumes] * 4,
            reviewer: [review_artifact, inspect_bad, self.native_turn("", ("wait_for_reply", {})),
                       review_artifact, inspect_fixed],
        }
        self.rt.team.receive(self.task, "Implement with review", request_id="relay-review-cycle")
        self.run_team()
        self.assertEqual(self.rt.team.view(self.task)["status"], "done")
        self.assertEqual(self.rt.team.view(self.task)["pending_count"], 0)
        self.assertEqual(len(self.reports_to(self.manager)), 1)
        self.assertIn("independently rechecked", self.reports_to(self.manager)[0].payload["text"])
        self.assertEqual(len(self.reports_to(reviewer)), 1)
        self.assertEqual(len(self.reports_to(self.worker)), 1)
        self.assert_one_run_per_role()

    def test_review_cycle_also_works_when_roles_use_plain_addressed_messages(self):
        native_turn = self.native_turn

        def addressed(text="", *calls):
            return native_turn(text, *(("send_message", {
                "recipient": arguments["recipient"], "text": arguments["instruction"]})
                if name == "delegate" else (name, arguments) for name, arguments in calls))

        self.native_turn = addressed
        self.test_author_reviewer_correction_recheck_returns_to_original_manager()

    def test_plain_message_result_is_delivered_once_without_receipt_echo(self):
        self.driver.steps = {
            self.manager: [self.native_turn("", ("send_message", {
                "recipient": self.worker, "text": "Check this assumption and report your conclusion."})),
                self.native_turn("", ("wait_for_reply", {})),
                self.native_turn("Conclusion received; nothing else to do.", ("wait_for_reply", {}))],
            self.worker: [self.native_turn("I checked the input; the assumption does not hold.")],
        }
        self.rt.team.receive(self.task, "Check the assumption", request_id="relay-no-echo")
        self.run_team()
        reports = self.reports_to(self.manager)
        self.assertEqual(len(reports), 1)
        self.assertIn("does not hold", reports[0].payload["text"])
        self.assertNotIn("visible", reports[0].payload)
        self.assertEqual(self.reports_to(self.worker), [])
        self.assertEqual(len(self.driver.calls), 4)

    def test_reply_reference_cannot_bind_another_group_or_wrong_sender(self):
        other = self.rt.create_task("Another group")
        self.rt.team.enable(other)
        foreign = self.rt.team.receive(other, "Private context", request_id="relay-foreign-source")
        source = self.rt.team.receive(self.task, "Local context", request_id="relay-local-source")
        self.driver.steps[self.manager] = [self.native_turn("",
            ("send_message", {"recipient": self.worker, "text": "Invalid foreign reference", "reply_to": foreign.seq}),
            ("send_message", {"recipient": self.worker, "text": "Invalid sender reference", "reply_to": source.seq})),
            self.native_turn("Invalid references were not delivered.")]
        self.run_team()
        results = self.results("send_message")
        self.assertEqual([r["status"] for r in results], ["error", "error"])
        self.assertFalse([e for e in self.events("team.message_received") if e.actor.kind == "agent"])

    def test_failed_model_request_explains_rate_limit_without_leaking_exception_text(self):
        self.driver.steps[self.manager] = [ToolRequestRejected("secret-diagnostic-sentinel", status_code=429)]
        self.rt.team.receive(self.task, "Start", request_id="relay-http-failure")
        self.rt.collaborate(self.task)
        finished, = self.events("team.run_finished")
        self.assertEqual(finished.payload["status"], "failed")
        self.assertEqual(finished.payload["error_type"], "ToolRequestRejected")
        self.assertEqual(finished.payload["status_code"], 429)
        self.assertIn("限流", finished.payload["reason"])
        self.assertNotIn("secret-diagnostic-sentinel", json.dumps([e.payload for e in self.rt.log.read(task_id=self.task)]))
        self.assertEqual(self.rt.team.view(self.task)["status"], "running")

    def test_old_objective_message_cannot_start_obsolete_work(self):
        manager = self.rt.organization.leader
        self.rt.team._send(self.task, manager, self.worker, "Publish the obsolete implementation", "message")
        self.rt.log.emit(type="team.objective_updated", actor=HOST, task_id=self.task,
                         payload={"goal": "Use the new implementation instead"})
        self.driver.steps[self.manager] = [self.native_turn("Old message was retired.", ("wait_for_reply", {}))]
        self.run_team()
        self.assertEqual(self.rt.task(self.task).deliverables, [])
        self.assertFalse([c for c in self.driver.calls if c["context"]["you"]["id"] == self.worker])
        self.assertTrue(any(r["status"] == "interrupted" and r["role"] == self.worker
                            for r in self.rt.team.view(self.task)["runs"]))

    def test_pause_buffers_peer_messages_until_resume_but_customer_can_chat(self):
        message = self.rt.team._send(self.task, self.rt.organization.leader, self.worker,
                                     "Prepare the result after resuming", "message")
        self.rt.execution_control.change(self.task, "pause", request_id="relay-pause")
        self.driver.steps[self.manager] = [self.native_turn("Paused; your message is visible.", ("wait_for_reply", {}))]
        self.rt.team.receive(self.task, "How is it going?", request_id="relay-paused-chat")
        self.run_team()
        self.assertEqual(self.rt.team.view(self.task)["pending_count"], 1)
        self.assertFalse([e for e in self.events("team.run_started") if e.payload["message_seq"] == message.seq])
        self.assertTrue(all(c["context"]["you"]["id"] == self.manager for c in self.driver.calls))
        self.rt.execution_control.change(self.task, "resume", request_id="relay-resume")
        self.driver.steps[self.worker] = [self.native_turn("The queued result is ready.")]
        self.driver.steps[self.manager] = [self.native_turn("Result received.", ("wait_for_reply", {}))]
        self.run_team()
        self.assertEqual(len(self.reports_to(self.manager)), 1)
        self.assertEqual(self.rt.team.view(self.task)["pending_count"], 0)

    def test_manager_completion_does_not_wake_reviewer_for_another_receipt(self):
        self.driver.steps = {
            self.manager: [self.native_turn("", ("send_message", {
                "recipient": self.worker, "text": "Check the result and tell me"})),
                self.native_turn("", ("wait_for_reply", {})),
                self.native_turn("", ("finish_work", {"summary": "Delivery confirmed", "verification": "Fixture only"})),
                self.native_turn("Delivery confirmed.")],
            self.worker: [self.native_turn("", ("send_message", {
                "recipient": self.manager, "text": "I checked the result; the fixture is ready."})),
                self.native_turn("", ("wait_for_reply", {}))],
        }
        self.rt.team.receive(self.task, "Check and finish", request_id="relay-final-no-echo")
        self.run_team()
        self.assertEqual(self.rt.team.view(self.task)["status"], "done")
        self.assertEqual(self.reports_to(self.worker), [])
        self.assertEqual(sum(c["context"]["you"]["id"] == self.worker for c in self.driver.calls), 2)

    def test_new_peer_message_keeps_unreturned_original_work_in_context(self):
        worker = self.rt._role_by_id(self.worker)
        helper = next(r for r in self.rt.worker_roles if r.id != self.worker)
        original = self.rt.team._send(self.task, self.rt.organization.leader, worker.id,
                                     "Original implementation and review", "message")
        self.rt.team._send(self.task, worker, helper.id, "Review the implementation", "message",
                           parent_assignment_seq=original.seq)
        incoming = self.rt.team._send(self.task, helper, worker.id, "The review has concrete findings", "message")
        context = self.rt.team._context(self.task, incoming, worker)
        self.assertIn(original.seq, [a["seq"] for a in context["open_assignments"]])
        self.assertEqual(context["active_assignment"]["seq"], incoming.seq)
        self.rt.team._send(self.task, worker, self.manager, "Original work delivered", "report", reply_to=original.seq)
        context = self.rt.team._context(self.task, incoming, worker)
        self.assertNotIn(original.seq, [a["seq"] for a in context["open_assignments"]])

    def test_work_answered_before_claim_is_not_executed_again(self):
        manager = self.rt.organization.leader
        first = self.rt.team._send(self.task, manager, self.worker, "Work A", "message")
        second = self.rt.team._send(self.task, manager, self.worker, "Work B", "message")
        self.driver.steps = {
            self.worker: [self.native_turn("", ("send_message", {
                "recipient": self.manager, "text": "Work B is already handled with A.", "reply_to": second.seq})),
                self.native_turn("Work A is complete."), self.native_turn("THIS MUST NOT RUN")],
            self.manager: [self.native_turn("Received B.", ("wait_for_reply", {})),
                           self.native_turn("Received A.", ("wait_for_reply", {}))],
        }
        self.run_team()
        starts = [e.payload["message_seq"] for e in self.events("team.run_started")]
        self.assertIn(first.seq, starts)
        self.assertNotIn(second.seq, starts)
        self.assertEqual(self.rt.team.view(self.task)["pending_count"], 0)
        self.assertEqual(len(self.reports_to(self.manager)), 2)
        self.assertNotIn("THIS MUST NOT RUN", json.dumps([e.payload for e in self.rt.log.read(task_id=self.task)]))

    def test_manager_finish_and_wait_does_not_send_late_progress(self):
        self.rt.team._send(self.task, self.rt._role_by_id(self.worker), self.manager, "Confirm the final delivery", "assignment")
        self.driver.steps[self.manager] = [self.native_turn("",
            ("finish_work", {"summary": "Confirmed", "verification": "Fixture only"}), ("wait_for_reply", {}))]
        self.run_team()
        self.assertEqual(self.rt.team.view(self.task)["status"], "done")
        self.assertEqual(len(self.driver.calls), 1)
        self.assertFalse([e for e in self.events("team.message_received") if e.actor.id == self.manager])


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(TeamRelayTests(name) for name in TeamRelayTests.__dict__ if name.startswith("test_"))


if __name__ == "__main__":
    unittest.main()
