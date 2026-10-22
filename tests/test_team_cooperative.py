"""Production team scheduling with gated cooperative role runs, no model claims."""
from dataclasses import replace
import json
import time
import unittest
from unittest.mock import patch

from cater.acp import AcpPromptResult
from cater.events import Actor
from cater.external_agent import ExternalYield
from cater.tool_protocol import ToolCall
import test_team_external as fixture
import test_team_scope_polling as scope


class CooperativeRun(fixture.ScriptedExternalRun):
    def call(self, name, **arguments):
        if self.revoked:
            raise AssertionError('A revoked role cannot execute tools')
        call = ToolCall(f'cooperative-{self.ordinal}-{len(self.receipts)}', name, arguments)
        result = self.execute(call)
        self.receipts.append((call, result))
        self.check_current()
        return result

    def iter_run(self, prompt):
        self.context = json.loads(prompt.rsplit('\n', 1)[-1])
        self.state = replace(self.state, phase='prompting', prompt_attempted=True,
                             session_id=f'cooperative-{len(self.driver.runs)}')
        try:
            scripts = self.driver.scripts.get(self.context['you']['id'], [])
            if not scripts:
                raise AssertionError('Unexpected cooperative input')
            script = scripts.pop(0)
            result = script(self)
            if hasattr(result, '__next__'):
                result = yield from result
            self.state = replace(self.state, phase='finished', text=result or '', stop_reason='end_turn')
            return AcpPromptResult(result or '', 'end_turn')
        except ExternalYield:
            self.state = replace(self.state, phase='yielded')
            raise
        except BaseException as exc:
            self.state = replace(self.state, phase='failed')
            if isinstance(exc, AssertionError):
                self.driver.errors.append(exc)
            raise
        finally:
            self.state = replace(self.state, cleanup_complete=self.cleanup_complete)


class CooperativeDriver(fixture.ScriptedExternalDriver):
    def agent_run(self, **kwargs):
        run = CooperativeRun(self, **kwargs)
        run.ordinal = len(self.runs) + 1
        self.runs.append(run)
        return run


