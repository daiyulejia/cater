from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from cater.events import Actor
from cater.runtime import ExecutionScopeChanged, Runtime
from cater.web import ActivityRunner
from cater.work import WorkConflict


class ConversationSchedulingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.db = Path(temporary.name) / "scheduling.db"
        with self.factory() as rt:
            self.task = rt.create_task("Original goal")
        self.runner = ActivityRunner(self.factory)
        self.start = self.enterContext(patch.object(self.runner, "_start"))
        self.run = self.enterContext(patch.object(Runtime, "collaborate"))
        self.battle = self.enterContext(patch.object(Runtime, "battle"))

    def factory(self):
        return Runtime(self.db)

    def finished(self, *, action="resume_task", continue_work=True):
        with self.factory() as rt:
            received = rt.conversation.receive(self.task, "Requested change", continue_work=continue_work)
            started = rt.log.emit(type="conversation.started", task_id=self.task,
                actor=Actor.system("conversation"), payload={"conversation_seq": received.seq, "lease_until": 0})
            before = rt.work.scope(self.task)
            command = rt.log.emit(type="human.commanded", task_id=self.task, actor=Actor.human(),
                payload={"conversation_seq": received.seq, "instruction": "Requested change",
                         "intent": "direction" if action == "update_task" else "resume", "next_action": "run"})
            finished = rt.log.emit(type="conversation.finished", task_id=self.task,
                actor=Actor.system("conversation"), payload={"conversation_seq": received.seq,
                    "attempt_seq": started.seq, "scope_before": before,
                    "status": "finished", "action": action, "next_action": "run" if continue_work else "none"})
            return {**finished.payload, "finish_seq": finished.seq, "command_seq": command.seq}

    def events(self, kind):
        with self.factory() as rt:
            return list(rt.log.iter_events(task_id=self.task, event_types=(kind,)))

    def execute(self, activity=None):
        activity = activity or self.runner.status(self.task)
        self.runner._run(self.task, activity["id"], activity["action"])

    def recover(self, runner=None):
        runner = runner or self.runner
        with patch.object(runner, "answer_conversations"), patch.object(runner, "answer_questions"):
            return runner.recover_interrupted()

    def test_resume_is_bound_and_persisted_before_thread_start(self):
        result = self.finished()
        def assert_durable(task_id, activity):
            queued = self.events("activity.queued")
            self.assertEqual(len(queued), 1)
            self.assertEqual(queued[0].payload["activity_id"], activity["id"])
        self.start.side_effect = assert_durable
        self.runner.dispatch_conversation(self.task, result)
        queued = self.events("activity.queued")[0].payload
        self.assertEqual(queued["conversation_seq"], result["conversation_seq"])
        self.assertEqual(queued["expected_scope"], result["scope_before"])
        self.assertEqual(queued["finished_seq"], result["finish_seq"])
        self.execute()
        self.run.assert_called_once_with(self.task, expected_scope=result["scope_before"])
        self.assertEqual(self.runner.status(self.task)["state"], "completed")

    def test_update_uses_command_direction_not_previous_scope(self):
        result = self.finished(action="update_task")
        self.runner.dispatch_conversation(self.task, result)
        expected = {**result["scope_before"], "direction_seq": result["command_seq"]}
        self.assertEqual(self.events("activity.queued")[0].payload["expected_scope"], expected)
        self.execute()
        self.run.assert_called_once_with(self.task, expected_scope=expected)

    def test_a_authorization_cannot_run_b_direction_without_continue(self):
        result = self.finished(action="update_task")
        self.runner.dispatch_conversation(self.task, result)
        self.finished(action="update_task", continue_work=False)
        self.execute()
        self.run.assert_not_called()
        self.assertEqual(self.runner.status(self.task)["state"], "failed")
        self.assertEqual(self.events("activity.failed")[-1].payload["error_type"], "ExecutionScopeChanged")
        self.runner.dispatch_conversation(self.task, result)
        self.assertEqual(len(self.events("activity.queued")), 1)

    def test_followup_also_rechecks_scope_before_execution(self):
        self.runner._activities[self.task] = {"id": "current", "state": "running", "action": "run"}
        result = self.finished()
        self.runner.dispatch_conversation(self.task, result)
        self.finished(action="update_task", continue_work=False)
        self.runner._finish(self.task, "current", "completed")
        self.execute()
        self.run.assert_not_called()
        self.assertEqual(self.runner.status(self.task)["state"], "failed")

    def test_stale_a_starts_fresh_b_followup_once_without_replaying_a(self):
        first = self.finished(action="update_task")
        self.runner.dispatch_conversation(self.task, first)
        first_activity = self.runner.status(self.task)
        second = self.finished(action="update_task")
        self.runner.dispatch_conversation(self.task, second)
        second_id = self.runner.status(self.task)["follow_up"]["id"]
        self.execute(first_activity)
        self.run.assert_not_called()
        self.assertEqual(self.events("activity.failed")[-1].payload["activity_id"], first_activity["id"])
        self.assertEqual(self.runner.status(self.task)["id"], second_id)
        self.assertEqual(self.runner.status(self.task)["state"], "queued")
        self.start.assert_called_with(self.task, self.runner._activities[self.task])
        self.assertEqual(self.start.call_count, 2)
        self.execute()
        self.run.assert_called_once_with(self.task,
            expected_scope={**second["scope_before"], "direction_seq": second["command_seq"]})
        self.runner.dispatch_conversation(self.task, first)
        self.runner.dispatch_conversation(self.task, second)
        self.assertEqual(self.start.call_count, 2)
        self.assertEqual(len(self.events("activity.queued")), 2)
        self.assertEqual(self.runner.status(self.task)["state"], "completed")

    def test_runtime_entry_scope_rejection_also_preserves_queued_followup(self):
        self.runner.dispatch_conversation(self.task, self.finished())
        self.runner.dispatch_conversation(self.task, self.finished())
        second_id = self.runner.status(self.task)["follow_up"]["id"]
        self.run.side_effect = [ExecutionScopeChanged("entry authorization expired"), None]
        self.execute()
        self.assertEqual(self.runner.status(self.task)["id"], second_id)
        self.assertEqual(self.start.call_count, 2)
        self.execute()
        self.assertEqual(self.run.call_count, 2)
        self.assertEqual(self.runner.status(self.task)["state"], "completed")

    def test_other_failures_do_not_automatically_start_followup(self):
        for error in (WorkConflict("ordinary conflict"), TimeoutError("timeout"), RuntimeError("failed")):
            with self.subTest(error=type(error).__name__):
                self.runner.dispatch_conversation(self.task, self.finished())
                activity = self.runner.status(self.task)
                self.runner.dispatch_conversation(self.task, self.finished())
                starts = self.start.call_count
                self.run.side_effect = error
                self.execute(activity)
                self.assertEqual(self.start.call_count, starts)
                self.assertEqual(self.runner.status(self.task)["id"], activity["id"])
                self.assertEqual(self.runner.status(self.task)["state"], "failed")
                self.assertNotIn("follow_up", self.runner.status(self.task))

    def test_same_action_followup_rebinds_and_records_latest_authorization(self):
        self.runner._activities[self.task] = {"id": "current", "state": "running", "action": "run"}
        first = self.finished()
        self.runner.dispatch_conversation(self.task, first)
        second = self.finished(action="update_task")
        self.runner.dispatch_conversation(self.task, second)
        queued = self.events("activity.queued")
        self.assertEqual(len(queued), 2)
        self.assertEqual(queued[0].payload["activity_id"], queued[1].payload["activity_id"])
        self.assertEqual(self.runner.status(self.task)["follow_up"]["conversation_seq"], second["conversation_seq"])
        self.runner.dispatch_conversation(self.task, first)
        self.assertEqual(len(self.events("activity.queued")), 2)
        self.runner._finish(self.task, "current", "completed")
        self.execute()
        self.run.assert_called_once_with(self.task,
            expected_scope={**second["scope_before"], "direction_seq": second["command_seq"]})

    def test_legacy_run_does_not_pass_scope_keyword(self):
        self.runner.submit(self.task, "run")
        self.execute()
        self.run.assert_called_once_with(self.task)

    def test_recovery_dispatches_finished_without_rerunning_model(self):
        result = self.finished()
        with patch("cater.conversation.ConversationService.resolve_next", side_effect=AssertionError("model replay")):
            self.assertEqual(self.recover(), 0)
        self.start.assert_called_once()
        self.assertEqual(self.events("activity.queued")[0].payload["conversation_seq"], result["conversation_seq"])

    def test_recovery_marks_queued_interruption_failed_without_replaying(self):
        result = self.finished()
        self.runner.dispatch_conversation(self.task, result)
        restarted = ActivityRunner(self.factory)
        with patch.object(restarted, "_start") as start:
            self.assertEqual(self.recover(restarted), 1)
            self.recover(restarted)
            restarted.dispatch_conversation(self.task, result)
            start.assert_not_called()
        self.assertEqual(len(self.events("activity.queued")), 1)
        self.assertEqual(self.events("activity.failed")[-1].payload["error_type"], "InterruptedActivity")

    def test_completed_and_failed_authorizations_are_never_replayed(self):
        for fail in (False, True):
            result = self.finished()
            self.runner.dispatch_conversation(self.task, result)
            self.run.side_effect = RuntimeError("execution failed") if fail else None
            self.execute()
            self.runner.dispatch_conversation(self.task, result)
            restarted = ActivityRunner(self.factory)
            with patch.object(restarted, "_start") as start:
                self.recover(restarted)
                restarted.dispatch_conversation(self.task, result)
                start.assert_not_called()
        self.assertEqual(len(self.events("activity.queued")), 2)
        self.assertEqual(self.run.call_count, 2)

    def test_thread_start_failure_is_durable_and_not_replayed(self):
        result = self.finished()
        other = ActivityRunner(self.factory)
        with patch("cater.web.threading.Thread.start", side_effect=RuntimeError("thread unavailable")):
            with self.assertRaisesRegex(RuntimeError, "thread unavailable"):
                other.dispatch_conversation(self.task, result)
        self.assertEqual(other.status(self.task)["state"], "failed")
        self.assertEqual(self.events("activity.failed")[-1].payload["error_type"], "RuntimeError")
        with patch.object(other, "_start") as start:
            other.dispatch_conversation(self.task, result)
            self.recover(other)
            start.assert_not_called()
        self.assertEqual(len(self.events("activity.queued")), 1)

    def test_dispatch_uses_durable_result_not_caller_payload(self):
        result = self.finished(continue_work=False)
        self.runner.dispatch_conversation(self.task, {**result, "next_action": "run"})
        self.assertEqual(self.events("activity.queued"), [])
        self.runner.dispatch_conversation(self.task, {"conversation_seq": 999999, "next_action": "run"})
        self.assertEqual(self.events("activity.queued"), [])

    def test_missing_or_mismatched_command_is_not_authorization(self):
        result = self.finished()
        with self.factory() as rt:
            finish = rt.log.emit(type="conversation.finished", task_id=self.task,
                actor=Actor.system("conversation"), payload={**result, "conversation_seq": 99999})
        self.runner.dispatch_conversation(self.task, {**finish.payload, "finish_seq": finish.seq})
        self.assertEqual(self.events("activity.queued"), [])

    def test_rules_and_planning_scope_changes_also_invalidate_authorization(self):
        result = self.finished()
        self.runner.dispatch_conversation(self.task, result)
        with self.factory() as rt:
            rt.rulebook.record("A new task rule", scope="task", task_id=self.task)
        self.execute()
        self.run.assert_not_called()
        result = self.finished()
        self.runner.dispatch_conversation(self.task, result)
        with self.factory() as rt:
            rt.log.emit(type="planning.requested", actor=Actor.human(), task_id=self.task, payload={})
        self.execute()
        self.run.assert_not_called()
        self.assertEqual(len(self.events("activity.failed")), 2)

    def test_multiple_runners_claim_authorization_once(self):
        result = self.finished()
        runners = [ActivityRunner(self.factory), ActivityRunner(self.factory)]
        errors = []
        barrier = threading.Barrier(2)
        def dispatch(runner):
            try:
                barrier.wait(3)
                runner.dispatch_conversation(self.task, result)
            except Exception as exc:
                errors.append(exc)
        with patch.object(ActivityRunner, "_start") as start:
            threads = [threading.Thread(target=dispatch, args=(runner,)) for runner in runners]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(5)
                self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            start.assert_called_once()
        self.assertEqual(len(self.events("activity.queued")), 1)

    def test_explicit_followup_can_replace_binding_and_battle_keeps_legacy_behavior(self):
        self.runner._activities[self.task] = {"id": "current", "state": "running", "action": "run"}
        self.runner.dispatch_conversation(self.task, self.finished())
        self.runner.continue_after_current(self.task, "battle")
        self.assertNotIn("conversation_seq", self.runner.status(self.task)["follow_up"])
        self.runner._finish(self.task, "current", "completed")
        self.execute()
        self.battle.assert_called_once_with(self.task)


if __name__ == "__main__":
    unittest.main()
