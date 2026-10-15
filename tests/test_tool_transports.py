from __future__ import annotations

import io
import json
import os
import socket
import traceback
import unittest
from copy import deepcopy
from dataclasses import FrozenInstanceError
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from cater.anthropic import AnthropicMessagesDriver
from cater.drivers import (MAX_API_RESPONSE_BYTES, DriverProtocolError, DriverUnavailable,
                           OpenAICompatibleDriver, TextRequestRejected, TextTransportFailure, ToolRequestRejected,
                           _RejectRedirects)
from cater.runtime import _TEXT_ONLY_CALLS
from cater.tool_protocol import ToolCall, ToolTurn


DRIVERS = (AnthropicMessagesDriver, OpenAICompatibleDriver)
TOOLS = [{"name": "read_file", "description": "Read a workspace file", "input_schema": {
    "type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}]


def call(call_id="call_1", name="read_file", arguments=None):
    return {"id": call_id, "name": name, "arguments": {"path": "sample.txt"} if arguments is None else arguments}


def native_call(cls, value):
    if cls is AnthropicMessagesDriver:
        return {"type": "tool_use", "id": value["id"], "name": value["name"], "input": value["arguments"]}
    return {"type": "function", "id": value["id"], "function": {
        "name": value["name"], "arguments": json.dumps(value["arguments"])}}


def response(cls, *, calls=(), text="done", stop=None, usage=True):
    if cls is AnthropicMessagesDriver:
        data = {"type": "message", "role": "assistant", "stop_reason": stop or ("tool_use" if calls else "end_turn"),
                "content": ([{"type": "text", "text": text}] if text is not None else [])
                           + [native_call(cls, c) for c in calls]}
        if usage:
            data["usage"] = {"input_tokens": 20, "output_tokens": 5}
    else:
        message = {"role": "assistant", "content": text}
        if calls:
            message["tool_calls"] = [native_call(cls, c) for c in calls]
        data = {"choices": [{"message": message, "finish_reason": stop or ("tool_calls" if calls else "stop")}]}
        if usage:
            data["usage"] = {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25}
    return data


class ToolTransportTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {"CATER_TOOL_FIXTURE": "synthetic-secret"}))
        self.enterContext(patch.object(socket, "socket", side_effect=AssertionError("Real network forbidden")))

    def driver(self, cls, **options):
        return cls("fixture", endpoint="https://fixture.invalid/v1/messages", model="synthetic-model",
                   api_key_env="CATER_TOOL_FIXTURE", timeout=7, **options)

    def transport(self, cls):
        return "cater.anthropic._open_api_request" if cls is AnthropicMessagesDriver else "cater.drivers._open_api_request"

    def invoke(self, cls, data=None, *, raw=None, history=None, tools=None, driver=None):
        if raw is None:
            raw = json.dumps(response(cls) if data is None else data).encode()
        stream = io.BytesIO(raw)
        with patch(self.transport(cls), return_value=stream) as opened:
            turn = (driver or self.driver(cls)).tool_turn(role="worker", prompt="synthetic task",
                history=[] if history is None else history, tools=deepcopy(TOOLS) if tools is None else tools)
        opened.assert_called_once()
        request = opened.call_args.args[0]
        self.assertEqual(opened.call_args.kwargs, {"timeout": 7})
        self.assertEqual(request.method, "POST")
        self.assertNotIn("synthetic-secret", request.data.decode())
        return turn, request

    def assert_contained(self, turn, *, tokens=25):
        self.assertEqual(turn.calls, [])
        self.assertEqual(turn.text, "")
        self.assertEqual(turn.tokens, tokens)
        self.assertTrue(turn.meta["contained"])
        self.assertTrue(turn.meta["incomplete"])
        self.assertTrue(turn.meta["invocation_started"])
        self.assertEqual(turn.message, {"role": "assistant", "content": "", "tool_calls": []})

    def test_shared_types_are_frozen_and_message_repr_is_private(self):
        item = ToolCall("id", "read_file", {})
        turn = ToolTurn("visible", [item], message={"provider_content": "PRIVATE_SIGNATURE"})
        for obj, field, value in ((item, "name", "other"), (turn, "text", "other")):
            with self.assertRaises(FrozenInstanceError):
                setattr(obj, field, value)
        self.assertNotIn("PRIVATE", repr(turn))
        self.assertEqual((turn.tokens, turn.meta), (0, None))

    def test_native_parallel_transcript_round_trip(self):
        for cls in DRIVERS:
            with self.subTest(cls=cls.__name__):
                driver = self.driver(cls)
                first, request = self.invoke(cls, response(cls, calls=[call(), call("call_2")], text="checking"), driver=driver)
                body = json.loads(request.data)
                self.assertFalse(body["stream"])
                self.assertEqual(first.text, "checking")
                self.assertEqual(first.calls, [ToolCall(**call()), ToolCall(**call("call_2"))])
                self.assertFalse(first.meta["incomplete"])
                self.assertEqual(first.message["tool_calls"], [call(), call("call_2")])
                self.assertEqual((first.tokens, first.meta["token_usage"]), (25, "reported"))
                history = [first.message, {"role": "tool", "tool_call_id": "call_2", "content": "result two"},
                           {"role": "tool", "tool_call_id": "call_1", "content": "failed", "is_error": True}]
                snapshot = deepcopy(history)
                final, second_request = self.invoke(cls, history=history, driver=driver)
                self.assertEqual(history, snapshot)
                self.assertEqual((final.text, final.calls, final.message["content"]), ("done", [], "done"))
                second = json.loads(second_request.data)
                if cls is AnthropicMessagesDriver:
                    self.assertEqual(body["tools"], TOOLS)
                    self.assertIsInstance(body["system"], str)
                    self.assertIn("worker", body["system"])
                    self.assertEqual(body["max_tokens"], 4096)
                    self.assertEqual(request.get_header("X-api-key"), "synthetic-secret")
                    self.assertEqual(request.get_header("Anthropic-version"), "2023-06-01")
                    self.assertEqual(second["messages"], [
                        {"role": "user", "content": "synthetic task"},
                        {"role": "assistant", "content": first.message["provider_content"]},
                        {"role": "user", "content": [
                            {"type": "tool_result", "tool_use_id": "call_2", "content": "result two"},
                            {"type": "tool_result", "tool_use_id": "call_1", "content": "failed", "is_error": True}]}])
                else:
                    self.assertEqual(body["tools"], [{"type": "function", "function": {
                        "name": "read_file", "description": TOOLS[0]["description"], "parameters": TOOLS[0]["input_schema"]}}])
                    self.assertEqual(request.get_header("Authorization"), "Bearer synthetic-secret")
                    messages = second["messages"]
                    self.assertEqual([m["role"] for m in messages], ["system", "user", "assistant", "tool", "tool"])
                    self.assertIn("worker", messages[0]["content"])
                    self.assertEqual(messages[1], {"role": "user", "content": "synthetic task"})
                    self.assertEqual(messages[2]["tool_calls"], [native_call(cls, call()), native_call(cls, call("call_2"))])
                    self.assertEqual(messages[3], {"role": "tool", "tool_call_id": "call_2", "content": "result two"})
                    self.assertEqual(json.loads(messages[4]["content"]), {"is_error": True, "content": "failed"})

    def test_anthropic_signed_blocks_are_replayed_without_public_reasoning(self):
        data = response(AnthropicMessagesDriver, calls=[call()], text="visible")
        blocks = [{"type": "thinking", "thinking": "PRIVATE_THOUGHT", "signature": "PRIVATE_SIGNATURE"},
                  {"type": "redacted_thinking", "data": "PRIVATE_CIPHERTEXT"}] + data["content"]
        data["content"] = blocks
        turn, _ = self.invoke(AnthropicMessagesDriver, data)
        self.assertEqual(turn.text, "visible")
        self.assertEqual(turn.message["provider_content"], blocks)
        self.assertNotIn("PRIVATE", repr(turn))
        self.assertNotIn("PRIVATE", json.dumps(turn.meta))
        history = [turn.message, {"role": "tool", "tool_call_id": "call_1", "content": "result"}]
        _, request = self.invoke(AnthropicMessagesDriver, history=history)
        self.assertEqual(json.loads(request.data)["messages"][1]["content"], blocks)
        _, request = self.invoke(OpenAICompatibleDriver, history=history)
        self.assertNotIn("PRIVATE", request.data.decode())

    def test_anthropic_normalized_history_without_provider_blocks(self):
        history = [{"role": "assistant", "content": "checking", "tool_calls": [call()]},
                   {"role": "tool", "tool_call_id": "call_1", "content": "result", "is_error": False}]
        _, request = self.invoke(AnthropicMessagesDriver, history=history)
        messages = json.loads(request.data)["messages"]
        self.assertEqual(messages[1]["content"], [{"type": "text", "text": "checking"}, native_call(AnthropicMessagesDriver, call())])
        self.assertFalse(messages[2]["content"][0]["is_error"])

    def test_tool_only_turns_do_not_need_visible_text(self):
        for cls in DRIVERS:
            with self.subTest(cls=cls.__name__):
                turn, _ = self.invoke(cls, response(cls, calls=[call()], text=None))
                self.assertEqual(turn.text, "")
                self.assertEqual(len(turn.calls), 1)
                self.assertFalse(turn.meta["incomplete"])

    def test_multiple_tool_rounds_keep_transcript_order(self):
        for cls in DRIVERS:
            with self.subTest(cls=cls.__name__):
                history = []
                for number in range(2):
                    value = call(f"call_{number}")
                    turn, _ = self.invoke(cls, response(cls, calls=[value]), history=history)
                    history.append(turn.message)
                    history.append({"role": "tool", "tool_call_id": value["id"], "content": f"result {number}"})
                final, request = self.invoke(cls, history=history)
                self.assertEqual(final.text, "done")
                roles = [m["role"] for m in json.loads(request.data)["messages"]]
                self.assertEqual(roles, (["user", "assistant", "user", "assistant", "user"]
                    if cls is AnthropicMessagesDriver else ["system", "user", "assistant", "tool", "assistant", "tool"]))

    def test_openai_null_tool_calls_is_a_normal_text_response(self):
        data = response(OpenAICompatibleDriver)
        data["choices"][0]["message"]["tool_calls"] = None
        turn, _ = self.invoke(OpenAICompatibleDriver, data)
        self.assertEqual((turn.text, turn.calls, turn.meta["incomplete"]), ("done", [], False))

    def test_missing_and_invalid_usage_remain_unavailable(self):
        for cls in DRIVERS:
            bad_usage = ([None, {}, {"input_tokens": True, "output_tokens": 5},
                          {"input_tokens": 1, "output_tokens": -1},
                          {"input_tokens": 1, "output_tokens": 2, "cache_read_input_tokens": None}]
                         if cls is AnthropicMessagesDriver else
                         [None, {}, {"total_tokens": True}, {"total_tokens": -1}, {"total_tokens": 1.5},
                          {"prompt_tokens": 20, "completion_tokens": 5}])
            for usage in bad_usage:
                with self.subTest(cls=cls.__name__, usage=usage):
                    data = response(cls)
                    data["usage"] = usage
                    turn, _ = self.invoke(cls, data)
                    self.assertEqual(turn.tokens, 0)
                    self.assertEqual(turn.meta["token_usage"], "unavailable")

    def test_zero_and_cache_usage_are_reported_without_double_counting(self):
        for cls in DRIVERS:
            with self.subTest(cls=cls.__name__):
                data = response(cls)
                data["usage"] = {"input_tokens": 0, "output_tokens": 0} if cls is AnthropicMessagesDriver else {"total_tokens": 0}
                turn, _ = self.invoke(cls, data)
                self.assertEqual((turn.tokens, turn.meta["token_usage"]), (0, "reported"))
        data = response(AnthropicMessagesDriver)
        data["usage"].update(cache_creation_input_tokens=3, cache_read_input_tokens=7)
        turn, _ = self.invoke(AnthropicMessagesDriver, data)
        self.assertEqual(turn.tokens, 35)
        self.assertEqual(turn.meta["usage"], data["usage"])

    def test_truncated_or_illegal_stops_never_release_partial_calls_and_keep_usage(self):
        for cls in DRIVERS:
            stops = (["max_tokens", "pause_turn", "refusal", "model_context_window_exceeded", None, [], "PRIVATE_UNKNOWN"]
                     if cls is AnthropicMessagesDriver else ["length", "content_filter", "function_call", None, [], "PRIVATE_UNKNOWN"])
            for stop in stops:
                with self.subTest(cls=cls.__name__, stop=stop):
                    data = response(cls, calls=[call()], text="PRIVATE_PARTIAL")
                    if cls is AnthropicMessagesDriver:
                        data["stop_reason"] = stop
                        data["content"].append({"type": "tool_use"})
                    else:
                        data["choices"][0]["finish_reason"] = stop
                        data["choices"][0]["message"]["tool_calls"].append({"type": "function"})
                    turn, _ = self.invoke(cls, data)
                    self.assert_contained(turn)
                    self.assertEqual(turn.meta["usage"], data["usage"])
                    self.assertNotIn("PRIVATE", repr(turn))
                    self.assertNotIn("PRIVATE", repr(turn.meta))

    def test_inconsistent_stop_and_call_presence_are_contained(self):
        for cls in DRIVERS:
            final_stop, tool_stop = ("end_turn", "tool_use") if cls is AnthropicMessagesDriver else ("stop", "tool_calls")
            for data in (response(cls, calls=[call()], stop=final_stop), response(cls, stop=tool_stop), response(cls, text="")):
                with self.subTest(cls=cls.__name__, data=data):
                    turn, _ = self.invoke(cls, data)
                    self.assert_contained(turn)

    def test_invalid_calls_are_rejected_as_a_whole(self):
        invalid = [call(""), call(" "), call(7), call(name="unknown"), call(name="bad.name"),
                   call(name="a" * 65), call(arguments=[]), call(arguments="{}"), call(arguments=4)]
        for cls in DRIVERS:
            for bad in invalid + [call()]:
                with self.subTest(cls=cls.__name__, bad=bad):
                    data = response(cls, calls=[call(), bad])
                    turn, _ = self.invoke(cls, data)
                    self.assert_contained(turn)

    def test_reusing_an_id_from_history_is_contained(self):
        history = [{"role": "assistant", "content": "", "tool_calls": [call()]},
                   {"role": "tool", "tool_call_id": "call_1", "content": "result"}]
        for cls in DRIVERS:
            with self.subTest(cls=cls.__name__):
                turn, _ = self.invoke(cls, response(cls, calls=[call()]), history=history)
                self.assert_contained(turn)

    def test_openai_argument_json_is_strict_and_requires_an_object(self):
        for arguments in ('{"path":"a","path":"b"}', '{"nested":{"x":1,"x":2}}',
                          '{"x":NaN}', '{"x":Infinity}', '{"x":1e999}', '{', '[]', 'null', '"text"', 42, {}):
            with self.subTest(arguments=arguments):
                data = response(OpenAICompatibleDriver, calls=[call("good"), call("bad")])
                data["choices"][0]["message"]["tool_calls"][1]["function"]["arguments"] = arguments
                turn, _ = self.invoke(OpenAICompatibleDriver, data)
                self.assert_contained(turn)

    def test_outer_json_rejects_duplicate_keys_and_nonfinite_values(self):
        for cls in DRIVERS:
            data = response(cls, calls=[call()])
            raw = json.dumps(data).replace('"path": "sample.txt"', '"path": "one", "path": "two"').encode()
            # OpenAI argument text is escaped; test the outer object separately.
            cases = [b'{"usage":{},"usage":{}}', b'{"x":NaN}', b'{"x":1e999}']
            if cls is AnthropicMessagesDriver:
                cases.append(raw)
            for raw in cases:
                with self.subTest(cls=cls.__name__, raw=raw), self.assertRaises(DriverProtocolError):
                    self.invoke(cls, raw=raw)

    def test_provider_server_tools_and_legacy_calls_are_unsupported(self):
        for kind in ("server_tool_use", "web_search_tool_result", "bash_code_execution_tool_result", "tool_result"):
            data = response(AnthropicMessagesDriver, calls=[call()])
            data["content"].append({"type": kind, "id": "server_1", "name": "web_search", "input": {}})
            turn, _ = self.invoke(AnthropicMessagesDriver, data)
            self.assert_contained(turn)
        for kind in ("custom", "web_search", "computer_use"):
            data = response(OpenAICompatibleDriver, calls=[call(), call("other")])
            data["choices"][0]["message"]["tool_calls"][1]["type"] = kind
            turn, _ = self.invoke(OpenAICompatibleDriver, data)
            self.assert_contained(turn)
        data = response(OpenAICompatibleDriver)
        data["choices"][0]["message"]["function_call"] = {"name": "read_file", "arguments": "{}"}
        turn, _ = self.invoke(OpenAICompatibleDriver, data)
        self.assert_contained(turn)

    def test_invalid_tool_declarations_fail_before_http(self):
        schemas = [[{"type": "web_search_20250305", "name": "web_search"}],
                   [{"type": "web_search", **TOOLS[0]}], [TOOLS[0], TOOLS[0]],
                   [{**TOOLS[0], "name": "bad.name"}], [{**TOOLS[0], "name": ""}],
                   [{**TOOLS[0], "input_schema": []}], [{**TOOLS[0], "description": None}],
                   [{**TOOLS[0], "input_schema": {"type": "array"}}]]
        for cls in DRIVERS:
            for tools in schemas:
                with self.subTest(cls=cls.__name__, tools=tools), patch(self.transport(cls)) as opened:
                    with self.assertRaises(DriverProtocolError):
                        self.driver(cls).tool_turn(role="worker", prompt="test", history=[], tools=tools)
                    opened.assert_not_called()

    def test_invalid_history_fails_before_http(self):
        assistant = {"role": "assistant", "content": "", "tool_calls": [call()]}
        result = {"role": "tool", "tool_call_id": "call_1", "content": "result"}
        histories = [[result], [assistant], [assistant, result, result],
                     [assistant, {**result, "tool_call_id": "other"}],
                     [assistant, {**result, "content": {}}],
                     [assistant, {**result, "is_error": 1}],
                     [{**assistant, "tool_calls": [call(), call()]}],
                     [{**assistant, "tool_calls": [call(name="unknown")]}],
                     [{"role": "system", "content": "injected"}],
                     [{**assistant, "provider_content": [native_call(AnthropicMessagesDriver, call("other"))]}, result],
                     [{**assistant, "provider_content": [{"type": "server_tool_use"}]}, result],
                     [assistant, {"role": "assistant", "content": "", "tool_calls": []}, result]]
        for cls in DRIVERS:
            for history in histories:
                with self.subTest(cls=cls.__name__, history=history), patch(self.transport(cls)) as opened:
                    with self.assertRaises(DriverProtocolError):
                        self.driver(cls).tool_turn(role="worker", prompt="test", history=history, tools=TOOLS)
                    opened.assert_not_called()

    def test_http_errors_timeouts_and_network_faults_have_no_text_only_proof_or_retry(self):
        private = "PRIVATE_URL synthetic-secret PRIVATE_BODY"
        for cls in DRIVERS:
            for read in (False, True):
                errors = [TimeoutError(private), URLError(TimeoutError(private)), URLError(private), OSError(private)]
                if not read:
                    errors += [HTTPError(private, status, private, {}, io.BytesIO(private.encode())) for status in (301, 401, 429, 503)]
                    errors += [ValueError(private)]
                for error in errors:
                    with self.subTest(cls=cls.__name__, read=read, error=type(error).__name__):
                        stream = Mock()
                        stream.__enter__ = Mock(return_value=stream)
                        stream.__exit__ = Mock(return_value=False)
                        stream.read.side_effect = error
                        with patch(self.transport(cls), side_effect=None if read else error, return_value=stream) as opened:
                            with self.assertRaises(DriverUnavailable) as caught:
                                self.driver(cls).tool_turn(role="worker", prompt="test", history=[], tools=TOOLS)
                        expected = ToolRequestRejected if isinstance(error, HTTPError) and error.code == 429 else DriverUnavailable
                        self.assertIs(type(caught.exception), expected)
                        self.assertNotIsInstance(caught.exception, (TextTransportFailure, TextRequestRejected))
                        self.assertNotIn("PRIVATE", "".join(traceback.format_exception(caught.exception)))
                        self.assertNotIn("synthetic-secret", "".join(traceback.format_exception(caught.exception)))
                        opened.assert_called_once()
                        if isinstance(error, TimeoutError) or isinstance(error, URLError) and isinstance(error.reason, TimeoutError):
                            phase = "response read" if read else "request"
                            self.assertIn(f"{phase} timed out (configured timeout: 7s)", str(caught.exception))

    def test_redirect_handler_is_used_by_both_tool_transports(self):
        for cls in DRIVERS:
            with self.subTest(cls=cls.__name__):
                def make_opener(handler):
                    self.assertIsInstance(handler, _RejectRedirects)
                    def open_request(request, **kwargs):
                        handler.redirect_request(request, None, 307, "redirect", {}, "https://other.invalid")
                    return Mock(open=Mock(side_effect=open_request))
                with patch("cater.drivers.build_opener", side_effect=make_opener) as build:
                    with self.assertRaises(DriverUnavailable) as caught:
                        self.driver(cls).tool_turn(role="worker", prompt="test", history=[], tools=TOOLS)
                self.assertIs(type(caught.exception), DriverUnavailable)
                self.assertIn("HTTP 307", str(caught.exception))
                build.assert_called_once()

    def test_response_size_and_json_protocol_errors_are_sanitized(self):
        for cls in DRIVERS:
            for raw in (b"PRIVATE_BODY", b"\xff", b"[]", b"{}", b"null", b"{" + b" " * MAX_API_RESPONSE_BYTES):
                with self.subTest(cls=cls.__name__, size=len(raw)):
                    with self.assertRaises(DriverProtocolError) as caught:
                        self.invoke(cls, raw=raw)
                    self.assertNotIn("PRIVATE", "".join(traceback.format_exception(caught.exception)))
            stream = Mock()
            stream.__enter__ = Mock(return_value=stream)
            stream.__exit__ = Mock(return_value=False)
            stream.read.return_value = json.dumps(response(cls)).encode()
            with patch(self.transport(cls), return_value=stream):
                self.driver(cls).tool_turn(role="worker", prompt="test", history=[], tools=TOOLS)
            stream.read.assert_called_once_with(MAX_API_RESPONSE_BYTES + 1)

    def test_malformed_response_blocks_never_expose_partial_work(self):
        for blocks in (None, [None], [{"type": "text", "text": 1}],
                       [{"type": "thinking", "thinking": "PRIVATE", "signature": 1}]):
            data = response(AnthropicMessagesDriver)
            data["content"] = blocks
            turn, _ = self.invoke(AnthropicMessagesDriver, data)
            self.assert_contained(turn)
        for content in ([], {}, 3):
            data = response(OpenAICompatibleDriver)
            data["choices"][0]["message"]["content"] = content
            turn, _ = self.invoke(OpenAICompatibleDriver, data)
            self.assert_contained(turn)

    def test_no_key_and_empty_tools_do_not_enable_tools(self):
        for cls in DRIVERS:
            with self.subTest(cls=cls.__name__):
                turn, request = self.invoke(cls, tools=[])
                self.assertNotIn("tools", json.loads(request.data))
                self.assertNotIn("tool_choice", json.loads(request.data))
                self.assertEqual(turn.calls, [])
                turn, _ = self.invoke(cls, response(cls, calls=[call()]), tools=[])
                self.assert_contained(turn)
                with patch.dict(os.environ, {}, clear=True), patch(self.transport(cls)) as opened:
                    with self.assertRaises(DriverUnavailable) as caught:
                        self.driver(cls).tool_turn(role="worker", prompt="test", history=[], tools=TOOLS)
                    self.assertIs(type(caught.exception), DriverUnavailable)
                    opened.assert_not_called()

    def test_tool_turn_faults_without_declarations_still_have_no_text_only_proof(self):
        for cls in DRIVERS:
            for error in (TimeoutError("private"), HTTPError("https://fixture.invalid", 429, "private", {}, None)):
                with self.subTest(cls=cls.__name__, error=type(error).__name__), patch(self.transport(cls), side_effect=error) as opened:
                    with self.assertRaises(DriverUnavailable) as caught:
                        self.driver(cls).tool_turn(role="worker", prompt="test", history=[], tools=[])
                    expected = ToolRequestRejected if isinstance(error, HTTPError) and error.code == 429 else DriverUnavailable
                    self.assertIs(type(caught.exception), expected)
                    self.assertNotIsInstance(caught.exception, (TextTransportFailure, TextRequestRejected))
                    opened.assert_called_once()

    def test_invalid_direct_endpoint_and_timeout_fail_with_driver_error(self):
        for field, value in (("endpoint", "https://[PRIVATE"), ("endpoint", "http://external.invalid"),
                             ("endpoint", "https://user:PRIVATE@fixture.invalid"),
                             ("timeout", True), ("timeout", -1), ("timeout", float("nan")), ("timeout", 10 ** 400)):
            with self.subTest(field=field, value=value), patch(self.transport(OpenAICompatibleDriver)) as opened:
                driver = self.driver(OpenAICompatibleDriver)
                setattr(driver, field, value)
                with self.assertRaises(DriverProtocolError) as caught:
                    driver.tool_turn(role="worker", prompt="test", history=[], tools=TOOLS)
                self.assertNotIn("PRIVATE", "".join(traceback.format_exception(caught.exception)))
                opened.assert_not_called()

    def test_complete_retains_text_semantics_and_original_method_identity(self):
        for cls in DRIVERS:
            with self.subTest(cls=cls.__name__):
                driver = self.driver(cls)
                self.assertIs(_TEXT_ONLY_CALLS[cls], cls.complete)
                self.invoke(cls, response(cls, calls=[call()]), driver=driver)
                with patch(self.transport(cls), return_value=io.BytesIO(json.dumps(response(cls)).encode())) as opened:
                    answer = driver.complete(role="worker", prompt="plain chat", context={"tools": TOOLS})
                body = json.loads(opened.call_args.args[0].data)
                self.assertEqual(answer.text, "done")
                self.assertEqual(answer.tokens, 25)
                self.assertNotIn("tools", body)
                self.assertNotIn("tool_choice", body)
                self.assertTrue(all(m["role"] in {"system", "user"} and isinstance(m["content"], str) for m in body["messages"]))
                for error, expected in ((TimeoutError("private"), TextTransportFailure),
                                        (HTTPError("https://fixture.invalid", 429, "private", {}, None), TextRequestRejected)):
                    with patch(self.transport(cls), side_effect=error) as opened:
                        with self.assertRaises(expected):
                            driver.complete(role="worker", prompt="plain chat", context={})
                        opened.assert_called_once()


if __name__ == "__main__":
    unittest.main()
