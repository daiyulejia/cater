"""Offline fault injection only: no model API calls or external client processes."""

from contextlib import redirect_stdout
from dataclasses import replace
import gc
import io
import json
from pathlib import Path
import sqlite3
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import weakref

from cater.acp import AcpTimeout
from cater.cli import main
from cater.events import Actor, Event, EventLog
from cater.external_runs import ExternalRuns
from cater.external_scratch import LaunchDirectory
from cater.runtime import Runtime
from cater.team import HOST
from cater.work import WorkConflict
import test_team_external as external_fixture


class ExternalScratchCleanupTests(unittest.TestCase):
    private_cleanup = "OFFLINE_PRIVATE_CLEANUP credential=never-export-this"
    private_settlement = "OFFLINE_PRIVATE_SETTLEMENT credential=never-export-this"

    def setUp(self):
        # Composition avoids rediscovering the imported fixture's TestCase tests.
        self.fixture = external_fixture.TeamExternalTests(methodName="runTest")
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.rt, self.task = self.fixture.rt, self.fixture.task
        mkdtemp = tempfile.mkdtemp
        self.enterContext(patch("cater.external_scratch.tempfile.mkdtemp",
                                side_effect=lambda **kw: mkdtemp(dir=self.fixture.root, **kw)))
        process = self.enterContext(patch("subprocess.Popen", side_effect=AssertionError(
            "Offline scratch injection must never start a client or call a model API")))
        self.addCleanup(process.assert_not_called)

    def events(self, kind):
        return list(self.rt.log.iter_events(task_id=self.task, event_types=(kind,)))

    def new_scratch(self):
        message = self.rt.log.emit(type="team.message_received", actor=Actor.human(),
            task_id=self.task, payload={"recipient": self.fixture.manager})
        run = self.rt.log.emit(type="team.run_started", actor=HOST, task_id=self.task,
            payload={"role": self.fixture.manager, "run_id": f"offline-scratch-{message.seq}",
                     "message_seq": message.seq})
        attempt = self.rt.team.external_runs.begin(self.task, self.rt.organization.leader, run,
            basis={"as_of_seq": run.seq, "as_of_hash": run.hash}, binding={},
            tool_names=[], driver_fingerprint="offline-injected-driver")
        return LaunchDirectory(self.rt.log, attempt)

    def deny_cleanup(self):
        return patch("cater.external_scratch.shutil.rmtree",
                     side_effect=PermissionError(self.private_cleanup))

    def fail_settlement_write(self, error):
        original = EventLog.emit

        def emit(log, **kwargs):
            # Only this receipt fails; other database writes remain real.
            if kwargs["type"] == "team.external_directory_settled":
                raise error
            return original(log, **kwargs)

        return patch.object(EventLog, "emit", autospec=True, side_effect=emit)

    def assert_unconfirmed_settlements(self, writes, error_type, count):
        attempts = [call for call in writes.call_args_list
                    if call.kwargs["type"] == "team.external_directory_settled"]
        self.assertEqual(len(attempts), count)
        created = self.events("team.external_directory_created")
        self.assertEqual(len(created), count)
        self.assertEqual(self.events("team.external_directory_settled"), [])
        finished = self.events("team.run_finished")
        self.assertEqual(len(finished), count)
        for event in finished:
            self.assertEqual(event.payload["launch_directory_settlement_error"], error_type)
        rows = self.rt.team.external_runs.launch_directories(self.task)
        self.assertEqual([row["created_seq"] for row in rows], [event.seq for event in created])
        for row, event in zip(rows, created):
            self.assertEqual(row["status"], "unconfirmed")
            self.assertIsNone(row["settlement"])
            self.assertEqual(row["started_hash"], event.payload["started_hash"])
        persisted = self.rt.log.read(task_id=self.task)
        self.assertNotIn(self.private_settlement, json.dumps([e.payload for e in persisted]))
        other = EventLog(self.rt.log.path)
        try:
            recovered = ExternalRuns(SimpleNamespace(rt=SimpleNamespace(log=other)))
            self.assertEqual(other.read(task_id=self.task), persisted)
            self.assertEqual(recovered.launch_directories(self.task), rows)
            self.assertTrue(other.verify_chain().ok)
        finally:
            other.close()
        self.assertEqual(self.rt.log.read(task_id=self.task), persisted)

    def assert_directory_receipts(self, status, count):
        created = self.events("team.external_directory_created")
        settled = self.events("team.external_directory_settled")
        self.assertEqual(len(created), count)
        self.assertEqual(len(settled), count)
        starts = {e.seq: e for e in self.events("team.external_run_started")}
        rows = self.rt.team.external_runs.launch_directories(self.task)
        self.assertEqual(len(rows), count)
        for source, finish, row in zip(created, settled, rows):
            start = starts[source.payload["started_seq"]]
            self.assertEqual(source.actor, HOST)
            self.assertEqual(finish.actor, HOST)
            self.assertEqual(source.category, "driver")
            self.assertEqual(finish.category, "driver")
            self.assertLess(start.seq, source.seq)
            self.assertLess(source.seq, finish.seq)
            self.assertEqual(finish.payload["created_seq"], source.seq)
            self.assertEqual(finish.payload["created_hash"], source.hash)
            for event in (source, finish):
                self.assertEqual(event.payload["started_hash"], start.hash)
                for key in ("role", "run_id", "message_seq"):
                    self.assertEqual(event.payload[key], start.payload[key])
            self.assertEqual(finish.payload["status"], status)
            self.assertEqual(row["created_seq"], source.seq)
            self.assertEqual(row["status"], status)
            self.assertEqual(row["path"], source.payload["path"])
            self.assertEqual(set(row["identity"]), {"device", "inode"})
            self.assertEqual(row["settlement"]["status"], status)
            self.assertEqual(Path(row["path"]).exists(), status != "removed")
        self.assertTrue(self.rt.verify_chain().ok)

    def test_offline_permission_error_preserves_wait_formal_return_and_delivery(self):
        with self.deny_cleanup() as remove:
            self.fixture.test_delegate_wait_publish_return_and_delivery()
        self.assertEqual(remove.call_count, 3)
        self.assert_directory_receipts("retained", 3)
        self.assertEqual([e.payload["status"] for e in self.events("team.run_finished")],
                         ["waiting", "completed", "completed"])
        self.assertTrue(all(e.payload["cleanup_complete"]
                            for e in self.events("team.external_run_finished")))
        completion, = self.events("team.objective_completed")
        delivered, = [e for e in self.events("msg.said") if "delivery_seq" in e.payload]
        self.assertEqual(delivered.payload["delivery_seq"], completion.seq)
        self.assertEqual(delivered.payload["text"], "Offline fixture delivered")
        self.assertEqual(self.rt.team.external_runs.pending(self.task), [])

    def test_offline_permission_error_preserves_review_rejections_and_approval(self):
        with self.deny_cleanup() as remove:
            self.fixture.test_two_review_rejections_third_version_approval_then_formal_return()
        self.assertEqual(remove.call_count, 9)
        self.assert_directory_receipts("retained", 9)
        self.assertEqual(self.rt.team.external_runs.pending(self.task), [])
        self.assertFalse([e for e in self.events("team.run_finished")
                          if e.payload["status"] == "failed"])

    def test_offline_cleanup_error_preserves_original_model_error(self):
        def timeout(run):
            self.fixture.publish(run, "Artifact before injected timeout\n")
            run.state = replace(run.state, failure_phase="prompting")
            raise AcpTimeout("OFFLINE_PRIVATE_MODEL_ERROR")

        self.fixture.driver.scripts = {self.fixture.manager: [timeout]}
        with self.deny_cleanup() as remove:
            self.fixture.receive()
            self.fixture.drain()
        self.assertEqual(remove.call_count, 1)
        self.assertEqual(len(self.fixture.driver.runs), 1)
        finished, = self.events("team.run_finished")
        self.assertEqual(finished.payload["status"], "failed")
        self.assertEqual(finished.payload["error_type"], "AcpTimeout")
        self.assertEqual(finished.payload["external_failure_code"], "deadline_exceeded")
        self.assertEqual(finished.payload["failure_stage"], "prompting")
        external, = self.events("team.external_run_finished")
        self.assertEqual(external.payload["outcome"], "unknown")
        self.assertTrue(external.payload["cleanup_complete"])
        self.assertEqual(len(self.events("team.model_called")), 1)
        artifact, = self.rt.task(self.task).deliverables
        self.assertEqual(Path(artifact["local_path"]).read_text(encoding="utf-8"),
                         "Artifact before injected timeout\n")
        self.assertEqual(self.rt.team.external_runs.pending(self.task), [])
        self.assert_directory_receipts("retained", 1)
        persisted = json.dumps([e.payload for e in self.rt.log.read(task_id=self.task)])
        self.assertNotIn(self.private_cleanup, persisted)
        self.assertNotIn("OFFLINE_PRIVATE_MODEL_ERROR", persisted)

    def test_offline_settlement_write_failure_preserves_primary_timeout(self):
        def timeout(run):
            run.state = replace(run.state, failure_phase="prompting")
            raise AcpTimeout("OFFLINE_PRIVATE_MODEL_TIMEOUT")

        self.fixture.driver.scripts = {self.fixture.manager: [timeout]}
        health = self.rt._driver_health
        with self.fail_settlement_write(sqlite3.OperationalError(self.private_settlement)) as writes, \
                patch.object(health, "record_failure", wraps=health.record_failure) as failure:
            self.fixture.receive()
            self.fixture.drain()
        failure.assert_called_once_with(self.fixture.driver.name)
        finished, = self.events("team.run_finished")
        self.assertEqual(finished.payload["status"], "failed")
        self.assertEqual(finished.payload["error_type"], "AcpTimeout")
        self.assertEqual(finished.payload["external_failure_code"], "deadline_exceeded")
        self.assertEqual(finished.payload["failure_stage"], "prompting")
        external, = self.events("team.external_run_finished")
        self.assertEqual(external.payload["outcome"], "unknown")
        self.assertTrue(external.payload["cleanup_complete"])
        self.assertEqual(len(self.fixture.driver.runs), 1)
        self.assertEqual(len(self.events("team.model_called")), 1)
        self.assertEqual(self.rt.team.external_runs.pending(self.task), [])
        self.assert_unconfirmed_settlements(writes, "OperationalError", 1)
        self.assertNotIn("OFFLINE_PRIVATE_MODEL_TIMEOUT",
                         json.dumps([e.payload for e in self.rt.log.read(task_id=self.task)]))

    def test_offline_settlement_write_failure_preserves_host_control(self):
        reason = "Offline original host control stopped this run"

        def controlled(run):
            raise WorkConflict(reason)

        self.fixture.driver.scripts = {self.fixture.manager: [controlled]}
        health = self.rt._driver_health
        with self.fail_settlement_write(OSError(self.private_settlement)) as writes, \
                patch.object(health, "record_failure", wraps=health.record_failure) as failure:
            self.fixture.receive()
            self.fixture.drain()
        failure.assert_not_called()
        finished, = self.events("team.run_finished")
        self.assertEqual(finished.payload["status"], "interrupted")
        self.assertEqual(finished.payload["reason"], reason)
        self.assertNotIn("error_type", finished.payload)
        self.assertNotIn("external_failure_code", finished.payload)
        self.assertEqual(self.rt.team.external_runs.pending(self.task), [])
        self.assertFalse(self.rt._is_circuit_open(self.fixture.driver.name))
        self.assert_unconfirmed_settlements(writes, "OSError", 1)

    def test_offline_settlement_write_failure_preserves_handoffs_and_channel_health(self):
        health = self.rt._driver_health
        with self.fail_settlement_write(PermissionError(self.private_settlement)) as writes, \
                patch.object(health, "record_failure", wraps=health.record_failure) as failure, \
                patch.object(health, "record_success", wraps=health.record_success) as success:
            self.fixture.test_delegate_wait_publish_return_and_delivery()
        failure.assert_not_called()
        self.assertEqual(success.call_count, 3)
        self.assertFalse(self.rt._is_circuit_open(self.fixture.driver.name))
        self.assertEqual([e.payload["status"] for e in self.events("team.run_finished")],
                         ["waiting", "completed", "completed"])
        completion, = self.events("team.objective_completed")
        delivered, = [e for e in self.events("msg.said") if "delivery_seq" in e.payload]
        self.assertEqual(delivered.payload["delivery_seq"], completion.seq)
        self.assertEqual(delivered.payload["text"], "Offline fixture delivered")
        self.assertEqual(self.rt.team._pending_operations(self.task), [])
        self.assert_unconfirmed_settlements(writes, "PermissionError", 3)

    def test_offline_settlement_write_failure_cannot_release_process_or_tool_holds(self):
        orphan = []

        def unconfirmed(run):
            run.cleanup_complete = False
            start = self.events("team.external_run_started")[-1]
            # Offline host receipt injection; no workspace command executes.
            orphan.append(self.rt.log.emit(type="team.tool_started", actor=HOST,
                task_id=self.task, payload={
                    **{key: start.payload[key] for key in ("role", "run_id", "message_seq")},
                    "external_started_seq": start.seq, "name": "workspace_write",
                    "call_id": "offline-unresolved-write", "arguments": {"path": "unknown.txt"}}))
            run.call("wait_for_reply")

        self.fixture.driver.scripts = {self.fixture.manager: [unconfirmed]}
        health = self.rt._driver_health
        with self.fail_settlement_write(sqlite3.OperationalError(self.private_settlement)) as writes, \
                patch("cater.external_scratch.shutil.rmtree") as remove, \
                patch.object(health, "record_failure", wraps=health.record_failure) as failure:
            self.fixture.receive()
            self.fixture.drain()
        remove.assert_not_called()
        failure.assert_not_called()
        self.assertEqual(self.events("team.run_finished")[0].payload["status"], "interrupted")
        external, = self.events("team.external_run_finished")
        self.assertIs(external.payload["cleanup_complete"], False)
        pending = self.rt.team._pending_operations(self.task)
        self.assertEqual({row["started_seq"] for row in pending},
                         {external.payload["started_seq"], orphan[0].seq})
        self.assertEqual({row["name"] for row in pending}, {"external_agent", "workspace_write"})
        self.assertTrue(all(row["outcome"] == "unknown" and row["run_ended"]
                            and not row["replay_authorized"] for row in pending))
        self.assert_unconfirmed_settlements(writes, "OperationalError", 1)
        self.assertEqual(self.rt.team._pending_operations(self.task), pending)
        row, = self.rt.team.external_runs.launch_directories(self.task)
        self.assertTrue(Path(row["path"]).is_dir())

    def test_offline_factory_failure_has_durable_directory_and_preserves_setup_error(self):
        observed = []

        def fail_factory():
            self.assertFalse(self.rt.log.db.in_transaction)
            other = EventLog(self.rt.log.path)
            try:
                created, = other.iter_events(task_id=self.task,
                    event_types=("team.external_directory_created",))
                info = Path(created.payload["path"]).lstat()
                self.assertEqual(created.payload["identity"],
                                 {"device": info.st_dev, "inode": info.st_ino})
                self.assertEqual(list(other.iter_events(task_id=self.task,
                    event_types=("team.tool_started",))), [])
                observed.append(created.seq)
            finally:
                other.close()
            raise ValueError("OFFLINE_PRIVATE_FACTORY_ERROR")

        self.fixture.driver.factory_observer = fail_factory
        with self.deny_cleanup() as remove:
            self.fixture.receive()
            self.fixture.drain()
        self.assertEqual(len(observed), 1)
        self.assertEqual(remove.call_count, 1)
        self.assertEqual(self.fixture.driver.runs, [])
        finished, = self.events("team.run_finished")
        self.assertEqual(finished.payload["status"], "failed")
        self.assertEqual(finished.payload["error_type"], "ValueError")
        self.assertEqual(finished.payload["failure_stage"], "setup")
        external, = self.events("team.external_run_finished")
        self.assertEqual(external.payload["outcome"], "not_started")
        self.assertFalse(external.payload["prompt_attempted"])
        self.assertTrue(external.payload["cleanup_complete"])
        self.assertEqual(self.events("team.model_called"), [])
        self.assertEqual(self.rt.team.external_runs.pending(self.task), [])
        self.assert_directory_receipts("retained", 1)

    def test_offline_unconfirmed_client_defers_removal_and_keeps_pending_process(self):
        def unconfirmed(run):
            run.cleanup_complete = False
            run.call("wait_for_reply")

        self.fixture.driver.scripts = {self.fixture.manager: [unconfirmed]}
        with patch("cater.external_scratch.shutil.rmtree") as remove:
            self.fixture.receive()
            self.fixture.drain()
            remove.assert_not_called()
        self.assert_directory_receipts("deferred", 1)
        settlement, = self.events("team.external_directory_settled")
        self.assertIs(settlement.payload["client_stopped"], False)
        external, = self.events("team.external_run_finished")
        self.assertIs(external.payload["cleanup_complete"], False)
        pending, = self.rt.team.external_runs.pending(self.task)
        self.assertEqual(pending["started_seq"], external.payload["started_seq"])
        self.assertEqual(pending["outcome"], "unknown")
        self.assertTrue(pending["run_ended"])
        self.assertFalse(pending["replay_authorized"])
        self.assertEqual(self.events("team.run_finished")[0].payload["status"], "interrupted")

    def test_removed_receipt_and_repeated_close_are_idempotent(self):
        scratch = self.new_scratch()
        path = Path(scratch.path)
        (path / "offline-client-file").write_text("fixture", encoding="ascii")
        event = scratch.close(client_stopped=True)
        self.assertIsInstance(event, Event)
        self.assertEqual(event.payload["status"], "removed")
        self.assertFalse(path.exists())
        with patch("cater.external_scratch.shutil.rmtree") as remove:
            self.assertIs(scratch.close(client_stopped=True), event)
            self.assertIs(scratch.close(client_stopped=False), event)
            remove.assert_not_called()
        self.assertEqual(len(self.events("team.external_directory_settled")), 1)
        self.assert_directory_receipts("removed", 1)

    def test_retained_receipt_is_sanitized_and_never_retries(self):
        scratch = self.new_scratch()
        with self.deny_cleanup() as remove:
            event = scratch.close(client_stopped=True)
            self.assertIs(scratch.close(client_stopped=True), event)
            self.assertEqual(remove.call_count, 1)
        with patch("cater.external_scratch.shutil.rmtree") as retry:
            self.assertIs(scratch.close(client_stopped=True), event)
            retry.assert_not_called()
        self.assertEqual(event.payload["status"], "retained")
        self.assertEqual(event.payload["error_type"], "PermissionError")
        self.assertNotIn(self.private_cleanup, json.dumps(event.payload))
        self.assert_directory_receipts("retained", 1)

    def test_deferred_directory_survives_gc_without_finalizer_or_retry(self):
        scratch = self.new_scratch()
        path, reference = Path(scratch.path), weakref.ref(scratch)
        sentinel = path / "live-client-file"
        sentinel.write_text("keep while client unknown", encoding="ascii")
        with patch("cater.external_scratch.shutil.rmtree") as remove:
            event = scratch.close(client_stopped=False)
            self.assertEqual(event.payload["status"], "deferred")
            self.assertIs(scratch.close(client_stopped=True), event)
            del scratch
            gc.collect()
            self.assertIsNone(reference())
            remove.assert_not_called()
        self.assertEqual(sentinel.read_text(encoding="ascii"), "keep while client unknown")
        self.assert_directory_receipts("deferred", 1)

    def test_replaced_directory_identity_preserves_original_and_replacement(self):
        scratch = self.new_scratch()
        path = Path(scratch.path)
        original = path.with_name(path.name + "-original")
        (path / "original.txt").write_text("original", encoding="ascii")
        path.rename(original)
        path.mkdir()
        (path / "replacement.txt").write_text("replacement", encoding="ascii")
        with patch("cater.external_scratch.shutil.rmtree") as remove:
            event = scratch.close(client_stopped=True)
            remove.assert_not_called()
        self.assertEqual(event.payload["status"], "identity_changed")
        self.assertEqual((original / "original.txt").read_text(encoding="ascii"), "original")
        self.assertEqual((path / "replacement.txt").read_text(encoding="ascii"), "replacement")
        self.assert_directory_receipts("identity_changed", 1)

    def test_symlink_replacement_never_removes_outside_target(self):
        scratch = self.new_scratch()
        path = Path(scratch.path)
        original = path.with_name(path.name + "-original")
        outside = self.fixture.root / "outside-launch-directory"
        outside.mkdir()
        sentinel = outside / "do-not-delete.txt"
        sentinel.write_text("outside bytes", encoding="ascii")
        path.rename(original)
        try:
            path.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"Directory symlink unavailable on this host: {type(exc).__name__}")
        self.addCleanup(path.unlink)
        with patch("cater.external_scratch.shutil.rmtree") as remove:
            event = scratch.close(client_stopped=True)
            remove.assert_not_called()
        self.assertEqual(event.payload["status"], "identity_changed")
        self.assertTrue(path.is_symlink())
        self.assertTrue(original.is_dir())
        self.assertEqual(sentinel.read_text(encoding="ascii"), "outside bytes")
        self.assert_directory_receipts("identity_changed", 1)

    def test_injected_symlink_and_reparse_metadata_rejected_with_original_identity(self):
        for mode, attributes in ((stat.S_IFLNK, 0), (stat.S_IFDIR, 0x400)):
            with self.subTest(mode=mode, attributes=attributes):
                scratch = self.new_scratch()
                sentinel = Path(scratch.path) / "must-survive.txt"
                sentinel.write_text("owned bytes", encoding="ascii")
                # Offline lstat injection also exercises link guards on Windows
                # hosts where creating a real symlink requires extra privileges.
                info = SimpleNamespace(st_mode=mode, st_file_attributes=attributes,
                    st_dev=scratch.identity["device"], st_ino=scratch.identity["inode"])
                with patch.object(Path, "lstat", return_value=info), \
                        patch("cater.external_scratch.shutil.rmtree") as remove:
                    event = scratch.close(client_stopped=True)
                    remove.assert_not_called()
                self.assertEqual(event.payload["status"], "identity_changed")
                self.assertEqual(sentinel.read_text(encoding="ascii"), "owned bytes")
        self.assert_directory_receipts("identity_changed", 2)

    def test_projection_requires_host_receipts_and_exact_persisted_links(self):
        scratch = self.new_scratch()
        original = dict(scratch.created.payload)
        invalid_created = [
            (Actor.human(), {}), (HOST, {"started_hash": "wrong"}),
            (HOST, {"started_seq": True}), (HOST, {"role": "wrong-role"}),
            (HOST, {"run_id": "wrong-run"}), (HOST, {"message_seq": True}),
            (HOST, {"identity": {"device": True, "inode": 1}}),
        ]
        for actor, changes in invalid_created:
            self.rt.log.emit(type="team.external_directory_created", actor=actor,
                task_id=self.task, payload={**original, **changes})
        pending = self.rt.team.external_runs.pending(self.task)
        row, = self.rt.team.external_runs.launch_directories(self.task)
        self.assertEqual(row["created_seq"], scratch.created.seq)
        self.assertEqual(row["status"], "unconfirmed")
        self.assertIsNone(row["settlement"])
        settlement = {**scratch.reference, "created_seq": scratch.created.seq,
            "created_hash": scratch.created.hash, "status": "removed", "client_stopped": True}
        invalid_settled = [
            (Actor.human(), {}), (HOST, {"created_hash": "wrong"}),
            (HOST, {"created_seq": True}), (HOST, {"started_hash": "wrong"}),
            (HOST, {"run_id": "wrong-run"}), (HOST, {"message_seq": True}),
            (HOST, {"client_stopped": 1}), (HOST, {"client_stopped": False}),
        ]
        for actor, changes in invalid_settled:
            with self.subTest(actor=actor, changes=changes):
                self.rt.log.emit(type="team.external_directory_settled", actor=actor,
                    task_id=self.task, payload={**settlement, **changes})
                row, = self.rt.team.external_runs.launch_directories(self.task)
                self.assertEqual(row["status"], "unconfirmed")
                self.assertIsNone(row["settlement"])
        scratch.close(client_stopped=True)
        row, = self.rt.team.external_runs.launch_directories(self.task)
        self.assertEqual(row["status"], "removed")
        self.assertEqual(self.rt.team.external_runs.pending(self.task), pending)
        self.assertTrue(self.rt.verify_chain().ok)

    def test_restart_cli_shows_directory_states_without_mutations_or_raw_error_text(self):
        retained = self.new_scratch()
        with self.deny_cleanup():
            retained.close(client_stopped=True)
        self.rt.team.external_runs.finish(retained.attempt, phase="finished",
            prompt_attempted=True, cleanup_complete=True, result="returned")
        removed = self.new_scratch()
        removed.close(client_stopped=True)
        deferred = self.new_scratch()
        deferred.close(client_stopped=False)
        unconfirmed = self.new_scratch()
        expected = self.rt.team.external_runs.launch_directories(self.task)
        pending = self.rt.team.external_runs.pending(self.task)
        self.assertEqual([row["status"] for row in expected],
                         ["retained", "removed", "deferred", "unconfirmed"])
        self.assertEqual([row["started_seq"] for row in pending],
                         [removed.attempt.seq, deferred.attempt.seq, unconfirmed.attempt.seq])
        db = self.rt.log.path
        self.rt.close()
        self.rt = self.fixture.rt = Runtime(db, roles_path=self.fixture.roles,
            drivers={self.fixture.driver.name: self.fixture.driver})
        self.fixture.addCleanup(self.rt.close)
        before = self.rt.log.read()
        with patch("cater.external_scratch.shutil.rmtree") as remove:
            self.assertEqual(self.rt.team.external_runs.launch_directories(self.task), expected)
            output = io.StringIO()
            with redirect_stdout(output):
                status = main(["--db", db, "external-run", self.task])
            remove.assert_not_called()
        self.assertEqual(status, 0)
        body = json.loads(output.getvalue())
        self.assertEqual(body["launch_directories"], expected)
        self.assertEqual([row["started_seq"] for row in body["pending"]],
                         [row["started_seq"] for row in pending])
        self.assertEqual(body["launch_directories"][0]["settlement"],
                         {"status": "retained", "client_stopped": True,
                          "error_type": "PermissionError"})
        self.assertIsNone(body["launch_directories"][-1]["settlement"])
        self.assertNotIn(self.private_cleanup, output.getvalue())
        self.assertEqual(self.rt.team.external_runs.pending(self.task), pending)
        self.assertEqual(self.rt.log.read(), before)
        self.assertTrue(self.rt.verify_chain().ok)


if __name__ == "__main__":
    unittest.main()
