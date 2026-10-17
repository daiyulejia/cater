"""Pending-operation joins preserve binding, ordering, and legacy equality."""

from __future__ import annotations

import unittest
from unittest.mock import patch

import test_simple_team as team_fixture

from cater.events import Actor, EventLog
from cater.team import HOST, LOCAL_EVENT_CONTRACT


MISSING = object()


class PendingOperationIndexTests(unittest.TestCase):
    setUp = team_fixture.SimpleTeamTests.setUp

    def emit(self, event_type, *, actor=HOST, task=None, **payload):
        return self.rt.log.emit(type=event_type, actor=actor, task_id=self.task if task is None else task,
            payload={key: value for key, value in payload.items() if value is not MISSING})

    def source(self, role=None):
        return self.emit("team.message_received", actor=Actor.human(),
                         recipient=self.manager if role is None else role, kind="message", text="Inspect")

    def begin_run(self, source, **changes):
        return self.emit("team.run_started", **{
            "run_id": "shared-run", "role": source.payload["recipient"],
            "message_seq": source.seq, **changes})

    def start(self, run=None, **changes):
        if run is None:
            run = self.begin_run(self.source())
        return self.emit("team.tool_started", **{
            **{key: run.payload.get(key) for key in ("run_id", "role", "message_seq")},
            "name": "publish_artifact", "call_id": "shared-call", "arguments": {"filename": "a.txt"},
            **changes})

    def finish(self, start, **changes):
        return self.emit("team.tool_finished", **{
            **{key: start.payload.get(key) for key in ("run_id", "role", "name")},
            "started_seq": start.seq, "result": {"status": "ok"}, **changes})

    def reconcile(self, start, *, kind="team.tool_reconciled", **changes):
        return self.emit(kind, **{
            "actor": Actor.human(), "started_seq": start.seq, "started_hash": start.hash,
            **{key: start.payload.get(key) for key in ("run_id", "role", "name", "call_id")},
            "invocation_stopped": True, **changes})

    def rows(self):
        count = self.rt.log.count()
        rows = self.rt.team._pending_operations(self.task)
        self.assertEqual(self.rt.log.count(), count)
        self.assertEqual(self.driver.calls, [])
        return {row["started_seq"]: row for row in rows}

    def tearDown(self):
        self.assertTrue(self.rt.log.verify_chain().ok)

    def test_shared_run_and_call_ids_do_not_merge_distinct_starts(self):
        first = self.start()
        second = self.start()
        worker = self.start(self.begin_run(self.source(self.worker)))
        other_name = self.start(name="publish_workspace_file")
        self.assertEqual(set(self.rows()), {first.seq, second.seq, worker.seq, other_name.seq})
        self.finish(second)
        self.assertEqual(set(self.rows()), {first.seq, worker.seq, other_name.seq})
        self.finish(worker, call_id="shared-call")
        self.assertEqual(set(self.rows()), {first.seq, other_name.seq})
        self.finish(other_name)
        self.assertEqual(set(self.rows()), {first.seq})

    def test_run_binding_never_combines_message_and_role_from_different_runs(self):
        source = self.source()
        other = self.source()
        self.begin_run(other)
        wrong_role = self.begin_run(source, role=self.worker)
        start = self.start(wrong_role, role=self.manager)
        self.finish(start)
        self.emit("team.run_finished", run_id="shared-run", role=self.manager,
                  message_seq=other.seq, status="completed")
        row = self.rows()[start.seq]
        self.assertEqual(row["message_seq"], source.seq)
        self.assertFalse(row["run_ended"])
        self.assertEqual(row["outcome"], "unsettled")
        self.assertNotIn("finished_seq", row)

    def test_endings_require_full_run_key_and_follow_the_tool_start(self):
        run = self.begin_run(self.source())
        self.emit("team.run_finished", **run.payload, status="completed")
        start = self.start(run, name="send_message", execution_contract=dict(LOCAL_EVENT_CONTRACT))
        for changes in ({"run_id": "other"}, {"role": self.worker},
                        {"message_seq": self.source().seq}, {"actor": Actor.human()},
                        {"status": "running"}, {"status": []}):
            with self.subTest(changes=changes):
                self.emit("team.run_finished", **{**run.payload, "status": "completed", **changes})
                self.assertFalse(self.rows()[start.seq]["run_ended"])
        self.emit("team.run_finished", **run.payload, status="interrupted")
        row = self.rows()[start.seq]
        self.assertTrue(row["run_ended"])
        self.assertEqual(row["outcome"], "not_committed")

    def test_run_must_follow_source_and_precede_tool_start(self):
        future_source = self.rt.log.count() + 2
        run = self.emit("team.run_started", run_id="shared-run", role=self.manager,
                        message_seq=future_source)
        source = self.source()
        self.assertEqual(source.seq, future_source)
        start = self.start(run)
        self.begin_run(source)
        self.finish(start)
        self.assertNotIn("finished_seq", self.rows()[start.seq])

    def test_later_valid_run_candidate_survives_an_earlier_invalid_same_key(self):
        future_source = self.rt.log.count() + 2
        self.emit("team.run_started", run_id="shared-run", role=self.manager, message_seq=future_source)
        source = self.source()
        self.assertEqual(source.seq, future_source)
        run = self.begin_run(source)
        start = self.start(run)
        self.finish(start)
        self.assertNotIn(start.seq, self.rows())

    def test_source_recipient_and_exact_run_actor_are_required(self):
        for changes in ({"actor": Actor.human()}, {"actor": Actor.system("other")},
                        {"actor": Actor(HOST.id, "other", HOST.kind)}, {}):
            with self.subTest(changes=changes):
                source = self.source(self.worker if not changes else self.manager)
                run = self.begin_run(source, role=self.manager, **changes)
                start = self.start(run)
                self.finish(start)
                self.assertNotIn("finished_seq", self.rows()[start.seq])

    def test_first_legal_unknown_finish_wins_over_later_complete_finish(self):
        start = self.start()
        first = self.finish(start, result={"status": "error", "complete": False})
        self.finish(start, result={"status": "ok"})
        row = self.rows()[start.seq]
        self.assertEqual(row["finished_seq"], first.seq)
        self.assertFalse(row["run_ended"])
        self.assertEqual(row["outcome"], "unsettled")
        self.reconcile(start)
        self.assertNotIn(start.seq, self.rows())

    def test_first_legal_complete_finish_wins_over_later_unknown_finish(self):
        start = self.start()
        self.finish(start, result={"status": "error", "error": {"code": "invalid_argument"}})
        self.finish(start, result={"status": "ok", "complete": False})
        self.assertNotIn(start.seq, self.rows())

    def test_early_forged_misbound_and_malformed_finishes_are_skipped(self):
        run = self.begin_run(self.source())
        next_start = self.rt.log.count() + 2
        self.emit("team.tool_finished", started_seq=next_start, run_id="shared-run", role=self.manager,
                  name="publish_artifact", result={"status": "ok"})
        start = self.start(run)
        self.assertEqual(start.seq, next_start)
        invalid = ({"actor": Actor.human()}, {"actor": Actor(HOST.id, "other", HOST.kind)},
                   {"actor": Actor("other", HOST.role, HOST.kind)},
                   {"actor": Actor(HOST.id, HOST.role, "agent")},
                   {"run_id": "other"}, {"role": self.worker}, {"name": "publish_workspace_file"},
                   {"call_id": "other"}, {"started_seq": float(start.seq)},
                   {"started_seq": True}, {"started_seq": []}, {"started_seq": {}},
                   {"result": None}, {"result": []}, {"result": {}},
                   {"result": {"status": []}}, {"result": {"status": {}}})
        for changes in invalid:
            with self.subTest(changes=changes):
                self.finish(start, **changes)
                self.assertNotIn("finished_seq", self.rows()[start.seq])
        first = self.finish(start, call_id="shared-call", result={"status": "ok", "outcome": "timeout"})
        self.finish(start)
        self.assertEqual(self.rows()[start.seq]["finished_seq"], first.seq)

    def test_foreign_task_finish_and_reconcile_cannot_close_start(self):
        start = self.start()
        other = self.rt.create_task("Other task")
        self.finish(start, task=other)
        self.reconcile(start, task=other)
        self.assertIn(start.seq, self.rows())
        self.finish(start)
        self.assertNotIn(start.seq, self.rows())

    def test_reconcile_requires_exact_human_hash_call_and_stopped(self):
        for name, kind in (("publish_artifact", "team.tool_reconciled"),
                           ("workspace_write", "workspace.tool_reconciled")):
            start = self.start(name=name)
            invalid = ({"started_hash": MISSING}, {"started_hash": "wrong"}, {"started_hash": []},
                       {"call_id": MISSING}, {"call_id": "other"}, {"call_id": {}},
                       {"invocation_stopped": MISSING}, {"invocation_stopped": False},
                       {"invocation_stopped": 1}, {"invocation_stopped": "true"},
                       {"actor": HOST}, {"actor": Actor("human", "other", "human")},
                       {"actor": Actor("other", "human", "human")},
                       {"actor": Actor("human", "human", "agent")},
                       {"started_seq": float(start.seq)}, {"started_seq": True},
                       {"started_seq": []}, {"started_seq": {}})
            for changes in invalid:
                with self.subTest(name=name, changes=changes):
                    self.reconcile(start, kind=kind, **changes)
                    self.assertIn(start.seq, self.rows())
            self.reconcile(start, kind=kind)
            self.assertNotIn(start.seq, self.rows())

    def test_reconcile_family_and_team_binding_cannot_cross(self):
        publication = self.start()
        workspace = self.start(name="workspace_write")
        self.reconcile(publication, kind="workspace.tool_reconciled")
        self.reconcile(workspace)
        for field in ("run_id", "role", "name"):
            for value in ("other", [], {}):
                with self.subTest(field=field, value=value):
                    self.reconcile(publication, **{field: value})
                    self.assertIn(publication.seq, self.rows())
        self.assertIn(workspace.seq, self.rows())
        self.reconcile(workspace, kind="workspace.tool_reconciled", run_id=MISSING, role=MISSING, name=MISSING)
        self.assertNotIn(workspace.seq, self.rows())

    def test_early_reconcile_cannot_close_a_future_start(self):
        run = self.begin_run(self.source())
        next_start = self.rt.log.count() + 2
        self.emit("team.tool_reconciled", actor=Actor.human(), started_seq=next_start,
                  started_hash="unavailable", call_id="shared-call", run_id="shared-run",
                  role=self.manager, name="publish_artifact", invocation_stopped=True)
        start = self.start(run)
        self.assertEqual(start.seq, next_start)
        self.assertIn(start.seq, self.rows())

    def test_malformed_start_identities_cannot_bind_identically_malformed_receipts(self):
        for field in ("run_id", "role", "name", "call_id", "message_seq"):
            for value in (MISSING, None, "", " ", True, 1.0, [], {}):
                with self.subTest(field=field, value=value):
                    start = self.start(**{field: value})
                    self.finish(start, call_id=start.payload.get("call_id"))
                    row = self.rows()[start.seq]
                    self.assertNotIn("finished_seq", row)
                    self.assertFalse(row["run_ended"])

    def test_unhashable_run_identities_and_receipt_bindings_are_ignored(self):
        run = self.begin_run(self.source())
        start = self.start(run)
        for field in ("run_id", "role", "message_seq"):
            for value in ([], {}, None, MISSING):
                with self.subTest(field=field, value=value):
                    payload = {**run.payload, field: value}
                    self.emit("team.run_started", **payload)
                    self.emit("team.run_finished", **payload, status="completed")
                    self.assertFalse(self.rows()[start.seq]["run_ended"])
        for field in ("run_id", "role", "name", "call_id"):
            for value in ([], {}):
                self.finish(start, **{field: value})
        self.assertNotIn("finished_seq", self.rows()[start.seq])
        self.finish(start)
        self.assertNotIn(start.seq, self.rows())

    def test_legacy_numeric_run_message_seq_equality_is_preserved(self):
        # A separate real log puts the source at seq=1, where True aliases int.
        log = EventLog(self.root / "numeric-events.db")
        self.addCleanup(log.close)
        with patch.object(self.rt, "log", log):
            source = self.source()
            self.assertEqual(source.seq, 1)
            for numeric in (True, 1.0):
                with self.subTest(numeric=numeric):
                    run = self.begin_run(source, run_id=f"numeric-{type(numeric).__name__}", message_seq=numeric)
                    start = self.start(run, message_seq=source.seq)
                    self.emit("team.run_finished", **run.payload, status="completed")
                    row = self.rows()[start.seq]
                    self.assertTrue(row["run_ended"])
                    self.assertEqual(row["outcome"], "unknown")
                    self.finish(start)
                    self.assertNotIn(start.seq, self.rows())
            self.assertTrue(log.verify_chain().ok)

    def test_numeric_tool_message_seq_is_still_strict_integer(self):
        log = EventLog(self.root / "strict-tool-events.db")
        self.addCleanup(log.close)
        with patch.object(self.rt, "log", log):
            source = self.source()
            self.assertEqual(source.seq, 1)
            run = self.begin_run(source)
            for numeric in (True, 1.0):
                with self.subTest(numeric=numeric):
                    start = self.start(run, message_seq=numeric)
                    self.finish(start)
                    self.assertNotIn("finished_seq", self.rows()[start.seq])
            self.assertTrue(log.verify_chain().ok)

    def test_pending_row_contract_and_order_survive_repeated_reads(self):
        run = self.begin_run(self.source())
        local = self.start(run, name="send_message", execution_contract=dict(LOCAL_EVENT_CONTRACT))
        external = self.start(run, call_id="external-call")
        self.emit("team.run_finished", **run.payload, status="completed")
        first = self.finish(external, result={"status": "error", "outcome": "effects_unknown"})
        expected = [
            {"seq": local.seq, "started_seq": local.seq, "run_id": "shared-run", "role": self.manager,
             "message_seq": run.payload["message_seq"], "name": "send_message",
             "arguments": {"filename": "a.txt"}, "outcome": "not_committed", "run_ended": True,
             "replay_authorized": False},
            {"seq": external.seq, "started_seq": external.seq, "run_id": "shared-run", "role": self.manager,
             "message_seq": run.payload["message_seq"], "name": "publish_artifact",
             "arguments": {"filename": "a.txt"}, "outcome": "unknown", "run_ended": True,
             "replay_authorized": False, "finished_seq": first.seq},
        ]
        self.assertEqual(list(self.rows().values()), expected)
        self.assertEqual(list(self.rows().values()), expected)

    def test_read_receipts_pending_pagination_preserves_filters_and_cursor(self):
        run = self.begin_run(self.source())
        starts = [self.start(run, call_id=f"page-{index}") for index in range(5)]
        self.start(run, name="publish_workspace_file")
        reader = self.start(run, name="read_receipts")
        arguments = {"include_pending": True, "limit": 2, "run_id": "shared-run",
                     "role": self.manager, "message_seq": run.payload["message_seq"],
                     "name": "publish_artifact"}

        def read(**changes):
            count = self.rt.log.count()
            result = self.rt.team._execute(self.task, self.rt.organization.leader, "read_receipts",
                {**arguments, **changes}, None, self.rt.workspaces.view(self.task), reader, None)
            self.assertEqual(self.rt.log.count(), count)
            return result

        page = read()
        self.assertEqual(page["status"], "ok")
        self.assertEqual([row["seq"] for row in page["pending_operations"]], [e.seq for e in starts[-2:]])
        self.assertTrue(page["pending_has_more"])
        self.assertEqual(page["pending_next_before_seq"], starts[-2].seq)
        page = read(before_seq=page["pending_next_before_seq"])
        self.assertEqual([row["seq"] for row in page["pending_operations"]], [e.seq for e in starts[1:3]])
        page = read(before_seq=page["pending_next_before_seq"])
        self.assertEqual([row["seq"] for row in page["pending_operations"]], [starts[0].seq])
        self.assertFalse(page["pending_has_more"])
        self.assertIsNone(page["pending_next_before_seq"])
        self.assertEqual(self.driver.calls, [])

    def test_history_event_visits_remain_linear_for_128_tool_calls(self):
        run = self.begin_run(self.source())
        starts = []
        for index in range(128):
            start = self.start(run, call_id=f"linear-{index}")
            starts.append(start)
            self.finish(start, result={"status": "ok", "complete": False})
        event_count = len(self.rt.log.read(task_id=self.task))
        visits = [0]

        class CountedEvents(list):
            def __iter__(self):
                for event in super().__iter__():
                    visits[0] += 1
                    yield event

        read = self.rt.log.read

        def counted_read(*args, **kwargs):
            return CountedEvents(read(*args, **kwargs))

        # Observe both team and workspace reads; all event and receipt checks run normally.
        with patch.object(self.rt.log, "read", side_effect=counted_read):
            rows = self.rows()
        self.assertEqual(list(rows), [start.seq for start in starts])
        self.assertTrue(all("finished_seq" in row for row in rows.values()))
        self.assertGreaterEqual(visits[0], event_count)
        self.assertLessEqual(visits[0], 40 * event_count,
                             f"History rescanned per tool: {visits[0]} visits for {event_count} events")

    def test_read_receipts_and_untrusted_tool_starts_are_excluded(self):
        run = self.begin_run(self.source())
        self.start(run, name="read_receipts")
        self.start(run, actor=Actor.human())
        self.start(run, actor=Actor(HOST.id, "other", HOST.kind))
        self.assertEqual(self.rows(), {})


if __name__ == "__main__":
    unittest.main()
