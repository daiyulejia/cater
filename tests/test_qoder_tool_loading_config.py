"""Unsupported external loading is a configuration error, not silent cost control."""
from copy import deepcopy
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from cater.drivers import driver_from_dict, load_drivers
from cater.external_agent import QoderAcpDriver


class StringSubclass(str):
    pass


class QoderToolLoadingConfigTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(socket, "socket", side_effect=AssertionError("Unexpected network")))
        self.enterContext(patch.object(subprocess, "Popen", side_effect=AssertionError("Unexpected process")))
        self.config = {"name": "external", "kind": "qoder_acp", "executable": "fixture-client",
                       "model": "fixture-model", "timeout": 30, "max_run_seconds": 60}

    def load(self, route, options):
        config = {**self.config, **options}
        before = deepcopy(config)
        try:
            if route == "factory":
                return driver_from_dict(config)
            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "drivers.json"
                raw = json.dumps([config] if route == "list" else {"drivers": [config]})
                path.write_text(raw, encoding="utf-8")
                try:
                    return load_drivers(path)["external"]
                finally:
                    self.assertEqual(path.read_text(encoding="utf-8"), raw)
        finally:
            self.assertEqual(config, before)

    def test_default_and_explicit_all_preserve_the_existing_driver(self):
        for route in ("factory", "list", "object"):
            drivers = [self.load(route, options) for options in ({}, {"tool_loading": "all"})]
            for driver in drivers:
                self.assertIsInstance(driver, QoderAcpDriver)
                self.assertEqual((driver.name, driver.model, driver.timeout, driver.max_run_seconds),
                                 ("external", "fixture-model", 30, 60))
                self.assertFalse(driver.closed)
                driver.close()
                self.assertTrue(driver.closed)
            self.assertEqual(drivers[0].command, drivers[1].command)

    def test_on_demand_fails_before_driver_construction_in_all_file_shapes(self):
        for route in ("factory", "list", "object"):
            with self.subTest(route=route), patch("cater.external_agent.QoderAcpDriver") as constructor:
                with self.assertRaisesRegex(ValueError, "qoder_acp supports only"):
                    self.load(route, {"tool_loading": "on_demand"})
                constructor.assert_not_called()

    def test_invalid_values_fail_without_echoing_the_configuration_value(self):
        for value in (None, True, False, 0, 1, [], {}, "", "ALL", " all", "all ", "on-demand", "PRIVATE_OPTION_SECRET"):
            for route in ("factory", "list", "object"):
                with self.subTest(value=value, route=route), patch("cater.external_agent.QoderAcpDriver") as constructor:
                    with self.assertRaises(ValueError) as caught:
                        self.load(route, {"tool_loading": value})
                    self.assertNotIn("PRIVATE_OPTION_SECRET", str(caught.exception))
                    constructor.assert_not_called()

    def test_non_plain_strings_are_not_silently_accepted(self):
        for value in (StringSubclass("all"), StringSubclass("on_demand")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.load("factory", {"tool_loading": value})
