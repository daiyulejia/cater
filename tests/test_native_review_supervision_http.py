"""Explicit native supervision through real local HTTP workers; no model service."""
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import threading
import unittest
from unittest.mock import patch

from cater import native_transport
from cater.activity_owner import activity_owner
from cater.anthropic import AnthropicMessagesDriver
from cater.drivers import OpenAICompatibleDriver
from cater.review_supervision_candidate import create
from cater.runtime import Runtime
from cater.team import HOST
from cater.tool_protocol import ToolTurn
from cater.work import WorkConflict
import test_native_review_return_evidence as native_fixture
import test_native_response_evidence as native_source_fixture
import test_review_supervision_candidate as candidate_fixture


class NativeReviewSupervisionHTTPTests(unittest.TestCase):
    def test_cross_role_source_reaches_both_real_native_requests(self):
        for cls in (OpenAICompatibleDriver, AnthropicMessagesDriver):
            with self.subTest(protocol=cls.__name__):
                helper = native_source_fixture.NativeResponseEvidenceTests(methodName='runTest')
                helper.setUp()
                self.addCleanup(helper.doCleanups)
                original = helper.fixture
                sent = []
                def with_input(**kwargs):
                    f = original(**kwargs)
                    worker = f.rt.team._role(f.worker)
                    message = f.rt.team.receive(f.task, f'@{worker.name} 另外需要离线支持。', request_id='native-source-cross')
                    sender = candidate_fixture.ReviewSupervisionCandidateTests(methodName='runTest')
                    sender.setUp()
                    self.addCleanup(sender.doCleanups)
                    sender.run_calls(f, message, ('reply_to_customer', {'text': '已收到。'}), statuses=('completed',))
                    sent.append(message.payload['text'])
                    return f
                with patch.object(helper, 'fixture', side_effect=with_input):
                    f, original_run, _, _ = helper.run_report(cls)
                candidate = create(f.rt.team, f.task, run_seq=original_run[2].seq, run_hash=original_run[2].hash)
                self.assertIsNotNone(candidate)
                with patch.object(self, 'ready', return_value=(f, candidate)):
                    _, _, _, requests = self.execute(cls, 'wait')
                self.assertEqual(len(requests), 1)
                self.assertIn(sent[0], json.dumps(requests[0], ensure_ascii=False))
                self.assertIn('不证明没有新要求', json.dumps(requests[0], ensure_ascii=False))

    def test_committed_delivery_settles_before_independent_customer_message(self):
        for cls in (OpenAICompatibleDriver, AnthropicMessagesDriver):
            with self.subTest(protocol=cls.__name__):
                f, candidate, claimed, requests = self.execute(cls, 'post-finish')
                self.assertEqual(len(requests), 1)
                delivery, = f.events('team.objective_completed')
                finishes = [e for e in f.events('team.tool_finished') if e.payload.get('name') == 'finish_work'
                            and e.payload.get('run_id') == claimed[2].payload['run_id']]
                self.assertEqual(len(finishes), 1)
                reply, = [e for e in f.events('msg.said') if e.payload.get('delivery_seq') == delivery.seq]
                self.assertEqual(reply.payload['text'], delivery.payload['summary'])
                settled, = [e for e in f.events('team.run_finished') if e.payload.get('message_seq') == candidate.seq]
                self.assertEqual(settled.payload['status'], 'completed')
                next_claim = f.rt.team._claim(f.task, human_only=True)
                self.assertIsNotNone(next_claim)
                self.assertEqual(next_claim[1].id, f.worker)
                class OfflineDriver:
                    def tool_turn(self, **kwargs):
                        return ToolTurn(text='Independent reply', calls=[], tokens=0,
                                        meta={'token_usage': 'unavailable'}, message={'role': 'assistant', 'content': 'Independent reply'})
                f.rt.drivers[next_claim[1].driver] = OfflineDriver()
                list(f.rt.team._run_message(f.task, *next_claim))
                self.assertTrue(any(e.payload.get('text') == 'Independent reply' and e.payload.get('reply_to') == next_claim[0].seq
                                    for e in f.events('msg.said')))
                self.assertEqual(len(f.events('team.objective_completed')), 1)
                self.assertTrue(f.rt.verify_chain().ok)

    def test_wait_and_delegate_both_orders(self):
        for cls in (OpenAICompatibleDriver, AnthropicMessagesDriver):
            for mode in ('wait-delegate', 'delegate-wait'):
                with self.subTest(protocol=cls.__name__, mode=mode):
                    f, candidate, claimed, requests = self.execute(cls, mode)
                    self.assertEqual(len(requests), 1)
                    self.assertTrue(any(e.payload.get('kind') == 'assignment' and e.seq > candidate.seq
                                        for e in f.events('team.message_received')))
                    finish, = [e for e in f.events('team.run_finished') if e.payload.get('message_seq') == candidate.seq]
                    self.assertEqual(finish.payload['status'], 'waiting')
                    self.assertNotEqual(finish.payload.get('interruption_kind'), 'review_supervision_empty_wait')

    def test_direct_owned_entry_rejects_expired_lease(self):
        for cls in (OpenAICompatibleDriver, AnthropicMessagesDriver):
            with self.subTest(protocol=cls.__name__):
                f, candidate = self.ready(cls)
                with f.rt.team.recovery.review_candidate_lease(f.task, candidate_seq=candidate.seq, candidate_hash=candidate.hash) as claimed:
                    self.assertIsNotNone(claimed)
                    with activity_owner(f.rt.log.path, f.task, 'simple-team') as competitor:
                        self.assertFalse(competitor)
                with activity_owner(f.rt.log.path, f.task, 'simple-team') as available:
                    self.assertTrue(available)
                before = f.rt.log.count()
                with self.assertRaises(WorkConflict):
                    list(f.rt.team._run_message_owned(f.task, *claimed))
                self.assertEqual(f.rt.log.count(), before)

    def ready(self, cls, *, consultation=False):
        helper = native_fixture.NativeReviewReturnEvidenceTests(methodName='runTest')
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        f, claimed, _ = helper.run_fixture(cls, consultation=consultation)
        run = claimed[2]
        candidate = create(f.rt.team, f.task, run_seq=run.seq, run_hash=run.hash)
        self.assertIsNotNone(candidate)
        return f, candidate

    def execute(self, cls, mode, *, consultation=False, budget=None, untrusted=False, unclean=False, source_cls=None):
        f, candidate = self.ready(source_cls or cls, consultation=consultation)
        arrived, release = threading.Event(), threading.Event()
        requests, errors = [], []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append(payload)
                if mode in {'cross', 'unknown', 'active'}:
                    arrived.set()
                    if not release.wait(8):
                        self.send_error(500)
                        return
                name = ('finish_work' if mode in {'finish', 'post-finish', 'cross', 'unknown', 'unclean', 'atomic-cross', 'atomic-unknown', 'atomic-direction', 'claimed-direction'} else
                        'delegate' if mode == 'delegate' else 'wait_for_reply' if mode in {'wait', 'active'} else
                        'read_checkpoint' if mode == 'read' else None)
                args = ({'summary': 'Offline verified delivery', 'verification': 'Local fixture only'} if name == 'finish_work' else
                        {'recipient': f.worker, 'instruction': 'Continue the existing work'} if name == 'delegate' else {})
                calls = [(name, args)] if name else []
                if mode in {'wait-delegate', 'delegate-wait'}:
                    calls = [('wait_for_reply', {}), ('delegate', {'recipient': f.worker, 'instruction': 'Continue the existing work'})]
                    if mode == 'delegate-wait':
                        calls.reverse()
                if cls is OpenAICompatibleDriver:
                    msg = {'role': 'assistant', 'content': 'Internal observation.'}
                    if calls:
                        msg['tool_calls'] = [{'id': f'call-{len(requests)}-{index}', 'type': 'function',
                                              'function': {'name': tool, 'arguments': json.dumps(arguments)}}
                                             for index, (tool, arguments) in enumerate(calls)]
                    data = {'choices': [{'finish_reason': 'tool_calls' if calls else 'stop', 'message': msg}],
                            'usage': {'prompt_tokens': 5, 'completion_tokens': 2, 'total_tokens': 7}}
                else:
                    blocks = [{'type': 'text', 'text': 'Internal observation.'}]
                    for index, (tool, arguments) in enumerate(calls):
                        blocks.append({'type': 'tool_use', 'id': f'call-{len(requests)}-{index}', 'name': tool, 'input': arguments})
                    data = {'type': 'message', 'role': 'assistant', 'stop_reason': 'tool_use' if calls else 'end_turn',
                            'content': blocks, 'usage': {'input_tokens': 5, 'output_tokens': 2}}
                raw = json.dumps(data).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                try:
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError):
                    pass
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server_thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01})
        server_thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server_thread.join, 5)
        self.addCleanup(server.shutdown)
        driver = cls(f.driver.name, endpoint=f'http://127.0.0.1:{server.server_port}/fixture',
                     model='offline-fixture', api_key_env='CATER_OFFLINE_SUPERVISION', timeout=8,
                     token_budget=budget, tool_response_corrections=0)
        if untrusted == 'bound':
            driver.tool_turn = driver.tool_turn
        elif untrusted:
            driver._tool_result = lambda *args: None
        f.rt.drivers[driver.name] = driver
        original_scope = native_transport.ProcessScope
        class UncleanScope:
            def __init__(self, process):
                self.scope = original_scope(process)
            def close(self):
                self.scope.close()
                raise OSError('fixture cleanup failure')
        def inject():
            try:
                if not arrived.wait(8):
                    raise AssertionError('HTTP request did not reach local server')
                with Runtime(f.rt.log.path, roles_path=f.roles, drivers={f.driver.name: f.driver},
                             close_drivers_on_close=False) as rt:
                    if mode == 'cross':
                        worker = rt.team._role(f.worker)
                        rt.team.receive(f.task, f'@{worker.name} new customer instruction', request_id='native-supervision-cross')
                    else:
                        notice = rt.team._send(f.task, rt.team._role(f.manager), f.worker, 'notice', 'notification')
                        run = rt.log.emit(type='team.run_started', task_id=f.task, actor=HOST, payload={
                            'message_seq': notice.seq, 'role': f.worker, 'run_id': 'unknown-native-supervision', 'revision': 0})
                        if mode == 'unknown':
                            rt.log.emit(type='team.tool_started', task_id=f.task, actor=HOST, payload={
                                **run.payload, 'name': 'workspace_write', 'call_id': 'unknown', 'arguments': {}})
                            rt.log.emit(type='team.run_finished', task_id=f.task, actor=HOST,
                                        payload={**run.payload, 'status': 'interrupted'})
            except BaseException as exc:
                errors.append(exc)
            finally:
                release.set()
        injector = threading.Thread(target=inject) if mode in {'cross', 'unknown', 'active'} else None
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {'CATER_OFFLINE_SUPERVISION': 'fixture-only'}))
            if unclean:
                stack.enter_context(patch.object(native_transport, 'ProcessScope', UncleanScope))
            if mode in {'atomic-cross', 'atomic-unknown', 'atomic-direction'}:
                original_invoke = f.rt.team._invoke_tool
                def invoke(task_id, role, started, revision, call, schema, workspace, binding, tool_start, assignment, **kwargs):
                    if call.name == 'finish_work':
                        if mode == 'atomic-cross':
                            worker = f.rt.team._role(f.worker)
                            f.rt.team.receive(f.task, f'@{worker.name} new direction', request_id='native-supervision-atomic')
                        elif mode == 'atomic-direction':
                            f.rt.command(task_id, 'Update direction', goal='A new objective')
                        else:
                            notice = f.rt.team._send(f.task, f.rt.team._role(f.manager), f.worker, 'notice', 'notification')
                            other = f.rt.log.emit(type='team.run_started', task_id=f.task, actor=HOST, payload={
                                'message_seq': notice.seq, 'role': f.worker, 'run_id': 'native-atomic-unknown', 'revision': 0})
                            f.rt.log.emit(type='team.tool_started', task_id=f.task, actor=HOST, payload={
                                **other.payload, 'name': 'workspace_write', 'call_id': 'unknown', 'arguments': {}})
                            f.rt.log.emit(type='team.run_finished', task_id=f.task, actor=HOST,
                                          payload={**other.payload, 'status': 'interrupted'})
                    return original_invoke(task_id, role, started, revision, call, schema, workspace, binding, tool_start, assignment, **kwargs)
                stack.enter_context(patch.object(f.rt.team, '_invoke_tool', side_effect=invoke))
            if mode == 'post-finish':
                dispatch = f.rt.team._dispatch_tool
                def after_dispatch(*args, **kwargs):
                    tool_start, result = dispatch(*args, **kwargs)
                    if tool_start.payload['name'] == 'finish_work' and result.get('status') == 'ok':
                        worker = f.rt.team._role(f.worker)
                        f.rt.team.receive(f.task, f'@{worker.name} independent new input', request_id='post-finish-input')
                    return tool_start, result
                stack.enter_context(patch.object(f.rt.team, '_dispatch_tool', side_effect=after_dispatch))
            claimed = stack.enter_context(f.rt.team.recovery.review_candidate_lease(
                f.task, candidate_seq=candidate.seq, candidate_hash=candidate.hash))
            self.assertIsNotNone(claimed)
            if mode == 'claimed-direction':
                f.rt.command(f.task, 'Update direction', goal='A new objective')
            if injector:
                injector.start()
            try:
                list(f.rt.team._run_message(f.task, *claimed))
                if mode == 'post-finish':
                    before = f.rt.log.count()
                    list(f.rt.team._run_message_owned(f.task, *claimed))
                    self.assertEqual(f.rt.log.count(), before)
            finally:
                release.set()
                if injector:
                    injector.join(10)
                    self.assertFalse(injector.is_alive())
        self.assertEqual(errors, [])
        return f, candidate, claimed, requests

    def test_finish_delegate_text_empty_wait_and_bounded_reads(self):
        for cls in (OpenAICompatibleDriver, AnthropicMessagesDriver):
            for mode in ('finish', 'delegate', 'text', 'wait', 'read'):
                with self.subTest(protocol=cls.__name__, mode=mode):
                    f, candidate, claimed, requests = self.execute(cls, mode)
                    self.assertEqual(len(requests), 3 if mode == 'read' else 1)
                    wire = requests[0].get('tools', [])
                    names = {item.get('name') if cls is AnthropicMessagesDriver else item['function']['name'] for item in wire}
                    self.assertIn('finish_work', names)
                    self.assertIn('delegate', names)
                    self.assertFalse(names & {'reply_to_customer', 'workspace_write', 'workspace_run', 'publish_artifact', 'request_review'})
                    finish, = [e for e in f.events('team.run_finished') if e.payload.get('message_seq') == candidate.seq]
                    self.assertEqual(finish.payload['status'], 'completed' if mode == 'finish' else 'waiting' if mode == 'delegate' else 'interrupted')
                    if mode in {'text', 'wait', 'read'}:
                        self.assertEqual(finish.payload['interruption_kind'], 'review_supervision_empty_wait' if mode == 'wait' else 'review_supervision_no_action')
                    if mode == 'finish':
                        delivery, = f.events('team.objective_completed')
                        customer = [e for e in f.events('msg.said') if e.payload.get('run_id') == claimed[2].payload['run_id']
                                    and e.payload.get('audience') == 'customer']
                        self.assertEqual(len(customer), 1)
                        self.assertEqual(customer[0].payload.get('delivery_seq'), delivery.seq)
                    else:
                        self.assertEqual(f.events('team.objective_completed'), [])
                        self.assertFalse([e for e in f.events('msg.said') if e.payload.get('run_id') == claimed[2].payload['run_id']
                                          and e.payload.get('audience') == 'customer'])
                    if mode == 'delegate':
                        self.assertTrue(any(e.seq > candidate.seq and e.payload.get('kind') == 'assignment' for e in f.events('team.message_received')))
                    self.assertEqual(len([e for e in f.events('team.message_received') if e.payload.get('trigger') == 'review_return_candidate']), 1)
                    self.assertEqual(f.rt.team.recovery.prepare(f.task), [])
                    self.assertTrue(f.rt.verify_chain().ok)

    def test_prior_consultation_answer_is_not_repeated(self):
        for cls in (OpenAICompatibleDriver, AnthropicMessagesDriver):
            with self.subTest(protocol=cls.__name__):
                f, _, claimed, requests = self.execute(cls, 'text', consultation=True)
                self.assertEqual(len(requests), 1)
                answers = [e for e in f.events('msg.said') if e.payload.get('audience') == 'customer'
                           and e.payload.get('text') == '复核流程是读取文件并登记意见。']
                self.assertEqual(len(answers), 1)
                self.assertFalse([e for e in f.events('msg.said') if e.payload.get('run_id') == claimed[2].payload['run_id']
                                  and e.payload.get('audience') == 'customer'])

    def test_empty_wait_mentions_other_active_role_without_claiming_wakeup(self):
        for cls in (OpenAICompatibleDriver, AnthropicMessagesDriver):
            with self.subTest(protocol=cls.__name__):
                f, candidate, _, requests = self.execute(cls, 'active')
                self.assertEqual(len(requests), 1)
                finish, = [e for e in f.events('team.run_finished') if e.payload.get('message_seq') == candidate.seq]
                self.assertEqual(finish.payload['status'], 'interrupted')
                self.assertEqual(finish.payload['interruption_kind'], 'review_supervision_empty_wait')
                self.assertIn('其他角色仍在处理', finish.payload['reason'])
                self.assertEqual(f.events('team.objective_completed'), [])

    def test_different_native_protocol_cannot_consume_claimed_source(self):
        for source, current in ((OpenAICompatibleDriver, AnthropicMessagesDriver),
                                (AnthropicMessagesDriver, OpenAICompatibleDriver)):
            with self.subTest(source=source.__name__, current=current.__name__):
                f, candidate, _, requests = self.execute(current, 'finish', source_cls=source)
                self.assertEqual(requests, [])
                finish, = [e for e in f.events('team.run_finished') if e.payload.get('message_seq') == candidate.seq]
                self.assertEqual(finish.payload['status'], 'interrupted')
                self.assertEqual(f.events('team.objective_completed'), [])

    def test_new_input_unknown_budget_cleanup_and_untrusted_paths(self):
        for cls in (OpenAICompatibleDriver, AnthropicMessagesDriver):
            for mode in ('cross', 'unknown', 'atomic-cross', 'atomic-unknown', 'budget', 'unclean', 'untrusted', 'bound'):
                with self.subTest(protocol=cls.__name__, mode=mode):
                    f, candidate, _, requests = self.execute(cls, mode, budget=0 if mode == 'budget' else None,
                        untrusted='bound' if mode == 'bound' else mode == 'untrusted', unclean=mode == 'unclean')
                    self.assertEqual(len(requests), 0 if mode in {'budget', 'untrusted', 'bound'} else 1)
                    finish, = [e for e in f.events('team.run_finished') if e.payload.get('message_seq') == candidate.seq]
                    self.assertEqual(finish.payload['status'], 'interrupted')
                    self.assertEqual(f.events('team.objective_completed'), [])
                    if mode.startswith('atomic-'):
                        tool, = [e for e in f.events('team.tool_finished') if e.payload.get('name') == 'finish_work']
                        self.assertEqual(tool.payload['result']['status'], 'error')
                    self.assertTrue(f.rt.verify_chain().ok)

    def test_direction_change_after_claim_or_at_atomic_commit(self):
        for cls in (OpenAICompatibleDriver, AnthropicMessagesDriver):
            for mode in ('claimed-direction', 'atomic-direction'):
                with self.subTest(protocol=cls.__name__, mode=mode):
                    f, candidate, _, requests = self.execute(cls, mode)
                    self.assertEqual(len(requests), 0 if mode == 'claimed-direction' else 1)
                    self.assertEqual(f.events('team.objective_completed'), [])
                    finish, = [e for e in f.events('team.run_finished') if e.payload.get('message_seq') == candidate.seq]
                    self.assertEqual(finish.payload['status'], 'interrupted')
                    self.assertTrue(any(e.actor.kind == 'human' for e in f.events('human.commanded')))
                    if mode == 'atomic-direction':
                        tool, = [e for e in f.events('team.tool_finished') if e.payload.get('name') == 'finish_work']
                        self.assertEqual(tool.payload['result']['status'], 'error')


if __name__ == '__main__':
    unittest.main()
