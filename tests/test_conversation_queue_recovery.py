from __future__ import annotations

import json
import threading
import unittest
from contextlib import nullcontext
from dataclasses import replace
from unittest.mock import patch

from cater.conversation import SYSTEM
from cater.drivers import DriverResponse
from cater.runtime import Runtime, load_runtime_config
from cater.web import ActivityRunner


_THREAD = threading.Thread


class DialogueDriver:
    def __init__(self):
        self.calls = []

    def complete(self, *, context, **kwargs):
        message = context["conversation"]["message"]
        self.calls.append((context["task_id"], message))
        return DriverResponse(json.dumps({
            "reply": "Recorded discussion.", "action": "discuss", "task_id": None,
            "goal": None, "acceptance": None, "quote": None,
        }), 3, {"token_usage": "reported"})


class DeferredThread:
    def __init__(self, *, target, args, **kwargs):
        self.target, self.args = target, args
        self.started = False

    def start(self):
        self.started = True

    def run(self):
        self.target(*self.args)


class ConversationQueueRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.enterContext(patch("cater.web.time.time", side_effect=lambda: self.now))
        config = load_runtime_config()
        config = replace(config, organization=replace(config.organization,
            leader=replace(config.organization.leader, driver="queue_test")))
        self.driver = DialogueDriver()
        with patch("cater.runtime.load_runtime_config", return_value=config):
            self.rt = Runtime(":memory:", drivers={"queue_test": self.driver})
        self.addCleanup(self.rt.close)
        self.task = self.rt.create_task("Queue recovery")
        self.runner = ActivityRunner(lambda: nullcontext(self.rt))
        self.threads = []
        self.enterContext(patch("cater.web.threading.Thread", side_effect=self.make_thread))
        self.waits = []
        self.on_wait = None
        self.sleep = self.enterContext(patch("cater.web.time.sleep", side_effect=self.wait))

    def make_thread(self, **kwargs):
        thread = DeferredThread(**kwargs)
        self.threads.append(thread)
        return thread

    def wait(self, seconds):
        self.assertGreater(seconds, 0)
        self.assertLessEqual(seconds, 1)
        self.waits.append(seconds)
        self.assertLess(len(self.waits), 10, "consumer did not make progress")
        if self.on_wait:
            self.on_wait()
        self.now += seconds

    def receive(self, text, *, task=None, request_id=None):
        return self.rt.conversation.receive(task or self.task, text, request_id=request_id)

    def started(self, received, *, lease_until=1002.0):
        return self.rt.log.emit(type="conversation.started", task_id=received.task_id, actor=SYSTEM,
            payload={"conversation_seq": received.seq, "scope": self.rt.work.scope(received.task_id),
                     "lease_until": lease_until})

    def finish(self, received, started):
        self.rt.log.emit(type="conversation.finished", task_id=received.task_id, actor=SYSTEM,
            payload={"conversation_seq": received.seq, "attempt_seq": started.seq,
                     "status": "finished", "action": "discuss", "next_action": "none",
                     "reply": "Previous call finished."})

    def consume(self):
        self.runner.answer_conversations(self.task)
        self.assertEqual(len(self.threads), 1)
        self.assertTrue(self.threads[0].started)
        self.threads[0].run()
        self.assertEqual(self.runner._conversation_tasks, {})

    def assert_started_once(self, received):
        starts = [event for event in self.rt.log.iter_events(
            task_id=received.task_id, event_types=("conversation.started",))
            if event.payload["conversation_seq"] == received.seq]
        self.assertEqual(len(starts), 1)

    def test_live_attempt_expires_and_later_messages_run_in_fifo_without_another_send(self):
        old = self.receive("old")
        self.started(old)
        self.receive("second")
        self.receive("third")
        self.consume()
        self.assertTrue(self.waits)
        self.assertEqual(self.driver.calls, [(self.task, "second"), (self.task, "third")])
        self.assertEqual([row["status"] for row in self.rt.conversation.view(self.task)],
                         ["uncertain", "finished", "finished"])
        self.assert_started_once(old)

    def test_duplicate_sends_and_wake_requests_keep_one_consumer(self):
        old = self.receive("old")
        self.started(old)
        second = self.receive("second", request_id="queue-request-0001")

        def send_again():
            self.assertEqual(self.receive("second", request_id="queue-request-0001"), second)
            self.receive("third", request_id="queue-request-0002")
            for _ in range(5):
                self.runner.answer_conversations(self.task)
            self.assertEqual(len(self.threads), 1)
            self.now = 1003.0

        self.on_wait = send_again
        self.consume()
        self.assertEqual(self.driver.calls, [(self.task, "second"), (self.task, "third")])
        self.assertEqual(len(self.rt.conversation.view(self.task)), 3)
        self.assert_started_once(old)

    def test_wait_releases_runner_lock_and_other_tasks_can_be_consumed(self):
        old = self.receive("old")
        self.started(old)
        self.receive("second")
        other = self.rt.create_task("Other task")
        self.receive("other", task=other)

        def process_other():
            acquired = []

            def probe_lock():
                locked = self.runner._lock.acquire(blocking=False)
                acquired.append(locked)
                if locked:
                    self.runner._lock.release()

            probe = _THREAD(target=probe_lock)
            probe.start()
            probe.join(1)
            self.assertFalse(probe.is_alive())
            self.assertEqual(acquired, [True])
            self.runner.answer_conversations(other)
            self.threads[-1].run()
            self.assertEqual(self.driver.calls, [(other, "other")])
            self.assertIn(self.task, self.runner._conversation_tasks)
            self.now = 1003.0

        self.on_wait = process_other
        self.consume()
        self.assertEqual(self.driver.calls, [(other, "other"), (self.task, "second")])

    def test_old_call_can_finish_before_expiry_without_waiting_out_its_lease(self):
        old = self.receive("old")
        started = self.started(old, lease_until=1600.0)
        self.receive("second")
        self.on_wait = lambda: self.finish(old, started)
        self.consume()
        self.assertEqual(self.waits, [1.0])
        self.assertLess(self.now, 1600.0)
        self.assertEqual(self.driver.calls, [(self.task, "second")])
        self.assert_started_once(old)

    def test_no_later_pending_message_does_not_leave_a_waiting_consumer(self):
        old = self.receive("old")
        self.started(old, lease_until=1600.0)
        self.consume()
        self.sleep.assert_not_called()
        self.assertEqual(self.driver.calls, [])
        self.assert_started_once(old)

    def test_already_expired_attempt_is_not_replayed_or_waited_on(self):
        old = self.receive("old")
        self.started(old, lease_until=999.0)
        self.receive("second")
        self.consume()
        self.sleep.assert_not_called()
        self.assertEqual(self.driver.calls, [(self.task, "second")])
        self.assert_started_once(old)

    def test_all_live_attempts_must_expire_before_the_next_message_runs(self):
        first = self.receive("first old call")
        self.started(first, lease_until=1001.0)
        second = self.receive("second old call")
        self.started(second, lease_until=1003.0)
        self.receive("next")
        self.consume()
        self.assertGreater(self.now, 1003.0)
        self.assertEqual(self.driver.calls, [(self.task, "next")])
        self.assert_started_once(first)
        self.assert_started_once(second)

    def test_failed_result_write_waits_then_consumes_next_without_replaying_model(self):
        first = self.receive("first")
        self.receive("second")
        emit = self.rt.log.emit

        def fail_first_result(**kwargs):
            if (kwargs["type"] == "conversation.finished"
                    and kwargs["payload"]["conversation_seq"] == first.seq):
                raise OSError("simulated result write failure")
            return emit(**kwargs)

        self.on_wait = lambda: setattr(self, "now", 1601.0)
        with patch.object(self.rt.log, "emit", side_effect=fail_first_result), self.assertLogs("cater.web", level="ERROR"):
            self.consume()
        self.assertEqual(self.driver.calls, [(self.task, "first"), (self.task, "second")])
        self.assertEqual([row["status"] for row in self.rt.conversation.view(self.task)],
                         ["uncertain", "finished"])
        self.assert_started_once(first)


if __name__ == "__main__":
    unittest.main()
