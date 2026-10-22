"""Creation and update contracts must be explicit before a model invokes a write."""
from pathlib import Path
import tempfile
import unittest

from cater.workspace_tools import WorkspaceExecutor, WorkspacePolicy


class WorkspaceHashSchemaTests(unittest.TestCase):
    def test_schema_and_executor_agree_on_creation_null_and_hash_length(self):
        with tempfile.TemporaryDirectory() as directory:
            executor = WorkspaceExecutor(WorkspacePolicy.from_dict({'root': directory, 'mode': 'edit'}))
            schema = next(tool for tool in executor.schemas() if tool['name'] == 'workspace_write')
            field = schema['input_schema']['properties']['expected_sha256']
            self.assertEqual(field['type'], ['string', 'null'])
            self.assertEqual((field['minLength'], field['maxLength']), (64, 64))
            for wrong in ('', 'null', 'short'):
                result = executor.execute('workspace_write', {'path': 'sample.txt', 'content': 'first',
                    'expected_sha256': wrong})
                self.assertEqual(result['status'], 'error')
                self.assertEqual(result['error']['code'], 'invalid_arguments')
                self.assertFalse((Path(directory) / 'sample.txt').exists())
            created = executor.execute('workspace_write', {'path': 'sample.txt', 'content': 'first',
                'expected_sha256': None})
            self.assertEqual(created['status'], 'ok')
            conflict = executor.execute('workspace_write', {'path': 'sample.txt', 'content': 'overwrite',
                'expected_sha256': None})
            self.assertEqual(conflict['status'], 'error')
            updated = executor.execute('workspace_write', {'path': 'sample.txt', 'content': 'second',
                'expected_sha256': created['sha256']})
            self.assertEqual(updated['status'], 'ok')
            self.assertEqual((Path(directory) / 'sample.txt').read_text(), 'second')
