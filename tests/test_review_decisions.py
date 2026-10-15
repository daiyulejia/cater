"""Small review decisions retain real execution and version boundaries."""

import sys
import tempfile
import unittest
from pathlib import Path

from cater.artifacts import normalize_deliverables
from cater.events import Actor
from cater.runtime import Runtime
from cater.work import WorkConflict
from test_work import WorkDriver, item


class ReviewDecisionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        driver = WorkDriver()
        self.rt = Runtime(Path(temp.name) / "review.db", drivers={
            role: driver for role in ("product", "design", "dev", "test")})
        self.addCleanup(self.rt.close)
        self.task = self.rt.create_task("Deliver a working program")
        self.rt.plan_work(self.task, [item("code", local=True)])
        claim = self.rt.work.claim(self.task, "code")
        body = normalize_deliverables([{"title": "Code", "summary": "Submitted source",
            "filename": "main.py", "format": "code", "content": "print(42)\n"}])[0]
        self.artifact = self.rt.log.emit(type="deliverable.published", task_id=self.task,
            actor=Actor.agent("dev", "worker"), payload={**body, "work_item_id": "code",
                                                        "attempt_id": claim.payload["attempt_id"]})
        self.submission = self.rt.work.submit(self.task, claim, [self.artifact.seq])

    def start(self):
        current = self.rt.work.view(self.task)["items"][0]
        return self.rt.work.start_review(self.task, "code", reviewer="test",
            submission_seq=self.submission.seq,
            expected_check_seqs={k: v.seq for k, v in self.rt.work.checks(self.task, current).items()},
            expected_review_seq=current.get("review_seq"),
            expected_review_attempt_seq=current.get("review_lifecycle", {}).get("attempt_seq"))

    def decide(self, start, choice="approve", **kwargs):
        return self.rt.work.review_decision(self.task, "code", reviewer="test",
            submission_seq=self.submission.seq, review_attempt_seq=start.seq,
            decision={"decision": choice, "reason": "Inspected the exact source and observed output.", **kwargs})

    def verify(self, code="print(42)"):
        return self.rt.verify_work(self.task, "code", "behavior", [sys.executable, "-c", code],
                                   record_output=True)[0]

    def test_approval_binds_real_inputs_without_model_transcribing_quotes(self):
        receipt = self.verify()
        result = self.decide(self.start())
        self.assertEqual(result.payload["status"], "accepted")
        self.assertEqual(result.payload["review_protocol"], "decision_v1")
        for check in result.payload["checks"]:
            self.assertNotIn("quote", check)
            self.assertEqual(check["evidence_binding"]["artifacts"], [
                {"seq": self.artifact.seq, "sha256": self.artifact.payload["content_sha256"]}])
        self.assertEqual(result.payload["checks"][1]["receipt_seq"], receipt.seq)

    def test_model_cannot_approve_missing_or_failed_execution(self):
        for available in (False, True):
            with self.subTest(available=available):
                if available:
                    self.verify("raise SystemExit(1)")
                started = self.start()
                result = self.decide(started)
                self.assertEqual(result.payload["status"], "submitted")
                self.assertEqual(result.payload["checks"][1]["status"], "unknown")
                self.rt.work.finish_review(self.task, started.seq, status="completed", reason="Returned")

    def test_concrete_source_defect_routes_back_to_author(self):
        self.verify()
        result = self.decide(self.start(), "revise_artifact", criterion_ids=["meaning"])
        self.assertEqual(result.payload["status"], "rework")
        self.assertEqual([c["status"] for c in result.payload["checks"]], ["failed", "passed"])

    def test_invalid_criteria_and_unknown_decisions_never_become_approval(self):
        started = self.start()
        for ids in ([], ["unknown"], ["meaning", "meaning"], [True], "meaning"):
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                self.decide(started, criterion_ids=ids)
        for choice in ("probably fine", [], {}):
            with self.subTest(choice=choice), self.assertRaises(ValueError):
                self.decide(started, choice)
        with self.assertRaises(ValueError):
            self.decide(started, "revise_check", criterion_ids=["meaning"])

    def test_format_correction_cannot_upgrade_genuine_unknown_on_same_inputs(self):
        self.verify()
        started = self.start()
        self.decide(started, "needs_information")
        result = self.decide(started)
        self.assertEqual(result.payload["status"], "submitted")
        self.assertTrue(all(c["status"] == "unknown" for c in result.payload["checks"]))

    def test_old_decision_cannot_accept_changed_requirements(self):
        self.verify()
        started = self.start()
        self.rt.command(self.task, "Change the program", goal="Different output")
        with self.assertRaises(WorkConflict):
            self.decide(started)
