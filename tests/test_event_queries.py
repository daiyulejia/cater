from __future__ import annotations

from contextlib import closing
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cater.availability import team_availability
from cater.events import Actor, EventLog, UnknownEventType
from cater.runtime import Runtime
from cater.fold import fold


class EventQueryTests(unittest.TestCase):
    def populate(self, log):
        events = []
        for task, type in (("a", "task.created"), ("a", "msg.said"), ("b", "msg.challenge"), ("a", "deliverable.published")):
            events.append(log.emit(actor=Actor.agent("product", "worker"), type=type, task_id=task, payload={"driver": "product", "tokens": 5, "token_usage": "reported", "content": "x" * 65536 if type == "deliverable.published" else ""}))
        return events

    def test_stream_filter_order_task_and_sequence_match_full_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = EventLog(Path(tmp) / "queries.db", audit_key="fixture")
            try:
                self.populate(log)
                full = log.read()
                types = ("msg.said", "msg.challenge")
                self.assertEqual(list(log.iter_events(event_types=types)), [event for event in full if event.type in types])
                self.assertEqual(list(log.iter_events(task_id="a", event_types=types, after_seq=1)), [full[1]])
                self.assertEqual(list(log.iter_events(after_seq=2)), full[2:])
                self.assertEqual(log.read("a"), [event for event in full if event.task_id == "a"])
                self.assertEqual(list(log.iter_events(event_types=[])), [])
                self.assertTrue(log.verify_chain().ok)
            finally:
                log.close()

    def test_budget_query_never_deserializes_artifact_bodies(self):
        with Runtime(":memory:") as rt:
            self.populate(rt.log)
            original = rt.log._row_to_event
            converted = []
            def selected_only(row):
                self.assertIn(row["type"], {"msg.said", "msg.challenge"})
                converted.append(row["seq"])
                return original(row)
            with patch.object(rt.log, "_row_to_event", side_effect=selected_only):
                self.assertEqual(rt._reported_driver_tokens("product"), 10)
            self.assertEqual(converted, [2, 3])

    def test_no_budget_does_not_scan_ledger_for_status_or_model_call(self):
        with Runtime(":memory:") as rt:
            task = rt.create_task("无需预算扫描")
            with patch.object(rt.log, "iter_events", side_effect=AssertionError("ledger scan")):
                self.assertTrue(team_availability(rt)["members"])
            with patch.object(rt, "_reported_driver_tokens", side_effect=AssertionError("unused budget")):
                rt.collaborate(task)

    def test_status_budget_reads_only_message_events_and_ignores_bad_driver_metadata(self):
        with Runtime(":memory:") as rt:
            rt.drivers["product"].token_budget = 10
            self.populate(rt.log)
            rt.log.emit(actor=Actor.agent("product", "worker"), type="msg.said", task_id="a", payload={"driver": [], "tokens": 100, "token_usage": "reported"})
            original = rt.log._row_to_event
            def selected_only(row):
                self.assertIn(row["type"], {"msg.said", "msg.challenge"})
                return original(row)
            with patch.object(rt.log, "_row_to_event", side_effect=selected_only):
                status = next(item for item in team_availability(rt)["members"] if item["role_id"] == "product")
            self.assertEqual(status["reason"], "budget_exhausted")

    def test_invalid_custom_budget_is_not_treated_as_unlimited(self):
        for budget in (True, -1, "5"):
            with self.subTest(budget=budget), Runtime(":memory:") as rt:
                driver = rt.drivers["product"]
                driver.token_budget = budget
                task = rt.create_task("非法预算配置")
                rt.assign(task, "product")
                with patch.object(driver, "complete", side_effect=AssertionError("invalid budget must not call driver")) as complete:
                    rt.collaborate(task)
                    complete.assert_not_called()
                status = next(item for item in team_availability(rt)["members"] if item["role_id"] == "product")
                self.assertEqual(status["reason"], "invalid_budget")

    def test_filters_validate_names_and_nonnegative_sequence(self):
        log = EventLog(":memory:")
        try:
            for seq in (-1, True, "1"):
                with self.subTest(seq=seq), self.assertRaises(ValueError): list(log.iter_events(after_seq=seq))
            with self.assertRaises(UnknownEventType): list(log.iter_events(event_types=["not.registered"]))
            with self.assertRaises(ValueError): list(log.iter_events(event_types="msg.said"))
            for seq in (0, -1, True, "1"):
                with self.subTest(before=seq), self.assertRaises(ValueError):
                    list(log.iter_events(before_seq=seq))
        finally:
            log.close()

    def test_exclusive_upper_bound_filters_in_sql_before_deserializing_future_artifact(self):
        with closing(EventLog(":memory:")) as log:
            events = self.populate(log)
            original = log._row_to_event
            seen = []
            def before_only(row):
                self.assertLess(row["seq"], events[-1].seq)
                seen.append(row["seq"])
                return original(row)
            with patch.object(log, "_row_to_event", side_effect=before_only):
                self.assertEqual(list(log.iter_events(after_seq=1, before_seq=4)), events[1:3])
                self.assertEqual(list(log.iter_events(task_id="b", before_seq=4)), [events[2]])
                self.assertEqual(list(log.iter_events(after_seq=4, before_seq=4)), [])
            self.assertEqual(seen, [2, 3, 3])
            self.assertEqual(log.read(), events)

    def test_task_snapshot_matches_full_projection_without_reading_other_tasks(self):
        with Runtime(":memory:") as rt:
            self.populate(rt.log)
            expected = fold(rt.log.read())["a"]
            original = rt.log._row_to_event
            def selected_only(row):
                self.assertEqual(row["task_id"], "a")
                return original(row)
            with patch.object(rt.log, "_row_to_event", side_effect=selected_only):
                self.assertEqual(rt.task("a"), expected)
            self.assertIsNone(rt.task("absent"))

    def test_existing_event_store_gains_indexes_without_changing_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "upgrade.db"
            log = EventLog(path, audit_key="fixture")
            self.populate(log)
            old = log.read(); head = log.head_hash()
            log.db.execute("DROP INDEX events_type_seq")
            log.db.execute("DROP INDEX events_task_seq")
            log.db.commit(); log.close()
            reopened = EventLog(path, audit_key="fixture")
            try:
                self.assertEqual(reopened.read(), old)
                self.assertEqual(reopened.head_hash(), head)
                self.assertTrue(reopened.verify_chain().ok)
                indexes = {row["name"] for row in reopened.db.execute("PRAGMA index_list(events)")}
                self.assertTrue({"events_type_seq", "events_task_seq"} <= indexes)
                plan = reopened.db.execute("EXPLAIN QUERY PLAN SELECT * FROM events WHERE type IN (?, ?) AND seq > ? ORDER BY seq", ("msg.said", "msg.challenge", 0)).fetchall()
                self.assertTrue(any("events_type_seq" in row["detail"] for row in plan))
            finally:
                reopened.close()
