from __future__ import annotations

import copy
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from cater.artifacts import normalize_deliverables
from cater.checkpoints import work_checkpoint
from cater.delivery import DeliveryService
from cater.events import Actor, EventLog
from cater.rules import RuleBook
from cater.work import WorkLedger


NAMES = {"product": "月亮", "design": "罐头", "dev": "米寇", "test": "三角", "manager": "店长"}


def item(item_id="gomoku-plan", *, status="submitted", owner="product", deps=(), local=False):
    return {"id": item_id, "objective": "五子棋功能清单与验收范围", "owner": owner, "reviewer": "test",
            "depends_on": list(deps), "status": status, "attempt_id": "attempt" if status == "submitted" else None,
            "submission": {"seq": 10, "artifact_seqs": [9]} if status in {"submitted", "accepted"} else None,
            "criteria": [{"id": "meaning", "kind": "review", "text": "核对功能清单"}]
            + ([{"id": "behavior", "kind": "local_check", "text": "实际运行五子棋规则检查"}] if local else []),
            "checks": [], "reason": "", "check_lifecycle": {}, "check_in_flight": False,
            "check_uncertain": False, "execution_in_flight": status == "running", "in_flight": status == "running"}


def view(*items, **extra):
    return {"items": list(items), "stage_accepted": bool(items) and all(i["status"] == "accepted" for i in items),
            "scope_current": True, **extra}


