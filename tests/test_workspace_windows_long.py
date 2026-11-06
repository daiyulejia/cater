"""Real Windows long paths, aliases and workspace security boundaries."""
import hashlib
import io
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import zipfile

from cater.workspace_tools import WorkspaceExecutor, WorkspacePolicy, _io_path, _visible_path, _write_lock
from cater.workspace_archive import create_snapshot
from cater.activity_owner import activity_owner
from cater.workspaces import HOST, WorkspaceService
from cater.events import Actor
from cater.runtime import Runtime
import test_team_review_cycle as reviews


def units(path):
    return len(str(path).encode('utf-16-le')) // 2


def acquire_write_lock(root, entered):
    with _write_lock(Path(root)):
        entered.set()


def try_owner_lock(root, result):
    with WorkspaceService._owner(root) as owned:
        result.put(owned)


@unittest.skipUnless(os.name == 'nt', 'Windows path namespace')
class WindowsWorkspaceLongPaths(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='cater-win-workspace-',
                                          dir=str(_io_path(Path(tempfile.gettempdir()), force=True)))
        self.addCleanup(tmp.cleanup)
        self.root = _visible_path(tmp.name)
        self.work = WorkspaceExecutor(WorkspacePolicy.from_dict({'root': str(self.root), 'mode': 'edit'}))

    def assert_ok(self, result):
        self.assertEqual(result.get('status'), 'ok', result)
        self.assertNotIn('\\\\?\\', str(result))
        return result

    def deep_case(self, parts):
        rel = '/'.join([*parts, 'result.txt'])
        full = self.root.joinpath(*parts, 'result.txt')
        created = self.assert_ok(self.work.execute('workspace_write',
            {'path': rel, 'content': 'fixture needle\n', 'expected_sha256': None}))
        self.assertTrue(created['created'])
        read = self.assert_ok(self.work.execute('workspace_read', {'path': rel}))
        self.assertEqual(read['content'], 'fixture needle\n')
        stale = self.work.execute('workspace_write', {'path': rel, 'content': 'bad', 'expected_sha256': '0' * 64})
        self.assertEqual((stale['status'], stale['error']['code']), ('error', 'conflict'))
        self.assert_ok(self.work.execute('workspace_write',
            {'path': rel, 'content': 'updated needle\n', 'expected_sha256': read['sha256']}))
        listing = self.assert_ok(self.work.execute('workspace_list', {'path': '/'.join(parts)}))
        self.assertFalse(listing['truncated'])
        self.assertIn(rel, [item['path'] for item in listing['entries']])
        search = self.assert_ok(self.work.execute('workspace_search', {'path': '/'.join(parts), 'query': 'needle'}))
        self.assertIn(rel, [item['path'] for item in search['matches']])
        body, manifest = create_snapshot(self.work, [rel])
        self.assertEqual(manifest['files'][0]['path'], rel)
        with zipfile.ZipFile(io.BytesIO(body)) as z:
            self.assertEqual(z.read(rel), b'updated needle\n')
        self.assertEqual(self.assert_ok(self.work.execute('workspace_read', {'path': rel}))['content'], 'updated needle\n')
        return full

    def test_ascii_deep_workspace_io_and_readable_root(self):
        parts = [('part%d-' % i) + 'a' * 38 for i in range(6)]
        full = self.deep_case(parts)
        self.assertGreater(units(full), 260)
        parent = full.parent
        normal = WorkspacePolicy.from_dict({'root': str(parent), 'mode': 'edit'})
        extended = WorkspacePolicy.from_dict({'root': str(_io_path(parent, force=True)), 'mode': 'edit'})
        self.assertEqual(normal.root, extended.root)
        self.assertFalse(str(extended.root).startswith('\\\\?\\'))

    def test_non_bmp_utf16_threshold_io_list_and_locks(self):
        parts = [('n%d-' % i) + '🐱' * 25 for i in range(4)]
        full = self.root.joinpath(*parts, 'result.txt')
        self.assertLess(len(str(full)), 248)
        self.assertGreater(units(full), 260)
        self.deep_case(parts)
        root = full.parent
        extended = str(_io_path(root, force=True))
        normal_policy = WorkspacePolicy.from_dict({'root': str(root), 'mode': 'edit'})
        extended_policy = WorkspacePolicy.from_dict({'root': extended, 'mode': 'edit'})
        self.assertEqual(normal_policy.root, extended_policy.root)
        self.assert_lock_contention(normal_policy.root, extended_policy.root)

    def test_root_with_emoji_joiner_is_usable(self):
        root = self.root / '👩‍💻'
        root.mkdir()
        normal = WorkspacePolicy.from_dict({'root': str(root), 'mode': 'edit'})
        extended = WorkspacePolicy.from_dict({'root': str(_io_path(root, force=True)), 'mode': 'edit'})
        self.assertEqual(normal.root, extended.root)
        self.assertFalse(str(normal.root).startswith('\\\\?\\'))
        work = WorkspaceExecutor(normal)
        self.assertEqual(work.execute('workspace_write', {'path': 'note.txt', 'content': 'fixture',
                                                         'expected_sha256': None})['status'], 'ok')
        self.assertEqual(work.execute('workspace_read', {'path': 'note.txt'})['content'], 'fixture')
        self.assert_lock_contention(normal.root, extended.root)

    def test_root_with_windows_legal_c1_control_is_usable(self):
        root = self.root / 'legal\u0085name'
        root.mkdir()
        work = WorkspaceExecutor(WorkspacePolicy.from_dict({'root': str(root), 'mode': 'edit'}))
        self.assertEqual(work.execute('workspace_write', {'path': 'note.txt', 'content': 'ok',
                                                           'expected_sha256': None})['status'], 'ok')
        self.assertEqual(work.execute('workspace_read', {'path': 'note.txt'})['content'], 'ok')
        self.assert_lock_contention(root, _io_path(root, force=True))
        with self.assertRaises(ValueError):
            WorkspacePolicy.from_dict({'root': str(root / 'invalid\x1fname'), 'mode': 'edit'})

    def test_root_with_legal_fullwidth_names_is_usable(self):
        for name in ('合法／目录', 'ｃｏｎ'):
            with self.subTest(name=name):
                root = self.root / name
                root.mkdir()
                policy = WorkspacePolicy.from_dict({'root': str(root), 'mode': 'edit'})
                work = WorkspaceExecutor(policy)
                self.assertEqual(work.execute('workspace_write', {'path': 'note.txt', 'content': name,
                    'expected_sha256': None})['status'], 'ok')
                self.assertEqual(work.execute('workspace_read', {'path': 'note.txt'})['content'], name)
                self.assert_lock_contention(root, _io_path(root, force=True))
        for name in ('CON', 'NUL.txt', 'COM¹', 'LPT².txt', 'bad:name', 'tail.'):
            with self.subTest(rejected=name), self.assertRaises(ValueError):
                WorkspacePolicy.from_dict({'root': str(self.root / name), 'mode': 'edit'})
        for suffix in ('\\.\\', '\\..\\', '/child'):
            with self.subTest(namespace_suffix=suffix), self.assertRaises(ValueError):
                WorkspacePolicy.from_dict({'root': str(_io_path(self.root, force=True)) + suffix, 'mode': 'edit'})

    def assert_lock_contention(self, first, second):
        with WorkspaceService._owner(str(first)) as owned:
            self.assertTrue(owned)
            with WorkspaceService._owner(str(second)) as competing:
                self.assertFalse(competing)
        ready, acquired = threading.Event(), threading.Event()
        errors = []
        def competitor():
            try:
                ready.set()
                with _write_lock(second): acquired.set()
            except BaseException as exc: errors.append(type(exc).__name__)
        with _write_lock(first):
            thread = threading.Thread(target=competitor); thread.start()
            self.assertTrue(ready.wait(2)); self.assertFalse(acquired.wait(.2))
        thread.join(4)
        self.assertFalse(thread.is_alive()); self.assertFalse(errors); self.assertTrue(acquired.is_set())

    def test_real_drive_and_unc_aliases_share_both_locks(self):
        drive, tail = os.path.splitdrive(str(self.root))
        if len(drive) != 2 or drive[1] != ':':
            self.skipTest('fixture is not on a drive path')
        unc = '\\\\localhost\\' + drive[0] + '$' + tail
        if not os.path.isdir(unc) or not os.path.samefile(self.root, unc):
            self.skipTest('local administrative UNC share unavailable')
        normal = WorkspacePolicy.from_dict({'root': str(self.root), 'mode': 'edit'})
        variants = [str(_io_path(self.root, force=True)), unc, str(_io_path(unc, force=True))]
        for value in variants:
            with self.subTest(alias='UNC' if 'localhost' in value.lower() else 'extended'):
                other = WorkspacePolicy.from_dict({'root': value, 'mode': 'edit'})
                self.assertTrue(os.path.samefile(normal.root, other.root))
                self.assertFalse(str(other.root).startswith('\\\\?\\'))
                self.assert_lock_contention(normal.root, other.root)

    def test_cross_process_unc_alias_waits_for_same_root(self):
        drive, tail = os.path.splitdrive(str(self.root))
        if len(drive) != 2 or drive[1] != ':':
            self.skipTest('fixture is not on a drive path')
        unc = '\\\\localhost\\' + drive[0] + '$' + tail
        if not os.path.isdir(unc) or not os.path.samefile(self.root, unc):
            self.skipTest('local administrative UNC share unavailable')
        context = multiprocessing.get_context('spawn')
        entered = context.Event()
        child = context.Process(target=acquire_write_lock, args=(unc, entered))
        try:
            with _write_lock(self.root):
                child.start()
                self.assertFalse(entered.wait(1))
            self.assertTrue(entered.wait(5))
            child.join(5)
            self.assertEqual(child.exitcode, 0)
        finally:
            if child.is_alive():
                child.terminate(); child.join(5)
        result = context.Queue()
        child = context.Process(target=try_owner_lock, args=(unc, result))
        try:
            with WorkspaceService._owner(str(self.root)) as owned:
                self.assertTrue(owned)
                child.start()
                self.assertFalse(result.get(timeout=5))
            child.join(5)
            self.assertEqual(child.exitcode, 0)
        finally:
            if child.is_alive():
                child.terminate(); child.join(5)
            result.close()

    def test_case_sensitive_distinct_roots_keep_identity_and_legacy_serialization(self):
        result = subprocess.run(['fsutil', 'file', 'setCaseSensitiveInfo', str(self.root), 'enable'],
                                capture_output=True, timeout=10)
        if result.returncode:
            self.skipTest('case-sensitive directory cannot be enabled for this fixture')
        first, second = self.root / 'CaseWork', self.root / 'casework'
        first.mkdir(); second.mkdir()
        self.assertFalse(os.path.samefile(first, second))
        a = WorkspacePolicy.from_dict({'root': str(first), 'mode': 'edit'}).root
        b = WorkspacePolicy.from_dict({'root': str(second), 'mode': 'edit'}).root
        self.assert_lock_write_contention(a, b)
        with WorkspaceService._owner(str(a)) as one:
            with WorkspaceService._owner(str(b)) as two:
                self.assertTrue(one and two)
        event = SimpleNamespace(seq=7, type='workspace.tool_started', actor=HOST, task_id='fixture',
                                payload={'name': 'workspace_write', 'root': str(a), 'call_id': 'fixture'})
        service = object.__new__(WorkspaceService)
        service.rt = SimpleNamespace(log=SimpleNamespace(read=lambda **kwargs: [event]))
        self.assertEqual(service.pending(root=str(b), effects_only=True), [])
        self.assert_lock_contention(a, _io_path(a, force=True))

    def assert_lock_write_contention(self, first, second):
        entered = threading.Event()
        def competitor():
            with _write_lock(second): entered.set()
        with _write_lock(first):
            thread = threading.Thread(target=competitor); thread.start()
            self.assertFalse(entered.wait(.2))
        thread.join(4)
        self.assertTrue(entered.is_set())
        self.assertFalse(thread.is_alive())

    def test_legacy_locks_prevent_stale_overwrite_for_plain_and_extended_paths(self):
        import msvcrt
        root = self.work.policy.root
        folder = Path(tempfile.gettempdir()) / 'cater-workspace-locks'
        folder.mkdir(exist_ok=True)
        for old_root in (str(root), str(_io_path(root, force=True))):
            (root / 'race.txt').write_text('initial', encoding='utf-8')
            original = hashlib.sha256(b'initial').hexdigest()
            key = hashlib.sha256(os.path.normcase(old_root).encode('utf-8')).hexdigest()
            fd = os.open(folder / key, os.O_CREAT | os.O_RDWR | os.O_BINARY, 0o600)
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                result = []
                thread = threading.Thread(target=lambda: result.append(self.work.execute('workspace_write',
                    {'path': 'race.txt', 'content': 'new', 'expected_sha256': original})))
                thread.start()
                thread.join(.2)
                self.assertTrue(thread.is_alive())
                (root / 'race.txt').write_text('old', encoding='utf-8')
            finally:
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                os.close(fd)
            thread.join(4)
            self.assertFalse(thread.is_alive())
            self.assertEqual((result[0]['status'], result[0]['error']['code']), ('error', 'conflict'))
            self.assertEqual((root / 'race.txt').read_text(encoding='utf-8'), 'old')
            lock_db = str(Path(tempfile.gettempdir()) / 'cater-workspace-owners')
            with activity_owner(lock_db, str(Path(old_root).resolve()), 'workspace') as old:
                with WorkspaceService._owner(str(root)) as new:
                    self.assertTrue(old)
                    self.assertFalse(new)

    def test_legacy_extended_binding_matches_exact_policy_and_revokes_changes(self):
        config = self.root / 'configuration.json'
        entry = {'id': 'fixture', 'title': 'fixture', 'root': str(self.root), 'mode': 'edit'}
        config.write_text(json.dumps({'workspaces': [entry]}), encoding='utf-8')
        with Runtime(self.root / 'fixture.db', workspaces_path=config) as rt:
            task = 'task_legacy'
            rt.log.emit(type='task.created', task_id=task, actor=Actor.human(), payload={'goal': 'fixture'})
            row = rt.workspaces.config['fixture']
            legacy_root = str(_io_path(row['policy'].root, force=True))
            legacy_policy = {**row['policy'].as_dict(), 'root': legacy_root}
            digest = hashlib.sha256(json.dumps(legacy_policy, sort_keys=True, ensure_ascii=True,
                                    separators=(',', ':')).encode()).hexdigest()
            fingerprint = hashlib.sha256(json.dumps({'policy': digest, 'checks': row['checks']},
                                         sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            payload = {'workspace_id': 'fixture', 'title': 'fixture', 'root': legacy_root,
                       'mode': 'edit', 'fingerprint': fingerprint}
            bound = rt.log.emit(type='workspace.bound', task_id=task, actor=Actor.human(), payload=payload)
            view = rt.workspaces.view(task)
            self.assertTrue(view['available'])
            self.assertEqual((view['revision'], view['root'], view['fingerprint']), (bound.seq, legacy_root, fingerprint))
            self.assertEqual(rt.workspaces.fingerprint(task), fingerprint)
            self.assertEqual(rt.work.scope(task)['workspace']['configured'], fingerprint)
            self.assertEqual(rt.team.read_sources._scope(task), {'binding_seq': bound.seq, 'workspace_id': 'fixture',
                             'root': legacy_root, 'fingerprint': fingerprint})
            self.assertEqual(WorkspaceExecutor(row['policy']).execute('workspace_read', {'path': 'configuration.json'})['status'], 'ok')
            from cater.workspaces import _binding_matches
            self.assertFalse(_binding_matches(row, {**payload, 'root': str(_io_path(self.root / 'other', force=True))}))
            self.assertFalse(_binding_matches(row, {**payload, 'fingerprint': '0' * 64}))
            self.assertFalse(_binding_matches(row, {**payload, 'mode': 'execute'}))
            self.assertFalse(_binding_matches({**row, 'checks': [{'changed': True}]}, payload))
            config.write_text(json.dumps({'workspaces': [{**entry, 'mode': 'read_only'}]}), encoding='utf-8')
            self.assertFalse(rt.workspaces.view(task)['available'])

    def test_legacy_managed_binding_keeps_task_scope_and_revokes_template_changes(self):
        config = self.root / 'managed-config.json'
        managed = self.root / 'managed'
        managed.mkdir()
        config.write_text(json.dumps({'workspaces': [], 'managed': {'root': str(managed), 'mode': 'edit'}}), encoding='utf-8')
        with Runtime(self.root / 'managed.db', workspaces_path=config) as rt:
            task = 'task_legacy_managed'
            rt.log.emit(type='task.created', task_id=task, actor=Actor.human(), payload={'goal': 'fixture'})
            initial = rt.workspaces.bind(task, '__managed__', expected_revision=None)
            row = rt.workspaces.config[initial['workspace_id']]
            legacy_root = str(_io_path(row['policy'].root, force=True))
            old_policy = {**row['policy'].as_dict(), 'root': legacy_root}
            digest = hashlib.sha256(json.dumps(old_policy, sort_keys=True, ensure_ascii=True,
                                    separators=(',', ':')).encode()).hexdigest()
            fingerprint = hashlib.sha256(json.dumps({'policy': digest, 'checks': row['checks']},
                                         sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            rt.log.emit(type='workspace.bound', task_id=task, actor=Actor.human(), payload={
                'workspace_id': initial['workspace_id'], 'title': initial['title'], 'root': legacy_root,
                'mode': 'edit', 'fingerprint': fingerprint, 'managed': True})
            self.assertTrue(rt.workspaces.view(task)['available'])
            self.assertEqual(rt.workspaces.fingerprint(task), fingerprint)
            self.assertIsNotNone(rt.team.read_sources._scope(task))
            config.write_text(json.dumps({'workspaces': [], 'managed': {'root': str(managed), 'mode': 'read_only'}}), encoding='utf-8')
            self.assertFalse(rt.workspaces.view(task)['available'])

    def test_policy_skips_do_not_claim_truncation(self):
        (self.root / '.git').mkdir()
        (self.root / 'binary.dat').write_bytes(b'\xff\x00')
        (self.root / 'note.txt').write_text('needle', encoding='utf-8')
        (self.root / 'linked.txt').write_text('not eligible', encoding='utf-8')
        os.link(self.root / 'linked.txt', self.root / 'hardlink.txt')
        (self.root / 'node_modules').mkdir()
        (self.root / 'node_modules' / 'skipped.txt').write_text('needle', encoding='utf-8')
        listing = self.assert_ok(self.work.execute('workspace_list', {'path': '.'}))
        search = self.assert_ok(self.work.execute('workspace_search', {'path': '.', 'query': 'needle'}))
        self.assertFalse(listing['truncated'])
        self.assertNotIn('.git', [item['path'] for item in listing['entries']])
        self.assertNotIn('hardlink.txt', [item['path'] for item in listing['entries']])
        self.assertFalse(search['truncated'])
        self.assertEqual([item['path'] for item in search['matches']], ['note.txt'])

    def test_pending_effects_follow_physical_root_aliases(self):
        event = SimpleNamespace(seq=7, type='workspace.tool_started', actor=HOST, task_id='fixture',
                                payload={'name': 'workspace_write', 'root': str(self.root), 'call_id': 'fixture'})
        service = object.__new__(WorkspaceService)
        service.rt = SimpleNamespace(log=SimpleNamespace(read=lambda **kwargs: [event]))
        self.assertEqual(service.pending(root=str(_io_path(self.root, force=True)), effects_only=True), [event])
        drive, tail = os.path.splitdrive(str(self.root))
        unc = '\\\\localhost\\' + drive[0] + '$' + tail
        if os.path.isdir(unc) and os.path.samefile(self.root, unc):
            self.assertEqual(service.pending(root=unc, effects_only=True), [event])
        other = self.root / 'other'; other.mkdir()
        self.assertEqual(service.pending(root=str(other), effects_only=True), [])
        event.payload['root'] = str(self.root / 'missing')
        self.assertEqual(service.pending(root=str(other), effects_only=True), [event])

    def test_deep_link_traversal_ads_and_reserved_names_rejected(self):
        import _winapi
        parts = [('part%d-' % i) + 'b' * 38 for i in range(6)]
        base = self.root.joinpath(*parts)
        os.makedirs(_io_path(base, force=True))
        outside = self.root / 'outside'; outside.mkdir()
        (outside / 'secret.txt').write_text('fixture', encoding='utf-8')
        junction = base / 'junction'
        _winapi.CreateJunction(str(outside), str(_io_path(junction, force=True)))
        try:
            result = self.work.execute('workspace_read', {'path': '/'.join([*parts, 'junction', 'secret.txt'])})
            self.assertEqual((result['status'], result['error']['code']), ('error', 'unsafe_path'))
            with self.assertRaises(ValueError):
                WorkspacePolicy.from_dict({'root': str(_io_path(junction, force=True)), 'mode': 'edit'})
        finally:
            os.rmdir(_io_path(junction, force=True))
        source = base / 'source.txt'; source_io = _io_path(source, force=True)
        source_io.write_text('fixture', encoding='utf-8')
        hard = base / 'hard.txt'; os.link(source_io, _io_path(hard, force=True))
        try:
            result = self.work.execute('workspace_read', {'path': '/'.join([*parts, 'hard.txt'])})
            self.assertEqual((result['status'], result['error']['code']), ('error', 'unsafe_path'))
        finally:
            _io_path(hard, force=True).unlink()
        for invalid in ('../escape', 'x/../escape', 'data.txt:stream', 'NUL.txt', 'CON', 'file.', 'file ',
                        'x／..／escape', str(_io_path(source, force=True))):
            with self.subTest(path=invalid):
                result = self.work.execute('workspace_read', {'path': invalid})
                self.assertEqual(result['status'], 'error')
        for invalid in ('unsafe:stream', 'NUL', 'tail.', 'tail '):
            with self.subTest(root=invalid), self.assertRaises(ValueError):
                WorkspacePolicy.from_dict({'root': str(_io_path(self.root / invalid, force=True)), 'mode': 'edit'})

    def test_unreadable_list_and_search_are_marked_incomplete(self):
        import cater.workspace_tools as tools
        (self.root / 'hidden.txt').write_text('needle', encoding='utf-8')
        original = tools._inspect
        def inaccessible(path, *args, **kwargs):
            if path.name == 'hidden.txt': raise PermissionError('fixture')
            return original(path, *args, **kwargs)
        with patch.object(tools, '_inspect', side_effect=inaccessible):
            listing = self.assert_ok(self.work.execute('workspace_list', {'path': '.'}))
            search = self.assert_ok(self.work.execute('workspace_search', {'path': '.', 'query': 'needle'}))
        self.assertTrue(listing['truncated']); self.assertTrue(search['truncated'])


@unittest.skipUnless(os.name == 'nt', 'Windows path namespace')
class WindowsDeepTeamPublication(unittest.TestCase):
    def test_deep_text_and_zip_publish_and_download(self):
        # The other workstream owns artifact-store changes. Keep this test on
        # actual workspace publication and delivery from the event database.
        fixture = reviews.TeamReviewCycleTests()
        with patch.object(tempfile, 'tempdir', str(_io_path(Path(tempfile.gettempdir()), force=True))):
            fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.rt._artifact_root = None
        project = fixture.bind_workspace()
        work = WorkspaceExecutor(WorkspacePolicy.from_dict({'root': str(project), 'mode': 'edit'}))
        parts = [('p%d-' % i) + '🐱' * 25 for i in range(4)]
        rel = '/'.join([*parts, 'result.txt'])
        full = project.joinpath(*parts, 'result.txt')
        self.assertLess(len(str(full)), 248); self.assertGreater(units(full), 260)
        written = work.execute('workspace_write', {'path': rel, 'content': 'fixture content\n', 'expected_sha256': None})
        self.assertEqual(written['status'], 'ok', written)
        text, = fixture.exchange(fixture.worker, ('publish_workspace_file',
            {'path': rel, 'summary': 'Fixture', 'format': 'text'}))
        self.assertEqual(text['status'], 'ok', text)
        archive, = fixture.exchange(fixture.worker, ('publish_workspace_archive',
            {'paths': [rel], 'filename': 'project.zip', 'summary': 'Fixture'}))
        self.assertEqual(archive['status'], 'ok', archive)
        self.assertEqual(fixture.finish()['status'], 'ok')
        body = fixture.rt.team.archive(fixture.task)
        with zipfile.ZipFile(io.BytesIO(body)) as result:
            self.assertEqual(result.read('result.txt'), b'fixture content\n')
            with zipfile.ZipFile(io.BytesIO(result.read('project.zip'))) as nested:
                self.assertEqual(nested.read(rel), b'fixture content\n')
        self.assertTrue(fixture.rt.verify_chain().ok)


if __name__ == '__main__': unittest.main()
