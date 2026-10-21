"""Parser-derived safe fault categories; HTTP fixtures are not remote models."""
from copy import deepcopy
import json
import unittest

from cater.tool_protocol import anthropic_turn, contained_response_diagnostics, openai_turn
from cater.runtime import Runtime
from cater.team import _contained_reason
import test_protocol_usage as protocol


PRIVATE = 'PRIVATE_TOOL_RESPONSE_CONTENT'


def openai_cases():
    def case(issue, change, seen=None):
        data = protocol.response(protocol.OpenAICompatibleDriver, tool=True)
        message = data['choices'][0]['message']
        change(message)
        return issue, data, seen or set()

    def call(message):
        return message['tool_calls'][0]

    yield case('calls_not_array', lambda m: m.update(tool_calls=PRIVATE))
    yield case('invalid_call_shape', lambda m: m.update(tool_calls=[PRIVATE]))
    yield case('unsupported_call_type', lambda m: call(m).update(type=PRIVATE))
    yield case('invalid_function', lambda m: call(m).update(function=PRIVATE))
    yield case('invalid_call_id', lambda m: call(m).update(id=' '))
    yield case('duplicate_call_id', lambda m: None, {'read-1'})
    yield case('invalid_tool_name', lambda m: call(m)['function'].update(name=PRIVATE + '/invalid'))
    yield case('unregistered_tool', lambda m: call(m)['function'].update(name=PRIVATE))
    yield case('arguments_not_text', lambda m: call(m)['function'].update(arguments={'secret': PRIVATE}))
    for arguments in (PRIVATE, '{"limit":1,"limit":2}', '{"limit":NaN}', '{"limit":1e999}'):
        yield case('invalid_arguments_json', lambda m: call(m)['function'].update(arguments=arguments))
    for arguments in ('null', '[]', 'true', '1', '"' + PRIVATE + '"'):
        yield case('arguments_not_object', lambda m: call(m)['function'].update(arguments=arguments))


def anthropic_cases():
    def case(issue, change, seen=None):
        data = protocol.response(protocol.AnthropicMessagesDriver, tool=True)
        change(data['content'][0])
        return issue, data, seen or set()

    yield case('invalid_call_shape', lambda c: c.update(extra=PRIVATE))
    yield case('invalid_call_id', lambda c: c.update(id=None))
    yield case('duplicate_call_id', lambda c: None, {'read-1'})
    yield case('invalid_tool_name', lambda c: c.update(name=PRIVATE + '/invalid'))
    yield case('unregistered_tool', lambda c: c.update(name=PRIVATE))
    yield case('arguments_not_object', lambda c: c.update(input=PRIVATE))
    # These cannot arrive through strict HTTP JSON, but direct adapters must fail safely too.
    yield case('invalid_arguments_value', lambda c: c.update(input={'limit': float('inf')}))


