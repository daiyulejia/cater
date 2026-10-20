"""Review tools expose the decision identity without weakening citation checks."""
import unittest

from cater.team_delivery import HandoffError, handoff_schema
import test_team_review_cycle as reviews


class ReviewEvidenceIdentityTests(unittest.TestCase):
    def setUp(self):
        self.f = reviews.TeamReviewCycleTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def prepare(self, verdict="approved"):
        f = self.f
        artifact = f.publish()
        requested = f.request([artifact])
        self.assertEqual(requested["status"], "ok")
        result = f.record(requested["review_seq"], verdict, feedback="Checked exact published bytes.")
        self.assertEqual(result["status"], "ok")
        return artifact, result

    def normalize(self, artifact, source_seq):
        f = self.f
        item = next(a for a in f.rt.task(f.task).deliverables if a["seq"] == artifact)
        ref = {key: item[key] for key in ("seq", "filename", "content_sha256")}
        return f.rt.team.delivery.normalize(f.task, {
            "why": "Explain the actual review", "reproduce": [], "limits": ["No runtime check"],
            "evidence": [{"source_seq": source_seq, "quote": "Checked exact published bytes."}],
        }, artifacts=[ref], revision=f.rt.team.view(f.task)["revision"])

    def test_returned_decision_identity_matches_event_projection_and_citation(self):
        self.assert_decision("approved")

    def test_rejection_also_exposes_its_actual_decision_not_an_approval(self):
        self.assert_decision("changes_requested")

    def assert_decision(self, verdict):
        artifact, result = self.prepare(verdict)
        event = next(e for e in self.f.rt.log.read(task_id=self.f.task)
                     if e.seq == result["decision_seq"])
        self.assertEqual(event.type, "team.review_recorded")
        self.assertEqual(event.payload["review_seq"], result["review_seq"])
        self.assertEqual(event.payload["verdict"], verdict)
        row = next(r for r in self.f.rt.team_reviews.view(self.f.task)
                   if r["request_seq"] == result["review_seq"])
        self.assertEqual(row["decision_seq"], result["decision_seq"])
        evidence, = self.normalize(artifact, result["decision_seq"])["evidence"]
        self.assertEqual(evidence["source_hash"], event.hash)
        self.assertEqual(evidence["kind"], "peer_review")
        self.assertEqual(evidence["status"], verdict)

    def test_request_message_and_tool_receipt_are_not_decision_sources(self):
        artifact, result = self.prepare()
        receipt = next(e for e in reversed(self.f.rt.log.read(task_id=self.f.task))
                       if e.type == "team.tool_finished" and e.payload["name"] == "record_review")
        for seq in (result["review_seq"], result["message_seq"], receipt.seq):
            with self.subTest(seq=seq), self.assertRaises(HandoffError):
                self.normalize(artifact, seq)

    def test_returned_decision_does_not_authorize_a_later_version(self):
        _, result = self.prepare()
        newer = self.f.publish("Different bytes\n")
        with self.assertRaises(HandoffError):
            self.normalize(newer, result["decision_seq"])

    def test_finish_rejects_request_identity_then_accepts_actual_decision(self):
        f = self.f
        artifact, review = self.prepare()
        f.acknowledge_latest_review()
        arguments = {"summary": "Reviewed artifact delivered", "verification": "Source review only",
                     "artifact_seqs": [artifact]}

        def deliver(source_seq):
            result, = f.exchange(f.manager, ("finish_work", {**arguments, "handoff": {
                "why": "Explain the independent review", "reproduce": [], "limits": ["No runtime check"],
                "evidence": [{"source_seq": source_seq, "quote": "Checked exact published bytes."}],
            }}))
            return result

        rejected = deliver(review["review_seq"])
        self.assertEqual(rejected["code"], "invalid_handoff")
        self.assertFalse(any(e.type == "team.objective_completed"
                             for e in f.rt.log.read(task_id=f.task)))
        self.assertEqual(deliver(review["decision_seq"])["status"], "ok")
        completed, = [e for e in f.rt.log.read(task_id=f.task) if e.type == "team.objective_completed"]
        evidence, = completed.payload["handoff"]["evidence"]
        self.assertEqual(evidence["source_seq"], review["decision_seq"])
        self.assertEqual(evidence["kind"], "peer_review")
        self.assertEqual(evidence["status"], "approved")
        self.assertEqual(f.rt.team.view(f.task)["status"], "done")
        self.assertTrue(f.rt.verify_chain().ok)

    def test_schema_explains_decision_identity_at_the_citation_field(self):
        schema = handoff_schema()["properties"]["evidence"]["items"]["properties"]["source_seq"]
        for term in ("decision_seq", "review_seq/request_seq", "message_seq", "record_review"):
            self.assertIn(term, schema["description"])
