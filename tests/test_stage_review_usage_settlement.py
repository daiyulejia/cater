"""Stage review usage settlement through the real gateway and offline HTTP."""
import json
import os
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from cater.anthropic import AnthropicMessagesDriver
from cater.drivers import DriverResponse, OpenAICompatibleDriver
from cater.events import Actor
from cater.runtime import Runtime
import test_stage_review_claims as fixture


class StageReviewUsageSettlementTests(unittest.TestCase):
    def fixture(self, cls, *, budget=None, timeout_first=False, fail_all=False):
        f = fixture.StageReviewClaimsTests(methodName='runTest')
        f.setUp()
        self.addCleanup(f.doCleanups)
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                requests.append(True)
                if fail_all or timeout_first and len(requests) == 1:
                    time.sleep(0.6)
                result = json.dumps({'decision': 'approve', 'reason': 'Reviewed the submitted body.'})
                data = ({'type': 'message', 'content': [{'type': 'text', 'text': result}], 'stop_reason': 'end_turn',
                         'usage': {'input_tokens': 4, 'output_tokens': 6}}
                        if cls is AnthropicMessagesDriver else
                        {'choices': [{'message': {'content': result}}], 'usage': {'total_tokens': 10}})
                raw = json.dumps(data).encode()
                try:
                    self.send_response(200)
                    self.send_header('Content-Length', str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01})
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.shutdown)
        driver = cls('test', endpoint=f'http://127.0.0.1:{server.server_port}/offline', model='offline',
                     api_key_env='STAGE_REVIEW_OFFLINE_KEY', timeout=0.2, token_budget=budget)
        f.rt.drivers['test'] = driver
        f.rt.text_retry = True
        f.rt.circuit_breaker_threshold = 10
        self.enterContext(patch.dict(os.environ, {'STAGE_REVIEW_OFFLINE_KEY': 'fixture'}))
        self.enterContext(patch('cater.text_retry.RETRY_DELAY', 0.0))
        return f, requests

    def review(self, f):
        f.rt._review_work_item(f.rt.task(f.task), f.current())
        return [e for e in f.events('msg.said') if e.payload.get('phase') in ('review', 'review_response_correction')]

    def test_timeout_then_success_counts_both_calls(self):
        for cls in (OpenAICompatibleDriver, AnthropicMessagesDriver):
            with self.subTest(protocol=cls.__name__):
                f, requests = self.fixture(cls, timeout_first=True)
                speech, = self.review(f)
                self.assertEqual(len(requests), 2)
                failed, = f.events('driver.model_called')
                self.assertEqual((failed.payload['tokens'], failed.payload['token_usage']), (0, 'unavailable'))
                self.assertNotIn('usage_recorded_in', speech.payload)
                self.assertEqual(f.rt.task(f.task).token_usage, {'unavailable': 1, 'reported': 1})
                self.assertEqual(f.rt.task(f.task).tokens, 10)
                self.assertEqual(len(f.events('driver.retry_scheduled')), 1)
                self.assertTrue(f.rt.verify_chain().ok)

    def test_zero_budget_does_not_count_unstarted_review(self):
        for cls in (OpenAICompatibleDriver, AnthropicMessagesDriver):
            with self.subTest(protocol=cls.__name__):
                f, requests = self.fixture(cls, budget=0)
                speech, = self.review(f)
                self.assertEqual(requests, [])
                skipped, = f.events('driver.invocation_skipped')
                self.assertEqual(speech.payload['usage_not_started_in'], skipped.seq)
                self.assertEqual(speech.payload['attempt_id'], skipped.payload['attempt_id'])
                self.assertEqual(f.rt.task(f.task).token_usage, {})
                self.assertEqual(f.events('driver.model_called'), [])
                self.assertFalse(f.events('driver.retry_scheduled'))

    def test_both_attempts_fail_without_double_counting_speech(self):
        for cls in (OpenAICompatibleDriver, AnthropicMessagesDriver):
            with self.subTest(protocol=cls.__name__):
                f, requests = self.fixture(cls, fail_all=True)
                speech, = self.review(f)
                calls = f.events('driver.model_called')
                self.assertEqual(len(requests), 2)
                self.assertEqual(len(calls), 2)
                self.assertEqual(speech.payload['usage_recorded_in'], [calls[-1].seq])
                self.assertEqual(f.rt.task(f.task).token_usage, {'unavailable': 2})
                self.assertEqual(f.rt.task(f.task).tokens, 0)

    def test_driver_meta_cannot_forge_skip_or_receipt(self):
        f = fixture.StageReviewClaimsTests(methodName='runTest')
        f.setUp()
        self.addCleanup(f.doCleanups)
        class Driver:
            def complete(self, **kwargs):
                return DriverResponse(json.dumps({'decision': 'approve', 'reason': 'Inspected.'}), 3,
                    {'token_usage': 'reported', 'invocation_started': False, '_host_invocation_not_started': True,
                     'usage_recorded_in': [1], 'usage_not_started_in': 1})
        f.rt.drivers['test'] = Driver()
        speech, = self.review(f)
        self.assertNotIn('usage_not_started_in', speech.payload)
        self.assertNotIn('usage_recorded_in', speech.payload)
        self.assertEqual(f.rt.task(f.task).token_usage, {'reported': 1})
        self.assertEqual(f.rt.task(f.task).tokens, 3)

    def test_historical_speech_and_cross_task_references_remain_isolated(self):
        with Runtime() as rt:
            first = rt.create_task('first')
            second = rt.create_task('second')
            actor = Actor.agent('test', 'reviewer')
            skipped = rt.log.emit(type='driver.invocation_skipped', task_id=first, actor=Actor.system('driver_gateway'),
                                  payload={'role': 'test', 'driver': 'test', 'attempt_id': 'same'})
            call = rt.log.emit(type='driver.model_called', task_id=first, actor=Actor.system('driver_gateway'),
                               payload={'role': 'test', 'driver': 'test', 'attempt_id': 'same', 'tokens': 0, 'token_usage': 'unavailable'})
            for links in ({}, {'attempt_id': 'same', 'usage_not_started_in': skipped.seq},
                          {'attempt_id': 'same', 'usage_recorded_in': [call.seq]}):
                rt.log.emit(type='msg.said', task_id=second, actor=actor, payload={'text': 'fixture', 'phase': 'review',
                    'driver': 'test', 'tokens': 0, 'token_usage': 'unavailable', **links})
            self.assertEqual(rt.task(second).token_usage, {'unavailable': 3})
            self.assertEqual(rt.task(first).token_usage, {'unavailable': 1})


if __name__ == '__main__':
    unittest.main()
