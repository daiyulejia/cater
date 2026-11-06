"""Real loopback HTTP and native worker evidence; no model service is contacted."""
from dataclasses import replace
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import threading
import unittest
from unittest.mock import patch

from cater.anthropic import AnthropicMessagesDriver
from cater.drivers import OpenAICompatibleDriver
from cater.native_response_evidence import validate
from cater import native_transport
from cater.review_supervision_candidate import create
from cater import review_supervision_candidate
from cater.runtime import Runtime
from cater.team_recovery import _same_scope
import test_review_supervision_candidate as fixtures


class NativeResponseEvidenceTests(unittest.TestCase):
    def fixture(self, *, consultation=False):
        helper = fixtures.ReviewSupervisionCandidateTests(methodName='runTest')
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        f = helper.fixture('external')
        artifact, = helper.run_calls(f, helper.human(f), ('publish_artifact', {
            'filename': 'review.txt', 'content': 'fixture', 'summary': 'fixture', 'format': 'text'}))
        review, = helper.run_calls(f, helper.human(f), ('request_review', {
            'reviewer': f.reviewer, 'artifact_seqs': [artifact['artifact_seq']], 'instruction': 'Inspect'}))
        if consultation:
            question = f.rt.team.receive(f.task, '这是普通咨询：请解释复核流程，不要交付。', request_id='native-consultation')
            helper.run_calls(f, question, ('reply_to_customer', {'text': '复核流程是读取文件并登记意见。'}), statuses=('completed',))
        helper.run_calls(f, helper.message(f, review['message_seq']), ('record_review', {
            'review_seq': review['review_seq'], 'verdict': 'approved', 'feedback': 'Checked'}), statuses=('waiting',))
        return f

    def run_report(self, cls, *, bad_cleanup=False, overridden=False, contained=False, tool=False,
                   replaced_transport=False, replaced_parser=False, cross_role_input=False, first_read=False,
                   consultation=False):
        f = self.fixture(consultation=consultation)
        requests = []
        arrived, release = threading.Event(), threading.Event()
        injected, errors = [], []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                requests.append(True)
                if cross_role_input:
                    arrived.set()
                    if not release.wait(8):
                        self.send_error(500)
                        return
                if cls is OpenAICompatibleDriver:
                    data = {'choices': [{'finish_reason': 'length' if contained else 'stop',
                            'message': {'role': 'assistant', 'content': 'Review noted.'}}],
                            'usage': {'prompt_tokens': 5, 'completion_tokens': 2, 'total_tokens': 7}}
                else:
                    data = {'type': 'message', 'role': 'assistant', 'stop_reason': 'max_tokens' if contained else 'end_turn',
                            'content': [{'type': 'text', 'text': 'Review noted.'}],
                            'usage': {'input_tokens': 5, 'output_tokens': 2}}
                if tool and cls is OpenAICompatibleDriver:
                    data['choices'][0] = {'finish_reason': 'tool_calls', 'message': {'role': 'assistant',
                        'content': None, 'tool_calls': [{'id': 'offline-wait', 'type': 'function',
                        'function': {'name': 'wait_for_reply', 'arguments': '{}'}}]}}
                elif tool:
                    data.update(stop_reason='tool_use', content=[{'type': 'tool_use', 'id': 'offline-wait',
                                                                 'name': 'wait_for_reply', 'input': {}}])
                if first_read and len(requests) == 1:
                    if cls is OpenAICompatibleDriver:
                        data['choices'][0] = {'finish_reason': 'tool_calls', 'message': {'role': 'assistant',
                            'content': None, 'tool_calls': [{'id': 'offline-read', 'type': 'function',
                            'function': {'name': 'read_checkpoint', 'arguments': '{}'}}]}}
                    else:
                        data.update(stop_reason='tool_use', content=[{'type': 'tool_use', 'id': 'offline-read',
                                                                    'name': 'read_checkpoint', 'input': {}}])
                raw = json.dumps(data).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01})
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.shutdown)
        driver = cls(f.driver.name, endpoint=f'http://127.0.0.1:{server.server_port}/fixture',
                     model='offline-fixture', api_key_env='CATER_OFFLINE_NATIVE_TEST', timeout=8,
                     tool_response_corrections=0)
        if overridden:
            driver.tool_turn = driver.tool_turn
        if replaced_parser:
            original_parser = driver._tool_result
            driver._tool_result = lambda *args, **kwargs: original_parser(*args, **kwargs)
        f.rt.drivers[driver.name] = driver
        claimed = f.rt.team._claim(f.task)
        self.assertEqual(claimed[0].payload.get('kind'), 'report')
        original = native_transport.ProcessScope
        class UncleanScope:
            def __init__(self, process):
                self.scope = original(process)
            def close(self):
                self.scope.close()
                raise OSError('synthetic unconfirmed cleanup')
        def inject():
            try:
                if not arrived.wait(8):
                    raise AssertionError('loopback HTTP request did not arrive')
                with Runtime(f.rt.log.path, roles_path=f.roles, drivers={f.driver.name: f.driver},
                             close_drivers_on_close=False) as incoming:
                    worker = incoming.team._role(f.worker)
                    injected.append(incoming.team.receive(f.task, f'@{worker.name} new customer input',
                                                         request_id='native-evidence-cross-role'))
            except BaseException as error:
                errors.append(error)
            finally:
                release.set()
        injector = threading.Thread(target=inject) if cross_role_input else None
        original_iter = native_transport.iter_request
        def replaced_iter(**kwargs):
            return (yield from original_iter(**kwargs))
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {'CATER_OFFLINE_NATIVE_TEST': 'fixture-only'}))
            stack.enter_context(patch.object(native_transport, 'ProcessScope', UncleanScope if bad_cleanup else original))
            if replaced_transport:
                stack.enter_context(patch.object(native_transport, 'iter_request', replaced_iter))
            if injector:
                injector.start()
            try:
                list(f.rt.team._run_message(f.task, *claimed))
            finally:
                release.set()
                if injector:
                    injector.join(10)
                    self.assertFalse(injector.is_alive())
            self.assertEqual(errors, [])
        return f, claimed, requests, injected

    def test_both_protocols_bind_clean_response_to_real_report_and_run(self):
        for cls in (OpenAICompatibleDriver, AnthropicMessagesDriver):
            with self.subTest(protocol=cls.__name__):
                f, claimed, requests, _ = self.run_report(cls)
                self.assertEqual(len(requests), 1)
                receipt, = f.events('team.native_response_completed')
                self.assertTrue(validate(f.rt.log.read(task_id=f.task), task_id=f.task, receipt=receipt))
                self.assertEqual(receipt.payload['report_hash'], claimed[0].hash)
                self.assertEqual(receipt.payload['run_hash'], claimed[2].hash)
                self.assertEqual(receipt.payload['scope']['revision'], claimed[2].payload['revision'])
                self.assertTrue(receipt.payload['local_cleanup_confirmed'])
                self.assertEqual(len([e for e in f.events('team.model_called') if e.payload.get('run_id') == claimed[2].payload['run_id']]), 1)
                self.assertTrue(f.rt.verify_chain().ok)
                forged = replace(receipt, payload={**receipt.payload, 'report_hash': 'wrong'})
                self.assertFalse(validate(f.rt.log.read(task_id=f.task), task_id=f.task, receipt=forged))
                forged_events = [forged if e.seq == receipt.seq else e for e in f.rt.log.read(task_id=f.task)]
                self.assertFalse(validate(forged_events, task_id=f.task, receipt=forged))
                self.assertFalse(validate(f.rt.log.read(task_id=f.task), task_id='wrong', receipt=receipt))

    def test_cleanup_failure_contained_and_overridden_transport_never_attest(self):
        for cls in (OpenAICompatibleDriver, AnthropicMessagesDriver):
            for case in ('cleanup', 'contained', 'override', 'tool', 'iter_request', 'parser'):
                with self.subTest(protocol=cls.__name__, case=case):
                    f, claimed, requests, _ = self.run_report(cls, bad_cleanup=case == 'cleanup',
                                                           overridden=case == 'override', contained=case == 'contained',
                                                           tool=case == 'tool', replaced_transport=case == 'iter_request',
                                                           replaced_parser=case == 'parser')
                    self.assertEqual(len(requests), 1)
                    self.assertEqual(f.events('team.native_response_completed'), [])
                    if case == 'cleanup':
                        finish, = [e for e in f.events('team.run_finished') if e.payload.get('run_id') == claimed[2].payload['run_id']]
                        self.assertTrue(finish.payload.get('native_cleanup_failed'))

    def test_cross_role_input_before_receipt_is_historical_not_supervision_authority(self):
        for cls in (OpenAICompatibleDriver, AnthropicMessagesDriver):
            with self.subTest(protocol=cls.__name__):
                f, claimed, requests, injected = self.run_report(cls, cross_role_input=True)
                self.assertEqual(len(requests), 1)
                message, = injected
                receipt, = f.events('team.native_response_completed')
                self.assertEqual(message.payload['recipient'], f.worker)
                self.assertNotEqual(f.worker, claimed[1].id)
                self.assertLess(claimed[2].seq, message.seq)
                self.assertLess(message.seq, receipt.seq)
                self.assertTrue(validate(f.rt.log.read(task_id=f.task), task_id=f.task, receipt=receipt))
                self.assertIsNone(create(f.rt.team, f.task, run_seq=claimed[2].seq, run_hash=claimed[2].hash))
                # Isolate the existing any-role input gate with a test-only
                # stand-in for future native chain authentication. No event is
                # fabricated or written by this probe.
                self.assertTrue(_same_scope(claimed[2].payload['recovery_scope'], f.rt.team.recovery.scope(f.task)))
                with patch.object(review_supervision_candidate, 'authenticate', return_value={'test': True}) as auth, \
                        patch.object(f.rt.team, '_pending_operations', side_effect=AssertionError('input gate was bypassed')):
                    self.assertIsNone(review_supervision_candidate._candidate_payload(
                        f.rt.team, f.task, run_seq=claimed[2].seq, run_hash=claimed[2].hash,
                        events=f.rt.log.read(task_id=f.task)))
                    auth.assert_called_once()
                self.assertEqual([e for e in f.events('team.message_received')
                                  if e.payload.get('trigger') == 'review_return_candidate'], [])
                self.assertTrue(f.rt.verify_chain().ok)


if __name__ == '__main__':
    unittest.main()
