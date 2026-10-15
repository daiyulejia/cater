"""Real source-copy execution and version-bound acceptance, without a paid model."""

from __future__ import annotations

import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile

from cater.drivers import DriverResponse
from cater.events import Actor
from cater.runtime import Runtime
from cater.tool_protocol import ToolCall, ToolTurn
from cater.work import WorkConflict
from cater.workspace_checks import captured_path
from test_delivery import DeliveryDriver
from test_work import WorkDriver, item


SOURCE = "def add(a, b):\n    return a + b\n"
TEST = "from calculator import add\nassert add(2, 3) == 5\nassert add(-2, 2) == 0\nprint('two arithmetic cases passed')\n"


class SourceDriver(WorkDriver):
    def __init__(self, *, bad=False):
        super().__init__()
        self.turns = 0
        self.bad = bad
        self.receipts = []

    def tool_turn(self, *, role, prompt, history, tools):
        self.turns += 1
        if not history:
            calls = [ToolCall("source", "workspace_write", {"path": "calculator.py", "content": SOURCE, "expected_sha256": None}),
                     ToolCall("test", "workspace_write", {"path": "check_calculator.py", "content": TEST if not self.bad else TEST.replace("== 5", "== 6"),
                                                               "expected_sha256": None})]
            return ToolTurn("", calls, 7, {"token_usage": "reported"},
                {"role": "assistant", "content": "", "tool_calls": [{"id": c.id, "name": c.name, "arguments": c.arguments} for c in calls]})
        self.receipts = [json.loads(h["content"]) for h in history if h["role"] == "tool"]
        raw = {"stance": "assert", "claim": "已实现并附源码", "text": "已保存实现和检查文件。",
               "workspace_files": ["calculator.py", "check_calculator.py"], "deliverables": []}
        return ToolTurn(json.dumps(raw), [], 11, {"token_usage": "reported"})

    def complete(self, **request):
        response = super().complete(**request)
        work = request["context"]["work"]
        if work.get("phase") == "review":
            data = json.loads(response.text)
            for row in data["checks"]:
                if row["id"] == "behavior" and work["local_checks"].get("behavior", {}).get("outcome") != "passed":
                    row.update(status="unknown", reason="测试预期写错，尚不能据此认定实现有误。")
            return DriverResponse(json.dumps(data), response.tokens, response.meta)
        return response


class WorkspaceChecksTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="cater-source-flow-")
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.root = self.base / "project"
        self.root.mkdir()
        self.config = self.base / "workspaces.json"
        self.config.write_text(json.dumps({"workspaces": [{"id": "project", "title": "测试工程", "root": str(self.root),
            "mode": "execute", "commands": {"python": [sys.executable, "-B"]},
            "checks": [{"id": "arithmetic", "title": "算术回归", "command": "python", "args": ["check_calculator.py"], "cwd": "."}]}]}), encoding="utf-8")
        self.driver = SourceDriver()
        self.rt = Runtime(self.base / "tasks.db", workspaces_path=self.config,
                          drivers={"dev": self.driver, "test": self.driver, "product": DeliveryDriver()})
        self.addCleanup(self.rt.close)
        self.task = self.rt.create_task("正确实现加法并保留回归检查")
        self.rt.workspaces.bind(self.task, "project", expected_revision=None)
        self.rt.plan_work(self.task, [item("implementation", local=True)])

    def test_real_files_stage_and_distinct_whole_check_use_submitted_bytes(self):
        self.rt.collaborate(self.task)
        work = self.rt.work.view(self.task)
        self.assertTrue(work["stage_accepted"], work)
        self.assertEqual((self.root / "calculator.py").read_text(), SOURCE)
        self.assertEqual(len(self.driver.receipts), 2)
        self.assertTrue(all(r["status"] == "ok" for r in self.driver.receipts))
        current = work["items"][0]
        artifacts = self.rt.work.artifacts(self.task, current["submission"])
        self.assertEqual({captured_path(self.rt, self.task, a) for a in artifacts}, {"calculator.py", "check_calculator.py"})
        stage = self.rt.work.checks(self.task, current)["behavior"]
        self.assertEqual(stage.payload["outcome"], "passed")
        self.assertEqual(stage.payload["check_kind"], "workspace")
        self.assertIn("two arithmetic cases passed", stage.payload["stdout"]["excerpt"])
        self.assertEqual(stage.payload["submission_digest"], current["submission"]["digest"])
        # The source in the user's directory can change; acceptance still targets submitted bytes.
        (self.root / "calculator.py").write_text("raise RuntimeError('unsubmitted later change')\n", encoding="utf-8")
        self.rt.delivery.prepare(self.task, reviewer="cabinet_product")
        delivery = self.rt.delivery.run(self.task)
        self.assertEqual(delivery["status"], "accepted", delivery)
        whole = self.rt.delivery._checks(self.task, delivery["digest"])["integration_behavior"]
        self.assertNotEqual(whole.seq, stage.seq)
        self.assertEqual(whole.payload["outcome"], "passed")
        self.assertEqual(self.rt._reported_driver_tokens("dev"), 18)
        self.assertEqual(self.rt.task(self.task).tokens, 38)
        self.assertNotIn("unavailable", self.rt.task(self.task).token_usage)
        self.assertTrue(self.rt.verify_chain().ok)
        with zipfile.ZipFile(io.BytesIO(self.rt.delivery.archive(self.task))) as archive:
            self.assertEqual(archive.read("calculator.py"), SOURCE.encode())
            self.assertEqual(archive.read("check_calculator.py"), TEST.encode())

    def test_failed_test_cannot_pass_or_force_source_rewrite_without_diagnosis(self):
        self.driver.bad = True
        self.rt.collaborate(self.task)
        current = self.rt.work.view(self.task)["items"][0]
        self.assertEqual(current["status"], "submitted")
        self.assertEqual((self.root / "calculator.py").read_text(), SOURCE)
        self.assertEqual(self.driver.turns, 2)
        checks = self.rt.work.checks(self.task, current)
        self.assertEqual(checks["behavior"].payload["outcome"], "failed")
        self.assertNotEqual(self.rt.delivery.view(self.task)["status"], "accepted")

    def test_same_submission_does_not_repeat_configured_checks(self):
        self.rt.collaborate(self.task)
        current = self.rt.work.view(self.task)["items"][0]
        before = self.rt.log.count()
        self.rt.workspace_checks.run(self.task, current)
        self.assertEqual(self.rt.log.count(), before)

    def test_model_cannot_forge_capture_by_describing_a_local_path(self):
        forged = {"attempt_id": "fake", "content_sha256": "fake", "filename": "calculator.py",
                  "workspace_file": {"path": "calculator.py", "filename": "calculator.py", "capture_seq": 1,
                                     "content_sha256": "fake"}}
        with self.assertRaises(WorkConflict):
            captured_path(self.rt, self.task, forged)

    def test_duplicate_or_parent_source_paths_fail_closed(self):
        self.rt.collaborate(self.task)
        submission = self.rt.work.view(self.task)["items"][0]["submission"]
        artifact = self.rt.work.artifacts(self.task, submission)[0]
        source = artifact["workspace_file"]
        self.rt.log.emit(type="workspace.files_captured", task_id=self.task, actor=Actor.system("workspace_gateway"),
            payload={"attempt_id": artifact["attempt_id"], "files": [{"path": "../outside.py", "filename": source["filename"],
                                                                     "content_sha256": source["content_sha256"]}]})
        capture = self.rt.log.read(task_id=self.task)[-1]
        artifact["workspace_file"] = {"path": "../outside.py", "filename": source["filename"],
                                      "content_sha256": source["content_sha256"], "capture_seq": capture.seq}
        with self.assertRaises(WorkConflict):
            captured_path(self.rt, self.task, artifact)


if __name__ == "__main__":
    unittest.main()
