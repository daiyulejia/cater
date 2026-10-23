"""Local iterator and SQLite boundary tests; no network or remote model calls."""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
import json
import unittest
from unittest.mock import patch

from cater.drivers import DriverProtocolError, _tool_response_error, tool_protocol_error_usage
from cater.runtime import Runtime
from cater.team_response_correction import correctable
from cater.tool_protocol import ToolTurn
from cater.workspaces import WorkspaceService
import test_simple_team as simple


TOKENS = 19
USAGE = {"prompt_tokens": 12, "completion_tokens": 7, "total_tokens": TOKENS,
         "prompt_tokens_details": {"cached_tokens": 4}}
PRIVATE = "PRIVATE_NATIVE_RESPONSE"
TEXT = "UNADOPTED_COMPLETE_RESPONSE"


class ControlledDriver(simple.TeamDriver):
    tool_response_corrections = 1

    def __init__(self):
        super().__init__()
        self.outcome = None
        self.returned = []

    def tool_turn(self, **kwargs):
        raise AssertionError("TeamService must use the iterator")

    def iter_tool_turn(self, *, role, prompt, history, tools, check_current=None):
        self.calls.append({"role": role, "context": json.loads(prompt.rsplit("\n", 1)[-1]),
                           "history": deepcopy(history), "tools": deepcopy(tools)})
        if len(self.calls) != 1:
            raise AssertionError("Unexpected retry or automatic correction")
        if check_current is None:
            raise AssertionError("Missing scope callback")
        check_current()
        # A complete local result is ready; the caller controls the return boundary.
        outcome = self.outcome
        yield "complete-response-ready"
        self.returned.append(outcome)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class NativeTeamBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch("urllib.request.OpenerDirector.open",
                                side_effect=AssertionError("HTTP is forbidden in local boundary tests")))
        self.enterContext(patch("socket.create_connection",
                                side_effect=AssertionError("Network is forbidden in local boundary tests")))

    @contextmanager
    def scenario(self):
        fixture = simple.SimpleTeamTests()
        fixture.setUp()
        try:
            roots = [fixture.root / name for name in ("original", "replacement")]
            for root in roots:
                root.mkdir()
            config = fixture.root / "boundary-workspaces.json"
            config.write_text(json.dumps({"workspaces": [
                {"id": root.name, "title": root.name, "root": str(root), "mode": "edit"}
                for root in roots]}), encoding="utf-8")
            fixture.rt.workspaces = WorkspaceService(fixture.rt, config)
            fixture.rt.workspaces.bind(fixture.task, "original", expected_revision=None)
            driver = ControlledDriver()
            fixture.rt.drivers[driver.name] = driver
            yield fixture, driver, roots, config
        finally:
            fixture.doCleanups()

    def result(self, fixture, *, tools=True, cleanup=False):
        calls = [("workspace_write", {"path": "boundary.txt", "content": "must not be written",
                                       "expected_sha256": None}),
                 ("publish_artifact", {"filename": "boundary.md", "content": "must not be published",
                                       "summary": "boundary fixture", "format": "markdown"}),
                 ("delegate", {"recipient": fixture.worker, "instruction": "must not be delegated"}),
                 ("wait_for_reply", {})] if tools else []
        turn = simple.turn(TEXT, *calls)
        meta = {"token_usage": "reported", "usage": {**deepcopy(USAGE), "private": PRIVATE}}
        if cleanup:
            meta["native_cleanup_failed"] = True
        return replace(turn, tokens=TOKENS, meta=meta)

    def start(self, fixture):
        source = fixture.rt.team.receive(fixture.task, "Inspect this local fixture only",
                                         request_id="native-boundary-request")
        claimed = fixture.rt.team._claim(fixture.task)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed[0].seq, source.seq)
        run = fixture.rt.team._run_message(fixture.task, *claimed)
        self.addCleanup(run.close)
        self.assertEqual(next(run), "complete-response-ready")
        self.assertEqual(fixture.events("team.model_called"), [])
        self.assertEqual(fixture.events("msg.said"), [])
        self.assertEqual(fixture.events("team.tool_started"), [])
        return run, claimed[2]

    def assert_usage(self, fixture, driver, started, *, protocol_error=False):
        model_call, = fixture.events("team.model_called")
        finish, = fixture.events("team.run_finished")
        self.assertEqual(model_call.payload["run_id"], started.payload["run_id"])
        self.assertEqual(model_call.payload["tokens"], TOKENS)
        self.assertEqual(model_call.payload["token_usage"], "reported")
        self.assertEqual(model_call.payload["usage"], USAGE)
        self.assertEqual(fixture.rt.task(fixture.task).tokens, TOKENS)
        self.assertEqual(fixture.rt._reported_driver_tokens(driver.name), TOKENS)
        self.assertLess(started.seq, model_call.seq)
        self.assertLess(model_call.seq, finish.seq)
        self.assertEqual(len(driver.calls), 1)
        self.assertEqual(len(driver.returned), 1)
        self.assertEqual(fixture.events("team.response_correction_requested"), [])
        if protocol_error:
            self.assertEqual(model_call.payload["response_status"], "protocol_error")
        self.assertTrue(fixture.rt.verify_chain().ok)
        return model_call, finish

    def assert_no_effects(self, fixture, roots, dispatch):
        dispatch.assert_not_called()
        for kind in ("team.tool_started", "team.tool_finished", "msg.said", "team.objective_completed"):
            self.assertEqual(fixture.events(kind), [], kind)
        self.assertEqual(fixture.rt.task(fixture.task).deliverables, [])
        self.assertEqual([event for event in fixture.events("team.message_received")
                          if event.payload.get("kind") == "assignment"], [])
        for root in roots:
            self.assertFalse((root / "boundary.txt").exists())
        payloads = json.dumps([event.payload for event in fixture.rt.log.read(task_id=fixture.task)])
        self.assertNotIn(TEXT, payloads)
        self.assertNotIn(PRIVATE, payloads)

    def test_unchanged_scope_accepts_complete_result_and_dispatches_valid_tools(self):
        with self.scenario() as (fixture, driver, roots, _):
            driver.outcome = self.result(fixture)
            run, started = self.start(fixture)
            with patch.object(fixture.rt.team, "_dispatch_tool", wraps=fixture.rt.team._dispatch_tool) as dispatch:
                self.assertEqual(list(run), [])
            _, finish = self.assert_usage(fixture, driver, started)
            self.assertEqual(finish.payload["status"], "waiting")
            self.assertEqual(dispatch.call_count, 4)
            self.assertEqual([event.payload["result"]["status"] for event in fixture.events("team.tool_finished")],
                             ["ok"] * 4)
            self.assertTrue((roots[0] / "boundary.txt").is_file())
            self.assertEqual(len(fixture.rt.task(fixture.task).deliverables), 1)
            self.assertEqual(len([event for event in fixture.events("team.message_received")
                                  if event.payload.get("kind") == "assignment"]), 1)
            said, = fixture.events("msg.said")
            self.assertEqual(said.payload["text"], TEXT)

    def test_committed_scope_changes_meter_complete_return_before_rejecting_text_and_tools(self):
        for change in ("rules", "workspace", "pause_resume"):
            for tools in (False, True):
                with self.subTest(change=change, tools=tools), self.scenario() as (fixture, driver, roots, config):
                    driver.outcome = self.result(fixture, tools=tools)
                    run, started = self.start(fixture)
                    previous = fixture.rt.team.recovery.scope(fixture.task)
                    with Runtime(fixture.rt.log.path, roles_path=fixture.roles,
                                 drivers={driver.name: driver}, close_drivers_on_close=False) as incoming:
                        incoming.workspaces = WorkspaceService(incoming, config)
                        if change == "rules":
                            incoming.rulebook.record("Discuss only; do not execute the old response",
                                                     scope="task", task_id=fixture.task)
                        elif change == "workspace":
                            revision = incoming.workspaces.view(fixture.task)["revision"]
                            incoming.workspaces.bind(fixture.task, "replacement", expected_revision=revision)
                        else:
                            incoming.execution_control.change(fixture.task, "pause", request_id="boundary-pause")
                            incoming.execution_control.change(fixture.task, "resume", request_id="boundary-resume")
                        self.assertFalse(incoming.log.db.in_transaction)
                    current = fixture.rt.team.recovery.scope(fixture.task)
                    field = {"rules": "rule_fingerprint", "workspace": "workspace_revision",
                             "pause_resume": "control_seq"}[change]
                    self.assertNotEqual(previous[field], current[field])
                    self.assertEqual(fixture.events("team.model_called"), [])
                    scope_change_seq = fixture.rt.log.read(task_id=fixture.task)[-1].seq
                    with patch.object(fixture.rt.team, "_dispatch_tool", wraps=fixture.rt.team._dispatch_tool) as dispatch:
                        self.assertEqual(list(run), [])
                    model_call, finish = self.assert_usage(fixture, driver, started)
                    self.assertIs(driver.returned[0], driver.outcome)
                    self.assertLess(scope_change_seq, model_call.seq)
                    self.assertEqual(finish.payload["status"], "interrupted")
                    self.assertNotIn("native_cleanup_failed", finish.payload)
                    self.assert_no_effects(fixture, roots, dispatch)

    def test_trusted_cleanup_failure_meters_complete_text_and_tool_returns_without_dispatch(self):
        for tools in (False, True):
            with self.subTest(tools=tools), self.scenario() as (fixture, driver, roots, _):
                driver.outcome = self.result(fixture, tools=tools, cleanup=True)
                run, started = self.start(fixture)
                with patch.object(fixture.rt.team, "_dispatch_tool", wraps=fixture.rt.team._dispatch_tool) as dispatch:
                    self.assertEqual(list(run), [])
                _, finish = self.assert_usage(fixture, driver, started)
                self.assertEqual(finish.payload["status"], "interrupted")
                self.assertIs(finish.payload["native_cleanup_failed"], True)
                self.assertTrue(finish.payload["reason"])
                self.assert_no_effects(fixture, roots, dispatch)

    def test_cleanup_failure_blocks_otherwise_correctable_contained_response(self):
        for mode in ("openai_compatible", "anthropic"):
            with self.subTest(mode=mode), self.scenario() as (fixture, driver, roots, _):
                meta = {"mode": mode, "token_usage": "reported", "usage": deepcopy(USAGE),
                        "contained": True, "incomplete": True, "invocation_started": True,
                        "reason": "invalid_tool_calls" if mode == "openai_compatible" else "invalid_content",
                        "tool_issue": "arguments_not_object",
                        **({"finish_reason": "tool_calls"} if mode == "openai_compatible" else {"stop_reason": "tool_use"})}
                unflagged = ToolTurn("", [], TOKENS, meta, {"role": "assistant", "content": "", "tool_calls": []})
                self.assertTrue(correctable(driver, unflagged), "Fixture must be eligible for automatic correction")
                driver.outcome = replace(unflagged, meta={**meta, "native_cleanup_failed": True})
                run, started = self.start(fixture)
                with patch.object(fixture.rt.team, "_dispatch_tool", wraps=fixture.rt.team._dispatch_tool) as dispatch:
                    self.assertEqual(list(run), [])
                model_call, finish = self.assert_usage(fixture, driver, started)
                self.assertEqual(model_call.payload["response_status"], "contained")
                self.assertIs(model_call.payload["response_client_tools_executed"], False)
                self.assertIs(finish.payload["native_cleanup_failed"], True)
                self.assertEqual(finish.payload["status"], "interrupted")
                self.assert_no_effects(fixture, roots, dispatch)

    def test_cleanup_protocol_error_preserves_reported_usage_without_correction_or_dispatch(self):
        with self.scenario() as (fixture, driver, roots, _):
            error = _tool_response_error("Invalid local fixture response", {
                "usage": {**deepcopy(USAGE), "private": PRIVATE}, "private": PRIVATE}, anthropic=False)
            error.native_cleanup_failed = True
            self.assertIs(type(error), DriverProtocolError)
            driver.outcome = error
            run, started = self.start(fixture)
            with patch.object(fixture.rt.team, "_dispatch_tool", wraps=fixture.rt.team._dispatch_tool) as dispatch:
                self.assertEqual(list(run), [])
            model_call, finish = self.assert_usage(fixture, driver, started, protocol_error=True)
            self.assertIs(driver.returned[0], error)
            self.assertEqual(model_call.payload["usage"], tool_protocol_error_usage(error)["usage"])
            self.assertEqual(finish.payload["status"], "failed")
            self.assertEqual(finish.payload["error_type"], "DriverProtocolError")
            self.assertEqual(finish.payload["failure_stage"], "response_handling")
            self.assertIs(finish.payload["native_cleanup_failed"], True)
            self.assert_no_effects(fixture, roots, dispatch)


if __name__ == "__main__":
    unittest.main()
