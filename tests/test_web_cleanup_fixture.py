"""Controlled cleanup failures for the local HTTP test fixtures."""
from contextlib import ExitStack, contextmanager
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen

from cater.runtime import Runtime
import test_runtime as fixture
import test_external_failure_privacy as privacy_fixture
import test_web_host_security as host_fixture
import test_managed_workspaces as managed_fixture


def errors(error):
    if isinstance(error, BaseExceptionGroup):
        return [nested for item in error.exceptions for nested in errors(item)]
    return [error]


class WebCleanupFixtureTests(unittest.TestCase):
    def test_release_failure_does_not_skip_server_or_background_cleanup(self):
        original = ValueError('synthetic body failure')
        release_error = OSError('synthetic release failure')
        driver = fixture.BlockingDriver()
        event = driver.release
        closed, servers = threading.Event(), []
        class FailingRelease:
            def wait(self, timeout=None):
                return event.wait(timeout)
            def set(self):
                event.set()
                raise release_error
        driver.release = FailingRelease()
        original_close = fixture.TrackedTestServer.close_bounded
        def observe_close(server, thread):
            servers.append((server, thread))
            return original_close(server, thread)
        class ObservedRuntime(Runtime):
            def close(self):
                try:
                    return super().close()
                finally:
                    closed.set()
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / 'release-failure.db')
            def runtime_factory():
                return ObservedRuntime(db, drivers={name: driver for name in ('product', 'design', 'dev', 'test')})
            try:
                with patch.object(fixture.TrackedTestServer, 'close_bounded', observe_close):
                    with self.assertRaises(BaseExceptionGroup) as raised:
                        with fixture.background_test_web(self, db, driver=driver, runtime_factory=runtime_factory) as base:
                            request = Request(base + '/api/tasks', data=json.dumps({'goal': 'fixture', 'run': True,
                                'background': True}).encode(), headers={'Content-Type': 'application/json'}, method='POST')
                            with urlopen(request, timeout=3) as response:
                                response.read()
                            self.assertTrue(driver.started.wait(3))
                            closed.clear()
                            raise original
                failures = errors(raised.exception)
                self.assertTrue(any(error is original for error in failures))
                self.assertTrue(any(error is release_error for error in failures))
                self.assertTrue(closed.is_set())
                self.assertEqual(len(servers), 1)
                server, thread = servers[0]
                self.assertFalse(thread.is_alive())
                self.assertEqual(server.fileno(), -1)
                self.assertTrue(all(not owner.is_alive() for owner, _ in server._requests))
            finally:
                event.set()
                closed.wait(8)

    def test_managed_workspace_setup_retains_directory_through_later_cleanup_error(self):
        entered, release = threading.Event(), threading.Event()
        directory = tempfile.TemporaryDirectory()
        original_cleanup = directory.cleanup
        original_start = fixture.start_test_web
        original_close = fixture.TrackedTestServer.close_bounded
        servers = []
        case = managed_fixture.ManagedWorkspaceTests('test_http_creation_contract_and_replay_without_execution')

        @contextmanager
        def held_runtime():
            with Runtime(case.db, workspaces_path=case.config) as rt:
                try:
                    yield rt
                finally:
                    entered.set()
                    release.wait(8)

        def close_short(server, serving):
            self.assertTrue(entered.wait(3))
            return original_close(server, serving, timeout=0.2)

        def secondary_failure():
            raise RuntimeError('synthetic later cleanup failure')

        try:
            with patch('test_runtime.tempfile.TemporaryDirectory', return_value=directory), \
                 patch.object(directory, 'cleanup', wraps=original_cleanup) as cleanup, \
                 patch.object(fixture.TrackedTestServer, 'close_bounded', close_short):
                case.setUp()
                case.resources.callback(secondary_failure)
                base, server, _ = original_start(case, case.db, resources=case.resources,
                    return_server=True, runtime_factory=held_runtime, recover_on_startup=False)
                servers.append(server)
                with urlopen(base + '/api/tasks', timeout=3) as response:
                    response.read()
                with self.assertRaises(RuntimeError) as raised:
                    case.resources.close()
            self.assertIn('synthetic later cleanup failure', str(raised.exception))
            self.assertIsInstance(raised.exception.__context__, fixture.LiveTestThreadTimeout)
            self.assertTrue(any(thread.is_alive() for thread, _ in servers[0]._requests))
            self.assertTrue(Path(directory.name, 'events.db').exists())
            cleanup.assert_not_called()
        finally:
            release.set()
            for server in servers:
                for thread, _ in server._requests:
                    thread.join(5)
                    self.assertFalse(thread.is_alive())
            case.doCleanups()
            original_cleanup()

    def test_host_security_setup_preserves_live_request_timeout(self):
        entered, release = threading.Event(), threading.Event()
        directory = tempfile.TemporaryDirectory()
        original_cleanup = directory.cleanup
        original_start = fixture.start_test_web
        original_close = fixture.TrackedTestServer.close_bounded
        servers = []

        @contextmanager
        def held_runtime():
            with Runtime(str(Path(directory.name) / 'host.db')) as rt:
                try:
                    yield rt
                finally:
                    entered.set()
                    release.wait(8)

        def start(testcase, db, **options):
            options.update(return_server=True, runtime_factory=held_runtime, recover_on_startup=False)
            base, server, _ = original_start(testcase, db, **options)
            servers.append(server)
            return base

        def close_short(server, serving):
            self.assertTrue(entered.wait(3))
            return original_close(server, serving, timeout=0.2)

        case = host_fixture.WebHostSecurityTests('test_unauthenticated_server_rejects_foreign_host_for_reads_and_same_origin_writes')
        try:
            with patch('test_runtime.tempfile.TemporaryDirectory', return_value=directory), \
                 patch.object(directory, 'cleanup', wraps=original_cleanup) as cleanup, \
                 patch.object(host_fixture, 'start_test_web', start), \
                 patch.object(fixture.TrackedTestServer, 'close_bounded', close_short):
                case.setUp()
                case.test_unauthenticated_server_rejects_foreign_host_for_reads_and_same_origin_writes()
                with self.assertRaises(fixture.LiveTestThreadTimeout) as raised:
                    case.resources.close()
            self.assertIn('request thread is still running', str(raised.exception))
            self.assertTrue(any(thread.is_alive() for thread, _ in servers[0]._requests))
            self.assertTrue(Path(directory.name, 'host.db').exists())
            cleanup.assert_not_called()
        finally:
            release.set()
            for server in servers:
                for thread, _ in server._requests:
                    thread.join(5)
                    self.assertFalse(thread.is_alive())
            case.doCleanups()
            original_cleanup()

    def test_privacy_test_preserves_actual_request_timeout(self):
        entered, release = threading.Event(), threading.Event()
        directory = tempfile.TemporaryDirectory()
        original_cleanup = directory.cleanup
        original_start = fixture.start_test_web
        original_close = fixture.TrackedTestServer.close_bounded
        servers = []

        @contextmanager
        def held_runtime():
            with Runtime(str(Path(directory.name) / 'events.db')) as rt:
                try:
                    yield rt
                finally:
                    entered.set()
                    release.wait(8)

        def start(testcase, db, **options):
            options.update(return_server=True, runtime_factory=held_runtime, recover_on_startup=False)
            base, server, _ = original_start(testcase, db, **options)
            servers.append(server)
            return base

        def close_short(server, serving):
            self.assertTrue(entered.wait(3))
            return original_close(server, serving, timeout=0.2)

        try:
            with patch('test_runtime.tempfile.TemporaryDirectory', return_value=directory), \
                 patch.object(directory, 'cleanup', wraps=original_cleanup) as cleanup, \
                 patch.object(privacy_fixture, 'start_test_web', start), \
                 patch.object(fixture.TrackedTestServer, 'close_bounded', close_short):
                case = privacy_fixture.ExternalFailurePrivacyTests(
                    'test_gateway_event_reply_and_api_do_not_expose_exception')
                with self.assertRaises(fixture.LiveTestThreadTimeout) as raised:
                    case.test_gateway_event_reply_and_api_do_not_expose_exception()
            self.assertIn('request thread is still running', str(raised.exception))
            self.assertTrue(any(thread.is_alive() for thread, _ in servers[0]._requests))
            self.assertTrue(Path(directory.name, 'events.db').exists())
            cleanup.assert_not_called()
        finally:
            release.set()
            for server in servers:
                for thread, _ in server._requests:
                    thread.join(5)
                    self.assertFalse(thread.is_alive())
            original_cleanup()

    def test_live_database_timeout_retains_directory_without_deleting(self):
        entered, release = threading.Event(), threading.Event()
        directory = tempfile.TemporaryDirectory()
        original_cleanup = directory.cleanup
        server = None
        real_dir_factory = lambda: directory
        @contextmanager
        def runtime_factory():
            with Runtime(str(Path(directory.name) / 'held.db')) as rt:
                try:
                    yield rt
                finally:
                    entered.set()
                    release.wait(8)
        try:
            with patch('test_runtime.tempfile.TemporaryDirectory', side_effect=real_dir_factory), \
                 patch.object(directory, 'cleanup', side_effect=PermissionError('synthetic temporary cleanup')) as cleanup:
                with self.assertRaises(fixture.LiveTestThreadTimeout) as raised:
                    with fixture.preserving_temporary_directory() as tmp, ExitStack() as resources:
                        base, server, _ = fixture.start_test_web(self, str(Path(tmp) / 'held.db'), resources=resources,
                            return_server=True, runtime_factory=runtime_factory, recover_on_startup=False)
                        with urlopen(base + '/api/tasks', timeout=2) as response:
                            response.read()
                        self.assertTrue(entered.wait(3))
                        # Use a short deadline while keeping the request alive.
                        resources.close()
            flattened = errors(raised.exception)
            self.assertTrue(any(isinstance(e, TimeoutError) and 'request thread' in str(e) for e in flattened))
            cleanup.assert_not_called()
            self.assertTrue(Path(directory.name).exists())
            self.assertTrue(any(thread.is_alive() for thread, _ in server._requests))
        finally:
            release.set()
            # The handler owns its connection; wait for it before deleting the fixture directory.
            if server is not None:
                for thread, _ in server._requests:
                    thread.join(5)
            original_cleanup()

    def test_unrelated_temp_cleanup_error_does_not_hide_primary_timeout(self):
        directory = tempfile.TemporaryDirectory()
        original_cleanup = directory.cleanup
        try:
            with patch('test_runtime.tempfile.TemporaryDirectory', return_value=directory), \
                 patch.object(directory, 'cleanup', side_effect=PermissionError('synthetic cleanup failure')):
                with self.assertRaises(BaseExceptionGroup) as raised:
                    with fixture.preserving_temporary_directory():
                        raise TimeoutError('synthetic primary timeout')
            failures = errors(raised.exception)
            self.assertTrue(any(isinstance(error, TimeoutError) and 'primary timeout' in str(error) for error in failures))
            self.assertTrue(any(isinstance(error, PermissionError) for error in failures))
        finally:
            original_cleanup()

    def test_background_join_and_owner_close_continue_after_server_timeout(self):
        driver = fixture.BlockingDriver()
        closed = threading.Event()
        original_close = fixture.TrackedTestServer.close_bounded
        def timeout_after_close(server, thread):
            original_close(server, thread)
            raise TimeoutError('synthetic server timeout')
        class ObservedRuntime(Runtime):
            def close(self):
                try:
                    return super().close()
                finally:
                    closed.set()
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / 'background.db')
            def runtime_factory():
                return ObservedRuntime(db, drivers={name: driver for name in ('product', 'design', 'dev', 'test')})
            try:
                with patch.object(fixture.TrackedTestServer, 'close_bounded', timeout_after_close):
                    with self.assertRaises(TimeoutError) as raised:
                        with fixture.background_test_web(self, db, driver=driver, runtime_factory=runtime_factory) as base:
                            request = Request(base + '/api/tasks', data=json.dumps({'goal': 'fixture', 'run': True,
                                'background': True}).encode(), headers={'Content-Type': 'application/json'}, method='POST')
                            with urlopen(request, timeout=3) as response:
                                response.read()
                            self.assertTrue(driver.started.wait(3))
                            closed.clear()
                self.assertIn('synthetic server timeout', str(raised.exception))
                self.assertTrue(closed.is_set(), 'background Runtime did not close before returning')
            finally:
                driver.release.set()
                closed.wait(8)

    def test_background_live_thread_is_reported_and_eventually_released(self):
        release = threading.Event()
        started = threading.Event()
        closed = threading.Event()
        release_error = OSError('release failed before signalling')
        class UnreleasedDriver(fixture.BlockingDriver):
            def complete(self, **kwargs):
                started.set()
                release.wait(15)
                return fixture.DriverResponse(text='{"text":"fixture"}', tokens=1)
        driver = UnreleasedDriver()
        # Cleanup's release fails and does not release this fixture.
        def fail_release(self):
            raise release_error
        driver.release = type('NoRelease', (), {'set': fail_release})()
        original_close = fixture.TrackedTestServer.close_bounded
        def timeout_after_close(server, thread):
            original_close(server, thread)
            raise TimeoutError('synthetic server timeout')
        class ObservedRuntime(Runtime):
            def close(self):
                try:
                    return super().close()
                finally:
                    closed.set()
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / 'live.db')
            def runtime_factory():
                return ObservedRuntime(db, drivers={name: driver for name in ('product', 'design', 'dev', 'test')})
            try:
                with patch.object(fixture.TrackedTestServer, 'close_bounded', timeout_after_close):
                    with self.assertRaises(BaseExceptionGroup) as raised:
                        with fixture.background_test_web(self, db, driver=driver, runtime_factory=runtime_factory) as base:
                            request = Request(base + '/api/tasks', data=json.dumps({'goal': 'fixture', 'run': True,
                                'background': True}).encode(), headers={'Content-Type': 'application/json'}, method='POST')
                            with urlopen(request, timeout=3) as response:
                                response.read()
                            self.assertTrue(started.wait(3))
                            closed.clear()
                failures = errors(raised.exception)
                self.assertTrue(any(e is release_error for e in failures))
                self.assertTrue(any(isinstance(e, TimeoutError) and 'synthetic server timeout' in str(e) for e in failures))
                self.assertTrue(any(isinstance(e, TimeoutError) and 'thread still alive' in str(e) for e in failures))
                self.assertTrue(any('Runtime connection left open or unverified' in str(e) for e in failures))
                self.assertFalse(closed.is_set())
            finally:
                release.set()
                self.assertTrue(closed.wait(8), 'fixture owner did not close after release')


if __name__ == '__main__':
    unittest.main()
