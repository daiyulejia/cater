"""Deterministic scheduling at activation transaction and ownership boundaries."""

import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from cater.activity_owner import activity_owner
from cater.events import Actor
from cater.runtime import Runtime
from cater.task_requests import prepare
from cater.web import ActivityRunner


class ActivationBoundaryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name) / "isolated.db"
        self.factory = lambda: Runtime(self.db)
        self.runner = ActivityRunner(self.factory)
        self.enterContext(patch.object(Runtime, "_complete_with_containment",
                                      side_effect=AssertionError("external call forbidden")))

    def prepare(self, *, battle=False):
        with self.factory() as rt:
            return prepare(rt, {"goal": "Original goal", "run": not battle, "battle": battle},
                           "review-activation-" + str(battle))

    def events(self):
        with self.factory() as rt:
            self.assertTrue(rt.verify_chain().ok)
            return rt.log.read()

    def test_changed_scope_between_claim_and_round_blocks_run_and_battle(self):
        for battle in (False, True):
            with self.subTest(battle=battle):
                task, _, intent = self.prepare(battle=battle)
                claim = self.runner._claim_activation_start

                def claim_then_change(rt, task_id, binding):
                    claimed = claim(rt, task_id, binding)
                    with self.factory() as other:
                        other.command(task_id, "Replace the goal", goal="Changed without new authorization")
                    return claimed

                with patch.object(self.runner, "_claim_activation_start", side_effect=claim_then_change), \
                        patch.object(Runtime, "_run_round") as run:
                    self.runner.dispatch_activation(task, intent, background=False)
                run.assert_not_called()
                terminals = [e for e in self.events() if e.task_id == task and
                             e.type in {"activity.completed", "activity.failed"}]
                self.assertEqual(len(terminals), 1)
                self.assertEqual(terminals[0].payload["error_type"], "ExecutionScopeChanged")

    def test_manual_completion_between_precheck_and_queue_supersedes_activation(self):
        task, _, intent = self.prepare()
        other = ActivityRunner(self.factory)
        record = self.runner._record_queued

        def queue_after_manual(task_id, item, **kwargs):
            other._submit(task_id, "run", queue_after_current=False, run_inline=True)
            return record(task_id, item, **kwargs)

        with patch.object(self.runner, "_record_queued", side_effect=queue_after_manual), \
                patch.object(Runtime, "collaborate") as run:
            self.runner.dispatch_activation(task, intent, background=False)
        run.assert_called_once_with(task)
        self.assertEqual(sum(e.type == "activity.completed" for e in self.events()), 1)
        failures = [e for e in self.events() if e.type == "activity.failed"]
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0].payload["error_type"], "ActivationSuperseded")
        self.assertFalse(failures[0].payload["execution_started"])
        self.assertEqual(self.runner.status(task)["state"], "failed")

    def test_execution_after_queue_is_rechecked_inside_start_claim(self):
        task, _, intent = self.prepare()
        with patch.object(self.runner, "_start"):
            self.runner.dispatch_activation(task, intent)
        queued = next(e for e in self.events() if e.type == "activity.queued")
        claim = self.runner._claim_activation_start

        def claim_after_other_work(rt, task_id, binding):
            with self.factory() as other:
                other.log.emit(type="agent.working", task_id=task_id, actor=Actor.agent("worker", "worker"))
            return claim(rt, task_id, binding)

        with patch.object(self.runner, "_claim_activation_start", side_effect=claim_after_other_work), \
                patch.object(Runtime, "collaborate") as run:
            self.runner._run(task, queued.payload["activity_id"], "run")
        run.assert_not_called()
        failure = next(e for e in self.events() if e.type == "activity.failed")
        self.assertEqual(failure.payload["error_type"], "ActivationSuperseded")

    def test_second_recoverer_does_not_fail_live_resumed_worker(self):
        task, _, intent = self.prepare()
        with patch.object(self.runner, "_start"):
            self.runner.dispatch_activation(task, intent)
        first, second = ActivityRunner(self.factory), ActivityRunner(self.factory)
        entered, release = threading.Event(), threading.Event()
        errors = []

        def blocked_run(rt, task_id, **kwargs):
            entered.set()
            if not release.wait(10):
                raise AssertionError("worker not released")

        def recover_first():
            try:
                first.recover_interrupted()
            except BaseException as exc:
                errors.append(exc)

        with patch.object(first, "_start", side_effect=lambda task_id, item:
                          first._run(task_id, item["id"], item["action"])), \
                patch.object(Runtime, "collaborate", blocked_run):
            thread = threading.Thread(target=recover_first)
            thread.start()
            try:
                self.assertTrue(entered.wait(5))
                for _ in range(3):
                    self.assertEqual(second.recover_interrupted(), 0)
                self.assertFalse(any(e.type == "activity.failed" for e in self.events()))
            finally:
                release.set()
                thread.join(10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual([e.type for e in self.events() if e.type in
                          {"activity.failed", "activity.completed"}], ["activity.completed"])

    def test_terminal_compare_and_set_has_one_winner(self):
        task, _, intent = self.prepare()
        with patch.object(self.runner, "_start"):
            self.runner.dispatch_activation(task, intent)
        item = self.runner.status(task)
        barrier = threading.Barrier(2)

        def finish(kind):
            with self.factory() as rt:
                barrier.wait(timeout=5)
                return self.runner._record_terminal(rt, task, item["id"], "run", kind)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(finish, ("activity.completed", "activity.failed")))
        self.assertEqual(sum(created for _, created in results), 1)
        self.assertEqual(results[0][0], results[1][0])
        self.assertEqual(sum(e.type in {"activity.failed", "activity.completed"} for e in self.events()), 1)

    def test_chat_followup_survives_live_parent_and_gapless_worker_handoff(self):
        task, _, intent = self.prepare()
        with patch.object(self.runner, "_start"):
            self.runner.dispatch_activation(task, intent)
        parent = self.runner.status(task)
        parent_entered, release_parent = threading.Event(), threading.Event()
        handoff_entered, reserve_child = threading.Event(), threading.Event()
        child_entered, release_child = threading.Event(), threading.Event()
        child_done = threading.Event()
        errors, child_threads = [], []
        original_run = self.runner._run
        original_start = self.runner._start
        other = ActivityRunner(self.factory)

        def delayed_reservation(task_id, item):
            handoff_entered.set()
            if not reserve_child.wait(10):
                raise AssertionError("handoff not released")
            original_start(task_id, item)

        def model(task_id, **kwargs):
            if not parent_entered.is_set():
                parent_entered.set()
                if not release_parent.wait(10):
                    raise AssertionError("parent not released")

        def delayed_child(*args, **kwargs):
            child_threads.append(threading.current_thread())
            try:
                child_entered.set()
                if not release_child.wait(10):
                    raise AssertionError("child not released")
                original_run(*args, **kwargs)
            except BaseException as exc:
                errors.append(exc)
            finally:
                child_done.set()

        def run_parent():
            try:
                original_run(task, parent["id"], "run")
            except BaseException as exc:
                errors.append(exc)

        with patch.object(Runtime, "collaborate", side_effect=model) as run, \
                patch.object(self.runner, "_run", side_effect=delayed_child), \
                patch.object(self.runner, "_start", side_effect=delayed_reservation), \
                patch.object(ActivityRunner, "answer_conversations"):
            thread = threading.Thread(target=run_parent)
            thread.start()
            try:
                self.assertTrue(parent_entered.wait(5))
                with self.factory() as rt:
                    received = rt.conversation.receive(task, "Continue current work")
                    scope = rt.work.scope(task)
                    command = rt.log.emit(type="human.commanded", actor=Actor.human(), task_id=task,
                        payload={"conversation_seq": received.seq, "instruction": "Continue",
                                 "intent": "resume", "next_action": "run"})
                    finished = rt.log.emit(type="conversation.finished", actor=Actor.system("conversation"),
                        task_id=task, payload={"conversation_seq": received.seq, "scope_before": scope,
                            "command_seq": command.seq, "status": "finished", "next_action": "run",
                            "action": "resume_task"})
                self.runner.dispatch_conversation(task, {**finished.payload, "finish_seq": finished.seq})
                child = self.runner.status(task)["follow_up"]
                for _ in range(3):
                    self.assertEqual(other.recover_interrupted(), 0)
                self.assertFalse(any(e.type == "activity.failed" for e in self.events()))
                release_parent.set()
                self.assertTrue(handoff_entered.wait(5))
                # Even a terminal parent still protects the not-yet-reserved child.
                self.assertTrue(any(e.type == "activity.completed" for e in self.events()))
                with activity_owner(str(self.db), task, child["id"]) as owned:
                    self.assertTrue(owned)
                self.assertEqual(other.recover_interrupted(), 0)
                reserve_child.set()
                self.assertTrue(child_entered.wait(5))
                thread.join(5)
                self.assertFalse(thread.is_alive())
                # The parent is gone and the child has not entered _run yet.
                with activity_owner(str(self.db), task, parent["id"]) as owned:
                    self.assertTrue(owned)
                with activity_owner(str(self.db), task, child["id"]) as owned:
                    self.assertFalse(owned)
                for _ in range(3):
                    self.assertEqual(other.recover_interrupted(), 0)
                self.assertFalse(any(e.type == "activity.failed" for e in self.events()))
            finally:
                release_parent.set()
                reserve_child.set()
                release_child.set()
                thread.join(10)
                for worker in child_threads:
                    worker.join(10)
            self.assertTrue(child_done.is_set())
            self.assertFalse(any(worker.is_alive() for worker in child_threads))
            self.assertEqual(run.call_count, 2)
        self.assertEqual(errors, [])
        terminals = [e for e in self.events() if e.type in {"activity.completed", "activity.failed"}]
        self.assertEqual([(e.payload["activity_id"], e.type) for e in terminals],
                         [(parent["id"], "activity.completed"), (child["id"], "activity.completed")])

    def test_start_failure_releases_reserved_owner(self):
        task, _, intent = self.prepare()
        with patch.object(self.runner, "_start"):
            self.runner.dispatch_activation(task, intent)
        item = self.runner.status(task)
        with patch("cater.web.threading.Thread.start", side_effect=RuntimeError("Cannot start")):
            with self.assertRaisesRegex(RuntimeError, "Cannot start"):
                self.runner._start(task, item)
        with activity_owner(str(self.db), task, item["id"]) as owned:
            self.assertTrue(owned)
        self.assertEqual(ActivityRunner(self.factory).recover_interrupted(), 0)
        terminals = [e for e in self.events() if e.type in {"activity.completed", "activity.failed"}]
        self.assertEqual(len(terminals), 1)
        self.assertEqual(terminals[0].payload["error_type"], "RuntimeError")

    def test_followup_recovers_after_process_death_on_either_side_of_handoff(self):
        script = """
import os, sys, threading
from pathlib import Path
from cater.runtime import Runtime
from cater.task_requests import activation
from cater.web import ActivityRunner
factory = lambda: Runtime(sys.argv[1])
runner = ActivityRunner(factory)
def exit_when_released():
    Path(sys.argv[3]).touch()
    sys.stdin.readline()
    os._exit(73)
def model(self, task, **kwargs):
    runner.continue_after_current(task, 'run')
    if sys.argv[4] == 'parent':
        exit_when_released()
Runtime.collaborate = model
original_run = runner._run
def delayed_child(*args, **kwargs):
    if '_ownership' in kwargs:
        exit_when_released()
    original_run(*args, **kwargs)
runner._run = delayed_child
with factory() as rt:
    intent = activation(rt, sys.argv[2])
runner.dispatch_activation(sys.argv[2], intent, background=False)
Path(sys.argv[3] + '.returned').touch()
threading.Event().wait()
"""
        for stage in ("parent", "child"):
            with self.subTest(stage=stage):
                with self.factory() as rt:
                    task, _, intent = prepare(rt, {"goal": "Follow-up ownership " + stage, "run": True},
                                              "followup-" + stage)
                ready = self.db.parent / stage
                process = subprocess.Popen([sys.executable, "-B", "-c", script, str(self.db), task,
                                            str(ready), stage], stdin=subprocess.PIPE,
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                try:
                    marker = ready if stage == "parent" else Path(str(ready) + ".returned")
                    deadline = time.monotonic() + 10
                    while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
                        time.sleep(0.02)
                    self.assertTrue(marker.exists(), "owner did not reach handoff barrier")
                    other = ActivityRunner(self.factory)
                    self.assertEqual(other.recover_interrupted(), 0)
                    self.assertFalse(any(e.type == "activity.failed" and e.task_id == task for e in self.events()))
                    _, stderr = process.communicate(input="exit\n", timeout=10)
                    self.assertEqual(process.returncode, 73, stderr)
                    with patch.object(Runtime, "collaborate") as run:
                        self.assertEqual(other.recover_interrupted(), 2 if stage == "parent" else 1)
                        self.assertEqual(other.recover_interrupted(), 0)
                    run.assert_not_called()
                    events = [e for e in self.events() if e.task_id == task]
                    queues = [e for e in events if e.type == "activity.queued"]
                    self.assertEqual(len(queues), 2)
                    for queued in queues:
                        terminal = [e for e in events if e.type in {"activity.completed", "activity.failed"}
                                    and e.payload["activity_id"] == queued.payload["activity_id"]]
                        self.assertEqual(len(terminal), 1)
                    followup_failure = next(e for e in events if e.type == "activity.failed"
                                            and "after_activity_id" in e.payload)
                    self.assertEqual(followup_failure.payload["error_type"], "InterruptedActivity")
                finally:
                    if process.poll() is None:
                        process.kill()
                    process.communicate(timeout=10)

    def test_simultaneous_recovery_executes_unstarted_activation_once(self):
        task, _, intent = self.prepare()
        with patch.object(self.runner, "_start"):
            self.runner.dispatch_activation(task, intent)
        barrier = threading.Barrier(2)

        def recover(_):
            runner = ActivityRunner(self.factory)
            with patch.object(runner, "_start", side_effect=lambda task_id, item:
                              runner._run(task_id, item["id"], item["action"])):
                barrier.wait(timeout=5)
                return runner.recover_interrupted()

        with patch.object(Runtime, "collaborate") as run, ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(list(pool.map(recover, range(2))), [0, 0])
        run.assert_called_once()
        self.assertEqual([e.type for e in self.events() if e.type in
                          {"activity.failed", "activity.completed"}], ["activity.completed"])

    def test_returning_worker_cannot_overwrite_existing_failure(self):
        task, _, intent = self.prepare()

        def fail_while_running(rt, task_id, **kwargs):
            item = self.runner.status(task_id)
            self.runner._record_failure(task_id, item["id"], "run", error_type="InjectedFailure", message="failure")

        with patch.object(Runtime, "collaborate", fail_while_running):
            self.runner.dispatch_activation(task, intent, background=False)
        self.assertEqual([e.type for e in self.events() if e.type in
                          {"activity.failed", "activity.completed"}], ["activity.failed"])
        self.assertEqual(self.runner.status(task)["state"], "failed")

    def test_dead_worker_thread_releases_owner_and_same_runner_recovers(self):
        task, _, intent = self.prepare()
        with patch.object(Runtime, "collaborate", side_effect=SystemExit("worker exited")):
            with self.assertRaises(SystemExit):
                self.runner.dispatch_activation(task, intent, background=False)
        with patch.object(Runtime, "collaborate") as run:
            self.assertEqual(self.runner.recover_interrupted(), 1)
        run.assert_not_called()
        failure = next(e for e in self.events() if e.type == "activity.failed")
        self.assertEqual(failure.payload["error_type"], "InterruptedActivity")

    def test_worker_initialization_failure_does_not_hide_unstarted_queue(self):
        task, _, intent = self.prepare()
        with patch.object(self.runner, "_start"):
            self.runner.dispatch_activation(task, intent)
        item = self.runner.status(task)
        with patch.object(self.runner, "_runtime_factory", side_effect=OSError("database unavailable")):
            with self.assertRaises(OSError):
                self.runner._run(task, item["id"], "run")
        self.assertEqual(self.runner.status(task)["state"], "observing")
        with patch.object(self.runner, "_start", side_effect=lambda task_id, activity:
                          self.runner._run(task_id, activity["id"], activity["action"])), \
                patch.object(Runtime, "collaborate") as run:
            self.runner.recover_interrupted()
        run.assert_called_once()
        self.assertEqual(self.runner.status(task)["state"], "completed")

    def test_start_failure_with_unavailable_storage_remains_recoverable(self):
        task, _, intent = self.prepare()
        with patch.object(self.runner, "_start"):
            self.runner.dispatch_activation(task, intent)
        item = self.runner.status(task)
        with patch("cater.web.threading.Thread.start", side_effect=RuntimeError("Cannot start")), \
                patch.object(self.runner, "_runtime_factory",
                             side_effect=[self.factory(), OSError("database unavailable")]), \
                self.assertLogs("cater.web", level="ERROR"):
            with self.assertRaisesRegex(RuntimeError, "Cannot start"):
                self.runner._start(task, item)
        self.assertEqual(self.runner.status(task)["state"], "observing")
        with patch.object(self.runner, "_start", side_effect=lambda task_id, activity:
                          self.runner._run(task_id, activity["id"], activity["action"])), \
                patch.object(Runtime, "collaborate") as run:
            self.runner.recover_interrupted()
        run.assert_called_once()
        self.assertEqual(self.runner.status(task)["state"], "completed")

    def test_live_process_is_preserved_and_dead_process_is_interrupted(self):
        task, _, intent = self.prepare()
        ready = self.db.parent / "ready"
        script = """
import os, sys
from pathlib import Path
from cater.runtime import Runtime
from cater.task_requests import activation
from cater.web import ActivityRunner
def block(self, task, **kwargs):
    Path(sys.argv[3]).touch()
    sys.stdin.readline()
    os._exit(73)
Runtime.collaborate = block
factory = lambda: Runtime(sys.argv[1])
with factory() as rt:
    intent = activation(rt, sys.argv[2])
ActivityRunner(factory).dispatch_activation(sys.argv[2], intent, background=False)
"""
        child = subprocess.Popen([sys.executable, "-B", "-c", script, str(self.db), task, str(ready)],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 10
            while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(ready.exists(), "child did not enter execution")
            self.assertEqual(self.runner.recover_interrupted(), 0)
            self.assertFalse(any(e.type == "activity.failed" for e in self.events()))
            _, stderr = child.communicate(input="exit\n", timeout=10)
            self.assertEqual(child.returncode, 73, stderr)
            with patch.object(Runtime, "collaborate") as run:
                self.assertEqual(self.runner.recover_interrupted(), 1)
                self.assertEqual(self.runner.recover_interrupted(), 0)
            run.assert_not_called()
            failure = next(e for e in self.events() if e.type == "activity.failed")
            self.assertEqual(failure.payload["error_type"], "InterruptedActivity")
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=10)

    def test_owner_process_exit_before_start_marker_still_allows_auto_recovery(self):
        task, _, intent = self.prepare()
        script = """
import os, sys
from cater.activity_owner import activity_owner
from cater.runtime import Runtime
from cater.task_requests import activation
from cater.web import ActivityRunner
factory = lambda: Runtime(sys.argv[1])
with factory() as rt:
    intent = activation(rt, sys.argv[2])
runner = ActivityRunner(factory)
runner._start = lambda *args: None
runner.dispatch_activation(sys.argv[2], intent)
item = runner.status(sys.argv[2])
with activity_owner(sys.argv[1], sys.argv[2], item['id']) as owned:
    assert owned
    os._exit(73)
"""
        result = subprocess.run([sys.executable, "-B", "-c", script, str(self.db), task],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 73, result.stderr)
        with patch.object(self.runner, "_start", side_effect=lambda task_id, item:
                          self.runner._run(task_id, item["id"], item["action"])), \
                patch.object(Runtime, "collaborate") as run:
            self.runner.recover_interrupted()
            self.runner.recover_interrupted()
        run.assert_called_once()
        self.assertEqual([e.type for e in self.events() if e.type in
                          {"activity.failed", "activity.completed"}], ["activity.completed"])


if __name__ == "__main__":
    unittest.main()
