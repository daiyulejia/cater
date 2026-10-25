"""Offline feedback regressions using real Runtime dispatch and local Python.

Scripted native/external drivers exercise production branches, not model APIs.
All publications, policies, databases and project edits are disposable fixtures.
"""

import copy
import hashlib
import json
import os
import sys
import unittest
from unittest.mock import patch

from cater import artifact_versions
from cater.artifact_blobs import read_blob
from cater.workspace_tools import WorkspaceExecutor
from cater.workspaces import WorkspaceService
import test_artifact_versions as versions
import test_team_external as external


# Public messages are pinned independently of the implementation's error table.
ERRORS = {
    'invalid_version_selection': (
        '\u8bf7\u9009\u62e9\u4e00\u81f3128\u4e2a\u660e\u786e\u7248\u672c\u53ca\u5176\u6302\u8f7d\u76ee\u5f55\u3002', ['artifact_versions']),
    'invalid_version_entry': (
        '\u6bcf\u4e2a\u7248\u672c\u9700\u8981\u6b63\u6574\u6570artifact_seq\u548c\u5355\u5c42directory\u3002',
        ['artifact_versions[].artifact_seq', 'artifact_versions[].directory']),
    'invalid_version_directory': (
        'directory\u9700\u8981\u5355\u5c42\u4fbf\u643a\u76ee\u5f55\uff0c\u4e0d\u80fd\u662f\u53d7\u4fdd\u62a4\u540d\u79f0\u3001\u70b9\u8def\u5f84\u6216\u975e\u6cd5\u8def\u5f84\u3002',
        ['artifact_versions[].directory']),
    'duplicate_artifact_sequence': (
        '\u540c\u4e00\u6210\u679c\u5e8f\u53f7\u4e0d\u80fd\u91cd\u590d\u9009\u62e9\u3002', ['artifact_versions[].artifact_seq']),
    'version_directories_conflict': (
        '\u5404\u7248\u672c\u7684directory\u91cd\u590d\u6216\u5b58\u5728\u5927\u5c0f\u5199\u3001Unicode\u7b49\u4ef7\u51b2\u7a81\u3002\u6bd4\u8f83\u4e0d\u540c\u7248\u672c\u8bf7\u4f7f\u7528\u5404\u81ea\u72ec\u7acb\u7684\u76ee\u5f55\uff1b'
        '\u82e5\u53ea\u68c0\u67e5\u5f53\u524d\u591a\u4e2a\u5df2\u53d1\u5e03\u6587\u4ef6\uff0c\u53ef\u6539\u7528artifact_seqs\u4e00\u8d77\u9009\u62e9\u3002',
        ['artifact_versions[].directory']),
    'artifact_selectors_conflict': (
        'artifact_versions\u4e0eartifact_seqs\u4e0d\u80fd\u540c\u65f6\u63d0\u4f9b\u3002'
        '\u68c0\u67e5\u5f53\u524d\u591a\u4e2a\u6587\u4ef6\u9009artifact_seqs\uff1b\u5386\u53f2\u7248\u672c\u5bf9\u7167\u9009artifact_versions\u5e76\u4e3a\u5404\u7248\u672c\u6307\u5b9a\u72ec\u7acbdirectory\u3002',
        ['artifact_versions', 'artifact_seqs']),
    'artifact_version_budget': (
        '\u7248\u672c\u526f\u672c\u603b\u8ba1\u9650128\u6587\u4ef6\u3001\u5355\u6587\u4ef616MiB\u3001\u603b32MiB\u3002', ['artifact_versions']),
    'invalid_version_paths': (
        '\u6302\u8f7d\u540e\u7684\u5b8c\u6574\u6210\u5458\u8def\u5f84\u65e0\u6548\u6216\u51b2\u7a81\uff1b\u8bf7\u6838\u5bf9\u6240\u9009\u7248\u672c\u7684\u6587\u4ef6\u6e05\u5355\u53ca\u76ee\u5f55\u5c42\u7ea7\uff0c'
        '\u4e0d\u8981\u7528\u8282\u9009\u6216\u672a\u53d1\u5e03\u6587\u4ef6\u66ff\u4ee3\u3002', ['artifact_versions']),
}
GENERIC = {
    True: '\u786e\u5207\u7248\u672c\u6216\u6302\u8f7d\u76ee\u5f55\u65e0\u6548\uff0c\u672a\u6267\u884c\u547d\u4ee4\uff1bartifact_versions\u4e0eartifact_seqs\u4e92\u65a5\uff0c\u76ee\u5f55\u987b\u72ec\u7acb\uff0c'
          '\u4e0d\u80fd\u6709\u5927\u5c0f\u5199\u3001Unicode\u6216\u8def\u5f84\u51b2\u7a81\uff0c\u5e76\u9075\u5b88\u6587\u4ef6\u9884\u7b97\u3002',
    False: '\u6210\u679c\u7248\u672c\u3001\u5b8c\u6574\u5185\u5bb9\u6216\u8def\u5f84\u65e0\u6548\uff0c\u672a\u6267\u884c\u547d\u4ee4\uff1b\u8bf7\u8bfb\u53d6\u672c\u7fa4\u6700\u65b0\u6210\u679c\u6e05\u5355\uff0c\u660e\u786e\u9009\u62e9\u5b8c\u6574\u6587\u4ef6\u6216\u5de5\u7a0bZIP\u3002',
}


