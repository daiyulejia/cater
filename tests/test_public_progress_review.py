"""Independent review regressions for executor/GET visibility disagreements."""

import http.client
from http.server import ThreadingHTTPServer
import json
import threading
import unittest
from unittest.mock import patch

from cater.runtime import Runtime
from cater.team import HOST
from cater.tool_protocol import ToolCall
from cater.web import ActivityRunner, make_handler
from cater.work import WorkConflict
import test_team_external as external


class PublicProgressReviewTests(unittest.TestCase):
    def setUp(self):
        self.f = external.TeamExternalTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.rt, self.task = self.f.rt, self.f.task

    def factory(self):
        return Runtime(self.f.root / "team.db", roles_path=self.f.roles,
                       drivers={self.f.driver.name: self.f.driver},
                       close_drivers_on_close=False)

    def serve(self, runner):
        with patch("cater.web.ActivityRunner", return_value=runner):
            handler = make_handler(self.f.root / "team.db", runtime_factory=self.factory,
                                   recover_on_startup=False)
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()

        def close():
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())

        self.addCleanup(close)
        return server

    def get(self, server):
        head = self.rt.log.head_hash()
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        try:
            connection.request("GET", f"/api/tasks/{self.task}")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            payload = json.loads(response.read())
            self.assertEqual(self.rt.log.head_hash(), head, "Detail projection must remain read-only")
            return payload
        finally:
            connection.close()

    def test_get_hides_boundary_supervision_superseded_by_input_to_another_role(self):
        self.f.receive("Start the original bounded turn")
        source, role, original = self.rt.team._claim(self.task)
        scope = self.rt.team.recovery.scope(self.task)
        # Valid durable boundary receipts, with the production recovery validator.
        finish = self.rt.log.emit(type="team.run_finished", task_id=self.task, actor=HOST,
            payload={**{key: original.payload[key] for key in ("role", "run_id", "message_seq")},
                     "status": "interrupted", "reason": "Bounded turn ended",
                     "recovery_of": original.seq, "interruption_kind": "turn_boundary",
                     "recovery_scope": scope})
        recovery = self.rt.team.recovery._notify(self.task, original, finish, scope,
                                                trigger="turn_boundary")
        self.assertTrue(self.rt.team.recovery.is_message(recovery))
        self.assertTrue(self.rt.team.recovery.current(self.task, recovery))
        message, role, started = self.rt.team._claim(self.task)
        self.assertEqual(message, recovery)
        _, _, binding, tools, basis = self.rt.team._model_input(
            self.task, message, role, run_id=started.payload["run_id"])
        attempt = self.rt.team.external_runs.begin(self.task, role, started, basis=basis,
            binding=binding, tool_names=[tool["name"] for tool in tools],
            driver_fingerprint="offline-review-fixture")

        runner = ActivityRunner(self.factory)
        activity_id = "review-boundary-activity"
        runner._activities[self.task] = {"id": activity_id, "state": "running", "action": "run"}
        observe = self.enterContext(runner.public_progress.capture(self.task, activity_id))
        observe(attempt, scope["revision"], {"sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": "OLD_BOUNDARY_SUPERVISION_FRAGMENT"}})
        server = self.serve(runner)
        before = self.get(server)
        self.assertEqual([row["text"] for row in before["public_progress"]["runs"]],
                         ["OLD_BOUNDARY_SUPERVISION_FRAGMENT"])
        self.assertFalse(any(event.type == "team.external_run_finished"
                             for event in self.rt.log.read(task_id=self.task)))

        worker = self.rt.team._role(self.f.worker)
        self.assertNotEqual(worker.id, role.id)
        fresh = self.rt.team.receive(self.task, f"@{worker.name} New customer requirement")
        self.assertEqual(fresh.payload["recipient"], worker.id)
        self.assertFalse(self.rt.team.recovery.current(self.task, recovery))
        with self.assertRaises(WorkConflict):
            self.rt.team._current(self.task, started, role, scope["revision"])
        after = self.get(server)
        self.assertNotIn(recovery.seq, self.rt.team._inactive_messages(self.task))
        self.assertEqual(after["public_progress"]["runs"], [],
                         "GET must reject the same superseded boundary input as the executor")

    def test_get_keeps_current_feedback_run_after_it_withdraws_its_own_assignment(self):
        manager = self.rt.team._role(self.f.manager)
        worker = self.rt.team._role(self.f.worker)
        assignment = self.rt.team._send(self.task, manager, worker.id,
                                       "Inspect delegated work", "assignment")
        _, _, worker_start = self.rt.team._claim(self.task)
        self.rt.log.emit(type="team.run_finished", task_id=self.task, actor=HOST,
            payload={**{key: worker_start.payload[key] for key in ("role", "run_id", "message_seq")},
                     "status": "waiting", "reason": "Waiting for the sender"})
        feedback = self.rt.team._send(self.task, worker, manager.id,
                                     "Inspection in progress", "progress", reply_to=assignment.seq)
        message, role, started = self.rt.team._claim(self.task)
        self.assertEqual(message, feedback)
        _, workspace, binding, tools, basis = self.rt.team._model_input(
            self.task, message, role, run_id=started.payload["run_id"])
        revision = self.rt.team.view(self.task)["revision"]
        attempt = self.rt.team.external_runs.begin(self.task, role, started, basis=basis,
            binding=binding, tool_names=[tool["name"] for tool in tools],
            driver_fingerprint="offline-review-fixture")
        # The sender consumes progress, withdraws its still-open assignment,
        # and is allowed to finish this already-running feedback turn.
        arguments = {"message_seq": assignment.seq, "reason": "No further work needed"}
        _, result = self.rt.team._dispatch_tool(self.task, message, role, started, revision,
            ToolCall("review-withdraw", "withdraw_work", arguments), tools,
            workspace, binding, {}, None, external_started_seq=attempt.seq)
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result.get("withdrawn"), result)
        self.assertIn(feedback.seq, self.rt.team._inactive_messages(self.task))
        self.assertNotIn(feedback.seq, self.rt.team._inactive_messages(self.task, started=started))
        self.rt.team._current(self.task, started, role, revision)

        runner = ActivityRunner(self.factory)
        activity_id = "review-current-feedback"
        runner._activities[self.task] = {"id": activity_id, "state": "running", "action": "run"}
        observe = self.enterContext(runner.public_progress.capture(self.task, activity_id))
        observe(attempt, revision, {"sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": "CURRENT_FEEDBACK_FRAGMENT"}})
        detail = self.get(self.serve(runner))
        self.assertEqual([row["text"] for row in detail["public_progress"]["runs"]],
                         ["CURRENT_FEEDBACK_FRAGMENT"],
                         "The executor's surviving current feedback run must remain observable")


if __name__ == "__main__":
    unittest.main()
