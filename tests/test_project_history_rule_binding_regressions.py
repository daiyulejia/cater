"""Offline regressions for incomplete rule metadata and invalid bindings.

Both exceptional records below are synthetic, append-only temporary-ledger
inputs, not claims about what the public project-binding or derive APIs emit.
"""
import sqlite3
import unittest

from cater.events import Actor
import test_project_history_metadata as fixtures


class ProjectHistoryRuleBindingRegressions(unittest.TestCase):
    def fixture(self):
        f = fixtures.ProjectMetadataTests(methodName='runTest')
        f.setUp()
        self.addCleanup(f.doCleanups)
        return f

    @staticmethod
    def deny_json(action, first, second, database, trigger):
        return (sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_FUNCTION
                and str(second).startswith('json_') else sqlite3.SQLITE_OK)

    @staticmethod
    def has_incomplete_marker(value):
        if isinstance(value, dict):
            return (value.get('metadata_incomplete') is True
                    or (isinstance(value.get('rules_metadata'), dict)
                        and value['rules_metadata'].get('complete') is False)
                    or any(ProjectHistoryRuleBindingRegressions.has_incomplete_marker(v) for v in value.values()))
        if isinstance(value, list):
            return any(ProjectHistoryRuleBindingRegressions.has_incomplete_marker(v) for v in value)
        return False

    def test_incomplete_rule_snapshot_is_visible_to_model_context(self):
        for mode in ('no_json', 'ambiguous_foreign_creation'):
            with self.subTest(mode=mode):
                f = self.fixture()
                base = f.rt.rulebook.record('Workspace baseline fixture', scope='workspace')
                inherited = {k: v for k, v in base.items() if k not in ('seq', 'updated_at')}
                inherited.update(id='legacy-inherited-workspace-rule', text='Inherited rule fixture')
                payload = {'goal': 'Legacy fixture', 'inherited_rules': [inherited]}
                if mode == 'ambiguous_foreign_creation':
                    payload['unrelated\0suffix'] = 'fixture'
                f.rt.log.emit(type='task.created', task_id='legacy-other-task', actor=Actor.human(), payload=payload)
                # The existing non-private projection accepts this legacy shape.
                legacy = f.rt.rulebook.current_snapshot(f.task)
                self.assertTrue(any(row['id'] == inherited['id'] for row in legacy['rules']))
                if mode == 'no_json':
                    f.rt.log.db.set_authorizer(self.deny_json)
                    self.addCleanup(f.rt.log.db.set_authorizer, None)
                safe = f.rt.rulebook.current_snapshot(f.task, privacy_safe=True)
                self.assertTrue(safe.get('metadata_incomplete'))
                f.exchange()
                context = f.driver.calls[-1]['context']
                self.assertTrue(self.has_incomplete_marker(context),
                                'The model must be told that the supplied rule metadata is incomplete')

    def test_normal_active_rules_remain_present_with_or_without_json(self):
        for no_json in (False, True):
            with self.subTest(no_json=no_json):
                f = self.fixture()
                rows = [f.rt.rulebook.record('Workspace fixture', scope='workspace'),
                        f.rt.rulebook.record('Project fixture', scope='project', task_id=f.task),
                        f.rt.rulebook.record('Task fixture', scope='task', task_id=f.task)]
                if no_json:
                    f.rt.log.db.set_authorizer(self.deny_json)
                    self.addCleanup(f.rt.log.db.set_authorizer, None)
                f.exchange()
                present = {row['id'] for row in f.driver.calls[-1]['context']['rules']}
                self.assertTrue({row['id'] for row in rows} <= present)
                self.assertEqual(f.events('team.run_finished')[-1].payload['status'], 'completed')

    def test_nonhuman_later_binding_cannot_expose_prior_project_history(self):
        f = self.fixture()
        artifact = f.publication(f.source, 'PRIVATE_FIXTURE_CONTENT')
        f.complete(f.source)
        before = f.rt.team.project_history.index(f.task)
        entry, = [row for row in before['entries'] if row['task_id'] == f.source]
        other = f.rt.rulebook.create_project('Another fixture project')['id']
        worker = f.rt.team._role(f.worker)
        f.rt.log.emit(type='task.project_bound', task_id=f.source,
                      actor=Actor.agent(worker.id, worker.role), payload={'project_id': other})
        self.assertEqual(f.rt.rulebook.task_project(f.source), other)
        after = f.rt.team.project_history.index(f.task)
        read = f.rt.team.project_history.read(f.task, {'section': 'artifact',
            'as_of_seq': before['as_of_seq'], 'delivery_seq': entry['delivery_seq'], 'artifact_seq': artifact})
        self.assertTrue(f.rt.verify_chain().ok)
        self.assertFalse(after['available'] and any(row['task_id'] == f.source for row in after.get('entries', [])),
                         'Inconsistent project binding must not remain in the old catalog')
        self.assertNotEqual(read['status'], 'ok', 'An old catalog anchor cannot bypass current binding checks')

    def test_normal_project_binding_shares_only_with_same_project(self):
        f = self.fixture()
        artifact = f.publication(f.source, 'PRIVATE_FIXTURE_CONTENT')
        f.complete(f.source)
        index = f.rt.team.project_history.index(f.task)
        entry, = [row for row in index['entries'] if row['task_id'] == f.source]
        args = {'section': 'artifact', 'as_of_seq': index['as_of_seq'],
                'delivery_seq': entry['delivery_seq'], 'artifact_seq': artifact}
        self.assertEqual(f.rt.team.project_history.read(f.task, args)['status'], 'ok')
        other = f.rt.rulebook.create_project('Another fixture project')['id']
        foreign = f.rt.create_task('Different project', project_id=other)
        self.assertNotEqual(f.rt.team.project_history.read(foreign, args)['status'], 'ok')


if __name__ == '__main__':
    unittest.main()
