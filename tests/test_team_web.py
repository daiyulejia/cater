"""Web contracts for message collaboration, using temporary data and a fake driver."""

from __future__ import annotations

import io
import json
import tempfile
import threading
import unittest
import zipfile
from email.message import Message
from http.client import HTTPResponse
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

from cater.artifacts import normalize_deliverables
from cater.conversation import ConversationService
from cater.delivery import DeliveryService
from cater.events import Actor
from cater.fold import TaskSnapshot
from cater.runtime import Runtime
from cater.task_requests import activation, prepare
from cater.team import TeamService
from cater.tool_protocol import ToolCall, ToolTurn
from cater.web import ActivityRunner, _task_summary, make_handler, task_api_payload, task_to_dict
from cater.work import WorkConflict
from cater.workspaces import HOST as WORKSPACE_HOST


class NativeDriver:
    name = "web-native-fixture"

    def __init__(self):
        self.calls = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.block = False
        self.turns = []

    def complete(self, **kwargs):
        raise AssertionError("team web tests must not use legacy model completion")

    def tool_turn(self, **kwargs):
        self.calls.append(kwargs)
        self.entered.set()
        if self.block and not self.release.wait(5):
            raise AssertionError("test did not release the fake native turn")
        if self.turns:
            return self.turns.pop(0)
        return ToolTurn("Fixture reply", [], tokens=1, meta={"token_usage": "reported"})


class MemorySocket:
    def __init__(self, raw):
        self.raw = raw

    def makefile(self, *args):
        return io.BytesIO(self.raw)


class TextOnlyDriver:
    name = NativeDriver.name

    def complete(self, **kwargs):
        raise AssertionError("fallback tests must not call a model")


class TeamWebTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.db = self.root / "team-web.db"
        self.roles = self.root / "roles.json"
        config = json.loads(Path("cater/packs/default_roles.json").read_text(encoding="utf-8"))
        config["organization"]["leader"]["driver"] = NativeDriver.name
        self.roles.write_text(json.dumps(config), encoding="utf-8")
        (self.root / "project").mkdir()
        self.workspaces = self.root / "workspaces.json"
        self.workspaces.write_text(json.dumps({"workspaces": [
            {"id": "source", "title": "Source", "root": str(self.root / "project"), "mode": "edit"}
        ]}), encoding="utf-8")
        self.driver = NativeDriver()
        self.runner = ActivityRunner(self.factory)
        with patch("cater.web.ActivityRunner", return_value=self.runner):
            self.handler = make_handler(self.db, runtime_factory=self.factory)
        self.start = self.enterContext(patch.object(self.runner, "_start"))

    def factory(self):
        return Runtime(self.db, roles_path=self.roles, workspaces_path=self.workspaces,
                       drivers={NativeDriver.name: self.driver}, close_drivers_on_close=False)

    def request(self, path, data=None, *, expected=200, form=False):
        method = "GET" if data is None else "POST"
        raw = b"" if data is None else (urlencode(data) if form else json.dumps(data)).encode("utf-8")
        handler = self.handler.__new__(self.handler)
        handler.path, handler.command = path, method
        handler.request_version = "HTTP/1.1"
        handler.requestline = f"{method} {path} HTTP/1.1"
        handler.headers = Message()
        handler.headers["Host"] = "localhost"
        handler.headers["Content-Type"] = "application/x-www-form-urlencoded" if form else "application/json"
        handler.headers["Content-Length"] = str(len(raw))
        handler.rfile, handler.wfile = io.BytesIO(raw), io.BytesIO()
        getattr(handler, "do_" + method)()
        response = HTTPResponse(MemorySocket(handler.wfile.getvalue()))
        response.begin()
        body = response.read()
        self.assertEqual(response.status, expected, body)
        if response.headers.get_content_type() == "application/json":
            body = json.loads(body)
        return body

    def create(self, **changes):
        return self.request("/api/tasks", {
            "goal": "Build a small fixture", "collaboration": "team",
            "run": True, "background": True, "request_id": "team-create-001", **changes
        }, expected=201 if changes.get("run") is False else 202)

    def command(self, task, **changes):
        return self.request(f"/api/tasks/{task}/command", {
            "instruction": "Continue with this direction", "request_id": "team-command-001", **changes
        }, expected=202)

    def events(self, task):
        with self.factory() as rt:
            self.assertTrue(rt.verify_chain().ok)
            return rt.log.read(task_id=task)

    def test_create_enables_before_message_and_activation_without_legacy_intake(self):
        with patch.object(Runtime, "intake", side_effect=AssertionError("legacy intake")), \
                patch.object(Runtime, "dispatch", side_effect=AssertionError("legacy dispatch")):
            detail = self.create(workspace_id="source", dispatch=True)
        task = detail["task"]["id"]
        events = self.events(task)
        kinds = [e.type for e in events]
        self.assertLess(kinds.index("workspace.bound"), kinds.index("team.started"))
        self.assertLess(kinds.index("team.started"), kinds.index("team.message_received"))
        self.assertLess(kinds.index("team.message_received"), kinds.index("task.activation_requested"))
        source = next(e for e in events if e.type == "task.created")
        message = next(e for e in events if e.type == "team.message_received")
        self.assertEqual(message.payload["request_id"], f"team-creation-{source.seq}")
        with self.factory() as rt:
            intent = activation(rt, task)
            self.assertEqual(intent.payload["scope"], rt.work.scope(task))
            self.assertEqual(intent.payload["protocol"], 1)
        self.assertTrue(detail["team"]["enabled"])
        self.assertEqual(detail["team"]["pending_count"], 1)
        self.assertIsNone(detail["task"]["work"])
        self.assertIsNone(detail["task"]["brief"])
        self.assertEqual(detail["conversation"], [])
        self.assertEqual(self.driver.calls, [])
        self.start.assert_called_once()

    def test_creation_replay_does_not_receive_or_dispatch_twice(self):
        first = self.create()
        task = first["task"]["id"]
        before = self.events(task)
        duplicate = self.request("/api/tasks", {
            "goal": "Build a small fixture", "collaboration": "team", "run": True,
            "background": True, "request_id": "team-create-001"
        })
        self.assertEqual(duplicate["task"]["id"], task)
        self.assertEqual(self.events(task), before)
        self.start.assert_called_once()

    def test_creation_forwards_original_goal_to_team(self):
        original = TeamService.receive
        received = []

        def receive(service, task, text, **kwargs):
            received.append(text)
            return original(service, task, text, **kwargs)

        goal = "  Original goal\nwith exact whitespace  \n"
        with patch.object(TeamService, "receive", receive):
            self.create(goal=goal)
        self.assertEqual(received, [goal])

    def test_failed_receive_rolls_back_creation_binding_and_enable(self):
        with self.factory() as rt, patch.object(TeamService, "receive", side_effect=ValueError("bad message")):
            with self.assertRaisesRegex(ValueError, "bad message"):
                prepare(rt, {"goal": "Create atomically", "collaboration": "team", "workspace_id": "source",
                             "run": True}, "team-atomic-001")
            self.assertEqual(rt.log.read(), [])

    def test_demo_team_request_still_uses_legacy_intake(self):
        with Runtime(self.root / "demo.db") as rt:
            for selection in ({}, {"collaboration": "team"}):
                with self.subTest(selection=selection):
                    task, _, intent = prepare(rt, {"goal": "Demo fixture", "run": True, **selection}, None)
                    self.assertFalse(rt.team.enabled(task))
                    self.assertIsNotNone(rt.task(task).plan)
                    self.assertEqual(intent.payload["action"], "run")

    def test_native_creation_without_selection_defaults_to_team_and_replays_once(self):
        payload = {"goal": "  Default native fixture\nwith exact whitespace  \n", "run": True,
                   "background": True, "request_id": "default-create-001", "workspace_id": "source"}
        with patch.object(Runtime, "intake", side_effect=AssertionError("legacy intake")), \
                patch.object(Runtime, "dispatch", side_effect=AssertionError("legacy dispatch")):
            detail = self.request("/api/tasks", payload, expected=202)
            task = detail["task"]["id"]
            before = self.events(task)
            duplicate = self.request("/api/tasks", payload)
        self.assertEqual(duplicate["task"]["id"], task)
        self.assertEqual(self.events(task), before)
        self.assertTrue(detail["team"]["enabled"])
        self.assertEqual(detail["team"]["pending_count"], 1)
        self.assertEqual(detail["conversation"], [])
        self.assertIsNone(detail["task"]["plan"])
        source = next(e for e in before if e.type == "task.created")
        self.assertNotIn("collaboration", source.payload["creation_request"])
        message = next(e for e in before if e.type == "team.message_received")
        self.assertEqual(message.payload["text"], payload["goal"])
        kinds = [e.type for e in before]
        self.assertLess(kinds.index("workspace.bound"), kinds.index("team.started"))
        self.assertLess(kinds.index("team.message_received"), kinds.index("task.activation_requested"))
        self.start.assert_called_once()
        self.assertEqual(self.run_current(task)["state"], "completed")
        self.assertEqual(len(self.driver.calls), 1)
        self.assertTrue(any(e.type == "team.run_finished" for e in self.events(task)))

    def test_legacy_creation_retry_keeps_original_identity_and_activation(self):
        payload = {"goal": "  Historical native request  ", "run": True,
                   "background": True, "request_id": "legacy-create-retry"}
        # Simulate the old default and a lost response before activation dispatch.
        with patch.object(TeamService, "available", return_value=False), \
                patch.object(self.runner, "dispatch_activation"):
            first = self.request("/api/tasks", payload, expected=202)
        task = first["task"]["id"]
        before = self.events(task)
        with self.factory() as rt:
            intent = activation(rt, task)
        self.assertIsNotNone(intent)
        with patch.object(Runtime, "intake", side_effect=AssertionError("repeated intake")), \
                patch.object(TeamService, "enable", side_effect=AssertionError("retry switched engine")):
            replay = self.request("/api/tasks", payload)
            after = self.events(task)
            self.request("/api/tasks", payload)
        self.assertEqual(replay["task"]["id"], task)
        self.assertFalse(replay["team"]["enabled"])
        self.assertEqual(after[:len(before)], before)
        self.assertEqual(self.events(task), after)
        with self.factory() as rt:
            self.assertEqual(activation(rt, task), intent)
            self.assertEqual(len(rt.tasks()), 1)
        queued = [e for e in after if e.type == "activity.queued"]
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0].payload["activation_seq"], intent.seq)
        self.start.assert_called_once()
        for change in ({"goal": "Changed"}, {"run": False}, {"collaboration": "team"}):
            self.request("/api/tasks", {**payload, **change}, expected=409)
        self.assertEqual(self.events(task), after)

    def test_default_creation_keeps_native_mentions_in_team(self):
        detail = self.request("/api/tasks", {"goal": "@dev inspect this fixture"}, expected=201)
        self.assertTrue(detail["team"]["enabled"])
        message = next(e for e in self.events(detail["task"]["id"]) if e.type == "team.message_received")
        self.assertEqual(message.payload["recipient"], "dev")

    def test_non_native_ordinary_entries_keep_legacy_behavior(self):
        self.driver = TextOnlyDriver()
        detail = self.request("/api/tasks", {"goal": "Text only fixture"}, expected=201)
        task = detail["task"]["id"]
        self.assertFalse(detail["team"]["enabled"])
        self.assertTrue(any(e["type"] == "planning.requested" for e in detail["events"]))
        with patch.object(self.runner, "answer_conversations") as answer:
            command = self.command(task)
        self.assertFalse(command["team"]["enabled"])
        self.assertEqual(len(command["conversation"]), 1)
        answer.assert_called_once_with(task)
        child = self.request(f"/api/tasks/{task}/derive", {
            "goal": "Text only child", "request_id": "text-only-derive", "run": True
        }, expected=201)
        self.assertFalse(child["team"]["enabled"])
        self.assertTrue(any(e["type"] == "planning.requested" for e in child["events"]))

    def test_explicit_legacy_creation_options_do_not_enable_team(self):
        for options in ({"battle": True}, {"battle": True, "collaboration": "team"},
                        {"dispatch": True}, {"requested_route": "auto"},
                        {"assign_to": "dev", "assign_objective": "Explicit work"}):
            with self.subTest(options=options):
                detail = self.request("/api/tasks", {
                    "goal": "Explicit legacy fixture", "background": True, **options
                }, expected=202 if options.get("battle") else 201)
                self.assertFalse(detail["team"]["enabled"])
                self.assertTrue(any(e["type"] == "planning.requested" for e in detail["events"]))
                if options.get("battle"):
                    self.assertEqual(detail["activity"]["action"], "battle")

    def test_explicit_legacy_actions_and_structured_commands_keep_semantics(self):
        for action, data, method in (("battle", {}, "battle"), ("dispatch", {}, "dispatch"),
                                     ("assign", {"owner": "dev"}, "assign"),
                                     ("command", {"instruction": "Update", "goal": "New goal"}, "chat"),
                                     ("command", {"instruction": "Update", "acceptance": ["Check"]}, "chat")):
            with self.subTest(action=action, data=data):
                with self.factory() as rt:
                    task = rt.create_task("Legacy operation fixture")
                with patch.object(Runtime, method, autospec=True) as operation:
                    self.request(f"/api/tasks/{task}/{action}", data)
                self.assertEqual(operation.call_args.args[1], task)
                with self.factory() as rt:
                    self.assertFalse(rt.team.enabled(task))

    def test_non_native_command_team_request_falls_back_to_legacy_chat(self):
        with self.factory() as rt:
            task = rt.create_task("Non native fixture")
        with patch.object(TeamService, "available", return_value=False), \
                patch.object(self.runner, "answer_conversations") as answer:
            detail = self.command(task, collaboration="team")
        self.assertFalse(detail["team"]["enabled"])
        self.assertEqual(len(detail["conversation"]), 1)
        answer.assert_called_once_with(task)

    def test_command_enables_and_preserves_original_argument_and_idempotency(self):
        with self.factory() as rt:
            task = rt.create_task("Existing fixture")
        original = TeamService.receive
        received = []

        def receive(service, task_id, text, **kwargs):
            received.append(text)
            return original(service, task_id, text, **kwargs)

        text = "  Keep original\n  indentation  \n"
        with patch.object(TeamService, "receive", receive), \
                patch.object(Runtime, "chat", side_effect=AssertionError("legacy chat")):
            first = self.command(task, instruction=text, collaboration="team")
            self.command(task, instruction=text)
        self.assertEqual(received, [text, text])
        self.assertTrue(first["team"]["enabled"])
        events = self.events(task)
        self.assertEqual(sum(e.type == "team.message_received" for e in events), 1)
        self.assertEqual(sum(e.type == "team.started" for e in events), 1)
        self.assertFalse(any(e.type.startswith("conversation.") for e in events))
        self.assertEqual(len([e for e in events if e.type == "activity.queued"]), 1)

    def test_enabled_command_queues_new_message_after_current_activity(self):
        task = self.create()["task"]["id"]
        detail = self.command(task, **{"continue": False})
        self.assertEqual(detail["team"]["pending_count"], 2)
        self.assertEqual(detail["activity"]["follow_up"]["action"], "run")
        self.start.assert_called_once()

    def test_switch_rejects_legacy_activity_even_if_only_durable_queue_remains(self):
        for durable_only in (False, True):
            with self.subTest(durable_only=durable_only):
                with self.factory() as rt:
                    task = rt.create_task("Legacy task")
                self.runner.submit(task, "run")
                if durable_only:
                    self.runner._activities.pop(task)
                before = self.events(task)
                self.request(f"/api/tasks/{task}/command", {
                    "instruction": "Switch engine"
                }, expected=409)
                self.assertEqual(self.events(task), before)
                with self.factory() as rt:
                    self.assertFalse(rt.team.enabled(task))

    def test_switch_rejects_legacy_conversation_and_question_workers(self):
        for workers in (self.runner._conversation_tasks, self.runner._question_tasks):
            with self.factory() as rt:
                task = rt.create_task("Legacy conversation")
            workers[task] = "fixture-worker"
            before = self.events(task)
            self.request(f"/api/tasks/{task}/command", {
                "instruction": "Switch engine"
            }, expected=409)
            self.assertEqual(self.events(task), before)
            with self.factory() as rt:
                self.assertFalse(rt.team.enabled(task))
        self.start.assert_not_called()

    def test_invalid_team_command_does_not_enable_task_or_queue_execution(self):
        with self.factory() as rt:
            task = rt.create_task("Validate first")
        for text in (None, "", "  ", [], 42):
            with self.subTest(text=text):
                before = self.events(task)
                self.request(f"/api/tasks/{task}/command", {
                    "instruction": text
                }, expected=400)
                self.assertEqual(self.events(task), before)
        self.start.assert_not_called()

    def test_conflicting_message_identity_does_not_change_existing_queue(self):
        task = self.create()["task"]["id"]
        self.command(task)
        before = self.events(task)
        self.request(f"/api/tasks/{task}/command", {
            "instruction": "Different text", "request_id": "team-command-001"
        }, expected=409)
        self.assertEqual(self.events(task), before)

    def test_command_without_selection_switches_idle_legacy_once(self):
        with self.factory() as rt:
            task = rt.create_task("Legacy conversation")
            rt.intake(task)
            before = rt.log.read(task_id=task)
        with patch.object(self.runner, "answer_conversations", side_effect=AssertionError("legacy conversation")), \
                patch.object(Runtime, "chat", side_effect=AssertionError("legacy chat")):
            detail = self.command(task)
            self.command(task)
        self.assertTrue(detail["team"]["enabled"])
        self.assertEqual(detail["conversation"], [])
        events = self.events(task)
        self.assertEqual(events[:len(before)], before)
        self.assertEqual(sum(e.type == "team.started" for e in events), 1)
        self.assertEqual(sum(e.type == "team.message_received" for e in events), 1)
        self.start.assert_called_once()

    def test_plain_form_create_and_command_default_to_team(self):
        payload = {"goal": "  Form fixture  ", "request_id": "default-form-create", "run_now": "on"}
        with patch.object(Runtime, "intake", side_effect=AssertionError("legacy intake")), \
                patch.object(Runtime, "chat", side_effect=AssertionError("legacy chat")):
            self.request("/tasks", payload, form=True, expected=303)
            with self.factory() as rt:
                task, = rt.tasks()
            before = self.events(task.id)
            self.request("/tasks", payload, form=True, expected=303)
            self.assertEqual(self.events(task.id), before)
            self.assertEqual(len(self.driver.calls), 1)
            message = {"instruction": "  Continue form fixture  ", "request_id": "default-form-command"}
            self.request(f"/tasks/{task.id}/command", message, form=True, expected=303)
            before = self.events(task.id)
            self.request(f"/tasks/{task.id}/command", message, form=True, expected=303)
            self.assertEqual(self.events(task.id), before)
        self.assertEqual(self.run_current(task.id)["state"], "completed")
        self.assertEqual(len(self.driver.calls), 2)
        received = [e.payload["text"] for e in self.events(task.id) if e.type == "team.message_received"]
        self.assertEqual(received, [payload["goal"], message["instruction"]])

    def test_plain_form_command_switches_idle_legacy_but_rejects_active_legacy(self):
        for active in (False, True):
            with self.subTest(active=active):
                with self.factory() as rt:
                    task = rt.create_task("Legacy form fixture")
                if active:
                    self.runner.submit(task, "run")
                before = self.events(task)
                self.request(f"/tasks/{task}/command", {"instruction": "Continue form fixture"},
                             form=True, expected=409 if active else 303)
                with self.factory() as rt:
                    self.assertEqual(rt.team.enabled(task), not active)
                if active:
                    self.assertEqual(self.events(task), before)

    def test_old_pending_conversation_is_not_consumed_after_switch_or_restart(self):
        with self.factory() as rt:
            task = rt.create_task("Old pending conversation")
            rt.conversation.receive(task, "Old unsent direction", request_id="old-message-001")
        self.command(task, collaboration="team")
        with patch.object(ConversationService, "resolve_next", side_effect=AssertionError("old conversation")):
            self.runner.answer_conversations(task)
            self.runner.dispatch_conversation(task, {"conversation_seq": 1})
            self.runner._recover_pending_questions()
            # Recovery's conversation wakeup uses the same guarded entry point.
            with patch.object(self.runner, "_recover_activity", return_value=None):
                self.runner.recover_interrupted()
        with self.factory() as rt:
            self.assertEqual(rt.conversation.view(task)[0]["status"], "pending")
            self.assertEqual(rt.team.view(task)["pending_count"], 1)

    def test_native_turn_does_not_block_receiving_and_queuing_another_message(self):
        task = self.create()["task"]["id"]
        activity = self.runner.status(task)
        self.driver.block = True
        worker = threading.Thread(target=self.runner._run, args=(task, activity["id"], "run"))
        worker.start()
        try:
            self.assertTrue(self.driver.entered.wait(3))
            detail = self.command(task)
            self.assertFalse(self.driver.release.is_set())
            self.assertTrue(detail["activity"]["active"])
            self.assertEqual(detail["activity"]["follow_up"]["action"], "run")
        finally:
            self.driver.release.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        with self.factory() as rt:
            self.assertEqual(rt.team.view(task)["pending_count"], 0)
            self.assertEqual(len(rt.team.view(task)["runs"]), 2)
        self.assertEqual(len(self.driver.calls), 2)

    def test_run_and_control_resume_enter_collaborate(self):
        task = self.create(run=False)["task"]["id"]
        with patch.object(Runtime, "collaborate", autospec=True) as collaborate:
            self.request(f"/api/tasks/{task}/run", {})
            self.assertEqual(collaborate.call_args.args[1], task)
            self.request(f"/api/tasks/{task}/control", {"action": "pause", "request_id": "team-pause-001"})
            self.request(f"/api/tasks/{task}/control", {"action": "resume", "request_id": "team-resume-001"})
            current = self.runner.status(task)
            self.runner._run(task, current["id"], current["action"])
            self.assertEqual(collaborate.call_count, 2)

    def test_derive_inherits_team_and_runs_through_collaborate(self):
        task = self.create()["task"]["id"]
        with patch.object(Runtime, "intake", side_effect=AssertionError("legacy intake")):
            child = self.request(f"/api/tasks/{task}/derive", {
                "goal": "Derived fixture", "request_id": "team-derive-001", "run": True
            }, expected=201)
        self.assertTrue(child["team"]["enabled"])
        self.assertEqual(child["team"]["objective"], "Derived fixture")
        self.assertIsNone(child["task"]["plan"])
        child_id = child["task"]["id"]
        current = self.runner.status(child_id)
        with patch.object(Runtime, "collaborate", autospec=True) as collaborate:
            self.runner._run(child_id, current["id"], current["action"])
        self.assertEqual(collaborate.call_args.args[1], child_id)

    def test_derive_from_legacy_defaults_child_to_team_and_replays_once(self):
        with self.factory() as rt:
            parent = rt.create_task("Legacy source")
            rt.intake(parent)
        parent_events = self.events(parent)
        payload = {"goal": "  Derived native fixture  ", "request_id": "default-native-derive", "run": True}
        with patch.object(Runtime, "intake", side_effect=AssertionError("legacy child intake")):
            child = self.request(f"/api/tasks/{parent}/derive", payload, expected=201)
            task = child["task"]["id"]
            before = self.events(task)
            duplicate = self.request(f"/api/tasks/{parent}/derive", payload)
        self.assertEqual(duplicate["task"]["id"], task)
        self.assertEqual(self.events(task), before)
        self.assertEqual(self.events(parent), parent_events)
        self.assertTrue(child["team"]["enabled"])
        self.assertIsNone(child["task"]["plan"])
        self.assertEqual(sum(e.type == "team.message_received" for e in before), 1)
        self.assertEqual(next(e.payload["text"] for e in before if e.type == "team.message_received"), payload["goal"])
        self.start.assert_called_once()
        self.assertEqual(self.run_current(task)["state"], "completed")
        self.assertEqual(len(self.driver.calls), 1)

    def test_old_derived_request_retry_does_not_switch_existing_child(self):
        with self.factory() as rt:
            parent = rt.create_task("Legacy source")
        payload = {"goal": "Historical child", "request_id": "legacy-child-retry", "run": True}
        with patch.object(TeamService, "available", return_value=False):
            child = self.request(f"/api/tasks/{parent}/derive", payload, expected=201)
        task = child["task"]["id"]
        before = self.events(task)
        duplicate = self.request(f"/api/tasks/{parent}/derive", payload)
        self.assertEqual(duplicate["task"]["id"], task)
        self.assertFalse(duplicate["team"]["enabled"])
        self.assertEqual(self.events(task), before)
        self.start.assert_called_once()

    def test_failed_derived_message_rolls_back_child_creation(self):
        task = self.create()["task"]["id"]
        with self.factory() as rt:
            before = rt.log.read()
        with patch.object(TeamService, "receive", side_effect=ValueError("invalid derived message")):
            self.request(f"/api/tasks/{task}/derive", {
                "goal": "Derived fixture", "request_id": "team-derive-rollback", "run": True
            }, expected=400)
        with self.factory() as rt:
            self.assertEqual(rt.log.read(), before)

    def test_details_hide_legacy_work_and_checkpoint_but_keep_history(self):
        with Runtime(self.db) as rt:
            task = rt.create_task("Legacy work")
            rt.intake(task)
            rt.log.emit(type="brief.generated", task_id=task, actor=Actor.system(),
                        payload={"brief": {"checkpoint": {"state": "old-work"}}, "reason": "work_checkpoint"})
            before = rt.log.read(task_id=task)
        self.command(task, collaboration="team")
        with self.factory() as rt:
            detail = task_api_payload(rt, task)
            self.assertEqual(rt.log.read(task_id=task)[:len(before)], before)
            self.assertIsNotNone(rt.task(task).plan)
        self.assertIsNone(detail["task"]["work"])
        self.assertIsNone(detail["task"]["brief"])
        self.assertIsNone(detail["task"]["plan"])
        self.assertIsNone(detail["review"])
        self.assertEqual(detail["decision_ladder"], [])
        self.assertTrue(any(e["type"] == "brief.generated" for e in detail["events"]))
        rendered = self.request(f"/tasks/{task}").decode("utf-8")
        self.assertIn("Current Work", rendered)
        self.assertNotIn("Organization Path", rendered)
        self.assertNotIn("Decision Ladder", rendered)

    def test_team_archive_download_does_not_require_legacy_delivery_acceptance(self):
        task = self.create()["task"]["id"]
        with self.factory() as rt:
            artifact, = normalize_deliverables([{
                "filename": "result.txt", "title": "Result", "summary": "Fixture result", "format": "text",
                "content": "Exact fixture content"
            }])
            rt.log.emit(type="deliverable.published", task_id=task, actor=rt._leader_actor(),
                        payload={**artifact, "source": "team", "phase": "team"})
        with patch.object(DeliveryService, "archive", side_effect=AssertionError("legacy archive")):
            raw = self.request(f"/api/tasks/{task}/delivery")
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            self.assertEqual(archive.read("result.txt"), b"Exact fixture content")
            self.assertIn("__cater__/manifest.json", archive.namelist())

    def test_legacy_delivery_download_keeps_existing_archive_path(self):
        with self.factory() as rt:
            task = rt.create_task("Legacy download")
        with patch.object(DeliveryService, "archive", return_value=b"legacy-zip") as archive, \
                patch.object(TeamService, "archive", side_effect=AssertionError("team archive")):
            self.assertEqual(self.request(f"/api/tasks/{task}/delivery"), b"legacy-zip")
        self.assertEqual(archive.call_args.args, (task,))

    def test_team_summary_matches_detail_without_claiming_accepted_delivery(self):
        task = self.create()["task"]["id"]
        current = self.runner.status(task)
        self.runner._run(task, current["id"], current["action"])
        with self.factory() as rt:
            rt.log.emit(type="team.objective_completed", task_id=task, actor=Actor.system("team"),
                        payload={"summary": "Fixture complete", "verification": "Fixture only"})
        detail = self.request(f"/api/tasks/{task}")
        summary = self.request("/api/task-summaries")["tasks"][0]
        self.assertEqual(summary["team"]["status"], "done")
        self.assertEqual(summary["team"], detail["summary"]["team"])
        self.assertEqual(summary["status_label"], detail["summary"]["status_label"])
        self.assertIsNone(detail["task"]["work"])
        self.assertNotIn("accepted", summary)
        self.assertEqual(summary["conversation_count"], detail["summary"]["conversation_count"])

    def run_current(self, task):
        current = self.runner.status(task)
        self.runner._run(task, current["id"], current["action"])
        return self.runner.status(task)

    def pause(self, task):
        self.request(f"/api/tasks/{task}/control", {"action": "pause", "request_id": "team-readonly-pause"})

    def test_paused_customer_chat_answers_read_only_and_keeps_assignments_paused(self):
        task = self.create(workspace_id="source")["task"]["id"]
        self.run_current(task)
        self.pause(task)
        with self.factory() as rt:
            rt.team._send(task, rt.organization.leader, "dev", "Do not execute while paused", "assignment")
            control = rt.execution_control.view(task)
        before = len(self.driver.calls)
        detail = self.command(task, instruction="What has been done so far?")
        self.assertEqual(detail["execution_control"], control)
        self.assertIn("team_message_seq", detail["activity"])
        self.assertEqual(self.run_current(task)["state"], "completed")
        self.assertEqual(len(self.driver.calls), before + 1)
        self.assertEqual({tool["name"] for tool in self.driver.calls[-1]["tools"]}, {"read_artifact", "read_receipts", "wait_for_reply"})
        with self.factory() as rt:
            self.assertEqual(rt.execution_control.view(task), control)
            self.assertEqual(rt.team.view(task)["pending_count"], 1)
            self.assertTrue(all(run["role"] != "dev" for run in rt.team.view(task)["runs"]))
        self.assertFalse(any(e.type == "team.tool_started" for e in self.events(task)))

    def test_paused_chat_cannot_execute_a_model_requested_workspace_write(self):
        task = self.create(workspace_id="source")["task"]["id"]
        self.run_current(task)
        self.pause(task)
        self.driver.turns = [ToolTurn("", [ToolCall("forbidden-write", "workspace_write", {
            "path": "must-not-exist.txt", "content": "unexpected", "expected_sha256": None
        })], message={"role": "assistant"})]
        self.command(task)
        with patch("cater.workspace_tools.WorkspaceExecutor.execute", side_effect=AssertionError("write executed")) as execute:
            self.run_current(task)
        execute.assert_not_called()
        self.assertEqual({tool["name"] for tool in self.driver.calls[-1]["tools"]}, {"read_artifact", "read_receipts", "wait_for_reply"})
        self.assertFalse((self.root / "project" / "must-not-exist.txt").exists())
        with self.factory() as rt:
            self.assertEqual(rt.execution_control.view(task)["state"], "paused")

    def test_paused_chat_does_not_authorize_unbound_run_or_legacy_messages(self):
        task = self.create()["task"]["id"]
        self.run_current(task)
        self.pause(task)
        with patch.object(Runtime, "collaborate", side_effect=AssertionError("unbound run")):
            self.request(f"/api/tasks/{task}/run", {}, expected=409)
            current = self.runner.submit(task, "run")
        self.assertEqual(current["state"], "failed")
        before = len(self.driver.calls)
        with self.factory() as rt:
            self.assertIsNotNone(ActivityRunner._control_failure(rt, task))
        self.assertEqual(len(self.driver.calls), before)

    def test_team_chat_binding_rejects_foreign_nonhuman_and_missing_sources(self):
        task = self.create()["task"]["id"]
        self.run_current(task)
        self.pause(task)
        with self.factory() as rt:
            other = rt.create_task("Other container")
            rt.team.enable(other)
            foreign = rt.team.receive(other, "Foreign message")
            internal = rt.team._send(task, rt.organization.leader, "dev", "Internal assignment", "assignment")
            for seq in (True, -1, foreign.seq, internal.seq):
                with self.subTest(seq=seq), self.assertRaises(WorkConflict):
                    self.runner._submit(task, "run", queue_after_current=True, binding={"team_message_seq": seq})
        self.assertEqual(self.runner.status(task)["state"], "completed")

    def test_pause_during_old_activity_does_not_discard_new_chat_follow_up(self):
        task = self.create()["task"]["id"]
        current = self.runner.status(task)
        self.driver.block = True
        worker = threading.Thread(target=self.runner._run, args=(task, current["id"], "run"))
        worker.start()
        try:
            self.assertTrue(self.driver.entered.wait(3))
            self.pause(task)
            self.command(task, instruction="Explain while paused")
        finally:
            self.driver.release.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertIn(self.runner.status(task)["state"], {"queued", "completed"})
        if self.runner.status(task)["state"] == "queued":
            self.run_current(task)
        with self.factory() as rt:
            self.assertEqual(rt.execution_control.view(task)["state"], "paused")
            self.assertEqual(rt.team.view(task)["pending_count"], 0)
        self.assertEqual({tool["name"] for tool in self.driver.calls[-1]["tools"]}, {"read_artifact", "read_receipts", "wait_for_reply"})

    def test_old_workspace_uncertainty_blocks_team_write_but_allows_chat(self):
        task = self.create(workspace_id="source")["task"]["id"]
        self.run_current(task)
        with self.factory() as rt:
            binding = rt.workspaces.view(task)
            started = rt.log.emit(type="workspace.tool_started", task_id=task, actor=WORKSPACE_HOST,
                payload={"root": binding["root"], "name": "workspace_write", "call_id": "old-write"})
        self.driver.turns = [ToolTurn("", [ToolCall("new-write", "workspace_write", {
            "path": "must-not-exist.txt", "content": "unexpected", "expected_sha256": None
        })], message={"role": "assistant"})]
        self.command(task)
        with patch("cater.workspace_tools.WorkspaceExecutor.execute", side_effect=AssertionError("uncertain write")) as execute:
            self.run_current(task)
        execute.assert_not_called()
        self.assertFalse((self.root / "project" / "must-not-exist.txt").exists())
        with self.factory() as rt:
            self.assertTrue(any(e.seq == started.seq for e in rt.workspaces.pending(task_id=task)))

    def test_team_projection_does_not_inherit_done_from_old_accepted_work(self):
        task = TaskSnapshot("container", goal="Old accepted objective", status="done", work={
            "items": [{"status": "accepted"}], "scope_current": True, "delivery": {"status": "accepted"}
        })
        team = {"enabled": True, "objective": "Current objective", "status": "running", "pending_count": 0,
                "revision": 3}
        projected = task_to_dict(task, team=team)
        summary = _task_summary(task, version=3, latest_seq=3, conversation_count=0, team=team)
        self.assertEqual(projected["status"], "running")
        self.assertEqual(summary["status"], "running")
        self.assertEqual(summary["goal"], "Current objective")
        self.assertIsNone(projected["work"])
        self.assertEqual(task.status, "done")
        self.assertEqual(task.work["delivery"]["status"], "accepted")


if __name__ == "__main__":
    unittest.main()