class CheckpointTests(unittest.TestCase):
    def test_gomoku_only_plan_submitted_unknown_review_design_dev_not_started(self):
        plan = item()
        plan["checks"] = [{"id": "meaning", "kind": "review", "status": "unknown", "reason": "尚无判断依据"}]
        design = item("gomoku-design", status="blocked", owner="design", deps=["gomoku-plan"])
        dev = item("gomoku-dev", status="blocked", owner="dev", deps=["gomoku-design"])
        result = work_checkpoint(view(plan, design, dev), {"status": "unprepared"}, NAMES)
        self.assertEqual(result["state"], "waiting_review")
        self.assertTrue(result["started"])
        self.assertEqual({k: result["counts"][k] for k in ("produced", "artifacts", "accepted", "running", "not_started")},
                         {"produced": 1, "artifacts": 1, "accepted": 0, "running": 0, "not_started": 2})
        summary = result["summary"]
        for text in ("月亮已提交 1 项成果", "三角的复核结论尚未确认", "罐头、米寇", "尚未开始",
                     "目前没有已确认正在运行", "下一步：三角"):
            self.assertIn(text, summary)
        self.assertEqual(len(summary.splitlines()), 4)
        for text in ("gomoku-", "product", "design", "dev", plan["objective"], "自动"):
            self.assertNotIn(text, summary)
        self.assertEqual(result["details"][0]["objective"], plan["objective"])
        self.assertEqual(result["details"][-1]["blocked_by"], ["gomoku-design"])

    def test_response_issue_is_review_reply_error_not_quality_rejection(self):
        for issue in ("missing_result", "quote_not_found", "invalid_receipt"):
            with self.subTest(issue=issue):
                submitted = item()
                submitted["checks"] = [{"id": "meaning", "status": "unknown", "response_issue": issue,
                                        "reason": "引用的编号 gomoku-plan 无效"}]
                result = work_checkpoint(view(submitted), {}, NAMES)
                self.assertEqual(result["state"], "review_response_issue")
                self.assertIn("复核回复存在格式或引用错误", result["summary"])
                self.assertIn("这不表示成果本身被否决", result["summary"])
                self.assertIn("下一步：三角更正", result["summary"])
                self.assertNotIn("gomoku-plan", result["summary"])
                self.assertEqual(result["details"][0]["checks"][0]["response_issue"], issue)

    def test_blocked_does_not_hide_independent_running_item(self):
        result = work_checkpoint(view(item("blocked", status="blocked"),
                                      item("live", status="running", owner="dev")), {}, NAMES)
        self.assertEqual(result["state"], "running")
        self.assertEqual(result["counts"]["running"], 1)
        self.assertIn("当前 1 项成果执行在途", result["summary"])
        self.assertNotIn("目前没有", result["summary"])

    def test_check_wait_is_not_running_work(self):
        submitted = item(local=True)
        submitted.update(check_in_flight=True, in_flight=True)
        result = work_checkpoint(view(submitted), {}, NAMES)
        self.assertEqual(result["state"], "waiting_check")
        self.assertEqual(result["counts"]["running"], 0)
        self.assertEqual(result["counts"]["checking"], 1)
        self.assertIn("等待完整回执", result["summary"])
        self.assertIn("目前没有已确认正在运行", result["summary"])
        self.assertIn("代码审查不等于实际运行", result["summary"])
        self.assertIn("可信本机执行工具", result["summary"])
        self.assertIn("针对本次成果的完整回执", result["summary"])

    def test_local_claims_without_actual_receipts_do_not_pass(self):
        submitted = item(local=True)
        submitted["checks"] = [{"id": "behavior", "status": "passed", "reason": "API 声称测试通过"}]
        result = work_checkpoint(view(submitted), {}, NAMES)
        self.assertEqual(result["state"], "waiting_local_check")
        self.assertEqual(result["counts"]["waiting_local_check"], 1)
        self.assertIn("下一步：月亮安排可信本机检查", result["summary"])
        self.assertIn("缺少完整实际检查回执", result["summary"])
        self.assertNotIn("local_check", result["summary"])
        self.assertNotIn("API 声称", result["summary"])

    def test_current_receipt_still_needs_independent_review(self):
        for receipt in ({"checks": [{"id": "behavior", "status": "unknown", "receipt_seq": 20}]},
                        {"check_lifecycle": {"behavior": {"receipt_seqs": [20], "pending_seqs": []}}}):
            with self.subTest(receipt=receipt):
                submitted = item(local=True)
                submitted.update(receipt)
                result = work_checkpoint(view(submitted), {}, NAMES)
                self.assertEqual(result["state"], "waiting_review")
                self.assertEqual(result["counts"]["accepted"], 0)
                self.assertEqual(result["counts"]["waiting_local_check"], 0)

    def test_uncertainty_does_not_become_live_execution_or_retry_promise(self):
        for check in (False, True):
            with self.subTest(check=check):
                uncertain = item(status="uncertain", local=check)
                uncertain.update(in_flight=True, check_uncertain=check, check_in_flight=check)
                result = work_checkpoint(view(uncertain, item("live", status="running")), {}, NAMES)
                self.assertEqual(result["state"], "uncertain")
                self.assertEqual(result["counts"]["running"], 1)
                self.assertIn("不能自动重放", result["summary"])
                if check:
                    self.assertIn("可信本机执行工具", result["summary"])
                    self.assertIn("完整回执", result["summary"])

    def test_rework_and_ready_remain_distinct(self):
        for status, state, text in (("rework", "rework", "明确需要返工"), ("ready", "ready_pending", "待核对或补正")):
            with self.subTest(status=status):
                work_item = item(status=status)
                work_item["reason"] = "先前执行被撤回，不能自动续做"
                result = work_checkpoint(view(work_item), {}, NAMES)
                self.assertEqual(result["state"], state)
                self.assertFalse(result["started"])
                self.assertIn(text, result["summary"])
                self.assertNotIn(work_item["reason"], result["summary"])
                self.assertEqual(result["details"][0]["source_reason"], work_item["reason"])

    def test_fresh_ready_is_distinct_from_ready_with_retained_history(self):
        fresh = work_checkpoint(view(item(status="ready")), {}, NAMES)
        self.assertEqual(fresh["state"], "ready")
        self.assertFalse(fresh["started"])
        self.assertIn("本次执行尚未开始", fresh["summary"])
        for history in ({"attempt_id": "previous-attempt"}, {"review_seq": 20}, {"reason": "等待核对"}):
            with self.subTest(history=history):
                work_item = item(status="ready")
                work_item.update(history)
                result = work_checkpoint(view(work_item), {}, NAMES)
                self.assertEqual(result["state"], "ready_pending")
                self.assertEqual(result["counts"]["ready"], 1)
                self.assertEqual(result["counts"]["running"], 0)
                self.assertIn("待核对或补正", result["summary"])
                self.assertNotIn("本次执行尚未开始", result["summary"])
                self.assertEqual(result["details"][0]["status"], "ready")
                self.assertEqual(result["details"][0]["state"], "ready_pending")

    def test_stale_does_not_reuse_old_acceptance_or_hide_inflight_evidence(self):
        old = item(status="stale")
        old.update(attempt_id="old-attempt", execution_in_flight=True, in_flight=True)
        result = work_checkpoint(view(old, scope_current=False), {"status": "accepted"}, NAMES)
        self.assertEqual(result["state"], "stale")
        self.assertEqual(result["counts"]["running"], 0)
        self.assertTrue(result["started"])
        self.assertNotIn("整体验收已通过", result["summary"])

    def test_delivery_pending_local_check_and_review_are_distinct_from_accepted(self):
        for delivery, state, text in (
            ({}, "delivery_pending", "整体验收"),
            ({"status": "unprepared"}, "delivery_pending", "配置独立整体复核人"),
            ({"status": "pending", "reviewer": "manager"}, "delivery_pending", "店长"),
            ({"status": "pending", "reviewer": "manager", "local_checks": [{"id": "behavior", "status": "unknown"}]},
             "delivery_local_check", "可信本机"),
            ({"status": "checking", "reviewer": "manager", "check_in_flight": True}, "delivery_checking", "等待完整回执"),
            ({"status": "reviewing", "reviewer": "manager"}, "delivery_reviewing", "调用在途"),
            ({"status": "needs_evidence", "reviewer": "manager"}, "delivery_pending", "整体验收"),
            ({"status": "rework", "reviewer": "manager"}, "delivery_rework", "明确要求返工"),
            ({"status": "uncertain", "reviewer": "manager"}, "delivery_uncertain", "不能自动重放"),
            ({"status": "stale", "reviewer": "manager"}, "delivery_stale", "已失效"),
            ({"status": "accepted", "reviewer": "manager"}, "accepted", "整体验收已通过"),
        ):
            with self.subTest(delivery=delivery):
                result = work_checkpoint(view(item(status="accepted")), delivery, NAMES)
                self.assertEqual(result["state"], state)
                self.assertIn(text, result["summary"])
                if state == "accepted":
                    self.assertEqual(result["blockers"], [])
                else:
                    self.assertNotIn("整体验收已通过", result["summary"])

    def test_delivery_manifest_cannot_hide_missing_local_check(self):
        delivery = {"status": "pending", "reviewer": "manager", "manifest": {
            "criteria": [{"id": "behavior", "kind": "local_check", "text": "整体行为检查"}]}}
        result = work_checkpoint(view(item(status="accepted")), delivery, NAMES)
        self.assertEqual(result["state"], "delivery_local_check")
        self.assertIn("代码审查不等于实际运行", result["summary"])
        self.assertIn("反复发送继续不会自动补出回执", result["summary"])

    def test_delivery_acceptance_cannot_override_unaccepted_or_stale_work(self):
        for work in (view(item()), view(item(status="accepted"), scope_current=False),
                     view(item(status="accepted"), stage_accepted=False)):
            with self.subTest(work=work):
                result = work_checkpoint(work, {"status": "accepted"}, NAMES)
                self.assertNotEqual(result["state"], "accepted")
                self.assertNotIn("整体验收已通过", result["summary"])

    def test_summary_is_bounded_by_items_and_keeps_all_critical_limits(self):
        items = [item(f"plan-{i}", local=True) for i in range(16)]
        for row in items:
            row["objective"] = "完整且非常长的任务目标" * 100
            row["checks"] = [{"id": "meaning", "response_issue": "quote_not_found", "status": "unknown"}]
        uncertain = item("uncertain", status="uncertain")
        result = work_checkpoint(view(*items, uncertain), {}, NAMES)
        self.assertLessEqual(len(result["summary"].splitlines()), 6)
        self.assertIn("代码审查不等于实际运行", result["summary"])
        self.assertIn("可信本机执行工具", result["summary"])
        self.assertIn("完整回执", result["summary"])
        self.assertIn("不能自动重放", result["summary"])
        self.assertNotIn("完整且非常长", result["summary"])
        self.assertEqual(len(result["blockers"]), 33)
        self.assertEqual(result["details"][0]["objective"], items[0]["objective"])

    def test_missing_names_use_one_fallback_and_never_show_role_ids(self):
        result = work_checkpoint(view(item()), {}, {})
        self.assertIn("未命名角色", result["summary"])
        self.assertNotIn("product", result["summary"])
        self.assertNotIn("test", result["summary"])
        self.assertEqual(result["blockers"][0]["reviewer"], "test")

    def test_empty_and_legacy_views_are_supported(self):
        empty = work_checkpoint({}, {}, {})
        self.assertFalse(empty["started"])
        self.assertEqual(empty["state"], "unplanned")
        legacy = item()
        del legacy["checks"]
        self.assertEqual(work_checkpoint({"items": [legacy]}, {}, NAMES)["state"], "waiting_review")

    def test_pure_snapshot_does_not_read_clock_or_change_inputs(self):
        inputs = (view(item(local=True)), {"status": "unprepared"}, NAMES.copy())
        original = copy.deepcopy(inputs)
        with patch("time.time", side_effect=AssertionError("must use the supplied view")), \
                patch("builtins.open", side_effect=AssertionError("must not read the host")):
            first = work_checkpoint(*inputs)
            self.assertEqual(first, work_checkpoint(*inputs))
        self.assertEqual(inputs, original)


