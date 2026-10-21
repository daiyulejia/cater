"""Withdrawal changes future work, never review approval or unknown effects."""

import unittest
from dataclasses import replace

from cater.runtime import Runtime
from cater.team import HOST
from cater.work import WorkConflict
import test_team_review_cycle as reviews
import test_team_external as external


class WorkWithdrawalIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.f = reviews.TeamReviewCycleTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.rt, self.task = self.f.rt, self.f.task

    def assign(self, sender=None, recipient=None, source=None):
        result, = self.f.exchange(sender or self.f.manager, ('delegate', {
            'recipient': recipient or self.f.worker, 'instruction': 'Inspect the requested result.'}), source=source)
        self.assertEqual(result['status'], 'ok', result)
        return next(e for e in self.rt.team._messages(self.task) if e.seq == result['message_seq'])

    def withdraw(self, source, role=None):
        result, = self.f.exchange(role or self.f.manager, ('withdraw_work', {
            'message_seq': source.seq, 'reason': 'This request is redundant; the actual work remains tracked.'}))
        return result

    def start(self, source):
        return self.rt.log.emit(type='team.run_started', task_id=self.task, actor=HOST, payload={
            'message_seq': source.seq, 'role': source.payload['recipient'],
            'run_id': 'withdraw-source-' + str(source.seq),
            'revision': self.rt.team.view(self.task)['revision'],
            'control_seq': self.rt.execution_control.view(self.task)['seq']})

    def test_queued_work_is_withdrawn_not_falsely_returned_or_started(self):
        source = self.assign()
        result = self.withdraw(source)
        self.assertEqual(result['status'], 'ok', result)
        self.assertTrue(result['withdrawn'])
        view = self.rt.team.view(self.task)
        row = next(a for a in view['assignments'] if a['seq'] == source.seq)
        self.assertEqual(row['status'], 'withdrawn')
        self.assertEqual(row['withdrawal_seq'], result['withdrawal_seq'])
        self.assertEqual(self.rt.team.recovery.assignments(self.task), [])
        self.assertEqual(view['pending_count'], 0)
        self.assertIsNone(self.rt.team._claim(self.task))
        self.assertFalse([e for e in self.rt.team._messages(self.task) if e.payload['kind'] == 'report'])
        self.assertEqual(self.f.events('team.review_recorded'), [])
        self.assertTrue(self.rt.verify_chain().ok)

    def test_failed_ordinary_work_can_be_withdrawn_then_delivery_closes(self):
        source = self.assign()
        run = self.start(source)
        self.rt.log.emit(type='team.run_finished', task_id=self.task, actor=HOST,
                         payload={**run.payload, 'status': 'failed', 'reason': 'Fixture failed input'})
        self.assertTrue(self.rt.team.recovery.assignments(self.task)[0]['requires_return'])
        self.assertEqual(self.withdraw(source)['status'], 'ok')
        result = self.f.finish()
        self.assertEqual(result['status'], 'ok', result)
        self.assertEqual(self.rt.team.view(self.task)['status'], 'done')

    def test_running_work_stops_at_next_boundary_without_fabricated_run_end(self):
        source = self.assign()
        run = self.start(source)
        self.assertEqual(self.withdraw(source)['status'], 'ok')
        role = self.rt.team._role(source.payload['recipient'])
        with self.assertRaises(WorkConflict):
            self.rt.team._current(self.task, run, role, run.payload['revision'])
        self.assertFalse([e for e in self.f.events('team.run_finished')
                          if e.payload['run_id'] == run.payload['run_id']])

    def test_child_work_survives_parent_withdrawal_and_cannot_revive_parent(self):
        parent = self.assign()
        child = self.assign(sender=self.f.worker, recipient=self.f.reviewer, source=parent)
        self.assertEqual(self.withdraw(parent)['status'], 'ok')
        rows = self.rt.team.recovery.assignments(self.task)
        self.assertIn(child.seq, {r['seq'] for r in rows})
        self.assertNotIn(parent.seq, {r['seq'] for r in rows})
        self.assertEqual(self.f.finish()['code'], 'work_pending')
        reported, = self.f.exchange(self.f.reviewer, ('send_message', {
            'recipient': self.f.worker, 'reply_to': child.seq, 'text': 'Child result returned.'}), source=child)
        report = next(e for e in self.rt.team._messages(self.task) if e.seq == reported['message_seq'])
        self.assertIsNone(self.rt.team._waiting_assignment(self.task, report, self.rt.team._role(self.f.worker)))

    def test_current_formal_review_is_not_withdrawn_or_approved(self):
        artifact = self.f.publish('Review candidate')
        request = self.f.request([artifact])
        source = next(e for e in self.rt.team._messages(self.task) if e.seq == request['message_seq'])
        result = self.withdraw(source)
        self.assertEqual((result['status'], result['code']), ('error', 'formal_review'))
        self.assertEqual(self.rt.team_reviews.view(self.task)[0]['status'], 'pending')
        self.assertEqual(self.f.finish()['code'], 'review_pending')
        self.assertEqual(self.f.events('team.assignment_withdrawn'), [])

    def test_withdrawing_child_does_not_complete_waiting_parent(self):
        parent = self.assign()
        child = self.assign(sender=self.f.worker, recipient=self.f.reviewer, source=parent)
        self.assertEqual(self.withdraw(child, role=self.f.worker)['status'], 'ok')
        rows = self.rt.team.recovery.assignments(self.task)
        row = next(r for r in rows if r['seq'] == parent.seq)
        self.assertEqual(row['status'], 'waiting')
        self.assertTrue(row['has_delegated_work'])
        result = self.f.finish()
        self.assertEqual((result['status'], result['code']), ('error', 'work_pending'))
        self.assertIn(parent.seq, {r['seq'] for r in result['assignments']})
        returned, = self.f.exchange(self.f.worker, ('send_message', {
            'recipient': self.f.manager, 'reply_to': parent.seq, 'text': 'Parent work is actually ready.'}))
        self.assertEqual(returned['status'], 'ok')
        self.assertEqual(self.f.finish()['status'], 'ok')

    def test_unknown_workspace_effect_is_still_pending_after_withdrawal(self):
        root = self.f.bind_workspace()
        source = self.assign()
        run = self.start(source)
        unknown = self.rt.log.emit(type='team.tool_started', task_id=self.task, actor=HOST, payload={
            **run.payload, 'name': 'workspace_write', 'root': str(root), 'call_id': 'unknown-withdrawal-write',
            'arguments': {'path': 'unknown.txt', 'expected_sha256': None}})
        self.rt.log.emit(type='team.run_finished', task_id=self.task, actor=HOST,
                         payload={**run.payload, 'status': 'interrupted', 'reason': 'Fixture unknown effect'})
        self.assertEqual(self.withdraw(source)['status'], 'ok')
        self.assertIn(unknown.seq, {e.seq for e in self.rt.workspaces.pending(task_id=self.task)})
        result = self.f.finish()
        self.assertEqual(result['code'], 'operation_pending')
        self.assertIn(unknown.seq, result['started_seqs'])

    def test_withdrawal_survives_runtime_reopen_without_model_calls(self):
        source = self.assign()
        result = self.withdraw(source)
        db = self.rt.log.path
        self.rt.close()
        with Runtime(db, roles_path=self.f.roles, drivers={self.f.driver.name: self.f.driver}) as rt:
            self.assertEqual(rt.team.view(self.task)['withdrawals'][0]['withdrawal_seq'], result['withdrawal_seq'])
            self.assertEqual(rt.team.recovery.assignments(self.task), [])
            self.assertIsNone(rt.team._claim(self.task))
            self.assertTrue(rt.verify_chain().ok)

    def test_leader_change_does_not_revive_a_previously_withdrawn_request(self):
        source = self.assign(sender=self.f.worker, recipient=self.f.reviewer)
        self.assertEqual(self.withdraw(source)['status'], 'ok')
        previous = self.rt.organization.leader.id
        self.rt.organization = replace(self.rt.organization,
            leader=replace(self.rt.organization.leader, id='new_store_manager'))
        self.assertIn(source.seq, self.rt.team.work_control.inactive(self.task))
        self.assertEqual(self.rt.team.work_control.view(self.task)[0]['leader_id'], previous)
        self.assertNotIn(source.seq, {r['seq'] for r in self.rt.team.recovery.assignments(self.task)})

    def test_old_leader_feedback_remains_inactive_after_leader_change(self):
        source = self.assign()
        progress = self.rt.team._send(self.task, self.rt.team._role(self.f.worker), self.f.manager,
            'Historical progress only.', 'progress', reply_to=source.seq)
        self.assertEqual(self.withdraw(source)['status'], 'ok')
        self.rt.organization = replace(self.rt.organization,
            leader=replace(self.rt.organization.leader, id='replacement_manager'))
        self.assertIn(progress.seq, self.rt.team.work_control.inactive(self.task))
        self.assertEqual(self.rt.team.view(self.task)['pending_count'], 0)
        self.assertIsNone(self.rt.team._claim(self.task))

    def test_external_role_can_withdraw_its_queued_delegation_and_finish(self):
        f = external.TeamExternalTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        def action(run):
            delegated = run.call('delegate', recipient=f.worker, instruction='A duplicate request')
            withdrawn = run.call('withdraw_work', message_seq=delegated['message_seq'], reason='No longer needed')
            self.assertEqual(withdrawn['status'], 'ok', withdrawn)
            run.call('finish_work', summary='The duplicate request was withdrawn.', verification='Fixture only')
        f.driver.scripts[f.manager] = [action]
        f.rt.team.receive(f.task, 'Fixture withdrawal')
        f.rt.collaborate(f.task)
        self.assertEqual(f.rt.team.view(f.task)['status'], 'done')
        self.assertEqual(len(f.events('team.assignment_withdrawn')), 1)
        self.assertEqual(len(f.driver.runs), 1)
        self.assertFalse(f.driver.errors)
        self.assertFalse(f.rt.team._pending_operations(f.task))
