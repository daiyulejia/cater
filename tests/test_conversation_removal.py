"""List removal contracts using temporary storage and in-memory HTTP only."""

from __future__ import annotations

import io
import json
import sqlite3
import threading
import unittest
from email.message import Message
from http.client import HTTPResponse
from unittest.mock import Mock, patch
from urllib.parse import urlencode

import test_team_web as team_web
from cater.artifacts import normalize_deliverables
from cater.events import Actor, EventLog
from cater.runtime import Runtime
from cater.tool_protocol import ToolCall, ToolTurn
from cater.web import make_handler
from cater.work import WorkConflict


class ConversationRemovalTests(unittest.TestCase):
    def test_cached_removal_projection_does_not_rescan_history(self):
        task = self.create()
        self.remove(task, "remove-cache-contract")
        first = self.web.request("/api/task-summaries")
        self.assertEqual(first["removed"][0]["id"], task)
        with patch.object(EventLog, "iter_events", side_effect=AssertionError("history iteration")), \
                patch.object(EventLog, "read", side_effect=AssertionError("history read")), \
                patch.object(Runtime, "tasks", side_effect=AssertionError("task refold")):
            self.assertEqual(self.web.request("/api/task-summaries"), first)

    def setUp(self):
        self.web = team_web.TeamWebTests(methodName="runTest")
        self.addCleanup(self.web.doCleanups)
        self.web.setUp()

    def create(self, **changes):
        return self.web.create(run=False, **changes)["task"]["id"]

    def remove(self, task, request_id="remove-request-001", *, expected=200):
        return self.web.request(f"/api/tasks/{task}/remove", {"request_id": request_id}, expected=expected)

    def request_with_headers(self, path, data, *, expected, headers=None, handler_class=None, form=False):
        raw = (urlencode(data) if form else json.dumps(data)).encode("utf-8")
        handler_class = handler_class or self.web.handler
        handler = handler_class.__new__(handler_class)
        handler.path, handler.command = path, "POST"
        handler.request_version = "HTTP/1.1"
        handler.requestline = f"POST {path} HTTP/1.1"
        handler.headers = Message()
        content_type = "application/x-www-form-urlencoded" if form else "application/json"
        for name, value in {"Host": "localhost", "Content-Type": content_type,
                            "Content-Length": str(len(raw)), **(headers or {})}.items():
            handler.headers[name] = value
        handler.rfile, handler.wfile = io.BytesIO(raw), io.BytesIO()
        handler.connection = Mock()
        handler.connection.gettimeout.return_value = None
        handler.do_POST()
        response = HTTPResponse(team_web.MemorySocket(handler.wfile.getvalue()))
        response.begin()
        body = response.read()
        self.assertEqual(response.status, expected, body)
        return json.loads(body) if response.headers.get_content_type() == "application/json" else body

    def test_remove_appends_contiguous_human_cancel_and_removal_in_one_transaction(self):
        task = self.create()
        with self.web.factory() as rt:
            before = rt.log.read()
            scope = rt.work.scope(task)
            statements = []
            rt.log.db.set_trace_callback(statements.append)
            try:
                removed = rt.conversation_list.remove(task, request_id="remove-atomic-success")
            finally:
                rt.log.db.set_trace_callback(None)
            events = rt.log.read()
            self.assertEqual(events[:len(before)], before)
            self.assertEqual(len(events), len(before) + 2)
            cancel, removal = events[-2:]
            self.assertEqual(removal, removed)
            self.assertEqual([cancel.type, removal.type],
                             ["task.execution_controlled", "task.removed_from_list"])
            self.assertEqual([cancel.task_id, removal.task_id], [task, task])
            self.assertEqual([cancel.actor, removal.actor], [Actor.human(), Actor.human()])
            self.assertEqual(cancel.payload["action"], "cancel")
            self.assertEqual(cancel.payload["state"], "cancelled")
            self.assertEqual(cancel.payload["request_id"], "remove-atomic-success")
            self.assertEqual(removal.payload["request_id"], "remove-atomic-success")
            self.assertEqual(removal.seq, cancel.seq + 1)
            self.assertEqual(removal.prev_hash, cancel.hash)
            self.assertEqual(rt.conversation_list.removed(), {task: removal.seq})
            self.assertEqual(rt.work.scope(task), scope)
            self.assertEqual(statements.count("BEGIN IMMEDIATE"), 1)
            self.assertEqual(statements.count("COMMIT"), 1)
            self.assertTrue(rt.verify_chain().ok)
        self.web.start.assert_not_called()
        self.assertEqual(self.web.driver.calls, [])

    def test_removal_survives_restart_with_internal_task_and_replay_intact(self):
        task = self.create(workspace_id="source")
        other = self.create(request_id="other-create-001")
        with self.web.factory() as rt:
            before = rt.log.read(task_id=task)
            original = rt.task(task)
            removal = rt.conversation_list.remove(task, request_id="remove-restart-001")
        with self.web.factory() as reopened:
            self.assertEqual(reopened.conversation_list.removed(), {task: removal.seq})
            self.assertEqual(reopened.task(task), original)
            self.assertEqual({row.id for row in reopened.tasks()}, {task, other})
            self.assertEqual(reopened.log.read(task_id=task)[:len(before)], before)
            self.assertEqual(reopened.log.read(task_id=task)[-1], removal)
            self.assertEqual(reopened.execution_control.view(task)["state"], "cancelled")
            self.assertTrue(reopened.verify_chain().ok)

    def test_same_request_is_noop_and_new_request_appends_only_removal_confirmation(self):
        task = self.create()
        with self.web.factory() as rt:
            first = rt.conversation_list.remove(task, request_id="remove-replay-001")
            before = rt.log.read()
            control = rt.execution_control.view(task)
            self.assertEqual(rt.conversation_list.remove(task, request_id="remove-replay-001"), first)
            self.assertEqual(rt.log.read(), before)
            confirmation = rt.conversation_list.remove(task, request_id="remove-replay-002")
            self.assertEqual(confirmation.type, "task.removed_from_list")
            self.assertEqual(confirmation.task_id, task)
            self.assertEqual(confirmation.actor, Actor.human())
            self.assertEqual(confirmation.payload["request_id"], "remove-replay-002")
            self.assertEqual(confirmation.payload["previous_removal_seq"], first.seq)
            after = rt.log.read()
            self.assertEqual(after, [*before, confirmation])
            self.assertEqual(rt.conversation_list.removed(), {task: confirmation.seq})
            self.assertEqual(rt.execution_control.view(task), control)
            cancels = [event for event in after if event.type == "task.execution_controlled"
                       and event.task_id == task and event.payload.get("action") == "cancel"]
            self.assertEqual(len(cancels), 1)
            self.assertEqual(rt.conversation_list.remove(task, request_id="remove-replay-002"), confirmation)
            self.assertEqual(rt.log.read(), after)
        with self.web.factory() as reopened:
            self.assertEqual(reopened.conversation_list.remove(task, request_id="remove-replay-001"), first)
            self.assertEqual(reopened.conversation_list.remove(task, request_id="remove-replay-002"), confirmation)
            self.assertEqual(reopened.log.read(), after)
            self.assertEqual(reopened.conversation_list.removed(), {task: confirmation.seq})
            self.assertEqual(reopened.execution_control.view(task), control)
            self.assertTrue(reopened.verify_chain().ok)

    def test_removal_request_cannot_be_reused_for_another_task(self):
        task = self.create()
        other = self.create(request_id="other-create-001")
        with self.web.factory() as rt:
            rt.conversation_list.remove(task, request_id="remove-conflict-001")
            before = rt.log.read()
            with self.assertRaises(WorkConflict):
                rt.conversation_list.remove(other, request_id="remove-conflict-001")
            self.assertEqual(rt.log.read(), before)
            self.assertNotIn(other, rt.conversation_list.removed())
            self.assertEqual(rt.execution_control.view(other)["state"], "running")

    def test_control_request_collision_is_rejected_without_hiding_task(self):
        task = self.create()
        other = self.create(request_id="other-create-001")
        with self.web.factory() as rt:
            for index, (target, action) in enumerate(((task, "pause"), (task, "cancel"), (other, "cancel"))):
                with self.subTest(target=target, action=action):
                    request_id = f"control-collision-{index}"
                    rt.execution_control.change(target, action, request_id=request_id)
                    before = rt.log.read()
                    with self.assertRaises(WorkConflict):
                        rt.conversation_list.remove(task, request_id=request_id)
                    self.assertEqual(rt.log.read(), before)
                    self.assertEqual(rt.conversation_list.removed(), {})

    def test_already_cancelled_task_can_be_removed(self):
        task = self.create()
        with self.web.factory() as rt:
            cancel, _ = rt.execution_control.change(task, "cancel", request_id="cancel-before-removal")
            removal = rt.conversation_list.remove(task, request_id="remove-after-cancel")
            self.assertEqual(rt.conversation_list.removed(), {task: removal.seq})
            self.assertEqual(rt.execution_control.view(task)["state"], "cancelled")
            self.assertIn(cancel, rt.log.read(task_id=task))
            self.assertTrue(rt.verify_chain().ok)

    def test_agent_and_system_removal_events_do_not_hide_or_claim_request_ids(self):
        task = self.create()
        with self.web.factory() as rt:
            for actor in (Actor.agent("dev", "worker"), Actor.system("fixture")):
                rt.log.emit(type="task.removed_from_list", task_id=task, actor=actor,
                            payload={"request_id": "remove-forged-001"})
            self.assertEqual(rt.conversation_list.removed(), {})
        self.assertIn(task, {row["id"] for row in self.web.request("/api/tasks")})
        summary = self.web.request("/api/task-summaries")
        self.assertIn(task, {row["id"] for row in summary["tasks"]})
        self.assertNotIn(task, {row["id"] for row in summary.get("removed", [])})
        with self.web.factory() as rt:
            removed = rt.conversation_list.remove(task, request_id="remove-forged-001")
            self.assertEqual(removed.actor, Actor.human())
            self.assertEqual(rt.conversation_list.removed(), {task: removed.seq})

    def test_summaries_exposes_empty_removal_list_before_any_human_removal(self):
        self.create()
        summary = self.web.request("/api/task-summaries")
        self.assertIn("removed", summary, "The planned summaries contract includes removed even when empty")
        self.assertEqual(summary["removed"], [])

    def test_original_records_workspace_and_artifact_files_are_retained(self):
        task = self.create(workspace_id="source")
        project_file = self.web.root / "project" / "original.txt"
        project_file.write_bytes(b"original workspace data\n")
        artifact_file = self.web.root / "artifacts" / task / "result.txt"
        artifact_file.parent.mkdir(parents=True)
        artifact_file.write_bytes(b"original artifact data\n")
        with self.web.factory() as rt:
            artifact, = normalize_deliverables([{
                "filename": "result.txt", "title": "Result", "summary": "Preserved output",
                "format": "text", "content": "original artifact data\n",
            }])
            rt.log.emit(type="deliverable.published", task_id=task, actor=rt._leader_actor(),
                        payload={**artifact, "source": "team", "phase": "team", "local_path": str(artifact_file)})
            original = rt.log.read()
            deliverables = rt.task(task).deliverables
            binding = rt.workspaces.view(task)
        self.remove(task)
        self.assertTrue(self.web.db.is_file())
        self.assertEqual(project_file.read_bytes(), b"original workspace data\n")
        self.assertEqual(artifact_file.read_bytes(), b"original artifact data\n")
        with self.web.factory() as reopened:
            self.assertEqual(reopened.log.read()[:len(original)], original)
            self.assertEqual(reopened.task(task).deliverables, deliverables)
            self.assertEqual(reopened.workspaces.view(task), binding)
            self.assertTrue(reopened.verify_chain().ok)

    def test_failure_writing_either_event_rolls_back_cancel_and_removal_and_allows_retry(self):
        task = self.create()
        with self.web.factory() as rt:
            before = rt.log.read()
            head = rt.log.head_hash()
            control = rt.execution_control.view(task)
            for event_type in ("task.execution_controlled", "task.removed_from_list"):
                with self.subTest(event_type=event_type):
                    rt.log.db.execute(f"""CREATE TEMP TRIGGER fail_removal_insert
                        BEFORE INSERT ON events WHEN NEW.type = '{event_type}'
                        BEGIN SELECT RAISE(ABORT, 'injected removal write failure'); END""")
                    try:
                        with self.assertRaisesRegex(sqlite3.IntegrityError, "injected removal write failure"):
                            rt.conversation_list.remove(task, request_id="remove-rollback-001")
                    finally:
                        rt.log.db.execute("DROP TRIGGER fail_removal_insert")
                    self.assertFalse(rt.log.db.in_transaction)
                    self.assertEqual(rt.log.read(), before)
                    self.assertEqual(rt.log.head_hash(), head)
                    self.assertEqual(rt.execution_control.view(task), control)
                    self.assertEqual(rt.conversation_list.removed(), {})
                    with self.web.factory() as reopened:
                        self.assertEqual(reopened.log.read(), before)
                        self.assertEqual(reopened.conversation_list.removed(), {})
                        self.assertTrue(reopened.verify_chain().ok)
            removed = rt.conversation_list.remove(task, request_id="remove-rollback-001")
            self.assertEqual(rt.conversation_list.removed(), {task: removed.seq})

    def test_unknown_runtime_task_and_invalid_request_ids_never_write(self):
        task = self.create()
        with self.web.factory() as rt:
            before = rt.log.read()
            with self.assertRaises(KeyError):
                rt.conversation_list.remove("missing-task", request_id="remove-missing-001")
            for request_id in (None, "", "short", "x" * 129, "invalid request", 123, [], {}, "invalid/identifier"):
                with self.subTest(request_id=request_id), self.assertRaises(ValueError):
                    rt.conversation_list.remove(task, request_id=request_id)
            self.assertEqual(rt.log.read(), before)

    def test_web_remove_replays_response_and_filters_both_lists_after_restart(self):
        task = self.create()
        other = self.create(request_id="other-create-001")
        self.web.request("/api/task-summaries")
        response = self.remove(task)
        with self.web.factory() as rt:
            removal = rt.log.read(task_id=task)[-1]
        self.assertEqual(response, {"id": task, "removed": True, "removed_seq": removal.seq})
        before = self.web.events(task)
        self.assertEqual(self.remove(task), response)
        self.assertEqual(self.web.events(task), before)
        self.assertEqual({row["id"] for row in self.web.request("/api/tasks")}, {other})
        for _ in range(2):
            summaries = self.web.request("/api/task-summaries")
            self.assertEqual({row["id"] for row in summaries["tasks"]}, {other})
            self.assertEqual(summaries["removed"], [{"id": task, "seq": removal.seq}])
        with patch("cater.web.ActivityRunner", return_value=self.web.runner):
            self.web.handler = make_handler(self.web.db, runtime_factory=self.web.factory)
        self.assertEqual({row["id"] for row in self.web.request("/api/tasks")}, {other})
        self.assertEqual(self.web.request("/api/task-summaries")["removed"],
                         [{"id": task, "seq": removal.seq}])
        self.assertEqual(self.web.request(f"/api/tasks/{task}")["task"]["id"], task)
        self.web.start.assert_not_called()
        self.assertEqual(self.web.driver.calls, [])

    def test_web_cross_task_replay_is_conflict_without_cancelling_other_task(self):
        task = self.create()
        other = self.create(request_id="other-create-001")
        self.remove(task)
        with self.web.factory() as rt:
            before = rt.log.read()
        self.remove(other, expected=409)
        with self.web.factory() as rt:
            self.assertEqual(rt.log.read(), before)
            self.assertEqual(rt.execution_control.view(other)["state"], "running")

    def test_web_unknown_task_and_unknown_or_invalid_body_fields_never_write(self):
        task = self.create()
        with self.web.factory() as rt:
            before = rt.log.read()
        self.remove("missing-task", expected=404)
        payloads = [{}, {"request_id": None}, {"request_id": "short"}, {"request_id": []},
                    {"request_id": "remove-invalid-001", "delete_files": True},
                    {"request_id": "remove-invalid-001", "task_id": task},
                    {"request_id": "remove-invalid-001", "actor": "human"}]
        for payload in payloads:
            with self.subTest(payload=payload):
                self.web.request(f"/api/tasks/{task}/remove", payload, expected=400)
        with self.web.factory() as rt:
            self.assertEqual(rt.log.read(), before)
            self.assertEqual(rt.conversation_list.removed(), {})

    def test_cross_origin_and_unauthenticated_remove_are_rejected_without_writes(self):
        task = self.create()
        path = f"/api/tasks/{task}/remove"
        payload = {"request_id": "remove-security-001"}
        with self.web.factory() as rt:
            before = rt.log.read()
        self.request_with_headers(path, payload, expected=403, headers={"Origin": "https://attacker.example"})
        with patch("cater.web.ActivityRunner", return_value=self.web.runner):
            secured = make_handler(self.web.db, runtime_factory=self.web.factory, access_token="fixture-token")
        for headers in ({}, {"X-Cater-Access-Token": "wrong-token"}):
            with self.subTest(headers=headers):
                self.request_with_headers(path, payload, expected=401, headers=headers, handler_class=secured)
        self.request_with_headers(path, payload, expected=403, handler_class=secured,
                                  headers={"X-Cater-Access-Token": "fixture-token", "Origin": "https://attacker.example"})
        with self.web.factory() as rt:
            self.assertEqual(rt.log.read(), before)
            self.assertEqual(rt.conversation_list.removed(), {})
        self.request_with_headers(path, payload, expected=200, handler_class=secured,
                                  headers={"X-Cater-Access-Token": "fixture-token", "Origin": "http://localhost"})
        self.web.start.assert_not_called()
        self.assertEqual(self.web.driver.calls, [])

    def test_removed_task_rejects_api_mutations_without_events_or_model_calls(self):
        task = self.create()
        self.remove(task)
        before = self.web.events(task)
        requests = {
            "command": {"instruction": "Continue", "request_id": "removed-command-001"},
            "control": {"action": "resume", "request_id": "removed-resume-001"},
            "activate": {"request_id": "removed-activate-001"},
            "run": {"background": True}, "battle": {"background": True}, "dispatch": {},
            "assign": {"owner": "dev"}, "decide": {"resolution": "done"},
            "questions": {"question": "What happened?", "request_id": "removed-question-001"},
            "derive": {"goal": "Child task", "request_id": "removed-derive-001"},
            "workspace": {"workspace_id": "source", "expected_revision": None},
        }
        for action, payload in requests.items():
            with self.subTest(action=action):
                self.web.request(f"/api/tasks/{task}/{action}", payload, expected=409)
        self.assertEqual(self.web.events(task), before)
        self.web.start.assert_not_called()
        self.assertEqual(self.web.driver.calls, [])

    def test_removed_task_rejects_legacy_form_mutations(self):
        task = self.create()
        self.remove(task)
        before = self.web.events(task)
        for action, payload in (("command", {"instruction": "Continue", "request_id": "removed-form-command"}),
                                ("run", {}), ("battle", {}), ("dispatch", {}),
                                ("assign", {"owner": "dev"}), ("decide", {"resolution": "done"})):
            with self.subTest(action=action):
                self.request_with_headers(f"/tasks/{task}/{action}", payload, form=True, expected=409)
        self.assertEqual(self.web.events(task), before)
        self.web.start.assert_not_called()
        self.assertEqual(self.web.driver.calls, [])

    def test_inflight_model_return_is_recorded_without_followup_tools_or_false_stop_claim(self):
        task = self.web.create(workspace_id="source")["task"]["id"]
        current = self.web.runner.status(task)
        self.web.driver.block = True
        self.web.driver.turns = [ToolTurn("Late model return", [ToolCall("late-write", "workspace_write", {
            "path": "must-not-exist.txt", "content": "unexpected", "expected_sha256": None,
        })], message={"role": "assistant"})]
        errors = []

        def run():
            try:
                self.web.runner._run(task, current["id"], "run")
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=run)
        with patch("cater.workspace_tools.WorkspaceExecutor.execute") as execute:
            worker.start()
            try:
                self.assertTrue(self.web.driver.entered.wait(3))
                response = self.remove(task, "remove-inflight-001")
                self.assertTrue(response["removed"])
                self.assertFalse(self.web.driver.release.is_set())
                self.assertTrue(worker.is_alive())
                self.assertTrue(self.web.runner.status(task)["active"])
                with self.web.factory() as rt:
                    reason = rt.execution_control.view(task)["reason"]
                self.assertIn("\u5728\u9014", reason)
                self.assertNotIn("\u5df2\u505c\u6b62", reason)
                self.assertNotIn("\u5df2\u7ec8\u6b62", reason)
            finally:
                self.web.driver.release.set()
                worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            execute.assert_not_called()
        self.assertEqual(len(self.web.driver.calls), 1)
        self.assertFalse((self.web.root / "project" / "must-not-exist.txt").exists())
        self.assertFalse(self.web.runner.status(task)["active"])
        events = self.web.events(task)
        returned = [e for e in events if e.type == "team.model_called"]
        self.assertEqual(len(returned), 1)
        self.assertGreater(returned[0].seq, response["removed_seq"])
        self.assertFalse(any(e.type in {"team.tool_started", "workspace.tool_started"} for e in events))
        with self.web.factory() as rt:
            self.assertEqual(rt.execution_control.view(task)["state"], "cancelled")
            self.assertEqual(rt.conversation_list.removed(), {task: response["removed_seq"]})


    def test_successful_removal_retry_request_id_cannot_target_another_task(self):
        task = self.create()
        other = self.create(request_id="other-create-alias")
        first = self.remove(task, "remove-alias-original")
        with self.web.factory() as rt:
            original = rt.log.read()
            control = rt.execution_control.view(task)
        confirmation = self.remove(task, "remove-alias-retry")
        self.assertEqual(confirmation["id"], first["id"])
        self.assertTrue(confirmation["removed"])
        self.assertGreater(confirmation["removed_seq"], first["removed_seq"])
        with self.web.factory() as rt:
            before = rt.log.read()
            self.assertEqual(before[:-1], original)
            event = before[-1]
            self.assertEqual(event.type, "task.removed_from_list")
            self.assertEqual(event.task_id, task)
            self.assertEqual(event.actor, Actor.human())
            self.assertEqual(event.seq, confirmation["removed_seq"])
            self.assertEqual(event.payload["request_id"], "remove-alias-retry")
            self.assertEqual(event.payload["previous_removal_seq"], first["removed_seq"])
            cancels = [event for event in before if event.type == "task.execution_controlled"
                       and event.task_id == task and event.payload.get("action") == "cancel"]
            self.assertEqual(len(cancels), 1)
            self.assertEqual(rt.execution_control.view(task), control)
        self.assertEqual(self.remove(task, "remove-alias-retry"), confirmation)
        with self.web.factory() as rt:
            self.assertEqual(rt.log.read(), before)
        self.remove(other, "remove-alias-retry", expected=409)
        with self.web.factory() as rt:
            self.assertEqual(rt.log.read(), before)
            self.assertEqual(rt.execution_control.view(task), control)
            self.assertEqual(rt.execution_control.view(other)["state"], "running")

    def test_removed_legacy_creation_replay_rejects_old_activation(self):
        payload = {"goal": "Legacy creation replay", "request_id": "legacy-remove-create", "run_now": "on"}
        with patch.object(self.web.runner, "dispatch_activation") as dispatch:
            self.request_with_headers("/tasks", payload, form=True, expected=303)
            with self.web.factory() as rt:
                task, = rt.tasks()
            dispatch.assert_called_once()
        self.remove(task.id, "remove-legacy-create")
        before = self.web.events(task.id)
        with patch.object(self.web.runner, "dispatch_activation") as dispatch:
            self.request_with_headers("/tasks", payload, form=True, expected=409)
            dispatch.assert_not_called()
        self.assertEqual(self.web.events(task.id), before)
        self.assertEqual(self.web.driver.calls, [])

    def test_removal_confirmation_request_id_cannot_be_used_for_control(self):
        task = self.create()
        other = self.create(request_id="control-after-alias-create")
        self.remove(task, "remove-before-alias-control")
        self.remove(task, "remove-control-alias")
        with self.web.factory() as rt:
            before = rt.log.read()
            for target, action in ((other, "cancel"), (other, "pause"), (task, "cancel")):
                with self.subTest(target=target, action=action), self.assertRaises(WorkConflict):
                    rt.execution_control.change(target, action, request_id="remove-control-alias")
            self.assertEqual(rt.log.read(), before)
            self.assertEqual(rt.execution_control.view(other)["state"], "running")

    def test_command_rechecks_removal_inside_receive_transaction(self):
        task = self.create()
        receive = self.web.runner.receive_team
        committed = []

        def remove_before_receive(runtime, task_id, *args, **kwargs):
            with self.web.factory() as other:
                other.conversation_list.remove(task_id, request_id="remove-command-race")
                committed.extend(other.log.read())
            return receive(runtime, task_id, *args, **kwargs)

        with patch.object(self.web.runner, "receive_team", side_effect=remove_before_receive):
            self.web.request(f"/api/tasks/{task}/command", {
                "instruction": "Must not be accepted after removal", "request_id": "command-after-removal",
            }, expected=409)
        with self.web.factory() as rt:
            self.assertEqual(rt.log.read(), committed)
        self.web.start.assert_not_called()
        self.assertEqual(self.web.driver.calls, [])


if __name__ == "__main__":
    unittest.main()
