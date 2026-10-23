"""Rejected input must not become a durable, unknown workspace effect."""

import json
import sys
import unittest
from unittest.mock import patch

from cater.workspace_tools import WorkspaceExecutor
from cater.workspaces import WorkspaceService
import test_simple_team


class ToolPreflightTests(unittest.TestCase):
    setUp = test_simple_team.SimpleTeamTests.setUp
    events = test_simple_team.SimpleTeamTests.events
    run_ok = test_simple_team.SimpleTeamTests.run_ok

    def bind_workspace(self):
        self.project = self.root / "project"
        self.project.mkdir()
        config = self.root / "workspaces.json"
        config.write_text(json.dumps({"workspaces": [{"id": "project", "title": "Project", "root": str(self.project),
            "mode": "execute", "commands": {"python": [sys.executable]}, "timeout": 10}]}), encoding="utf-8")
        self.rt.workspaces = WorkspaceService(self.rt, config)
        self.rt.workspaces.bind(self.task, "project", expected_revision=None)

    def test_bad_write_parameters_can_be_corrected_in_same_run_then_executed(self):
        self.bind_workspace()
        self.driver.steps[self.manager] = [
            test_simple_team.turn("", ("workspace_write", {
                "path": "hello.py", "content": "print('not-written')", "summary": "wrong tool", "format": "code"})),
            test_simple_team.turn("", ("workspace_write", {
                "path": "hello.py", "content": "print('actual-output')", "expected_sha256": None})),
            test_simple_team.turn("", ("workspace_run", {"command": "python", "args": ["hello.py"]})),
            test_simple_team.turn("Done"),
        ]
        self.rt.team.receive(self.task, "Write and check", request_id="correct-bad-write")
        self.run_ok()
        results = [e.payload["result"] for e in self.events("team.tool_finished")]
        self.assertEqual(results[0]["code"], "invalid_arguments")
        self.assertEqual(results[0]["outcome"], "not_started")
        self.assertTrue(results[0]["complete"])
        self.assertEqual(results[0]["required_fields"], ["expected_sha256", "path"])
        self.assertEqual([r["status"] for r in results], ["error", "ok", "ok"])
        self.assertIn("actual-output", json.dumps(results[2]))
        self.assertEqual((self.project / "hello.py").read_text(), "print('actual-output')")
        self.assertEqual(self.rt.workspaces.pending(root=str(self.project)), [])
        self.assertEqual(self.rt.team._pending_operations(self.task), [])
        feedback = self.driver.calls[1]["history"][-1]
        self.assertTrue(feedback["is_error"])
        self.assertEqual(json.loads(feedback["content"]), results[0])

    def test_missing_and_extra_fields_do_not_dispatch_any_external_executor(self):
        self.bind_workspace()
        cases = [
            ("workspace_write", {"path": "hello.py", "content": "not-written"}),
            ("workspace_write", {"path": "hello.py", "content": "not-written", "expected_sha256": None, "format": "code"}),
            ("workspace_run", {"args": ["-V"]}),
            ("publish_artifact", {"filename": "note.md"}),
            ("publish_workspace_file", {"path": "hello.py", "unknown": True}),
            ("browser_check", {}),
        ]
        # Browser schemas are optional; exercise only installed tools here.
        available = {t["name"] for t in self.rt.team._schemas(self.rt.organization.leader,
            WorkspaceExecutor(self.rt.workspaces.config["project"]["policy"]))}
        cases = [case for case in cases if case[0] in available]
        self.assertGreaterEqual(len(cases), 5)
        self.driver.steps[self.manager] = [test_simple_team.turn("", case) for case in cases] + [test_simple_team.turn("Correct later")]
        self.rt.team.receive(self.task, "Check fields", request_id="bad-external-fields")
        with patch.object(self.rt.team, "_execute", side_effect=AssertionError("Must not execute")):
            self.run_ok()
        self.assertEqual(len(self.events("team.tool_finished")), len(cases))
        self.assertTrue(all(e.payload["result"]["outcome"] == "not_started" for e in self.events("team.tool_finished")))
        self.assertEqual(self.rt.workspaces.pending(task_id=self.task), [])
        self.assertEqual(self.rt.team._pending_operations(self.task), [])
        self.assertEqual(list(self.project.iterdir()), [])

    def test_exception_after_file_write_remains_unknown_and_blocks_next_mutation(self):
        self.bind_workspace()
        execute = WorkspaceExecutor.execute

        def uncertain(executor, name, arguments):
            result = execute(executor, name, arguments)
            if name == "workspace_write":
                raise OSError("Private failure after replacement")
            return result

        self.driver.steps[self.manager] = [
            test_simple_team.turn("", ("workspace_write", {"path": "hello.py", "content": "actual", "expected_sha256": None})),
            test_simple_team.turn("", ("workspace_write", {"path": "other.py", "content": "must-not-write", "expected_sha256": None})),
            test_simple_team.turn("Need to inspect"),
        ]
        self.rt.team.receive(self.task, "Write", request_id="uncertain-after-write")
        with patch.object(WorkspaceExecutor, "execute", uncertain):
            self.run_ok()
        first, second = [e.payload["result"] for e in self.events("team.tool_finished")]
        self.assertEqual(first["outcome"], "effects_unknown")
        self.assertFalse(first["complete"])
        self.assertNotIn("Private failure", json.dumps(first))
        self.assertEqual(second["status"], "error")
        self.assertNotEqual(second.get("outcome"), "not_started")
        self.assertEqual((self.project / "hello.py").read_text(), "actual")
        self.assertFalse((self.project / "other.py").exists())
        self.assertEqual(len(self.rt.workspaces.pending(task_id=self.task)), 1)
        self.assertEqual(len(self.rt.team._pending_operations(self.task)), 1)

    def test_local_field_rejections_commit_only_failure_receipts(self):
        cases = [
            ("delegate", {"recipient": self.worker}),
            ("set_objective", {"goal": "Must not change", "unexpected": True}),
            ("send_message", {"recipient": self.worker}),
            ("finish_work", {"summary": "Must not finish"}),
        ]
        self.driver.steps[self.manager] = [test_simple_team.turn("", case) for case in cases] + [test_simple_team.turn("Correct later")]
        original = self.rt.team.view(self.task)["objective"]
        self.rt.team.receive(self.task, "Check local fields", request_id="bad-local-fields")
        with patch.object(self.rt.team, "_execute", side_effect=AssertionError("Must not execute")):
            self.run_ok()
        self.assertEqual(len(self.events("team.tool_finished")), len(cases))
        self.assertTrue(all(e.payload["result"]["code"] == "invalid_arguments" for e in self.events("team.tool_finished")))
        self.assertEqual(self.rt.team.view(self.task)["objective"], original)
        self.assertEqual(self.events("team.objective_updated"), [])
        self.assertEqual(self.events("team.objective_completed"), [])
        self.assertEqual(len(self.events("team.message_received")), 1)
        self.assertEqual(self.rt.team._pending_operations(self.task), [])


if __name__ == "__main__":
    unittest.main()
