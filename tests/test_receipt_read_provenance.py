"""Receipt readback provenance with composed, disposable offline fixtures only."""

from __future__ import annotations

from contextlib import contextmanager, ExitStack
import copy
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from cater.events import Actor
from cater.team import HOST
from cater.team_read_sources import NOTE
from cater.workspace_tools import WorkspaceExecutor
import test_file_read_sources as sources
import test_team_external as external
import test_team_receipt_lookup as lookups


class ReceiptReadProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.f = self.fixture(sources.FileReadSourcesTests)
        self.native = self.f.f
        self.rt, self.task = self.native.rt, self.native.task
        self.team, self.log = self.rt.team, self.rt.log
        self.project = self.f.project

    def fixture(self, cls):
        fixture = cls()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def tearDown(self):
        self.assertTrue(self.rt.verify_chain().ok)
        self.assertEqual(self.native.protocol_errors, [])

    def query_events(self, fixture=None):
        fixture = fixture or self.native
        starts = {event.seq: event for event in fixture.events("team.tool_started")}
        return [(starts[event.payload["started_seq"]], event)
                for event in fixture.events("team.tool_finished")
                if event.payload["name"] == "read_receipts"]

    def assert_record_read(self, result, start=None):
        expected = {"kind": "persisted_events", "reexecutes_operations": False}
        if start is not None:
            expected.update(reader_run_id=start.payload["run_id"],
                            reader_message_seq=start.payload["message_seq"],
                            reader_started_seq=start.seq)
        self.assertEqual(result["record_read"], expected)
        self.assertIs(result["record_read"]["reexecutes_operations"], False)

    def query(self, arguments, *, role=None):
        result = self.f.query(arguments, role=role)
        start, finish = self.query_events()[-1]
        self.assertEqual(finish.payload["result"], result)
        self.assert_record_read(result, start)
        return result, start

    def direct(self, arguments, start=None):
        return self.team._execute(self.task, self.team._role(self.native.reviewer),
                                  "read_receipts", arguments, None,
                                  self.rt.workspaces.view(self.task), start, None)

    def reader_start(self):
        role = self.team._role(self.native.reviewer)
        source = self.team.receive(self.task, f"@{role.name} Inspect saved receipts")
        run = self.log.emit(type="team.run_started", task_id=self.task, actor=HOST,
            payload={"run_id": f"provenance-reader-{source.seq}", "role": role.id,
                     "message_seq": source.seq, "revision": self.team.view(self.task)["revision"],
                     "control_seq": self.rt.execution_control.view(self.task)["seq"]})
        return self.log.emit(type="team.tool_started", task_id=self.task, actor=HOST,
            payload={**run.payload, "name": "read_receipts", "call_id": f"reader-{source.seq}",
                     "arguments": {}})

    def snapshot(self):
        return {row["seq"]: tuple(row) for row in self.log.db.execute("SELECT * FROM events")}

    def assert_saved(self, before):
        after = self.snapshot()
        self.assertEqual({seq: after.get(seq) for seq in before}, before)

    def assert_page(self, result, events, *, has_more=False):
        lookups.TeamReceiptLookupTests.assert_page(self, result, events, has_more=has_more)

    def assert_original(self, row, start, finish):
        self.assertEqual(row, {**finish.payload, "seq": finish.seq,
                               "message_seq": start.payload["message_seq"]})
        self.assertNotIn("record_read", row)
        self.assertNotIn("record_read", row["result"])

    def assert_file_state(self, result, state):
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["query_state"], state)
        self.assertIs(result["scope_available"], state != "scope_unavailable")
        self.assertTrue(result["note"].endswith(NOTE))
        if state == "scope_unavailable":
            self.assertIsNone(result["workspace_binding"])
            self.assertIn("\u4e0d\u8868\u793a\u6ca1\u6709\u5386\u53f2\u8bfb\u53d6", result["note"])
        else:
            scope = self.team.read_sources._scope(self.task)
            self.assertEqual(result["workspace_binding"],
                             {key: value for key, value in scope.items() if key != "root"})
            self.assertNotIn("root", result["workspace_binding"])
            if state == "matched_records":
                self.assertTrue(result["observations"])
                self.assertEqual(result["note"], NOTE)
            else:
                self.assertIn("\u4e0d\u8bc1\u660e\u6587\u4ef6\u4ece\u672a\u8bfb\u8fc7", result["note"])
        if state != "matched_records":
            self.assertEqual(result["observations"], [])
            self.assertIs(result["has_more"], False)
            self.assertIsNone(result["next_before_seq"])

    @contextmanager
    def no_workspace_io(self):
        original_open = Path.open
        project = self.project

        def open_path(path, *args, **kwargs):
            if path.is_relative_to(project):
                raise AssertionError(f"Receipt lookup opened a workspace file: {path}")
            return original_open(path, *args, **kwargs)

        with ExitStack() as stack:
            stack.enter_context(patch.object(Path, "open", open_path))
            guards = [stack.enter_context(patch(target,
                side_effect=AssertionError(f"Receipt lookup invoked {target}"))) for target in
                ("os.scandir", "os.walk", "subprocess.Popen")]
            guards.append(stack.enter_context(patch.object(WorkspaceExecutor, "execute",
                side_effect=AssertionError("Receipt lookup reran a workspace operation"))))
            yield
            for guard in guards:
                guard.assert_not_called()

    def test_native_exact_read_keeps_original_run_and_body_in_tool_history(self):
        self.f.put("original.txt", "PERSISTED_ORIGINAL_BODY\n")
        original_start, original_finish, original_result = self.f.read("original.txt")
        saved = self.snapshot()
        self.f.put("original.txt", "CURRENT_DISK_IS_DIFFERENT\n")
        self.f.put("unobserved.txt", "UNOBSERVED_DISK_SECRET\n")
        args = {"receipt_seq": original_finish.seq, "run_id": original_start.payload["run_id"],
                "role": self.native.worker, "message_seq": original_start.payload["message_seq"],
                "name": "workspace_read", "limit": 1}
        readers = []
        with self.no_workspace_io():
            for role in (self.native.reviewer, self.native.worker):
                result, reader = self.query(args, role=role)
                readers.append(reader)
                self.assert_page(result, [original_finish])
                self.assert_original(result["receipts"][0], original_start, original_finish)
                self.assertEqual(result["receipts"][0]["result"], original_result)
                self.assertNotEqual(reader.payload["run_id"], original_start.payload["run_id"])
                self.assertNotEqual(reader.payload["message_seq"], original_start.payload["message_seq"])
                replies = [message for message in self.native.driver.calls[-1]["history"]
                           if message.get("role") == "tool"
                           and message.get("tool_call_id") == reader.payload["call_id"]]
                self.assertEqual(len(replies), 1)
                self.assertEqual(json.loads(replies[0]["content"]), result)
                self.assertNotIn("CURRENT_DISK_IS_DIFFERENT", json.dumps(result))
                self.assertNotIn("UNOBSERVED_DISK_SECRET", json.dumps(result))
        self.assertNotEqual(readers[0].payload["run_id"], readers[1].payload["run_id"])
        self.assertEqual(len(self.native.results("workspace_read")), 1)
        self.assert_saved(saved)

    def test_receipt_pagination_preserves_saved_rows_and_reader_identity(self):
        self.f.put("pages.txt", "PERSISTED_PAGE_BODY\n")
        reads = [self.f.read("pages.txt", offset=index, limit=6) for index in range(5)]
        saved = self.snapshot()
        cursor, seen = None, []
        with self.no_workspace_io():
            for expected, more in ((reads[3:], True), (reads[1:3], True), (reads[:1], False)):
                args = {"name": "workspace_read", "role": self.native.worker, "limit": 2}
                if cursor is not None:
                    args["before_seq"] = cursor
                result, reader = self.query(args, role=self.native.reviewer)
                self.assert_page(result, [read[1] for read in expected], has_more=more)
                for row, (start, finish, _) in zip(result["receipts"], expected):
                    self.assert_original(row, start, finish)
                    self.assertNotEqual(row["run_id"], reader.payload["run_id"])
                seen.extend(row["seq"] for row in result["receipts"])
                cursor = result["next_before_seq"]
        self.assertEqual(sorted(seen), [read[1].seq for read in reads])
        self.assertEqual(len(seen), len(set(seen)))
        self.assert_saved(saved)

    def test_file_metadata_pagination_preserves_observation_windows_and_provenance(self):
        self.f.put("windows.txt", "OBSERVED_WINDOW_BODY\n" * 8)
        reads = [self.f.read("windows.txt", offset=index, limit=7) for index in range(5)]
        saved = self.snapshot()
        self.f.put("windows.txt", "UNOBSERVED_CURRENT_BODY\n")
        cursor, seen = None, []
        with self.no_workspace_io():
            for expected, more in ((reads[3:], True), (reads[1:3], True), (reads[:1], False)):
                args = {"section": "file_reads", "file_path": "windows.txt", "limit": 2}
                if cursor is not None:
                    args["before_seq"] = cursor
                result, reader = self.query(args, role=self.native.reviewer)
                self.assert_file_state(result, "matched_records")
                self.assertEqual([row["seq"] for row in result["observations"]],
                                 [read[1].seq for read in expected])
                self.assertIs(result["has_more"], more)
                self.assertEqual(result["next_before_seq"], expected[0][1].seq if more else None)
                for row, (start, finish, body) in zip(result["observations"], expected):
                    for key in ("run_id", "message_seq", "role", "revision"):
                        self.assertEqual(row[key], start.payload[key])
                    for key in ("path", "sha256", "bytes", "offset", "returned_bytes", "truncated"):
                        self.assertEqual(row[key], body[key])
                    self.assertEqual(row["started_seq"], start.seq)
                    self.assertEqual(row["read_args"], {"receipt_seq": finish.seq})
                    self.assertIs(row["content_complete"], False)
                    self.assertNotIn("content", row)
                    self.assertNotIn("record_read", row)
                    self.assertNotEqual(row["run_id"], reader.payload["run_id"])
                seen.extend(row["seq"] for row in result["observations"])
                cursor = result["next_before_seq"]
        self.assertEqual(sorted(seen), [read[1].seq for read in reads])
        self.assertEqual(len(seen), len(set(seen)))
        self.assert_saved(saved)

    def test_current_run_earlier_receipt_stays_an_observation_not_a_new_execution(self):
        self.f.put("same-run.txt", "CURRENT_RUN_OBSERVED_BODY\n")
        body, result = self.f.exchange(("workspace_read", {"path": "same-run.txt"}),
                                      ("read_receipts", {"name": "workspace_read"}))
        reader, finish = self.query_events()[-1]
        original_finish, = [event for event in self.native.events("team.tool_finished")
                            if event.payload["name"] == "workspace_read"]
        original_start = next(event for event in self.native.events("team.tool_started")
                              if event.seq == original_finish.payload["started_seq"])
        self.assert_record_read(result, reader)
        self.assert_page(result, [original_finish])
        self.assert_original(result["receipts"][0], original_start, original_finish)
        self.assertEqual(result["receipts"][0]["result"], body)
        self.assertEqual(original_start.payload["run_id"], reader.payload["run_id"])
        self.assertEqual(original_start.payload["message_seq"], reader.payload["message_seq"])
        self.assertLess(original_finish.seq, reader.seq)
        self.assertEqual(finish.payload["result"], result)
        self.assertEqual(len(self.native.results("workspace_read")), 1)

    def test_unknown_pending_effects_remain_pending_with_independent_pagination(self):
        orphan, disk_body = self.native.orphan_write(self.project)
        pending = [orphan]
        for index in range(4):
            pending.append(self.log.emit(type="team.tool_started", task_id=self.task, actor=HOST,
                payload={**orphan.payload, "call_id": f"unresolved-provenance-{index}"}))
        completed = self.log.emit(type="team.tool_started", task_id=self.task, actor=HOST,
            payload={**orphan.payload, "call_id": "completed-provenance"})
        receipt = self.log.emit(type="team.tool_finished", task_id=self.task, actor=HOST,
            payload={key: completed.payload[key] for key in ("run_id", "role", "name", "call_id")}
                    | {"started_seq": completed.seq, "result": {"status": "error", "exit_code": 7}})
        reader = self.reader_start()
        args = {"name": "workspace_write", "role": self.native.manager,
                "run_id": orphan.payload["run_id"], "message_seq": orphan.payload["message_seq"],
                "include_pending": True, "limit": 2}
        saved = self.snapshot()
        pending_before = copy.deepcopy(self.team._pending_operations(self.task))
        cursor, seen = None, []
        with self.no_workspace_io(), patch.object(self.native.driver, "tool_turn") as model:
            for expected, more in ((pending[3:], True), (pending[1:3], True), (pending[:1], False)):
                page = self.direct({**args, **({"before_seq": cursor} if cursor else {})}, reader)
                self.assert_record_read(page, reader)
                self.assert_page(page, [receipt] if cursor is None else [])
                self.assertEqual([row["seq"] for row in page["pending_operations"]],
                                 [start.seq for start in expected])
                for row in page["pending_operations"]:
                    self.assertEqual(row["run_id"], orphan.payload["run_id"])
                    self.assertEqual(row["message_seq"], orphan.payload["message_seq"])
                    self.assertEqual(row["outcome"], "unsettled")
                    self.assertNotIn("record_read", row)
                self.assertIs(page["pending_has_more"], more)
                self.assertEqual(page["pending_next_before_seq"], expected[0].seq if more else None)
                seen.extend(row["seq"] for row in page["pending_operations"])
                cursor = page["pending_next_before_seq"]
            model.assert_not_called()
        self.assertEqual(sorted(seen), [start.seq for start in pending])
        self.assertEqual(self.team._pending_operations(self.task), pending_before)
        self.assertEqual((self.project / "effect.txt").read_text(encoding="utf-8"), disk_body)
        self.assertEqual(self.snapshot(), saved)

    def test_native_unknown_run_error_and_missing_exact_receipt_keep_compatibility(self):
        unknown, missing = self.f.exchange(("read_receipts", {"run_id": "unknown-provenance-run"}),
                                           ("read_receipts", {"receipt_seq": 2 ** 62}))
        self.assertEqual(unknown["status"], "error")
        self.assertIn("role", unknown["message"])
        self.assertIn("message_seq", unknown["message"])
        self.assertNotIn("record_read", unknown)
        self.assertNotIn("receipts", unknown)
        reader, _ = self.query_events()[-1]
        self.assert_record_read(missing, reader)
        self.assert_page(missing, [])

    def test_paused_legacy_direct_queries_only_add_kind_and_false(self):
        self.f.put("paused.txt", "PERSISTED_PAUSED_BODY\n")
        start, finish, _ = self.f.read("paused.txt")
        self.rt.execution_control.change(self.task, "pause", request_id="provenance-pause")
        saved = self.snapshot()
        with self.no_workspace_io(), patch.object(self.native.driver, "tool_turn") as model:
            for args in ({"receipt_seq": finish.seq}, {"name": "workspace_read"},
                         {"name": "workspace_read", "include_pending": True},
                         {"section": "file_reads"}, {"receipt_seq": 2 ** 62}):
                with self.subTest(arguments=args):
                    result = self.direct(args)
                    self.assert_record_read(result)
                    if args.get("section") == "file_reads":
                        self.assert_file_state(result, "matched_records")
                        self.assertEqual([row["seq"] for row in result["observations"]], [finish.seq])
                    else:
                        self.assert_page(result, [] if args.get("receipt_seq") == 2 ** 62 else [finish])
                        for row in result["receipts"]:
                            self.assert_original(row, start, finish)
                    if args.get("include_pending"):
                        self.assertEqual(result["pending_operations"], [])
                        self.assertIs(result["pending_has_more"], False)
                        self.assertIsNone(result["pending_next_before_seq"])
            model.assert_not_called()
        self.assertEqual(self.rt.execution_control.view(self.task)["state"], "paused")
        self.assertEqual(self.snapshot(), saved)

    def test_reader_identity_requires_an_exact_persisted_local_host_start(self):
        full = self.reader_start()
        payload = {key: full.payload[key] for key in ("name", "role", "run_id", "message_seq")}

        def persist(**changes):
            return self.log.emit(**{"type": "team.tool_started", "task_id": self.task,
                                    "actor": HOST, "payload": payload, **changes})

        legacy = persist()
        self.assertNotIn("revision", legacy.payload)
        self.assertNotIn("root", legacy.payload)
        original_start = persist(payload={**full.payload, "name": "workspace_read",
                                          "call_id": "reader-identity-original"})
        original_body = {"status": "ok", "content": "SAVED_READER_IDENTITY_BODY",
                         "run_id": "original-operation-run",
                         "nested": {"reader_run_id": "original-inner-value"}}
        original_finish = self.log.emit(type="team.tool_finished", task_id=self.task, actor=HOST,
            payload={**{key: original_start.payload[key] for key in ("name", "role", "run_id", "call_id")},
                     "started_seq": original_start.seq, "result": original_body})
        foreign_task = self.rt.create_task("Reader identity foreign fixture")
        foreign = persist(task_id=foreign_task)
        maximum_run = persist(payload={**payload, "run_id": "r" * 128})
        cases = [
            ("none", None, False),
            ("plain-object", object(), False),
            ("dictionary", dict(legacy.__dict__), False),
            ("event-lookalike", SimpleNamespace(**legacy.__dict__), False),
            ("minimal-legacy", legacy, True),
            ("copy-of-stored-event", copy.deepcopy(legacy), True),
            ("maximum-run-length", maximum_run, True),
            ("unpersisted-event", replace(legacy, seq=2 ** 62), False),
            ("changed-payload", replace(legacy, payload={**payload, "run_id": "unpersisted-run"}), False),
            ("changed-hash", replace(legacy, hash="0" * 64), False),
            ("wrong-task", foreign, False),
            ("foreign-event-relabelled", replace(foreign, task_id=self.task), False),
            ("wrong-actor", persist(actor=Actor(HOST.id, "forged-role", HOST.kind)), False),
            ("wrong-type", persist(type="team.tool_finished"), False),
            ("wrong-name", persist(payload={**payload, "name": "workspace_read"}), False),
            ("wrong-role", persist(payload={**payload, "role": self.native.worker}), False),
        ]
        for key, label, value in (
                ("run_id", "blank-run", " \t\r\n"),
                ("run_id", "empty-run", ""),
                ("run_id", "oversized-run", "r" * 129),
                ("run_id", "nonstring-run", True),
                ("run_id", "null-run", None),
                ("message_seq", "zero-message", 0),
                ("message_seq", "negative-message", -1),
                ("message_seq", "boolean-message", True),
                ("message_seq", "float-message", float(payload["message_seq"])),
                ("message_seq", "string-message", str(payload["message_seq"])),
                ("message_seq", "null-message", None),
                ("message_seq", "future-message", 2 ** 63)):
            cases.append((label, persist(payload={**payload, key: value}), False))
        for key in ("run_id", "message_seq"):
            cases.append((f"missing-{key}", persist(payload={k: v for k, v in payload.items() if k != key}), False))
        next_seq = self.log.db.execute("SELECT MAX(seq) + 1 FROM events").fetchone()[0]
        cases.append(("message-equals-start", persist(payload={**payload, "message_seq": next_seq}), False))
        for label, seq in (("zero", 0), ("negative", -1), ("boolean", True),
                           ("float", float(legacy.seq)), ("string", str(legacy.seq)), ("null", None),
                           ("upper-bound", 2 ** 63 - 1), ("overflow", 2 ** 63), ("huge", 10 ** 100)):
            cases.append((f"{label}-start-seq", replace(legacy, seq=seq), False))

        queries = (("exact", {"receipt_seq": original_finish.seq}),
                   ("paged", {"name": "workspace_read", "limit": 1}),
                   ("pending", {"name": "workspace_read", "include_pending": True}),
                   ("file-metadata", {"section": "file_reads"}))
        bounded_cases = {"minimal-legacy", "copy-of-stored-event", "maximum-run-length",
                         "unpersisted-event", "changed-payload", "changed-hash", "foreign-event-relabelled"}
        saved = self.snapshot()
        iter_events = self.log.iter_events
        with self.no_workspace_io(), patch.object(self.native.driver, "tool_turn",
                side_effect=AssertionError("Reader identity test must not call a model")) as model:
            baselines = {label: self.direct(args) for label, args in queries}
            for label, args in queries[:3]:
                self.assert_page(baselines[label], [original_finish])
                self.assert_original(baselines[label]["receipts"][0], original_start, original_finish)
                self.assertEqual(baselines[label]["receipts"][0]["result"], original_body)
            for identity, candidate, trusted in cases:
                for label, args in queries:
                    with self.subTest(identity=identity, query=label):
                        with patch.object(self.log, "iter_events", wraps=iter_events) as reads:
                            result = self.direct(args, candidate)
                        self.assert_record_read(result, candidate if trusted else None)
                        self.assertEqual({k: v for k, v in result.items() if k != "record_read"},
                                         {k: v for k, v in baselines[label].items() if k != "record_read"})
                        if identity in bounded_cases:
                            reads.assert_any_call(task_id=self.task, event_types=("team.tool_started",),
                                                  after_seq=candidate.seq - 1, before_seq=candidate.seq + 1)
            model.assert_not_called()
        self.assertEqual(self.native.driver.calls, [])
        self.assertEqual(self.snapshot(), saved)

    def test_valid_empty_binding_and_filters_report_no_matching_records(self):
        empty, _ = self.query({"section": "file_reads"})
        self.assert_file_state(empty, "no_matching_records")
        self.f.put("filters.txt", "FILTERED_OBSERVATION_BODY\n")
        start, finish, _ = self.f.read("filters.txt")
        filters = {"section": "file_reads", "file_path": "filters.txt", "name": "workspace_read",
                   "role": self.native.worker, "run_id": start.payload["run_id"],
                   "message_seq": start.payload["message_seq"], "limit": 1}
        matched, _ = self.query(filters)
        self.assert_file_state(matched, "matched_records")
        self.assertEqual([row["seq"] for row in matched["observations"]], [finish.seq])
        saved = self.snapshot()
        with self.no_workspace_io():
            for key, value in (("file_path", "never-observed.txt"), ("name", "workspace_list"),
                               ("role", self.native.reviewer), ("run_id", "unknown-file-run"),
                               ("message_seq", 2 ** 62), ("before_seq", finish.seq)):
                with self.subTest(filter=key):
                    result, _ = self.query({**filters, key: value})
                    self.assert_file_state(result, "no_matching_records")
                    self.assertEqual(result["workspace_binding"], matched["workspace_binding"])
        self.assertNotEqual(empty["note"], matched["note"])
        self.assert_saved(saved)

    def test_missing_scope_keeps_historical_exact_receipt_readable(self):
        self.f.put("scope.txt", "PERSISTED_WITHOUT_CURRENT_SCOPE\n")
        start, finish, body = self.f.read("scope.txt")
        config = self.native.root / "supervision-workspaces.json"
        config.write_text('{"workspaces": []}', encoding="utf-8")
        reader = self.reader_start()
        saved = self.snapshot()
        with self.no_workspace_io():
            metadata = self.direct({"section": "file_reads"}, reader)
            self.assert_record_read(metadata, reader)
            self.assert_file_state(metadata, "scope_unavailable")
            self.assertIn("\u4e0d\u8bc1\u660e\u53d1\u751f\u8fc7\u89e3\u7ed1\u6216\u6743\u9650\u53d8\u66f4", metadata["note"])
            exact = self.direct({"receipt_seq": finish.seq}, reader)
            self.assert_record_read(exact, reader)
            self.assert_original(exact["receipts"][0], start, finish)
            self.assertEqual(exact["receipts"][0]["result"], body)
        self.assertEqual(self.snapshot(), saved)

    def test_never_bound_scope_is_unavailable_and_rebinding_creates_valid_empty_scope(self):
        self.f.put("epoch.txt", "OLDER_BINDING_BODY\n")
        _, old, _ = self.f.read("epoch.txt")
        binding = self.rt.workspaces.view(self.task)
        self.rt.workspaces.bind(self.task, "supervision", expected_revision=binding["revision"])
        result, _ = self.query({"section": "file_reads"})
        self.assert_file_state(result, "no_matching_records")
        self.assertGreater(result["workspace_binding"]["binding_seq"], old.seq)
        exact, _ = self.query({"receipt_seq": old.seq})
        self.assert_page(exact, [old])
        unbound = self.rt.create_task("Never-bound provenance fixture")
        self.team.enable(unbound)
        result = self.f.query({"section": "file_reads"}, task=unbound)
        starts = {event.seq: event for event in self.log.iter_events(
            task_id=unbound, event_types=("team.tool_started",))}
        finish, = [event for event in self.log.iter_events(task_id=unbound,
                   event_types=("team.tool_finished",)) if event.payload["name"] == "read_receipts"]
        self.assert_record_read(result, starts[finish.payload["started_seq"]])
        self.assert_file_state(result, "scope_unavailable")

    def test_scope_races_return_error_without_success_state_or_partial_rows(self):
        self.f.put("race.txt", "RACE_OBSERVATION_BODY\n")
        self.f.read("race.txt")
        reader = self.reader_start()
        scope = self.team.read_sources._scope(self.task)
        changed = {**scope, "binding_seq": scope["binding_seq"] + 1}
        saved = self.snapshot()
        for initial, final in ((scope, changed), (scope, None), (None, scope)):
            with self.subTest(initial=initial, final=final), self.no_workspace_io():
                with patch.object(self.team.read_sources, "_scope", side_effect=[initial, final]) as scopes:
                    result = self.direct({"section": "file_reads"}, reader)
                self.assertEqual(scopes.call_count, 2)
                self.assertEqual(result["status"], "error")
                self.assertEqual(result["code"], "file_read_scope_changed")
                self.assert_record_read(result, reader)
                for key in ("query_state", "scope_available", "workspace_binding", "observations",
                            "has_more", "next_before_seq", "note"):
                    self.assertNotIn(key, result)
                self.assertNotIn("RACE_OBSERVATION_BODY", json.dumps(result))
        self.assertEqual(self.snapshot(), saved)

    def test_metadata_failure_stays_error_instead_of_becoming_empty_success(self):
        reader = self.reader_start()
        saved = self.snapshot()
        with patch.object(self.team.read_sources, "_observations", side_effect=sqlite3.OperationalError("offline")):
            result = self.direct({"section": "file_reads"}, reader)
        self.assert_record_read(result, reader)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["code"], "file_read_metadata_unavailable")
        self.assertNotIn("query_state", result)
        self.assertNotIn("observations", result)
        self.assertEqual(self.snapshot(), saved)

    def test_foreign_task_same_root_and_run_id_never_leaks_into_readback(self):
        self.f.put("shared.txt", "LOCAL_PROVENANCE_BODY\n")
        local = self.f.read("shared.txt")
        foreign = self.rt.create_task("Foreign provenance fixture")
        self.team.enable(foreign)
        self.rt.workspaces.bind(foreign, "supervision", expected_revision=None)
        self.f.put("shared.txt", "FOREIGN_PROVENANCE_SECRET\n")
        private_body, = self.f.exchange(("workspace_read", {"path": "shared.txt"}), task=foreign)
        self.assertEqual(private_body["status"], "ok")
        private, = [event for event in self.log.iter_events(task_id=foreign,
                    event_types=("team.tool_finished",)) if event.payload["name"] == "workspace_read"]
        private_start = next(event for event in self.log.iter_events(task_id=foreign,
                             event_types=("team.tool_started",))
                             if event.seq == private.payload["started_seq"])
        _, collision, _ = self.f.forge(local, task=foreign,
            run_changes={"run_id": local[0].payload["run_id"]},
            start_changes={"run_id": local[0].payload["run_id"]},
            finish_changes={"result": private_body})
        self.assertEqual([row["seq"] for row in self.team.read_sources.exact(
            foreign, {"receipt_seq": collision.seq})], [collision.seq])
        saved = self.snapshot()
        calls_before = len(self.native.driver.calls)
        responses = []
        with self.no_workspace_io():
            for args in ({"receipt_seq": private.seq}, {"name": "workspace_read"},
                         {"name": "workspace_read", "run_id": local[0].payload["run_id"],
                          "include_pending": True},
                         {"section": "file_reads", "file_path": "shared.txt"},
                         {"section": "file_reads", "message_seq": private_start.payload["message_seq"]}):
                result, _ = self.query(args)
                responses.append(result)
                if args.get("section") == "file_reads":
                    expected = "no_matching_records" if "message_seq" in args else "matched_records"
                    self.assert_file_state(result, expected)
                    if expected == "matched_records":
                        self.assertEqual([row["seq"] for row in result["observations"]], [local[1].seq])
                else:
                    self.assert_page(result, [] if "receipt_seq" in args else [local[1]])
                    for row in result["receipts"]:
                        self.assert_original(row, local[0], local[1])
                    if args.get("include_pending"):
                        self.assertEqual(result["pending_operations"], [])
        self.assertNotIn("FOREIGN_PROVENANCE_SECRET", json.dumps(responses))
        self.assertNotIn("FOREIGN_PROVENANCE_SECRET", json.dumps(self.native.driver.calls[calls_before:]))
        self.assert_saved(saved)

    def test_external_callback_marks_reader_and_preserves_prior_run_result(self):
        fixture = self.fixture(external.TeamExternalTests)
        # Reuse the existing workspace setup against the external fixture's runtime.
        project = lookups.TeamReceiptLookupTests.bind_workspace(fixture)
        (project / "observed.txt").write_text("EXTERNAL_PERSISTED_BODY\n", encoding="utf-8")
        observed = []

        def original(run):
            body = run.call("workspace_read", path="observed.txt")
            self.assertEqual(body["status"], "ok")
            observed.append((fixture.events("team.tool_started")[-1],
                             fixture.events("team.tool_finished")[-1], copy.deepcopy(body)))
            return "Saved the original observation."

        fixture.driver.scripts = {fixture.manager: [original]}
        fixture.receive("Observe a disposable file once")
        fixture.drain()
        old_start, old_finish, old_body = observed[0]
        raw_before = tuple(fixture.rt.log.db.execute("SELECT * FROM events WHERE seq = ?",
                                                    (old_finish.seq,)).fetchone())
        (project / "observed.txt").write_text("EXTERNAL_CURRENT_DISK_BODY\n", encoding="utf-8")
        results = []

        def readback(run):
            for args in ({"receipt_seq": old_finish.seq},
                         {"run_id": old_start.payload["run_id"], "include_pending": True, "limit": 1},
                         {"section": "file_reads", "file_path": "observed.txt"}):
                result = run.call("read_receipts", **args)
                start, finish = self.query_events(fixture)[-1]
                self.assert_record_read(result, start)
                self.assertEqual(finish.payload["result"], result)
                self.assertNotEqual(start.payload["run_id"], old_start.payload["run_id"])
                self.assertEqual(start.payload["message_seq"], run.context["message"]["seq"])
                self.assertIn("external_started_seq", start.payload)
                if args.get("section") == "file_reads":
                    self.assertEqual(result["query_state"], "matched_records")
                    row, = result["observations"]
                    self.assertEqual(row["seq"], old_finish.seq)
                    self.assertEqual(row["run_id"], old_start.payload["run_id"])
                    self.assertEqual(row["message_seq"], old_start.payload["message_seq"])
                    self.assertEqual(row["read_args"], {"receipt_seq": old_finish.seq})
                else:
                    self.assert_page(result, [old_finish])
                    self.assert_original(result["receipts"][0], old_start, old_finish)
                    self.assertEqual(result["receipts"][0]["result"], old_body)
                    if args.get("include_pending"):
                        self.assertEqual(result["pending_operations"], [])
                results.append(result)
            return "Read the persisted observations."

        fixture.driver.scripts = {fixture.manager: [readback]}
        fixture.receive("Read the saved receipts in another run")
        with patch.object(WorkspaceExecutor, "execute", side_effect=AssertionError("External readback reran a tool")) as executor:
            fixture.drain()
            executor.assert_not_called()
        for finished in fixture.events("team.run_finished"):
            self.assertEqual(finished.payload["status"], "completed", finished.payload)
        self.assertEqual(len(results), 3)
        self.assertEqual(len(fixture.driver.runs), 2)
        self.assertEqual(len(fixture.results("workspace_read")), 1)
        self.assertNotIn("EXTERNAL_CURRENT_DISK_BODY", json.dumps(results))
        self.assertEqual(tuple(fixture.rt.log.db.execute("SELECT * FROM events WHERE seq = ?",
                                                       (old_finish.seq,)).fetchone()), raw_before)


if __name__ == "__main__":
    unittest.main()
