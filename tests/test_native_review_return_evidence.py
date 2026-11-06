"""Trusted task snapshots and real loopback API responses; no paid requests."""
from dataclasses import replace
import unittest
from unittest.mock import patch

from cater.anthropic import AnthropicMessagesDriver
from cater.drivers import OpenAICompatibleDriver
from cater.events import Actor
from cater.native_review_return_evidence import authenticate
from cater.runtime import Runtime
import test_native_response_evidence as native_fixture


class NativeReviewReturnEvidenceTests(unittest.TestCase):
    def run_fixture(self, cls, *, audit='unsigned', **kwargs):
        helper = native_fixture.NativeResponseEvidenceTests(methodName='runTest')
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        original_fixture = helper.fixture
        def mixed_fixture(**fixture_kwargs):
            f = original_fixture(**fixture_kwargs)
            f.rt.log.audit_key = b'offline-test-key'
            f.rt.log.audit_keys[f.rt.log.key_id] = f.rt.log.audit_key
            return f
        def signed_runtime(*args, **options):
            return Runtime(*args, audit_key='offline-test-key', **options)
        from contextlib import ExitStack
        with ExitStack() as stack:
            if audit == 'signed':
                stack.enter_context(patch('test_team_external.Runtime', side_effect=signed_runtime))
            elif audit == 'mixed':
                stack.enter_context(patch.object(helper, 'fixture', side_effect=mixed_fixture))
            f, claimed, requests, injected = helper.run_report(cls, **kwargs)
        self.assertEqual(len(requests), 2 if kwargs.get('first_read') else 1)
        options = {'task_id': f.task, 'leader_actor': Actor.agent(f.manager, claimed[1].role),
                   'reviewer_actor': claimed[0].actor, 'run_seq': claimed[2].seq, 'run_hash': claimed[2].hash}
        return f, claimed, options

    def test_both_native_http_protocols_authenticate_default_and_optional_audit_logs(self):
        for cls in (OpenAICompatibleDriver, AnthropicMessagesDriver):
            for audit in ('unsigned', 'mixed', 'signed'):
                with self.subTest(protocol=cls.__name__, audit=audit):
                    f, claimed, options = self.run_fixture(cls, audit=audit)
                    before = f.rt.log.count()
                    chain = f.rt.verify_chain()
                    self.assertTrue(chain.ok, chain)
                    self.assertEqual(chain.signed == 0, audit == 'unsigned')
                    if audit == 'mixed':
                        self.assertGreater(chain.signed, 0)
                        self.assertLess(chain.signed, chain.checked)
                    result = authenticate(f.rt.log.read(task_id=f.task), **options)
                    self.assertIsNotNone(result)
                    self.assertEqual(result['report']['seq'], claimed[0].seq)
                    self.assertEqual(result['leader_run']['hash'], claimed[2].hash)
                    self.assertEqual(result['native_response']['seq'], f.events('team.native_response_completed')[0].seq)
                    self.assertEqual(f.rt.log.count(), before)
                    self.assertFalse(f.rt.log.db.in_transaction)
                    self.assertEqual(f.events('team.objective_completed'), [])
                    self.assertEqual([e for e in f.events('team.message_received')
                                      if e.payload.get('trigger') == 'review_return_candidate'], [])

    def test_bad_transport_or_truncated_response_has_no_evidence(self):
        for cls in (OpenAICompatibleDriver, AnthropicMessagesDriver):
            for kwargs in ({'contained': True}, {'bad_cleanup': True}, {'replaced_parser': True}):
                with self.subTest(protocol=cls.__name__, case=kwargs):
                    f, _, options = self.run_fixture(cls, **kwargs)
                    self.assertIsNone(authenticate(f.rt.log.read(task_id=f.task), **options))

    def test_real_prior_read_tool_prevents_authentication_despite_later_clean_response(self):
        for cls in (OpenAICompatibleDriver, AnthropicMessagesDriver):
            with self.subTest(protocol=cls.__name__):
                f, claimed, options = self.run_fixture(cls, first_read=True)
                self.assertEqual(len([e for e in f.events('team.tool_started')
                                      if e.payload.get('run_id') == claimed[2].payload['run_id']]), 1)
                self.assertEqual(len(f.events('team.native_response_completed')), 1)
                self.assertIsNone(authenticate(f.rt.log.read(task_id=f.task), **options))

    def test_mutations_wrong_binding_and_prior_actions_fail_closed(self):
        for cls in (OpenAICompatibleDriver, AnthropicMessagesDriver):
            with self.subTest(protocol=cls.__name__):
                f, claimed, options = self.run_fixture(cls)
                events = f.rt.log.read(task_id=f.task)
                receipt, = f.events('team.native_response_completed')
                finish = next(e for e in events if e.type == 'team.run_finished' and e.payload.get('run_id') == claimed[2].payload['run_id'])
                call = next(e for e in events if e.seq == receipt.payload['model_call_seq'])
                def alter(event, **payload):
                    return [replace(e, payload={**e.payload, **payload}) if e.seq == event.seq else e for e in events]
                cases = [
                    [e for e in events if e.seq != receipt.seq],
                    [e for e in events if e.seq != finish.seq],
                    alter(receipt, report_hash='wrong'), alter(receipt, run_hash='wrong'),
                    alter(receipt, model_call_hash='wrong'), alter(receipt, local_cleanup_confirmed=False),
                    alter(receipt, scope={**receipt.payload['scope'], 'revision': 999}),
                    alter(finish, status='interrupted'), alter(finish, native_cleanup_failed=True),
                    alter(call, run_id='wrong'),
                    [replace(e, task_id='another') if e.seq == receipt.seq else e for e in events],
                    [replace(e, actor=Actor.human()) if e.seq == receipt.seq else e for e in events],
                    [e for e in events if e.seq != options['run_seq']],
                    [*events, receipt],
                ]
                prior_tool = replace(call, seq=receipt.seq + 1, type='team.tool_started',
                                     payload={'run_id': claimed[2].payload['run_id'], 'name': 'read_messages'})
                cases.append([*events, replace(prior_tool, seq=max(e.seq for e in events) + 1)])
                for index, mutated in enumerate(cases):
                    with self.subTest(case=index):
                        self.assertIsNone(authenticate(mutated, **options))
                for changes in ({'run_hash': 'wrong'}, {'task_id': 'another'},
                                {'leader_actor': Actor.agent('other', 'manager')},
                                {'reviewer_actor': Actor.agent('other', 'reviewer')}):
                    self.assertIsNone(authenticate(events, **{**options, **changes}))

    def test_truncation_of_required_history_is_rejected_but_complete_prefix_is_not_detected(self):
        f, _, options = self.run_fixture(OpenAICompatibleDriver)
        events = f.rt.log.read(task_id=f.task)
        finish = next(e for e in events if e.type == 'team.run_finished' and e.payload.get('run_id') ==
                      next(r for r in events if r.seq == options['run_seq']).payload['run_id'])
        self.assertIsNone(authenticate([e for e in events if e.seq < finish.seq], **options))
        tail = f.rt.log.emit(type='task.progress_reported', actor=Actor.system('team'),
                             task_id=f.task, payload={'text': 'offline fixture'})
        full = f.rt.log.read(task_id=f.task)
        self.assertIsNotNone(authenticate(full, **options))
        # An otherwise complete prefix cannot reveal a missing irrelevant tail.
        self.assertIsNotNone(authenticate([e for e in full if e.seq < tail.seq], **options))


if __name__ == '__main__':
    unittest.main()
