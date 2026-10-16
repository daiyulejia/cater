"""Optional native integration with a real local MCP SDK fixture, never a model."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import shutil
import unittest
import zipfile

from cater.mcp import CALL, LIST, McpService
import test_mcp_team
import test_simple_team


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "tools/mcp-bridge/bridge.mjs"
FIXTURE = ROOT / "tools/mcp-bridge/test/fixture-server.mjs"


@unittest.skipUnless(os.environ.get("CATER_MCP_SDK_TESTS") == "1", "optional MCP SDK integration disabled")
class McpSdkTests(unittest.TestCase):
    setUp = test_simple_team.SimpleTeamTests.setUp
    exchange = test_mcp_team.McpTeamTests.exchange
    events = test_simple_team.SimpleTeamTests.events

    def configure(self, mode="normal"):
        node = shutil.which("node")
        self.assertIsNotNone(node, "Install Node and tools/mcp-bridge dependencies for this optional suite")
        self.assertTrue((ROOT / "tools/mcp-bridge/node_modules/@modelcontextprotocol/sdk").is_dir())
        self.service_log = self.root / "service.ndjson"
        self.state = self.root / "actual.txt"
        self.configuration = {"bridge_command": [node, str(BRIDGE)], "servers": [{
            "id": "notes", "title": "真实本地SDK测试", "description": "只修改临时目录中的验收文件",
            "command": [node, str(FIXTURE), mode, str(self.service_log), str(self.state)],
            "cwd": str(self.root), "roles": [r.id for r in [self.rt.organization.leader, *self.rt.worker_roles]],
            "tools": {"read_note": "read", "write_note": "write"}, "timeout": 15}]}
        self.config = self.root / "mcp.json"
        self.config.write_text(json.dumps(self.configuration, ensure_ascii=False), encoding="utf-8")
        self.rt.mcp.close()
        self.rt.mcp = McpService(self.rt, self.config)
        self.number = 0

    def arguments(self, name, *, role=None, **arguments):
        result, = self.exchange((LIST, {"server_id": "notes"}), role=role)
        self.assertEqual(result["status"], "ok", result)
        tool = next(row for row in result["tools"] if row["name"] == name)
        return {"server_id": "notes", "tool_name": name, "schema_sha256": tool["schema_sha256"], "arguments": arguments}

    def service_events(self):
        return [json.loads(line) for line in self.service_log.read_text(encoding="utf-8").splitlines()]

    def test_actual_sdk_write_repair_read_review_and_zip_same_bytes(self):
        self.configure()
        first = self.arguments("write_note", role=self.worker, text="初稿", behavior="error")
        error, = self.exchange((CALL, first), role=self.worker)
        self.assertEqual((error["status"], error["outcome"], error["complete"]), ("error", "tool_error", True))
        self.assertEqual(self.state.read_text(encoding="utf-8"), "初稿\n")
        fixed = self.arguments("write_note", role=self.worker, text="修正版")
        good, = self.exchange((CALL, fixed), role=self.worker)
        self.assertEqual(good["outcome"], "returned")
        read_args = self.arguments("read_note", role=self.worker)
        read, = self.exchange((CALL, read_args), role=self.worker)
        self.assertEqual(read["mcp_result"]["content"][0]["text"], "初稿\n修正版\n")
        publication, = self.exchange(("publish_artifact", {"filename": "result.txt", "format": "text",
            "content": self.state.read_text(encoding="utf-8"), "summary": "按实际文件交付，含修正记录"}), role=self.worker)
        self.assertEqual(publication["status"], "ok")
        reviewer = next(r.id for r in self.rt.worker_roles if r.id != self.worker)
        review, = self.exchange(("request_review", {"reviewer": reviewer,
            "artifact_seqs": [publication["artifact_seq"]], "instruction": "核对SDK实际文件和交付副本相同"}), role=self.worker)
        self.assertEqual(review["status"], "ok", review)
        review_read = self.arguments("read_note", role=reviewer)
        observed, = self.exchange((CALL, review_read), role=reviewer)
        self.assertEqual(observed["mcp_result"]["content"][0]["text"].encode(), self.state.read_bytes())
        approved, = self.exchange(("record_review", {"review_seq": review["review_seq"], "verdict": "approved",
            "feedback": "已独立读取实际文件，与发布内容逐字核对一致；只证明本地脚本集成"}), role=reviewer)
        self.assertEqual(approved["status"], "ok", approved)
        report = next(e for e in reversed(self.rt.team._messages(self.task))
                      if e.payload.get("kind") == "report" and e.payload.get("review_seq") == review["review_seq"])
        self.exchange(role=self.worker, source=report)
        delivered, = self.exchange(("finish_work", {"summary": "真实SDK本地流程已核对",
            "verification": "实际写入、错误后修正、独立读取和指定版本复核；未调用真实模型"}))
        self.assertEqual(delivered["status"], "ok", delivered)
        with zipfile.ZipFile(io.BytesIO(self.rt.team.archive(self.task))) as archive:
            self.assertEqual(archive.read("result.txt"), self.state.read_bytes())
        self.assertTrue(self.rt.verify_chain().ok)
        events = self.service_events()
        self.assertEqual(len([e for e in events if e["event"] == "spawn"]), 1)
        self.assertEqual(len([e for e in events if e["event"] == "initialized"]), 1)
        calls = [e for e in events if e["event"] == "call"]
        self.assertEqual([e["name"] for e in calls], ["write_note", "write_note", "read_note", "read_note"])
        self.assertTrue(all(e["meta"] for e in calls))
        self.rt.mcp.close()

    def test_actual_write_then_crash_unknown_no_replay_and_read_inspection(self):
        self.configure()
        arguments = self.arguments("write_note", text="只写一次", behavior="crash")
        unknown, = self.exchange((CALL, arguments))
        self.assertEqual((unknown["status"], unknown["complete"], unknown["outcome"]), ("error", False, "effects_unknown"))
        self.assertEqual(self.state.read_text(encoding="utf-8"), "只写一次\n")
        blocked, = self.exchange((CALL, arguments))
        self.assertEqual(blocked["code"], "mcp_operation_pending")
        read_args = self.arguments("read_note")
        observed, = self.exchange((CALL, read_args))
        self.assertEqual(observed["mcp_result"]["content"][0]["text"], "只写一次\n")
        calls = [e for e in self.service_events() if e["event"] == "call"]
        self.assertEqual([e["name"] for e in calls], ["write_note", "read_note"])
        self.assertEqual(len(self.rt.mcp.pending("notes")), 1)
        self.rt.mcp.close()

    def test_schema_change_is_pre_dispatch_rejection_not_unknown(self):
        self.configure(mode="changed")
        arguments = self.arguments("write_note", text="12345678901")
        rejected, = self.exchange((CALL, arguments))
        self.assertEqual((rejected["status"], rejected["complete"], rejected["outcome"]), ("error", True, "not_started"))
        self.assertFalse(self.state.exists())
        self.assertFalse([e for e in self.service_events() if e["event"] == "call"])
        self.rt.mcp.close()


if __name__ == "__main__":
    unittest.main()
