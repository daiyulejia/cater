"""Real pipe waits yield on the calling thread without replaying ACP requests."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from cater.acp import AcpSession, AcpTimeout, AcpUnavailable, _exhaust
from cater.mcp_host import McpToolBridge
import test_acp_duplex as duplex


PEER = Path(__file__).resolve().parent / 'fixtures/acp_peer.py'


class AcpCooperativeTests(unittest.TestCase):
    def session(self, mode):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        client = AcpSession([sys.executable, '-B', '-u', str(PEER), mode, str(root)], cwd=root, timeout=3)
        self.addCleanup(client.close)
        return client, root

    def until(self, iterator, predicate):
        limit = time.monotonic() + 4
        count = 0
        while not predicate():
            self.assertLess(time.monotonic(), limit, 'Protocol gate not reached')
            self.assertIsNone(next(iterator))
            count += 1
        self.assertGreater(count, 0)

    def test_startup_wait_allows_another_session_to_open_and_prompt(self):
        first, root = self.session('initialize-timeout')
        second, _ = self.session('normal')
        owner = threading.get_ident()
        threads = []
        pending = first.iter_open(service=lambda: threads.append(threading.get_ident()))
        self.addCleanup(pending.close)
        self.until(pending, lambda: (root / 'wire.jsonl').exists()
                   and 'initialize' in (root / 'wire.jsonl').read_text())
        self.assertIsNone(first.session_id)
        self.assertEqual(_exhaust(second.iter_open())['sessionId'], 'fixture-session')
        self.assertEqual(_exhaust(second.iter_prompt('second can finish')).stop_reason, 'end_turn')
        (root / 'release').touch()
        self.assertEqual(_exhaust(pending)['sessionId'], 'fixture-session')
        self.assertEqual(set(threads), {owner})
        wire = [json.loads(line) for line in (root / 'wire.jsonl').read_text().splitlines()]
        self.assertEqual(sum(e.get('method') == 'initialize' for e in wire), 1)

    def test_prompt_yields_and_close_aborts_without_replay(self):
        client, root = self.session('hold')
        client.open()
        updates = []
        pending = client.iter_prompt('held once', on_update=updates.append)
        self.until(pending, lambda: bool(updates))
        self.assertIsNone(client._proc.poll())
        with self.assertRaises(AcpUnavailable):
            client.prompt('cannot run concurrently')
        pending.close()
        self.assertIsNotNone(client._proc.poll())
        self.assertFalse(client._prompt_active)
        wire = [json.loads(line) for line in (root / 'wire.jsonl').read_text().splitlines()]
        self.assertEqual(sum(e.get('method') == 'session/prompt' for e in wire), 1)
        with self.assertRaises(AcpUnavailable):
            _exhaust(client.iter_prompt('cannot replay'))

    def test_deadline_continues_while_iterator_is_not_scheduled(self):
        client, _ = self.session('hold')
        client.open()
        pending = client.iter_prompt('deadline', timeout=0.03)
        next(pending)
        time.sleep(0.05)
        with self.assertRaises(AcpTimeout) as caught:
            _exhaust(pending)
        self.assertTrue(caught.exception.prompt_may_have_run)
        self.assertIsNotNone(client._proc.poll())

    def test_control_error_keeps_identity_and_stops_process(self):
        client, _ = self.session('normal')
        client.open()
        marker = KeyboardInterrupt('synthetic host control')
        def stop():
            raise marker
        with self.assertRaises(KeyboardInterrupt) as caught:
            _exhaust(client.iter_prompt('stop', service=stop))
        self.assertIs(caught.exception, marker)
        self.assertIsNotNone(client._proc.poll())

    def test_private_chunks_and_permission_callbacks_keep_sync_contract(self):
        client, _ = self.session('permission')
        calls = []
        client.on_request = lambda method, params: calls.append(threading.get_ident()) or {}
        _exhaust(client.iter_open())
        updates = []
        result = _exhaust(client.iter_prompt('hello', on_update=updates.append))
        self.assertEqual(result.text, '你好，世界')
        self.assertEqual(set(calls), {threading.get_ident()})
        self.assertFalse(any(e['sessionUpdate'] == 'agent_thought_chunk' for e in updates))

    def test_large_input_services_real_mcp_through_iterator_entry(self):
        executions = []
        bridge = McpToolBridge([{'name': 'memory_echo', 'description': 'Offline echo',
            'input_schema': {'type': 'object'}}],
            lambda call: executions.append(threading.get_ident()) or {'status': 'ok'}, timeout=8)
        self.addCleanup(bridge.close)
        bridge.activate()
        case = duplex.PipeCase(self, 'mcp', on_request=lambda method, args: {'ok': True}, bridge=bridge)
        case.open()
        with patch.object(case.client, 'prompt', lambda text, **kw: _exhaust(case.client.iter_prompt(text, **kw))):
            result = case.prompt()
        self.assertEqual(result.text, 'ok')
        self.assertEqual(executions, [threading.get_ident()])
        self.assertLess(case.events.index(case.seen('mcp-complete-before-drain')[0]),
                        case.events.index(case.seen('prompt')[0]))
        case.assert_exact(duplex.large_prompt())
        case.assert_clean()

    def test_input_flood_preserves_order_and_filters_private_chunks(self):
        for mode in ('public', 'private'):
            with self.subTest(mode=mode):
                case = duplex.PipeCase(self, mode)
                case.open()
                updates = []
                def update(body):
                    text = case.update(body)
                    updates.append(text)
                    if mode == 'public' and text != 'ok' and (int(text) + 1) % 32 == 0:
                        self.assertFalse(case.client.cancel())
                        case.command('public-' + str(int(text) + 1))
                with patch.object(case.client, 'prompt', lambda text, **kw: _exhaust(case.client.iter_prompt(text, **kw))):
                    result = case.prompt(on_update=update)
                self.assertEqual(updates, [str(i) for i in range(384)] + ['ok'] if mode == 'public' else ['ok'])
                self.assertEqual(result.text, ''.join(updates))
                case.assert_exact(duplex.large_prompt())
                case.assert_clean()

    def test_close_during_large_input_stops_writer_and_descendants(self):
        case = duplex.PipeCase(self, 'service-failure')
        case.open()
        case.command('go')
        pending = case.client.iter_prompt(duplex.large_prompt(), service=case.service)
        self.until(pending, lambda: bool(case.seen('input-prefix')))
        self.assertFalse(case.client.cancel())
        pending.close()
        self.assertEqual(case.seen('prompt'), [])
        case.assert_clean()
