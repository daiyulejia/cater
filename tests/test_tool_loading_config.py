"""Explicit local tool loading modes never become remote request options."""

import io
import json
import os
import socket
import subprocess
import tempfile
import unittest
from copy import deepcopy
from enum import Enum
from pathlib import Path
from unittest.mock import patch

from cater.anthropic import AnthropicMessagesDriver
from cater.drivers import OpenAICompatibleDriver, driver_from_dict, load_drivers


DRIVERS = {"openai_compatible": OpenAICompatibleDriver, "anthropic": AnthropicMessagesDriver}
CONFIG = {
    "name": "loading_fixture",
    "endpoint": "https://fixture.invalid/v1/messages",
    "model": "synthetic-model",
    "api_key_env": "CATER_LOADING_FIXTURE",
}


class StringSubclass(str):
    pass


class StringOption(str, Enum):
    ALL = "all"
    ON_DEMAND = "on_demand"


class ToolLoadingConfigTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(socket, "socket", side_effect=AssertionError("Real network forbidden")))
        self.enterContext(patch.object(subprocess, "Popen", side_effect=AssertionError("External process forbidden")))
        self.openers = [self.enterContext(patch(target, side_effect=AssertionError("Unexpected API request")))
                        for target in ("cater.drivers._open_api_request", "cater.anthropic._open_api_request")]

    def routes(self, kind):
        routes = ["constructor", "factory", "list_file", "object_file"]
        if kind == "anthropic":
            routes.append("from_config")
        return routes

    def load(self, kind, route, options):
        config = {"kind": kind, **CONFIG, **options}
        snapshot = deepcopy(config)
        if route == "constructor":
            driver = DRIVERS[kind](**CONFIG, **options)
        elif route == "from_config":
            driver = AnthropicMessagesDriver.from_config(CONFIG["name"], config)
        elif route == "factory":
            driver = driver_from_dict(config)
        else:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "drivers.json"
                items = [config]
                raw = json.dumps(items if route == "list_file" else {"drivers": items})
                path.write_text(raw, encoding="utf-8")
                try:
                    driver = load_drivers(path)[CONFIG["name"]]
                finally:
                    self.assertEqual(path.read_text(encoding="utf-8"), raw)
        self.assertEqual(config, snapshot)
        return driver

    def assert_no_requests(self):
        for opener in self.openers:
            opener.assert_not_called()

    def test_default_and_explicit_modes_in_all_loading_paths(self):
        for kind, cls in DRIVERS.items():
            for route in self.routes(kind):
                for options in ({}, {"tool_loading": "on_demand"}, {"tool_loading": "all"}):
                    with self.subTest(kind=kind, route=route, options=options):
                        driver = self.load(kind, route, options)
                        self.assertIsInstance(driver, cls)
                        self.assertIs(type(driver.tool_loading), str)
                        self.assertEqual(driver.tool_loading, options.get("tool_loading", "all"))
                        self.assertIsNone(driver.token_budget)
                        self.assertEqual(driver.tool_response_corrections, 1)
                        if kind == "anthropic":
                            self.assertEqual((driver.max_tokens, driver.timeout, driver.version, driver.thinking),
                                             (4096, 60.0, "2023-06-01", None))
                        else:
                            self.assertEqual((driver.max_tokens, driver.max_completion_tokens,
                                              driver.timeout, driver.temperature), (None, None, 120.0, None))
        self.assert_no_requests()

    def test_invalid_json_modes_are_rejected_in_all_loading_paths(self):
        invalid = (None, True, False, 0, 1, -1, 0.0, [], ["all"], {}, {"mode": "all"},
                   "", "unknown", "ALL", "ON_DEMAND", " all", "all ", "on-demand", "on_demand\n")
        for kind in DRIVERS:
            for route in self.routes(kind):
                for value in invalid:
                    with self.subTest(kind=kind, route=route, value=value):
                        with self.assertRaisesRegex(ValueError, "^tool_loading"):
                            self.load(kind, route, {"tool_loading": value})
        self.assert_no_requests()

    def test_non_exact_strings_and_non_json_values_are_rejected(self):
        invalid = (StringSubclass("all"), StringSubclass("on_demand"),
                   StringOption.ALL, StringOption.ON_DEMAND, b"all", ("all",), {"all"})
        for kind in DRIVERS:
            for route in self.routes(kind):
                if route.endswith("file"):
                    continue
                for value in invalid:
                    with self.subTest(kind=kind, route=route, value=value):
                        with self.assertRaisesRegex(ValueError, "^tool_loading"):
                            self.load(kind, route, {"tool_loading": value})
        self.assert_no_requests()

    def test_explicit_modes_preserve_existing_configuration(self):
        cases = (
            ("openai_compatible", {"max_tokens": 123, "temperature": 0.25}),
            ("openai_compatible", {"max_completion_tokens": 234, "temperature": 0.75}),
            ("anthropic", {"max_tokens": 345, "thinking": "disabled", "version": "fixture-version"}),
        )
        for kind, extra in cases:
            for route in self.routes(kind):
                for mode in ("all", "on_demand"):
                    options = {"timeout": 7, "token_budget": 900, "tool_response_corrections": 0,
                               **extra, "tool_loading": mode}
                    with self.subTest(kind=kind, route=route, mode=mode):
                        driver = self.load(kind, route, options)
                        for field, value in {**CONFIG, **options}.items():
                            self.assertEqual(getattr(driver, field), value)
        self.assert_no_requests()

    def test_mocked_requests_do_not_send_local_mode_in_body_or_headers(self):
        cases = (
            ("openai_compatible", {"max_tokens": 123, "temperature": 0.25}),
            ("openai_compatible", {"max_completion_tokens": 234, "temperature": 0.75}),
            ("anthropic", {"max_tokens": 345, "thinking": "disabled", "version": "fixture-version"}),
        )
        definition = {"name": "fixture_tool", "description": "Synthetic tool", "input_schema": {
            "type": "object", "properties": {}, "additionalProperties": False}}
        for kind, extra in cases:
            for route in ("complete", "empty_tools", "tools"):
                baseline = None
                for options in ({}, {"tool_loading": "all"}, {"tool_loading": "on_demand"}):
                    with self.subTest(kind=kind, route=route, options=options):
                        driver = driver_from_dict({"kind": kind, **CONFIG, **extra, "timeout": 7,
                                                   "token_budget": 900, **options})
                        if kind == "anthropic":
                            data = {"type": "message", "role": "assistant", "stop_reason": "end_turn",
                                    "content": [{"type": "text", "text": "done"}],
                                    "usage": {"input_tokens": 20, "output_tokens": 5}}
                            target = "cater.anthropic._open_api_request"
                        else:
                            data = {"choices": [{"finish_reason": "stop", "message": {
                                "role": "assistant", "content": "done"}}],
                                "usage": {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25}}
                            target = "cater.drivers._open_api_request"
                        with patch.dict(os.environ, {CONFIG["api_key_env"]: "synthetic-only"}), patch(
                            target, return_value=io.BytesIO(json.dumps(data).encode("utf-8"))
                        ) as opened:
                            if route == "complete":
                                result = driver.complete(role="worker", prompt="fixture", context={})
                            else:
                                result = driver.tool_turn(role="worker", prompt="fixture", history=[],
                                                          tools=[definition] if route == "tools" else [])
                        opened.assert_called_once()
                        self.assertEqual(opened.call_args.kwargs, {"timeout": 7})
                        request = opened.call_args.args[0]
                        payload = json.loads(request.data)
                        headers = dict(request.header_items())
                        self.assertNotIn("tool_loading", payload)
                        self.assertFalse(any("tool_loading" in name.lower().replace("-", "_")
                                             for name in headers))
                        self.assertEqual(payload["model"], CONFIG["model"])
                        for field in ("max_tokens", "max_completion_tokens", "temperature"):
                            if field in extra:
                                self.assertEqual(payload[field], extra[field])
                            else:
                                self.assertNotIn(field, payload)
                        if kind == "anthropic":
                            self.assertEqual(payload["thinking"], {"type": "disabled"})
                            self.assertEqual(request.get_header("Anthropic-version"), extra["version"])
                        self.assertEqual(bool(payload.get("tools")), route == "tools")
                        self.assertEqual((result.text, result.tokens), ("done", 25))
                        self.assertEqual(result.meta["token_usage"], "reported")
                        observed = (request.full_url, request.method, payload, headers, result)
                        if baseline is None:
                            baseline = observed
                        else:
                            self.assertEqual(observed, baseline)
        self.assert_no_requests()
