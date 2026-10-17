"""Observe real follow-up fixture resources before TemporaryDirectory deletion."""

from contextlib import ExitStack, redirect_stderr
import io
from pathlib import Path
import sqlite3
import threading
import time
import unittest
from unittest.mock import patch

from cater import runtime as runtime_module
import test_runtime as fixtures


METHODS = (
    "test_web_queues_a_follow_up_round_after_a_human_direction",
    "test_web_uses_the_latest_queued_strategy_after_multiple_directions",
)


class FollowupCleanupTests(unittest.TestCase):
    def check_cleanup(self, method, mode):
        fixture = fixtures.WebTests(method)
        runtime_type = fixtures.Runtime
        server_type = fixtures.ThreadingHTTPServer
        directory_type = fixtures.tempfile.TemporaryDirectory
        real_urlopen = fixtures.urlopen
        real_run = fixtures.web_module.ActivityRunner._run
        real_finish = fixtures.web_module.ActivityRunner._finish
        real_fail_activity = fixtures.web_module.ActivityRunner._fail_activity
        real_join = threading.Thread.join
        real_assert_true = fixture.assertTrue
        opened, closed, sql_probed = [], [], []
        threads, servers, directories, drivers, deletions = [], [], [], [], []
        wait_failures, requests, command_failures = [], [], []
        probe, acknowledged = threading.Event(), threading.Event()
        injected = AssertionError("injected follow-up command assertion")
        factory_error = RuntimeError("injected pre-construction follow-up factory failure")
        factory_failures, captured_failures, failure_events = [], [], []
        parent_joined, battle_joined, battle_entered = (
            threading.Event(), threading.Event(), threading.Event()
        )
        rescuing = False
        stderr = io.StringIO()
        check = self

        class ObservedRuntime(runtime_type):
            def __init__(self, *args, **kwargs):
                if (mode == "startup-failure" and not factory_failures
                        and threading.current_thread().name.startswith("cater-run-")):
                    factory_failures.append(factory_error)
                    raise factory_error
                super().__init__(*args, **kwargs)
                opened.append((self, threading.current_thread()))
                check.assertEqual(self.log.db.execute("SELECT 1").fetchone()[0], 1)
                sql_probed.append(self)

            def collaborate(self, *args, **kwargs):
                driver = self.drivers["blocking"]
                drivers.append(driver)
                if mode == "wait-failure":
                    deadline = time.monotonic() + 8
                    while not driver.release.wait(timeout=0.01):
                        if probe.is_set():
                            check.assertEqual(self.log.db.execute("SELECT 1").fetchone()[0], 1)
                            acknowledged.set()
                        if time.monotonic() >= deadline:
                            raise AssertionError("follow-up cleanup gate exceeded its bound")
                return super().collaborate(*args, **kwargs)

            def close(self):
                owner, = [owner for runtime, owner in opened if runtime is self]
                check.assertIs(threading.current_thread(), owner)
                if mode == "startup-failure":
                    failure_events.extend(self.log.iter_events(event_types=("activity.failed",)))
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

        def rescue():
            nonlocal rescuing
            rescuing = True
            parent_joined.set()
            battle_joined.set()
            for driver in drivers:
                driver.release.set()
            fixture.doCleanups()
            for server in servers:
                server.shutdown()
                server.server_close()
            for owner in set(threads + [owner for _, owner in opened]):
                if owner is not threading.current_thread():
                    owner.join(timeout=5)
                    check.assertFalse(owner.is_alive(), owner.name)

        class ObservedDirectory(directory_type):
            def __enter__(self):
                directories.append(self)
                return super().__enter__()

            def __exit__(self, *exc):
                owners = set(threads + [owner for _, owner in opened])
                owners.discard(threading.current_thread())
                # These facts precede rescue; successful rescue cannot repair the verdict.
                deletions.append({
                    "exception": exc[1],
                    "live": [owner.name for owner in owners if owner.is_alive()],
                    "listening": [server.fileno() for server in servers if server.fileno() != -1],
                    "opened": {runtime for runtime, _ in opened},
                    "closed": set(closed),
                    "parent_joined": parent_joined.is_set(),
                    "battle_joined": battle_joined.is_set(),
                    "battle_entered": battle_entered.is_set(),
                })
                if mode == "wait-failure" and any(
                    runtime not in closed for runtime, _ in opened
                ):
                    probe.set()
                    check.assertTrue(acknowledged.wait(timeout=2), "owner SQL probe did not respond")
                rescue()
                return super().__exit__(*exc)

        def observe_run(runner, *args, **kwargs):
            threads.append(threading.current_thread())
            if mode == "late-battle" and args[2] == "battle":
                battle_entered.set()
                check.assertTrue(battle_joined.wait(timeout=5), "battle was never joined")
            return real_run(runner, *args, **kwargs)

        def observe_finish(runner, *args, **kwargs):
            parent = mode == "late-battle" and threading.current_thread().name.startswith("cater-run-")
            if parent:
                check.assertTrue(parent_joined.wait(timeout=5), "parent was never joined")
            result = real_finish(runner, *args, **kwargs)
            if parent:
                # Make the child visible before parent join returns, even on old helpers.
                check.assertTrue(battle_entered.wait(timeout=2), "follow-up did not start")
            return result

        def observe_join(owner, *args, **kwargs):
            if mode == "late-battle" and not rescuing:
                if owner.name.startswith("cater-run-"):
                    parent_joined.set()
                elif owner.name.startswith("cater-battle-"):
                    battle_joined.set()
            return real_join(owner, *args, **kwargs)

        def observe_failure(runner, task_id, activity_id, action, error, **kwargs):
            captured_failures.append(error)
            return real_fail_activity(runner, task_id, activity_id, action, error, **kwargs)

        def observe_assert_true(value, *args, **kwargs):
            try:
                return real_assert_true(value, *args, **kwargs)
            except AssertionError as error:
                wait_failures.append(error)
                raise

        def observe_request(request, **kwargs):
            url = str(getattr(request, "full_url", request))
            check.assertTrue(url.startswith("http://127.0.0.1:"), url)
            requests.append((url, kwargs.get("timeout")))
            response = real_urlopen(request, **kwargs)
            if mode in {"command-failure", "late-battle"} and url.endswith("/command"):
                with response:
                    response.read()
                check.assertTrue(drivers)
                check.assertTrue(drivers[0].started.is_set())
                command_failures.append(injected)
                raise injected
            return response

        try:
            with ExitStack() as patches:
                patches.enter_context(redirect_stderr(stderr))
                for target, name, value in (
                    (fixtures, "Runtime", ObservedRuntime),
                    (runtime_module, "load_drivers", lambda *args, **kwargs: {}),
                    (fixtures, "ThreadingHTTPServer", ObservedServer),
                    (fixtures.tempfile, "TemporaryDirectory", ObservedDirectory),
                    (fixtures.web_module.ActivityRunner, "_run", observe_run),
                    (fixtures, "urlopen", observe_request),
                    (fixture, "assertTrue", observe_assert_true),
                ):
                    patches.enter_context(patch.object(target, name, value))
                if mode == "late-battle":
                    patches.enter_context(patch.object(fixtures.web_module.ActivityRunner, "_finish", observe_finish))
                    patches.enter_context(patch.object(threading.Thread, "join", observe_join))
                if mode == "startup-failure":
                    patches.enter_context(patch.object(fixtures.web_module.ActivityRunner, "_fail_activity", observe_failure))
                if mode == "normal":
                    getattr(fixture, method)()
                    error = None
                else:
                    with self.assertRaises(AssertionError) as raised:
                        getattr(fixture, method)()
                    error = raised.exception
                    self.assertIsNone(error.__context__)
                    if mode == "wait-failure":
                        self.assertEqual(len(wait_failures), 1)
                        self.assertIs(error, wait_failures[0])
                        self.assertEqual(str(error), "False is not true")
                        self.assertEqual(len(requests), 1)
                    elif mode == "startup-failure":
                        self.assertEqual(factory_failures, [factory_error])
                        self.assertEqual(len(captured_failures), 1)
                        self.assertIs(captured_failures[0], factory_error)
                        self.assertEqual(len(wait_failures), 1)
                        self.assertIs(error, wait_failures[0])
                        self.assertEqual(str(error), "False is not true")
                        self.assertEqual(len(requests), 1)
                        self.assertTrue(any(
                            event.payload.get("error_type") == "RuntimeError"
                            and event.payload.get("message") == str(factory_error)
                            and event.payload.get("action") == "run"
                            for event in failure_events
                        ))
                    else:
                        self.assertIs(error, injected)
                        self.assertEqual(command_failures, [injected])
                        self.assertEqual(len(requests), 2)
                self.assertEqual(len(deletions), 1)
                deletion, = deletions
                if mode == "late-battle":
                    self.assertTrue(deletion["parent_joined"])
                    self.assertTrue(deletion["battle_entered"])
                    self.assertTrue(deletion["battle_joined"])
                    self.assertTrue(any(owner.name.startswith("cater-battle-") for _, owner in opened))
                self.assertIs(deletion["exception"], error)
                self.assertEqual(deletion["live"], [])
                self.assertEqual(deletion["listening"], [])
                self.assertTrue(deletion["opened"])
                self.assertEqual(deletion["opened"], deletion["closed"])
                self.assertEqual(set(sql_probed), deletion["opened"])
                self.assertTrue(any(owner.name.startswith("cater-run-") for _, owner in opened))
                self.assertTrue(any(thread.name.startswith("cater-run-") for thread in threads))
                self.assertTrue(servers)
                self.assertTrue(all(not server.daemon_threads for server in servers))
                self.assertTrue(all(timeout == 2 for _, timeout in requests))
                self.assertTrue(all(not Path(directory.name).exists() for directory in directories))
                self.assertNotIn("PermissionError", stderr.getvalue())
        finally:
            rescue()
            for directory in directories:
                directory.cleanup()

    def test_normal_followups_close_before_directory_delete(self):
        for method in METHODS:
            with self.subTest(method=method):
                self.check_cleanup(method, "normal")

    def test_real_started_wait_failure_preserves_error_and_closes_resources(self):
        for method in METHODS:
            with self.subTest(method=method):
                self.check_cleanup(method, "wait-failure")

    def test_command_assertion_preserves_identity_and_closes_resources(self):
        for method in METHODS:
            with self.subTest(method=method):
                self.check_cleanup(method, "command-failure")

    def test_cleanup_joins_battle_born_while_joining_parent(self):
        self.check_cleanup(METHODS[0], "late-battle")

    def test_preconstruction_factory_failure_preserves_started_assertion(self):
        self.check_cleanup(METHODS[0], "startup-failure")


if __name__ == "__main__":
    unittest.main()
