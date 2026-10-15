from __future__ import annotations

import unittest

from cater.events import Actor
from cater.fold import TaskSnapshot
from cater.review import _next_steps, _reliability_lens
from cater.runtime import Runtime
from test_work import item


class ReviewReliabilityTests(unittest.TestCase):
    def test_uncertain_work_without_driver_failure_cannot_be_reported_healthy(self):
        with Runtime(":memory:") as rt:
            task = rt.create_task("核对丢失的执行结果")
            rt.plan_work(task, [item("work")])
            claim = rt.work.claim(task, "work")
            rt.work.fail(task, claim, "调用返回不完整，结果未知。", uncertain=True)
            snapshot = rt.task(task)
            self.assertEqual(snapshot.driver_failures, [])
            review = rt.review(task)
            lens = next(row for row in review["lenses"] if row["id"] == "reliability")
            self.assertEqual(lens["status"], "needs_attention")
            self.assertIn("1 work/check item(s)", lens["finding"])
            self.assertIn("reconcile", lens["action"])
            self.assertIn(lens["action"], review["next_actions"])

    def test_uncertain_delivery_or_expired_check_is_not_a_transport_success(self):
        for work in ({"items": [], "delivery": {"status": "uncertain"}},
                     {"items": [{"status": "submitted", "check_uncertain": True}]}):
            with self.subTest(work=work):
                lens = _reliability_lens([TaskSnapshot("task", work=work)], [])
                self.assertEqual(lens["status"], "needs_attention")

    def test_latest_planning_uncertainty_is_visible_and_a_later_finished_attempt_replaces_it(self):
        with Runtime(":memory:") as rt:
            task = rt.create_task("核对规划结果")
            rt.log.emit(type="planning.finished", task_id=task, actor=Actor.system("store_manager"),
                        payload={"attempt_seq": 1, "status": "uncertain", "reason": "输出截断"})
            lens = _reliability_lens([rt.task(task)], rt.log.read(task_id=task))
            self.assertEqual(lens["status"], "needs_attention")
            self.assertIn("1 planning call(s)", lens["finding"])
            rt.log.emit(type="planning.finished", task_id=task, actor=Actor.system("store_manager"),
                        payload={"attempt_seq": 3, "status": "installed", "reason": "新尝试已完成"})
            self.assertEqual(_reliability_lens([rt.task(task)], rt.log.read(task_id=task))["status"], "ok")

    def test_late_old_planning_receipt_cannot_hide_a_newer_uncertain_attempt(self):
        with Runtime(":memory:") as rt:
            task = rt.create_task("核对迟到回执")
            for attempt, status in ((20, "uncertain"), (10, "stale")):
                rt.log.emit(type="planning.finished", task_id=task, actor=Actor.system("store_manager"),
                            payload={"attempt_seq": attempt, "status": status, "reason": "试验记录"})
            self.assertEqual(_reliability_lens([rt.task(task)], rt.log.read(task_id=task))["status"], "needs_attention")

    def test_normal_pending_or_running_work_is_not_misclassified_as_unknown(self):
        snapshot = TaskSnapshot("task", work={"items": [{"status": "running"}, {"status": "blocked"}],
                                               "delivery": {"status": "pending"}})
        self.assertEqual(_reliability_lens([snapshot], [])["status"], "ok")

    def test_high_priority_reconciliation_precedes_ordinary_cost_followup(self):
        lenses = [{"id": "cost", "label": "Cost", "status": "watch", "action": "Inspect cost"},
                  {"id": "reliability", "label": "Reliability", "status": "needs_attention", "action": "Reconcile"},
                  {"id": "discipline", "label": "Discipline", "status": "watch", "action": "Inspect formatting"}]
        steps = _next_steps([TaskSnapshot("task")], lenses)
        self.assertEqual([s["source"] for s in steps], ["reliability", "cost", "discipline"])
