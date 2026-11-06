"""Offline ACP peer with actual MCP relay; never launches a real model."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def main():
    mode, root = sys.argv[1], Path(sys.argv[2])
    relay = None
    def send(value):
        print(json.dumps(value), flush=True)
    def rpc(identifier, method, params):
        relay.stdin.write((json.dumps({'jsonrpc': '2.0', 'id': identifier, 'method': method, 'params': params}) + '\n').encode())
        relay.stdin.flush()
        return json.loads(relay.stdout.readline())
    def tool(name, args):
        response = rpc(name, 'tools/call', {'name': name, 'arguments': args})
        return response
    try:
        for line in sys.stdin:
            request = json.loads(line)
            method = request.get('method')
            if method == 'initialize':
                result = {'protocolVersion': 1, 'agentInfo': {'name': 'qoder-cli-cn'}}
            elif method == 'session/new':
                server = request['params']['mcpServers'][0]
                env = {k: os.environ[k] for k in ('SystemRoot', 'WINDIR', 'TEMP', 'TMP') if k in os.environ}
                env.update({e['name']: e['value'] for e in server['env']})
                relay = subprocess.Popen([server['command'], *server['args']], cwd=root, env=env,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                rpc('init', 'initialize', {'protocolVersion': '2025-06-18'})
                relay.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
                relay.stdin.flush()
                result = {'sessionId': 'offline-review', 'models': {'currentModelId': 'chosen',
                          'availableModels': [{'modelId': 'chosen', 'name': 'offline'}]}}
            elif method == 'session/prompt':
                context = json.loads(request['params']['prompt'][0]['text'].rsplit('\n', 1)[-1])
                names = {t['name'] for t in rpc('list', 'tools/list', {})['result']['tools']}
                if mode in {'publish', 'request', 'record', 'initial', 'old-publish'}:
                    if mode == 'publish':
                        tool('publish_artifact', {'filename': 'full-chain.txt', 'content': 'reviewed bytes',
                                                  'format': 'text', 'summary': 'Offline candidate'})
                        tool('wait_for_reply', {})
                    elif mode == 'request':
                        artifact = context['artifacts'][-1]['seq']
                        reviewer = (root / 'reviewer-id').read_text(encoding='utf-8')
                        tool('request_review', {'reviewer': reviewer, 'artifact_seqs': [artifact], 'instruction': 'Inspect exact bytes'})
                    elif mode == 'record':
                        review = context['reviews'][0]
                        tool('record_review', {'review_seq': review['request_seq'], 'verdict': 'approved', 'feedback': 'Offline checked'})
                    elif mode == 'old-publish':
                        tool('publish_artifact', {'filename': 'stale.txt', 'content': 'stale',
                                                  'format': 'text', 'summary': 'stale'})
                    else:
                        send({'jsonrpc': '2.0', 'method': 'session/update', 'params': {'sessionId': 'offline-review',
                            'update': {'sessionUpdate': 'agent_message_chunk',
                                       'content': {'type': 'text', 'text': 'Review received internally.'}}}})
                    result = {'stopReason': 'end_turn'}
                    send({'jsonrpc': '2.0', 'id': request['id'], 'result': result})
                    continue
                assert context['message']['trigger'] == 'review_return_candidate'
                assert 'finish_work' in names and 'delegate' in names
                assert not names.intersection({'reply_to_customer', 'workspace_write', 'workspace_run', 'publish_artifact', 'request_review'})
                if mode == 'reject':
                    send({'jsonrpc': '2.0', 'id': request['id'], 'error': {'code': -32000, 'message': 'Offline failure'}})
                    continue
                if mode == 'consult':
                    assert '普通咨询' in json.dumps(context, ensure_ascii=False)
                if mode == 'source':
                    sources = context['review_customer_inputs']
                    assert sources['complete'] is True
                    assert any('另外需要离线支持' in row['text'] for row in sources['entries'])
                    assert '不证明没有新要求' in sources['note']
                if mode in {'cross', 'unknown'}:
                    (root / 'peer-ready').write_text('ready', encoding='utf-8')
                    deadline = time.monotonic() + 8
                    while not (root / 'peer-release').exists() and time.monotonic() < deadline:
                        time.sleep(.02)
                    assert (root / 'peer-release').exists()
                if mode in {'finish', 'cross', 'unknown'}:
                    tool('finish_work', {'summary': 'Offline checked result', 'verification': 'Fixture only'})
                elif mode == 'delegate':
                    recipient = next(row['id'] for row in context['members'] if row['id'] != context['you']['id'])
                    tool('delegate', {'recipient': recipient, 'instruction': 'Continue the existing work'})
                    tool('wait_for_reply', {})
                elif mode == 'wait':
                    tool('wait_for_reply', {})
                elif mode == 'notify_wait':
                    recipient = next(row['id'] for row in context['members'] if row['id'] != context['you']['id'])
                    tool('send_message', {'recipient': recipient, 'text': 'Progress only', 'notify_only': True})
                    tool('wait_for_reply', {})
                else:
                    send({'jsonrpc': '2.0', 'method': 'session/update', 'params': {'sessionId': 'offline-review',
                        'update': {'sessionUpdate': 'agent_message_chunk',
                                   'content': {'type': 'text', 'text': 'Only an internal observation.'}}}})
                result = {'stopReason': 'end_turn'}
            else:
                continue
            send({'jsonrpc': '2.0', 'id': request['id'], 'result': result})
    finally:
        if relay is not None:
            relay.terminate()
            relay.wait(timeout=3)
            relay.stdin.close()
            relay.stdout.close()


if __name__ == '__main__':
    main()
