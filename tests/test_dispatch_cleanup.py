"""Preserve failures while draining the real compact dispatch HTTP fixture."""

from contextlib import ExitStack, redirect_stderr
import io
from pathlib import Path
import sqlite3
import threading
import unittest
from unittest.mock import patch

import test_runtime as fixtures


METHOD = "test_web_supports_compact_members_and_leader_dispatch"


class DispatchCleanupTests(unittest.TestCase):
    def check_failure(self, *, pending_response=False, error=None):
        fixture = fixtures.WebTests(METHOD)
        runtime_type = fixtures.web_module.Runtime
        server_type = fixtures.ThreadingHTTPServer
        directory_type = fixtures.tempfile.TemporaryDirectory
        real_urlopen = fixtures.urlopen
        release, started = threading.Event(), threading.Event()
        servers, threads, opened, closed, directories = [], [], [], [], []
        deletions, timeouts, errors = [], [], []
        stderr = io.StringIO()
        check = self

        class ObservedRuntime(runtime_type):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                opened.append((self, threading.current_thread()))

            def battle(self, *args, **kwargs):
                if pending_response:
                    started.set()
                    if not release.wait(timeout=12):
                        raise AssertionError("fixture cleanup did not release the actual battle")
                return super().battle(*args, **kwargs)

            def close(self):
                super().close()
                with check.assertRaisesRegex(sqlite3.ProgrammingError, "closed"):
                    self.log.db.execute("SELECT 1")
                closed.append(self)

        class ObservedServer(server_type):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                servers.append(self)

            def serve_forever(self, *args, **kwargs):
                threads.append(threading.current_thread())
                return super().serve_forever(*args, **kwargs)

            def process_request_thread(self, *args, **kwargs):
                threads.append(threading.current_thread())
                return super().process_request_thread(*args, **kwargs)

            def shutdown(self):
                release.set()
                return super().shutdown()

        class ObservedDirectory(directory_type):
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
                    "opened": {runtime for runtime, _ in opened}, "closed": set(closed),
                })
                # Capture the ordering first, then keep even a failing regression leak-free.
                release.set()
                fixture.doCleanups()
                for server in servers:
                    server.shutdown()
                    server.server_close()
                for owner in owners:
                    owner.join(timeout=5)
                    check.assertFalse(owner.is_alive(), owner.name)
                return super().__exit__(*exc)

        def observe_request(request, **kwargs):
            timeouts.append(kwargs.get("timeout"))
            if not str(getattr(request, "full_url", request)).endswith("/battle"):
                return real_urlopen(request, **kwargs)
            try:
                response = real_urlopen(request, **kwargs)
            except TimeoutError as timeout:
                check.assertTrue(pending_response)
                check.assertTrue(started.is_set())
                errors.append(timeout)
                raise
            with response:
                response.read()
            check.assertFalse(pending_response, "the blocked battle did not time out")
            errors.append(error)
            raise error

        try:
            with ExitStack() as patches:
                patches.enter_context(redirect_stderr(stderr))
                for module, name, value in (
                    (fixtures.web_module, "Runtime", ObservedRuntime),
                    (fixtures, "ThreadingHTTPServer", ObservedServer),
                    (fixtures.tempfile, "TemporaryDirectory", ObservedDirectory),
                    (fixtures, "urlopen", observe_request),
                ):
                    patches.enter_context(patch.object(module, name, value))
                with self.assertRaises(TimeoutError if pending_response else type(error)) as raised:
                    getattr(fixture, METHOD)()
            self.assertEqual(timeouts, [5, 5, 5])
            self.assertEqual(len(errors), 1)
            self.assertIs(raised.exception, errors[0])
            self.assertIsNone(raised.exception.__context__)
            self.assertEqual(len(deletions), 1)
            deletion, = deletions
            self.assertIs(deletion["exception"], raised.exception)
            self.assertEqual(deletion["live"], [])
            self.assertEqual(deletion["listening"], [])
            self.assertTrue(deletion["opened"])
            self.assertEqual(deletion["opened"], deletion["closed"])
            self.assertTrue(all(not Path(directory.name).exists() for directory in directories))
            self.assertTrue(all(not server.daemon_threads for server in servers))
            self.assertNotIn("PermissionError", stderr.getvalue())
        finally:
            release.set()
            fixture.doCleanups()
            for server in servers:
                server.shutdown()
                server.server_close()
            for owner in set(threads + [owner for _, owner in opened]):
                if owner is not threading.current_thread():
                    owner.join(timeout=5)
            for directory in directories:
                directory.cleanup()

    def test_assertion_after_battle_preserves_original_exception(self):
        self.check_failure(error=AssertionError("injected battle response assertion"))

    def test_real_battle_timeout_closes_request_before_directory_delete(self):
        self.check_failure(pending_response=True)


if __name__ == "__main__":
    unittest.main()
