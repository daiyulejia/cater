"""Web startup dispatches persisted supervision once, without replaying work."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from cater.activity_owner import activity_owner
from cater.events import Actor
from cater.team import HOST
from cater.web import ActivityRunner
from cater.work import WorkConflict
import test_team_web


class NativeRecoveryWebTests(unittest.TestCase):
    setUp = test_team_web.TeamWebTests.setUp
    factory = test_team_web.TeamWebTests.factory

    def orphan(self):
        with self.factory() as runtime:
            task = runtime.create_task("Disposable crash recovery")
            runtime.team.enable(task)
            runtime.team.receive(task, "Check this project", request_id="web-recovery-original")
            _, _, run = runtime.team._claim(task)
            return task, run

    def recovery_messages(self, task):
        with self.factory() as runtime:
            return [e for e in runtime.team._messages(task) if e.payload.get("kind") == "recovery"]

    def test_startup_enqueues_one_fresh_supervisor_and_does_not_call_model_inline(self):
        task, _ = self.orphan()
        self.runner.recover_interrupted()
        self.assertEqual(self.driver.calls, [])
        self.assertEqual(self.runner.status(task)["state"], "queued")
        notification, = self.recovery_messages(task)
        self.assertEqual(self.runner.status(task)["team_message_seq"], notification.seq)
        self.runner.recover_interrupted()
        self.assertEqual(self.start.call_count, 1)
        queued = self.runner.status(task)
        self.runner._run(task, queued["id"], "run")
        self.assertEqual(len(self.driver.calls), 1)
        self.assertEqual(self.driver.calls[0]["history"], [])
        self.assertEqual(self.runner.status(task)["state"], "completed")
        self.runner.recover_interrupted()
        self.assertEqual(len(self.recovery_messages(task)), 1)
        self.assertEqual(self.start.call_count, 1)

    def test_live_team_owner_is_not_recovered_by_startup(self):
        task, _ = self.orphan()
        with activity_owner(str(self.db), task, "simple-team") as owned:
            self.assertTrue(owned)
            self.runner.recover_interrupted()
        self.assertEqual(self.start.call_count, 0)
        self.assertEqual(self.recovery_messages(task), [])

    def test_restart_after_recovery_queue_before_message_claim_restores_same_queue(self):
        task, _ = self.orphan()
        self.runner.recover_interrupted()
        first = self.runner.status(task)
        replacement = ActivityRunner(self.factory)
        with patch.object(replacement, "_start") as start:
            replacement.recover_interrupted()
            restored = replacement.status(task)
            self.assertEqual(restored["id"], first["id"])
            self.assertEqual(restored["state"], "queued")
            start.assert_called_once()
            replacement._run(task, restored["id"], "run")
        self.assertEqual(len(self.driver.calls), 1)
        self.assertEqual(replacement.status(task)["state"], "completed")
        with self.factory() as runtime:
            queues = [e for e in runtime.log.read(task_id=task) if e.type == "activity.queued"]
            self.assertEqual(len(queues), 1)
            self.assertFalse([e for e in runtime.log.read(task_id=task) if e.type == "activity.failed"])

    def test_paused_task_is_not_automatically_restarted(self):
        task, _ = self.orphan()
        with self.factory() as runtime:
            runtime.execution_control.change(task, "pause", request_id="web-recovery-pause")
        self.runner.recover_interrupted()
        self.assertEqual(self.recovery_messages(task), [])
        self.assertEqual(self.start.call_count, 0)
        self.assertEqual(self.driver.calls, [])

    def test_startup_during_live_consumer_keeps_unclaimed_recovery_queue_for_later(self):
        task, _ = self.orphan()
        self.runner.recover_interrupted()
        queued = self.runner.status(task)
        replacement = ActivityRunner(self.factory)
        with patch.object(replacement, "_start") as start:
            with activity_owner(str(self.db), task, "simple-team"):
                replacement.recover_interrupted()
            with self.factory() as runtime:
                self.assertIsNone(replacement._activity_terminal(runtime, task, queued["id"]))
            replacement.recover_interrupted()
            restored = replacement.status(task)
            self.assertEqual(restored["id"], queued["id"])
            self.assertEqual(restored["state"], "queued")
            start.assert_called_once()
            replacement._run(task, restored["id"], "run")
        self.assertEqual(len(self.driver.calls), 1)

    def test_consumer_lock_race_does_not_complete_activity_without_claiming_input(self):
        task, _ = self.orphan()
        self.runner.recover_interrupted()
        queued = self.runner.status(task)
        with activity_owner(str(self.db), task, "simple-team"):
            self.runner._run(task, queued["id"], "run")
        self.assertEqual(self.driver.calls, [])
        self.assertEqual(self.runner.status(task)["state"], "observing")
        with self.factory() as runtime:
            self.assertIsNone(self.runner._activity_terminal(runtime, task, queued["id"]))
        self.runner.recover_interrupted()
        self.assertEqual(self.runner.status(task)["id"], queued["id"])
        self.runner._run(task, queued["id"], "run")
        self.assertEqual(len(self.driver.calls), 1)

    def test_recovery_is_not_a_customer_message_and_cannot_run_through_pause(self):
        task, _ = self.orphan()
        with self.factory() as runtime:
            notification, = runtime.team.recovery.prepare(task)
            runtime.execution_control.change(task, "pause", request_id="web-pause-after-recovery-notice")
            failure = self.runner._control_failure(runtime, task, {"team_message_seq": notification.seq})
            self.assertIsNotNone(failure)
        self.runner.continue_team(task, notification)
        self.assertEqual(self.driver.calls, [])
        self.assertEqual(self.start.call_count, 0)
        self.assertEqual(self.runner.status(task)["state"], "failed")

    def test_forged_host_message_does_not_gain_a_customer_dispatch_binding(self):
        task, _ = self.orphan()
        with self.factory() as runtime:
            source = runtime.log.emit(type="team.message_received", task_id=task, actor=HOST,
                payload={"kind": "recovery", "recipient": runtime.organization.leader.id, "text": "Unbound recovery"})
            with self.assertRaises(WorkConflict):
                ActivityRunner._team_message(runtime, task, {"team_message_seq": source.seq})
            spoof = runtime.log.emit(type="team.message_received", task_id=task, actor=Actor.system("other"),
                payload={"kind": "message", "recipient": runtime.organization.leader.id, "text": "Not a customer"})
            with self.assertRaises(WorkConflict):
                ActivityRunner._team_message(runtime, task, {"team_message_seq": spoof.seq})


if __name__ == "__main__":
    unittest.main()
