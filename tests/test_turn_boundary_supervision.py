"""A bounded root turn hands supervision to a fresh, durable inbox input."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import zipfile

from cater import team
from cater.events import Actor
from cater.runtime import Runtime
from cater.workspaces import WorkspaceService
import test_simple_team as fixture
import test_team_supervision as native_fixture


class TurnBoundarySupervisionTests(unittest.TestCase):
    setUp = fixture.SimpleTeamTests.setUp
    events = fixture.SimpleTeamTests.events
    run_ok = fixture.SimpleTeamTests.run_ok

    def loops(self, count=2):
        return [fixture.turn('', ('read_messages', {})) for _ in range(count)]

    def notices(self):
        return [e for e in self.events('team.message_received')
                if e.actor == team.HOST and e.payload.get('trigger') == 'turn_boundary']

    def stage(self):
        source = self.rt.team.receive(self.task, 'Inspect the existing work', request_id='boundary-root-input')
        message, role, start = self.rt.team._claim(self.task)
        self.assertEqual(message, source)
        self.driver.steps[self.manager] = self.loops()
        with patch.object(team, 'MAX_ACTOR_TURNS', 2):
            list(self.rt.team._run_message(self.task, source, role, start))
        notice, = self.notices()
        return source, start, notice

    def test_real_turn_limit_root_gets_fresh_supervision_without_replaying_history(self):
        source = self.rt.team.receive(self.task, 'Check the original project', request_id='real-boundary-root')
        captured = []
        def supervise(context, history, tools):
            captured.append(context)
            self.assertEqual(history, [])
            self.assertEqual(context['message']['kind'], 'recovery')
            self.assertEqual(context['message']['sender_kind'], 'system')
            self.assertEqual(context['message']['trigger'], 'turn_boundary')
            self.assertEqual(context['work_checkpoint']['objective'], self.rt.task(self.task).goal)
            self.assertIn('read_checkpoint', {tool['name'] for tool in tools})
            return fixture.turn('I will inspect the saved work before arranging the remainder.')
        self.driver.steps[self.manager] = [*self.loops(team.MAX_ACTOR_TURNS), supervise]
        self.run_ok()
        notice, = self.notices()
        self.assertTrue(self.rt.team.recovery.is_message(notice))
        self.assertEqual(len(self.driver.calls), team.MAX_ACTOR_TURNS + 1)
        self.assertEqual(len(captured), 1)
        self.assertEqual([e.seq for e in self.events('team.message_received') if e.actor == Actor.human()], [source.seq])
        first, last = self.events('team.run_finished')
        self.assertEqual(first.payload['interruption_kind'], 'turn_boundary')
        self.assertEqual(first.payload['status'], 'interrupted')
        self.assertEqual(last.payload['status'], 'completed')
        self.assertEqual(self.rt.team.view(self.task)['pending_count'], 0)
        self.assertEqual(self.events('team.objective_completed'), [])

    def test_supervision_is_not_an_unbounded_self_wakeup_chain(self):
        self.rt.team.receive(self.task, 'Inspect and decide', request_id='bounded-supervision-loop')
        self.driver.steps[self.manager] = self.loops(4)
        with patch.object(team, 'MAX_ACTOR_TURNS', 2):
            self.run_ok()
            self.run_ok()
            self.assertEqual(self.rt.team.recovery.prepare(self.task), [])
        self.assertEqual(len(self.driver.calls), 4)
        self.assertEqual(len(self.notices()), 1)
        self.assertEqual([e.payload['status'] for e in self.events('team.run_finished')], ['interrupted', 'interrupted'])
        self.assertEqual(self.events('team.objective_completed'), [])

    def test_supervision_does_not_bypass_configured_token_budget(self):
        self.driver.token_budget = 14
        self.rt.team.receive(self.task, 'Inspect within configured usage', request_id='boundary-configured-budget')
        self.driver.steps[self.manager] = [*self.loops(), fixture.turn('This request must remain unused.')]
        with patch.object(team, 'MAX_ACTOR_TURNS', 2):
            self.run_ok()
            self.run_ok()
        self.assertEqual(len(self.driver.calls), 2)
        self.assertEqual(len(self.driver.steps[self.manager]), 1)
        self.assertEqual(len(self.notices()), 1)
        self.assertEqual([e.payload['status'] for e in self.events('team.run_finished')], ['interrupted', 'interrupted'])
        self.assertNotIn('interruption_kind', self.events('team.run_finished')[-1].payload)
        self.assertEqual(self.events('team.objective_completed'), [])

    def test_direct_customer_worker_root_is_supervised_by_manager(self):
        worker = self.rt.team._role(self.worker)
        source = self.rt.team.receive(self.task, f'@{worker.name} inspect this work', request_id='boundary-direct-worker')
        self.assertEqual(source.payload['recipient'], self.worker)
        self.driver.steps[self.worker] = self.loops()
        self.driver.steps[self.manager] = [fixture.turn('I will inspect the remaining work, not claim it is done.')]
        with patch.object(team, 'MAX_ACTOR_TURNS', 2):
            self.run_ok()
        notice, = self.notices()
        self.assertEqual(notice.payload['recipient'], self.manager)
        self.assertEqual([call['context']['you']['id'] for call in self.driver.calls],
                         [self.worker, self.worker, self.manager])

    def test_child_with_real_parent_reports_without_duplicate_manager_notice(self):
        source = self.rt.team._send(self.task, self.rt.organization.leader, self.worker,
                                   'Inspect the actual files', 'assignment')
        self.driver.steps[self.worker] = self.loops()
        self.driver.steps[self.manager] = [fixture.turn('I will inspect this progress and arrange continuation.')]
        with patch.object(team, 'MAX_ACTOR_TURNS', 2):
            self.run_ok()
        self.assertEqual(self.notices(), [])
        progress, = [e for e in self.events('team.message_received') if e.payload.get('kind') == 'progress']
        self.assertEqual(progress.payload['reply_to'], source.seq)
        self.assertEqual(self.rt.team.recovery.assignments(self.task)[0]['status'], 'interrupted')

    def test_manager_adopted_goal_is_bound_at_the_boundary_not_at_old_start(self):
        self.rt.team.receive(self.task, 'Adopt the website direction', request_id='boundary-adopt-goal')
        def inspect(context, history, tools):
            self.assertEqual(history, [])
            self.assertEqual(context['current_work']['objective'], 'A new website goal')
            self.assertEqual(context['message']['scope']['revision'], context['current_work']['revision'])
            return fixture.turn('The adopted goal remains current.')
        self.driver.steps[self.manager] = [fixture.turn('', ('set_objective', {'goal': 'A new website goal'})),
                                          *self.loops(1), inspect]
        with patch.object(team, 'MAX_ACTOR_TURNS', 2):
            self.run_ok()
        notice, = self.notices()
        start = next(e for e in self.events('team.run_started') if e.seq == notice.payload['recovery_of'])
        self.assertNotEqual(notice.payload['scope']['revision'], start.payload['recovery_scope']['revision'])
        self.assertTrue(self.rt.team.recovery.is_message(notice))

    def test_boundary_finish_and_notice_rollback_together_then_crash_recovery_is_available(self):
        self.rt.team.receive(self.task, 'Inspect safely', request_id='boundary-atomic-input')
        self.driver.steps[self.manager] = self.loops()
        emit = self.rt.log.emit
        def crash(**kwargs):
            event = emit(**kwargs)
            if kwargs['type'] == 'team.message_received' and kwargs['payload'].get('trigger') == 'turn_boundary':
                raise SystemExit('fixture interruption before notification commit')
            return event
        with patch.object(team, 'MAX_ACTOR_TURNS', 2), \
                patch.object(self.rt.log, 'emit', side_effect=crash), self.assertRaises(SystemExit):
            self.rt.collaborate(self.task)
        self.assertEqual(self.events('team.run_finished'), [])
        self.assertEqual(self.notices(), [])
        self.assertFalse(self.rt.log.db.in_transaction)
        recovered, = self.rt.team.recovery.prepare(self.task)
        self.assertNotIn('trigger', recovered.payload)
        self.assertTrue(self.rt.team.recovery.is_message(recovered))

    def test_queued_notice_survives_runtime_reopen_without_duplicate(self):
        _, _, notice = self.stage()
        with Runtime(self.rt.log.path, roles_path=self.roles, drivers={'team_test': self.driver},
                     close_drivers_on_close=False) as other:
            self.assertEqual(other.team.recovery.prepare(self.task), [notice])
            self.driver.steps[self.manager] = [fixture.turn('Fresh state checked.')]
            other.collaborate(self.task)
            self.assertEqual(other.team.recovery.prepare(self.task), [])
        self.assertEqual(len(self.notices()), 1)
        self.assertEqual(len(self.driver.calls), 3)

    def test_new_customer_message_takes_over_queued_boundary_notice(self):
        _, _, notice = self.stage()
        newest = self.rt.team.receive(self.task, 'Discuss another plan first', request_id='boundary-new-customer')
        self.assertFalse(self.rt.team.recovery.current(self.task, notice))
        self.driver.steps[self.manager] = [fixture.turn('We will discuss the new plan first.')]
        self.run_ok()
        self.assertEqual(len(self.driver.calls), 3)
        self.assertEqual(self.driver.calls[-1]['context']['message']['seq'], newest.seq)
        self.assertEqual(self.rt.team.view(self.task)['pending_count'], 0)

    def test_new_customer_during_supervision_assembly_preempts_old_request(self):
        self.stage()
        capture = self.rt.team._model_input
        changed = []
        def update(*args, **kwargs):
            value = capture(*args, **kwargs)
            if not changed:
                worker = self.rt.team._role(self.worker)
                changed.append(self.rt.team.receive(self.task, f'@{worker.name} discuss first',
                                                     request_id='boundary-preflight-customer'))
            return value
        self.driver.steps[self.worker] = [fixture.turn('I will discuss first.')]
        self.driver.steps[self.manager] = [fixture.turn('This stale supervision must not be requested.')]
        with patch.object(self.rt.team, '_model_input', side_effect=update):
            self.run_ok()
        self.assertEqual(len(self.driver.calls), 3)
        self.assertEqual(self.driver.calls[-1]['context']['message']['seq'], changed[0].seq)
        self.assertEqual(len(self.driver.steps[self.manager]), 1)

    def test_pause_or_cancel_before_claim_does_not_revive_boundary(self):
        for command in ('pause', 'cancel'):
            with self.subTest(command=command):
                f = TurnBoundarySupervisionTests()
                f.setUp()
                try:
                    _, _, notice = f.stage()
                    f.rt.execution_control.change(f.task, command, request_id=f'boundary-{command}-request')
                    self.assertFalse(f.rt.team.recovery.current(f.task, notice))
                    self.assertEqual(f.rt.team.recovery.prepare(f.task), [])
                    self.assertIsNone(f.rt.team._claim(f.task))
                    self.assertEqual(len(f.driver.calls), 2)
                finally:
                    f.doCleanups()

    def test_finish_scope_marker_and_trigger_are_required_and_actor_cannot_forge_notice(self):
        _, _, notice = self.stage()
        cases = [({'trigger': 'unknown'}, team.HOST), ({'trigger': None}, team.HOST),
                 ({'scope': {}}, team.HOST), ({'recovery_hash': 'wrong'}, team.HOST),
                 ({'recovery_of': True}, team.HOST), ({'interrupted_seq': True}, team.HOST),
                 ({}, Actor.system('other')), ({}, Actor.agent(self.worker, self.rt.team._role(self.worker).role))]
        for changed, actor in cases:
            with self.subTest(changed=changed, actor=actor):
                payload = {**deepcopy(notice.payload), **changed}
                forged = self.rt.log.emit(type='team.message_received', task_id=self.task, actor=actor, payload=payload)
                self.assertFalse(self.rt.team.recovery.is_message(forged))
                self.assertFalse(self.rt.team.recovery.current(self.task, forged))
                self.assertNotIn(forged, self.rt.team._messages(self.task))
        finish = next(e for e in self.events('team.run_finished') if e.seq == notice.payload['interrupted_seq'])
        events = self.rt.log.read(task_id=self.task)
        for changed in ({'interruption_kind': 'other'}, {'recovery_scope': {}}, {'recovery_of': None}):
            mutated = replace(finish, payload={**finish.payload, **changed})
            stream = [mutated if e.seq == finish.seq else e for e in events]
            self.assertFalse(self.rt.team.recovery.is_message(notice, stream))

    def test_explicit_wait_plain_reply_and_model_failure_do_not_schedule_supervision(self):
        for response, expected in ((fixture.turn('A plain answer.'), 'completed'),
                                   (fixture.turn('', ('wait_for_reply', {})), 'waiting'),
                                   (RuntimeError('private fixture error'), 'failed')):
            with self.subTest(expected=expected):
                f = TurnBoundarySupervisionTests()
                f.setUp()
                try:
                    f.rt.team.receive(f.task, 'Inspect this request', request_id=f'no-boundary-{expected}')
                    f.driver.steps[f.manager] = [response]
                    with patch.object(team, 'MAX_ACTOR_TURNS', 1):
                        f.rt.collaborate(f.task)
                    self.assertEqual(f.notices(), [])
                    finish, = f.events('team.run_finished')
                    self.assertEqual(finish.payload['status'], expected)
                    self.assertNotIn('interruption_kind', finish.payload)
                    self.assertEqual(len(f.driver.calls), 1)
                finally:
                    f.doCleanups()

    def test_delivery_on_last_turn_is_not_false_interruption(self):
        self.rt.team.receive(self.task, 'Deliver the text', request_id='boundary-final-delivery')
        self.driver.steps[self.manager] = [fixture.turn('', ('publish_artifact', {
            'filename': 'result.txt', 'content': 'Final text.', 'summary': 'Fixture text', 'format': 'text'})),
            fixture.turn('', ('finish_work', {'summary': 'Text delivered', 'verification': 'Text only; no program executed'}))]
        with patch.object(team, 'MAX_ACTOR_TURNS', 2):
            self.run_ok()
        self.assertEqual(self.rt.team.view(self.task)['status'], 'done')
        finish, = self.events('team.run_finished')
        self.assertEqual(finish.payload['status'], 'completed')
        self.assertNotIn('interruption_kind', finish.payload)
        self.assertEqual(self.notices(), [])

    def test_formal_parent_return_on_last_turn_is_not_false_interruption(self):
        source = self.rt.team._send(self.task, self.rt.organization.leader, self.worker,
                                   'Write the guide and return it', 'assignment')
        self.driver.steps[self.worker] = [fixture.turn('', ('publish_artifact', {
            'filename': 'guide.txt', 'content': 'Guide text.', 'summary': 'Fixture guide', 'format': 'text'})),
            fixture.turn('', ('send_message', {'recipient': self.manager, 'reply_to': source.seq,
                                               'text': 'The guide is saved for your inspection.'}))]
        self.driver.steps[self.manager] = [fixture.turn('I will inspect the exact guide before delivery.')]
        with patch.object(team, 'MAX_ACTOR_TURNS', 2):
            self.run_ok()
        worker_run, = [e for e in self.events('team.run_finished') if e.payload['role'] == self.worker]
        self.assertEqual(worker_run.payload['status'], 'completed')
        self.assertNotIn('interruption_kind', worker_run.payload)
        self.assertEqual(self.notices(), [])
        self.assertEqual(self.rt.team.recovery.assignments(self.task), [])
        self.assertEqual(self.events('team.objective_completed'), [])

    def test_scope_changes_after_queue_do_not_start_old_supervision(self):
        for change in ('goal', 'done', 'workspace', 'rule'):
            with self.subTest(change=change):
                f = TurnBoundarySupervisionTests()
                f.setUp()
                try:
                    _, _, notice = f.stage()
                    if change in {'goal', 'done'}:
                        f.rt.log.emit(type='team.objective_updated' if change == 'goal' else 'team.objective_completed',
                            actor=team.HOST, task_id=f.task, payload={'goal': 'New fixture goal', 'summary': 'Fixture ended'})
                    elif change == 'rule':
                        f.rt.rulebook.record('New guidance', scope='task', task_id=f.task)
                    else:
                        root = f.root / 'project'
                        root.mkdir()
                        config = f.root / 'boundary-workspaces.json'
                        config.write_text(json.dumps({'workspaces': [{'id': 'project', 'title': 'Project',
                            'root': str(root), 'mode': 'edit'}]}), encoding='utf-8')
                        f.rt.workspaces = WorkspaceService(f.rt, config)
                        f.rt.workspaces.bind(f.task, 'project', expected_revision=None)
                    self.assertFalse(f.rt.team.recovery.current(f.task, notice))
                    self.assertEqual(f.rt.team.recovery.prepare(f.task), [])
                    self.assertIsNone(f.rt.team._claim(f.task))
                    self.assertEqual(len(f.driver.calls), 2)
                finally:
                    f.doCleanups()

    def test_scope_change_during_supervision_assembly_skips_first_request(self):
        self.stage()
        capture = self.rt.team._model_input
        def update(*args, **kwargs):
            value = capture(*args, **kwargs)
            self.rt.rulebook.record('New guidance while reading', scope='task', task_id=self.task)
            return value
        self.driver.steps[self.manager] = [fixture.turn('This stale request must not start.')]
        with patch.object(self.rt.team, '_model_input', side_effect=update):
            self.run_ok()
        self.assertEqual(len(self.driver.calls), 2)
        self.assertEqual(len(self.driver.steps[self.manager]), 1)
        self.assertEqual(len(self.notices()), 1)

    def test_customer_message_after_last_tool_prevents_boundary_notice(self):
        self.rt.team.receive(self.task, 'First request', request_id='boundary-final-tool-old')
        invoke, calls, newer = self.rt.team._invoke_tool, [], []
        def update(*args, **kwargs):
            result = invoke(*args, **kwargs)
            calls.append(True)
            if len(calls) == 2:
                newer.append(self.rt.team.receive(self.task, 'Discuss another plan first', request_id='boundary-last-tool-new'))
            return result
        self.driver.steps[self.manager] = [*self.loops(), fixture.turn('We will discuss the new plan.')]
        with patch.object(team, 'MAX_ACTOR_TURNS', 2), patch.object(self.rt.team, '_invoke_tool', side_effect=update):
            self.run_ok()
        self.assertEqual(self.notices(), [])
        self.assertEqual(len(self.driver.calls), 3)
        self.assertEqual(self.driver.calls[-1]['context']['message']['seq'], newer[0].seq)


class BoundaryReviewDeliveryTests(unittest.TestCase):
    setUp = native_fixture.TeamSupervisionTests.setUp
    events = fixture.SimpleTeamTests.events
    native_turn = native_fixture.TeamSupervisionTests.native_turn

    def test_fresh_supervision_finishes_two_reworks_and_delivers_only_approved_bytes(self):
        reviewer = next(r.id for r in self.rt.worker_roles if r.id != self.worker)
        versions = ['Missing zero.', 'Zero included but boundary missing.', 'Negative and zero covered; execution unverified.']
        original, returns, supervised = [], [], []
        def manager(context, history, tools):
            if history:
                return self.native_turn('', ('wait_for_reply', {}))
            if context['message'].get('trigger') == 'turn_boundary':
                supervised.append(context['message']['seq'])
                self.assertEqual(context['message']['sender_kind'], 'system')
                return self.native_turn('', ('delegate', {'recipient': self.worker,
                    'instruction': 'Write the agreed guide, obtain independent review, fix issues, and report the exact version.'}),
                    ('wait_for_reply', {}))
            if context['message']['kind'] == 'report':
                self.assertEqual(context['message']['reply_to'], original[0])
                return self.native_turn('', ('finish_work', {'summary': 'Reviewed guide delivered',
                    'verification': 'Exact text inspected; no conversion executed'}))
            return self.native_turn('', ('wait_for_reply', {}))
        def author(context, history, tools):
            active = context['active_assignment']
            if not original:
                original.append(active['seq'])
            self.assertEqual(active['seq'], original[0])
            rows = context['reviews']
            if history:
                artifact = json.loads(history[-1]['content'])
                arguments = {'reviewer': reviewer, 'artifact_seqs': [artifact['artifact_seq']],
                             'instruction': 'Inspect the exact guide and its stated boundaries.'}
                if rows:
                    arguments['review_seq'] = rows[0]['request_seq']
                return self.native_turn('', ('request_review', arguments))
            if rows and rows[0]['status'] == 'approved':
                returns.append(active['seq'])
                return self.native_turn('', ('send_message', {'recipient': self.manager, 'reply_to': active['seq'],
                    'text': 'The revised exact guide is approved; no conversion was executed.'}), ('wait_for_reply', {}))
            index = len(self.rt.task(self.task).deliverables)
            return self.native_turn('', ('publish_artifact', {'filename': 'guide.md', 'content': versions[index],
                'summary': f'Fixture guide version {index + 1}', 'format': 'markdown'}))
        def review(context, history, tools):
            row, = context['reviews']
            if not history:
                return self.native_turn('', ('read_artifact', {'artifact_seq': row['artifacts'][0]['seq']}))
            actual = json.loads(history[-1]['content'])
            index = versions.index(actual['content'])
            return self.native_turn('', ('record_review', {'review_seq': row['request_seq'],
                'verdict': 'approved' if index == 2 else 'changes_requested',
                'feedback': ['Include zero.', 'Explain execution boundary.', 'Current guide checked.'][index]}))
        self.driver.steps = {
            self.manager: [*[self.native_turn('', ('read_messages', {})) for _ in range(3)], *[manager] * 12],
            self.worker: [author] * 8, reviewer: [review] * 6,
        }
        source = self.rt.team.receive(self.task, 'Write a guide and independently review the exact version',
                                      request_id='boundary-two-rework-delivery')
        with patch.object(team, 'MAX_ACTOR_TURNS', 3):
            self.rt.collaborate(self.task)
        self.assertEqual(self.protocol_errors, [])
        self.assertEqual(len(supervised), 1)
        self.assertEqual(returns, original)
        self.assertEqual([e.payload['verdict'] for e in self.events('team.review_recorded')],
                         ['changes_requested', 'changes_requested', 'approved'])
        delivery, = self.events('team.objective_completed')
        final = self.rt.task(self.task).deliverables[-1]
        review_state, = self.rt.team_reviews.view(self.task)
        self.assertEqual(delivery.payload['artifacts'], review_state['artifacts'])
        self.assertEqual(delivery.payload['artifacts'][0]['seq'], final['seq'])
        self.assertEqual(Path(final['local_path']).read_bytes(), versions[-1].encode())
        with zipfile.ZipFile(io.BytesIO(self.rt.team.archive(self.task))) as archive:
            self.assertEqual(archive.read('guide.md'), versions[-1].encode())
        self.assertEqual(self.rt.team.recovery.assignments(self.task), [])
        self.assertEqual(self.rt.team.view(self.task)['status'], 'done')
        self.assertEqual([e.seq for e in self.events('team.message_received') if e.actor == Actor.human()], [source.seq])
        self.assertTrue(self.rt.verify_chain().ok)


if __name__ == '__main__':
    unittest.main()
