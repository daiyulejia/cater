"""Read-only inspection of the real QA ledger; no model and no DB writes."""
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

from cater.events import Actor, Event
from cater.review_return_evidence import authenticate
from cater.work import WorkConflict
from test_return_work import ReturnWorkFixtures


class PortableEvidenceTests(unittest.TestCase):
    def test_independent_minimal_snapshot_and_failed_cleanup(self):
        host = Actor.system('team')
        leader, reviewer = Actor.agent('leader', 'manager'), Actor.agent('reviewer', 'reviewer')
        task = 'fixture'
        def event(seq, kind, actor, payload):
            return Event(seq, str(seq), 'time', task, actor, kind, 'team', payload, 'prev', f'hash-{seq}')
        common = {'role': 'leader', 'run_id': 'leader-run', 'message_seq': 7}
        contract = {'version': 1, 'kind': 'atomic_local_events', 'receipt': 'same_transaction'}
        events = [
            event(1, 'deliverable.published', leader, {'filename': 'a.txt', 'content_sha256': 'digest'}),
            event(2, 'team.review_requested', host, {'author': 'leader', 'reviewer': 'reviewer', 'revision': 0,
                'artifacts': [{'seq': 1, 'filename': 'a.txt', 'content_sha256': 'digest'}]}),
            event(3, 'team.message_received', leader, {'kind': 'assignment', 'recipient': 'reviewer', 'review_seq': 2, 'revision': 0}),
            event(4, 'team.run_started', host, {'message_seq': 3, 'role': 'reviewer', 'run_id': 'review-run', 'revision': 0}),
            event(5, 'team.tool_started', host, {'message_seq': 3, 'role': 'reviewer', 'run_id': 'review-run',
                'revision': 0, 'name': 'record_review', 'execution_contract': contract,
                'arguments': {'review_seq': 2, 'verdict': 'approved', 'feedback': 'checked'}}),
            event(6, 'team.review_recorded', host, {'review_seq': 2, 'root_seq': 2, 'reviewer': 'reviewer',
                'run_id': 'review-run', 'verdict': 'approved', 'feedback': 'checked'}),
            event(7, 'team.message_received', reviewer, {'kind': 'report', 'recipient': 'leader', 'review_seq': 2,
                'revision': 0, 'reply_to': 3, 'visible': True, 'text': '复核通过。checked\n请收齐这版成果与相关工作回报，再向原委托人交回或由店长整体交付。若继续修改，需将新版再次交回复核。'}),
            event(8, 'team.tool_finished', host, {'name': 'record_review', 'role': 'reviewer', 'run_id': 'review-run',
                'started_seq': 5, 'result': {'status': 'ok', 'review_seq': 2, 'decision_seq': 6,
                                             'message_seq': 7, 'verdict': 'approved'}}),
            event(9, 'team.run_started', host, {**common, 'revision': 0}),
            event(10, 'team.external_run_started', host, {**common, 'run_started_seq': 9,
                'run_started_hash': 'hash-9', 'execution_contract': {'version': 1, 'kind': 'external_agent'}}),
            event(11, 'team.external_run_finished', host, {**common, 'started_seq': 10, 'started_hash': 'hash-10',
                'phase': 'finished', 'prompt_attempted': True, 'cleanup_complete': True,
                'result': 'returned', 'outcome': 'returned', 'stop_reason': 'end_turn'}),
            event(12, 'team.run_finished', host, {**common, 'status': 'completed'}),
        ]
        kwargs = {'task_id': task, 'leader_actor': leader, 'reviewer_actor': reviewer,
                  'run_seq': 9, 'run_hash': 'hash-9'}
        self.assertEqual(authenticate(events, **kwargs)['report']['seq'], 7)
        for verdict in ('approved', 'changes_requested'):
            for version in (1, 2):
                from cater.review_report_text import review_report_text
                mutated = list(events)
                mutated[4] = replace(mutated[4], payload={**mutated[4].payload,
                    'arguments': {**mutated[4].payload['arguments'], 'verdict': verdict}})
                mutated[5] = replace(mutated[5], payload={**mutated[5].payload, 'verdict': verdict})
                mutated[6] = replace(mutated[6], payload={**mutated[6].payload,
                    'text': review_report_text(verdict, 'checked', 2, version=version)})
                mutated[7] = replace(mutated[7], payload={**mutated[7].payload,
                    'result': {**mutated[7].payload['result'], 'verdict': verdict}})
                with self.subTest(verdict=verdict, version=version):
                    self.assertIsNotNone(authenticate(mutated, **kwargs))
                    mutated[6] = replace(mutated[6], payload={**mutated[6].payload,
                        'text': mutated[6].payload['text'] + ' forged'})
                    self.assertIsNone(authenticate(mutated, **kwargs))
        events[10] = replace(events[10], payload={**events[10].payload, 'cleanup_complete': False})
        self.assertIsNone(authenticate(events, **kwargs))


