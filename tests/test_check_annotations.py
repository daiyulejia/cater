import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from scripts.run_tests import AnnotatedResult, escape_annotation, main


class CheckAnnotationTests(unittest.TestCase):
    def run_case(self, case, *, github: bool):
        output = io.StringIO()
        with patch.dict("os.environ", {"GITHUB_ACTIONS": "true" if github else "false"}), redirect_stdout(output):
            result = unittest.TextTestRunner(stream=io.StringIO(), resultclass=AnnotatedResult, verbosity=2).run(unittest.TestSuite([case]))
        return result, output.getvalue()

    def test_annotation_escapes_protocol_delimiters_and_newlines(self):
        self.assertEqual(escape_annotation("a%\r\nb,:", property_value=True), "a%25%0D%0Ab%2C%3A")
        self.assertEqual(escape_annotation("a\n::warning::value"), "a%0A::warning::value")

    def test_errors_failures_and_subtests_keep_failure_result_and_add_diagnostics(self):
        class Case(unittest.TestCase):
            def runTest(self):
                with self.subTest(label="comma,value:100%"):
                    self.fail("subtest failed")
                raise ValueError("exception\n::notice::must not become a command")

        result, output = self.run_case(Case(), github=True)
        self.assertFalse(result.wasSuccessful())
        self.assertEqual(len(result.errors), 1)
        self.assertEqual(len(result.failures), 1)
        self.assertEqual(output.count("::error title="), 2)
        self.assertIn("subtest failed", output)
        self.assertIn("ValueError", output)
        self.assertIn("%2C", output)
        self.assertNotIn("\n::notice::", output)

    def test_local_runs_do_not_emit_workflow_commands(self):
        class Case(unittest.TestCase):
            def runTest(self): self.fail("local failure")

        result, output = self.run_case(Case(), github=False)
        self.assertFalse(result.wasSuccessful())
        self.assertEqual(output, "")

    def test_ci_runner_uses_utf8_and_preserves_nonzero_failure_exit(self):
        with patch.dict("os.environ", {"GITHUB_ACTIONS": "true"}), patch("scripts.run_tests.sys.stdout") as output, patch("scripts.run_tests.unittest.defaultTestLoader.discover"), patch("scripts.run_tests.unittest.TextTestRunner") as runner:
            runner.return_value.run.return_value.wasSuccessful.return_value = False
            self.assertEqual(main(), 1)
            output.reconfigure.assert_called_once_with(encoding="utf-8", errors="backslashreplace")