class ToolResponseIssueTests(unittest.TestCase):
    def test_real_parsers_identify_fault_without_exposing_content_or_mutating_inputs(self):
        for parser, cases, mode, reason in (
                (openai_turn, openai_cases, 'openai_compatible', 'invalid_tool_calls'),
                (anthropic_turn, anthropic_cases, 'anthropic', 'invalid_content')):
            for issue, data, seen in cases():
                with self.subTest(protocol=mode, issue=issue):
                    original = json.dumps(data, sort_keys=True)
                    original_seen = set(seen)
                    turn = parser(data, driver='fixture', names={'read_messages'}, seen=seen)
                    self.assertEqual((turn.text, turn.calls, turn.tokens), ('', [], 14))
                    self.assertEqual(turn.meta.get('tool_issue'), issue)
                    self.assertEqual(turn.meta['reason'], reason)
                    self.assertEqual(contained_response_diagnostics(turn.meta)['response_tool_issue'], issue)
                    self.assertNotIn(PRIVATE, json.dumps(turn.meta))
                    self.assertEqual(json.dumps(data, sort_keys=True), original)
                    self.assertEqual(seen, original_seen)

    def test_partially_valid_batch_is_fully_contained_in_both_protocols(self):
        for cls, parser in ((protocol.OpenAICompatibleDriver, openai_turn),
                            (protocol.AnthropicMessagesDriver, anthropic_turn)):
            data = protocol.response(cls, tool=True)
            values = data['content'] if cls is protocol.AnthropicMessagesDriver else data['choices'][0]['message']['tool_calls']
            values.append(deepcopy(values[0]))
            seen = set()
            turn = parser(data, driver='fixture', names={'read_messages'}, seen=seen)
            self.assertEqual(turn.calls, [])
            self.assertEqual(turn.meta.get('tool_issue'), 'duplicate_call_id')
            self.assertEqual(seen, set())

    def test_usage_and_fault_survive_actual_loopback_adapter_host_and_reopen(self):
        for cls, cases in ((protocol.OpenAICompatibleDriver, openai_cases),
                           (protocol.AnthropicMessagesDriver, anthropic_cases)):
            for issue, data, seen in cases():
                if seen or issue == 'invalid_arguments_value':
                    continue
                with self.subTest(protocol=cls.__name__, issue=issue), protocol.ProtocolUsageTests().session(
                        cls, [(200, data)]) as (fixture, driver, requests):
                    fixture.rt.team.receive(fixture.task, 'Inspect fixture response')
                    fixture.rt.collaborate(fixture.task)
                    call, = fixture.events('team.model_called')
                    finished, = fixture.events('team.run_finished')
                    for event in (call, finished):
                        self.assertEqual(event.payload['response_tool_issue'], issue)
                        self.assertIs(event.payload['response_client_tools_executed'], False)
                    self.assertEqual(call.payload['tokens'], 14)
                    self.assertEqual(fixture.rt._reported_driver_tokens('team_test'), 14)
                    self.assertEqual(fixture.events('team.tool_started'), [])
                    self.assertEqual(fixture.events('team.objective_completed'), [])
                    self.assertFalse(fixture.rt.team._pending_operations(fixture.task))
                    self.assertNotIn(PRIVATE, json.dumps([e.payload for e in fixture.rt.log.read(task_id=fixture.task)]))
                    fixture.rt.collaborate(fixture.task)
                    self.assertEqual(len(requests), 1, 'Diagnostics do not authorize another model request')
                    self.assertTrue(fixture.rt.verify_chain().ok)
                    with Runtime(fixture.root / 'test.db', roles_path=fixture.roles,
                                 drivers={'team_test': driver}) as reopened:
                        saved = [e for e in reopened.log.read(task_id=fixture.task)
                                 if e.type == 'team.run_finished']
                        self.assertEqual(saved[0].payload['response_tool_issue'], issue)
                        self.assertEqual(reopened._reported_driver_tokens('team_test'), 14)
                        self.assertTrue(reopened.verify_chain().ok)

    def test_helper_requires_exact_protocol_reason_and_allowlisted_issue(self):
        base = {'contained': True, 'mode': 'openai_compatible', 'reason': 'invalid_tool_calls',
                'finish_reason': 'tool_calls', 'tool_issue': 'invalid_arguments_json'}
        snapshot = deepcopy(base)
        self.assertEqual(contained_response_diagnostics(base)['response_tool_issue'], 'invalid_arguments_json')
        self.assertEqual(base, snapshot)
        for change in ({'contained': 1}, {'mode': PRIVATE}, {'reason': 'incomplete_stop'},
                       {'reason': 'invalid_content'}, {'tool_issue': PRIVATE}, {'tool_issue': []},
                       {'tool_issue': True}, {'tool_issue': {}}, {'tool_issue': None}):
            with self.subTest(change=change):
                self.assertNotIn('response_tool_issue', contained_response_diagnostics({**base, **change}))
        native = {**base, 'mode': 'anthropic', 'reason': 'invalid_content', 'tool_issue': 'arguments_not_object'}
        self.assertEqual(contained_response_diagnostics(native)['response_tool_issue'], 'arguments_not_object')
        self.assertNotIn('response_tool_issue', contained_response_diagnostics({**native, 'reason': 'invalid_tool_calls'}))

    def test_old_and_unrelated_responses_do_not_acquire_tool_faults(self):
        old = {'contained': True, 'mode': 'openai_compatible', 'reason': 'invalid_tool_calls'}
        self.assertNotIn('response_tool_issue', contained_response_diagnostics(old))
        for cls, parser in ((protocol.OpenAICompatibleDriver, openai_turn),
                            (protocol.AnthropicMessagesDriver, anthropic_turn)):
            data = protocol.response(cls)
            data['tool_issue'] = 'unregistered_tool'
            turn = parser(data, driver='fixture', names=set(), seen=set())
            self.assertFalse(turn.meta.get('contained'))
            self.assertNotIn('tool_issue', turn.meta)
            data = protocol.response(cls, tool=True)
            if cls is protocol.OpenAICompatibleDriver:
                data['choices'][0]['finish_reason'] = 'length'
            else:
                data['stop_reason'] = 'max_tokens'
            data['tool_issue'] = 'unregistered_tool'
            turn = parser(data, driver='fixture', names=set(), seen=set())
            self.assertEqual(turn.meta['reason'], 'incomplete_stop')
            self.assertNotIn('tool_issue', turn.meta)

    def test_chinese_feedback_uses_only_matching_fixed_codes(self):
        failure = {'response_status': 'contained', 'response_reason': 'invalid_tool_calls',
                   'response_protocol': 'openai_compatible', 'response_tool_issue': 'unregistered_tool'}
        reason = _contained_reason('店长', failure)
        self.assertIn('请求了本轮未提供的工具', reason)
        for value in (PRIVATE, [], {}, True, None):
            message = _contained_reason('店长', {**failure, 'response_tool_issue': value})
            self.assertNotIn(PRIVATE, message)
            self.assertNotIn('请求了本轮未提供的工具', message)
        for change in ({'response_protocol': 'anthropic'}, {'response_reason': 'incomplete_stop'},
                       {'response_status': 'protocol_error'}):
            message = _contained_reason('店长', {**failure, **change})
            self.assertNotIn('请求了本轮未提供的工具', message)

    def test_invalid_mixed_batch_cannot_publish_or_set_objective(self):
        for cls in (protocol.OpenAICompatibleDriver, protocol.AnthropicMessagesDriver):
            data = protocol.response(cls, tool=True)
            if cls is protocol.AnthropicMessagesDriver:
                data['content'][0].update(name='set_objective', input={'goal': PRIVATE})
                data['content'].append({'type': 'tool_use', 'id': 'bad-2', 'name': PRIVATE, 'input': {}})
            else:
                values = data['choices'][0]['message']['tool_calls']
                values[0]['function'].update(name='set_objective', arguments=json.dumps({'goal': PRIVATE}))
                values.append({'type': 'function', 'id': 'bad-2', 'function': {'name': PRIVATE, 'arguments': '{}'}})
            with protocol.ProtocolUsageTests().session(cls, [(200, data)]) as (fixture, _, requests):
                fixture.rt.team.receive(fixture.task, 'Test batch atomic containment')
                fixture.rt.collaborate(fixture.task)
                self.assertEqual(fixture.events('team.objective_updated'), [])
                self.assertEqual(fixture.events('team.tool_started'), [])
                self.assertEqual(fixture.rt.task(fixture.task).deliverables, [])
                self.assertEqual(fixture.events('team.run_finished')[0].payload['response_tool_issue'], 'unregistered_tool')
                self.assertEqual(len(requests), 1)


if __name__ == '__main__':
    unittest.main()
