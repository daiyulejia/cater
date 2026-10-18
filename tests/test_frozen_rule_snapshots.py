"""Frozen guidance receipts do not invent or replace the observed rules."""
from __future__ import annotations

import copy
import unittest
from unittest.mock import patch

from cater.events import Actor, EventLog
from cater.rules import RuleBook
import test_simple_team as fixture


class FrozenRuleSnapshotTests(unittest.TestCase):
    setUp = fixture.SimpleTeamTests.setUp

    def applications(self):
        return list(self.rt.log.iter_events(task_id=self.task, event_types=('rules.applied',)))

    def rule(self):
        return self.rt.rulebook.record('规则A：保留来源', scope='task', task_id=self.task)

    def test_live_and_frozen_recorders_do_not_reuse_an_agent_application(self):
        self.rule()
        payload = self.rt.rulebook.current_snapshot(self.task)
        for frozen in (True, False):
            with self.subTest(frozen=frozen):
                fake = self.rt.log.emit(type='rules.applied', task_id=self.task,
                    actor=Actor.agent(self.worker, self.rt.team._role(self.worker).role), payload=payload)
                result = (self.rt.rulebook._record_snapshot(self.task, payload) if frozen
                          else self.rt.rulebook.snapshot(self.task))
                self.assertNotEqual(result['seq'], fake.seq)
                event = next(e for e in self.applications() if e.seq == result['seq'])
                self.assertEqual(event.actor, Actor.system('rulebook'))
                self.assertEqual(event.payload, payload)

    def test_matching_fingerprint_with_wrong_body_is_not_a_matching_snapshot(self):
        self.rule()
        payload = self.rt.rulebook.current_snapshot(self.task)
        for frozen in (True, False):
            with self.subTest(frozen=frozen):
                forged = copy.deepcopy(payload)
                forged['rules'][0]['text'] = '不同正文但复用了旧fingerprint字段'
                event = self.rt.log.emit(type='rules.applied', task_id=self.task,
                    actor=Actor.system('rulebook'), payload=forged)
                result = (self.rt.rulebook._record_snapshot(self.task, payload) if frozen
                          else self.rt.rulebook.snapshot(self.task))
                self.assertGreater(result['seq'], event.seq)
                self.assertEqual({k: v for k, v in result.items() if k != 'seq'}, payload)

    def test_numeric_type_aliases_are_not_the_same_canonical_rule_body(self):
        self.rule()
        payload = self.rt.rulebook.current_snapshot(self.task)
        for field, value in (('revision', True), ('revision', 1.0), ('seq', float(payload['rules'][0]['seq']))):
            for frozen in (True, False):
                with self.subTest(field=field, value=value, frozen=frozen):
                    forged = copy.deepcopy(payload)
                    forged['rules'][0][field] = value
                    event = self.rt.log.emit(type='rules.applied', task_id=self.task,
                        actor=Actor.system('rulebook'), payload=forged)
                    result = (self.rt.rulebook._record_snapshot(self.task, payload) if frozen
                              else self.rt.rulebook.snapshot(self.task))
                    self.assertGreater(result['seq'], event.seq)

    def test_frozen_a_after_live_b_is_recorded_as_a_not_reinterpreted_as_b(self):
        rule = self.rule()
        a = self.rt.rulebook.current_snapshot(self.task)
        first = self.rt.rulebook._record_snapshot(self.task, a)
        self.rt.rulebook.update(rule['id'], revision=1, text='规则B：额外核对', status='active')
        b = self.rt.rulebook.snapshot(self.task)
        again = self.rt.rulebook._record_snapshot(self.task, a)
        self.assertNotEqual(first['seq'], again['seq'])
        self.assertGreater(again['seq'], b['seq'])
        self.assertEqual([e.payload['rules'][0]['text'] for e in self.applications()],
                         ['规则A：保留来源', '规则B：额外核对', '规则A：保留来源'])
        self.assertEqual(self.rt.rulebook.current_snapshot(self.task)['rules'][0]['text'], '规则B：额外核对')
        reopened = EventLog(self.rt.log.path)
        try:
            self.assertEqual(RuleBook(reopened)._record_snapshot(self.task, a)['seq'], again['seq'])
        finally:
            reopened.close()

    def test_empty_first_snapshot_and_retirement_keep_original_receipt_semantics(self):
        empty = self.rt.rulebook.current_snapshot(self.task)
        self.assertIsNone(self.rt.rulebook._record_snapshot(self.task, empty)['seq'])
        self.assertEqual(self.applications(), [])
        rule = self.rule()
        self.rt.rulebook.snapshot(self.task)
        self.rt.rulebook.update(rule['id'], revision=1, text=rule['text'], status='retired')
        retired = self.rt.rulebook.current_snapshot(self.task)
        result = self.rt.rulebook._record_snapshot(self.task, retired)
        self.assertIsInstance(result['seq'], int)
        self.assertEqual(result['rules'], [])
        self.assertEqual(len(self.applications()), 2)
        self.assertEqual(self.rt.rulebook.snapshot(self.task)['seq'], result['seq'])

    def test_competing_frozen_recorders_deduplicate_inside_the_short_write(self):
        self.rt.log.db.execute('PRAGMA journal_mode=WAL')
        self.rule()
        payload = self.rt.rulebook.current_snapshot(self.task)
        other = EventLog(self.rt.log.path)
        self.addCleanup(other.close)
        book = RuleBook(other)
        existing = self.rt.rulebook._existing_snapshot
        peer, reads = [], []
        def compete(*args, **kwargs):
            result = existing(*args, **kwargs)
            reads.append(True)
            if len(reads) == 1:
                peer.append(book._record_snapshot(self.task, payload))
            return result
        with patch.object(self.rt.rulebook, '_existing_snapshot', side_effect=compete):
            result = self.rt.rulebook._record_snapshot(self.task, payload)
        self.assertEqual(len(reads), 2)
        self.assertEqual(result['seq'], peer[0]['seq'])
        self.assertEqual(len(self.applications()), 1)
        self.assertTrue(self.rt.verify_chain().ok)

    def test_frozen_record_does_not_refresh_live_rules_or_use_a_global_head_cas(self):
        self.rule()
        payload = self.rt.rulebook.current_snapshot(self.task)
        with patch.object(self.rt.rulebook, 'current_snapshot', side_effect=AssertionError('read live rules')), \
                patch.object(self.rt.log, 'emit', wraps=self.rt.log.emit) as emit:
            result = self.rt.rulebook._record_snapshot(self.task, payload)
        self.assertEqual(result['fingerprint'], payload['fingerprint'])
        self.assertNotIn('expected_head', emit.call_args.kwargs)
        self.assertFalse(self.rt.log.db.in_transaction)


if __name__ == '__main__':
    unittest.main()
