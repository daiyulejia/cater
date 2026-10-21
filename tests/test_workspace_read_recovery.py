"""Unanswered built-in reads are evidence gaps, not unknown workspace mutations."""

import json
from pathlib import Path
import queue
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

import cater
from cater.activity_owner import activity_owner
from cater.events import Actor
from cater.runtime import Runtime
from cater.team import HOST
from cater.tool_protocol import ToolCall
from cater.workspace_tools import WorkspaceExecutor
from cater.workspaces import WorkspaceService
import test_simple_team as fixture
import test_workspace_integration as workspace_fixture
import test_project_file_restore as restore_fixture


READS = ("workspace_read", "workspace_list", "workspace_search")


class WorkspaceReadRecoveryTests(unittest.TestCase):
    events = fixture.SimpleTeamTests.events

    def setUp(self):
        fixture.SimpleTeamTests.setUp(self)
        self.project = self.root / "project"
        self.project.mkdir()
        (self.project / "source.txt").write_text("Existing source\n", encoding="utf-8", newline="\n")
        self.config = self.root / "workspaces.json"
        self.config.write_text(json.dumps({"workspaces": [{
            "id": "project", "title": "Project", "root": str(self.project), "mode": "edit",
        }]}), encoding="utf-8")
        self.rt.workspaces = WorkspaceService(self.rt, self.config)
        self.binding = self.rt.workspaces.bind(self.task, "project", expected_revision=None)

    def orphan(self, name, *, task=None, family="team"):
        task = task or self.task
        if family == "workspace":
            return self.rt.log.emit(type="workspace.tool_started", task_id=task,
                actor=Actor.system("workspace_gateway"), payload={
                    "name": name, "call_id": "old-read", "root": self.binding["root"]})
        message = self.rt.team.receive(task, "Inspect existing source", request_id=f"orphan-{name}-{task}")
        _, _, start = self.rt.team._claim(task)
        operation = self.rt.log.emit(type="team.tool_started", task_id=task, actor=HOST, payload={
            "name": name, "call_id": "old-read", "root": self.binding["root"],
            "message_seq": message.seq, "role": self.manager, "run_id": start.payload["run_id"]})
        self.rt.log.emit(type="team.run_finished", task_id=task, actor=HOST, payload={
            **start.payload, "status": "interrupted", "reason": "Offline process stopped during a read"})
        return operation

    def finish_after_inspection(self):
        self.driver.steps[self.manager] = [fixture.turn("", ("workspace_read", {"path": "source.txt"}),
            ("workspace_write", {"path": "result.txt", "content": "Verified new work\n", "expected_sha256": None}),
            ("publish_workspace_archive", {"filename": "result.zip", "paths": ["source.txt", "result.txt"], "summary": "Actual files"}),
            ("finish_work", {"summary": "New work saved", "verification": "Read actual source; no model API used"})),
            fixture.turn("Saved and delivered after actual inspection.")]
        self.rt.team.receive(self.task, "Inspect current files and finish new work", request_id="fresh-after-read")
        self.rt.collaborate(self.task)
        return {e.payload["name"]: e.payload["result"] for e in self.events("team.tool_finished")}

    def test_orphan_read_does_not_block_new_write_snapshot_and_delivery(self):
        starts = [self.orphan(name) for name in READS]
        results = self.finish_after_inspection()
        self.assertEqual(results["workspace_read"]["status"], "ok")
        self.assertEqual(results["workspace_write"]["status"], "ok", results)
        self.assertEqual(results["publish_workspace_archive"]["status"], "ok", results)
        self.assertEqual(results["finish_work"]["status"], "ok", results)
        self.assertEqual(self.rt.team.view(self.task)["status"], "done")
        self.assertEqual(self.rt.workspaces.pending(task_id=self.task), starts)
        self.assertEqual(self.rt.workspaces.view(self.task)["pending"], [])
        self.assertTrue(self.rt.verify_chain().ok)

    def test_effect_filter_keeps_unknown_tools_and_mutations_for_both_host_families(self):
        for family in ("team", "workspace"):
            for name in (*READS, "workspace_write", "workspace_run", "workspace_restore_project_file", "workspace_future"):
                with self.subTest(family=family, name=name):
                    task = self.rt.create_task("Independent pending entry")
                    self.rt.team.enable(task)
                    start = self.orphan(name, task=task, family=family)
                    self.assertEqual(self.rt.workspaces.pending(task_id=task), [start])
                    self.assertEqual(self.rt.workspaces.pending(task_id=task, effects_only=True),
                                     [] if name in READS else [start])

    def test_foreign_read_is_not_a_root_wide_write_lock(self):
        foreign = self.rt.create_task("Other group")
        self.rt.team.enable(foreign)
        self.rt.workspaces.bind(foreign, "project", expected_revision=None)
        self.orphan("workspace_read", task=foreign)
        self.assertEqual(self.finish_after_inspection()["workspace_write"]["status"], "ok")
        self.assertEqual(self.rt.workspaces.view(self.task)["pending"], [])

    def test_foreign_mutation_still_blocks_shared_root(self):
        foreign = self.rt.create_task("Other mutating group")
        self.rt.team.enable(foreign)
        self.rt.workspaces.bind(foreign, "project", expected_revision=None)
        start = self.orphan("workspace_write", task=foreign)
        results = self.finish_after_inspection()
        self.assertEqual(results["workspace_write"]["status"], "error")
        self.assertEqual(results["publish_workspace_archive"]["status"], "error")
        self.assertEqual([row["seq"] for row in self.rt.workspaces.view(self.task)["pending"]], [start.seq])
        self.assertFalse((self.project / "result.txt").exists())

    def test_read_record_retained_without_blocking_explicit_binding_change(self):
        start = self.orphan("workspace_read")
        before = self.rt.log.read(task_id=self.task)
        self.rt.workspaces.bind(self.task, None, expected_revision=self.binding["revision"])
        self.assertFalse(self.rt.workspaces.view(self.task)["available"])
        self.assertEqual(self.rt.workspaces.pending(task_id=self.task), [start])
        self.assertEqual(self.rt.log.read(task_id=self.task)[:len(before)], before)

    def test_effect_filter_is_explicit_and_reading_does_not_settle_evidence(self):
        start = self.orphan("workspace_read")
        before = self.rt.log.read(task_id=self.task)
        for invalid in (None, 0, 1, "true", [], {}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.rt.workspaces.pending(task_id=self.task, effects_only=invalid)
        self.assertEqual(self.rt.workspaces.pending(task_id=self.task), [start])
        self.assertEqual(self.rt.workspaces.pending(task_id=self.task, effects_only=False), [start])
        self.assertEqual(self.rt.workspaces.pending(task_id=self.task, effects_only=True), [])
        self.assertEqual(self.rt.log.read(task_id=self.task), before)

    def test_live_owner_still_prevents_new_write(self):
        self.orphan("workspace_read")
        with self.rt.workspaces._owner(self.binding["root"]) as owned:
            self.assertTrue(owned)
            results = self.finish_after_inspection()
        self.assertEqual(results["workspace_write"]["status"], "error")
        self.assertFalse((self.project / "result.txt").exists())

    def test_mutation_still_blocks_new_write_snapshot_and_delivery(self):
        start = self.orphan("workspace_write")
        results = self.finish_after_inspection()
        self.assertEqual(results["workspace_read"]["status"], "ok")
        self.assertEqual(results["workspace_write"]["status"], "error")
        self.assertEqual(results["publish_workspace_archive"]["status"], "error")
        self.assertEqual(results["finish_work"]["code"], "operation_pending")
        self.assertEqual(self.rt.workspaces.pending(task_id=self.task, effects_only=True), [start])
        self.assertFalse((self.project / "result.txt").exists())

    def test_real_process_kill_during_read_retains_evidence_without_locking_new_work(self):
        code = r'''
import json, sys, threading
from pathlib import Path
sys.path[:0] = [sys.argv[1], sys.argv[2]]
from cater.runtime import Runtime
import test_simple_team as fixture
driver = fixture.TeamDriver()
with Runtime(sys.argv[3], roles_path=sys.argv[4], workspaces_path=sys.argv[5],
             drivers={"team_test": driver}) as rt:
    task = sys.argv[6]
    driver.steps[rt.organization.leader.id] = [fixture.turn("", ("workspace_read", {"path": "source.txt"}))]
    rt.team.receive(task, "Read existing source", request_id="real-held-read")
    emit = rt.log.emit
    def hold(**kwargs):
        if kwargs["type"] == "team.tool_finished" and kwargs["payload"]["name"] == "workspace_read":
            assert kwargs["payload"]["result"]["status"] == "ok"
            assert kwargs["payload"]["result"]["content"] == "Existing source\n"
            print("actual-read-before-receipt", flush=True)
            threading.Event().wait()
        return emit(**kwargs)
    rt.log.emit = hold
    rt.collaborate(task)
'''
        process = subprocess.Popen([sys.executable, "-I", "-X", "utf8", "-B", "-c", code,
            str(Path(cater.__file__).resolve().parent.parent), str(Path(__file__).resolve().parent),
            self.rt.log.path, str(self.roles), str(self.config), self.task],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8")
        ready = queue.Queue()
        reader = threading.Thread(target=lambda: ready.put(process.stdout.readline()), daemon=True)
        reader.start()
        try:
            self.assertEqual(ready.get(timeout=15).strip(), "actual-read-before-receipt")
            self.assertIsNone(process.poll())
            with activity_owner(self.rt.log.path, self.task, "simple-team") as owned:
                self.assertFalse(owned)
            process.kill()
            _, error = process.communicate(timeout=10)
            self.assertNotEqual(process.returncode, 0, error)
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=10)
            reader.join(timeout=5)
        self.assertFalse(reader.is_alive())
        start, = self.events("team.tool_started")
        self.assertEqual(self.events("team.tool_finished"), [])
        before = self.project.joinpath("source.txt").read_bytes()
        with patch.object(WorkspaceExecutor, "_read", wraps=WorkspaceExecutor._read) as unused:
            # Preparing recovery closes only host runs, never synthesizes the lost read result.
            self.rt.team.recovery.prepare(self.task)
            unused.assert_not_called()
        self.assertEqual(self.rt.workspaces.pending(task_id=self.task), [start])
        with Runtime(self.rt.log.path, roles_path=self.roles, workspaces_path=self.config,
                     drivers={"team_test": self.driver}, close_drivers_on_close=False) as reopened:
            self.assertEqual(reopened.workspaces.pending(task_id=self.task), [start])
        self.assertEqual(self.finish_after_inspection()["finish_work"]["status"], "ok")
        self.assertEqual(self.project.joinpath("source.txt").read_bytes(), before)
        self.assertEqual(sum(e.payload["name"] == "workspace_read" for e in self.events("team.tool_started")), 2)
        self.assertFalse(any(e.type == "workspace.tool_reconciled" for e in self.rt.log.read(task_id=self.task)))
        self.assertTrue(self.rt.verify_chain().ok)


class OtherWorkspaceReadRecoveryTests(unittest.TestCase):
    def test_work_item_gateway_can_read_and_write_after_an_unanswered_read(self):
        case = workspace_fixture.WorkspaceIntegrationTests()
        self.addCleanup(case.doCleanups)
        case.setUp()
        _, context = case.prepare_call()
        (case.root / "a.txt").write_bytes(b"Original source\n")
        started = case.rt.log.emit(type="workspace.tool_started", task_id=case.task,
            actor=Actor.system("workspace_gateway"), payload={
                "name": "workspace_read", "call_id": "lost-read", "root": str(case.root),
                **{k: context[k] for k in ("attempt_id", "work_item_id", "plan_seq", "scope")}})
        case.driver.turns = [workspace_fixture.turn(
            ToolCall("fresh-read", "workspace_read", {"path": "a.txt"}),
            ToolCall("new-write", "workspace_write", {
                "path": "new.txt", "content": "Actual new file\n", "expected_sha256": None})),
            workspace_fixture.turn(text=workspace_fixture.final_text(workspace_files=["new.txt"]))]
        with patch("cater.workspace_tools.WorkspaceExecutor", new=WorkspaceExecutor):
            result = case.service(context)
        self.assertEqual((case.root / "new.txt").read_bytes(), b"Actual new file\n")
        self.assertEqual(json.loads(result.text)["deliverables"][-1]["content"], "Actual new file\n")
        self.assertEqual(case.rt.workspaces.pending(task_id=case.task), [started])
        self.assertEqual(case.rt.workspaces.pending(task_id=case.task, effects_only=True), [])
        self.assertTrue(case.rt.verify_chain().ok)

    def test_exact_historical_restore_is_not_blocked_by_an_unanswered_read(self):
        case = restore_fixture.ProjectFileRestoreTests()
        self.addCleanup(case.doCleanups)
        case.setUp()
        delivery, artifact = case.archive.delivered()
        start = case.f.rt.log.emit(type="team.tool_started", task_id=case.f.target, actor=HOST,
            payload={"name": "workspace_read", "root": str(case.target_root), "call_id": "lost-read",
                     "run_id": "old-run", "role": "dev"})
        args = case.selection(delivery, artifact)
        result = case.restore(args)
        self.assertEqual(result["outcome"], "written", result)
        self.assertEqual((case.target_root / args["path"]).read_bytes(),
                         restore_fixture.archive_fixtures.FILES[args["path"]])
        self.assertEqual(case.f.rt.workspaces.pending(task_id=case.f.target), [start])
        self.assertTrue(case.f.rt.verify_chain().ok)


if __name__ == "__main__":
    unittest.main()
