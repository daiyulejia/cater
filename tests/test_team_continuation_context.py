"""Supervisors see the current attempt of the original delegated job."""
from __future__ import annotations

import io
import json
from pathlib import Path
import unittest
import zipfile

from cater.team import HOST
import test_simple_team as fixture
import test_team_supervision as native_fixture


class ContinuationContextTests(unittest.TestCase):
    setUp = native_fixture.TeamSupervisionTests.setUp
    events = fixture.SimpleTeamTests.events
    native_turn = native_fixture.TeamSupervisionTests.native_turn

    def job(self, *, kind='assignment', sender=None, recipient=None):
        sender = sender or self.rt.organization.leader
        recipient = recipient or self.worker
        source = self.rt.team._send(self.task, sender, recipient, '核对已有材料后继续原工作', kind)
        run = self.start(source)
        self.end(run, 'failed')
        return source, sender

    def start(self, source):
        return self.rt.log.emit(type='team.run_started', task_id=self.task, actor=HOST, payload={
            'message_seq': source.seq, 'role': source.payload['recipient'], 'run_id': f'run-{source.seq}',
            'revision': self.rt.team.view(self.task)['revision'],
            'control_seq': self.rt.execution_control.view(self.task)['seq']})

    def end(self, run, status):
        self.rt.log.emit(type='team.run_finished', task_id=self.task, actor=HOST,
                         payload={**run.payload, 'status': status, 'reason': '离线测试的运行终态'})

    def context(self, sender):
        reader = self.rt.team._role(self.worker if sender.id != self.worker else self.manager)
        notice = self.rt.team._send(self.task, reader, sender.id, '查看当前委托事实', 'notification')
        return self.rt.team._context(self.task, notice, sender)

    def resume(self, source, sender, *, actor=None, recipient=None):
        return self.rt.team._send(self.task, actor or sender, recipient or source.payload['recipient'],
                                 '先核对现状再接续，不重做已完成的操作', 'continuation', continue_assignment_seq=source.seq)

    def test_queued_continuation_replaces_old_failure_in_outgoing_work(self):
        source, sender = self.job()
        continuation = self.resume(source, sender)
        context = self.context(sender)
        row, = context['outgoing_work']
        checkpoint, = context['work_checkpoint']['open_work']
        self.assertEqual(row['status'], checkpoint['status'])
        self.assertEqual(row['status'], 'queued')
        self.assertEqual(row['latest_message_seq'], continuation.seq)
        self.assertTrue(row['requires_return'])
        self.assertEqual(row['seq'], source.seq)
        self.assertEqual(row['text'], source.payload['text'])
        old, = context['current_work']['runs']
        self.assertEqual(old['status'], 'failed')

    def test_latest_attempt_status_is_shared_for_running_waiting_and_terminal_states(self):
        source, sender = self.job(kind='message')
        continuation = self.resume(source, sender)
        run = self.start(continuation)
        self.assertEqual(self.context(sender)['outgoing_work'][0]['status'], 'running')
        for status in ('waiting', 'interrupted', 'completed'):
            source, sender = self.job(kind='message')
            continuation = self.resume(source, sender)
            self.end(self.start(continuation), status)
            with self.subTest(status=status):
                context = self.context(sender)
                row = next(r for r in context['outgoing_work'] if r['seq'] == source.seq)
                self.assertEqual(row['status'], status)
                self.assertEqual(row['latest_message_seq'], continuation.seq)
                self.assertTrue(row['requires_return'])

    def test_multiple_continuations_keep_the_original_job_and_latest_attempt(self):
        source, sender = self.job()
        first = self.resume(source, sender)
        self.end(self.start(first), 'interrupted')
        latest = self.resume(source, sender)
        self.start(latest)
        row, = self.context(sender)['outgoing_work']
        self.assertEqual((row['seq'], row['status'], row['latest_message_seq']), (source.seq, 'running', latest.seq))
        self.assertEqual(len([e for e in self.rt.team._messages(self.task) if e.payload['kind'] == 'assignment']), 1)

    def test_worker_supervisor_gets_the_same_projection_as_the_manager(self):
        sender = self.rt.team._role(self.worker)
        recipient = next(r.id for r in self.rt.worker_roles if r.id != sender.id)
        source, _ = self.job(sender=sender, recipient=recipient)
        continuation = self.resume(source, sender)
        self.start(continuation)
        self.assertEqual(self.context(sender)['outgoing_work'][0]['status'], 'running')

    def test_jobs_outside_the_short_checkpoint_are_still_updated(self):
        jobs = [self.job() for _ in range(25)]
        source, sender = jobs[0]
        continuation = self.resume(source, sender)
        self.start(continuation)
        context = self.context(sender)
        self.assertTrue(context['work_checkpoint']['open_work_truncated'])
        self.assertNotIn(source.seq, [r['seq'] for r in context['work_checkpoint']['open_work']])
        row = next(r for r in context['outgoing_work'] if r['seq'] == source.seq)
        self.assertEqual(row['status'], 'running')

    def test_returning_original_work_removes_it_instead_of_creating_a_new_job(self):
        source, sender = self.job()
        self.start(self.resume(source, sender))
        self.rt.team._send(self.task, self.rt.team._role(self.worker), sender.id,
                           '原工作已交回，未做额外验证', 'report', reply_to=source.seq)
        context = self.context(sender)
        self.assertEqual(context['outgoing_work'], [])
        self.assertEqual(context['work_checkpoint']['open_work_count'], 0)

    def test_wrong_sender_continuation_does_not_replace_original_failure(self):
        source, sender = self.job()
        other = next(r for r in self.rt.roles if r.id not in {sender.id, self.worker})
        forged = self.resume(source, sender, actor=other)
        self.start(forged)
        row, = self.context(sender)['outgoing_work']
        self.assertEqual(row['status'], 'failed')
        self.assertNotIn('latest_message_seq', row)

    def test_wrong_recipient_continuation_does_not_replace_original_failure(self):
        source, sender = self.job()
        other = next(r.id for r in self.rt.worker_roles if r.id != self.worker)
        self.start(self.resume(source, sender, recipient=other))
        row, = self.context(sender)['outgoing_work']
        self.assertEqual(row['status'], 'failed')
        self.assertNotIn('latest_message_seq', row)

    def test_new_goal_does_not_inherit_old_continuation_state(self):
        source, sender = self.job()
        self.start(self.resume(source, sender))
        self.rt.log.emit(type='team.objective_updated', actor=HOST, task_id=self.task,
                         payload={'goal': '改为讨论另一项独立目标'})
        context = self.context(sender)
        self.assertEqual(context['outgoing_work'], [])
        self.assertEqual(context['work_checkpoint']['open_work_count'], 0)

    def test_plain_queued_job_needs_no_extra_attempt_metadata(self):
        sender = self.rt.organization.leader
        source = self.rt.team._send(self.task, sender, self.worker, '普通新委托', 'assignment')
        row, = self.context(sender)['outgoing_work']
        self.assertEqual((row['seq'], row['status']), (source.seq, 'queued'))
        self.assertNotIn('latest_message_seq', row)
        self.assertNotIn('requires_return', row)

    def test_native_supervisor_reads_queued_and_running_then_delivers_returned_bytes(self):
        source, sender = self.job()
        continuation = self.resume(source, sender)
        observed = []
        def queued(context, history, tools):
            row, = context['outgoing_work']
            self.assertEqual(row['status'], 'queued')
            self.assertEqual(row['latest_message_seq'], continuation.seq)
            observed.append(row['status'])
            return self.native_turn('', ('read_checkpoint', {}))
        def running(context, history, tools):
            row, = context['outgoing_work']
            self.assertEqual(row['status'], 'running')
            self.assertEqual(row['latest_message_seq'], continuation.seq)
            observed.append(row['status'])
            return self.native_turn('', ('wait_for_reply', {}))
        self.driver.steps = {self.manager: [queued, running,
            self.native_turn('', ('finish_work', {'summary': '原委托的说明已交付', 'verification': '只核对文件正文'})),
            self.native_turn('说明已交回，转换执行仍未验证。')], self.worker: [
            self.native_turn('', ('publish_artifact', {'filename': 'guide.md', 'content': '# 接续结果\n未执行转换。',
                'summary': '实际保存的说明', 'format': 'markdown'})), self.native_turn('原委托已完成，说明已保存。')]}
        progress = self.rt.team.receive(self.task, '现在原工作进行到哪里了', request_id='continuation-context-native')
        message, role, started = self.rt.team._claim(self.task)
        self.assertEqual((message.seq, role.id), (progress.seq, self.manager))
        manager_run = self.rt.team._run_message(self.task, message, role, started)
        self.addCleanup(manager_run.close)
        # Advance actual native runs explicitly; scheduler wall time cannot pick these stages.
        next(manager_run)
        self.assertEqual(observed, ['queued'])
        message, role, started = self.rt.team._claim(self.task)
        self.assertEqual((message.seq, role.id), (continuation.seq, self.worker))
        worker_run = self.rt.team._run_message(self.task, message, role, started)
        self.addCleanup(worker_run.close)
        next(worker_run)
        with self.assertRaises(StopIteration):
            next(manager_run)
        self.assertEqual(observed, ['queued', 'running'])
        list(worker_run)
        resumed, = [e for e in self.events('team.run_finished')
                    if e.payload['message_seq'] == continuation.seq]
        self.assertEqual(resumed.payload['status'], 'completed')
        returned, = [e for e in self.events('team.message_received')
                     if e.actor.id == self.worker and e.payload.get('kind') == 'report'
                     and e.payload.get('reply_to') == source.seq]
        self.rt.collaborate(self.task)
        self.assertEqual(self.rt.team.view(self.task)['status'], 'done')
        artifact, = self.rt.task(self.task).deliverables
        delivery, = self.events('team.objective_completed')
        self.assertLess(resumed.seq, returned.seq)
        self.assertLess(returned.seq, delivery.seq)
        self.assertEqual(delivery.payload['artifacts'][0]['seq'], artifact['seq'])
        body = Path(artifact['local_path']).read_bytes()
        with zipfile.ZipFile(io.BytesIO(self.rt.team.archive(self.task))) as archive:
            self.assertEqual(archive.read('guide.md'), body)
        self.assertEqual(body, '# 接续结果\n未执行转换。'.encode())
        self.assertEqual(self.rt.team.recovery.assignments(self.task), [])
        self.assertEqual(self.protocol_errors, [])
        self.assertTrue(self.rt.verify_chain().ok)

    def test_recovered_original_job_survives_two_reviews_and_customer_progress_query(self):
        source, sender = self.job()
        continuation = self.resume(source, sender)
        reviewer = next(r.id for r in self.rt.worker_roles if r.id != self.worker)
        versions = ['只说明了负数，零值遗漏。', '补上零值，运行边界仍遗漏。', '负数与零值均说明，明确尚未运行转换。']
        author_returns = []

        def author(context, history, tools):
            self.assertEqual(context['active_assignment']['seq'], source.seq)
            rows = context['reviews']
            if history:
                artifact = json.loads(history[-1]['content'])
                arguments = {'reviewer': reviewer, 'artifact_seqs': [artifact['artifact_seq']],
                             'instruction': '检查负数、零值说明以及未运行的边界，核对实际正文。'}
                if rows:
                    arguments['review_seq'] = rows[0]['request_seq']
                return self.native_turn('', ('request_review', arguments))
            if rows and rows[0]['status'] == 'approved':
                author_returns.append(context['active_assignment']['seq'])
                return self.native_turn('', ('send_message', {'recipient': self.manager, 'reply_to': source.seq,
                    'text': '最终说明已通过原复核者核对；尚未执行转换。'}), ('wait_for_reply', {}))
            index = len(self.rt.task(self.task).deliverables)
            return self.native_turn('', ('publish_artifact', {'filename': 'guide.md', 'content': versions[index],
                'summary': f'第{index + 1}版说明', 'format': 'markdown'}))

        def review(context, history, tools):
            row, = context['reviews']
            if not history:
                if len(self.rt.task(self.task).deliverables) == 1:
                    self.rt.team.receive(self.task, '目前进展怎么样', request_id='recovered-review-progress-query')
                return self.native_turn('', ('read_artifact', {'artifact_seq': row['artifacts'][0]['seq']}))
            actual = json.loads(history[-1]['content'])
            index = versions.index(actual['content'])
            return self.native_turn('', ('record_review', {'review_seq': row['request_seq'],
                'verdict': 'approved' if index == 2 else 'changes_requested',
                'feedback': ['补上零值说明。', '补上尚未运行的边界。', '已核对当前正文，边界完整。'][index]}))

        def manager(context, history, tools):
            if history:
                return self.native_turn('', ('wait_for_reply', {}))
            if context['message']['kind'] == 'report':
                self.assertEqual(context['message']['reply_to'], source.seq)
            else:
                row, = context['outgoing_work']
                self.assertEqual(row['status'], 'waiting')
                self.assertEqual(row['latest_message_seq'], continuation.seq)
                self.assertTrue(row['requires_return'])
            return self.native_turn('', ('finish_work', {'summary': '当前最终说明已交付',
                'verification': '复核了正文与明确边界，未执行转换'}))

        self.driver.steps = {self.manager: [manager] * 8, self.worker: [author] * 8, reviewer: [review] * 6}
        self.rt.collaborate(self.task)
        self.assertEqual(self.protocol_errors, [])
        self.assertEqual(author_returns, [source.seq])
        decisions = self.events('team.review_recorded')
        self.assertEqual([e.payload['verdict'] for e in decisions], ['changes_requested', 'changes_requested', 'approved'])
        delivery, = self.events('team.objective_completed')
        final = self.rt.task(self.task).deliverables[-1]
        review, = self.rt.team_reviews.view(self.task)
        self.assertEqual(delivery.payload['artifacts'], review['artifacts'])
        self.assertEqual(delivery.payload['artifacts'][0]['seq'], final['seq'])
        returns = [e for e in self.events('team.message_received') if e.payload.get('kind') == 'report'
                   and e.actor.id == self.worker and e.payload.get('reply_to') == source.seq]
        self.assertEqual(len(returns), 1)
        self.assertLess(decisions[-1].seq, returns[0].seq)
        self.assertLess(returns[0].seq, delivery.seq)
        attempts = [e.payload['result'] for e in self.events('team.tool_finished') if e.payload['name'] == 'finish_work']
        self.assertEqual([r.get('code') for r in attempts], ['work_pending', None])
        self.assertEqual(self.rt.team.view(self.task)['status'], 'done')
        self.assertEqual(self.rt.team.recovery.assignments(self.task), [])
        with zipfile.ZipFile(io.BytesIO(self.rt.team.archive(self.task))) as archive:
            self.assertEqual(archive.read('guide.md'), versions[-1].encode())
        self.assertEqual(Path(final['local_path']).read_bytes(), versions[-1].encode())
        self.assertEqual(len([e for e in self.rt.team._messages(self.task)
                              if e.actor.id == self.manager and e.payload['kind'] == 'assignment']), 1)
        self.assertTrue(self.rt.verify_chain().ok)


if __name__ == '__main__':
    unittest.main()
