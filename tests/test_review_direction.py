"""Review supervision must bind the work direction, including legacy runs."""
import unittest
from unittest.mock import patch
from cater.events import Actor
from cater.review_supervision_candidate import create, validate, _payload, _review_evidence
import test_review_supervision_candidate as fixtures


class ReviewDirectionTests(unittest.TestCase):
    def test_legacy_candidate_scope_is_reauthenticated_and_claimable(self):
        helper = fixtures.ReviewSupervisionCandidateTests(methodName='runTest')
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        f, run = helper.ready()
        events = f.rt.log.read(task_id=f.task)
        report = next(e for e in events if e.seq == run.payload['message_seq'])
        leader = f.rt.organization.leader
        reviewer = f.rt.team._role(report.actor.id)
        evidence = _review_evidence(events, task_id=f.task, leader_actor=Actor.agent(leader.id, leader.role),
            reviewer_actor=Actor.agent(reviewer.id, reviewer.role), run_seq=run.seq, run_hash=run.hash)
        payload = _payload(leader.id, run.seq, run.hash, run.payload['recovery_scope'], evidence)
        candidate = f.rt.log.emit(type='team.message_received', task_id=f.task, actor=Actor.system('team'), payload=payload)
        self.assertTrue(validate(f.rt.team, f.task, candidate, events=f.rt.log.read(task_id=f.task)))
        with f.rt.team.recovery.review_candidate_lease(f.task, candidate_seq=candidate.seq, candidate_hash=candidate.hash) as claimed:
            self.assertIsNotNone(claimed)
            self.assertNotIn('work_direction_seq', claimed[2].payload['recovery_scope'])

    def setup_review(self):
        helper = fixtures.ReviewSupervisionCandidateTests(methodName='runTest')
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        f = helper.fixture('external')
        artifact, = helper.run_calls(f, helper.human(f), ('publish_artifact', {
            'filename': 'review.txt', 'content': 'fixture', 'summary': 'fixture', 'format': 'text'}))
        review, = helper.run_calls(f, helper.human(f), ('request_review', {
            'reviewer': f.reviewer, 'artifact_seqs': [artifact['artifact_seq']], 'instruction': 'Inspect'}))
        helper.run_calls(f, helper.message(f, review['message_seq']), ('record_review', {
            'review_seq': review['review_seq'], 'verdict': 'approved', 'feedback': 'Checked'}), statuses=('waiting',))
        return helper, f

    def test_direction_changed_after_report_before_manager_run_has_no_candidate(self):
        helper, f = self.setup_review()
        previous = f.rt.work.scope(f.task)['direction_seq']
        old_team_revision = f.rt.team.view(f.task)['revision']
        command = f.rt.command(f.task, 'Change work direction', goal='A different deliverable')
        self.assertEqual(command.actor, Actor.human())
        self.assertGreater(f.rt.work.scope(f.task)['direction_seq'], previous)
        self.assertEqual(f.rt.team.view(f.task)['revision'], old_team_revision)
        claimed = f.rt.team._claim(f.task)
        self.assertIsNone(claimed)
        self.assertFalse([e for e in f.events('team.message_received') if e.payload.get('trigger') == 'review_return_candidate'])

    def test_old_report_cannot_publish_when_direction_changes_before_or_during_run(self):
        for when in ('before', 'during'):
            with self.subTest(when=when):
                helper, f = self.setup_review()
                if when == 'before':
                    f.rt.command(f.task, 'Update direction', goal='New scope')
                claimed = f.rt.team._claim(f.task)
                if when == 'before':
                    self.assertIsNone(claimed)
                    self.assertFalse([e for e in f.events('deliverable.published') if e.payload.get('filename') == 'stale.txt'])
                    continue
                def script(run):
                    if when == 'during':
                        f.rt.command(f.task, 'Update direction', goal='New scope')
                    run.call('publish_artifact', filename='stale.txt', content='stale', summary='stale', format='text')
                f.driver.scripts[f.manager] = [script]
                list(f.rt.team._run_message(f.task, *claimed))
                self.assertFalse([e for e in f.events('deliverable.published') if e.payload.get('filename') == 'stale.txt'])
                finish, = [e for e in f.events('team.run_finished') if e.payload.get('run_id') == claimed[2].payload['run_id']]
                self.assertEqual(finish.payload['status'], 'interrupted')

    def test_direction_change_after_candidate_prevents_claim_and_old_review_delivery(self):
        helper = fixtures.ReviewSupervisionCandidateTests(methodName='runTest')
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        f, run = helper.ready()
        candidate = create(f.rt.team, f.task, run_seq=run.seq, run_hash=run.hash)
        self.assertIsNotNone(candidate)
        f.rt.command(f.task, 'Change direction', goal='Updated scope')
        with f.rt.team.recovery.review_candidate_lease(f.task, candidate_seq=candidate.seq, candidate_hash=candidate.hash) as claimed:
            self.assertIsNone(claimed)
        old_review, = f.rt.team_reviews.view(f.task)
        old_artifacts = [ref['seq'] for ref in old_review['artifacts']]
        self.assertTrue(old_artifacts)
        result, = helper.run_calls(f, helper.human(f), ('finish_work', {'summary': 'Old review', 'verification': 'fixture',
                                                                        'artifact_seqs': old_artifacts}))
        self.assertEqual(result.get('code'), 'review_direction_changed')
        self.assertEqual(f.events('team.objective_completed'), [])

    def test_new_direction_approved_review_can_deliver_with_old_history(self):
        helper, f = self.setup_review()
        old, = f.events('team.review_requested')
        f.rt.command(f.task, 'Change direction', goal='Updated scope')
        artifact, = helper.run_calls(f, helper.human(f), ('publish_artifact', {
            'filename': 'updated.txt', 'content': 'updated fixture', 'summary': 'updated', 'format': 'text'}))
        artifact_seq = artifact['artifact_seq']
        new, = helper.run_calls(f, helper.human(f), ('request_review', {
            'reviewer': f.reviewer, 'artifact_seqs': [artifact_seq],
            'instruction': 'Check against the new direction'}))
        self.assertEqual(new['status'], 'ok', new)
        helper.run_calls(f, helper.message(f, new['message_seq']), ('record_review', {
            'review_seq': new['review_seq'], 'verdict': 'approved', 'feedback': 'Checked updated scope'}), statuses=('waiting',))
        # Consume the formal report so that no pending input blocks delivery.
        claimed = f.rt.team._claim(f.task)
        f.driver.scripts[f.manager] = [lambda run: 'Reviewed.']
        list(f.rt.team._run_message(f.task, *claimed))
        result, = helper.run_calls(f, helper.human(f), ('finish_work', {
            'summary': 'Updated delivery', 'verification': 'Reviewed updated scope', 'artifact_seqs': [artifact_seq]}))
        self.assertEqual(result['status'], 'ok', result)
        self.assertEqual(len(f.events('team.review_recorded')), 2)
        self.assertEqual(sorted(r['status'] for r in f.rt.team_reviews.view(f.task)), ['approved', 'stale'])
        self.assertEqual(len(f.events('team.objective_completed')), 1)

    def test_status_command_does_not_invalidate_formal_review(self):
        helper = fixtures.ReviewSupervisionCandidateTests(methodName='runTest')
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        f, run = helper.ready()
        before = f.rt.work.scope(f.task)['direction_seq']
        command = f.rt.command(f.task, '现在进度如何？')
        self.assertEqual(command.payload['intent'], 'status')
        self.assertEqual(f.rt.work.scope(f.task)['direction_seq'], before)
        candidate = create(f.rt.team, f.task, run_seq=run.seq, run_hash=run.hash)
        self.assertIsNotNone(candidate)

    def test_answered_short_consultation_before_report_run_is_allowed(self):
        helper, f = self.setup_review()
        before = f.rt.work.scope(f.task)['direction_seq']
        question = f.rt.team.receive(f.task, '这个网页怎么使用？', request_id='short-consultation')
        helper.run_calls(f, question, ('reply_to_customer', {'text': '复核者检查提交并登记意见。'}), statuses=('completed',))
        self.assertEqual(f.rt.work.scope(f.task)['direction_seq'], before)
        claimed = f.rt.team._claim(f.task)
        self.assertEqual(claimed[0].payload.get('kind'), 'report')
        f.driver.scripts[f.manager] = [lambda run: 'Review acknowledged.']
        list(f.rt.team._run_message(f.task, *claimed))
        self.assertIsNotNone(create(f.rt.team, f.task, run_seq=claimed[2].seq, run_hash=claimed[2].hash))

    def test_mixed_customer_message_is_exposed_in_full_without_semantic_guess(self):
        helper, f = self.setup_review()
        question = f.rt.team.receive(f.task, '这个网页怎么使用？另外修改目标。', request_id='mixed-consultation')
        helper.run_calls(f, question, ('reply_to_customer', {'text': '已收到。'}), statuses=('completed',))
        claimed = f.rt.team._claim(f.task)
        f.driver.scripts[f.manager] = [lambda run: 'Review acknowledged.']
        list(f.rt.team._run_message(f.task, *claimed))
        candidate = create(f.rt.team, f.task, run_seq=claimed[2].seq, run_hash=claimed[2].hash)
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.payload['customer_inputs'][0]['seq'], question.seq)
        context = f.rt.team._context(f.task, candidate, f.rt.organization.leader)
        sources = context['review_customer_inputs']
        self.assertEqual(sources['entries'][0]['text'], question.payload['text'])
        self.assertIn('不证明没有新要求', sources['note'])

    def test_cross_role_new_requirement_is_bound_and_visible_to_supervision(self):
        helper, f = self.setup_review()
        worker = f.rt.team._role(f.worker)
        question = f.rt.team.receive(f.task, f'@{worker.name} 另外需要支持离线模式。', request_id='cross-role-new-requirement')
        helper.run_calls(f, question, ('reply_to_customer', {'text': '已收到。'}), statuses=('completed',))
        claimed = f.rt.team._claim(f.task)
        f.driver.scripts[f.manager] = [lambda run: 'Review acknowledged.']
        list(f.rt.team._run_message(f.task, *claimed))
        candidate = create(f.rt.team, f.task, run_seq=claimed[2].seq, run_hash=claimed[2].hash)
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.payload['customer_inputs'], [{'seq': question.seq, 'hash': question.hash, 'recipient': f.worker}])
        context = f.rt.team._context(f.task, candidate, f.rt.organization.leader)
        self.assertEqual(context['review_customer_inputs']['entries'][0]['text'], question.payload['text'])

    def test_long_answered_customer_input_is_not_a_keyword_or_length_gate(self):
        helper, f = self.setup_review()
        question = f.rt.team.receive(f.task, '请核对：' + '甲' * 5000, request_id='long-consultation')
        helper.run_calls(f, question, ('reply_to_customer', {'text': '已收到。'}), statuses=('completed',))
        claimed = f.rt.team._claim(f.task)
        f.driver.scripts[f.manager] = [lambda run: 'Review acknowledged.']
        list(f.rt.team._run_message(f.task, *claimed))
        candidate = create(f.rt.team, f.task, run_seq=claimed[2].seq, run_hash=claimed[2].hash)
        self.assertIsNotNone(candidate)
        context = f.rt.team._context(f.task, candidate, f.rt.organization.leader)
        self.assertEqual(context['review_customer_inputs']['entries'][0]['text'], question.payload['text'])

    def test_unhandled_cross_role_input_does_not_authorize_old_report(self):
        helper, f = self.setup_review()
        report = next(e for e in f.events('team.message_received') if e.payload.get('kind') == 'report' and e.payload.get('review_seq'))
        worker = f.rt.team._role(f.worker)
        f.rt.team.receive(f.task, f'@{worker.name} 请增加离线要求。', request_id='unhandled-cross-role')
        started = helper.start(f, report)
        f.driver.scripts[f.manager] = [lambda run: 'Must not run']
        list(f.rt.team._run_message(f.task, report, f.rt.team._role(f.manager), started))
        finished, = [e for e in f.events('team.run_finished') if e.payload.get('run_id') == started.payload['run_id']]
        self.assertEqual(finished.payload['status'], 'interrupted')
        self.assertIsNone(create(f.rt.team, f.task, run_seq=started.seq, run_hash=started.hash))

    def test_completed_plain_status_reply_does_not_permanently_block_report(self):
        helper, f = self.setup_review()
        question = f.rt.team.receive(f.task, '现在进度怎么样？', request_id='plain-status')
        started = helper.start(f, question)
        f.driver.scripts[f.manager] = [lambda run: '正在整理复核结果。']
        list(f.rt.team._run_message(f.task, question, f.rt.team._role(f.manager), started))
        claimed = f.rt.team._claim(f.task)
        self.assertEqual(claimed[0].payload.get('kind'), 'report')
        f.driver.scripts[f.manager] = [lambda run: 'Review acknowledged.']
        list(f.rt.team._run_message(f.task, *claimed))
        self.assertIsNotNone(create(f.rt.team, f.task, run_seq=claimed[2].seq, run_hash=claimed[2].hash))

    def test_untrusted_direction_event_does_not_authorize_supervision(self):
        helper = fixtures.ReviewSupervisionCandidateTests(methodName='runTest')
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        f, run = helper.ready()
        f.rt.log.emit(type='human.commanded', task_id=f.task, actor=Actor.system('fixture'),
                      payload={'goal': 'Untrusted goal', 'instruction': 'fixture'})
        self.assertIsNone(create(f.rt.team, f.task, run_seq=run.seq, run_hash=run.hash))


if __name__ == '__main__':
    unittest.main()
