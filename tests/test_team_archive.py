"""Native archive delivery uses exact published bytes, not a later workspace."""

import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
import zipfile

from cater.events import Actor
from cater.runtime import Runtime
from cater.team_archive import artifact_bytes
import test_team_review_cycle as reviews


class TeamArchiveTests(unittest.TestCase):
    def setUp(self):
        self.f = reviews.TeamReviewCycleTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.project = self.f.bind_workspace()

    def snapshot(self, paths, **changes):
        result, = self.f.exchange(self.f.worker, ("publish_workspace_archive", {
            "paths": paths, "filename": "project.zip", "summary": "Actual project snapshot", **changes}))
        return result

    def read(self, seq, **changes):
        result, = self.f.exchange(self.f.reviewer, ("read_artifact", {"artifact_seq": seq, **changes}))
        return result

    def test_multidirectory_binary_empty_and_large_files_survive_delivery_and_reopen(self):
        files = {"src/main.py": b"def answer():\r\n    return 42\r\n",
                 "tests/main.py": b"from src.main import answer\nassert answer() == 42\nprint('downloaded project passed')\n",
                 "src/__init__.py": b"", "assets/image.bin": bytes(range(256)) * 400,
                 "docs/notes.md": ("\u4e2d\u6587\n" * 22000).encode()}
        for name, body in files.items():
            path = self.project / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
        published = self.snapshot(list(files))
        self.assertEqual(published["status"], "ok", published)
        self.assertEqual(published["file_count"], 5)
        artifact, = self.f.rt.task(self.f.task).deliverables
        self.assertNotIn("content", artifact)
        actual = artifact_bytes(self.f.rt.log, artifact)
        self.assertEqual(Path(artifact["local_path"]).read_bytes(), actual)
        with zipfile.ZipFile(io.BytesIO(actual)) as archive:
            self.assertEqual({name: archive.read(name) for name in archive.namelist()}, files)
        self.assertEqual(self.read(artifact["seq"])["archive"], artifact["archive"])
        paged = self.read(artifact["seq"], path="docs/notes.md", offset=2, length=9)
        self.assertEqual(paged["content"], files["docs/notes.md"].decode()[2:11])
        self.assertTrue(paged["read_range"]["has_more"])
        self.assertEqual(self.read(artifact["seq"], path="src/__init__.py")["content"], "")
        binary = self.read(artifact["seq"], path="assets/image.bin")
        self.assertIsNone(binary["content"])
        self.assertEqual(binary["sha256"], hashlib.sha256(files["assets/image.bin"]).hexdigest())
        self.assertEqual(binary["content_kind"], "binary")
        self.assertEqual(self.f.request([artifact["seq"]])["status"], "ok")
        row, = self.f.rt.team_reviews.view(self.f.task)
        self.assertEqual(self.f.record(row["request_seq"])["status"], "ok")
        self.f.acknowledge_latest_review()
        self.assertEqual(self.f.finish()["status"], "ok")
        (self.project / "src/main.py").write_bytes(b"later unapproved change")
        output = self.f.rt.team.archive(self.f.task, expected_delivery_seq=self.f.rt.team.view(self.f.task)["revision"])
        with zipfile.ZipFile(io.BytesIO(output)) as delivery:
            self.assertEqual(delivery.read("project.zip"), actual)
            manifest = json.loads(delivery.read("__cater__/manifest.json"))
            self.assertEqual(manifest["files"][0]["sha256"], hashlib.sha256(actual).hexdigest())
            consumer = self.f.root / "downloaded-project"
            with zipfile.ZipFile(io.BytesIO(delivery.read("project.zip"))) as project:
                for name in files:
                    target = consumer / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(project.read(name))
        checked = subprocess.run([sys.executable, "-I", "-c",
            "import runpy,sys; sys.path.insert(0,'.'); runpy.run_path('tests/main.py',run_name='__main__')"],
            cwd=consumer, capture_output=True, timeout=5)
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertEqual(checked.stdout.strip(), b"downloaded project passed")
        with Runtime(self.f.root / "test.db", roles_path=self.f.roles,
                     drivers={"team_test": self.f.driver}) as reopened:
            self.assertEqual(artifact_bytes(reopened.log, reopened.task(self.f.task).deliverables[0]), actual)
            with zipfile.ZipFile(io.BytesIO(reopened.team.archive(self.f.task))) as delivery:
                self.assertEqual(delivery.read("project.zip"), actual)
            self.assertTrue(reopened.verify_chain().ok)
        serialized = json.dumps([e.payload for e in self.f.rt.log.read(task_id=self.f.task)])
        self.assertNotIn("downloaded project passed", serialized)
        self.assertEqual(self.f.rt.team._pending_operations(self.f.task), [])

    def test_two_reworks_cannot_deliver_the_rejected_or_promised_version(self):
        path = self.project / "answer.py"
        path.write_bytes(b"def answer():\n    return 1\n")
        requested = None
        versions = []
        for number in (1, 2, 3):
            path.write_bytes(f"def answer():\n    return {number}\n".encode())
            published = self.snapshot(["answer.py"])
            self.assertEqual(published["status"], "ok")
            versions.append(published["artifact_seq"])
            request = self.f.request([versions[-1]], review_seq=requested)
            self.assertEqual(request["status"], "ok", request)
            requested = request["review_seq"]
            content = self.read(versions[-1], path="answer.py")["content"]
            namespace = {}
            exec(compile(content, "answer.py", "exec"), namespace)
            observed = namespace["answer"]()
            self.assertEqual(observed, number)
            verdict = "approved" if observed == 3 else "changes_requested"
            self.assertEqual(self.f.record(requested, verdict, feedback=f"Observed answer={observed}")["status"], "ok")
            if number < 3:
                self.assertEqual(self.f.finish()["code"], "review_pending")
        self.f.acknowledge_latest_review()
        rejected, = self.f.exchange(self.f.manager, ("finish_work", {
            "summary": "Must not ship the rejected first version", "verification": "Controlled review fixture",
            "artifact_seqs": [versions[0]]}))
        self.assertEqual(rejected["status"], "error")
        self.assertEqual(rejected["code"], "delivery_review_mismatch")
        self.assertEqual(self.f.events("team.objective_completed"), [])
        self.assertEqual(self.f.finish()["status"], "ok")
        completed, = self.f.events("team.objective_completed")
        self.assertEqual([a["seq"] for a in completed.payload["artifacts"]], [versions[-1]])
        with zipfile.ZipFile(io.BytesIO(self.f.rt.team.archive(self.f.task))) as outer:
            with zipfile.ZipFile(io.BytesIO(outer.read("project.zip"))) as inner:
                self.assertEqual(inner.read("answer.py"), path.read_bytes())
        self.assertTrue(self.f.rt.verify_chain().ok)

    def test_capture_rejections_do_not_create_unknown_publications(self):
        for paths in ([], ["missing"], ["../outside"], [".env"], ["ok", "ok"], "ok", [True]):
            with self.subTest(paths=paths):
                result = self.snapshot(paths)
                self.assertEqual(result["status"], "error")
                self.assertEqual(result.get("outcome"), "not_started")
        self.assertEqual(self.f.rt.task(self.f.task).deliverables, [])
        self.assertEqual(self.f.rt.team._pending_operations(self.f.task), [])

    def test_blob_and_event_roll_back_together_on_failed_publication(self):
        (self.project / "ok.txt").write_bytes(b"original")
        original = self.f.rt.log.emit

        def fail(**kwargs):
            if kwargs["type"] == "deliverable.published":
                raise ValueError("injected after blob insert")
            return original(**kwargs)

        with patch.object(self.f.rt.log, "emit", side_effect=fail):
            result = self.snapshot(["ok.txt"])
        self.assertEqual(result["status"], "error")
        self.assertEqual(self.f.rt.task(self.f.task).deliverables, [])
        table = self.f.rt.log.db.execute("SELECT name FROM sqlite_master WHERE name='artifact_blobs'").fetchone()
        if table:
            self.assertEqual(self.f.rt.log.db.execute("SELECT count(*) FROM artifact_blobs").fetchone()[0], 0)
        self.assertTrue(self.f.rt.verify_chain().ok)

    def test_cannot_read_archive_from_another_task_or_use_member_path_on_plain_text(self):
        (self.project / "private.txt").write_bytes(b"PRIVATE_SNAPSHOT_BODY")
        published = self.snapshot(["private.txt"])
        other = self.f.rt.create_task("another independent task")
        self.f.rt.team.enable(other)
        result, = self.f.exchange(self.f.reviewer, ("read_artifact", {
            "artifact_seq": published["artifact_seq"], "path": "private.txt"}), task_id=other)
        self.assertEqual(result["status"], "error")
        self.assertNotIn("PRIVATE_SNAPSHOT_BODY", json.dumps(result))
        plain = self.f.publish()
        self.assertEqual(self.read(plain, path="result.txt")["status"], "error")

    def test_observed_goal_change_during_capture_does_not_publish_old_snapshot(self):
        from cater import team_archive

        (self.project / "ok.txt").write_bytes(b"original")
        original = team_archive.create_snapshot

        def change(*args):
            captured = original(*args)
            self.f.rt.log.emit(type="team.objective_updated", actor=Actor.system("team"),
                task_id=self.f.task, payload={"goal": "Changed while capture was reading"})
            return captured

        with patch.object(team_archive, "create_snapshot", side_effect=change):
            result, = self.f.exchange(self.f.manager, ("publish_workspace_archive", {
                "paths": ["ok.txt"], "filename": "project.zip", "summary": "Manager snapshot"}))
        self.assertEqual(result["status"], "error")
        self.assertEqual(self.f.rt.task(self.f.task).deliverables, [])

    def test_permission_revoked_after_capture_does_not_publish_or_create_unknown(self):
        from cater import team_archive

        (self.project / "ok.txt").write_bytes(b"original")
        original = team_archive.create_snapshot
        def revoke(*args):
            captured = original(*args)
            binding = self.f.rt.workspaces.view(self.f.task)
            self.f.rt.workspaces.bind(self.f.task, None, expected_revision=binding["revision"])
            return captured
        with patch.object(team_archive, "create_snapshot", side_effect=revoke):
            result = self.snapshot(["ok.txt"])
        self.assertEqual(result["status"], "error")
        self.assertEqual(self.f.rt.task(self.f.task).deliverables, [])
        self.assertEqual(self.f.rt.team._pending_operations(self.f.task), [])

    def test_saved_blob_without_materialization_receipt_is_not_blindly_republished(self):
        (self.project / "ok.txt").write_bytes(b"original")
        with patch("cater.team_archive.materialize_team_artifact", side_effect=OSError("injected")):
            result = self.snapshot(["ok.txt"])
        self.assertEqual(result["outcome"], "effects_unknown")
        artifact, = self.f.rt.task(self.f.task).deliverables
        self.assertTrue(artifact_bytes(self.f.rt.log, artifact))
        self.assertEqual(self.snapshot(["ok.txt"])["status"], "error")
        self.assertEqual(len(self.f.rt.task(self.f.task).deliverables), 1)
        pending, = self.f.rt.team._pending_operations(self.f.task)
        self.assertEqual(self.f.finish()["code"], "operation_pending")
        self.f.rt.team.recovery.reconcile_publication(self.f.task, pending["started_seq"],
            reason="Fixture operator inspected exact blob and confirmed invocation stopped", invocation_stopped=True)
        self.assertEqual(self.snapshot(["ok.txt"])["status"], "ok")

    def test_plain_text_explicit_selection_also_cannot_revert_an_approved_version(self):
        old = self.f.publish("rejected body")
        request = self.f.request([old])
        self.assertEqual(self.f.record(request["review_seq"], "changes_requested")["status"], "ok")
        latest = self.f.publish("approved body")
        request = self.f.request([latest], review_seq=request["review_seq"])
        self.assertEqual(self.f.record(request["review_seq"])["status"], "ok")
        self.f.acknowledge_latest_review()
        result, = self.f.exchange(self.f.manager, ("finish_work", {
            "artifact_seqs": [old], "summary": "Old must not replace approved", "verification": "Fixture only"}))
        self.assertEqual(result["code"], "delivery_review_mismatch")
        self.assertEqual(self.f.finish()["status"], "ok")
        with zipfile.ZipFile(io.BytesIO(self.f.rt.team.archive(self.f.task))) as archive:
            self.assertEqual(archive.read("result.txt"), b"approved body")

    def test_same_bytes_published_and_approved_by_two_authors_use_latest_approval(self):
        old = self.f.publish("identical body")
        first = self.f.request([old])
        self.assertEqual(self.f.record(first["review_seq"])["status"], "ok")
        self.f.acknowledge_latest_review()
        latest = self.f.publish("identical body", author=self.f.observer)
        second = self.f.request([latest], requester=self.f.observer)
        self.assertEqual(self.f.record(second["review_seq"])["status"], "ok")
        report = next(e for e in reversed(self.f.rt.team._messages(self.f.task))
                      if e.payload.get("review_seq") == second["review_seq"] and e.payload.get("kind") == "report")
        self.f.exchange(self.f.observer, source=report)
        self.assertEqual([r["status"] for r in self.f.rt.team_reviews.view(self.f.task)], ["approved", "approved"])
        self.assertEqual(self.f.finish()["status"], "ok")
        completed, = self.f.events("team.objective_completed")
        self.assertEqual([a["seq"] for a in completed.payload["artifacts"]], [latest])


if __name__ == "__main__":
    unittest.main()
