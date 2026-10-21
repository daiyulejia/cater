"""Offline waiting-child boundaries; scripted drivers never launch a model.

Integration cases compose existing fixtures instead of inheriting their tests.
Ledger cases seed host run facts explicitly to isolate recovery projections;
these facts are not presented as model/tool execution receipts.
"""

from dataclasses import replace
import unittest

from cater.events import Actor
from cater.team import HOST, _reported_messages
import test_team_external
import test_team_supervision


class WaitingChildExternalTests(unittest.TestCase):
    def setUp(self):
        self.f = test_team_external.TeamExternalTests()
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()
        self.rt, self.task = self.f.rt, self.f.task
        self.manager, self.worker, self.helper = self.f.manager, self.f.worker, self.f.reviewer

    def tearDown(self):
        self.assertEqual(self.f.driver.errors, [])
        self.assertTrue(self.rt.verify_chain().ok)
        self.f.assert_receipt_chain()

    def messages(self, sender=None, recipient=None):
        return [e for e in self.f.events("team.message_received")
                if (sender is None or e.actor.id == sender)
                and (recipient is None or e.payload["recipient"] == recipient)]

    def run_one(self, role, script, *, kind=None, status="waiting"):
        self.f.driver.scripts[role] = [script]
        claimed = self.rt.team._claim(self.task)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed[1].id, role)
        if kind is not None:
            self.assertEqual(claimed[0].payload["kind"], kind)
        list(self.rt.team._run_message(self.task, *claimed))
        self.assertEqual(self.f.driver.scripts[role], [])
        finished, = [e for e in self.f.events("team.run_finished")
                     if e.payload["run_id"] == claimed[2].payload["run_id"]]
        self.assertEqual(finished.payload["status"], status, finished.payload)
        return claimed

    def begin(self):
        self.f.receive()
        self.run_one(self.manager, self.f.delegate_wait)

    def wait_with_child(self, run, *, text="", publish=False):
        result = run.call("delegate", recipient=self.helper, instruction="Inspect and formally return evidence.")
        self.assertEqual(result["status"], "ok", result)
        if publish:
            self.f.publish(run, "Draft evidence; the helper has not returned.\n")
        run.state = replace(run.state, text=text)
        run.call("wait_for_reply")
        self.fail("Successful external wait must yield")

    def test_public_wait_text_is_only_notification_then_real_returns_complete(self):
        self.begin()
        text = "Please clarify compatibility? I saved a draft and am checking the evidence."
        parent, _, _ = self.run_one(
            self.worker, lambda run: self.wait_with_child(run, text=text, publish=True))
        upstream = self.messages(self.worker, self.manager)
        self.assertEqual([e.payload["kind"] for e in upstream], ["notification"],
                         "Public final text and a new artifact must not queue parent progress while child work is active")
        notice, = upstream
        self.assertEqual(notice.payload["parent_assignment_seq"], parent.seq)
        self.assertIsNone(notice.payload["reply_to"])
        self.assertIn(text, notice.payload["text"])
        self.assertIn("result.txt", notice.payload["text"])
        public, = [e for e in self.f.events("msg.said") if e.actor.id == self.worker]
        self.assertEqual(public.payload["text"], text)
        self.assertNotIn(parent.seq, _reported_messages(self.messages()))
        self.assertEqual(self.rt.team.view(self.task)["pending_count"], 1)
        context = self.rt.team._context(self.task, parent, self.rt.team._role(self.manager))
        self.assertIn(notice.seq, [row["seq"] for row in context["notifications"]])
        self.assertEqual(self.f.events("team.objective_completed"), [])
        self.assertEqual(self.f.events("team.review_recorded"), [])

        def helper_return(run):
            run.call("send_message", recipient=self.worker,
                     reply_to=run.context["active_assignment"]["seq"], text="Evidence inspected and returned.")

        self.run_one(self.helper, helper_return, kind="assignment", status="completed")
        self.run_one(self.worker, self.f.return_assignment, kind="report", status="completed")
        self.run_one(self.manager, self.f.deliver, kind="report", status="completed")
        self.assertEqual(self.rt.team.view(self.task)["status"], "done")
        self.assertEqual(self.rt.team.view(self.task)["pending_count"], 0)
        self.assertIsNone(self.rt.team._claim(self.task))
        self.assertEqual(self.rt.team.recovery.assignments(self.task), [])
        self.assertEqual([e.payload["kind"] for e in self.messages(self.worker, self.manager)],
                         ["notification", "report"])
        self.assertEqual([run.context["message"]["kind"] for run in self.f.driver.runs
                          if run.context["you"]["id"] == self.manager], ["message", "report"])
        completed, = self.f.events("team.objective_completed")
        returned, = [e for e in self.messages(self.worker, self.manager) if e.payload["kind"] == "report"]
        self.assertLess(returned.seq, completed.seq)
        self.assertEqual(self.rt.team.external_runs.pending(self.task), [])

    def test_empty_wait_with_child_adds_no_upstream_event(self):
        self.begin()
        self.run_one(self.worker, self.wait_with_child)
        self.assertEqual(self.messages(self.worker, self.manager), [])
        self.assertEqual(self.rt.team.view(self.task)["pending_count"], 1)

    def test_publication_without_final_text_is_only_notification(self):
        self.begin()
        parent, _, _ = self.run_one(
            self.worker, lambda run: self.wait_with_child(run, publish=True))
        notice, = self.messages(self.worker, self.manager)
        self.assertEqual(notice.payload["kind"], "notification")
        self.assertEqual(notice.payload["parent_assignment_seq"], parent.seq)
        self.assertIn("result.txt", notice.payload["text"])
        artifact, = self.rt.task(self.task).deliverables
        self.assertIn(str(artifact["seq"]), notice.payload["text"])
        self.assertEqual(self.rt.team.view(self.task)["pending_count"], 1)
        self.assertNotIn(parent.seq, _reported_messages(self.messages()))

    def assert_external_problem_wakes_parent(self, status):
        self.begin()

        def author(run):
            result = run.call("delegate", recipient=self.helper, instruction="Inspect evidence.")
            self.assertEqual(result["status"], "ok")
            if status == "failed":
                raise RuntimeError("Offline scripted external failure")
            return "Waiting for evidence; this text does not formally return the assignment."

        parent, _, _ = self.run_one(self.worker, author, status=status)
        progress, = self.messages(self.worker, self.manager)
        self.assertEqual(progress.payload["kind"], "progress")
        self.assertEqual(progress.payload["reply_to"], parent.seq)
        self.assertNotIn(parent.seq, _reported_messages(self.messages()))
        self.assertEqual(self.f.events("team.objective_completed"), [])
        self.run_one(self.helper, lambda run: run.call("wait_for_reply"))
        self.run_one(self.manager, lambda run: run.call("wait_for_reply"), kind="progress")
        self.assertEqual(self.f.driver.runs[-1].context["message"]["seq"], progress.seq)
        self.assertEqual(self.f.driver.runs[-1].context["message"]["reply_to"], parent.seq)

    def test_external_failure_with_active_child_still_wakes_parent(self):
        self.assert_external_problem_wakes_parent("failed")

    def test_external_interruption_with_active_child_still_wakes_parent(self):
        self.assert_external_problem_wakes_parent("interrupted")

    def test_explicit_help_message_remains_actionable_with_active_child(self):
        self.begin()
        question = "Choose the compatibility target so I can proceed."

        def author(run):
            result = run.call("delegate", recipient=self.helper, instruction="Inspect evidence.")
            self.assertEqual(result["status"], "ok")
            sent = run.call("send_message", recipient=self.manager, text=question)
            self.assertEqual(sent["status"], "ok")
            run.state = replace(run.state, text=question)
            run.call("wait_for_reply")

        parent, _, _ = self.run_one(self.worker, author)
        addressed, = self.messages(self.worker, self.manager)
        self.assertEqual(addressed.payload["kind"], "message")
        self.assertEqual(addressed.payload["text"], question)
        self.assertTrue(addressed.payload["visible"])
        self.assertEqual(addressed.payload["parent_assignment_seq"], parent.seq)
        self.assertIsNone(addressed.payload["reply_to"])
        self.assertNotIn(parent.seq, _reported_messages(self.messages()))
        self.assertEqual(self.rt.team.view(self.task)["pending_count"], 2)
        self.run_one(self.helper, lambda run: run.call("wait_for_reply"))
        # A child without active subwork sends its own progress to the author.
        self.run_one(self.manager, lambda run: run.call("wait_for_reply"), kind="message")
        self.assertEqual(self.f.driver.runs[-1].context["message"]["seq"], addressed.seq)

    def test_review_notification_is_not_duplicated_and_approval_is_not_return(self):
        self.begin()

        def author(run):
            artifact = self.f.publish(run)
            run.state = replace(run.state, text="Draft saved; independent inspection is pending.")
            run.call("request_review", reviewer=self.helper, artifact_seqs=[artifact],
                     instruction="Inspect the exact saved body.")

        parent, _, _ = self.run_one(self.worker, author)
        notice, = self.messages(self.worker, self.manager)
        self.assertEqual(notice.payload["kind"], "notification")
        self.assertEqual(notice.payload["parent_assignment_seq"], parent.seq)
        self.assertEqual(self.rt.team_reviews.view(self.task)[0]["status"], "pending")
        self.assertEqual(self.f.events("team.review_recorded"), [])
        self.assertNotIn(parent.seq, _reported_messages(self.messages()))

        def approve(run):
            review, = run.context["reviews"]
            read = run.call("read_artifact", artifact_seq=review["artifacts"][0]["seq"])
            self.assertEqual(read["content"], "version one\n")
            run.call("record_review", review_seq=review["request_seq"], verdict="approved",
                     feedback="Exact saved body inspected by offline fixture.")

        self.run_one(self.helper, approve)
        self.assertEqual(len(self.messages(self.worker, self.manager)), 1)
        self.assertEqual(self.rt.team_reviews.view(self.task)[0]["status"], "approved")
        self.assertNotIn(parent.seq, _reported_messages(self.messages()))
        self.assertEqual(self.f.events("team.objective_completed"), [])
        self.run_one(self.worker, self.f.return_assignment, kind="report", status="completed")
        self.run_one(self.manager, self.f.deliver, kind="report", status="completed")
        self.assertEqual(self.rt.team.view(self.task)["status"], "done")
        approved, = self.f.events("team.review_recorded")
        returned, = [e for e in self.messages(self.worker, self.manager) if e.payload["kind"] == "report"]
        self.assertLess(approved.seq, returned.seq)


