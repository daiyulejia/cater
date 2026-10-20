"""Final-answer guidance reaches native tools without adding a content gate."""
import json
import unittest

import test_simple_team as simple
import test_team_external as external


class DeliveryAnswerGuidanceTests(unittest.TestCase):
    def setUp(self):
        self.f = simple.SimpleTeamTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def test_summary_is_customer_answer_and_keeps_units(self):
        f = self.f
        answer = "本例为767字节、293码点、209基本汉字；只验证计数，不代表内容正确。"
        f.driver.steps[f.manager] = [simple.turn("", ("finish_work", {
            "summary": answer, "verification": "Fixture only; no real command executed here."})), simple.turn("已完成。")]
        f.rt.team.receive(f.task, "给我最后的计数结论")
        f.run_ok()
        schema = next(t for t in f.driver.calls[0]["tools"] if t["name"] == "finish_work")
        self.assertIn("顾客看到的最终答复", schema["description"])
        self.assertIn("实际答案", schema["description"])
        self.assertIn("数字及口径", schema["input_schema"]["properties"]["summary"]["description"])
        handoff = schema["input_schema"]["properties"]["handoff"]
        self.assertIn("完整命令", handoff["properties"]["reproduce"]["description"])
        delivery, = f.events("team.objective_completed")
        visible = [e for e in f.events("msg.said") if e.payload.get("delivery_seq") == delivery.seq]
        self.assertEqual(len(visible), 1)
        self.assertEqual(visible[0].payload["text"], answer)

    def test_external_delivery_uses_actual_answer_without_another_model_turn(self):
        f = external.TeamExternalTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        manager = f.rt.organization.leader.id
        answer = "767字节、293码点、209基本汉字。仅计数，不评价内容。"

        def finish(run):
            run.call("finish_work", summary=answer, verification="Offline fixture, not measured evidence.")

        f.driver.scripts[manager] = [finish]
        task = f.rt.create_task("计数结论")
        f.rt.team.enable(task)
        f.rt.team.receive(task, "请给完整结论")
        f.rt.collaborate(task)
        events = f.rt.log.read(task_id=task)
        delivery, = [e for e in events if e.type == "team.objective_completed"]
        visible = [e for e in events if e.type == "msg.said" and e.payload.get("delivery_seq") == delivery.seq]
        self.assertEqual([e.payload["text"] for e in visible], [answer])
        self.assertEqual(len(f.driver.runs), 1)
        self.assertEqual(f.driver.errors, [])

    def test_delivery_skill_is_read_back_without_becoming_a_gate(self):
        f = self.f
        f.driver.steps[f.manager] = [simple.turn("", ("read_skill", {"skill_id": "explanatory-delivery"})),
                                    simple.turn("仅解释交付格式，不启动工程。")]
        f.rt.team.receive(f.task, "说明最终交付怎样写")
        f.run_ok()
        body = json.loads(f.driver.calls[-1]["history"][-1]["content"])
        self.assertFalse(body["has_more"])
        self.assertIn("不是内部工作小结", body["content"])
        self.assertIn("复查命令应完整可用", body["content"])
        self.assertEqual(f.events("team.objective_completed"), [])


if __name__ == '__main__':
    unittest.main()
