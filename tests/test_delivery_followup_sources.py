"""Historical evidence projection, without a model, database or workspace run."""

import copy
from dataclasses import replace
from types import SimpleNamespace
import unittest

from cater.delivery_followup_sources import delivery_followup_sources
from cater.events import Actor, Event
from cater.team_read_sources import bound_receipts
from cater.tool_protocol import json_utf8


HOST = Actor.system("team")
SCOPE = {"run_id": "original-run", "message_seq": 5, "revision": 13, "root": "F:/project"}
# Original 16/22 source; original 24 output with only absolute traceback paths shortened.
IMPLEMENTATION = "def total_cents(values):\n    return sum(value for value in values if value > 0)\n"
TEST_SOURCE = """import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from total import total_cents


class TotalTests(unittest.TestCase):
    def test_positive_and_zero(self):
        self.assertEqual(total_cents([120, 80, 0]), 200)

    def test_refund_contributes_to_total(self):
        self.assertEqual(total_cents([800, -300]), 500)

    def test_empty_is_zero(self):
        self.assertEqual(total_cents([]), 0)

    def test_input_is_not_modified(self):
        values = [-20, 30]
        self.assertEqual(total_cents(values), 10)
        self.assertEqual(values, [-20, 30])


if __name__ == '__main__':
    unittest.main()
"""
FAILURE = """test_empty_is_zero (test_total.TotalTests.test_empty_is_zero) ... ok
test_input_is_not_modified (test_total.TotalTests.test_input_is_not_modified) ... FAIL
test_positive_and_zero (test_total.TotalTests.test_positive_and_zero) ... ok
test_refund_contributes_to_total (test_total.TotalTests.test_refund_contributes_to_total) ... FAIL

======================================================================
FAIL: test_input_is_not_modified (test_total.TotalTests.test_input_is_not_modified)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "F:/project/tests/test_total.py", line 21, in test_input_is_not_modified
    self.assertEqual(total_cents(values), 10)
AssertionError: 30 != 10

======================================================================
FAIL: test_refund_contributes_to_total (test_total.TotalTests.test_refund_contributes_to_total)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "F:/project/tests/test_total.py", line 14, in test_refund_contributes_to_total
    self.assertEqual(total_cents([800, -300]), 500)
AssertionError: 800 != 500

----------------------------------------------------------------------
Ran 4 tests in 0.001s

FAILED (failures=2)
""".replace("\n", "\r\n")


def event(seq, kind, payload, *, actor=HOST, task_id="task"):
    return Event(seq, str(seq), "2026-10-22", task_id, actor, kind, "execution", payload,
                 "0" * 64, f"{seq:064x}")


def stream(text):
    return {"excerpt": text, "bytes": len(text.encode("utf-8")), "complete": True,
            "truncated": False, "utf8_valid": True}


def command_result(text=FAILURE):
    return {"status": "error", "outcome": "failed", "exit_code": 1, "complete": True,
            "truncated": False, "timed_out": False, "stdout": stream(""), "stderr": stream(text)}


def file_result(path, text):
    size = len(text.encode("utf-8"))
    return {"status": "ok", "path": path, "content": text, "bytes": size,
            "offset": 0, "returned_bytes": size, "truncated": False, "encoding": "utf-8"}


def pair(seq, name, result, **scope):
    start = event(seq - 1, "team.tool_started", {**SCOPE, **scope, "name": name,
                  "role": "store_manager", "call_id": str(seq), "arguments": {}})
    finish = event(seq, "team.tool_finished", {"started_seq": start.seq, "name": name,
                   "role": "store_manager", "run_id": start.payload["run_id"], "result": result})
    return finish, start


def delivery(sources):
    refs = [{"source_seq": e.seq, "source_hash": e.hash} for e, _ in sources]
    return event(98, "team.objective_completed", {"objective_seq": 13, "handoff": {
        "version": 1, "command_receipts": [ref for ref, (e, _) in zip(refs, sources)
                                            if e.payload["name"] == "workspace_run"][:4],
        "evidence": refs}})


