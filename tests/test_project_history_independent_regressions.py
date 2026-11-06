"""Independent offline regressions for project-history scope and metadata.

Fixtures use temporary ledgers and a scripted local driver; no real model is
called. The later invalid project binding is deliberately synthetic and does
not claim that the public project-binding API permits rebinding.
"""
import json
import sqlite3
import unittest
from unittest.mock import patch

from cater.events import Actor
from cater.project_history import TOOL
import test_project_history_metadata as fixtures


class ProjectHistoryIndependentRegressions(unittest.TestCase):
    def fixture(self):
        f = fixtures.ProjectMetadataTests(methodName='runTest')
        f.setUp()
        self.addCleanup(f.doCleanups)
        return f

    def test_no_json_and_ambiguous_metadata_do_not_decode_foreign_bodies(self):
        for mode in ('no_json', 'ambiguous'):
            with self.subTest(mode=mode):
                f = self.fixture()
                other = f.rt.rulebook.create_project('Other fixture project')['id']
                foreign = f.rt.create_task('FOREIGN_GOAL_CANARY', project_id=other)
                f.rt.team.enable(foreign)
                f.exchange(('publish_artifact', {'filename': 'private.txt', 'content': 'FOREIGN_BODY_CANARY',
                    'summary': 'Fixture', 'format': 'text'}), task=foreign)
                child = f.rt.derive_task(foreign, 'Independent fixture', request_id='history-privacy-child')['task_id']
                f.publication(f.task)  # ordinary chat must also work with a current-task artifact
                if mode == 'ambiguous':
                    f.rt.log.emit(type='task.created', task_id='ambiguous-foreign', actor=Actor.human(),
                        payload={'goal': 'AMBIGUOUS_GOAL_CANARY', 'project_id': other, 'project_id\0extra': other})

                if mode == 'no_json':
                    def deny_json(action, first, second, database, trigger):
                        return (sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_FUNCTION
                                and str(second).startswith('json_') else sqlite3.SQLITE_OK)
                    f.rt.log.db.set_authorizer(deny_json)
                    self.addCleanup(f.rt.log.db.set_authorizer, None)

                original_row = f.rt.log._row_to_event
                original_loads = json.loads
                seen = set()
                def row_probe(row):
                    event = original_row(row)
                    if row['task_id'] == foreign and 'FOREIGN_GOAL_CANARY' in str(event.payload):
                        seen.add('foreign_goal')
                    if row['task_id'] == child and 'FOREIGN_BODY_CANARY' in str(event.payload):
                        seen.add('foreign_frozen_body')
                    return event
                def json_probe(value, *args, **kwargs):
                    if isinstance(value, str) and 'AMBIGUOUS_GOAL_CANARY' in value:
                        seen.add('ambiguous_foreign_goal')
                    return original_loads(value, *args, **kwargs)
                with patch.object(f.rt.log, '_row_to_event', side_effect=row_probe), \
                        patch('cater.rules.json.loads', side_effect=json_probe):
                    f.exchange()
                self.assertEqual(f.events('team.run_finished')[-1].payload['status'], 'completed')
                context = f.driver.calls[-1]['context']
                self.assertEqual(context['project_history']['reason'], 'metadata_query_unavailable')
                self.assertNotIn(TOOL, {tool['name'] for tool in f.driver.calls[-1]['tools']})
                self.assertEqual(seen, set(), 'Other project customer or frozen artifact content entered Python')

    def test_unknown_later_project_binding_fails_closed(self):
        f = self.fixture()
        artifact = f.publication(f.source, 'PRIVATE_DELIVERED_FIXTURE')
        f.complete(f.source)
        before = f.rt.team.project_history.index(f.task)
        entry, = [row for row in before['entries'] if row['task_id'] == f.source]
        # Model an imported/inconsistent ledger, using an append-only event;
        # the normal bind_project API deliberately refuses this rebinding.
        f.rt.log.emit(type='task.project_bound', task_id=f.source, actor=Actor.human(),
                      payload={'project_id': 'unknown-project-fixture'})
        self.assertNotEqual(f.rt.rulebook.task_project(f.source), f.project)
        after = f.rt.team.project_history.index(f.task)
        read = f.rt.team.project_history.read(f.task, {'section': 'artifact',
            'as_of_seq': before['as_of_seq'], 'delivery_seq': entry['delivery_seq'], 'artifact_seq': artifact})
        self.assertTrue(f.rt.verify_chain().ok)
        self.assertFalse(after['available'], 'Unverifiable current project binding must not reuse the old scope')
        self.assertNotEqual(read['status'], 'ok', 'Old as-of catalog must not bypass the current binding')

    def test_agent_objective_history_is_not_silently_omitted(self):
        f = self.fixture()
        sent, = f.exchange(('send_message', {'recipient': f.manager, 'text': 'Please refine the objective'}),
                           task=f.source, role=f.worker)
        self.assertEqual(sent['status'], 'ok')
        source = next(e for e in f.rt.team._messages(f.source) if e.seq == sent['message_seq'])
        updated, = f.exchange(('set_objective', {'goal': 'Refined fixture objective'}),
                              task=f.source, role=f.manager, source=source)
        self.assertEqual(updated['status'], 'ok')
        f.publication(f.source)
        f.complete(f.source)
        self.assertEqual(f.rt.team.view(f.source)['status'], 'done')
        result = f.rt.team.project_history.index(f.task)
        if result['available']:
            self.assertTrue(any(e['task_id'] == f.source for e in result['entries']),
                            'A successful catalog must not omit an unauthenticated delivered history')
        else:
            self.assertEqual(result['reason'], 'metadata_query_unavailable')

    def test_direct_human_goal_is_the_delivered_objective(self):
        f = self.fixture()
        f.rt.command(f.source, 'Change goal', goal='NEW_DIRECT_GOAL_FIXTURE')
        f.publication(f.source)
        f.complete(f.source)
        result = f.rt.team.project_history.index(f.task)
        self.assertTrue(result['available'])
        entry, = [row for row in result['entries'] if row['task_id'] == f.source]
        self.assertEqual(entry['objective_excerpt'], 'NEW_DIRECT_GOAL_FIXTURE')


if __name__ == '__main__':
    unittest.main()
