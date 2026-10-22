"""Independent boundary probes: fake Team driver, temporary DBs, local Python only."""

import copy
import hashlib
import json
import sqlite3
import sys
import unittest
from unittest.mock import patch

from cater import artifact_versions
from cater.artifact_blobs import read_blob
from cater.team import HOST
from cater.workspace_tools import WorkspaceExecutor
from cater.workspaces import WorkspaceService
import test_team_review_cycle as reviews


class ArtifactVersionBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.f = reviews.TeamReviewCycleTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.root = self.f.bind_workspace()
        self.config = self.f.root / 'supervision-workspaces.json'
        self.config.write_text(json.dumps({'workspaces': [{
            'id': 'supervision', 'title': 'Independent boundary fixture',
            'root': str(self.root), 'mode': 'execute', 'timeout': 5,
            'commands': {'python': [sys.executable, '-I', '-B', '-X', 'utf8']},
        }]}), encoding='utf-8')
        self.f.rt.workspaces = WorkspaceService(self.f.rt, self.config)
        revision = self.f.rt.workspaces.view(self.f.task)['revision']
        self.f.rt.workspaces.bind(self.f.task, 'supervision', expected_revision=revision)
        self.invocations = 0

    def archive(self, body=b"print('published-boundary')\n"):
        (self.root / 'probe.py').write_bytes(body)
        result, = self.f.exchange(self.f.worker, ('publish_workspace_archive', {
            'filename': 'project.zip', 'summary': 'Independent complete fixture', 'paths': ['probe.py'],
        }))
        self.assertEqual(result['status'], 'ok', result)
        return result['artifact_seq']

    def event(self, seq):
        return next(event for event in self.f.rt.log.read(task_id=self.f.task) if event.seq == seq)

    def revoke(self):
        policy = json.loads(self.config.read_text(encoding='utf-8'))
        policy['workspaces'][0].update(mode='edit', commands={})
        self.config.write_text(json.dumps(policy), encoding='utf-8')

    def invoke(self, selections, **changes):
        self.invocations += 1
        role = self.f.rt.team._role(self.f.manager)
        message = self.f.rt.team.receive(self.f.task, f'@{role.name} independent {self.invocations}')
        run = self.f.rt.log.emit(type='team.run_started', actor=HOST, task_id=self.f.task, payload={
            'run_id': f'independent-boundary-{self.invocations}', 'role': role.id,
            'message_seq': message.seq, 'revision': self.f.rt.team.view(self.f.task)['revision'],
            'control_seq': self.f.rt.execution_control.view(self.f.task)['seq'],
        })
        arguments = {'command': 'python', 'args': ['mount/probe.py'],
                     **({'artifact_versions': selections} if selections is not None else {}), **changes}
        self.f.driver.steps[role.id] = [self.f.native_turn('', ('workspace_run', arguments)),
                                       self.f.native_turn('', ('wait_for_reply', {}))]
        list(self.f.rt.team._run_message(self.f.task, message, role, run))
        events = [event for event in self.f.rt.log.read(task_id=self.f.task)
                  if event.payload.get('run_id') == run.payload['run_id']]
        self.assertEqual(self.f.protocol_errors, [])
        receipts = [event.payload['result'] for event in events
                    if event.type == 'team.tool_finished' and event.payload['name'] == 'workspace_run']
        return (receipts[0] if receipts else None), events

    def test_scope_revoked_during_final_byte_recheck_is_not_reported_current(self):
        for change in ('permission', 'customer'):
            with self.subTest(change=change):
                case = ArtifactVersionBoundaryTests()
                case.setUp()
                try:
                    seq = case.archive()
                    original = artifact_versions.read_members
                    reads = []

                    def read(*args, **kwargs):
                        result = original(*args, **kwargs)
                        reads.append(True)
                        if len(reads) == 2:
                            if change == 'permission':
                                case.revoke()
                            else:
                                case.f.rt.team.receive(case.f.task, 'Independent changed customer direction')
                        return result

                    with patch('cater.artifact_versions.read_members', side_effect=read):
                        result, _ = case.invoke([{'artifact_seq': seq, 'directory': 'mount'}])
                    self.assertEqual(len(reads), 2)
                    self.assertEqual(result['outcome'], 'passed', result)
                    self.assertIn('published-boundary', result['stdout']['excerpt'])
                    self.assertTrue(result['artifacts_unchanged'])
                    self.assertFalse(result['scope_current'], result)
                    self.assertFalse(result['artifact_check_valid'], result)
                finally:
                    case.doCleanups()

    def test_transient_final_reference_sqlite_error_preserves_process_receipt(self):
        seq = self.archive()
        original_execute = WorkspaceExecutor.execute
        original_files = self.f.rt.team.browser._files
        completed, injected = [], []

        def execute(*args, **kwargs):
            result = original_execute(*args, **kwargs)
            completed.append(result)
            return result

        def files(*args, **kwargs):
            if completed and not injected:
                injected.append(True)
                with patch.object(self.f.rt.log, 'read', side_effect=sqlite3.OperationalError('temporary fixture read failure')):
                    return original_files(*args, **kwargs)
            return original_files(*args, **kwargs)

        with patch('cater.team_artifact_check.WorkspaceExecutor.execute', new=execute), \
                patch.object(self.f.rt.team.browser, '_files', side_effect=files):
            result, events = self.invoke([{'artifact_seq': seq, 'directory': 'mount'}])
        self.assertEqual(len(injected), 1)
        self.assertEqual(completed[0]['outcome'], 'passed')
        self.assertIn('published-boundary', completed[0]['stdout']['excerpt'])
        self.assertIsNotNone(result, {
            'run_finished': [event.payload for event in events if event.type == 'team.run_finished'],
            'pending': self.f.rt.team._pending_operations(self.f.task),
        })
        self.assertEqual(result['outcome'], 'passed', result)
        self.assertEqual(result['exit_code'], 0)
        self.assertFalse(result['scope_current'])

    def test_all_mount_paths_preflight_before_first_archive_blob(self):
        seq = self.archive()
        event = self.event(seq)
        manifest = copy.deepcopy(event.payload['archive'])
        deep_path = '/'.join(['d'] * 31 + ['probe.py'])
        manifest['files'][0]['path'] = deep_path
        malformed = self.f.rt.log.emit(type=event.type, actor=event.actor, task_id=self.f.task,
                                      payload={**event.payload, 'archive': manifest})
        with patch('cater.archive_members.read_blob', wraps=read_blob) as blob, \
                patch('cater.team_artifact_check._write') as write, \
                patch('cater.team_artifact_check.WorkspaceExecutor.execute') as execute:
            result, _ = self.invoke([{'artifact_seq': seq, 'directory': 'first'},
                                     {'artifact_seq': malformed.seq, 'directory': 'second'}])
        self.assertEqual(result['outcome'], 'not_started', result)
        blob.assert_not_called()
        write.assert_not_called()
        execute.assert_not_called()

    def test_mixed_text_and_archives_total_budget_precedes_blob_read(self):
        seq = self.archive()
        event = self.event(seq)
        size = 16 * 1024 * 1024
        text = 'x'
        text_event = self.f.rt.log.emit(type='deliverable.published', actor=event.actor, task_id=self.f.task,
            payload={'filename': 'large.txt', 'format': 'text', 'content': text,
                     'content_sha256': hashlib.sha256(text.encode()).hexdigest()})
        manifest = copy.deepcopy(event.payload['archive'])
        manifest['total_bytes'] = size
        manifest['files'][0]['bytes'] = size
        large = [self.f.rt.log.emit(type=event.type, actor=event.actor, task_id=self.f.task,
                                   payload={**event.payload, 'archive': manifest}) for _ in range(2)]
        with patch('cater.archive_members.read_blob', wraps=read_blob) as blob, \
                patch('cater.team_artifact_check._write') as write:
            result, _ = self.invoke([{'artifact_seq': large[0].seq, 'directory': 'first'},
                                     {'artifact_seq': text_event.seq, 'directory': 'text'},
                                     {'artifact_seq': large[1].seq, 'directory': 'second'}])
        self.assertEqual(result['outcome'], 'not_started', result)
        blob.assert_not_called()
        write.assert_not_called()

    def test_casefold_only_and_compatibility_mount_aliases_reject_before_blob(self):
        first = self.archive()
        second = self.archive(b"print('second')\n")
        for a, b in (('Stra\u00dfe', 'STRASSE'), ('\u03c2', '\u03c3'), ('\uff21', 'a')):
            with self.subTest(mounts=(a, b)), \
                    patch('cater.archive_members.read_blob', wraps=read_blob) as blob:
                result, _ = self.invoke([{'artifact_seq': first, 'directory': a},
                                         {'artifact_seq': second, 'directory': b}])
                self.assertEqual(result['outcome'], 'not_started', result)
                blob.assert_not_called()

    def test_historical_member_permission_tamper_never_launches(self):
        seq = self.archive()
        event = self.event(seq)
        manifest = copy.deepcopy(event.payload['archive'])
        manifest['files'][0]['executable'] = True
        tampered = self.f.rt.log.emit(type=event.type, actor=event.actor, task_id=self.f.task,
                                     payload={**event.payload, 'archive': manifest})
        self.archive(b"print('newest')\n")
        with patch('cater.team_artifact_check.WorkspaceExecutor.execute') as execute:
            result, _ = self.invoke([{'artifact_seq': tampered.seq, 'directory': 'mount'}])
        self.assertEqual(result['outcome'], 'not_started', result)
        execute.assert_not_called()

    def test_history_alias_relaxation_does_not_change_legacy_or_browser_selector(self):
        first = self.f.publish("print('lower')\n", filename='probe.py')
        second = self.f.publish("print('upper')\n", filename='PROBE.py')
        result, _ = self.invoke([{'artifact_seq': first, 'directory': 'mount'},
                                 {'artifact_seq': second, 'directory': 'other'}])
        self.assertEqual(result['outcome'], 'passed', result)
        self.assertTrue(result['artifact_check_valid'])
        self.assertTrue(result['selected_versions_current'])
        for seq in (first, second):
            with self.subTest(seq=seq), self.assertRaises(ValueError):
                self.f.rt.team.browser._inputs(self.f.task, {'artifact_seqs': [seq]})
        with patch('cater.team_artifact_check.WorkspaceExecutor.execute') as execute:
            legacy, _ = self.invoke(None, artifact_seqs=[first])
        self.assertEqual(legacy['outcome'], 'not_started')
        execute.assert_not_called()

    def test_legacy_selector_final_byte_recheck_invalidates_changed_scope(self):
        from cater import archive_members

        for change in ('permission', 'customer'):
            with self.subTest(change=change):
                case = ArtifactVersionBoundaryTests()
                case.setUp()
                try:
                    seq = case.archive()
                    original = archive_members.read_members
                    reads = []

                    def read(*args, **kwargs):
                        result = original(*args, **kwargs)
                        reads.append(True)
                        if len(reads) == 2:
                            if change == 'permission':
                                case.revoke()
                            else:
                                case.f.rt.team.receive(case.f.task, 'Independent legacy customer direction changed')
                        return result

                    with patch('cater.archive_members.read_members', side_effect=read):
                        result, _ = case.invoke(None, artifact_seqs=[seq], args=['probe.py'])
                    self.assertEqual(len(reads), 2)
                    self.assertIsNotNone(result)
                    self.assertEqual(result['outcome'], 'passed', result)
                    self.assertEqual(result['exit_code'], 0)
                    self.assertIn('published-boundary', result['stdout']['excerpt'])
                    self.assertTrue(result['complete'])
                    self.assertTrue(result['artifacts_unchanged'])
                    self.assertFalse(result['scope_current'], result)
                    self.assertFalse(result['artifact_check_valid'])
                    self.assertNotIn('artifact_selection', result)
                    self.assertNotIn('selected_versions_current', result)
                    self.assertEqual(result['checked_artifacts'], [{
                        'seq': seq, 'filename': 'project.zip',
                        'content_sha256': case.event(seq).payload['content_sha256'],
                    }])
                    self.assertEqual([entry['path'] for entry in result['checked_files']], ['probe.py'])
                    self.assertTrue(all('source_path' not in entry for entry in result['checked_files']))
                    self.assertEqual(case.f.rt.team._pending_operations(case.f.task), [])
                    self.assertTrue(case.f.rt.verify_chain().ok)
                finally:
                    case.doCleanups()

    def test_legacy_selector_tail_sqlite_errors_preserve_real_success_and_failure(self):
        # Post-run reads: initial scope reference, final input reference, terminal scope reference.
        for failed_read, exit_code in ((1, 0), (2, 9), (3, 0)):
            with self.subTest(failed_read=failed_read, exit_code=exit_code):
                case = ArtifactVersionBoundaryTests()
                case.setUp()
                try:
                    seq = case.archive()
                    original_execute = WorkspaceExecutor.execute
                    original_files = case.f.rt.team.browser._files
                    completed, reads, injected = [], [], []

                    def execute(*args, **kwargs):
                        result = original_execute(*args, **kwargs)
                        completed.append(result)
                        return result

                    def files(*args, **kwargs):
                        if completed:
                            reads.append(True)
                            if len(reads) == failed_read:
                                injected.append(True)
                                with patch.object(case.f.rt.log, 'read', side_effect=sqlite3.OperationalError('legacy tail fixture read failure')):
                                    return original_files(*args, **kwargs)
                        return original_files(*args, **kwargs)

                    script = ("import sys; print('legacy-tail-out'); "
                              "print('legacy-tail-err', file=sys.stderr); "
                              f'raise SystemExit({exit_code})')
                    with patch('cater.team_artifact_check.WorkspaceExecutor.execute', new=execute), \
                            patch.object(case.f.rt.team.browser, '_files', side_effect=files):
                        result, _ = case.invoke(None, artifact_seqs=[seq], args=['-c', script])
                    self.assertEqual(len(injected), 1)
                    self.assertEqual(len(completed), 1)
                    self.assertIsNotNone(result)
                    for key in ('status', 'outcome', 'exit_code', 'stdout', 'stderr', 'complete'):
                        self.assertEqual(result[key], completed[0][key], (key, result))
                    self.assertEqual(result['outcome'], 'failed' if exit_code else 'passed')
                    self.assertEqual(result['exit_code'], exit_code)
                    self.assertIn('legacy-tail-out', result['stdout']['excerpt'])
                    self.assertIn('legacy-tail-err', result['stderr']['excerpt'])
                    self.assertTrue(result['complete'])
                    self.assertTrue(result['artifacts_unchanged'])
                    self.assertFalse(result['scope_current'])
                    self.assertFalse(result['artifact_check_valid'])
                    self.assertNotIn('artifact_selection', result)
                    self.assertNotIn('selected_versions_current', result)
                    self.assertEqual(case.f.rt.team._pending_operations(case.f.task), [])
                    self.assertTrue(case.f.rt.verify_chain().ok)
                finally:
                    case.doCleanups()

    def test_latest_diagnostic_permission_revocation_preserves_process_and_invalidates_scope(self):
        for exit_code in (0, 9):
            with self.subTest(exit_code=exit_code):
                case = ArtifactVersionBoundaryTests()
                case.setUp()
                try:
                    seq = case.archive()
                    original_diagnostic = artifact_versions.checked_artifacts
                    original_read = case.f.rt.log.read
                    observed = []

                    def diagnose(team, task_id, sources):
                        def read(*args, **kwargs):
                            events = original_read(*args, **kwargs)
                            case.revoke()
                            observed.append(True)
                            return events

                        with patch.object(team.rt.log, 'read', side_effect=read):
                            return original_diagnostic(team, task_id, sources)

                    script = ("import sys; print('latest-tail-out'); "
                              "print('latest-tail-err', file=sys.stderr); "
                              f'raise SystemExit({exit_code})')
                    with patch('cater.artifact_versions.checked_artifacts', side_effect=diagnose):
                        result, _ = case.invoke([{'artifact_seq': seq, 'directory': 'mount'}],
                                                args=['-c', script])
                    self.assertEqual(len(observed), 1)
                    self.assertIsNotNone(result)
                    self.assertEqual(result['outcome'], 'failed' if exit_code else 'passed')
                    self.assertEqual(result['status'], 'error' if exit_code else 'ok')
                    self.assertEqual(result['exit_code'], exit_code)
                    self.assertIn('latest-tail-out', result['stdout']['excerpt'])
                    self.assertIn('latest-tail-err', result['stderr']['excerpt'])
                    self.assertTrue(result['complete'])
                    self.assertTrue(result['artifacts_unchanged'])
                    self.assertFalse(result['scope_current'])
                    self.assertFalse(result['artifact_check_valid'])
                    self.assertEqual(result['artifact_selection'], 'version_directories')
                    self.assertTrue(result['selected_versions_current'])
                    self.assertEqual(result['checked_artifacts'][0]['latest_seq_at_check'], seq)
                    self.assertTrue(result['checked_artifacts'][0]['is_latest_at_check'])
                    self.assertEqual(result['checked_files'][0]['source_path'], 'probe.py')
                    self.assertEqual(result['checked_files'][0]['path'], 'mount/probe.py')
                    self.assertEqual(case.f.rt.team._pending_operations(case.f.task), [])
                    self.assertTrue(case.f.rt.verify_chain().ok)
                finally:
                    case.doCleanups()


if __name__ == '__main__':
    unittest.main()
