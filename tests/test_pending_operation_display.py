"""Pending display is a read-only projection, never an execution permission."""

from copy import deepcopy
from dataclasses import replace
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from cater.events import Actor
from cater.external_agent import ExternalAgentState
from cater.team import HOST
from cater.tool_protocol import ToolCall
from cater.work import WorkConflict
import test_checkpoint_consistency as consistency
import test_team_cooperative as cooperative
import test_team_scope_polling as scope


class PendingOperationDisplayTests(unittest.TestCase):
    def setUp(self):
        self.f = scope._ScopeFixture()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.rt, self.team, self.task = self.f.rt, self.f.team, self.f.task
        self.message, self.role, self.run = self.f.claim()

    def counts(self, value, total, live, review):
        for key in ('pending_operation_count', 'pending_live_count', 'pending_review_count'):
            self.assertIn(key, value)
        self.assertEqual({key: value[key] for key in (
            'pending_operation_count', 'pending_live_count', 'pending_review_count')},
            dict(pending_operation_count=total, pending_live_count=live, pending_review_count=review))
        self.assertEqual(total, live + review)

    def start(self, name='read_checkpoint', **changes):
        payload = {**self.run.payload, 'name': name, 'call_id': 'query-' + str(self.rt.log.read()[-1].seq),
                   'arguments': {}}
        payload.update(changes)
        return self.rt.log.emit(type='team.tool_started', actor=HOST, task_id=self.task, payload=payload)

    def query(self, name='read_checkpoint', arguments=None, start=None):
        return self.team._execute(self.task, self.role, name, arguments or {}, None,
                                  self.rt.workspaces.view(self.task), start, None)

    def pending(self, **filters):
        return self.query('read_receipts', {'include_pending': True, **filters})

    def external(self, check=None):
        event = self.team.external_runs.begin(self.task, self.role, self.run, basis={}, binding={},
                                              tool_names=[], driver_fingerprint='offline-display')
        check = check or Mock()
        handle = SimpleNamespace(state=ExternalAgentState(phase='prompting', prompt_attempted=True))
        self.team.external.begin_scheduler(self.task)
        self.team.external._live[event.seq] = (event, handle, check)
        self.addCleanup(self.team.external.end_scheduler)
        self.addCleanup(self.team.external._live.clear)
        return event, handle, check

    def test_real_dispatch_excludes_only_current_checkpoint(self):
        old = self.start()
        raw = self.team._pending_operations(self.task)
        context, workspace, binding, tools, _ = self.team._model_input(
            self.task, self.message, self.role, run_id=self.run.payload['run_id'])
        current, result = self.team._dispatch_tool(self.task, self.message, self.role, self.run, 0,
            ToolCall('actual-read', 'read_checkpoint', {}), tools, workspace, binding, context, None)
        self.assertNotEqual(old.seq, current.seq)
        self.assertEqual(result['checkpoint']['pending_operation_count'], 1)
        self.counts(result['checkpoint'], 1, 0, 1)
        self.assertEqual(self.team._pending_operations(self.task), raw)
        self.counts(self.team.recovery.checkpoint(self.task), 1, 0, 1)

    def test_direct_read_does_not_hide_an_unsettled_query(self):
        self.start()
        self.counts(self.team.recovery.read(self.task, {})['checkpoint'], 1, 0, 1)
        self.counts(self.team.recovery.checkpoint(self.task), 1, 0, 1)

    def test_forged_wrong_role_and_stale_query_are_not_excluded(self):
        old = self.start()
        current = self.start()
        invalid = [None, old, replace(current, seq=True), replace(current, hash='forged'),
                   replace(current, task_id=self.f.new_task()),
                   replace(current, actor=Actor.human()),
                   replace(current, payload={**current.payload, 'run_id': 'wrong'}),
                   replace(current, payload={**current.payload, 'message_seq': True}),
                   replace(current, payload={**current.payload, 'role': self.f.worker.id})]
        for candidate in invalid:
            with self.subTest(candidate=candidate):
                self.counts(self.query(start=candidate)['checkpoint'], 2, 0, 2)
        self.counts(self.query(start=current)['checkpoint'], 1, 0, 1)
        self.rt.log.emit(type='team.run_finished', actor=HOST, task_id=self.task,
                         payload={**self.run.payload, 'status': 'interrupted'})
        self.counts(self.query(start=current)['checkpoint'], 2, 0, 2)

    def test_persisted_query_with_unbound_run_cannot_hide_itself(self):
        current = self.start(run_id='not-a-real-run')
        self.counts(self.query(start=current)['checkpoint'], 1, 0, 1)

    def test_live_external_has_same_display_in_checkpoint_receipts_and_prompt(self):
        event, _, check = self.external()
        original = deepcopy(self.team._pending_operations(self.task))
        self.counts(self.team.recovery.checkpoint(self.task), 1, 1, 0)
        receipt = self.pending()
        self.counts(receipt, 1, 1, 0)
        self.assertIs(receipt['pending_operations'][0]['scheduler_owned_live'], True)
        context, *_ = self.team._model_input(self.task, self.message, self.role,
                                            run_id=self.run.payload['run_id'])
        self.counts(context['work_checkpoint'], 1, 1, 0)
        self.assertEqual(context['pending_operations'][0]['started_seq'], event.seq)
        self.assertIs(context['pending_operations'][0]['scheduler_owned_live'], True)
        self.assertEqual(self.team._pending_operations(self.task), original)
        self.assertTrue(check.called)
        self.assertTrue(all(c.kwargs.get('force') is True for c in check.call_args_list))

    def test_unknown_effect_stays_review_and_does_not_inherit_external_liveness(self):
        self.external()
        operation = self.start('workspace_run')
        self.rt.log.emit(type='team.tool_finished', actor=HOST, task_id=self.task, payload={
            'started_seq': operation.seq, 'role': self.role.id, 'run_id': self.run.payload['run_id'],
            'name': 'workspace_run', 'result': {'status': 'error', 'complete': False,
                                              'outcome': 'effects_unknown'}})
        raw = deepcopy(self.team._pending_operations(self.task))
        result = self.pending()
        self.counts(result, 2, 1, 1)
        effect = next(r for r in result['pending_operations'] if r['started_seq'] == operation.seq)
        self.assertIs(effect['scheduler_owned_live'], False)
        self.assertIs(effect['replay_authorized'], False)
        self.assertEqual(self.team._pending_operations(self.task), raw)
        finished = self.query('finish_work', {'summary': 'must fail', 'verification': 'not checked'},
                              self.start('finish_work'))
        self.assertEqual(finished['code'], 'operation_pending')
        self.assertIn(operation.seq, finished['started_seqs'])

    def test_failed_scope_wrong_scheduler_and_ended_handle_are_not_live(self):
        event, handle, check = self.external()
        cases = [patch.object(self.team.external, '_scheduler', None),
                 patch.object(self.team.external, '_scheduler', ('other-task', 1)),
                 patch.object(self.team.external, '_scheduler', (self.task, -1)),
                 patch.object(handle, 'state', replace(handle.state, phase='finished')),
                 patch.object(check, 'side_effect', WorkConflict('scope changed'))]
        for manager in cases:
            with manager:
                result = self.pending()
                self.counts(result, 1, 0, 1)
                self.assertIs(result['pending_operations'][0]['scheduler_owned_live'], False)
        self.team.external_runs.finish(event, phase='failed', prompt_attempted=True,
                                       cleanup_complete=False, result='failed')
        result = self.pending()
        self.counts(result, 1, 0, 1)
        self.assertEqual(result['pending_operations'][0]['outcome'], 'unknown')

    def test_filtered_counts_precede_page_limit(self):
        self.external()
        first = self.start('workspace_run')
        second = self.start('workspace_run')
        result = self.pending(name='workspace_run', limit=1)
        self.counts(result, 2, 0, 2)
        self.assertEqual([r['started_seq'] for r in result['pending_operations']], [second.seq])
        self.assertTrue(result['pending_has_more'])
        self.assertEqual(result['pending_next_before_seq'], second.seq)
        older = self.pending(name='workspace_run', limit=1, before_seq=second.seq)
        self.counts(older, 1, 0, 1)
        self.assertEqual(older['pending_operations'][0]['started_seq'], first.seq)

    def test_read_only_capture_and_outer_transaction_ownership(self):
        self.external()
        before = self.rt.log.head_hash(), self.rt.log.db.total_changes
        allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION,
                   sqlite3.SQLITE_RECURSIVE, sqlite3.SQLITE_TRANSACTION}
        self.rt.log.db.set_authorizer(lambda action, *args:
                                     sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY)
        try:
            self.counts(self.team.recovery.checkpoint(self.task), 1, 1, 0)
            self.counts(self.pending(), 1, 1, 0)
        finally:
            self.rt.log.db.set_authorizer(None)
        self.assertFalse(self.rt.log.db.in_transaction)
        self.rt.log.db.execute('BEGIN')
        try:
            self.counts(self.team.recovery.checkpoint(self.task), 1, 1, 0)
            self.counts(self.pending(), 1, 1, 0)
            self.assertTrue(self.rt.log.db.in_transaction)
        finally:
            self.rt.log.db.rollback()
        self.assertEqual((self.rt.log.head_hash(), self.rt.log.db.total_changes), before)

    def real_run(self, action, *, supervision=False):
        driver = cooperative.CooperativeDriver()
        driver.name = self.f.driver.name
        self.rt.drivers[driver.name] = driver
        driver.scripts = {self.role.id: [action]}
        if supervision:
            self.message = self.rt.log.emit(type='team.message_received', actor=HOST, task_id=self.task,
                payload={'recipient': self.role.id, 'kind': 'message', 'text': 'Offline supervision',
                         'trigger': 'turn_boundary'})
            self.run = self.rt.log.emit(type='team.run_started', actor=HOST, task_id=self.task,
                payload={**self.run.payload, 'message_seq': self.message.seq, 'run_id': 'supervision'})
        self.team.external.begin_scheduler(self.task)
        try:
            result = self.team.external.run_message(self.task, self.message, self.role, self.run, 0,
                                                   None, driver, 'Offline display regression.\n')
        finally:
            self.team.external.end_scheduler()
        self.assertEqual(driver.errors, [])
        return result

    def test_observation_does_not_refresh_normal_scope_poll_throttle(self):
        observed = []
        def action(run):
            self.f.now += 1
            with patch.object(self.team, '_current', wraps=self.team._current) as current:
                self.counts(self.team.recovery.checkpoint(self.task), 1, 1, 0)
                self.assertTrue(current.called, 'Display still requires a forced scope check')
                current.reset_mock()
                self.f.now += .01
                run.check_current()
                self.assertTrue(current.called, 'Display must not postpone the next ordinary scope check')
            observed.append(True)
        self.real_run(action)
        self.assertEqual(observed, [True])

    def test_budget_supervision_query_does_not_weaken_owns_or_recurse(self):
        observed = []
        def action(run):
            result = run.call('read_checkpoint')
            self.counts(result['checkpoint'], 1, 0, 1)
            # Once the pure query is settled, the existing full guard can confirm ownership.
            self.counts(run.call('read_receipts', include_pending=True), 1, 1, 0)
            observed.append(True)
        with patch.object(self.team.recovery, 'budget_boundary', return_value=True):
            self.real_run(action, supervision=True)
        self.assertEqual(observed, [True])

    def test_read_failure_propagates_without_empty_pending_fallback(self):
        with patch.object(self.team, '_pending_operations', side_effect=RuntimeError('read failed')):
            with self.assertRaisesRegex(RuntimeError, 'read failed'):
                self.team.recovery.checkpoint(self.task)
        self.assertFalse(self.rt.log.db.in_transaction)

    def test_receipts_snapshot_does_not_mix_a_later_pending_start(self):
        f = consistency.CheckpointConsistencyTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        f.assignment()
        _, role, run = f.rt.team._claim(f.task)
        other = f.other()
        receipts = f.rt.team._receipts
        inserted = []
        def read_then_write(task):
            result = receipts(task)
            if not inserted:
                inserted.append(other.log.emit(type='team.tool_started', actor=HOST, task_id=f.task,
                    payload={**run.payload, 'name': 'workspace_run', 'call_id': 'after-snapshot', 'arguments': {}}))
            return result
        def query():
            return f.rt.team._execute(f.task, role, 'read_receipts', {'include_pending': True}, None,
                                     f.rt.workspaces.view(f.task), None, None)
        with patch.object(f.rt.team, '_receipts', side_effect=read_then_write):
            result = query()
        self.assertEqual(result['pending_operations'], [])
        self.counts(result, 0, 0, 0)
        self.counts(query(), 1, 0, 1)
        self.assertFalse(f.rt.log.db.in_transaction)


if __name__ == '__main__':
    unittest.main()
