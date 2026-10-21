"""A reply to a valid continuation returns its original work, without a new job."""

import unittest
from unittest.mock import patch

from cater.work import WorkConflict
import test_team_review_cycle as reviews


class ContinuationReturnTests(unittest.TestCase):
    def setUp(self):
        self.f = reviews.TeamReviewCycleTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.rt, self.task = self.f.rt, self.f.task
        sent, = self.f.exchange(self.f.manager, ('delegate', {
            'recipient': self.f.worker, 'instruction': 'Prepare a checked result.'}))
        self.original = next(e for e in self.rt.team._messages(self.task) if e.seq == sent['message_seq'])
        run = self.rt.log.emit(type='team.run_started', actor=reviews.HOST, task_id=self.task, payload={
            'run_id': 'interrupted-worker', 'message_seq': self.original.seq,
            'role': self.f.worker, 'revision': 0, 'control_seq': None})
        with patch.object(self.rt.team, '_current', side_effect=WorkConflict('Fixture interruption')):
            list(self.rt.team._run_message(self.task, self.original, self.rt.team._role(self.f.worker), run))
        continued, = self.f.exchange(self.f.manager, ('send_message', {
            'recipient': self.f.worker, 'text': 'Inspect what exists, then finish.',
            'continue_assignment_seq': self.original.seq}))
        self.continuation = next(e for e in self.rt.team._messages(self.task) if e.seq == continued['message_seq'])

    def test_reply_to_current_continuation_returns_original_once(self):
        result, = self.f.exchange(self.f.worker, ('send_message', {
            'recipient': self.f.manager, 'reply_to': self.continuation.seq,
            'text': 'Checked and completed.'}), source=self.continuation)
        self.assertEqual(result['status'], 'ok', result)
        reports = [e for e in self.rt.team._messages(self.task) if e.payload.get('kind') == 'report'
                   and e.actor.id == self.f.worker]
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0].payload['reply_to'], self.original.seq)
        self.assertEqual(result['resolved_reply_to'], self.original.seq)
        self.assertNotIn(self.original.seq, {r['seq'] for r in self.rt.team.recovery.assignments(self.task)})
        self.assertTrue(self.rt.verify_chain().ok)

    def test_wrong_original_sender_cannot_be_used_to_close_work(self):
        forged = self.rt.team._send(self.task, self.rt.team._role(self.f.reviewer), self.f.worker,
            'Not the original sender', 'continuation', continue_assignment_seq=self.original.seq)
        result, = self.f.exchange(self.f.worker, ('send_message', {
            'recipient': self.f.reviewer, 'reply_to': forged.seq, 'text': 'Attempted return'}))
        self.assertEqual(result['status'], 'error')
        self.assertIn(self.original.seq, {r['seq'] for r in self.rt.team.recovery.assignments(self.task)})

    def test_other_recipient_cannot_return_someone_elses_continuation(self):
        result, = self.f.exchange(self.f.reviewer, ('send_message', {
            'recipient': self.f.manager, 'reply_to': self.continuation.seq, 'text': 'Not my work'}))
        self.assertEqual(result['status'], 'error')
        self.assertIn(self.original.seq, {r['seq'] for r in self.rt.team.recovery.assignments(self.task)})

    def test_reply_to_original_is_still_supported(self):
        result, = self.f.exchange(self.f.worker, ('send_message', {
            'recipient': self.f.manager, 'reply_to': self.original.seq, 'text': 'Original returned.'}),
            source=self.continuation)
        self.assertEqual(result['status'], 'ok', result)
        self.assertNotIn('resolved_reply_to', result)
        self.assertNotIn(self.original.seq, {r['seq'] for r in self.rt.team.recovery.assignments(self.task)})

    def test_other_group_continuation_is_not_a_local_reply_target(self):
        other = self.rt.create_task('Separate group')
        self.rt.team.enable(other)
        parent = self.rt.team._send(other, self.rt.team._role(self.f.manager), self.f.worker,
            'Other project work', 'assignment')
        continuing = self.rt.team._send(other, self.rt.team._role(self.f.manager), self.f.worker,
            'Other project continuation', 'continuation', continue_assignment_seq=parent.seq)
        result, = self.f.exchange(self.f.worker, ('send_message', {
            'recipient': self.f.manager, 'reply_to': continuing.seq, 'text': 'Attempted cross-group return'}))
        self.assertEqual(result['status'], 'error')
        self.assertIn(self.original.seq, {r['seq'] for r in self.rt.team.recovery.assignments(self.task)})

    def test_new_goal_rejects_reply_to_old_continuation(self):
        result, = self.f.exchange(self.f.manager, ('set_objective', {'goal': 'A new independent goal'}))
        self.assertEqual(result['status'], 'ok')
        result, = self.f.exchange(self.f.worker, ('send_message', {
            'recipient': self.f.manager, 'reply_to': self.continuation.seq, 'text': 'Old work return'}))
        self.assertEqual(result['status'], 'error')
        reports = [e for e in self.rt.team._messages(self.task) if e.payload.get('kind') == 'report']
        self.assertEqual(reports, [])

    def test_outgoing_reply_error_identifies_actual_sender_and_recipient(self):
        outgoing = self.rt.team._send(self.task, self.rt.team._role(self.f.worker), self.f.manager,
            'Please inspect my result.', 'message')
        before = len(self.rt.team._messages(self.task))
        result, = self.f.exchange(self.f.worker, ('send_message', {
            'recipient': self.f.manager, 'reply_to': outgoing.seq, 'text': 'Attempt to answer my own request.'}))
        self.assertEqual((result['status'], result['code'], result['outcome']),
                         ('error', 'reply_direction_mismatch', 'not_started'))
        self.assertEqual((result['you'], result['original_sender'], result['original_recipient']),
                         (self.f.worker, self.f.worker, self.f.manager))
        self.assertIn('continue_assignment_seq', result['message'])
        self.assertEqual(len(self.rt.team._messages(self.task)), before + 1)
        self.assertFalse(self.rt.team._pending_operations(self.task))


if __name__ == '__main__':
    unittest.main()
