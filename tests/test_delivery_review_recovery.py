from __future__ import annotations

import copy
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
from cater.artifacts import normalize_deliverables
from cater.delivery import DeliveryService, _review_state
from cater.drivers import CliDriver, DriverResponse, OpenAICompatibleDriver, TextRequestRejected
from cater.events import Actor
from cater.runtime import Runtime
from cater.work import WorkConflict
from test_delivery import DeliveryDriver
from test_work import WorkDriver, item


class DerivedAnthropic(AnthropicMessagesDriver):
    pass


class DerivedOpenAI(OpenAICompatibleDriver):
    pass


class DeliveryReviewRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {"CATER_DELIVERY_FIXTURE": "synthetic-key"}))
        self.enterContext(patch.object(socket, "socket", side_effect=AssertionError("network forbidden")))
        self.enterContext(patch.object(subprocess, "Popen", side_effect=AssertionError("process forbidden")))
        self.now = 1000.0
        self.enterContext(patch("cater.delivery.time", SimpleNamespace(time=lambda: self.now)))
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        config = json.loads(Path("cater/packs/default_roles.json").read_text(encoding="utf-8"))
        config["organization"]["leader"]["driver"] = "final"
        self.roles = self.root / "roles.json"
        self.roles.write_text(json.dumps(config), encoding="utf-8")
        self.count = 0

    def runtime(self, cls=AnthropicMessagesDriver, *, driver=None):
        self.count += 1
        driver = driver or cls("final", endpoint="https://fixture.invalid/v1/messages", model="fixture",
                               api_key_env="CATER_DELIVERY_FIXTURE")
        worker = WorkDriver()
        drivers = {**{name: worker for name in ("product", "dev", "test", "design")}, "final": driver}
        rt = Runtime(self.root / f"delivery-{self.count}.db", roles_path=self.roles, drivers=drivers,
                     driver_attempts=9, circuit_breaker_threshold=20, close_drivers_on_close=False)
        self.addCleanup(rt.close)
        task = rt.create_task("Deliver a reviewed document")
        self.prepare(rt, task)
        return rt, task, driver

    def prepare(self, rt, task, *, local=False):
        rt.plan_work(task, [item("draft", local=local)])
        claim = rt.work.claim(task, "draft")
        body = normalize_deliverables([{"title": "Report", "summary": "Read the source", "filename": "report.md",
                                        "format": "markdown", "content": "# Report\n\nInspect this result."}])[0]
        artifact = rt.log.emit(type="deliverable.published", task_id=task, actor=Actor.agent("dev", "worker"),
            payload={**body, "work_item_id": "draft", "attempt_id": claim.payload["attempt_id"]})
        submission = rt.work.submit(task, claim, [artifact.seq])
        rows = [{"id": "meaning", "status": "passed", "reason": "Read the submitted report", "receipt_seq": None,
                 "artifact_seq": artifact.seq, "quote": body["content"]}]
        if local:
            started = rt.work.start_check(task, "draft", "behavior", lease_seconds=30, expected_submission_seq=submission.seq)
            receipt = rt.log.emit(type="verification.completed", task_id=task, actor=Actor.system("local_verifier"),
                payload={**started.payload, "check_attempt_seq": started.seq, "outcome": "passed", "exit_code": 0,
                         "artifacts_unchanged": True, "stdout": {"complete": True}, "stderr": {"complete": True}})
            rows.append({**rows[0], "id": "behavior", "receipt_seq": receipt.seq})
        rt.work.review(task, "draft", reviewer="test", submission_seq=submission.seq, results=rows)
        view = rt.delivery.prepare(task)
        if local:
            self.check(rt, task)
        return view

    @staticmethod
    def check(rt, task):
        view = rt.delivery.view(task)
        start = rt.delivery._start_check(task, view, "integration_behavior", 60)
        return rt.log.emit(type="verification.completed", task_id=task, actor=Actor.system("local_verifier"),
            payload={"delivery_digest": view["digest"], "criterion_id": "integration_behavior", "check_attempt_seq": start.seq,
                     "outcome": "passed", "exit_code": 0, "artifacts_unchanged": True,
                     "stdout": {"complete": True}, "stderr": {"complete": True}})

    @staticmethod
    def events(rt, task, kind):
        return list(rt.log.iter_events(task_id=task, event_types=(kind,)))

    @staticmethod
    def transport(cls):
        return "cater.anthropic._open_api_request" if cls is AnthropicMessagesDriver else "cater.drivers._open_api_request"

    def response(self, rt, task, cls=AnthropicMessagesDriver):
        view = rt.delivery.view(task)
        context = {"manifest": view["manifest"], "artifacts": [a for item in view["manifest"]["items"]
                   for a in rt.work.artifacts(task, item["submission"])]}
        text = DeliveryDriver().complete(role="fixture", prompt="", context={"work": context}).text
        body = ({"type": "message", "content": [{"type": "text", "text": text}], "stop_reason": "end_turn",
                 "usage": {"input_tokens": 7, "output_tokens": 3}} if cls is AnthropicMessagesDriver else
                {"choices": [{"message": {"content": text}, "finish_reason": "stop"}],
                 "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}})
        return io.BytesIO(json.dumps(body).encode())

    def reconciliation(self, rt, task, started):
        evidence = rt.log.emit(type="human.commanded", task_id=task, actor=Actor.human(),
            payload={"instruction": "Verified invocation stopped and its effects were reconciled", "intent": "status"})
        view = rt.delivery.view(task)
        _, finishes, _ = _review_state(rt.log.read(task_id=task))
        finish = finishes.get(started.seq)
        return {"attempt_seq": started.seq, "attempt_hash": started.hash,
                "expected_finish_seq": finish.seq if finish else None, "expected_delivery_digest": view["digest"],
                "expected_scope": rt.work.scope(task), "expected_plan_seq": rt.work.view(task)["plan_seq"],
                "expected_check_seqs": {k: v.seq for k, v in rt.delivery._checks(task, view["digest"]).items()},
                "evidence_seqs": [evidence.seq], "reason": "Operator checked the invocation and its effects",
                "invocation_stopped": True, "effects_reconciled": True}

    def concurrent(self, rt, action):
        barrier, errors, outcomes = threading.Barrier(2), [], []

        def run():
            try:
                with Runtime(rt.log.path, roles_path=self.roles, drivers=rt.drivers, close_drivers_on_close=False) as reopened:
                    barrier.wait(5)
                    outcomes.append(action(reopened))
            except WorkConflict as exc:
                outcomes.append(exc)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        return outcomes

    def test_expired_text_review_recovers_after_restart_then_later_run_calls_once(self):
        for cls in (AnthropicMessagesDriver, OpenAICompatibleDriver):
            with self.subTest(adapter=cls.__name__):
                rt, task, _ = self.runtime(cls)
                started, _ = rt.delivery._start(task, lease_seconds=10)
                self.assertEqual(started.payload["lease_until"], self.now + 10)
                self.assertEqual(started.payload["execution_contract"]["kind"], "builtin_text_api")
                self.now += 10
                with Runtime(rt.log.path, roles_path=self.roles, drivers=rt.drivers, close_drivers_on_close=False) as reopened:
                    with patch(self.transport(cls)) as opened:
                        result = reopened.delivery.run(task)
                        self.assertEqual(result["status"], "interrupted")
                        opened.assert_not_called()
                    finish = self.events(reopened, task, "delivery.review_finished")[-1]
                    self.assertEqual(finish.actor, Actor.system("delivery_recovery"))
                    self.assertEqual(finish.payload["attempt_hash"], started.hash)
                    self.assertEqual(finish.payload["evidence_seqs"], [started.seq])
                    with patch(self.transport(cls), return_value=self.response(reopened, task, cls)) as opened:
                        self.assertEqual(reopened.delivery.run(task)["status"], "accepted")
                        opened.assert_called_once()
                    self.assertTrue(reopened.verify_chain().ok)

    def test_live_lease_and_invalid_leases_cannot_start_another_call(self):
        rt, task, driver = self.runtime()
        for value in (0, -1, True, "10", float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                rt.delivery._start(task, lease_seconds=value)
        self.assertEqual(self.events(rt, task, "delivery.review_started"), [])
        driver.timeout = 900
        started, _ = rt.delivery._start(task)
        self.assertEqual(started.payload["lease_until"], self.now + 930)
        with patch(self.transport(type(driver))) as opened:
            self.assertIsNone(rt.delivery.recover(task))
            self.assertEqual(rt.delivery.run(task)["status"], "reviewing")
            opened.assert_not_called()

    def test_unknown_cli_custom_subclass_or_replaced_method_never_recovers_by_timeout(self):
        drivers = [CliDriver("final", ["never-launched"]), DeliveryDriver(),
                   DerivedAnthropic("final", endpoint="https://fixture.invalid", model="fixture", api_key_env="CATER_DELIVERY_FIXTURE"),
                   DerivedOpenAI("final", endpoint="https://fixture.invalid", model="fixture", api_key_env="CATER_DELIVERY_FIXTURE")]
        for driver in drivers:
            rt, task, _ = self.runtime(driver=driver)
            started, _ = rt.delivery._start(task, lease_seconds=1)
            self.assertEqual(started.payload["execution_contract"]["kind"], "unknown")
            self.now += 2
            rt.command(task, "resume")
            with patch.object(driver, "complete") as called:
                self.assertIsNone(rt.delivery.recover(task))
                self.assertEqual(rt.delivery.run(task)["status"], "uncertain")
                called.assert_not_called()
        rt, task, driver = self.runtime()
        with patch.object(driver, "complete"):
            started, _ = rt.delivery._start(task, lease_seconds=1)
        self.now += 2
        self.assertEqual(started.payload["execution_contract"]["kind"], "unknown")
        self.assertIsNone(rt.delivery.recover(task))

    def test_legacy_attempt_cannot_inherit_current_text_driver_contract(self):
        for finished in (False, True):
            rt, task, _ = self.runtime()
            view = rt.delivery.view(task)
            started = rt.log.emit(type="delivery.review_started", task_id=task,
                actor=Actor.agent(view["reviewer"], "delivery_reviewer"),
                payload={"delivery_digest": view["digest"], "check_seqs": {}, "lease_until": self.now - 1})
            if finished:
                rt.log.emit(type="delivery.review_finished", task_id=task, actor=started.actor,
                    payload={"delivery_digest": view["digest"], "check_seqs": {}, "attempt_seq": started.seq, "status": "uncertain"})
            with patch(self.transport(AnthropicMessagesDriver)) as opened:
                self.assertIsNone(rt.delivery.recover(task))
                self.assertEqual(rt.delivery.run(task)["status"], "uncertain")
                opened.assert_not_called()
            rt.delivery.reconcile(task, **self.reconciliation(rt, task, started))
            self.assertEqual(rt.delivery.view(task)["status"], "interrupted")

    def test_real_adapter_429_and_transport_sentinels_only_allow_later_run(self):
        for cls in (AnthropicMessagesDriver, OpenAICompatibleDriver):
            for failure in ("429", "timeout", "urlerror", "read_timeout"):
                with self.subTest(adapter=cls.__name__, failure=failure):
                    rt, task, _ = self.runtime(cls)
                    error = (HTTPError("https://fixture.invalid", 429, "fixture", {}, io.BytesIO()) if failure == "429" else
                             URLError(TimeoutError("fixture")) if failure == "urlerror" else TimeoutError("fixture"))
                    if failure == "read_timeout":
                        stream = io.BytesIO()
                        self.enterContext(patch.object(stream, "read", side_effect=error))
                        transport = patch(self.transport(cls), return_value=stream)
                    else:
                        transport = patch(self.transport(cls), side_effect=error)
                    with transport as opened:
                        self.assertEqual(rt.delivery.run(task)["status"], "interrupted")
                        opened.assert_called_once()
                    finish = self.events(rt, task, "delivery.review_finished")[-1]
                    self.assertEqual(finish.payload["recovery"], "text_request_rejected" if failure == "429" else "text_transport_failure")
                    self.assertEqual(rt._reported_driver_tokens("final"), 0)
                    self.assertEqual(self.events(rt, task, "msg.said")[-1].payload["token_usage"], "unavailable")
                    if failure == "429":
                        self.assertFalse(rt._is_circuit_open("final"))
                    with patch(self.transport(cls), return_value=self.response(rt, task, cls)) as opened:
                        self.assertEqual(rt.delivery.run(task)["status"], "accepted")
                        opened.assert_called_once()
                    self.assertEqual(rt._reported_driver_tokens("final"), 10)

    def test_cli_and_custom_forged_sentinels_remain_uncertain(self):
        forged = DriverResponse("{}", meta={"contained": True, "invocation_started": False,
            "_text_request_rejected": runtime_module._TEXT_REQUEST_REJECTED,
            "_text_transport_failure": runtime_module._TEXT_TRANSPORT_FAILURE})
        for driver in (CliDriver("final", ["never-launched"]), DeliveryDriver()):
            for response in (forged, TextRequestRejected("HTTP 429", status_code=429)):
                rt, task, _ = self.runtime(driver=driver)
                kwargs = {"side_effect": response} if isinstance(response, Exception) else {"return_value": response}
                with patch.object(driver, "complete", **kwargs) as called:
                    self.assertEqual(rt.delivery.run(task)["status"], "uncertain")
                    self.now += 10000
                    self.assertEqual(rt.delivery.run(task)["status"], "uncertain")
                    called.assert_called_once()

    def test_original_cli_timeout_stays_locked_until_explicit_reconciliation(self):
        cli = CliDriver("final", ["never-launched"])
        rt, task, _ = self.runtime(driver=cli)
        with patch.object(cli, "_request", side_effect=TimeoutError("lost CLI response")) as request:
            self.assertEqual(rt.delivery.run(task, lease_seconds=1)["status"], "uncertain")
            self.now += 2
            self.assertEqual(rt.delivery.run(task)["status"], "uncertain")
            request.assert_called_once()
        started = self.events(rt, task, "delivery.review_started")[0]
        event = rt.delivery.reconcile(task, **self.reconciliation(rt, task, started))
        self.assertEqual(event.payload["recovery"], "operator_reconciled")
        self.assertEqual(len(self.events(rt, task, "delivery.review_started")), 1)

    def test_overlapping_run_cannot_spend_again_after_another_calls_failure(self):
        for interleaving in ("before_claim", "during_append"):
            rt, task, _ = self.runtime()
            original, entered = rt.log.emit, False

            def emit(**kwargs):
                nonlocal entered
                if kwargs["type"] == "delivery.review_started" and not entered:
                    entered = True
                    self.assertEqual(DeliveryService(rt).run(task)["status"], "interrupted")
                return original(**kwargs)

            def recover(task_id):
                self.assertEqual(DeliveryService(rt).run(task_id)["status"], "interrupted")
                return None

            interleave = (patch.object(rt.delivery, "recover", side_effect=recover) if interleaving == "before_claim"
                          else patch.object(rt.log, "emit", side_effect=emit))
            with interleave, patch(self.transport(AnthropicMessagesDriver), side_effect=TimeoutError("fixture")) as opened:
                self.assertEqual(rt.delivery.run(task)["status"], "interrupted")
                opened.assert_called_once()
            self.assertEqual(len(self.events(rt, task, "delivery.review_started")), 1)

    def test_concurrent_recovery_has_one_terminal_and_no_model_call(self):
        rt, task, _ = self.runtime()
        rt.delivery._start(task, lease_seconds=1)
        self.now += 2
        with patch(self.transport(AnthropicMessagesDriver)) as opened:
            self.concurrent(rt, lambda other: other.delivery.recover(task))
            opened.assert_not_called()
        self.assertEqual(len(self.events(rt, task, "delivery.review_finished")), 1)

    def test_late_return_after_recovery_cannot_accept_or_rework(self):
        rt, task, _ = self.runtime()
        body = self.response(rt, task).getvalue()

        def returning(*args, **kwargs):
            self.now += 2
            rt.delivery.recover(task)
            return io.BytesIO(body)

        with patch(self.transport(AnthropicMessagesDriver), side_effect=returning):
            self.assertEqual(rt.delivery.run(task, lease_seconds=1)["status"], "interrupted")
        self.assertEqual(len(self.events(rt, task, "delivery.review_finished")), 1)
        self.assertEqual(rt._reported_driver_tokens("final"), 10)
        self.assertTrue(rt.work.view(task)["stage_accepted"])
        with patch(self.transport(AnthropicMessagesDriver), return_value=self.response(rt, task)):
            self.assertEqual(rt.delivery.run(task)["status"], "accepted")

    def test_expired_real_response_is_interrupted_for_text_but_unknown_for_custom(self):
        rt, task, _ = self.runtime()
        body = self.response(rt, task).getvalue()

        def returning(*args, **kwargs):
            self.now += 2
            return io.BytesIO(body)

        with patch(self.transport(AnthropicMessagesDriver), side_effect=returning):
            self.assertEqual(rt.delivery.run(task, lease_seconds=1)["status"], "interrupted")
        driver = DeliveryDriver()
        rt, task, _ = self.runtime(driver=driver)
        driver.after = lambda context: setattr(self, "now", self.now + 2)
        self.assertEqual(rt.delivery.run(task, lease_seconds=1)["status"], "uncertain")
        self.assertIsNone(rt.delivery.recover(task))

    def test_contract_change_after_claim_blocks_invocation(self):
        rt, task, driver = self.runtime()
        original = rt.rulebook.snapshot
        calls = []

        def change(task_id):
            driver.complete = lambda **kwargs: calls.append(kwargs)
            return original(task_id)

        with patch.object(rt.rulebook, "snapshot", side_effect=change):
            self.assertEqual(rt.delivery.run(task)["status"], "blocked")
        self.assertEqual(calls, [])

    def test_recovery_during_snapshot_prevents_new_invocation(self):
        rt, task, _ = self.runtime()
        original = rt.rulebook.snapshot

        def change(task_id):
            self.now += 2
            self.assertIsNotNone(rt.delivery.recover(task_id))
            return original(task_id)

        with patch.object(rt.rulebook, "snapshot", side_effect=change), patch(self.transport(AnthropicMessagesDriver)) as opened:
            self.assertEqual(rt.delivery.run(task, lease_seconds=1)["status"], "interrupted")
            opened.assert_not_called()
        self.assertEqual(len(self.events(rt, task, "delivery.review_finished")), 1)

    def test_saved_text_contract_survives_driver_change_without_calling_new_driver(self):
        rt, task, _ = self.runtime()
        rt.delivery._start(task, lease_seconds=1)
        self.now += 2
        driver = DeliveryDriver()
        rt.drivers["final"] = driver
        self.assertEqual(rt.delivery.run(task)["status"], "interrupted")
        self.assertEqual(driver.calls, [])

    def test_recorded_recovery_does_not_change_when_clock_moves_backwards(self):
        rt, task, _ = self.runtime()
        started, _ = rt.delivery._start(task, lease_seconds=1)
        self.now += 2
        recovered = rt.delivery.recover(task)
        events = rt.log.read(task_id=task)
        self.now -= 100
        self.assertEqual(rt.delivery.view(task)["status"], "interrupted")
        early = replace(recovered, ts="1970-01-01T00:16:40+00:00")
        self.assertNotIn(started.seq, _review_state([*events[:-1], early])[1])
        self.now += 10000
        self.assertNotIn(started.seq, _review_state([*events[:-1], early])[1])

    def test_reconcile_then_new_attempt_fences_old_finish_and_keeps_history(self):
        rt, task, _ = self.runtime(driver=DeliveryDriver())
        started, context = rt.delivery._start(task)
        args = self.reconciliation(rt, task, started)
        event = rt.delivery.reconcile(task, **args)
        self.assertEqual(event.actor, Actor.human())
        self.assertEqual(event.payload["check_seqs"], started.payload["check_seqs"])
        with self.assertRaises(WorkConflict):
            rt.delivery.reconcile(task, **args)
        self.assertEqual(rt.delivery.run(task)["status"], "accepted")
        with self.assertRaises(WorkConflict):
            rt.delivery._finish(task, started, context, {"status": "accepted"})
        forged = rt.delivery._finish_payload(started, {"status": "accepted"})
        rt.log.emit(type="delivery.review_finished", task_id=task, actor=started.actor, payload=forged)
        self.assertEqual(_review_state(rt.log.read(task_id=task))[1][started.seq], event)
        self.assertTrue(rt.verify_chain().ok)

    def test_recovery_of_old_digest_releases_only_its_unknown_review(self):
        rt, task, _ = self.runtime(driver=DeliveryDriver())
        started, context = rt.delivery._start(task)
        rt.delivery._finish(task, started, context, {"status": "uncertain", "reason": "Lost result"})
        self.prepare(rt, task)
        self.assertNotEqual(rt.delivery.view(task)["digest"], started.payload["delivery_digest"])
        self.assertEqual(rt.delivery.view(task)["status"], "uncertain")
        args = self.reconciliation(rt, task, started)
        rt.delivery.reconcile(task, **args)
        self.assertEqual(rt.delivery.view(task)["status"], "pending")
        self.assertEqual(rt.delivery.run(task)["status"], "accepted")

    def test_changed_requirements_do_not_turn_unknown_failure_into_stale_success(self):
        rt, task, _ = self.runtime(driver=DeliveryDriver())
        started, context = rt.delivery._start(task)
        rt.command(task, "Change the requirements")
        event = rt.delivery._finish(task, started, context, {"status": "uncertain", "reason": "Lost result"})
        self.assertEqual(event.payload["status"], "uncertain")
        self.prepare(rt, task)
        self.assertEqual(rt.delivery.view(task)["status"], "uncertain")

    def test_reconcile_rejects_stale_binding_invalid_evidence_and_missing_attestation(self):
        rt, task, _ = self.runtime(driver=DeliveryDriver())
        started, _ = rt.delivery._start(task)
        args = self.reconciliation(rt, task, started)
        agent = rt.log.emit(type="msg.said", task_id=task, actor=started.actor, payload={"text": "I checked"})
        unrelated = rt.log.emit(type="brief.generated", task_id=task, actor=Actor.system(), payload={})
        other = rt.create_task("Other task")
        foreign = rt.log.emit(type="human.commanded", task_id=other, actor=Actor.human(), payload={"intent": "status"})
        changes = [{"invocation_stopped": False}, {"effects_reconciled": False}, {"reason": " "},
                   {"attempt_seq": True}, {"attempt_hash": "wrong"}, {"expected_finish_seq": started.seq + 1000},
                   {"expected_delivery_digest": "wrong"}, {"expected_scope": {}}, {"expected_plan_seq": True},
                   {"expected_check_seqs": {"fake": 123}}, {"evidence_seqs": []}, {"evidence_seqs": [started.seq]},
                   {"evidence_seqs": [agent.seq]}, {"evidence_seqs": [unrelated.seq]}, {"evidence_seqs": [foreign.seq]}]
        for change in changes:
            with self.subTest(change=change), self.assertRaises((ValueError, WorkConflict)):
                rt.delivery.reconcile(task, **{**args, **change})
        self.assertEqual(self.events(rt, task, "delivery.review_finished"), [])

    def test_check_changes_before_and_after_reconciliation_never_reuse_old_acceptance(self):
        rt, task, _ = self.runtime(driver=DeliveryDriver())
        self.prepare(rt, task, local=True)
        started, context = rt.delivery._start(task)
        args = self.reconciliation(rt, task, started)
        self.check(rt, task)
        with self.assertRaises(WorkConflict):
            rt.delivery.reconcile(task, **args)
        rt.delivery.reconcile(task, **self.reconciliation(rt, task, started))
        newest = self.check(rt, task)
        with self.assertRaises(WorkConflict):
            rt.delivery._finish(task, started, context, {"status": "accepted"})
        self.assertEqual(rt.delivery.run(task)["check_seqs"], {"integration_behavior": newest.seq})

    def test_reconciliation_rejects_in_flight_check_even_with_fresh_snapshot(self):
        rt, task, _ = self.runtime(driver=DeliveryDriver())
        self.prepare(rt, task, local=True)
        started, _ = rt.delivery._start(task)
        rt.delivery._start_check(task, rt.delivery.view(task), "integration_behavior", 60)
        args = self.reconciliation(rt, task, started)
        self.assertEqual(args["expected_check_seqs"], {})
        with self.assertRaises(WorkConflict):
            rt.delivery.reconcile(task, **args)
        self.assertEqual(self.events(rt, task, "delivery.review_finished"), [])

    def test_check_change_during_reconciliation_append_is_revalidated(self):
        rt, task, _ = self.runtime(driver=DeliveryDriver())
        self.prepare(rt, task, local=True)
        started, _ = rt.delivery._start(task)
        args = self.reconciliation(rt, task, started)
        original, changed = rt.log.emit, False

        def emit(**kwargs):
            nonlocal changed
            if kwargs["type"] == "delivery.review_finished" and not changed:
                changed = True
                self.check(rt, task)
            return original(**kwargs)

        with patch.object(rt.log, "emit", side_effect=emit), self.assertRaises(WorkConflict):
            rt.delivery.reconcile(task, **args)
        self.assertEqual(self.events(rt, task, "delivery.review_finished"), [])

    def test_finish_rejects_mutated_review_context(self):
        rt, task, _ = self.runtime(driver=DeliveryDriver())
        started, context = rt.delivery._start(task)
        for field, value in (("scope", {}), ("reviewer", "dev"), ("delivery_digest", "wrong"),
                             ("local_checks", {"fake": {"seq": 1}}),
                             ("manifest", {**context["manifest"], "plan_seq": -1})):
            bad = copy.deepcopy(context)
            bad[field] = value
            with self.subTest(field=field), self.assertRaises(WorkConflict):
                rt.delivery._finish(task, started, bad, {"status": "accepted"})
        self.assertEqual(self.events(rt, task, "delivery.review_finished"), [])

    def test_concurrent_reconcile_and_finish_has_one_effective_terminal(self):
        rt, task, _ = self.runtime(driver=DeliveryDriver())
        started, context = rt.delivery._start(task)
        args = self.reconciliation(rt, task, started)
        counter = iter((False, True))
        lock = threading.Lock()

        def action(other):
            with lock:
                reconcile = next(counter)
            return (other.delivery.reconcile(task, **args) if reconcile else
                    other.delivery._finish(task, started, context, {"status": "needs_evidence", "reason": "Review incomplete"}))

        self.concurrent(rt, action)
        self.assertEqual(len(self.events(rt, task, "delivery.review_finished")), 1)

    def test_forged_or_mismatched_results_cannot_end_claim_or_unlock_it(self):
        for change in ({"attempt_seq": True}, {"attempt_hash": "wrong"}, {"delivery_digest": "wrong"},
                       {"scope": {}}, {"plan_seq": True}, {"check_seqs": {"invented": 1}}, {"attempt_hash": None},
                       {"execution_contract": {"version": 1, "kind": "unknown"}}, {"status": "invented"},
                       {"status": "interrupted"}, {"recovery": []}):
            rt, task, _ = self.runtime()
            started, _ = rt.delivery._start(task)
            payload = rt.delivery._finish_payload(started, {"status": "accepted"})
            rt.log.emit(type="delivery.review_finished", task_id=task, actor=started.actor, payload={**payload, **change})
            self.assertEqual(rt.delivery.view(task)["status"], "reviewing")
        rt, task, _ = self.runtime()
        started, _ = rt.delivery._start(task)
        for actor in (Actor.agent("dev", "delivery_reviewer"), Actor.system("delivery_recovery"), Actor.human()):
            rt.log.emit(type="delivery.review_finished", task_id=task, actor=actor,
                        payload=rt.delivery._finish_payload(started, {"status": "accepted"}))
        self.assertEqual(rt.delivery.view(task)["status"], "reviewing")

    def test_recovery_reducer_rejects_foreign_evidence_and_malformed_operator_binding(self):
        rt, task, _ = self.runtime(driver=DeliveryDriver())
        started, _ = rt.delivery._start(task)
        rt.delivery.reconcile(task, **self.reconciliation(rt, task, started))
        events = rt.log.read(task_id=task)
        finish = events[-1]
        for change in ({"reconciled_finish_seq": True}, {"reconciled_plan_seq": True},
                       {"reconciled_delivery_digest": "unknown"}, {"reconciled_scope": None},
                       {"reconciled_check_seqs": {"fake": True}}, {"evidence_seqs": [started.seq]},
                       {"invocation_stopped": False}, {"effects_reconciled": False}):
            altered = replace(finish, payload={**finish.payload, **change})
            self.assertNotIn(started.seq, _review_state([*events[:-1], altered])[1])
        evidence_seq = finish.payload["evidence_seqs"][0]
        foreign = [replace(e, task_id="other-task") if e.seq == evidence_seq else e for e in events]
        self.assertNotIn(started.seq, _review_state(foreign)[1])
        self.assertNotIn(started.seq, _review_state([*events[:-1], replace(finish, task_id="other-task")])[1])

    def test_orphan_result_or_mismatched_start_is_not_acceptance(self):
        rt, task, _ = self.runtime()
        view = rt.delivery.view(task)
        actor = Actor.agent(view["reviewer"], "delivery_reviewer")
        rt.log.emit(type="delivery.review_finished", task_id=task, actor=actor,
            payload={"attempt_seq": 10000, "delivery_digest": view["digest"], "check_seqs": {}, "status": "accepted"})
        for change in ({"scope": {}}, {"plan_seq": True}, {"check_seqs": {"a": True}}):
            rt.log.emit(type="delivery.review_started", task_id=task, actor=actor, payload={
                "delivery_digest": view["digest"], "check_seqs": {}, "scope": view["manifest"]["scope"],
                "plan_seq": view["manifest"]["plan_seq"], "lease_until": self.now + 30, **change})
        self.assertEqual(rt.delivery.view(task)["status"], "pending")


if __name__ == "__main__":
    unittest.main()
