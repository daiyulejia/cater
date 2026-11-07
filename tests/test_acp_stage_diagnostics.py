"""Offline ACP and MCP observations: local facts, never peer/model proof."""

import json
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from cater.acp import AcpSession, AcpTimeout, AcpProtocolError, MAX_FRAME_BYTES
from cater.external_agent import QoderAcpDriver
from cater.external_catalog import GUIDANCE
from cater.tool_protocol import json_utf8
import test_external_tool_observations as observations
import test_external_cooperative as cooperative
import test_mcp_host as mcp_tests


class StageDiagnosticsTests(unittest.TestCase):
    def test_real_acp_prompt_size_and_private_public_counters(self):
        with tempfile.TemporaryDirectory() as root:
            class Session(AcpSession):
                def __init__(self, command, **kwargs):
                    super().__init__([sys.executable, '-B', '-u', '-c', observations.PEER,
                        'success', json.dumps(observations.sensitive_update())], **kwargs)

            tools = [{'name': 'save', 'description': 'fixture', 'input_schema': {'type': 'object'}}]
            driver = QoderAcpDriver('fixture', 'unused', 'ExactModel', timeout=5, tool_surface='catalog')
            run = driver.agent_run(cwd=root, tools=tools, execute=lambda call: {},
                                   check_current=lambda: None, on_update=None)
            prompt = '中文🔒 PRIVATE_INPUT_SENTINEL'
            with patch('cater.external_agent.AcpSession', Session):
                run.run(prompt)
            facts = dict(run.state.observations)
            actual = GUIDANCE + prompt
            self.assertEqual(facts['acp_diagnostics_version'], 2)
            self.assertEqual(facts['acp_prompt_chars'], len(actual))
            self.assertEqual(facts['acp_prompt_utf8_bytes'], len(actual.encode('utf-8')))
            self.assertEqual((facts['acp_prompt_send_attempted'], facts['acp_prompt_send_completed']), (1, 1))
            self.assertEqual(facts['acp_prompt_pipe_write_attempted'], 1)
            self.assertEqual(facts['private_thought_activity_count'], 1)
            self.assertEqual(facts['public_tool_call_update_count'], 3)
            for secret in ('PRIVATE_INPUT_SENTINEL', observations.PRIVATE, observations.PRIVATE_ID,
                           'PRIVATE_THOUGHT_SENTINEL', root):
                self.assertNotIn(secret, repr(facts))
            self.assertTrue(all(type(v) is int and v >= 0 for v in facts.values()))

    def test_send_failure_is_not_claimed_completed(self):
        with tempfile.TemporaryDirectory() as root:
            client = AcpSession(['unused'], cwd=root, timeout=2)
            client.session_id = 'offline'
            self.addCleanup(client.close)
            self.assertEqual(client.prompt_send_observation, (False, False, False))
            with patch.object(client, '_send', side_effect=AcpTimeout('fixture', phase='input')):
                with self.assertRaises(AcpTimeout):
                    client.prompt('fixture')
            self.assertEqual(client.prompt_send_observation, (True, False, False))

    def test_run_reports_failed_prompt_write_without_exposing_exception(self):
        with tempfile.TemporaryDirectory() as root:
            class Session(AcpSession):
                def __init__(self, command, **kwargs):
                    super().__init__([sys.executable, '-B', '-u', '-c', observations.PEER,
                        'zero', '{}'], **kwargs)

                def _send(self, value, deadline, **kwargs):
                    if isinstance(value, dict) and value.get('method') == 'session/prompt':
                        raise AcpTimeout('PRIVATE_SEND_ERROR', phase='input')
                    return super()._send(value, deadline, **kwargs)

            driver = QoderAcpDriver('fixture', 'unused', 'ExactModel', timeout=5)
            run = driver.agent_run(cwd=root, tools=[], execute=lambda call: {},
                                   check_current=lambda: None, on_update=None)
            with patch('cater.external_agent.AcpSession', Session):
                with self.assertRaises(AcpTimeout):
                    run.run('private prompt')
            facts = dict(run.state.observations)
            self.assertEqual((facts['acp_prompt_send_attempted'], facts['acp_prompt_send_completed']), (1, 0))
            self.assertEqual(facts['acp_prompt_pipe_write_attempted'], 0)
            self.assertNotIn('PRIVATE_SEND_ERROR', repr(facts))

    def test_frame_limit_rejection_occurs_before_pipe_write(self):
        with tempfile.TemporaryDirectory() as root:
            client = AcpSession(['unused'], cwd=root, timeout=2)
            client.session_id = 'offline'
            self.addCleanup(client.close)
            params = {'sessionId': 'offline', 'prompt': [{'type': 'text', 'text': ''}]}
            overhead = len(client._encode(params)) - 1
            prompt = 'x' * (MAX_FRAME_BYTES - overhead)
            self.assertEqual(len(client._encode({**params, 'prompt': [{'type': 'text', 'text': prompt}]})) - 1,
                             MAX_FRAME_BYTES)
            with patch('cater.acp.threading.Thread') as writer:
                with self.assertRaises(AcpProtocolError) as caught:
                    client.prompt(prompt)
            writer.assert_not_called()
            self.assertEqual(client.prompt_send_observation, (True, False, False))
            # Existing conservative exception semantics do not authorize replay.
            self.assertTrue(caught.exception.prompt_may_have_run)

    def test_real_peer_frame_limit_records_entered_flow_without_pipe_write(self):
        with tempfile.TemporaryDirectory() as root:
            class Session(AcpSession):
                def __init__(self, command, **kwargs):
                    super().__init__([sys.executable, '-B', '-u', '-c', observations.PEER,
                        'zero', '{}'], **kwargs)

            params = {'sessionId': 'offline-tools', 'prompt': [{'type': 'text', 'text': ''}]}
            prompt = 'x' * (MAX_FRAME_BYTES - len(json_utf8(params)))
            driver = QoderAcpDriver('fixture', 'unused', 'ExactModel', timeout=5)
            run = driver.agent_run(cwd=root, tools=[], execute=lambda call: {},
                                   check_current=lambda: None, on_update=None)
            with patch('cater.external_agent.AcpSession', Session):
                with self.assertRaises(AcpProtocolError) as caught:
                    run.run(prompt)
            facts = dict(run.state.observations)
            self.assertEqual(facts['acp_prompt_send_attempted'], 1)
            self.assertEqual(facts['acp_prompt_pipe_write_attempted'], 0)
            self.assertEqual(facts['acp_prompt_send_completed'], 0)
            self.assertTrue(caught.exception.prompt_may_have_run)
            self.assertTrue(run.state.cleanup_complete)

    def test_dispatched_writer_without_observed_entry_is_unknown(self):
        with tempfile.TemporaryDirectory() as root:
            client = AcpSession(['unused'], cwd=root, timeout=2)
            self.addCleanup(client.close)
            client._prompt_send_attempted = True
            client._prompt_writer_dispatched = True
            self.assertEqual(client.prompt_send_observation, (True, None, False))

    def test_delayed_writer_can_start_after_timeout_snapshot(self):
        with tempfile.TemporaryDirectory() as root:
            client = AcpSession(['unused'], cwd=root, timeout=2)
            gate = threading.Event()
            pipe = Mock()
            client._proc = SimpleNamespace(stdin=pipe)
            real_thread = threading.Thread

            class DelayedWriter(real_thread):
                def run(self):
                    gate.wait(2)
                    super().run()

            try:
                with patch('cater.acp.threading.Thread', DelayedWriter):
                    with self.assertRaises(AcpTimeout):
                        client._send({'method': 'session/prompt'}, time.monotonic() + 0.1)
                self.assertEqual(client.prompt_send_observation, (False, None, False))
                pipe.write.assert_not_called()
                gate.set()
                client._writers[0].join(2)
                self.assertFalse(client._writers[0].is_alive())
                pipe.write.assert_called_once()
                self.assertEqual(client.prompt_send_observation, (False, True, False))
            finally:
                gate.set()
                for writer in client._writers:
                    writer.join(2)
                client._proc = None
                client.close()

    def test_private_activity_requires_valid_nonempty_text_and_session(self):
        with tempfile.TemporaryDirectory() as root:
            calls = []
            session = AcpSession(['unused'], cwd=root, on_activity=lambda: calls.append(1))
            session.session_id = 'offline'
            self.addCleanup(session.close)
            for content in ({'type': 'text', 'text': ''}, {'type': 'text', 'text': ' '},
                            {'type': 'image', 'text': 'private'}, {'type': 'text', 'text': 'private'}):
                session._session_update({'sessionId': 'offline', 'update': {
                    'sessionUpdate': 'agent_thought_chunk', 'content': content}},
                    starting_session=False, on_update=None)
            self.assertEqual(len(calls), 1)

    def test_real_loopback_counts_only_tool_responses(self):
        fixture = mcp_tests.McpToolBridgeTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        bridge = fixture.bridge()
        durations = []
        bridge.observe_tool_responses(durations.append)
        peer = fixture.session(bridge)
        fixture.exchange(bridge, peer, fixture.request(1, 'tools/list'))
        self.assertEqual(durations, [])
        fixture.exchange(bridge, peer, fixture.request(2, 'tools/call', name='save_note',
                                                       arguments={'text': 'PRIVATE'}))
        fixture.exchange(bridge, peer, fixture.request(3, 'tools/call', name='not_registered'))
        self.assertEqual(len(durations), 2)  # Includes explicit tool errors.
        self.assertTrue(all(type(d) is int and d >= 0 for d in durations))

    def test_partial_response_and_failed_write_do_not_claim_completion(self):
        for fail in (False, True):
            with self.subTest(fail=fail):
                fixture = cooperative.ExternalCooperativeTests()
                fixture.setUp()
                self.addCleanup(fixture.doCleanups)
                fixture.return_early = True
                iterator = fixture.controlled()
                fixture.advance_to_receipt(iterator)
                self.assertNotIn('bridge_tool_responses_written', dict(fixture.run.state.observations))
                if fail:
                    fixture.socket.failure = OSError('private failure')
                    with self.assertRaises(cooperative.ToolBridgeError):
                        cooperative.drain(iterator)
                    self.assertNotIn('bridge_tool_responses_written', dict(fixture.run.state.observations))
                else:
                    cooperative.drain(iterator)
                    facts = dict(fixture.run.state.observations)
                    self.assertEqual(facts['bridge_tool_responses_written'], 1)
                    self.assertGreaterEqual(facts['bridge_tool_response_write_ms'], 0)
                    self.assertNotIn('private failure', repr(facts))


if __name__ == '__main__':
    unittest.main()
