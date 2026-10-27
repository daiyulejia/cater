"""Offline index contracts; compose lookup helpers without discovering their tests."""

from __future__ import annotations

import copy
import io
import json
import sqlite3
import unittest
from unittest.mock import patch

from cater.events import Actor
from cater.team import HOST
from cater.tool_protocol import json_utf8
from cater.verification import _Capture
import test_team_receipt_lookup as lookups


class ReceiptIndexTests(unittest.TestCase):
    def setUp(self):
        self.f = lookups.TeamReceiptLookupTests()
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()
        self.rt, self.task = self.f.rt, self.f.task
        self.team, self.log = self.rt.team, self.rt.log
        self.role = self.team._role(self.f.manager)
        self.work = self.team.view(self.task)
        self.counter = 0
        blocked = self.enterContext(patch.object(self.f.driver, "tool_turn", side_effect=AssertionError(
            "Receipt index regressions must not call a model")))
        self.addCleanup(blocked.assert_not_called)
        process = self.enterContext(patch("subprocess.Popen", side_effect=AssertionError(
            "Receipt queries must not execute commands")))
        self.addCleanup(process.assert_not_called)

    def query(self, arguments=None, *, reader=None):
        # Isolate the reader from the unchanged whole-work status preamble.
        with patch.object(self.team, "view", return_value=self.work):
            return self.team._execute(self.task, self.role, "read_receipts",
                                      arguments or {}, None, None, reader, None)

    def index(self, *, reader=None, **filters):
        return self.query({"section": "index", **filters}, reader=reader)

    def stored_result_bytes(self, event):
        # Event storage canonicalizes object key order. Compare with the persisted
        # result, not the pre-emit Python dictionary's insertion order.
        payload, = self.log.db.execute("SELECT payload_json FROM events WHERE seq=?", (event.seq,)).fetchone()
        return json_utf8(json.loads(payload)["result"])

    def emit(self, kind, payload, *, actor=HOST, task=None):
        return self.log.emit(type=kind, actor=actor, task_id=task or self.task, payload=payload)

    def source_run(self, *, role=None, task_id=None, run_id=None):
        # The legacy lookup fixture's default leader actor is not an exact-read
        # authority. Seed a real human input before composing its run helper.
        role = role or self.f.worker
        message = self.emit("team.message_received", {
            "recipient": role, "text": "Offline receipt index input", "kind": "request",
            "revision": self.work["revision"]}, actor=Actor.human(), task=task_id)
        return self.f.receipt_run(role=role, task_id=task_id, run_id=run_id, message=message)

    def start(self, run, *, name="workspace_run", arguments=None, actor=HOST, **overrides):
        self.counter += 1
        return self.emit("team.tool_started", {
            "run_id": run.payload["run_id"], "role": run.payload["role"],
            "message_seq": run.payload["message_seq"], "name": name,
            "call_id": f"index-call-{self.counter}", "arguments": arguments or {},
            **overrides}, actor=actor, task=run.task_id)

    def finish(self, start, result=None, *, actor=HOST, task=None, **overrides):
        return self.emit("team.tool_finished", {
            **{key: start.payload[key] for key in ("run_id", "role", "name", "call_id")},
            "started_seq": start.seq, "result": result if result is not None else {"status": "ok"},
            **overrides}, actor=actor, task=task or start.task_id)

    def reader(self):
        run, _ = self.source_run(role=self.f.manager, run_id="阅" * 120)
        return self.start(run, name="read_receipts", arguments={"section": "index"})

    def assert_page(self, page, expected, *, has_more=False, reader=None):
        self.assertEqual(page["status"], "ok", page)
        self.assertEqual(page["section"], "index")
        self.assertEqual([row["seq"] for row in page["receipts"]], [e.seq for e in expected])
        self.assertIs(page["has_more"], has_more)
        self.assertEqual(page["next_before_seq"], expected[0].seq if has_more else None)
        record = {"kind": "persisted_events", "reexecutes_operations": False}
        if reader is not None:
            record.update(reader_run_id=reader.payload["run_id"],
                          reader_message_seq=reader.payload["message_seq"], reader_started_seq=reader.seq)
        self.assertEqual(page["record_read"], record)
        self.assertLessEqual(len(json_utf8(page)), 8000, "Budget includes record_read and UTF-8")
        for row, event in zip(page["receipts"], expected):
            self.assertEqual(row["source_hash"], event.hash)
            self.assertEqual(row["started_seq"], event.payload["started_seq"])
            self.assertEqual(row["read_args"], {"receipt_seq": event.seq})
            for key in ("run_id", "role", "name"):
                self.assertEqual(row[key], event.payload[key])

    @staticmethod
    def capture(text):
        capture = _Capture()
        capture.drain(io.BytesIO(text.encode("utf-8")))
        return capture.summary(record_output=True)

    def newshape(self, **overrides):
        return {"status": "ok", "outcome": "passed", "exit_code": 0,
                "complete": True, "truncated": False, "timed_out": False,
                "command": "offline-fixture", "duration_ms": 12,
                "stdout": self.capture("BODY_ONLY 中文\r\n"),
                "stderr": self.capture("Ran 2 tests in 0.001s\n\nOK\n"), **overrides}

    def test_count_pages_keep_every_receipt_once_and_exclude_recursive_reads(self):
        run, _ = self.source_run()
        expected = []
        for _ in range(8):
            expected.append(self.finish(self.start(run)))
            recursive = self.finish(self.start(run, name="read_receipts"), {"status": "ok", "receipts": []})
        reader = self.reader()
        seen, cursor = [], None
        for end in (8, 5, 2):
            filters = {"limit": 3}
            if cursor is not None:
                filters["before_seq"] = cursor
            page = self.index(reader=reader, **filters)
            self.assert_page(page, expected[max(0, end - 3):end], has_more=end > 3, reader=reader)
            seen.extend(row["seq"] for row in page["receipts"])
            cursor = page["next_before_seq"]
        self.assertEqual(sorted(seen), [e.seq for e in expected])
        self.assertEqual(len(set(seen)), len(seen))
        self.assert_page(self.index(before_seq=expected[0].seq), [])
        self.assert_page(self.index(name="read_receipts"), [])
        self.assertEqual(self.query({"receipt_seq": recursive.seq})["receipts"], [])

    def test_utf8_byte_pages_include_full_reader_provenance_without_skips(self):
        run, _ = self.source_run(run_id="source-" + "x" * 120)
        result = self.newshape(code="码" * 40, command="令" * 40)
        expected = [self.finish(self.start(run), result) for _ in range(23)]
        reader = self.reader()
        seen, cursor, end, sizes = [], None, len(expected), []
        for _ in range(len(expected) + 1):
            filters = {"limit": 50}
            if cursor is not None:
                filters["before_seq"] = cursor
            page = self.index(reader=reader, **filters)
            count = len(page.get("receipts", []))
            self.assertGreater(count, 0, page)
            self.assertLessEqual(count, end)
            self.assert_page(page, expected[end - count:end], has_more=end > count, reader=reader)
            sizes.append(len(json_utf8(page)))
            seen.extend(row["seq"] for row in page["receipts"])
            if end > count:
                # The omitted next row really exceeds the byte budget, not the count limit.
                older = expected[end - count - 1]
                older_page = self.index(reader=reader, before_seq=page["receipts"][0]["seq"], limit=1)
                self.assert_page(older_page, [older], has_more=end - count > 1, reader=reader)
                candidate = {**page, "receipts": [older_page["receipts"][0], *page["receipts"]],
                             "next_before_seq": older.seq, "has_more": True}
                self.assertGreater(len(json_utf8(candidate)), 8000)
            end -= count
            cursor = page["next_before_seq"]
            if cursor is None:
                break
        self.assertEqual(end, 0)
        self.assertGreater(len(sizes), 1, "Fixture must exercise the byte limit")
        self.assertEqual(sorted(seen), [e.seq for e in expected])
        self.assertEqual(len(seen), len(set(seen)))

    def test_and_filters_apply_before_limit_and_exclusive_cursor(self):
        run, message = self.source_run()
        one = self.finish(self.start(run))
        self.finish(self.start(run, name="workspace_write"))
        two = self.finish(self.start(run))
        boundary = self.finish(self.start(run))
        other, other_message = self.source_run(role=self.f.manager)
        for _ in range(4):
            self.finish(self.start(other))
        filters = {"run_id": run.payload["run_id"], "role": self.f.worker,
                   "message_seq": message.seq, "name": "workspace_run", "before_seq": boundary.seq, "limit": 1}
        self.assert_page(self.index(**filters), [two], has_more=True)
        self.assert_page(self.index(**{**filters, "before_seq": two.seq}), [one])
        for key, value in (("role", self.f.manager), ("message_seq", other_message.seq),
                           ("name", "publish_artifact"), ("run_id", other.payload["run_id"])):
            with self.subTest(filter=key):
                self.assert_page(self.index(**{**filters, key: value}), [])
        # message_seq is joined from starts; it is absent on production finishes.
        self.assertNotIn("message_seq", two.payload)
        self.assertEqual(self.index(**filters)["receipts"][0]["message_seq"], message.seq)

    def test_status_timeout_unknown_and_missing_fields_are_not_rewritten(self):
        run, _ = self.source_run()
        summaries = [
            {"status": "ok", "outcome": "passed", "exit_code": 0, "complete": True, "timed_out": False},
            {"status": "error", "outcome": "failed", "exit_code": 7, "code": "command_failed"},
            {"status": "error", "outcome": "timeout", "exit_code": None, "timed_out": True,
             "complete": False, "truncated": True},
            {"status": "error", "outcome": "effects_unknown", "code": "effects_unknown", "complete": False},
            {"status": "error"},
        ]
        expected = [self.finish(self.start(run), {**summary, "error": {"message": "ERROR_BODY_ONLY"}})
                    for summary in summaries]
        page = self.index()
        self.assert_page(page, expected)
        self.assertEqual([r["result_summary"] for r in page["receipts"]], summaries)
        self.assertNotIn("ERROR_BODY_ONLY", json.dumps(page))
        for row, event in zip(page["receipts"], expected):
            exact = self.query(row["read_args"])
            self.assertEqual(json_utf8(exact["receipts"][0]["result"]), self.stored_result_bytes(event))

    def test_index_omits_bodies_and_exact_newshape_keeps_bytes_and_source_identity(self):
        run, message = self.source_run()
        arguments = {"argv": ["python", "ARGV_ONLY"], "script": "SCRIPT_ONLY"}
        original = self.newshape(argv=arguments["argv"], script=arguments["script"],
                                 content="CONTENT_ONLY 中文\r\n" * 1000,
                                 error={"message": "ERROR_ONLY"}, metadata={"source_hash": "FORGED_HASH"})
        finish = self.finish(self.start(run, arguments=arguments), original)
        before = self.log.head_hash()
        persisted = self.stored_result_bytes(finish)
        page = self.index()
        self.assert_page(page, [finish])
        serialized = json.dumps(page, ensure_ascii=False)
        for marker in ("ARGV_ONLY", "SCRIPT_ONLY", "CONTENT_ONLY", "ERROR_ONLY", "BODY_ONLY", "FORGED_HASH",
                       "Ran 2 tests"):
            self.assertNotIn(marker, serialized)
        row = page["receipts"][0]
        self.assertNotIn("argv", row["result_summary"])
        self.assertEqual(row["message_seq"], message.seq)
        exact = self.query(row["read_args"])
        saved = exact["receipts"][0]
        self.assertGreater(len(json_utf8(saved["result"])), 8000)
        self.assertEqual(json_utf8(saved["result"]), persisted)
        self.assertEqual(saved["result"], original)
        for stream in ("stdout", "stderr"):
            self.assertEqual(saved["result"][stream]["excerpt"].encode("utf-8"),
                             original[stream]["excerpt"].encode("utf-8"))
        self.assertEqual(saved["seq"], finish.seq)
        observation, = saved["observations"]
        self.assertEqual(observation["source_seq"], finish.seq)
        self.assertEqual(observation["source_hash"], finish.hash)
        self.assertEqual(observation["reported_tests"], 2)
        self.assertEqual(self.log.head_hash(), before, "Reading must not execute or rewrite the source")

    def test_metadata_path_never_loads_event_bodies_or_whole_log(self):
        run, _ = self.source_run()
        finish = self.finish(self.start(run, arguments={"script": "PRIVATE_SCRIPT" * 10000}),
                             self.newshape(content="PRIVATE_BODY" * 20000, code="x" * 10000))
        original_loads = json.loads

        def bounded_loads(value, *args, **kwargs):
            text = value.decode("utf-8") if isinstance(value, bytes) else value
            self.assertNotIn("PRIVATE_BODY", text)
            self.assertNotIn("PRIVATE_SCRIPT", text)
            self.assertLess(len(text), 8192)
            return original_loads(value, *args, **kwargs)

        with patch.object(self.log, "read", side_effect=AssertionError("Whole log read")), \
             patch.object(self.log, "_row_to_event", side_effect=AssertionError("Full event decode")), \
             patch.object(json, "loads", side_effect=bounded_loads):
            page = self.index()
        self.assert_page(page, [finish])
        self.assertNotIn("code", page["receipts"][0]["result_summary"])

    def test_task_scope_and_host_identity_even_with_colliding_run_ids(self):
        local, _ = self.source_run(run_id="shared-index-run")
        visible = self.finish(self.start(local))
        untrusted = Actor.agent(self.f.worker, "worker")
        agent_finish = self.finish(self.start(local), actor=untrusted)
        agent_start = self.finish(self.start(local, actor=untrusted))
        other = self.rt.create_task("Private index task")
        self.rt.team.enable(other)
        foreign, foreign_message = self.source_run(task_id=other, run_id="shared-index-run")
        hidden = self.finish(self.start(foreign), {"status": "error", "stderr": "FOREIGN_BODY_ONLY"})
        wrong_task = self.finish(self.start(foreign), task=self.task)
        for filters in ({}, {"run_id": "shared-index-run"}, {"role": self.f.worker}, {"name": "workspace_run"}):
            self.assert_page(self.index(**filters), [visible])
        self.assert_page(self.index(message_seq=foreign_message.seq), [])
        for event in (agent_finish, agent_start, hidden, wrong_task):
            self.assertEqual(self.query({"receipt_seq": event.seq})["receipts"], [])

    def test_mismatched_and_ambiguous_run_bindings_are_hidden_from_index_and_exact(self):
        run, message = self.source_run()
        visible = self.finish(self.start(run))
        hidden = []
        for key, value in (("run_id", "wrong-run"), ("role", self.f.manager),
                           ("name", "workspace_write"), ("call_id", "wrong-call")):
            hidden.append(self.finish(self.start(run), **{key: value}))
        hidden.append(self.finish(self.start(run, message_seq=message.seq + 100000)))
        hidden.append(self.finish(self.start(run, run_id="never-started")))
        late_start = self.start(run)
        self.f.close_fixture_run()
        hidden.append(self.finish(late_start))
        duplicate, _ = self.source_run()
        hidden.append(self.finish(self.start(duplicate)))
        self.emit("team.run_started", dict(duplicate.payload))
        self.assert_page(self.index(), [visible])
        for event in hidden:
            with self.subTest(seq=event.seq):
                self.assertEqual(self.query({"receipt_seq": event.seq})["receipts"], [])

    def test_deep_duplicate_and_nul_keys_match_exact_ambiguity_rules(self):
        run, _ = self.source_run()
        valid = self.finish(self.start(run), {"status": "ok", "body": [
            {"same": 1}, {"same": 2}, {"text": '{"same":1,"same":2}', "nul_value": "\0"}]})
        raw_bodies = [
            '{"nested":[{"key":1,"key":2}]}',
            '{"nested":[{"key":1,"k\\u0065y":2}]}',
            '{"nested":[{"bad\\u0000key":1}]}',
            '{"nested":[{"deep":{"key":1,"key":2}}]}',
        ]
        invalid = [self.finish(self.start(run), {"status": "ok", "body": "RAW_BODY_PLACEHOLDER"})
                   for _ in raw_bodies]
        # Parser-only SQLite copy: preserve the append-only fixture and its source hashes.
        original = self.log.db
        shadow = sqlite3.connect(":memory:")
        shadow.row_factory = sqlite3.Row
        self.addCleanup(shadow.close)
        schema = original.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='events'").fetchone()[0]
        shadow.execute(schema)
        cursor = original.execute("SELECT * FROM events")
        shadow.executemany("INSERT INTO events VALUES (" + ",".join("?" for _ in cursor.description) + ")", cursor)
        for event, raw in zip(invalid, raw_bodies):
            payload = json.dumps(event.payload).replace('"RAW_BODY_PLACEHOLDER"', raw)
            shadow.execute("UPDATE events SET payload_json=? WHERE seq=?", (payload, event.seq))
        shadow.commit()
        with patch.object(self.log, "db", shadow):
            self.assert_page(self.index(limit=1), [valid])
            self.assertEqual(self.query({"receipt_seq": valid.seq})["receipts"][0]["result"], valid.payload["result"])
            for event in invalid:
                with self.subTest(seq=event.seq):
                    self.assertEqual(self.query({"receipt_seq": event.seq})["receipts"], [])
        self.assertTrue(self.rt.verify_chain().ok)

    def test_invalid_index_combinations_return_correctable_errors(self):
        run, _ = self.source_run()
        finish = self.finish(self.start(run))
        invalid = [
            ({"receipt_seq": finish.seq}, ["section", "receipt_seq"]),
            ({"include_pending": True}, ["section", "include_pending"]),
            ({"include_pending": False}, ["section", "include_pending"]),
            ({"receipt_seq": finish.seq, "include_pending": False}, ["section", "receipt_seq", "include_pending"]),
            ({"file_path": "PRIVATE_INPUT.txt"}, ["file_path", "section"]),
            ({"limit": True}, ["limit"]), ({"limit": 0}, ["limit"]), ({"limit": 51}, ["limit"]),
            ({"before_seq": False}, ["before_seq"]), ({"message_seq": "1"}, ["message_seq"]),
            ({"role": "PRIVATE_INPUT"}, ["role"]), ({"name": []}, ["name"]),
        ]
        for filters, fields in invalid:
            with self.subTest(filters=filters):
                result = self.index(**filters)
                self.assertEqual(result["status"], "error")
                self.assertEqual(result["code"], "invalid_receipt_query")
                self.assertEqual(result["fields"], fields)
                self.assertIs(result["complete"], True)
                self.assertEqual(result["outcome"], "not_started")
                self.assertTrue(result["message"])
                self.assertNotIn("PRIVATE_INPUT", json.dumps(result))
        unknown = self.index(run_id="absent-run")
        self.assertEqual(unknown["status"], "error")
        self.assertIn("role", unknown["message"])
        self.assertIn("message_seq", unknown["message"])
        self.assert_page(self.index(), [finish])
        self.assertEqual(self.query({"receipt_seq": finish.seq})["receipts"][0]["result"], finish.payload["result"])

    def test_default_and_explicit_receipts_and_exact_keep_original_semantics(self):
        run, _ = self.source_run()
        body = {"status": "error", "stdout": "LEGACY_FULL_BODY" * 1000, "exit_code": 7}
        expected = [self.finish(self.start(run), copy.deepcopy(body)) for _ in range(21)]
        default = self.query()
        explicit = self.query({"section": "receipts"})
        self.assertEqual(default, explicit)
        self.assertEqual([r["seq"] for r in default["receipts"]], [e.seq for e in expected[-20:]])
        self.assertTrue(default["has_more"])
        self.assertEqual(default["next_before_seq"], expected[1].seq)
        self.assertNotIn("section", default)
        self.assertTrue(all(r["result"] == body for r in default["receipts"]))
        maximum = self.query({"limit": 50})
        self.assertEqual([r["seq"] for r in maximum["receipts"]], [e.seq for e in expected])
        self.assertFalse(maximum["has_more"])
        self.assertIsNone(maximum["next_before_seq"])
        exact = self.query({"receipt_seq": expected[0].seq, "include_pending": False})
        self.assertEqual([r["seq"] for r in exact["receipts"]], [expected[0].seq])
        self.assertEqual(json_utf8(exact["receipts"][0]["result"]), self.stored_result_bytes(expected[0]))
        self.assertFalse(exact["has_more"])
        self.assertIsNone(exact["next_before_seq"])
        for arguments in ({"receipt_seq": expected[0].seq, "before_seq": expected[0].seq},
                          {"receipt_seq": expected[0].seq, "name": "workspace_write"},
                          {"receipt_seq": 2 ** 63}):
            self.assertEqual(self.query(arguments)["receipts"], [])


if __name__ == "__main__":
    unittest.main()
