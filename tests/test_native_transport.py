"""Real owned workers and loopback HTTP only; modeled model calls are zero."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from cater import native_transport
from cater.drivers import (MAX_API_RESPONSE_BYTES, DriverProtocolError, DriverUnavailable,
                           ToolRequestRejected, tool_failure_diagnostics)


_KEY = "synthetic-native-pipe-key"
_BODY = {"text": "PRIVATE_REQUEST_BODY"}
_ENV_KEYS = {"systemroot", "windir", "temp", "tmp", "home", "userprofile", "path",
             "http_proxy", "https_proxy", "all_proxy", "no_proxy", "ssl_cert_file", "ssl_cert_dir"}


def _json_bytes(value):
    # Independent encoder: keep UTF-8 on the wire, escaping lone surrogates.
    text = json.dumps(value, ensure_ascii=False)
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError:
        return json.dumps(value, ensure_ascii=True).encode("ascii")


class _Route:
    def __init__(self, raw=b'{"ok":true}', *, status=200, headers=None,
                 expected_body=None, hold=False, headers_first=False):
        self.raw, self.status = raw, status
        self.headers = headers or {}
        self.expected_body = _BODY if expected_body is None else expected_body
        self.hold, self.headers_first = hold, headers_first
        self.seen, self.release, self.finished = (threading.Event() for _ in range(3))
        self.observations = []


class _LoopbackPeer:
    def __init__(self):
        self.routes = {}
        self.unknown_requests = 0
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.do_POST()

            def do_POST(self):
                route = owner.routes.get(urlsplit(self.path).path)
                if route is None:
                    owner.unknown_requests += 1
                    self.send_error(404)
                    return
                try:
                    self.connection.settimeout(10)
                    raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                    try:
                        body_ok = json.loads(raw) == route.expected_body
                    except (ValueError, UnicodeError):
                        body_ok = False
                    # Keep only boolean/size observations, never credentials or bodies.
                    route.observations.append({
                        "post": self.command == "POST", "body_ok": body_ok,
                        "auth_ok": self.headers.get("Authorization") == "Bearer " + _KEY,
                        "content_type_ok": self.headers.get("Content-Type") == "application/json",
                        "utf8": any(byte >= 128 for byte in raw), "size": len(raw),
                    })
                    route.seen.set()

                    def send_headers():
                        self.send_response(route.status, "PRIVATE_HTTP_REASON")
                        self.send_header("Content-Type", "application/json; charset=utf-8")
                        self.send_header("Content-Length", str(len(route.raw)))
                        self.send_header("Connection", "close")
                        for key, value in route.headers.items():
                            self.send_header(key, value)
                        self.end_headers()
                        self.wfile.flush()

                    if route.headers_first:
                        send_headers()
                    if route.hold and not route.release.wait(15):
                        return
                    if not route.headers_first:
                        send_headers()
                    self.wfile.write(route.raw)
                    self.wfile.flush()
                except OSError:
                    # Cancellation intentionally disconnects a waiting HTTP client.
                    pass
                finally:
                    route.finished.set()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.02}, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        for route in self.routes.values():
            route.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
        if self.thread.is_alive():
            raise AssertionError("Loopback listener did not stop")
        for route in self.routes.values():
            if route.seen.is_set() and not route.finished.wait(3):
                raise AssertionError("Loopback handler did not stop")

    def add(self, path="/exchange", **kwargs):
        route = self.routes[path] = _Route(**kwargs)
        return route

    def url(self, path="/exchange"):
        return "http://127.0.0.1:%d%s" % (self.server.server_port, path)


class _InputProbe:
    def __init__(self, stream, observation):
        self.stream, self.observation = stream, observation

    def write(self, data):
        self.observation.writes += 1
        self.observation.early_write |= not self.observation.attached
        return self.stream.write(data)

    @property
    def closed(self):
        return self.stream.closed

    def close(self):
        self.stream.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class NativeTransportTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="cater-native-test-")))
        self.peer = self.enterContext(_LoopbackPeer())
        self.owned, self.io_threads = [], []
        self.script = None
        real_popen, real_thread = subprocess.Popen, threading.Thread
        self.real_scope = native_transport.ProcessScope

        # Replace only this test process's mapping; do not edit machine/user env.
        environment = {key: value for key, value in os.environ.items() if key.lower() != "no_proxy"}
        environment.update(NO_PROXY="127.0.0.1,localhost", NATIVE_PARENT_ONLY=_KEY,
                           OPENAI_API_KEY=_KEY, ANTHROPIC_API_KEY=_KEY,
                           PYTHONPATH="PRIVATE_PARENT_PATH", PYTHONSTARTUP="PRIVATE_STARTUP")
        self.enterContext(patch.object(os, "environ", environment))

        def spawn(argv, **kwargs):
            self.assertTrue(_KEY not in repr(argv) and _BODY["text"] not in repr(argv),
                            "Private input escaped into worker argv")
            child_env = kwargs["env"]
            self.assertTrue({key.lower() for key in child_env} <= _ENV_KEYS)
            self.assertTrue(all(_KEY not in value for value in child_env.values()))
            self.assertTrue(child_env == {key: value for key, value in environment.items()
                                           if key.lower() in _ENV_KEYS},
                            "Worker did not preserve the permitted environment")
            self.assertEqual(argv[1:3], ["-I", "-B"])
            self.assertEqual(Path(argv[-1]).name, "_native_api_worker.py")
            self.assertIs(kwargs["stdin"], subprocess.PIPE)
            self.assertIs(kwargs["stdout"], subprocess.PIPE)
            self.assertIs(kwargs["stderr"], subprocess.DEVNULL)
            self.assertIs(kwargs["shell"], False)
            if self.script is not None:
                argv = [*argv[:-1], str(self.script)]
            process = real_popen(argv, **kwargs)
            observation = SimpleNamespace(process=process, writes=0, attached=False, early_write=False)
            self.owned.append(observation)
            process.stdin = _InputProbe(process.stdin, observation)
            return process

        def attach(process):
            observation = next(item for item in self.owned if item.process is process)
            self.assertEqual(observation.writes, 0, "stdin was fed before scope attachment")
            scope = self.real_scope(process)
            observation.attached = True
            return scope

        def thread_factory(*args, **kwargs):
            thread = real_thread(*args, **kwargs)
            self.io_threads.append(thread)
            return thread

        self.enterContext(patch.object(native_transport.subprocess, "Popen", new=spawn))
        self.enterContext(patch.object(native_transport, "ProcessScope", new=attach))
        self.enterContext(patch.object(native_transport, "threading", SimpleNamespace(
            Thread=thread_factory, Event=threading.Event, TIMEOUT_MAX=threading.TIMEOUT_MAX)))
        self.addCleanup(self._cleanup_owned)

    def _cleanup_owned(self):
        problems = []
        for item in self.owned:
            process = item.process
            if process.poll() is None:
                problems.append("live owned PID %s" % process.pid)
                process.kill()
            process.wait(timeout=5)
            if item.early_write:
                problems.append("stdin written before attachment")
            for stream in (process.stdin, process.stdout):
                if not stream.closed:
                    problems.append("open owned pipe for PID %s" % process.pid)
        for thread in self.io_threads:
            if thread.is_alive():
                problems.append("live transport I/O thread")
                thread.join(3)
        for item in self.owned:
            for stream in (item.process.stdin, item.process.stdout):
                if not stream.closed and not any(thread.is_alive() for thread in self.io_threads):
                    stream.close()
        self.assertEqual(problems, [], "Transport leaked resources before fixture cleanup")

    def assert_reaped(self, owned=None, threads=None):
        for item in self.owned if owned is None else owned:
            self.assertIsNotNone(item.process.poll(), "Owned worker PID is still alive")
            self.assertTrue(item.process.stdin.closed)
            self.assertTrue(item.process.stdout.closed)
            self.assertFalse(item.early_write)
        for thread in self.io_threads if threads is None else threads:
            self.assertFalse(thread.is_alive(), "Owned I/O thread is still alive")

    def request(self, path="/exchange", **kwargs):
        arguments = dict(endpoint=self.peer.url(path), body=_BODY,
                         headers={"Content-Type": "application/json", "Authorization": "Bearer " + _KEY},
                         name="loopback-fixture", timeout=8, byte_limit=MAX_API_RESPONSE_BYTES)
        arguments.update(kwargs)
        iterator = native_transport.iter_request(**arguments)
        self.addCleanup(iterator.close)
        return iterator

    def drain(self, iterator):
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            try:
                self.assertIsNone(next(iterator))
            except StopIteration as done:
                return done.value
        self.fail("Native iterator exceeded the independent test deadline")

    def advance_until(self, iterator, predicate):
        deadline = time.monotonic() + 6
        while not predicate():
            self.assertLess(time.monotonic(), deadline, "Worker did not reach the test barrier")
            self.assertIsNone(next(iterator))

    def use_script(self, source):
        self.script = self.directory / "worker.py"
        self.script.write_text(source, encoding="ascii")

    def assert_private(self, error):
        public = "".join(traceback.format_exception(error)) + repr(vars(error))
        for marker in (_KEY, _BODY["text"], "PRIVATE_HTTP_BODY", "PRIVATE_HTTP_REASON", "PRIVATE_ENDPOINT"):
            self.assertNotIn(marker, public)
        self.assertIsNone(error.__cause__)
        if error.__context__ is not None:
            self.assertTrue(error.__suppress_context__)

    def assert_exchange(self, route):
        self.assertEqual(len(route.observations), 1)
        observation = route.observations[0]
        self.assertTrue(all(observation[key] for key in ("post", "body_ok", "auth_ok", "content_type_ok")))

    def test_lazy_start_and_close_before_first_iteration_have_no_effects(self):
        route = self.peer.add()
        iterator = self.request()
        self.assertEqual(self.owned, [])
        iterator.close()
        self.assertEqual(self.owned, [])
        self.assertFalse(route.seen.is_set())

    def test_real_worker_utf8_and_lone_surrogates_round_trip(self):
        for index, text in enumerate(("\u4f60\u597d \U0001f642 caf\u00e9", "left\ud800right\udfff")):
            with self.subTest(case=index):
                body = {"text": text, "nested": [text, {text: text}]}
                data = {"echo": body, "cleanup_failed": True}
                path = "/unicode-%d" % index
                route = self.peer.add(path, raw=_json_bytes(data), expected_body=body)
                result = self.drain(self.request(path, body=body))
                self.assertIs(type(result), native_transport.NativeResponse)
                self.assertEqual(result, data)
                self.assertFalse(native_transport.cleanup_failed(result))
                self.assert_exchange(route)
                self.assertEqual(route.observations[0]["utf8"], index == 0)
                self.assert_reaped()

    def test_large_http_response_drains_stdout_without_deadlock(self):
        data = {"text": "\u4f60\U0001f642" * 180000}
        raw = _json_bytes(data)
        self.assertGreater(len(raw), 1024 * 1024)
        route = self.peer.add(raw=raw)
        result = self.drain(self.request(byte_limit=len(raw)))
        self.assertEqual(result, data)
        self.assertFalse(native_transport.cleanup_failed(result))
        self.assert_exchange(route)
        self.assert_reaped()

    def test_only_os_proxy_ca_environment_and_private_pipe_input(self):
        route = self.peer.add()
        result = self.drain(self.request())
        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(self.owned), 1)
        self.assertNotEqual(self.owned[0].process.pid, os.getpid())
        self.assertTrue(self.owned[0].attached)
        self.assertGreater(self.owned[0].writes, 0)
        self.assertEqual(sorted(thread.name for thread in self.io_threads),
                         ["cater-api-input", "cater-api-output"])
        self.assert_exchange(route)
        self.assert_reaped()

    def test_http_429_and_503_have_safe_diagnostics_and_no_retry(self):
        for status, error_type in ((429, ToolRequestRejected), (503, DriverUnavailable)):
            with self.subTest(status=status):
                path = "/http-%d" % status
                route = self.peer.add(path, status=status,
                                      raw=b"PRIVATE_HTTP_BODY " + _KEY.encode() + b" PRIVATE_REQUEST_BODY",
                                      headers={"Retry-After": "1", "X-Private": "PRIVATE_HTTP_BODY"})
                with self.assertRaises(error_type) as caught:
                    self.drain(self.request(path + "?PRIVATE_ENDPOINT=hidden"))
                self.assertIs(type(caught.exception), error_type)
                self.assertEqual(tool_failure_diagnostics(caught.exception), {
                    "failure_kind": "http_error", "transport_phase": "request", "status_code": status})
                self.assert_private(caught.exception)
                self.assertIsNone(caught.exception.__context__)
                self.assert_exchange(route)
                self.assert_reaped()

    def test_redirects_are_not_followed_even_to_another_loopback_peer(self):
        destination = self.enterContext(_LoopbackPeer())
        target = destination.add("/target")
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                path = "/redirect-%d" % status
                route = self.peer.add(path, status=status, raw=b"PRIVATE_HTTP_BODY",
                                      headers={"Location": destination.url("/target")})
                with self.assertRaises(DriverUnavailable) as caught:
                    self.drain(self.request(path))
                self.assertEqual(tool_failure_diagnostics(caught.exception), {
                    "failure_kind": "http_error", "transport_phase": "request", "status_code": status})
                self.assert_private(caught.exception)
                self.assert_exchange(route)
                self.assertFalse(target.seen.is_set())
                self.assertEqual(destination.unknown_requests, 0)
                self.assert_reaped()

    def test_http_response_byte_limit_is_inclusive(self):
        raw = b'{"text":"PRIVATE_HTTP_BODY"}'
        for limit in (len(raw), len(raw) - 1):
            with self.subTest(limit=limit):
                path = "/limit-%d" % limit
                route = self.peer.add(path, raw=raw)
                iterator = self.request(path, byte_limit=limit)
                if limit == len(raw):
                    self.assertEqual(self.drain(iterator), json.loads(raw))
                else:
                    with self.assertRaises(DriverProtocolError) as caught:
                        self.drain(iterator)
                    self.assert_private(caught.exception)
                self.assert_exchange(route)
                self.assert_reaped()

    def test_http_ambiguous_or_invalid_json_is_rejected(self):
        cases = (b'{"x":1,"x":2}', b'{"nested":{"x":1,"x":2}}', b'{}{}',
                 b'{"x":NaN}', b'{"x":1e999}', b'{"x":"\xff"}',
                 b'[]', b'null', b'PRIVATE_HTTP_BODY')
        for index, raw in enumerate(cases):
            with self.subTest(case=index):
                path = "/invalid-%d" % index
                route = self.peer.add(path, raw=raw)
                with self.assertRaises(DriverProtocolError) as caught:
                    self.drain(self.request(path))
                self.assert_private(caught.exception)
                self.assert_exchange(route)
                self.assert_reaped()

    def test_callback_exceptions_before_spawn_preserve_identity(self):
        route = self.peer.add()
        for error in (ValueError("cancel fixture"), KeyboardInterrupt("cancel fixture")):
            with self.subTest(kind=type(error).__name__):
                def check():
                    raise error

                with self.assertRaises(type(error)) as caught:
                    self.drain(self.request(check_current=check))
                self.assertIs(caught.exception, error)
                self.assertEqual(self.owned, [])
                self.assertFalse(route.seen.is_set())

    def test_close_and_callback_cancellation_do_not_kill_another_invocation(self):
        for index, error in enumerate((None, ValueError("cancel fixture"), KeyboardInterrupt("cancel fixture"))):
            with self.subTest(case=index):
                first_route = self.peer.add("/cancel-%d" % index, hold=True)
                second_route = self.peer.add("/survivor-%d" % index, hold=True, raw=b'{"survived":true}')
                cancel = threading.Event()

                def check():
                    if cancel.is_set():
                        raise error

                first = self.request("/cancel-%d" % index, check_current=check)
                self.advance_until(first, first_route.seen.is_set)
                first_owned, first_threads = self.owned[-1:], self.io_threads[-2:]
                second = self.request("/survivor-%d" % index)
                self.advance_until(second, second_route.seen.is_set)
                second_process = self.owned[-1].process
                self.assertNotEqual(first_owned[0].process.pid, second_process.pid)
                if error is None:
                    first.close()
                else:
                    cancel.set()
                    with self.assertRaises(type(error)) as caught:
                        next(first)
                    self.assertIs(caught.exception, error)
                self.assert_reaped(first_owned, first_threads)
                self.assertIsNone(second_process.poll(), "Cancelling one call killed its peer")
                first_route.release.set()
                second_route.release.set()
                result = self.drain(second)
                self.assertEqual(result, {"survived": True})
                self.assertFalse(native_transport.cleanup_failed(result))
                self.assert_exchange(first_route)
                self.assert_exchange(second_route)
                self.assert_reaped()

    def test_timeout_reaps_worker_pipes_and_threads(self):
        for index, headers_first in enumerate((False, True)):
            with self.subTest(headers_first=headers_first):
                path = "/timeout-%d" % index
                route = self.peer.add(path, hold=True, headers_first=headers_first)
                started = time.monotonic()
                with self.assertRaises(DriverUnavailable) as caught:
                    self.drain(self.request(path, timeout=1.5))
                self.assertLess(time.monotonic() - started, 6)
                diagnostics = tool_failure_diagnostics(caught.exception)
                self.assertEqual(diagnostics.get("failure_kind"), "timeout")
                # Only the worker can identify the HTTP phase; the parent's
                # total deadline must leave it unknown rather than guess.
                self.assertIn(diagnostics.get("transport_phase"), (None, "request", "response_read"))
                self.assert_private(caught.exception)
                self.assert_exchange(route)
                self.assert_reaped()
                route.release.set()

    def test_scope_attach_failure_sends_zero_input_and_zero_http_and_reaps(self):
        route = self.peer.add()
        attempts = []

        def refuse(process):
            attempts.append(process.pid)
            self.assertIsNone(process.poll())
            self.assertEqual(self.owned[-1].writes, 0)
            raise OSError("synthetic scope attachment failure")

        with patch.object(native_transport, "ProcessScope", new=refuse):
            with self.assertRaises(DriverUnavailable) as caught:
                self.drain(self.request())
        self.assertEqual(attempts, [self.owned[0].process.pid])
        self.assertEqual(self.owned[0].writes, 0)
        self.assertEqual(self.io_threads, [])
        self.assertFalse(route.seen.is_set())
        self.assertEqual(route.observations, [])
        self.assert_private(caught.exception)
        self.assert_reaped()

    def test_blocked_stdin_write_can_be_closed_or_timed_out(self):
        route = self.peer.add()
        marker = self.directory / "not-reading"
        self.use_script("from pathlib import Path\nimport time\n"
                        "Path(%r).touch()\ntime.sleep(30)\n" % str(marker))
        for mode in ("close", "timeout"):
            with self.subTest(mode=mode):
                marker.unlink(missing_ok=True)
                iterator = self.request(body={"text": "x" * (8 * 1024 * 1024)}, timeout=2)
                self.advance_until(iterator, marker.exists)
                observation = self.owned[-1]
                writer = self.io_threads[-1]
                self.assertGreater(observation.writes, 0)
                self.assertTrue(writer.is_alive(), "Large stdin write did not create backpressure")
                self.assertIsNone(observation.process.poll())
                started = time.monotonic()
                if mode == "close":
                    iterator.close()
                else:
                    with self.assertRaises(DriverUnavailable) as caught:
                        self.drain(iterator)
                    self.assertEqual(tool_failure_diagnostics(caught.exception).get("failure_kind"), "timeout")
                self.assertLess(time.monotonic() - started, 6)
                self.assertFalse(route.seen.is_set())
                self.assert_reaped()

    def test_complete_envelope_survives_nonzero_worker_exit(self):
        for code in (0, 19):
            with self.subTest(exit_code=code):
                self.use_script("import sys\nsys.stdin.buffer.read()\n"
                                "sys.stdout.buffer.write(b'{\"data\":{\"received\":true,\"cleanup_failed\":false}}')\n"
                                "sys.stdout.buffer.flush()\nsys.exit(%d)\n" % code)
                result = self.drain(self.request())
                self.assertIs(type(result), native_transport.NativeResponse)
                self.assertEqual(result, {"received": True, "cleanup_failed": False})
                self.assertEqual(native_transport.cleanup_failed(result), code != 0)
                self.assertEqual(self.owned[-1].process.returncode, code)
                self.assert_reaped()

    def test_received_envelope_is_not_lost_when_stdin_later_breaks(self):
        marker = self.directory / "envelope-sent"
        break_input = self.directory / "break-input"
        self.use_script("from pathlib import Path\nimport os\nimport sys\nimport time\n"
                        "sys.stdout.buffer.write(b'{\"data\":{\"received\":true}}')\n"
                        "sys.stdout.buffer.flush()\nos.close(1)\nPath(%r).touch()\n"
                        "while not Path(%r).exists():\n    time.sleep(0.01)\n"
                        "os.close(0)\ntime.sleep(0.3)\nos._exit(19)\n" % (str(marker), str(break_input)))
        iterator = self.request(body={"text": "x" * (8 * 1024 * 1024)})
        self.advance_until(iterator, lambda: marker.exists() and not self.io_threads[-2].is_alive())
        self.assertTrue(self.io_threads[-1].is_alive(), "stdin must still be backpressured")
        self.assertIsNone(self.owned[-1].process.poll())
        break_input.touch()
        result = self.drain(iterator)
        self.assertIs(type(result), native_transport.NativeResponse)
        self.assertEqual(result, {"received": True})
        self.assertTrue(native_transport.cleanup_failed(result))
        self.assert_reaped()

    def test_scope_cleanup_error_retains_received_data_and_trusted_flag(self):
        route = self.peer.add(raw=b'{"received":true}')

        def attach(process):
            scope = self.real_scope(process)
            self.owned[-1].attached = True

            def close():
                scope.close()
                raise OSError("synthetic cleanup failure")

            return SimpleNamespace(close=close)

        with patch.object(native_transport, "ProcessScope", new=attach):
            result = self.drain(self.request())
        self.assertEqual(result, {"received": True})
        self.assertIs(type(result), native_transport.NativeResponse)
        self.assertTrue(native_transport.cleanup_failed(result))
        self.assert_exchange(route)
        self.assert_reaped()

    def test_cleanup_failure_does_not_replace_original_callback_error(self):
        route = self.peer.add(hold=True)
        cancel = threading.Event()
        error = ValueError("cancel fixture")

        def check():
            if cancel.is_set():
                raise error

        def attach(process):
            scope = self.real_scope(process)
            self.owned[-1].attached = True

            def close():
                scope.close()
                raise OSError("synthetic cleanup failure")

            return SimpleNamespace(close=close)

        with patch.object(native_transport, "ProcessScope", new=attach):
            iterator = self.request(check_current=check)
            self.advance_until(iterator, route.seen.is_set)
            cancel.set()
            with self.assertRaises(ValueError) as caught:
                next(iterator)
        self.assertIs(caught.exception, error)
        self.assertIs(getattr(error, "native_cleanup_failed", None), True)
        self.assertIn("Native API transport cleanup was not confirmed", error.__notes__)
        self.assert_reaped()

    def test_invalid_worker_envelopes_are_rejected_after_reaping(self):
        for index, raw in enumerate((b'{"data":{},"data":{}}', b'{"data":{"x":1,"x":2}}',
                                     b'{"data":{}}{}', b'{"data":[]}', b'[]', b'{"data":{},"extra":0}')):
            with self.subTest(case=index):
                self.use_script("import sys\nsys.stdin.buffer.read()\n"
                                "sys.stdout.buffer.write(%r)\nsys.stdout.buffer.flush()\n" % raw)
                with self.assertRaises(DriverProtocolError) as caught:
                    self.drain(self.request())
                self.assert_private(caught.exception)
                self.assert_reaped()

    def test_cleanup_flag_accepts_only_exact_native_response_and_boolean_true(self):
        class Forged(dict):
            cleanup_failed = True

        class Derived(native_transport.NativeResponse):
            pass

        result = native_transport.NativeResponse({"cleanup_failed": True})
        for value in (True, False, 1, "true", None):
            result.cleanup_failed = value
            self.assertEqual(native_transport.cleanup_failed(result), value is True)
        derived = Derived({})
        derived.cleanup_failed = True
        for untrusted in ({"cleanup_failed": True}, Forged(), derived, None, True):
            self.assertFalse(native_transport.cleanup_failed(untrusted))


if __name__ == "__main__":
    unittest.main()
