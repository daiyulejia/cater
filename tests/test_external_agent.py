"""Offline integration: real ACP transport, real relay, no model executable."""

import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from cater.acp import AcpSession, AcpProtocolError, AcpUnavailable
from cater.availability import team_availability
from cater.drivers import driver_from_dict
from cater.events import Actor
from cater.external_agent import ExternalYield, QoderAcpDriver
from cater.mcp_host import McpToolBridge, ToolBridgeError
from cater.runtime import Runtime


PEER = Path(__file__).parent / "fixtures" / "external_agent_peer.py"
TOOLS = [{"name": "save", "description": "Record receipt",
          "input_schema": {"type": "object"}}]


class ExternalAgentTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.driver = QoderAcpDriver("worker", "never-launch-qoder", "ExactModel", timeout=5,
                                     token_budget=1000)
        self.events, self.sessions, self.bridges, self.calls = [], [], [], []
        self.mode = "normal"
        self.close_error = self.cancel_error = self.bridge_error = None
        self.cancel_send_error = None
        owner = self

        class Session(AcpSession):
            def __init__(self, command, **kwargs):
                owner.assertEqual(command, owner.driver.command)
                super().__init__([sys.executable, "-u", str(PEER.resolve()),
                                  owner.mode, str(owner.root)], **kwargs)
                owner.sessions.append(self)

            def cancel(self):
                owner.events.append("cancel")
                result = super().cancel()
                if owner.cancel_error:
                    raise owner.cancel_error
                return result

            def _send(self, value, deadline, **kwargs):
                if isinstance(value, dict) and value.get("method") == "session/cancel":
                    owner.events.append("wire-cancel")
                    if owner.cancel_send_error:
                        raise owner.cancel_send_error
                return super()._send(value, deadline, **kwargs)

            def close(self):
                owner.events.append("session-close")
                super().close()
                if owner.close_error:
                    raise owner.close_error

        class Bridge(McpToolBridge):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                owner.bridges.append(self)

            def revoke(self):
                owner.events.append("revoke")
                super().revoke()

            def close(self):
                owner.events.append("bridge-close")
                super().close()
                if owner.bridge_error:
                    raise owner.bridge_error

        self.enterContext(patch("cater.external_agent.AcpSession", Session))
        self.enterContext(patch("cater.external_agent.McpToolBridge", Bridge))

    def execute(self, call):
        self.calls.append((threading.get_ident(), call))
        return {"status": "ok"}

    def new_run(self, **kwargs):
        return self.driver.agent_run(
            cwd=self.root, tools=TOOLS, execute=kwargs.get("execute", self.execute),
            check_current=kwargs.get("check_current", lambda: None),
            on_update=kwargs.get("on_update"),
        )

    def wire(self):
        return [json.loads(line) for line in
                (self.root / "external-wire.jsonl").read_text().splitlines()]

    def assert_stopped(self, run):
        self.assertTrue(run.state.cleanup_complete)
        for session in self.sessions:
            if session._proc is not None:
                self.assertIsNotNone(session._proc.poll())
        self.assertTrue(all(b._closed and b._revoked for b in self.bridges))

    def test_last_observations_advance_and_failed_tools_record_end(self):
        with patch("cater.external_agent.time.monotonic", return_value=101.0) as clock:
            fail = False
            def execute(call):
                clock.return_value += 1
                if fail:
                    raise ValueError("PRIVATE_TOOL_ERROR")
                return {"status": "ok"}
            run = self.new_run(execute=execute)
            run._started_at = 100.0
            run._phase = "prompting"
            run._update({"sessionUpdate": "plan"})
            first = run.state.observations
            clock.return_value = 102.0
            run._update({"sessionUpdate": "plan"})
            facts = dict(run.state.observations)
            self.assertEqual(facts["first_public_update_ms"], 1000)
            self.assertEqual(facts["last_public_update_ms"], 2000)
            self.assertEqual(dict(first)["last_public_update_ms"], 1000)
            clock.return_value = 103.0
            run._call(object())
            self.assertEqual(dict(run.state.observations)["last_tool_end_ms"], 4000)
            clock.return_value = 105.0
            fail = True
            with self.assertRaises(ValueError):
                run._call(object())
            facts = dict(run.state.observations)
            self.assertEqual(facts["first_tool_ms"], 3000)
            self.assertEqual(facts["last_tool_end_ms"], 6000)
            self.assertEqual(facts["tool_elapsed_ms"], 2000)
            self.assertEqual(facts["tool_calls"], 2)
            self.assertNotIn("PRIVATE_TOOL_ERROR", str(facts))

    def test_observations_are_frozen_host_timings_without_payloads(self):
        updates = []
        run = self.new_run(on_update=updates.append)
        self.assertEqual(run.state.observations, ())
        run.run("PRIVATE_PROMPT")
        state = run.state
        facts = dict(state.observations)
        self.assertEqual(facts["tool_calls"], 2)
        self.assertEqual(facts["public_updates"], 2)
        self.assertLessEqual(facts["handshake_ms"], facts["prompt_attempt_ms"])
        self.assertLessEqual(facts["prompt_attempt_ms"], facts["first_public_update_ms"])
        self.assertLessEqual(facts["first_public_update_ms"], facts["first_tool_ms"])
        self.assertLessEqual(facts["first_public_update_ms"], facts["last_public_update_ms"])
        self.assertLessEqual(facts["last_public_update_ms"], facts["response_ms"])
        self.assertLessEqual(facts["first_tool_ms"], facts["last_tool_end_ms"])
        self.assertLessEqual(facts["last_tool_end_ms"], facts["response_ms"])
        self.assertLessEqual(facts["first_tool_ms"], facts["response_ms"])
        self.assertLessEqual(facts["response_ms"], facts["settled_ms"])
        self.assertTrue(all(type(value) is int and value >= 0 for value in facts.values()))
        self.assertNotIn("PRIVATE_PROMPT", str(facts))
        self.assertNotIn("observed", str(facts))
        self.assertNotIn("PRIVATE_THOUGHT", str(updates))
        self.assertNotIn("PRIVATE_THOUGHT", str(state))
        self.assertEqual(state.observations, run.state.observations)
        self.assertIsNone(state.failure_phase)

    def test_handshake_failure_observations_do_not_claim_prompt_or_tools(self):
        self.mode = "identity"
        run = self.new_run()
        with self.assertRaises(AcpProtocolError):
            run.run("PRIVATE_PROMPT")
        self.assertEqual(run.state.failure_phase, "opening")
        facts = dict(run.state.observations)
        self.assertEqual(set(facts), {"failure_ms", "settled_ms"})
        self.assertLessEqual(facts["failure_ms"], facts["settled_ms"])

    def test_exact_flags_lazy_construction_and_configuration_close(self):
        self.assertEqual(self.driver.command, [
            "never-launch-qoder", "--acp", "--model", "ExactModel",
            "--max-model-request-retries", "0", "--tools", "", "--strict-mcp-config",
            "--setting-sources", "local", "--permission-mode", "default",
        ])
        self.assertEqual((self.driver.name, self.driver.model, self.driver.token_budget,
                          self.driver.execution_profile),
                         ("worker", "ExactModel", 1000, "qoder-managed-v1"))
        run = self.new_run()
        self.assertEqual((self.sessions, self.bridges), ([], []))
        self.assertEqual(run.state.phase, "created")
        self.driver.command.append("--paid-fallback")
        self.assertNotIn("--paid-fallback", self.driver.command)
        self.driver.close()
        with self.assertRaises(RuntimeError):
            self.new_run()
        with self.assertRaises(NotImplementedError):
            self.driver.complete("hello")
        self.assertEqual(run.run("hello").stop_reason, "end_turn")
        self.assert_stopped(run)

    def test_real_relay_executes_on_run_thread_with_sqlite_and_inactive_open(self):
        database = sqlite3.connect(":memory:")
        self.addCleanup(database.close)
        database.execute("create table receipts (value integer)")
        updates = []

        def execute(call):
            database.execute("insert into receipts values (?)", (call.arguments["value"],))
            return self.execute(call)

        run = self.new_run(execute=execute, on_update=updates.append)
        result = run.run("do work")
        self.assertEqual((result.text, result.stop_reason), ("observed", "end_turn"))
        self.assertEqual(database.execute("select value from receipts").fetchall(), [(1,), (2,)])
        self.assertEqual({tid for tid, _ in self.calls}, {threading.get_ident()})
        self.assertEqual(run.state.phase, "finished")
        self.assertTrue(run.state.prompt_attempted)
        self.assertEqual(run.state.text, "observed")
        self.assertEqual(run.state.stop_reason, "end_turn")
        self.assertNotEqual(run.state.phase, "delivered")
        wire = self.wire()
        early = next(row["relay"] for row in wire if row.get("relay", {}).get("id") == "early")
        self.assertIn("error", early)
        replies = {row["id"]: row for row in wire if "id" in row and "method" not in row}
        self.assertEqual(replies["allowed"]["result"]["outcome"],
                         {"outcome": "selected", "optionId": "yes"})
        for name in ("unknown", "prefix", "builtin", "always", "malformed"):
            self.assertEqual(replies[name]["result"]["outcome"]["outcome"], "cancelled")
        self.assertIn("error", replies["fs"])
        self.assertFalse((self.root / "forbidden").exists())
        self.assertTrue(any(u["sessionUpdate"] == "tool_call" for u in updates))
        self.assert_stopped(run)

    def test_runs_have_independent_sessions_capabilities_and_tools(self):
        first, second = self.new_run(), self.new_run()
        first.run("one")
        second.run("two")
        self.assertNotEqual(first.state.session_id, second.state.session_id)
        self.assertIsNot(self.sessions[0], self.sessions[1])
        self.assertIsNot(self.bridges[0], self.bridges[1])
        self.assertNotEqual(self.bridges[0]._secret, self.bridges[1]._secret)
        self.assertEqual([call.id for _, call in self.calls], ["mcp-1", "mcp-2"] * 2)
        with self.assertRaises(RuntimeError):
            first.run("replay")
        self.assert_stopped(second)

    def test_model_and_agent_mismatches_never_prompt_or_execute(self):
        for mode in ("model", "identity", "duplicate", "missing"):
            with self.subTest(mode=mode):
                self.mode = mode
                run = self.new_run()
                with self.assertRaises(AcpProtocolError):
                    run.run("must not send")
                self.assertFalse(run.state.prompt_attempted)
                self.assertEqual(run.state.phase, "failed")
                self.assertFalse(any(row.get("method") == "session/prompt" for row in self.wire()))
                self.assertEqual(self.calls, [])
                self.assert_stopped(run)

    def test_host_state_change_stops_next_service_after_successful_receipt(self):
        signal = ExternalYield("host delivered")
        current = True

        def execute(call):
            nonlocal current
            current = False
            run.revoke_tools()
            return self.execute(call)

        def check():
            if not current:
                raise signal

        run = self.new_run(execute=execute, check_current=check)
        with self.assertRaises(ExternalYield) as caught:
            run.run("deliver")
        self.assertIs(caught.exception, signal)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(run.state.phase, "yielded")
        self.assertEqual(run.state.text, "observed")
        self.assertLess(self.events.index("revoke"), self.events.index("cancel"))
        self.assertLess(self.events.index("wire-cancel"), self.events.index("session-close"))
        self.assert_stopped(run)

    def test_executor_control_exception_keeps_identity_and_cancels_before_close(self):
        for signal in (ExternalYield("waited"), ValueError("scope"), KeyboardInterrupt()):
            with self.subTest(signal=type(signal)):
                self.events.clear()
                def execute(call):
                    self.execute(call)
                    raise signal
                run = self.new_run(execute=execute)
                before = len(self.calls)
                with self.assertRaises(type(signal)) as caught:
                    run.run("stop")
                self.assertIs(caught.exception, signal)
                self.assertEqual(len(self.calls) - before, 1)
                self.assertEqual(run.state.failure_phase, "tool_execution")
                facts = dict(run.state.observations)
                self.assertEqual(facts["tool_calls"], 1)
                self.assertIn("tool_elapsed_ms", facts)
                self.assertLessEqual(facts["first_tool_ms"], facts["failure_ms"])
                self.assertNotIn("response_ms", facts)
                self.assertLess(self.events.index("revoke"), self.events.index("wire-cancel"))
                self.assertLess(self.events.index("wire-cancel"), self.events.index("bridge-close"))
                self.assert_stopped(run)

    def test_observer_control_exception_survives_acp_wrapping(self):
        signal = ExternalYield("observer stopped")
        def update(body):
            raise signal
        run = self.new_run(on_update=update)
        with self.assertRaises(ExternalYield) as caught:
            run.run("observe")
        self.assertIs(caught.exception, signal)
        self.assertEqual(self.calls, [])
        self.assertIn("wire-cancel", self.events)
        self.assert_stopped(run)

    def test_cleanup_failures_never_mask_control_exception_or_claim_success(self):
        self.cancel_error = OSError("cancel failed")
        self.close_error = AcpUnavailable("session cleanup failed")
        self.bridge_error = OSError("bridge cleanup failed")
        signal = ExternalYield("delivered")
        def execute(call):
            raise signal
        run = self.new_run(execute=execute)
        with self.assertRaises(ExternalYield) as caught:
            run.run("stop")
        self.assertIs(caught.exception, signal)
        self.assertFalse(run.state.cleanup_complete)
        self.assertTrue(run.state.cancel_failed)
        self.assertEqual(run.state.cancel_failures, (self.cancel_error,))
        self.assertNotIn(self.cancel_error, run.state.cleanup_failures)
        for error in (self.close_error, self.bridge_error):
            self.assertIn(error, run.state.cleanup_failures)
        self.assertIn("session-close", self.events)
        self.assertIn("bridge-close", self.events)

    def test_revoke_tools_preserves_success_receipt_and_rejects_next_call(self):
        receipt = {"status": "ok", "outcome": "waited"}

        def execute(call):
            self.execute(call)
            run.revoke_tools()
            run.revoke_tools()
            self.assertNotIn("cancel", self.events)
            self.assertNotIn("bridge-close", self.events)
            denied = self.sessions[-1].on_request("session/request_permission", {
                "sessionId": run.state.session_id,
                "toolCall": {"_meta": {"qoder": {"toolName": "mcp__cater__save"}}},
                "options": [{"kind": "allow_once", "optionId": "yes"}],
            })
            self.assertEqual(denied["outcome"]["outcome"], "cancelled")
            return receipt

        run = self.new_run(execute=execute)
        run.run("wait and return receipt")
        replies = {row["relay"]["id"]: row["relay"] for row in self.wire() if "relay" in row}
        self.assertFalse(replies["one"]["result"]["isError"])
        self.assertEqual(json.loads(replies["one"]["result"]["content"][0]["text"]), receipt)
        self.assertIn("error", replies["two"])
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(run.state.cancel_failed)
        self.assert_stopped(run)

    def test_revoke_before_start_is_not_reactivated(self):
        run = self.new_run()
        run.revoke_tools()
        run.run("no authority")
        self.assertEqual(self.calls, [])
        self.assert_stopped(run)

    def test_revoke_failure_is_local_cleanup_evidence_without_losing_receipt(self):
        error = OSError("revoke failed")
        receipt = {"status": "ok", "outcome": "returned"}

        def execute(call):
            self.execute(call)
            bridge = self.bridges[-1]
            original = bridge.revoke

            def revoke():
                original()
                raise error

            with patch.object(bridge, "revoke", revoke):
                run.revoke_tools()
            return receipt

        run = self.new_run(execute=execute)
        with self.assertRaises(OSError) as caught:
            run.run("return")
        self.assertIs(caught.exception, error)
        reply = next(row["relay"] for row in self.wire()
                     if row.get("relay", {}).get("id") == "one")
        self.assertEqual(json.loads(reply["result"]["content"][0]["text"]), receipt)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(run.state.cleanup_failures, (error,))
        self.assertFalse(run.state.cleanup_complete)
        self.assertFalse(run.state.cancel_failed)

    def test_cancel_failure_does_not_mark_closed_local_resources_unclean(self):
        for send_failure in (False, True):
            with self.subTest(send_failure=send_failure):
                error = AcpUnavailable("cancel transport failed")
                self.cancel_send_error = error if send_failure else None
                self.cancel_error = None if send_failure else error
                signal = ExternalYield("host finished")

                def execute(call):
                    raise signal

                run = self.new_run(execute=execute)
                with self.assertRaises(ExternalYield) as caught:
                    run.run("finish")
                self.assertIs(caught.exception, signal)
                self.assertTrue(run.state.cancel_failed)
                self.assertEqual(run.state.cancel_failures, (error,))
                self.assertEqual(run.state.cleanup_failures, ())
                self.assertEqual(run.state.phase, "yielded")
                self.assertIsNone(run.state.stop_reason)
                self.assert_stopped(run)

    def test_successful_prompt_still_reports_cleanup_failure(self):
        self.mode = "text"
        self.close_error = OSError("close failed")
        run = self.new_run()
        with self.assertRaises(OSError) as caught:
            run.run("text")
        self.assertIs(caught.exception, self.close_error)
        self.assertEqual(run.state.stop_reason, "end_turn")
        self.assertFalse(run.state.cleanup_complete)
        self.assertEqual(run.state.phase, "failed")
        self.assertEqual(run.state.failure_phase, "cleanup")
        facts = dict(run.state.observations)
        self.assertLessEqual(facts["response_ms"], facts["failure_ms"])
        self.assertLessEqual(facts["failure_ms"], facts["settled_ms"])

    def test_no_retry_login_or_fallback_after_transport_failure(self):
        for mode in ("reject", "disconnect"):
            with self.subTest(mode=mode):
                self.mode = mode
                before = len(self.sessions)
                run = self.new_run()
                with self.assertRaises((AcpUnavailable, ToolBridgeError)):
                    run.run("only once")
                self.assertEqual(len(self.sessions), before + 1)
                self.assertTrue(run.state.prompt_attempted)
                with self.assertRaises(RuntimeError):
                    run.run("no replay")
                self.assertEqual(self.calls, [])
                methods = [row["method"] for row in self.wire() if "method" in row]
                self.assertEqual(methods.count("session/prompt"), before + 1)
                self.assertTrue(set(methods) <= {"initialize", "session/new", "session/prompt",
                                                "session/cancel"})
                self.assert_stopped(run)

    def test_transport_error_revokes_before_acp_abort_and_preserves_original(self):
        self.mode = "reject"
        self.close_error = OSError("cleanup failed")
        run = self.new_run()
        with self.assertRaises(AcpUnavailable):
            run.run("reject")
        self.assertLess(self.events.index("revoke"), self.events.index("cancel"))
        self.assertLess(self.events.index("cancel"), self.events.index("session-close"))
        self.assertIn(self.close_error, run.state.cleanup_failures)
        self.assertFalse(run.state.cleanup_complete)
        self.assertTrue(run.state.prompt_attempted)

    def test_prompt_timeout_cancels_without_retry(self):
        self.mode = "timeout"
        self.driver.timeout = 1
        run = self.new_run()
        with self.assertRaises(AcpUnavailable):
            run.run("timeout")
        self.assertEqual(run.state.failure_phase, "prompting")
        facts = dict(run.state.observations)
        self.assertLessEqual(facts["handshake_ms"], facts["prompt_attempt_ms"])
        self.assertGreaterEqual(facts["failure_ms"], facts["prompt_attempt_ms"])
        self.assertNotIn("first_public_update_ms", facts)
        self.assertNotIn("first_tool_ms", facts)
        self.assertNotIn("response_ms", facts)
        self.assertEqual(sum(row.get("method") == "session/prompt" for row in self.wire()), 1)
        self.assertIn("wire-cancel", self.events)
        self.assertEqual(self.calls, [])
        self.assert_stopped(run)

    def test_scope_exception_during_open_cannot_activate_or_prompt(self):
        signal = ExternalYield("obsolete during handshake")
        def check():
            if self.sessions:
                raise signal
        run = self.new_run(check_current=check)
        with self.assertRaises(ExternalYield) as caught:
            run.run("obsolete")
        self.assertIs(caught.exception, signal)
        self.assertFalse(run.state.prompt_attempted)
        self.assertEqual(self.calls, [])
        self.assertLess(self.events.index("revoke"), self.events.index("cancel"))
        self.assert_stopped(run)

    def test_closed_driver_does_not_cancel_active_run(self):
        def execute(call):
            self.driver.close()
            return self.execute(call)
        run = self.new_run(execute=execute)
        run.run("finish")
        self.assertEqual(len(self.calls), 2)
        self.assertNotIn("cancel", self.events)
        self.assert_stopped(run)

    def test_owner_close_is_observed_by_next_host_service_check(self):
        database = sqlite3.connect(":memory:")
        self.addCleanup(database.close)
        database.execute("create table receipts (value integer)")
        signal = ExternalYield("driver owner closed")

        def execute(call):
            database.execute("insert into receipts values (?)", (call.arguments["value"],))
            owner = threading.Thread(target=self.driver.close)
            owner.start()
            owner.join()
            self.assertTrue(self.driver.closed)
            self.assertNotIn("cancel", self.events)
            self.assertNotIn("session-close", self.events)
            self.assertNotIn("bridge-close", self.events)
            return self.execute(call)

        def check():
            if self.driver.closed:
                raise signal

        run = self.new_run(execute=execute, check_current=check)
        with self.assertRaises(ExternalYield) as caught:
            run.run("observe owner lifecycle")
        self.assertIs(caught.exception, signal)
        self.assertEqual(database.execute("select value from receipts").fetchall(), [(1,)])
        self.assertEqual({tid for tid, _ in self.calls}, {threading.get_ident()})
        self.assertEqual(len(self.calls), 1)
        self.assertLess(self.events.index("revoke"), self.events.index("wire-cancel"))
        self.assert_stopped(run)

    def test_run_constructed_elsewhere_is_owned_by_calling_thread(self):
        runs = []
        thread = threading.Thread(target=lambda: runs.append(self.new_run()))
        thread.start()
        thread.join()
        runs[0].run("on main")
        self.assertEqual({tid for tid, _ in self.calls}, {threading.get_ident()})
        self.assert_stopped(runs[0])

    def test_check_false_stops_before_start(self):
        run = self.new_run(check_current=lambda: False)
        with self.assertRaises(ExternalYield):
            run.run("obsolete")
        self.assertEqual((self.sessions, self.bridges), ([], []))
        self.assertFalse(run.state.prompt_attempted)
        self.assert_stopped(run)


class ExternalAgentConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.process = self.enterContext(patch(
            "subprocess.Popen", side_effect=AssertionError("Offline configuration started a process")))
        self.addCleanup(self.process.assert_not_called)
        self.config = {"kind": "qoder_acp", "name": "product",
                       "executable": "never-launch-qoder", "model": "ExactModel"}

    def member(self, runtime):
        return next(item for item in team_availability(runtime)["members"]
                    if item["role_id"] == "product")

    def test_factory_constructs_lazily_with_defaults_and_overrides(self):
        default = driver_from_dict(self.config)
        self.assertIsInstance(default, QoderAcpDriver)
        self.assertEqual((default.name, default.model, default.timeout, default.token_budget),
                         ("product", "ExactModel", 120, None))
        self.assertEqual(default.execution_profile, "qoder-managed-v1")
        configured = driver_from_dict({**self.config, "timeout": 17, "token_budget": 23})
        self.assertEqual((configured.timeout, configured.token_budget), (17, 23))
        self.assertEqual(configured.command, default.command)
        with tempfile.TemporaryDirectory() as directory:
            run = configured.agent_run(cwd=directory, tools=[], execute=lambda call: {},
                                       check_current=lambda: None, on_update=None)
            self.assertEqual(run.state.phase, "created")
            self.assertFalse(run.state.prompt_attempted)
        default.close()
        configured.close()

    def test_closed_property_is_read_only_and_close_is_idempotent(self):
        driver = driver_from_dict(self.config)
        self.assertFalse(driver.closed)
        with self.assertRaises(AttributeError):
            driver.closed = True
        self.assertFalse(driver.closed)
        driver.close()
        driver.close()
        self.assertTrue(driver.closed)
        with self.assertRaises(AttributeError):
            driver.closed = False
        self.assertTrue(driver.closed)

    def test_zero_budget_is_valid_and_disables_requests_in_availability(self):
        direct = QoderAcpDriver("product", Path("qoder.exe"), "ExactModel", token_budget=0)
        self.assertEqual(direct.token_budget, 0)
        self.assertEqual(direct.command[0], str(Path("qoder.exe")))
        direct.close()
        driver = driver_from_dict({**self.config, "token_budget": 0})
        with Runtime(":memory:", drivers={"product": driver}) as runtime:
            self.assertEqual(self.member(runtime), {
                "role_id": "product", "status": "on_leave", "reason": "budget_exhausted"})

    def test_missing_and_invalid_executable_raise_value_error(self):
        missing = {key: value for key, value in self.config.items() if key != "executable"}
        with self.assertRaisesRegex(ValueError, "executable"):
            driver_from_dict(missing)

        class InvalidPath:
            def __fspath__(self):
                return None

        for executable in (None, 42, False, [], {}, b"qoder", "", "  ", "qoder\0", InvalidPath()):
            with self.subTest(executable=executable):
                with self.assertRaisesRegex(ValueError, "executable"):
                    driver_from_dict({**self.config, "executable": executable})
                with self.assertRaisesRegex(ValueError, "executable"):
                    QoderAcpDriver("product", executable, "ExactModel")

    def test_invalid_budgets_are_still_rejected(self):
        for budget in (-1, True, 1.5, "invalid"):
            with self.subTest(budget=budget):
                with self.assertRaises(ValueError):
                    driver_from_dict({**self.config, "token_budget": budget})
                with self.assertRaises(ValueError):
                    QoderAcpDriver("product", "qoder", "ExactModel", token_budget=budget)

    def test_availability_counts_only_reported_team_model_usage_for_driver(self):
        driver = driver_from_dict({**self.config, "token_budget": 5})
        with Runtime(":memory:", drivers={"product": driver}) as runtime:
            runtime._driver_health.record_success("product")
            task = runtime.create_task("Offline availability accounting")

            def emit(tokens, usage="reported", name="product"):
                runtime.log.emit(type="team.model_called", actor=Actor.system("host"), task_id=task,
                                 payload={"driver": name, "tokens": tokens, "token_usage": usage})

            emit(100, "unavailable")
            emit(100, name="other-driver")
            emit(2)
            self.assertEqual(self.member(runtime)["status"], "available")
            emit(3)
            self.assertEqual(self.member(runtime), {
                "role_id": "product", "status": "on_leave", "reason": "budget_exhausted"})


if __name__ == "__main__":
    unittest.main()
