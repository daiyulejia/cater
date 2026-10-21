"""Scope polling contracts using real in-memory events, without models or files."""

from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from cater.acp import AcpPromptResult
from cater.events import Actor
from cater.external_agent import ExternalAgentState
from cater.runtime import Runtime, load_runtime_config
from cater.team import HOST
from cater.work import WorkConflict


class _OfflineRun:
    def __init__(self, driver, **kwargs):
        self.driver = driver
        self.check_current = kwargs["check_current"]
        self.state = ExternalAgentState()

    def run(self, prompt):
        self.state = replace(self.state, phase="prompting", prompt_attempted=True,
                             session_id="offline-scope-poll")
        try:
            self.driver.action(self, prompt)
            self.state = replace(self.state, phase="finished", stop_reason="end_turn")
            return AcpPromptResult("", "end_turn")
        except AssertionError as error:
            self.driver.assertions.append(error)
            raise
        finally:
            self.state = replace(self.state, cleanup_complete=True)


class _OfflineDriver:
    name = "scope_polling_fixture"
    command = ["offline-never-launched"]
    model = "offline"
    execution_profile = "qoder-managed-v1"
    timeout = 180
    max_run_seconds = 540
    closed = False

    def __init__(self):
        self.action = lambda run, prompt: None
        self.runs = []
        self.assertions = []

    def agent_run(self, **kwargs):
        run = _OfflineRun(self, **kwargs)
        self.runs.append(run)
        return run

    def tool_turn(self, **kwargs):
        raise AssertionError("Scope polling must not call a model")


class _ScopeFixture(unittest.TestCase):
    def setUp(self):
        config = load_runtime_config()
        self.driver = _OfflineDriver()
        config = replace(config,
            organization=replace(config.organization,
                leader=replace(config.organization.leader, driver=self.driver.name)),
            roles=[replace(role, driver=self.driver.name) for role in config.roles])
        with patch("cater.runtime.load_runtime_config", return_value=config):
            self.rt = Runtime(":memory:", drivers={self.driver.name: self.driver})
        self.addCleanup(self.rt.close)
        self.team = self.rt.team
        self.task = self.new_task()
        self.leader = self.rt.organization.leader
        self.worker = self.rt.worker_roles[0]
        self.clock = SimpleNamespace(monotonic=lambda: self.now)
        self.now = 100.0
        self.enterContext(patch("cater.team_external.time", self.clock))
        process = self.enterContext(patch("subprocess.Popen", side_effect=AssertionError(
            "Scope polling must not launch a process")))
        self.addCleanup(process.assert_not_called)
        # Replace only the OS launch-directory boundary; all scope checks and
        # external run receipts still use the real TeamExternal implementation.
        self.scratch = SimpleNamespace(path=str(Path(__file__).resolve().parent),
                                       close=lambda **kwargs: None)
        self.enterContext(patch("cater.team_external.LaunchDirectory", return_value=self.scratch))

    def new_task(self):
        task = self.rt.create_task("Offline scope polling regression")
        self.rt.team.enable(task)
        return task

    def emit(self, kind, payload=None, *, actor=HOST, task=None):
        return self.rt.log.emit(type=kind, actor=actor, task_id=task or self.task,
                                payload=payload or {})

    def objective(self, *, completed=False, task=None, actor=HOST):
        return self.emit("team.objective_completed" if completed else "team.objective_updated",
                         {"goal": "Adopted scope", "summary": "Completed scope"},
                         task=task, actor=actor)

    def claim(self, *, worker=False, task=None):
        task = task or self.task
        if worker:
            self.team._send(task, self.leader, self.worker.id, "Inspect only", "assignment")
        else:
            self.team.receive(task, "Inspect the current scope")
        claimed = self.team._claim(task)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed[1].id, self.worker.id if worker else self.leader.id)
        return claimed

    def poll_worker(self, claimed):
        _, role, started = claimed
        return self.team._current(self.task, started, role, started.payload["revision"])

    def external(self, action, *, task=None):
        task = task or self.task
        self.driver.closed = False
        self.now = 100.0
        self.driver.action = action
        message, role, started = self.claim(task=task)
        result = self.team.external.run_message(task, message, role, started,
            started.payload["revision"], None, self.driver, "Offline scope fixture.\n")
        self.assertEqual(self.driver.assertions, [], "An assertion inside the callback was caught by the host")
        self.assertTrue(self.driver.runs[-1].state.cleanup_complete)
        self.assertEqual(list(self.rt.log.iter_events(task_id=task,
            event_types=("team.tool_started",))), [])
        return result


