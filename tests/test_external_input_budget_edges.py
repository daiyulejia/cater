"""Offline budget edges: memory pipes/bridge, fake clock, no process or model.

Compose the duplex fixture without inheriting its tests. Only handshake setup and
bridge scheduling are fixtures; ACP send/permission handling and run budget,
service, tool dispatch, stop and cleanup use their real implementations.
"""

from __future__ import annotations

from contextlib import contextmanager
import json
import queue
import threading
import unittest
from unittest.mock import patch

from cater import external_agent
from cater.acp import AcpSession, AcpTimeout
from cater.external_agent import ExternalRunLimit
from cater.tool_protocol import ToolCall
import test_acp_duplex_boundaries as memory


class ExternalInputBudgetEdgeTests(unittest.TestCase):
    @contextmanager
    def fixture(self, now, pump=None):
        fixture = memory.AcpDuplexBoundaryTests()
        fixture.setUp()
        original_prompt = AcpSession._iter_prompt

        def prompt(session, *args, **kwargs):
            # Keep the input deadline after the run's first budget deadline.
            now[0] = 100.1
            return (yield from original_prompt(session, *args, **kwargs))

        def iter_open(session, **kwargs):
            if False:
                yield
            return session.open(**kwargs)

        def service_bridge(*args, **kwargs):
            return False if pump is None else pump()

        try:
            with patch("cater.acp.time.monotonic", lambda: now[0]), \
                    patch.object(AcpSession, "_iter_prompt", prompt), \
                    patch.object(AcpSession, "iter_open", iter_open), \
                    patch.object(memory.MemoryBridge, "pump", service_bridge), \
                    patch.object(memory.MemoryBridge, "poll", service_bridge, create=True):
                yield fixture
        finally:
            try:
                fixture.check_cleanup()
            finally:
                fixture.doCleanups()

    def drive(self, run, cooperative):
        if not cooperative:
            return run.run("Purely offline memory budget boundary")
        iterator = run.iter_run("Purely offline memory budget boundary")
        try:
            while True:
                next(iterator)
        except StopIteration as done:
            return done.value
        finally:
            iterator.close()

    def assert_stopped(self, fixture, run, bridges, error):
        self.assertIs(run._failure, error)
        self.assertEqual(run.state.phase, "failed")
        self.assertTrue(run.state.prompt_attempted)
        self.assertTrue(run.state.cleanup_complete)
        self.assertEqual(run.state.cleanup_failures, ())
        self.assertEqual(run.state.cancel_failures, ())
        self.assertTrue(bridges)
        self.assertTrue(all(bridge.revoked and bridge.closed for bridge in bridges))
        self.assertFalse(fixture.sessions[-1].input_write_pending)
        fixture.terminal(fixture.sessions[-1])
        with self.assertRaises(RuntimeError):
            run.run("Never replay a settled or failed offline prompt")

    def test_permission_reply_writer_pending_is_input_failure(self):
        for cooperative in (False, True):
            for blocked_at in ("write", "flush"):
                for total in (2, 5):
                    with self.subTest(cooperative=cooperative, blocked_at=blocked_at, total=total):
                        self.check_permission_reply(cooperative, blocked_at, total)

    def check_permission_reply(self, cooperative, blocked_at, total):
        now = [100.0]
        reply_entered = threading.Event()

        class ReplyInput(memory.MemoryInput):
            def write(self, data):
                message = json.loads(data)
                if message.get("id") == "permission-1" and blocked_at == "write":
                    self.release.clear()
                    reply_entered.set()
                return super().write(data)

            def flush(self):
                if self.attempts[-1].get("id") == "permission-1" and blocked_at == "flush":
                    self.release.clear()
                    reply_entered.set()
                    if not self.release.wait(memory.WAIT):
                        raise OSError("Offline permission flush watchdog expired")
                return super().flush()

        pipe = ReplyInput(reject_cancel=True)
        permission = {"jsonrpc": "2.0", "id": "permission-1",
                      "method": "session/request_permission", "params": {
                          "sessionId": memory.SID,
                          "toolCall": {"_meta": {"qoder": {"toolName": "mcp__cater__inspect"}}},
                          "options": [{"kind": "allow_once", "optionId": "offline-once"}],
                      }}
        with self.fixture(now) as fixture:
            run, bridges, execute, updates = fixture.external(
                pipe=pipe, total=total, incoming=[memory.frame(permission),
                    memory.frame(memory.update("after-permission")), memory.frame(memory.result())])
            run._tools = [{"name": "inspect"}]
            pending = []

            def current():
                if not reply_entered.is_set():
                    return
                session = fixture.sessions[-1]
                self.assertTrue(session.input_write_pending)
                self.assertEqual(session._write_owner, threading.get_ident())
                self.assertFalse(session._write_done.is_set())
                self.assertFalse(pipe.threads[0].is_alive(), "Outer prompt write must already be done")
                pending.append(session._write_done)
                now[0] = 102.0

            run._check_current = current
            with self.assertRaises(AcpTimeout) as caught:
                self.drive(run, cooperative)
            error = caught.exception
            self.assertIs(type(error), AcpTimeout)
            self.assertEqual(error.phase, "input")
            self.assertTrue(error.prompt_may_have_run)
            self.assertTrue(pending)
            self.assertEqual(pipe.attempts, [pipe.attempts[0], {
                "jsonrpc": "2.0", "id": "permission-1", "result": {
                    "outcome": {"outcome": "selected", "optionId": "offline-once"}}}])
            self.assertEqual(pipe.attempts[0]["method"], "session/prompt")
            self.assertEqual(updates, [])
            self.assertIsNone(run.state.stop_reason)
            self.assertEqual((run._budget.started, run._budget.total), (100.0, total))
            self.assertTrue(fixture.sessions[-1]._input_failed)
            execute.assert_not_called()
            self.assert_stopped(fixture, run, bridges, error)

    def test_done_writer_and_restored_write_state_keep_idle_or_total_limit(self):
        for cooperative in (False, True):
            for boundary in ("done_registered", "state_restored"):
                for total in (2, 5):
                    with self.subTest(cooperative=cooperative, boundary=boundary, total=total):
                        self.check_completed_write(cooperative, boundary, total)

    def check_completed_write(self, cooperative, boundary, total):
        now = [100.0]
        pipe = memory.MemoryInput(blocked=True)
        with self.fixture(now) as fixture:
            run, bridges, execute, _ = fixture.external(pipe=pipe, total=total)
            completed = []

            def current():
                if not fixture.sessions:
                    return
                session = fixture.sessions[-1]
                if session._write_done is not None:
                    self.assertTrue(pipe.started.wait(memory.WAIT))
                    done = session._write_done
                    pipe.release.set()
                    self.assertTrue(done.wait(memory.WAIT))
                    self.assertEqual(session._write_owner, threading.get_ident())
                    self.assertFalse(session.input_write_pending)
                    if boundary == "done_registered":
                        completed.append(done)
                        now[0] = 102.0
                elif pipe.started.is_set() and boundary == "state_restored":
                    self.assertIsNone(session._write_owner)
                    self.assertFalse(session.input_write_pending)
                    completed.append(None)
                    now[0] = 102.0

            run._check_current = current
            with self.assertRaises(ExternalRunLimit) as caught:
                self.drive(run, cooperative)
            self.assertEqual(caught.exception.limit, "total" if total == 2 else "idle")
            self.assertNotEqual(caught.exception.phase, "input")
            self.assertTrue(completed)
            self.assertEqual([m["method"] for m in pipe.attempts], ["session/prompt", "session/cancel"])
            self.assertEqual((run._budget.started, run._budget.total), (100.0, total))
            execute.assert_not_called()
            self.assert_stopped(fixture, run, bridges, caught.exception)

    def test_foreign_thread_sees_no_pending_input_and_keeps_its_budget_limit(self):
        for cooperative in (False, True):
            for total in (2, 5):
                with self.subTest(cooperative=cooperative, total=total):
                    now = [100.0]
                    pipe = memory.MemoryInput(blocked=True, reject_cancel=True)
                    with self.fixture(now) as fixture:
                        run, bridges, execute, _ = fixture.external(pipe=pipe, total=total)
                        observations, errors = [], []

                        def foreign_check():
                            session = fixture.sessions[-1]
                            observations.append((threading.get_ident(), session.input_write_pending))
                            try:
                                run._check_budget()
                            except BaseException as error:
                                errors.append(error)

                        def current():
                            if not fixture.sessions or fixture.sessions[-1]._write_done is None:
                                return
                            self.assertTrue(pipe.started.wait(memory.WAIT))
                            session = fixture.sessions[-1]
                            self.assertTrue(session.input_write_pending)
                            now[0] = 102.0
                            observer = threading.Thread(target=foreign_check, daemon=True)
                            observer.start()
                            observer.join(memory.WAIT)
                            self.assertFalse(observer.is_alive())
                            self.assertTrue(session.input_write_pending)
                            self.assertFalse(session._write_done.is_set())

                        run._check_current = current
                        with self.assertRaises(AcpTimeout) as caught:
                            self.drive(run, cooperative)
                        self.assertEqual(len(observations), 1)
                        self.assertNotEqual(observations[0][0], threading.get_ident())
                        self.assertIs(observations[0][1], False)
                        self.assertEqual(len(errors), 1)
                        self.assertIs(type(errors[0]), ExternalRunLimit)
                        self.assertEqual(errors[0].limit, "total" if total == 2 else "idle")
                        self.assertIs(type(caught.exception), AcpTimeout)
                        self.assertEqual(caught.exception.phase, "input")
                        execute.assert_not_called()
                        self.assert_stopped(fixture, run, bridges, caught.exception)

    def test_legacy_missing_or_merely_truthy_property_keeps_execution_limit(self):
        for cooperative in (False, True):
            for property_value in ("missing", 1):
                for total in (2, 5):
                    with self.subTest(cooperative=cooperative, property_value=property_value, total=total):
                        self.check_legacy_session(cooperative, property_value, total)

    def check_legacy_session(self, cooperative, property_value, total):
        now = [100.0]
        pipe = memory.MemoryInput(blocked=True, reject_cancel=True)

        class LegacySession:
            def __init__(self, session):
                self.wrapped = session

            def __getattr__(self, name):
                if name == "input_write_pending":
                    if property_value == "missing":
                        raise AttributeError(name)
                    return property_value
                return getattr(self.wrapped, name)

        with self.fixture(now) as fixture:
            run, bridges, execute, _ = fixture.external(pipe=pipe, total=total)
            factory = external_agent.AcpSession.side_effect
            pending = []
            original_errors = []
            original_check = external_agent._ProgressBudget.check

            def checked(budget):
                try:
                    return original_check(budget)
                except ExternalRunLimit as error:
                    original_errors.append(error)
                    raise

            def current():
                if not fixture.sessions or fixture.sessions[-1]._write_done is None:
                    return
                self.assertTrue(pipe.started.wait(memory.WAIT))
                self.assertTrue(fixture.sessions[-1].input_write_pending)
                self.assertFalse(fixture.sessions[-1]._write_done.is_set())
                pending.append(True)
                now[0] = 102.0

            run._check_current = current
            with patch("cater.external_agent.AcpSession", side_effect=lambda *a, **kw:
                       LegacySession(factory(*a, **kw))), \
                    patch.object(external_agent._ProgressBudget, "check", checked):
                with self.assertRaises(ExternalRunLimit) as caught:
                    self.drive(run, cooperative)
            self.assertTrue(pending)
            self.assertEqual(len(original_errors), 1)
            self.assertIs(caught.exception, original_errors[0])
            self.assertEqual(run._cooperative, cooperative)
            self.assertEqual(caught.exception.limit, "total" if total == 2 else "idle")
            self.assertEqual([m["method"] for m in pipe.attempts], ["session/prompt"])
            execute.assert_not_called()
            self.assert_stopped(fixture, run, bridges, caught.exception)

    def test_call_budget_check_stops_before_executor_with_pending_input(self):
        for cooperative in (False, True):
            for total in (2, 5):
                with self.subTest(cooperative=cooperative, total=total):
                    self.check_call_budget(cooperative, total)

    def check_call_budget(self, cooperative, total):
        now = [100.0]
        pipe = memory.MemoryInput(blocked=True, reject_cancel=True)
        call = ToolCall("offline-1", "inspect", {"page": 1})
        dispatching = []

        def pump():
            dispatching.append(threading.get_ident())
            return run._call(call)

        with self.fixture(now, pump) as fixture:
            run, bridges, execute, _ = fixture.external(pipe=pipe, total=total)

            def current():
                if dispatching:
                    self.assertTrue(pipe.started.wait(memory.WAIT))
                    self.assertTrue(fixture.sessions[-1].input_write_pending)
                    self.assertFalse(fixture.sessions[-1]._write_done.is_set())
                    now[0] = 102.0

            run._check_current = current
            with self.assertRaises(AcpTimeout) as caught:
                self.drive(run, cooperative)
            self.assertIs(type(caught.exception), AcpTimeout)
            self.assertEqual(caught.exception.phase, "input")
            self.assertTrue(caught.exception.prompt_may_have_run)
            self.assertEqual(dispatching, [threading.get_ident()])
            self.assertEqual(run.state.failure_phase, "prompting")
            self.assertNotIn("tool_calls", dict(run.state.observations))
            execute.assert_not_called()
            self.assertEqual([m["method"] for m in pipe.attempts], ["session/prompt"])
            self.assert_stopped(fixture, run, bridges, caught.exception)

    def test_executor_exception_keeps_identity_after_budget_expires_with_pending_write(self):
        for cooperative in (False, True):
            for kind in ("limit", "runtime", "queue_empty"):
                with self.subTest(cooperative=cooperative, kind=kind):
                    self.check_executor_exception(cooperative, kind)

    def check_executor_exception(self, cooperative, kind):
        now = [100.0]
        pipe = memory.MemoryInput(blocked=True, reject_cancel=True)
        marker = {"limit": ExternalRunLimit("total"),
                  "runtime": RuntimeError("Offline executor failed"),
                  "queue_empty": queue.Empty("Offline executor control signal")}[kind]
        call = ToolCall("offline-1", "inspect", {"page": 1})
        dispatched = []

        def pump():
            if not dispatched:
                dispatched.append(threading.get_ident())
                return run._call(call)
            return False

        with self.fixture(now, pump) as fixture:
            run, bridges, execute, _ = fixture.external(pipe=pipe, total=5)

            def fail(_call):
                self.assertTrue(pipe.started.wait(memory.WAIT))
                self.assertTrue(fixture.sessions[-1].input_write_pending)
                now[0] = 102.0
                raise marker

            execute.side_effect = fail
            with self.assertRaises(type(marker)) as caught:
                self.drive(run, cooperative)
            self.assertIs(caught.exception, marker)
            self.assertEqual(dispatched, [threading.get_ident()])
            execute.assert_called_once_with(call)
            self.assertEqual(run.state.failure_phase, "tool_execution")
            self.assertIsInstance(run._budget.expired(), ExternalRunLimit)
            self.assertEqual([m["method"] for m in pipe.attempts], ["session/prompt"])
            # A later budget check/stop must not overwrite the executor's first error.
            with self.assertRaises(ExternalRunLimit) as later:
                run._check_budget()
            run._stop(later.exception)
            with self.assertRaises(type(marker)) as repeated:
                run._service()
            self.assertIs(repeated.exception, marker)
            self.assertIs(run._failure, marker)
            self.assertEqual(run.state.failure_phase, "tool_execution")
            execute.assert_called_once_with(call)
            self.assert_stopped(fixture, run, bridges, marker)

    def test_settled_tool_result_survives_input_timeout_without_reexecution(self):
        for cooperative in (False, True):
            for settlement in ("before_expiry", "at_expiry"):
                for total in (2, 5):
                    with self.subTest(cooperative=cooperative, settlement=settlement, total=total):
                        self.check_settled_result(cooperative, settlement, total)

    def check_settled_result(self, cooperative, settlement, total):
        now = [100.0]
        pipe = memory.MemoryInput(blocked=True, reject_cancel=True)
        call = ToolCall("offline-1", "inspect", {"page": 1})
        receipt = {"status": "ok", "complete": True, "offline_receipt": "settled-once"}
        effects, receipts = [], []

        def pump():
            if not receipts:
                receipts.append(run._call(call))
                now[0] = 102.0 if total == 2 or settlement == "at_expiry" else 103.0
            return False

        with self.fixture(now, pump) as fixture:
            run, bridges, execute, _ = fixture.external(pipe=pipe, total=total)

            def settle(_call):
                self.assertTrue(pipe.started.wait(memory.WAIT))
                self.assertTrue(fixture.sessions[-1].input_write_pending)
                effects.append(call.arguments.copy())
                now[0] = 101.0 if settlement == "before_expiry" else 102.0
                return receipt

            execute.side_effect = settle
            with self.assertRaises(AcpTimeout) as caught:
                self.drive(run, cooperative)
            self.assertIs(type(caught.exception), AcpTimeout)
            self.assertEqual(caught.exception.phase, "input")
            self.assertTrue(caught.exception.prompt_may_have_run)
            self.assertEqual(len(receipts), 1)
            self.assertIs(receipts[0], receipt)
            self.assertEqual(effects, [{"page": 1}])
            execute.assert_called_once_with(call)
            facts = dict(run.state.observations)
            self.assertEqual(facts["tool_calls"], 1)
            self.assertIn("last_tool_end_ms", facts)
            self.assertEqual(facts.get("progress_receipts", 0), int(settlement == "before_expiry"))
            self.assertEqual(run._budget.progress_at, 101.0 if settlement == "before_expiry" else 100.0)
            self.assertEqual((run._budget.started, run._budget.total), (100.0, total))
            self.assertEqual([m["method"] for m in pipe.attempts], ["session/prompt"])
            self.assert_stopped(fixture, run, bridges, caught.exception)
            self.assertIs(receipts[0], receipt)
            self.assertEqual(effects, [{"page": 1}])


if __name__ == "__main__":
    unittest.main()
