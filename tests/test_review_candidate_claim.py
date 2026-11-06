"""Explicit candidate claim only: offline drivers, no supervisor execution."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import threading
import subprocess
import sys
import time
from pathlib import Path
import unittest
from unittest.mock import patch

from cater.events import Actor
from cater.review_supervision_candidate import authenticate_history, create
from cater.runtime import Runtime
from cater.team import HOST
from cater.team_reply import customer_source
from cater.work import WorkConflict
import test_review_supervision_candidate as candidate_fixture


class ReviewCandidateClaimTests(unittest.TestCase):
    def ready(self):
        fixture = candidate_fixture.ReviewSupervisionCandidateTests(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        f, run = fixture.ready()
        candidate = create(f.rt.team, f.task, run_seq=run.seq, run_hash=run.hash)
        self.assertIsNotNone(candidate)
        return f, run, candidate

    def claim(self, f, candidate):
        with f.rt.team.recovery.review_candidate_lease(f.task, candidate_seq=candidate.seq, candidate_hash=candidate.hash) as claimed:
            return claimed

    def test_claim_once_binds_barrier_without_model_or_customer_reply(self):
        f, run, candidate = self.ready()
        before_runs = len(f.driver.runs)
        self.assertTrue(f.rt.team.recovery.authenticate_review_candidate(f.task, candidate))
        claimed = self.claim(f, candidate)
        self.assertIsNotNone(claimed)
        source, role, started = claimed
        self.assertEqual(source, candidate)
        self.assertEqual(started.payload['customer_input_barrier'], {
            'version': 1, 'kind': 'all_customer_messages', 'source_seq': candidate.seq, 'source_hash': candidate.hash})
        self.assertEqual(len(f.driver.runs), before_runs)
        self.assertIsNone(self.claim(f, candidate))
        self.assertIsNone(customer_source(f.rt.team, f.task, candidate, role))
        _, _, _, tools, _ = f.rt.team._model_input(f.task, candidate, role, run_id=started.payload['run_id'])
        self.assertNotIn('reply_to_customer', {tool['name'] for tool in tools})
        self.assertEqual(f.events('team.objective_completed'), [])
        self.assertTrue(f.rt.verify_chain().ok)

    def test_other_role_customer_after_candidate_preempts_claim(self):
        f, _, candidate = self.ready()
        worker = f.rt.team._role(f.worker)
        f.rt.team.receive(f.task, f'@{worker.name} new direction', request_id='after-candidate-customer')
        self.assertTrue(f.rt.team.recovery.authenticate_review_candidate(f.task, candidate))
        self.assertIsNone(self.claim(f, candidate))
        self.assertFalse(any(e.payload.get('message_seq') == candidate.seq for e in f.events('team.run_started')))

    def test_cross_role_customer_after_claim_is_caught_by_bound_barrier(self):
        f, _, candidate = self.ready()
        source, role, started = self.claim(f, candidate)
        worker = f.rt.team._role(f.worker)
        f.rt.team.receive(f.task, f'@{worker.name} new direction', request_id='after-candidate-claim')
        with self.assertRaises(WorkConflict):
            f.rt.team._current(f.task, started, role, started.payload['revision'])
        self.assertEqual(f.events('team.objective_completed'), [])

    def test_unknown_effect_and_changed_scope_block_claim(self):
        for case in ('unknown', 'scope'):
            with self.subTest(case=case):
                f, _, candidate = self.ready()
                if case == 'scope':
                    f.rt.log.emit(type='team.objective_updated', task_id=f.task, actor=HOST, payload={'goal': 'new'})
                else:
                    source = f.rt.team._send(f.task, f.rt.team._role(f.manager), f.worker, 'notice', 'notification')
                    run = f.rt.log.emit(type='team.run_started', task_id=f.task, actor=HOST, payload={
                        'message_seq': source.seq, 'role': f.worker, 'run_id': 'unknown-effect', 'revision': 0})
                    f.rt.log.emit(type='team.tool_started', task_id=f.task, actor=HOST, payload={
                        **run.payload, 'name': 'workspace_write', 'call_id': 'uncertain', 'arguments': {}})
                    f.rt.log.emit(type='team.run_finished', task_id=f.task, actor=HOST,
                                  payload={**run.payload, 'status': 'interrupted'})
                self.assertIsNone(self.claim(f, candidate))

    def test_other_roles_unrelated_customer_reply_does_not_block(self):
        f, _, candidate = self.ready()
        worker = f.rt.team._role(f.worker)
        f.rt.log.emit(type='msg.said', task_id=f.task, actor=Actor.agent(worker.id, worker.role),
            payload={'run_id': 'unrelated', 'audience': 'customer', 'text': 'An unrelated answer'})
        self.assertIsNotNone(self.claim(f, candidate))

    def test_forged_or_changed_history_does_not_authenticate(self):
        f, _, candidate = self.ready()
        events = f.rt.log.read(task_id=f.task)
        cases = [replace(candidate, actor=Actor.human()),
                 replace(candidate, payload={**candidate.payload, 'original_run_hash': 'wrong'}),
                 replace(candidate, payload={**candidate.payload, 'recipient': f.worker}),
                 replace(candidate, task_id='other-task')]
        for forged in cases:
            with self.subTest(forged=forged):
                forged_events = [forged if event.seq == candidate.seq else event for event in events]
                self.assertFalse(authenticate_history(f.rt.team, f.task, forged, events=forged_events))
        evidence = candidate.payload['evidence']
        mutations = [
            (evidence['review_decision']['seq'], {'actor': Actor.human()}),
            (evidence['external_start']['seq'], {'payload': {'run_started_hash': 'wrong'}}),
            (evidence['external_finish']['seq'], {'payload': {'cleanup_complete': False}}),
            (evidence['report']['seq'], {'task_id': 'other-task'}),
        ]
        for seq, changes in mutations:
            mutated = []
            for event in events:
                if event.seq == seq:
                    if 'payload' in changes:
                        event = replace(event, payload={**event.payload, **changes['payload']})
                    else:
                        event = replace(event, **changes)
                mutated.append(event)
            self.assertFalse(authenticate_history(f.rt.team, f.task, candidate, events=mutated))
        with f.rt.team.recovery.review_candidate_lease(f.task, candidate_seq=candidate.seq, candidate_hash='wrong') as claimed:
            self.assertIsNone(claimed)

    def test_duplicate_candidate_history_is_rejected(self):
        f, _, candidate = self.ready()
        f.rt.log.emit(type='team.message_received', task_id=f.task, actor=HOST, payload=dict(candidate.payload))
        self.assertFalse(f.rt.team.recovery.authenticate_review_candidate(f.task, candidate))
        self.assertIsNone(self.claim(f, candidate))

    def test_claim_write_failure_rolls_back_and_can_be_retried(self):
        f, _, candidate = self.ready()
        emit = f.rt.log.emit
        def fail_after_insert(**kwargs):
            value = emit(**kwargs)
            if kwargs.get('type') == 'team.run_started' and kwargs.get('payload', {}).get('message_seq') == candidate.seq:
                raise OSError('injected failure')
            return value
        with patch.object(f.rt.log, 'emit', side_effect=fail_after_insert), self.assertRaises(OSError):
            self.claim(f, candidate)
        self.assertFalse(any(e.payload.get('message_seq') == candidate.seq for e in f.events('team.run_started')))
        self.assertIsNotNone(self.claim(f, candidate))

    def test_claim_requires_lock_and_cannot_execute_after_lease_ends(self):
        f, _, candidate = self.ready()
        with self.assertRaises(WorkConflict):
            f.rt.team.recovery.claim_review_candidate(f.task, candidate_seq=candidate.seq, candidate_hash=candidate.hash)
        claimed = self.claim(f, candidate)
        with self.assertRaises(WorkConflict):
            list(f.rt.team._run_message(f.task, *claimed))
        self.assertEqual([e for e in f.events('team.external_run_started')
                          if e.payload.get('message_seq') == candidate.seq], [])

    def test_scheduler_marker_without_owned_drain_cannot_claim(self):
        f, _, candidate = self.ready()
        f.rt.team.external.begin_scheduler(f.task)
        try:
            with self.assertRaises(WorkConflict):
                with f.rt.team.recovery.drain_review_candidate_lease(
                        f.task, candidate_seq=candidate.seq, candidate_hash=candidate.hash):
                    self.fail('unowned drain must not claim')
        finally:
            f.rt.team.external.end_scheduler()
        self.assertFalse(any(e.payload.get('message_seq') == candidate.seq for e in f.events('team.run_started')))

    def test_real_process_claim_lock_and_crash_never_replay(self):
        from cater.web import ActivityRunner
        for mode in ('before_execution', 'unknown'):
            with self.subTest(mode=mode):
                f, _, candidate = self.ready()
                marker = f.root / 'claimed-ready'
                peer = Path(__file__).parent / 'fixtures' / 'review_candidate_crash_peer.py'
                process = subprocess.Popen([sys.executable, '-u', str(peer.resolve()), str(f.rt.log.path), str(f.roles),
                    f.task, str(candidate.seq), candidate.hash, str(marker), mode], stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                self.addCleanup(lambda p=process: p.kill() if p.poll() is None else None)
                deadline = time.monotonic() + 10
                while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(.02)
                if not marker.exists():
                    process.kill()
                    _, err = process.communicate(timeout=5)
                    self.fail(f'child did not claim: {err.decode(errors="replace")}')
                def factory():
                    return Runtime(f.rt.log.path, roles_path=f.roles, drivers={f.driver.name: f.driver},
                                   close_drivers_on_close=False)
                runner = ActivityRunner(factory)
                with patch.object(runner, '_start') as start:
                    runner.recover_interrupted()
                    start.assert_not_called()
                    self.assertFalse([e for e in f.events('team.run_finished')
                                      if e.payload.get('message_seq') == candidate.seq])
                    process.kill()
                    process.communicate(timeout=5)
                    runner.recover_interrupted()
                    start.assert_not_called()
                finished, = [e for e in f.events('team.run_finished') if e.payload.get('message_seq') == candidate.seq]
                self.assertEqual(finished.payload['status'], 'interrupted')
                with f.rt.team.recovery.review_candidate_lease(f.task, candidate_seq=candidate.seq,
                                                               candidate_hash=candidate.hash) as again:
                    self.assertIsNone(again)
                pending = [row for row in f.rt.team._pending_operations(f.task)
                           if row.get('name') == 'external_agent' and row.get('message_seq') == candidate.seq]
                self.assertEqual(bool(pending), mode == 'unknown')
                if pending:
                    self.assertEqual(pending[0]['outcome'], 'unknown')
                self.assertEqual(len([e for e in f.events('team.run_started')
                                      if e.payload.get('message_seq') == candidate.seq]), 1)

    def test_concurrent_claim_and_restart_are_at_most_once(self):
        from test_team_external import ScriptedExternalDriver
        f, _, candidate = self.ready()
        gate = threading.Barrier(2)
        def worker():
            driver = ScriptedExternalDriver()
            rt = Runtime(f.rt.log.path, roles_path=f.roles, drivers={driver.name: driver})
            try:
                gate.wait(timeout=5)
                with rt.team.recovery.review_candidate_lease(f.task, candidate_seq=candidate.seq, candidate_hash=candidate.hash) as result:
                    return result[2].seq if result else None
            finally:
                rt.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            values = [future.result(timeout=15) for future in (pool.submit(worker), pool.submit(worker))]
        self.assertEqual(sum(value is not None for value in values), 1)
        started = next(e for e in f.events('team.run_started') if e.payload.get('message_seq') == candidate.seq)
        f.rt.log.emit(type='team.run_finished', task_id=f.task, actor=HOST, payload={
            'message_seq': candidate.seq, 'run_id': started.payload['run_id'],
            'role': started.payload['role'], 'status': 'interrupted'})
        driver = ScriptedExternalDriver()
        reopened = Runtime(f.rt.log.path, roles_path=f.roles, drivers={driver.name: driver})
        self.addCleanup(reopened.close)
        with reopened.team.recovery.review_candidate_lease(f.task, candidate_seq=candidate.seq, candidate_hash=candidate.hash) as claimed:
            self.assertIsNone(claimed)
        starts = [e for e in f.events('team.run_started') if e.payload.get('message_seq') == candidate.seq]
        self.assertEqual(len(starts), 1)
