from __future__ import annotations

import io
import json
import os
import tempfile
import traceback
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from cater.anthropic import AnthropicMessagesDriver
from cater.availability import team_availability
from cater.drivers import DriverProtocolError, DriverUnavailable, load_drivers
from cater.runtime import Runtime


def provider_message(**fields):
    return {"type": "message", "content": [{"type": "text", "text": '{"reply":"你好"}'}],
            "stop_reason": "end_turn", "usage": {"input_tokens": 20, "output_tokens": 5}, **fields}


class AnthropicDriverTests(unittest.TestCase):
    def driver(self, **fields):
        return AnthropicMessagesDriver("anthropic-test", endpoint="https://provider.test/v1/messages",
                                        model="fixture", api_key_env="CATER_ANTHROPIC_FIXTURE", **fields)

    def complete(self, data, **fields):
        with patch.dict(os.environ, {"CATER_ANTHROPIC_FIXTURE": "fixture-secret"}), patch(
            "cater.anthropic._open_api_request", return_value=io.BytesIO(json.dumps(data).encode())
        ):
            return self.driver(**fields).complete(role="worker", prompt="a synthetic request", context={})

    def test_messages_transport_uses_top_level_system_and_header_key(self):
        captured = []

        def open_request(request, **options):
            captured.append((request, options))
            return io.BytesIO(json.dumps(provider_message()).encode())

        with patch.dict(os.environ, {"CATER_ANTHROPIC_FIXTURE": "fixture-secret"}), patch(
            "cater.anthropic._open_api_request", side_effect=open_request
        ):
            response = self.driver(max_tokens=123, timeout=8).complete(role="猫咖店长", prompt="synthetic", context={})
        request, options = captured[0]
        body = json.loads(request.data)
        self.assertEqual(body["messages"], [{"role": "user", "content": "synthetic"}])
        self.assertIsInstance(body["system"], str)
        self.assertFalse(body["stream"])
        self.assertEqual((body["model"], body["max_tokens"]), ("fixture", 123))
        self.assertEqual(request.get_header("X-api-key"), "fixture-secret")
        self.assertEqual(request.get_header("Anthropic-version"), "2023-06-01")
        self.assertNotIn("fixture-secret", request.data.decode())
        self.assertEqual(options["timeout"], 8)
        self.assertEqual(response.tokens, 25)
        self.assertEqual(response.meta["token_usage"], "reported")

    def test_text_is_separate_from_hidden_thinking_and_cache_usage_is_counted(self):
        response = self.complete(provider_message(
            content=[{"type": "thinking", "thinking": "PRIVATE_THOUGHT"},
                     {"type": "redacted_thinking", "data": "PRIVATE_DATA"},
                     {"type": "text", "text": "hello "}, {"type": "text", "text": "world"}],
            usage={"input_tokens": 20, "output_tokens": 5, "cache_creation_input_tokens": 3,
                   "cache_read_input_tokens": 7},
        ))
        self.assertEqual(response.text, "hello world")
        self.assertEqual(response.tokens, 35)
        self.assertNotIn("PRIVATE", repr(response))

    def test_thinking_is_omitted_by_default_and_disabled_only_when_configured(self):
        for thinking in (None, "disabled"):
            with self.subTest(thinking=thinking), patch.dict(
                os.environ, {"CATER_ANTHROPIC_FIXTURE": "fixture-secret"}
            ), patch("cater.anthropic._open_api_request", return_value=io.BytesIO(json.dumps(provider_message()).encode())) as opened:
                self.driver(thinking=thinking).complete(role="worker", prompt="test", context={})
                payload = json.loads(opened.call_args.args[0].data)
                if thinking is None:
                    self.assertNotIn("thinking", payload)
                else:
                    self.assertEqual(payload["thinking"], {"type": "disabled"})

    def test_missing_or_invalid_usage_is_unknown_instead_of_reported_zero(self):
        for usage in (None, {}, {"input_tokens": True, "output_tokens": 3},
                      {"input_tokens": 3, "output_tokens": -1},
                      {"input_tokens": 3, "output_tokens": 4, "cache_read_input_tokens": None}):
            with self.subTest(usage=usage):
                result = self.complete(provider_message(usage=usage))
                self.assertEqual(result.meta["token_usage"], "unavailable")
                self.assertEqual(result.tokens, 0)

    def test_partial_and_tool_responses_are_contained_with_usage_preserved(self):
        for reason, blocks in (("max_tokens", [{"type": "text", "text": '{"reply":"truncated"}'}]),
                               ("tool_use", [{"type": "tool_use", "input": {"command": "bad"}}]),
                               ("end_turn", [{"type": "thinking", "thinking": "private"}]),
                               (None, [{"type": "text", "text": "text"}]),
                               ([], [{"type": "text", "text": "text"}])):
            with self.subTest(reason=reason):
                result = self.complete(provider_message(stop_reason=reason, content=blocks))
                self.assertTrue(result.meta["contained"])
                self.assertTrue(result.meta["invocation_started"])
                self.assertEqual(result.tokens, 25)
                self.assertNotIn("truncated", result.text)
                self.assertNotIn("private", result.text)

    def test_no_key_means_no_request(self):
        with patch.dict(os.environ, {}, clear=True), patch("cater.anthropic._open_api_request") as request:
            with self.assertRaises(DriverUnavailable):
                self.driver().complete(role="worker", prompt="test", context={})
            request.assert_not_called()

    def test_http_and_header_errors_do_not_expose_body_or_credentials(self):
        errors = [HTTPError("https://provider.test", 401, "fixture-secret", {}, io.BytesIO(b"fixture-secret")),
                  ValueError("header contains fixture-secret"), TimeoutError("fixture-secret")]
        for error in errors:
            with self.subTest(error=type(error).__name__), patch.dict(
                os.environ, {"CATER_ANTHROPIC_FIXTURE": "fixture-secret"}
            ), patch("cater.anthropic._open_api_request", side_effect=error):
                with self.assertRaises(DriverUnavailable) as caught:
                    self.driver().complete(role="worker", prompt="test", context={})
                self.assertNotIn("fixture-secret", str(caught.exception))
                self.assertNotIn("fixture-secret", "".join(traceback.format_exception(caught.exception)))

    def test_wrong_protocol_and_oversized_response_are_rejected(self):
        for data in ({"choices": [{"message": {"content": "other protocol"}}]}, [],
                     provider_message(content="text"), provider_message(content=[{"type": "text", "text": 123}])):
            with self.subTest(data=data), self.assertRaises(DriverProtocolError):
                self.complete(data)
        with patch("cater.anthropic.MAX_API_RESPONSE_BYTES", 10), self.assertRaises(DriverProtocolError):
            self.complete(provider_message())

    def test_factory_loads_kind_and_rejects_invalid_configuration(self):
        config = {"kind": "anthropic", "name": "intern", "endpoint": "https://provider.test/v1/messages",
                  "model": "fixture", "api_key_env": "CATER_ANTHROPIC_FIXTURE"}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "drivers.json"
            path.write_text(json.dumps({"drivers": [config]}), encoding="utf-8")
            driver = load_drivers(path)["intern"]
            self.assertIsInstance(driver, AnthropicMessagesDriver)
            self.assertEqual(driver.max_tokens, 4096)
        for changes in ({"endpoint": "http://external.test/v1/messages"},
                        {"endpoint": "https://user:secret@provider.test/v1/messages"}, {"model": " "},
                        {"api_key_env": "bad\nname"}, {"timeout": float("nan")}, {"timeout": True},
                        {"max_tokens": True}, {"max_tokens": 0}, {"version": "bad\nheader"}, {"token_budget": True}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                AnthropicMessagesDriver.from_config("intern", {**config, **changes})

    def test_team_availability_recognizes_anthropic_missing_key_without_probing(self):
        with Runtime(":memory:", drivers={"product": self.driver()}) as rt, patch.dict(os.environ, {}, clear=True), patch(
            "cater.anthropic._open_api_request", side_effect=AssertionError("must not probe")
        ):
            members = team_availability(rt)["members"]
            self.assertEqual(next(m for m in members if m["role_id"] == "product")["reason"], "missing_key")
