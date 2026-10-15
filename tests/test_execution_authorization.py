from __future__ import annotations

import unittest
from unittest.mock import patch

from cater.drivers import DriverResponse
from cater.runtime import Runtime
from cater.work import WorkConflict


class ExecutionAuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.rt = Runtime()
        self.addCleanup(self.rt.close)
        self.task = self.rt.create_task("Original task", [])
        self.scope = self.rt.work.scope(self.task)
        self.role = self.rt.worker_roles[0]
        self.driver = self.rt.drivers[self.role.driver]

    def call(self):
        return self.rt._complete_with_containment(task_id=self.task, task=self.rt.task(self.task),
            role=self.role, driver=self.driver, prompt="bounded call")

    def test_stale_authorization_cannot_enter_round(self):
        self.rt.command(self.task, "Changed requirement", goal="New task")
        with patch.object(self.rt, "_run_round") as run:
            with self.assertRaises(WorkConflict):
                self.rt.collaborate(self.task, expected_scope=self.scope)
            run.assert_not_called()
        self.assertIsNone(self.rt._execution_authorization)

    def test_change_after_round_entry_is_checked_before_model_invocation(self):
        def round_body(*args, **kwargs):
            self.rt.command(self.task, "Changed during dispatch", goal="Unapproved next scope")
            return self.call()

        with patch.object(self.rt, "_run_round", side_effect=round_body), patch.object(self.driver, "complete") as model:
            response = self.rt.collaborate(self.task, expected_scope=self.scope)
            self.assertTrue(response.meta["contained"])
            self.assertFalse(response.meta["invocation_started"])
            model.assert_not_called()
        self.assertIsNone(self.rt._execution_authorization)

    def test_matching_authorization_calls_once_and_is_cleared_on_exit(self):
        with patch.object(self.rt, "_run_round", side_effect=lambda *a, **k: self.call()), \
             patch.object(self.driver, "complete", return_value=DriverResponse("ok", 1)) as model:
            self.assertEqual(self.rt.collaborate(self.task, expected_scope=self.scope).text, "ok")
            model.assert_called_once()
        self.assertIsNone(self.rt._execution_authorization)

    def test_legacy_unbound_collaboration_keeps_its_contract(self):
        with patch.object(self.rt, "_run_round", return_value={"done": True}) as run:
            self.assertEqual(self.rt.collaborate(self.task), {"done": True})
            run.assert_called_once_with(self.task, force_governance=False)


if __name__ == "__main__":
    unittest.main()
