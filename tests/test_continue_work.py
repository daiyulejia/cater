"""Persistent continue_work regressions with real ledgers and offline drivers.

Compose the return-work helpers, never inherit their collected test classes.
Only selected inputs run, so unrelated open work remains observable.
"""

import unittest
from unittest.mock import patch

from cater.team import HOST, LOCAL_EVENT_CONTRACT, LOCAL_EVENT_TOOLS
from cater.work import WorkConflict
import test_return_work as returns


INSTRUCTION = "17 + 29; inspect the existing evidence and apply this next step."
MODES = ("native", "external")


class ContinueWorkTests(returns.ReturnWorkFixtures, unittest.TestCase):
    def run_calls(self, f, source, *calls, **kwargs):
        try:
            return super().run_calls(f, source, *calls, **kwargs)
        except AssertionError as exc:
            errors = getattr(f.driver, "errors", [])
            if errors:
                raise AssertionError(f"{exc}\nExternal script assertions: {errors!r}") from exc
            raise

    def call(self, seq, instruction=INSTRUCTION):
        return "continue_work", {"assignment_seq": seq, "instruction": instruction}

    def rows(self, f):
        return {row["seq"]: row for row in f.rt.team.recovery.assignments(f.task)}

    def continuations(self, f):
        return [e for e in f.rt.team._messages(f.task) if e.payload["kind"] == "continuation"]

    def settle(self, f, original, status="interrupted"):
        if status == "interrupted":
            with patch.object(f.rt.team, "_current", side_effect=WorkConflict("Fixture interruption")):
                self.assertEqual(self.run_calls(f, original, statuses=(status,)), [])
        else:
            self.assertEqual(self.run_calls(f, original, statuses=(status,)), [])
        root_seq = original.payload.get("continue_assignment_seq", original.seq)
        self.assertEqual(self.rows(f)[root_seq]["status"], status)
        if original.payload["kind"] == "continuation":
            self.assertNotIn(original.seq, self.rows(f))
        return original

    def ready(self, f, *, sender=None, recipient=None, status="interrupted"):
        return self.settle(f, self.assign(f, sender=sender, recipient=recipient), status)

    def assert_continued(self, f, original, result, instruction=INSTRUCTION):
        self.assertEqual(result["status"], "ok", result)
        continued = self.message(f, result["message_seq"])
        self.assertEqual(continued.actor, original.actor)
        self.assertEqual(continued.payload["kind"], "continuation")
        self.assertEqual(continued.payload["recipient"], original.payload["recipient"])
        self.assertEqual(continued.payload["continue_assignment_seq"], original.seq)
        self.assertEqual(continued.payload["revision"], original.payload["revision"])
        self.assertEqual(continued.payload["text"], instruction)
        self.assertIs(continued.payload["visible"], True)
        self.assertIsNone(continued.payload["reply_to"])
        row = self.rows(f)[original.seq]
        self.assertEqual(row["latest_message_seq"], continued.seq)
        self.assertEqual(row["status"], "queued")
        self.assertNotIn(continued.seq, self.rows(f))
        self.assertEqual(self.reports(f, original), [])
        self.assertEqual(f.events("team.objective_completed"), [])
        self.assertEqual(f.rt.team._pending_operations(f.task), [])
        self.assertTrue(f.rt.verify_chain().ok)
        return continued

    def reject_then_correct(self, f, source, invalid, valid):
        messages = list(f.rt.team._messages(f.task))
        rows = self.rows(f)

        def correct(results):
            self.assertEqual(len(results), len(invalid))
            for result in results:
                self.assertEqual(result["status"], "error", result)
                self.assertIsNot(result.get("complete"), False, result)
                self.assertNotEqual(result.get("outcome"), "effects_unknown", result)
            self.assertEqual(f.rt.team._messages(f.task), messages)
            self.assertEqual(self.rows(f), rows)
            self.assertEqual([row for row in f.rt.team._pending_operations(f.task)
                              if row["name"] != "external_agent"], [])
            if hasattr(f.driver, "agent_run"):
                self.assertFalse(f.driver.runs[-1].revoked)
            return [self.call(valid.seq)]

        results = self.run_calls(f, source, *invalid, followup=correct, statuses=("waiting",))
        self.assertEqual(len(results), len(invalid) + 1)
        return results[:-1], self.assert_continued(f, valid, results[-1])

    def test_schema_is_exact_and_available_initially_in_both_loading_modes(self):
        for mode in MODES:
            for loading in (("all", "on_demand") if mode == "native" else ("all",)):
                with self.subTest(mode=mode, loading=loading):
                    f = self.fixture(mode)
                    original = self.ready(f)
                    f.driver.tool_loading = loading

                    def inspect(context, tools):
                        declarations = {tool["name"]: tool["input_schema"] for tool in tools}
                        schema = declarations["continue_work"]
                        self.assertEqual(set(schema["properties"]), {"assignment_seq", "instruction"})
                        self.assertEqual(set(schema["required"]), {"assignment_seq", "instruction"})
                        self.assertEqual(schema["properties"]["assignment_seq"]["type"], "integer")
                        self.assertEqual(schema["properties"]["assignment_seq"]["minimum"], 1)
                        self.assertEqual(schema["properties"]["instruction"]["type"], "string")
                        self.assertIs(schema["additionalProperties"], False)
                        self.assertIn("send_message", declarations)
                        self.assertIn(original.seq, {row["seq"] for row in context["outgoing_work"]})
                        if mode == "native" and loading == "on_demand":
                            self.assertEqual(context["tool_catalog"]["mode"], "on_demand")
                            self.assertNotIn("delegate", declarations)
                        elif mode == "external":
                            self.assertNotIn("tool_catalog", context)

                    result, = self.run_calls(f, self.human(f), self.call(original.seq), inspect=inspect,
                                             statuses=("waiting",))
                    continued = self.assert_continued(f, original, result)
                    self.assertEqual(f.results("load_tools"), [])
                    returned, = self.run_calls(f, continued, returns.RETURN, statuses=("completed",))
                    self.assert_return(f, original, returned)

    def test_worker_and_manager_receive_original_assignment_or_peer_request(self):
        for mode in MODES:
            for target in ("worker", "manager"):
                for kind in ("assignment", "message"):
                    with self.subTest(mode=mode, target=target, kind=kind):
                        f = self.fixture(mode)
                        sender = f.manager if target == "worker" else f.reviewer
                        recipient = getattr(f, target)
                        name, args = (("delegate", {"recipient": recipient, "instruction": "Exact work"})
                                      if kind == "assignment" else
                                      ("send_message", {"recipient": recipient, "text": "Exact work"}))
                        sent, = self.run_calls(f, self.human(f, sender), (name, args))
                        original = self.settle(f, self.message(f, sent["message_seq"]), "interrupted")
                        unrelated = self.assign(f, sender=sender, recipient=recipient)
                        assignments = [e for e in f.rt.team._messages(f.task)
                                       if e.payload["kind"] == "assignment"]
                        continued_result, notice = self.run_calls(
                            f, self.human(f, sender), self.call(original.seq),
                            ("send_message", {"recipient": recipient, "text": "Still inspecting.",
                                              "notify_only": True}), statuses=("waiting",))
                        continued = self.assert_continued(f, original, continued_result)
                        self.assertEqual(notice["status"], "ok", notice)
                        self.assertEqual(self.message(f, notice["message_seq"]).payload["kind"], "notification")
                        self.assertEqual([e for e in f.rt.team._messages(f.task)
                                          if e.payload["kind"] == "assignment"], assignments)

                        def inspect(context, tools):
                            self.assertEqual(context["message"]["seq"], continued.seq)
                            self.assertEqual(context["active_assignment"]["seq"], original.seq)
                            self.assertEqual(context["active_assignment"]["sender"], sender)

                        returned, = self.run_calls(f, continued, returns.RETURN, inspect=inspect,
                                                    statuses=("completed",))
                        self.assert_return(f, original, returned)
                        self.assertEqual(self.reports(f, continued), [])
                        self.assertIn(unrelated.seq, self.rows(f))
                        if mode == "external":
                            f.assert_receipt_chain()
                            self.assertEqual(f.rt.team.external_runs.pending(f.task), [])

    def test_exact_role_selected_seq_wins_over_newer_work_and_instruction_words(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                f = self.fixture(mode)
                older = self.ready(f)
                newer = self.ready(f)
                other = self.assign(f, recipient=f.reviewer)
                before = self.rows(f)
                instruction = f"Task {newer.seq}, recipient {f.reviewer}, resume continue follow up."
                result, = self.run_calls(f, self.human(f), self.call(older.seq, instruction))
                continued = self.assert_continued(f, older, result, instruction)
                self.assertEqual(self.rows(f)[newer.seq], before[newer.seq])
                self.assertEqual(self.rows(f)[other.seq], before[other.seq])
                returned, = self.run_calls(f, continued, returns.RETURN, statuses=("completed",))
                self.assert_return(f, older, returned)
                self.assertEqual(set(self.rows(f)), {newer.seq, other.seq})

    def test_nested_continuation_returns_child_then_feedback_returns_parent(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                f = self.fixture(mode)
                parent = self.assign(f)
                child = self.assign(f, recipient=f.reviewer, source=parent)
                self.assertEqual(child.payload["parent_assignment_seq"], parent.seq)
                self.settle(f, child)
                unrelated = self.assign(f, sender=f.reviewer)
                other_child = self.assign(f, sender=f.worker, recipient=f.reviewer)
                result, = self.run_calls(f, self.human(f, f.worker), self.call(child.seq))
                continued = self.assert_continued(f, child, result)

                def inspect_child(context, tools):
                    self.assertEqual(context["active_assignment"]["seq"], child.seq)

                returned, = self.run_calls(f, continued, returns.RETURN, inspect=inspect_child,
                                            statuses=("completed",))
                feedback = self.assert_return(f, child, returned)

                def inspect_parent(context, tools):
                    self.assertEqual(context["message"]["seq"], feedback.seq)
                    self.assertEqual(context["active_assignment"]["seq"], parent.seq)

                returned, = self.run_calls(f, feedback, returns.RETURN, inspect=inspect_parent,
                                            statuses=("completed",))
                self.assert_return(f, parent, returned)
                self.assertEqual(set(self.rows(f)), {unrelated.seq, other_child.seq})

    def test_missing_fields_and_routing_injection_are_correctable_without_effects(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                f = self.fixture(mode)
                original = self.ready(f)
                args = self.call(original.seq)[1]
                invalid = [{}, {"assignment_seq": original.seq}, {"instruction": INSTRUCTION}]
                invalid.extend({**args, field: value} for field, value in (
                    ("recipient", f.reviewer), ("reply_to", original.seq),
                    ("continue_assignment_seq", original.seq), ("notify_only", True),
                    ("text", "Injected routing")))
                rejected, _ = self.reject_then_correct(
                    f, self.human(f), [("continue_work", value) for value in invalid], original)
                for result in rejected:
                    self.assertEqual(result["code"], "invalid_arguments", result)
                    self.assertEqual(result["allowed_fields"], ["assignment_seq", "instruction"])
                    self.assertEqual(result["required_fields"], ["assignment_seq", "instruction"])
                    self.assertEqual(result["outcome"], "not_started")
                    self.assertIs(result["complete"], True)

    def test_positive_integer_seq_and_nonempty_string_instruction_are_enforced(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                f = self.fixture(mode)
                original = self.ready(f)
                invalid = [self.call(seq) for seq in (0, -1, True, False, 1.0, str(original.seq), None, [], {})]
                invalid.extend(self.call(original.seq, text)
                               for text in (None, True, 42, [], {}, "", " \t\n", "x" * 32001))
                self.reject_then_correct(f, self.human(f), invalid, original)

    def test_finished_waiting_and_failed_runs_can_continue_without_rebuilding_work(self):
        for mode in MODES:
            for status in ("completed", "waiting", "failed"):
                with self.subTest(mode=mode, status=status):
                    f = self.fixture(mode)
                    original = self.assign(f)
                    # These are durable host run facts; no model or external process is launched.
                    started = self.start(f, original)
                    f.rt.log.emit(type="team.run_finished", actor=HOST, task_id=f.task,
                                  payload={**started.payload, "status": status})
                    self.assertEqual(self.rows(f)[original.seq]["status"], status)
                    accepted, = self.run_calls(f, self.human(f), self.call(original.seq))
                    self.assert_continued(f, original, accepted)
                    self.assertEqual(set(self.rows(f)), {original.seq})

    def test_wrong_seq_group_sender_and_nonrequest_events_cannot_continue(self):
        cases = ("missing", "receipt", "human", "notification", "continuation", "cross_group",
                 "other_sender", "recipient_as_sender", "manager_for_other_sender")
        for mode in MODES:
            for case in cases:
                with self.subTest(mode=mode, case=case):
                    f = self.fixture(mode)
                    caller = (f.worker if case == "recipient_as_sender" else
                              f.reviewer if case == "other_sender" else f.manager)
                    if case == "manager_for_other_sender":
                        original = self.ready(f, sender=f.reviewer)
                    else:
                        original = self.ready(f)
                    valid = self.ready(f, sender=caller,
                                       recipient=f.manager if caller == f.reviewer else f.reviewer)
                    source = self.human(f, caller)
                    seq = original.seq
                    if case == "missing":
                        seq = 10**9
                    elif case == "receipt":
                        seq = f.events("team.tool_finished")[-1].seq
                    elif case == "human":
                        seq = source.seq
                    elif case == "notification":
                        seq = f.rt.team._send(f.task, f.rt.team._role(caller), f.worker,
                                             "Notice only", "notification", visible=True).seq
                    elif case == "continuation":
                        seq = f.rt.team._send(f.task, f.rt.team._role(f.manager), f.worker,
                                             "Existing continuation", "continuation",
                                             continue_assignment_seq=original.seq).seq
                    elif case == "cross_group":
                        other = f.rt.create_task("Other continue-work group")
                        f.rt.team.enable(other)
                        foreign = f.rt.team._send(other, f.rt.team._role(caller), f.worker,
                                                  "Foreign original", "assignment")
                        started = f.rt.log.emit(type="team.run_started", actor=HOST, task_id=other,
                            payload={"message_seq": foreign.seq, "role": f.worker,
                                     "run_id": "foreign-wait", "revision": 0, "control_seq": None})
                        f.rt.log.emit(type="team.run_finished", actor=HOST, task_id=other,
                                      payload={**started.payload, "status": "waiting"})
                        self.assertEqual(f.rt.team.recovery.assignments(other)[0]["status"], "waiting")
                        seq = foreign.seq
                    self.reject_then_correct(f, source, [self.call(seq)], valid)
                    self.assertIn(original.seq, self.rows(f))

    def test_reported_withdrawn_and_old_goal_work_are_not_reopened(self):
        for mode in MODES:
            for case in ("reported", "withdrawn", "old_goal"):
                with self.subTest(mode=mode, case=case):
                    f = self.fixture(mode)
                    original = self.ready(f)
                    if case == "reported":
                        returned, = self.run_calls(f, original, returns.RETURN, statuses=("completed",))
                        report = self.assert_return(f, original, returned)
                    else:
                        change = (("withdraw_work", {"message_seq": original.seq, "reason": "No longer needed"})
                                  if case == "withdrawn" else
                                  ("set_objective", {"goal": "Independent new goal"}))
                        changed, = self.run_calls(f, self.human(f), change)
                        self.assertEqual(changed["status"], "ok", changed)
                    valid = self.ready(f, recipient=f.reviewer)
                    invalid = [self.call(original.seq)]
                    if case == "reported":
                        invalid.append(self.call(report.seq))
                    self.reject_then_correct(f, self.human(f), invalid, valid)
                    self.assertNotIn(original.seq, self.rows(f))
                    self.assertEqual(self.reports(f, original), [report] if case == "reported" else [])

    def test_queued_continuation_cannot_revive_withdrawn_or_old_goal_work(self):
        for mode in MODES:
            for case in ("withdrawn", "old_goal"):
                with self.subTest(mode=mode, case=case):
                    f = self.fixture(mode)
                    original = self.ready(f)
                    result, = self.run_calls(f, self.human(f), self.call(original.seq))
                    continued = self.assert_continued(f, original, result)
                    change = (("withdraw_work", {"message_seq": original.seq, "reason": "Stop this work"})
                              if case == "withdrawn" else
                              ("set_objective", {"goal": "Independent replacement goal"}))
                    changed, = self.run_calls(f, self.human(f), change)
                    self.assertEqual(changed["status"], "ok", changed)
                    valid = self.ready(f, recipient=f.reviewer)
                    self.reject_then_correct(f, self.human(f),
                                             [self.call(original.seq), self.call(continued.seq)], valid)
                    self.assertEqual(self.run_calls(f, continued, returns.RETURN,
                                                    statuses=("interrupted",)), [])
                    self.assertEqual(self.reports(f, original), [])
                    self.assertEqual(self.reports(f, continued), [])
                    self.assertEqual(set(self.rows(f)), {valid.seq})

    def test_delivery_guard_rejects_continuation_and_allows_same_turn_correction(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                f = self.fixture(mode)
                original = self.ready(f)
                returned, = self.run_calls(f, original, returns.RETURN, statuses=("completed",))
                report = self.assert_return(f, original, returned)
                finished, = self.run_calls(f, self.human(f), ("finish_work", {
                    "summary": "Fixture delivered", "verification": "Offline host regression only",
                }), statuses=("completed",))
                self.assertEqual(finished["status"], "ok", finished)
                self.assertEqual(f.rt.team.view(f.task)["status"], "done")
                deliveries = list(f.events("team.objective_completed"))

                def correct(results):
                    rejected, = results
                    self.assertEqual(rejected["status"], "error", rejected)
                    self.assertEqual(self.continuations(f), [])
                    self.assertEqual(self.reports(f, original), [report])
                    return [("set_objective", {"goal": "New work after delivery"})]

                rejected, changed = self.run_calls(f, self.human(f), self.call(original.seq),
                                                   followup=correct)
                self.assertEqual(changed["status"], "ok", changed)
                self.assertEqual(f.events("team.objective_completed"), deliveries)
                self.assertEqual(f.rt.team.view(f.task)["status"], "running")
                self.assertEqual(self.rows(f), {})
                self.assertEqual(self.continuations(f), [])

    def test_superseded_review_and_its_continuation_cannot_replace_current_review(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                f = self.fixture(mode)

                def publish(content):
                    result, = self.run_calls(f, self.human(f, f.worker), ("publish_artifact", {
                        "filename": "review.txt", "content": content,
                        "summary": "Exact fixture version", "format": "text",
                    }))
                    self.assertEqual(result["status"], "ok", result)
                    return result["artifact_seq"]

                def request(artifact, previous=None):
                    args = {"reviewer": f.reviewer, "artifact_seqs": [artifact],
                            "instruction": "Inspect these exact bytes"}
                    if previous is not None:
                        args["review_seq"] = previous
                    result, = self.run_calls(f, self.human(f, f.worker), ("request_review", args))
                    self.assertEqual(result["status"], "ok", result)
                    return result

                old = request(publish("Old bytes\n"))
                original = self.settle(f, self.message(f, old["message_seq"]))
                result, = self.run_calls(f, self.human(f, f.worker), self.call(original.seq))
                continued = self.assert_continued(f, original, result)
                latest = request(publish("New bytes\n"), old["review_seq"])
                current = self.settle(f, self.message(f, latest["message_seq"]))
                self.assertEqual(latest["root_seq"], old["root_seq"])
                _, current_continuation = self.reject_then_correct(
                    f, self.human(f, f.worker), [self.call(original.seq), self.call(continued.seq)], current)
                self.assertEqual(self.run_calls(f, continued, returns.RETURN,
                                                statuses=("interrupted",)), [])
                self.assertEqual(self.reports(f, original), [])
                self.assertEqual(set(self.rows(f)), {current.seq})

                def inspect(context, tools):
                    self.assertEqual(context["active_assignment"]["seq"], current.seq)

                decided, = self.run_calls(f, current_continuation, ("record_review", {
                    "review_seq": latest["review_seq"], "verdict": "approved",
                    "feedback": "Offline scripted version check",
                }), inspect=inspect, statuses=("waiting",))
                self.assertEqual(decided["status"], "ok", decided)
                report, = self.reports(f, current)
                self.assertEqual(report.seq, decided["message_seq"])
                self.assertEqual(report.payload["reply_to"], current.seq)
                self.assertEqual(report.payload["review_seq"], latest["review_seq"])
                self.assertEqual(self.reports(f, current_continuation), [])
                self.assertEqual(self.rows(f), {})
                self.assertEqual(f.events("team.objective_completed"), [])

    def test_queued_running_and_inflight_continuations_block_duplicate_delivery(self):
        for mode in MODES:
            for case in ("queued", "running", "continuation_queued", "continuation_running", "other_running"):
                with self.subTest(mode=mode, case=case):
                    f = self.fixture(mode)
                    original = self.assign(f)
                    active = original
                    if case != "queued":
                        self.settle(f, original)
                    if case.startswith("continuation"):
                        result, = self.run_calls(f, self.human(f), self.call(original.seq))
                        active = self.assert_continued(f, original, result)
                    if case == "other_running":
                        active = self.human(f, f.worker)
                    running = self.start(f, active) if case.endswith("running") else None
                    source = self.human(f)
                    before = list(f.rt.team._messages(f.task))
                    rejected, = self.run_calls(f, source, self.call(original.seq))
                    self.assertEqual(rejected["status"], "error", rejected)
                    self.assertEqual(f.rt.team._messages(f.task), before)
                    self.assertEqual(f.rt.team._pending_operations(f.task), [])
                    if running:
                        f.rt.log.emit(type="team.run_finished", actor=HOST, task_id=f.task,
                                      payload={**running.payload, "status": "waiting"})
                    else:
                        self.settle(f, active)
                    accepted, = self.run_calls(f, self.human(f), self.call(original.seq))
                    self.assert_continued(f, original, accepted)

    def test_message_or_receipt_failure_rolls_back_and_same_turn_retry_succeeds(self):
        for mode in MODES:
            for failure in ("message", "receipt"):
                with self.subTest(mode=mode, failure=failure):
                    f = self.fixture(mode)
                    original = self.ready(f)
                    source = self.human(f)
                    before = list(f.rt.team._messages(f.task))
                    rows = self.rows(f)
                    emit = f.rt.log.emit
                    failures = []

                    def fail_once(**kwargs):
                        payload = kwargs.get("payload", {})
                        is_message = kwargs["type"] == "team.message_received" and payload.get("kind") == "continuation"
                        is_receipt = kwargs["type"] == "team.tool_finished" and payload.get("name") == "continue_work"
                        if is_receipt and payload["result"]["status"] == "error":
                            self.assertFalse(f.rt.log.db.in_transaction)
                            self.assertEqual(f.rt.team._messages(f.task), before)
                            self.assertEqual(self.rows(f), rows)
                        if not failures and ((failure == "message" and is_message) or
                                             (failure == "receipt" and is_receipt)):
                            self.assertTrue(f.rt.log.db.in_transaction)
                            if failure == "message":
                                emit(**kwargs)
                            self.assertEqual(len(self.continuations(f)), 1)
                            failures.append(failure)
                            raise OSError("Injected continuation transaction failure")
                        return emit(**kwargs)

                    with patch.object(f.rt.log, "emit", side_effect=fail_once):
                        rejected, accepted, notice = self.run_calls(
                            f, source, self.call(original.seq), self.call(original.seq),
                            ("send_message", {"recipient": f.worker, "text": "Follow-up still runs",
                                              "notify_only": True}), statuses=("waiting",))
                    self.assertEqual(failures, [failure])
                    self.assertEqual(rejected["status"], "error", rejected)
                    continued = self.assert_continued(f, original, accepted)
                    self.assertEqual(notice["status"], "ok", notice)
                    self.assertEqual(self.continuations(f), [continued])
                    starts = [e for e in f.events("team.tool_started") if e.payload["name"] == "continue_work"]
                    receipts = [e for e in f.events("team.tool_finished") if e.payload["name"] == "continue_work"]
                    self.assertEqual(len(starts), 2)
                    self.assertEqual(len(receipts), 2)
                    self.assertIn("continue_work", LOCAL_EVENT_TOOLS)
                    for start, receipt in zip(starts, receipts):
                        self.assertEqual(start.payload["execution_contract"], LOCAL_EVENT_CONTRACT)
                        self.assertEqual(receipt.payload["started_seq"], start.seq)
                        self.assertLess(start.seq, receipt.seq)
                    self.assertLess(starts[1].seq, continued.seq)
                    self.assertLess(continued.seq, receipts[1].seq)
                    if mode == "external":
                        f.assert_receipt_chain()

    def test_success_continues_current_batch_and_next_turn_without_automatic_return(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                f = self.fixture(mode)
                root = returns.supervision.TeamSupervisionTests.bind_workspace(f)
                parent = self.assign(f)
                child = self.assign(f, recipient=f.reviewer, source=parent)
                self.settle(f, child)
                resumed, = self.run_calls(f, self.human(f), self.call(parent.seq))
                source = self.assert_continued(f, parent, resumed)
                before = len(f.driver.runs if mode == "external" else f.driver.calls)

                def followup(results):
                    self.assertEqual([result["status"] for result in results], ["ok", "ok"])
                    self.assertEqual(self.reports(f, parent), [])
                    if mode == "external":
                        self.assertFalse(f.driver.runs[-1].revoked)
                    return [self.write("next-turn.txt")]

                def inspect(context, tools):
                    self.assertEqual(context["active_assignment"]["seq"], parent.seq)

                result, first, second = self.run_calls(
                    f, source, self.call(child.seq), self.write("same-batch.txt"),
                    inspect=inspect, followup=followup, statuses=("waiting",))
                self.assert_continued(f, child, result)
                self.assertEqual((first["status"], second["status"]), ("ok", "ok"))
                self.assertTrue((root / "same-batch.txt").exists())
                self.assertTrue((root / "next-turn.txt").exists())
                self.assertEqual(self.reports(f, parent), [])
                self.assertEqual(set(self.rows(f)), {parent.seq, child.seq})
                self.assertEqual(len(f.driver.runs if mode == "external" else f.driver.calls),
                                 before + (1 if mode == "external" else 3))

    def test_pause_or_new_input_before_dispatch_prevents_stale_continuation(self):
        for mode in MODES:
            for change in ("pause", "new_input"):
                with self.subTest(mode=mode, change=change):
                    f = self.fixture(mode)
                    original = self.ready(f)
                    source = self.human(f)
                    starts = list(f.events("team.tool_started"))

                    def inspect(context, tools):
                        if change == "pause":
                            f.rt.execution_control.change(f.task, "pause", request_id="continue-before-pause")
                        else:
                            self.human(f)

                    self.assertEqual(self.run_calls(f, source, self.call(original.seq), inspect=inspect,
                                                    statuses=("interrupted",)), [])
                    self.assertEqual(f.events("team.tool_started"), starts)
                    self.assertEqual(self.continuations(f), [])
                    self.assertIn(original.seq, self.rows(f))
                    self.assertEqual(self.reports(f, original), [])
                    if change == "pause":
                        f.rt.execution_control.change(f.task, "resume", request_id="continue-after-resume")
                    accepted, = self.run_calls(f, self.human(f), self.call(original.seq))
                    self.assert_continued(f, original, accepted)

    def test_pause_or_new_input_after_commit_keeps_continuation_but_stops_next_tool(self):
        for mode in MODES:
            for change in ("pause", "new_input"):
                with self.subTest(mode=mode, change=change):
                    f = self.fixture(mode)
                    original = self.ready(f)
                    source = self.human(f)
                    invoke = f.rt.team._invoke_tool
                    changed = []

                    def change_after_commit(*args, **kwargs):
                        result = invoke(*args, **kwargs)
                        call = args[4]
                        if call.name == "continue_work" and result["status"] == "ok" and not changed:
                            self.assertFalse(f.rt.log.db.in_transaction)
                            changed.append(result["message_seq"])
                            if change == "pause":
                                f.rt.execution_control.change(f.task, "pause", request_id="continue-after-pause")
                            else:
                                self.human(f)
                        return result

                    with patch.object(f.rt.team, "_invoke_tool", side_effect=change_after_commit):
                        result, = self.run_calls(f, source, self.call(original.seq),
                            ("send_message", {"recipient": f.worker, "text": "Must not send", "notify_only": True}),
                            statuses=("interrupted",))
                    self.assertEqual(len(changed), 1)
                    continued = self.assert_continued(f, original, result)
                    self.assertEqual(self.continuations(f), [continued])
                    self.assertFalse([e for e in f.rt.team._messages(f.task)
                                      if e.payload.get("text") == "Must not send"])
                    if change == "pause":
                        f.rt.execution_control.change(f.task, "resume", request_id="continue-commit-resume")
                    returned, = self.run_calls(f, continued, returns.RETURN, statuses=("completed",))
                    self.assert_return(f, original, returned)

    def test_plain_send_message_never_infers_continuation_and_legacy_recipient_stays_checked(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                f = self.fixture(mode)
                original = self.ready(f)
                text = f"continue_work resume original assignment {original.seq}; follow up"
                sent, = self.run_calls(f, self.human(f), ("send_message", {"recipient": f.worker, "text": text}))
                plain = self.message(f, sent["message_seq"])
                self.assertEqual(plain.payload["kind"], "message")
                self.assertNotIn("continue_assignment_seq", plain.payload)
                self.assertEqual(self.continuations(f), [])
                self.assertEqual(set(self.rows(f)), {original.seq, plain.seq})
                self.settle(f, plain)
                invalid = [("send_message", {"recipient": f.reviewer, "text": INSTRUCTION,
                                              "continue_assignment_seq": original.seq})]
                _, continued = self.reject_then_correct(f, self.human(f), invalid, original)
                self.assertEqual(continued.payload["recipient"], f.worker)
                self.settle(f, continued)
                legacy, = self.run_calls(f, self.human(f), ("send_message", {
                    "recipient": f.worker, "text": INSTRUCTION, "continue_assignment_seq": original.seq}))
                self.assert_continued(f, original, legacy)
                self.assertIn(plain.seq, self.rows(f))


if __name__ == "__main__":
    unittest.main()
