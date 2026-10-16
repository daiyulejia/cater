"""Offline runner contracts: complete discovery, module shards and wrapper CLIs."""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts import check
from scripts import run_tests as runner


ROOT = Path(__file__).resolve().parents[1]
SHELL = shutil.which("pwsh") or shutil.which("powershell")


def fixture_cases(module, names=("test_second", "test_first")):
    case = type("Cases", (unittest.TestCase,), {
        "__module__": module, **{name: lambda self: None for name in names},
    })
    return [case(name) for name in names]


def child_environment():
    allowed = {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "TMPDIR"}
    result = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    result.update(PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
    return result


class TestRunnerSelectionTests(unittest.TestCase):
    def invoke(self, suite, argv=(), *, errors=(), raises=None):
        output, diagnostic = io.StringIO(), io.StringIO()
        loader = unittest.defaultTestLoader

        def discover(*args, **kwargs):
            loader.errors.extend(errors)
            if raises:
                raise raises
            return suite

        result = SimpleNamespace(slowest=[], wasSuccessful=lambda: True)
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "false"}), \
                patch.object(loader, "errors", ["stale previous discovery"]), \
                patch.object(loader, "discover", side_effect=discover), \
                patch.object(runner.unittest, "TextTestRunner") as execute, \
                redirect_stdout(output), redirect_stderr(diagnostic):
            execute.return_value.run.return_value = result
            status = runner.main(list(argv))
        return status, output.getvalue(), diagnostic.getvalue(), execute

    def test_two_shards_are_disjoint_complete_and_module_atomic(self):
        cases = [case for index in range(30) for case in fixture_cases(f"fixture.module_{index}")]
        shards = [runner.select_tests(cases, (index, 2)) for index in (1, 2)]
        ids = [{case.id() for case in shard} for shard in shards]
        self.assertTrue(all(shards))
        self.assertFalse(ids[0] & ids[1])
        self.assertEqual(Counter(case.id() for case in shards[0] + shards[1]),
                         Counter(case.id() for case in cases))
        for index in range(30):
            module = f"fixture.module_{index}"
            self.assertEqual(sum(any(type(case).__module__ == module for case in shard) for shard in shards), 1)

    def test_assignment_is_fixed_sha256_and_independent_of_order_or_new_modules(self):
        cases = [case for index in range(20) for case in fixture_cases(f"fixture.module_{index}")]
        for case in cases:
            module = type(case).__module__
            expected = int.from_bytes(hashlib.sha256(module.encode()).digest()[:8], "big") % 2 + 1
            self.assertEqual(runner.module_shard(module, 2), expected)
        selected = {case.id() for case in runner.select_tests(cases, (1, 2))}
        reversed_ids = {case.id() for case in runner.select_tests(list(reversed(cases)), (1, 2))}
        expanded = runner.select_tests(cases + fixture_cases("fixture.added"), (1, 2))
        self.assertEqual(selected, reversed_ids)
        self.assertEqual(selected, {case.id() for case in expanded if type(case).__module__ != "fixture.added"})

    def test_selection_preserves_load_tests_order_and_existing_id_multiplicity(self):
        cases = fixture_cases("fixture.custom_order")
        cases.append(type(cases[0])(cases[0]._testMethodName))
        index = runner.module_shard("fixture.custom_order", 2)
        self.assertEqual(runner.select_tests(cases, (index, 2)), cases)
        self.assertEqual(Counter(case.id() for case in runner.select_tests(cases, (index, 2))),
                         Counter(case.id() for case in cases))
        suite = unittest.TestSuite([unittest.TestSuite(cases[:2]), unittest.TestSuite(cases[2:])])
        status, _, _, execute = self.invoke(suite)
        self.assertEqual(status, 0)
        self.assertIs(execute.return_value.run.call_args.args[0], suite)

    def test_full_default_ignores_callers_argv_and_clears_stale_loader_errors(self):
        suite = unittest.TestSuite(fixture_cases("fixture.full"))
        with patch.object(sys, "argv", ["outer-unittest", "--not-a-runner-option"]):
            status, _, diagnostic, execute = self.invoke(suite)
        self.assertEqual(status, 0)
        self.assertIn("Selection: full suite", diagnostic)
        execute.return_value.run.assert_called_once_with(suite)

    def test_shard_execution_keeps_discovered_case_order(self):
        cases = fixture_cases("fixture.a") + fixture_cases("fixture.b")
        expected = runner.select_tests(cases, (1, 2))
        if not expected:
            expected = runner.select_tests(cases, (2, 2))
            index = 2
        else:
            index = 1
        status, _, _, execute = self.invoke(unittest.TestSuite(cases), ["--shard", f"{index}/2"])
        self.assertEqual(status, 0)
        selected = list(runner.test_cases(execute.return_value.run.call_args.args[0]))
        self.assertEqual(selected, expected)

    def test_shards_reject_nonplain_suites_at_root_or_nested_even_in_list_mode(self):
        class CustomSuite(unittest.TestSuite):
            pass

        class CustomBaseSuite(unittest.BaseTestSuite):
            pass

        for suite_type in (CustomSuite, CustomBaseSuite, unittest.BaseTestSuite):
            for nested in (False, True):
                for listing in ([], ["--list-tests"]):
                    with self.subTest(suite=suite_type.__name__, nested=nested, listing=listing):
                        inner = suite_type(fixture_cases("fixture.custom_suite"))
                        suite = unittest.TestSuite([inner]) if nested else inner
                        status, output, diagnostic, execute = self.invoke(suite, ["--shard", "1/2", *listing])
                        self.assertEqual(status, 1)
                        self.assertIn("cannot be flattened safely", diagnostic)
                        self.assertEqual(output, "")
                        execute.assert_not_called()
                        status, _, _, execute = self.invoke(suite)
                        self.assertEqual(status, 0)
                        execute.return_value.run.assert_called_once_with(suite)

    def test_list_mode_prints_only_sorted_ids_and_never_executes_cases(self):
        cases = fixture_cases("fixture.inventory")
        cases.append(type(cases[0])(cases[0]._testMethodName))
        status, output, diagnostic, execute = self.invoke(unittest.TestSuite(cases), ["--list-tests"])
        self.assertEqual(status, 0)
        self.assertEqual(output.splitlines(), sorted(case.id() for case in cases))
        self.assertIn("1 existing repeated ID occurrence", diagnostic)
        execute.assert_not_called()

    def test_empty_full_suite_and_empty_shard_fail_in_run_and_list_modes(self):
        for listing in ([], ["--list-tests"]):
            status, _, diagnostic, execute = self.invoke(unittest.TestSuite(), listing)
            self.assertEqual(status, 1)
            self.assertIn("empty", diagnostic)
            execute.assert_not_called()
            cases = fixture_cases("fixture.only")
            excluded = 3 - runner.module_shard("fixture.only", 2)
            status, _, diagnostic, execute = self.invoke(
                unittest.TestSuite(cases), ["--shard", f"{excluded}/2", *listing])
            self.assertEqual(status, 1)
            self.assertIn("empty", diagnostic)
            execute.assert_not_called()

    def test_failed_import_cannot_disappear_into_another_shard(self):
        failed = unittest.loader._FailedTest("test_bad_import", ImportError("PRIVATE_FIXTURE_BODY"))
        excluded = 3 - runner.module_shard(type(failed).__module__, 2)
        for listing in ([], ["--list-tests"]):
            status, output, diagnostic, execute = self.invoke(
                unittest.TestSuite([*fixture_cases("fixture.valid"), failed]),
                ["--shard", f"{excluded}/2", *listing], errors=["PRIVATE_FIXTURE_BODY"],
            )
            self.assertEqual(status, 1)
            self.assertIn("no shard selection applied", diagnostic)
            self.assertIn("test_bad_import", diagnostic)
            self.assertNotIn("PRIVATE_FIXTURE_BODY", output + diagnostic)
            execute.assert_not_called()

    def test_failed_test_without_loader_errors_and_loader_errors_without_failed_test_both_fail(self):
        failed = unittest.loader._FailedTest("test_load_tests", ValueError("fixture"))
        for suite, errors in ((unittest.TestSuite([failed]), []),
                              (unittest.TestSuite(fixture_cases("fixture.valid")), ["fixture discovery error"])):
            status, _, _, execute = self.invoke(suite, ["--list-tests"], errors=errors)
            self.assertEqual(status, 1)
            execute.assert_not_called()

    def test_raised_discovery_exception_is_nonzero_without_echoing_its_body(self):
        status, output, diagnostic, execute = self.invoke(None, raises=ImportError("PRIVATE_FIXTURE_BODY"))
        self.assertEqual(status, 1)
        self.assertIn("ImportError", diagnostic)
        self.assertNotIn("PRIVATE_FIXTURE_BODY", output + diagnostic)
        execute.assert_not_called()

    def test_discovery_system_exit_is_nonzero_without_echoing_its_code(self):
        for code in (0, None, 7, "PRIVATE_FIXTURE_BODY"):
            with self.subTest(code=code):
                status, output, diagnostic, execute = self.invoke(None, raises=SystemExit(code))
                self.assertEqual(status, 1)
                self.assertIn("Test discovery failed (SystemExit)", diagnostic)
                self.assertIn("no tests executed", diagnostic)
                self.assertNotIn("PRIVATE_FIXTURE_BODY", output + diagnostic)
                execute.assert_not_called()

    def test_discovery_keyboard_interrupt_propagates_unchanged(self):
        for selection in ([], ["--shard", "1/2"], ["--shard", "2/2"]):
            for listing in ([], ["--list-tests"]):
                with self.subTest(selection=selection, listing=listing):
                    interrupt = KeyboardInterrupt()
                    with self.assertRaises(KeyboardInterrupt) as caught:
                        self.invoke(None, [*selection, *listing], raises=interrupt)
                    self.assertIs(caught.exception, interrupt)

    def test_bad_options_fail_before_discovery(self):
        invalid = ["0/2", "1/0", "3/2", "1/129", "01/2", "1/02", "-1/2", "a/2", "1.0/2", "1 /2", "1/2/3"]
        for value in invalid:
            with self.subTest(value=value), redirect_stderr(io.StringIO()), \
                    patch.object(unittest.defaultTestLoader, "discover") as discover:
                with self.assertRaises(SystemExit) as caught:
                    runner.main(["--shard", value])
                self.assertEqual(caught.exception.code, 2)
                discover.assert_not_called()
        with self.assertRaises(argparse.ArgumentTypeError):
            runner.shard_argument("9" * 100 + "/2")
        self.assertEqual(runner.shard_argument("128/128"), (128, 128))

    def test_unknown_cli_argument_is_not_silently_ignored(self):
        with redirect_stderr(io.StringIO()), patch.object(unittest.defaultTestLoader, "discover") as discover:
            with self.assertRaises(SystemExit) as caught:
                runner.main(["--unknown"])
        self.assertEqual(caught.exception.code, 2)
        discover.assert_not_called()

    def test_execution_failure_keeps_nonzero_exit_and_bounded_slow_report(self):
        suite = unittest.TestSuite(fixture_cases("fixture.fail"))
        result = SimpleNamespace(slowest=[(1.25, "fixture.test\n::warning::not-a-command")],
                                 wasSuccessful=lambda: False)
        diagnostic = io.StringIO()
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "false"}), \
                patch.object(unittest.defaultTestLoader, "discover", return_value=suite), \
                patch.object(unittest.TextTestRunner, "run", return_value=result), redirect_stderr(diagnostic):
            self.assertEqual(runner.main([]), 1)
        self.assertIn("1.250s", diagnostic.getvalue())
        self.assertNotIn("\n::warning::", diagnostic.getvalue())
        self.assertIn("excludes discovery and module/class fixtures", diagnostic.getvalue())

    def test_timing_storage_retains_only_ten_slowest_case_ids(self):
        result = runner.AnnotatedResult(io.StringIO(), True, 0)
        cases = fixture_cases("fixture.timings", tuple(f"test_{index}" for index in range(14)))
        ticks = [value for index in range(14) for value in (100.0 * index, 101.0 * index)]
        with patch.object(runner.time, "perf_counter", side_effect=ticks):
            for case in cases:
                result.startTest(case)
                result.stopTest(case)
        self.assertEqual(len(result.slowest), 10)
        self.assertEqual(sorted(elapsed for elapsed, _ in result.slowest), list(map(float, range(4, 14))))
        self.assertTrue(all(name.startswith("fixture.timings.Cases.test_") for _, name in result.slowest))

    def test_check_wrapper_forwards_options_and_failure_exit(self):
        arguments = ["--shard", "2/2", "--list-tests"]
        with patch.object(check.subprocess, "run", return_value=SimpleNamespace(returncode=7)) as process, \
                redirect_stderr(io.StringIO()):
            self.assertEqual(check.main(arguments), 7)
        self.assertEqual(process.call_args.args[0],
                         [sys.executable, "-u", str(ROOT / "scripts/run_tests.py"), *arguments])


