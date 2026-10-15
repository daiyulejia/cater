from __future__ import annotations

import io
import json
import os
import socket
import subprocess
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import cater.runtime as runtime_module
from cater.anthropic import AnthropicMessagesDriver
from cater.drivers import CliDriver, DriverResponse, OpenAICompatibleDriver, TextRequestRejected
from cater.events import Actor
from cater.planning import Planner, _attempt_state, _known_text, requirement_sources
from cater.runtime import Runtime
from cater.work import WorkConflict
from test_planning import PlanningDriver
from test_work import WorkDriver, item


class DerivedAnthropic(AnthropicMessagesDriver):
    pass


class DerivedOpenAI(OpenAICompatibleDriver):
    pass


class PlanningRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {"CATER_PLANNING_FIXTURE": "synthetic-key"}))
        self.enterContext(patch.object(socket, "socket", side_effect=AssertionError("network forbidden")))
        self.enterContext(patch.object(subprocess, "Popen", side_effect=AssertionError("process forbidden")))
        self.now = 1000.0
        self.enterContext(patch("cater.planning.time", SimpleNamespace(time=lambda: self.now)))
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        config = json.loads(Path("cater/packs/default_roles.json").read_text(encoding="utf-8"))
        config["organization"]["leader"]["driver"] = "planner"
        self.roles = self.root / "roles.json"
        self.roles.write_text(json.dumps(config), encoding="utf-8")
        self.count = 0

    def runtime(self, cls=AnthropicMessagesDriver, *, driver=None):
        self.count += 1
        driver = driver or cls("planner", endpoint="https://fixture.invalid/v1/messages", model="fixture",
                               api_key_env="CATER_PLANNING_FIXTURE")
        worker = WorkDriver()
        drivers = {**{name: worker for name in ("product", "dev", "test", "design")}, "planner": driver}
        rt = Runtime(self.root / f"planning-{self.count}.db", roles_path=self.roles, drivers=drivers,
                     driver_attempts=9, circuit_breaker_threshold=20, close_drivers_on_close=False)
        self.addCleanup(rt.close)
        task = rt.create_task("Create a note")
        rt.intake(task)
        return rt, task, driver

    def events(self, rt, task, kind):
        return list(rt.log.iter_events(task_id=task, event_types=(kind,)))

    def transport(self, cls):
        return ("cater.anthropic._open_api_request" if issubclass(cls, AnthropicMessagesDriver)
                else "cater.drivers._open_api_request")

    def success(self, rt, task, cls=AnthropicMessagesDriver, *, status="ready", summary="New plan"):
        payload = {"status": status, "summary": summary, "items": [], "coverage": [], "reuse": []}
        if status == "ready":
            payload["items"] = [item("draft")]
            payload["coverage"] = [{"source_id": source["id"], "item_ids": ["draft"], "superseded_by": None,
                                    "reason": "Requested output", "quote": source["text"]}
                                   for source in requirement_sources(rt.log.read(task_id=task))]
        elif status == "needs_input":
            payload["clarification"] = {"question": "Which audience?", "options": ["Internal", "Public"]}
        text = json.dumps(payload) if status != "invalid" else "not a plan"
        raw = ({"type": "message", "stop_reason": "end_turn", "content": [{"type": "text", "text": text}],
                "usage": {"input_tokens": 4, "output_tokens": 6}} if issubclass(cls, AnthropicMessagesDriver) else
               {"choices": [{"message": {"content": text}}], "usage": {"total_tokens": 10}})
        return io.BytesIO(json.dumps(raw).encode())

    def evidence(self, rt, task):
        return rt.log.emit(type="human.commanded", task_id=task, actor=Actor.human(),
                           payload={"instruction": "Verified process stopped and effects reconciled", "intent": "status"})

    def reconciliation(self, rt, task, started):
        finished = [e for e in self.events(rt, task, "planning.finished")
                    if e.payload["attempt_seq"] == started.seq]
        evidence = self.evidence(rt, task)
        return {"attempt_seq": started.seq, "attempt_hash": started.hash,
                "expected_finish_seq": finished[-1].seq if finished else None,
                "expected_scope": rt.work.scope(task), "expected_plan_seq": rt.work.view(task)["plan_seq"],
                "evidence_seqs": [evidence.seq], "reason": "Operator checked the referenced process evidence",
                "invocation_stopped": True, "effects_reconciled": True}

    def concurrent(self, rt, action):
        barrier = threading.Barrier(2)
        outcomes, errors = [], []

        def run():
            try:
                with Runtime(rt.log.path, roles_path=self.roles, drivers=rt.drivers,
                             close_drivers_on_close=False) as reopened:
                    barrier.wait(timeout=5)
                    outcomes.append(action(Planner(reopened)))
            except WorkConflict as exc:
                outcomes.append(exc)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        return outcomes

    def test_persisted_lease_recovers_after_restart_without_calling_until_later_run(self):
        for cls in (AnthropicMessagesDriver, OpenAICompatibleDriver):
            with self.subTest(adapter=cls.__name__):
                rt, task, _ = self.runtime(cls)
                started, _ = Planner(rt)._start(task, lease_seconds=10)
                self.assertEqual(started.payload["lease_until"], 1010)
                self.assertEqual(started.payload["execution_contract"]["kind"], "builtin_text_api")
                self.assertEqual(started.payload["execution_contract"]["remote_billing"], "unknown")
                self.now = 1010
                with Runtime(rt.log.path, roles_path=self.roles, drivers=rt.drivers,
                             close_drivers_on_close=False) as reopened:
                    with patch(self.transport(cls)) as opened:
                        result = Planner(reopened).run(task)
                        self.assertFalse(result["installed"])
                        opened.assert_not_called()
                    finished = self.events(reopened, task, "planning.finished")
                    self.assertEqual(len(finished), 1)
                    self.assertEqual(finished[0].payload["status"], "interrupted")
                    self.assertEqual(finished[0].payload["evidence_seqs"], [started.seq])
                    self.assertEqual(finished[0].payload["attempt_hash"], started.hash)
                    with patch(self.transport(cls), return_value=self.success(reopened, task, cls)) as opened:
                        self.assertTrue(Planner(reopened).run(task)["installed"])
                        opened.assert_called_once()
                    self.assertTrue(reopened.verify_chain().ok)
                self.now = 1000

    def test_live_lease_blocks_duplicate_call_and_default_covers_configured_timeout(self):
        rt, task, driver = self.runtime()
        driver.timeout = 900
        started, _ = Planner(rt)._start(task)
        self.assertEqual(started.payload["lease_until"], self.now + 930)
        with patch(self.transport(type(driver))) as opened:
            self.assertFalse(Planner(rt).run(task)["installed"])
            opened.assert_not_called()
        self.assertEqual(self.events(rt, task, "planning.finished"), [])

    def test_invalid_lease_does_not_create_claim(self):
        rt, task, _ = self.runtime()
        for value in (0, -1, True, "10", float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Planner(rt)._start(task, lease_seconds=value)
        self.assertEqual(self.events(rt, task, "planning.started"), [])

    def test_cli_custom_and_subclass_expiry_never_unlocks_automatically(self):
        drivers = [CliDriver("planner", ["never-launched"]), PlanningDriver(),
                   DerivedAnthropic("planner", endpoint="https://fixture.invalid", model="fixture",
                                    api_key_env="CATER_PLANNING_FIXTURE"),
                   DerivedOpenAI("planner", endpoint="https://fixture.invalid", model="fixture",
                                 api_key_env="CATER_PLANNING_FIXTURE")]
        for driver in drivers:
            with self.subTest(driver=type(driver).__name__):
                rt, task, _ = self.runtime(driver=driver)
                started, _ = Planner(rt)._start(task, lease_seconds=1)
                self.assertEqual(started.payload["execution_contract"]["kind"], "unknown")
                self.now += 2
                rt.log.emit(type="human.commanded", task_id=task, actor=Actor.human(),
                            payload={"instruction": "Resume", "intent": "resume"})
                rt.log.emit(type="planning.requested", task_id=task, actor=Actor.human(), payload={})
                with patch.object(driver, "complete") as called:
                    self.assertIsNone(Planner(rt).recover(task))
                    self.assertFalse(Planner(rt).run(task)["installed"])
                    called.assert_not_called()
                self.assertEqual(self.events(rt, task, "planning.finished"), [])

    def test_original_type_with_replaced_complete_gets_unknown_contract(self):
        for cls in (AnthropicMessagesDriver, OpenAICompatibleDriver):
            rt, task, driver = self.runtime(cls)
            with patch.object(driver, "complete"):
                started, _ = Planner(rt)._start(task, lease_seconds=1)
            self.assertEqual(started.payload["execution_contract"]["kind"], "unknown")
            self.now += 2
            self.assertIsNone(Planner(rt).recover(task))

    def test_legacy_history_without_contract_stays_unknown_after_driver_reconfiguration(self):
        for finished in (False, True):
            rt, task, _ = self.runtime()
            started = rt.log.emit(type="planning.started", task_id=task, actor=rt._leader_actor(),
                                 payload={"scope": rt.work.scope(task), "previous_plan_seq": None})
            if finished:
                rt.log.emit(type="planning.finished", task_id=task, actor=rt._leader_actor(),
                            payload={"attempt_seq": started.seq, "scope": started.payload["scope"],
                                     "status": "uncertain", "reason": "Legacy unknown result"})
            rt.command(task, "A new direction")
            rt.intake(task)
            before = self.events(rt, task, "planning.finished")
            self.now += 100000
            with patch(self.transport(AnthropicMessagesDriver)) as opened:
                self.assertFalse(Planner(rt).run(task)["installed"])
                opened.assert_not_called()
            self.assertEqual(self.events(rt, task, "planning.finished"), before)

    def test_concurrent_runs_have_one_original_api_invocation(self):
        rt, task, _ = self.runtime()
        body = self.success(rt, task).getvalue()
        with patch(self.transport(AnthropicMessagesDriver), side_effect=lambda *a, **k: io.BytesIO(body)) as opened:
            outcomes = self.concurrent(rt, lambda planner: planner.run(task))
            opened.assert_called_once()
        self.assertEqual(sum(result["installed"] for result in outcomes), 1)
        self.assertEqual(len(self.events(rt, task, "planning.started")), 1)
        self.assertEqual(len(self.events(rt, task, "planning.finished")), 1)

    def test_concurrent_recovery_appends_one_terminal_and_never_calls(self):
        rt, task, _ = self.runtime()
        Planner(rt)._start(task, lease_seconds=1)
        self.now += 2
        with patch(self.transport(AnthropicMessagesDriver)) as opened:
            self.concurrent(rt, lambda planner: planner.recover(task))
            opened.assert_not_called()
        self.assertEqual(len(self.events(rt, task, "planning.finished")), 1)
        self.assertEqual(self.events(rt, task, "work.plan_created"), [])

    def test_inflight_run_cannot_inherit_permission_to_retry_another_runs_429(self):
        for interleaving in ("before_claim", "during_append"):
            with self.subTest(interleaving=interleaving):
                rt, task, _ = self.runtime()
                planner = Planner(rt)
                original, entered = rt.log.emit, False

                def emit(**kwargs):
                    nonlocal entered
                    if kwargs["type"] == "planning.started" and not entered:
                        entered = True
                        self.assertFalse(Planner(rt).run(task)["installed"])
                    return original(**kwargs)

                def recover(task_id):
                    self.assertFalse(Planner(rt).run(task_id)["installed"])
                    return None

                interleave = (patch.object(planner, "recover", side_effect=recover) if interleaving == "before_claim"
                              else patch.object(rt.log, "emit", side_effect=emit))
                error = HTTPError("https://fixture.invalid", 429, "fixture", {}, io.BytesIO())
                with interleave, patch(self.transport(AnthropicMessagesDriver), side_effect=error) as opened:
                    self.assertFalse(planner.run(task)["installed"])
                    opened.assert_called_once()
                self.assertEqual(len(self.events(rt, task, "planning.started")), 1)
                with patch(self.transport(AnthropicMessagesDriver), return_value=self.success(rt, task)) as opened:
                    self.assertTrue(planner.run(task)["installed"])
                    opened.assert_called_once()

    def test_real_adapter_429_and_transport_failures_allow_only_subsequent_run(self):
        for cls in (AnthropicMessagesDriver, OpenAICompatibleDriver):
            for failure in ("429", "timeout", "urlerror", "read_timeout"):
                with self.subTest(adapter=cls.__name__, failure=failure):
                    rt, task, _ = self.runtime(cls)
                    error = (HTTPError("https://fixture.invalid", 429, "fixture", {}, io.BytesIO())
                             if failure == "429" else URLError(TimeoutError("fixture"))
                             if failure == "urlerror" else TimeoutError("fixture"))
                    if failure == "read_timeout":
                        stream = io.BytesIO()
                        self.enterContext(patch.object(stream, "read", side_effect=error))
                        transport_patch = patch(self.transport(cls), return_value=stream)
                    else:
                        transport_patch = patch(self.transport(cls), side_effect=error)
                    with transport_patch as opened:
                        self.assertFalse(Planner(rt).run(task)["installed"])
                        opened.assert_called_once()
                    finished = self.events(rt, task, "planning.finished")[-1]
                    self.assertEqual(finished.payload["status"], "blocked" if failure == "429" else "interrupted")
                    self.assertEqual(finished.payload["recovery"], "text_request_rejected" if failure == "429"
                                     else "text_transport_failure")
                    self.assertEqual(rt._reported_driver_tokens("planner"), 0)
                    receipt = self.events(rt, task, "msg.said")[-1]
                    self.assertEqual(receipt.payload["token_usage"], "unavailable")
                    self.assertEqual(receipt.payload["attempt_seq"], finished.payload["attempt_seq"])
                    if failure == "429":
                        self.assertIn("HTTP 429", finished.payload["reason"])
                        self.assertFalse(rt._is_circuit_open("planner"))
                    with patch(self.transport(cls), return_value=self.success(rt, task, cls)) as opened:
                        self.assertTrue(Planner(rt).run(task)["installed"])
                        opened.assert_called_once()
                    self.assertEqual(rt._reported_driver_tokens("planner"), 10)

    def test_custom_and_cli_failures_or_forged_markers_remain_uncertain(self):
        forged = DriverResponse("{}", meta={"contained": True, "invocation_started": False,
            "_text_request_rejected": runtime_module._TEXT_REQUEST_REJECTED,
            "_text_transport_failure": runtime_module._TEXT_TRANSPORT_FAILURE})
        for driver in (CliDriver("planner", ["never-launched"]), PlanningDriver()):
            for response in (forged, TextRequestRejected("HTTP 429", status_code=429)):
                rt, task, _ = self.runtime(driver=driver)
                kwargs = {"side_effect": response} if isinstance(response, Exception) else {"return_value": response}
                with patch.object(driver, "complete", **kwargs) as called:
                    Planner(rt).run(task)
                    rt.log.emit(type="human.commanded", task_id=task, actor=Actor.human(),
                                payload={"instruction": "Resume", "intent": "resume"})
                    rt.intake(task)
                    Planner(rt).run(task)
                    called.assert_called_once()
                self.assertEqual(self.events(rt, task, "planning.finished")[-1].payload["status"], "uncertain")

    def test_original_cli_timeout_stays_unknown_until_operator_reconciliation(self):
        cli = CliDriver("planner", ["never-launched"])
        rt, task, _ = self.runtime(driver=cli)
        with patch.object(cli, "_request", side_effect=TimeoutError("lost CLI response")) as requested:
            Planner(rt).run(task, lease_seconds=1)
            self.now += 2
            Planner(rt).run(task)
            requested.assert_called_once()
        started = self.events(rt, task, "planning.started")[0]
        self.assertEqual(started.payload["execution_contract"]["kind"], "unknown")
        self.assertEqual(self.events(rt, task, "planning.finished")[-1].payload["status"], "uncertain")
        Planner(rt).reconcile(task, **self.reconciliation(rt, task, started))
        self.assertEqual(len(self.events(rt, task, "planning.started")), 1)

    def test_contract_change_after_claim_is_blocked_before_invocation(self):
        rt, task, driver = self.runtime()
        snapshot = rt.rulebook.snapshot
        calls = []

        def replace(task_id):
            driver.complete = lambda **kwargs: calls.append(kwargs)
            return snapshot(task_id)

        with patch.object(rt.rulebook, "snapshot", side_effect=replace):
            self.assertFalse(Planner(rt).run(task)["installed"])
        self.assertEqual(calls, [])
        self.assertEqual(self.events(rt, task, "planning.finished")[-1].payload["status"], "blocked")

    def test_unclassified_failure_uses_persisted_contract_only_after_expiry(self):
        rt, task, _ = self.runtime()
        error = HTTPError("https://fixture.invalid", 503, "fixture", {}, io.BytesIO())
        with patch(self.transport(AnthropicMessagesDriver), side_effect=error) as opened:
            Planner(rt).run(task, lease_seconds=1)
            Planner(rt).run(task)
            opened.assert_called_once()
        before = self.events(rt, task, "planning.finished")[0]
        self.assertEqual(before.payload["status"], "uncertain")
        self.now += 2
        with patch(self.transport(AnthropicMessagesDriver)) as opened:
            self.assertFalse(Planner(rt).run(task)["installed"])
            opened.assert_not_called()
        recovered = self.events(rt, task, "planning.finished")[-1]
        self.assertEqual(recovered.payload["status"], "interrupted")
        self.assertIn(before.seq, recovered.payload["evidence_seqs"])

    def test_expired_result_is_fenced_even_without_a_recovery_worker(self):
        for status in ("ready", "needs_input", "blocked", "invalid"):
            rt, task, _ = self.runtime()
            response = self.success(rt, task, status=status)

            def late(*args, **kwargs):
                self.now += 2
                return response

            with patch(self.transport(AnthropicMessagesDriver), side_effect=late):
                self.assertFalse(Planner(rt).run(task, lease_seconds=1)["installed"])
            self.assertEqual(self.events(rt, task, "planning.finished")[-1].payload["status"], "interrupted")
            self.assertEqual(self.events(rt, task, "work.plan_created"), [])
            self.assertEqual(self.events(rt, task, "clarification.requested"), [])
            self.assertEqual(rt._reported_driver_tokens("planner"), 10)

    def test_expired_custom_response_requires_operator_check(self):
        driver = PlanningDriver()
        rt, task, _ = self.runtime(driver=driver)
        driver.after = lambda work: setattr(self, "now", self.now + 2)
        self.assertFalse(Planner(rt).run(task, lease_seconds=1)["installed"])
        self.assertEqual(self.events(rt, task, "planning.finished")[-1].payload["status"], "uncertain")
        self.assertFalse(Planner(rt).run(task)["installed"])
        self.assertEqual(len(driver.calls), 1)
        self.assertEqual(self.events(rt, task, "work.plan_created"), [])

    def test_takeover_before_old_reply_prevents_all_late_results_and_preserves_new_plan(self):
        for old_status in ("ready", "needs_input", "blocked", "invalid", "429"):
            with self.subTest(old_status=old_status):
                rt, task, _ = self.runtime()
                old = self.success(rt, task, status=old_status)
                fresh = self.success(rt, task, summary="Replacement plan")
                calls = 0

                def reply(*args, **kwargs):
                    nonlocal calls
                    calls += 1
                    if calls == 2:
                        return fresh
                    self.now += 2
                    recovery = Planner(rt).recover(task)
                    self.assertIsNotNone(recovery)
                    self.assertTrue(Planner(rt).run(task)["installed"])
                    if old_status == "429":
                        raise HTTPError("https://fixture.invalid", 429, "fixture", {}, io.BytesIO())
                    return old

                with patch(self.transport(AnthropicMessagesDriver), side_effect=reply):
                    self.assertFalse(Planner(rt).run(task, lease_seconds=1)["installed"])
                self.assertEqual(calls, 2)
                self.assertEqual([e.payload["status"] for e in self.events(rt, task, "planning.finished")],
                                 ["interrupted", "installed"])
                self.assertEqual(rt.work.view(task)["planning"]["summary"], "Replacement plan")
                self.assertEqual(self.events(rt, task, "clarification.requested"), [])

    def test_takeover_at_conditional_append_fences_plan_and_clarification(self):
        for status, event_type in (("ready", "work.plan_created"), ("needs_input", "clarification.requested")):
            rt, task, _ = self.runtime()
            original, taken = rt.log.emit, False

            def emit(**kwargs):
                nonlocal taken
                if kwargs["type"] == event_type and not taken:
                    taken = True
                    self.now += 2
                    Planner(rt).recover(task)
                return original(**kwargs)

            with patch(self.transport(AnthropicMessagesDriver), return_value=self.success(rt, task, status=status)), \
                    patch.object(rt.log, "emit", side_effect=emit):
                self.assertFalse(Planner(rt).run(task, lease_seconds=1)["installed"])
            self.assertTrue(taken)
            self.assertEqual(self.events(rt, task, event_type), [])
            self.assertEqual(len(self.events(rt, task, "planning.finished")), 1)

    def test_delayed_finish_of_committed_plan_cannot_hide_newer_blocked_attempt(self):
        rt, task, driver = self.runtime(driver=PlanningDriver())
        planner = Planner(rt)
        started, context = planner._start(task)
        parsed = json.loads(driver.complete(role="planner", prompt="fixture", context={"work": context}).text)
        planner._apply(task, started, parsed)
        rt.command(task, "New requirement")
        driver.transform = lambda payload: {"status": "blocked", "summary": "Needs investigation",
                                             "items": [], "coverage": [], "reuse": []}
        self.assertFalse(planner.run(task)["installed"])
        planner._finish(task, started, "installed", "Earlier committed plan")
        self.assertFalse(planner.run(task)["installed"])
        self.assertEqual(len(driver.calls), 2)
        self.assertEqual(len(self.events(rt, task, "planning.started")), 2)

    def test_operator_reconciliation_is_version_bound_and_fences_old_writer(self):
        rt, task, _ = self.runtime(driver=PlanningDriver())
        started, context = Planner(rt)._start(task)
        args = self.reconciliation(rt, task, started)
        event = Planner(rt).reconcile(task, **args)
        self.assertEqual(event.actor, Actor.human())
        self.assertEqual(event.payload["recovery"], "operator_reconciled")
        self.assertEqual(event.payload["evidence_seqs"], args["evidence_seqs"])
        with self.assertRaises(WorkConflict):
            Planner(rt)._finish(task, started, "invalid", "Late response")
        with self.assertRaises(WorkConflict):
            Planner(rt).reconcile(task, **args)
        self.assertTrue(Planner(rt).run(task)["installed"])
        self.assertEqual(self.events(rt, task, "planning.started")[0], started)
        self.assertEqual(started.payload["scope"], context["scope"])
        self.assertTrue(rt.verify_chain().ok)

    def test_operator_reconciles_uncertain_and_legacy_without_rewriting_history(self):
        for legacy in (False, True):
            rt, task, driver = self.runtime(driver=PlanningDriver())
            if legacy:
                started = rt.log.emit(type="planning.started", task_id=task, actor=rt._leader_actor(),
                                     payload={"scope": rt.work.scope(task), "previous_plan_seq": None})
            else:
                with patch.object(driver, "complete", side_effect=TimeoutError("fixture")):
                    Planner(rt).run(task)
                started = self.events(rt, task, "planning.started")[0]
            old_events = rt.log.read(task_id=task)
            args = self.reconciliation(rt, task, started)
            self.concurrent(rt, lambda planner: planner.reconcile(task, **args))
            reconciled = [e for e in self.events(rt, task, "planning.finished")
                          if e.payload.get("recovery") == "operator_reconciled"]
            self.assertEqual(len(reconciled), 1)
            self.assertEqual(rt.log.read(task_id=task)[:len(old_events)], old_events)
            self.assertTrue(Planner(rt).run(task)["installed"])

    def test_reconciliation_rejects_missing_attestation_or_invalid_evidence_and_versions(self):
        rt, task, _ = self.runtime(driver=PlanningDriver())
        started, _ = Planner(rt)._start(task)
        args = self.reconciliation(rt, task, started)
        other = rt.create_task("Other task")
        wrong_task = self.evidence(rt, other)
        model = rt.log.emit(type="msg.said", task_id=task, actor=rt._leader_actor(),
                            payload={"text": "I did not use any tools"})
        changes = ({"invocation_stopped": False}, {"effects_reconciled": False}, {"reason": ""},
                   {"evidence_seqs": []}, {"evidence_seqs": [started.seq]},
                   {"evidence_seqs": [wrong_task.seq]}, {"evidence_seqs": [model.seq]},
                   {"attempt_hash": "wrong"}, {"expected_finish_seq": started.seq},
                   {"expected_scope": {}}, {"expected_plan_seq": 999}, {"attempt_seq": True})
        for change in changes:
            with self.subTest(change=change), self.assertRaises((ValueError, WorkConflict)):
                Planner(rt).reconcile(task, **{**args, **change})
        self.assertEqual(self.events(rt, task, "planning.finished"), [])
        rt.command(task, "Changed direction")
        with self.assertRaises(WorkConflict):
            Planner(rt).reconcile(task, **args)

    def test_scope_change_during_reconciliation_conditional_append_is_rejected(self):
        rt, task, _ = self.runtime(driver=PlanningDriver())
        started, _ = Planner(rt)._start(task)
        args = self.reconciliation(rt, task, started)
        original, changed = rt.log.emit, False

        def emit(**kwargs):
            nonlocal changed
            if kwargs["type"] == "planning.finished" and not changed:
                changed = True
                rt.command(task, "Changed during append")
            return original(**kwargs)

        with patch.object(rt.log, "emit", side_effect=emit), self.assertRaises(WorkConflict):
            Planner(rt).reconcile(task, **args)
        self.assertEqual(self.events(rt, task, "planning.finished"), [])

    def test_foreign_or_misbound_finishes_do_not_unlock_or_overwrite_uncertain(self):
        for prior_uncertain in (False, True):
            rt, task, driver = self.runtime(driver=PlanningDriver())
            started, _ = Planner(rt)._start(task)
            prior = Planner(rt)._finish(task, started, "uncertain", "Unknown call") if prior_uncertain else None
            payload = Planner._finish_payload(started, "blocked", "Untrusted completion")
            changes = [(Actor.agent(started.actor.id, started.actor.role), {}),
                       (Actor.system("unrelated"), {}), (Actor.human(), {}),
                       (started.actor, {"scope": {}}), (started.actor, {"attempt_hash": "wrong"}),
                       (started.actor, {"previous_plan_seq": 999}), (started.actor, {"status": "anything"}),
                       (started.actor, {"attempt_seq": []}), (started.actor, {"attempt_seq": True})]
            if prior_uncertain:
                changes.append((started.actor, {}))
            for actor, change in changes:
                with self.subTest(prior_uncertain=prior_uncertain, actor=actor, change=change):
                    rt.log.emit(type="planning.finished", task_id=task, actor=actor, payload={**payload, **change})
                    _, finishes, applied, pending = _attempt_state(rt.log.read(task_id=task))
                    self.assertEqual(finishes, {started.seq: prior} if prior else {})
                    self.assertEqual(applied, {})
                    self.assertEqual(pending, [started])
                    self.assertFalse(Planner(rt).run(task)["installed"])
            self.assertEqual(driver.calls, [])

    def test_legacy_owner_completions_and_applications_remain_valid_without_new_hash_fields(self):
        for status in ("blocked", "invalid", "stale", "uncertain", "installed", "needs_input"):
            with self.subTest(status=status):
                rt, task, _ = self.runtime(driver=PlanningDriver())
                scope = rt.work.scope(task)
                started = rt.log.emit(type="planning.started", task_id=task, actor=rt._leader_actor(),
                                     payload={"scope": scope, "previous_plan_seq": None})
                plan = None
                if status == "installed":
                    plan = rt.work.plan(task, [item("draft")], role_ids={r.id for r in rt.roles}, source_seq=0,
                        planning={"attempt_seq": started.seq, "summary": "Legacy plan", "coverage": [], "reuse": []})
                elif status == "needs_input":
                    rt.clarifications.request(task, rt.organization.leader.id,
                                              {"question": "Legacy question?", "options": []}, scope=scope)
                finished = rt.log.emit(type="planning.finished", task_id=task, actor=started.actor,
                    payload={"attempt_seq": started.seq, "scope": scope, "status": status, "reason": "Legacy finish",
                             "plan_seq": plan.seq if plan else None})
                _, finishes, applied, pending = _attempt_state(rt.log.read(task_id=task))
                self.assertEqual(finishes, {started.seq: finished})
                self.assertEqual(applied, {started.seq: plan} if plan else {})
                self.assertEqual(pending, [started] if status == "uncertain" else [])
                self.assertTrue(rt.verify_chain().ok)

    def test_plan_and_clarification_application_bind_actor_scope_hash_and_prior_plan(self):
        for status in ("ready", "needs_input"):
            rt, task, driver = self.runtime(driver=PlanningDriver())
            started, context = Planner(rt)._start(task)
            parsed = json.loads(driver.complete(role="planner", prompt="fixture", context={"work": context}).text)
            if status == "needs_input":
                parsed = {"status": status, "clarification": {"question": "Which audience?", "options": []}}
            base = rt.log.read(task_id=task)
            applied = Planner(rt)._apply(task, started, parsed)
            binding_key = "planning" if status == "ready" else None
            binding = applied.payload["planning"] if binding_key else applied.payload
            self.assertEqual(binding["attempt_hash"], started.hash)
            self.assertEqual(binding["previous_plan_seq"], started.payload["previous_plan_seq"])
            legacy = {k: v for k, v in binding.items() if k not in {"attempt_hash", "previous_plan_seq"}}
            legacy_payload = {**applied.payload, "planning": legacy} if binding_key else legacy
            self.assertEqual(_attempt_state(base + [replace(applied, payload=legacy_payload)])[2], {started.seq: replace(applied, payload=legacy_payload)})

            def changed_binding(change):
                return {**applied.payload, "planning": {**binding, **change}} if binding_key else {**binding, **change}

            invalid = [replace(applied, actor=Actor.agent(applied.actor.id, applied.actor.role)),
                       replace(applied, actor=Actor.system("unrelated")),
                       replace(applied, task_id="other-task"),
                       replace(applied, payload={**applied.payload, "scope": {}}),
                       replace(applied, payload=changed_binding({"attempt_hash": "wrong"})),
                       replace(applied, payload=changed_binding({"previous_plan_seq": 999})),
                       replace(applied, payload=changed_binding({"attempt_seq" if binding_key else "planning_attempt_seq": []}))]
            if status == "needs_input":
                invalid.append(replace(applied, payload={**applied.payload, "role_id": "other-leader"}))
            for fake in invalid:
                with self.subTest(status=status, fake=fake):
                    _, finishes, applications, pending = _attempt_state(base + [fake])
                    self.assertEqual(finishes, {})
                    self.assertEqual(applications, {})
                    self.assertEqual(pending, [started])

    def test_expired_recovery_requires_dedicated_actor_hash_contract_and_evidence(self):
        rt, task, _ = self.runtime()
        started, _ = Planner(rt)._start(task, lease_seconds=1)
        base = rt.log.read(task_id=task)
        self.now += 2
        recovered = Planner(rt).recover(task)
        self.assertEqual(_attempt_state(base + [recovered])[1], {started.seq: recovered})
        invalid = [replace(recovered, actor=started.actor), replace(recovered, actor=Actor.human()),
                   replace(recovered, actor=Actor.agent("planning_recovery", "planning_recovery")),
                   replace(recovered, task_id="other-task"),
                   replace(recovered, ts="1970-01-01T00:16:40+00:00")]
        for change in ({"attempt_hash": "wrong"}, {"scope": {}}, {"previous_plan_seq": 999},
                       {"recovery": None}, {"recovery": "unknown"}, {"status": "blocked"},
                       {"execution_contract": {"version": 1, "kind": "unknown"}},
                       {"execution_contract": {**started.payload["execution_contract"], "version": True}},
                       {"evidence_seqs": [float(started.seq)]}, {"evidence_seqs": []}):
            invalid.append(replace(recovered, payload={**recovered.payload, **change}))
        for missing in ("attempt_hash", "previous_plan_seq"):
            invalid.append(replace(recovered, payload={k: v for k, v in recovered.payload.items() if k != missing}))
        for fake in invalid:
            with self.subTest(fake=fake):
                _, finishes, applied, pending = _attempt_state(base + [fake])
                self.assertEqual(finishes, {})
                self.assertEqual(applied, {})
                self.assertEqual(pending, [started])

    def test_operator_recovery_requires_attestation_evidence_and_observed_versions(self):
        rt, task, _ = self.runtime(driver=PlanningDriver())
        started, _ = Planner(rt)._start(task)
        prior = Planner(rt)._finish(task, started, "uncertain", "Call outcome unknown")
        args = self.reconciliation(rt, task, started)
        model = rt.log.emit(type="msg.said", task_id=task, actor=started.actor, payload={"text": "I stopped"})
        base = rt.log.read(task_id=task)
        recovered = Planner(rt).reconcile(task, **args)
        self.assertEqual(_attempt_state(base + [recovered])[1], {started.seq: recovered})
        invalid = [replace(recovered, actor=started.actor),
                   replace(recovered, actor=Actor.system("planning_recovery")),
                   replace(recovered, actor=Actor.agent("human", "human")),
                   replace(recovered, task_id="other-task")]
        for change in ({"attempt_hash": "wrong"}, {"scope": {}}, {"previous_plan_seq": 999},
                       {"recovery": None}, {"recovery": "unknown"}, {"status": "blocked"},
                       {"invocation_stopped": False}, {"effects_reconciled": False},
                       {"reconciled_finish_seq": None}, {"reconciled_plan_seq": 999},
                       {"reconciled_finish_seq": float(prior.seq)},
                       {"reconciled_scope": None}, {"evidence_seqs": [model.seq]},
                       {"evidence_seqs": [recovered.seq]}, {"evidence_seqs": []}):
            invalid.append(replace(recovered, payload={**recovered.payload, **change}))
        for missing in ("attempt_hash", "previous_plan_seq", "reconciled_finish_seq", "reconciled_plan_seq"):
            invalid.append(replace(recovered, payload={k: v for k, v in recovered.payload.items() if k != missing}))
        for fake in invalid:
            with self.subTest(fake=fake):
                _, finishes, applied, pending = _attempt_state(base + [fake])
                self.assertEqual(finishes, {started.seq: prior})
                self.assertEqual(applied, {})
                self.assertEqual(pending, [started])

    def test_valid_terminal_cannot_be_replaced_by_late_finish_or_application(self):
        rt, task, driver = self.runtime(driver=PlanningDriver())
        started, context = Planner(rt)._start(task)
        parsed = json.loads(driver.complete(role="planner", prompt="fixture", context={"work": context}).text)
        base = rt.log.read(task_id=task)
        applied = Planner(rt)._apply(task, started, parsed)
        terminal = replace(applied, type="planning.finished", actor=started.actor,
                           payload=Planner._finish_payload(started, "blocked", "Original terminal"))
        late_finish = replace(terminal, seq=terminal.seq + 1,
                              payload=Planner._finish_payload(started, "uncertain", "Late response"))
        late_apply = replace(applied, seq=terminal.seq + 2)
        _, finishes, applications, pending = _attempt_state(base + [terminal, late_finish, late_apply])
        self.assertEqual(finishes, {started.seq: terminal})
        self.assertEqual(applications, {})
        self.assertEqual(pending, [])

    def test_mismatched_plan_predecessor_and_fake_install_finish_cannot_close_claim(self):
        rt, task, driver = self.runtime(driver=PlanningDriver())
        started, context = Planner(rt)._start(task)
        parsed = json.loads(driver.complete(role="planner", prompt="fixture", context={"work": context}).text)
        base = rt.log.read(task_id=task)
        applied = Planner(rt)._apply(task, started, parsed)
        preceding = replace(applied, payload={**applied.payload, "planning": None})
        late = replace(applied, seq=applied.seq + 1)
        fake_finish = replace(applied, seq=applied.seq + 2, type="planning.finished", actor=started.actor,
                              payload=Planner._finish_payload(started, "installed", "Fake installation", plan_seq=late.seq))
        _, finishes, applications, pending = _attempt_state(base + [preceding, late, fake_finish])
        self.assertEqual(finishes, {})
        self.assertEqual(applications, {})
        self.assertEqual(pending, [started])

    def test_boolean_or_non_integer_contract_versions_never_attest_text_recovery(self):
        rt, task, _ = self.runtime()
        started, _ = Planner(rt)._start(task, lease_seconds=1)
        self.assertTrue(_known_text(started))
        self.now += 2
        recovered = Planner(rt).recover(task)
        for version in (True, False, 1.0, "1", None):
            with self.subTest(version=version):
                malformed = replace(started, payload={**started.payload,
                    "execution_contract": {**started.payload["execution_contract"], "version": version}})
                self.assertFalse(_known_text(malformed))
                _, finishes, applied, pending = _attempt_state([malformed, recovered])
                self.assertEqual(finishes, {})
                self.assertEqual(applied, {})
                self.assertEqual(pending, [malformed])


if __name__ == "__main__":
    unittest.main()
