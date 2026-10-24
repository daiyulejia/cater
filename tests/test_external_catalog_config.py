"""Deterministic external surface configuration and legacy fingerprint checks."""

import contextlib
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

if __name__ == "__main__":
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cater.drivers import driver_from_dict, load_drivers
from cater.external_agent import QoderAcpDriver
from cater.external_catalog import validate_surface
from cater.team_external import driver_fingerprint
from cater.tool_protocol import ToolCall


SURFACE_ERROR = "tool_surface must be all or catalog"
LEGACY_DEFAULT_HASH = "bce560bebcb80f8555af43dc7e75a9a5bd6bbc942be9e4fcb2954908ec236cbd"


class StringSubclass(str):
    pass


def legacy_fingerprint_data(driver):
    # Independently retain the pre-catalog field set and JSON encoding.
    return {"implementation": type(driver).__module__ + "." + type(driver).__qualname__,
            "command": getattr(driver, "command", None), "model": getattr(driver, "model", None),
            "execution_profile": getattr(driver, "execution_profile", None),
            "timeout": getattr(driver, "timeout", None),
            "max_run_seconds": getattr(driver, "max_run_seconds", None)}


def fingerprint(data):
    return hashlib.sha256(json.dumps(
        data, ensure_ascii=False, sort_keys=True, allow_nan=False
    ).encode("utf-8")).hexdigest()


