import tempfile
import subprocess
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from cater.events import Actor, EventLog, HeadChanged
from cater.runtime import Runtime
from cater.task_requests import activation, create, prepare
from cater.web import ActivityRunner


class AtomicEventsTests(unittest.TestCase):
    def test_process_exit_cannot_leave_half_prepared_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "crash.db"
            script = (
                "import os,sys; from cater.runtime import Runtime; from cater.task_requests import prepare; "
                "rt=Runtime(sys.argv[1]); rt.log.db.execute('BEGIN IMMEDIATE'); "
                "prepare(rt, {'goal':'Crash before commit','run':True}, 'crash-request-001'); os._exit(73)"
            )
            result = subprocess.run([sys.executable, "-B", "-c", script, str(path)],
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 73, result.stderr)
            with Runtime(path) as rt:
                self.assertEqual(rt.log.count(), 0)
                task, created, intent = prepare(rt, {"goal": "Crash before commit", "run": True}, "crash-request-001")
                self.assertTrue(created)
                self.assertEqual(intent.task_id, task)
                self.assertTrue(rt.verify_chain().ok)

    def test_rollback_including_nested_batches_and_signature(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.db"
            log = EventLog(path, audit_key="test-only-key")
            self.addCleanup(log.close)
            with self.assertRaisesRegex(RuntimeError, "fail"):
                with log.atomic():
                    log.emit(actor=Actor.human(), type="task.created", task_id="one")
                    with log.atomic():
                        log.emit(actor=Actor.system(), type="task.started", task_id="one")
                    raise RuntimeError("fail")
            self.assertEqual(log.count(), 0)
            with log.atomic():
                first = log.emit(actor=Actor.human(), type="task.created", task_id="two")
                with self.assertRaises(HeadChanged):
                    log.emit(actor=Actor.system(), type="task.started", expected_head="wrong")
                log.emit(actor=Actor.system(), type="task.started", task_id="two", expected_head=first.hash)
                reader = EventLog(path, audit_key="test-only-key")
                try:
                    self.assertEqual(reader.count(), 0)
                finally:
                    reader.close()
            report = log.verify_chain()
            self.assertTrue(report.ok)
            self.assertEqual(report.signed, 2)
            log.close()


class TaskActivationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name) / "tasks.db"
        self.factory = lambda: Runtime(self.db)
        self.runner = ActivityRunner(self.factory)
        self.request = {"goal": "Write a small note", "run": True, "background": True}

    def prepare(self, **changes):
        with self.factory() as rt:
            return prepare(rt, {**self.request, **changes}, "activation-request-001")

    def events(self):
        with self.factory() as rt:
            self.assertTrue(rt.verify_chain().ok)
            return rt.log.read()

    def run_inline(self, runner):
        return patch.object(runner, "_start", side_effect=lambda task, item: runner._run(task, item["id"], item["action"]))

    def test_failed_initial_intake_rolls_back_entire_creation(self):
        original = Runtime.intake

        def fail(rt, task, **kwargs):
            original(rt, task, **kwargs)
            raise RuntimeError("crash during intake")

        with patch.object(Runtime, "intake", fail), self.assertRaises(RuntimeError):
            self.prepare()
        self.assertEqual(self.events(), [])
        task, created, intent = self.prepare()
        self.assertTrue(created)
        self.assertEqual(intent.task_id, task)
        with self.factory() as rt:
            self.assertIsNotNone(rt.task(task).plan)

    def test_creation_does_not_execute_model_under_write_lock(self):
        with patch.object(Runtime, "_complete_with_containment", side_effect=AssertionError("model invoked")):
            self.prepare()

    def test_no_run_request_does_not_create_execution_intent(self):
        task, _, intent = self.prepare(run=False)
        self.assertIsNone(intent)
        with self.factory() as rt:
            self.assertIsNone(activation(rt, task))
        with patch.object(Runtime, "collaborate") as run:
            self.runner.recover_interrupted()
        run.assert_not_called()

    def test_invalid_id_or_conflicting_mode_creates_nothing(self):
        with self.factory() as rt:
            for request, identity in ((self.request, ""), ({**self.request, "battle": True}, "valid-identity")):
                with self.assertRaises(ValueError):
                    prepare(rt, request, identity)
        self.assertEqual(self.events(), [])

    def test_restart_dispatches_committed_but_not_queued_intent_once(self):
        task, _, intent = self.prepare()
        with self.run_inline(self.runner), patch.object(Runtime, "collaborate") as run:
            self.runner.recover_interrupted()
            self.runner.recover_interrupted()
            self.runner.dispatch_activation(task, intent)
        run.assert_called_once()
        kinds = [e.type for e in self.events()]
        for kind in ("task.activation_requested", "activity.queued", "activity.started", "activity.completed"):
            self.assertEqual(kinds.count(kind), 1)

    def test_restart_resumes_queue_that_never_started(self):
        task, _, intent = self.prepare()
        with patch.object(self.runner, "_start"):
            self.runner.dispatch_activation(task, intent)
        new = ActivityRunner(self.factory)
        with self.run_inline(new), patch.object(Runtime, "collaborate") as run:
            new.recover_interrupted()
            new.recover_interrupted()
        run.assert_called_once()
        self.assertEqual(len([e for e in self.events() if e.type == "activity.queued"]), 1)

    def test_started_without_terminal_never_replays_on_restart(self):
        task, _, intent = self.prepare()
        with patch.object(self.runner, "_start"):
            self.runner.dispatch_activation(task, intent)
        queued = next(e for e in self.events() if e.type == "activity.queued")
        with self.factory() as rt:
            self.assertTrue(self.runner._claim_activation_start(rt, task, {**queued.payload, "queued_seq": queued.seq}))
        new = ActivityRunner(self.factory)
        with self.run_inline(new), patch.object(Runtime, "collaborate") as run:
            new.recover_interrupted()
            new.dispatch_activation(task, intent)
        run.assert_not_called()
        failed = [e for e in self.events() if e.type == "activity.failed"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0].payload["error_type"], "InterruptedActivity")

    def test_scope_change_blocks_queued_and_unqueued_activation(self):
        for queued in (False, True):
            with self.subTest(queued=queued):
                # Separate requests need separate task identities.
                with self.factory() as rt:
                    task, _, intent = prepare(rt, self.request, f"scope-change-{queued}")
                if queued:
                    with patch.object(self.runner, "_start"):
                        self.runner.dispatch_activation(task, intent)
                with self.factory() as rt:
                    rt.command(task, "Change the goal", goal="New scope")
                new = ActivityRunner(self.factory)
                with self.run_inline(new), patch.object(Runtime, "collaborate") as run:
                    new.recover_interrupted()
                run.assert_not_called()
                self.assertTrue(any(e.type == "activity.failed" and e.task_id == task for e in self.events()))

    def test_pause_then_resume_does_not_revive_initial_authorization(self):
        task, _, intent = self.prepare()
        with self.factory() as rt:
            rt.execution_control.change(task, "pause", request_id="pause-initial")
            rt.execution_control.change(task, "resume", request_id="resume-initial")
        # Dispatch only the old activation; the explicit resume has its own authorization.
        with self.run_inline(self.runner), patch.object(Runtime, "collaborate") as run:
            self.runner.dispatch_activation(task, intent)
        run.assert_not_called()
        self.assertTrue(any(e.type == "activity.failed" for e in self.events()))

    def test_repeated_http_identity_does_not_repeat_intake(self):
        first = self.prepare()
        head = self.events()[-1].hash
        second = self.prepare()
        self.assertEqual(first[0], second[0])
        self.assertFalse(second[1])
        self.assertEqual(first[2], second[2])
        self.assertEqual(self.events()[-1].hash, head)

    def test_legacy_creation_without_intent_is_not_inferred_as_execution(self):
        with self.factory() as rt:
            create(rt, self.request, "legacy-creation-001")
        with self.run_inline(self.runner), patch.object(Runtime, "collaborate") as run:
            self.runner.recover_interrupted()
        run.assert_not_called()

    def test_forged_intent_cannot_execute(self):
        with self.factory() as rt:
            task, _ = create(rt, self.request, "forged-creation-001")
            source = rt.log.read(task_id=task)[0]
            intent = rt.log.emit(type="task.activation_requested", task_id=task, actor=Actor.agent("bad", "worker"),
                payload={"creation_seq": source.seq, "creation_hash": source.hash, "action": "run",
                         "scope": rt.work.scope(task), "protocol": 1})
        with self.run_inline(self.runner), patch.object(Runtime, "collaborate") as run:
            self.runner.dispatch_activation(task, intent)
            self.runner.recover_interrupted()
        run.assert_not_called()

    def test_later_manual_execution_supersedes_undispatched_creation(self):
        task, _, intent = self.prepare()
        with patch.object(Runtime, "collaborate") as run:
            self.runner._submit(task, "run", queue_after_current=False, run_inline=True)
            self.runner.dispatch_activation(task, intent, background=False)
        run.assert_called_once()
        self.assertTrue(any(e.type == "activity.failed" and e.payload["error_type"] == "ActivationSuperseded"
                            for e in self.events()))

    def test_cancelled_queued_activation_never_resumes(self):
        task, _, intent = self.prepare()
        with patch.object(self.runner, "_start"):
            self.runner.dispatch_activation(task, intent)
        with self.factory() as rt:
            rt.execution_control.change(task, "cancel", request_id="cancel-initial-001")
        new = ActivityRunner(self.factory)
        with self.run_inline(new), patch.object(Runtime, "collaborate") as run:
            new.recover_interrupted()
        run.assert_not_called()
        self.assertTrue(any(e.type == "activity.failed" and e.payload["error_type"] == "ExecutionCancelled"
                            for e in self.events()))

    def test_start_claim_rejects_nonexistent_queue(self):
        task, _, intent = self.prepare()
        with self.factory() as rt:
            from cater.work import WorkConflict
            with self.assertRaises(WorkConflict):
                self.runner._claim_activation_start(rt, task, {"activity_id": "made-up", "queued_seq": 9999,
                    "action": "run", "activation_seq": intent.seq, "expected_scope": intent.payload["scope"]})

    def test_synchronous_battle_uses_same_durable_start(self):
        task, _, intent = self.prepare(run=False, battle=True, background=False)
        with patch.object(Runtime, "battle") as battle:
            self.runner.dispatch_activation(task, intent, background=False)
            self.runner.dispatch_activation(task, intent, background=False)
        battle.assert_called_once_with(task, expected_scope=intent.payload["scope"])
        self.assertEqual(self.runner.status(task)["state"], "completed")

    def test_start_thread_failure_is_terminal_and_not_retried(self):
        task, _, intent = self.prepare()
        with patch("cater.web.threading.Thread.start", side_effect=RuntimeError("Cannot start")):
            with self.assertRaisesRegex(RuntimeError, "Cannot start"):
                self.runner.dispatch_activation(task, intent)
        new = ActivityRunner(self.factory)
        with self.run_inline(new), patch.object(Runtime, "collaborate") as run:
            new.recover_interrupted()
        run.assert_not_called()
        self.assertEqual(len([e for e in self.events() if e.type == "activity.failed"]), 1)

    def test_concurrent_dispatch_and_start_claim_execute_once(self):
        task, _, intent = self.prepare()
        barrier = threading.Barrier(2)

        def queue(_):
            runner = ActivityRunner(self.factory)
            with patch.object(runner, "_start"):
                barrier.wait(timeout=5)
                runner.dispatch_activation(task, intent)

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(queue, range(2)))
        queues = [e for e in self.events() if e.type == "activity.queued"]
        self.assertEqual(len(queues), 1)
        binding = {**queues[0].payload, "queued_seq": queues[0].seq}
        barrier = threading.Barrier(2)

        def claim(_):
            with self.factory() as rt:
                barrier.wait(timeout=5)
                return self.runner._claim_activation_start(rt, task, binding)

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(claim, range(2))), [False, True])


if __name__ == "__main__":
    unittest.main()
