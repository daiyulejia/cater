"""Bounded native format correction with loopback and deterministic fixtures, not remote AI."""
from copy import deepcopy
import json
import sys
import unittest
from unittest.mock import patch

from cater.events import Actor
from cater.runtime import Runtime
from cater.team import HOST
from cater.team_response_correction import correctable
from cater.tool_protocol import ToolTurn, anthropic_turn, openai_turn
import test_protocol_usage as protocol
import test_simple_team as simple


PRIVATE = 'PRIVATE_INVALID_RESPONSE'


def bad(cls):
    data = protocol.response(cls, tool=True)
    if cls is protocol.AnthropicMessagesDriver:
        data['content'][0]['input'] = PRIVATE
        data['content'].insert(0, {'type': 'thinking', 'thinking': PRIVATE, 'signature': PRIVATE})
    else:
        data['choices'][0]['message']['tool_calls'][0]['function']['arguments'] = PRIVATE
        data['choices'][0]['message']['content'] = PRIVATE
    return data


def parsed_bad():
    return openai_turn(bad(protocol.OpenAICompatibleDriver), driver='fixture', names={'read_messages'}, seen=set())


def wire_tool(cls, name, arguments, call_id):
    data = protocol.response(cls, tool=True)
    if cls is protocol.AnthropicMessagesDriver:
        data['content'] = [{'type': 'tool_use', 'id': call_id, 'name': name, 'input': arguments}]
    else:
        data['choices'][0]['message']['tool_calls'] = [
            {'type': 'function', 'id': call_id, 'function': {'name': name, 'arguments': json.dumps(arguments)}}]
    return data


