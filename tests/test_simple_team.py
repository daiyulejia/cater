from __future__ import annotations

import copy
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile

from cater.events import Actor
from cater.runtime import Runtime
from cater.team import HOST
from cater.tool_protocol import ToolCall, ToolTurn
from cater.work import WorkConflict


def turn(text="", *calls):
    calls = [ToolCall(f"call-{i}", name, args) for i, (name, args) in enumerate(calls)]
    return ToolTurn(text, calls, 7, {"token_usage": "reported"}, {
        "role": "assistant", "content": text,
        "tool_calls": [{"id": c.id, "name": c.name, "arguments": c.arguments} for c in calls]})


class TeamDriver:
    name = "team_test"

    def __init__(self):
        self.steps = {}
        self.calls = []

    def tool_turn(self, *, role, prompt, history, tools):
        context = json.loads(prompt.rsplit("\n", 1)[1])
        self.calls.append({"context": context, "history": copy.deepcopy(history), "tools": copy.deepcopy(tools)})
        result = self.steps[context["you"]["id"]].pop(0)
        if callable(result):
            result = result(context, history, tools)
        if isinstance(result, Exception):
            raise result
        return result


class SimpleTeamTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        config = json.loads((Path(__file__).resolve().parents[1] / "cater/packs/default_roles.json").read_text(encoding="utf-8"))
        config["organization"]["leader"]["driver"] = "team_test"
        for role in config["roles"]:
            role["driver"] = "team_test"
        self.roles = self.root / "roles.json"
        self.roles.write_text(json.dumps(config), encoding="utf-8")
        self.driver = TeamDriver()
        self.rt = Runtime(self.root / "test.db", roles_path=self.roles, drivers={"team_test": self.driver})
        self.addCleanup(self.rt.close)
        self.task = self.rt.create_task("制作 doc 转 PDF 工具")
        self.manager = self.rt.organization.leader.id
        self.worker = self.rt.worker_roles[0].id
        self.rt.team.enable(self.task)

    def events(self, kind):
        return list(self.rt.log.iter_events(task_id=self.task, event_types=(kind,)))

    def run_ok(self):
        self.rt.collaborate(self.task)
        self.assertFalse([r for r in self.rt.team.view(self.task)["runs"] if r["status"] == "failed"])
        self.assertTrue(self.rt.verify_chain().ok)

    def test_manager_delegates_worker_executes_and_reports_without_legacy_planner(self):
        self.driver.steps = {
            self.manager: [turn("我来整理这个工具的交付。", ("delegate", {
                "recipient": self.worker, "instruction": "目标：写一份转换工具使用说明。输入：顾客需求。交付：guide.md，注明依赖与验证范围。"})),
                turn("已经交给伙伴，做好会回报。"),
                turn("我已读到回报，说明文档可以交付。", ("finish_work", {
                    "summary": "使用说明已保存，可打开下载。", "verification": "检查了说明正文，未实际转换办公文件。"})),
                turn("说明已准备好；实际转换尚未验证。")],
            self.worker: [turn("我先把使用方式和依赖交代清楚。", ("publish_artifact", {
                "filename": "guide.md", "content": "# 使用说明\n安装转换引擎后上传文档。\n尚未执行转换。",
                "summary": "工具使用说明与验证边界", "format": "markdown"})), turn("说明写好了，实际转换仍需验证。")],
        }
        self.rt.team.receive(self.task, "先整理工具说明", request_id="initial-message")
        self.run_ok()
        self.assertEqual(self.events("planning.started"), [])
        self.assertEqual(self.events("work.plan_created"), [])
        self.assertEqual(len(self.events("team.message_received")), 3)
        artifact, = self.rt.task(self.task).deliverables
        self.assertEqual(Path(artifact["local_path"]).read_text(encoding="utf-8"), artifact["content"])
        self.assertEqual(self.rt.task(self.task).status, "done")
        lenses = {lens["id"]: lens for lens in self.rt.review(self.task)["lenses"]}
        self.assertEqual(lenses["distribution"]["status"], "ok")
        self.assertEqual(lenses["logic"]["status"], "ok")
        self.assertEqual(self.rt.task(self.task).tokens, 42)
        self.assertEqual(self.rt._reported_driver_tokens("team_test"), 42)
        with zipfile.ZipFile(io.BytesIO(self.rt.team.archive(self.task))) as archive:
            self.assertEqual(archive.read("guide.md"), artifact["content"].encode())
            manifest = json.loads(archive.read("__cater__/manifest.json"))
            self.assertEqual(manifest["files"][0]["sha256"], hashlib.sha256(archive.read("guide.md")).hexdigest())

    def test_short_confirmation_adopts_history_and_progress_uses_new_objective(self):
        self.driver.steps[self.manager] = [turn("可以改成上传 Word 下载 PDF 的网页，我来安排。")]
        self.rt.team.receive(self.task, "改成网站，我上传Word后下载PDF", request_id="website-request")
        self.run_ok()
        def confirmed(context, history, tools):
            self.assertEqual(context["message"]["text"], "可以")
            self.assertTrue(any("改成网站" in row["text"] for row in context["conversation"]))
            return turn("按刚才的网页方案推进。", ("set_objective", {"goal": "上传 Word 并下载 PDF 的网站"}))
        self.driver.steps[self.manager] = [confirmed, turn("当前目标已更新。")]
        self.rt.team.receive(self.task, "可以", request_id="confirm-website")
        self.run_ok()
        def progress(context, history, tools):
            self.assertEqual(context["current_work"]["objective"], "上传 Word 并下载 PDF 的网站")
            self.assertEqual(context["message"]["text"], "还有多久")
            return turn("网站方案已确定，目前还没有登记实现成果，不能承诺完成时间。")
        self.driver.steps[self.manager] = [progress]
        self.rt.team.receive(self.task, "还有多久", request_id="progress-question")
        self.run_ok()
        self.assertEqual(self.rt.task(self.task).goal, "上传 Word 并下载 PDF 的网站")
        self.assertEqual(self.events("task.progress_reported"), [])

    def test_new_customer_direction_is_handled_between_worker_tool_turns(self):
        self.driver.steps[self.manager] = [turn("先交给你。", ("delegate", {
            "recipient": self.worker, "instruction": "实现命令行工具"})), turn("等候伙伴。")]
        def worker_started(context, history, tools):
            self.rt.team.receive(self.task, "不要命令行了，改成网页", request_id="mid-work-direction")
            return turn("我正在读取需求。", ("read_artifact", {"artifact_seq": 999999}))
        self.driver.steps[self.worker] = [worker_started]
        self.driver.steps[self.manager] += [turn("收到新方向，先停旧委托。", ("set_objective", {"goal": "制作转换网页"})),
            turn("已切换到网页方向。"), turn("旧委托的记录我保留了，接下来按网页安排。")]
        self.rt.team.receive(self.task, "做个工具", request_id="start-old-direction")
        self.run_ok()
        self.assertEqual(sum(c["context"]["you"]["id"] == self.worker for c in self.driver.calls), 1)
        self.assertEqual(self.rt.team.view(self.task)["objective"], "制作转换网页")
        self.assertTrue(any(r["status"] == "interrupted" for r in self.rt.team.view(self.task)["runs"]))

    def test_receive_is_idempotent_and_old_task_records_are_not_rewritten(self):
        first = self.rt.team.receive(self.task, "换一个方案", request_id="one-message-id")
        second = self.rt.team.receive(self.task, "换一个方案", request_id="one-message-id")
        self.assertEqual(first.seq, second.seq)
        with self.assertRaises(WorkConflict):
            self.rt.team.receive(self.task, "换成另一份内容", request_id="one-message-id")
        created, = self.events("task.created")
        self.assertEqual(created.payload["goal"], "制作 doc 转 PDF 工具")

    def test_direct_worker_chat_answers_without_waking_manager_after_completion(self):
        self.rt.log.emit(type="team.objective_completed", task_id=self.task, actor=HOST,
                         payload={"summary": "旧任务完成", "verification": "记录"})
        name = self.rt._role_by_id(self.worker).name
        self.driver.steps = {self.worker: [turn("可以，我解释一下上次的实现。")], self.manager: [turn("我也看到了这次沟通。") ]}
        self.rt.team.receive(self.task, f"@{name} 解释一下", request_id="direct-worker-chat")
        self.run_ok()
        self.assertEqual(len(self.driver.calls), 1)
        self.assertEqual(self.events("team.message_received")[-1].actor.kind, "human")
        reply, = self.events("msg.said")
        self.assertEqual(reply.payload["audience"], "customer")
        self.assertEqual(reply.payload["recipient"], "human")
        self.assertNotIn("delivery_seq", reply.payload)
        self.assertEqual(self.rt.task(self.task).status, "done")

    def test_paused_group_can_answer_but_has_no_execution_tools(self):
        self.rt.execution_control.change(self.task, "pause", request_id="pause-simple-team")
        def answer(context, history, tools):
            self.assertEqual({t["name"] for t in tools}, {"read_artifact", "read_receipts", "read_messages", "wait_for_reply"})
            return turn("当前已暂停，已有文件保留。")
        self.driver.steps[self.manager] = [answer]
        self.rt.team.receive(self.task, "现在什么情况", request_id="paused-progress")
        self.run_ok()

    def test_transport_failure_closes_only_this_message_and_later_chat_is_not_stuck(self):
        self.driver.steps[self.manager] = [RuntimeError("secret-should-not-leak"), turn("我收到新消息了。")]
        self.rt.team.receive(self.task, "第一次", request_id="failed-turn-first")
        self.rt.team.run(self.task)
        self.assertEqual(self.rt.team.view(self.task)["runs"][0]["status"], "failed")
        self.assertNotIn("secret-should-not-leak", json.dumps([e.payload for e in self.rt.log.read()]))
        lens = next(row for row in self.rt.review(self.task)["lenses"] if row["id"] == "reliability")
        self.assertEqual(lens["status"], "watch")
        self.assertIn("1 failed or interrupted native chat run(s)", lens["finding"])
        self.rt.team.receive(self.task, "第二次", request_id="next-turn-second")
        self.run_ok_after_failure()

    def run_ok_after_failure(self):
        self.rt.team.run(self.task)
        self.assertEqual(self.rt.team.view(self.task)["runs"][-1]["status"], "completed")

    def test_orphan_run_is_retired_without_replaying_original_message(self):
        first = self.rt.team.receive(self.task, "原请求", request_id="orphan-message-id")
        self.rt.log.emit(type="team.run_started", task_id=self.task, actor=HOST,
            payload={"message_seq": first.seq, "role": self.manager, "run_id": "dead-process", "control_seq": None})
        self.driver.steps[self.manager] = [turn("旧请求未重放，我来处理新问题。")]
        self.rt.team.receive(self.task, "现在如何", request_id="after-orphan-run")
        self.run_ok()
        self.assertEqual(len(self.driver.calls), 1)
        self.assertEqual(self.driver.calls[0]["context"]["message"]["text"], "现在如何")
        lens = next(row for row in self.rt.review(self.task)["lenses"] if row["id"] == "reliability")
        self.assertEqual(lens["status"], "watch")
        self.assertIn("1 failed or interrupted native chat run(s)", lens["finding"])

    def test_real_workspace_tools_can_write_and_run_without_old_work_contract(self):
        config = self.root / "workspaces.json"
        workspace = self.root / "project"
        workspace.mkdir()
        config.write_text(json.dumps({"workspaces": [{"id": "project", "title": "项目", "root": str(workspace),
            "mode": "execute", "commands": {"python": [sys.executable]}, "timeout": 10}]}), encoding="utf-8")
        from cater.workspaces import WorkspaceService
        self.rt.workspaces = WorkspaceService(self.rt, config)
        self.rt.workspaces.bind(self.task, "project", expected_revision=None)
        self.driver.steps[self.manager] = [turn("我来实际检查。", ("workspace_write", {
            "path": "hello.py", "content": "print('actual-output')\n", "expected_sha256": None})),
            turn("执行一下。", ("workspace_run", {"command": "python", "args": ["hello.py"]})), turn("命令已执行。")]
        self.rt.team.receive(self.task, "写文件并运行", request_id="real-workspace-operation")
        self.run_ok()
        self.assertTrue((workspace / "hello.py").is_file())
        results = [e.payload["result"] for e in self.events("team.tool_finished")]
        self.assertEqual(results[-1]["status"], "ok")
        self.assertIn("actual-output", json.dumps(results[-1]))

    def test_publish_workspace_file_uses_real_bytes_without_repeating_source(self):
        workspace = self.root / "source"
        workspace.mkdir()
        content = "<!doctype html><title>真实文件</title>\n"
        (workspace / "index.html").write_bytes(content.encode("utf-8"))
        config = self.root / "publish-workspaces.json"
        config.write_text(json.dumps({"workspaces": [{"id": "source", "title": "源码", "root": str(workspace), "mode": "read_only"}]}), encoding="utf-8")
        from cater.workspaces import WorkspaceService
        self.rt.workspaces = WorkspaceService(self.rt, config)
        self.rt.workspaces.bind(self.task, "source", expected_revision=None)
        self.driver.steps[self.manager] = [turn("", ("publish_workspace_file", {
            "path": "index.html", "summary": "直接发布真实文件", "format": "code"})), turn("文件已保存。")]
        self.rt.team.receive(self.task, "发布实际文件", request_id="publish-existing-file")
        self.run_ok()
        artifact, = self.rt.task(self.task).deliverables
        self.assertEqual(artifact["content"], content)
        self.assertEqual(Path(artifact["local_path"]).read_bytes(), content.encode())

    def test_original_customer_text_and_reserved_manifest_filename_are_preserved(self):
        text = "  第一行\n    保留缩进  "
        message = self.rt.team.receive(self.task, text, request_id="original-whitespace")
        self.assertEqual(message.payload["text"], text)
        self.driver.steps[self.manager] = [turn("", ("publish_artifact", {
            "filename": "cater-manifest.json", "content": "{\"customer\":true}", "summary": "顾客文件", "format": "code"})), turn("已保存。")]
        self.run_ok()
        with zipfile.ZipFile(io.BytesIO(self.rt.team.archive(self.task))) as archive:
            self.assertEqual(archive.read("cater-manifest.json"), b'{"customer":true}')
            self.assertIn("__cater__/manifest.json", archive.namelist())

    def test_receipt_queries_do_not_nest_their_own_previous_results(self):
        self.driver.steps[self.manager] = [turn("", ("read_receipts", {})), turn("", ("read_receipts", {})), turn("查好了。")]
        self.rt.team.receive(self.task, "查回执", request_id="receipt-no-recursion")
        self.run_ok()
        results = [e.payload["result"] for e in self.events("team.tool_finished") if e.payload["name"] == "read_receipts"]
        self.assertEqual([r["receipts"] for r in results], [[], []])

    def test_manager_can_reopen_completed_goal_for_a_followup_fix(self):
        goal = self.rt.team.view(self.task)["objective"]
        self.rt.log.emit(type="team.objective_completed", actor=HOST, task_id=self.task,
                         payload={"summary": "第一次完成", "verification": "已说明边界"})
        self.driver.steps[self.manager] = [turn("", ("set_objective", {"goal": goal}),
            ("publish_artifact", {"filename": "fix.md", "content": "修正后的说明", "format": "markdown", "summary": "后续修正"})), turn("已处理后续修改。")]
        self.rt.team.receive(self.task, "原目标不变，修正其中一处问题", request_id="fix-same-completed-goal")
        self.run_ok()
        self.assertEqual(self.rt.team.view(self.task)["status"], "running")
        self.assertEqual(self.rt.task(self.task).deliverables[-1]["filename"], "fix.md")

    def test_worker_publishes_then_waits_still_wakes_manager_for_supervision(self):
        self.driver.steps = {
            self.manager: [turn("", ("delegate", {"recipient": self.worker, "instruction": "写一份说明"})),
                turn("", ("wait_for_reply", {})),
                turn("我来检查刚发布的说明。", ("finish_work", {"summary": "说明已交付", "verification": "正文检查"})), turn("已准备好。")],
            self.worker: [turn("", ("publish_artifact", {"filename": "note.md", "content": "实际说明", "format": "markdown", "summary": "说明"})),
                turn("", ("wait_for_reply", {}))],
        }
        self.rt.team.receive(self.task, "安排伙伴写说明", request_id="publish-then-wait")
        self.run_ok()
        self.assertEqual(self.rt.team.view(self.task)["status"], "done")
        updates = [e for e in self.events("team.message_received") if e.payload["kind"] == "progress"]
        self.assertEqual(len(updates), 1)
        self.assertIn("note.md", updates[0].payload["text"])


if __name__ == "__main__":
    unittest.main()
