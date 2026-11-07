"""Preserve failures while draining the real compact dispatch HTTP fixture."""

from contextlib import ExitStack, redirect_stderr
import io
from pathlib import Path
import sqlite3
import sys
import threading
import unittest
from unittest.mock import patch

import test_runtime as fixtures


METHOD = "test_web_supports_compact_members_and_leader_dispatch"


class DispatchCleanupTests(unittest.TestCase):
    def check_failure(self, *, pending_response=False, error=None):
        fixture = fixtures.WebTests(METHOD)
        runtime_type = fixtures.web_module.Runtime
        server_type = fixtures.TrackedTestServer
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
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                directories.append(self)

            def cleanup(self):
                owners = set(threads + [owner for _, owner in opened])
                owners.discard(threading.current_thread())
                deletions.append({
                    "exception": sys.exception(),
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
                return super().cleanup()

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
                    (fixtures, "TrackedTestServer", ObservedServer),
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
            # TrackedTestServer explicitly joins daemon handlers.
            self.assertTrue(servers)
            self.assertTrue(any(server._requests for server in servers))
            self.assertTrue(all(not thread.is_alive() for server in servers for thread, _ in server._requests))
            self.assertTrue(all(not thread.is_alive() for thread in threads))
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

    def test_real_timeout_without_release_retains_directory_and_original_error(self):
        fixture = fixtures.WebTests(METHOD)
        runtime_type = fixtures.web_module.Runtime
        server_type = fixtures.TrackedTestServer
        directory_type = fixtures.tempfile.TemporaryDirectory
        real_urlopen = fixtures.urlopen
        started, release = threading.Event(), threading.Event()
        servers, directories, cleanup_calls, http_errors, request_errors = [], [], [], [], []

        class BlockedRuntime(runtime_type):
            def battle(self, *args, **kwargs):
                started.set()
                if not release.wait(30):
                    raise AssertionError('test did not release the blocked battle')
                return super().battle(*args, **kwargs)

        class CapturedServer(server_type):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                servers.append(self)
            def handle_error(self, *args):
                request_errors.append(sys.exception())

        class CapturedDirectory(directory_type):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                directories.append(self)
            def cleanup(self):
                cleanup_calls.append(self)
                return super().cleanup()

        def observe_request(request, **kwargs):
            try:
                return real_urlopen(request, **kwargs)
            except TimeoutError as error:
                if str(getattr(request, 'full_url', request)).endswith('/battle'):
                    http_errors.append(error)
                raise

        def chain(error):
            pending, seen, found = [error], set(), []
            while pending:
                current = pending.pop()
                if current is None or id(current) in seen:
                    continue
                seen.add(id(current))
                found.append(current)
                if isinstance(current, BaseExceptionGroup):
                    pending.extend(current.exceptions)
                pending.extend((current.__cause__, current.__context__))
            return found

        try:
            with patch.object(fixtures.web_module, 'Runtime', BlockedRuntime), \
                 patch.object(fixtures, 'TrackedTestServer', CapturedServer), \
                 patch.object(fixtures.tempfile, 'TemporaryDirectory', CapturedDirectory), \
                 patch.object(fixtures, 'urlopen', observe_request), \
                 redirect_stderr(io.StringIO()):
                with self.assertRaises(fixtures.LiveTestThreadTimeout) as raised:
                    getattr(fixture, METHOD)()
            self.assertTrue(started.is_set())
            self.assertEqual(len(http_errors), 1)
            self.assertTrue(any(error is http_errors[0] for error in chain(raised.exception)))
            self.assertEqual(len(servers), 1)
            self.assertEqual(len(directories), 1)
            self.assertTrue(any(owner.is_alive() for owner, _ in servers[0]._requests))
            self.assertTrue(Path(directories[0].name).is_dir())
            self.assertEqual(cleanup_calls, [])
        finally:
            release.set()
            stopped = True
            for server in servers:
                for owner, _ in server._requests:
                    owner.join(timeout=10)
                    stopped = stopped and not owner.is_alive()
            fixture.doCleanups()
            if stopped:
                for directory in directories:
                    directory.cleanup()
            self.assertTrue(stopped, 'blocked request still alive; temporary directory retained')
            self.assertTrue(all(isinstance(error, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError))
                                for error in request_errors))


if __name__ == "__main__":
    unittest.main()
