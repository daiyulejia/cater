"""Offline catalog contracts and direct callback tests, without an ACP peer."""

import contextlib
from copy import deepcopy
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

if __name__ == "__main__":
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cater.external_agent import ExternalRunLimit, ExternalYield, QoderAcpDriver, _ProgressBudget
from cater.external_catalog import ExternalToolCatalog
from cater.mcp_host import _ExecutorAbort
from cater.tool_protocol import RecordedToolResult, ToolCall, prepare_tool_turn


def tool(name="echo", description="Synthetic echo"):
    return {"name": name, "description": description, "input_schema": {
        "type": "object", "properties": {"payload": {
            "type": "object", "properties": {"items": {"type": "array", "items": {"type": "string"}}},
            "required": ["items"],
        }}, "required": ["payload"], "additionalProperties": False,
    }}


class OfflineCase(unittest.TestCase):
    def setUp(self):
        for target in ("subprocess.Popen", "os.system", "socket.socket", "socket.create_connection",
                       "socket.getaddrinfo", "cater.external_agent.AcpSession",
                       "cater.external_agent.McpToolBridge"):
            guard = self.enterContext(patch(target, side_effect=AssertionError("Offline test: " + target)))
            self.addCleanup(guard.assert_not_called)


class ExternalToolCatalogTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.source = [tool(), tool("other", "Other synthetic tool")]
        self.execute = Mock(return_value={"status": "ok", "complete": True})
        self.catalog = ExternalToolCatalog(self.source, self.execute)

    def describe(self, arguments=None):
        return self.catalog.dispatch(ToolCall("read-id", "describe_tools", {} if arguments is None else arguments))

    def invoke(self, name="echo", arguments=None):
        return self.catalog.dispatch(ToolCall("original-id", "invoke_tool", {
            "name": name, "arguments": {} if arguments is None else arguments,
        }))

    def assert_rejected(self, name, arguments):
        before = deepcopy(arguments)
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            result = self.catalog.dispatch(ToolCall("private-call-id", name, arguments))
        self.assertEqual(result, {
            "status": "error", "complete": True, "outcome": "not_started",
            "code": "invalid_catalog_arguments", "message": "请选择本轮工具并按定义提供参数；未执行动作。",
        })
        self.assertNotIn("PRIVATE_CATALOG_SECRET", json.dumps(result))
        self.assertNotIn("private-call-id", json.dumps(result))
        self.assertEqual((output.getvalue(), errors.getvalue()), ("", ""))
        self.assertEqual(arguments, before)
        self.execute.assert_not_called()

    def test_fixed_surface_has_only_two_valid_client_tools(self):
        self.assertEqual([item["name"] for item in self.catalog.tools], ["describe_tools", "invoke_tool"])
        prepare_tool_turn(role="test", prompt="", history=[], tools=self.catalog.tools)
        describe, invoke = [item["input_schema"] for item in self.catalog.tools]
        self.assertFalse(describe["additionalProperties"])
        self.assertEqual(describe["properties"]["names"], {
            "type": "array", "minItems": 1, "maxItems": 2, "uniqueItems": True,
            "items": {"type": "string", "enum": ["echo", "other"]},
        })
        self.assertFalse(invoke["additionalProperties"])
        self.assertEqual(invoke["required"], ["name", "arguments"])
        self.assertEqual(invoke["properties"], {
            "name": {"type": "string", "enum": ["echo", "other"]}, "arguments": {"type": "object"},
        })
        self.execute.assert_not_called()

    def test_constructor_deepcopies_definition_snapshot(self):
        expected = deepcopy(self.source)
        self.source[0]["input_schema"]["properties"]["payload"]["required"].append("changed")
        self.source[0]["name"] = "renamed"
        self.source.append(tool("late"))
        self.assertEqual(self.describe({"names": ["echo", "other"]})["definitions"], expected)
        self.assert_rejected("invoke_tool", {"name": "late", "arguments": {}})

    def test_full_definitions_are_fresh_deepcopies_in_requested_order(self):
        first = self.describe({"names": ["other", "echo"]})["definitions"]
        self.assertEqual(first, self.source[::-1])
        first[1]["input_schema"]["properties"]["payload"]["required"].append("changed")
        first[0]["name"] = "renamed"
        self.assertEqual(self.describe({"names": ["other", "echo"]})["definitions"], self.source[::-1])
        self.execute.assert_not_called()

    def test_summary_is_readonly_and_has_no_full_schema(self):
        expected = {"status": "ok", "tools": [
            {"name": item["name"], "description_excerpt": item["description"], "description_truncated": False}
            for item in self.source
        ]}
        result = self.describe()
        self.assertEqual(result, expected)
        result["tools"][0]["name"] = "renamed"
        self.assertEqual(self.describe(), expected)
        self.execute.assert_not_called()

    def test_summary_truncation_does_not_truncate_full_definition(self):
        for size in (0, 239, 240, 241):
            with self.subTest(size=size):
                definition = tool(description="x" * size)
                catalog = ExternalToolCatalog([definition], self.execute)
                summary = catalog.dispatch(ToolCall("id", "describe_tools", {}))["tools"][0]
                self.assertEqual(summary["description_excerpt"], "x" * min(size, 240))
                self.assertEqual(summary["description_truncated"], size > 240)
                self.assertEqual(catalog.dispatch(ToolCall("id", "describe_tools", {
                    "names": ["echo"],
                }))["definitions"], [definition])
        self.execute.assert_not_called()

    def test_unknown_wrapper_or_inner_name_never_executes_or_echoes(self):
        for name in ("echo", "PRIVATE_CATALOG_SECRET", "", None):
            with self.subTest(wrapper=name):
                self.assert_rejected(name, {"secret": "PRIVATE_CATALOG_SECRET"})
        for name in ("PRIVATE_CATALOG_SECRET", "echo ", "ECHO", "workspace_run", "describe_tools", None, [], {}, True):
            with self.subTest(inner=name):
                self.assert_rejected("invoke_tool", {"name": name, "arguments": {"secret": "PRIVATE_CATALOG_SECRET"}})

    def test_extra_or_missing_outer_fields_reject_entire_request(self):
        for name, arguments in (
            ("describe_tools", {"secret": "PRIVATE_CATALOG_SECRET"}),
            ("describe_tools", {"names": ["echo"], "secret": "PRIVATE_CATALOG_SECRET"}),
            ("invoke_tool", {"name": "echo", "arguments": {}, "secret": "PRIVATE_CATALOG_SECRET"}),
            ("invoke_tool", {}), ("invoke_tool", {"name": "echo"}),
            ("invoke_tool", {"arguments": {"secret": "PRIVATE_CATALOG_SECRET"}}),
        ):
            with self.subTest(name=name, arguments=arguments):
                self.assert_rejected(name, arguments)

    def test_non_object_outer_or_inner_arguments_are_rejected(self):
        for arguments in (None, [], "PRIVATE_CATALOG_SECRET", 0, True):
            for name in ("describe_tools", "invoke_tool"):
                with self.subTest(wrapper=name, arguments=arguments):
                    self.assert_rejected(name, arguments)
            with self.subTest(inner_arguments=arguments):
                self.assert_rejected("invoke_tool", {"name": "echo", "arguments": arguments})

    def test_invalid_names_lists_reject_without_partial_disclosure(self):
        for names in (None, "echo", [], ["echo", "echo"], ["echo", "PRIVATE_CATALOG_SECRET"],
                      ["echo", "other", "echo"], [None], [True], [[]], [{}]):
            with self.subTest(names=names):
                self.assert_rejected("describe_tools", {"names": names})

    def test_invalid_error_objects_are_independent(self):
        first = self.catalog.dispatch(ToolCall("id", "missing", {}))
        first["code"] = "changed"
        self.assert_rejected("missing", {})

    def test_direct_invocation_does_not_require_reading_definitions(self):
        self.assertIs(self.invoke(arguments={"payload": {"items": ["known"]}}), self.execute.return_value)
        self.execute.assert_called_once_with(ToolCall("original-id", "echo", {"payload": {"items": ["known"]}}))

    def test_reading_or_mutating_returned_definitions_never_adds_authority(self):
        self.describe()
        definition = self.describe({"names": ["echo"]})["definitions"][0]
        definition["name"] = "PRIVATE_CATALOG_SECRET"
        self.assert_rejected("describe_tools", {"names": ["PRIVATE_CATALOG_SECRET"]})
        self.assert_rejected("invoke_tool", {"name": "PRIVATE_CATALOG_SECRET", "arguments": {}})

    def test_host_permission_denial_after_read_is_returned_unchanged(self):
        denied = {"status": "error", "complete": True, "outcome": "not_started", "code": "scope_revoked"}
        self.describe({"names": ["echo"]})
        self.execute.assert_not_called()
        self.execute.return_value = denied
        self.assertIs(self.invoke(), denied)
        self.execute.assert_called_once()

    def test_host_remains_responsible_for_original_argument_schema(self):
        rejected = {"status": "error", "code": "host_schema_rejected"}
        self.execute.return_value = rejected
        arguments = {"host_only_field": ["not the declared payload"]}
        self.assertIs(self.invoke(arguments=arguments), rejected)
        self.execute.assert_called_once_with(ToolCall("original-id", "echo", arguments))

    def test_forwarding_preserves_id_name_values_and_deepcopies_arguments(self):
        arguments = {"payload": {"items": ["nested"]}, "values": [None, False, 0, 1.5]}
        original = ToolCall("opaque-id-42", "invoke_tool", {"name": "other", "arguments": arguments})
        expected = deepcopy(original)
        self.assertIs(self.catalog.dispatch(original), self.execute.return_value)
        forwarded = self.execute.call_args.args[0]
        self.assertIs(type(forwarded), ToolCall)
        self.assertEqual(forwarded, ToolCall("opaque-id-42", "other", arguments))
        self.assertIsNot(forwarded.arguments, arguments)
        self.assertIsNot(forwarded.arguments["payload"]["items"], arguments["payload"]["items"])
        self.assertEqual(original, expected)
        forwarded.arguments["payload"]["items"].append("executor mutation")
        self.assertEqual(original, expected)
        arguments["values"].append("caller mutation")
        self.assertEqual(forwarded.arguments["values"], [None, False, 0, 1.5])

    def test_repeated_calls_are_not_replayed_or_cached_by_catalog(self):
        self.invoke()
        self.invoke()
        self.assertEqual(self.execute.call_count, 2)
        self.assertIsNot(self.execute.call_args_list[0].args[0].arguments, self.execute.call_args_list[1].args[0].arguments)

    def test_result_objects_keep_identity_including_recorded_receipts(self):
        receipt = RecordedToolResult({"status": "ok", "complete": True, "content": [{"text": "kept"}]}, 7, "a" * 64)
        pointer = deepcopy(receipt.receipt_reference())
        for result in (receipt, {"status": "error", "outcome": "failed"}, ["opaque"], None, object()):
            with self.subTest(result_type=type(result).__name__):
                self.execute.return_value = result
                self.assertIs(self.invoke(), result)
        self.assertEqual(receipt.receipt_reference(), pointer)
        self.assertEqual((receipt.receipt_seq, receipt.receipt_hash), (7, "a" * 64))

    def test_executor_control_exceptions_propagate_as_identical_objects(self):
        for error in (ExternalYield("delivered"), ExternalRunLimit("idle"), ExternalRunLimit("total"),
                      _ExecutorAbort(ExternalYield("stop")), KeyboardInterrupt(), SystemExit(9), RuntimeError("host failed")):
            with self.subTest(error_type=type(error).__name__):
                self.execute.reset_mock()
                self.execute.side_effect = error
                with self.assertRaises(type(error)) as caught:
                    self.invoke()
                self.assertIs(caught.exception, error)
                self.execute.assert_called_once()

    def test_invalid_declarations_raise_valueerror_without_execution(self):
        invalid = [None, {}, "tools", 1, [], [None], [[]], [1], [tool(), tool()]]
        for field, values in (
            ("name", (None, "", "a" * 65, "has.dot", "has space", True, [], {})),
            ("description", (None, [], {}, True, 1)),
            ("input_schema", (None, [], True, "object", {}, {"type": "array"})),
        ):
            for value in values:
                definition = tool()
                definition[field] = value
                invalid.append([definition])
        for field in ("name", "description", "input_schema"):
            definition = tool()
            del definition[field]
            invalid.append([definition])
        extra = tool()
        extra["extra"] = "unrecognized definition field"
        invalid.append([extra])
        non_json = tool()
        non_json["input_schema"]["default"] = object()
        invalid.append([non_json])
        for definitions in invalid:
            with self.subTest(definitions=definitions), self.assertRaises(ValueError):
                ExternalToolCatalog(definitions, self.execute)
        self.execute.assert_not_called()

    def test_noncallable_executors_raise_valueerror(self):
        for execute in (None, {}, [], 1, False):
            with self.subTest(execute=execute), self.assertRaises(ValueError):
                ExternalToolCatalog([tool()], execute)

    def test_valid_small_catalogs_and_name_boundary(self):
        for names in (["x"], ["_", "-", "A0_z-"], ["a" * 64, "other"]):
            with self.subTest(names=names):
                definitions = [tool(name) for name in names]
                catalog = ExternalToolCatalog(definitions, self.execute)
                self.assertEqual(catalog.dispatch(ToolCall("id", "describe_tools", {"names": names}))["definitions"], definitions)


class ExternalCatalogCallbackTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.clock = self.enterContext(patch("cater.external_agent.time.monotonic", return_value=0.0))
        self.definitions = [tool(), tool("workspace_run")]
        self.execute = Mock(return_value={"status": "ok", "complete": True})
        self.check_current = Mock(return_value=None)
        self.driver = QoderAcpDriver("worker", "never-launch-qoder", "ExactModel",
                                     timeout=2, max_run_seconds=5, tool_surface="catalog")
        self.addCleanup(self.driver.close)
        self.run = self.driver.agent_run(cwd=".", tools=self.definitions, execute=self.execute,
                                         check_current=self.check_current, on_update=None)
        self.run._budget = _ProgressBudget(2, 5)
        self.run._started_at = 0.0

    def invoke(self, name="echo", arguments=None, call_id="original-id"):
        return self.run._catalog_call(ToolCall(call_id, "invoke_tool", {
            "name": name, "arguments": {} if arguments is None else arguments,
        }))

    def test_run_creation_uses_fixed_surface_and_snapshots_original_tools(self):
        expected = deepcopy(self.definitions)
        self.assertEqual([item["name"] for item in self.run._wire_tools], ["describe_tools", "invoke_tool"])
        self.definitions[0]["input_schema"]["required"].append("changed")
        self.definitions.append(tool("late"))
        self.assertEqual(self.run._tools, expected)
        self.assertEqual(self.run._catalog_call(ToolCall("id", "describe_tools", {
            "names": ["echo", "workspace_run"],
        }))["definitions"], expected)
        self.assertEqual(self.run.state.phase, "created")
        self.assertFalse(self.run.state.prompt_attempted)
        self.assertIsNone(self.run._session)
        self.assertIsNone(self.run._bridge)

    def test_describe_checks_scope_budget_without_recording_tool_or_progress(self):
        budget = self.run._budget
        before = (budget.started, budget.progress_at, budget.activity_at, deepcopy(budget.seen))
        for when, arguments in ((0.5, {}), (1.0, {"names": ["echo"]}), (1.5, {})):
            self.clock.return_value = when
            self.assertEqual(self.run._catalog_call(ToolCall("id", "describe_tools", arguments))["status"], "ok")
        self.assertEqual(self.check_current.call_count, 3)
        self.execute.assert_not_called()
        self.assertEqual((budget.started, budget.progress_at, budget.activity_at, budget.seen), before)
        self.assertEqual(dict(self.run.state.observations), {})
        self.clock.return_value = 2.0
        with self.assertRaises(ExternalRunLimit) as caught:
            self.run._catalog_call(ToolCall("expired", "describe_tools", {}))
        self.assertEqual(caught.exception.limit, "idle")
        self.execute.assert_not_called()

    def test_describe_cannot_bypass_current_scope_failure(self):
        error = ExternalYield("scope changed")
        self.check_current.side_effect = error
        with self.assertRaises(ExternalYield) as caught:
            self.run._catalog_call(ToolCall("id", "describe_tools", {}))
        self.assertIs(caught.exception, error)
        self.execute.assert_not_called()
        self.assertTrue(self.run._tools_revoked)

    def test_direct_invoke_calls_original_tool_once_and_keeps_receipt_identity(self):
        receipt = RecordedToolResult({"status": "ok", "complete": True}, 11, "b" * 64)
        pointer = deepcopy(receipt.receipt_reference())
        self.execute.return_value = receipt
        self.clock.return_value = 1.0
        arguments = {"payload": {"items": ["known without discovery"]}}
        self.assertIs(self.invoke(arguments=arguments), receipt)
        self.execute.assert_called_once_with(ToolCall("original-id", "echo", arguments))
        self.assertEqual(receipt.receipt_reference(), pointer)
        observations = dict(self.run.state.observations)
        self.assertEqual((observations["tool_calls"], observations["progress_receipts"]), (1, 1))

    def test_failed_workspace_command_counts_as_original_tool_progress(self):
        result = {"status": "error", "complete": True, "outcome": "failed", "timed_out": False,
                  "exit_code": 1, "error": {"code": "failed"}}
        self.execute.return_value = result
        self.clock.return_value = 1.0
        arguments = {"command": "synthetic-only-never-executed"}
        self.assertIs(self.invoke("workspace_run", arguments), result)
        self.execute.assert_called_once_with(ToolCall("original-id", "workspace_run", arguments))
        self.assertEqual(self.run._budget.progress_at, 1.0)
        self.assertEqual(dict(self.run.state.observations)["progress_receipts"], 1)
        self.clock.return_value = 2.5
        self.run._budget.check()

    def test_repeated_original_invocation_executes_but_does_not_renew_progress(self):
        self.clock.return_value = 1.0
        self.invoke(arguments={"page": 1}, call_id="one")
        self.clock.return_value = 2.0
        self.invoke(arguments={"page": 1}, call_id="different-id")
        self.assertEqual(self.execute.call_count, 2)
        self.assertEqual(self.run._budget.progress_at, 1.0)
        self.assertEqual(dict(self.run.state.observations)["tool_calls"], 2)
        self.assertEqual(dict(self.run.state.observations)["progress_receipts"], 1)

    def test_catalog_invocations_never_extend_total_budget(self):
        for when, page in ((1.0, 1), (2.5, 2), (4.0, 3)):
            self.clock.return_value = when
            self.invoke(arguments={"page": page})
        self.clock.return_value = 5.0
        with self.assertRaises(ExternalRunLimit) as caught:
            self.invoke(arguments={"page": 4})
        self.assertEqual(caught.exception.limit, "total")
        self.assertEqual(self.execute.call_count, 3)
        self.assertEqual(self.run._budget.started, 0.0)

    def test_invalid_outer_call_neither_executes_nor_records_progress(self):
        for name, arguments in (("missing", {"secret": "PRIVATE_CATALOG_SECRET"}),
                                ("invoke_tool", {"name": "echo", "arguments": {}, "extra": True}),
                                ("describe_tools", {"names": ["missing"]})):
            with self.subTest(name=name):
                result = self.run._catalog_call(ToolCall("id", name, arguments))
                self.assertEqual(result["code"], "invalid_catalog_arguments")
        self.execute.assert_not_called()
        self.assertEqual(dict(self.run.state.observations), {})
        self.assertEqual(self.run._budget.seen, set())

    def test_scope_revocation_after_read_blocks_invocation(self):
        self.run._catalog_call(ToolCall("id", "describe_tools", {"names": ["echo"]}))
        error = ExternalYield("scope revoked")
        self.check_current.side_effect = error
        with self.assertRaises(ExternalYield) as caught:
            self.invoke()
        self.assertIs(caught.exception, error)
        self.execute.assert_not_called()

    def test_explicit_revocation_blocks_both_catalog_entries(self):
        self.run.revoke_tools()
        for name, arguments in (("describe_tools", {}), ("invoke_tool", {"name": "echo", "arguments": {}})):
            with self.subTest(name=name), self.assertRaises(ExternalYield):
                self.run._catalog_call(ToolCall("id", name, arguments))
        self.execute.assert_not_called()

    def test_unwrapped_executor_control_signal_is_not_replaced_by_callback(self):
        error = ExternalYield("host completed handoff")
        self.execute.side_effect = error
        with self.assertRaises(ExternalYield) as caught:
            self.invoke()
        self.assertIs(caught.exception, error)
        self.assertIs(self.run._failure, error)
        self.assertTrue(self.run._tools_revoked)
        self.execute.assert_called_once_with(ToolCall("original-id", "echo", {}))


if __name__ == "__main__":
    unittest.main()
