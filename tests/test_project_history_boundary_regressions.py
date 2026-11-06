"""Offline coverage for unavailable project and rule metadata."""

import sqlite3
import unittest

from cater.events import Actor
import test_project_history_metadata as fixtures


class ProjectHistoryBoundaryRegressions(unittest.TestCase):
    def fixture(self):
        fixture = fixtures.ProjectMetadataTests(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def test_unresolved_binding_blocks_old_snapshot_for_target_and_source(self):
        for target in (False, True):
            for value in (None, 42, 'missing-project'):
                with self.subTest(target=target, value=value):
                    f = self.fixture()
                    artifact = f.publication(f.source)
                    f.complete(f.source)
                    before = f.rt.team.project_history.index(f.task)
                    entry, = before['entries']
                    f.rt.log.emit(type='task.project_bound', task_id=f.task if target else f.source,
                                  actor=Actor.human(), payload={'project_id': value})
                    self.assertFalse(f.rt.team.project_history.index(f.task)['available'])
                    result = f.rt.team.project_history.read(f.task, {'section': 'artifact',
                        'as_of_seq': before['as_of_seq'], 'delivery_seq': entry['delivery_seq'],
                        'artifact_seq': artifact})
                    self.assertNotEqual(result['status'], 'ok')

    def test_no_json_rule_snapshot_declares_incomplete_metadata(self):
        f = self.fixture()
        def deny_json(action, first, second, database, trigger):
            return (sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_FUNCTION
                    and str(second).startswith('json_') else sqlite3.SQLITE_OK)
        f.rt.log.db.set_authorizer(deny_json)
        self.addCleanup(f.rt.log.db.set_authorizer, None)
        snapshot = f.rt.rulebook.current_snapshot(f.task, privacy_safe=True)
        self.assertTrue(snapshot['metadata_incomplete'])
        self.assertEqual(snapshot['project_id'], f.project)
        f.exchange()
        self.assertEqual(f.events('team.run_finished')[-1].payload['status'], 'completed')
        context = f.driver.calls[-1]['context']
        self.assertFalse(context['rules_metadata']['complete'])
        self.assertEqual(context['rules_metadata']['enforcement'], 'guidance_only')
        self.assertIn('不能声称', context['rules_metadata']['note'])
        self.assertTrue(f.rt.rulebook.view(task_id=f.task, privacy_safe=True)['metadata_incomplete'])
        applied = list(f.rt.log.iter_events(task_id=f.task, event_types=('rules.applied',)))
        self.assertTrue(applied[-1].payload['metadata_incomplete'])

    def test_ambiguous_foreign_creation_marks_model_context_incomplete(self):
        f = self.fixture()
        f.rt.log.emit(type='task.created', task_id='foreign-ambiguous', actor=Actor.human(),
                      payload={'goal': 'private', 'inherited_rules\0x': []})
        f.exchange()
        context = f.driver.calls[-1]['context']
        self.assertFalse(context['rules_metadata']['complete'])
        self.assertIn('部分继承要求可能未列出', context['rules_metadata']['note'])
        self.assertTrue(f.rt.rulebook.view(task_id=f.task, privacy_safe=True)['metadata_incomplete'])

    def test_non_customer_bindings_cannot_expose_prior_deliveries(self):
        for target in (False, True):
            for actor in (Actor.agent('fake', 'worker'), Actor.system('team'), Actor.human('other')):
                with self.subTest(target=target, actor=actor):
                    f = self.fixture()
                    other = f.rt.rulebook.create_project('Other')['id']
                    artifact = f.publication(f.source, 'private-delivery-body')
                    f.complete(f.source)
                    before = f.rt.team.project_history.index(f.task)
                    entry, = before['entries']
                    f.rt.log.emit(type='task.project_bound', task_id=f.task if target else f.source,
                                  actor=actor, payload={'project_id': other})
                    self.assertEqual(f.rt.rulebook.task_project(f.task if target else f.source), other)
                    after = f.rt.team.project_history.index(f.task)
                    self.assertFalse(after['available'] and any(row['task_id'] == f.source
                                                               for row in after.get('entries', [])))
                    result = f.rt.team.project_history.read(f.task, {'section': 'artifact',
                        'as_of_seq': before['as_of_seq'], 'delivery_seq': entry['delivery_seq'],
                        'artifact_seq': artifact})
                    self.assertNotEqual(result['status'], 'ok')
                    self.assertNotIn('private-delivery-body', str(result))

    def test_later_human_goal_does_not_certify_an_unverified_team_boundary(self):
        f = self.fixture()
        sent, = f.exchange(('send_message', {'recipient': f.manager, 'text': 'Refine'}),
                           task=f.source, role=f.worker)
        source = next(e for e in f.rt.team._messages(f.source) if e.seq == sent['message_seq'])
        f.exchange(('set_objective', {'goal': 'Agent goal'}), task=f.source, role=f.manager, source=source)
        f.rt.command(f.source, 'New direction', goal='Verified human goal')
        f.publication(f.source)
        f.complete(f.source)
        result = f.rt.team.project_history.index(f.task)
        self.assertFalse(result['available'])
        self.assertEqual(result['reason'], 'metadata_query_unavailable')


if __name__ == '__main__':
    unittest.main()
