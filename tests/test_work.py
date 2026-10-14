from __future__ import annotations

import copy
import json
import sys
import tempfile
import threading
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from cater.artifacts import normalize_deliverables
from cater.drivers import CliDriver, DriverResponse, DriverError
from cater.events import Actor
from cater.runtime import Runtime
from cater.work import WorkConflict


def item(name, *, owner="dev", reviewer="test", deps=(), local=False):
    criteria = [{"id": "meaning", "kind": "review", "text": "Contains a verifiable result"}]
    if local:
        criteria.append({"id": "behavior", "kind": "local_check", "text": "Run the submitted test"})
    return {"id": name, "owner": owner, "reviewer": reviewer, "objective": f"Deliver {name}",
            "depends_on": list(deps), "criteria": criteria}


class WorkDriver:
    def __init__(self):
        self.calls = []
        self.after = None
        self.quote = None
        self.status = "passed"
        self.risk = False

    def complete(self, *, role, prompt, context):
        work = context["work"]
        self.calls.append(copy.deepcopy(work))
        if self.after:
            self.after(work)
        if work.get("phase") == "review":
            artifact = work["artifacts"][0]
            payload = {"checks": [{"id": "meaning", "status": self.status, "reason": "Inspected the exact body",
                                   "artifact_seq": artifact["seq"], "quote": self.quote or artifact["content"]}]}
        else:
            payload = {"claim": "Completed", "text": "Completed", "stance": "assert", "option_id": "A",
                       "deliverables": [{"title": work["work_item_id"], "summary": "Result",
                                         "content": "assert 2 + 2 == 4\n", "format": "code", "filename": "check.py"}]}
            if self.risk:
                payload.update(stance="challenge", evidence=["The caller impact has not been checked"])
        return DriverResponse(text=json.dumps(payload), tokens=7, meta={"token_usage": "reported"})


class WorkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "work.db"
        self.driver = WorkDriver()
        self.rt = Runtime(self.db, drivers={name: self.driver for name in ("product", "design", "dev", "test")})
        self.addCleanup(self.rt.close)
        self.task = self.rt.create_task("Work with real checkpoints")

    def statuses(self):
        return {i["id"]: i["status"] for i in self.rt.work.view(self.task)["items"]}

    def submit(self, name):
        claim = self.rt.work.claim(self.task, name)
        artifact = normalize_deliverables([{"title": "Result", "summary": "Body", "content": "assert True\n",
                                             "format": "code", "filename": "check.py"}])[0]
        event = self.rt.log.emit(type="deliverable.published", actor=Actor.agent("dev", "worker"), task_id=self.task,
                                 payload={**artifact, "work_item_id": name, "attempt_id": claim.payload["attempt_id"]})
        submission = self.rt.work.submit(self.task, claim, [event.seq])
        return claim, event, submission

    def test_same_owner_multiple_items_execute_in_dependency_order_with_separate_reviews(self):
        self.rt.plan_work(self.task, [item("second", deps=["first"]), item("first"), item("independent")])
        self.rt.collaborate(self.task)
        self.assertEqual(self.statuses(), {"first": "accepted", "second": "accepted", "independent": "accepted"})
        work = [c for c in self.driver.calls if c.get("phase") != "review"]
        self.assertEqual([c["work_item_id"] for c in work], ["first", "second", "independent"])
        self.assertIn("assert 2 + 2", work[1]["accepted_inputs"]["first"][0]["content"])
        self.assertNotEqual(work[0]["attempt_id"], work[1]["attempt_id"])
        self.assertNotEqual(self.rt.task(self.task).status, "done")
        with Runtime(self.db) as reopened:
            self.assertTrue(reopened.work.view(self.task)["stage_accepted"])
            self.assertTrue(reopened.verify_chain().ok)

    def test_invalid_dag_or_criteria_never_partially_writes_plan(self):
        invalid = [[item("a"), item("a")], [item("a", deps=["missing"])], [item("a", deps=["a"])],
                   [item("a", deps=["b"]), item("b", deps=["a"])], [item("a", reviewer="dev")],
                   [{**item("a"), "criteria": []}], [item("a", reviewer="unknown")]]
        for items in invalid:
            before = self.rt.log.count()
            with self.subTest(items=items), self.assertRaises(ValueError):
                self.rt.plan_work(self.task, items)
            self.assertEqual(before, self.rt.log.count())

    def test_submission_does_not_release_dependency_and_author_cannot_self_accept(self):
        self.rt.plan_work(self.task, [item("a"), item("b", deps=["a"])])
        _, _, submission = self.submit("a")
        self.assertEqual(self.statuses(), {"a": "submitted", "b": "blocked"})
        with self.assertRaises(ValueError):
            self.rt.work.review(self.task, "a", reviewer="dev", submission_seq=submission.seq, results=[])
        with self.assertRaises(WorkConflict):
            self.rt.work.claim(self.task, "b")

    def test_missing_or_fabricated_quotes_keep_work_unaccepted(self):
        self.driver.quote = "A made up successful test result"
        self.rt.plan_work(self.task, [item("a"), item("b", deps=["a"])])
        self.rt.collaborate(self.task)
        self.assertEqual(self.statuses(), {"a": "submitted", "b": "blocked"})
        self.assertEqual(self.rt.work.view(self.task)["items"][0]["checks"][0]["status"], "unknown")

    def test_required_checks_are_real_and_bound_to_artifact_copy(self):
        self.rt.plan_work(self.task, [item("a", local=True), item("b", deps=["a"])])
        self.rt.collaborate(self.task)
        self.assertEqual(len(self.driver.calls), 1)
        self.assertEqual(self.statuses()["b"], "blocked")
        receipt, _ = self.rt.verify_work(self.task, "a", "behavior", [sys.executable, "check.py"])
        self.assertTrue(receipt.payload["artifacts_unchanged"])
        self.assertEqual(receipt.payload["exit_code"], 0)
        self.rt.collaborate(self.task)
        self.assertEqual(self.statuses(), {"a": "accepted", "b": "accepted"})

    def test_modified_artifact_copy_is_not_a_successful_receipt(self):
        self.rt.plan_work(self.task, [item("a", local=True)])
        self.rt.collaborate(self.task)
        receipt, _ = self.rt.verify_work(self.task, "a", "behavior", [sys.executable, "-c", "open('check.py','w').write('changed')"])
        self.assertFalse(receipt.payload["artifacts_unchanged"])
        self.rt.collaborate(self.task)
        reviews = list(self.rt.log.iter_events(task_id=self.task, event_types=("work.reviewed",)))
        self.assertEqual(reviews[0].payload["status"], "rework")
        self.assertNotEqual(self.statuses()["a"], "accepted")

    def test_model_verification_event_cannot_impersonate_local_verifier(self):
        self.rt.plan_work(self.task, [item("a", local=True)])
        self.rt.collaborate(self.task)
        current = self.rt.work.view(self.task)["items"][0]
        self.rt.log.emit(type="verification.completed", actor=Actor.agent("dev", "local_verifier"), task_id=self.task,
                         payload={"work_item_id": "a", "criterion_id": "behavior", "outcome": "passed", "exit_code": 0,
                                  "submission_digest": current["submission"]["digest"]})
        self.rt.collaborate(self.task)
        self.assertEqual(self.statuses()["a"], "submitted")

    def test_rule_change_during_execution_discards_stale_submission(self):
        self.rt.plan_work(self.task, [item("a"), item("b", deps=["a"])])
        self.driver.after = lambda work: self.rt.rulebook.record("New rule", scope="task", task_id=self.task)
        self.rt.collaborate(self.task)
        self.assertEqual(self.statuses(), {"a": "stale", "b": "stale"})
        self.assertEqual(len(self.driver.calls), 1)
        self.assertFalse(list(self.rt.log.iter_events(task_id=self.task, event_types=("work.submitted",))))

    def test_direction_change_during_review_prevents_downstream_execution(self):
        self.rt.plan_work(self.task, [item("a"), item("b", deps=["a"])])
        self.driver.after = lambda work: self.rt.command(self.task, "New requirement") if work.get("phase") == "review" else None
        self.rt.collaborate(self.task)
        self.assertEqual(self.statuses()["b"], "stale")
        self.assertEqual(len(self.driver.calls), 2)

    def test_atomic_review_rechecks_scope_if_rule_changes_during_append(self):
        self.rt.plan_work(self.task, [item("a")])
        original = self.rt.log.emit
        changed = False

        def emit(**args):
            nonlocal changed
            if args["type"] == "work.reviewed" and not changed:
                changed = True
                self.rt.rulebook.record("Concurrent rule", scope="task", task_id=self.task)
            return original(**args)

        with patch.object(self.rt.log, "emit", side_effect=emit):
            self.rt.collaborate(self.task)
        self.assertTrue(changed)
        self.assertEqual(self.statuses()["a"], "stale")

    def test_rework_invalidates_only_descendants_and_old_receipts_do_not_reapply(self):
        self.rt.plan_work(self.task, [item("a"), item("b", deps=["a"]), item("c")])
        self.rt.collaborate(self.task)
        previous = self.rt.work.view(self.task)["items"][0]["submission"]["seq"]
        self.rt.work.rework(self.task, "a", "Incorrect input")
        self.assertEqual(self.statuses(), {"a": "ready", "b": "blocked", "c": "accepted"})
        with self.assertRaises(WorkConflict):
            self.rt.work.review(self.task, "a", reviewer="test", submission_seq=previous, results=[])
        self.rt.collaborate(self.task)
        self.assertEqual(len([c for c in self.driver.calls if c["work_item_id"] == "c"]), 2)

    def test_crashed_or_expired_attempt_is_never_blindly_replayed(self):
        self.rt.plan_work(self.task, [item("a")])
        claim = self.rt.work.claim(self.task, "a", lease_seconds=0.001)
        with patch("cater.work.time.time", return_value=claim.payload["lease_until"] + 1):
            self.assertEqual(self.statuses()["a"], "uncertain")
            self.rt.collaborate(self.task)
            self.assertEqual(self.driver.calls, [])
            with self.assertRaises(WorkConflict):
                self.rt.plan_work(self.task, [item("b")])

    def test_concurrent_claims_have_one_winner(self):
        self.rt.plan_work(self.task, [item("a")])
        barrier = threading.Barrier(2)
        outcomes = []

        def claim():
            with Runtime(self.db) as rt:
                barrier.wait()
                try:
                    rt.work.claim(self.task, "a")
                    outcomes.append("claimed")
                except WorkConflict:
                    outcomes.append("conflict")

        workers = [threading.Thread(target=claim) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=10)
            self.assertFalse(worker.is_alive())
        self.assertCountEqual(outcomes, ["claimed", "conflict"])
        self.assertTrue(self.rt.verify_chain().ok)

    def test_default_chat_intake_uses_work_gate_not_consensus_completion(self):
        self.rt.intake(self.task, requested_route="direct", preferred_owner="dev")
        self.rt.collaborate(self.task)
        self.assertTrue(self.rt.task(self.task).work["items"])
        self.assertFalse(list(self.rt.log.iter_events(task_id=self.task, event_types=("agent.decided",))))
        self.assertNotEqual(self.rt.task(self.task).status, "done")

    def test_pending_or_invalidated_work_does_not_enter_shared_outcome_memory(self):
        other = self.rt.create_task("Follow up")
        self.rt.plan_work(self.task, [item("a")])
        self.submit("a")
        self.assertEqual(self.rt.shared_memory(exclude_task_id=other), [])
        self.rt.collaborate(self.task)
        self.assertTrue(self.rt.shared_memory(exclude_task_id=other))
        self.rt.work.rework(self.task, "a", "Recheck the evidence")
        self.assertEqual(self.rt.shared_memory(exclude_task_id=other), [])

    def test_replanning_while_a_call_is_in_flight_cannot_accept_its_old_result(self):
        self.rt.plan_work(self.task, [item("a")])
        self.driver.after = lambda work: self.rt.dispatch(self.task)
        self.rt.collaborate(self.task)
        self.assertFalse(self.rt.work.view(self.task)["stage_accepted"])
        self.assertEqual(self.statuses()["a"], "stale")

    def test_cli_internal_restart_does_not_repeat_a_work_attempt(self):
        marker = Path(self.tmp.name) / "calls.txt"
        code = "import sys,pathlib; sys.stdin.readline(); p=pathlib.Path(sys.argv[1]); p.open('a').write('called\\n'); sys.exit(7)"
        driver = CliDriver("crash", [sys.executable, "-c", code, str(marker)], restart_limit=3, timeout=3)
        self.addCleanup(driver.close)
        with self.assertRaises(DriverError):
            driver.complete(role="dev", prompt="Execute", context={"work": {"attempt_id": "one"}})
        self.assertEqual(marker.read_text().splitlines(), ["called"])

    def test_reported_risk_is_not_ignored_even_with_a_readable_artifact(self):
        self.driver.risk = True
        self.rt.plan_work(self.task, [item("a"), item("b", deps=["a"])])
        self.rt.collaborate(self.task)
        self.assertEqual(self.statuses(), {"a": "ready", "b": "blocked"})
        self.assertIn("风险", self.rt.work.view(self.task)["items"][0]["reason"])
        self.assertEqual(len(self.driver.calls), 1)

    def test_repeated_run_does_not_repeat_accepted_work_or_reviews(self):
        self.rt.plan_work(self.task, [item("a")])
        self.rt.collaborate(self.task)
        count = len(self.driver.calls)
        self.rt.collaborate(self.task)
        self.assertEqual(count, len(self.driver.calls))

    def test_failed_check_cannot_be_overridden_by_a_positive_review_or_forced_battle(self):
        self.rt.plan_work(self.task, [item("a", local=True), item("b", deps=["a"])])
        self.rt.collaborate(self.task)
        self.rt.verify_work(self.task, "a", "behavior", [sys.executable, "-c", "raise SystemExit(1)"])
        self.rt.battle(self.task)
        self.assertEqual(self.statuses(), {"a": "ready", "b": "blocked"})
        self.assertFalse(self.rt.work.view(self.task)["stage_accepted"])

    def test_codex_adapter_selects_review_contract_without_executing_a_real_model(self):
        from examples import codex_cli_driver as adapter
        payload = {"checks": [{"id": "meaning", "status": "passed", "reason": "Checked the source",
                                "artifact_seq": 4, "quote": "assert True"}]}
        observed = []

        def run(command, **kwargs):
            schema = json.loads(Path(command[command.index("--output-schema") + 1]).read_text())
            observed.append(schema)
            Path(command[command.index("--output-last-message") + 1]).write_text(json.dumps(payload))
            return subprocess.CompletedProcess(command, 0)

        with patch.object(adapter.subprocess, "run", side_effect=run):
            result = adapter.handle({"id": "review", "type": "complete", "context": {"work": {"phase": "review"}}})
        self.assertTrue(result["ok"])
        self.assertEqual(observed[0]["required"], ["checks"])
        self.assertEqual(json.loads(result["text"]), payload)
        payload["checks"][0]["artifact_seq"] = True
        self.assertFalse(adapter._valid_review_payload(payload))


if __name__ == "__main__":
    unittest.main()
