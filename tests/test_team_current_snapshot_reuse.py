"""Transactional scope-poll reuse and real two-connection admission races."""
import time
import unittest
from unittest.mock import patch

from cater.events import Actor
from cater.runtime import Runtime
from cater.work import WorkConflict
import test_ordinary_action_direction as action
import test_team_scope_polling as scope


class CurrentSnapshotReuseTests(unittest.TestCase):
    def scope_fixture(self):
        f = scope.TeamNativeScopePollingTests(methodName='runTest')
        f.setUp()
        self.addCleanup(f.doCleanups)
        return f

    def action_fixture(self):
        helper = action.OrdinaryActionDirectionTests(methodName='runTest')
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        return helper, helper.fixture()

    def test_long_history_transactional_poll_reads_task_events_once(self):
        f = self.scope_fixture()
        f.objective()
        _, role, run = f.claim(worker=True)
        f.rt.log.emit_many([{'task_id': f.task, 'actor': Actor.system('driver_gateway'),
                            'type': 'driver.budget_exhausted', 'payload': {}} for _ in range(5000)])
        with f.rt.log.atomic():
            with patch.object(f.rt.log, 'read', wraps=f.rt.log.read) as reads:
                started = time.perf_counter()
                f.team._current(f.task, run, role, run.payload['revision'])
                elapsed = time.perf_counter() - started
            self.assertEqual(reads.call_count, 1)
            self.assertTrue(f.rt.log.db.in_transaction)
        self.assertGreater(elapsed, 0)  # Timing is reported by an optional benchmark, not a flaky threshold.

    def test_standalone_poll_rechecks_customer_message_after_initial_read(self):
        helper, f = self.action_fixture()
        f.rt.team.receive(f.task, 'Original')
        _, role, run = f.rt.team._claim(f.task)
        original = f.rt.log.read
        injected = []
        def read(*args, **kwargs):
            result = original(*args, **kwargs)
            if not injected:
                with Runtime(f.rt.log.path, roles_path=f.roles, drivers={'team_test': f.driver},
                             close_drivers_on_close=False) as other:
                    injected.append(other.team.receive(f.task, 'New customer input').seq)
            return result
        with patch.object(f.rt.log, 'read', side_effect=read), self.assertRaises(WorkConflict):
            f.rt.team._current(f.task, run, role, run.payload['revision'])
        self.assertTrue(injected)

    def test_local_commit_rechecks_direction_and_customer_after_tool_start(self):
        for change in ('direction', 'same_role_chat', 'other_role_chat'):
            with self.subTest(change=change):
                helper, f = self.action_fixture()
                f.rt.team.receive(f.task, 'Original')
                claimed = f.rt.team._claim(f.task)
                def inject():
                    with Runtime(f.rt.log.path, roles_path=f.roles, drivers={'team_test': f.driver},
                                 close_drivers_on_close=False) as other:
                        if change == 'direction':
                            other.command(f.task, 'Change direction', goal='New objective')
                        elif change == 'same_role_chat':
                            other.team.receive(f.task, 'New customer input')
                        else:
                            worker = other.team._role(f.worker)
                            other.team.receive(f.task, f'@{worker.name} Parallel ordinary chat')
                start, result = helper.dispatch(f, claimed, 'delegate',
                    {'recipient': f.worker, 'instruction': 'Old work'}, on_started=inject)
                jobs = [e for e in f.events('team.message_received')
                        if e.actor.kind == 'agent' and e.payload.get('kind') == 'assignment']
                receipts = [e for e in f.events('team.tool_finished') if e.payload.get('started_seq') == start.seq]
                self.assertEqual(len(receipts), 1)
                if change == 'other_role_chat':
                    self.assertEqual(result['status'], 'ok')
                    self.assertEqual(len(jobs), 1)
                else:
                    self.assertNotEqual(result['status'], 'ok')
                    self.assertEqual(jobs, [])
                self.assertTrue(f.rt.verify_chain().ok)


if __name__ == '__main__':
    unittest.main()
