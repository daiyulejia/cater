from __future__ import annotations

import argparse
import contextlib
import copy
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cater.cli import cmd_checks
from cater.events import Actor
from cater.planning import requirement_sources, validate_plan
from cater.runtime import Runtime
from cater.work import WorkConflict, validate_items
from test_work import WorkDriver, item


class CheckObligationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "checks.db"
        self.driver = WorkDriver()
        self.drivers = {r: self.driver for r in ("product", "design", "dev", "test")}
        self.rt = Runtime(self.db, drivers=self.drivers)
        self.addCleanup(self.rt.close)
        self.task = self.rt.create_task("保留实际检查的对象和验收要求")
        self.rt.plan_work(self.task, [item("a", local=True), item("b", local=True)])

    def contract(self):
        return self.rt.work.check_contract(self.task)

    def revise(self, changes, revision=None):
        return self.rt.revise_checks(self.task, changes, reason="核对实际检查对象后修订",
            expected_revision=self.contract()["revision"] if revision is None else revision)

    def current_items(self):
        return copy.deepcopy(self.rt.work.view(self.task)["items"])

    def test_identical_text_on_distinct_objects_remains_two_obligations(self):
        rows = self.contract()["obligations"]
        self.assertEqual(rows[0]["text"], rows[1]["text"])
        self.assertNotEqual(rows[0]["id"], rows[1]["id"])
        before = self.rt.log.count()
        with self.assertRaises(ValueError):
            self.rt.plan_work(self.task, [item("a", local=True)])
        self.assertEqual(self.rt.log.count(), before)

    def test_replanning_cannot_change_id_object_subject_or_requirement(self):
        transforms = [
            lambda rows: rows[0].update(id="renamed"),
            lambda rows: rows[0]["criteria"][1].update(id="renamed"),
            lambda rows: rows[0]["criteria"][1].update(text="A weaker check"),
            lambda rows: rows[0]["criteria"][1].update(subject="A different target"),
            lambda rows: rows[0]["criteria"][1].update(obligation_id="invented"),
            lambda rows: rows[0]["criteria"].pop(),
        ]
        for transform in transforms:
            with self.subTest(transform=transform):
                rows = self.current_items()
                transform(rows)
                before = self.rt.log.count()
                with self.assertRaises(ValueError):
                    self.rt.plan_work(self.task, rows)
                self.assertEqual(self.rt.log.count(), before)

    def test_same_obligation_cannot_be_cloned_to_another_work_item(self):
        rows = self.current_items()
        rows[1]["criteria"][1]["obligation_id"] = rows[0]["criteria"][1]["obligation_id"]
        with self.assertRaises(ValueError):
            self.rt.plan_work(self.task, rows)

    def test_changed_business_objective_keeps_explicit_original_check_subject(self):
        rows = self.current_items()
        old_subject = rows[0]["criteria"][1]["subject"]
        rows[0]["objective"] = "补充分工和说明，但仍核对原检查对象"
        self.rt.plan_work(self.task, rows)
        self.assertEqual(self.contract()["obligations"][0]["subject"], old_subject)
        self.assertEqual(self.rt.work.view(self.task)["items"][0]["objective"], rows[0]["objective"])

    def test_new_check_can_be_added_without_losing_existing_ones(self):
        rows = self.current_items()
        rows.append(item("c", local=True))
        self.rt.plan_work(self.task, rows)
        self.assertEqual(len(self.contract()["obligations"]), 3)

    def test_explicit_retirement_requires_plan_removal_and_cannot_silently_restore(self):
        required = self.contract()["obligations"][0]
        event = self.revise([{"id": required["id"], "action": "retire"}])
        self.assertEqual(event.actor, Actor.human())
        self.assertEqual(self.rt.work.scope(self.task)["checks_revision"], event.seq)
        rows = self.current_items()
        with self.assertRaises(ValueError):
            self.rt.plan_work(self.task, rows)
        rows[0]["criteria"].pop()
        self.rt.plan_work(self.task, rows)
        self.assertEqual(self.contract()["obligations"][0]["status"], "retired")
        with self.assertRaises(ValueError):
            self.rt.plan_work(self.task, [item("a", local=True), item("b", local=True)])
        self.revise([{"id": required["id"], "action": "restore"}])
        self.rt.plan_work(self.task, [item("a", local=True), item("b", local=True)])
        self.assertEqual(self.contract()["obligations"][0]["id"], required["id"])

    def test_rebinding_is_only_allowed_after_trusted_revision(self):
        required = self.contract()["obligations"][0]
        self.revise([{"id": required["id"], "action": "update", "work_item_id": "new-a",
                      "criterion_id": "regression", "subject": "指定的新检查对象"}])
        rows = self.current_items()
        rows[0]["id"] = "new-a"
        rows[0]["criteria"][1].update(id="regression", subject="指定的新检查对象")
        self.rt.plan_work(self.task, rows)
        current = self.contract()["obligations"][0]
        self.assertEqual(current["id"], required["id"])
        self.assertEqual(current["work_item_id"], "new-a")

    def test_revision_has_compare_and_swap_and_does_not_change_legacy_scope_shape(self):
        self.assertNotIn("checks_revision", self.rt.work.scope(self.task))
        previous = self.contract()["revision"]
        check_id = self.contract()["obligations"][0]["id"]
        self.revise([{"id": check_id, "action": "update", "text": "核对全部调用路径"}], previous)
        before = self.rt.log.count()
        with self.assertRaises(WorkConflict):
            self.revise([{"id": check_id, "action": "retire"}], previous)
        self.assertEqual(self.rt.log.count(), before)

    def test_invalid_revisions_do_not_write_partial_history(self):
        a, b = self.contract()["obligations"]
        changes = [[], [{"id": "unknown", "action": "retire"}],
            [{"id": a["id"], "action": "retire"}, {"id": a["id"], "action": "restore"}],
            [{"id": a["id"], "action": "retire", "text": "extra"}],
            [{"id": a["id"], "action": "update", "text": ""}],
            [{"id": a["id"], "action": "update", "subject": False}],
            [{"id": a["id"], "action": "update", "work_item_id": b["work_item_id"]}],
            [{"id": a["id"], "action": "update", "status": "retired"}]]
        for change in changes:
            with self.subTest(change=change):
                before = self.rt.log.count()
                with self.assertRaises(ValueError):
                    self.revise(change)
                self.assertEqual(self.rt.log.count(), before)

    def test_in_flight_execution_prevents_revision_even_after_lease_expiry(self):
        self.rt.work.claim(self.task, "a", lease_seconds=1)
        with patch("cater.work.time.time", return_value=10**12):
            with self.assertRaises(WorkConflict):
                self.revise([{"id": self.contract()["obligations"][0]["id"], "action": "retire"}])

    def test_in_flight_local_check_prevents_revision(self):
        self.rt.collaborate(self.task)
        self.rt.work.start_check(self.task, "a", "behavior")
        with self.assertRaises(WorkConflict):
            self.revise([{"id": self.contract()["obligations"][0]["id"], "action": "retire"}])

    def test_revision_invalidates_old_stage_receipts(self):
        self.rt.collaborate(self.task)
        for name in ("a", "b"):
            self.rt.verify_work(self.task, name, "behavior", [sys.executable, "check.py"])
        self.rt.collaborate(self.task)
        self.assertTrue(self.rt.work.view(self.task)["stage_accepted"])
        self.revise([{"id": self.contract()["obligations"][0]["id"], "action": "update",
                      "text": "还需验证调用方兼容性"}])
        view = self.rt.work.view(self.task)
        self.assertFalse(view["scope_current"])
        self.assertFalse(view["stage_accepted"])
        self.assertTrue(all(i["status"] == "stale" for i in view["items"]))
        with self.assertRaises(WorkConflict):
            self.rt.delivery.archive(self.task)

    def test_overall_check_cannot_be_bypassed_by_revision_or_wrong_actor_receipt(self):
        started = self.rt.log.emit(type="delivery.check_started", task_id=self.task,
            actor=Actor.system("local_verifier"), payload={"delivery_digest": "old-bundle", "criterion_id": "integration_behavior", "lease_until": 0})
        receipt = {"delivery_digest": "old-bundle", "criterion_id": "integration_behavior", "check_attempt_seq": started.seq}
        self.rt.log.emit(type="verification.completed", task_id=self.task, actor=Actor.human(), payload=receipt)
        change = [{"id": self.contract()["obligations"][0]["id"], "action": "retire"}]
        with self.assertRaises(WorkConflict):
            self.revise(change)
        self.rt.log.emit(type="verification.completed", task_id=self.task, actor=Actor.system("local_verifier"), payload=receipt)
        self.revise(change)

    def test_overall_review_pending_and_uncertain_prevent_revision(self):
        actor = Actor.agent("store_manager", "delivery_reviewer")
        started = self.rt.log.emit(type="delivery.review_started", task_id=self.task,
            actor=actor, payload={"delivery_digest": "old-bundle", "lease_until": 0})
        change = [{"id": self.contract()["obligations"][0]["id"], "action": "retire"}]
        with self.assertRaises(WorkConflict):
            self.revise(change)
        self.rt.log.emit(type="delivery.review_finished", task_id=self.task, actor=actor,
            payload={"delivery_digest": "old-bundle", "attempt_seq": started.seq, "status": "uncertain"})
        with self.assertRaises(WorkConflict):
            self.revise(change)

    def test_agent_cannot_replace_trusted_contract_projection(self):
        contract, scope = self.contract(), self.rt.work.scope(self.task)
        self.rt.log.emit(type="checks.contract_revised", task_id=self.task, actor=Actor.agent("dev", "worker"),
            payload={"obligations": [], "reason": "Pretend approved"})
        self.assertEqual(self.contract(), contract)
        self.assertEqual(self.rt.work.scope(self.task), scope)

    def test_registry_survives_restart_and_keeps_retired_definitions(self):
        self.revise([{"id": self.contract()["obligations"][0]["id"], "action": "retire"}])
        rows = self.current_items()
        rows[0]["criteria"].pop()
        self.rt.plan_work(self.task, rows)
        with Runtime(self.db) as reopened:
            self.assertEqual(reopened.work.check_contract(self.task), self.contract())
            self.assertTrue(reopened.verify_chain().ok)

    def test_legacy_registry_seeds_only_latest_plan_not_obsolete_history(self):
        task = self.rt.create_task("兼容旧版本")
        for name in ("obsolete", "latest"):
            self.rt.log.emit(type="work.plan_created", task_id=task, actor=Actor.system("work_scheduler"),
                payload={"items": [item(name, local=True)], "source_seq": 0, "scope": self.rt.work.scope(task)})
        rows = self.rt.work.check_contract(task)["obligations"]
        self.assertEqual([r["work_item_id"] for r in rows], ["latest"])
        self.rt.plan_work(task, [item("latest", local=True)])

    def test_planning_installation_rechecks_concurrent_revision(self):
        view = self.rt.work.view(self.task)
        rows = self.current_items()
        self.revise([{"id": self.contract()["obligations"][0]["id"], "action": "retire"}])
        with self.assertRaises(WorkConflict):
            self.rt.work.plan(self.task, rows, role_ids={r.id for r in self.rt.roles}, source_seq=0,
                expected_scope=view["scope"], expected_plan_seq=view["plan_seq"])

    def test_model_planning_cannot_collapse_same_text_checks_or_restore_retired_check(self):
        def response(rows):
            sources = requirement_sources(self.rt.log.read(task_id=self.task))
            context = {"roles": [{"id": r.id, "worker": True} for r in self.rt.roles],
                       "sources": sources, "previous_plan": self.rt.work.view(self.task)}
            raw = {"status": "ready", "summary": "核对当前要求后安排工作", "items": rows,
                   "coverage": [{"source_id": s["id"], "item_ids": [r["id"] for r in rows],
                                 "superseded_by": None, "reason": "保持要求覆盖", "quote": s["text"]}
                                for s in sources], "reuse": []}
            return raw, context
        raw, context = response([item("a", local=True)])
        with self.assertRaises(ValueError):
            validate_plan(raw, context)
        self.revise([{"id": self.contract()["obligations"][0]["id"], "action": "retire"}])
        raw, context = response([item("a", local=True), item("b", local=True)])
        with self.assertRaises(ValueError):
            validate_plan(raw, context)

    def test_local_check_metadata_rejects_empty_values_and_review_impersonation(self):
        for field, value in (("obligation_id", ""), ("subject", ""), ("subject", False)):
            rows = [item("a", local=True)]
            rows[0]["criteria"][1][field] = value
            with self.assertRaises(ValueError):
                validate_items(rows, {"dev", "test"})
        rows = [item("a")]
        rows[0]["criteria"][0]["obligation_id"] = "fake"
        with self.assertRaises(ValueError):
            validate_items(rows, {"dev", "test"})

    def test_cli_reads_without_mutation_and_requires_explicit_revision(self):
        args = argparse.Namespace(task_id=self.task, changes=None, reason=None, expected_revision=None)
        out, before = io.StringIO(), self.rt.log.count()
        with patch("cater.cli.open_runtime", return_value=Runtime(self.db)), contextlib.redirect_stdout(out):
            self.assertEqual(cmd_checks(args), 0)
        self.assertEqual(json.loads(out.getvalue()), self.contract())
        self.assertEqual(self.rt.log.count(), before)
        path = Path(self.tmp.name) / "revision.json"
        path.write_text(json.dumps([{"id": self.contract()["obligations"][0]["id"], "action": "retire"}]), encoding="utf-8")
        args.changes = str(path)
        with patch("cater.cli.open_runtime", return_value=Runtime(self.db)), self.assertRaises(ValueError):
            cmd_checks(args)
        args.reason, args.expected_revision = "已核对不再适用的检查", self.contract()["revision"]
        with patch("cater.cli.open_runtime", return_value=Runtime(self.db)), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cmd_checks(args), 0)
        self.assertEqual(self.contract()["obligations"][0]["status"], "retired")


if __name__ == "__main__":
    unittest.main()
