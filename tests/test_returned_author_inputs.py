"""A real return settles queued work, not every input addressed to its author."""

import unittest

import test_team_review_cycle as reviews


class ReturnedAuthorInputsTests(unittest.TestCase):
    def setUp(self):
        self.f = reviews.TeamReviewCycleTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.rt, self.task = self.f.rt, self.f.task
        self.artifact = self.f.publish('Actual published result')

    def message(self, sender, recipient, text, kind='message', **extra):
        return self.rt.team._send(self.task, self.rt.team._role(sender), recipient,
                                  text, kind, **extra)

    def test_side_reply_settles_an_author_request_without_starting_that_input(self):
        source = self.message(self.f.manager, self.f.worker, 'Return the checked result.')
        result, = self.f.exchange(self.f.worker, ('send_message', {
            'recipient': self.f.manager, 'reply_to': source.seq, 'text': 'Checked result returned.'}))
        self.assertEqual(result['status'], 'ok', result)
        self.assertNotIn(source.seq, {row['message_seq'] for row in self.rt.team.view(self.task)['runs']})
        self.assertNotIn(source.seq, {row['seq'] for row in self.rt.team.recovery.assignments(self.task)})
        finished = self.f.finish()
        self.assertEqual(finished['status'], 'ok', finished)
        self.assertEqual(self.rt.team.view(self.task)['status'], 'done')
        self.assertTrue(self.rt.verify_chain().ok)

    def test_a_return_does_not_settle_another_queued_request_to_same_author(self):
        source = self.message(self.f.manager, self.f.worker, 'First request.')
        other = self.message(self.f.manager, self.f.worker, 'Different unfinished request.')
        self.f.exchange(self.f.worker, ('send_message', {
            'recipient': self.f.manager, 'reply_to': source.seq, 'text': 'First request returned.'}))
        finished = self.f.finish()
        self.assertEqual(finished['code'], 'author_pending')
        self.assertEqual({row['seq'] for row in finished['assignments']}, {other.seq})

    def test_queued_review_feedback_still_requires_author_attention(self):
        source = self.message(self.f.manager, self.f.worker, 'Return the result.')
        self.f.exchange(self.f.worker, ('send_message', {
            'recipient': self.f.manager, 'reply_to': source.seq, 'text': 'Returned.'}))
        feedback = self.message(self.f.reviewer, self.f.worker, 'A review issue remains.',
                                'report', reply_to=source.seq)
        finished = self.f.finish()
        self.assertEqual(finished['code'], 'author_pending')
        self.assertIn(feedback.seq, {row['seq'] for row in finished['assignments']})


if __name__ == '__main__':
    unittest.main()
