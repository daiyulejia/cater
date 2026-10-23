"""Rule projections reach native model inputs without unrelated frozen bodies."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from cater.events import Actor
import test_simple_team


class TeamRuleProjectionTests(unittest.TestCase):
    setUp = test_simple_team.SimpleTeamTests.setUp

    def test_next_input_sees_updated_rule_and_old_response_cannot_execute(self):
        project = self.rt.rulebook.create_project("当前项目")["id"]
        self.rt.rulebook.bind_project(self.task, project)
        rule = self.rt.rulebook.record("保留原接口", scope="project", project_id=project)
        foreign = self.rt.create_task("无关群")
        self.rt.log.emit(type="task.created", actor=Actor.human(), task_id="foreign-frozen",
                         payload={"goal": "无关的冻结背景", "background": {"sources": ["PRIVATE" * 30000]},
                                  "inherited_rules": []})
        before, after = [], []

        def first(context, history, tools):
            before.extend(context["rules"])
            self.rt.rulebook.update(rule["id"], revision=1, text="保留原接口并说明验证范围", status="active")
            return test_simple_team.turn("", ("read_receipts", {}))

        def second(context, history, tools):
            after.extend(context["rules"])
            return test_simple_team.turn("收到，本轮使用更新后的要求。")

        self.driver.steps[self.manager] = [first, second]
        self.rt.team.receive(self.task, "请核对这次适用的店规", request_id="native-rule-projection")
        convert = self.rt.log._row_to_event

        def scoped(row):
            if row["type"] == "task.created":
                self.assertNotIn(row["task_id"], {foreign, "foreign-frozen"})
            return convert(row)

        with patch.object(self.rt.log, "_row_to_event", side_effect=scoped):
            self.rt.collaborate(self.task)
            self.assertEqual(after, [])
            finished = list(self.rt.log.iter_events(task_id=self.task, event_types=("team.run_finished",)))
            self.assertEqual(finished[-1].payload["status"], "interrupted")
            self.assertEqual(list(self.rt.log.iter_events(task_id=self.task, event_types=("team.tool_started",))), [])
            self.rt.team.receive(self.task, "按当前店规重新核对", request_id="native-rule-refreshed-input")
            self.rt.collaborate(self.task)
        self.assertEqual([(r["text"], r["revision"]) for r in before], [("保留原接口", 1)])
        self.assertEqual([(r["text"], r["revision"]) for r in after], [("保留原接口并说明验证范围", 2)])
        applied = list(self.rt.log.iter_events(task_id=self.task, event_types=("rules.applied",)))
        self.assertEqual(len(applied), 2)
        self.assertNotEqual(applied[0].payload["fingerprint"], applied[1].payload["fingerprint"])
        self.assertEqual(len(self.driver.calls), 2)
        self.assertTrue(self.rt.verify_chain().ok)

    def test_native_derived_task_keeps_frozen_task_rule_after_parent_changes(self):
        parent = self.task
        source = self.rt.rulebook.record("创建时的任务边界", scope="task", task_id=parent)
        child = self.rt.derive_task(parent, "开展独立工作", request_id="native-inherited-rule")["task_id"]
        self.rt.team.enable(child)
        self.rt.rulebook.update(source["id"], revision=1, text="原群后续新边界", status="active")
        self.rt.rulebook.record("全店通用要求", scope="workspace")
        observed = []

        def reply(context, history, tools):
            observed.extend(context["rules"])
            return test_simple_team.turn("保留本群创建时的任务边界，同时使用当前通用要求。")

        self.driver.steps[self.manager] = [reply]
        self.rt.team.receive(child, "看看本群适用要求", request_id="native-frozen-rule")
        self.rt.collaborate(child)
        self.assertEqual({r["text"] for r in observed}, {"创建时的任务边界", "全店通用要求"})
        inherited = next(r for r in self.rt.rulebook.view(task_id=child)["rules"] if r["scope"] == "task")
        self.assertEqual(inherited["source"]["rule_id"], source["id"])
        self.assertEqual(inherited["source"]["task_id"], parent)
        self.assertNotEqual(inherited["id"], source["id"])
        self.assertEqual(self.rt.rulebook.current_snapshot(parent)["rules"][0]["text"], "原群后续新边界")
        self.assertTrue(self.rt.verify_chain().ok)


if __name__ == "__main__":
    unittest.main()
