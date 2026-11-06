"""Two-connection project history snapshot boundaries, using disposable ledgers."""
from contextlib import closing
import unittest
from unittest.mock import patch

from cater.events import Actor, EventLog
from cater.project_history import TOOL
import test_project_history_metadata as fixtures


class ProjectHistoryReadSnapshotTests(unittest.TestCase):
    def fixture(self):
        f = fixtures.ProjectMetadataTests(methodName='runTest')
        f.setUp()
        self.addCleanup(f.doCleanups)
        artifact = f.publication(f.source, 'synthetic private historical artifact')
        f.complete(f.source)
        index = f.rt.team.project_history.index(f.task)
        entry, = index['entries']
        self.assertEqual(f.rt.log.db.execute('PRAGMA journal_mode=WAL').fetchone()[0].lower(), 'wal')
        return f, artifact, index, entry

    @staticmethod
    def args(section, index, entry, artifact):
        return {'section': section, 'as_of_seq': index['as_of_seq'],
                **({'delivery_seq': entry['delivery_seq']} if section != 'index' else {}),
                **({'artifact_seq': artifact} if section == 'artifact' else {})}

    def test_binding_committed_before_read_rejects_old_anchor(self):
        for target in (True, False):
            for section in ('index', 'delivery', 'artifact'):
                with self.subTest(target=target, section=section):
                    f, artifact, index, entry = self.fixture()
                    with closing(EventLog(f.rt.log.path)) as writer:
                        writer.emit(type='task.project_bound', task_id=f.task if target else f.source,
                                    actor=Actor.agent('untrusted', 'worker'), payload={'project_id': f.project})
                    result = f.rt.team.project_history.read(f.task, self.args(section, index, entry, artifact),
                                                            expected_project_id=f.project)
                    self.assertNotEqual(result['status'], 'ok')
                    self.assertNotIn('synthetic private historical artifact', str(result))

    def test_valid_binding_committed_before_read_rejects_old_anchor(self):
        for target in (True, False):
            with self.subTest(target=target):
                f, artifact, index, entry = self.fixture()
                other = f.rt.rulebook.create_project('Other snapshot project')['id']
                with closing(EventLog(f.rt.log.path)) as writer:
                    writer.emit(type='task.project_bound', task_id=f.task if target else f.source,
                                actor=Actor.human(), payload={'project_id': other})
                result = f.rt.team.project_history.read(f.task, self.args('artifact', index, entry, artifact),
                                                        expected_project_id=f.project)
                self.assertNotEqual(result['status'], 'ok')
                self.assertNotIn('synthetic private historical artifact', str(result))

    def test_binding_after_snapshot_does_not_mix_scope_and_body(self):
        for target in (True, False):
            for section in ('index', 'delivery', 'artifact'):
                with self.subTest(target=target, section=section):
                    f, artifact, index, entry = self.fixture()
                    history = f.rt.team.project_history
                    original = history._catalog
                    with closing(EventLog(f.rt.log.path)) as writer:
                        def checked(*args, **kwargs):
                            self.assertTrue(f.rt.log.db.in_transaction)
                            result = original(*args, **kwargs)
                            before = history._anchor()
                            writer.emit(type='task.project_bound', task_id=f.task if target else f.source,
                                        actor=Actor.agent('untrusted', 'worker'), payload={'project_id': f.project})
                            self.assertEqual(history._anchor(), before)
                            return result
                        with patch.object(history, '_catalog', side_effect=checked):
                            result = history.read(f.task, self.args(section, index, entry, artifact),
                                                  expected_project_id=f.project)
                    self.assertEqual(result['status'], 'ok')
                    if section == 'artifact':
                        self.assertEqual(result['content'], 'synthetic private historical artifact')
                    self.assertFalse(f.rt.log.db.in_transaction)
                    later = history.read(f.task, self.args(section, index, entry, artifact),
                                         expected_project_id=f.project)
                    self.assertNotEqual(later['status'], 'ok')
                    self.assertNotIn('synthetic private historical artifact', str(later))

    def test_existing_transaction_is_neither_committed_nor_rolled_back(self):
        f, artifact, index, entry = self.fixture()
        with f.rt.log.atomic():
            f.rt.log.emit(type='task.project_bound', task_id=f.task, actor=Actor.agent('bad', 'worker'),
                          payload={'project_id': f.project})
            result = f.rt.team.project_history.read(f.task, self.args('artifact', index, entry, artifact),
                                                    expected_project_id=f.project)
            self.assertNotEqual(result['status'], 'ok')
            self.assertTrue(f.rt.log.db.in_transaction)
        self.assertFalse(f.rt.log.db.in_transaction)
        self.assertNotEqual(f.rt.team.project_history.read(f.task, self.args('artifact', index, entry, artifact))['status'], 'ok')

    def test_exception_releases_only_owned_snapshot(self):
        f, artifact, index, entry = self.fixture()
        history = f.rt.team.project_history
        args = self.args('artifact', index, entry, artifact)
        with patch.object(history, '_catalog', side_effect=RuntimeError('synthetic failure')):
            with self.assertRaisesRegex(RuntimeError, 'synthetic failure'):
                history.read(f.task, args)
            self.assertFalse(f.rt.log.db.in_transaction)
            with f.rt.log.atomic():
                with self.assertRaisesRegex(RuntimeError, 'synthetic failure'):
                    history.read(f.task, args)
                self.assertTrue(f.rt.log.db.in_transaction)

    def test_real_tool_allows_snapshot_then_concurrent_binding(self):
        f, artifact, index, entry = self.fixture()
        history = f.rt.team.project_history
        real_read, catalog = history.read, history._catalog
        observations = []
        with closing(EventLog(f.rt.log.path)) as writer:
            def checked_catalog(*args, **kwargs):
                result = catalog(*args, **kwargs)
                writer.emit(type='task.project_bound', task_id=f.task,
                            actor=Actor.agent('untrusted', 'worker'), payload={'project_id': f.project})
                observations.append(f.rt.log.db.in_transaction)
                return result
            def actual_read(*args, **kwargs):
                with patch.object(history, '_catalog', side_effect=checked_catalog):
                    return real_read(*args, **kwargs)
            with patch.object(history, 'read', side_effect=actual_read):
                result, = f.exchange((TOOL, self.args('artifact', index, entry, artifact)))
            self.assertEqual(result['status'], 'ok')
            self.assertEqual(result['content'], 'synthetic private historical artifact')
            self.assertTrue(observations)
            self.assertTrue(all(observations))
        self.assertNotEqual(history.read(f.task, self.args('artifact', index, entry, artifact))['status'], 'ok')


if __name__ == '__main__':
    unittest.main()
