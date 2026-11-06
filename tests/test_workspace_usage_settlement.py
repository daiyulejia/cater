"""Full projection of host-owned workspace model invocation receipts."""
from unittest.mock import patch
import unittest

from cater.drivers import DriverResponse
from cater.events import Actor
from cater.tool_protocol import ToolCall, ToolTurn
from cater.workspaces import HOST
from test_workspace_integration import FakeDriver, WorkspaceIntegrationTests, final_text, turn


class WorkspaceUsageSettlementTests(unittest.TestCase):
    def fixture(self):
        f = WorkspaceIntegrationTests(methodName='runTest')
        f.setUp()
        self.addCleanup(f.doCleanups)
        _, context = f.prepare_call()
        return f, context

    def collect(self, f, context):
        return f.rt._collect_utterances(f.task, f.rt.task(f.task), [f.role], {}, work_context=context)[0]

    def test_invalid_response_has_one_unknown_and_no_tool(self):
        class Key(str): pass
        f, context = self.fixture()
        f.driver.turns = [ToolTurn('', [ToolCall('write', 'workspace_write', {'path': 'a'})], 50,
                                   {Key('contained'): True}, {'role': 'assistant'})]
        result = self.collect(f, context)
        call, = f.events('workspace.model_called')
        self.assertEqual(result['event'].payload['usage_recorded_in'], [call.seq])
        self.assertEqual(f.rt.task(f.task).token_usage, {'unavailable': 1})
        self.assertTrue(result['contained'])
        self.assertEqual(f.executor.calls, [])

    def test_known_call_followed_by_tool_failure_is_not_another_model_call(self):
        f, context = self.fixture()
        f.executor.failure = RuntimeError('synthetic')
        f.driver.turns = [turn(ToolCall('write', 'workspace_write', {'path': 'a'}), tokens=5)]
        self.collect(f, context)
        self.assertEqual(f.rt.task(f.task).token_usage, {'reported': 1})
        self.assertEqual(f.rt.task(f.task).tokens, 5)
        self.assertEqual(len(f.rt.workspaces.pending(task_id=f.task, effects_only=True)), 1)

    def test_driver_failure_is_one_unknown_and_opens_circuit(self):
        f, context = self.fixture()
        f.rt.circuit_breaker_threshold = 1
        f.driver.turns = [RuntimeError('synthetic')]
        result = self.collect(f, context)
        call, = f.events('workspace.model_called')
        self.assertEqual(call.payload['response_status'], 'failed')
        self.assertEqual(result['event'].payload['usage_recorded_in'], [call.seq])
        self.assertEqual(f.rt.task(f.task).token_usage, {'unavailable': 1})
        self.assertTrue(f.rt._is_circuit_open('dev'))
        self.assertFalse(f.events('driver.retry_scheduled'))
        self.collect(f, context)
        self.assertEqual(len(f.driver.tool_requests), 1)
        self.assertEqual(f.rt.task(f.task).token_usage, {'unavailable': 1})

    def test_budget_stops_before_call_with_host_skipped_receipt(self):
        f, context = self.fixture()
        f.driver.token_budget = 0
        result = self.collect(f, context)
        skipped, = f.events('driver.invocation_skipped')
        self.assertEqual(skipped.actor, Actor.system('driver_gateway'))
        self.assertEqual(result['event'].payload['usage_not_started_in'], skipped.seq)
        self.assertEqual(f.rt.task(f.task).token_usage, {})
        self.assertEqual(f.driver.tool_requests, [])

    def test_text_method_descriptor_failure_is_not_a_model_call(self):
        f, context = self.fixture()
        context = {**context, 'phase': 'review'}
        class BrokenDriver:
            @property
            def complete(self):
                raise RuntimeError('synthetic')
        f.rt.drivers['dev'] = BrokenDriver()
        self.collect(f, context)
        self.assertEqual(f.rt.task(f.task).token_usage, {})
        self.assertFalse(f.events('driver.model_called'))
        self.assertEqual(len(f.events('driver.invocation_skipped')), 1)
        self.assertEqual(len(f.events('driver.failed')), 1)

    def test_failed_or_invalid_correction_is_its_own_unknown_call(self):
        class Key(str): pass
        for mode in ('invalid', 'exception'):
            with self.subTest(mode=mode):
                f, context = self.fixture()
                f.driver.turns = [turn(text='invalid-json', tokens=5)]
                def correction(driver, **request):
                    driver.text_requests.append(request)
                    if mode == 'exception':
                        raise RuntimeError('synthetic')
                    return DriverResponse('not used', 30, {Key('contained'): True})
                with patch.object(FakeDriver, 'complete', correction), patch.dict('cater.runtime._TEXT_ONLY_CALLS', {FakeDriver: correction}):
                    result = self.collect(f, context)
                calls = f.events('workspace.model_called')
                self.assertEqual(len(calls), 2)
                self.assertEqual(calls[-1].payload['phase'], 'response_correction')
                self.assertEqual(calls[-1].payload['response_status'], mode if mode == 'invalid' else 'failed')
                self.assertEqual(result['event'].payload['usage_recorded_in'], [e.seq for e in calls])
                self.assertEqual(f.rt.task(f.task).token_usage, {'reported': 1, 'unavailable': 1})
                self.assertEqual(f.rt.task(f.task).tokens, 5)

    def test_driver_cannot_fake_host_skipped_marker_or_prior_receipt(self):
        f, context = self.fixture()
        prior = f.rt.log.emit(type='workspace.model_called', task_id=f.task, actor=HOST,
            payload={'role': f.role.id, 'driver': f.role.driver, 'attempt_id': context['attempt_id'],
                     'tokens': 2, 'token_usage': 'reported'})
        f.driver.turns = [turn(text=final_text(), tokens=3)]
        result = self.collect(f, context)
        self.assertNotIn(prior.seq, result['event'].payload['usage_recorded_in'])
        self.assertEqual(f.rt.task(f.task).token_usage, {'reported': 2})

    def test_text_driver_cannot_claim_workspace_receipt_or_skip(self):
        f, context = self.fixture()
        context = {**context, 'phase': 'review'}
        def complete(driver, **request):
            f.rt.log.emit(type='workspace.model_called', task_id=f.task, actor=HOST,
                payload={'role': f.role.id, 'driver': f.role.driver, 'attempt_id': context['attempt_id'],
                         'tokens': 2, 'token_usage': 'reported'})
            return DriverResponse(final_text(), 3, {'token_usage': 'reported',
                'workspace_usage_recorded': True, '_host_workspace_accounted': True,
                '_host_invocation_not_started': True, 'invocation_started': False})
        with patch.object(FakeDriver, 'complete', complete):
            result = self.collect(f, context)
        self.assertNotIn('usage_recorded_in', result['event'].payload)
        self.assertNotIn('usage_not_started_in', result['event'].payload)
        self.assertEqual(f.rt.task(f.task).token_usage, {'reported': 2})
        self.assertEqual(f.rt.task(f.task).tokens, 5)


if __name__ == '__main__':
    unittest.main()
