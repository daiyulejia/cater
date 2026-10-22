"""Cooperative external lifecycle: real gated ACP and controlled MCP backpressure."""

import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from cater.acp import AcpPromptResult, AcpSession
from cater.external_agent import ExternalRunLimit, ExternalYield, QoderAcpDriver
from cater.mcp_host import McpToolBridge, ToolBridgeError


PEER = Path(__file__).parent / "fixtures" / "external_cooperative_peer.py"
TOOLS = [{"name": "save", "description": "Record a receipt", "input_schema": {"type": "object"}}]
OPENED = {"models": {"currentModelId": "chosen", "availableModels": [
    {"modelId": "chosen", "name": "ExactModel"}]}}


def drain(iterator):
    while True:
        try:
            next(iterator)
        except StopIteration as done:
            return done.value


class PartialSocket:
    """Short sends against the real bridge parser/dispatcher, without timing races."""

    def __init__(self):
        self.incoming = b""
        self.sent = bytearray()
        self.closed = False
        self.failure = None

    def settimeout(self, timeout):
        pass

    def recv(self, size):
        chunk, self.incoming = self.incoming[:size], self.incoming[size:]
        if not chunk:
            raise BlockingIOError()
        return chunk

    def send(self, data):
        if self.failure is not None:
            raise self.failure
        size = min(len(data), 17)
        self.sent.extend(data[:size])
        return size

    def shutdown(self, how):
        pass

    def close(self):
        self.closed = True

    def request(self, identifier):
        self.incoming += json.dumps({"jsonrpc": "2.0", "id": identifier,
            "method": "tools/call", "params": {"name": "save", "arguments": {"value": identifier}}
        }).encode() + b"\n"


class ExternalCooperativeTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.driver = QoderAcpDriver("offline", "never-real-model", "ExactModel", timeout=5)
        self.owner = threading.get_ident()
        self.threads = []
        self.sessions = []
        self.bridges = []
        self.effects = []
        self.lifecycle = []
        self.current = True
        self.signal = ExternalYield("host settled")
        self.execute_error = self.cancel_error = self.close_error = None
        self.terminal = False
        self.return_early = False
        self.permissions = []

    def run_for(self, **kwargs):
        return self.driver.agent_run(cwd=kwargs.get("cwd", self.root), tools=TOOLS,
            execute=kwargs.get("execute", self.execute),
            check_current=kwargs.get("check_current", self.check), on_update=kwargs.get("on_update"))

    def check(self):
        self.threads.append(threading.get_ident())
        if not self.current:
            raise self.signal

    def execute(self, call):
        self.threads.append(threading.get_ident())
        self.effects.append(call.arguments["value"])
        if self.execute_error is not None:
            raise self.execute_error
        if self.terminal:
            self.run.revoke_tools()
            self.current = False
        return {"status": "ok", "complete": True, "value": call.arguments["value"]}

    def controlled(self):
        owner = self
        self.socket = PartialSocket()

        class Bridge(McpToolBridge):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self._connection = owner.socket
                self._authenticated = self._initialized = self._ready = True
                owner.bridges.append(self)

            def pump(self, timeout=0.1):
                raise AssertionError("Cooperative service must use poll")

            def revoke(self):
                owner.lifecycle.append("revoke")
                super().revoke()

            def close(self):
                owner.lifecycle.append("bridge-close")
                super().close()

        class Session:
            session_id = "controlled"
            agent_info = {"name": "qoder-cli-cn"}

            def __init__(self, command, **kwargs):
                self.on_request = kwargs["on_request"]
                self.prompts = []
                owner.sessions.append(self)

            def open(self, **kwargs):
                raise AssertionError("Iterator path must use iter_open")

            def prompt(self, *args, **kwargs):
                raise AssertionError("Iterator path must use iter_prompt")

            def iter_open(self, **kwargs):
                yield None
                kwargs["service"]()
                return OPENED

            def iter_prompt(self, prompt, **kwargs):
                self.prompts.append(prompt)
                owner.socket.request(1)
                owner.socket.request(2)
                while True:
                    kwargs["service"]()
                    kwargs["on_update"]({"sessionUpdate": "plan"})
                    if owner.bridges[0].has_pending_output:
                        owner.permissions.append(self.on_request("session/request_permission", {
                            "sessionId": self.session_id, "toolCall": {"_meta": {"qoder": {
                            "toolName": "mcp__cater__save"}}},
                            "options": [{"kind": "allow_once", "optionId": "yes"}]}))
                    yield None
                    if owner.return_early or (not owner.socket.incoming and not owner.bridges[0]._buffer
                                              and not owner.bridges[0].has_pending_output):
                        return AcpPromptResult("controlled", "end_turn")

            def cancel(self):
                owner.lifecycle.append("cancel")
                if owner.cancel_error is not None:
                    raise owner.cancel_error

            def close(self):
                owner.lifecycle.append("session-close")
                if owner.close_error is not None:
                    raise owner.close_error

        self.enterContext(patch("cater.external_agent.AcpSession", Session))
        self.enterContext(patch("cater.external_agent.McpToolBridge", Bridge))
        self.run = self.run_for()
        iterator = self.run.iter_run("one prompt")
        self.addCleanup(iterator.close)
        return iterator

    def advance_to_receipt(self, iterator):
        for _ in range(100):
            self.assertIsNone(next(iterator))
            if self.effects:
                self.assertTrue(self.bridges[0].has_pending_output)
                return
        self.fail("No settled tool receipt")

    def assert_clean(self, run):
        self.assertTrue(run.state.cleanup_complete)
        self.assertTrue(run._bridge._closed)
        self.assertTrue(run._bridge._revoked)
        self.assertEqual(set(self.threads), {self.owner})

    def test_partial_terminal_receipt_flushes_before_yield_without_new_authority(self):
        self.terminal = True
        iterator = self.controlled()
        self.advance_to_receipt(iterator)
        partial = bytes(self.socket.sent)
        self.assertNotIn(b"\n", partial)
        self.assertNotIn("cancel", self.lifecycle)
        self.assertTrue(self.run._tools_revoked)
        self.assertEqual(self.effects, [1])
        yields = 0
        with self.assertRaises(ExternalYield) as caught:
            while True:
                self.assertIsNone(next(iterator))
                yields += 1
        self.assertIs(caught.exception, self.signal)
        self.assertGreater(yields, 1)
        self.assertTrue(bytes(self.socket.sent).startswith(partial))
        response = json.loads(self.socket.sent)["response"]
        self.assertFalse(response["result"]["isError"])
        self.assertEqual(json.loads(response["result"]["content"][0]["text"])["value"], 1)
        self.assertEqual(self.effects, [1])
        self.assertTrue(self.bridges[0]._buffer)
        self.assertTrue(all(p["outcome"]["outcome"] == "cancelled" for p in self.permissions))
        self.assertLess(self.lifecycle.index("revoke"), self.lifecycle.index("cancel"))
        self.assertLess(self.lifecycle.index("cancel"), self.lifecycle.index("session-close"))
        self.assertEqual(self.sessions[0].prompts, ["one prompt"])
        self.assertEqual(self.run.state.phase, "yielded")
        self.assert_clean(self.run)

    def test_prompt_result_cannot_close_bridge_with_pending_receipt(self):
        self.return_early = True
        iterator = self.controlled()
        self.advance_to_receipt(iterator)
        result = drain(iterator)
        self.assertEqual(result, AcpPromptResult("controlled", "end_turn"))
        self.assertFalse(self.bridges[0].has_pending_output)
        self.assertEqual(len(bytes(self.socket.sent).splitlines()), 1)
        self.assertEqual(self.effects, [1])
        self.assert_clean(self.run)

    def test_transport_failure_after_settlement_is_not_hidden_by_deferred_yield(self):
        self.terminal = True
        iterator = self.controlled()
        self.advance_to_receipt(iterator)
        self.socket.failure = OSError("controlled receipt transport failure")
        with self.assertRaises(ToolBridgeError):
            drain(iterator)
        self.assertEqual(self.effects, [1])
        self.assertEqual(self.run.state.phase, "failed")
        self.assertEqual(dict(self.run.state.observations)["tool_calls"], 1)
        self.assert_clean(self.run)

    def test_real_control_error_during_partial_flush_preserves_primary_and_effect(self):
        self.terminal = True
        iterator = self.controlled()
        self.advance_to_receipt(iterator)
        marker = KeyboardInterrupt("control changed while flushing")
        self.signal = marker
        with self.assertRaises(KeyboardInterrupt) as caught:
            drain(iterator)
        self.assertIs(caught.exception, marker)
        self.assertEqual(self.effects, [1])
        self.assert_clean(self.run)

    def test_scope_change_before_prompt_does_not_activate_or_send(self):
        iterator = self.controlled()
        self.assertIsNone(next(iterator))
        self.current = False
        with self.assertRaises(ExternalYield) as caught:
            next(iterator)
        self.assertIs(caught.exception, self.signal)
        self.assertFalse(self.run.state.prompt_attempted)
        self.assertEqual(self.sessions[0].prompts, [])
        self.assertEqual(self.effects, [])
        self.assert_clean(self.run)

    def test_scope_false_with_partial_receipt_revokes_before_flush(self):
        iterator = self.controlled()
        self.advance_to_receipt(iterator)
        self.run._check_current = lambda: False
        with self.assertRaises(ExternalYield):
            drain(iterator)
        self.assertEqual(self.effects, [1])
        self.assertEqual(json.loads(self.socket.sent)["response"]["id"], 1)
        self.assertLess(self.lifecycle.index("revoke"), self.lifecycle.index("cancel"))
        self.assert_clean(self.run)

    def test_close_during_open_never_prompts_and_is_single_use(self):
        iterator = self.controlled()
        self.assertIsNone(next(iterator))
        iterator.close()
        self.assertFalse(self.run.state.prompt_attempted)
        self.assertEqual(self.sessions[0].prompts, [])
        self.assertEqual(self.run.state.failure_phase, "opening")
        self.assertIsInstance(self.run._failure, GeneratorExit)
        self.assertTrue(self.run.state.cleanup_complete)
        self.assertTrue(self.socket.closed)
        with self.assertRaises(RuntimeError):
            next(self.run.iter_run("never start again"))

    def test_second_entry_does_not_replay_or_close_active_iterator(self):
        iterator = self.controlled()
        self.advance_to_receipt(iterator)
        with self.assertRaises(RuntimeError):
            self.run.run("replay")
        with self.assertRaises(RuntimeError):
            next(self.run.iter_run("replay"))
        self.assertNotIn("cancel", self.lifecycle)
        self.assertFalse(self.socket.closed)
        self.assertEqual(drain(iterator).stop_reason, "end_turn")
        self.assertEqual(self.sessions[0].prompts, ["one prompt"])
        self.assertEqual(self.effects, [1, 2])
        self.assert_clean(self.run)

    def test_close_synchronously_revokes_and_cleans_partial_response(self):
        iterator = self.controlled()
        self.advance_to_receipt(iterator)
        iterator.close()
        iterator.close()
        self.assertEqual(self.effects, [1])
        self.assertIsInstance(self.run._failure, GeneratorExit)
        self.assertEqual(self.run.state.phase, "failed")
        self.assertLess(self.lifecycle.index("revoke"), self.lifecycle.index("cancel"))
        self.assertLess(self.lifecycle.index("cancel"), self.lifecycle.index("session-close"))
        self.assert_clean(self.run)
        with self.assertRaises(RuntimeError):
            self.run.run("cannot replay")
        with self.assertRaises(RuntimeError):
            next(self.run.iter_run("cannot replay either"))

    def test_executor_primary_error_survives_cancel_and_cleanup_failures(self):
        marker = ValueError("scope changed in executor")
        self.execute_error = marker
        self.cancel_error = OSError("cancel failed")
        self.close_error = OSError("close failed")
        iterator = self.controlled()
        with self.assertRaises(ValueError) as caught:
            drain(iterator)
        self.assertIs(caught.exception, marker)
        self.assertEqual(self.effects, [1])
        self.assertEqual(self.run.state.failure_phase, "tool_execution")
        self.assertEqual(self.run.state.cancel_failures, (self.cancel_error,))
        self.assertIn(self.close_error, self.run.state.cleanup_failures)
        self.assertFalse(self.run.state.cleanup_complete)
        self.assertTrue(self.socket.closed)

    def test_revoke_without_stop_flushes_and_denies_second_tool(self):
        iterator = self.controlled()
        self.advance_to_receipt(iterator)
        self.run.revoke_tools()
        self.assertEqual(drain(iterator).stop_reason, "end_turn")
        replies = [json.loads(line)["response"] for line in bytes(self.socket.sent).splitlines()]
        self.assertIn("result", replies[0])
        self.assertIn("error", replies[1])
        self.assertEqual(self.effects, [1])
        self.assertNotIn("cancel", self.lifecycle)
        self.assert_clean(self.run)

    def test_yields_do_not_reset_idle_or_total_budget(self):
        for idle, total, end, expected in ((5, 20, 6, "idle"), (5, 5, 6, "total")):
            with self.subTest(expected=expected):
                f = ExternalCooperativeTests()
                f.setUp()
                try:
                    f.driver.timeout = idle
                    f.driver.max_run_seconds = total
                    with patch("cater.external_agent.time.monotonic", return_value=0) as clock:
                        iterator = f.controlled()
                        f.advance_to_receipt(iterator)
                        for _ in range(3):
                            self.assertIsNone(next(iterator))
                        self.assertEqual(f.run._budget.progress_at, 0)
                        clock.return_value = end
                        with self.assertRaises(ExternalRunLimit) as caught:
                            next(iterator)
                        self.assertEqual(caught.exception.limit, expected)
                        self.assertEqual(f.effects, [1])
                        f.assert_clean(f.run)
                finally:
                    f.doCleanups()

    def test_sync_only_custom_session_falls_back_once_without_retry(self):
        calls = []

        class Session:
            session_id = "legacy"
            agent_info = {"name": "qoder-cli-cn"}

            def __init__(self, *args, **kwargs):
                calls.append("construct")

            def open(self, **kwargs):
                calls.append("open")
                return OPENED

            def prompt(self, prompt, **kwargs):
                calls.append(("prompt", prompt))
                return AcpPromptResult("legacy", "end_turn")

            def cancel(self):
                calls.append("cancel")

            def close(self):
                calls.append("close")

        with patch("cater.external_agent.AcpSession", Session):
            run = self.run_for()
            self.assertEqual(drain(run.iter_run("one")), AcpPromptResult("legacy", "end_turn"))
            self.assertEqual(calls, ["construct", "open", ("prompt", "one"), "close"])
            with self.assertRaises(RuntimeError):
                run.run("replay")
        self.assert_clean(run)

    def test_sync_run_preserves_patched_real_session_open_and_prompt(self):
        calls = []

        def opened(session, **kwargs):
            calls.append("open")
            session.session_id = "patched"
            session.agent_info = {"name": "qoder-cli-cn"}
            return OPENED

        def prompted(session, prompt, **kwargs):
            calls.append(("prompt", prompt))
            return AcpPromptResult("patched", "end_turn")

        with patch.object(AcpSession, "open", opened), patch.object(AcpSession, "prompt", prompted), \
                patch.object(AcpSession, "iter_open", side_effect=AssertionError("Bypassed open patch")), \
                patch.object(AcpSession, "iter_prompt", side_effect=AssertionError("Bypassed prompt patch")), \
                patch("subprocess.Popen", side_effect=AssertionError("No real process permitted")):
            run = self.run_for()
            self.assertEqual(run.run("once"), AcpPromptResult("patched", "end_turn"))
        self.assertEqual(calls, ["open", ("prompt", "once")])
        self.assert_clean(run)

    def test_close_real_waiting_session_synchronously_reaps_process(self):
        sessions = []

        class Session(AcpSession):
            def __init__(self, command, **kwargs):
                super().__init__([sys.executable, "-u", str(PEER.resolve()), str(kwargs["cwd"])], **kwargs)
                sessions.append(self)

        with patch("cater.external_agent.AcpSession", Session):
            run = self.run_for()
            iterator = run.iter_run("wait for close")
            self.addCleanup(iterator.close)
            deadline = time.monotonic() + 8
            while not (self.root / "prompt-received").exists():
                self.assertLess(time.monotonic(), deadline)
                self.assertIsNone(next(iterator))
            iterator.close()
            self.assertIsNotNone(sessions[0]._proc.poll())
            self.assert_clean(run)
            self.assertIsInstance(run._failure, GeneratorExit)
            with self.assertRaises(RuntimeError):
                run.run("cannot replay closed session")

    def test_two_real_gated_sessions_interleave_on_owner_thread_and_prompt_once(self):
        owner = self

        class Session(AcpSession):
            def __init__(self, command, **kwargs):
                super().__init__([sys.executable, "-u", str(PEER.resolve()), str(kwargs["cwd"])], **kwargs)
                owner.sessions.append(self)

            def open(self, **kwargs):
                raise AssertionError("Cooperative open bypassed")

            def prompt(self, *args, **kwargs):
                raise AssertionError("Cooperative prompt bypassed")

        roots = [self.root / name for name in ("first", "second")]
        for root in roots:
            root.mkdir()
        calls = []

        def execute(index, call):
            self.threads.append(threading.get_ident())
            calls.append((index, call.arguments["value"]))
            return {"status": "ok", "complete": True}

        with patch("cater.external_agent.AcpSession", Session):
            runs = [self.run_for(cwd=root, execute=lambda call, i=i: execute(i, call))
                    for i, root in enumerate(roots)]
            iterators = [run.iter_run("one prompt") for run in runs]
            for iterator in iterators:
                self.addCleanup(iterator.close)
            deadline = time.monotonic() + 10
            while not all((root / "prompt-received").exists() for root in roots):
                self.assertLess(time.monotonic(), deadline)
                for iterator in iterators:
                    self.assertIsNone(next(iterator))
            for _ in range(4):
                for iterator in iterators:
                    self.assertIsNone(next(iterator))
            self.assertEqual(calls, [])
            self.assertTrue(all(run.state.phase == "prompting" for run in runs))
            (roots[1] / "release").touch()
            while True:
                self.assertLess(time.monotonic(), deadline)
                self.assertIsNone(next(iterators[0]))
                try:
                    self.assertIsNone(next(iterators[1]))
                except StopIteration as done:
                    self.assertEqual(done.value.text, "second")
                    break
            self.assertEqual(calls, [(1, 1), (1, 2)])
            self.assertEqual(runs[0].state.phase, "prompting")
            (roots[0] / "release").touch()
            self.assertEqual(drain(iterators[0]).text, "first")
        self.assertEqual(calls, [(1, 1), (1, 2), (0, 1), (0, 2)])
        for root, run, session in zip(roots, runs, self.sessions):
            wire = [json.loads(line) for line in (root / "wire.jsonl").read_text().splitlines()]
            self.assertEqual(sum(row.get("method") == "session/prompt" for row in wire), 1)
            self.assertEqual(sum(row.get("method") == "session/new" for row in wire), 1)
            self.assertIsNotNone(session._proc.poll())
            self.assert_clean(run)


if __name__ == "__main__":
    unittest.main()
