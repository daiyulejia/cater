"""Web-only effects attention over isolated Runtime and scripted model turns."""

from __future__ import annotations

import copy
import json
import unittest
from unittest.mock import patch

import test_project_file_restore as restore_fixtures
import test_team_web as web_fixtures
import test_task_summaries as summary_fixtures
from cater.events import EventLog
from cater.fold import TaskSnapshot
from cater.mcp import CALL, CONTRACT_KIND
from cater.runtime import Runtime
from cater.team import HOST, TeamService
from cater.web import (_effects_attention_label, _summary_activity, _task_summary,
                       _team_view, task_api_payload)
from cater.workspace_tools import WorkspaceExecutor


class RestoreAttentionTests(unittest.TestCase):
    def setUp(self):
        self.case = restore_fixtures.ProjectFileRestoreTests()
        self.addCleanup(self.case.doCleanups)
        self.case.setUp()
        self.f = self.case.f
        self.delivery, self.artifact = self.case.archive.delivered()
        self.args = self.case.selection(self.delivery, self.artifact)

    def assert_count(self, task, count, *, activity=None):
        rt = self.f.rt
        engine = copy.deepcopy(rt.team.view(task))
        detail = task_api_payload(rt, task, activity=activity)
        projected = _team_view(rt, task)
        row = _summary_activity(_task_summary(rt.task(task), version=detail["summary"]["version"],
            latest_seq=detail["summary"]["latest_seq"], conversation_count=detail["summary"]["conversation_count"],
            execution_control=rt.execution_control.view(task), team=projected),
            activity or {"state": "idle", "active": False})
        self.assertEqual(projected["effects_pending_count"], count)
        self.assertEqual(detail["team"]["effects_pending_count"], count)
        self.assertEqual(detail["summary"]["team"], row["team"])
        self.assertEqual(detail["summary"]["status_label"], row["status_label"])
        self.assertEqual(rt.team.view(task), engine)
        self.assertNotIn("effects_pending_count", engine)
        self.assertLessEqual(len(row["team"]["runs"]), 2)
        self.assertNotIn("arguments", json.dumps(row))
        return detail

    def unknown(self):
        self.case.unknown_after_install(self.args)
        start, finish = self.case.pair()
        self.assertEqual((self.case.target_root / self.args["path"]).read_bytes(),
                         restore_fixtures.archive_fixtures.FILES[self.args["path"]])
        self.assertEqual(finish.payload["result"]["outcome"], "effects_unknown")
        return start

    def test_real_unknown_restart_reconcile_and_source_scope(self):
        start = self.unknown()
        self.assert_count(self.f.target, 1)
        self.assert_count(self.delivery.task_id, 0)
        shared = self.f.task(self.f.project)
        self.case.bind(shared, "target")
        self.assert_count(shared, 0)
        self.assertEqual([row["task_id"] for row in self.f.rt.workspaces.view(shared)["pending"]],
                         [self.f.target])
        self.f.rt.close()
        self.f.rt = Runtime(self.f.database, roles_path=self.f.roles,
            workspaces_path=self.case.archive.config, drivers={"team_test": self.f.driver})
        self.assert_count(self.f.target, 1)
        before = self.case.disk()
        with patch.object(WorkspaceExecutor, "install_bytes", side_effect=AssertionError("must not replay")):
            self.f.rt.workspaces.reconcile(self.f.target, start.seq,
                reason="Stopped fixture invocation and checked installed bytes", invocation_stopped=True)
        self.assertEqual(self.case.disk(), before)
        self.assert_count(self.f.target, 0)
        self.assertEqual(self.f.rt.team._pending_operations(self.f.target), [])
        self.assertTrue(self.f.rt.verify_chain().ok)

    def test_real_not_started_and_success_do_not_raise_attention(self):
        self.case.not_started(self.case.restore({**self.args, "path": "missing.txt"}))
        self.assert_count(self.f.target, 0)
        self.assertEqual(self.case.restore(self.args)["outcome"], "written")
        self.assert_count(self.f.target, 0)

    def test_unknown_survives_changed_objective_and_completed_projection(self):
        self.unknown()
        self.f.update(self.f.target, "New objective keeps unresolved external effects")
        self.assert_count(self.f.target, 1)
        # An old completion marker must not conceal later unknown effects.
        view = _team_view(self.f.rt, self.f.target)
        row = _task_summary(self.f.rt.task(self.f.target), version=1, latest_seq=1,
                            conversation_count=0, team={**view, "status": "done"})
        self.assertEqual(row["status_label"], "有操作影响待核对")

    def test_live_unsettled_is_zero_finished_unknown_before_run_end_is_one(self):
        original = WorkspaceExecutor.install_bytes
        seen = []

        def inspect_live(executor, *args, **kwargs):
            pending, = self.f.rt.team._pending_operations(self.f.target)
            self.assertEqual(pending["outcome"], "unsettled")
            self.assertNotIn("finished_seq", pending)
            seen.append(self.assert_count(self.f.target, 0))
            original(executor, *args, **kwargs)
            raise OSError("PRIVATE_EXCEPTION after fixture install")

        emit = self.f.rt.log.emit

        def inspect_finished(**kwargs):
            event = emit(**kwargs)
            if event.type == "team.tool_finished" and event.payload["name"] == restore_fixtures.TOOL:
                pending, = self.f.rt.team._pending_operations(self.f.target)
                self.assertFalse(pending["run_ended"])
                self.assertIn("finished_seq", pending)
                seen.append(self.assert_count(self.f.target, 1))
            return event

        with patch.object(WorkspaceExecutor, "install_bytes", new=inspect_live), \
                patch.object(self.f.rt.log, "emit", side_effect=inspect_finished):
            result = self.case.restore(self.args)
        self.assertEqual(result["outcome"], "effects_unknown")
        self.assertEqual(len(seen), 2)

    def test_real_missing_finish_ended_run_counts_unknown(self):
        emit = self.f.rt.log.emit

        def lose_finish(**kwargs):
            if kwargs.get("type") == "team.tool_finished" and kwargs["payload"]["name"] == restore_fixtures.TOOL:
                raise OSError("PRIVATE_EXCEPTION lost fixture receipt")
            return emit(**kwargs)

        turn = restore_fixtures.archive_fixtures.fixtures.turn
        with patch.object(self.f.rt.log, "emit", side_effect=lose_finish):
            run = self.case.native_run(self.f.target, [turn("", (restore_fixtures.TOOL, self.args))])
        self.assertEqual(run.payload["status"], "failed")
        pending, = self.f.rt.team._pending_operations(self.f.target)
        self.assertEqual(pending["outcome"], "unknown")
        self.assertNotIn("finished_seq", pending)
        self.assert_count(self.f.target, 1)

    def test_readonly_mcp_and_local_operations_are_excluded_and_seqs_deduplicated(self):
        read = {"started_seq": 1, "name": CALL, "outcome": "unknown",
            "arguments": {"server_id": "fixture"}, "execution_contract": {
                "kind": CONTRACT_KIND, "version": 1, "effect": "read", "server_id": "fixture",
                "configuration_sha256": "a" * 64}}
        write = {**read, "started_seq": 2, "execution_contract": {**read["execution_contract"], "effect": "write"}}
        forged = {**read, "started_seq": 3, "execution_contract": {**read["execution_contract"], "version": True}}
        local = {"started_seq": 4, "name": "delegate", "outcome": "unknown"}
        live = {"started_seq": 5, "name": "workspace_write", "outcome": "unsettled"}
        finished = {**live, "started_seq": 6, "finished_seq": 100}
        with patch.object(self.f.rt.team, "_pending_operations", return_value=[read, local, live]):
            self.assert_count(self.f.target, 0)
        with patch.object(self.f.rt.team, "_pending_operations", return_value=[read, write, write, forged, local, live, finished]):
            self.assert_count(self.f.target, 3)


