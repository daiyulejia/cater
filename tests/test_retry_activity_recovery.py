from __future__ import annotations

import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from cater.activity_owner import activity_owner
from cater.events import Actor, EventLog
from cater.runtime import Runtime
from cater.web import ActivityRunner


ORCHESTRATOR = Actor.system("orchestrator")
GATEWAY = Actor.system("driver_gateway")


class RetryActivityRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.db = Path(temporary.name) / "retry-recovery.db"
        self.network = self.enterContext(patch.object(socket, "socket",
            side_effect=AssertionError("Real network forbidden")))
        self.run = self.enterContext(patch.object(Runtime, "collaborate", autospec=True))
        self.battle = self.enterContext(patch.object(Runtime, "battle", autospec=True))
        self.start = self.enterContext(patch.object(ActivityRunner, "_start"))
        self.enterContext(patch.object(ActivityRunner, "answer_conversations"))
        self.enterContext(patch.object(ActivityRunner, "answer_questions"))
        self.runner = ActivityRunner(self.factory)
        with self.factory() as runtime:
            self.task = runtime.create_task("Recover text retry waits without model replay")

    def tearDown(self):
        self.network.assert_not_called()

    def factory(self):
        return Runtime(self.db, drivers={})

    def emit(self, kind, payload, *, actor=GATEWAY, task_id=None):
        with self.factory() as runtime:
            return runtime.log.emit(type=kind, payload=payload, actor=actor,
                                    task_id=self.task if task_id is None else task_id)

    def events(self, *kinds, task_id=None):
        with self.factory() as runtime:
            return list(runtime.log.iter_events(task_id=self.task if task_id is None else task_id,
                                               event_types=kinds or None))

    def queue(self, activity_id, **kwargs):
        return self.emit("activity.queued", {"activity_id": activity_id, "action": "run",
                                             **kwargs.pop("payload", {})}, actor=kwargs.pop("actor", ORCHESTRATOR),
                         **kwargs)

    def schedule(self, activity_id, *, task_id=None, actor=GATEWAY, **payload):
        return self.emit("driver.retry_scheduled", {"activity_id": activity_id, "role": "dev",
            "driver": "api", "retry_index": 1, "retry_limit": 1, "delay_seconds": 5, **payload},
            task_id=task_id, actor=actor)

    def finish(self, started, *, task_id=None, actor=GATEWAY, **payload):
        return self.emit("driver.retry_finished", {"request_seq": started.seq,
            "role": started.payload["role"], "driver": started.payload["driver"],
            "status": "succeeded", "reason": "Fixture finish", **payload}, actor=actor, task_id=task_id)

    def terminal(self, activity_id, kind="activity.failed"):
        with self.factory() as runtime:
            return self.runner._record_terminal(runtime, self.task, activity_id, "run", kind)

    def assert_cancelled(self, *starts):
        finishes = [event for event in self.events("driver.retry_finished")
                    if event.actor == GATEWAY and event.payload.get("status") == "cancelled"]
        self.assertEqual({event.payload["request_seq"] for event in finishes}, {event.seq for event in starts})
        self.assertEqual(len(finishes), len(starts))
        for event in finishes:
            started = next(start for start in starts if start.seq == event.payload["request_seq"])
            self.assertEqual(event.payload["role"], started.payload["role"])
            self.assertEqual(event.payload["driver"], started.payload["driver"])
            self.assertTrue(any("\u4e00" <= char <= "\u9fff" for char in event.payload["reason"]))
        return finishes

    def test_worker_binds_activity_before_run_and_battle_with_or_without_scope(self):
        for action in ("run", "battle"):
            for scoped in (False, True):
                with self.subTest(action=action, scoped=scoped):
                    with self.factory() as runtime:
                        self.task = runtime.create_task("Worker activity binding")
                        scope = runtime.work.scope(self.task)
                    activity_id = f"{action}-{scoped}"
                    self.queue(activity_id, payload={"action": action,
                        **({"expected_scope": scope} if scoped else {})})
                    self.runner._activities[self.task] = {"id": activity_id, "action": action, "state": "queued"}
                    starts = []

                    def model(runtime, task_id, **kwargs):
                        self.assertEqual(runtime._active_activity_id, activity_id)
                        self.assertEqual(kwargs, {"expected_scope": scope} if scoped else {})
                        starts.append(self.schedule(runtime._active_activity_id))

                    selected = self.run if action == "run" else self.battle
                    selected.side_effect = model
                    self.runner._run(self.task, activity_id, action)
                    self.assertEqual(self.runner.status(self.task)["state"], "completed")
                    self.assertEqual(len(starts), 1)
                    self.assert_cancelled(*starts)

    def test_serial_recoverers_close_parent_and_followup_once_without_model_calls(self):
        self.runner.submit(self.task, "run")
        self.runner.continue_after_current(self.task, "battle")
        activity = self.runner.status(self.task)
        followup = activity["follow_up"]
        starts = [self.schedule(activity["id"]), self.schedule(activity["id"], role="reviewer"),
                  self.schedule(followup["id"])]
        for expected in (2, 0, 0):
            self.assertEqual(ActivityRunner(self.factory).recover_interrupted(), expected)
        failures = self.events("activity.failed")
        self.assertEqual(len(failures), 2)
        self.assertTrue(all(event.payload["error_type"] == "InterruptedActivity" for event in failures))
        finishes = self.assert_cancelled(*starts)
        for started in starts:
            failure = next(event for event in failures
                           if event.payload["activity_id"] == started.payload["activity_id"])
            finish = next(event for event in finishes if event.payload["request_seq"] == started.seq)
            self.assertLess(failure.seq, finish.seq)
        self.runner._run(self.task, activity["id"], "run")
        self.runner._run(self.task, followup["id"], "battle")
        self.run.assert_not_called()
        self.battle.assert_not_called()
        with self.factory() as runtime:
            self.assertTrue(runtime.verify_chain().ok)

    def test_live_parent_owner_protects_its_wait_and_queued_followup(self):
        self.queue("parent")
        self.queue("child", payload={"after_activity_id": "parent"})
        starts = [self.schedule("parent"), self.schedule("child")]
        with activity_owner(str(self.db), self.task, "parent") as owned:
            self.assertTrue(owned)
            for _ in range(2):
                self.assertEqual(ActivityRunner(self.factory).recover_interrupted(), 0)
            self.assertEqual(self.events("activity.failed", "driver.retry_finished"), [])
        self.assertEqual(ActivityRunner(self.factory).recover_interrupted(), 2)
        self.assert_cancelled(*starts)
        self.run.assert_not_called()
        self.battle.assert_not_called()

    def test_recovery_isolates_other_live_activities_in_same_and_other_tasks(self):
        self.queue("orphan")
        orphan = self.schedule("orphan")
        self.queue("live")
        self.schedule("live")
        with self.factory() as runtime:
            other = runtime.create_task("Another live task")
        self.queue("orphan", task_id=other)
        self.schedule("orphan", task_id=other)
        with activity_owner(str(self.db), self.task, "live") as live:
            with activity_owner(str(self.db), other, "orphan") as other_live:
                self.assertTrue(live and other_live)
                for expected in (1, 0):
                    self.assertEqual(ActivityRunner(self.factory).recover_interrupted(), expected)
                self.assert_cancelled(orphan)
                self.assertEqual([event.payload["activity_id"] for event in self.events("activity.failed")], ["orphan"])
                self.assertEqual(self.events("driver.retry_finished", "activity.failed", task_id=other), [])
        self.run.assert_not_called()
        self.battle.assert_not_called()

    def test_untrusted_or_unbound_schedules_are_not_cancelled(self):
        self.schedule("target")  # A later queue cannot authorize an earlier wait.
        self.queue("target")
        valid = self.schedule("target")
        for actor in (Actor.agent("driver_gateway", "driver_gateway"),
                      Actor("forged", "driver_gateway", "system"), Actor("driver_gateway", "forged", "system")):
            self.schedule("target", actor=actor)
        self.schedule("unrelated")
        self.schedule(None)
        self.queue("forged-queue", actor=Actor.agent("orchestrator", "orchestrator"))
        self.schedule("forged-queue")
        with self.factory() as runtime:
            other = runtime.create_task("Foreign evidence")
        self.queue("foreign-queue", task_id=other)
        self.schedule("foreign-queue")
        self.schedule("target", task_id=other)
        self.terminal("target")
        self.terminal("forged-queue")
        self.terminal("foreign-queue")
        self.terminal("unrelated")
        self.assert_cancelled(valid)
        self.assertEqual(self.events("driver.retry_finished", task_id=other), [])

    def test_forged_or_mismatched_finishes_cannot_hide_a_pending_wait(self):
        self.queue("target")
        started = self.schedule("target")
        for actor in (Actor.agent("driver_gateway", "driver_gateway"),
                      Actor("forged", "driver_gateway", "system"), Actor("driver_gateway", "forged", "system")):
            self.finish(started, actor=actor)
        with self.factory() as runtime:
            other = runtime.create_task("Foreign finish")
        self.finish(started, task_id=other)
        for payload in ({"role": "other"}, {"driver": "other"}, {"request_seq": str(started.seq)},
                        {"request_seq": True}, {"status": "waiting"}):
            self.finish(started, **payload)
        self.emit("activity.failed", {"activity_id": "target"}, actor=Actor.agent("orchestrator", "orchestrator"))
        self.assertEqual(ActivityRunner(self.factory).recover_interrupted(), 1)
        self.assert_cancelled(started)
        self.run.assert_not_called()
        self.battle.assert_not_called()

    def test_finished_retries_and_existing_activity_terminal_are_not_duplicated(self):
        self.queue("target")
        for status in ("succeeded", "failed", "cancelled"):
            self.finish(self.schedule("target"), status=status)
        before = self.events("driver.retry_finished")
        terminal, created = self.terminal("target", "activity.completed")
        self.assertTrue(created)
        duplicate, created = self.terminal("target")
        self.assertFalse(created)
        self.assertEqual(duplicate, terminal)
        self.assertEqual(ActivityRunner(self.factory).recover_interrupted(), 0)
        self.assertEqual(self.events("driver.retry_finished"), before)
        self.assertEqual(self.events("activity.failed"), [])
        self.assertEqual(len(self.events("activity.completed")), 1)

    def test_terminal_and_retry_finish_roll_back_together_on_write_failure(self):
        self.queue("target")
        started = self.schedule("target")
        original = EventLog.emit

        def emit(log, **kwargs):
            if kwargs["type"] == "driver.retry_finished":
                self.assertTrue(log.db.in_transaction)
                self.assertEqual(len(list(log.iter_events(event_types=("activity.failed",)))), 1)
                raise OSError("Fixture retry finish storage unavailable")
            return original(log, **kwargs)

        with patch.object(EventLog, "emit", new=emit):
            with self.assertRaises(OSError):
                self.terminal("target")
        self.assertEqual(self.events("activity.failed", "driver.retry_finished"), [])
        self.assertEqual(ActivityRunner(self.factory).recover_interrupted(), 1)
        self.assertEqual(ActivityRunner(self.factory).recover_interrupted(), 0)
        self.assert_cancelled(started)
        self.run.assert_not_called()
        self.battle.assert_not_called()

    def test_real_worker_owner_keeps_wait_until_worker_exits(self):
        self.runner.submit(self.task, "run")
        activity = self.runner.status(self.task)
        ready, release = threading.Event(), threading.Event()
        starts, errors = [], []

        def model(runtime, task_id, **kwargs):
            starts.append(self.schedule(runtime._active_activity_id))
            ready.set()
            if not release.wait(5):
                raise AssertionError("Fixture worker release timed out")
            raise SystemExit("Fixture owner exit")

        def worker():
            try:
                self.runner._run(self.task, activity["id"], "run")
            except SystemExit:
                pass
            except BaseException as exc:
                errors.append(exc)
                ready.set()

        self.run.side_effect = model
        thread = threading.Thread(target=worker)
        thread.start()
        try:
            self.assertTrue(ready.wait(5))
            self.assertEqual(errors, [])
            for _ in range(2):
                self.assertEqual(ActivityRunner(self.factory).recover_interrupted(), 0)
            self.assertEqual(self.events("activity.failed", "driver.retry_finished"), [])
        finally:
            release.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(ActivityRunner(self.factory).recover_interrupted(), 1)
        self.assertEqual(ActivityRunner(self.factory).recover_interrupted(), 0)
        self.assert_cancelled(*starts)
        self.run.assert_called_once()
        self.battle.assert_not_called()


if __name__ == "__main__":
    unittest.main()
