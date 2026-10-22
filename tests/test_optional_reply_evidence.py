"""Optional quotes never fabricate evidence or replay an already sent answer."""

import copy
from dataclasses import replace
import json
import sqlite3
from unittest.mock import patch

from cater import team_reply
from cater.events import Actor
from cater.reply_evidence import ReplyEvidenceError, normalize
import test_reply_evidence as fixtures


class OptionalReplyEvidenceTests(fixtures.ReplyFixture):
    def omitted(self, context, refs):
        task, role, source, run = context
        before = self.log.read(task_id=task)
        original = copy.deepcopy(refs)
        with self.assertRaises(ReplyEvidenceError):
            normalize(self.team, task, refs, before_seq=self.log.count() + 1)
        start, result = self.dispatch(context, refs)
        answer, = self.answers(task)
        self.assertEqual(result, {"status": "ok", "message_seq": answer.seq, "reply_to": source.seq,
            "reply_evidence_status": "omitted_invalid", "reply_evidence_warning": team_reply.OMITTED_WARNING})
        self.assertEqual(refs, original)
        self.assertEqual(answer.payload["text"], fixtures.TEXT)
        self.assertEqual(answer.payload["reply_evidence_started_seq"], start.seq)
        self.assertEqual(answer.actor, fixtures.role_actor(role))
        self.assertNotIn("reply_evidence", answer.payload)
        self.assertNotIn("reply_evidence_warning", answer.payload)
        receipt, = [e for e in self.events("team.tool_finished", task) if e.payload.get("started_seq") == start.seq]
        self.assertEqual(receipt.payload["result"], result)
        self.assertLess(start.seq, answer.seq)
        self.assertLess(answer.seq, receipt.seq)
        self.assertEqual(start.payload["arguments"]["evidence"], original)
        self.assertEqual(self.log.read(task_id=task)[:len(before)], before)
        self.assertEqual(team_reply.committed_reply(self.team, run, self.log.read(task_id=task)), answer)
        self.assertEqual(self.events("team.objective_completed", task), [])
        self.assertEqual(self.events("team.review_recorded", task), [])
        self.assertTrue(self.rt.verify_chain().ok)
        return start, answer, receipt

    def test_partial_valid_and_invalid_quotes_are_discarded_together(self):
        _s, original = self.source()
        _, _, receipt = self.omitted(self.context(), [self.ref(original), self.ref(original, "INVENTED_QUOTE")])
        self.assertNotIn("RAW_RECEIPT_BODY", json.dumps(receipt.payload["result"]))
        self.assertNotIn("INVENTED_QUOTE", json.dumps(receipt.payload["result"]))

    def test_foreign_source_and_text_are_not_exposed_in_reply_or_warning(self):
        foreign = self.new_task()
        _s, original = self.source(task=foreign, result=self.result("FOREIGN_PRIVATE_BODY"))
        _, answer, receipt = self.omitted(self.context(), [self.ref(original, "FOREIGN_PRIVATE_BODY")])
        self.assertNotIn("FOREIGN_PRIVATE_BODY", json.dumps([answer.payload, receipt.payload["result"]]))

    def test_bad_optional_shapes_leave_valid_body_sendable(self):
        invalid = (None, True, {}, "text", [None], [{}], [{"source_seq": True, "quote": "x"}],
                   [{"source_seq": 999999, "quote": "x", "status": "approved"}],
                   [{"source_seq": 999999, "quote": "x"}] * 9)
        for refs in invalid:
            with self.subTest(refs=refs):
                self.omitted(self.context(self.new_task()), refs)

    def test_large_optional_evidence_is_not_partially_published(self):
        task = self.new_task()
        body = "中" * 2000
        sources = [self.source(task=task, result=self.result(body))[1] for _ in range(5)]
        self.omitted(self.context(task), [self.ref(source, body) for source in sources])

    def test_identical_retry_does_not_send_or_commit_another_answer(self):
        _s, original = self.source()
        context = self.context()
        refs = [self.ref(original, "WRONG")]
        _, answer, receipt = self.omitted(context, refs)
        _start, result = self.dispatch(context, copy.deepcopy(refs))
        self.assertEqual(result, receipt.payload["result"])
        self.assertEqual(self.answers(), [answer])

    def test_retry_cannot_replace_omitted_refs_or_upgrade_them_to_verified(self):
        _s, original = self.source()
        context = self.context()
        _, answer, _r = self.omitted(context, [self.ref(original, "WRONG")])
        for refs in ([], [self.ref(original)], [self.ref(original, "ANOTHER_WRONG")], fixtures.MISSING):
            with self.subTest(refs=refs):
                _start, result = self.dispatch(context, refs)
                self.assertEqual(result["status"], "error")
                self.assertEqual(self.answers(), [answer])

    def test_omission_never_relaxes_text_or_unknown_argument_validation(self):
        context = self.context()
        for args in ({}, {"text": None}, {"text": " "}, {"text": "x" * 32001},
                     {"text": fixtures.TEXT, "unknown": "not permitted"}):
            _start, result = self.dispatch(context, [{"source_seq": 99999, "quote": "WRONG"}], arguments=args)
            self.assertEqual(result["status"], "error")
            self.assertEqual(self.answers(), [])

    def test_unexpected_normalizer_failure_does_not_commit_a_reply(self):
        context = self.context()
        with patch.object(team_reply, "normalize", side_effect=OSError("unexpected source storage failure")):
            _start, result = self.dispatch(context, [])
        self.assertEqual(result["status"], "error")
        self.assertEqual(self.answers(), [])

    def test_atomic_receipt_failure_rolls_back_body_and_omission(self):
        context = self.context()
        emit = self.log.emit
        def fail_receipt(**kwargs):
            if (kwargs["type"] == "team.tool_finished" and kwargs["payload"].get("name") == "reply_to_customer"
                    and kwargs["payload"]["result"].get("status") == "ok"):
                self.assertEqual(len(self.answers()), 1)
                raise OSError("write failed")
            return emit(**kwargs)
        with patch.object(self.log, "emit", side_effect=fail_receipt):
            _start, result = self.dispatch(context, None)
        self.assertEqual(result["status"], "error")
        self.assertEqual(self.answers(), [])
        self.assertIsNone(team_reply.committed_reply(self.team, context[3], self.log.read(task_id=self.task)))
        self.omitted(context, None)

    def test_recovery_accepts_omission_without_model_replay_or_second_message(self):
        context = self.context()
        _, answer, _ = self.omitted(context, None)
        with patch.object(self.f.driver, "agent_run", side_effect=AssertionError("Must not replay")):
            self.assertEqual(self.team.recovery.prepare(self.task), [])
            snapshot = self.log.read(task_id=self.task)
            self.assertEqual(self.team.recovery.prepare(self.task), [])
            self.assertEqual(self.log.read(task_id=self.task), snapshot)
        self.assertEqual(self.answers(), [answer])
        ended, = self.events("team.run_finished")
        self.assertEqual(ended.payload["recovered_customer_reply_seq"], answer.seq)

    def test_temporary_source_read_failure_stays_omitted_after_source_recovers(self):
        _source_start, source = self.source()
        context = self.context()
        refs = [self.ref(source)]
        with patch.object(self.team.read_sources, "exact", side_effect=sqlite3.OperationalError("temporary read failure")):
            _start, result = self.dispatch(context, refs)
        answer, = self.answers()
        self.assertEqual(result["reply_evidence_status"], "omitted_invalid")
        self.assertIsNotNone(normalize(self.team, self.task, refs, before_seq=self.log.count() + 1))
        with patch.object(team_reply, "normalize", side_effect=AssertionError("Do not reinterpret omission")):
            _start, retried = self.dispatch(context, copy.deepcopy(refs))
            self.assertEqual(retried, result)
            self.assertEqual(team_reply.committed_reply(self.team, context[3], self.log.read(task_id=self.task)), answer)
            self.assertEqual(self.team.recovery.prepare(self.task), [])
        self.assertEqual(self.answers(), [answer])

    def test_unpaired_unicode_optional_quote_keeps_auditable_exactly_once_answer(self):
        _source_start, source = self.source()
        context = self.context()
        refs = [self.ref(source, "\ud800")]
        start, result = self.dispatch(context, refs)
        answer, = self.answers()
        self.assertEqual(start.payload["arguments"]["encoding"], "escaped_json")
        self.assertEqual(json.loads(start.payload["arguments"]["json"])["evidence"], refs)
        self.assertEqual(result["reply_evidence_status"], "omitted_invalid")
        _start, retried = self.dispatch(context, copy.deepcopy(refs))
        self.assertEqual(retried, result)
        self.assertEqual(team_reply.committed_reply(self.team, context[3], self.log.read(task_id=self.task)), answer)
        self.assertEqual(self.team.recovery.prepare(self.task), [])
        self.assertEqual(self.answers(), [answer])
        self.assertTrue(self.rt.verify_chain().ok)

    def test_verified_reply_cannot_be_downgraded_on_retry_read_failure(self):
        _source_start, source = self.source()
        context = self.context()
        refs = [self.ref(source)]
        _start, result = self.dispatch(context, refs)
        answer, = self.answers()
        self.assertIn("reply_evidence", result)
        with patch.object(self.team.read_sources, "exact", side_effect=sqlite3.OperationalError("temporary read failure")):
            _start, retried = self.dispatch(context, refs)
        self.assertEqual(retried["status"], "error")
        self.assertEqual(team_reply.committed_reply(self.team, context[3], self.log.read(task_id=self.task)), answer)
        self.assertEqual(self.answers(), [answer])

    def test_escaped_audit_wrapper_is_data_and_must_bind_the_original_text(self):
        context = self.context()
        start, _answer, _receipt = self.omitted(context, None)
        events = self.log.read(task_id=self.task)
        for value in ({"encoding": "escaped_json", "json": "{"},
                      {"encoding": "escaped_json", "json": "[]"},
                      {"encoding": "escaped_json", "json": '{"text":"other","evidence":null}'},
                      {"encoding": "escaped_json", "json": '{"text":"a","text":"b","evidence":null}'},
                      {"encoding": "escaped_json", "json": json.dumps(start.payload["arguments"]), "extra": True}):
            candidate = [replace(e, payload={**e.payload, "arguments": value}) if e.seq == start.seq else e for e in events]
            self.assertIsNone(team_reply.committed_reply(self.team, context[3], candidate))

    def test_omission_recovery_rejects_forged_or_unpaired_results(self):
        context = self.context()
        start, answer, receipt = self.omitted(context, None)
        events = self.log.read(task_id=self.task)
        def changed(event, payload):
            return [replace(e, payload=payload) if e.seq == event.seq else e for e in events]
        cases = [[e for e in events if e.seq != receipt.seq], [e for e in events if e.seq != answer.seq]]
        for key, value in (("reply_evidence_status", "approved"), ("reply_evidence_warning", "forged"),
                           ("reply_evidence", None), ("reply_evidence", {"sources": [{"quote": "WRONG"}]})):
            cases.append(changed(receipt, {**receipt.payload, "result": {**receipt.payload["result"], key: value}}))
        for key in ("reply_evidence_status", "reply_evidence_warning"):
            cases.append(changed(receipt, {**receipt.payload, "result": {
                k: v for k, v in receipt.payload["result"].items() if k != key}}))
        cases.append(changed(answer, {**answer.payload, "reply_evidence_started_seq": True}))
        cases.append(changed(start, {**start.payload, "arguments": {"text": fixtures.TEXT}}))
        cases.append([replace(e, actor=Actor.agent(self.manager, "leader")) if e.seq == receipt.seq else e for e in events])
        for candidate in cases:
            self.assertIsNone(team_reply.committed_reply(self.team, context[3], candidate))
        self.assertEqual(self.log.read(task_id=self.task), events)

    def test_valid_or_text_only_reply_cannot_gain_an_omission_marker(self):
        context = self.context()
        _start, _result = self.dispatch(context)
        events = self.log.read(task_id=self.task)
        receipt = self.events("team.tool_finished")[-1]
        forged = replace(receipt, payload={**receipt.payload, "result": {**receipt.payload["result"],
            "reply_evidence_status": "omitted_invalid", "reply_evidence_warning": team_reply.OMITTED_WARNING}})
        self.assertIsNone(team_reply.committed_reply(self.team, context[3],
            [forged if e.seq == receipt.seq else e for e in events]))