class TestRunnerCLITests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "scripts").mkdir()
        (self.root / "tests").mkdir()
        for name in ("run_tests.py", "test_progress.py", "check.py", "check.ps1"):
            shutil.copyfile(ROOT / "scripts" / name, self.root / "scripts" / name)
        for name in ("alpha", "beta", "gamma", "delta"):
            (self.root / "tests" / f"test_fixture_{name}.py").write_text(
                "import unittest\nclass Cases(unittest.TestCase):\n"
                "    def test_first(self): pass\n    def test_second(self): pass\n"
                "def load_tests(loader, tests, pattern):\n"
                "    return unittest.TestSuite([Cases('test_second'), Cases('test_first')])\n", encoding="utf-8")

    def cli(self, *args, script="run_tests.py", env=None):
        return subprocess.run([sys.executable, "-X", "utf8", "-B", str(self.root / "scripts" / script), *args],
                              cwd=self.root, env=env or child_environment(), capture_output=True, text=True,
                              encoding="utf-8", timeout=15)

    def test_real_cli_shards_preserve_full_inventory_across_hash_seeds(self):
        full = self.cli("--list-tests")
        self.assertEqual(full.returncode, 0, full.stderr)
        shards = []
        for index in (1, 2):
            environment = child_environment() | {"PYTHONHASHSEED": str(index)}
            result = self.cli("--shard", f"{index}/2", "--list-tests", env=environment)
            self.assertEqual(result.returncode, 0, result.stderr)
            shards.append(result.stdout.splitlines())
        self.assertFalse(set(shards[0]) & set(shards[1]))
        self.assertEqual(Counter(shards[0] + shards[1]), Counter(full.stdout.splitlines()))
        repeat = self.cli("--shard", "1/2", "--list-tests", env=child_environment() | {"PYTHONHASHSEED": "99"})
        self.assertEqual(repeat.stdout.splitlines(), shards[0])

    def test_default_check_cli_runs_all_cases_and_forwards_invalid_options(self):
        full = self.cli(script="check.py")
        self.assertEqual(full.returncode, 0, full.stderr)
        self.assertIn("Ran 8 tests", full.stderr)
        self.assertIn("Slowest tests", full.stderr)
        invalid = self.cli("--shard", "0/2", script="check.py")
        self.assertEqual(invalid.returncode, 2)

    def test_check_list_cli_forwards_shard_without_polluting_ids(self):
        direct = self.cli("--shard", "1/2", "--list-tests")
        wrapped = self.cli("--shard", "1/2", "--list-tests", script="check.py")
        self.assertEqual(wrapped.returncode, 0, wrapped.stderr)
        self.assertEqual(wrapped.stdout, direct.stdout)

    def test_real_import_and_load_tests_errors_fail_both_shards_before_execution(self):
        path = self.root / "tests/test_fixture_broken.py"
        for source in ("raise ImportError('PRIVATE_FIXTURE_BODY')\n",
                       "def load_tests(loader, tests, pattern):\n    raise ValueError('PRIVATE_FIXTURE_BODY')\n"):
            path.write_text(source, encoding="utf-8")
            for index in (1, 2):
                with self.subTest(source=source.splitlines()[0], shard=index):
                    result = self.cli("--shard", f"{index}/2")
                    self.assertEqual(result.returncode, 1)
                    self.assertIn("no tests executed", result.stderr)
                    self.assertNotIn("PRIVATE_FIXTURE_BODY", result.stdout + result.stderr)
                    self.assertNotIn("Ran 8 tests", result.stderr)

    def test_real_load_tests_zero_exit_fails_full_shards_and_list_modes(self):
        path = self.root / "tests/test_fixture_exit.py"
        for code in ("0", "None"):
            path.write_text("def load_tests(loader, tests, pattern):\n"
                            f"    raise SystemExit({code})\n", encoding="utf-8")
            for script in ("run_tests.py", "check.py"):
                for selection in ([], ["--shard", "1/2"], ["--shard", "2/2"]):
                    for listing in ([], ["--list-tests"]):
                        with self.subTest(code=code, script=script, selection=selection, listing=listing):
                            result = self.cli(*selection, *listing, script=script)
                            self.assertEqual(result.returncode, 1, result.stderr)
                            self.assertIn("Test discovery failed (SystemExit)", result.stderr)
                            self.assertIn("no tests executed", result.stderr)
                            self.assertEqual(result.stdout, "")
                            self.assertNotIn("Selection:", result.stderr)
                            self.assertNotIn("Ran ", result.stderr)

    def test_real_custom_suite_hooks_run_in_full_but_both_shards_refuse(self):
        path = self.root / "tests/test_fixture_hooks.py"
        for base in ("TestSuite", "BaseTestSuite"):
            path.write_text(
                "import unittest\nactive = False\n"
                "class Case(unittest.TestCase):\n"
                "    def test_hook_active(self): self.assertTrue(active)\n"
                f"class HookSuite(unittest.{base}):\n"
                "    def run(self, result):\n"
                "        global active\n"
                "        active = True\n        print('hook_enter')\n"
                "        try: return super().run(result)\n"
                "        finally:\n            active = False\n            print('hook_exit')\n"
                "def load_tests(loader, tests, pattern):\n"
                "    return unittest.TestSuite([HookSuite([Case('test_hook_active')])])\n",
                encoding="utf-8")
            with self.subTest(base=base):
                full = self.cli(script="check.py")
                self.assertEqual(full.returncode, 0, full.stderr)
                self.assertEqual(full.stdout.splitlines(), ["hook_enter", "hook_exit"])
                self.assertIn("Ran 9 tests", full.stderr)
                for index in (1, 2):
                    for listing in ([], ["--list-tests"]):
                        result = self.cli("--shard", f"{index}/2", *listing, script="check.py")
                        self.assertEqual(result.returncode, 1, result.stderr)
                        self.assertIn("cannot be flattened safely", result.stderr)
                        self.assertIn("no tests executed", result.stderr)
                        self.assertEqual(result.stdout, "")
                        self.assertNotIn("Ran ", result.stderr)

    def test_real_plain_base_suite_is_counted_and_keeps_its_no_shared_fixtures_contract(self):
        (self.root / "tests/test_fixture_base.py").write_text(
            "import unittest\nclass Case(unittest.TestCase):\n"
            "    @classmethod\n    def setUpClass(cls): raise AssertionError('must not inject fixtures')\n"
            "    def test_base(self): pass\n"
            "def load_tests(loader, tests, pattern):\n"
            "    return unittest.BaseTestSuite([unittest.BaseTestSuite([Case('test_base')])])\n",
            encoding="utf-8")
        full = self.cli(script="check.py")
        self.assertEqual(full.returncode, 0, full.stderr)
        self.assertIn("9/9 execution items", full.stderr)
        self.assertIn("Ran 9 tests", full.stderr)
        listing = self.cli("--list-tests")
        self.assertEqual(listing.returncode, 0, listing.stderr)
        self.assertEqual(len(listing.stdout.splitlines()), 9)
        self.assertIn("test_fixture_base.Case.test_base", listing.stdout.splitlines())
        for index in (1, 2):
            result = self.cli("--shard", f"{index}/2")
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("cannot be flattened safely", result.stderr)

    def test_real_failing_case_is_not_retried_and_check_keeps_nonzero_exit(self):
        (self.root / "tests/test_fixture_failure.py").write_text(
            "import unittest\nclass Case(unittest.TestCase):\n"
            "    def test_failure(self): self.fail('offline fixture failure')\n", encoding="utf-8")
        index = runner.module_shard("test_fixture_failure", 2)
        result = self.cli("--shard", f"{index}/2", script="check.py")
        self.assertEqual(result.returncode, 1)
        self.assertIn("FAILED (failures=1)", result.stderr)

    @unittest.skipUnless(SHELL, "PowerShell wrapper requires a PowerShell executable")
    def test_powershell_forwards_shard_and_list_options_from_another_directory(self):
        environment = child_environment() | {"PYTHON": sys.executable}
        result = subprocess.run([SHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
                                 str(self.root / "scripts/check.ps1"), "--shard", "2/2", "--list-tests"],
                                cwd=self.root.parent, env=environment, capture_output=True,
                                text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        expected = self.cli("--shard", "2/2", "--list-tests")
        ids = [line for line in result.stdout.splitlines() if line.startswith("test_fixture_")]
        self.assertEqual(ids, expected.stdout.splitlines())


if __name__ == "__main__":
    unittest.main()
