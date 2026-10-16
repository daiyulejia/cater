"""Offline context-cost regressions, with no remote model invocation."""

from __future__ import annotations

import copy
import json
import unittest
from unittest.mock import patch

from cater.events import Actor
from cater.team import HOST
from cater.team_context import CONVERSATION_BYTES, history_window, json_bytes, model_context
from cater.tool_protocol import prepare_tool_turn
from test_simple_team import SimpleTeamTests, turn


class HistoryWindowTests(unittest.TestCase):
    tools = [{"name": "read_artifact", "description": "Read", "input_schema": {"type": "object"}}]

    def exchange(self, call_id, content="body", *, signed=False):
        call = {"id": call_id, "name": "read_artifact", "arguments": {"artifact_seq": 12}}
        assistant = {"role": "assistant", "content": "Inspect source", "tool_calls": [call]}
        if signed:
            assistant["provider_content"] = [
                {"type": "thinking", "thinking": "private", "signature": "signed"},
                {"type": "text", "text": "Inspect source"},
                {"type": "tool_use", "id": call_id, "name": "read_artifact", "input": {"artifact_seq": 12}},
            ]
        return [assistant, {"role": "tool", "content": content, "tool_call_id": call_id}]

    def test_small_history_is_untouched(self):
        history = self.exchange("one")
        tail, info = history_window(history, [], tools=self.tools)
        self.assertIs(tail, history)
        self.assertEqual(info["omitted_groups"], 0)

    def test_evicts_whole_groups_preserves_signed_provider_blocks(self):
        old = self.exchange("old", "x" * 20000)
        latest = self.exchange("latest", signed=True)
        history = old + latest
        saved = copy.deepcopy(history)
        tail, info = history_window(history, [
            {"call_id": "old", "started_seq": 20, "name": "read_artifact", "status": "ok"},
        ], tools=self.tools, budget=1000)
        self.assertEqual(tail, latest)
        self.assertEqual(history, saved)
        self.assertEqual(info["omitted_groups"], 1)
        self.assertEqual(info["omitted_receipts"][0]["started_seq"], 20)
        tools = [{"name": "read_artifact", "description": "Read", "input_schema": {"type": "object"}}]
        prepare_tool_turn(role="test", prompt="context", history=tail, tools=tools)

    def test_oversized_latest_group_is_kept_in_full(self):
        latest = self.exchange("new", "月" * 50000)
        tail, info = history_window(self.exchange("old") + latest, [], tools=self.tools, budget=1000)
        self.assertEqual(tail, latest)
        self.assertTrue(info["oversized_latest_group"])
        self.assertGreater(info["retained_json_bytes"], 1000)

    def test_multitool_batch_is_never_split(self):
        latest = self.exchange("a", "x" * 4000)
        latest[0]["tool_calls"].append({"id": "b", "name": "read_artifact", "arguments": {"artifact_seq": 13}})
        latest.append({"role": "tool", "content": "second result", "tool_call_id": "b"})
        tail, _ = history_window(self.exchange("old") + latest, [], tools=self.tools, budget=1000)
        self.assertEqual(tail, latest)

    def test_cannot_hide_an_incomplete_or_duplicate_exchange(self):
        incomplete = self.exchange("old")[:1]
        incomplete[0]["content"] = "x" * 4000
        for history in (incomplete + self.exchange("new"),
                        self.exchange("same", "x" * 4000) + self.exchange("same"),
                        self.exchange("old", "x" * 4000) + [{"role": "tool", "content": "bad", "tool_call_id": "bad"}]):
            with self.subTest(history_length=len(history)), self.assertRaises(ValueError):
                history_window(history, [], tools=self.tools, budget=1000)

    def test_eviction_cannot_mask_bad_provider_blocks_tool_names_arguments_or_result_flags(self):
        for kind in ("provider_blocks", "tool_name", "arguments", "is_error"):
            old = self.exchange("old", "x" * 4000, signed=True)
            if kind == "provider_blocks":
                old[0]["provider_content"][1]["text"] = "Changed outside signed transcript"
            elif kind == "tool_name":
                old[0]["tool_calls"][0]["name"] = "not_registered"
            elif kind == "arguments":
                old[0]["tool_calls"][0]["arguments"] = []
            else:
                old[1]["is_error"] = "not a bool"
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                history_window(old + self.exchange("new"), [], tools=self.tools, budget=1000)


