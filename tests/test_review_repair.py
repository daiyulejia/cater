from __future__ import annotations

import json
import sys
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
        self.review_transform = None

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
        if self.review_transform:
            self.review_transform(raw, kwargs["context"]["work"])
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

    def mixed_review(self, *, first="unknown", second="passed", repaired="passed", local=False):
        contract = item("draft")
        if local:
            contract["criteria"][0]["kind"] = "local_check"
        contract["criteria"].append({"id": "citation", "kind": "review", "text": "独立核对正文引用"})
        self.rt.plan_work(self.task, [contract])

        def transform(raw, context):
            rows = {r["id"]: r for r in raw["checks"]}
            for row in rows.values():
                row["quote"] = context["artifacts"][0]["content"]
            judgment = first if self.driver.review_count == 1 else second
            rows["meaning"].update(status=judgment, reason="缺少调用者输入约定，不能确认覆盖范围。"
                                   if self.driver.review_count == 1 else "纠错轮给出的新判断。")
            if judgment == "unknown":
                rows["meaning"].update(artifact_seq=None, quote="")
            if self.driver.review_count == 1:
                rows["citation"]["quote"] = "并不存在的原文"
            else:
                rows["citation"].update(status=repaired, reason="纠错轮核对了正文。")

        self.driver.review_transform = transform

    def test_correction_cannot_upgrade_untouched_genuine_unknown(self):
        self.mixed_review()
        self.rt.collaborate(self.task)
        current = self.rt.work.view(self.task)
        checks = {c["id"]: c for c in current["items"][0]["checks"]}
        self.assertFalse(current["stage_accepted"])
        self.assertEqual(checks["meaning"]["status"], "unknown")
        self.assertEqual(checks["meaning"]["reason"], "缺少调用者输入约定，不能确认覆盖范围。")
        self.assertNotIn("response_issue", checks["meaning"])
        self.assertEqual(checks["citation"]["status"], "passed")
        self.assertEqual(self.driver.review_count, 2)
        self.assertEqual(len(self.repairs()), 1)
        reviews = list(self.rt.log.iter_events(task_id=self.task, event_types=("work.reviewed",)))
        self.assertEqual([e.payload["status"] for e in reviews], ["submitted", "submitted"])
        self.assertEqual(len([c for c in self.driver.calls if c.get("phase") != "review"]), 1)

    def test_correction_retains_first_unknown_reason_when_still_unknown(self):
        self.mixed_review(second="unknown")
        self.rt.collaborate(self.task)
        check = self.rt.work.view(self.task)["items"][0]["checks"][0]
        self.assertEqual(check["status"], "unknown")
        self.assertEqual(check["reason"], "缺少调用者输入约定，不能确认覆盖范围。")

    def test_correction_keeps_new_negative_findings_on_untouched_rows(self):
        for first, second in (("unknown", "failed"), ("passed", "failed"), ("passed", "unknown")):
            with self.subTest(first=first, second=second):
                self.driver.review_count = 0
                self.mixed_review(first=first, second=second)
                self.rt.collaborate(self.task)
                current = self.rt.work.view(self.task)
                self.assertFalse(current["stage_accepted"])
                check = current["items"][0]["checks"][0]
                self.assertEqual(check["status"], second)
                self.assertEqual(check["reason"], "纠错轮给出的新判断。")
                self.assertNotIn("response_issue", check)
                self.assertEqual(self.driver.review_count, 2)

    def test_correction_keeps_new_failure_on_repaired_row(self):
        self.mixed_review(repaired="failed")
        self.rt.collaborate(self.task)
        checks = {c["id"]: c for c in self.rt.work.view(self.task)["items"][0]["checks"]}
        self.assertEqual(checks["meaning"]["status"], "unknown")
        self.assertEqual(checks["citation"]["status"], "failed")
        self.assertNotIn("response_issue", checks["citation"])

    def test_correction_cannot_upgrade_unknown_check_relevance_with_real_receipt(self):
        self.mixed_review(local=True)
        self.rt.collaborate(self.task)
        self.assertEqual(self.driver.review_count, 0)
        receipt, _ = self.rt.verify_work(self.task, "draft", "meaning", [sys.executable, "check.py"])
        self.rt.collaborate(self.task)
        current = self.rt.work.view(self.task)
        check = current["items"][0]["checks"][0]
        self.assertFalse(current["stage_accepted"])
        self.assertEqual(check["status"], "unknown")
        self.assertEqual(check["execution_status"], "passed")
        self.assertEqual(check["receipt_seq"], receipt.seq)
        self.assertEqual(check["reason"], "缺少调用者输入约定，不能确认覆盖范围。")
        self.assertEqual(self.driver.review_count, 2)

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
        messages = [e for e in events if e.type == "msg.said" and e.actor.id == "test"]
        self.assertEqual([e.payload["phase"] for e in messages], ["review_response_correction", "review"])
        self.assertIn("复核引用或格式正在校正", messages[0].payload["text"])
        self.assertEqual(messages[0].payload["speech_source"], "system_receipt")
        self.assertEqual(messages[1].payload["speech_source"], "review_assessment")
        self.assertNotIn("这一项的阶段标准通过了", messages[1].payload["text"])
        review = next(e for e in events if e.seq == messages[1].payload["review_seq"])
        self.assertIn(review.payload["checks"][0]["reason"], messages[1].payload["text"])
        for message in messages:
            self.assertEqual(message.payload["work_item_id"], "draft")
            self.assertEqual(message.payload["driver"], "test")
            self.assertEqual(message.payload["tokens"], 7)
            self.assertEqual(message.payload["token_usage"], "reported")
            self.assertTrue(message.payload["rule_fingerprint"])
        self.assertTrue(any(e.payload.get("checks", [{}])[0].get("response_issue") == "quote_not_found"
                            for e in events if e.type == "work.reviewed"))
        self.assertTrue(self.rt.verify_chain().ok)

    def test_final_invalid_correction_remains_a_review_message_with_usage_and_audit(self):
        self.driver.mode = "always_bad"
        result = self.rt.collaborate(self.task)
        messages = [e for e in self.rt.log.iter_events(task_id=self.task, event_types=("msg.said",))
                    if e.actor.id == "test"]
        self.assertEqual([e.payload["phase"] for e in messages], ["review_response_correction", "review"])
        terminal = messages[-1].payload
        self.assertIn("复核回复未通过格式或引用检查", terminal["text"])
        self.assertIn("当前已暂停", terminal["text"])
        self.assertEqual(terminal["tokens"], 7)
        self.assertEqual(terminal["token_usage"], "reported")
        self.assertEqual(self.rt._reported_driver_tokens("test"), 14)
        self.assertEqual(result["brief"]["checkpoint"]["state"], "review_response_issue")
        reviews = list(self.rt.log.iter_events(task_id=self.task, event_types=("work.reviewed",)))
        self.assertEqual(len(reviews), 2)
        self.assertTrue(all(e.payload["status"] == "submitted" for e in reviews))
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
        message = [e for e in self.rt.log.read(task_id=self.task)
                   if e.type == "msg.said" and e.payload.get("phase") == "review"][-1]
        self.assertEqual(message.payload["speech_source"], "review_assessment")
        self.assertIn(check["reason"], message.payload["text"])

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
        message = [e for e in self.rt.log.read(task_id=self.task)
                   if e.type == "msg.said" and e.payload.get("phase") == "review"][-1]
        self.assertEqual(message.payload["speech_source"], "system_receipt")
        self.assertIn("复核调用未完整返回", message.payload["text"])

    def test_specific_approval_reasons_are_not_replaced_with_a_success_slogan(self):
        self.driver.mode = "valid"
        self.driver.review_count = 1
        explanation = "按钮保留键盘焦点，错误提示紧邻输入框，覆盖了这次约定的交互要求。"

        def transform(raw, context):
            for row in raw["checks"]:
                row["reason"] = explanation
        self.driver.review_transform = transform
        self.rt.collaborate(self.task)
        message = [e for e in self.rt.log.read(task_id=self.task)
                   if e.type == "msg.said" and e.payload.get("phase") == "review"][-1]
        self.assertEqual(message.payload["text"], explanation)
        self.assertEqual(message.payload["speech_source"], "review_assessment")
        self.assertTrue(self.rt.work.view(self.task)["stage_accepted"])
        self.assertEqual(self.repairs(), [])

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
        message = [e for e in self.rt.log.read(task_id=self.task)
                   if e.type == "msg.said" and e.payload.get("phase") == "review"][-1]
        self.assertEqual(message.payload["speech_source"], "system_receipt")
        self.assertIsNone(message.payload["review_seq"])

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
