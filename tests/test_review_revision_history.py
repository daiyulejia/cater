"""Revision history regressions using composed native-cycle fixtures only."""

import io
import json
import unittest
import zipfile

from cater.events import Actor
from cater.runtime import Runtime
from cater.work import WorkConflict
import test_team_review_cycle as cycle


HOST = Actor.system("team")


class ReviewRevisionHistoryTests(unittest.TestCase):
    def setUp(self):
        self.f = cycle.TeamReviewCycleTests()
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()

    def refs(self, *seqs):
        artifacts = {a["seq"]: a for a in self.f.rt.task(self.f.task).deliverables}
        return [{key: artifacts[seq][key] for key in ("seq", "filename", "content_sha256")}
                for seq in seqs]

    def acknowledge(self, request):
        f = self.f
        report = next(e for e in reversed(f.rt.team._messages(f.task))
                      if e.payload.get("kind") == "report"
                      and e.payload.get("review_seq") == request["review_seq"])
        f.exchange(f.worker, source=report)

    def decide(self, request, verdict="approved", *, reviewer=None):
        result = self.f.record(request["review_seq"], verdict, reviewer=reviewer)
        self.assertEqual(result["status"], "ok", result)
        self.acknowledge(request)
        return result

    def finish(self, seqs=None):
        arguments = {"summary": "Exact revision scope delivered",
                     "verification": "Disposable native fixture; no real model"}
        if seqs is not None:
            arguments["artifact_seqs"] = seqs
        result, = self.f.exchange(self.f.manager, ("finish_work", arguments))
        return result

    def renamed_chain(self):
        f = self.f
        drafts = [f.publish(f"obsolete draft {index}\n", filename="draft.txt")
                  for index in range(3)]
        first = f.request([drafts[-1]])
        self.assertEqual(first["status"], "ok", first)
        self.decide(first, "changes_requested")
        middle = f.publish("intermediate candidate\n", filename="candidate.txt")
        second = f.request([middle], review_seq=first["review_seq"])
        self.assertEqual(second["status"], "ok", second)
        self.decide(second, "changes_requested")
        final = f.publish("approved final bytes\n", filename="final.txt")
        third = f.request([final], review_seq=second["review_seq"])
        self.assertEqual(third["status"], "ok", third)
        self.decide(third)
        notes = f.publish("independent explanation\n", author=f.observer, filename="notes.txt")
        return drafts, middle, final, notes

    def assert_delivery(self, expected):
        f = self.f
        completion = f.events("team.objective_completed")[-1]
        expected_refs = self.refs(*expected)
        self.assertEqual(sorted(completion.payload["artifacts"], key=lambda a: a["seq"]),
                         sorted(expected_refs, key=lambda a: a["seq"]))
        artifacts = {a["seq"]: a for a in f.rt.task(f.task).deliverables}
        with zipfile.ZipFile(io.BytesIO(f.rt.team.archive(
                f.task, expected_delivery_seq=completion.seq))) as archive:
            self.assertEqual(set(archive.namelist()),
                             {ref["filename"] for ref in expected_refs} | {"__cater__/manifest.json"})
            for ref in expected_refs:
                self.assertEqual(archive.read(ref["filename"]),
                                 artifacts[ref["seq"]]["content"].encode("utf-8"))
            manifest = json.loads(archive.read("__cater__/manifest.json"))
            self.assertEqual({entry["seq"] for entry in manifest["files"]}, set(expected))
            for entry in manifest["files"]:
                self.assertEqual(entry["sha256"], artifacts[entry["seq"]]["content_sha256"])
        self.assertTrue(f.rt.verify_chain().ok)

    def legacy_request(self, artifact, *, root=None):
        f = self.f
        request = f.rt.log.emit(type="team.review_requested", actor=HOST, task_id=f.task,
            payload={"root_seq": root, "author": f.worker, "reviewer": f.reviewer,
                     "artifacts": self.refs(artifact), "revision": f.rt.team.view(f.task)["revision"],
                     "instruction": "Inspect legacy exact bytes and the missing edge case.",
                     "run_id": f"legacy-request-{artifact}", "requested_by": f.worker})
        f.rt.team._send(f.task, f.rt.team._role(f.worker), f.reviewer,
                        "Legacy formal review assignment", "assignment", review_seq=request.seq)
        self.assertNotIn("previous_review_seq", request.payload)
        self.assertNotIn("previous_artifacts", request.payload)
        return {"review_seq": request.seq, "root_seq": root or request.seq}

    def legacy_chain(self):
        f = self.f
        original = f.publish("missing edge case\n", filename="legacy.txt")
        first = self.legacy_request(original)
        decision = f.record(first["review_seq"], "changes_requested",
                            feedback="LEGACY_EDGE_CASE: preserve negative input behavior.")
        self.assertEqual(decision["status"], "ok", decision)
        self.acknowledge(first)
        attempt = f.publish("pending legacy attempt\n", filename="legacy.txt")
        second = self.legacy_request(attempt, root=first["root_seq"])
        final = f.publish("current revision awaiting independent review\n", filename="current.txt")
        third = f.request([final], review_seq=second["review_seq"])
        self.assertEqual(third["status"], "ok", third)
        return first, second, third, original, attempt, decision

    def assert_legacy_read(self, rt, first, second, third, original, attempt, decision):
        before = rt.log.read(task_id=self.f.task)
        result = rt.team.recovery.read(self.f.task, {"review_seq": second["review_seq"]})
        self.assertEqual(result["status"], "ok")
        row = result["review"]
        self.assertEqual(row["status"], "superseded")
        self.assertEqual(row["historical_status"], "pending")
        self.assertEqual(row["current_request_seq"], third["review_seq"])
        self.assertEqual(row["artifacts"], self.refs(attempt))
        self.assertEqual(row.get("previous_review_seq"), first["review_seq"])
        self.assertEqual(row.get("previous_artifacts"), self.refs(original))
        parent = rt.team.recovery.read(self.f.task, {"review_seq": row["previous_review_seq"]})
        self.assertEqual(parent["status"], "ok")
        self.assertEqual(parent["review"]["status"], "superseded")
        self.assertEqual(parent["review"]["historical_status"], "changes_requested")
        self.assertEqual(parent["review"]["decision_seq"], decision["decision_seq"])
        self.assertIn("LEGACY_EDGE_CASE", parent["review"]["feedback"])
        self.assertEqual(rt.log.read(task_id=self.f.task), before)
        return row

    def test_default_three_same_name_drafts_and_two_renames_do_not_resurrect(self):
        drafts, middle, final, notes = self.renamed_chain()
        before = self.f.rt.task(self.f.task).deliverables
        self.assertEqual(self.finish()["status"], "ok")
        self.assert_delivery([final, notes])
        self.assertEqual(self.f.rt.task(self.f.task).deliverables, before)
        self.assertTrue(set(drafts + [middle]) <= {a["seq"] for a in before})
        self.assertEqual(len(self.f.events("team.review_requested")), 3)

    def test_explicit_retired_refs_reject_then_final_and_independent_notes_deliver(self):
        drafts, middle, final, notes = self.renamed_chain()
        for retired in (drafts[-1], middle):
            with self.subTest(retired=retired):
                before = self.f.events("team.objective_completed")
                result = self.finish([retired, final, notes])
                self.assertEqual(result.get("code"), "delivery_review_mismatch", result)
                self.assertEqual(self.f.events("team.objective_completed"), before)
        self.assertEqual(self.finish([final, notes])["status"], "ok")
        self.assert_delivery([final, notes])

    def test_other_current_root_keeps_original_exact_ref_after_rename(self):
        f = self.f
        original = f.publish("shared immutable bytes\n", filename="original.txt")
        first = f.request([original])
        self.decide(first)
        side = f.publish("side draft\n", filename="side.txt")
        other = f.request([side], reviewer=f.observer)
        self.decide(other, "changes_requested", reviewer=f.observer)
        revised_side = f.publish("approved side bytes\n", filename="side.txt")
        other = f.request([original, revised_side], reviewer=f.observer, review_seq=other["review_seq"])
        self.assertEqual(other["status"], "ok", other)
        self.decide(other, reviewer=f.observer)
        replacement = f.publish("renamed primary bytes\n", filename="replacement.txt")
        latest = f.request([replacement], review_seq=first["review_seq"])
        self.assertEqual(latest["status"], "ok", latest)
        self.decide(latest)
        rows = f.rt.team_reviews.view(f.task)
        self.assertEqual({row["status"] for row in rows}, {"approved"})
        self.assertEqual(len(rows), 2)
        self.assertEqual(self.finish()["status"], "ok")
        self.assert_delivery([original, revised_side, replacement])

    def test_legacy_a2_read_finds_rejected_a1_but_old_signatures_still_reject(self):
        f = self.f
        chain = self.legacy_chain()
        first, second, third, original, attempt, decision = chain
        self.assert_legacy_read(f.rt, *chain)
        result, = f.exchange(f.manager, ("read_checkpoint", {"review_seq": second["review_seq"]}))
        self.assertEqual((result["status"], result["review"]["status"]), ("ok", "superseded"))
        self.assertEqual(result["review"]["current_request_seq"], third["review_seq"])
        for request in (first, second):
            with self.subTest(review_seq=request["review_seq"]):
                before = (f.events("team.review_requested"), f.events("team.review_recorded"))
                with self.assertRaises(WorkConflict):
                    f.rt.team_reviews._find(f.task, request["review_seq"])
                self.assertEqual(f.record(request["review_seq"])["status"], "error")
                self.assertEqual((f.events("team.review_requested"), f.events("team.review_recorded")), before)
        self.assertEqual(f.rt.team_reviews._find(f.task, third["review_seq"])["status"], "pending")

    def test_legacy_predecessor_survives_runtime_reopen_without_writing_events(self):
        f = self.f
        chain = self.legacy_chain()
        before = f.rt.log.read()
        with Runtime(f.root / "test.db", roles_path=f.roles, drivers={"team_test": f.driver}) as reopened:
            self.assert_legacy_read(reopened, *chain)
            for request in chain[:2]:
                with self.assertRaises(WorkConflict):
                    reopened.team_reviews._find(f.task, request["review_seq"])
            self.assertEqual(reopened.log.read(), before)
            self.assertTrue(reopened.verify_chain().ok)
        self.assertEqual(f.rt.log.read(), before)
        for request in f.events("team.review_requested")[:2]:
            self.assertNotIn("previous_review_seq", request.payload)
            self.assertNotIn("previous_artifacts", request.payload)

    def test_superseded_legacy_history_marks_stale_successor_without_reviving_it(self):
        f = self.f
        chain = self.legacy_chain()
        first, second, third = chain[:3]
        result, = f.exchange(f.manager, ("set_objective", {"goal": "Independent text-only objective"}))
        self.assertEqual(result["status"], "ok")
        before = f.rt.log.read()
        row = self.assert_legacy_read(f.rt, *chain)
        self.assertIs(row.get("objective_current"), False)
        self.assertEqual(row.get("current_request_status"), "stale")
        for request in (first, second, third):
            with self.assertRaises(WorkConflict):
                f.rt.team_reviews._find(f.task, request["review_seq"])
        self.assertEqual(f.rt.log.read(), before)
        self.assertEqual(f.rt.team_reviews.blockers(f.task), [])

    def test_new_root_needs_new_independent_approval_to_reuse_old_immutable_seq(self):
        f = self.f
        original = f.publish("approved historical bytes\n", filename="original.txt")
        first = f.request([original])
        old_decision = self.decide(first)
        replacement = f.publish("approved replacement bytes\n", filename="replacement.txt")
        latest = f.request([replacement], review_seq=first["review_seq"])
        self.assertEqual(latest["status"], "ok", latest)
        self.decide(latest)
        self.assertEqual(self.finish([original]).get("code"), "delivery_review_mismatch")
        publications = f.rt.task(f.task).deliverables
        reused = f.request([original], reviewer=f.observer)
        self.assertEqual(reused["status"], "ok", reused)
        self.assertNotEqual(reused["root_seq"], first["root_seq"])
        current = f.rt.team_reviews._find(f.task, reused["review_seq"])
        self.assertEqual(current["status"], "pending")
        self.assertNotIn("decision_seq", current)
        self.assertEqual(current["artifacts"], self.refs(original))
        self.assertEqual(self.finish([original]).get("code"), "review_pending")
        decisions_before = f.events("team.review_recorded")
        self.assertEqual(f.record(reused["review_seq"])["status"], "error")
        self.assertEqual(f.events("team.review_recorded"), decisions_before)
        new_decision = self.decide(reused, reviewer=f.observer)
        self.assertGreater(new_decision["decision_seq"], old_decision["decision_seq"])
        self.assertEqual(f.rt.task(f.task).deliverables, publications)
        self.assertEqual(self.finish([original, replacement])["status"], "ok")
        self.assert_delivery([original, replacement])


if __name__ == "__main__":
    unittest.main()