class ActualViewCheckpointTests(unittest.TestCase):
    """Exercise the public view shapes with an isolated in-memory event log."""

    def setUp(self):
        self.log = EventLog()
        self.addCleanup(self.log.close)
        self.rules = RuleBook(self.log)
        self.ledger = WorkLedger(self.log, self.rules)
        self.task = "gomoku"
        self.log.emit(type="task.created", actor=Actor.human(), task_id=self.task,
                      payload={"goal": "实现五子棋", "acceptance": []})
        runtime = SimpleNamespace(log=self.log, work=self.ledger, rulebook=self.rules,
                                  conversation_background=lambda task_id: None)
        self.delivery = DeliveryService(runtime)

    def submit(self, item_id="gomoku-plan", owner="product"):
        claim = self.ledger.claim(self.task, item_id)
        artifact = normalize_deliverables([{"title": "功能清单", "summary": "范围",
                                           "content": "五子棋功能清单：双人落子、胜负判断。", "format": "markdown"}])[0]
        published = self.log.emit(type="deliverable.published", actor=Actor.agent(owner, "worker"),
                                  task_id=self.task, payload={**artifact, "work_item_id": item_id,
                                                             "attempt_id": claim.payload["attempt_id"]})
        submission = self.ledger.submit(self.task, claim, [published.seq])
        return submission, artifact, published

    def test_actual_gomoku_unknown_review_view(self):
        self.ledger.plan(self.task, [item(), item("gomoku-design", status="blocked", owner="design", deps=["gomoku-plan"]),
                                    item("gomoku-dev", status="blocked", owner="dev", deps=["gomoku-design"])],
                         role_ids=set(NAMES), source_seq=0)
        submission, _, _ = self.submit()
        self.ledger.review(self.task, "gomoku-plan", reviewer="test", submission_seq=submission.seq,
                           results=[{"id": "meaning", "status": "unknown", "reason": "尚未核对清单正文"}])
        work = self.ledger.view(self.task)
        self.assertEqual([i["status"] for i in work["items"]], ["submitted", "blocked", "blocked"])
        result = work_checkpoint(work, self.delivery.view(self.task), NAMES)
        self.assertEqual(result["state"], "waiting_review")
        self.assertIn("罐头、米寇", result["summary"])
        self.assertIn("尚未开始", result["summary"])

    def test_actual_failed_review_resolves_to_ready_but_still_needs_attention(self):
        self.ledger.plan(self.task, [item()], role_ids=set(NAMES), source_seq=0)
        submission, artifact, published = self.submit()
        reviewed = self.ledger.review(self.task, "gomoku-plan", reviewer="test", submission_seq=submission.seq,
                                     results=[{"id": "meaning", "status": "failed", "reason": "缺少和棋处理",
                                               "quote": artifact["content"], "artifact_seq": published.seq}])
        self.assertEqual(reviewed.payload["status"], "rework")
        work = self.ledger.view(self.task)
        self.assertEqual(work["items"][0]["status"], "ready")
        result = work_checkpoint(work, self.delivery.view(self.task), NAMES)
        self.assertEqual(result["state"], "ready_pending")
        self.assertTrue(result["started"])
        self.assertIn("月亮的工作待核对或补正", result["summary"])
        self.assertNotIn("本次执行尚未开始", result["summary"])
        self.assertEqual(result["details"][0]["reason"], reviewed.payload["reason"])
        self.assertEqual(result["details"][0]["checks"][0]["reason"], "缺少和棋处理")

    def test_actual_invalidated_submission_ready_keeps_correction_reason(self):
        self.ledger.plan(self.task, [item()], role_ids=set(NAMES), source_seq=0)
        self.submit()
        self.ledger.rework(self.task, "gomoku-plan", "需补正功能清单")
        work = self.ledger.view(self.task)
        current = work["items"][0]
        self.assertEqual(current["status"], "ready")
        self.assertIsNone(current["submission"])
        self.assertTrue(current["attempt_id"])
        result = work_checkpoint(work, self.delivery.view(self.task), NAMES)
        self.assertEqual(result["state"], "ready_pending")
        self.assertTrue(result["started"])
        self.assertEqual(result["counts"]["running"], 0)
        self.assertEqual(result["details"][0]["source_reason"], "需补正功能清单")
        self.assertIn("下一步：月亮核对", result["summary"])

    def test_actual_stale_dependent_ready_is_not_presented_as_first_run(self):
        self.ledger.plan(self.task, [item(), item("gomoku-design", owner="design", deps=["gomoku-plan"])],
                         role_ids=set(NAMES), source_seq=0)
        for item_id, owner in (("gomoku-plan", "product"), ("gomoku-design", "design")):
            submission, artifact, published = self.submit(item_id, owner)
            self.ledger.review(self.task, item_id, reviewer="test", submission_seq=submission.seq,
                               results=[{"id": "meaning", "status": "passed", "reason": "核对正文",
                                         "quote": artifact["content"], "artifact_seq": published.seq}])
        self.ledger.rework(self.task, "gomoku-plan", "上游成果需更新")
        submission, artifact, published = self.submit()
        self.ledger.review(self.task, "gomoku-plan", reviewer="test", submission_seq=submission.seq,
                           results=[{"id": "meaning", "status": "passed", "reason": "核对新正文",
                                     "quote": artifact["content"], "artifact_seq": published.seq}])
        work = self.ledger.view(self.task)
        dependent = work["items"][1]
        self.assertEqual(dependent["status"], "ready")
        self.assertTrue(dependent["attempt_id"])
        self.assertIsNone(dependent["submission"])
        result = work_checkpoint(work, self.delivery.view(self.task), NAMES)
        self.assertEqual(result["state"], "ready_pending")
        self.assertIn("罐头的工作待核对或补正", result["summary"])
        self.assertNotIn("本次执行尚未开始", result["summary"])
        self.assertEqual(result["details"][1]["source_reason"], dependent["reason"])

    def test_actual_delivery_prepared_and_accepted_views(self):
        self.ledger.plan(self.task, [item()], role_ids=set(NAMES), source_seq=0)
        submission, artifact, published = self.submit()
        self.ledger.review(self.task, "gomoku-plan", reviewer="test", submission_seq=submission.seq,
                           results=[{"id": "meaning", "status": "passed", "reason": "核对当前清单正文",
                                     "quote": artifact["content"], "artifact_seq": published.seq}])
        manifest = self.delivery._manifest(self.task)
        self.log.emit(type="delivery.prepared", actor=Actor.system(), task_id=self.task,
                      payload={"manifest": manifest, "reviewer": "manager", "digest": "current-bundle"})
        result = work_checkpoint(self.ledger.view(self.task), self.delivery.view(self.task), NAMES)
        self.assertEqual(result["state"], "delivery_pending")
        self.assertIn("下一步：店长", result["summary"])
        started = self.log.emit(type="delivery.review_started", actor=Actor.agent("manager", "delivery_reviewer"),
                      task_id=self.task, payload={"delivery_digest": "current-bundle", "check_seqs": {}})
        self.log.emit(type="delivery.review_finished", actor=Actor.agent("manager", "delivery_reviewer"),
                      task_id=self.task, payload={"status": "accepted", "delivery_digest": "current-bundle",
                                                 "check_seqs": {}, "attempt_seq": started.seq})
        result = work_checkpoint(self.ledger.view(self.task), self.delivery.view(self.task), NAMES)
        self.assertEqual(result["state"], "accepted")
        self.assertIn("整体验收已通过", result["summary"])


if __name__ == "__main__":
    unittest.main()
