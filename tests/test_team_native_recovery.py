"""Crash recovery supervises persisted work instead of replaying old requests."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from cater.activity_owner import activity_owner
from cater.artifacts import normalize_deliverables
from cater.events import Actor
from cater.runtime import Runtime
from cater.team import HOST
from cater.work import WorkConflict
from cater.workspaces import WorkspaceService
import test_simple_team


class NativeRecoveryTests(unittest.TestCase):
    setUp = test_simple_team.SimpleTeamTests.setUp
    events = test_simple_team.SimpleTeamTests.events
    run_ok = test_simple_team.SimpleTeamTests.run_ok

    def orphan(self, *, worker=False):
        message = (self.rt.team._send(self.task, self.rt.organization.leader, self.worker,
                    "Read existing files and finish the original work", "assignment") if worker else
                   self.rt.team.receive(self.task, "Inspect this project", request_id="crash-original-message"))
        claimed = self.rt.team._claim(self.task)
        self.assertEqual(claimed[0].seq, message.seq)
        return message, claimed[2]

    def finish(self, run, status="interrupted"):
        return self.rt.log.emit(type="team.run_finished", task_id=self.task, actor=HOST,
            payload={**run.payload, "status": status, "reason": "Fixture terminal"})

    def recovery_messages(self):
        return [e for e in self.events("team.message_received") if e.payload.get("kind") == "recovery"]

    def test_crash_produces_one_trusted_notification_and_no_model_replay(self):
        source, run = self.orphan()
        first, = self.rt.team.recovery.prepare(self.task)
        second, = self.rt.team.recovery.prepare(self.task)
        self.assertEqual(first, second)
        self.assertTrue(self.rt.team.recovery.is_message(first))
        self.assertEqual(first.payload["recovery_of"], run.seq)
        self.assertEqual(first.payload["recovery_hash"], run.hash)
        self.assertEqual(len(self.events("team.run_finished")), 1)
        self.assertEqual(self.driver.calls, [])

        def supervise(context, history, tools):
            self.assertEqual(context["message"]["kind"], "recovery")
            self.assertEqual(context["message"]["sender_kind"], "system")
            self.assertEqual(history, [])
            self.assertIn("read_checkpoint", {t["name"] for t in tools})
            self.assertEqual(context["work_checkpoint"]["objective"], "制作 doc 转 PDF 工具")
            return test_simple_team.turn("中断记录已核对，还没有新的完成证据。")
        self.driver.steps[self.manager] = [supervise]
        self.rt.collaborate(self.task)
        self.assertEqual(len(self.driver.calls), 1)
        self.assertEqual(len(self.recovery_messages()), 1)
        self.assertEqual([r["message_seq"] for r in self.rt.team.view(self.task)["runs"]], [source.seq, first.seq])
        self.assertEqual(self.rt.team.recovery.prepare(self.task), [])
        self.assertEqual(self.rt.team.view(self.task)["status"], "running")
        self.assertTrue(self.rt.verify_chain().ok)

    def test_terminal_and_notification_rollback_together_on_crash(self):
        self.orphan()
        emit = self.rt.log.emit
        def crashing(**kwargs):
            if kwargs["type"] == "team.message_received":
                raise SystemExit("Crash before recovery notification")
            return emit(**kwargs)
        with patch.object(self.rt.log, "emit", side_effect=crashing), self.assertRaises(SystemExit):
            self.rt.team.recovery.prepare(self.task)
        self.assertEqual(self.events("team.run_finished"), [])
        self.assertEqual(self.recovery_messages(), [])
        self.assertEqual(len(self.rt.team.recovery.prepare(self.task)), 1)

    def test_live_kernel_consumer_is_not_retired(self):
        self.orphan()
        with activity_owner(self.rt.log.path, self.task, "simple-team") as owned:
            self.assertTrue(owned)
            self.assertEqual(self.rt.team.recovery.prepare(self.task), [])
        self.assertEqual(self.events("team.run_finished"), [])
        self.assertEqual(self.recovery_messages(), [])

    def test_outer_transaction_cannot_run_models_or_advertise_durable_recovery(self):
        self.rt.team.receive(self.task, "Check this project", request_id="outer-transaction-message")
        with self.rt.log.atomic():
            with self.assertRaises(WorkConflict):
                self.rt.collaborate(self.task)
            with self.assertRaises(WorkConflict):
                self.rt.team.recovery.prepare(self.task)
            _, _, started = self.rt.team._claim(self.task)
            self.assertNotIn("recovery_contract", started.payload)
        self.assertEqual(self.driver.calls, [])

    def test_real_other_process_holds_consumer_ownership_until_released(self):
        self.orphan()
        code = r'''
import sys
from cater.activity_owner import activity_owner
with activity_owner(sys.argv[1], sys.argv[2], "simple-team") as owned:
    print("owned" if owned else "busy", flush=True)
    sys.stdin.readline()
'''
        process = subprocess.Popen([sys.executable, "-X", "utf8", "-B", "-c", code, self.rt.log.path, self.task],
            cwd=Path(__file__).resolve().parents[1], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8")
        try:
            self.assertEqual(process.stdout.readline().strip(), "owned")
            self.assertIsNone(process.poll())
            self.assertEqual(self.rt.team.recovery.prepare(self.task), [])
            self.assertEqual(self.events("team.run_finished"), [])
        finally:
            try:
                process.communicate("release\n", timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0)
        self.assertEqual(len(self.rt.team.recovery.prepare(self.task)), 1)

    def test_recovery_notification_survives_another_runtime_without_duplication(self):
        self.orphan()
        first, = self.rt.team.recovery.prepare(self.task)
        with Runtime(self.rt.log.path, roles_path=self.roles, drivers={"team_test": self.driver},
                     close_drivers_on_close=False) as other:
            self.assertEqual(other.team.recovery.prepare(self.task), [first])
        self.assertEqual(len(self.recovery_messages()), 1)
        self.assertEqual(self.driver.calls, [])

    def test_new_customer_message_takes_over_instead_of_extra_recovery(self):
        self.orphan()
        self.rt.team.receive(self.task, "先不要做了，我想换个方向", request_id="new-customer-direction")
        self.driver.steps[self.manager] = [test_simple_team.turn("先讨论新方向，不重跑原请求。")]
        self.rt.collaborate(self.task)
        self.assertEqual(self.recovery_messages(), [])
        self.assertEqual(len(self.driver.calls), 1)
        self.assertEqual(self.driver.calls[0]["context"]["message"]["text"], "先不要做了，我想换个方向")

    def test_pause_cancel_and_changed_goal_do_not_recover_old_work(self):
        for change in ("pause", "cancel", "goal", "done"):
            with self.subTest(change=change):
                task = self.rt.create_task("Another work")
                self.rt.team.enable(task)
                self.rt.team.receive(task, "Original work", request_id=f"original-{change}-work")
                self.rt.team._claim(task)
                if change in {"pause", "cancel"}:
                    self.rt.execution_control.change(task, change, request_id=f"control-{change}-test")
                else:
                    self.rt.log.emit(type="team.objective_updated" if change == "goal" else "team.objective_completed",
                        task_id=task, actor=HOST, payload={"goal": "New direction", "summary": "Done", "verification": "Fixture"})
                self.assertEqual(self.rt.team.recovery.prepare(task), [])
                self.assertEqual(self.rt.team.view(task)["runs"][0]["status"], "interrupted")
        self.assertEqual(self.driver.calls, [])

    def test_pause_resume_does_not_revive_old_notification(self):
        self.orphan()
        notification, = self.rt.team.recovery.prepare(self.task)
        self.rt.execution_control.change(self.task, "pause", request_id="pause-after-notice")
        self.rt.execution_control.change(self.task, "resume", request_id="resume-after-notice")
        self.assertFalse(self.rt.team.recovery.current(self.task, notification))
        self.assertEqual(self.rt.team.view(self.task)["pending_count"], 0)
        self.assertIsNone(self.rt.team._claim(self.task))

    def test_workspace_change_does_not_recover_old_authorization(self):
        self.orphan()
        config = self.root / "workspaces.json"
        config.write_text(json.dumps({"workspaces": [{"id": "project", "title": "Project",
            "root": str(self.root), "mode": "edit"}]}), encoding="utf-8")
        self.rt.workspaces = WorkspaceService(self.rt, config)
        self.rt.workspaces.bind(self.task, "project", expected_revision=None)
        self.assertEqual(self.rt.team.recovery.prepare(self.task), [])
        self.assertEqual(self.driver.calls, [])

    def test_legacy_run_is_closed_without_implicitly_upgrading_recovery_contract(self):
        source, run = self.orphan()
        self.finish(run)
        self.rt.log.emit(type="team.run_started", task_id=self.task, actor=HOST,
            payload={"message_seq": source.seq, "role": self.manager, "run_id": "legacy-run", "revision": 0,
                     "control_seq": self.rt.execution_control.view(self.task)["seq"]})
        self.assertEqual(self.rt.team.recovery.prepare(self.task), [])
        self.assertEqual(self.recovery_messages(), [])

    def test_interrupted_recovery_does_not_spawn_an_infinite_recovery_chain(self):
        self.orphan()
        notification, = self.rt.team.recovery.prepare(self.task)
        message, _, _ = self.rt.team._claim(self.task)
        self.assertEqual(message.seq, notification.seq)
        self.assertEqual(self.rt.team.recovery.prepare(self.task), [])
        self.assertEqual(len(self.recovery_messages()), 1)
        self.assertEqual(len(self.events("team.run_finished")), 2)

    def test_forged_recovery_actor_hash_or_binding_is_not_executable(self):
        self.orphan()
        notification, = self.rt.team.recovery.prepare(self.task)
        cases = [(Actor.system("other"), {}), (HOST, {"recovery_hash": "wrong"}),
                 (HOST, {"recovery_of": True}), (HOST, {"interrupted_seq": 1}),
                 (HOST, {"recipient": self.worker}), (HOST, {"revision": 999})]
        for actor, changes in cases:
            with self.subTest(actor=actor, changes=changes):
                forged = self.rt.log.emit(type="team.message_received", task_id=self.task, actor=actor,
                    payload={**notification.payload, **changes})
                self.assertFalse(self.rt.team.recovery.is_message(forged))
                self.assertNotIn(forged, self.rt.team._messages(self.task))

    def test_checkpoint_pages_all_open_work_and_excludes_reported_and_old_goals(self):
        old = self.rt.team._send(self.task, self.rt.organization.leader, self.worker, "Old work", "assignment")
        self.rt.log.emit(type="team.objective_updated", task_id=self.task, actor=HOST, payload={"goal": "New work"})
        sources = [self.rt.team._send(self.task, self.rt.organization.leader, self.worker,
                   f"Exact work instruction {i}", "assignment") for i in range(26)]
        self.rt.team._send(self.task, self.rt.team._role(self.worker), self.manager, "Closed one", "report", reply_to=sources[0].seq)
        checkpoint = self.rt.team.recovery.checkpoint(self.task)
        self.assertEqual(checkpoint["open_work_count"], 25)
        self.assertTrue(checkpoint["open_work_truncated"])
        self.assertTrue(all("text" not in row for row in checkpoint["open_work"]))
        read = self.rt.team.recovery.read(self.task, {"limit": 20})
        self.assertTrue(read["has_more"])
        rest = self.rt.team.recovery.read(self.task, {"after_seq": read["next_seq"], "limit": 20})
        rows = read["assignments"] + rest["assignments"]
        self.assertEqual([r["seq"] for r in rows], [e.seq for e in sources[1:]])
        self.assertTrue(all(r["text"].startswith("Exact work instruction") for r in rows))
        self.assertNotIn(old.seq, [r["seq"] for r in rows])
        self.assertFalse(rest["has_more"])
        for arguments in ({"after_seq": True}, {"after_seq": -1}, {"limit": 0}, {"limit": True}, {"limit": 51}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                self.rt.team.recovery.read(self.task, arguments)

    def test_continuation_resumes_original_assignment_and_reports_it_without_new_job(self):
        original, _ = self.orphan(worker=True)
        self.rt.team.recovery.prepare(self.task)
        def inspect(context, history, tools):
            row, = context["work_checkpoint"]["open_work"]
            self.assertEqual(row["seq"], original.seq)
            self.assertEqual(row["status"], "interrupted")
            return test_simple_team.turn("", ("read_checkpoint", {}))
        def resume(context, history, tools):
            receipt = json.loads(history[-1]["content"])
            self.assertEqual(receipt["assignments"][0]["text"], original.payload["text"])
            return test_simple_team.turn("核对后请继续原工作。", ("send_message", {"recipient": self.worker,
                "text": "先检查已有材料，不重写已完成文件，补齐说明后交还原委托。", "continue_assignment_seq": original.seq}),
                ("wait_for_reply", {}))
        def work(context, history, tools):
            self.assertEqual(context["message"]["kind"], "continuation")
            self.assertEqual(context["active_assignment"]["seq"], original.seq)
            return test_simple_team.turn("已有材料已核对。", ("publish_artifact", {"filename": "guide.md",
                "content": "# Checked guide\nNo conversion was executed.", "summary": "Checked guide", "format": "markdown"}))
        self.driver.steps = {self.manager: [inspect, resume,
            test_simple_team.turn("成果已核对。", ("finish_work", {"summary": "交付说明", "verification": "仅核对说明正文"})),
            test_simple_team.turn("说明已准备好，未验证转换引擎。")],
            self.worker: [work, test_simple_team.turn("原委托已完成，说明已保存。") ]}
        self.rt.collaborate(self.task)
        self.assertEqual([e.seq for e in self.events("team.message_received") if e.payload.get("kind") == "assignment"], [original.seq])
        reports = [e for e in self.events("team.message_received") if e.payload.get("kind") == "report"]
        self.assertEqual([e.payload["reply_to"] for e in reports], [original.seq])
        self.assertEqual(self.rt.team.view(self.task)["status"], "done")
        self.assertEqual(self.rt.team.recovery.assignments(self.task), [])
        self.assertTrue(self.rt.verify_chain().ok)

    def test_continuation_rejects_wrong_sender_recipient_closed_queued_and_other_task(self):
        original, run = self.orphan(worker=True)
        self.finish(run)
        service = self.rt.team.recovery
        manager, worker = self.rt.organization.leader, self.rt.team._role(self.worker)
        self.assertEqual(service.continuation(self.task, manager, self.worker, original.seq)["seq"], original.seq)
        for role, recipient, seq in ((worker, self.manager, original.seq), (manager, self.manager, original.seq),
                                     (manager, self.worker, True), (manager, self.worker, 999999)):
            with self.subTest(role=role.id, recipient=recipient, seq=seq), self.assertRaises((WorkConflict, ValueError)):
                service.continuation(self.task, role, recipient, seq)
        other = self.rt.create_task("Other group")
        self.rt.team.enable(other)
        with self.assertRaises(WorkConflict):
            service.continuation(other, manager, self.worker, original.seq)
        continuing = self.rt.team._send(self.task, manager, self.worker, "Continue after inspection", "continuation",
                                       continue_assignment_seq=original.seq)
        with self.assertRaises(WorkConflict):
            service.continuation(self.task, manager, self.worker, original.seq)
        self.assertEqual(service.assignments(self.task)[0]["latest_message_seq"], continuing.seq)
        self.rt.team._send(self.task, worker, self.manager, "Original done", "report", reply_to=original.seq)
        with self.assertRaises(WorkConflict):
            service.continuation(self.task, manager, self.worker, original.seq)

    def test_failed_worker_notifies_supervisor_without_closing_original_assignment(self):
        original = self.rt.team._send(self.task, self.rt.organization.leader, self.worker,
                                     "Produce checked work", "assignment")
        def follow_up(context, history, tools):
            self.assertEqual(context["message"]["kind"], "progress")
            row, = context["work_checkpoint"]["open_work"]
            self.assertEqual(row["seq"], original.seq)
            self.assertEqual(row["status"], "failed")
            return test_simple_team.turn("先检查接入，原委托仍未完成。", ("wait_for_reply", {}))
        self.driver.steps = {self.worker: [RuntimeError("private failure")], self.manager: [follow_up]}
        self.rt.collaborate(self.task)
        self.assertFalse([e for e in self.events("team.message_received") if e.payload.get("kind") == "report"])
        self.assertEqual(self.rt.team.recovery.assignments(self.task)[0]["seq"], original.seq)
        self.assertEqual(len(self.driver.calls), 2)

    def test_original_reported_before_continuation_is_consumed_prevents_any_model_call(self):
        original, run = self.orphan(worker=True)
        self.finish(run)
        self.rt.team._send(self.task, self.rt.organization.leader, self.worker, "Continue", "continuation",
                           continue_assignment_seq=original.seq)
        self.rt.team._send(self.task, self.rt.team._role(self.worker), self.manager,
                           "Already finished", "report", reply_to=original.seq)
        self.driver.steps[self.manager] = [test_simple_team.turn("原委托已收回。")]
        self.rt.collaborate(self.task)
        self.assertFalse([c for c in self.driver.calls if c["context"]["you"]["id"] == self.worker])
        self.assertEqual(self.rt.task(self.task).deliverables, [])

    def test_interrupted_unreturned_worker_blocks_delivery_even_after_its_run_is_closed(self):
        original, _ = self.orphan(worker=True)
        self.rt.team.recovery.prepare(self.task)
        self.driver.steps[self.manager] = [test_simple_team.turn("", ("finish_work", {
            "summary": "Must not finish", "verification": "Worker has not reported"})),
            test_simple_team.turn("原工作尚未收回，不能作为完成交付。")]
        self.rt.collaborate(self.task)
        receipt, = [e for e in self.events("team.tool_finished") if e.payload["name"] == "finish_work"]
        self.assertEqual(receipt.payload["result"]["code"], "work_pending")
        self.assertEqual(receipt.payload["result"]["assignments"][0]["seq"], original.seq)
        self.assertEqual(self.rt.team.view(self.task)["status"], "running")

    def test_run_terminal_and_automatic_report_rollback_together(self):
        original, run = self.orphan(worker=True)
        emit = self.rt.log.emit
        def crash(**kwargs):
            if kwargs["type"] == "team.message_received" and kwargs["payload"].get("kind") == "report":
                raise SystemExit("Crash between run terminal and report")
            return emit(**kwargs)
        self.driver.steps[self.worker] = [test_simple_team.turn("原委托已完成。")]
        with patch.object(self.rt.log, "emit", side_effect=crash), self.assertRaises(SystemExit):
            list(self.rt.team._run_message(self.task, original, self.rt.team._role(self.worker), run))
        self.assertEqual(self.events("team.run_finished"), [])
        self.assertEqual(len(self.rt.team.recovery.prepare(self.task)), 1)
        self.assertEqual(self.rt.team.recovery.assignments(self.task)[0]["seq"], original.seq)

    def test_queued_continuation_does_not_clear_interrupted_work_delivery_obligation(self):
        original, _ = self.orphan(worker=True)
        self.rt.team.recovery.prepare(self.task)
        self.driver.steps = {self.manager: [test_simple_team.turn("",
            ("send_message", {"recipient": self.worker, "text": "Continue after checking", "continue_assignment_seq": original.seq}),
            ("finish_work", {"summary": "Must not finish before worker returns", "verification": "Queued is not completed"}),
            ("wait_for_reply", {})), test_simple_team.turn("等待原委托结果。"), test_simple_team.turn("已收回工作，尚未最终验收。")],
            self.worker: [test_simple_team.turn("原委托已完成。") ]}
        self.rt.collaborate(self.task)
        receipt, = [e for e in self.events("team.tool_finished") if e.payload["name"] == "finish_work"]
        self.assertEqual(receipt.payload["result"]["code"], "work_pending")
        row, = receipt.payload["result"]["assignments"]
        self.assertEqual(row["status"], "queued")
        self.assertTrue(row["requires_return"])
        self.assertEqual(self.rt.team.view(self.task)["status"], "running")

    def test_continuation_can_explicitly_return_original_work_and_finish_its_reply(self):
        original, run = self.orphan(worker=True)
        self.finish(run)
        continuing = self.rt.team._send(self.task, self.rt.organization.leader, self.worker,
            "Inspect then return original", "continuation", continue_assignment_seq=original.seq)
        self.driver.steps = {self.worker: [test_simple_team.turn("", ("send_message", {"recipient": self.manager,
            "text": "原委托已完成，实际范围如记录。", "reply_to": original.seq})), test_simple_team.turn("原委托已正式交回。")],
            self.manager: [test_simple_team.turn("原结果已收到。", ("wait_for_reply", {}))]}
        self.rt.collaborate(self.task)
        row, = [r for r in self.rt.team.view(self.task)["runs"] if r["message_seq"] == continuing.seq]
        self.assertEqual(row["status"], "completed")
        self.assertEqual(self.rt.team.recovery.assignments(self.task), [])

    def test_unknown_published_file_cannot_be_published_again_until_reconciled(self):
        source, run = self.orphan()
        start = self.rt.log.emit(type="team.tool_started", actor=HOST, task_id=self.task, payload={
            "message_seq": source.seq, "role": self.manager, "run_id": run.payload["run_id"],
            "name": "publish_artifact", "call_id": "unknown-same-file", "arguments": {"filename": "guide.md"}})
        self.rt.team.recovery.prepare(self.task)
        arguments = {"filename": "guide.md", "format": "markdown", "summary": "Must not repeat unknown write", "content": "# Guide"}
        self.driver.steps[self.manager] = [test_simple_team.turn("", ("publish_artifact", arguments)), test_simple_team.turn("先核对原发布影响。")]
        self.rt.collaborate(self.task)
        receipt, = [e for e in self.events("team.tool_finished") if e.payload["name"] == "publish_artifact"]
        self.assertEqual(receipt.payload["result"]["status"], "error")
        self.assertEqual(self.rt.task(self.task).deliverables, [])
        self.assertEqual([r["started_seq"] for r in self.rt.team._pending_operations(self.task)], [start.seq])

    def test_unknown_publication_uses_the_real_publishers_normalized_filename(self):
        source, run = self.orphan()
        arguments = {"filename": " guide.md ", "format": "markdown", "summary": "Actual first publication", "content": "# Original guide"}
        start = self.rt.log.emit(type="team.tool_started", actor=HOST, task_id=self.task, payload={
            "message_seq": source.seq, "role": self.manager, "run_id": run.payload["run_id"],
            "name": "publish_artifact", "call_id": "spaced-file-publication", "arguments": {"filename": " guide.md "}})
        result = self.rt.team._execute(self.task, self.rt.organization.leader, "publish_artifact", arguments,
                                       None, self.rt.workspaces.view(self.task), start, None)
        self.assertEqual(result["filename"], "guide.md")
        first_path = Path(result["local_path"])
        original_bytes = first_path.read_bytes()
        self.rt.team.recovery.prepare(self.task)
        self.driver.steps[self.manager] = [test_simple_team.turn("", ("publish_artifact", {**arguments, "filename": "guide.md"})),
                                          test_simple_team.turn("先核对已发布文件。")]
        self.rt.collaborate(self.task)
        self.assertEqual(len(self.rt.task(self.task).deliverables), 1)
        self.assertEqual(first_path.read_bytes(), original_bytes)
        receipt, = [e for e in self.events("team.tool_finished") if e.payload["name"] == "publish_artifact"]
        self.assertEqual(receipt.payload["result"]["status"], "error")

    def test_unknown_publication_blocks_delivery_and_needs_trusted_reconciliation(self):
        source, run = self.orphan()
        start = self.rt.log.emit(type="team.tool_started", actor=HOST, task_id=self.task, payload={
            "message_seq": source.seq, "role": self.manager, "run_id": run.payload["run_id"],
            "name": "publish_artifact", "call_id": "unknown-publication", "arguments": {}})
        self.rt.team.recovery.prepare(self.task)
        self.driver.steps[self.manager] = [test_simple_team.turn("", ("finish_work", {
            "summary": "Unknown publication", "verification": "Must not deliver"})), test_simple_team.turn("发布影响仍待核对。")]
        self.rt.collaborate(self.task)
        receipt, = [e for e in self.events("team.tool_finished") if e.payload["name"] == "finish_work"]
        self.assertEqual(receipt.payload["result"]["code"], "operation_pending")
        self.assertEqual(receipt.payload["result"]["started_seqs"], [start.seq])
        with self.assertRaises(ValueError):
            self.rt.team.recovery.reconcile_publication(self.task, start.seq, reason="Not confirmed stopped")
        with activity_owner(self.rt.log.path, self.task, "simple-team"), self.assertRaises(WorkConflict):
            self.rt.team.recovery.reconcile_publication(self.task, start.seq, reason="Held by live consumer", invocation_stopped=True)
        confirmation = self.rt.team.recovery.reconcile_publication(self.task, start.seq,
            reason="Fixture has no published event or created files; inspected after process stopped", invocation_stopped=True)
        self.assertEqual(confirmation.actor, Actor.human())
        self.assertEqual(confirmation.payload["started_hash"], start.hash)
        self.assertEqual(self.rt.team._pending_operations(self.task), [])
        self.assertFalse(any(t["name"].startswith("reconcile") for t in self.rt.team._schemas(self.rt.organization.leader, None)))

    def test_recovery_does_not_bypass_pending_rejected_or_outdated_review(self):
        for status in ("pending", "changes_requested", "outdated"):
            with self.subTest(status=status):
                task = self.rt.create_task(f"Review recovery {status}")
                self.rt.team.enable(task)
                self.rt.team.receive(task, "Check the current work", request_id=f"recovery-review-{status}")
                self.rt.team._claim(task)
                payload, = normalize_deliverables([{"filename": "result.txt", "content": "Original result",
                    "title": "Result", "summary": "Fixture source", "format": "text"}])
                artifact = self.rt.log.emit(type="deliverable.published", task_id=task,
                    actor=Actor.agent(self.worker, "worker"), payload=payload)
                request = self.rt.log.emit(type="team.review_requested", task_id=task, actor=HOST, payload={
                    "root_seq": None, "author": self.worker, "reviewer": "test", "revision": 0,
                    "instruction": "Inspect this exact version", "run_id": "fixture-review",
                    "artifacts": [{"seq": artifact.seq, "filename": "result.txt", "content_sha256": payload["content_sha256"]}]})
                if status == "changes_requested":
                    self.rt.log.emit(type="team.review_recorded", task_id=task, actor=HOST, payload={
                        "root_seq": request.seq, "review_seq": request.seq, "verdict": status,
                        "reviewer": "test", "feedback": "A concrete issue still needs repair"})
                if status == "outdated":
                    newer, = normalize_deliverables([{"filename": "result.txt", "content": "New unreviewed bytes",
                        "title": "Result", "summary": "New version", "format": "text"}])
                    self.rt.log.emit(type="deliverable.published", task_id=task, actor=Actor.agent(self.worker, "worker"), payload=newer)
                self.rt.team.recovery.prepare(task)
                self.driver.steps[self.manager] = [test_simple_team.turn("", ("finish_work", {
                    "summary": "Must not bypass review", "verification": "Review is still open"})),
                    test_simple_team.turn("原复核尚未闭环。")]
                self.rt.collaborate(task)
                receipt, = [e for e in self.rt.log.read(task_id=task) if e.type == "team.tool_finished" and e.payload["name"] == "finish_work"]
                self.assertEqual(receipt.payload["result"]["code"], "review_pending")
                self.assertEqual(receipt.payload["result"]["reviews"][0]["status"], status)
                self.assertEqual(self.rt.team.view(task)["status"], "running")

    def test_invalid_continuation_options_are_error_receipts_without_messages(self):
        original, run = self.orphan(worker=True)
        self.finish(run)
        self.driver.steps[self.manager] = [test_simple_team.turn("",
            ("send_message", {"recipient": self.worker, "text": "Invalid", "continue_assignment_seq": original.seq, "notify_only": True}),
            ("send_message", {"recipient": self.worker, "text": "Invalid", "continue_assignment_seq": original.seq, "reply_to": original.seq}),
            ("send_message", {"recipient": self.worker, "text": "Invalid", "continue_assignment_seq": None})),
            test_simple_team.turn("不合法的接续没有投递。")]
        self.rt.team.receive(self.task, "Inspect invalid continuation", request_id="invalid-continuation-options")
        self.rt.collaborate(self.task)
        results = [e.payload["result"] for e in self.events("team.tool_finished") if e.payload["name"] == "send_message"]
        self.assertEqual([r["status"] for r in results], ["error"] * 3)
        self.assertFalse([e for e in self.events("team.message_received") if e.payload.get("kind") == "continuation"])

    def test_real_process_crash_after_file_write_does_not_repeat_external_effect(self):
        workspace = self.root / "project"
        workspace.mkdir()
        config = self.root / "workspaces.json"
        config.write_text(json.dumps({"workspaces": [{"id": "project", "title": "Project",
            "root": str(workspace), "mode": "edit"}]}), encoding="utf-8")
        self.rt.workspaces = WorkspaceService(self.rt, config)
        self.rt.workspaces.bind(self.task, "project", expected_revision=None)
        code = r'''
import os, sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / "tests"))
import test_simple_team
from cater.runtime import Runtime
driver = test_simple_team.TeamDriver()
with Runtime(sys.argv[1], roles_path=sys.argv[2], workspaces_path=sys.argv[4], drivers={"team_test": driver}) as rt:
    task = sys.argv[3]
    driver.steps[rt.organization.leader.id] = [test_simple_team.turn("", ("workspace_write", {
        "path": "marker.txt", "content": "only-once", "expected_sha256": None}))]
    rt.team.receive(task, "Write the isolated marker", request_id="crash-file-write-request")
    emit = rt.log.emit
    def crashing(**kwargs):
        if kwargs["type"] == "team.tool_finished":
            os._exit(41)
        return emit(**kwargs)
    rt.log.emit = crashing
    rt.collaborate(task)
'''
        result = subprocess.run([sys.executable, "-X", "utf8", "-B", "-c", code, self.rt.log.path,
            str(self.roles), self.task, str(config)], cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 41, result.stderr.decode("utf-8", errors="replace"))
        marker = workspace / "marker.txt"
        self.assertEqual(marker.read_text(), "only-once")
        self.driver.steps[self.manager] = [test_simple_team.turn("先核对现状。", ("read_checkpoint", {}),
            ("finish_work", {"summary": "Must not deliver", "verification": "Unknown file outcome"})),
            test_simple_team.turn("文件存在，但原操作没有可信结束回执，尚未交付。")]
        self.rt.collaborate(self.task)
        self.assertEqual(marker.read_text(), "only-once")
        self.assertEqual(sum(e.payload["name"] == "workspace_write" for e in self.events("team.tool_started")), 1)
        receipt = [e for e in self.events("team.tool_finished") if e.payload["name"] == "finish_work"][-1]
        self.assertEqual(receipt.payload["result"]["code"], "operation_pending")
        self.assertEqual(self.rt.team.view(self.task)["status"], "running")
        self.assertEqual(self.driver.calls[0]["history"], [])
        self.assertTrue(self.rt.verify_chain().ok)


if __name__ == "__main__":
    unittest.main()
