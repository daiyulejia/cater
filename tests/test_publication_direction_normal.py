"""Ordinary run publication direction admission, including an offline ACP peer."""
import unittest
import json
import os
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from pathlib import Path

from cater import artifact_store
from cater.runtime import Runtime
from cater.team import HOST
from cater.anthropic import AnthropicMessagesDriver
from cater.drivers import OpenAICompatibleDriver
import test_simple_team as simple
import test_review_supervision_acp as acp_fixture

turn = simple.turn


class OrdinaryPublicationDirectionTests(unittest.TestCase):
    def fixture(self):
        f = simple.SimpleTeamTests(methodName='runTest')
        f.setUp()
        self.addCleanup(f.doCleanups)
        return f

    def test_actual_native_http_direction_first(self):
        for cls in (OpenAICompatibleDriver, AnthropicMessagesDriver):
            with self.subTest(protocol=cls.__name__):
                f = self.fixture()
                f.rt.team.receive(f.task, 'Work on the current objective')
                claimed = f.rt.team._claim(f.task)
                calls = []
                class Handler(BaseHTTPRequestHandler):
                    def do_POST(self):
                        self.rfile.read(int(self.headers['Content-Length']))
                        calls.append(1)
                        args = {'filename': 'http.txt', 'content': 'stale', 'format': 'text', 'summary': 'fixture'}
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
                             api_key_env='OFFLINE_DIRECTION_TEST_KEY', timeout=8, tool_response_corrections=0)
                f.rt.drivers[driver.name] = driver
                original = f.rt.team._execute
                def inject(task_id, role, name, arguments, workspace, binding, tool_start, assignment, **kwargs):
                    if name == 'publish_artifact':
                        with Runtime(f.rt.log.path, roles_path=f.roles, drivers={'team_test': f.driver}, close_drivers_on_close=False) as other:
                            other.command(task_id, 'Change direction', goal='New objective')
                    return original(task_id, role, name, arguments, workspace, binding, tool_start, assignment, **kwargs)
                with patch.dict(os.environ, {'OFFLINE_DIRECTION_TEST_KEY': 'fixture'}), patch.object(f.rt.team, '_execute', side_effect=inject):
                    list(f.rt.team._run_message(f.task, *claimed))
                # A bounded correction request may follow the rejected tool;
                # the stale publication itself must never be admitted again.
                self.assertGreaterEqual(len(calls), 1)
                self.assertLessEqual(len(calls), 2)
                self.assertFalse(f.events('deliverable.published'))
                receipt, = [e for e in f.events('team.tool_finished') if e.payload.get('name') == 'publish_artifact']
                self.assertEqual(receipt.payload['result']['outcome'], 'not_started')

    def test_second_connection_cannot_write_during_publication_transaction(self):
        f = self.fixture()
        f.rt.team.receive(f.task, 'Start work')
        claimed = f.rt.team._claim(f.task)
        f.driver.steps[f.manager] = [turn('', ('publish_artifact', {'filename': 'lock.txt', 'content': 'x',
            'summary': 'fixture', 'format': 'text'})), turn('done')]
        guard = f.rt.team._publication_guard
        evidence = []
        def inside(*args, **kwargs):
            guard(*args, **kwargs)
            db = sqlite3.connect(f.rt.log.path, timeout=0)
            try:
                with self.assertRaises(sqlite3.OperationalError) as error:
                    db.execute('BEGIN IMMEDIATE')
                self.assertEqual(error.exception.sqlite_errorcode, sqlite3.SQLITE_BUSY)
                evidence.append(True)
            finally:
                db.close()
        with patch.object(f.rt.team, '_publication_guard', side_effect=inside):
            list(f.rt.team._run_message(f.task, *claimed))
        self.assertEqual(evidence, [True])

    def test_two_connection_commit_orders_native_and_qoder(self):
        for protocol in ('native', 'qoder'):
            for order in ('direction_first', 'publication_first'):
                with self.subTest(protocol=protocol, order=order):
                    f = self.fixture()
                    f.rt.team.receive(f.task, 'Work on the current objective')
                    claimed = f.rt.team._claim(f.task)
                    direction = f.rt.work.scope(f.task)['direction_seq']
                    self.assertEqual(claimed[2].payload['work_direction_seq'], direction)
                    self.assertEqual(claimed[2].payload['work_direction_hash'],
                                     next(e.hash for e in f.rt.log.read(task_id=f.task) if e.seq == direction))
                    if protocol == 'native':
                        f.driver.steps[f.manager] = [turn('', ('publish_artifact', {'filename': 'stale.txt',
                            'content': 'stale', 'summary': 'fixture', 'format': 'text'})), turn('done')]
                    else:
                        acp = acp_fixture.ReviewSupervisionAcpTests(methodName='runTest')
                        driver, session, sessions = acp.driver_and_session(f, 'old-publish')
                        self.addCleanup(driver.close)
                        f.rt.drivers[driver.name] = driver
                    execute = f.rt.team._execute
                    materialize = artifact_store.materialize_team_artifact
                    commands = []
                    def change():
                        with Runtime(f.rt.log.path, roles_path=f.roles, drivers={'team_test': f.driver},
                                     close_drivers_on_close=False) as other:
                            commands.append(other.command(f.task, 'Set a new goal', goal='Different objective'))
                    def before(task_id, role, name, arguments, workspace, binding, tool_start, assignment, **kwargs):
                        if name == 'publish_artifact':
                            change()
                        return execute(task_id, role, name, arguments, workspace, binding, tool_start, assignment, **kwargs)
                    def after(*args, **kwargs):
                        change()
                        return materialize(*args, **kwargs)
                    p = patch.object(f.rt.team, '_execute', side_effect=before) if order == 'direction_first' else patch('cater.artifact_store.materialize_team_artifact', side_effect=after)
                    with p:
                        if protocol == 'qoder':
                            with patch('cater.external_agent.AcpSession', session):
                                list(f.rt.team._run_message(f.task, *claimed))
                        else:
                            list(f.rt.team._run_message(f.task, *claimed))
                    started, = [e for e in f.events('team.tool_started') if e.payload.get('name') == 'publish_artifact']
                    receipts = [e for e in f.events('team.tool_finished') if e.payload.get('started_seq') == started.seq]
                    self.assertEqual(len(receipts), 1)
                    publications = [e for e in f.events('deliverable.published') if e.payload.get('filename') == 'stale.txt']
                    if order == 'direction_first':
                        self.assertGreater(commands[0].seq, started.seq)
                        self.assertEqual(publications, [])
                        self.assertEqual(receipts[0].payload['result']['outcome'], 'not_started')
                    else:
                        published, = publications
                        self.assertGreater(commands[0].seq, published.seq)
                        self.assertEqual(receipts[0].payload['result']['status'], 'ok')
                        saved, = [e for e in f.events('team.artifact_saved') if e.payload.get('artifact_seq') == published.seq]
                        self.assertEqual(Path(saved.payload['path']).read_text(encoding='utf-8'), 'stale')
                    self.assertTrue(f.rt.verify_chain().ok)

    def test_own_objective_and_unrelated_chat_allow_publication(self):
        for case in ('own_objective', 'cross_role_chat'):
            with self.subTest(case=case):
                f = self.fixture()
                f.rt.team.receive(f.task, 'Start work')
                claimed = f.rt.team._claim(f.task)
                publish = ('publish_artifact', {'filename': 'normal.txt', 'content': 'content',
                                                'summary': 'fixture', 'format': 'text'})
                if case == 'own_objective':
                    f.driver.steps[f.manager] = [turn('', ('set_objective', {'goal': 'Self refined objective'}), publish), turn('done')]
                else:
                    f.driver.steps[f.manager] = [turn('', publish), turn('done')]
                    worker = f.rt._role_by_id(f.worker)
                    f.rt.team.receive(f.task, f'@{worker.name} Please change the subject of our chat')
                list(f.rt.team._run_message(f.task, *claimed))
                self.assertEqual(len([e for e in f.events('deliverable.published') if e.payload.get('filename') == 'normal.txt']), 1)

    def test_legacy_run_can_reconstruct_but_partial_binding_is_unknown(self):
        for case in ('legacy', 'partial'):
            with self.subTest(case=case):
                f = self.fixture()
                message = f.rt.team.receive(f.task, 'Work on this')
                role = f.rt.organization.leader
                payload = {'message_seq': message.seq, 'role': role.id, 'run_id': 'legacy-fixture',
                           'revision': f.rt.team.view(f.task)['revision'],
                           'control_seq': f.rt.execution_control.view(f.task)['seq']}
                if case == 'partial':
                    payload['work_direction_seq'] = f.rt.work.scope(f.task)['direction_seq']
                started = f.rt.log.emit(type='team.run_started', task_id=f.task, actor=HOST, payload=payload)
                f.driver.steps[f.manager] = [turn('', ('publish_artifact', {'filename': 'legacy.txt', 'content': 'x',
                    'summary': 'fixture', 'format': 'text'})), turn('done')]
                list(f.rt.team._run_message(f.task, message, role, started))
                pubs = [e for e in f.events('deliverable.published') if e.payload.get('filename') == 'legacy.txt']
                if case == 'legacy':
                    self.assertEqual(len(pubs), 1)
                else:
                    self.assertEqual(pubs, [])
                    result = [e for e in f.events('team.tool_finished') if e.payload.get('name') == 'publish_artifact'][0].payload['result']
                    self.assertEqual(result['outcome'], 'unknown')
                    self.assertEqual(result['code'], 'publication_binding_unverified')
