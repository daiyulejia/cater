"""Golden persisted review report formats; no model or external service."""
import unittest

from cater.review_report_text import CURRENT_VERSION, review_report_text


class ReviewReportTextTests(unittest.TestCase):
    def test_historical_and_current_bytes_for_both_verdicts(self):
        expected = {
            (1, "approved"): "复核通过。已核对\n请向原委托人交回这版成果。若继续修改，需将新版再次交回复核。",
            (2, "approved"): "复核通过。已核对\n请收齐这版成果与相关工作回报，再向原委托人交回或由店长整体交付。若继续修改，需将新版再次交回复核。",
            (1, "changes_requested"): "本轮需要修复，尚未验收。已核对\n请修改并发布新版后，调用request_review，review_seq=42，仍交给我复核；不把修复自述当成验收。",
            (2, "changes_requested"): "本轮需要修复，尚未验收。已核对\n请协调实际修复并取得新版后，调用request_review，review_seq=42，仍交给我复核；不把修复自述当成验收。",
        }
        for (version, verdict), value in expected.items():
            with self.subTest(version=version, verdict=verdict):
                actual = review_report_text(verdict, "已核对", 42, version=version)
                self.assertEqual(actual.encode("utf-8"), value.encode("utf-8"))
                if version == CURRENT_VERSION:
                    self.assertEqual(review_report_text(verdict, "已核对", 42), value)

    def test_unknown_version_and_invalid_verdict_are_rejected(self):
        for version in (0, 3, True):
            with self.subTest(version=version), self.assertRaises(ValueError):
                review_report_text("approved", "ok", 1, version=version)
        with self.assertRaises(ValueError):
            review_report_text("unknown", "ok", 1)

    def test_record_review_writes_current_golden_text_for_both_verdicts(self):
        from test_team_review_cycle import TeamReviewCycleTests

        for verdict in ("approved", "changes_requested"):
            with self.subTest(verdict=verdict):
                fixture = TeamReviewCycleTests()
                fixture.setUp()
                try:
                    artifact = fixture.publish()
                    request = fixture.request([artifact])
                    result = fixture.record(request["review_seq"], verdict, feedback="已核对")
                    self.assertEqual(result["status"], "ok")
                    report = next(e for e in fixture.rt.log.read(task_id=fixture.task)
                                  if e.seq == result["message_seq"])
                    self.assertEqual(report.payload["text"], review_report_text(verdict, "已核对", request["review_seq"], version=2))
                    self.assertTrue(fixture.rt.verify_chain().ok)
                finally:
                    fixture.doCleanups()
