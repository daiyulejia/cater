import copy
import unittest

from cater.artifacts import check_measurements
from cater.delivery import DeliveryService, delivery_schema


class DeliveryMeasurementTests(unittest.TestCase):
    def setUp(self):
        self.source = "Each artifact must contain at most 120 CJK ideographs."
        self.context = {"manifest": {"sources": [{"id": "1:goal:0", "seq": 1, "text": self.source}],
            "criteria": [], "items": [{"id": "a", "submission": {"artifact_seqs": [10]}},
                                      {"id": "b", "submission": {"artifact_seqs": [20]}}]},
            "artifacts": [{"seq": 10, "content": "文" * 87}, {"seq": 20, "content": "文" * 128}],
            "local_checks": {}}
        self.measure = {"artifact_seq": 20, "metric": "cjk_ideographs", "operator": "le", "limit": 120,
                        "source_quote": self.source}
        self.raw = {"requirements": [{"id": "1:goal:0", "status": "passed", "reason": "128 is within 120.",
            "evidence": [{"artifact_seq": 10, "quote": "文"}], "superseded_by": None, "source_quote": None,
            "measurements": [self.measure]}], "checks": [],
            "handoff": {"summary": "Notes", "why": "Review text", "reproduce": ["Open each note"], "limits": []}}

    def test_host_rejects_false_pass_and_only_reworks_measured_failure(self):
        result = DeliveryService._verdict(self.raw, self.context)
        self.assertEqual(result["status"], "rework")
        self.assertEqual(result["affected"], ["b"])
        row = result["requirements"][0]
        self.assertEqual(row["model_status"], "passed")
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["measurements"][0]["observed"], 128)
        self.assertIn("128", row["reason"])
        self.assertEqual(self.raw["requirements"][0]["status"], "passed")

    def test_exact_boundary_pass_does_not_override_semantic_failure(self):
        self.context["artifacts"][1]["content"] = "文" * 120
        self.assertEqual(DeliveryService._verdict(self.raw, self.context)["status"], "accepted")
        self.raw["requirements"][0]["status"] = "failed"
        result = DeliveryService._verdict(self.raw, self.context)
        self.assertEqual(result["status"], "rework")
        self.assertEqual(result["affected"], ["a"])

    def test_tampered_observed_values_and_foreign_sources_cannot_be_adopted(self):
        changes = [{"observed": 80}, {"limit": True}, {"limit": -1}, {"limit": float("inf")},
                   {"source_quote": "Different requirement"}, {"artifact_seq": 99}, {"metric": "tokens"},
                   {"operator": "eval"}]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                check_measurements([{**self.measure, **change}], {a["seq"]: a for a in self.context["artifacts"]}, self.source)

    def test_comparisons_are_computed_and_do_not_trust_metadata(self):
        artifacts = {20: {"content": "文" * 128, "content_metrics": {"cjk_ideographs": 1}}}
        for operator, limit, status in (("eq", 128, "passed"), ("ge", 129, "failed"), ("ge", 128, "passed")):
            result = check_measurements([{**self.measure, "operator": operator, "limit": limit}], artifacts, self.source)
            self.assertEqual(result[0]["observed"], 128)
            self.assertEqual(result[0]["status"], status)

    def test_schema_binds_artifacts_and_legacy_unmeasured_rows_remain_compatible(self):
        schema = delivery_schema(self.context)
        fields = schema["$defs"]["requirements"]["properties"]
        self.assertEqual(fields["measurements"]["items"]["properties"]["artifact_seq"]["enum"], [10, 20])
        old = copy.deepcopy(self.raw)
        del old["requirements"][0]["measurements"]
        self.assertEqual(DeliveryService._verdict(old, self.context)["status"], "accepted")

    def test_failure_reason_translates_metrics_and_operators_without_changing_raw_fields(self):
        metrics = {"cjk_ideographs": ("原文汉字数", 128), "non_whitespace_codepoints": ("非空白字符数", 128),
                   "utf8_bytes": ("UTF-8字节数", 384), "codepoints": ("原文字符数", 128)}
        operators = {"le": "不超过", "ge": "不少于", "eq": "等于"}
        for metric, (label, observed) in metrics.items():
            for operator, comparison in operators.items():
                with self.subTest(metric=metric, operator=operator):
                    raw = copy.deepcopy(self.raw)
                    measure = raw["requirements"][0]["measurements"][0]
                    measure.update(metric=metric, operator=operator, limit=observed - 1 if operator == "le" else observed + 1)
                    row = DeliveryService._verdict(raw, self.context)["requirements"][0]
                    self.assertIn(f"{label}实测 {observed}，要求{comparison} {measure['limit']}", row["reason"])
                    self.assertNotIn(metric, row["reason"])
                    self.assertEqual(row["model_status"], "passed")
                    self.assertEqual(row["status"], "failed")
                    self.assertIn(self.raw["requirements"][0]["reason"], row["reason"])
                    self.assertEqual({key: row["measurements"][0][key] for key in measure}, measure)

    def test_failure_reason_prefers_filename_and_falls_back_to_artifact_seq(self):
        original = DeliveryService._verdict(self.raw, self.context)
        self.assertIn("正文 #20", original["requirements"][0]["reason"])
        self.context["artifacts"][1]["filename"] = "second.md"
        named = DeliveryService._verdict(self.raw, self.context)
        self.assertIn("second.md，原文汉字数", named["requirements"][0]["reason"])
        self.assertNotIn("正文 #20", named["requirements"][0]["reason"])
        self.assertEqual(named["affected"], original["affected"])
        self.assertEqual(named["requirements"][0]["measurements"], original["requirements"][0]["measurements"])


if __name__ == "__main__":
    unittest.main()
