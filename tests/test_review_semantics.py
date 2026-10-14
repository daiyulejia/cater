import unittest

from cater.runtime import Runtime
from cater.fold import TaskSnapshot
from cater.review import _distribution_lens
from test_work import item


class ReviewSemanticsTests(unittest.TestCase):
    def test_lens_ids_are_unique_and_cost_has_its_own_identity(self):
        with Runtime(":memory:") as rt:
            task = rt.create_task("复盘角度")
            rt.intake(task); rt.collaborate(task)
            lenses = rt.review(task)["lenses"]
            ids = [item["id"] for item in lenses]
            self.assertEqual(len(ids), len(set(ids)))
            self.assertTrue({"cost", "distribution", "collaboration"} <= set(ids))

    def test_normal_collaboration_does_not_generate_forced_governance_action(self):
        with Runtime(":memory:") as rt:
            task = rt.create_task("整理一页网页体验")
            rt.intake(task); rt.collaborate(task)
            review = rt.review(task)
            self.assertEqual(next(item for item in review["lenses"] if item["id"] == "logic")["status"], "ok")
            self.assertEqual(next(item for item in review["lenses"] if item["id"] == "distribution")["status"], "ok")
            self.assertEqual(next(item for item in review["lenses"] if item["id"] == "collaboration")["status"], "ok")
            self.assertEqual(next(item for item in review["lenses"] if item["id"] == "testing")["status"], "watch")
            self.assertFalse(any("Force one governance" in action for action in review["next_actions"]))

    def test_direct_assignment_alone_is_not_a_coverage_defect(self):
        with Runtime(":memory:") as rt:
            task = rt.create_task("执行一项简单工作")
            rt.assign(task, "dev")
            lens = next(item for item in rt.review(task)["lenses"] if item["id"] == "distribution")
            self.assertEqual(lens["status"], "ok")

    def test_reassignment_history_is_not_mistaken_for_active_agent_overload(self):
        with Runtime(":memory:") as rt:
            task = rt.create_task("重复调整同一负责人")
            for _ in range(8): rt.assign(task, "dev")
            rt.collaborate(task)
            self.assertEqual(_distribution_lens([rt.task(task)])["status"], "ok")

    def test_completed_work_with_many_active_assignments_still_gets_reviewed(self):
        task = TaskSnapshot(id="overloaded", status="done", messages=[{"actor": "one"}], assignments=[{"owner": f"worker-{index}", "status": "assigned"} for index in range(8)])
        lens = _distribution_lens([task])
        self.assertEqual(lens["status"], "watch")
        self.assertIn("active assignments", lens["finding"])

    def test_work_plan_ownership_does_not_require_duplicate_legacy_assignments(self):
        with Runtime(":memory:") as rt:
            task = rt.create_task("同一成员承担多个工作项")
            rt.plan_work(task, [item("first"), item("second", deps=["first"])])
            rt.collaborate(task)
            snapshot = rt.task(task)
            self.assertEqual(snapshot.assignments, [])
            self.assertTrue(snapshot.messages)
            lens = next(lens for lens in rt.review(task)["lenses"] if lens["id"] == "distribution")
            self.assertEqual(lens["status"], "ok")
            self.assertIn("Work items", lens["finding"])

    def test_stale_work_plan_requires_replanning_not_more_legacy_dispatch(self):
        with Runtime(":memory:") as rt:
            task = rt.create_task("原始目标")
            rt.plan_work(task, [item("first")])
            rt.command(task, "补充一个新要求")
            lens = next(lens for lens in rt.review(task)["lenses"] if lens["id"] == "distribution")
            self.assertEqual(lens["status"], "watch")
            self.assertIn("no longer match", lens["finding"])
            self.assertNotIn("Use direct assign", lens["action"])

    def test_planning_receipt_does_not_count_as_executed_unassigned_work(self):
        task = TaskSnapshot(id="planning", messages=[{"actor": "store_manager", "phase": "planning"}])
        lens = _distribution_lens([task])
        self.assertNotIn("Some executed tasks", lens["finding"])

    def test_planned_task_does_not_hide_another_unassigned_execution(self):
        planned = TaskSnapshot(id="planned", work={"items": [item("first")], "scope_current": True})
        unassigned = TaskSnapshot(id="unassigned", messages=[{"actor": "dev"}])
        lens = _distribution_lens([planned, unassigned])
        self.assertEqual(lens["status"], "watch")
        self.assertIn("no visible assignment path", lens["finding"])
