"""Windows ACP Job cleanup observations using real offline child processes."""
import ctypes
import os
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

import test_external_agent as fixture
import test_external_runs as receipts
import test_review_supervision_guarded_qoder as guarded

if os.name == 'nt':
    from cater._windows_job import WindowsJob
    from cater.processes import ProcessScope, worker_python
from cater.acp import AcpSession, AcpUnavailable
from cater.external_agent import ExternalYield
from cater.external_runs import ExternalRuns


@unittest.skipUnless(os.name == 'nt', 'requires Windows Jobs')
class WindowsAcpJobCleanupTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(guarded.GuardedQoderSupervisionTests, 'session', self.guarded_session))

    @staticmethod
    def guarded_session(owner, f, modes):
        sessions = []
        peer = Path(__file__).parent / 'fixtures' / 'review_supervision_acp_peer.py'
        original = AcpSession.__init__
        def offline_init(session, command, **kwargs):
            original(session, [sys.executable, '-B', '-u', str(peer.resolve()), modes.pop(0), str(f.root)], **kwargs)
            sessions.append(session)
        owner.enterContext(patch.object(AcpSession, '__init__', offline_init))
        return AcpSession, sessions

    def fixture(self):
        case = fixture.ExternalAgentTests('test_last_observations_advance_and_failed_tools_record_end')
        case.setUp()
        self.addCleanup(lambda: self.assertTrue(case.doCleanups()))
        original = AcpSession.__init__
        def offline_init(session, command, **kwargs):
            case.assertEqual(command, case.driver.command)
            original(session, [sys.executable, '-B', '-u', str(fixture.PEER.resolve()),
                               case.mode, str(case.root)], **kwargs)
            case.sessions.append(session)
        case.enterContext(patch.object(AcpSession, '__init__', offline_init))
        case.enterContext(patch('cater.external_agent.AcpSession', AcpSession))
        return case

    def test_late_job_member_is_not_in_snapshot_wait_set(self):
        # The worker cannot spawn the child until the snapshot member has been
        # opened; the hook waits for a real child before TerminateJobObject.
        code = ("import subprocess,sys,time\n"
                "if sys.stdin.readline().strip() == 'spawn':\n"
                " p=subprocess.Popen([sys.executable,'-B','-c','import time; time.sleep(60)'])\n"
                " print(p.pid,flush=True)\n"
                " p.wait()\n")
        process = subprocess.Popen([worker_python(), '-B', '-c', code], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        job = None
        child_handle = None
        try:
            job = WindowsJob(process.pid)
            k = job._kernel
            k.IsProcessInJob.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
            k.IsProcessInJob.restype = ctypes.c_int
            k.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            k.WaitForSingleObject.restype = ctypes.c_uint32
            k.GetProcessId.argtypes = [ctypes.c_void_p]
            k.GetProcessId.restype = ctypes.c_uint32
            member_check, real_wait = k.IsProcessInJob, k.WaitForSingleObject
            late_pid, waited = [], []

            def spawn_after_snapshot(handle, job_handle, belongs):
                nonlocal child_handle
                result = member_check(handle, job_handle, belongs)
                self.assertTrue(result)
                self.assertEqual(k.GetProcessId(handle), process.pid)
                process.stdin.write('spawn\n')
                process.stdin.flush()
                line = []
                reader = threading.Thread(target=lambda: line.append(process.stdout.readline()), daemon=True)
                reader.start()
                reader.join(5)
                self.assertFalse(reader.is_alive(), 'child did not report before termination')
                pid = int(line[0].strip())
                late_pid.append(pid)
                child_handle = k.OpenProcess(0x100000 | 0x1000, False, pid)
                self.assertTrue(child_handle)
                member = ctypes.c_int()
                self.assertTrue(member_check(child_handle, job_handle, ctypes.byref(member)))
                self.assertTrue(member.value)
                self.assertEqual(real_wait(child_handle, 0), 258)
                return result

            def record_wait(handle, timeout):
                waited.append(k.GetProcessId(handle))
                return real_wait(handle, timeout)

            with patch.object(k, 'IsProcessInJob', side_effect=spawn_after_snapshot), \
                 patch.object(k, 'WaitForSingleObject', side_effect=record_wait):
                observation = job.close_observed(timeout=10)
            self.assertEqual(len(late_pid), 1)
            self.assertNotEqual(late_pid[0], process.pid)
            self.assertEqual(observation['status'], 'snapshot_observed')
            self.assertEqual(observation['observed_members'], 1)
            self.assertEqual(waited, [process.pid])
            self.assertNotIn(late_pid[0], waited)
            # This independent later wait is cleanup evidence for the test,
            # not evidence that close_observed waited for the late member.
            self.assertEqual(real_wait(child_handle, 5000), 0)
        finally:
            if job is not None and job._handle is not None:
                job.close()
            elif job is None and process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            process.stdin.close()
            process.stdout.close()
            if child_handle:
                k.CloseHandle(child_handle)

    def test_offline_acp_peer_relay_handles_signal_after_close(self):
        case = self.fixture()
        witnesses = []
        count = []
        original = WindowsJob.close_observed
        def observe(job, *args, **kwargs):
            class Pids(ctypes.Structure):
                _fields_ = [('assigned', ctypes.c_uint32), ('count', ctypes.c_uint32),
                            ('ids', ctypes.c_size_t * 32)]
            values = Pids()
            k = job._kernel
            # close_observed configures these signatures, but capture first.
            k.QueryInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p,
                                                    ctypes.c_uint32, ctypes.c_void_p]
            k.QueryInformationJobObject.restype = ctypes.c_int
            self.assertTrue(k.QueryInformationJobObject(job._handle, 3, ctypes.byref(values), ctypes.sizeof(values), None))
            self.assertEqual(values.count, values.assigned)
            count.append(values.count)
            for pid in values.ids[:values.count]:
                handle = k.OpenProcess(0x100000 | 0x1000, False, pid)
                self.assertTrue(handle)
                witnesses.append((k, handle))
            return original(job, *args, **kwargs)
        try:
            run = case.new_run()
            with patch.object(WindowsJob, 'close_observed', observe):
                run.run('offline')
            self.assertTrue(run.state.cleanup_complete)
            info = dict(run.state.job_cleanup_observation)
            self.assertEqual(info['version'], 1)
            self.assertEqual(info['status'], 'snapshot_observed')
            self.assertGreaterEqual(count[0], 3)  # Worker, ACP peer, MCP relay.
            self.assertEqual(info['observed_members'], count[0])
            for k, handle in witnesses:
                self.assertEqual(k.WaitForSingleObject(handle, 5000), 0)
        finally:
            for k, handle in witnesses:
                k.CloseHandle(handle)

    def test_enumeration_failure_preserves_control_exception_and_unconfirmed_state(self):
        case = self.fixture()
        signal = ExternalYield('synthetic control')
        original = WindowsJob.close_observed
        def fail_enumeration(job, *args, **kwargs):
            with patch.object(job._kernel, 'QueryInformationJobObject', return_value=0), \
                 patch('cater._windows_job.ctypes.get_last_error', return_value=5):
                return original(job, *args, **kwargs)
        run = case.new_run(execute=lambda call: (_ for _ in ()).throw(signal))
        with patch.object(WindowsJob, 'close_observed', fail_enumeration):
            with self.assertRaises(ExternalYield) as raised:
                run.run('offline')
        self.assertIs(raised.exception, signal)
        self.assertFalse(run.state.cleanup_complete)
        self.assertTrue(any(isinstance(error, AcpUnavailable) for error in run.state.cleanup_failures))
        info = dict(run.state.job_cleanup_observation)
        self.assertEqual(info['status'], 'unconfirmed')
        self.assertTrue(info['termination_requested'])
        self.assertTrue(info['job_closed'])
        self.assertNotIn('synthetic control', repr(info))
        self.assertIsNotNone(case.sessions[-1]._proc.poll())

    def test_real_acp_wait_failure_is_unconfirmed_and_not_replayed(self):
        case = self.fixture()
        original = WindowsJob.close_observed
        def fail_wait(job, *args, **kwargs):
            with patch.object(job._kernel, 'WaitForSingleObject', return_value=258):
                return original(job, *args, **kwargs)
        run = case.new_run()
        with patch.object(WindowsJob, 'close_observed', fail_wait):
            with self.assertRaises(AcpUnavailable):
                run.run('offline')
        self.assertFalse(run.state.cleanup_complete)
        info = dict(run.state.job_cleanup_observation)
        self.assertEqual((info['snapshot'], info['wait'], info['status']),
                         ('captured', 'unconfirmed', 'unconfirmed'))
        self.assertTrue(info['termination_requested'])
        self.assertTrue(info['job_closed'])
        with self.assertRaises(RuntimeError):
            run.run('must not replay')

    def test_untrusted_scope_closes_but_cannot_claim_job_observation(self):
        for control in (False, True):
            with self.subTest(control=control):
                case = self.fixture()
                signal = ExternalYield('synthetic control')
                closed = []
                class Wrapper:
                    kind = 'windows_job'
                    def __init__(self, process):
                        self.inner = ProcessScope(process)
                    @property
                    def job(self):
                        raise AssertionError('untrusted job property must not run')
                    def close(self):
                        self.inner.close()
                        closed.append(True)
                execute = (lambda call: (_ for _ in ()).throw(signal)) if control else case.execute
                run = case.new_run(execute=execute)
                with patch('cater.acp.ProcessScope', Wrapper):
                    if control:
                        with self.assertRaises(ExternalYield) as caught:
                            run.run('offline')
                        self.assertIs(caught.exception, signal)
                    else:
                        with self.assertRaises(AcpUnavailable):
                            run.run('offline')
                self.assertTrue(closed)
                self.assertFalse(run.state.cleanup_complete)
                self.assertEqual(run.state.job_cleanup_observation, ())
                self.assertIsNotNone(case.sessions[-1]._proc.poll())
                self.assertTrue(case.doCleanups())

    def test_job_instance_cannot_override_host_cleanup_method(self):
        case = self.fixture()
        original = WindowsJob.__init__
        called = []
        def initialize(job, pid):
            original(job, pid)
            job.close_observed = lambda: called.append('forged')
        run = case.new_run()
        with patch.object(WindowsJob, '__init__', initialize):
            run.run('offline')
        self.assertEqual(called, [])
        self.assertTrue(run.state.cleanup_complete)
        self.assertEqual(dict(run.state.job_cleanup_observation)['status'], 'snapshot_observed')

    def test_real_guarded_qoder_path_persists_observations(self):
        case = guarded.GuardedQoderSupervisionTests('test_normal_drain_runs_guarded_supervisor_with_filtered_tools')
        result = unittest.TestResult()
        finished = []
        original = ExternalRuns.finish
        def capture(runs, *args, **kwargs):
            event = original(runs, *args, **kwargs)
            finished.append(event)
            return event
        with patch.object(ExternalRuns, 'finish', capture):
            case.run(result)
        self.assertTrue(result.wasSuccessful(), result.errors + result.failures)
        observed = [event for event in finished if 'job_cleanup_observation' in event.payload]
        self.assertEqual(len(observed), 2)
        for event in observed:
            info = event.payload['job_cleanup_observation']
            self.assertEqual(info['version'], 1)
            self.assertEqual(info['status'], 'snapshot_observed')
            self.assertNotIn('pid', repr(info).lower())

    def test_real_team_failure_stays_pending_and_does_not_start_supervisor(self):
        owner = guarded.GuardedQoderSupervisionTests('test_normal_drain_runs_guarded_supervisor_with_filtered_tools')
        owner.setUp()
        try:
            helper = owner.fixture()
            team_fixture = owner.pending_review(helper)
            driver = owner.driver(team_fixture, 2)
            modes = ['initial', 'finish']
            session, _ = owner.session(team_fixture, modes)
            original = WindowsJob.close_observed
            def fail_enumeration(job, *args, **kwargs):
                with patch.object(job._kernel, 'QueryInformationJobObject', return_value=0), \
                     patch('cater._windows_job.ctypes.get_last_error', return_value=5):
                    return original(job, *args, **kwargs)
            with patch('cater.external_agent.AcpSession', session), \
                 patch.object(WindowsJob, 'close_observed', fail_enumeration):
                team_fixture.rt.collaborate(team_fixture.task)
            self.assertEqual(driver.starts, 1)
            self.assertEqual(modes, ['finish'])
            finished = [e for e in team_fixture.events('team.external_run_finished')
                        if 'job_cleanup_observation' in e.payload]
            self.assertEqual(len(finished), 1)
            self.assertFalse(finished[0].payload['cleanup_complete'])
            self.assertEqual(finished[0].payload['job_cleanup_observation']['status'], 'unconfirmed')
            self.assertTrue(team_fixture.rt.team.external_runs.pending(team_fixture.task))
        finally:
            self.assertTrue(owner.doCleanups())


