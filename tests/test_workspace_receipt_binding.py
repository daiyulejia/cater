"""Workspace pending joins use real event logs and disposable native tool runs."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import json
import tempfile
import unittest
from unittest.mock import patch

import test_simple_team as team_fixture
import test_workspace_integration as workspace_fixture

from cater.events import Actor, EventLog
from cater.workspace_tools import WorkspaceExecutor
from cater.workspaces import HOST, WorkspaceService


TEAM = Actor.system("team")
MISSING = object()


class WorkspaceReceiptBindingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="cater-receipt-binding-")
        self.addCleanup(temporary.cleanup)
        self.db = Path(temporary.name) / "events.db"
        self.log = EventLog(self.db)
        self.addCleanup(self.log.close)
        self.service = WorkspaceService(SimpleNamespace(log=self.log))

    def start(self, *, family="team", task="group-a", actor=None, **changes):
        payload = {"name": "workspace_write", "call_id": "call-a", "root": "shared-root"}
        if family == "team":
            payload.update(run_id="run-a", role="dev")
        payload.update(changes)
        payload = {key: value for key, value in payload.items() if value is not MISSING}
        return self.log.emit(type=f"{family}.tool_started", task_id=task,
            actor=actor if actor is not None else TEAM if family == "team" else HOST,
            payload=payload)

    def finish(self, start, *, kind=None, actor=None, task=MISSING, **changes):
        team = start.type == "team.tool_started"
        kind = kind or ("team.tool_finished" if team else "workspace.tool_finished")
        payload = {"started_seq": start.seq, "result": {"status": "ok"}}
        if kind == "workspace.tool_reconciled":
            payload.update(call_id=start.payload.get("call_id"), started_hash=start.hash,
                           invocation_stopped=True)
        elif team:
            payload.update({key: start.payload.get(key) for key in ("run_id", "role", "name")})
        else:
            payload["call_id"] = start.payload.get("call_id")
        payload.update(changes)
        payload = {key: value for key, value in payload.items() if value is not MISSING}
        return self.log.emit(type=kind,
            actor=actor if actor is not None else Actor.human() if kind == "workspace.tool_reconciled" else TEAM if team else HOST,
            task_id=start.task_id if task is MISSING else task, payload=payload)

    def assert_pending(self, start, expected=True):
        count = self.log.count()
        for filters in ({}, {"root": "shared-root"}, {"task_id": start.task_id}):
            self.assertEqual(start.seq in {event.seq for event in self.service.pending(**filters)}, expected)
        self.assertEqual(self.log.count(), count, "Reading pending must never replay or append events")

    def test_team_finish_matches_nonempty_run_role_name_and_optional_call_id(self):
        for field in ("run_id", "role", "name", "call_id"):
            values = ("other", None, "", " ", True, 1, 1.0, [], {})
            if field != "call_id":
                values += (MISSING,)
            for value in values:
                with self.subTest(field=field, value=value):
                    start = self.start()
                    self.finish(start, **{field: value})
                    self.assert_pending(start)

    def test_invalid_start_identity_cannot_match_identically_invalid_finish(self):
        for field in ("run_id", "role", "call_id"):
            for value in (MISSING, None, "", " ", True, 1, 1.0, [], {}):
                with self.subTest(field=field, value=value):
                    start = self.start(**{field: value})
                    extra = {} if field != "call_id" else {"call_id": value}
                    self.finish(start, **extra)
                    self.assert_pending(start)

    def test_team_finish_without_call_id_or_with_matching_call_id_is_valid(self):
        for call_id in (MISSING, "call-a"):
            with self.subTest(call_id=call_id):
                start = self.start()
                self.finish(start, call_id=call_id)
                self.assert_pending(start, False)

    def test_foreign_group_receipts_cannot_clear_same_root_start(self):
        for family in ("team", "workspace"):
            for reconciled in (False, True):
                with self.subTest(family=family, reconciled=reconciled):
                    start = self.start(family=family)
                    foreign = self.start(family=family, task="group-b")
                    self.finish(start, task=foreign.task_id,
                                kind="workspace.tool_reconciled" if reconciled else None)
                    self.assert_pending(start)
                    self.assert_pending(foreign)

    def test_finished_requires_exact_host_identity(self):
        for family, host in (("team", TEAM), ("workspace", HOST)):
            for actor in (Actor(host.id, "other", host.kind), Actor("other", host.role, host.kind),
                          Actor(host.id, host.role, "agent"), Actor.human(), HOST if family == "team" else TEAM):
                with self.subTest(family=family, actor=actor):
                    start = self.start(family=family)
                    self.finish(start, actor=actor)
                    self.assert_pending(start)

    def test_untrusted_starts_are_not_workspace_host_calls(self):
        for family, host in (("team", TEAM), ("workspace", HOST)):
            for actor in (Actor(host.id, "other", host.kind), Actor("other", host.role, host.kind),
                          Actor(host.id, host.role, "agent"), Actor.human()):
                with self.subTest(family=family, actor=actor):
                    start = self.start(family=family, actor=actor)
                    self.assert_pending(start, False)

    def test_started_seq_is_strictly_positive_integer_for_all_receipts(self):
        # Keep the start at seq=1 so True and 1.0 would alias the dictionary key.
        start = self.start()
        self.assertEqual(start.seq, 1)
        for kind in ("team.tool_finished", "workspace.tool_reconciled"):
            for value in (MISSING, None, True, False, 1.0, "1", 0, -1, [], {}):
                with self.subTest(kind=kind, value=value):
                    self.finish(start, kind=kind, started_seq=value)
                    self.assert_pending(start)
        legacy = self.start(family="workspace")
        for value in (MISSING, None, True, False, float(legacy.seq), str(legacy.seq), 0, -1, [], {}):
            with self.subTest(family="workspace", value=value):
                self.finish(legacy, started_seq=value)
                self.assert_pending(legacy)

    def test_receipt_family_cannot_cross_bind(self):
        for family, kind, actor in (("team", "workspace.tool_finished", HOST),
                                    ("workspace", "team.tool_finished", TEAM)):
            with self.subTest(family=family):
                start = self.start(family=family)
                self.finish(start, kind=kind, actor=actor, call_id="call-a",
                            run_id="run-a", role="dev", name="workspace_write")
                self.assert_pending(start)

    def test_earlier_receipts_cannot_close_future_starts(self):
        for family, kind, actor in (("team", "team.tool_finished", TEAM),
                                    ("workspace", "workspace.tool_finished", HOST),
                                    ("team", "workspace.tool_reconciled", Actor.human())):
            with self.subTest(kind=kind):
                next_seq = self.log.count() + 2
                self.log.emit(type=kind, task_id="group-a", actor=actor,
                    payload={"started_seq": next_seq, "run_id": "run-a", "role": "dev",
                             "name": "workspace_write", "call_id": "call-a",
                             "invocation_stopped": True, "result": {"status": "ok"}})
                start = self.start(family=family)
                self.assertEqual(start.seq, next_seq)
                self.assert_pending(start)

    def test_legacy_finish_preserves_minimal_call_id_contract(self):
        start = self.start(family="workspace")
        self.finish(start)
        self.assert_pending(start, False)

    def test_legacy_call_ids_must_be_nonblank_matching_strings(self):
        for value in (MISSING, None, "", " ", "wrong", True, 1, [], {}):
            with self.subTest(receipt_call_id=value):
                start = self.start(family="workspace")
                self.finish(start, call_id=value)
                self.assert_pending(start)
        for value in (MISSING, None, "", " ", True, 1, [], {}):
            with self.subTest(start_call_id=value):
                start = self.start(family="workspace", call_id=value)
                self.finish(start, call_id=value)
                self.assert_pending(start)

    def test_reconcile_requires_exact_human_hash_and_stopped_attestation(self):
        for family in ("team", "workspace"):
            for changes in ({"started_hash": MISSING}, {"started_hash": None}, {"started_hash": "wrong"},
                            {"invocation_stopped": MISSING}, {"invocation_stopped": False},
                            {"invocation_stopped": 1}, {"call_id": MISSING}, {"call_id": "wrong"},
                            {"call_id": None}, {"call_id": []}):
                with self.subTest(family=family, changes=changes):
                    start = self.start(family=family)
                    self.finish(start, kind="workspace.tool_reconciled", **changes)
                    self.assert_pending(start)
            for actor in (HOST, TEAM, Actor("human", "other", "human"),
                          Actor("other", "human", "human"), Actor("human", "human", "agent")):
                with self.subTest(family=family, actor=actor):
                    start = self.start(family=family)
                    self.finish(start, kind="workspace.tool_reconciled", actor=actor)
                    self.assert_pending(start)
            start = self.start(family=family)
            self.finish(start, kind="workspace.tool_reconciled")
            self.assert_pending(start, False)

    def test_reconcile_does_not_require_team_fields_but_requires_nonblank_call_id(self):
        start = self.start(run_id=MISSING, role=MISSING)
        self.finish(start, kind="workspace.tool_reconciled")
        self.assert_pending(start, False)
        for family in ("team", "workspace"):
            for value in (MISSING, None, "", " ", True, 1, [], {}):
                with self.subTest(family=family, call_id=value):
                    start = self.start(family=family, call_id=value)
                    self.finish(start, kind="workspace.tool_reconciled", call_id=value)
                    self.assert_pending(start)

    def test_unknown_or_invalid_results_stay_pending_until_human_reconciliation(self):
        results = (MISSING, None, [], {}, {"status": []}, {"status": "unknown"},
                   {"status": "ok", "complete": False}, {"status": "error", "complete": False},
                   {"status": "ok", "outcome": "timeout"},
                   {"status": "error", "error": {"code": "process_failed"}})
        for family in ("team", "workspace"):
            for result in results:
                with self.subTest(family=family, result=result):
                    start = self.start(family=family)
                    self.finish(start, result=result)
                    self.assert_pending(start)
                    self.finish(start, kind="workspace.tool_reconciled")
                    self.assert_pending(start, False)

    def test_historical_mutation_operation_failed_stays_pending_until_reconciled(self):
        for family in ("team", "workspace"):
            for name in ("workspace_write", "workspace_run"):
                with self.subTest(family=family, name=name):
                    start = self.start(family=family, name=name)
                    receipt = self.finish(start,
                        result={"status": "error", "error": {"code": "operation_failed"}})
                    self.assertNotIn("complete", receipt.payload["result"])
                    self.assertNotIn("outcome", receipt.payload["result"])
                    self.assert_pending(start)
                    self.finish(start, kind="workspace.tool_reconciled")
                    self.assert_pending(start, False)

    def test_read_only_operation_failed_receipt_can_clear_pending(self):
        for family in ("team", "workspace"):
            for name in ("workspace_read", "workspace_list", "workspace_search"):
                with self.subTest(family=family, name=name):
                    start = self.start(family=family, name=name)
                    self.finish(start, result={"status": "error", "error": {"code": "operation_failed"}})
                    self.assert_pending(start, False)

    def test_completed_error_receipt_can_clear_pending(self):
        for family in ("team", "workspace"):
            for name in ("workspace_write", "workspace_run", "workspace_read"):
                for code in ("invalid_argument", "invalid_arguments", "path_denied", "tool_denied"):
                    with self.subTest(family=family, name=name, code=code):
                        start = self.start(family=family, name=name)
                        self.finish(start, result={"status": "error", "error": {"code": code}})
                        self.assert_pending(start, False)

    def test_corrupt_team_names_do_not_crash_or_clear_a_valid_start(self):
        valid = self.start()
        for value in (MISSING, None, [], {}, 1, True):
            with self.subTest(name=value):
                malformed = self.start(name=value)
                self.finish(malformed)
                self.finish(valid, name=value)
                self.assert_pending(valid)
                self.assert_pending(malformed, False)

    def test_pending_binding_survives_database_reopen(self):
        start = self.start()
        self.finish(start, role="other")
        reopened = EventLog(self.db)
        self.addCleanup(reopened.close)
        service = WorkspaceService(SimpleNamespace(log=reopened))
        self.assertEqual([event.seq for event in service.pending(root="shared-root")], [start.seq])
        self.assertTrue(reopened.verify_chain().ok)


class WorkspaceReceiptBindingNativeTests(unittest.TestCase):
    events = team_fixture.SimpleTeamTests.events

    def setUp(self):
        team_fixture.SimpleTeamTests.setUp(self)
        self.project = self.root / "project"
        self.project.mkdir()
        config = self.root / "workspaces.json"
        config.write_text(json.dumps({"workspaces": [{
            "id": "source", "title": "Source", "root": str(self.project), "mode": "execute",
            "commands": {"check": ["unused-test-program"]},
        }]}), encoding="utf-8")
        self.rt.workspaces = WorkspaceService(self.rt, config)
        self.binding = self.rt.workspaces.bind(self.task, "source", expected_revision=None)

    def test_foreign_group_wrong_receipts_keep_real_write_and_command_blocked(self):
        foreign = self.rt.create_task("Other group sharing the root")
        self.rt.team.enable(foreign)
        self.rt.workspaces.bind(foreign, "source", expected_revision=None)
        message = self.rt.team.receive(foreign, "Interrupted mutation", request_id="foreign-interrupted")
        self.rt.log.emit(type="team.run_started", actor=TEAM, task_id=foreign,
            payload={"run_id": "old-run", "role": self.manager, "message_seq": message.seq})
        start = self.rt.log.emit(type="team.tool_started", actor=TEAM, task_id=foreign,
            payload={"run_id": "old-run", "role": self.manager, "message_seq": message.seq,
                     "name": "workspace_write", "call_id": "old-call", "root": self.binding["root"]})
        preserved = "Previously applied effect\n"
        (self.project / "effect.txt").write_text(preserved, encoding="utf-8", newline="\n")
        for task, changes in ((self.task, {}), (foreign, {"role": "wrong"}),
                              (foreign, {"name": "workspace_read"}), (foreign, {"run_id": "wrong"})):
            self.rt.log.emit(type="team.tool_finished", actor=TEAM, task_id=task,
                payload={"started_seq": start.seq, "run_id": "old-run", "role": self.manager,
                         "name": "workspace_write", "result": {"status": "ok"}, **changes})
        self.driver.steps[self.manager] = [team_fixture.turn("",
            ("workspace_read", {"path": "effect.txt"}),
            ("workspace_write", {"path": "new.txt", "content": "must stay blocked", "expected_sha256": None}),
            ("workspace_run", {"command": "check", "args": [], "cwd": "."})),
            team_fixture.turn("Inspected; waiting for reconciliation.")]
        self.rt.team.receive(self.task, "Inspect shared directory", request_id="inspect-shared")
        with patch.object(WorkspaceExecutor, "_run", side_effect=AssertionError("Pending command executed")):
            self.rt.collaborate(self.task)
        finishes = {event.payload["name"]: event.payload["result"] for event in self.events("team.tool_finished")
                    if event.payload.get("started_seq") != start.seq}
        self.assertEqual(finishes["workspace_read"]["status"], "ok")
        self.assertEqual(finishes["workspace_read"]["content"], preserved)
        self.assertEqual(finishes["workspace_write"]["status"], "error")
        self.assertEqual(finishes["workspace_run"]["status"], "error")
        self.assertFalse((self.project / "new.txt").exists())
        self.assertEqual((self.project / "effect.txt").read_text(encoding="utf-8"), preserved)
        self.assertEqual([event.seq for event in self.rt.workspaces.pending(root=self.binding["root"])], [start.seq])
        self.assertTrue(self.rt.verify_chain().ok)

    def test_native_team_receipt_without_call_id_clears_start_and_allows_next_write(self):
        self.driver.steps[self.manager] = [team_fixture.turn("",
            ("workspace_write", {"path": "first.txt", "content": "first", "expected_sha256": None}),
            ("workspace_write", {"path": "second.txt", "content": "second", "expected_sha256": None})),
            team_fixture.turn("Both files written.")]
        self.rt.team.receive(self.task, "Write two files", request_id="native-two-writes")
        self.rt.collaborate(self.task)
        starts = self.events("team.tool_started")
        finishes = self.events("team.tool_finished")
        self.assertEqual(len(starts), 2)
        self.assertEqual(len(finishes), 2)
        for start, finish in zip(starts, finishes):
            self.assertEqual(finish.payload["started_seq"], start.seq)
            self.assertNotIn("call_id", finish.payload)
            self.assertEqual(finish.payload["result"]["status"], "ok")
        self.assertEqual((self.project / "first.txt").read_text(encoding="utf-8"), "first")
        self.assertEqual((self.project / "second.txt").read_text(encoding="utf-8"), "second")
        self.assertEqual(self.rt.workspaces.pending(root=self.binding["root"]), [])
        calls = len(self.driver.calls)
        self.rt.team.run(self.task)
        self.assertEqual(len(self.driver.calls), calls)
        self.assertTrue(self.rt.verify_chain().ok)

    def test_real_human_reconcile_preserves_hash_contract_without_execution(self):
        start = self.rt.log.emit(type="team.tool_started", actor=TEAM, task_id=self.task,
            payload={"name": "workspace_run", "run_id": "old-run", "role": self.manager,
                     "call_id": "old-call", "root": self.binding["root"]})
        with patch.object(WorkspaceExecutor, "execute", side_effect=AssertionError("Replayed tool")):
            receipt = self.rt.workspaces.reconcile(self.task, start.seq,
                reason="Inspected files and confirmed the process stopped", invocation_stopped=True)
        self.assertEqual(receipt.actor, Actor.human())
        self.assertEqual(receipt.payload["started_hash"], start.hash)
        self.assertIs(receipt.payload["invocation_stopped"], True)
        self.assertEqual(self.rt.workspaces.pending(task_id=self.task), [])
        self.assertEqual(self.driver.calls, [])
        self.assertTrue(self.rt.verify_chain().ok)


class WorkspaceReceiptBindingLegacyNativeTests(unittest.TestCase):
    def test_native_workspace_gateway_receipt_clears_pending(self):
        fixture = workspace_fixture.WorkspaceIntegrationTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        _, context = fixture.prepare_call()
        fixture.driver.turns = [workspace_fixture.turn(
            workspace_fixture.ToolCall("legacy-write", "workspace_write", {"path": "a.txt"})),
            workspace_fixture.turn(text=workspace_fixture.final_text())]
        fixture.service(context)
        start, = fixture.events("workspace.tool_started")
        finish, = fixture.events("workspace.tool_finished")
        self.assertEqual(finish.payload["started_seq"], start.seq)
        self.assertEqual(finish.payload["call_id"], start.payload["call_id"])
        self.assertEqual(fixture.rt.workspaces.pending(task_id=fixture.task), [])
        self.assertEqual(len(fixture.executor.calls), 1)


if __name__ == "__main__":
    unittest.main()
