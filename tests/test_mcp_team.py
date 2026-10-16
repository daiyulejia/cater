"""Native MCP supervision with deterministic turns, no model or external service."""

from __future__ import annotations

import hashlib
import json
import sys
import unittest
from unittest.mock import Mock, patch

from cater.drivers import DriverUnavailable
from cater.events import Actor
from cater.mcp import CALL, LIST, McpService, read_only_call
from cater.team import HOST
from cater.work import WorkConflict
import test_simple_team


SHA = "a" * 64


def returned(*, error=False, partial=False):
    result = {"status": "error" if error else "ok", "complete": True,
              "outcome": "tool_error" if error else "returned",
              "mcp_result": {"content": [{"type": "text", "text": "本地工具回执"}], "isError": error}}
    if partial:
        result.update(representation_complete=False, result_bytes=300000, result_sha256=SHA,
                      unsupported_content_types=["image"])
    return {"result": result}


class McpTeamTests(unittest.TestCase):
    setUp = test_simple_team.SimpleTeamTests.setUp
    events = test_simple_team.SimpleTeamTests.events

    def configure(self, *, roles=None, tools=None):
        self.config = self.root / "mcp.json"
        entry = {"id": "notes", "title": "本地笔记", "description": "按需读取与更新笔记",
                 "command": [sys.executable], "cwd": str(self.root),
                 "tools": tools or {"read_note": "read", "write_note": "write"},
                 "roles": roles or [self.manager, self.worker]}
        self.configuration = {"bridge_command": [sys.executable], "servers": [entry, {**entry, "id": "other"}]}
        self.save_config()
        self.pool = Mock(spec=["exchange", "invalidate", "close"])
        self.pool.exchange.return_value = returned()
        self.rt.mcp.close()
        self.rt.mcp = McpService(self.rt, self.config, pool=self.pool)
        self.number = 0

    def save_config(self):
        self.config.write_text(json.dumps(self.configuration, ensure_ascii=False), encoding="utf-8")

    def args(self, tool="write_note", server="notes", **values):
        return {"server_id": server, "tool_name": tool, "schema_sha256": SHA,
                "arguments": {"text": "private-input-body", **values}}

    def exchange(self, *calls, task=None, role=None, step=None, source=None):
        task, role = task or self.task, role or self.manager
        self.number += 1
        target = self.rt.team._role(role)
        if source is None:
            source = self.rt.team.receive(task, f"@{target.name} 核对本地工具 {self.number}",
                                          request_id=f"mcp-native-{self.number}")
        self.assertEqual(source.payload["recipient"], role)
        started = self.rt.log.emit(type="team.run_started", task_id=task, actor=HOST, payload={
            "message_seq": source.seq, "role": role, "run_id": f"mcp-native-{self.number}",
            "revision": self.rt.team.view(task)["revision"],
            "control_seq": self.rt.execution_control.view(task)["seq"]})
        self.driver.steps[role] = [step or test_simple_team.turn("", *calls), test_simple_team.turn("")]
        list(self.rt.team._run_message(task, source, target, started))
        rows = [e for e in self.rt.log.read(task_id=task) if e.type == "team.tool_finished"
                and e.payload["run_id"] == started.payload["run_id"]]
        return [row.payload["result"] for row in rows]

    def old_call(self, *, task=None, tool="write_note", contract=True, result=None, actor=HOST):
        task = task or self.task
        arguments = self.args(tool)
        source = self.rt.team.receive(task, "核对原调用", request_id=f"old-mcp-{len(self.rt.log.read())}")
        run = self.rt.log.emit(type="team.run_started", actor=HOST, task_id=task, payload={
            "message_seq": source.seq, "role": self.manager, "run_id": f"old-mcp-{source.seq}",
            "revision": self.rt.team.view(task)["revision"], "control_seq": None})
        payload = {**run.payload, "name": CALL, "call_id": f"old-call-{source.seq}",
                   "arguments": self.rt.mcp.public_arguments(arguments)}
        if contract:
            payload["execution_contract"] = self.rt.mcp.contract(task, self.manager, arguments)
        start = self.rt.log.emit(type="team.tool_started", actor=actor, task_id=task, payload=payload)
        if result is not None:
            self.rt.log.emit(type="team.tool_finished", actor=HOST, task_id=task, payload={
                "started_seq": start.seq, "run_id": run.payload["run_id"], "role": self.manager,
                "name": CALL, "result": result})
        self.rt.log.emit(type="team.run_finished", actor=HOST, task_id=task, payload={**run.payload, "status": "interrupted"})
        return start

    def test_default_runtime_advertises_nothing_and_starts_nothing(self):
        role = self.rt.organization.leader
        with patch("cater.mcp.CliDriver") as process:
            names = {s["name"] for s in self.rt.team._schemas(role, None)}
            self.assertFalse({CALL, LIST} & names)
            source = self.rt.team.receive(self.task, "查看当前工作", request_id="mcp-default-context")
            self.assertNotIn("mcp_index", self.rt.team._context(self.task, source, role))
            process.assert_not_called()

    def test_context_has_only_scoped_metadata_and_discovery_is_lazy(self):
        self.configure(roles=[self.manager])
        source = self.rt.team.receive(self.task, "看看工具目录", request_id="mcp-index-context")
        context = self.rt.team._context(self.task, source, self.rt.organization.leader)
        index = context["mcp_index"]
        self.assertEqual(index["total"], 2)
        self.assertEqual(set(index["servers"][0]), {"id", "title", "description", "tool_count"})
        self.assertNotIn("bridge_command", json.dumps(index))
        manager = {s["name"] for s in self.rt.team._schemas(self.rt.organization.leader, None)}
        worker = {s["name"] for s in self.rt.team._schemas(self.rt.team._role(self.worker), None)}
        self.assertTrue({CALL, LIST} <= manager)
        self.assertFalse({CALL, LIST} & worker)
        self.pool.exchange.assert_not_called()

    def test_native_call_records_host_contract_but_not_argument_body(self):
        self.configure()
        result, = self.exchange((CALL, self.args()))
        self.assertEqual((result["status"], result["outcome"]), ("ok", "returned"))
        start, = [e for e in self.events("team.tool_started") if e.payload["name"] == CALL]
        contract = start.payload["execution_contract"]
        self.assertEqual(contract["effect"], "write")
        self.assertEqual(contract["configuration_sha256"], hashlib.sha256(self.config.read_bytes()).hexdigest())
        self.assertNotIn("private-input-body", json.dumps(start.payload))
        self.assertEqual(start.payload["arguments"]["arguments_bytes"], len(b'{"text": "private-input-body"}'))
        packet = self.pool.exchange.call_args.args[1]
        self.assertEqual(packet["arguments"], {"text": "private-input-body"})
        self.assertEqual(result["invocation_id"], f"{start.seq}:{start.payload['call_id']}")
        self.assertEqual(self.rt.team._pending_operations(self.task), [])
        self.assertTrue(self.rt.verify_chain().ok)

    def test_terminal_tool_error_can_be_corrected_without_manual_unlock(self):
        self.configure()
        self.pool.exchange.side_effect = [returned(error=True), returned()]
        failed, = self.exchange((CALL, self.args()))
        self.assertEqual((failed["complete"], failed["outcome"]), (True, "tool_error"))
        self.assertEqual(self.rt.team._pending_operations(self.task), [])
        corrected, = self.exchange((CALL, self.args(text="修正输入")))
        self.assertEqual(corrected["status"], "ok")
        self.assertEqual(self.pool.exchange.call_count, 2)

    def test_representation_incomplete_does_not_invent_unknown_effects(self):
        self.configure()
        self.pool.exchange.return_value = returned(partial=True)
        result, = self.exchange((CALL, self.args()))
        self.assertTrue(result["complete"])
        self.assertFalse(result["representation_complete"])
        self.assertEqual(self.rt.team._pending_operations(self.task), [])

    def test_representation_flag_cannot_hide_contradictory_or_missing_terminal_data(self):
        self.configure()
        for mutate in (lambda value: value.pop("mcp_result"),
                       lambda value: value["mcp_result"].update(isError=True)):
            with self.subTest(mutate=mutate):
                envelope = returned(partial=True)
                mutate(envelope["result"])
                self.pool.exchange.return_value = envelope
                result, = self.exchange((CALL, self.args(server="other")))
                if result.get("code") == "mcp_operation_pending":
                    pending, = self.rt.mcp.pending("other")
                    self.rt.team.recovery.reconcile_mcp_call(self.task, pending["started_seq"],
                        reason="内存fake未执行外部动作，已终止并核对", invocation_stopped=True)
                    result, = self.exchange((CALL, self.args(server="other")))
                self.assertEqual((result["complete"], result["outcome"]), (False, "effects_unknown"))

    def test_invalid_discovery_extra_parameters_never_persist_plaintext(self):
        self.configure()
        result, = self.exchange((LIST, {"server_id": "notes", "arguments": {"token": "FAKE_SECRET_DO_NOT_PERSIST"}}))
        self.assertEqual(result["outcome"], "not_started")
        serialized = json.dumps([e.payload for e in self.events("team.tool_started")])
        self.assertNotIn("FAKE_SECRET_DO_NOT_PERSIST", serialized)
        self.pool.exchange.assert_not_called()

    def test_unknown_write_blocks_same_server_across_groups_but_allows_read_and_other_server(self):
        self.configure()
        other = self.rt.create_task("另一个工作组")
        self.rt.team.enable(other)
        self.pool.exchange.side_effect = DriverUnavailable("private runtime error")
        unknown, = self.exchange((CALL, self.args()))
        self.assertEqual((unknown["complete"], unknown["outcome"]), (False, "effects_unknown"))
        self.assertNotIn("private runtime error", str(unknown))
        self.pool.invalidate.assert_called_once_with("notes")
        self.pool.exchange.side_effect = None
        self.pool.exchange.return_value = returned()
        blocked, = self.exchange((CALL, self.args()), task=other)
        self.assertEqual(blocked["code"], "mcp_operation_pending")
        self.assertEqual(self.pool.exchange.call_count, 1)
        read, = self.exchange((CALL, self.args("read_note")), task=other)
        independent, = self.exchange((CALL, self.args(server="other")), task=other)
        self.assertEqual((read["status"], independent["status"]), ("ok", "ok"))
        final, = self.exchange(("finish_work", {"summary": "交付", "verification": "核对"}))
        self.assertEqual(final["code"], "operation_pending")
        self.assertEqual(len(self.rt.mcp.pending("notes")), 1)

    def test_readonly_unknown_observable_but_does_not_block_finish(self):
        self.configure()
        old = self.old_call(tool="read_note")
        row, = self.rt.team._pending_operations(self.task)
        self.assertTrue(read_only_call(row))
        final, = self.exchange(("finish_work", {"summary": "无写入的状态检查结束", "verification": "读取结果未知，未作成功声明"}))
        self.assertEqual(final["status"], "ok")
        self.assertEqual(self.rt.team._pending_operations(self.task)[0]["started_seq"], old.seq)

    def test_missing_contract_is_not_trusted_readonly_and_forged_actor_not_authoritative(self):
        self.configure()
        self.old_call(tool="read_note", contract=False)
        self.old_call(tool="write_note", actor=Actor.agent(self.worker, self.rt.team._role(self.worker).role))
        rows = self.rt.mcp.pending("notes")
        self.assertEqual(len(rows), 1)
        self.assertFalse(read_only_call(rows[0]))

    def test_human_reconciliation_binds_original_call_and_does_not_replay(self):
        self.configure()
        old = self.old_call()
        with self.assertRaises(ValueError):
            self.rt.team.recovery.reconcile_mcp_call(self.task, old.seq, reason="核对")
        receipt = self.rt.team.recovery.reconcile_mcp_call(self.task, old.seq,
            reason="已检查本地服务日志及文件，确认执行停止且实际版本正确", invocation_stopped=True)
        self.assertEqual(receipt.payload["started_hash"], old.hash)
        self.assertEqual(receipt.payload["call_id"], old.payload["call_id"])
        self.assertEqual(receipt.actor, Actor.human())
        self.assertEqual(self.rt.mcp.pending("notes"), [])
        with self.assertRaises(WorkConflict):
            self.rt.team.recovery.reconcile_mcp_call(self.task, old.seq, reason="不能重复确认", invocation_stopped=True)
        self.pool.exchange.assert_not_called()

    def test_configuration_change_during_model_turn_prevents_old_call(self):
        self.configure()
        def change(context, history, tools):
            self.configuration["servers"][0]["tools"]["write_note"] = "read"
            self.save_config()
            return test_simple_team.turn("", (CALL, self.args()))
        result, = self.exchange(step=change)
        self.assertEqual(result["code"], "mcp_configuration_changed")
        self.assertEqual(result["outcome"], "not_started")
        self.pool.exchange.assert_not_called()

    def test_new_customer_message_prevents_old_call_after_model_returns(self):
        self.configure()
        def change(context, history, tools):
            self.rt.team.receive(self.task, "改方向，先不要写笔记", request_id="mcp-direction-change")
            return test_simple_team.turn("", (CALL, self.args()))
        self.assertEqual(self.exchange(step=change), [])
        self.pool.exchange.assert_not_called()
        self.assertEqual(self.events("team.run_finished")[-1].payload["status"], "interrupted")

    def test_pause_during_model_turn_prevents_old_call(self):
        self.configure()
        def change(context, history, tools):
            self.rt.execution_control.change(self.task, "pause", request_id="mcp-pause-during-turn")
            return test_simple_team.turn("", (CALL, self.args()))
        self.assertEqual(self.exchange(step=change), [])
        self.pool.exchange.assert_not_called()
        self.assertEqual(self.events("team.run_finished")[-1].payload["status"], "interrupted")


if __name__ == "__main__":
    unittest.main()
