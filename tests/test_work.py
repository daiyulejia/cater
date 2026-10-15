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
from cater.directions import command_intent
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
            payload = {"checks": [{"id": c["id"], "status": self.status, "reason": "Inspected the exact body and check scope",
                                   "artifact_seq": artifact["seq"], "quote": self.quote or artifact["content"],
                                   "receipt_seq": work["local_checks"].get(c["id"], {}).get("seq")}
                                  for c in work["contract"]["criteria"]]}
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

    def test_control_recognition_is_exact_and_mixed_messages_remain_changes(self):
        for text in ("进度怎么样？", "@店长 现在做到哪了", "@米寇：汇报进度", "status"):
            self.assertEqual(command_intent(text, ["店长", "米寇"]), "status", text)
        for text in ("继续", "@米寇 请继续！", "resume"):
            self.assertEqual(command_intent(text, ["米寇"]), "resume", text)
        for text in ("进度怎么样？另加一个登录页", "继续，但不要改接口", "@陌生人 查看进度",
                     "目标改为：查看进度", "是否考虑改成异步？", "继续\n另外要支持中文"):
            self.assertEqual(command_intent(text, ["店长", "米寇"]), "direction", text)

    def test_status_and_resume_during_work_do_not_invalidate_or_duplicate_execution(self):
        self.rt.plan_work(self.task, [item("a"), item("b", deps=["a"])])
        scope = self.rt.work.scope(self.task)
        self.driver.after = lambda work: self.rt.command(self.task, "@米寇 进度怎么样？")
        self.rt.collaborate(self.task)
        self.assertEqual(self.statuses(), {"a": "accepted", "b": "accepted"})
        self.assertEqual(self.rt.work.scope(self.task), scope)
        before = len(self.driver.calls)
        command = self.rt.command(self.task, "继续")
        self.rt.collaborate(self.task)
        self.assertEqual(command.payload["intent"], "resume")
        self.assertEqual(command.payload["next_action"], "run")
        self.assertEqual(len(self.driver.calls), before)
        self.assertEqual(self.rt.work.scope(self.task), scope)
        self.assertEqual(len(list(self.rt.log.iter_events(task_id=self.task, event_types=("work.plan_created",)))), 1)

    def test_progress_report_uses_evidence_not_activity_completion(self):
        self.rt.plan_work(self.task, [item("a"), item("b", deps=["a"])])
        self.submit("a")
        command = self.rt.command(self.task, "做完了吗？")
        report = self.rt.log.read(task_id=self.task)[-1]
        self.assertEqual(report.type, "task.progress_reported")
        self.assertEqual(report.payload["command_seq"], command.seq)
        self.assertEqual(report.payload["counts"], {"submitted": 1, "blocked": 1})
        self.assertIn("验收证据还没齐", report.payload["text"])
        self.assertEqual(report.payload["observed_head"], command.hash)
        self.assertEqual(self.driver.calls, [])

    def test_progress_query_on_done_task_does_not_reopen_it(self):
        self.rt.decide(self.task, "done", "Human confirmation")
        self.rt.command(self.task, "做完了吗？")
        self.assertEqual(self.rt.task(self.task).status, "done")
        self.assertIn("人工完成确认", self.rt.log.read(task_id=self.task)[-1].payload["text"])
        self.assertEqual(self.driver.calls, [])

    def test_status_text_with_explicit_goal_or_acceptance_is_still_a_change(self):
        self.rt.plan_work(self.task, [item("a")])
        before = self.rt.work.scope(self.task)
        command = self.rt.command(self.task, "进度怎么样", goal="Different goal")
        self.assertEqual(command.payload["intent"], "direction")
        self.assertNotEqual(self.rt.work.scope(self.task), before)
        command = self.rt.command(self.task, "继续", acceptance=["New requirement"])
        self.assertEqual(command.payload["intent"], "direction")
        self.assertEqual(self.statuses()["a"], "stale")

    def test_untyped_historical_commands_keep_conservative_invalidation(self):
        self.rt.plan_work(self.task, [item("a")])
        self.rt.log.emit(type="human.commanded", actor=Actor.human(), task_id=self.task,
                         payload={"instruction": "status"})
        self.assertEqual(self.statuses()["a"], "stale")

    def test_progress_report_retries_when_projection_changes_during_append(self):
        self.rt.plan_work(self.task, [item("a")])
        original = self.rt.log.emit
        changed = False

        def emit(**args):
            nonlocal changed
            if args["type"] == "task.progress_reported" and not changed:
                changed = True
                self.rt.rulebook.record("Changed during status read", scope="task", task_id=self.task)
            return original(**args)

        with patch.object(self.rt.log, "emit", side_effect=emit):
            self.rt.command(self.task, "查看进度")
        report = self.rt.log.read(task_id=self.task)[-1]
        self.assertEqual(report.payload["counts"], {"stale": 1})
        self.assertFalse(report.payload["scope_current"])
        self.assertTrue(self.rt.verify_chain().ok)

    def test_all_full_customer_directions_reach_execution_and_independent_review(self):
        instructions = ["Keep requirement " + str(i) + ": " + "a" * 200 + " required-tail" for i in range(5)]
        for instruction in instructions:
            self.rt.command(self.task, instruction)
        self.rt.command(self.task, "进度怎么样")
        self.rt.command(self.task, "继续")
        self.rt.plan_work(self.task, [item("a")])
        self.rt.collaborate(self.task)
        self.assertEqual(len(self.driver.calls), 2)
        for call in self.driver.calls:
            self.assertEqual([d["instruction"] for d in call["customer_directions"]], instructions)
        self.assertTrue(self.rt.work.view(self.task)["stage_accepted"])

    def test_older_plan_reconstructs_only_its_bound_directions(self):
        old = self.rt.command(self.task, "Keep the old requirement")
        self.rt.plan_work(self.task, [item("a")])
        original = self.rt.log.read(task_id=self.task)[-1]
        payload = {k: v for k, v in original.payload.items() if k != "customer_directions"}
        self.rt.log.emit(type="work.plan_created", task_id=self.task, actor=Actor.system("work_scheduler"), payload=payload)
        self.rt.command(self.task, "New requirement")
        view = self.rt.work.view(self.task)
        self.assertEqual([d["seq"] for d in view["customer_directions"]], [old.seq])
        self.assertFalse(view["scope_current"])

    def test_oversized_directions_block_execution_instead_of_silently_truncating(self):
        for i in range(12):
            self.rt.command(self.task, f"Requirement {i}: " + "x" * 11950)
        self.rt.plan_work(self.task, [item("a")])
        self.rt.collaborate(self.task)
        self.assertEqual(self.driver.calls, [])
        current = self.rt.work.view(self.task)["items"][0]
        self.assertIn("未截断", current["reason"])
        self.assertNotEqual(current["status"], "accepted")

    def test_oversized_review_input_is_visible_and_never_truncated_to_pass(self):
        self.rt.plan_work(self.task, [item("a")])
        claim = self.rt.work.claim(self.task, "a")
        ids = []
        for i in range(3):
            artifact = normalize_deliverables([{"title": str(i), "summary": "Body", "content": "x" * 64000}])[0]
            ids.append(self.rt.log.emit(type="deliverable.published", actor=Actor.agent("dev", "worker"), task_id=self.task,
                                       payload={**artifact, "work_item_id": "a", "attempt_id": claim.payload["attempt_id"]}).seq)
        self.rt.work.submit(self.task, claim, ids)
        self.rt.collaborate(self.task)
        current = self.rt.work.view(self.task)["items"][0]
        self.assertEqual(current["status"], "submitted")
        self.assertIn("验收输入过大", current["reason"])
        self.assertEqual(self.driver.calls, [])

    def test_gateway_decline_before_invocation_does_not_require_side_effect_reconciliation(self):
        self.rt.plan_work(self.task, [item("a")])
        self.driver.token_budget = 0
        self.rt.collaborate(self.task)
        current = self.rt.work.view(self.task)["items"][0]
        self.assertFalse(current["in_flight"])
        self.assertEqual(current["status"], "ready")
        self.assertIn("调用尚未发出", current["reason"])
        self.assertEqual(self.driver.calls, [])
        self.driver.token_budget = 100
        self.rt.command(self.task, "继续")
        self.rt.collaborate(self.task)
        self.assertTrue(self.rt.work.view(self.task)["stage_accepted"])

    def test_preflight_scope_change_does_not_lock_an_uninvoked_attempt(self):
        self.rt.plan_work(self.task, [item("a")])
        original = self.rt.log.emit
        changed = False

        def emit(**args):
            nonlocal changed
            if args["type"] == "agent.working" and not changed:
                changed = True
                self.rt.command(self.task, "New requirement before invocation")
            return original(**args)

        with patch.object(self.rt.log, "emit", side_effect=emit):
            self.rt.collaborate(self.task)
        current = self.rt.work.view(self.task)["items"][0]
        self.assertEqual(current["status"], "stale")
        self.assertFalse(current["in_flight"])
        self.assertEqual(self.driver.calls, [])
        self.rt.plan_work(self.task, [item("updated")])

    def test_driver_cannot_claim_an_actual_invocation_never_started(self):
        self.rt.plan_work(self.task, [item("a")])
        with patch.object(self.driver, "complete", return_value=DriverResponse(
                text='{"claim":"Response lost"}', meta={"contained": True, "invocation_started": False})):
            self.rt.collaborate(self.task)
        self.assertEqual(self.statuses()["a"], "uncertain")
        with self.assertRaises(WorkConflict):
            self.rt.plan_work(self.task, [item("replacement")])

    def test_replaced_plan_before_claim_uses_new_contract_owner_and_directions(self):
        self.rt.plan_work(self.task, [item("a")])
        original = self.rt.work.claim
        revised = False

        def claim(task_id, item_id):
            nonlocal revised
            if not revised:
                revised = True
                self.rt.command(self.task, "Must honor the new requirement")
                self.rt.plan_work(self.task, [{**item("a", owner="product"), "objective": "New contract"}])
            return original(task_id, item_id)

        with patch.object(self.rt.work, "claim", side_effect=claim):
            self.rt.collaborate(self.task)
        execution = self.driver.calls[0]
        self.assertEqual(execution["contract"]["owner"], "product")
        self.assertEqual(execution["contract"]["objective"], "New contract")
        self.assertEqual(execution["customer_directions"][0]["instruction"], "Must honor the new requirement")
        self.assertTrue(self.rt.work.view(self.task)["stage_accepted"])

    def test_change_immediately_after_claim_does_not_execute_or_leave_uncertain_work(self):
        self.rt.plan_work(self.task, [item("a")])
        original = self.rt.work.claim

        def claim(task_id, item_id):
            result = original(task_id, item_id)
            self.rt.command(self.task, "A change immediately after claiming")
            return result

        with patch.object(self.rt.work, "claim", side_effect=claim):
            self.rt.collaborate(self.task)
        self.assertEqual(self.driver.calls, [])
        current = self.rt.work.view(self.task)["items"][0]
        self.assertEqual(current["status"], "stale")
        self.assertFalse(current["in_flight"])

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
        calls = len(self.driver.calls)
        self.rt.collaborate(self.task)
        self.assertEqual(len(self.driver.calls), calls)
        invalidated = list(self.rt.log.iter_events(task_id=self.task, event_types=("work.invalidated",)))
        self.assertEqual(invalidated[0].payload["affected"], ["a"])
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
        calls = len(self.driver.calls)
        self.rt.battle(self.task)
        self.assertEqual(len(self.driver.calls), calls)
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
