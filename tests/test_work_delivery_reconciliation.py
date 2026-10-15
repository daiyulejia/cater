from __future__ import annotations

import unittest
from unittest.mock import patch

from cater.drivers import OpenAICompatibleDriver
from cater.events import Actor
from cater.planning import _execution_contract
from cater.runtime import Runtime
from cater.work import WorkConflict
from test_work import item


class WorkDeliveryReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.rt = Runtime(":memory:")
        self.addCleanup(self.rt.close)
        self.task = self.rt.create_task("Revise checks after delivery review reconciliation")
        self.rt.plan_work(self.task, [item("a", local=True)])
        self.contract = self.rt.work.check_contract(self.task)

    def start(self, digest="old-bundle", *, execution_contract=None):
        # An older delivery can remain unresolved while the current plan is pending.
        self.rt.log.emit(type="delivery.prepared", task_id=self.task, actor=Actor.system("store_manager"),
            payload={"digest": digest, "reviewer": "test", "manifest": {
                "scope": self.rt.work.scope(self.task), "plan_seq": self.rt.work.view(self.task)["plan_seq"],
                "criteria": [], "items": [], "files": [], "sources": []}})
        return self.rt.log.emit(type="delivery.review_started", task_id=self.task,
            actor=Actor.agent("test", "delivery_reviewer"), payload={
                "delivery_digest": digest, "check_seqs": {}, "lease_until": 0,
                **({"execution_contract": execution_contract} if execution_contract is not None else {}),
                "scope": self.rt.work.scope(self.task), "plan_seq": self.rt.work.view(self.task)["plan_seq"]})

    def finish(self, started, status="uncertain"):
        return self.rt.log.emit(type="delivery.review_finished", task_id=self.task, actor=started.actor,
            payload=self.rt.delivery._finish_payload(started, {"status": status, "reason": "Fixture response"}))

    def reconcile(self, started, finish=None):
        evidence = self.rt.log.emit(type="human.commanded", task_id=self.task, actor=Actor.human(),
            payload={"intent": "status", "instruction": "Confirmed the old invocation stopped and reconciled its effects"})
        return self.rt.delivery.reconcile(self.task, attempt_seq=started.seq, attempt_hash=started.hash,
            expected_finish_seq=finish.seq if finish else None,
            expected_delivery_digest=self.rt.delivery.view(self.task)["digest"],
            expected_scope=self.rt.work.scope(self.task), expected_plan_seq=self.rt.work.view(self.task)["plan_seq"],
            expected_check_seqs={}, evidence_seqs=[evidence.seq], reason="Operator checked the original invocation",
            invocation_stopped=True, effects_reconciled=True)

    def revise(self):
        return self.rt.work.revise_checks(self.task,
            [{"id": self.contract["obligations"][0]["id"], "action": "retire"}],
            expected_revision=self.contract["revision"], reason="The check obligation has been reassessed")

    def test_uncertain_history_no_longer_blocks_after_real_reconcile_api(self):
        started = self.start()
        with self.assertRaises(WorkConflict):
            self.revise()
        uncertain = self.finish(started)
        with self.assertRaises(WorkConflict):
            self.revise()
        reconciled = self.reconcile(started, uncertain)
        self.assertEqual(reconciled.actor, Actor.human())
        self.assertEqual(reconciled.payload["status"], "interrupted")
        revision = self.revise()
        self.assertEqual(self.rt.work.check_contract(self.task)["revision"], revision.seq)
        self.assertEqual(self.rt.work.check_contract(self.task)["obligations"][0]["status"], "retired")
        finishes = list(self.rt.log.iter_events(task_id=self.task, event_types=("delivery.review_finished",)))
        self.assertEqual([e.payload["status"] for e in finishes], ["uncertain", "interrupted"])
        self.assertTrue(self.rt.verify_chain().ok)

    def test_reconciliation_of_missing_finish_also_allows_revision(self):
        started = self.start()
        self.reconcile(started)
        self.revise()

    def test_system_text_lease_recovery_also_allows_revision(self):
        driver = OpenAICompatibleDriver("test", endpoint="https://fixture.invalid", model="fixture", api_key_env="UNUSED")
        started = self.start(execution_contract=_execution_contract(driver))
        self.finish(started)
        with self.assertRaises(WorkConflict):
            self.revise()
        recovered = self.rt.delivery.recover(self.task)
        self.assertEqual(recovered.actor, Actor.system("delivery_recovery"))
        self.assertEqual(recovered.payload["recovery"], "text_lease_expired")
        self.revise()

    def test_invalid_recovery_records_cannot_unlock_check_revision(self):
        started = self.start()
        uncertain = self.finish(started)
        evidence = self.rt.log.emit(type="human.commanded", task_id=self.task, actor=Actor.human(),
            payload={"intent": "status", "instruction": "Operator checked the invocation"})
        payload = self.rt.delivery._finish_payload(started, {
            "status": "interrupted", "recovery": "operator_reconciled", "reconciled_finish_seq": uncertain.seq,
            "invocation_stopped": True, "effects_reconciled": True, "evidence_seqs": [evidence.seq]})
        for actor, overrides in ((started.actor, {}), (Actor.human(), {"attempt_hash": "wrong"}),
                                 (Actor.human(), {"reconciled_finish_seq": None}),
                                 (Actor.human(), {"effects_reconciled": False})):
            with self.subTest(actor=actor, overrides=overrides):
                self.rt.log.emit(type="delivery.review_finished", task_id=self.task, actor=actor,
                                 payload={**payload, **overrides})
                with self.assertRaises(WorkConflict):
                    self.revise()
        self.reconcile(started, uncertain)
        self.revise()

    def test_late_uncertain_record_cannot_replace_effective_terminal_state(self):
        started = self.start()
        uncertain = self.finish(started)
        self.reconcile(started, uncertain)
        self.finish(started)
        self.revise()

    def test_another_unresolved_digest_still_blocks_revision(self):
        old = self.start()
        self.reconcile(old, self.finish(old))
        current = self.start("new-bundle")
        with self.assertRaises(WorkConflict):
            self.revise()
        self.reconcile(current)
        self.revise()

    def test_cas_rechecks_new_delivery_claim_before_writing_revision(self):
        old = self.start()
        self.reconcile(old, self.finish(old))
        emit = self.rt.log.emit
        raced = False

        def racing_emit(**kwargs):
            nonlocal raced
            if kwargs["type"] == "checks.contract_revised" and not raced:
                raced = True
                self.start("racing-bundle")
            return emit(**kwargs)

        with patch.object(self.rt.log, "emit", side_effect=racing_emit), self.assertRaises(WorkConflict):
            self.revise()
        self.assertEqual(self.rt.work.check_contract(self.task)["revision"], self.contract["revision"])
        self.assertEqual(list(self.rt.log.iter_events(task_id=self.task, event_types=("checks.contract_revised",))), [])


if __name__ == "__main__":
    unittest.main()
