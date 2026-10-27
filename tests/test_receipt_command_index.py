"""Offline command projections; compose the index fixture without inheritance."""

from __future__ import annotations

from contextlib import contextmanager
import json
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from cater.events import Actor
from cater.team_command_index import command_identity
import test_receipt_index as index_fixture


class ReceiptCommandIndexTests(unittest.TestCase):
    def setUp(self):
        self.f = index_fixture.ReceiptIndexTests()
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()
        self.sources = self.f.team.read_sources
        self.run, _ = self.f.source_run()

    def pair(self, arguments=None, result=None, **kwargs):
        start = self.f.start(self.run, arguments=arguments, **kwargs)
        return start, self.f.finish(start, result)

    def identity(self, start, event):
        validated = self.sources._chain(self.f.task, event)
        self.assertIsNotNone(validated)
        self.assertEqual(validated.seq, start.seq)
        return command_identity(self.sources, self.f.task, event, validated)

    @staticmethod
    def altered(event, **changes):
        return SimpleNamespace(**{**vars(event), **changes})

    @contextmanager
    def raw_rows(self, replacements, *, column="payload_json"):
        # Mutate only a parser-test memory copy, never the append-only fixture.
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
            shadow.executemany(f"UPDATE events SET {column}=? WHERE seq=?", replacements)
            shadow.commit()
            with patch.object(self.f.log, "db", shadow):
                yield
        finally:
            shadow.close()

    def test_request_and_actual_argv_are_independent_with_request_provenance(self):
        request = {"command": "python", "args": ["-m", "unittest", "tests.test_one"], "cwd": "src"}
        argv = ["C:/Python/python.exe", "-m", "unittest", "tests.test_one"]
        start, event = self.pair(request, {"status": "ok", "argv": argv})
        self.assertEqual(self.identity(start, event), {
            "request": request, "request_fields_unavailable": [], "stdin_recorded": False,
            "argv": argv, "argv_available": True, "request_hash": start.hash,
            "request_read_args": {"section": "request", "receipt_seq": event.seq}})

    def test_failed_not_started_still_has_request_without_inventing_argv(self):
        request = {"command": "python", "args": ["missing.py"]}
        start, event = self.pair(request, {"status": "error", "outcome": "not_started",
                                          "code": "invalid_cwd"})
        result = self.identity(start, event)
        self.assertEqual(result["request"], request)
        self.assertEqual(result["request_fields_unavailable"], [])
        self.assertIsNone(result["argv"])
        self.assertIs(result["argv_available"], False)
        self.assertNotIn("cwd", result["request"])

    def test_missing_null_and_empty_values_do_not_infer_defaults(self):
        for arguments, result, request, unavailable, stdin, argv, available in (
            ({}, {"status": "ok"}, {}, [], False, None, False),
            ({"command": None, "args": None, "cwd": None, "stdin": None},
             {"status": "error", "argv": None}, {}, ["command", "args", "cwd"], True, None, False),
            ({"command": "", "args": [], "cwd": "", "stdin": ""},
             {"status": "ok", "argv": []}, {"command": "", "args": [], "cwd": ""}, [], True, [], True),
        ):
            with self.subTest(arguments=arguments):
                identity = self.identity(*self.pair(arguments, result))
                self.assertEqual(identity["request"], request)
                self.assertEqual(identity["request_fields_unavailable"], unavailable)
                self.assertIs(identity["stdin_recorded"], stdin)
                self.assertEqual(identity["argv"], argv)
                self.assertIs(identity["argv_available"], available)

    def test_utf8_scalar_and_array_json_limits_are_inclusive(self):
        for extra in (0, 1):
            with self.subTest(extra=extra):
                request = {"command": "界" * 42 + "ab" + "x" * extra,
                           "cwd": "界" * 85 + "a" + "x" * extra,
                           "args": ["界" * 169 + "a" + "x" * extra]}
                self.assertEqual(len(json.dumps(request["args"], ensure_ascii=False).encode()), 512 + extra)
                identity = self.identity(*self.pair(request, {"status": "ok", "argv": request["args"]}))
                self.assertEqual(identity["request"], {} if extra else request)
                self.assertEqual(identity["request_fields_unavailable"], ["command", "args", "cwd"] if extra else [])
                self.assertEqual(identity["argv"], None if extra else request["args"])
                self.assertIs(identity["argv_available"], not extra)

    def test_wrong_scalar_and_array_types_never_become_recorded_null(self):
        for value in (None, True, 3, 1.5, "python", {}, {"args": []}, [1], [None], [True], [[]], [{}], ["ok", 2]):
            with self.subTest(value=value):
                identity = self.identity(*self.pair({"command": [], "cwd": 3, "args": value},
                                                    {"status": "error", "argv": value}))
                self.assertEqual(identity["request"], {})
                self.assertEqual(identity["request_fields_unavailable"], ["command", "args", "cwd"])
                self.assertIsNone(identity["argv"])
                self.assertIs(identity["argv_available"], False)

    def test_bad_unicode_is_unavailable_but_valid_unicode_and_nul_values_survive(self):
        start, event = self.pair({"command": "python"}, {"status": "ok"})
        for text, valid in (("\ud800", False), ("\udfff", False), ("中文😀", True), ("a\0b", True)):
            request = {"command": text, "cwd": text, "args": [text]}
            replacements = [(json.dumps({**start.payload, "arguments": request}), start.seq),
                            (json.dumps({**event.payload, "result": {"status": "ok", "argv": [text]}}), event.seq)]
            with self.subTest(text=repr(text)), self.raw_rows(replacements):
                identity = self.identity(start, event)
                self.assertEqual(identity["request"], request if valid else {})
                self.assertEqual(identity["request_fields_unavailable"], [] if valid else ["command", "args", "cwd"])
                self.assertEqual(identity["argv"], [text] if valid else None)
                self.assertIs(identity["argv_available"], valid)
                json.dumps(identity, ensure_ascii=False).encode("utf-8")

    def test_duplicate_escaped_duplicate_and_nul_keys_are_rejected_at_every_depth(self):
        start, event = self.pair({"command": "python"})
        bad_objects = (
            '{"command":"python","command":"sh"}',
            '{"command":"python","comm\\u0061nd":"sh"}',
            '{"command\\u0000suffix":"python"}',
            '{"command":"python","extra":[{"a":1,"a":2}]}',
            '{"command":"python","extra":[{"bad\\u0000key":1}]}',
        )
        for item, container in ((start, "arguments"), (event, "result")):
            for raw in bad_objects:
                if container == "result":
                    raw = '{"status":"ok","argv":["python"],' + raw[1:]
                payload = json.dumps({**item.payload, container: "RAW_PLACEHOLDER"}).replace('"RAW_PLACEHOLDER"', raw)
                with self.subTest(seq=item.seq, raw=raw), self.raw_rows([(payload, item.seq)]):
                    self.assertIsNone(command_identity(self.sources, self.f.task, event, start))
        # Reused keys in different objects and JSON-looking string values are safe.
        start, event = self.pair({"command": "python", "extra": [{"a": 1}, {"a": 2}],
                                 "script": '{"a":1,"a":2}'})
        self.assertEqual(self.identity(start, event)["request"], {"command": "python"})

    def test_finish_without_optional_call_id_and_recorded_stdin_of_any_type(self):
        for stdin in (None, False, 42, [], {}):
            with self.subTest(stdin=stdin):
                start, event = self.pair({"command": "python", "stdin": stdin})
                payload = {key: value for key, value in event.payload.items() if key != "call_id"}
                without_call_id = self.altered(event, payload=payload)
                with self.raw_rows([(json.dumps(payload), event.seq)]):
                    identity = self.identity(start, without_call_id)
                    self.assertEqual(identity["request"], {"command": "python"})
                    self.assertIs(identity["stdin_recorded"], True)
                    self.assertNotIn("stdin", identity["request"])

    def test_non_commands_and_unvalidated_or_mismatched_pairs_are_rejected(self):
        start, event = self.pair()
        other_start, other_event = self.pair(name="workspace_read")
        self.assertIsNone(command_identity(self.sources, self.f.task, other_event, other_start))
        self.assertIsNone(command_identity(self.sources, self.f.task, event, None))
        impostor = Actor.agent(self.f.f.worker, "worker")
        bad_start, bad_event = self.pair(actor=impostor)
        self.assertIsNone(self.sources._chain(self.f.task, bad_event))
        self.assertIsNone(command_identity(self.sources, self.f.task, bad_event, bad_start))
        for changes in ({"hash": "0" * 64}, {"actor": impostor}, {"seq": True}, {"seq": 2 ** 63},
                        {"task_id": "foreign"}, {"payload": {**event.payload, "started_seq": other_start.seq}}):
            with self.subTest(changes=changes):
                self.assertIsNone(command_identity(self.sources, self.f.task, self.altered(event, **changes), start))
        self.assertIsNone(command_identity(self.sources, self.f.task, event,
                                           self.altered(start, hash="0" * 64)))
        # Forging Python metadata cannot override the stored host, task or pairing.
        with self.raw_rows([(json.dumps(impostor.to_dict()), event.seq)], column="actor_json"):
            self.assertIsNone(command_identity(self.sources, self.f.task, event, start))
        forged = self.altered(event, payload={**event.payload, "started_seq": other_start.seq,
                                            "name": "workspace_run", "call_id": other_start.payload["call_id"]})
        forged_start = self.altered(other_start, payload={**other_start.payload, "name": "workspace_run"})
        self.assertIsNone(command_identity(self.sources, self.f.task, forged, forged_start))
        sibling, _ = self.pair({"command": "PRIVATE_SIBLING"})
        later_start, later = self.pair()
        self.assertLess(sibling.seq, later.seq)
        forged = self.altered(later, payload={**later.payload, "started_seq": sibling.seq,
                                             "call_id": sibling.payload["call_id"]})
        self.assertIsNone(command_identity(self.sources, self.f.task, forged, sibling))
        self.assertIsNotNone(self.identity(later_start, later))

    def test_foreign_task_cannot_be_relabelled_as_local(self):
        other = self.f.rt.create_task("Private command index fixture")
        self.f.team.enable(other)
        run, _ = self.f.source_run(task_id=other, run_id=self.run.payload["run_id"])
        start = self.f.start(run, arguments={"command": "PRIVATE_COMMAND"})
        event = self.f.finish(start, {"status": "ok", "argv": ["PRIVATE_ARGV"]})
        self.assertIsNotNone(self.sources._chain(other, event))
        self.assertIsNone(command_identity(self.sources, self.f.task, event, start))
        self.assertIsNone(command_identity(self.sources, self.f.task,
                                           self.altered(event, task_id=self.f.task),
                                           self.altered(start, task_id=self.f.task)))
        self.assertIsNotNone(command_identity(self.sources, other, event, start))

    def test_twenty_one_large_logs_only_return_bounded_sql_cells(self):
        markers = ("PRIVATE_SCRIPT", "PRIVATE_STDIN", "PRIVATE_LOG")
        pairs = [self.pair({"command": "python", "args": ["-c", markers[0] * 1000],
                            "cwd": ".", "stdin": markers[1] * 1000, "script": markers[0] * 1000},
                           {"status": "error", "argv": ["python", "-c", markers[0] * 1000],
                            "stdout": markers[2] * 1000, "stderr": markers[2] * 1000}) for _ in range(21)]
        before = self.f.log.head_hash()
        validated = [(self.sources._chain(self.f.task, event), event) for _, event in pairs]
        self.assertTrue(all(start is not None for start, _ in validated))
        original_db, original_loads = self.f.log.db, json.loads
        selects = []

        def bounded_loads(value, *args, **kwargs):
            text = value.decode() if isinstance(value, bytes) else value
            self.assertLessEqual(len(text.encode()), 512)
            for marker in markers:
                self.assertNotIn(marker, text)
            return original_loads(value, *args, **kwargs)

        def execute(sql, parameters):
            selects.append(sql)
            self.assertIn("task_id = ?", sql)
            self.assertIn("seq = ?", sql)
            cursor = original_db.execute(sql, parameters)

            def fetchone():
                row = cursor.fetchone()
                self.assertIsNotNone(row)
                for value in row:
                    if isinstance(value, (str, bytes)):
                        blob = value.encode() if isinstance(value, str) else value
                        self.assertLessEqual(len(blob), 512)
                        for marker in markers:
                            self.assertNotIn(marker.encode(), blob)
                return row

            return SimpleNamespace(fetchone=fetchone, close=cursor.close)

        with patch.object(self.f.log, "read", side_effect=AssertionError("Whole log read")), \
             patch.object(self.f.log, "_row_to_event", side_effect=AssertionError("Full event decode")), \
             patch.object(self.sources, "_chain", side_effect=AssertionError("Already validated")), \
             patch.object(self.f.log, "db", SimpleNamespace(execute=execute)), \
             patch.object(json, "loads", side_effect=bounded_loads):
            for start, event in validated:
                result = command_identity(self.sources, self.f.task, event, start)
                self.assertEqual(result["request"], {"command": "python", "cwd": "."})
                self.assertEqual(result["request_fields_unavailable"], ["args"])
                self.assertIs(result["stdin_recorded"], True)
                self.assertIsNone(result["argv"])
                self.assertIs(result["argv_available"], False)
        self.assertEqual(len(selects), 42)
        self.assertEqual(self.f.log.head_hash(), before)
        self.assertTrue(self.f.rt.verify_chain().ok)

    def test_storage_failure_is_reported_by_index_not_hidden_as_missing_identity(self):
        start, event = self.pair({"command": "python"})
        with patch.object(self.f.log, "db", SimpleNamespace(
                execute=lambda *args: (_ for _ in ()).throw(sqlite3.OperationalError("PRIVATE_DB_ERROR")))):
            with self.assertRaises(sqlite3.OperationalError):
                command_identity(self.sources, self.f.task, event, start)
        with patch("cater.team_command_index._project", side_effect=sqlite3.OperationalError("PRIVATE_DB_ERROR")):
            response = self.f.index()
        self.assertEqual(response["status"], "error")
        self.assertEqual(response["code"], "receipt_index_unavailable")
        self.assertNotIn("PRIVATE_DB_ERROR", json.dumps(response))


if __name__ == "__main__":
    unittest.main()
