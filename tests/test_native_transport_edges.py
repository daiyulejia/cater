"""Four transport regressions with memory pipes; no files, HTTP or child processes."""

from __future__ import annotations

from contextlib import contextmanager
import io
import json
import subprocess
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from cater import native_transport
from cater.drivers import (DriverProtocolError, DriverUnavailable, _tool_api_request,
                           tool_failure_diagnostics)
from cater.work import WorkConflict


DATA = {"choices": [], "usage": {"total_tokens": 17}}


class _Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, delay):
        pass


class _Input(io.BytesIO):
    def __init__(self):
        super().__init__()
        self.writes = 0
        self.finished = threading.Event()

    def write(self, value):
        self.writes += 1
        return super().write(value)

    def close(self):
        super().close()
        self.finished.set()


class _Output(io.BytesIO):
    def __init__(self, raw, stopped):
        super().__init__(raw or b"")
        self.pending = raw is None
        self.stopped = stopped
        self.entered = threading.Event()
        self.finished = threading.Event()

    def read(self, size=-1):
        self.entered.set()
        if self.pending and not self.stopped.wait(3):
            raise OSError("Fixture reader was not stopped")
        return super().read(size)

    def close(self):
        super().close()
        self.finished.set()


class _Process:
    def __init__(self, raw):
        self.stopped = threading.Event()
        self.stdin = _Input()
        self.stdout = _Output(raw, self.stopped)
        self.returncode = None

    def poll(self):
        return self.returncode

    def kill(self):
        self.returncode = -9
        self.stopped.set()

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired("memory-worker", timeout)
        return self.returncode


