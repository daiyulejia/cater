"""Project catalog reuse is limited to one real model-input read snapshot."""
import json
import unittest
from contextlib import closing
from unittest.mock import patch

from cater.events import Actor, EventLog
from cater.project_history import TOOL, _MetadataUnavailable
import test_team_project_history as fixture


class ProjectHistorySnapshotReuseTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.TeamProjectHistoryTests(methodName='runTest')
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def claim(self):
        f = self.f
        source = f.rt.team.receive(f.task, 'Inspect project history', request_id='snapshot-history-input')
        claimed = f.rt.team._claim(f.task)
        self.assertEqual(claimed[0].seq, source.seq)
        return claimed

    def model_input(self, claimed):
        source, role, run = claimed
        return self.f.rt.team._model_input(self.f.task, source, role, run_id=run.payload['run_id'])

    def test_150_unfinished_tasks_are_projected_once_with_equivalent_catalog(self):
        f = self.f
        for i in range(150):
            f.rt.create_task('PRIVATE_UNFINISHED_FIXTURE', project_id=f.project)
        claimed = self.claim()
        history = f.rt.team.project_history
        expected = history.index(f.task)
        self.assertTrue(expected['available'])
        self.assertEqual(expected['entries'], [])
        with patch.object(history, 'index', wraps=history.index) as index, \
             patch.object(history, '_latest', wraps=history._latest) as latest, \
             patch.object(history, '_state', wraps=history._state) as state, \
             patch.object(f.rt.team, '_skill_scope', wraps=f.rt.team._skill_scope) as skill:
            context, _, _, tools, basis = self.model_input(claimed)
        self.assertEqual(index.call_count, 1)
        self.assertEqual(latest.call_count, 151)
        self.assertEqual(state.call_count, 1)
        self.assertEqual(context['project_history'], expected)
        self.assertEqual(context['project_history']['as_of_seq'], basis['as_of_seq'])
        self.assertTrue(skill.call_args.kwargs['has_project_history'])
        self.assertIn(TOOL, {t['name'] for t in tools})
        self.assertNotIn('PRIVATE_UNFINISHED_FIXTURE', json.dumps(context, ensure_ascii=False))

    def test_completed_catalog_and_independent_context_keep_one_snapshot(self):
        f = self.f
        f.publication(f.source)
        f.complete(f.source)
        claimed = self.claim()
        history = f.rt.team.project_history
        expected = history.index(f.task)
        self.assertEqual(len(expected['entries']), 1)
        with patch.object(history, 'index', wraps=history.index) as index:
            context, _, _, tools, _ = self.model_input(claimed)
            self.assertEqual(index.call_count, 1)
            self.assertEqual(context['project_history'], expected)
            self.assertIn(TOOL, {t['name'] for t in tools})
        with patch.object(history, 'index', wraps=history.index) as index:
            context = f.rt.team._context(f.task, claimed[0], claimed[1])
            self.assertEqual(index.call_count, 1)
            self.assertTrue(context['project_history']['available'])
        # Outside context assembly, skill and direct history reads remain fresh.
        with patch.object(history, 'index', wraps=history.index) as index:
            self.assertIn(TOOL, f.rt.team._skill_scope(f.task, claimed[1])['tools'])
            self.assertEqual(index.call_count, 1)

    def test_metadata_unavailable_is_consistent_and_unexpected_errors_propagate(self):
        f = self.f
        claimed = self.claim()
        history = f.rt.team.project_history
        with patch.object(history, '_state', side_effect=_MetadataUnavailable('fixture')), \
             patch.object(history, 'index', wraps=history.index) as index, \
             patch.object(f.rt.team, '_skill_scope', wraps=f.rt.team._skill_scope) as skill:
            context, _, _, tools, _ = self.model_input(claimed)
            self.assertEqual(index.call_count, 1)
            self.assertEqual(context['project_history']['reason'], 'metadata_query_unavailable')
            self.assertFalse(skill.call_args.kwargs['has_project_history'])
            self.assertNotIn(TOOL, {t['name'] for t in tools})
        with patch.object(history, 'index', side_effect=RuntimeError('synthetic business failure')):
            with self.assertRaisesRegex(RuntimeError, 'synthetic business failure'):
                self.model_input(claimed)
        self.assertFalse(f.rt.log.db.in_transaction)

    def test_unbound_task_has_no_project_tool(self):
        f = self.f
        task = f.rt.create_task('Unbound fixture')
        f.rt.team.enable(task)
        source = f.rt.team.receive(task, 'Inspect', request_id='unbound-snapshot-fixture')
        claimed = f.rt.team._claim(task)
        self.assertEqual(claimed[0].seq, source.seq)
        with patch.object(f.rt.team.project_history, 'index', wraps=f.rt.team.project_history.index) as index:
            context, _, _, tools, _ = f.rt.team._model_input(task, *claimed[:2], run_id=claimed[2].payload['run_id'])
            self.assertEqual(index.call_count, 1)
        self.assertNotIn('project_history', context)
        self.assertNotIn(TOOL, {t['name'] for t in tools})

    def test_other_connection_rebind_is_not_reused_or_authorized(self):
        f = self.f
        f.publication(f.source)
        f.complete(f.source)
        other = f.rt.rulebook.create_project('Other fixture project')['id']
        claimed = self.claim()
        before, _, _, _, _ = self.model_input(claimed)
        self.assertEqual(before['project_history']['project_id'], f.project)
        self.assertEqual(len(before['project_history']['entries']), 1)
        with closing(EventLog(f.rt.log.path)) as writer:
            writer.emit(type='task.project_bound', task_id=f.task, actor=Actor.human(),
                        payload={'project_id': other})
        history = f.rt.team.project_history
        old_request = history.read(f.task, {'section': 'index'}, expected_project_id=f.project)
        self.assertEqual(old_request['code'], 'project_history_scope_changed')
        self.assertNotIn('entries', old_request)
        after, _, _, _, _ = self.model_input(claimed)
        self.assertEqual(after['project_history']['project_id'], other)
        self.assertEqual(after['project_history']['entries'], [])
        self.assertGreater(after['project_history']['as_of_seq'], before['project_history']['as_of_seq'])
        self.assertNotIn(str(before['project_history']['entries'][0]['delivery_seq']),
                         json.dumps(after['project_history']['entries']))

    def test_rebind_during_snapshot_does_not_mix_tool_and_context_scopes(self):
        f = self.f
        f.publication(f.source)
        f.complete(f.source)
        other = f.rt.rulebook.create_project('Other concurrent fixture')['id']
        claimed = self.claim()
        history = f.rt.team.project_history
        original = history.index
        self.assertEqual(f.rt.log.db.execute('PRAGMA journal_mode=WAL').fetchone()[0].lower(), 'wal')
        with closing(EventLog(f.rt.log.path)) as writer:
            def index_then_rebind(task):
                self.assertTrue(f.rt.log.db.in_transaction)
                result = original(task)
                writer.emit(type='task.project_bound', task_id=f.task, actor=Actor.human(),
                            payload={'project_id': other})
                return result
            with patch.object(history, 'index', side_effect=index_then_rebind) as index, \
                 patch.object(f.rt.team, '_skill_scope', wraps=f.rt.team._skill_scope) as skill:
                context, _, _, tools, basis = self.model_input(claimed)
            self.assertEqual(index.call_count, 1)
        self.assertEqual(context['project_history']['project_id'], f.project)
        self.assertEqual(context['project_history']['as_of_seq'], basis['as_of_seq'])
        self.assertTrue(skill.call_args.kwargs['has_project_history'])
        self.assertIn(TOOL, {t['name'] for t in tools})
        rejected = history.read(f.task, {'section': 'index'}, expected_project_id=f.project)
        self.assertEqual(rejected['code'], 'project_history_scope_changed')
        self.assertEqual(history.index(f.task)['project_id'], other)

    def test_rolled_back_binding_is_not_reused_after_transaction(self):
        f = self.f
        other = f.rt.rulebook.create_project('Rolled back fixture')['id']
        claimed = self.claim()
        with self.assertRaisesRegex(RuntimeError, 'rollback fixture'):
            with f.rt.log.atomic():
                f.rt.log.emit(type='task.project_bound', task_id=f.task, actor=Actor.human(),
                              payload={'project_id': other})
                context, _, _, _, _ = self.model_input(claimed)
                self.assertEqual(context['project_history']['project_id'], other)
                raise RuntimeError('rollback fixture')
        self.assertFalse(f.rt.log.db.in_transaction)
        context, _, _, _, _ = self.model_input(claimed)
        self.assertEqual(context['project_history']['project_id'], f.project)


if __name__ == '__main__':
    unittest.main()
