"""Catalog calls preserve original scope, progress, receipt and yield semantics."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from cater.external_agent import ExternalYield, QoderAcpDriver, _ProgressBudget
from cater.external_catalog import GUIDANCE
from cater.tool_protocol import RecordedToolResult, ToolCall
import test_external_cooperative as cooperative


TOOLS = [{"name": name, "description": name, "input_schema": {"type": "object"}}
         for name in ("workspace_run", "return_work")]


class ExternalCatalogRunTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.calls = []
        self.driver = QoderAcpDriver("fixture", "never-launch", "ExactModel", timeout=5,
                                     max_run_seconds=20, tool_surface="catalog")
        self.addCleanup(self.driver.close)

    def run_for(self, execute=None, check=None, tools=None):
        return self.driver.agent_run(cwd=self.root, tools=TOOLS if tools is None else tools,
            execute=execute or (lambda call: self.calls.append(call) or {"status": "ok"}),
            check_current=check or (lambda: None), on_update=None)

    def invoke(self, name="workspace_run", arguments=None, identifier="one"):
        return ToolCall(identifier, "invoke_tool", {"name": name, "arguments": arguments or {}})

    def test_wire_definitions_do_not_replace_original_authorized_tools(self):
        run = self.run_for()
        self.assertEqual([tool["name"] for tool in run._tools], ["workspace_run", "return_work"])
        self.assertEqual([tool["name"] for tool in run._wire_tools], ["describe_tools", "invoke_tool"])
        self.assertEqual(self.calls, [])

    def test_description_reads_are_not_execution_or_useful_progress(self):
        with patch("cater.external_agent.time.monotonic", return_value=0) as clock:
            run = self.run_for()
            run._budget = _ProgressBudget(5, 20)
            clock.return_value = 1
            for arguments in ({}, {"names": ["workspace_run"]}, {"names": ["return_work"]}):
                self.assertEqual(run._catalog_call(ToolCall("read", "describe_tools", arguments))["status"], "ok")
            self.assertEqual(self.calls, [])
            self.assertEqual(run._budget.progress_at, 0)
            self.assertEqual(run._budget.seen, set())
            self.assertNotIn("tool_calls", dict(run.state.observations))
            clock.return_value = 6
            self.assertEqual(run._budget.expired().limit, "idle")

    def test_failed_command_and_duplicate_progress_use_the_original_tool(self):
        result = {"status": "error", "outcome": "failed", "complete": True, "timed_out": False,
                  "exit_code": 1, "error": {"code": "failed"}}
        def execute(call):
            self.calls.append(call)
            return result
        with patch("cater.external_agent.time.monotonic", return_value=0) as clock:
            run = self.run_for(execute=execute)
            run._budget = _ProgressBudget(5, 20)
            clock.return_value = 1
            self.assertIs(run._catalog_call(self.invoke(arguments={"command": "python"})), result)
            self.assertEqual(run._budget.progress_at, 1)
            self.assertEqual(len(run._budget.seen), 1)
            clock.return_value = 2
            run._catalog_call(self.invoke(arguments={"command": "python"}, identifier="two"))
            self.assertEqual(run._budget.progress_at, 1)
            self.assertEqual([call.name for call in self.calls], ["workspace_run", "workspace_run"])

    def test_original_recorded_receipt_survives_a_terminal_call(self):
        result = RecordedToolResult({"status": "ok", "message_seq": 44}, 45, "a" * 64)
        run = None
        def execute(call):
            self.calls.append(call)
            run.revoke_tools()
            return result
        run = self.run_for(execute=execute)
        self.assertIs(run._catalog_call(self.invoke("return_work", {"summary": "done"})), result)
        self.assertEqual(result.receipt_reference()["source_seq"], 45)
        self.assertTrue(run._tools_revoked)
        for call in (self.invoke(), ToolCall("read", "describe_tools", {})):
            with self.assertRaises(ExternalYield):
                run._catalog_call(call)
        self.assertEqual(len(self.calls), 1)

    def test_scope_signal_prevents_both_metadata_and_execution(self):
        for call in (self.invoke(), ToolCall("read", "describe_tools", {})):
            signal = ExternalYield("scope changed")
            def check():
                raise signal
            run = self.run_for(check=check)
            with self.assertRaises(ExternalYield) as caught:
                run._catalog_call(call)
            self.assertIs(caught.exception, signal)
            self.assertTrue(run._tools_revoked)
        self.assertEqual(self.calls, [])

    def test_outer_rejections_do_not_reach_or_count_the_host_executor(self):
        run = self.run_for()
        for call in (self.invoke("workspace_write"), ToolCall("bad", "invoke_tool", {"name": "return_work"}),
                     ToolCall("bad", "workspace_run", {})):
            result = run._catalog_call(call)
            self.assertEqual(result["status"], "error")
            self.assertEqual(result["outcome"], "not_started")
        self.assertEqual(self.calls, [])
        self.assertNotIn("tool_calls", dict(run.state.observations))

    def test_permissions_only_allow_current_wire_names_and_never_always(self):
        run = self.run_for()
        run._phase = "prompting"
        run._session = SimpleNamespace(session_id="fixture")
        for name in ("describe_tools", "invoke_tool", "workspace_run", "return_work", "other"):
            params = {"sessionId": "fixture", "toolCall": {"_meta": {"qoder": {"toolName": "mcp__cater__" + name}}},
                      "options": [{"kind": "allow_once", "optionId": "yes"}]}
            outcome = run._request("session/request_permission", params)["outcome"]["outcome"]
            self.assertEqual(outcome, "selected" if name in {"describe_tools", "invoke_tool"} else "cancelled")
            params["options"][0]["kind"] = "allow_always"
            self.assertEqual(run._request("session/request_permission", params)["outcome"]["outcome"], "cancelled")

    def test_catalog_with_no_original_tools_remains_text_only(self):
        run = self.run_for(tools=[])
        self.assertIsNone(run._catalog)
        self.assertEqual(run._wire_tools, [])

    def test_partial_terminal_receipt_drains_before_cancel_without_second_action(self):
        host = cooperative.ExternalCooperativeTests(methodName="runTest")
        host.setUp()
        self.addCleanup(host.doCleanups)
        host.driver.tool_surface = "catalog"
        host.terminal = True
        iterator = host.controlled()
        def request(identifier):
            host.socket.incoming += json.dumps({"jsonrpc": "2.0", "id": identifier,
                "method": "tools/call", "params": {"name": "invoke_tool",
                    "arguments": {"name": "save", "arguments": {"value": identifier}}}}).encode() + b"\n"
        host.socket.request = request
        host.advance_to_receipt(iterator)
        partial = bytes(host.socket.sent)
        self.assertNotIn(b"\n", partial)
        self.assertNotIn("cancel", host.lifecycle)
        self.assertEqual(host.effects, [1])
        with self.assertRaises(ExternalYield) as caught:
            while True:
                next(iterator)
        self.assertIs(caught.exception, host.signal)
        response = json.loads(host.socket.sent)["response"]
        self.assertFalse(response["result"]["isError"])
        self.assertEqual(json.loads(response["result"]["content"][0]["text"])["value"], 1)
        self.assertEqual(host.effects, [1])
        self.assertTrue(bytes(host.socket.sent).startswith(partial))
        self.assertTrue(host.bridges[0]._buffer)
        self.assertLess(host.lifecycle.index("revoke"), host.lifecycle.index("cancel"))
        self.assertTrue(host.sessions[0].prompts[0].startswith(GUIDANCE))
        host.assert_clean(host.run)
