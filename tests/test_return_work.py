"""Deterministic native/ACP host regressions, not real-model acceptance.

Reuse the existing scripted drivers, real event ledger and disposable workspace.
Only the selected input is run so unrelated queued work remains observable.
"""

import json
import unittest
from unittest.mock import patch

from cater.team import HOST
from cater.work import WorkConflict
import test_team_external as external
import test_team_review_cycle as reviews
import test_team_supervision as supervision


TEXT = "Checked result and evidence returned; no overall delivery claimed."
RETURN = ("return_work", {"text": TEXT})


class ReturnWorkFixtures:
    def fixture(self, mode="native"):
        f = external.TeamExternalTests() if mode == "external" else reviews.TeamReviewCycleTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        return f

    def message(self, f, seq):
        return next(e for e in f.rt.team._messages(f.task) if e.seq == seq)

    def reports(self, f, assignment):
        return [e for e in f.rt.team._messages(f.task)
                if e.payload["kind"] == "report" and e.payload.get("reply_to") == assignment.seq]

    def human(self, f, role=None):
        role = f.rt.team._role(role or f.manager)
        return f.rt.team.receive(f.task, f"@{role.name} Inspect the current fixture input.")

    def start(self, f, source):
        return f.rt.log.emit(type="team.run_started", actor=HOST, task_id=f.task, payload={
            "run_id": f"return-work-{source.seq}-{len(f.events('team.run_started'))}",
            "role": source.payload["recipient"], "message_seq": source.seq,
            "revision": f.rt.team.view(f.task)["revision"],
            "control_seq": f.rt.execution_control.view(f.task)["seq"],
        })

    def run_calls(self, f, source, *calls, inspect=None, followup=None, statuses=("completed", "waiting")):
        role = f.rt.team._role(source.payload["recipient"])
        started = self.start(f, source)
        if hasattr(f.driver, "agent_run"):
            def script(run):
                if inspect:
                    inspect(run.context, run.tools)
                results = []
                for name, arguments in calls:
                    results.append(run.call(name, **arguments))
                if followup:
                    for name, arguments in followup(results):
                        run.call(name, **arguments)
                run.call("wait_for_reply")
            f.driver.scripts[role.id] = [script]
        else:
            def script(context, history, tools):
                if inspect:
                    inspect(context, tools)
                return f.native_turn("", *calls)
            def next_turn(context, history, tools):
                results = [json.loads(message["content"]) for message in history if message.get("role") == "tool"]
                return f.native_turn("", *followup(results))
            f.driver.steps[role.id] = [script, *([next_turn] if followup else []),
                                      f.native_turn("", ("wait_for_reply", {}))]
        list(f.rt.team._run_message(f.task, source, role, started))
        finished, = [e for e in f.events("team.run_finished")
                     if e.payload["run_id"] == started.payload["run_id"]]
        self.assertIn(finished.payload["status"], statuses, finished.payload)
        self.assertEqual(getattr(f, "protocol_errors", []), [])
        self.assertEqual(getattr(f.driver, "errors", []), [])
        self.assertTrue(f.rt.verify_chain().ok)
        return [e.payload["result"] for e in f.events("team.tool_finished")
                if e.payload["run_id"] == started.payload["run_id"]
                and e.payload["name"] != "wait_for_reply"]

    def assign(self, f, *, sender=None, recipient=None, source=None):
        result, = self.run_calls(f, source or self.human(f, sender), ("delegate", {
            "recipient": recipient or f.worker, "instruction": "Inspect and return this exact work.",
        }))
        self.assertEqual(result["status"], "ok", result)
        return self.message(f, result["message_seq"])

    def continuation(self, f, original):
        with patch.object(f.rt.team, "_current", side_effect=WorkConflict("Fixture interruption")):
            self.run_calls(f, original, statuses=("interrupted",))
        result, = self.run_calls(f, self.human(f, original.actor.id), ("send_message", {
            "recipient": original.payload["recipient"], "text": "Resume the same interrupted work.",
            "continue_assignment_seq": original.seq,
        }))
        self.assertEqual(result["status"], "ok", result)
        return self.message(f, result["message_seq"])

    def write(self, path="after-return.txt"):
        return ("workspace_write", {"path": path, "content": "Fixture mutation", "expected_sha256": None})

    def assert_return(self, f, assignment, result):
        self.assertEqual(result["status"], "ok", result)
        report, = self.reports(f, assignment)
        self.assertEqual(report.seq, result["message_seq"])
        self.assertEqual(report.actor.id, assignment.payload["recipient"])
        self.assertEqual(report.payload["recipient"], assignment.actor.id)
        self.assertEqual(report.payload["text"], TEXT)
        self.assertIs(report.payload["visible"], True)
        self.assertNotIn(assignment.seq, {r["seq"] for r in f.rt.team.recovery.assignments(f.task)})
        self.assertEqual(f.events("team.objective_completed"), [])
        self.assertEqual(f.rt.team.view(f.task)["status"], "running")
        return report


