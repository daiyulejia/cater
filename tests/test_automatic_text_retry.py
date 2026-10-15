import io
import json
import os
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from cater.anthropic import AnthropicMessagesDriver
from cater.drivers import OpenAICompatibleDriver, TextTransportFailure
from cater.events import Actor
from cater.runtime import Runtime
from cater.text_retry import TextRetryGuard
from test_work import WorkDriver, item


class AutomaticTextRetryTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {'CATER_RETRY_TEST': 'fixture-key'}))
        self.enterContext(patch('cater.text_retry.RETRY_DELAY', 0.0))

    def runtime(self, cls=AnthropicMessagesDriver, *, enabled=True, reviewer_api=False):
        api = cls('api', endpoint='https://fixture.invalid/messages', model='fixture', api_key_env='CATER_RETRY_TEST')
        drivers = {'dev': WorkDriver() if reviewer_api else api, 'test': api if reviewer_api else WorkDriver(),
                   'design': WorkDriver()}
        rt = Runtime(drivers=drivers, text_retry=enabled, circuit_breaker_threshold=10)
        self.addCleanup(rt.close)
        task = rt.create_task('Deliver a small independently checked note')
        rt.plan_work(task, [item('draft'), item('next', owner='design', deps=['draft'])])
        return rt, task, api

    def events(self, rt, task, kind):
        return list(rt.log.iter_events(task_id=task, event_types=(kind,)))

    def wire(self, cls, raw):
        value = ({'type': 'message', 'content': [{'type': 'text', 'text': json.dumps(raw)}],
                  'stop_reason': 'end_turn', 'usage': {'input_tokens': 4, 'output_tokens': 6}}
                 if cls is AnthropicMessagesDriver else
                 {'choices': [{'message': {'content': json.dumps(raw)}}], 'usage': {'total_tokens': 10}})
        return io.BytesIO(json.dumps(value).encode())

    def success(self, cls, *, reviewer=False):
        return self.wire(cls, {'decision': 'approve', 'reason': 'Inspected the actual note.'} if reviewer else
            {'text': 'Prepared note', 'claim': 'Prepared note', 'deliverables': [
                {'title': 'Note', 'summary': 'A short note', 'filename': 'note.md', 'format': 'markdown', 'content': 'Checked result.'}]})

    def opener(self, cls):
        return 'cater.anthropic._open_api_request' if cls is AnthropicMessagesDriver else 'cater.drivers._open_api_request'

    def test_live_author_retries_once_then_hands_off_without_a_new_customer_command(self):
        for cls in (AnthropicMessagesDriver, OpenAICompatibleDriver):
            for error in (TimeoutError('fixture'), HTTPError('https://fixture.invalid', 429, 'fixture', {}, None),
                          HTTPError('https://fixture.invalid', 502, 'fixture', {}, None)):
                with self.subTest(cls=cls.__name__, failure=type(error).__name__):
                    rt, task, _ = self.runtime(cls)
                    with patch(self.opener(cls), side_effect=[error, self.success(cls)]) as opened:
                        rt.collaborate(task)
                    self.assertEqual(opened.call_count, 2)
                    self.assertTrue(rt.work.view(task)['stage_accepted'])
                    self.assertEqual(len(self.events(rt, task, 'work.claimed')), 2)
                    self.assertEqual(len(self.events(rt, task, 'human.commanded')), 0)
                    start, = self.events(rt, task, 'driver.retry_scheduled')
                    finish, = self.events(rt, task, 'driver.retry_finished')
                    self.assertEqual((finish.payload['request_seq'], finish.payload['status']), (start.seq, 'succeeded'))
                    self.assertEqual(start.payload['failure_seq'], self.events(rt, task, 'driver.failed')[0].seq)
                    self.assertEqual(start.payload['retry_limit'], 1)
                    self.assertEqual(rt._reported_driver_tokens('dev'), 10)
                    cost = next(l for l in rt.review(task)['lenses'] if l['id'] == 'cost')
                    self.assertEqual(cost['status'], 'watch')
                    self.assertIn('failed driver attempt', cost['finding'])
                    self.assertTrue(rt.verify_chain().ok)

    def test_live_stage_review_retries_without_regenerating_the_accepted_input(self):
        for cls in (AnthropicMessagesDriver, OpenAICompatibleDriver):
            with self.subTest(cls=cls.__name__):
                rt, task, _ = self.runtime(cls, reviewer_api=True)
                with patch(self.opener(cls), side_effect=[TimeoutError('fixture'), self.success(cls, reviewer=True),
                                                        self.success(cls, reviewer=True)]) as opened:
                    rt.collaborate(task)
                self.assertEqual(opened.call_count, 3)
                self.assertTrue(rt.work.view(task)['stage_accepted'])
                self.assertEqual(len(self.events(rt, task, 'work.submitted')), 2)
                self.assertEqual(len(self.events(rt, task, 'driver.retry_scheduled')), 1)
                self.assertEqual(len(self.events(rt, task, 'work.review_started')), 2)

    def test_second_transport_failure_stops_and_retains_the_checkpoint(self):
        rt, task, _ = self.runtime()
        with patch(self.opener(AnthropicMessagesDriver), side_effect=TimeoutError('fixture')) as opened:
            rt.collaborate(task)
        self.assertEqual(opened.call_count, 2)
        self.assertEqual([i['status'] for i in rt.work.view(task)['items']], ['ready', 'blocked'])
        self.assertFalse(self.events(rt, task, 'work.failed')[-1].payload['uncertain'])
        self.assertEqual(self.events(rt, task, 'driver.retry_finished')[-1].payload['status'], 'failed')
        self.assertEqual(self.events(rt, task, 'deliverable.published'), [])

    def test_permanent_or_unclassified_failure_is_never_retried(self):
        for error in (HTTPError('https://fixture.invalid', 403, 'fixture', {}, None), RuntimeError('unknown result')):
            with self.subTest(error=type(error).__name__):
                rt, task, _ = self.runtime()
                with patch(self.opener(AnthropicMessagesDriver), side_effect=error) as opened:
                    rt.collaborate(task)
                opened.assert_called_once()
                self.assertEqual(self.events(rt, task, 'driver.retry_scheduled'), [])
                self.assertTrue(self.events(rt, task, 'work.failed')[-1].payload['uncertain'])

    def test_disabled_policy_preserves_the_original_single_call_contract(self):
        rt, task, _ = self.runtime(enabled=False)
        with patch(self.opener(AnthropicMessagesDriver), side_effect=TimeoutError('fixture')) as opened:
            rt.collaborate(task)
        opened.assert_called_once()
        self.assertEqual(self.events(rt, task, 'driver.retry_scheduled'), [])

    def test_pause_cancel_direction_and_pause_resume_cancel_the_wait(self):
        for change in ('pause', 'cancel', 'direction', 'pause_resume'):
            with self.subTest(change=change):
                rt, task, _ = self.runtime()
                def interrupt(_seconds):
                    if change == 'direction':
                        rt.command(task, 'The goal is now a different deliverable')
                    else:
                        rt.execution_control.change(task, 'pause' if change == 'pause_resume' else change,
                                                    request_id='retry-control-first')
                        if change == 'pause_resume':
                            rt.execution_control.change(task, 'resume', request_id='retry-control-next')
                with patch('cater.text_retry.RETRY_DELAY', 1), patch('cater.text_retry.time.sleep', side_effect=interrupt), \
                        patch(self.opener(AnthropicMessagesDriver), side_effect=TimeoutError('fixture')) as opened:
                    rt.collaborate(task)
                opened.assert_called_once()
                self.assertEqual(self.events(rt, task, 'driver.retry_finished')[-1].payload['status'], 'cancelled')
                self.assertEqual(self.events(rt, task, 'deliverable.published'), [])

    def test_changed_model_configuration_and_expired_owner_cancel_the_wait(self):
        for change in ('config', 'expired', 'reviewer'):
            with self.subTest(change=change):
                rt, task, api = self.runtime(reviewer_api=change == 'reviewer')
                def interrupt(_seconds):
                    if change == 'config':
                        api.model = 'another-model'
                    elif change == 'reviewer':
                        started = self.events(rt, task, 'work.review_started')[-1]
                        rt.work.reconcile_review(task, started.seq, reason='Original process stopped', invocation_stopped=True)
                    else:
                        # Keep the rest of the runtime clock untouched while checking this owner.
                        guard = TextRetryGuard.current
                        def expired(owner):
                            with patch('cater.text_retry.time.time', return_value=10**12):
                                return guard(owner)
                        rt._retry_expiry_patch = patch.object(TextRetryGuard, 'current', expired)
                        rt._retry_expiry_patch.start()
                        self.addCleanup(rt._retry_expiry_patch.stop)
                with patch('cater.text_retry.RETRY_DELAY', 1), patch('cater.text_retry.time.sleep', side_effect=interrupt), \
                        patch(self.opener(AnthropicMessagesDriver), side_effect=TimeoutError('fixture')) as opened:
                    rt.collaborate(task)
                opened.assert_called_once()
                self.assertEqual(self.events(rt, task, 'driver.retry_finished')[-1].payload['status'], 'cancelled')
                if change == 'expired':
                    rt._retry_expiry_patch.stop()

    def test_new_local_effect_evidence_disables_automatic_replay(self):
        rt, task, _ = self.runtime()
        def fail(*args, **kwargs):
            current = rt.work.view(task)['items'][0]
            rt.log.emit(type='workspace.tool_started', actor=Actor.system('workspace_gateway'), task_id=task,
                        payload={'attempt_id': current['attempt_id'], 'work_item_id': 'draft'})
            raise TimeoutError('fixture')
        with patch(self.opener(AnthropicMessagesDriver), side_effect=fail) as opened:
            rt.collaborate(task)
        opened.assert_called_once()
        self.assertEqual(self.events(rt, task, 'driver.retry_scheduled'), [])
        self.assertTrue(self.events(rt, task, 'work.failed')[-1].payload['uncertain'])
        self.assertEqual(rt.work.view(task)['items'][0]['status'], 'uncertain')

    def test_new_tool_results_or_source_capture_during_wait_keep_effects_uncertain(self):
        for kind in ('workspace.tool_finished', 'workspace.tool_reconciled', 'workspace.files_captured'):
            with self.subTest(kind=kind):
                rt, task, _ = self.runtime()
                def result_arrives(_seconds):
                    current = rt.work.view(task)['items'][0]
                    rt.log.emit(type=kind, actor=Actor.system('workspace_gateway'), task_id=task,
                                payload={'attempt_id': current['attempt_id'], 'work_item_id': 'draft'})
                with patch('cater.text_retry.RETRY_DELAY', 1), patch('cater.text_retry.time.sleep', side_effect=result_arrives), \
                        patch(self.opener(AnthropicMessagesDriver), side_effect=TimeoutError('fixture')) as opened:
                    rt.collaborate(task)
                opened.assert_called_once()
                self.assertEqual(self.events(rt, task, 'driver.retry_finished')[-1].payload['status'], 'cancelled')
                self.assertTrue(self.events(rt, task, 'work.failed')[-1].payload['uncertain'])

    def test_policy_and_actual_credential_revocation_cancel_the_wait(self):
        for change in ('policy', 'credential'):
            with self.subTest(change=change):
                rt, task, _ = self.runtime()
                def revoke(_seconds):
                    if change == 'policy':
                        rt.text_retry = False
                    else:
                        os.environ['CATER_RETRY_TEST'] = 'replacement-fixture-key'
                with patch('cater.text_retry.RETRY_DELAY', 1), patch('cater.text_retry.time.sleep', side_effect=revoke), \
                        patch(self.opener(AnthropicMessagesDriver), side_effect=TimeoutError('fixture')) as opened:
                    rt.collaborate(task)
                opened.assert_called_once()
                self.assertEqual(self.events(rt, task, 'driver.retry_finished')[-1].payload['status'], 'cancelled')
                serialized = json.dumps([e.payload for e in self.events(rt, task, 'driver.retry_scheduled')])
                self.assertNotIn('replacement-fixture-key', serialized)
                self.assertNotIn('fixture-key', serialized)

    def test_preexisting_pending_tools_disable_automatic_retry_even_if_they_finish_during_call(self):
        rt, task, _ = self.runtime()
        initialize = TextRetryGuard.__init__
        started = None
        def pending_first(guard, runtime, task_id, role, driver, context):
            nonlocal started
            started = runtime.log.emit(type='workspace.tool_started', actor=Actor.system('workspace_gateway'), task_id=task_id,
                payload={'attempt_id': context['attempt_id'], 'work_item_id': 'draft', 'call_id': 'old-tool'})
            initialize(guard, runtime, task_id, role, driver, context)
        def finished(*args, **kwargs):
            rt.log.emit(type='workspace.tool_finished', actor=Actor.system('workspace_gateway'), task_id=task,
                payload={'started_seq': started.seq, 'call_id': 'old-tool', 'result': {'status': 'ok', 'complete': True}})
            raise TimeoutError('fixture')
        with patch.object(TextRetryGuard, '__init__', pending_first), \
                patch(self.opener(AnthropicMessagesDriver), side_effect=finished) as opened:
            rt.collaborate(task)
        opened.assert_called_once()
        self.assertEqual(self.events(rt, task, 'driver.retry_scheduled'), [])
        self.assertTrue(self.events(rt, task, 'work.failed')[-1].payload['uncertain'])

    def test_existing_circuit_does_not_gain_an_automatic_probe(self):
        rt, task, _ = self.runtime()
        rt.circuit_breaker_threshold = 1
        with patch(self.opener(AnthropicMessagesDriver), side_effect=TimeoutError('fixture')) as opened:
            rt.collaborate(task)
        opened.assert_called_once()
        self.assertEqual(self.events(rt, task, 'driver.retry_scheduled'), [])

    def test_long_server_wait_never_becomes_an_early_default_retry(self):
        for value in ('3600', str(2**54), '9' * 100):
            with self.subTest(value=value):
                rt, task, _ = self.runtime()
                error = HTTPError('https://fixture.invalid', 429, 'fixture', {'Retry-After': value}, None)
                with patch(self.opener(AnthropicMessagesDriver), side_effect=error) as opened:
                    rt.collaborate(task)
                opened.assert_called_once()
                self.assertEqual(self.events(rt, task, 'driver.retry_scheduled'), [])

    def test_reported_budget_change_during_wait_cancels_without_an_extra_request(self):
        rt, task, api = self.runtime()
        api.token_budget = 10
        other_task = rt.create_task('Another task sharing this model channel')
        def exhaust(_seconds):
            rt.log.emit(type='msg.said', task_id=other_task, actor=Actor.agent('dev', 'worker'),
                        payload={'text': 'Another request completed', 'driver': 'dev', 'tokens': 10, 'token_usage': 'reported'})
        with patch('cater.text_retry.RETRY_DELAY', 1), patch('cater.text_retry.time.sleep', side_effect=exhaust), \
                patch(self.opener(AnthropicMessagesDriver), side_effect=TimeoutError('fixture')) as opened:
            rt.collaborate(task)
        opened.assert_called_once()
        self.assertEqual(self.events(rt, task, 'driver.retry_finished')[-1].payload['status'], 'cancelled')

    def test_replaced_builtin_complete_cannot_gain_permission_to_retry(self):
        rt, task, api = self.runtime()
        with patch.object(api, 'complete', side_effect=TextTransportFailure('fixture')) as completed:
            rt.collaborate(task)
        completed.assert_called_once()
        self.assertEqual(self.events(rt, task, 'driver.retry_scheduled'), [])
        self.assertTrue(self.events(rt, task, 'work.failed')[-1].payload['uncertain'])

    def test_tool_enabled_work_never_inherits_the_text_retry_policy(self):
        rt, task, api = self.runtime()
        claim = rt.work.claim(task, 'draft')
        with patch.object(rt.workspaces, 'view', return_value={'workspace_id': 'attached', 'mode': 'edit'}), \
                patch.object(rt.workspaces, 'complete', side_effect=TextTransportFailure('unknown tool result')) as complete:
            result = rt._complete_with_containment(task_id=task, task=rt.task(task), role=rt._role_by_id('dev'),
                driver=api, prompt='fixture', work_context=claim.payload)
        complete.assert_called_once()
        self.assertTrue(result.meta['contained'])
        self.assertEqual(self.events(rt, task, 'driver.retry_scheduled'), [])

    def test_adapter_subclass_does_not_inherit_original_transport_retry_permission(self):
        class Derived(AnthropicMessagesDriver):
            pass
        rt, task, _ = self.runtime(Derived)
        with patch('cater.anthropic._open_api_request', side_effect=TimeoutError('fixture')) as opened:
            rt.collaborate(task)
        opened.assert_called_once()
        self.assertTrue(self.events(rt, task, 'work.failed')[-1].payload['uncertain'])
        self.assertEqual(self.events(rt, task, 'driver.retry_scheduled'), [])

    def test_invalid_policy_is_rejected(self):
        for value in (1, 'yes', None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Runtime(text_retry=value)
