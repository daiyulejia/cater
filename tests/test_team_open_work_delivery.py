"""A goal cannot close while delegated work has not returned its first result."""
from __future__ import annotations

import unittest
import io
from pathlib import Path
import zipfile

from cater.team import HOST
import test_simple_team as fixture

turn = fixture.turn


class OpenWorkDeliveryTests(unittest.TestCase):
    setUp = fixture.SimpleTeamTests.setUp
    events = fixture.SimpleTeamTests.events
    run_ok = fixture.SimpleTeamTests.run_ok

    def test_delegate_and_finish_in_one_turn_waits_for_the_first_worker_file(self):
        self.driver.steps = {
            self.manager: [
                turn('', ('delegate', {'recipient': self.worker, 'instruction': '写一份中文使用说明并交回。'}),
                     ('finish_work', {'summary': '错误的提前交付', 'verification': '接收者尚未产出'})),
                turn('', ('wait_for_reply', {})),
                turn('', ('finish_work', {'summary': '说明已交回，可以打开', 'verification': '仅核对正文，未执行转换'})),
                turn('说明已保存，运行依赖与未验证范围见正文。'),
            ],
            self.worker: [
                turn('', ('publish_artifact', {'filename': 'guide.md', 'content': '# 使用说明\n需要转换引擎，未执行转换。',
                    'summary': '说明与边界', 'format': 'markdown'})),
                turn('使用说明已写好，尚未运行转换。'),
            ],
        }
        self.rt.team.receive(self.task, '请写一份使用说明', request_id='open-work-before-first-file')
        self.run_ok()
        attempts = [event for event in self.events('team.tool_finished') if event.payload.get('name') == 'finish_work']
        self.assertEqual(attempts[0].payload['result']['status'], 'error')
        self.assertEqual(attempts[0].payload['result']['code'], 'work_pending')
        pending, = attempts[0].payload['result']['assignments']
        self.assertEqual(pending['recipient'], self.worker)
        self.assertEqual(pending['status'], 'queued')
        self.assertFalse(pending['requires_return'])
        self.assertEqual(attempts[-1].payload['result']['status'], 'ok')
        delivery, = self.events('team.objective_completed')
        artifact, = self.rt.task(self.task).deliverables
        self.assertLess(artifact['seq'], delivery.seq)
        self.assertEqual(delivery.payload['summary'], '说明已交回，可以打开')
        self.assertEqual(delivery.payload['artifacts'][0]['seq'], artifact['seq'])
        self.assertEqual(Path(artifact['local_path']).read_text(encoding='utf-8'), artifact['content'])
        with zipfile.ZipFile(io.BytesIO(self.rt.team.archive(self.task))) as archive:
            self.assertEqual(archive.read('guide.md'), artifact['content'].encode('utf-8'))
        self.assertTrue(any(call['context']['you']['id'] == self.worker for call in self.driver.calls))

    def finish_input(self):
        self.rt.team.receive(self.task, '请核对后完成', request_id='open-work-finish-input')
        source, role, run = self.rt.team._claim(self.task)
        self.assertEqual(role.id, self.manager)
        tool = self.rt.log.emit(type='team.tool_started', task_id=self.task, actor=HOST, payload={
            **run.payload, 'name': 'finish_work', 'call_id': 'finish-input', 'arguments': {}})
        return source, role, run, tool

    def finish(self, role, tool):
        return self.rt.team._finish_work(self.task, role,
            {'summary': '结果说明', 'verification': '只核对本轮记录', 'artifact_seqs': []}, tool)

    def test_analysis_without_files_is_not_silently_dropped_before_return(self):
        _, role, _, tool = self.finish_input()
        assignment = self.rt.team._send(self.task, role, self.worker, '请分析依赖，不需要文件', 'assignment')
        result = self.finish(role, tool)
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['code'], 'work_pending')
        self.assertEqual(result['assignments'][0]['seq'], assignment.seq)
        self.assertEqual(self.events('team.objective_completed'), [])
        self.rt.team._send(self.task, self.rt.team._role(self.worker), role.id,
                           '分析结果：需要外部转换引擎。', 'report', reply_to=assignment.seq)
        self.assertEqual(self.finish(role, tool)['status'], 'ok')
        self.assertEqual(self.rt.task(self.task).deliverables, [])

    def test_notifications_do_not_create_a_completion_obligation(self):
        _, role, _, tool = self.finish_input()
        self.rt.team._send(self.task, role, self.worker, '只同步已核对的信息，无需回复', 'notification')
        self.assertEqual(self.finish(role, tool)['status'], 'ok')

    def test_running_worker_without_artifacts_still_has_to_return(self):
        _, role, _, tool = self.finish_input()
        assignment = self.rt.team._send(self.task, role, self.worker, '先分析接口，不必生成文件', 'assignment')
        _, worker, run = self.rt.team._claim(self.task)
        self.assertEqual(worker.id, self.worker)
        result = self.finish(role, tool)
        self.assertEqual(result['code'], 'work_pending')
        row, = result['assignments']
        self.assertEqual((row['seq'], row['status']), (assignment.seq, 'running'))
        self.rt.log.emit(type='team.run_finished', task_id=self.task, actor=HOST,
            payload={**run.payload, 'status': 'completed'})
        self.rt.team._send(self.task, worker, role.id, '接口分析已交回，外部依赖尚未验证。', 'report', reply_to=assignment.seq)
        self.assertEqual(self.finish(role, tool)['status'], 'ok')

    def test_plain_agent_request_waits_for_the_same_result_as_a_delegate(self):
        _, role, _, tool = self.finish_input()
        assignment = self.rt.team._send(self.task, role, self.worker, '请回答转换依赖是否齐全', 'message')
        result = self.finish(role, tool)
        self.assertEqual(result['code'], 'work_pending')
        self.assertEqual(result['assignments'][0]['seq'], assignment.seq)
        self.rt.team._send(self.task, self.rt.team._role(self.worker), role.id,
                           '依赖未安装，本次只整理说明。', 'report', reply_to=assignment.seq)
        self.assertEqual(self.finish(role, tool)['status'], 'ok')

    def test_only_returning_one_of_two_jobs_does_not_close_the_other(self):
        _, role, _, tool = self.finish_input()
        jobs = [self.rt.team._send(self.task, role, self.worker, f'分析第{index}项', 'assignment') for index in range(2)]
        worker = self.rt.team._role(self.worker)
        self.rt.team._send(self.task, worker, role.id, '第一项分析完成', 'report', reply_to=jobs[0].seq)
        result = self.finish(role, tool)
        self.assertEqual(result['code'], 'work_pending')
        self.assertEqual([row['seq'] for row in result['assignments']], [jobs[1].seq])
        self.rt.team._send(self.task, worker, role.id, '第二项分析完成', 'report', reply_to=jobs[1].seq)
        self.assertEqual(self.finish(role, tool)['status'], 'ok')

    def test_child_work_without_a_file_is_not_lost_after_the_parent_returns(self):
        _, role, _, tool = self.finish_input()
        parent = self.rt.team._send(self.task, role, self.worker, '分析后可请伙伴核对依赖', 'assignment')
        worker = self.rt.team._role(self.worker)
        reviewer = next(candidate for candidate in self.rt.roles if candidate.id not in {role.id, worker.id})
        child = self.rt.team._send(self.task, worker, reviewer.id, '请核对依赖说明', 'assignment', parent_assignment_seq=parent.seq)
        self.rt.team._send(self.task, worker, role.id, '我的分析已交回，伙伴仍在核对', 'report', reply_to=parent.seq)
        result = self.finish(role, tool)
        self.assertEqual(result['code'], 'work_pending')
        self.assertEqual(result['assignments'][0]['seq'], child.seq)
        self.rt.team._send(self.task, reviewer, worker.id, '依赖说明核对结束，未运行转换。', 'report', reply_to=child.seq)
        self.assertEqual(self.finish(role, tool)['status'], 'ok')

    def test_old_goal_jobs_do_not_become_new_goal_obligations(self):
        old = self.rt.team._send(self.task, self.rt.organization.leader, self.worker, '旧方案的分析', 'assignment')
        self.rt.log.emit(type='team.objective_updated', task_id=self.task, actor=HOST,
                         payload={'goal': '新目标只整理已有资料'})
        _, role, _, tool = self.finish_input()
        self.assertEqual(self.finish(role, tool)['status'], 'ok')
        self.assertEqual(next(event for event in self.events('team.message_received') if event.seq == old.seq).payload['text'], '旧方案的分析')

    def test_direct_delivery_without_delegation_needs_no_extra_role(self):
        _, role, _, tool = self.finish_input()
        self.assertEqual(self.finish(role, tool)['status'], 'ok')
        self.assertEqual(self.driver.calls, [])

    def test_waiting_result_does_not_require_a_purely_formal_report(self):
        _, role, _, tool = self.finish_input()
        assignment = self.rt.team._send(self.task, role, self.worker, '请给出依赖分析', 'assignment')
        _, worker, run = self.rt.team._claim(self.task)
        self.rt.log.emit(type='team.run_finished', task_id=self.task, actor=HOST,
            payload={**run.payload, 'status': 'waiting'})
        self.rt.team._send(self.task, worker, role.id, '分析已给出：需要外部转换引擎，尚未运行。', 'progress', reply_to=assignment.seq)
        self.assertEqual(self.finish(role, tool)['status'], 'ok')

    def manager_delivers_incoming(self, kind, *, resume=False):
        source = self.rt.team._send(self.task, self.rt.team._role(self.worker), self.manager,
                                   '请确认并交付这份无文件结论', kind)
        if resume:
            _, _, run = self.rt.team._claim(self.task)
            self.rt.log.emit(type='team.run_finished', task_id=self.task, actor=HOST,
                             payload={**run.payload, 'status': 'failed', 'reason': 'offline interruption'})
            self.rt.team._send(self.task, self.rt.team._role(self.worker), self.manager,
                              '核对已有结论后接续交付', 'continuation', continue_assignment_seq=source.seq)
        self.driver.steps = {self.manager: [
            turn('', ('finish_work', {'summary': '无文件结论已交付', 'verification': '离线记录核对'})),
            turn('结论已交付，验证边界保留。'),
        ]}
        self.rt.collaborate(self.task)
        result, = [e.payload['result'] for e in self.events('team.tool_finished')
                   if e.payload['name'] == 'finish_work']
        self.assertEqual(result['status'], 'ok')
        view = self.rt.team.view(self.task)
        self.assertEqual(view['status'], 'done')
        self.assertEqual(view['pending_count'], 0)
        calls = len(self.driver.calls)
        self.rt.collaborate(self.task)
        self.assertEqual(len(self.driver.calls), calls)
        self.assertEqual(len(self.events('team.objective_completed')), 1)

    def test_current_manager_message_is_answered_by_delivery_itself(self):
        self.manager_delivers_incoming('message')

    def test_current_manager_assignment_is_answered_by_delivery_itself(self):
        self.manager_delivers_incoming('assignment')

    def test_resumed_manager_input_can_finish_its_original_failed_request(self):
        self.manager_delivers_incoming('assignment', resume=True)

    def test_other_queued_manager_request_is_not_the_current_input(self):
        _, role, _, tool = self.finish_input()
        other = self.rt.team._send(self.task, self.rt.team._role(self.worker), self.manager,
                                  '还需要你核对另一项结论', 'message')
        result = self.finish(role, tool)
        self.assertEqual(result['code'], 'work_pending')
        self.assertEqual([row['seq'] for row in result['assignments']], [other.seq])
        self.assertEqual(self.events('team.objective_completed'), [])

    def test_different_manager_run_is_not_exempt_even_for_same_message(self):
        source = self.rt.team._send(self.task, self.rt.team._role(self.worker), self.manager,
                                   '请核对结论', 'message')
        _, role, run = self.rt.team._claim(self.task)
        tool = self.rt.log.emit(type='team.tool_started', task_id=self.task, actor=HOST, payload={
            **run.payload, 'run_id': 'different-run', 'name': 'finish_work', 'call_id': 'not-current', 'arguments': {}})
        result = self.finish(role, tool)
        self.assertEqual(result['code'], 'work_pending')
        self.assertEqual(result['assignments'][0]['seq'], source.seq)
        self.assertEqual(self.events('team.objective_completed'), [])


if __name__ == '__main__':
    unittest.main()
