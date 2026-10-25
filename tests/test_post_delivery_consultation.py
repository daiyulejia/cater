"""Offline post-delivery conversations through real native/external tool dispatch."""

import copy
from contextlib import contextmanager
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

from cater.events import Actor
from cater.mcp import CALL, McpService
from cater.runtime import Runtime
from cater.team import HOST
from cater.team_reply import _message_input, committed_reply, customer_source
from cater.workspace_tools import WorkspaceExecutor
from cater.workspaces import WorkspaceService
import test_simple_team as native
import test_team_external as external


MODES = (False, True)
WAIT = ('wait_for_reply', {})
RETURN = ('return_work', {'text': 'The existing source says 17 + 29 = 46.'})


class PostDeliveryConsultationTests(unittest.TestCase):
    def fixture(self, use_external=False, *, old_request=False, done=True):
        f = external.TeamExternalTests() if use_external else native.SimpleTeamTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        f.peer = next(r.id for r in f.rt.worker_roles if r.id != f.worker)
        project = f.root / 'project'
        project.mkdir()
        (project / 'source.py').write_text('print(17 + 29)\n', encoding='utf-8')
        config = f.root / 'workspaces.json'
        config.write_text(json.dumps({'workspaces': [{'id': 'project', 'title': 'Project',
            'root': str(project), 'mode': 'execute',
            'commands': {'python': [sys.executable, '-I', '-B']}, 'timeout': 5}]}), encoding='utf-8')
        f.rt.workspaces = WorkspaceService(f.rt, config)
        f.rt.workspaces.bind(f.task, 'project', expected_revision=None)
        if old_request:
            f.old_request = self.delegate(f)
            self.exchange(f, WAIT, source=f.old_request)
        published, = self.exchange(f, ('publish_artifact', {
            'filename': 'source.py', 'content': 'print(17 + 29)\n',
            'summary': 'Original source', 'format': 'code'}))
        self.assertEqual(published['status'], 'ok', published)
        f.artifact = published['artifact_seq']
        if not done:
            return f
        result, = self.exchange(f, ('finish_work', {
            'summary': 'Original delivery', 'verification': 'Offline fixture only'}))
        self.assertEqual(result['status'], 'ok', result)
        f.delivery, = f.events('team.objective_completed')
        f.snapshot = self.snapshot(f)
        return f

    def message(self, f, seq):
        return next(e for e in f.rt.team._messages(f.task) if e.seq == seq)

    def exchange(self, f, *calls, source=None, role=None, before=None, final=None,
                 statuses=('completed', 'waiting'), expect_model=True):
        """Claim real queued input; never fabricate a run or bypass _current."""
        if source is None:
            target = f.rt.team._role(role or f.manager)
            source = f.rt.team.receive(f.task, '@' + target.name + ' Check existing evidence.')
        claimed = f.rt.team._claim(f.task)
        self.assertIsNotNone(claimed)
        message, target, started = claimed
        self.assertEqual(message.seq, source.seq)
        observed = []
        script_errors = []
        if hasattr(f.driver, 'agent_run'):
            def script(run):
                observed.append('model')
                if before:
                    before(run.context, run.tools)
                for name, args in calls:
                    run.call(name, **args)
                if final is not None:
                    return final
                run.call('wait_for_reply')
            f.driver.scripts[target.id] = [script]
        else:
            def script(context, history, tools):
                observed.append('model')
                try:
                    if before:
                        before(context, tools)
                    return native.turn(final if final is not None else '', *calls)
                except AssertionError as error:
                    script_errors.append(error)
                    raise
            f.driver.steps[target.id] = [script, native.turn('', WAIT)]
        owns_scheduler = hasattr(f.driver, 'agent_run') and not f.rt.team.external.scheduling(f.task)
        if owns_scheduler:
            f.rt.team.external.begin_scheduler(f.task)
        try:
            list(f.rt.team._run_message(f.task, message, target, started))
        finally:
            if owns_scheduler:
                f.rt.team.external.end_scheduler()
        ended, = [e for e in f.events('team.run_finished')
                  if e.payload['run_id'] == started.payload['run_id']]
        if script_errors:
            raise script_errors[0]
        self.assertEqual(observed, ['model'] if expect_model else [], ended.payload)
        self.assertEqual(getattr(f.driver, 'errors', []), [])
        self.assertIn(ended.payload['status'], statuses, ended.payload)
        self.assertTrue(f.rt.verify_chain().ok)
        return [e.payload['result'] for e in f.events('team.tool_finished')
                if e.payload['run_id'] == started.payload['run_id']
                and e.payload['name'] != 'wait_for_reply']

    def delegate(self, f, *, role=None, recipient=None, source=None, ordinary=False):
        name, args = ('send_message', {'recipient': recipient or f.worker, 'text': 'Inspect existing source.'}) if ordinary else (
            'delegate', {'recipient': recipient or f.worker, 'instruction': 'Inspect existing source and return evidence.'})
        result, = self.exchange(f, (name, args), role=role, source=source)
        self.assertEqual(result['status'], 'ok', result)
        return self.message(f, result['message_seq'])

    def withdraw(self, f, source, *, role=None):
        result, = self.exchange(f, ('withdraw_work', {
            'message_seq': source.seq, 'reason': 'This consultation is no longer needed.'}), role=role)
        return result

    def reports(self, f, source):
        return [e for e in f.rt.team._messages(f.task)
                if e.payload['kind'] == 'report' and e.payload.get('reply_to') == source.seq]

    def snapshot(self, f):
        artifacts = copy.deepcopy(f.rt.task(f.task).deliverables)
        return (f.rt.task(f.task).goal, f.rt.team.view(f.task)['objective'],
                [(e.seq, e.hash, copy.deepcopy(e.payload)) for e in f.events('team.objective_completed')],
                artifacts, {a['seq']: hashlib.sha256(Path(a['local_path']).read_bytes()).hexdigest()
                            for a in artifacts}, (f.root / 'project' / 'source.py').read_bytes(),
                [e.hash for e in f.events('team.objective_updated')],
                [e.hash for e in f.events('team.tool_started')
                 if e.payload['name'] in {'set_objective', 'finish_work'}])

    def assert_frozen(self, f):
        self.assertEqual(f.rt.team.view(f.task)['status'], 'done')
        self.assertEqual(f.rt.team.view(f.task)['revision'], f.delivery.seq)
        self.assertEqual(self.snapshot(f), f.snapshot)
        self.assertEqual(sorted(p.name for p in (f.root / 'project').iterdir()), ['source.py'])
        self.assertTrue(f.rt.verify_chain().ok)

    def assert_returned(self, f, source, result):
        self.assertEqual(result['status'], 'ok', result)
        report, = self.reports(f, source)
        self.assertEqual(report.seq, result['message_seq'])
        self.assertEqual(report.actor.id, source.payload['recipient'])
        self.assertEqual(report.payload['recipient'], source.actor.id)
        self.assertEqual(report.payload['revision'], f.delivery.seq)
        return report

    def test_delegate_read_return_then_customer_reply_preserves_delivery(self):
        for mode in MODES:
            for ordinary, output in ((False, 'tool'), (True, 'tool'), (False, 'final'), (True, 'final')):
                with self.subTest(external=mode, ordinary=ordinary, output=output):
                    f = self.fixture(mode)
                    customer_count = len([m for m in f.rt.team._messages(f.task) if m.actor.kind == 'human'])
                    source = self.delegate(f, ordinary=ordinary)
                    customer = f.events('team.message_received')[-2]
                    self.assertEqual(customer.actor, Actor.human())
                    read, returned = self.exchange(f, ('read_artifact', {'artifact_seq': f.artifact}), RETURN, source=source)
                    self.assertEqual(read['status'], 'ok', read)
                    self.assertIn('17 + 29', json.dumps(read))
                    report = self.assert_returned(f, source, returned)
                    if output == 'tool':
                        replied, = self.exchange(f, ('reply_to_customer', {'text': RETURN[1]['text']}), source=report)
                        self.assertEqual(replied['status'], 'ok', replied)
                        self.assertEqual(replied['reply_to'], customer.seq)
                    else:
                        self.assertEqual(self.exchange(f, source=report, final=RETURN[1]['text']), [])
                    self.assertEqual(len([m for m in f.rt.team._messages(f.task) if m.actor.kind == 'human']), customer_count + 1)
                    answer = f.events('msg.said')[-1]
                    self.assertEqual(answer.payload['audience'], 'customer')
                    self.assertEqual(answer.payload['reply_to'], customer.seq)
                    self.assertNotIn('delivery_seq', answer.payload)
                    self.assert_frozen(f)
                    if mode:
                        f.assert_receipt_chain()

    def test_two_hop_consultation_returns_child_then_original(self):
        for mode in MODES:
            with self.subTest(external=mode):
                f = self.fixture(mode)
                parent = self.delegate(f)
                customer = f.events('team.message_received')[-2]
                child = self.delegate(f, source=parent, recipient=f.peer)
                self.assertEqual(child.payload['parent_assignment_seq'], parent.seq)
                read, returned = self.exchange(f, ('read_artifact', {'artifact_seq': f.artifact}), RETURN, source=child)
                self.assertEqual(read['status'], 'ok', read)
                feedback = self.assert_returned(f, child, returned)
                def inspect_child_feedback(context, tools):
                    self.assertNotIn('reply_to_customer', {tool['name'] for tool in tools})
                returned, = self.exchange(f, RETURN, source=feedback, before=inspect_child_feedback)
                report = self.assert_returned(f, parent, returned)
                result, = self.exchange(f, ('reply_to_customer', {'text': RETURN[1]['text']}), source=report)
                self.assertEqual(result['status'], 'ok', result)
                self.assertEqual(result['reply_to'], customer.seq)
                self.assertEqual(f.rt.team.recovery.assignments(f.task), [])
                self.assert_frozen(f)

    def test_wait_then_continue_returns_original_request(self):
        for mode in MODES:
            with self.subTest(external=mode):
                f = self.fixture(mode)
                source = self.delegate(f)
                self.exchange(f, WAIT, source=source, statuses=('waiting',))
                continued, = self.exchange(f, ('continue_work', {
                    'assignment_seq': source.seq, 'instruction': 'Read the original source and return.'}))
                self.assertEqual(continued['status'], 'ok', continued)
                message = self.message(f, continued['message_seq'])
                self.assertEqual(message.payload['continue_assignment_seq'], source.seq)
                def inspect(context, tools):
                    self.assertEqual(context['active_assignment']['seq'], source.seq)
                returned, = self.exchange(f, RETURN, source=message, before=inspect)
                report = self.assert_returned(f, source, returned)
                events = f.rt.log.read(task_id=f.task)
                self.assertEqual(_message_input(f.rt.team, f.task, report, events,
                                                {event.seq: event for event in events}), message)
                self.assertEqual(self.reports(f, message), [])
                self.assertEqual(len([m for m in f.rt.team._messages(f.task)
                                      if m.payload['kind'] == 'assignment']), 1)
                self.assert_frozen(f)

    def test_running_consultation_uses_current_objective_and_never_crosses_root_revision(self):
        for mode in MODES:
            for scope in ('revision_zero', 'established_goal', 'goal_changed_in_root_input'):
                with self.subTest(external=mode, scope=scope):
                    f = self.fixture(mode, done=False)
                    if scope == 'established_goal':
                        changed, = self.exchange(f, ('set_objective', {'goal': 'An existing active objective'}))
                        self.assertEqual(changed['status'], 'ok', changed)
                    frozen = self.snapshot(f)
                    if scope == 'goal_changed_in_root_input':
                        changed, delegated = self.exchange(f,
                            ('set_objective', {'goal': 'A new engineering objective'}),
                            ('delegate', {'recipient': f.worker, 'instruction': 'Work under the new objective.'}))
                        self.assertEqual(changed['status'], 'ok', changed)
                        self.assertEqual(delegated['status'], 'ok', delegated)
                        source = self.message(f, delegated['message_seq'])
                    else:
                        source = self.delegate(f)
                    customer = f.events('team.message_received')[-2]
                    self.assertEqual(customer.actor, Actor.human())
                    revision = f.rt.team.view(f.task)['revision']
                    self.assertEqual(source.payload['revision'], revision)
                    if scope == 'revision_zero':
                        self.assertEqual(revision, 0)
                    elif scope == 'goal_changed_in_root_input':
                        self.assertLess(customer.seq, revision)
                    else:
                        self.assertGreater(customer.seq, revision)
                    def inspect_worker(context, tools):
                        self.assertNotIn('reply_to_customer', {tool['name'] for tool in tools})
                    read, result = self.exchange(f, ('read_artifact', {'artifact_seq': f.artifact}), RETURN,
                                                 source=source, before=inspect_worker)
                    self.assertEqual(read['status'], 'ok', read)
                    self.assertEqual(result['status'], 'ok', result)
                    report, = self.reports(f, source)
                    self.assertEqual(report.seq, result['message_seq'])
                    self.assertEqual(report.payload['revision'], revision)
                    reply = ('reply_to_customer', {'text': RETURN[1]['text']})
                    if scope == 'goal_changed_in_root_input':
                        public = [e.hash for e in f.events('msg.said') if e.payload.get('audience') == 'customer']
                        def inspect_report(context, tools):
                            self.assertNotIn('reply_to_customer', {tool['name'] for tool in tools})
                        results = self.exchange(f, reply, source=report, before=inspect_report,
                                                statuses=('failed', 'interrupted'))
                        self.assertEqual(results, [])
                        self.assertEqual([e.hash for e in f.events('msg.said')
                                          if e.payload.get('audience') == 'customer'], public)
                        self.assertEqual(self.snapshot(f)[2:6], frozen[2:6])
                    else:
                        replied, = self.exchange(f, reply, source=report)
                        self.assertEqual(replied['status'], 'ok', replied)
                        self.assertEqual(replied['reply_to'], customer.seq)
                        answer = f.events('msg.said')[-1]
                        self.assertEqual(answer.payload['audience'], 'customer')
                        self.assertEqual(answer.payload['reply_to'], customer.seq)
                        self.assertNotIn('delivery_seq', answer.payload)
                        self.assertEqual(self.snapshot(f), frozen)
                    self.assertEqual(f.rt.team.view(f.task)['status'], 'running')
                    self.assertEqual(f.rt.team.view(f.task)['revision'], revision)
                    self.assertEqual(f.events('team.objective_completed'), [])
                    self.assertFalse([e for e in f.events('team.tool_started') if e.payload['name'] == 'finish_work'])

    def test_original_sender_withdraws_current_assignment_and_plain_request(self):
        for mode in MODES:
            for ordinary in (False, True):
                with self.subTest(external=mode, ordinary=ordinary):
                    f = self.fixture(mode)
                    source = self.delegate(f, role=f.worker, recipient=f.peer, ordinary=ordinary)
                    result = self.withdraw(f, source, role=f.worker)
                    self.assertEqual(result['status'], 'ok', result)
                    self.assertTrue(result['withdrawn'])
                    row, = f.rt.team.view(f.task)['withdrawals']
                    self.assertEqual(row['source_hash'], source.hash)
                    self.assertEqual(row['revision'], f.delivery.seq)
                    self.assertEqual(row['requester'], f.worker)
                    self.assertEqual(self.reports(f, source), [])
                    self.assertIsNone(f.rt.team._claim(f.task))
                    self.assert_frozen(f)

    def test_leader_can_withdraw_another_senders_consultation(self):
        for mode in MODES:
            with self.subTest(external=mode):
                f = self.fixture(mode)
                source = self.delegate(f, role=f.worker, recipient=f.peer)
                result = self.withdraw(f, source)
                self.assertEqual(result['status'], 'ok', result)
                row, = f.rt.team.view(f.task)['withdrawals']
                self.assertEqual((row['sender'], row['requester']), (f.worker, f.manager))
                self.assertEqual(f.rt.team.recovery.assignments(f.task), [])
                self.assertEqual(self.reports(f, source), [])
                self.assert_frozen(f)

    def test_withdrawal_survives_reopen_without_running_receiver(self):
        for mode in MODES:
            with self.subTest(external=mode):
                f = self.fixture(mode)
                source = self.delegate(f)
                result = self.withdraw(f, source)
                self.assertEqual(result['status'], 'ok', result)
                snapshot = f.rt.log.read(task_id=f.task)
                db = f.rt.log.path
                f.rt.close()
                with Runtime(db, roles_path=f.roles, drivers={f.driver.name: f.driver}) as rt:
                    row, = rt.team.view(f.task)['withdrawals']
                    self.assertEqual(row['withdrawal_seq'], result['withdrawal_seq'])
                    self.assertEqual(rt.team.work_control.view(f.task, events=snapshot), [row])
                    self.assertEqual(rt.team.view(f.task)['status'], 'done')
                    self.assertEqual(rt.team.recovery.assignments(f.task), [])
                    self.assertIsNone(rt.team._claim(f.task))
                    self.assertTrue(rt.verify_chain().ok)

    def test_running_receiver_stops_at_tool_boundary_after_real_withdrawal(self):
        for mode in MODES:
            with self.subTest(external=mode):
                f = self.fixture(mode)
                source = self.delegate(f)
                def withdraw_during_model(context, tools):
                    self.assertEqual(context['active_assignment']['seq'], source.seq)
                    self.assertEqual(self.withdraw(f, source)['status'], 'ok')
                results = self.exchange(f, RETURN, source=source, before=withdraw_during_model,
                                        statuses=('interrupted',))
                self.assertEqual(results, [])
                self.assertEqual(self.reports(f, source), [])
                self.assertEqual(f.events('team.review_recorded'), [])
                self.assert_frozen(f)

    def test_repeated_withdrawal_is_idempotent_without_false_return(self):
        for mode in MODES:
            with self.subTest(external=mode):
                f = self.fixture(mode)
                source = self.delegate(f)
                first = self.withdraw(f, source)
                again = self.withdraw(f, source)
                self.assertEqual(first['status'], 'ok', first)
                self.assertTrue(again['already_withdrawn'])
                self.assertEqual(first['withdrawal_seq'], again['withdrawal_seq'])
                self.assertEqual(len(f.events('team.assignment_withdrawn')), 1)
                self.assertEqual(self.reports(f, source), [])
                self.assertIsNone(f.rt.team._claim(f.task))
                self.assert_frozen(f)

    def test_reported_work_is_not_withdrawn_or_continued_again(self):
        for mode in MODES:
            with self.subTest(external=mode):
                f = self.fixture(mode)
                source = self.delegate(f)
                returned, = self.exchange(f, RETURN, source=source)
                report = self.assert_returned(f, source, returned)
                self.exchange(f, WAIT, source=report)
                result = self.withdraw(f, source)
                self.assertTrue(result['already_reported'])
                self.assertFalse(result['withdrawn'])
                continued, = self.exchange(f, ('continue_work', {
                    'assignment_seq': source.seq, 'instruction': 'Do not reopen returned work.'}))
                self.assertEqual(continued['status'], 'error', continued)
                self.assertEqual(len(self.reports(f, source)), 1)
                self.assertEqual(f.events('team.assignment_withdrawn'), [])
                self.assert_frozen(f)

    def test_receiver_and_unrelated_role_cannot_withdraw(self):
        for mode in MODES:
            with self.subTest(external=mode):
                f = self.fixture(mode)
                source = self.delegate(f, role=f.worker, recipient=f.peer)
                other = next(r.id for r in f.rt.worker_roles if r.id not in {f.worker, f.peer})
                for role in (f.peer, other):
                    result = self.withdraw(f, source, role=role)
                    self.assertEqual(result.get('code'), 'withdrawal_forbidden', result)
                self.assertEqual(f.events('team.assignment_withdrawn'), [])
                self.assertEqual(self.reports(f, source), [])
                self.assert_frozen(f)

    def test_only_original_sender_can_continue_even_when_leader(self):
        for mode in MODES:
            with self.subTest(external=mode):
                f = self.fixture(mode)
                source = self.delegate(f, role=f.worker, recipient=f.peer)
                self.exchange(f, WAIT, source=source)
                for role in (f.peer, f.manager):
                    result, = self.exchange(f, ('continue_work', {
                        'assignment_seq': source.seq, 'instruction': 'Wrong sender.'}), role=role)
                    self.assertEqual(result['status'], 'error', result)
                self.assertFalse([m for m in f.rt.team._messages(f.task) if m.payload['kind'] == 'continuation'])
                self.assert_frozen(f)

    def test_pre_completion_revision_cannot_be_withdrawn_or_continued(self):
        for mode in MODES:
            with self.subTest(external=mode):
                f = self.fixture(mode, old_request=True)
                self.assertLess(f.old_request.payload['revision'], f.delivery.seq)
                withdrawn = self.withdraw(f, f.old_request)
                self.assertEqual(withdrawn.get('code'), 'work_not_current', withdrawn)
                continued, = self.exchange(f, ('continue_work', {
                    'assignment_seq': f.old_request.seq, 'instruction': 'Old goal must remain closed.'}))
                self.assertEqual(continued['status'], 'error', continued)
                self.assertEqual(f.events('team.assignment_withdrawn'), [])
                self.assertEqual(self.reports(f, f.old_request), [])
                self.assert_frozen(f)

    def test_queued_and_running_consultations_cannot_be_duplicated_by_continue(self):
        for mode in MODES:
            with self.subTest(external=mode):
                f = self.fixture(mode)
                source = self.delegate(f)
                call = ('continue_work', {'assignment_seq': source.seq, 'instruction': 'Do not duplicate active work.'})
                queued, = self.exchange(f, call)
                self.assertEqual(queued['status'], 'error', queued)
                def inspect_running(context, tools):
                    running, = self.exchange(f, call)
                    self.assertEqual(running['status'], 'error', running)
                returned, = self.exchange(f, RETURN, source=source, before=inspect_running)
                self.assert_returned(f, source, returned)
                self.assertFalse([m for m in f.rt.team._messages(f.task) if m.payload['kind'] == 'continuation'])
                self.assert_frozen(f)

    def test_withdrawn_work_cannot_be_continued_or_late_returned(self):
        for mode in MODES:
            with self.subTest(external=mode):
                f = self.fixture(mode)
                source = self.delegate(f)
                self.assertEqual(self.withdraw(f, source)['status'], 'ok')
                continued, = self.exchange(f, ('continue_work', {
                    'assignment_seq': source.seq, 'instruction': 'Must not revive withdrawn work.'}))
                self.assertEqual(continued['status'], 'error', continued)
                returned, = self.exchange(f, ('send_message', {
                    'recipient': f.manager, 'reply_to': source.seq, 'text': 'Late return must be refused.'}), role=f.worker)
                self.assertEqual(returned.get('code'), 'reply_work_inactive', returned)
                self.assertEqual(self.reports(f, source), [])
                self.assertEqual(len(f.events('team.assignment_withdrawn')), 1)
                self.assert_frozen(f)

    def test_foreign_group_and_customer_sources_cannot_be_withdrawn_or_continued(self):
        for mode in MODES:
            with self.subTest(external=mode):
                f = self.fixture(mode)
                other = f.rt.create_task('Isolated foreign group')
                f.rt.team.enable(other)
                foreign = f.rt.team._send(other, f.rt.team._role(f.manager), f.worker, 'Foreign request', 'assignment')
                customer = f.events('team.message_received')[0]
                for source in (foreign, customer):
                    result = self.withdraw(f, source)
                    self.assertEqual(result.get('code'), 'invalid_work', result)
                    result, = self.exchange(f, ('continue_work', {
                        'assignment_seq': source.seq, 'instruction': 'Invalid source.'}))
                    self.assertEqual(result['status'], 'error', result)
                self.assertEqual(f.events('team.assignment_withdrawn'), [])
                self.assert_frozen(f)

    def test_paused_consultation_tools_are_unavailable_and_cannot_mutate(self):
        for mode in MODES:
            for name in ('delegate', 'continue_work', 'withdraw_work'):
                with self.subTest(external=mode, tool=name):
                    f = self.fixture(mode)
                    source = self.delegate(f)
                    f.rt.execution_control.change(f.task, 'pause', request_id='pause-consultation')
                    message_count = len(f.events('team.message_received'))
                    args = {'delegate': {'recipient': f.worker, 'instruction': 'Paused'},
                            'continue_work': {'assignment_seq': source.seq, 'instruction': 'Paused'},
                            'withdraw_work': {'message_seq': source.seq, 'reason': 'Paused'}}[name]
                    def inspect(context, tools):
                        self.assertNotIn(name, {t['name'] for t in tools})
                    results = self.exchange(f, (name, args), before=inspect,
                                            statuses=('failed', 'interrupted'))
                    self.assertEqual(results, [])
                    self.assertEqual(len(f.events('team.message_received')), message_count + 1)
                    self.assertEqual(f.events('team.assignment_withdrawn'), [])
                    self.assert_frozen(f)

    def test_new_control_transition_stops_inflight_consultation(self):
        for mode in MODES:
            with self.subTest(external=mode):
                f = self.fixture(mode)
                source = self.delegate(f)
                def pause(context, tools):
                    f.rt.execution_control.change(f.task, 'pause', request_id='pause-inflight')
                results = self.exchange(f, RETURN, source=source, before=pause, statuses=('interrupted',))
                self.assertEqual(results, [])
                self.assertEqual(self.reports(f, source), [])
                self.assert_frozen(f)
        for mode in MODES:
            for incoming in ('human', 'report'):
                for change in ('pause', 'objective', 'rules', 'binding'):
                    with self.subTest(external=mode, incoming=incoming, change=change):
                        f = self.fixture(mode)
                        report = None
                        if incoming == 'report':
                            source = self.delegate(f)
                            returned, = self.exchange(f, RETURN, source=source)
                            report = self.assert_returned(f, source, returned)
                        previous = f.rt.team.recovery.scope(f.task)
                        public = [e.hash for e in f.events('msg.said') if e.payload.get('audience') == 'customer']
                        armed, changed = [], []
                        def mutate():
                            changed.append(change)
                            if change == 'pause':
                                f.rt.execution_control.change(f.task, 'pause', request_id='pause-before-final')
                            elif change == 'objective':
                                f.rt.log.emit(type='team.objective_updated', task_id=f.task, actor=HOST,
                                              payload={'goal': 'A concurrently adopted goal', 'run_id': 'other-host-run'})
                            elif change == 'rules':
                                f.rt.rulebook.record('Explain only the newly agreed scope.', scope='task', task_id=f.task)
                            else:
                                f.rt.workspaces.bind(f.task, None, expected_revision=f.rt.workspaces.view(f.task)['revision'])
                        def model_returned(context, tools):
                            armed.append(True)
                        atomic = f.rt.log.atomic
                        @contextmanager
                        def final_entry():
                            # Model usage has committed, but final's transaction
                            # has not entered. Recheck and emit must share it.
                            if armed and not changed and f.rt.log.read(task_id=f.task)[-1].type == 'team.model_called':
                                mutate()
                            with atomic():
                                yield
                        finish = f.rt.team.external_runs.finish
                        def settled(*args, **kwargs):
                            result = finish(*args, **kwargs)
                            if not changed:
                                mutate()
                            return result
                        target, name, hook = ((f.rt.team.external_runs, 'finish', settled) if mode
                                              else (f.rt.log, 'atomic', final_entry))
                        with patch.object(target, name, hook):
                            results = self.exchange(f, source=report, before=model_returned,
                                final='This stale final must be suppressed.', statuses=('interrupted',))
                        self.assertEqual(results, [])
                        self.assertEqual(armed, [True])
                        self.assertEqual(changed, [change])
                        field = {'pause': 'control_seq', 'objective': 'revision',
                                 'rules': 'rule_fingerprint', 'binding': 'workspace_revision'}[change]
                        self.assertNotEqual(f.rt.team.recovery.scope(f.task)[field], previous[field])
                        self.assertEqual([e.hash for e in f.events('msg.said')
                                          if e.payload.get('audience') == 'customer'], public)
                        self.assertFalse([e for e in f.events('msg.said')
                                          if e.payload.get('text') == 'This stale final must be suppressed.'])
                        self.assertEqual(self.snapshot(f)[2:6], f.snapshot[2:6])
                        self.assertEqual(self.snapshot(f)[7], f.snapshot[7])

    def test_invalid_delegate_arguments_fail_schema_without_sending(self):
        for mode in MODES:
            with self.subTest(external=mode):
                f = self.fixture(mode)
                before = len(f.rt.team._messages(f.task))
                results = self.exchange(f, ('delegate', {'recipient': f.worker}),
                                        ('delegate', {'recipient': f.worker, 'instruction': 'Check', 'extra': True}))
                self.assertEqual(len(results), 2)
                for result in results:
                    self.assertEqual(result.get('code'), 'invalid_arguments', result)
                    self.assertEqual(result.get('outcome'), 'not_started')
                self.assertEqual(len(f.rt.team._messages(f.task)), before + 1)
                self.assertFalse([m for m in f.rt.team._messages(f.task) if m.payload['kind'] == 'assignment'])
                self.assert_frozen(f)

    def test_all_engineering_mutations_remain_blocked_by_actual_receipts(self):
        for mode in MODES:
            with self.subTest(external=mode):
                f = self.fixture(mode)
                calls = [('workspace_write', {'path': 'forbidden.py', 'content': 'changed', 'expected_sha256': None}),
                         ('publish_artifact', {'filename': 'new.txt', 'content': 'new', 'summary': 'new', 'format': 'text'}),
                         ('publish_workspace_file', {'path': 'source.py', 'summary': 'new', 'format': 'code'}),
                         ('publish_workspace_archive', {'paths': ['source.py'], 'filename': 'new.zip', 'summary': 'new'}),
                         ('request_review', {'reviewer': f.worker, 'artifact_seqs': [f.artifact], 'instruction': 'Formal review'}),
                         ('request_review', {'reviewer': f.worker, 'workspace_file': {'path': 'source.py', 'format': 'code'},
                                             'instruction': 'Formal workspace review'}),
                         ('delegate', {'recipient': f.worker, 'instruction': 'Formal review',
                                       'review': {'artifact_seqs': [f.artifact]}}),
                         ('delegate', {'recipient': f.worker, 'instruction': 'Formal workspace review',
                                       'review': {'workspace_file': {'path': 'source.py', 'format': 'code'}}}),
                         ('record_review', {'review_seq': f.delivery.seq, 'verdict': 'approved', 'feedback': 'No approval'})]
                for call in calls:
                    with self.subTest(tool=call[0], arguments=call[1]):
                        with patch.object(f.rt.team_reviews, '_guard', wraps=f.rt.team_reviews._guard) as review_guard, \
                             patch.object(f.rt.team, '_publication_guard', wraps=f.rt.team._publication_guard) as publication_guard:
                            result, = self.exchange(f, call)
                        if call[0] == 'delegate' or call[0] == 'request_review' and 'workspace_file' in call[1]:
                            review_guard.assert_called_once()
                            publication_guard.assert_not_called()
                        self.assertEqual(result['status'], 'error', result)
                        if call[0] != 'delegate':
                            self.assertIn('\u5df2\u4ea4\u4ed8', result['message'])
                self.assertEqual(f.events('team.review_requested'), [])
                self.assertEqual(f.events('team.review_recorded'), [])
                self.assertFalse([m for m in f.rt.team._messages(f.task) if m.payload['kind'] == 'assignment'])
                self.assert_frozen(f)

    def test_mcp_remains_blocked_without_contacting_bridge(self):
        for mode in MODES:
            with self.subTest(external=mode):
                f = self.fixture(mode)
                config = f.root / 'mcp.json'
                config.write_text(json.dumps({'bridge_command': [sys.executable, '-B'], 'servers': [{
                    'id': 'notes', 'title': 'Offline notes', 'description': 'Offline fixture',
                    'command': [sys.executable, '-B'], 'cwd': str(f.root),
                    'tools': {'write_note': 'write'}, 'roles': [f.manager, f.worker]}]}), encoding='utf-8')
                pool = Mock(spec=['exchange', 'invalidate', 'close'])
                f.rt.mcp.close()
                f.rt.mcp = McpService(f.rt, config, pool=pool)
                result, = self.exchange(f, (CALL, {'server_id': 'notes', 'tool_name': 'write_note',
                    'schema_sha256': 'a' * 64, 'arguments': {'text': 'Forbidden'}}))
                self.assertEqual(result['status'], 'error', result)
                self.assertIn('\u5df2\u4ea4\u4ed8', result['message'])
                pool.exchange.assert_not_called()
                self.assert_frozen(f)

    def test_actual_formal_review_assignment_remains_protected_from_withdrawal(self):
        for mode in MODES:
            with self.subTest(external=mode):
                # Formal review can only be created while running. Use its real
                # request, not a forged completed-revision review or patched guard.
                f = self.fixture(mode, done=False)
                requested, = self.exchange(f, ('request_review', {
                    'reviewer': f.worker, 'artifact_seqs': [f.artifact], 'instruction': 'Inspect exact source.'}))
                self.assertEqual(requested['status'], 'ok', requested)
                source = self.message(f, requested['message_seq'])
                withdrawn = self.withdraw(f, source)
                self.assertEqual(withdrawn.get('code'), 'formal_review', withdrawn)
                self.assertEqual(f.rt.team_reviews.view(f.task)[0]['status'], 'pending')
                self.assertEqual(f.events('team.assignment_withdrawn'), [])
                self.assertEqual(f.events('team.review_recorded'), [])
                self.assertEqual(f.events('team.objective_completed'), [])

    def test_delegation_never_grants_worker_postdelivery_command_authority(self):
        for mode in MODES:
            with self.subTest(external=mode):
                f = self.fixture(mode)
                source = self.delegate(f)
                with patch.object(WorkspaceExecutor, '_run') as execute:
                    result, = self.exchange(f, ('workspace_run', {'command': 'python', 'args': ['source.py']}), source=source)
                    self.assertEqual(result['status'], 'error', result)
                    execute.assert_not_called()
                self.assertEqual(self.reports(f, source), [])
                self.assert_frozen(f)

    def test_unknown_effect_survives_withdrawal_and_blocks_another_command(self):
        for mode in MODES:
            with self.subTest(external=mode):
                f = self.fixture(mode)
                source = self.delegate(f)
                with patch.object(WorkspaceExecutor, '_run', side_effect=OSError('Offline uncertain executor')) as execute:
                    result, = self.exchange(f, ('workspace_run', {'command': 'python', 'args': ['source.py']}))
                    execute.assert_called_once()
                self.assertEqual(result.get('outcome'), 'effects_unknown', result)
                pending = [e.seq for e in f.rt.workspaces.pending(task_id=f.task)]
                self.assertEqual(len(pending), 1)
                self.assertEqual(self.withdraw(f, source)['status'], 'ok')
                self.assertEqual([e.seq for e in f.rt.workspaces.pending(task_id=f.task)], pending)
                with patch.object(WorkspaceExecutor, '_run') as execute:
                    result, = self.exchange(f, ('workspace_run', {'command': 'python', 'args': ['source.py']}))
                    self.assertEqual(result['status'], 'error', result)
                    execute.assert_not_called()
                self.assertEqual([e.seq for e in f.rt.workspaces.pending(task_id=f.task)], pending)
                self.assertEqual(self.reports(f, source), [])
                self.assert_frozen(f)

    def test_report_reply_requires_authentic_current_customer_chain(self):
        cases = ('spoof_report', 'unpaired_return', 'unpaired_request', 'wrong_role',
                 'newer_human', 'newer_other_role_human', 'new_goal', 'unreturned', 'unknown_effect', 'historical_reply',
                 'unpaired_return_final', 'wrong_role_final', 'child_final', 'unknown_effect_final', 'retargeted_report',
                 'newer_other_role_human_final')
        for mode in MODES:
            for variant in cases:
                with self.subTest(external=mode, case=variant):
                    final_fallback = variant.endswith('_final')
                    case = variant.removesuffix('_final')
                    f = self.fixture(mode)
                    if case == 'retargeted_report':
                        first = self.delegate(f)
                        second = self.delegate(f)
                        root = next(e for e in reversed(f.rt.team._messages(f.task))
                                    if e.actor == Actor.human() and e.seq < second.seq)
                        returned, = self.exchange(f, RETURN, source=first)
                        report = self.assert_returned(f, first, returned)
                        original = f.rt.log.read(task_id=f.task)
                        # Keep every genuine HOST pair intact. Only the report's
                        # target changes in memory to another authentic request.
                        forged = replace(report, payload={**report.payload, 'reply_to': second.seq})
                        attacked = [forged if e.seq == report.seq else e for e in original]
                        returned, = self.exchange(f, RETURN, source=second)
                        actual = self.assert_returned(f, second, returned)
                        authentic = f.rt.log.read(task_id=f.task)
                        leader = f.rt.team._role(f.manager)
                        with patch.object(f.rt.log, 'read', side_effect=AssertionError('Snapshot resolution read live state')):
                            self.assertIsNone(customer_source(f.rt.team, f.task, forged, leader, events=attacked))
                            self.assertEqual(customer_source(f.rt.team, f.task, actual, leader, events=authentic), root)
                        self.assertEqual(self.message(f, report.seq), report)
                        self.assertEqual(self.message(f, report.seq).payload['reply_to'], first.seq)
                        self.assert_frozen(f)
                        continue
                    reply = ('reply_to_customer', {'text': 'Only an authentic returned consultation may answer.'})
                    if case == 'unpaired_request':
                        # An injected message has no paired successful dispatch.
                        # Its receiver still uses the real return_work tool.
                        source = f.rt.team._send(f.task, f.rt.team._role(f.manager), f.worker,
                                                 'Untrusted request fixture', 'assignment')
                    elif case == 'child':
                        parent = self.delegate(f)
                        source = self.delegate(f, source=parent, recipient=f.peer)
                    elif case == 'unknown_effect':
                        with patch.object(WorkspaceExecutor, '_run', side_effect=OSError('Offline uncertain executor')):
                            delegated, unknown = self.exchange(f, ('delegate', {
                                'recipient': f.worker, 'instruction': 'Inspect existing source.'}),
                                ('workspace_run', {'command': 'python', 'args': ['source.py']}))
                        self.assertEqual(delegated['status'], 'ok', delegated)
                        self.assertEqual(unknown.get('outcome'), 'effects_unknown', unknown)
                        pending_before = list(f.rt.workspaces.pending(task_id=f.task))
                        self.assertEqual(len(pending_before), 1)
                        source = self.message(f, delegated['message_seq'])
                    else:
                        source = self.delegate(f)
                    if case in {'newer_human', 'newer_other_role_human'}:
                        self.exchange(f, WAIT, role=f.peer if case == 'newer_other_role_human' else f.manager)
                    if case in {'spoof_report', 'unpaired_return'}:
                        self.exchange(f, WAIT, source=source)
                        sender = f.peer if case == 'spoof_report' else f.worker
                        role = f.rt.team._role(sender)
                        # Deliberately forged report, never a fabricated success receipt.
                        report = f.rt.log.emit(type='team.message_received', task_id=f.task,
                            actor=Actor.agent(role.id, role.role), payload={
                                'kind': 'report', 'text': 'Untrusted report fixture', 'recipient': f.manager,
                                'reply_to': source.seq, 'revision': f.delivery.seq, 'parent_assignment_seq': None})
                    elif case == 'unreturned':
                        report = source
                    else:
                        returned, = self.exchange(f, RETURN, source=source)
                        report = self.assert_returned(f, source, returned)
                    if case == 'wrong_role':
                        self.exchange(f, WAIT, source=report)
                        report = f.rt.log.emit(type='team.message_received', task_id=f.task, actor=report.actor,
                                               payload={**report.payload, 'recipient': f.peer})
                    if case == 'newer_other_role_human':
                        root = next(e for e in reversed(f.rt.team._messages(f.task))
                                    if e.actor == Actor.human() and e.payload['recipient'] == f.manager and e.seq < source.seq)
                        other = next(e for e in reversed(f.rt.team._messages(f.task))
                                     if e.actor == Actor.human() and e.payload['recipient'] == f.peer)
                        results = self.exchange(f, *([] if final_fallback else [reply]), source=report,
                                                final=reply[1]['text'] if final_fallback else None)
                        answer = f.events('msg.said')[-1]
                        self.assertEqual(answer.actor.id, f.manager)
                        self.assertEqual(answer.payload['audience'], 'customer')
                        self.assertEqual(answer.payload['reply_to'], root.seq)
                        self.assertNotEqual(answer.payload['reply_to'], other.seq)
                        self.assertNotIn('delivery_seq', answer.payload)
                        if final_fallback:
                            self.assertEqual(results, [])
                        else:
                            result, = results
                            self.assertEqual(result['status'], 'ok', result)
                            self.assertEqual(result['reply_to'], root.seq)
                        self.assert_frozen(f)
                        continue
                    if case == 'unknown_effect':
                        operations_before = copy.deepcopy(f.rt.team._pending_operations(f.task))
                        customer = next(e for e in reversed(f.rt.team._messages(f.task))
                                        if e.actor == Actor.human() and e.seq < source.seq)
                        text = 'The earlier command has unknown effects. I can explain existing evidence; execution remains pending.'
                        def inspect_pending(context, tools):
                            self.assertIn('reply_to_customer', {tool['name'] for tool in tools})
                            self.assertIn(pending_before[0].seq,
                                          {row['started_seq'] for row in context['pending_operations']})
                        results = self.exchange(f,
                            *([] if final_fallback else [('reply_to_customer', {'text': text})]),
                            source=report, before=inspect_pending, final=text if final_fallback else None,
                            statuses=('completed',))
                        answer = f.events('msg.said')[-1]
                        self.assertEqual(answer.payload['text'], text)
                        self.assertEqual(answer.payload['audience'], 'customer')
                        self.assertEqual(answer.payload['reply_to'], customer.seq)
                        self.assertNotIn('delivery_seq', answer.payload)
                        if final_fallback:
                            self.assertEqual(results, [])
                        else:
                            result, = results
                            self.assertEqual(result['status'], 'ok', result)
                            self.assertEqual(result['message_seq'], answer.seq)
                            self.assertEqual(result['reply_to'], customer.seq)
                        self.assertEqual(f.rt.workspaces.pending(task_id=f.task), pending_before)
                        self.assertEqual(f.rt.team._pending_operations(f.task), operations_before)
                        with patch.object(WorkspaceExecutor, '_run') as execute:
                            blocked, = self.exchange(f, ('workspace_run', {'command': 'python', 'args': ['source.py']}))
                            self.assertEqual(blocked['status'], 'error', blocked)
                            execute.assert_not_called()
                        self.assertEqual(f.rt.workspaces.pending(task_id=f.task), pending_before)
                        self.assertEqual(f.rt.team._pending_operations(f.task), operations_before)
                        for kind in ('team.review_requested', 'team.review_recorded', 'team.assignment_withdrawn',
                                     'workspace.tool_reconciled', 'team.tool_reconciled'):
                            self.assertEqual(f.events(kind), [])
                        self.assert_frozen(f)
                        continue
                    if case == 'historical_reply':
                        result, = self.exchange(f, reply, source=report)
                        self.assertEqual(result['status'], 'ok', result)
                        answer = f.events('msg.said')[-1]
                        run = next(e for e in f.events('team.run_started')
                                   if e.payload['run_id'] == answer.payload['run_id'])
                        before = f.rt.log.read(task_id=f.task)
                        self.assertEqual(committed_reply(f.rt.team, run, before), answer)
                        self.assert_frozen(f)
                        f.rt.execution_control.change(f.task, 'pause', request_id='pause-after-committed-reply')
                        self.assertEqual(committed_reply(f.rt.team, run, f.rt.log.read(task_id=f.task)), answer)
                        self.assertEqual(f.events('msg.said')[-1], answer)
                        f.rt.execution_control.change(f.task, 'resume', request_id='resume-after-committed-reply')
                        changed, = self.exchange(f, ('set_objective', {'goal': 'A later independent task'}))
                        self.assertEqual(changed['status'], 'ok', changed)
                        self.assertEqual(committed_reply(f.rt.team, run, f.rt.log.read(task_id=f.task)), answer)
                        continue
                    if case == 'new_goal':
                        changed, = self.exchange(f, ('set_objective', {'goal': 'A later independent task'}))
                        self.assertEqual(changed['status'], 'ok', changed)
                    public_before = [e.hash for e in f.events('msg.said') if e.payload.get('audience') == 'customer']
                    declared = []
                    def inspect(context, tools):
                        declared.extend(tool['name'] for tool in tools)
                    results = self.exchange(f, *([] if final_fallback else [reply]), source=report, before=inspect,
                                            final=RETURN[1]['text'] if final_fallback else None,
                                            statuses=('completed', 'waiting', 'failed', 'interrupted'),
                                            expect_model=case != 'new_goal')
                    self.assertEqual(results, [])
                    self.assertNotIn('reply_to_customer', declared)
                    self.assertEqual([e.hash for e in f.events('msg.said')
                                      if e.payload.get('audience') == 'customer'], public_before)
                    if case != 'new_goal':
                        self.assert_frozen(f)
                    else:
                        self.assertEqual(f.events('team.objective_completed'), [f.delivery])
                    if case == 'child':
                        self.assertEqual(self.reports(f, parent), [])


if __name__ == '__main__':
    unittest.main()
