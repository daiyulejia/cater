"""Qoder thinking configuration tests: no credentials or model requests."""

from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from cater.acp import AcpUnavailable
from cater.cli import cmd_drivers
from cater.drivers import driver_from_dict, load_drivers
from cater.external_agent import QoderAcpDriver
from cater.runtime import _execution_fingerprint


CONFIG = {"name": "worker", "kind": "qoder_acp", "executable": "offline-client", "model": "ExampleModel"}


class QoderThinkingConfigTests(unittest.TestCase):
    def test_default_null_and_disabled_commands_and_fingerprints(self):
        default = driver_from_dict(CONFIG)
        null = driver_from_dict({**CONFIG, "thinking": None})
        disabled = driver_from_dict({**CONFIG, "thinking": "disabled"})
        legacy = driver_from_dict(CONFIG)
        del legacy._thinking  # The old driver had no thinking attribute.
        self.assertEqual(default.command, null.command)
        self.assertEqual(disabled.command, default.command + ["--thinking", "disabled"])
        self.assertIsNone(default.thinking)
        config = SimpleNamespace(roles=[SimpleNamespace(driver="worker")],
                                 organization=SimpleNamespace(leader=SimpleNamespace(driver=None)))
        browser = SimpleNamespace(command=None)
        with patch("cater.runtime._organization_fingerprint", return_value="fixture"):
            def fingerprint(driver):
                return _execution_fingerprint(config, {"worker": driver}, browser)
            self.assertEqual(fingerprint(legacy), fingerprint(default))
            self.assertEqual(fingerprint(default), fingerprint(null))
            self.assertNotEqual(fingerprint(default), fingerprint(disabled))

    def test_mode_is_read_only_in_both_directions(self):
        for initial, attempted in ((None, "disabled"), ("disabled", None)):
            with self.subTest(initial=initial):
                driver = driver_from_dict({**CONFIG, "thinking": initial})
                command = driver.command
                with self.assertRaises(AttributeError):
                    driver.thinking = attempted
                self.assertEqual(driver.thinking, initial)
                self.assertEqual(driver.command, command)

    def test_strict_validation_at_both_entry_points(self):
        class StringSubclass(str):
            pass
        for invalid in (True, False, 0, 1, "", "enabled", "off", " disabled", "disabled ",
                        "DISABLED", [], {}, ["disabled"], StringSubclass("disabled")):
            with self.subTest(value=invalid):
                with self.assertRaisesRegex(ValueError, "Qoder thinking"):
                    driver_from_dict({**CONFIG, "thinking": invalid})
                with self.assertRaisesRegex(ValueError, "Qoder thinking"):
                    QoderAcpDriver("worker", "offline-client", "ExampleModel", thinking=invalid)

    def test_json_configuration_loads_and_rejects_invalid_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "drivers.json"
            for value in (None, "disabled"):
                path.write_text(json.dumps({"drivers": [{**CONFIG, "thinking": value}]}), encoding="utf-8")
                driver = load_drivers(path)["worker"]
                self.assertEqual(driver.thinking, value)
            path.write_text(json.dumps({"drivers": [{**CONFIG, "thinking": "enabled"}]}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Qoder thinking"):
                load_drivers(path)

    def test_drivers_command_displays_request_not_effective_mode(self):
        for value, label in ((None, "unspecified"), ("disabled", "requested-disabled")):
            driver = driver_from_dict({**CONFIG, "thinking": value})
            with self.assertRaises(AttributeError):
                driver.thinking = "disabled" if value is None else None
            role = SimpleNamespace(id="worker", role="dev", driver="worker")
            rt = SimpleNamespace(roles=[role], drivers={"worker": driver})
            context = MagicMock()
            context.__enter__.return_value = rt
            output = StringIO()
            with patch("cater.cli.open_runtime", return_value=context), redirect_stdout(output):
                self.assertEqual(cmd_drivers(SimpleNamespace()), 0)
            self.assertIn("thinking=" + label, output.getvalue())
            self.assertNotIn("offline-client", output.getvalue())

    def test_rejected_client_does_not_retry_without_parameter(self):
        driver = driver_from_dict({**CONFIG, "thinking": "disabled"})
        session = MagicMock()
        session.session_id = None
        session.open.side_effect = AcpUnavailable("client rejected startup")
        bridge = MagicMock()
        with tempfile.TemporaryDirectory() as directory, \
             patch("cater.external_agent.AcpSession", return_value=session) as session_factory, \
             patch("cater.external_agent.McpToolBridge", return_value=bridge):
            run = driver.agent_run(cwd=directory, tools=[], execute=lambda call: {},
                                   check_current=lambda: None, on_update=None)
            with self.assertRaises(AcpUnavailable):
                run.run("offline fixture")
            self.assertEqual(session_factory.call_count, 1)
            self.assertEqual(session_factory.call_args.args[0], driver.command)
            self.assertEqual(session.open.call_count, 1)
            session.prompt.assert_not_called()


if __name__ == "__main__":
    unittest.main()
