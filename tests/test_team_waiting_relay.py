"""A host-generated empty waiting echo must not become another role's job."""

from __future__ import annotations

import unittest

from cater.team import _reported_messages
import test_team_supervision


class WaitingRelayTests(unittest.TestCase):
    def setUp(self):
        self.f = test_team_supervision.TeamSupervisionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.helper = next(r.id for r in self.f.rt.worker_roles if r.id != self.f.worker)

    def run_chain(self, *, addressed=False, separate_wait=False, final=""):
        f = self.f
        def manager(context, history, tools):
            if context["message"]["sender"] == "human":
                return f.native_turn("", ("delegate", {"recipient": f.worker, "instruction": "Prepare a result with evidence"}),
                                     ("wait_for_reply", {}))
            return f.native_turn("Result or update received.")

        def author(context, history, tools):
            if context["message"]["kind"] == "report":
                self.assertEqual(context["active_assignment"]["sender"], f.manager)
                return f.native_turn("Original result complete with the helper's evidence.")
            if history:
                return f.native_turn(final, ("wait_for_reply", {}))
            tool = ("send_message", {"recipient": self.helper, "text": "Return the evidence for this result"}) if addressed else (
                "delegate", {"recipient": self.helper, "instruction": "Return the evidence for this result"})
            return f.native_turn("" if separate_wait else final, tool, *(() if separate_wait else (("wait_for_reply", {}),)))

        f.driver.steps = {f.manager: [manager] * 4, f.worker: [author] * 4,
                          self.helper: [f.native_turn("Evidence ready; verified only within this scripted fixture.")]}
        f.rt.team.receive(f.task, "Prepare and hand back the result", request_id="waiting-relay-chain")
        f.run_team()
        messages = f.rt.team._messages(f.task)
        original, = [e for e in messages if e.payload["kind"] == "assignment" and e.actor.id == f.manager]
        child, = [e for e in messages if e.actor.id == f.worker and e.payload["recipient"] == self.helper]
        self.assertEqual(child.payload["parent_assignment_seq"], original.seq)
        self.assertIn(original.seq, _reported_messages(messages))
        self.assertIn(child.seq, _reported_messages(messages))
        self.assertEqual(f.rt.team.view(f.task)["pending_count"], 0)
        return messages

    def test_delegate_and_wait_does_not_wake_parent_without_new_information(self):
        messages = self.run_chain()
        manager_inputs = [c["context"]["message"]["kind"] for c in self.f.driver.calls
                          if c["context"]["you"]["id"] == self.f.manager]
        self.assertEqual(manager_inputs, ["message", "report"])
        self.assertFalse([e for e in messages if e.payload["kind"] == "progress"])
        self.assertEqual(len(self.f.driver.calls), 5)

    def test_addressed_subwork_keeps_the_same_return_path_without_empty_echo(self):
        messages = self.run_chain(addressed=True)
        self.assertFalse([e for e in messages if e.payload["kind"] == "progress"])
        self.assertEqual(len(self.f.driver.calls), 5)

    def test_later_wait_in_same_native_run_keeps_child_dependency(self):
        messages = self.run_chain(separate_wait=True)
        self.assertFalse([e for e in messages if e.payload["kind"] == "progress"])
        self.assertEqual(len(self.f.driver.calls), 6)

    def test_public_text_is_preserved_without_waking_parent_while_child_is_active(self):
        text = "The compatibility evidence is being checked by the helper."
        messages = self.run_chain(final=text)
        notice, = [e for e in messages if e.payload["kind"] == "notification"]
        self.assertEqual(notice.payload["text"], text)
        self.assertEqual(notice.payload["recipient"], self.f.manager)
        self.assertIsNone(notice.payload["reply_to"])
        self.assertTrue(notice.payload["parent_assignment_seq"])
        self.assertFalse(any(c["context"]["message"]["seq"] == notice.seq for c in self.f.driver.calls))
        self.assertEqual([c["context"]["message"]["kind"] for c in self.f.driver.calls
                          if c["context"]["you"]["id"] == self.f.manager], ["message", "report"])

    def test_customer_preempted_child_without_report_still_reaches_supervision(self):
        f, team = self.f, self.f.rt.team
        def preempt(context, history, tools):
            message = team.receive(f.task, "@" + self.helper + " What is your role? Answer only this question.",
                                   request_id="waiting-relay-preempt-helper")
            self.assertEqual(message.payload["recipient"], self.helper)
            return f.native_turn("")

        def supervise(context, history, tools):
            original, = [e for e in team._messages(f.task)
                         if e.actor.id == f.manager and e.payload["kind"] == "assignment"]
            self.assertTrue(any(r["role"] == self.helper and r["status"] == "interrupted"
                                for r in context["current_work"]["runs"]))
            return f.native_turn("", ("send_message", {"recipient": f.worker,
                "continue_assignment_seq": original.seq,
                "text": "The helper was interrupted. Return the actual outcome and missing evidence."}),
                ("wait_for_reply", {}))

        boundary = "Evidence unavailable: the helper was interrupted, no evidence produced."
        f.driver.steps = {
            f.manager: [f.native_turn("", ("delegate", {"recipient": f.worker,
                "instruction": "Prepare and return evidence"}), ("wait_for_reply", {})), supervise,
                f.native_turn("Missing evidence and the incomplete outcome acknowledged.")],
            f.worker: [f.native_turn("", ("delegate", {"recipient": self.helper,
                "instruction": "Collect and return evidence"})),
                f.native_turn("", ("read_messages", {})), f.native_turn("", ("read_messages", {})),
                f.native_turn("", ("wait_for_reply", {})), f.native_turn(boundary)],
            self.helper: [f.native_turn("", ("read_messages", {})), preempt,
                f.native_turn("I am the helper. This answer does not collect evidence.")],
        }
        team.receive(f.task, "Prepare the evidence", request_id="waiting-relay-interruption")
        f.run_team()
        messages = team._messages(f.task)
        original, = [e for e in messages if e.actor.id == f.manager and e.payload["kind"] == "assignment"]
        self.assertIn(original.seq, _reported_messages(messages))
        self.assertEqual([c["context"]["message"]["kind"] for c in f.driver.calls
                          if c["context"]["you"]["id"] == f.manager], ["message", "progress", "report"])
        report, = [e for e in messages if e.actor.id == f.worker and e.payload["kind"] == "report"]
        self.assertEqual((report.payload["text"], report.payload["reply_to"]), (boundary, original.seq))
        self.assertFalse([e for e in messages if e.actor.id == self.helper and e.payload["recipient"] == f.worker])
        self.assertEqual(team.view(f.task)["pending_count"], 0)
        self.assertIsNone(team._claim(f.task))
        self.assertEqual(f.rt.task(f.task).deliverables, [])
        self.assertNotEqual(team.view(f.task)["status"], "done")


if __name__ == "__main__":
    unittest.main()
