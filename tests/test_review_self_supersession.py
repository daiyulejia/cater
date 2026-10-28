"""Offline external handoff settlement and concurrent invalidation regressions."""
from dataclasses import replace
import tempfile
import unittest
from unittest.mock import PropertyMock, patch

from cater.external_agent import ExternalYield, QoderAcpDriver
from cater.external_scratch import LaunchDirectory
from cater.team import HOST
from cater.tool_protocol import ToolCall
from cater.work import WorkConflict
from test_team_external import ScriptedExternalDriver
import test_team_review_cycle as cycle


class ReviewSelfSupersessionTests(unittest.TestCase):
    def setUp(self):
        self.f = cycle.TeamReviewCycleTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.rt, self.task = self.f.rt, self.f.task
        self.worker = self.rt.team._role(self.f.worker)
        artifact = self.f.publish('version one\n')
        delegated, = self.f.exchange(self.f.manager, ('delegate', {
            'recipient': self.f.worker, 'instruction': 'Publish, review, and return.'}))
        self.parent = self.message(delegated['message_seq'])
        requested, = self.f.exchange(self.f.worker, ('request_review', {
            'reviewer': self.f.reviewer, 'artifact_seqs': [artifact],
            'instruction': 'Review exact version.'}), source=self.parent)
        self.old = requested

    def message(self, seq):
        return next(e for e in self.rt.team._messages(self.task) if e.seq == seq)

    def interrupted_feedback(self):
        reviewer = self.rt.team._role(self.f.reviewer)
        source = self.message(self.old['message_seq'])
        started = self.rt.log.emit(type='team.run_started', actor=HOST, task_id=self.task,
            payload={'message_seq': source.seq, 'role': reviewer.id,
                     'run_id': 'interrupted-review', 'revision': self.rt.team._revision(self.task),
                     'control_seq': self.rt.execution_control.view(self.task)['seq']})
        driver = ScriptedExternalDriver()
        def interrupt(run):
            raise WorkConflict('Reviewer interrupted before a verdict.')
        driver.scripts[reviewer.id] = [interrupt]
        with patch.dict(self.rt.drivers, {reviewer.driver: driver}), \
                patch('subprocess.Popen', side_effect=AssertionError('No processes allowed')):
            list(self.rt.team._run_message(self.task, source, reviewer, started))
        self.assertEqual(driver.errors, [])
        feedback = next(e for e in reversed(self.rt.team._messages(self.task))
                        if e.payload.get('kind') == 'progress' and e.payload.get('reply_to') == source.seq)
        self.assertNotIn('review_seq', feedback.payload)
        return feedback

    def run_resubmission(self, *, verdict='approved', text='Revised and submitted.', race=None, feedback=None):
        if feedback is None:
            decision = self.f.record(self.old['review_seq'], verdict=verdict,
                                     source=self.message(self.old['message_seq']))
            feedback = self.message(decision['message_seq'])
        started = self.rt.log.emit(type='team.run_started', actor=HOST, task_id=self.task,
            payload={'message_seq': feedback.seq, 'role': self.worker.id,
                     'run_id': 'self-resubmission', 'revision': self.rt.team._revision(self.task),
                     'control_seq': self.rt.execution_control.view(self.task)['seq']})
        driver = ScriptedExternalDriver()
        def script(run):
            run.state = replace(run.state, text=text)
            new = run.call('publish_artifact', filename='result.txt', content='version two\n',
                           summary='Revised', format='text')
            self.assertEqual(new['status'], 'ok')
            run.call('request_review', reviewer=self.f.reviewer, artifact_seqs=[new['artifact_seq']],
                     instruction='Review revised version.', review_seq=self.old['review_seq'])
            self.fail('A successful handoff must yield before another model action')
        driver.scripts[self.worker.id] = [script]
        original_close = LaunchDirectory.close
        def close(scratch, **kwargs):
            result = original_close(scratch, **kwargs)
            if race:
                # Cleanup is complete; inject a durable external change before final publication.
                with patch.dict(self.rt.drivers, {self.worker.driver: self.f.driver}):
                    race()
            return result
        with patch.dict(self.rt.drivers, {self.worker.driver: driver}), \
                patch.object(LaunchDirectory, 'close', close), \
                patch('subprocess.Popen', side_effect=AssertionError('No processes allowed')):
            list(self.rt.team._run_message(self.task, feedback, self.worker, started))
        self.assertEqual(driver.errors, [])
        self.assertEqual(len(driver.runs), 1)
        self.assertTrue(driver.runs[0].revoked)
        ext = next(e for e in self.f.events('team.external_run_finished')
                   if e.payload['run_id'] == 'self-resubmission')
        self.assertEqual(ext.payload['outcome'], 'yielded')
        self.assertTrue(ext.payload['cleanup_complete'])
        result = next(e for e in self.f.events('team.tool_finished')
                      if e.payload['run_id'] == 'self-resubmission' and e.payload['name'] == 'request_review')
        self.assertEqual(result.payload['result']['status'], 'ok')
        self.assertIn(feedback.seq, self.rt.team._inactive_messages(self.task))
        # Successful settlement must never grant another tool admission on the old input.
        with self.assertRaises(WorkConflict):
            self.rt.team._current(self.task, started, self.worker, started.payload['revision'])
        self.assertTrue(self.rt.verify_chain().ok)
        self.started, self.feedback, self.result = started, feedback, result.payload['result']
        return next(e for e in self.f.events('team.run_finished')
                    if e.payload['run_id'] == 'self-resubmission')

    def assert_waiting_without_false_progress(self, **kwargs):
        finished = self.run_resubmission(**kwargs)
        self.assertEqual(finished.payload['status'], 'waiting', finished.payload)
        self.assertEqual(finished.payload['reason'], '')
        self.assertFalse([e for e in self.rt.team._messages(self.task)
                          if e.payload.get('kind') == 'progress'
                          and e.payload.get('reply_to') == self.parent.seq])
        self.assertEqual(self.rt.team_reviews.view(self.task)[0]['status'], 'pending')
        self.assertNotIn(self.parent.seq, self.rt.team._inactive_messages(self.task))
        self.assertNotIn(self.result['message_seq'], self.rt.team._inactive_messages(self.task))

    def test_approved_feedback_with_public_text(self):
        self.assert_waiting_without_false_progress()

    def test_changes_requested_feedback_with_public_text(self):
        self.assert_waiting_without_false_progress(verdict='changes_requested')

    def test_empty_text_control(self):
        self.assert_waiting_without_false_progress(text='')

    def test_interrupted_review_progress_with_public_text(self):
        self.assert_waiting_without_false_progress(feedback=self.interrupted_feedback())

    def test_interrupted_review_progress_still_respects_parent_withdrawal(self):
        def race():
            result, = self.f.exchange(self.f.manager, ('withdraw_work', {
                'message_seq': self.parent.seq, 'reason': 'External cancellation.'}))
            self.assertEqual(result['status'], 'ok')
        finished = self.run_resubmission(feedback=self.interrupted_feedback(), race=race)
        self.assertEqual(finished.payload['status'], 'interrupted')

    def test_external_parent_withdrawal_during_cleanup_still_interrupts(self):
        def race():
            result, = self.f.exchange(self.f.manager, ('withdraw_work', {
                'message_seq': self.parent.seq, 'reason': 'External cancellation.'}))
            self.assertEqual(result['status'], 'ok')
        self.assertEqual(self.run_resubmission(race=race).payload['status'], 'interrupted')

    def test_external_objective_change_during_cleanup_still_interrupts(self):
        def race():
            result, = self.f.exchange(self.f.manager, ('set_objective', {'goal': 'Changed objective.'}))
            self.assertEqual(result['status'], 'ok')
        self.assertEqual(self.run_resubmission(race=race).payload['status'], 'interrupted')

    def test_another_run_replacement_during_cleanup_still_interrupts(self):
        def race():
            current = self.rt.team_reviews.view(self.task)[0]
            # Use an agent assignment, not a newer human input that could mask
            # a broken supersession guard with an unrelated interruption.
            source = self.rt.team._send(self.task, self.rt.team._role(self.f.manager),
                self.f.worker, 'Concurrent revision.', 'assignment')
            published, = self.f.exchange(self.f.worker, ('publish_artifact', {
                'filename': 'result.txt', 'content': 'version three\n',
                'summary': 'Concurrent version', 'format': 'text'}), source=source)
            # exchange starts a new host run; the fixture must not treat it
            # as another model turn in the preceding run's native transcript.
            self.f.native_exchanges.pop((self.f.worker, source.seq))
            result, = self.f.exchange(self.f.worker, ('request_review', {
                'reviewer': self.f.reviewer, 'artifact_seqs': [published['artifact_seq']],
                'instruction': 'Concurrent review.', 'review_seq': current['request_seq']}), source=source)
            self.assertEqual(result['status'], 'ok')
        finished = self.run_resubmission(race=race)
        self.assertEqual(finished.payload['status'], 'interrupted')
        self.assertIn('同链新版复核替代', finished.payload['reason'])

    def test_pause_during_cleanup_still_interrupts(self):
        finished = self.run_resubmission(race=lambda: self.rt.execution_control.change(
            self.task, 'pause', request_id='self-review-pause'))
        self.assertEqual(finished.payload['status'], 'interrupted')
        self.assertIn('执行已暂停', finished.payload['reason'])

    def test_new_human_input_during_cleanup_still_interrupts(self):
        finished = self.run_resubmission(race=lambda: self.rt.team.receive(
            self.task, f'@{self.worker.name} New instructions.'))
        self.assertEqual(finished.payload['status'], 'interrupted')
        self.assertIn('收到顾客的新消息', finished.payload['reason'])

    def test_settlement_requires_own_run_and_success_receipt(self):
        self.assert_waiting_without_false_progress()
        seq = self.result['review_seq']
        other_run = replace(self.started, payload={**self.started.payload, 'run_id': 'unrelated-run'})
        with self.assertRaises(WorkConflict):
            self.rt.team._current(self.task, other_run, self.worker, self.started.payload['revision'],
                                  settled_review_seq=seq)
        events = [e for e in self.rt.log.read(task_id=self.task)
                  if not (e.type == 'team.tool_finished' and e.payload.get('run_id') == 'self-resubmission'
                          and e.payload.get('name') == 'request_review')]
        with patch.object(self.rt.log, 'read', return_value=events), self.assertRaises(WorkConflict):
            self.rt.team._current(self.task, self.started, self.worker, self.started.payload['revision'],
                                  settled_review_seq=seq)

    def assert_cleanup_failure_after_accepted_tail(self, handoff, *, race=None):
        decision = self.f.record(self.old['review_seq'], verdict='changes_requested',
                                 source=self.message(self.old['message_seq']))
        feedback = self.message(decision['message_seq'])
        started = self.rt.log.emit(type='team.run_started', actor=HOST, task_id=self.task,
            payload={'message_seq': feedback.seq, 'role': self.worker.id,
                     'run_id': 'self-resubmission-cleanup', 'revision': self.rt.team._revision(self.task),
                     'control_seq': self.rt.execution_control.view(self.task)['seq']})
        driver = QoderAcpDriver(self.worker.driver, 'never-launch-qoder', 'OfflineModel')
        before, tail = 'Inspecting the revised version.\n', 'Accepted after the new review receipt.\n'
        cleanup_error = OSError('Injected unconfirmed client cleanup')
        runs, receipts, lifecycle = [], [], []
        factory = driver.agent_run
        owner = self

        def capture(**kwargs):
            run = factory(**kwargs)
            runs.append(run)
            return run

        class Session:
            def __init__(self, command, **kwargs):
                self.session_id = 'offline-self-review-cleanup'
                self.agent_info = {'name': 'qoder-cli-cn'}

            def iter_open(self, **kwargs):
                yield None
                return {'models': {'currentModelId': 'offline', 'availableModels': [
                    {'modelId': 'offline', 'name': driver.model}]}}

            def iter_prompt(self, prompt, **kwargs):
                yield None
                run, = runs
                update = kwargs['on_update']
                update({'sessionUpdate': 'agent_message_chunk',
                        'content': {'type': 'text', 'text': before}})
                published = run._call(ToolCall('publish-revision', 'publish_artifact', {
                    'filename': 'result.txt', 'content': 'version two\n',
                    'summary': 'Revised', 'format': 'text'}))
                owner.assertEqual(published['status'], 'ok')
                source = {'artifact_seqs': [published['artifact_seq']],
                          'review_seq': owner.old['review_seq']}
                arguments = {'instruction': 'Review revised version.'}
                arguments.update({'recipient': owner.f.reviewer, 'review': source}
                                 if handoff == 'delegate' else {'reviewer': owner.f.reviewer, **source})
                receipt = run._call(ToolCall('resubmit-review', handoff, arguments))
                receipts.append(receipt)
                owner.assertEqual(receipt['status'], 'ok')
                owner.assertTrue(run._tools_revoked)
                owner.assertIn(feedback.seq, owner.rt.team._inactive_messages(owner.task))
                # Real adapter acceptance while the successful tool receipt is
                # still draining; do not assign state.text or bypass _current.
                with patch.object(type(run._bridge), 'has_pending_output',
                                  new_callable=PropertyMock, return_value=True):
                    update({'sessionUpdate': 'agent_message_chunk',
                            'content': {'type': 'text', 'text': tail}})
                owner.assertEqual(run.state.text, before + tail)
                owner.assertIsInstance(run._deferred_yield, ExternalYield)
                kwargs['service']()
                owner.fail('The committed review handoff must yield after its receipt drains')

            def cancel(self):
                lifecycle.append('cancel')

            def close(self):
                lifecycle.append('close')
                if race == 'pause':
                    owner.rt.execution_control.change(owner.task, 'pause', request_id='cleanup-tail-pause')
                elif race == 'customer':
                    owner.rt.team.receive(owner.task, f'@{owner.worker.name} New instructions.')
                raise cleanup_error

        mkdtemp = tempfile.mkdtemp
        with patch.dict(self.rt.drivers, {self.worker.driver: driver}), \
                patch.object(driver, 'agent_run', side_effect=capture), \
                patch('cater.external_agent.AcpSession', Session), \
                patch('cater.external_scratch.tempfile.mkdtemp',
                      side_effect=lambda **kw: mkdtemp(dir=self.f.root, **kw)), \
                patch('subprocess.Popen', side_effect=AssertionError('No processes allowed')) as process:
            list(self.rt.team._run_message(self.task, feedback, self.worker, started))
            process.assert_not_called()

        run, = runs
        receipt, = receipts
        self.assertEqual(lifecycle, ['cancel', 'close'])
        self.assertEqual(run.state.text, before + tail)
        self.assertEqual(run.state.cleanup_failures, (cleanup_error,))
        self.assertFalse(run.state.cleanup_complete)
        self.assertTrue(run._bridge._closed)
        events = [e for e in self.rt.log.read(task_id=self.task)
                  if e.payload.get('run_id') == started.payload['run_id']]
        finished, = [e for e in events if e.type == 'team.run_finished']
        external, = [e for e in events if e.type == 'team.external_run_finished']
        handoff_receipt, = [e for e in events if e.type == 'team.tool_finished'
                           and e.payload['name'] == handoff]
        self.assertEqual(finished.payload['status'], 'interrupted')
        self.assertEqual(finished.payload['failure_stage'], run.state.failure_phase or 'cleanup')
        self.assertEqual(finished.payload['external_observations'], dict(run.state.observations))
        self.assertEqual(external.payload['outcome'], 'yielded')
        self.assertFalse(external.payload['cleanup_complete'])
        pending, = self.rt.team.external_runs.pending(self.task)
        self.assertEqual(pending['started_seq'], external.payload['started_seq'])
        self.assertEqual(pending['outcome'], 'unknown')
        self.assertTrue(pending['run_ended'])
        self.assertFalse(pending['replay_authorized'])
        with self.assertRaises(WorkConflict):
            self.rt.team._current(self.task, started, self.worker, started.payload['revision'])
        if race is None:
            self.rt.team._current(self.task, started, self.worker, started.payload['revision'],
                                  settled_review_seq=receipt['review_seq'])
        else:
            with self.assertRaises(WorkConflict):
                self.rt.team._current(self.task, started, self.worker, started.payload['revision'],
                                      settled_review_seq=receipt['review_seq'])
        said = [e for e in events if e.type == 'msg.said']
        self.assertEqual([e.payload['text'] for e in said], [before, tail] if race is None else [before])
        for event in said:
            self.assertEqual(event.payload['response_kind'], 'progress')
            self.assertEqual(event.payload['audience'], 'team')
            self.assertEqual(event.payload['reply_to'], feedback.seq)
            self.assertEqual(event.payload['revision'], started.payload['revision'])
            self.assertNotIn('delivery_seq', event.payload)
        self.assertLess(said[0].seq, handoff_receipt.seq)
        if race is None:
            self.assertLess(external.seq, said[1].seq)
            self.assertLess(said[1].seq, finished.seq)
        expected_reason = {'pause': '执行已暂停', 'customer': '收到顾客的新消息'}
        self.assertIn(expected_reason.get(race, '尚未确认清理完成'), finished.payload['reason'])
        self.assertFalse([e for e in events if e.type in {'team.review_recorded', 'team.objective_completed'}])
        self.assertEqual(self.rt.team_reviews.view(self.task)[0]['status'], 'pending')
        self.assertTrue(self.rt.verify_chain().ok)

    def test_request_review_cleanup_failure_preserves_accepted_tail_and_pending(self):
        self.assert_cleanup_failure_after_accepted_tail('request_review')

    def test_delegate_review_cleanup_failure_preserves_accepted_tail_and_pending(self):
        self.assert_cleanup_failure_after_accepted_tail('delegate')

    def test_request_review_cleanup_failure_still_respects_pause(self):
        self.assert_cleanup_failure_after_accepted_tail('request_review', race='pause')

    def test_delegate_review_cleanup_failure_still_respects_pause(self):
        self.assert_cleanup_failure_after_accepted_tail('delegate', race='pause')

    def test_request_review_cleanup_failure_still_respects_new_customer(self):
        self.assert_cleanup_failure_after_accepted_tail('request_review', race='customer')

    def test_delegate_review_cleanup_failure_still_respects_new_customer(self):
        self.assert_cleanup_failure_after_accepted_tail('delegate', race='customer')


if __name__ == '__main__':
    unittest.main(verbosity=2)
