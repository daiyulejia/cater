"""Independent receipt boundaries; disposable existing fixtures, no model turns."""

from __future__ import annotations

from contextlib import contextmanager
import copy
import hashlib
import io
import json
import sqlite3
import unittest
from unittest.mock import patch

from cater.events import Actor
from cater.team import HOST
from cater.team_read_sources import bound_receipts
from cater.verification import _Capture
from cater.workspace_tools import WorkspaceExecutor
import test_team_receipt_lookup as lookups


class ReceiptSourceBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.f = lookups.TeamReceiptLookupTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.project = self.f.bind_workspace()
        self.rt, self.task = self.f.rt, self.f.task
        self.team, self.log = self.rt.team, self.rt.log
        self.binding = self.rt.workspaces.view(self.task)
        self.role = self.team._role(self.f.reviewer if hasattr(self.f, "reviewer") else self.f.worker)
        self.work = self.team.view(self.task)
        self._counter = 0
        blocked = patch.object(self.f.driver, "tool_turn", side_effect=AssertionError("Model entry is forbidden"))
        self.model = blocked.start()
        self.addCleanup(blocked.stop)
        self.addCleanup(self.model.assert_not_called)

    def emit(self, kind, payload, *, task=None, actor=HOST):
        return self.log.emit(type=kind, actor=actor, task_id=task or self.task, payload=payload)

    @staticmethod
    def capture(text):
        output = _Capture()
        output.drain(io.BytesIO(text.encode("utf-8")))
        return output.summary(record_output=True)

    def command_result(self):
        return {"status": "ok", "outcome": "passed", "exit_code": 0,
                "complete": True, "truncated": False, "timed_out": False,
                "stdout": self.capture(""),
                "stderr": self.capture("Ran 2 tests in 0.001s\n\nOK\n")}

    def source(self):
        self._counter += 1
        worker = self.team._role(self.f.worker)
        message = self.team.receive(self.task, f"@{worker.name} Boundary source input",
                                    request_id=f"boundary-source-{self._counter}")
        self.assertEqual(message.actor, Actor.human())
        self.assertEqual(message.payload["recipient"], self.f.worker)
        return message

    def start(self, run, *, name="workspace_run", arguments=None):
        self._counter += 1
        return self.emit("team.tool_started", {
            "run_id": run.payload["run_id"], "role": run.payload["role"],
            "message_seq": run.payload["message_seq"], "name": name,
            "call_id": f"boundary-call-{self._counter}", "root": self.binding["root"],
            "revision": self.work["revision"], "arguments": arguments or {}})

    def finish(self, start, result):
        return self.emit("team.tool_finished", {
            "started_seq": start.seq, "run_id": start.payload["run_id"],
            "role": start.payload["role"], "name": start.payload["name"],
            "call_id": start.payload["call_id"], "result": result})

    def command(self, *, late=False, duplicate=None):
        run, _ = self.f.receipt_run(message=self.source())
        if duplicate == "before":
            self.emit("team.run_started", dict(run.payload))
        start = self.start(run)
        if late:
            self.f.close_fixture_run()
        finish = self.finish(start, self.command_result())
        self.f.close_fixture_run()
        if duplicate == "after":
            self.emit("team.run_started", dict(run.payload))
        return start, finish

    def file_read(self, path="observed.txt"):
        (self.project / path).write_text("BOUNDARY_OBSERVED_BODY\n", encoding="utf-8")
        executor = WorkspaceExecutor(self.rt.workspaces.config[self.binding["workspace_id"]]["policy"])
        result = executor.execute("workspace_read", {"path": path})
        self.assertEqual(result["status"], "ok", result)
        run, _ = self.f.receipt_run(message=self.source())
        start = self.start(run, name="workspace_read", arguments={"path": path})
        finish = self.finish(start, result)
        self.f.close_fixture_run()
        return start, finish, result

    def unrelated(self):
        local_marker, foreign_marker = "BOUNDARY_UNRELATED_LOCAL_BODY", "BOUNDARY_FOREIGN_BODY"
        local = self.emit("deliverable.published", {"filename": "unrelated.txt", "format": "text",
            "summary": "Unrelated publication", "content": local_marker * 3000,
            "content_sha256": hashlib.sha256((local_marker * 3000).encode()).hexdigest()},
            actor=Actor.agent(self.f.worker, self.f.worker))
        foreign_task = self.rt.create_task("Boundary foreign task")
        foreign = self.emit("team.tool_finished", {"started_seq": 999999, "run_id": "foreign-run",
            "role": self.f.worker, "name": "workspace_run",
            "result": {"status": "ok", "stdout": foreign_marker * 3000}}, task=foreign_task)
        return {local.seq, foreign.seq}, (local_marker, foreign_marker)

    def query(self, arguments):
        # Isolate the new reader from the unchanged work-status preamble.
        with patch.object(self.team, "view", return_value=self.work):
            return self.team._execute(self.task, self.role, "read_receipts", arguments,
                                      None, self.binding, None, None)

    def metadata_rows(self, response):
        self.assertEqual(response["status"], "ok", response)
        key = next((key for key in ("observations", "file_reads", "receipts") if key in response), None)
        self.assertIsNotNone(key, response)
        self.assertIsInstance(response[key], list)
        return response[key]

    def assert_file(self, response, finish):
        self.assertEqual([row.get("receipt_seq", row.get("seq")) for row in self.metadata_rows(response)],
                         [finish.seq], response)
        self.assertNotIn("BOUNDARY_OBSERVED_BODY", json.dumps(response))

    @contextmanager
    def decoded(self, markers):
        events, bodies = [], []
        original_event, original_loads = self.log._row_to_event, json.loads

        def event(row):
            events.append(row["seq"])
            return original_event(row)

        def loads(value, *args, **kwargs):
            text = value if isinstance(value, str) else bytes(value).decode("utf-8", errors="replace")
            bodies.extend(marker for marker in markers if marker in text)
            return original_loads(value, *args, **kwargs)

        with patch.object(self.log, "_row_to_event", side_effect=event), patch.object(json, "loads", side_effect=loads):
            yield events, bodies

    def strict_seqs(self):
        events = list(self.log.iter_events(task_id=self.task))
        return {event.seq for event, _start in bound_receipts(self.team, self.task, events)}

    def receipt(self, finish):
        return next(row for row in self.team._receipts(self.task) if row["seq"] == finish.seq)

    def raw_payload(self, event, text):
        self.shadow_log()
        self.log.db.execute("UPDATE events SET payload_json = ? WHERE seq = ?", (text, event.seq))
        self.log.db.commit()

    def shadow_log(self):
        if getattr(self, "_shadow", None) is not None:
            return
        # Corrupt a parser-only in-memory copy; the real fixture log stays append-only.
        original = self.log.db
        shadow = sqlite3.connect(":memory:")
        shadow.row_factory = sqlite3.Row
        schema = original.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='events'").fetchone()[0]
        shadow.execute(schema)
        cursor = original.execute("SELECT * FROM events")
        columns = len(cursor.description)
        shadow.executemany("INSERT INTO events VALUES (" + ",".join("?" for _ in range(columns)) + ")", cursor)
        shadow.commit()
        self._shadow = shadow
        self.log.db = shadow

        def restore():
            self.log.db = original
            shadow.close()

        self.addCleanup(restore)

    @staticmethod
    def duplicate_member(value, key, first):
        items = [(key, first), *value.items()]
        return "{" + ",".join(json.dumps(name) + ":" + json.dumps(item) for name, item in items) + "}"

    def test_fixture_valid_chain_is_strict_and_derives_without_model(self):
        _start, finish = self.command()
        before = self.log.head_hash()
        self.assertIn(finish.seq, self.strict_seqs())
        row = self.receipt(finish)
        self.assertEqual(row["observations"][0]["source_seq"], finish.seq)
        self.assertEqual(row["observations"][0]["reported_tests"], 2)
        self.assertEqual(row["result"], finish.payload["result"])
        self.assertEqual(self.log.head_hash(), before)

    def test_exact_receipt_does_not_call_full_log_read(self):
        _start, finish = self.command()
        self.unrelated()
        with patch.object(self.log, "read", wraps=self.log.read) as read:
            response = self.query({"receipt_seq": finish.seq})
        self.assertEqual([row["seq"] for row in response["receipts"]], [finish.seq])
        self.assertEqual(response["receipts"][0]["result"], finish.payload["result"])
        self.assertEqual(read.call_args_list, [], "Exact lookup must not materialize a task/global log")

    def test_exact_receipt_does_not_decode_unrelated_events_or_bodies(self):
        _start, finish = self.command()
        unrelated, markers = self.unrelated()
        with self.decoded(markers) as (events, bodies):
            response = self.query({"receipt_seq": finish.seq})
        self.assertEqual([row["seq"] for row in response["receipts"]], [finish.seq])
        self.assertEqual(unrelated.intersection(events), set(), "Unrelated events were decoded")
        self.assertEqual(bodies, [], "Unrelated JSON bodies were decoded outside the event adapter")

    def test_missing_and_foreign_exact_selectors_do_not_decode_other_bodies(self):
        self.command()
        _unrelated, markers = self.unrelated()
        foreign = next(e for e in reversed(list(self.log.iter_events())) if e.task_id != self.task
                       and e.type == "team.tool_finished")
        for seq in (self.log.count() + 1000, foreign.seq):
            with self.subTest(receipt_seq=seq), self.decoded(markers) as (_events, bodies):
                response = self.query({"receipt_seq": seq})
                self.assertEqual(response["receipts"], [])
                self.assertEqual(bodies, [], "A rejected reference decoded unrelated bodies")

    def test_metadata_reader_does_not_call_full_log_read(self):
        _start, finish, _result = self.file_read()
        with patch.object(self.log, "read", wraps=self.log.read) as read:
            response = self.query({"section": "file_reads"})
        self.assert_file(response, finish)
        self.assertEqual(read.call_args_list, [], "Metadata lookup must not materialize task/global history")

    def test_metadata_reader_does_not_use_workspace_pending(self):
        _start, finish, _result = self.file_read()
        self.unrelated()
        with patch.object(self.rt.workspaces, "pending", wraps=self.rt.workspaces.pending) as pending:
            response = self.query({"section": "file_reads", "file_path": "observed.txt"})
        self.assert_file(response, finish)
        self.assertEqual(pending.call_args_list, [], "Read-only metadata scope must not invoke the global pending view")

    def test_metadata_reader_does_not_decode_unrelated_local_or_foreign_bodies(self):
        _start, finish, _result = self.file_read()
        unrelated, markers = self.unrelated()
        with self.decoded(markers) as (events, bodies):
            response = self.query({"section": "file_reads"})
        self.assert_file(response, finish)
        self.assertEqual(unrelated.intersection(events), set(), "Metadata read decoded unrelated events")
        self.assertEqual(bodies, [], "Metadata scope or query decoded unrelated JSON bodies")

    def test_late_tool_finish_is_not_strict(self):
        _start, valid = self.command()
        _late_start, late = self.command(late=True)
        strict = self.strict_seqs()
        self.assertIn(valid.seq, strict)
        self.assertNotIn(late.seq, strict, "A finish after run_finished must not gain strict attribution")

    def test_late_tool_finish_does_not_derive_observations(self):
        _start, late = self.command(late=True)
        row = self.receipt(late)
        self.assertEqual(row["result"], late.payload["result"], "Raw legacy receipt must remain readable")
        self.assertFalse(row.get("observations"), "Late settlement must not acquire strict observations")

    def test_duplicate_run_identity_before_tool_is_not_strict(self):
        _start, valid = self.command()
        _duplicate_start, duplicate = self.command(duplicate="before")
        strict = self.strict_seqs()
        self.assertIn(valid.seq, strict)
        self.assertNotIn(duplicate.seq, strict, "Two HOST starts with one run identity are ambiguous")

    def test_duplicate_run_identity_after_finish_is_not_strict(self):
        _start, valid = self.command()
        _duplicate_start, duplicate = self.command(duplicate="after")
        strict = self.strict_seqs()
        self.assertIn(valid.seq, strict)
        self.assertNotIn(duplicate.seq, strict, "An old start must not be guessed when identity is duplicated")

    def test_duplicate_run_identity_does_not_derive_observations(self):
        for timing in ("before", "after"):
            with self.subTest(duplicate=timing):
                _start, duplicate = self.command(duplicate=timing)
                row = self.receipt(duplicate)
                self.assertEqual(row["result"], duplicate.payload["result"])
                self.assertFalse(row.get("observations"), "Ambiguous identity must not acquire strict observations")

    def test_metadata_numeric_fields_keep_bool_float_null_distinct(self):
        start, finish, result = self.file_read()
        original_start = copy.deepcopy(start.payload)
        original_finish = copy.deepcopy(finish.payload)
        cases = [("result", "bytes", True), ("result", "bytes", float(result["bytes"])),
                 ("result", "offset", False), ("result", "offset", 0.0),
                 ("result", "returned_bytes", float(result["returned_bytes"])),
                 ("result", "truncated", 0), ("result", "truncated", None),
                 ("arguments", "offset", None), ("arguments", "offset", False),
                 ("arguments", "limit", None), ("arguments", "limit", True)]
        for location, key, value in cases:
            with self.subTest(location=location, key=key, value=value):
                s, p = copy.deepcopy(original_start), copy.deepcopy(original_finish)
                (p["result"] if location == "result" else s["arguments"])[key] = value
                self.raw_payload(start, json.dumps(s))
                self.raw_payload(finish, json.dumps(p))
                self.assertEqual(self.metadata_rows(self.query({"section": "file_reads"})), [])

    def test_exact_and_source_sequences_do_not_coerce_types(self):
        start, finish = self.command()
        for value in (True, None, float(finish.seq)):
            with self.subTest(selector=value), patch.object(self.team.read_sources, "exact") as exact:
                result = self.query({"receipt_seq": value})
                self.assertEqual(result["status"], "error", result)
                self.assertEqual(result["code"], "invalid_receipt_query", result)
                self.assertEqual(result["fields"], ["receipt_seq"])
                self.assertIs(result["complete"], True)
                self.assertEqual(result["outcome"], "not_started")
                self.assertNotIn("receipts", result)
                exact.assert_not_called()
        for event, key, value in ((start, "message_seq", float(start.payload["message_seq"])),
                                  (finish, "started_seq", float(start.seq)),
                                  (finish, "started_seq", True), (finish, "started_seq", None)):
            with self.subTest(event=event.seq, key=key, value=value):
                self.raw_payload(start, json.dumps(start.payload))
                self.raw_payload(finish, json.dumps(finish.payload))
                self.raw_payload(event, json.dumps({**event.payload, key: value}))
                self.assertEqual(self.query({"receipt_seq": finish.seq})["receipts"], [])

    def test_metadata_rejects_duplicate_and_nul_nested_result_keys(self):
        _start, finish, result = self.file_read()
        variants = [self.duplicate_member(result, "status", "error"),
                    self.duplicate_member(result, "bytes", result["bytes"] + 1),
                    json.dumps({**result, "status\x00extra": "ok"}),
                    json.dumps({**result, "path\x00extra": "observed.txt"})]
        for nested in variants:
            with self.subTest(nested=nested):
                payload = {key: value for key, value in finish.payload.items() if key != "result"}
                self.raw_payload(finish, json.dumps(payload)[:-1] + ',"result":' + nested + "}")
                self.assertEqual(self.metadata_rows(self.query({"section": "file_reads"})), [])

    def test_metadata_rejects_duplicate_and_nul_nested_argument_keys(self):
        start, finish, _result = self.file_read()
        args = start.payload["arguments"]
        variants = [self.duplicate_member(args, "path", "different.txt"),
                    json.dumps({**args, "path\x00extra": "observed.txt"})]
        for nested in variants:
            with self.subTest(nested=nested):
                payload = {key: value for key, value in start.payload.items() if key != "arguments"}
                self.raw_payload(start, json.dumps(payload)[:-1] + ',"arguments":' + nested + "}")
                self.assertEqual(self.metadata_rows(self.query({"section": "file_reads"})), [])

    def test_exact_rejects_ambiguous_finish_identity(self):
        _start, finish = self.command()
        variants = [self.duplicate_member(finish.payload, "started_seq", 999999),
                    self.duplicate_member(finish.payload, "name", "workspace_write"),
                    json.dumps({**finish.payload, "started_seq\x00extra": finish.payload["started_seq"]})]
        for payload in variants:
            with self.subTest(payload=payload):
                self.raw_payload(finish, payload)
                self.assertEqual(self.query({"receipt_seq": finish.seq})["receipts"], [],
                                 "Ambiguous finish keys must not gain strict exact attribution")

    def test_exact_rejects_ambiguous_nested_result_keys(self):
        _start, finish = self.command()
        result = finish.payload["result"]
        variants = [self.duplicate_member(result, "status", "error"),
                    json.dumps({**result, "status\x00extra": "ok"})]
        for nested in variants:
            with self.subTest(nested=nested):
                payload = {key: value for key, value in finish.payload.items() if key != "result"}
                self.raw_payload(finish, json.dumps(payload)[:-1] + ',"result":' + nested + "}")
                self.assertEqual(self.query({"receipt_seq": finish.seq})["receipts"], [],
                                 "Exact observations must not authenticate ambiguous result objects")

    def test_malformed_unrelated_payload_does_not_poison_selected_queries(self):
        _command_start, command = self.command()
        _read_start, read, _result = self.file_read()
        unrelated, _markers = self.unrelated()
        self.shadow_log()
        for seq in unrelated:
            self.log.db.execute("UPDATE events SET payload_json = ? WHERE seq = ?", ("{", seq))
        self.log.db.commit()
        exact = self.query({"receipt_seq": command.seq})
        self.assertEqual([row["seq"] for row in exact["receipts"]], [command.seq])
        self.assert_file(self.query({"section": "file_reads"}), read)

    def test_malformed_selected_payload_rejects_without_history_fallback(self):
        _start, finish = self.command()
        self.raw_payload(finish, "{")
        with patch.object(self.log, "read", wraps=self.log.read) as read:
            response = self.query({"receipt_seq": finish.seq})
        self.assertEqual(response["receipts"], [])
        self.assertEqual(read.call_args_list, [])


def load_tests(loader, tests, pattern):
    return loader.loadTestsFromTestCase(ReceiptSourceBoundaryTests)


if __name__ == "__main__":
    unittest.main()
