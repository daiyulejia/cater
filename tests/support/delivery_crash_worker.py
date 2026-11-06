"""Offline child killed immediately after a real finish_work receipt commits."""
import json
import os
from pathlib import Path
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from cater.anthropic import AnthropicMessagesDriver
from cater.drivers import OpenAICompatibleDriver
from cater.external_agent import QoderAcpDriver
from cater.runtime import Runtime
from cater.tool_protocol import ToolCall


root, mode = Path(sys.argv[1]), sys.argv[2]
injection = sys.argv[3] if len(sys.argv) > 3 else None
roles = json.loads(Path('cater/packs/default_roles.json').read_text(encoding='utf-8'))
roles['organization']['leader']['driver'] = 'offline'
for role in roles['roles']:
    role['driver'] = 'offline'
roles_path = root / 'roles.json'
roles_path.write_text(json.dumps(roles), encoding='utf-8')
arguments = {'summary': 'Durable offline delivery.', 'verification': 'Offline fixture only.'}

class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers['Content-Length']))
        if mode == 'openai':
            data = {'choices': [{'finish_reason': 'tool_calls', 'message': {'role': 'assistant',
                'content': None, 'tool_calls': [{'id': 'finish', 'type': 'function',
                    'function': {'name': 'finish_work', 'arguments': json.dumps(arguments)}}]}}],
                'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}}
        else:
            data = {'type': 'message', 'role': 'assistant', 'stop_reason': 'tool_use',
                    'content': [{'type': 'tool_use', 'id': 'finish', 'name': 'finish_work', 'input': arguments}],
                    'usage': {'input_tokens': 1, 'output_tokens': 1}}
        raw = json.dumps(data).encode()
        self.send_response(200)
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)
    def log_message(self, *args):
        pass

server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
os.environ['CATER_DELIVERY_OFFLINE'] = 'fixture'
if mode == 'qoder':
    driver = QoderAcpDriver('offline', 'never-launch-qoder', 'OfflineModel')
    runs = []
    original = driver.agent_run
    def agent_run(**kwargs):
        run = original(**kwargs)
        runs.append(run)
        return run
    driver.agent_run = agent_run
    class Session:
        def __init__(self, *args, **kwargs):
            self.session_id = 'offline-delivery'
            self.agent_info = {'name': 'qoder-cli-cn'}
        def open(self, **kwargs):
            return {'models': {'currentModelId': 'offline', 'availableModels': [
                {'modelId': 'offline', 'name': 'OfflineModel'}]}}
        def prompt(self, prompt, **kwargs):
            runs[-1]._call(ToolCall('finish', 'finish_work', arguments))
            raise AssertionError('expected process exit')
        def cancel(self): pass
        def close(self): pass
    patch('cater.external_agent.AcpSession', Session).start()
else:
    cls = OpenAICompatibleDriver if mode == 'openai' else AnthropicMessagesDriver
    driver = cls('offline', endpoint=f'http://127.0.0.1:{server.server_port}/fixture',
                 model='offline', api_key_env='CATER_DELIVERY_OFFLINE', timeout=8)
rt = Runtime(root / 'team.db', roles_path=roles_path, drivers={'offline': driver})
task = rt.create_task('Offline crash recovery')
rt.team.enable(task)
(root / 'task.txt').write_text(task, encoding='utf-8')
rt.team.receive(task, 'Deliver the offline result')
dispatch = rt.team._dispatch_tool
def crash(*args, **kwargs):
    if injection == 'before' and args[5].name == 'finish_work':
        worker = rt.worker_roles[0]
        rt.team.receive(task, f'@{worker.name} Earlier customer instruction')
    if injection == 'objective' and args[5].name == 'finish_work':
        from cater.team import HOST
        rt.log.emit(type='team.objective_updated', task_id=task, actor=HOST, payload={'goal': 'Changed goal'})
    result = dispatch(*args, **kwargs)
    if args[5].name == 'finish_work' and result[1].get('status') == 'ok':
        os._exit(73)
    return result
rt.team._dispatch_tool = crash
rt.collaborate(task)
if injection == 'objective':
    os._exit(74)
raise AssertionError('delivery was not reached')
