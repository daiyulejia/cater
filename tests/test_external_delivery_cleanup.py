"""Frozen delivery survives external cleanup failure with explicit uncertainty."""

import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from cater.external_agent import ExternalAgentRun, ExternalYield, QoderAcpDriver
from cater.runtime import Runtime
from cater.tool_protocol import ToolCall
from cater.web import _team_view


class ExternalDeliveryCleanupTests(unittest.TestCase):
    content = "Frozen published bytes\n"
    summary = "Offline delivery complete"

    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        config = json.loads((Path(__file__).resolve().parents[1]
                             / "cater/packs/default_roles.json").read_text(encoding="utf-8"))
        self.driver = QoderAcpDriver("cleanup_fixture", "never-launch-qoder", "OfflineModel")
        config["organization"]["leader"]["driver"] = self.driver.name
        for role in config["roles"]:
            role["driver"] = self.driver.name
        roles = root / "roles.json"
        roles.write_text(json.dumps(config), encoding="utf-8")
        self.rt = Runtime(root / "team.db", roles_path=roles,
                          drivers={self.driver.name: self.driver})
        self.addCleanup(self.rt.close)
        self.task = self.rt.create_task("Offline delivery cleanup regression")
        self.rt.team.enable(self.task)
        self.runs = []
        self.receipts = []
        self.lifecycle = []
        self.close_error = None
        self.next_tool_error = None
        original = self.driver.agent_run

        def capture_run(**kwargs):
            run = original(**kwargs)
            self.runs.append(run)
            return run

        self.enterContext(patch.object(self.driver, "agent_run", side_effect=capture_run))
        owner = self

        class Session:
            def __init__(self, command, **kwargs):
                self.session_id = "offline-cleanup"
                self.agent_info = {"name": "qoder-cli-cn"}

            def open(self, **kwargs):
                return {"models": {"currentModelId": "offline-model", "availableModels": [
                    {"modelId": "offline-model", "name": owner.driver.model}]}}

            def prompt(self, prompt, **kwargs):
                run = owner.runs[-1]
                owner.receipts.append(run._call(ToolCall("publish", "publish_artifact", {
                    "filename": "frozen.txt", "content": owner.content,
                    "summary": "Offline artifact", "format": "text"})))
                owner.receipts.append(run._call(ToolCall("finish", "finish_work", {
                    "summary": owner.summary, "verification": "Published bytes inspected offline"})))
                owner.revoked_at_finish = run._tools_revoked
                owner.archive_before_cleanup = owner.rt.team.archive(owner.task)
                # Attempt another mutation before transport cleanup has even begun.
                try:
                    run._call(owner.late_call())
                except ExternalYield as exc:
                    owner.next_tool_error = exc
                kwargs["service"]()
                raise AssertionError("Successful finish must yield before another prompt result")

            def cancel(self):
                owner.lifecycle.append("cancel")

            def close(self):
                owner.lifecycle.append("close")
                if owner.close_error is not None:
                    raise owner.close_error

        self.enterContext(patch("cater.external_agent.AcpSession", Session))
        process = self.enterContext(patch("subprocess.Popen", side_effect=AssertionError(
            "Delivery cleanup regressions must never launch a process")))
        self.addCleanup(process.assert_not_called)

    @staticmethod
    def late_call():
        return ToolCall("late", "publish_artifact", {
            "filename": "late.txt", "content": "Must not publish",
            "summary": "Late mutation", "format": "text"})

    def events(self, kind):
        return list(self.rt.log.iter_events(task_id=self.task, event_types=(kind,)))

    @staticmethod
    def archive_files(raw):
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            return {name: archive.read(name) for name in archive.namelist()}

    def deliver(self):
        self.rt.team.receive(self.task, "Publish and deliver offline")
        self.rt.collaborate(self.task)
        run, = self.runs
        self.assertIsInstance(run, ExternalAgentRun)
        self.assertEqual([receipt["status"] for receipt in self.receipts], ["ok", "ok"])
        self.assertTrue(self.revoked_at_finish)
        self.assertIsInstance(self.next_tool_error, ExternalYield)
        self.assertEqual(self.lifecycle, ["cancel", "close"])
        self.assertTrue(run._bridge._closed)
        before = self.rt.log.count()
        with self.assertRaises(ExternalYield):
            run._call(self.late_call())
        self.assertEqual(self.rt.log.count(), before)
        self.assertEqual([e.payload["name"] for e in self.events("team.tool_started")],
                         ["publish_artifact", "finish_work"])
        completion, = self.events("team.objective_completed")
        message, = self.events("msg.said")
        view = _team_view(self.rt, self.task)
        self.assertEqual(view["status"], "done")
        self.assertEqual(view["revision"], completion.seq)
        self.assertEqual(message.payload["delivery_seq"], completion.seq)
        self.assertEqual(message.payload["run_id"], completion.payload["run_id"])
        self.assertEqual(message.payload["audience"], "customer")
        self.assertEqual(completion.payload["summary"], self.summary)
        artifact, = self.rt.task(self.task).deliverables
        self.assertEqual(completion.payload["artifacts"], [{
            "seq": artifact["seq"], "filename": "frozen.txt",
            "content_sha256": hashlib.sha256(self.content.encode()).hexdigest()}])
        files = self.archive_files(self.rt.team.archive(
            self.task, expected_delivery_seq=completion.seq))
        self.assertEqual(files, self.archive_files(self.archive_before_cleanup))
        self.assertEqual(files["frozen.txt"], self.content.encode())
        self.assertNotIn("late.txt", files)
        self.assertEqual(self.rt.workspaces.pending(task_id=self.task), [])
        self.assertTrue(self.rt.verify_chain().ok)
        return run, message, view

    def test_failed_cleanup_preserves_delivery_and_reports_uncertainty(self):
        self.close_error = OSError("Injected unconfirmed process cleanup")
        run, message, view = self.deliver()
        self.assertFalse(run.state.cleanup_complete)
        self.assertIn(self.close_error, run.state.cleanup_failures)
        self.assertTrue(message.payload["text"].startswith(self.summary))
        self.assertIn("\u5df2\u53d1\u5e03\u6210\u679c\u53ef\u4e0b\u8f7d", message.payload["text"])
        self.assertIn("\u6e05\u7406\u5c1a\u672a\u786e\u8ba4", message.payload["text"])
        self.assertIn("\u9700\u6838\u5bf9\u8fdb\u7a0b", message.payload["text"])
        pending, = self.rt.team.external_runs.pending(self.task)
        start, = self.events("team.external_run_started")
        self.assertEqual(pending["started_seq"], start.seq)
        self.assertEqual(pending["outcome"], "unknown")
        self.assertFalse(pending["replay_authorized"])
        self.assertTrue(pending["run_ended"])
        self.assertEqual(view["effects_pending_count"], 1)
        finish, = self.events("team.external_run_finished")
        self.assertFalse(finish.payload["cleanup_complete"])
        host, = self.events("team.run_finished")
        self.assertEqual(host.payload["status"], "interrupted")

    def test_successful_cleanup_preserves_delivery_without_false_warning(self):
        run, message, view = self.deliver()
        self.assertTrue(run.state.cleanup_complete)
        self.assertEqual(run.state.cleanup_failures, ())
        self.assertEqual(message.payload["text"], self.summary)
        self.assertEqual(self.rt.team.external_runs.pending(self.task), [])
        self.assertEqual(view["effects_pending_count"], 0)
        finish, = self.events("team.external_run_finished")
        self.assertTrue(finish.payload["cleanup_complete"])
        host, = self.events("team.run_finished")
        self.assertEqual(host.payload["status"], "completed")


if __name__ == "__main__":
    unittest.main()
