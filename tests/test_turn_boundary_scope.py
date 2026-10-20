"""Supervision cannot expand its execution scope by observing a later change."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
import unittest
from unittest.mock import patch

from cater import team
from cater.runtime import Runtime
from cater.workspaces import WorkspaceService
import test_simple_team as fixture
import test_turn_boundary_supervision as boundary


class TurnBoundaryScopeTests(unittest.TestCase):
    setUp = fixture.SimpleTeamTests.setUp
    events = fixture.SimpleTeamTests.events
    loops = boundary.TurnBoundarySupervisionTests.loops
    stage = boundary.TurnBoundarySupervisionTests.stage
    notices = boundary.TurnBoundarySupervisionTests.notices

    def other(self):
        runtime = Runtime(self.rt.log.path, roles_path=self.roles, drivers={'team_test': self.driver},
                          close_drivers_on_close=False)
        self.addCleanup(runtime.close)
        return runtime

    def bind(self, name='project'):
        root = self.root / name
        root.mkdir()
        config = self.root / f'{name}-workspaces.json'
        config.write_text(json.dumps({'workspaces': [{'id': name, 'title': name,
            'root': str(root), 'mode': 'edit'}]}), encoding='utf-8')
        self.rt.workspaces = WorkspaceService(self.rt, config)
        previous = self.rt.workspaces.view(self.task)['revision']
        self.rt.workspaces.bind(self.task, name, expected_revision=previous)
        return root

    def test_inflight_recovery_rule_change_cannot_dispatch_old_delegate_or_write(self):
        for tool in ('delegate', 'workspace_write'):
            with self.subTest(tool=tool):
                f = TurnBoundaryScopeTests()
                f.setUp()
                try:
                    root = f.bind()
                    f.stage()
                    other = f.other()
                    def stale(context, history, tools):
                        other.rulebook.record('Do not implement; discuss the updated plan first.', scope='task', task_id=f.task)
                        arguments = ({'recipient': f.worker, 'instruction': 'Implement the old plan.'} if tool == 'delegate'
                                     else {'path': 'stale.txt', 'content': 'old effect', 'expected_sha256': None})
                        return fixture.turn('', (tool, arguments), ('wait_for_reply', {}))
                    f.driver.steps[f.manager] = [stale]
                    claimed = f.rt.team._claim(f.task)
                    list(f.rt.team._run_message(f.task, *claimed))
                    self.assertFalse((root / 'stale.txt').exists())
                    self.assertFalse([e for e in f.events('team.message_received') if e.payload.get('kind') == 'assignment'])
                    self.assertFalse([e for e in f.events('team.tool_started') if e.payload.get('name') == tool])
                    finish = f.events('team.run_finished')[-1]
                    self.assertEqual(finish.payload['status'], 'interrupted')
                    self.assertNotIn('interruption_kind', finish.payload)
                    self.assertEqual(len(f.driver.calls), 3)
                finally:
                    f.doCleanups()

    def test_rule_change_between_recovery_requests_stops_before_another_call(self):
        self.stage()
        self.driver.steps[self.manager] = [fixture.turn('', ('read_messages', {})), fixture.turn('Must not be requested.')]
        claimed = self.rt.team._claim(self.task)
        run = self.rt.team._run_message(self.task, *claimed)
        next(run)
        self.other().rulebook.record('Discuss only.', scope='task', task_id=self.task)
        list(run)
        self.assertEqual(len(self.driver.calls), 3)
        self.assertEqual(len(self.driver.steps[self.manager]), 1)
        self.assertEqual(len(self.notices()), 1)

    def test_external_change_after_last_yield_does_not_become_new_boundary_authority(self):
        for change in ('rule', 'pause-resume', 'workspace', 'external-goal'):
            with self.subTest(change=change):
                f = TurnBoundaryScopeTests()
                f.setUp()
                try:
                    f.rt.team.receive(f.task, 'Inspect the existing work', request_id='scope-tail-root')
                    claimed = f.rt.team._claim(f.task)
                    f.driver.steps[f.manager] = f.loops(1)
                    with patch.object(team, 'MAX_ACTOR_TURNS', 1):
                        run = f.rt.team._run_message(f.task, *claimed)
                        next(run)
                        other = f.other()
                        if change == 'rule':
                            other.rulebook.record('Discuss only.', scope='task', task_id=f.task)
                        elif change == 'pause-resume':
                            other.execution_control.change(f.task, 'pause', request_id='scope-tail-pause')
                            other.execution_control.change(f.task, 'resume', request_id='scope-tail-resume')
                        elif change == 'workspace':
                            f.bind('changed-root')
                        else:
                            other.log.emit(type='team.objective_updated', actor=team.HOST, task_id=f.task,
                                payload={'goal': 'Another direction', 'run_id': 'another-run'})
                        list(run)
                    self.assertEqual(f.notices(), [])
                    self.assertEqual(len(f.driver.calls), 1)
                    finish, = f.events('team.run_finished')
                    self.assertEqual(finish.payload['status'], 'interrupted')
                    self.assertEqual(finish.payload['interruption_kind'], 'turn_boundary')
                    self.assertNotIn('recovery_scope', finish.payload)
                    self.assertTrue(f.rt.verify_chain().ok)
                finally:
                    f.doCleanups()

    def test_last_turn_own_goal_adoption_can_handoff_without_weakening_scope(self):
        source = self.rt.team.receive(self.task, 'Adopt the website direction', request_id='scope-own-tail-goal')
        self.driver.steps[self.manager] = [fixture.turn('', ('set_objective', {'goal': 'The adopted website goal'})),
                                          fixture.turn('Current goal inspected.')]
        with patch.object(team, 'MAX_ACTOR_TURNS', 1):
            self.rt.collaborate(self.task)
        notice, = self.notices()
        adopted, = self.events('team.objective_updated')
        original, supervision = self.events('team.run_started')
        self.assertEqual(adopted.payload['run_id'], original.payload['run_id'])
        self.assertEqual(notice.payload['scope']['revision'], adopted.seq)
        self.assertEqual(supervision.payload['message_seq'], notice.seq)
        self.assertTrue(self.rt.team.recovery.is_message(notice))
        self.assertEqual(self.driver.calls[-1]['context']['latest_customer_message']['seq'], source.seq)
        self.assertEqual(self.driver.calls[-1]['context']['current_work']['objective'], adopted.payload['goal'])

    def test_supervision_can_adopt_goal_publish_and_finish_in_its_own_run(self):
        self.stage()
        self.driver.steps[self.manager] = [fixture.turn('', ('set_objective', {'goal': 'Clarified guide goal'})),
            fixture.turn('', ('publish_artifact', {'filename': 'guide.txt', 'content': 'Actual guide text.',
                'summary': 'Fixture guide', 'format': 'text'})),
            fixture.turn('', ('finish_work', {'summary': 'Guide delivered', 'verification': 'Text checked; no program executed'}))]
        with patch.object(team, 'MAX_ACTOR_TURNS', 3):
            self.rt.collaborate(self.task)
        self.assertEqual(self.rt.team.view(self.task)['status'], 'done')
        self.assertEqual(len(self.notices()), 1)
        self.assertEqual([e.payload['status'] for e in self.events('team.run_finished')], ['interrupted', 'completed'])
        supervisor = self.events('team.run_started')[-1]
        adopted, = self.events('team.objective_updated')
        completed, = self.events('team.objective_completed')
        self.assertEqual(adopted.payload['run_id'], supervisor.payload['run_id'])
        self.assertEqual(completed.payload['run_id'], supervisor.payload['run_id'])
        self.assertEqual(len(self.driver.calls), 5)

    def test_normal_root_can_adopt_guidance_in_an_actual_later_request(self):
        self.rt.team.receive(self.task, 'Inspect existing files', request_id='scope-observed-new-guidance')
        claimed = self.rt.team._claim(self.task)
        self.driver.steps[self.manager] = self.loops()
        with patch.object(team, 'MAX_ACTOR_TURNS', 2):
            run = self.rt.team._run_message(self.task, *claimed)
            next(run)
            self.other().rulebook.record('Describe verification boundaries.', scope='task', task_id=self.task)
            list(run)
        notice, = self.notices()
        self.assertTrue(self.rt.team.recovery.current(self.task, notice))
        self.assertEqual(self.driver.calls[0]['context']['rules'], [])
        self.assertEqual(self.driver.calls[1]['context']['rules'][0]['text'], 'Describe verification boundaries.')
        self.assertNotEqual(notice.payload['scope']['rule_fingerprint'], claimed[2].payload['recovery_scope']['rule_fingerprint'])
        self.driver.steps[self.manager] = [fixture.turn('Current guidance read before arranging the remainder.')]
        self.rt.collaborate(self.task)
        self.assertEqual(len(self.driver.calls), 3)
        self.assertEqual(self.driver.calls[-1]['context']['rules'][0]['text'], 'Describe verification boundaries.')

    def test_boolean_or_float_scope_alias_does_not_validate(self):
        _, _, notice = self.stage()
        for key, value in (('revision', False), ('revision', 0.0), ('workspace_available', 0), ('done', 0)):
            with self.subTest(key=key, value=value):
                payload = deepcopy(notice.payload)
                payload['scope'][key] = value
                forged = replace(notice, payload=payload)
                self.assertFalse(self.rt.team.recovery.is_message(forged))
                self.assertFalse(self.rt.team.recovery.current(self.task, forged))


if __name__ == '__main__':
    unittest.main()
