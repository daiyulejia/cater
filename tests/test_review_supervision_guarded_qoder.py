"""Offline regressions for bounded Qoder subclasses."""
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from cater.acp import AcpSession
from cater.external_agent import QoderAcpDriver
from cater.review_supervision_candidate import create
from support import guarded_qoder
import test_review_supervision_candidate as fixtures


QA = Path(__file__).resolve().parents[1] / '.cater' / 'qa'
LOCAL_WRAPPERS = all((QA / name).is_file() for name in ('qoder_team_trial.py', 'qoder_engineering_trial.py'))


class GuardedQoderSupervisionTests(unittest.TestCase):
    driver_type = guarded_qoder.GuardedQoder
    allowed = guarded_qoder.ALLOWED

    def fixture(self):
        helper = fixtures.ReviewSupervisionCandidateTests(methodName='runTest')
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        return helper

    def driver(self, f, limit):
        driver = self.driver_type('never-real-qoder', 'offline', max_starts=limit, seconds=60, timeout=10)
        driver.name = f.driver.name
        self.addCleanup(driver.close)
        f.rt.drivers[driver.name] = driver
        return driver

    def session(self, f, modes):
        sessions = []
        peer = Path(__file__).parent / 'fixtures' / 'review_supervision_acp_peer.py'
        class Session(AcpSession):
            def __init__(self, command, **kwargs):
                super().__init__([sys.executable, '-u', str(peer.resolve()), modes.pop(0), str(f.root)], **kwargs)
                sessions.append(self)
        return Session, sessions

    def pending_review(self, helper):
        f = helper.fixture('external')
        artifact, = helper.run_calls(f, helper.human(f), ('publish_artifact', {
            'filename': 'candidate.txt', 'content': 'content', 'summary': 'fixture', 'format': 'text'}))
        review, = helper.run_calls(f, helper.human(f), ('request_review', {
            'reviewer': f.reviewer, 'artifact_seqs': [artifact['artifact_seq']], 'instruction': 'Inspect'}))
        helper.run_calls(f, helper.message(f, review['message_seq']), ('record_review', {
            'review_seq': review['review_seq'], 'verdict': 'approved', 'feedback': 'Checked'}), statuses=('waiting',))
        return f

    def test_normal_drain_runs_guarded_supervisor_with_filtered_tools(self):
        helper = self.fixture()
        f = self.pending_review(helper)
        driver = self.driver(f, 2)
        modes = ['initial', 'finish']
        session, sessions = self.session(f, modes)
        offered = []
        original = QoderAcpDriver.agent_run
        def observe(instance, **kwargs):
            offered.append({item['name'] for item in kwargs['tools']})
            return original(instance, **kwargs)
        with patch('cater.external_agent.AcpSession', session), patch.object(QoderAcpDriver, 'agent_run', observe):
            f.rt.collaborate(f.task)
        self.assertEqual(modes, [], 'normal drain must invoke the guarded supervisor')
        self.assertEqual(driver.starts, 2)
        self.assertEqual(len(sessions), 2)
        self.assertTrue(all(s._proc.poll() is not None for s in sessions))
        self.assertEqual(len(offered), 2)
        self.assertTrue(all(names <= self.allowed for names in offered))
        self.assertIn('finish_work', offered[-1])
        self.assertNotIn('read_artifact', offered[-1])
        candidate, = [e for e in f.events('team.message_received') if e.payload.get('trigger') == 'review_return_candidate']
        self.assertEqual(len([e for e in f.events('team.run_started') if e.payload.get('message_seq') == candidate.seq]), 1)
        self.assertEqual(len(f.events('team.objective_completed')), 1)
        self.assertTrue(f.rt.verify_chain().ok)

    def test_claimed_candidate_runs_guarded_subclass(self):
        helper = self.fixture()
        f, original = helper.ready()
        candidate = create(f.rt.team, f.task, run_seq=original.seq, run_hash=original.hash)
        self.assertIsNotNone(candidate)
        driver = self.driver(f, 1)
        modes = ['finish']
        session, sessions = self.session(f, modes)
        with f.rt.team.recovery.review_candidate_lease(f.task, candidate_seq=candidate.seq, candidate_hash=candidate.hash) as claimed:
            self.assertIsNotNone(claimed)
            with patch('cater.external_agent.AcpSession', session):
                list(f.rt.team._run_message(f.task, *claimed))
        self.assertEqual(driver.starts, 1, 'claimed supervision must dynamically call the subclass')
        self.assertEqual(modes, [])
        self.assertEqual(len(sessions), 1)
        self.assertEqual(len(f.events('team.objective_completed')), 1)

    def test_shared_budget_stops_supervisor_without_an_extra_acp_start(self):
        helper = self.fixture()
        f = self.pending_review(helper)
        driver = self.driver(f, 1)
        modes = ['initial']
        session, sessions = self.session(f, modes)
        with patch('cater.external_agent.AcpSession', session):
            f.rt.collaborate(f.task)
            f.rt.collaborate(f.task)
        self.assertEqual(driver.starts, 1)
        self.assertEqual(len(sessions), 1)
        candidates = [e for e in f.events('team.message_received') if e.payload.get('trigger') == 'review_return_candidate']
        self.assertEqual(len(candidates), 1, 'budget exhaustion must leave a visible claimed supervision result')
        candidate = candidates[0]
        finished, = [e for e in f.events('team.run_finished') if e.payload.get('message_seq') == candidate.seq]
        self.assertIn(finished.payload['status'], {'failed', 'interrupted'})
        self.assertEqual(f.events('team.objective_completed'), [])
        self.assertTrue(f.rt.verify_chain().ok)


@unittest.skipUnless(LOCAL_WRAPPERS, 'local QA wrappers are not present in this checkout')
class LocalEngineeringQoderSupervisionTests(GuardedQoderSupervisionTests):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with patch.object(sys, 'path', [str(QA), *sys.path]):
            spec = importlib.util.spec_from_file_location('offline_engineering_trial', QA / 'qoder_engineering_trial.py')
            trial = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(trial)
        cls.driver_type = trial.GuardedEngineeringQoder
        cls.allowed = trial.ALLOWED
