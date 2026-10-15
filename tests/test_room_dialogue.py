from __future__ import annotations

import unittest

from cater.room_dialogue import build_request, validate_response


def response(action="discuss", **fields):
    return {"reply": "我们先把这部分聊清楚。", "action": action, "task_id": None,
            "goal": None, "acceptance": None, "quote": None, **fields}


class RoomDialogueTests(unittest.TestCase):
    def test_ordinary_discussion_carries_no_task_change(self):
        raw = response()
        self.assertEqual(validate_response(raw, message="先讨论系统设计", task_ids={"task_a"}), raw)
        for field, value in (("task_id", "task_a"), ("goal", "another goal"), ("acceptance", [])):
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_response({**raw, field: value}, message="先讨论系统设计", task_ids={"task_a"})

    def test_discussion_may_quote_the_customer_without_becoming_a_change(self):
        raw = response(quote="先聊聊天")
        self.assertEqual(validate_response(raw, message="先聊聊天，不安排新活", task_ids={"task_a"}), raw)
        with self.assertRaises(ValueError):
            validate_response({**raw, "quote": "伪造的原话"}, message="先聊聊天", task_ids={"task_a"})

    def test_new_work_does_not_reuse_existing_task_identity(self):
        raw = response("new_task", goal="整理交接说明", acceptance=[], quote="交接说明")
        self.assertEqual(validate_response(raw, message="另外做份交接说明", task_ids={"task_a"}), raw)
        for changes in ({"task_id": "task_a"}, {"goal": None}, {"acceptance": None}, {"quote": None}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_response({**raw, **changes}, message="另外做份交接说明", task_ids={"task_a"})

    def test_task_updates_reference_current_message_and_candidate(self):
        raw = response("update_task", task_id="task_a", quote="支持中文", acceptance=["支持中文"])
        self.assertEqual(validate_response(raw, message="原来的页面也要支持中文", task_ids={"task_a"}), raw)
        for changes in ({"task_id": "outside"}, {"task_id": ["task_a"]}, {"quote": "历史消息中支持联网"},
                        {"quote": " "}, {"goal": ""}, {"acceptance": [" "]}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_response({**raw, **changes}, message="原来的页面也要支持中文", task_ids={"task_a"})

    def test_explain_and_resume_never_smuggle_goal_or_acceptance_edits(self):
        for action in ("explain_task", "resume_task", "status_task"):
            raw = response(action, task_id="task_a", quote="这项工作")
            self.assertEqual(validate_response(raw, message="说说这项工作", task_ids={"task_a"}), raw)
            for field, value in (("goal", "changed"), ("acceptance", [])):
                with self.subTest(action=action, field=field), self.assertRaises(ValueError):
                    validate_response({**raw, field: value}, message="说说这项工作", task_ids={"task_a"})

    def test_executable_or_permission_fields_and_invalid_shape_are_rejected(self):
        for raw in (None, [], {**response(), "command": "run something"},
                    {**response(), "permissions": "all"}, {**response(), "action": []},
                    {**response(), "action": "accept_delivery"}, {**response(), "reply": " "}):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                validate_response(raw, message="hello", task_ids=set())

    def test_context_is_explicit_and_read_only_without_a_fake_task(self):
        tasks = [{"id": "task_a", "goal": "做页面", "status": "running"},
                 {"id": "task_b", "goal": "写文档", "status": "done"}]
        history = [{"actor": "human", "text": "先把页面做完"}]
        prompt, context = build_request(message="文档为什么这么写", history=history, tasks=tasks)
        self.assertEqual(context["context_scope"], "request")
        self.assertNotIn("task_id", context)
        self.assertTrue(context["conversation"]["read_only"])
        self.assertEqual(context["conversation"]["tasks"], tasks)
        self.assertIn("文档为什么这么写", prompt)

    def test_build_request_preserves_long_messages_or_rejects_without_truncation(self):
        message = "只改一个错别字，其他别动。" + "背景说明保持不变。" * 100
        prompt, context = build_request(message=message, history=[], tasks=[])
        self.assertEqual(context["conversation"]["message"], message)
        self.assertIn(message, prompt)
        with self.assertRaises(ValueError):
            build_request(message=message, history=[{"text": "大" * 50000}], tasks=[])
        for text in (None, " ", "a" * 32001):
            with self.subTest(text=str(text)[:20]), self.assertRaises(ValueError):
                build_request(message=text, history=[], tasks=[])
