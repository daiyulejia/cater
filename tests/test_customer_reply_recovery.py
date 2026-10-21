"""Recover a committed customer answer without replaying an answered input."""

import copy
import unittest
from unittest.mock import patch

from cater import team_reply
from cater.events import Actor
from cater.team import HOST, LOCAL_EVENT_CONTRACT
from cater.workspaces import HOST as WORKSPACE_HOST
import test_team_external as external


class CustomerReplyRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.f = external.TeamExternalTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.rt, self.task = self.f.rt, self.f.task
        self.manager, self.worker = self.f.manager, self.f.worker

    def events(self, kind, task=None):
        return list(self.rt.log.iter_events(task_id=task or self.task, event_types=(kind,)))

    def crash_before_host_finish(self, script):
        self.f.driver.scripts = {self.manager: [script]}
        source = self.f.receive("Explain the saved facts; keep the current objective.")
        with patch.object(self.rt.team, "_finish_message", side_effect=SystemExit("after client settlement")):
            with self.assertRaises(SystemExit):
                self.rt.collaborate(self.task)
        self.assertEqual(self.f.driver.errors, [])
        self.assertEqual(self.events("team.run_finished"), [])
        run, = self.events("team.run_started")
        client_finish, = self.events("team.external_run_finished")
        self.assertIsInstance(self.f.driver.runs[-1], external.ScriptedExternalRun)
        self.f.assert_receipt_chain()
        self.assertTrue(self.rt.verify_chain().ok)
        return source, run, client_finish

    def crash_after_reply(self, *, cleanup_complete=True, prelude=None):
        def answer(run):
            run.cleanup_complete = cleanup_complete
            if prelude:
                prelude(run)
            run.call("reply_to_customer", text="The original customer answer is already sent.")
            self.fail("A successful customer reply must yield immediately")

        source, run, client_finish = self.crash_before_host_finish(answer)
        message, = self.events("msg.said")
        receipt, = [e for e in self.events("team.tool_finished")
                    if e.payload["name"] == "reply_to_customer"]
        self.assertEqual(receipt.payload["result"], {
            "status": "ok", "message_seq": message.seq, "reply_to": source.seq})
        self.assertLess(message.seq, receipt.seq)
        self.assertLess(receipt.seq, client_finish.seq)
        self.assertTrue(self.f.driver.runs[-1].revoked)
        return run, message, client_finish

    def assert_recovered_answer(self, run, message):
        original = self.rt.team.view(self.task)
        calls = len(self.f.driver.runs)
        self.assertEqual(self.rt.team.recovery.prepare(self.task), [])
        finished, = self.events("team.run_finished")
        self.assertEqual(finished.actor, HOST)
        self.assertEqual(finished.payload["run_id"], run.payload["run_id"])
        self.assertEqual(finished.payload["status"], "completed")
        self.assertEqual(finished.payload["recovered_customer_reply_seq"], message.seq)
        self.assertEqual(self.events("msg.said"), [message])
        self.assertFalse([e for e in self.events("team.message_received")
                          if e.payload.get("kind") == "recovery"])
        current = self.rt.team.view(self.task)
        self.assertEqual((current["objective"], current["revision"]),
                         (original["objective"], original["revision"]))
        for kind in ("team.objective_completed", "team.objective_updated", "team.review_recorded",
                     "team.external_run_reconciled", "workspace.tool_reconciled", "team.tool_reconciled"):
            self.assertEqual(self.events(kind), [], kind)
        after = self.rt.log.read(task_id=self.task)
        self.assertEqual(self.rt.team.recovery.prepare(self.task), [])
        self.assertEqual(self.rt.log.read(task_id=self.task), after)
        self.assertEqual(len(self.f.driver.runs), calls)
        self.assertTrue(self.rt.verify_chain().ok)

    def candidate(self, defect):
        """Persist deliberately invalid reply evidence in a disposable task."""
        task = self.rt.create_task("Untrusted reply evidence: " + defect)
        self.rt.team.enable(task)
        role = self.rt.team._role(self.manager)
        if defect == "agent_source":
            source = self.rt.team._send(task, self.rt.team._role(self.worker), self.manager,
                                        "Return this work to the sender.", "assignment")
        else:
            source = self.rt.team.receive(task, "This input has no proven customer answer.")
        claimed, _, run = self.rt.team._claim(task)
        self.assertEqual(claimed, source)
        start = None
        if defect != "standalone":
            contract = dict(LOCAL_EVENT_CONTRACT)
            if defect == "contract":
                contract["receipt"] = "separate_transaction"
            start = self.rt.log.emit(type="team.tool_started", task_id=task, actor=HOST, payload={
                **{k: run.payload[k] for k in ("run_id", "role", "message_seq", "revision")},
                "name": "reply_to_customer", "call_id": "candidate-reply",
                "execution_contract": contract, "arguments": {"text": "The bound answer."}})

        def receipt(message_seq):
            return self.rt.log.emit(type="team.tool_finished", task_id=task,
                actor=Actor.agent(role.id, role.role) if defect == "nonhost_receipt" else HOST,
                payload={"started_seq": start.seq, "run_id": run.payload["run_id"], "role": role.id,
                         "name": "reply_to_customer", "result": {"status": "ok",
                             "message_seq": message_seq, "reply_to": source.seq}})

        if defect == "ordering":
            # Reference the next event, but settle before that message exists.
            receipt(self.rt.log.read(task_id=task)[-1].seq + 2)
        message = self.rt.log.emit(type="msg.said", task_id=task, actor=Actor.agent(role.id, role.role),
            payload={"text": "A different answer." if defect == "text" else "The bound answer.",
                     "phase": "team", "run_id": run.payload["run_id"], "reply_to": source.seq,
                     "customer_reply_tool": "reply_to_customer", "audience": "customer",
                     "recipient": "human", "response_kind": "reply"})
        if defect not in {"standalone", "unpaired", "ordering"}:
            receipt(source.seq if defect == "result_link" else message.seq)
        return task, run

    def assert_normal_recovery(self, task, run):
        before = self.rt.log.read(task_id=task)
        self.assertIsNone(team_reply.committed_reply(self.rt.team, run, before))
        self.assertEqual(self.rt.log.read(task_id=task), before, "Proof lookup must be read-only")
        queued = self.rt.team.recovery.prepare(task)
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0].payload["recovery_of"], run.seq)
        finished, = self.events("team.run_finished", task)
        self.assertEqual(finished.payload["status"], "interrupted")
        self.assertNotIn("recovered_customer_reply_seq", finished.payload)
        after = self.rt.log.read(task_id=task)
        self.assertEqual(self.rt.team.recovery.prepare(task), queued)
        self.assertEqual(self.rt.log.read(task_id=task), after)
        self.assertTrue(self.rt.verify_chain().ok)

    def test_committed_reply_recovers_completed_once_without_supervision_or_duplicate(self):
        run, message, client_finish = self.crash_after_reply()
        self.assertTrue(client_finish.payload["cleanup_complete"])
        before = self.rt.log.read(task_id=self.task)
        self.assertEqual(team_reply.committed_reply(self.rt.team, run, before), message)
        self.assertEqual(self.rt.log.read(task_id=self.task), before)
        self.assert_recovered_answer(run, message)
        self.assertEqual(self.rt.team.external_runs.pending(self.task), [])
        self.assertEqual(self.rt.team._pending_operations(self.task), [])

    def test_recovering_answer_does_not_confirm_incomplete_external_cleanup(self):
        run, message, client_finish = self.crash_after_reply(cleanup_complete=False)
        self.assertFalse(client_finish.payload["cleanup_complete"])
        pending, = self.rt.team.external_runs.pending(self.task)
        self.assertEqual(pending["outcome"], "unknown")
        self.assert_recovered_answer(run, message)
        recovered, = self.rt.team.external_runs.pending(self.task)
        for key in ("started_seq", "run_id", "message_seq", "role", "outcome", "replay_authorized"):
            self.assertEqual(recovered[key], pending[key], key)
        self.assertTrue(recovered["run_ended"])
        self.assertFalse(recovered["replay_authorized"])
        self.assertEqual(self.events("team.external_run_finished"), [client_finish])

    def test_recovering_answer_preserves_unknown_workspace_and_queued_delegation(self):
        workspace_start = self.rt.log.emit(type="workspace.tool_started", task_id=self.task,
            actor=WORKSPACE_HOST, payload={"name": "workspace_run", "call_id": "unknown-command",
                "root": str(self.f.root), "arguments": {"command": "offline-never-executed"}})
        self.rt.log.emit(type="workspace.tool_finished", task_id=self.task, actor=WORKSPACE_HOST,
            payload={"started_seq": workspace_start.seq, "call_id": "unknown-command",
                     "result": {"status": "error", "complete": False, "outcome": "effects_unknown"}})

        def delegate(run):
            result = run.call("delegate", recipient=self.worker, instruction="This queued work still needs execution.")
            self.assertEqual(result["status"], "ok")

        run, message, _ = self.crash_after_reply(prelude=delegate)
        workspace_pending = self.rt.workspaces.pending(task_id=self.task)
        self.assertEqual([e.seq for e in workspace_pending], [workspace_start.seq])
        queued = copy.deepcopy(self.rt.team.recovery.assignments(self.task))
        self.assertEqual(len(queued), 1)
        self.assertEqual((queued[0]["recipient"], queued[0]["status"]), (self.worker, "queued"))
        self.assert_recovered_answer(run, message)
        self.assertEqual(self.rt.workspaces.pending(task_id=self.task), workspace_pending)
        self.assertEqual(self.rt.team.recovery.assignments(self.task), queued)
        self.assertEqual(self.rt.team.view(self.task)["pending_count"], 1)
        self.assertEqual([e.payload["role"] for e in self.events("team.run_started")], [self.manager])

    def test_standalone_unpaired_or_nonhost_answer_is_not_completion_proof(self):
        for defect in ("standalone", "unpaired", "nonhost_receipt"):
            with self.subTest(defect=defect):
                task, run = self.candidate(defect)
                self.assert_normal_recovery(task, run)

    def test_wrong_contract_bindings_order_or_source_cannot_suppress_recovery(self):
        for defect in ("contract", "text", "result_link", "ordering", "agent_source"):
            with self.subTest(defect=defect):
                task, run = self.candidate(defect)
                self.assert_normal_recovery(task, run)

    def test_crash_without_customer_answer_still_queues_normal_supervision(self):
        _, run, client_finish = self.crash_before_host_finish(lambda external_run: "")
        self.assertTrue(client_finish.payload["cleanup_complete"])
        self.assertEqual(self.events("msg.said"), [])
        self.assertEqual(self.f.results("reply_to_customer"), [])
        self.assert_normal_recovery(self.task, run)


if __name__ == "__main__":
    unittest.main()
