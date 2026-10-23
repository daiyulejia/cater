"""Tool calls are not native model turns; external time budgets still apply.

Offline host protocol checks, not real-model task acceptance.
"""
import unittest
from unittest.mock import patch

import cater.team as team
import test_team_external as fixture


class ExternalToolContinuityTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.TeamExternalTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def run_one(self, script):
        f = self.f
        f.driver.scripts[f.manager] = [script]
        message = f.rt.team.receive(f.task, 'Inspect a sequence and reply without restarting.')
        claimed = f.rt.team._claim(f.task)
        self.assertEqual(claimed[0].seq, message.seq)
        list(f.rt.team._run_message(f.task, *claimed))
        self.assertEqual(f.driver.errors, [])
        self.assertEqual(len(f.driver.runs), 1)
        self.assertEqual(len(f.events('team.model_called')), 1)
        self.assertEqual(f.events('team.message_received')[0].seq, message.seq)
        self.assertTrue(f.rt.verify_chain().ok)

    def test_more_than_32_reads_continue_in_one_external_session(self):
        def inspect(run):
            for seq in range(40):
                result = run.call('read_messages', after_seq=seq, limit=1)
                self.assertEqual(result['status'], 'ok')
            run.call('reply_to_customer', text='The requested sequence was inspected.')
        self.run_one(inspect)
        f = self.f
        self.assertEqual(len(f.events('team.tool_started')), 41)
        finished, = f.events('team.run_finished')
        self.assertEqual(finished.payload['status'], 'completed')
        self.assertNotIn('interruption_kind', finished.payload)
        self.assertEqual([e for e in f.events('team.message_received') if e.payload.get('kind') == 'recovery'], [])

    def test_native_model_turn_limit_does_not_limit_external_tool_calls(self):
        def inspect(run):
            for seq in range(3):
                run.call('read_messages', after_seq=seq, limit=1)
            run.call('wait_for_reply')
        with patch.object(team, 'MAX_ACTOR_TURNS', 1):
            self.run_one(inspect)
        finished, = self.f.events('team.run_finished')
        self.assertEqual(finished.payload['status'], 'waiting')
        self.assertNotIn('interruption_kind', finished.payload)
        self.assertEqual(len(self.f.events('team.tool_started')), 4)


if __name__ == '__main__':
    unittest.main()
