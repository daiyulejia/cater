"""Publication discovery in real context builders; fixture turns, not model QA."""
from copy import deepcopy
import unittest

from cater.events import Actor
from cater.team_context import model_context
from cater.team_instructions import context_instructions
import test_team_review_cycle as cycle


class ArtifactHistoryContextTests(unittest.TestCase):
    def setUp(self):
        self.f = cycle.TeamReviewCycleTests()
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()

    def context(self):
        f = self.f
        message = f.rt.team.receive(f.task, '查看原始版本与来源，不创建新的工作', request_id='artifact-history-context')
        return f.rt.team._context(f.task, message, f.rt.organization.leader)

    def test_raw_and_compact_metadata_keep_real_publisher_source_size_and_not_editor(self):
        f = self.f
        old = f.publish('first\n', author=f.manager)
        new = f.publish('updated\n', author=f.worker)
        raw = self.context()
        saved = deepcopy(raw)
        compact = model_context(raw)
        self.assertEqual(raw, saved)
        self.assertEqual([a['seq'] for a in raw['artifacts']], [old, new])
        self.assertEqual([a['actor'] for a in raw['artifacts']], [f.manager, f.worker])
        row, = compact['artifacts']
        self.assertEqual((row['seq'], row['actor'], row['source'], row['content_bytes'], row['versions_total']),
                         (new, f.worker, 'team', len('updated\n'.encode()), 2))
        self.assertNotIn('content', row)
        self.assertNotIn('editor', row)
        self.assertEqual(compact['context_window']['artifact_versions_reader'],
                         {'name': 'read_artifact', 'section': 'versions', 'argument': 'artifact_seq'})
        self.assertEqual(compact['context_window']['artifact_versions_total'], 2)

    def test_same_bytes_are_distinct_publications_but_not_current_approval_or_parentage(self):
        f = self.f
        old = f.publish('same\n', author=f.manager)
        new = f.publish('same\n', author=f.worker)
        row, = model_context(self.context())['artifacts']
        self.assertEqual((row['seq'], row['versions_total']), (new, 2))
        self.assertNotEqual(old, new)
        for key in ('approved', 'previous_seq', 'parent_seq', 'modified_by'):
            self.assertNotIn(key, row)

    def test_single_files_do_not_inject_history_hints_or_counts(self):
        f = self.f
        f.publish('first', filename='One.py')
        f.publish('second', filename='one.py')
        compact = model_context(self.context())
        self.assertNotIn('artifact_versions_reader', compact['context_window'])
        self.assertTrue(all('versions_total' not in row for row in compact['artifacts']))

    def test_legacy_unnamed_artifacts_do_not_invent_a_shared_filename_history(self):
        f = self.f
        for i in range(2):
            f.rt.log.emit(type='deliverable.published', task_id=f.task, actor=Actor.agent(f.worker, 'dev'),
                payload={'title': f'Legacy {i}', 'summary': 'No filename'})
        compact = model_context(self.context())
        self.assertNotIn('artifact_versions_reader', compact['context_window'])
        self.assertNotIn('versions_total', compact['artifacts'][0])

    def test_history_discovery_does_not_repeat_old_bodies_or_long_summaries(self):
        f = self.f
        for i in range(4):
            f.publish(str(i) + 'x' * 12000)
        raw = self.context()
        for row in raw['artifacts']:
            row['summary'] = '月' * 3000
        compact = model_context(raw)
        row, = compact['artifacts']
        self.assertEqual(row['versions_total'], 4)
        self.assertLessEqual(len(row['summary'].encode()), 512)
        self.assertTrue(row['summary_truncated'])
        self.assertNotIn('content', row)
        self.assertEqual(len(f.rt.task(f.task).deliverables), 4)

    def test_baseline_guidance_is_selected_by_done_human_state_not_customer_words(self):
        marker = '讲解改动须明确比较的旧、新版本'
        for text in ('你好', '比较旧版新版，不要改磁盘', '可以继续吗'):
            active = {'current_work': {'status': 'running'}, 'message': {'sender_kind': 'human', 'text': text}}
            self.assertNotIn(marker, context_instructions(active))
            done = {**active, 'current_work': {'status': 'done'}}
            note = context_instructions(done)
            self.assertIn(marker, note)
            self.assertIn('未发布新版不证明未改磁盘文件', note)
            self.assertNotIn(marker, context_instructions({**done, 'message': {'sender_kind': 'agent', 'text': text}}))