class WindowedReviewCycleTests(unittest.TestCase):
    def test_two_rework_rounds_with_eviction_still_deliver_the_approved_bytes(self):
        import io
        import zipfile
        import test_team_review_cycle as review_cycle
        import test_simple_team as simple
        from cater.tool_protocol import anthropic_history, openai_history

        fixture = review_cycle.TeamReviewCycleTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        windows = []

        def checked_turn(*, role, prompt, history, tools):
            system, transcript, _, _ = prepare_tool_turn(role=role, prompt=prompt, history=history, tools=tools)
            anthropic_history(prompt, transcript)
            openai_history(system, prompt, transcript)
            context = json.loads(prompt.rsplit("\n", 1)[1])
            windows.append(context["tool_history_window"])
            return simple.TeamDriver.tool_turn(fixture.driver, role=role, prompt=prompt, history=history, tools=tools)

        fixture.driver.tool_turn = checked_turn
        with patch("cater.team.history_window", side_effect=lambda history, receipts, **kwargs:
                   history_window(history, receipts, **kwargs, budget=1)):
            fixture.test_two_rejections_then_approval_resume_author_before_delivery()
        self.assertTrue(any(window["omitted_groups"] > 0 for window in windows))
        artifact = fixture.rt.task(fixture.task).deliverables[-1]
        review, = fixture.rt.team_reviews.view(fixture.task)
        self.assertEqual(review["status"], "approved")
        self.assertEqual(review["artifacts"][0]["seq"], artifact["seq"])
        with zipfile.ZipFile(io.BytesIO(fixture.rt.team.archive(fixture.task))) as archive:
            self.assertEqual(archive.read("result.txt"), artifact["content"].encode("utf-8"))


