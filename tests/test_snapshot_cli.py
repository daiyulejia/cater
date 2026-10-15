from __future__ import annotations

import json
import queue
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from cater.drivers import CliDriver, DriverProtocolError, DriverUnavailable


class HistoryProcess:
    """In-memory NDJSON pipes, including the worker's startup handshake."""

    def __init__(self, pid):
        self.pid = pid
        self.returncode = None
        self.command = None
        self.history = []
        self.responses = queue.Queue()
        self.stdin = Mock()
        self.stdout = Mock()
        self.stdin.write.side_effect = self.write
        self.stdout.readline.side_effect = lambda limit: self.responses.get(timeout=1)[:limit]
        self.scope = Mock()
        self.scope.close.side_effect = self.kill

    def write(self, line):
        request = json.loads(line)
        if isinstance(request, list):
            self.command = request
            response = {"ready": True}
        elif request["type"] == "ping":
            response = {"id": request["id"], "ok": True, "pong": True}
        else:
            self.history.append(request["prompt"])
            response = {
                "id": request["id"], "ok": True, "tokens": 7,
                "text": json.dumps({"pid": self.pid, "history": self.history,
                                    "role": request["role"], "context": request["context"]}),
                "meta": {"adapter": "mock-pipe", "token_usage": "reported"},
            }
            context = request["context"]
            failure = context.get("failure") if isinstance(context, dict) else None
            if failure == "invalid_json":
                self.responses.put("invalid JSON\n")
                return len(line)
            if failure == "unavailable":
                response.update(ok=False, error="fixture failure")
            elif failure == "invalid_text":
                response["text"] = []
            elif failure == "invalid_tokens":
                response["tokens"] = -1
            elif failure == "invalid_meta":
                response["meta"] = ["invalid"]
        self.responses.put(json.dumps(response) + "\n")
        return len(line)

    def poll(self):
        return self.returncode

    def kill(self):
        self.returncode = -1

    def wait(self, timeout):
        if self.returncode is None:
            raise AssertionError("worker must be terminated before waiting")
        return self.returncode


class SnapshotCliTests(unittest.TestCase):
    def setUp(self):
        self.processes = []
        self.workers = []
        self.driver = CliDriver("history", ["unused", "--fixture"], cwd="fixture-workdir",
                                timeout=4.5, restart_limit=2, token_budget=123)
        self.popen = self.enterContext(patch("cater.drivers.subprocess.Popen", side_effect=self.start_process))
        self.enterContext(patch("cater.drivers.ProcessScope", side_effect=lambda proc: proc.scope))
        self.factory = self.enterContext(patch("cater.drivers.CliDriver", side_effect=self.new_worker))
        self.addCleanup(self.driver.close)

    def start_process(self, *args, **kwargs):
        process = HistoryProcess(1000 + len(self.processes))
        self.processes.append(process)
        return process

    def new_worker(self, *args, **kwargs):
        worker = CliDriver(*args, **kwargs)
        self.workers.append(worker)
        self.addCleanup(worker.close)
        return worker

    def call(self, prompt, context):
        return self.driver.complete(role="tester", prompt=prompt, context=context)

    def assert_closed(self, worker, process):
        self.assertIsNone(worker._proc)
        self.assertIsNone(worker._scope)
        self.assertIsNone(worker._io_thread)
        self.assertIsNotNone(process.poll())
        process.scope.close.assert_called_once()
        process.stdin.close.assert_called_once()
        process.stdout.close.assert_called_once()

    def test_parent_child_parent_child_uses_fresh_workers_and_keeps_parent_history(self):
        self.assertTrue(self.driver.probe())
        parent = self.driver._proc
        first = self.call("parent-before", {"task_id": "parent"})
        self.assertEqual(json.loads(first.text)["history"], ["parent-before"])

        for index, background in enumerate(({"source_task_id": "parent"}, {}), 1):
            if index == 2:
                later = self.call("parent-after", {"task_id": "parent"})
                self.assertEqual(json.loads(later.text)["history"], ["parent-before", "parent-after"])
                self.assertEqual(json.loads(later.text)["pid"], parent.pid)
            context = {"task_id": "child", "background": background}
            response = self.call(f"child-{index}", context)
            child = self.processes[-1]
            self.assertEqual(json.loads(response.text), {
                "pid": child.pid, "history": [f"child-{index}"], "role": "tester", "context": context,
            })
            self.assertEqual(response.tokens, first.tokens)
            self.assertEqual(response.meta, first.meta)
            self.assert_closed(self.workers[-1], child)
            self.assertIs(self.driver._proc, parent)
            self.assertIsNone(parent.poll())
            parent.scope.close.assert_not_called()

        self.assertEqual(len(self.processes), 3)
        self.assertEqual(len(self.workers), 2)
        self.assertEqual(parent.history, ["parent-before", "parent-after"])
        for worker in self.workers:
            for key in ("name", "command", "cwd", "timeout", "restart_limit", "token_budget"):
                self.assertEqual(getattr(worker, key), getattr(self.driver, key), key)
            self.assertIsNot(worker.command, self.driver.command)
        for process in self.processes:
            self.assertEqual(process.command, self.driver.command)
        for call in self.popen.call_args_list:
            self.assertEqual(call.kwargs["cwd"], self.driver.cwd)

    def test_other_contexts_keep_the_original_long_lived_protocol(self):
        contexts = [
            None, [], "legacy-context", True,
            {}, {"task_id": "parent"}, {"task_id": "parent", "work": {}},
            *({"task_id": "parent", "background": value} for value in (None, [], "snapshot", True, 1)),
            {"background": {}},
            *({"task_id": value, "background": {}} for value in (None, "", 1, True, [], {})),
        ]
        history = []
        for index, context in enumerate(contexts):
            with self.subTest(context=context):
                history.append(str(index))
                response = self.call(str(index), context)
                self.assertEqual(json.loads(response.text)["history"], history)
                self.assertIs(self.driver._proc, self.processes[0])
        self.factory.assert_not_called()
        self.assertEqual(len(self.processes), 1)
        self.assertIsNone(self.driver._proc.poll())

    def test_transport_and_decode_errors_clean_up_only_the_snapshot_worker(self):
        self.driver.restart_limit = 0
        self.call("parent-before", {"task_id": "parent"})
        parent = self.driver._proc
        for failure in ("invalid_json", "unavailable", "invalid_text", "invalid_tokens", "invalid_meta"):
            with self.subTest(failure=failure):
                error = DriverUnavailable if failure == "unavailable" else DriverProtocolError
                with self.assertRaises(error):
                    self.call("child", {"task_id": "child", "background": {}, "failure": failure})
                self.assert_closed(self.workers[-1], self.processes[-1])
                self.assertIs(self.driver._proc, parent)
                self.assertIsNone(parent.poll())
                parent.scope.close.assert_not_called()
        response = self.call("parent-after", {"task_id": "parent"})
        self.assertEqual(json.loads(response.text)["history"], ["parent-before", "parent-after"])
        self.assertEqual(json.loads(response.text)["pid"], parent.pid)

    def test_snapshot_retains_restart_limit_but_complete_calls_never_retry(self):
        for with_work in (False, True):
            with self.subTest(with_work=with_work):
                before = len(self.processes)
                context = {"task_id": "child", "background": {}, "failure": "invalid_json"}
                if with_work:
                    context["work"] = {}
                with self.assertRaises(DriverProtocolError):
                    self.call("child", context)
                attempts = self.processes[before:]
                self.assertEqual(len(attempts), 1)
                self.assertEqual(self.workers[-1].restart_limit, self.driver.restart_limit)
                for process in attempts:
                    self.assertEqual(process.history, ["child"])
                    self.assert_closed(self.workers[-1], process)
        self.assertIsNone(self.driver._proc)
        self.assertEqual(len(self.workers), 2)

    def test_ping_can_recover_without_replaying_complete(self):
        send_once = self.driver._send_once
        attempts = []

        def send(payload):
            attempts.append(payload)
            if len(attempts) <= self.driver.restart_limit:
                raise DriverUnavailable("fixture ping failure")
            return send_once(payload)

        with patch.object(self.driver, "_send_once", side_effect=send):
            self.assertTrue(self.driver.probe())
        self.assertEqual(len(attempts), self.driver.restart_limit + 1)
        self.assertTrue(all(payload["type"] == "ping" for payload in attempts))
        self.assertEqual(self.processes[0].history, [])
        self.assertEqual(json.loads(self.call("after-ping", {}).text)["history"], ["after-ping"])


