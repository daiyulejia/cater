"""Execute published bytes through the existing native workspace command pipeline."""

from contextlib import contextmanager
import json
import os
import sys
import unittest
from unittest.mock import patch

from cater.workspace_tools import WorkspaceExecutor
from cater.workspaces import WorkspaceService
import test_team_review_cycle as reviews


class TeamArtifactCheckTests(unittest.TestCase):
    def setUp(self):
        self.f = reviews.TeamReviewCycleTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.root = self.f.bind_workspace()
        self.config = self.f.root / 'supervision-workspaces.json'
        self.policy()
        self.files = {'package/check.py': b"from pathlib import Path\nassert Path('data.bin').read_bytes() == b'\\x00\\xff'\nprint('published bytes')\n",
                      'package/data.bin': b'\x00\xff', 'package/empty.txt': b''}
        for name, body in self.files.items():
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
        self.seq = self.archive()

    def policy(self, mode='execute', timeout=5):
        self.config.write_text(json.dumps({'workspaces': [{
            'id': 'supervision', 'title': 'Check fixture', 'root': str(self.root), 'mode': mode,
            'commands': {'python': [sys.executable, '-I', '-B', '-X', 'utf8']} if mode == 'execute' else {},
            'timeout': timeout}]}), encoding='utf-8')
        self.f.rt.workspaces = WorkspaceService(self.f.rt, self.config)
        revision = self.f.rt.workspaces.view(self.f.task)['revision']
        self.f.rt.workspaces.bind(self.f.task, 'supervision', expected_revision=revision)

    def archive(self):
        result, = self.f.exchange(self.f.worker, ('publish_workspace_archive', {
            'filename': 'project.zip', 'summary': 'Fixture files', 'paths': list(self.files)}))
        self.assertEqual(result['status'], 'ok', result)
        return result['artifact_seq']

    def check(self, *, expected_statuses=('waiting', 'completed'), **changes):
        args = {'command': 'python', 'args': ['check.py'], 'cwd': 'package', 'artifact_seqs': [self.seq], **changes}
        result, = self.f.exchange(self.f.manager, ('workspace_run', args), expected_statuses=expected_statuses)
        return result

    def test_published_zip_executes_not_changed_workspace_and_keeps_version(self):
        (self.root / 'package/check.py').write_text("raise RuntimeError('unpublished')", encoding='utf-8')
        before = self.f.rt.team.view(self.f.task)
        result = self.check()
        self.assertEqual(result['outcome'], 'passed', result)
        self.assertEqual(result['stdout']['excerpt'].strip(), 'published bytes')
        self.assertTrue(result['artifact_check_valid'])
        self.assertEqual(result['cwd'], 'package')
        self.assertEqual(result['checked_artifacts'][0]['seq'], self.seq)
        self.assertEqual({f['path'] for f in result['checked_files']}, set(self.files))
        self.assertEqual(self.f.rt.team.view(self.f.task)['revision'], before['revision'])
        self.assertEqual(self.f.rt.team_reviews.view(self.f.task), [])
        self.assertEqual(self.f.rt.team._pending_operations(self.f.task), [])
        self.assertTrue(self.f.rt.verify_chain().ok)

    def test_plain_files_root_and_normal_workspace_run_still_work(self):
        seq = self.f.publish("print('plain')\n", filename='plain.py')
        result = self.check(artifact_seqs=[seq], cwd='.', args=['plain.py'])
        self.assertEqual(result['stdout']['excerpt'].strip(), 'plain')
        normal, = self.f.exchange(self.f.manager, ('workspace_run', {'command': 'python', 'args': ['package/check.py']}))
        # Existing root behavior is observable: check.py expects data in its cwd.
        self.assertEqual(normal['outcome'], 'failed')
        self.assertNotIn('checked_artifacts', normal)
        executor = WorkspaceExecutor(self.f.rt.workspaces.config['supervision']['policy'])
        self.assertNotIn('artifact_seqs', next(t for t in executor.schemas() if t['name'] == 'workspace_run')['input_schema']['properties'])

    def test_invalid_selection_never_falls_back_to_real_project(self):
        other = self.f.rt.create_task('Other group')
        self.f.rt.team.enable(other)
        foreign = self.f.publish('foreign', task_id=other)
        for seqs in ([], [True], [self.seq, self.seq], [foreign], [999999]):
            with self.subTest(seqs=seqs), patch('cater.team_artifact_check.WorkspaceExecutor.execute') as run:
                result = self.check(artifact_seqs=seqs)
                self.assertEqual(result['outcome'], 'not_started')
                run.assert_not_called()
        latest = self.archive()
        self.assertNotEqual(latest, self.seq)
        self.assertEqual(self.check()['code'], 'invalid_artifact_check')

    def test_portable_nonbrowser_paths_and_alias_rejection(self):
        self.files = {'.config-test/has%name#1.txt': b'input'}
        path = self.root / next(iter(self.files))
        path.parent.mkdir()
        path.write_bytes(b'input')
        self.seq = self.archive()
        result = self.check(cwd='.', args=['-c', "from pathlib import Path; assert Path('.config-test/has%name#1.txt').read_text() == 'input'"])
        self.assertEqual(result['outcome'], 'passed', result)
        a = self.f.publish('a', filename='note.txt')
        b = self.f.publish('b', filename='NOTE.txt')
        self.assertEqual(self.check(artifact_seqs=[a, b])['outcome'], 'not_started')

    def test_failures_preserve_output_and_do_not_approve(self):
        result = self.check(args=['-c', "import sys; print('failure detail', file=sys.stderr); sys.exit(3)"])
        self.assertEqual((result['outcome'], result['exit_code']), ('failed', 3))
        self.assertIn('failure detail', result['stderr']['excerpt'])
        self.assertTrue(result['artifact_check_valid'])
        self.assertEqual(self.f.rt.team_reviews.view(self.f.task), [])

    def test_input_mutation_invalidates_check_but_does_not_change_publication(self):
        result = self.check(args=['-c', "from pathlib import Path; Path('data.bin').write_bytes(b'changed')"])
        self.assertEqual(result['exit_code'], 0)
        self.assertFalse(result['artifacts_unchanged'])
        self.assertFalse(result['artifact_check_valid'])
        self.assertEqual((self.root / 'package/data.bin').read_bytes(), b'\x00\xff')
        source = self.f.rt.team.browser._inputs(self.f.task, {'artifact_seqs': [self.seq]}, browser_paths=False)[1]
        self.assertEqual(next(f['body'] for f in source if f['path'].endswith('data.bin')), b'\x00\xff')

    def test_republication_during_execution_makes_receipt_historical(self):
        original = WorkspaceExecutor.execute
        paths = []
        def execute(executor, name, args):
            paths.append(executor.policy.root)
            result = original(executor, name, args)
            event = next(e for e in self.f.rt.log.read(task_id=self.f.task) if e.seq == self.seq)
            self.f.rt.log.emit(type=event.type, actor=event.actor, task_id=self.f.task, payload=event.payload)
            return result
        with patch('cater.team_artifact_check.WorkspaceExecutor.execute', new=execute):
            result = self.check()
        self.assertEqual(result['outcome'], 'passed')
        self.assertFalse(result['scope_current'])
        self.assertFalse(result['artifact_check_valid'])
        self.assertFalse(paths[0].exists())

    def test_pause_during_copy_prevents_launch(self):
        from cater import team_artifact_check as checks
        original = checks._write
        def write(*args):
            original(*args)
            if self.f.rt.execution_control.view(self.f.task)['state'] == 'running':
                self.f.rt.execution_control.change(self.f.task, 'pause', request_id='pause-copy')
        with patch('cater.team_artifact_check._write', side_effect=write), patch('cater.team_artifact_check.WorkspaceExecutor.execute') as run:
            result = self.check(expected_statuses=('interrupted',))
        self.assertEqual(result['status'], 'error')
        run.assert_not_called()

    def test_timeout_keeps_existing_pending_contract(self):
        result = self.check(timeout=0.2, args=['-c', 'import time; time.sleep(5)'])
        self.assertEqual(result['outcome'], 'timeout')
        pending = self.f.rt.team._pending_operations(self.f.task)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]['name'], 'workspace_run')
        start = next(e for e in self.f.rt.log.read(task_id=self.f.task) if e.seq == pending[0]['started_seq'])
        self.assertEqual(start.payload['root'], str(self.root))
        self.assertNotIn('execution_contract', start.payload)

    def test_permission_revoked_during_copy_prevents_launch(self):
        from cater import team_artifact_check as checks
        original = checks._write
        def write(*args):
            original(*args)
            config = json.loads(self.config.read_text(encoding='utf-8'))
            config['workspaces'][0].update(mode='edit', commands={})
            self.config.write_text(json.dumps(config), encoding='utf-8')
        with patch('cater.team_artifact_check._write', side_effect=write), patch('cater.team_artifact_check.WorkspaceExecutor.execute') as run:
            result = self.check(expected_statuses=('interrupted',))
        self.assertEqual(result['status'], 'error')
        run.assert_not_called()

    def test_new_customer_input_during_copy_prevents_launch(self):
        from cater import team_artifact_check as checks
        original = checks._write
        def write(*args):
            original(*args)
            self.f.rt.team.receive(self.f.task, '先等等，换一种要求')
        with patch('cater.team_artifact_check._write', side_effect=write), patch('cater.team_artifact_check.WorkspaceExecutor.execute') as run:
            result = self.check(expected_statuses=('interrupted',))
        self.assertEqual(result['status'], 'error')
        run.assert_not_called()

    def test_permission_and_command_alias_remain_required(self):
        result = self.check(command='unlisted')
        self.assertEqual(result['outcome'], 'not_started')
        for mode in ('read_only', 'edit'):
            self.policy(mode)
            workspace = WorkspaceExecutor(self.f.rt.workspaces.config['supervision']['policy'])
            schemas = self.f.rt.team._schemas(self.f.rt.team._role(self.f.manager), workspace)
            self.assertNotIn('workspace_run', [s['name'] for s in schemas])

    def test_revocation_after_owner_acquisition_prevents_initial_capture(self):
        original = self.f.rt.workspaces._owner
        @contextmanager
        def owner(root):
            with original(root) as owned:
                config = json.loads(self.config.read_text(encoding='utf-8'))
                config['workspaces'][0].update(mode='edit', commands={})
                self.config.write_text(json.dumps(config), encoding='utf-8')
                yield owned
        with patch.object(self.f.rt.workspaces, '_owner', owner), patch('cater.team_artifact_check.WorkspaceExecutor.execute') as run:
            result = self.check(expected_statuses=('interrupted',))
        self.assertEqual(result['status'], 'error')
        run.assert_not_called()

    def test_new_publication_during_copy_prevents_launch(self):
        from cater import team_artifact_check as checks
        original = checks._write
        def write(*args):
            original(*args)
            event = next(e for e in self.f.rt.log.read(task_id=self.f.task) if e.seq == self.seq)
            self.f.rt.log.emit(type=event.type, actor=event.actor, task_id=self.f.task, payload=event.payload)
        with patch('cater.team_artifact_check._write', side_effect=write), patch('cater.team_artifact_check.WorkspaceExecutor.execute') as run:
            result = self.check()
        self.assertEqual(result['status'], 'error')
        run.assert_not_called()

    def test_copy_cleanup_failure_keeps_unknown_effect(self):
        import tempfile
        original = tempfile.TemporaryDirectory
        @contextmanager
        def temporary(*args, **kwargs):
            with original(*args, **kwargs) as root:
                yield root
            raise OSError('fixture cleanup failed')
        # Replace the helper's module reference, not tempfile globally used by the runner.
        class CopyTemps:
            TemporaryDirectory = staticmethod(temporary)
        with patch('cater.team_artifact_check.tempfile', CopyTemps):
            result = self.check()
        self.assertEqual(result['outcome'], 'effects_unknown')
        self.assertFalse(result['complete'])
        self.assertEqual(len(self.f.rt.team._pending_operations(self.f.task)), 1)

    def test_other_task_unknown_command_on_same_root_blocks_copy(self):
        other = self.f.rt.create_task('Other task sharing root')
        self.f.rt.team.enable(other)
        self.f.rt.workspaces.bind(other, 'supervision', expected_revision=None)
        source = self.f.rt.team.receive(other, 'Interrupted command')
        from cater.team import HOST
        run = self.f.rt.log.emit(type='team.run_started', actor=HOST, task_id=other, payload={
            'run_id': 'orphan-command', 'role': self.f.manager, 'message_seq': source.seq,
            'revision': 0, 'control_seq': None})
        self.f.rt.log.emit(type='team.tool_started', actor=HOST, task_id=other, payload={
            **run.payload, 'name': 'workspace_run', 'root': str(self.root), 'call_id': 'unknown',
            'arguments': {'command': 'python'}})
        with patch('cater.team_artifact_check.WorkspaceExecutor.execute') as execute:
            result = self.check()
        self.assertEqual(result['status'], 'error')
        execute.assert_not_called()

    def test_customer_can_check_published_copy_after_delivery(self):
        result, = self.f.exchange(self.f.manager, ('finish_work', {
            'summary': 'Fixture delivery', 'verification': 'Not a real model', 'artifact_seqs': [self.seq]}))
        self.assertEqual(result['status'], 'ok', result)
        revision = self.f.rt.team.view(self.f.task)['revision']
        result = self.check()
        self.assertEqual(result['outcome'], 'passed', result)
        self.assertEqual(self.f.rt.team.view(self.f.task)['revision'], revision)
        self.assertEqual(len(self.f.events('team.objective_completed')), 1)

    @unittest.skipIf(os.name == 'nt', 'Windows does not expose POSIX executable bits')
    def test_changed_executable_flag_invalidates_check(self):
        result = self.check(args=['-c', "from pathlib import Path; Path('check.py').chmod(0o755)"])
        self.assertEqual(result['exit_code'], 0)
        self.assertTrue(result['executable_flags_checked'])
        self.assertFalse(result['artifact_check_valid'])


if __name__ == '__main__':
    unittest.main()
