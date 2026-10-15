import json
import unittest

from cater.events import Actor, EventLog
from cater.review_contract import receipt_context


class ReceiptContextTests(unittest.TestCase):
    def test_browser_fact_has_one_representation_and_full_receipt_remains_unchanged(self):
        log = EventLog(":memory:")
        self.addCleanup(log.close)
        report = {"outcome": "passed", "steps": [{"index": 1, "action": "assert_js", "actual": True}]}
        payload = {"check_kind": "browser", "outcome": "passed", "browser_report": report,
                   "stdout": {"complete": True, "truncated": False, "excerpt": json.dumps(report), "sha256": "a" * 64}}
        event = log.emit(type="verification.completed", actor=Actor.system("local_verifier"), payload=payload)
        context = receipt_context(event)
        self.assertNotIn("excerpt", context["stdout"])
        self.assertEqual(context["browser_report"], report)
        self.assertEqual(context["event_hash"], event.hash)
        self.assertEqual(context["seq"], event.seq)
        self.assertEqual(event.payload, payload)
        self.assertEqual(log.read()[0].payload, payload)

    def test_other_command_output_and_invalid_browser_diagnostics_are_retained(self):
        log = EventLog(":memory:")
        self.addCleanup(log.close)
        for kind, report in (("command", {}), ("browser", {"outcome": "unavailable"})):
            event = log.emit(type="verification.completed", actor=Actor.system("local_verifier"),
                payload={"check_kind": kind, "browser_report": report,
                         "stdout": {"complete": True, "truncated": False, "excerpt": "actual diagnostic"}})
            self.assertEqual(receipt_context(event)["stdout"]["excerpt"], "actual diagnostic")
