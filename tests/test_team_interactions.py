"""Interaction semantics with real native transcripts and deterministic test models."""

import unittest

from cater.team import _reported_messages
from test_team_supervision import TeamSupervisionTests


class TeamInteractionTests(TeamSupervisionTests):
    def test_each_tool_turn_sees_new_peer_information_without_losing_native_history(self):
        def incoming(context, history, tools):
            self.assertFalse(any(e['text'] == '刚完成的验证信息' for e in context['conversation']))
            self.rt.team._send(self.task, self.rt._role_by_id(self.worker), self.manager,
                               '刚完成的验证信息', 'notification', visible=True)
            return self.native_turn('本轮工具操作说明', ('read_receipts', {}))

        def continued(context, history, tools):
            peer, = [e for e in context['conversation'] if e['text'] == '刚完成的验证信息']
            self.assertEqual(peer['sender_name'], self.rt._role_by_id(self.worker).name)
            self.assertFalse(peer['is_you'])
            self.assertEqual(peer['sender_kind'], 'agent')
            self.assertEqual(context['message']['sender_name'], '顾客')
            self.assertFalse(any(e['text'] == '本轮工具操作说明' for e in context['conversation']))
            self.assertTrue(any(e.get('content') == '本轮工具操作说明' for e in history))
            return self.native_turn('依据刚到的真实记录回答。')

        self.driver.steps[self.manager] = [incoming, continued]
        self.rt.team.receive(self.task, '请核对进展', request_id='fresh-context')
        self.run_team()
        self.assertEqual(len(self.driver.calls), 2)
        self.assertGreater(self.validated_continuations, 0)
        self.assertEqual(self.rt.team.view(self.task)['pending_count'], 0)

    def test_notification_is_readable_but_never_claimed_or_counted_as_pending(self):
        name = self.rt._role_by_id(self.worker).name
        self.driver.steps[self.worker] = [self.native_turn('', ('send_message', {
            'recipient': self.manager, 'text': '仅同步已收到，不需要回复。', 'notify_only': True}), ('wait_for_reply', {}))]
        self.rt.team.receive(self.task, f'@{name} 同步一下即可', request_id='notify-no-wakeup')
        self.run_team()
        note, = [e for e in self.events('team.message_received') if e.payload['kind'] == 'notification']
        self.assertEqual(self.rt.team.view(self.task)['pending_count'], 0)
        self.assertEqual(len(self.driver.calls), 1)
        self.assertFalse(any(e.payload['message_seq'] == note.seq for e in self.events('team.run_started')))
        new = self.rt.team.receive(self.task, '后续再看', request_id='manager-reads-note')
        context = self.rt.team._context(self.task, new, self.rt.organization.leader)
        self.assertTrue(any(e['seq'] == note.seq for e in context['conversation']))

    def test_notifying_wait_does_not_generate_another_progress_request(self):
        self.driver.steps = {
            self.manager: [self.native_turn('', ('delegate', {'recipient': self.worker, 'instruction': '等待资料后整理说明'})),
                           self.native_turn('', ('wait_for_reply', {}))],
            self.worker: [self.native_turn('', ('send_message', {
                'recipient': self.manager, 'text': '正在等资料，暂不需要你行动。', 'notify_only': True}), ('wait_for_reply', {}))]}
        self.rt.team.receive(self.task, '整理说明', request_id='delegate-with-notice')
        self.run_team()
        messages = self.rt.team._messages(self.task)
        original, = [e for e in messages if e.payload['kind'] == 'assignment']
        self.assertNotIn(original.seq, _reported_messages(messages))
        self.assertFalse([e for e in messages if e.payload['kind'] in {'report', 'progress'}])
        self.assertEqual(len(self.driver.calls), 3)
        context = self.rt.team._context(self.task, messages[0], self.rt.organization.leader)
        outgoing, = context['outgoing_work']
        self.assertEqual(outgoing['seq'], original.seq)
        self.assertEqual(outgoing['status'], 'waiting')
        self.assertEqual(outgoing['latest_update'], '正在等资料，暂不需要你行动。')

    def test_reply_to_notification_is_not_a_new_report_or_parent_completion(self):
        original = self.rt.team._send(self.task, self.rt.organization.leader, self.worker, '等资料完成原工作', 'assignment')
        self.driver.steps[self.worker] = [self.native_turn('', ('send_message', {
            'recipient': self.manager, 'text': '资料尚缺，仅供知悉', 'notify_only': True}), ('wait_for_reply', {}))]
        self.run_team()
        note, = [e for e in self.events('team.message_received') if e.payload['kind'] == 'notification']
        self.driver.steps[self.manager] = [self.native_turn('', ('send_message', {
            'recipient': self.worker, 'text': '收到', 'reply_to': note.seq})), self.native_turn('', ('wait_for_reply', {}))]
        self.rt.team.receive(self.task, '看下最新状态', request_id='reply-to-notification')
        self.run_team()
        self.assertEqual(self.results('send_message')[-1]['status'], 'error')
        self.assertNotIn(original.seq, _reported_messages(self.rt.team._messages(self.task)))
        self.assertEqual(sum(c['context']['you']['id'] == self.worker for c in self.driver.calls), 1)

    def test_notifications_have_their_own_bounded_context_and_scoped_history_reader(self):
        worker = self.rt._role_by_id(self.worker)
        other = next(r for r in self.rt.worker_roles if r.id != self.worker)
        first = self.rt.team._send(self.task, worker, self.manager, '第一条知会不可被其他人的消息淹没', 'notification')
        for index in range(60):
            self.rt.team._send(self.task, worker, other.id, f'其他伙伴知会{index}', 'notification')
        human = self.rt.team.receive(self.task, '什么情况', request_id='bounded-inbox')
        context = self.rt.team._context(self.task, human, self.rt.organization.leader)
        self.assertNotIn(first.seq, [e['seq'] for e in context['conversation']])
        self.assertEqual(context['notifications'][0]['seq'], first.seq)
        for index in range(21):
            self.rt.team._send(self.task, worker, self.manager, f'本人的知会{index}', 'notification')
        context = self.rt.team._context(self.task, human, self.rt.organization.leader)
        self.assertEqual(len(context['notifications']), 20)
        self.assertTrue(context['notifications_truncated'])
        page = self.rt.team._execute(self.task, self.rt.organization.leader, 'read_messages',
            {'kind': 'notification', 'limit': 10}, None, {}, None, None)
        self.assertEqual(page['messages'][0]['seq'], first.seq)
        self.assertTrue(page['has_more'])
        self.assertFalse(any('其他伙伴' in e['text'] for e in page['messages']))
        second = self.rt.team._execute(self.task, self.rt.organization.leader, 'read_messages',
            {'kind': 'notification', 'after_seq': page['next_seq'], 'limit': 50}, None, {}, None, None)
        self.assertFalse(second['has_more'])
        self.assertTrue(all(e['seq'] > page['next_seq'] for e in second['messages']))
        self.assertEqual(self.rt.team.view(self.task)['pending_count'], 1)

    def test_notify_and_reply_cannot_be_mixed_or_inferred_from_arbitrary_text(self):
        def invalid(context, history, tools):
            return self.native_turn('', ('send_message', {'recipient': self.manager, 'text': 'invalid',
                'reply_to': context['message']['seq'], 'notify_only': True}),
                ('send_message', {'recipient': self.manager, 'text': 'invalid', 'notify_only': 'true'}))

        self.rt.team._send(self.task, self.rt.organization.leader, self.worker, '检查并回复', 'assignment')
        self.driver.steps[self.worker] = [invalid, self.native_turn('已核对，正式交回。')]
        self.driver.steps[self.manager] = [self.native_turn('', ('wait_for_reply', {}))]
        self.run_team()
        self.assertEqual([r['status'] for r in self.results('send_message')], ['error', 'error'])
        self.assertFalse([e for e in self.events('team.message_received') if e.payload['kind'] == 'notification'])

    def test_new_customer_question_does_not_falsely_close_active_delegation(self):
        name = self.rt._role_by_id(self.worker).name
        def interrupted(context, history, tools):
            self.rt.team.receive(self.task, f'@{name} 先告诉我目前卡在哪里', request_id='question-mid-execution')
            return self.native_turn('过期操作不应运行', ('publish_artifact', {
                'filename': 'wrong.txt', 'content': 'wrong', 'summary': 'wrong', 'format': 'text'}))

        def answer(context, history, tools):
            self.assertIsNone(context['active_assignment'])
            self.assertEqual(len(context['open_assignments']), 1)
            return self.native_turn('资料还缺一部分，这份工作尚未完成。')

        self.driver.steps = {
            self.manager: [self.native_turn('', ('delegate', {'recipient': self.worker, 'instruction': '整理原说明'})),
                           self.native_turn('', ('wait_for_reply', {}))],
            self.worker: [interrupted, answer]}
        self.rt.team.receive(self.task, '整理说明', request_id='work-before-question')
        self.run_team()
        original, = [e for e in self.events('team.message_received') if e.payload['kind'] == 'assignment']
        self.assertNotIn(original.seq, _reported_messages(self.rt.team._messages(self.task)))
        self.assertFalse(self.rt.task(self.task).deliverables)
        self.assertFalse([e for e in self.events('team.message_received') if e.payload['kind'] == 'report'])


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(TeamInteractionTests(name) for name in TeamInteractionTests.__dict__ if name.startswith('test_'))


if __name__ == '__main__':
    unittest.main()