class TeamContextCostTests(SimpleTeamTests):
    def context(self, message, role_id=None):
        return self.rt.team._context(self.task, message, self.rt.team._role(role_id or self.manager))

    def test_projection_preserves_inputs_reviews_rules_and_full_public_history(self):
        for index in range(18):
            self.rt.team._send(self.task, self.rt.team._role(self.manager), self.worker,
                               "Long previous assignment " + "x" * 3000, "assignment")
        message = self.rt.team.receive(self.task, "新的目标不能丢掉" + "月" * 3000, request_id="cost-pinned-customer")
        original = self.context(message)
        original["reviews"] = [{"request_seq": 22, "feedback": "Exact feedback " + "y" * 20000}]
        original["rules"] = [{"text": "Exact rule " + "z" * 20000}]
        saved = copy.deepcopy(original)
        projected = model_context(original)
        self.assertEqual(original, saved)
        self.assertEqual(projected["message"], original["message"])
        self.assertEqual(projected["latest_customer_message"]["text"], message.payload["text"])
        self.assertEqual(projected["reviews"], original["reviews"])
        self.assertEqual(projected["rules"], original["rules"])
        self.assertNotIn("assignments", projected["current_work"])
        self.assertNotIn("reviews", projected["current_work"])
        self.assertEqual(len(self.rt.team.view(self.task)["assignments"]), 18)
        self.assertLess(json_bytes(projected["conversation"]), CONVERSATION_BYTES)
        self.assertTrue(self.rt.verify_chain().ok)

    def test_old_still_running_role_is_not_lost_among_recent_finished_runs(self):
        message = self.rt.team.receive(self.task, "看看谁还没完成", request_id="cost-pin-running-role")
        context = self.context(message)
        running = {"message_seq": 10, "role": self.worker, "status": "running", "run_id": "still-working"}
        context["current_work"]["runs"] = [running] + [
            {"message_seq": 20 + index, "role": self.manager, "status": "completed"} for index in range(20)]
        projected = model_context(context)
        self.assertIn(running, projected["current_work"]["runs"])
        self.assertEqual(len(projected["current_work"]["runs"]), 9)
        self.assertEqual(projected["current_work"]["runs_total"], 21)

    def test_latest_customer_update_is_pinned_even_when_chat_window_is_full(self):
        customer = self.rt.team.receive(self.task, "改成另一套方案，先讨论别开工", request_id="cost-latest-direction")
        for index in range(65):
            self.rt.log.emit(type="msg.said", task_id=self.task, actor=self.rt._leader_actor(),
                             payload={"text": "旧任务进展" * 1000})
        message = self.rt.team._send(self.task, self.rt.team._role(self.manager), self.worker, "继续检查", "assignment")
        projected = model_context(self.context(message, self.worker))
        self.assertEqual(projected["latest_customer_message"]["seq"], customer.seq)
        self.assertEqual(projected["latest_customer_message"]["text"], customer.payload["text"])
        self.assertEqual(projected["active_assignment"]["text"], "继续检查")

    def test_conversation_reader_returns_exact_excerpt_source_without_waking_peers(self):
        sent = self.rt.team._send(self.task, self.rt.team._role(self.manager), self.worker,
                                  "Full original " + "月" * 9000, "notification")
        self.driver.steps[self.manager] = [
            turn("", ("read_messages", {"scope": "conversation", "after_seq": sent.seq - 1, "limit": 1})),
            turn("已读原文。"),
        ]
        self.rt.team.receive(self.task, "补读", request_id="cost-read-conversation")
        self.run_ok()
        receipt, = [e for e in self.events("team.tool_finished") if e.payload["name"] == "read_messages"]
        row, = receipt.payload["result"]["messages"]
        self.assertEqual(row["seq"], sent.seq)
        self.assertEqual(row["text"], sent.payload["text"])
        self.assertEqual(len(self.driver.calls), 2)

    def test_runtime_windows_large_history_and_records_only_size_telemetry(self):
        artifact = self.rt.log.emit(type="deliverable.published", task_id=self.task,
            actor=Actor.agent(self.worker, "dev"), payload={
                "filename": "large.txt", "content": "x" * 65000, "format": "text", "summary": "large",
            })
        def read(index):
            value = turn("", ("read_artifact", {"artifact_seq": artifact.seq}))
            call_id = f"cost-call-{index}"
            from cater.tool_protocol import ToolCall, ToolTurn
            return ToolTurn(value.text, [ToolCall(call_id, value.calls[0].name, value.calls[0].arguments)],
                value.tokens, value.meta, {**value.message, "tool_calls": [
                    {**value.message["tool_calls"][0], "id": call_id}]})
        self.driver.steps[self.manager] = [read(1), read(2), read(3), turn("已核对。")]
        self.rt.team.receive(self.task, "读取实际成果", request_id="cost-long-tool-history")
        self.run_ok()
        last = self.driver.calls[-1]
        self.assertEqual(len(last["history"]), 2)
        self.assertEqual(json.loads(last["history"][-1]["content"])["content"], "x" * 65000)
        window = last["context"]["tool_history_window"]
        self.assertEqual(window["omitted_groups"], 2)
        self.assertEqual(len(window["omitted_receipts"]), 2)
        calls = self.events("team.model_called")
        self.assertEqual(len(calls), 4)
        sizes = calls[-1].payload["request_sizes"]
        self.assertEqual(sizes["history_json_bytes"], json_bytes(last["history"]))
        self.assertEqual(sizes["history_omitted_groups"], 2)
        self.assertNotIn("content", sizes)
        self.assertEqual(len([e for e in self.events("team.tool_finished") if e.payload["name"] == "read_artifact"]), 3)

    def test_runtime_persists_only_allowlisted_usage_without_double_counting(self):
        from cater.tool_protocol import ToolTurn
        value = turn("已收到。")
        self.driver.steps[self.manager] = [ToolTurn(value.text, [], 130, {
            "token_usage": "reported", "usage": {"prompt_tokens": 100, "completion_tokens": 30,
                "total_tokens": 130, "prompt_tokens_details": {"cached_tokens": 80, "secret": "DO_NOT_LOG"},
                "secret": "DO_NOT_LOG", "invalid": True}, "private": "DO_NOT_LOG",
        }, value.message)]
        self.rt.team.receive(self.task, "看下费用", request_id="cost-usage-whitelist")
        self.run_ok()
        event, = self.events("team.model_called")
        self.assertEqual(event.payload["tokens"], 130)
        self.assertEqual(event.payload["usage"], {"prompt_tokens": 100, "completion_tokens": 30,
            "total_tokens": 130, "prompt_tokens_details": {"cached_tokens": 80}})
        self.assertNotIn("DO_NOT_LOG", json.dumps(event.payload))

    def test_reusing_an_evicted_tool_id_is_rejected_before_any_new_tool_effect(self):
        from cater.tool_protocol import ToolCall, ToolTurn
        artifact = self.rt.log.emit(type="deliverable.published", task_id=self.task,
            actor=Actor.agent(self.worker, "dev"), payload={
                "filename": "large.txt", "content": "x" * 65000, "format": "text", "summary": "large"})
        def read(call_id):
            args = {"artifact_seq": artifact.seq}
            return ToolTurn("", [ToolCall(call_id, "read_artifact", args)], 7, {"token_usage": "reported"}, {
                "role": "assistant", "content": "", "tool_calls": [
                    {"id": call_id, "name": "read_artifact", "arguments": args}]})
        self.driver.steps[self.manager] = [read("old"), read("new"), read("old")]
        self.rt.team.receive(self.task, "核对大文件", request_id="cost-duplicate-evicted-id")
        self.rt.team.run(self.task)
        self.assertEqual(len(self.driver.calls), 3)
        self.assertGreater(self.driver.calls[-1]["context"]["tool_history_window"]["omitted_groups"], 0)
        self.assertEqual(len(self.events("team.tool_finished")), 2)
        self.assertEqual(len(self.events("team.model_called")), 3)
        self.assertEqual(self.rt.team.view(self.task)["runs"][-1]["status"], "failed")

    def test_default_inbox_reader_does_not_include_other_recipients(self):
        message = self.rt.team._send(self.task, self.rt.team._role(self.manager), self.worker,
                                     "Only sent to worker", "notification")
        self.driver.steps[self.manager] = [turn("", ("read_messages", {"after_seq": message.seq - 1})),
                                           turn("已核对收件箱。")]
        self.rt.team.receive(self.task, "读收件箱", request_id="cost-inbox-compatible")
        self.run_ok()
        receipt, = [e for e in self.events("team.tool_finished") if e.payload["name"] == "read_messages"]
        self.assertNotIn(message.seq, [row["seq"] for row in receipt.payload["result"]["messages"]])


if __name__ == "__main__":
    unittest.main()
