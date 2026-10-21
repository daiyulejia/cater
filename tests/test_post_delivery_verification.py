"""A fresh customer may verify a delivery using existing execution authority."""

import json
from contextlib import contextmanager
from dataclasses import replace
import sys
import unittest
from unittest.mock import patch

from cater.events import Actor
from cater.team import HOST
from cater.workspaces import WorkspaceService
from cater.workspace_tools import WorkspaceExecutor
from cater.work import WorkConflict
import test_simple_team as native
import test_team_external as external


class PostDeliveryVerificationTests(unittest.TestCase):
    def fixture(self, external_driver=False):
        f = external.TeamExternalTests() if external_driver else native.SimpleTeamTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        project = f.root / 'project'
        project.mkdir()
        (project / 'ledger.py').write_text('def balance(opening, tx):\n    return opening + sum(tx)\n', encoding='utf-8')
        config = f.root / 'workspaces.json'
        config.write_text(json.dumps({'workspaces': [{'id': 'project', 'title': 'Project',
            'root': str(project), 'mode': 'execute', 'commands': {'python': [sys.executable, '-I', '-B']}, 'timeout': 5}]}), encoding='utf-8')
        f.rt.workspaces = WorkspaceService(f.rt, config)
        f.rt.workspaces.bind(f.task, 'project', expected_revision=None)
        if external_driver:
            def finish(run):
                run.call('finish_work', summary='Original delivery', verification='Fixture only')
            f.driver.scripts[f.manager] = [finish]
        else:
            f.driver.steps[f.manager] = [native.turn('', ('finish_work', {'summary': 'Original delivery', 'verification': 'Fixture only'}))]
        f.rt.team.receive(f.task, 'Deliver original')
        f.rt.collaborate(f.task)
        self.assertEqual(f.rt.team.view(f.task)['status'], 'done')
        f.delivery = f.events('team.objective_completed')[0]
        return f

    def test_native_and_external_customer_check_without_reopening_delivery(self):
        for use_external in (False, True):
            with self.subTest(external=use_external):
                f = self.fixture(use_external)
                args = {'command': 'python', 'args': ['-c', "import runpy; from decimal import Decimal; print(runpy.run_path('ledger.py')['balance'](10,[Decimal('-4')]))"]}
                if use_external:
                    def check(run):
                        result = run.call('workspace_run', **args)
                        self.assertEqual(result['status'], 'ok')
                        self.assertEqual(result['stdout']['excerpt'].strip(), '6')
                        return '旧说明有误，实际结果为6。'
                    f.driver.scripts[f.manager] = [check]
                else:
                    f.driver.steps[f.manager] = [native.turn('', ('workspace_run', args)), native.turn('旧说明有误，实际结果为6。')]
                f.rt.team.receive(f.task, '请实际核对旧解释，不改工程')
                f.rt.collaborate(f.task)
                receipts = [e.payload['result'] for e in f.events('team.tool_finished') if e.payload['name'] == 'workspace_run']
                self.assertEqual(receipts[-1]['status'], 'ok')
                self.assertEqual(receipts[-1]['stdout']['excerpt'].strip(), '6')
                self.assertEqual(f.rt.team.view(f.task)['revision'], f.delivery.seq)
                self.assertEqual(f.rt.team.view(f.task)['status'], 'done')
                self.assertEqual(len(f.events('team.objective_completed')), 1)
                answer = f.events('msg.said')[-1]
                self.assertEqual(answer.payload['audience'], 'customer')
                self.assertNotIn('delivery_seq', answer.payload)
                self.assertEqual(list((f.root / 'project').iterdir()), [f.root / 'project' / 'ledger.py'])
                self.assertTrue(f.rt.verify_chain().ok)

    def claimed(self):
        f = self.fixture()
        f.rt.team.receive(f.task, 'Check explanation')
        message, role, started = f.rt.team._claim(f.task)
        tool = f.rt.log.emit(type='team.tool_started', task_id=f.task, actor=HOST,
            payload={'run_id': started.payload['run_id'], 'role': role.id, 'name': 'workspace_run',
                     'call_id': 'check', 'root': str(f.root / 'project'), 'message_seq': message.seq,
                     'revision': f.delivery.seq, 'arguments': {'command': 'python', 'args': ['-V']}})
        return f, message, role, started, tool

    def test_freshness_rejects_newer_message_even_to_another_role(self):
        f, message, role, started, tool = self.claimed()
        self.assertTrue(f.rt.team._post_delivery_command(f.task, role, tool))
        f.rt.team.receive(f.task, '@' + f.rt.team._role(f.worker).name + ' 换方向')
        self.assertFalse(f.rt.team._post_delivery_command(f.task, role, tool))

    def test_paused_or_finished_runs_cannot_use_exception(self):
        for change in ('pause', 'finish'):
            with self.subTest(change=change):
                f, message, role, started, tool = self.claimed()
                if change == 'pause':
                    f.rt.execution_control.change(f.task, 'pause', request_id='pause-followup')
                else:
                    f.rt.log.emit(type='team.run_finished', task_id=f.task, actor=HOST,
                        payload={'run_id': started.payload['run_id'], 'message_seq': message.seq,
                                 'role': role.id, 'status': 'completed', 'reason': ''})
                self.assertFalse(f.rt.team._post_delivery_command(f.task, role, tool))

    def test_mismatched_tool_scope_and_old_source_are_rejected(self):
        f, message, role, started, tool = self.claimed()
        for updates in ({'revision': 0}, {'run_id': 'other'}, {'role': f.worker},
                        {'message_seq': f.events('team.message_received')[0].seq},
                        {'name': 'workspace_write'}):
            with self.subTest(updates=updates):
                changed = replace(tool, payload={**tool.payload, **updates})
                self.assertFalse(f.rt.team._post_delivery_command(f.task, role, changed))

    def test_rechecks_customer_input_after_acquiring_workspace(self):
        f, message, role, started, tool = self.claimed()
        binding = f.rt.workspaces.view(f.task)
        executor = WorkspaceExecutor(f.rt.workspaces.config['project']['policy'])
        original = f.rt.workspaces._owner

        @contextmanager
        def owner(root):
            with original(root) as owned:
                f.rt.team.receive(f.task, '@' + f.rt.team._role(f.worker).name + ' 改方向')
                yield owned

        with patch.object(f.rt.workspaces, '_owner', owner), patch.object(executor, 'execute') as execute:
            with self.assertRaises(WorkConflict):
                f.rt.team._execute(f.task, role, 'workspace_run', {'command': 'python'}, executor, binding, tool, None)
            execute.assert_not_called()

    def test_other_mutations_still_blocked_after_delivery(self):
        f, message, role, started, tool = self.claimed()
        for name in ('workspace_write', 'delegate', 'publish_artifact', 'request_review', 'record_review'):
            with self.subTest(name=name), self.assertRaises(WorkConflict):
                f.rt.team._execute(f.task, role, name, {}, None, {}, tool, None)

    def test_new_objective_during_check_does_not_allow_old_command(self):
        f, message, role, started, tool = self.claimed()
        f.rt.log.emit(type='team.objective_updated', task_id=f.task, actor=HOST,
                      payload={'goal': 'Different task', 'run_id': started.payload['run_id']})
        with self.assertRaises(WorkConflict):
            f.rt.team._execute(f.task, role, 'workspace_run', {'command': 'python'}, None, {}, tool, None)

    def test_pre_delivery_input_cannot_run_after_its_own_finish(self):
        f = self.fixture()
        start = f.events('team.run_started')[0]
        role = f.rt.team._role(f.manager)
        tool = f.rt.log.emit(type='team.tool_started', task_id=f.task, actor=HOST,
            payload={'run_id': start.payload['run_id'], 'role': role.id, 'name': 'workspace_run',
                     'call_id': 'late', 'root': str(f.root / 'project'), 'message_seq': start.payload['message_seq'],
                     'revision': f.delivery.seq, 'arguments': {'command': 'python'}})
        with self.assertRaises(WorkConflict):
            f.rt.team._execute(f.task, role, 'workspace_run', {'command': 'python'}, None, {}, tool, None)

    def test_binding_change_and_unsettled_operations_still_block(self):
        for change in ('binding', 'pending'):
            with self.subTest(change=change):
                f, message, role, started, tool = self.claimed()
                binding = f.rt.workspaces.view(f.task)
                executor = WorkspaceExecutor(f.rt.workspaces.config['project']['policy'])
                with patch.object(executor, 'execute') as execute:
                    if change == 'binding':
                        with patch.object(f.rt.workspaces, 'view', return_value={**binding, 'revision': -1}):
                            with self.assertRaises(WorkConflict):
                                f.rt.team._execute(f.task, role, 'workspace_run', {'command': 'python'}, executor, binding, tool, None)
                    else:
                        with patch.object(f.rt.workspaces, 'pending', return_value=[replace(tool, seq=tool.seq + 1)]):
                            with self.assertRaises(WorkConflict):
                                f.rt.team._execute(f.task, role, 'workspace_run', {'command': 'python'}, executor, binding, tool, None)
                    execute.assert_not_called()

    def test_exception_does_not_grant_missing_execution_or_alias(self):
        f, message, role, started, tool = self.claimed()
        binding = f.rt.workspaces.view(f.task)
        policy = f.rt.workspaces.config['project']['policy']
        for mode in ('read_only', 'edit', 'execute'):
            with self.subTest(mode=mode):
                executor = WorkspaceExecutor(replace(policy, mode=mode, commands=policy.commands if mode == 'execute' else {}))
                result = f.rt.team._execute(f.task, role, 'workspace_run', {'command': 'missing' if mode == 'execute' else 'python'}, executor, binding, tool, None)
                self.assertEqual(result['status'], 'error')
                self.assertEqual(result['error']['code'], 'invalid_arguments' if mode == 'execute' else 'tool_denied')

    def test_directly_addressed_worker_can_check_but_internal_message_cannot(self):
        for human in (True, False):
            with self.subTest(human=human):
                f = self.fixture()
                worker = f.rt.team._role(f.worker)
                if human:
                    f.rt.team.receive(f.task, '@' + worker.name + ' 请核对')
                else:
                    f.rt.team._send(f.task, f.rt.team._role(f.manager), worker.id, 'Check', 'message')
                message, role, started = f.rt.team._claim(f.task)
                tool = f.rt.log.emit(type='team.tool_started', task_id=f.task, actor=HOST,
                    payload={'run_id': started.payload['run_id'], 'role': role.id, 'name': 'workspace_run',
                             'call_id': 'worker', 'root': str(f.root / 'project'), 'message_seq': message.seq,
                             'revision': f.delivery.seq, 'arguments': {'command': 'python'}})
                self.assertEqual(f.rt.team._post_delivery_command(f.task, role, tool), human)

    def test_unknown_command_receipt_blocks_next_customer_execution(self):
        f = self.fixture()
        f.driver.steps[f.manager] = [native.turn('', ('workspace_run', {'command': 'python', 'args': ['-V']})), native.turn('命令影响待核对。')]
        f.rt.team.receive(f.task, '请核验')
        with patch.object(WorkspaceExecutor, '_run', side_effect=OSError('simulated uncertain executor')) as execute:
            f.rt.collaborate(f.task)
            execute.assert_called_once()
        pending = f.rt.workspaces.pending(task_id=f.task)
        self.assertEqual(len(pending), 1)
        receipts = [e.payload['result'] for e in f.events('team.tool_finished') if e.payload['name'] == 'workspace_run']
        self.assertFalse(receipts[-1]['complete'])
        self.assertEqual(receipts[-1]['outcome'], 'effects_unknown')
        f.driver.steps[f.manager] = [native.turn('', ('workspace_run', {'command': 'python', 'args': ['-V']})), native.turn('先核对上次影响。')]
        f.rt.team.receive(f.task, '再试一下')
        with patch.object(WorkspaceExecutor, '_run') as execute:
            f.rt.collaborate(f.task)
            execute.assert_not_called()
        self.assertEqual([e.seq for e in f.rt.workspaces.pending(task_id=f.task)], [pending[0].seq])
        self.assertTrue(f.rt.verify_chain().ok)


if __name__ == '__main__':
    unittest.main()
