"""Isolated withdrawal regressions: no Runtime, model, workspace, or database."""

from dataclasses import replace
from hashlib import sha256
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from cater.events import Actor, Event
from cater.team_reviews import TeamReviews
from cater.team_work_control import HOST, TeamWorkControl, linked_inputs


class MemoryLog:
    def __init__(self):
        self.events = []

    def emit(self, *, type, actor, task_id, payload):
        seq = len(self.events) + 1
        digest = sha256(repr((seq, type, actor, task_id, payload)).encode()).hexdigest()
        event = Event(seq, str(seq), "fixture", task_id, actor, type, "fixture",
                      dict(payload), "0" * 64, digest)
        self.events.append(event)
        return event

    def read(self, *, task_id):
        return [event for event in self.events if event.task_id == task_id]


class TeamWorkControlTests(unittest.TestCase):
    def setUp(self):
        self.task = "disposable-team"
        self.leader = SimpleNamespace(id="leader", role="manager")
        self.sender = SimpleNamespace(id="author", role="author")
        self.worker = SimpleNamespace(id="worker", role="worker")
        self.other = SimpleNamespace(id="other", role="other")
        self.log = MemoryLog()
        self.rt = SimpleNamespace(log=self.log, roles=[self.sender, self.worker, self.other],
            organization=SimpleNamespace(leader=self.leader),
            execution_control=SimpleNamespace(view=Mock(return_value={"state": "running"})),
            task=Mock(return_value=SimpleNamespace(deliverables=[])))
        self.team = SimpleNamespace(rt=self.rt, _messages=self.messages, view=self.team_view)
        self.control = TeamWorkControl(self.team)
        self.rt.team = self.team
        self.rt.team_reviews = TeamReviews(self.rt)
        self.revision = self.emit("team.objective_updated", {"goal": "Current objective"}).seq
        self.source = self.message()

    def emit(self, kind, payload, actor=HOST, task=None):
        return self.log.emit(type=kind, task_id=task or self.task, actor=actor, payload=payload)

    def messages(self, task_id, *, events=None):
        events = self.log.read(task_id=task_id) if events is None else events
        return [e for e in events if e.task_id == task_id and e.type == "team.message_received"]

    def team_view(self, task_id):
        objectives = [e for e in self.log.read(task_id=task_id) if e.actor == HOST
                      and e.type in {"team.objective_updated", "team.objective_completed"}]
        latest = objectives[-1] if objectives else None
        return {"revision": latest.seq if latest else 0,
                "status": "done" if latest and latest.type == "team.objective_completed" else "running"}

    def message(self, sender=None, recipient=None, kind="assignment", task=None, **extra):
        sender = sender or self.sender
        return self.emit("team.message_received", {
            "kind": kind, "recipient": recipient or self.worker.id, "revision": self.revision,
            "text": "Disposable work", **extra}, Actor.agent(sender.id, sender.role), task=task)

    def start(self, role=None, **overrides):
        role = role or self.sender
        return self.emit("team.tool_started", {"name": "withdraw_work", "role": role.id,
            "revision": self.revision, "message_seq": self.source.seq, "run_id": "trusted-fixture",
            **overrides})

    def withdraw(self, source=None, role=None, start=None, **arguments):
        source, role = source or self.source, role or self.sender
        return self.control.execute(self.task, role,
            {"message_seq": source.seq, "reason": "Duplicate request no longer needed", **arguments},
            start or self.start(role))

    def withdrawals(self):
        return [e for e in self.log.events if e.type == "team.assignment_withdrawn"]

    def test_sender_and_leader_can_withdraw_assignments_and_messages(self):
        for role in (self.sender, self.leader):
            for kind in ("assignment", "message"):
                with self.subTest(role=role.id, kind=kind):
                    source = self.message(kind=kind)
                    result = self.withdraw(source, role)
                    self.assertEqual(result["status"], "ok")
                    event = self.withdrawals()[-1]
                    self.assertEqual(event.actor, HOST)
                    self.assertEqual(event.payload["source_hash"], source.hash)
                    self.assertEqual(event.payload["requester"], role.id)
                    self.assertIn(source.seq, self.control.inactive(self.task))
                    row = self.control.view(self.task)[-1]
                    for key in ("withdrawal_seq", "message_seq", "requester", "reason", "revision"):
                        self.assertIn(key, row)

    def test_recipient_and_unrelated_role_cannot_withdraw(self):
        for role in (self.worker, self.other):
            with self.subTest(role=role.id):
                self.assertEqual(self.withdraw(role=role)["code"], "withdrawal_forbidden")
        self.assertEqual(self.withdrawals(), [])

    def test_customer_cross_team_and_nonwork_inputs_rejected(self):
        customer = self.emit("team.message_received", {"kind": "message", "recipient": "leader"}, Actor.human())
        foreign = self.message(task="different-team")
        for source in (customer, foreign, self.message(kind="report"), self.message(kind="continuation"),
                       self.message(kind="notification"), self.message(recipient="unknown")):
            with self.subTest(seq=source.seq):
                self.assertEqual(self.withdraw(source, self.leader)["code"], "invalid_work")
        self.assertEqual(self.withdrawals(), [])

    def test_old_objective_paused_and_completed_rejected(self):
        self.rt.execution_control.view.return_value = {"state": "paused"}
        self.assertEqual(self.withdraw()["code"], "work_not_current")
        self.rt.execution_control.view.return_value = {"state": "running"}
        self.emit("team.objective_completed", {})
        self.assertEqual(self.withdraw()["code"], "work_not_current")
        self.revision = self.emit("team.objective_updated", {"goal": "New goal"}).seq
        self.assertEqual(self.withdraw()["code"], "work_not_current")
        self.assertEqual(self.withdrawals(), [])

    def test_trusted_tool_context_must_match_task_role_and_revision(self):
        start = self.start()
        variants = [replace(start, task_id="foreign"), replace(start, actor=Actor.agent("author", "author")),
                    replace(start, type="team.run_started")]
        variants += [replace(start, payload={**start.payload, key: value}) for key, value in (
            ("role", "other"), ("revision", self.revision + 1), ("revision", True),
            ("run_id", ""), ("name", "delegate"))]
        for variant in variants:
            with self.subTest(start=variant):
                self.assertEqual(self.withdraw(start=variant)["code"], "work_not_current")
        self.assertEqual(self.withdrawals(), [])

    def test_arguments_validated_without_writes(self):
        for args in ({"message_seq": True}, {"message_seq": 0}, {"message_seq": "2"},
                     {"reason": " "}, {"reason": None}, {"reason": "x" * 4001}, {"extra": 1}):
            with self.subTest(args=args):
                self.assertEqual(self.withdraw(**args)["code"], "invalid_arguments")
        self.assertEqual(self.withdrawals(), [])

    def test_reason_at_schema_limit_is_accepted(self):
        result = self.withdraw(reason="x" * 4000)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(self.withdrawals()[0].payload["reason"]), 4000)

    def test_withdrawal_is_idempotent_and_preserves_first_reason(self):
        first = self.withdraw()
        result = self.withdraw(role=self.leader, reason="A later reason")
        self.assertTrue(result["already_withdrawn"])
        self.assertEqual(first["withdrawal_seq"], result["withdrawal_seq"])
        self.assertEqual(len(self.withdrawals()), 1)
        self.assertEqual(self.control.view(self.task)[0]["reason"], "Duplicate request no longer needed")

    def test_reported_work_returns_fact_without_withdrawal(self):
        self.message(sender=self.worker, recipient=self.sender.id, kind="report", reply_to=self.source.seq)
        result = self.withdraw()
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["already_reported"])
        self.assertFalse(result["withdrawn"])
        self.assertEqual(self.withdrawals(), [])

    def test_forged_or_wrong_revision_reports_do_not_settle_work(self):
        self.message(sender=self.other, recipient=self.sender.id, kind="report", reply_to=self.source.seq)
        self.message(sender=self.worker, recipient=self.sender.id, kind="report",
                     reply_to=self.source.seq, revision=self.revision + 1)
        self.assertTrue(self.withdraw()["withdrawn"])

    def formal_review(self):
        request = self.emit("team.review_requested", {"root_seq": None, "author": self.sender.id,
            "reviewer": self.worker.id, "revision": self.revision, "artifacts": []})
        actual = self.message(review_seq=request.seq)
        return request, actual

    def test_actual_current_formal_review_cannot_be_withdrawn(self):
        request, actual = self.formal_review()
        self.assertEqual(self.withdraw(actual, self.leader)["code"], "formal_review")
        self.assertEqual(self.withdrawals(), [])
        self.assertEqual(self.rt.team_reviews.view(self.task)[0]["request_seq"], request.seq)

    def test_ordinary_duplicate_review_tag_is_not_a_formal_review(self):
        request, actual = self.formal_review()
        ordinary = self.message(review_seq=request.seq)
        self.assertTrue(self.withdraw(ordinary)["withdrawn"])
        self.assertNotIn(actual.seq, self.control.inactive(self.task))

    def test_returned_formal_review_is_idempotent_without_withdrawing(self):
        _, actual = self.formal_review()
        self.message(sender=self.worker, recipient=self.sender.id, kind="report", reply_to=actual.seq)
        result = self.withdraw(actual, self.leader)
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["already_reported"])
        self.assertFalse(result["withdrawn"])
        self.assertEqual(self.withdrawals(), [])

    def test_replaced_formal_delivery_is_already_inactive_without_new_withdrawal(self):
        request, actual = self.formal_review()
        newer = self.emit("team.review_requested", {**request.payload, "root_seq": request.seq})
        latest = self.message(review_seq=newer.seq)
        for role in (self.sender, self.leader):
            result = self.withdraw(actual, role)
            self.assertEqual(result["status"], "ok")
            self.assertTrue(result["already_inactive"])
            self.assertFalse(result["withdrawn"])
        self.assertEqual(self.withdrawals(), [])
        self.assertNotIn(latest.seq, self.control.inactive(self.task))
        self.assertEqual(self.withdraw(latest)["code"], "formal_review")

    def test_bound_continuations_and_feedback_resuming_root_become_inactive(self):
        continuation = self.message(kind="continuation", continue_assignment_seq=self.source.seq)
        chained = self.message(kind="continuation", continue_assignment_seq=continuation.seq)
        progress = self.message(sender=self.worker, recipient=self.sender.id, kind="progress", reply_to=self.source.seq)
        report = self.message(sender=self.worker, recipient=self.sender.id, kind="report", reply_to=self.source.seq)
        child = self.message(sender=self.worker, recipient=self.other.id, parent_assignment_seq=self.source.seq)
        child_report = self.message(sender=self.other, recipient=self.worker.id, kind="report", reply_to=child.seq)
        continuation_reply = self.message(sender=self.worker, recipient=self.sender.id, kind="progress", reply_to=continuation.seq)
        wrong_sender = self.message(sender=self.other, kind="continuation", continue_assignment_seq=self.source.seq)
        wrong_recipient = self.message(recipient=self.other.id, kind="continuation", continue_assignment_seq=self.source.seq)
        wrong_revision = self.message(kind="continuation", continue_assignment_seq=self.source.seq, revision=0)
        forged_progress = self.message(sender=self.other, recipient=self.sender.id, kind="progress", reply_to=self.source.seq)
        linked = linked_inputs(reversed(self.messages(self.task)), {self.source.seq})
        self.assertEqual(linked, {self.source.seq, continuation.seq, chained.seq, progress.seq, report.seq, child_report.seq})
        self.assertTrue({child.seq, continuation_reply.seq, wrong_sender.seq,
                         wrong_recipient.seq, wrong_revision.seq, forged_progress.seq}.isdisjoint(linked))
        self.assertEqual(linked_inputs(self.messages(self.task), {self.source.seq}, include_feedback=False),
                         {self.source.seq, continuation.seq, chained.seq})

    def test_no_pending_effect_or_child_work_is_settled(self):
        child = self.message(sender=self.worker, recipient=self.other.id, parent_assignment_seq=self.source.seq)
        pending = self.emit("team.tool_started", {"name": "workspace_run", "run_id": "unsettled",
            "message_seq": self.source.seq, "role": self.worker.id, "revision": self.revision})
        before = list(self.log.events)
        self.withdraw()
        added = self.log.events[len(before):]
        self.assertEqual([e.type for e in added], ["team.tool_started", "team.assignment_withdrawn"])
        self.assertIn(pending, self.log.events)
        self.assertNotIn(child.seq, self.control.inactive(self.task))

    def test_late_child_feedback_cannot_restart_withdrawn_parent_work(self):
        self.withdraw()
        continuation = self.message(kind="continuation", continue_assignment_seq=self.source.seq)
        report = self.message(sender=self.worker, recipient=self.sender.id, kind="report", reply_to=self.source.seq)
        child = self.message(sender=self.worker, recipient=self.other.id, parent_assignment_seq=self.source.seq)
        child_report = self.message(sender=self.other, recipient=self.worker.id, kind="report", reply_to=child.seq)
        self.assertEqual(self.control.inactive(self.task), {self.source.seq, continuation.seq, report.seq, child_report.seq})
        self.assertNotIn(child.seq, self.control.inactive(self.task))

    def test_snapshot_projection_does_not_read_live_views(self):
        before = list(self.log.events)
        self.withdraw()
        after = list(self.log.events)
        self.revision = self.emit("team.objective_updated", {"goal": "Later goal"}).seq
        self.team.view = Mock(side_effect=AssertionError("Projection recursion"))
        self.rt.team_reviews.view = Mock(side_effect=AssertionError("Live review read"))
        self.log.read = Mock(side_effect=AssertionError("Live log read"))
        self.assertEqual(self.control.inactive(self.task, events=before), set())
        self.assertEqual(self.control.view(self.task, events=before), [])
        self.assertEqual(self.control.inactive(self.task, events=after), {self.source.seq})
        self.assertEqual(self.control.view(self.task, events=after)[0]["message_seq"], self.source.seq)

    def test_projection_rejects_forged_withdrawals(self):
        self.withdraw()
        valid = self.withdrawals()[0]
        snapshot = [e for e in self.log.events if e != valid]
        variants = [replace(valid, actor=Actor.agent("leader", "manager")),
                    replace(valid, actor=Actor.system("other")), replace(valid, task_id="foreign"),
                    replace(valid, seq=self.source.seq - 1)]
        variants += [replace(valid, payload={**valid.payload, key: value}) for key, value in (
            ("message_seq", True), ("message_seq", 999), ("source_hash", "forged"),
            ("requester", "worker"), ("sender", "other"), ("recipient", "other"),
            ("revision", self.revision + 1), ("revision", True), ("reason", ""), ("run_id", ""))]
        for variant in variants:
            with self.subTest(variant=variant):
                events = snapshot + [variant]
                self.assertEqual(self.control.view(self.task, events=events), [])
                self.assertEqual(self.control.inactive(self.task, events=events), set())

    def test_projection_rejects_old_revision_and_customer_sources(self):
        self.withdraw()
        valid = self.withdrawals()[0]
        snapshot = [e for e in self.log.events if e != valid]
        objective = self.emit("team.objective_updated", {"goal": "Changed"})
        late = replace(valid, seq=objective.seq + 1)
        self.assertEqual(self.control.view(self.task, events=snapshot + [objective, late]), [])
        customer = replace(self.source, actor=Actor.human())
        events = [customer if e == self.source else e for e in snapshot] + [valid]
        self.assertEqual(self.control.inactive(self.task, events=events), set())

    def test_historical_withdrawal_survives_new_objective_and_duplicate_events(self):
        self.withdraw()
        valid = self.withdrawals()[0]
        self.emit("team.assignment_withdrawn", valid.payload)
        self.revision = self.emit("team.objective_updated", {"goal": "Changed"}).seq
        self.assertEqual(len(self.control.view(self.task)), 1)
        self.assertEqual(self.control.inactive(self.task), {self.source.seq})


if __name__ == "__main__":
    unittest.main()