def rejection(reason):
    message, fields = ERRORS[reason]
    return {'status': 'error', 'code': 'invalid_artifact_check', 'complete': True,
            'outcome': 'not_started', 'reason': reason, 'fields': fields, 'message': message}


def without_descriptions(value):
    if isinstance(value, dict):
        return {key: without_descriptions(item) for key, item in value.items()
                if key != 'description'}
    if isinstance(value, list):
        return [without_descriptions(item) for item in value]
    return value


class ArtifactSelectionFeedbackTests(unittest.TestCase):
    def setUp(self):
        # Import modules, never bind their TestCase classes into this module.
        self.v = versions.ArtifactVersionTests()
        self.v.setUp()
        self.addCleanup(self.v.doCleanups)
        self.f = self.v.f
        self.seq = self.v.archive()
        self.item = {'artifact_seq': self.seq, 'directory': 'mount'}

    def reject(self, selection, reason=None, *, legacy=False, **changes):
        arguments = {'command': 'python', 'args': ['-'], 'stdin': 'raise AssertionError()',
                     **({} if legacy else {'artifact_versions': selection}), **changes}
        with patch('cater.archive_members.read_blob', wraps=read_blob) as blob, \
                patch('cater.team_artifact_check.tempfile.TemporaryDirectory') as temporary, \
                patch('cater.team_artifact_check._write') as write, \
                patch('cater.team_artifact_check.WorkspaceExecutor.execute') as execute, \
                patch('cater.workspace_tools.subprocess.Popen') as process:
            result, = self.f.exchange(self.f.manager, ('workspace_run', arguments))
        for blocked in (blob, temporary, write, execute, process):
            blocked.assert_not_called()
        if reason is not None:
            self.assertEqual(result, rejection(reason))
        self.assertEqual(self.f.rt.team._pending_operations(self.f.task), [])
        return result

    def test_eight_fixed_errors_are_value_errors_and_ignore_exception_text(self):
        self.assertTrue(issubclass(artifact_versions.VersionInputError, ValueError))
        self.assertEqual(len(ERRORS), 8)
        for reason in ERRORS:
            with self.subTest(reason=reason):
                error = artifact_versions.VersionInputError(reason)
                self.assertEqual(str(error), ERRORS[reason][0])
                error.args = ('PRIVATE credential C:/private/source.py',)
                with patch('cater.artifact_versions.inputs', side_effect=error):
                    self.reject([self.item], reason)
                result = error.result()
                result['fields'].append('untrusted mutation')
                self.assertEqual(error.result(), rejection(reason))

    def test_selection_and_entry_structure_reject_before_source_lookup(self):
        selections = [None, {}, 'mount', [], [self.item] * 129]
        entries = [None, 1, True, [], {}, {'artifact_seq': self.seq},
                   {'directory': 'mount'}, {**self.item, 'extra': 'PRIVATE'}]
        entries += [{**self.item, 'artifact_seq': seq}
                    for seq in (True, False, 0, -1, 1.0, str(self.seq), None)]
        entries += [{**self.item, 'directory': directory}
                    for directory in ('', 'x' * 65, 'a/b', 'a\\b', None, True, 1)]
        for selection, reason in [
                *[(value, 'invalid_version_selection') for value in selections],
                *[([value], 'invalid_version_entry') for value in entries]]:
            with self.subTest(selection=repr(selection)[:160]), \
                    patch.object(self.f.rt.team.browser, '_files') as lookup:
                self.reject(selection, reason)
                lookup.assert_not_called()
        with patch.object(self.f.rt.team.browser, '_files') as lookup:
            with self.assertRaises(artifact_versions.VersionInputError) as caught:
                artifact_versions.references(self.f.rt.team, self.f.task, ())
            self.assertEqual(caught.exception.result(), rejection('invalid_version_selection'))
            lookup.assert_not_called()

    def test_mutual_exclusion_and_duplicate_sequence_precede_source_lookup(self):
        with patch.object(self.f.rt.team.browser, '_files') as lookup:
            self.reject([self.item, {**self.item, 'directory': 'other'}],
                        'duplicate_artifact_sequence')
            for seqs in ([], [self.seq], None):
                with self.subTest(seqs=seqs):
                    self.reject([self.item], 'artifact_selectors_conflict', artifact_seqs=seqs)
            lookup.assert_not_called()

    def test_illegal_portable_directories_have_fixed_feedback(self):
        for directory in ('.', '..', '.git', '.GIT', '\uff0egit', '\uff0e\uff0e',
                          '.env', 'secrets', 'credentials.json', 'CON', 'nul.txt',
                          'C:', 'name.', 'name ', 'bad:name', 'bad\x00name'):
            with self.subTest(directory=directory):
                self.reject([{**self.item, 'directory': directory}], 'invalid_version_directory')

    def test_duplicate_casefold_and_nfkc_mounts_have_actionable_feedback(self):
        second = self.v.archive()
        for first_dir, second_dir in (('same', 'same'), ('mount', 'MOUNT'),
                ('K', '\u212a'), ('cafe\u0301', 'caf\u00e9'), ('mount', '\uff4dount'),
                ('Stra\u00dfe', 'STRASSE'), ('\u03c2', '\u03c3')):
            with self.subTest(directories=(first_dir, second_dir)), \
                    patch.object(self.f.rt.team.browser, '_files') as lookup:
                self.reject([{'artifact_seq': self.seq, 'directory': first_dir},
                             {'artifact_seq': second, 'directory': second_dir}],
                            'version_directories_conflict')
                lookup.assert_not_called()

    def test_file_member_and_aggregate_budgets_precede_every_blob(self):
        def manifest(count, size):
            return {'version': 1, 'total_bytes': count * size, 'files': [
                {'path': f'files/{index:03}.bin', 'bytes': size,
                 'sha256': '0' * 64, 'executable': False} for index in range(count)]}

        mib = 1024 * 1024
        for manifests in ((manifest(129, 0),), (manifest(1, 16 * mib + 1),),
                          (manifest(3, 12 * mib),), (manifest(65, 0), manifest(64, 0)),
                          (manifest(1, 16 * mib), manifest(2, 9 * mib))):
            with self.subTest(sizes=[m['total_bytes'] for m in manifests]):
                # Valid archive metadata; only the combined check budget is invalid.
                selection = [{'artifact_seq': self.seq, 'directory': 'valid'}]
                selection += [{'artifact_seq': self.v.altered(self.seq, archive=manifest),
                               'directory': f'copy{index}'}
                              for index, manifest in enumerate(manifests)]
                self.reject(selection, 'artifact_version_budget')
        text = self.f.publish('x', filename='one.txt')
        selection = [{'artifact_seq': text, 'directory': 'text'}]
        selection += [{'artifact_seq': self.v.altered(self.seq, archive=manifest(1, 16 * mib)),
                       'directory': f'copy{index}'} for index in range(2)]
        self.reject(selection, 'artifact_version_budget')

    def test_mount_added_to_valid_member_depth_is_rejected_before_first_blob(self):
        manifest = copy.deepcopy(self.v.event(self.seq).payload['archive'])
        manifest['files'][0]['path'] = '/'.join(['d'] * 31 + ['source.py'])
        # Keep one valid member at the source depth limit; mounting adds one level.
        manifest['files'] = manifest['files'][:1]
        manifest['total_bytes'] = manifest['files'][0]['bytes']
        from cater.workspace_archive import validate_manifest
        validate_manifest(manifest)
        bad = self.v.altered(self.seq, archive=manifest)
        self.reject([self.item, {'artifact_seq': bad, 'directory': 'deep'}],
                    'invalid_version_paths')

    def test_unknown_input_exceptions_stay_generic_and_do_not_leak(self):
        secret = 'PRIVATE_TOKEN=secret C:/private/customer.py unpublished source'
        for versioned in (True, False):
            target = ('cater.artifact_versions.inputs' if versioned
                      else 'cater.team_browser.TeamBrowser._inputs')
            for error_type in (ValueError, KeyError, TypeError):
                with self.subTest(versioned=versioned, error=error_type.__name__), \
                        patch(target, side_effect=error_type(secret)):
                    result = (self.reject([self.item]) if versioned else
                              self.reject(None, legacy=True, artifact_seqs=[self.seq]))
                    self.assertEqual(result, {
                        'status': 'error', 'code': 'invalid_artifact_check', 'complete': True,
                        'outcome': 'not_started', 'message': GENERIC[versioned]})
                    receipts = [e.payload['result'] for e in self.f.events('team.tool_finished')]
                    self.assertNotIn(secret, json.dumps(receipts))

    def test_missing_publication_stays_generic_without_false_known_reason(self):
        result = self.reject([{'artifact_seq': 999999, 'directory': 'mount'}])
        self.assertEqual(result, {'status': 'error', 'code': 'invalid_artifact_check',
            'complete': True, 'outcome': 'not_started', 'message': GENERIC[True]})

    def test_schema_structure_and_existing_permission_gates_are_unchanged(self):
        expected = {'type': 'array', 'minItems': 1, 'maxItems': 128,
            'items': {'type': 'object', 'additionalProperties': False,
                'required': ['artifact_seq', 'directory'], 'properties': {
                    'artifact_seq': {'type': 'integer', 'minimum': 1},
                    'directory': {'type': 'string', 'minLength': 1, 'maxLength': 64}}}}
        self.assertEqual(without_descriptions(artifact_versions.schema()), expected)
        role = self.f.rt.team._role(self.f.manager)
        workspace = WorkspaceExecutor(self.f.rt.workspaces.config['supervision']['policy'])
        base = next(t['input_schema'] for t in workspace.schemas() if t['name'] == 'workspace_run')
        team = next(t['input_schema'] for t in self.f.rt.team._schemas(role, workspace)
                    if t['name'] == 'workspace_run')
        stripped = without_descriptions(team)
        self.assertEqual(stripped['properties'].pop('artifact_versions'), expected)
        self.assertEqual(stripped['properties'].pop('artifact_seqs'), {
            'type': 'array', 'minItems': 1, 'maxItems': 128,
            'items': {'type': 'integer', 'minimum': 1}})
        self.assertEqual(stripped, without_descriptions(base))
        with patch('cater.workspace_tools.subprocess.Popen') as process:
            result = self.v.check([self.item], command='unlisted')
        self.assertEqual(result['outcome'], 'not_started')
        process.assert_not_called()
        for mode in ('read_only', 'edit'):
            with self.subTest(mode=mode):
                self.v.policy(mode)
                workspace = WorkspaceExecutor(self.f.rt.workspaces.config['supervision']['policy'])
                self.assertNotIn('workspace_run', [t['name'] for t in self.f.rt.team._schemas(role, workspace)])
                started = self.f.events('team.tool_started')
                with patch('cater.team_artifact_check._write') as write, \
                        patch('cater.workspace_tools.subprocess.Popen') as process:
                    denied = self.f.exchange(self.f.manager, ('workspace_run', {
                        'command': 'python', 'args': ['-'], 'stdin': 'raise AssertionError()',
                        'artifact_versions': [self.item]}), expected_statuses=('failed',),
                        expected_result_count=0)
                self.assertEqual(denied, [])
                self.assertEqual(self.f.events('team.tool_started'), started)
                write.assert_not_called()
                process.assert_not_called()
        self.assertEqual(self.f.events('team.review_recorded'), [])
        self.assertEqual(self.f.events('team.objective_completed'), [])


