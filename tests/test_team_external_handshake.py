"""Offline handshake boundaries through the real team and external run."""

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from cater.acp import AcpPromptResult
from cater.events import EventLog
from cater.external_agent import QoderAcpDriver
from cater.runtime import Runtime
from cater.team import HOST


class TeamExternalHandshakeTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        config = json.loads((Path(__file__).resolve().parents[1]
                             / "cater/packs/default_roles.json").read_text(encoding="utf-8"))
        self.driver = QoderAcpDriver("handshake_fixture", "never-launch-qoder", "OfflineModel")
        config["organization"]["leader"]["driver"] = self.driver.name
        for role in config["roles"]:
            role["driver"] = self.driver.name
        roles = root / "roles.json"
        roles.write_text(json.dumps(config), encoding="utf-8")
        self.rt = Runtime(root / "team.db", roles_path=roles,
                          drivers={self.driver.name: self.driver})
        self.addCleanup(self.rt.close)
        self.task = self.rt.create_task("Offline handshake scope regression")
        self.rt.team.enable(self.task)
        self.sessions = []
        self.on_open = lambda: None
        owner = self

        class Session:
            def __init__(self, command, **kwargs):
                self.session_id = "offline-handshake"
                self.agent_info = {"name": "qoder-cli-cn"}
                self.prompts = []
                self.lifecycle = []
                self.durable_starts = []
                owner.sessions.append(self)

            def open(self, **kwargs):
                self.lifecycle.append("open")
                log = EventLog(owner.rt.log.path)
                try:
                    self.durable_starts = list(log.iter_events(
                        task_id=owner.task, event_types=("team.external_run_started",)))
                finally:
                    log.close()
                # Exercise a real service check just before the handshake changes scope.
                kwargs["service"]()
                owner.on_open()
                self.lifecycle.append("changed")
                return {"models": {"currentModelId": "offline-model", "availableModels": [
                    {"modelId": "offline-model", "name": owner.driver.model}]}}

            def prompt(self, prompt, **kwargs):
                self.prompts.append(prompt)
                return AcpPromptResult("Offline response", "end_turn")

            def cancel(self):
                self.lifecycle.append("cancel")

            def close(self):
                self.lifecycle.append("close")

        self.enterContext(patch("cater.external_agent.AcpSession", Session))
        # Keep checks inside one polling window regardless of machine/SQLite speed.
        self.enterContext(patch("cater.team_external.time", SimpleNamespace(monotonic=lambda: 100.0)))
        process = self.enterContext(patch("subprocess.Popen", side_effect=AssertionError(
            "Handshake regressions must never launch an external process")))
        self.addCleanup(process.assert_not_called)

    def events(self, kind):
        return list(self.rt.log.iter_events(task_id=self.task, event_types=(kind,)))

    def run_handshake(self):
        self.rt.team.receive(self.task, "Start the offline handshake")
        self.rt.collaborate(self.task)
        self.assertEqual(len(self.sessions), 1)
        session = self.sessions[0]
        start, = self.events("team.external_run_started")
        finish, = self.events("team.external_run_finished")
        self.assertEqual(session.durable_starts, [start])
        self.assertEqual(finish.payload["started_seq"], start.seq)
        self.assertEqual(finish.payload["started_hash"], start.hash)
        self.assertLess(start.seq, finish.seq)
        self.assertTrue(finish.payload["cleanup_complete"])
        self.assertEqual(self.rt.team.external_runs.pending(self.task), [])
        self.assertEqual(self.events("team.tool_started"), [])
        self.assertEqual(self.events("team.objective_completed"), [])
        self.assertTrue(self.rt.verify_chain().ok)
        return session, finish

    def assert_handshake_interrupted(self):
        session, finish = self.run_handshake()
        self.assertEqual(session.lifecycle, ["open", "changed", "cancel", "close"])
        self.assertEqual(session.prompts, [])
        self.assertFalse(finish.payload["prompt_attempted"])
        self.assertEqual(finish.payload["phase"], "failed")
        self.assertEqual(finish.payload["outcome"], "not_started")
        self.assertEqual(self.events("team.model_called"), [])
        self.assertEqual(self.events("msg.said"), [])
        finished, = self.events("team.run_finished")
        self.assertEqual(finished.payload["status"], "interrupted")

    def test_pause_during_handshake_prevents_prompt(self):
        self.on_open = lambda: self.rt.execution_control.change(
            self.task, "pause", request_id="handshake-pause")
        self.assert_handshake_interrupted()
        self.assertEqual(self.rt.execution_control.view(self.task)["state"], "paused")

    def test_goal_change_during_handshake_prevents_prompt(self):
        self.on_open = lambda: self.rt.log.emit(
            type="team.objective_updated", actor=HOST, task_id=self.task,
            payload={"goal": "New goal adopted during handshake"})
        self.assert_handshake_interrupted()
        self.assertEqual(self.rt.team.view(self.task)["objective"],
                         "New goal adopted during handshake")

    def test_rule_change_during_handshake_prevents_prompt(self):
        before = self.rt.rulebook.current_snapshot(self.task)["fingerprint"]
        self.on_open = lambda: self.rt.rulebook.record(
            "Discuss the updated requirements before implementing.", scope="task", task_id=self.task)
        self.assert_handshake_interrupted()
        self.assertNotEqual(self.rt.rulebook.current_snapshot(self.task)["fingerprint"], before)

    def test_driver_close_during_handshake_prevents_prompt(self):
        self.on_open = self.driver.close
        self.assert_handshake_interrupted()
        self.assertTrue(self.driver.closed)

    def test_unchanged_handshake_allows_prompt(self):
        session, finish = self.run_handshake()
        self.assertEqual(session.lifecycle, ["open", "changed", "close"])
        self.assertEqual(len(session.prompts), 1)
        self.assertTrue(finish.payload["prompt_attempted"])
        self.assertEqual(finish.payload["outcome"], "returned")
        self.assertEqual(len(self.events("team.model_called")), 1)
        finished, = self.events("team.run_finished")
        self.assertEqual(finished.payload["status"], "completed")


if __name__ == "__main__":
    unittest.main()
