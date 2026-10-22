"""Format observations cannot promote program declarations into execution proof."""

import copy
import io
import json
import unittest

from cater.receipt_observations import receipt_observations
from cater.verification import _Capture


SUMMARY = "Ran 17 tests in 0.001s\n\nOK\n"


def capture(text):
    output = _Capture()
    output.drain(io.BytesIO(text.encode("utf-8")))
    return output.summary(record_output=True)


def receipt(stdout="", stderr=SUMMARY):
    return {"status": "ok", "outcome": "passed", "exit_code": 0,
            "complete": True, "truncated": False, "timed_out": False,
            "stdout": capture(stdout), "stderr": capture(stderr)}


class ReceiptObservationTests(unittest.TestCase):
    def test_actual_capture_has_stable_small_metadata_without_private_body(self):
        result = receipt(stderr="PRIVATE_TEST_BODY token=value\n" + "-" * 70 + "\n" + SUMMARY)
        result.update(command="PRIVATE_ALIAS", argv=["PRIVATE_ARGV"], output="PRIVATE_COMBINED")
        observed = receipt_observations(result)
        self.assertEqual(len(observed), 1)
        row = observed[0]
        self.assertEqual(set(row), {"kind", "source_stream", "literal_summary", "reported_tests",
                                    "reported_outcome", "reported_counts", "program_report_note"})
        self.assertEqual(row["kind"], "unittest_text_summary")
        self.assertEqual(row["source_stream"], "stderr")
        self.assertEqual(row["literal_summary"], SUMMARY[:-1])
        self.assertEqual(row["reported_tests"], 17)
        self.assertEqual(row["reported_outcome"], "OK")
        self.assertEqual(row["reported_counts"], {})
        encoded = json.dumps(observed)
        self.assertNotIn("PRIVATE", encoded)
        self.assertLessEqual(len(encoded.encode()), 1024)
        self.assertIn("declaration only", row["program_report_note"])
        self.assertIn("does not prove program identity", row["program_report_note"])

    def test_crlf_stdout_singular_zero_and_optional_final_newline(self):
        for summary, count, outcome in ((SUMMARY.replace("\n", "\r\n"), 17, "OK"),
                ("Ran 1 test in 1.234s\n\nOK", 1, "OK"),
                ("Ran 0 tests in 0.000s\n\nNO TESTS RAN\n", 0, "NO TESTS RAN"),
                ("Ran 0 tests in 0.000s\n\nOK\n", 0, "OK")):
            with self.subTest(summary=summary):
                row = receipt_observations(receipt(stdout=summary, stderr=""))[0]
                self.assertEqual(row["source_stream"], "stdout")
                self.assertEqual(row["reported_tests"], count)
                self.assertEqual(row["reported_outcome"], outcome)
                self.assertEqual(row["literal_summary"], summary.removesuffix("\n").removesuffix("\r"))

    def test_failure_skip_expected_and_unexpected_counts(self):
        cases = [("FAILED (failures=1, errors=2, skipped=3, expected failures=4, unexpected successes=5)",
                  {"failures": 1, "errors": 2, "skipped": 3, "expected_failures": 4, "unexpected_successes": 5}),
                 ("FAILED(failures=1,errors=2,skipped=3)", {"failures": 1, "errors": 2, "skipped": 3}),
                 ("FAILED (unexpected successes=1)", {"unexpected_successes": 1}),
                 ("OK (skipped=1, expected failures=2)", {"skipped": 1, "expected_failures": 2})]
        for outcome, counts in cases:
            with self.subTest(outcome=outcome):
                row = receipt_observations(receipt(stderr="Ran 17 tests in 0.001s\n\n" + outcome + "\n"))[0]
                self.assertEqual(row["reported_counts"], counts)
                self.assertEqual(row["reported_outcome"], outcome.split("(")[0].strip())

    def test_real_stdlib_runner_summaries_match_the_reported_result(self):
        class Sample(unittest.TestCase):
            def passes(self):
                pass

            def fails(self):
                self.fail("PRIVATE_FAILURE_BODY")

            def errors(self):
                raise ValueError("PRIVATE_ERROR_BODY")

            @unittest.skip("PRIVATE_SKIP_REASON")
            def skips(self):
                pass

            @unittest.expectedFailure
            def expected(self):
                self.fail("PRIVATE_EXPECTED_BODY")

            @unittest.expectedFailure
            def unexpected(self):
                pass

        for names in ([], ["passes"], ["skips", "expected"],
                      ["passes", "fails", "errors", "skips", "expected", "unexpected"]):
            with self.subTest(names=names):
                stream = io.StringIO()
                actual = unittest.TextTestRunner(stream=stream, verbosity=2).run(
                    unittest.TestSuite(Sample(name) for name in names))
                row = receipt_observations(receipt(stderr=stream.getvalue()))[0]
                self.assertEqual(row["reported_tests"], actual.testsRun)
                if not names:
                    self.assertIn(row["reported_outcome"], ("OK", "NO TESTS RAN"))
                else:
                    self.assertEqual(row["reported_outcome"], "OK" if actual.wasSuccessful() else "FAILED")
                self.assertEqual(row["reported_counts"], {
                    key: len(value) for key, value in (("failures", actual.failures), ("errors", actual.errors),
                        ("skipped", actual.skipped), ("expected_failures", actual.expectedFailures),
                        ("unexpected_successes", actual.unexpectedSuccesses)) if value})
                self.assertNotIn("PRIVATE", json.dumps(row))

    def test_outer_flags_cannot_mask_missing_or_incomplete_stream_flags(self):
        for location in ("result", "stdout", "stderr"):
            for key, values in (("complete", (None, False, 1, "true")),
                                ("truncated", (None, True, 0, "false")),
                                ("utf8_valid", (None, False, 1, "true"))):
                if location == "result" and key == "utf8_valid":
                    continue
                for value in (*values, "missing"):
                    result = receipt()
                    target = result if location == "result" else result[location]
                    if value == "missing":
                        target.pop(key)
                    else:
                        target[key] = value
                    with self.subTest(location=location, key=key, value=value):
                        self.assertEqual(receipt_observations(result), [])

    def test_multiple_identical_or_conflicting_reports_in_either_stream_reject(self):
        failed = SUMMARY.replace("OK", "FAILED (errors=1)")
        for stdout, stderr in (("", SUMMARY + SUMMARY), ("", failed + SUMMARY),
                               (SUMMARY, SUMMARY), (failed, SUMMARY),
                               ("Ran invalid tests in unknown\n", SUMMARY),
                               ("FAILED (errors=1)\n", SUMMARY)):
            with self.subTest(stdout=stdout, stderr=stderr):
                self.assertEqual(receipt_observations(receipt(stdout, stderr)), [])

    def test_program_alias_and_argv_never_determine_identity_or_report(self):
        result = receipt(stderr="unknown format\n")
        result.update(command="unittest", argv=["python", "-m", "unittest"], output=SUMMARY)
        self.assertEqual(receipt_observations(result), [])
        result = receipt()
        result.update(command="definitely-not-unittest", argv=["printf", SUMMARY])
        row = receipt_observations(result)[0]
        self.assertEqual(row["reported_tests"], 17)
        self.assertNotIn("program", set(row))
        self.assertIn("does not prove program identity", row["program_report_note"])

    def test_unknown_malformed_nonterminal_or_contradictory_grammar_rejects(self):
        for text in ("17 passed in 0.001s\n", '{"tests":17,"result":"OK"}',
                SUMMARY + "trailing log\n", SUMMARY + "\n", SUMMARY.replace("\n\n", "\n"),
                SUMMARY.replace("tests", "test"), SUMMARY.replace("17", "01"),
                SUMMARY.replace("0.001", "0.1"), SUMMARY.replace("17", "\u0661\u0667"),
                SUMMARY.replace("OK", "OK (failures=1)"), SUMMARY.replace("OK", "FAILED"),
                SUMMARY.replace("OK", "FAILED (skipped=1)"),
                SUMMARY.replace("OK", "FAILED (errors=1, errors=2)"),
                SUMMARY.replace("OK", "FAILED (errors=1, failures=2)"),
                SUMMARY.replace("OK", "FAILED (errors=0)"),
                SUMMARY.replace("OK", "FAILED (errors=-1)"),
                SUMMARY.replace("OK", "FAILED (private=value)"),
                SUMMARY.replace("OK", "OK ( skipped=1)"),
                SUMMARY.replace("OK", "NO TESTS RAN"),
                SUMMARY.replace("OK", "\x1b[32mOK\x1b[0m")):
            with self.subTest(text=text):
                self.assertEqual(receipt_observations(receipt(stderr=text)), [])

    def test_unknown_shapes_and_exceptional_results_reject(self):
        for result in (None, [], True, "unittest", {}, {"stdout": SUMMARY},
                {**receipt(), "stderr": SUMMARY}, {**receipt(), "stdout": None}):
            with self.subTest(result=result):
                self.assertEqual(receipt_observations(result), [])
        for changes in ({"timed_out": True}, {"timed_out": 0}, {"outcome": "timeout"},
                {"outcome": "launch_error"}, {"outcome": "effects_unknown"}, {"outcome": "not_started"},
                {"exception": "PRIVATE"}, {"launch_error": "PRIVATE"}, {"cleanup_error": "PRIVATE"}):
            with self.subTest(changes=changes):
                self.assertEqual(receipt_observations({**receipt(), **changes}), [])

    def test_bounds_byte_fidelity_and_recorded_excerpt_are_required(self):
        body = "x" * (8192 - len(SUMMARY) - 1) + "\n" + SUMMARY
        self.assertEqual(len(receipt_observations(receipt(stderr=body))), 1)
        for text in ("x" + body, "\u4e2d" * 3000 + "\n" + SUMMARY):
            result = receipt(stderr=text)
            result["stderr"].update(complete=True, truncated=False, excerpt=text)
            self.assertEqual(receipt_observations(result), [])
        for changes in ({"bytes": 1}, {"bytes": True}, {"bytes": None},
                {"preview_mode": "head_tail"}, {"tail_excerpt": "PRIVATE"},
                {"excerpt": "\ud800"}, {"excerpt": "private\r" + SUMMARY}):
            result = receipt()
            result["stderr"].update(changes)
            with self.subTest(changes=changes):
                self.assertEqual(receipt_observations(result), [])
        for key in ("excerpt", "bytes"):
            result = receipt()
            del result["stderr"][key]
            self.assertEqual(receipt_observations(result), [])
        result = receipt()
        result["stderr"]["preview"] = result["stderr"].pop("excerpt")
        self.assertEqual(receipt_observations(result), [])

    def test_command_failure_and_output_ok_are_independent_in_both_directions(self):
        for change in ({"exit_code": 7}, {"status": "error"}, {"outcome": "failed"}):
            with self.subTest(change=change):
                row = receipt_observations({**receipt(), **change})[0]
                self.assertEqual(row["reported_outcome"], "OK")
                self.assertIn("Output reports OK while command metadata reports failure", row["program_report_note"])
        result = receipt(stderr=SUMMARY.replace("OK", "FAILED (errors=1)"))
        row = receipt_observations(result)[0]
        self.assertEqual(row["reported_outcome"], "FAILED")
        self.assertIn("Output reports FAILED while command metadata reports success", row["program_report_note"])

    def test_result_is_unchanged_and_observations_do_not_alias_input(self):
        result = receipt(stderr=SUMMARY.replace("OK", "FAILED (errors=1)"))
        original = copy.deepcopy(result)
        observed = receipt_observations(result)
        self.assertEqual(result, original)
        observed[0]["reported_counts"]["errors"] = 999
        observed[0]["program_report_note"] = "changed"
        self.assertEqual(result, original)
        self.assertEqual(receipt_observations(result)[0]["reported_counts"], {"errors": 1})
        result["stderr"]["truncated"] = True
        original = copy.deepcopy(result)
        self.assertEqual(receipt_observations(result), [])
        self.assertEqual(result, original)
