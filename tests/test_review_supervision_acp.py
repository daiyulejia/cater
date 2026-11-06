"""Claimed supervision through real offline ACP and MCP, no remote model."""
from pathlib import Path
from dataclasses import replace
import sys
import threading
import time
import unittest
from unittest.mock import patch

from cater.acp import AcpSession
from cater.events import Actor
from cater.external_agent import QoderAcpDriver
from cater.review_supervision_candidate import create
from cater.runtime import Runtime
from cater.team import HOST
import test_review_supervision_candidate as candidate_fixture


class ReviewSupervisionAcpTests(unittest.TestCase):
    def test_old_report_cannot_publish_after_direction_changes_at_commit(self):
        helper = candidate_fixture.ReviewSupervisionCandidateTests(methodName='runTest')
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        f = helper.fixture('external')
        artifact, = helper.run_calls(f, helper.human(f), ('publish_artifact', {
            'filename': 'review.txt', 'content': 'fixture', 'summary': 'fixture', 'format': 'text'}))
        review, = helper.run_calls(f, helper.human(f), ('request_review', {
            'reviewer': f.reviewer, 'artifact_seqs': [artifact['artifact_seq']], 'instruction': 'Inspect'}))
        helper.run_calls(f, helper.message(f, review['message_seq']), ('record_review', {
            'review_seq': review['review_seq'], 'verdict': 'approved', 'feedback': 'Checked'}), statuses=('waiting',))
        driver, session, _ = self.driver_and_session(f, 'old-publish')
        f.rt.drivers[driver.name] = driver
        original = f.rt.team._publication_guard
        def inject(task_id, filename, tool_start):
            original(task_id, filename, tool_start)
            f.rt.command(task_id, 'Update scope', goal='Different result')
        with patch('cater.external_agent.AcpSession', session), patch.object(f.rt.team, '_publication_guard', side_effect=inject):
            f.rt.collaborate(f.task)
        self.assertFalse([e for e in f.events('deliverable.published') if e.payload.get('filename') == 'stale.txt'])

    def test_cross_role_source_reaches_qoder_supervision_prompt(self):
        helper = candidate_fixture.ReviewSupervisionCandidateTests(methodName='runTest')
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        f = helper.fixture('external')
        artifact, = helper.run_calls(f, helper.human(f), ('publish_artifact', {
            'filename': 'review.txt', 'content': 'fixture', 'summary': 'fixture', 'format': 'text'}))
        review, = helper.run_calls(f, helper.human(f), ('request_review', {
            'reviewer': f.reviewer, 'artifact_seqs': [artifact['artifact_seq']], 'instruction': 'Inspect'}))
        helper.run_calls(f, helper.message(f, review['message_seq']), ('record_review', {
            'review_seq': review['review_seq'], 'verdict': 'approved', 'feedback': 'Checked'}), statuses=('waiting',))
        worker = f.rt.team._role(f.worker)
        message = f.rt.team.receive(f.task, f'@{worker.name} 另外需要离线支持。', request_id='qoder-source-cross')
        helper.run_calls(f, message, ('reply_to_customer', {'text': '已收到。'}), statuses=('completed',))
        claimed = f.rt.team._claim(f.task)
        f.driver.scripts[f.manager] = [lambda run: 'Review acknowledged.']
        list(f.rt.team._run_message(f.task, *claimed))
        candidate = create(f.rt.team, f.task, run_seq=claimed[2].seq, run_hash=claimed[2].hash)
        self.assertIsNotNone(candidate)
        driver, session, sessions = self.driver_and_session(f, 'source')
        f.rt.drivers[driver.name] = driver
        with f.rt.team.recovery.review_candidate_lease(f.task, candidate_seq=candidate.seq, candidate_hash=candidate.hash) as supervision:
            self.assertIsNotNone(supervision)
            with patch('cater.external_agent.AcpSession', session):
                list(f.rt.team._run_message(f.task, *supervision))
        finish, = [e for e in f.events('team.run_finished') if e.payload.get('message_seq') == candidate.seq]
        self.assertEqual(finish.payload.get('interruption_kind'), 'review_supervision_no_action')
        self.assertEqual(len(sessions), 1)

    def ready(self, *, consultation=False, claim=True):
        helper = candidate_fixture.ReviewSupervisionCandidateTests(methodName='runTest')
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        f, original = helper.ready(consultation=consultation)
        candidate = create(f.rt.team, f.task, run_seq=original.seq, run_hash=original.hash)
        self.assertIsNotNone(candidate)
        claimed = (self.enterContext(f.rt.team.recovery.review_candidate_lease(
            f.task, candidate_seq=candidate.seq, candidate_hash=candidate.hash)) if claim else None)
        if claim:
            self.assertIsNotNone(claimed)
        return f, candidate, claimed

    def driver_and_session(self, f, mode):
        driver = QoderAcpDriver(f.driver.name, 'never-real-qoder', 'offline', timeout=10)
        self.addCleanup(driver.close)
        peer = Path(__file__).parent / 'fixtures' / 'review_supervision_acp_peer.py'
        sessions = []
        class Session(AcpSession):
            def __init__(self, command, **kwargs):
                super().__init__([sys.executable, '-u', str(peer.resolve()), mode, str(f.root)], **kwargs)
                sessions.append(self)
        return driver, Session, sessions

    def test_entire_formal_review_chain_and_delivery_use_offline_acp(self):
        helper = candidate_fixture.ReviewSupervisionCandidateTests(methodName='runTest')
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        f = helper.fixture('external')
        driver = QoderAcpDriver(f.driver.name, 'never-real-qoder', 'offline', timeout=10)
        self.addCleanup(driver.close)
        f.rt.drivers[driver.name] = driver
        (f.root / 'reviewer-id').write_text(f.reviewer, encoding='utf-8')
        peer = Path(__file__).parent / 'fixtures' / 'review_supervision_acp_peer.py'
        phase = {'value': '', 'queue': []}
        sessions = []
        class Session(AcpSession):
            def __init__(self, command, **kwargs):
                mode = phase['queue'].pop(0) if phase['queue'] else phase['value']
                super().__init__([sys.executable, '-u', str(peer.resolve()), mode, str(f.root)], **kwargs)
                sessions.append(self)
        with patch('cater.external_agent.AcpSession', Session):
            for mode in ('publish', 'request'):
                phase['value'] = mode
                f.rt.team.receive(f.task, '请完成当前工作', request_id='full-chain-' + mode)
                claimed = f.rt.team._claim(f.task)
                self.assertEqual(claimed[1].id, f.manager)
                list(f.rt.team._run_message(f.task, *claimed))
            phase['queue'] = ['record', 'initial', 'finish']
            f.rt.collaborate(f.task)
            self.assertEqual(phase['queue'], [])
        self.assertEqual(len(sessions), 5)
        self.assertTrue(all(s._proc.poll() is not None for s in sessions))
        self.assertEqual(len(f.events('team.review_recorded')), 1)
        candidate, = [e for e in f.events('team.message_received') if e.payload.get('trigger') == 'review_return_candidate']
        self.assertEqual(len([e for e in f.events('team.run_started') if e.payload.get('message_seq') == candidate.seq]), 1)
        delivery, = f.events('team.objective_completed')
        self.assertEqual(delivery.payload['summary'], 'Offline checked result')
        self.assertTrue(f.rt.verify_chain().ok)

    def test_automatic_supervision_no_action_and_empty_wait_stop_once(self):
        for outcome in ('text', 'wait', 'reject'):
            with self.subTest(outcome=outcome):
                helper = candidate_fixture.ReviewSupervisionCandidateTests(methodName='runTest')
                helper.setUp()
                self.addCleanup(helper.doCleanups)
                f = helper.fixture('external')
                artifact, = helper.run_calls(f, helper.human(f), ('publish_artifact', {
                    'filename': 'candidate.txt', 'content': 'content', 'summary': 'fixture', 'format': 'text'}))
                review, = helper.run_calls(f, helper.human(f), ('request_review', {
                    'reviewer': f.reviewer, 'artifact_seqs': [artifact['artifact_seq']], 'instruction': 'Inspect'}))
                assignment = helper.message(f, review['message_seq'])
                helper.run_calls(f, assignment, ('record_review', {'review_seq': review['review_seq'],
                    'verdict': 'approved', 'feedback': 'Checked'}), statuses=('waiting',))
                driver = QoderAcpDriver(f.driver.name, 'never-real-qoder', 'offline', timeout=10)
                self.addCleanup(driver.close)
                f.rt.drivers[driver.name] = driver
                peer = Path(__file__).parent / 'fixtures' / 'review_supervision_acp_peer.py'
                modes = ['initial', outcome]
                sessions = []
                class Session(AcpSession):
                    def __init__(self, command, **kwargs):
                        super().__init__([sys.executable, '-u', str(peer.resolve()), modes.pop(0), str(f.root)], **kwargs)
                        sessions.append(self)
                with patch('cater.external_agent.AcpSession', Session):
                    f.rt.collaborate(f.task)
                    f.rt.collaborate(f.task)
                self.assertEqual(modes, [])
                self.assertEqual(len(sessions), 2)
                candidate, = [e for e in f.events('team.message_received') if e.payload.get('trigger') == 'review_return_candidate']
                finished, = [e for e in f.events('team.run_finished') if e.payload.get('message_seq') == candidate.seq]
                self.assertEqual(finished.payload['status'], 'failed' if outcome == 'reject' else 'interrupted')
                if outcome != 'reject':
                    self.assertEqual(finished.payload['interruption_kind'],
                                     'review_supervision_empty_wait' if outcome == 'wait' else 'review_supervision_no_action')
                self.assertEqual(f.events('team.objective_completed'), [])
                self.assertTrue(f.rt.verify_chain().ok)

    def test_ordinary_chat_and_old_completed_review_do_not_trigger(self):
        for case in ('chat', 'historical'):
            with self.subTest(case=case):
                helper = candidate_fixture.ReviewSupervisionCandidateTests(methodName='runTest')
                helper.setUp()
                self.addCleanup(helper.doCleanups)
                if case == 'historical':
                    f, _ = helper.ready()
                else:
                    f = helper.fixture('external')
                    f.rt.team.receive(f.task, '普通聊天：你好', request_id='ordinary-chat')
                driver, session, sessions = self.driver_and_session(f, 'initial')
                f.rt.drivers[driver.name] = driver
                with patch('cater.external_agent.AcpSession', session):
                    f.rt.collaborate(f.task)
                self.assertEqual(len(sessions), 1 if case == 'chat' else 0)
                self.assertEqual([e for e in f.events('team.message_received')
                                  if e.payload.get('trigger') == 'review_return_candidate'], [])
                self.assertEqual(f.events('team.objective_completed'), [])

    def test_automatic_drain_rechecks_cross_role_input_and_unknown_effect(self):
        for case in ('cross', 'unknown'):
            with self.subTest(case=case):
                helper = candidate_fixture.ReviewSupervisionCandidateTests(methodName='runTest')
                helper.setUp()
                self.addCleanup(helper.doCleanups)
                f = helper.fixture('external')
                artifact, = helper.run_calls(f, helper.human(f), ('publish_artifact', {
                    'filename': 'candidate.txt', 'content': 'content', 'summary': 'fixture', 'format': 'text'}))
                review, = helper.run_calls(f, helper.human(f), ('request_review', {
                    'reviewer': f.reviewer, 'artifact_seqs': [artifact['artifact_seq']], 'instruction': 'Inspect'}))
                helper.run_calls(f, helper.message(f, review['message_seq']), ('record_review', {
                    'review_seq': review['review_seq'], 'verdict': 'approved', 'feedback': 'Checked'}), statuses=('waiting',))
                driver = QoderAcpDriver(f.driver.name, 'never-real-qoder', 'offline', timeout=10)
                self.addCleanup(driver.close)
                peer_path = Path(__file__).parent / 'fixtures' / 'review_supervision_acp_peer.py'
                modes = ['initial', case, 'initial'] if case == 'cross' else ['initial', case]
                sessions, errors = [], []
                class Session(AcpSession):
                    def __init__(self, command, **kwargs):
                        super().__init__([sys.executable, '-u', str(peer_path.resolve()), modes.pop(0), str(f.root)], **kwargs)
                        sessions.append(self)
                def run():
                    rt = Runtime(f.rt.log.path, roles_path=f.roles, drivers={f.driver.name: f.driver}, close_drivers_on_close=False)
                    try:
                        rt.drivers[driver.name] = driver
                        rt.collaborate(f.task)
                    except BaseException as exc:
                        errors.append(exc)
                    finally:
                        rt.close()
                with patch('cater.external_agent.AcpSession', Session):
                    thread = threading.Thread(target=run)
                    thread.start()
                    ready = f.root / 'peer-ready'
                    deadline = time.monotonic() + 10
                    while not ready.exists() and thread.is_alive() and time.monotonic() < deadline:
                        time.sleep(.02)
                    try:
                        self.assertTrue(ready.exists(), errors)
                        if case == 'cross':
                            worker = f.rt.team._role(f.worker)
                            f.rt.team.receive(f.task, f'@{worker.name} 新方向', request_id='automatic-cross-role')
                        else:
                            notice = f.rt.team._send(f.task, f.rt.team._role(f.manager), f.worker, 'notice', 'notification')
                            run = f.rt.log.emit(type='team.run_started', task_id=f.task, actor=HOST, payload={
                                'message_seq': notice.seq, 'role': f.worker, 'run_id': 'automatic-unknown', 'revision': 0})
                            f.rt.log.emit(type='team.tool_started', task_id=f.task, actor=HOST, payload={
                                **run.payload, 'name': 'workspace_write', 'call_id': 'uncertain', 'arguments': {}})
                            f.rt.log.emit(type='team.run_finished', task_id=f.task, actor=HOST,
                                          payload={**run.payload, 'status': 'interrupted'})
                    finally:
                        (f.root / 'peer-release').write_text('go', encoding='utf-8')
                        thread.join(15)
                    self.assertFalse(thread.is_alive())
                self.assertEqual(errors, [])
                candidate, = [e for e in f.events('team.message_received') if e.payload.get('trigger') == 'review_return_candidate']
                finished, = [e for e in f.events('team.run_finished') if e.payload.get('message_seq') == candidate.seq]
                self.assertEqual(finished.payload['status'], 'interrupted')
                self.assertEqual(f.events('team.objective_completed'), [])
                self.assertEqual(len(sessions), 3 if case == 'cross' else 2)
                self.assertTrue(f.rt.verify_chain().ok)

    def test_actual_protocol_finish_delegate_and_plain_text(self):
        for mode in ('finish', 'delegate', 'text', 'consult', 'wait', 'notify_wait'):
            with self.subTest(mode=mode):
                f, candidate, claimed = self.ready(consultation=mode == 'consult')
                driver, session, sessions = self.driver_and_session(f, mode)
                f.rt.drivers[driver.name] = driver
                with patch('cater.external_agent.AcpSession', session):
                    list(f.rt.team._run_message(f.task, *claimed))
                finished, = [e for e in f.events('team.run_finished') if e.payload.get('message_seq') == candidate.seq]
                self.assertEqual(finished.payload['status'], 'interrupted' if mode in {'text', 'consult', 'wait', 'notify_wait'} else 'waiting' if mode == 'delegate' else 'completed')
                if mode in {'text', 'consult', 'wait', 'notify_wait'}:
                    self.assertEqual(finished.payload.get('interruption_kind'), 'review_supervision_empty_wait' if mode in {'wait', 'notify_wait'} else 'review_supervision_no_action')
                    self.assertEqual(f.events('team.objective_completed'), [])
                    self.assertEqual([e for e in f.events('msg.said') if e.payload.get('run_id') == claimed[2].payload['run_id'] and e.payload.get('audience') == 'customer'], [])
                    self.assertIn('没有待处理工作' if mode in {'wait', 'notify_wait'} else '未作出明确后续动作', f.rt.team.view(f.task)['checkpoint'])
                    if mode == 'consult':
                        answers = [e for e in f.events('msg.said') if e.payload.get('audience') == 'customer'
                                   and e.payload.get('text') == '复核流程是读取文件并登记意见。']
                        self.assertEqual(len(answers), 1)
                    self.assertEqual(f.rt.team.recovery.prepare(f.task), [])
                    self.assertEqual(len([e for e in f.events('team.run_started') if e.payload.get('message_seq') == candidate.seq]), 1)
                elif mode == 'finish':
                    delivery, = f.events('team.objective_completed')
                    customer, = [e for e in f.events('msg.said') if e.payload.get('delivery_seq') == delivery.seq]
                    self.assertEqual(customer.payload['text'], delivery.payload['summary'])
                else:
                    self.assertTrue(any(e.payload.get('kind') == 'assignment' and e.seq > candidate.seq for e in f.events('team.message_received')))
                    self.assertEqual(f.events('team.objective_completed'), [])
                self.assertEqual(len(sessions), 1)
                self.assertIsNotNone(sessions[0]._proc.poll())
                self.assertTrue(f.rt.verify_chain().ok)

    def test_forged_claim_is_rejected_before_acp_launch(self):
        f, candidate, claimed = self.ready()
        driver, session, sessions = self.driver_and_session(f, 'finish')
        f.rt.drivers[driver.name] = driver
        forged = replace(candidate, payload={**candidate.payload, 'original_run_hash': 'forged'})
        with patch('cater.external_agent.AcpSession', session):
            list(f.rt.team._run_message(f.task, forged, claimed[1], claimed[2]))
        self.assertEqual(sessions, [])
        finished, = [e for e in f.events('team.run_finished') if e.payload.get('message_seq') == candidate.seq]
        self.assertEqual(finished.payload['status'], 'interrupted')
        self.assertEqual(f.events('team.objective_completed'), [])

    def test_unknown_effect_between_dispatch_and_local_commit_blocks_delivery(self):
        f, candidate, claimed = self.ready()
        driver, session, _ = self.driver_and_session(f, 'finish')
        f.rt.drivers[driver.name] = driver
        invoke = f.rt.team._invoke_tool
        def inject(task_id, role, started, revision, call, schema, workspace, binding, tool_start, assignment, **kwargs):
            if call.name == 'finish_work':
                notice = f.rt.team._send(f.task, f.rt.team._role(f.manager), f.worker, 'notice', 'notification')
                peer = f.rt.log.emit(type='team.run_started', actor=HOST, task_id=f.task, payload={
                    'message_seq': notice.seq, 'role': f.worker, 'run_id': 'atomic-unknown', 'revision': 0})
                f.rt.log.emit(type='team.tool_started', actor=HOST, task_id=f.task, payload={
                    **peer.payload, 'name': 'workspace_write', 'call_id': 'uncertain', 'arguments': {}})
                f.rt.log.emit(type='team.run_finished', actor=HOST, task_id=f.task, payload={**peer.payload, 'status': 'interrupted'})
            return invoke(task_id, role, started, revision, call, schema, workspace, binding, tool_start, assignment, **kwargs)
        with patch('cater.external_agent.AcpSession', session), patch.object(f.rt.team, '_invoke_tool', side_effect=inject):
            list(f.rt.team._run_message(f.task, *claimed))
        self.assertEqual(f.events('team.objective_completed'), [])
        finished, = [e for e in f.events('team.run_finished') if e.payload.get('message_seq') == candidate.seq]
        self.assertEqual(finished.payload['status'], 'interrupted')

    def test_cross_role_input_between_dispatch_and_local_commit_blocks_delivery(self):
        f, candidate, claimed = self.ready()
        driver, session, _ = self.driver_and_session(f, 'finish')
        f.rt.drivers[driver.name] = driver
        invoke = f.rt.team._invoke_tool
        def inject(task_id, role, started, revision, call, schema, workspace, binding, tool_start, assignment, **kwargs):
            if call.name == 'finish_work':
                worker = f.rt.team._role(f.worker)
                f.rt.team.receive(f.task, f'@{worker.name} new direction', request_id='atomic-cross-role')
            return invoke(task_id, role, started, revision, call, schema, workspace, binding, tool_start, assignment, **kwargs)
        with patch('cater.external_agent.AcpSession', session), patch.object(f.rt.team, '_invoke_tool', side_effect=inject):
            list(f.rt.team._run_message(f.task, *claimed))
        self.assertEqual(f.events('team.objective_completed'), [])
        receipt, = [e for e in f.events('team.tool_finished') if e.payload.get('name') == 'finish_work']
        self.assertEqual(receipt.payload['result']['status'], 'error')
        finished, = [e for e in f.events('team.run_finished') if e.payload.get('message_seq') == candidate.seq]
        self.assertEqual(finished.payload['status'], 'interrupted')

    def test_direction_change_between_dispatch_and_local_commit_blocks_delivery(self):
        f, candidate, claimed = self.ready()
        driver, session, _ = self.driver_and_session(f, 'finish')
        f.rt.drivers[driver.name] = driver
        invoke = f.rt.team._invoke_tool
        def inject(task_id, role, started, revision, call, schema, workspace, binding, tool_start, assignment, **kwargs):
            if call.name == 'finish_work':
                f.rt.command(task_id, 'Update direction', goal='Updated deliverable')
            return invoke(task_id, role, started, revision, call, schema, workspace, binding, tool_start, assignment, **kwargs)
        with patch('cater.external_agent.AcpSession', session), patch.object(f.rt.team, '_invoke_tool', side_effect=inject):
            list(f.rt.team._run_message(f.task, *claimed))
        self.assertEqual(f.events('team.objective_completed'), [])
        tool, = [e for e in f.events('team.tool_finished') if e.payload.get('name') == 'finish_work']
        self.assertEqual(tool.payload['result']['status'], 'error')
        finished, = [e for e in f.events('team.run_finished') if e.payload.get('message_seq') == candidate.seq]
        self.assertEqual(finished.payload['status'], 'interrupted')

    def test_actual_protocol_new_input_or_unknown_effect_preempts(self):
        for mode in ('cross', 'unknown'):
            with self.subTest(mode=mode):
                f, candidate, _ = self.ready(claim=False)
                driver, session, sessions = self.driver_and_session(f, mode)
                errors = []
                def run():
                    rt = Runtime(f.rt.log.path, roles_path=f.roles, drivers={f.driver.name: f.driver}, close_drivers_on_close=False)
                    try:
                        rt.drivers[driver.name] = driver
                        with rt.team.recovery.review_candidate_lease(f.task, candidate_seq=candidate.seq, candidate_hash=candidate.hash) as claimed:
                            if claimed is None:
                                raise AssertionError('candidate was not claimed')
                            list(rt.team._run_message(f.task, *claimed))
                    except BaseException as exc:
                        errors.append(exc)
                    finally:
                        rt.close()
                with patch('cater.external_agent.AcpSession', session):
                    thread = threading.Thread(target=run)
                    thread.start()
                    ready = f.root / 'peer-ready'
                    deadline = time.monotonic() + 8
                    while not ready.exists() and thread.is_alive() and time.monotonic() < deadline:
                        time.sleep(.02)
                    self.assertTrue(ready.exists(), (errors, [e.payload for e in f.events('team.run_finished') if e.payload.get('message_seq') == candidate.seq]))
                    self.assertEqual(f.rt.team.recovery.prepare(f.task), [])
                    self.assertFalse([e for e in f.events('team.run_finished') if e.payload.get('message_seq') == candidate.seq])
                    if mode == 'cross':
                        role = f.rt.team._role(f.worker)
                        f.rt.team.receive(f.task, f'@{role.name} new direction', request_id='acp-cross-role')
                    else:
                        notice = f.rt.team._send(f.task, f.rt.team._role(f.manager), f.worker, 'notice', 'notification')
                        peer = f.rt.log.emit(type='team.run_started', actor=HOST, task_id=f.task, payload={
                            'message_seq': notice.seq, 'role': f.worker, 'run_id': 'unknown-effect', 'revision': 0})
                        f.rt.log.emit(type='team.tool_started', actor=HOST, task_id=f.task, payload={
                            **peer.payload, 'name': 'workspace_write', 'call_id': 'unknown', 'arguments': {}})
                        f.rt.log.emit(type='team.run_finished', actor=HOST, task_id=f.task, payload={**peer.payload, 'status': 'interrupted'})
                    (f.root / 'peer-release').write_text('go', encoding='utf-8')
                    thread.join(15)
                    self.assertFalse(thread.is_alive())
                self.assertEqual(errors, [])
                finished, = [e for e in f.events('team.run_finished') if e.payload.get('message_seq') == candidate.seq]
                self.assertEqual(finished.payload['status'], 'interrupted')
                self.assertEqual(f.events('team.objective_completed'), [])
                self.assertTrue(f.rt.verify_chain().ok)
