"""A completed new objective does not silently inherit all previous files."""
import io
import unittest
import zipfile

from cater.artifacts import normalize_deliverables
from cater.events import Actor
from cater.team import HOST
import test_simple_team


class TeamDeliverySelectionTests(unittest.TestCase):
    setUp = test_simple_team.SimpleTeamTests.setUp
    events = test_simple_team.SimpleTeamTests.events
    run_ok = test_simple_team.SimpleTeamTests.run_ok

    def publish(self, filename="old.txt", content="Old goal file", task=None):
        payload, = normalize_deliverables([{"filename": filename, "content": content, "format": "text", "title": filename, "summary": "Fixture bytes"}])
        return self.rt.log.emit(type="deliverable.published", task_id=task or self.task,
            actor=Actor.agent(self.manager, self.rt.organization.leader.role), payload=payload)

    def finish(self, arguments=None, *, calls=()):
        self.driver.steps[self.manager] = [test_simple_team.turn("", *calls,
            ("finish_work", {"summary": "按实际文件清单交付", "verification": "仅离线契约测试", **(arguments or {})})), test_simple_team.turn("已按记录说明范围。")]
        self.rt.team.receive(self.task, "处理当前消息", request_id=f"selection-input-{len(self.driver.calls)}")
        self.run_ok()
        return next(e.payload["result"] for e in reversed(self.events("team.tool_finished")) if e.payload["name"] == "finish_work")

    def test_new_objective_without_publication_has_empty_delivery_not_old_goal_files(self):
        old = self.publish()
        result = self.finish(calls=[("set_objective", {"goal": "纯文字解释，不交付旧文件"})])
        self.assertEqual(result["status"], "ok")
        complete = self.events("team.objective_completed")[-1]
        self.assertEqual(complete.payload["artifacts"], [])
        self.assertEqual(complete.payload["artifact_selection"], "current_objective")
        self.assertGreater(complete.payload["objective_seq"], old.seq)
        with self.assertRaises(ValueError):
            self.rt.team.archive(self.task, expected_delivery_seq=complete.seq)

    def test_default_uses_only_latest_named_files_since_current_objective(self):
        self.publish()
        self.rt.log.emit(type="team.objective_updated", task_id=self.task, actor=HOST, payload={"goal": "New work"})
        self.publish("new.txt", "First candidate")
        final = self.publish("new.txt", "Current candidate")
        self.assertEqual(self.finish()["status"], "ok")
        complete = self.events("team.objective_completed")[-1]
        self.assertEqual([a["seq"] for a in complete.payload["artifacts"]], [final.seq])
        with zipfile.ZipFile(io.BytesIO(self.rt.team.archive(self.task, expected_delivery_seq=complete.seq))) as archive:
            self.assertEqual(archive.read("new.txt"), b"Current candidate")
            self.assertNotIn("old.txt", archive.namelist())

    def test_explicit_reuse_keeps_unchanged_dependency_and_records_current_goal_provenance(self):
        old = self.publish("shared.js", "Unchanged dependency")
        boundary = self.rt.log.emit(type="team.objective_updated", task_id=self.task, actor=HOST, payload={"goal": "Reuse a library"})
        new = self.publish("index.html", "New entrypoint")
        self.assertEqual(self.finish({"artifact_seqs": [old.seq, new.seq]})["status"], "ok")
        complete = self.events("team.objective_completed")[-1]
        self.assertEqual(complete.payload["artifact_selection"], "explicit")
        self.assertEqual(complete.payload["objective_seq"], boundary.seq)
        with zipfile.ZipFile(io.BytesIO(self.rt.team.archive(self.task, expected_delivery_seq=complete.seq))) as archive:
            self.assertEqual(archive.read("shared.js"), b"Unchanged dependency")
            self.assertEqual(archive.read("index.html"), b"New entrypoint")

    def test_explicit_empty_files_excludes_scratch_publication(self):
        self.publish()
        self.assertEqual(self.finish({"artifact_seqs": []})["status"], "ok")
        self.assertEqual(self.events("team.objective_completed")[-1].payload["artifacts"], [])

    def test_invalid_or_cross_group_file_selection_can_be_corrected_without_completing(self):
        other = self.rt.create_task("Other group")
        foreign = self.publish(task=other)
        for seqs in ([foreign.seq], [True], [0], [-1], [999999], "bad", [1] * 129):
            self.task = self.rt.create_task("Independent malformed selection")
            self.rt.team.enable(self.task)
            result = self.finish({"artifact_seqs": seqs})
            self.assertEqual(result["code"], "invalid_delivery_files", seqs)
            self.assertEqual(self.events("team.objective_completed"), [])

    def test_duplicate_file_versions_and_duplicate_sequences_are_rejected(self):
        for repeated_seq in (False, True):
            self.task = self.rt.create_task("Independent duplicate selection")
            self.rt.team.enable(self.task)
            one, two = self.publish(), self.publish(content="Second version")
            result = self.finish({"artifact_seqs": [one.seq, one.seq if repeated_seq else two.seq]})
            self.assertEqual(result["code"], "invalid_delivery_files")
            self.assertEqual(self.events("team.objective_completed"), [])

    def test_old_cross_goal_snapshot_requires_explicit_scope_for_versioned_download(self):
        old = self.publish()
        boundary = self.rt.log.emit(type="team.objective_updated", task_id=self.task, actor=HOST, payload={"goal": "New goal"})
        refs = [{"seq": old.seq, **{k: old.payload[k] for k in ("filename", "content_sha256")}}]
        completed = self.rt.log.emit(type="team.objective_completed", task_id=self.task, actor=HOST,
            payload={"summary": "Old-format snapshot", "verification": "Unverified reuse", "artifacts": refs})
        with self.assertRaises(ValueError):
            self.rt.team.archive(self.task, expected_delivery_seq=completed.seq)
        # Historical unparameterized access remains compatible, not a verified-current UI entry.
        with zipfile.ZipFile(io.BytesIO(self.rt.team.archive(self.task))) as archive:
            self.assertEqual(archive.read("old.txt"), b"Old goal file")
        explicit = self.rt.log.emit(type="team.objective_completed", task_id=self.task, actor=HOST,
            payload={**completed.payload, "artifact_selection": "explicit", "objective_seq": boundary.seq})
        with zipfile.ZipFile(io.BytesIO(self.rt.team.archive(self.task, expected_delivery_seq=explicit.seq))) as archive:
            self.assertEqual(archive.read("old.txt"), b"Old goal file")


if __name__ == "__main__":
    unittest.main()
