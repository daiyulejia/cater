"""Keep clarification API fixture cleanup inside its temporary directory."""

from contextlib import ExitStack, redirect_stderr
import io
from pathlib import Path
import sqlite3
import threading
import unittest
from unittest.mock import patch

import test_clarifications as clarification_tests
import test_runtime as runtime_tests


METHOD = "test_api_persists_answer_and_does_not_queue_it_twice"


class ClarificationCleanupTests(unittest.TestCase):
    def check_first_request_failure(self, *, pending_response=False, error=None):
        fixture = clarification_tests.ClarificationFlowTests(METHOD)
        real_driver = clarification_tests.BlockingDriver
        real_runtime = clarification_tests.Runtime
        real_server = runtime_tests.ThreadingHTTPServer
        real_directory = clarification_tests.tempfile.TemporaryDirectory
        real_handler = runtime_tests.make_handler
        real_urlopen = clarification_tests.urlopen
        drivers, servers, threads, directories = [], [], [], []
        opened, closed, deletions, errors, timeouts = [], [], [], [], []
        response_pending = threading.Event()
        stderr = io.StringIO()
        check = self

        class ObservedDriver(real_driver):
            def __init__(self):
                super().__init__()
                drivers.append(self)

            def complete(self, **kwargs):
                self.started.set()
                # Stay blocked beyond the real four-second request timeout.
                if not self.release.wait(timeout=10):
                    raise AssertionError("cleanup did not release the actual fake driver")
                return super().complete(**kwargs)

        class ObservedRuntime(real_runtime):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                opened.append((self, threading.current_thread()))

            def close(self):
                super().close()
                with check.assertRaisesRegex(sqlite3.ProgrammingError, "closed"):
                    self.log.db.execute("SELECT 1")
                closed.append(self)

        class ObservedServer(real_server):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                servers.append(self)

            def serve_forever(self, *args, **kwargs):
                threads.append(threading.current_thread())
                return super().serve_forever(*args, **kwargs)

            def process_request_thread(self, *args, **kwargs):
                threads.append(threading.current_thread())
                return super().process_request_thread(*args, **kwargs)

        class ObservedDirectory(real_directory):
            def __enter__(self):
                directories.append(self)
                return super().__enter__()

            def __exit__(self, *exc):
                owners = set(threads + [owner for _, owner in opened])
                owners.discard(threading.current_thread())
                deletions.append({
                    "exception": exc[1],
                    "live": [owner.name for owner in owners if owner.is_alive()],
                    "listening": [server.fileno() for server in servers if server.fileno() != -1],
                    "opened": {runtime for runtime, _ in opened},
                    "closed": set(closed),
                    "released": drivers[0].release.is_set(),
                })
                return super().__exit__(*exc)

        def make_handler(*args, **kwargs):
            handler = real_handler(*args, **kwargs)
            if pending_response:
                def task_payload(request, task, runner):
                    with request._runtime() as runtime:
                        check.assertTrue(drivers[0].started.wait(timeout=2))
                        response_pending.set()
                        if not drivers[0].release.wait(timeout=10):
                            raise AssertionError("cleanup did not unblock the HTTP request")
                        return runtime_tests.web_module.task_api_payload(
                            runtime, task, activity=runner.status(task))
                handler._task_payload = task_payload
            return handler

        def first_request(*args, **kwargs):
            timeouts.append(kwargs.get("timeout"))
            self.assertEqual(len(timeouts), 1)
            try:
                with real_urlopen(*args, **kwargs) as response:
                    self.assertEqual(response.status, 202)
                    response.read()
            except TimeoutError as timeout:
                if not pending_response:
                    raise
                self.assertTrue(response_pending.is_set())
                self.assertTrue(drivers[0].started.is_set())
                errors.append(timeout)
                raise
            self.assertFalse(pending_response, "the blocked response did not time out")
            self.assertTrue(drivers[0].started.wait(timeout=2))
            self.assertFalse(drivers[0].release.is_set())
            errors.append(error)
            raise error

        try:
            with ExitStack() as patches:
                patches.enter_context(redirect_stderr(stderr))
                for module, name, value in (
                    (clarification_tests, "BlockingDriver", ObservedDriver),
                    (clarification_tests, "Runtime", ObservedRuntime),
                    (clarification_tests, "urlopen", first_request),
                    (runtime_tests, "ThreadingHTTPServer", ObservedServer),
                    (runtime_tests, "make_handler", make_handler),
                    (clarification_tests.tempfile, "TemporaryDirectory", ObservedDirectory),
                ):
                    patches.enter_context(patch.object(module, name, value))
                with self.assertRaises(TimeoutError if pending_response else type(error)) as raised:
                    getattr(fixture, METHOD)()
            self.assertEqual(timeouts, [4])
            self.assertEqual(len(errors), 1)
            self.assertIs(raised.exception, errors[0])
            self.assertIsNone(raised.exception.__context__)
            self.assertEqual(len(deletions), 1)
            deletion, = deletions
            self.assertIs(deletion["exception"], raised.exception)
            self.assertTrue(deletion["released"])
            self.assertEqual(deletion["live"], [])
            self.assertEqual(deletion["listening"], [])
            self.assertTrue(deletion["opened"])
            self.assertEqual(deletion["opened"], deletion["closed"])
            self.assertTrue(all(not Path(directory.name).exists() for directory in directories))
            self.assertGreater(drivers[0].calls, 0)
            self.assertTrue(any(owner.name.startswith("cater-run-") for _, owner in opened))
            self.assertNotIn("PermissionError", stderr.getvalue())
        finally:
            # A regression must not leak its own blocked fixture resources.
            for driver in drivers:
                driver.release.set()
            fixture.doCleanups()
            for server in servers:
                server.shutdown()
                server.server_close()
            for thread in set(threads + [owner for _, owner in opened]):
                if thread is not threading.current_thread():
                    thread.join(timeout=5)
            for directory in directories:
                directory.cleanup()

    def test_assertion_after_first_response_keeps_original_exception(self):
        self.check_first_request_failure(error=AssertionError("injected first response assertion"))

    def test_timeout_after_first_response_keeps_original_exception(self):
        self.check_first_request_failure(error=TimeoutError("injected first response timeout"))

    def test_real_timeout_drains_request_and_consumer_before_directory_delete(self):
        self.check_first_request_failure(pending_response=True)


if __name__ == "__main__":
    unittest.main()
