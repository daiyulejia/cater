"""Declared workspace arguments are rejected before filesystem/process dispatch."""

import io
import json
import sys
import unittest
from unittest.mock import patch
import zipfile

from cater.workspace_tools import WorkspaceExecutor
from cater.workspaces import WorkspaceService
import test_simple_team
import test_workspace_integration


class WorkspaceArgumentPreflightTests(unittest.TestCase):
    setUp = test_simple_team.SimpleTeamTests.setUp
    events = test_simple_team.SimpleTeamTests.events
    run_ok = test_simple_team.SimpleTeamTests.run_ok

    def bind_workspace(self):
        self.project = self.root / "project"
        self.project.mkdir()
        config = self.root / "workspaces.json"
        config.write_text(json.dumps({"workspaces": [{"id": "project", "title": "Project",
            "root": str(self.project), "mode": "execute", "commands": {"python": [sys.executable]}, "timeout": 5}]}), encoding="utf-8")
        self.rt.workspaces = WorkspaceService(self.rt, config)
        self.rt.workspaces.bind(self.task, "project", expected_revision=None)
        return WorkspaceExecutor(self.rt.workspaces.config["project"]["policy"])

    def test_bad_timeout_is_not_an_unknown_effect_and_corrected_run_can_continue(self):
        self.bind_workspace()
        self.driver.steps[self.manager] = [
            test_simple_team.turn("", ("workspace_run", {"command": "python", "args": ["-V"], "timeout": True})),
            test_simple_team.turn("", ("workspace_write", {"path": "check.py", "content": "print('corrected-run')", "expected_sha256": None})),
            test_simple_team.turn("", ("workspace_run", {"command": "python", "args": ["check.py"], "timeout": 5})),
            test_simple_team.turn("", ("publish_workspace_file", {"path": "check.py", "summary": "Actual checked source", "format": "code"})),
            test_simple_team.turn("", ("finish_work", {"summary": "Checked local output", "verification": "Observed Python stdout only; no broader behavior claim"})),
            test_simple_team.turn("Checked actual output"),
        ]
        self.rt.team.receive(self.task, "Run the local fixture", request_id="timeout-corrected-run")
        self.run_ok()
        bad, written, ran, published, finished = [e.payload["result"] for e in self.events("team.tool_finished")]
        self.assertEqual(bad["error"]["code"], "invalid_arguments")
        self.assertEqual(bad["outcome"], "not_started")
        self.assertIs(bad["complete"], True)
        self.assertEqual(bad["invalid_fields"], ["timeout"])
        self.assertEqual([written["status"], ran["status"]], ["ok", "ok"])
        self.assertIn("corrected-run", ran["stdout"]["excerpt"])
        self.assertEqual([published["status"], finished["status"]], ["ok", "ok"])
        self.assertEqual(self.rt.workspaces.pending(task_id=self.task), [])
        self.assertEqual(self.rt.team._pending_operations(self.task), [])
        self.assertEqual(self.rt.team.view(self.task)["status"], "done")
        completed, = self.events("team.objective_completed")
        with zipfile.ZipFile(io.BytesIO(self.rt.team.archive(self.task, expected_delivery_seq=completed.seq))) as archive:
            self.assertEqual(archive.read("check.py"), (self.project / "check.py").read_bytes())

    def test_invalid_types_ranges_and_elements_never_dispatch(self):
        executor = self.bind_workspace()
        cases = [
            ("workspace_run", {"command": "python", "timeout": value})
            for value in (True, False, 0, -1, "5", None, 6, float("nan"), float("inf"))
        ] + [
            ("workspace_run", {"command": "python", "args": value}) for value in (None, "PRIVATE_ARGUMENT", [False], [1], [None])
        ] + [
            ("workspace_read", {"path": "nothing", "offset": True}),
            ("workspace_read", {"path": "nothing", "limit": 65537}),
            ("workspace_write", {"path": "nothing", "content": 3, "expected_sha256": None}),
            ("workspace_write", {"path": "nothing", "content": "x", "expected_sha256": False}),
            ("workspace_search", {"query": False}),
            ("workspace_list", {"path": ["PRIVATE_ARGUMENT"]}),
        ]
        for name, arguments in cases:
            with self.subTest(name=name, arguments=arguments), \
                    patch.object(executor, "_" + name.removeprefix("workspace_"), side_effect=AssertionError("must not dispatch")):
                result = executor.execute(name, arguments)
                self.assertEqual(result["error"]["code"], "invalid_arguments")
                self.assertEqual(result["outcome"], "not_started")
                self.assertIs(result["complete"], True)
                self.assertNotIn("PRIVATE_ARGUMENT", json.dumps(result))
        self.assertEqual(list(self.project.iterdir()), [])

    def test_bad_utf8_lengths_and_unknown_fields_do_not_leak_values_or_dispatch(self):
        executor = self.bind_workspace()
        cases = [
            ("workspace_run", {"command": "python", "args": ["\ud800"]}),
            ("workspace_run", {"command": "PRIVATE_UNLISTED_PROGRAM"}),
            ("workspace_run", {"command": "python", "args": ["x"] * 101}),
            ("workspace_run", {"command": "python", "args": ["x" * 8193]}),
            ("workspace_write", {"path": "file.txt", "content": "\ud800", "expected_sha256": None}),
            ("workspace_write", {"path": "file.txt", "content": "x" * (1024 * 1024 + 1), "expected_sha256": None}),
            ("workspace_write", {"path": "PRIVATE_PATH", "content": "PRIVATE_CONTENT", "PRIVATE_UNKNOWN_FIELD": "PRIVATE_VALUE"}),
            ("workspace_read", {"path": "x" * 2049}),
            ("workspace_read", {"path": "x", "offset": 1.0}),
            ("workspace_read", None),
        ]
        for name, arguments in cases:
            with self.subTest(name=name), patch.object(executor, "_" + name.removeprefix("workspace_"), side_effect=AssertionError("must not dispatch")):
                result = executor.execute(name, arguments)
                self.assertEqual(result["outcome"], "not_started")
                self.assertIs(result["complete"], True)
                self.assertEqual(result["error"]["code"], "invalid_arguments")
                self.assertNotIn("PRIVATE_", json.dumps(result))
        self.assertEqual(list(self.project.iterdir()), [])

    def test_nullable_sha_real_utf8_and_fractional_timeout_keep_existing_semantics(self):
        executor = self.bind_workspace()
        body = "\u4e2d\u6587\r\n  source\n"
        written = executor.execute("workspace_write", {"path": "file.txt", "content": body, "expected_sha256": None})
        self.assertEqual(written["status"], "ok")
        source = executor.execute("workspace_read", {"path": "file.txt"})
        self.assertEqual(source["content"], body)
        updated = executor.execute("workspace_write", {"path": "file.txt", "content": "Updated\n", "expected_sha256": source["sha256"]})
        self.assertEqual(updated["status"], "ok")
        with patch.object(executor, "_run", return_value={"outcome": "passed"}) as run:
            result = executor.execute("workspace_run", {"command": "python", "args": [], "timeout": 4.5})
        self.assertEqual(result["status"], "ok")
        run.assert_called_once_with(command="python", args=[], timeout=4.5)
        self.assertEqual((self.project / "file.txt").read_bytes(), b"Updated\n")

    def test_post_dispatch_failure_stays_unknown_and_prevents_next_mutation(self):
        self.bind_workspace()

        def failed_run(executor, *args, **kwargs):
            (self.project / "partial.txt").write_bytes(b"Real partial side effect")
            raise OSError("PRIVATE_EXCEPTION_CONTENT")

        self.driver.steps[self.manager] = [
            test_simple_team.turn("", ("workspace_run", {"command": "python", "args": ["-V"], "timeout": 5})),
            test_simple_team.turn("", ("workspace_read", {"path": "partial.txt", "limit": True})),
            test_simple_team.turn("", ("workspace_write", {"path": "other.txt", "content": "Must not write", "expected_sha256": None})),
            test_simple_team.turn("", ("finish_work", {"summary": "Must not finish", "verification": "Unknown operation remains"})),
            test_simple_team.turn("Need actual reconciliation"),
        ]
        self.rt.team.receive(self.task, "Run fixture", request_id="post-dispatch-unknown")
        with patch.object(WorkspaceExecutor, "_run", failed_run):
            self.run_ok()
        first, rejected, second, finish = [e.payload["result"] for e in self.events("team.tool_finished")]
        self.assertEqual(first["outcome"], "effects_unknown")
        self.assertIs(first["complete"], False)
        self.assertNotIn("PRIVATE_EXCEPTION", json.dumps(first))
        self.assertEqual(rejected["outcome"], "not_started")
        self.assertIs(rejected["complete"], True)
        self.assertEqual(second["status"], "error")
        self.assertNotEqual(second.get("outcome"), "not_started")
        self.assertEqual((self.project / "partial.txt").read_bytes(), b"Real partial side effect")
        self.assertFalse((self.project / "other.txt").exists())
        self.assertEqual(len(self.rt.workspaces.pending(task_id=self.task)), 1)
        self.assertEqual(len(self.rt.team._pending_operations(self.task)), 1)
        self.assertEqual(finish["code"], "operation_pending")
        self.assertEqual(self.events("team.objective_completed"), [])

    def test_legacy_work_item_can_correct_arguments_in_the_same_claim(self):
        self.bind_workspace()
        self.rt.plan_work(self.task, [{"id": "source", "owner": "dev", "reviewer": "test",
            "objective": "Check actual source", "depends_on": [],
            "criteria": [{"id": "checked", "kind": "review", "text": "Check actual output"}]}])
        claim = self.rt.work.claim(self.task, "source")
        item = self.rt.work.view(self.task)["items"][0]
        context = {**claim.payload, "contract": item, "accepted_inputs": {}, "customer_directions": []}
        driver = test_workspace_integration.FakeDriver()
        driver.turns = [
            test_workspace_integration.turn(test_simple_team.ToolCall("invalid", "workspace_run", {"command": "python", "timeout": False})),
            test_workspace_integration.turn(test_simple_team.ToolCall("valid", "workspace_run", {"command": "python", "args": ["-c", "print('legacy-corrected')"]})),
            test_workspace_integration.turn(text=test_workspace_integration.final_text()),
        ]
        result = self.rt.workspaces.complete(task_id=self.task, role=self.rt._role_by_id("dev"),
            driver=driver, prompt="Check fixture source", work_context=context)
        self.assertEqual(json.loads(result.text)["claim"], "Observed result")
        first, second = [e.payload["result"] for e in self.events("workspace.tool_finished")]
        self.assertEqual(first["outcome"], "not_started")
        self.assertEqual(first["error"]["code"], "invalid_arguments")
        self.assertEqual(second["status"], "ok")
        self.assertIn("legacy-corrected", second["stdout"]["excerpt"])
        self.assertEqual(self.rt.workspaces.pending(task_id=self.task), [])

    def test_native_bad_unicode_arguments_can_be_corrected_without_rewriting_history(self):
        self.bind_workspace()
        cases = [
            ("workspace_run", {"command": "python", "args": ["\ud800"]}),
            ("workspace_write", {"path": "bad.txt", "content": "\ud800", "expected_sha256": None}),
            ("workspace_read", {"path": "\ud800"}),
        ]
        turns = []
        for index, (name, arguments) in enumerate([*cases,
                ("workspace_run", {"command": "python", "args": ["-c", "print('unicode-corrected')"]})]):
            value = test_simple_team.turn("", (name, arguments))
            identity = f"unicode-{index}"
            value.calls[0] = test_simple_team.ToolCall(identity, name, arguments)
            value.message["tool_calls"][0]["id"] = identity
            turns.append(value)
        self.driver.steps[self.manager] = [*turns, test_simple_team.turn("Observed corrected output")]
        self.rt.team.receive(self.task, "Check encoding fixture", request_id="native-encoding-correct")
        original = WorkspaceExecutor._run
        with patch.object(WorkspaceExecutor, "_run", autospec=True, side_effect=original) as executed:
            self.run_ok()
        results = [e.payload["result"] for e in self.events("team.tool_finished")]
        self.assertEqual(len(results), 4)
        for result in results[:3]:
            self.assertEqual(result["outcome"], "not_started")
            self.assertEqual(result["error"]["code"], "invalid_arguments")
        self.assertEqual(results[-1]["status"], "ok")
        self.assertIn("unicode-corrected", results[-1]["stdout"]["excerpt"])
        executed.assert_called_once()
        self.assertEqual(list(self.project.iterdir()), [])
        self.assertEqual(self.rt.team._pending_operations(self.task), [])
        for event in self.rt.log.read():
            json.dumps(event.payload, ensure_ascii=False).encode("utf-8")
        requests = [row for row in self.driver.calls[-1]["history"] if row["role"] == "assistant"]
        self.assertEqual([row["tool_calls"][0]["arguments"] for row in requests[:3]], [arguments for _, arguments in cases])

    def test_legacy_bad_unicode_arguments_can_be_corrected_in_the_same_claim(self):
        self.bind_workspace()
        self.rt.plan_work(self.task, [{"id": "source", "owner": "dev", "reviewer": "test",
            "objective": "Check actual source", "depends_on": [],
            "criteria": [{"id": "checked", "kind": "review", "text": "Check actual output"}]}])
        claim = self.rt.work.claim(self.task, "source")
        item = self.rt.work.view(self.task)["items"][0]
        context = {**claim.payload, "contract": item, "accepted_inputs": {}, "customer_directions": []}
        cases = [
            ("workspace_run", {"command": "python", "args": ["\ud800"]}),
            ("workspace_write", {"path": "bad.txt", "content": "\ud800", "expected_sha256": None}),
            ("workspace_read", {"path": "\ud800"}),
            ("workspace_run", {"command": "python", "args": ["-c", "print('legacy-unicode-corrected')"]}),
        ]
        driver = test_workspace_integration.FakeDriver()
        driver.turns = [test_workspace_integration.turn(test_simple_team.ToolCall(f"unicode-{index}", name, arguments))
                        for index, (name, arguments) in enumerate(cases)]
        driver.turns.append(test_workspace_integration.turn(text=test_workspace_integration.final_text()))
        original = WorkspaceExecutor._run
        with patch.object(WorkspaceExecutor, "_run", autospec=True, side_effect=original) as executed:
            self.rt.workspaces.complete(task_id=self.task, role=self.rt._role_by_id("dev"),
                driver=driver, prompt="Check encoding fixture", work_context=context)
        results = [e.payload["result"] for e in self.events("workspace.tool_finished")]
        self.assertEqual(len(results), 4)
        for result in results[:3]:
            self.assertEqual(result["outcome"], "not_started")
            self.assertEqual(result["error"]["code"], "invalid_arguments")
        self.assertEqual(results[-1]["status"], "ok")
        self.assertIn("legacy-unicode-corrected", results[-1]["stdout"]["excerpt"])
        executed.assert_called_once()
        self.assertEqual(list(self.project.iterdir()), [])
        self.assertEqual(self.rt.workspaces.pending(task_id=self.task), [])
        for event in self.rt.log.read():
            json.dumps(event.payload, ensure_ascii=False).encode("utf-8")


if __name__ == "__main__":
    unittest.main()
