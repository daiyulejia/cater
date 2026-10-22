"""Supplementary clock and real-stdio regressions; never real-model acceptance."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from cater.acp import AcpSession, AcpTimeout
from cater.external_agent import ExternalRunLimit, QoderAcpDriver, _ProgressBudget
from cater.tool_protocol import ToolCall
import test_team_external as external


class ProgressBudgetTests(unittest.TestCase):
    def test_delayed_check_uses_earliest_deadline_and_total_wins_ties(self):
        for receipts, expected in (((1, 2.5, 4), 'total'), ((1,), 'idle'), ((1, 2, 3), 'total')):
            with self.subTest(receipts=receipts), patch('cater.external_agent.time.monotonic', return_value=0) as clock:
                budget = _ProgressBudget(2, 5)
                for index, when in enumerate(receipts):
                    clock.return_value = when
                    self.assertTrue(budget.settled(ToolCall(str(index), 'inspect', {'page': index}), {'status': 'ok'}))
                clock.return_value = 6.5
                self.assertEqual(budget.expired().limit, expected)

    def test_distinct_successful_receipts_renew_idle_but_never_total(self):
        with patch('cater.external_agent.time.monotonic', return_value=0) as clock:
            budget = _ProgressBudget(2, 5)
            for index, when in enumerate((1, 2.5, 4)):
                clock.return_value = when
                budget.check()
                self.assertTrue(budget.settled(ToolCall(str(index), 'read', {'page': index}), {'status': 'ok'}))
            clock.return_value = 5
            with self.assertRaises(ExternalRunLimit) as caught:
                budget.check()
            self.assertEqual(caught.exception.limit, 'total')
            self.assertEqual(budget.started, 0)

    def test_failed_incomplete_and_repeated_calls_do_not_renew_idle(self):
        with patch('cater.external_agent.time.monotonic', return_value=0) as clock:
            budget = _ProgressBudget(2, 8)
            call = ToolCall('one', 'read', {'a': 1, 'b': 2})
            clock.return_value = 1
            self.assertTrue(budget.settled(call, {'status': 'ok'}))
            clock.return_value = 2
            self.assertFalse(budget.settled(ToolCall('another-id', 'read', {'b': 2, 'a': 1}), {'status': 'ok'}))
            for result in ({'status': 'error'}, {'status': 'ok', 'complete': False},
                           {'status': 'ok', 'outcome': 'timeout'}, {'status': 'ok', 'outcome': 'effects_unknown'}, 'ok'):
                self.assertFalse(budget.settled(ToolCall('bad', 'other', {}), result))
            clock.return_value = 3
            with self.assertRaises(ExternalRunLimit) as caught:
                budget.check()
            self.assertEqual(caught.exception.limit, 'idle')

    def test_prompt_override_validation_never_starts_a_process_or_changes_session_default(self):
        with tempfile.TemporaryDirectory() as root, patch('subprocess.Popen') as process:
            session = AcpSession(['never-launch'], cwd=root, timeout=2)
            for invalid in (False, 0, -1, '3', float('inf'), float('nan')):
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    session.prompt('test', timeout=invalid)
            self.assertEqual(session.timeout, 2)
            process.assert_not_called()
            session.close()

    def test_late_host_receipt_remains_recorded_but_cannot_revive_expired_idle_or_total(self):
        for total, end, expected in ((10, 3, 'idle'), (5, 100, 'idle')):
            with self.subTest(total=total), patch('cater.external_agent.time.monotonic', return_value=0) as clock:
                driver = QoderAcpDriver('test', 'never-start', 'ExactModel', timeout=2, max_run_seconds=total)
                effects = []

                def execute(call):
                    clock.return_value = end
                    effects.append('committed host effect and receipt')
                    return {'status': 'ok', 'complete': True}

                run = driver.agent_run(cwd='.', tools=[], execute=execute,
                    check_current=lambda: None, on_update=None)
                run._budget = _ProgressBudget(2, total)
                clock.return_value = 1
                receipt = run._call(ToolCall('one', 'inspect', {}))
                self.assertEqual(receipt, {'status': 'ok', 'complete': True})
                self.assertEqual(effects, ['committed host effect and receipt'])
                self.assertEqual(run._budget.progress_at, 0)
                with self.assertRaises(ExternalRunLimit) as caught:
                    run._call(ToolCall('two', 'inspect', {'page': 2}))
                self.assertEqual(caught.exception.limit, expected)
                self.assertEqual(len(effects), 1)


class ProgressBudgetTransportTests(unittest.TestCase):
    def exercise(self, mode, *, total):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        driver = QoderAcpDriver('offline', 'never-real-qoder', 'ExactModel', timeout=2, max_run_seconds=total)
        sessions, calls = [], []
        peer = Path(__file__).parent / 'fixtures/external_budget_peer.py'

        class Session(AcpSession):
            def __init__(self, command, **kwargs):
                super().__init__([sys.executable, '-u', str(peer.resolve()), mode, str(root)], **kwargs)
                sessions.append(self)

        def execute(call):
            calls.append(call)
            return {'status': 'ok', 'page': call.arguments['page']}

        with patch('cater.external_agent.AcpSession', Session):
            run = driver.agent_run(cwd=root, tools=[{'name': 'inspect', 'description': 'Read one page',
                'input_schema': {'type': 'object'}}], execute=execute, check_current=lambda: None, on_update=None)
            try:
                result = run.run('Offline actual stdio, not a real model')
            except BaseException as exc:
                result = exc
        self.assertTrue(run.state.cleanup_complete)
        self.assertFalse((root / 'peer-error.txt').exists(),
                         (root / 'peer-error.txt').read_text(encoding='utf-8') if (root / 'peer-error.txt').exists() else '')
        self.assertTrue(all(s._proc.poll() is not None for s in sessions))
        self.assertEqual(sessions[0].timeout, 2)
        self.assertRaises(RuntimeError, run.run, 'never replay')
        return result, run, calls

    def test_real_stdio_progress_outlives_idle_window_within_fixed_total(self):
        result, run, calls = self.exercise('progress', total=7)
        self.assertEqual(result.stop_reason, 'end_turn')
        self.assertEqual(len(calls), 3)
        self.assertEqual(dict(run.state.observations)['progress_receipts'], 3)
        self.assertGreater(dict(run.state.observations)['response_ms'] - dict(run.state.observations)['prompt_attempt_ms'], 2000)

    def test_public_text_keeps_transport_alive_but_cannot_extend_total_or_start_tools(self):
        result, run, calls = self.exercise('text', total=4)
        self.assertIsInstance(result, ExternalRunLimit)
        self.assertEqual(result.limit, 'total')
        self.assertEqual(calls, [])
        self.assertGreater(dict(run.state.observations)['public_updates'], 0)
        self.assertIn('last_activity_ms', dict(run.state.observations))
        self.assertNotIn('progress_receipts', dict(run.state.observations))

    def test_real_stream_finishes_beyond_idle_without_any_tool_calls(self):
        result, run, calls = self.exercise('stream', total=7)
        self.assertEqual(result.stop_reason, 'end_turn')
        self.assertEqual(calls, [])
        self.assertIn('last_activity_ms', dict(run.state.observations))
        self.assertNotIn('progress_receipts', dict(run.state.observations))

    def test_silent_client_still_stops_at_idle(self):
        result, run, calls = self.exercise('silent', total=7)
        self.assertIsInstance(result, ExternalRunLimit)
        self.assertEqual(result.limit, 'idle')
        self.assertEqual(calls, [])

    def test_private_stream_counts_only_as_liveness_and_is_never_exposed(self):
        result, run, calls = self.exercise('thinking', total=7)
        self.assertEqual(result.stop_reason, 'end_turn')
        self.assertEqual(result.text, '')
        self.assertEqual(calls, [])
        facts = dict(run.state.observations)
        self.assertIn('last_activity_ms', facts)
        self.assertNotIn('public_updates', facts)
        self.assertNotIn('progress_receipts', facts)
        self.assertNotIn('PRIVATE_ACTUAL_STREAM', repr(run.state))

    def test_repeated_read_cannot_keep_idle_alive(self):
        result, run, calls = self.exercise('repeat', total=7)
        self.assertIsInstance(result, ExternalRunLimit)
        self.assertEqual(result.limit, 'idle')
        self.assertGreater(len(calls), 1)
        self.assertEqual(dict(run.state.observations)['progress_receipts'], 1)

    def test_continuous_distinct_tools_still_stop_at_total(self):
        result, run, calls = self.exercise('continuous', total=4)
        self.assertIsInstance(result, ExternalRunLimit)
        self.assertEqual(result.limit, 'total')
        self.assertGreater(len(calls), 2)
        self.assertEqual(run.state.phase, 'failed')


class ExternalBudgetHealthTests(unittest.TestCase):
    def setUp(self):
        self.f = external.TeamExternalTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def test_total_budget_preserves_failure_and_prior_health_without_opening_shared_circuit(self):
        f = self.f
        f.rt._driver_health.record_failure(f.driver.name)

        def budget(run):
            raise ExternalRunLimit('total')

        f.driver.scripts = {f.manager: [budget]}
        f.receive('Explicit local execution budget')
        f.drain()
        finish, = f.events('team.run_finished')
        self.assertEqual(finish.payload['status'], 'failed')
        self.assertEqual(finish.payload['external_failure_code'], 'execution_budget')
        self.assertFalse(f.rt._is_circuit_open(f.driver.name))
        self.assertEqual(f.events('team.objective_completed'), [])
        f.rt._driver_health.record_failure(f.driver.name)
        self.assertTrue(f.rt._is_circuit_open(f.driver.name), 'The original failure was not cleared')

    def test_idle_and_legacy_timeouts_still_count_as_channel_failures(self):
        for error, code in ((ExternalRunLimit('idle'), 'idle_timeout'), (AcpTimeout('legacy'), 'deadline_exceeded')):
            f = external.TeamExternalTests()
            f.setUp()
            try:
                f.rt._driver_health.record_failure(f.driver.name)

                def fail(run):
                    raise error

                f.driver.scripts = {f.manager: [fail]}
                f.receive('No verified useful progress')
                f.drain()
                self.assertTrue(f.rt._is_circuit_open(f.driver.name))
                self.assertEqual(f.events('team.run_finished')[-1].payload['external_failure_code'], code)
            finally:
                f.doCleanups()

    def test_budget_failure_retains_committed_artifact_and_unknown_remote_fact_without_replay(self):
        f = self.f

        def budget(run):
            f.publish(run, 'Committed once before local budget ended\n')
            raise ExternalRunLimit('total')

        f.driver.scripts = {f.manager: [budget]}
        f.receive('Do not discard completed tool effects')
        f.drain()
        self.assertEqual(len(f.driver.runs), 1)
        artifact, = f.rt.task(f.task).deliverables
        self.assertEqual(Path(artifact['local_path']).read_text(encoding='utf-8'),
                         'Committed once before local budget ended\n')
        self.assertEqual(len(f.results('publish_artifact')), 1)
        receipt, = f.events('team.external_run_finished')
        self.assertEqual(receipt.payload['outcome'], 'unknown')
        self.assertEqual(f.events('team.objective_completed'), [])
        self.assertEqual(f.events('team.review_recorded'), [])

    def test_budget_cannot_release_unconfirmed_client_cleanup(self):
        f = self.f

        def budget(run):
            run.cleanup_complete = False
            raise ExternalRunLimit('total')

        f.driver.scripts = {f.manager: [budget]}
        f.receive('Local stopping is unconfirmed')
        f.drain()
        self.assertEqual(len(f.rt.team.external_runs.pending(f.task)), 1)
        self.assertEqual(f.events('team.run_finished')[-1].payload['status'], 'interrupted')
        self.assertEqual(f.events('team.objective_completed'), [])

    def test_budget_and_idle_configuration_changes_invalidate_driver_fingerprint(self):
        from cater.team_external import driver_fingerprint

        driver = QoderAcpDriver('local', 'never-launch', 'ExactModel', timeout=120, max_run_seconds=600)
        initial = driver_fingerprint(driver)
        driver.max_run_seconds = 500
        self.assertNotEqual(driver_fingerprint(driver), initial)
        driver.max_run_seconds = 600
        driver.timeout = 100
        self.assertNotEqual(driver_fingerprint(driver), initial)
