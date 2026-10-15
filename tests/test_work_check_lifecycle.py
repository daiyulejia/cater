from __future__ import annotations

import itertools
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from cater.artifacts import normalize_deliverables
from cater.events import Actor, EventLog
from cater.rules import RuleBook
from cater.work import WorkConflict, WorkLedger


def item(name, *, deps=(), local=True):
    criterion = {"id": "behavior", "kind": "local_check", "text": "Run the submitted test"} if local else {
        "id": "meaning", "kind": "review", "text": "Inspect the submitted result"}
    return {"id": name, "owner": "dev", "reviewer": "test", "objective": "Deliver " + name,
            "depends_on": list(deps), "criteria": [criterion]}


class WorkCheckLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "checks.db"
        self.log = EventLog(self.db)
        self.addCleanup(self.log.close)
        self.rules = RuleBook(self.log)
        self.work = WorkLedger(self.log, self.rules)
        self.create_plan()

    def create_plan(self, items=None):
        self.task = "task-" + str(self.log.count())
        self.log.emit(type="task.created", actor=Actor.human(), task_id=self.task,
                      payload={"goal": "Check lifecycle", "acceptance": []})
        self.plan = self.work.plan(self.task, items or [item("a"), item("b", deps=["a"], local=False)],
                                   role_ids={"dev", "test"}, source_seq=0)

    def current(self, name="a"):
        return self.work._item(self.task, name)[1]

    def statuses(self):
        return {i["id"]: i["status"] for i in self.work.view(self.task)["items"]}

    def publish(self, claim):
        artifact = normalize_deliverables([{"title": "Result", "summary": "Body", "content": "assert True\n",
                                           "format": "code", "filename": "check.py"}])[0]
        return self.log.emit(type="deliverable.published", actor=Actor.agent("dev", "worker"), task_id=self.task,
                             payload={**artifact, "work_item_id": claim.payload["work_item_id"],
                                      "attempt_id": claim.payload["attempt_id"]})

    def submit(self, name="a"):
        claim = self.work.claim(self.task, name)
        artifact = self.publish(claim)
        return self.work.submit(self.task, claim, [artifact.seq])

    def review(self, name="a"):
        current = self.current(name)
        artifact = self.work.artifacts(self.task, current["submission"])[0]
        local = self.work.checks(self.task, current)
        return self.work.review(self.task, name, reviewer="test", submission_seq=current["submission"]["seq"],
                                results=[{"id": c["id"], "status": "passed", "reason": "Inspected the result and check scope",
                                          "artifact_seq": artifact["seq"], "quote": "assert True",
                                          "receipt_seq": local[c["id"]].seq if c["id"] in local else None}
                                         for c in current["criteria"]])

    def receipt(self, start=None, *, passed=True, actor=None, overrides=None, name="a", task_id=None):
        if start:
            p = start.payload
            binding = {key: p[key] for key in ("work_item_id", "criterion_id", "submission_digest", "submission_seq")}
            binding["check_attempt_seq"] = start.seq
        else:
            submission = self.current(name)["submission"]
            binding = {"work_item_id": name, "criterion_id": "behavior", "submission_digest": submission["digest"]}
        payload = {**binding, "outcome": "passed" if passed else "failed", "exit_code": 0 if passed else 1,
                   "artifacts_unchanged": True, "stdout": {"complete": True}, "stderr": {"complete": True},
                   **(overrides or {})}
        return self.log.emit(type="verification.completed", actor=actor or Actor.system("local_verifier"),
                             task_id=task_id or self.task, payload=payload)

    def accept(self, name="a"):
        self.submit(name)
        if any(c["kind"] == "local_check" for c in self.current(name)["criteria"]):
            self.receipt(name=name)
        self.assertEqual(self.review(name).payload["status"], "accepted")

    def test_start_is_bound_to_submission_and_blocks_old_acceptance(self):
        self.accept()
        before = self.current()
        start = self.work.start_check(self.task, "a", "behavior")
        p = start.payload
        self.assertEqual(start.actor, Actor.system("local_verifier"))
        self.assertEqual(p["plan_seq"], self.plan.seq)
        self.assertEqual(p["submission_seq"], before["submission"]["seq"])
        self.assertEqual(p["submission_digest"], before["submission"]["digest"])
        self.assertEqual(p["supersedes_receipt_seqs"], [before["checks"][0]["receipt_seq"]])
        self.assertTrue(p["check_attempt"])
        self.assertTrue(p["check_wave"])
        self.assertEqual(self.statuses(), {"a": "submitted", "b": "blocked"})
        self.assertEqual(self.work.checks(self.task, self.current()), {})
        self.assertTrue(self.current()["check_in_flight"])
        self.assertFalse(self.work.view(self.task)["stage_accepted"])
        self.assertEqual(self.review().payload["status"], "submitted")
        with self.assertRaises(WorkConflict):
            self.work.claim(self.task, "b")

    def test_legacy_receipt_without_start_remains_compatible(self):
        self.accept()
        self.assertEqual(self.statuses(), {"a": "accepted", "b": "ready"})
        self.assertFalse(self.current()["in_flight"])

    def test_new_legacy_failure_also_revokes_accepted_dependents(self):
        self.accept()
        self.accept("b")
        self.receipt(passed=False)
        self.assertEqual(self.statuses(), {"a": "submitted", "b": "blocked"})
        self.assertTrue(self.current()["check_lifecycle"]["behavior"]["failed"])
        self.assertEqual(self.review().payload["status"], "rework")

    def test_legacy_failure_preserves_running_dependent_effects_without_a_start(self):
        self.accept()
        claim = self.work.claim(self.task, "b")
        self.receipt(passed=False)
        self.assertEqual(self.statuses(), {"a": "submitted", "b": "uncertain"})
        self.assertTrue(self.current("b")["execution_in_flight"])
        self.assertEqual(self.current("b")["attempt_id"], claim.payload["attempt_id"])
        with self.assertRaises(WorkConflict):
            self.work.claim(self.task, "b")
        with self.assertRaises(WorkConflict):
            self.work.rework(self.task, "a", "Retry")
        self.assertEqual(list(self.log.iter_events(task_id=self.task, event_types=("work.check_started",))), [])

    def test_all_overlapping_successes_must_finish_before_acceptance(self):
        self.submit()
        first = self.work.start_check(self.task, "a", "behavior")
        second = self.work.start_check(self.task, "a", "behavior")
        self.assertEqual(first.payload["check_wave"], second.payload["check_wave"])
        self.assertNotEqual(first.payload["check_attempt"], second.payload["check_attempt"])
        self.receipt(second)
        self.assertEqual(self.work.checks(self.task, self.current()), {})
        self.assertEqual(self.review().payload["status"], "submitted")
        self.receipt(first)
        self.assertEqual(self.review().payload["status"], "accepted")
        self.assertFalse(self.current()["in_flight"])

    def test_failure_wins_for_both_start_assignments_and_completion_orders(self):
        for failed_index, order in itertools.product(range(2), itertools.permutations(range(2))):
            with self.subTest(failed_index=failed_index, order=order):
                self.create_plan()
                self.accept()
                starts = [self.work.start_check(self.task, "a", "behavior") for _ in range(2)]
                receipts = {}
                for index in order:
                    receipts[index] = self.receipt(starts[index], passed=index != failed_index)
                selected = self.work.checks(self.task, self.current())["behavior"]
                self.assertEqual(selected.seq, receipts[failed_index].seq)
                self.assertEqual(self.review().payload["status"], "rework")
                self.assertEqual(self.statuses()["b"], "blocked")

    def test_legacy_success_cannot_hide_a_new_failed_wave(self):
        self.accept()
        start = self.work.start_check(self.task, "a", "behavior")
        failed = self.receipt(start, passed=False)
        self.receipt()
        self.assertEqual(self.work.checks(self.task, self.current())["behavior"].seq, failed.seq)
        self.assertEqual(self.review().payload["status"], "rework")

    def test_completed_failed_wave_can_be_explicitly_rechecked_without_reexecuting_work(self):
        self.submit()
        first, second = [self.work.start_check(self.task, "a", "behavior") for _ in range(2)]
        self.receipt(first, passed=False)
        self.receipt(second)
        self.assertEqual(self.review().payload["status"], "rework")
        submission = self.current()["submission"]
        retry = self.work.start_check(self.task, "a", "behavior")
        self.assertEqual(retry.payload["supersedes"], [first.seq, second.seq])
        self.assertNotEqual(retry.payload["check_wave"], first.payload["check_wave"])
        self.receipt(retry)
        self.assertEqual(self.current()["submission"], submission)
        self.assertEqual(self.statuses()["a"], "submitted")
        self.assertEqual(self.review().payload["status"], "accepted")
        claims = list(self.log.iter_events(task_id=self.task, event_types=("work.claimed",)))
        self.assertEqual(len(claims), 1)

    def test_duplicate_or_superseded_completions_cannot_change_the_result(self):
        self.submit()
        old = self.work.start_check(self.task, "a", "behavior")
        failed = self.receipt(old, passed=False)
        self.receipt(old)
        self.assertEqual(self.work.checks(self.task, self.current())["behavior"].seq, failed.seq)
        retry = self.work.start_check(self.task, "a", "behavior")
        success = self.receipt(retry)
        self.review()
        self.receipt(old, passed=False)
        self.receipt(retry, passed=False)
        self.assertEqual(self.statuses()["a"], "accepted")
        self.assertEqual(self.work.checks(self.task, self.current())["behavior"].seq, success.seq)

    def test_other_criterion_success_does_not_replace_failed_criterion(self):
        a = item("a")
        a["criteria"].append({"id": "integration", "kind": "local_check", "text": "Run integration"})
        self.create_plan([a])
        self.submit()
        behavior = self.work.start_check(self.task, "a", "behavior")
        integration = self.work.start_check(self.task, "a", "integration")
        self.receipt(behavior, passed=False)
        self.receipt(integration)
        self.assertEqual(self.review().payload["status"], "rework")

    def test_expired_check_stays_uncertain_until_its_trusted_completion(self):
        self.submit()
        start = self.work.start_check(self.task, "a", "behavior", lease_seconds=1)
        with patch("cater.work.time.time", return_value=start.payload["lease_until"] + 1):
            self.assertEqual(self.statuses()["a"], "uncertain")
            self.assertTrue(self.current()["check_uncertain"])
            for operation in (
                lambda: self.work.start_check(self.task, "a", "behavior"),
                lambda: self.work.rework(self.task, "a", "Retry"),
                lambda: self.work.plan(self.task, [item("a")], role_ids={"dev", "test"}, source_seq=0),
                lambda: self.work.claim(self.task, "a"),
            ):
                with self.assertRaises(WorkConflict):
                    operation()
            self.receipt(start, actor=Actor.agent("dev", "local_verifier"))
            self.assertEqual(self.statuses()["a"], "uncertain")
            self.receipt(start)
            self.assertEqual(self.statuses()["a"], "submitted")
            self.assertEqual(self.review().payload["status"], "accepted")

    def test_wrong_actor_version_attempt_and_task_cannot_end_a_start(self):
        self.submit()
        start = self.work.start_check(self.task, "a", "behavior")
        for overrides in (
            {"plan_seq": self.plan.seq + 1}, {"plan_seq": True}, {"scope": {}},
            {"submission_digest": "wrong"}, {"submission_seq": start.payload["submission_seq"] + 1},
            {"submission_seq": True}, {"work_item_id": "b"}, {"work_item_id": []},
            {"criterion_id": "unknown"}, {"criterion_id": []},
            {"check_attempt_seq": start.seq + 100}, {"check_attempt_seq": True},
            {"check_attempt_seq": None}, {"check_attempt": "wrong"},
        ):
            with self.subTest(overrides=overrides):
                self.receipt(start, overrides=overrides)
                self.assertTrue(self.current()["check_in_flight"])
        self.receipt(start, actor=Actor.agent("local_verifier", "local_verifier"))
        self.receipt(start, task_id="different-task")
        self.receipt(start, overrides={"submission_seq": None})
        self.assertEqual(self.work.checks(self.task, self.current()), {})
        self.receipt(start)
        self.assertEqual(self.review().payload["status"], "accepted")

    def test_forged_or_wrong_version_starts_do_not_revoke_acceptance(self):
        self.accept()
        submission = self.current()["submission"]
        payload = {"plan_seq": self.plan.seq, "scope": submission["scope"], "work_item_id": "a",
                   "criterion_id": "behavior", "submission_seq": submission["seq"],
                   "submission_digest": submission["digest"], "check_attempt": "fake-attempt", "check_wave": "fake-wave",
                   "lease_until": 9999999999, "supersedes": [], "supersedes_receipt_seqs": []}
        self.log.emit(type="work.check_started", actor=Actor.agent("dev", "local_verifier"),
                      task_id=self.task, payload=payload)
        for overrides in ({"submission_digest": "wrong"}, {"plan_seq": -1}, {"submission_seq": -1},
                          {"lease_until": True}, {"supersedes_receipt_seqs": [True]}):
            self.log.emit(type="work.check_started", actor=Actor.system("local_verifier"),
                          task_id=self.task, payload={**payload, **overrides})
        self.assertEqual(self.statuses()["a"], "accepted")

    def test_unfinished_wave_cannot_be_superseded_by_a_start_record(self):
        self.submit()
        start = self.work.start_check(self.task, "a", "behavior")
        forged = self.log.emit(type="work.check_started", actor=Actor.system("local_verifier"), task_id=self.task,
                              payload={**start.payload, "check_attempt": "another", "check_wave": "another-wave",
                                       "supersedes": [start.seq]})
        self.receipt(forged)
        self.assertEqual(self.work.checks(self.task, self.current()), {})
        self.assertEqual(self.current()["check_lifecycle"]["behavior"]["pending_seqs"], [start.seq])

    def test_recheck_invalidates_transitive_dependents_but_keeps_independent_work(self):
        self.create_plan([item("a"), item("b", deps=["a"], local=False),
                          item("c", deps=["b"], local=False), item("z", local=False)])
        for name in ("a", "b", "c", "z"):
            self.accept(name)
        start = self.work.start_check(self.task, "a", "behavior")
        self.assertEqual(self.statuses(), {"a": "submitted", "b": "blocked", "c": "blocked", "z": "accepted"})
        self.receipt(start)
        self.review()
        self.assertEqual(self.statuses(), {"a": "accepted", "b": "ready", "c": "blocked", "z": "accepted"})

    def test_running_dependent_keeps_uncertain_effects_even_after_upstream_passes(self):
        self.accept()
        claim = self.work.claim(self.task, "b")
        start = self.work.start_check(self.task, "a", "behavior")
        self.assertEqual(self.statuses()["b"], "uncertain")
        artifact = self.publish(claim)
        with self.assertRaises(WorkConflict):
            self.work.submit(self.task, claim, [artifact.seq])
        self.log.emit(type="work.submitted", actor=Actor.agent("dev", "worker"), task_id=self.task,
                      payload={**claim.payload, "artifact_seqs": [artifact.seq], "digest": "late-output"})
        self.receipt(start)
        self.review()
        self.assertEqual(self.statuses()["b"], "uncertain")
        for operation in (lambda: self.work.claim(self.task, "b"), lambda: self.work.rework(self.task, "a", "Recheck")):
            with self.assertRaises(WorkConflict):
                operation()
        self.work.fail(self.task, claim, "Effects explicitly reconciled", uncertain=False)
        self.assertEqual(self.statuses()["b"], "ready")

    def test_inflight_dependent_check_survives_upstream_invalidation(self):
        self.create_plan([item("a"), item("b", deps=["a"])])
        self.accept()
        self.submit("b")
        dependent = self.work.start_check(self.task, "b", "behavior")
        upstream = self.work.start_check(self.task, "a", "behavior")
        self.assertTrue(self.current("b")["check_in_flight"])
        with self.assertRaises(WorkConflict):
            self.work.claim(self.task, "b")
        with self.assertRaises(WorkConflict):
            self.work.rework(self.task, "a", "Retry")
        self.receipt(upstream)
        self.review()
        with self.assertRaises(WorkConflict):
            self.work.start_check(self.task, "b", "behavior")
        self.receipt(dependent)
        self.assertEqual(self.statuses()["b"], "ready")
        self.assertFalse(self.current("b")["in_flight"])
        self.assertIsNone(self.current("b")["submission"])

    def test_rework_and_replanning_are_blocked_by_pending_check(self):
        self.submit()
        start = self.work.start_check(self.task, "a", "behavior")
        for operation in (lambda: self.work.rework(self.task, "a", "Retry"),
                          lambda: self.work.plan(self.task, [item("a")], role_ids={"dev", "test"}, source_seq=0)):
            with self.assertRaises(WorkConflict):
                operation()
        self.receipt(start)
        old = self.current()
        self.work.rework(self.task, "a", "Rebuild")
        self.assertEqual(self.work.checks(self.task, old), {})
        self.submit()
        self.receipt(start)
        self.assertEqual(self.work.checks(self.task, self.current()), {})
        self.assertEqual(self.review().payload["status"], "submitted")

    def test_same_scope_replacement_plan_does_not_reuse_old_check_receipts(self):
        self.submit()
        start = self.work.start_check(self.task, "a", "behavior")
        self.receipt(start)
        old = self.current()
        self.work.plan(self.task, [item("a")], role_ids={"dev", "test"}, source_seq=0)
        self.submit()
        self.receipt(start)
        self.assertEqual(self.work.checks(self.task, old), {})
        self.assertEqual(self.review().payload["status"], "submitted")

    def test_scope_change_preserves_pending_effects_but_never_accepts_old_checks(self):
        self.submit()
        start = self.work.start_check(self.task, "a", "behavior")
        self.rules.record("Changed rule", scope="task", task_id=self.task)
        self.assertTrue(self.current()["in_flight"])
        with self.assertRaises(WorkConflict):
            self.work.start_check(self.task, "a", "behavior")
        self.receipt(start)
        self.assertFalse(self.current()["in_flight"])
        self.assertEqual(self.work.checks(self.task, self.current()), {})
        self.assertEqual(self.statuses()["a"], "stale")

    def test_start_rechecks_submission_after_concurrent_rework(self):
        self.submit()
        original = self.log.emit
        changed = False

        def emit(**args):
            nonlocal changed
            if args["type"] == "work.check_started" and not changed:
                changed = True
                self.work.rework(self.task, "a", "Concurrent rework")
            return original(**args)

        with patch.object(self.log, "emit", side_effect=emit), self.assertRaises(WorkConflict):
            self.work.start_check(self.task, "a", "behavior")
        self.assertTrue(changed)
        self.assertEqual(list(self.log.iter_events(task_id=self.task, event_types=("work.check_started",))), [])

    def test_expected_submission_prevents_binding_copied_files_to_new_submission(self):
        old = self.submit()
        self.work.rework(self.task, "a", "Replace files")
        new = self.submit()
        before = self.log.count()
        with self.assertRaises(WorkConflict):
            self.work.start_check(self.task, "a", "behavior", expected_submission_seq=old.seq)
        self.assertEqual(self.log.count(), before)
        start = self.work.start_check(self.task, "a", "behavior", expected_submission_seq=new.seq)
        self.assertEqual(start.payload["submission_seq"], new.seq)

    def test_expected_submission_is_rechecked_after_cas_retry(self):
        old = self.submit()
        original = self.log.emit
        changed = False

        def emit(**args):
            nonlocal changed
            if args["type"] == "work.check_started" and not changed:
                changed = True
                self.work.rework(self.task, "a", "Concurrent replacement")
                self.submit()
            return original(**args)

        with patch.object(self.log, "emit", side_effect=emit), self.assertRaises(WorkConflict):
            self.work.start_check(self.task, "a", "behavior", expected_submission_seq=old.seq)
        self.assertTrue(changed)
        self.assertNotEqual(self.current()["submission"]["seq"], old.seq)
        self.assertEqual(list(self.log.iter_events(task_id=self.task, event_types=("work.check_started",))), [])

    def test_review_cas_cannot_accept_check_started_during_append(self):
        self.submit()
        self.receipt()
        original = self.log.emit
        changed = False

        def emit(**args):
            nonlocal changed
            if args["type"] == "work.reviewed" and not changed:
                changed = True
                self.work.start_check(self.task, "a", "behavior")
            return original(**args)

        with patch.object(self.log, "emit", side_effect=emit):
            self.assertEqual(self.review().payload["status"], "submitted")
        self.assertEqual(self.statuses()["b"], "blocked")

    def test_concurrent_starts_join_one_wave_with_distinct_attempts(self):
        self.submit()
        barrier = threading.Barrier(2)
        outcomes, errors = [], []

        def start():
            log = EventLog(self.db)
            try:
                ledger = WorkLedger(log, RuleBook(log))
                barrier.wait(timeout=5)
                outcomes.append(ledger.start_check(self.task, "a", "behavior"))
            except Exception as exc:
                errors.append(exc)
            finally:
                log.close()

        workers = [threading.Thread(target=start) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=10)
            self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(outcomes), 2)
        self.assertEqual(len({e.payload["check_wave"] for e in outcomes}), 1)
        self.assertEqual(len({e.payload["check_attempt"] for e in outcomes}), 2)
        self.receipt(outcomes[0], passed=False)
        self.receipt(outcomes[1])
        self.assertEqual(self.review().payload["status"], "rework")
        self.assertTrue(self.log.verify_chain().ok)

    def test_reopened_ledger_reconstructs_pending_and_failed_wave(self):
        self.accept()
        first, second = [self.work.start_check(self.task, "a", "behavior") for _ in range(2)]
        self.receipt(first, passed=False)
        reopened = EventLog(self.db)
        self.addCleanup(reopened.close)
        ledger = WorkLedger(reopened, RuleBook(reopened))
        current = ledger._item(self.task, "a")[1]
        self.assertTrue(current["in_flight"])
        self.assertEqual(ledger.checks(self.task, current), {})
        self.receipt(second)
        current = ledger._item(self.task, "a")[1]
        self.assertFalse(current["in_flight"])
        self.assertFalse(ledger._receipt_passed(ledger.checks(self.task, current)["behavior"]))

    def test_start_rejects_invalid_criterion_lease_and_unsubmitted_work(self):
        with self.assertRaises(WorkConflict):
            self.work.start_check(self.task, "a", "behavior")
        self.submit()
        for lease in (0, -1, 3601, True, float("nan"), float("inf")):
            with self.subTest(lease=lease), self.assertRaises(ValueError):
                self.work.start_check(self.task, "a", "behavior", lease_seconds=lease)
        with self.assertRaises(ValueError):
            self.work.start_check(self.task, "a", "unknown")

    def test_rework_expected_plan_and_scope_are_independent_guards(self):
        old_scope = self.work.scope(self.task)
        self.work.plan(self.task, [item("a")], role_ids={"dev", "test"}, source_seq=0)
        before = self.log.count()
        with self.assertRaises(WorkConflict):
            self.work.rework(self.task, "a", "Old review", expected_plan_seq=self.plan.seq)
        self.assertEqual(self.log.count(), before)
        self.rules.record("New rule", scope="task", task_id=self.task)
        with self.assertRaises(WorkConflict):
            self.work.rework(self.task, "a", "Old review", expected_scope=old_scope)

    def test_rework_guards_are_rechecked_after_cas_conflict(self):
        original = self.log.emit
        scope = self.work.scope(self.task)
        changed = False

        def emit(**args):
            nonlocal changed
            if args["type"] == "work.invalidated" and not changed:
                changed = True
                self.work.plan(self.task, [item("a")], role_ids={"dev", "test"}, source_seq=0)
            return original(**args)

        with patch.object(self.log, "emit", side_effect=emit), self.assertRaises(WorkConflict):
            self.work.rework(self.task, "a", "Old review", expected_plan_seq=self.plan.seq, expected_scope=scope)
        self.assertTrue(changed)
        self.assertEqual(list(self.log.iter_events(task_id=self.task, event_types=("work.invalidated",))), [])

    def test_rework_with_current_expectations_succeeds(self):
        event = self.work.rework(self.task, "a", "Verified issue", expected_plan_seq=self.plan.seq,
                                 expected_scope=self.work.scope(self.task))
        self.assertEqual(event.payload["affected"], ["a", "b"])


if __name__ == "__main__":
    unittest.main()
