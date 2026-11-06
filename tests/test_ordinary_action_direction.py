"""Offline races for ordinary-run local actions and ZIP publication."""
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch

from cater.runtime import Runtime
from cater.team import HOST
from cater.tool_protocol import ToolCall
import cater.team_archive as archive
import test_simple_team as simple
import test_team_supervision as supervision


class OrdinaryActionDirectionTests(unittest.TestCase):
    def fixture(self, workspace=False):
        cls = supervision.TeamSupervisionTests if workspace else simple.SimpleTeamTests
        f = cls(methodName='runTest')
        f.setUp()
        self.addCleanup(f.doCleanups)
        if workspace:
            root = f.bind_workspace()
            (root / 'input.txt').write_text('fixture bytes', encoding='utf-8')
        return f

    def command(self, f):
        with Runtime(f.rt.log.path, roles_path=f.roles, drivers={'team_test': f.driver}, close_drivers_on_close=False) as other:
            return other.command(f.task, 'Change goal', goal='New direction')

    def dispatch(self, f, claimed, name, args, on_started=None, revision=None):
        source, role, run = claimed
        return f.rt.team._dispatch_tool(f.task, source, role, run,
            run.payload['revision'] if revision is None else revision, ToolCall('fixture', name, args),
            f.rt.team._schemas(role, None), None, {}, {}, None, on_started=on_started)

    def test_local_actions_direction_first_or_action_first_two_connections(self):
        for name in ('set_objective', 'delegate'):
            for order in ('direction_first', 'action_first'):
                with self.subTest(name=name, order=order):
                    f = self.fixture()
                    f.rt.team.receive(f.task, 'Start work')
                    claimed = f.rt.team._claim(f.task)
                    args = ({'goal': 'Old objective'} if name == 'set_objective' else
                            {'recipient': f.worker, 'instruction': 'Do old work'})
                    original = f.rt.team._execute
                    locked = []
                    def verify_lock(*args, **kwargs):
                        db = sqlite3.connect(f.rt.log.path, timeout=0)
                        try:
                            with self.assertRaises(sqlite3.OperationalError) as error:
                                db.execute('BEGIN IMMEDIATE')
                            self.assertEqual(error.exception.sqlite_errorcode, sqlite3.SQLITE_BUSY)
                            locked.append(True)
                        finally:
                            db.close()
                        return original(*args, **kwargs)
                    with patch.object(f.rt.team, '_execute', side_effect=verify_lock):
                        start, result = self.dispatch(f, claimed, name, args,
                            on_started=(lambda: self.command(f)) if order == 'direction_first' else None)
                    events = f.rt.log.read(task_id=f.task)
                    actions = ([e for e in events if e.type == 'team.objective_updated'] if name == 'set_objective' else
                               [e for e in events if e.type == 'team.message_received' and e.payload.get('kind') == 'assignment'])
                    receipt, = [e for e in events if e.type == 'team.tool_finished' and e.payload.get('started_seq') == start.seq]
                    if order == 'direction_first':
                        self.assertEqual(actions, [])
                        self.assertEqual(result['outcome'], 'not_started')
                        self.assertEqual(result['code'], 'action_scope_changed')
                        self.assertEqual(locked, [])
                    else:
                        self.assertEqual(locked, [True])
                        self.assertEqual(result['status'], 'ok')
                        command = self.command(f)
                        action, = actions
                        self.assertLess(action.seq, receipt.seq)
                        self.assertLess(receipt.seq, command.seq)
                        self.assertEqual(receipt.payload['result'], result)
                    self.assertTrue(f.rt.verify_chain().ok)

    def test_normal_cross_role_chat_and_own_objective_do_not_block_delegate(self):
        f = self.fixture()
        f.rt.team.receive(f.task, 'Start work')
        claimed = f.rt.team._claim(f.task)
        def chat():
            worker = f.rt.team._role(f.worker)
            with Runtime(f.rt.log.path, roles_path=f.roles, drivers={'team_test': f.driver}, close_drivers_on_close=False) as other:
                other.team.receive(f.task, f'@{worker.name} Change this conversation subject')
        _, objective = self.dispatch(f, claimed, 'set_objective', {'goal': 'Refined objective'}, on_started=chat)
        self.assertEqual(objective['status'], 'ok')
        _, delegated = self.dispatch(f, claimed, 'delegate', {'recipient': f.worker, 'instruction': 'Proceed'}, revision=objective['revision'])
        self.assertEqual(delegated['status'], 'ok')

    def test_legacy_and_incomplete_direction_bindings(self):
        for case in ('legacy', 'partial', 'duplicate'):
            with self.subTest(case=case):
                f = self.fixture()
                source = f.rt.team.receive(f.task, 'Start work')
                role = f.rt.organization.leader
                payload = {'message_seq': source.seq, 'role': role.id, 'run_id': 'legacy-action',
                           'revision': f.rt.team.view(f.task)['revision'],
                           'control_seq': f.rt.execution_control.view(f.task)['seq']}
                if case == 'partial':
                    payload['work_direction_seq'] = f.rt.work.scope(f.task)['direction_seq']
                run = f.rt.log.emit(type='team.run_started', task_id=f.task, actor=HOST, payload=payload)
                if case == 'duplicate':
                    f.rt.log.emit(type='team.run_started', task_id=f.task, actor=HOST, payload=payload)
                _, result = self.dispatch(f, (source, role, run), 'set_objective', {'goal': 'Refined'})
                if case == 'legacy':
                    self.assertEqual(result['status'], 'ok')
                else:
                    self.assertEqual(result['code'], 'action_binding_unverified')
                    self.assertEqual(result['outcome'], 'unknown')
                    self.assertEqual(f.events('team.objective_updated'), [])

    def test_archive_snapshot_before_direction_and_publication_before_direction(self):
        for order in ('direction_first', 'publication_first'):
            with self.subTest(order=order):
                f = self.fixture(workspace=True)
                f.rt.team.receive(f.task, 'Start work')
                claimed = f.rt.team._claim(f.task)
                f.driver.steps[f.manager] = [f.native_turn('', ('publish_workspace_archive', {
                    'filename': 'project.zip', 'paths': ['input.txt'], 'summary': 'fixture'})), f.native_turn('done')]
                snapshot = archive.create_snapshot
                materialize = archive.materialize_team_artifact
                put_blob = archive.put_blob
                commands = []
                locked = []
                def after_snapshot(*args, **kwargs):
                    result = snapshot(*args, **kwargs)
                    commands.append(self.command(f))
                    return result
                def before_materialize(*args, **kwargs):
                    commands.append(self.command(f))
                    return materialize(*args, **kwargs)
                def during_blob(*args, **kwargs):
                    db = sqlite3.connect(f.rt.log.path, timeout=0)
                    try:
                        with self.assertRaises(sqlite3.OperationalError) as error:
                            db.execute('BEGIN IMMEDIATE')
                        self.assertEqual(error.exception.sqlite_errorcode, sqlite3.SQLITE_BUSY)
                        locked.append(True)
                    finally:
                        db.close()
                    return put_blob(*args, **kwargs)
                patcher = (patch.object(archive, 'create_snapshot', side_effect=after_snapshot) if order == 'direction_first'
                           else patch.object(archive, 'materialize_team_artifact', side_effect=before_materialize))
                with patcher, patch.object(archive, 'put_blob', side_effect=during_blob):
                    list(f.rt.team._run_message(f.task, *claimed))
                started, = [e for e in f.events('team.tool_started') if e.payload.get('name') == 'publish_workspace_archive']
                receipt, = [e for e in f.events('team.tool_finished') if e.payload.get('started_seq') == started.seq]
                publications = [e for e in f.events('deliverable.published') if e.payload.get('filename') == 'project.zip']
                if order == 'direction_first':
                    self.assertGreater(commands[0].seq, started.seq)
                    self.assertEqual(publications, [])
                    self.assertEqual(receipt.payload['result']['outcome'], 'not_started')
                    self.assertEqual(locked, [])
                else:
                    event, = publications
                    self.assertGreater(commands[0].seq, event.seq)
                    self.assertEqual(receipt.payload['result']['status'], 'ok')
                    self.assertEqual(locked, [True])
                    saved, = [e for e in f.events('team.artifact_saved') if e.payload.get('artifact_seq') == event.seq]
                    self.assertTrue(Path(saved.payload['path']).is_file())
                    self.assertEqual(archive.artifact_bytes(f.rt.log, f.rt.task(f.task).deliverables[-1]), Path(saved.payload['path']).read_bytes())
                self.assertTrue(f.rt.verify_chain().ok)

    def test_archive_allows_own_objective_and_cross_role_chat(self):
        f = self.fixture(workspace=True)
        f.rt.team.receive(f.task, 'Start work')
        claimed = f.rt.team._claim(f.task)
        f.driver.steps[f.manager] = [f.native_turn('', ('set_objective', {'goal': 'Refined objective'}),
            ('publish_workspace_archive', {'filename': 'project.zip', 'paths': ['input.txt'], 'summary': 'fixture'})),
            f.native_turn('done')]
        snapshot = archive.create_snapshot
        def chat_after_snapshot(*args, **kwargs):
            result = snapshot(*args, **kwargs)
            worker = f.rt.team._role(f.worker)
            with Runtime(f.rt.log.path, roles_path=f.roles, drivers={'team_test': f.driver}, close_drivers_on_close=False) as other:
                other.team.receive(f.task, f'@{worker.name} Change the subject in our chat')
            return result
        with patch.object(archive, 'create_snapshot', side_effect=chat_after_snapshot):
            list(f.rt.team._run_message(f.task, *claimed))
        receipts = [e.payload['result'] for e in f.events('team.tool_finished')
                    if e.payload.get('name') in {'set_objective', 'publish_workspace_archive'}]
        self.assertEqual([r['status'] for r in receipts], ['ok', 'ok'])
        self.assertEqual(len(f.events('deliverable.published')), 1)
