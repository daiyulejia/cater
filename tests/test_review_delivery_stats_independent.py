"""Formal review statistics against actual offline team event sequences."""
from dataclasses import replace
import unittest

from cater.events import Actor
from cater.review_delivery_stats import project_review_delivery_stats
import test_team_review_cycle as fixtures


class ReviewDeliveryStatsTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.TeamReviewCycleTests(methodName='runTest')
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def run_chain(self, second_content='version two\n', second_filename='result.txt'):
        f = self.f
        first = f.publish()
        req1 = f.request([first])['review_seq']
        f.record(req1, 'changes_requested')
        second = f.publish(second_content, filename=second_filename)
        req2 = f.request([second], review_seq=req1)['review_seq']
        f.record(req2, 'approved')
        f.acknowledge_latest_review()
        f.finish()
        events = f.rt.log.read(task_id=f.task)
        delivery = next(e.seq for e in events if e.type == 'team.objective_completed')
        return events, delivery, req1, req2

    def project(self, events, delivery):
        return project_review_delivery_stats(events, task_id=self.f.task, delivery_seq=delivery)

    def test_two_reviews_one_artifact_set_change_resubmission(self):
        events, delivery, req1, req2 = self.run_chain()
        result = self.project(events, delivery)
        self.assertTrue(result['complete'], result)
        self.assertEqual((result['review_conclusions'], result['changes_requested'],
                          result['artifact_set_change_resubmissions']), (2, 1, 1))
        self.assertEqual([x['request_seq'] for x in result['chains'][0]['decisions']], [req1, req2])
        self.assertNotIn('rework', str(result))

    def test_same_name_and_content_resubmission_is_not_a_set_change(self):
        events, delivery, _, _ = self.run_chain('version one\n')
        result = self.project(events, delivery)
        self.assertTrue(result['complete'], result)
        self.assertEqual(result['review_conclusions'], 2)
        self.assertEqual(result['artifact_set_change_resubmissions'], 0)

    def test_same_content_renamed_is_artifact_set_change_only(self):
        events, delivery, _, _ = self.run_chain('version one\n', 'renamed.txt')
        result = self.project(events, delivery)
        self.assertTrue(result['complete'], result)
        self.assertEqual(result['artifact_set_change_resubmissions'], 1)
        self.assertNotIn('changed_version_resubmissions', result)
        self.assertNotIn('rework', str(result))

    def test_delivery_of_old_version_with_new_pending_or_conflict_is_incomplete(self):
        events, delivery, req1, req2 = self.run_chain()
        first = next(e for e in events if e.seq == req1)
        newer_decision = next(e for e in events if e.type == 'team.review_recorded'
                              and e.payload['review_seq'] == req2)
        # The actual gateway normally blocks this ordering: mutate only this
        # disposable snapshot to exercise old/inconsistent history safely.
        for mode in ('pending', 'conflict'):
            with self.subTest(mode=mode):
                rows = [replace(e, payload={**e.payload, 'artifacts': first.payload['artifacts']})
                        if e.seq == delivery else e for e in events]
                if mode == 'pending':
                    rows = [e for e in rows if e.seq != newer_decision.seq]
                else:
                    spare = next(e for e in rows if newer_decision.seq < e.seq < delivery
                                 and e.type == 'team.run_finished')
                    rows[rows.index(spare)] = replace(newer_decision, seq=spare.seq,
                        payload={**newer_decision.payload, 'verdict': 'changes_requested'})
                result = self.project(rows, delivery)
                self.assertFalse(result['complete'], result)
                self.assertEqual(result['review_conclusions'], 0)

    def test_republished_old_version_makes_pending_or_conflicting_chain_incomplete(self):
        events, delivery, req1, req2 = self.run_chain()
        first = next(e for e in events if e.seq == req1)
        original_ref = first.payload['artifacts'][0]
        original = next(e for e in events if e.seq == original_ref['seq'])
        newer_decision = next(e for e in events if e.type == 'team.review_recorded'
                              and e.payload['review_seq'] == req2)
        candidates = [e for e in events if newer_decision.seq < e.seq < delivery
                      and e.type == 'team.run_finished']
        self.assertGreaterEqual(len(candidates), 2)
        for mode in ('pending', 'conflict', 'other_content', 'other_name', 'other_approved', 'untrusted_publication'):
            with self.subTest(mode=mode):
                republished = replace(original, seq=candidates[0].seq)
                if mode in ('other_content', 'other_approved'):
                    republished = replace(republished, payload={**republished.payload, 'content_sha256': 'b' * 64})
                if mode == 'other_name':
                    republished = replace(republished, payload={**republished.payload, 'filename': 'else.txt'})
                if mode == 'untrusted_publication':
                    republished = replace(republished, actor=Actor.human())
                ref = {'seq': republished.seq, 'filename': republished.payload['filename'],
                       'content_sha256': republished.payload['content_sha256']}
                rows = [republished if e.seq == candidates[0].seq else
                        replace(e, payload={**e.payload, 'artifacts': [ref]}) if e.seq == delivery else e
                        for e in events]
                if mode == 'conflict':
                    rows = [replace(newer_decision, seq=e.seq, payload={**newer_decision.payload,
                            'verdict': 'changes_requested'}) if e.seq == candidates[1].seq else e for e in rows]
                elif mode != 'other_approved':
                    rows = [e for e in rows if e.seq != newer_decision.seq]
                result = self.project(rows, delivery)
                self.assertEqual(result['complete'], mode == 'other_approved', result)
                self.assertEqual(result['review_conclusions'], 0)

    def test_actual_text_only_delivery_has_complete_zero_counts(self):
        f = self.f
        self.assertEqual(f.finish()['status'], 'ok')
        events = f.rt.log.read(task_id=f.task)
        delivery = next(e for e in events if e.type == 'team.objective_completed')
        self.assertEqual(delivery.payload['artifacts'], [])
        result = self.project(events, delivery.seq)
        self.assertTrue(result['complete'], result)
        self.assertEqual((result['review_conclusions'], result['changes_requested'],
                          result['artifact_set_change_resubmissions']), (0, 0, 0))
        # A missing or malformed list is not the explicit empty list.
        for value in (None, {}, '', [None]):
            with self.subTest(value=value):
                changed = [replace(e, payload={**e.payload, 'artifacts': value}) if e.seq == delivery.seq else e for e in events]
                self.assertFalse(self.project(changed, delivery.seq)['complete'])
        missing = [replace(e, payload={k: v for k, v in e.payload.items() if k != 'artifacts'})
                   if e.seq == delivery.seq else e for e in events]
        self.assertFalse(self.project(missing, delivery.seq)['complete'])

    def test_empty_delivery_does_not_hide_actual_pending_review(self):
        f = self.f
        artifact = f.publish()
        request = f.request([artifact])
        self.assertEqual(request['status'], 'ok')
        attempt = f.finish()
        self.assertEqual(attempt['status'], 'error')
        run = [e for e in f.rt.log.read(task_id=f.task) if e.type == 'team.run_started'][-1]
        # The host correctly blocked this finish. Add a synthetic old/abnormal
        # completion to exercise projection, never claim it was delivered.
        delivery = f.rt.log.emit(type='team.objective_completed', task_id=f.task,
            actor=Actor.system('team'), payload={'run_id': run.payload['run_id'],
                'summary': 'Synthetic historical record', 'verification': 'Fixture only', 'artifacts': []})
        result = self.project(f.rt.log.read(task_id=f.task), delivery.seq)
        self.assertFalse(result['complete'], result)
        self.assertEqual(result['review_conclusions'], 0)

    def test_empty_review_artifacts_is_invalid_even_with_empty_delivery(self):
        f = self.f
        self.assertEqual(f.finish()['status'], 'ok')
        events = f.rt.log.read(task_id=f.task)
        delivery = next(e for e in events if e.type == 'team.objective_completed')
        spare = next(e for e in events if e.type == 'team.run_finished' and e.seq < delivery.seq) if any(
            e.type == 'team.run_finished' and e.seq < delivery.seq for e in events) else None
        if spare is None:
            spare = next(e for e in events if e.type == 'team.tool_started' and e.seq < delivery.seq)
        malformed = replace(spare, type='team.review_requested', actor=Actor.system('team'),
            payload={'artifacts': [], 'root_seq': None, 'author': 'a', 'reviewer': 'b', 'revision': 0})
        rows = [malformed if e.seq == spare.seq else e for e in events]
        self.assertFalse(self.project(rows, delivery.seq)['complete'])

    def test_missing_conflicting_or_wrong_binding_is_incomplete(self):
        events, delivery, req1, req2 = self.run_chain()
        for defect in ('missing_previous', 'missing_artifact', 'missing_decision', 'conflicting_decision', 'wrong_actor',
                       'wrong_reviewer', 'wrong_revision', 'wrong_hash', 'wrong_task', 'duplicate_seq'):
            with self.subTest(defect=defect):
                rows = list(events)
                second = next(e for e in rows if e.seq == req2)
                first_decision = next(e for e in rows if e.type == 'team.review_recorded' and e.payload['review_seq'] == req1)
                if defect == 'missing_previous':
                    rows[rows.index(second)] = replace(second, payload={k: v for k, v in second.payload.items() if k != 'previous_review_seq'})
                elif defect == 'missing_artifact':
                    rows = [e for e in rows if e.seq != second.payload['artifacts'][0]['seq']]
                elif defect == 'missing_decision':
                    rows = [e for e in rows if not (e.type == 'team.review_recorded' and e.payload.get('review_seq') == req2)]
                elif defect == 'conflicting_decision':
                    # A second host conclusion inside the same snapshot is ambiguous.
                    spare = next(e for e in rows if e.seq > first_decision.seq and e.seq < req2 and e.type == 'team.run_finished')
                    rows[rows.index(spare)] = replace(first_decision, seq=spare.seq,
                        payload={**first_decision.payload, 'verdict': 'approved'})
                elif defect == 'wrong_actor':
                    rows[rows.index(first_decision)] = replace(first_decision, actor=Actor.agent('fake', 'worker'))
                elif defect == 'wrong_reviewer':
                    rows[rows.index(first_decision)] = replace(first_decision, payload={**first_decision.payload, 'reviewer': 'fake'})
                elif defect == 'wrong_revision':
                    rows[rows.index(second)] = replace(second, payload={**second.payload, 'revision': 999})
                elif defect == 'wrong_hash':
                    refs = [{**second.payload['artifacts'][0], 'content_sha256': '0' * 64}]
                    rows[rows.index(second)] = replace(second, payload={**second.payload, 'artifacts': refs})
                elif defect == 'wrong_task':
                    rows[0] = replace(rows[0], task_id='another-task')
                else:
                    rows.append(first_decision)
                result = self.project(rows, delivery)
                self.assertFalse(result['complete'], result)

    def test_delivery_boundary_ignores_later_events(self):
        events, delivery, req1, _ = self.run_chain()
        decision = next(e for e in events if e.type == 'team.review_recorded' and e.payload['review_seq'] == req1)
        later = replace(decision, seq=max(e.seq for e in events) + 1,
                        payload={**decision.payload, 'verdict': 'approved'})
        self.assertEqual(self.project(events, delivery), self.project(events + [later], delivery))

    def test_independent_delivered_chains_do_not_invent_resubmissions(self):
        f = self.f
        for filename in ('one.txt', 'two.txt'):
            artifact = f.publish(filename=filename)
            req = f.request([artifact])['review_seq']
            f.record(req)
            report = next(e for e in reversed(f.rt.team._messages(f.task))
                          if e.payload.get('kind') == 'report' and e.payload.get('review_seq') == req)
            f.exchange(f.worker, source=report)
        f.finish()
        events = f.rt.log.read(task_id=f.task)
        delivery = next(e.seq for e in events if e.type == 'team.objective_completed')
        result = self.project(events, delivery)
        self.assertTrue(result['complete'], result)
        self.assertEqual((result['review_conclusions'], result['changes_requested'],
                          result['artifact_set_change_resubmissions']), (2, 0, 0))
        self.assertEqual(len(result['chains']), 2)

    def test_missing_delivery_or_task_creation_is_not_zero_with_complete_status(self):
        events, delivery, _, _ = self.run_chain()
        for kind in ('task.created', 'team.objective_completed'):
            rows = [e for e in events if e.type != kind]
            self.assertFalse(self.project(rows, delivery)['complete'])


if __name__ == '__main__':
    unittest.main()
