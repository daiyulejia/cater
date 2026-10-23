"""Native handoff ends ownership of this turn, not the team's objective.

Scripted model responses exercise real scheduling and disposable file writes;
these are deterministic regressions, not remote-model acceptance.
"""

import unittest

from test_team_supervision import TeamSupervisionTests


class TeamReturnBoundaryTests(unittest.TestCase):
    def fixture(self):
        f = TeamSupervisionTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        root = f.bind_workspace()
        return f, root

    def arrange(self, f, worker_steps):
        f.driver.steps = {
            f.manager: [
                f.native_turn('', ('delegate', {
                    'recipient': f.worker, 'instruction': 'Return the inspected result.'})),
                f.native_turn('', ('wait_for_reply', {})),
                f.native_turn('Result received; other work remains.'),
            ],
            f.worker: worker_steps,
        }
        f.rt.team.receive(f.task, 'Arrange a bounded check', request_id='return-boundary-start')
        f.run_team()

    def report(self, f, context, *following):
        return f.native_turn('', ('send_message', {
            'recipient': f.manager, 'reply_to': context['active_assignment']['seq'],
            'text': 'Inspected result formally returned.',
        }), *following)

    def write(self):
        return ('workspace_write', {
            'path': 'after-return.txt', 'content': 'Unexpected late mutation',
            'expected_sha256': None,
        })

    def assert_returned_once(self, f, root):
        reports = [e for e in f.rt.team._messages(f.task) if e.payload['kind'] == 'report']
        self.assertEqual(len(reports), 1)
        self.assertFalse((root / 'after-return.txt').exists())
        self.assertEqual(f.results('workspace_write'), [])
        self.assertEqual(sum(c['context']['you']['id'] == f.worker for c in f.driver.calls), 1)
        self.assertEqual(f.rt.team.view(f.task)['status'], 'running')
        self.assertEqual(f.rt.team.view(f.task)['pending_count'], 0)
        self.assertEqual(f.rt.team.recovery.assignments(f.task), [])

    def test_formal_return_does_not_execute_later_tools_in_same_batch(self):
        f, root = self.fixture()
        self.arrange(f, [
            lambda context, history, tools: self.report(f, context, self.write()),
            f.native_turn('This extra model request must not happen.'),
        ])
        self.assert_returned_once(f, root)

    def test_formal_return_does_not_start_another_model_turn(self):
        f, root = self.fixture()
        self.arrange(f, [
            lambda context, history, tools: self.report(f, context),
            f.native_turn('', self.write()),
            f.native_turn('Late result must not replace the returned result.'),
        ])
        self.assert_returned_once(f, root)

    def test_notification_does_not_end_the_assignment(self):
        f, root = self.fixture()
        self.arrange(f, [
            f.native_turn('', ('send_message', {
                'recipient': f.manager, 'text': 'Still checking.', 'notify_only': True})),
            f.native_turn('', self.write()),
            lambda context, history, tools: self.report(f, context),
            f.native_turn('Must not request a fourth turn.'),
        ])
        self.assertTrue((root / 'after-return.txt').exists())
        self.assertEqual(len(f.results('workspace_write')), 1)
        self.assertEqual(sum(c['context']['you']['id'] == f.worker for c in f.driver.calls), 3)
        self.assertEqual(len([e for e in f.rt.team._messages(f.task)
                              if e.payload['kind'] == 'report']), 1)

    def test_rejected_return_allows_correction_and_does_not_end_the_assignment(self):
        f, root = self.fixture()
        self.arrange(f, [
            lambda context, history, tools: f.native_turn('', ('send_message', {
                'recipient': f.worker, 'reply_to': context['active_assignment']['seq'],
                'text': 'Wrong direction, not a return.',
            })),
            f.native_turn('', self.write()),
            lambda context, history, tools: self.report(f, context),
            f.native_turn('Must not request a fourth turn.'),
        ])
        self.assertEqual([r['status'] for r in f.results('send_message')], ['error', 'ok'])
        self.assertTrue((root / 'after-return.txt').exists())
        self.assertEqual(sum(c['context']['you']['id'] == f.worker for c in f.driver.calls), 3)

    def test_new_input_can_start_new_work_after_a_formal_return(self):
        f, root = self.fixture()
        self.arrange(f, [lambda context, history, tools: self.report(f, context)])
        self.assert_returned_once(f, root)
        first_report, = [e for e in f.rt.team._messages(f.task) if e.payload['kind'] == 'report']
        new = f.rt.team._send(f.task, f.rt.organization.leader, f.worker,
                             'A separate follow-up assignment.', 'assignment')
        f.driver.steps[f.worker] = [
            f.native_turn('', self.write()),
            lambda context, history, tools: self.report(f, context),
        ]
        f.driver.steps[f.manager] = [f.native_turn('New result received.')]
        f.run_team()
        reports = [e for e in f.rt.team._messages(f.task) if e.payload['kind'] == 'report']
        self.assertEqual(len(reports), 2)
        self.assertEqual(reports[0], first_report)
        self.assertEqual(reports[1].payload['reply_to'], new.seq)
        self.assertTrue((root / 'after-return.txt').exists())


def load_tests(loader, tests, pattern):
    return loader.loadTestsFromTestCase(TeamReturnBoundaryTests)


if __name__ == '__main__':
    unittest.main()
