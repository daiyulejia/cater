import unittest

import test_simple_team as fixture


class LeaderConsultationNoFollowupTests(unittest.TestCase):
    setUp = fixture.SimpleTeamTests.setUp
    events = fixture.SimpleTeamTests.events

    def exercise(self, *, incoming_during_reply=False):
        def answer(context, history, tools):
            if incoming_during_reply:
                other = self.rt.team._role(self.worker)
                self.rt.team.receive(self.task, f'@{other.name} 请核对另一项', request_id='cross-role-input')
            return fixture.turn('转换需要安装引擎，当前未验证。')

        self.driver.steps = {
            self.manager: [fixture.turn('', ('delegate', {'recipient': self.worker, 'instruction': '查询转换依赖'})),
                           fixture.turn('', ('wait_for_reply', {})), answer,
                           fixture.turn('', ('finish_work', {'summary': '不应交付', 'verification': '无'}))],
            self.worker: [fixture.turn('', ('return_work', {'text': '需要安装转换引擎，未验证。'})),
                          fixture.turn('另一项尚未核对。')],
        }
        source = self.rt.team.receive(self.task, '转换需要什么依赖？', request_id='consultation-root')
        self.rt.collaborate(self.task)
        replies = [e for e in self.events('msg.said') if e.payload.get('audience') == 'customer'
                   and e.payload.get('reply_to') == source.seq]
        self.assertEqual([e.payload['text'] for e in replies], ['转换需要安装引擎，当前未验证。'])
        self.assertEqual(self.events('team.objective_completed'), [])
        self.assertEqual([e for e in self.events('team.tool_started') if e.payload.get('name') == 'finish_work'], [])
        manager_calls = [c for c in self.driver.calls if c['context']['you']['id'] == self.manager]
        self.assertEqual(len(manager_calls), 3)
        self.assertTrue(self.rt.verify_chain().ok)
        return replies

    def test_consultation_reply_is_not_delivery_or_repeated(self):
        self.exercise()

    def test_cross_role_customer_message_during_reply_cannot_trigger_extra_turn(self):
        self.exercise(incoming_during_reply=True)
        self.assertEqual(len([c for c in self.driver.calls if c['context']['you']['id'] == self.worker]), 2)

    def test_explicit_customer_reply_does_not_repeat_or_deliver(self):
        self.driver.steps = {
            self.manager: [fixture.turn('', ('delegate', {'recipient': self.worker, 'instruction': '核对依赖'})),
                fixture.turn('', ('wait_for_reply', {})),
                fixture.turn('', ('reply_to_customer', {'text': '需要安装转换引擎。'})),
                fixture.turn('', ('reply_to_customer', {'text': '不应重复答复。'}))],
            self.worker: [fixture.turn('', ('return_work', {'text': '需要安装转换引擎。'}))],
        }
        source = self.rt.team.receive(self.task, '需要什么依赖？', request_id='explicit-consult')
        self.rt.collaborate(self.task)
        replies = [e for e in self.events('msg.said') if e.payload.get('audience') == 'customer'
                   and e.payload.get('reply_to') == source.seq]
        self.assertEqual([e.payload['text'] for e in replies], ['需要安装转换引擎。'])
        self.assertEqual(self.events('team.objective_completed'), [])
        self.assertEqual(len([e for e in self.events('team.tool_finished')
                              if e.payload.get('name') == 'reply_to_customer']), 1)

    def test_unattributed_internal_report_does_not_trigger_automatic_finish(self):
        leader = self.rt.team._role(self.manager)
        worker = self.rt.team._role(self.worker)
        assignment = self.rt.team._send(self.task, leader, self.worker, '核对结果', 'assignment')
        self.rt.team._send(self.task, worker, self.manager, '已核对', 'report', reply_to=assignment.seq)
        source, role, started = self.rt.team._claim(self.task)
        self.driver.steps[self.manager] = [fixture.turn('收到'),
            fixture.turn('', ('finish_work', {'summary': '不应交付', 'verification': '无'}))]
        list(self.rt.team._run_message(self.task, source, role, started))
        self.assertEqual(len(self.driver.calls), 1)
        self.assertEqual(self.events('team.objective_completed'), [])
        self.assertEqual(len(self.events('msg.said')), 1)
