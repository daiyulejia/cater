"""Offline checks for catalog/tool availability under abnormal bindings."""
import unittest

from cater.events import Actor
from cater.project_history import TOOL
import test_project_history_metadata as fixtures


class BindingAvailabilityTests(unittest.TestCase):
    def fixture(self):
        f = fixtures.ProjectMetadataTests(methodName='runTest')
        f.setUp()
        self.addCleanup(f.doCleanups)
        return f

    def test_target_binding_anomaly_disables_catalog_and_tool(self):
        f = self.fixture()
        other = f.rt.rulebook.create_project('Other project')['id']
        f.rt.log.emit(type='task.project_bound', task_id=f.task, actor=Actor.system('team'),
                      payload={'project_id': other})
        history = f.rt.team.project_history
        self.assertFalse(history.available(f.task))
        self.assertFalse(history.index(f.task)['available'])
        f.exchange()
        call = f.driver.calls[-1]
        self.assertEqual(call['context']['project_history']['reason'], 'metadata_query_unavailable')
        self.assertNotIn(TOOL, {tool['name'] for tool in call['tools']})

    def test_relevant_source_anomaly_marks_catalog_unavailable_including_old_anchor(self):
        f = self.fixture()
        artifact = f.publication(f.source, 'private fixture body')
        f.complete(f.source)
        history = f.rt.team.project_history
        before = history.index(f.task)
        entry, = before['entries']
        other = f.rt.rulebook.create_project('Other project')['id']
        f.rt.log.emit(type='task.project_bound', task_id=f.source, actor=Actor.agent('fake', 'worker'),
                      payload={'project_id': other})
        self.assertFalse(history.available(f.task))
        for anchor in (None, before['as_of_seq']):
            index = history.index(f.task, as_of_seq=anchor)
            self.assertFalse(index['available'])
            self.assertEqual(index['reason'], 'metadata_query_unavailable')
            self.assertNotIn('total', index)
        result = history.read(f.task, {'section': 'artifact', 'as_of_seq': before['as_of_seq'],
                                      'delivery_seq': entry['delivery_seq'], 'artifact_seq': artifact})
        self.assertNotEqual(result['status'], 'ok')
        self.assertNotIn('private fixture body', str(result))
        f.exchange()
        self.assertNotIn(TOOL, {tool['name'] for tool in f.driver.calls[-1]['tools']})

    def test_unrelated_source_anomaly_does_not_disable_normal_project(self):
        f = self.fixture()
        other = f.rt.rulebook.create_project('Other project')['id']
        foreign = f.rt.create_task('Other task', project_id=other)
        f.rt.log.emit(type='task.project_bound', task_id=foreign, actor=Actor.agent('fake', 'worker'),
                      payload={'project_id': f.project})
        history = f.rt.team.project_history
        self.assertTrue(history.available(f.task))
        index = history.index(f.task)
        self.assertTrue(index['available'])
        self.assertEqual(index['total'], 0)
        f.exchange()
        self.assertIn(TOOL, {tool['name'] for tool in f.driver.calls[-1]['tools']})


if __name__ == '__main__':
    unittest.main()