CHECK = '''import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys

assert sys.dont_write_bytecode
assert os.environ['PYTHONDONTWRITEBYTECODE'] == '1'
paths = sorted(p.name for p in Path('.').iterdir())
assert paths == ['cases.json', 'responses.py'], paths
spec = importlib.util.spec_from_file_location('published_responses', 'responses.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
cases = json.loads(Path('cases.json').read_text(encoding='utf-8'))
actual = []
for case in cases:
    try:
        actual.append({'value': module.load_response_object(case['input'])})
    except ValueError:
        actual.append({'error': 'ValueError'})
assert actual == [case['expected'] for case in cases], actual
assert sorted(p.name for p in Path('.').iterdir()) == paths
print(json.dumps({'actual': actual, 'hashes': {
    name: hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in paths}}, sort_keys=True))
'''


class ArtifactSelectionRuntimeFeedbackTests(unittest.TestCase):
    def test_native_runtime_recovers_in_same_run_after_delivery(self):
        self.recover('native')

    def test_external_runtime_recovers_in_same_run_after_delivery(self):
        self.recover('external')

    def recover(self, branch):
        if branch == 'native':
            fixture = versions.ArtifactVersionTests()
            fixture.setUp()
            self.addCleanup(fixture.doCleanups)
            f, root = fixture.f, fixture.root
        else:
            f = external.TeamExternalTests()
            f.setUp()
            self.addCleanup(f.doCleanups)
            root = f.root / 'project'
            root.mkdir()
        config = f.root / 'feedback-workspaces.json'
        config.write_text(json.dumps({'workspaces': [{
            'id': 'feedback', 'title': 'Offline feedback fixture', 'root': str(root),
            'mode': 'execute', 'timeout': 10,
            'commands': {'python': [sys.executable, '-I', '-B', '-X', 'utf8']},
            'env_allowlist': ['PYTHONDONTWRITEBYTECODE'],
        }]}), encoding='utf-8')
        f.rt.workspaces = WorkspaceService(f.rt, config)
        f.rt.workspaces.bind(f.task, 'feedback',
                            expected_revision=f.rt.workspaces.view(f.task)['revision'])
        cases = [
            {'input': '{"amount": 3}', 'expected': {'value': {'amount': 3}}},
            {'input': '```json\n{"amount": 3}\n```', 'expected': {'value': {'amount': 3}}},
            {'input': '{"amount": 1, "amount": 2}', 'expected': {'error': 'ValueError'}},
            {'input': '{"amount": NaN}', 'expected': {'error': 'ValueError'}},
            {'input': '[]', 'expected': {'error': 'ValueError'}},
        ]
        contents = {'responses.py': versions.MODULE.read_text(encoding='utf-8'),
                    'cases.json': json.dumps(cases)}
        seqs = []
        if branch == 'native':
            seqs.extend(f.publish(body, filename=name) for name, body in contents.items())
            delivered, = f.exchange(f.manager, ('finish_work', {
                'summary': 'Offline fixture delivery', 'verification': 'No model API called',
                'artifact_seqs': seqs}))
            self.assertEqual(delivered['status'], 'ok')
        else:
            def publish_and_deliver(run):
                for name, body in contents.items():
                    result = run.call('publish_artifact', filename=name, content=body,
                                      summary='Offline complete fixture', format='text')
                    self.assertEqual(result['status'], 'ok', result)
                    seqs.append(result['artifact_seq'])
                run.call('finish_work', summary='Offline fixture delivery',
                         verification='No model API called', artifact_seqs=seqs)
                self.fail('Delivery must yield')

            f.driver.scripts = {f.manager: [publish_and_deliver]}
            f.receive('Publish and deliver the offline fixtures')
            f.drain()
        self.assertEqual(len(seqs), 2)
        self.assertEqual(f.rt.team.view(f.task)['status'], 'done')
        for name in contents:
            (root / name).write_text('unpublished project bytes', encoding='utf-8')
        (root / 'unselected.txt').write_text('must not be copied', encoding='utf-8')

        def snapshot():
            return {p.relative_to(root).as_posix():
                    (p.read_bytes() if p.is_file() else None, p.stat().st_mtime_ns, p.stat().st_mode)
                    for p in [root, *root.rglob('*')]}

        project = snapshot()
        binding = f.rt.workspaces.view(f.task)
        revision = f.rt.team.view(f.task)['revision']
        publications = f.events('deliverable.published')
        deliveries = f.events('team.objective_completed')
        self.assertEqual(len(deliveries), 1)
        bad = {'command': 'python', 'args': ['-'], 'cwd': '.', 'stdin': CHECK,
               'artifact_versions': [{'artifact_seq': seq, 'directory': 'same'} for seq in seqs]}
        observed = []
        roots, processes = [], []
        execute = WorkspaceExecutor.execute
        import subprocess
        popen = subprocess.Popen

        def real_execute(executor, name, arguments):
            roots.append(executor.policy.root)
            self.assertNotEqual(executor.policy.root, root)
            self.assertNotIn('artifact_versions', arguments)
            self.assertNotIn('artifact_seqs', arguments)
            self.assertEqual(arguments['stdin'], CHECK)
            return execute(executor, name, arguments)

        def real_popen(*args, **kwargs):
            self.assertEqual(kwargs['env']['PYTHONDONTWRITEBYTECODE'], '1')
            process = popen(*args, **kwargs)
            processes.append(process)
            return process

        def repair(result):
            self.assertEqual(result, rejection('version_directories_conflict'))
            self.assertIn('artifact_seqs', result['message'])
            self.assertEqual(roots, [])
            self.assertEqual(processes, [])
            self.assertEqual(snapshot(), project)
            observed.append(result)
            return {**{k: v for k, v in bad.items() if k != 'artifact_versions'},
                    'artifact_seqs': seqs}

        if branch == 'native':
            def retry(context, history, tools):
                result = json.loads(history[-1]['content'])
                return f.native_turn('', ('workspace_run', repair(result)))

            def finish(context, history, tools):
                observed.append(json.loads(history[-1]['content']))
                return f.native_turn('Offline checks complete; no new delivery or approval.')

            f.driver.steps[f.manager] = [f.native_turn('', ('workspace_run', bad)), retry, finish]
            f.rt.team.receive(f.task, 'Check both published files after delivery')
        else:
            def recover(run):
                result = run.call('workspace_run', **bad)
                observed.append(run.call('workspace_run', **repair(result)))
                return 'Offline checks complete; no new delivery or approval.'

            f.driver.scripts[f.manager] = [recover]
            f.receive('Check both published files after delivery')
        before = f.rt.log.read(task_id=f.task)[-1].seq
        with patch.dict(os.environ, {'PYTHONDONTWRITEBYTECODE': '1'}), \
                patch('cater.team_artifact_check.WorkspaceExecutor.execute', new=real_execute), \
                patch('cater.workspace_tools.subprocess.Popen', new=real_popen):
            if branch == 'native':
                f.run_team()
                self.assertEqual(f.driver.steps[f.manager], [])
            else:
                f.drain()
        self.assertEqual(len(observed), 2)
        result = observed[1]
        self.assertEqual((result['status'], result['outcome'], result['exit_code']), ('ok', 'passed', 0))
        self.assertTrue(result['complete'])
        self.assertFalse(result['timed_out'])
        self.assertEqual(result['stderr']['bytes'], 0)
        self.assertEqual(result['environment'], 'temporary_artifact_copy_not_sandbox')
        for key in ('scope_current', 'artifacts_unchanged', 'artifact_check_valid'):
            self.assertTrue(result[key], result)
        self.assertEqual(result['argv'], [sys.executable, '-I', '-B', '-X', 'utf8', '-'])
        self.assertEqual(result['stdin']['sha256'], hashlib.sha256(CHECK.encode()).hexdigest())
        self.assertEqual(result['stdin']['bytes'], len(CHECK.encode()))
        expected_artifacts = [{'seq': seq, 'filename': name,
            'content_sha256': hashlib.sha256(body.encode()).hexdigest()}
            for seq, (name, body) in zip(seqs, contents.items())]
        self.assertEqual(result['checked_artifacts'], expected_artifacts)
        self.assertEqual(result['checked_files'], [
            {'artifact_seq': item['seq'], 'path': item['filename'],
             'bytes': len(contents[item['filename']].encode()),
             'sha256': item['content_sha256'], 'executable': False} for item in expected_artifacts])
        stdout = json.loads(result['stdout']['excerpt'])
        self.assertEqual(stdout, {'actual': [case['expected'] for case in cases],
                                 'hashes': {item['filename']: item['content_sha256']
                                            for item in expected_artifacts}})
        self.assertEqual(set(result), {
            'status', 'command', 'cwd', 'argv', 'outcome', 'exit_code', 'timed_out',
            'duration_ms', 'stdout', 'stderr', 'execution_context', 'bytes', 'truncated',
            'complete', 'process_scope', 'stdin', 'output', 'environment', 'scope_current',
            'artifacts_unchanged', 'artifact_check_valid', 'executable_flags_checked',
            'checked_artifacts', 'checked_files', 'note'})
        self.assertEqual(len(roots), 1)
        self.assertFalse(roots[0].exists())
        self.assertEqual(len(processes), 1)
        self.assertEqual(processes[0].poll(), 0)
        self.assertEqual(snapshot(), project)
        self.assertEqual(f.rt.workspaces.view(f.task), binding)
        self.assertEqual(f.rt.team.view(f.task)['revision'], revision)
        self.assertEqual(f.rt.team.view(f.task)['status'], 'done')
        self.assertEqual(f.events('deliverable.published'), publications)
        self.assertEqual(f.events('team.objective_completed'), deliveries)
        self.assertEqual(f.events('team.review_recorded'), [])
        self.assertEqual(f.rt.team_reviews.view(f.task), [])
        self.assertEqual(f.rt.team._pending_operations(f.task), [])
        events = [e for e in f.rt.log.read(task_id=f.task) if e.seq > before]
        calls = [e for e in events if e.type == 'team.tool_started']
        self.assertEqual([e.payload['name'] for e in calls], ['workspace_run', 'workspace_run'])
        self.assertEqual(len({e.payload['run_id'] for e in calls}), 1)
        self.assertEqual(len({e.payload['message_seq'] for e in calls}), 1)
        finishes = [e for e in events if e.type == 'team.tool_finished']
        self.assertEqual([e.payload['result'] for e in finishes], observed)
        run_finished, = [e for e in events if e.type == 'team.run_finished']
        self.assertEqual(run_finished.payload['status'], 'completed')
        if branch == 'external':
            self.assertEqual(f.rt.team.external_runs.pending(f.task), [])
            self.assertTrue(all(run.revoked and run.state.cleanup_complete for run in f.driver.runs))
            external_finished, = [e for e in events if e.type == 'team.external_run_finished']
            self.assertEqual(external_finished.payload['outcome'], 'returned')
        self.assertTrue(f.rt.verify_chain().ok)


if __name__ == '__main__':
    unittest.main()
