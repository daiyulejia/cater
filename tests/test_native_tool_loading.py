"""Opt-in schema loading with real workspace actions and scripted native turns."""

import copy
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from cater.team_context import history_window, json_bytes
from cater.tool_loading import CORE, TOOL, load_result, select_tools, with_loader
from cater.tool_protocol import prepare_tool_turn
from cater.workspaces import WorkspaceService
from cater.workspace_tools import WorkspaceExecutor
import test_team_supervision as supervision
import test_protocol_usage as protocols


class NativeToolLoadingTests(unittest.TestCase):
    def fixture(self):
        f = supervision.TeamSupervisionTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        f.driver.tool_loading = 'on_demand'
        return f

    def bind(self, f, mode='execute'):
        project = f.root / 'project'
        project.mkdir()
        config = f.root / 'loading-workspace.json'
        config.write_text(json.dumps({'workspaces': [{'id': 'project', 'title': 'Loading fixture',
            'root': str(project), 'mode': mode,
            **({'commands': {'python': [sys.executable, '-I', '-B']}, 'timeout': 5}
               if mode == 'execute' else {})}]}), encoding='utf-8')
        f.rt.workspaces = WorkspaceService(f.rt, config)
        f.rt.workspaces.bind(f.task, 'project', expected_revision=None)
        return project

    def input(self, f, role=None):
        role = role or f.rt.organization.leader
        source = f.rt.team.receive(f.task, '@' + role.name + ' inspect', request_id='loading-fixture-input')
        return f.rt.team._model_input(f.task, source, role, run_id='loading-fixture-run')

    def test_default_all_returns_original_definitions_without_loader(self):
        f = self.fixture()
        f.driver.tool_loading = 'all'
        raw, _, _, tools, _ = self.input(f)
        self.assertNotIn(TOOL, {t['name'] for t in tools})
        selected, catalog = select_tools(tools, set())
        self.assertIs(selected, tools)
        self.assertIsNone(catalog)
        self.assertNotIn('tool_catalog', raw)

    def test_first_input_keeps_catalog_and_original_definitions_with_lower_footprint(self):
        f = self.fixture()
        self.bind(f)
        raw, _, _, tools, _ = self.input(f)
        original = copy.deepcopy(tools)
        selected, catalog = select_tools(tools, set())
        names = {t['name'] for t in selected}
        self.assertEqual(names, CORE & {t['name'] for t in tools})
        self.assertIn('workspace_run', catalog['available_names'])
        self.assertNotIn('workspace_run', names)
        skill = next(row for row in raw['skills_index']['skills'] if row['id'] == 'engineering-checks')
        self.assertEqual(skill['status'], 'ready')
        self.assertEqual(f.rt.skills.read({'skill_id': 'engineering-checks'},
            **f.rt.team._skill_scope(f.task, f.rt.organization.leader))['status'], 'ok')
        requested = {'workspace_read', 'workspace_write', 'workspace_run', 'request_review'}
        expanded, _ = select_tools(tools, requested)
        by_name = {t['name']: t for t in tools}
        for tool in expanded:
            self.assertEqual(tool, by_name[tool['name']], 'Descriptions and parameter contracts were trimmed')
        self.assertLess(json_bytes(selected) + json_bytes(catalog), json_bytes(tools) * 0.5)
        self.assertEqual(tools, original)
        self.assertNotIn('tool_catalog', raw, 'Public/raw context must not be mutated')

    def test_loader_rejects_invalid_or_unknown_names_without_partial_selection(self):
        tools = with_loader([{'name': 'read_item', 'description': 'Read only',
                             'input_schema': {'type': 'object', 'properties': {}, 'required': []}}])
        schema = tools[-1]['input_schema']
        cases = [None, [], {}, {'names': []}, {'names': 'read_item'}, {'names': [None]},
                 {'names': [True]}, {'names': ['read_item', 'read_item']},
                 {'names': ['read_item', 'unsafe_write']}, {'names': ['load_tools']},
                 {'names': ['read_item'], 'extra': 'private'}]
        for args in cases:
            with self.subTest(args=args):
                before = copy.deepcopy(args)
                result = load_result(args, schema)
                self.assertEqual((result['status'], result['outcome']), ('error', 'not_started'))
                self.assertIs(result['complete'], True)
                self.assertNotIn('loaded_names', result)
                self.assertEqual(args, before)
        valid = load_result({'names': ['read_item']}, schema)
        self.assertEqual(valid['loaded_names'], ['read_item'])
        self.assertNotIn('input_schema', valid, 'Do not duplicate definitions in result and next request')

    def test_loading_never_enlarges_permissions_and_paused_catalog_is_readonly(self):
        f = self.fixture()
        self.bind(f, 'read_only')
        _, _, _, tools, _ = self.input(f)
        enum = tools[-1]['input_schema']['properties']['names']['items']['enum']
        self.assertIn('workspace_read', enum)
        self.assertNotIn('workspace_write', enum)
        self.assertNotIn('workspace_run', enum)
        f.rt.execution_control.change(f.task, 'pause', request_id='loading-pause-fixture')
        _, _, _, paused, _ = self.input(f)
        enum = paused[-1]['input_schema']['properties']['names']['items']['enum']
        self.assertIn('read_artifact', enum)
        self.assertNotIn('workspace_read', enum)
        self.assertNotIn('set_objective', enum)
        self.assertNotIn('finish_work', enum)

    def test_loaded_definitions_survive_history_eviction_unchanged(self):
        f = self.fixture()
        self.bind(f)
        _, _, _, tools, _ = self.input(f)
        selected, _ = select_tools(tools, {'workspace_read'})
        history = []
        receipts = []
        for index in range(3):
            call_id = 'history-' + str(index)
            history.extend([{'role': 'assistant', 'content': 'x' * 1000,
                'tool_calls': [{'id': call_id, 'name': 'workspace_read', 'arguments': {'path': 'note.txt'}}]},
                {'role': 'tool', 'tool_call_id': call_id, 'content': '{}'}])
            receipts.append({'call_id': call_id, 'started_seq': index + 1, 'name': 'workspace_read', 'status': 'ok'})
        old = copy.deepcopy(history)
        retained, window = history_window(history, receipts, tools=selected, budget=1)
        self.assertGreater(window['omitted_groups'], 0)
        prepare_tool_turn(role='worker', prompt='', history=retained, tools=selected)
        self.assertEqual(history, old)
        self.assertEqual(retained[-2:], history[-2:])

    def response(self, cls, call_id=None, calls=None):
        body = protocols.response(cls)
        if cls is protocols.AnthropicMessagesDriver:
            body['stop_reason'] = 'tool_use' if calls else 'end_turn'
            body['content'] = ([{'type': 'thinking', 'thinking': 'synthetic-only',
                                 'signature': 'signed-loading-' + call_id}] if calls else [])
            body['content'] += ([{'type': 'tool_use', 'id': call_id + str(i), 'name': name, 'input': args}
                                for i, (name, args) in enumerate(calls)] if calls else
                               [{'type': 'text', 'text': 'Read only, no delivery claim.'}])
        else:
            body['choices'][0]['finish_reason'] = 'tool_calls' if calls else 'stop'
            body['choices'][0]['message'] = ({'role': 'assistant', 'content': None, 'tool_calls': [
                {'id': call_id + str(i), 'type': 'function', 'function': {
                    'name': name, 'arguments': json.dumps(args)}} for i, (name, args) in enumerate(calls)]}
                if calls else {'role': 'assistant', 'content': 'Read only, no delivery claim.'})
        return body

    def test_both_wire_protocols_register_exact_next_definitions_and_preserve_history(self):
        for cls in protocols.CLASSES:
            replies = [(200, self.response(cls, 'load', [(TOOL, {'names': ['workspace_read']})])),
                       (200, self.response(cls, 'read', [('workspace_read', {'path': 'note.txt'})])),
                       (200, self.response(cls))]
            with self.subTest(protocol=cls.__name__), protocols.ProtocolUsageTests().session(cls, replies) as (f, driver, requests):
                project = self.bind(f, 'read_only')
                (project / 'note.txt').write_bytes(b'actual local fixture bytes\n')
                driver.tool_loading = 'on_demand'
                protocols.ProtocolUsageTests().run_message(f)
                self.assertEqual(len(requests), 3)
                tool_maps = [{(tool['name'] if cls is protocols.AnthropicMessagesDriver else
                               tool['function']['name']): tool for tool in request['tools']} for request in requests]
                self.assertNotIn('workspace_read', tool_maps[0])
                self.assertIn('workspace_read', tool_maps[1])
                self.assertNotIn('workspace_search', tool_maps[1])
                definition = tool_maps[1]['workspace_read']
                if cls is not protocols.AnthropicMessagesDriver:
                    definition = definition['function']
                parameters = definition.get('input_schema', definition.get('parameters'))
                expected = next(t for t in f.rt.team._schemas(f.rt.organization.leader,
                    WorkspaceExecutor(f.rt.workspaces.config['project']['policy']))
                    if t['name'] == 'workspace_read')
                self.assertEqual(parameters, expected['input_schema'])
                self.assertEqual(definition['description'], expected['description'])
                if cls is protocols.AnthropicMessagesDriver:
                    first_history = requests[1]['messages'][1]['content']
                    self.assertEqual(first_history[0]['signature'], 'signed-loading-load')
                    self.assertEqual(requests[2]['messages'][1]['content'], first_history)
                self.assertIn('actual local fixture bytes', json.dumps(requests[2]))
                self.assertEqual(len(f.events('team.tool_started')), 2)
                self.assertEqual(f.events('team.tool_started')[0].payload['execution_contract']['kind'], 'atomic_local_events')
                self.assertEqual(f.rt.team._pending_operations(f.task), [])
                self.assertEqual(len(f.events('team.objective_completed')), 0)

    def test_loading_and_undeclared_action_in_same_response_executes_neither(self):
        for cls in protocols.CLASSES:
            reply = self.response(cls, 'bad-batch', [(TOOL, {'names': ['workspace_write']}),
                ('workspace_write', {'path': 'not-created.py', 'content': 'never', 'expected_sha256': None})])
            with self.subTest(protocol=cls.__name__), protocols.ProtocolUsageTests().session(cls, [(200, reply)]) as (f, driver, requests):
                project = self.bind(f)
                driver.tool_loading = 'on_demand'
                protocols.ProtocolUsageTests().run_message(f)
                self.assertEqual(len(requests), 1)
                self.assertFalse((project / 'not-created.py').exists())
                self.assertEqual(f.events('team.tool_started'), [])
                self.assertEqual(f.events('team.model_called')[0].payload['response_status'], 'contained')

    def test_protocol_correction_does_not_autoload_or_replay_the_rejected_batch(self):
        for cls in protocols.CLASSES:
            bad = self.response(cls, 'rejected', [(TOOL, {'names': ['workspace_read']}),
                ('workspace_read', {'path': 'note.txt'})])
            replies = [(200, bad),
                (200, self.response(cls, 'correct-load', [(TOOL, {'names': ['workspace_read']})])),
                (200, self.response(cls, 'correct-read', [('workspace_read', {'path': 'note.txt'})])),
                (200, self.response(cls))]
            with self.subTest(protocol=cls.__name__), protocols.ProtocolUsageTests().session(
                    cls, replies, tool_response_corrections=1) as (f, driver, requests):
                project = self.bind(f, 'read_only')
                (project / 'note.txt').write_bytes(b'one actual read\n')
                driver.tool_loading = 'on_demand'
                protocols.ProtocolUsageTests().run_message(f)
                self.assertEqual(len(requests), 4)
                names = lambda request: {(row['name'] if cls is protocols.AnthropicMessagesDriver else
                                          row['function']['name']) for row in request['tools']}
                self.assertNotIn('workspace_read', names(requests[1]))
                self.assertIn('workspace_read', names(requests[2]))
                self.assertEqual([event.payload['name'] for event in f.events('team.tool_started')],
                                 [TOOL, 'workspace_read'])
                self.assertEqual(len(f.events('team.response_correction_requested')), 1)
                self.assertEqual(len(f.events('team.model_called')), 4)
                self.assertEqual(f.rt.team._pending_operations(f.task), [])

    def test_custom_driver_cannot_partially_dispatch_loader_and_hidden_action(self):
        f = self.fixture()
        project = self.bind(f)
        f.driver.steps[f.manager] = [f.native_turn('',
            (TOOL, {'names': ['workspace_write']}),
            ('workspace_write', {'path': 'never.py', 'content': 'never', 'expected_sha256': None}))]
        f.rt.team.receive(f.task, 'Invalid custom batch', request_id='loading-batch-not-admitted')
        f.rt.collaborate(f.task)
        self.assertEqual(f.events('team.tool_started'), [])
        self.assertFalse((project / 'never.py').exists())
        self.assertEqual(len(f.events('team.model_called')), 1)

    def test_failed_loader_receipt_does_not_block_later_work_or_hide_audit(self):
        f = self.fixture()
        self.bind(f)
        f.driver.steps[f.manager] = [f.native_turn('', (TOOL, {'names': ['workspace_write']}))]
        f.rt.team.receive(f.task, 'Load metadata only', request_id='loading-local-receipt-failure')
        original = f.rt.log.emit

        def fail_receipt(**values):
            if values['type'] == 'team.tool_finished' and values['payload']['name'] == TOOL:
                raise RuntimeError('Synthetic receipt failure')
            return original(**values)

        with patch.object(f.rt.log, 'emit', side_effect=fail_receipt):
            f.rt.collaborate(f.task)
        self.assertEqual(f.rt.team._pending_operations(f.task), [])
        self.assertEqual(f.rt.workspaces.pending(task_id=f.task), [])
        self.assertEqual(f.events('team.tool_finished'), [])
        starts = f.events('team.tool_started')
        self.assertEqual([row.payload['name'] for row in starts], [TOOL])
        f.driver.steps[f.manager] = [f.native_turn('', ('set_objective', {'goal': 'A new bounded text task'})),
            f.native_turn('', ('finish_work', {'summary': 'A clear text answer', 'verification': 'No file work requested'}))]
        f.rt.team.receive(f.task, 'Answer a new bounded text task', request_id='loading-after-receipt-failure')
        f.rt.collaborate(f.task)
        self.assertEqual(f.rt.team.view(f.task)['status'], 'done')
        self.assertEqual(f.events('team.tool_started')[0], starts[0], 'Prior audit must stay intact')
        self.assertTrue(f.rt.verify_chain().ok)

    def run_actual_workflow(self, mode):
        f = self.fixture()
        f.driver.tool_loading = mode
        project = self.bind(f)
        reviewer = next(r.id for r in f.rt.worker_roles if r.id != f.worker)
        source_bytes = b'print(30)\n'
        observed = []

        def last(history):
            return json.loads(history[-1]['content'])

        def create():
            return f.native_turn('', ('set_objective', {'goal': 'Save and independently check a tiny Python file'}),
                ('workspace_write', {'path': 'answer.py', 'content': source_bytes.decode(), 'expected_sha256': None}))

        def manager(context, history, tools):
            names = {t['name'] for t in tools}
            if context['message']['kind'] == 'report':
                if mode == 'on_demand':
                    self.assertNotIn('workspace_run', names, 'A new run must not inherit loaded definitions')
                self.assertEqual(context['reviews'][0]['status'], 'approved')
                return f.native_turn('', ('finish_work', {'summary': 'Actual reviewed file delivered',
                    'verification': 'Both author and peer ran the submitted file; stdout 30.'}))
            if not history:
                if mode == 'on_demand':
                    self.assertNotIn('workspace_write', names)
                    return f.native_turn('', (TOOL, {'names': ['workspace_write', 'workspace_run', 'request_review']}))
                return create()
            name = next(r['tool_calls'][-1]['name'] for r in reversed(history) if r['role'] == 'assistant')
            if name == TOOL:
                self.assertEqual(set(last(history)['loaded_names']), {'workspace_write', 'workspace_run', 'request_review'})
                self.assertTrue({'workspace_write', 'workspace_run', 'request_review'} <= names)
                return create()
            if name == 'workspace_write':
                self.assertEqual(last(history)['sha256'], hashlib.sha256(source_bytes).hexdigest())
                return f.native_turn('', ('workspace_run', {'command': 'python', 'args': ['answer.py']}))
            if name == 'workspace_run':
                self.assertEqual(last(history)['stdout']['excerpt'].strip(), '30')
                observed.append('author-ran')
                return f.native_turn('', ('request_review', {'reviewer': reviewer,
                    'instruction': 'Read and execute this exact submitted file.',
                    'workspace_file': {'path': 'answer.py', 'format': 'code'}}))
            self.fail(name)

        def peer(context, history, tools):
            row, = context['reviews']
            artifact = row['artifacts'][0]['seq']
            if not history:
                self.assertIn('record_review', {t['name'] for t in tools},
                              'A formal reviewer must not need discovery merely to record its decision')
                if mode == 'on_demand':
                    self.assertNotIn('workspace_run', {t['name'] for t in tools})
                    return f.native_turn('', (TOOL, {'names': ['read_artifact', 'workspace_run', 'record_review']}))
                return f.native_turn('', ('read_artifact', {'artifact_seq': artifact}))
            name = next(r['tool_calls'][-1]['name'] for r in reversed(history) if r['role'] == 'assistant')
            if name == TOOL:
                return f.native_turn('', ('read_artifact', {'artifact_seq': artifact}))
            if name == 'read_artifact':
                self.assertEqual(last(history)['content'].encode(), source_bytes)
                return f.native_turn('', ('workspace_run', {'command': 'python', 'args': ['answer.py'],
                    'artifact_seqs': [artifact]}))
            self.assertEqual(name, 'workspace_run')
            self.assertEqual(last(history)['stdout']['excerpt'].strip(), '30')
            self.assertTrue(last(history)['artifacts_unchanged'])
            observed.append('peer-ran-version')
            return f.native_turn('', ('record_review', {'review_seq': row['request_seq'],
                'verdict': 'approved', 'feedback': 'Read exact submitted bytes and ran them: 30.'}))

        f.driver.steps = {f.manager: [manager] * 12, reviewer: [peer] * 8}
        f.rt.team.receive(f.task, 'Create and review the tiny file', request_id='loading-complete-fixture')
        f.run_team()
        self.assertEqual(observed, ['author-ran', 'peer-ran-version'])
        self.assertEqual((project / 'answer.py').read_bytes(), source_bytes)
        self.assertEqual(f.rt.team.view(f.task)['status'], 'done')
        self.assertEqual(len(f.events('team.objective_completed')), 1)
        self.assertEqual([r['status'] for r in f.results(TOOL)], ['ok', 'ok'] if mode == 'on_demand' else [])
        self.assertEqual(f.rt.team._pending_operations(f.task), [])
        self.assertTrue(all(e.payload['tokens'] == 7 for e in f.events('team.model_called')))
        sizes = [e.payload['request_sizes'] for e in f.events('team.model_called')]
        return {'mode': mode, 'requests': len(sizes), 'sizes': sizes,
                'input_component_bytes': sum(row[key] for row in sizes for key in
                    ('prompt_utf8_bytes', 'history_json_bytes', 'tools_json_bytes')),
                'file_sha256': hashlib.sha256(source_bytes).hexdigest()}

    def test_actual_write_run_version_review_and_delivery_with_run_local_loading(self):
        baseline = self.run_actual_workflow('all')
        loaded = self.run_actual_workflow('on_demand')
        self.assertEqual(loaded['file_sha256'], baseline['file_sha256'])
        self.assertEqual(loaded['requests'], baseline['requests'] + 2)
        self.assertLess(loaded['input_component_bytes'], baseline['input_component_bytes'])
        self.footprint_comparison = {'all': baseline, 'on_demand': loaded,
            'scope': 'scripted native task with actual files/checks, byte components only, not paid token savings'}


if __name__ == '__main__':
    unittest.main()
