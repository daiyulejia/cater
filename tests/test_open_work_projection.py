"""Read-only web projection of open work after a child stops."""
import unittest

from cater.web import _team_view
import test_team_external as external


class OpenWorkProjectionTests(unittest.TestCase):
    def test_open_work_survives_child_failure_and_is_not_a_pending_message(self):
        f = external.TeamExternalTests()
        f.setUp()
        self.addCleanup(f.doCleanups)

        def fail(run):
            raise RuntimeError("offline failure")

        def parent(run):
            run.call("wait_for_reply")

        f.driver.scripts = {f.manager: [f.delegate_wait, parent], f.worker: [fail]}
        f.receive()
        f.drain()
        before = f.rt.log.read(task_id=f.task)
        view = _team_view(f.rt, f.task)
        self.assertEqual(view["pending_count"], 0)
        self.assertEqual(view["open_work_count"], 1)
        self.assertFalse(any(run["status"] == "running" for run in view["runs"]))
        self.assertEqual(before, f.rt.log.read(task_id=f.task))
        self.assertEqual(len(f.rt.team.recovery.assignments(f.task)), 1)
