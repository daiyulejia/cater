from __future__ import annotations

import http.client
import io
import socket
import threading
import time
import unittest
from email.message import Message
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler

from cater import web


class RecordingConnection:
    def __init__(self):
        self.timeout = 7.0
        self.timeouts = []

    def gettimeout(self):
        return self.timeout

    def settimeout(self, value):
        self.timeouts.append(value)
        self.timeout = value


class RecordingBody(io.BytesIO):
    def __init__(self, body, *, chunk_size=None, on_read=None):
        super().__init__(body)
        self.calls = []
        self.chunk_size = chunk_size
        self.on_read = on_read

    def read(self, size=-1):
        if size < 0:
            raise AssertionError("unbounded rejection read")
        self.calls.append(size)
        if self.on_read:
            self.on_read()
        return super().read(size)

    def read1(self, size=-1):
        return self.read(min(size, self.chunk_size) if self.chunk_size else size)


def handler_type(**options):
    # No database, model, driver, or recovery work is needed by these paths.
    with patch.object(web, "ActivityRunner"):
        return web.make_handler("unused-rejection-test.db", **options)


class RejectionBodyTests(unittest.TestCase):
    def handler(self, body=b"invalid JSON", *, length=None, headers=None, **options):
        cls = handler_type(**options)
        handler = cls.__new__(cls)
        handler.command = "POST"
        handler.path = "/api/rooms/missing/run"
        handler.request_version = "HTTP/1.1"
        handler.requestline = "POST /api/rooms/missing/run HTTP/1.1"
        handler.close_connection = True
        handler._request_body_handled = False
        handler.headers = Message()
        handler.headers["Host"] = "127.0.0.1"
        handler.headers["Content-Length"] = str(len(body) if length is None else length)
        for name, value in (headers or {}).items():
            handler.headers[name] = value
        handler.connection = RecordingConnection()
        handler.rfile = RecordingBody(body)
        handler.wfile = io.BytesIO()
        handler.log_request = lambda *args: None
        return handler

    def test_early_rejections_consume_small_body_before_response_without_parsing(self):
        cases = (
            ({"access_token": "test-token"}, {}, 401),
            ({"access_token": "test-token"},
             {"Authorization": "Bearer test-token", "Origin": "https://foreign.invalid"}, 403),
            ({}, {"Origin": "https://foreign.invalid"}, 403),
            ({}, {}, 404),
        )
        for options, headers, status in cases:
            with self.subTest(status=status, headers=headers):
                handler = self.handler(headers=headers, **options)
                body = handler.rfile

                class Response(io.BytesIO):
                    def write(output, raw):
                        self.assertEqual(body.tell(), len(body.getvalue()),
                                         "response sent while rejected upload is unread")
                        return super().write(raw)

                handler.wfile = Response()
                with patch.object(type(handler), "_read_json", side_effect=AssertionError("parsed")):
                    handler.do_POST()
                self.assertIn(f" {status} ".encode(), handler.wfile.getvalue())
                self.assertEqual(handler.connection.timeout, 7.0)

    def test_untrusted_host_also_drains_before_403(self):
        handler = self.handler()
        handler.headers.replace_header("Host", "foreign.invalid")
        handler.do_POST()
        self.assertEqual(handler.rfile.tell(), len(handler.rfile.getvalue()))
        self.assertIn(b" 403 ", handler.wfile.getvalue())

    def test_consumed_bodies_are_not_read_again_for_errors(self):
        for body in (b"{}", b"{", b"[]", b"\xff"):
            with self.subTest(body=body):
                handler = self.handler(body)
                handler._read_json()
                handler._bad_request("bad")
                handler._not_found()
                handler._conflict("conflict")
                handler.send_error(405)
                self.assertEqual(handler.rfile.calls, [len(body)])
                self.assertEqual(handler.connection.timeouts, [])

    def test_consumed_form_is_not_read_again_for_errors(self):
        handler = self.handler(b"name=value")
        self.assertEqual(handler._read_form(), {"name": ["value"]})
        handler._forbidden("denied")
        self.assertEqual(handler.rfile.calls, [10])
        self.assertEqual(handler.connection.timeouts, [])

    def test_next_post_resets_body_state(self):
        handler = self.handler(b"{}")
        handler._read_json()
        handler.rfile = RecordingBody(b"next")
        handler.headers.replace_header("Content-Length", "4")
        handler.do_POST()
        self.assertEqual(handler.rfile.calls, [4])

    def test_invalid_length_is_not_read_again_by_error_response(self):
        handler = self.handler(length="invalid")
        self.assertIsNone(handler._read_body())
        self.assertEqual(handler.rfile.calls, [])
        self.assertEqual(handler.connection.timeouts, [])

    def test_early_drain_has_a_strict_byte_limit_and_valid_length(self):
        for length in (0, -1, "bad", "9" * 5000, web.MAX_REJECT_DRAIN_BYTES + 1,
                       web.MAX_REQUEST_BYTES + 1):
            with self.subTest(length=str(length)[:30]):
                handler = self.handler(length=length)
                handler.do_POST()
                self.assertEqual(handler.rfile.calls, [])
                self.assertEqual(handler.connection.timeouts, [])
                self.assertIn(b" 404 ", handler.wfile.getvalue())
        handler = self.handler(b"x" * web.MAX_REJECT_DRAIN_BYTES)
        handler.do_POST()
        self.assertEqual(handler.rfile.tell(), web.MAX_REJECT_DRAIN_BYTES)
        self.assertLessEqual(max(handler.rfile.calls), web.MAX_REJECT_DRAIN_BYTES)

    def test_ambiguous_framing_is_not_drained(self):
        for extra in ({"Content-Length": "2"}, {"Transfer-Encoding": "chunked"}):
            with self.subTest(extra=extra):
                handler = self.handler(headers=extra)
                handler.do_POST()
                self.assertEqual(handler.rfile.calls, [])

    def test_over_limit_drain_preserves_existing_cap_and_never_reads_negative_length(self):
        for length, expected in ((-1, 0), (web.MAX_REQUEST_BYTES + 1, web.MAX_REQUEST_BYTES + 1),
                                 (web.MAX_REQUEST_BYTES + web.MAX_REJECT_DRAIN_BYTES,
                                  web.MAX_REQUEST_BYTES + web.MAX_REJECT_DRAIN_BYTES),
                                 (web.MAX_REQUEST_BYTES + web.MAX_REJECT_DRAIN_BYTES + 1, 0)):
            with self.subTest(length=length):
                handler = self.handler(b"x" * max(expected, 1), length=length)
                self.assertIsNone(handler._read_body())
                self.assertEqual(handler.rfile.tell(), expected)
                self.assertTrue(handler.close_connection)
                self.assertIn(b" 400 ", handler.wfile.getvalue())
                self.assertTrue(all(size <= web.MAX_REJECT_DRAIN_BYTES for size in handler.rfile.calls))

    def test_drain_uses_total_deadline_even_when_chunks_keep_arriving(self):
        for early in (True, False):
            with self.subTest(early=early):
                length = 10 if early else web.MAX_REQUEST_BYTES + 1
                handler = self.handler(b"x" * 10, length=length)
                clock = [0.0]

                def advance():
                    clock[0] += web.REJECT_DRAIN_TIMEOUT_SECONDS * 0.6

                handler.rfile = RecordingBody(b"x" * 10, chunk_size=1, on_read=advance)
                with patch.object(web.time, "monotonic", side_effect=lambda: clock[0]):
                    if early:
                        handler.do_POST()
                    else:
                        handler._read_body()
                self.assertEqual(len(handler.rfile.calls), 2)
                self.assertEqual(handler.connection.timeout, 7.0)
                self.assertTrue(handler.close_connection)
                self.assertLess(handler.connection.timeouts[1], handler.connection.timeouts[0])

    def test_timeout_or_disconnect_does_not_retry_or_replace_rejection(self):
        for error in (TimeoutError("slow"), ConnectionResetError("gone")):
            with self.subTest(error=error):
                handler = self.handler()

                def fail():
                    raise error

                handler.rfile = RecordingBody(b"x", on_read=fail)
                handler.do_POST()
                handler._not_found()
                self.assertEqual(len(handler.rfile.calls), 1)
                self.assertEqual(handler.connection.timeout, 7.0)
                self.assertTrue(handler.close_connection)
                self.assertIn(b" 404 ", handler.wfile.getvalue())


