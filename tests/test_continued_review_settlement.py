"""Offline host regressions for continued reviews, not real-model acceptance.

Compose existing disposable fixtures without inheriting their collected tests.
Tool decisions and receipts use production dispatch; only the external client and
the timing of concurrent host actions are scripted. Public text is essential:
an empty ExternalAgentState.text skips the final scope validation being tested.
"""

from dataclasses import replace
import unittest
from unittest.mock import patch

from cater.external_agent import ExternalYield
from cater.tool_protocol import ToolCall
import test_return_work as returns
import test_team_supervision as supervision


PUBLIC_TEXT = "The exact submitted version was reviewed; see the recorded decision."


class ContinuedReviewSettlementTests(returns.ReturnWorkFixtures, unittest.TestCase):
    def prepare_review(self, f, *, filename="result.txt", owner=None):
        published, = self.run_calls(f, self.human(f, f.worker), ("publish_artifact", {
            "filename": filename, "content": "Disposable review fixture\n",
            "summary": "Offline regression source", "format": "text",
        }))
        self.assertEqual(published["status"], "ok", published)
        requested, = self.run_calls(f, self.human(f, owner or f.worker), ("request_review", {
            "reviewer": f.reviewer, "artifact_seqs": [published["artifact_seq"]],
            "instruction": "Inspect this exact version and record the review decision.",
        }))
        self.assertEqual(requested["status"], "ok", requested)
        return self.message(f, requested["message_seq"])

    def decision(self, review, verdict="approved"):
        return ("record_review", {
            "review_seq": review.payload["review_seq"], "verdict": verdict,
            "feedback": "Scripted host regression; no real-model acceptance claimed.",
        })

    def run_external(self, f, source, action, *, settling=None):
        """Run only this input; inject changes after the real external settlement."""
        role = f.rt.team._role(source.payload["recipient"])
        started = self.start(f, source)
        observed, hooks = [], []

        def script(run):
            observed.append(run)
            self.assertEqual(run.context["message"]["seq"], source.seq)
            run.state = replace(run.state, text=PUBLIC_TEXT)
            action(run)
            self.fail("The scripted terminal tool should yield to the host")

        finish = f.rt.team.external_runs.finish

        def finish_then_change(attempt, **kwargs):
            result = finish(attempt, **kwargs)
            if settling and attempt.payload["run_id"] == started.payload["run_id"]:
                self.assertEqual(hooks, [], "Settlement hook must run once")
                hooks.append(attempt.seq)
                settling()
            return result

        f.driver.scripts[role.id] = [script]
        with patch.object(f.rt.team.external_runs, "finish", side_effect=finish_then_change):
            list(f.rt.team._run_message(f.task, source, role, started))
        finished, = [e for e in f.events("team.run_finished")
                     if e.payload["run_id"] == started.payload["run_id"]]
        self.assertEqual(f.driver.errors, [])
        self.assertTrue(f.rt.verify_chain().ok)
        f.assert_receipt_chain()
        self.assertEqual(f.rt.team.external_runs.pending(f.task), [])
        self.assertEqual(f.rt.team._pending_operations(f.task), [])
        if settling:
            self.assertEqual(len(hooks), 1)
        return started, finished, observed

    def run_events(self, f, kind, started):
        return [e for e in f.events(kind)
                if e.payload.get("run_id") == started.payload["run_id"]]

    def assert_decision(self, f, review, started, verdict):
        decision, = self.run_events(f, "team.review_recorded", started)
        receipt, = [e for e in self.run_events(f, "team.tool_finished", started)
                    if e.payload["name"] == "record_review"]
        result = receipt.payload["result"]
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["verdict"], verdict)
        self.assertEqual(result["decision_seq"], decision.seq)
        self.assertEqual(decision.payload["review_seq"], review.payload["review_seq"])
        self.assertEqual(decision.payload["verdict"], verdict)
        report, = self.reports(f, review)
        self.assertEqual(report.seq, result["message_seq"])
        self.assertEqual(report.actor.id, review.payload["recipient"])
        self.assertEqual(report.payload["recipient"], review.actor.id)
        self.assertEqual(report.payload["review_seq"], review.payload["review_seq"])
        self.assertIs(report.payload["visible"], True)
        self.assertLess(decision.seq, report.seq)
        self.assertLess(report.seq, receipt.seq)
        self.assertNotIn(review.seq, {r["seq"] for r in f.rt.team.recovery.assignments(f.task)})
        self.assertEqual(f.events("team.objective_completed"), [])
        return receipt

    def test_external_nonempty_text_settles_both_verdicts_and_revokes_extra_calls(self):
        for verdict in ("approved", "changes_requested"):
            with self.subTest(verdict=verdict):
                f = self.fixture("external")
                root = supervision.TeamSupervisionTests.bind_workspace(f)
                review = self.prepare_review(f)
                continued = self.continuation(f, review)
                call = self.decision(review, verdict)
                rejected = []

                def action(run):
                    self.assertEqual(run.context["active_assignment"]["seq"], review.seq)
                    try:
                        run.call(call[0], **call[1])
                    except ExternalYield:
                        self.assertTrue(run.revoked)
                        # Probe the host callback, not just the scripted client's
                        # revoked flag: queued calls must never reach dispatch.
                        for index, (name, arguments) in enumerate((self.write(), call)):
                            with self.assertRaises(ExternalYield):
                                run.execute(ToolCall(f"late-review-{index}", name, arguments))
                            rejected.append(name)
                        raise

                started, finished, observed = self.run_external(f, continued, action)
                receipt = self.assert_decision(f, review, started, verdict)
                run, = observed
                self.assertEqual(run.state.text, PUBLIC_TEXT)
                self.assertEqual(run.state.phase, "yielded")
                self.assertIs(run.state.cleanup_complete, True)
                external, = self.run_events(f, "team.external_run_finished", started)
                self.assertEqual(external.payload["outcome"], "yielded")
                self.assertIs(external.payload["cleanup_complete"], True)
                self.assertLess(receipt.seq, external.seq)
                self.assertLess(external.seq, finished.seq)
                self.assertEqual(rejected, ["workspace_write", "record_review"])
                self.assertEqual([e.payload["name"] for e in
                                  self.run_events(f, "team.tool_started", started)], ["record_review"])
                self.assertFalse((root / "after-return.txt").exists())
                self.assertEqual(self.reports(f, continued), [])
                # Before the fix, all evidence above succeeds but this is
                # interrupted: own_return does not recognize record_review.
                self.assertEqual(finished.payload["status"], "waiting", finished.payload)
                public, = self.run_events(f, "msg.said", started)
                self.assertEqual(public.payload["text"], PUBLIC_TEXT)

    def test_native_review_handoff_stops_same_batch_write_and_duplicate_decision(self):
        for verdict in ("approved", "changes_requested"):
            with self.subTest(verdict=verdict):
                f = self.fixture()
                root = f.bind_workspace()
                review = self.prepare_review(f)
                continued = self.continuation(f, review)
                call = self.decision(review, verdict)
                calls_before = len(f.driver.calls)
                recorded, = self.run_calls(f, continued, call, self.write(), call,
                                           statuses=("waiting",))
                self.assertEqual(recorded["status"], "ok", recorded)
                decision, = f.events("team.review_recorded")
                started, = [e for e in f.events("team.run_started")
                            if e.payload["run_id"] == decision.payload["run_id"]]
                self.assert_decision(f, review, started, verdict)
                self.assertEqual(len(f.driver.calls), calls_before + 1)
                self.assertEqual([e.payload["name"] for e in
                                  self.run_events(f, "team.tool_started", started)], ["record_review"])
                self.assertFalse((root / "after-return.txt").exists())

    def test_another_runs_success_cannot_reopen_a_closed_continuation(self):
        f = self.fixture("external")
        root = supervision.TeamSupervisionTests.bind_workspace(f)
        review = self.prepare_review(f)
        continued = self.continuation(f, review)
        recorded, = self.run_calls(f, review, self.decision(review), statuses=("waiting",))
        self.assertEqual(recorded["status"], "ok", recorded)
        previous_runs = len(f.driver.runs)

        def stale_action(run):
            name, args = self.write()
            run.call(name, **args)
            run.call("wait_for_reply")

        started, finished, observed = self.run_external(f, continued, stale_action)
        self.assertEqual(finished.payload["status"], "interrupted", finished.payload)
        self.assertEqual(observed, [])
        self.assertEqual(len(f.driver.runs), previous_runs)
        self.assertEqual(self.run_events(f, "team.tool_started", started), [])
        self.assertEqual(len(f.events("team.review_recorded")), 1)
        self.assertEqual(len(self.reports(f, review)), 1)
        self.assertFalse((root / "after-return.txt").exists())

    def test_same_run_success_for_other_review_direction_cannot_settle_original(self):
        f = self.fixture("external")
        review = self.prepare_review(f)
        continued = self.continuation(f, review)
        other = self.prepare_review(f, filename="other.txt", owner=f.manager)
        self.assertNotEqual(review.actor.id, other.actor.id)
        call = self.decision(other)

        def close_original_elsewhere():
            result, = self.run_calls(f, review, self.decision(review), statuses=("waiting",))
            self.assertEqual(result["status"], "ok", result)

        started, finished, observed = self.run_external(
            f, continued, lambda run: run.call(call[0], **call[1]),
            settling=close_original_elsewhere)
        self.assert_decision(f, other, started, "approved")
        original_decision, = [e for e in f.events("team.review_recorded")
                             if e.payload["review_seq"] == review.payload["review_seq"]]
        self.assertNotEqual(original_decision.payload["run_id"], started.payload["run_id"])
        self.assertEqual(observed[0].state.text, PUBLIC_TEXT)
        self.assertEqual(finished.payload["status"], "interrupted", finished.payload)
        self.assertEqual(self.run_events(f, "msg.said", started), [])
        self.assertEqual(len(self.reports(f, review)), 1)
        self.assertEqual(len(self.reports(f, other)), 1)

    def test_failed_review_receipt_cannot_borrow_another_runs_return(self):
        f = self.fixture("external")
        review = self.prepare_review(f)
        continued = self.continuation(f, review)
        emit = f.rt.log.emit
        rolled_back = []

        def fail_receipt(**kwargs):
            p = kwargs.get("payload", {})
            if (kwargs["type"] == "team.tool_finished" and p.get("name") == "record_review"
                    and p["result"]["status"] == "ok" and not rolled_back):
                self.assertTrue(f.rt.log.db.in_transaction)
                rolled_back.extend(self.reports(f, review))
                raise OSError("Offline injected review receipt failure")
            return emit(**kwargs)

        def action(run):
            call = self.decision(review)
            with patch.object(f.rt.log, "emit", side_effect=fail_receipt):
                result = run.call(call[0], **call[1])
            self.assertEqual(result["status"], "error", result)
            self.assertEqual(len(rolled_back), 1)
            self.assertEqual(f.events("team.review_recorded"), [])
            self.assertEqual(self.reports(f, review), [])
            self.assertFalse(run.revoked)
            run.call("wait_for_reply")

        def close_original_elsewhere():
            result, = self.run_calls(f, review, self.decision(review), statuses=("waiting",))
            self.assertEqual(result["status"], "ok", result)

        started, finished, observed = self.run_external(
            f, continued, action, settling=close_original_elsewhere)
        receipts = self.run_events(f, "team.tool_finished", started)
        self.assertEqual([e.payload["name"] for e in receipts], ["record_review", "wait_for_reply"])
        self.assertEqual(receipts[0].payload["result"]["status"], "error")
        self.assertEqual(self.run_events(f, "team.review_recorded", started), [])
        self.assertEqual(observed[0].state.text, PUBLIC_TEXT)
        self.assertEqual(finished.payload["status"], "interrupted", finished.payload)
        self.assertEqual(self.run_events(f, "msg.said", started), [])
        self.assertEqual(len(self.reports(f, review)), 1)

    def test_pause_or_new_customer_input_during_settlement_still_interrupts(self):
        for verdict in ("approved", "changes_requested"):
            for change in ("pause", "customer"):
                with self.subTest(verdict=verdict, change=change):
                    f = self.fixture("external")
                    review = self.prepare_review(f)
                    continued = self.continuation(f, review)
                    call = self.decision(review, verdict)
                    changes = []

                    def supersede():
                        if change == "pause":
                            f.rt.execution_control.change(f.task, "pause", request_id="review-settlement-pause")
                        else:
                            self.human(f, f.reviewer)
                        changes.append(f.rt.log.read(task_id=f.task)[-1].seq)

                    started, finished, observed = self.run_external(
                        f, continued, lambda run: run.call(call[0], **call[1]), settling=supersede)
                    receipt = self.assert_decision(f, review, started, verdict)
                    external, = self.run_events(f, "team.external_run_finished", started)
                    self.assertEqual(external.payload["outcome"], "yielded")
                    self.assertIs(external.payload["cleanup_complete"], True)
                    self.assertEqual(observed[0].state.text, PUBLIC_TEXT)
                    self.assertLess(receipt.seq, external.seq)
                    self.assertLess(external.seq, changes[0])
                    self.assertLess(changes[0], finished.seq)
                    self.assertEqual(finished.payload["status"], "interrupted", finished.payload)
                    self.assertIn("暂停" if change == "pause" else "顾客", finished.payload["reason"])
                    self.assertEqual(self.run_events(f, "msg.said", started), [])
                    self.assertEqual([e.payload["name"] for e in
                                      self.run_events(f, "team.tool_started", started)], ["record_review"])


if __name__ == "__main__":
    unittest.main()
