"""Offline durability, provenance, and cleanup boundaries for external runs."""

from dataclasses import replace
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from cater.activity_owner import activity_owner
from cater.events import Actor, EventLog
from cater.external_runs import ExternalRuns, HOST
from cater.work import WorkConflict


class ExternalRunsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "events.db"
        self.log = EventLog(self.path)
        self.addCleanup(lambda: self.log.close())
        self.rt = SimpleNamespace(log=self.log)
        self.team = SimpleNamespace(rt=self.rt)
        self.runs = ExternalRuns(self.team)
        self.role = SimpleNamespace(id="worker")
        self.task = "task-one"
        self.message, self.run = self.source()

    def emit(self, kind, payload, *, actor=HOST, task=None):
        return self.log.emit(type=kind, actor=actor, task_id=task or self.task, payload=payload)

    def source(self, task=None):
        message = self.emit("team.message_received", {"recipient": self.role.id},
                            actor=Actor.human(), task=task)
        run = self.emit("team.run_started",
                        {"role": self.role.id, "run_id": f"run-{message.seq}", "message_seq": message.seq},
                        task=task)
        return message, run

    def begin(self, **overrides):
        args = dict(task_id=self.task, role=self.role, run_start=self.run,
                    basis={"as_of_seq": self.run.seq, "as_of_hash": self.run.hash,
                           "token": "SECRET", "launch_env": {"KEY": "SECRET"}},
                    binding={"workspace_id": "ws", "revision": 7, "fingerprint": "workspace-fp",
                             "root": "/offline", "token": "SECRET", "launch_env": {"KEY": "SECRET"}},
                    tool_names=["workspace_read", "workspace_write"], driver_fingerprint="driver-fp")
        args.update(overrides)
        return self.runs.begin(**args)

    def end(self, **overrides):
        return self.emit("team.run_finished", {**self.run.payload, "status": "interrupted", **overrides})

    def finish(self, start, **overrides):
        args = dict(phase="failed", prompt_attempted=True, cleanup_complete=True, result="failed")
        args.update(overrides)
        return self.runs.finish(start, **args)

    def reconcile(self, start, **overrides):
        args = dict(task_id=self.task, started_seq=start.seq, expected_hash=start.hash,
                    invocation_stopped=True, note="Inspected and stopped the local invocation.")
        args.update(overrides)
        return self.runs.reconcile(**args)

    def test_start_committed_public_provenance_and_restart_projection(self):
        start = self.begin()
        self.assertFalse(self.log.db.in_transaction)
        self.assertEqual(start.payload["run_started_seq"], self.run.seq)
        self.assertEqual(start.payload["run_started_hash"], self.run.hash)
        self.assertEqual(start.payload["workspace_revision"], 7)
        self.assertNotIn("SECRET", json.dumps(start.payload))
        other = EventLog(self.path)
        try:
            recovered = ExternalRuns(SimpleNamespace(rt=SimpleNamespace(log=other)))
            row, = recovered.pending(self.task)
            self.assertEqual(row, {"seq": start.seq, "started_seq": start.seq,
                "name": "external_agent", "role": self.role.id, "run_id": self.run.payload["run_id"],
                "message_seq": self.message.seq, "outcome": "unsettled", "run_ended": False,
                "replay_authorized": False})
            self.end()
            self.assertEqual(recovered.pending(self.task)[0]["outcome"], "unknown")
            receipt = self.reconcile(start)
            self.assertEqual(receipt.actor, Actor.human())
            self.assertEqual(recovered.pending(self.task), [])
        finally:
            other.close()
        self.assertTrue(self.log.verify_chain().ok)

    def test_all_results_and_cleanup_are_independent(self):
        for attempted in (False, True):
            for result in ("returned", "yielded", "failed"):
                for cleanup in (False, True):
                    with self.subTest(attempted=attempted, result=result, cleanup=cleanup):
                        self.message, self.run = self.source()
                        start = self.begin()
                        receipt = self.finish(start, prompt_attempted=attempted, result=result,
                                              cleanup_complete=cleanup)
                        expected = "not_started" if not attempted else "unknown" if result == "failed" else result
                        self.assertEqual(receipt.payload["outcome"], expected)
                        pending = {r["seq"]: r for r in self.runs.pending(self.task)}
                        self.assertEqual(start.seq in pending, not cleanup)
                        if not cleanup:
                            self.assertEqual(pending[start.seq]["outcome"], "unknown")

    def test_begin_rejects_duplicate_run_start_without_appending(self):
        start = self.begin()
        before = self.log.read()
        with self.assertRaises(WorkConflict):
            self.begin()
        self.assertEqual(self.log.read(), before)
        self.assertFalse(self.log.db.in_transaction)
        self.assertEqual([r["started_seq"] for r in self.runs.pending(self.task)], [start.seq])

    def test_finished_attempt_cannot_replay_same_run_start_after_restart(self):
        for attempted, result in ((False, "failed"), (True, "returned"),
                                  (True, "yielded"), (True, "failed")):
            with self.subTest(prompt_attempted=attempted, result=result):
                self.message, self.run = self.source()
                start = self.begin()
                self.finish(start, prompt_attempted=attempted, result=result, cleanup_complete=True)
                self.assertEqual(self.runs.pending(self.task), [])
                # Leave the host run open: rejection must come from no-replay,
                # not the separate guard against an already ended host run.
                self.log.close()
                self.log = EventLog(self.path)
                self.rt.log = self.log
                self.runs = ExternalRuns(self.team)
                before = self.log.read()
                with self.assertRaises(WorkConflict):
                    self.begin()
                self.assertEqual(self.log.read(), before)
                self.assertFalse(self.log.db.in_transaction)
        self.assertTrue(self.log.verify_chain().ok)

    def test_outer_transactions_rejected_without_writes(self):
        start = self.begin()
        self.end()
        count = self.log.count()
        with self.log.atomic():
            for action in (self.begin, lambda: self.finish(start), lambda: self.reconcile(start)):
                with self.assertRaises(WorkConflict):
                    action()
        self.assertEqual(self.log.count(), count)

    def test_emit_failure_rolls_back(self):
        original = self.log.emit
        def crash(**kwargs):
            original(**kwargs)
            raise SystemExit("offline crash")
        count = self.log.count()
        with patch.object(self.log, "emit", side_effect=crash), self.assertRaises(SystemExit):
            self.begin()
        self.assertEqual(self.log.count(), count)
        self.assertFalse(self.log.db.in_transaction)
        start = self.begin()
        with patch.object(self.log, "emit", side_effect=crash), self.assertRaises(SystemExit):
            self.finish(start)
        self.assertEqual(len(self.runs.pending(self.task)), 1)

    def test_begin_rejects_wrong_task_role_actor_message_and_modified_event(self):
        for kwargs in ({"task_id": "other"}, {"role": SimpleNamespace(id="other")},
                       {"run_start": replace(self.run, hash="wrong")},
                       {"run_start": replace(self.run, payload={**self.run.payload, "message_seq": True})}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.begin(**kwargs)
        for p, actor in [
            (self.run.payload, Actor.agent("team", "team")),
            ({**self.run.payload, "message_seq": True}, HOST),
            ({**self.run.payload, "message_seq": float(self.message.seq)}, HOST),
            ({**self.run.payload, "message_seq": []}, HOST),
            ({**self.run.payload, "role": "other"}, HOST),
            ({**self.run.payload, "run_id": ""}, HOST),
        ]:
            run = self.emit("team.run_started", p, actor=actor)
            with self.assertRaises(ValueError):
                self.begin(run_start=run)
        self.end()
        with self.assertRaises(ValueError):
            self.begin()

    def test_forged_finishes_cannot_clear_pending(self):
        start = self.begin()
        base = {**self.runs._reference(start), "phase": "finished", "prompt_attempted": True,
                "cleanup_complete": True, "result": "returned", "outcome": "returned"}
        cases = [
            ({}, Actor.agent("team", "team"), self.task),
            ({"started_hash": "wrong"}, HOST, self.task),
            ({"role": "other"}, HOST, self.task),
            ({"run_id": "other"}, HOST, self.task),
            ({"message_seq": True}, HOST, self.task),
            ({"message_seq": float(self.message.seq)}, HOST, self.task),
            ({"started_seq": True}, HOST, self.task),
            ({"started_seq": float(start.seq)}, HOST, self.task),
            ({"started_seq": []}, HOST, self.task),
            ({"cleanup_complete": 1}, HOST, self.task),
            ({"prompt_attempted": 1}, HOST, self.task),
            ({"outcome": "not_started"}, HOST, self.task),
            ({}, HOST, "other"),
        ]
        for changes, actor, task in cases:
            with self.subTest(changes=changes, actor=actor, task=task):
                self.emit("team.external_run_finished", {**base, **changes}, actor=actor, task=task)
                self.assertEqual(len(self.runs.pending(self.task)), 1)
        self.finish(start)
        self.assertEqual(self.runs.pending(self.task), [])

    def test_finish_validation_and_immutable_idempotency(self):
        start = self.begin()
        for kwargs in ({"cleanup_complete": 1}, {"cleanup_complete": None},
                       {"prompt_attempted": 0}, {"result": "unknown"}, {"phase": ""}):
            with self.assertRaises(ValueError):
                self.finish(start, **kwargs)
        receipt = self.finish(start)
        self.assertEqual(receipt, self.finish(start))
        with self.assertRaises(WorkConflict):
            self.finish(start, cleanup_complete=False)
        with self.assertRaises(ValueError):
            self.finish(replace(start, payload={**start.payload, "role": "other"}))

    def test_reconcile_requires_end_and_kernel_ownership(self):
        start = self.begin()
        with self.assertRaises(WorkConflict):
            self.reconcile(start)
        self.end(message_seq=True)
        with self.assertRaises(WorkConflict):
            self.reconcile(start)
        self.end()
        with activity_owner(self.log.path, self.task, "simple-team") as owned:
            self.assertTrue(owned)
            with self.assertRaises(WorkConflict):
                self.reconcile(start)
        self.assertEqual(len(self.runs.pending(self.task)), 1)
        self.reconcile(start)
        self.assertEqual(self.runs.pending(self.task), [])

    def test_reconcile_identity_validation_and_immutable_idempotency(self):
        start = self.begin()
        self.end()
        for kwargs in ({"started_seq": True}, {"invocation_stopped": 1},
                       {"invocation_stopped": False}, {"note": "  "}):
            with self.assertRaises(ValueError):
                self.reconcile(start, **kwargs)
        for kwargs in ({"task_id": "other"}, {"expected_hash": "wrong"}):
            with self.assertRaises(WorkConflict):
                self.reconcile(start, **kwargs)
        first = self.reconcile(start)
        self.assertEqual(first, self.reconcile(start))
        with self.assertRaises(WorkConflict):
            self.reconcile(start, note="different evidence")

    def test_forged_reconciliations_and_premature_human_receipt(self):
        start = self.begin()
        base = {**self.runs._reference(start), "invocation_stopped": True, "note": "checked",
                "outcome": "effects_reviewed_no_replay"}
        self.emit("team.external_run_reconciled", base, actor=Actor.human())
        self.end()
        self.assertEqual(len(self.runs.pending(self.task)), 1)
        for changes, actor in [
            ({}, HOST), ({}, Actor.agent("human", "human")), ({"started_hash": "wrong"}, Actor.human()),
            ({"role": "other"}, Actor.human()), ({"run_id": "other"}, Actor.human()),
            ({"message_seq": True}, Actor.human()), ({"started_seq": True}, Actor.human()),
            ({"started_seq": float(start.seq)}, Actor.human()),
            ({"invocation_stopped": 1}, Actor.human()), ({"note": ""}, Actor.human()),
        ]:
            self.emit("team.external_run_reconciled", {**base, **changes}, actor=actor)
            self.assertEqual(len(self.runs.pending(self.task)), 1)
        self.reconcile(start)
        self.assertEqual(self.runs.pending(self.task), [])

    def test_external_cleanup_and_reconciliation_do_not_settle_raw_tools(self):
        from cater.team import TeamService

        self.rt.workspaces = SimpleNamespace(pending=lambda **kwargs: [])
        self.team.external_runs = self.runs
        start = self.begin()
        tool = self.emit("team.tool_started", {**self.run.payload, "name": "workspace_write",
                        "call_id": "call-one", "arguments": {"path": "output.txt"}})
        self.finish(start, cleanup_complete=False)
        self.end()
        self.reconcile(start)
        rows = TeamService._pending_operations(self.team, self.task)
        self.assertEqual([r["started_seq"] for r in rows], [tool.seq])
        self.assertEqual(rows[0]["outcome"], "unknown")
        self.message, self.run = self.source()
        clean = self.begin()
        self.finish(clean)
        self.assertEqual([r["started_seq"] for r in TeamService._pending_operations(self.team, self.task)], [tool.seq])

    def test_cross_task_message_cannot_bind_a_run(self):
        message, _ = self.source(task="other")
        run = self.emit("team.run_started", {**self.run.payload, "message_seq": message.seq})
        with self.assertRaises(ValueError):
            self.begin(run_start=run)

    def test_malformed_start_cannot_be_settled_by_apparently_matching_finish(self):
        start = self.begin()
        for changes in (
            {"execution_contract": {"version": True, "kind": "external_agent"}},
            {"run_started_hash": "wrong"},
            {"run_started_seq": float(self.run.seq)},
            {"run_started_seq": []},
        ):
            malformed = self.emit("team.external_run_started", {**start.payload, **changes})
            self.emit("team.external_run_finished", {
                **self.runs._reference(malformed), "phase": "finished", "prompt_attempted": True,
                "cleanup_complete": True, "result": "returned", "outcome": "returned"})
            self.assertIn(malformed.seq, [r["seq"] for r in self.runs.pending(self.task)])
            with self.assertRaises(ValueError):
                self.finish(malformed)

    def test_reconcile_crash_rolls_back_and_can_retry_after_restart(self):
        start = self.begin()
        self.end()
        original = self.log.emit
        def crash(**kwargs):
            original(**kwargs)
            raise SystemExit("offline reconciliation crash")
        count = self.log.count()
        with patch.object(self.log, "emit", side_effect=crash), self.assertRaises(SystemExit):
            self.reconcile(start)
        self.assertEqual(self.log.count(), count)
        self.log.close()
        self.log = EventLog(self.path)
        self.rt.log = self.log
        self.assertEqual(len(self.runs.pending(self.task)), 1)
        receipt = self.reconcile(start)
        self.assertEqual(receipt, self.reconcile(start))
        self.assertEqual(self.runs.pending(self.task), [])

    def test_explicit_retirement_targets_only_bound_orphan_and_is_idempotent(self):
        start = self.begin()
        _, unrelated_run = self.source()
        unrelated = self.begin(run_start=unrelated_run)
        self.source(task="other")
        before = self.log.read()
        receipt = self.reconcile(start, retire_host_run=True)
        appended = self.log.read()[len(before):]
        self.assertEqual([e.type for e in appended],
                         ["team.run_finished", "team.external_run_reconciled"])
        retired = appended[0]
        self.assertEqual(retired.actor, HOST)
        self.assertEqual(retired.task_id, self.task)
        self.assertEqual(retired.payload, {
            **{k: self.run.payload[k] for k in ("role", "run_id", "message_seq")},
            "status": "interrupted", "recovery_of": self.run.seq,
            "reason": "Inspected and stopped the local invocation.",
        })
        self.assertEqual(receipt.actor, Actor.human())
        row, = self.runs.pending(self.task)
        self.assertEqual(row["started_seq"], unrelated.seq)
        self.assertFalse(row["run_ended"])
        self.assertFalse(row["replay_authorized"])
        self.assertEqual(self.log.read()[:len(before)], before)
        settled = self.log.read()
        self.assertEqual(self.reconcile(start, retire_host_run=True), receipt)
        self.assertEqual(self.reconcile(start), receipt)
        self.assertEqual(self.log.read(), settled)
        self.assertTrue(self.log.verify_chain().ok)

    def test_retirement_rejects_invalid_attestation_identity_and_live_owner_without_writes(self):
        start = self.begin()
        before = self.log.read()
        for value in (None, 0, 1, "true", [], {}):
            with self.subTest(flag=value), self.assertRaises(ValueError):
                self.reconcile(start, retire_host_run=value)
        for overrides in ({"expected_hash": "wrong"}, {"task_id": "other"},
                          {"started_seq": self.run.seq}, {"invocation_stopped": False},
                          {"invocation_stopped": 1}, {"note": " "}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self.reconcile(start, retire_host_run=True, **overrides)
        with activity_owner(self.log.path, self.task, "simple-team") as owned:
            self.assertTrue(owned)
            with self.assertRaises(WorkConflict):
                self.reconcile(start, retire_host_run=True)
        with self.log.atomic(), self.assertRaises(WorkConflict):
            self.reconcile(start, retire_host_run=True)
        self.assertEqual(self.log.read(), before)

    def test_retirement_flag_does_not_duplicate_existing_host_end(self):
        start = self.begin()
        self.end()
        before = self.log.read()
        self.reconcile(start, retire_host_run=True)
        self.assertEqual([e.type for e in self.log.read()[len(before):]],
                         ["team.external_run_reconciled"])

    def test_retirement_does_not_change_already_confirmed_cleanup(self):
        start = self.begin()
        self.finish(start)
        before = self.log.read()
        with self.assertRaises(WorkConflict):
            self.reconcile(start, retire_host_run=True)
        self.assertEqual(self.log.read(), before)

    def test_retirement_and_reconciliation_roll_back_together_on_each_emit_failure(self):
        start = self.begin()
        before = self.log.read()
        original = self.log.emit
        for failed_type in ("team.run_finished", "team.external_run_reconciled"):
            def crash(**kwargs):
                event = original(**kwargs)
                if kwargs["type"] == failed_type:
                    raise SystemExit("offline crash after append")
                return event
            with self.subTest(failed_type=failed_type):
                with patch.object(self.log, "emit", side_effect=crash), self.assertRaises(SystemExit):
                    self.reconcile(start, retire_host_run=True)
                self.assertEqual(self.log.read(), before)
                self.assertFalse(self.log.db.in_transaction)
                self.assertFalse(self.runs.pending(self.task)[0]["run_ended"])
        self.log.close()
        self.log = EventLog(self.path)
        self.rt.log = self.log
        self.reconcile(start, retire_host_run=True)
        self.assertEqual(self.runs.pending(self.task), [])
        self.assertTrue(self.log.verify_chain().ok)


if __name__ == "__main__":
    unittest.main()