class TeamCooperativeTests(unittest.TestCase):
    def setUp(self):
        self.fx = fixture.TeamExternalTests()
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        self.driver = CooperativeDriver()
        self.fx.driver = self.driver
        self.fx.rt.drivers[self.driver.name] = self.driver

    def wait_for(self, run, predicate):
        deadline = time.monotonic() + 2
        while not predicate():
            self.assertLess(time.monotonic(), deadline, 'Other role did not advance')
            self.assertFalse(self.fx.rt.log.db.in_transaction)
            run.check_current()
            yield None

    def test_customer_answer_precedes_worker_end_without_restart(self):
        fx = self.fx
        answered = []
        def worker(run):
            fx.receive('请确认默认行为不变，原工作继续')
            yield from self.wait_for(run, lambda: bool(answered))
            self.assertFalse(run.revoked)
            fx.publish(run)
            fx.return_assignment(run)
        def manager_answer(run):
            worker_run, = [r for r in self.driver.runs if r.context['you']['id'] == fx.worker]
            self.assertFalse(worker_run.state.cleanup_complete)
            self.assertEqual(run.call('read_messages')['status'], 'ok')
            answered.append(True)
            run.call('reply_to_customer', text='原工作继续，本次仅确认。')
        self.driver.scripts = {fx.manager: [fx.delegate_wait, manager_answer, fx.deliver], fx.worker: [worker]}
        fx.receive()
        fx.drain()
        self.assertEqual(len([r for r in self.driver.runs if r.context['you']['id'] == fx.worker]), 1)
        self.assertEqual(len(fx.events('team.objective_completed')), 1)
        self.assertEqual(fx.rt.team.external._live, {})
        reply, = [e for e in fx.events('msg.said') if e.payload.get('reply_to') == fx.events('team.message_received')[2].seq]
        worker_end, = [e for e in fx.events('team.run_finished') if e.payload['role'] == fx.worker]
        self.assertLess(reply.seq, worker_end.seq)

    def test_goal_change_invalidates_old_worker_before_another_tool(self):
        fx = self.fx
        changed = []
        def worker(run):
            fx.receive('不要继续旧任务，换新方向')
            yield from self.wait_for(run, lambda: bool(changed))
            run.call('publish_artifact', filename='obsolete.txt', content='obsolete', summary='old', format='text')
            self.fail('Obsolete worker must stop')
        def manager_change(run):
            self.assertEqual(run.call('set_objective', goal='新的方向')['status'], 'ok')
            changed.append(True)
            run.call('reply_to_customer', text='已采用新目标，不继续旧工作。')
        self.driver.scripts = {fx.manager: [fx.delegate_wait, manager_change], fx.worker: [worker]}
        fx.receive()
        fx.drain()
        self.assertEqual(fx.events('deliverable.published'), [])
        self.assertEqual(fx.rt.team.view(fx.task)['objective'], '新的方向')
        self.assertEqual(fx.rt.team.external_runs.pending(fx.task), [])

    def test_known_live_worker_does_not_allow_early_delivery(self):
        fx = self.fx
        observed = []
        def worker(run):
            fx.receive('现在可以交付了吗')
            yield from self.wait_for(run, lambda: bool(observed))
            fx.publish(run)
            fx.return_assignment(run)
        def manager_answer(run):
            result = run.call('finish_work', summary='premature', verification='not checked')
            self.assertEqual(result['code'], 'operation_pending')
            observed.append(True)
            run.call('reply_to_customer', text='伙伴仍在执行，尚未最终交付。')
        self.driver.scripts = {fx.manager: [fx.delegate_wait, manager_answer, fx.deliver], fx.worker: [worker]}
        fx.receive()
        fx.drain()
        self.assertEqual([r['status'] for r in fx.results('finish_work')], ['error', 'ok'])

    def test_fatal_actor_error_revokes_and_closes_every_other_run(self):
        fx = self.fx
        marker = KeyboardInterrupt('synthetic scheduler shutdown')
        def worker(run):
            fx.receive('店长请回答')
            while True:
                self.assertFalse(fx.rt.log.db.in_transaction)
                yield None
        def fail(run):
            raise marker
        self.driver.scripts = {fx.manager: [fx.delegate_wait, fail], fx.worker: [worker]}
        fx.receive()
        with self.assertRaises(KeyboardInterrupt) as caught:
            fx.rt.collaborate(fx.task)
        self.assertIs(caught.exception, marker)
        self.assertTrue(all(r.revoked and r.state.cleanup_complete for r in self.driver.runs))
        self.assertEqual(fx.rt.team.external._live, {})
        self.assertIsNone(fx.rt.team.external._scheduler)
        self.assertEqual(fx.rt.team.external_runs.pending(fx.task), [])
        self.assertEqual(len(fx.events('team.run_started')), len(fx.events('team.run_finished')))

    def test_live_identity_cannot_be_forged_and_unknown_still_blocks_dispatch(self):
        fx = self.fx
        answered = []
        def worker(run):
            rows = fx.rt.team.external_runs.pending(fx.task)
            self.assertEqual(len(rows), 1)
            self.assertTrue(fx.rt.team.external.owns(fx.task, rows[0]))
            self.assertFalse(fx.rt.team.external.owns(fx.task, {**rows[0], 'run_id': 'forged'}))
            self.assertFalse(fx.rt.team.external.owns(fx.task, {**rows[0], 'message_seq': True}))
            with patch.object(fx.rt.team.external, '_scheduler', None):
                self.assertFalse(fx.rt.team.external.owns(fx.task, rows[0]))
            start, = fx.events('team.external_run_started')[-1:]
            fx.rt.log.emit(type='team.external_run_started', task_id=fx.task, actor=fixture.HOST,
                           payload={**start.payload, 'run_id': 'no-live-handle'})
            fx.receive('请看看进度，别重复开工')
            yield from self.wait_for(run, lambda: bool(answered))
        def manager(run):
            answered.append(True)
            run.call('delegate', recipient=fx.reviewer, instruction='Must not be dispatched with unknown run')
            self.fail('Unknown run must block new work')
        self.driver.scripts = {fx.manager: [fx.delegate_wait, manager, lambda run: run.call('wait_for_reply')],
                               fx.worker: [worker]}
        fx.receive()
        fx.rt.collaborate(fx.task)
        self.assertEqual(self.driver.errors, [])
        assignments = [e for e in fx.events('team.message_received') if e.payload['kind'] == 'assignment']
        self.assertEqual(len(assignments), 1)
        pending = fx.rt.team.external_runs.pending(fx.task)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]['outcome'], 'unknown')
        self.assertFalse(pending[0]['replay_authorized'])
        self.assertEqual(fx.events('team.objective_completed'), [])

    def test_legacy_cleanup_requires_exact_boolean_confirmation(self):
        for reported in (True, False, 1, 0, 'yes', None):
            with self.subTest(reported=reported):
                fx = scope._ScopeFixture()
                fx.setUp()
                self.addCleanup(fx.doCleanups)
                class LegacyRun(scope._OfflineRun):
                    def run(self, prompt):
                        try:
                            return super().run(prompt)
                        finally:
                            self.state = replace(self.state, cleanup_complete=reported)
                def factory(**kwargs):
                    run = LegacyRun(fx.driver, **kwargs)
                    fx.driver.runs.append(run)
                    return run
                message, role, started = fx.claim()
                with patch.object(fx.driver, 'agent_run', side_effect=factory):
                    result = fx.team.external.run_message(fx.task, message, role, started,
                        started.payload['revision'], None, fx.driver, 'Offline strict cleanup.\n')
                with fx.rt.log.atomic():
                    revision = result.pop('revision')
                    fx.team._finish_message(fx.task, message, role, started, revision, None, **result)
                pending = fx.team.external_runs.pending(fx.task)
                if reported is True:
                    self.assertEqual(result['status'], 'completed')
                    self.assertEqual(pending, [])
                else:
                    row, = pending
                    self.assertEqual(row['outcome'], 'unknown')
                    self.assertIs(row['replay_authorized'], False)
                    self.assertFalse(fx.team.external.owns(fx.task, row))
                finish, = list(fx.rt.log.iter_events(task_id=fx.task,
                    event_types=('team.external_run_finished',)))
                self.assertIs(finish.payload['cleanup_complete'], reported is True)