class ExternalCatalogConfigTests(unittest.TestCase):
    def setUp(self):
        for target in ("subprocess.Popen", "os.system", "socket.socket", "socket.create_connection",
                       "socket.getaddrinfo", "cater.external_agent.AcpSession",
                       "cater.external_agent.McpToolBridge"):
            guard = self.enterContext(patch(target, side_effect=AssertionError("Offline test: " + target)))
            self.addCleanup(guard.assert_not_called)
        self.config = {"kind": "qoder_acp", "name": "worker", "executable": "never-launch-qoder", "model": "ExactModel"}

    def load(self, route, options):
        config = {**deepcopy(self.config), **deepcopy(options)}
        before = deepcopy(config)
        try:
            if route == "direct":
                kwargs = {key: value for key, value in config.items() if key != "kind"}
                return QoderAcpDriver(**kwargs)
            if route == "factory":
                return driver_from_dict(config)
            raw = json.dumps([config] if route == "list" else {"drivers": [config]})
            # Exercise the real JSON loader without writing extra fixture files.
            with patch.object(Path, "read_text", return_value=raw) as read:
                drivers = load_drivers(Path("not-created-drivers.json"))
            read.assert_called_once_with(encoding="utf-8")
            self.assertEqual(list(drivers), ["worker"])
            return drivers["worker"]
        finally:
            self.assertEqual(config, before)

    def test_validate_surface_accepts_only_exact_supported_plain_strings(self):
        for value in ("all", "catalog"):
            with self.subTest(value=value):
                self.assertIs(validate_surface(value), value)
        for value in (StringSubclass("all"), StringSubclass("catalog"), b"all", ("catalog",)):
            with self.subTest(value=value), self.assertRaises(ValueError) as caught:
                validate_surface(value)
            self.assertEqual(str(caught.exception), SURFACE_ERROR)

    def test_invalid_modes_have_fixed_non_echoing_error_across_routes(self):
        values = (None, True, False, 0, 1, [], {}, "", "ALL", "Catalog", " all", "catalog ",
                  "on_demand", "PRIVATE_CATALOG_SECRET")
        for value in values:
            for route in ("direct", "factory", "list", "object"):
                with self.subTest(value=value, route=route):
                    output, errors = io.StringIO(), io.StringIO()
                    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                        with self.assertRaises(ValueError) as caught:
                            self.load(route, {"tool_surface": value})
                    self.assertEqual(str(caught.exception), SURFACE_ERROR)
                    self.assertEqual((output.getvalue(), errors.getvalue()), ("", ""))

    def test_nonplain_strings_reject_in_direct_and_factory_routes(self):
        for route in ("direct", "factory"):
            for value in (StringSubclass("all"), StringSubclass("catalog")):
                with self.subTest(route=route, value=value), self.assertRaises(ValueError) as caught:
                    self.load(route, {"tool_surface": value})
                self.assertEqual(str(caught.exception), SURFACE_ERROR)

    def test_default_and_explicit_all_preserve_legacy_command_and_defaults(self):
        expected_command = ["never-launch-qoder", "--acp", "--model", "ExactModel",
                            "--max-model-request-retries", "0", "--tools", "", "--strict-mcp-config",
                            "--setting-sources", "local", "--permission-mode", "default"]
        for route in ("direct", "factory", "list", "object"):
            for options in ({}, {"tool_surface": "all"}):
                with self.subTest(route=route, options=options):
                    driver = self.load(route, options)
                    self.assertIsInstance(driver, QoderAcpDriver)
                    self.assertEqual(driver.tool_surface, "all")
                    self.assertEqual(driver.command, expected_command)
                    self.assertEqual((driver.name, driver.model, driver.timeout, driver.token_budget, driver.max_run_seconds),
                                     ("worker", "ExactModel", 120, None, None))
                    self.assertFalse(driver.closed)
                    driver.close()

    def test_catalog_is_constructed_lazily_through_all_routes(self):
        for route in ("direct", "factory", "list", "object"):
            with self.subTest(route=route):
                driver = self.load(route, {"tool_surface": "catalog"})
                self.assertEqual(driver.tool_surface, "catalog")
                run = driver.agent_run(cwd=".", tools=[{
                    "name": "inspect", "description": "Synthetic inspection", "input_schema": {"type": "object"},
                }], execute=Mock(), check_current=Mock(), on_update=None)
                self.assertEqual([item["name"] for item in run._wire_tools], ["describe_tools", "invoke_tool"])
                self.assertEqual(run.state.phase, "created")
                self.assertFalse(run.state.prompt_attempted)
                driver.close()

    def test_options_and_command_are_unchanged_by_surface_choice(self):
        for route in ("direct", "factory", "list", "object"):
            with self.subTest(route=route):
                options = {"timeout": 17, "max_run_seconds": 23, "token_budget": 0}
                drivers = [self.load(route, {**options, "tool_surface": mode}) for mode in ("all", "catalog")]
                self.assertEqual(drivers[0].command, drivers[1].command)
                for driver in drivers:
                    self.assertEqual((driver.timeout, driver.max_run_seconds, driver.token_budget), (17, 23, 0))
                    driver.close()

    def test_factory_and_file_input_options_are_not_mutated(self):
        options = {"tool_surface": "catalog", "timeout": 17, "max_run_seconds": 23}
        before = deepcopy((self.config, options))
        for route in ("factory", "list", "object"):
            self.load(route, options).close()
        self.assertEqual((self.config, options), before)

    def test_default_all_run_keeps_original_tools_and_direct_callback(self):
        tools = [{"name": "inspect", "description": "Synthetic inspection", "input_schema": {
            "type": "object", "properties": {"page": {"type": "integer"}},
        }}]
        expected = deepcopy(tools)
        for options in ({}, {"tool_surface": "all"}):
            with self.subTest(options=options):
                execute = Mock(return_value={"status": "ok"})
                driver = self.load("factory", options)
                run = driver.agent_run(cwd=".", tools=tools, execute=execute, check_current=lambda: None, on_update=None)
                self.assertIsNone(run._catalog)
                self.assertEqual(run._wire_tools, expected)
                self.assertIsNot(run._wire_tools, tools)
                call = ToolCall("old-id", "inspect", {"page": 1})
                self.assertIs(run._call(call), execute.return_value)
                execute.assert_called_once_with(call)
                driver.close()
        self.assertEqual(tools, expected)

    def test_run_definitions_are_isolated_from_input_mutation_in_both_modes(self):
        for mode in ("all", "catalog"):
            with self.subTest(mode=mode):
                tools = [{"name": "inspect", "description": "Read", "input_schema": {
                    "type": "object", "required": ["page"],
                }}]
                expected = deepcopy(tools)
                driver = self.load("factory", {"tool_surface": mode})
                run = driver.agent_run(cwd=".", tools=tools, execute=Mock(), check_current=lambda: None, on_update=None)
                tools[0]["input_schema"]["required"].append("changed")
                self.assertEqual(run._tools, expected)
                driver.close()

    def test_command_property_returns_independent_copies(self):
        for mode in ("all", "catalog"):
            with self.subTest(mode=mode):
                driver = self.load("factory", {"tool_surface": mode})
                expected = driver.command
                changed = driver.command
                changed[0] = "modified"
                changed.append("--changed")
                self.assertEqual(driver.command, expected)
                driver.close()

    def test_default_and_explicit_all_fingerprints_equal_exact_old_hash(self):
        for route in ("direct", "factory", "list", "object"):
            for options in ({}, {"tool_surface": "all"}):
                with self.subTest(route=route, options=options):
                    driver = self.load(route, options)
                    self.assertEqual(driver_fingerprint(driver), fingerprint(legacy_fingerprint_data(driver)))
                    self.assertEqual(driver_fingerprint(driver), LEGACY_DEFAULT_HASH)
                    driver.close()

    def test_all_fingerprint_matches_legacy_algorithm_with_config_overrides(self):
        for options in ({"timeout": 17, "max_run_seconds": 23}, {"token_budget": 0},
                        {"model": "OtherModel", "executable": "other-client"}):
            with self.subTest(options=options):
                driver = self.load("factory", {**options, "tool_surface": "all"})
                self.assertEqual(driver_fingerprint(driver), fingerprint(legacy_fingerprint_data(driver)))
                driver.close()

    def test_drivers_without_surface_attribute_keep_legacy_hash(self):
        driver = SimpleNamespace(command=["synthetic"], model="model", timeout=17,
                                 execution_profile="fixture", max_run_seconds=23)
        self.assertEqual(driver_fingerprint(driver), fingerprint(legacy_fingerprint_data(driver)))

    def test_catalog_fingerprint_adds_only_surface_and_differs_from_all(self):
        driver = self.load("factory", {})
        original = driver_fingerprint(driver)
        driver.tool_surface = "catalog"
        expected = {**legacy_fingerprint_data(driver), "tool_surface": "catalog"}
        self.assertEqual(driver_fingerprint(driver), fingerprint(expected))
        self.assertNotEqual(driver_fingerprint(driver), original)
        driver.tool_surface = "all"
        self.assertEqual(driver_fingerprint(driver), original)
        self.assertEqual(original, LEGACY_DEFAULT_HASH)
        driver.close()

    def test_catalog_does_not_enable_native_on_demand_loading(self):
        for route in ("factory", "list", "object"):
            for mode in ("all", "catalog"):
                with self.subTest(route=route, mode=mode):
                    with self.assertRaisesRegex(ValueError, "qoder_acp supports only tool_loading='all'"):
                        self.load(route, {"tool_surface": mode, "tool_loading": "on_demand"})
                    driver = self.load(route, {"tool_surface": mode, "tool_loading": "all"})
                    self.assertEqual(driver.tool_surface, mode)
                    driver.close()

    def test_legacy_positional_constructor_and_appended_surface_option(self):
        old = QoderAcpDriver("worker", "never-launch-qoder", "ExactModel", 17, 31, 23)
        catalog = QoderAcpDriver("worker", "never-launch-qoder", "ExactModel", 17, 31, 23, "catalog")
        self.assertEqual((old.tool_surface, catalog.tool_surface), ("all", "catalog"))
        self.assertEqual((old.timeout, old.token_budget, old.max_run_seconds), (17, 31, 23))
        self.assertEqual((catalog.timeout, catalog.token_budget, catalog.max_run_seconds), (17, 31, 23))
        old.close()
        catalog.close()


if __name__ == "__main__":
    unittest.main()
