from __future__ import annotations

import copy
from dataclasses import asdict, replace
import hashlib
import unittest

from cater.events import Actor, Event
from cater.fold import fold
from cater.work import digest


def event(seq, kind, payload, actor=None, task="task"):
    return Event(seq=seq, id=str(seq), ts="2026-10-15T00:00:00Z", task_id=task,
                 actor=actor or Actor.agent("dev", "worker"), type=kind, category="outcome",
                 payload=payload, prev_hash="", hash="")


class MaterializationFoldTests(unittest.TestCase):
    def setUp(self):
        self.events = [event(1, "task.created", {"goal": "Save source"}, Actor.human())]
        for seq in (2, 3):
            content = f"# source {seq}\nassert True\n"
            self.events.append(event(seq, "deliverable.published", {
                "title": f"Source {seq}", "summary": "Stored body", "content": content,
                "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
                "work_item_id": "implementation", "attempt_id": "attempt-one",
                "filename": f"source-{seq}.py", "format": "code",
            }))
        payload = {"plan_seq": 1, "work_item_id": "implementation", "attempt_id": "attempt-one",
                   "artifact_seqs": [2, 3], "hashes": {str(e.seq): e.payload["content_sha256"] for e in self.events[1:]},
                   "inputs": {}, "scope": {"direction_seq": 1}}
        payload["digest"] = digest(payload)
        self.submission = event(4, "work.submitted", payload)
        self.events.append(self.submission)
        self.receipt = {"submission_seq": 4, "submission_digest": payload["digest"], "directory": "C:/artifacts/task/version",
                        "files": [{"seq": seq, "content_sha256": payload["hashes"][str(seq)],
                                   "path": f"C:/artifacts/task/version/files/source-{seq}.py"} for seq in (2, 3)]}

    def success(self, seq=5, payload=None, actor=None, task="task"):
        return event(seq, "artifacts.materialized", payload if payload is not None else self.receipt,
                     actor or Actor.system("artifact_store"), task)

    def failure(self, seq=6, **updates):
        return event(seq, "artifacts.materialization_failed", {
            "submission_seq": 4, "submission_digest": self.submission.payload["digest"], "reason": "Disk unavailable", **updates,
        }, Actor.system("artifact_store"))

    def assert_unsaved(self, events):
        for artifact in fold(events)["task"].deliverables:
            self.assertNotIn("local_path", artifact)
            self.assertNotIn("local_save_error", artifact)

    def test_valid_receipt_projects_all_files_without_mutating_events_or_acceptance(self):
        events = [*self.events, event(5, "human.decided", {"resolution": "done"}, Actor.human()), self.success(6)]
        original = copy.deepcopy(events)
        task = fold(events)["task"]
        self.assertEqual(task.status, "done")
        self.assertEqual([a["local_path"] for a in task.deliverables], [f["path"] for f in self.receipt["files"]])
        self.assertEqual(events, original)
        self.assertEqual(task.deliverables[0]["content"], self.events[1].payload["content"])
        self.assertNotIn("accepted", task.deliverables[0])

    def test_model_save_fields_and_sequence_override_are_stripped(self):
        self.events[1].payload.update(local_path="C:/forged.py", local_save_error="forged failure", seq=999, actor="other")
        self.assert_unsaved(self.events)
        artifact = fold([*self.events, self.success()])["task"].deliverables[0]
        self.assertEqual(artifact["seq"], 2)
        self.assertEqual(artifact["actor"], "dev")
        self.assertEqual(artifact["local_path"], self.receipt["files"][0]["path"])

    def test_failure_clears_prior_success_preserves_body_and_later_success_clears_error(self):
        events = [*self.events, self.success(), self.failure()]
        for artifact in fold(events)["task"].deliverables:
            self.assertNotIn("local_path", artifact)
            self.assertEqual(artifact["local_save_error"], "Disk unavailable")
            self.assertTrue(artifact["content"])
        for artifact in fold([*events, self.success(7)])["task"].deliverables:
            self.assertIn("local_path", artifact)
            self.assertNotIn("local_save_error", artifact)

    def test_receipts_require_exact_system_actor_and_current_task_submission_binding(self):
        receipts = [
            self.success(actor=Actor.agent("artifact_store", "artifact_store")),
            self.success(actor=Actor.system("other")),
            self.success(actor=Actor("artifact_store", "other", "system")),
            self.success(task="other-task"), self.success(3),
            self.success(payload={**self.receipt, "submission_seq": True}),
            self.success(payload={**self.receipt, "submission_seq": 99}),
            self.success(payload={**self.receipt, "submission_digest": "0" * 64}),
        ]
        for receipt in receipts:
            with self.subTest(receipt=receipt): self.assert_unsaved([*self.events, receipt])
        self.assert_unsaved([*self.events[:3], self.success()])

    def test_invalid_failures_cannot_clear_a_saved_path(self):
        failures = [self.failure(submission_seq=999), self.failure(submission_digest="wrong"), self.failure(reason=""),
                    replace(self.failure(), actor=Actor.agent("artifact_store", "artifact_store")),
                    replace(self.failure(), task_id="other-task")]
        for failure in failures:
            with self.subTest(failure=failure):
                artifacts = fold([*self.events, self.success(), failure])["task"].deliverables
                self.assertTrue(all("local_path" in a and "local_save_error" not in a for a in artifacts))

    def test_malformed_partial_duplicate_and_outside_paths_are_ignored_atomically(self):
        mutations = [
            lambda p: p.update(files=[]), lambda p: p["files"].pop(),
            lambda p: p["files"].append(p["files"][0]),
            lambda p: p["files"][1].update(seq=2), lambda p: p["files"][1].update(seq=99),
            lambda p: p["files"][1].update(content_sha256="wrong"),
            lambda p: p["files"][1].update(path=p["files"][0]["path"]),
            lambda p: p["files"][1].update(path="C:/outside.py"),
            lambda p: p["files"][1].update(path="C:/artifacts/task/version/../outside.py"),
            lambda p: p["files"][1].update(path="relative.py"),
            lambda p: p["files"][1].update(path="file:///C:/source.py"),
            lambda p: p["files"][1].update(path="/other/source.py"),
            lambda p: p["files"][1].update(path=p["directory"]),
            lambda p: p.update(directory="relative"), lambda p: p.update(files=[None, None]),
            lambda p: p.update(files=None), lambda p: p["files"][1].update(seq=True),
        ]
        for mutate in mutations:
            payload = copy.deepcopy(self.receipt)
            mutate(payload)
            with self.subTest(payload=payload): self.assert_unsaved([*self.events, self.success(payload=payload)])

    def test_posix_absolute_paths_work_without_accessing_the_filesystem(self):
        payload = copy.deepcopy(self.receipt)
        payload["directory"] = "/not-mounted/artifacts/version"
        for item in payload["files"]: item["path"] = f"{payload['directory']}/files/{item['seq']}.py"
        self.assertEqual(fold([*self.events, self.success(payload=payload)])["task"].deliverables[0]["local_path"],
                         payload["files"][0]["path"])

    def test_submission_digest_membership_body_hash_and_owner_must_match(self):
        for field, value in (("digest", "0" * 64), ("artifact_seqs", [2, 999]), ("artifact_seqs", [2, 2]),
                             ("attempt_id", "other"), ("work_item_id", "other"), ("hashes", {"2": "wrong", "3": "wrong"})):
            payload = copy.deepcopy(self.submission.payload)
            payload[field] = value
            if field != "digest": payload["digest"] = digest({k: v for k, v in payload.items() if k != "digest"})
            submission = replace(self.submission, payload=payload)
            receipt = {**self.receipt, "submission_digest": payload["digest"]}
            with self.subTest(field=field, value=value):
                self.assert_unsaved([*self.events[:3], submission, self.success(payload=receipt)])
        for actor in (Actor.human(), Actor.system("artifact_store"), Actor.agent("other", "worker")):
            self.assert_unsaved([*self.events[:3], replace(self.submission, actor=actor), self.success()])
        self.events[1].payload["content"] = "tampered body"
        self.assert_unsaved([*self.events, self.success(), self.failure()])

    def test_cross_task_artifacts_cannot_satisfy_submission_membership(self):
        events = [self.events[0], replace(self.events[1], task_id="other-task"), *self.events[2:], self.success()]
        self.assert_unsaved(events)
        self.assertNotIn("local_path", fold(events)["other-task"].deliverables[0])

    def test_trusted_reuse_accepts_original_artifacts_but_not_unbound_reuse(self):
        payload = {**self.submission.payload, "attempt_id": "attempt-two", "reused_from": 4}
        payload["digest"] = digest({k: v for k, v in payload.items() if k != "digest"})
        reused = event(6, "work.submitted", payload, Actor.system("work_scheduler"))
        receipt = {**self.receipt, "submission_seq": 6, "submission_digest": payload["digest"]}
        events = [*self.events, reused, self.success(7, receipt)]
        self.assertTrue(all("local_path" in a for a in fold(events)["task"].deliverables))
        self.assert_unsaved([*self.events[:3], reused, self.success(7, receipt)])
        self.assert_unsaved([*self.events, replace(reused, actor=Actor.agent("dev", "worker")), self.success(7, receipt)])

    def reused_receipts(self):
        payload = {**self.submission.payload, "attempt_id": "attempt-two", "reused_from": 4}
        payload["digest"] = digest({k: v for k, v in payload.items() if k != "digest"})
        reused = event(6, "work.submitted", payload, Actor.system("work_scheduler"))
        receipt = {**self.receipt, "submission_seq": 6, "submission_digest": payload["digest"],
                   "directory": "C:/artifacts/task/new-version",
                   "files": [{**item, "path": item["path"].replace("/version/", "/new-version/")}
                             for item in self.receipt["files"]]}
        failure = self.failure(7, submission_seq=6, submission_digest=payload["digest"], reason="new version failed")
        return reused, self.success(7, receipt), failure

    def test_older_submission_receipts_cannot_override_newer_success_or_failure(self):
        reused, success, failure = self.reused_receipts()
        for current in (success, failure):
            for late in (self.success(8), self.failure(8, reason="old version failed")):
                with self.subTest(current=current.type, late=late.type):
                    events = [*self.events, self.success(5), reused, current]
                    before = fold(events)["task"]
                    self.assertEqual(asdict(fold([*events, late])["task"]), asdict(before))
                    for artifact in before.deliverables:
                        if current.type == "artifacts.materialized":
                            self.assertIn("/new-version/", artifact["local_path"])
                        else:
                            self.assertEqual(artifact["local_save_error"], "new version failed")
                            self.assertNotIn("local_path", artifact)

    def test_same_submission_still_follows_receipt_order_without_public_version_metadata(self):
        reused, success, failure = self.reused_receipts()
        events = [*self.events, self.success(5), reused, success, self.failure(8),
                  replace(failure, seq=9), self.success(10)]
        failed = fold(events)["task"]
        self.assertTrue(all(a.get("local_save_error") == "new version failed" and "local_path" not in a
                            for a in failed.deliverables))
        events.append(replace(success, seq=11))
        saved = fold(events)["task"]
        for artifact, original in zip(saved.deliverables, self.events[1:3]):
            expected = {**original.payload, "seq": original.seq, "actor": original.actor.id,
                        "local_path": success.payload["files"][artifact["seq"] - 2]["path"]}
            self.assertEqual(artifact, expected)
        self.assertEqual(set(asdict(saved)), set(asdict(fold(self.events)["task"])))
        self.assertEqual(fold(self.events)["task"].deliverables[0].get("local_path"), None)

    def test_invalid_newer_receipt_does_not_advance_version_guard(self):
        reused, success, failure = self.reused_receipts()
        invalid = replace(success, payload={**success.payload, "files": success.payload["files"][:1]})
        for receipt in (invalid, replace(failure, payload={**failure.payload, "reason": ""}),
                        replace(success, actor=Actor.agent("artifact_store", "artifact_store"))):
            with self.subTest(receipt=receipt):
                events = [*self.events, self.success(5), reused, receipt, self.failure(8)]
                self.assertTrue(all(a.get("local_save_error") == "Disk unavailable" and "local_path" not in a
                                    for a in fold(events)["task"].deliverables))


if __name__ == "__main__":
    unittest.main()
