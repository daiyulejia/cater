"""Dormant host-only barrier: no model, network, or automatic supervision."""
import unittest
import sqlite3
from unittest.mock import patch

from cater.events import Actor
from cater.team import HOST
from cater.tool_protocol import ToolCall
from cater.work import WorkConflict
import test_simple_team as fixture


class SupervisionCustomerBarrierTests(unittest.TestCase):
    setUp = fixture.SimpleTeamTests.setUp
    events = fixture.SimpleTeamTests.events

    def start(self, *, barrier=True):
        role = self.rt.team._role(self.manager)
        source = self.rt.log.emit(type='team.message_received', task_id=self.task, actor=HOST,
            payload={'kind': 'recovery', 'recipient': role.id, 'text': 'fixture supervision'})
        contract = {'version': 1, 'kind': 'all_customer_messages',
                    'source_seq': source.seq, 'source_hash': source.hash}
        started = self.rt.log.emit(type='team.run_started', task_id=self.task, actor=HOST,
            payload={'message_seq': source.seq, 'role': role.id, 'run_id': 'barrier-fixture',
                     'revision': self.rt.team.view(self.task)['revision'],
                     'control_seq': self.rt.execution_control.view(self.task)['seq'],
                     **({'customer_input_barrier': contract} if barrier else {})})
        return source, role, started

    def incoming(self):
        worker = self.rt.team._role(self.worker)
        return self.rt.team.receive(self.task, f'@{worker.name} 新方向', request_id='cross-role-barrier')

    def dispatch(self, source, role, started, name, arguments, *, on_started=None):
        tools = self.rt.team._schemas(role, None)
        call = ToolCall('barrier-call', name, arguments)
        return self.rt.team._dispatch_tool(self.task, source, role, started, started.payload['revision'],
            call, tools, None, {}, {}, None, on_started=on_started)

    def test_cross_role_message_between_tool_start_and_commit_blocks_local_action(self):
        source, role, started = self.start()
        _, result = self.dispatch(source, role, started, 'set_objective',
            {'goal': '不应采用'}, on_started=self.incoming)
        self.assertEqual(result['status'], 'error')
        self.assertEqual(self.events('team.objective_updated'), [])
        self.assertEqual(self.events('team.objective_completed'), [])
        self.assertEqual(self.rt.team.view(self.task)['status'], 'running')
        self.assertTrue(self.rt.verify_chain().ok)

    def test_message_during_model_response_prevents_tool_dispatch(self):
        source, role, started = self.start()
        def response(context, history, tools):
            self.incoming()
            return fixture.turn('', ('finish_work', {'summary': '不应交付', 'verification': 'fixture'}))
        self.driver.steps[self.manager] = [response]
        list(self.rt.team._run_message(self.task, source, role, started))
        self.assertEqual(self.events('team.tool_started'), [])
        self.assertEqual(self.events('team.objective_completed'), [])
        finished, = self.events('team.run_finished')
        self.assertEqual(finished.payload['status'], 'interrupted')

    def test_committed_local_action_precedes_later_input(self):
        source, role, started = self.start()
        _, result = self.dispatch(source, role, started, 'set_objective', {'goal': '采用目标'})
        self.assertEqual(result['status'], 'ok')
        self.incoming()
        updated, = self.events('team.objective_updated')
        self.assertLess(updated.seq, self.events('team.message_received')[-1].seq)

    def test_started_external_effect_retains_its_actual_receipt(self):
        source, role, started = self.start()
        tools = [{'name': 'external_fixture', 'input_schema': {'properties': {}, 'required': []}}]
        def effect(*args, **kwargs):
            self.incoming()
            return {'status': 'ok', 'observed': 'effect already ran'}
        with patch.object(self.rt.team, '_execute', side_effect=effect):
            _, result = self.rt.team._dispatch_tool(self.task, source, role, started, 0,
                ToolCall('effect', 'external_fixture', {}), tools, None, {}, {}, None)
        self.assertEqual(result, {'status': 'ok', 'observed': 'effect already ran'})
        receipt, = self.events('team.tool_finished')
        self.assertEqual(receipt.payload['result'], result)
        with self.assertRaises(WorkConflict):
            self.rt.team._current(self.task, started, role, 0)

    def test_customer_writer_cannot_commit_inside_local_action_transaction(self):
        from cater.events import EventLog
        source, role, started = self.start()
        other = EventLog(self.rt.log.path)
        self.addCleanup(other.close)
        other.db.execute('PRAGMA busy_timeout = 0')
        locked = []
        original = self.rt.team._execute
        def during_action(*args, **kwargs):
            with self.assertRaises(sqlite3.OperationalError) as failure:
                other.db.execute('BEGIN IMMEDIATE')
            self.assertEqual(failure.exception.sqlite_errorcode, sqlite3.SQLITE_BUSY)
            locked.append(True)
            return original(*args, **kwargs)
        with patch.object(self.rt.team, '_execute', side_effect=during_action):
            _, result = self.dispatch(source, role, started, 'set_objective', {'goal': '原动作'})
        self.assertEqual(locked, [True])
        self.assertEqual(result['status'], 'ok')
        other.emit(actor=Actor.human(), type='team.message_received', task_id=self.task,
                   payload={'kind': 'message', 'recipient': self.worker, 'text': '新消息'})
        updated, = self.events('team.objective_updated')
        self.assertLess(updated.seq, self.events('team.message_received')[-1].seq)

    def test_customer_transaction_commits_before_local_admission(self):
        from cater.events import EventLog
        source, role, started = self.start()
        other = EventLog(self.rt.log.path)
        self.addCleanup(other.close)
        probe = sqlite3.connect(self.rt.log.path, timeout=0)
        self.addCleanup(probe.close)
        with other.atomic():
            message = other.emit(actor=Actor.human(), type='team.message_received', task_id=self.task,
                payload={'kind': 'message', 'recipient': self.worker, 'text': '新消息先提交'})
            with self.assertRaises(sqlite3.OperationalError) as failure:
                probe.execute('BEGIN IMMEDIATE')
            self.assertEqual(failure.exception.sqlite_errorcode, sqlite3.SQLITE_BUSY)
        with self.assertRaises(WorkConflict):
            self.dispatch(source, role, started, 'set_objective', {'goal': '不得采用'})
        self.assertGreater(message.seq, source.seq)
        self.assertEqual(self.events('team.objective_updated'), [])
        self.assertEqual(self.events('team.tool_started'), [])

    def test_ordinary_run_is_not_given_new_cross_role_barrier(self):
        source, role, started = self.start(barrier=False)
        self.incoming()
        self.rt.team._current(self.task, started, role, 0)

    def test_invalid_or_forged_contract_fails_closed(self):
        source, role, started = self.start()
        from dataclasses import replace
        forged = replace(started, payload={**started.payload, 'customer_input_barrier': {
            'version': True, 'kind': 'all_customer_messages', 'source_seq': source.seq, 'source_hash': source.hash}})
        with self.assertRaises(WorkConflict):
            self.rt.team._current(self.task, forged, role, 0)

    def test_persisted_wrong_source_hash_fails_closed(self):
        source, role, started = self.start()
        invalid = self.rt.log.emit(type='team.run_started', task_id=self.task, actor=HOST, payload={
            **started.payload, 'run_id': 'invalid-source-hash',
            'customer_input_barrier': {'version': 1, 'kind': 'all_customer_messages',
                                       'source_seq': source.seq, 'source_hash': 'wrong'}})
        with self.assertRaises(WorkConflict):
            self.rt.team._current(self.task, invalid, role, 0)

    def test_other_task_customer_input_does_not_preempt(self):
        from cater.supervision_barrier import check
        source, role, started = self.start()
        other_task = self.rt.create_task('另一任务')
        self.rt.log.emit(type='team.message_received', task_id=other_task, actor=Actor.human(),
            payload={'kind': 'message', 'recipient': self.worker, 'text': '另一任务的消息'})
        check(self.task, started, role, self.rt.log.read())
        _, result = self.dispatch(source, role, started, 'set_objective', {'goal': '本任务目标'})
        self.assertEqual(result['status'], 'ok')

    def test_wrong_sources_fail_closed(self):
        from cater.supervision_barrier import check
        other_task = self.rt.create_task('另一任务')
        role = self.rt.team._role(self.manager)
        for index, (task, actor, kind, recipient, event_type) in enumerate((
                (other_task, HOST, 'recovery', self.manager, 'team.message_received'),
                (self.task, Actor.human(), 'recovery', self.manager, 'team.message_received'),
                (self.task, HOST, 'message', self.manager, 'team.message_received'),
                (self.task, HOST, 'recovery', self.worker, 'team.message_received'),
                (self.task, HOST, 'recovery', self.manager, 'msg.said'))):
            with self.subTest(index=index):
                source = self.rt.log.emit(type=event_type, task_id=task, actor=actor,
                    payload={'kind': kind, 'recipient': recipient, 'text': 'source'})
                started = self.rt.log.emit(type='team.run_started', task_id=self.task, actor=HOST, payload={
                    'message_seq': source.seq, 'role': role.id, 'run_id': f'invalid-source-{index}',
                    'revision': 0, 'control_seq': self.rt.execution_control.view(self.task)['seq'],
                    'customer_input_barrier': {'version': 1, 'kind': 'all_customer_messages',
                                               'source_seq': source.seq, 'source_hash': source.hash}})
                with self.assertRaises(WorkConflict):
                    check(self.task, started, role, self.rt.log.read(task_id=self.task))

    def test_barrier_identity_is_not_recovery_history_authorization(self):
        source, role, started = self.start()
        self.assertFalse(self.rt.team.recovery.is_message(source))
        # The standalone barrier checks the source identity, not its history.
        from cater.supervision_barrier import check
        check(self.task, started, role, self.rt.log.read(task_id=self.task))
        # A future scheduler must reject this input before issuing a run.
        self.assertFalse(self.rt.team.recovery.current(self.task, source))


