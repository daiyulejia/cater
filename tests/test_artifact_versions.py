"""Native Team dispatch of exact historical bytes through real Python processes.

The only model is the existing deterministic protocol driver. All databases,
project files, policy edits and adversarial publications belong to this fixture.
"""

from contextlib import contextmanager
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
import unittest
from unittest.mock import patch
import zipfile

from cater.artifact_blobs import put_blob, read_blob
from cater.events import Actor
from cater.team import HOST
from cater.workspace_tools import WorkspaceExecutor
from cater.workspaces import WorkspaceService
import test_team_review_cycle as reviews


MIB = 1024 * 1024
MODULE = Path(__file__).resolve().parents[1] / 'cater' / 'responses.py'
COMPARE = '''import hashlib
import importlib.util
import json
from pathlib import Path

inputs = ['{"amount": 3}', '```json\\n{"amount": 3}\\n```',
          '{"amount": 1, "amount": 2}', '{"amount": NaN}', '[]']
observations = []
for directory in ('before', 'after'):
    path = Path(directory) / 'package' / 'responses.py'
    spec = importlib.util.spec_from_file_location(directory + '_responses', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    actual = []
    for text in inputs:
        try:
            actual.append({'value': module.load_response_object(text)})
        except ValueError:
            actual.append({'error': 'ValueError'})
    observations.append({'path': path.as_posix(), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                         'inputs': inputs, 'actual': actual})
    assert (Path(directory) / 'package' / 'data.bin').read_bytes() == b'\\x00\\xff\\r\\n'
    assert (Path(directory) / 'package' / 'empty.txt').read_bytes() == b''
print(json.dumps(observations, ensure_ascii=True))
'''


