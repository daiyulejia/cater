"""Offline JSON encoding checks for tool replay and workspace audit evidence."""

import hashlib
import io
import json
import os
import socket
import sqlite3
import unittest
from copy import deepcopy
from unittest.mock import Mock, patch

from cater.anthropic import AnthropicMessagesDriver
from cater.drivers import DriverProtocolError, OpenAICompatibleDriver, _tool_api_request
from cater.team_context import history_window, json_bytes
from cater.tool_protocol import json_utf8
from cater.workspaces import WorkspaceService


TOOLS = [{"name": "workspace_run", "description": "Run a fixture", "input_schema": {
    "type": "object", "properties": {"args": {"type": "array", "items": {"type": "string"}}}}}]


def exchange(arguments, *, signed=False):
    call = {"id": "fixture-call", "name": "workspace_run", "arguments": arguments}
    assistant = {"role": "assistant", "content": "Checking", "tool_calls": [call]}
    if signed:
        assistant["provider_content"] = [
            {"type": "thinking", "thinking": "Private \u4e2d\ud800", "signature": "fixture-signature"},
            {"type": "redacted_thinking", "data": "fixture-opaque"},
            {"type": "text", "text": "Checking"},
            {"type": "tool_use", "id": call["id"], "name": call["name"], "input": arguments},
        ]
    return [assistant, {"role": "tool", "tool_call_id": call["id"],
                        "content": "Rejected before dispatch", "is_error": True}]


class ToolJsonEncodingTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(socket, "socket", side_effect=AssertionError("Real network forbidden")))

    def test_normal_bytes_and_hashes_match_previous_encoding(self):
        value = {"z": [None, True, 1, 1.5, "\u4e2d\u6587", "\U0001f600"], "a": "line\n\\\""}
        for sort_keys in (False, True):
            for allow_nan in (False, True):
                with self.subTest(sort_keys=sort_keys, allow_nan=allow_nan):
                    before = json.dumps(value, ensure_ascii=False, sort_keys=sort_keys,
                                        allow_nan=allow_nan).encode("utf-8")
                    encoded = json_utf8(value, sort_keys=sort_keys, allow_nan=allow_nan)
                    self.assertEqual(encoded, before)
                    self.assertEqual(hashlib.sha256(encoded).hexdigest(), hashlib.sha256(before).hexdigest())
                    self.assertEqual(json_bytes(value), len(before))

    def test_surrogates_in_keys_and_values_round_trip_without_replacement(self):
        value = {"key\ud800": ["\udfff", "\u4e2d\ud800\n", "literal \\ud800", "\U0001f600"]}
        before = deepcopy(value)
        encoded = json_utf8(value, sort_keys=True, allow_nan=False)
        self.assertTrue(encoded.isascii())
        self.assertEqual(json.loads(encoded.decode("utf-8")), value)
        self.assertEqual(value, before)
        self.assertEqual(json_bytes(value), len(json_utf8(value)))

    def test_nonfinite_policy_and_other_json_errors_are_preserved(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            body = {"bad_utf8": "\ud800", "number": value}
            with self.subTest(value=value), self.assertRaises(ValueError):
                json_utf8(body, allow_nan=False)
            self.assertEqual(json_utf8(body), json.dumps(body, ensure_ascii=True).encode("utf-8"))
        for value in (object(), {"bad_utf8": "\ud800", "unsupported": object()}):
            with self.assertRaises(TypeError):
                json_utf8(value)
        circular = []
        circular.append(circular)
        with self.assertRaises(ValueError):
            json_utf8(circular)

    def test_audit_shape_is_unchanged_for_normal_arguments_and_excludes_content_first(self):
        arguments = {"path": "\u4e2d\u6587.txt", "content": "PRIVATE_SOURCE\ud800", "expected_sha256": None}
        snapshot = deepcopy(arguments)
        public = WorkspaceService._public_arguments("workspace_write", arguments)
        self.assertEqual(public, {"path": "\u4e2d\u6587.txt", "expected_sha256": None})
        json.dumps(public, ensure_ascii=False).encode("utf-8")
        self.assertEqual(arguments, snapshot)

    def test_escaped_audit_wrapper_preserves_values_and_is_sqlite_safe_without_content(self):
        arguments = {"path": "\ud800", "content": "PRIVATE_SOURCE\udfff", "args": ["\udfff"],
                     "unknown\ud800": {"encoding": "escaped_json", "json": "untrusted"}}
        snapshot = deepcopy(arguments)
        public = WorkspaceService._public_arguments("workspace_write", arguments)
        self.assertEqual(set(public), {"encoding", "json"})
        self.assertEqual(public["encoding"], "escaped_json")
        self.assertTrue(public["json"].isascii())
        self.assertEqual(json.loads(public["json"]), {k: v for k, v in arguments.items() if k != "content"})
        encoded = json.dumps(public, ensure_ascii=False)
        self.assertNotIn("PRIVATE_SOURCE", encoded)
        self.assertNotIn('"content"', public["json"])
        encoded.encode("utf-8")
        with sqlite3.connect(":memory:") as database:
            self.addCleanup(database.close)
            database.execute("CREATE TABLE audit (payload TEXT)")
            database.execute("INSERT INTO audit VALUES (?)", (encoded,))
            stored, = database.execute("SELECT payload FROM audit").fetchone()
        self.assertEqual(json.loads(stored), public)
        self.assertEqual(arguments, snapshot)

    def test_history_size_and_eviction_preserve_signed_surrogate_blocks(self):
        history = exchange({"args": ["\ud800"]}, signed=True)
        snapshot = deepcopy(history)
        tail, info = history_window(history, [], tools=TOOLS, budget=1)
        self.assertEqual(tail, history)
        self.assertEqual(tail[0]["provider_content"], snapshot[0]["provider_content"])
        self.assertTrue(info["oversized_latest_group"])
        self.assertEqual(info["retained_json_bytes"], len(json_utf8(history)))
        self.assertEqual(history, snapshot)

    def request(self, provider, history):
        if provider is AnthropicMessagesDriver:
            response = {"type": "message", "role": "assistant", "stop_reason": "end_turn",
                        "content": [{"type": "text", "text": "done"}]}
            transport = "cater.anthropic._open_api_request"
        else:
            response = {"choices": [{"finish_reason": "stop",
                         "message": {"role": "assistant", "content": "done"}}]}
            transport = "cater.drivers._open_api_request"
        driver = provider("fixture", endpoint="https://fixture.invalid/v1/messages", model="fixture",
                          api_key_env="CATER_JSON_ENCODING_FIXTURE")
        with patch.dict(os.environ, {"CATER_JSON_ENCODING_FIXTURE": "synthetic-key"}), patch(
            transport, return_value=io.BytesIO(json.dumps(response).encode("utf-8"))
        ) as opened:
            turn = driver.tool_turn(role="worker", prompt="Fixture \u4e2d", history=history, tools=TOOLS)
        opened.assert_called_once()
        self.assertEqual(turn.text, "done")
        return opened.call_args.args[0].data

    def test_both_provider_http_payloads_round_trip_and_normal_bytes_are_unchanged(self):
        for provider in (AnthropicMessagesDriver, OpenAICompatibleDriver):
            for args in (["\u4e2d\u6587", "\U0001f600"], ["\ud800", "\u4e2d\udfff"]):
                with self.subTest(provider=provider.__name__, args=ascii(args)):
                    history = exchange({"args": args})
                    snapshot = deepcopy(history)
                    raw = self.request(provider, history)
                    body = json.loads(raw.decode("utf-8"))
                    if provider is AnthropicMessagesDriver:
                        self.assertEqual(body["messages"][1]["content"][1]["input"], {"args": args})
                    else:
                        field = body["messages"][2]["tool_calls"][0]["function"]["arguments"]
                        field.encode("utf-8")
                        self.assertEqual(json.loads(field), {"args": args})
                        self.assertEqual(field, json_utf8({"args": args}, allow_nan=False).decode("utf-8"))
                    if args[0] != "\ud800":
                        self.assertEqual(raw, json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8"))
                    self.assertEqual(history, snapshot)

    def test_anthropic_http_replays_all_signed_blocks_unchanged_after_json_parse(self):
        history = exchange({"args": ["\ud800"]}, signed=True)
        snapshot = deepcopy(history)
        body = json.loads(self.request(AnthropicMessagesDriver, history).decode("utf-8"))
        self.assertEqual(body["messages"][1]["content"], snapshot[0]["provider_content"])
        self.assertEqual(history, snapshot)

    def test_actual_tool_transport_still_rejects_nonfinite_numbers_before_http(self):
        for number in (float("nan"), float("inf"), float("-inf")):
            opener = Mock()
            with self.subTest(number=number), self.assertRaises(DriverProtocolError):
                _tool_api_request(endpoint="https://fixture.invalid/v1/messages",
                    body={"surrogate": "\ud800", "number": number}, headers={}, name="fixture",
                    timeout=7, opener=opener, byte_limit=128)
            opener.assert_not_called()


if __name__ == "__main__":
    unittest.main()