class ObservationReceiptTests(unittest.TestCase):
    def test_snapshot_observation_never_resolves_an_unclean_run(self):
        case = receipts.ExternalRunsTests('test_start_committed_public_provenance_and_restart_projection')
        case.setUp()
        try:
            start = case.begin()
            info = {'version': 1, 'termination_requested': True, 'snapshot': 'captured',
                    'observed_members': 3, 'wait': 'observed_exited', 'job_closed': True,
                    'status': 'snapshot_observed'}
            end = case.finish(start, cleanup_complete=False, job_cleanup_observation=info)
            self.assertEqual(end.payload['job_cleanup_observation'], info)
            self.assertEqual(len(case.runs.pending(case.task)), 1)
            case.log.close()
            from cater.events import EventLog
            case.log = case.rt.log = EventLog(case.path)
            self.assertEqual(len(case.runs.pending(case.task)), 1)
            self.assertFalse(case.runs.pending(case.task)[0]['replay_authorized'])
        finally:
            self.assertTrue(case.doCleanups())

    def test_invalid_or_unconfirmed_observation_cannot_settle_clean(self):
        case = receipts.ExternalRunsTests('test_start_committed_public_provenance_and_restart_projection')
        case.setUp()
        try:
            start = case.begin()
            info = {'version': 1, 'termination_requested': True, 'snapshot': 'unconfirmed',
                    'observed_members': 0, 'wait': 'unconfirmed', 'job_closed': True,
                    'status': 'unconfirmed'}
            with self.assertRaises(ValueError):
                case.finish(start, cleanup_complete=True, job_cleanup_observation=info)
            forged = {**case.runs._reference(start), 'phase': 'finished', 'prompt_attempted': True,
                      'cleanup_complete': True, 'result': 'returned', 'outcome': 'returned',
                      'job_cleanup_observation': info}
            case.emit('team.external_run_finished', forged)
            self.assertEqual(len(case.runs.pending(case.task)), 1)
        finally:
            self.assertTrue(case.doCleanups())


if __name__ == '__main__':
    unittest.main()
