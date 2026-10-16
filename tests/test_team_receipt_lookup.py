"""Receipt discovery regressions with disposable logs and native fake-model turns."""

from __future__ import annotations

import json
import unittest

from cater.events import Actor
from cater.team import HOST
from test_team_supervision import TeamSupervisionTests


class TeamReceiptLookupTests(TeamSupervisionTests):
    def setUp(self):
        super().setUp()
        self._fixture_run = None

    def close_fixture_run(self):
        if self._fixture_run is not None:
            run = self._fixture_run
            self.rt.log.emit(type="team.run_finished", actor=HOST, task_id=run.task_id, payload={
                **run.payload, "status": "completed", "reason": "Fixture run"})
            self._fixture_run = None

    def receipt_run(self, *, role=None, task_id=None, message=None, run_id=None):
        self.close_fixture_run()
        role = role or self.worker
        task_id = task_id or self.task
        if message is None:
            message = self.rt.log.emit(
                type="team.message_received", actor=self.rt._leader_actor(), task_id=task_id,
                payload={"recipient": role, "text": "Inspect fixture evidence", "kind": "assignment",
                         "revision": self.rt.team.view(task_id)["revision"]})
        run_id = run_id or f"lookup-run-{message.seq}-{len(self.rt.log.read(task_id=task_id))}"
        run = self.rt.log.emit(type="team.run_started", actor=HOST, task_id=task_id, payload={
            "message_seq": message.seq, "role": role, "run_id": run_id,
            "control_seq": self.rt.execution_control.view(task_id)["seq"]})
        self._fixture_run = run
        return run, message

    def receipt(self, run, *, name="workspace_run", result=None, actor=HOST):
        if result is None:
            result = {"status": "ok", "stdout": f"fixture-output-{run.seq}"}
        started = self.rt.log.emit(type="team.tool_started", actor=actor, task_id=run.task_id,
            payload={"run_id": run.payload["run_id"], "role": run.payload["role"],
                     "message_seq": run.payload["message_seq"], "name": name,
                     "call_id": f"lookup-call-{len(self.rt.log.read())}", "arguments": {}})
        # Production tool_finished omits message_seq; discovery must join the trusted start.
        finished = self.rt.log.emit(type="team.tool_finished", actor=actor, task_id=run.task_id,
            payload={"started_seq": started.seq, "run_id": run.payload["run_id"],
                     "role": run.payload["role"], "name": name,
                     "call_id": started.payload["call_id"], "result": result})
        return started, finished

    def queries(self, *arguments):
        self.close_fixture_run()
        previous = len(self.results("read_receipts"))
        self.driver.steps[self.manager] = [
            self.native_turn("", *(("read_receipts", args) for args in arguments)),
            self.native_turn("Receipt lookup complete."),
        ]
        self.rt.team.receive(self.task, "Inspect receipt evidence",
                             request_id=f"lookup-query-{len(self.driver.calls)}")
        self.run_team()
        results = self.results("read_receipts")[previous:]
        self.assertEqual(len(results), len(arguments), "Each lookup must settle as a tool result")
        return results

    def context(self):
        self.close_fixture_run()
        first = len(self.driver.calls)
        self.driver.steps[self.manager] = [self.native_turn("Evidence index inspected.")]
        self.rt.team.receive(self.task, "Inspect the evidence index",
                             request_id=f"lookup-context-{first}")
        self.run_team()
        return self.driver.calls[first]["context"]

    def assert_page(self, result, expected, *, has_more=False):
        self.assertEqual(result["status"], "ok")
        seqs = [row["seq"] for row in result["receipts"]]
        self.assertEqual(seqs, [event.seq for event in expected])
        self.assertEqual(seqs, sorted(seqs))
        self.assertIs(result["has_more"], has_more)
        self.assertEqual(result["next_before_seq"], expected[0].seq if has_more else None)

    def test_tool_schema_exposes_lookup_fields(self):
        self.queries({})
        tool, = [tool for tool in self.driver.calls[0]["tools"] if tool["name"] == "read_receipts"]
        schema = tool["input_schema"]
        self.assertEqual(schema["required"], [])
        for field in ("run_id", "role", "name"):
            with self.subTest(field=field):
                self.assertEqual(schema["properties"][field]["type"], "string")
        for field in ("message_seq", "before_seq", "limit"):
            with self.subTest(field=field):
                self.assertEqual(schema["properties"][field]["type"], "integer")
                self.assertEqual(schema["properties"][field]["minimum"], 1)

    def test_role_selects_all_runs_for_the_same_role(self):
        first, _ = self.receipt_run()
        _, one = self.receipt(first)
        other, _ = self.receipt_run(role=self.manager)
        _, excluded = self.receipt(other)
        second, _ = self.receipt_run()
        _, two = self.receipt(second)
        result, exact = self.queries({"role": self.worker}, {"run_id": second.payload["run_id"]})
        self.assert_page(result, [one, two])
        self.assertEqual({row["run_id"] for row in result["receipts"]},
                         {first.payload["run_id"], second.payload["run_id"]})
        self.assertNotIn(excluded.seq, [row["seq"] for row in result["receipts"]])
        self.assert_page(exact, [two])

    def test_message_seq_comes_from_started_and_can_span_runs(self):
        first, message = self.receipt_run()
        _, one = self.receipt(first)
        second, _ = self.receipt_run(message=message)
        _, two = self.receipt(second)
        unrelated, _ = self.receipt_run()
        self.receipt(unrelated)
        self.assertNotIn("message_seq", one.payload)
        result, = self.queries({"message_seq": message.seq})
        self.assert_page(result, [one, two])
        self.assertEqual([row["message_seq"] for row in result["receipts"]], [message.seq] * 2)

    def test_name_filter_and_full_reads_of_other_reading_tools(self):
        run, _ = self.receipt_run()
        _, execution = self.receipt(run)
        reads = {}
        for name in ("read_messages", "read_artifact", "workspace_read", "workspace_list", "workspace_search"):
            _, reads[name] = self.receipt(run, name=name,
                result={"status": "ok", "content": f"complete-{name}-body"})
        self.receipt(run, name="read_receipts", result={"status": "ok", "receipts": []})
        names = ["workspace_run", *reads, "read_receipts"]
        results = self.queries(*({"name": name} for name in names))
        for name, result in zip(names, results):
            with self.subTest(name=name):
                expected = [execution] if name == "workspace_run" else [reads[name]] if name in reads else []
                self.assert_page(result, expected)
                if name in reads:
                    self.assertEqual(result["receipts"][0]["result"], reads[name].payload["result"])

    def test_filters_are_anded_before_limit_and_cursor(self):
        run, message = self.receipt_run()
        _, one = self.receipt(run)
        self.receipt(run, name="workspace_write")
        _, two = self.receipt(run)
        _, boundary = self.receipt(run)
        other, _ = self.receipt_run(role=self.manager)
        self.receipt(other)
        filters = {"run_id": run.payload["run_id"], "role": self.worker,
                   "message_seq": message.seq, "name": "workspace_run",
                   "before_seq": boundary.seq, "limit": 1}
        good, wrong_role, wrong_message, wrong_name = self.queries(
            filters, {**filters, "role": self.manager},
            {**filters, "message_seq": other.payload["message_seq"]},
            {**filters, "name": "publish_artifact"})
        self.assert_page(good, [two], has_more=True)
        self.assertEqual(good["next_before_seq"], two.seq)
        for result in (wrong_role, wrong_message, wrong_name):
            self.assert_page(result, [])
        earlier, = self.queries({**filters, "before_seq": two.seq})
        self.assert_page(earlier, [one])

    def test_backward_pagination_collects_every_receipt_once(self):
        run, _ = self.receipt_run()
        expected = []
        for index in range(8):
            _, finished = self.receipt(run)
            expected.append(finished)
            if index % 2:
                self.receipt(run, name="read_receipts")
        seen = []
        cursor = None
        for end in (8, 5, 2):
            arguments = {"run_id": run.payload["run_id"], "limit": 3}
            if cursor is not None:
                arguments["before_seq"] = cursor
            page, = self.queries(arguments)
            self.assert_page(page, expected[max(0, end - 3):end], has_more=end > 3)
            seen.extend(row["seq"] for row in page["receipts"])
            cursor = page["next_before_seq"]
        self.assertIsNone(cursor)
        self.assertEqual(sorted(seen), [event.seq for event in expected])
        self.assertEqual(len(seen), len(set(seen)))
        empty, = self.queries({"run_id": run.payload["run_id"], "before_seq": expected[0].seq})
        self.assert_page(empty, [])

    def test_default_and_maximum_limit_return_the_latest_receipts(self):
        run, _ = self.receipt_run()
        expected = [self.receipt(run)[1] for _ in range(51)]
        default, maximum, single = self.queries({}, {"limit": 50}, {"limit": 1})
        self.assert_page(default, expected[-20:], has_more=True)
        self.assert_page(maximum, expected[-50:], has_more=True)
        self.assert_page(single, expected[-1:], has_more=True)

    def test_invalid_parameters_return_recoverable_errors(self):
        run, _ = self.receipt_run()
        _, finished = self.receipt(run)
        invalid = []
        for field in ("run_id", "role", "name"):
            invalid.extend({field: value} for value in (None, True, False, 7, [], {}, "", "   "))
        for field in ("message_seq", "before_seq", "limit"):
            invalid.extend({field: value} for value in (None, True, False, 0, -1, 1.5, "1", "", [], {}))
        invalid.extend(({"limit": 51}, {"role": "unknown-role"}, {"task_id": self.task}))
        results = self.queries(*invalid, {"run_id": run.payload["run_id"]})
        for arguments, result in zip(invalid, results[:-1]):
            with self.subTest(arguments=arguments):
                self.assertEqual(result["status"], "error")
                self.assertTrue(result.get("message"), "A parameter error must help the model recover")
        self.assert_page(results[-1], [finished])
        replies = [message for message in self.driver.calls[-1]["history"] if message.get("role") == "tool"]
        self.assertEqual(len(replies), len(results))
        self.assertTrue(all(message["is_error"] for message in replies[:-1]))
        self.assertFalse(replies[-1]["is_error"])

    def test_unknown_run_id_explains_role_and_message_filters(self):
        run, _ = self.receipt_run()
        self.receipt(run)
        for result in self.queries({"run_id": "test"}, {"run_id": self.worker},
                                   {"run_id": "never-started-in-this-task"}):
            self.assertEqual(result["status"], "error")
            self.assertIn("role", result["message"])
            self.assertIn("message_seq", result["message"])
            self.assertNotIn("fixture-output", json.dumps(result))

    def test_locally_started_run_without_receipts_is_valid_empty_page(self):
        run, _ = self.receipt_run()
        result, = self.queries({"run_id": run.payload["run_id"]})
        self.assert_page(result, [])

    def test_agent_claimed_run_is_not_a_valid_host_run_id(self):
        self.rt.log.emit(type="team.run_started", actor=Actor.agent(self.worker, "worker"),
            task_id=self.task, payload={"run_id": "agent-claimed-run", "role": self.worker,
                                       "message_seq": 1, "control_seq": None})
        result, = self.queries({"run_id": "agent-claimed-run"})
        self.assertEqual(result["status"], "error")
        self.assertIn("role", result["message"])
        self.assertIn("message_seq", result["message"])

    def test_queries_are_task_scoped_even_when_run_ids_collide(self):
        local, message = self.receipt_run(run_id="shared-fixture-run")
        _, visible = self.receipt(local, result={"status": "ok", "stdout": "LOCAL_LOOKUP_OUTPUT"})
        other = self.rt.create_task("Private receipt lookup fixture")
        self.rt.team.enable(other)
        foreign, foreign_message = self.receipt_run(task_id=other, run_id="shared-fixture-run")
        self.receipt(foreign, result={"status": "error", "stderr": "FOREIGN_LOOKUP_SECRET"})
        private, _ = self.receipt_run(task_id=other, run_id="private-foreign-run")
        self.receipt(private, result={"status": "error", "stderr": "FOREIGN_LOOKUP_SECRET"})
        results = self.queries({}, {"role": self.worker}, {"name": "workspace_run"},
            {"message_seq": message.seq}, {"run_id": "shared-fixture-run"},
            {"message_seq": foreign_message.seq}, {"run_id": "private-foreign-run"},
            {"task_id": other}, {"before_seq": 1000000})
        for index in (0, 1, 2, 3, 4, 8):
            self.assert_page(results[index], [visible])
        self.assert_page(results[5], [])
        self.assertEqual(results[6]["status"], "error")
        self.assertEqual(results[7]["status"], "error")
        self.assertNotIn("FOREIGN_LOOKUP_SECRET", json.dumps(results))
        self.assertNotIn("FOREIGN_LOOKUP_SECRET", json.dumps(self.driver.calls))

    def test_only_host_execution_receipts_are_discoverable(self):
        run, _ = self.receipt_run()
        _, visible = self.receipt(run)
        self.receipt(run, actor=Actor.agent(self.worker, "worker"),
                     result={"status": "ok", "stdout": "UNTRUSTED_RECEIPT_BODY"})
        self.rt.log.emit(type="workspace.tool_finished", actor=HOST, task_id=self.task,
            payload={"run_id": run.payload["run_id"], "name": "workspace_run",
                     "result": {"status": "ok", "stdout": "UNRELATED_EVENT_BODY"}})
        self.rt.log.emit(type="team.tool_started", actor=HOST, task_id=self.task,
            payload={"run_id": run.payload["run_id"], "role": self.worker,
                     "message_seq": run.payload["message_seq"], "name": "workspace_run",
                     "call_id": "unfinished-lookup-call", "arguments": {}})
        result, = self.queries({})
        self.assert_page(result, [visible])
        context = self.context()
        self.assertEqual([row["seq"] for row in context["receipt_index"]], [visible.seq])
        self.assertNotIn("UNTRUSTED_RECEIPT_BODY", json.dumps(self.driver.calls))
        self.assertNotIn("UNRELATED_EVENT_BODY", json.dumps(self.driver.calls))

    def test_context_index_is_lightweight_and_joins_started_message_seq(self):
        run, message = self.receipt_run()
        failure = {"status": "error", "outcome": "failed", "exit_code": 7,
                   "stdout": "LARGE_OUTPUT_SENTINEL" * 4000, "stderr": "PRIVATE_STDERR_SENTINEL",
                   "error": {"code": "failed", "message": "FULL_FAILURE_BODY"}}
        started, finished = self.receipt(run, result=failure)
        _, minimal = self.receipt(run, name="workspace_write", result={"status": "ok"})
        for name in ("read_receipts", "read_messages", "read_artifact",
                     "workspace_read", "workspace_list", "workspace_search"):
            self.receipt(run, name=name, result={"status": "ok", "content": "READ_BODY_SENTINEL"})
        other = self.rt.create_task("Private index fixture")
        self.rt.team.enable(other)
        foreign, _ = self.receipt_run(task_id=other)
        self.receipt(foreign, result={"status": "error", "stderr": "FOREIGN_INDEX_SENTINEL"})
        context = self.context()
        self.assertIs(context["receipts_truncated"], False)
        self.assertEqual(context["receipt_index"], [
            {"seq": finished.seq, "started_seq": started.seq, "run_id": run.payload["run_id"],
             "message_seq": message.seq, "role": self.worker, "name": "workspace_run",
             "status": "error", "outcome": "failed", "exit_code": 7},
            {"seq": minimal.seq, "started_seq": minimal.payload["started_seq"],
             "run_id": run.payload["run_id"], "message_seq": message.seq,
             "role": self.worker, "name": "workspace_write", "status": "ok"},
        ])
        serialized = json.dumps(context)
        for marker in ("LARGE_OUTPUT_SENTINEL", "PRIVATE_STDERR_SENTINEL", "FULL_FAILURE_BODY",
                       "READ_BODY_SENTINEL", "FOREIGN_INDEX_SENTINEL"):
            with self.subTest(marker=marker):
                self.assertNotIn(marker, serialized)
        self.assertLess(len(json.dumps(context["receipt_index"])), 1200)

    def test_context_index_truncation_counts_only_eligible_receipts(self):
        empty = self.context()
        self.assertEqual(empty["receipt_index"], [])
        self.assertIs(empty["receipts_truncated"], False)
        run, _ = self.receipt_run()
        eligible = []
        for _ in range(20):
            _, finished = self.receipt(run)
            eligible.append(finished)
        for name in ("read_receipts", "read_messages", "read_artifact",
                     "workspace_read", "workspace_list", "workspace_search"):
            for _ in range(8):
                self.receipt(run, name=name)
        exact = self.context()
        self.assertEqual([row["seq"] for row in exact["receipt_index"]], [event.seq for event in eligible])
        self.assertIs(exact["receipts_truncated"], False)
        _, newest = self.receipt(run)
        eligible.append(newest)
        truncated = self.context()
        self.assertEqual([row["seq"] for row in truncated["receipt_index"]],
                         [event.seq for event in eligible[-20:]])
        self.assertIs(truncated["receipts_truncated"], True)

    def test_index_run_id_retrieves_the_complete_failure_in_native_history(self):
        started, finished, failure = self.failed_receipt(self.task, "RECOVER_FULL_FAILURE_SENTINEL")
        observed = {}

        def discover(context, history, tools):
            index = context["receipt_index"]
            self.assertEqual(len(index), 1)
            reference = index[0]
            self.assertEqual(reference["seq"], finished.seq)
            self.assertEqual(reference["started_seq"], started.seq)
            self.assertEqual(reference["run_id"], finished.payload["run_id"])
            self.assertNotIn("RECOVER_FULL_FAILURE_SENTINEL", json.dumps(context))
            observed.update(reference)
            return self.native_turn("", ("read_receipts", {"run_id": reference["run_id"]}))

        def inspect(context, history, tools):
            reply, = [message for message in history if message.get("role") == "tool"]
            result = json.loads(reply["content"])
            self.assert_page(result, [finished])
            self.assertEqual(result["receipts"][0]["result"], failure)
            self.assertIn("RECOVER_FULL_FAILURE_SENTINEL-END", result["receipts"][0]["result"]["stdout"])
            self.assertEqual([row["seq"] for row in context["receipt_index"]], [finished.seq])
            return self.native_turn("The full failure output is available for correction.")

        self.driver.steps[self.manager] = [discover, inspect]
        self.rt.team.receive(self.task, "Find the exact failed run", request_id="lookup-index-to-full-failure")
        self.run_team()
        self.assertEqual(observed["run_id"], finished.payload["run_id"])
        self.assertEqual(len(self.results("read_receipts")), 1)
        self.assertGreater(self.validated_continuations, 0)


def load_tests(loader, tests, pattern):
    # Reuse supervision helpers without discovering its inherited regression tests.
    return unittest.TestSuite(TeamReceiptLookupTests(name) for name in sorted(
        name for name in TeamReceiptLookupTests.__dict__ if name.startswith("test_")))


if __name__ == "__main__":
    unittest.main()