class RejectionLoopbackTests(unittest.TestCase):
    def setUp(self):
        cls = handler_type(access_token="test-token")
        self.finished = threading.Event()
        self.drain_started = threading.Event()
        self.body_reads = []
        finished = self.finished
        drain_started = self.drain_started
        body_reads = self.body_reads

        class Reader:
            def __init__(self, stream):
                self.stream = stream

            def __getattr__(self, name):
                return getattr(self.stream, name)

            def read(self, size):
                body_reads.append(("read", size))
                return self.stream.read(size)

            def read1(self, size):
                body_reads.append(("read1", size))
                drain_started.set()
                return self.stream.read1(size)

        class Handler(cls):
            def setup(self):
                super().setup()
                self.rfile = Reader(self.rfile)

            def _read_json(self):
                if self.path.endswith("/run"):
                    raise AssertionError("early rejection parsed JSON")
                return super()._read_json()

            def log_message(self, *args):
                pass

            def finish(self):
                try:
                    super().finish()
                finally:
                    finished.set()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = False
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.01})
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.thread.join, 2)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def test_urllib_early_401_403_and_room_run_404(self):
        opener = build_opener(ProxyHandler({}))
        cases = (({}, 401),
                 ({"Authorization": "Bearer test-token", "Origin": "https://foreign.invalid"}, 403),
                 ({"Authorization": "Bearer test-token"}, 404))
        for headers, status in cases:
            with self.subTest(status=status):
                request = Request(self.base + "/api/rooms/missing/run", data=b"not JSON",
                                  headers=headers, method="POST")
                with self.assertRaises(HTTPError) as raised:
                    opener.open(request, timeout=2)
                with raised.exception as response:
                    self.assertEqual(response.status, status)
                    self.assertEqual(len(response.read()), int(response.headers["Content-Length"]))
                    self.assertEqual(response.headers["Cache-Control"], "no-store")
                    self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")

    def test_small_body_arriving_after_headers_is_consumed_before_response(self):
        with socket.create_connection(self.server.server_address, timeout=2) as client:
            client.sendall(b"POST /api/rooms/missing/run HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                           b"Content-Length: 4\r\n\r\n")
            self.assertTrue(self.drain_started.wait(1), "response bypassed body drain")
            client.sendall(b"nope")
            response = http.client.HTTPResponse(client)
            response.begin()
            self.assertEqual(response.status, 401)
            self.assertEqual(response.read(), b"access token is required")

    def test_consumed_invalid_json_body_is_not_drained_again(self):
        opener = build_opener(ProxyHandler({}))
        request = Request(self.base + "/api/rooms", data=b"{",
                          headers={"Authorization": "Bearer test-token"}, method="POST")
        with self.assertRaises(HTTPError) as raised:
            opener.open(request, timeout=2)
        with raised.exception as response:
            self.assertEqual(response.status, 400)
            self.assertEqual(response.read(), b"invalid JSON body")
        self.assertTrue(self.finished.wait(1))
        self.assertEqual(self.body_reads, [("read", 1)])

    def test_missing_small_body_is_time_bounded_and_large_body_is_not_awaited(self):
        for length in (4, web.MAX_REJECT_DRAIN_BYTES + 1):
            with self.subTest(length=length):
                self.finished.clear()
                with socket.create_connection(self.server.server_address, timeout=2) as client:
                    start = time.monotonic()
                    client.sendall(("POST /api/rooms/missing/run HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                                    f"Content-Length: {length}\r\n\r\n").encode("ascii"))
                    response = http.client.HTTPResponse(client)
                    response.begin()
                    self.assertEqual(response.status, 401)
                    self.assertEqual(response.read(), b"access token is required")
                    self.assertLess(time.monotonic() - start, 1.5)
                self.assertTrue(self.finished.wait(1))


if __name__ == "__main__":
    unittest.main()
