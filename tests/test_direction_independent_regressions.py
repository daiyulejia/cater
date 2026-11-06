"""Independent offline reproductions of direction-switch review findings.

Every fixture owns a temporary database. The historical delivery cases use
real EventLog writes in the old event order; they do not run a model or replay
the historical finish_work operation.
"""

import unittest

from cater.events import Actor
from cater.team import HOST, LOCAL_EVENT_CONTRACT
from cater.team_recovery import _unsettled_delivery
import test_simple_team as simple
import test_team_review_cycle as review


class IndependentDirectionRegressions(unittest.TestCase):
    def fixture(self, *, reviews=False):
        cls = review.TeamReviewCycleTests if reviews else simple.SimpleTeamTests
        f = cls(methodName='runTest')
        f.setUp()
        self.addCleanup(f.doCleanups)
        return f

    def legacy_run(self, f, text):
        source = f.rt.team.receive(f.task, text)
        role = f.rt.organization.leader
        run = f.rt.log.emit(type='team.run_started', task_id=f.task, actor=HOST,
            payload={'message_seq': source.seq, 'role': role.id, 'run_id': 'legacy-run',
                     'revision': f.rt.team.view(f.task)['revision'],
                     'control_seq': f.rt.execution_control.view(f.task)['seq']})
        return source, role, run

    def test_01_new_direction_does_not_resume_orphaned_old_run(self):
        f = self.fixture()
        original = f.rt.team.receive(f.task, 'Original request')
        source, _, started = f.rt.team._claim(f.task)
        self.assertEqual(source.seq, original.seq)
        direction = f.rt.command(f.task, 'Change direction', goal='New objective')
        self.assertLess(started.seq, direction.seq)

        # Simulate an abandoned owner: prepare is the real restart recovery
        # entry, and no old model call or tool action is invoked by the test.
        pending = f.rt.team.recovery.prepare(f.task)
        old_notices = [e for e in pending if e.payload.get('recovery_of') == started.seq]
        claimed = f.rt.team._claim(f.task)
        self.assertEqual(old_notices, [], 'New direction must not enqueue recovery of the old run')
        self.assertFalse(claimed and claimed[0].payload.get('recovery_of') == started.seq,
                         'New direction must not claim recovery of the old run')

    def test_existing_recovery_is_retired_only_after_direction_change(self):
        f = self.fixture()
        f.rt.team.receive(f.task, 'Original request')
        _, _, started = f.rt.team._claim(f.task)
        notice, = f.rt.team.recovery.prepare(f.task)
        self.assertTrue(f.rt.team.recovery.current(f.task, notice))
        f.rt.command(f.task, 'Change direction', goal='New objective')
        self.assertFalse(f.rt.team.recovery.current(f.task, notice))
        self.assertIsNone(f.rt.team.recovery.run_scope(f.task, started))
        self.assertNotIn(notice, f.rt.team.recovery.prepare(f.task))
        claimed = f.rt.team._claim(f.task)
        self.assertFalse(claimed and claimed[0].seq == notice.seq)

    def test_02_fresh_review_after_previous_delivery_is_current(self):
        f = self.fixture(reviews=True)
        self.assertEqual(f.finish()['status'], 'ok')
        previous, = f.events('team.objective_completed')
        direction = f.rt.command(f.task, 'Start different work', goal='New objective')
        artifact = f.publish(content='New objective fixture')
        requested = f.request([artifact])
        self.assertEqual(requested['status'], 'ok')
        row, = f.rt.team_reviews.view(f.task)
        self.assertLess(previous.seq, direction.seq)
        self.assertLess(direction.seq, row['request_seq'])
        self.assertEqual(row['status'], 'pending', 'A newly submitted review must not inherit stale status')
        self.assertEqual(f.record(requested['review_seq'])['status'], 'ok')

    def test_03_legacy_delivery_before_and_after_direction(self):
        for order in ('direction_first', 'delivery_first'):
            with self.subTest(order=order):
                f = self.fixture()
                source, role, run = self.legacy_run(f, 'Deliver the original work')
                args = {'summary': 'Original delivery', 'verification': 'Offline fixture'}
                tool = f.rt.log.emit(type='team.tool_started', task_id=f.task, actor=HOST,
                    payload={'run_id': run.payload['run_id'], 'role': role.id, 'message_seq': source.seq,
                             'name': 'finish_work', 'call_id': 'legacy-finish', 'revision': run.payload['revision'],
                             'execution_contract': dict(LOCAL_EVENT_CONTRACT), 'arguments': args})
                if order == 'direction_first':
                    direction = f.rt.command(f.task, 'Change direction', goal='New objective')
                boundary = max(e.seq for e in f.rt.log.read(task_id=f.task) if e.actor == HOST
                               and e.type in {'team.started', 'team.objective_updated'})
                delivery = f.rt.log.emit(type='team.objective_completed', task_id=f.task, actor=HOST,
                    payload={'run_id': run.payload['run_id'], 'objective_seq': boundary, **args})
                receipt = f.rt.log.emit(type='team.tool_finished', task_id=f.task, actor=HOST,
                    payload={'started_seq': tool.seq, 'name': 'finish_work', 'role': role.id,
                             'run_id': run.payload['run_id'], 'result': {'status': 'ok'}})
                if order == 'delivery_first':
                    direction = f.rt.command(f.task, 'Change direction', goal='New objective')
                before = f.rt.log.read(task_id=f.task)
                eligible = _unsettled_delivery(f.rt.team, f.task, run, before)
                f.rt.team.recovery.prepare(f.task)
                replies = [e for e in f.events('msg.said') if e.payload.get('delivery_seq') == delivery.seq]
                if order == 'direction_first':
                    self.assertTrue(tool.seq < direction.seq < delivery.seq < receipt.seq)
                    self.assertIsNone(eligible, 'Old delivery must not settle after a prior direction change')
                    self.assertEqual(replies, [])
                else:
                    self.assertTrue(tool.seq < delivery.seq < receipt.seq < direction.seq)
                    self.assertEqual(eligible, delivery)
                    self.assertEqual(len(replies), 1)
                    f.rt.team.recovery.prepare(f.task)
                    self.assertEqual(len([e for e in f.events('msg.said')
                                          if e.payload.get('delivery_seq') == delivery.seq]), 1)

    def test_04_old_approval_does_not_block_unrelated_new_delivery(self):
        f = self.fixture(reviews=True)
        artifact = f.publish()
        requested = f.request([artifact])
        f.record(requested['review_seq'])
        f.acknowledge_latest_review()
        direction = f.rt.command(f.task, 'Start unrelated work', goal='Unrelated new objective')
        result = f.finish()
        self.assertEqual(result['status'], 'ok', result)
        completed, = [e for e in f.events('team.objective_completed') if e.seq > direction.seq]
        self.assertNotIn(artifact, [a['seq'] for a in completed.payload.get('artifacts', [])])

    def test_05_late_legacy_objective_events_do_not_override_new_task_projection(self):
        for event_type in ('team.objective_updated', 'team.objective_completed'):
            with self.subTest(event_type=event_type):
                f = self.fixture()
                _, _, run = self.legacy_run(f, 'Original work')
                direction = f.rt.command(f.task, 'Change direction', goal='New objective')
                payload = ({'run_id': run.payload['run_id'], 'goal': 'Old objective'}
                           if event_type == 'team.objective_updated' else
                           {'run_id': run.payload['run_id'], 'summary': 'Old delivery', 'verification': 'Fixture'})
                late = f.rt.log.emit(type=event_type, task_id=f.task, actor=Actor.system('team'), payload=payload)
                self.assertTrue(run.seq < direction.seq < late.seq)
                task, team = f.rt.task(f.task), f.rt.team.view(f.task)
                self.assertEqual((task.goal, team['objective']), ('New objective', 'New objective'))
                self.assertEqual((task.status, team['status']), ('running', 'running'))
                self.assertFalse(f.rt.team.recovery.scope(f.task)['done'])
                if event_type == 'team.objective_updated':
                    from cater.team_customer_sources import index
                    self.assertEqual(index(f.rt.log.read(task_id=f.task))['objective_source']['event_seq'], direction.seq)

    def test_unattributed_or_ambiguous_late_completion_does_not_finish_new_direction(self):
        for kind in ('missing', 'unknown', 'duplicate', 'missing_source'):
            with self.subTest(kind=kind):
                f = self.fixture()
                _, _, run = self.legacy_run(f, 'Original work')
                if kind == 'duplicate':
                    f.rt.log.emit(type='team.run_started', task_id=f.task, actor=HOST, payload=dict(run.payload))
                if kind == 'missing_source':
                    f.rt.log.emit(type='team.run_started', task_id=f.task, actor=HOST,
                                  payload={**run.payload, 'run_id': 'orphan-run', 'message_seq': 999999})
                f.rt.command(f.task, 'Change direction', goal='New objective')
                payload = {'summary': 'Old delivery', 'verification': 'Fixture'}
                if kind != 'missing':
                    payload['run_id'] = (run.payload['run_id'] if kind == 'duplicate' else
                                         'orphan-run' if kind == 'missing_source' else 'unknown-run')
                f.rt.log.emit(type='team.objective_completed', task_id=f.task, actor=HOST, payload=payload)
                self.assertEqual(f.rt.task(f.task).status, 'running')
                self.assertEqual(f.rt.team.view(f.task)['status'], 'running')
                self.assertFalse(f.rt.team.recovery.scope(f.task)['done'])

    def test_late_old_update_does_not_stale_new_review_or_revision(self):
        f = self.fixture(reviews=True)
        f.rt.team.receive(f.task, 'Original work')
        _, _, old = f.rt.team._claim(f.task)
        f.rt.command(f.task, 'Change direction', goal='New objective')
        artifact = f.publish(content='New objective fixture')
        request = f.request([artifact])
        before = f.rt.team.view(f.task)['revision']
        f.rt.log.emit(type='team.objective_updated', task_id=f.task, actor=HOST,
                      payload={'run_id': old.payload['run_id'], 'goal': 'Old objective'})
        self.assertEqual(f.rt.team.view(f.task)['revision'], before)
        self.assertEqual(f.rt.team.recovery.scope(f.task)['revision'], before)
        self.assertEqual(f.rt.team_reviews.view(f.task)[-1]['status'], 'pending')
        self.assertEqual(f.record(request['review_seq'])['status'], 'ok')

    def test_unattributed_legacy_completion_before_switch_remains_visible(self):
        f = self.fixture()
        f.rt.log.emit(type='team.objective_completed', task_id=f.task, actor=HOST,
                      payload={'summary': 'Legacy delivery', 'verification': 'Fixture'})
        self.assertEqual(f.rt.task(f.task).status, 'done')
        self.assertEqual(f.rt.team.view(f.task)['status'], 'done')
        f.rt.command(f.task, 'New direction', goal='New objective')
        self.assertEqual(f.rt.team.view(f.task)['status'], 'running')


if __name__ == '__main__':
    unittest.main()
