"""Exercise the actual nested Web setUp and its owned resource cleanup."""
from contextlib import contextmanager
from pathlib import Path
import shutil
import threading
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen

from cater.runtime import Runtime
import test_archive_member_preview as preview
import test_team_archive_web as archive
import test_runtime as web
import support.nested_team_web as nested


class RecordingResult(unittest.TestResult):
    def __init__(self):
        super().__init__()
        self.exceptions = []

    def addError(self, test, err):
        self.exceptions.append(err[1])
        super().addError(test, err)

    def addFailure(self, test, err):
        self.exceptions.append(err[1])
        super().addFailure(test, err)


def leaves(error):
    if isinstance(error, BaseExceptionGroup):
        return [leaf for item in error.exceptions for leaf in leaves(item)]
    return [error]


class NestedTeamWebCleanupTests(unittest.TestCase):
    cases = ((archive, archive.TeamArchiveWebTests, 'test_bad_sequence_and_plain_text_are_not_archive_downloads'),
             (preview, preview.ArchiveMemberPreviewWebTests, 'test_authentication_host_and_task_scope_apply_to_preview_and_download'))

    def test_actual_setup_failure_cleans_nested_resources(self):
        for module, cls, method in self.cases:
            with self.subTest(cls=cls.__name__):
                case = cls(method)
                error = RuntimeError('synthetic setup failure')
                result = RecordingResult()
                with patch.object(nested.NestedTeamFixture, 'bind_workspace', side_effect=error):
                    case.run(result)
                self.assertEqual(result.exceptions, [error])
                self.assertFalse(case.f.root.exists())

    def test_inner_setup_failure_after_directory_registration_is_cleaned(self):
        for module, cls, method in self.cases:
            with self.subTest(cls=cls.__name__):
                case = cls(method)
                error = RuntimeError('synthetic runtime setup failure')
                result = RecordingResult()
                with patch('test_simple_team.Runtime', side_effect=error):
                    case.run(result)
                self.assertEqual(result.exceptions, [error])
                self.assertFalse(case.f.root.exists())

    def test_setup_failure_after_server_start_closes_server_and_directory(self):
        for module, cls, method in self.cases:
            with self.subTest(cls=cls.__name__):
                case = cls(method)
                result = RecordingResult()
                servers = []
                error = RuntimeError('synthetic post-start setup failure')
                def start_then_fail(testcase, db, **options):
                    options['return_server'] = True
                    _, server, thread = web.start_test_web(testcase, db, **options)
                    servers.append((server, thread))
                    raise error
                with patch.object(module, 'start_test_web', start_then_fail):
                    case.run(result)
                self.assertEqual(result.exceptions, [error])
                self.assertEqual(len(servers), 1)
                self.assertFalse(servers[0][1].is_alive())
                self.assertEqual(servers[0][0].fileno(), -1)
                self.assertFalse(case.f.root.exists())

    def test_directory_cleanup_failure_is_reported_and_preserves_remaining_files(self):
        for module, cls, method in self.cases:
            with self.subTest(cls=cls.__name__):
                case = cls(method)
                result = RecordingResult()
                error = OSError('synthetic directory cleanup failure')
                def body():
                    case.resources.directory.cleanup = lambda: (_ for _ in ()).throw(error)
                try:
                    with patch.object(case, method, body):
                        case.run(result)
                    self.assertEqual(len(result.exceptions), 1)
                    self.assertIn(error, leaves(result.exceptions[0]))
                    self.assertTrue((case.f.root / 'test.db').exists())
                finally:
                    if hasattr(case.f, 'root') and case.f.root.exists():
                        shutil.rmtree(case.f.root)

    def test_actual_setup_keeps_live_request_directory_and_all_exceptions(self):
        for module, cls, method in self.cases:
            with self.subTest(cls=cls.__name__):
                entered, release = threading.Event(), threading.Event()
                servers = []
                case = cls(method)
                result = RecordingResult()
                body_error = ValueError('synthetic body error')
                first = RuntimeError('synthetic inner cleanup one')
                second = OSError('synthetic inner cleanup two')
                start = web.start_test_web
                close = web.TrackedTestServer.close_bounded

                @contextmanager
                def held_runtime():
                    with Runtime(case.f.root / 'test.db', roles_path=case.f.roles,
                                 drivers={'team_test': case.f.driver}, close_drivers_on_close=False) as rt:
                        try:
                            yield rt
                        finally:
                            entered.set()
                            release.wait(15)

                def start_held(testcase, db, **options):
                    options.update(return_server=True, runtime_factory=held_runtime, recover_on_startup=False)
                    base, server, thread = start(testcase, db, **options)
                    servers.append((server, thread))
                    return base

                def close_short(server, thread):
                    return close(server, thread, timeout=0.2)

                def fail(error):
                    raise error

                def body():
                    case.f.addCleanup(fail, first)
                    case.f.addCleanup(fail, second)
                    request = Request(case.base + '/api/tasks', headers={'X-Cater-Access-Token': 'fixture-only-access'})
                    with urlopen(request, timeout=3) as response:
                        response.read()
                    self.assertTrue(entered.wait(3))
                    raise body_error

                try:
                    with patch.object(module, 'start_test_web', start_held), \
                         patch.object(web.TrackedTestServer, 'close_bounded', close_short), \
                         patch.object(case, method, body):
                        case.run(result)
                    self.assertEqual(len(result.exceptions), 2)
                    self.assertIs(result.exceptions[0], body_error)
                    failures = leaves(result.exceptions[1])
                    self.assertTrue(any(error is first for error in failures))
                    self.assertTrue(any(error is second for error in failures))
                    self.assertTrue(any(isinstance(error, web.LiveTestThreadTimeout) for error in failures))
                    self.assertTrue(any(thread.is_alive() for thread, _ in servers[0][0]._requests))
                    self.assertTrue((case.f.root / 'test.db').exists())
                finally:
                    release.set()
                    for server, serving in servers:
                        for thread, _ in server._requests:
                            thread.join(5)
                            self.assertFalse(thread.is_alive())
                        serving.join(5)
                        self.assertFalse(serving.is_alive())
                    if hasattr(case.f, 'root') and case.f.root.exists():
                        shutil.rmtree(case.f.root)

    def test_preview_additional_servers_preserve_directory_for_live_request(self):
        methods = ('test_unauthenticated_loopback_server_rejects_foreign_host',
                   'test_http_responses_release_snapshot_before_send_and_allow_independent_commit')
        for method in methods:
            with self.subTest(method=method):
                case = preview.ArchiveMemberPreviewWebTests(method)
                result = RecordingResult()
                entered, release = threading.Event(), threading.Event()
                servers = []
                start, close = web.start_test_web, web.TrackedTestServer.close_bounded
                started = 0
                first = True

                class HeldRuntime(Runtime):
                    def close(self):
                        nonlocal first
                        if first:
                            first = False
                            entered.set()
                            release.wait(15)
                        return super().close()

                def observed_start(testcase, db, **options):
                    nonlocal started
                    started += 1
                    extra = started == 2
                    requested_tuple = options.get('return_server', False)
                    options['return_server'] = True
                    if extra:
                        options['recover_on_startup'] = False
                        options['runtime_factory'] = lambda: HeldRuntime(db)
                    base, server, thread = start(testcase, db, **options)
                    servers.append((server, thread))
                    if extra:
                        request = Request(base + '/api/tasks', headers={'X-Cater-Access-Token': 'fixture-only-access'})
                        with urlopen(request, timeout=3) as response:
                            response.read()
                        self.assertTrue(entered.wait(3))
                    return (base, server, thread) if requested_tuple else base

                def close_short(server, thread):
                    return close(server, thread, timeout=0.2)

                try:
                    with patch.object(preview, 'start_test_web', observed_start), \
                         patch.object(web.TrackedTestServer, 'close_bounded', close_short):
                        case.run(result)
                    self.assertEqual(started, 2)
                    self.assertEqual(len(result.exceptions), 1, result.errors + result.failures)
                    failures = leaves(result.exceptions[0])
                    self.assertTrue(any(isinstance(error, web.LiveTestThreadTimeout) for error in failures))
                    self.assertTrue(any(thread.is_alive() for thread, _ in servers[1][0]._requests))
                    self.assertTrue((case.f.root / 'test.db').exists())
                finally:
                    release.set()
                    for server, serving in servers:
                        for thread, _ in server._requests:
                            thread.join(5)
                            self.assertFalse(thread.is_alive())
                        serving.join(5)
                        self.assertFalse(serving.is_alive())
                    if hasattr(case.f, 'root') and case.f.root.exists():
                        shutil.rmtree(case.f.root)


if __name__ == '__main__':
    unittest.main()
