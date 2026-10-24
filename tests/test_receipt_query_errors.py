"""Correctable receipt queries through offline hosts and disposable fixtures."""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from cater.team import HOST
from cater.tool_protocol import ToolCall
from cater.workspace_tools import WorkspaceExecutor
import test_external_catalog_run as catalogs
import test_file_read_sources as sources
import test_team_external as external


class ReceiptQueryErrorTests(unittest.TestCase):
    def setUp(self):
        self.s = self.compose(sources.FileReadSourcesTests)
        self.f = self.s.f
        self.rt, self.task = self.f.rt, self.f.task
        process = self.enterContext(patch("subprocess.Popen", side_effect=AssertionError(
            "Receipt feedback tests must never launch a process")))
        self.addCleanup(process.assert_not_called)

    def compose(self, case):
        fixture = case(methodName="runTest")
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        return fixture

    def assert_invalid(self, result, *fields):
        self.assertEqual(result.get("status"), "error", result)
        self.assertEqual(result.get("code"), "invalid_receipt_query", result)
        self.assertIs(result.get("complete"), True, result)
        self.assertEqual(result.get("outcome"), "not_started", result)
        self.assertEqual(result.get("fields"), list(fields), result)
        self.assertIsInstance(result.get("message"), str)
        self.assertRegex(result["message"], r"[\u4e00-\u9fff]")
        self.assertLessEqual(set(result), {
            "status", "code", "complete", "outcome", "message", "fields", "record_read"})
        self.assertNotIn("QUERY_PRIVATE_VALUE", json.dumps(result, ensure_ascii=False))
        return result["message"]

    def invalid_values(self, field, values, **base):
        results = self.s.exchange(*(("read_receipts", {**base, field: value}) for value in values))
        messages = [self.assert_invalid(result, field) for result in results]
        self.assertEqual(len(set(messages)), 1, "Diagnostics must not reflect rejected values")
        return results

    def starts(self, name, *, fixture=None):
        fixture = fixture or self.f
        return [event for event in fixture.events("team.tool_started")
                if event.payload["name"] == name]

    def seed_write(self):
        result, = self.s.exchange(("workspace_write", {
            "path": "effect.txt", "content": "ORIGINAL_SAVED_EFFECT\n", "expected_sha256": None}))
        self.assertEqual(result["status"], "ok", result)
        finish, = [event for event in self.f.events("team.tool_finished")
                   if event.payload["name"] == "workspace_write"]
        return finish, result

    def native_script(self, steps, *, expected_status="waiting"):
        role = self.rt.team._role(self.f.worker)
        message = self.rt.team.receive(self.task, f"@{role.name} Inspect existing receipt feedback")
        started = self.rt.log.emit(type="team.run_started", actor=HOST, task_id=self.task, payload={
            "message_seq": message.seq, "role": role.id, "run_id": f"query-feedback-{message.seq}",
            "revision": self.rt.team.view(self.task)["revision"],
            "control_seq": self.rt.execution_control.view(self.task)["seq"]})
        self.f.driver.steps[role.id] = [*steps, self.f.native_turn("", ("wait_for_reply", {}))]
        list(self.rt.team._run_message(self.task, message, role, started))
        finished, = [event for event in self.f.events("team.run_finished")
                     if event.payload["run_id"] == started.payload["run_id"]]
        self.assertEqual(finished.payload["status"], expected_status, finished.payload)
        self.assertEqual(self.f.protocol_errors, [])
        self.assertTrue(self.rt.verify_chain().ok)
        return started

    def last_reply(self, history):
        reply = next(message for message in reversed(history) if message.get("role") == "tool")
        return reply, json.loads(reply["content"])

    def test_limit_type_and_range_errors_are_fixed(self):
        self.invalid_values("limit", (None, True, False, 0, -1, 51, 1.5, "1", [], {}))

    def test_include_pending_requires_an_actual_boolean(self):
        self.invalid_values("include_pending", (None, 0, 1, "false", "QUERY_PRIVATE_VALUE", [], {}))

    def test_string_filters_reject_empty_wrong_type_and_overlong_values(self):
        for field in ("run_id", "role", "name"):
            with self.subTest(field=field):
                self.invalid_values(field, (None, True, False, 7, [], {}, "", "   ",
                                            "QUERY_PRIVATE_VALUE" * 8))

    def test_unknown_role_has_static_feedback_without_echoing_input(self):
        self.invalid_values("role", ("QUERY_PRIVATE_VALUE", "another-unconfigured-role"))

    def test_sequence_filters_require_positive_integers(self):
        for field in ("receipt_seq", "message_seq", "before_seq"):
            with self.subTest(field=field):
                self.invalid_values(field, (None, True, False, 0, -1, 1.0, "1", [], {}))

    def test_section_rejects_unknown_values_and_non_strings(self):
        self.invalid_values("section", (None, True, 1, [], {}, "", "QUERY_PRIVATE_VALUE"))

    def test_file_reads_rejects_presence_of_exact_or_pending_arguments(self):
        arguments = [{"section": "file_reads", "receipt_seq": 1},
                     {"section": "file_reads", "include_pending": False},
                     {"section": "file_reads", "include_pending": True},
                     {"section": "file_reads", "receipt_seq": 1, "include_pending": False}]
        for args, result in zip(arguments, self.s.exchange(*(("read_receipts", a) for a in arguments))):
            with self.subTest(arguments=args):
                self.assert_invalid(result, "section", *(key for key in ("receipt_seq", "include_pending")
                                                         if key in args))

    def test_file_path_requires_file_reads_and_rejects_unsafe_paths(self):
        wrong_modes = ({"file_path": "QUERY_PRIVATE_VALUE.txt"},
                       {"section": "receipts", "file_path": "QUERY_PRIVATE_VALUE.txt"})
        for result in self.s.exchange(*(("read_receipts", a) for a in wrong_modes)):
            self.assert_invalid(result, "file_path", "section")
        self.invalid_values("file_path", (
            None, True, 1, [], {}, "", " ", ".", "./", "../QUERY_PRIVATE_VALUE", "/QUERY_PRIVATE_VALUE",
            "C:\\QUERY_PRIVATE_VALUE", "C:QUERY_PRIVATE_VALUE", "\\\\server\\QUERY_PRIVATE_VALUE",
            "QUERY_PRIVATE_VALUE\x00.txt", "*.txt", "a/../QUERY_PRIVATE_VALUE", "NUL", ".env",
            "x" * 2049), section="file_reads")

    def test_exact_receipt_pending_conflict_can_be_corrected_with_false(self):
        self.s.put("original.txt", "EXACT_HISTORICAL_BODY\n")
        _, finish, original = self.s.read("original.txt")
        bad, good, omitted, pending = self.s.exchange(
            ("read_receipts", {"receipt_seq": finish.seq, "include_pending": True}),
            ("read_receipts", {"receipt_seq": finish.seq, "include_pending": False}),
            ("read_receipts", {"receipt_seq": finish.seq}),
            ("read_receipts", {"include_pending": True, "name": "workspace_read"}))
        self.assert_invalid(bad, "receipt_seq", "include_pending")
        for response in (good, omitted, pending):
            self.assertEqual(response["status"], "ok", response)
            self.assertEqual([row["seq"] for row in response["receipts"]], [finish.seq])
            self.assertEqual(response["receipts"][0]["result"], original)
            self.assertIs(response["record_read"]["reexecutes_operations"], False)
        self.assertEqual(pending["pending_operations"], [])
        self.assertNotIn("pending_operations", good)

    def test_schema_clarifies_exclusions_without_new_required_fields(self):
        self.s.exchange()
        tool, = [tool for tool in self.f.driver.calls[-1]["tools"] if tool["name"] == "read_receipts"]
        schema = tool["input_schema"]
        self.assertEqual(schema["required"], [])
        self.assertEqual(set(schema["properties"]), {"run_id", "role", "section", "receipt_seq",
            "file_path", "message_seq", "name", "before_seq", "include_pending", "limit"})
        exact = schema["properties"]["receipt_seq"]["description"]
        pending = schema["properties"]["include_pending"]["description"]
        self.assertIn("include_pending", exact)
        self.assertIn("file_reads", exact)
        self.assertIn("receipt_seq", pending)
        self.assertIn("file_reads", pending)
        self.assertIn("file_reads", schema["properties"]["file_path"]["description"])

    def test_valid_filters_pagination_and_metadata_still_read_saved_records(self):
        self.s.put("observed.txt", "SAVED_FILE_BODY\n")
        first = self.s.read("observed.txt")
        second = self.s.read("observed.txt")
        self.s.put("observed.txt", "CURRENT_UNOBSERVED_BODY\n")
        page, older, exact, metadata, empty = self.s.exchange(
            ("read_receipts", {"name": "workspace_read", "limit": 1}),
            ("read_receipts", {"name": "workspace_read", "before_seq": second[1].seq, "limit": 50}),
            ("read_receipts", {"receipt_seq": first[1].seq, "run_id": first[0].payload["run_id"],
                "role": self.f.worker, "message_seq": first[0].payload["message_seq"], "name": "workspace_read"}),
            ("read_receipts", {"section": "file_reads", "file_path": "observed.txt"}),
            ("read_receipts", {"receipt_seq": first[1].seq, "name": "workspace_write"}))
        self.assertEqual([row["seq"] for row in page["receipts"]], [second[1].seq])
        self.assertIs(page["has_more"], True)
        self.assertEqual(page["next_before_seq"], second[1].seq)
        self.assertEqual([row["seq"] for row in older["receipts"]], [first[1].seq])
        self.assertEqual(exact["receipts"][0]["result"], first[2])
        self.assertEqual([row["seq"] for row in self.s.file_rows(metadata)], [first[1].seq, second[1].seq])
        self.assertNotIn("SAVED_FILE_BODY", json.dumps(metadata))
        self.assertNotIn("CURRENT_UNOBSERVED_BODY", json.dumps((page, older, exact, metadata)))
        self.assertEqual(empty["receipts"], [])
        self.assertEqual(len(self.starts("workspace_read")), 2)

    def test_native_same_run_correction_receives_history_without_reexecution(self):
        finish, original = self.seed_write()
        (self.s.project / "effect.txt").write_text("LATER_DISK_STATE\n", encoding="utf-8")
        seen = []

        def correct(context, history, tools):
            reply, result = self.last_reply(history)
            self.assertIs(reply["is_error"], True)
            self.assert_invalid(result, "receipt_seq", "include_pending")
            seen.append(result)
            return self.f.native_turn("", ("read_receipts", {"receipt_seq": finish.seq}))

        def inspect(context, history, tools):
            reply, result = self.last_reply(history)
            self.assertIs(reply["is_error"], False)
            self.assertEqual([row["seq"] for row in result["receipts"]], [finish.seq])
            self.assertEqual(result["receipts"][0]["result"], original)
            self.assertIs(result["record_read"]["reexecutes_operations"], False)
            seen.append(result)
            return self.f.native_turn("", ("wait_for_reply", {}))

        run = self.native_script([self.f.native_turn("", ("read_receipts", {
            "receipt_seq": finish.seq, "include_pending": True})), correct, inspect])
        self.assertEqual(len(seen), 2)
        queries = [event for event in self.starts("read_receipts")
                   if event.payload["run_id"] == run.payload["run_id"]]
        self.assertEqual(len(queries), 2)
        self.assertEqual(len(self.starts("workspace_write")), 1)
        self.assertEqual((self.s.project / "effect.txt").read_text(encoding="utf-8"), "LATER_DISK_STATE\n")
        self.assertGreaterEqual(self.f.validated_continuations, 2)

    def external_correction(self, *, catalog=False):
        host = self.compose(external.TeamExternalTests)
        catalog_fixture = self.compose(catalogs.ExternalCatalogRunTests) if catalog else None
        forwarded = []
        observed = []

        def script(run):
            def execute(call):
                forwarded.append(call)
                result = run.execute(call)
                run.receipts.append((call, result))
                run.check_current()
                return result

            adapter = (catalog_fixture.run_for(execute=execute, check=run.check_current, tools=run.tools)
                       if catalog else None)
            if adapter is not None:
                described = adapter._catalog_call(ToolCall("receipt-definition", "describe_tools",
                    {"names": ["read_receipts"]}))
                definition, = described["definitions"]
                original_definition, = [tool for tool in run.tools if tool["name"] == "read_receipts"]
                self.assertEqual(definition, original_definition)
                self.assertEqual(forwarded, [])

            def call(name, arguments):
                if adapter is None:
                    return run.call(name, **arguments)
                return adapter._catalog_call(ToolCall(f"catalog-feedback-{len(forwarded)}", "invoke_tool",
                    {"name": name, "arguments": arguments}))

            original = call("read_messages", {"limit": 1})
            self.assertEqual(original["status"], "ok", original)
            finish, = [e for e in host.events("team.tool_finished") if e.payload["name"] == "read_messages"]
            bad = call("read_receipts", {"receipt_seq": finish.seq, "include_pending": True})
            self.assert_invalid(bad, "receipt_seq", "include_pending")
            good = call("read_receipts", {"receipt_seq": finish.seq})
            self.assertEqual(good["status"], "ok", good)
            self.assertEqual([row["seq"] for row in good["receipts"]], [finish.seq])
            self.assertEqual(good["receipts"][0]["result"], original)
            self.assertIs(good["record_read"]["reexecutes_operations"], False)
            observed.extend((bad, good))
            return "Saved receipt inspected offline."

        host.driver.scripts = {host.manager: [script]}
        host.receive()
        host.drain()
        self.assertEqual(len(observed), 2)
        self.assertEqual(host.results("read_receipts"), observed)
        self.assertEqual(len(self.starts("read_messages", fixture=host)), 1)
        query_starts = self.starts("read_receipts", fixture=host)
        self.assertEqual(len({event.payload["run_id"] for event in query_starts}), 1)
        self.assertTrue(all("external_started_seq" in event.payload for event in query_starts))
        if catalog:
            self.assertEqual([call.name for call in forwarded], ["read_messages", "read_receipts", "read_receipts"])
            self.assertEqual(forwarded[1].arguments, {"receipt_seq": forwarded[2].arguments["receipt_seq"],
                                                     "include_pending": True})
            self.assertEqual(self.starts("invoke_tool", fixture=host), [])
        else:
            self.assertEqual([result for call, result in host.driver.runs[0].receipts
                              if call.name == "read_receipts"], observed)

    def test_external_host_preserves_error_and_corrected_historical_receipt(self):
        self.external_correction()

    def test_catalog_forwards_original_arguments_to_external_host(self):
        self.external_correction(catalog=True)

    def test_internal_value_and_os_errors_remain_generic_without_secret_text(self):
        responses = []
        for error in (ValueError, OSError):
            with self.subTest(error=error.__name__):
                with patch.object(self.rt.team.read_sources, "exact", side_effect=error(
                        "QUERY_PRIVATE_VALUE C:\\private\\token SELECT secret FROM events")) as read:
                    result = self.s.query({"receipt_seq": 1})
                read.assert_called_once()
                self.assertEqual(result["status"], "error", result)
                self.assertTrue(result.get("message"), result)
                self.assertNotEqual(result.get("code"), "invalid_receipt_query")
                self.assertNotEqual(result.get("outcome"), "not_started")
                self.assertNotIn("fields", result)
                for marker in ("QUERY_PRIVATE_VALUE", "private", "token", "SELECT", "ValueError", "OSError"):
                    self.assertNotIn(marker, json.dumps(result))
                responses.append(result)
        self.assertEqual(responses[0], responses[1])

    def test_failure_after_applied_write_keeps_unknown_effect_and_pending_receipt(self):
        original_execute = WorkspaceExecutor.execute
        for error in (ValueError, OSError):
            with self.subTest(error=error.__name__):
                fixture = self.compose(sources.FileReadSourcesTests)
                effects = []

                def fail_after_write(executor, name, arguments):
                    result = original_execute(executor, name, arguments)
                    if name == "workspace_write":
                        self.assertEqual(result["status"], "ok", result)
                        effects.append(name)
                        raise error("QUERY_PRIVATE_VALUE after external effect")
                    return result

                with patch.object(WorkspaceExecutor, "execute", new=fail_after_write):
                    result, = fixture.exchange(("workspace_write", {"path": "applied.txt",
                        "content": "APPLIED_BEFORE_FAILURE\n", "expected_sha256": None}))
                self.assertEqual(result["status"], "error", result)
                self.assertIs(result["complete"], False)
                self.assertEqual(result["outcome"], "effects_unknown")
                self.assertNotEqual(result.get("code"), "invalid_receipt_query")
                self.assertNotIn("QUERY_PRIVATE_VALUE", json.dumps(result))
                self.assertEqual((fixture.project / "applied.txt").read_text(encoding="utf-8"),
                                 "APPLIED_BEFORE_FAILURE\n")
                start, = self.starts("workspace_write", fixture=fixture.f)
                finish, = [e for e in fixture.f.events("team.tool_finished")
                           if e.payload["name"] == "workspace_write"]
                saved = fixture.query({"receipt_seq": finish.seq})
                self.assertEqual(saved["receipts"][0]["result"], result)
                self.assertIn(start.seq, [e.seq for e in fixture.f.rt.workspaces.pending(task_id=fixture.f.task)])
                self.assertEqual(effects, ["workspace_write"])
                self.assertEqual(len(self.starts("workspace_write", fixture=fixture.f)), 1)

    def test_foreign_and_paused_queries_preserve_read_scope_and_authority(self):
        self.s.put("scoped.txt", "PRIVATE_LOCAL_RECEIPT_BODY\n")
        _, finish, original = self.s.read("scoped.txt")
        other = self.rt.create_task("Independent receipt scope")
        self.rt.team.enable(other)
        binding = self.rt.workspaces.view(self.task)
        self.rt.workspaces.bind(other, binding["workspace_id"], expected_revision=None)
        self.assertEqual(self.s.query({"receipt_seq": finish.seq}, task=other)["receipts"], [])
        metadata = self.s.query({"section": "file_reads"}, task=other)
        self.assertEqual(self.s.file_rows(metadata), [])
        self.assertNotIn("PRIVATE_LOCAL_RECEIPT_BODY", json.dumps(metadata))
        self.rt.execution_control.change(self.task, "pause", request_id="receipt-feedback-pause")
        mutations = self.starts("workspace_write") + self.starts("workspace_run")
        bad = self.s.query({"receipt_seq": finish.seq, "include_pending": True})
        self.assert_invalid(bad, "receipt_seq", "include_pending")
        good = self.s.query({"receipt_seq": finish.seq})
        self.assertEqual(good["receipts"][0]["result"], original)
        self.assertEqual(self.rt.execution_control.view(self.task)["state"], "paused")
        self.assertEqual(self.starts("workspace_write") + self.starts("workspace_run"), mutations)
        tools = {tool["name"] for tool in self.f.driver.calls[-1]["tools"]}
        self.assertIn("read_receipts", tools)
        self.assertNotIn("workspace_write", tools)
        self.assertNotIn("workspace_run", tools)
        self.assertEqual((self.s.project / "scoped.txt").read_text(encoding="utf-8"), original["content"])

    def test_new_input_prevents_stale_correction_from_dispatching(self):
        finish, original = self.seed_write()
        seen = []

        def supersede(context, history, tools):
            _, result = self.last_reply(history)
            self.assert_invalid(result, "receipt_seq", "include_pending")
            seen.append(result)
            role = self.rt.team._role(self.f.worker)
            self.rt.team.receive(self.task, f"@{role.name} New input supersedes the old query")
            return self.f.native_turn("", ("read_receipts", {"receipt_seq": finish.seq}))

        run = self.native_script([self.f.native_turn("", ("read_receipts", {
            "receipt_seq": finish.seq, "include_pending": True})), supersede], expected_status="interrupted")
        self.assertEqual(len(seen), 1)
        queries = [event for event in self.starts("read_receipts")
                   if event.payload["run_id"] == run.payload["run_id"]]
        self.assertEqual(len(queries), 1)
        self.assertEqual(len(self.starts("workspace_write")), 1)
        self.assertEqual(finish.payload["result"], original)
        self.assertEqual((self.s.project / "effect.txt").read_text(encoding="utf-8"), "ORIGINAL_SAVED_EFFECT\n")


if __name__ == "__main__":
    unittest.main()