class ExternalSupervisionBarrierTests(unittest.TestCase):
    def test_cross_role_input_during_external_run_stops_new_local_action(self):
        import test_team_external as external
        f = external.TeamExternalTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        role = f.rt.team._role(f.manager)
        f.rt.team.receive(f.task, '初始工作', request_id='external-original')
        f.rt.team._claim(f.task)
        f.rt.team.recovery.retire(f.task)
        source, = [e for e in f.rt.team._messages(f.task) if e.payload.get('kind') == 'recovery']
        started = f.rt.log.emit(type='team.run_started', task_id=f.task, actor=HOST, payload={
            'message_seq': source.seq, 'role': role.id, 'run_id': 'external-barrier', 'revision': 0,
            'control_seq': f.rt.execution_control.view(f.task)['seq'],
            'recovery_scope': f.rt.team.recovery.scope(f.task),
            'customer_input_barrier': {'version': 1, 'kind': 'all_customer_messages',
                                       'source_seq': source.seq, 'source_hash': source.hash}})
        def script(run):
            worker = f.rt.team._role(f.worker)
            f.rt.team.receive(f.task, f'@{worker.name} 新方向', request_id='external-cross-role')
            run.call('finish_work', summary='不得交付', verification='fixture')
        f.driver.scripts[f.manager] = [script]
        list(f.rt.team._run_message(f.task, source, role, started))
        self.assertEqual(f.events('team.objective_completed'), [])
        self.assertEqual(f.events('team.tool_started'), [])
        finished, = [e for e in f.events('team.run_finished') if e.payload.get('run_id') == 'external-barrier']
        self.assertEqual(finished.payload['status'], 'interrupted')
        self.assertTrue(f.driver.runs[0].state.cleanup_complete)
        self.assertTrue(f.rt.verify_chain().ok)
