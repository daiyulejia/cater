"""Explicit one-task supervision diagnostics, with no execution or replay."""
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from cater.acp import AcpSession
from cater.drivers import OpenAICompatibleDriver
from cater.events import Actor
from cater.external_agent import QoderAcpDriver
from cater.review_supervision_candidate import create
from cater.review_supervision_status import inspect_task
import test_review_supervision_intent as intent_fixture
import test_review_supervision_candidate as candidate_fixture


class ReviewSupervisionStatusTests(unittest.TestCase):
    def ready(self):
        helper = intent_fixture.SupervisionIntentTests(methodName='runTest')
        f = helper.run_case(OpenAICompatibleDriver, crash_after=True)
        self.addCleanup(helper.doCleanups)
        return f

    def test_real_intent_cli_is_read_only_then_tracks_claim_and_finish(self):
        f = self.ready()
        before = f.rt.log.read(task_id=f.task)
        cmd = [sys.executable, '-m', 'cater', '--db', str(f.rt.log.path),
               'review-supervision-status', f.task]
        result = subprocess.run(cmd, cwd=Path(__file__).resolve().parents[1],
                                capture_output=True, text=True, encoding='utf-8', timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        intent, = output['intents']
        self.assertEqual(intent['state'], 'awaiting_continuation')
        self.assertIsNone(intent['intent_hash'])
        self.assertIn('不会启动', output['note'])
        self.assertEqual(f.rt.log.read(task_id=f.task), before)
        original, = [e for e in f.events('team.run_started') if e.seq == f.events('team.review_supervision_intended')[0].payload['run_seq']]
        candidate = create(f.rt.team, f.task, run_seq=original.seq, run_hash=original.hash)
        self.assertIsNotNone(candidate)
        self.assertEqual(inspect_task(f.rt.log.path, f.task)['intents'][0]['state'], 'candidate_unclaimed')
        with f.rt.team.recovery.review_candidate_lease(f.task, candidate_seq=candidate.seq, candidate_hash=candidate.hash) as claim:
            self.assertIsNotNone(claim)
            self.assertEqual(inspect_task(f.rt.log.path, f.task)['intents'][0]['state'], 'run_started_no_finish_record')
            run = claim[2]
            f.rt.log.emit(type='team.run_finished', task_id=f.task, actor=Actor.system('team'),
                          payload={**{k: run.payload[k] for k in ('run_id', 'role', 'message_seq')}, 'status': 'interrupted'})
        projection = inspect_task(f.rt.log.path, f.task)['intents'][0]
        self.assertEqual((projection['state'], projection['run_status']), ('run_finished_recorded', 'interrupted'))
        self.assertEqual(inspect_task(f.rt.log.path, f.task), inspect_task(f.rt.log.path, f.task))
        self.assertTrue(f.rt.verify_chain().ok)

    def test_invalid_candidate_and_new_input_do_not_hide_intent(self):
        f = self.ready()
        intent, = f.events('team.review_supervision_intended')
        worker = f.rt.team._role(f.worker)
        f.rt.team.receive(f.task, f'@{worker.name} New information')
        f.rt.log.emit(type='team.message_received', task_id=f.task, actor=Actor.system('team'),
                      payload={'kind': 'recovery', 'trigger': 'review_return_candidate',
                               'original_run_seq': intent.payload['run_seq'], 'original_run_hash': 'wrong'})
        row, = inspect_task(f.rt.log.path, f.task)['intents']
        self.assertEqual(row['state'], 'record_unverified')
        self.assertTrue(row['later_customer_input'])

    def test_only_requested_task_and_missing_db_never_created(self):
        f = self.ready()
        other = f.rt.create_task('Independent task')
        self.assertEqual(inspect_task(f.rt.log.path, other)['intents'], [])
        with self.assertRaises(ValueError):
            inspect_task(f.rt.log.path, 'missing-task')
        missing = f.root / 'does-not-exist.db'
        with self.assertRaises(Exception):
            inspect_task(missing, f.task)
        self.assertFalse(missing.exists())

    def test_incomplete_intent_and_untrusted_hash_are_not_reported_as_valid(self):
        f = self.ready()
        intent, = f.events('team.review_supervision_intended')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'copy.db'
            with closing(sqlite3.connect(path)) as db:
                f.rt.log.db.backup(db)
                for (name,) in db.execute("select name from sqlite_master where type='trigger'").fetchall():
                    db.execute('DROP TRIGGER "' + name.replace('"', '""') + '"')
                original = intent.payload
                for field in ('scope', 'protocol', 'customer_inputs', 'evidence'):
                    with self.subTest(missing=field):
                        payload = {k: v for k, v in original.items() if k != field}
                        db.execute('update events set payload_json=? where seq=?', (json.dumps(payload), intent.seq))
                        db.commit()
                        row, = inspect_task(path, f.task)['intents']
                        self.assertEqual(row['state'], 'record_unverified')
                payload = {**original, 'evidence': {k: v for k, v in original['evidence'].items()
                                                    if k != 'review_request'}}
                db.execute('update events set payload_json=? where seq=?', (json.dumps(payload), intent.seq))
                db.commit()
                self.assertEqual(inspect_task(path, f.task)['intents'][0]['state'], 'record_unverified')
                db.execute('update events set payload_json=?, hash=? where seq=?',
                           (json.dumps(original), 'PRIVATE_SENTINEL', intent.seq))
                db.commit()
            cmd = [sys.executable, '-m', 'cater', '--db', str(path), 'review-supervision-status', f.task]
            result = subprocess.run(cmd, cwd=Path(__file__).resolve().parents[1],
                                    capture_output=True, text=True, encoding='utf-8', timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn('PRIVATE_SENTINEL', result.stdout + result.stderr)
            row, = json.loads(result.stdout)['intents']
            self.assertIsNone(row['intent_hash'])
            self.assertEqual(row['state'], 'record_unverified')
            with closing(sqlite3.connect(path)) as db:
                db.execute('update events set hash=? where seq=?', ('a' * 64, intent.seq))
                db.commit()
            self.assertIsNone(inspect_task(path, f.task)['intents'][0]['intent_hash'])

    def test_actual_offline_qoder_intent_is_visible(self):
        helper = candidate_fixture.ReviewSupervisionCandidateTests(methodName='runTest')
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        f = helper.fixture('external')
        artifact, = helper.run_calls(f, helper.human(f), ('publish_artifact', {
            'filename': 'review.txt', 'content': 'fixture', 'summary': 'fixture', 'format': 'text'}))
        review, = helper.run_calls(f, helper.human(f), ('request_review', {
            'reviewer': f.reviewer, 'artifact_seqs': [artifact['artifact_seq']], 'instruction': 'Inspect'}))
        helper.run_calls(f, helper.message(f, review['message_seq']), ('record_review', {
            'review_seq': review['review_seq'], 'verdict': 'approved', 'feedback': 'Checked'}), statuses=('waiting',))
        driver = QoderAcpDriver(f.driver.name, 'never-real-qoder', 'offline', timeout=10)
        self.addCleanup(driver.close)
        f.rt.drivers[driver.name] = driver
        peer = Path(__file__).parent / 'fixtures' / 'review_supervision_acp_peer.py'
        class Session(AcpSession):
            def __init__(self, command, **kwargs):
                super().__init__([sys.executable, '-u', str(peer.resolve()), 'initial', str(f.root)], **kwargs)
        with patch('cater.external_agent.AcpSession', Session), patch.object(
                f.rt.team, '_supervise_finished_review', side_effect=intent_fixture.Crash()):
            with self.assertRaises(intent_fixture.Crash):
                f.rt.collaborate(f.task)
        before = f.rt.log.read(task_id=f.task)
        row, = inspect_task(f.rt.log.path, f.task)['intents']
        self.assertEqual(row['state'], 'awaiting_continuation')
        self.assertEqual(f.rt.log.read(task_id=f.task), before)
