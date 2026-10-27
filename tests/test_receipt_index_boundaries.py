"""Four independent index regressions; compose fixtures without extra discovery."""

from __future__ import annotations

from contextlib import contextmanager
import json
import sqlite3
import unittest
from unittest.mock import patch

from cater.tool_protocol import json_utf8
import test_receipt_index as index_fixture


class ReceiptIndexBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.f = index_fixture.ReceiptIndexTests()
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()

    @contextmanager
    def raw_payloads(self, replacements):
        # Parser-only memory copy; never alter the append-only fixture database.
        original = self.f.log.db
        shadow = sqlite3.connect(":memory:")
        shadow.row_factory = sqlite3.Row
        try:
            schema = original.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='events'").fetchone()[0]
            shadow.execute(schema)
            rows = original.execute("SELECT * FROM events")
            shadow.executemany("INSERT INTO events VALUES (" +
                               ",".join("?" for _ in rows.description) + ")", rows)
            shadow.executemany("UPDATE events SET payload_json=? WHERE seq=?", replacements)
            shadow.commit()
            with patch.object(self.f.log, "db", shadow):
                yield
        finally:
            shadow.close()

    def test_terminal_envelope_stays_within_byte_cap_without_losing_receipts(self):
        run, _ = self.f.source_run(run_id="boundary-run")
        result = {"status": "ok", "outcome": "passed", "exit_code": 0,
                  "code": "", "command": ""}
        expected = [self.f.finish(self.f.start(run), dict(result)) for _ in range(15)]
        reader = self.f.reader()
        baseline = self.f.index(limit=50, reader=reader)
        self.f.assert_page(baseline, expected, reader=reader)
        # Tune legitimate bounded fields so the terminal envelope is 8001 bytes.
        # Its true/numeric counterpart fits, exposing the old accounting error.
        padding = 8001 - len(json_utf8(baseline))
        self.assertTrue(0 < padding <= 256 * len(expected), padding)
        self.assertLess(expected[0].seq, 10000)
        replacements = []
        for event in expected:
            payload = {**event.payload, "result": dict(event.payload["result"])}
            for key in ("code", "command"):
                count = min(128, padding)
                payload["result"][key] = "x" * count
                padding -= count
            replacements.append((json.dumps(payload), event.seq))
        self.assertEqual(padding, 0)
        with self.raw_payloads(replacements):
            seen, before = [], None
            for _ in range(len(expected) + 1):
                filters = {"before_seq": before} if before is not None else {}
                page = self.f.index(limit=50, reader=reader, **filters)
                self.assertEqual(page["status"], "ok", page)
                self.assertLessEqual(len(json_utf8(page)), 8000,
                                     "Final false/null envelope must fit the byte budget")
                seqs = [row["seq"] for row in page["receipts"]]
                self.assertTrue(seqs, page)
                self.assertEqual(seqs, sorted(seqs))
                for row in page["receipts"]:
                    exact = self.f.query(row["read_args"])["receipts"]
                    self.assertEqual([item["seq"] for item in exact], [row["seq"]])
                seen.extend(seqs)
                if not page["has_more"]:
                    self.assertIsNone(page["next_before_seq"])
                    break
                self.assertEqual(page["next_before_seq"], min(seqs))
                if before is not None:
                    self.assertLess(page["next_before_seq"], before)
                before = page["next_before_seq"]
            else:
                self.fail("Byte pagination did not terminate")
        self.assertEqual(sorted(seen), [event.seq for event in expected])
        self.assertEqual(len(seen), len(set(seen)))

    def test_invalid_exit_code_types_are_not_reported_as_explicit_null(self):
        run, _ = self.f.source_run()
        values = ["0", {}, [], None, 1]
        expected = [self.f.finish(self.f.start(run), {"status": "error", "exit_code": value})
                    for value in values]
        page = self.f.index()
        self.f.assert_page(page, expected)
        invalid_summaries = []
        for row, value in zip(page["receipts"], values):
            original = self.f.query(row["read_args"])["receipts"][0]["result"]
            self.assertEqual(original, {"status": "error", "exit_code": value})
            self.assertIs(type(original["exit_code"]), type(value))
            summary = row["result_summary"]
            self.assertEqual(summary["status"], "error")
            if value is None or type(value) is int:
                self.assertIn("exit_code", summary)
                self.assertEqual(summary["exit_code"], value)
                self.assertIs(type(summary["exit_code"]), type(value))
            elif "exit_code" in summary:
                invalid_summaries.append(summary)
        self.assertEqual(invalid_summaries, [],
                         "Unsupported exit_code types must not become a recorded null")

    def test_run_filter_does_not_decode_unbounded_run_payload(self):
        run, _ = self.f.source_run(run_id="bounded-index-run")
        finish = self.f.finish(self.f.start(run), {"status": "error", "exit_code": 1})
        marker = "RAW_RUN_BODY"
        payload = json.dumps({**run.payload, "irrelevant": marker * 100000})
        loads = json.loads
        decoded_sizes = []

        def observed_loads(value, *args, **kwargs):
            text = value.decode("utf-8") if isinstance(value, bytes) else value
            if marker in text:
                decoded_sizes.append(len(text))
            return loads(value, *args, **kwargs)

        with self.raw_payloads([(payload, run.seq)]):
            self.assertEqual(self.f.query({"receipt_seq": finish.seq})["receipts"][0]["seq"],
                             finish.seq)
            with patch.object(json, "loads", side_effect=observed_loads):
                self.f.assert_page(self.f.index(), [finish])
                self.assertEqual(decoded_sizes, [])
                page = self.f.index(run_id=run.payload["run_id"])
            self.f.assert_page(page, [finish])
        self.assertEqual(decoded_sizes, [],
                         "The public run_id validation must also use bounded SQL metadata")

    def test_invalid_optional_utf8_does_not_hide_failure_receipts(self):
        run, _ = self.f.source_run()
        fields = ("command", "code", "outcome")
        expected, replacements = [], []
        for field in fields:
            event = self.f.finish(self.f.start(run),
                                  {"status": "error", "outcome": "failed", "exit_code": 1})
            result = {**event.payload["result"], field: "\ud800"}
            replacements.append((json.dumps({**event.payload, "result": result}, ensure_ascii=True),
                                 event.seq))
            expected.append(event)
        with self.raw_payloads(replacements):
            for event, field in zip(expected, fields):
                exact = self.f.query({"receipt_seq": event.seq})["receipts"]
                self.assertEqual([row["seq"] for row in exact], [event.seq])
                self.assertEqual(exact[0]["result"][field], "\ud800")
            page = self.f.index()
            self.f.assert_page(page, expected)
            for row, field in zip(page["receipts"], fields):
                summary = row["result_summary"]
                self.assertEqual(summary["status"], "error")
                self.assertEqual(summary["exit_code"], 1)
                self.assertNotIn(field, summary)
                if field != "outcome":
                    self.assertEqual(summary["outcome"], "failed")


if __name__ == "__main__":
    unittest.main()