class TeamRevisionProjectionTests(_ScopeFixture):
    def test_zero_revision_before_an_objective_matches_full_view(self):
        self.assertEqual(self.team.view(self.task)["revision"], 0)
        self.assertEqual(self.team._revision(self.task), 0)

    def test_live_updates_completion_and_reopening_match_full_view(self):
        for completed in (False, False, True, False, True):
            with self.subTest(completed=completed):
                event = self.objective(completed=completed)
                full = self.team.view(self.task)
                self.assertEqual(full["revision"], event.seq)
                self.assertEqual(full["status"], "done" if completed else "running")
                self.assertEqual(self.team._revision(self.task), full["revision"])
                self.assertEqual(self.team._revision(self.task), event.seq)

    def test_other_tasks_and_nonhost_actors_cannot_change_revision(self):
        current = self.objective()
        foreign = self.new_task()
        actors = (Actor.human(), Actor.agent("team", "team"), Actor.system("other"),
                  Actor(id="team", role="other", kind="system"),
                  Actor(id="other", role="team", kind="system"))
        for completed in (False, True):
            self.objective(completed=completed, task=foreign)
            for actor in actors:
                self.objective(completed=completed, actor=actor)
        self.emit("team.run_finished", {"message_seq": 999, "status": "interrupted",
                                       "revision": 999999})
        self.assertEqual(self.team.view(self.task)["revision"], current.seq)
        self.assertEqual(self.team._revision(self.task), current.seq)
        self.assertEqual(self.team._revision(foreign), self.team.view(foreign)["revision"])

    def test_projection_reads_only_objective_events_without_full_views(self):
        current = self.objective(completed=True)
        with patch.object(self.team, "view", side_effect=AssertionError("Full team view")), patch.object(
                self.rt, "task", side_effect=AssertionError("Full task fold")), patch.object(
                self.rt.log, "read", side_effect=AssertionError("Full event read")), patch.object(
                self.rt.log, "iter_events", wraps=self.rt.log.iter_events) as selected:
            self.assertEqual(self.team._revision(self.task), current.seq)
        self.assertTrue(selected.called)
        for call in selected.call_args_list:
            self.assertEqual(call.kwargs.get("task_id"), self.task)
            self.assertEqual(set(call.kwargs.get("event_types", ())),
                             {"team.objective_updated", "team.objective_completed"})


class TeamNativeScopePollingTests(_ScopeFixture):
    def test_current_worker_poll_does_not_rebuild_full_view(self):
        self.objective()
        claimed = self.claim(worker=True)
        before = self.rt.log.read(task_id=self.task)
        with patch.object(self.team, "view", wraps=self.team.view) as full:
            self.poll_worker(claimed)
            self.poll_worker(claimed)
        full.assert_not_called()
        self.assertEqual(self.rt.log.read(task_id=self.task), before)

    def test_worker_stops_after_real_objective_update_or_completion(self):
        for completed in (False, True):
            with self.subTest(completed=completed):
                task = self.new_task()
                self.objective(task=task)
                _, role, started = self.claim(worker=True, task=task)
                self.team._current(task, started, role, started.payload["revision"])
                self.objective(task=task, completed=completed)
                with self.assertRaises(WorkConflict):
                    self.team._current(task, started, role, started.payload["revision"])

    def test_pause_cancel_and_pause_resume_invalidate_worker_control(self):
        for action in ("pause", "cancel", "pause-resume"):
            with self.subTest(action=action):
                task = self.new_task()
                _, role, started = self.claim(worker=True, task=task)
                self.rt.execution_control.change(task, "pause" if action == "pause-resume" else action,
                                                 request_id="scope-" + action)
                if action == "pause-resume":
                    self.rt.execution_control.change(task, "resume", request_id="scope-resumed")
                with self.assertRaises(WorkConflict):
                    self.team._current(task, started, role, started.payload["revision"])

    def test_new_customer_input_and_removal_still_stop_the_worker(self):
        for change in ("message", "remove"):
            with self.subTest(change=change):
                task = self.new_task()
                _, role, started = self.claim(worker=True, task=task)
                if change == "message":
                    self.team.receive(task, "@" + role.name + " Stop the old request")
                else:
                    self.rt.conversation_list.remove(task, request_id="scope-remove")
                with self.assertRaises(WorkConflict):
                    self.team._current(task, started, role, started.payload["revision"])

    def test_leader_can_adopt_a_new_objective_in_its_current_run(self):
        _, role, started = self.claim()
        self.objective()
        self.team._current(self.task, started, role, started.payload["revision"])


