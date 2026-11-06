"""Real drain intent transactions with local-only model fixtures."""
from contextlib import ExitStack
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

from cater.anthropic import AnthropicMessagesDriver
from cater.acp import AcpSession
from cater.drivers import OpenAICompatibleDriver
from cater.events import Actor
from cater.external_agent import QoderAcpDriver
from cater.review_supervision_intent import TYPE, record
from cater.team import HOST
import test_native_response_evidence as fixture
import test_review_supervision_candidate as qoder_fixture


class Crash(BaseException):
    pass


class SupervisionIntentTests(unittest.TestCase):
    def test_process_exit_before_and_after_finish_commit(self):
        for mode in ('before_commit', 'after_commit'):
            with self.subTest(mode=mode), ExitStack() as stack:
                helper = fixture.NativeResponseEvidenceTests(methodName='runTest')
                helper.setUp()
                stack.callback(helper.doCleanups)
                f = helper.fixture()
                requests = []
                class Handler(BaseHTTPRequestHandler):
                    def do_POST(self):
                        self.rfile.read(int(self.headers['Content-Length']))
                        requests.append(True)
                        raw = json.dumps({'choices': [{'finish_reason': 'stop', 'message': {
                            'role': 'assistant', 'content': 'Review acknowledged.'}}],
                            'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}}).encode()
                        self.send_response(200)
                        self.send_header('Content-Length', str(len(raw)))
                        self.end_headers()
                        self.wfile.write(raw)
                    def log_message(self, *args):
                        pass
                server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
                thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01})
                thread.start()
                stack.callback(server.server_close)
                stack.callback(thread.join, 5)
                stack.callback(server.shutdown)
                script = '''
import os, sys
from cater.drivers import OpenAICompatibleDriver
from cater.runtime import Runtime
import cater.review_supervision_intent as intents
db, roles, task, name, endpoint, mode = sys.argv[1:]
driver = OpenAICompatibleDriver(name, endpoint=endpoint, model='offline', api_key_env='CATER_TEST_INTENT', timeout=8)
rt = Runtime(db, roles_path=roles, drivers={name: driver})
if mode == 'before_commit':
    original = intents.record
    def record(*args):
        result = original(*args)
        if result is not None:
            os._exit(71)
        return result
    intents.record = record
else:
    def crash(*args):
        os._exit(72)
    rt.team._supervise_finished_review = crash
rt.collaborate(task)
os._exit(73)
'''
                env = dict(os.environ, CATER_TEST_INTENT='fixture')
                completed = subprocess.run([sys.executable, '-c', script, str(f.rt.log.path), str(f.roles), f.task,
                    f.driver.name, f'http://127.0.0.1:{server.server_port}/fixture', mode],
                    cwd=Path(__file__).resolve().parents[1], env=env, timeout=20, capture_output=True)
                self.assertEqual(completed.returncode, 71 if mode == 'before_commit' else 72)
                self.assertEqual(len(requests), 1)
                intents = f.events(TYPE)
                self.assertEqual(len(intents), 0 if mode == 'before_commit' else 1)
                reports = {e.seq for e in f.events('team.message_received') if e.payload.get('kind') == 'report' and e.payload.get('review_seq')}
                completed_runs = [e for e in f.events('team.run_finished') if e.payload.get('message_seq') in reports and e.payload.get('status') == 'completed']
                self.assertEqual(len(completed_runs), 0 if mode == 'before_commit' else 1)
                self.assertTrue(f.rt.verify_chain().ok)
                if mode == 'after_commit':
                    self.assertEqual(f.rt.team.recovery.prepare(f.task), [])
                    self.assertFalse([e for e in f.events('team.message_received')
                                      if e.payload.get('trigger') == 'review_return_candidate'])
                    self.assertEqual(len(requests), 1)

    def test_qoder_real_drain_records_intent_before_supervision(self):
        helper = qoder_fixture.ReviewSupervisionCandidateTests(methodName='runTest')
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        f = helper.fixture('external')
        artifact, = helper.run_calls(f, helper.human(f), ('publish_artifact', {
            'filename': 'review.txt', 'content': 'fixture', 'summary': 'fixture', 'format': 'text'}))
        review, = helper.run_calls(f, helper.human(f), ('request_review', {
            'reviewer': f.reviewer, 'artifact_seqs': [artifact['artifact_seq']], 'instruction': 'Inspect'}))
        helper.run_calls(f, helper.message(f, review['message_seq']), ('record_review', {
            'review_seq': review['review_seq'], 'verdict': 'approved', 'feedback': 'Checked'}), statuses=('waiting',))
        driver = QoderAcpDriver(f.driver.name, 'never-real-qoder', 'offline', timeout=10)
        self.addCleanup(driver.close)
        f.rt.drivers[driver.name] = driver
        peer = Path(__file__).parent / 'fixtures' / 'review_supervision_acp_peer.py'
        sessions = []
        class Session(AcpSession):
            def __init__(self, command, **kwargs):
                super().__init__([sys.executable, '-u', str(peer.resolve()), 'initial', str(f.root)], **kwargs)
                sessions.append(self)
        with patch('cater.external_agent.AcpSession', Session), patch.object(f.rt.team, '_supervise_finished_review', side_effect=Crash()):
            with self.assertRaises(Crash):
                f.rt.collaborate(f.task)
        intent, = f.events(TYPE)
        self.assertEqual(intent.payload['protocol'], 'qoder_acp')
        self.assertEqual(len(sessions), 1)
        self.assertFalse([e for e in f.events('team.message_received') if e.payload.get('trigger') == 'review_return_candidate'])
        self.assertTrue(f.rt.verify_chain().ok)

    def run_case(self, cls, *, injection=None, crash_after=False, rollback=False, duplicate=False):
        helper = fixture.NativeResponseEvidenceTests(methodName='runTest')
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        f = helper.fixture()
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                requests.append(True)
                if cls is OpenAICompatibleDriver:
                    data = {'choices': [{'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': 'Review acknowledged.'}}],
                            'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}}
                else:
                    data = {'type': 'message', 'role': 'assistant', 'stop_reason': 'end_turn',
                            'content': [{'type': 'text', 'text': 'Review acknowledged.'}],
                            'usage': {'input_tokens': 1, 'output_tokens': 1}}
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
                     model='offline-fixture', api_key_env='CATER_TEST_INTENT', timeout=8)
        f.rt.drivers[driver.name] = driver
        if injection == 'prior-direction':
            f.rt.command(f.task, 'Change work direction', goal='A different deliverable')
        original_finish = f.rt.team._finish_message
        def before_finish(task, message, role, started, *args, **kwargs):
            if message.payload.get('kind') == 'report' and message.payload.get('review_seq'):
                if injection == 'customer':
                    worker = f.rt.team._role(f.worker)
                    f.rt.team.receive(task, f'@{worker.name} new instruction', request_id='intent-new-input')
                if injection == 'pause':
                    f.rt.execution_control.change(task, 'pause', request_id='intent-pause')
            return original_finish(task, message, role, started, *args, **kwargs)
        def wrap_record(*args):
            if duplicate:
                forged = replace(args[2], payload={**args[2].payload, 'text': 'forged report'})
                self.assertIsNone(record(args[0], args[1], forged, *args[3:]))
            result = record(*args)
            if duplicate and result is not None:
                self.assertIsNone(record(*args))
                with sqlite3.connect(f.rt.log.path, timeout=0) as other:
                    with self.assertRaises(sqlite3.OperationalError) as error:
                        other.execute('BEGIN IMMEDIATE')
                    self.assertEqual(error.exception.sqlite_errorcode, sqlite3.SQLITE_BUSY)
            if rollback and result is not None:
                raise Crash()
            return result
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {'CATER_TEST_INTENT': 'fixture'}))
            stack.enter_context(patch.object(f.rt.team, '_finish_message', side_effect=before_finish))
            stack.enter_context(patch('cater.review_supervision_intent.record', side_effect=wrap_record))
            stack.enter_context(patch.object(f.rt.team, '_supervise_finished_review', side_effect=Crash() if crash_after else lambda *args: False))
            if crash_after or rollback:
                with self.assertRaises(Crash):
                    f.rt.collaborate(f.task)
            else:
                f.rt.collaborate(f.task)
        self.assertEqual(len(requests), 2 if injection == 'customer' else 0 if injection == 'prior-direction' else 1)
        return f

    def test_intent_survives_crash_after_commit_and_is_unique(self):
        for cls in (OpenAICompatibleDriver, AnthropicMessagesDriver):
            with self.subTest(protocol=cls.__name__):
                f = self.run_case(cls, crash_after=True, duplicate=True)
                intent, = f.events(TYPE)
                events = f.rt.log.read(task_id=f.task)
                by_seq = {e.seq: e for e in events}
                started = by_seq[intent.payload['run_seq']]
                finished = by_seq[intent.payload['finish_seq']]
                report = by_seq[intent.payload['report_seq']]
                self.assertEqual((started.hash, finished.hash, report.hash),
                                 (intent.payload['run_hash'], intent.payload['finish_hash'], intent.payload['report_hash']))
                self.assertEqual(finished.payload['status'], 'completed')
                self.assertEqual(intent.payload['scope'], {**started.payload['recovery_scope'],
                                 'work_direction_seq': f.rt.work.scope(f.task)['direction_seq']})
                self.assertEqual(report.payload['kind'], 'report')
                self.assertLess(finished.seq, intent.seq)
                self.assertFalse([e for e in f.events('team.message_received') if e.payload.get('trigger') == 'review_return_candidate'])
                self.assertTrue(f.rt.verify_chain().ok)
                self.assertEqual(len(f.events(TYPE)), 1)

    def test_crash_after_intent_write_rolls_back_finished_and_intent(self):
        for cls in (OpenAICompatibleDriver, AnthropicMessagesDriver):
            with self.subTest(protocol=cls.__name__):
                f = self.run_case(cls, rollback=True)
                self.assertEqual(f.events(TYPE), [])
                reports = {e.seq for e in f.events('team.message_received') if e.payload.get('kind') == 'report' and e.payload.get('review_seq')}
                self.assertFalse([e for e in f.events('team.run_finished') if e.payload.get('message_seq') in reports and e.payload.get('status') == 'completed'])
                self.assertTrue(f.rt.verify_chain().ok)

    def test_pause_and_customer_input_before_finish_do_not_create_intent(self):
        for cls in (OpenAICompatibleDriver, AnthropicMessagesDriver):
            for injection in ('pause', 'customer'):
                with self.subTest(protocol=cls.__name__, injection=injection):
                    f = self.run_case(cls, injection=injection)
                    self.assertEqual(f.events(TYPE), [])
                    if injection == 'customer':
                        incoming, = [e for e in f.events('team.message_received')
                                     if e.actor == Actor.human() and e.payload.get('request_id') == 'intent-new-input']
                        self.assertTrue(any(e.payload.get('message_seq') == incoming.seq
                                            for e in f.events('team.run_finished')))

    def test_direction_changed_before_report_run_does_not_create_intent(self):
        for cls in (OpenAICompatibleDriver, AnthropicMessagesDriver):
            with self.subTest(protocol=cls.__name__):
                f = self.run_case(cls, injection='prior-direction')
                self.assertEqual(f.events(TYPE), [])

    def test_old_or_forged_run_cannot_be_retroactively_recorded(self):
        helper = fixture.NativeResponseEvidenceTests(methodName='runTest')
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        f, claimed, _, _ = helper.run_report(OpenAICompatibleDriver)
        finished, = [e for e in f.events('team.run_finished') if e.payload.get('run_id') == claimed[2].payload['run_id']]
        with f.rt.log.atomic():
            self.assertIsNone(record(f.rt.team, f.task, *claimed[:2], claimed[2], finished))
        self.assertEqual(f.events(TYPE), [])


if __name__ == '__main__':
    unittest.main()
