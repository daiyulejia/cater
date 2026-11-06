"""Offline visibility regressions for synthetic invalid binding events."""
import unittest

from cater.events import Actor
from cater.project_history import TOOL
import test_project_history_metadata as fixtures


class BindingVisibilityTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.ProjectMetadataTests(methodName='runTest')
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def invalid_binding(self, task):
        f = self.f
        other = f.rt.rulebook.create_project('Another project')['id']
        f.rt.log.emit(type='task.project_bound', task_id=task,
                      actor=Actor.agent('untrusted-worker', 'worker'),
                      payload={'project_id': other})

    def test_target_invalid_binding_is_not_available_or_advertised(self):
        f = self.f
        self.assertTrue(f.rt.team.project_history.available(f.task))
        self.invalid_binding(f.task)
        self.assertFalse(f.rt.team.project_history.index(f.task)['available'])
        # Exercise the actual run's tool list, using the in-process fake driver.
        f.exchange()
        names = {tool['name'] for tool in f.driver.calls[-1]['tools']}
        with self.subTest('available'):
            self.assertFalse(f.rt.team.project_history.available(f.task))
        with self.subTest('tool_advertised'):
            self.assertNotIn(TOOL, names)

    def test_source_invalid_binding_does_not_claim_a_complete_empty_catalog(self):
        f = self.f
        artifact = f.publication(f.source, 'private-fixture-content')
        f.complete(f.source)
        before = f.rt.team.project_history.index(f.task)
        entry, = before['entries']
        args = {'section': 'artifact', 'as_of_seq': before['as_of_seq'],
                'delivery_seq': entry['delivery_seq'], 'artifact_seq': artifact}
        self.assertEqual(f.rt.team.project_history.read(f.task, args)['status'], 'ok')
        self.invalid_binding(f.source)
        after = f.rt.team.project_history.index(f.task)
        read = f.rt.team.project_history.read(f.task, args)
        self.assertNotEqual(read['status'], 'ok')
        self.assertNotIn('private-fixture-content', str(read))
        self.assertFalse(any(row['task_id'] == f.source for row in after.get('entries', [])))
        self.assertTrue(not after['available'] or after.get('complete') is False
                        or after.get('metadata_incomplete') is True,
                        'A catalog excluding an unverified source must declare incompleteness')

    def test_unrelated_project_remains_queryable_after_invalid_binding(self):
        f = self.f
        project = f.rt.rulebook.create_project('Independent project')['id']
        source = f.rt.create_task('Independent source', project_id=project)
        f.rt.team.enable(source)
        target = f.rt.create_task('Independent target', project_id=project)
        artifact = f.publication(source, 'independent-fixture-content')
        f.complete(source)
        self.invalid_binding(f.task)
        index = f.rt.team.project_history.index(target)
        self.assertTrue(index['available'])
        entry, = [row for row in index['entries'] if row['task_id'] == source]
        result = f.rt.team.project_history.read(target, {'section': 'artifact',
            'as_of_seq': index['as_of_seq'], 'delivery_seq': entry['delivery_seq'],
            'artifact_seq': artifact})
        self.assertEqual(result['status'], 'ok')
        self.assertIn('independent-fixture-content', str(result))


if __name__ == '__main__':
    unittest.main()
