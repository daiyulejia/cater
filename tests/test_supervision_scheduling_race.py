"""A returned message must not become a circular delivery dependency."""

import unittest
from dataclasses import replace

from cater.team import HOST
import test_simple_team as fixture


class SupervisionSchedulingRaceTests(unittest.TestCase):
    setUp = fixture.SimpleTeamTests.setUp
    events = fixture.SimpleTeamTests.events

    def send(self, sender, recipient, *, kind="message", parent=None, reply_to=None):
        return self.rt.team._send(self.task, self.rt.team._role(sender), recipient,
                                  "Scheduling boundary fixture", kind,
                                  parent_assignment_seq=parent.seq if parent else None,
                                  reply_to=reply_to.seq if reply_to else None)

    def start(self, source):
        return self.rt.log.emit(type="team.run_started", task_id=self.task, actor=HOST, payload={
            "message_seq": source.seq, "role": source.payload["recipient"],
            "run_id": f"scheduling-{source.seq}", "revision": self.rt.team.view(self.task)["revision"],
            "control_seq": self.rt.execution_control.view(self.task)["seq"],
        })

    def run_input(self, source, *calls):
        role = self.rt.team._role(source.payload["recipient"])
        started = self.start(source)
        self.driver.steps[role.id] = ([fixture.turn("", *calls)] if calls else []) + [
            fixture.turn("", ("wait_for_reply", {}))]
        list(self.rt.team._run_message(self.task, source, role, started))
        finished, = [event for event in self.events("team.run_finished")
                      if event.payload["run_id"] == started.payload["run_id"]]
        self.assertEqual(finished.payload["status"], "waiting", finished.payload)
        self.assertEqual(self.driver.steps[role.id], [])

    def waiting_parent(self, *, upstream=True):
        parent = self.send(self.manager, self.worker)
        calls = [("send_message", {"recipient": self.manager, "text": "The checked result is ready."})]
        self.run_input(parent, *(calls if upstream else []))
        return parent

    def upstream(self, parent):
        result, = [event for event in self.events("team.message_received")
                   if event.actor.id == self.worker and event.payload["recipient"] == self.manager
                   and event.payload.get("parent_assignment_seq") == parent.seq]
        return result

    def return_child(self, parent):
        helper = next(role.id for role in self.rt.worker_roles if role.id != self.worker)
        child = self.send(self.worker, helper, kind="assignment", parent=parent)
        self.send(helper, self.worker, kind="report", reply_to=child)
        row, = self.rt.team.recovery.assignments(self.task)
        self.assertEqual(row["seq"], parent.seq)
        self.assertTrue(row["has_delegated_work"])
        return helper

    def finish_input(self, source):
        # Direct finish fixtures isolate durable input/run matching from queue order.
        started = self.start(source)
        return self.rt.log.emit(type="team.tool_started", task_id=self.task, actor=HOST, payload={
            **started.payload, "name": "finish_work", "call_id": f"finish-{source.seq}",
            "arguments": {"summary": "Delivery confirmed", "verification": "Fixture only"},
        })

    def finish(self, tool):
        return self.rt.team._finish_work(self.task, self.rt.organization.leader,
                                         tool.payload["arguments"], tool)

    def assert_blocked(self, tool, parent):
        result = self.finish(tool)
        self.assertEqual((result["status"], result.get("code")), ("error", "work_pending"), result)
        self.assertIn(parent.seq, {row["seq"] for row in result["assignments"]})
        self.assertEqual(self.events("team.objective_completed"), [])
        self.assertTrue(self.rt.verify_chain().ok)
        return result

    def test_upstream_result_does_not_block_its_current_manager_input(self):
        self.driver.steps = {
            self.manager: [
                fixture.turn("", ("send_message", {
                    "recipient": self.worker, "text": "Check the result and tell me"})),
                fixture.turn("", ("wait_for_reply", {})),
                fixture.turn("", ("finish_work", {
                    "summary": "Delivery confirmed", "verification": "Fixture only"})),
                # Keep a rejected delivery from exhausting the scripted model.
                fixture.turn("", ("wait_for_reply", {})),
            ],
            self.worker: [
                fixture.turn("", ("send_message", {
                    "recipient": self.manager, "text": "The checked result is ready."})),
                fixture.turn("", ("wait_for_reply", {})),
            ],
        }
        self.rt.team.receive(self.task, "Check and finish", request_id="upstream-result-dependency")
        self.rt.collaborate(self.task)

        self.assertTrue(self.rt.verify_chain().ok)
        runs = self.events("team.run_finished")
        self.assertFalse([event for event in runs if event.payload["status"] == "failed"])
        self.assertEqual(sum(call["context"]["you"]["id"] == self.worker
                             for call in self.driver.calls), 2)
        returned, = [event for event in self.events("team.message_received")
                     if event.actor.id == self.worker and event.payload["kind"] == "message"]
        waiting, = [event for event in runs if event.payload["role"] == self.worker]
        attempted, = [event for event in self.events("team.tool_started")
                      if event.payload["name"] == "finish_work"]
        self.assertEqual(attempted.payload["message_seq"], returned.seq)
        self.assertEqual(waiting.payload["message_seq"], returned.payload["parent_assignment_seq"])
        self.assertEqual(waiting.payload["status"], "waiting")
        self.assertLess(waiting.seq, attempted.seq)

        finished, = [event for event in self.events("team.tool_finished")
                     if event.payload["name"] == "finish_work"]
        result = finished.payload["result"]
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(self.rt.team.view(self.task)["status"], "done")
        self.assertFalse([event for event in self.events("team.message_received")
                          if event.payload["recipient"] == self.worker
                          and event.payload["kind"] in {"report", "progress"}])

    def test_another_waiting_child_must_return_before_upstream_result_allows_finish(self):
        parent = self.waiting_parent()
        helper = next(role.id for role in self.rt.worker_roles if role.id != self.worker)
        child = self.send(self.worker, helper, kind="assignment", parent=parent)
        self.run_input(child, ("send_message", {
            "recipient": self.worker, "text": "Still checking", "notify_only": True}))
        tool = self.finish_input(self.upstream(parent))
        rows = {row["seq"]: row for row in self.rt.team.recovery.assignments(self.task)}
        self.assertEqual(rows[child.seq]["status"], "waiting")
        self.assertFalse(rows[child.seq]["has_delegated_work"])
        self.assertTrue(rows[parent.seq]["has_delegated_work"])
        self.assert_blocked(tool, parent)

        self.send(helper, self.worker, kind="report", reply_to=child)
        self.assertEqual(self.finish(tool)["status"], "ok")

    def test_withdrawing_child_without_upstream_result_does_not_complete_parent(self):
        parent = self.send(self.manager, self.worker)
        helper = next(role.id for role in self.rt.worker_roles if role.id != self.worker)
        self.run_input(parent, ("delegate", {"recipient": helper, "instruction": "Check a detail"}))
        child, = [event for event in self.events("team.message_received")
                   if event.payload.get("parent_assignment_seq") == parent.seq]
        request = self.rt.team.receive(self.task, "Withdraw only the redundant child", request_id="withdraw-child")
        self.run_input(request, ("withdraw_work", {"message_seq": child.seq, "reason": "Redundant child"}))
        finish_request = self.rt.team.receive(self.task, "Check delivery", request_id="after-withdrawal")
        tool = self.finish_input(finish_request)
        rows = self.rt.team.recovery.assignments(self.task)
        self.assertEqual([row["seq"] for row in rows], [parent.seq])
        self.assertTrue(rows[0]["has_delegated_work"])
        self.assertFalse([event for event in self.events("team.message_received")
                          if event.actor.id == self.worker and event.payload["recipient"] == self.manager])
        self.assert_blocked(tool, parent)

    def test_unrelated_plain_message_is_not_an_upstream_result(self):
        parent = self.waiting_parent(upstream=False)
        self.return_child(parent)
        unrelated = self.send(self.worker, self.manager)
        tool = self.finish_input(unrelated)
        self.assertIsNone(unrelated.payload.get("parent_assignment_seq"))
        self.assert_blocked(tool, parent)

    def test_message_from_different_worker_cannot_release_waiting_parent(self):
        parent = self.waiting_parent(upstream=False)
        helper = self.return_child(parent)
        unrelated = self.send(helper, self.manager, parent=parent)
        self.assert_blocked(self.finish_input(unrelated), parent)

    def test_result_for_other_request_does_not_release_waiting_parent(self):
        parent = self.waiting_parent(upstream=False)
        self.return_child(parent)
        other = self.waiting_parent()
        self.assertNotEqual(parent.seq, other.seq)
        self.assert_blocked(self.finish_input(self.upstream(other)), parent)

    def test_assignment_is_not_a_plain_upstream_result(self):
        parent = self.waiting_parent(upstream=False)
        incoming = self.send(self.worker, self.manager, kind="assignment", parent=parent)
        self.assert_blocked(self.finish_input(incoming), parent)

    def test_other_run_cannot_borrow_upstream_result_exception(self):
        parent = self.waiting_parent()
        tool = self.finish_input(self.upstream(parent))
        other_run = replace(tool, payload={**tool.payload, "run_id": "different-run"})
        self.assert_blocked(other_run, parent)
        self.assertEqual(self.finish(tool)["status"], "ok")

    def test_incoming_result_from_finished_run_cannot_release_parent(self):
        parent = self.waiting_parent()
        tool = self.finish_input(self.upstream(parent))
        self.rt.log.emit(type="team.run_finished", task_id=self.task, actor=HOST, payload={
            **tool.payload, "status": "waiting", "reason": "The manager is no longer consuming this input",
        })
        self.assert_blocked(tool, parent)

    def test_running_parent_cannot_use_waiting_result_exception(self):
        parent = self.waiting_parent()
        self.start(parent)
        self.assert_blocked(self.finish_input(self.upstream(parent)), parent)
