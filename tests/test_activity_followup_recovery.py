from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from cater.events import Actor, EventLog
from cater.runtime import ExecutionScopeChanged, Runtime
from cater.web import ActivityRunner
from cater.work import WorkConflict


ORCHESTRATOR = Actor.system("orchestrator")
_START = ActivityRunner._start


class ActivityFollowupRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.db = Path(temporary.name) / "activity-recovery.db"
        with self.factory() as runtime:
            self.task = runtime.create_task("Recover queued activity authorizations")
        self.runner = ActivityRunner(self.factory)
        self.start = self.enterContext(patch.object(ActivityRunner, "_start"))
        self.run = self.enterContext(patch.object(Runtime, "collaborate"))
        self.battle = self.enterContext(patch.object(Runtime, "battle"))
        self.enterContext(patch.object(ActivityRunner, "answer_conversations"))
        self.enterContext(patch.object(ActivityRunner, "answer_questions"))
        self.control_count = 0

    def factory(self):
        return Runtime(self.db)

    def events(self, *kinds):
        with self.factory() as runtime:
            return list(runtime.log.iter_events(task_id=self.task, event_types=kinds or None))

    def authorize(self, *, direction=False):
        with self.factory() as runtime:
            received = runtime.conversation.receive(self.task, "Continue from the current direction")
            before = runtime.work.scope(self.task)
            command = runtime.log.emit(type="human.commanded", actor=Actor.human(), task_id=self.task,
                payload={"conversation_seq": received.seq, "instruction": "Continue",
                         "intent": "direction" if direction else "resume", "next_action": "run"})
            finished = runtime.log.emit(type="conversation.finished", actor=Actor.system("conversation"),
                task_id=self.task, payload={"conversation_seq": received.seq, "scope_before": before,
                    "command_seq": command.seq, "status": "finished", "next_action": "run",
                    "action": "update_task" if direction else "resume_task"})
        return {**finished.payload, "finish_seq": finished.seq}

    def enqueue_pair(self):
        first = self.authorize()
        self.runner.dispatch_conversation(self.task, first)
        second = self.authorize()
        self.runner.dispatch_conversation(self.task, second)
        activity = self.runner.status(self.task)
        return activity, activity["follow_up"], first, second

    def execute(self, activity=None, *, runner=None):
        runner = runner or self.runner
        activity = activity or runner.status(self.task)
        runner._run(self.task, activity["id"], activity["action"])

    def terminal(self, activity_id):
        rows = [event for event in self.events("activity.completed", "activity.failed")
                if event.actor == ORCHESTRATOR and event.payload.get("activity_id") == activity_id]
        self.assertEqual(len(rows), 1, f"expected exactly one terminal for {activity_id}")
        return rows[0]

    def fail_writes(self, activity_id):
        original = EventLog.emit

        def emit(log, **kwargs):
            if kwargs["type"] == "activity.failed" and kwargs["payload"]["activity_id"] == activity_id:
                raise OSError("terminal storage unavailable")
            return original(log, **kwargs)

        return patch.object(EventLog, "emit", new=emit)

    def orphan_call(self, kind, *, status=None, digest="old-version", **extra):
        with self.factory() as runtime:
            actor = runtime._leader_actor() if kind == "planning" else Actor.agent("reviewer", "delivery_reviewer")
            payload = ({"scope": runtime.work.scope(self.task)} if kind == "planning" else
                       {"delivery_digest": digest, "lease_until": 0, "check_seqs": {},
                        "scope": runtime.work.scope(self.task)})
            if kind == "delivery":
                plan = runtime.log.emit(type="work.plan_created", task_id=self.task,
                    actor=Actor.system("work_scheduler"), payload={"items": [], "scope": payload["scope"],
                        "source_seq": payload["scope"]["direction_seq"]})
                payload["plan_seq"] = plan.seq
                runtime.log.emit(type="delivery.prepared", task_id=self.task, actor=Actor.system("delivery"),
                    payload={"digest": digest, "reviewer": "reviewer",
                             "manifest": {"scope": payload["scope"], "plan_seq": plan.seq,
                                          "criteria": [], "items": [], "files": [], "sources": []}})
            payload.update(extra)
            started = runtime.log.emit(type=kind + (".started" if kind == "planning" else ".review_started"),
                task_id=self.task, actor=actor, payload=payload)
            if status is not None:
                from cater.planning import Planner

                finish = (Planner._finish_payload(started, status, "Fixture response") if kind == "planning" else
                          runtime.delivery._finish_payload(started, {"status": status, "reason": "Fixture response"}))
                runtime.log.emit(type=kind + (".finished" if kind == "planning" else ".review_finished"),
                    task_id=self.task, actor=actor, payload=finish)
            return started

    def control(self, action):
        self.control_count += 1
        with self.factory() as runtime:
            return runtime.execution_control.change(self.task, action,
                request_id=f"activity-control-{self.control_count}")[0]

    def test_ordinary_failures_close_followup_with_authorization_and_predecessor(self):
        for error in (WorkConflict("conflict"), TimeoutError("unknown effect"), RuntimeError("failed")):
            with self.subTest(error=type(error).__name__):
                activity, followup, _, second = self.enqueue_pair()
                starts = self.start.call_count
                self.run.side_effect = error
                self.execute(activity)
                self.assertEqual(self.start.call_count, starts)
                self.assertEqual(self.runner.status(self.task)["id"], activity["id"])
                self.assertNotIn("follow_up", self.runner.status(self.task))
                failure = self.terminal(followup["id"])
                self.assertEqual(failure.type, "activity.failed")
                self.assertEqual(failure.payload["error_type"], "PredecessorActivityFailed")
                self.assertEqual(failure.payload["after_activity_id"], activity["id"])
                self.assertEqual(failure.payload["conversation_seq"], second["conversation_seq"])
                self.assertEqual(failure.payload["finished_seq"], second["finish_seq"])
                self.assertEqual(failure.payload["expected_scope"], followup["expected_scope"])
                self.assertFalse(failure.payload["execution_started"])
                self.assertEqual(self.events("activity.queued")[-1].seq, failure.payload["queued_seq"])
                self.assertLess(self.terminal(activity["id"]).seq, failure.seq)
                self.runner._finish(self.task, activity["id"], "failed")
                self.terminal(followup["id"])

    def test_revised_followup_terminal_refers_to_latest_queue_revision(self):
        activity, followup, _, second = self.enqueue_pair()
        latest = self.authorize(direction=True)
        self.runner.dispatch_conversation(self.task, latest)
        self.runner._fail_activity(self.task, activity["id"], "run", RuntimeError("interrupted"))
        failure = self.terminal(followup["id"])
        self.assertEqual(failure.payload["conversation_seq"], latest["conversation_seq"])
        self.assertEqual(failure.payload["queued_seq"], self.events("activity.queued")[-1].seq)
        self.assertEqual(failure.payload["expected_scope"]["direction_seq"], latest["command_seq"])
        count = len(self.events("activity.queued"))
        self.runner.dispatch_conversation(self.task, second)
        self.runner.dispatch_conversation(self.task, latest)
        self.assertEqual(len(self.events("activity.queued")), count)
        self.assertEqual(ActivityRunner(self.factory).recover_interrupted(), 0)

    def test_unbound_battle_followup_also_has_a_durable_failure(self):
        self.runner.submit(self.task, "run")
        self.runner.continue_after_current(self.task, "battle")
        activity = self.runner.status(self.task)
        self.run.side_effect = RuntimeError("failed")
        self.execute(activity)
        failure = self.terminal(activity["follow_up"]["id"])
        self.assertEqual(failure.payload["action"], "battle")
        self.assertEqual(failure.payload["after_activity_id"], activity["id"])
        self.battle.assert_not_called()

    def test_failed_terminal_write_keeps_followup_and_prevents_replacement_until_repaired(self):
        activity, followup, _, _ = self.enqueue_pair()
        self.run.side_effect = RuntimeError("failed")
        with self.fail_writes(followup["id"]), self.assertLogs("cater.web", level="ERROR"):
            self.execute(activity)
            status = self.runner.status(self.task)
            self.assertEqual(status["state"], "failed")
            self.assertEqual(status["follow_up"]["id"], followup["id"])
            self.assertEqual(status["follow_up"]["state"], "blocked")
            with self.assertRaises(WorkConflict):
                self.runner.submit(self.task, "battle")
        self.assertEqual(self.start.call_count, 1)
        self.runner.submit(self.task, "battle")
        terminal = self.terminal(followup["id"])
        self.assertLess(terminal.seq, self.events("activity.queued")[-1].seq)
        self.assertEqual(self.start.call_count, 2)

    def test_restart_repairs_failed_followup_terminal_write_without_reexecution(self):
        activity, followup, _, second = self.enqueue_pair()
        self.run.side_effect = RuntimeError("failed")
        with self.fail_writes(followup["id"]), self.assertLogs("cater.web", level="ERROR"):
            self.execute(activity)
        restarted = ActivityRunner(self.factory)
        self.assertEqual(restarted.recover_interrupted(), 1)
        failure = self.terminal(followup["id"])
        self.assertEqual(failure.payload["error_type"], "InterruptedActivity")
        self.assertEqual(failure.payload["conversation_seq"], second["conversation_seq"])
        self.assertEqual(failure.payload["after_activity_id"], activity["id"])
        self.assertEqual(restarted.recover_interrupted(), 0)
        restarted.dispatch_conversation(self.task, second)
        self.assertEqual(self.start.call_count, 1)

    def test_parent_terminal_write_failure_still_closes_followup_and_recovers_parent(self):
        activity, followup, _, _ = self.enqueue_pair()
        self.run.side_effect = TimeoutError("unknown effect")
        with self.fail_writes(activity["id"]), self.assertLogs("cater.web", level="ERROR"):
            self.execute(activity)
        self.terminal(followup["id"])
        restarted = ActivityRunner(self.factory)
        self.assertEqual(restarted.recover_interrupted(), 1)
        self.assertEqual(self.terminal(activity["id"]).payload["error_type"], "InterruptedActivity")
        self.terminal(followup["id"])
        self.assertEqual(self.start.call_count, 1)

    def test_scope_rejection_cannot_continue_if_its_failure_record_was_not_persisted(self):
        activity, followup, _, _ = self.enqueue_pair()
        self.run.side_effect = ExecutionScopeChanged("stale authorization")
        with self.fail_writes(activity["id"]), self.assertLogs("cater.web", level="ERROR"):
            self.execute(activity)
        self.assertEqual(self.start.call_count, 1)
        self.assertEqual(self.terminal(followup["id"]).payload["error_type"], "PredecessorActivityFailed")

    def test_recovery_does_not_close_this_processes_live_activity_or_followup(self):
        activity, followup, _, _ = self.enqueue_pair()
        self.runner._mark_running(self.task, activity["id"])
        self.assertEqual(self.runner.recover_interrupted(), 0)
        self.assertEqual(self.runner.recover_interrupted(), 0)
        self.assertEqual(self.events("activity.failed"), [])
        self.assertEqual(self.runner.status(self.task)["follow_up"]["id"], followup["id"])
        self.execute(activity)
        self.execute()
        self.assertEqual(self.run.call_count, 2)
        self.terminal(activity["id"])
        self.terminal(followup["id"])

    def test_recovery_closes_every_old_queue_claim_once_and_delayed_threads_do_not_run(self):
        activity, followup, first, second = self.enqueue_pair()
        restarted = ActivityRunner(self.factory)
        self.assertEqual(restarted.recover_interrupted(), 2)
        self.assertEqual(restarted.recover_interrupted(), 0)
        self.terminal(activity["id"])
        self.terminal(followup["id"])
        self.execute(activity)
        self.execute(followup)
        restarted.dispatch_conversation(self.task, first)
        restarted.dispatch_conversation(self.task, second)
        self.run.assert_not_called()
        self.assertEqual(self.start.call_count, 1)

    def test_concurrent_recoverers_close_old_claim_once(self):
        self.runner.submit(self.task, "run")
        activity = self.runner.status(self.task)
        runners = [ActivityRunner(self.factory), ActivityRunner(self.factory)]
        barrier = threading.Barrier(2)
        counts, errors = [], []

        def recover(runner):
            try:
                barrier.wait(3)
                counts.append(runner.recover_interrupted())
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=recover, args=(runner,)) for runner in runners]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(sum(counts), 1)
        self.terminal(activity["id"])

    def test_recovery_ignores_untrusted_or_other_task_terminal_events(self):
        self.runner.submit(self.task, "run")
        activity = self.runner.status(self.task)
        with self.factory() as runtime:
            other = runtime.create_task("Other task")
            for task, actor in ((self.task, Actor.human()), (other, ORCHESTRATOR)):
                runtime.log.emit(type="activity.completed", task_id=task, actor=actor,
                    payload={"activity_id": activity["id"], "action": "run"})
        self.assertEqual(ActivityRunner(self.factory).recover_interrupted(), 1)
        self.terminal(activity["id"])

    def test_recovery_write_failure_does_not_abandon_other_tasks(self):
        self.runner.submit(self.task, "run")
        first = self.runner.status(self.task)
        with self.factory() as runtime:
            other = runtime.create_task("Other task")
        self.runner.submit(other, "run")
        restarted = ActivityRunner(self.factory)
        with self.fail_writes(first["id"]), self.assertLogs("cater.web", level="ERROR"):
            self.assertEqual(restarted.recover_interrupted(), 1)
        self.assertEqual(restarted.status(other)["state"], "failed")
        self.assertEqual(restarted.recover_interrupted(), 1)
        self.terminal(first["id"])

    def test_undispatched_message_sent_before_failure_is_terminal_without_execution(self):
        self.runner.submit(self.task, "run")
        activity = self.runner.status(self.task)
        authorization = self.authorize()
        self.run.side_effect = RuntimeError("failed")
        self.execute(activity)
        self.run.reset_mock(side_effect=True)
        restarted = ActivityRunner(self.factory)
        self.assertEqual(restarted.recover_interrupted(), 0)
        queued = restarted.status(self.task)
        self.assertEqual(queued["conversation_seq"], authorization["conversation_seq"])
        self.execute(queued, runner=restarted)
        self.run.assert_not_called()
        failure = self.terminal(queued["id"])
        self.assertIn(f"#{self.terminal(activity['id']).seq}", failure.payload["message"])
        restarted.dispatch_conversation(self.task, authorization)
        self.assertEqual(restarted.recover_interrupted(), 0)
        self.assertEqual(len(self.events("activity.queued")), 2)

    def test_recovered_interruption_also_blocks_old_undispatched_authorization(self):
        self.runner.submit(self.task, "run")
        self.authorize()
        restarted = ActivityRunner(self.factory)
        self.assertEqual(restarted.recover_interrupted(), 1)
        queued = restarted.status(self.task)
        self.execute(queued, runner=restarted)
        self.run.assert_not_called()
        self.assertEqual(self.terminal(queued["id"]).type, "activity.failed")

    def test_failed_recovery_write_cannot_unblock_an_undispatched_authorization(self):
        self.runner.submit(self.task, "run")
        old = self.runner.status(self.task)
        self.authorize()
        restarted = ActivityRunner(self.factory)
        with self.fail_writes(old["id"]), self.assertLogs("cater.web", level="ERROR"):
            self.assertEqual(restarted.recover_interrupted(), 0)
        queued = restarted.status(self.task)
        self.execute(queued, runner=restarted)
        self.run.assert_not_called()
        old_claim = next(event for event in self.events("activity.queued")
                         if event.payload["activity_id"] == old["id"])
        self.assertIn(f"#{old_claim.seq}", self.terminal(queued["id"]).payload["message"])
        self.assertEqual(restarted.recover_interrupted(), 1)

    def test_recovery_of_old_claim_preserves_current_worker_status(self):
        self.runner.submit(self.task, "run")
        old = self.runner.status(self.task)
        current_runner = ActivityRunner(self.factory)
        current_runner.submit(self.task, "run")
        current = current_runner.status(self.task)
        current_runner._mark_running(self.task, current["id"])
        self.assertEqual(current_runner.recover_interrupted(), 1)
        self.terminal(old["id"])
        self.assertEqual(current_runner.status(self.task)["id"], current["id"])
        self.assertEqual(current_runner.status(self.task)["state"], "running")

    def test_new_authorization_after_a_durable_failure_can_run(self):
        activity, followup, _, second = self.enqueue_pair()
        self.run.side_effect = RuntimeError("failed")
        self.execute(activity)
        self.run.reset_mock(side_effect=True)
        self.runner.dispatch_conversation(self.task, second)
        self.assertEqual(self.start.call_count, 1)
        fresh = self.authorize()
        self.runner.dispatch_conversation(self.task, fresh)
        self.execute()
        self.run.assert_called_once_with(self.task, expected_scope=fresh["scope_before"])
        self.assertEqual(self.runner.status(self.task)["state"], "completed")
        self.terminal(followup["id"])

    def test_orphan_planning_or_review_stops_recovered_dispatch_before_execution(self):
        for kind in ("planning", "delivery"):
            with self.subTest(kind=kind):
                with self.factory() as runtime:
                    self.task = runtime.create_task(kind)
                started = self.orphan_call(kind)
                self.authorize()
                restarted = ActivityRunner(self.factory)
                self.assertEqual(restarted.recover_interrupted(), 0)
                queued = restarted.status(self.task)
                self.execute(queued, runner=restarted)
                failure = self.terminal(queued["id"])
                self.assertIn(f"#{started.seq}", failure.payload["message"])
                self.run.assert_not_called()
                self.assertEqual(restarted.recover_interrupted(), 0)

    def test_normal_return_with_unresolved_call_is_not_completion_or_permission_for_followup(self):
        for kind in ("planning", "delivery"):
            with self.subTest(kind=kind):
                with self.factory() as runtime:
                    self.task = runtime.create_task(kind)
                activity, followup, _, _ = self.enqueue_pair()
                self.run.side_effect = lambda *args, **kwargs: self.orphan_call(kind)
                starts = self.start.call_count
                self.execute(activity)
                self.assertEqual(self.start.call_count, starts)
                self.assertEqual(self.terminal(activity["id"]).type, "activity.failed")
                self.assertEqual(self.terminal(followup["id"]).payload["error_type"], "PredecessorActivityFailed")
                self.assertEqual(self.events("activity.completed"), [])

    def test_review_unknown_is_not_cleared_by_another_digest_or_new_direction(self):
        started = self.orphan_call("delivery", status="uncertain")
        self.orphan_call("delivery", status="accepted", digest="new-version")
        authorization = self.authorize(direction=True)
        self.runner.dispatch_conversation(self.task, authorization)
        activity = self.runner.status(self.task)
        self.execute(activity)
        self.run.assert_not_called()
        self.assertIn(f"#{started.seq}", self.terminal(activity["id"]).payload["message"])

    def test_completed_planning_and_review_allow_queue_to_run(self):
        self.orphan_call("planning", status="blocked")
        self.orphan_call("delivery", status="rework")
        self.runner.dispatch_conversation(self.task, self.authorize())
        self.execute()
        self.run.assert_called_once()
        self.assertEqual(self.runner.status(self.task)["state"], "completed")

    def test_operator_reconciliation_allows_only_a_fresh_authorization(self):
        from cater.planning import Planner

        started = self.orphan_call("planning", status="uncertain")
        old = self.authorize()
        with self.factory() as runtime:
            evidence = runtime.log.emit(type="human.commanded", actor=Actor.human(), task_id=self.task,
                payload={"intent": "resume", "instruction": "Invocation stopped and effects checked"})
            ended = self.events("planning.finished")[-1]
            Planner(runtime).reconcile(self.task, attempt_seq=started.seq, attempt_hash=started.hash,
                expected_finish_seq=ended.seq, expected_scope=runtime.work.scope(self.task),
                expected_plan_seq=None, evidence_seqs=[evidence.seq], reason="Checked local effects",
                invocation_stopped=True, effects_reconciled=True)
        self.runner.dispatch_conversation(self.task, old)
        self.execute()
        self.run.assert_not_called()
        self.runner.dispatch_conversation(self.task, self.authorize())
        self.execute()
        self.run.assert_called_once()

    def test_expired_attested_planning_is_recovered_but_requires_fresh_queue_authorization(self):
        from cater.planning import _TEXT_CONTRACTS

        started = self.orphan_call("planning", lease_until=0,
            execution_contract=_TEXT_CONTRACTS["AnthropicMessagesDriver"])
        activity, followup, _, second = self.enqueue_pair()
        self.execute(activity)
        self.run.assert_not_called()
        recovered = self.events("planning.finished")[-1]
        self.assertEqual(recovered.actor, Actor.system("planning_recovery"))
        self.assertEqual(recovered.payload["attempt_hash"], started.hash)
        self.assertEqual(recovered.payload["status"], "interrupted")
        self.terminal(followup["id"])
        self.runner.dispatch_conversation(self.task, second)
        self.assertEqual(self.start.call_count, 1)
        self.runner.dispatch_conversation(self.task, self.authorize())
        self.execute()
        self.run.assert_called_once()

    def test_paused_or_cancelled_submit_has_terminal_without_starting(self):
        for action, state in (("pause", "paused"), ("cancel", "cancelled")):
            with self.subTest(action=action):
                with self.factory() as runtime:
                    self.task = runtime.create_task(action)
                control = self.control(action)
                authorization = self.authorize()
                self.runner.dispatch_conversation(self.task, authorization)
                activity = self.runner.status(self.task)
                terminal = self.terminal(activity["id"])
                self.assertEqual(terminal.payload["execution_state"], state)
                self.assertEqual(terminal.payload["control_seq"], control.seq)
                self.assertFalse(terminal.payload["execution_started"])
                self.runner.dispatch_conversation(self.task, authorization)
                self.assertEqual(len(self.events("activity.queued")), 1)
                self.start.assert_not_called()
                self.run.assert_not_called()

    def test_pause_after_queueing_prevents_execution_and_closes_followup(self):
        activity, followup, _, _ = self.enqueue_pair()
        control = self.control("pause")
        self.execute(activity)
        self.run.assert_not_called()
        self.assertEqual(self.start.call_count, 1)
        for queued in (activity, followup):
            terminal = self.terminal(queued["id"])
            self.assertEqual(terminal.payload["error_type"], "ExecutionPaused")
            self.assertEqual(terminal.payload["control_seq"], control.seq)

    def test_pause_scope_exception_does_not_start_followup(self):
        activity, followup, _, _ = self.enqueue_pair()

        def pause_then_reject(*args, **kwargs):
            self.control("pause")
            raise ExecutionScopeChanged("task paused")

        self.run.side_effect = pause_then_reject
        self.execute(activity)
        self.assertEqual(self.start.call_count, 1)
        self.assertEqual(self.terminal(activity["id"]).payload["error_type"], "ExecutionPaused")
        self.assertEqual(self.terminal(followup["id"]).payload["error_type"], "ExecutionPaused")

    def test_pause_does_not_hide_concurrent_unknown_invocation_failure(self):
        activity, followup, _, _ = self.enqueue_pair()

        def fail_while_paused(*args, **kwargs):
            self.control("pause")
            raise TimeoutError("invocation outcome unknown")

        self.run.side_effect = fail_while_paused
        self.execute(activity)
        failure = self.terminal(activity["id"])
        self.assertEqual(failure.payload["error_type"], "ExecutionPaused")
        self.assertEqual(failure.payload["cause_error_type"], "TimeoutError")
        self.assertEqual(failure.payload["cause_message"], "invocation outcome unknown")
        self.assertEqual(self.terminal(followup["id"]).payload["error_type"], "ExecutionPaused")
        self.assertEqual(self.start.call_count, 1)

    def test_cancellation_during_normal_return_preserves_result_and_stops_followup(self):
        activity, followup, _, _ = self.enqueue_pair()

        def return_after_cancel(*args, **kwargs):
            self.control("cancel")
            with self.factory() as runtime:
                runtime.log.emit(type="msg.said", actor=Actor.agent("worker", "worker"), task_id=self.task,
                                 payload={"text": "Already started invocation returned"})

        self.run.side_effect = return_after_cancel
        self.execute(activity)
        self.assertEqual(len(self.events("msg.said")), 1)
        self.assertEqual(self.start.call_count, 1)
        self.assertEqual(self.terminal(activity["id"]).payload["error_type"], "ExecutionCancelled")
        self.assertEqual(self.terminal(followup["id"]).payload["error_type"], "ExecutionCancelled")

    def test_finish_rechecks_pause_even_when_parent_completed_or_scope_changed(self):
        for state in ("completed", "failed"):
            with self.subTest(state=state):
                with self.factory() as runtime:
                    self.task = runtime.create_task(state)
                activity, followup, _, _ = self.enqueue_pair()
                self.control("pause")
                starts = self.start.call_count
                self.runner._finish(self.task, activity["id"], state, follow_after_failure=True)
                self.assertEqual(self.start.call_count, starts)
                self.assertEqual(self.terminal(followup["id"]).payload["error_type"], "ExecutionPaused")

    def test_recovery_closes_paused_queues_and_undispatched_messages_without_starting(self):
        activity, followup, _, _ = self.enqueue_pair()
        self.authorize()
        self.control("pause")
        restarted = ActivityRunner(self.factory)
        self.assertEqual(restarted.recover_interrupted(), 2)
        self.assertEqual(restarted.recover_interrupted(), 0)
        self.assertEqual(self.start.call_count, 1)
        for queued in (activity, followup, restarted.status(self.task)):
            self.assertEqual(self.terminal(queued["id"]).payload["error_type"], "ExecutionPaused")

    def test_recovery_keeps_inflight_worker_but_closes_its_cancelled_followup(self):
        activity, followup, _, _ = self.enqueue_pair()
        self.runner._mark_running(self.task, activity["id"])
        self.control("cancel")
        self.assertEqual(self.runner.recover_interrupted(), 1)
        self.assertEqual(self.runner.status(self.task)["state"], "running")
        self.assertNotIn("follow_up", self.runner.status(self.task))
        self.assertEqual(self.terminal(followup["id"]).payload["error_type"], "ExecutionCancelled")

    def test_same_runner_recovery_closes_never_started_paused_pair(self):
        activity, followup, _, _ = self.enqueue_pair()
        self.control("pause")
        self.assertEqual(self.runner.recover_interrupted(), 2)
        self.execute(activity)
        self.execute(followup)
        self.run.assert_not_called()
        self.assertEqual(self.runner.recover_interrupted(), 0)

    def test_resume_does_not_revive_authorization_sent_before_pause(self):
        old = self.authorize()
        self.control("pause")
        self.control("resume")
        self.runner.dispatch_conversation(self.task, old)
        self.start.assert_not_called()
        self.assertEqual(self.terminal(self.runner.status(self.task)["id"]).payload["error_type"], "ExecutionPaused")
        fresh = self.authorize()
        self.runner.dispatch_conversation(self.task, fresh)
        self.execute()
        self.run.assert_called_once()

    def test_fifo_chat_resume_is_authorized_when_both_messages_precede_pause_adoption(self):
        with self.factory() as runtime:
            pause = runtime.conversation.receive(self.task, "Pause")
            resume = runtime.conversation.receive(self.task, "Resume", continue_work=True)
            entry = runtime.execution_control.entry(self.task, "pause", request_id="fifo-chat-pause")
            entry["payload"]["conversation_seq"] = pause.seq
            runtime.log.emit(**entry)
            result = runtime.conversation._apply_command(resume,
                {"conversation_seq": resume.seq, "scope_before": runtime.work.scope(self.task),
                 "status": "finished", "action": "resume_task"}, {"action": "resume_task"}, {})
        self.runner.dispatch_conversation(self.task, result)
        self.execute()
        self.run.assert_called_once()
        self.assertEqual(self.runner.status(self.task)["state"], "completed")

    def test_explicit_resume_replaces_stopped_followup_and_can_continue_after_inflight_return(self):
        activity, followup, _, _ = self.enqueue_pair()

        def pause_and_resume(*args, **kwargs):
            self.control("pause")
            self.control("resume")
            self.runner.continue_after_current(self.task, "run")

        self.run.side_effect = pause_and_resume
        self.execute(activity)
        self.assertEqual(self.terminal(followup["id"]).payload["error_type"], "ExecutionPaused")
        self.assertEqual(self.terminal(activity["id"]).type, "activity.completed")
        fresh = self.runner.status(self.task)
        self.assertNotEqual(fresh["id"], followup["id"])
        self.assertEqual(fresh["state"], "queued")
        self.assertEqual(self.start.call_count, 2)
        self.run.side_effect = None
        self.execute(fresh)
        self.assertEqual(self.terminal(fresh["id"]).type, "activity.completed")

    def test_paused_submit_keeps_active_worker_and_closes_existing_followup(self):
        activity, followup, _, _ = self.enqueue_pair()
        self.runner._mark_running(self.task, activity["id"])
        self.control("pause")
        self.runner.continue_after_current(self.task, "battle")
        self.assertEqual(self.runner.status(self.task)["id"], activity["id"])
        self.assertEqual(self.runner.status(self.task)["state"], "running")
        self.assertNotIn("follow_up", self.runner.status(self.task))
        self.assertEqual(self.terminal(followup["id"]).payload["error_type"], "ExecutionPaused")
        last_queued = self.events("activity.queued")[-1]
        self.assertEqual(self.terminal(last_queued.payload["activity_id"]).payload["action"], "battle")
        self.assertEqual(self.start.call_count, 1)

    def test_installed_plan_closes_planning_claim_even_without_finished_event(self):
        started = self.orphan_call("planning")
        with self.factory() as runtime:
            runtime.log.emit(type="work.plan_created", actor=Actor.system("work_scheduler"), task_id=self.task,
                payload={"items": [], "scope": runtime.work.scope(self.task), "source_seq": 1,
                         "planning": {"attempt_seq": started.seq}})
        self.runner.dispatch_conversation(self.task, self.authorize())
        self.execute()
        self.run.assert_called_once()
        self.assertEqual(self.runner.status(self.task)["state"], "completed")

    def test_thread_constructor_failure_closes_the_claim(self):
        runner = ActivityRunner(self.factory)
        runner.submit(self.task, "run")
        activity = runner.status(self.task)
        with patch("cater.web.threading.Thread", side_effect=RuntimeError("cannot construct thread")):
            with self.assertRaisesRegex(RuntimeError, "cannot construct thread"):
                _START(runner, self.task, activity)
        self.assertEqual(self.terminal(activity["id"]).payload["error_type"], "RuntimeError")
        self.assertEqual(runner.status(self.task)["state"], "failed")

    def test_resume_commit_before_dispatch_is_recovered_and_claimed_once(self):
        self.control("pause")
        resume = self.control("resume")
        restarted = ActivityRunner(self.factory)
        self.assertEqual(restarted.recover_interrupted(), 0)
        activity = restarted.status(self.task)
        self.assertEqual(activity["resume_seq"], resume.seq)
        self.assertEqual(restarted.recover_interrupted(), 0)
        self.execute(activity, runner=restarted)
        restarted.dispatch_control(self.task, resume)
        self.assertEqual(ActivityRunner(self.factory).recover_interrupted(), 0)
        self.assertEqual(len(self.events("activity.queued")), 1)
        self.assertEqual(self.start.call_count, 1)
        self.run.assert_called_once_with(self.task, expected_scope=activity["expected_scope"])
        self.assertEqual(self.terminal(activity["id"]).type, "activity.completed")

    def test_resume_changed_false_retry_repairs_failure_before_durable_claim(self):
        self.control("pause")
        resume = self.control("resume")
        with patch.object(self.runner, "_record_queued", side_effect=OSError("schedule unavailable")):
            with self.assertRaises(OSError):
                self.runner.dispatch_control(self.task, resume)
        with self.factory() as runtime:
            retried, changed = runtime.execution_control.change(self.task, "resume",
                request_id=resume.payload["request_id"])
        self.assertFalse(changed)
        self.runner.dispatch_control(self.task, retried)
        self.runner.dispatch_control(self.task, retried)
        self.assertEqual(len(self.events("activity.queued")), 1)
        self.execute()
        self.run.assert_called_once()

    def test_resume_claim_survives_start_failure_without_retrying_execution(self):
        self.control("pause")
        resume = self.control("resume")
        with patch.object(self.runner, "_start", side_effect=OSError("process interrupted after claim")):
            with self.assertRaises(OSError):
                self.runner.dispatch_control(self.task, resume)
        activity = self.runner.status(self.task)
        restarted = ActivityRunner(self.factory)
        self.assertEqual(restarted.recover_interrupted(), 1)
        restarted.dispatch_control(self.task, resume)
        self.assertEqual(self.terminal(activity["id"]).payload["error_type"], "InterruptedActivity")
        self.assertEqual(len(self.events("activity.queued")), 1)
        self.run.assert_not_called()
        self.start.assert_not_called()

    def test_noop_resume_and_chat_resume_do_not_create_direct_dispatch(self):
        self.runner.dispatch_control(self.task, self.control("resume"))
        self.control("pause")
        with self.factory() as runtime:
            entry = runtime.execution_control.entry(self.task, "resume", request_id="chat-resume-control")
            entry["payload"]["conversation_seq"] = 123
            chat = runtime.log.emit(**entry)
        self.runner.dispatch_control(self.task, chat)
        self.runner.recover_interrupted()
        self.assertEqual(self.events("activity.queued"), [])
        self.start.assert_not_called()

    def test_delayed_resume_invalidated_by_pause_or_cancel_has_one_terminal(self):
        for action in ("pause", "cancel"):
            with self.subTest(action=action):
                with self.factory() as runtime:
                    self.task = runtime.create_task(action)
                self.control("pause")
                resume = self.control("resume")
                stop = self.control(action)
                self.runner.dispatch_control(self.task, resume)
                activity = self.runner.status(self.task)
                terminal = self.terminal(activity["id"])
                self.assertEqual(terminal.payload["resume_seq"], resume.seq)
                self.assertEqual(terminal.payload["control_seq"], stop.seq)
                self.assertFalse(terminal.payload["execution_started"])
                self.runner.dispatch_control(self.task, resume)
                self.runner.recover_interrupted()
                self.assertEqual(len(self.events("activity.queued")), 1)
        self.start.assert_not_called()

    def test_recovery_does_not_revive_old_resume_or_block_new_resume_with_its_no_execution_terminal(self):
        self.control("pause")
        old = self.control("resume")
        self.control("pause")
        fresh = self.control("resume")
        restarted = ActivityRunner(self.factory)
        restarted.recover_interrupted()
        old_claim = next(row for row in self.events("activity.queued") if row.payload["resume_seq"] == old.seq)
        self.assertEqual(self.terminal(old_claim.payload["activity_id"]).payload["error_type"], "ExecutionPaused")
        activity = restarted.status(self.task)
        self.assertEqual(activity["resume_seq"], fresh.seq)
        self.execute(activity, runner=restarted)
        self.run.assert_called_once()
        self.assertEqual(self.terminal(activity["id"]).type, "activity.completed")

    def test_resume_retires_paused_never_started_local_queue_before_scheduling_new_work(self):
        old, followup, _, _ = self.enqueue_pair()
        self.control("pause")
        resume = self.control("resume")
        self.runner.dispatch_control(self.task, resume)
        activity = self.runner.status(self.task)
        self.assertEqual(activity["resume_seq"], resume.seq)
        for queued in (old, followup):
            terminal = self.terminal(queued["id"])
            self.assertEqual(terminal.payload["error_type"], "ExecutionPaused")
            self.assertFalse(terminal.payload["execution_started"])
            self.execute(queued)
        self.run.assert_not_called()
        self.execute(activity)
        self.run.assert_called_once()
        self.assertEqual(self.terminal(activity["id"]).type, "activity.completed")

    def test_retired_worker_cannot_run_if_resume_arrives_after_preflight_before_mark_running(self):
        old = self.runner.submit(self.task, "run")
        check = self.runner._check_reconciliation

        def resume_during_preflight(*args, **kwargs):
            check(*args, **kwargs)
            self.control("pause")
            self.runner.dispatch_control(self.task, self.control("resume"))

        with patch.object(self.runner, "_check_reconciliation", side_effect=resume_during_preflight):
            self.execute(old)
        self.assertFalse(self.terminal(old["id"]).payload["execution_started"])
        self.run.assert_not_called()
        self.execute()
        self.run.assert_called_once()

    def test_unbound_legacy_resume_queue_cannot_be_replayed_by_startup_scan(self):
        self.control("pause")
        resume = self.control("resume")
        old = self.runner.continue_after_current(self.task, "run")
        self.execute(old)
        self.assertEqual(self.terminal(old["id"]).type, "activity.completed")
        self.run.reset_mock()
        restarted = ActivityRunner(self.factory)
        restarted.recover_interrupted()
        recovered = restarted.status(self.task)
        self.assertEqual(recovered["resume_seq"], resume.seq)
        self.execute(recovered, runner=restarted)
        self.run.assert_not_called()
        self.assertEqual(self.terminal(recovered["id"]).type, "activity.failed")
        self.control("pause")
        restarted.dispatch_control(self.task, self.control("resume"))
        self.execute(runner=restarted)
        self.run.assert_called_once()

    def test_resume_scope_change_after_claim_stops_execution(self):
        self.control("pause")
        resume = self.control("resume")
        activity = self.runner.dispatch_control(self.task, resume)
        self.authorize(direction=True)
        self.execute(activity)
        self.run.assert_not_called()
        self.assertEqual(self.terminal(activity["id"]).payload["error_type"], "ExecutionScopeChanged")

    def test_resume_scope_is_bound_before_direction_change_in_dispatch_and_restart_recovery(self):
        for restart in (False, True):
            with self.subTest(restart=restart):
                with self.factory() as runtime:
                    self.task = runtime.create_task("Original resume direction")
                self.start.reset_mock()
                self.run.reset_mock()
                self.control("pause")
                resume = self.control("resume")
                with self.factory() as runtime:
                    runtime.command(self.task, "Use the new direction", goal="New resume direction")
                    current_scope = runtime.work.scope(self.task)
                self.assertNotEqual(resume.payload["scope"], current_scope)
                runner = ActivityRunner(self.factory) if restart else self.runner
                if restart:
                    runner.recover_interrupted()
                else:
                    runner.dispatch_control(self.task, resume)
                rejected = runner.status(self.task)
                failure = self.terminal(rejected["id"])
                self.assertEqual(failure.payload["expected_scope"], resume.payload["scope"])
                self.assertEqual(failure.payload["error_type"], "ExecutionScopeChanged")
                self.assertFalse(failure.payload["execution_started"])
                runner.dispatch_control(self.task, resume)
                self.assertEqual(len(self.events("activity.queued")), 1)
                self.execute(rejected, runner=runner)
                self.start.assert_not_called()
                self.run.assert_not_called()
                self.control("pause")
                fresh = self.control("resume")
                self.assertEqual(fresh.payload["scope"], current_scope)
                runner.dispatch_control(self.task, fresh)
                self.execute(runner=runner)
                self.run.assert_called_once_with(self.task, expected_scope=current_scope)
                self.assertEqual(self.terminal(runner.status(self.task)["id"]).type, "activity.completed")

    def test_legacy_or_invalid_resume_scope_gets_durable_terminal_without_inferred_authorization(self):
        for scope in (None, {}, [], "invalid"):
            with self.subTest(scope=scope):
                with self.factory() as runtime:
                    self.task = runtime.create_task("Legacy resume authorization")
                self.control("pause")
                with self.factory() as runtime:
                    entry = runtime.execution_control.entry(self.task, "resume", request_id="legacy-scope-resume")
                    if scope is None:
                        entry["payload"].pop("scope")
                    else:
                        entry["payload"]["scope"] = scope
                    resume = runtime.log.emit(**entry)
                restarted = ActivityRunner(self.factory)
                restarted.recover_interrupted()
                activity = restarted.status(self.task)
                terminal = self.terminal(activity["id"])
                self.assertEqual(terminal.payload["resume_seq"], resume.seq)
                self.assertEqual(terminal.payload["error_type"], "MissingExecutionScope")
                self.assertFalse(terminal.payload["execution_started"])
                restarted.dispatch_control(self.task, resume)
                restarted.recover_interrupted()
                self.assertEqual(len(self.events("activity.queued")), 1)
                self.execute(activity, runner=restarted)
        self.start.assert_not_called()
        self.run.assert_not_called()

    def test_stale_resume_does_not_replace_new_direction_followup(self):
        self.control("pause")
        old_resume = self.control("resume")
        with self.factory() as runtime:
            runtime.command(self.task, "Use the new direction", goal="New queue direction")
        activity, followup, _, _ = self.enqueue_pair()
        self.runner.dispatch_control(self.task, old_resume)
        self.assertEqual(self.runner.status(self.task)["id"], activity["id"])
        self.assertEqual(self.runner.status(self.task)["follow_up"]["id"], followup["id"])
        rejected = self.events("activity.queued")[-1]
        self.assertEqual(self.terminal(rejected.payload["activity_id"]).payload["error_type"], "ExecutionScopeChanged")
        self.execute(activity)
        self.execute()
        self.assertEqual(self.run.call_count, 2)

    def test_dispatch_does_not_hide_queue_repair_conflict_as_idempotent_success(self):
        _, followup, _, _ = self.enqueue_pair()
        self.control("pause")
        resume = self.control("resume")
        with self.fail_writes(followup["id"]), self.assertLogs("cater.web", level="ERROR"):
            with self.assertRaises(WorkConflict):
                self.runner.dispatch_control(self.task, resume)
        self.assertFalse(any(row.payload.get("resume_seq") == resume.seq for row in self.events("activity.queued")))
        self.runner.dispatch_control(self.task, resume)
        self.execute()
        self.run.assert_called_once()

    def test_concurrent_resume_dispatchers_share_one_durable_claim(self):
        self.control("pause")
        resume = self.control("resume")
        barrier = threading.Barrier(2)
        original = ActivityRunner._record_queued
        errors = []

        def claim(runner, *args, **kwargs):
            barrier.wait(5)
            return original(runner, *args, **kwargs)

        def dispatch():
            try:
                ActivityRunner(self.factory).dispatch_control(self.task, resume)
            except Exception as exc:
                errors.append(exc)

        with patch.object(ActivityRunner, "_record_queued", new=claim):
            threads = [threading.Thread(target=dispatch) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(8)
                self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(self.events("activity.queued")), 1)
        self.assertEqual(self.start.call_count, 1)

    def test_invalid_same_owner_finish_cannot_clear_planning_or_review_unknown(self):
        for kind in ("planning", "delivery"):
            with self.subTest(kind=kind):
                with self.factory() as runtime:
                    self.task = runtime.create_task(kind)
                started = self.orphan_call(kind, status="uncertain")
                with self.factory() as runtime:
                    runtime.log.emit(type=kind + (".finished" if kind == "planning" else ".review_finished"),
                        task_id=self.task, actor=started.actor,
                        payload={**started.payload, "attempt_seq": started.seq, "attempt_hash": "wrong",
                                 "status": "blocked" if kind == "planning" else "accepted"})
                self.runner.dispatch_conversation(self.task, self.authorize())
                self.execute()
                self.assertIn(f"#{started.seq}", self.terminal(self.runner.status(self.task)["id"]).payload["message"])
        self.run.assert_not_called()

    def test_matching_hash_without_reconciliation_evidence_cannot_clear_unknown(self):
        for kind in ("planning", "delivery"):
            with self.subTest(kind=kind):
                with self.factory() as runtime:
                    self.task = runtime.create_task(kind)
                started = self.orphan_call(kind, status="uncertain")
                with self.factory() as runtime:
                    runtime.log.emit(type=kind + (".finished" if kind == "planning" else ".review_finished"),
                        task_id=self.task, actor=Actor.human(),
                        payload={**started.payload, "attempt_seq": started.seq, "attempt_hash": started.hash,
                                 "recovery": "operator_reconciled", "status": "interrupted"})
                self.runner.dispatch_conversation(self.task, self.authorize())
                self.execute()
                self.assertEqual(self.runner.status(self.task)["state"], "failed")
        self.run.assert_not_called()

    def test_review_finish_with_wrong_check_version_does_not_close_valid_start(self):
        started = self.orphan_call("delivery")
        with self.factory() as runtime:
            runtime.log.emit(type="delivery.review_finished", actor=started.actor, task_id=self.task,
                payload={**started.payload, "attempt_seq": started.seq, "status": "accepted",
                         "check_seqs": {"check": started.seq}})
        self.runner.dispatch_conversation(self.task, self.authorize())
        self.execute()
        self.run.assert_not_called()
        self.assertIn(f"#{started.seq}", self.terminal(self.runner.status(self.task)["id"]).payload["message"])

    def test_review_recovery_uses_prepared_bundle_and_all_task_evidence(self):
        started = self.orphan_call("delivery", status="uncertain")
        old = self.authorize()
        with self.factory() as runtime:
            evidence = runtime.log.emit(type="human.commanded", actor=Actor.human(), task_id=self.task,
                payload={"intent": "resume", "instruction": "Stopped invocation, reconciled effects"})
            ended = self.events("delivery.review_finished")[-1]
            runtime.delivery.reconcile(self.task, attempt_seq=started.seq, attempt_hash=started.hash,
                expected_finish_seq=ended.seq, expected_delivery_digest=started.payload["delivery_digest"],
                expected_scope=runtime.work.scope(self.task), expected_plan_seq=started.payload["plan_seq"],
                expected_check_seqs=started.payload["check_seqs"], evidence_seqs=[evidence.seq],
                reason="Checked local effects", invocation_stopped=True, effects_reconciled=True)
        self.runner.dispatch_conversation(self.task, old)
        self.execute()
        self.run.assert_not_called()
        self.runner.dispatch_conversation(self.task, self.authorize())
        self.execute()
        self.run.assert_called_once()
        self.assertEqual(self.runner.status(self.task)["state"], "completed")

    def test_expired_attested_review_closes_without_replay_then_allows_fresh_resume(self):
        from cater.planning import _TEXT_CONTRACTS

        started = self.orphan_call("delivery", execution_contract=_TEXT_CONTRACTS["AnthropicMessagesDriver"])
        self.control("pause")
        resume = self.control("resume")
        self.runner.dispatch_control(self.task, resume)
        self.execute()
        self.run.assert_not_called()
        recovered = self.events("delivery.review_finished")[-1]
        self.assertEqual(recovered.actor, Actor.system("delivery_recovery"))
        self.assertEqual(recovered.payload["attempt_hash"], started.hash)
        self.runner.dispatch_control(self.task, resume)
        self.assertEqual(self.start.call_count, 1)
        self.control("pause")
        self.runner.dispatch_control(self.task, self.control("resume"))
        self.execute()
        self.run.assert_called_once()

    def test_unknown_cli_contract_is_not_recovered_by_expired_text_lease(self):
        for kind in ("planning", "delivery"):
            with self.subTest(kind=kind):
                with self.factory() as runtime:
                    self.task = runtime.create_task(kind)
                self.orphan_call(kind, lease_until=0, execution_contract={"version": 1, "kind": "unknown"})
                self.runner.dispatch_conversation(self.task, self.authorize())
                self.execute()
                self.assertEqual(self.events("planning.finished", "delivery.review_finished"), [])
                self.assertEqual(self.runner.status(self.task)["state"], "failed")
        self.run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
