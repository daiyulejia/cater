"""Same-input references retain exact text and native execution contracts."""

from __future__ import annotations

import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import zipfile

from cater.team_context import INPUT_SOURCES
import test_simple_team
import test_team_review_cycle


def exact_text(test, context, field):
    row = context[field]
    if isinstance(row.get("text"), str):
        return row["text"]
    test.assertIsNone(row["text"])
    test.assertIn(row["context_ref"], INPUT_SOURCES)
    source = context[row["context_ref"]]
    test.assertNotIn("context_ref", source)
    test.assertEqual((source["seq"], source["sender"]), (row["seq"], row["sender"]))
    test.assertIsInstance(source["text"], str)
    return source["text"]


class NativeContextReferenceTests(unittest.TestCase):
    setUp = test_simple_team.SimpleTeamTests.setUp

    def test_actual_native_call_keeps_customer_text_once_and_event_original_unchanged(self):
        text = "请保留这份完整要求：" + "月亮与三角一起核对边界。" * 500

        def reply(context, history, tools):
            self.assertEqual(exact_text(self, context, "message"), text)
            self.assertEqual(exact_text(self, context, "latest_customer_message"), text)
            self.assertIsNone(context["latest_customer_message"]["text"])
            self.assertIn("本轮同名字段", context["context_window"]["note"])
            return test_simple_team.turn("已收到完整要求，没有丢掉正文。")

        self.driver.steps[self.manager] = [reply]
        message = self.rt.team.receive(self.task, text, request_id="reference-full-customer")
        self.rt.collaborate(self.task)
        self.assertEqual(len(self.driver.calls), 1)
        stored = next(e for e in self.rt.team._messages(self.task) if e.seq == message.seq)
        self.assertEqual(stored.payload["text"], text)
        raw = self.rt.team._context(self.task, stored, self.rt.team._role(self.manager))
        self.assertEqual(raw["latest_customer_message"]["text"], text)
        self.assertNotIn("context_ref", raw["latest_customer_message"])
        self.assertTrue(self.rt.verify_chain().ok)

    def test_referenced_input_does_not_bypass_a_pause_before_tool_dispatch(self):
        text = "先讨论不要丢掉原文。" * 1000

        def paused(context, history, tools):
            self.assertEqual(exact_text(self, context, "latest_customer_message"), text)
            self.assertIsNone(context["latest_customer_message"]["text"])
            self.rt.execution_control.change(self.task, "pause", request_id="pause-reference-turn")
            return test_simple_team.turn("", ("publish_artifact", {
                "filename": "should-not-exist.txt", "content": "not dispatched", "summary": "not dispatched", "format": "text"}))

        self.driver.steps[self.manager] = [paused]
        self.rt.team.receive(self.task, text, request_id="reference-pause-customer")
        self.rt.team.run(self.task)
        self.assertEqual(len(self.driver.calls), 1)
        self.assertEqual(self.rt.execution_control.view(self.task)["state"], "paused")
        self.assertEqual(list(self.rt.log.iter_events(task_id=self.task, event_types=("team.tool_started",))), [])
        self.assertEqual(self.rt.task(self.task).deliverables, [])
        self.assertTrue(self.rt.verify_chain().ok)


class ReferenceReviewCycleTests(unittest.TestCase):
    def test_two_rework_rounds_keep_exact_sources_and_complete_native_history(self):
        fixture = test_team_review_cycle.TeamReviewCycleTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        native, send = fixture.driver.tool_turn, fixture.rt.team._send
        seen = []

        def checked(*, role, prompt, history, tools):
            context = json.loads(prompt.rsplit("\n", 1)[1])
            for field in INPUT_SOURCES:
                row = context.get(field)
                if not row or "context_ref" not in row:
                    continue
                source = next(e for e in fixture.rt.log.iter_events(task_id=fixture.task,
                    after_seq=row["seq"] - 1, before_seq=row["seq"] + 1))
                self.assertEqual(exact_text(self, context, field), source.payload["text"])
                seen.append((field, row["seq"]))
            return native(role=role, prompt=prompt, history=history, tools=tools)

        def long_assignment(task_id, sender, recipient, text, kind, **kwargs):
            if kind == "assignment":
                text += "\n完整输入不能省略：" + "月" * 5000
            return send(task_id, sender, recipient, text, kind, **kwargs)

        fixture.driver.tool_turn = checked
        with patch.object(fixture.rt.team, "_send", side_effect=long_assignment):
            fixture.test_two_rejections_then_approval_resume_author_before_delivery()
        self.assertTrue(any(field == "active_assignment" for field, _ in seen))
        self.assertEqual(fixture.protocol_errors, [])
        self.assertGreater(fixture.validated_continuations, 0)
        review, = fixture.rt.team_reviews.view(fixture.task)
        artifact = fixture.rt.task(fixture.task).deliverables[-1]
        self.assertEqual(review["status"], "approved")
        self.assertEqual(review["artifacts"][0]["seq"], artifact["seq"])
        self.assertEqual(Path(artifact["local_path"]).read_bytes(), artifact["content"].encode())
        with zipfile.ZipFile(io.BytesIO(fixture.rt.team.archive(fixture.task))) as archive:
            self.assertEqual(archive.read("result.txt"), artifact["content"].encode())


if __name__ == "__main__":
    unittest.main()
