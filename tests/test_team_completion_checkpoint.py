"""Late retirement of old work must not replace an existing delivery summary."""

import unittest

from cater.team import HOST
from test_team_supervision import TeamSupervisionTests


class TeamCompletionCheckpointTests(TeamSupervisionTests):
    def start_old_run(self):
        source = self.rt.team._send(self.task, self.rt.organization.leader, self.worker,
                                    'Work before delivery', 'assignment')
        self.rt.log.emit(type='team.run_started', actor=HOST, task_id=self.task, payload={
            'message_seq': source.seq, 'role': self.worker, 'run_id': 'checkpoint-worker',
            'control_seq': self.rt.execution_control.view(self.task)['seq'],
            'revision': self.rt.team.view(self.task)['revision']})
        return source

    def complete(self):
        return self.rt.log.emit(type='team.objective_completed', actor=HOST, task_id=self.task, payload={
            'summary': 'Delivery is ready; five behavior tests passed.',
            'verification': 'Actual verification recorded.', 'run_id': 'checkpoint-manager'})

    def retire(self, source, status='interrupted'):
        self.rt.log.emit(type='team.run_finished', actor=HOST, task_id=self.task, payload={
            'message_seq': source.seq, 'role': self.worker, 'run_id': 'checkpoint-worker',
            'status': status, 'reason': 'Old work was retired after delivery.'})

    def test_old_run_retirement_does_not_erase_completed_checkpoint(self):
        for status in ('interrupted', 'failed'):
            with self.subTest(status=status):
                source = self.start_old_run()
                delivery = self.complete()
                self.retire(source, status)
                view = self.rt.team.view(self.task)
                self.assertEqual(view['status'], 'done')
                self.assertEqual(view['checkpoint'], delivery.payload['summary'])
                self.assertEqual(view['runs'][-1]['status'], status)
                self.assertEqual(view['runs'][-1]['reason'], 'Old work was retired after delivery.')

    def test_failure_before_delivery_is_replaced_by_actual_completion(self):
        source = self.start_old_run()
        self.retire(source, 'failed')
        self.assertEqual(self.rt.team.view(self.task)['checkpoint'], 'Old work was retired after delivery.')
        delivery = self.complete()
        self.assertEqual(self.rt.team.view(self.task)['checkpoint'], delivery.payload['summary'])

    def test_new_objective_still_shows_its_own_failure(self):
        self.complete()
        self.rt.log.emit(type='team.objective_updated', actor=HOST, task_id=self.task,
                         payload={'goal': 'A distinct next task'})
        source = self.start_old_run()
        self.retire(source, 'failed')
        view = self.rt.team.view(self.task)
        self.assertEqual(view['status'], 'running')
        self.assertEqual(view['objective'], 'A distinct next task')
        self.assertEqual(view['checkpoint'], 'Old work was retired after delivery.')


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(TeamCompletionCheckpointTests(name)
        for name in TeamCompletionCheckpointTests.__dict__ if name.startswith('test_'))
