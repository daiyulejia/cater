import unittest

import test_team_supervision
import test_team_review_cycle


class FinishRelatedAnswerGateTests(unittest.TestCase):
    def setUp(self):
        self.f = test_team_supervision.TeamSupervisionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.helper = next(r.id for r in self.f.rt.worker_roles if r.id != self.f.worker)

    def run_case(self, *, plain_message=False, extra_work=False):
        f = self.f
        initial = [('delegate', {'recipient': f.worker, 'instruction': 'Find and return the answer.'})]
        if extra_work:
            initial.append(('delegate', {'recipient': self.helper, 'instruction': 'Complete separate work.'}))
        initial.append(('wait_for_reply', {}))
        f.driver.steps = {
            f.manager: [f.native_turn('', *initial),
                        f.native_turn('', ('finish_work', {'summary': 'Answer received.', 'verification': 'Fixture only.'})),
                        f.native_turn('', ('wait_for_reply', {})),
                        f.native_turn('', ('wait_for_reply', {}))],
            f.worker: [f.native_turn('', ('send_message', {'recipient': f.manager, 'text': 'What should I do next?'}),
                                    ('wait_for_reply', {})) if plain_message else
                       f.native_turn('', ('return_work', {'text': 'Answer complete.'}))],
        }
        if extra_work:
            f.driver.steps[self.helper] = [f.native_turn('', ('wait_for_reply', {}))]
        f.rt.team.receive(f.task, 'Find the answer', request_id='related-answer-gate')
        f.run_team()
        result, = f.results('finish_work')
        finish_start, = [e for e in f.events('team.tool_started') if e.payload['name'] == 'finish_work']
        consumed = next(e for e in f.rt.team._messages(f.task)
                        if e.seq == finish_start.payload['message_seq'])
        self.assertEqual(consumed.payload['kind'], 'message' if plain_message else 'report')
        return result

    def test_explicit_answer_can_be_consumed_and_delivered(self):
        result = self.run_case()
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(self.f.rt.team.view(self.f.task)['status'], 'done')

    def test_plain_related_message_does_not_close_original_work(self):
        result = self.run_case(plain_message=True)
        self.assertEqual(result['code'], 'work_pending')
        self.assertNotEqual(self.f.rt.team.view(self.f.task)['status'], 'done')

    def test_explicit_answer_does_not_hide_other_unfinished_work(self):
        result = self.run_case(extra_work=True)
        self.assertEqual(result['code'], 'work_pending')
        self.assertNotEqual(self.f.rt.team.view(self.f.task)['status'], 'done')


class FinishReviewPriorityTests(unittest.TestCase):
    def setUp(self):
        self.f = test_team_review_cycle.TeamReviewCycleTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def test_review_then_other_unreturned_work_both_block_delivery(self):
        f = self.f
        review = f.request([f.publish()])
        delegated, = f.exchange(f.manager, ('delegate', {
            'recipient': f.observer, 'instruction': 'Check the remaining work.'}))
        result = f.finish()
        self.assertEqual(result['code'], 'review_pending')
        self.assertTrue(result['reviews'])
        self.assertFalse(f.events('team.objective_completed'))
        self.assertEqual(f.record(review['review_seq'])['status'], 'ok')
        f.acknowledge_latest_review()
        result = f.finish()
        self.assertEqual(result['code'], 'work_pending')
        self.assertIn(delegated['message_seq'], [row['seq'] for row in result['assignments']])
        self.assertFalse(f.events('team.objective_completed'))


if __name__ == '__main__':
    unittest.main()
