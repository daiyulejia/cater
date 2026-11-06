"""Real local HTTP exercise of old-report action fencing."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import threading
import unittest
from unittest.mock import patch
from cater.anthropic import AnthropicMessagesDriver
from cater.drivers import OpenAICompatibleDriver
import test_native_response_evidence as fixtures


class NativeReportDirectionTests(unittest.TestCase):
    def test_old_report_publish_is_blocked_at_commit_for_both_protocols(self):
        for cls in (OpenAICompatibleDriver, AnthropicMessagesDriver):
            with self.subTest(protocol=cls.__name__):
                helper = fixtures.NativeResponseEvidenceTests(methodName='runTest')
                helper.setUp()
                self.addCleanup(helper.doCleanups)
                f = helper.fixture()
                calls = []
                class Handler(BaseHTTPRequestHandler):
                    def do_POST(self):
                        self.rfile.read(int(self.headers['Content-Length']))
                        calls.append(True)
                        args = {'filename': 'stale.txt', 'content': 'stale', 'format': 'text', 'summary': 'stale'}
                        data = ({'type': 'message', 'role': 'assistant', 'stop_reason': 'tool_use',
                                 'content': [{'type': 'tool_use', 'id': 'call', 'name': 'publish_artifact', 'input': args}],
                                 'usage': {'input_tokens': 1, 'output_tokens': 1}} if cls is AnthropicMessagesDriver else
                                {'choices': [{'finish_reason': 'tool_calls', 'message': {'role': 'assistant', 'content': None,
                                 'tool_calls': [{'id': 'call', 'type': 'function', 'function': {'name': 'publish_artifact',
                                                                                               'arguments': json.dumps(args)}}]}}],
                                 'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}})
                        raw = json.dumps(data).encode()
                        self.send_response(200)
                        self.send_header('Content-Length', str(len(raw)))
                        self.end_headers()
                        self.wfile.write(raw)
                    def log_message(self, *args): pass
                server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
                thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01})
                thread.start()
                self.addCleanup(server.server_close)
                self.addCleanup(thread.join, 5)
                self.addCleanup(server.shutdown)
                driver = cls(f.driver.name, endpoint=f'http://127.0.0.1:{server.server_port}/fixture', model='offline',
                             api_key_env='DIRECTION_LOCAL_KEY', timeout=8, tool_response_corrections=0)
                f.rt.drivers[driver.name] = driver
                original = f.rt.team._publication_guard
                def inject(task_id, filename, tool_start):
                    original(task_id, filename, tool_start)
                    f.rt.command(task_id, 'Update scope', goal='Different result')
                with patch.dict(os.environ, {'DIRECTION_LOCAL_KEY': 'fixture'}), patch.object(f.rt.team, '_publication_guard', side_effect=inject):
                    f.rt.collaborate(f.task)
                self.assertEqual(len(calls), 1)
                self.assertFalse([e for e in f.events('deliverable.published') if e.payload.get('filename') == 'stale.txt'])
                self.assertFalse(f.events('team.objective_completed'))
                self.assertTrue(f.rt.verify_chain().ok)


if __name__ == '__main__':
    unittest.main()
