"""Local HTTP barriers exercise native scheduling, never a paid model."""

from contextlib import contextmanager
import json
import os
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from cater.drivers import OpenAICompatibleDriver
from cater.anthropic import AnthropicMessagesDriver
from cater.runtime import Runtime
import test_simple_team as simple


class NativeSchedulerTests(unittest.TestCase):
    @contextmanager
    def scenario(self, cls=OpenAICompatibleDriver):
        f = simple.SimpleTeamTests()
        f.setUp()
        worker_started, release_worker = threading.Event(), threading.Event()
        result = {'reply_before_release': False, 'injection_errors': [], 'requests': []}
        owner = self
        counter = 0
        lock = threading.Lock()

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                nonlocal counter
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                prompt = payload['messages'][0 if cls is AnthropicMessagesDriver else 1]['content']
                context = json.loads(prompt.rsplit('\n', 1)[1])
                role, source = context['you']['id'], context['message']
                with lock:
                    counter += 1
                    number = counter
                    result['requests'].append({'role': role, 'source': source['seq']})
                if role == f.worker:
                    worker_started.set()
                    if not release_worker.wait(6):
                        self.send_error(500)
                        return
                    calls = [('send_message', {'recipient': f.manager,
                        'reply_to': context['active_assignment']['seq'], 'text': 'Original bounded work returned.'})]
                elif source['sender'] == 'human' and source['text'] == 'Initial bounded work':
                    calls = [('delegate', {'recipient': f.worker, 'instruction': 'Do the bounded work and return it.'}),
                             ('wait_for_reply', {})]
                elif source['sender'] == 'human':
                    calls = [('reply_to_customer', {'text': 'The original partner is still working; no new work was assigned.'})]
                else:
                    calls = [('finish_work', {'summary': 'Original work received.', 'verification': 'Text fixture only.'})]
                if cls is AnthropicMessagesDriver:
                    data = {'type': 'message', 'role': 'assistant', 'stop_reason': 'tool_use',
                        'content': [{'type': 'tool_use', 'id': f'request-{number}-{index}',
                            'name': name, 'input': args} for index, (name, args) in enumerate(calls)],
                        'usage': {'input_tokens': 5, 'output_tokens': 2}}
                else:
                    data = {'choices': [{'finish_reason': 'tool_calls', 'message': {'role': 'assistant', 'content': None,
                        'tool_calls': [{'id': f'request-{number}-{index}', 'type': 'function',
                            'function': {'name': name, 'arguments': json.dumps(args)}} for index, (name, args) in enumerate(calls)]}}],
                        'usage': {'prompt_tokens': 5, 'completion_tokens': 2, 'total_tokens': 7}}
                raw = json.dumps(data).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                try:
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.daemon_threads = False
        serving = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01))
        serving.start()

        def inject():
            try:
                owner.assertTrue(worker_started.wait(4), 'Worker request never reached the local server')
                with Runtime(f.root / 'test.db', roles_path=f.roles,
                             drivers={'team_test': simple.TeamDriver()}) as incoming:
                    message = incoming.team.receive(f.task, 'Confirm progress without changing the work',
                        request_id='native-pending-progress')
                    result['new_input_seq'] = message.seq
                    began, deadline = time.monotonic(), time.monotonic() + 2
                    while time.monotonic() < deadline:
                        replies = [event for event in incoming.log.read(task_id=f.task)
                            if event.type == 'msg.said' and event.payload.get('reply_to') == message.seq
                            and event.payload.get('audience') == 'customer']
                        if replies:
                            result['reply_before_release'] = True
                            result['reply_latency_seconds'] = time.monotonic() - began
                            break
                        time.sleep(0.01)
            except BaseException as error:
                result['injection_errors'].append(type(error).__name__ + ': ' + str(error))
            finally:
                release_worker.set()

        injector = threading.Thread(target=inject)
        try:
            driver = cls('team_test', endpoint=f'http://127.0.0.1:{server.server_port}/fixture',
                model='local-synthetic-only', api_key_env='CATER_NATIVE_SCHEDULER_TEST', timeout=8)
            f.rt.drivers['team_test'] = driver
            with patch.dict(os.environ, {'CATER_NATIVE_SCHEDULER_TEST': 'synthetic-only-key'}):
                injector.start()
                f.rt.team.receive(f.task, 'Initial bounded work', request_id='native-initial-work')
                f.rt.collaborate(f.task)
                injector.join(timeout=9)
                owner.assertFalse(injector.is_alive())
                owner.assertEqual(result['injection_errors'], [])
                yield f, result
        finally:
            release_worker.set()
            if injector.ident is not None:
                injector.join(timeout=9)
            server.shutdown()
            serving.join(timeout=3)
            server.server_close()
            f.doCleanups()

    def test_customer_progress_reply_does_not_wait_for_the_worker_model_response(self):
        for cls in (OpenAICompatibleDriver, AnthropicMessagesDriver):
            with self.subTest(protocol=cls.__name__), self.scenario(cls) as (f, result):
                self.assertTrue(result['reply_before_release'], result)
                self.assertEqual(f.rt.team.view(f.task)['status'], 'done')
                self.assertEqual(len(f.events('team.objective_updated')), 0)
                self.assertEqual(len(f.events('team.message_received')), 4)
                self.assertEqual(len(f.events('team.tool_finished')), 5)
                self.assertEqual(len(result['requests']), 4)
                self.assertEqual(len(f.events('team.objective_completed')), 1)
                self.assertEqual(sum(event.payload['tokens'] for event in f.events('team.model_called')), 28)
                self.assertTrue(f.rt.verify_chain().ok)


if __name__ == '__main__':
    unittest.main()