class TeamResponseCorrectionTests(unittest.TestCase):
    def fixture(self):
        fixture = simple.SimpleTeamTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.driver.tool_response_corrections = 1
        return fixture

    def start(self, fixture, *steps):
        fixture.driver.steps[fixture.manager] = list(steps)
        fixture.rt.team.receive(fixture.task, 'Continue this fixture input')
        message, role, started = fixture.rt.team._claim(fixture.task)
        running = fixture.rt.team._run_message(fixture.task, message, role, started)
        self.addCleanup(running.close)
        return running

    def test_corrects_once_through_native_http_and_retains_successful_history_and_usage(self):
        for cls in (protocol.OpenAICompatibleDriver, protocol.AnthropicMessagesDriver):
            publish = wire_tool(cls, 'publish_artifact', {'filename': 'kept.txt', 'content': 'Retain these actual bytes',
                'summary': 'Already saved', 'format': 'text'}, 'publish-once')
            if cls is protocol.AnthropicMessagesDriver:
                publish['content'].insert(0, {'type': 'thinking', 'thinking': 'SIGNED_ONLY', 'signature': 'signed-proof'})
            replies = [(200, publish), (200, bad(cls)), (200, protocol.response(cls))]
            with self.subTest(cls=cls.__name__), protocol.ProtocolUsageTests().session(
                    cls, replies, tool_response_corrections=1) as (f, _, requests):
                protocol.ProtocolUsageTests().run_message(f)
                self.assertEqual(len(requests), 3)
                calls = f.events('team.model_called')
                self.assertEqual(len(calls), 3)
                correction, = f.events('team.response_correction_requested')
                self.assertEqual(correction.payload['model_call_seq'], calls[1].seq)
                self.assertEqual(calls[2].payload['correction_of'], calls[1].seq)
                self.assertEqual(calls[2].payload['correction_attempt'], 1)
                finished, = f.events('team.run_finished')
                self.assertEqual(finished.payload['status'], 'completed')
                self.assertNotIn('response_status', finished.payload)
                self.assertEqual(finished.payload['correction_of'], calls[1].seq)
                self.assertEqual(len(f.events('team.tool_finished')), 1)
                self.assertEqual(len(f.rt.task(f.task).deliverables), 1)
                self.assertEqual(f.rt._reported_driver_tokens('team_test'), 42)
                self.assertEqual(f.events('team.objective_completed'), [], 'A corrected reply is not whole-task delivery')
                body = json.dumps(requests[2], ensure_ascii=False)
                self.assertIn('response_correction', body)
                self.assertIn('publish-once', body)
                self.assertIn('Retain these actual bytes', body)
                self.assertNotIn(PRIVATE, body)
                events = json.dumps([e.payload for e in f.rt.log.read(task_id=f.task)])
                self.assertNotIn(PRIVATE, events)
                self.assertNotIn('SIGNED_ONLY', events)
                if cls is protocol.AnthropicMessagesDriver:
                    self.assertIn('SIGNED_ONLY', body)
                    self.assertIn('signed-proof', body)
                f.rt.collaborate(f.task)
                self.assertEqual(len(requests), 3)

    def test_second_invalid_response_is_terminal_not_recursive(self):
        for cls in (protocol.OpenAICompatibleDriver, protocol.AnthropicMessagesDriver):
            with self.subTest(cls=cls.__name__), protocol.ProtocolUsageTests().session(
                    cls, [(200, bad(cls)), (200, bad(cls))], tool_response_corrections=1) as (f, _, requests):
                protocol.ProtocolUsageTests().run_message(f)
                self.assertEqual(len(requests), 2)
                self.assertEqual(len(f.events('team.response_correction_requested')), 1)
                self.assertEqual(f.events('team.tool_started'), [])
                finished, = f.events('team.run_finished')
                self.assertEqual(finished.payload['status'], 'failed')
                self.assertIn('已尝试一次格式纠正', finished.payload['reason'])
                self.assertEqual(f.rt._reported_driver_tokens('team_test'), 28)
                f.rt.collaborate(f.task)
                self.assertEqual(len(requests), 2)

    def test_successful_correction_does_not_reset_the_single_attempt_allowance(self):
        cls = protocol.OpenAICompatibleDriver
        replies = [(200, bad(cls)), (200, wire_tool(cls, 'read_messages', {}, 'read-after-correction')), (200, bad(cls))]
        with protocol.ProtocolUsageTests().session(cls, replies, tool_response_corrections=1) as (f, _, requests):
            protocol.ProtocolUsageTests().run_message(f)
            self.assertEqual(len(requests), 3)
            self.assertEqual(len(f.events('team.response_correction_requested')), 1)
            self.assertEqual(len(f.events('team.tool_finished')), 1)
            self.assertEqual(f.events('team.run_finished')[-1].payload['status'], 'failed')
            self.assertEqual(f.rt._reported_driver_tokens('team_test'), 42)

    def test_length_and_outer_transport_failures_do_not_get_format_correction(self):
        for cls in (protocol.OpenAICompatibleDriver, protocol.AnthropicMessagesDriver):
            data = bad(cls)
            if cls is protocol.OpenAICompatibleDriver:
                data['choices'][0]['finish_reason'] = 'length'
            else:
                data['stop_reason'] = 'max_tokens'
            for status, body in ((200, data), (503, {'error': PRIVATE})):
                with self.subTest(cls=cls.__name__, status=status), protocol.ProtocolUsageTests().session(
                        cls, [(status, body)], tool_response_corrections=1) as (f, _, requests):
                    protocol.ProtocolUsageTests().run_message(f)
                    self.assertEqual(len(requests), 1)
                    self.assertEqual(f.events('team.response_correction_requested'), [])
                    self.assertEqual(f.events('team.tool_started'), [])
                    self.assertEqual(f.events('team.run_finished')[-1].payload['status'], 'failed')
    def test_budget_reached_by_bad_response_does_not_schedule_or_call_correction(self):
        with protocol.ProtocolUsageTests().session(protocol.OpenAICompatibleDriver,
                [(200, bad(protocol.OpenAICompatibleDriver))], tool_response_corrections=1) as (f, driver, requests):
            driver.token_budget = 14
            protocol.ProtocolUsageTests().run_message(f)
            self.assertEqual(len(requests), 1)
            self.assertEqual(f.events('team.response_correction_requested'), [])
            self.assertEqual(f.events('team.run_finished')[0].payload['status'], 'interrupted')
            self.assertEqual(f.rt._reported_driver_tokens('team_test'), 14)

    def test_preflight_rechecks_changes_after_scheduling_before_new_model_request(self):
        for change in ('pause', 'cancel', 'customer', 'objective', 'remove', 'switch_off', 'budget'):
            with self.subTest(change=change):
                f = self.fixture()
                running = self.start(f, parsed_bad(), simple.turn('Must not be requested'))
                next(running)
                self.assertEqual(len(f.driver.calls), 1)
                self.assertEqual(len(f.events('team.response_correction_requested')), 1)
                if change in ('pause', 'cancel'):
                    f.rt.execution_control.change(f.task, change, request_id='correction-control-' + change)
                elif change == 'customer':
                    f.rt.team.receive(f.task, 'Use the new direction, not the old response')
                elif change == 'objective':
                    f.rt.log.emit(type='team.objective_updated', actor=HOST, task_id=f.task, payload={'goal': 'Different scope'})
                elif change == 'remove':
                    f.rt.conversation_list.remove(f.task, request_id='correction-remove-task')
                elif change == 'switch_off':
                    f.driver.tool_response_corrections = 0
                elif change == 'budget':
                    f.driver.token_budget = 14
                list(running)
                self.assertEqual(len(f.driver.calls), 1)
                self.assertEqual(f.events('team.run_finished')[-1].payload['status'], 'interrupted')
                self.assertEqual(f.events('team.tool_started'), [])
                self.assertTrue(f.rt.verify_chain().ok)

    def test_context_scope_and_unknown_effect_rechecks_stop_correction(self):
        for change in ('scope', 'pending'):
            with self.subTest(change=change):
                f = self.fixture()
                running = self.start(f, parsed_bad(), simple.turn('Must not be requested'))
                next(running)
                if change == 'pending':
                    with patch.object(f.rt.team, '_pending_operations', return_value=[{'status': 'unknown'}]):
                        list(running)
                else:
                    original = f.rt.team._model_input
                    def changed(*args, **kwargs):
                        context, workspace, binding, tools, basis = original(*args, **kwargs)
                        context['work_checkpoint']['scope']['rule_fingerprint'] = 'changed-after-original-request'
                        return context, workspace, binding, tools, basis
                    with patch.object(f.rt.team, '_model_input', side_effect=changed):
                        list(running)
                self.assertEqual(len(f.driver.calls), 1)
                self.assertEqual(f.events('team.run_finished')[-1].payload['status'], 'interrupted')

    def test_correction_request_failure_does_not_inherit_old_containment_diagnosis(self):
        cls = protocol.OpenAICompatibleDriver
        with protocol.ProtocolUsageTests().session(cls, [(200, bad(cls)), (503, {'error': PRIVATE})],
                tool_response_corrections=1) as (f, _, requests):
            protocol.ProtocolUsageTests().run_message(f)
            finished, = f.events('team.run_finished')
            self.assertEqual(len(requests), 2)
            self.assertEqual(finished.payload['failure_kind'], 'http_error')
            self.assertEqual(finished.payload['status_code'], 503)
            self.assertNotIn('response_status', finished.payload)
            self.assertIn('HTTP 503', finished.payload['reason'])
            self.assertEqual(len(f.events('team.model_called')), 1)
            self.assertEqual(finished.payload['correction_attempt'], 1)

    def test_new_customer_input_committed_during_final_preflight_blocks_http_correction(self):
        for cls in (protocol.OpenAICompatibleDriver, protocol.AnthropicMessagesDriver):
            with self.subTest(cls=cls.__name__), protocol.ProtocolUsageTests().session(
                    cls, [(200, bad(cls)), (200, protocol.response(cls))], tool_response_corrections=1) as (f, driver, requests):
                f.rt.team.receive(f.task, 'Start original fixture input')
                message, role, started = f.rt.team._claim(f.task)
                running = f.rt.team._run_message(f.task, message, role, started)
                self.addCleanup(running.close)
                next(running)
                original = f.rt.team._pending_operations
                committed = []
                def pending_then_new_input(task_id):
                    result = original(task_id)
                    caller = sys._getframe(1)
                    while caller is not None and caller.f_code.co_name != '_run_message':
                        caller = caller.f_back
                    if (caller is not None
                            and caller.f_locals.get('failure_stage') == 'model_request'
                            and caller.f_locals.get('correction') is not None and not committed):
                        with Runtime(f.root / 'test.db', roles_path=f.roles, drivers={'team_test': driver},
                                     close_drivers_on_close=False) as other:
                            event = other.team.receive(f.task, 'Use the new direction instead', request_id='new-input-during-correction')
                            committed.append(event.seq)
                    return result
                with patch.object(f.rt.team, '_pending_operations', side_effect=pending_then_new_input):
                    list(running)
                self.assertEqual(len(committed), 1, 'The second Runtime must commit at the final preflight')
                self.assertEqual(len(requests), 1)
                self.assertEqual(f.rt._reported_driver_tokens('team_test'), 14)
                self.assertEqual(f.events('team.tool_started'), [])
                self.assertEqual(f.events('team.run_finished')[-1].payload['status'], 'interrupted')
                self.assertTrue(f.rt.verify_chain().ok)

    def test_turn_limit_does_not_schedule_correction_or_fresh_supervision(self):
        f = self.fixture()
        with patch('cater.team.MAX_ACTOR_TURNS', 1):
            list(self.start(f, parsed_bad()))
        self.assertEqual(len(f.driver.calls), 1)
        self.assertEqual(f.events('team.response_correction_requested'), [])
        self.assertEqual(f.events('team.run_finished')[-1].payload['status'], 'failed')
        self.assertFalse([e for e in f.rt.team._messages(f.task) if e.payload.get('kind') == 'recovery'])

    def test_missing_or_unsafe_correction_metadata_cannot_authorize_extra_call(self):
        f = self.fixture()
        source = parsed_bad()
        for setting in (None, True, False, '1', 1.0, 2, 0):
            f.driver.tool_response_corrections = setting
            self.assertFalse(correctable(f.driver, source))
        f.driver.tool_response_corrections = 1
        self.assertTrue(correctable(f.driver, source))
        for mutate in ('contained', 'incomplete', 'invocation_started', 'finish_reason', 'mode', 'reason', 'tool_issue', 'text', 'calls', 'message'):
            altered = deepcopy(source)
            if mutate in ('text', 'calls', 'message'):
                altered = ToolTurn(PRIVATE if mutate == 'text' else '', ['not-a-call'] if mutate == 'calls' else [],
                    meta=source.meta, message={'role': 'assistant', 'content': PRIVATE} if mutate == 'message' else source.message)
            else:
                altered.meta[mutate] = PRIVATE if mutate not in ('contained', 'incomplete') else 1
            self.assertFalse(correctable(f.driver, altered), mutate)


if __name__ == '__main__':
    unittest.main()
