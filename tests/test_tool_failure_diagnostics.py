from __future__ import annotations

import io
import os
import socket
import threading
import traceback
import unittest
from http.client import BadStatusLine, IncompleteRead, RemoteDisconnected
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from cater.anthropic import AnthropicMessagesDriver
from cater.drivers import (MAX_API_RESPONSE_BYTES, TOOL_FAILURE_KINDS, TOOL_TRANSPORT_PHASES,
                           DriverError, DriverProtocolError, DriverUnavailable, OpenAICompatibleDriver,
                           TextRequestRejected, TextTransportFailure, ToolRequestRejected,
                           _tool_api_request, tool_failure_diagnostics)


PRIVATE = "PRIVATE_URL_PRIVATE_KEY_PRIVATE_HEADERS_PRIVATE_BODY"


class ToolFailureDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(socket, "socket", side_effect=AssertionError("Real network forbidden")))

    def invoke(self, opener, **options):
        params = {"endpoint": "https://fixture.invalid/v1/tools?private=" + PRIVATE,
                  "body": {"private": PRIVATE}, "headers": {"Authorization": "Bearer " + PRIVATE},
                  "name": PRIVATE, "timeout": 7, "opener": opener, "byte_limit": 128}
        params.update(options)
        return _tool_api_request(**params)

    def failure_for(self, error, *, phase="request", **options):
        stream = Mock()
        stream.__enter__ = Mock(return_value=stream)
        stream.__exit__ = Mock(return_value=False)
        stream.read.side_effect = error
        opener = Mock(return_value=stream, side_effect=error if phase == "request" else None)
        with self.assertRaises(DriverError) as caught:
            self.invoke(opener, **options)
        opener.assert_called_once()
        if phase == "request":
            stream.read.assert_not_called()
        else:
            stream.read.assert_called_once_with(options.get("byte_limit", 128) + 1)
        return caught.exception

    def assert_private(self, error):
        public = (str(error), repr(error), vars(error), tool_failure_diagnostics(error),
                  "".join(traceback.format_exception(error)))
        for value in public:
            self.assertNotIn(PRIVATE, str(value))
        self.assertNotIn("unbilled", str(error).lower())
        self.assertNotIn("not executed", str(error).lower())
        self.assertNotIn("not charged", str(error).lower())
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)
        self.assertTrue(set(vars(error)) <= {"status_code", "failure_kind", "transport_phase", "transport_issue"})
        self.assertNotIsInstance(error, (TextTransportFailure, TextRequestRejected))

    def test_http_statuses_have_safe_metadata_in_both_phases_without_body_or_header_access(self):
        class PrivateHeaders:
            def get(self, *args):
                raise AssertionError("HTTP failure headers must not be inspected")

            get_all = get

        for phase in TOOL_TRANSPORT_PHASES:
            for status in (100, 199, 200, 299, 301, 307, 400, 401, 402, 403, 408, 413, 429,
                           500, 501, 502, 503, 504, 599):
                with self.subTest(phase=phase, status=status):
                    body = Mock()
                    body.read.side_effect = AssertionError("HTTP failure body must not be read")
                    original = HTTPError("https://" + PRIVATE, status, PRIVATE, PrivateHeaders(), body)
                    error = self.failure_for(original, phase=phase)
                    self.assertIs(type(error), ToolRequestRejected if status == 429 else DriverUnavailable)
                    self.assertIsInstance(error, DriverUnavailable)
                    self.assertEqual(error.status_code, status)
                    self.assertEqual(tool_failure_diagnostics(error), {
                        "failure_kind": "http_error", "transport_phase": phase, "status_code": status})
                    self.assert_private(error)
                    body.read.assert_not_called()

    def test_invalid_http_statuses_are_never_coerced_or_formatted(self):
        class PrivateStatus:
            def __str__(self):
                raise AssertionError("Unknown HTTP status must not be formatted")

            def __eq__(self, other):
                raise AssertionError("Unknown HTTP status must not be compared")

        for status in (None, True, False, 99, 600, -1, 10 ** 400, 429.0, "429", PRIVATE, PrivateStatus()):
            original = HTTPError(PRIVATE, status, PRIVATE, {}, None)
            error = self.failure_for(original)
            self.assertIs(type(error), DriverUnavailable)
            self.assertFalse(hasattr(error, "status_code"))
            self.assertEqual(tool_failure_diagnostics(error), {"failure_kind": "http_error", "transport_phase": "request"})
            self.assert_private(error)

    def test_timeout_and_network_types_are_classified_without_reason_string_parsing(self):
        for phase in TOOL_TRANSPORT_PHASES:
            for original, kind, issue in ((TimeoutError(PRIVATE), "timeout", None),
                                   (socket.timeout(PRIVATE), "timeout", None),
                                   (URLError(TimeoutError(PRIVATE)), "timeout", None),
                                   (URLError("timed out " + PRIVATE), "network_error", None),
                                   (URLError(OSError(PRIVATE)), "network_error", None),
                                   (OSError(PRIVATE), "network_error", None),
                                   (ConnectionResetError(PRIVATE), "network_error", "connection_reset"),
                                   (RemoteDisconnected(PRIVATE), "network_error", "remote_disconnected"),
                                   (BadStatusLine(PRIVATE), "network_error", "http_protocol_error"),
                                   (IncompleteRead(PRIVATE.encode(), 999), "network_error", "incomplete_response")):
                with self.subTest(phase=phase, error_type=type(original).__name__):
                    error = self.failure_for(original, phase=phase)
                    self.assertIs(type(error), DriverUnavailable)
                    expected = {"failure_kind": kind, "transport_phase": phase}
                    if issue is not None:
                        expected["transport_issue"] = issue
                    self.assertEqual(tool_failure_diagnostics(error), expected)
                    self.assert_private(error)

    def test_arbitrary_reason_objects_are_not_stringified(self):
        class PrivateReason:
            def __str__(self):
                raise AssertionError("Transport reasons must not be formatted")

        error = self.failure_for(URLError(PrivateReason()))
        self.assertEqual(tool_failure_diagnostics(error)["failure_kind"], "network_error")
        self.assert_private(error)

    def test_request_and_read_argument_errors_are_sanitized_and_not_retried(self):
        for phase in TOOL_TRANSPORT_PHASES:
            for original in (ValueError(PRIVATE), TypeError(PRIVATE), UnicodeError(PRIVATE)):
                with self.subTest(phase=phase, error_type=type(original).__name__):
                    error = self.failure_for(original, phase=phase)
                    self.assertEqual(tool_failure_diagnostics(error), {"failure_kind": "invalid_request", "transport_phase": phase})
                    self.assert_private(error)

    def test_invalid_parameters_and_request_json_fail_before_opening(self):
        values = [("timeout", v) for v in (True, 0, -1, None, "7", float("nan"), float("inf"),
                                           threading.TIMEOUT_MAX + 1, 10 ** 100, 10 ** 400)]
        values += [("byte_limit", v) for v in (True, 0, -1, None, "128", 1.5, MAX_API_RESPONSE_BYTES + 1, 10 ** 400)]
        values += [("endpoint", v) for v in (None, 7, "https://[" + PRIVATE, "http://external.invalid",
                    "https://user:" + PRIVATE + "@fixture.invalid", "https://fixture.invalid/#" + PRIVATE,
                    "https://fixture.invalid:0", "https://fixture.invalid:65536", "https://fixture.invalid:private")]
        values += [("headers", v) for v in (None, [], {"Authorization": None}, {1: PRIVATE})]
        values += [("body", v) for v in (None, [], {"private": float("nan")}, {"private": object()})]
        circular = {}
        circular["private"] = circular
        values.append(("body", circular))
        for field, value in values:
            with self.subTest(field=field, value_type=type(value).__name__):
                opener = Mock()
                with self.assertRaises(DriverProtocolError) as caught:
                    self.invoke(opener, **{field: value})
                opener.assert_not_called()
                self.assertEqual(tool_failure_diagnostics(caught.exception), {"failure_kind": "invalid_request"})
                self.assert_private(caught.exception)

    def test_byte_limit_boundaries_success_and_protocol_errors_do_not_add_failure_metadata(self):
        for limit in (2, MAX_API_RESPONSE_BYTES):
            stream = Mock()
            stream.__enter__ = Mock(return_value=stream)
            stream.__exit__ = Mock(return_value=False)
            stream.read.return_value = b"{}"
            opener = Mock(return_value=stream)
            self.assertEqual(self.invoke(opener, byte_limit=limit), {})
            opener.assert_called_once()
            stream.read.assert_called_once_with(limit + 1)
        for raw in (b"{} ", b"PRIVATE", "{}", b"[]", b'{"a":1,"a":2}'):
            with self.subTest(raw_type=type(raw).__name__):
                with self.assertRaises(DriverProtocolError) as caught:
                    self.invoke(Mock(return_value=io.BytesIO(raw)) if isinstance(raw, bytes) else self.response(raw), byte_limit=2)
                self.assertEqual(tool_failure_diagnostics(caught.exception), {})
        opener = Mock(return_value=io.BytesIO(b"{}"))
        with self.assertRaises(DriverProtocolError):
            self.invoke(opener, byte_limit=1)
        opener.assert_called_once()

    def test_valid_timeout_boundaries_are_forwarded_once_without_retry(self):
        for timeout in (0.001, threading.TIMEOUT_MAX):
            opener = Mock(return_value=io.BytesIO(b"{}"))
            self.assertEqual(self.invoke(opener, timeout=timeout), {})
            opener.assert_called_once()
            self.assertEqual(opener.call_args.kwargs, {"timeout": timeout})

    @staticmethod
    def response(raw):
        stream = Mock()
        stream.__enter__ = Mock(return_value=stream)
        stream.__exit__ = Mock(return_value=False)
        stream.read.return_value = raw
        return Mock(return_value=stream)

    def test_missing_openai_credentials_are_classified_without_env_or_driver_names(self):
        driver = OpenAICompatibleDriver(PRIVATE, endpoint="https://fixture.invalid", model="fixture",
                                        api_key_env=PRIVATE)
        with patch.dict(os.environ, {}, clear=True), patch("cater.drivers._open_api_request") as opener:
            with self.assertRaises(DriverUnavailable) as caught:
                driver.tool_turn(role="worker", prompt="fixture", history=[], tools=[])
        self.assertIs(type(caught.exception), DriverUnavailable)
        self.assertEqual(tool_failure_diagnostics(caught.exception), {"failure_kind": "missing_credentials"})
        self.assert_private(caught.exception)
        opener.assert_not_called()

    def test_whitelist_excludes_strings_objects_unknown_enums_and_text_driver_errors(self):
        for error in (DriverUnavailable(PRIVATE), DriverProtocolError(PRIVATE),
                      TextTransportFailure(PRIVATE), TextRequestRejected(PRIVATE, status_code=429),
                      ToolRequestRejected(PRIVATE, status_code=429), RuntimeError(PRIVATE)):
            self.assertEqual(tool_failure_diagnostics(error), {})
        for invalid in (None, True, [], {}, PRIVATE, 429):
            error = DriverUnavailable(PRIVATE)
            error.failure_kind = invalid
            self.assertEqual(tool_failure_diagnostics(error), {})
        for kind in TOOL_FAILURE_KINDS:
            error = DriverUnavailable(PRIVATE)
            error.failure_kind = kind
            error.transport_phase = PRIVATE
            error.status_code = 429
            error.headers = {"Authorization": PRIVATE}
            error.url = PRIVATE
            expected = {"failure_kind": kind}
            if kind == "http_error":
                expected["status_code"] = 429
            self.assertEqual(tool_failure_diagnostics(error), expected)
        for invalid in (None, True, [], PRIVATE, 99, 600, "429", 429.0):
            error = DriverUnavailable(PRIVATE)
            error.failure_kind = "http_error"
            error.transport_phase = "response_read"
            error.status_code = invalid
            self.assertEqual(tool_failure_diagnostics(error), {"failure_kind": "http_error", "transport_phase": "response_read"})

    def test_both_tool_transports_expose_diagnostics_even_without_tool_declarations(self):
        with patch.dict(os.environ, {"CATER_DIAGNOSTIC_FIXTURE": PRIVATE}):
            for cls, transport in ((OpenAICompatibleDriver, "cater.drivers._open_api_request"),
                                   (AnthropicMessagesDriver, "cater.anthropic._open_api_request")):
                driver = cls("fixture", endpoint="https://fixture.invalid/v1/messages", model="fixture",
                             api_key_env="CATER_DIAGNOSTIC_FIXTURE")
                for original, kind in ((HTTPError(PRIVATE, 402, PRIVATE, {}, None), "http_error"),
                                       (TimeoutError(PRIVATE), "timeout")):
                    with self.subTest(driver=cls.__name__, kind=kind), patch(transport, side_effect=original) as opened:
                        with self.assertRaises(DriverUnavailable) as caught:
                            driver.tool_turn(role="worker", prompt="fixture", history=[], tools=[])
                    self.assertEqual(tool_failure_diagnostics(caught.exception)["failure_kind"], kind)
                    self.assert_private(caught.exception)
                    opened.assert_called_once()


if __name__ == "__main__":
    unittest.main()
