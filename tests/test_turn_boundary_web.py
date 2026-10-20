"""Persisted bounded supervision integrates with the existing activity owner."""
from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from cater import team
from cater.activity_owner import activity_owner
from cater.events import Actor
from cater.web import ActivityRunner
import test_simple_team as turns
import test_team_web as fixture


class TurnBoundaryWebTests(unittest.TestCase):
    setUp = fixture.TeamWebTests.setUp
    factory = fixture.TeamWebTests.factory

    def stage(self):
        with self.factory() as runtime:
            task = runtime.create_task('Disposable bounded root supervision')
            runtime.team.enable(task)
            source = runtime.team.receive(task, 'Inspect saved files before arranging work', request_id='web-boundary-source')
            message, role, start = runtime.team._claim(task)
            self.assertEqual(source.seq, message.seq)
            self.driver.turns = [turns.turn('', ('read_messages', {})) for _ in range(2)]
            with patch.object(team, 'MAX_ACTOR_TURNS', 2):
                list(runtime.team._run_message(task, message, role, start))
            notice, = [e for e in runtime.team._messages(task) if e.payload.get('trigger') == 'turn_boundary']
            self.assertTrue(runtime.team.recovery.is_message(notice))
            self.assertEqual(len(self.driver.calls), 2)
            return task, source, notice

    def test_startup_dispatches_same_boundary_once_with_fresh_native_input(self):
        task, source, notice = self.stage()
        self.runner.recover_interrupted()
        first = self.runner.status(task)
        self.assertEqual(first['state'], 'queued')
        self.assertEqual(first['team_message_seq'], notice.seq)
        self.runner.recover_interrupted()
        self.assertEqual(self.start.call_count, 1)
        self.assertEqual(len(self.driver.calls), 2)
        self.runner._run(task, first['id'], 'run')
        self.assertEqual(self.runner.status(task)['state'], 'completed')
        self.assertEqual(len(self.driver.calls), 3)
        self.assertEqual(self.driver.calls[-1]['history'], [])
        context = json.loads(self.driver.calls[-1]['prompt'].rsplit('\n', 1)[1])
        self.assertEqual(context['message']['seq'], notice.seq)
        self.assertEqual(context['message']['sender_kind'], 'system')
        self.runner.recover_interrupted()
        self.assertEqual(self.start.call_count, 1)
        with self.factory() as runtime:
            events = runtime.log.read(task_id=task)
            self.assertEqual([e.seq for e in events if e.type == 'team.message_received' and e.actor == Actor.human()], [source.seq])
            self.assertEqual(len([e for e in events if e.type == 'activity.queued']), 1)
            self.assertEqual(runtime.team.view(task)['status'], 'running')
            self.assertTrue(runtime.verify_chain().ok)

    def test_restart_between_queue_and_claim_restores_same_activity(self):
        task, _, notice = self.stage()
        self.runner.recover_interrupted()
        first = self.runner.status(task)
        replacement = ActivityRunner(self.factory)
        with patch.object(replacement, '_start') as start:
            replacement.recover_interrupted()
            restored = replacement.status(task)
            self.assertEqual(restored['id'], first['id'])
            self.assertEqual(restored['team_message_seq'], notice.seq)
            start.assert_called_once()
            replacement._run(task, restored['id'], 'run')
        self.assertEqual(len(self.driver.calls), 3)
        self.assertEqual(replacement.status(task)['state'], 'completed')

    def test_live_consumer_keeps_queue_until_owner_releases(self):
        task, _, notice = self.stage()
        self.runner.recover_interrupted()
        queued = self.runner.status(task)
        with activity_owner(str(self.db), task, 'simple-team') as owned:
            self.assertTrue(owned)
            self.runner._run(task, queued['id'], 'run')
        self.assertEqual(self.runner.status(task)['state'], 'observing')
        self.assertEqual(len(self.driver.calls), 2)
        with self.factory() as runtime:
            self.assertIsNone(self.runner._activity_terminal(runtime, task, queued['id']))
        self.runner.recover_interrupted()
        self.assertEqual(self.runner.status(task)['id'], queued['id'])
        self.runner._run(task, queued['id'], 'run')
        self.assertEqual(len(self.driver.calls), 3)
        context = json.loads(self.driver.calls[-1]['prompt'].rsplit('\n', 1)[1])
        self.assertEqual(context['message']['seq'], notice.seq)

    def test_pause_after_queue_never_starts_supervision(self):
        task, _, _ = self.stage()
        self.runner.recover_interrupted()
        queued = self.runner.status(task)
        with self.factory() as runtime:
            runtime.execution_control.change(task, 'pause', request_id='web-boundary-pause')
        self.runner._run(task, queued['id'], 'run')
        self.assertEqual(len(self.driver.calls), 2)
        self.assertEqual(self.runner.status(task)['state'], 'failed')
        with self.factory() as runtime:
            self.assertEqual(runtime.execution_control.view(task)['state'], 'paused')

    def test_new_customer_takes_over_without_claiming_old_notice(self):
        task, _, notice = self.stage()
        self.runner.recover_interrupted()
        queued = self.runner.status(task)
        with self.factory() as runtime:
            newest = runtime.team.receive(task, 'Discuss another plan first', request_id='web-boundary-new-message')
            self.assertFalse(runtime.team.recovery.current(task, notice))
        self.runner._run(task, queued['id'], 'run')
        self.assertEqual(len(self.driver.calls), 3)
        context = json.loads(self.driver.calls[-1]['prompt'].rsplit('\n', 1)[1])
        self.assertEqual(context['message']['seq'], newest.seq)
        with self.factory() as runtime:
            started = [e.payload['message_seq'] for e in runtime.log.read(task_id=task) if e.type == 'team.run_started']
            self.assertNotIn(notice.seq, started)
            self.assertEqual(runtime.team.view(task)['pending_count'], 0)
            self.assertEqual(runtime.team.view(task)['status'], 'running')


if __name__ == '__main__':
    unittest.main()
