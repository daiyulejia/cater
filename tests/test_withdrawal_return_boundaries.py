"""Return and withdrawal boundaries with real temporary Runtime fixtures."""

import unittest
from unittest.mock import patch

from cater.events import Actor
from cater.team import HOST, _reported_messages
from cater.team_work_control import linked_inputs
from cater.work import WorkConflict
import test_team_review_cycle as reviews


class WithdrawalReturnBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.f = reviews.TeamReviewCycleTests()
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()
        self.rt, self.task = self.f.rt, self.f.task

    def tearDown(self):
        self.assertEqual(self.f.protocol_errors, [])
        self.assertTrue(self.rt.verify_chain().ok)

    def message(self, seq):
        return next(e for e in self.rt.team._messages(self.task) if e.seq == seq)

    def emit_message(self, sender, recipient, kind, *, task_id=None, **extra):
        task_id = task_id or self.task
        role = self.rt.team._role(sender)
        return self.rt.log.emit(type='team.message_received', task_id=task_id,
            actor=Actor.agent(role.id, role.role), payload={
                'kind': kind, 'recipient': recipient, 'text': 'Boundary fixture input.',
                'revision': self.rt.team.view(task_id)['revision'], **extra,
            })

    def assign(self, *, sender=None, recipient=None, source=None):
        result, = self.f.exchange(sender or self.f.manager, ('delegate', {
            'recipient': recipient or self.f.worker,
            'instruction': 'Inspect the requested result.',
        }), source=source)
        self.assertEqual(result['status'], 'ok', result)
        return self.message(result['message_seq'])

    def withdraw(self, source):
        result, = self.f.exchange(self.f.manager, ('withdraw_work', {
            'message_seq': source.seq,
            'reason': 'This parent request is no longer needed.',
        }))
        self.assertEqual(result['status'], 'ok', result)
        self.assertTrue(result['withdrawn'], result)
        self.assertIn(source.seq, self.rt.team._inactive_messages(self.task))
        return result

    def start(self, source):
        return self.rt.log.emit(type='team.run_started', task_id=self.task, actor=HOST, payload={
            'message_seq': source.seq, 'role': source.payload['recipient'],
            'run_id': 'withdrawal-boundary-' + str(source.seq),
            'revision': self.rt.team.view(self.task)['revision'],
            'control_seq': self.rt.execution_control.view(self.task)['seq'],
        })

    def run_steps(self, source, *steps):
        role = self.rt.team._role(source.payload['recipient'])
        started = self.start(source)
        self.f.driver.steps[role.id] = list(steps)
        running = self.rt.team._run_message(self.task, source, role, started)
        self.addCleanup(running.close)
        return started, running

    def reports_to(self, source):
        return [e for e in self.rt.team._messages(self.task)
                if e.payload.get('kind') == 'report'
                and e.payload.get('reply_to') == source.seq
                and e.actor.id == source.payload['recipient']
                and e.payload.get('recipient') == source.actor.id]

    def test_side_return_allows_delivery_without_starting_queued_continuation(self):
        original = self.assign()
        started = self.start(original)
        with patch.object(self.rt.team, '_current', side_effect=WorkConflict('Fixture interruption')):
            list(self.rt.team._run_message(
                self.task, original, self.rt.team._role(self.f.worker), started))
        continued, = self.f.exchange(self.f.manager, ('send_message', {
            'recipient': self.f.worker, 'text': 'Inspect what exists, then finish.',
            'continue_assignment_seq': original.seq,
        }))
        self.assertEqual(continued['status'], 'ok', continued)
        continuation = self.message(continued['message_seq'])
        artifact = self.f.publish('Published and checked result')
        returned, = self.f.exchange(self.f.worker, ('send_message', {
            'recipient': self.f.manager, 'reply_to': original.seq,
            'text': 'Actually completed via side input.',
        }))
        self.assertEqual(returned['status'], 'ok', returned)
        self.assertEqual([e.seq for e in self.reports_to(original)], [returned['message_seq']])
        reported = _reported_messages(self.rt.team._messages(self.task))
        self.assertIn(continuation.seq, reported)
        self.assertNotIn(returned['message_seq'], reported)
        self.assertNotIn(returned['message_seq'], self.rt.team._inactive_messages(self.task))
        self.assertNotIn(continuation.seq,
                         {r['message_seq'] for r in self.rt.team.view(self.task)['runs']})
        self.assertEqual(self.rt.team.recovery.assignments(self.task), [])

        finished = self.f.finish()

        self.assertEqual(finished['status'], 'ok', finished)
        self.assertEqual(self.rt.team.view(self.task)['status'], 'done')
        delivery, = self.f.events('team.objective_completed')
        self.assertIn(artifact, {a['seq'] for a in delivery.payload['artifacts']})
        self.assertNotIn(continuation.seq,
                         {r['message_seq'] for r in self.rt.team.view(self.task)['runs']})

    def test_withdrawn_parent_stops_run_already_processing_child_return(self):
        parent = self.assign()
        child = self.assign(sender=self.f.worker, recipient=self.f.reviewer, source=parent)
        returned, = self.f.exchange(self.f.reviewer, ('send_message', {
            'recipient': self.f.worker, 'reply_to': child.seq, 'text': 'Detail checked.',
        }), source=child)
        self.assertEqual(returned['status'], 'ok', returned)
        feedback = self.message(returned['message_seq'])
        assignment = self.rt.team._waiting_assignment(
            self.task, feedback, self.rt.team._role(self.f.worker))
        self.assertIsNotNone(assignment)
        self.assertEqual(assignment.seq, parent.seq)
        started, running = self.run_steps(
            feedback,
            self.f.native_turn('', ('read_receipts', {})),
            self.f.native_turn('', ('publish_artifact', {
                'filename': 'after-withdraw.txt',
                'content': 'This must not be published after the parent is withdrawn.',
                'summary': 'Cancelled parent output', 'format': 'text',
            })),
            self.f.native_turn('', ('wait_for_reply', {})),
        )
        # The run has already captured the parent assignment before withdrawal.
        next(running)
        self.assertTrue(any(e.payload['run_id'] == started.payload['run_id']
                            and e.payload['name'] == 'read_receipts'
                            and e.payload['result']['status'] == 'ok'
                            for e in self.f.events('team.tool_finished')))
        self.withdraw(parent)
        list(running)

        self.assertEqual(self.rt.task(self.task).deliverables, [],
                         'A withdrawn parent continued publishing from a child-report input')
        finished, = [e for e in self.f.events('team.run_finished')
                     if e.payload['run_id'] == started.payload['run_id']]
        self.assertEqual(finished.payload['status'], 'interrupted', finished.payload)
        self.assertEqual(self.reports_to(parent), [])
        self.assertFalse(self.rt.team._pending_operations(self.task))

    def test_running_author_remains_protected_after_formal_return(self):
        self.f.publish('Published result')
        source = self.assign()
        started, running = self.run_steps(
            source,
            self.f.native_turn('', ('send_message', {
                'recipient': self.f.manager, 'reply_to': source.seq, 'text': 'Result returned.',
            })),
            self.f.native_turn('', ('wait_for_reply', {})),
        )
        # Send a real report, then let the manager run before the author ends.
        next(running)
        report, = self.reports_to(source)
        self.assertGreater(report.seq, started.seq)
        active, = [r for r in self.rt.team.view(self.task)['runs']
                   if r['run_id'] == started.payload['run_id']]
        self.assertEqual(active['status'], 'running')
        self.assertEqual(self.rt.team.recovery.assignments(self.task), [])

        blocked = self.f.finish()

        self.assertEqual((blocked['status'], blocked.get('code')), ('error', 'author_pending'), blocked)
        self.assertIn(source.seq, {r['seq'] for r in blocked['assignments']})
        self.assertEqual(self.f.events('team.objective_completed'), [])
        list(running)
        finished = self.f.finish()
        self.assertEqual(finished['status'], 'ok', finished)
        self.assertEqual(self.rt.team.view(self.task)['status'], 'done')
        self.assertEqual(self.reports_to(source), [report])

    def test_child_can_execute_and_return_after_parent_is_withdrawn(self):
        parent = self.assign()
        child = self.assign(sender=self.f.worker, recipient=self.f.reviewer, source=parent)
        withdrawal = self.withdraw(parent)
        self.assertNotIn(child.seq, self.rt.team._inactive_messages(self.task))
        self.assertIn(child.seq, {r['seq'] for r in self.rt.team.recovery.assignments(self.task)})
        self.assertNotIn(child.seq, {r['message_seq'] for r in self.rt.team.view(self.task)['runs']})

        published, returned = self.f.exchange(
            self.f.reviewer,
            ('publish_artifact', {
                'filename': 'child-result.txt', 'content': 'Independent child result.',
                'summary': 'Child completed after parent withdrawal', 'format': 'text',
            }),
            ('send_message', {
                'recipient': self.f.worker, 'reply_to': child.seq, 'text': 'Child result returned.',
            }),
            source=child,
        )

        self.assertEqual(published['status'], 'ok', published)
        self.assertEqual(returned['status'], 'ok', returned)
        artifact, = self.rt.task(self.task).deliverables
        self.assertEqual(artifact['seq'], published['artifact_seq'])
        self.assertEqual(artifact['actor'], self.f.reviewer)
        self.assertGreater(artifact['seq'], withdrawal['withdrawal_seq'])
        report, = self.reports_to(child)
        self.assertEqual(report.seq, returned['message_seq'])
        self.assertIsNone(self.rt.team._waiting_assignment(
            self.task, report, self.rt.team._role(self.f.worker)))
        self.assertEqual(self.reports_to(parent), [])
        self.assertEqual(self.rt.team.recovery.assignments(self.task), [])
        self.assertNotIn(child.seq, self.rt.team._inactive_messages(self.task))
        self.assertIn(report.seq, self.rt.team._inactive_messages(self.task))
        self.assertEqual(self.rt.team.view(self.task)['pending_count'], 0)
        self.assertIsNone(self.rt.team._claim(self.task))

    def test_returned_input_links_only_matching_continuations_and_keeps_report(self):
        original = self.assign()
        revision = original.payload['revision']
        valid = self.emit_message(self.f.manager, self.f.worker, 'continuation',
                                  continue_assignment_seq=original.seq)
        invalid = {
            'wrong_sender': self.emit_message(self.f.reviewer, self.f.worker, 'continuation',
                                             continue_assignment_seq=original.seq),
            'wrong_recipient': self.emit_message(self.f.manager, self.f.reviewer, 'continuation',
                                                continue_assignment_seq=original.seq),
            'wrong_revision': self.emit_message(self.f.manager, self.f.worker, 'continuation',
                                               continue_assignment_seq=original.seq, revision=revision + 1),
        }
        returned, = self.f.exchange(self.f.worker, ('send_message', {
            'recipient': self.f.manager, 'reply_to': original.seq, 'text': 'Actual return.',
        }))
        self.assertEqual(returned['status'], 'ok', returned)

        reported = _reported_messages(self.rt.team._messages(self.task))

        self.assertEqual(reported, {original.seq, valid.seq})
        self.assertNotIn(returned['message_seq'], reported)
        for edge, message in invalid.items():
            with self.subTest(edge=edge):
                self.assertNotIn(message.seq, reported)

    def test_withdrawal_does_not_follow_child_feedback_with_wrong_direction(self):
        parent = self.assign()
        child = self.assign(sender=self.f.worker, recipient=self.f.reviewer, source=parent)
        self.withdraw(parent)
        # Invalid envelopes enter through the real event log to test projection guards.
        valid = [self.emit_message(self.f.reviewer, self.f.worker, kind, reply_to=child.seq)
                 for kind in ('report', 'progress')]
        invalid = {}
        for kind in ('report', 'progress'):
            invalid[kind + '_wrong_sender'] = self.emit_message(
                self.f.observer, self.f.worker, kind, reply_to=child.seq)
            invalid[kind + '_wrong_recipient'] = self.emit_message(
                self.f.reviewer, self.f.manager, kind, reply_to=child.seq)
        wrong_child = self.emit_message(self.f.manager, self.f.reviewer, 'assignment',
                                        parent_assignment_seq=parent.seq)
        invalid['child_wrong_sender'] = self.emit_message(
            self.f.reviewer, self.f.manager, 'report', reply_to=wrong_child.seq)

        inactive = self.rt.team._inactive_messages(self.task)

        self.assertTrue({parent.seq, *(e.seq for e in valid)} <= inactive)
        self.assertTrue({child.seq, wrong_child.seq}.isdisjoint(inactive))
        for edge, message in invalid.items():
            with self.subTest(edge=edge):
                self.assertNotIn(message.seq, inactive)

    def test_child_feedback_does_not_cross_objectives_or_groups(self):
        parent = self.assign()
        child = self.assign(sender=self.f.worker, recipient=self.f.reviewer, source=parent)
        self.withdraw(parent)
        updated, = self.f.exchange(self.f.manager, ('set_objective', {'goal': 'Independent new goal'}))
        self.assertEqual(updated['status'], 'ok', updated)
        revision = self.rt.team.view(self.task)['revision']
        self.assertNotEqual(revision, parent.payload['revision'])
        new_child = self.emit_message(self.f.worker, self.f.reviewer, 'assignment',
                                      parent_assignment_seq=parent.seq)
        mismatched_report = self.emit_message(self.f.reviewer, self.f.worker, 'report', reply_to=child.seq)
        new_report = self.emit_message(self.f.reviewer, self.f.worker, 'report', reply_to=new_child.seq)

        inactive = self.rt.team._inactive_messages(self.task)

        self.assertIn(parent.seq, inactive)
        self.assertTrue({child.seq, new_child.seq, mismatched_report.seq, new_report.seq}.isdisjoint(inactive))
        other = self.rt.create_task('Separate boundary fixture group')
        self.rt.team.enable(other)
        foreign_child = self.emit_message(self.f.worker, self.f.reviewer, 'assignment', task_id=other,
                                          parent_assignment_seq=parent.seq,
                                          revision=parent.payload['revision'])
        foreign_report = self.emit_message(self.f.reviewer, self.f.worker, 'report',
                                           task_id=other, reply_to=foreign_child.seq)
        cross_group_report = self.emit_message(self.f.reviewer, self.f.worker, 'report',
                                               task_id=other, reply_to=child.seq)
        mixed = [*self.rt.team._messages(self.task), *self.rt.team._messages(other)]
        linked = linked_inputs(mixed, {parent.seq})
        self.assertTrue({foreign_child.seq, foreign_report.seq, cross_group_report.seq}.isdisjoint(linked))


if __name__ == '__main__':
    unittest.main()
