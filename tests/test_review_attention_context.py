"""Advisory review context against real offline team events; no model service."""
import copy
import unittest
from unittest.mock import patch

from cater.team import _review_attention
from cater.team_context import model_context
from cater.work import WorkConflict
import test_team_review_cycle as fixtures


class ReviewAttentionContextTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.TeamReviewCycleTests(methodName='runTest')
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def interrupted_review(self):
        f = self.f
        artifact = f.publish()
        request = f.request([artifact], requester=f.manager)
        assignment = next(e for e in f.rt.team._messages(f.task) if e.seq == request['message_seq'])
        with patch.object(f.driver, 'tool_turn', side_effect=WorkConflict('fixture interruption')):
            f.exchange(f.reviewer, source=assignment, expected_statuses=('interrupted',))
        progress = next(e for e in reversed(f.rt.team._messages(f.task))
                        if e.payload.get('kind') == 'progress' and e.payload.get('reply_to') == assignment.seq)
        return request, assignment, progress

    def test_owner_receives_hint_in_actual_message_and_external_context(self):
        f = self.f
        request, assignment, progress = self.interrupted_review()
        f.exchange(f.manager, source=progress)
        context = f.driver.calls[-1]['context']
        attention = context['review_attention']
        self.assertEqual(attention['entries'], [{'review_seq': request['review_seq'],
            'assignment_seq': assignment.seq, 'recorded_run_status': 'interrupted'}])
        self.assertIn('未认证', attention['note'])
        self.assertIn('不代表可安全接续', attention['note'])
        self.assertEqual(model_context(context)['review_attention'], attention)
        self.assertFalse([e for e in f.rt.team._messages(f.task)
                          if e.payload.get('continue_assignment_seq') == assignment.seq])
        self.assertEqual(f.rt.team_reviews.view(f.task)[0]['status'], 'pending')

    def test_second_review_after_changes_requested_matches_real_event_shape(self):
        f = self.f
        first_artifact = f.publish()
        first = f.request([first_artifact], requester=f.manager)
        first_assignment = next(e for e in f.rt.team._messages(f.task) if e.seq == first['message_seq'])
        decision = f.record(first['review_seq'], 'changes_requested', source=first_assignment)
        report = next(e for e in f.rt.team._messages(f.task) if e.seq == decision['message_seq'])
        f.exchange(f.manager, source=report)
        revised = f.publish('revised fixture\n')
        second = f.request([revised], requester=f.manager, review_seq=first['review_seq'])
        original = next(e for e in f.rt.team._messages(f.task) if e.seq == second['message_seq'])
        with patch.object(f.driver, 'tool_turn', side_effect=WorkConflict('offline time limit')):
            f.exchange(f.reviewer, source=original, expected_statuses=('interrupted',))
        progress = next(e for e in reversed(f.rt.team._messages(f.task))
                        if e.payload.get('kind') == 'progress' and e.payload.get('reply_to') == original.seq)
        f.exchange(f.manager, source=progress)
        hint, = f.driver.calls[-1]['context']['review_attention']['entries']
        self.assertEqual(hint, {'review_seq': second['review_seq'], 'assignment_seq': original.seq,
                                'recorded_run_status': 'interrupted'})
        self.assertEqual(second['root_seq'], first['review_seq'])
        self.assertEqual(f.rt.team_reviews.view(f.task)[0]['status'], 'pending')

    def test_direct_consultation_is_not_inferred_or_auto_continued(self):
        f = self.f
        request, assignment, _ = self.interrupted_review()
        source = f.rt.team.receive(f.task, '@' + f.rt.team._role(f.reviewer).name + ' Explain this review',
                                  request_id='direct-consult-attention')
        f.exchange(f.reviewer, source=source, expected_statuses=('completed',))
        self.assertNotIn('review_attention', f.driver.calls[-1]['context'])
        self.assertFalse([e for e in f.rt.team._messages(f.task)
                          if e.payload.get('continue_assignment_seq') == assignment.seq])
        f.exchange(f.manager)
        hint, = f.driver.calls[-1]['context']['review_attention']['entries']
        self.assertEqual((hint['review_seq'], hint['recorded_run_status']), (request['review_seq'], 'interrupted'))

    def test_direction_change_removes_old_hint_and_continuation_stays_blocked(self):
        f = self.f
        _, assignment, _ = self.interrupted_review()
        f.rt.command(f.task, 'New direction', goal='Different objective')
        f.exchange(f.manager)
        self.assertNotIn('review_attention', f.driver.calls[-1]['context'])
        self.assertEqual(f.rt.team_reviews.view(f.task)[0]['status'], 'stale')
        result, = f.exchange(f.manager, ('continue_work', {'assignment_seq': assignment.seq, 'instruction': 'Check'}))
        self.assertEqual(result['status'], 'error')

    def test_explicit_owner_continuation_and_review_are_unchanged(self):
        f = self.f
        request, assignment, _ = self.interrupted_review()
        result, = f.exchange(f.manager, ('continue_work', {'assignment_seq': assignment.seq, 'instruction': 'Review the original version'}))
        self.assertEqual(result['status'], 'ok')
        source = next(e for e in f.rt.team._messages(f.task) if e.seq == result['message_seq'])
        self.assertEqual(f.record(request['review_seq'], 'changes_requested', source=source)['status'], 'ok')

    def test_missing_or_ambiguous_view_is_advisory_unknown(self):
        reviews = [{'author': 'owner', 'reviewer': 'reviewer', 'message_seq': 20,
                    'request_seq': 10, 'revision': 0, 'status': 'pending'}]
        assignments = [{'seq': 20, 'sender': 'owner', 'recipient': 'reviewer', 'status': 'interrupted'}]
        original = copy.deepcopy((reviews, assignments))
        for revs, work in ((reviews, []), (reviews * 2, assignments), (reviews, assignments * 2),
                           ([{**reviews[0], 'revision': 1}], assignments)):
            hints = _review_attention(revs, work, 'owner', 0)['entries']
            self.assertTrue(all(row['recorded_run_status'] == 'unknown' and 'assignment_seq' not in row for row in hints))
        self.assertEqual((reviews, assignments), original)

    def test_indexed_join_preserves_duplicates_aliases_and_truncation(self):
        reviews = [{'author': 'owner', 'reviewer': 'r', 'message_seq': n, 'request_seq': 1000 + n,
                    'revision': 0, 'status': 'pending'} for n in range(1, 27)]
        assignments = [{'seq': n, 'sender': 'owner', 'recipient': 'r', 'status': 'interrupted'}
                       for n in range(1, 27)]
        reviews.append({**reviews[20], 'request_seq': 9999, 'message_seq': 21.0})
        assignments.append({**assignments[21], 'seq': 22.0})
        assignments.append({**assignments[22], 'recipient': 'another'})
        assignments.append({'seq': [], 'sender': 'owner', 'recipient': 'r'})
        reviews.append({'author': 'owner', 'reviewer': 'r', 'message_seq': [], 'request_seq': 9998,
                        'revision': 0, 'status': 'pending'})
        view = _review_attention(reviews, assignments, 'owner', 0)
        self.assertEqual(view['total'], 28)
        self.assertTrue(view['truncated'])
        self.assertEqual(len(view['entries']), 20)
        by_request = {row['review_seq']: row for row in view['entries']}
        self.assertEqual(by_request[1021]['recorded_run_status'], 'unknown')
        self.assertNotIn('assignment_seq', by_request[1021])
        self.assertEqual(by_request[1022]['recorded_run_status'], 'unknown')
        self.assertEqual(by_request[1023]['recorded_run_status'], 'interrupted')
        self.assertEqual(by_request[9998]['recorded_run_status'], 'unknown')
        self.assertEqual(by_request[9999]['recorded_run_status'], 'unknown')


if __name__ == '__main__':
    unittest.main()