class WaitingChildProjectionTests(unittest.TestCase):
    def setUp(self):
        self.f = test_team_supervision.TeamSupervisionTests()
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()
        self.rt, self.task = self.f.rt, self.f.task
        self.manager, self.worker = self.f.manager, self.f.worker
        self.helper, self.other = [r.id for r in self.rt.worker_roles if r.id != self.worker][:2]

    def tearDown(self):
        self.assertTrue(self.rt.verify_chain().ok)
        self.assertEqual(self.f.protocol_errors, [])

    def send(self, sender, recipient, text="Ledger fixture input", kind="assignment", **kwargs):
        return self.rt.team._send(self.task, self.rt.team._role(sender), recipient, text, kind, **kwargs)

    def parent(self):
        return self.send(self.manager, self.worker)

    def child(self, parent, **kwargs):
        return self.send(self.worker, self.helper, parent_assignment_seq=parent.seq, **kwargs)

    def run_fact(self, message, status="running"):
        """Host ledger fixture, deliberately independent of scheduler execution."""
        started = self.rt.log.emit(type="team.run_started", task_id=self.task, actor=HOST, payload={
            "message_seq": message.seq, "role": message.payload["recipient"],
            "run_id": f"ledger-{message.seq}", "revision": self.rt.team.view(self.task)["revision"],
            "control_seq": self.rt.execution_control.view(self.task)["seq"]})
        if status != "running":
            self.rt.log.emit(type="team.run_finished", task_id=self.task, actor=HOST, payload={
                "message_seq": message.seq, "role": message.payload["recipient"],
                "run_id": started.payload["run_id"], "status": status, "reason": "Offline ledger fixture"})
        return started

    def settle(self, parent, *, message=None, final="Please clarify compatibility? Draft inspection continues.",
               status="waiting", reason="", started=None):
        message = message or parent
        started = started or self.run_fact(message)
        with self.rt.log.atomic():
            self.rt.team._finish_message(
                self.task, message, self.rt.team._role(self.worker), started,
                started.payload["revision"], parent, status=status, reason=reason,
                final=final, failure={}, awaiting_customer=False)
        finished, = [e for e in self.f.events("team.run_finished")
                     if e.payload["run_id"] == started.payload["run_id"]]
        self.assertEqual(finished.payload["status"], status)

    def upstream(self, parent):
        return [e for e in self.rt.team._messages(self.task) if e.actor.id == self.worker
                and e.payload["recipient"] == self.manager
                and (e.payload.get("reply_to") == parent.seq
                     or e.payload.get("parent_assignment_seq") == parent.seq)]

    def assert_upstream(self, parent, kind):
        events = self.upstream(parent)
        self.assertEqual([e.payload["kind"] for e in events], [kind])
        event, = events
        self.assertEqual(event.payload["reply_to"], parent.seq if kind == "progress" else None)
        self.assertEqual(event.payload["parent_assignment_seq"], parent.seq if kind == "notification" else None)
        self.assertNotIn(parent.seq, _reported_messages(self.rt.team._messages(self.task)))
        self.assertEqual(self.f.events("team.objective_completed"), [])
        return event

    def row(self, source):
        return next(row for row in self.rt.team.recovery.assignments(self.task) if row["seq"] == source.seq)

    def test_earlier_child_latest_continuation_controls_active_state(self):
        for state in ("queued", "running", "waiting"):
            with self.subTest(latest_state=state):
                parent = self.parent()
                child = self.child(parent)
                self.run_fact(child, "failed")
                continuation = self.send(self.worker, self.helper, kind="continuation",
                                         continue_assignment_seq=child.seq, parent_assignment_seq=parent.seq)
                if state != "queued":
                    self.run_fact(continuation, state)
                row = self.row(child)
                self.assertEqual((row["latest_message_seq"], row["status"]), (continuation.seq, state))
                self.assertTrue(row["requires_return"])
                # This parent turn starts after the old child AND its continuation.
                resumed = self.send(self.manager, self.worker, kind="continuation",
                                    continue_assignment_seq=parent.seq)
                started = self.run_fact(resumed)
                self.assertLess(child.seq, started.seq)
                self.assertLess(continuation.seq, started.seq)
                self.settle(parent, message=resumed, started=started)
                self.assert_upstream(parent, "progress" if state == "waiting" else "notification")

    def test_latest_failed_continuation_does_not_inherit_waiting_root(self):
        for state in ("failed", "interrupted"):
            with self.subTest(latest_state=state):
                parent = self.parent()
                child = self.child(parent)
                self.run_fact(child, "waiting")
                continuation = self.send(self.worker, self.helper, kind="continuation",
                                         continue_assignment_seq=child.seq, parent_assignment_seq=parent.seq)
                self.run_fact(continuation, state)
                self.assertEqual(self.row(child)["status"], state)
                self.settle(parent, final="Still waiting for the helper.")
                self.assert_upstream(parent, "progress")

    def test_failed_child_without_continuation_still_wakes_parent(self):
        parent = self.parent()
        child = self.child(parent)
        self.run_fact(child, "failed")
        self.assertEqual(self.row(child)["latest_message_seq"], child.seq)
        self.settle(parent, final="Waiting for evidence.")
        self.assert_upstream(parent, "progress")

    def test_parent_failure_and_interruption_wake_even_with_active_child(self):
        for status in ("failed", "interrupted"):
            with self.subTest(parent_status=status):
                parent = self.parent()
                child = self.child(parent)
                self.assertEqual(self.row(child)["status"], "queued")
                reason = f"Offline parent {status}; inspect preserved receipts."
                self.settle(parent, status=status, reason=reason)
                progress = self.assert_upstream(parent, "progress")
                self.assertEqual(progress.payload["text"], reason)

    def test_no_active_child_still_emits_progress_with_waiting_words(self):
        parent = self.parent()
        self.settle(parent, final="Waiting for child work; please continue later.")
        self.assert_upstream(parent, "progress")

    def test_returned_child_and_late_continuation_cannot_silence_parent(self):
        parent = self.parent()
        child = self.child(parent)
        self.send(self.helper, self.worker, "Formally returned", "report", reply_to=child.seq)
        # A durable late continuation cannot reopen a formally returned root.
        self.send(self.worker, self.helper, kind="continuation", continue_assignment_seq=child.seq,
                  parent_assignment_seq=parent.seq)
        self.assertNotIn(child.seq, [row["seq"] for row in self.rt.team.recovery.assignments(self.task)])
        self.settle(parent)
        self.assert_upstream(parent, "progress")

    def test_withdrawn_child_does_not_silence_parent(self):
        parent = self.parent()
        child = self.child(parent)
        started = self.run_fact(parent)
        self.f.driver.steps[self.worker] = [self.f.native_turn("", ("withdraw_work", {
            "message_seq": child.seq, "reason": "Evidence scope removed"}), ("wait_for_reply", {}))]
        list(self.rt.team._run_message(self.task, parent, self.rt.team._role(self.worker), started))
        result, = self.f.results("withdraw_work")
        self.assertEqual((result["status"], result["withdrawn"]), ("ok", True))
        self.assertIn(child.seq, self.rt.team._inactive_messages(self.task))
        self.assertNotIn(child.seq, [row["seq"] for row in self.rt.team.recovery.assignments(self.task)])
        self.assert_upstream(parent, "progress")

    def test_stale_child_record_cannot_supply_current_dependency(self):
        revision = self.rt.log.emit(type="team.objective_updated", task_id=self.task, actor=HOST,
                                    payload={"goal": "Current fixture objective"}).seq
        parent = self.parent()
        # Deliberately inconsistent old-target record, not a normal _send result.
        stale = self.rt.log.emit(type="team.message_received", task_id=self.task,
            actor=Actor.agent(self.worker, self.rt.team._role(self.worker).role), payload={
                "recipient": self.helper, "text": "Old target", "kind": "assignment",
                "reply_to": None, "parent_assignment_seq": parent.seq, "revision": 0})
        self.assertEqual(parent.payload["revision"], revision)
        self.assertNotIn(stale.seq, [row["seq"] for row in self.rt.team.recovery.assignments(self.task)])
        self.settle(parent)
        self.assert_upstream(parent, "progress")

    def test_other_parent_and_other_sender_work_cannot_silence_parent(self):
        parent = self.parent()
        unrelated = self.parent()
        self.child(unrelated)
        self.send(self.other, self.helper, parent_assignment_seq=parent.seq)
        self.send(self.worker, self.helper, kind="notification", parent_assignment_seq=parent.seq)
        self.settle(parent)
        self.assert_upstream(parent, "progress")

    def test_full_projection_finds_old_active_child_before_many_closed_children(self):
        parent = self.parent()
        active = self.child(parent)
        for index in range(21):
            closed = self.child(parent, text=f"Already returned child {index}")
            self.send(self.helper, self.worker, "Returned", "report", reply_to=closed.seq)
        # Also put enough unrelated open work after it to defeat a last-20 projection.
        for index in range(21):
            self.send(self.manager, self.other, text=f"Other open work {index}")
        self.assertNotIn(active.seq, [row["seq"] for row in self.rt.team.recovery.assignments(self.task)[-20:]])
        self.settle(parent)
        self.assert_upstream(parent, "notification")

    def test_empty_resumed_wait_with_old_active_child_adds_no_event(self):
        parent = self.parent()
        child = self.child(parent)
        self.run_fact(child, "running")
        resumed = self.send(self.manager, self.worker, kind="continuation", continue_assignment_seq=parent.seq)
        self.settle(parent, message=resumed, final="")
        self.assertEqual(self.upstream(parent), [])

    def test_old_waiting_child_clarification_then_author_wait_still_wakes_manager(self):
        parent = self.parent()

        def run_wait(role, turn, *, source_seq):
            claimed = self.rt.team._claim(self.task)
            self.assertIsNotNone(claimed)
            self.assertEqual((claimed[0].seq, claimed[1].id), (source_seq, role))
            self.f.driver.steps[role] = [turn]
            list(self.rt.team._run_message(self.task, *claimed))
            self.assertEqual(self.f.driver.steps[role], [])
            finished, = [e for e in self.f.events("team.run_finished")
                         if e.payload["run_id"] == claimed[2].payload["run_id"]]
            self.assertEqual(finished.payload["status"], "waiting")
            return claimed

        run_wait(self.worker, self.f.native_turn("", ("delegate", {
            "recipient": self.helper, "instruction": "Inspect and return the evidence"}),
            ("wait_for_reply", {})), source_seq=parent.seq)
        child, = [e for e in self.rt.team._messages(self.task)
                  if e.actor.id == self.worker and e.payload["kind"] == "assignment"]
        self.assertEqual(self.upstream(parent), [])
        question = "Please clarify the evidence scope before I can continue."
        run_wait(self.helper, self.f.native_turn(question, ("wait_for_reply", {})), source_seq=child.seq)
        clarification, = [e for e in self.rt.team._messages(self.task)
                          if e.actor.id == self.helper and e.payload["kind"] == "progress"]
        self.assertEqual((clarification.payload["text"], clarification.payload["reply_to"]),
                         (question, child.seq))
        resumed = run_wait(self.worker, self.f.native_turn("", ("wait_for_reply", {})),
                           source_seq=clarification.seq)
        row = self.row(child)
        self.assertEqual((row["status"], row["latest_message_seq"]), ("waiting", child.seq))
        self.assertLess(row["latest_message_seq"], resumed[2].seq)
        self.assertFalse([e for e in self.rt.team._messages(self.task)
                          if e.actor.id == self.worker and e.seq > resumed[2].seq
                          and e.payload["recipient"] == self.helper])
        progress = self.assert_upstream(parent, "progress")
        self.assertEqual(self.f.driver.calls[-1]["context"]["active_assignment"]["seq"], parent.seq)
        self.assertNotIn(child.seq, _reported_messages(self.rt.team._messages(self.task)))
        run_wait(self.manager, self.f.native_turn("", ("wait_for_reply", {})), source_seq=progress.seq)
        self.assertEqual(self.f.driver.calls[-1]["context"]["message"]["kind"], "progress")
        self.assertEqual(self.rt.team.view(self.task)["pending_count"], 0)
        self.assertIsNone(self.rt.team._claim(self.task))

    def test_plain_message_parent_without_publication_does_not_gain_notification(self):
        parent = self.send(self.manager, self.worker, kind="message")
        self.child(parent)
        self.settle(parent)
        self.assertEqual(self.upstream(parent), [],
                         "Only an automatic progress candidate may be converted to notification")


if __name__ == "__main__":
    unittest.main()
