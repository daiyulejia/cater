from __future__ import annotations

import socket
import ssl
import traceback
import unittest
from http.client import BadStatusLine, HTTPException, IncompleteRead, RemoteDisconnected
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from cater.drivers import (TOOL_FAILURE_KINDS, TOOL_TRANSPORT_ISSUES, TOOL_TRANSPORT_PHASES,
                           DriverUnavailable, _tool_api_request, tool_failure_diagnostics)


PRIVATE = "PRIVATE_TRANSPORT_HOST_URL_KEY_HEADERS_BODY"
ISSUES = {
    "dns_lookup_failed", "tls_certificate_error", "tls_error", "connection_refused",
    "connection_reset", "broken_pipe", "remote_disconnected", "incomplete_response", "http_protocol_error",
}


class Uninspectable:
    def __str__(self):
        raise AssertionError("Private value must not be formatted")

    __repr__ = __str__

    def __eq__(self, other):
        raise AssertionError("Private value must not be compared")

    def __hash__(self):
        raise AssertionError("Private value must not be hashed")


class PrivateException(Exception, Uninspectable):
    __str__ = Uninspectable.__str__
    __repr__ = Uninspectable.__repr__

    @property
    def errno(self):
        raise AssertionError("errno must not be inspected")

    @property
    def reason(self):
        raise AssertionError("Nested reason must not be inspected")


class TransportIssueTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(socket, "socket", side_effect=AssertionError("Real network forbidden")))

    def failure_for(self, original, phase):
        stream = Mock()
        stream.__enter__ = Mock(return_value=stream)
        stream.__exit__ = Mock(return_value=False)
        stream.read.side_effect = original
        opener = Mock(return_value=stream, side_effect=original if phase == "request" else None)
        try:
            _tool_api_request(endpoint="https://fixture.invalid/" + PRIVATE,
                              body={"private": PRIVATE}, headers={"Authorization": PRIVATE},
                              name=PRIVATE, timeout=7, opener=opener, byte_limit=128)
        except DriverUnavailable as error:
            opener.assert_called_once()
            if phase == "response_read":
                stream.read.assert_called_once_with(129)
            else:
                stream.read.assert_not_called()
            self.assertIsNone(error.__context__)
            self.assertIsNone(error.__cause__)
            self.assertTrue(set(vars(error)) <= {
                "failure_kind", "transport_phase", "transport_issue", "status_code"})
            for value in (str(error), repr(error), repr(vars(error)),
                          repr(tool_failure_diagnostics(error)), "".join(traceback.format_exception(error))):
                self.assertNotIn(PRIVATE, value)
            return error
        self.fail("Expected a sanitized transport failure")

    def test_known_classes_in_both_phases_direct_and_url_wrapped(self):
        cases = (
            (socket.gaierror(PRIVATE), "dns_lookup_failed"),
            (ssl.SSLCertVerificationError(PRIVATE), "tls_certificate_error"),
            (ssl.CertificateError(PRIVATE), "tls_certificate_error"),
            (ssl.SSLError(PRIVATE), "tls_error"),
            (ssl.SSLEOFError(PRIVATE), "tls_error"),
            (ConnectionRefusedError(PRIVATE), "connection_refused"),
            (ConnectionResetError(PRIVATE), "connection_reset"),
            (BrokenPipeError(PRIVATE), "broken_pipe"),
            (RemoteDisconnected(PRIVATE), "remote_disconnected"),
            (IncompleteRead(PRIVATE.encode(), 999), "incomplete_response"),
            (HTTPException(PRIVATE), "http_protocol_error"),
            (BadStatusLine(PRIVATE), "http_protocol_error"),
        )
        for phase in TOOL_TRANSPORT_PHASES:
            for original, issue in cases:
                # Raw transport details and exception chains must never be copied.
                original.original = PRIVATE
                original.__cause__ = RuntimeError(PRIVATE)
                original.__context__ = RuntimeError(PRIVATE)
                for wrapped in (False, True):
                    with self.subTest(phase=phase, cls=type(original).__name__, wrapped=wrapped):
                        error = self.failure_for(URLError(original) if wrapped else original, phase)
                        expected = {"failure_kind": "network_error", "transport_phase": phase,
                                    "transport_issue": issue}
                        self.assertEqual(tool_failure_diagnostics(error), expected)
                        self.assertEqual(vars(error), expected)

    def test_unknown_legacy_reasons_do_not_infer_from_text_errno_or_attributes(self):
        generic = OSError(PRIVATE)
        generic.errno = 10061
        generic.transport_issue = "connection_refused"
        generic.__cause__ = ConnectionRefusedError(PRIVATE)
        generic.__context__ = ssl.SSLError(PRIVATE)
        for phase in TOOL_TRANSPORT_PHASES:
            for original in (generic, socket.herror(PRIVATE), ConnectionAbortedError(PRIVATE),
                             URLError(generic), URLError("connection refused TLS DNS timed out " + PRIVATE),
                             URLError("dns_lookup_failed"), URLError(10061), URLError(None),
                             URLError(Uninspectable()), URLError(PrivateException(PRIVATE)),
                             URLError({"reason": ConnectionResetError(PRIVATE)}),
                             URLError(URLError(ConnectionResetError(PRIVATE)))):
                with self.subTest(phase=phase, cls=type(original).__name__):
                    error = self.failure_for(original, phase)
                    expected = {"failure_kind": "network_error", "transport_phase": phase}
                    self.assertEqual(tool_failure_diagnostics(error), expected)
                    self.assertEqual(vars(error), expected)

    def test_timeout_http_and_invalid_request_keep_their_classification(self):
        for phase in TOOL_TRANSPORT_PHASES:
            for original, kind in (
                (TimeoutError(PRIVATE), "timeout"),
                (socket.timeout(PRIVATE), "timeout"),
                (URLError(TimeoutError(PRIVATE)), "timeout"),
                (HTTPError(PRIVATE, 503, PRIVATE, {}, None), "http_error"),
                (ValueError(PRIVATE), "invalid_request"),
            ):
                original.transport_issue = "connection_reset"
                with self.subTest(phase=phase, kind=kind):
                    error = self.failure_for(original, phase)
                    expected = {"failure_kind": kind, "transport_phase": phase}
                    if kind == "http_error":
                        expected["status_code"] = 503
                    self.assertEqual(tool_failure_diagnostics(error), expected)
                    self.assertNotIn("transport_issue", vars(error))

    def test_diagnostics_require_exact_whitelist_and_network_kind(self):
        self.assertEqual(TOOL_TRANSPORT_ISSUES, ISSUES)

        class StringSubclass(str):
            pass

        for kind in TOOL_FAILURE_KINDS:
            for issue in (*ISSUES, None, True, 1, [], {}, Uninspectable(),
                          StringSubclass("connection_reset"), "CONNECTION_RESET", "unknown", PRIVATE):
                error = DriverUnavailable("Safe message")
                error.failure_kind = kind
                error.transport_issue = issue
                error.status_code = 503
                expected = {"failure_kind": kind}
                if kind == "http_error":
                    expected["status_code"] = 503
                if kind == "network_error" and type(issue) is str and issue in ISSUES:
                    expected["transport_issue"] = issue
                self.assertEqual(tool_failure_diagnostics(error), expected)
        legacy = DriverUnavailable("ConnectionResetError connection refused timed out")
        self.assertEqual(tool_failure_diagnostics(legacy), {})
        legacy.failure_kind = "network_error"
        legacy.transport_phase = "request"
        self.assertEqual(tool_failure_diagnostics(legacy), {
            "failure_kind": "network_error", "transport_phase": "request"})


if __name__ == "__main__":
    unittest.main()
