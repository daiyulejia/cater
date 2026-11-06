"""Current-drain native supervision over real loopback HTTP, never a model service."""
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import threading
import unittest
from unittest.mock import patch

from cater import native_transport
from cater.anthropic import AnthropicMessagesDriver
from cater.drivers import OpenAICompatibleDriver
from cater.events import ChainReport, EventLog
from cater.review_supervision_candidate import SupervisionLogIntegrityError
from cater.runtime import Runtime
from cater.team import HOST
import test_native_response_evidence as fixtures


class NativeAutomaticReviewSupervisionTests(unittest.TestCase):
    def run_case(self, cls, mode):
        helper = fixtures.NativeResponseEvidenceTests(methodName='runTest')
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        f = helper.fixture(consultation=mode == 'prior_consult')
        if mode == 'consult':
            f.task = f.rt.create_task('Ordinary consultation')
            f.rt.team.enable(f.task)
            f.rt.team.receive(f.task, 'Explain only')
        requests, errors = [], []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append(body)
                index = len(requests)
                if index == 2 and mode in {'cross', 'unknown'}:
                    try:
                        with Runtime(f.rt.log.path, roles_path=f.roles, drivers={f.driver.name: f.driver},
                                     close_drivers_on_close=False) as incoming:
                            if mode == 'cross':
                                worker = incoming.team._role(f.worker)
                                incoming.team.receive(f.task, f'@{worker.name} New customer instruction',
                                                      request_id='automatic-native-cross')
                            else:
                                notice = incoming.team._send(f.task, incoming.team._role(f.manager), f.worker,
                                                             'fixture', 'notification')
                                run = incoming.log.emit(type='team.run_started', task_id=f.task, actor=HOST,
                                    payload={'message_seq': notice.seq, 'role': f.worker, 'run_id': 'unknown-fixture',
                                             'revision': incoming.team.view(f.task)['revision']})
                                incoming.log.emit(type='team.tool_started', task_id=f.task, actor=HOST,
                                    payload={**run.payload, 'name': 'workspace_write', 'call_id': 'unknown', 'arguments': {}})
                                incoming.log.emit(type='team.run_finished', task_id=f.task, actor=HOST,
                                    payload={**run.payload, 'status': 'interrupted'})
                    except Exception as exc:
                        errors.append(type(exc).__name__)
                if index == 2 and mode == 'failure':
                    self.send_response(503)
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                tool = None
                if index >= 2 and mode in {'finish', 'cross', 'unknown'} and index == 2:
                    tool = ('finish_work', {'summary': 'Offline result', 'verification': 'Local fixture only'})
                elif index >= 2 and mode == 'limit':
                    tool = ('read_checkpoint', {})
                if cls is OpenAICompatibleDriver:
                    msg = {'role': 'assistant', 'content': 'Review noted.' if index == 1 else 'Offline observation.'}
                    if tool:
                        msg['tool_calls'] = [{'id': f'call-{index}', 'type': 'function',
                            'function': {'name': tool[0], 'arguments': json.dumps(tool[1])}}]
                    data = {'choices': [{'finish_reason': 'tool_calls' if tool else 'stop', 'message': msg}],
                            'usage': {'prompt_tokens': 5, 'completion_tokens': 2, 'total_tokens': 7}}
                else:
                    content = [{'type': 'text', 'text': 'Review noted.' if index == 1 else 'Offline observation.'}]
                    if tool:
                        content.append({'type': 'tool_use', 'id': f'call-{index}', 'name': tool[0], 'input': tool[1]})
                    data = {'type': 'message', 'role': 'assistant', 'stop_reason': 'tool_use' if tool else 'end_turn',
                            'content': content, 'usage': {'input_tokens': 5, 'output_tokens': 2}}
                raw = json.dumps(data).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01})
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.shutdown)
        driver = cls(f.driver.name, endpoint=f'http://127.0.0.1:{server.server_port}/fixture',
                     model='offline-fixture', api_key_env='CATER_OFFLINE_AUTO', timeout=8,
                     token_budget=7 if mode == 'budget' else None, tool_response_corrections=0)
        if mode == 'untrusted':
            original_parser = driver._tool_result
            driver._tool_result = lambda *args, **kwargs: original_parser(*args, **kwargs)
        f.rt.drivers[driver.name] = driver
        original_scope = native_transport.ProcessScope
        scope_count = [0]
        class Scope:
            def __init__(self, process):
                self.scope = original_scope(process)
                scope_count[0] += 1
                self.index = scope_count[0]
            def close(self):
                self.scope.close()
                if mode == 'unclean' and self.index == 2:
                    raise OSError('synthetic cleanup failure')
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {'CATER_OFFLINE_AUTO': 'fixture'}))
            if mode == 'unclean':
                stack.enter_context(patch.object(native_transport, 'ProcessScope', Scope))
            if mode == 'web_integrity':
                from cater.web import ActivityRunner
                def factory():
                    return Runtime(f.rt.log.path, roles_path=f.roles, drivers={f.driver.name: driver},
                                   close_drivers_on_close=False)
                runner = ActivityRunner(factory)
                runner._activities[f.task] = {'id': 'offline-activity', 'action': 'run', 'state': 'queued'}
                stack.enter_context(patch.object(EventLog, 'verify_chain', return_value=ChainReport(False, 1, reason='broken')))
                runner._run_owned(f.task, 'offline-activity', 'run')
                status = runner.status(f.task)
                self.assertEqual(status['state'], 'blocked')
                self.assertIn('完整性校验未通过', status['error'])
                self.assertFalse(f.events('activity.completed'))
                self.assertFalse(f.events('activity.failed'))
            elif mode == 'integrity':
                stack.enter_context(patch.object(f.rt.log, 'verify_chain', return_value=ChainReport(False, 1, reason='broken')))
                with self.assertRaises(SupervisionLogIntegrityError):
                    f.rt.collaborate(f.task)
            else:
                f.rt.collaborate(f.task)
        self.assertEqual(errors, [])
        return f, requests

    def test_auto_finish_and_no_action_failure_limit(self):
        for cls in (OpenAICompatibleDriver, AnthropicMessagesDriver):
            for mode in ('finish', 'text', 'failure', 'limit', 'budget', 'unclean', 'integrity', 'web_integrity'):
                with self.subTest(protocol=cls.__name__, mode=mode):
                    f, requests = self.run_case(cls, mode)
                    candidate = [e for e in f.events('team.message_received')
                                 if e.payload.get('trigger') == 'review_return_candidate']
                    self.assertEqual(len(candidate), 0 if mode in {'integrity', 'web_integrity'} else 1)
                    self.assertEqual(len(requests), 1 if mode in {'budget', 'integrity', 'web_integrity'} else 4 if mode == 'limit' else 2)
                    if mode in {'integrity', 'web_integrity'}:
                        self.assertEqual(f.events('team.objective_completed'), [])
                        continue
                    if len(requests) > 1:
                        wire = requests[1].get('tools', [])
                        names = {item.get('name') if cls is AnthropicMessagesDriver else item['function']['name'] for item in wire}
                        self.assertIn('finish_work', names)
                        self.assertFalse(names & {'reply_to_customer', 'workspace_write', 'workspace_run', 'publish_artifact'})
                    run, = [e for e in f.events('team.run_started') if e.payload.get('message_seq') == candidate[0].seq]
                    end, = [e for e in f.events('team.run_finished') if e.payload.get('run_id') == run.payload['run_id']]
                    self.assertEqual(end.payload['status'], 'completed' if mode == 'finish' else 'failed' if mode == 'failure' else 'interrupted')
                    deliveries = f.events('team.objective_completed')
                    self.assertEqual(len(deliveries), int(mode == 'finish'))
                    if deliveries:
                        replies = [e for e in f.events('msg.said') if e.payload.get('delivery_seq') == deliveries[0].seq]
                        self.assertEqual(len(replies), 1)
                    self.assertTrue(f.rt.verify_chain().ok)
                    f.rt.collaborate(f.task)
                    self.assertEqual(len(requests), 1 if mode in {'budget'} else 4 if mode == 'limit' else 2)
                    self.assertEqual(len([e for e in f.events('team.message_received')
                                          if e.payload.get('trigger') == 'review_return_candidate']), 1)

    def test_cross_role_and_unknown_effect_preempt_supervision_action(self):
        for cls in (OpenAICompatibleDriver, AnthropicMessagesDriver):
            for mode in ('cross', 'unknown'):
                with self.subTest(protocol=cls.__name__, mode=mode):
                    f, requests = self.run_case(cls, mode)
                    candidate, = [e for e in f.events('team.message_received')
                                  if e.payload.get('trigger') == 'review_return_candidate']
                    end, = [e for e in f.events('team.run_finished') if e.payload.get('message_seq') == candidate.seq]
                    self.assertEqual(end.payload['status'], 'interrupted')
                    self.assertEqual(f.events('team.objective_completed'), [])
                    self.assertEqual(len(requests), 3 if mode == 'cross' else 2)
                    if mode == 'cross':
                        self.assertTrue(any(e.payload.get('recipient') == f.worker and e.payload.get('request_id') == 'automatic-native-cross'
                                            for e in f.events('team.message_received')))
                    self.assertTrue(f.rt.verify_chain().ok)

    def test_ordinary_consultation_does_not_trigger(self):
        for cls in (OpenAICompatibleDriver, AnthropicMessagesDriver):
            with self.subTest(protocol=cls.__name__):
                f, requests = self.run_case(cls, 'consult')
                self.assertEqual(len(requests), 1)
                events = f.rt.log.read(task_id=f.task)
                self.assertFalse([e for e in events if e.payload.get('trigger') == 'review_return_candidate'])
                self.assertFalse([e for e in events if e.type == 'team.objective_completed'])
                self.assertEqual(len([e for e in events if e.type == 'msg.said'
                                      and e.payload.get('audience') == 'customer']), 1)

    def test_answered_consultation_is_not_repeated_by_supervision(self):
        for cls in (OpenAICompatibleDriver, AnthropicMessagesDriver):
            with self.subTest(protocol=cls.__name__):
                f, requests = self.run_case(cls, 'prior_consult')
                self.assertEqual(len(requests), 2)
                candidate, = [e for e in f.events('team.message_received')
                              if e.payload.get('trigger') == 'review_return_candidate']
                run, = [e for e in f.events('team.run_started') if e.payload.get('message_seq') == candidate.seq]
                self.assertFalse([e for e in f.events('msg.said') if e.payload.get('run_id') == run.payload['run_id']
                                  and e.payload.get('audience') == 'customer'])
                answers = [e for e in f.events('msg.said') if e.payload.get('text') == '复核流程是读取文件并登记意见。']
                self.assertEqual(len(answers), 1)

    def test_untrusted_native_parser_does_not_start_supervision(self):
        for cls in (OpenAICompatibleDriver, AnthropicMessagesDriver):
            with self.subTest(protocol=cls.__name__):
                f, requests = self.run_case(cls, 'untrusted')
                self.assertEqual(len(requests), 1)
                self.assertFalse([e for e in f.events('team.message_received')
                                  if e.payload.get('trigger') == 'review_return_candidate'])
                self.assertFalse(f.events('team.objective_completed'))

    def test_historical_report_is_not_scanned(self):
        for cls in (OpenAICompatibleDriver, AnthropicMessagesDriver):
            with self.subTest(protocol=cls.__name__):
                helper = fixtures.NativeResponseEvidenceTests(methodName='runTest')
                helper.setUp()
                self.addCleanup(helper.doCleanups)
                f, _, requests, _ = helper.run_report(cls)
                self.assertEqual(len(requests), 1)
                f.rt.collaborate(f.task)
                self.assertEqual(len(requests), 1)
                self.assertFalse([e for e in f.events('team.message_received')
                                  if e.payload.get('trigger') == 'review_return_candidate'])


if __name__ == '__main__':
    unittest.main()
