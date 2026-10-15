from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from cater.drivers import DriverResponse
from cater.runtime import Runtime
from test_work import WorkDriver, item


class ReviewRepairDriver(WorkDriver):
    review_response_repair = True

    def __init__(self):
        super().__init__()
        self.review_count = 0
        self.mode = "repair"

    def complete(self, **kwargs):
        response = super().complete(**kwargs)
        if kwargs["context"]["work"].get("phase") != "review":
            return response
        self.review_count += 1
        raw = json.loads(response.text)
        if self.mode == "contained":
            return DriverResponse("{}", tokens=7, meta={"contained": True, "token_usage": "reported"})
        if self.mode == "unknown":
            for row in raw["checks"]:
                row.update(status="unknown", artifact_seq=None, quote="", reason="缺少调用者的输入约定，尚不能确认覆盖范围。")
        elif self.mode == "failed":
            for row in raw["checks"]:
                row.update(status="failed", reason="正文不符合这项要求。")
        elif self.mode == "always_bad" or self.review_count == 1:
            for row in raw["checks"]:
                row["quote"] = "并不存在的原文"
        return DriverResponse(json.dumps(raw), tokens=7,
                              meta={"token_usage": "reported", "review_response_repair": True})


class ReviewRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "repair.db"
        self.driver = ReviewRepairDriver()
        self.drivers = {r: self.driver for r in ("product", "dev", "test", "design")}
        self.rt = Runtime(self.db, drivers=self.drivers)
        self.addCleanup(self.rt.close)
        self.task = self.rt.create_task("保持实际成果，纠正错误的复核引用")
        self.rt.plan_work(self.task, [item("draft")])

    def repairs(self):
        return [e for e in self.rt.log.iter_events(task_id=self.task, event_types=("work.review_deferred",))
                if e.payload.get("response_repair")]

    def test_bad_citation_gets_one_correction_against_same_artifacts_and_preserves_usage(self):
        self.rt.collaborate(self.task)
        self.assertTrue(self.rt.work.view(self.task)["stage_accepted"])
        self.assertEqual(self.driver.review_count, 2)
        self.assertEqual(len(self.repairs()), 1)
        work = [c for c in self.driver.calls if c.get("phase") != "review"]
        reviews = [c for c in self.driver.calls if c.get("phase") == "review"]
        self.assertEqual(len(work), 1)
        self.assertEqual(reviews[0]["artifacts"], reviews[1]["artifacts"])
        self.assertEqual(reviews[1]["response_correction"]["issues"][0]["issue"], "quote_not_found")
        fields = reviews[0]["response_schema"]["properties"]["checks"]["items"]["properties"]
        self.assertEqual(fields["id"]["enum"], ["meaning"])
        self.assertEqual(fields["artifact_seq"]["enum"], [None, reviews[0]["artifacts"][0]["seq"]])
        self.assertEqual(self.rt._reported_driver_tokens("test"), 14)
        events = self.rt.log.read(task_id=self.task)
        self.assertTrue(any(e.payload.get("checks", [{}])[0].get("response_issue") == "quote_not_found"
                            for e in events if e.type == "work.reviewed"))
        self.assertTrue(self.rt.verify_chain().ok)

    def test_repeated_bad_response_does_not_loop_and_correction_limit_survives_restart(self):
        self.driver.mode = "always_bad"
        self.rt.collaborate(self.task)
        self.assertFalse(self.rt.work.view(self.task)["stage_accepted"])
        self.assertEqual(self.driver.review_count, 2)
        with Runtime(self.db, drivers=self.drivers) as reopened:
            reopened.collaborate(self.task)
        self.assertEqual(self.driver.review_count, 3)
        self.assertEqual(len(self.repairs()), 1)
        self.assertEqual(len([c for c in self.driver.calls if c.get("phase") != "review"]), 1)

    def test_genuine_unknown_retains_specific_reason_and_is_not_a_format_retry(self):
        self.driver.mode = "unknown"
        self.rt.collaborate(self.task)
        check = self.rt.work.view(self.task)["items"][0]["checks"][0]
        self.assertEqual(check["status"], "unknown")
        self.assertIn("调用者的输入约定", check["reason"])
        self.assertNotIn("response_issue", check)
        self.assertEqual(self.driver.review_count, 1)
        self.assertEqual(self.repairs(), [])

    def test_negative_quality_judgment_is_not_retried_until_it_passes(self):
        self.driver.mode = "failed"
        self.rt.collaborate(self.task)
        self.assertFalse(self.rt.work.view(self.task)["stage_accepted"])
        self.assertEqual(self.driver.review_count, 1)
        self.assertEqual(self.repairs(), [])

    def test_contained_response_does_not_trigger_an_automatic_second_invocation(self):
        self.driver.mode = "contained"
        self.rt.collaborate(self.task)
        self.assertEqual(self.driver.review_count, 1)
        self.assertEqual(self.repairs(), [])
        check = self.rt.work.view(self.task)["items"][0]["checks"][0]
        self.assertIn("复核调用未完整返回", check["reason"])
        self.assertNotIn("response_issue", check)

    def test_unknown_driver_cannot_enable_repair_through_response_metadata(self):
        self.driver.review_response_repair = False
        self.rt.collaborate(self.task)
        self.assertFalse(self.rt.work.view(self.task)["stage_accepted"])
        self.assertEqual(self.driver.review_count, 1)
        self.assertEqual(self.repairs(), [])

    def test_scope_change_during_review_discards_result_instead_of_retrying(self):
        def change(work):
            if work.get("phase") == "review":
                self.rt.command(self.task, "保持原来的实现，但补一个新要求")
        self.driver.after = change
        self.rt.collaborate(self.task)
        self.assertEqual(self.driver.review_count, 1)
        self.assertEqual(self.repairs(), [])
        self.assertFalse(self.rt.work.view(self.task)["scope_current"])

    def test_brief_and_progress_report_explain_reply_error_without_claiming_live_work(self):
        self.driver.mode = "always_bad"
        result = self.rt.collaborate(self.task)
        brief = result["brief"]
        self.assertEqual(brief["checkpoint"]["state"], "review_response_issue")
        self.assertIn("这不表示成果本身被否决", brief["root_cause"])
        self.assertIn("目前没有已确认正在运行", brief["root_cause"])
        before = len(self.driver.calls)
        self.rt.command(self.task, "进度怎么样？")
        report = self.rt.log.read(task_id=self.task)[-1]
        self.assertEqual(report.type, "task.progress_reported")
        self.assertEqual(report.payload["text"], brief["checkpoint"]["summary"])
        self.assertEqual(len(self.driver.calls), before)

    def test_missing_local_receipt_is_not_explained_as_response_format_failure(self):
        self.rt.plan_work(self.task, [item("draft", local=True)])
        result = self.rt.collaborate(self.task)
        checkpoint = result["brief"]["checkpoint"]
        self.assertEqual(checkpoint["state"], "waiting_local_check")
        self.assertIn("实际检查回执", checkpoint["summary"])
        self.assertEqual(self.driver.review_count, 0)
        self.assertEqual(self.repairs(), [])
