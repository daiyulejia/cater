"""Native tool correction settings load locally without changing request options."""

import io
import json
import os
import socket
import subprocess
import tempfile
import unittest
from copy import deepcopy
from decimal import Decimal
from enum import IntEnum
from pathlib import Path
from unittest.mock import patch

from cater.anthropic import AnthropicMessagesDriver
from cater.drivers import OpenAICompatibleDriver, driver_from_dict, load_drivers


DRIVERS = {"openai_compatible": OpenAICompatibleDriver, "anthropic": AnthropicMessagesDriver}
CONFIG = {
    "name": "correction_fixture",
    "endpoint": "https://fixture.invalid/v1/messages",
    "model": "synthetic-model",
    "api_key_env": "CATER_CORRECTION_FIXTURE",
}


class IntSubclass(int):
    pass


class IntOption(IntEnum):
    ZERO = 0
    ONE = 1


class ToolCorrectionConfigTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(socket, "socket", side_effect=AssertionError("Real network forbidden")))
        self.enterContext(patch.object(subprocess, "Popen", side_effect=AssertionError("External process forbidden")))
        self.openers = [self.enterContext(patch(target, side_effect=AssertionError("Unexpected API request")))
                        for target in ("cater.drivers._open_api_request", "cater.anthropic._open_api_request")]

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

    def routes(self, kind):
        routes = ["constructor", "factory", "list_file", "object_file"]
        if kind == "anthropic":
            routes.append("from_config")
        return routes

    def assert_no_requests(self):
        for opener in self.openers:
            opener.assert_not_called()

    def test_default_and_explicit_values_in_all_loading_paths(self):
        for kind, cls in DRIVERS.items():
            for route in self.routes(kind):
                for options in ({}, {"tool_response_corrections": 0}, {"tool_response_corrections": 1}):
                    with self.subTest(kind=kind, route=route, options=options):
                        driver = self.load(kind, route, options)
                        self.assertIsInstance(driver, cls)
                        self.assertIs(type(driver.tool_response_corrections), int)
                        self.assertEqual(driver.tool_response_corrections, options.get("tool_response_corrections", 1))
                        self.assertEqual(driver.token_budget, None)
                        if kind == "anthropic":
                            self.assertEqual((driver.max_tokens, driver.timeout, driver.version, driver.thinking),
                                             (4096, 60.0, "2023-06-01", None))
                        else:
                            self.assertEqual((driver.max_tokens, driver.max_completion_tokens,
                                              driver.timeout, driver.temperature), (None, None, 120.0, None))
        self.assert_no_requests()

    def test_invalid_json_values_are_rejected_in_all_loading_paths(self):
        invalid = (True, False, -1, 2, 10**100, 0.0, 1.0, 0.5, "0", "1", "", None, [], {}, [1])
        for kind in DRIVERS:
            for route in self.routes(kind):
                for value in invalid:
                    with self.subTest(kind=kind, route=route, value=value):
                        with self.assertRaisesRegex(ValueError, "tool_response_corrections"):
                            self.load(kind, route, {"tool_response_corrections": value})
        self.assert_no_requests()

    def test_non_native_numbers_are_rejected_without_coercion(self):
        invalid = (IntSubclass(0), IntSubclass(1), IntOption.ZERO, IntOption.ONE,
                   Decimal(0), Decimal(1), float("nan"), float("inf"), (1,), {1}, b"1", object())
        for kind in DRIVERS:
            for route in self.routes(kind):
                if route.endswith("file"):
                    continue
                for value in invalid:
                    with self.subTest(kind=kind, route=route, value=value):
                        with self.assertRaisesRegex(ValueError, "tool_response_corrections"):
                            self.load(kind, route, {"tool_response_corrections": value})
        self.assert_no_requests()

    def test_other_drivers_do_not_gain_a_correction_default(self):
        configs = (
            {"kind": "scripted", "script": ["fixture"]},
            {"kind": "guided"},
            {"kind": "cli", "command": ["fixture-command"]},
            {"kind": "qoder_acp", "executable": "fixture-command", "model": "fixture"},
        )
        for config in configs:
            with self.subTest(kind=config["kind"]):
                driver = driver_from_dict({"name": "other_fixture", "token_budget": 300, **config})
                self.assertFalse(hasattr(driver, "tool_response_corrections"))
                self.assertEqual(driver.token_budget, 300)
        self.assert_no_requests()

    def test_existing_configuration_survives_all_loading_paths(self):
        cases = (
            ("openai_compatible", {"max_tokens": 123, "temperature": 0.25}),
            ("openai_compatible", {"max_completion_tokens": 234, "temperature": 0.75}),
            ("anthropic", {"max_tokens": 345, "thinking": "disabled", "version": "fixture-version"}),
        )
        for kind, extra in cases:
            for route in self.routes(kind):
                for correction in (0, 1):
                    options = {"timeout": 7, "token_budget": 900, **extra,
                               "tool_response_corrections": correction}
                    with self.subTest(kind=kind, route=route, options=options):
                        driver = self.load(kind, route, options)
                        for field, value in {**CONFIG, **options}.items():
                            self.assertEqual(getattr(driver, field), value)
        self.assert_no_requests()

    def test_mocked_requests_preserve_output_parameters_and_do_not_send_the_setting(self):
        cases = (
            ("openai_compatible", {"max_tokens": 123, "temperature": 0.25}),
            ("openai_compatible", {"max_completion_tokens": 234, "temperature": 0.75}),
            ("anthropic", {"max_tokens": 345, "thinking": "disabled", "version": "fixture-version"}),
        )
        for kind, extra in cases:
            for native in (False, True):
                baseline = None
                for correction in (0, 1):
                    with self.subTest(kind=kind, native=native, extra=extra, correction=correction):
                        driver = driver_from_dict({"kind": kind, **CONFIG, **extra, "timeout": 7,
                                                   "token_budget": 900, "tool_response_corrections": correction})
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
                            if native:
                                result = driver.tool_turn(role="worker", prompt="fixture", history=[], tools=[])
                            else:
                                result = driver.complete(role="worker", prompt="fixture", context={})
                        opened.assert_called_once()
                        self.assertEqual(opened.call_args.kwargs, {"timeout": 7})
                        request = opened.call_args.args[0]
                        payload = json.loads(request.data)
                        self.assertNotIn("tool_response_corrections", payload)
                        self.assertNotIn("token_budget", payload)
                        self.assertEqual(payload["model"], CONFIG["model"])
                        for field in ("max_tokens", "max_completion_tokens", "temperature"):
                            if field in extra:
                                self.assertEqual(payload[field], extra[field])
                            else:
                                self.assertNotIn(field, payload)
                        if kind == "anthropic":
                            self.assertEqual(payload["thinking"], {"type": "disabled"})
                            self.assertEqual(request.get_header("Anthropic-version"), extra["version"])
                        self.assertEqual((result.text, result.tokens), ("done", 25))
                        if baseline is None:
                            baseline = payload
                        else:
                            self.assertEqual(payload, baseline)
        self.assert_no_requests()
