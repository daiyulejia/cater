"""Recovery failures stay local and never authorize replay of claimed inputs."""

from __future__ import annotations

import threading
import unittest
from unittest.mock import patch

from cater.activity_owner import activity_owner
from cater.events import Actor, EventLog
from cater.execution_control import ExecutionControl
from cater.team import HOST
from cater.web import ActivityRunner
from cater.work import WorkConflict
import test_team_web


ORCHESTRATOR = Actor.system("orchestrator")


class NativeRecoveryFailureTests(unittest.TestCase):
    setUp = test_team_web.TeamWebTests.setUp
    factory = test_team_web.TeamWebTests.factory

    def events(self, task, kind=None):
        with self.factory() as runtime:
            return list(runtime.log.iter_events(task_id=task, event_types=(kind,) if kind else None))

    def queued_orphan(self):
        with self.factory() as runtime:
            task = runtime.create_task("Disposable partial recovery")
            runtime.team.enable(task)
            message = runtime.team.receive(task, "Check the remaining work", request_id="partial-recovery-original")
        self.runner.continue_team(task, message)
        queued = self.runner.status(task)
        with self.factory() as runtime:
            _, _, started = runtime.team._claim(task)
        self.assertLess(self.events(task, "activity.queued")[0].seq, started.seq)
        return task, queued

    def stranded_notice(self):
        task, old = self.queued_orphan()
        with self.factory() as runtime:
            notice, = runtime.team.recovery.prepare(task)
        runner = ActivityRunner(self.factory)
        start = self.enterContext(patch.object(runner, "_start"))
        runner.continue_team(task, notice)
        supervisor = runner.status(task)
        runner._run(task, supervisor["id"], "run")
        self.assertEqual(runner.status(task)["state"], "failed")
        failure, = [e for e in self.events(task, "activity.failed")
                    if e.payload["activity_id"] == supervisor["id"]]
        self.assertEqual(self.driver.calls, [])
        return task, old, notice, runner, start, failure

    def fail_terminal(self, activity_id):
        original = EventLog.emit

        def emit(log, **kwargs):
            if kwargs["type"] == "activity.failed" and kwargs["payload"]["activity_id"] == activity_id:
                raise OSError("fixture terminal storage unavailable")
            return original(log, **kwargs)

        return patch.object(EventLog, "emit", new=emit)

    def repaired_notice(self):
        task, old, notice, runner, start, failure = self.stranded_notice()
        queued = next(e for e in self.events(task, "activity.queued") if e.payload["activity_id"] == old["id"])
        self.assertIsNotNone(runner._recover_activity(task, queued))
        return task, old, notice, runner, start, failure

    def assert_not_retried(self, task, notice, runner, start):
        queues = self.events(task, "activity.queued")
        starts = start.call_count
        runner.continue_team(task, notice)
        self.assertEqual(self.events(task, "activity.queued"), queues)
        self.assertEqual(start.call_count, starts)
        self.assertEqual(self.driver.calls, [])

    def test_unclaimed_failed_supervisor_gets_a_new_traceable_queue_after_repair(self):
        task, old, notice, runner, start, failure = self.stranded_notice()
        self.assertEqual(runner.recover_interrupted(), 1)
        retry = runner.status(task)
        self.assertNotEqual(retry["id"], failure.payload["activity_id"])
        self.assertEqual(retry["state"], "queued")
        self.assertEqual(retry["team_message_seq"], notice.seq)
        self.assertEqual(retry["recovery_retry_of"], failure.seq)
        runner.recover_interrupted()
        self.assertEqual(start.call_count, 2)
        runner._run(task, retry["id"], "run")
        self.assertEqual(len(self.driver.calls), 1)
        self.assertEqual(runner.status(task)["state"], "completed")
        self.assertEqual([e for e in self.events(task, "activity.failed")
                          if e.payload["activity_id"] == failure.payload["activity_id"]], [failure])
        self.assertEqual(len([e for e in self.events(task, "team.run_started")
                              if e.payload["message_seq"] == notice.seq]), 1)
        replacement = ActivityRunner(self.factory)
        with patch.object(replacement, "_start") as replacement_start:
            replacement.recover_interrupted()
            replacement_start.assert_not_called()

    def test_terminal_write_failure_defers_native_dispatch_until_that_task_is_repaired(self):
        task, old = self.queued_orphan()
        runner = ActivityRunner(self.factory)
        with patch.object(runner, "_start") as start:
            with self.fail_terminal(old["id"]), self.assertLogs("cater.web", level="ERROR"):
                self.assertEqual(runner.recover_interrupted(), 0)
            start.assert_not_called()
            self.assertEqual(len(self.events(task, "activity.queued")), 1)
            self.assertEqual(runner.recover_interrupted(), 1)
            start.assert_called_once()
            queued = runner.status(task)
            runner._run(task, queued["id"], "run")
        self.assertEqual(len(self.driver.calls), 1)
        self.assertEqual(runner.status(task)["state"], "completed")

    def check_control_read_failure(self, *, running):
        task, old = self.queued_orphan()
        if running:
            self.runner._mark_running(task, old["id"])
        with self.factory() as runtime:
            next_message = runtime.team.receive(task, "New customer input", request_id="partial-recovery-followup")
            other = runtime.create_task("Independent interrupted task")
        self.runner.continue_team(task, next_message)
        before = self.runner.status(task)
        orphan_runner = ActivityRunner(self.factory)
        with patch.object(orphan_runner, "_start"):
            other_activity = orphan_runner.submit(other, "run")
        view = ExecutionControl.view

        def unavailable(control, task_id):
            if task_id == task:
                raise OSError("fixture task-local control read unavailable")
            return view(control, task_id)

        with activity_owner(str(self.db), task, old["id"]) as owned:
            self.assertTrue(owned)
            with patch.object(ExecutionControl, "view", new=unavailable), \
                    self.assertLogs("cater.web", level="ERROR") as logged:
                self.assertEqual(self.runner.recover_interrupted(), 1)
            self.assertTrue(any(task in line for line in logged.output))
            after = self.runner.status(task)
            self.assertEqual({k: v for k, v in after.items() if k != "revision"},
                             {k: v for k, v in before.items() if k != "revision"})
            self.assertEqual(self.events(task, "activity.failed"), [])
            self.assertEqual(self.events(task, "team.run_finished"), [])
            self.assertEqual(len(self.events(task, "activity.queued")), 2)
            with self.factory() as runtime:
                self.assertIsNotNone(self.runner._activity_terminal(runtime, other, other_activity["id"]))
            self.assertEqual(self.runner.recover_interrupted(), 0)
        self.assertEqual(self.driver.calls, [])

    def test_live_task_control_read_failure_skips_task_and_recovers_other_task(self):
        self.check_control_read_failure(running=True)

    def test_queued_control_read_failure_is_not_proof_of_stopping(self):
        self.check_control_read_failure(running=False)

    def test_orphan_control_read_failure_skips_task_without_recording_stopped_work(self):
        task, old = self.queued_orphan()
        with self.factory() as runtime:
            other = runtime.create_task("Independent orphan during control outage")
        self.runner.submit(other, "run")
        runner = ActivityRunner(self.factory)
        view = ExecutionControl.view

        def unavailable(control, task_id):
            if task_id == task:
                raise OSError("fixture orphan control read unavailable")
            return view(control, task_id)

        with patch.object(runner, "_start") as start:
            with patch.object(ExecutionControl, "view", new=unavailable), \
                    self.assertLogs("cater.web", level="ERROR"):
                self.assertEqual(runner.recover_interrupted(), 1)
            start.assert_not_called()
            self.assertEqual(self.events(task, "activity.failed"), [])
            self.assertEqual(self.events(task, "team.run_finished"), [])
            self.assertEqual(len(self.events(task, "activity.queued")), 1)
            self.assertEqual(runner.recover_interrupted(), 1)
            start.assert_called_once()

    def test_live_consumer_or_terminal_activity_owner_prevents_retry(self):
        for owner in ("consumer", "original", "supervisor"):
            with self.subTest(owner=owner):
                task, old, notice, runner, start, failure = self.repaired_notice()
                identity = {"consumer": "simple-team", "original": old["id"],
                            "supervisor": failure.payload["activity_id"]}[owner]
                with activity_owner(str(self.db), task, identity) as owned:
                    self.assertTrue(owned)
                    self.assert_not_retried(task, notice, runner, start)
                runner.continue_team(task, notice)
                self.assertEqual(runner.status(task)["state"], "queued")
                self.assertEqual(runner.status(task)["recovery_retry_of"], failure.seq)

    def test_claimed_input_cannot_retry_inflight_or_after_its_run_failed(self):
        task, _, notice, runner, start, _ = self.repaired_notice()
        with self.factory() as runtime:
            message, _, claimed = runtime.team._claim(task)
        self.assertEqual(message.seq, notice.seq)
        with activity_owner(str(self.db), task, "simple-team") as owned:
            self.assertTrue(owned)
            self.assert_not_retried(task, notice, runner, start)
        self.assert_not_retried(task, notice, runner, start)
        with self.factory() as runtime:
            runtime.log.emit(type="team.run_finished", task_id=task, actor=HOST,
                             payload={**claimed.payload, "status": "failed", "reason": "Fixture invocation failed"})
        self.assert_not_retried(task, notice, runner, start)

    def change_scope(self, runtime, task, change):
        if change in {"pause", "cancel", "resume"}:
            runtime.execution_control.change(task, "pause" if change == "resume" else change,
                                             request_id="native-retry-stop-" + change)
            if change == "resume":
                runtime.execution_control.change(task, "resume", request_id="native-retry-fresh-resume")
        elif change == "customer":
            runtime.team.receive(task, "Use this new customer direction", request_id="native-retry-new-customer")
        elif change == "workspace":
            runtime.workspaces.bind(task, "source", expected_revision=None)
        else:
            runtime.log.emit(type="team.objective_completed" if change == "done" else "team.objective_updated",
                             task_id=task, actor=HOST,
                             payload={"goal": "New objective", "summary": "Done", "verification": "Fixture"})

    def test_scope_changes_prevent_requeue_even_when_notice_remains_unclaimed(self):
        for change in ("pause", "cancel", "resume", "customer", "goal", "done", "workspace"):
            with self.subTest(change=change):
                task, _, notice, runner, start, _ = self.repaired_notice()
                with self.factory() as runtime:
                    self.change_scope(runtime, task, change)
                self.assert_not_retried(task, notice, runner, start)

    def test_retry_rechecks_customer_scope_and_claims_before_execution(self):
        for change in ("customer", "pause", "cancel", "goal", "claim"):
            with self.subTest(change=change):
                task, _, notice, runner, _, _ = self.repaired_notice()
                retry = runner.continue_team(task, notice)
                self.assertEqual(retry["state"], "queued")
                with self.factory() as runtime:
                    if change == "claim":
                        message, _, _ = runtime.team._claim(task)
                        self.assertEqual(message.seq, notice.seq)
                    else:
                        self.change_scope(runtime, task, change)
                runner._run(task, retry["id"], "run")
                self.assertEqual(runner.status(task)["state"], "failed")
                self.assertEqual(self.driver.calls, [])

    def test_restart_restores_the_same_unclaimed_retry_queue(self):
        task, _, notice, runner, _, failure = self.repaired_notice()
        retry = runner.continue_team(task, notice)
        replacement = ActivityRunner(self.factory)
        with patch.object(replacement, "_start") as start:
            replacement.recover_interrupted()
            restored = replacement.status(task)
            self.assertEqual(restored["id"], retry["id"])
            self.assertEqual(restored["recovery_retry_of"], failure.seq)
            self.assertEqual(len(self.events(task, "activity.queued")), 3)
            replacement.recover_interrupted()
            start.assert_called_once()
            replacement._run(task, retry["id"], "run")
        self.assertEqual(len(self.driver.calls), 1)
        self.assertEqual(replacement.status(task)["state"], "completed")

    def test_two_dispatchers_cannot_claim_the_same_failed_dispatch(self):
        task, _, notice, runner, _, failure = self.repaired_notice()
        competitor = ActivityRunner(self.factory)
        original = EventLog.emit
        entered = []

        def interleave(log, **kwargs):
            if kwargs["type"] == "activity.queued" and kwargs["payload"].get("recovery_retry_of") == failure.seq:
                entered.append(True)
                competitor.continue_team(task, notice)
            return original(log, **kwargs)

        with patch.object(competitor, "_start") as competitor_start:
            with patch.object(EventLog, "emit", new=interleave):
                runner.continue_team(task, notice)
            competitor.continue_team(task, notice)
            competitor_start.assert_not_called()
        self.assertEqual(entered, [True])
        retries = [e for e in self.events(task, "activity.queued") if "recovery_retry_of" in e.payload]
        self.assertEqual(len(retries), 1)
        self.assertEqual(retries[0].payload["recovery_retry_of"], failure.seq)

    def test_concurrent_runners_create_one_retry_queue_and_one_worker(self):
        task, _, notice, runner, start, failure = self.repaired_notice()
        competitor = ActivityRunner(self.factory)
        barrier = threading.Barrier(2)
        original = ActivityRunner._record_queued
        errors = []

        def synchronized(runner, task_id, activity, **kwargs):
            if "recovery_retry_of" in activity:
                barrier.wait(5)
            return original(runner, task_id, activity, **kwargs)

        def dispatch(target):
            try:
                target.continue_team(task, notice)
            except Exception as exc:
                errors.append(exc)

        with patch.object(competitor, "_start") as competitor_start:
            with patch.object(ActivityRunner, "_record_queued", new=synchronized):
                workers = [threading.Thread(target=dispatch, args=(target,)) for target in (runner, competitor)]
                for worker in workers:
                    worker.start()
                for worker in workers:
                    worker.join(8)
                    self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(start.call_count + competitor_start.call_count, 2)
            retries = [e for e in self.events(task, "activity.queued") if "recovery_retry_of" in e.payload]
            self.assertEqual(len(retries), 1)
            self.assertEqual(retries[0].payload["recovery_retry_of"], failure.seq)
            winner = runner if runner.status(task)["state"] == "queued" else competitor
            retry = winner.status(task)
            winner._run(task, retry["id"], "run")
        self.assertEqual(len(self.driver.calls), 1)

    def test_new_customer_between_validation_and_commit_prevents_queue(self):
        task, _, notice, runner, start, failure = self.repaired_notice()
        original = EventLog.emit
        entered = []

        def interleave(log, **kwargs):
            if (not entered and kwargs["type"] == "activity.queued"
                    and kwargs["payload"].get("recovery_retry_of") == failure.seq):
                entered.append(True)
                with self.factory() as runtime:
                    self.change_scope(runtime, task, "customer")
            return original(log, **kwargs)

        with patch.object(EventLog, "emit", new=interleave):
            self.assert_not_retried(task, notice, runner, start)
        self.assertEqual(entered, [True])

    def test_wrong_failure_reference_or_customer_message_cannot_use_retry_binding(self):
        task, _, notice, runner, _, failure = self.repaired_notice()
        for reference in (failure.seq + 1, True):
            with self.subTest(reference=reference), self.assertRaises(WorkConflict):
                runner._record_queued(task, {"id": "forged-retry", "action": "run", "team_message_seq": notice.seq,
                                             "recovery_retry_of": reference})
        with self.factory() as runtime:
            customer = runtime.team.receive(task, "Fresh request", request_id="retry-binding-customer")
        with self.assertRaises(WorkConflict):
            runner._record_queued(task, {"id": "forged-customer-retry", "action": "run",
                                        "team_message_seq": customer.seq, "recovery_retry_of": failure.seq})
        self.assertEqual(len(self.events(task, "activity.queued")), 2)

    def test_reused_identity_and_superseded_failure_cannot_bypass_retry_deduplication(self):
        task, _, notice, runner, _, failure = self.repaired_notice()
        with self.assertRaises(WorkConflict):
            runner._record_queued(task, {"id": failure.payload["activity_id"], "action": "run",
                                        "team_message_seq": notice.seq, "recovery_retry_of": failure.seq})
        retry = runner.continue_team(task, notice)
        for identity in (retry["id"], "duplicate-retry"):
            with self.subTest(identity=identity), self.assertRaises(WorkConflict):
                runner._record_queued(task, {"id": identity, "action": "run", "team_message_seq": notice.seq,
                                            "recovery_retry_of": failure.seq})
        later, _ = runner._record_failure(task, retry["id"], "run", error_type="OSError", message="Fixture preflight failure")
        runner._finish(task, retry["id"], "failed")
        with self.assertRaises(WorkConflict):
            runner._record_queued(task, {"id": "expired-reference", "action": "run", "team_message_seq": notice.seq,
                                        "recovery_retry_of": failure.seq})
        next_retry = runner.continue_team(task, notice)
        self.assertEqual(next_retry["recovery_retry_of"], later.seq)
        self.assertNotEqual(next_retry["id"], retry["id"])
        self.assertEqual(len(self.events(task, "activity.queued")), 4)


if __name__ == "__main__":
    unittest.main()
