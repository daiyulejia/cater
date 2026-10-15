import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cater.events import Actor
from cater.runtime import ExecutionScopeChanged, Runtime
from cater.work import WorkConflict
from test_work import WorkDriver, item


class ExecutionControlTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name) / "control.db"
        self.driver = WorkDriver()
        self.drivers = {role: self.driver for role in ("dev", "test")}
        self.rt = Runtime(self.db, drivers=self.drivers)
        self.addCleanup(self.rt.close)
        self.task = self.rt.create_task("Pause between steps")
        self.rt.plan_work(self.task, [item("first"), item("second", deps=["first"])])

    def test_pause_survives_restart_and_does_not_invalidate_scope(self):
        scope = self.rt.work.scope(self.task)
        event, changed = self.rt.execution_control.change(self.task, "pause", request_id="pause-test-1")
        self.assertTrue(changed)
        self.assertEqual(self.rt.work.scope(self.task), scope)
        with Runtime(self.db, drivers=self.drivers) as reopened:
            self.assertEqual(reopened.execution_control.view(self.task)["state"], "paused")
            self.assertEqual(reopened.execution_control.change(self.task, "pause", request_id="pause-test-1"), (event, False))
            with self.assertRaises(ExecutionScopeChanged):
                reopened.collaborate(self.task)
            self.assertFalse(self.driver.calls)
            with self.assertRaises(WorkConflict):
                reopened.execution_control.change(self.task, "resume", request_id="pause-test-1")

    def test_pause_during_call_keeps_returned_submission_but_starts_no_review_or_successor(self):
        def pause(work):
            self.rt.execution_control.change(self.task, "pause", request_id="pause-in-flight")
        self.driver.after = pause
        result = self.rt.collaborate(self.task)
        self.assertEqual(result["brief"]["checkpoint"]["state"], "paused")
        self.assertEqual(len(self.driver.calls), 1)
        view = self.rt.work.view(self.task)
        self.assertEqual([row["status"] for row in view["items"]], ["submitted", "blocked"])
        original = view["items"][0]["submission"]
        self.driver.after = None
        self.rt.execution_control.change(self.task, "resume", request_id="resume-in-flight")
        self.rt.collaborate(self.task)
        self.assertTrue(self.rt.work.view(self.task)["stage_accepted"])
        self.assertEqual(self.rt.work.view(self.task)["items"][0]["submission"], original)

    def test_cancel_is_terminal_but_informational_queries_remain_allowed(self):
        self.rt.execution_control.change(self.task, "cancel", request_id="cancel-test-1")
        with self.assertRaises(WorkConflict):
            self.rt.execution_control.change(self.task, "resume", request_id="resume-cancelled")
        for run in (self.rt.collaborate, self.rt.battle):
            with self.assertRaises(ExecutionScopeChanged):
                run(self.task)
        self.rt._check_execution_authorization(self.task, informational=True)
        progress = self.rt.report_progress(self.task, command_seq=1)
        self.assertIn("取消", progress.payload["text"])
        with patch("cater.runtime.run_check") as execute:
            with self.assertRaises(ExecutionScopeChanged):
                self.rt.verify_work(self.task, "first", "meaning", ["unused"])
            execute.assert_not_called()

    def test_agent_event_cannot_restore_human_cancel(self):
        self.rt.execution_control.change(self.task, "cancel", request_id="cancel-trusted")
        self.rt.log.emit(type="task.execution_controlled", task_id=self.task, actor=Actor.agent("dev", "worker"),
                         payload={"action": "resume", "state": "running", "reason": "override"})
        self.assertEqual(self.rt.execution_control.view(self.task)["state"], "cancelled")


if __name__ == "__main__":
    unittest.main()