class ExecutedContinuationEvidenceTests(ReturnWorkFixtures, unittest.TestCase):
    def test_real_tool_execution_from_continuation_to_formal_return(self):
        for method in ('continue_work', 'send_message'):
            with self.subTest(method=method):
                f = self.fixture('external')
                published, = self.run_calls(f, self.human(f), ('publish_artifact', {
                    'filename': 'candidate.txt', 'content': 'candidate', 'summary': 'fixture', 'format': 'text'}))
                requested, = self.run_calls(f, self.human(f), ('request_review', {
                    'reviewer': f.reviewer, 'artifact_seqs': [published['artifact_seq']], 'instruction': 'Inspect'}))
                original = self.message(f, requested['message_seq'])
                with patch.object(f.rt.team, '_current', side_effect=WorkConflict('fixture interruption')):
                    self.run_calls(f, original, statuses=('interrupted',))
                continuation_args = ({'assignment_seq': original.seq, 'instruction': '  Resume exact review  '}
                    if method == 'continue_work' else {'recipient': f.reviewer, 'text': '  Resume exact review  ',
                                                       'continue_assignment_seq': original.seq})
                continued, = self.run_calls(f, self.human(f), (method, continuation_args))
                notice = self.message(f, continued['message_seq'])
                decision, = self.run_calls(f, notice, ('record_review', {'review_seq': requested['review_seq'],
                    'verdict': 'approved', 'feedback': 'Actual offline review'}), statuses=('waiting',))
                report = self.message(f, decision['message_seq'])
                self.assertEqual(report.payload['reply_to'], original.seq)
                started = self.start(f, report)
                f.driver.scripts[f.manager] = [lambda run: 'Read the review return.']
                list(f.rt.team._run_message(f.task, report, f.rt.team._role(f.manager), started))
                events = f.rt.log.read(task_id=f.task)
                options = {'task_id': f.task, 'leader_actor': original.actor, 'reviewer_actor': report.actor,
                           'run_seq': started.seq, 'run_hash': started.hash}
                result = authenticate(events, **options)
                self.assertIsNotNone(result)
                self.assertEqual(result['reviewer_input']['seq'], notice.seq)
                self.assertEqual(result['review_assignment']['seq'], original.seq)
                self.assertTrue(f.rt.verify_chain().ok)
                for changes in ({'continue_assignment_seq': original.seq + 1},
                                {'continue_assignment_seq': True}, {'recipient': f.worker},
                                {'revision': 999}, {'kind': 'message'}, {'visible': False}):
                    mutated = [replace(e, payload={**e.payload, **changes}) if e.seq == notice.seq else e for e in events]
                    self.assertIsNone(authenticate(mutated, **options), changes)
                mutated = [replace(e, actor=Actor.agent(f.worker, f.rt.team._role(f.worker).role))
                           if e.seq == notice.seq else e for e in events]
                self.assertIsNone(authenticate(mutated, **options))
                mutated = [replace(e, task_id='other-task') if e.seq == notice.seq else e for e in events]
                self.assertIsNone(authenticate(mutated, **options))
                reviewer_run_seq = result['reviewer_run']['seq']
                mutated = [replace(e, seq=0) if e.seq == reviewer_run_seq else e for e in events]
                self.assertIsNone(authenticate(mutated, **options))
                continuation_receipt = next(e for e in events if e.type == 'team.tool_finished'
                    and e.payload.get('name') == method and e.payload.get('result', {}).get('message_seq') == notice.seq)
                mutated = [replace(e, payload={**e.payload, 'result': {**e.payload['result'], 'status': 'error'}})
                           if e.seq == continuation_receipt.seq else e for e in events]
                self.assertIsNone(authenticate(mutated, **options))
                reviewer_external_finish = next(e for e in events if e.type == 'team.external_run_finished'
                    and e.payload.get('run_id') == next(r for r in events if r.seq == reviewer_run_seq).payload['run_id'])
                mutated = [replace(e, payload={**e.payload, 'cleanup_complete': False})
                           if e.seq == reviewer_external_finish.seq else e for e in events]
                # This function proves the review return, not reviewer process cleanup.
                self.assertIsNotNone(authenticate(mutated, **options))


class ReviewReturnEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / '.cater/qa/runs/team-ohuu3r23/team.db'
        if not path.is_file():
            raise unittest.SkipTest('Local QA ledger team-ohuu3r23 is unavailable')
        with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as db:
            db.row_factory = sqlite3.Row
            cls.events = [Event(seq=r['seq'], id=r['id'], ts=r['ts'], task_id=r['task_id'],
                actor=Actor(**json.loads(r['actor_json'])), type=r['type'], category=r['category'],
                payload=json.loads(r['payload_json']), prev_hash=r['prev_hash'], hash=r['hash'],
                key_id=r['key_id'], signature=r['signature']) for r in db.execute('select * from events order by seq')]
        cls.start = next(e for e in cls.events if e.seq == 108)

    def verify(self, events=None, **kwargs):
        options = {'task_id': self.start.task_id,
                   'leader_actor': next(e.actor for e in self.events if e.seq == 87),
                   'reviewer_actor': next(e.actor for e in self.events if e.seq == 102),
                   'run_seq': self.start.seq, 'run_hash': self.start.hash}
        options.update(kwargs)
        return authenticate(self.events if events is None else events, **options)

    def alter(self, seq, *, payload=None, **changes):
        return [replace(e, **changes, **({'payload': {**e.payload, **payload}} if payload is not None else {}))
                if e.seq == seq else e for e in self.events]

    def test_real_review_return_and_clean_external_run(self):
        result = self.verify()
        self.assertIsNotNone(result)
        self.assertEqual({key: value['seq'] for key, value in result.items()}, {
            'review_request': 86, 'review_assignment': 87, 'reviewer_run': 93, 'reviewer_input': 87,
            'review_tool_start': 100, 'review_decision': 101, 'report': 102,
            'review_tool_finish': 103, 'leader_run': 108, 'external_start': 109,
            'external_finish': 111, 'leader_finish': 115})
        self.assertEqual(result['leader_run']['hash'], self.start.hash)

    def test_wrong_identity_hash_task_and_run_fail_closed(self):
        self.assertIsNone(self.verify(run_hash='wrong'))
        self.assertIsNone(self.verify(task_id='other-task'))
        self.assertIsNone(self.verify(leader_actor=Actor.agent('other-leader', 'manager')))
        cases = [
            (86, {'actor': Actor.human()}),
            (87, {'actor': Actor.human()}),
            (93, {'payload': {'message_seq': 16}}),
            (100, {'payload': {'message_seq': 16}}),
            (101, {'actor': Actor.human()}),
            (102, {'actor': Actor.human()}),
            (102, {'actor': Actor.agent('cabinet_engineering', 'wrong-role')}),
            (103, {'actor': Actor.human()}),
            (108, {'actor': Actor.human()}),
            (109, {'payload': {'run_started_hash': 'wrong'}}),
            (111, {'payload': {'started_hash': 'wrong'}}),
            (103, {'payload': {'run_id': 'other-run'}}),
            (101, {'payload': {'run_id': 'other-run'}}),
            (102, {'task_id': 'other-task'}),
            (109, {'task_id': 'other-task'}),
        ]
        for seq, changes in cases:
            with self.subTest(seq=seq, changes=changes):
                self.assertIsNone(self.verify(self.alter(seq, **changes)))

    def test_mismatched_record_and_cleanup_are_not_evidence(self):
        cases = [
            (100, {'arguments': {'review_seq': 15, 'verdict': 'approved', 'feedback': 'wrong'}}),
            (101, {'reviewer': 'someone-else'}),
            (101, {'verdict': 'changes_requested'}),
            (102, {'review_seq': 15}),
            (102, {'text': '伪造的复核回报'}),
            (102, {'visible': False}),
            (87, {'revision': False}),
            (101, {'review_seq': True}),
            (103, {'result': {'status': 'ok', 'review_seq': 86, 'decision_seq': 101,
                              'message_seq': 102, 'verdict': 'changes_requested'}}),
            (111, {'cleanup_complete': False}),
            (111, {'stop_reason': 'cancelled'}),
            (111, {'outcome': 'unknown', 'result': 'failed'}),
            (115, {'status': 'failed'}),
        ]
        for seq, payload in cases:
            with self.subTest(seq=seq, payload=payload):
                self.assertIsNone(self.verify(self.alter(seq, payload=payload)))

    def test_newer_revision_or_file_invalidates_old_review(self):
        template = self.events[-1]
        objective = replace(template, seq=template.seq + 1, actor=Actor.system('team'),
                            type='team.objective_updated', payload={'goal': 'new goal'})
        newer_file = replace(template, seq=template.seq + 1, type='deliverable.published',
                             payload={'filename': 'inventory.py', 'content_sha256': 'new-version'})
        newer_review = replace(template, seq=template.seq + 1, actor=Actor.system('team'),
                               type='team.review_requested', payload={'root_seq': 15})
        for event in (objective, newer_file, newer_review):
            with self.subTest(event=event.type):
                self.assertIsNone(self.verify([*self.events, event]))

    def test_duplicate_receipt_or_malformed_fields_fail_closed(self):
        duplicate = replace(next(e for e in self.events if e.seq == 103), seq=self.events[-1].seq + 1)
        self.assertIsNone(self.verify([*self.events, duplicate]))
        self.assertIsNone(self.verify(self.alter(101, payload={'verdict': []})))
        self.assertIsNone(self.verify(self.alter(86, payload={'artifacts': [{}]})))
