"""One native model input must not mix committed database versions."""
from __future__ import annotations

import json
import sqlite3
import threading
import unittest
from unittest.mock import patch

from cater.events import EventLog
from cater.runtime import Runtime
from cater.team import HOST
from cater.workspaces import WorkspaceService
import test_simple_team as fixture


class ModelContextSnapshotTests(unittest.TestCase):
    def setUp(self):
        fixture.SimpleTeamTests.setUp(self)
        self.rt.log.db.execute('PRAGMA journal_mode=WAL')
        self.source = self.rt.team.receive(self.task, '先核对旧目标', request_id='model-snapshot-original')

    def other(self):
        runtime = Runtime(self.rt.log.path, roles_path=self.roles,
                          drivers={'team_test': self.driver}, close_drivers_on_close=False)
        self.addCleanup(runtime.close)
        return runtime

    def context(self):
        return self.rt.team._context(self.task, self.source, self.rt.organization.leader)

    def after_first_read(self, change, *, model=False):
        read = self.rt.log.read
        calls = []
        def interleave(*args, **kwargs):
            rows = read(*args, **kwargs)
            if not calls:
                calls.append(True)
                change()
            return rows
        with patch.object(self.rt.log, 'read', side_effect=interleave):
            result = (self.rt.team._model_input(self.task, self.source, self.rt.organization.leader, run_id='snapshot-fixture')
                      if model else self.context())
        self.assertEqual(calls, [True])
        return result

    def bind_roots(self):
        roots = [self.root / 'first', self.root / 'second']
        for root in roots:
            root.mkdir()
        config = self.root / 'snapshot-workspaces.json'
        config.write_text(json.dumps({'workspaces': [{'id': f'root-{i}', 'title': f'Root {i}',
            'root': str(root), 'mode': 'edit'} for i, root in enumerate(roots)]}), encoding='utf-8')
        self.rt.workspaces = WorkspaceService(self.rt, config)
        return config, self.rt.workspaces.bind(self.task, 'root-0', expected_revision=None)

    def test_goal_and_latest_customer_message_stay_at_one_committed_snapshot(self):
        other = self.other()
        changes = []
        def change():
            with other.log.atomic():
                changes.append(other.log.emit(type='team.objective_updated', actor=HOST, task_id=self.task,
                                              payload={'goal': '新目标只讨论运行方式'}))
                other.team.receive(self.task, '改为讨论新目标', request_id='model-snapshot-new-message')
        context = self.after_first_read(change)
        self.assertEqual(context['current_work']['objective'], '制作 doc 转 PDF 工具')
        self.assertEqual(context['current_work']['revision'], 0)
        self.assertEqual(context['work_checkpoint']['objective'], context['current_work']['objective'])
        self.assertEqual(context['work_checkpoint']['scope']['revision'], 0)
        self.assertEqual(context['latest_customer_message']['text'], self.source.payload['text'])
        self.assertLess(context['work_checkpoint']['as_of_seq'], changes[0].seq)
        fresh = self.context()
        self.assertEqual(fresh['current_work']['revision'], changes[0].seq)
        self.assertEqual(fresh['latest_customer_message']['text'], '改为讨论新目标')

    def test_control_change_is_not_attached_to_earlier_messages(self):
        other = self.other()
        context = self.after_first_read(lambda: other.execution_control.change(
            self.task, 'pause', request_id='model-snapshot-pause'))
        self.assertEqual(context['work_checkpoint']['scope']['control_state'], 'running')
        self.assertEqual(self.context()['work_checkpoint']['scope']['control_state'], 'paused')
        self.assertEqual(self.driver.calls, [])

    def test_frozen_rules_are_recorded_exactly_even_if_live_rules_change(self):
        rule = self.rt.rulebook.record('旧店规：只核对正文', scope='task', task_id=self.task)
        other = self.other()
        context = self.after_first_read(lambda: other.rulebook.update(
            rule['id'], revision=1, text='新店规：另外核对来源', status='active'))
        self.assertEqual([row['text'] for row in context['rules']], ['旧店规：只核对正文'])
        applied, = list(self.rt.log.iter_events(task_id=self.task, event_types=('rules.applied',)))
        self.assertEqual(applied.payload['rules'], context['rules'])
        self.assertEqual(applied.payload['fingerprint'], context['work_checkpoint']['scope']['rule_fingerprint'])
        self.assertEqual(self.rt.rulebook.current_snapshot(self.task)['rules'][0]['text'], '新店规：另外核对来源')
        fresh = self.context()
        self.assertEqual(fresh['rules'][0]['text'], '新店规：另外核对来源')
        snapshots = list(self.rt.log.iter_events(task_id=self.task, event_types=('rules.applied',)))
        self.assertEqual(len(snapshots), 2)
        self.assertEqual(snapshots[-1].payload['rules'], fresh['rules'])
        self.context()
        self.assertEqual(len(list(self.rt.log.iter_events(task_id=self.task, event_types=('rules.applied',)))), 2)

    def test_workspace_binding_stays_at_the_read_cutoff(self):
        config, initial = self.bind_roots()
        other = self.other()
        other.workspaces = WorkspaceService(other, config)
        context = self.after_first_read(lambda: other.workspaces.bind(self.task, 'root-1', expected_revision=initial['revision']))
        self.assertEqual(context['workspace']['workspace_id'], 'root-0')
        self.assertEqual(context['workspace']['revision'], initial['revision'])
        self.assertEqual(context['work_checkpoint']['scope']['workspace_revision'], initial['revision'])
        self.assertEqual(self.context()['workspace']['workspace_id'], 'root-1')

    def test_new_customer_input_during_assembly_skips_old_model_request(self):
        context = self.rt.team._model_input
        changed = []
        def replace(*args, **kwargs):
            result = context(*args, **kwargs)
            if not changed:
                changed.append(self.rt.team.receive(self.task, '先等一下，只讨论新的方向', request_id='snapshot-preflight-new'))
            return result
        self.driver.steps[self.manager] = [fixture.turn('只讨论新方向，不执行旧工作。')]
        with patch.object(self.rt.team, '_model_input', side_effect=replace):
            self.rt.collaborate(self.task)
        self.assertEqual(len(self.driver.calls), 1)
        self.assertEqual(self.driver.calls[0]['context']['message']['seq'], changed[0].seq)
        runs = self.rt.team.view(self.task)['runs']
        self.assertEqual(runs[0]['status'], 'interrupted')
        self.assertFalse([run for run in runs if run['status'] == 'failed'])

    def test_pause_during_assembly_does_not_spend_a_model_request(self):
        context = self.rt.team._model_input
        def pause(*args, **kwargs):
            result = context(*args, **kwargs)
            self.rt.execution_control.change(self.task, 'pause', request_id='snapshot-preflight-pause')
            return result
        self.driver.steps[self.manager] = [fixture.turn('这句不应请求模型。')]
        with patch.object(self.rt.team, '_model_input', side_effect=pause):
            self.rt.collaborate(self.task)
        self.assertEqual(self.driver.calls, [])
        self.assertEqual(self.rt.execution_control.view(self.task)['state'], 'paused')

    def test_model_call_holds_no_read_or_write_transaction(self):
        other = self.other()
        def reply(context, history, tools):
            self.assertFalse(self.rt.log.db.in_transaction)
            other.log.emit(type='task.progress_reported', actor=HOST, task_id=self.task,
                           payload={'text': '模型回调期间其他连接可提交'})
            return fixture.turn('事实已核对，未执行工具。')
        self.driver.steps[self.manager] = [reply]
        self.rt.collaborate(self.task)
        self.assertEqual(len(self.driver.calls), 1)
        self.assertFalse(self.rt.log.db.in_transaction)

    def test_model_tools_and_workspace_share_the_context_snapshot(self):
        config, initial = self.bind_roots()
        other = self.other()
        other.workspaces = WorkspaceService(other, config)
        context, workspace, binding, tools, basis = self.after_first_read(
            lambda: other.workspaces.bind(self.task, 'root-1', expected_revision=initial['revision']), model=True)
        self.assertEqual(context['workspace']['revision'], initial['revision'])
        self.assertEqual(binding['revision'], context['workspace']['revision'])
        self.assertIsNotNone(workspace)
        self.assertIn('workspace_write', {tool['name'] for tool in tools})
        self.assertEqual(basis['rule_fingerprint'], context['work_checkpoint']['scope']['rule_fingerprint'])
        self.assertEqual(self.rt.workspaces.view(self.task)['workspace_id'], 'root-1')

    def test_model_paused_tool_filter_uses_the_same_database_cutoff(self):
        other = self.other()
        context, _, _, tools, _ = self.after_first_read(lambda: other.execution_control.change(
            self.task, 'pause', request_id='snapshot-model-tools-pause'), model=True)
        self.assertEqual(context['work_checkpoint']['scope']['control_state'], 'running')
        self.assertIn('finish_work', {tool['name'] for tool in tools})
        fresh, _, _, next_tools, _ = self.rt.team._model_input(self.task, self.source, self.rt.organization.leader,
                                                            run_id='paused-fixture')
        self.assertEqual(fresh['work_checkpoint']['scope']['control_state'], 'paused')
        self.assertNotIn('finish_work', {tool['name'] for tool in next_tools})

    def test_workspace_change_after_capture_skips_request_from_old_directory(self):
        _, initial = self.bind_roots()
        capture = self.rt.team._model_input
        def change(*args, **kwargs):
            result = capture(*args, **kwargs)
            self.rt.workspaces.bind(self.task, 'root-1', expected_revision=initial['revision'])
            return result
        self.driver.steps[self.manager] = [fixture.turn('不应请求这句。')]
        with patch.object(self.rt.team, '_model_input', side_effect=change):
            self.rt.collaborate(self.task)
        self.assertEqual(self.driver.calls, [])
        self.assertEqual(self.rt.team.view(self.task)['runs'][0]['status'], 'interrupted')

    def test_directory_change_between_turns_does_not_reuse_old_history_in_new_root(self):
        _, initial = self.bind_roots()
        (self.root / 'first' / 'input.txt').write_text('旧目录正文', encoding='utf-8')
        invoke = self.rt.team._invoke_tool
        def switch(task_id, role, started, revision, call, *rest):
            result = invoke(task_id, role, started, revision, call, *rest)
            if call.name == 'workspace_read' and result['status'] == 'ok':
                self.rt.workspaces.bind(self.task, 'root-1', expected_revision=initial['revision'])
            return result
        self.driver.steps[self.manager] = [fixture.turn('', ('workspace_read', {'path': 'input.txt'})),
                                          fixture.turn('不应带旧历史进入新目录。')]
        with patch.object(self.rt.team, '_invoke_tool', side_effect=switch):
            self.rt.collaborate(self.task)
        self.assertEqual(len(self.driver.calls), 1)
        run, = self.rt.team.view(self.task)['runs']
        self.assertEqual(run['status'], 'interrupted')
        self.assertIn('工程范围', run['reason'])
        self.assertEqual(self.rt.task(self.task).deliverables, [])
        self.assertEqual(list((self.root / 'second').iterdir()), [])

    def test_project_tool_set_change_does_not_corrupt_native_history(self):
        project = self.rt.rulebook.create_project('新增项目范围')['id']
        invoke = self.rt.team._invoke_tool
        def switch(task_id, role, started, revision, call, *rest):
            result = invoke(task_id, role, started, revision, call, *rest)
            if call.name == 'read_messages' and result['status'] == 'ok':
                self.rt.rulebook.bind_project(self.task, project)
            return result
        self.driver.steps[self.manager] = [fixture.turn('', ('read_messages', {})),
                                          fixture.turn('不应请求不一致的工具历史。')]
        with patch.object(self.rt.team, '_invoke_tool', side_effect=switch):
            self.rt.collaborate(self.task)
        self.assertEqual(len(self.driver.calls), 1)
        run, = self.rt.team.view(self.task)['runs']
        self.assertEqual(run['status'], 'interrupted')
        self.assertIn('工具集合', run['reason'])
        self.assertNotIn('read_project_history', {tool['name'] for tool in self.driver.calls[0]['tools']})

    def test_owned_snapshot_is_read_only_and_released_before_rules_receipt(self):
        self.rt.rulebook.record('准确保留规则正文', scope='task', task_id=self.task)
        snapshot = self.rt.team._context_snapshot
        record = self.rt.rulebook._record_snapshot
        phases = []
        allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION,
                   sqlite3.SQLITE_RECURSIVE, sqlite3.SQLITE_TRANSACTION}
        def observe(*args, **kwargs):
            self.assertTrue(self.rt.log.db.in_transaction)
            phases.append('read')
            self.rt.log.db.set_authorizer(lambda action, *rest: sqlite3.SQLITE_OK
                                         if action in allowed else sqlite3.SQLITE_DENY)
            try:
                return snapshot(*args, **kwargs)
            finally:
                self.rt.log.db.set_authorizer(None)
        def receipt(*args, **kwargs):
            self.assertFalse(self.rt.log.db.in_transaction)
            phases.append('receipt')
            return record(*args, **kwargs)
        with patch.object(self.rt.team, '_context_snapshot', side_effect=observe), \
                patch.object(self.rt.rulebook, '_record_snapshot', side_effect=receipt):
            result = self.context()
        self.assertEqual(phases, ['read', 'receipt'])
        self.assertEqual(len(result['rules']), 1)
        self.assertFalse(self.rt.log.db.in_transaction)
        self.assertEqual(len(list(self.rt.log.iter_events(task_id=self.task, event_types=('rules.applied',)))), 1)

    def test_read_exception_releases_snapshot_without_recording_unreturned_context(self):
        self.rt.rulebook.record('保持这份店规', scope='task', task_id=self.task)
        failure = RuntimeError('fixture read failure')
        with patch.object(self.rt.skills, 'index', side_effect=failure):
            with self.assertRaises(RuntimeError) as caught:
                self.context()
        self.assertIs(caught.exception, failure)
        self.assertFalse(self.rt.log.db.in_transaction)
        self.assertEqual(list(self.rt.log.iter_events(task_id=self.task, event_types=('rules.applied',))), [])
        self.assertEqual(len(self.context()['rules']), 1)

    def test_outer_caller_owns_commit_and_rollback_including_rules_receipt(self):
        self.rt.rulebook.record('调用者事务内看到的店规', scope='task', task_id=self.task)
        before = self.rt.log.head_hash()
        with self.assertRaisesRegex(RuntimeError, 'rollback caller'):
            with self.rt.log.atomic():
                update = self.rt.log.emit(type='team.objective_updated', actor=HOST, task_id=self.task,
                                          payload={'goal': '调用者尚未提交的目标'})
                context = self.context()
                self.assertEqual(context['current_work']['revision'], update.seq)
                self.assertTrue(self.rt.log.db.in_transaction)
                self.assertEqual(len(list(self.rt.log.iter_events(task_id=self.task, event_types=('rules.applied',)))), 1)
                raise RuntimeError('rollback caller')
        self.assertEqual(self.rt.log.head_hash(), before)
        self.assertEqual(list(self.rt.log.iter_events(task_id=self.task, event_types=('rules.applied',))), [])

    def test_outer_deferred_read_snapshot_is_not_released_by_context(self):
        other = self.other()
        self.rt.log.db.execute('BEGIN')
        try:
            self.context()
            other.execution_control.change(self.task, 'pause', request_id='context-outer-snapshot-pause')
            context = self.context()
            self.assertTrue(self.rt.log.db.in_transaction)
            self.assertEqual(context['work_checkpoint']['scope']['control_state'], 'running')
        finally:
            self.rt.log.db.rollback()
        self.assertEqual(self.context()['work_checkpoint']['scope']['control_state'], 'paused')

    def test_receipt_write_failure_rolls_back_and_does_not_call_model(self):
        self.rt.rulebook.record('这次采用的完整正文', scope='task', task_id=self.task)
        emit = self.rt.log.emit
        def crash(**kwargs):
            event = emit(**kwargs)
            if kwargs['type'] == 'rules.applied':
                raise SystemExit('fixture receipt crash')
            return event
        self.driver.steps[self.manager] = [fixture.turn('不应调用模型。')]
        with patch.object(self.rt.log, 'emit', side_effect=crash), self.assertRaises(SystemExit):
            self.rt.collaborate(self.task)
        self.assertFalse(self.rt.log.db.in_transaction)
        self.assertEqual(self.driver.calls, [])
        self.assertEqual(list(self.rt.log.iter_events(task_id=self.task, event_types=('rules.applied',))), [])

    def test_model_result_and_failure_keep_exact_context_basis_without_prompt_copy(self):
        self.rt.rulebook.record('只核对已有正文', scope='task', task_id=self.task)
        capture = self.rt.team._model_input
        observed = []
        other = self.other()
        another = other.create_task('独立的别群工作')
        def shared_rules(*args, **kwargs):
            other.rulebook.record('全局店规：保留明确边界', scope='workspace')
            other.log.emit(type='task.progress_reported', actor=HOST, task_id=another,
                           payload={'text': '发生在本群最后事件之后的别群事件'})
            observed.append(other.log.read()[-1])
            return capture(*args, **kwargs)
        self.driver.steps[self.manager] = [fixture.turn('没有执行转换。')]
        with patch.object(self.rt.team, '_model_input', side_effect=shared_rules):
            self.rt.collaborate(self.task)
        called, = list(self.rt.log.iter_events(task_id=self.task, event_types=('team.model_called',)))
        basis = called.payload['context_basis']
        context = self.driver.calls[0]['context']
        self.assertEqual(basis['as_of_seq'], observed[0].seq)
        self.assertEqual(basis['as_of_hash'], observed[0].hash)
        self.assertGreater(basis['as_of_seq'], context['work_checkpoint']['as_of_seq'])
        self.assertIn('全局店规：保留明确边界', [rule['text'] for rule in context['rules']])
        applied = next(e for e in self.rt.log.read(task_id=self.task) if e.seq == basis['rule_snapshot_seq'])
        self.assertEqual(applied.payload['rules'], context['rules'])
        self.assertEqual(applied.payload['fingerprint'], basis['rule_fingerprint'])
        self.assertEqual(set(basis), {'as_of_seq', 'as_of_hash', 'rule_fingerprint', 'rule_snapshot_seq'})
        self.rt.team.receive(self.task, '再核对一次边界', request_id='basis-failure-source')
        self.driver.steps[self.manager] = [RuntimeError('private provider failure')]
        self.rt.collaborate(self.task)
        failed = list(self.rt.log.iter_events(task_id=self.task, event_types=('team.run_finished',)))[-1]
        self.assertEqual(failed.payload['status'], 'failed')
        self.assertEqual(failed.payload['context_basis']['rule_snapshot_seq'], basis['rule_snapshot_seq'])
        self.assertNotIn('private provider failure', json.dumps(failed.payload))

    def test_unrelated_commits_do_not_force_capture_retry(self):
        other = self.other()
        unrelated = other.create_task('无关工作')
        snapshot = self.rt.team._context_snapshot
        calls = []
        def write_elsewhere(*args, **kwargs):
            calls.append(True)
            for i in range(12):
                other.log.emit(type='task.progress_reported', actor=HOST, task_id=unrelated, payload={'text': f'并发{i}'})
            return snapshot(*args, **kwargs)
        with patch.object(self.rt.team, '_context_snapshot', side_effect=write_elsewhere):
            context = self.context()
        self.assertEqual(len(calls), 1)
        self.assertEqual(context['current_work']['objective'], '制作 doc 转 PDF 工具')
        self.assertEqual(self.driver.calls, [])

    def test_delete_journal_allows_writer_reservation_then_releases_commit(self):
        self.rt.log.db.execute('PRAGMA journal_mode=DELETE')
        self.rt.rulebook.record('读取期间也需完整留存的店规', scope='task', task_id=self.task)
        ready, begin, reserved, finished = (threading.Event() for _ in range(4))
        errors = []
        path = self.rt.log.path
        def write():
            log = None
            try:
                log = EventLog(path)
                ready.set()
                if not begin.wait(10):
                    raise RuntimeError('reader did not start')
                log.db.execute('BEGIN IMMEDIATE')
                reserved.set()
                log.emit(type='task.progress_reported', task_id=self.task, actor=HOST,
                         payload={'text': '读取期间其他连接提交'})
                log.db.commit()
            except BaseException as exc:
                errors.append(exc)
                reserved.set()
            finally:
                if log is not None:
                    log.close()
                finished.set()
        writer = threading.Thread(target=write)
        writer.start()
        index = self.rt.skills.index
        def wait_for_writer(*args, **kwargs):
            begin.set()
            self.assertTrue(reserved.wait(10))
            self.assertEqual(errors, [])
            self.assertFalse(finished.is_set())
            return index(*args, **kwargs)
        try:
            self.assertTrue(ready.wait(10))
            with patch.object(self.rt.skills, 'index', side_effect=wait_for_writer):
                context = self.context()
            self.assertFalse(self.rt.log.db.in_transaction)
            self.assertEqual(len(context['rules']), 1)
        finally:
            begin.set()
            writer.join(10)
        self.assertFalse(writer.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(finished.is_set())


if __name__ == '__main__':
    unittest.main()
