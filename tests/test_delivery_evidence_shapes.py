import copy
import json
import unittest

from cater.delivery import DeliveryService
import test_delivery as delivery_tests


class DeliveryEvidenceShapeTests(unittest.TestCase):
    def setUp(self):
        self.context = {"manifest": {"sources": [{"id": "goal", "seq": 1, "text": "Add numbers"}],
            "criteria": [{"id": "integration", "kind": "review", "text": "Check integration"}], "items": []},
            "artifacts": [{"seq": 4, "content": "return a + b"}], "local_checks": {}}
        row = {"id": "goal", "status": "passed", "reason": "Observed source", "superseded_by": None,
               "evidence": [{"artifact_seq": 4, "quote": "return a + b"}]}
        self.raw = {"requirements": [row], "checks": [{**copy.deepcopy(row), "id": "integration"}],
                    "handoff": {"summary": "Added numbers", "why": "Fix the shared helper", "reproduce": ["Run tests"], "limits": []}}

    def test_single_evidence_object_preserves_the_same_exact_quote_and_judgment(self):
        self.raw["requirements"][0]["evidence"] = self.raw["requirements"][0]["evidence"][0]
        result = DeliveryService._verdict(self.raw, self.context)
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["requirements"][0]["evidence"], [{"artifact_seq": 4, "quote": "return a + b"}])

    def test_unknown_null_evidence_remains_unknown(self):
        self.raw["requirements"][0].update(status="unknown", evidence=None)
        self.assertEqual(DeliveryService._verdict(self.raw, self.context)["status"], "needs_evidence")

    def test_singleton_negative_judgment_is_not_promoted(self):
        self.raw["requirements"][0].update(status="failed", evidence={"artifact_seq": 4, "quote": "a + b"})
        self.assertEqual(DeliveryService._verdict(self.raw, self.context)["status"], "rework")

    def test_bad_quote_or_string_evidence_is_not_inferred(self):
        for evidence in ({"artifact_seq": 4, "quote": "invented"}, "return a + b", None):
            self.raw["requirements"][0]["evidence"] = evidence
            with self.assertRaises(ValueError):
                DeliveryService._verdict(self.raw, self.context)

    def test_invalid_response_is_recorded_for_diagnosis_without_execution(self):
        fixture = delivery_tests.DeliveryTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.final.transform = lambda raw: {**raw, "requirements": [{**row, "evidence": "wrong shape"} for row in raw["requirements"]]}
        fixture.prepare()
        events = list(fixture.rt.log.iter_events(task_id=fixture.task, event_types=("delivery.response_recorded",)))
        self.assertEqual(len(events), 1)
        self.assertIn("wrong shape", events[0].payload["text"])
        self.assertIn("evidence", events[0].payload["error"])
        self.assertEqual(json.loads(events[0].payload["text"])["requirements"][0]["evidence"], "wrong shape")
        self.assertNotEqual(fixture.rt.delivery.view(fixture.task)["status"], "accepted")

    def actual_receipt(self, seq=20):
        return {"seq": seq, "command": ["python", "tests.py"], "outcome": "passed", "exit_code": 0,
                "artifacts_unchanged": True, "stdout": {"complete": True, "excerpt": "four original cases passed"},
                "stderr": {"complete": True, "excerpt": ""}}

    def test_requirements_can_cite_observed_current_execution(self):
        self.context["local_checks"] = {"behavior": self.actual_receipt()}
        self.raw["requirements"][0]["evidence"] = [{"receipt_seq": 20, "quote": "four original cases passed"}]
        result = DeliveryService._verdict(self.raw, self.context)
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["requirements"][0]["evidence"], [{"receipt_seq": 20, "quote": "four original cases passed"}])

    def test_wrong_source_namespace_resolves_only_one_exact_actual_match(self):
        self.context["local_checks"] = {"behavior": self.actual_receipt()}
        self.raw["requirements"][0]["evidence"] = [{"artifact_seq": 4, "quote": "four original cases passed"}]
        result = DeliveryService._verdict(self.raw, self.context)
        row = result["requirements"][0]
        self.assertEqual(row["citation_repairs"][0]["original_artifact_seq"], 4)
        self.assertEqual(row["evidence"][0]["receipt_seq"], 20)
        self.context["local_checks"]["other"] = self.actual_receipt(21)
        with self.assertRaises(ValueError):
            DeliveryService._verdict(self.raw, self.context)

    def test_forged_receipt_quote_and_two_sources_are_rejected(self):
        self.context["local_checks"] = {"behavior": self.actual_receipt()}
        for evidence in ({"receipt_seq": 99, "quote": "four original cases passed"},
                         {"receipt_seq": 20, "quote": "invented success"},
                         {"receipt_seq": 20, "artifact_seq": 4, "quote": "four original cases passed"}):
            self.raw["requirements"][0]["evidence"] = [evidence]
            with self.assertRaises(ValueError):
                DeliveryService._verdict(self.raw, self.context)

    def test_source_resolution_keeps_negative_and_unknown_judgments(self):
        self.context["local_checks"] = {"behavior": self.actual_receipt()}
        self.raw["requirements"][0].update(status="failed", evidence=[{"artifact_seq": 4, "quote": "four original cases passed"}])
        self.assertEqual(DeliveryService._verdict(self.raw, self.context)["status"], "rework")


if __name__ == "__main__":
    unittest.main()
