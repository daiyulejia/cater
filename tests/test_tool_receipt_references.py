"""Independent offline contract for out-of-band tool receipt references.

Do not import the new result class at collection time: legacy implementations
must still run ordinary-dict, MCP lifecycle, framing, and host contract checks.
"""

from __future__ import annotations

import copy
import json
import threading
import unittest
from unittest.mock import patch

from cater import mcp_host, tool_protocol
from cater.events import Actor
from cater.team import HOST
from cater.tool_protocol import ToolCall, json_utf8
from cater.work import WorkConflict
from cater.workspace_tools import WorkspaceExecutor
import test_mcp_host as bridge_fixtures
import test_team_external as external
import test_team_supervision as native


HASH = "0123456789abcdef" * 4
SEQ = 712
TOOL = "workspace_read"
SUPPORTED = {"workspace_read", "workspace_write", "workspace_run", "workspace_list",
             "workspace_search", "browser_check", "read_artifact"}
TOOLS = [{"name": TOOL, "description": "Read an offline fixture",
          "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}}}]


def recorded_type(case):
    cls = getattr(tool_protocol, "RecordedToolResult", None)
    case.assertIsNotNone(cls, "Missing dictionary-compatible RecordedToolResult contract")
    case.assertTrue(issubclass(cls, dict))
    return cls


def recorded(case, body, seq=SEQ, fingerprint=HASH):
    return recorded_type(case)(body, seq, fingerprint)


def assert_reference(case, value, seq=SEQ, fingerprint=HASH):
    case.assertIs(type(value), recorded_type(case))
    case.assertEqual(value.receipt_seq, seq)
    case.assertIs(type(value.receipt_seq), int)
    case.assertEqual(value.receipt_hash, fingerprint)
    ref = value.receipt_reference()
    case.assertEqual(set(ref), {"source_seq", "source_hash", "read_tool", "read_args", "note"})
    case.assertEqual(ref["source_seq"], seq)
    case.assertEqual(ref["source_hash"], fingerprint)
    case.assertEqual(ref["read_tool"], "read_receipts")
    case.assertEqual(ref["read_args"], {"receipt_seq": seq})
    case.assertIsInstance(ref["note"], str)
    case.assertTrue(ref["note"].strip())
    return ref


def assert_wire(case, result, body, *, pointer=True, seq=SEQ, fingerprint=HASH):
    case.assertEqual(set(result), {"content", "isError"})
    case.assertIs(result["isError"], body.get("status") != "ok")
    case.assertEqual(len(result["content"]), 2 if pointer else 1)
    case.assertEqual(result["content"][0], {"type": "text", "text": json_utf8(body, allow_nan=False).decode("utf-8")})
    case.assertEqual(json.loads(result["content"][0]["text"]), body)
    if pointer:
        block = result["content"][1]
        case.assertEqual(block["type"], "text")
        footer = json.loads(block["text"])
        case.assertEqual(set(footer), {"cater_receipt"})
        ref = footer["cater_receipt"]
        case.assertEqual(set(ref), {"source_seq", "source_hash", "read_tool", "read_args", "note"})
        case.assertEqual(ref["source_seq"], seq)
        case.assertEqual(ref["source_hash"], fingerprint)
        case.assertEqual(ref["read_tool"], "read_receipts")
        case.assertEqual(ref["read_args"], {"receipt_seq": seq})
        case.assertTrue(isinstance(ref["note"], str) and ref["note"].strip())


class RecordedResultValueTests(unittest.TestCase):
    def test_original_dictionary_equality_iteration_and_json_are_unchanged(self):
        body = {"status": "ok", "content": "\u4e2d\u6587\n\"body\"", "nested": {"rows": [1, False, None]},
                "escaped": "\ud800"}
        frozen = copy.deepcopy(body)
        value = recorded(self, body)
        self.assertEqual(value, body)
        self.assertEqual(body, value)
        self.assertEqual(list(value), list(body))
        self.assertEqual(list(value.items()), list(body.items()))
        self.assertEqual(len(value), len(body))
        self.assertEqual(dict(value), body)
        self.assertEqual(json.dumps(value), json.dumps(body))
        self.assertEqual(json_utf8(value), json_utf8(body))
        self.assertEqual(body, frozen)
        assert_reference(self, value)
        self.assertFalse(hasattr(value, "__dict__"), "Reference metadata must live in slots")
        self.assertNotIn("_reference", value)
        for name in ("receipt_seq", "receipt_hash"):
            with self.assertRaises(AttributeError):
                setattr(value, name, 9)

    def test_deepcopy_preserves_pointer_and_independent_body_without_cycles(self):
        body = {"status": "ok", "nested": {"rows": [1, {"value": "original"}]}}
        value = recorded(self, body)
        cloned = copy.deepcopy(value)
        self.assertIsNot(cloned, value)
        assert_reference(self, cloned)
        self.assertEqual(json_utf8(cloned), json_utf8(body))
        cloned["nested"]["rows"][1]["value"] = "changed"
        self.assertEqual(value["nested"]["rows"][1]["value"], "original")
        self.assertEqual(body["nested"]["rows"][1]["value"], "original")
        plain = json.loads(json_utf8(value))
        self.assertIs(type(plain), dict)
        self.assertFalse(hasattr(plain, "receipt_reference"))
        ref = value.receipt_reference()
        ref["read_args"]["receipt_seq"] = 1
        self.assertEqual(value.receipt_reference()["read_args"], {"receipt_seq": SEQ})

    def test_constructor_enforces_positive_int64_and_lowercase_hash(self):
        cls = recorded_type(self)
        for seq in (1, 2 ** 63 - 1):
            with self.subTest(valid_seq=seq):
                assert_reference(self, cls({"status": "ok"}, seq, HASH), seq, HASH)
        for seq in (True, False, 0, -1, 2 ** 63, 2 ** 80, 1.0, "1", None, [], {}):
            with self.subTest(invalid_seq=seq), self.assertRaises((ValueError, TypeError)):
                cls({}, seq, HASH)
        for fingerprint in (HASH.upper(), "a" * 63, "a" * 65, "g" * 64, " " + HASH,
                            HASH + "\n", None, True, 123, b"a" * 64, [], {}):
            with self.subTest(hash=repr(fingerprint)), self.assertRaises((ValueError, TypeError)):
                cls({}, SEQ, fingerprint)
        for body in (None, [], "body", 1, True):
            with self.subTest(body=body), self.assertRaises((ValueError, TypeError)):
                cls(body, SEQ, HASH)

    def test_same_named_user_keys_stay_plain_data_and_cannot_replace_slot(self):
        body = {"status": "ok", "receipt_seq": 9, "receipt_hash": "b" * 64,
                "_reference": [8, "fake"], "cater_receipt": {"source_seq": 7},
                "source_seq": 6, "source_hash": "c" * 64}
        value = recorded(self, body)
        assert_reference(self, value)
        self.assertEqual(json_utf8(value), json_utf8(body))
        self.assertEqual(value["receipt_seq"], 9)
        self.assertEqual(value.receipt_seq, SEQ)


class ReferenceBridgeFixture(unittest.TestCase):
    def setUp(self):
        self.f = bridge_fixtures.McpToolBridgeTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.tools = copy.deepcopy(TOOLS)
        self.effects = []

    def session(self, value, *, active=True):
        def execute(call):
            self.assertEqual(threading.get_ident(), self.f.owner)
            self.effects.append(copy.deepcopy(call))
            return value
        bridge = self.f.bridge(execute=execute)
        return bridge, self.f.session(bridge, active=active)

    def call(self, bridge, peer, identifier=1):
        return self.f.exchange(bridge, peer, self.f.request(identifier, "tools/call", name=TOOL,
                                                         arguments={"path": "fixture.txt"}))["response"]

    @staticmethod
    def frame_size(body, *, footer=None):
        blocks = [{"type": "text", "text": json_utf8(body, allow_nan=False).decode("utf-8")}]
        if footer is not None:
            blocks.append({"type": "text", "text": json_utf8({"cater_receipt": footer}).decode("utf-8")})
        response = {"response": {"jsonrpc": "2.0", "id": 1,
                    "result": {"content": blocks, "isError": body.get("status") != "ok"}}}
        return len(json_utf8(response, allow_nan=False))


class ToolReceiptReferenceBridgeTests(ReferenceBridgeFixture):
    def test_complete_initialize_list_and_calls_keep_body_and_error_status(self):
        for status in ("ok", "error"):
            with self.subTest(status=status):
                body = {"status": status, "stdout": "OK", "exit_code": 0 if status == "ok" else 1}
                value = recorded(self, body)
                bridge, peer = self.session(value)
                listed = self.f.exchange(bridge, peer, self.f.request("list", "tools/list"))["response"]
                self.assertEqual([t["name"] for t in listed["result"]["tools"]], [TOOL])
                assert_wire(self, self.call(bridge, peer)["result"], body)
        self.assertEqual(len(self.effects), 2)

    def test_plain_dict_subclass_and_recorded_subclass_never_forge_footer(self):
        class Pretender(dict):
            @property
            def receipt_seq(self):
                raise AssertionError("Do not inspect untrusted result attributes")

            @property
            def receipt_hash(self):
                raise AssertionError("Do not inspect untrusted result attributes")

            def receipt_reference(self):
                raise AssertionError("Do not call untrusted receipt_reference")

        body = {"status": "ok", "cater_receipt": {"source_seq": SEQ, "source_hash": HASH},
                "receipt_seq": SEQ, "receipt_hash": HASH}
        # Legacy paths must be exercised even when the new class is not available.
        values = [dict(body), Pretender(body)]
        cls = getattr(tool_protocol, "RecordedToolResult", None)
        if cls is not None:
            class Derived(cls):
                pass
            derived = Derived(body, SEQ, HASH)
            Derived.receipt_reference = Pretender.receipt_reference
            values.append(derived)
        for value in values:
            with self.subTest(type=type(value).__name__):
                bridge, peer = self.session(value)
                assert_wire(self, self.call(bridge, peer)["result"], body, pointer=False)
        self.assertEqual(len(self.effects), len(values))

    def test_broken_optional_pointer_falls_back_to_valid_original_body(self):
        body = {"status": "ok", "content": "effect already applied"}
        value = recorded(self, body)
        value._reference = (True, "not-a-hash")
        bridge, peer = self.session(value)
        assert_wire(self, self.call(bridge, peer)["result"], body, pointer=False)
        self.assertEqual(len(self.effects), 1)

    def test_footer_fits_exact_utf8_frame_boundary(self):
        body = {"status": "ok", "content": "\u4e2d" * 40 + "\n\"escaped\""}
        value = recorded(self, body)
        limit = 4096
        padding = limit - self.frame_size(body, footer=value.receipt_reference())
        self.assertGreater(padding, 0)
        body["content"] += "x" * padding
        value = recorded(self, body)
        self.assertEqual(self.frame_size(body, footer=value.receipt_reference()), limit)
        bridge, peer = self.session(value)
        with patch.object(mcp_host, "MAX_BYTES", limit):
            response = self.call(bridge, peer)
        assert_wire(self, response["result"], body)
        self.assertEqual(len(self.effects), 1)

    def test_footer_overflow_returns_verified_body_at_exact_frame_cap(self):
        body = {"status": "ok", "content": "\u4e2d" * 40 + "\\\n"}
        limit = 4096
        body["content"] += "x" * (limit - self.frame_size(body))
        value = recorded(self, body)
        self.assertEqual(self.frame_size(body), limit)
        self.assertGreater(self.frame_size(body, footer=value.receipt_reference()), limit)
        bridge, peer = self.session(value)
        with patch.object(mcp_host, "MAX_BYTES", limit):
            response = self.call(bridge, peer)
            ping = self.f.exchange(bridge, peer, self.f.request("ping", "ping"))
        assert_wire(self, response["result"], body, pointer=False)
        self.assertEqual(ping["response"]["result"], {})
        self.assertEqual(len(self.effects), 1, "Footer fallback must never retry the executor")

    def test_failed_original_stays_error_when_optional_footer_exceeds_cap(self):
        body = {"status": "error", "stdout": "OK", "exit_code": 1, "message": "\u5931\u8d25" * 40}
        body["message"] += "x" * (4096 - self.frame_size(body))
        bridge, peer = self.session(recorded(self, body))
        with patch.object(mcp_host, "MAX_BYTES", 4096):
            result = self.call(bridge, peer)["result"]
        assert_wire(self, result, body, pointer=False)
        self.assertIs(result["isError"], True)
        self.assertEqual(len(self.effects), 1)

    def test_invalid_or_original_body_one_byte_over_cap_keeps_legacy_error(self):
        oversized = {"status": "ok", "content": "\u4e2d" * 40}
        oversized["content"] += "x" * (4097 - self.frame_size(oversized))
        self.assertEqual(self.frame_size(oversized), 4097)
        cyclic = {"status": "ok"}
        cyclic["cycle"] = cyclic
        bodies = [oversized, {"status": "ok", "value": float("nan")},
                  {"status": "ok", "value": object()}, cyclic, ["not an object"]]
        cls = getattr(tool_protocol, "RecordedToolResult", None)
        expected_calls = 0
        for body in bodies:
            # Ordinary legacy results run independently of the new class's availability.
            values = [body]
            if cls is not None and isinstance(body, dict):
                values.append(cls(body, SEQ, HASH))
            for value in values:
                with self.subTest(body_type=type(value).__name__):
                    bridge, peer = self.session(value)
                    with patch.object(mcp_host, "MAX_BYTES", 4096):
                        result = self.call(bridge, peer)["result"]
                    expected_calls += 1
                    self.assertIs(result["isError"], True)
                    self.assertEqual(len(result["content"]), 1)
                    self.assertIn("effects may have occurred", result["content"][0]["text"])
        self.assertEqual(len(self.effects), expected_calls)

    def test_socket_capability_activation_and_revoke_never_grant_execution(self):
        bridge = self.f.bridge(execute=lambda call: self.effects.append(call) or {"status": "ok"})
        bad_peer = self.f.peer(bridge)
        rejected, errors = self.f.run_io(bridge, lambda: (
            bad_peer[0].sendall(json_utf8({"capability": "untrusted-capability"}) + b"\n"),
            bad_peer[1].readline())[1])
        self.assertEqual(rejected, b"")
        self.assertEqual(errors, [])
        self.assertEqual(self.effects, [])
        peer = self.f.session(bridge, active=False)
        self.assertIn("error", self.call(bridge, peer))
        self.assertEqual(self.effects, [])
        bridge.activate()
        assert_wire(self, self.call(bridge, peer, 2)["result"], {"status": "ok"}, pointer=False)
        bridge.revoke()
        self.assertIn("error", self.call(bridge, peer, 3))
        self.assertEqual(len(self.effects), 1)

    def test_real_stdio_relay_transports_two_blocks_and_executes_once(self):
        body = {"status": "error", "stdout": "OK\n\u4e2d\u6587", "exit_code": 1}
        value = recorded(self, body)
        def execute(call):
            self.assertEqual(threading.get_ident(), self.f.owner)
            self.effects.append(call)
            return value
        bridge = self.f.bridge(execute=execute)
        bridge.activate()
        frames = self.f.transcript(self.f.request(1, "tools/call", name=TOOL, arguments={"path": "fixture.txt"}),
                                   self.f.request(2, "ping"))
        returncode, stdout, stderr, _disconnect = self.f.relay(bridge, frames)
        self.assertEqual(returncode, 0)
        self.assertEqual(stderr, b"")
        replies = [json.loads(line) for line in stdout.splitlines()]
        self.assertEqual([reply["id"] for reply in replies], ["init", 1, 2])
        assert_wire(self, replies[1]["result"], body)
        self.assertEqual(replies[2]["result"], {})
        self.assertEqual(len(self.effects), 1)


class ToolReceiptReferenceHostTests(unittest.TestCase):
    def fixture(self, cls=native.TeamSupervisionTests):
        fixture = cls()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def context(self, fixture):
        message = fixture.rt.team.receive(fixture.task, "Inspect the original host receipt only.")
        self.assertEqual(message.actor, Actor.human())
        claimed, role, run = fixture.rt.team._claim(fixture.task)
        self.assertEqual(claimed, message)
        return role, message, run

    def invoke(self, fixture, context, name, *, result=None, error=None):
        team, log, task = fixture.rt.team, fixture.rt.log, fixture.task
        role, message, run = context
        start = log.emit(type="team.tool_started", task_id=task, actor=HOST, payload={
            **run.payload, "name": name, "call_id": f"reference-{log.count()}", "arguments": {}})
        call = ToolCall(start.payload["call_id"], name, {})
        with patch.object(team, "_execute", side_effect=error, return_value=result) as execute:
            value = team._invoke_tool(task, role, run, run.payload["revision"], call,
                {"properties": {}, "required": []}, None, {}, start, None)
        execute.assert_called_once()
        receipt, = [e for e in log.read(task_id=task) if e.type == "team.tool_finished"
                    and e.payload["started_seq"] == start.seq]
        return start, receipt, value

    def assert_host_reference(self, fixture, start, receipt, value):
        self.assertEqual(receipt.actor, HOST)
        self.assertLess(start.seq, receipt.seq)
        self.assertNotEqual(value.receipt_seq, start.seq)
        self.assertNotEqual(value.receipt_hash, start.hash)
        assert_reference(self, value, receipt.seq, receipt.hash)
        persisted = receipt.payload["result"]
        self.assertIs(type(persisted), dict)
        self.assertFalse(hasattr(persisted, "receipt_reference"))
        self.assertEqual(dict(value), persisted)
        self.assertEqual(json_utf8(value, sort_keys=True), json_utf8(persisted, sort_keys=True))
        self.assertEqual(fixture.rt.log.read(task_id=fixture.task)[-1], receipt)

    def test_settle_attaches_actual_finished_identity_for_each_supported_status(self):
        fixture = self.fixture()
        context = self.context(fixture)
        from cater.reply_evidence import SUPPORTED as actual_supported
        self.assertEqual(actual_supported, SUPPORTED)
        for name in sorted(SUPPORTED):
            for status in ("ok", "error"):
                with self.subTest(tool=name, status=status):
                    body = {"status": status, "content": "literal result", "source_seq": 123,
                            "source_hash": "b" * 64, "receipt_seq": 456}
                    start, receipt, value = self.invoke(fixture, context, name, result=body)
                    self.assertEqual(value, body)
                    self.assertEqual(json_utf8(value), json_utf8(body))
                    self.assert_host_reference(fixture, start, receipt, value)
                    self.assertEqual(receipt.payload["result"], body)

    def test_non_candidates_keep_plain_result_and_no_new_wire_fields(self):
        fixture = self.fixture()
        context = self.context(fixture)
        for name in ("read_receipts", "read_messages", "wait_for_reply", "reply_to_customer", "delegate"):
            with self.subTest(tool=name):
                body = {"status": "ok", "message": "unchanged body"}
                _start, receipt, value = self.invoke(fixture, context, name, result=body)
                self.assertIs(type(value), dict)
                self.assertFalse(hasattr(value, "receipt_reference"))
                self.assertEqual(value, body)
                self.assertEqual(receipt.payload["result"], body)

    def test_failed_program_ok_output_and_scope_errors_keep_finished_reference(self):
        fixture = self.fixture()
        context = self.context(fixture)
        body = {"status": "error", "stdout": "Ran 15 tests\nOK\n", "stderr": "",
                "outcome": "failed", "exit_code": 1, "complete": True}
        start, receipt, value = self.invoke(fixture, context, "workspace_run", result=body)
        self.assert_host_reference(fixture, start, receipt, value)
        bridge_fixture = ReferenceBridgeFixture()
        bridge_fixture.setUp()
        self.addCleanup(bridge_fixture.doCleanups)
        bridge, peer = bridge_fixture.session(value)
        wire = bridge_fixture.call(bridge, peer)["result"]
        assert_wire(self, wire, body, seq=receipt.seq, fingerprint=receipt.hash)
        self.assertIs(wire["isError"], True)
        start, receipt, value = self.invoke(fixture, context, "workspace_read",
                                            error=WorkConflict("Original workspace scope conflict."))
        self.assert_host_reference(fixture, start, receipt, value)
        self.assertEqual(value["status"], "error")
        self.assertEqual(value["message"], "Original workspace scope conflict.")

    def test_native_actual_read_receipt_slot_never_enters_model_history(self):
        fixture = self.fixture()
        fixture.bind_workspace()
        binding = fixture.rt.workspaces.view(fixture.task)
        executor = WorkspaceExecutor(fixture.rt.workspaces.config[binding["workspace_id"]]["policy"])
        written = executor.execute("workspace_write", {"path": "native.txt", "content": "NATIVE_EXACT_BODY",
                                                       "expected_sha256": None})
        self.assertEqual(written["status"], "ok", written)
        observed = []
        dispatch = fixture.rt.team._dispatch_tool
        def dispatch_spy(*args, **kwargs):
            start, value = dispatch(*args, **kwargs)
            if start.payload["name"] == TOOL:
                receipt, = [e for e in fixture.events("team.tool_finished") if e.payload["started_seq"] == start.seq]
                self.assert_host_reference(fixture, start, receipt, value)
                observed.append((receipt, value))
            return start, value
        def inspect(context, history, tools):
            reply, = [row for row in history if row.get("role") == "tool"]
            decoded = json.loads(reply["content"])
            self.assertEqual(decoded, observed[0][0].payload["result"])
            self.assertEqual(decoded["content"], "NATIVE_EXACT_BODY")
            self.assertEqual(set(decoded), set(observed[0][1]))
            self.assertNotIn("cater_receipt", decoded)
            self.assertNotIn("_reference", decoded)
            return fixture.native_turn("Native result inspected.")
        fixture.driver.steps[fixture.manager] = [fixture.native_turn("", (TOOL, {"path": "native.txt"})), inspect]
        fixture.rt.team.receive(fixture.task, "Read the existing fixture file.")
        with patch.object(fixture.rt.team, "_dispatch_tool", side_effect=dispatch_spy):
            fixture.run_team()
        self.assertEqual(len(observed), 1)
        self.assertEqual(len(fixture.driver.calls), 2)

    def test_external_host_actual_read_and_error_cross_real_socket_once_each(self):
        fixture = self.fixture(external.TeamExternalTests)
        io_fixture = bridge_fixtures.McpToolBridgeTests()
        io_fixture.setUp()
        self.addCleanup(io_fixture.doCleanups)
        observed = []
        def script(run):
            published = run.call("publish_artifact", filename="external.txt", content="EXTERNAL_EXACT_BODY",
                                 summary="Offline fixture artifact", format="text")
            self.assertIs(type(published), dict)
            def execute(call):
                value = run.call(call.name, **call.arguments)
                receipt = fixture.events("team.tool_finished")[-1]
                start = next(e for e in fixture.events("team.tool_started") if e.seq == receipt.payload["started_seq"])
                self.assert_host_reference(fixture, start, receipt, value)
                observed.append((receipt, value))
                return value
            io_fixture.tools = [tool for tool in run.tools if tool["name"] == "read_artifact"]
            bridge = io_fixture.bridge(execute=execute)
            peer = io_fixture.session(bridge)
            for identifier, seq in ((1, published["artifact_seq"]), (2, 2 ** 62)):
                response = io_fixture.exchange(bridge, peer, io_fixture.request(identifier, "tools/call",
                    name="read_artifact", arguments={"artifact_seq": seq}))["response"]["result"]
                receipt, value = observed[-1]
                assert_wire(self, response, dict(value), seq=receipt.seq, fingerprint=receipt.hash)
            self.assertEqual(observed[0][1]["status"], "ok")
            self.assertEqual(observed[0][1]["content"], "EXTERNAL_EXACT_BODY")
            self.assertEqual(observed[1][1]["status"], "error")
            return "Original external receipts inspected."
        fixture.driver.scripts = {fixture.manager: [script]}
        fixture.receive("Inspect the saved artifact and report the missing one.")
        fixture.drain()
        self.assertEqual(len(fixture.driver.runs), 1)
        self.assertEqual(len(observed), 2)

    def test_old_plain_event_readback_does_not_create_slots_or_reference_cycles(self):
        fixture = self.fixture()
        body = {"status": "error", "stdout": "OK", "exit_code": 1}
        event = fixture.rt.log.emit(type="team.tool_finished", task_id=fixture.task, actor=HOST,
            payload={"started_seq": 1, "run_id": "legacy", "role": fixture.manager,
                     "name": "workspace_run", "result": body})
        before = fixture.rt.log.read(task_id=fixture.task)
        saved = next(e for e in before if e.seq == event.seq)
        plain = saved.payload["result"]
        self.assertIs(type(plain), dict)
        self.assertEqual(plain, body)
        self.assertFalse(hasattr(plain, "receipt_seq"))
        self.assertEqual(json_utf8(copy.deepcopy(saved.payload)), json_utf8(saved.payload))
        io_fixture = ReferenceBridgeFixture()
        io_fixture.setUp()
        self.addCleanup(io_fixture.doCleanups)
        bridge, peer = io_fixture.session(plain)
        assert_wire(self, io_fixture.call(bridge, peer)["result"], plain, pointer=False)
        self.assertEqual(fixture.rt.log.read(task_id=fixture.task), before)

    def test_unrecorded_finish_cannot_return_reference_or_repeat_executor(self):
        fixture = self.fixture()
        context = self.context(fixture)
        role, _message, run = context
        log, team = fixture.rt.log, fixture.rt.team
        start = log.emit(type="team.tool_started", task_id=fixture.task, actor=HOST,
                         payload={**run.payload, "name": TOOL, "call_id": "failed-settlement", "arguments": {}})
        emit = log.emit
        def fail_finish(**kwargs):
            if kwargs["type"] == "team.tool_finished":
                raise OSError("Offline finish persistence failed")
            return emit(**kwargs)
        with patch.object(log, "emit", side_effect=fail_finish), \
                patch.object(team, "_execute", return_value={"status": "ok"}) as execute:
            with self.assertRaises(OSError):
                team._invoke_tool(fixture.task, role, run, run.payload["revision"],
                    ToolCall("failed-settlement", TOOL, {}), {"properties": {}, "required": []},
                    None, {}, start, None)
        execute.assert_called_once()
        self.assertEqual(fixture.events("team.tool_finished"), [])


if __name__ == "__main__":
    unittest.main()
