"""Inert native candidates and one-time claims using real loopback HTTP."""
from concurrent.futures import ThreadPoolExecutor
import sqlite3
import threading
import unittest
from unittest.mock import patch

from cater.anthropic import AnthropicMessagesDriver
from cater.drivers import OpenAICompatibleDriver
from cater.events import ChainReport, EventLog
from cater.review_supervision_candidate import create, SupervisionLogIntegrityError
from cater.runtime import Runtime
from cater.team import HOST
from cater.work import WorkConflict
import cater.review_supervision_candidate as candidate_module
import test_native_review_return_evidence as native_fixture
from test_team_external import ScriptedExternalDriver


class NativeReviewCandidateTests(unittest.TestCase):
    def ready(self, cls, **kwargs):
        helper = native_fixture.NativeReviewReturnEvidenceTests(methodName='runTest')
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        f, claimed, _ = helper.run_fixture(cls, **kwargs)
        return f, claimed[2]

    def candidate(self, f, run):
        return create(f.rt.team, f.task, run_seq=run.seq, run_hash=run.hash)

    def claim(self, f, candidate):
        with f.rt.team.recovery.review_candidate_lease(f.task, candidate_seq=candidate.seq,
                                                        candidate_hash=candidate.hash) as claimed:
            return claimed

    def test_native_candidate_and_single_claim_have_barrier_and_no_automatic_launch(self):
        for cls in (OpenAICompatibleDriver, AnthropicMessagesDriver):
            with self.subTest(protocol=cls.__name__):
                f, run = self.ready(cls)
                candidate = self.candidate(f, run)
                self.assertIsNotNone(candidate)
                self.assertIn('native_response', candidate.payload['evidence'])
                self.assertEqual(self.candidate(f, run), candidate)
                self.assertTrue(f.rt.team.recovery.authenticate_review_candidate(f.task, candidate))
                self.assertEqual(f.rt.team.recovery.prepare(f.task), [])
                self.assertIsNone(f.rt.team._claim(f.task))
                self.assertEqual(f.rt.team.view(f.task)['pending_count'], 0)
                claimed = self.claim(f, candidate)
                self.assertIsNotNone(claimed)
                self.assertEqual(claimed[2].payload['customer_input_barrier']['source_hash'], candidate.hash)
                self.assertIsNone(self.claim(f, candidate))
                self.assertEqual(len([e for e in f.events('team.run_started')
                                      if e.payload.get('message_seq') == candidate.seq]), 1)
                self.assertEqual([e for e in f.events('team.model_called')
                                  if e.payload.get('run_id') == claimed[2].payload['run_id']], [])
                self.assertEqual(f.events('team.objective_completed'), [])
                worker = f.rt.team._role(f.worker)
                f.rt.team.receive(f.task, f'@{worker.name} later direction', request_id='native-after-claim')
                with self.assertRaises(WorkConflict):
                    f.rt.team._current(f.task, claimed[2], claimed[1], claimed[2].payload['revision'])
                self.assertTrue(f.rt.verify_chain().ok)

    def test_no_evidence_truncated_response_old_report_and_new_input_fail(self):
        for cls in (OpenAICompatibleDriver, AnthropicMessagesDriver):
            for case in ('contained', 'unclean', 'old', 'input', 'inflight', 'prior_tool'):
                with self.subTest(protocol=cls.__name__, case=case):
                    f, run = self.ready(cls, contained=case == 'contained', bad_cleanup=case == 'unclean',
                                        first_read=case == 'prior_tool', cross_role_input=case == 'inflight')
                    if case == 'old':
                        f.rt.log.emit(type='team.objective_updated', actor=HOST, task_id=f.task,
                                      payload={'goal': 'new goal'})
                    elif case == 'input':
                        worker = f.rt.team._role(f.worker)
                        f.rt.team.receive(f.task, f'@{worker.name} new instruction', request_id='native-after-run')
                    self.assertIsNone(self.candidate(f, run))
                    self.assertEqual([e for e in f.events('team.message_received')
                                      if e.payload.get('trigger') == 'review_return_candidate'], [])

    def test_claim_rechecks_new_input_scope_active_and_unknown_effects(self):
        for cls in (OpenAICompatibleDriver, AnthropicMessagesDriver):
            for case in ('input', 'scope', 'active', 'unknown'):
                with self.subTest(protocol=cls.__name__, case=case):
                    f, run = self.ready(cls)
                    candidate = self.candidate(f, run)
                    self.assertIsNotNone(candidate)
                    if case == 'input':
                        worker = f.rt.team._role(f.worker)
                        f.rt.team.receive(f.task, f'@{worker.name} new direction', request_id='native-before-claim')
                    elif case == 'scope':
                        f.rt.log.emit(type='team.objective_updated', actor=HOST, task_id=f.task, payload={'goal': 'new'})
                    else:
                        notice = f.rt.team._send(f.task, f.rt.team._role(f.manager), f.worker, 'notice', 'notification')
                        other = f.rt.log.emit(type='team.run_started', task_id=f.task, actor=HOST, payload={
                            'message_seq': notice.seq, 'role': f.worker, 'run_id': 'other-test-run', 'revision': 0})
                        if case == 'unknown':
                            f.rt.log.emit(type='team.tool_started', task_id=f.task, actor=HOST, payload={
                                **other.payload, 'name': 'workspace_write', 'call_id': 'uncertain', 'arguments': {}})
                            f.rt.log.emit(type='team.run_finished', task_id=f.task, actor=HOST,
                                          payload={**other.payload, 'status': 'interrupted'})
                    self.assertIsNone(self.claim(f, candidate))
                    self.assertEqual([e for e in f.events('team.run_started') if e.payload.get('message_seq') == candidate.seq], [])

    def test_native_creation_and_claim_use_real_sqlite_write_lock(self):
        for cls in (OpenAICompatibleDriver, AnthropicMessagesDriver):
            with self.subTest(protocol=cls.__name__):
                f, run = self.ready(cls)
                other = EventLog(f.rt.log.path)
                self.addCleanup(other.close)
                other.db.execute('PRAGMA busy_timeout = 0')
                original = candidate_module._review_evidence
                observations = []
                def locked(*args, **kwargs):
                    with self.assertRaises(sqlite3.OperationalError) as error:
                        other.db.execute('BEGIN IMMEDIATE')
                    observations.append(error.exception.sqlite_errorcode)
                    return original(*args, **kwargs)
                with patch.object(candidate_module, '_review_evidence', side_effect=locked):
                    candidate = self.candidate(f, run)
                    self.assertIsNotNone(candidate)
                    self.assertIsNotNone(self.claim(f, candidate))
                self.assertGreaterEqual(len(observations), 2)
                self.assertTrue(all(code == sqlite3.SQLITE_BUSY for code in observations))

    def test_concurrent_creators_and_claimers_are_single_use(self):
        for cls in (OpenAICompatibleDriver, AnthropicMessagesDriver):
            with self.subTest(protocol=cls.__name__):
                f, run = self.ready(cls)
                def race(operation):
                    gate = threading.Barrier(2)
                    def worker():
                        driver = ScriptedExternalDriver()
                        rt = Runtime(f.rt.log.path, roles_path=f.roles, drivers={driver.name: driver})
                        try:
                            gate.wait(timeout=5)
                            return operation(rt)
                        finally:
                            rt.close()
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        return [future.result(timeout=15) for future in (pool.submit(worker), pool.submit(worker))]
                sequences = race(lambda rt: (c.seq if (c := create(rt.team, f.task, run_seq=run.seq, run_hash=run.hash)) else None))
                self.assertIsNotNone(sequences[0])
                self.assertEqual(sequences[0], sequences[1])
                candidate = next(e for e in f.events('team.message_received') if e.seq == sequences[0])
                def claim(rt):
                    with rt.team.recovery.review_candidate_lease(f.task, candidate_seq=candidate.seq,
                                                                  candidate_hash=candidate.hash) as result:
                        return result[2].seq if result else None
                claimed = race(claim)
                self.assertEqual(sum(value is not None for value in claimed), 1)
                self.assertTrue(f.rt.verify_chain().ok)

    def test_log_integrity_is_rechecked_in_creation_and_claim_transactions(self):
        for phase in ('create', 'claim'):
            with self.subTest(phase=phase):
                f, run = self.ready(OpenAICompatibleDriver)
                candidate = self.candidate(f, run) if phase == 'claim' else None
                self.assertEqual(candidate is not None, phase == 'claim')
                # Only the disposable fixture DB is deliberately corrupted.
                f.rt.log.db.execute('DROP TRIGGER events_no_update')
                f.rt.log.db.execute("UPDATE events SET payload_json = '{}' WHERE seq = ?", (run.seq,))
                f.rt.log.db.commit()
                self.assertFalse(f.rt.verify_chain().ok)
                with self.assertRaisesRegex(SupervisionLogIntegrityError, '完整性校验未通过'):
                    if candidate is None:
                        self.candidate(f, run)
                    else:
                        self.claim(f, candidate)

    def test_missing_audit_key_is_distinct_from_normal_no_candidate(self):
        helper = native_fixture.NativeReviewReturnEvidenceTests(methodName='runTest')
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        f, claimed, _ = helper.run_fixture(OpenAICompatibleDriver, audit='signed')
        run = claimed[2]
        candidate = self.candidate(f, run)
        self.assertIsNotNone(candidate)
        driver = ScriptedExternalDriver()
        reopened = Runtime(f.rt.log.path, roles_path=f.roles, drivers={driver.name: driver})
        self.addCleanup(reopened.close)
        with self.assertRaisesRegex(SupervisionLogIntegrityError, '缺少.*验证密钥') as caught:
            create(reopened.team, f.task, run_seq=run.seq, run_hash=run.hash)
        self.assertNotIn('offline-test-key', str(caught.exception))
        self.assertNotIn('key_id', str(caught.exception))
        with self.assertRaisesRegex(SupervisionLogIntegrityError, '缺少.*验证密钥'):
            with reopened.team.recovery.review_candidate_lease(f.task, candidate_seq=candidate.seq,
                                                                candidate_hash=candidate.hash):
                pass
        self.assertIsNone(create(f.rt.team, f.task, run_seq=run.seq, run_hash='invalid'))

    def test_existing_activity_failure_path_reports_safe_diagnostic(self):
        from cater.web import ActivityRunner
        f, run = self.ready(OpenAICompatibleDriver)
        def factory():
            return Runtime(f.rt.log.path, roles_path=f.roles, drivers={f.driver.name: f.driver},
                           close_drivers_on_close=False)
        runner = ActivityRunner(factory)
        runner._activities[f.task] = {'id': 'offline-activity', 'action': 'run', 'state': 'queued'}
        def collaborate(rt, task_id, **kwargs):
            return create(rt.team, task_id, run_seq=run.seq, run_hash=run.hash)
        report = ChainReport(False, 1, broken_at=1,
                             reason="signature present but no audit key supplied for key_id='private-key-id'")
        with patch.object(Runtime, 'collaborate', autospec=True, side_effect=collaborate), \
                patch.object(EventLog, 'verify_chain', return_value=report):
            runner._run_owned(f.task, 'offline-activity', 'run')
        self.assertFalse(f.events('activity.failed'))
        status = runner.status(f.task)
        self.assertEqual(status['state'], 'blocked')
        self.assertIn('缺少', status['error'])
        self.assertNotIn('private-key-id', status['error'])
        self.assertFalse(f.events('activity.completed'))


if __name__ == '__main__':
    unittest.main()
