"""Exercise the background WebTests fixture with real localhost requests."""

from contextlib import ExitStack, redirect_stderr
import io
from pathlib import Path
import sqlite3
import sys
import threading
import unittest
from unittest.mock import patch

import test_runtime as runtime_tests


METHOD = "test_web_can_run_collaboration_in_background_without_duplicate_rounds"


class WebBackgroundCleanupTests(unittest.TestCase):
    def check_first_request_failure(self, *, pending_response=False, error=None):
        fixture = runtime_tests.WebTests(METHOD)
        real_driver = runtime_tests.BlockingDriver
        real_runtime = runtime_tests.Runtime
        real_server = runtime_tests.ThreadingHTTPServer
        real_temporary_directory = runtime_tests.tempfile.TemporaryDirectory
        real_make_handler = runtime_tests.make_handler
        real_urlopen = runtime_tests.urlopen
        drivers, servers, directories, temporary_directories = [], [], [], []
        opened, closed = [], []
        requests, server_threads, request_errors = [], [], []
        deletion_checks, raised_errors, http_timeouts = [], [], []
        response_pending = threading.Event()
        stderr = io.StringIO()

        class ObservedDriver(real_driver):
            def __init__(self):
                super().__init__()
                drivers.append(self)

            def complete(self, **kwargs):
                self.started.set()
                if not self.release.wait(timeout=5):
                    raise AssertionError("cleanup did not release the fake driver")
                return super().complete(**kwargs)

        class ObservedRuntime(real_runtime):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                opened.append((self, threading.current_thread()))

            def close(self):
                super().close()
                with self_test.assertRaisesRegex(sqlite3.ProgrammingError, "closed"):
                    self.log.db.execute("SELECT 1")
                closed.append(self)

        class ObservedServer(real_server):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                servers.append(self)

            def serve_forever(self, *args, **kwargs):
                server_threads.append(threading.current_thread())
                return super().serve_forever(*args, **kwargs)

            def process_request_thread(self, *args, **kwargs):
                requests.append(threading.current_thread())
                return super().process_request_thread(*args, **kwargs)

            def handle_error(self, *args, **kwargs):
                request_errors.append(sys.exc_info()[1])
                return super().handle_error(*args, **kwargs)

        class ObservedDirectory(real_temporary_directory):
            def __enter__(self):
                path = super().__enter__()
                temporary_directories.append(self)
                directories.append(Path(path))
                return path

            def __exit__(self, *exc):
                owned = set(server_threads + requests + [owner for _, owner in opened])
                owned.discard(threading.current_thread())
                deletion_checks.append({
                    "exception": exc[1],
                    "released": drivers[0].release.is_set(),
                    "live": [thread.name for thread in owned if thread.is_alive()],
                    "listening": [server.server_address for server in servers if server.fileno() != -1],
                    "opened": len(opened),
                    "closed": len(closed),
                })
                return super().__exit__(*exc)

        def make_handler(*args, **kwargs):
            handler = real_make_handler(*args, **kwargs)
            if pending_response:
                def task_payload(request_handler, task_id, runner):
                    with request_handler._runtime() as runtime:
                        response_pending.set()
                        if not drivers[0].release.wait(timeout=5):
                            raise AssertionError("cleanup did not unblock the HTTP request")
                        return runtime_tests.web_module.task_api_payload(
                            runtime, task_id, activity=runner.status(task_id))
                handler._task_payload = task_payload
            return handler

        def first_request(*args, **kwargs):
            http_timeouts.append(kwargs.get("timeout"))
            self.assertEqual(len(http_timeouts), 1)
            try:
                with real_urlopen(*args, **kwargs) as response:
                    self.assertEqual(response.status, 202)
                    response.read()
            except TimeoutError as timeout:
                if not pending_response:
                    raise
                self.assertTrue(response_pending.is_set())
                self.assertTrue(drivers[0].started.is_set())
                self.assertTrue(any(thread.is_alive() for thread in requests))
                raised_errors.append(timeout)
                raise
            self.assertFalse(pending_response, "the blocked response did not time out")
            self.assertTrue(drivers[0].started.wait(timeout=1))
            self.assertFalse(drivers[0].release.is_set())
            raised_errors.append(error)
            raise error

        self_test = self
        try:
            with ExitStack() as patches:
                patches.enter_context(redirect_stderr(stderr))
                for name, value in (
                    ("BlockingDriver", ObservedDriver), ("Runtime", ObservedRuntime),
                    ("ThreadingHTTPServer", ObservedServer), ("make_handler", make_handler),
                    ("urlopen", first_request),
                ):
                    patches.enter_context(patch.object(runtime_tests, name, value))
                patches.enter_context(patch.object(
                    runtime_tests.tempfile, "TemporaryDirectory", ObservedDirectory))
                with self.assertRaises(TimeoutError if pending_response else type(error)) as raised:
                    getattr(fixture, METHOD)()
                self.assertEqual(len(raised_errors), 1)
                self.assertIs(raised.exception, raised_errors[0])
                self.assertIsNone(raised.exception.__context__)
                self.assertEqual(http_timeouts, [2])
                self.assertEqual(len(deletion_checks), 1)
                deletion, = deletion_checks
                self.assertIs(deletion["exception"], raised.exception)
                self.assertTrue(deletion["released"])
                self.assertEqual(deletion["live"], [])
                self.assertEqual(deletion["listening"], [])
                self.assertTrue(all(not server.daemon_threads for server in servers))
                self.assertGreater(deletion["opened"], 0)
                self.assertEqual(deletion["opened"], deletion["closed"])
                self.assertTrue(all(not path.exists() for path in directories))
                self.assertEqual(drivers[0].calls, 1)
                self.assertTrue(requests)
                self.assertTrue(any(owner.name.startswith("cater-run-") for _, owner in opened))
                for request_error in request_errors:
                    self.assertIsInstance(request_error, (
                        BrokenPipeError, ConnectionResetError, ConnectionAbortedError))
                self.assertNotIn("PermissionError", stderr.getvalue())
        finally:
            # Keep a failing regression from leaking its own fixture resources.
            for driver in drivers:
                driver.release.set()
            fixture.doCleanups()
            for server in servers:
                server.shutdown()
                server.server_close()
            for thread in set(server_threads + requests + [owner for _, owner in opened]):
                if thread is not threading.current_thread():
                    thread.join(timeout=5)
            for directory in temporary_directories:
                directory.cleanup()

    def test_real_first_request_timeout_drains_request_and_consumer_before_delete(self):
        self.check_first_request_failure(pending_response=True)

    def test_injected_timeout_after_first_http_response_keeps_original_exception(self):
        self.check_first_request_failure(error=TimeoutError("injected first HTTP response failure"))

    def test_non_timeout_first_request_exception_also_cleans_up(self):
        self.check_first_request_failure(error=ValueError("injected first HTTP response failure"))


if __name__ == "__main__":
    unittest.main()
