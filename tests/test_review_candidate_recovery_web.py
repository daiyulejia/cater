"""Web startup must not dispatch an explicitly claimed-only review candidate."""
import unittest
from unittest.mock import patch

from cater.review_supervision_candidate import create
from cater.runtime import Runtime
from cater.web import ActivityRunner
from cater.work import WorkConflict
import test_review_supervision_candidate as fixture_module


class ReviewCandidateRecoveryWebTests(unittest.TestCase):
    def setUp(self):
        helper = fixture_module.ReviewSupervisionCandidateTests(methodName='runTest')
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        self.f, run = helper.ready()
        self.candidate = create(self.f.rt.team, self.f.task, run_seq=run.seq, run_hash=run.hash)
        self.assertIsNotNone(self.candidate)

    def factory(self):
        return Runtime(self.f.rt.log.path, roles_path=self.f.roles,
            drivers={self.f.driver.name: self.f.driver}, close_drivers_on_close=False)

    def test_real_web_recovery_and_restart_leave_candidate_unqueued(self):
        calls = len(self.f.driver.runs)
        for _ in range(2):
            runner = ActivityRunner(self.factory)
            with patch.object(runner, '_start') as start:
                runner.recover_interrupted()
                start.assert_not_called()
                self.assertFalse(runner.status(self.f.task)['active'])
            with self.factory() as runtime:
                self.assertEqual(runtime.team.recovery.prepare(self.f.task), [])
                self.assertEqual(runtime.team.view(self.f.task)['pending_count'], 0)
                self.assertTrue(runtime.team.recovery.authenticate_review_candidate(self.f.task, self.candidate))
                self.assertFalse(runtime.team.recovery.is_message(self.candidate))
                with self.assertRaises(WorkConflict):
                    ActivityRunner._team_message(runtime, self.f.task, {'team_message_seq': self.candidate.seq})
                self.assertFalse([e for e in runtime.log.read(task_id=self.f.task)
                    if e.type == 'activity.queued' and e.payload.get('team_message_seq') == self.candidate.seq])
        self.assertEqual(len(self.f.driver.runs), calls)

    def test_normal_orphan_recovery_still_queues_while_candidate_stays_inert(self):
        with self.factory() as runtime:
            task = runtime.create_task('Normal recovery control')
            runtime.team.enable(task)
            runtime.team.receive(task, 'Process this request', request_id='normal-orphan-case')
            runtime.team._claim(task)
        runner = ActivityRunner(self.factory)
        with patch.object(runner, '_start') as start:
            runner.recover_interrupted()
            self.assertEqual(start.call_count, 1)
            self.assertEqual(runner.status(task)['state'], 'queued')
            self.assertFalse(runner.status(self.f.task)['active'])
        with self.factory() as runtime:
            notifications = runtime.team.recovery.prepare(task)
            self.assertEqual(len(notifications), 1)
            self.assertTrue(runtime.team.recovery.is_message(notifications[0]))
            self.assertEqual(runtime.team.recovery.prepare(self.f.task), [])
            self.assertEqual(runtime.team.view(self.f.task)['pending_count'], 0)
