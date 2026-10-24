"""Owner-led resubmissions through native tools and disposable Runtime data."""

import hashlib
import io
import json
import unittest
import zipfile

from cater.events import Actor
import test_team_review_cycle as cycle


class ReviewResubmissionOwnerTests(unittest.TestCase):
    def setUp(self):
        self.f = cycle.TeamReviewCycleTests()
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()

    def ok(self, result):
        self.assertEqual(result.get("status"), "ok", result)
        return result

    def refs(self, *seqs):
        artifacts = {a["seq"]: a for a in self.f.rt.task(self.f.task).deliverables}
        return [{key: artifacts[seq][key] for key in ("seq", "filename", "content_sha256")}
                for seq in seqs]

    def event(self, seq):
        return next(e for e in self.f.rt.log.read() if e.seq == seq)

    def effects(self):
        return [e for e in self.f.rt.log.read() if e.type in {
            "deliverable.published", "team.artifact_saved", "team.review_requested",
            "team.review_recorded", "team.objective_completed"}
            or e.type == "team.message_received" and e.actor.kind == "agent"]

    def reject(self, action, *, code=None, expected=None):
        before = self.effects()
        result = action()
        self.assertEqual(result.get("status"), "error", result)
        if code:
            self.assertEqual(result.get("code"), code, result)
            self.assertIs(result.get("complete"), True)
            self.assertEqual(result.get("outcome"), "not_started")
        if expected:
            self.assertEqual(result.get("expected_" + expected[0]), expected[1], result)
        self.assertEqual(self.effects(), before)
        self.assertTrue(self.f.rt.verify_chain().ok)
        return result

    def start(self, *seqs):
        f = self.f
        return self.ok(f.request(list(seqs), requester=f.manager))

    def resubmit(self, previous, *seqs, **kwargs):
        f = self.f
        return f.request(list(seqs), requester=f.manager,
                         review_seq=previous["review_seq"], **kwargs)

    def decide(self, request, verdict="approved"):
        f = self.f
        result = self.ok(f.record(request["review_seq"], verdict,
                                  source=self.event(request["message_seq"])))
        report = self.event(result["message_seq"])
        self.assertEqual(report.payload["recipient"], f.manager)
        f.exchange(f.manager, source=report)
        return result

    def assert_delivery(self, *seqs):
        f = self.f
        completion, = f.events("team.objective_completed")
        self.assertCountEqual(completion.payload["artifacts"], self.refs(*seqs))
        artifacts = {a["seq"]: a for a in f.rt.task(f.task).deliverables}
        with zipfile.ZipFile(io.BytesIO(f.rt.team.archive(
                f.task, expected_delivery_seq=completion.seq))) as archive:
            manifest = json.loads(archive.read("__cater__/manifest.json"))
            self.assertCountEqual(manifest["files"], [
                {"seq": a["seq"], "filename": a["filename"], "sha256": a["content_sha256"]}
                for a in self.refs(*seqs)])
            self.assertEqual(set(archive.namelist()),
                             {artifacts[s]["filename"] for s in seqs} | {"__cater__/manifest.json"})
            for seq in seqs:
                artifact = artifacts[seq]
                body = archive.read(artifact["filename"])
                self.assertEqual(body, artifact["content"].encode("utf-8"))
                self.assertEqual(hashlib.sha256(body).hexdigest(), artifact["content_sha256"])
        self.assertTrue(f.rt.verify_chain().ok)

    def test_owner_selects_partner_versions_without_republishing_and_delivers_exact_final(self):
        f = self.f
        old = f.publish("manager draft\n", author=f.manager, filename="draft.txt")
        first = self.start(old)
        self.decide(first, "changes_requested")
        historical = f.rt.log.read()
        middle = f.publish("developer correction\n", author=f.worker, filename="candidate.txt")
        publications = f.events("deliverable.published")
        second = self.ok(self.resubmit(first, middle))
        self.assertEqual(f.events("deliverable.published"), publications)
        self.assertEqual(f.rt.log.read()[:len(historical)], historical)
        self.assertEqual(f.rt.team_reviews.superseded_artifacts(f.task), set())
        self.assertEqual(f.finish().get("code"), "review_pending")
        self.decide(second, "changes_requested")
        final = f.publish("final correction\n", author=f.observer, filename="final.txt")
        publications = f.events("deliverable.published")
        third = self.ok(self.resubmit(second, final))
        self.assertEqual(third["root_seq"], first["root_seq"])
        self.assertEqual(f.events("deliverable.published"), publications)
        self.assertEqual([e.actor.id for e in publications], [f.manager, f.worker, f.observer])
        requests = f.events("team.review_requested")
        self.assertEqual(len(requests), 3)
        for request in requests:
            self.assertEqual(request.payload["author"], f.manager)
            self.assertEqual(request.payload["requested_by"], f.manager)
            self.assertEqual(request.payload["reviewer"], f.reviewer)
            self.assertEqual(request.payload["revision"], requests[0].payload["revision"])
        row, = f.rt.team_reviews.view(f.task)
        self.assertEqual((row["status"], row["artifacts"]), ("pending", self.refs(final)))
        assignment = self.event(third["message_seq"])
        self.assertEqual(assignment.actor.id, f.manager)
        self.assertEqual(assignment.payload["recipient"], f.reviewer)
        self.assertEqual(f.rt.team_reviews._request_message(f.task, row), assignment)
        self.assertEqual(row["previous_review_seq"], second["review_seq"])
        self.assertEqual(row["previous_artifacts"], self.refs(middle))
        history, = f.exchange(f.manager, ("read_checkpoint", {"review_seq": first["review_seq"]}))
        self.ok(history)
        self.assertEqual(history["review"]["historical_status"], "changes_requested")
        self.assertEqual(history["review"]["current_request_seq"], third["review_seq"])
        self.assertEqual(history["review"]["artifacts"], self.refs(old))
        for signer in (f.manager, f.worker, f.observer):
            self.reject(lambda: f.record(third["review_seq"], reviewer=signer),
                        code="reviewer_mismatch", expected=("reviewer", f.reviewer))
        self.decide(third)
        self.assertEqual(f.rt.team_reviews.superseded_artifacts(f.task), {old, middle})
        rejected, = f.exchange(f.manager, ("finish_work", {
            "summary": "Must not deliver a retired draft", "verification": "Offline fixture",
            "artifact_seqs": [old, final]}))
        self.assertEqual(rejected.get("code"), "delivery_review_mismatch", rejected)
        self.ok(f.finish())
        self.assert_delivery(final)
        self.assertEqual(f.events("deliverable.published"), publications)

    def test_complete_mixed_set_can_retain_owner_file_and_select_two_partner_publications(self):
        f = self.f
        retained = f.publish("stable instructions\n", author=f.manager, filename="readme.txt")
        old = f.publish("broken implementation\n", author=f.manager)
        first = self.start(retained, old)
        self.decide(first, "changes_requested")
        revised = f.publish("fixed implementation\n", author=f.worker)
        notes = f.publish("verified usage\n", author=f.observer, filename="usage.txt")
        publications = f.events("deliverable.published")
        # delegate.review shares the same request path and must retain the owner too.
        second, = f.exchange(f.manager, ("delegate", {
            "recipient": f.reviewer, "instruction": "Check the complete revised set.",
            "review": {"review_seq": first["review_seq"],
                       "artifact_seqs": [retained, revised, notes]}}))
        self.ok(second)
        self.assertEqual(second["root_seq"], first["root_seq"])
        self.assertEqual(f.events("deliverable.published"), publications)
        row, = f.rt.team_reviews.view(f.task)
        self.assertEqual(row["artifacts"], self.refs(retained, revised, notes))
        self.assertEqual(row["author"], f.manager)
        self.decide(second)
        self.assertEqual(f.rt.team_reviews.superseded_artifacts(f.task), {old})
        self.ok(f.finish())
        self.assert_delivery(retained, revised, notes)

    def test_contributors_cannot_continue_or_take_over_the_owner_chain(self):
        f = self.f
        old = f.publish(author=f.manager)
        first = self.start(old)
        self.decide(first, "changes_requested")
        revised = f.publish("partner correction\n", author=f.worker)
        for requester in (f.worker, f.observer, f.reviewer):
            with self.subTest(requester=requester):
                self.reject(lambda: f.request([revised], requester=requester,
                            review_seq=first["review_seq"]), code="review_author_mismatch",
                            expected=("author", f.manager))
        self.ok(self.resubmit(first, revised))

    def test_initial_proxy_and_multiple_publishers_still_reject(self):
        f = self.f
        manager_file = f.publish(author=f.manager, filename="manager.txt")
        partner_file = f.publish(author=f.worker, filename="partner.txt")
        publications = f.events("deliverable.published")
        first = self.start(partner_file)
        request = self.event(first["review_seq"])
        self.assertEqual(request.payload["owner"], f.manager)
        self.assertEqual(request.payload["author"], f.manager)
        self.assertEqual(request.payload["requested_by"], f.manager)
        self.assertEqual(request.payload["publishers"], [f.worker])
        self.assertEqual(f.events("deliverable.published"), publications)
        # Use a disjoint set so this checks another initial request, not takeover.
        another = f.publish(author=f.worker, filename="another.txt")
        publications = f.events("deliverable.published")
        second = self.ok(f.request([manager_file, another], requester=f.worker))
        request = self.event(second["review_seq"])
        self.assertEqual(request.payload["owner"], f.worker)
        self.assertEqual(request.payload["author"], f.worker)
        self.assertEqual(request.payload["requested_by"], f.worker)
        self.assertEqual(request.payload["publishers"], sorted([f.manager, f.worker]))
        self.assertEqual(f.events("deliverable.published"), publications)
        self.assertEqual(len(f.rt.team_reviews.view(f.task)), 2)

    def test_partner_republication_cannot_open_parallel_root_then_owner_can_continue(self):
        f = self.f
        old = f.publish(author=f.manager)
        first = self.start(old)
        self.decide(first, "changes_requested")
        revised = f.publish("partner correction\n", author=f.worker)
        result = self.reject(lambda: f.request([revised], requester=f.worker))
        self.assertTrue(result["message"].startswith(
            "\u8fd9\u4efd\u6210\u679c\u5df2\u6709\u590d\u6838\u8bb0\u5f55"), result)
        row, = f.rt.team_reviews.view(f.task)
        self.assertEqual((row["root_seq"], row["status"]), (first["root_seq"], "outdated"))
        second = self.ok(self.resubmit(first, revised))
        self.assertEqual(second["root_seq"], first["root_seq"])
        row, = f.rt.team_reviews.view(f.task)
        self.assertEqual(row["author"], f.manager)
        self.assertEqual(row["artifacts"], self.refs(revised))

    def test_persisted_parallel_roots_can_each_explicitly_continue_without_cross_blocking(self):
        f = self.f
        old = f.publish(author=f.manager)
        first = self.start(old)
        self.decide(first, "changes_requested")
        partner_file = f.publish("previous parallel candidate\n", author=f.worker)
        # Reproduce a parallel root persisted by the former author-filtered guard.
        legacy = f.rt.log.emit(type="team.review_requested", actor=self.event(first["review_seq"]).actor,
            task_id=f.task, payload={**{key: value for key, value in self.event(first["review_seq"]).payload.items()
                                      if key not in {"owner", "publishers"}},
                "root_seq": None, "author": f.worker, "requested_by": f.worker,
                "artifacts": self.refs(partner_file), "run_id": "legacy-parallel-root"})
        message = f.rt.team._send(f.task, f.rt.team._role(f.worker), f.reviewer,
            "Inspect the persisted parallel candidate.", "assignment", review_seq=legacy.seq)
        self.ok(f.record(legacy.seq, "changes_requested", source=message))
        current = f.publish("corrected shared filename\n", author=f.worker)
        publications = f.events("deliverable.published")
        manager_revision = self.ok(self.resubmit(first, current))
        partner_revision = self.ok(f.request([current], requester=f.worker, review_seq=legacy.seq))
        self.assertEqual(manager_revision["root_seq"], first["root_seq"])
        self.assertEqual(partner_revision["root_seq"], legacy.seq)
        rows = f.rt.team_reviews.view(f.task)
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["author"] for r in rows}, {f.manager, f.worker})
        self.assertEqual({r["status"] for r in rows}, {"pending"})
        self.assertTrue(all(r["artifacts"] == self.refs(current) for r in rows))
        self.assertEqual(f.events("deliverable.published"), publications)
        self.assertEqual(len(f.events("team.review_recorded")), 2)
        for revision, owner in ((manager_revision, f.manager), (partner_revision, f.worker)):
            decision = self.ok(f.record(revision["review_seq"]))
            report = self.event(decision["message_seq"])
            self.assertEqual(report.payload["recipient"], owner)
            f.exchange(owner, source=report)
        rows = f.rt.team_reviews.view(f.task)
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["status"] for r in rows}, {"approved"})
        self.assertEqual(len(f.events("team.review_recorded")), 4)
        self.ok(f.finish())
        self.assert_delivery(current)
        self.assertEqual(f.events("deliverable.published"), publications)
        self.assertTrue(f.rt.verify_chain().ok)

    def test_reviewer_cannot_publish_any_candidate_or_be_the_owner(self):
        f = self.f
        retained = f.publish(author=f.manager, filename="readme.txt")
        first = self.start(retained)
        self.decide(first, "changes_requested")
        reviewer_file = f.publish("reviewer-authored correction\n", author=f.reviewer)
        partner_file = f.publish("partner addition\n", author=f.worker, filename="partner.txt")
        for selection in ([reviewer_file], [retained, partner_file, reviewer_file]):
            self.reject(lambda: self.resubmit(first, *selection), code="reviewer_mismatch")
        self.reject(lambda: self.resubmit(first, partner_file, reviewer=f.manager),
                    code="reviewer_mismatch")
        self.reject(lambda: self.resubmit(first, reviewer_file, reviewer=f.observer),
                    code="reviewer_mismatch", expected=("reviewer", f.reviewer))
        self.assertEqual(len(f.events("team.review_requested")), 1)
        self.reject(lambda: f.record(first["review_seq"]))

    def test_latest_content_and_publication_after_feedback_remain_required(self):
        f = self.f
        old = f.publish(author=f.manager)
        first = self.start(old)
        before_feedback = f.publish("partner candidate\n", author=f.worker, filename="candidate.txt")
        self.decide(first, "changes_requested")
        self.reject(lambda: self.resubmit(first, before_feedback))
        current = f.publish("partner final\n", author=f.worker, filename="candidate.txt")
        for selection in ([before_feedback], [before_feedback, current], [old]):
            self.reject(lambda: self.resubmit(first, *selection))
        self.ok(self.resubmit(first, current))
        self.reject(lambda: self.resubmit(first, current))
        self.reject(lambda: f.record(first["review_seq"]))

    def test_pending_chain_requires_new_publication_but_not_an_invented_feedback_requirement(self):
        f = self.f
        old = f.publish(author=f.manager)
        first = self.start(old)
        self.reject(lambda: self.resubmit(first, old))
        current = f.publish("pending replacement\n", author=f.worker)
        second = self.ok(self.resubmit(first, current))
        self.assertEqual(f.events("team.review_recorded"), [])
        self.assertEqual(second["root_seq"], first["root_seq"])
        assignments = {r["seq"] for r in f.rt.team.recovery.assignments(f.task)}
        self.assertNotIn(first["message_seq"], assignments)
        self.assertIn(second["message_seq"], assignments)

    def test_stale_foreign_and_unknown_requests_or_artifacts_remain_rejected(self):
        f = self.f
        old = f.publish(author=f.manager)
        first = self.start(old)
        other = f.rt.create_task("Private group")
        f.rt.team.enable(other)
        private = f.publish("PRIVATE_BODY", author=f.manager, filename="private.txt", task_id=other)
        foreign = self.ok(f.request([private], requester=f.manager, task_id=other))
        current = f.publish("local corrected\n", author=f.worker)
        errors = [self.reject(lambda: f.request([current], requester=f.manager, review_seq=seq))
                  for seq in (foreign["review_seq"], 99999999)]
        self.assertEqual(errors[0]["message"], errors[1]["message"])
        for seq in (private, 99999999):
            result = self.reject(lambda: self.resubmit(first, seq))
            self.assertNotIn("private.txt", json.dumps(result))
            self.assertNotIn("PRIVATE_BODY", json.dumps(result))
        self.ok(f.exchange(f.manager, ("set_objective", {"goal": "A different objective"}))[0])
        self.reject(lambda: self.resubmit(first, current))
        row, = f.rt.team_reviews.view(f.task)
        self.assertEqual(row["status"], "stale")

    def test_unknown_publisher_is_not_accepted_as_a_partner(self):
        f = self.f
        old = f.publish(author=f.manager)
        first = self.start(old)
        self.decide(first, "changes_requested")
        unknown = f.rt.log.emit(type="deliverable.published", task_id=f.task,
            actor=Actor.agent("unknown-partner", "developer"), payload={**self.event(old).payload})
        self.reject(lambda: self.resubmit(first, unknown.seq))


if __name__ == "__main__":
    unittest.main()
