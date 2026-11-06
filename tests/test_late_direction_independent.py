"""Independent late-direction regressions; all gateways use offline fixtures.

The native cases use actual localhost HTTP workers and the Qoder case uses the
real ACP/MCP transport with a local peer. No service or real model is called.
"""
import unittest
from dataclasses import replace
from unittest.mock import patch

from cater.anthropic import AnthropicMessagesDriver
from cater.drivers import OpenAICompatibleDriver
from cater.events import Actor
from cater.runtime import Runtime
from cater.team import HOST
from cater.tool_protocol import ToolCall
import test_native_review_supervision_auto as native_fixture
import test_review_supervision_acp as acp_fixture
import test_simple_team as simple
import test_team_review_cycle as review


class LateDirectionIndependentTests(unittest.TestCase):
    def test_review_recovery_rejects_forged_scope_and_barrier(self):
        from cater.team_review_direction import _recovery_source
        helper = acp_fixture.ReviewSupervisionAcpTests(methodName='runTest')
        self.addCleanup(helper.doCleanups)
        f, candidate, claimed = helper.ready()
        run = claimed[2]
        events = f.rt.log.read(task_id=f.task)
        self.assertTrue(_recovery_source(candidate, run, events))
        for field, value in [('original_run_hash', 'forged'), ('evidence', {}), ('scope', {})]:
            with self.subTest(field=field):
                forged = replace(candidate, payload={**candidate.payload, field: value})
                self.assertFalse(_recovery_source(forged, run, events))
        forged_run = replace(run, payload={**run.payload, 'customer_input_barrier': {}})
        self.assertFalse(_recovery_source(candidate, forged_run, events))
        duplicate = replace(candidate, seq=candidate.seq - 1)
        self.assertFalse(_recovery_source(candidate, run, events + [duplicate]))
    def simple_fixture(self, *, reviews=False):
        cls = review.TeamReviewCycleTests if reviews else simple.SimpleTeamTests
        f = cls(methodName='runTest')
        f.setUp()
        self.addCleanup(f.doCleanups)
        return f

    def assert_delivery(self, f, *, run_id=None):
        deliveries = [e for e in f.events('team.objective_completed')
                      if run_id is None or e.payload.get('run_id') == run_id]
        self.assertEqual(len(deliveries), 1)
        delivery = deliveries[0]
        starts = [e for e in f.events('team.tool_started') if e.payload.get('name') == 'finish_work'
                  and e.payload.get('run_id') == delivery.payload.get('run_id')]
        self.assertEqual(len(starts), 1)
        receipts = [e for e in f.events('team.tool_finished') if e.payload.get('started_seq') == starts[0].seq]
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0].payload['result']['status'], 'ok')
        replies = [e for e in f.events('msg.said') if e.payload.get('delivery_seq') == delivery.seq]
        self.assertEqual(len(replies), 1, 'Committed delivery requires exactly one customer reply')
        self.assertEqual(replies[0].payload.get('audience'), 'customer')
        self.assertEqual(f.rt.team.view(f.task)['status'], 'done')
        self.assertEqual(f.rt.task(f.task).status, 'done')
        self.assertTrue(f.rt.team.recovery.scope(f.task)['done'])
        return delivery

    def test_native_supervision_delivery_survives_restart(self):
        for cls in (OpenAICompatibleDriver, AnthropicMessagesDriver):
            with self.subTest(protocol=cls.__name__):
                helper = native_fixture.NativeAutomaticReviewSupervisionTests(methodName='runTest')
                self.addCleanup(helper.doCleanups)
                f, requests = helper.run_case(cls, 'finish')
                self.assertEqual(len(requests), 2)
                delivery = self.assert_delivery(f)
                # Reopening and recovery must not replay or duplicate the answer.
                with Runtime(f.rt.log.path, roles_path=f.roles, drivers={f.driver.name: f.driver},
                             close_drivers_on_close=False) as reopened:
                    reopened.team.recovery.prepare(f.task)
                    reopened.team.recovery.prepare(f.task)
                    self.assertEqual(reopened.team.view(f.task)['status'], 'done')
                    replies = [e for e in reopened.log.read(task_id=f.task)
                               if e.type == 'msg.said' and e.payload.get('delivery_seq') == delivery.seq]
                    self.assertEqual(len(replies), 1)
                self.assertEqual(len(requests), 2)

    def test_qoder_supervision_delivery_is_acknowledged_once(self):
        helper = acp_fixture.ReviewSupervisionAcpTests(methodName='runTest')
        self.addCleanup(helper.doCleanups)
        f, candidate, _ = helper.ready(claim=False)
        driver, session, sessions = helper.driver_and_session(f, 'finish')
        f.rt.drivers[driver.name] = driver
        with f.rt.team.recovery.review_candidate_lease(f.task, candidate_seq=candidate.seq,
                                                        candidate_hash=candidate.hash) as claimed:
            self.assertIsNotNone(claimed)
            run_id = claimed[2].payload['run_id']
            with patch('cater.external_agent.AcpSession', session):
                list(f.rt.team._run_message(f.task, *claimed))
        self.assertEqual(len(sessions), 1)
        # An orphaned committed delivery must be settled once, not rerun.
        with Runtime(f.rt.log.path, roles_path=f.roles, drivers={f.driver.name: f.driver},
                     close_drivers_on_close=False) as reopened:
            reopened.team.recovery.prepare(f.task)
            reopened.team.recovery.prepare(f.task)
        self.assert_delivery(f, run_id=run_id)
        self.assertEqual(len(sessions), 1)

    def test_normal_recovery_goal_update_is_effective(self):
        f = self.simple_fixture()
        f.rt.team.receive(f.task, 'Original work')
        f.rt.team._claim(f.task)
        notices = f.rt.team.recovery.prepare(f.task)
        self.assertEqual(len(notices), 1)
        source, role, run = f.rt.team._claim(f.task)
        self.assertEqual(source.seq, notices[0].seq)
        context, workspace, binding, tools, _ = f.rt.team._model_input(f.task, source, role,
                                                                       run_id=run.payload['run_id'])
        _, result = f.rt.team._dispatch_tool(f.task, source, role, run, run.payload['revision'],
            ToolCall('recover-objective', 'set_objective', {'goal': 'Recovered objective'}),
            tools, workspace, binding, context, None)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(f.rt.team.view(f.task)['objective'], 'Recovered objective')
        self.assertEqual(f.rt.task(f.task).goal, 'Recovered objective')
        self.assertEqual(f.rt.team.view(f.task)['revision'], result['revision'])

    def test_default_delivery_read_ignores_late_old_completion(self):
        f = self.simple_fixture(reviews=True)
        source = f.rt.team.receive(f.task, 'Old request')
        _, _, old = f.rt.team._claim(f.task)
        f.rt.log.emit(type='team.run_finished', task_id=f.task, actor=HOST,
            payload={'message_seq': source.seq, 'role': old.payload['role'],
                     'run_id': old.payload['run_id'], 'status': 'interrupted'})
        f.rt.command(f.task, 'Change direction', goal='New objective')
        self.assertEqual(f.finish()['status'], 'ok')
        current = f.rt.team.view(f.task)['revision']
        late = f.rt.log.emit(type='team.objective_completed', task_id=f.task, actor=HOST,
            payload={'run_id': old.payload['run_id'], 'summary': 'Old historical fixture', 'verification': 'Fixture'})
        self.assertGreater(late.seq, current)
        self.assertEqual(f.rt.team.view(f.task)['status'], 'done')
        index = f.rt.team.delivery.index(f.task)
        default = f.rt.team.delivery.read(f.task, {})
        self.assertEqual(index['seq'], current)
        self.assertFalse(index['historical'])
        self.assertEqual(default['delivery']['seq'], current)
        self.assertTrue(f.rt.team.delivery.read(f.task, {'delivery_seq': late.seq})['historical'])

    def test_wrong_run_sources_do_not_complete_new_direction(self):
        for case in ('unknown_agent', 'impersonated_member', 'fake_human', 'notification', 'wrong_recipient', 'missing'):
            with self.subTest(source=case):
                f = self.simple_fixture()
                f.rt.command(f.task, 'Change direction', goal='New objective')
                leader = f.rt.organization.leader
                if case == 'missing':
                    source_seq = 999999
                else:
                    worker = f.rt.team._role(f.worker)
                    actor = (Actor.agent('stranger', 'not a team member') if case == 'unknown_agent' else
                             Actor.agent(worker.id, worker.role) if case == 'impersonated_member' else
                             Actor('stranger', 'stranger', 'human') if case == 'fake_human' else
                             Actor.agent(worker.id, worker.role) if case == 'notification' else Actor.human())
                    source = f.rt.log.emit(type='team.message_received', task_id=f.task, actor=actor,
                        payload={'recipient': f.worker if case == 'wrong_recipient' else leader.id,
                                 'kind': 'notification' if case == 'notification' else 'message', 'text': 'Fixture'})
                    source_seq = source.seq
                run = f.rt.log.emit(type='team.run_started', task_id=f.task, actor=HOST,
                    payload={'message_seq': source_seq, 'role': leader.id, 'run_id': 'invalid-source',
                             'revision': f.rt.team.view(f.task)['revision'], 'control_seq': None})
                f.rt.log.emit(type='team.objective_completed', task_id=f.task, actor=HOST,
                    payload={'run_id': run.payload['run_id'], 'summary': 'Fixture', 'verification': 'Fixture'})
                f.rt.log.emit(type='team.run_finished', task_id=f.task, actor=HOST,
                    payload={'message_seq': source_seq, 'role': leader.id, 'run_id': run.payload['run_id'],
                             'status': 'interrupted'})
                self.assertEqual(f.rt.task(f.task).status, 'running')
                self.assertEqual(f.rt.team.view(f.task)['status'], 'running')
                self.assertFalse(f.rt.team.recovery.scope(f.task)['done'])


if __name__ == '__main__':
    unittest.main()
