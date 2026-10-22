"""Offline model scripts; real Runtime, receipts, supervision and exact-version review."""
from dataclasses import replace
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from cater.acp import AcpSession, AcpTimeout
from cater.events import Actor
from cater.external_agent import ExternalRunLimit, QoderAcpDriver
import test_team_external as external


class BudgetCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.f = external.TeamExternalTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.qoder = QoderAcpDriver(self.f.driver.name, "offline-never-launch", "offline",
                                    timeout=2, max_run_seconds=10)
        self.enterContext(patch.object(self.qoder, "agent_run", side_effect=self.f.driver.agent_run))
        self.f.rt.drivers[self.qoder.name] = self.qoder

    def budget(self, run, *, last=9500, progress=1, end=10000, error=None):
        run.state = replace(run.state, observations=tuple({
            "last_progress_ms": last, "failure_ms": end, "progress_receipts": progress,
            "tool_calls": len(run.receipts)}.items()), failure_phase="prompting")
        raise error or ExternalRunLimit("total")

    def notices(self):
        return [e for e in self.f.events("team.message_received")
                if e.payload.get("trigger") == "turn_boundary"]

    def unknown(self, start):
        f = self.f
        external_start = next(e for e in f.events("team.external_run_started")
                              if e.payload["run_id"] == start.payload["run_id"])
        return f.rt.log.emit(type="team.tool_started", task_id=f.task, actor=external.HOST,
            payload={**{key: start.payload[key] for key in ("run_id", "role", "message_seq")},
                     "external_started_seq": external_start.seq, "name": "workspace_run",
                     "call_id": "unconfirmed-external-effect", "arguments": {"command": "python", "args": []}})

    def stage(self):
        f = self.f

        def work(run):
            run.call("read_checkpoint")
            self.budget(run)

        f.driver.scripts = {f.manager: [work]}
        f.receive()
        message, role, start = f.rt.team._claim(f.task)
        list(f.rt.team._run_message(f.task, message, role, start))
        notice, = self.notices()
        return start, notice

    def test_budget_after_committed_file_supervises_then_reviews_exact_bytes_and_delivers(self):
        f, published = self.f, []

        def budget(run):
            published.append(f.publish(run, "Once, before the local deadline\n"))
            self.budget(run)

        def supervise(run):
            self.assertEqual(run.context["message"]["kind"], "recovery")
            self.assertNotEqual(run.context["message"]["seq"], original.seq)
            result = run.call("read_artifact", artifact_seq=published[0])
            self.assertEqual(result["content"], "Once, before the local deadline\n")
            run.call("request_review", reviewer=f.reviewer, artifact_seqs=published,
                     instruction="Read the exact saved file; no republishing or replay")

        def review(run):
            row, = run.context["reviews"]
            body = run.call("read_artifact", artifact_seq=published[0])
            self.assertEqual(body["content"], "Once, before the local deadline\n")
            run.call("record_review", review_seq=row["request_seq"], verdict="approved",
                     feedback="Exact saved bytes read back")

        f.driver.scripts = {f.manager: [budget, supervise, f.deliver], f.reviewer: [review]}
        original = f.receive()
        f.drain()
        notice, = self.notices()
        self.assertTrue(f.rt.team.recovery.is_message(notice))
        self.assertEqual(len(f.results("publish_artifact")), 1)
        self.assertEqual(f.rt.team.view(f.task)["status"], "done")
        completed, = f.events("team.objective_completed")
        approved, = f.rt.team_reviews.view(f.task)
        self.assertEqual(completed.payload["artifacts"], approved["artifacts"])
        first = f.events("team.run_finished")[0]
        self.assertEqual(first.payload["status"], "interrupted")
        self.assertEqual(first.payload["external_failure_code"], "execution_budget")
        self.assertEqual(first.payload["boundary_reason"], "execution_budget")
        self.assertEqual(f.events("team.external_run_finished")[0].payload["outcome"], "unknown")
        self.assertEqual(sum(run.context["message"]["seq"] == original.seq for run in f.driver.runs), 1)
        self.assertEqual([e for e in f.events("team.message_received") if e.actor == Actor.human()], [original])

    def test_actual_stdio_budget_cleanup_and_new_mcp_session_do_not_republish(self):
        f, sessions = self.f, []
        self.qoder.max_run_seconds = 4
        peer = Path(__file__).parent / "fixtures/external_team_budget_peer.py"

        class Session(AcpSession):
            def __init__(self, command, **kwargs):
                super().__init__([sys.executable, "-u", str(peer.resolve()), str(f.root)], **kwargs)
                sessions.append(self)

        # Restore the actual managed adapter for this test, not its script replacement.
        with patch.object(self.qoder, "agent_run", QoderAcpDriver.agent_run.__get__(self.qoder)), \
                patch("cater.external_agent.AcpSession", Session):
            original = f.receive("Offline actual protocol: save once, then inspect and deliver")
            f.rt.collaborate(f.task)
        self.assertTrue(f.rt.verify_chain().ok)
        self.assertEqual(f.rt.team.view(f.task)["status"], "done")
        self.assertEqual(len(sessions), 2)
        self.assertTrue(all(session._proc.poll() is not None for session in sessions))
        self.assertEqual(len(f.results("publish_artifact")), 1)
        self.assertEqual(f.rt.team.external_runs.pending(f.task), [])
        notice, = self.notices()
        self.assertTrue(f.rt.team.recovery.is_message(notice))
        self.assertEqual([e.payload["status"] for e in f.events("team.run_finished")], ["interrupted", "completed"])
        self.assertEqual(sum(e.payload["message_seq"] == original.seq for e in f.events("team.run_started")), 1)
        self.assertGreater(f.events("team.model_called")[0].payload["external_observations"]["progress_receipts"], 1)

    def test_supervision_budget_does_not_recursively_wake_itself(self):
        f = self.f

        def work(run):
            f.publish(run, "Saved proposal " + str(len(f.driver.runs)))
            self.budget(run)

        f.driver.scripts = {f.manager: [work, work]}
        f.receive()
        f.drain()
        self.assertEqual(len(self.notices()), 1)
        self.assertEqual(len(f.driver.runs), 2)
        self.assertEqual(f.rt.team.recovery.prepare(f.task), [])
        self.assertEqual(f.events("team.objective_completed"), [])

    def test_pending_effect_arriving_before_finish_commit_suppresses_notification(self):
        f = self.f
        original = f.rt.team._finish_message

        def finish(task, message, role, start, revision, assignment, **kwargs):
            self.unknown(start)
            return original(task, message, role, start, revision, assignment, **kwargs)

        def work(run):
            run.call("read_checkpoint")
            self.budget(run)

        f.driver.scripts = {f.manager: [work]}
        f.receive()
        with patch.object(f.rt.team, "_finish_message", side_effect=finish):
            f.drain()
        self.assertEqual(self.notices(), [])
        self.assertEqual(len(f.rt.team._pending_operations(f.task)), 1)
        self.assertEqual(len(f.driver.runs), 1)

    def test_pending_effect_after_notification_blocks_its_claim(self):
        start, notice = self.stage()
        f = self.f
        self.assertTrue(f.rt.team.recovery.current(f.task, notice))
        self.unknown(start)
        self.assertTrue(f.rt.team.recovery.is_message(notice))
        self.assertFalse(f.rt.team.recovery.current(f.task, notice))
        self.assertIsNone(f.rt.team._claim(f.task))
        self.assertEqual(len(f.driver.runs), 1)

    def test_pending_effect_after_claim_stops_before_another_external_request(self):
        start, notice = self.stage()
        f = self.f
        message, role, supervision = f.rt.team._claim(f.task)
        self.assertEqual(message, notice)
        self.unknown(start)
        list(f.rt.team._run_message(f.task, message, role, supervision))
        self.assertEqual(len(f.driver.runs), 1)
        self.assertEqual(len(f.events("team.external_run_started")), 1)
        self.assertEqual(self.notices(), [notice])
        self.assertEqual(f.events("team.run_finished")[-1].payload["status"], "interrupted",
                         f.events("team.run_finished")[-1].payload)

    def test_reconciled_pre_admission_block_can_claim_same_supervision_but_never_replay_a_sent_request(self):
        start, notice = self.stage()
        f = self.f
        claimed = f.rt.team._claim(f.task)
        self.assertEqual(claimed[0], notice)
        f.rt.team._send(f.task, f.rt.organization.leader, f.worker, "Offline uncertain publication", "assignment")
        other, role, worker_start = f.rt.team._claim(f.task)
        self.assertEqual(role.id, f.worker)
        pending = f.rt.log.emit(type="team.tool_started", actor=external.HOST, task_id=f.task,
            payload={**{key: worker_start.payload[key] for key in ("run_id", "role", "message_seq")},
                     "name": "publish_artifact", "call_id": "unconfirmed", "arguments": {"filename": "unconfirmed.txt"}})
        f.rt.log.emit(type="team.run_finished", actor=external.HOST, task_id=f.task,
            payload={**{key: worker_start.payload[key] for key in ("run_id", "role", "message_seq")},
                     "status": "interrupted", "reason": "Offline uncertain publication"})
        list(f.rt.team._run_message(f.task, *claimed))
        self.assertEqual(len(f.driver.runs), 1)
        self.assertIs(f.events("team.run_finished")[-1].payload["admission_not_started"], True)
        self.assertEqual(f.rt.team.recovery.prepare(f.task), [])
        f.rt.team.recovery.reconcile_publication(f.task, pending.seq,
            reason="Offline fixture never invoked a publisher; process stopped", invocation_stopped=True)
        self.assertEqual(f.rt.team.recovery.prepare(f.task), [notice])
        self.assertEqual(f.rt.team.view(f.task)["pending_count"], 1)
        same = f.rt.team._claim(f.task)
        self.assertEqual(same[0], notice)
        self.assertNotEqual(same[2].payload["run_id"], claimed[2].payload["run_id"])

        def transmitted(run):
            self.assertEqual(run.context["message"]["seq"], notice.seq)
            raise AcpTimeout("Offline: actual prompt attempted, response unknown")

        f.driver.scripts = {f.manager: [transmitted]}
        list(f.rt.team._run_message(f.task, *same))
        self.assertEqual(len(f.driver.runs), 2)
        self.assertEqual(f.rt.team.recovery.prepare(f.task), [])
        self.assertIsNone(f.rt.team._claim(f.task))
        self.assertEqual(self.notices(), [notice])
        self.assertTrue(f.rt.verify_chain().ok)

    def test_client_settlement_failure_does_not_erase_budget_error_or_authorize_supervision(self):
        f = self.f

        def work(run):
            run.call("read_checkpoint")
            self.budget(run)

        f.driver.scripts = {f.manager: [work]}
        f.receive()
        with patch.object(f.rt.team.external_runs, "finish", side_effect=OSError("Offline settlement failure")):
            f.drain()
        self.assertEqual(self.notices(), [])
        failed, = f.events("team.run_finished")
        self.assertEqual(failed.payload["external_failure_code"], "execution_budget")
        self.assertEqual(failed.payload["external_settlement_error"], "OSError")
        self.assertTrue(f.rt.team.external_runs.pending(f.task))

    def test_next_supervision_still_checks_configured_channel_usage_budget(self):
        f, usage = self.f, [0]
        self.qoder.token_budget = 1
        self.enterContext(patch.object(f.rt, "_reported_driver_tokens", side_effect=lambda name: usage[0]))

        def work(run):
            f.publish(run)
            usage[0] = 1
            self.budget(run)

        f.driver.scripts = {f.manager: [work]}
        f.receive()
        f.drain()
        self.assertEqual(len(self.notices()), 1)
        self.assertEqual(len(f.driver.runs), 1)
        self.assertEqual(f.events("team.objective_completed"), [])

    def test_child_budget_reports_to_original_parent_without_closing_assignment(self):
        f = self.f

        def worker(run):
            f.publish(run, "Saved child work")
            self.budget(run)

        def parent(run):
            open_work = run.context["outgoing_work"]
            self.assertTrue(open_work)
            self.assertNotEqual(open_work[0]["status"], "completed")
            run.call("wait_for_reply")

        f.driver.scripts = {f.manager: [f.delegate_wait, parent], f.worker: [worker]}
        f.receive()
        f.drain()
        self.assertEqual(self.notices(), [])
        assignment, = [e for e in f.events("team.message_received") if e.payload.get("kind") == "assignment"]
        progress, = [e for e in f.events("team.message_received")
                      if e.payload.get("kind") == "progress" and e.payload.get("reply_to") == assignment.seq]
        self.assertEqual(progress.actor.id, f.worker)
        self.assertEqual(progress.payload["recipient"], f.manager)
        self.assertIn(assignment.seq, {row["seq"] for row in f.rt.team.recovery.assignments(f.task)})
        self.assertEqual(f.events("team.objective_completed"), [])

    def test_no_tool_or_stale_tool_or_malformed_progress_cannot_authorize_checkpoint(self):
        f = self.f
        for index, facts in enumerate(({}, {"last": 1000}, {"progress": 0}, {"progress": True},
                                        {"progress": 2}, {"last": -1}, {"last": 10001}, {"end": None})):
            with self.subTest(facts=facts):
                task = f.rt.create_task("Offline invalid progress " + str(index))
                f.rt.team.enable(task)

                def fail(run, facts=facts):
                    if facts:
                        result = run.call("read_checkpoint")
                        self.assertEqual(result["status"], "ok", result)
                    self.budget(run, **facts)

                f.driver.scripts = {f.manager: [fail]}
                f.rt.team.receive(task, "Inspect, not replay", request_id="bad-progress-" + str(index))
                f.rt.collaborate(task)
                events = f.rt.log.read(task_id=task)
                self.assertFalse([e for e in events if e.payload.get("trigger") == "turn_boundary"])
                self.assertEqual([e.payload["status"] for e in events if e.type == "team.run_finished"], ["failed"])
        self.assertEqual(f.driver.errors, [])

    def test_idle_or_transport_timeout_after_a_write_does_not_gain_budget_supervision(self):
        f = self.f
        for index, error in enumerate((ExternalRunLimit("idle"), AcpTimeout("Unknown transport result"))):
            with self.subTest(error=type(error).__name__):
                task = f.rt.create_task("Offline non-budget failure " + str(index))
                f.rt.team.enable(task)

                def fail(run, error=error):
                    run.call("publish_artifact", filename="result.txt", content="Saved once", summary="Saved", format="text")
                    self.budget(run, error=error)

                f.driver.scripts = {f.manager: [fail]}
                f.rt.team.receive(task, "Preserve the real failure", request_id="non-budget-" + str(index))
                f.rt.collaborate(task)
                self.assertFalse([e for e in f.rt.log.read(task_id=task) if e.payload.get("trigger") == "turn_boundary"])
        self.assertEqual(len(f.driver.runs), 2)

    def test_unclean_or_failed_cancel_retains_failure_without_supervision(self):
        f = self.f
        for index, cleanup in enumerate((False, True)):
            with self.subTest(cleanup=cleanup):
                task = f.rt.create_task("Offline cleanup uncertainty " + str(index))
                f.rt.team.enable(task)

                def fail(run, cleanup=cleanup):
                    run.call("read_checkpoint")
                    run.cleanup_complete = cleanup
                    run.state = replace(run.state, cancel_failed=cleanup)
                    self.budget(run)

                f.driver.scripts = {f.manager: [fail]}
                f.rt.team.receive(task, "Cleanup must be real", request_id="unclean-budget-" + str(index))
                f.rt.collaborate(task)
                self.assertFalse([e for e in f.rt.log.read(task_id=task) if e.payload.get("trigger") == "turn_boundary"])
                self.assertEqual(bool(f.rt.team.external_runs.pending(task)), not cleanup)

    def test_control_or_rules_change_does_not_renew_old_authorization(self):
        f = self.f
        for index, change in enumerate(("pause", "rules", "model")):
            with self.subTest(change=change):
                task = f.rt.create_task("Offline boundary scope " + str(index))
                f.rt.team.enable(task)

                def fail(run, change=change):
                    run.call("read_checkpoint")
                    if change == "pause":
                        f.rt.execution_control.change(task, "pause", request_id="pause-before-budget")
                    elif change == "rules":
                        f.rt.rulebook.record("Discuss only", scope="task", task_id=task)
                    else:
                        self.qoder.model = "changed-model"
                    self.budget(run)

                f.driver.scripts = {f.manager: [fail]}
                f.rt.team.receive(task, "Do not continue stale work", request_id="scope-budget-" + str(index))
                f.rt.collaborate(task)
                self.assertFalse([e for e in f.rt.log.read(task_id=task) if e.payload.get("trigger") == "turn_boundary"])
                self.qoder.model = "offline"


if __name__ == "__main__":
    unittest.main()
