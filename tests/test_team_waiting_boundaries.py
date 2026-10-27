"""Waiting boundaries with scripted native turns and append-only fixture ledgers.

Incoming assignments are seeded through _send; native cases use real _claim and
_run_message. The explicitly named finish fixtures isolate mismatched durable
records, without claiming those records were produced by a normal tool call.
"""

from __future__ import annotations

import unittest

from cater.events import Actor
from cater.team import HOST, _reported_messages
import test_team_supervision


class WaitingBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.f = test_team_supervision.TeamSupervisionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.rt, self.task = self.f.rt, self.f.task
        self.manager, self.worker = self.f.manager, self.f.worker
        self.helper, self.observer = [r.id for r in self.rt.worker_roles if r.id != self.worker][:2]
        self.prefix = self.rt.log.read(task_id=self.task)

    def tearDown(self):
        self.assertEqual(self.rt.log.read(task_id=self.task)[:len(self.prefix)], self.prefix)
        self.assertTrue(self.rt.verify_chain().ok)

    def messages(self):
        return self.rt.team._messages(self.task)

    def send(self, sender, recipient, text="Boundary fixture work", kind="assignment", **kwargs):
        return self.rt.team._send(self.task, self.rt.team._role(sender), recipient, text, kind, **kwargs)

    def start(self):
        source = self.send(self.manager, self.worker)
        claimed = self.rt.team._claim(self.task)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed[0].seq, source.seq)
        self.assertEqual(claimed[1].id, self.worker)
        self.assertEqual(claimed[2].payload["message_seq"], source.seq)
        self.assertGreater(claimed[2].seq, source.seq)
        return claimed

    def execute(self, claimed, *turns, status="waiting", protocol_errors=()):
        message, role, started = claimed
        self.f.driver.steps[role.id] = list(turns)
        list(self.rt.team._run_message(self.task, *claimed))
        self.assertEqual(self.f.driver.steps[role.id], [], "The scripted native path was not fully exercised")
        self.assertEqual(self.f.protocol_errors, list(protocol_errors))
        finished, = [e for e in self.f.events("team.run_finished")
                     if e.payload["run_id"] == started.payload["run_id"]]
        self.assertEqual(finished.payload["message_seq"], message.seq)
        self.assertEqual(finished.payload["status"], status, finished.payload)
        return finished

    def finish_fixture(self, claimed):
        """Direct settlement fixture, not a scripted model/tool delivery."""
        message, role, started = claimed
        with self.rt.log.atomic():
            self.rt.team._finish_message(
                self.task, message, role, started, started.payload["revision"], message,
                status="waiting", reason="", final="", failure={}, awaiting_customer=False)
        self.assertEqual(self.f.driver.calls, [])
        finished, = self.f.events("team.run_finished")
        self.assertEqual(finished.payload["message_seq"], message.seq)
        self.assertEqual(finished.payload["status"], "waiting")

    def assert_open(self, original):
        self.assertNotIn(original.seq, _reported_messages(self.messages()))
        context = self.rt.team._context(self.task, original, self.rt.team._role(self.worker))
        self.assertEqual(context["active_assignment"]["seq"], original.seq)
        self.assertIn(original.seq, [row["seq"] for row in context["open_assignments"]])
        self.assertEqual(self.rt.team.view(self.task)["status"], "running")

    def assert_pending(self, *events):
        started = {e.payload["message_seq"] for e in self.f.events("team.run_started")}
        reported = _reported_messages(self.messages())
        actual = {e.seq for e in self.messages() if e.payload["kind"] != "notification"
                  and e.seq not in started and e.seq not in reported}
        self.assertEqual(actual, {e.seq for e in events})
        self.assertEqual(self.rt.team.view(self.task)["pending_count"], len(events))

    def upstream(self, original, *, kind="progress"):
        event, = [e for e in self.messages() if e.actor.id == self.worker
                  and e.payload["recipient"] == self.manager and e.payload["kind"] == kind]
        self.assertEqual(event.payload["reply_to"], None if kind == "notification" else original.seq)
        self.assertEqual(event.payload["parent_assignment_seq"], original.seq if kind == "notification" else None)
        self.assertEqual(event.payload["revision"], original.payload["revision"])
        self.assertGreater(event.seq, original.seq)
        self.assert_open(original)
        return event

    def child(self, original):
        child, = [e for e in self.messages() if e.actor.id == self.worker
                  and e.payload["recipient"] == self.helper and e.payload["kind"] == "assignment"]
        self.assertEqual(child.payload["parent_assignment_seq"], original.seq)
        self.assertEqual(child.payload["revision"], original.payload["revision"])
        receipt, = self.f.results("delegate")
        self.assertEqual((receipt["status"], receipt["message_seq"]), ("ok", child.seq))
        return child

    def wait(self, text="", *calls):
        return self.f.native_turn(text, *calls, ("wait_for_reply", {}))

    def delegate(self):
        return ("delegate", {"recipient": self.helper, "instruction": "Return checked evidence"})

    def test_native_empty_wait_without_child_still_escalates_and_is_consumable(self):
        claimed = self.start()
        self.execute(claimed, self.wait())
        progress = self.upstream(claimed[0])
        self.assertTrue(progress.payload["text"].strip())
        self.assert_pending(progress)
        parent_run = self.rt.team._claim(self.task)
        self.assertEqual((parent_run[0].seq, parent_run[1].id), (progress.seq, self.manager))
        self.execute(parent_run, self.wait())
        self.assertEqual(self.f.driver.calls[-1]["context"]["message"]["kind"], "progress")
        self.assertEqual(self.f.driver.calls[-1]["context"]["message"]["reply_to"], claimed[0].seq)
        self.assert_pending()
        self.assert_open(claimed[0])

    def test_native_publication_is_notified_while_child_is_pending(self):
        claimed = self.start()
        self.execute(claimed, self.wait("", self.delegate(), ("publish_artifact", {
            "filename": "boundary.txt", "content": "Published boundary evidence\n",
            "summary": "Scripted fixture only", "format": "text"})))
        child = self.child(claimed[0])
        artifact, = self.rt.task(self.task).deliverables
        self.assertGreater(artifact["seq"], claimed[2].seq)
        self.assertEqual(artifact["actor"], self.worker)
        self.assertEqual(self.f.results("publish_artifact")[0]["status"], "ok")
        notice = self.upstream(claimed[0], kind="notification")
        self.assertIn("boundary.txt", notice.payload["text"])
        self.assertIn(str(artifact["seq"]), notice.payload["text"])
        self.assertNotIn(child.seq, _reported_messages(self.messages()))
        self.assert_pending(child)

    def test_native_explicit_question_survives_with_pending_child(self):
        claimed = self.start()
        question = "Which compatibility target should I use before finishing?"
        self.execute(claimed, self.wait(question, self.delegate(), ("send_message", {
            "recipient": self.manager, "text": question})))
        child = self.child(claimed[0])
        addressed, = [e for e in self.messages() if e.actor.id == self.worker
                      and e.payload["recipient"] == self.manager]
        self.assertEqual(addressed.payload["kind"], "message")
        self.assertEqual(addressed.payload["text"], question)
        self.assertEqual(addressed.payload["parent_assignment_seq"], claimed[0].seq)
        self.assertIsNone(addressed.payload["reply_to"])
        self.assertTrue(addressed.payload["visible"])
        self.assert_pending(child, addressed)
        self.assert_open(claimed[0])
        said, = [e for e in self.f.events("msg.said") if e.actor.id == self.worker]
        self.assertEqual((said.payload["text"], said.payload["reply_to"]), (question, claimed[0].seq))

    def test_native_explicit_send_message_is_preserved_when_empty_echo_is_suppressed(self):
        claimed = self.start()
        question = "Please choose the supported compatibility target."
        self.execute(claimed, self.wait("", self.delegate(), ("send_message", {
            "recipient": self.manager, "text": question})))
        child = self.child(claimed[0])
        addressed, = [e for e in self.messages() if e.actor.id == self.worker
                      and e.payload["recipient"] == self.manager]
        self.assertEqual(addressed.payload["kind"], "message")
        self.assertEqual(addressed.payload["text"], question)
        self.assertEqual(addressed.payload["parent_assignment_seq"], claimed[0].seq)
        self.assertIsNone(addressed.payload["reply_to"])
        self.assertTrue(addressed.payload["visible"])
        self.assertEqual(self.f.results("send_message")[0]["message_seq"], addressed.seq)
        self.assert_open(claimed[0])
        self.assert_pending(child, addressed)

    def test_native_model_failure_after_delegation_still_escalates(self):
        claimed = self.start()
        # Intentional fake-driver exception exercises the real runtime failure branch.
        finished = self.execute(claimed, self.f.native_turn("", self.delegate()),
            RuntimeError("scripted boundary failure"), status="failed",
            protocol_errors=("RuntimeError: scripted boundary failure",))
        child = self.child(claimed[0])
        self.assertEqual(finished.payload["error_type"], "RuntimeError")
        progress = self.upstream(claimed[0])
        self.assertEqual(progress.payload["text"], finished.payload["reason"])
        self.assertNotIn(child.seq, _reported_messages(self.messages()))
        self.assert_pending(child, progress)

    def test_native_pause_interrupts_and_escalates_then_new_customer_takes_priority(self):
        claimed = self.start()

        def pause_during_model(context, history, tools):
            self.rt.execution_control.change(self.task, "pause", request_id="waiting-boundary-pause")
            return self.f.native_turn("")

        finished = self.execute(claimed, self.f.native_turn("", self.delegate()),
                                pause_during_model, status="interrupted")
        child = self.child(claimed[0])
        progress = self.upstream(claimed[0])
        self.assertEqual(progress.payload["text"], finished.payload["reason"])
        self.assertEqual(self.rt.execution_control.view(self.task)["state"], "paused")
        self.assert_pending(child, progress)
        self.assertIsNone(self.rt.team._claim(self.task))
        customer = self.rt.team.receive(self.task, "Inspect the latest direction first",
                                        request_id="waiting-boundary-customer")
        customer_run = self.rt.team._claim(self.task)
        self.assertEqual((customer_run[0].seq, customer_run[1].id), (customer.seq, self.manager))
        self.execute(customer_run, self.wait())
        self.assert_pending(child, progress)
        self.rt.execution_control.change(self.task, "resume", request_id="waiting-boundary-resume")
        newer = self.rt.team.receive(self.task, "Check this before resuming delegated work",
                                     request_id="waiting-boundary-newer")
        newer_run = self.rt.team._claim(self.task)
        self.assertEqual((newer_run[0].seq, newer_run[1].id), (newer.seq, self.manager))
        self.execute(newer_run, self.wait())
        resumed = self.rt.team._claim(self.task)
        self.assertEqual((resumed[0].seq, resumed[1].id), (child.seq, self.helper))
        self.execute(resumed, self.wait())
        self.assert_open(claimed[0])
        self.assertNotIn(child.seq, _reported_messages(self.messages()))

    def test_finish_fixture_unrelated_parent_cannot_supply_child_dependency(self):
        claimed = self.start()
        unrelated = self.send(self.manager, self.worker, "A different parent assignment")
        child = self.send(self.worker, self.helper, parent_assignment_seq=unrelated.seq)
        self.assertGreater(child.seq, claimed[2].seq)
        self.assertNotEqual(child.payload["parent_assignment_seq"], claimed[0].seq)
        self.finish_fixture(claimed)
        progress = self.upstream(claimed[0])
        self.assert_pending(unrelated, child, progress)

    def test_finish_fixture_other_sender_cannot_supply_child_dependency(self):
        claimed = self.start()
        child = self.send(self.helper, self.observer, parent_assignment_seq=claimed[0].seq)
        self.assertGreater(child.seq, claimed[2].seq)
        self.assertEqual(child.payload["parent_assignment_seq"], claimed[0].seq)
        self.assertNotEqual(child.actor.id, self.worker)
        self.finish_fixture(claimed)
        progress = self.upstream(claimed[0])
        self.assert_pending(child, progress)

    def test_finish_fixture_old_revision_cannot_supply_child_dependency(self):
        revision = self.rt.log.emit(type="team.objective_updated", task_id=self.task, actor=HOST,
                                    payload={"goal": "Current boundary goal"}).seq
        claimed = self.start()
        # Deliberately inconsistent historical record isolates the revision guard.
        # _send always uses the current revision, so this is NOT normal tool output.
        child = self.rt.log.emit(type="team.message_received", task_id=self.task,
            actor=Actor.agent(self.worker, self.rt.team._role(self.worker).role), payload={
                "recipient": self.helper, "text": "Injected stale target record", "kind": "assignment",
                "reply_to": None, "parent_assignment_seq": claimed[0].seq, "revision": 0})
        self.assertEqual(claimed[0].payload["revision"], revision)
        self.assertGreater(child.seq, claimed[2].seq)
        self.assertNotEqual(child.payload["revision"], revision)
        self.finish_fixture(claimed)
        progress = self.upstream(claimed[0])
        self.assert_pending(child, progress)

    def test_finish_fixture_notification_to_helper_is_not_child_work(self):
        claimed = self.start()
        notice = self.send(self.worker, self.helper, "FYI only", "notification",
                           parent_assignment_seq=claimed[0].seq)
        self.finish_fixture(claimed)
        progress = self.upstream(claimed[0])
        self.assertEqual(notice.payload["kind"], "notification")
        self.assert_pending(progress)

    def test_native_failed_delegate_receipt_does_not_silence_later_empty_wait(self):
        claimed = self.start()
        self.execute(claimed, self.wait("", ("delegate", {
            "recipient": self.worker, "instruction": "Invalid self-delegation"})), self.wait())
        result, = self.f.results("delegate")
        self.assertEqual(result["status"], "error")
        self.assertNotIn("message_seq", result)
        self.assertEqual([e.seq for e in self.messages() if e.payload["kind"] == "assignment"], [claimed[0].seq])
        progress = self.upstream(claimed[0])
        self.assert_pending(progress)
        self.assertGreater(self.f.validated_continuations, 0)

    def test_native_preexisting_child_does_not_silence_a_new_run(self):
        claimed = self.start()
        self.execute(claimed, self.wait("", self.delegate()))
        child = self.child(claimed[0])
        self.assert_pending(child)
        helper_run = self.rt.team._claim(self.task)
        self.assertEqual(helper_run[0].seq, child.seq)
        self.execute(helper_run, self.wait("Please clarify the evidence scope."))
        update, = [e for e in self.messages() if e.actor.id == self.helper and e.payload["kind"] == "progress"]
        self.assertEqual(update.payload["reply_to"], child.seq)
        self.assert_pending(update)
        resumed = self.rt.team._claim(self.task)
        self.assertEqual(resumed[0].seq, update.seq)
        self.assertLess(child.seq, resumed[2].seq)
        self.assertEqual(self.rt.team._waiting_assignment(self.task, update, resumed[1]).seq, claimed[0].seq)
        self.execute(resumed, self.wait())
        progress = self.upstream(claimed[0])
        self.assert_pending(progress)
        self.assertNotIn(child.seq, _reported_messages(self.messages()))
        context = self.f.driver.calls[-1]["context"]
        self.assertEqual(context["active_assignment"]["seq"], claimed[0].seq)
        self.assertIn(child.seq, [row["seq"] for row in context["outgoing_work"]])

    def test_native_queued_child_report_resumes_owner_without_empty_upstream_wakeup(self):
        claimed = self.start()
        self.f.driver.steps[self.worker] = [self.f.native_turn("", self.delegate()), self.wait()]
        parent_run = self.rt.team._run_message(self.task, *claimed)
        self.addCleanup(parent_run.close)
        next(parent_run)
        child = self.child(claimed[0])
        self.assertGreater(child.seq, claimed[2].seq)
        helper_run = self.rt.team._claim(self.task)
        self.assertEqual(helper_run[0].seq, child.seq)
        self.execute(helper_run, self.f.native_turn("Checked evidence returned."), status="completed")
        report, = [e for e in self.messages() if e.actor.id == self.helper and e.payload["kind"] == "report"]
        self.assertEqual(report.payload["reply_to"], child.seq)
        self.assertEqual(report.payload["recipient"], self.worker)
        self.assertIn(child.seq, _reported_messages(self.messages()))
        self.assertEqual(self.rt.team._waiting_assignment(self.task, report, claimed[1]).seq, claimed[0].seq)
        self.assert_pending(report)
        list(parent_run)
        self.assertEqual(self.f.protocol_errors, [])
        self.assertEqual(self.f.driver.steps[self.worker], [])
        self.assertFalse([e for e in self.messages() if e.actor.id == self.worker
                          and e.payload["recipient"] == self.manager])
        self.assert_pending(report)
        parent_finished, = [e for e in self.f.events("team.run_finished")
                            if e.payload["message_seq"] == claimed[0].seq]
        self.assertEqual(parent_finished.payload["status"], "waiting")
        self.assert_open(claimed[0])
        resumed = self.rt.team._claim(self.task)
        self.assertEqual((resumed[0].seq, resumed[1].id), (report.seq, self.worker))
        self.execute(resumed, self.f.native_turn("", ("return_work", {
            "text": "Checked evidence accepted and returned."})), status="completed")
        result, = [e for e in self.messages() if e.actor.id == self.worker
                   and e.payload["recipient"] == self.manager]
        self.assertEqual((result.payload["kind"], result.payload["reply_to"]), ("report", claimed[0].seq))
        self.assert_pending(result)
        parent = self.rt.team._claim(self.task)
        self.assertEqual((parent[0].seq, parent[1].id), (result.seq, self.manager))
        self.execute(parent, self.wait())
        self.assert_pending()


if __name__ == "__main__":
    unittest.main()