class TeamExternalScopePollingTests(_ScopeFixture):
    def test_external_callback_preserves_throttle_without_rebuilding_full_view(self):
        self.objective()

        def action(run, prompt):
            with patch.object(self.team, "view", wraps=self.team.view) as full, patch.object(
                    self.team, "_current", wraps=self.team._current) as current, patch.object(
                    self.rt.workspaces, "view", wraps=self.rt.workspaces.view) as workspace, patch.object(
                    self.rt.rulebook, "current_snapshot", wraps=self.rt.rulebook.current_snapshot) as rules:
                for now in (100.25, 100.30, 100.50, 100.55):
                    self.now = now
                    run.check_current()
                self.assertEqual(current.call_count, 2)
                self.assertEqual(workspace.call_count, 2)
                self.assertEqual(rules.call_count, 2)
                full.assert_not_called()

        self.assertEqual(self.external(action)["status"], "completed")

    def test_context_assembly_still_uses_full_view(self):
        event = self.objective()

        def action(run, prompt):
            context = json.loads(prompt.rsplit("\n", 1)[-1])
            self.assertEqual(context["current_work"]["revision"], event.seq)
            self.assertEqual(context["current_work"]["objective"], event.payload["goal"])
            self.now = 100.25
            run.check_current()

        with patch.object(self.team, "view", wraps=self.team.view) as full:
            self.assertEqual(self.external(action)["status"], "completed")
        self.assertTrue(full.called)

    def test_external_callback_detects_live_scope_changes(self):
        for change in ("objective", "completed", "pause", "cancel", "pause-resume",
                       "message", "remove", "rules", "workspace", "driver-close", "driver-config"):
            with self.subTest(change=change):
                task = self.new_task()

                def action(run, prompt):
                    self.now = 100.25
                    run.check_current()
                    if change in {"objective", "completed"}:
                        self.objective(completed=change == "completed", task=task)
                    elif change in {"pause", "cancel", "pause-resume"}:
                        self.rt.execution_control.change(task,
                            "pause" if change == "pause-resume" else change, request_id="external-" + change)
                        if change == "pause-resume":
                            self.rt.execution_control.change(task, "resume", request_id="external-resumed")
                    elif change == "message":
                        self.team.receive(task, "Changed customer input")
                    elif change == "remove":
                        self.rt.conversation_list.remove(task, request_id="external-removed")
                    elif change == "rules":
                        self.rt.rulebook.record("Discuss only", scope="task", task_id=task)
                    elif change == "workspace":
                        self.rt.workspaces.bind(task, None, expected_revision=None)
                    elif change == "driver-close":
                        self.driver.closed = True
                    else:
                        self.driver.model = "changed-offline-model"
                    self.now = 100.50
                    with self.assertRaises(WorkConflict):
                        run.check_current()

                self.assertEqual(self.external(action, task=task)["status"], "interrupted")

    def test_each_workspace_permission_and_directory_field_remains_checked(self):
        for key in ("available", "workspace_id", "revision", "fingerprint", "root"):
            with self.subTest(key=key):
                task = self.new_task()

                def action(run, prompt):
                    original = self.rt.workspaces.view

                    def changed(task_id):
                        value = original(task_id)
                        return {**value, key: True if key == "available" else "changed-scope"}

                    self.now = 100.25
                    with patch.object(self.rt.workspaces, "view", side_effect=changed):
                        with self.assertRaises(WorkConflict):
                            run.check_current()

                # The injected projection is restored before final settlement.
                self.assertEqual(self.external(action, task=task)["status"], "completed")


class TeamScopeUnknownEffectsTests(_ScopeFixture):
    def test_polling_cannot_settle_or_authorize_replay_of_unknown_effects(self):
        message, role, old_run = self.claim()
        effect = self.emit("team.tool_started", {
            "message_seq": message.seq, "role": role.id, "run_id": old_run.payload["run_id"],
            "name": "workspace_run", "call_id": "offline-unknown-effect", "arguments": {}})
        self.emit("team.tool_finished", {
            "started_seq": effect.seq, "role": role.id, "run_id": old_run.payload["run_id"],
            "name": "workspace_run", "call_id": "offline-unknown-effect",
            "result": {"status": "error", "complete": False, "outcome": "effects_unknown"}})
        self.emit("team.run_finished", {**old_run.payload, "status": "failed"})
        _, worker, worker_run = self.claim(worker=True)
        pending = self.team._pending_operations(self.task)
        unknown = next(row for row in pending if row["started_seq"] == effect.seq)
        self.assertEqual(unknown["outcome"], "unknown")
        self.assertFalse(unknown["replay_authorized"])
        before = self.rt.log.read(task_id=self.task)
        self.team._current(self.task, worker_run, worker, worker_run.payload["revision"])
        self.assertEqual(self.rt.log.read(task_id=self.task), before)
        self.assertEqual(self.team._pending_operations(self.task), pending)

        def action(run, prompt):
            before_poll = self.rt.log.read(task_id=self.task)
            self.now = 100.25
            run.check_current()
            self.assertEqual(self.rt.log.read(task_id=self.task), before_poll)
            rows = self.team._pending_operations(self.task)
            self.assertEqual(next(row for row in rows if row["started_seq"] == effect.seq), unknown)

        self.driver.action = action
        new_message, manager, started = self.claim()
        result = self.team.external.run_message(self.task, new_message, manager, started,
            started.payload["revision"], None, self.driver, "Offline unknown-effects fixture.\n")
        self.assertEqual(self.driver.assertions, [])
        self.assertEqual(result["status"], "completed")
        tool_start = self.emit("team.tool_started", {**started.payload, "name": "finish_work"})
        completion = self.team._finish_work(self.task, manager,
            {"summary": "Must remain blocked", "verification": "Offline only"}, tool_start)
        self.assertEqual(completion["code"], "operation_pending")
        self.assertIn(effect.seq, completion["started_seqs"])
        self.assertEqual(list(self.rt.log.iter_events(task_id=self.task,
            event_types=("team.objective_completed",))), [])
        self.assertFalse(next(row for row in self.team._pending_operations(self.task)
                              if row["started_seq"] == effect.seq)["replay_authorized"])


if __name__ == "__main__":
    unittest.main()