class ReturnWorkTests(ReturnWorkFixtures, unittest.TestCase):
    def test_assignment_return_stops_batch_and_next_turn_without_closing_other_work(self):
        for mode in ("native", "external"):
            for name in ("return_work", "send_message"):
                with self.subTest(mode=mode, tool=name):
                    f = self.fixture(mode)
                    root = supervision.TeamSupervisionTests.bind_workspace(f)
                    original = self.assign(f, sender=f.reviewer)
                    unrelated = self.assign(f)
                    before = len(f.driver.runs if mode == "external" else f.driver.calls)
                    call = RETURN if name == "return_work" else (name, {
                        "recipient": original.actor.id, "reply_to": original.seq, "text": TEXT,
                    })
                    result, = self.run_calls(f, original, call, self.write(), RETURN,
                                            statuses=("completed",))
                    self.assert_return(f, original, result)
                    self.assertIn(unrelated.seq, {r["seq"] for r in f.rt.team.recovery.assignments(f.task)})
                    self.assertFalse((root / "after-return.txt").exists())
                    self.assertEqual(f.results("workspace_write"), [])
                    self.assertEqual(len(f.driver.runs if mode == "external" else f.driver.calls), before + 1)
                    if mode == "external":
                        run = f.driver.runs[-1]
                        self.assertTrue(run.revoked)
                        self.assertEqual(run.state.phase, "yielded")
                        self.assertTrue(run.cleanup_complete)
                        f.assert_receipt_chain()
                        self.assertEqual(f.rt.team.external_runs.pending(f.task), [])

    def test_continuation_returns_original_and_finishes_its_own_run(self):
        for mode in ("native", "external"):
            with self.subTest(mode=mode):
                f = self.fixture(mode)
                original = self.assign(f)
                continued = self.continuation(f, original)
                def inspect(context, tools):
                    self.assertEqual(context["message"]["seq"], continued.seq)
                    self.assertEqual(context["active_assignment"]["seq"], original.seq)
                result, = self.run_calls(f, continued, RETURN, RETURN, inspect=inspect,
                                        statuses=("completed",))
                self.assert_return(f, original, result)
                self.assertEqual(self.reports(f, continued), [])
                self.assertEqual(f.rt.team._pending_operations(f.task), [])

    def test_child_feedback_returns_parent_not_child_or_latest_unrelated_assignment(self):
        for mode in ("native", "external"):
            with self.subTest(mode=mode):
                f = self.fixture(mode)
                parent = self.assign(f)
                child = self.assign(f, recipient=f.reviewer, source=parent)
                child_result, = self.run_calls(f, child, RETURN)
                feedback = self.assert_return(f, child, child_result)
                unrelated = self.assign(f, sender=f.reviewer)
                def inspect(context, tools):
                    self.assertEqual(context["message"]["seq"], feedback.seq)
                    self.assertEqual(context["active_assignment"]["seq"], parent.seq)
                result, = self.run_calls(f, feedback, RETURN, inspect=inspect,
                                        statuses=("completed",))
                self.assert_return(f, parent, result)
                self.assertEqual(self.reports(f, child), [feedback])
                self.assertIn(unrelated.seq, {r["seq"] for r in f.rt.team.recovery.assignments(f.task)})

    def test_plain_customer_input_never_guesses_an_open_assignment_and_can_continue(self):
        for mode in ("native", "external"):
            for has_open_work in (False, True):
                with self.subTest(mode=mode, has_open_work=has_open_work):
                    f = self.fixture(mode)
                    root = supervision.TeamSupervisionTests.bind_workspace(f)
                    original = self.assign(f) if has_open_work else None
                    source = self.human(f, f.worker)
                    def inspect(context, tools):
                        self.assertIsNone(context["active_assignment"])
                        if original:
                            self.assertIn(original.seq, {r["seq"] for r in context["open_assignments"]})
                    result, written = self.run_calls(f, source, RETURN, self.write(), inspect=inspect)
                    self.assertEqual((result["status"], result["code"], result["outcome"]),
                                     ("error", "no_current_assignment", "not_started"))
                    self.assertIs(result["complete"], True)
                    self.assertEqual(written["status"], "ok", written)
                    self.assertTrue((root / "after-return.txt").exists())
                    self.assertFalse([e for e in f.rt.team._messages(f.task) if e.payload["kind"] == "report"])
                    if original:
                        self.assertIn(original.seq, {r["seq"] for r in f.rt.team.recovery.assignments(f.task)})
                    self.assertEqual(f.rt.team._pending_operations(f.task), [])

    def test_missing_text_and_extra_routing_arguments_are_correctable(self):
        for mode in ("native", "external"):
            with self.subTest(mode=mode):
                f = self.fixture(mode)
                root = supervision.TeamSupervisionTests.bind_workspace(f)
                original = self.assign(f)
                invalid = [{}, {"text": TEXT, "recipient": f.manager},
                           {"text": TEXT, "reply_to": original.seq}]
                results = self.run_calls(f, original,
                    *[("return_work", args) for args in invalid], self.write("corrected.txt"), RETURN,
                    statuses=("completed",))
                self.assertEqual(len(results), len(invalid) + 2)
                for result in results[:-2]:
                    self.assertEqual((result["status"], result["code"]), ("error", "invalid_arguments"))
                    self.assertEqual(result["allowed_fields"], ["text"])
                    self.assertEqual(result["required_fields"], ["text"])
                self.assertEqual(results[-2]["status"], "ok")
                self.assertTrue((root / "corrected.txt").exists())
                self.assert_return(f, original, results[-1])
                self.assertEqual(f.rt.team._pending_operations(f.task), [])

    def test_on_demand_initial_tools_include_text_only_return_and_legacy_send(self):
        f = self.fixture()
        original = self.assign(f)
        f.driver.tool_loading = "on_demand"
        def inspect(context, tools):
            declarations = {tool["name"]: tool["input_schema"] for tool in tools}
            schema = declarations["return_work"]
            self.assertEqual(set(schema["properties"]), {"text"})
            self.assertEqual(schema["properties"]["text"]["type"], "string")
            self.assertEqual(schema["required"], ["text"])
            self.assertIs(schema["additionalProperties"], False)
            self.assertIn("send_message", declarations)
        result, = self.run_calls(f, original, RETURN, inspect=inspect, statuses=("completed",))
        self.assert_return(f, original, result)
        self.assertEqual(f.results("load_tools"), [])

    def test_peer_message_is_returned_to_its_actual_sender(self):
        for mode in ("native", "external"):
            with self.subTest(mode=mode):
                f = self.fixture(mode)
                sent, = self.run_calls(f, self.human(f, f.reviewer), ("send_message", {
                    "recipient": f.worker, "text": "Please answer this peer request.",
                }))
                source = self.message(f, sent["message_seq"])
                self.assertEqual(source.payload["kind"], "message")
                result, = self.run_calls(f, source, RETURN, statuses=("completed",))
                self.assert_return(f, source, result)

    def prepare_review(self, f):
        published, = self.run_calls(f, self.human(f, f.worker), ("publish_artifact", {
            "filename": "result.txt", "content": "Unreviewed source\n",
            "summary": "Fixture source", "format": "text",
        }))
        self.assertEqual(published["status"], "ok", published)
        parent = self.assign(f)
        requested, = self.run_calls(f, parent, ("request_review", {
            "reviewer": f.reviewer, "artifact_seqs": [published["artifact_seq"]],
            "instruction": "Inspect the exact published version.",
        }))
        self.assertEqual(requested["status"], "ok", requested)
        return parent, self.message(f, requested["message_seq"])

    def review_return(self, f, name, source):
        return RETURN if name == "return_work" else ("send_message", {
            "recipient": f.worker, "reply_to": source.seq, "text": TEXT,
        })

    def assert_review_required(self, f, review, result):
        self.assertEqual((result["status"], result["code"], result["outcome"]),
                         ("error", "review_decision_required", "not_started"))
        self.assertIs(result["complete"], True)
        self.assertEqual(result["review_seq"], review.payload["review_seq"])
        self.assertEqual(self.reports(f, review), [])
        self.assertEqual(f.events("team.review_recorded"), [])
        self.assertEqual(f.rt.team_reviews.view(f.task)[0]["status"], "pending")
        self.assertIn(review.seq, {r["seq"] for r in f.rt.team.recovery.assignments(f.task)})

    def test_review_assignment_return_cannot_replace_record_review_or_allow_delivery(self):
        for mode in ("native", "external"):
            for name in ("return_work", "send_message"):
                with self.subTest(mode=mode, tool=name):
                    f = self.fixture(mode)
                    parent, review = self.prepare_review(f)
                    rejected, = self.run_calls(f, review, self.review_return(f, name, review), statuses=("waiting",))
                    self.assert_review_required(f, review, rejected)
                    self.assertEqual(self.reports(f, parent), [])
                    blocked, = self.run_calls(f, self.human(f), ("finish_work", {
                        "summary": "Attempt delivery", "verification": "Scripted fixture only",
                    }))
                    self.assertEqual(blocked["status"], "error", blocked)
                    self.assertEqual(f.rt.team_reviews.view(f.task)[0]["status"], "pending")
                    self.assertEqual(f.rt.team.view(f.task)["status"], "running")
                    self.assertEqual(f.events("team.objective_completed"), [])

    def test_pending_review_return_can_notify_and_record_in_same_run_including_continuation(self):
        for mode in ("native", "external"):
            for name in ("return_work", "send_message"):
                for continued in (False, True):
                    with self.subTest(mode=mode, tool=name, continuation=continued):
                        f = self.fixture(mode)
                        parent, review = self.prepare_review(f)
                        source = self.continuation(f, review) if continued else review
                        before = len(f.driver.runs if mode == "external" else f.driver.calls)
                        messages = list(f.rt.team._messages(f.task))

                        def inspect(context, tools):
                            self.assertEqual(context["message"]["seq"], source.seq)
                            self.assertEqual(context["active_assignment"]["seq"], review.seq)

                        def correct(results):
                            rejected, = results
                            self.assert_review_required(f, review, rejected)
                            self.assertEqual(f.rt.team._messages(f.task), messages)
                            self.assertEqual(self.reports(f, parent), [])
                            self.assertFalse([row for row in f.rt.team._pending_operations(f.task)
                                              if row.get("name") in {"return_work", "send_message"}])
                            if mode == "external":
                                self.assertFalse(f.driver.runs[-1].revoked)
                            return [
                                ("send_message", {"recipient": f.worker, "text": "Review still in progress.",
                                                  "notify_only": True}),
                                ("record_review", {"review_seq": review.payload["review_seq"],
                                                   "verdict": "approved", "feedback": "Scripted fixture check only."}),
                            ]

                        rejected, notified, recorded = self.run_calls(
                            f, source, self.review_return(f, name, source), inspect=inspect,
                            followup=correct, statuses=("waiting",))
                        self.assertEqual(notified["status"], "ok", notified)
                        notice = self.message(f, notified["message_seq"])
                        self.assertEqual(notice.payload["kind"], "notification")
                        self.assertEqual(notice.payload["recipient"], f.worker)
                        self.assertEqual(recorded["status"], "ok", recorded)
                        report, = self.reports(f, review)
                        self.assertEqual(report.seq, recorded["message_seq"])
                        self.assertEqual(report.payload["review_seq"], review.payload["review_seq"])
                        self.assertEqual(report.payload["recipient"], f.worker)
                        self.assertIs(report.payload["visible"], True)
                        decision, = f.events("team.review_recorded")
                        receipts = [e for e in f.events("team.tool_finished")
                                    if e.payload["run_id"] == decision.payload["run_id"]]
                        self.assertEqual([e.payload["name"] for e in receipts], [name, "send_message", "record_review"])
                        self.assertEqual(receipts[0].payload["result"], rejected)
                        self.assertLess(receipts[0].seq, notice.seq)
                        self.assertLess(notice.seq, decision.seq)
                        self.assertLess(decision.seq, report.seq)
                        self.assertEqual(len(f.driver.runs if mode == "external" else f.driver.calls),
                                         before + (1 if mode == "external" else 2))
                        self.assertEqual(f.rt.team_reviews.view(f.task)[0]["status"], "approved")
                        self.assertNotIn(review.seq, {r["seq"] for r in f.rt.team.recovery.assignments(f.task)})
                        self.assertIn(parent.seq, {r["seq"] for r in f.rt.team.recovery.assignments(f.task)})
                        self.assertEqual(f.events("team.objective_completed"), [])
                        if mode == "external":
                            self.assertTrue(f.driver.runs[-1].revoked)
                            self.assertEqual(f.driver.runs[-1].state.phase, "yielded")
                            f.assert_receipt_chain()

    def test_native_same_batch_review_handoff_stops_after_error_but_plain_wait_allows_correction(self):
        for handoff in ("record_review", "request_review", "delegate.review", "wait_for_reply"):
            with self.subTest(handoff=handoff):
                f = self.fixture()
                root = f.bind_workspace()
                if handoff == "record_review":
                    _, source = self.prepare_review(f)
                    call = ("record_review", {
                        "review_seq": source.payload["review_seq"], "verdict": "approved",
                        "feedback": "Same-batch scripted correction.",
                    })
                    error_code = "review_decision_required"
                else:
                    artifact = f.publish() if handoff != "wait_for_reply" else None
                    source = self.human(f, f.worker)
                    error_code = "no_current_assignment"
                    if handoff == "request_review":
                        call = (handoff, {"reviewer": f.reviewer, "artifact_seqs": [artifact],
                                          "instruction": "Review this exact published version."})
                    elif handoff == "delegate.review":
                        call = ("delegate", {"recipient": f.reviewer,
                                             "instruction": "Review this exact published version.",
                                             "review": {"artifact_seqs": [artifact]}})
                    else:
                        call = ("wait_for_reply", {})
                before = len(f.driver.calls)
                results = self.run_calls(
                    f, source, RETURN, call, self.write("same-batch.txt"),
                    followup=lambda results: [self.write("next-turn.txt"), ("wait_for_reply", {})],
                    statuses=("waiting",))
                self.assertEqual((results[0]["status"], results[0]["code"], results[0]["outcome"]),
                                 ("error", error_code, "not_started"))
                self.assertIs(results[0]["complete"], True)
                if handoff == "wait_for_reply":
                    self.assertEqual([result["status"] for result in results], ["error", "ok", "ok"])
                    self.assertEqual(len(f.driver.calls), before + 2)
                    self.assertEqual(len(f.results("workspace_write")), 2)
                    self.assertTrue((root / "same-batch.txt").exists())
                    self.assertTrue((root / "next-turn.txt").exists())
                else:
                    self.assertEqual(len(results), 2)
                    self.assertEqual(results[1]["status"], "ok", results[1])
                    self.assertEqual(len(f.driver.calls), before + 1)
                    self.assertEqual(f.results("workspace_write"), [])
                    self.assertFalse((root / "same-batch.txt").exists())
                    self.assertFalse((root / "next-turn.txt").exists())
                    review, = f.rt.team_reviews.view(f.task)
                    self.assertEqual(review["status"], "approved" if handoff == "record_review" else "pending")
                    if handoff == "record_review":
                        report, = self.reports(f, source)
                        self.assertEqual(report.seq, results[1]["message_seq"])
                        self.assertEqual(report.payload["review_seq"], review["request_seq"])
                self.assertEqual(f.events("team.objective_completed"), [])
                self.assertEqual(f.rt.team._pending_operations(f.task), [])

    def test_outdated_review_returns_version_error_and_cannot_approve_old_artifact(self):
        for mode in ("native", "external"):
            for name in ("return_work", "send_message"):
                with self.subTest(mode=mode, tool=name):
                    f = self.fixture(mode)
                    parent, review = self.prepare_review(f)
                    published, = self.run_calls(f, self.human(f, f.worker), ("publish_artifact", {
                        "filename": "result.txt", "content": "New bytes requiring a new submission\n",
                        "summary": "Revised fixture source", "format": "text",
                    }))
                    self.assertEqual(published["status"], "ok", published)
                    self.assertEqual(f.rt.team_reviews.view(f.task)[0]["status"], "outdated")
                    messages = list(f.rt.team._messages(f.task))

                    def correct(results):
                        rejected, = results
                        self.assertEqual((rejected["status"], rejected["code"], rejected["outcome"]),
                                         ("error", "review_version_changed", "not_started"))
                        self.assertIs(rejected["complete"], True)
                        self.assertEqual(rejected["review_seq"], review.payload["review_seq"])
                        self.assertNotIn("record_review", rejected["message"])
                        self.assertNotIn("approved", rejected["message"])
                        self.assertEqual(f.rt.team._messages(f.task), messages)
                        self.assertEqual(self.reports(f, review), [])
                        self.assertIn(review.seq, {r["seq"] for r in f.rt.team.recovery.assignments(f.task)})
                        if mode == "external":
                            self.assertFalse(f.driver.runs[-1].revoked)
                        return [("send_message", {"recipient": f.worker, "notify_only": True,
                                                  "text": "Resubmit the current artifact version for review."})]

                    rejected, notified = self.run_calls(
                        f, review, self.review_return(f, name, review), followup=correct, statuses=("waiting",))
                    self.assertEqual(notified["status"], "ok", notified)
                    self.assertEqual(self.message(f, notified["message_seq"]).payload["kind"], "notification")
                    approval, = self.run_calls(f, self.human(f, f.reviewer), ("record_review", {
                        "review_seq": review.payload["review_seq"], "verdict": "approved",
                        "feedback": "Attempt to approve the obsolete version.",
                    }))
                    self.assertEqual(approval["status"], "error", approval)
                    self.assertEqual(f.events("team.review_recorded"), [])
                    self.assertEqual(self.reports(f, review), [])
                    self.assertEqual(self.reports(f, parent), [])
                    self.assertEqual(f.rt.team_reviews.view(f.task)[0]["status"], "outdated")
                    self.assertEqual(f.events("team.objective_completed"), [])

    def test_old_goal_and_withdrawn_inputs_cannot_return_or_close_current_work(self):
        for mode in ("native", "external"):
            for invalidation in ("new_goal", "withdrawal"):
                with self.subTest(mode=mode, invalidation=invalidation):
                    f = self.fixture(mode)
                    old = self.assign(f)
                    continued = self.continuation(f, old)
                    change = (("set_objective", {"goal": "Independent replacement goal"})
                              if invalidation == "new_goal" else
                              ("withdraw_work", {"message_seq": old.seq, "reason": "No longer required."}))
                    changed, = self.run_calls(f, self.human(f), change)
                    self.assertEqual(changed["status"], "ok", changed)
                    current = self.assign(f)
                    for source in (old, continued):
                        self.assertEqual(self.run_calls(f, source, RETURN, statuses=("interrupted",)), [])
                    self.assertEqual(self.reports(f, old), [])
                    self.assertIn(current.seq, {r["seq"] for r in f.rt.team.recovery.assignments(f.task)})
                    returned, = self.run_calls(f, current, RETURN, statuses=("completed",))
                    self.assert_return(f, current, returned)


if __name__ == "__main__":
    unittest.main()
