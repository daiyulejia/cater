"""Review follow-up contracts: deterministic native turns and local HTTP, no remote AI."""

from contextlib import contextmanager
import json
import sys
import unittest
from unittest.mock import patch

from cater.events import EVENT_CATEGORIES
from cater.runtime import Runtime
from cater.team import HOST, MAX_ACTOR_TURNS, _reported_messages
from cater.tool_protocol import anthropic_history, openai_history, prepare_tool_turn
import test_protocol_usage as protocol
import test_simple_team as simple
import test_team_review_cycle as cycle
from test_team_response_correction import wire_tool


PUBLIC_REPLY = "I checked the submitted bytes; approved / 通过, but no formal decision was recorded."
PRIVATE = "PRIVATE_EXCEPTION_PATH_OR_PROVIDER_BODY"


class TeamReviewFollowupTests(unittest.TestCase):
    def fixture(self):
        f = cycle.TeamReviewCycleTests()
        f.setUp()
        self.addCleanup(f.doCleanups)

        def checked_turn(*, role, prompt, history, tools):
            system, transcript, _, _ = prepare_tool_turn(
                role=role, prompt=prompt, history=history, tools=tools)
            anthropic_history(prompt, transcript)
            openai_history(system, prompt, transcript)
            return simple.TeamDriver.tool_turn(f.driver, role=role, prompt=prompt,
                                               history=history, tools=tools)

        # The older cycle helper requires every assistant turn in history. A prose
        # follow-up intentionally carries that reply in context, avoiding prefill.
        f.driver.tool_turn = checked_turn
        return f

    def prepare(self, f):
        artifact = f.publish("exact fixture bytes\n")
        request = f.request([artifact])
        self.assertEqual(request['status'], 'ok')
        source, = [e for e in f.rt.team._messages(f.task) if e.seq == request['message_seq']]
        return request, source

    def start(self, f, source, *steps):
        role = f.rt.team._role(source.payload['recipient'])
        f.driver.steps[role.id] = list(steps)
        started = f.rt.log.emit(type='team.run_started', actor=HOST, task_id=f.task, payload={
            'message_seq': source.seq, 'role': role.id,
            'run_id': f'followup-fixture-{len(f.events("team.run_started"))}',
            'revision': f.rt.team.view(f.task)['revision'],
            'control_seq': f.rt.execution_control.view(f.task)['seq'],
        })
        running = f.rt.team._run_message(f.task, source, role, started)
        self.addCleanup(running.close)
        return running, started

    def finish(self, f, started):
        finished, = [e for e in f.events('team.run_finished')
                     if e.payload['run_id'] == started.payload['run_id']]
        self.assertTrue(f.rt.verify_chain().ok)
        return finished

    def assert_open(self, f, request, source):
        row, = [r for r in f.rt.team_reviews.view(f.task) if r['request_seq'] == request['review_seq']]
        self.assertEqual(row['status'], 'pending')
        self.assertNotIn(source.seq, _reported_messages(f.rt.team._messages(f.task)))
        self.assertIn(source.seq, [r['seq'] for r in f.rt.team.recovery.assignments(f.task)])
        self.assertEqual(f.events('team.review_recorded'), [])
        self.assertEqual(f.events('team.objective_completed'), [])

    def assert_no_new_work(self, f, request):
        self.assertEqual([e.seq for e in f.events('team.review_requested')], [request['review_seq']])
        self.assertEqual(len(f.rt.task(f.task).deliverables), 1)
        self.assertEqual(f.results('delegate'), [])

    @contextmanager
    def loopback(self, cls, replies):
        f = self.fixture()
        request, source = self.prepare(f)
        if callable(replies):
            replies = replies(request)
        with protocol.ProtocolUsageTests().session(cls, replies) as (_, driver, requests):
            f.rt.drivers['team_test'] = driver
            yield f, request, source, driver, requests

    def test_wrong_roles_get_fixed_reviewer_mismatch_without_proxy_signing(self):
        f = self.fixture()
        request, source = self.prepare(f)
        for role in (f.worker, f.manager, f.observer):
            with self.subTest(role=role):
                before = len(f.events('team.tool_finished'))
                calls_before = len(f.driver.calls)
                result = f.record(request['review_seq'], reviewer=role)
                self.assertEqual(set(result), {'status', 'code', 'review_seq', 'expected_reviewer',
                                               'complete', 'outcome', 'message'})
                self.assertEqual({k: result[k] for k in result if k != 'message'}, {
                    'status': 'error', 'code': 'reviewer_mismatch', 'review_seq': request['review_seq'],
                    'expected_reviewer': f.reviewer, 'complete': True, 'outcome': 'not_started'})
                self.assertIs(result['complete'], True)
                for wording in (f.reviewer, '代签', '发消息', '复核', '重新发布'):
                    self.assertIn(wording, result['message'])
                receipts = f.events('team.tool_finished')[before:]
                self.assertEqual(sum(e.payload['name'] == 'record_review' for e in receipts), 1)
                self.assertEqual(len(f.driver.calls) - calls_before, 2)
                self.assert_open(f, request, source)
        self.assertEqual(f.events('team.review_followup_requested'), [])
        self.assertEqual(f.record(request['review_seq'], source=source)['status'], 'ok')
        self.assertEqual(len(f.events('team.review_recorded')), 1)

    def test_arbitrary_tool_exceptions_remain_sanitized(self):
        for error in (ValueError, KeyError, TypeError, OSError):
            with self.subTest(error=error.__name__):
                f = self.fixture()
                request, source = self.prepare(f)
                with patch.object(f.rt.team_reviews, '_find', side_effect=error(PRIVATE)):
                    result = f.record(request['review_seq'])
                self.assertEqual(result['status'], 'error')
                self.assertNotIn(PRIVATE, json.dumps([e.payload for e in f.rt.log.read(task_id=f.task)]))
                self.assertNotEqual(result.get('code'), 'reviewer_mismatch')
                self.assert_open(f, request, source)

    def test_formal_assignment_ignores_reports_without_exact_integer_review_metadata(self):
        f = self.fixture()
        request, source = self.prepare(f)
        reviewer = f.rt.team._role(f.reviewer)
        for metadata in (None, request['review_seq'] + 1000, str(request['review_seq']), True):
            with self.subTest(metadata=metadata):
                f.rt.team._send(f.task, reviewer, f.worker, 'Ordinary approved report', 'report',
                                reply_to=source.seq, review_seq=metadata)
                self.assert_open(f, request, source)
        self.assertEqual(f.record(request['review_seq'], source=source)['status'], 'ok')
        self.assertIn(source.seq, _reported_messages(f.rt.team._messages(f.task)))
        self.assertNotIn(source.seq, [r['seq'] for r in f.rt.team.recovery.assignments(f.task)])
        formal = [e for e in f.rt.team._messages(f.task) if e.payload.get('kind') == 'report'
                  and type(e.payload.get('review_seq')) is int
                  and e.payload['review_seq'] == request['review_seq']]
        self.assertEqual(len(formal), 1)

    def test_second_prose_interrupts_without_inferring_any_verdict(self):
        for text in (PUBLIC_REPLY, 'changes_requested', 'Need additional evidence before deciding.'):
            with self.subTest(text=text):
                f = self.fixture()
                request, source = self.prepare(f)
                before = len(f.driver.calls)
                running, started = self.start(f, source, f.native_turn(text), f.native_turn(text),
                                              f.native_turn('Must not be requested'))
                list(running)
                self.assertEqual(len(f.driver.calls) - before, 2)
                followup, = f.events('team.review_followup_requested')
                model_calls = [e for e in f.events('team.model_called')
                               if e.payload['run_id'] == started.payload['run_id']]
                self.assertEqual(followup.actor, HOST)
                self.assertEqual(followup.payload, {'review_seq': request['review_seq'],
                    'message_seq': source.seq, 'role': f.reviewer,
                    'run_id': started.payload['run_id'], 'model_call_seq': model_calls[0].seq})
                self.assertEqual(EVENT_CATEGORIES['team.review_followup_requested'], 'review')
                self.assertLess(model_calls[0].seq, followup.seq)
                self.assertLess(followup.seq, model_calls[1].seq)
                finished = self.finish(f, started)
                self.assertEqual(finished.payload['status'], 'interrupted')
                self.assertEqual(finished.payload['interruption_kind'], 'review_decision_missing')
                replies = [e for e in f.rt.team._messages(f.task) if e.payload.get('reply_to') == source.seq]
                self.assertEqual([e.payload['kind'] for e in replies], ['progress'])
                self.assert_open(f, request, source)
                self.assert_no_new_work(f, request)

    def test_followup_retains_successful_native_history_and_public_reply_in_context(self):
        f = self.fixture()
        request, source = self.prepare(f)
        read = f.native_turn('', ('read_artifact', {'artifact_seq': f.rt.task(f.task).deliverables[0]['seq']}))
        prose = f.native_turn(PUBLIC_REPLY)
        before = len(f.driver.calls)
        running, started = self.start(f, source, read, prose, f.native_turn('', ('record_review', {
            'review_seq': request['review_seq'], 'verdict': 'changes_requested', 'feedback': 'Concrete fixture defect.'})))
        list(running)
        first, second, third = f.driver.calls[before:]
        self.assertEqual(first['history'], [])
        self.assertEqual(third['history'], second['history'])
        self.assertIn(read.message, third['history'])
        self.assertNotIn(prose.message, third['history'])
        self.assertEqual([m['role'] for m in third['history']], ['assistant', 'tool'])
        reminder = third['context']['review_followup']
        self.assertEqual(reminder['review_seq'], request['review_seq'])
        self.assertEqual(reminder['previous_public_reply'], PUBLIC_REPLY)
        for word in ('record_review', 'approved', 'changes_requested'):
            self.assertIn(word, reminder['instruction'])
        self.assertEqual(third['context']['active_assignment']['seq'], source.seq)
        self.assertEqual(third['context']['message']['seq'], source.seq)
        self.assertEqual(self.finish(f, started).payload['status'], 'waiting')
        self.assertEqual(f.rt.team_reviews.view(f.task)[0]['status'], 'changes_requested')
        self.assert_no_new_work(f, request)

    def test_followup_can_continue_reading_then_record_either_verdict(self):
        for verdict in ('approved', 'changes_requested'):
            with self.subTest(verdict=verdict):
                f = self.fixture()
                request, source = self.prepare(f)
                artifact = f.rt.task(f.task).deliverables[0]
                before = len(f.driver.calls)
                running, started = self.start(f, source, f.native_turn(PUBLIC_REPLY),
                    f.native_turn('', ('read_artifact', {'artifact_seq': artifact['seq']})),
                    f.native_turn('', ('record_review', {'review_seq': request['review_seq'],
                                                       'verdict': verdict, 'feedback': 'Actual fixture bytes checked.'})))
                list(running)
                self.assertEqual(len(f.driver.calls) - before, 3)
                self.assertEqual(len(f.events('team.review_followup_requested')), 1)
                self.assertEqual(f.rt.team_reviews.view(f.task)[0]['status'], verdict)
                self.assertEqual(self.finish(f, started).payload['status'], 'waiting')
                self.assertIn(source.seq, _reported_messages(f.rt.team._messages(f.task)))
                self.assertEqual(len(f.events('team.review_recorded')), 1)
                self.assert_no_new_work(f, request)

    def test_followup_may_wait_without_fabricating_a_decision(self):
        f = self.fixture()
        request, source = self.prepare(f)
        before = len(f.driver.calls)
        running, started = self.start(f, source, f.native_turn(PUBLIC_REPLY),
                                      f.native_turn('', ('wait_for_reply', {})))
        list(running)
        self.assertEqual(len(f.driver.calls) - before, 2)
        self.assertEqual(self.finish(f, started).payload['status'], 'waiting')
        self.assert_open(f, request, source)
        self.assert_no_new_work(f, request)

    def test_followup_may_request_customer_evidence_and_wait(self):
        f = self.fixture()
        request, source = self.prepare(f)
        questions = {'title': 'Confirm the acceptance case', 'questions': [
            {'id': 'case', 'question': 'Which input must be supported?',
             'selection': 'single', 'options': []}]}
        before = len(f.driver.calls)
        running, started = self.start(f, source, f.native_turn(PUBLIC_REPLY),
            f.native_turn('', ('request_questions', questions)), f.native_turn('Do not force a verdict'))
        list(running)
        self.assertEqual(len(f.driver.calls) - before, 2)
        self.assertEqual(self.finish(f, started).payload['status'], 'waiting')
        question, = f.rt.questionnaires.view(f.task)
        self.assertEqual(question['status'], 'pending')
        self.assertEqual(question['role_id'], f.reviewer)
        event, = f.events('team.questions_requested')
        self.assertEqual(event.seq, question['seq'])
        self.assertEqual(event.payload['assignment_seq'], source.seq)
        self.assert_open(f, request, source)
        self.assert_no_new_work(f, request)

    def test_normal_formal_decisions_do_not_request_followup(self):
        for verdict in ('approved', 'changes_requested'):
            with self.subTest(verdict=verdict):
                f = self.fixture()
                request, source = self.prepare(f)
                before = len(f.driver.calls)
                running, started = self.start(f, source, f.native_turn('', ('record_review', {
                    'review_seq': request['review_seq'], 'verdict': verdict, 'feedback': 'Fixture decision.'})))
                list(running)
                self.assertEqual(len(f.driver.calls) - before, 1)
                self.assertEqual(f.events('team.review_followup_requested'), [])
                self.assertEqual(f.rt.team_reviews.view(f.task)[0]['status'], verdict)
                self.assertEqual(self.finish(f, started).payload['status'], 'waiting')

    def test_nonreview_chat_preserves_completion_and_ordinary_report(self):
        f = self.fixture()
        source = f.rt.team._send(f.task, f.rt.team._role(f.worker), f.reviewer,
                                 'Ordinary chat about approved / review', 'message')
        before = len(f.driver.calls)
        running, started = self.start(f, source, f.native_turn(PUBLIC_REPLY))
        list(running)
        self.assertEqual(len(f.driver.calls) - before, 1)
        self.assertEqual(self.finish(f, started).payload['status'], 'completed')
        self.assertEqual(f.events('team.review_followup_requested'), [])
        self.assertIn(source.seq, _reported_messages(f.rt.team._messages(f.task)))

    def test_exact_request_allowance_survives_runtime_reopen_and_continuation(self):
        f = self.fixture()
        request, source = self.prepare(f)
        running, started = self.start(f, source, f.native_turn(PUBLIC_REPLY), f.native_turn(PUBLIC_REPLY))
        list(running)
        self.assertEqual(self.finish(f, started).payload['status'], 'interrupted')
        f.rt.close()
        f.rt = Runtime(f.root / 'test.db', roles_path=f.roles, drivers={'team_test': f.driver})
        self.addCleanup(f.rt.close)
        continuation, = f.exchange(f.worker, ('send_message', {'recipient': f.reviewer,
            'text': 'Continue checking the original exact version.', 'continue_assignment_seq': source.seq}))
        self.assertEqual(continuation['status'], 'ok')
        message = next(e for e in reversed(f.rt.team._messages(f.task))
                       if e.payload.get('continue_assignment_seq') == source.seq)
        before = len(f.driver.calls)
        running, started = self.start(f, message, f.native_turn(PUBLIC_REPLY),
                                      f.native_turn('No second automatic reminder'))
        list(running)
        self.assertEqual(len(f.driver.calls) - before, 1)
        self.assertEqual(len(f.events('team.review_followup_requested')), 1)
        self.assertEqual(self.finish(f, started).payload['interruption_kind'], 'review_decision_missing')
        self.assert_open(f, request, source)
        self.assert_no_new_work(f, request)

    def test_ordinary_reports_do_not_deduplicate_or_block_formal_continuation(self):
        f = self.fixture()
        request, source = self.prepare(f)
        running, _ = self.start(f, source, f.native_turn(PUBLIC_REPLY), f.native_turn(PUBLIC_REPLY))
        list(running)
        first, second = f.exchange(f.reviewer,
            ('send_message', {'recipient': f.worker, 'reply_to': source.seq, 'text': 'First ordinary report'}),
            ('send_message', {'recipient': f.worker, 'reply_to': source.seq, 'text': 'Second ordinary report'}))
        for result in (first, second):
            self.assertEqual(result['status'], 'ok')
            self.assertNotIn('already_reported', result)
        self.assertNotEqual(first['message_seq'], second['message_seq'])
        self.assert_open(f, request, source)
        result, = f.exchange(f.worker, ('send_message', {'recipient': f.reviewer,
            'text': 'Register the original exact review after checking.', 'continue_assignment_seq': source.seq}))
        self.assertEqual(result['status'], 'ok')
        continuation = next(e for e in f.rt.team._messages(f.task) if e.seq == result['message_seq'])
        running, started = self.start(f, continuation, f.native_turn('', ('record_review', {
            'review_seq': request['review_seq'], 'verdict': 'changes_requested', 'feedback': 'Fixture needs correction.'})))
        list(running)
        self.assertEqual(self.finish(f, started).payload['status'], 'waiting')
        reported = _reported_messages(f.rt.team._messages(f.task))
        self.assertTrue({source.seq, continuation.seq}.issubset(reported))
        self.assertFalse({source.seq, continuation.seq} & {
            r['seq'] for r in f.rt.team.recovery.assignments(f.task)})
        reports = [e for e in f.rt.team._messages(f.task) if e.payload.get('kind') == 'report'
                   and e.payload.get('reply_to') == source.seq]
        self.assertEqual(len(reports), 3)
        self.assertEqual(sum(e.payload.get('review_seq') == request['review_seq'] for e in reports), 1)
        self.assertEqual(len(f.events('team.review_followup_requested')), 1)
        self.assert_no_new_work(f, request)

    def test_new_exact_request_gets_its_own_single_allowance(self):
        f = self.fixture()
        first, source = self.prepare(f)
        running, _ = self.start(f, source, f.native_turn(PUBLIC_REPLY), f.native_turn(PUBLIC_REPLY))
        list(running)
        self.assertEqual(f.record(first['review_seq'], 'changes_requested')['status'], 'ok')
        revised = f.publish('revised exact fixture bytes\n')
        second = f.request([revised], review_seq=first['review_seq'])
        self.assertEqual(second['status'], 'ok')
        source = next(e for e in f.rt.team._messages(f.task) if e.seq == second['message_seq'])
        running, _ = self.start(f, source, f.native_turn(PUBLIC_REPLY), f.native_turn('', ('wait_for_reply', {})))
        list(running)
        self.assertEqual([e.payload['review_seq'] for e in f.events('team.review_followup_requested')],
                         [first['review_seq'], second['review_seq']])

    def test_last_of_32_slots_retains_usage_without_appending_a_call(self):
        f = self.fixture()
        request, source = self.prepare(f)
        self.assertEqual(MAX_ACTOR_TURNS, 32)
        turns = [f.native_turn('', ('read_messages', {'limit': 1})) for _ in range(31)]
        turns.append(f.native_turn(PUBLIC_REPLY))
        before = len(f.driver.calls)
        tokens = f.rt._reported_driver_tokens('team_test')
        running, started = self.start(f, source, *turns, f.native_turn('No slot for this'))
        list(running)
        self.assertEqual(len(f.driver.calls) - before, 32)
        self.assertEqual(f.rt._reported_driver_tokens('team_test') - tokens, 32 * 7)
        self.assertEqual(f.events('team.review_followup_requested'), [])
        self.assertEqual(self.finish(f, started).payload['interruption_kind'], 'review_decision_missing')
        self.assert_open(f, request, source)

    def test_budget_exhausted_by_first_reply_retains_usage_without_followup(self):
        f = self.fixture()
        request, source = self.prepare(f)
        tokens = f.rt._reported_driver_tokens('team_test')
        f.driver.token_budget = tokens + 7
        before = len(f.driver.calls)
        running, started = self.start(f, source, f.native_turn(PUBLIC_REPLY), f.native_turn('Over budget'))
        list(running)
        self.assertEqual(len(f.driver.calls) - before, 1)
        self.assertEqual(f.rt._reported_driver_tokens('team_test'), tokens + 7)
        self.assertEqual(f.events('team.review_followup_requested'), [])
        self.assertEqual(self.finish(f, started).payload['interruption_kind'], 'review_decision_missing')
        self.assert_open(f, request, source)

    def test_unknown_effect_at_first_reply_blocks_followup(self):
        f = self.fixture()
        request, source = self.prepare(f)
        before = len(f.driver.calls)
        running, started = self.start(f, source, f.native_turn(PUBLIC_REPLY), f.native_turn('Unsafe followup'))
        with patch.object(f.rt.team, '_pending_operations', return_value=[{'status': 'unknown'}]):
            list(running)
        self.assertEqual(len(f.driver.calls) - before, 1)
        self.assertEqual(f.events('team.review_followup_requested'), [])
        self.assertEqual(self.finish(f, started).payload['status'], 'interrupted')
        self.assert_open(f, request, source)

    def test_outdated_version_is_not_eligible_for_automatic_followup(self):
        f = self.fixture()
        request, source = self.prepare(f)
        f.publish('new bytes not covered by the original request\n')
        before = len(f.driver.calls)
        running, started = self.start(f, source, f.native_turn(PUBLIC_REPLY), f.native_turn('No stale followup'))
        list(running)
        self.assertEqual(len(f.driver.calls) - before, 1)
        self.assertEqual(f.events('team.review_followup_requested'), [])
        self.assertEqual(f.rt.team_reviews.view(f.task)[0]['status'], 'outdated')
        self.assertEqual(f.events('team.review_recorded'), [])
        self.assertNotEqual(self.finish(f, started).payload.get('interruption_kind'), 'review_decision_missing')
        self.assertEqual([e.seq for e in f.events('team.review_requested')], [request['review_seq']])

    def test_changed_control_or_customer_during_first_reply_blocks_followup(self):
        for change in ('pause', 'cancel', 'customer', 'objective'):
            with self.subTest(change=change):
                f = self.fixture()
                request, source = self.prepare(f)
                def changed(context, history, tools):
                    if change in ('pause', 'cancel'):
                        f.rt.execution_control.change(f.task, change, request_id='first-reply-' + change)
                    elif change == 'customer':
                        name = f.rt.team._role(f.reviewer).name
                        f.rt.team.receive(f.task, f'@{name} New customer direction')
                    else:
                        f.rt.log.emit(type='team.objective_updated', actor=HOST, task_id=f.task,
                                      payload={'goal': 'Changed objective'})
                    return f.native_turn(PUBLIC_REPLY)
                before = len(f.driver.calls)
                tokens = f.rt._reported_driver_tokens('team_test')
                running, started = self.start(f, source, changed, f.native_turn('Must not be called'))
                list(running)
                self.assertEqual(len(f.driver.calls) - before, 1)
                self.assertEqual(f.rt._reported_driver_tokens('team_test'), tokens + 7)
                self.assertEqual(f.events('team.review_followup_requested'), [])
                self.assertEqual(self.finish(f, started).payload['status'], 'interrupted')
                self.assertEqual(f.events('team.review_recorded'), [])
                self.assert_no_new_work(f, request)

    def test_scheduled_followup_rechecks_control_customer_budget_and_removal(self):
        for change in ('pause', 'cancel', 'customer', 'objective', 'budget', 'remove'):
            with self.subTest(change=change):
                f = self.fixture()
                request, source = self.prepare(f)
                before = len(f.driver.calls)
                tokens = f.rt._reported_driver_tokens('team_test')
                running, started = self.start(f, source, f.native_turn(PUBLIC_REPLY), f.native_turn('Blocked followup'))
                next(running)
                self.assertEqual(len(f.events('team.review_followup_requested')), 1)
                if change in ('pause', 'cancel'):
                    f.rt.execution_control.change(f.task, change, request_id='scheduled-' + change)
                elif change == 'customer':
                    name = f.rt.team._role(f.reviewer).name
                    f.rt.team.receive(f.task, f'@{name} Use the new customer direction')
                elif change == 'objective':
                    f.rt.log.emit(type='team.objective_updated', actor=HOST, task_id=f.task,
                                  payload={'goal': 'Changed objective'})
                elif change == 'budget':
                    f.driver.token_budget = tokens + 7
                else:
                    f.rt.conversation_list.remove(f.task, request_id='scheduled-remove')
                list(running)
                self.assertEqual(len(f.driver.calls) - before, 1)
                self.assertEqual(f.rt._reported_driver_tokens('team_test'), tokens + 7)
                self.assertEqual(self.finish(f, started).payload['status'], 'interrupted')
                self.assertEqual(f.events('team.review_recorded'), [])
                self.assert_no_new_work(f, request)

    def test_scheduled_followup_rechecks_scope_and_unknown_effects(self):
        for change in ('scope', 'pending'):
            with self.subTest(change=change):
                f = self.fixture()
                request, source = self.prepare(f)
                before = len(f.driver.calls)
                running, started = self.start(f, source, f.native_turn(PUBLIC_REPLY), f.native_turn('Unsafe followup'))
                next(running)
                if change == 'pending':
                    with patch.object(f.rt.team, '_pending_operations', return_value=[{'status': 'unknown'}]):
                        list(running)
                else:
                    original = f.rt.team._model_input
                    def changed(*args, **kwargs):
                        context, workspace, binding, tools, basis = original(*args, **kwargs)
                        context['work_checkpoint']['scope']['rule_fingerprint'] = 'changed-after-first-reply'
                        return context, workspace, binding, tools, basis
                    with patch.object(f.rt.team, '_model_input', side_effect=changed):
                        list(running)
                self.assertEqual(len(f.driver.calls) - before, 1)
                self.assertEqual(self.finish(f, started).payload['status'], 'interrupted')
                self.assert_open(f, request, source)
                self.assert_no_new_work(f, request)

    def test_both_protocols_send_preserved_tool_history_and_context_without_assistant_prefill(self):
        for cls in protocol.CLASSES:
            read = wire_tool(cls, 'read_messages', {'limit': 1}, 'read-before-prose')
            prose = protocol.response(cls)
            if cls is protocol.AnthropicMessagesDriver:
                prose['content'] = [{'type': 'text', 'text': PUBLIC_REPLY}]
                read['content'].insert(0, {'type': 'thinking', 'thinking': 'SIGNED_FIXTURE', 'signature': 'fixture-signature'})
            else:
                prose['choices'][0]['message']['content'] = PUBLIC_REPLY
            with self.subTest(cls=cls.__name__), self.loopback(cls, [(200, read), (200, prose),
                    (200, wire_tool(cls, 'wait_for_reply', {}, 'wait-after-prose'))]) as (f, request, source, _, requests):
                tokens = f.rt._reported_driver_tokens('team_test')
                running, started = self.start(f, source)
                list(running)
                self.assertEqual(len(requests), 3)
                self.assertEqual(len(f.events('team.review_followup_requested')), 1)
                messages = requests[2]['messages']
                self.assertEqual(messages[-1]['role'], 'user' if cls is protocol.AnthropicMessagesDriver else 'tool')
                self.assertEqual(messages[-2:], requests[1]['messages'][-2:])
                body = json.dumps(requests[2], ensure_ascii=False)
                self.assertIn('review_followup', body)
                self.assertIn('previous_public_reply', body)
                self.assertIn(PUBLIC_REPLY, body)
                self.assertIn('read-before-prose', body)
                if cls is protocol.AnthropicMessagesDriver:
                    self.assertIn('SIGNED_FIXTURE', body)
                    self.assertIn('fixture-signature', body)
                calls = [e for e in f.events('team.model_called') if e.payload['run_id'] == started.payload['run_id']]
                self.assertEqual(len(calls), 3)
                self.assertTrue(all(e.payload['usage'] == protocol.usage(cls) for e in calls))
                self.assertTrue(all('response_status' not in e.payload for e in calls))
                self.assertEqual(f.rt._reported_driver_tokens('team_test'), tokens + 42)
                self.assertEqual(self.finish(f, started).payload['status'], 'waiting')
                self.assert_open(f, request, source)
                self.assert_no_new_work(f, request)

    def test_both_protocols_second_prose_keeps_usage_and_interrupts(self):
        for cls in protocol.CLASSES:
            with self.subTest(cls=cls.__name__), self.loopback(cls, [(200, protocol.response(cls))] * 2) as (
                    f, request, source, _, requests):
                tokens = f.rt._reported_driver_tokens('team_test')
                running, started = self.start(f, source)
                list(running)
                self.assertEqual(len(requests), 2)
                self.assertEqual(f.rt._reported_driver_tokens('team_test'), tokens + 28)
                self.assertEqual(self.finish(f, started).payload['interruption_kind'], 'review_decision_missing')
                self.assertEqual(len(f.events('team.review_followup_requested')), 1)
                self.assert_open(f, request, source)
                self.assert_no_new_work(f, request)

    def test_both_protocols_followup_records_either_verdict_against_original_assignment(self):
        for cls in protocol.CLASSES:
            for verdict in ('approved', 'changes_requested'):
                def replies(request):
                    return [(200, protocol.response(cls)), (200, wire_tool(cls, 'record_review', {
                        'review_seq': request['review_seq'], 'verdict': verdict,
                        'feedback': 'Loopback fixture evidence, not an external model.'}, 'formal-after-prose'))]
                with self.subTest(cls=cls.__name__, verdict=verdict), self.loopback(cls, replies) as (
                        f, request, source, _, requests):
                    tokens = f.rt._reported_driver_tokens('team_test')
                    running, started = self.start(f, source)
                    list(running)
                    self.assertEqual(len(requests), 2)
                    self.assertEqual(f.rt._reported_driver_tokens('team_test'), tokens + 28)
                    decision, = f.events('team.review_recorded')
                    self.assertEqual(decision.payload['review_seq'], request['review_seq'])
                    self.assertEqual(decision.payload['reviewer'], f.reviewer)
                    self.assertEqual(decision.payload['verdict'], verdict)
                    self.assertEqual(decision.payload['run_id'], started.payload['run_id'])
                    self.assertEqual(self.finish(f, started).payload['status'], 'waiting')
                    reports = [e for e in f.rt.team._messages(f.task) if e.payload.get('kind') == 'report'
                               and e.payload.get('reply_to') == source.seq]
                    self.assertEqual(len(reports), 1)
                    self.assertEqual(reports[0].payload['review_seq'], request['review_seq'])
                    self.assertIn(source.seq, _reported_messages(f.rt.team._messages(f.task)))
                    self.assert_no_new_work(f, request)

    def test_both_protocols_budget_reached_by_first_reply_blocks_second_http_request(self):
        for cls in protocol.CLASSES:
            with self.subTest(cls=cls.__name__), self.loopback(cls, [(200, protocol.response(cls))]) as (
                    f, request, source, driver, requests):
                tokens = f.rt._reported_driver_tokens('team_test')
                driver.token_budget = tokens + 14
                running, started = self.start(f, source)
                list(running)
                self.assertEqual(len(requests), 1)
                self.assertEqual(f.rt._reported_driver_tokens('team_test'), tokens + 14)
                self.assertEqual(f.events('team.review_followup_requested'), [])
                self.assertEqual(self.finish(f, started).payload['status'], 'interrupted')
                self.assert_open(f, request, source)

    def test_new_customer_committed_at_final_preflight_blocks_second_http_request(self):
        for cls in protocol.CLASSES:
            with self.subTest(cls=cls.__name__), self.loopback(cls, [(200, protocol.response(cls)),
                    (200, protocol.response(cls))]) as (f, request, source, driver, requests):
                running, started = self.start(f, source)
                next(running)
                original = f.rt.team._pending_operations
                committed = []
                def pending_then_customer(task_id):
                    result = original(task_id)
                    caller = sys._getframe(1)
                    while caller is not None and caller.f_code.co_name != '_run_message':
                        caller = caller.f_back
                    if (caller is not None and caller.f_locals.get('failure_stage') == 'model_request'
                            and caller.f_locals.get('review_followup') is not None and not committed):
                        with Runtime(f.root / 'test.db', roles_path=f.roles, drivers={'team_test': driver},
                                     close_drivers_on_close=False) as other:
                            name = other.team._role(f.reviewer).name
                            committed.append(other.team.receive(f.task, f'@{name} New customer direction',
                                                                 request_id='followup-final-preflight').seq)
                    return result
                with patch.object(f.rt.team, '_pending_operations', side_effect=pending_then_customer):
                    list(running)
                self.assertEqual(len(committed), 1, 'The final preflight must observe another Runtime committing input')
                self.assertEqual(len(requests), 1)
                self.assertEqual(self.finish(f, started).payload['status'], 'interrupted')
                self.assert_open(f, request, source)

    def test_final_usage_read_rechecks_second_runtime_effect_rule_and_exact_version(self):
        from cater.artifacts import normalize_deliverables
        from cater.events import Actor

        for cls in protocol.CLASSES:
            for change in ('pending', 'rule', 'artifact'):
                with self.subTest(cls=cls.__name__, change=change), self.loopback(
                        cls, [(200, protocol.response(cls)), (200, protocol.response(cls))]) as (
                        f, request, source, driver, requests):
                    tokens = f.rt._reported_driver_tokens('team_test')
                    driver.token_budget = tokens + 100000
                    task_events = [e for e in f.rt.log.read() if e.type == 'task.created']
                    running, started = self.start(f, source)
                    next(running)
                    first_calls = [e for e in f.events('team.model_called')
                                   if e.payload['run_id'] == started.payload['run_id']]
                    self.assertEqual(len(first_calls), 1)
                    self.assertEqual(len(requests), 1)
                    original = f.rt._reported_driver_tokens
                    reads, committed = [], []

                    def usage_then_commit(driver_name):
                        count = original(driver_name)
                        reads.append(count)
                        # The first read is at turn entry; the second is the final
                        # follow-up admission read, after context was assembled.
                        if len(reads) == 2:
                            with Runtime(f.root / 'test.db', roles_path=f.roles,
                                         drivers={'team_test': driver}, close_drivers_on_close=False) as other:
                                self.assertFalse(other.log.db.in_transaction)
                                if change == 'pending':
                                    event = other.log.emit(type='team.tool_started', actor=HOST, task_id=f.task,
                                        payload={**started.payload, 'name': 'call_mcp_tool',
                                                 'call_id': 'late-unknown-effect', 'arguments': {}})
                                    committed.append(event.seq)
                                elif change == 'rule':
                                    rule = other.rulebook.record('Require an additional exact-version check',
                                                                 scope='task', task_id=f.task)
                                    committed.append(rule['seq'])
                                else:
                                    # Commit the same validated publication payload used by
                                    # publish_artifact, without any extra model invocation.
                                    artifact, = other.task(f.task).deliverables
                                    payload, = normalize_deliverables([{
                                        'filename': artifact['filename'], 'title': artifact['filename'],
                                        'content': 'new exact version committed during admission\n',
                                        'summary': 'Concurrent fixture revision', 'format': 'text'}])
                                    event = other.log.emit(type='deliverable.published',
                                        actor=Actor.agent(f.worker, other.team._role(f.worker).role),
                                        task_id=f.task, payload={**payload, 'source': 'team', 'phase': 'team'})
                                    committed.append(event.seq)
                                self.assertFalse(other.log.db.in_transaction)
                        return count

                    with patch.object(f.rt, '_reported_driver_tokens', side_effect=usage_then_commit):
                        list(running)
                    self.assertEqual(reads, [tokens + 14, tokens + 14])
                    self.assertEqual(len(committed), 1, 'A separate Runtime must commit at the second usage read')
                    self.assertEqual(len(requests), 1, 'No second HTTP/driver invocation may start')
                    self.assertEqual([e for e in f.events('team.model_called')
                                      if e.payload['run_id'] == started.payload['run_id']], first_calls)
                    self.assertEqual(first_calls[0].payload['tokens'], 14)
                    self.assertEqual(first_calls[0].payload['usage'], protocol.usage(cls))
                    self.assertEqual(f.rt._reported_driver_tokens('team_test'), tokens + 14)
                    finished = self.finish(f, started)
                    self.assertEqual(finished.payload['status'], 'interrupted')
                    self.assertLess(committed[0], finished.seq)
                    self.assertEqual(f.events('team.review_recorded'), [])
                    self.assertEqual(f.events('team.objective_completed'), [])
                    self.assertEqual(f.results('delegate'), [])
                    self.assertEqual([e.seq for e in f.events('team.review_requested')], [request['review_seq']])
                    self.assertEqual(len(f.events('team.review_followup_requested')), 1)
                    self.assertEqual([e for e in f.rt.log.read() if e.type == 'task.created'], task_events)
                    self.assertNotIn(source.seq, _reported_messages(f.rt.team._messages(f.task)))
                    self.assertIn(source.seq, [r['seq'] for r in f.rt.team.recovery.assignments(f.task)])
                    self.assertEqual(f.rt.team_reviews.view(f.task)[0]['status'],
                                     'outdated' if change == 'artifact' else 'pending')
                    self.assertEqual(len(f.rt.task(f.task).deliverables), 2 if change == 'artifact' else 1)
                    if change == 'pending':
                        pending, = f.rt.team._pending_operations(f.task)
                        self.assertEqual(pending['started_seq'], committed[0])
                        self.assertEqual(pending['outcome'], 'unknown')
                    elif change == 'rule':
                        self.assertNotEqual(f.rt.team.recovery.scope(f.task)['rule_fingerprint'],
                                            first_calls[0].payload['context_basis']['rule_fingerprint'])


if __name__ == '__main__':
    unittest.main()