class DeliveryFollowupSourcesTests(unittest.TestCase):
    def setUp(self):
        self.sources = [pair(16, "workspace_read", file_result("total.py", IMPLEMENTATION)),
                        pair(22, "workspace_read", file_result("tests/test_total.py", TEST_SOURCE)),
                        pair(24, "workspace_run", command_result())]
        self.delivery = delivery(self.sources)

    def test_bound_actual_evidence_is_present_without_semantic_conclusions_or_mutation(self):
        message = event(5, "team.message_received", {"recipient": "store_manager", "kind": "message"},
                        actor=Actor.human())
        run = event(8, "team.run_started", {"run_id": SCOPE["run_id"], "role": "store_manager", "message_seq": 5})
        events = [message, run, *[e for pair_ in self.sources for e in reversed(pair_)], self.delivery]
        team = SimpleNamespace(rt=SimpleNamespace(organization=SimpleNamespace(
            leader=SimpleNamespace(id="store_manager")), roles=[]), _messages=lambda *a, **kw: [message])
        original = copy.deepcopy(events)
        view = delivery_followup_sources(self.delivery, bound_receipts(team, "task", events), objective_revision=13)
        rows = view["sources"]
        self.assertEqual([r["source_seq"] for r in rows], [24, 22, 16])
        self.assertEqual(rows[0]["parts"], [{"field": "stdout.excerpt", "literal": ""},
                                             {"field": "stderr.excerpt", "literal": FAILURE}])
        self.assertEqual(rows[1]["parts"], [{"field": "content", "literal": TEST_SOURCE}])
        self.assertEqual(rows[2]["parts"], [{"field": "content", "literal": IMPLEMENTATION}])
        self.assertEqual((rows[0]["status"], rows[0]["outcome"], rows[0]["exit_code"]), ("error", "failed", 1))
        for row in rows:
            self.assertEqual(row["source_hash"], f"{row['source_seq']:064x}")
            self.assertEqual(row["read_args"], {"receipt_seq": row["source_seq"]})
            self.assertTrue(row["body_included"])
            self.assertFalse({"failed_requirements", "assertions_reached", "runtime_version", "bug_cause"} & row.keys())
        self.assertLessEqual(len(json_utf8(view)), 4096)
        rows[0]["parts"][1]["literal"] = "changed output"
        rows[1]["read_args"]["receipt_seq"] = 999
        self.assertEqual(events, original)

    def test_selects_only_referenced_latest_failure_and_two_prior_same_scope_reads(self):
        earlier = pair(18, "workspace_read", file_result("other.py", "earlier\n"))
        later_read = pair(28, "workspace_read", file_result("later.py", "later\n"))
        success = pair(30, "workspace_run", {**command_result("OK\n"), "status": "ok", "outcome": "passed", "exit_code": 0})
        unrelated = pair(72, "workspace_run", command_result("unreferenced failure\n"))
        sources = [*self.sources, earlier, later_read, success]
        saved = delivery(sources)
        saved.payload["handoff"]["evidence"].append({"source_seq": 24, "source_hash": self.sources[-1][0].hash})
        view = delivery_followup_sources(saved, reversed([*sources, unrelated]), objective_revision=13)
        self.assertEqual([r["source_seq"] for r in view["sources"]], [24, 22, 18])
        self.assertTrue(view["sources_truncated"])
        newer = pair(32, "workspace_run", command_result("another failure\n"))
        sources.append(newer)
        view = delivery_followup_sources(delivery(sources), iter(sources), objective_revision=13)
        self.assertEqual([r["source_seq"] for r in view["sources"]], [32, 28, 22])

    def test_stale_mismatched_and_foreign_sources_cannot_supply_bodies(self):
        for key, value in (("root", "F:/other"), ("revision", 12), ("run_id", "other-run"), ("message_seq", 6)):
            with self.subTest(scope=key):
                sources = copy.deepcopy(self.sources)
                sources[1][1].payload[key] = value
                view = delivery_followup_sources(self.delivery, sources, objective_revision=13)
                self.assertEqual([r["source_seq"] for r in view["sources"]], [24, 16])
        for change in ("task", "hash", "revision", "after_delivery", "conflicting_reference"):
            with self.subTest(change=change):
                sources, saved = copy.deepcopy((self.sources, self.delivery))
                finish, start = sources[-1]
                if change == "task":
                    sources[-1] = replace(finish, task_id="foreign"), replace(start, task_id="foreign")
                elif change == "hash":
                    sources[-1] = replace(finish, hash="f" * 64), start
                elif change == "revision":
                    start.payload["revision"] = 12
                elif change == "after_delivery":
                    saved = replace(saved, seq=23)
                else:
                    saved.payload["handoff"]["evidence"].append({"source_seq": 24, "source_hash": "f" * 64})
                self.assertIsNone(delivery_followup_sources(saved, sources, objective_revision=13))

    def test_budgets_count_utf8_and_json_and_omit_whole_bodies(self):
        sources = copy.deepcopy(self.sources)
        sources[-1][0].payload["result"] = command_result("\u4e2d" * 683)
        view = delivery_followup_sources(self.delivery, sources, objective_revision=13)
        self.assertEqual(view["sources"][0]["omission_reason"], "source_budget")
        self.assertNotIn("parts", view["sources"][0])
        for budget in (1, 400, 800, 1400, 2400, 4096):
            with self.subTest(budget=budget):
                view = delivery_followup_sources(self.delivery, self.sources, objective_revision=13, max_bytes=budget)
                if view is None:
                    continue
                self.assertLessEqual(len(json_utf8(view)), budget)
                self.assertEqual(view["sources"][0]["source_seq"], 24)
                for row in view["sources"]:
                    if row["body_included"]:
                        expected = {24: FAILURE, 22: TEST_SOURCE, 16: IMPLEMENTATION}[row["source_seq"]]
                        self.assertEqual(row["parts"][-1]["literal"], expected)
                    else:
                        self.assertNotIn("parts", row)
        self.assertIsNone(delivery_followup_sources(self.delivery, self.sources, objective_revision=13, max_bytes=1))
        for budget in (True, 0, 4097):
            with self.subTest(invalid_budget=budget), self.assertRaises(ValueError):
                delivery_followup_sources(self.delivery, self.sources, objective_revision=13, max_bytes=budget)

    def test_initial_revision_is_separate_from_start_boundary(self):
        sources = copy.deepcopy(self.sources)
        for _, start in sources:
            start.payload["revision"] = 0
        view = delivery_followup_sources(self.delivery, sources, objective_revision=0)
        self.assertEqual([r["source_seq"] for r in view["sources"]], [24, 22, 16])
        late_boundary = replace(self.delivery, payload={**self.delivery.payload, "objective_seq": 23})
        self.assertIsNone(delivery_followup_sources(late_boundary, sources, objective_revision=0))
        for invalid in (True, -1, 98, "0", None):
            with self.subTest(revision=invalid), self.assertRaises(ValueError):
                delivery_followup_sources(self.delivery, sources, objective_revision=invalid)

    def test_partial_or_inconsistent_capture_keeps_only_precise_read_pointer(self):
        for location, key, value in (("stderr", "truncated", True), ("stdout", "complete", False),
                                     ("stderr", "bytes", 1), ("stderr", "utf8_valid", False),
                                     (None, "complete", False)):
            with self.subTest(location=location, key=key):
                sources = copy.deepcopy(self.sources)
                result = sources[-1][0].payload["result"]
                (result[location] if location else result)[key] = value
                row = delivery_followup_sources(self.delivery, sources, objective_revision=13)["sources"][0]
                self.assertEqual(row["read_args"], {"receipt_seq": 24})
                self.assertEqual(row["omission_reason"], "incomplete_capture")
                self.assertFalse(row["body_included"])
                self.assertNotIn("parts", row)
        sources = copy.deepcopy(self.sources)
        sources[1][0].payload["result"]["offset"] = 1
        row = delivery_followup_sources(self.delivery, sources, objective_revision=13)["sources"][1]
        self.assertEqual(row["omission_reason"], "incomplete_capture")

    def test_no_eligible_delivery_failure_or_scope_does_not_invent_evidence(self):
        for outcome in ("timeout", "launch_error", "effects_unknown", "not_started"):
            with self.subTest(outcome=outcome):
                sources = copy.deepcopy(self.sources)
                sources[-1][0].payload["result"]["outcome"] = outcome
                self.assertIsNone(delivery_followup_sources(self.delivery, sources, objective_revision=13))
        self.assertIsNone(delivery_followup_sources(self.delivery, self.sources[:2], objective_revision=13))
        self.assertIsNone(delivery_followup_sources(replace(self.delivery, payload={}), self.sources, objective_revision=13))
        self.assertIsNone(delivery_followup_sources(None, self.sources, objective_revision=13))
        sources = copy.deepcopy(self.sources)
        for _, start in sources:
            start.payload.pop("root")
        view = delivery_followup_sources(self.delivery, sources, objective_revision=13)
        self.assertEqual([r["source_seq"] for r in view["sources"]], [24])


if __name__ == "__main__":
    unittest.main()
