"""Exercise the real API example over local HTTP, using fixture tokens only."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import io
import json
from pathlib import Path
import threading
import unittest
from urllib.error import HTTPError
from urllib.response import addinfourl
from email.message import Message
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('cater_api_example_under_test', ROOT / 'examples/api_client.py')
client = importlib.util.module_from_spec(spec)
spec.loader.exec_module(client)


@contextmanager
def server(target=None, code=302, *, status=200):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def handle_request(self):
            length = int(self.headers.get('Content-Length', '0'))
            body = self.rfile.read(length) if length else b''
            requests.append({'method': self.command, 'token': self.headers.get('X-Cater-Access-Token'),
                             'body': body, 'path': self.path})
            if target is not None:
                self.send_response(code)
                self.send_header('Location', target)
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            result = json.dumps({'ok': True, 'method': self.command,
                                 'body': json.loads(body) if body else None}).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(result)))
            self.end_headers()
            self.wfile.write(result)

        do_GET = do_POST = handle_request

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(('127.0.0.1', 0), Handler) as http:
        thread = threading.Thread(target=http.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
        thread.start()
        try:
            yield f'http://127.0.0.1:{http.server_port}', requests
        finally:
            http.shutdown()
            thread.join(5)
            assert not thread.is_alive()


class ApiExampleRedirectTests(unittest.TestCase):
    def setUp(self):
        # Empty environment settings can still fall back to Windows registry proxies.
        patcher = patch('urllib.request.getproxies', return_value={})
        patcher.start()
        self.addCleanup(patcher.stop)
        import urllib.request
        opener = patch.object(urllib.request, '_opener', None)
        opener.start()
        self.addCleanup(opener.stop)

    def test_normal_get_and_post_keep_json_and_authentication(self):
        with server() as (base, calls):
            self.assertEqual(client.get_json(base, '/api/chain', access_token='fixture-only-token'),
                             {'ok': True, 'method': 'GET', 'body': None})
            self.assertEqual(client.post_json(base, '/api/tasks', {'goal': '中文'}, access_token='fixture-only-token'),
                             {'ok': True, 'method': 'POST', 'body': {'goal': '中文'}})
            self.assertEqual([row['token'] for row in calls], ['fixture-only-token'] * 2)

    def test_get_and_post_reject_redirects_without_contacting_destination(self):
        for method in ('GET', 'POST'):
            for code in (301, 302, 303, 307, 308):
                with self.subTest(method=method, code=code), server() as (sink, received):
                    with server(sink + '/untrusted', code) as (base, original):
                        with self.assertRaises(HTTPError) as raised:
                            if method == 'GET':
                                client.get_json(base, '/api/chain', access_token='fixture-only-token')
                            else:
                                client.post_json(base, '/api/tasks', {'goal': 'Private fixture'}, access_token='fixture-only-token')
                        self.assertEqual(raised.exception.code, code)
                        self.assertEqual(len(original), 1)
                        self.assertEqual(received, [], 'No request or token may reach the redirect destination')


    def test_https_response_processing_rejects_redirect_before_reaching_http_target(self):
        # Exercise the real HTTPS response chain; only TLS transport is substituted.
        # This catches overriding http_response while inheriting the HTTPS alias.
        for method in ('GET', 'POST'):
            with self.subTest(method=method), server() as (sink, received):
                headers = Message()
                headers['Location'] = sink + '/untrusted'
                response = addinfourl(io.BytesIO(b''), headers, 'https://source.invalid/api', 302)
                response.msg = 'Found'
                with patch('urllib.request.HTTPSHandler.https_open', return_value=response):
                    with self.assertRaises(HTTPError) as raised:
                        if method == 'GET':
                            client.get_json('https://source.invalid', '/api', access_token='fixture-only-token')
                        else:
                            client.post_json('https://source.invalid', '/api', {'private': 'fixture'},
                                             access_token='fixture-only-token')
                self.assertEqual(raised.exception.code, 302)
                self.assertEqual(received, [])

    def test_http_errors_keep_original_status_headers_and_readable_body(self):
        for method in ('GET', 'POST'):
            with self.subTest(method=method), server(status=404) as (base, calls):
                with self.assertRaises(HTTPError) as raised:
                    if method == 'GET':
                        client.get_json(base, '/missing')
                    else:
                        client.post_json(base, '/missing', {'fixture': True})
                error = raised.exception
                self.assertEqual(error.code, 404)
                self.assertEqual(error.reason, 'Not Found')
                self.assertEqual(error.headers['Content-Type'], 'application/json')
                self.assertEqual(json.loads(error.read())['method'], method)
                error.close()
                self.assertEqual(len(calls), 1)


if __name__ == '__main__':
    unittest.main()
