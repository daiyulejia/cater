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
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import cater.runtime as runtime_module
from cater.anthropic import AnthropicMessagesDriver
from cater.artifacts import normalize_deliverables
from cater.drivers import CliDriver, DriverResponse, OpenAICompatibleDriver, TextRequestRejected, TextTransportFailure
from cater.events import Actor, HeadChanged
from cater.runtime import Runtime
from cater.work import WorkConflict, WorkLedger
from test_review_repair import ReviewRepairDriver
from test_work import WorkDriver, item


class StageReviewClaimsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "reviews.db"
        self.driver = WorkDriver()
        self.drivers = {name: self.driver for name in ("product", "dev", "test", "design")}
        self.rt = Runtime(self.db, drivers=self.drivers)
        self.addCleanup(self.rt.close)
        self.task = self.rt.create_task("Review the current submitted body")
        self.rt.plan_work(self.task, [item("a"), item("b", deps=["a"])])
        self.submission = self.submit("a")

    def submit(self, name):
        claim = self.rt.work.claim(self.task, name)
        artifact = normalize_deliverables([{"title": "Result", "summary": "Body", "content": "assert True\n",
                                             "format": "code", "filename": "check.py"}])[0]
        event = self.rt.log.emit(type="deliverable.published", actor=Actor.agent("dev", "worker"), task_id=self.task,
                                 payload={**artifact, "work_item_id": name, "attempt_id": claim.payload["attempt_id"]})
        return self.rt.work.submit(self.task, claim, [event.seq])

    def current(self, rt=None, name="a"):
        return (rt or self.rt).work._item(self.task, name)[1]

    def start(self, rt=None, snapshot=None, **kwargs):
        rt = rt or self.rt
        current = snapshot or self.current(rt)
        return rt.work.start_review(self.task, current["id"], reviewer=current["reviewer"],
            submission_seq=current["submission"]["seq"], expected_check_seqs={k: v.seq for k, v in rt.work.checks(self.task, current).items()},
            expected_review_seq=current.get("review_seq"),
            expected_review_attempt_seq=current.get("review_lifecycle", {}).get("attempt_seq"), **kwargs)

    def results(self, status="passed", name="a"):
        current = self.current(name=name)
        artifact = self.rt.work.artifacts(self.task, current["submission"])[0]
        return [{"id": "meaning", "status": status, "reason": "Inspected this exact body",
                 "artifact_seq": artifact["seq"], "quote": artifact["content"], "receipt_seq": None}]

    def review(self, started=None, status="passed"):
        return self.rt.work.review(self.task, "a", reviewer="test", submission_seq=self.submission.seq,
                                   results=self.results(status), review_attempt_seq=started.seq if started else None)

    def finish(self, started, status="completed"):
        return self.rt.work.finish_review(self.task, started.seq, status=status, reason="Recorded response outcome")

    def events(self, kind):
        return [e for e in self.rt.log.read(task_id=self.task) if e.type == kind]

    def test_cas_race_between_independent_connections_reserves_once(self):
        barrier = threading.Barrier(2)

        def contender():
            with Runtime(self.db, drivers=self.drivers) as rt:
                emit = rt.log.emit
                first = True

                def racing_emit(**kwargs):
                    nonlocal first
                    if kwargs["type"] == "work.review_started" and first:
                        first = False
                        barrier.wait(timeout=10)
                    return emit(**kwargs)

                with patch.object(rt.log, "emit", side_effect=racing_emit):
                    try:
                        return self.start(rt).seq
                    except WorkConflict:
                        return None

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(lambda _: contender(), range(2)))
        self.assertEqual(sum(value is not None for value in outcomes), 1)
        self.assertEqual(len(self.events("work.review_started")), 1)
        self.assertTrue(self.rt.verify_chain().ok)

    def test_parallel_runtime_review_makes_one_model_call(self):
        launch, release, entered = threading.Barrier(2), threading.Event(), threading.Event()

        def hold(work):
            entered.set()
            if not release.wait(10):
                raise RuntimeError("test did not release fake model")

        self.driver.after = hold
        snapshot = self.current()

        def contender():
            with Runtime(self.db, drivers=self.drivers) as rt:
                launch.wait(timeout=10)
                rt._review_work_item(rt.task(self.task), copy.deepcopy(snapshot))

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(contender) for _ in range(2)]
            try:
                self.assertTrue(entered.wait(10))
                done, pending = wait(futures, timeout=10, return_when=FIRST_COMPLETED)
                self.assertEqual(len(done), 1)
                self.assertEqual(len(pending), 1)
                for future in done:
                    future.result()
                self.assertEqual(len(self.driver.calls), 1)
                self.assertTrue(self.current()["review_in_flight"])
            finally:
                release.set()
            for future in futures:
                future.result(timeout=10)
        self.assertEqual(len(self.driver.calls), 1)
        self.assertEqual(self.current()["status"], "accepted")
        self.assertFalse(self.current()["review_in_flight"])
        self.assertEqual(len(self.events("work.review_finished")), 1)

    def test_old_snapshot_cannot_reclaim_after_unknown_judgment_completes(self):
        snapshot = self.current()
        started = self.start(snapshot=snapshot)
        self.review(started, "unknown")
        self.finish(started)
        with self.assertRaises(WorkConflict):
            self.start(snapshot=snapshot)
        self.assertEqual(self.current()["status"], "submitted")
        # A deliberate later pass from a fresh snapshot retains existing API behavior.
        self.assertGreater(self.start().seq, started.seq)

    def test_old_snapshot_cannot_reclaim_after_not_invoked(self):
        snapshot = self.current()
        started = self.start(snapshot=snapshot)
        self.finish(started, "not_invoked")
        with self.assertRaises(WorkConflict):
            self.start(snapshot=snapshot)
        self.assertFalse(self.current()["in_flight"])
        self.start()

    def test_verdict_does_not_release_downstream_until_final_finish(self):
        started = self.start()
        review = self.review(started)
        self.assertEqual(review.payload["status"], "accepted")
        self.assertEqual(self.current()["status"], "submitted")
        self.assertEqual(self.current(name="b")["status"], "blocked")
        with self.assertRaises(WorkConflict):
            self.rt.work.claim(self.task, "b")
        self.finish(started)
        self.assertEqual(self.current(name="b")["status"], "ready")

    def test_failed_verdict_does_not_release_repair_until_final_finish(self):
        started = self.start()
        self.review(started, "failed")
        self.assertEqual(self.current()["status"], "submitted")
        with self.assertRaises(WorkConflict):
            self.rt.work.claim(self.task, "a")
        self.finish(started)
        self.assertEqual(self.current()["status"], "ready")
        self.rt.work.claim(self.task, "a")

    def test_manual_review_rework_replan_and_check_revision_respect_reservation(self):
        started = self.start()
        for operation in (
            lambda: self.review(),
            lambda: self.rt.work.rework(self.task, "a", "Replace the body"),
            lambda: self.rt.plan_work(self.task, [item("a")]),
            lambda: self.rt.work.revise_checks(self.task, [{"id": "none", "action": "retire"}], reason="Update",
                                               expected_revision=self.rt.work.check_contract(self.task)["revision"]),
        ):
            with self.subTest(operation=operation), self.assertRaises(WorkConflict):
                operation()
        self.finish(started, "not_invoked")
        self.review()
        self.assertEqual(self.current()["status"], "accepted")

    def test_expiry_and_restart_keep_unknown_claim_without_automatic_takeover(self):
        started = self.start(lease_seconds=1)
        with patch("cater.work.time.time", return_value=started.payload["lease_until"] + 1):
            with Runtime(self.db, drivers=self.drivers) as reopened:
                current = self.current(reopened)
                self.assertEqual(current["status"], "uncertain")
                self.assertTrue(current["review_uncertain"])
                self.assertTrue(current["in_flight"])
                reopened.command(self.task, "继续")
                with self.assertRaises(WorkConflict):
                    self.start(reopened)
                reopened._review_work_item(reopened.task(self.task), current)
        self.assertEqual(self.driver.calls, [])

    def test_late_real_response_can_complete_expired_but_unreconciled_claim(self):
        started = self.start(lease_seconds=1)
        with patch("cater.work.time.time", return_value=started.payload["lease_until"] + 1):
            self.review(started)
            self.finish(started)
        self.assertEqual(self.current()["status"], "accepted")

    def test_explicit_reconciliation_discards_partial_results_and_fences_late_owner(self):
        started = self.start()
        self.review(started)
        self.finish(started, "uncertain")
        with self.assertRaises(ValueError):
            self.rt.work.reconcile_review(self.task, started.seq, reason="Checked", invocation_stopped=False)
        self.rt.work.reconcile_review(self.task, started.seq,
            reason="Operator confirmed process stopped and remote usage accounted for; discard incomplete judgment",
            invocation_stopped=True)
        self.assertFalse(self.current()["in_flight"])
        self.assertEqual(self.current()["status"], "submitted")
        self.assertEqual(self.current()["checks"], [])
        with self.assertRaises(WorkConflict):
            self.review(started)
        with self.assertRaises(WorkConflict):
            self.finish(started)
        replacement = self.start()
        self.review(replacement)
        self.finish(replacement)
        self.assertEqual(self.current()["status"], "accepted")

    def test_direction_change_rejects_old_verdict_and_records_stale_finish(self):
        started = self.start()
        self.rt.command(self.task, "Add a new acceptance requirement")
        with self.assertRaises(WorkConflict):
            self.review(started)
        finish = self.finish(started)
        self.assertEqual(finish.payload["status"], "stale")
        self.assertFalse(self.current()["in_flight"])
        self.assertEqual(self.current()["status"], "stale")

    def test_unknown_call_remains_locked_even_when_direction_changes(self):
        started = self.start()
        self.rt.command(self.task, "Change the required output")
        self.assertEqual(self.finish(started, "uncertain").payload["status"], "uncertain")
        self.assertTrue(self.current()["in_flight"])
        with self.assertRaises(WorkConflict):
            self.rt.plan_work(self.task, [item("a")])
        self.rt.work.reconcile_review(self.task, started.seq, reason="Confirmed stopped and reconciled", invocation_stopped=True)
        self.rt.plan_work(self.task, [item("a")])

    def test_forged_or_wrong_binding_finish_does_not_unlock(self):
        started = self.start()
        payload = {**started.payload, "attempt_seq": started.seq, "status": "completed", "reason": "Not authoritative"}
        self.rt.log.emit(type="work.review_finished", actor=Actor.human(), task_id=self.task, payload=payload)
        self.rt.log.emit(type="work.review_finished", actor=started.actor, task_id=self.task,
                         payload={**payload, "submission_digest": "wrong"})
        self.assertTrue(self.current()["review_in_flight"])
        with self.assertRaises(WorkConflict):
            self.start()
        self.review(started)
        self.finish(started)
        self.assertEqual(self.current()["status"], "accepted")

    def test_finish_and_manual_review_cannot_target_other_task_or_item(self):
        started = self.start()
        other = self.rt.create_task("Another task")
        with self.assertRaises(WorkConflict):
            self.rt.work.finish_review(other, started.seq, status="completed", reason="Wrong task")
        with self.assertRaises(WorkConflict):
            self.rt.work.review(self.task, "b", reviewer="test", submission_seq=self.submission.seq,
                                results=[], review_attempt_seq=started.seq)

    def test_completed_without_a_judgment_is_explicit_invalid_response(self):
        started = self.start()
        result = self.finish(started)
        self.assertEqual(result.payload["status"], "invalid_response")
        self.assertFalse(self.current()["in_flight"])
        self.assertEqual(self.current()["status"], "submitted")

    def test_cas_finish_rechecks_direction_before_publishing(self):
        started = self.start()
        self.review(started)
        emit = self.rt.log.emit
        changed = False

        def raced_emit(**kwargs):
            nonlocal changed
            if kwargs["type"] == "work.review_finished" and not changed:
                changed = True
                self.rt.command(self.task, "Different requested output")
            return emit(**kwargs)

        with patch.object(self.rt.log, "emit", side_effect=raced_emit):
            result = self.finish(started)
        self.assertEqual(result.payload["status"], "stale")
        self.assertNotEqual(self.current()["status"], "accepted")

    def test_concurrent_finish_and_reconciliation_have_one_terminal_winner(self):
        started = self.start()
        self.review(started)
        barrier = threading.Barrier(2)

        def finish_or_reconcile(reconcile):
            with Runtime(self.db, drivers=self.drivers) as rt:
                emit = rt.log.emit
                first = True

                def racing_emit(**kwargs):
                    nonlocal first
                    if kwargs["type"] in {"work.review_finished", "work.review_reconciled"} and first:
                        first = False
                        barrier.wait(timeout=10)
                    return emit(**kwargs)

                with patch.object(rt.log, "emit", side_effect=racing_emit):
                    try:
                        if reconcile:
                            return rt.work.reconcile_review(self.task, started.seq, invocation_stopped=True,
                                reason="Operator confirmed invocation ended and discarded response").type
                        return rt.work.finish_review(self.task, started.seq, status="completed", reason="Known response").type
                    except WorkConflict:
                        return None

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(finish_or_reconcile, (False, True)))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertFalse(self.current()["in_flight"])
        self.assertEqual(self.current()["status"], "accepted" if "work.review_finished" in results else "submitted")
        self.assertTrue(self.rt.verify_chain().ok)

    def test_manual_unknown_judgment_fences_model_with_older_snapshot(self):
        snapshot = self.current()
        self.review(status="unknown")
        self.rt._review_work_item(self.rt.task(self.task), snapshot)
        self.assertEqual(self.driver.calls, [])
        self.assertEqual(self.events("work.review_started"), [])

    def test_final_append_failure_leaves_reservation_for_reconciliation(self):
        emit = self.rt.log.emit

        def unavailable(**kwargs):
            if kwargs["type"] == "work.review_finished":
                raise HeadChanged("simulated contention")
            return emit(**kwargs)

        with patch.object(self.rt.log, "emit", side_effect=unavailable), self.assertRaises(WorkConflict):
            self.rt._review_work_item(self.rt.task(self.task), self.current())
        self.assertTrue(self.current()["review_in_flight"])
        self.assertEqual(self.current()["status"], "submitted")
        self.assertEqual(self.current(name="b")["status"], "blocked")

    def test_actual_driver_exception_becomes_unknown_and_cannot_reinvoke(self):
        with patch.object(self.rt, "_complete_with_containment", side_effect=RuntimeError("lost transport")):
            with self.assertRaises(RuntimeError):
                self.rt._review_work_item(self.rt.task(self.task), self.current())
        self.assertEqual(self.events("work.review_finished")[-1].payload["status"], "uncertain")
        self.rt._review_work_item(self.rt.task(self.task), self.current())
        self.assertEqual(self.driver.calls, [])

    def test_contained_invoked_result_stays_unknown_but_noninvoked_is_recoverable(self):
        for invoked in (True, False, None):
            with self.subTest(invoked=invoked):
                current = self.current()
                meta = {"contained": True}
                if invoked is not None:
                    meta["invocation_started"] = invoked
                with patch.object(self.rt, "_complete_with_containment", return_value=DriverResponse("{}", meta=meta)):
                    self.rt._review_work_item(self.rt.task(self.task), current)
                result = self.events("work.review_finished")[-1]
                self.assertEqual(result.payload["status"], "not_invoked" if invoked is False else "uncertain")
                self.assertEqual(self.current()["review_in_flight"], invoked is not False)
                if invoked is not False:
                    self.rt.work.reconcile_review(self.task, result.payload["attempt_seq"],
                        reason="Test operator checked invocation ended", invocation_stopped=True)

    def test_format_repair_retains_one_claim_until_second_response(self):
        driver = ReviewRepairDriver()
        self.rt.drivers["test"] = driver
        phases = []

        def inspect(work):
            current = self.current()
            phases.append((current["status"], current["review_in_flight"], len(self.events("work.review_finished"))))
            with self.assertRaises(WorkConflict):
                self.start()

        driver.after = inspect
        self.rt._review_work_item(self.rt.task(self.task), self.current())
        self.assertEqual(driver.review_count, 2)
        self.assertEqual(phases, [("submitted", True, 0), ("submitted", True, 0)])
        self.assertEqual(len(self.events("work.review_started")), 1)
        self.assertEqual(len(self.events("work.review_finished")), 1)
        self.assertEqual(self.current()["status"], "accepted")
        self.assertEqual(self.rt._reported_driver_tokens("test"), 14)

    def test_new_check_receipt_before_claim_rejects_old_prompt(self):
        self.rt.plan_work(self.task, [item("a", local=True)])
        self.submission = self.submit("a")
        current = self.current()
        self.emit_legacy_check(current)
        with self.assertRaises(WorkConflict):
            self.rt.work.start_review(self.task, "a", reviewer="test", submission_seq=self.submission.seq,
                                      expected_check_seqs={})

    def emit_legacy_check(self, current):
        return self.rt.log.emit(type="verification.completed", actor=Actor.system("local_verifier"), task_id=self.task,
            payload={"work_item_id": "a", "criterion_id": "behavior", "submission_digest": current["submission"]["digest"],
                     "outcome": "passed", "exit_code": 0, "artifacts_unchanged": True,
                     "stdout": {"complete": True}, "stderr": {"complete": True}})

    def test_receipt_change_during_model_call_discards_old_verdict(self):
        self.rt.plan_work(self.task, [item("a", local=True)])
        self.submission = self.submit("a")
        self.emit_legacy_check(self.current())
        started = self.start()
        with self.assertRaises(WorkConflict):
            self.rt.work.start_check(self.task, "a", "behavior")
        self.emit_legacy_check(self.current())
        with self.assertRaises(WorkConflict):
            self.review(started)
        self.assertEqual(self.finish(started).payload["status"], "stale")
        self.assertEqual(self.current()["status"], "submitted")

    def test_new_receipt_after_verdict_before_finish_does_not_release_old_acceptance(self):
        self.rt.plan_work(self.task, [item("a", local=True), item("b", deps=["a"])])
        self.submission = self.submit("a")
        receipt = self.emit_legacy_check(self.current())
        started = self.start()
        rows = self.results() + [{**self.results()[0], "id": "behavior", "receipt_seq": receipt.seq}]
        self.rt.work.review(self.task, "a", reviewer="test", submission_seq=self.submission.seq,
                            results=rows, review_attempt_seq=started.seq)
        self.emit_legacy_check(self.current())
        self.assertEqual(self.finish(started).payload["status"], "stale")
        self.assertEqual(self.current()["status"], "submitted")
        self.assertEqual(self.current(name="b")["status"], "blocked")

    def test_organization_fingerprint_is_optional_and_invalidates_existing_plan(self):
        legacy = WorkLedger(self.rt.log, self.rt.rulebook)
        self.assertNotIn("organization", legacy.scope(self.task))
        bound = WorkLedger(self.rt.log, self.rt.rulebook, organization_fingerprint="organization-v1")
        self.assertEqual(bound.scope(self.task)["organization"], "organization-v1")
        self.assertFalse(bound.view(self.task)["scope_current"])
        bound.plan(self.task, [item("a")], role_ids={"dev", "test"}, source_seq=self.submission.seq)
        self.assertTrue(bound.view(self.task)["scope_current"])
        reopened = WorkLedger(self.rt.log, self.rt.rulebook, organization_fingerprint="organization-v2")
        self.assertFalse(reopened.view(self.task)["scope_current"])

    def test_organization_change_rejects_old_review_and_keeps_uncertain_claim(self):
        started = self.start()
        self.rt.work.organization_fingerprint = "new-organization"
        with self.assertRaises(WorkConflict):
            self.review(started)
        self.finish(started, "uncertain")
        self.assertTrue(self.current()["review_in_flight"])

    def test_parent_recheck_retains_and_invalidates_child_review_reservation(self):
        self.rt.plan_work(self.task, [item("a", local=True), item("b", deps=["a"])])
        self.submission = self.submit("a")
        receipt = self.emit_legacy_check(self.current())
        rows = self.results() + [{**self.results()[0], "id": "behavior", "receipt_seq": receipt.seq}]
        self.rt.work.review(self.task, "a", reviewer="test", submission_seq=self.submission.seq, results=rows)
        child_submission = self.submit("b")
        started = self.start(snapshot=self.current(name="b"))
        self.rt.work.start_check(self.task, "a", "behavior")
        child = self.current(name="b")
        self.assertTrue(child["review_in_flight"])
        self.assertEqual(child["submission"]["seq"], child_submission.seq)
        with self.assertRaises(WorkConflict):
            self.rt.work.review(self.task, "b", reviewer="test", submission_seq=child_submission.seq,
                                results=self.results(name="b"), review_attempt_seq=started.seq)
        self.assertEqual(self.finish(started).payload["status"], "stale")
        self.assertFalse(self.current(name="b")["review_in_flight"])
        self.assertEqual(self.current(name="b")["status"], "blocked")

    def test_invalid_leases_never_write_a_reservation(self):
        for lease in (True, 0, -1, float("nan"), float("inf"), 3601):
            with self.subTest(lease=lease), self.assertRaises(ValueError):
                self.start(lease_seconds=lease)
        self.assertEqual(self.events("work.review_started"), [])


class StageReviewRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {"CATER_STAGE_REVIEW_FIXTURE": "synthetic-key"}))
        self.enterContext(patch.object(socket, "socket", side_effect=AssertionError("network forbidden")))
        self.enterContext(patch.object(subprocess, "Popen", side_effect=AssertionError("process forbidden")))
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime_count = 0

    def runtime(self, cls=AnthropicMessagesDriver, *, driver=None):
        self.runtime_count += 1
        driver = driver or cls("test", endpoint="https://fixture.invalid/v1/messages", model="fixture",
                               api_key_env="CATER_STAGE_REVIEW_FIXTURE")
        rt = Runtime(Path(self.temp.name) / f"review-{self.runtime_count}.db",
                     drivers={"dev": WorkDriver(), "test": driver}, driver_attempts=9,
                     circuit_breaker_threshold=10)
        self.addCleanup(rt.close)
        task = rt.create_task("Independently review a small submitted artifact")
        rt.plan_work(task, [item("draft")])
        return rt, task, driver

    @staticmethod
    def current(rt, task):
        return rt.work.view(task)["items"][0]

    @staticmethod
    def events(rt, task, kind):
        return [e for e in rt.log.read(task_id=task) if e.type == kind]

    @staticmethod
    def transport(cls):
        return "cater.anthropic._open_api_request" if issubclass(cls, AnthropicMessagesDriver) else "cater.drivers._open_api_request"

    def success(self, rt, task, cls, *, bad_quote=False):
        current = self.current(rt, task)
        artifact = rt.work.artifacts(task, current["submission"])[0]
        text = json.dumps({"checks": [{"id": "meaning", "status": "passed", "reason": "Checked this exact body",
            "artifact_seq": artifact["seq"], "quote": "not in this body" if bad_quote else artifact["content"],
            "receipt_seq": None}]})
        raw = ({"type": "message", "stop_reason": "end_turn", "content": [{"type": "text", "text": text}],
                "usage": {"input_tokens": 4, "output_tokens": 6}} if issubclass(cls, AnthropicMessagesDriver) else
               {"choices": [{"message": {"content": text}}], "usage": {"total_tokens": 10}})
        return io.BytesIO(json.dumps(raw).encode())

    def test_real_text_failures_resume_after_restart_without_reexecuting_artifact(self):
        for cls in (AnthropicMessagesDriver, OpenAICompatibleDriver):
            for failure in ("429", "timeout", "urlerror", "read_timeout", "502", "503", "504"):
                with self.subTest(adapter=cls.__name__, failure=failure):
                    rt, task, _ = self.runtime(cls)
                    error = (HTTPError("https://fixture.invalid", int(failure), "fixture", {}, io.BytesIO())
                             if failure.isdigit() else URLError(TimeoutError("fixture"))
                             if failure == "urlerror" else TimeoutError("fixture"))
                    if failure == "read_timeout":
                        stream = io.BytesIO()
                        self.enterContext(patch.object(stream, "read", side_effect=error))
                        transport_patch = patch(self.transport(cls), return_value=stream)
                    else:
                        transport_patch = patch(self.transport(cls), side_effect=error)
                    with transport_patch as opened:
                        rt.collaborate(task)
                        opened.assert_called_once()
                    current = self.current(rt, task)
                    submission_seq = current["submission"]["seq"]
                    self.assertEqual(current["status"], "submitted")
                    self.assertFalse(current["review_in_flight"])
                    self.assertFalse(current["review_uncertain"])
                    self.assertEqual(current["review_lifecycle"]["status"], "recoverable")
                    finished = self.events(rt, task, "work.review_finished")[-1]
                    self.assertEqual(finished.payload["recovery"], "text_request_rejected" if failure == "429"
                                     else "text_transport_failure")
                    for text in ("本地无工具副作用", "远端是否计费未知", "等待用户继续", "不会自动重试"):
                        self.assertIn(text, current["reason"])
                    message = self.events(rt, task, "msg.said")[-1]
                    self.assertEqual(message.payload["token_usage"], "unavailable")
                    self.assertEqual(message.payload["speech_source"], "system_receipt")
                    self.assertIn("HTTP 429" if failure == "429" else "传输失败", message.payload["text"])
                    self.assertEqual(rt._reported_driver_tokens("test"), 0)
                    self.assertEqual(len(self.events(rt, task, "work.review_started")), 1)
                    self.assertFalse(rt._is_circuit_open("test"))
                    with Runtime(rt.log.path, drivers=rt.drivers) as reopened:
                        self.assertFalse(self.current(reopened, task)["in_flight"])
                        reopened.command(task, "继续")
                        with patch(self.transport(cls), return_value=self.success(reopened, task, cls)) as opened:
                            reopened.collaborate(task)
                            opened.assert_called_once()
                        self.assertEqual(self.current(reopened, task)["submission"]["seq"], submission_seq)
                        self.assertTrue(reopened.work.view(task)["stage_accepted"])
                        self.assertEqual(reopened._reported_driver_tokens("test"), 10)
                    self.assertEqual(len(self.events(rt, task, "work.claimed")), 1)
                    self.assertEqual(len(self.events(rt, task, "work.review_started")), 2)
                    self.assertEqual([e.payload["status"] for e in self.events(rt, task, "work.review_finished")],
                                     ["recoverable", "completed"])
                    self.assertEqual(self.events(rt, task, "work.review_reconciled"), [])
                    self.assertTrue(rt.verify_chain().ok)

    def test_transport_failure_during_format_repair_releases_only_after_final_call(self):
        for cls in (AnthropicMessagesDriver, OpenAICompatibleDriver):
            with self.subTest(adapter=cls.__name__):
                rt, task, _ = self.runtime(cls)
                calls = 0

                def transport(*args, **kwargs):
                    nonlocal calls
                    calls += 1
                    self.assertTrue(self.current(rt, task)["review_in_flight"])
                    self.assertEqual(self.events(rt, task, "work.review_finished"), [])
                    if calls == 1:
                        return self.success(rt, task, cls, bad_quote=True)
                    raise TimeoutError("correction response lost")

                with patch(self.transport(cls), side_effect=transport) as opened:
                    rt.collaborate(task)
                    self.assertEqual(opened.call_count, 2)
                self.assertEqual(len(self.events(rt, task, "work.review_started")), 1)
                self.assertEqual(self.events(rt, task, "work.review_finished")[-1].payload["status"], "recoverable")
                self.assertFalse(self.current(rt, task)["in_flight"])
                self.assertEqual(self.current(rt, task)["status"], "submitted")
                self.assertEqual(rt._reported_driver_tokens("test"), 10)
                rt.command(task, "继续")
                with patch(self.transport(cls), return_value=self.success(rt, task, cls)) as opened:
                    rt.collaborate(task)
                    opened.assert_called_once()
                self.assertTrue(rt.work.view(task)["stage_accepted"])

    def test_cli_timeout_remains_locked_after_resume(self):
        cli = CliDriver("test", ["never-launched"])
        rt, task, _ = self.runtime(driver=cli)
        with patch.object(cli, "_request", side_effect=TimeoutError("CLI response lost")) as requested:
            rt.collaborate(task)
            rt.command(task, "继续")
            rt.collaborate(task)
            requested.assert_called_once()
        self.assertEqual(self.events(rt, task, "work.review_finished")[-1].payload["status"], "uncertain")
        self.assertTrue(self.current(rt, task)["review_in_flight"])
        self.assertTrue(self.current(rt, task)["review_uncertain"])

    def test_forged_sentinels_and_custom_typed_errors_remain_unknown(self):
        for factory in (WorkDriver, lambda: CliDriver("test", ["never-launched"])):
            for outcome in (DriverResponse("{}", meta={"contained": True, "invocation_started": False,
                                "_text_request_rejected": runtime_module._TEXT_REQUEST_REJECTED,
                                "_text_transport_failure": runtime_module._TEXT_TRANSPORT_FAILURE}),
                            TextRequestRejected("HTTP 429", status_code=429), TextTransportFailure("lost response")):
                with self.subTest(driver=factory, outcome=type(outcome).__name__):
                    driver = factory()
                    rt, task, _ = self.runtime(driver=driver)
                    kwargs = {"side_effect": outcome} if isinstance(outcome, Exception) else {"return_value": outcome}
                    with patch.object(driver, "complete", **kwargs) as called:
                        rt.collaborate(task)
                        rt.command(task, "继续")
                        rt.collaborate(task)
                        called.assert_called_once()
                    finished = self.events(rt, task, "work.review_finished")[-1]
                    self.assertEqual(finished.payload["status"], "uncertain")
                    self.assertNotIn("recovery", finished.payload)
                    self.assertTrue(self.current(rt, task)["review_in_flight"])

    def test_subclasses_replaced_methods_and_non_text_requests_get_no_exemption(self):
        for cls in (AnthropicMessagesDriver, OpenAICompatibleDriver):
            class Derived(cls):
                pass

            with self.subTest(adapter=cls.__name__, boundary="subclass"):
                rt, task, _ = self.runtime(Derived)
                with patch(self.transport(cls), side_effect=TimeoutError("lost response")) as opened:
                    rt.collaborate(task)
                    opened.assert_called_once()
                self.assertEqual(self.events(rt, task, "work.review_finished")[-1].payload["status"], "uncertain")
            with self.subTest(adapter=cls.__name__, boundary="replaced_method"):
                rt, task, driver = self.runtime(cls)
                with patch.object(driver, "complete", side_effect=TextTransportFailure("synthetic")) as called:
                    rt.collaborate(task)
                    called.assert_called_once()
                self.assertTrue(self.current(rt, task)["review_uncertain"])
            with self.subTest(adapter=cls.__name__, boundary="non_text_request"):
                rt, task, _ = self.runtime(cls)
                module = "cater.anthropic" if cls is AnthropicMessagesDriver else "cater.drivers"
                with patch(module + "._text_only_request", return_value=False), \
                        patch(self.transport(cls), side_effect=TimeoutError("lost response")) as opened:
                    rt.collaborate(task)
                    opened.assert_called_once()
                self.assertTrue(self.current(rt, task)["review_uncertain"])

    def test_permanent_http_failures_do_not_release_reservation(self):
        for cls in (AnthropicMessagesDriver, OpenAICompatibleDriver):
            for status in (401, 403, 501):
                with self.subTest(adapter=cls.__name__, status=status):
                    rt, task, _ = self.runtime(cls)
                    with patch(self.transport(cls), side_effect=HTTPError("https://fixture.invalid", status, "fixture", {}, None)):
                        rt.collaborate(task)
                    self.assertEqual(self.events(rt, task, "work.review_finished")[-1].payload["status"], "uncertain")
                    self.assertTrue(self.current(rt, task)["review_in_flight"])


if __name__ == "__main__":
    unittest.main()
