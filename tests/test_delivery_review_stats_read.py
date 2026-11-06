"""Offline integration of review statistics with the actual read_delivery tool."""
import unittest
from unittest.mock import patch

from cater.events import Actor, EventLog
from cater.team import HOST
from cater.review_delivery_stats import project_review_delivery_stats
import test_team_review_cycle as fixtures


class DeliveryReviewStatsReadTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.TeamReviewCycleTests(methodName='runTest')
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def deliver(self, *, with_review=True):
        f = self.f
        first = f.publish()
        if with_review:
            req = f.request([first])['review_seq']
            f.record(req, 'changes_requested', feedback='PRIVATE_REVIEW_FEEDBACK_FIXTURE')
            second = f.publish('version two\n')
            newer = f.request([second], review_seq=req)['review_seq']
            f.record(newer, 'approved', feedback='PRIVATE_SECOND_FEEDBACK_FIXTURE')
            f.acknowledge_latest_review()
        f.finish()
        return next(e.seq for e in f.rt.log.read(task_id=f.task) if e.type == 'team.objective_completed')

    def test_actual_tool_and_read_return_same_public_counts(self):
        f = self.f
        seq = self.deliver()
        direct = f.rt.team.delivery.read(f.task, {})
        tool, = f.exchange(f.manager, ('read_delivery', {'delivery_seq': seq}))
        self.assertEqual(tool['review_stats'], direct['review_stats'])
        self.assertEqual(tool['source_status'], 'direction_attributed')
        self.assertIn('不证明', tool['source_note'])
        stats = tool['review_stats']
        self.assertTrue(stats['complete'])
        self.assertEqual((stats['review_conclusions'], stats['changes_requested'],
                          stats['artifact_set_change_resubmissions']), (2, 1, 1))
        self.assertNotIn('PRIVATE_', str(stats))
        self.assertNotIn('chains', stats)

    def test_no_reviews_and_historical_delivery(self):
        f = self.f
        seq = self.deliver(with_review=False)
        before = f.rt.team.delivery.read(f.task, {})
        self.assertTrue(before['review_stats']['complete'])
        self.assertEqual(before['review_stats']['review_conclusions'], 0)
        self.assertFalse(before['historical'])
        f.rt.command(f.task, 'Change direction', goal='New direction')
        after = f.rt.team.delivery.read(f.task, {'delivery_seq': seq})
        self.assertTrue(after['historical'])
        self.assertEqual(after['source_status'], 'direction_attributed')
        self.assertEqual(before['review_stats'], after['review_stats'])

    def test_unattributed_fallback_preserves_body_and_actual_tool_warns(self):
        f = self.f
        f.rt.command(f.task, 'Change direction', goal='New direction')
        saved = f.rt.log.emit(type='team.objective_completed', task_id=f.task, actor=HOST,
            payload={'run_id': 'missing-run', 'summary': 'Historical fixture body',
                     'verification': 'Historical fixture verification'})
        direct = f.rt.team.delivery.read(f.task, {})
        tool, = f.exchange(f.manager, ('read_delivery', {}))
        for result in (direct, tool):
            self.assertEqual(result['status'], 'ok')
            self.assertEqual(result['delivery']['seq'], saved.seq)
            self.assertEqual(result['delivery']['summary'], 'Historical fixture body')
            self.assertEqual(result['delivery']['verification'], 'Historical fixture verification')
            self.assertTrue(result['historical'])
            self.assertEqual(result['source_status'], 'unverified')
            self.assertIn('来源', result['source_note'])
            self.assertIn('不能据此认定', result['source_note'])
            self.assertFalse(result['review_stats']['complete'])
        explicit = f.rt.team.delivery.read(f.task, {'delivery_seq': saved.seq})
        self.assertEqual(explicit['source_status'], 'unverified')
        forged_stats = {'delivery_seq': saved.seq, 'complete': True, 'review_conclusions': 1,
                        'changes_requested': 0, 'artifact_set_change_resubmissions': 0}
        with patch('cater.review_delivery_stats.project_review_delivery_stats', return_value=forged_stats):
            result = f.rt.team.delivery.read(f.task, {'delivery_seq': saved.seq})
        self.assertEqual(result['source_status'], 'unverified')
        self.assertFalse(result['review_stats']['complete'])

    def test_legacy_sourceless_completion_is_not_source_verified(self):
        f = self.f
        saved = f.rt.log.emit(type='team.objective_completed', task_id=f.task, actor=HOST,
            payload={'summary': 'Legacy body', 'verification': 'Legacy verification'})
        for arguments in ({}, {'delivery_seq': saved.seq}):
            result = f.rt.team.delivery.read(f.task, arguments)
            self.assertEqual(result['status'], 'ok')
            self.assertEqual(result['delivery']['summary'], 'Legacy body')
            self.assertEqual(result['source_status'], 'unverified')
            self.assertIn('无法核对', result['source_note'])
            self.assertFalse(result['review_stats']['complete'])

    def test_borrowed_real_run_without_finish_receipt_is_unverified(self):
        f = self.f
        f.exchange(f.manager)
        run = [e for e in f.rt.log.read(task_id=f.task) if e.type == 'team.run_started'][-1]
        saved = f.rt.log.emit(type='team.objective_completed', task_id=f.task, actor=HOST,
            payload={'run_id': run.payload['run_id'], 'summary': 'Borrowed run fixture',
                     'verification': 'Fixture only', 'artifacts': []})
        direct = f.rt.team.delivery.read(f.task, {'delivery_seq': saved.seq})
        self.assertEqual(direct['source_status'], 'unverified')
        self.assertEqual(direct['delivery']['summary'], 'Borrowed run fixture')
        tool, = f.exchange(f.manager, ('read_delivery', {'delivery_seq': saved.seq}))
        self.assertEqual(tool['status'], 'ok')
        self.assertEqual(tool['source_status'], 'unverified')
        self.assertIn('无法核对', tool['source_note'])

    def test_receipt_contract_conflicts_and_changed_arguments_are_unverified(self):
        from dataclasses import replace
        f = self.f
        seq = self.deliver()
        real_read = f.rt.log.read
        baseline = real_read(task_id=f.task)
        finish = next(e for e in baseline if e.type == 'team.tool_started' and e.payload.get('name') == 'finish_work')
        receipt = next(e for e in baseline if e.type == 'team.tool_finished' and e.payload.get('started_seq') == finish.seq)
        for mode in ('missing_receipt', 'failed_receipt', 'wrong_role', 'no_contract', 'wrong_summary', 'wrong_artifact', 'unknown_argument', 'conflict'):
            with self.subTest(mode=mode):
                def altered(*args, **kwargs):
                    events = real_read(*args, **kwargs)
                    if mode == 'missing_receipt':
                        return [e for e in events if e.seq != receipt.seq]
                    if mode == 'conflict':
                        extra = replace(receipt, seq=max(e.seq for e in events) + 1,
                                        payload={**receipt.payload, 'result': {'status': 'error'}})
                        return [*events, extra]
                    out = []
                    for e in events:
                        if e.seq == receipt.seq and mode == 'failed_receipt':
                            e = replace(e, payload={**e.payload, 'result': {'status': 'error'}})
                        if e.seq == receipt.seq and mode == 'wrong_role':
                            e = replace(e, payload={**e.payload, 'role': 'wrong'})
                        if e.seq == finish.seq and mode == 'no_contract':
                            e = replace(e, payload={k: v for k, v in e.payload.items() if k != 'execution_contract'})
                        if e.seq == finish.seq and mode == 'unknown_argument':
                            e = replace(e, payload={**e.payload, 'arguments': {**e.payload['arguments'],
                                        'unexpected_fixture': 'PRIVATE_UNKNOWN_FIELD'}})
                        if e.seq == seq and mode == 'wrong_summary':
                            e = replace(e, payload={**e.payload, 'summary': 'Different saved summary'})
                        if e.seq == seq and mode == 'wrong_artifact':
                            refs = [{**e.payload['artifacts'][0], 'content_sha256': '0' * 64}]
                            e = replace(e, payload={**e.payload, 'artifacts': refs})
                        out.append(e)
                    return out
                with patch.object(f.rt.log, 'read', side_effect=altered):
                    result = f.rt.team.delivery.read(f.task, {'delivery_seq': seq})
                self.assertEqual(result['status'], 'ok')
                self.assertEqual(result['source_status'], 'unverified', mode)
                self.assertFalse(result['review_stats']['complete'])
                self.assertNotIn('PRIVATE_UNKNOWN_FIELD', str(result))
                if mode == 'unknown_argument':
                    self.assertEqual(result['delivery']['summary'], 'Fixture delivered')
                    with patch.object(f.rt.log, 'read', side_effect=altered):
                        tool, = f.exchange(f.manager, ('read_delivery', {'delivery_seq': seq}))
                    self.assertEqual(tool['source_status'], 'unverified')
                    self.assertEqual(tool['delivery']['summary'], 'Fixture delivered')
                    self.assertNotIn('PRIVATE_UNKNOWN_FIELD', str(tool))

    def test_unavailable_projection_does_not_block_or_leak(self):
        f = self.f
        seq = self.deliver()
        target = 'cater.review_delivery_stats.project_review_delivery_stats'
        for outcome in (ValueError('PRIVATE_EXCEPTION_FIXTURE'), {'complete': False},
                        {'complete': True, 'delivery_seq': seq, 'review_conclusions': True}):
            with self.subTest(outcome=type(outcome)):
                with patch(target, side_effect=outcome if isinstance(outcome, Exception) else None,
                           return_value=outcome if isinstance(outcome, dict) else None):
                    response = f.rt.team.delivery.read(f.task, {'delivery_seq': seq})
                self.assertEqual(response['status'], 'ok')
                self.assertEqual(response['delivery']['seq'], seq)
                self.assertFalse(response['review_stats']['complete'])
                self.assertIn('待核对', response['review_stats']['note'])
                self.assertNotIn('PRIVATE_EXCEPTION_FIXTURE', str(response))
                self.assertNotIn('review_conclusions', response['review_stats'])

    def test_missing_old_artifacts_degrades_to_unknown(self):
        f = self.f
        seq = self.deliver()
        original_read = f.rt.log.read
        def old_format(*args, **kwargs):
            from dataclasses import replace
            return [replace(e, payload={k: v for k, v in e.payload.items() if k != 'artifacts'})
                    if e.seq == seq else e for e in original_read(*args, **kwargs)]
        with patch.object(f.rt.log, 'read', side_effect=old_format):
            response = f.rt.team.delivery.read(f.task, {'delivery_seq': seq})
        self.assertEqual(response['status'], 'ok')
        self.assertNotIn('artifacts', response['delivery'])
        self.assertFalse(response['review_stats']['complete'])

    def test_transaction_ownership_and_exception(self):
        f = self.f
        self.deliver()
        db = f.rt.log.db
        self.assertFalse(db.in_transaction)
        with patch.object(f.rt.team, 'view', side_effect=RuntimeError('fixture')):
            with self.assertRaises(RuntimeError):
                f.rt.team.delivery.read(f.task, {})
        self.assertFalse(db.in_transaction)
        db.execute('BEGIN')
        try:
            f.rt.team.delivery.read(f.task, {})
            self.assertTrue(db.in_transaction)
            with patch.object(f.rt.team, 'view', side_effect=RuntimeError('fixture')):
                with self.assertRaises(RuntimeError):
                    f.rt.team.delivery.read(f.task, {})
            self.assertTrue(db.in_transaction)
        finally:
            db.rollback()

    def test_two_connections_snapshot_keeps_delivery_and_status_consistent(self):
        f = self.f
        seq = self.deliver()
        f.rt.log.db.execute('PRAGMA journal_mode=WAL')
        writer = EventLog(f.rt.log.path)
        self.addCleanup(writer.close)
        original_read = f.rt.log.read
        appended = []
        def append_after_events(*args, **kwargs):
            events = original_read(*args, **kwargs)
            if not appended:
                self.assertTrue(f.rt.log.db.in_transaction)
                appended.append(writer.emit(type='human.commanded', task_id=f.task, actor=Actor.human(),
                    payload={'instruction': 'New direction', 'intent': 'direction', 'goal': 'New goal'}).seq)
                self.assertIsNone(f.rt.log.db.execute('SELECT seq FROM events WHERE seq = ?', (appended[0],)).fetchone())
            return events
        with patch.object(f.rt.log, 'read', side_effect=append_after_events):
            first = f.rt.team.delivery.read(f.task, {})
        self.assertEqual(len(appended), 1)
        self.assertEqual(first['delivery']['seq'], seq)
        self.assertTrue(first['review_stats']['complete'])
        self.assertFalse(first['historical'])
        later = f.rt.team.delivery.read(f.task, {'delivery_seq': seq})
        self.assertTrue(later['historical'])
        self.assertEqual(first['review_stats'], later['review_stats'])


if __name__ == '__main__':
    unittest.main()
