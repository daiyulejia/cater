"""Offline completed-request contracts; compose fixtures without extra discovery."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import sqlite3
import unittest
from unittest.mock import patch

from cater.events import Actor
from cater.tool_protocol import json_utf8
import test_receipt_index as index_fixture


class ReceiptRequestTests(unittest.TestCase):
    def setUp(self):
        self.f = index_fixture.ReceiptIndexTests()
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()

    def request(self, receipt, *, reader=None, **arguments):
        seq = receipt.seq if hasattr(receipt, "seq") else receipt
        return self.f.query({"section": "request", "receipt_seq": seq, **arguments},
                            reader=reader)

    def saved(self, arguments, *, result=None):
        run, _ = self.f.source_run()
        start = self.f.start(run, arguments=arguments)
        return start, self.f.finish(start, result)

    @contextmanager
    def raw_payloads(self, replacements):
        # Corrupt only a disposable parser copy, never the append-only log.
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

    def assert_error(self, response, code, *markers):
        self.assertEqual(response["status"], "error", response)
        self.assertEqual(response["code"], code, response)
        self.assertNotIn("request", response)
        serialized = json.dumps(response, ensure_ascii=True)
        for marker in ("Traceback", "UnicodeEncodeError", "JSONDecodeError", *markers):
            self.assertNotIn(marker, serialized)

    def assert_missing(self, response):
        self.assertEqual(response["status"], "ok", response)
        self.assertEqual(response["section"], "request")
        self.assertIsNone(response["request"])
        self.assertNotIn("HIDDEN_REQUEST", json.dumps(response))

    def assert_part(self, response, start, finish, full, *, part="stdin", offset=0,
                    length=4000, reader=None):
        self.assertEqual(response["status"], "ok", response)
        self.assertEqual(response["section"], "request")
        self.assertTrue(response["note"])
        self.assertLessEqual(len(json_utf8(response)), 8000)
        record = {"kind": "persisted_events", "reexecutes_operations": False}
        if reader is not None:
            record.update(reader_run_id=reader.payload["run_id"],
                          reader_message_seq=reader.payload["message_seq"],
                          reader_started_seq=reader.seq)
        self.assertEqual(response["record_read"], record)
        request = response["request"]
        self.assertIsInstance(request, dict)
        for key, expected in {
            "seq": finish.seq, "source_hash": finish.hash, "started_seq": start.seq,
            "request_hash": start.hash, "part": part,
            "content_sha256": hashlib.sha256(full.encode("utf-8")).hexdigest(),
            **{key: start.payload[key] for key in ("run_id", "message_seq", "role", "name")},
        }.items():
            self.assertEqual(request[key], expected, key)
        content = request["content"]
        count = len(content)
        self.assertLessEqual(count, min(length, len(full) - offset))
        if offset < len(full):
            self.assertGreater(count, 0, "Nonterminal pages must advance")
        self.assertEqual(content, full[offset:offset + count])
        end = offset + count
        expected_range = {
            "unit": "unicode_codepoints", "offset": offset, "returned_chars": count,
            "total_chars": len(full), "has_more": end < len(full),
            "next_offset": end if end < len(full) else None,
            "content_complete": offset == 0 and end == len(full),
        }
        if part == "stdin":
            expected_range.update(first_line=full.count("\n", 0, offset) + 1,
                                  starts_mid_line=offset > 0 and full[offset - 1] != "\n")
        self.assertEqual(request["read_range"], expected_range)
        return request

    def test_unicode_and_json_escaping_pages_reassemble_both_parts_under_byte_cap(self):
        text = ('中文😀"\\\t\r\n第二行🧪e\u0301\x00\u2028' * 900) + "末尾"
        arguments = {"stdin": text, "command": "python", "argv": ["-", '引号"\\']}
        start, finish = self.saved(arguments)
        reader = self.f.reader()
        for part, full in (("stdin", text), ("arguments", json.dumps(
                arguments, ensure_ascii=False, sort_keys=True, allow_nan=False))):
            with self.subTest(part=part):
                chunks, offset, shrunk = [], 0, False
                for _ in range(len(full) + 1):
                    response = self.request(finish, request_part=part, offset=offset, reader=reader)
                    request = self.assert_part(response, start, finish, full, part=part,
                                               offset=offset, reader=reader)
                    count = len(request["content"])
                    shrunk |= count < min(4000, len(full) - offset)
                    chunks.append(request["content"])
                    if not request["read_range"]["has_more"]:
                        break
                    offset = request["read_range"]["next_offset"]
                else:
                    self.fail("Request pagination failed to terminate")
                self.assertGreater(len(chunks), 1)
                self.assertTrue(shrunk, "Fixture must exercise bytes, not just the character limit")
                self.assertEqual("".join(chunks), full)

    def test_default_arguments_are_sorted_json_with_default_spaces_and_full_part_hash(self):
        arguments = {"z": [True, None, 1.25, {"乙": "😀", "a": '"\\'}],
                     "stdin": "print('原文')\n", "a": {"z": 1, "a": 2}}
        start, finish = self.saved(arguments)
        expected = json.dumps(arguments, ensure_ascii=False, sort_keys=True, allow_nan=False)
        response = self.request(finish)
        request = self.assert_part(response, start, finish, expected, part="arguments")
        self.assertEqual(request["content"], expected)
        self.assertEqual(json.loads(request["content"]), arguments)
        self.assertEqual(response, self.request(finish, request_part="arguments"))
        page = self.request(finish, offset=3, length=7)
        partial = self.assert_part(page, start, finish, expected, part="arguments", offset=3, length=7)
        self.assertEqual(partial["content"], expected[3:10])
        self.assertNotEqual(partial["content_sha256"],
                            hashlib.sha256(partial["content"].encode("utf-8")).hexdigest())

    def test_default_character_limit_and_explicit_length_endpoints(self):
        start, finish = self.saved({"stdin": "x" * 4001})
        first = self.assert_part(self.request(finish, request_part="stdin"), start, finish, "x" * 4001)
        self.assertEqual(first["read_range"]["returned_chars"], 4000)
        for length in (1, 4000):
            with self.subTest(length=length):
                response = self.request(finish, request_part="stdin", offset=1, length=length)
                page = self.assert_part(response, start, finish, "x" * 4001, offset=1, length=length)
                self.assertEqual(len(page["content"]), length)

    def test_stdin_line_ranges_count_lf_and_codepoints_at_every_boundary(self):
        text = "甲😀\r\n\n乙\r丙\n终"
        start, finish = self.saved({"stdin": text})
        for offset in range(len(text) + 1):
            with self.subTest(offset=offset):
                response = self.request(finish, request_part="stdin", offset=offset, length=1)
                page = self.assert_part(response, start, finish, text, offset=offset, length=1)
                self.assertEqual(page["content"], text[offset:offset + 1])
        for ending in ("", "x", "x\n"):
            start, finish = self.saved({"stdin": ending})
            for offset in {0, len(ending)}:
                with self.subTest(ending=ending, offset=offset):
                    self.assert_part(self.request(finish, request_part="stdin", offset=offset),
                                     start, finish, ending, offset=offset)

    def test_each_receipt_uses_its_paired_start_not_latest_command_or_result_claims(self):
        run, _ = self.f.source_run()
        first = self.f.start(run, arguments={"stdin": "FIRST_INPUT"})
        second = self.f.start(run, arguments={"stdin": "SECOND_INPUT"})
        two = self.f.finish(second)
        one = self.f.finish(first, {"status": "error", "stdin": "FORGED_INPUT",
                                  "started_seq": second.seq, "source_hash": "FORGED_HASH",
                                  "request_hash": second.hash})
        for start, finish in ((first, one), (second, two)):
            with self.subTest(seq=finish.seq):
                self.assert_part(self.request(finish, request_part="stdin"), start, finish,
                                 start.payload["arguments"]["stdin"])

    def test_filters_are_anded_and_before_seq_is_exclusive(self):
        run, message = self.f.source_run()
        start = self.f.start(run, arguments={"stdin": "FILTER_INPUT"})
        finish = self.f.finish(start)
        other, other_message = self.f.source_run(role=self.f.f.manager)
        filters = {"run_id": run.payload["run_id"], "role": run.payload["role"],
                   "name": "workspace_run", "message_seq": message.seq, "before_seq": finish.seq + 1}
        self.assert_part(self.request(finish, request_part="stdin", **filters),
                         start, finish, "FILTER_INPUT")
        for key, value in (("run_id", other.payload["run_id"]), ("run_id", "missing-run"),
                           ("role", self.f.f.manager), ("name", "workspace_write"),
                           ("message_seq", other_message.seq), ("before_seq", finish.seq)):
            with self.subTest(key=key, value=value):
                self.assert_missing(self.request(finish, **{**filters, key: value}))

    def test_absent_unfinished_and_noncommand_sources_are_ok_null(self):
        run, message = self.f.source_run()
        pending = self.f.start(run, arguments={"stdin": "HIDDEN_REQUEST"})
        hidden = [self.f.finish(self.f.start(run, name=name,
                                            arguments={"stdin": "HIDDEN_REQUEST"}))
                  for name in ("workspace_write", "workspace_read", "read_receipts")]
        for event in (run, message, pending, *hidden, 2 ** 63 - 1, 2 ** 63, 10 ** 100):
            with self.subTest(seq=getattr(event, "seq", event)):
                self.assert_missing(self.request(event))

    def test_cross_task_and_forged_host_sources_never_reveal_requests(self):
        run, _ = self.f.source_run(run_id="shared-request-run")
        visible = self.f.finish(self.f.start(run, arguments={"stdin": "VISIBLE"}))
        hidden = []
        for actor in (Actor.agent(self.f.f.worker, "worker"), Actor.system("not-team")):
            hidden.append(self.f.finish(self.f.start(run, arguments={"stdin": "HIDDEN_REQUEST"}),
                                        actor=actor))
            hidden.append(self.f.finish(self.f.start(run, actor=actor,
                                                     arguments={"stdin": "HIDDEN_REQUEST"})))
        other_task = self.f.rt.create_task("Private request task")
        self.f.team.enable(other_task)
        foreign, foreign_message = self.f.source_run(task_id=other_task, run_id="shared-request-run")
        start = self.f.start(foreign, arguments={"stdin": "HIDDEN_REQUEST"})
        hidden.extend((self.f.finish(start), self.f.finish(start, task=self.f.task)))
        self.assertEqual(self.request(visible, request_part="stdin")["request"]["content"], "VISIBLE")
        self.assert_missing(self.request(visible, message_seq=foreign_message.seq))
        for finish in hidden:
            with self.subTest(seq=finish.seq):
                self.assert_missing(self.request(finish, run_id="shared-request-run"))

    def test_mismatched_pair_run_and_message_bindings_are_hidden(self):
        run, message = self.f.source_run()
        hidden = []
        for key, value in (("run_id", "wrong-run"), ("role", self.f.f.manager),
                           ("name", "workspace_write"), ("call_id", "wrong-call"),
                           ("started_seq", message.seq), ("started_seq", True)):
            hidden.append(self.f.finish(self.f.start(run, arguments={"stdin": "HIDDEN_REQUEST"}),
                                        **{key: value}))
        hidden.append(self.f.finish(self.f.start(run, message_seq=message.seq + 100000)))
        hidden.append(self.f.finish(self.f.start(run, run_id="never-started")))
        late = self.f.start(run)
        self.f.f.close_fixture_run()
        hidden.append(self.f.finish(late))
        duplicate, _ = self.f.source_run()
        hidden.append(self.f.finish(self.f.start(duplicate)))
        self.f.emit("team.run_started", dict(duplicate.payload))
        for finish in hidden:
            with self.subTest(seq=finish.seq):
                self.assert_missing(self.request(finish))

    def test_unhashable_or_invalid_role_and_status_metadata_are_hidden(self):
        run, _ = self.f.source_run()
        for value in ([], {}, ["ok"], {"HIDDEN_REQUEST": 1}, None, True, 0):
            for field in ("role", "status"):
                with self.subTest(field=field, value=value):
                    if field == "role":
                        start = self.f.start(run, role=value, arguments={"stdin": "HIDDEN_REQUEST"})
                        finish = self.f.finish(start)
                    else:
                        start = self.f.start(run, arguments={"stdin": "HIDDEN_REQUEST"})
                        finish = self.f.finish(start, {"status": value})
                    self.assert_missing(self.request(finish))

    def test_request_requires_receipt_and_rejects_legacy_only_arguments(self):
        _, finish = self.saved({"stdin": "PRIVATE_REQUEST"})
        invalid = [{"section": "request"}]
        for key, values in {
            "include_pending": (False, True, None), "file_path": ("PRIVATE_INPUT.txt", None),
            "limit": (1, 20, None),
        }.items():
            invalid.extend({"section": "request", "receipt_seq": finish.seq, key: value}
                           for value in values)
        for arguments in invalid:
            with self.subTest(arguments=arguments):
                self.assert_error(self.f.query(arguments), "invalid_receipt_query", "PRIVATE_INPUT", "PRIVATE_REQUEST")

    def test_request_parameter_types_and_ranges_reject_bool_and_unhashable_values(self):
        _, finish = self.saved({"stdin": "PRIVATE_REQUEST"})
        invalid = {
            "receipt_seq": (None, False, 0, -1, "PRIVATE_INPUT", [], {}),
            "request_part": (None, False, "PRIVATE_INPUT", [], {}),
            "offset": (None, False, -1, 1.5, "PRIVATE_INPUT", [], {}),
            "length": (None, True, 0, -1, 4001, 1.5, "PRIVATE_INPUT", [], {}),
            "message_seq": (False, 0, []), "before_seq": (True, -1, {}),
            "run_id": ("", [], {}), "role": ("PRIVATE_INPUT", []), "name": ({},),
        }
        for key, values in invalid.items():
            for value in values:
                with self.subTest(field=key, value=value):
                    response = self.f.query({"section": "request", "receipt_seq": finish.seq, key: value})
                    self.assert_error(response, "invalid_receipt_query", "PRIVATE_INPUT", "PRIVATE_REQUEST")

    def test_nonrequest_sections_reject_even_default_request_parameters(self):
        for section in (None, "receipts", "index", "file_reads"):
            for key, value in (("request_part", "arguments"), ("offset", 0), ("length", 4000)):
                with self.subTest(section=section, field=key):
                    arguments = {key: value}
                    if section is not None:
                        arguments["section"] = section
                    self.assert_error(self.f.query(arguments), "invalid_receipt_query")

    def test_missing_and_nonstring_stdin_are_unavailable_but_arguments_remain_readable(self):
        for arguments in ({}, {"stdin": None}, {"stdin": 17}, {"stdin": []}, {"stdin": {}}):
            with self.subTest(arguments=arguments):
                start, finish = self.saved(arguments)
                self.assert_error(self.request(finish, request_part="stdin"), "receipt_request_unavailable")
                expected = json.dumps(arguments, ensure_ascii=False, sort_keys=True, allow_nan=False)
                self.assert_part(self.request(finish), start, finish, expected, part="arguments")

    def test_offsets_beyond_eof_are_errors_for_both_parts(self):
        arguments = {"stdin": "甲😀"}
        _, finish = self.saved(arguments)
        for part, full in (("stdin", arguments["stdin"]), ("arguments", json.dumps(
                arguments, ensure_ascii=False, sort_keys=True, allow_nan=False))):
            for offset in (len(full) + 1, 10 ** 100):
                with self.subTest(part=part, offset=offset):
                    self.assert_error(self.request(finish, request_part=part, offset=offset),
                                      "invalid_receipt_query")

    def test_malformed_arguments_duplicate_nul_unicode_and_nonfinite_are_unavailable(self):
        start, finish = self.saved({"stdin": "PRIVATE_REQUEST"})
        raw_arguments = [
            'null', '[]', '"PRIVATE_REQUEST"',
            '{"stdin":"PRIVATE_REQUEST","nested":[{"key":1,"key":2}]}',
            '{"stdin":"PRIVATE_REQUEST","nested":{"key":1,"k\\u0065y":2}}',
            '{"stdin":"PRIVATE_REQUEST","nested":{"bad\\u0000key":1}}',
            '{"stdin":"PRIVATE_REQUEST","nested":{"bad\\ud800key":1}}',
            '{"stdin":"PRIVATE_REQUEST","nested":{"value":"\\udfff"}}',
            '{"stdin":"\\ud800"}',
            '{"stdin":"PRIVATE_REQUEST","number":1e400}',
            '{"stdin":"PRIVATE_REQUEST","number":-1e400}',
        ]
        template = json.dumps({**start.payload, "arguments": "RAW_ARGUMENTS"})
        for raw in raw_arguments:
            with self.subTest(raw=raw), self.raw_payloads([
                    (template.replace('"RAW_ARGUMENTS"', raw), start.seq)]):
                for part in ("arguments", "stdin"):
                    self.assert_error(self.request(finish, request_part=part),
                                      "receipt_request_unavailable", "PRIVATE_REQUEST", "bad")
        missing = {key: value for key, value in start.payload.items() if key != "arguments"}
        with self.raw_payloads([(json.dumps(missing), start.seq)]):
            self.assert_error(self.request(finish), "receipt_request_unavailable")
        self.assertTrue(self.f.rt.verify_chain().ok)

    def test_repeated_keys_in_separate_objects_and_nul_values_are_valid(self):
        arguments = {"stdin": "原文\0😀", "nested": [{"key": 1}, {"key": 2}],
                     "literal": '{"key":1,"key":2}'}
        start, finish = self.saved(arguments)
        for part, full in (("stdin", arguments["stdin"]), ("arguments", json.dumps(
                arguments, ensure_ascii=False, sort_keys=True, allow_nan=False))):
            self.assert_part(self.request(finish, request_part=part), start, finish, full, part=part)

    def test_source_limit_is_256_kib_utf8_bytes_and_oversize_body_is_not_decoded(self):
        start, finish = self.saved({"stdin": "可读😀"})
        base = json.dumps({**start.payload, "padding": ""}, ensure_ascii=False)
        padding_bytes = 256 * 1024 - len(base.encode("utf-8"))
        padding = "界" * (padding_bytes // 3) + "x" * (padding_bytes % 3)
        exact = json.dumps({**start.payload, "padding": padding}, ensure_ascii=False)
        self.assertEqual(len(exact.encode("utf-8")), 256 * 1024)
        with self.raw_payloads([(exact, start.seq)]):
            self.assert_part(self.request(finish, request_part="stdin"), start, finish, "可读😀")
        over = json.dumps({**start.payload, "padding": padding + "x"}, ensure_ascii=False)
        self.assertEqual(len(over.encode("utf-8")), 256 * 1024 + 1)
        loads = json.loads

        def bounded_loads(value, *args, **kwargs):
            encoded = value.encode("utf-8") if isinstance(value, str) else value
            self.assertLessEqual(len(encoded), 256 * 1024, "Oversized source decoded in Python")
            return loads(value, *args, **kwargs)

        with self.raw_payloads([(over, start.seq)]), patch.object(json, "loads", side_effect=bounded_loads):
            for part in ("arguments", "stdin"):
                self.assert_error(self.request(finish, request_part=part), "receipt_request_unavailable")

    def test_request_never_loads_completed_output_or_whole_log(self):
        marker = "COMPLETED_BODY_MUST_NOT_LOAD"
        start, finish = self.saved({"stdin": "small input"}, result={
            "status": "error", "stdout": marker * 20000, "exit_code": 9})
        loads = json.loads

        def observed_loads(value, *args, **kwargs):
            text = value.decode("utf-8") if isinstance(value, bytes) else value
            self.assertNotIn(marker, text)
            return loads(value, *args, **kwargs)

        with patch.object(self.f.log, "read", side_effect=AssertionError("Whole log read")), \
             patch.object(self.f.log, "_row_to_event", side_effect=AssertionError("Full event decode")), \
             patch.object(json, "loads", side_effect=observed_loads):
            # Exercise the real dispatch preamble, not the fixture's mocked view.
            response = self.f.team._execute(self.f.task, self.f.role, "read_receipts",
                {"section": "request", "receipt_seq": finish.seq, "request_part": "stdin"},
                None, None, None, None)
        self.assert_part(response, start, finish, "small input")

    def test_unparseable_host_start_reports_unavailable_without_body_disclosure(self):
        start, finish = self.saved({"stdin": "PRIVATE_REQUEST"})
        template = json.dumps(start.payload)
        for raw in (template[:-1], template[:-1] + ', "bad": NaN}',
                    template[:-1] + ', "bad": Infinity}', '[]', 'null'):
            with self.subTest(raw=raw), self.raw_payloads([(raw, start.seq)]):
                self.assert_error(self.request(finish), "receipt_request_unavailable", "PRIVATE_REQUEST")

    def test_legacy_full_results_and_index_are_unchanged_by_request_reads(self):
        arguments = {"stdin": "REQUEST_ONLY_SCRIPT", "argv": ["REQUEST_ONLY_ARGV"]}
        result = self.f.newshape(status="error", outcome="failed", exit_code=7,
                                 content="LEGACY_FULL_BODY 中文😀\r\n" * 1500,
                                 error={"message": "original error", "nested": [None, False, 3]})
        start, finish = self.saved(arguments, result=result)
        queries = ({}, {"section": "receipts"}, {"receipt_seq": finish.seq}, {"section": "index"})
        before = [self.f.query(query) for query in queries]
        head = self.f.log.head_hash()
        persisted = self.f.stored_result_bytes(finish)
        self.assertGreater(len(persisted), 8000)
        self.assert_part(self.request(finish, request_part="stdin"), start, finish, arguments["stdin"])
        self.request(finish, offset=2, length=1)
        self.assert_error(self.request(finish, offset=10 ** 100), "invalid_receipt_query")
        after = [self.f.query(query) for query in queries]
        self.assertEqual(after, before)
        self.assertEqual(before[0], before[1])
        for response in after[:3]:
            saved, = response["receipts"]
            self.assertEqual(saved["result"], result)
            self.assertEqual(json_utf8(saved["result"]), persisted)
        for marker in ("REQUEST_ONLY_SCRIPT", "REQUEST_ONLY_ARGV", "LEGACY_FULL_BODY"):
            self.assertNotIn(marker, json.dumps(after[3]))
        self.assertEqual(self.f.log.head_hash(), head)
        self.assertTrue(self.f.rt.verify_chain().ok)


if __name__ == "__main__":
    unittest.main()
