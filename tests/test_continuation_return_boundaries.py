"""Return idempotency and continuation boundaries with disposable fake turns."""

import unittest
from unittest.mock import patch

from cater.work import WorkConflict
import test_continuation_returns as continuation_returns
import test_team_review_cycle as review_cycle


class ContinuationReturnBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = continuation_returns.ContinuationReturnTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.f = self.fixture.f
        self.rt, self.task = self.fixture.rt, self.fixture.task
        self.original = self.fixture.original
        self.continuation = self.fixture.continuation
        self.text = 'Checked and completed.'

    def message(self, seq):
        return next(e for e in self.rt.team._messages(self.task) if e.seq == seq)

    def reply(self, seq, text=None, recipient=None):
        return ('send_message', {
            'recipient': recipient or self.f.manager,
            'reply_to': seq,
            'text': self.text if text is None else text,
        })

    def assert_single_return(self, first):
        self.assertEqual(first['status'], 'ok', first)
        reports = [e for e in self.rt.team._messages(self.task)
                   if e.payload.get('kind') == 'report'
                   and e.payload.get('reply_to') == self.original.seq]
        self.assertEqual(len(reports), 1)
        report = reports[0]
        self.assertEqual(report.seq, first['message_seq'])
        self.assertEqual(report.actor.id, self.f.worker)
        self.assertEqual(report.payload['recipient'], self.f.manager)
        self.assertEqual(report.payload['text'], self.text)
        self.assertNotIn(self.original.seq,
                         {row['seq'] for row in self.rt.team.recovery.assignments(self.task)})
        self.assertTrue(self.rt.verify_chain().ok)
        return report

    def assert_batch_ends_at_return(self, first_target, second_target):
        before = len(self.rt.team._messages(self.task))
        first, = self.f.exchange(
            self.f.worker, self.reply(first_target), self.reply(second_target),
            source=self.continuation, expected_result_count=1)
        self.assertEqual(len(self.f.results('send_message')), 2)  # continuation and one return
        self.assertEqual(len(self.rt.team._messages(self.task)), before + 1)
        self.assert_single_return(first)

    def test_batch_stops_after_continuation_return_before_duplicate(self):
        self.assert_batch_ends_at_return(self.continuation.seq, self.continuation.seq)

    def test_batch_stops_after_original_return_before_duplicate(self):
        self.assert_batch_ends_at_return(self.original.seq, self.original.seq)

    def test_batch_stops_after_original_before_continuation_return(self):
        self.assert_batch_ends_at_return(self.original.seq, self.continuation.seq)

    def test_batch_stops_after_continuation_before_original_return(self):
        self.assert_batch_ends_at_return(self.continuation.seq, self.original.seq)

    def test_new_input_repeated_text_reuses_report_for_both_targets(self):
        first, = self.f.exchange(self.f.worker, self.reply(self.continuation.seq),
                                 source=self.continuation)
        report = self.assert_single_return(first)
        for target in (self.original.seq, self.continuation.seq):
            with self.subTest(reply_to=target):
                before = len(self.rt.team._messages(self.task))
                repeated, = self.f.exchange(self.f.worker, self.reply(target))
                self.assertEqual(repeated['status'], 'ok', repeated)
                self.assertIs(repeated.get('already_reported'), True)
                self.assertEqual(repeated['message_seq'], report.seq)
                # exchange creates one new human input, but no outgoing message.
                self.assertEqual(len(self.rt.team._messages(self.task)), before + 1)
                self.assertEqual(self.assert_single_return(first), report)

    def test_same_turn_different_text_is_not_executed_after_return(self):
        before = len(self.rt.team._messages(self.task))
        first, = self.f.exchange(
            self.f.worker, self.reply(self.continuation.seq),
            self.reply(self.original.seq, 'Correction: the check failed.'),
            source=self.continuation, expected_result_count=1)
        self.assertEqual(len(self.f.results('send_message')), 2)
        self.assertEqual(len(self.rt.team._messages(self.task)), before + 1)
        self.assert_single_return(first)

    def test_new_input_different_text_preserves_original_report(self):
        first, = self.f.exchange(self.f.worker, self.reply(self.original.seq),
                                 source=self.continuation)
        report = self.assert_single_return(first)
        for target in (self.original.seq, self.continuation.seq):
            with self.subTest(reply_to=target):
                before = len(self.rt.team._messages(self.task))
                changed, = self.f.exchange(
                    self.f.worker, self.reply(target, 'Correction: more work remains.'))
                self.assertEqual((changed['status'], changed.get('code')),
                                 ('error', 'assignment_already_returned'), changed)
                self.assertEqual(changed['message_seq'], report.seq)
                self.assertEqual(len(self.rt.team._messages(self.task)), before + 1)
                self.assertEqual(self.assert_single_return(first), report)

    def assert_rejected_without_outgoing_message(self, role, call):
        before = list(self.rt.team._messages(self.task))
        result, = self.f.exchange(role, call)
        self.assertEqual(result['status'], 'error', result)
        after = self.rt.team._messages(self.task)
        self.assertEqual(after[:len(before)], before)
        self.assertEqual(len(after), len(before) + 1)
        self.assertEqual(after[-1].actor.kind, 'human')
        self.assertTrue(self.rt.verify_chain().ok)

    def test_parent_child_recipient_mismatch_is_rejected(self):
        forged = self.rt.team._send(
            self.task, self.rt.team._role(self.f.manager), self.f.reviewer,
            'Mismatched continuation recipient', 'continuation',
            continue_assignment_seq=self.original.seq)
        self.assert_rejected_without_outgoing_message(self.f.reviewer, self.reply(forged.seq))
        self.assertIn(self.original.seq,
                      {row['seq'] for row in self.rt.team.recovery.assignments(self.task)})

    def test_parent_child_revision_mismatch_is_rejected(self):
        result, = self.f.exchange(self.f.manager, ('set_objective', {'goal': 'New target'}))
        self.assertEqual(result['status'], 'ok', result)
        forged = self.rt.team._send(
            self.task, self.rt.team._role(self.f.manager), self.f.worker,
            'Current continuation with an old parent', 'continuation',
            continue_assignment_seq=self.original.seq)
        self.assertEqual(forged.payload['revision'], self.rt.team.view(self.task)['revision'])
        self.assertNotEqual(forged.payload['revision'], self.original.payload['revision'])
        self.assert_rejected_without_outgoing_message(self.f.worker, self.reply(forged.seq))

    def test_new_target_rejects_direct_reply_to_old_original(self):
        result, = self.f.exchange(self.f.manager, ('set_objective', {'goal': 'New target'}))
        self.assertEqual(result['status'], 'ok', result)
        self.assert_rejected_without_outgoing_message(self.f.worker, self.reply(self.original.seq))

    def test_current_input_cannot_return_superseded_review_continuation(self):
        artifact = self.f.publish('Initial review version\n')
        old = self.f.request([artifact])
        self.assertEqual(old['status'], 'ok', old)
        source = self.message(old['message_seq'])
        run = self.rt.log.emit(type='team.run_started', actor=review_cycle.HOST,
            task_id=self.task, payload={
                'message_seq': source.seq, 'role': self.f.reviewer,
                'run_id': 'boundary-interrupted-review',
                'revision': self.rt.team.view(self.task)['revision'],
                'control_seq': self.rt.execution_control.view(self.task)['seq'],
            })
        with patch.object(self.rt.team, '_current', side_effect=WorkConflict('Fixture interruption')):
            list(self.rt.team._run_message(
                self.task, source, self.rt.team._role(self.f.reviewer), run))
        continued, = self.f.exchange(self.f.worker, ('send_message', {
            'recipient': self.f.reviewer, 'text': 'Resume the exact-version review.',
            'continue_assignment_seq': source.seq,
        }))
        self.assertEqual(continued['status'], 'ok', continued)
        artifact = self.f.publish('Revised review version\n')
        latest = self.f.request([artifact], review_seq=old['review_seq'])
        self.assertEqual(latest['status'], 'ok', latest)
        self.assertEqual(latest['root_seq'], old['root_seq'])
        self.assertNotEqual(latest['review_seq'], old['review_seq'])
        self.assertIn(continued['message_seq'],
                      self.rt.team_reviews.superseded_assignments(self.task))
        self.assert_rejected_without_outgoing_message(
            self.f.reviewer, self.reply(continued['message_seq'], recipient=self.f.worker))
        rows = self.rt.team.recovery.assignments(self.task)
        self.assertIn(latest['message_seq'], {row['seq'] for row in rows})
        review = next(row for row in self.rt.team_reviews.view(self.task)
                      if row['request_seq'] == latest['review_seq'])
        self.assertEqual(review['status'], 'pending')


if __name__ == '__main__':
    unittest.main()
