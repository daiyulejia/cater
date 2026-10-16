"""Waiting in a native tool batch must not swallow an actionable failure."""

import unittest

from test_team_supervision import TeamSupervisionTests


class TeamToolBatchingTests(TeamSupervisionTests):
    def test_failed_delegation_and_wait_allow_correction_in_the_same_run(self):
        self.driver.steps[self.manager] = [
            self.native_turn('', ('delegate', {'recipient': self.manager, 'instruction': 'Invalid self-delegation'}),
                             ('wait_for_reply', {})),
            self.native_turn('The first delivery failed; correcting the recipient.',
                             ('delegate', {'recipient': self.worker, 'instruction': 'Check this bounded task'}),
                             ('wait_for_reply', {})),
            self.native_turn('Received the actual result.'),
        ]
        self.driver.steps[self.worker] = [self.native_turn('Checked the task.')]
        self.rt.team.receive(self.task, 'Arrange the check', request_id='batch-repair-delegate')
        self.run_team()
        results = self.results('delegate')
        self.assertEqual([r['status'] for r in results], ['error', 'ok'])
        self.assertEqual(len(self.events('team.run_started')), 3)
        self.assertEqual(self.rt.team.view(self.task)['pending_count'], 0)
        self.assertGreater(self.validated_continuations, 0)

    def test_failed_wait_itself_does_not_silently_stop_the_run(self):
        self.driver.steps[self.manager] = [
            self.native_turn('', ('wait_for_reply', {'unexpected': True})),
            self.native_turn('I can still answer after correcting the failed tool call.'),
        ]
        self.rt.team.receive(self.task, 'Check failed waiting', request_id='batch-invalid-wait')
        self.run_team()
        self.assertEqual(self.results('wait_for_reply')[0]['status'], 'error')
        self.assertEqual(len(self.driver.calls), 2)
        self.assertEqual(self.events('team.run_finished')[0].payload['status'], 'completed')

    def test_successful_batched_handoff_needs_no_extra_waiting_call(self):
        self.driver.steps[self.manager] = [
            self.native_turn('', ('delegate', {'recipient': self.worker, 'instruction': 'Check once'}),
                             ('wait_for_reply', {})),
            self.native_turn('Received the result.'),
        ]
        self.driver.steps[self.worker] = [self.native_turn('Check completed.')]
        self.rt.team.receive(self.task, 'A small check', request_id='batch-successful-wait')
        self.run_team()
        self.assertEqual(len(self.driver.calls), 3)
        self.assertEqual([e.payload['status'] for e in self.events('team.run_finished')],
                         ['waiting', 'completed', 'completed'])

    def test_successful_sibling_is_not_replayed_when_a_failed_batch_continues(self):
        self.driver.steps[self.manager] = [
            self.native_turn('', ('publish_artifact', {'filename': 'note.txt', 'content': 'One saved result',
                             'summary': 'Saved once', 'format': 'text'}),
                             ('read_receipts', {'run_id': 'not-a-run'}), ('wait_for_reply', {})),
            self.native_turn('The file was saved; only the receipt lookup failed.'),
        ]
        self.rt.team.receive(self.task, 'Save once and inspect', request_id='batch-preserve-success')
        self.run_team()
        self.assertEqual(len(self.events('deliverable.published')), 1)
        self.assertEqual(len(self.results('publish_artifact')), 1)
        self.assertEqual(self.results('read_receipts')[0]['status'], 'error')
        self.assertEqual(len(self.driver.calls), 2)


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(TeamToolBatchingTests(name) for name in TeamToolBatchingTests.__dict__ if name.startswith('test_'))
