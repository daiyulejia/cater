"""Explicit returns consume only fully presented child feedback.

Offline native/external drivers exercise real dispatch, receipts and queue
claims. Seeded delegation edges isolate the queued-input race; these fixtures
are not a replay of production events or real-model acceptance.
"""

from dataclasses import replace
import unittest
from unittest.mock import patch

from cater.events import Actor
from cater.team import HOST, _reported_messages
from cater.team_feedback import consumed_feedback
import test_return_work as returns


WAIT = ("wait_for_reply", {})
FINISH = ("finish_work", {"summary": "Offline fixture delivered.",
                          "verification": "Scripted ledger assertions only; no real model."})


class _ReturnHarness(returns.ReturnWorkFixtures):
    """Compose fixture helpers without importing/inheriting their test cases."""

    def __init__(self, case):
        self.case = case

    def __getattr__(self, name):
        return getattr(self.case, name)


class ConsumedFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.h = _ReturnHarness(self)

    def send(self, f, sender, recipient, text, kind, **extra):
        return f.rt.team._send(f.task, f.rt.team._role(sender), recipient, text, kind, **extra)

    def publish(self, f, *, author=None, content="First version\n"):
        result, = self.h.run_calls(f, self.h.human(f, author or f.worker), ("publish_artifact", {
            "filename": "feedback-result.txt", "content": content,
            "summary": "Offline publication", "format": "text",
        }))
        self.assertEqual(result["status"], "ok", result)
        return result["artifact_seq"]

    def arrange(self, mode="native", *, text="Complete child evidence.", feedback=True, published=False):
        f = self.h.fixture(mode)
        if published:
            self.publish(f)
        parent = self.h.assign(f)
        # Seed a previously issued child edge, keeping the parent's original
        # input unclaimed. Child return and parent execution use real dispatch.
        child = self.send(f, f.worker, f.reviewer, "Inspect the bounded child work.",
                          "assignment", parent_assignment_seq=parent.seq)
        report = None
        if feedback:
            result, = self.h.run_calls(f, child, ("return_work", {"text": text}))
            self.assertEqual(result["status"], "ok", result)
            report = self.h.message(f, result["message_seq"])
        return f, parent, child, report

    def claim(self, f, source):
        claimed = f.rt.team._claim(f.task)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed[0].seq, source.seq)
        return claimed

    def drive(self, f, claimed, *calls, inspect=None, text="", statuses=("completed", "waiting")):
        message, role, started = claimed
        if hasattr(f.driver, "agent_run"):
            def script(run):
                if inspect:
                    inspect(run.context)
                for name, args in calls:
                    run.call(name, **args)
                if calls:
                    run.call("wait_for_reply")
                return text
            f.driver.scripts[role.id] = [script]
        else:
            def script(context, history, tools):
                if inspect:
                    inspect(context)
                return f.native_turn(text, *calls)
            f.driver.steps[role.id] = [script, f.native_turn("", WAIT)]
        list(f.rt.team._run_message(f.task, message, role, started))
        finished, = [e for e in f.events("team.run_finished")
                     if e.payload["run_id"] == started.payload["run_id"]]
        self.assertIn(finished.payload["status"], statuses, finished.payload)
        self.assertEqual(getattr(f, "protocol_errors", []), [])
        self.assertEqual(getattr(f.driver, "errors", []), [])
        self.assertTrue(f.rt.verify_chain().ok)
        return [e for e in f.events("team.tool_finished")
                if e.payload["run_id"] == started.payload["run_id"]]

    def receipt(self, receipts, name):
        receipt, = [e for e in receipts if e.payload["name"] == name]
        return receipt

    def assert_present(self, context, report):
        row, = [r for r in context["conversation"] if r["seq"] == report.seq]
        self.assertEqual(row["text"], report.payload["text"])
        self.assertEqual((row["sender"], row["recipient"], row["kind"]),
                         (report.actor.id, report.payload["recipient"], "report"))
        self.assertFalse(row.get("text_truncated"))

    def assert_consumed(self, f, receipt, report):
        self.assertEqual(receipt.actor, HOST)
        self.assertEqual(receipt.payload["result"]["status"], "ok")
        self.assertEqual(receipt.payload.get("consumed_input_seqs"), [report.seq])
        self.assertNotIn("consumed_input_seqs", receipt.payload["result"])
        self.assertIn(report.seq, f.rt.team._inactive_messages(f.task))
        self.assertEqual(self.h.message(f, report.seq), report, "Original feedback must stay immutable/readable")

    def assert_unconsumed(self, f, receipts, report):
        for receipt in receipts:
            self.assertNotIn("consumed_input_seqs", receipt.payload, receipt.payload)
        self.assertNotIn(report.seq, f.rt.team._inactive_messages(f.task))
        self.assertEqual(self.h.message(f, report.seq), report)

    def test_new_explicit_return_consumes_visible_feedback_and_claims_only_upstream(self):
        for mode in ("native", "external"):
            for tool in ("return_work", "send_message"):
                with self.subTest(mode=mode, tool=tool):
                    f, parent, _, report = self.arrange(mode)
                    call = returns.RETURN if tool == "return_work" else (tool, {
                        "recipient": f.manager, "reply_to": parent.seq, "text": returns.TEXT})
                    receipts = self.drive(f, self.claim(f, parent), call,
                        inspect=lambda context: self.assert_present(context, report))
                    receipt = self.receipt(receipts, tool)
                    self.assert_consumed(f, receipt, report)
                    upstream = self.h.message(f, receipt.payload["result"]["message_seq"])
                    self.assertNotIn(upstream.seq, f.rt.team._inactive_messages(f.task))
                    self.assertEqual(f.rt.team.view(f.task)["pending_count"], 1)
                    self.drive(f, self.claim(f, upstream), WAIT)
                    self.assertEqual(f.rt.team.view(f.task)["pending_count"], 0)
                    self.assertIsNone(f.rt.team._claim(f.task))

    def test_truncated_or_omitted_feedback_is_not_consumed_even_before_context_cut(self):
        for mode in ("native", "external"):
            for shape in ("truncated", "omitted"):
                with self.subTest(mode=mode, shape=shape):
                    f, parent, _, report = self.arrange(mode, text="Evidence " * 700 if shape == "truncated" else "Evidence")
                    if shape == "omitted":
                        for i in range(6):
                            self.send(f, f.reviewer, f.manager, f"History {i}: " + "x" * 3500, "notification")
                    def inspect(context):
                        rows = [r for r in context["conversation"] if r["seq"] == report.seq]
                        if shape == "omitted":
                            self.assertEqual(rows, [])
                        else:
                            self.assertEqual(len(rows), 1)
                            self.assertTrue(rows[0]["text_truncated"])
                            self.assertNotEqual(rows[0]["text"], report.payload["text"])
                    receipts = self.drive(f, self.claim(f, parent), returns.RETURN, inspect=inspect)
                    self.assert_unconsumed(f, receipts, report)
                    self.assertGreaterEqual(f.events("team.model_called")[-1].payload["context_basis"]["as_of_seq"], report.seq)
                    self.assertEqual(f.rt.team.view(f.task)["pending_count"], 2)
                    self.claim(f, report)

    def test_feedback_arriving_after_capture_and_new_customer_input_stay_claimable(self):
        for mode in ("native", "external"):
            with self.subTest(mode=mode):
                f, parent, child, _ = self.arrange(mode, feedback=False)
                late = []
                def arrive(context):
                    late.append(self.send(f, f.reviewer, f.worker, "Concurrent child completion.",
                                          "report", reply_to=child.seq))
                    self.assertNotIn(late[0].seq, {r["seq"] for r in context["conversation"]})
                receipts = self.drive(f, self.claim(f, parent), returns.RETURN, inspect=arrive)
                self.assert_unconsumed(f, receipts, late[0])
                customer = self.h.human(f, f.worker)
                self.assertEqual(f.rt.team.view(f.task)["pending_count"], 3)
                self.drive(f, self.claim(f, customer), WAIT)
                self.assertEqual(f.rt.team.view(f.task)["pending_count"], 2)
                self.claim(f, late[0])

    def test_external_later_full_read_does_not_expand_initial_context_confirmation(self):
        f, parent, child, _ = self.arrange("external", feedback=False)
        late = []
        def arrive(context):
            late.append(self.send(f, f.reviewer, f.worker, "Full text read after startup.", "report", reply_to=child.seq))
            self.assertNotIn(late[0].seq, {r["seq"] for r in context["conversation"]})
        receipts = self.drive(f, self.claim(f, parent),
            ("read_messages", {"after_seq": child.seq}), returns.RETURN, inspect=arrive)
        read = self.receipt(receipts, "read_messages")
        self.assertEqual(read.payload["result"]["messages"][0]["text"], late[0].payload["text"])
        self.assert_unconsumed(f, receipts, late[0])
        self.claim(f, late[0])

    def test_native_next_model_turn_can_consume_feedback_seen_during_original_run(self):
        f = self.h.fixture()
        parent = self.h.assign(f)
        claimed = self.claim(f, parent)
        observed = []
        def return_after_child(context, history, tools):
            self.assertEqual(context["message"]["seq"], parent.seq)
            self.assertEqual(context["active_assignment"]["seq"], parent.seq)
            self.assert_present(context, observed[0])
            return f.native_turn("", returns.RETURN)
        f.driver.steps[f.worker] = [f.native_turn("", ("delegate", {
            "recipient": f.reviewer, "instruction": "Return child evidence."})), return_after_child]
        owner = f.rt.team._run_message(f.task, *claimed)
        self.addCleanup(owner.close)
        next(owner)
        child, = [e for e in f.rt.team._messages(f.task)
                  if e.payload.get("kind") == "assignment" and e.payload.get("parent_assignment_seq") == parent.seq]
        child_receipts = self.drive(f, self.claim(f, child), returns.RETURN)
        observed.append(self.h.message(f, self.receipt(child_receipts, "return_work").payload["result"]["message_seq"]))
        list(owner)
        receipt, = [e for e in f.events("team.tool_finished")
                    if e.payload["name"] == "return_work" and e.payload["role"] == f.worker]
        self.assert_consumed(f, receipt, observed[0])
        self.assertEqual(f.rt.team.view(f.task)["pending_count"], 1)
        self.assertEqual(f.protocol_errors, [])
        self.assertTrue(f.rt.verify_chain().ok)

    def test_legacy_reply_uses_actual_returned_parent_not_captured_assignment(self):
        for mode in ("native", "external"):
            with self.subTest(mode=mode):
                f, parent, _, report = self.arrange(mode)
                other = self.send(f, f.manager, f.worker, "Independent second parent.", "assignment")
                child = self.send(f, f.worker, f.reviewer, "Second parent's child.", "assignment", parent_assignment_seq=other.seq)
                other_report = self.send(f, f.reviewer, f.worker, "Second parent's evidence.", "report", reply_to=child.seq)
                def inspect(context):
                    self.assertEqual(context["active_assignment"]["seq"], parent.seq)
                    self.assert_present(context, report)
                    self.assert_present(context, other_report)
                receipts = self.drive(f, self.claim(f, parent), ("send_message", {
                    "recipient": f.manager, "reply_to": other.seq, "text": returns.TEXT}), WAIT, inspect=inspect)
                receipt = self.receipt(receipts, "send_message")
                self.assert_consumed(f, receipt, other_report)
                self.assertNotIn(report.seq, f.rt.team._inactive_messages(f.task))
                returned = self.h.message(f, receipt.payload["result"]["message_seq"])
                self.assertEqual(returned.payload["reply_to"], other.seq)
                self.assertEqual(returned.payload["parent_assignment_seq"], parent.seq)
                self.assertIn(parent.seq, {r["seq"] for r in f.rt.team.recovery.assignments(f.task)})

    def test_previously_claimed_feedback_is_not_confirmed_by_another_return(self):
        f, parent, _, report = self.arrange()
        self.h.run_calls(f, report, WAIT)
        receipts = self.drive(f, self.claim(f, parent), returns.RETURN,
                              inspect=lambda context: self.assert_present(context, report))
        self.assert_unconsumed(f, receipts, report)

    def test_error_notification_and_implicit_text_do_not_confirm_feedback(self):
        for mode in ("native", "external"):
            for action in ("error", "notify", "implicit"):
                with self.subTest(mode=mode, action=action):
                    f, parent, _, report = self.arrange(mode)
                    calls = () if action == "implicit" else (("send_message", {
                        "recipient": f.worker, "reply_to": parent.seq, "text": "Invalid direction."}), WAIT) if action == "error" else (
                        ("send_message", {"recipient": f.manager, "text": "Progress only.", "notify_only": True}), WAIT)
                    receipts = self.drive(f, self.claim(f, parent), *calls,
                        text=returns.TEXT if action == "implicit" else "",
                        inspect=lambda context: self.assert_present(context, report),
                        statuses=("interrupted",) if mode == "external" and action == "implicit"
                        else ("completed", "waiting"))
                    self.assert_unconsumed(f, receipts, report)
                    if action == "error":
                        self.assertEqual(self.receipt(receipts, "send_message").payload["result"]["status"], "error")
                    elif action == "implicit":
                        # External end_turn deliberately leaves delegated work
                        # open; native legacy plain text emits an implicit return.
                        self.assertEqual(len(self.h.reports(f, parent)), int(mode == "native"))

    def test_visible_feedback_with_wrong_direction_or_revision_is_not_consumed(self):
        f, parent, child, report = self.arrange()
        wrong_sender = self.send(f, f.manager, f.worker, "Not the child's assignee.", "report", reply_to=child.seq)
        wrong_recipient = self.send(f, f.reviewer, f.manager, "Not addressed to the owner.", "report", reply_to=child.seq)
        # Deliberately historical/malformed ledger rows, not claimed to be
        # output of the current dispatcher.
        old_revision = f.rt.log.emit(type="team.message_received", task_id=f.task, actor=report.actor,
            payload={**report.payload, "text": "Different objective revision.",
                     "revision": parent.payload["revision"] + 1})
        invalid = (wrong_sender, wrong_recipient, old_revision)
        def inspect(context):
            for candidate in (report, *invalid):
                self.assert_present(context, candidate)
        receipts = self.drive(f, self.claim(f, parent), returns.RETURN, inspect=inspect)
        self.assert_consumed(f, self.receipt(receipts, "return_work"), report)
        self.assertTrue({e.seq for e in invalid}.isdisjoint(f.rt.team._inactive_messages(f.task)))

    def test_duplicate_return_cannot_confirm_newly_arrived_feedback(self):
        for mode in ("native", "external"):
            with self.subTest(mode=mode):
                f, parent, child, _ = self.arrange(mode, feedback=False)
                first = self.drive(f, self.claim(f, parent), returns.RETURN)
                self.assertNotIn("consumed_input_seqs", self.receipt(first, "return_work").payload)
                late = self.send(f, f.reviewer, f.worker, "Late evidence after return.", "report", reply_to=child.seq)
                source = self.h.human(f, f.worker)
                receipts = self.drive(f, self.claim(f, source), returns.RETURN, ("send_message", {
                    "recipient": f.manager, "reply_to": parent.seq, "text": returns.TEXT}), WAIT,
                    inspect=lambda context: self.assert_present(context, late))
                self.assertEqual(self.receipt(receipts, "return_work").payload["result"]["code"], "no_current_assignment")
                self.assertTrue(self.receipt(receipts, "send_message").payload["result"]["already_reported"])
                self.assert_unconsumed(f, receipts, late)
                self.assertEqual(len(self.h.reports(f, parent)), 1)

    def test_receipt_failure_rolls_back_report_and_confirmation_before_retry(self):
        for mode in ("native", "external"):
            with self.subTest(mode=mode):
                f, parent, _, report = self.arrange(mode)
                emit, failed = f.rt.log.emit, []
                def fail_first_receipt(**kwargs):
                    p = kwargs.get("payload", {})
                    if kwargs["type"] == "team.tool_finished" and p.get("name") == "return_work" and p.get("role") == f.worker:
                        if p["result"]["status"] == "ok" and not failed:
                            self.assertTrue(f.rt.log.db.in_transaction)
                            self.assertEqual(p.get("consumed_input_seqs"), [report.seq])
                            failed.append(emit(**kwargs))
                            raise OSError("Injected failure after writing the return receipt")
                        if p["result"]["status"] == "error":
                            self.assertEqual(self.h.reports(f, parent), [])
                            self.assertNotIn("consumed_input_seqs", p)
                            self.assertNotIn(report.seq, f.rt.team._inactive_messages(f.task))
                    return emit(**kwargs)
                with patch.object(f.rt.log, "emit", side_effect=fail_first_receipt):
                    receipts = self.drive(f, self.claim(f, parent), returns.RETURN, returns.RETURN)
                returned = [e for e in receipts if e.payload["name"] == "return_work"]
                self.assertEqual([e.payload["result"]["status"] for e in returned], ["error", "ok"])
                self.assertEqual(len(failed), 1)
                self.assert_consumed(f, returned[-1], report)
                self.assertEqual(len(self.h.reports(f, parent)), 1)
                self.assertEqual(f.rt.team._pending_operations(f.task), [])
                self.assertEqual(f.rt.team.view(f.task)["pending_count"], 1)

    def test_consumed_author_feedback_does_not_leave_a_phantom_delivery_blocker(self):
        for mode in ("native", "external"):
            with self.subTest(mode=mode):
                f, parent, _, report = self.arrange(mode, published=True)
                receipts = self.drive(f, self.claim(f, parent), returns.RETURN)
                receipt = self.receipt(receipts, "return_work")
                self.assert_consumed(f, receipt, report)
                upstream = self.h.message(f, receipt.payload["result"]["message_seq"])
                delivered = self.receipt(self.drive(f, self.claim(f, upstream), FINISH), "finish_work")
                self.assertEqual(delivered.payload["result"]["status"], "ok", delivered.payload)
                self.assertEqual(f.rt.team.view(f.task)["status"], "done")

    def test_consuming_child_report_keeps_its_artifact_author_assignment_returned(self):
        for mode in ("native", "external"):
            with self.subTest(mode=mode):
                f = self.h.fixture(mode)
                artifact = self.publish(f, author=f.reviewer)
                checker = next(r.id for r in f.rt.worker_roles if r.id not in {f.worker, f.reviewer})
                requested, = self.h.run_calls(f, self.h.human(f, f.reviewer), ("request_review", {
                    "reviewer": checker, "artifact_seqs": [artifact],
                    "instruction": "Approve the child's independently published result."}))
                self.assertEqual(requested["status"], "ok", requested)
                review_input = self.h.message(f, requested["message_seq"])
                recorded, = self.h.run_calls(f, review_input, ("record_review", {
                    "review_seq": requested["review_seq"], "verdict": "approved", "feedback": "Exact artifact checked."}))
                self.assertEqual(recorded["status"], "ok", recorded)
                self.h.run_calls(f, self.h.message(f, recorded["message_seq"]), WAIT)
                parent = self.h.assign(f)
                child = self.send(f, f.worker, f.reviewer, "Return your approved artifact.",
                                  "assignment", parent_assignment_seq=parent.seq)
                returned, = self.h.run_calls(f, child, returns.RETURN)
                report = self.h.message(f, returned["message_seq"])
                receipt = self.receipt(self.drive(f, self.claim(f, parent), returns.RETURN), "return_work")
                self.assert_consumed(f, receipt, report)
                self.assertIn(child.seq, _reported_messages(f.rt.team._messages(f.task)))
                self.assertNotIn(child.seq, {r["seq"] for r in f.rt.team.recovery.assignments(f.task)})
                self.assertEqual(f.rt.team_reviews.view(f.task)[0]["author"], f.reviewer)
                self.assertEqual(f.rt.team_reviews.view(f.task)[0]["status"], "approved")
                upstream = self.h.message(f, receipt.payload["result"]["message_seq"])
                finished = self.receipt(self.drive(f, self.claim(f, upstream), FINISH), "finish_work")
                self.assertEqual(finished.payload["result"]["status"], "ok", finished.payload)
                self.assertEqual(f.rt.team.view(f.task)["status"], "done")

    def review_fixture(self, mode, verdict):
        f = self.h.fixture(mode)
        artifact = self.publish(f)
        parent = self.h.assign(f)
        requested, = self.h.run_calls(f, parent, ("request_review", {
            "reviewer": f.reviewer, "artifact_seqs": [artifact], "instruction": "Inspect this exact version."}))
        self.assertEqual(requested["status"], "ok", requested)
        child = self.h.message(f, requested["message_seq"])
        continued, = self.h.run_calls(f, self.h.human(f), ("continue_work", {
            "assignment_seq": parent.seq, "instruction": "Integrate the review and return the actual result."}))
        self.assertEqual(continued["status"], "ok", continued)
        source = self.h.message(f, continued["message_seq"])
        recorded, = self.h.run_calls(f, child, ("record_review", {
            "review_seq": requested["review_seq"], "verdict": verdict, "feedback": "Exact-version fixture decision."}))
        self.assertEqual(recorded["status"], "ok", recorded)
        return f, parent, source, self.h.message(f, recorded["message_seq"])

    def test_review_feedback_consumption_does_not_approve_changes_requested(self):
        for mode in ("native", "external"):
            with self.subTest(mode=mode):
                f, _, source, report = self.review_fixture(mode, "changes_requested")
                receipts = self.drive(f, self.claim(f, source), returns.RETURN)
                receipt = self.receipt(receipts, "return_work")
                self.assert_consumed(f, receipt, report)
                self.assertEqual(f.rt.team_reviews.view(f.task)[0]["status"], "changes_requested")
                upstream = self.h.message(f, receipt.payload["result"]["message_seq"])
                blocked = self.receipt(self.drive(f, self.claim(f, upstream), FINISH), "finish_work")
                self.assertEqual(blocked.payload["result"]["code"], "review_pending")
                self.assertEqual(f.events("team.objective_completed"), [])

    def test_outdated_review_is_not_consumed_but_later_review_changes_cannot_revive_a_confirmation(self):
        for change_before_return in (True, False):
            with self.subTest(change_before_return=change_before_return):
                f, _, source, report = self.review_fixture("native", "approved")
                if change_before_return:
                    self.publish(f, author=f.reviewer, content="Replaced version\n")
                receipts = self.drive(f, self.claim(f, source), returns.RETURN)
                if change_before_return:
                    self.assert_unconsumed(f, receipts, report)
                    self.assertEqual(f.rt.team_reviews.view(f.task)[0]["status"], "outdated")
                    self.claim(f, report)
                else:
                    receipt = self.receipt(receipts, "return_work")
                    self.assert_consumed(f, receipt, report)
                    self.publish(f, author=f.reviewer, content="Replaced after confirmation\n")
                    self.assertEqual(f.rt.team_reviews.view(f.task)[0]["status"], "outdated")
                    self.assert_consumed(f, receipt, report)
                    self.h.run_calls(f, self.h.human(f), ("set_objective", {"goal": "A genuinely separate fixture objective"}))
                    self.assertEqual(f.rt.team_reviews.view(f.task)[0]["status"], "stale")
                    self.assert_consumed(f, receipt, report)

    def test_projection_requires_host_receipt_and_matching_run_tool_report(self):
        f, parent, _, report = self.arrange()
        receipt = self.receipt(self.drive(f, self.claim(f, parent), returns.RETURN), "return_work")
        self.assert_consumed(f, receipt, report)
        events = f.rt.log.read(task_id=f.task)
        self.assertEqual(consumed_feedback(events), {report.seq})
        variants = {
            "agent_authored": replace(receipt, actor=Actor.agent(f.worker, f.rt.team._role(f.worker).role)),
            "wrong_run": replace(receipt, payload={**receipt.payload, "run_id": "another-run"}),
            "old_report": replace(receipt, payload={**receipt.payload, "result": {"status": "ok", "message_seq": report.seq}}),
            "legacy_receipt": replace(receipt, payload={k: v for k, v in receipt.payload.items() if k != "consumed_input_seqs"}),
        }
        for kind, replacement in variants.items():
            with self.subTest(kind=kind):
                self.assertEqual(consumed_feedback([replacement if e.seq == receipt.seq else e for e in events]), set())


if __name__ == "__main__":
    unittest.main()
