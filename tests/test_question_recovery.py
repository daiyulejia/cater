from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from cater.drivers import DriverResponse
from cater.events import Actor, EventLog
from cater.explanations import ExplanationService
from cater.runtime import Runtime
from cater.web import ActivityRunner, MAX_QUESTION_QUEUE_ATTEMPTS, make_handler


_THREAD = threading.Thread


class RecoveryDriver:
    name = "recovery"

    def __init__(self):
        self.calls = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()

    def complete(self, *, role, prompt, context):
        work = context["work"]
        self.calls.append((context["task_id"], work["question"]))
        self.entered.set()
        if not self.release.wait(5):
            raise RuntimeError("test did not release the driver")
        source = next(s for s in work["sources"] if s["kind"] == "task")
        return DriverResponse(json.dumps({
            "status": "explained", "answer": "The recorded goal is the basis.",
            "evidence": [{"source_seq": source["seq"], "quote": source["text"]}],
            "limits": [],
        }), 3, {"token_usage": "reported"})


class QuestionRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "recovery.db"
        self.roles = Path(self.tmp.name) / "roles.json"
        config = json.loads(Path("cater/packs/default_roles.json").read_text(encoding="utf-8"))
        config["organization"]["leader"]["driver"] = "recovery"
        self.roles.write_text(json.dumps(config), encoding="utf-8")
        self.driver = RecoveryDriver()
        self.task = self.create_task("First group")
        self.request_count = 0
        self.threads = []
        self.thread_errors = []
        self.thread_patch = patch("cater.web.threading.Thread", side_effect=self.make_thread)
        self.thread_patch.start()
        self.addCleanup(self.thread_patch.stop)
        self.addCleanup(self.finish_threads)

    def factory(self):
        return Runtime(self.db, roles_path=self.roles, drivers={"recovery": self.driver},
                       close_drivers_on_close=False)

    def create_task(self, goal):
        with self.factory() as rt:
            return rt.create_task(goal)

    def ask(self, text, task=None):
        self.request_count += 1
        with self.factory() as rt:
            return rt.questions.ask(task or self.task, text,
                                    request_id=f"recovery-request-{self.request_count}")

    def rows(self, task=None):
        with self.factory() as rt:
            return rt.questions.view(task or self.task)

    def make_thread(self, *, target, args, **kwargs):
        def run():
            try:
                target(*args)
            except BaseException as exc:
                self.thread_errors.append(exc)

        thread = _THREAD(target=run, **kwargs)
        self.threads.append(thread)
        return thread

    def join_threads(self):
        for thread in self.threads:
            thread.join(5)
            self.assertFalse(thread.is_alive(), "question queue did not stop")
        self.assertEqual(self.thread_errors, [])

    def finish_threads(self):
        self.driver.release.set()
        self.join_threads()

    def mark_started(self, question, *, expired=False, status=None):
        with self.factory() as rt:
            started = rt.log.emit(type="question.started", task_id=question.task_id,
                actor=Actor.system("explainer"), payload={"question_seq": question.seq,
                    "lease_until": time.time() + (-60 if expired else 600)})
            if status:
                rt.log.emit(type="question.finished", task_id=question.task_id,
                    actor=Actor.system("explainer"), payload={"question_seq": question.seq,
                        "attempt_seq": started.seq, "status": status, "reason": "previous attempt"})

    def test_restart_resumes_pending_groups_in_order_without_changing_return_count(self):
        self.ask("first")
        self.ask("second")
        other = self.create_task("Second group")
        self.ask("other", other)
        runner = ActivityRunner(self.factory)
        self.assertEqual(runner.recover_interrupted(), 0)
        self.join_threads()
        self.assertEqual([q for task, q in self.driver.calls if task == self.task], ["first", "second"])
        self.assertEqual([q for task, q in self.driver.calls if task == other], ["other"])
        self.assertEqual([r["status"] for r in self.rows()], ["answered", "answered"])
        self.assertEqual(self.rows(other)[0]["status"], "answered")
        self.assertEqual(runner._question_tasks, {})

    def test_restart_never_replays_live_expired_finished_or_uncertain_attempts(self):
        self.ask("already answered")
        with self.factory() as rt:
            rt.questions.answer_next(self.task)
        self.mark_started(self.ask("live attempt"))
        self.mark_started(self.ask("expired attempt"), expired=True)
        self.mark_started(self.ask("uncertain result"), status="uncertain")
        started_only = self.create_task("Only previously started questions")
        self.mark_started(self.ask("started in other group", started_only), expired=True)
        self.ask("never started")
        runner = ActivityRunner(self.factory)
        self.assertEqual(runner.recover_interrupted(), 0)
        self.join_threads()
        self.assertEqual([q for _, q in self.driver.calls], ["already answered", "never started"])
        self.assertEqual([r["status"] for r in self.rows()],
                         ["answered", "answering", "uncertain", "uncertain", "answered"])
        self.assertEqual(len(self.threads), 1)
        with self.factory() as rt:
            self.assertEqual(len(list(rt.log.iter_events(event_types=("question.started",)))), 6)
        self.assertEqual(runner.recover_interrupted(), 0)
        self.assertEqual(len(self.threads), 1)

    def test_execution_recovery_count_and_failure_records_remain_separate(self):
        self.ask("pending question")
        with self.factory() as rt:
            for activity, terminal in (("open", None), ("done", "completed"), ("failed", "failed")):
                payload = {"activity_id": activity, "action": "run"}
                rt.log.emit(type="activity.queued", task_id=self.task,
                            actor=Actor.system("orchestrator"), payload=payload)
                if terminal:
                    rt.log.emit(type="activity." + terminal, task_id=self.task,
                                actor=Actor.system("orchestrator"), payload=payload)
        runner = ActivityRunner(self.factory)
        self.assertEqual(runner.recover_interrupted(), 1)
        self.join_threads()
        self.assertEqual(runner.status(self.task)["id"], "open")
        self.assertEqual(runner.status(self.task)["state"], "failed")
        self.assertEqual(self.rows()[0]["status"], "answered")
        with self.factory() as rt:
            recovered = [e for e in rt.log.read() if e.type == "activity.failed"
                         and e.payload.get("error_type") == "InterruptedActivity"]
            started = list(rt.log.iter_events(event_types=("question.started",)))
        self.assertEqual(len(recovered), 1)
        self.assertLess(recovered[0].seq, started[0].seq)
        self.assertEqual(runner.recover_interrupted(), 0)

    def test_handler_construction_performs_recovery_without_starting_a_server(self):
        self.ask("pending before handler construction")
        runner = ActivityRunner(self.factory)
        with patch("cater.web.ActivityRunner", return_value=runner):
            make_handler(self.db, runtime_factory=self.factory)
        self.join_threads()
        self.assertEqual(self.rows()[0]["status"], "answered")

    def test_repeated_recovery_while_driver_is_active_does_not_duplicate_calls(self):
        self.ask("blocking question")
        self.driver.release.clear()
        runner = ActivityRunner(self.factory)
        runner.recover_interrupted()
        self.assertTrue(self.driver.entered.wait(3))
        self.ask("queued while answering")
        runner.recover_interrupted()
        runner.answer_questions(self.task)
        self.assertEqual(len(self.threads), 1)
        self.driver.release.set()
        self.join_threads()
        self.assertEqual([q for _, q in self.driver.calls], ["blocking question", "queued while answering"])
        runner.recover_interrupted()
        self.assertEqual(len(self.threads), 1)

    def test_result_write_failures_do_not_drop_later_never_started_questions(self):
        interrupted = [self.ask(f"interrupted-{i}") for i in range(5)]
        self.ask("last question")
        interrupted_seqs = {q.seq for q in interrupted}
        emit = EventLog.emit

        def fail_results(log, **kwargs):
            if kwargs["type"] == "question.finished" and kwargs["payload"]["question_seq"] in interrupted_seqs:
                raise OSError("result storage interrupted after model call")
            return emit(log, **kwargs)

        runner = ActivityRunner(self.factory)
        with patch.object(EventLog, "emit", fail_results), self.assertLogs("cater.web", level="ERROR"):
            runner.answer_questions(self.task)
            self.join_threads()
        self.assertEqual([r["status"] for r in self.rows()], ["answering"] * 5 + ["answered"])
        self.assertEqual([q for _, q in self.driver.calls], [f"interrupted-{i}" for i in range(5)] + ["last question"])
        self.assertEqual(runner._question_tasks, {})
        runner.recover_interrupted()
        self.assertEqual(len(self.threads), 1)

    def test_progress_on_last_retry_still_allows_the_next_question(self):
        first = self.ask("transient storage failure")
        self.ask("next question")
        emit = EventLog.emit
        starts = 0

        def flaky_emit(log, **kwargs):
            nonlocal starts
            if kwargs["type"] == "question.started" and kwargs["payload"]["question_seq"] == first.seq:
                starts += 1
                if starts < MAX_QUESTION_QUEUE_ATTEMPTS:
                    raise OSError("temporary start storage failure")
            if kwargs["type"] == "question.finished" and kwargs["payload"]["question_seq"] == first.seq:
                raise OSError("result storage failed after model call")
            return emit(log, **kwargs)

        runner = ActivityRunner(self.factory)
        with patch.object(EventLog, "emit", flaky_emit), self.assertLogs("cater.web", level="ERROR"):
            runner.answer_questions(self.task)
            self.join_threads()
        self.assertEqual(starts, MAX_QUESTION_QUEUE_ATTEMPTS)
        self.assertEqual([r["status"] for r in self.rows()], ["answering", "answered"])
        self.assertEqual([q for _, q in self.driver.calls], ["transient storage failure", "next question"])

    def test_persistent_start_failure_is_bounded_and_queue_can_be_woken_again(self):
        self.ask("first")
        self.ask("second")
        emit = EventLog.emit
        starts = 0

        def fail_start(log, **kwargs):
            nonlocal starts
            if kwargs["type"] == "question.started":
                starts += 1
                raise OSError("persistent start storage failure")
            return emit(log, **kwargs)

        runner = ActivityRunner(self.factory)
        with patch.object(EventLog, "emit", fail_start), self.assertLogs("cater.web", level="ERROR"):
            runner.answer_questions(self.task)
            self.join_threads()
        self.assertEqual(starts, MAX_QUESTION_QUEUE_ATTEMPTS)
        self.assertEqual(self.driver.calls, [])
        self.assertEqual([r["status"] for r in self.rows()], ["pending", "pending"])
        self.assertEqual(runner._question_tasks, {})
        runner.answer_questions(self.task)
        self.join_threads()
        self.assertEqual([q for _, q in self.driver.calls], ["first", "second"])

    def test_persistent_runtime_open_failure_is_bounded_and_releases_registration(self):
        self.ask("pending")
        failing_factory = Mock(side_effect=OSError("database unavailable"))
        runner = ActivityRunner(failing_factory)
        with self.assertLogs("cater.web", level="ERROR"):
            runner.answer_questions(self.task)
            self.join_threads()
        self.assertEqual(failing_factory.call_count, MAX_QUESTION_QUEUE_ATTEMPTS)
        self.assertEqual(runner._question_tasks, {})
        self.assertEqual(self.driver.calls, [])
        self.assertEqual(self.rows()[0]["status"], "pending")

    def test_no_progress_conflicts_are_bounded_without_calling_the_driver(self):
        self.ask("pending")
        runner = ActivityRunner(self.factory)
        with patch.object(ExplanationService, "answer_next", return_value=False) as attempt, \
                self.assertLogs("cater.web", level="ERROR"):
            runner.answer_questions(self.task)
            self.join_threads()
        self.assertEqual(attempt.call_count, MAX_QUESTION_QUEUE_ATTEMPTS)
        self.assertEqual(runner._question_tasks, {})
        self.assertEqual(self.driver.calls, [])
        self.assertEqual(self.rows()[0]["status"], "pending")

    def test_one_recovery_thread_start_failure_does_not_block_other_groups(self):
        self.ask("first group question")
        other = self.create_task("Second group")
        self.ask("other group question", other)

        def selective_thread(**kwargs):
            if kwargs["args"][0] == self.task:
                return Mock(start=Mock(side_effect=RuntimeError("cannot start thread")))
            return self.make_thread(**kwargs)

        runner = ActivityRunner(self.factory)
        with patch("cater.web.threading.Thread", side_effect=selective_thread), \
                self.assertLogs("cater.web", level="ERROR"):
            self.assertEqual(runner.recover_interrupted(), 0)
            self.join_threads()
        self.assertEqual(self.rows()[0]["status"], "pending")
        self.assertEqual(self.rows(other)[0]["status"], "answered")
        self.assertEqual(runner._question_tasks, {})
        runner.recover_interrupted()
        self.join_threads()
        self.assertCountEqual(self.driver.calls, [(self.task, "first group question"), (other, "other group question")])

    def test_one_unreadable_task_does_not_block_other_groups_during_recovery(self):
        self.ask("first group question")
        other = self.create_task("Second group")
        self.ask("other group question", other)
        pending = ExplanationService.pending

        def selective_pending(service, task_id):
            if task_id == self.task:
                raise ValueError("cannot read this task")
            return pending(service, task_id)

        runner = ActivityRunner(self.factory)
        with patch.object(ExplanationService, "pending", selective_pending), \
                self.assertLogs("cater.web", level="ERROR"):
            self.assertEqual(runner.recover_interrupted(), 0)
            self.join_threads()
        self.assertEqual(self.rows()[0]["status"], "pending")
        self.assertEqual(self.rows(other)[0]["status"], "answered")
        runner.recover_interrupted()
        self.join_threads()
        self.assertCountEqual(self.driver.calls, [(self.task, "first group question"), (other, "other group question")])


if __name__ == "__main__":
    unittest.main()
