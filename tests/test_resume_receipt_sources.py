"""Deterministic receipt boundaries using Runtime and append-only event APIs.

Reuse the existing disposable fixture; no new model or external API is involved.
Tool results here are synthetic attribution inputs, not process execution proof.
"""

from __future__ import annotations

import hashlib
import unittest
from unittest.mock import patch

from cater.delivery_commands import CommandRecordError, normalize as command_records
from cater.events import Actor
from cater.team import HOST
from cater.team_delivery import HandoffError
import test_delivery_commands as commands


class ResumeReceiptSourcesTests(unittest.TestCase):
    def setUp(self):
        self.fixture = commands.DeliveryCommandTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.rt, self.task = self.fixture.rt, self.fixture.task
        self.team, self.log = self.rt.team, self.rt.log
        self.manager = self.rt.organization.leader.id
        self.number = 0
        self.emit("team.objective_updated", {"goal": "Resume receipt boundary fixture"})
        source = self.team.receive(self.task, "Inspect recorded work", request_id="resume-receipt-input")
        claimed, _role, run = self.team._claim(self.task)
        self.assertEqual(claimed.seq, source.seq)
        self.emit("team.run_finished", {**run.payload, "status": "completed"})

    def emit(self, kind, payload, *, task=None, actor=HOST):
        return self.fixture.emit(kind, payload, task=task, actor=actor)

    def control(self, action, *, task=None):
        self.number += 1
        event, _changed = self.rt.execution_control.change(task or self.task, action,
            request_id=f"resume-receipt-control-{self.number}")
        return event

    def resumed(self, *, repeats=0):
        pause = self.control("pause")
        control = self.control("resume")
        for _ in range(repeats):
            control = self.control("resume")
        message = self.team.recovery.resume(self.task, control.seq)
        self.assertIsNotNone(message)
        self.assertTrue(self.team.recovery.is_message(message))
        return pause, control, message

    def notice(self, control, *, changes=None, task=None, actor=HOST):
        task = task or self.task
        scope = self.team.recovery.scope(task)
        return self.emit("team.message_received", {
            "kind": "recovery", "trigger": "control_resume", "recipient": self.manager,
            "resume_seq": control.seq, "resume_hash": control.hash,
            "revision": scope["revision"], "scope": scope,
            "text": "Synthetic recovery provenance fixture", **(changes or {}),
        }, task=task, actor=actor)

    def receipt(self, message, *, name="workspace_run", role=None, task=None):
        self.number += 1
        task = task or self.task
        run = {"run_id": f"resume-receipt-run-{self.number}", "role": role or self.manager,
               "message_seq": message.seq, "revision": self.team.recovery.scope(task)["revision"]}
        self.emit("team.run_started", run, task=task)
        arguments = {"command": "python", "args": ["-c", "pass"]}
        result = {"status": "error", "outcome": "not_started"}
        if name == "workspace_read":
            content = "Synthetic recorded content\n"
            arguments = {"path": "observed.txt"}
            result = {"status": "ok", "path": "observed.txt", "content": content,
                      "sha256": hashlib.sha256(content.encode()).hexdigest(), "bytes": len(content),
                      "offset": 0, "returned_bytes": len(content), "encoding": "utf-8", "truncated": False}
        start = self.emit("team.tool_started", {**run, "name": name,
            "call_id": f"resume-receipt-call-{self.number}", "arguments": arguments,
            "root": str(self.fixture.project)}, task=task)
        finish = self.emit("team.tool_finished", {"started_seq": start.seq,
            **{key: start.payload[key] for key in ("run_id", "role", "name", "call_id")},
            "result": result}, task=task)
        self.emit("team.run_finished", {**run, "status": "completed"}, task=task)
        return start, finish

    def exact(self, finish, **filters):
        return self.team.read_sources.exact(self.task, {"receipt_seq": finish.seq, **filters})

    def assert_rejected(self, message, *, role=None):
        self.assertFalse(self.team.recovery.is_message(message))
        _start, finish = self.receipt(message, role=role)
        self.assertEqual(self.exact(finish), [])
        with self.assertRaises(CommandRecordError):
            command_records(self.team, self.task, [finish.seq],
                revision=message.payload["revision"], artifacts=[])
        _start, read = self.receipt(message, name="workspace_read", role=role)
        self.assertEqual(self.exact(read), [])
        self.assertIsNone(self.team.read_sources.index(self.task))

    def test_valid_resume_workspace_run_exact(self):
        _pause, _control, message = self.resumed()
        start, finish = self.receipt(message)
        rows = self.exact(finish, message_seq=message.seq, role=self.manager, name="workspace_run")
        self.assertEqual([row["seq"] for row in rows], [finish.seq])
        self.assertEqual(rows[0]["result"], finish.payload["result"])
        self.assertEqual(rows[0]["started_seq"], start.seq)

    def test_valid_resume_workspace_run_index_and_command_records(self):
        _pause, _control, message = self.resumed()
        start, finish = self.receipt(message)
        context = self.team._context(self.task, message, self.rt.organization.leader)
        row, = [row for row in context["receipt_index"] if row["seq"] == finish.seq]
        self.assertEqual(row["message_seq"], message.seq)
        self.assertEqual(row["name"], "workspace_run")
        record, = self.fixture.records(finish)
        self.assertEqual(record["source_seq"], finish.seq)
        self.assertEqual(record["source_hash"], finish.hash)
        self.assertEqual(record["started_hash"], start.hash)
        self.assertEqual(record["request"], start.payload["arguments"])
        self.assertEqual(record["outcome"], "not_started")

    def test_valid_resume_file_read_index_and_exact(self):
        _pause, _control, message = self.resumed()
        _start, finish = self.receipt(message, name="workspace_read")
        index = self.team.read_sources.index(self.task)
        self.assertIsNotNone(index)
        row, = index["files"]
        self.assertEqual(row["last_observed"]["seq"], finish.seq)
        self.assertEqual(row["last_observed"]["read_args"], {"receipt_seq": finish.seq})
        self.assertEqual(self.exact(finish)[0]["result"], finish.payload["result"])

    def test_repeated_resume_controls_trace_back_to_pause(self):
        pause, control, message = self.resumed(repeats=3)
        self.assertNotEqual(control.payload["previous_seq"], pause.seq)
        self.assertEqual(self.team.recovery.resume(self.task, control.seq), message)
        _start, finish = self.receipt(message)
        self.assertEqual(self.exact(finish)[0]["seq"], finish.seq)
        self.assertEqual(self.fixture.records(finish)[0]["source_seq"], finish.seq)

    def test_resume_hash_and_seq_must_match_real_control(self):
        changes = [{"resume_hash": value} for value in ("0" * 64, "", None, True)]
        changes += [{"resume_seq": value} for value in (True, 1.0, "1", [], {}, 0, -1, 999999, 2 ** 63)]
        for changed in changes:
            with self.subTest(changed=changed):
                self.control("pause")
                control = self.control("resume")
                self.assert_rejected(self.notice(control, changes=changed))

    def test_resume_without_pause_is_rejected(self):
        control = self.control("resume")
        self.assertIsNone(control.payload["previous_seq"])
        self.assert_rejected(self.notice(control))

    def test_previous_link_must_exist_and_strictly_precede_resume(self):
        for previous in (None, True, 1.0, "1", [], {}, 999999, "self", "future"):
            with self.subTest(previous=previous):
                self.control("pause")
                control = self.control("resume")
                seq = control.seq + (1 if previous == "self" else 2) if previous in ("self", "future") else previous
                forged = self.emit(control.type, {**control.payload, "previous_seq": seq}, actor=Actor.human())
                self.assert_rejected(self.notice(forged))

    def test_cancel_in_control_lineage_cannot_authorize_resume(self):
        self.control("pause")
        entry = self.rt.execution_control.entry(self.task, "resume", request_id="cancel-chain-resume")
        cancel = self.control("cancel")
        forged = self.emit(entry["type"], {**entry["payload"], "previous_seq": cancel.seq}, actor=Actor.human())
        self.assert_rejected(self.notice(forged))

    def test_pause_predecessor_requires_human_control_and_pause_action(self):
        cases = ((Actor.system("other"), "task.execution_controlled", "pause"),
                 (Actor.human(), "team.run_finished", "pause"),
                 (Actor.human(), "task.execution_controlled", "cancel"))
        for actor, kind, action in cases:
            with self.subTest(actor=actor, kind=kind, action=action):
                pause = self.control("pause")
                previous = self.emit(kind, {**pause.payload, "action": action}, actor=actor)
                control = self.control("resume")
                forged = self.emit(control.type, {**control.payload, "previous_seq": previous.seq}, actor=Actor.human())
                self.assert_rejected(self.notice(forged))

    def test_control_requires_human_resume_running(self):
        cases = ((Actor.system("other"), "task.execution_controlled", {}),
                 (Actor.human(), "team.run_finished", {}),
                 (Actor.human(), "task.execution_controlled", {"action": "pause"}),
                 (Actor.human(), "task.execution_controlled", {"state": "paused"}))
        for actor, kind, changes in cases:
            with self.subTest(actor=actor, kind=kind, changes=changes):
                self.control("pause")
                control = self.control("resume")
                forged = self.emit(kind, {**control.payload, **changes}, actor=actor)
                self.assert_rejected(self.notice(forged))

    def test_objective_change_between_resume_and_notice_is_rejected(self):
        for kind, keep_old_scope in (("team.objective_updated", False),
                                     ("team.objective_updated", True),
                                     ("team.objective_completed", False)):
            with self.subTest(kind=kind, keep_old_scope=keep_old_scope):
                self.emit("team.objective_updated", {"goal": "Before resume"})
                self.control("pause")
                control = self.control("resume")
                scope = self.team.recovery.scope(self.task)
                self.emit(kind, {"goal": "Changed after resume", "summary": "Synthetic objective boundary"})
                changes = {"revision": scope["revision"], "scope": scope} if keep_old_scope else {}
                self.assert_rejected(self.notice(control, changes=changes))

    def test_duplicate_recovery_is_rejected_without_hiding_original(self):
        _pause, _control, message = self.resumed()
        duplicate = self.emit(message.type, message.payload)
        self.assert_rejected(duplicate)
        self.assertTrue(self.team.recovery.is_message(message))
        _start, original = self.receipt(message)
        self.assertEqual(self.exact(original)[0]["seq"], original.seq)
        self.assertEqual(self.fixture.records(original)[0]["source_seq"], original.seq)

    def test_non_host_objective_and_duplicate_lookalikes_do_not_block_valid_resume(self):
        self.control("pause")
        control = self.control("resume")
        for actor in (Actor.human(), Actor.system("other")):
            self.notice(control, actor=actor)
            self.emit("team.objective_updated", {"goal": "Untrusted objective"}, actor=actor)
        other = self.rt.create_task("Foreign duplicate lookalike")
        self.emit("team.message_received", {"kind": "recovery", "trigger": "control_resume",
            "resume_seq": control.seq, "recipient": self.manager}, task=other)
        message = self.notice(control)
        self.assertTrue(self.team.recovery.is_message(message))
        _start, finish = self.receipt(message)
        self.assertEqual(self.exact(finish)[0]["seq"], finish.seq)
        self.assertEqual(self.fixture.records(finish)[0]["source_seq"], finish.seq)

    def test_distinct_resume_notices_do_not_count_as_duplicates(self):
        _pause, _control, first = self.resumed()
        _start, earlier = self.receipt(first)
        _pause, _control, second = self.resumed()
        _start, later = self.receipt(second)
        for finish in (earlier, later):
            with self.subTest(seq=finish.seq):
                self.assertEqual(self.exact(finish)[0]["seq"], finish.seq)
                self.assertEqual(self.fixture.records(finish)[0]["source_seq"], finish.seq)

    def test_foreign_task_control_and_pause_links_are_rejected(self):
        other = self.rt.create_task("Foreign control fixture")
        self.team.enable(other)
        foreign_pause = self.control("pause", task=other)
        foreign_control = self.control("resume", task=other)
        self.assert_rejected(self.notice(foreign_control))
        self.control("pause")
        control = self.control("resume")
        forged = self.emit(control.type, {**control.payload, "previous_seq": foreign_pause.seq}, actor=Actor.human())
        self.assert_rejected(self.notice(forged))

    def test_foreign_task_receipt_and_message_links_are_rejected(self):
        other = self.rt.create_task("Foreign receipt fixture")
        self.team.enable(other)
        self.control("pause", task=other)
        control = self.control("resume", task=other)
        message = self.notice(control, task=other)
        self.assertTrue(self.team.recovery.is_message(message))
        foreign = self.receipt(message, task=other)[1]
        linked = self.receipt(message)[1]
        for finish in (foreign, linked):
            with self.subTest(seq=finish.seq):
                self.assertEqual(self.exact(finish), [])
                with self.assertRaises(HandoffError):
                    self.fixture.records(finish)

    def test_role_mismatch_and_non_leader_recovery_are_rejected(self):
        _pause, _control, message = self.resumed()
        for role in (self.fixture.f.worker, "removed-member"):
            with self.subTest(role=role):
                _start, finish = self.receipt(message, role=role)
                self.assertEqual(self.exact(finish), [])
                with self.assertRaises(HandoffError):
                    self.fixture.records(finish)
        self.control("pause")
        control = self.control("resume")
        message = self.notice(control, changes={"recipient": self.fixture.f.worker})
        self.assert_rejected(message, role=self.fixture.f.worker)

    def test_old_objective_receipt_remains_exact_but_cannot_be_delivered(self):
        _pause, _control, message = self.resumed()
        _start, finish = self.receipt(message)
        self.emit("team.objective_updated", {"goal": "New delivery target"})
        self.assertTrue(self.team.recovery.is_message(message))
        self.assertEqual(self.exact(finish)[0]["seq"], finish.seq)
        with self.assertRaises(HandoffError):
            self.fixture.records(finish)
        current = self.fixture.chain()[3]
        self.assertEqual(self.fixture.records(current)[0]["source_seq"], current.seq)

    def test_ordinary_recovery_keeps_exact_and_command_records(self):
        source = self.team.receive(self.task, "Interrupted work fixture", request_id="ordinary-recovery-input")
        claimed, _role, run = self.team._claim(self.task)
        self.assertEqual(claimed.seq, source.seq)
        message, = self.team.recovery.prepare(self.task)
        self.assertEqual(message.payload["recovery_of"], run.seq)
        self.assertNotIn("resume_seq", message.payload)
        self.assertTrue(self.team.recovery.is_message(message))
        _start, finish = self.receipt(message)
        self.assertEqual(self.exact(finish)[0]["seq"], finish.seq)
        self.assertEqual(self.fixture.records(finish)[0]["source_seq"], finish.seq)
        duplicate = self.emit(message.type, message.payload)
        self.assert_rejected(duplicate)

    def test_exact_and_command_records_do_not_read_whole_conversation(self):
        _pause, _control, message = self.resumed(repeats=2)
        _start, finish = self.receipt(message)
        revision = self.team.view(self.task)["revision"]
        with patch.object(self.log, "read", side_effect=AssertionError("Unexpected full event-log read")):
            self.assertEqual(self.exact(finish)[0]["seq"], finish.seq)
            record, = command_records(self.team, self.task, [finish.seq], revision=revision, artifacts=[])
        self.assertEqual(record["source_seq"], finish.seq)

    def assert_v2_negative_proof_rejected(self, message):
        start, finish = self.receipt(message)
        # Match the tool revision so rejection must come from the source proof.
        with patch.object(self.log, "read", side_effect=AssertionError("Unexpected full event-log read")):
            self.assertEqual(self.exact(finish), [])
            with self.assertRaises(CommandRecordError):
                command_records(self.team, self.task, [finish.seq],
                    revision=start.payload["revision"], artifacts=[])

    def test_v2_nul_key_in_earlier_recovery_still_vetoes_duplicate(self):
        self.control("pause")
        control = self.control("resume")
        self.notice(control, changes={"bad\0key": True})
        message = self.notice(control)
        self.assertFalse(self.team.recovery.is_message(message))
        self.assertTrue(self.rt.verify_chain().ok)
        self.assert_v2_negative_proof_rejected(message)

    def test_v2_nul_last_objective_before_resume_cannot_fall_back(self):
        for kind in ("team.objective_updated", "team.objective_completed"):
            with self.subTest(kind=kind):
                old = self.emit("team.objective_updated", {"goal": "Earlier valid objective"})
                self.emit(kind, {"goal": "Unprojectable latest objective", "bad\0key": True})
                self.control("pause")
                control = self.control("resume")
                scope = {**self.team.recovery.scope(self.task), "revision": old.seq, "done": False}
                message = self.notice(control, changes={"revision": old.seq, "scope": scope})
                self.assertFalse(self.team.recovery.is_message(message))
                self.assertTrue(self.rt.verify_chain().ok)
                self.assert_v2_negative_proof_rejected(message)

    def test_v2_nul_last_objective_after_resume_cannot_fall_back(self):
        for kind in ("team.objective_updated", "team.objective_completed"):
            with self.subTest(kind=kind):
                self.emit("team.objective_updated", {"goal": "Earlier valid objective"})
                self.control("pause")
                control = self.control("resume")
                scope = self.team.recovery.scope(self.task)
                self.emit(kind, {"goal": "Unprojectable latest objective", "bad\0key": True})
                message = self.notice(control, changes={"revision": scope["revision"], "scope": scope})
                self.assertFalse(self.team.recovery.is_message(message))
                self.assertTrue(self.rt.verify_chain().ok)
                self.assert_v2_negative_proof_rejected(message)

    def test_v2_duplicate_kind_identity_cannot_hide_prior_recovery(self):
        for first, second in (("message", "recovery"), ("recovery", "message")):
            with self.subTest(first=first, second=second):
                self.control("pause")
                control = self.control("resume")
                previous = self.notice(control, changes={"kind": first})
                message = self.notice(control)
                encoded = self.fixture.duplicate_json(previous.payload, "kind", second)
                with self.fixture.raw_json(previous, "payload_json", encoded):
                    self.assert_v2_negative_proof_rejected(message)
                self.assertTrue(self.rt.verify_chain().ok)

    def test_v2_duplicate_resume_seq_identity_cannot_hide_prior_recovery(self):
        for matching_first in (False, True):
            with self.subTest(matching_first=matching_first):
                self.control("pause")
                control = self.control("resume")
                other = control.seq + 10000
                first, second = (control.seq, other) if matching_first else (other, control.seq)
                previous = self.notice(control, changes={"resume_seq": first})
                message = self.notice(control)
                encoded = self.fixture.duplicate_json(previous.payload, "resume_seq", second)
                with self.fixture.raw_json(previous, "payload_json", encoded):
                    self.assert_v2_negative_proof_rejected(message)
                self.assertTrue(self.rt.verify_chain().ok)

    def test_v2_non_host_malformed_unrelated_records_do_not_veto(self):
        for actor in (Actor.human(), Actor.system("other")):
            with self.subTest(actor=actor):
                self.emit("team.objective_updated", {"goal": "Non-host before resume", "bad\0key": True}, actor=actor)
                self.control("pause")
                control = self.control("resume")
                self.emit("team.objective_completed", {"summary": "Non-host after resume", "bad\0key": True}, actor=actor)
                self.notice(control, actor=actor, changes={"kind": "message", "bad\0key": True})
                previous = self.notice(control, actor=actor, changes={"kind": "message", "resume_seq": control.seq + 10000})
                message = self.notice(control)
                self.assertTrue(self.team.recovery.is_message(message))
                start, finish = self.receipt(message)
                for key, value in (("kind", "recovery"), ("resume_seq", control.seq)):
                    with self.subTest(key=key), self.fixture.raw_json(previous, "payload_json",
                            self.fixture.duplicate_json(previous.payload, key, value)):
                        with patch.object(self.log, "read", side_effect=AssertionError("Unexpected full event-log read")):
                            self.assertEqual(self.exact(finish)[0]["seq"], finish.seq)
                            record, = command_records(self.team, self.task, [finish.seq],
                                revision=start.payload["revision"], artifacts=[])
                        self.assertEqual(record["source_seq"], finish.seq)
                self.assertTrue(self.rt.verify_chain().ok)


if __name__ == "__main__":
    unittest.main()