class RealSnapshotCliTests(unittest.TestCase):
    def setUp(self):
        self.processes = []
        real_popen = subprocess.Popen

        def start(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            self.processes.append(process)
            return process

        self.enterContext(patch("cater.drivers.subprocess.Popen", side_effect=start))
        fixture = Path(__file__).parent / "fixtures" / "ndjson_history.py"
        self.driver = CliDriver("real-history", [sys.executable, "-u", str(fixture)],
                                timeout=5, restart_limit=0)
        self.addCleanup(self.driver.close)

    def call(self, prompt, context):
        return json.loads(self.driver.complete(role="tester", prompt=prompt, context=context).text)

    def test_real_workers_keep_child_history_independent_and_leave_parent_alive(self):
        first = self.call("parent-before", {"task_id": "parent"})
        parent = self.processes[0]
        child = self.call("child-first", {"task_id": "child", "background": {}})
        self.assertEqual(child["history"], ["child-first"])
        self.assertNotEqual(child["pid"], first["pid"])
        self.assertIsNotNone(self.processes[1].poll())
        self.assertIsNone(parent.poll())

        later = self.call("parent-after", {"task_id": "parent"})
        self.assertEqual(later, {"pid": first["pid"], "history": ["parent-before", "parent-after"]})
        repeated = self.call("child-next", {"task_id": "child", "background": {}})
        self.assertEqual(repeated["history"], ["child-next"])
        self.assertIsNotNone(self.processes[2].poll())
        self.assertIs(self.driver._proc, parent)
        self.assertIsNone(parent.poll())

    def test_real_child_crash_cleans_up_and_parent_can_continue(self):
        first = self.call("parent-before", {"task_id": "parent"})
        parent = self.processes[0]
        with self.assertRaises(DriverUnavailable):
            self.call("child-crash", {"task_id": "child", "background": {}, "crash": True, "work": {}})
        self.assertEqual(len(self.processes), 2)
        self.assertIsNotNone(self.processes[1].poll())
        self.assertIsNone(parent.poll())
        later = self.call("parent-after", {"task_id": "parent"})
        self.assertEqual(later, {"pid": first["pid"], "history": ["parent-before", "parent-after"]})

    def test_real_question_calls_have_no_implicit_execution_history_or_shared_process(self):
        first = self.call("execution-before", {"task_id": "parent"})
        parent = self.processes[0]
        for text in ("why-this-change", "what-about-callers"):
            answer = self.call(text, {"task_id": "parent", "work": {"phase": "explanation"}})
            self.assertEqual(answer["history"], [text])
            self.assertNotEqual(answer["pid"], first["pid"])
            self.assertIsNotNone(self.processes[-1].poll())
            self.assertIsNone(parent.poll())
        after = self.call("execution-after", {"task_id": "parent"})
        self.assertEqual(after, {"pid": first["pid"], "history": ["execution-before", "execution-after"]})


if __name__ == "__main__":
    unittest.main()
