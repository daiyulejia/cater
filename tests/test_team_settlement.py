"""Native tool crash boundaries with temporary ledgers and fake model turns."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from cater.artifacts import normalize_deliverables
from cater.events import Actor
from cater.runtime import Runtime
from cater.team import HOST, LOCAL_EVENT_CONTRACT, LOCAL_EVENT_TOOLS
from cater.tool_protocol import ToolCall
from cater.workspaces import WorkspaceService
import test_simple_team


class TeamSettlementTests(unittest.TestCase):
    def setUp(self):
        test_simple_team.SimpleTeamTests.setUp(self)

    def source(self, task=None, *, name="delegate", contract=True, role=None):
        task = task or self.task
        role = self.rt.team._role(role or self.manager)
        message = self.rt.team.receive(task, f"@{role.name} inspect this fixture")
        run = self.rt.log.emit(type="team.run_started", actor=HOST, task_id=task, payload={
            "run_id": f"run-{message.seq}", "role": role.id, "message_seq": message.seq,
            "revision": self.rt.team.view(task)["revision"],
            "control_seq": self.rt.execution_control.view(task)["seq"],
        })
        start = self.rt.log.emit(type="team.tool_started", actor=HOST, task_id=task, payload={
            "run_id": run.payload["run_id"], "role": role.id, "message_seq": message.seq,
            "name": name, "call_id": f"call-{message.seq}", "arguments": {"instruction": "fixture"},
            **({"execution_contract": dict(LOCAL_EVENT_CONTRACT)} if contract else {}),
        })
        return message, role, run, start

    def finish(self, start, *, actor=HOST, task=None, result=None, **changes):
        return self.rt.log.emit(type="team.tool_finished", actor=actor, task_id=task or start.task_id, payload={
            "started_seq": start.seq, **{k: start.payload.get(k) for k in ("run_id", "role", "name")},
            "result": {"status": "ok"} if result is None else result, **changes,
        })

    def end(self, run, **changes):
        return self.rt.log.emit(type="team.run_finished", actor=HOST, task_id=run.task_id,
                                payload={**run.payload, "status": "interrupted", **changes})

    def prepare(self, name):
        task = self.rt.create_task(f"Settlement case {name}")
        self.rt.team.enable(task)
        arguments = {
            "delegate": {"recipient": self.worker, "instruction": "Inspect the result"},
            "send_message": {"recipient": self.worker, "text": "Inspect this result"},
            "set_objective": {"goal": "Adopted new fixture goal"},
            "finish_work": {"summary": "Fixture delivered", "verification": "Text only"},
            "request_questions": {"title": "Fixture choice", "questions": [
                {"id": "kind", "question": "Which direction?", "selection": "single", "options": []}]},
        }.get(name)
        if name in {"request_review", "record_review"}:
            author = self.manager if name == "request_review" else self.worker
            payload, = normalize_deliverables([{"filename": "result.txt", "title": "Fixture", "summary": "Fixture",
                                                "format": "text", "content": "Fixture source"}])
            artifact = self.rt.log.emit(type="deliverable.published", actor=Actor.agent(author, "worker"),
                                        task_id=task, payload=payload)
            if name == "request_review":
                arguments = {"reviewer": self.worker, "artifact_seqs": [artifact.seq], "instruction": "Read the actual source"}
            else:
                request = self.rt.log.emit(type="team.review_requested", actor=HOST, task_id=task, payload={
                    "root_seq": None, "author": self.worker, "reviewer": self.manager, "revision": 0,
                    "run_id": "fixture-preparation", "requested_by": self.worker, "instruction": "Inspect the source",
                    "artifacts": [{"seq": artifact.seq, "filename": "result.txt", "content_sha256": payload["content_sha256"]}],
                })
                self.rt.team._send(task, self.rt.team._role(self.worker), self.manager, "Review this source", "assignment",
                                   review_seq=request.seq)
                arguments = {"review_seq": request.seq, "verdict": "approved", "feedback": "Read the source; no execution claimed"}
        message = self.rt.team.receive(task, "Run the prepared fixture action")
        run = self.rt.log.emit(type="team.run_started", actor=HOST, task_id=task, payload={
            "message_seq": message.seq, "role": self.manager, "run_id": f"case-{message.seq}", "revision": 0,
            "control_seq": self.rt.execution_control.view(task)["seq"],
        })
        self.driver.steps[self.manager] = [test_simple_team.turn("", (name, arguments)), test_simple_team.turn("Action settled.")]
        return task, message, run

    def test_every_local_action_rolls_back_if_crashed_before_finished(self):
        for name in sorted(LOCAL_EVENT_TOOLS):
            with self.subTest(name=name):
                task, message, run = self.prepare(name)
                before = self.rt.log.read(task_id=task)
                emit = self.rt.log.emit

                def interrupted(**kwargs):
                    if kwargs["type"] == "team.tool_finished":
                        raise SystemExit("Simulate termination before receipt")
                    return emit(**kwargs)

                with patch.object(self.rt.log, "emit", side_effect=interrupted), self.assertRaises(SystemExit):
                    list(self.rt.team._run_message(task, message, self.rt.organization.leader, run))
                after = self.rt.log.read(task_id=task)
                self.assertEqual(after[:len(before)], before)
                self.assertEqual([e.type for e in after[len(before):]], ["team.model_called", "team.tool_started"])
                self.assertEqual(after[-1].payload["execution_contract"], LOCAL_EVENT_CONTRACT)
                self.assertEqual(self.rt.team._pending_operations(task)[-1]["outcome"], "unsettled")
                self.rt.team._retire_interrupted(task)
                pending, = self.rt.team._pending_operations(task)
                self.assertEqual(pending["outcome"], "not_committed")
                self.assertFalse(pending["replay_authorized"])
                self.assertEqual(self.rt.team.view(task)["status"], "running")
                self.assertFalse(self.rt.log.db.in_transaction)
        self.assertTrue(self.rt.verify_chain().ok)

    def test_every_local_action_commits_with_its_receipt(self):
        for name in sorted(LOCAL_EVENT_TOOLS):
            with self.subTest(name=name):
                task, message, run = self.prepare(name)
                execute = self.rt.team._execute

                def checked(*args, **kwargs):
                    self.assertTrue(self.rt.log.db.in_transaction)
                    return execute(*args, **kwargs)

                with patch.object(self.rt.team, "_execute", side_effect=checked):
                    list(self.rt.team._run_message(task, message, self.rt.organization.leader, run))
                receipts = [e for e in self.rt.log.read(task_id=task)
                            if e.type == "team.tool_finished" and e.payload["name"] == name]
                self.assertEqual(len(receipts), 1)
                self.assertEqual(receipts[0].payload["result"]["status"], "ok")
                self.assertEqual(self.rt.team._pending_operations(task), [])
                self.assertFalse(self.rt.log.db.in_transaction)
        self.assertTrue(self.rt.verify_chain().ok)

    def test_error_receipt_failure_does_not_commit_delegation(self):
        task, message, run = self.prepare("delegate")
        emit, failed = self.rt.log.emit, []

        def receipt_unavailable(**kwargs):
            if kwargs["type"] == "team.tool_finished" and not failed:
                failed.append(True)
                raise OSError("Receipt storage transient failure")
            return emit(**kwargs)

        with patch.object(self.rt.log, "emit", side_effect=receipt_unavailable):
            list(self.rt.team._run_message(task, message, self.rt.organization.leader, run))
        self.assertEqual(self.rt.team.view(task)["assignments"], [])
        receipt, = [e for e in self.rt.log.read(task_id=task) if e.type == "team.tool_finished"]
        self.assertEqual(receipt.payload["result"]["status"], "error")
        self.assertEqual(self.rt.team._pending_operations(task), [])

    def test_process_exit_after_delegate_before_receipt_does_not_repeat_work(self):
        code = r'''
import os
import sys
sys.path.insert(0, str(__import__("pathlib").Path.cwd() / "tests"))
from cater.runtime import Runtime
import test_simple_team
driver = test_simple_team.TeamDriver()
with Runtime(sys.argv[1], roles_path=sys.argv[2], drivers={"team_test": driver}) as rt:
    task = sys.argv[3]
    leader = rt.organization.leader.id
    worker = rt.worker_roles[0].id
    driver.steps[leader] = [test_simple_team.turn("", ("delegate", {"recipient": worker, "instruction": "Must not commit without receipt"}))]
    rt.team.receive(task, "Crash the isolated fixture after delegation")
    emit = rt.log.emit
    def crashing_emit(**kwargs):
        if kwargs["type"] == "team.tool_finished":
            os._exit(37)
        return emit(**kwargs)
    rt.log.emit = crashing_emit
    rt.collaborate(task)
'''
        result = subprocess.run([sys.executable, "-X", "utf8", "-B", "-c", code,
                                 self.rt.log.path, str(self.roles), self.task],
                                cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 37, result.stderr.decode("utf-8", errors="replace"))
        self.assertEqual(self.rt.team.view(self.task)["assignments"], [])
        self.assertEqual(self.rt.team._pending_operations(self.task)[0]["outcome"], "unsettled")
        self.rt.team.run(self.task)
        self.assertEqual(self.rt.team._pending_operations(self.task)[0]["outcome"], "not_committed")
        self.assertEqual(self.driver.calls, [])
        self.assertEqual(self.rt.team.view(self.task)["assignments"], [])
        self.assertTrue(self.rt.verify_chain().ok)

    def test_control_change_before_action_lock_is_rechecked_without_dispatch(self):
        task, message, run = self.prepare("delegate")
        invoke = self.rt.team._invoke_tool
        changed = []

        def pause_then_invoke(*args):
            if not changed:
                changed.append(True)
                with Runtime(self.rt.log.path, roles_path=self.roles, drivers={"team_test": self.driver},
                             close_drivers_on_close=False) as other:
                    other.execution_control.change(task, "pause", request_id="pause-before-tool-lock")
            return invoke(*args)

        with patch.object(self.rt.team, "_invoke_tool", side_effect=pause_then_invoke):
            list(self.rt.team._run_message(task, message, self.rt.organization.leader, run))
        self.assertEqual(self.rt.team.view(task)["assignments"], [])
        self.assertEqual(self.rt.execution_control.view(task)["state"], "paused")
        receipt, = [e for e in self.rt.log.read(task_id=task) if e.type == "team.tool_finished"]
        self.assertEqual(receipt.payload["result"]["status"], "error")

    def test_error_return_rolls_back_partial_events_and_keeps_structured_reason(self):
        task, message, run = self.prepare("delegate")
        expected = {"status": "error", "code": "fixture_rejected", "assignments": [123], "message": "Specific cause"}

        def reject_after_effect(*args):
            self.rt.team._send(task, self.rt.organization.leader, self.worker, "Must roll back", "assignment")
            return copy.deepcopy(expected)

        with patch.object(self.rt.team, "_execute", side_effect=reject_after_effect):
            list(self.rt.team._run_message(task, message, self.rt.organization.leader, run))
        self.assertEqual(self.rt.team.view(task)["assignments"], [])
        receipt, = [e for e in self.rt.log.read(task_id=task) if e.type == "team.tool_finished"]
        self.assertEqual(receipt.payload["result"], expected)

    def test_bad_finished_cannot_hide_an_unsettled_operation(self):
        _, _, run, start = self.source()
        other = self.rt.create_task("Other group")
        bad = [{"actor": Actor.agent("team", "system")}, {"task": other},
               {"role": self.worker}, {"name": "workspace_write"}, {"run_id": "other-run"}, {"call_id": "other-call"},
               {"started_seq": True}, {"started_seq": str(start.seq)}, {"result": {"status": "invented"}}]
        for changes in bad:
            with self.subTest(changes=changes):
                self.finish(start, **changes)
                self.assertEqual([row["seq"] for row in self.rt.team._pending_operations(self.task)], [start.seq])
        self.end(run)
        self.assertEqual(self.rt.team._pending_operations(self.task)[0]["outcome"], "not_committed")
        self.finish(start)
        self.assertEqual(self.rt.team._pending_operations(self.task), [])

    def test_old_or_nonlocal_contract_never_claims_rollback(self):
        for name, contract in [("delegate", None), ("delegate", {**LOCAL_EVENT_CONTRACT, "version": True}),
                               ("delegate", {**LOCAL_EVENT_CONTRACT, "version": 2}),
                               ("workspace_run", LOCAL_EVENT_CONTRACT), ("publish_artifact", LOCAL_EVENT_CONTRACT)]:
            with self.subTest(name=name, contract=contract):
                task = self.rt.create_task("Unknown operation fixture")
                self.rt.team.enable(task)
                _, _, run, start = self.source(task, name=name, contract=False)
                if contract:
                    # Use a new append-only start, not mutation of the event's payload.
                    start = self.rt.log.emit(type="team.tool_started", actor=HOST, task_id=task,
                        payload={**start.payload, "execution_contract": contract})
                self.end(run)
                row = next(row for row in self.rt.team._pending_operations(task) if row["seq"] == start.seq)
                self.assertEqual(row["outcome"], "unknown")
                self.assertFalse(row["replay_authorized"])

    def test_missing_run_binding_stays_unknown_not_false_success(self):
        for changes in ({"run_id": None}, {"role": None}, {"name": None}, {"message_seq": None}, {"call_id": None}):
            with self.subTest(changes=changes):
                task = self.rt.create_task("Malformed historical operation")
                self.rt.team.enable(task)
                _, _, _, start = self.source(task)
                broken = self.rt.log.emit(type="team.tool_started", actor=HOST, task_id=task,
                    payload={**start.payload, **changes})
                self.finish(broken)
                self.assertIn(broken.seq, [row["seq"] for row in self.rt.team._pending_operations(task)])

    def test_reconciliation_without_real_call_id_does_not_hide_unknown_operation(self):
        _, _, run, start = self.source(name="workspace_write", contract=False)
        broken = self.rt.log.emit(type="team.tool_started", actor=HOST, task_id=self.task,
                                  payload={**start.payload, "call_id": None})
        self.end(run)
        self.rt.log.emit(type="workspace.tool_reconciled", actor=Actor.human(), task_id=self.task, payload={
            "started_seq": broken.seq, "started_hash": broken.hash, "call_id": None, "invocation_stopped": True})
        self.assertIn(broken.seq, [e.seq for e in self.rt.workspaces.pending(task_id=self.task)])
        self.assertIn(broken.seq, [row["seq"] for row in self.rt.team._pending_operations(self.task)])

    def test_publish_file_failure_keeps_published_bytes_and_unknown_effect(self):
        task = self.task
        self.driver.steps[self.manager] = [test_simple_team.turn("", ("publish_artifact", {
            "filename": "result.txt", "content": "Already published", "summary": "Fixture", "format": "text"})),
            test_simple_team.turn("Saved copy needs checking.")]
        self.rt.team.receive(task, "Publish the fixture")

        def fail_copy(*args):
            self.assertFalse(self.rt.log.db.in_transaction)
            raise OSError("Disk unavailable")

        with patch("cater.artifact_store.materialize_team_artifact", side_effect=fail_copy):
            self.rt.collaborate(task)
        self.assertEqual(self.rt.task(task).deliverables[0]["content"], "Already published")
        pending, = self.rt.team._pending_operations(task)
        self.assertEqual(pending["outcome"], "unknown")
        self.assertIn("finished_seq", pending)
        self.assertFalse(pending["replay_authorized"])
        start, = list(self.rt.log.iter_events(task_id=task, event_types=("team.tool_started",)))
        self.assertNotIn("execution_contract", start.payload)

    def test_external_tools_never_hold_atomic_event_transaction(self):
        for name in ("publish_artifact", "publish_workspace_file", "workspace_write", "workspace_run"):
            with self.subTest(name=name):
                message, role, run, start = self.source(name=name, contract=False)
                call = ToolCall("external-fixture", name, {})

                def execute(*args):
                    self.assertFalse(self.rt.log.db.in_transaction)
                    return {"status": "ok"}

                with patch.object(self.rt.team, "_execute", side_effect=execute):
                    result = self.rt.team._invoke_tool(self.task, role, run, 0, call,
                        {"properties": {}, "required": []}, None, {}, start, None)
                self.assertEqual(result["status"], "ok")

    def test_write_that_succeeds_before_cleanup_error_stays_unknown_and_not_replayed(self):
        root = self.root / "project"
        root.mkdir()
        config = self.root / "settlement-workspaces.json"
        config.write_text(json.dumps({"workspaces": [{
            "id": "fixture", "title": "Fixture", "root": str(root), "mode": "edit"}]}), encoding="utf-8")
        self.rt.workspaces = WorkspaceService(self.rt, config)
        self.rt.workspaces.bind(self.task, "fixture", expected_revision=None)
        self.driver.steps[self.manager] = [test_simple_team.turn("", ("workspace_write", {
            "path": "effect.txt", "content": "Write happened", "expected_sha256": None})),
            test_simple_team.turn("The original effect needs checking.")]
        self.rt.team.receive(self.task, "Create the fixture file")
        unlink = Path.unlink

        def fail_staging_cleanup(path, *args, **kwargs):
            if path.parent.resolve() == root.resolve() and path.name.startswith(".workspace-tmp-"):
                raise OSError("Injected cleanup failure after link publication")
            return unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", fail_staging_cleanup):
            self.rt.collaborate(self.task)
        self.assertEqual((root / "effect.txt").read_text(encoding="utf-8"), "Write happened")
        pending, = self.rt.team._pending_operations(self.task)
        self.assertEqual(pending["outcome"], "unknown")
        self.assertIn(pending["seq"], [e.seq for e in self.rt.workspaces.pending(task_id=self.task)])
        receipts = [e.payload["result"] for e in self.rt.log.read(task_id=self.task) if e.type == "team.tool_finished"]
        self.assertEqual(receipts[0]["error"]["code"], "operation_failed")
        self.assertIs(receipts[0]["complete"], False)
        self.driver.steps[self.manager] = [test_simple_team.turn("", ("workspace_write", {
            "path": "second.txt", "content": "Must not run", "expected_sha256": None})), test_simple_team.turn("Not replayed.")]
        self.rt.team.receive(self.task, "Check whether another write is allowed")
        self.rt.collaborate(self.task)
        self.assertFalse((root / "second.txt").exists())
        self.assertEqual(self.rt.team._pending_operations(self.task)[0]["seq"], pending["seq"])

    def test_pending_index_is_bounded_and_details_are_filtered_and_pageable(self):
        starts = []
        for index in range(24):
            _, _, run, start = self.source(name="delegate")
            starts.append(start)
            self.end(run)
        message = self.rt.team.receive(self.task, "Inspect current receipts")
        context = self.rt.team._context(self.task, message, self.rt.organization.leader)
        self.assertEqual(len(context["pending_operations"]), 20)
        self.assertTrue(context["pending_operations_truncated"])
        self.assertTrue(all("arguments" not in row for row in context["pending_operations"]))
        self.driver.steps[self.manager] = [test_simple_team.turn("", ("read_receipts", {
            "include_pending": True, "name": "delegate", "limit": 10})),
            test_simple_team.turn("Unsettled operations inspected, not replayed.")]
        self.rt.collaborate(self.task)
        receipt, = [e for e in self.rt.log.read(task_id=self.task)
                     if e.type == "team.tool_finished" and e.payload["name"] == "read_receipts"]
        result = receipt.payload["result"]
        self.assertEqual(len(result["pending_operations"]), 10)
        self.assertTrue(result["pending_has_more"])
        self.assertEqual(result["pending_next_before_seq"], starts[-10].seq)
        self.assertEqual([row["seq"] for row in result["pending_operations"]], [s.seq for s in starts[-10:]])
        self.assertTrue(all("arguments" in row for row in result["pending_operations"]))
        self.assertFalse(any(row["name"] == "read_receipts" for row in self.rt.team._pending_operations(self.task)))

    def test_pending_query_filters_have_same_group_boundary(self):
        _, _, run, start = self.source(name="delegate")
        self.end(run)
        other = self.rt.create_task("Separate task")
        self.rt.team.enable(other)
        self.source(other, name="publish_artifact", contract=False)
        self.driver.steps[self.manager] = [test_simple_team.turn("", ("read_receipts", {
            "include_pending": True, "role": self.manager, "message_seq": start.payload["message_seq"],
            "run_id": run.payload["run_id"], "name": "delegate"})), test_simple_team.turn("Inspected.")]
        self.rt.team.receive(self.task, "Inspect relevant operation")
        self.rt.collaborate(self.task)
        receipt, = [e for e in self.rt.log.read(task_id=self.task)
                     if e.type == "team.tool_finished" and e.payload["name"] == "read_receipts"]
        self.assertEqual([row["seq"] for row in receipt.payload["result"]["pending_operations"]], [start.seq])
        self.assertEqual(len(self.rt.team._pending_operations(other)), 1)


if __name__ == "__main__":
    unittest.main()
