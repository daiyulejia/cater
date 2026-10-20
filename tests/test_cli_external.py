"""Offline operator reconciliation through the public CLI and durable service."""

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cater.activity_owner import activity_owner
from cater.cli import main
from cater.events import Actor, EventLog
from cater.external_runs import HOST
from cater.runtime import Runtime


class ExternalRunCliTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.db = self.root / "events.db"
        process = self.enterContext(patch("subprocess.Popen", side_effect=AssertionError("No processes")))
        replay = self.enterContext(patch("cater.team.TeamService.run", side_effect=AssertionError("No replay")))
        self.addCleanup(process.assert_not_called)
        self.addCleanup(replay.assert_not_called)
        for target in ("cater.team.TeamService._drain", "cater.team_recovery.TeamRecovery._notify",
                       "cater.external_agent.ExternalAgentRun.run"):
            call = self.enterContext(patch(target, side_effect=AssertionError("No execution or notifications")))
            self.addCleanup(call.assert_not_called)
        with Runtime(self.db) as rt:
            self.task = rt.create_task("Offline orphan reconciliation")
            self.other = rt.create_task("Unrelated task")
            role = rt.worker_roles[0]
            message = rt.log.emit(type="team.message_received", actor=Actor.human(),
                                  task_id=self.task, payload={"recipient": role.id})
            self.run = rt.log.emit(type="team.run_started", actor=HOST, task_id=self.task,
                                   payload={"role": role.id, "run_id": "offline-run",
                                            "message_seq": message.seq})
            self.start = rt.team.external_runs.begin(
                self.task, role, self.run,
                basis={"as_of_seq": self.run.seq, "as_of_hash": self.run.hash},
                binding={"workspace_id": "offline", "revision": 2,
                         "fingerprint": "workspace-fp", "root": str(self.root / "workspace")},
                tool_names=["workspace_write"], driver_fingerprint="driver-fp",
            )

    def invoke(self, *flags, task=None):
        output = io.StringIO()
        with redirect_stdout(output):
            status = main(["--db", str(self.db), "external-run", task or self.task, *flags])
        return status, output.getvalue()

    def reconcile_flags(self, *, seq=None, expected_hash=None, reason="Inspected and stopped invocation"):
        return ["--reconcile", str(self.start.seq if seq is None else seq),
                "--expected-hash", self.start.hash if expected_hash is None else expected_hash,
                "--invocation-stopped", "--reason", reason]

    def events(self):
        with Runtime(self.db) as rt:
            return rt.log.read()

    def end(self):
        with Runtime(self.db) as rt:
            rt.log.emit(type="team.run_finished", actor=HOST, task_id=self.task,
                        payload={**self.run.payload, "status": "interrupted"})

    def test_read_pending_metadata_and_start_hash_without_writes(self):
        before = self.events()
        status, output = self.invoke()
        self.assertEqual(status, 0)
        body = json.loads(output)
        self.assertEqual(body["task_id"], self.task)
        self.assertIsNone(body["reconciliation"])
        row, = body["pending"]
        self.assertEqual(row["seq"], self.start.seq)
        self.assertEqual(row["started_seq"], self.start.seq)
        self.assertEqual(row["started_hash"], self.start.hash)
        self.assertEqual(row["metadata"], self.start.payload)
        self.assertFalse(row["run_ended"])
        self.assertFalse(row["replay_authorized"])
        self.assertEqual(row["outcome"], "unsettled")
        self.assertEqual(self.events(), before)
        self.end()
        before = self.events()
        status, output = self.invoke()
        row, = json.loads(output)["pending"]
        self.assertEqual(status, 0)
        self.assertTrue(row["run_ended"])
        self.assertEqual(row["outcome"], "unknown")
        self.assertEqual(self.events(), before)

    def test_explicit_attestation_reconciles_after_restart_without_replay(self):
        with Runtime(self.db) as rt:
            rt.team.external_runs.finish(self.start, phase="failed", prompt_attempted=True,
                                         cleanup_complete=False, result="failed")
        self.end()
        before = self.events()
        status, output = self.invoke(*self.reconcile_flags())
        self.assertEqual(status, 0)
        body = json.loads(output)
        self.assertEqual(body["pending"], [])
        receipt = body["reconciliation"]
        self.assertEqual(receipt["type"], "team.external_run_reconciled")
        self.assertEqual(receipt["actor"], Actor.human().to_dict())
        self.assertEqual(receipt["payload"]["started_seq"], self.start.seq)
        self.assertEqual(receipt["payload"]["started_hash"], self.start.hash)
        self.assertTrue(receipt["payload"]["invocation_stopped"])
        self.assertEqual(receipt["payload"]["note"], "Inspected and stopped invocation")
        self.assertEqual(receipt["payload"]["outcome"], "effects_reviewed_no_replay")
        after = self.events()
        self.assertEqual(after[:-1], before)
        self.assertEqual(len(after), len(before) + 1)
        repeated_status, repeated_output = self.invoke(*self.reconcile_flags())
        self.assertEqual(repeated_status, status)
        self.assertEqual(json.loads(repeated_output), body)
        self.assertEqual(self.events(), after)
        self.assertEqual(json.loads(self.invoke()[1])["pending"], [])
        with Runtime(self.db) as rt:
            self.assertTrue(rt.verify_chain().ok)

    def test_partial_or_stray_attestation_flags_are_rejected_without_writes(self):
        self.end()
        complete = self.reconcile_flags()
        invalid = [
            ["--reconcile", str(self.start.seq)],
            [item for item in complete if item != "--invocation-stopped"],
            complete[:2] + complete[4:],
            complete[:-2],
            self.reconcile_flags(reason="  "),
            self.reconcile_flags(expected_hash="  "),
            ["--expected-hash", self.start.hash],
            ["--invocation-stopped"],
            ["--reason", "checked"],
            ["--retire-host-run"],
        ]
        invalid += [flags + ["--retire-host-run"] for flags in invalid[:-1]]
        before = self.events()
        for flags in invalid:
            with self.subTest(flags=flags):
                status, output = self.invoke(*flags)
                self.assertEqual(status, 1)
                self.assertIn("error:", output)
                self.assertEqual(self.events(), before)

    def test_wrong_hash_sequence_or_task_cannot_reconcile(self):
        self.end()
        before = self.events()
        cases = [(self.reconcile_flags(expected_hash="wrong"), self.task),
                 (self.reconcile_flags(seq=self.run.seq), self.task),
                 (self.reconcile_flags(seq=999999), self.task),
                 (self.reconcile_flags(seq=0), self.task),
                 (self.reconcile_flags(), self.other)]
        for flags, task in cases:
            with self.subTest(flags=flags, task=task):
                status, output = self.invoke(*flags, task=task)
                self.assertEqual(status, 1)
                self.assertIn("error:", output)
                self.assertEqual(self.events(), before)

    def test_unended_host_run_and_live_ownership_still_block_reconciliation(self):
        before = self.events()
        status, output = self.invoke(*self.reconcile_flags())
        self.assertEqual(status, 1)
        self.assertIn("finished host run", output)
        self.assertEqual(self.events(), before)
        self.end()
        before = self.events()
        with activity_owner(str(self.db), self.task, "simple-team") as owned:
            self.assertTrue(owned)
            status, output = self.invoke(*self.reconcile_flags())
            self.assertEqual(status, 1)
            self.assertIn("still owns", output)
            self.assertEqual(self.events(), before)
        self.assertEqual(self.invoke(*self.reconcile_flags())[0], 0)

    def test_changed_reconciliation_evidence_is_rejected(self):
        self.end()
        self.assertEqual(self.invoke(*self.reconcile_flags())[0], 0)
        before = self.events()
        status, output = self.invoke(*self.reconcile_flags(reason="changed evidence"))
        self.assertEqual(status, 1)
        self.assertIn("cannot be changed", output)
        self.assertEqual(self.events(), before)

    def test_confirmed_cleanup_cannot_be_reconciled(self):
        with Runtime(self.db) as rt:
            rt.team.external_runs.finish(self.start, phase="failed", prompt_attempted=True,
                                         cleanup_complete=True, result="failed")
        self.end()
        before = self.events()
        status, output = self.invoke(*self.reconcile_flags())
        self.assertEqual(status, 1)
        self.assertIn("already confirmed", output)
        self.assertEqual(self.events(), before)

    def test_empty_and_missing_task_reads(self):
        before = self.events()
        status, output = self.invoke(task=self.other)
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output)["pending"], [])
        status, output = self.invoke(task="missing-task")
        self.assertEqual(status, 1)
        self.assertIn("not found", output)
        self.assertEqual(self.events(), before)

    def test_retire_host_run_reconciles_only_target_without_execution(self):
        with Runtime(self.db) as rt:
            role = rt.worker_roles[0]
            message = rt.log.emit(type="team.message_received", actor=Actor.human(), task_id=self.task,
                                  payload={"recipient": role.id})
            other_run = rt.log.emit(type="team.run_started", actor=HOST, task_id=self.task,
                                    payload={"role": role.id, "run_id": "unrelated-run",
                                             "message_seq": message.seq})
            other_start = rt.team.external_runs.begin(
                self.task, role, other_run, basis={}, binding={}, tool_names=[],
                driver_fingerprint="other-driver")
        before = self.events()
        status, output = self.invoke(*self.reconcile_flags(), "--retire-host-run")
        self.assertEqual(status, 0)
        body = json.loads(output)
        row, = body["pending"]
        self.assertEqual(row["started_seq"], other_start.seq)
        self.assertFalse(row["run_ended"])
        self.assertFalse(row["replay_authorized"])
        after = self.events()
        self.assertEqual(after[:-2], before)
        retired, reconciled = after[-2:]
        self.assertEqual(retired.type, "team.run_finished")
        self.assertEqual(retired.actor, HOST)
        self.assertEqual(retired.payload["run_id"], self.run.payload["run_id"])
        self.assertEqual(retired.payload["message_seq"], self.run.payload["message_seq"])
        self.assertEqual(retired.payload["role"], self.run.payload["role"])
        self.assertEqual(retired.payload["recovery_of"], self.run.seq)
        self.assertEqual(retired.payload["status"], "interrupted")
        self.assertEqual(retired.payload["reason"], "Inspected and stopped invocation")
        self.assertEqual(reconciled.type, "team.external_run_reconciled")
        self.assertEqual(body["reconciliation"]["seq"], reconciled.seq)
        status, output = self.invoke(*self.reconcile_flags(), "--retire-host-run")
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output), body)
        self.assertEqual(self.events(), after)

    def test_retire_orphan_rejects_wrong_identity_and_live_owner_without_appends(self):
        before = self.events()
        cases = [(self.reconcile_flags(expected_hash="wrong"), self.task),
                 (self.reconcile_flags(seq=self.run.seq), self.task),
                 (self.reconcile_flags(), self.other)]
        for flags, task in cases:
            with self.subTest(task=task, flags=flags):
                status, output = self.invoke(*flags, "--retire-host-run", task=task)
                self.assertEqual(status, 1)
                self.assertIn("error:", output)
                self.assertEqual(self.events(), before)
        with activity_owner(str(self.db), self.task, "simple-team") as owned:
            self.assertTrue(owned)
            status, output = self.invoke(*self.reconcile_flags(), "--retire-host-run")
            self.assertEqual(status, 1)
            self.assertIn("still owns", output)
            self.assertEqual(self.events(), before)

    def test_retire_cli_failure_rolls_back_both_receipts(self):
        original = EventLog.emit
        before = self.events()

        def fail(log, **kwargs):
            event = original(log, **kwargs)
            if kwargs["type"] == "team.external_run_reconciled":
                raise ValueError("offline receipt write failure")
            return event

        with patch.object(EventLog, "emit", fail):
            status, output = self.invoke(*self.reconcile_flags(), "--retire-host-run")
        self.assertEqual(status, 1)
        self.assertIn("offline receipt write failure", output)
        self.assertEqual(self.events(), before)
        self.assertFalse(json.loads(self.invoke()[1])["pending"][0]["run_ended"])
        self.assertEqual(self.invoke(*self.reconcile_flags(), "--retire-host-run")[0], 0)


if __name__ == "__main__":
    unittest.main()