class AttentionProjectionTests(unittest.TestCase):
    def test_labels_preserve_activity_control_and_invalid_values(self):
        for count in (None, True, False, -1, 1.5, "1", {}, [], 2 ** 53):
            with self.subTest(count=count):
                self.assertEqual(_effects_attention_label({"effects_pending_count": count}), "操作状态待核对")
        self.assertIsNone(_effects_attention_label({}))
        self.assertIsNone(_effects_attention_label({"effects_pending_count": 0}))
        for state, label in (("idle", "有操作影响待核对"), ("queued", "消息已排队，有操作待核对"),
                             ("running", "正在处理，另有操作待核对")):
            team = {"enabled": True, "objective": "Fixture", "status": "done", "effects_pending_count": 1}
            activity = {"state": state, "active": state != "idle", "revision": 1}
            row = _task_summary(TaskSnapshot("fixture"), version=1, latest_seq=1, conversation_count=0, team=team)
            self.assertEqual(_summary_activity(row, activity)["status_label"], label)
            for control, expected in (("paused", "已暂停"), ("cancelled", "已取消")):
                controlled = _task_summary(TaskSnapshot("fixture"), version=1, latest_seq=1,
                    conversation_count=0, team=team, execution_control={"state": control})
                self.assertEqual(_summary_activity(controlled, activity)["status_label"], expected)
                self.assertEqual(controlled["team"]["effects_pending_count"], 1)

    def test_summary_scalar_stays_bounded_without_pending_details_or_arguments(self):
        full = {"enabled": True, "objective": "Fixture", "status": "running", "revision": 1,
            "effects_pending_count": 300, "pending_operations": [{"arguments": "PRIVATE_ARGUMENT"}] * 300,
            "runs": [{"revision": 1, "status": "running" if i == 0 else "completed", "role": "dev",
                      "message_seq": i, "arguments": "PRIVATE_ARGUMENT"} for i in range(300)]}
        before = copy.deepcopy(full)
        row = _task_summary(TaskSnapshot("fixture"), version=1, latest_seq=1, conversation_count=0, team=full)
        self.assertEqual(row["team"]["effects_pending_count"], 300)
        self.assertEqual(len(row["team"]["runs"]), 2)
        self.assertNotIn("pending_operations", row["team"])
        self.assertNotIn("PRIVATE_ARGUMENT", json.dumps(row))
        self.assertLess(len(json.dumps(row["team"])), 500)
        self.assertEqual(full, before)


