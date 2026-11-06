"""Candidate creation uses real events, disposable DBs, and offline scripted drivers."""
import unittest
from concurrent.futures import ThreadPoolExecutor
import threading
import sqlite3
from unittest.mock import patch

from cater.events import Actor
from cater.review_supervision_candidate import TRIGGER, create
from cater.runtime import Runtime
from cater.team import HOST
from cater.work import WorkConflict
from test_return_work import ReturnWorkFixtures


class ReviewSupervisionCandidateTests(ReturnWorkFixtures, unittest.TestCase):
    def ready(self, *, reviewer_cleanup=True, consultation=False):
        f = self.fixture('external')
        artifact, = self.run_calls(f, self.human(f), ('publish_artifact', {
            'filename': 'review.txt', 'content': 'reviewed content', 'summary': 'fixture', 'format': 'text'}))
        review, = self.run_calls(f, self.human(f), ('request_review', {
            'reviewer': f.reviewer, 'artifact_seqs': [artifact['artifact_seq']], 'instruction': 'Inspect'}))
        assignment = self.message(f, review['message_seq'])
        if consultation:
            question = f.rt.team.receive(f.task, '这是普通咨询：请解释复核流程，不要交付。', request_id='consultation-before-review')
            self.run_calls(f, question, ('reply_to_customer', {'text': '复核流程是读取文件并登记意见。'}), statuses=('completed',))
        record = ('record_review', {'review_seq': review['review_seq'], 'verdict': 'approved', 'feedback': 'Checked'})
        if reviewer_cleanup:
            result, = self.run_calls(f, assignment, record, statuses=('waiting',))
        else:
            agent_run = f.driver.agent_run
            def unclean(**kwargs):
                run = agent_run(**kwargs)
                run.cleanup_complete = False
                return run
            with patch.object(f.driver, 'agent_run', side_effect=unclean):
                result, = self.run_calls(f, assignment, record, statuses=('interrupted',))
        report = self.message(f, result['message_seq'])
        claimed = f.rt.team._claim(f.task)
        self.assertEqual(claimed[0].seq, report.seq)
        f.driver.scripts[f.manager] = [lambda run: 'Internal review acknowledged.']
        list(f.rt.team._run_message(f.task, *claimed))
        return f, claimed[2]

    def candidate(self, f, run):
        return create(f.rt.team, f.task, run_seq=run.seq, run_hash=run.hash)

    def candidates(self, f):
        return [e for e in f.events('team.message_received') if e.payload.get('trigger') == TRIGGER]

    def test_creates_once_inert_record_with_authenticated_evidence(self):
        f, run = self.ready()
        candidate = self.candidate(f, run)
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.actor, HOST)
        self.assertEqual(candidate.payload['original_run_seq'], run.seq)
        self.assertEqual(candidate.payload['original_run_hash'], run.hash)
        self.assertEqual(candidate.payload['scope'], {**f.rt.team.recovery.scope(f.task),
                         'work_direction_seq': f.rt.work.scope(f.task)['direction_seq']})
        self.assertEqual(candidate.payload['evidence']['leader_run'], {'seq': run.seq, 'hash': run.hash})
        self.assertEqual(self.candidate(f, run), candidate)
        self.assertEqual(self.candidates(f), [candidate])
        self.assertFalse(f.rt.team.recovery.is_message(candidate))
        self.assertTrue(f.rt.team.recovery.authenticate_review_candidate(f.task, candidate))
        self.assertEqual(f.rt.team.view(f.task)['pending_count'], 0)
        self.assertIsNone(f.rt.team._claim(f.task))
        self.assertEqual(f.events('team.objective_completed'), [])
        self.assertTrue(f.rt.verify_chain().ok)

    def test_two_independent_connections_create_one_candidate(self):
        from test_team_external import ScriptedExternalDriver
        f, run = self.ready()
        barrier = threading.Barrier(2)
        def worker():
            driver = ScriptedExternalDriver()
            rt = Runtime(f.rt.log.path, roles_path=f.roles, drivers={driver.name: driver})
            try:
                barrier.wait(timeout=5)
                result = create(rt.team, f.task, run_seq=run.seq, run_hash=run.hash)
                return result.seq if result else None
            finally:
                rt.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = [future.result(timeout=15) for future in (pool.submit(worker), pool.submit(worker))]
        self.assertIsNotNone(first)
        self.assertEqual(first, second)
        self.assertEqual(len(self.candidates(f)), 1)
        self.assertTrue(f.rt.verify_chain().ok)

    def test_customer_write_cannot_interleave_candidate_transaction(self):
        import cater.review_supervision_candidate as candidate_module
        from cater.events import EventLog
        f, run = self.ready()
        other = EventLog(f.rt.log.path)
        self.addCleanup(other.close)
        other.db.execute('PRAGMA busy_timeout = 0')
        original = candidate_module.authenticate
        observations = []
        def probe(*args, **kwargs):
            with self.assertRaises(sqlite3.OperationalError) as error:
                other.db.execute('BEGIN IMMEDIATE')
            observations.append(error.exception.sqlite_errorcode)
            return original(*args, **kwargs)
        with patch.object(candidate_module, 'authenticate', side_effect=probe):
            candidate = self.candidate(f, run)
        self.assertIsNotNone(candidate)
        self.assertEqual(observations, [sqlite3.SQLITE_BUSY])
        message = other.emit(type='team.message_received', task_id=f.task, actor=Actor.human(),
                             payload={'kind': 'message', 'recipient': f.worker, 'text': 'later'})
        self.assertGreater(message.seq, candidate.seq)

    def test_prior_customer_reply_or_queued_assignment_blocks_candidate(self):
        for case in ('customer_reply', 'queued_assignment'):
            with self.subTest(case=case):
                f, run = self.ready()
                if case == 'customer_reply':
                    role = f.rt.team._role(f.manager)
                    f.rt.log.emit(type='msg.said', task_id=f.task, actor=Actor.agent(role.id, role.role),
                                  payload={'run_id': run.payload['run_id'], 'audience': 'customer', 'text': 'answer'})
                else:
                    f.rt.team._send(f.task, f.rt.team._role(f.manager), f.worker, 'still working', 'assignment')
                self.assertIsNone(self.candidate(f, run))
                self.assertEqual(self.candidates(f), [])

    def test_wrong_run_new_customer_or_scope_are_rejected(self):
        for case in ('wrong_hash', 'cross_role_customer', 'scope'):
            with self.subTest(case=case):
                f, run = self.ready()
                if case == 'wrong_hash':
                    self.assertIsNone(create(f.rt.team, f.task, run_seq=run.seq, run_hash='wrong'))
                elif case == 'cross_role_customer':
                    role = f.rt.team._role(f.worker)
                    f.rt.team.receive(f.task, f'@{role.name} new input', request_id='new-cross-role')
                    self.assertIsNone(self.candidate(f, run))
                else:
                    f.rt.log.emit(type='team.objective_updated', actor=HOST, task_id=f.task,
                                  payload={'goal': 'new objective'})
                    self.assertIsNone(self.candidate(f, run))
                self.assertEqual(self.candidates(f), [])

    def test_active_run_and_unknown_external_effect_prevent_creation(self):
        for case in ('active', 'unknown'):
            with self.subTest(case=case):
                f, run = self.ready()
                source = f.rt.team._send(f.task, f.rt.team._role(f.manager), f.worker, 'inspect', 'notification')
                peer = self.start(f, source)
                if case == 'unknown':
                    f.rt.log.emit(type='team.tool_started', task_id=f.task, actor=HOST, payload={
                        **peer.payload, 'name': 'workspace_write', 'call_id': 'unknown', 'arguments': {}})
                    f.rt.log.emit(type='team.run_finished', task_id=f.task, actor=HOST,
                                  payload={**peer.payload, 'status': 'interrupted'})
                self.assertIsNone(self.candidate(f, run))
                self.assertEqual(self.candidates(f), [])

    def test_reviewers_unconfirmed_process_cleanup_blocks_candidate(self):
        f, run = self.ready(reviewer_cleanup=False)
        pending = f.rt.team._pending_operations(f.task)
        self.assertTrue(any(row['name'] == 'external_agent' and row['role'] == f.reviewer for row in pending))
        self.assertIsNone(self.candidate(f, run))
        self.assertEqual(self.candidates(f), [])

    def test_old_finish_does_not_settle_a_later_reused_run_identity(self):
        f, run = self.ready()
        f.rt.log.emit(type='team.run_started', task_id=f.task, actor=HOST, payload=dict(run.payload))
        self.assertIsNone(self.candidate(f, run))
        self.assertEqual(self.candidates(f), [])

    def test_exception_rolls_back_candidate_and_outer_transaction_is_rejected(self):
        f, run = self.ready()
        emit = f.rt.log.emit
        def fail_after_write(**kwargs):
            value = emit(**kwargs)
            if kwargs.get('type') == 'team.message_received' and kwargs.get('payload', {}).get('trigger') == TRIGGER:
                raise OSError('fixture failure after insert')
            return value
        with patch.object(f.rt.log, 'emit', side_effect=fail_after_write), self.assertRaises(OSError):
            self.candidate(f, run)
        self.assertEqual(self.candidates(f), [])
        with f.rt.log.atomic(), self.assertRaises(WorkConflict):
            self.candidate(f, run)
        self.assertEqual(len(self.candidates(f)), 0)
        self.assertIsNotNone(self.candidate(f, run))
