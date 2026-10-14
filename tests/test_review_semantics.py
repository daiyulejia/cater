import unittest

from cater.runtime import Runtime
from cater.fold import TaskSnapshot
from cater.review import _distribution_lens


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