class AttentionHttpTests(unittest.TestCase):
    setUp = web_fixtures.TeamWebTests.setUp
    factory = web_fixtures.TeamWebTests.factory
    request = web_fixtures.TeamWebTests.request
    create = web_fixtures.TeamWebTests.create
    run_current = web_fixtures.TeamWebTests.run_current
    get = summary_fixtures.TaskSummaryTests.get

    def test_failed_diagnostic_is_not_cached_or_returned_as_not_modified(self):
        task = self.create()["task"]["id"]
        self.run_current(task)
        with patch.object(TeamService, "_pending_operations", side_effect=RuntimeError("PRIVATE_EXCEPTION")), \
                self.assertLogs("cater.web", level="WARNING"):
            for tag in (None, "*"):
                status, headers, raw = self.get(tag)
                self.assertEqual(status, 200)
                self.assertNotIn("ETag", headers)
                self.assertEqual(headers["Cache-Control"], "no-store")
                row = next(r for r in json.loads(raw)["tasks"] if r["id"] == task)
                self.assertIsNone(row["team"]["effects_pending_count"])
        status, headers, raw = self.get("*")
        self.assertEqual(status, 200)
        row = next(r for r in json.loads(raw)["tasks"] if r["id"] == task)
        self.assertEqual(row["team"]["effects_pending_count"], 0)
        tag = headers["ETag"]
        self.assertTrue(tag)
        with patch.object(TeamService, "_pending_operations", side_effect=AssertionError("stable cache scan")):
            self.assertEqual(self.get(tag)[0], 304)

    def test_projection_failure_keeps_http_detail_and_summary_without_private_error(self):
        task = self.create()["task"]["id"]
        self.run_current(task)
        for bad in (RuntimeError("PRIVATE_EXCEPTION private/path"), [{"name": "workspace_write"}]):
            kwargs = {"side_effect": bad} if isinstance(bad, Exception) else {"return_value": bad}
            # New handler for each case so an earlier cached null cannot mask the projection.
            with patch("cater.web.ActivityRunner", return_value=self.runner):
                from cater.web import make_handler
                self.handler = make_handler(self.db, runtime_factory=self.factory)
            with patch.object(TeamService, "_pending_operations", **kwargs), self.assertLogs("cater.web", level="WARNING") as logs:
                detail = self.request(f"/api/tasks/{task}")
                row = next(r for r in self.request("/api/task-summaries")["tasks"] if r["id"] == task)
            self.assertIsNone(detail["team"]["effects_pending_count"])
            self.assertIsNone(row["team"]["effects_pending_count"])
            self.assertEqual(row["status_label"], "操作状态待核对")
            self.assertEqual(detail["summary"]["status_label"], row["status_label"])
            self.assertTrue(logs.records)
            for record in logs.records:
                self.assertEqual(record.getMessage(), "Cannot project unresolved operation state.")
                self.assertIsNone(record.exc_info)
            for private in ("PRIVATE_EXCEPTION", "private/path", "Traceback"):
                self.assertNotIn(private, json.dumps([detail, row, logs.output]))

    def test_stable_http_cache_preserves_attention_without_reading_history(self):
        task = self.create()["task"]["id"]
        self.run_current(task)
        with self.factory() as rt:
            role = rt.organization.leader
            message = rt.team.receive(task, "Fixture unfinished external operation")
            run = {"run_id": "attention-cache", "role": role.id, "message_seq": message.seq,
                   "revision": rt.team.view(task)["revision"]}
            rt.log.emit(type="team.run_started", actor=HOST, task_id=task, payload=run)
            rt.log.emit(type="team.tool_started", actor=HOST, task_id=task,
                payload={**run, "name": "workspace_write", "call_id": "fixture", "arguments": {"path": "PRIVATE_ARGUMENT"}})
            rt.log.emit(type="team.run_finished", actor=HOST, task_id=task, payload={**run, "status": "interrupted"})
        detail = self.request(f"/api/tasks/{task}")
        first = self.request("/api/task-summaries")
        row = next(r for r in first["tasks"] if r["id"] == task)
        self.assertEqual(row["team"]["effects_pending_count"], 1)
        self.assertEqual(row["team"], detail["summary"]["team"])
        self.assertEqual(row["status_label"], detail["summary"]["status_label"])
        self.assertNotIn("PRIVATE_ARGUMENT", json.dumps(first))
        with patch.object(EventLog, "read", side_effect=AssertionError("history read")), \
                patch.object(EventLog, "iter_events", side_effect=AssertionError("history iteration")), \
                patch.object(EventLog, "_row_to_event", side_effect=AssertionError("history decoding")), \
                patch.object(TeamService, "view", side_effect=AssertionError("team fold")), \
                patch.object(TeamService, "_pending_operations", side_effect=AssertionError("pending scan")):
            self.assertEqual(self.request("/api/task-summaries"), first)
            self.assertEqual(self.request("/api/task-summaries"), first)


if __name__ == "__main__":
    unittest.main()
