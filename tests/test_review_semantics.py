import unittest

from cater.events import Actor
from cater.runtime import Runtime
from cater.fold import TaskSnapshot
from cater.review import _distribution_lens
from cater.team import HOST
from test_work import item


class ReviewSemanticsTests(unittest.TestCase):
    def native_task(self, rt, *, done=False):
        task = rt.create_task("通过消息交代一份工作")
        rt.log.emit(type="team.started", task_id=task, actor=HOST, payload={"version": 1})
        source = rt.log.emit(type="team.message_received", task_id=task, actor=Actor.human(),
                             payload={"text": "请先看看", "recipient": "dev", "kind": "message"})
        rt.log.emit(type="msg.said", task_id=task, actor=Actor.agent("dev", "developer"),
                    payload={"text": "我已收到，先分析。", "phase": "team", "reply_to": source.seq})
        if done:
            rt.log.emit(type="team.objective_completed", task_id=task, actor=HOST,
                        payload={"summary": "店长确认交付", "verification": "仅检查说明，未运行"})
        return task

    def test_native_delivery_does_not_require_legacy_dispatch_or_governance(self):
        with Runtime(":memory:") as rt:
            task = self.native_task(rt, done=True)
            before = rt.log.head_hash()
            review = rt.review(task)
            lenses = {lens["id"]: lens for lens in review["lenses"]}
            self.assertEqual(rt.task(task).assignments, [])
            self.assertEqual(lenses["distribution"]["status"], "ok")
            self.assertIn("explicit recipients", lenses["distribution"]["finding"])
            self.assertEqual(lenses["logic"]["status"], "ok")
            self.assertIn("not independent proof", lenses["logic"]["action"])
            self.assertEqual(before, rt.log.head_hash())

    def test_in_progress_native_chat_is_not_a_completed_outcome_or_a_governance_defect(self):
        with Runtime(":memory:") as rt:
            task = self.native_task(rt)
            lens = next(lens for lens in rt.review(task)["lenses"] if lens["id"] == "logic")
            self.assertEqual(rt.task(task).status, "running")
            self.assertEqual(lens["status"], "ok")
            self.assertIn("no completed outcome", lens["finding"])

    def test_native_route_does_not_hide_another_unassigned_legacy_task(self):
        with Runtime(":memory:") as rt:
            self.native_task(rt, done=True)
            task = rt.create_task("确实缺少分工的旧任务")
            rt.log.emit(type="msg.said", task_id=task, actor=Actor.agent("dev", "developer"),
                        payload={"text": "我开始了", "phase": "execution"})
            lens = next(lens for lens in rt.review()["lenses"] if lens["id"] == "distribution")
            self.assertEqual(lens["status"], "watch")
            self.assertIn("no visible assignment path", lens["finding"])

    def test_native_receipt_requires_trusted_start_and_a_real_recipient(self):
        for case in ("untrusted-start", "missing-recipient", "empty-recipient", "invalid-recipient", "unknown-recipient",
                     "system-sender", "unknown-agent", "unknown-human", "wrong-order", "other-task"):
            with self.subTest(case=case), Runtime(":memory:") as rt:
                task = rt.create_task("核对原生消息来源")
                other = rt.create_task("其他群") if case == "other-task" else task
                actor = Actor.agent("team", "team") if case == "untrusted-start" else HOST
                payload = {"text": "请看看", "recipient": "dev", "kind": "message"}
                if case == "missing-recipient":
                    payload.pop("recipient")
                elif case == "empty-recipient":
                    payload["recipient"] = "  "
                elif case == "invalid-recipient":
                    payload["recipient"] = ["dev"]
                elif case == "unknown-recipient":
                    payload["recipient"] = "nonexistent-role"
                if case == "wrong-order":
                    rt.log.emit(type="team.message_received", task_id=task, actor=Actor.human(), payload=payload)
                rt.log.emit(type="team.started", task_id=other, actor=actor, payload={"version": 1})
                if case != "wrong-order":
                    sender = HOST if case == "system-sender" else Actor.human()
                    if case == "unknown-agent":
                        sender = Actor.agent("outsider", "developer")
                    elif case == "unknown-human":
                        sender = Actor.human("outsider")
                    rt.log.emit(type="team.message_received", task_id=task, actor=sender, payload=payload)
                rt.log.emit(type="msg.said", task_id=task, actor=Actor.agent("dev", "developer"),
                            payload={"text": "没有可核实的投递关联", "phase": "team"})
                lens = next(lens for lens in rt.review(task)["lenses"] if lens["id"] == "distribution")
                self.assertEqual(lens["status"], "watch")
                self.assertIn("no visible assignment path", lens["finding"])

    def test_native_completion_is_not_misreported_as_only_governance_heavy_history(self):
        with Runtime(":memory:") as rt:
            self.native_task(rt, done=True)
            legacy = rt.create_task("独立的治理记录")
            rt.log.emit(type="cabinet.voted", task_id=legacy, actor=Actor.system("cabinet"),
                        payload={"votes": [], "winner": "A"})
            lens = next(lens for lens in rt.review()["lenses"] if lens["id"] == "logic")
            self.assertEqual(lens["status"], "ok")
            self.assertIn("Both", lens["finding"])

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