class ArtifactVersionTests(unittest.TestCase):
    def setUp(self):
        self.f = reviews.TeamReviewCycleTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.root = self.f.bind_workspace()
        self.config = self.f.root / 'supervision-workspaces.json'
        self.policy()
        self.source = MODULE.read_bytes()

    def policy(self, mode='execute'):
        self.config.write_text(json.dumps({'workspaces': [{
            'id': 'supervision', 'title': 'Independent version fixture',
            'root': str(self.root), 'mode': mode, 'timeout': 5,
            'commands': {'python': [sys.executable, '-I', '-B', '-X', 'utf8']}
            if mode == 'execute' else {},
        }]}), encoding='utf-8')
        self.f.rt.workspaces = WorkspaceService(self.f.rt, self.config)
        revision = self.f.rt.workspaces.view(self.f.task)['revision']
        self.f.rt.workspaces.bind(self.f.task, 'supervision', expected_revision=revision)

    def revoke(self):
        config = json.loads(self.config.read_text(encoding='utf-8'))
        config['workspaces'][0].update(mode='edit', commands={})
        self.config.write_text(json.dumps(config), encoding='utf-8')

    def archive(self, files=None, filename='project.zip'):
        files = files if files is not None else {
            'package/responses.py': self.source,
            'package/data.bin': b'\x00\xff\r\n', 'package/empty.txt': b'',
        }
        for name, body in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
        result, = self.f.exchange(self.f.worker, ('publish_workspace_archive', {
            'filename': filename, 'summary': 'Independent fixture complete source', 'paths': list(files),
        }))
        self.assertEqual(result['status'], 'ok', result)
        return result['artifact_seq']

    def pair(self):
        before = self.archive()
        after = self.archive({
            'package/responses.py': self.source + b'\n# Independent fixture second publication.\n',
            'package/data.bin': b'\x00\xff\r\n', 'package/empty.txt': b'',
        })
        return before, after

    def selection(self, *seqs):
        return [{'artifact_seq': seq, 'directory': directory}
                for seq, directory in zip(seqs, ('before', 'after'))]

    def event(self, seq):
        return next(e for e in self.f.rt.log.read() if e.seq == seq)

    def altered(self, seq, *, actor=None, **changes):
        original = self.event(seq)
        return self.f.rt.log.emit(type='deliverable.published', task_id=self.f.task,
            actor=actor if actor is not None else original.actor,
            payload={**copy.deepcopy(original.payload), **changes}).seq

    def check(self, versions, *, expected_statuses=('waiting', 'completed'), **changes):
        args = {'command': 'python', 'args': ['-'], 'stdin': COMPARE,
                'cwd': '.', 'artifact_versions': versions, **changes}
        result, = self.f.exchange(self.f.manager, ('workspace_run', args),
                                  expected_statuses=expected_statuses)
        return result

    def reject(self, versions, *, before_blob=False, **changes):
        with patch('cater.team_artifact_check.WorkspaceExecutor.execute') as execute, \
                patch('cater.team_artifact_check._write') as write, \
                patch('cater.archive_members.read_blob', wraps=read_blob) as blob:
            result = self.check(versions, **changes)
        self.assertEqual((result['status'], result['outcome']), ('error', 'not_started'), result)
        self.assertEqual(result['code'], 'invalid_artifact_check', result)
        self.assertTrue(result['complete'], result)
        execute.assert_not_called()
        write.assert_not_called()
        if before_blob:
            blob.assert_not_called()
        self.assertEqual(self.f.rt.team._pending_operations(self.f.task), [])
        return result

    def assert_receipt(self, result, versions, *, latest, current):
        self.assertEqual(result['outcome'], 'passed', result)
        self.assertEqual(result['exit_code'], 0, result)
        self.assertEqual(result['artifact_selection'], 'version_directories')
        self.assertEqual(result['environment'], 'temporary_artifact_copy_not_sandbox')
        self.assertTrue(result['scope_current'], result)
        self.assertTrue(result['artifacts_unchanged'], result)
        self.assertTrue(result['artifact_check_valid'], result)
        self.assertEqual(result['selected_versions_current'], current)
        actual = {a['seq']: a for a in result['checked_artifacts']}
        self.assertEqual(set(actual), {a['artifact_seq'] for a in versions})
        expected_files = []
        for selection in versions:
            seq, directory = selection['artifact_seq'], selection['directory']
            event = self.event(seq)
            filename = event.payload['filename']
            self.assertEqual(actual[seq], {
                'seq': seq, 'filename': filename, 'content_sha256': event.payload['content_sha256'],
                'directory': directory, 'latest_seq_at_check': latest[filename],
                'is_latest_at_check': latest[filename] == seq,
            })
            entries = event.payload.get('archive', {}).get('files', [{
                'path': filename, 'bytes': len(event.payload.get('content', '').encode('utf-8')),
                'sha256': event.payload['content_sha256'], 'executable': False,
            }])
            expected_files.extend({
                'artifact_seq': seq, 'source_path': entry['path'],
                'path': directory + '/' + entry['path'],
                **{key: entry[key] for key in ('bytes', 'sha256', 'executable')},
            } for entry in entries)
        order = lambda a: (a['artifact_seq'], a['path'])
        self.assertEqual(sorted(result['checked_files'], key=order), sorted(expected_files, key=order))
        self.assertEqual(self.f.rt.team_reviews.view(self.f.task), [])
        self.assertEqual(self.f.events('team.review_recorded'), [])
        self.assertEqual(self.f.rt.team._pending_operations(self.f.task), [])
        self.assertTrue(self.f.rt.verify_chain().ok)

    def test_real_stdin_loads_historical_and_latest_same_name_modules(self):
        before, after = self.pair()
        versions = self.selection(before, after)
        # The live project is deliberately unusable; only published copies work.
        (self.root / 'package/responses.py').write_bytes(b'raise RuntimeError("unpublished")\n')
        revision = self.f.rt.team.view(self.f.task)['revision']
        roots = []
        original = WorkspaceExecutor.execute

        def execute(executor, name, arguments):
            roots.append(executor.policy.root)
            self.assertNotEqual(executor.policy.root, self.root)
            self.assertEqual(arguments['stdin'], COMPARE)
            self.assertNotIn('artifact_versions', arguments)
            return original(executor, name, arguments)

        with patch('cater.team_artifact_check.WorkspaceExecutor.execute', new=execute):
            result = self.check(versions)
        self.assert_receipt(result, versions, latest={'project.zip': after}, current=False)
        observed = json.loads(result['stdout']['excerpt'])
        self.assertEqual([a['path'] for a in observed],
                         ['before/package/responses.py', 'after/package/responses.py'])
        self.assertEqual(observed[0]['inputs'], observed[1]['inputs'])
        expected = [{'value': {'amount': 3}}, {'value': {'amount': 3}},
                    {'error': 'ValueError'}, {'error': 'ValueError'}, {'error': 'ValueError'}]
        self.assertEqual([a['actual'] for a in observed], [expected, expected])
        checked = {a['path']: a['sha256'] for a in result['checked_files']}
        self.assertEqual([a['sha256'] for a in observed], [checked[a['path']] for a in observed])
        self.assertNotEqual(observed[0]['sha256'], observed[1]['sha256'])
        self.assertEqual(result['argv'], [sys.executable, '-I', '-B', '-X', 'utf8', '-'])
        self.assertEqual(result['stdin']['sha256'], hashlib.sha256(COMPARE.encode()).hexdigest())
        self.assertEqual(result['stdin']['bytes'], len(COMPARE.encode()))
        self.assertEqual(len(roots), 1)
        self.assertFalse(roots[0].exists())
        self.assertEqual(self.f.rt.team.view(self.f.task)['revision'], revision)
        self.assertEqual(self.f.events('team.objective_completed'), [])

    def test_text_history_and_copy_relative_cwd(self):
        old = self.f.publish(self.source.decode('utf-8'), filename='responses.py')
        new = self.f.publish(self.source.decode('utf-8') + '\n# Second text publication.\n',
                             filename='responses.py')
        versions = [{'artifact_seq': old, 'directory': 'before'}]
        script = '''import importlib.util
spec = importlib.util.spec_from_file_location('published', 'responses.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
assert module.load_response_object('{"real": true}') == {'real': True}
print('loaded published text')
'''
        result = self.check(versions, cwd='before', stdin=script)
        self.assert_receipt(result, versions, latest={'responses.py': new}, current=False)
        self.assertEqual(result['cwd'], 'before')
        self.assertEqual(result['stdout']['excerpt'].strip(), 'loaded published text')

    def test_latest_versions_current_is_separate_from_execution_result(self):
        seq = self.archive()
        versions = [{'artifact_seq': seq, 'directory': 'latest'}]
        result = self.check(versions, args=['-c', 'raise SystemExit(7)'])
        self.assertEqual((result['outcome'], result['exit_code']), ('failed', 7), result)
        self.assertTrue(result['selected_versions_current'])
        self.assertTrue(result['scope_current'])
        self.assertTrue(result['artifact_check_valid'])
        self.assertEqual(self.f.rt.team_reviews.view(self.f.task), [])

    def test_schema_is_team_only_and_keeps_execute_permission(self):
        workspace = WorkspaceExecutor(self.f.rt.workspaces.config['supervision']['policy'])
        base = next(s for s in workspace.schemas() if s['name'] == 'workspace_run')
        self.assertNotIn('artifact_versions', base['input_schema']['properties'])
        schemas = self.f.rt.team._schemas(self.f.rt.team._role(self.f.manager), workspace)
        schema = next(s for s in schemas if s['name'] == 'workspace_run')['input_schema']
        version = schema['properties']['artifact_versions']
        self.assertEqual((version['type'], version['minItems'], version['maxItems']), ('array', 1, 128))
        item = version['items']
        self.assertEqual(set(item['required']), {'artifact_seq', 'directory'})
        self.assertEqual(item['properties']['artifact_seq']['type'], 'integer')
        self.assertIn('artifact_seqs', schema['properties'])
        for mode in ('read_only', 'edit'):
            with self.subTest(mode=mode):
                self.policy(mode)
                workspace = WorkspaceExecutor(self.f.rt.workspaces.config['supervision']['policy'])
                schemas = self.f.rt.team._schemas(self.f.rt.team._role(self.f.manager), workspace)
                self.assertNotIn('workspace_run', [s['name'] for s in schemas])

    def test_selector_types_duplicates_and_mutual_exclusion_reject_before_blob(self):
        seq = self.archive()
        item = {'artifact_seq': seq, 'directory': 'before'}
        invalid = [None, {}, 'before', [], [None], [seq], [True],
                   [{'directory': 'before'}], [{'artifact_seq': seq}],
                   [{**item, 'extra': True}], [item, {**item, 'directory': 'after'}],
                   [item] * 129]
        invalid.extend([{**item, 'artifact_seq': value}]
                       for value in (True, False, 0, -1, 1.0, str(seq), None, 999999))
        for versions in invalid:
            with self.subTest(versions=repr(versions)[:160]):
                self.reject(versions, before_blob=True)
        self.reject([item], artifact_seqs=[seq], before_blob=True)
        self.reject([item], artifact_seqs=[], before_blob=True)

    def test_mounts_are_single_portable_unprotected_components(self):
        seq = self.archive()
        for directory in ('', '.', '..', '/', '/before', '../before', 'before/after',
                          'before\\after', 'C:', 'C:/before', '//host/share', 'before/',
                          '.git', '.GIT', '\uff0egit', '.env', 'secrets', 'credentials.json',
                          'CON', 'nul.txt', 'before.', 'before ', 'bad:name', 'bad\x00name',
                          '\uff0e\uff0e', True, None, 3):
            with self.subTest(directory=directory):
                self.reject([{'artifact_seq': seq, 'directory': directory}], before_blob=True)

    def test_casefold_and_nfkc_directory_aliases_reject_before_any_blob(self):
        before, after = self.pair()
        for a, b in (('same', 'same'), ('before', 'BEFORE'), ('K', '\u212a'),
                     ('cafe\u0301', 'caf\u00e9'), ('before', '\uff42efore')):
            with self.subTest(directories=(a, b)):
                self.reject([{'artifact_seq': before, 'directory': a},
                             {'artifact_seq': after, 'directory': b}], before_blob=True)

    def test_foreign_and_unregistered_or_system_publishers_reject_before_blob(self):
        local = self.archive()
        other = self.f.rt.create_task('Independent fixture foreign group')
        self.f.rt.team.enable(other)
        foreign = self.f.publish('private foreign body', task_id=other)
        forged = [self.altered(local, actor=actor) for actor in
                  (Actor.system('team'), Actor.human(), Actor.agent('not-a-member', 'worker'))]
        for seq in [foreign, *forged]:
            with self.subTest(seq=seq):
                self.reject([{'artifact_seq': local, 'directory': 'before'},
                             {'artifact_seq': seq, 'directory': 'after'}], before_blob=True)

    def test_all_candidates_preflight_counts_and_uncompressed_budgets(self):
        original = self.archive()

        def manifest(count, size):
            return {'version': 1, 'files': [
                {'path': f'files/{i:03}.bin', 'bytes': size, 'sha256': '0' * 64, 'executable': False}
                for i in range(count)], 'total_bytes': count * size}

        cases = [
            (manifest(129, 0), None),
            (manifest(1, 16 * MIB + 1), None),
            (manifest(3, 12 * MIB), None),
            (manifest(65, 0), manifest(64, 0)),
            (manifest(1, 16 * MIB), manifest(2, 9 * MIB)),
        ]
        for first, second in cases:
            with self.subTest(counts=(len(first['files']), len(second['files']) if second else 0)):
                if second is None:
                    a = original
                    b = self.altered(original, archive=first)
                else:
                    a = self.altered(original, archive=first)
                    b = self.altered(original, archive=second)
                self.reject(self.selection(a, b), before_blob=True)

    def test_protected_alias_and_parent_child_member_paths_preflight_all_sources(self):
        original = self.archive()
        for paths in (['../escape'], ['/absolute'], ['.env'], ['.git/config'],
                      ['\uff0egit/config'], ['A.txt', 'a.txt'], ['K.txt', '\u212a.txt'],
                      ['file', 'file/child'], ['pkg/A', 'PKG/B']):
            with self.subTest(paths=paths):
                manifest = {'version': 1, 'files': [
                    {'path': path, 'bytes': 0, 'sha256': hashlib.sha256(b'').hexdigest(),
                     'executable': False} for path in sorted(paths)], 'total_bytes': 0}
                bad = self.altered(original, archive=manifest)
                self.reject(self.selection(original, bad), before_blob=True)

    def test_exactly_128_members_across_versions_are_accepted(self):
        files = {f'files/{i:03}.bin': b'' for i in range(64)}
        before = self.archive(files)
        after = self.archive(files)
        versions = self.selection(before, after)
        result = self.check(versions, args=['-c',
            "from pathlib import Path; files = [p for p in Path('.').rglob('*') if p.is_file()]; "
            "assert len(files) == 128; assert all(p.read_bytes() == b'' for p in files); print(len(files))"])
        self.assert_receipt(result, versions, latest={'project.zip': after}, current=False)
        self.assertEqual(len(result['checked_files']), 128)
        self.assertEqual(result['stdout']['excerpt'].strip(), '128')

    def test_text_hash_and_archive_hash_or_length_never_fall_back(self):
        archive = self.archive()
        text = self.f.publish(self.source.decode('utf-8'), filename='responses.py')
        bad_text = self.altered(text, content='changed without matching digest')
        event = self.event(archive)
        invalid = [bad_text, self.altered(archive, content_sha256='0' * 64),
                   self.altered(archive, content_bytes=event.payload['content_bytes'] + 1)]
        for seq in invalid:
            with self.subTest(seq=seq):
                self.reject([{'artifact_seq': seq, 'directory': 'before'}])

    def test_corrupt_stored_blob_digest_is_detected_using_disposable_database(self):
        seq = self.archive()
        p = self.event(seq).payload
        # Insert a distinct damaged row in this disposable DB without removing any guards.
        digest = hashlib.sha256(b'not these bytes').hexdigest()
        self.f.rt.log.db.execute('INSERT INTO artifact_blobs VALUES (?, ?)',
                                 (digest, b'x' * p['content_bytes']))
        bad = self.altered(seq, content_sha256=digest)
        self.reject([{'artifact_seq': bad, 'directory': 'before'}])

    def test_zip_member_crc_digest_and_permission_metadata_stay_enforced(self):
        seq = self.archive()
        event = self.event(seq)
        blob = read_blob(self.f.rt.log, event.payload['content_sha256'], event.payload['content_bytes'])
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            info = archive.infolist()[0]
        damaged = bytearray(blob)
        damaged[info.header_offset + 30 + len(info.filename.encode()) + len(info.extra)] ^= 1
        crc_bad = self.altered(seq, content_sha256=put_blob(self.f.rt.log, bytes(damaged)))
        manifest = copy.deepcopy(event.payload['archive'])
        manifest['files'][0]['sha256'] = '0' * 64
        sha_bad = self.altered(seq, archive=manifest)
        manifest = copy.deepcopy(event.payload['archive'])
        manifest['files'][0]['executable'] = not manifest['files'][0]['executable']
        mode_bad = self.altered(seq, archive=manifest)
        linked = bytearray(blob)
        central = blob.index(b'PK\x01\x02')
        import struct
        struct.pack_into('<I', linked, central + 38, (stat.S_IFLNK | 0o777) << 16)
        link_bad = self.altered(seq, content_sha256=put_blob(self.f.rt.log, bytes(linked)))
        for bad in (crc_bad, sha_bad, mode_bad, link_bad):
            with self.subTest(seq=bad):
                self.reject([{'artifact_seq': bad, 'directory': 'before'}])

    def test_copy_mutation_invalidates_receipt_and_preserves_publication_and_project(self):
        before, after = self.pair()
        p = self.event(before).payload
        original_blob = read_blob(self.f.rt.log, p['content_sha256'], p['content_bytes'])
        project = (self.root / 'package/responses.py').read_bytes()
        result = self.check(self.selection(before, after), args=['-c',
            "from pathlib import Path; Path('before/package/responses.py').write_bytes(b'changed')"])
        self.assertEqual(result['exit_code'], 0, result)
        self.assertFalse(result['artifacts_unchanged'])
        self.assertFalse(result['artifact_check_valid'])
        self.assertTrue(result['scope_current'])
        self.assertEqual(read_blob(self.f.rt.log, p['content_sha256'], p['content_bytes']), original_blob)
        self.assertEqual((self.root / 'package/responses.py').read_bytes(), project)

    def test_new_same_name_publication_at_receipt_end_keeps_historical_evidence(self):
        seq = self.archive()
        versions = [{'artifact_seq': seq, 'directory': 'before'}]
        original = WorkspaceExecutor.execute
        published = []

        def execute(executor, name, arguments):
            result = original(executor, name, arguments)
            published.append(self.altered(seq))
            return result

        with patch('cater.team_artifact_check.WorkspaceExecutor.execute', new=execute):
            result = self.check(versions, args=['-c', "print('original historical bytes checked')"])
        self.assertEqual(len(published), 1)
        self.assert_receipt(result, versions, latest={'project.zip': published[0]}, current=False)
        self.assertGreater(published[0], seq)

    def test_latest_diagnostic_read_failure_preserves_real_process_evidence(self):
        from cater import artifact_versions
        before, after = self.pair()
        versions = self.selection(before, after)
        original = artifact_versions.checked_artifacts
        for error in (OSError, sqlite3.OperationalError, ValueError):
            for failed in (False, True):
                with self.subTest(error=error.__name__, failed=failed):
                    def diagnose(team, task_id, sources):
                        # Limit the failure to the final diagnostic, after scope/byte checks.
                        with patch.object(team.rt.log, 'read', side_effect=error('fixture diagnostic read failed')) as read:
                            rows = original(team, task_id, sources)
                        read.assert_called_once_with(task_id=task_id)
                        return rows

                    changes = {'args': ['-c', "print('actual failed process'); raise SystemExit(9)"]} if failed else {}
                    with patch('cater.artifact_versions.checked_artifacts', new=diagnose):
                        result = self.check(versions, **changes)
                    self.assertEqual((result['outcome'], result['exit_code']),
                                     ('failed', 9) if failed else ('passed', 0), result)
                    self.assertEqual(result['status'], 'error' if failed else 'ok')
                    self.assertEqual(result['artifact_selection'], 'version_directories')
                    self.assertTrue(result['complete'])
                    self.assertTrue(result['scope_current'])
                    self.assertTrue(result['artifacts_unchanged'])
                    self.assertTrue(result['artifact_check_valid'])
                    self.assertIsNone(result['selected_versions_current'])
                    expected = [{
                        'seq': row['artifact_seq'], 'directory': row['directory'],
                        'filename': self.event(row['artifact_seq']).payload['filename'],
                        'content_sha256': self.event(row['artifact_seq']).payload['content_sha256'],
                        'latest_seq_at_check': None, 'is_latest_at_check': None,
                    } for row in versions]
                    self.assertEqual(result['checked_artifacts'], expected)
                    if failed:
                        self.assertEqual(result['stdout']['excerpt'].strip(), 'actual failed process')
                    else:
                        observed = json.loads(result['stdout']['excerpt'])
                        checked = {f['path']: f['sha256'] for f in result['checked_files']}
                        self.assertEqual(len(observed), 2)
                        self.assertEqual(observed[0]['actual'], observed[1]['actual'])
                        for row in observed:
                            self.assertEqual(row['sha256'], checked[row['path']])
                    self.assertTrue(all(f['path'].endswith('/' + f['source_path'])
                                        for f in result['checked_files']))
                    self.assertEqual(self.f.rt.team._pending_operations(self.f.task), [])
        self.assertEqual(self.f.rt.team_reviews.view(self.f.task), [])
        self.assertTrue(self.f.rt.verify_chain().ok)

    def test_new_same_name_publication_during_copy_keeps_selected_bytes_executable(self):
        from cater import team_artifact_check as checks
        seq = self.archive()
        original = checks._write
        published = []

        def write(*args):
            original(*args)
            if not published:
                published.append(self.altered(seq))

        with patch('cater.team_artifact_check._write', side_effect=write):
            result = self.check([{'artifact_seq': seq, 'directory': 'before'}],
                                args=['-c', "print('selected immutable version')"])
        self.assertEqual(result['outcome'], 'passed', result)
        self.assertTrue(result['scope_current'])
        self.assertTrue(result['artifact_check_valid'])
        self.assertFalse(result['selected_versions_current'])

    def test_copy_scope_changes_prevent_process_launch(self):
        from cater import team_artifact_check as checks
        # Each subcase owns its own runtime; a paused case must not poison the next one.
        for kind in ('permission', 'customer', 'pause'):
            with self.subTest(kind=kind):
                case = ArtifactVersionTests()
                case.setUp()
                try:
                    seq = case.archive()
                    original = checks._write
                    changed = []
                    roots = []

                    def write(path, body):
                        original(path, body)
                        roots.append(path)
                        if not changed:
                            changed.append(True)
                            if kind == 'permission':
                                case.revoke()
                            elif kind == 'customer':
                                case.f.rt.team.receive(case.f.task, 'Changed fixture customer direction')
                            else:
                                case.f.rt.execution_control.change(case.f.task, 'pause', request_id='version-copy-pause')

                    with patch('cater.team_artifact_check._write', side_effect=write), \
                            patch('cater.team_artifact_check.WorkspaceExecutor.execute') as execute:
                        result = case.check([{'artifact_seq': seq, 'directory': 'before'}],
                                            expected_statuses=('interrupted',))
                    self.assertEqual(result['status'], 'error', result)
                    execute.assert_not_called()
                    self.assertTrue(changed)
                    self.assertTrue(all(not path.exists() for path in roots))
                finally:
                    case.doCleanups()

    def test_scope_changes_during_execution_preserve_output_but_invalidate_receipt(self):
        for kind in ('permission', 'customer'):
            with self.subTest(kind=kind):
                case = ArtifactVersionTests()
                case.setUp()
                try:
                    seq = case.archive()
                    original = WorkspaceExecutor.execute

                    def execute(executor, name, arguments):
                        result = original(executor, name, arguments)
                        if kind == 'permission':
                            case.revoke()
                        else:
                            case.f.rt.team.receive(case.f.task, 'Changed fixture execution direction')
                        return result

                    with patch('cater.team_artifact_check.WorkspaceExecutor.execute', new=execute):
                        result = case.check([{'artifact_seq': seq, 'directory': 'before'}],
                            args=['-c', "print('real process finished')"], expected_statuses=('interrupted',))
                    self.assertEqual(result['outcome'], 'passed', result)
                    self.assertIn('real process finished', result['stdout']['excerpt'])
                    self.assertFalse(result['scope_current'])
                    self.assertFalse(result['artifact_check_valid'])
                    self.assertTrue(result['artifacts_unchanged'])
                    self.assertTrue(result['selected_versions_current'])
                finally:
                    case.doCleanups()

    def test_alias_and_bad_cwd_never_launch_or_fall_back_to_project(self):
        seq = self.archive()
        versions = [{'artifact_seq': seq, 'directory': 'before'}]
        for changes in ({'command': 'unlisted'}, {'cwd': 'missing'}, {'cwd': '..'},
                        {'cwd': str(self.root)}, {'cwd': '/before'}):
            with self.subTest(changes=changes), patch('cater.workspace_tools.subprocess.Popen') as process:
                result = self.check(versions, **changes)
            self.assertEqual(result['outcome'], 'not_started', result)
            process.assert_not_called()
            self.assertEqual(self.f.rt.team._pending_operations(self.f.task), [])

    def test_timeout_blocks_later_command_and_never_replays(self):
        seq = self.archive()
        versions = [{'artifact_seq': seq, 'directory': 'before'}]
        result = self.check(versions, timeout=0.25, args=['-c', 'import time; time.sleep(10)'])
        self.assertEqual(result['outcome'], 'timeout', result)
        pending = self.f.rt.team._pending_operations(self.f.task)
        self.assertEqual(len(pending), 1)
        start = self.event(pending[0]['started_seq'])
        self.assertEqual(start.payload['root'], str(self.root))
        self.assertNotIn('execution_contract', start.payload)
        with patch('cater.team_artifact_check._write') as write, \
                patch('cater.team_artifact_check.WorkspaceExecutor.execute') as execute:
            blocked = self.check(versions)
        self.assertEqual(blocked['status'], 'error')
        write.assert_not_called()
        execute.assert_not_called()
        self.assertEqual(self.f.rt.team._pending_operations(self.f.task), pending)

    def test_same_root_unknown_operation_and_live_owner_block_before_copy(self):
        seq = self.archive()
        versions = [{'artifact_seq': seq, 'directory': 'before'}]

        @contextmanager
        def busy(root):
            yield False

        with patch.object(self.f.rt.workspaces, '_owner', busy), \
                patch('cater.team_artifact_check._write') as write, \
                patch('cater.team_artifact_check.WorkspaceExecutor.execute') as execute:
            result = self.check(versions)
        self.assertEqual(result['status'], 'error')
        write.assert_not_called()
        execute.assert_not_called()
        other = self.f.rt.create_task('Independent fixture shares root')
        self.f.rt.team.enable(other)
        self.f.rt.workspaces.bind(other, 'supervision', expected_revision=None)
        source = self.f.rt.team.receive(other, 'Unsettled fixture command')
        run = self.f.rt.log.emit(type='team.run_started', actor=HOST, task_id=other, payload={
            'run_id': 'version-orphan', 'role': self.f.manager, 'message_seq': source.seq,
            'revision': 0, 'control_seq': None})
        self.f.rt.log.emit(type='team.tool_started', actor=HOST, task_id=other, payload={
            **run.payload, 'name': 'workspace_run', 'root': str(self.root), 'call_id': 'unknown',
            'arguments': {'command': 'python'}})
        with patch('cater.team_artifact_check._write') as write, \
                patch('cater.team_artifact_check.WorkspaceExecutor.execute') as execute:
            result = self.check(versions)
        self.assertEqual(result['status'], 'error')
        write.assert_not_called()
        execute.assert_not_called()

    def test_cleanup_uncertainty_blocks_replay(self):
        import tempfile
        seq = self.archive()
        original = tempfile.TemporaryDirectory

        @contextmanager
        def temporary(*args, **kwargs):
            with original(*args, **kwargs) as root:
                yield root
            raise OSError('independent fixture cleanup uncertain')

        class CopyTemps:
            TemporaryDirectory = staticmethod(temporary)

        versions = [{'artifact_seq': seq, 'directory': 'before'}]
        with patch('cater.team_artifact_check.tempfile', CopyTemps):
            result = self.check(versions, args=['-c', 'pass'])
        self.assertEqual(result['outcome'], 'effects_unknown', result)
        self.assertFalse(result['complete'])
        self.assertEqual(len(self.f.rt.team._pending_operations(self.f.task)), 1)
        with patch('cater.team_artifact_check.WorkspaceExecutor.execute') as execute:
            result = self.check(versions)
        self.assertEqual(result['status'], 'error')
        execute.assert_not_called()

    def test_postdelivery_customer_checks_same_name_history_without_new_objective(self):
        before, after = self.pair()
        result, = self.f.exchange(self.f.manager, ('finish_work', {
            'summary': 'Independent fixture delivery',
            'verification': 'Deterministic fixture; no real model or independent approval',
            'artifact_seqs': [after],
        }))
        self.assertEqual(result['status'], 'ok', result)
        revision = self.f.rt.team.view(self.f.task)['revision']
        versions = self.selection(before, after)
        result = self.check(versions)
        self.assert_receipt(result, versions, latest={'project.zip': after}, current=False)
        self.assertEqual(self.f.rt.team.view(self.f.task)['revision'], revision)
        self.assertEqual(len(self.f.events('team.objective_completed')), 1)

    def test_old_selector_and_browser_still_reject_superseded_version(self):
        before, after = self.pair()
        with patch('cater.team_artifact_check.WorkspaceExecutor.execute') as execute:
            result, = self.f.exchange(self.f.manager, ('workspace_run', {
                'command': 'python', 'args': ['-c', 'pass'], 'artifact_seqs': [before]}))
        self.assertEqual(result['outcome'], 'not_started', result)
        execute.assert_not_called()
        with self.assertRaises(ValueError):
            self.f.rt.team.browser._inputs(self.f.task, {'artifact_seqs': [before]})
        self.f.rt.browser_checks.command = [sys.executable, 'never-run-fixture-browser.py']
        with patch('cater.team_browser.run_check') as browser:
            result, = self.f.exchange(self.f.manager, ('browser_check', {
                'artifact_seqs': [before], 'plan': {'entrypoint': 'package/responses.py', 'steps': []}}))
        self.assertEqual(result['code'], 'invalid_browser_check', result)
        browser.assert_not_called()
        normal, = self.f.exchange(self.f.manager, ('workspace_run', {
            'command': 'python', 'args': ['-c', "print('ordinary project command')"]}))
        self.assertEqual(normal['outcome'], 'passed', normal)
        self.assertNotIn('checked_artifacts', normal)
        latest, = self.f.exchange(self.f.manager, ('workspace_run', {
            'command': 'python', 'args': ['-c', 'pass'], 'artifact_seqs': [after]}))
        self.assertEqual(latest['outcome'], 'passed', latest)
        self.assertEqual({e['path'] for e in latest['checked_files']},
                         {'package/responses.py', 'package/data.bin', 'package/empty.txt'})

    @unittest.skipIf(os.name == 'nt', 'Windows does not expose POSIX executable bits')
    def test_changed_copy_executable_bit_invalidates_version_receipt(self):
        seq = self.archive()
        result = self.check([{'artifact_seq': seq, 'directory': 'before'}],
            args=['-c', "from pathlib import Path; Path('before/package/responses.py').chmod(0o755)"])
        self.assertEqual(result['exit_code'], 0, result)
        self.assertTrue(result['executable_flags_checked'])
        self.assertFalse(result['artifacts_unchanged'])
        self.assertFalse(result['artifact_check_valid'])


if __name__ == '__main__':
    unittest.main()
