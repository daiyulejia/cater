"""Transport liveness is separate from host evidence and the fixed total budget."""
import unittest
import tempfile
from unittest.mock import patch

from cater.acp import AcpSession, AcpProtocolError
from cater.external_agent import _ProgressBudget


class ActivityBudgetTests(unittest.TestCase):
    def test_private_stream_observer_gets_no_content_and_rejects_foreign_session(self):
        with tempfile.TemporaryDirectory() as root:
            observed, updates = [], []
            session = AcpSession(['never-start'], cwd=root, on_activity=lambda: observed.append('alive'))
            self.addCleanup(session.close)
            session.session_id = 'owned'
            body = {'sessionId': 'foreign', 'update': {'sessionUpdate': 'agent_thought_chunk',
                    'content': {'type': 'text', 'text': 'PRIVATE_ACTUAL_STREAM'}}}
            with self.assertRaises(AcpProtocolError):
                session._session_update(body, starting_session=False, on_update=updates.append)
            self.assertEqual(observed, [])
            body['sessionId'] = 'owned'
            session._session_update(body, starting_session=False, on_update=updates.append)
            self.assertEqual(observed, ['alive'])
            self.assertEqual(updates, [])
            body['update']['content']['text'] = ' '
            session._session_update(body, starting_session=False, on_update=updates.append)
            self.assertEqual(observed, ['alive'])

    def test_slow_generation_is_alive_without_claiming_progress_or_extending_total(self):
        with patch('cater.external_agent.time.monotonic', return_value=0) as clock:
            budget = _ProgressBudget(180, 540)
            for timestamp in (150, 300, 450):
                clock.return_value = timestamp
                self.assertTrue(budget.observed({'sessionUpdate': 'agent_message_chunk',
                    'content': {'type': 'text', 'text': 'Generated output'}}))
            self.assertEqual(budget.progress_at, 0)
            self.assertEqual(budget.seen, set())
            clock.return_value = 540
            self.assertEqual(budget.expired().limit, 'total')
            self.assertFalse(budget.observed({'sessionUpdate': 'agent_message_chunk',
                'content': {'type': 'text', 'text': 'Late output'}}))

    def test_empty_private_plan_and_repeated_status_do_not_renew_idle(self):
        with patch('cater.external_agent.time.monotonic', return_value=0) as clock:
            budget = _ProgressBudget(2, 8)
            clock.return_value = 1
            update = {'sessionUpdate': 'tool_call_update', 'toolCallId': 'write',
                      'rawInput': {'content': 'first part'}, 'status': 'in_progress'}
            self.assertTrue(budget.observed(update))
            clock.return_value = 2
            for empty in (update, {'sessionUpdate': 'agent_thought_chunk', 'content': {'type': 'text', 'text': 'private'}},
                          {'sessionUpdate': 'plan'}, {'sessionUpdate': 'tool_call'},
                          {'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': '  '}},
                          {'sessionUpdate': 'tool_call', 'toolCallId': 'x', 'rawInput': float('nan')}):
                self.assertFalse(budget.observed(empty))
            self.assertEqual(budget.activity_at, 1)
            clock.return_value = 3
            self.assertEqual(budget.expired().limit, 'idle')

    def test_new_tool_arguments_are_activity_not_execution(self):
        with patch('cater.external_agent.time.monotonic', return_value=0) as clock:
            budget = _ProgressBudget(2, 6)
            for timestamp, body in ((1, 'part'), (2.5, 'part two'), (4, 'part two three')):
                clock.return_value = timestamp
                self.assertTrue(budget.observed({'sessionUpdate': 'tool_call_update',
                    'toolCallId': 'write', 'rawInput': {'content': body}}))
            self.assertEqual(budget.progress_at, 0)
            self.assertEqual(budget.seen, set())
            clock.return_value = 6
            self.assertEqual(budget.expired().limit, 'total')

    def test_interleaved_repeated_calls_do_not_keep_a_stalled_run_alive(self):
        with patch('cater.external_agent.time.monotonic', return_value=0) as clock:
            budget = _ProgressBudget(2, 8)
            calls = [{'sessionUpdate': 'tool_call_update', 'toolCallId': identity,
                      'status': 'in_progress'} for identity in ('A', 'B')]
            clock.return_value = 1
            for call in calls:
                self.assertTrue(budget.observed(call))
            clock.return_value = 2
            for call in calls * 3:
                self.assertFalse(budget.observed(call))
            clock.return_value = 3
            self.assertEqual(budget.expired().limit, 'idle')

    def test_tool_activity_tracking_is_bounded_without_evicting_repeat_detection(self):
        with patch('cater.external_agent.time.monotonic', return_value=0) as clock:
            budget = _ProgressBudget(2, 8)
            clock.return_value = 1
            for identity in range(256):
                self.assertTrue(budget.observed({'sessionUpdate': 'tool_call', 'toolCallId': str(identity),
                    'status': 'in_progress'}))
            clock.return_value = 2
            self.assertFalse(budget.observed({'sessionUpdate': 'tool_call', 'toolCallId': 'new',
                'status': 'in_progress'}))
            self.assertFalse(budget.observed({'sessionUpdate': 'tool_call', 'toolCallId': '0',
                'status': 'in_progress'}))
            self.assertEqual(len(budget._tool_updates), 256)
            self.assertTrue(budget.observed({'sessionUpdate': 'tool_call', 'toolCallId': '0',
                'status': 'completed'}))
            self.assertEqual(budget.activity_at, 2)

    def test_escaped_surrogate_identifier_does_not_crash_or_bypass_repeat_detection(self):
        with patch('cater.external_agent.time.monotonic', return_value=0) as clock:
            budget = _ProgressBudget(2, 8)
            clock.return_value = 1
            update = {'sessionUpdate': 'tool_call_update', 'toolCallId': '\ud800',
                      'status': 'in_progress'}
            self.assertTrue(budget.observed(update))
            clock.return_value = 2
            self.assertFalse(budget.observed(update))
            self.assertEqual(budget.activity_at, 1)
            self.assertEqual(budget.progress_at, 0)
            self.assertEqual(budget.seen, set())
            clock.return_value = 3
            self.assertEqual(budget.expired().limit, 'idle')
