"""Queued child returns, using scripted native turns and disposable ledgers.

Malformed/historical message cases seed run facts explicitly; they do not claim
that the native dispatcher produced those records. No real model is invoked.
"""

import unittest
from unittest.mock import patch

from cater.events import Actor
from cater.team import HOST, _reported_messages
import test_team_waiting_boundaries as boundaries


class QueuedChildReturnTests(unittest.TestCase):
    def setUp(self):
        self.b = boundaries.WaitingBoundaryTests()
        self.addCleanup(self.b.doCleanups)
        self.b.setUp()
        self.f, self.rt, self.task = self.b.f, self.b.rt, self.b.task
        self.manager, self.worker = self.b.manager, self.b.worker
        self.helper, self.observer = self.b.helper, self.b.observer

    def tearDown(self):
        self.b.tearDown()
        self.assertEqual(self.f.protocol_errors, [])

    def upstream(self, after_seq=0):
        return [e for e in self.b.messages() if e.seq > after_seq
                and e.actor.id == self.worker and e.payload["recipient"] == self.manager]

    def publish(self, content="First version\n"):
        return ("publish_artifact", {"filename": "queued-result.txt", "content": content,
                                    "summary": "Offline fixture", "format": "text"})

    def finish(self, claimed, *, final="", status="waiting", awaiting_customer=False):
        message, role, started = claimed
        with self.rt.log.atomic():
            self.rt.team._finish_message(
                self.task, message, role, started, started.payload["revision"],
                self.rt.team._waiting_assignment(self.task, message, role),
                status=status, reason="Fixture interruption" if status != "waiting" else "",
                final=final, failure={}, awaiting_customer=awaiting_customer)

    def queued_child(self, last_turn=None):
        claimed = self.b.start()
        self.f.driver.steps[self.worker] = [
            self.f.native_turn("", self.b.delegate()),
            self.b.wait() if last_turn is None else last_turn,
        ]
        owner = self.rt.team._run_message(self.task, *claimed)
        self.addCleanup(owner.close)
        next(owner)
        child = self.b.child(claimed[0])
        helper = self.rt.team._claim(self.task)
        self.assertEqual(helper[0].seq, child.seq)
        self.b.execute(helper, self.f.native_turn("Checked child evidence."), status="completed")
        report, = [e for e in self.b.messages() if e.payload["kind"] == "report"]
        self.assertEqual(report.payload["reply_to"], child.seq)
        return claimed, owner, child, report

    def seed_waiting(self, source):
        payload = {"message_seq": source.seq, "role": source.payload["recipient"],
                   "run_id": f"queued-ledger-{source.seq}",
                   "revision": source.payload["revision"],
                   "control_seq": self.rt.execution_control.view(self.task)["seq"]}
        self.rt.log.emit(type="team.run_started", task_id=self.task, actor=HOST, payload=payload)
        self.rt.log.emit(type="team.run_finished", task_id=self.task, actor=HOST,
                         payload={**payload, "status": "waiting", "reason": "Ledger fixture"})

    def ledger_return(self, variant):
        # An older waiting child cannot independently satisfy waiting_on_child.
        revision = self.rt.log.emit(type="team.objective_updated", actor=HOST,
                                   task_id=self.task, payload={"goal": "Current fixture goal"}).seq
        parent = self.b.send(self.manager, self.worker)
        other = self.b.send(self.manager, self.worker) if variant == "other_parent" else parent
        if other != parent:
            self.seed_waiting(other)
        child_payload = {"recipient": self.helper, "text": "Historical child", "kind": "assignment",
                         "reply_to": None, "revision": 0 if variant == "old_child" else revision,
                         "parent_assignment_seq": other.seq}
        if variant == "wrong_review":
            child_payload["review_seq"] = 12345
        child = self.rt.log.emit(type="team.message_received", task_id=self.task,
            actor=Actor.agent(self.worker, self.rt.team._role(self.worker).role), payload=child_payload)
        self.seed_waiting(child)
        claimed = self.rt.team._claim(self.task)
        self.assertEqual(claimed[0].seq, parent.seq)
        sender = self.observer if variant == "wrong_sender" else self.helper
        payload = {"recipient": self.observer if variant == "wrong_recipient" else self.worker,
                   "text": "Ledger child feedback", "kind": "report", "reply_to": child.seq,
                   "revision": 0 if variant in {"old_child", "old_report"} else revision,
                   "parent_assignment_seq": None}
        if variant == "wrong_review":
            payload["review_seq"] = 12346
        report = self.rt.log.emit(type="team.message_received", task_id=self.task,
            actor=Actor.agent(sender, self.rt.team._role(sender).role), payload=payload)
        return claimed, child, report

    def assert_invalid_report_escalates(self, variant):
        claimed, _, _ = self.ledger_return(variant)
        self.finish(claimed)
        progress, = self.upstream()
        self.assertEqual((progress.payload["kind"], progress.payload["reply_to"]),
                         ("progress", claimed[0].seq))
        self.assertEqual(self.f.driver.calls, [])

    def test_public_wait_text_becomes_notification_and_report_stays_claimable(self):
        text = "Child evidence is ready for integration."
        claimed, owner, _, report = self.queued_child(self.b.wait(text))
        list(owner)
        notice, = self.upstream()
        self.assertEqual((notice.payload["kind"], notice.payload["text"]), ("notification", text))
        self.assertEqual(notice.payload["parent_assignment_seq"], claimed[0].seq)
        self.assertIsNone(notice.payload["reply_to"])
        self.b.assert_pending(report)
        self.assertEqual(self.rt.team._claim(self.task)[0].seq, report.seq)

    def test_new_artifact_becomes_notification_without_empty_upstream_wakeup(self):
        claimed, owner, _, report = self.queued_child(self.b.wait("", self.publish()))
        list(owner)
        artifact, = self.rt.task(self.task).deliverables
        notice, = self.upstream()
        self.assertEqual(notice.payload["kind"], "notification")
        self.assertEqual(notice.payload["parent_assignment_seq"], claimed[0].seq)
        self.assertIn(artifact["filename"], notice.payload["text"])
        self.assertIn(str(artifact["seq"]), notice.payload["text"])
        self.assertEqual(self.f.results("publish_artifact")[0]["status"], "ok")
        self.b.assert_pending(report)

    def test_explicit_question_remains_actionable_beside_queued_report(self):
        question = "Which compatibility target should I use?"
        claimed, owner, _, report = self.queued_child(self.b.wait(question, ("send_message", {
            "recipient": self.manager, "text": question})))
        list(owner)
        addressed, = self.upstream()
        self.assertEqual((addressed.payload["kind"], addressed.payload["text"]), ("message", question))
        self.assertTrue(addressed.payload["visible"])
        self.assertEqual(addressed.payload["parent_assignment_seq"], claimed[0].seq)
        self.b.assert_pending(report, addressed)
        self.assertEqual(self.rt.team._claim(self.task)[0].seq, report.seq)
        manager = self.rt.team._claim(self.task)
        self.assertEqual((manager[0].seq, manager[1].id), (addressed.seq, self.manager))
        self.b.execute(manager, self.b.wait())

    def test_current_claimed_report_does_not_silence_another_empty_wait(self):
        claimed, owner, _, report = self.queued_child()
        list(owner)
        self.assertEqual(self.upstream(), [])
        resumed = self.rt.team._claim(self.task)
        self.assertEqual(resumed[0].seq, report.seq)
        self.b.execute(resumed, self.b.wait())
        progress, = self.upstream()
        self.assertEqual((progress.payload["kind"], progress.payload["reply_to"]),
                         ("progress", claimed[0].seq))
        self.b.assert_pending(progress)

    def test_late_child_continuation_does_not_reopen_work_or_hide_consumed_return(self):
        claimed, child, report = self.ledger_return("valid")
        late = self.b.send(self.worker, self.helper, kind="continuation",
                           continue_assignment_seq=child.seq, parent_assignment_seq=claimed[0].seq)
        self.assertIn(late.seq, _reported_messages(self.b.messages()))
        self.assertNotIn(child.seq, {r["seq"] for r in self.rt.team.recovery.assignments(self.task)})
        self.finish(claimed, final="Integrating returned evidence.")
        notice, = self.upstream()
        self.assertEqual(notice.payload["kind"], "notification")
        self.b.assert_pending(report)
        resumed = self.rt.team._claim(self.task)
        self.assertEqual(resumed[0].seq, report.seq)
        self.b.execute(resumed, self.b.wait())
        progress, = self.upstream(notice.seq)
        self.assertEqual((progress.payload["kind"], progress.payload["reply_to"]),
                         ("progress", claimed[0].seq))
        self.b.assert_pending(progress)

    def test_customer_question_remains_pending_beside_queued_child_report(self):
        proposal = {"title": "Compatibility target", "questions": [{
            "id": "target", "question": "Which target should I support?",
            "selection": "single", "options": []}]}
        claimed, owner, _, report = self.queued_child(
            self.f.native_turn("", ("request_questions", proposal)))
        list(owner)
        question, = self.rt.questionnaires.view(self.task)
        self.assertEqual((question["status"], question["role_id"]), ("pending", self.worker))
        self.assertEqual(question["questions"], proposal["questions"])
        requested, = self.f.events("team.questions_requested")
        self.assertEqual(requested.payload["assignment_seq"], claimed[0].seq)
        self.assertEqual(self.f.results("request_questions")[0]["status"], "ok")
        self.assertEqual(self.upstream(), [])
        self.b.assert_pending(report)

    def test_older_unclaimed_report_is_still_a_continuation(self):
        claimed, child, report = self.ledger_return("valid")
        self.rt.log.emit(type="team.run_finished", actor=HOST, task_id=self.task,
                         payload={**claimed[2].payload, "status": "waiting", "reason": "Ledger fixture"})
        continued = self.b.send(self.manager, self.worker, kind="continuation",
                                continue_assignment_seq=claimed[0].seq)
        # Isolate a fresh run while an older return remains queued.
        start = self.rt.log.emit(type="team.run_started", actor=HOST, task_id=self.task, payload={
            "message_seq": continued.seq, "role": self.worker, "run_id": "older-queued-return",
            "revision": continued.payload["revision"],
            "control_seq": self.rt.execution_control.view(self.task)["seq"]})
        self.assertLess(report.seq, start.seq)
        self.finish((continued, claimed[1], start))
        self.assertEqual(self.upstream(), [])
        self.assertIn(child.seq, _reported_messages(self.b.messages()))

    def test_old_child_revision_cannot_silence_parent(self):
        self.assert_invalid_report_escalates("old_child")

    def test_old_report_revision_cannot_silence_parent(self):
        self.assert_invalid_report_escalates("old_report")

    def test_other_parent_report_cannot_silence_parent(self):
        self.assert_invalid_report_escalates("other_parent")

    def test_wrong_report_sender_cannot_silence_parent(self):
        self.assert_invalid_report_escalates("wrong_sender")

    def test_wrong_report_recipient_cannot_silence_parent(self):
        self.assert_invalid_report_escalates("wrong_recipient")

    def test_mismatched_review_id_cannot_silence_parent(self):
        self.assert_invalid_report_escalates("wrong_review")

    def test_interruption_still_escalates_with_queued_child_return(self):
        claimed, _, report = self.ledger_return("valid")
        self.finish(claimed, status="interrupted")
        progress, = self.upstream()
        self.assertEqual(progress.payload["kind"], "progress")
        self.assertEqual(progress.payload["text"], "Fixture interruption")
        self.assertNotIn(report.seq, {e.payload["message_seq"] for e in self.f.events("team.run_started")})

    def test_failure_still_escalates_with_queued_child_return(self):
        claimed, _, _ = self.ledger_return("valid")
        self.finish(claimed, status="failed")
        progress, = self.upstream()
        self.assertEqual((progress.payload["kind"], progress.payload["reply_to"]),
                         ("progress", claimed[0].seq))

    def test_notification_failure_rolls_back_run_finish_but_keeps_child_return(self):
        claimed, _, report = self.ledger_return("valid")
        before = self.rt.log.read(task_id=self.task)
        original = self.rt.log.emit

        def fail_notification(**kwargs):
            if kwargs["type"] == "team.message_received" and kwargs["payload"]["kind"] == "notification":
                self.assertTrue(self.rt.log.db.in_transaction)
                original(**kwargs)
                raise OSError("Injected notification settlement failure")
            return original(**kwargs)

        with patch.object(self.rt.log, "emit", side_effect=fail_notification):
            with self.assertRaisesRegex(OSError, "Injected notification"):
                self.finish(claimed, final="Preserved progress")
        self.assertEqual(self.rt.log.read(task_id=self.task), before)
        self.assertIn(report, self.b.messages())
        self.finish(claimed, final="Preserved progress")
        finishes = [e for e in self.f.events("team.run_finished")
                    if e.payload["run_id"] == claimed[2].payload["run_id"]]
        self.assertEqual(len(finishes), 1)
        self.assertEqual([e.payload["kind"] for e in self.upstream()], ["notification"])

    def queued_review(self, verdict):
        original = self.b.start()

        def request(context, history, tools):
            artifact, = self.rt.task(self.task).deliverables
            return self.f.native_turn("", ("request_review", {
                "reviewer": self.helper, "artifact_seqs": [artifact["seq"]],
                "instruction": "Inspect this exact fixture version."}))

        self.b.execute(original, self.f.native_turn("", self.publish()), request)
        review, = self.rt.team_reviews.view(self.task)
        reviewer = self.rt.team._claim(self.task)
        self.assertEqual(reviewer[0].seq, review["message_seq"])
        continuation = self.b.send(self.manager, self.worker, kind="continuation",
                                   continue_assignment_seq=original[0].seq)
        owner = self.rt.team._claim(self.task)
        self.assertEqual(owner[0].seq, continuation.seq)
        self.b.execute(reviewer, self.f.native_turn("", ("record_review", {
            "review_seq": review["request_seq"], "verdict": verdict,
            "feedback": "Offline exact-version review feedback."})))
        report, = [e for e in self.b.messages() if e.payload["kind"] == "report"]
        self.assertEqual(report.payload["review_seq"], review["request_seq"])
        return original[0], owner, report

    def assert_review_resumes_owner(self, verdict):
        parent, owner, report = self.queued_review(verdict)
        self.b.execute(owner, self.b.wait())
        self.assertEqual(self.upstream(owner[2].seq), [])
        self.b.assert_pending(report)
        resumed = self.rt.team._claim(self.task)
        self.assertEqual(resumed[0].seq, report.seq)
        self.assertEqual(self.rt.team._waiting_assignment(self.task, report, resumed[1]).seq, parent.seq)
        self.b.execute(resumed, self.b.wait())
        progress, = self.upstream(owner[2].seq)
        self.assertEqual(progress.payload["kind"], "progress")
        self.assertEqual(self.rt.team_reviews.view(self.task)[0]["status"], verdict)

    def test_approved_review_report_resumes_owner_without_empty_upstream_wakeup(self):
        self.assert_review_resumes_owner("approved")

    def test_changes_requested_report_resumes_owner_without_empty_upstream_wakeup(self):
        self.assert_review_resumes_owner("changes_requested")

    def test_outdated_review_report_cannot_replace_upstream_escalation(self):
        parent, owner, report = self.queued_review("approved")
        self.b.execute(owner, self.b.wait("", self.publish("Second version\n")))
        self.assertEqual(self.rt.team_reviews.view(self.task)[0]["status"], "outdated")
        stale = self.rt.team._claim(self.task)
        self.assertEqual(stale[0].seq, report.seq)
        calls = len(self.f.driver.calls)
        self.b.execute(stale, status="interrupted")
        self.assertEqual(len(self.f.driver.calls), calls, "Outdated review must not invoke a model")
        self.assertNotIn(parent.seq, _reported_messages(self.b.messages()))
        progress, = self.upstream(owner[2].seq)
        self.assertEqual(progress.payload["kind"], "progress",
                         "An unusable queued review must not silently strand its parent")
        self.b.assert_pending(progress)


if __name__ == "__main__":
    unittest.main()
