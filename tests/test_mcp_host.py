"""Offline MCP policy, owner-thread tools, and real stdio relay regressions."""

from __future__ import annotations

import copy
from contextlib import closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from cater import mcp
from cater import mcp_host
from cater.drivers import DriverError
from cater.events import Actor
from cater.tool_protocol import ToolCall, json_utf8
from cater.workspace_tools import WorkspaceExecutor, WorkspacePolicy


SHA = "a" * 64


def terminal(*, error=False):
    return {"status": "error" if error else "ok", "complete": True,
            "outcome": "tool_error" if error else "returned",
            "mcp_result": {"content": [{"type": "text", "text": "fixture receipt"}],
                           "structuredContent": {"saved": True}, "isError": error}}


def directory():
    return {"status": "ok", "operation": "list", "complete": True,
            "tools": [{"name": "read_note", "title": "Read note", "description": "Read one note",
                       "input_schema": {"type": "object"}, "schema_sha256": SHA}],
            "total": 1, "offset": 0, "next_offset": 1, "has_more": False}


class FakePool:
    def __init__(self):
        self.calls = []
        self.invalidated = []
        self.close_calls = 0
        self.reply = None
        self.error = None
        self.on_exchange = None

    def exchange(self, entry, packet, environment):
        self.calls.append(copy.deepcopy((entry, packet, environment)))
        if self.on_exchange is not None:
            self.on_exchange()
        if self.error is not None:
            raise self.error
        result = self.reply if self.reply is not None else (
            directory() if packet["action"] == "list" else terminal())
        return {"result": copy.deepcopy(result)}

    def invalidate(self, server_id):
        self.invalidated.append(server_id)

    def close(self):
        self.close_calls += 1


class McpHostTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.path = self.root / "mcp.json"
        self.pool = FakePool()
        self.binding = {"available": True, "workspace_id": "project"}
        self.runtime = SimpleNamespace(
            workspaces=SimpleNamespace(view=Mock(side_effect=lambda task: dict(self.binding))),
            log=SimpleNamespace(iter_events=Mock(return_value=[])),
            team=SimpleNamespace(_pending_operations=Mock(return_value=[])))
        self.configuration = {"bridge_command": [sys.executable, str(self.root / "private-bridge.py")],
                              "servers": [self.entry()]}
        self.save()
        self.service = mcp.McpService(self.runtime, self.path, pool=self.pool)
        self.process = self.enterContext(patch("subprocess.Popen", side_effect=AssertionError("external process")))
        self.network = self.enterContext(patch("socket.socket", side_effect=AssertionError("network access")))
        self.enterContext(patch.dict(os.environ, {"HOST_FIXTURE_TOKEN": "fixture-private-token",
                                                 "OPENAI_API_KEY": "fixture-model-secret"}, clear=True))

    def entry(self, **changes):
        return {"id": "notes", "title": "Fixture notes", "description": "Read and update fixture notes",
                "command": [sys.executable, str(self.root / "private-server.py")],
                "cwd": str(self.root), "roles": ["dev", "test"], "workspaces": ["project"],
                "tools": {"read_note": "read", "write_note": "write"},
                "env": {"SERVER_TOKEN": "HOST_FIXTURE_TOKEN"}, "timeout": 3, **changes}

    def save(self):
        self.path.write_text(json.dumps(self.configuration, ensure_ascii=True), encoding="utf-8")

    def index(self, **changes):
        return self.service.index(**{"role_id": "dev", "workspace_id": "project", **changes})

    def arguments(self, **changes):
        return {"server_id": "notes", "tool_name": "read_note", "schema_sha256": SHA,
                "arguments": {"text": "private-argument-value"}, **changes}

    def start(self, arguments=None):
        arguments = self.arguments() if arguments is None else arguments
        return SimpleNamespace(seq=23, payload={"call_id": "fixture-call",
            "execution_contract": self.service.contract("task", "dev", arguments)})

    def call(self, arguments=None, *, start=None, role="dev"):
        arguments = self.arguments() if arguments is None else arguments
        return self.service.execute("task", role, "call", arguments,
                                    tool_start=self.start(arguments) if start is None else start)

    def assert_rejected(self, result, code=None):
        self.assertEqual(result["status"], "error")
        self.assertIs(result["complete"], True)
        self.assertEqual(result["outcome"], "not_started")
        if code is not None:
            self.assertEqual(result["code"], code)
        self.assertEqual(self.pool.calls, [])
        self.process.assert_not_called()
        self.network.assert_not_called()

    def assert_unknown(self, result):
        self.assertEqual((result["status"], result["complete"], result["outcome"]),
                         ("error", False, "effects_unknown"))
        self.assertEqual(len(self.pool.calls), 1)

    def assert_private_absent(self, value):
        encoded = json.dumps(value, ensure_ascii=True)
        for private in (str(self.root), "private-bridge.py", "private-server.py", "HOST_FIXTURE_TOKEN",
                        "SERVER_TOKEN", "fixture-private-token", "fixture-model-secret",
                        "private-argument-value", "private transport diagnostic"):
            self.assertNotIn(json.dumps(private)[1:-1], encoded)

    def test_no_configuration_is_lazy_and_exposes_no_servers(self):
        with patch.object(mcp, "CliDriver", side_effect=AssertionError("bridge constructed")) as driver:
            service = mcp.McpService(self.runtime)
            self.addCleanup(service.close)
            self.assertFalse(service.configured)
            index = service.index(role_id="dev", workspace_id=None)
            self.assertEqual((index["servers"], index["total"], index["configuration_status"]),
                             ([], 0, "not_configured"))
            self.assert_rejected(service.execute("task", "dev", "list", {"server_id": "notes"}),
                                 "mcp_unavailable")
            driver.assert_not_called()

    def test_configured_constructor_and_index_do_not_exchange_or_probe_environment(self):
        with patch.object(mcp, "CliDriver", side_effect=AssertionError("bridge constructed")), \
                patch.dict(os.environ, {}, clear=True):
            service = mcp.McpService(self.runtime, self.path, pool=self.pool)
            self.assertTrue(service.configured)
            index = service.index(role_id="dev", workspace_id="project")
        self.assertEqual(index["configuration_status"], "ok")
        self.assertEqual(index["total"], 1)
        self.assertEqual(set(index["servers"][0]), {"id", "title", "description", "tool_count"})
        self.assertEqual(index["servers"][0]["tool_count"], 2)
        self.assertEqual(self.pool.calls, [])
        self.assert_private_absent(index)

    def test_strict_json_rejects_duplicate_keys_constants_encoding_and_oversize(self):
        valid = json.dumps(self.configuration)
        cases = [b"not json", b"\xff", b"[]", b"null", b"{}", b'{"servers": [], "servers": []}',
                 valid.replace('"timeout": 3', '"timeout": NaN').encode(),
                 valid.replace('"timeout": 3', '"timeout": Infinity').encode(),
                 valid.replace('"read_note": "read"', '"read_note": "read", "read_note": "write"').encode(),
                 b" " * (1024 * 1024 + 1), b"[" * 2000 + b"]" * 2000]
        for body in cases:
            with self.subTest(prefix=body[:60], size=len(body)):
                self.path.write_bytes(body)
                index = self.index()
                self.assertEqual(index["configuration_status"], "unavailable")
                self.assertEqual(index["servers"], [])
                self.assertIsNone(index["configuration_sha256"])
                self.assert_rejected(self.service.execute("task", "dev", "list", {"server_id": "notes"}),
                                     "mcp_unavailable")
                self.assert_private_absent(index)

    def test_missing_config_is_unavailable_without_exception_or_process(self):
        self.path.unlink()
        self.assertTrue(self.service.configured)
        self.assertEqual(self.index()["configuration_status"], "unavailable")
        self.assertEqual(self.pool.calls, [])

    def test_top_level_and_server_schema_are_strict(self):
        valid = copy.deepcopy(self.configuration)
        candidates = [{**valid, "unknown": True}, {"servers": []}, {**valid, "servers": {}},
                      {**valid, "servers": [None]}, {**valid, "servers": valid["servers"] * 2},
                      {**valid, "servers": [self.entry(id=f"server-{i}") for i in range(17)]}]
        for key in ("id", "title", "description", "command", "cwd", "tools", "roles"):
            missing = self.entry()
            del missing[key]
            candidates.append({**valid, "servers": [missing]})
        candidates.append({**valid, "servers": [self.entry(annotations={"readOnlyHint": True})]})
        for candidate in candidates:
            with self.subTest(candidate=candidate):
                self.configuration = candidate
                self.save()
                self.assertEqual(self.index()["configuration_status"], "unavailable")
                self.assertEqual(self.pool.calls, [])

    def test_server_fields_validate_scopes_effects_environment_and_timeout(self):
        cases = {
            "id": ["", "bad/id", "a" * 65, 3],
            "title": ["", " ", "bad\nlabel", "\ud800", "x" * 161],
            "description": ["", "x" * 401],
            "roles": [[], ["dev", "dev"], ["bad role"], "dev", [None]],
            "workspaces": [[], ["project", "project"], ["bad/workspace"], None],
            "tools": [{}, {"read_note": "execute"}, {"read_note": True}, {"bad tool": "read"},
                      {"read_note": []}, {f"tool{i}": "read" for i in range(65)}],
            "env": [[], {"BAD-NAME": "SOURCE"}, {"TARGET": "bad source"}, {"TARGET": 1},
                    {f"TARGET{i}": "SOURCE" for i in range(65)}],
            "timeout": [False, 0, -1, 600.1, "30", None],
            "cwd": ["relative", str(self.root / "missing"), sys.executable, None],
        }
        for key, values in cases.items():
            for value in values:
                with self.subTest(field=key, value=value):
                    self.configuration["servers"] = [self.entry(**{key: value})]
                    self.save()
                    self.assertEqual(self.index()["configuration_status"], "unavailable")
                    self.assertEqual(self.pool.calls, [])

    def test_both_commands_require_bounded_absolute_argv_without_shell(self):
        bad = [[], "python", ["python"], [sys.executable, None], [sys.executable, "bad\0arg"],
               [sys.executable, "\ud800"], [sys.executable, "x" * 8193],
               [sys.executable] + ["x"] * 64, [sys.executable] + ["x" * 8192] * 4]
        for target in ("bridge_command", "command"):
            for command in bad:
                with self.subTest(target=target, command=repr(command)[:100]):
                    self.configuration = {"bridge_command": [sys.executable], "servers": [self.entry()]}
                    if target == "bridge_command":
                        self.configuration[target] = command
                    else:
                        self.configuration["servers"][0][target] = command
                    self.save()
                    self.assertEqual(self.index()["configuration_status"], "unavailable")

    def test_role_workspace_filtering_happens_before_pagination(self):
        self.configuration["servers"] = [self.entry(id="hidden", roles=["other"])] + [
            self.entry(id=f"server-{i}") for i in range(12)]
        self.save()
        first = self.index()
        second = self.index(offset=first["next_offset"])
        third = self.index(offset=second["next_offset"])
        self.assertEqual([len(page["servers"]) for page in (first, second, third)], [5, 5, 2])
        self.assertEqual([row["id"] for page in (first, second, third) for row in page["servers"]],
                         [f"server-{i}" for i in range(12)])
        self.assertEqual([page["total"] for page in (first, second, third)], [12, 12, 12])
        self.assertEqual([page["has_more"] for page in (first, second, third)], [True, True, False])
        self.assertEqual(self.index(offset=12)["servers"], [])
        for changes in ({"role_id": "unlisted"}, {"workspace_id": "other"}, {"workspace_id": None}):
            self.assertEqual(self.index(**changes)["total"], 0)
        self.assertEqual(self.pool.calls, [])

    def test_index_rejects_boolean_negative_and_out_of_range_pagination(self):
        for changes in ({"offset": True}, {"offset": -1}, {"offset": 2}, {"offset": 0.5},
                        {"limit": False}, {"limit": 0}, {"limit": 21}, {"limit": "5"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.index(**changes)
        self.assertEqual(self.pool.calls, [])

    def test_server_listing_via_execute_is_pure_and_accepts_only_pagination(self):
        result = self.service.execute("task", "dev", "list", {"offset": 0, "limit": 1})
        self.assertEqual((result["status"], result["operation"], result["total"]), ("ok", "servers", 1))
        self.assert_private_absent(result)
        self.assertEqual(self.pool.calls, [])
        for args in ({"offset": True}, {"limit": 21}, {"extra": "private-argument-value"}):
            self.assert_rejected(self.service.execute("task", "dev", "list", args), "invalid_mcp_arguments")

    def test_unknown_server_role_and_workspace_are_rejected_without_exchange(self):
        for server, role, binding in (("missing", "dev", self.binding), ("notes", "other", self.binding),
                                       ("notes", "dev", {"available": True, "workspace_id": "other"}),
                                       ("notes", "dev", {"available": False, "workspace_id": "project"})):
            with self.subTest(server=server, role=role, binding=binding):
                self.binding = dict(binding)
                self.assert_rejected(self.service.execute("task", role, "list", {"server_id": server}),
                                     "mcp_unavailable")
                self.assert_rejected(self.call(self.arguments(server_id=server), role=role), "mcp_unavailable")

    def test_no_workspace_constraint_allows_unbound_scope(self):
        del self.configuration["servers"][0]["workspaces"]
        self.save()
        self.binding = {"available": False, "workspace_id": "project"}
        self.assertEqual(self.service.scope("task", "dev"), {"role_id": "dev", "workspace_id": None})
        self.assertEqual(self.index(workspace_id=None)["total"], 1)
        self.assertEqual(self.call()["outcome"], "returned")

    def test_missing_empty_or_invalid_environment_is_rejected_before_exchange(self):
        for value, code in ((None, "mcp_credentials_missing"), ("", "mcp_credentials_missing"),
                            ("\ud800", "mcp_environment_invalid"), ("bad\0value", "mcp_environment_invalid")):
            with self.subTest(value=repr(value)):
                environment = {} if value is None else {"HOST_FIXTURE_TOKEN": value}
                with patch.object(mcp.os, "environ", environment):
                    for action in ("list", "call"):
                        result = (self.service.execute("task", "dev", "list", {"server_id": "notes"})
                                  if action == "list" else self.call())
                        self.assert_rejected(result, code)
                        self.assert_private_absent(result)

    def test_missing_bridge_or_server_program_is_rejected_before_exchange(self):
        for target in ("bridge_command", "command"):
            with self.subTest(target=target):
                self.configuration = {"bridge_command": [sys.executable], "servers": [self.entry()]}
                destination = self.configuration if target == "bridge_command" else self.configuration["servers"][0]
                destination[target] = [str(self.root / "missing-program.exe")]
                self.save()
                self.assert_rejected(self.call(), "mcp_program_missing")

    def test_invalid_actions_arguments_and_tool_versions_never_exchange(self):
        cases = [("unknown", {}), ("list", None), ("call", []), ("list", {"server_id": True}),
                 ("list", {"server_id": "bad/id"}), ("call", {"server_id": "notes"}),
                 ("call", self.arguments(extra=True)), ("call", self.arguments(tool_name="unlisted")),
                 ("call", self.arguments(schema_sha256="A" * 64)),
                 ("call", self.arguments(schema_sha256="short")),
                 ("call", self.arguments(arguments=[]))]
        for args in ({"offset": True}, {"offset": -1}, {"limit": 0}, {"limit": 21}):
            cases.append(("list", {"server_id": "notes", **args}))
        for action, args in cases:
            with self.subTest(action=action, args=args):
                self.assert_rejected(self.service.execute("task", "dev", action, args), "invalid_mcp_arguments")

    def test_non_json_nonfinite_recursive_and_invalid_utf8_call_arguments_are_rejected(self):
        recursive = {}
        recursive["self"] = recursive
        for value in ({"text": "\ud800"}, {"text": "\udfff"}, {"\ud800": "value"}, {"blob": b"data"},
                      {"set": {1}}, {"number": float("nan")}, {"number": float("inf")}, recursive):
            with self.subTest(value_type=type(next(iter(value.values()))).__name__):
                result = self.call(self.arguments(arguments=value))
                self.assert_rejected(result, "invalid_mcp_arguments")

    def test_oversize_request_is_rejected_before_exchange(self):
        for text in ("x" * mcp.PACKET_BYTES, "\u6c49" * (mcp.PACKET_BYTES // 3)):
            with self.subTest(characters=len(text)):
                self.assert_rejected(self.call(self.arguments(arguments={"text": text})), "mcp_request_too_large")

    def test_contract_is_host_scoped_and_bound_to_configuration(self):
        args = self.arguments(tool_name="write_note")
        contract = self.service.contract("task", "dev", args)
        self.assertEqual(contract, {"kind": "mcp_stdio_tool", "version": 1, "server_id": "notes",
                                   "configuration_sha256": hashlib.sha256(self.path.read_bytes()).hexdigest(),
                                   "effect": "write"})
        self.assertIsNone(self.service.contract("task", "other", args))
        self.assertIsNone(self.service.contract("task", "dev", self.arguments(tool_name="missing")))
        self.assertIsNone(self.service.contract("task", "dev", args, expected_configuration=SHA))
        self.assertEqual(self.service.contract("task", "dev", args,
                         expected_configuration=contract["configuration_sha256"]), contract)
        self.assert_private_absent(contract)
        self.assertEqual(self.pool.calls, [])

    def test_call_requires_current_host_start_contract(self):
        args = self.arguments()
        self.assert_rejected(self.service.execute("task", "dev", "call", args), "mcp_configuration_changed")
        start = self.start(args)
        start.payload["execution_contract"]["effect"] = "write"
        self.assert_rejected(self.call(args, start=start), "mcp_configuration_changed")

    def test_configuration_changes_revoke_old_contracts_without_cached_permission(self):
        args = self.arguments()
        start = self.start(args)
        self.configuration["servers"][0]["description"] = "Updated configuration"
        self.save()
        self.assert_rejected(self.call(args, start=start), "mcp_configuration_changed")
        self.path.write_text("invalid", encoding="utf-8")
        self.assertEqual(self.index()["servers"], [])
        self.assert_rejected(self.call(args, start=start), "mcp_unavailable")
        self.save()
        self.assertEqual(self.index()["total"], 1)

    def test_scope_and_configuration_are_rechecked_after_lock_acquisition(self):
        @contextmanager
        def changed_config(*args):
            self.configuration["servers"][0]["roles"] = ["other"]
            self.save()
            yield True

        @contextmanager
        def changed_scope(*args):
            self.binding["workspace_id"] = "other"
            yield True

        for lock in (changed_config, changed_scope):
            with self.subTest(lock=lock.__name__):
                self.configuration["servers"] = [self.entry()]
                self.binding = {"available": True, "workspace_id": "project"}
                self.save()
                with patch.object(mcp, "activity_owner", lock):
                    self.assert_rejected(self.call(), "mcp_configuration_changed")

    def test_busy_service_is_rejected_without_replay(self):
        @contextmanager
        def busy(*args):
            yield False

        with patch.object(mcp, "activity_owner", busy):
            self.assert_rejected(self.call(), "mcp_busy")

    def test_request_contains_only_explicit_environment_and_binds_invocation(self):
        result = self.call()
        entry, packet, environment = self.pool.calls[0]
        base_keys = {"PATH", "TEMP", "TMP", "SystemRoot", "WINDIR"}
        self.assertEqual({key: value for key, value in environment.items() if key not in base_keys},
                         {"PYTHONIOENCODING": "utf-8", "SERVER_TOKEN": "fixture-private-token"})
        self.assertEqual(environment["PATH"], os.defpath)
        self.assertNotIn("HOST_FIXTURE_TOKEN", environment)
        self.assertNotIn("OPENAI_API_KEY", environment)
        self.assertEqual(packet["server"]["env"], environment)
        self.assertEqual(packet["server"]["command"], entry["command"])
        self.assertEqual(packet["server"]["cwd"], str(self.root))
        self.assertEqual(packet["allowed_tools"], ["read_note", "write_note"])
        self.assertEqual(packet["timeout_ms"], 3000)
        self.assertEqual(packet["invocation_id"], "23:fixture-call")
        self.assertEqual(packet["arguments"], {"text": "private-argument-value"})
        self.assertNotIn("server_id", packet)
        self.assertNotIn("fixture-model-secret", json.dumps(packet))
        self.assertEqual(result["invocation_id"], "23:fixture-call")
        self.assertEqual(result["effect"], "read")
        self.assertTrue(result["configuration_current"])
        self.assert_private_absent(result)

    def test_tool_directory_adds_host_effect_and_discards_server_permission_hints(self):
        response = directory()
        response["tools"][0].update(effect="write", annotations={"destructiveHint": True},
                                     command="private-server.py", env="fixture-private-token")
        self.pool.reply = response
        result = self.service.execute("task", "dev", "list", {"server_id": "notes", "offset": 0, "limit": 1})
        self.assertEqual(result["tools"][0]["effect"], "read")
        self.assertEqual(set(result["tools"][0]),
                         {"name", "title", "description", "input_schema", "schema_sha256", "effect"})
        self.assert_private_absent(result)
        self.assertEqual(self.pool.calls[0][1]["limit"], 1)

    def test_invalid_tool_directories_fail_without_call_or_retry(self):
        bad_results = []
        for changes in ({"complete": False}, {"offset": True}, {"total": -1}, {"has_more": True},
                        {"next_offset": 2}, {"tools": directory()["tools"] * 2},
                        {"tools": directory()["tools"] * 21}):
            bad_results.append({**directory(), **changes})
        for changes in ({"name": "not_allowed"}, {"input_schema": []}, {"schema_sha256": "invalid"}):
            bad_results.append({**directory(), "tools": [{**directory()["tools"][0], **changes}]})
        for response in bad_results:
            with self.subTest(response=response):
                self.pool.calls.clear()
                self.pool.invalidated.clear()
                self.pool.reply = response
                result = self.service.execute("task", "dev", "list", {"server_id": "notes"})
                self.assertEqual((result["status"], result["complete"], result["outcome"]),
                                 ("error", True, "discovery_failed"))
                self.assertEqual(len(self.pool.calls), 1)
                self.assertEqual(self.pool.calls[0][1]["action"], "list")
                self.assertEqual(self.pool.invalidated, ["notes"])

    def test_terminal_success_and_tool_error_preserve_receipts_without_unknown_or_retry(self):
        for error in (False, True):
            with self.subTest(error=error):
                self.pool.calls.clear()
                self.pool.reply = terminal(error=error)
                result = self.call(self.arguments(tool_name="write_note"))
                self.assertEqual((result["status"], result["complete"], result["outcome"]),
                                 ("error" if error else "ok", True, "tool_error" if error else "returned"))
                self.assertEqual(result["mcp_result"], terminal(error=error)["mcp_result"])
                self.assertEqual(result["effect"], "write")
                self.assertEqual(len(self.pool.calls), 1)
                self.assertEqual(self.pool.invalidated, [])

    def test_explicit_pre_call_rejection_does_not_become_unknown(self):
        self.pool.reply = {"status": "error", "complete": True, "outcome": "not_started",
                           "code": "schema_changed", "message": "Re-read the directory"}
        result = self.call()
        self.assertEqual((result["complete"], result["outcome"], result["code"]),
                         (True, "not_started", "schema_changed"))
        self.assertEqual(len(self.pool.calls), 1)
        self.assertEqual(self.pool.invalidated, [])

    def test_transport_errors_are_redacted_unknown_calls_and_never_replayed(self):
        for error in (DriverError("private transport diagnostic"), OSError("fixture-private-token"),
                      ValueError("private-argument-value")):
            with self.subTest(error=type(error).__name__):
                self.pool.calls.clear()
                self.pool.invalidated.clear()
                self.pool.error = error
                result = self.call(self.arguments(tool_name="write_note"))
                self.assert_unknown(result)
                self.assertEqual(self.pool.invalidated, ["notes"])
                self.assert_private_absent(result)

    def test_discovery_transport_failure_is_not_a_write_unknown(self):
        self.pool.error = DriverError("private transport diagnostic")
        result = self.service.execute("task", "dev", "list", {"server_id": "notes"})
        self.assertEqual((result["complete"], result["outcome"]), (True, "discovery_failed"))
        self.assertEqual(len(self.pool.calls), 1)
        self.assert_private_absent(result)

    def test_explicit_unknown_receipt_preserves_unknown_outcome(self):
        self.pool.reply = {"status": "error", "complete": False, "outcome": "effects_unknown",
                           "code": "connection_lost", "message": "Reconcile the invocation"}
        self.assert_unknown(self.call())

    def test_invalid_inconsistent_and_oversize_call_receipts_become_unknown(self):
        cases = [[], "bad", 4, {"status": "ok", "complete": True},
                 {**terminal(), "complete": 1}, {**terminal(), "status": "error"},
                 {**terminal(), "complete": False}, {**terminal(), "outcome": "tool_error"},
                 {**terminal(), "server_id": "spoofed"},
                 {**terminal(), "message": "x" * mcp.RESULT_BYTES},
                 {**terminal(), "mcp_result": {"content": [], "isError": True}},
                 {**terminal(), "mcp_result": {"content": [], "isError": 1}},
                 {**terminal(), "mcp_result": {"content": "bad"}},
                 {**terminal(), "mcp_result": {"content": [], "number": float("nan")}}]
        for response in cases:
            with self.subTest(response_type=type(response).__name__):
                self.pool.calls.clear()
                self.pool.invalidated.clear()
                self.pool.reply = response
                self.assert_unknown(self.call())
                self.assertEqual(self.pool.invalidated, ["notes"])

    def test_representation_incomplete_still_has_known_terminal_outcome(self):
        for error in (False, True):
            with self.subTest(error=error):
                self.pool.calls.clear()
                response = terminal(error=error)
                response.update(representation_complete=False, unsupported_content_types=["image"],
                                result_bytes=300000, result_sha256=SHA, result_excerpt="Bounded excerpt")
                self.pool.reply = response
                result = self.call()
                self.assertTrue(result["complete"])
                self.assertFalse(result["representation_complete"])
                self.assertEqual(result["outcome"], "tool_error" if error else "returned")
                self.assertEqual(result["result_sha256"], SHA)
                self.assertEqual(result["unsupported_content_types"], ["image"])
                self.assertEqual(result["mcp_result"]["isError"], error)
                self.assertEqual(self.pool.invalidated, [])

    def test_partial_marker_does_not_excuse_missing_or_inconsistent_terminal_receipt(self):
        for changes in ({"mcp_result": None}, {"mcp_result": {"content": [], "isError": True}},
                        {"representation_complete": "false"}):
            with self.subTest(changes=changes):
                self.pool.calls.clear()
                self.pool.reply = {**terminal(), "representation_complete": False, **changes}
                self.assert_unknown(self.call())

    def test_surrogate_result_uses_explicit_escaped_representation(self):
        self.pool.reply = terminal()
        self.pool.reply["mcp_result"]["content"][0]["text"] = "\ud800"
        result = self.call()
        json.dumps(result, ensure_ascii=False).encode("utf-8")
        self.assertTrue(result["complete"])
        self.assertEqual(result["outcome"], "returned")
        self.assertFalse(result["representation_complete"])
        self.assertEqual(result["result_encoding"], "escaped_json")
        self.assertEqual(json.loads(result["result_json"])["mcp_result"]["content"][0]["text"], "\ud800")
        self.assertEqual(len(self.pool.calls), 1)

    def test_late_configuration_change_preserves_terminal_history_but_not_current_authorization(self):
        def change():
            self.configuration["servers"][0]["roles"] = ["other"]
            self.save()

        self.pool.on_exchange = change
        result = self.call()
        self.assertEqual(result["outcome"], "returned")
        self.assertTrue(result["complete"])
        self.assertFalse(result["configuration_current"])
        self.assertEqual(self.index()["servers"], [])
        self.pool.calls.clear()
        self.assert_rejected(self.call(), "mcp_unavailable")

    def test_public_arguments_hash_body_without_copying_private_values(self):
        args = self.arguments(arguments={"text": "private-argument-value", "nested": {"secret": "fixture-private-token"}})
        public = self.service.public_arguments(args)
        body = json_utf8(args["arguments"], allow_nan=False)
        self.assertEqual(public, {"server_id": "notes", "tool_name": "read_note", "schema_sha256": SHA,
                                  "arguments_sha256": hashlib.sha256(body).hexdigest(),
                                  "arguments_bytes": len(body), "argument_fields": ["text", "nested"]})
        self.assert_private_absent(public)
        invalid = self.service.public_arguments(self.arguments(arguments={"number": float("nan")}))
        self.assertIs(invalid["arguments_valid_json"], False)
        many = self.service.public_arguments(self.arguments(arguments={f"field{i}": i for i in range(100)}))
        self.assertEqual(len(many["argument_fields"]), 64)

    def test_service_close_owns_new_pool_but_never_closes_borrowed_pool(self):
        owned = FakePool()
        with patch.object(mcp, "McpPool", return_value=owned) as constructor:
            service = mcp.McpService(self.runtime, self.path)
            constructor.assert_called_once_with()
            self.assertIs(service.pool, owned)
            service.close()
        self.assertEqual(owned.close_calls, 1)
        self.service.close()
        self.service.close()
        self.assertEqual(self.pool.close_calls, 0)
        self.assertEqual(owned.calls, [])

    def test_real_pool_is_lazy_reuses_identity_and_replaces_changed_environment(self):
        pool = mcp.McpPool()
        self.addCleanup(pool.close)
        entry = {**self.entry(), "bridge_command": [sys.executable], "configuration_sha256": SHA}
        client1, client2, client3 = Mock(), Mock(), Mock()
        with patch.object(mcp, "CliDriver", side_effect=[client1, client2, client3]) as driver:
            driver.assert_not_called()
            for number in range(2):
                self.assertIs(pool.exchange(entry, {"request": number}, {"TOKEN": "one"}),
                              client1.exchange.return_value)
            self.assertEqual(driver.call_count, 1)
            client1.close.assert_not_called()
            pool.exchange(entry, {}, {"TOKEN": "two"})
            client1.close.assert_called_once_with()
            self.assertEqual(driver.call_count, 2)
            pool.exchange({**entry, "configuration_sha256": "b" * 64}, {}, {"TOKEN": "two"})
            client2.close.assert_called_once_with()
            self.assertEqual(driver.call_count, 3)
            self.assertEqual(driver.call_args.kwargs["restart_limit"], 0)
            self.assertNotIn("fixture-model-secret", json.dumps(driver.call_args.kwargs["env"]))
            pool.close()
            client3.close.assert_called_once_with()
            with self.assertRaises(DriverError):
                pool.exchange(entry, {}, {})
            self.assertEqual(driver.call_count, 3)

    def test_real_pool_invalidates_once_and_closes_other_clients_after_cleanup_failure(self):
        pool = mcp.McpPool()
        self.addCleanup(pool.close)
        entry = {**self.entry(), "bridge_command": [sys.executable], "configuration_sha256": SHA}
        first, second = Mock(), Mock()
        with patch.object(mcp, "CliDriver", side_effect=[first, second]):
            pool.exchange(entry, {}, {})
            pool.exchange({**entry, "id": "other"}, {}, {})
            first.close.side_effect = OSError("fixture-private-token")
            with self.assertRaisesRegex(DriverError, "^MCP process cleanup failed$"):
                pool.close()
            first.close.assert_called_once_with()
            second.close.assert_called_once_with()
            pool.invalidate("notes")
            pool.invalidate("missing")
            first.close.assert_called_once_with()

    def test_config_file_size_accepts_limit_but_rejects_one_byte_more(self):
        body = json.dumps(self.configuration).encode("utf-8")
        body += b" " * (1024 * 1024 - len(body))
        self.path.write_bytes(body)
        self.assertEqual(self.index()["configuration_status"], "ok")
        self.path.write_bytes(body + b" ")
        self.assertEqual(self.index()["configuration_status"], "unavailable")
        self.assertEqual(self.pool.calls, [])

    def test_maximum_servers_tools_and_timeout_are_accepted_without_discovery(self):
        tools = {f"tool{i}": "read" for i in range(64)}
        self.configuration["servers"] = [self.entry(id=f"server-{i}", tools=tools, timeout=600)
                                         for i in range(16)]
        self.save()
        index = self.index(limit=20)
        self.assertEqual(index["total"], 16)
        self.assertFalse(index["has_more"])
        self.assertEqual([row["tool_count"] for row in index["servers"]], [64] * 16)
        self.assertEqual(self.pool.calls, [])

    def test_request_byte_limit_accepts_exact_boundary_and_rejects_one_more(self):
        args = self.arguments(arguments={"text": ""})
        self.call(args)
        packet = self.pool.calls[0][1]
        remaining = mcp.PACKET_BYTES - 128 - len(json_utf8(packet, allow_nan=False))
        self.pool.calls.clear()
        args["arguments"]["text"] = "x" * remaining
        self.assertEqual(self.call(args)["outcome"], "returned")
        self.assertEqual(len(json_utf8(self.pool.calls[0][1], allow_nan=False)) + 128, mcp.PACKET_BYTES)
        self.pool.calls.clear()
        args["arguments"]["text"] += "x"
        self.assert_rejected(self.call(args), "mcp_request_too_large")

    def test_result_byte_limit_preserves_terminal_at_boundary_and_marks_oversize_unknown(self):
        response = terminal()
        remaining = mcp.RESULT_BYTES - len(json_utf8(response, allow_nan=False))
        response["mcp_result"]["content"][0]["text"] += "x" * remaining
        self.pool.reply = response
        self.assertEqual(self.call()["outcome"], "returned")
        self.pool.calls.clear()
        response["mcp_result"]["content"][0]["text"] += "x"
        self.assert_unknown(self.call())

    def test_invalidation_failure_still_returns_unknown_without_retry(self):
        self.pool.error = DriverError("private transport diagnostic")
        with patch.object(self.pool, "invalidate", side_effect=OSError("fixture-private-token")) as invalidate:
            result = self.call()
        self.assert_unknown(result)
        invalidate.assert_called_once_with("notes")
        self.assert_private_absent(result)

    def test_envelope_without_result_is_unknown_and_invalidated_once(self):
        with patch.object(self.pool, "exchange", return_value={}) as exchange:
            result = self.call()
        self.assertEqual((result["complete"], result["outcome"]), (False, "effects_unknown"))
        exchange.assert_called_once()
        self.assertEqual(self.pool.invalidated, ["notes"])

    def test_write_checks_pending_operations_but_read_can_reconcile(self):
        with patch.object(self.service, "pending", return_value=[{"started_seq": 5}]) as pending:
            self.assert_rejected(self.call(self.arguments(tool_name="write_note")), "mcp_operation_pending")
            pending.assert_called_once_with("notes", exclude=23)
            self.assertEqual(self.call()["outcome"], "returned")
            self.assertEqual(pending.call_count, 1)

    def test_read_only_classification_requires_host_contract_and_matching_server(self):
        contract = self.service.contract("task", "dev", self.arguments())
        row = {"name": mcp.CALL, "arguments": {"server_id": "notes"}, "execution_contract": contract}
        self.assertTrue(mcp.read_only_call(row))
        for change in ({"version": True}, {"version": 2}, {"effect": "write"}, {"effect": "readOnlyHint"},
                       {"kind": "other"}, {"server_id": "other"}, {"configuration_sha256": "short"}):
            with self.subTest(change=change):
                self.assertFalse(mcp.read_only_call({**row, "execution_contract": {**contract, **change}}))
        self.assertFalse(mcp.read_only_call({**row, "name": mcp.LIST}))
        self.assertFalse(mcp.read_only_call({**row, "execution_contract": None}))
        self.assertFalse(mcp.read_only_call({"name": mcp.CALL, "arguments": {"server_id": "notes", "effect": "read"}}))

    def test_pending_filters_other_servers_exclusions_and_host_attested_reads(self):
        starts = [SimpleNamespace(actor=Actor.system("team"), task_id="task", payload={
            "name": mcp.CALL, "arguments": {"server_id": "notes"}})]
        self.runtime.log.iter_events.return_value = starts
        read = {"name": mcp.CALL, "started_seq": 1, "arguments": {"server_id": "notes"},
                "execution_contract": self.service.contract("task", "dev", self.arguments())}
        write = {**read, "started_seq": 2, "execution_contract": {**read["execution_contract"], "effect": "write"}}
        unattested = {"name": mcp.CALL, "started_seq": 3, "arguments": {"server_id": "notes"}}
        other = {**write, "started_seq": 4, "arguments": {"server_id": "other"}}
        self.runtime.team._pending_operations.return_value = [read, write, unattested, other]
        self.assertEqual(self.service.pending("notes", exclude=2), [{**unattested, "task_id": "task"}])
        self.runtime.log.iter_events.return_value = [SimpleNamespace(
            actor=Actor.human(), task_id="task", payload=starts[0].payload)]
        self.assertEqual(self.service.pending("notes"), [])

    def test_pool_exchange_failure_is_single_attempt_and_invalidation_allows_fresh_client(self):
        pool = mcp.McpPool()
        self.addCleanup(pool.close)
        entry = {**self.entry(), "bridge_command": [sys.executable], "configuration_sha256": SHA}
        broken, fresh = Mock(), Mock()
        broken.exchange.side_effect = DriverError("fixture failure")
        with patch.object(mcp, "CliDriver", side_effect=[broken, fresh]) as constructor:
            with self.assertRaises(DriverError):
                pool.exchange(entry, {}, {})
            broken.exchange.assert_called_once_with({})
            self.assertEqual(constructor.call_count, 1)
            pool.invalidate("notes")
            broken.close.assert_called_once_with()
            self.assertIs(pool.exchange(entry, {}, {}), fresh.exchange.return_value)
            self.assertEqual(constructor.call_count, 2)

    @unittest.skipUnless(os.name == "nt", "Windows executable and environment rules")
    def test_windows_script_or_fake_executable_is_rejected_without_dispatch(self):
        for name, content in (("server.cmd", b"@exit 0"), ("server.exe", b"not an executable")):
            with self.subTest(name=name):
                program = self.root / name
                program.write_bytes(content)
                self.configuration["servers"][0]["command"] = [str(program)]
                self.save()
                self.assert_rejected(self.call(), "mcp_program_missing")

    @unittest.skipUnless(os.name == "nt", "Windows environment is case-insensitive")
    def test_windows_explicit_systemroot_overrides_base_without_duplicate_names(self):
        self.configuration["servers"][0]["env"] = {"SystemRoot": "HOST_FIXTURE_TOKEN"}
        self.save()
        self.assertEqual(self.call()["outcome"], "returned")
        environment = self.pool.calls[0][1]["server"]["env"]
        self.assertEqual(environment["SYSTEMROOT"], "fixture-private-token")
        self.assertEqual(len(environment), len({name.upper() for name in environment}))
        self.configuration["servers"][0]["env"]["SYSTEMROOT"] = "HOST_FIXTURE_TOKEN"
        self.save()
        self.assertEqual(self.index()["configuration_status"], "unavailable")


class McpToolBridgeTests(unittest.TestCase):
    """Loopback sockets, isolated Python relays, and temporary workspace effects."""

    def setUp(self):
        self.calls = []
        self.owner = threading.get_ident()
        self.tools = [{
            "name": "save_note", "description": "Save a fixture note",
            "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}},
        }]

    def execute(self, call):
        self.assertEqual(threading.get_ident(), self.owner)
        self.calls.append(call)
        return {"status": "ok", "receipt": call.arguments}

    def bridge(self, **kwargs):
        bridge = mcp_host.McpToolBridge(self.tools, kwargs.pop("execute", self.execute), **kwargs)
        self.addCleanup(bridge.close)
        return bridge

    @staticmethod
    def environment(bridge):
        return {row["name"]: row["value"] for row in bridge.server_config()["env"]}

    def peer(self, bridge):
        env = self.environment(bridge)
        connection = socket.create_connection(("127.0.0.1", int(env["CATER_MCP_PORT"])), timeout=5)
        self.addCleanup(connection.close)
        incoming = connection.makefile("rb")
        self.addCleanup(incoming.close)
        return connection, incoming

    def run_io(self, bridge, operation, *, disconnect=False):
        """Keep all pump calls on the test thread while bounded I/O runs elsewhere."""
        results, failures, pump_errors = [], [], []

        def run():
            try:
                results.append(operation())
            except BaseException as exc:
                failures.append(exc)

        thread = threading.Thread(target=run, daemon=True, name="mcp-test-io")
        thread.start()
        deadline = time.monotonic() + 10
        try:
            while thread.is_alive() and time.monotonic() < deadline:
                if pump_errors:
                    thread.join(0.01)
                    continue
                try:
                    bridge.pump(0.01)
                except mcp_host.ToolBridgeError as exc:
                    pump_errors.append(exc)
            self.assertFalse(thread.is_alive(), "MCP fixture I/O timed out")
            if failures:
                raise failures[0]
            if not disconnect and pump_errors:
                raise pump_errors[0]
            return results[0], pump_errors
        finally:
            if thread.is_alive():
                bridge.close()
            thread.join(6)
            self.assertFalse(thread.is_alive(), "MCP fixture I/O thread leaked")

    def exchange(self, bridge, peer, value, *, rejected=False):
        connection, incoming = peer
        body = value if isinstance(value, bytes) else json_utf8(value, allow_nan=False) + b"\n"

        def transfer():
            connection.sendall(body)
            return incoming.readline(mcp_host.MAX_BYTES + 2)

        reply, errors = self.run_io(bridge, transfer, disconnect=rejected)
        if rejected:
            self.assertEqual(reply, b"")
            self.assertEqual(len(errors), 1)
            return None
        self.assertTrue(reply.endswith(b"\n"), "Missing host frame")
        return json.loads(reply.decode("utf-8"))

    @staticmethod
    def request(identifier, method, **params):
        return {"jsonrpc": "2.0", "id": identifier, "method": method, "params": params}

    @staticmethod
    def notification(method, **params):
        return {"jsonrpc": "2.0", "method": method, "params": params}

    def session(self, bridge, *, active=True):
        peer = self.peer(bridge)
        self.assertEqual(self.exchange(bridge, peer, {
            "capability": self.environment(bridge)["CATER_MCP_CAPABILITY"],
        }), {"authenticated": True})
        initialized = self.exchange(bridge, peer, self.request(
            "init", "initialize", protocolVersion="2025-06-18",
            capabilities={}, clientInfo={"name": "offline-fixture", "version": "1"},
        ))["response"]
        self.assertEqual(initialized["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(initialized["result"]["capabilities"], {"tools": {}})
        self.assertEqual(self.exchange(bridge, peer, self.notification(
            "notifications/initialized")), {"response": None})
        if active:
            bridge.activate()
        return peer

    def assert_closed(self, bridge):
        with self.assertRaises(mcp_host.ToolBridgeError):
            bridge.server_config()
        with self.assertRaises(mcp_host.ToolBridgeError):
            bridge.pump(0.01)
        with self.assertRaises(mcp_host.ToolBridgeError):
            bridge.activate()

    def test_inactive_handshake_allows_listing_but_never_executes(self):
        bridge = self.bridge()
        peer = self.session(bridge, active=False)
        listing = self.exchange(bridge, peer, self.request(1, "tools/list"))["response"]
        self.assertEqual([tool["name"] for tool in listing["result"]["tools"]], ["save_note"])
        denied = self.exchange(bridge, peer, self.request(
            2, "tools/call", name="save_note"))["response"]
        self.assertEqual(denied["error"]["code"], -32600)
        self.assertEqual(self.calls, [])
        bridge.activate()
        accepted = self.exchange(bridge, peer, self.request(
            3, "tools/call", name="save_note"))["response"]
        self.assertFalse(accepted["result"]["isError"])
        self.assertEqual([call.id for call in self.calls], ["mcp-1"])

    def test_revoke_is_irreversible_before_and_after_activation(self):
        for active in (False, True):
            with self.subTest(active=active):
                bridge = self.bridge()
                peer = self.session(bridge, active=active)
                bridge.revoke()
                bridge.revoke()
                with self.assertRaises(mcp_host.ToolBridgeError):
                    bridge.activate()
                for identifier in (1, 2):
                    response = self.exchange(bridge, peer, self.request(
                        identifier, "tools/call", name="save_note"))["response"]
                    self.assertEqual(response["error"]["code"], -32600)
                self.assertEqual(self.calls, [])
                listing = self.exchange(bridge, peer, self.request(3, "tools/list"))["response"]
                self.assertEqual(len(listing["result"]["tools"]), 1)

    def test_only_owner_can_activate(self):
        bridge = self.bridge()
        failures = []
        def activate():
            try:
                bridge.activate()
            except BaseException as exc:
                failures.append(exc)
        thread = threading.Thread(target=activate)
        thread.start()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], mcp_host.ToolBridgeError)
        peer = self.session(bridge, active=False)
        response = self.exchange(bridge, peer, self.request(
            1, "tools/call", name="save_note"))["response"]
        self.assertEqual(response["error"]["code"], -32600)
        self.assertEqual(self.calls, [])

    def test_stdio_config_has_private_ephemeral_environment_and_is_detached(self):
        bridge, other = self.bridge(), self.bridge()
        config = bridge.server_config("fixture")
        self.assertEqual(set(config), {"name", "command", "args", "env"})
        self.assertEqual(config["name"], "fixture")
        self.assertEqual(config["command"], sys.executable)
        self.assertEqual(config["args"][:3], ["-I", "-S", "-B"])
        self.assertEqual(Path(config["args"][-1]).name, "_mcp_bridge_worker.py")
        self.assertTrue(Path(config["args"][-1]).is_file())
        self.assertTrue(all(set(row) == {"name", "value"} for row in config["env"]))
        env = self.environment(bridge)
        self.assertEqual(set(env), {"CATER_MCP_PORT", "CATER_MCP_CAPABILITY", "CATER_MCP_TIMEOUT"})
        self.assertEqual(float(env["CATER_MCP_TIMEOUT"]), 30)
        self.assertNotEqual(env["CATER_MCP_CAPABILITY"], self.environment(other)["CATER_MCP_CAPABILITY"])
        self.assertNotIn(env["CATER_MCP_CAPABILITY"], json.dumps(config["args"]))
        config["env"][0]["value"] = "0"
        config["args"].clear()
        self.assertEqual(self.environment(bridge), env)
        for name in ("", "../fixture", "two words", "x" * 65):
            with self.subTest(name=name), self.assertRaises(ValueError):
                bridge.server_config(name)

    def test_initialize_list_and_chinese_call_preserve_receipt_and_owner(self):
        bridge = self.bridge()
        self.tools[0]["input_schema"]["properties"].clear()
        self.tools.append({"name": "injected", "description": "", "input_schema": {}})
        peer = self.session(bridge)
        listing = self.exchange(bridge, peer, self.request(1, "tools/list"))["response"]
        self.assertEqual(listing, {"jsonrpc": "2.0", "id": 1, "result": {"tools": [{
            "name": "save_note", "description": "Save a fixture note",
            "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}},
        }]}})
        arguments = {"text": "\u4e2d\u6587\u56de\u6267", "nested": {"\u952e": ["\u503c", 7, True, None]}}
        response = self.exchange(bridge, peer, self.request(
            "\u8bf7\u6c42", "tools/call", name="save_note", arguments=arguments))["response"]
        self.assertEqual(response["id"], "\u8bf7\u6c42")
        self.assertFalse(response["result"]["isError"])
        self.assertEqual(json.loads(response["result"]["content"][0]["text"]),
                         {"status": "ok", "receipt": arguments})
        self.assertEqual(self.calls, [ToolCall("mcp-1", "save_note", arguments)])

    def test_initialization_and_tool_allowlist_gate_execution(self):
        bridge = self.bridge()
        bridge.activate()
        peer = self.peer(bridge)
        self.exchange(bridge, peer, {"capability": self.environment(bridge)["CATER_MCP_CAPABILITY"]})
        call = lambda i: self.request(i, "tools/call", name="save_note", arguments={})
        self.assertEqual(self.exchange(bridge, peer, call(1))["response"]["error"]["code"], -32600)
        self.exchange(bridge, peer, self.request(2, "initialize", protocolVersion="2025-06-18"))
        self.assertEqual(self.exchange(bridge, peer, call(3))["response"]["error"]["code"], -32600)
        self.exchange(bridge, peer, self.notification("notifications/initialized"))
        for identifier, params in enumerate((
            {"name": "undeclared"}, {"name": "../save_note"}, {"name": "SAVE_NOTE"},
            {"name": "save_note", "arguments": []}, {"name": "save_note", "arguments": None},
        ), 4):
            result = self.exchange(bridge, peer, self.request(identifier, "tools/call", **params))
            self.assertEqual(result["response"]["error"]["code"], -32602)
        self.assertEqual(self.calls, [])
        self.assertFalse(self.exchange(bridge, peer, call(10))["response"]["result"]["isError"])
        self.assertEqual(len(self.calls), 1)

    def test_pump_rejects_other_thread_and_reentrancy_but_close_is_cross_thread(self):
        bridge = self.bridge()
        errors = []

        def wrong_thread():
            try:
                bridge.pump(0.01)
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=wrong_thread)
        thread.start()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], mcp_host.ToolBridgeError)
        peer = self.session(bridge)
        def reentrant_execute(call):
            with self.assertRaises(mcp_host.ToolBridgeError):
                reentrant.pump(0.01)
            return self.execute(call)
        reentrant = self.bridge(execute=reentrant_execute)
        reentrant_peer = self.session(reentrant)
        result = self.exchange(reentrant, reentrant_peer, self.request(
            1, "tools/call", name="save_note"))
        self.assertFalse(result["response"]["result"]["isError"])
        closer = threading.Thread(target=bridge.close)
        closer.start()
        closer.join(2)
        self.assertFalse(closer.is_alive())
        self.assertEqual(peer[1].readline(), b"")
        self.assert_closed(bridge)
        bridge.close()

    def test_unauthenticated_bad_frames_drop_only_that_connection(self):
        bridge = self.bridge()
        bridge.activate()
        # A smaller bound exercises both complete and unterminated oversized frames.
        with patch.object(mcp_host, "MAX_BYTES", 1024):
            for payload in (
                self.request(1, "tools/call", name="save_note"),
                {"capability": "wrong"}, {"capability": "\u4f2a\u9020"},
                {"capability": "\u4f2a\u9020\ud800"},
                {"capability": None}, [], b"\xff\n",
                b'{"capability":"one","capability":"two"}\n',
                b"x" * 1025 + b"\n", b"x" * 1025,
            ):
                with self.subTest(payload_type=type(payload).__name__):
                    peer = self.peer(bridge)
                    body = payload if isinstance(payload, bytes) else json_utf8(payload) + b"\n"
                    def transfer():
                        peer[0].sendall(body)
                        return peer[1].readline()
                    reply, errors = self.run_io(bridge, transfer)
                    self.assertEqual(reply, b"")
                    self.assertEqual(errors, [])
                    peer[1].close()
                    peer[0].close()
                    self.assertEqual(self.calls, [])
                    bridge.server_config()
        peer = self.session(bridge)
        self.assertFalse(self.exchange(bridge, peer, self.request(
            1, "tools/call", name="save_note"))["response"]["result"]["isError"])
        self.assertEqual(len(self.calls), 1)

    def test_duplicate_call_id_closes_session_without_replay(self):
        for identifier in (7, "same"):
            with self.subTest(identifier=identifier):
                bridge = self.bridge()
                peer = self.session(bridge)
                request = self.request(identifier, "tools/call", name="save_note")
                before = len(self.calls)
                self.exchange(bridge, peer, request)
                self.exchange(bridge, peer, request, rejected=True)
                self.assertEqual(len(self.calls), before + 1)
                self.assert_closed(bridge)

    def test_typed_ids_are_distinct_and_request_budget_stops_execution(self):
        bridge = self.bridge()
        peer = self.session(bridge)
        with patch.object(mcp_host, "MAX_REQUESTS", 3):
            for identifier in (1, "1"):
                self.exchange(bridge, peer, self.request(identifier, "tools/call", name="save_note"))
            self.exchange(bridge, peer, self.request(2, "tools/call", name="save_note"), rejected=True)
        self.assertEqual([call.id for call in self.calls], ["mcp-1", "mcp-2"])

    def assert_callback_exception_aborts(self, error):
        calls = []

        def execute(call):
            self.assertEqual(threading.get_ident(), self.owner)
            calls.append(call)
            raise error

        bridge = self.bridge(execute=execute)
        peer = self.session(bridge)
        # Pipeline a second call to prove the obsolete run cannot continue.
        peer[0].sendall(b"".join(
            json_utf8(self.request(identifier, "tools/call", name="save_note")) + b"\n"
            for identifier in (1, 2)
        ))
        with self.assertRaises(type(error)) as raised:
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                bridge.pump(0.01)
            self.fail("Executor exception did not reach the pump owner")
        self.assert_closed(bridge)
        self.assertEqual(calls, [ToolCall("mcp-1", "save_note", {})])
        self.assertEqual(peer[1].readline(), b"", "Callback exception leaked onto the protocol")
        self.assertIs(raised.exception, error)

    def test_callback_base_exceptions_close_and_propagate_unchanged(self):
        for error in (
            RuntimeError("private-token private-arguments private-path"),
            ValueError("private invalid execution state"),
            OSError("private executor filesystem failure"),
            mcp_host.ToolBridgeError("private executor control failure"),
            KeyboardInterrupt("fixture interrupt"),
            SystemExit(17),
            GeneratorExit("fixture generator closed"),
        ):
            with self.subTest(exception_type=type(error).__name__):
                self.assert_callback_exception_aborts(error)

    def test_scope_changed_closes_bridge_and_never_executes_queued_call(self):
        class ScopeChanged(Exception):
            pass

        self.assert_callback_exception_aborts(ScopeChanged("private obsolete scope"))

    def test_invalid_receipt_is_redacted_with_unknown_effect_warning(self):
        for mode in ("not_dict", "nan", "oversize"):
            with self.subTest(mode=mode):
                effects = []
                def execute(call):
                    effects.append(call)
                    return {"not_dict": [], "nan": {"value": float("nan")},
                            "oversize": {"status": "ok", "text": "x" * 2048}}[mode]
                bridge = self.bridge(execute=execute)
                peer = self.session(bridge)
                with patch.object(mcp_host, "MAX_BYTES", 1024):
                    response = self.exchange(bridge, peer, self.request(
                        1, "tools/call", name="save_note", arguments={"secret": "private-arguments"}))
                result = response["response"]["result"]
                self.assertTrue(result["isError"])
                message = result["content"][0]["text"]
                self.assertIn("effects may have occurred", message)
                self.assertIn("receipts before retrying", message)
                for private in ("private-token", "private-arguments", "private-path",
                                self.environment(bridge)["CATER_MCP_CAPABILITY"]):
                    self.assertNotIn(private, json.dumps(response))
                self.assertEqual(len(effects), 1)
                self.exchange(bridge, peer, self.request(2, "ping"))
                self.assertEqual(len(effects), 1)

    def test_tool_error_receipt_remains_a_tool_result(self):
        receipt = {"status": "error", "complete": False, "outcome": "effects_unknown",
                   "message": "\u8bf7\u6838\u5bf9\u56de\u6267"}
        bridge = self.bridge(execute=lambda call: receipt)
        peer = self.session(bridge)
        result = self.exchange(bridge, peer, self.request(
            1, "tools/call", name="save_note"))["response"]["result"]
        self.assertTrue(result["isError"])
        self.assertEqual(json.loads(result["content"][0]["text"]), receipt)
        self.assertEqual(self.exchange(bridge, peer, self.request(2, "ping"))["response"]["result"], {})
        self.assertTrue(self.exchange(bridge, peer, self.request(
            3, "tools/call", name="save_note"))["response"]["result"]["isError"])

    def test_result_limit_includes_rpc_and_relay_envelopes(self):
        limit = 1024
        receipt = {"status": "ok", "text": ""}
        result = {"content": [{"type": "text", "text": json_utf8(receipt).decode()}], "isError": False}
        receipt["text"] = "x" * (limit - len(json_utf8(result)))
        def execute(call):
            self.execute(call)
            return receipt
        bridge = self.bridge(execute=execute)
        peer = self.session(bridge)
        # The result fits, but the complete wire frame needs additional bytes.
        with patch.object(mcp_host, "MAX_BYTES", limit):
            response = self.exchange(bridge, peer, self.request(
                1, "tools/call", name="save_note"))["response"]
        self.assertTrue(response["result"]["isError"])
        self.assertIn("effects may have occurred", response["result"]["content"][0]["text"])
        self.assertEqual(len(self.calls), 1)

    def test_request_byte_limit_accepts_exact_utf8_boundary_and_rejects_one_more(self):
        bridge = self.bridge()
        peer = self.session(bridge)
        text = "\u4e2d" * 100
        request = self.request(1, "tools/call", name="save_note", arguments={"text": text})
        frame = json_utf8(request)
        self.assertGreater(len(frame), len(frame.decode("utf-8")))
        limit = 1024
        with patch.object(mcp_host, "MAX_BYTES", limit):
            exact = frame + b" " * (limit - len(frame)) + b"\n"
            self.assertFalse(self.exchange(bridge, peer, exact)["response"]["result"]["isError"])
            request["id"] = 2
            frame = json_utf8(request)
            oversized = frame + b" " * (limit + 1 - len(frame)) + b"\n"
            self.exchange(bridge, peer, oversized, rejected=True)
        self.assertEqual(self.calls, [ToolCall("mcp-1", "save_note", {"text": text})])

    def test_revoke_during_execution_preserves_receipt_and_blocks_next_call(self):
        def execute(call):
            bridge.revoke()
            return self.execute(call)
        bridge = self.bridge(execute=execute)
        peer = self.session(bridge)
        first = self.exchange(bridge, peer, self.request(
            1, "tools/call", name="save_note"))["response"]
        self.assertFalse(first["result"]["isError"])
        second = self.exchange(bridge, peer, self.request(
            2, "tools/call", name="save_note"))["response"]
        self.assertEqual(second["error"]["code"], -32600)
        self.assertEqual(len(self.calls), 1)
        with self.assertRaises(mcp_host.ToolBridgeError):
            bridge.activate()

    def test_cross_thread_close_during_execution_does_not_replay_effect(self):
        effects, close_errors = [], []
        def execute(call):
            effects.append(call)
            def close():
                try:
                    bridge.close()
                except BaseException as exc:
                    close_errors.append(exc)
            thread = threading.Thread(target=close)
            thread.start()
            thread.join(2)
            self.assertFalse(thread.is_alive())
            return {"status": "ok", "saved": True}
        bridge = self.bridge(execute=execute)
        peer = self.session(bridge)
        self.exchange(bridge, peer, self.request(
            1, "tools/call", name="save_note"), rejected=True)
        self.assertEqual(close_errors, [])
        self.assertEqual(len(effects), 1)
        self.assert_closed(bridge)

    def test_authenticated_malformed_frames_close_without_execution(self):
        invalid = (
            b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"save_note","arguments":{"text":"\xff"}}}\n',
            b'{"jsonrpc":"2.0","id":1,"id":2,"method":"tools/call","params":{"name":"save_note"}}\n',
            b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"save_note","arguments":{"x":1,"x":2}}}\n',
            b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"save_note","arguments":{"x":NaN}}}\n',
            b"[]\n", b"x" * 1025 + b"\n", b"x" * 1025,
        )
        for index, frame in enumerate(invalid):
            with self.subTest(case=index):
                bridge = self.bridge()
                peer = self.session(bridge)
                with patch.object(mcp_host, "MAX_BYTES", 1024):
                    self.exchange(bridge, peer, frame, rejected=True)
                self.assert_closed(bridge)
                self.assertEqual(self.calls, [])

    def test_partial_frame_and_unauthenticated_disconnect_leave_no_executor_call(self):
        bridge = self.bridge(timeout=0.05)
        peer = self.peer(bridge)
        peer[0].sendall(b'{"capability":')
        reply, errors = self.run_io(bridge, lambda: peer[1].readline())
        self.assertEqual((reply, errors), (b"", []))
        peer = self.peer(bridge)
        peer[1].close()
        peer[0].close()
        self.assertFalse(bridge.pump(0.01))
        peer = self.session(bridge)
        self.exchange(bridge, peer, b'{"jsonrpc":', rejected=True)
        self.assertEqual(self.calls, [])
        self.assert_closed(bridge)

    def test_unauthenticated_tcp_reset_preserves_listener_for_normal_session(self):
        bridge = self.bridge()
        bridge.activate()
        configuration = bridge.server_config()
        peer = self.peer(bridge)
        peer[0].sendall(b'{"capability":')
        self.assertFalse(bridge.pump(0.01))
        # Abort a real TCP connection, including any buffered partial auth frame.
        linger = struct.pack("HH" if os.name == "nt" else "ii", 1, 0)
        peer[0].setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, linger)
        peer[1].close()
        peer[0].close()
        observed = []
        original_recv = socket.socket.recv

        def recv(connection, *args, **kwargs):
            try:
                return original_recv(connection, *args, **kwargs)
            except ConnectionResetError as exc:
                observed.append(exc)
                raise

        with patch.object(socket.socket, "recv", recv):
            self.assertFalse(bridge.pump(0.1))
        self.assertEqual(len(observed), 1, "Fixture must exercise TCP RST, not graceful EOF")
        self.assertEqual(self.calls, [])
        self.assertEqual(bridge.server_config(), configuration)
        healthy = self.session(bridge)
        response = self.exchange(bridge, healthy, self.request(
            1, "tools/call", name="save_note", arguments={"text": "after reset"}))
        self.assertFalse(response["response"]["result"]["isError"])
        self.assertEqual(self.calls, [ToolCall("mcp-1", "save_note", {"text": "after reset"})])

    def test_authenticated_disconnect_or_close_never_reopens_listener(self):
        for action in ("disconnect", "close"):
            with self.subTest(action=action):
                bridge = self.bridge()
                port = int(self.environment(bridge)["CATER_MCP_PORT"])
                peer = self.session(bridge)
                if action == "disconnect":
                    peer[1].close()
                    peer[0].close()
                    with self.assertRaises(mcp_host.ToolBridgeError):
                        bridge.pump(0.01)
                else:
                    bridge.close()
                self.assert_closed(bridge)
                with self.assertRaises(OSError):
                    with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                        pass
                self.assertEqual(self.calls, [])

    def test_cancellation_queued_during_execution_cannot_retract_effect(self):
        effects = []
        def execute(call):
            effects.append(call.arguments)
            peer[0].sendall(json_utf8(self.notification(
                "notifications/cancelled", requestId=1, reason="fixture cancellation")) + b"\n")
            return {"status": "ok", "saved": True}
        bridge = self.bridge(execute=execute)
        peer = self.session(bridge)
        result = self.exchange(bridge, peer, self.request(
            1, "tools/call", name="save_note", arguments={"text": "persisted"}))
        self.assertEqual(json.loads(result["response"]["result"]["content"][0]["text"]),
                         {"status": "ok", "saved": True})
        self.assertFalse(result["response"]["result"]["isError"])
        # The notification may already have been pumped while the reader finished.
        reply, errors = self.run_io(bridge, lambda: peer[1].readline())
        self.assertEqual(json.loads(reply), {"response": None})
        self.assertEqual(errors, [])
        self.exchange(bridge, peer, self.request(2, "ping"))
        self.assertEqual(effects, [{"text": "persisted"}])

    def relay(self, bridge, frames):
        config = bridge.server_config()
        # No model credentials, user Python paths, or project runtime state.
        env = {key: os.environ[key] for key in ("SystemRoot", "WINDIR", "TEMP", "TMP")
               if key in os.environ}
        env.update({row["name"]: row["value"] for row in config["env"]})
        with tempfile.TemporaryDirectory() as cwd:
            process = subprocess.Popen(
                [config["command"], *config["args"]], env=env, cwd=cwd,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            try:
                output, errors = self.run_io(
                    bridge, lambda: process.communicate(input=frames, timeout=8), disconnect=True)
                self.assertIsNotNone(process.poll())
                return process.returncode, output[0], output[1], errors
            finally:
                # This fixture owns the subprocess, as ACP's ProcessScope does
                # in production. Socket close alone cannot stop an idle stdin reader.
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
                for pipe in (process.stdin, process.stdout, process.stderr):
                    pipe.close()

    def transcript(self, *requests):
        messages = [
            self.request("init", "initialize", protocolVersion="2025-06-18",
                         capabilities={}, clientInfo={"name": "offline-fixture", "version": "1"}),
            self.notification("notifications/initialized"), *requests,
        ]
        return b"".join(json_utf8(message, allow_nan=False) + b"\n" for message in messages)

    def test_real_relay_workspace_read_write_cas_and_owner_sqlite_audit(self):
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        root = Path(temporary) / "workspace"
        root.mkdir()
        executor = WorkspaceExecutor(WorkspacePolicy.from_dict({"root": str(root), "mode": "edit"}))
        self.tools = executor.schemas()
        database = sqlite3.connect(Path(temporary) / "audit.db")
        self.addCleanup(database.close)
        database.execute("CREATE TABLE receipts (call_id TEXT, tool TEXT, receipt TEXT)")
        original, updated = "\u4e2d\u6587\u521d\u7a3f\r\n", "\u4e2d\u6587\u4fee\u8ba2\n"
        original_sha = hashlib.sha256(original.encode("utf-8")).hexdigest()
        updated_sha = hashlib.sha256(updated.encode("utf-8")).hexdigest()

        def execute(call):
            self.assertEqual(threading.get_ident(), self.owner)
            self.calls.append(call)
            receipt = executor.execute(call.name, call.arguments)
            database.execute("INSERT INTO receipts VALUES (?, ?, ?)",
                             (call.id, call.name, json.dumps(receipt)))
            database.commit()
            return receipt

        bridge = self.bridge(execute=execute)
        bridge.activate()
        code, stdout, stderr, errors = self.relay(bridge, self.transcript(
            self.request(1, "tools/list"),
            self.request(2, "tools/call", name="workspace_write",
                         arguments={"path": "note.txt", "content": original, "expected_sha256": None}),
            self.request(3, "tools/call", name="workspace_read", arguments={"path": "note.txt"}),
            self.request(4, "tools/call", name="workspace_write",
                         arguments={"path": "note.txt", "content": updated, "expected_sha256": original_sha}),
            self.request(5, "tools/call", name="workspace_write",
                         arguments={"path": "note.txt", "content": "stale", "expected_sha256": original_sha}),
            self.notification("notifications/cancelled", requestId=4),
            self.request(6, "tools/call", name="workspace_read", arguments={"path": "note.txt"}),
            self.request(7, "ping"),
        ))
        self.assertEqual((code, stderr), (0, b""))
        rows = [json.loads(line) for line in stdout.splitlines()]
        self.assertEqual([row["id"] for row in rows], ["init", 1, 2, 3, 4, 5, 6, 7])
        self.assertEqual(rows[0]["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(rows[1]["result"]["tools"], [
            {"name": tool["name"], "description": tool["description"], "inputSchema": tool["input_schema"]}
            for tool in executor.schemas()
        ])
        self.assertNotIn("workspace_run", {tool["name"] for tool in rows[1]["result"]["tools"]})
        results = [row["result"] for row in rows[2:7]]
        self.assertEqual([result["isError"] for result in results], [False, False, False, True, False])
        receipts = [json.loads(result["content"][0]["text"]) for result in results]
        self.assertTrue(receipts[0]["created"])
        self.assertEqual(receipts[0]["sha256"], original_sha)
        self.assertEqual(receipts[1]["content"], original)
        self.assertEqual(receipts[1]["sha256"], original_sha)
        self.assertEqual(receipts[1]["bytes"], len(original.encode("utf-8")))
        self.assertFalse(receipts[2]["created"])
        self.assertEqual(receipts[2]["sha256"], updated_sha)
        self.assertEqual(receipts[3]["error"]["code"], "conflict")
        self.assertEqual(receipts[4]["content"], updated)
        self.assertEqual(receipts[4]["sha256"], updated_sha)
        self.assertEqual((root / "note.txt").read_bytes(), updated.encode("utf-8"))
        self.assertEqual(hashlib.sha256((root / "note.txt").read_bytes()).hexdigest(), updated_sha)
        self.assertEqual(sorted(path.name for path in root.iterdir()), ["note.txt"])
        self.assertEqual(rows[7]["result"], {})
        self.assertIn("\u4e2d\u6587".encode("utf-8"), stdout)
        with closing(sqlite3.connect(Path(temporary) / "audit.db")) as observer:
            audit = observer.execute("SELECT call_id, tool, receipt FROM receipts ORDER BY rowid").fetchall()
        self.assertEqual([row[:2] for row in audit], [
            (f"mcp-{index}", name) for index, name in enumerate(
                ("workspace_write", "workspace_read", "workspace_write", "workspace_write", "workspace_read"), 1)
        ])
        self.assertEqual([json.loads(row[2]) for row in audit], receipts)
        self.assertEqual(len(self.calls), 5)
        # EOF may be observed before communicate finishes or on the following pump.
        if not errors:
            with self.assertRaises(mcp_host.ToolBridgeError):
                bridge.pump(0.1)
        self.assert_closed(bridge)

    def test_real_relay_large_chinese_receipt_does_not_expand_to_ascii(self):
        text = "\u4e2d" * (mcp_host.MAX_BYTES // 4)
        bridge = self.bridge(execute=lambda call: {"status": "ok", "text": text})
        bridge.activate()
        code, stdout, stderr, _ = self.relay(bridge, self.transcript(
            self.request(1, "tools/call", name="save_note")))
        self.assertEqual((code, stderr), (0, b""))
        rows = stdout.splitlines()
        self.assertEqual(len(rows), 2)
        self.assertLessEqual(len(rows[1]), mcp_host.MAX_BYTES)
        result = json.loads(rows[1])["result"]
        self.assertFalse(result["isError"])
        self.assertEqual(json.loads(result["content"][0]["text"])["text"], text)

    def test_real_relay_duplicate_id_exits_without_replay_or_secret_output(self):
        bridge = self.bridge()
        bridge.activate()
        capability = self.environment(bridge)["CATER_MCP_CAPABILITY"].encode()
        call = self.request(1, "tools/call", name="save_note", arguments={"text": "once"})
        code, stdout, stderr, _ = self.relay(bridge, self.transcript(call, call))
        self.assertEqual((code, stderr), (1, b""))
        self.assertEqual([json.loads(line)["id"] for line in stdout.splitlines()], ["init", 1])
        self.assertNotIn(capability, stdout + stderr)
        self.assertEqual(len(self.calls), 1)
        self.assert_closed(bridge)

    def test_real_relay_inactive_handshake_lists_tools_but_rejects_business_call(self):
        bridge = self.bridge()
        code, stdout, stderr, _ = self.relay(bridge, self.transcript(
            self.request(1, "tools/list"), self.request(2, "tools/call", name="save_note")))
        self.assertEqual((code, stderr), (0, b""))
        rows = [json.loads(line) for line in stdout.splitlines()]
        self.assertEqual([row["id"] for row in rows], ["init", 1, 2])
        self.assertEqual(rows[1]["result"]["tools"][0]["name"], "save_note")
        self.assertEqual(rows[2]["error"]["code"], -32600)
        self.assertEqual(self.calls, [])

    def test_real_relay_host_close_during_exchange_exits_without_retry_or_queued_execution(self):
        def execute(call):
            receipt = self.execute(call)
            bridge.close()
            return receipt
        bridge = self.bridge(execute=execute)
        bridge.activate()
        code, stdout, stderr, _ = self.relay(bridge, self.transcript(
            self.request(1, "tools/call", name="save_note"),
            self.request(2, "tools/call", name="save_note")))
        self.assertEqual((code, stderr), (1, b""))
        self.assertEqual([json.loads(line)["id"] for line in stdout.splitlines()], ["init"])
        self.assertEqual(len(self.calls), 1)
        self.assert_closed(bridge)

    def test_real_relay_rejects_invalid_utf8_duplicate_keys_and_oversize(self):
        for index, frame in enumerate((
            b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"save_note","arguments":{"x":"\xff"}}}\n',
            b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"save_note","name":"other"}}\n',
            b"x" * (mcp_host.MAX_BYTES + 1) + b"\n",
            b'{"jsonrpc":"2.0"',
        )):
            with self.subTest(case=index):
                bridge = self.bridge()
                bridge.activate()
                code, stdout, stderr, _ = self.relay(bridge, self.transcript() + frame)
                self.assertEqual((code, stderr), (1, b""))
                self.assertEqual([json.loads(line)["id"] for line in stdout.splitlines()], ["init"])
                self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
