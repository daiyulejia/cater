"""Offline configuration checks; no ACP peer or model process is started."""

from decimal import Decimal
from pathlib import Path
import unittest
from unittest.mock import patch

from cater.drivers import driver_from_dict
from cater.external_agent import QoderAcpDriver


class ExternalBudgetConfigTests(unittest.TestCase):
    def setUp(self):
        self.session = self.enterContext(patch(
            "cater.external_agent.AcpSession", side_effect=AssertionError("ACP must stay lazy")))
        self.bridge = self.enterContext(patch(
            "cater.external_agent.McpToolBridge", side_effect=AssertionError("MCP must stay lazy")))
        self.process = self.enterContext(patch(
            "subprocess.Popen", side_effect=AssertionError("No process may be started")))

    def tearDown(self):
        self.session.assert_not_called()
        self.bridge.assert_not_called()
        self.process.assert_not_called()

    def construct(self, source, **options):
        if source == "factory":
            return driver_from_dict({
                "kind": "qoder_acp", "name": "worker",
                "executable": "never-launch-qoder", "model": "ExactModel", **options,
            })
        return QoderAcpDriver("worker", "never-launch-qoder", "ExactModel", **options)

    def test_default_preserves_legacy_timeout_and_unset_budgets(self):
        for source in ("direct", "factory"):
            with self.subTest(source=source):
                driver = self.construct(source)
                self.assertIsInstance(driver, QoderAcpDriver)
                self.assertEqual(driver.timeout, 120)
                self.assertIsNone(driver.token_budget)
                self.assertIsNone(driver.max_run_seconds)

    def test_omitted_or_none_total_preserves_custom_timeout(self):
        for source in ("direct", "factory"):
            for options in ({}, {"max_run_seconds": None}):
                for timeout in (0.25, 180, 3600):
                    with self.subTest(source=source, options=options, timeout=timeout):
                        driver = self.construct(source, timeout=timeout, **options)
                        self.assertEqual(driver.timeout, timeout)
                        self.assertIsNone(driver.max_run_seconds)

    def test_explicit_total_accepts_default_timeout_boundaries(self):
        for source in ("direct", "factory"):
            for total in (120, 120.0, 600, 600.5, 3600, 3600.0):
                with self.subTest(source=source, total=total):
                    driver = self.construct(source, max_run_seconds=total)
                    self.assertEqual(driver.timeout, 120)
                    self.assertEqual(driver.max_run_seconds, total)
                    self.assertIs(type(driver.max_run_seconds), type(total))

    def test_explicit_total_accepts_custom_timeout_boundaries(self):
        for source in ("direct", "factory"):
            for timeout, total in ((0.25, 0.25), (0.25, 0.5), (180, 180),
                                   (180, 600.5), (3600, 3600)):
                with self.subTest(source=source, timeout=timeout, total=total):
                    driver = self.construct(source, timeout=timeout, max_run_seconds=total)
                    self.assertEqual(driver.timeout, timeout)
                    self.assertEqual(driver.max_run_seconds, total)

    def test_explicit_total_rejects_non_numeric_and_boolean_values(self):
        for source in ("direct", "factory"):
            for total in (True, False, "600", "", [], {}, (600,), Decimal("600")):
                with self.subTest(source=source, total=total):
                    with self.assertRaises(ValueError):
                        self.construct(source, max_run_seconds=total)

    def test_explicit_total_rejects_non_finite_values(self):
        for source in ("direct", "factory"):
            for total in (float("nan"), float("inf"), -float("inf")):
                with self.subTest(source=source, total=total):
                    with self.assertRaises(ValueError):
                        self.construct(source, max_run_seconds=total)

    def test_explicit_total_rejects_out_of_range_values(self):
        for source in ("direct", "factory"):
            for timeout, total in ((120, -1), (120, 0), (120, 119.999),
                                   (180, 120), (3600, 3599.999), (120, 3600.001), (120, 10 ** 1000)):
                with self.subTest(source=source, timeout=timeout, total=total):
                    with self.assertRaises(ValueError):
                        self.construct(source, timeout=timeout, max_run_seconds=total)

    def test_total_does_not_make_invalid_timeout_valid(self):
        for source in ("direct", "factory"):
            for timeout in (None, True, False, "120", -1, 0, 3600.001,
                            float("nan"), float("inf"), -float("inf")):
                for options in ({}, {"max_run_seconds": 3600}):
                    with self.subTest(source=source, timeout=timeout, options=options):
                        with self.assertRaises(ValueError):
                            self.construct(source, timeout=timeout, **options)

    def test_token_budget_is_preserved_in_both_modes(self):
        for source in ("direct", "factory"):
            for token_budget in (None, 0, 1000):
                for total in (None, 600):
                    with self.subTest(source=source, token_budget=token_budget, total=total):
                        driver = self.construct(source, token_budget=token_budget, max_run_seconds=total)
                        self.assertEqual(driver.token_budget, token_budget)
                        self.assertEqual(driver.max_run_seconds, total)

    def test_constructor_keeps_token_budget_position_and_appends_total(self):
        legacy = QoderAcpDriver("worker", "never-launch-qoder", "ExactModel", 180, 1000)
        positional = QoderAcpDriver("worker", "never-launch-qoder", "ExactModel", 180, 1000, 600)
        keyword = QoderAcpDriver("worker", "never-launch-qoder", "ExactModel", 180, 1000,
                                max_run_seconds=600)
        for driver, total in ((legacy, None), (positional, 600), (keyword, 600)):
            with self.subTest(total=total):
                self.assertEqual(driver.timeout, 180)
                self.assertEqual(driver.token_budget, 1000)
                self.assertEqual(driver.max_run_seconds, total)

    def test_configuration_and_run_creation_stay_lazy_in_both_modes(self):
        for source in ("direct", "factory"):
            for options in ({}, {"max_run_seconds": None}, {"max_run_seconds": 600}):
                with self.subTest(source=source, options=options):
                    driver = self.construct(source, **options)
                    run = driver.agent_run(
                        cwd=Path("."), tools=[], execute=lambda call: {"status": "ok"},
                        check_current=lambda: None, on_update=None,
                    )
                    self.assertEqual(run.state.phase, "created")
                    self.assertFalse(run.state.prompt_attempted)
                    self.assertFalse(driver.closed)
                    driver.close()


if __name__ == "__main__":
    unittest.main()
