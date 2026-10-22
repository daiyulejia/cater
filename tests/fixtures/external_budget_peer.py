"""Offline timed ACP peer using the real Cater MCP relay, not a model."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback


def main():
    mode, root = sys.argv[1:3]
    relay = None

    def send(value):
        print(json.dumps(value), flush=True)

    def relay_call(identifier, method, params):
        relay.stdin.write((json.dumps({'jsonrpc': '2.0', 'id': identifier, 'method': method, 'params': params}) + '\n').encode())
        relay.stdin.flush()
        return json.loads(relay.stdout.readline())

    try:
        for line in sys.stdin:
            request = json.loads(line)
            method = request.get('method')
            if method == 'initialize':
                result = {'protocolVersion': 1, 'agentInfo': {'name': 'qoder-cli-cn'}}
            elif method == 'session/new':
                server = request['params']['mcpServers'][0]
                env = {key: os.environ[key] for key in ('SystemRoot', 'WINDIR', 'TEMP', 'TMP') if key in os.environ}
                env.update({entry['name']: entry['value'] for entry in server['env']})
                relay = subprocess.Popen([server['command'], *server['args']], cwd=root, env=env,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                relay_call('initialize', 'initialize', {'protocolVersion': '2025-06-18'})
                relay.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
                relay.stdin.flush()
                result = {'sessionId': 'budget-session', 'models': {'currentModelId': 'chosen',
                    'availableModels': [{'modelId': 'chosen', 'name': 'ExactModel'}]}}
            elif method == 'session/prompt':
                for index in range(3 if mode in {'progress', 'stream', 'thinking'} else 20):
                    time.sleep(1 if mode in {'progress', 'stream', 'thinking'} else 0.5)
                    if mode in {'text', 'silent', 'stream', 'thinking'}:
                        if mode != 'silent':
                            send({'jsonrpc': '2.0', 'method': 'session/update', 'params': {
                                'sessionId': 'budget-session', 'update': {
                                'sessionUpdate': 'agent_thought_chunk' if mode == 'thinking' else 'agent_message_chunk',
                                'content': {'type': 'text', 'text': 'PRIVATE_ACTUAL_STREAM' if mode == 'thinking' else 'still talking'}}}})
                    else:
                        relay_call(str(index), 'tools/call', {'name': 'inspect',
                            'arguments': {'page': 0 if mode == 'repeat' else index}})
                result = {'stopReason': 'end_turn'}
            else:
                continue
            send({'jsonrpc': '2.0', 'id': request['id'], 'result': result})
    except Exception:
        (Path(root) / 'peer-error.txt').write_text(traceback.format_exc(), encoding='utf-8')
        raise
    finally:
        if relay is not None:
            relay.terminate()
            relay.wait(timeout=3)
            relay.stdin.close()
            relay.stdout.close()


if __name__ == '__main__':
    main()
