from __future__ import annotations

import unittest

from cater.events import Actor
from cater.fold import TaskSnapshot
from cater.review import _next_steps, _reliability_lens
from cater.runtime import Runtime
from cater.team import HOST
from test_work import item


class ReviewReliabilityTests(unittest.TestCase):
    def test_native_failed_runs_do_not_require_legacy_driver_failure_events(self):
        with Runtime(":memory:") as rt:
            task = rt.create_task("检查原生消息调用失败")
            start = rt.log.emit(type="team.run_started", task_id=task, actor=HOST,
                                payload={"message_seq": 1, "role": "dev", "run_id": "native-run"})
            rt.log.emit(type="team.run_finished", task_id=task, actor=HOST,
                        payload={**start.payload, "status": "failed", "reason": "调用未完成"})
            self.assertEqual(rt.task(task).driver_failures, [])
            review = rt.review(task)
            lens = next(row for row in review["lenses"] if row["id"] == "reliability")
            self.assertEqual(lens["status"], "watch")
            self.assertIn("1 failed or interrupted native chat run(s)", lens["finding"])
            self.assertIn("historical receipts", lens["finding"])
            self.assertIn(lens["action"], review["next_actions"])

    def test_native_interruption_without_run_id_retains_legacy_recovery_receipt(self):
        with Runtime(":memory:") as rt:
            task = rt.create_task("检查进程退出记录")
            rt.log.emit(type="team.run_started", task_id=task, actor=HOST,
                        payload={"message_seq": 1, "role": "dev", "run_id": "native-run"})
            rt.log.emit(type="team.run_finished", task_id=task, actor=HOST,
                        payload={"message_seq": 1, "role": "dev", "status": "interrupted"})
            self.assertEqual(_reliability_lens([rt.task(task)], rt.log.read())["status"], "watch")

    def test_native_running_waiting_and_completed_runs_are_not_failures(self):
        for status in (None, "waiting", "completed"):
            with self.subTest(status=status), Runtime(":memory:") as rt:
                task = rt.create_task("正常消息状态")
                start = rt.log.emit(type="team.run_started", task_id=task, actor=HOST,
                                    payload={"message_seq": 1, "role": "dev", "run_id": "native-run"})
                if status is not None:
                    rt.log.emit(type="team.run_finished", task_id=task, actor=HOST,
                                payload={**start.payload, "status": status})
                self.assertEqual(_reliability_lens([rt.task(task)], rt.log.read())["status"], "ok")

    def test_unbound_native_finish_cannot_report_a_failure_or_overwrite_one(self):
        for invalid in ("actor", "role", "run", "task", "missing-start"):
            with self.subTest(invalid=invalid), Runtime(":memory:") as rt:
                task = rt.create_task("核对消息回执关联")
                start = rt.log.emit(type="team.run_started", task_id=task, actor=HOST,
                                    payload={"message_seq": 1, "role": "dev", "run_id": "native-run"})
                bad = {**start.payload, "status": "failed"}
                if invalid == "role":
                    bad["role"] = "test"
                elif invalid == "run":
                    bad["run_id"] = "other-run"
                elif invalid == "missing-start":
                    bad["message_seq"] = 999
                other = rt.create_task("其他群") if invalid == "task" else task
                actor = Actor.agent("team", "team") if invalid == "actor" else HOST
                rt.log.emit(type="team.run_finished", task_id=other, actor=actor, payload=bad)
                self.assertEqual(_reliability_lens([rt.task(task)], rt.log.read())["status"], "ok")
                rt.log.emit(type="team.run_finished", task_id=task, actor=HOST,
                            payload={**start.payload, "status": "failed"})
                rt.log.emit(type="team.run_finished", task_id=other, actor=actor,
                            payload={**bad, "status": "completed"})
                self.assertEqual(_reliability_lens([rt.task(task)], rt.log.read())["status"], "watch")

    def test_native_failure_stays_historical_after_other_work_completes(self):
        with Runtime(":memory:") as rt:
            task = rt.create_task("完成不抹除调用历史")
            start = rt.log.emit(type="team.run_started", task_id=task, actor=HOST,
                                payload={"message_seq": 1, "role": "dev", "run_id": "native-run"})
            rt.log.emit(type="team.run_finished", task_id=task, actor=HOST,
                        payload={**start.payload, "status": "failed"})
            rt.log.emit(type="team.objective_completed", task_id=task, actor=HOST,
                        payload={"summary": "后续已处理", "verification": "原失败保留"})
            lens = _reliability_lens([rt.task(task)], rt.log.read())
            self.assertEqual(rt.task(task).status, "done")
            self.assertEqual(lens["status"], "watch")
            self.assertIn("not proof", lens["finding"])

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
