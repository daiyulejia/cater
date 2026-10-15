"""Host observation citations never turn a plan or stale receipt into evidence."""

import copy
import hashlib
import json
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cater.artifacts import normalize_deliverables
from cater.browser_checks import normalize_plan
from cater.events import Actor
from cater.review_contract import assessment, browser_observations
from cater.runtime import Runtime
from cater.verification import CheckResult
from test_work import item


def receipt_fixture():
    return {"seq": 27, "check_kind": "browser", "outcome": "passed", "browser_report": {
        "outcome": "passed", "phase": "execution", "truncated": False,
        "steps": [{"index": i, "action": "assert_js", "status": "passed", "actual": True,
                   "label": "PLAN LABEL ONLY", "expected": "PLAN EXPECTATION ONLY", "check": "PLAN CODE ONLY"}
                  for i in range(1, 4)]}}


def review_row(quote, receipt_seq=27, status="passed"):
    return {"id": "behavior", "status": status, "reason": "Inspected the actual browser observation",
            "artifact_seq": None, "quote": quote, "receipt_seq": receipt_seq}


class BrowserObservationTests(unittest.TestCase):
    def test_all_boolean_successes_have_stable_host_quotes(self):
        receipt = receipt_fixture()
        original = copy.deepcopy(receipt)
        quotes = browser_observations(receipt)
        self.assertEqual(quotes, [f'{{"index": {i}, "action": "assert_js", "status": "passed", "actual": true}}'
                                  for i in range(1, 4)])
        for quote in quotes:
            judgment, issue, _ = assessment([review_row(quote)], "behavior", {}, receipt)
            self.assertIsNone(issue)
            self.assertEqual(judgment["status"], "passed")
        self.assertEqual(receipt, original)

    def test_actions_without_actual_do_not_invent_one(self):
        receipt = receipt_fixture()
        receipt["browser_report"]["steps"] = [{"index": 1, "action": "click", "status": "passed"}]
        self.assertEqual(json.loads(browser_observations(receipt)[0]),
                         {"index": 1, "action": "click", "status": "passed"})

    def test_plan_fields_cannot_be_cited_as_observations(self):
        receipt = receipt_fixture()
        for quote in ("PLAN LABEL ONLY", "PLAN EXPECTATION ONLY", "PLAN CODE ONLY"):
            with self.subTest(quote=quote):
                self.assertEqual(assessment([review_row(quote)], "behavior", {}, receipt)[1], "quote_not_found")

    def test_malformed_or_unexecuted_steps_do_not_produce_success_quotes(self):
        changes = [{"index": value} for value in (True, 0, 2, "1", None)]
        changes += [{"action": value} for value in (None, [], "invented")]
        changes += [{"status": value} for value in ("not_run", "running", "unknown", True)]
        changes += [{"actual": value} for value in (False, 1, "true", None, {}, [])]
        changes += [{"error": "Execution did not succeed"}]
        for change in changes:
            receipt = receipt_fixture()
            receipt["browser_report"]["steps"][0].update(change)
            with self.subTest(change=change):
                quotes = browser_observations(receipt)
                self.assertEqual(quotes, [change["error"]] if "error" in change else [])
        receipt = receipt_fixture()
        del receipt["browser_report"]["steps"][0]["actual"]
        self.assertEqual(browser_observations(receipt), [])

    def test_failure_keeps_actual_false_and_original_errors(self):
        receipt = receipt_fixture()
        report = receipt["browser_report"]
        receipt["outcome"] = report["outcome"] = "failed"
        report["error"] = "one or more steps failed or were not run"
        report["steps"][0].update(status="failed", actual=False, error="assert_js: expression did not return exactly true")
        quotes = browser_observations(receipt)
        self.assertEqual(quotes[:2], [report["error"], report["steps"][0]["error"]])
        self.assertEqual(json.loads(quotes[2]), {"index": 1, "action": "assert_js", "status": "failed", "actual": False})
        self.assertEqual(len(quotes), 3)
        self.assertIsNone(assessment([review_row(quotes[2], status="failed")], "behavior", {}, receipt)[1])

    def test_failed_step_after_true_result_stays_failed(self):
        receipt = receipt_fixture()
        report = receipt["browser_report"]
        report["outcome"] = "failed"
        report["steps"][0].update(status="failed", error="page left the permitted artifact document")
        self.assertEqual(json.loads(browser_observations(receipt)[-1])["status"], "failed")

    def test_incomplete_unavailable_or_validation_reports_have_no_canonical_success(self):
        for change in ({"truncated": True}, {"phase": "validation"}, {"outcome": "unavailable"},
                       {"steps": None}, {"steps": [{"label": "PLAN LABEL ONLY", "expected": True}]}):
            receipt = receipt_fixture()
            receipt["browser_report"].update(change)
            with self.subTest(change=change):
                self.assertEqual(browser_observations(receipt), [])
        receipt = receipt_fixture()
        receipt["outcome"] = "unavailable"
        self.assertEqual(browser_observations(receipt), [])

    def test_legacy_error_and_string_actual_quotes_remain_compatible(self):
        receipt = {"check_kind": "browser", "browser_report": {"error": "page error", "steps": [
            {"error": "assertion failed", "actual": "Observed blank canvas", "label": "PLAN LABEL ONLY"}]}}
        self.assertEqual(browser_observations(receipt), ["page error", "assertion failed", "Observed blank canvas"])

    def test_canonical_quote_requires_exact_integer_receipt_identity(self):
        receipt = receipt_fixture()
        quote = browser_observations(receipt)[0]
        for seq in (None, True, 0, 26, "27", 27.0):
            with self.subTest(seq=seq):
                self.assertEqual(assessment([review_row(quote, seq)], "behavior", {}, receipt)[1], "invalid_receipt")
        for seq in (None, True, 0, "27", 27.0):
            with self.subTest(receipt_seq=seq):
                self.assertEqual(assessment([review_row(quote, 1 if seq is True else 27)], "behavior", {},
                                            {**receipt, "seq": seq})[1], "invalid_receipt")


class BrowserObservationBindingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.enterContext(patch.object(socket, "socket", side_effect=AssertionError("network forbidden")))
        self.rt = Runtime(Path(temporary.name) / "isolated.db")
        self.addCleanup(self.rt.close)
        self.task = self.rt.create_task("Check submitted HTML")
        self.rt.plan_work(self.task, [item("page", local=True)])
        claim = self.rt.work.claim(self.task, "page")
        body = normalize_deliverables([{"title": "Page", "summary": "Fixture", "filename": "index.html",
                                       "format": "code", "content": "<!doctype html><p>Ready</p>"}])[0]
        self.artifact = self.rt.log.emit(type="deliverable.published", task_id=self.task,
            actor=Actor.agent("dev", "worker"), payload={**body, "work_item_id": "page",
                                                       "attempt_id": claim.payload["attempt_id"]})
        self.submission = self.rt.work.submit(self.task, claim, [self.artifact.seq])
        self.plan = normalize_plan({"entrypoint": "index.html", "steps": [
            {"action": "assert_js", "label": "Inspect page", "expression": "() => true"}]}, ["index.html"])

    def receipt(self, *, failed=False, incomplete=False):
        report = receipt_fixture()["browser_report"]
        report["steps"] = report["steps"][:1]
        report["counts"] = {"page_errors": 0}
        report["plan_sha256"] = hashlib.sha256(json.dumps(self.plan, ensure_ascii=False).encode()).hexdigest()
        if failed:
            report["outcome"] = "failed"
            report["steps"][0].update(status="failed", actual=False, error="observed failure")
        text = json.dumps(report)
        result = CheckResult(payload={"outcome": "failed" if failed else "passed", "exit_code": int(failed),
                                      "stdout": {"complete": not incomplete, "truncated": incomplete},
                                      "stderr": {"complete": True}}, stdout_preview=text, stderr_preview="")
        with patch("cater.runtime.run_check", return_value=result):
            return self.rt.verify_work(self.task, "page", "behavior", [sys.executable, "unused"],
                                       browser_plan=self.plan)[0]

    def review(self, receipt, *, status="passed", quote=None, receipt_seq=None):
        quote = quote or browser_observations(receipt.payload)[-1]
        rows = [{"id": "meaning", "status": "passed", "reason": "Read exact source",
                 "artifact_seq": self.artifact.seq, "quote": self.artifact.payload["content"], "receipt_seq": None},
                review_row(quote, receipt.seq if receipt_seq is None else receipt_seq, status)]
        return self.rt.work.review(self.task, "page", reviewer="test", submission_seq=self.submission.seq, results=rows)

    def test_current_successful_boolean_receipt_can_pass_independent_review(self):
        receipt = self.receipt()
        result = self.review(receipt)
        self.assertEqual(result.payload["status"], "accepted")
        self.assertEqual(result.payload["checks"][-1]["receipt_seq"], receipt.seq)

    def test_failed_receipt_cannot_be_upgraded_by_canonical_quote(self):
        receipt = self.receipt(failed=True)
        result = self.review(receipt)
        self.assertEqual(result.payload["status"], "submitted")
        self.assertEqual(result.payload["checks"][-1]["execution_status"], "failed")
        self.assertNotEqual(result.payload["checks"][-1]["status"], "passed")
        result = self.review(receipt, status="failed")
        self.assertEqual(result.payload["status"], "rework")

    def test_incomplete_receipt_cannot_pass(self):
        receipt = self.receipt(incomplete=True)
        self.assertEqual(browser_observations(receipt.payload), [])
        result = self.review(receipt, quote=browser_observations(receipt_fixture())[0])
        self.assertEqual(result.payload["checks"][-1]["execution_status"], "unavailable")
        self.assertNotEqual(result.payload["status"], "accepted")

    def test_wrong_receipt_or_version_binding_cannot_pass(self):
        receipt = self.receipt()
        self.assertEqual(self.review(receipt, receipt_seq=receipt.seq + 1).payload["checks"][-1]["response_issue"],
                         "invalid_receipt")
        for change in ({"submission_seq": self.submission.seq + 1}, {"submission_digest": "wrong"},
                       {"plan_seq": -1}, {"scope": {"direction_seq": -1}}):
            forged = self.rt.log.emit(type="verification.completed", task_id=self.task,
                                     actor=Actor.system("local_verifier"), payload={**receipt.payload, **change})
            with self.subTest(change=change):
                result = self.review(forged)
                self.assertNotEqual(result.payload["status"], "accepted")
                self.assertEqual(result.payload["checks"][-1]["receipt_seq"], receipt.seq)


if __name__ == "__main__":
    unittest.main()