class NativeTransportEdgeTests(unittest.TestCase):
    def setUp(self):
        self.spawn = self.enterContext(patch.object(native_transport.subprocess, "Popen",
            side_effect=AssertionError("Unexpected child process")))
        self.enterContext(patch("urllib.request.OpenerDirector.open",
                               side_effect=AssertionError("HTTP is forbidden")))
        self.enterContext(patch("socket.socket", side_effect=AssertionError("Network is forbidden")))

    def request(self, **changes):
        values = dict(endpoint="http://127.0.0.1:9/never-requested", body={}, headers={},
                      name="memory-fixture", timeout=1, byte_limit=4096)
        values.update(changes)
        return values

    @contextmanager
    def worker(self, *, raw=None, attached=None, sent=None):
        clock, process, threads = _Clock(), _Process(raw), []
        real_thread = threading.Thread

        def attach(child):
            self.assertIs(child, process)
            self.assertEqual(process.stdin.writes, 0)
            if attached is not None:
                attached(clock)
            return SimpleNamespace(close=process.kill)

        def make_thread(**kwargs):
            thread = real_thread(**kwargs)
            start = thread.start

            def start_at_barrier():
                start()
                if thread.name == "cater-api-output":
                    barrier = process.stdout.entered if raw is None else process.stdout.finished
                else:
                    barrier = process.stdin.finished
                if not barrier.wait(2):
                    raise AssertionError("Memory pipe did not reach its barrier")
                if thread.name == "cater-api-input" and sent is not None:
                    sent(clock)

            thread.start = start_at_barrier
            threads.append(thread)
            return thread

        runtime_threads = SimpleNamespace(Thread=make_thread, Event=threading.Event,
                                         TIMEOUT_MAX=threading.TIMEOUT_MAX)
        with patch.object(native_transport.subprocess, "Popen", return_value=process), \
                patch.object(native_transport, "ProcessScope", side_effect=attach), \
                patch.object(native_transport, "threading", runtime_threads), \
                patch.object(native_transport, "time", clock):
            try:
                yield process, clock
                self.assertIsNotNone(process.poll(), "Owned process was not reaped")
                self.assertTrue(process.stdin.closed)
                self.assertTrue(process.stdout.closed)
                self.assertTrue(all(not thread.is_alive() for thread in threads))
            finally:
                process.kill()
                for thread in threads:
                    thread.join(2)
                for stream in (process.stdin, process.stdout):
                    if not stream.closed and all(not thread.is_alive() for thread in threads):
                        stream.close()

    def test_complete_eof_retains_usage_on_cancel_or_deadline_without_suppressing_base_exceptions(self):
        cases = (WorkConflict("new input"), ValueError("callback failed"), "deadline",
                 KeyboardInterrupt("stop"), SystemExit("stop"), GeneratorExit("close"))
        raw = json.dumps({"data": DATA}).encode("ascii")
        for outcome in cases:
            with self.subTest(outcome=type(outcome).__name__ if outcome != "deadline" else outcome), \
                    self.worker(raw=raw) as (process, clock):
                pending_error = []

                def check():
                    if pending_error:
                        raise pending_error[0]

                iterator = native_transport.iter_request(**self.request(), check_current=check)
                try:
                    self.assertIsNone(next(iterator))
                    self.assertTrue(process.stdout.closed)
                    self.assertIsNone(process.poll(), "EOF must precede process exit")
                    if outcome == "deadline":
                        clock.now = 2.0
                    else:
                        pending_error.append(outcome)
                    if isinstance(outcome, BaseException) and not isinstance(outcome, Exception):
                        with self.assertRaises(type(outcome)) as caught:
                            next(iterator)
                        self.assertIs(caught.exception, outcome)
                    else:
                        # Cancellation must be observed on the very next resume, with no clock advance.
                        with self.assertRaises(StopIteration) as returned:
                            next(iterator)
                        response = returned.exception.value
                        self.assertIs(type(response), native_transport.NativeResponse)
                        self.assertEqual(response, DATA)
                        self.assertEqual(response["usage"]["total_tokens"], 17)
                        self.assertTrue(native_transport.cleanup_failed(response))
                finally:
                    iterator.close()

    def test_scope_attachment_cancellation_or_expired_deadline_sends_zero_input(self):
        for mode in ("cancel", "deadline"):
            with self.subTest(mode=mode):
                cancelled = []
                error = WorkConflict("cancelled during attachment")

                def attached(clock):
                    if mode == "cancel":
                        cancelled.append(True)
                    else:
                        clock.now = 2.0

                def check():
                    if cancelled:
                        raise error

                with self.worker(attached=attached) as (process, _):
                    iterator = native_transport.iter_request(**self.request(), check_current=check)
                    try:
                        with self.assertRaises(WorkConflict if mode == "cancel" else DriverUnavailable) as caught:
                            next(iterator)
                        if mode == "cancel":
                            self.assertIs(caught.exception, error)
                        else:
                            self.assertEqual(tool_failure_diagnostics(caught.exception), {"failure_kind": "timeout"})
                        self.assertEqual(process.stdin.writes, 0)
                        self.assertFalse(process.stdout.entered.is_set())
                    finally:
                        iterator.close()

    def test_huge_timeout_and_nonfinite_body_match_sync_invalid_request_errors(self):
        for changes in ({"timeout": 10 ** 400}, {"body": {"temperature": float("nan")}}):
            with self.subTest(field=next(iter(changes))):
                values, opener = self.request(**changes), Mock(side_effect=AssertionError("HTTP attempted"))
                with self.assertRaises(DriverProtocolError) as sync:
                    _tool_api_request(**values, opener=opener)
                iterator = native_transport.iter_request(**values)
                try:
                    with self.assertRaises(DriverProtocolError) as native:
                        next(iterator)
                    self.assertEqual(str(native.exception), str(sync.exception))
                    self.assertEqual(tool_failure_diagnostics(native.exception), {"failure_kind": "invalid_request"})
                    self.assertEqual(tool_failure_diagnostics(native.exception), tool_failure_diagnostics(sync.exception))
                    opener.assert_not_called()
                    self.spawn.assert_not_called()
                finally:
                    iterator.close()

    def test_parent_deadline_before_eof_has_no_invented_http_phase(self):
        def expire(clock):
            clock.now = 2.0

        with self.worker(sent=expire) as (process, _):
            iterator = native_transport.iter_request(**self.request())
            try:
                with self.assertRaises(DriverUnavailable) as caught:
                    next(iterator)
                self.assertGreater(process.stdin.writes, 0)
                self.assertTrue(process.stdout.entered.is_set())
                self.assertEqual(tool_failure_diagnostics(caught.exception), {"failure_kind": "timeout"})
                self.assertNotIn("transport_phase", vars(caught.exception))
            finally:
                iterator.close()


if __name__ == "__main__":
    unittest.main()
