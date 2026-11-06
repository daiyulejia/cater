"""Workspace orchestration contracts; all model and tool execution is fake.

Only temporary databases/configuration are used. Failing recovery tests express
the required behavior, rather than accepting an unsafe current transition.
"""

from __future__ import annotations

import copy
from datetime import datetime
from dataclasses import replace
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from cater.drivers import DriverResponse, DriverUnavailable, OpenAICompatibleDriver, TextTransportFailure
from cater.events import Actor
from cater.runtime import Runtime, _TEXT_TRANSPORT_FAILURE
from cater.tool_protocol import ToolCall, ToolTurn
from cater.work import WorkConflict
from cater.workspace_tools import WorkspaceExecutor as RealWorkspaceExecutor
from cater.workspaces import HOST, load_workspaces, WorkspaceBudgetStopped, WorkspaceEffectsUncertain, WorkspaceStopped


def final_text(**changes):
    result = {"stance": "assert", "claim": "Observed result", "text": "Observed result",
              "deliverables": [{"title": "Result", "summary": "Observed result",
                                "content": "Observed result.\n", "format": "text", "filename": "result.txt"}]}
    result.update(changes)
    return json.dumps(result)


def turn(*calls, text="", tokens=5):
    return ToolTurn(text=text, calls=list(calls), tokens=tokens,
                    meta={"token_usage": "reported"},
                    message={"role": "assistant", "content": text, "tool_calls": [
                        {"id": c.id, "name": c.name, "arguments": c.arguments} for c in calls]})


def command_result():
    empty = {"bytes": 0, "sha256": hashlib.sha256(b"").hexdigest(), "complete": True,
             "truncated": False, "excerpt": ""}
    return {"status": "ok", "command": "check", "cwd": ".", "outcome": "passed", "exit_code": 0,
            "timed_out": False, "duration_ms": 1, "stdout": dict(empty), "stderr": dict(empty),
            "bytes": 0, "truncated": False, "complete": True, "process_scope": "fake", "output": ""}


class FakeDriver:
    def __init__(self):
        self.turns = []
        self.tool_requests = []
        self.text_requests = []
        self.after_turn = None
        self.token_budget = None

    def tool_turn(self, **request):
        self.tool_requests.append(copy.deepcopy(request))
        if not self.turns:
            raise AssertionError("Unexpected model tool turn")
        response = self.turns.pop(0)
        if self.after_turn:
            self.after_turn(len(self.tool_requests))
        if isinstance(response, BaseException):
            raise response
        return response

    def complete(self, **request):
        self.text_requests.append(copy.deepcopy(request))
        return DriverResponse(final_text(), 3, {"token_usage": "reported"})


class FakeExecutor:
    def __init__(self, runtime):
        self.runtime = runtime
        self.calls = []
        self.after_execute = None
        self.failure = None
        self.result = {"status": "ok", "content": "Observed result.\n", "truncated": False}
        self.start_visible_from_other_connection = []

    def schemas(self):
        return [{"name": name, "description": "Fake tool", "input_schema": {"type": "object"}}
                for name in ("workspace_read", "workspace_write", "workspace_run")]

    def execute(self, name, arguments):
        self.calls.append((name, copy.deepcopy(arguments)))
        with closing(sqlite3.connect(self.runtime.log.path)) as observer:
            count = observer.execute("SELECT count(*) FROM events WHERE type = ?",
                                     ("workspace.tool_started",)).fetchone()[0]
        self.start_visible_from_other_connection.append(count)
        if self.after_execute:
            self.after_execute()
        if self.failure:
            raise self.failure
        return copy.deepcopy(self.result)


class SimulatedCrash(BaseException):
    pass


class WorkspaceIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="cater-workspace-integration-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.root = self.directory / "source"
        self.root.mkdir()
        self.db = self.directory / "test.db"
        self.config = self.directory / "workspaces.json"
        self.write_config()
        self.driver = FakeDriver()
        self.rt = self.open_runtime()
        self.task = self.rt.create_task("Inspect the bound source")
        self.role = self.rt._role_by_id("dev")
        self.executor = FakeExecutor(self.rt)
        executor_patch = patch("cater.workspace_tools.WorkspaceExecutor", return_value=self.executor)
        executor_patch.start()
        self.addCleanup(executor_patch.stop)

    def write_config(self, *, mode="edit", commands=None, checks=None):
        row = {"id": "source", "title": "Source", "root": str(self.root), "mode": mode}
        if commands is not None:
            row["commands"] = commands
        if checks is not None:
            row["checks"] = checks
        self.config.write_text(json.dumps({"workspaces": [row]}), encoding="utf-8")

    def open_runtime(self):
        runtime = Runtime(self.db, drivers={"dev": self.driver, "test": self.driver},
                          workspaces_path=self.config)
        self.addCleanup(runtime.close)
        return runtime

    def bind(self):
        return self.rt.workspaces.bind(self.task, "source", expected_revision=None)

    def plan(self):
        return self.rt.plan_work(self.task, [{"id": "source", "owner": "dev", "reviewer": "test",
            "objective": "Inspect the bound source", "depends_on": [],
            "criteria": [{"id": "meaning", "kind": "review", "text": "Inspect the actual result"}]}])

    def claim(self):
        self.plan()
        claim = self.rt.work.claim(self.task, "source")
        item = self.rt.work.view(self.task)["items"][0]
        return claim, {**claim.payload, "contract": item, "accepted_inputs": {}, "customer_directions": []}

    def gateway(self, context):
        return self.rt._complete_with_containment(task_id=self.task, task=self.rt.task(self.task),
            role=self.role, driver=self.rt.drivers["dev"], prompt="Inspect source", work_context=context)

    def service(self, context, *, runtime=None):
        runtime = runtime or self.rt
        return runtime.workspaces.complete(task_id=self.task, role=self.role, driver=self.driver,
                                           prompt="Inspect source", work_context=context)

    def events(self, kind):
        return list(self.rt.log.iter_events(task_id=self.task, event_types=(kind,)))

    def prepare_call(self):
        self.bind()
        return self.claim()

    def start_unknown(self, context):
        return self.rt.log.emit(type="workspace.tool_started", task_id=self.task, actor=HOST,
            payload={**{k: context[k] for k in ("attempt_id", "work_item_id", "plan_seq", "scope")},
                     "root": self.rt.workspaces.view(self.task)["root"],
                     "workspace_revision": self.rt.workspaces.view(self.task)["revision"],
                     "role": "dev", "name": "workspace_run", "call_id": "interrupted-call",
                     "arguments": {"command": "check"}})

    def test_binding_changes_scope_and_invalidates_existing_plan(self):
        self.plan()
        old = self.rt.work.scope(self.task)
        binding = self.bind()
        self.assertNotEqual(self.rt.work.scope(self.task), old)
        self.assertEqual(self.rt.work.scope(self.task)["workspace"]["revision"], binding["revision"])
        self.assertFalse(self.rt.work.view(self.task)["scope_current"])

    def test_configuration_change_invalidates_scope_after_reopen(self):
        self.bind()
        self.plan()
        scope = self.rt.work.scope(self.task)
        self.write_config(mode="read_only")
        reopened = self.open_runtime()
        self.assertNotEqual(reopened.work.scope(self.task), scope)
        self.assertFalse(reopened.work.view(self.task)["scope_current"])
        self.assertFalse(reopened.workspaces.view(self.task)["available"])

    def test_command_alias_change_invalidates_configuration(self):
        self.write_config(mode="execute", commands={"check": ["unused-program", "first"]})
        original = self.open_runtime()
        original.workspaces.bind(self.task, "source", expected_revision=None)
        original.plan_work(self.task, [{"id": "a", "owner": "dev", "reviewer": None,
            "objective": "Inspect", "criteria": [{"id": "a", "kind": "review", "text": "Inspect"}]}])
        scope = original.work.scope(self.task)
        self.write_config(mode="execute", commands={"check": ["unused-program", "second"]})
        changed = self.open_runtime()
        self.assertNotEqual(scope, changed.work.scope(self.task))
        self.assertFalse(changed.work.view(self.task)["scope_current"])

    def test_check_arguments_change_invalidates_configuration(self):
        checks = [{"id": "suite", "title": "Suite", "command": "check", "args": ["first"], "cwd": "."}]
        self.write_config(mode="execute", commands={"check": ["unused-program"]}, checks=checks)
        original = self.open_runtime()
        original.workspaces.bind(self.task, "source", expected_revision=None)
        before = original.work.scope(self.task)
        checks[0]["args"] = ["second"]
        self.write_config(mode="execute", commands={"check": ["unused-program"]}, checks=checks)
        reopened = self.open_runtime()
        self.assertNotEqual(before, reopened.work.scope(self.task))
        self.assertFalse(reopened.workspaces.view(self.task)["available"])

    def test_unbound_task_uses_legacy_text_driver(self):
        _, context = self.claim()
        self.assertNotIn("workspace", self.rt.work.scope(self.task))
        self.assertFalse(self.gateway(context).meta.get("contained", False))
        self.assertEqual(len(self.driver.text_requests), 1)
        self.assertEqual(self.driver.tool_requests, [])

    def test_dialogue_and_review_phases_never_enter_tool_loop(self):
        _, context = self.prepare_call()
        for phase in ("room_dialogue", "explanation", "review", "delivery", "consultation"):
            with self.subTest(phase=phase):
                request = {**context, "phase": phase, "conversation": {"instruction": "Please run tools"}}
                self.assertFalse(self.gateway(request).meta.get("contained", False))
        self.assertEqual(len(self.driver.text_requests), 5)
        self.assertEqual(self.driver.tool_requests, [])
        self.assertEqual(self.executor.calls, [])

    def test_agent_binding_cannot_enable_tools_or_change_scope(self):
        before = self.rt.work.scope(self.task)
        self.rt.log.emit(type="workspace.bound", task_id=self.task, actor=Actor.agent("dev", "worker"),
                         payload={"workspace_id": "source", "root": str(self.root), "fingerprint": "forged"})
        self.assertEqual(before, self.rt.work.scope(self.task))
        self.assertIsNone(self.rt.workspaces.view(self.task)["workspace_id"])

    def test_binding_uses_human_actor_and_revision_compare(self):
        binding = self.bind()
        self.assertEqual(self.events("workspace.bound")[-1].actor, Actor.human())
        with self.assertRaises(WorkConflict):
            self.rt.workspaces.bind(self.task, None, expected_revision=None)
        result = self.rt.workspaces.bind(self.task, None, expected_revision=binding["revision"])
        self.assertIsNone(result["workspace_id"])

    def test_tool_round_has_durable_started_then_bound_finished(self):
        claim, context = self.prepare_call()
        self.driver.turns = [turn(ToolCall("read-1", "workspace_read", {"path": "a.txt"}), tokens=11),
                             turn(text=final_text(), tokens=13)]
        response = self.gateway(context)
        self.assertFalse(response.meta.get("contained", False))
        started, finished = self.events("workspace.tool_started")[0], self.events("workspace.tool_finished")[0]
        self.assertLess(started.seq, finished.seq)
        self.assertEqual(started.actor, HOST)
        self.assertEqual(finished.actor, HOST)
        self.assertEqual(finished.payload["started_seq"], started.seq)
        self.assertEqual(started.payload["attempt_id"], claim.payload["attempt_id"])
        self.assertEqual(started.payload["scope"], context["scope"])
        self.assertEqual(self.executor.start_visible_from_other_connection, [1])
        self.assertEqual(self.driver.tool_requests[1]["history"][-1]["tool_call_id"], "read-1")
        self.assertFalse(self.rt.workspaces.pending(task_id=self.task))

    def test_tool_usage_is_counted_once_after_visible_message(self):
        _, context = self.prepare_call()
        self.driver.turns = [turn(ToolCall("read-1", "workspace_read", {"path": "a.txt"}), tokens=11),
                             turn(text=final_text(), tokens=13)]
        outputs = self.rt._collect_utterances(self.task, self.rt.task(self.task), [self.role], {}, work_context=context)
        self.assertFalse(outputs[0]["contained"])
        self.assertEqual(self.rt._reported_driver_tokens("dev"), 24)
        self.assertEqual(sum(e.payload["tokens"] for e in self.events("workspace.model_called")), 24)
        self.assertEqual(outputs[0]["event"].payload["tokens"], 0)

    def test_failed_tool_result_is_preserved_in_receipt_and_model_history(self):
        _, context = self.prepare_call()
        self.executor.result = {"status": "error", "code": "conflict", "message": "Changed externally"}
        self.driver.turns = [turn(ToolCall("write-1", "workspace_write", {"path": "a.txt"})),
                             turn(text=final_text())]
        self.gateway(context)
        result = self.events("workspace.tool_finished")[0].payload["result"]
        self.assertEqual(result, self.executor.result)
        message = self.driver.tool_requests[1]["history"][-1]
        self.assertTrue(message["is_error"])
        self.assertEqual(json.loads(message["content"])["code"], "conflict")

    def test_pause_during_model_turn_starts_no_tool(self):
        _, context = self.prepare_call()
        self.driver.turns = [turn(ToolCall("write-1", "workspace_write", {"path": "a.txt"}))]
        self.driver.after_turn = lambda _: self.rt.execution_control.change(self.task, "pause", request_id="pause-model-turn")
        response = self.gateway(context)
        self.assertTrue(response.meta["contained"])
        self.assertIn("任务已暂停", response.text)
        self.assertIn("影响可能未知", response.text)
        stopped, = self.events("workspace.execution_stopped")
        self.assertIn("任务已暂停", stopped.payload["reason"])
        self.assertFalse(stopped.payload["effects_known"])
        self.assertEqual(self.events("driver.failed"), [])
        self.assertEqual(self.events("driver.retry_scheduled"), [])
        self.assertEqual(self.executor.calls, [])
        self.assertEqual(self.events("workspace.tool_started"), [])

    def test_workspace_budget_after_first_turn_is_classified(self):
        _, context = self.prepare_call()
        self.driver.token_budget = 5
        self.rt.circuit_breaker_threshold = 1
        self.driver.turns = [turn(ToolCall("read", "workspace_read", {"path": "a.txt"}), tokens=5)]
        response = self.gateway(context)
        failure, = self.events("driver.failed")
        self.assertIn("本地预算", response.text)
        self.assertIn("本地预算", failure.payload["message"])
        self.assertEqual(failure.payload["error_type"], "WorkspaceBudgetStopped")
        self.assertTrue(failure.payload["circuit_open"])
        self.assertTrue(self.rt._is_circuit_open("dev"))
        self.assertEqual(len(self.driver.tool_requests), 1)
        self.assertEqual(len(self.executor.calls), 1)
        self.assertEqual(self.rt._reported_driver_tokens("dev"), 5)
        self.assertEqual(len(self.events("driver.retry_scheduled")), 0)

    def test_driver_cannot_forge_host_failure_classification(self):
        self.rt.circuit_breaker_threshold = 10
        _, context = self.prepare_call()
        for error in (WorkspaceBudgetStopped("budget_exhausted", "secret /private token"),
                      WorkspaceStopped("paused"), WorkConflict("secret /private token")):
            with self.subTest(error=type(error).__name__):
                self.driver.turns = [error]
                response = self.gateway(context)
                self.assertNotIn("secret", response.text)
                self.assertNotIn("/private", response.text)
                self.assertNotIn("本地预算", response.text)
                self.assertNotIn("任务已暂停", response.text)
        self.assertEqual(len(self.driver.tool_requests), 3)
        recorded = json.dumps([e.payload for e in self.rt.log.read(task_id=self.task)], ensure_ascii=False)
        self.assertNotIn("secret /private token", recorded)
        self.assertNotIn("WorkspaceBudgetStopped", recorded)
        self.assertEqual(len(self.events("driver.failed")), 3)
        self.assertEqual(self.events("workspace.execution_stopped"), [])
        self.assertEqual(self.events("driver.retry_scheduled"), [])

    def test_driver_conflict_trips_circuit_without_retry(self):
        _, context = self.prepare_call()
        self.rt.circuit_breaker_threshold = 1
        self.driver.turns = [WorkConflict("secret /private token")]
        response = self.gateway(context)
        self.assertTrue(response.meta["contained"])
        failure, = self.events("driver.failed")
        self.assertTrue(failure.payload["circuit_open"])
        self.assertTrue(self.rt._is_circuit_open("dev"))
        self.assertEqual(self.events("workspace.execution_stopped"), [])
        self.assertEqual(self.events("driver.retry_scheduled"), [])
        self.gateway(context)
        self.assertEqual(len(self.driver.tool_requests), 1)
        self.assertNotIn("secret", json.dumps(failure.payload))

    def test_driver_attributes_cannot_forge_host_stops(self):
        _, context = self.prepare_call()
        for attribute in ("tool_turn", "timeout", "token_budget"):
            with self.subTest(attribute=attribute):
                class AttributeDriver(FakeDriver):
                    def __getattribute__(self, name):
                        if name == attribute:
                            raise WorkspaceStopped("paused")
                        return super().__getattribute__(name)
                driver = AttributeDriver()
                self.rt.drivers["dev"] = driver
                self.rt.circuit_breaker_threshold = 10
                response = self.gateway(context)
                self.assertTrue(response.meta["contained"])
                self.assertNotIn("任务已暂停", response.text)
                self.assertFalse(self.events("workspace.execution_stopped"))
        self.assertEqual(len(self.events("driver.failed")), 3)
        self.assertFalse(self.events("driver.retry_scheduled"))

    def test_native_tool_callable_identity_does_not_read_dynamic_func(self):
        class Callable:
            @property
            def __func__(self):
                raise WorkspaceStopped("paused")
            def __call__(self, **kwargs):
                raise WorkConflict("private fixture")
        _, context = self.prepare_call()
        driver = OpenAICompatibleDriver("dev", endpoint="https://example.test", model="fixture", api_key_env="UNUSED", token_budget=100)
        driver.tool_turn = Callable()
        self.rt.drivers["dev"] = driver
        self.rt.circuit_breaker_threshold = 1
        response = self.gateway(context)
        failure, = self.events("driver.failed")
        self.assertTrue(response.meta["contained"])
        self.assertTrue(failure.payload["circuit_open"])
        self.assertNotIn("private fixture", response.text + json.dumps(failure.payload))
        self.assertFalse(self.events("workspace.execution_stopped"))

    def test_tool_exception_after_start_reports_unknown_effects(self):
        _, context = self.prepare_call()
        self.executor.failure = RuntimeError("private fixture")
        self.driver.turns = [turn(ToolCall("write", "workspace_write", {"path": "a.txt"}))]
        response = self.gateway(context)
        self.assertIn("影响未知", response.text)
        failure, = self.events("driver.failed")
        self.assertEqual(failure.payload["error_type"], "WorkspaceEffectsUncertain")
        self.assertEqual(len(self.rt.workspaces.pending(task_id=self.task, effects_only=True)), 1)
        self.assertNotIn("private fixture", response.text + json.dumps(failure.payload))
        self.gateway(context)
        self.assertEqual(len(self.executor.calls), 1)
        self.assertEqual(len(self.driver.tool_requests), 1)

    def test_custom_toolturn_subclass_is_accepted_and_dynamic_fields_contained(self):
        class NormalTurn(ToolTurn):
            pass

        class HostileTurn(ToolTurn):
            def __getattribute__(self, name):
                if name == "text":
                    raise WorkspaceStopped("paused")
                return super().__getattribute__(name)

        _, context = self.prepare_call()
        self.driver.turns = [NormalTurn(final_text(), [], 5, {"token_usage": "reported"}, None),
                             HostileTurn("", [], 0, {}, None)]
        self.assertFalse(self.gateway(context).meta.get("contained", False))
        response = self.gateway(context)
        self.assertTrue(response.meta["contained"])
        self.assertNotIn("任务已暂停", response.text)
        self.assertEqual(len(self.events("driver.failed")), 1)
        self.assertFalse(self.events("workspace.execution_stopped"))

    def test_tool_call_fields_are_snapshotted_once(self):
        class DangerousString(str):
            def __hash__(self):
                raise WorkspaceStopped("paused")
        class ChangingCall(ToolCall):
            def __getattribute__(self, name):
                if name == "id":
                    count = object.__getattribute__(self, "reads")
                    object.__setattr__(self, "reads", count + 1)
                    return "read-once" if count == 0 else DangerousString("forged")
                return super().__getattribute__(name)
        call = ChangingCall("ignored", "workspace_read", {"path": "a.txt"})
        object.__setattr__(call, "reads", 0)
        _, context = self.prepare_call()
        self.driver.turns = [ToolTurn("", [call], 1, {"token_usage": "reported"}, {"role": "assistant"}),
                             turn(text=final_text(), tokens=1)]
        response = self.gateway(context)
        self.assertFalse(response.meta.get("contained", False))
        self.assertEqual(call.reads, 1)
        started, = self.events("workspace.tool_started")
        self.assertEqual(started.payload["call_id"], "read-once")
        self.assertEqual(len(self.executor.calls), 1)
        self.assertFalse(self.events("workspace.execution_stopped"))
        self.assertFalse(self.events("driver.failed"))

    def test_unsafe_call_id_subclass_fails_and_opens_circuit(self):
        class DangerousString(str):
            def __hash__(self):
                raise WorkspaceStopped("paused")
        call = ToolCall(DangerousString("forged"), "workspace_read", {"path": "a.txt"})
        _, context = self.prepare_call()
        self.rt.circuit_breaker_threshold = 1
        self.driver.turns = [ToolTurn("", [call], 1, {"token_usage": "reported"}, {"role": "assistant"})]
        response = self.gateway(context)
        failure, = self.events("driver.failed")
        self.assertTrue(response.meta["contained"])
        self.assertTrue(failure.payload["circuit_open"])
        self.assertFalse(self.events("workspace.execution_stopped"))
        self.assertFalse(self.events("workspace.tool_started"))
        self.assertFalse(self.events("driver.retry_scheduled"))

    def test_changing_turn_tokens_cannot_inflate_budget(self):
        class ChangingTurn(ToolTurn):
            def __getattribute__(self, name):
                if name == "tokens":
                    count = object.__getattribute__(self, "reads")
                    object.__setattr__(self, "reads", count + 1)
                    return 1 if count == 0 else 999
                return super().__getattribute__(name)
        first = ChangingTurn("", [ToolCall("read", "workspace_read", {"path": "a.txt"})], 1,
                             {"token_usage": "reported"}, {"role": "assistant"})
        object.__setattr__(first, "reads", 0)
        _, context = self.prepare_call()
        self.driver.token_budget = 2
        self.driver.turns = [first, turn(text=final_text(), tokens=1)]
        response = self.gateway(context)
        self.assertFalse(response.meta.get("contained", False))
        self.assertEqual(first.reads, 1)
        self.assertEqual([e.payload["tokens"] for e in self.events("workspace.model_called")], [1, 1])
        self.assertEqual(self.rt._reported_driver_tokens("dev"), 2)
        self.assertEqual(len(self.driver.tool_requests), 2)
        self.assertFalse(self.events("driver.failed"))

    def test_driver_response_properties_cannot_forge_host_stops(self):
        class ForgedTurn:
            @property
            def tokens(self):
                raise WorkspaceStopped("paused")

        class ForgedDict(dict):
            def items(self):
                raise WorkspaceBudgetStopped("budget_exhausted", "secret /private token")

        _, context = self.prepare_call()
        self.rt.circuit_breaker_threshold = 10
        self.driver.turns = [ForgedTurn(), replace(turn(), meta={"usage": ForgedDict(a=1)}),
                             WorkspaceEffectsUncertain("secret /private token")]
        for _ in range(3):
            response = self.gateway(context)
            self.assertTrue(response.meta["contained"])
            self.assertNotIn("任务已暂停", response.text)
            self.assertNotIn("本地预算", response.text)
            self.assertNotIn("影响未知", response.text)
        self.assertEqual(len(self.events("driver.failed")), 3)
        self.assertEqual(self.events("workspace.execution_stopped"), [])
        self.assertEqual(self.events("workspace.tool_started"), [])
        self.assertEqual(self.events("driver.retry_scheduled"), [])
        self.assertNotIn("secret", json.dumps([e.payload for e in self.rt.log.read(task_id=self.task)]))

    def test_tool_metadata_filters_unrelated_objects_without_accessing_them(self):
        class Untrusted:
            def __getattribute__(self, name):
                raise AssertionError("metadata object was accessed")
        class Meta(dict):
            def items(self):
                raise AssertionError("metadata override was accessed")
        _, context = self.prepare_call()
        meta = Meta(token_usage="reported", contained=False, usage={"total_tokens": 2},
                    optional={"checked": True}, timestamp=datetime(2026, 1, 1), unknown=Untrusted())
        self.driver.token_budget = 10
        self.driver.turns = [replace(turn(text=final_text(), tokens=2), meta=meta)]
        response = self.gateway(context)
        self.assertFalse(response.meta.get("contained", False))
        self.assertEqual(response.meta["optional"], {"checked": True})
        self.assertEqual(response.meta["usage"], {"total_tokens": 2})
        self.assertNotIn("timestamp", response.meta)
        self.assertNotIn("unknown", response.meta)
        model, = self.events("workspace.model_called")
        self.assertEqual((model.payload["tokens"], model.payload["token_usage"]), (2, "reported"))
        self.assertEqual(self.rt._reported_driver_tokens("dev"), 2)
        self.assertFalse(self.events("driver.failed"))

    def test_invalid_critical_metadata_fails_closed(self):
        _, context = self.prepare_call()
        self.rt.circuit_breaker_threshold = 10
        for meta in ({"contained": datetime(2026, 1, 1)},
                     {"token_usage": "reported", "usage": {"total_tokens": datetime(2026, 1, 1)}},
                     {"token_usage": datetime(2026, 1, 1)}):
            self.driver.turns = [replace(turn(text=final_text()), meta=meta)]
            self.assertTrue(self.gateway(context).meta["contained"])
        self.assertEqual(len(self.events("driver.failed")), 3)
        calls = self.events("workspace.model_called")
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(e.payload["token_usage"] == "unavailable" and e.payload["tokens"] == 0 for e in calls))

    def test_metadata_string_subclass_keys_rejected_without_dynamic_methods(self):
        class Key(str):
            armed = False
            def __hash__(self):
                if Key.armed:
                    raise AssertionError("key hash accessed")
                return str.__hash__(self)
            def __eq__(self, other):
                if Key.armed:
                    raise AssertionError("key equality accessed")
                return str.__eq__(self, other)
            def __str__(self):
                raise AssertionError("key conversion accessed")
        _, context = self.prepare_call()
        self.rt.circuit_breaker_threshold = 10
        self.driver.token_budget = 100
        for name, value in (("contained", True), ("token_usage", "reported")):
            Key.armed = False
            meta = {Key(name): value}
            Key.armed = True
            call = ToolCall("write", "workspace_write", {"path": "a.txt"})
            self.driver.turns = [ToolTurn("", [call], 999, meta, {"role": "assistant"})]
            response = self.gateway(context)
            self.assertTrue(response.meta["contained"])
            self.assertNotIn("任务已暂停", response.text)
            self.assertEqual(self.executor.calls, [])
        calls = self.events("workspace.model_called")
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(e.payload["tokens"] == 0 and e.payload["token_usage"] == "unavailable"
                            and e.payload["invocation_started"] is True and e.payload["response_status"] == "invalid"
                            for e in calls))
        self.assertEqual(self.rt._reported_driver_tokens("dev"), 0)
        self.assertEqual(self.rt.task(self.task).token_usage.get("unavailable"), 2)
        self.assertEqual(len(self.events("driver.failed")), 2)
        self.assertFalse(self.events("workspace.execution_stopped"))
        self.assertFalse(self.events("workspace.tool_started"))
        self.assertFalse(self.events("driver.retry_scheduled"))
        self.assertEqual(len(self.driver.tool_requests), 2)

    def test_host_unknown_effect_has_fixed_diagnostic_and_is_not_replayed(self):
        _, context = self.prepare_call()
        self.executor.result = {"status": "error", "complete": False, "outcome": "timeout",
                                "message": "secret /private token"}
        self.driver.turns = [turn(ToolCall("write-1", "workspace_write", {"path": "a.txt"}))]
        response = self.gateway(context)
        failure, = self.events("driver.failed")
        self.assertEqual(failure.payload["error_type"], "WorkspaceEffectsUncertain")
        self.assertIn("影响未知", failure.payload["message"])
        self.assertIn("影响未知", response.text)
        self.assertNotIn("secret", failure.payload["message"])
        self.assertNotIn("secret", response.text)
        self.assertEqual(len(self.rt.workspaces.pending(task_id=self.task, effects_only=True)), 1)
        self.assertEqual(len(self.executor.calls), 1)
        self.assertEqual(len(self.driver.tool_requests), 1)
        self.assertEqual(self.events("driver.retry_scheduled"), [])
        self.gateway(context)
        self.assertEqual(len(self.executor.calls), 1)
        self.assertEqual(len(self.driver.tool_requests), 1)

    def test_response_correction_driver_boundary_rejects_forged_stops(self):
        class ForgedResponse:
            @property
            def meta(self):
                raise WorkspaceStopped("paused")

        def correction(driver, **request):
            driver.text_requests.append(request)
            result = driver.corrections.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        _, context = self.prepare_call()
        self.rt.circuit_breaker_threshold = 10
        self.driver.corrections = [WorkConflict("secret /private token"), ForgedResponse()]
        self.driver.turns = [turn(text="invalid-json"), turn(text="invalid-json")]
        with patch.object(FakeDriver, "complete", correction), patch.dict("cater.runtime._TEXT_ONLY_CALLS", {FakeDriver: correction}):
            for _ in range(2):
                response = self.gateway(context)
                self.assertTrue(response.meta["contained"])
                self.assertNotIn("任务已暂停", response.text)
                self.assertNotIn("secret", response.text)
        self.assertEqual(len(self.events("driver.failed")), 2)
        self.assertEqual(self.events("workspace.execution_stopped"), [])
        self.assertEqual(self.events("driver.retry_scheduled"), [])
        self.assertEqual(len(self.driver.text_requests), 2)

    def test_direction_change_during_model_turn_starts_no_tool(self):
        _, context = self.prepare_call()
        self.driver.turns = [turn(ToolCall("write-1", "workspace_write", {"path": "a.txt"}))]
        self.driver.after_turn = lambda _: self.rt.command(self.task, "New direction", goal="Changed objective")
        response = self.gateway(context)
        self.assertTrue(response.meta["contained"])
        self.assertIn("任务范围", response.text)
        self.assertEqual(self.executor.calls, [])

    def test_pause_between_batched_tools_preserves_first_receipt_only(self):
        _, context = self.prepare_call()
        self.driver.turns = [turn(ToolCall("first", "workspace_read", {"path": "a.txt"}),
                                  ToolCall("second", "workspace_write", {"path": "a.txt"}))]
        self.executor.after_execute = lambda: self.rt.execution_control.change(self.task, "pause", request_id="pause-between-tools")
        self.assertTrue(self.gateway(context).meta["contained"])
        self.assertEqual(len(self.executor.calls), 1)
        self.assertEqual(len(self.events("workspace.tool_finished")), 1)

    def test_pause_resume_invalidates_old_model_tool_batch(self):
        _, context = self.prepare_call()
        self.driver.turns = [turn(ToolCall("write-1", "workspace_write", {"path": "a.txt"})), turn(text=final_text())]

        def interrupt(index):
            if index == 1:
                self.rt.execution_control.change(self.task, "pause", request_id="pause-old-model")
                self.rt.execution_control.change(self.task, "resume", request_id="resume-new-owner")

        self.driver.after_turn = interrupt
        self.gateway(context)
        self.assertEqual(self.executor.calls, [], "Old tool plans must not survive a control version change")

    def test_unknown_started_survives_reopen_and_prevents_replay_and_binding(self):
        _, context = self.prepare_call()
        started = self.start_unknown(context)
        reopened = self.open_runtime()
        self.assertEqual([e.seq for e in reopened.workspaces.pending(task_id=self.task)], [started.seq])
        with self.assertRaises(WorkConflict):
            self.service(context, runtime=reopened)
        with self.assertRaises(WorkConflict):
            reopened.workspaces.bind(self.task, None, expected_revision=reopened.workspaces.view(self.task)["revision"])
        self.assertEqual(self.driver.tool_requests, [])
        self.assertEqual(self.executor.calls, [])

    def test_process_crash_keeps_started_without_finished(self):
        _, context = self.prepare_call()
        self.driver.turns = [turn(ToolCall("write-1", "workspace_write", {"path": "a.txt"}))]
        self.executor.failure = SimulatedCrash("after external effect")
        with self.assertRaises(SimulatedCrash):
            self.service(context)
        self.assertEqual(len(self.events("workspace.tool_started")), 1)
        self.assertEqual(self.events("workspace.tool_finished"), [])
        reopened = self.open_runtime()
        with self.assertRaises(WorkConflict):
            self.service(context, runtime=reopened)
        self.assertEqual(len(self.executor.calls), 1)

    def test_agent_finish_or_reconciliation_cannot_clear_unknown_started(self):
        _, context = self.prepare_call()
        started = self.start_unknown(context)
        for kind in ("workspace.tool_finished", "workspace.tool_reconciled"):
            self.rt.log.emit(type=kind, task_id=self.task, actor=Actor.agent("dev", "worker"),
                payload={"started_seq": started.seq, "call_id": started.payload["call_id"], "result": {"status": "ok"}})
        self.assertEqual([e.seq for e in self.rt.workspaces.pending(task_id=self.task)], [started.seq])

    def test_reconcile_is_human_and_does_not_invoke_driver_or_executor(self):
        _, context = self.prepare_call()
        started = self.start_unknown(context)
        receipt = self.rt.workspaces.reconcile(self.task, started.seq, reason="Operator inspected actual effects", invocation_stopped=True)
        self.assertEqual(receipt.actor, Actor.human())
        self.assertFalse(self.rt.workspaces.pending(task_id=self.task))
        self.assertEqual(self.driver.tool_requests, [])
        self.assertEqual(self.executor.calls, [])

    def test_reconcile_rejects_a_live_workspace_owner(self):
        _, context = self.prepare_call()
        started = self.start_unknown(context)
        with self.rt.workspaces._owner(str(self.root)) as owned:
            self.assertTrue(owned)
            with self.assertRaises(WorkConflict):
                self.rt.workspaces.reconcile(self.task, started.seq, reason="Still running", invocation_stopped=True)
        self.assertEqual(len(self.rt.workspaces.pending(task_id=self.task)), 1)

    def test_reconciled_tool_must_offer_an_exit_for_uncertain_work_claim(self):
        claim, context = self.prepare_call()
        started = self.start_unknown(context)
        self.rt.work.fail(self.task, claim, "Interrupted after tool start", uncertain=True)
        self.rt.workspaces.reconcile(self.task, started.seq, reason="Operator inspected actual effects", invocation_stopped=True)
        item = self.rt.work.view(self.task)["items"][0]
        self.assertFalse(item["execution_in_flight"], "Tool reconciliation must not strand its work attempt forever")

    def test_finished_with_unknown_effects_must_remain_pending(self):
        _, context = self.prepare_call()
        started = self.start_unknown(context)
        self.rt.log.emit(type="workspace.tool_finished", task_id=self.task, actor=HOST,
            payload={"started_seq": started.seq, "call_id": started.payload["call_id"],
                     "result": {"status": "error", "outcome": "process_failed", "invocation_started": True,
                                "complete": False, "error": {"code": "process_failed"}}})
        self.assertEqual([e.seq for e in self.rt.workspaces.pending(task_id=self.task)], [started.seq])

    def test_small_workspace_file_can_be_captured_by_actual_read_backend(self):
        self.prepare_call()
        (self.root / "small.txt").write_text("Actual source bytes.\n", encoding="utf-8")
        executor = RealWorkspaceExecutor(self.rt.workspaces.config["source"]["policy"])
        try:
            self.rt.workspaces._capture_files(executor, final_text(workspace_files=["small.txt"], deliverables=[]))
        except ValueError as exc:
            self.fail(f"A small valid file must be capturable through the real read contract: {exc}")

    def test_successful_backend_exit_is_a_passed_configured_suite(self):
        checks = [{"id": "suite", "title": "Suite", "command": "check", "args": [], "cwd": "."}]
        self.write_config(mode="execute", commands={"check": ["unused-program"]}, checks=checks)
        row = load_workspaces(self.config)["source"]
        self.executor.result = command_result()
        result = self.rt.workspace_checks._execute(row, {"src/main.py": "assert True\n"})
        self.assertEqual(result["outcome"], "passed")
        self.assertEqual(result["exit_code"], 0)
        self.assertTrue(result["artifacts_unchanged"])

    def test_pause_during_stage_suite_prevents_next_configured_command(self):
        checks = [{"id": name, "title": name, "command": "check", "args": [name], "cwd": "."}
                  for name in ("first", "second")]
        self.write_config(mode="execute", commands={"check": ["unused-program"]}, checks=checks)
        self.rt.workspaces.config = load_workspaces(self.config)
        self.bind()
        self.rt.plan_work(self.task, [{"id": "source", "owner": "dev", "reviewer": "test",
            "objective": "Check source behavior", "criteria": [
                {"id": "behavior", "kind": "local_check", "text": "Check source behavior"}]}])
        claim = self.rt.work.claim(self.task, "source")
        item = self.rt.work.view(self.task)["items"][0]
        context = {**claim.payload, "contract": item, "accepted_inputs": {}, "customer_directions": []}
        self.driver.turns = [turn(text=final_text(workspace_files=["src/main.py"], deliverables=[]))]
        outputs = self.rt._collect_utterances(self.task, self.rt.task(self.task), [self.role], {}, work_context=context)
        self.rt.work.submit(self.task, claim, outputs[0]["artifact_seqs"])
        submitted = self.rt.work.view(self.task)["items"][0]
        self.executor.calls.clear()
        self.executor.result = command_result()
        self.executor.after_execute = lambda: self.rt.execution_control.change(
            self.task, "pause", request_id="pause-during-suite")
        try:
            self.rt.workspace_checks.run(self.task, submitted)
        except WorkConflict:
            pass
        self.assertEqual(len(self.executor.calls), 1, "A paused suite must not start its next command")

    def test_outer_transaction_cannot_execute_before_started_commit(self):
        _, context = self.prepare_call()
        self.driver.turns = [turn(ToolCall("write-1", "workspace_write", {"path": "a.txt"})), turn(text=final_text())]
        with self.rt.log.atomic():
            with self.assertRaises(WorkConflict):
                self.service(context)
        self.assertEqual(self.executor.calls, [])

    def test_nested_transaction_gateway_never_dispatches_tool(self):
        _, context = self.prepare_call()
        self.driver.turns = [turn(ToolCall("write-1", "workspace_write", {"path": "a.txt"})), turn(text=final_text())]
        with self.rt.log.atomic():
            with self.rt.log.atomic():
                response = self.gateway(context)
        self.assertTrue(response.meta.get("contained"))
        self.assertEqual(self.executor.calls, [])
        self.assertEqual(self.driver.tool_requests, [])

    def test_timeout_unknown_receipt_does_not_authorize_replay(self):
        _, context = self.prepare_call()
        started = self.start_unknown(context)
        result = command_result()
        result.update(status="error", outcome="timeout", exit_code=None, timed_out=True,
                      error={"code": "timeout", "message": "Command stopped; effects not reconciled"})
        self.rt.log.emit(type="workspace.tool_finished", task_id=self.task, actor=HOST,
            payload={"started_seq": started.seq, "call_id": started.payload["call_id"], "result": result})
        self.assertEqual([e.seq for e in self.rt.workspaces.pending(task_id=self.task)], [started.seq])
        with self.assertRaises(WorkConflict):
            self.service(context)
        self.assertEqual(self.driver.tool_requests, [])

    def test_tool_transport_failure_never_gets_text_only_recovery_marker(self):
        _, context = self.prepare_call()
        native = OpenAICompatibleDriver("dev", endpoint="https://invalid.example/v1/chat/completions",
            model="fake", api_key_env="CATER_TEST_UNUSED_KEY")
        self.rt.drivers["dev"] = native
        responses = [turn(ToolCall("write-1", "workspace_write", {"path": "a.txt"})),
                     TextTransportFailure("Lost response after successful write")]
        with patch.object(native, "tool_turn", side_effect=responses):
            response = self.gateway(context)
        self.assertTrue(response.meta["contained"])
        self.assertEqual(len(self.events("workspace.tool_finished")), 1)
        self.assertIsNot(response.meta.get("_text_transport_failure"), _TEXT_TRANSPORT_FAILURE)

    def test_native_workspace_conflict_is_visible_and_already_returned_tool_is_dispatched(self):
        _, context = self.prepare_call()
        native = OpenAICompatibleDriver("dev", endpoint="https://fixture.test/v1/chat/completions",
            model="fake", api_key_env="CATER_TEST_UNUSED_KEY", token_budget=100)
        self.rt.drivers["dev"] = native
        data = {"choices": [{"finish_reason": "tool_calls", "message": {"role": "assistant", "content": None,
                 "tool_calls": [{"id": "write-1", "type": "function", "function": {"name": "workspace_write", "arguments": '{"path":"a.txt"}'}}]}}],
                "usage": {"prompt_tokens": 101, "total_tokens": 1}}
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def read(self, _limit): return json.dumps(data).encode()
        with patch.dict("cater.drivers.os.environ", {"CATER_TEST_UNUSED_KEY": "fixture"}), patch("cater.drivers._open_api_request", return_value=Response()) as transport:
            with self.assertRaisesRegex(DriverUnavailable, "用量记录"):
                self.rt.workspaces.complete(task_id=self.task, role=self.role, driver=native,
                                            prompt="Inspect source", work_context=context)
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(len(self.executor.calls), 1)
        self.assertEqual(len(self.events("workspace.tool_finished")), 1)
        call, = self.events("workspace.model_called")
        self.assertEqual(call.payload["usage_issue"], "total_below_reported_components")
        notice, = self.events("driver.usage_inconsistent")
        self.assertTrue(notice.payload["budget_enforced"])

    def test_untrusted_workspace_metadata_does_not_cause_notice_or_pause(self):
        _, context = self.prepare_call()
        self.driver.token_budget = 100
        forged = replace(turn(ToolCall("write-1", "workspace_write", {"path": "a.txt"})), tokens=0,
            meta={"mode": "openai_compatible", "token_usage": "unavailable",
                  "usage_issue": "total_below_reported_components", "usage": {"total_tokens": 1, "prompt_tokens": 101}})
        self.driver.turns = [forged, turn(text=final_text())]
        result = self.service(context)
        self.assertTrue(result.text)
        self.assertEqual(len(self.executor.calls), 1)
        self.assertEqual(len(self.driver.tool_requests), 2)
        self.assertEqual(self.events("driver.usage_inconsistent"), [])
        self.assertNotIn("usage_issue", self.events("workspace.model_called")[0].payload)

    def test_malformed_final_response_after_write_keeps_work_uncertain(self):
        self.bind()
        self.plan()
        self.driver.turns = [turn(ToolCall("write-1", "workspace_write", {"path": "a.txt"})), turn(text="invalid-json")]
        with patch.object(self.rt, "_review_work_item"):
            self.rt.collaborate(self.task)
        item = self.rt.work.view(self.task)["items"][0]
        self.assertEqual(item["status"], "uncertain")
        self.assertEqual(len(self.events("workspace.tool_finished")), 1)
        before = len(self.driver.tool_requests)
        with patch.object(self.rt, "_review_work_item"):
            self.rt.collaborate(self.task)
        self.assertEqual(len(self.driver.tool_requests), before)


if __name__ == "__main__":
    unittest.main()
