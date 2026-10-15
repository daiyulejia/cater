from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from cater.artifacts import normalize_deliverables
from cater.events import Actor
from cater.runtime import Runtime
from examples import codex_cli_driver as adapter
from test_work import item


class CheckRelevanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "evidence.db"
        self.rt = Runtime(self.db)
        self.addCleanup(self.rt.close)
        self.task = self.rt.create_task("检查真实行为，不把命令成功等同于验收")
        self.rt.plan_work(self.task, [item("a", local=True), item("b", deps=["a"])])
        claim = self.rt.work.claim(self.task, "a")
        body = normalize_deliverables([{"title": "结果", "summary": "待核验", "content": "assert 2 + 2 == 4\n",
                                       "format": "code", "filename": "check.py"}])[0]
        self.artifact = self.rt.log.emit(type="deliverable.published", actor=Actor.agent("dev", "worker"),
            task_id=self.task, payload={**body, "work_item_id": "a", "attempt_id": claim.payload["attempt_id"]})
        self.submission = self.rt.work.submit(self.task, claim, [self.artifact.seq])

    def verify(self, command=None):
        return self.rt.verify_work(self.task, "a", "behavior", command or [sys.executable, "check.py"])[0]

    def rows(self, receipt):
        return [{"id": c, "status": "passed", "reason": "已核对正文和命令检查的断言范围。",
                 "artifact_seq": self.artifact.seq, "quote": "assert 2 + 2 == 4",
                 "receipt_seq": receipt.seq if c == "behavior" else None}
                for c in ("meaning", "behavior")]

    def review(self, rows):
        return self.rt.work.review(self.task, "a", reviewer="test", submission_seq=self.submission.seq, results=rows)

    def assert_blocked(self):
        view = self.rt.work.view(self.task)
        self.assertFalse(view["stage_accepted"])
        self.assertEqual(view["items"][1]["status"], "blocked")

    def test_exit_zero_without_relevance_assessment_cannot_release_dependency(self):
        receipt = self.verify([sys.executable, "-c", "print('OK')"])
        event = self.review(self.rows(receipt)[:1])
        check = event.payload["checks"][1]
        self.assertEqual(event.payload["status"], "submitted")
        self.assertEqual(check["execution_status"], "passed")
        self.assertEqual(check["receipt_seq"], receipt.seq)
        self.assertEqual(check["status"], "unknown")
        self.assertIn("复核回复缺少", check["reason"])
        self.assert_blocked()

    def test_reviewer_can_reject_irrelevant_success_without_falsifying_execution(self):
        receipt = self.verify([sys.executable, "-c", "print('OK')"])
        rows = self.rows(receipt)
        rows[1].update(status="failed", reason="命令只打印 OK，未运行交付正文中的断言。")
        event = self.review(rows)
        check = event.payload["checks"][1]
        self.assertEqual(event.payload["status"], "rework")
        self.assertEqual(check["execution_status"], "passed")
        self.assertEqual(check["status"], "failed")
        self.assertIn("未运行", check["reason"])
        self.assert_blocked()

    def test_reviewer_unknown_remains_at_checkpoint(self):
        receipt = self.verify()
        rows = self.rows(receipt)
        rows[1].update(status="unknown", reason="只有执行回执，尚未核对断言是否覆盖要求。")
        self.assertEqual(self.review(rows).payload["status"], "submitted")
        self.assert_blocked()

    def test_relevance_requires_exact_current_receipt_valid_quote_and_one_assessment(self):
        receipt = self.verify()
        stale = self.verify()
        mutations = [
            lambda rows: rows[1].pop("receipt_seq"),
            lambda rows: rows[1].update(receipt_seq=True),
            lambda rows: rows[1].update(receipt_seq=receipt.seq),
            lambda rows: rows[1].update(receipt_seq=stale.seq + 100),
            lambda rows: rows[1].update(quote="不存在的正文"),
            lambda rows: rows.append(copy.deepcopy(rows[1])),
        ]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                rows = self.rows(stale)
                mutate(rows)
                self.assertEqual(self.review(rows).payload["status"], "submitted")
                self.assert_blocked()

    def test_failed_execution_cannot_be_overridden_by_positive_relevance_review(self):
        receipt = self.verify([sys.executable, "-c", "raise SystemExit(1)"])
        check = self.review(self.rows(receipt)).payload["checks"][1]
        self.assertEqual(check["execution_status"], "failed")
        self.assertEqual(check["status"], "failed")
        self.assert_blocked()

    def test_missing_receipt_cannot_be_replaced_by_reviewer_claim(self):
        rows = self.rows(SimpleNamespace(seq=12345))
        event = self.review(rows)
        self.assertEqual(event.payload["checks"][1]["status"], "unknown")
        self.assertNotIn("execution_status", event.payload["checks"][1])
        self.assert_blocked()

    def test_current_execution_and_explicit_relevance_release_dependency_and_survive_reopen(self):
        receipt = self.verify()
        event = self.review(self.rows(receipt))
        self.assertEqual(event.payload["status"], "accepted")
        check = event.payload["checks"][1]
        self.assertEqual(check["execution_status"], "passed")
        self.assertEqual(check["artifact_seq"], self.artifact.seq)
        with Runtime(self.db) as reopened:
            view = reopened.work.view(self.task)
            self.assertEqual(view["items"][0]["checks"][1], check)
            self.assertEqual(view["items"][1]["status"], "ready")
            self.assertTrue(reopened.verify_chain().ok)

    def test_adapter_accepts_receipt_reference_without_executing_commands(self):
        row = {"id": "behavior", "status": "unknown", "reason": "检查范围不足。",
               "artifact_seq": 1, "quote": "assert True", "receipt_seq": 2}
        self.assertTrue(adapter._valid_review_payload({"checks": [row]}))
        for invalid in (True, 0, -1, "2", []):
            self.assertFalse(adapter._valid_review_payload({"checks": [{**row, "receipt_seq": invalid}]}))
        prompt = adapter._model_prompt({"context": {"work": {"phase": "review"}}})
        self.assertIn("Exit zero alone is insufficient", prompt)
        self.assertIn("receipt_seq", prompt)
        self.assertIn("receipt_seq", adapter.REVIEW_SCHEMA["properties"]["checks"]["items"]["required"])
