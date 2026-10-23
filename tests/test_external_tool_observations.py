"""Offline telemetry regressions for QA batch tool-boundary-20261024.

Synthetic ACP notifications are evidence of adapter behavior, not evidence that
a production client emits these fields or that any business tool ran.
"""

from dataclasses import FrozenInstanceError
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from cater.acp import AcpProtocolError, AcpSession
from cater.external_agent import QoderAcpDriver, _DIAGNOSTIC_COUNT_LIMIT, _ProgressBudget
import test_team_external as team_fixture


REASONS = ("not_initialized", "inactive", "unknown_tool", "arguments_not_object")
ZERO_FACTS = {
    "client_tool_diagnostics_version": 1,
    "client_tool_updates": 0,
    "client_tool_failed_calls": 0,
    "client_tool_diagnostics_dropped": 0,
    **{"host_bridge_rejected_" + reason: 0 for reason in REASONS},
}
FAILURE_TIMES = {"first_client_tool_failure_ms", "last_client_tool_failure_ms"}
PRIVATE = "PRIVATE_TOOL_PAYLOAD_SENTINEL"
PRIVATE_ID = "PRIVATE_CLIENT_IDENTIFIER"


def tool_update(identity="call-A", status="failed", **fields):
    return {"sessionUpdate": "tool_call_update", "toolCallId": identity,
            "status": status, **fields}


def sensitive_update():
    return tool_update(PRIVATE_ID, title="mcp__cater__finish_work " + PRIVATE,
                       rawInput={PRIVATE: PRIVATE}, rawOutput={"error": PRIVATE},
                       content=[{"type": "text", "text": PRIVATE}],
                       locations=[{"path": PRIVATE}],
                       error={"code": PRIVATE, "message": PRIVATE},
                       _meta={"qoder": {"toolName": "mcp__cater__finish_work"},
                              PRIVATE: PRIVATE})


# The child only speaks ACP over stdio. It never invokes a model or a tool.
PEER = r'''
import json
import sys

mode = sys.argv[1]
update = json.loads(sys.argv[2])

def send(value):
    print(json.dumps(value), flush=True)

def notify(value):
    send({"jsonrpc": "2.0", "method": "session/update",
          "params": {"sessionId": "offline-tools", "update": value}})

for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    result = {}
    if method == "initialize":
        result = {"protocolVersion": 1, "agentInfo": {
            "name": "impostor" if mode == "identity" else "qoder-cli-cn"}}
    elif method == "session/new":
        result = {"sessionId": "offline-tools", "models": {
            "currentModelId": "offline", "availableModels": [
                {"modelId": "offline", "name": "ExactModel"}]}}
    elif method == "session/prompt":
        if mode != "zero":
            thought = dict(update, sessionUpdate="agent_thought_chunk",
                           toolCallId="PRIVATE_THOUGHT_ID",
                           content={"type": "text", "text": "PRIVATE_THOUGHT_SENTINEL"})
            notify(thought)
            notify(update)
            notify(dict(update, status="completed"))
            notify(update)
        if mode == "reject":
            send({"jsonrpc": "2.0", "id": request["id"], "error": {
                "code": -32000, "message": "PRIVATE_RPC_ERROR_SENTINEL"}})
            continue
        result = {"stopReason": "end_turn"}
    else:
        continue
    send({"jsonrpc": "2.0", "id": request["id"], "result": result})
'''


class ExternalToolObservationTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.driver = QoderAcpDriver("external_fixture", "never-launch-model", "ExactModel", timeout=5)
        self.execute = Mock(side_effect=AssertionError("Telemetry must not execute tools"))

    def new_run(self, *, phase="prompting", on_update=None):
        run = self.driver.agent_run(cwd=self.root, tools=[], execute=self.execute,
                                    check_current=lambda: None, on_update=on_update)
        run._phase = phase
        run._started_at = 100.0
        return run

    def assert_safe(self, run):
        state = run.state
        facts = dict(state.observations)
        self.assertTrue(all(type(value) is int and value >= 0 for value in facts.values()))
        for secret in (PRIVATE, PRIVATE_ID, "PRIVATE_THOUGHT_SENTINEL", "PRIVATE_THOUGHT_ID"):
            self.assertNotIn(secret, repr(state))
        for digest in run._client_tool_failures:
            self.assertNotIn(digest.hex(), repr(state))
            self.assertNotIn(repr(digest), repr(state))
        self.assertFalse(any("finish_work" in key for key in facts))
        self.execute.assert_not_called()

    def peer_session(self, mode, sessions):
        class Session(AcpSession):
            def __init__(self, command, **kwargs):
                super().__init__([sys.executable, "-B", "-u", "-c", PEER, mode,
                                  json.dumps(sensitive_update())], **kwargs)
                sessions.append(self)
        return Session

    def test_created_and_non_prompting_phases_do_not_claim_collection(self):
        for phase in ("created", "opening", "finished", "failed", "yielded"):
            with self.subTest(phase=phase):
                run = self.new_run(phase=phase)
                run._observe_tool_update(tool_update())
                self.assertEqual(run.state.observations, ())
                self.assertEqual(run._client_tool_failures, {})

    def test_prompt_initialization_is_idempotent_and_preserves_early_bridge_rejection(self):
        run = self.new_run(phase="opening")
        run._bridge_rejection("inactive")
        self.assertEqual(dict(run.state.observations), {"host_bridge_rejected_inactive": 1})
        self.assertFalse(run.state.prompt_attempted)
        run._phase = "prompting"
        run._start_tool_diagnostics()
        run._start_tool_diagnostics()
        self.assertEqual(dict(run.state.observations), {**ZERO_FACTS, "host_bridge_rejected_inactive": 1})

    def test_only_exact_tool_categories_are_observed(self):
        run = self.new_run()
        for kind in ("agent_message_chunk", "agent_thought_chunk", "plan", "TOOL_CALL",
                     "tool_call_update_extra", PRIVATE, None, [], {}):
            run._observe_tool_update(tool_update(sessionUpdate=kind))
        for malformed in (None, [], "tool_call", 1):
            run._observe_tool_update(malformed)
        self.assertEqual(run.state.observations, ())
        for kind in ("tool_call", "tool_call_update"):
            run._observe_tool_update(tool_update(kind, sessionUpdate=kind))
        self.assertEqual(dict(run.state.observations)["client_tool_failed_calls"], 2)

    def test_update_first_interleaved_replays_and_transitions_count_each_id_once(self):
        run = self.new_run()
        with patch("cater.external_agent.time.monotonic", return_value=101) as clock:
            run._update(tool_update("A"))
            snapshot = run.state
            clock.return_value = 102
            run._update(tool_update("B", "pending", sessionUpdate="tool_call"))
            run._update(tool_update("A", "completed", sessionUpdate="tool_call"))
            run._update({"sessionUpdate": "tool_call_update", "toolCallId": "A", "title": PRIVATE})
            run._update(tool_update("A", rawInput={"changed": PRIVATE}))
            clock.return_value = 103
            run._update(tool_update("B"))
            clock.return_value = 110
            for identity in ("A", "B", "A", "B"):
                run._update(tool_update(identity))
        facts = dict(run.state.observations)
        self.assertEqual(facts["client_tool_updates"], 10)
        self.assertEqual(facts["client_tool_failed_calls"], 2)
        self.assertEqual(facts["client_tool_diagnostics_dropped"], 0)
        self.assertEqual(facts["first_client_tool_failure_ms"], 1000)
        self.assertEqual(facts["last_client_tool_failure_ms"], 3000)
        self.assertEqual(dict(snapshot.observations)["client_tool_failed_calls"], 1)
        self.assertEqual(dict(snapshot.observations)["last_client_tool_failure_ms"], 1000)
        with self.assertRaises(FrozenInstanceError):
            snapshot.observations = ()
        self.assert_safe(run)

    def test_allowed_statuses_and_omission_do_not_invent_failure(self):
        run = self.new_run()
        for status in ("pending", "in_progress", "completed"):
            run._update(tool_update(status, status))
        run._update({"sessionUpdate": "tool_call", "toolCallId": "omitted"})
        facts = dict(run.state.observations)
        self.assertEqual(facts["client_tool_updates"], 4)
        self.assertEqual(facts["client_tool_failed_calls"], 0)
        self.assertEqual(facts["client_tool_diagnostics_dropped"], 0)
        self.assertTrue(FAILURE_TIMES.isdisjoint(facts))
        self.assertNotIn("tool_calls", facts)

    def test_malformed_ids_are_dropped_without_reserving_capacity(self):
        invalid = (None, True, 1, [], {}, "", " \t\r\n", "x" * 513,
                   "a\0b", "\ud800", "\udfff", "x\ud800y")
        for identity in invalid:
            with self.subTest(identity=repr(identity)):
                run = self.new_run()
                run._update(tool_update(identity))
                facts = dict(run.state.observations)
                self.assertEqual(facts["client_tool_updates"], 1)
                self.assertEqual(facts["client_tool_diagnostics_dropped"], 1)
                self.assertEqual(facts["client_tool_failed_calls"], 0)
                self.assertEqual(run._client_tool_failures, {})
                self.assertTrue(FAILURE_TIMES.isdisjoint(facts))
                self.assertIsNone(run._failure)
        run = self.new_run()
        run._update({"sessionUpdate": "tool_call_update", "status": "failed"})
        self.assertEqual(dict(run.state.observations)["client_tool_diagnostics_dropped"], 1)

    def test_valid_utf8_ids_use_character_limit_and_preserve_exact_identity(self):
        run = self.new_run()
        for identity in ("x" * 512, "\U0001f600" * 512, "A", " A ", "a"):
            run._update(tool_update(identity))
        facts = dict(run.state.observations)
        self.assertEqual(facts["client_tool_failed_calls"], 5)
        self.assertEqual(facts["client_tool_diagnostics_dropped"], 0)

    def test_malformed_statuses_are_dropped_and_do_not_poison_later_valid_failure(self):
        for status in (None, True, 1, [], {}, "", "FAILED", " failed ", PRIVATE):
            with self.subTest(status=repr(status)):
                run = self.new_run()
                run._update(tool_update(status=status))
                self.assertEqual(run._client_tool_failures, {})
                run._update(tool_update())
                facts = dict(run.state.observations)
                self.assertEqual(facts["client_tool_updates"], 2)
                self.assertEqual(facts["client_tool_diagnostics_dropped"], 1)
                self.assertEqual(facts["client_tool_failed_calls"], 1)
                self.assert_safe(run)

    def test_capacity_never_evicts_and_flood_does_not_recount_old_failures(self):
        run = self.new_run()
        for index in range(256):
            run._observe_tool_update(tool_update(str(index), "pending"))
        original_keys = set(run._client_tool_failures)
        for index in range(256, 1280):
            run._observe_tool_update(tool_update(str(index)))
        for _ in range(3):
            run._observe_tool_update(tool_update("256"))
            run._observe_tool_update(tool_update("0"))
            run._observe_tool_update(tool_update("255"))
        facts = dict(run.state.observations)
        self.assertEqual(facts["client_tool_diagnostics_dropped"], 1027)
        self.assertEqual(facts["client_tool_failed_calls"], 2)
        self.assertEqual(facts["client_tool_updates"], 1289)
        self.assertEqual(set(run._client_tool_failures), original_keys)
        self.assertEqual(len(original_keys), 256)
        self.assertTrue(all(type(key) is bytes and len(key) == 32 for key in original_keys))
        self.assertTrue(all(type(value) is bool for value in run._client_tool_failures.values()))

    def test_all_diagnostic_counters_saturate_at_fixed_limit(self):
        self.assertEqual(_DIAGNOSTIC_COUNT_LIMIT, 2**31 - 1)
        run = self.new_run()
        run._start_tool_diagnostics()
        counters = set(ZERO_FACTS) - {"client_tool_diagnostics_version"}
        for name in counters:
            run._observations[name] = _DIAGNOSTIC_COUNT_LIMIT - 1
        for identity in ("A", "B", "C"):
            run._observe_tool_update(tool_update(identity))
            run._observe_tool_update(tool_update(None))
            for reason in REASONS:
                run._bridge_rejection(reason)
        facts = dict(run.state.observations)
        self.assertEqual({name: facts[name] for name in counters},
                         dict.fromkeys(counters, _DIAGNOSTIC_COUNT_LIMIT))
        self.assertEqual(facts["client_tool_diagnostics_version"], 1)

    def test_bridge_callback_accepts_only_fixed_reasons(self):
        run = self.new_run()
        run._start_tool_diagnostics()
        for reason in (PRIVATE, "UNKNOWN_TOOL", "inactive ", None, [], {}, True, 7):
            run._bridge_rejection(reason)
        self.assertEqual(dict(run.state.observations), ZERO_FACTS)
        for reason in REASONS:
            run._bridge_rejection(reason)
            run._bridge_rejection(reason)
        self.assertEqual(dict(run.state.observations), {
            **ZERO_FACTS, **{"host_bridge_rejected_" + reason: 2 for reason in REASONS}})
        self.assert_safe(run)

    def test_payloads_and_tool_names_never_enter_diagnostics_with_or_without_observer(self):
        for observed in (None, []):
            with self.subTest(observer=observed is not None):
                run = self.new_run(on_update=None if observed is None else observed.append)
                update = sensitive_update()
                run._update(update)
                facts = dict(run.state.observations)
                self.assertEqual(facts["client_tool_failed_calls"], 1)
                self.assertEqual(set(facts), set(ZERO_FACTS) | FAILURE_TIMES | {
                    "public_updates", "first_public_update_ms", "last_public_update_ms"})
                self.assertEqual(run._client_tool_failures, {
                    hashlib.sha256(PRIVATE_ID.encode("utf-8")).digest(): True})
                self.assert_safe(run)
                if observed is not None:
                    self.assertEqual(observed, [update])
                update["status"] = "pending"
                update["toolCallId"] = "mutated"
                self.assertEqual(dict(run.state.observations), facts)

    def test_documentation_prose_titles_and_errors_are_not_failure_evidence(self):
        run = self.new_run()
        run._start_tool_diagnostics()
        prose = "Documentation example: finish_work was rejected three times; status=failed."
        run._update({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": prose}})
        run._update({"sessionUpdate": "plan", "content": prose, "status": "failed"})
        for name in ("finish_work", "mcp__cater__finish_work", "MCP__CATER__FINISH_WORK",
                     "mcp__cater__finish_work_extra", "mcp__cater__unoffered"):
            update = sensitive_update()
            update.update(toolCallId=name, title=name)
            del update["status"]
            run._update(update)
        facts = dict(run.state.observations)
        self.assertEqual(facts["client_tool_failed_calls"], 0)
        self.assertEqual(facts["client_tool_diagnostics_dropped"], 0)
        self.assertTrue(FAILURE_TIMES.isdisjoint(facts))
        self.assertTrue(all(facts["host_bridge_rejected_" + reason] == 0 for reason in REASONS))
        self.assertEqual(run.state.text, prose)
        self.assert_safe(run)

    def test_diagnostic_helpers_never_refresh_activity_or_useful_progress(self):
        with patch("cater.external_agent.time.monotonic", return_value=100) as clock:
            run = self.new_run()
            run._budget = _ProgressBudget(2, 8)
            clock.return_value = 101
            run._observe_tool_update(tool_update())
            run._bridge_rejection("inactive")
            self.assertEqual(run._budget.activity_at, 100)
            self.assertEqual(run._budget.progress_at, 100)
            self.assertEqual(run._budget.seen, set())
            clock.return_value = 102
            self.assertEqual(run._budget.expired().limit, "idle")

    def test_wired_updates_preserve_activity_dedup_and_fixed_total_budget(self):
        with patch("cater.external_agent.time.monotonic", return_value=100) as clock:
            run = self.new_run()
            run._budget = _ProgressBudget(2, 5)
            update = tool_update()
            clock.return_value = 101
            run._update(update)
            clock.return_value = 102
            run._update(update)
            self.assertEqual(run._budget.activity_at, 101)
            clock.return_value = 103
            self.assertEqual(run._budget.expired().limit, "idle")
            self.assertEqual(run._budget.progress_at, 100)
            self.assertNotIn("progress_receipts", dict(run.state.observations))
            run._budget = _ProgressBudget(2, 5)
            for timestamp in (104, 105, 106, 107):
                clock.return_value = timestamp
                run._update(tool_update(str(timestamp)))
            clock.return_value = 108
            self.assertEqual(run._budget.expired().limit, "total")
            self.assertEqual(run._budget.progress_at, 103)

    def test_real_acp_zero_updates_initialize_and_handshake_failure_stays_unclaimed(self):
        for mode in ("zero", "identity"):
            with self.subTest(mode=mode):
                sessions = []
                run = self.new_run(phase="created")
                with patch("cater.external_agent.AcpSession", self.peer_session(mode, sessions)):
                    if mode == "identity":
                        with self.assertRaises(AcpProtocolError):
                            run.run("Offline handshake")
                        self.assertEqual(set(dict(run.state.observations)), {"failure_ms", "settled_ms"})
                        self.assertFalse(run.state.prompt_attempted)
                    else:
                        run.run("Offline empty turn")
                        facts = dict(run.state.observations)
                        self.assertEqual({key: facts[key] for key in ZERO_FACTS}, ZERO_FACTS)
                        self.assertTrue(FAILURE_TIMES.isdisjoint(facts))
                self.assertTrue(run.state.cleanup_complete)
                self.assertEqual(len(sessions), 1)
                self.assertIsNotNone(sessions[0]._proc.poll())

    def test_real_acp_terminal_observations_persist_in_runtime_without_private_payloads(self):
        for mode in ("success", "reject"):
            with self.subTest(mode=mode):
                fixture = team_fixture.TeamExternalTests()
                fixture.setUp()
                self.addCleanup(fixture.doCleanups)
                runs, sessions = [], []

                def agent_run(**kwargs):
                    run = self.driver.agent_run(**kwargs)
                    runs.append(run)
                    return run

                with patch.object(fixture.driver, "agent_run", side_effect=agent_run), \
                        patch("cater.external_agent.AcpSession", self.peer_session(mode, sessions)):
                    fixture.receive("Observe an offline scripted turn once")
                    fixture.rt.collaborate(fixture.task)
                    events = fixture.rt.log.read(task_id=fixture.task)
                    fixture.rt.collaborate(fixture.task)
                    self.assertEqual(fixture.rt.log.read(task_id=fixture.task), events)
                self.assertEqual(len(runs), 1)
                run = runs[0]
                called, = fixture.events("team.model_called")
                facts = dict(run.state.observations)
                self.assertEqual(called.payload["external_observations"], facts)
                self.assertEqual(facts["client_tool_updates"], 3)
                self.assertEqual(facts["client_tool_failed_calls"], 1)
                self.assertEqual(facts["client_tool_diagnostics_dropped"], 0)
                self.assertTrue(FAILURE_TIMES.issubset(facts))
                finished, = fixture.events("team.run_finished")
                if mode == "reject":
                    self.assertEqual(finished.payload["external_observations"], facts)
                    self.assertEqual(finished.payload["external_failure_code"], "request_rejected")
                    self.assertEqual(finished.payload["external_rpc_code"], -32000)
                    self.assertEqual(finished.payload["external_rpc_method"], "session/prompt")
                    self.assertEqual(finished.payload["failure_stage"], "prompting")
                external, = fixture.events("team.external_run_finished")
                self.assertTrue(external.payload["cleanup_complete"])
                self.assertTrue(external.payload["prompt_attempted"])
                self.assertEqual(fixture.rt.team.external_runs.pending(fixture.task), [])
                for kind in ("team.tool_started", "team.tool_finished", "team.objective_completed"):
                    self.assertEqual(fixture.events(kind), [])
                for secret in (PRIVATE, PRIVATE_ID, "PRIVATE_THOUGHT_SENTINEL",
                               "PRIVATE_THOUGHT_ID", "PRIVATE_RPC_ERROR_SENTINEL"):
                    self.assertNotIn(secret, repr(events))
                for digest in run._client_tool_failures:
                    self.assertNotIn(digest.hex(), repr(events))
                self.assert_safe(run)
                self.assertTrue(fixture.rt.verify_chain().ok)
                self.assertEqual(len(sessions), 1)
                self.assertIsNotNone(sessions[0]._proc.poll())


if __name__ == "__main__":
    unittest.main()
