import unittest

from cater.team import _reported_messages
import test_team_supervision


class ExplicitReturnTests(unittest.TestCase):
    def setUp(self):
        self.f = test_team_supervision.TeamSupervisionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def test_normal_end_wakes_parent_for_continuation_and_explicit_return(self):
        f = self.f

        def resume(context, history, tools):
            message = context['message']
            self.assertEqual(message['kind'], 'progress')
            self.assertIn('尚未正式交回', message['text'])
            assignment, = f.rt.team.recovery.assignments(f.task)
            self.assertTrue(assignment['requires_return'])
            self.assertNotIn(assignment['seq'], _reported_messages(f.rt.team._messages(f.task)))
            return f.native_turn('', ('continue_work', {
                'assignment_seq': assignment['seq'],
                'instruction': 'Finish the work and explicitly return the result.',
            }), ('wait_for_reply', {}))

        f.driver.steps = {
            f.manager: [f.native_turn('', ('delegate', {'recipient': f.worker, 'instruction': 'Do work'}),
                                      ('wait_for_reply', {})), resume, f.native_turn('Received the result.')],
            f.worker: [f.native_turn('Some progress only.'),
                       f.native_turn('', ('return_work', {'text': 'Completed result and limitations.'}))],
        }
        f.rt.team.receive(f.task, 'Do work', request_id='explicit-progress')
        f.run_team()
        messages = f.rt.team._messages(f.task)
        assignment, = [e for e in messages if e.payload.get('kind') == 'assignment']
        progress, = [e for e in messages if e.payload.get('kind') == 'progress']
        self.assertEqual(progress.payload['reply_to'], assignment.seq)
        continuation, = [e for e in messages if e.payload.get('kind') == 'continuation']
        self.assertEqual(continuation.payload['continue_assignment_seq'], assignment.seq)
        report, = [e for e in messages if e.payload.get('kind') == 'report']
        self.assertEqual(report.payload['reply_to'], assignment.seq)
        self.assertEqual(report.payload['text'], 'Completed result and limitations.')
        self.assertIn(assignment.seq, _reported_messages(messages))
        self.assertEqual(f.rt.team.recovery.assignments(f.task), [])
        self.assertEqual([c['context']['message']['kind'] for c in f.driver.calls
                          if c['context']['you']['id'] == f.manager], ['message', 'progress', 'report'])
        self.assertEqual([c['context']['message']['kind'] for c in f.driver.calls
                          if c['context']['you']['id'] == f.worker], ['assignment', 'continuation'])
        self.assertEqual(len(f.driver.calls), 5)
        self.assertEqual(f.rt.team.view(f.task)['pending_count'], 0)
        self.assertIsNone(f.rt.team._claim(f.task))
        f.run_team()
        self.assertEqual(len(f.driver.calls), 5, 'A settled assignment must not restart or loop.')

    def test_explicit_return_wakes_parent(self):
        f = self.f
        f.driver.steps = {
            f.manager: [f.native_turn('', ('delegate', {'recipient': f.worker, 'instruction': 'Do work'}), ('wait_for_reply', {})), f.native_turn('Received')],
            f.worker: [f.native_turn('', ('return_work', {'text': 'Result and limitations'}))],
        }
        f.rt.team.receive(f.task, 'Do work', request_id='explicit-return')
        f.run_team()
        messages = f.rt.team._messages(f.task)
        assignment, = [e for e in messages if e.payload.get('kind') == 'assignment']
        self.assertIn(assignment.seq, _reported_messages(messages))
        self.assertEqual(len(f.driver.calls), 3)
        self.assertEqual(f.rt.team.recovery.assignments(f.task), [])
