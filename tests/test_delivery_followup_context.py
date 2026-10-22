"""Small historical sources reach both model transports, without new workflow steps."""
import hashlib
import json
import unittest
from unittest.mock import patch

from cater.events import Actor
from cater.runtime import Runtime
from cater.team import HOST
from cater.team_context import json_bytes
import test_simple_team as native
import test_team_external as external


def captured(text):
    return {"excerpt": text, "bytes": len(text.encode("utf-8")),
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "complete": True, "truncated": False, "utf8_valid": True}


class DeliveryFollowupContextTests(unittest.TestCase):
    def fixture(self, external_driver=False):
        f = external.TeamExternalTests() if external_driver else native.SimpleTeamTests()
        self.addCleanup(f.doCleanups)
        f.setUp()
        return f

    def seed(self, f, *, complete=True):
        emit = lambda kind, payload: f.rt.log.emit(type=kind, actor=HOST, task_id=f.task, payload=payload)
        message = f.rt.team.receive(f.task, "请核对这份实现")
        run = {"run_id": "historical-check", "role": f.manager, "message_seq": message.seq,
               "revision": 0, "control_seq": f.rt.execution_control.view(f.task)["seq"]}
        emit("team.run_started", run)
        source = "def total_cents(values):\n    return sum(value for value in values if value > 0)\n"
        tests = ("def test_input_is_not_modified(self):\n    values = [-20, 30]\n"
                 "    self.assertEqual(total_cents(values), 10)\n    self.assertEqual(values, [-20, 30])\n")
        receipts = []

        def receipt(name, arguments, result):
            start = emit("team.tool_started", {**run, "root": str(f.root), "name": name,
                "call_id": "historical-" + str(len(receipts)), "arguments": arguments})
            end = emit("team.tool_finished", {"started_seq": start.seq, "run_id": run["run_id"],
                "role": f.manager, "name": name, "call_id": start.payload["call_id"], "result": result})
            receipts.append(end)
            return end

        for path, text in (("total.py", source), ("tests/test_total.py", tests)):
            receipt("workspace_read", {"path": path}, {"status": "ok", "path": path,
                "content": text, "bytes": len(text.encode()), "returned_bytes": len(text.encode()),
                "offset": 0, "encoding": "utf-8", "truncated": False,
                "sha256": hashlib.sha256(text.encode()).hexdigest()})
        output = ("test_empty_is_zero ... ok\ntest_input_is_not_modified ... FAIL\n"
                  "    self.assertEqual(total_cents(values), 10)\nAssertionError: 30 != 10\n"
                  "Ran 2 tests in 0.001s\n\nFAILED (failures=1)\n")
        receipt("workspace_run", {"command": "python", "args": ["-m", "unittest", "-v"]}, {
            "status": "error", "outcome": "failed", "exit_code": 1, "complete": True,
            "truncated": False, "timed_out": False, "stdout": captured(""), "stderr": captured(output)})
        emit("team.run_finished", {**run, "status": "completed", "reason": "Historical fixture"})
        if not complete:
            return None, receipts, source, tests, output
        delivery = emit("team.objective_completed", {"summary": "已交付，等待追问", "verification": "记录范围",
            "objective_seq": f.events("team.started")[0].seq, "artifact_seqs": [], "handoff": {"version": 1, "why": "修复求和",
                "changed": [], "impact": [], "alternatives": [], "reproduce": [], "limits": [],
                "evidence": [{"source_seq": e.seq, "source_hash": e.hash, "kind": "tool_receipt",
                              "role": f.manager, "tool": e.payload["name"], "quote": "记录原文"} for e in receipts]}})
        return delivery, receipts, source, tests, output

    def test_real_finish_work_initial_objective_keeps_original_failure_and_full_reads(self):
        f = self.fixture()
        _, receipts, source, tests, output = self.seed(f, complete=False)
        f.driver.steps[f.manager] = [native.turn("已有检查可以交付，说明未通过的部分。", ("finish_work", {
            "summary": "已完成检查，发现求和问题。", "verification": "检查失败，尚未修复。",
            "handoff": {"why": "说明实际检查结果", "reproduce": [], "limits": ["尚未修复"],
                "evidence": [{"source_seq": e.seq, "quote": literal}
                             for e, literal in zip(receipts, (source, tests, output))]}}))]
        f.rt.team.receive(f.task, "给出已有检查结果，不继续修复")
        f.rt.collaborate(f.task)
        delivery, = f.events("team.objective_completed")
        self.assertGreater(delivery.payload["objective_seq"], 0)
        self.assertEqual({e.payload["revision"] for e in f.events("team.tool_started")}, {0})
        self.assertEqual(f.rt.team.view(f.task)["status"], "done")
        f.driver.steps[f.manager] = [native.turn("依据原始检查说明。")]
        f.rt.team.receive(f.task, "具体哪里失败了，哪些断言实际执行了？")
        f.rt.collaborate(f.task)
        self.assert_sources(f.driver.calls[-1]["context"], (delivery, receipts, source, tests, output))
        self.assertEqual(len(f.events("team.tool_started")), 4)
        self.assertTrue(f.rt.verify_chain().ok)

    def assert_sources(self, context, seeded):
        delivery, receipts, source, tests, output = seeded
        value = context["delivery_followup_sources"]
        self.assertEqual(value["delivery_seq"], delivery.seq)
        self.assertLessEqual(json_bytes(value), 4096)
        self.assertEqual({row["source_seq"] for row in value["sources"]}, {e.seq for e in receipts})
        serialized = json.dumps(value, ensure_ascii=False)
        for text in (source, tests, output):
            self.assertIn(json.dumps(text, ensure_ascii=False)[1:-1], serialized)

    def test_native_first_prompt_has_sources_without_mandatory_read_or_extra_role(self):
        f = self.fixture()
        seeded = self.seed(f)
        f.driver.steps[f.manager] = [native.turn("原始记录可核对，未重新执行工作。")]
        f.rt.team.receive(f.task, "这次到底证明了什么？")
        f.rt.collaborate(f.task)
        self.assertEqual(len(f.driver.calls), 1)
        self.assert_sources(f.driver.calls[0]["context"], seeded)
        self.assertEqual(f.rt.team.view(f.task)["revision"], seeded[0].seq)
        self.assertEqual(len(f.events("team.tool_started")), 3)
        self.assertTrue(f.rt.verify_chain().ok)

    def test_external_first_prompt_has_same_sources_without_read_or_approval(self):
        f = self.fixture(True)
        seeded = self.seed(f)
        f.driver.scripts[f.manager] = [lambda run: "原始记录可核对，未重新执行工作。"]
        f.rt.team.receive(f.task, "这次到底证明了什么？")
        f.rt.collaborate(f.task)
        self.assertEqual(f.driver.errors, [])
        self.assertEqual(len(f.driver.runs), 1)
        self.assert_sources(f.driver.runs[0].context, seeded)
        self.assertEqual(f.rt.team.view(f.task)["revision"], seeded[0].seq)
        self.assertEqual(len(f.events("team.tool_started")), 3)
        self.assertTrue(f.rt.verify_chain().ok)

    def test_new_objective_and_direct_worker_chat_do_not_import_old_delivery_sources(self):
        f = self.fixture()
        self.seed(f)
        worker = f.rt.team._role(f.worker)
        message = f.rt.team.receive(f.task, f"@{worker.name} 谈谈下一项工作")
        context = f.rt.team._context(f.task, message, worker)
        self.assertNotIn("delivery_followup_sources", context)
        f.rt.log.emit(type="team.objective_updated", actor=HOST, task_id=f.task, payload={"goal": "新的不同工程"})
        message = f.rt.team.receive(f.task, "从新目标开始")
        context = f.rt.team._context(f.task, message, f.rt.organization.leader)
        self.assertNotIn("delivery_followup_sources", context)
        self.assertEqual(f.driver.calls, [])

    def test_concurrent_goal_change_keeps_sources_at_same_context_snapshot(self):
        f = self.fixture()
        f.rt.log.db.execute("PRAGMA journal_mode=WAL")
        seeded = self.seed(f)
        message = f.rt.team.receive(f.task, "核对刚才那份结果")
        other = Runtime(f.rt.log.path, roles_path=f.roles, drivers={"team_test": f.driver}, close_drivers_on_close=False)
        self.addCleanup(other.close)
        read = f.rt.log.read
        changed = []

        def interleave(*args, **kwargs):
            rows = read(*args, **kwargs)
            if not changed:
                changed.append(other.log.emit(type="team.objective_updated", actor=HOST, task_id=f.task,
                                              payload={"goal": "更新后的任务"}))
            return rows

        with patch.object(f.rt.log, "read", side_effect=interleave):
            context = f.rt.team._context(f.task, message, f.rt.organization.leader)
        self.assertEqual(context["current_work"]["revision"], seeded[0].seq)
        self.assert_sources(context, seeded)
        fresh = f.rt.team._context(f.task, message, f.rt.organization.leader)
        self.assertEqual(fresh["current_work"]["revision"], changed[0].seq)
        self.assertNotIn("delivery_followup_sources", fresh)
