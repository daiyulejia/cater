"""Final-answer guidance reaches native tools without adding a content gate."""
import json
import unittest
from unittest.mock import patch

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

    def test_native_delivery_does_not_call_model_again_or_turn_success_into_network_failure(self):
        f = self.f
        answer = "结果已保存，实际验证仅覆盖当前示例。"
        f.driver.steps[f.manager] = [simple.turn("", ("finish_work", {
            "summary": answer, "verification": "Offline fixture only."})),
            ConnectionError("A post-delivery request must not happen")]
        f.rt.team.receive(f.task, "请交付已核对的结果")
        f.run_ok()
        self.assertEqual(len(f.driver.calls), 1)
        self.assertEqual(len(f.events("team.model_called")), 1)
        delivery, = f.events("team.objective_completed")
        visible = [e for e in f.events("msg.said") if e.payload.get("delivery_seq") == delivery.seq]
        self.assertEqual([e.payload["text"] for e in visible], [answer])
        self.assertEqual(f.events("team.run_finished")[-1].payload["status"], "completed")

    def test_external_followup_using_idempotent_finish_does_not_replace_original_delivery(self):
        f = external.TeamExternalTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        manager = f.rt.organization.leader.id
        original = "工程已交付，测试范围见说明。"
        explanation = "解压后进入目录，运行 python -m unittest -v。"
        f.driver.scripts[manager] = [
            lambda run: run.call("finish_work", summary=original, verification="Fixture only."),
            lambda run: run.call("finish_work", summary=explanation, verification="Explanation only."),
        ]
        task = f.rt.create_task("交付后追问")
        f.rt.team.enable(task)
        f.rt.team.receive(task, "交付已有结果")
        f.rt.collaborate(task)
        f.rt.team.receive(task, "我在 Windows 上怎么运行？")
        f.rt.collaborate(task)
        events = f.rt.log.read(task_id=task)
        delivery, = [e for e in events if e.type == "team.objective_completed"]
        messages = [e for e in events if e.type == "msg.said"]
        self.assertEqual([e.payload["text"] for e in messages], [original, explanation])
        self.assertEqual([e.payload["text"] for e in messages if e.payload.get("delivery_seq") == delivery.seq], [original])
        self.assertEqual(messages[-1].payload["audience"], "customer")
        self.assertEqual(delivery.payload["summary"], original)
        self.assertEqual(len(f.driver.runs), 2)
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

    def test_finish_does_not_skip_later_tools_in_same_batch(self):
        f = self.f
        f.driver.steps[f.manager] = [simple.turn("", ("finish_work", {
            "summary": "已核对结果。", "verification": "Fixture only."}), ("read_delivery", {})), simple.turn("核对记录后确认结果。")]
        f.rt.team.receive(f.task, "交付并保留说明")
        f.run_ok()
        self.assertEqual(len(f.driver.calls), 2)
        self.assertEqual([e.payload["name"] for e in f.events("team.tool_finished")], ["finish_work", "read_delivery"])
        self.assertTrue(any('"delivery"' in row.get("content", "") for row in f.driver.calls[-1]["history"] if row["role"] == "tool"))

    def test_new_objective_in_same_batch_continues_without_reusing_old_final(self):
        f = self.f
        f.driver.steps[f.manager] = [simple.turn("", ("finish_work", {
            "summary": "旧结果。", "verification": "Fixture only."}),
            ("set_objective", {"goal": "处理新的说明"})), simple.turn("正在处理新的说明。")]
        f.rt.team.receive(f.task, "收好已有结果，再处理新说明")
        f.run_ok()
        self.assertEqual(len(f.driver.calls), 2)
        self.assertEqual(f.rt.team.view(f.task)["status"], "running")
        self.assertEqual(f.events("msg.said")[-1].payload["text"], "正在处理新的说明。")
        self.assertNotIn("delivery_seq", f.events("msg.said")[-1].payload)

    def test_rejected_finish_can_be_corrected_on_next_model_turn(self):
        f = self.f
        f.driver.steps[f.manager] = [simple.turn("", ("finish_work", {
            "summary": "", "verification": "Fixture only."})), simple.turn("", ("finish_work", {
            "summary": "补全后的结果。", "verification": "Fixture only."}))]
        f.rt.team.receive(f.task, "交付结果")
        f.run_ok()
        self.assertEqual(len(f.driver.calls), 2)
        self.assertEqual(len(f.events("team.objective_completed")), 1)
        self.assertEqual(f.events("msg.said")[-1].payload["text"], "补全后的结果。")

    def test_later_customer_question_runs_normally_without_reopening_delivery(self):
        f = self.f
        f.driver.steps[f.manager] = [simple.turn("", ("finish_work", {
            "summary": "结果已保存。", "verification": "Fixture only."}))]
        f.rt.team.receive(f.task, "交付结果")
        f.run_ok()
        f.driver.steps[f.manager] = [simple.turn("", ("read_delivery", {})), simple.turn("刚才的结论仅覆盖示例。")]
        f.rt.team.receive(f.task, "刚才这个结论适用于所有情况吗？")
        f.run_ok()
        self.assertEqual(len(f.driver.calls), 3)
        self.assertEqual(len(f.events("team.objective_completed")), 1)
        self.assertEqual(f.events("msg.said")[-1].payload["text"], "刚才的结论仅覆盖示例。")
        self.assertNotIn("delivery_seq", f.events("msg.said")[-1].payload)

    def test_correcting_read_after_delivery_does_not_duplicate_final_message(self):
        f = self.f
        f.driver.steps[f.manager] = [simple.turn("", ("finish_work", {
            "summary": "结果已保存。", "verification": "Fixture only."}),
            ("read_delivery", {"delivery_seq": 0})), simple.turn("", ("read_delivery", {})), simple.turn("核对结果后收束。")]
        f.rt.team.receive(f.task, "交付并核对记录")
        f.run_ok()
        delivery, = f.events("team.objective_completed")
        self.assertEqual(len(f.driver.calls), 3)
        self.assertEqual(len([e for e in f.events("msg.said") if e.payload.get("delivery_seq") == delivery.seq]), 1)

    def test_successful_tool_envelope_with_failed_check_is_returned_to_model(self):
        f = self.f
        execute = f.rt.team._execute
        def checked(*args, **kwargs):
            if args[2] == "read_delivery":
                return {"status": "ok", "outcome": "failed", "complete": True, "note": "Simulated check envelope."}
            return execute(*args, **kwargs)
        f.driver.steps[f.manager] = [simple.turn("", ("read_delivery", {}), ("finish_work", {
            "summary": "已保存的结论。", "verification": "Fixture only."})), simple.turn("需要检查工具结果。")]
        f.rt.team.receive(f.task, "检查后交付")
        with patch.object(f.rt.team, "_execute", side_effect=checked):
            f.run_ok()
        self.assertEqual(len(f.driver.calls), 2)
        self.assertEqual(len(f.events("team.objective_completed")), 1)
        started, = [e for e in f.events("team.tool_started") if e.payload["name"] == "read_delivery"]
        finished, = [e for e in f.events("team.tool_finished")
                     if e.payload["started_seq"] == started.seq]
        returned, = [row for row in f.driver.calls[-1]["history"]
                     if row["role"] == "tool" and row["tool_call_id"] == started.payload["call_id"]]
        expected = {"status": "ok", "outcome": "failed", "complete": True,
                    "note": "Simulated check envelope."}
        self.assertEqual(finished.payload["result"], expected)
        self.assertEqual(json.loads(returned["content"]), expected)
        self.assertFalse(returned["is_error"], "An ok envelope still carries the actual failed outcome")

    def test_new_customer_message_after_finish_preempts_terminal_reply(self):
        f = self.f
        dispatch = f.rt.team._dispatch_tool
        def intervening(*args, **kwargs):
            result = dispatch(*args, **kwargs)
            if args[5].name == "finish_work":
                f.rt.team.receive(f.task, "等等，先解释这个结果的适用范围。")
            return result
        f.driver.steps[f.manager] = [simple.turn("", ("finish_work", {
            "summary": "旧结论。", "verification": "Fixture only."})), simple.turn("只适用于当前示例。")]
        f.rt.team.receive(f.task, "交付结果")
        with patch.object(f.rt.team, "_dispatch_tool", side_effect=intervening):
            f.run_ok()
        self.assertEqual(len(f.driver.calls), 2)
        self.assertEqual([e.payload["status"] for e in f.events("team.run_finished")], ["interrupted", "completed"])
        self.assertEqual([e.payload["text"] for e in f.events("msg.said")], ["只适用于当前示例。"])

    def test_terminal_reply_and_run_finalization_share_transaction(self):
        f = self.f
        emit = f.rt.log.emit
        observed = []
        def recorded(**kwargs):
            if kwargs["type"] == "msg.said" and "delivery_seq" in kwargs.get("payload", {}):
                observed.append(("reply", f.rt.log.db.in_transaction))
            if kwargs["type"] == "team.run_finished":
                observed.append(("finish", f.rt.log.db.in_transaction))
            return emit(**kwargs)
        f.driver.steps[f.manager] = [simple.turn("", ("finish_work", {
            "summary": "结果。", "verification": "Fixture only."}), ("wait_for_reply", {}))]
        f.rt.team.receive(f.task, "交付结果")
        with patch.object(f.rt.log, "emit", side_effect=recorded):
            f.run_ok()
        self.assertEqual(observed, [("reply", True), ("finish", True)])
        self.assertEqual(len(f.driver.calls), 1)

    def test_idempotent_finish_after_read_error_emits_only_one_final_reply(self):
        f = self.f
        args = {"summary": "已保存答案。", "verification": "Fixture only."}
        f.driver.steps[f.manager] = [simple.turn("", ("finish_work", args), ("read_delivery", {"delivery_seq": 0})),
                                    simple.turn("确认已保存。", ("finish_work", args))]
        f.rt.team.receive(f.task, "交付并核对")
        f.run_ok()
        self.assertEqual(len(f.driver.calls), 2)
        self.assertEqual(len(f.events("team.objective_completed")), 1)
        self.assertEqual(len(f.events("team.run_finished")), 1)
        self.assertEqual(len([e for e in f.events("msg.said") if "delivery_seq" in e.payload]), 1)

    def test_pause_after_finish_preempts_shortcut(self):
        f = self.f
        dispatch = f.rt.team._dispatch_tool
        def paused(*args, **kwargs):
            result = dispatch(*args, **kwargs)
            if args[5].name == "finish_work":
                f.rt.execution_control.change(f.task, "pause", request_id="pause-after-finish")
            return result
        f.driver.steps[f.manager] = [simple.turn("", ("finish_work", {
            "summary": "保存结果。", "verification": "Fixture only."}))]
        f.rt.team.receive(f.task, "交付")
        with patch.object(f.rt.team, "_dispatch_tool", side_effect=paused):
            f.run_ok()
        self.assertEqual(len(f.driver.calls), 1)
        self.assertEqual(f.events("team.run_finished")[-1].payload["status"], "interrupted")
        self.assertEqual(f.events("msg.said"), [])

    def test_failed_finalization_rolls_back_shortcut_reply(self):
        f = self.f
        finish = f.rt.team._finish_message
        def once(*args, **kwargs):
            if kwargs["status"] == "completed":
                raise RuntimeError("Injected local finalization failure")
            return finish(*args, **kwargs)
        f.driver.steps[f.manager] = [simple.turn("", ("finish_work", {
            "summary": "已记录答案。", "verification": "Fixture only."}))]
        f.rt.team.receive(f.task, "交付")
        with patch.object(f.rt.team, "_finish_message", side_effect=once):
            f.rt.collaborate(f.task)
        self.assertEqual(f.events("msg.said"), [])
        self.assertEqual(len(f.events("team.objective_completed")), 1)
        self.assertEqual([e.payload["status"] for e in f.events("team.run_finished")], ["failed"])
        self.assertTrue(f.rt.verify_chain().ok)


if __name__ == '__main__':
    unittest.main()
