"""Scripted native project reuse with actual files and commands, no model API."""

from __future__ import annotations

import copy
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import zipfile

from cater.project_history import TOOL
from cater.runtime import Runtime
from cater.workspaces import WorkspaceService
import test_mcp_team
import test_simple_team


OLD = "def total(values):\n    return sum(values)\n"
NEW = OLD + "\ndef average(values):\n    return total(values) / len(values) if values else 0\n"


class TeamProjectHistoryTests(unittest.TestCase):
    exchange = test_mcp_team.McpTeamTests.exchange
    events = test_simple_team.SimpleTeamTests.events

    def setUp(self):
        test_simple_team.SimpleTeamTests.setUp(self)
        self.number = 0
        self.project = self.rt.rulebook.create_project("同一项目")["id"]
        self.rt.rulebook.bind_project(self.task, self.project)
        self.source = self.rt.create_task("提供总数函数", project_id=self.project)
        self.rt.team.enable(self.source)
        self.reviewer = next(role.id for role in self.rt.worker_roles if role.id != self.worker)
        self.exchange(("set_objective", {"goal": "基于已交付的总数接口增加平均数功能，保留原接口并独立验证"}))

    def publication(self, task, content=OLD):
        result, = self.exchange(("publish_artifact", {"filename": "totals.py", "content": content,
            "summary": "历史函数版本", "format": "code"}), task=task, role=self.worker)
        self.assertEqual(result["status"], "ok", result)
        return result["artifact_seq"]

    def complete(self, task):
        result, = self.exchange(("finish_work", {"summary": "总数函数已保存", "verification": "本例的脚本验证范围，不能外推新需求"}), task=task)
        self.assertEqual(result["status"], "ok", result)
        return self.rt.team.view(task)["revision"]

    def context(self, task=None, role=None):
        task, role = task or self.task, role or self.manager
        member = self.rt.team._role(role)
        message = self.rt.team.receive(task, f"@{member.name} 看看项目背景", request_id=f"history-context-{len(self.rt.log.read())}")
        return self.rt.team._context(task, message, member)

    def actual_workspace(self):
        roots = {"old": self.root / "old-files", "new": self.root / "new-files"}
        for root in roots.values():
            root.mkdir()
        path = self.root / "workspaces.json"
        path.write_text(json.dumps({"workspaces": [{"id": key, "title": key, "root": str(root),
            "mode": "execute", "commands": {"python": [sys.executable]}, "timeout": 10}
            for key, root in roots.items()]}), encoding="utf-8")
        self.rt.workspaces = WorkspaceService(self.rt, path)
        self.rt.workspaces.bind(self.source, "old", expected_revision=None)
        self.rt.workspaces.bind(self.task, "new", expected_revision=None)
        return roots

    def approve(self, task, artifact):
        requested, = self.exchange(("request_review", {"reviewer": self.reviewer,
            "artifact_seqs": [artifact], "instruction": "读取实际文件并检查当前目标，不沿用旧交付结论"}), task=task, role=self.worker)
        self.assertEqual(requested["status"], "ok", requested)
        source = next(e for e in self.rt.team._messages(task) if e.seq == requested["message_seq"])
        checked, tested, approved = self.exchange(("workspace_read", {"path": "totals.py"}),
            ("workspace_run", {"command": "python", "args": ["-B", "-c",
                "from totals import total, average; assert total([2,4]) == 6; assert average([]) == 0; assert average([2,4]) == 3; print('independent-check')"]}),
            ("record_review", {"review_seq": requested["review_seq"], "verdict": "approved",
                              "feedback": "已读取当前文件并运行总数、空列表与平均数断言"}), task=task, role=self.reviewer, source=source)
        self.assertEqual([r["status"] for r in (checked, tested, approved)], ["ok"] * 3)
        report = next(e for e in self.rt.team._messages(task) if e.seq == approved["message_seq"])
        self.exchange(task=task, role=self.worker, source=report)

    def test_same_project_reuse_reads_old_version_but_new_task_verifies_its_own_files(self):
        roots = self.actual_workspace()
        saved, published = self.exchange(("workspace_write", {"path": "totals.py", "content": OLD, "expected_sha256": None}),
            ("publish_workspace_file", {"path": "totals.py", "summary": "实际文件的历史版本", "format": "code"}),
            task=self.source, role=self.worker)
        self.assertEqual((saved["status"], published["status"]), ("ok", "ok"))
        original_delivery = self.complete(self.source)
        context = self.context()
        index = context["project_history"]
        entry, = index["entries"]
        self.assertEqual(entry["delivery_seq"], original_delivery)
        self.assertEqual(entry["verification_kind"], "manager_declared")
        args = {"as_of_seq": index["as_of_seq"], "delivery_seq": original_delivery}
        note, body = self.exchange((TOOL, {"section": "delivery", **args}),
                                  (TOOL, {"section": "artifact", **args, "artifact_seq": published["artifact_seq"]}))
        self.assertEqual(note["status"], "ok")
        self.assertEqual(body["content"], OLD)
        self.assertEqual(self.rt.task(self.task).deliverables, [])
        self.assertEqual(list(roots["new"].iterdir()), [])
        written, tested, result = self.exchange(("workspace_write", {
            "path": "totals.py", "content": NEW, "expected_sha256": None}),
            ("workspace_run", {"command": "python", "args": ["-B", "-c",
                "from totals import average; assert average([]) == 0; assert average([2,4]) == 3; print('actual-new-goal')"]}),
            ("publish_workspace_file", {"path": "totals.py", "summary": "增加平均数且保留原总数接口", "format": "code"}), role=self.worker)
        self.assertEqual([r["status"] for r in (written, tested, result)], ["ok"] * 3)
        self.approve(self.task, result["artifact_seq"])
        foreign, = self.exchange(("finish_work", {"summary": "新功能交付", "verification": "已运行实际断言",
            "handoff": {"why": "新增平均数并保留总数接口", "reproduce": ["运行 Python 断言"], "limits": [],
                        "evidence": [{"source_seq": published["artifact_seq"], "quote": "return sum(values)"}]}}))
        self.assertEqual((foreign["status"], foreign["handoff_status"]), ("ok", "references_omitted_invalid"))
        completed, = self.events("team.objective_completed")
        saved = copy.deepcopy(completed.payload)
        final, = self.exchange(("finish_work", {"summary": "平均数功能交付", "verification": "已独立运行当前版本断言",
            "handoff": {"why": "依据历史接口新增平均数，实际重新验证", "reproduce": ["from totals import average; assert average([]) == 0"],
                        "limits": ["受控脚本，不是真实模型质量证明"],
                        "evidence": [{"source_seq": result["artifact_seq"], "quote": "def average(values):"}]}}))
        self.assertEqual((final["status"], final["code"], final["delivery_seq"]),
                         ("error", "delivery_already_completed", completed.seq))
        self.assertIsNot(final.get("already_completed"), True)
        self.assertEqual([e.payload for e in self.events("team.objective_completed")], [saved])
        self.assertEqual(completed.payload["handoff"]["evidence"], [])
        self.assertEqual(completed.payload["handoff"]["why"], "新增平均数并保留总数接口")
        self.assertEqual(completed.payload["handoff_status"], "references_omitted_invalid")
        with zipfile.ZipFile(io.BytesIO(self.rt.team.archive(self.task))) as archive:
            self.assertEqual(archive.read("totals.py"), (roots["new"] / "totals.py").read_bytes())
            self.assertEqual(archive.read("totals.py"), NEW.encode())
        self.assertEqual((roots["old"] / "totals.py").read_bytes(), OLD.encode())
        self.assertEqual(self.rt.team.view(self.source)["revision"], original_delivery)
        self.assertTrue(self.rt.verify_chain().ok)

    def test_identity_and_project_index_survive_runtime_restart_without_private_claim_copy(self):
        self.publication(self.source)
        self.complete(self.source)
        self.rt.log.emit(type="msg.said", task_id=self.source,
            actor=self.rt._leader_actor(), payload={"text": "PRIVATE_UNSHARED_CHAT", "claim": "PRIVATE_LAST_CLAIM"})
        context = self.context()
        self.assertNotIn("PRIVATE_", json.dumps(context))
        self.assertIn("project_history", context)
        with Runtime(self.rt.log.path, roles_path=self.roles, drivers={"team_test": self.driver}) as reopened:
            self.assertEqual(reopened.team._role(self.worker), self.rt.team._role(self.worker))
            self.assertEqual(reopened.team.project_history.index(self.task)["entries"], context["project_history"]["entries"])
            self.assertTrue(reopened.verify_chain().ok)

    def test_unbound_and_frozen_derived_conversations_have_no_live_project_reader(self):
        self.publication(self.source)
        self.complete(self.source)
        unbound = self.rt.create_task("独立未关联群")
        self.rt.team.enable(unbound)
        derived = self.rt.derive_task(self.source, "冻结背景的新方向", request_id="project-frozen-child")["task_id"]
        self.rt.team.enable(derived)
        for task in (unbound, derived):
            with self.subTest(task=task):
                context = self.context(task)
                self.assertNotIn("project_history", context)
                self.assertFalse(self.rt.team.project_history.available(task))
                self.assertNotIn(TOOL, {tool["name"] for tool in self.rt.team._schemas(
                    self.rt.organization.leader, None, has_project_history=self.rt.team.project_history.available(task))})
                self.assertEqual(self.rt.team.project_history.read(task, {"section": "index"})["outcome"], "not_started")
        frozen = json.dumps(self.rt.conversation_background(derived), sort_keys=True)
        self.exchange(("set_objective", {"goal": "新的实现方向"}), task=self.source)
        self.publication(self.source, "NEW_SOURCE_CONTENT")
        self.complete(self.source)
        self.assertEqual(json.dumps(self.rt.conversation_background(derived), sort_keys=True), frozen)
        self.assertNotIn("NEW_SOURCE_CONTENT", json.dumps(self.context(derived)))

    def test_paused_reader_is_readonly_and_does_not_restore_execution(self):
        artifact = self.publication(self.source)
        delivery = self.complete(self.source)
        self.rt.execution_control.change(self.task, "pause", request_id="project-read-while-paused")
        result, = self.exchange((TOOL, {"section": "artifact", "delivery_seq": delivery, "artifact_seq": artifact}))
        self.assertEqual(result["content"], OLD)
        self.assertEqual(self.rt.execution_control.view(self.task)["state"], "paused")
        names = {tool["name"] for tool in self.driver.calls[-1]["tools"]}
        self.assertIn(TOOL, names)
        self.assertNotIn("workspace_write", names)
        self.assertFalse(self.events("team.objective_completed"))

    def test_invalid_extra_parameters_do_not_persist_secret_or_read_history(self):
        with patch.object(self.rt.team.project_history, "read") as read:
            result, = self.exchange((TOOL, {"section": "index", "arguments": {"token": "FAKE_SECRET_DO_NOT_PERSIST"}}))
            self.assertEqual(result["outcome"], "not_started")
            read.assert_not_called()
        self.assertNotIn("FAKE_SECRET_DO_NOT_PERSIST", json.dumps([e.payload for e in self.events("team.tool_started")]))

    def test_new_customer_message_prevents_old_history_tool_turn(self):
        def changed(context, history, tools):
            self.rt.team.receive(self.task, "先不读取历史，处理新目标", request_id="project-history-new-input")
            return test_simple_team.turn("", (TOOL, {"section": "index"}))
        with patch.object(self.rt.team.project_history, "read") as read:
            self.assertEqual(self.exchange(step=changed), [])
            read.assert_not_called()
        self.assertEqual(self.events("team.run_finished")[-1].payload["status"], "interrupted")


if __name__ == "__main__":
    unittest.main()
