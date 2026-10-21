"""Superseded review work must retire without erasing ordinary work or effects."""

import unittest
from unittest.mock import patch

from cater.team import HOST
from cater.tool_protocol import ToolCall
from cater.work import WorkConflict
import test_team_review_cycle as review_cycle


class SupersededReviewAssignmentsTests(unittest.TestCase):
    def setUp(self):
        self.f = review_cycle.TeamReviewCycleTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.rt, self.task = self.f.rt, self.f.task

    def message(self, seq):
        return next(e for e in self.rt.team._messages(self.task) if e.seq == seq)

    def start(self, source):
        return self.rt.log.emit(type="team.run_started", actor=HOST, task_id=self.task,
            payload={"message_seq": source.seq, "role": source.payload["recipient"],
                     "run_id": f"superseded-review-{source.seq}",
                     "revision": self.rt.team.view(self.task)["revision"],
                     "control_seq": self.rt.execution_control.view(self.task)["seq"]})

    def end(self, run, status="interrupted"):
        return self.rt.log.emit(type="team.run_finished", actor=HOST, task_id=self.task,
            payload={**run.payload, "status": status, "reason": "Disposable crash fixture"})

    def begin_review(self):
        artifact = self.f.publish("Initial version\n")
        delegated, = self.f.exchange(self.f.manager, ("delegate", {
            "recipient": self.f.worker, "instruction": "Publish, obtain review, and return the result."}))
        self.assertEqual(delegated["status"], "ok", delegated)
        self.parent = self.message(delegated["message_seq"])
        requested, = self.f.exchange(self.f.worker, ("request_review", {
            "reviewer": self.f.reviewer, "artifact_seqs": [artifact],
            "instruction": "Inspect this exact version."}), source=self.parent)
        self.assertEqual(requested["status"], "ok", requested)
        self.old = requested
        self.old_message = self.message(requested["message_seq"])
        return requested

    def interrupt_review(self, status):
        run = self.start(self.old_message)
        error = WorkConflict("Injected interrupted review") if status == "interrupted" else RuntimeError("Injected lost consumer")
        role = self.rt.team._role(self.f.reviewer)
        # Fail before a model turn; the production run finalizer records progress,
        # not a formal return or a review decision.
        with patch.object(self.rt.team, "_current", side_effect=error):
            list(self.rt.team._run_message(self.task, self.old_message, role, run))
        finished, = [e for e in self.f.events("team.run_finished")
                       if e.payload["run_id"] == run.payload["run_id"]]
        self.assertEqual(finished.payload["status"], status)
        row = next(r for r in self.rt.team.recovery.assignments(self.task)
                   if r["seq"] == self.old_message.seq)
        self.assertTrue(row["requires_return"])
        return run

    def replace_review(self):
        artifact = self.f.publish("Revised version\n")
        latest = self.f.request([artifact], review_seq=self.old["review_seq"])
        self.assertEqual(latest["status"], "ok", latest)
        self.assertEqual(latest["root_seq"], self.old["root_seq"])
        self.assertNotEqual(latest["review_seq"], self.old["review_seq"])
        self.latest = latest
        return latest

    def approve_and_return(self):
        decision = self.f.record(self.latest["review_seq"],
                                 source=self.message(self.latest["message_seq"]))
        self.assertEqual(decision["status"], "ok", decision)
        returned, = self.f.exchange(self.f.worker, ("send_message", {
            "recipient": self.f.manager, "reply_to": self.parent.seq,
            "text": "The revised exact version is approved; returning the original assignment."}),
            source=self.message(decision["message_seq"]))
        self.assertEqual(returned["status"], "ok", returned)
        report = self.message(returned["message_seq"])
        self.assertEqual((report.payload["kind"], report.payload["reply_to"]), ("report", self.parent.seq))
        self.f.exchange(self.f.manager, source=report)

    def test_interrupted_review_replaced_approved_and_returned_can_finish(self):
        self.begin_review()
        self.interrupt_review("interrupted")
        self.replace_review()
        self.approve_and_return()
        result = self.f.finish()
        self.assertEqual(result["status"], "ok", result)
        self.assertNotIn(self.old_message.seq,
                         {r["seq"] for r in self.rt.team.recovery.assignments(self.task)})
        self.assertEqual(len(self.f.events("team.objective_completed")), 1)
        self.assertTrue(self.rt.verify_chain().ok)

    def test_failed_review_replaced_approved_and_returned_can_finish(self):
        self.begin_review()
        self.interrupt_review("failed")
        self.replace_review()
        self.approve_and_return()
        result = self.f.finish()
        self.assertEqual(result["status"], "ok", result)

    def test_replacement_still_pending_blocks_on_current_review(self):
        self.begin_review()
        self.interrupt_review("interrupted")
        self.replace_review()
        result = self.f.finish()
        self.assertEqual((result["status"], result.get("code")), ("error", "review_pending"), result)
        self.assertIn(self.latest["review_seq"], {r["request_seq"] for r in result["reviews"]})
        self.assertEqual(self.f.events("team.objective_completed"), [])

    def test_ordinary_assignment_cannot_be_hidden_by_forged_review_seq(self):
        self.begin_review()
        # Correct author/reviewer identities and an old review_seq alone must
        # not turn a second, ordinary assignment into the actual review request.
        ordinary = self.rt.team._send(self.task, self.rt.team._role(self.f.worker),
            self.f.reviewer, "Independent analysis still requires a return.", "assignment",
            review_seq=self.old["review_seq"])
        self.end(self.start(ordinary), "failed")
        self.replace_review()
        self.approve_and_return()
        rows = self.rt.team.recovery.assignments(self.task)
        row = next(r for r in rows if r["seq"] == ordinary.seq)
        self.assertTrue(row["requires_return"])
        result = self.f.finish()
        self.assertEqual((result["status"], result.get("code")), ("error", "work_pending"), result)
        self.assertIn(ordinary.seq, {r["seq"] for r in result["assignments"]})
        self.assertEqual(self.f.events("team.objective_completed"), [])

    def test_queued_old_review_is_not_claimed_after_replacement(self):
        self.begin_review()
        self.replace_review()
        claimed = self.rt.team._claim(self.task)
        self.assertIsNotNone(claimed)
        source, role, run = claimed
        self.assertEqual(source.seq, self.latest["message_seq"], source.payload)
        self.assertEqual(role.id, self.f.reviewer)
        self.assertNotEqual(run.payload["message_seq"], self.old_message.seq)

    def test_running_old_review_cannot_dispatch_next_tool_after_replacement(self):
        self.begin_review()
        run = self.start(self.old_message)
        self.replace_review()
        role = self.rt.team._role(self.f.reviewer)
        tools = self.rt.team._schemas(role, None)
        before = self.f.events("team.tool_started")
        call = ToolCall("superseded-next-tool", "read_artifact", {
            "artifact_seq": self.rt.team_reviews.view(self.task)[0]["artifacts"][0]["seq"]})
        with self.assertRaises(WorkConflict):
            self.rt.team._dispatch_tool(self.task, self.old_message, role, run,
                run.payload["revision"], call, tools, None,
                self.rt.workspaces.view(self.task), {}, self.old_message)
        self.assertEqual(self.f.events("team.tool_started"), before)

    def test_superseded_continuation_is_not_claimed_or_sent_to_model(self):
        self.begin_review()
        self.interrupt_review("interrupted")
        continued, = self.f.exchange(self.f.worker, ("send_message", {
            "recipient": self.f.reviewer, "text": "Resume the interrupted exact-version review.",
            "continue_assignment_seq": self.old_message.seq}))
        self.assertEqual(continued["status"], "ok", continued)
        continuation = self.message(continued["message_seq"])
        self.assertEqual(continuation.payload["kind"], "continuation")
        self.replace_review()
        self.assertIn(continuation.seq, self.rt.team_reviews.superseded_assignments(self.task))
        claimed = self.rt.team._claim(self.task)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed[0].seq, self.latest["message_seq"])
        run = self.start(continuation)
        with patch.object(self.f.driver, "tool_turn") as model:
            list(self.rt.team._run_message(self.task, continuation,
                self.rt.team._role(self.f.reviewer), run))
        model.assert_not_called()
        finished, = [e for e in self.f.events("team.run_finished")
                       if e.payload["run_id"] == run.payload["run_id"]]
        self.assertEqual(finished.payload["status"], "interrupted")
        self.assertFalse(any(e.payload["run_id"] == run.payload["run_id"]
                             for e in self.f.events("team.tool_started")))

    def test_old_progress_remains_history_without_blocking_returned_author(self):
        self.begin_review()
        self.interrupt_review("interrupted")
        progress, = [e for e in self.rt.team._messages(self.task)
                       if e.payload.get("kind") == "progress"
                       and e.payload.get("reply_to") == self.old_message.seq]
        self.assertEqual(progress.payload["recipient"], self.f.worker)
        self.replace_review()
        self.approve_and_return()
        self.assertIn(progress.seq, self.rt.team_reviews.superseded_assignments(self.task))
        self.assertEqual(self.message(progress.seq), progress)
        self.assertFalse(any(e.payload["message_seq"] == progress.seq
                             for e in self.f.events("team.run_started")))
        result = self.f.finish()
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(len(self.f.events("team.objective_completed")), 1)

    def test_approved_replacement_does_not_return_original_development_assignment(self):
        self.begin_review()
        self.interrupt_review("interrupted")
        self.replace_review()
        decision = self.f.record(self.latest["review_seq"],
                                 source=self.message(self.latest["message_seq"]))
        self.assertEqual(decision["status"], "ok", decision)
        artifact = self.rt.team_reviews.view(self.task)[0]["artifacts"][0]["seq"]
        inspected, = self.f.exchange(self.f.worker, ("read_artifact", {
            "artifact_seq": artifact}), source=self.message(decision["message_seq"]))
        self.assertEqual(inspected["status"], "ok", inspected)
        self.assertNotIn(self.parent.seq, self.rt.team_reviews.superseded_assignments(self.task))
        self.assertIn(self.parent.seq,
                      {r["seq"] for r in self.rt.team.recovery.assignments(self.task)})
        result = self.f.finish()
        self.assertEqual((result["status"], result.get("code")), ("error", "author_pending"), result)
        self.assertIn(self.parent.seq, {r["seq"] for r in result["assignments"]})
        self.assertEqual(self.f.events("team.objective_completed"), [])

    def test_unrelated_reviewer_message_to_author_is_not_superseded(self):
        self.begin_review()
        self.interrupt_review("interrupted")
        ordinary = self.rt.team._send(self.task, self.rt.team._role(self.f.reviewer),
            self.f.worker, "Separate follow-up work still needs an answer.", "message",
            review_seq=self.old["review_seq"])
        self.replace_review()
        self.approve_and_return()
        self.assertNotIn(ordinary.seq, self.rt.team_reviews.superseded_assignments(self.task))
        result = self.f.finish()
        self.assertEqual((result["status"], result.get("code")), ("error", "author_pending"), result)
        self.assertIn(ordinary.seq, {r["seq"] for r in result["assignments"]})
        self.assertEqual(self.f.events("team.objective_completed"), [])

    def test_interrupted_current_review_is_not_retired_with_its_predecessor(self):
        self.begin_review()
        self.replace_review()
        current_message = self.message(self.latest["message_seq"])
        self.end(self.start(current_message))
        retired = self.rt.team_reviews.superseded_assignments(self.task)
        self.assertIn(self.old_message.seq, retired)
        self.assertNotIn(current_message.seq, retired)
        assignments = self.rt.team.recovery.assignments(self.task)
        current = next(r for r in assignments if r["seq"] == current_message.seq)
        self.assertTrue(current["requires_return"])
        self.assertEqual(current["status"], "interrupted")
        result = self.f.finish()
        self.assertEqual((result["status"], result.get("code")), ("error", "work_pending"), result)
        self.assertIn(current_message.seq, {r["seq"] for r in result["assignments"]})
        self.assertNotIn(self.old_message.seq, {r["seq"] for r in result["assignments"]})
        self.assertEqual(self.f.events("team.objective_completed"), [])

    def test_superseded_projection_respects_explicit_event_snapshot(self):
        self.begin_review()
        snapshot = self.rt.log.read(task_id=self.task)
        self.replace_review()
        historical = self.rt.team_reviews.superseded_assignments(self.task, events=snapshot)
        live = self.rt.team_reviews.superseded_assignments(self.task)
        self.assertIsInstance(historical, set)
        self.assertNotIn(self.old_message.seq, historical)
        self.assertIn(self.old_message.seq, live)
        self.assertNotIn(self.latest["message_seq"], live)

    def test_context_preserves_current_review_and_original_development_work(self):
        self.begin_review()
        self.interrupt_review("interrupted")
        self.replace_review()
        reviewer = self.f.context(role_id=self.f.reviewer)
        author = self.f.context(role_id=self.f.worker)
        self.assertNotIn(self.old_message.seq,
                         {r["seq"] for r in reviewer["open_assignments"]})
        self.assertIn(self.latest["message_seq"],
                      {r["seq"] for r in reviewer["open_assignments"]})
        self.assertNotIn(self.old_message.seq, {r["seq"] for r in author["outgoing_work"]})
        self.assertIn(self.latest["message_seq"], {r["seq"] for r in author["outgoing_work"]})
        self.assertIn(self.parent.seq, {r["seq"] for r in author["open_assignments"]})
        self.assertEqual(reviewer["reviews"][0]["request_seq"], self.latest["review_seq"])
        self.assertEqual(reviewer["reviews"][0]["status"], "pending")

    def test_unknown_workspace_effect_from_superseded_review_still_blocks_finish(self):
        self.begin_review()
        root = self.f.bind_workspace()
        run = self.start(self.old_message)
        unknown = self.rt.log.emit(type="team.tool_started", actor=HOST, task_id=self.task,
            payload={**run.payload, "name": "workspace_write", "call_id": "unknown-review-write",
                     "root": str(root), "arguments": {"path": "effect.txt", "expected_sha256": None}})
        self.end(run)
        self.replace_review()
        self.approve_and_return()
        self.assertIn(unknown.seq, {e.seq for e in self.rt.workspaces.pending(task_id=self.task)})
        result = self.f.finish()
        self.assertEqual((result["status"], result.get("code")), ("error", "operation_pending"), result)
        self.assertIn(unknown.seq, result["started_seqs"])
        self.assertEqual(self.f.events("team.objective_completed"), [])

    def test_unknown_external_effect_from_superseded_review_still_blocks_finish(self):
        self.begin_review()
        run = self.start(self.old_message)
        self.replace_review()
        self.approve_and_return()
        role = self.rt.team._role(self.f.reviewer)
        external = self.rt.team.external_runs.begin(self.task, role, run, basis={},
            binding=self.rt.workspaces.view(self.task), tool_names=[],
            driver_fingerprint="disposable-external-fixture")
        self.rt.team.external_runs.finish(external, phase="prompt", prompt_attempted=True,
                                          cleanup_complete=False, result="failed")
        self.end(run)
        self.assertIn(external.seq, {r["started_seq"] for r in self.rt.team.external_runs.pending(self.task)})
        manager = self.rt.team._role(self.f.manager)
        source = self.rt.team.receive(self.task, "Check delivery without replaying unknown effects")
        manager_run = self.start(source)
        arguments = {"summary": "Attempted delivery", "verification": "Disposable fixture only"}
        call = ToolCall("finish-with-unknown-external", "finish_work", arguments)
        with self.assertRaises(WorkConflict):
            self.rt.team._dispatch_tool(self.task, source, manager, manager_run,
                manager_run.payload["revision"], call, self.rt.team._schemas(manager, None),
                None, self.rt.workspaces.view(self.task), {}, None)
        # Also exercise the delivery guard itself, which must remain conservative
        # even when its caller already has a durable tool start.
        tool_start = self.rt.log.emit(type="team.tool_started", actor=HOST, task_id=self.task,
            payload={**manager_run.payload, "name": "finish_work", "call_id": call.id,
                     "arguments": arguments})
        result = self.rt.team._finish_work(self.task, manager, arguments, tool_start)
        self.assertEqual((result["status"], result.get("code")), ("error", "operation_pending"), result)
        self.assertIn(external.seq, result["started_seqs"])
        self.assertEqual(self.f.events("team.objective_completed"), [])


if __name__ == "__main__":
    unittest.main()
