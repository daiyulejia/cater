"""Exercise both write modes through real MCP frames and workspace CAS."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from cater.workspace_tools import WorkspaceExecutor, WorkspacePolicy
import test_mcp_host as mcp_fixture


class WorkspaceWriteModeTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.executor = WorkspaceExecutor(WorkspacePolicy.from_dict({'root': str(self.root), 'mode': 'edit'}))
        self.fixture = mcp_fixture.McpToolBridgeTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.tools = self.executor.schemas()
        self.calls, self.sequence = [], 0

        def execute(call):
            self.calls.append(call)
            return self.executor.execute(call.name, call.arguments)
        self.bridge = self.fixture.bridge(execute=execute)
        self.peer = self.fixture.session(self.bridge)

    def rpc(self, method, **params):
        self.sequence += 1
        return self.fixture.exchange(self.bridge, self.peer,
            self.fixture.request(self.sequence, method, **params))['response']['result']

    def call(self, name, **arguments):
        result = self.rpc('tools/call', name=name, arguments=arguments)
        body = json.loads(result['content'][0]['text'])
        self.assertEqual(result['isError'], body['status'] != 'ok')
        return body

    def read(self):
        result = self.call('workspace_read', path='note.md')
        self.assertEqual(result['status'], 'ok')
        return result

    def test_mcp_preserves_modes_and_exact_unicode_quotes_and_backslashes(self):
        listing = self.rpc('tools/list')['tools']
        supplied = {row['name']: row for row in self.executor.schemas()}
        self.assertEqual({row['name']: row['inputSchema'] for row in listing},
                         {name: row['input_schema'] for name, row in supplied.items()})
        original = '# \u5907\u5fd8\u5f55\r\n`printf "a\\nb"`\r\n\u524d OLD \u540e\r\n'
        created = self.call('workspace_write', path='note.md', content=original, expected_sha256=None)
        self.assertEqual(created['status'], 'ok')
        self.assertNotIn('old_text', self.calls[-1].arguments)
        actual = self.read()
        self.assertEqual(actual['content'], original)
        changed = self.call('workspace_write', path='note.md', old_text='OLD', content='\u6708\u4eae\U0001f680',
                            expected_sha256=actual['sha256'])
        self.assertEqual(changed['replacements'], 1)
        self.assertEqual((self.root / 'note.md').read_bytes(), original.replace('OLD', '\u6708\u4eae\U0001f680').encode())
        current = self.read()
        full = '# \u65b0\u7a3f\n\u5b8c\u6574\u66ff\u6362\uff0c\u4e0d\u7559\u65e7\u524d\u7f00\u6216\u540e\u7f00\u3002\n'
        rewritten = self.call('workspace_write', path='note.md', content=full, expected_sha256=current['sha256'])
        self.assertEqual(rewritten['status'], 'ok')
        self.assertNotIn('old_text', self.calls[-1].arguments)
        self.assertNotIn('replacements', rewritten)
        self.assertEqual((self.root / 'note.md').read_bytes(), full.encode())
        self.assertEqual(rewritten['sha256'], hashlib.sha256(full.encode()).hexdigest())

    def test_conflict_requires_explicit_new_choice_and_fresh_hash_for_full_replacement(self):
        original = b'OLD middle OLD\n'
        created = self.call('workspace_write', path='note.md', content=original.decode(), expected_sha256=None)
        rejected = self.call('workspace_write', path='note.md', old_text='OLD', content='NEW',
                             expected_sha256=created['sha256'])
        self.assertEqual(rejected['error']['code'], 'conflict')
        self.assertIn('omit old_text', rejected['error']['message'])
        self.assertEqual((self.root / 'note.md').read_bytes(), original)
        self.assertEqual(len(self.calls), 2, 'No implicit fallback or second write')
        current = self.read()
        external = b'Another writer changed the file\n'
        (self.root / 'note.md').write_bytes(external)
        stale = self.call('workspace_write', path='note.md', content='Complete new document\n',
                          expected_sha256=current['sha256'])
        self.assertEqual(stale['error']['code'], 'conflict')
        self.assertEqual((self.root / 'note.md').read_bytes(), external)
        reread = self.read()
        completed = self.call('workspace_write', path='note.md', content='Complete new document\n',
                              expected_sha256=reread['sha256'])
        self.assertEqual(completed['status'], 'ok')
        self.assertEqual((self.root / 'note.md').read_bytes(), b'Complete new document\n')
        self.assertFalse(list(self.root.glob('.workspace-tmp-*')))


if __name__ == '__main__':
    unittest.main()
