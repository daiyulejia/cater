"""Runtime integration for immutable copies, independent of work acceptance."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import socket
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from cater import artifact_store
from cater.artifacts import normalize_deliverables
from cater.events import Actor
from cater.runtime import Runtime
from test_delivery import DocumentWorkDriver
from test_work import item


class ArtifactPersistenceTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[1] / ".cater"
        base.mkdir(exist_ok=True)
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(dir=base, prefix="persistence-")))
        self.db = self.root / "work.db"
        self.driver = DocumentWorkDriver()
        self.drivers = {name: self.driver for name in ("product", "dev", "design", "test")}
        self.rt = Runtime(self.db, drivers=self.drivers)
        self.addCleanup(self.rt.close)
        self.task = self.rt.create_task("Produce a readable document")
        self.rt.plan_work(self.task, [item("a")])
        self.enterContext(patch.object(socket, "socket", side_effect=AssertionError("network forbidden")))
        self.enterContext(patch.object(subprocess, "Popen", side_effect=AssertionError("process forbidden")))

    def events(self, task=None):
        return list(self.rt.log.iter_events(task_id=task or self.task,
                    event_types=("artifacts.materialized", "artifacts.materialization_failed")))

    def submit(self, name="a", body="# Exact note\r\n\r\nOriginal bytes.", filename="note.md", task=None):
        task = task or self.task
        claim = self.rt.work.claim(task, name)
        owner = next(i["owner"] for i in self.rt.work.view(task)["items"] if i["id"] == name)
        artifact = normalize_deliverables([{"title": "Note", "summary": "Exact stored body",
            "content": body, "format": "markdown", "filename": "note.md"}])[0]
        artifact["filename"] = filename
        event = self.rt.log.emit(actor=Actor.agent(owner, "worker"), type="deliverable.published", task_id=task,
            payload={**artifact, "work_item_id": name, "attempt_id": claim.payload["attempt_id"]})
        return self.rt.work.submit(task, claim, [event.seq])

    def test_current_submission_materializes_exact_bytes_and_absolute_bound_paths(self):
        submission = self.submit()
        before = self.rt.work.view(self.task)
        receipts = self.rt.persist_artifacts(self.task)
        self.assertEqual(receipts, self.events())
        receipt = receipts[0]
        self.assertEqual(receipt.actor, Actor.system("artifact_store"))
        self.assertEqual(receipt.type, "artifacts.materialized")
        self.assertEqual(receipt.payload["submission_seq"], submission.seq)
        self.assertEqual(receipt.payload["submission_digest"], submission.payload["digest"])
        directory = Path(receipt.payload["directory"])
        self.assertTrue(directory.is_absolute())
        self.assertTrue(directory.is_relative_to(self.root / "artifacts"))
        self.assertTrue((directory / "manifest.json").is_file())
        for entry in receipt.payload["files"]:
            self.assertEqual(set(entry), {"seq", "content_sha256", "path"})
            path = Path(entry["path"])
            self.assertTrue(path.is_absolute())
            self.assertTrue(path.is_relative_to(directory))
            self.assertEqual(path.read_bytes(), b"# Exact note\r\n\r\nOriginal bytes.")
            self.assertEqual(entry["content_sha256"], submission.payload["hashes"][str(entry["seq"])])
        self.assertEqual(self.rt.work.view(self.task), before)
        self.assertFalse(before["stage_accepted"])
        self.assertTrue(self.rt.verify_chain().ok)

    def test_submission_is_automatically_persisted_before_independent_review(self):
        review = self.rt._review_work_item

        def inspect(task, contract):
            receipt = self.events()[-1]
            self.assertEqual(receipt.payload["submission_seq"], contract["submission"]["seq"])
            self.assertTrue(Path(receipt.payload["files"][0]["path"]).is_file())
            review(task, contract)

        with patch.object(self.rt, "_review_work_item", side_effect=inspect):
            self.rt.collaborate(self.task)
        self.assertEqual(len(self.events()), 1)
        self.assertTrue(self.rt.work.view(self.task)["stage_accepted"])

    def test_round_start_backfills_existing_submission_without_reproducing_work(self):
        self.submit()
        with patch.object(self.rt, "_review_work_item"):
            self.rt.collaborate(self.task)
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(self.driver.calls, [])
        self.assertFalse(self.rt.work.view(self.task)["stage_accepted"])

    def test_memory_runtime_never_calls_store_or_writes_a_receipt(self):
        with Runtime(drivers=self.drivers) as rt:
            task = rt.create_task("Memory only")
            rt.plan_work(task, [item("a")])
            with patch.object(artifact_store, "materialize") as store:
                rt.collaborate(task)
                self.assertEqual(rt.persist_artifacts(task), [])
                store.assert_not_called()
            self.assertFalse(any(e.type.startswith("artifacts.material") for e in rt.log.read(task_id=task)))

    def test_repeats_and_restart_reverify_disk_without_duplicate_receipts(self):
        self.submit()
        with patch.object(artifact_store, "materialize", wraps=artifact_store.materialize) as store:
            first = self.rt.persist_artifacts(self.task)
            self.assertEqual(self.rt.persist_artifacts(self.task), first)
            with Runtime(self.db, drivers=self.drivers) as reopened:
                self.assertEqual(reopened.persist_artifacts(self.task), first)
            self.assertEqual(store.call_count, 3)
        self.assertEqual(len(self.events()), 1)

    def test_tampering_after_success_is_reported_without_overwriting_or_losing_submission(self):
        submission = self.submit()
        first = self.rt.persist_artifacts(self.task)[0]
        path = Path(first.payload["files"][0]["path"])
        path.write_bytes(b"tampered")
        second = self.rt.persist_artifacts(self.task)[0]
        self.assertEqual(second.type, "artifacts.materialization_failed")
        self.assertIn("tampering", second.payload["reason"])
        self.assertEqual(second.payload["submission_seq"], submission.seq)
        self.assertEqual(self.rt.persist_artifacts(self.task), [second])
        self.assertEqual(len(self.events()), 2)
        self.assertEqual(path.read_bytes(), b"tampered")
        self.assertEqual(self.rt.work.view(self.task)["items"][0]["submission"]["seq"], submission.seq)

    def test_failure_is_retryable_and_does_not_block_acceptance_or_fail_work(self):
        with patch.object(artifact_store, "materialize", side_effect=PermissionError("fixture: disk unavailable")):
            self.rt.collaborate(self.task)
            self.rt.persist_artifacts(self.task)
        self.assertTrue(self.rt.work.view(self.task)["stage_accepted"])
        before = self.rt.work.view(self.task)
        self.assertEqual(len(self.events()), 1)
        failure = self.events()[0]
        self.assertEqual(failure.type, "artifacts.materialization_failed")
        self.assertIn("disk unavailable", failure.payload["reason"])
        success = self.rt.persist_artifacts(self.task)[0]
        self.assertEqual(success.type, "artifacts.materialized")
        self.assertEqual(success.payload["submission_seq"], failure.payload["submission_seq"])
        self.assertEqual(len(self.events()), 2)
        self.assertEqual(self.rt.work.view(self.task), before)
        self.assertFalse(any(e.type == "work.failed" for e in self.rt.log.read(task_id=self.task)))

    def test_one_failed_copy_does_not_prevent_other_submissions_from_materializing(self):
        self.rt.plan_work(self.task, [item("a"), item("b")])
        self.submit("a", filename="../escape.md")
        self.submit("b", body="Second note")
        receipts = self.rt.persist_artifacts(self.task)
        self.assertEqual([r.type for r in receipts], ["artifacts.materialization_failed", "artifacts.materialized"])
        self.assertEqual(len(self.events()), 2)
        self.assertFalse((self.root / "escape.md").exists())

    def test_new_submission_uses_new_version_and_preserves_original_files(self):
        self.submit()
        first = self.rt.persist_artifacts(self.task)[0]
        self.rt.work.rework(self.task, "a", "Write a revised note")
        self.submit(body="Revised note")
        second = self.rt.persist_artifacts(self.task)[0]
        self.assertNotEqual(first.payload["directory"], second.payload["directory"])
        self.assertEqual(Path(first.payload["files"][0]["path"]).read_bytes(), b"# Exact note\r\n\r\nOriginal bytes.")
        self.assertEqual(Path(second.payload["files"][0]["path"]).read_bytes(), b"Revised note")
        self.assertEqual(len(self.events()), 2)

    def test_reused_submission_gets_its_own_copy_and_receipt_without_worker_replay(self):
        old = self.submit()
        first = self.rt.persist_artifacts(self.task)[0]
        current = self.rt.work.view(self.task)
        self.rt.work.plan(self.task, [item("a")], role_ids={r.id for r in self.rt.roles},
            source_seq=current["source_seq"], planning={"reuse": [
                {"work_item_id": "a", "submission_seq": old.seq, "reason": "Recheck the existing bytes"}]})
        self.rt.collaborate(self.task)
        new = self.rt.work.view(self.task)["items"][0]["submission"]
        self.assertEqual(new["reused_from"], old.seq)
        self.assertEqual(len(self.events()), 2)
        second = self.events()[-1]
        self.assertEqual(second.payload["submission_seq"], new["seq"])
        self.assertNotEqual(first.payload["directory"], second.payload["directory"])
        self.assertEqual(Path(first.payload["files"][0]["path"]).read_bytes(),
                         Path(second.payload["files"][0]["path"]).read_bytes())
        self.assertTrue(all(c.get("phase") == "review" for c in self.driver.calls))

    def test_success_after_transient_failure_is_not_hidden_by_an_older_success(self):
        self.submit()
        first = self.rt.persist_artifacts(self.task)[0]
        with patch.object(artifact_store, "materialize", side_effect=OSError("fixture: temporarily unreadable")):
            self.rt.persist_artifacts(self.task)
        recovered = self.rt.persist_artifacts(self.task)[0]
        self.assertEqual(recovered.payload, first.payload)
        self.assertNotEqual(recovered.seq, first.seq)
        self.assertEqual([e.type for e in self.events()], ["artifacts.materialized",
                         "artifacts.materialization_failed", "artifacts.materialized"])

    def test_model_authored_receipt_cannot_suppress_host_receipt(self):
        self.submit()
        materialize = artifact_store.materialize

        def forge(*args):
            manifest = materialize(*args)
            submission = args[2]
            self.rt.log.emit(actor=Actor.agent("dev", "worker"), type="artifacts.materialized", task_id=self.task,
                payload={"submission_seq": submission["seq"], "submission_digest": submission["digest"],
                         "plan_seq": submission["plan_seq"], "work_item_id": submission["work_item_id"],
                         "attempt_id": submission["attempt_id"], "scope": submission["scope"]})
            return manifest

        with patch.object(artifact_store, "materialize", side_effect=forge):
            result = self.rt.persist_artifacts(self.task)
        self.assertEqual(len(self.events()), 2)
        self.assertEqual(result[0].actor, Actor.system("artifact_store"))
        self.assertIn("files", result[0].payload)

    def test_only_current_task_submissions_are_used_and_unsubmitted_artifacts_are_ignored(self):
        other = self.rt.create_task("Other task")
        self.rt.plan_work(other, [item("a")])
        self.submit(task=other)
        self.rt.log.emit(actor=Actor.agent("dev", "worker"), type="deliverable.published", task_id=self.task,
                         payload={"content": "Not submitted", "filename": "untrusted.md"})
        self.assertEqual(self.rt.persist_artifacts(self.task), [])
        self.assertFalse((self.root / "artifacts").exists())
        self.assertEqual(self.events(other), [])

    def test_existing_transaction_defers_io_until_after_commit(self):
        self.submit()
        with patch.object(artifact_store, "materialize", wraps=artifact_store.materialize) as store:
            with self.rt.log.atomic():
                self.assertEqual(self.rt.persist_artifacts(self.task), [])
                store.assert_not_called()
            self.rt.persist_artifacts(self.task)
            store.assert_called_once()

    def test_file_io_holds_no_database_transaction_and_concurrent_success_is_deduplicated(self):
        self.submit()
        local = threading.local()
        barrier = threading.Barrier(2)
        materialize = artifact_store.materialize

        def publish(*args):
            self.assertFalse(local.rt.log.db.in_transaction)
            result = materialize(*args)
            barrier.wait(timeout=10)
            return result

        def persist():
            with Runtime(self.db, drivers=self.drivers) as rt:
                local.rt = rt
                return rt.persist_artifacts(self.task)

        with patch.object(artifact_store, "materialize", side_effect=publish), ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(persist) for _ in range(2)]
            receipts = [future.result(timeout=15) for future in futures]
        self.assertEqual(receipts[0], receipts[1])
        self.assertEqual(receipts[0][0].type, "artifacts.materialized")
        self.assertEqual(len(self.events()), 1)
        self.assertTrue(self.rt.verify_chain().ok)

    def test_stale_success_observation_cannot_overwrite_concurrent_tampering_failure(self):
        self.submit()
        materialize = artifact_store.materialize

        def tamper_after_verification(*args):
            manifest = materialize(*args)
            path = self.root / "artifacts" / manifest["directory"] / manifest["files"][0]["path"]
            path.write_bytes(b"tampered after verification")
            with Runtime(self.db, drivers=self.drivers) as concurrent, patch.object(
                    artifact_store, "materialize", materialize):
                concurrent.persist_artifacts(self.task)
            return manifest

        with patch.object(artifact_store, "materialize", side_effect=tamper_after_verification):
            result = self.rt.persist_artifacts(self.task)
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(result[0].type, "artifacts.materialization_failed")

    def test_crash_after_files_before_receipt_is_recovered_by_verifying_existing_version(self):
        self.submit()
        emit = self.rt.log.emit

        def interrupt(**kwargs):
            if kwargs["type"] == "artifacts.materialized":
                raise KeyboardInterrupt("fixture: process stops before receipt")
            return emit(**kwargs)

        with patch.object(self.rt.log, "emit", side_effect=interrupt), self.assertRaises(KeyboardInterrupt):
            self.rt.persist_artifacts(self.task)
        self.assertEqual(self.events(), [])
        self.rt.persist_artifacts(self.task)
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(len(list((self.root / "artifacts").glob("*/*/manifest.json"))), 1)

    def test_receipt_write_interruption_after_production_does_not_mark_work_uncertain(self):
        emit = self.rt.log.emit

        def interrupt(**kwargs):
            if kwargs["type"] == "artifacts.materialized":
                raise sqlite3.OperationalError("fixture: receipt write interrupted")
            return emit(**kwargs)

        with patch.object(self.rt.log, "emit", side_effect=interrupt), self.assertLogs("cater.runtime", level="ERROR") as logs:
            self.rt.collaborate(self.task)
        current = self.rt.work.view(self.task)["items"][0]
        self.assertEqual(current["status"], "accepted")
        submission = current["submission"]
        self.assertIn(str(submission["seq"]), logs.output[0])
        self.assertIn("receipt write interrupted", logs.output[0])
        self.assertEqual(self.events(), [])
        self.assertFalse(current["in_flight"])
        self.assertFalse(any(e.type == "work.failed" for e in self.rt.log.read(task_id=self.task)))
        self.rt.collaborate(self.task)
        self.assertTrue(self.rt.work.view(self.task)["stage_accepted"])
        self.assertEqual(self.rt.work.view(self.task)["items"][0]["submission"], submission)
        self.assertEqual(len([c for c in self.driver.calls if c.get("phase") != "review"]), 1)
        self.assertEqual(len(self.events()), 1)

    def test_unexpected_receipt_disappearance_does_not_return_none_or_write_a_stale_observation(self):
        self.submit()
        self.rt.persist_artifacts(self.task)
        iterate = self.rt.log.iter_events
        reads = 0

        def disappearing(**kwargs):
            nonlocal reads
            if "artifacts.materialized" in kwargs.get("event_types", ()):
                reads += 1
                if reads > 1:
                    return iter(())
            return iterate(**kwargs)

        with patch.object(self.rt.log, "iter_events", side_effect=disappearing):
            self.assertEqual(self.rt.persist_artifacts(self.task), [])
        self.assertEqual(len(self.events()), 1)

    def test_receipt_is_not_returned_when_transaction_commit_fails(self):
        from contextlib import contextmanager

        self.submit()
        atomic = self.rt.log.atomic

        @contextmanager
        def failed_commit():
            nested = self.rt.log.db.in_transaction
            with atomic():
                yield
                if not nested:
                    raise sqlite3.OperationalError("fixture: commit failed")

        with patch.object(self.rt.log, "atomic", failed_commit), self.assertLogs("cater.runtime", level="ERROR"):
            self.assertEqual(self.rt.persist_artifacts(self.task), [])
        self.assertEqual(self.events(), [])
        self.assertEqual(self.rt.work.view(self.task)["items"][0]["status"], "submitted")
        self.rt.persist_artifacts(self.task)
        self.assertEqual(len(self.events()), 1)

    def test_work_submit_errors_are_not_swallowed_as_persistence_failures(self):
        with patch.object(self.rt.work, "submit", side_effect=RuntimeError("fixture: work submit failed")), \
                patch.object(artifact_store, "materialize") as materialize, self.assertRaisesRegex(RuntimeError, "work submit failed"):
            self.rt.collaborate(self.task)
        materialize.assert_not_called()
        self.assertEqual(self.rt.work.view(self.task)["items"][0]["status"], "uncertain")
        self.assertEqual(self.events(), [])


if __name__ == "__main__":
    unittest.main()
