from __future__ import annotations

import io
import json
import math
import os
import socket
import traceback
import unittest
from datetime import datetime, timedelta, timezone
from email.message import Message
from email.utils import format_datetime
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from cater.anthropic import AnthropicMessagesDriver
from cater.drivers import (MAX_RETRY_AFTER_SECONDS, DriverUnavailable, OpenAICompatibleDriver,
                           TextRequestRejected, TextTransportFailure, ToolRequestRejected)


DRIVERS = (OpenAICompatibleDriver, AnthropicMessagesDriver)
STATUSES = (429, 408, 500, 502, 503, 504)
NOW = datetime(2026, 10, 15, tzinfo=timezone.utc)
SECRET = "SYNTHETIC_PRIVATE_KEY"
PRIVATE_URL = "https://private.invalid/?key=" + SECRET
PRIVATE_BODY = "SYNTHETIC_PRIVATE_BODY"


class BrokenHeaders:
    def __init__(self, error):
        self.error = error

    def get(self, name):
        raise self.error


class BrokenMultiHeaders(BrokenHeaders):
    def get_all(self, name):
        raise self.error


class BrokenHeaderProperty:
    @property
    def get_all(self):
        raise RuntimeError(PRIVATE_URL)


class PrivateValue:
    def __str__(self):
        raise RuntimeError(PRIVATE_URL)


def headers(value):
    message = Message()
    message["rEtRy-AfTeR"] = value
    return message


class RetryAfterTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {"CATER_RETRY_FIXTURE": SECRET}))
        self.enterContext(patch.object(socket, "socket", side_effect=AssertionError("Real network forbidden")))
        self.enterContext(patch("cater.drivers.time.time", return_value=NOW.timestamp()))
        self.enterContext(patch("cater.drivers.time.sleep", side_effect=AssertionError("Unexpected retry sleep")))

    def driver(self, cls):
        return cls("fixture", endpoint="https://fixture.invalid/v1/messages", model="fixture",
                   api_key_env="CATER_RETRY_FIXTURE", timeout=7)

    def invoke(self, cls, header_values, *, status=429, read=False, tool=False, error=None):
        body = io.BytesIO(PRIVATE_BODY.encode())
        if error is None:
            error = HTTPError(PRIVATE_URL, status, SECRET, header_values, body)
        opener = Mock()
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.side_effect = error
        opener.open.side_effect = None if read else error
        opener.open.return_value = response
        with patch("cater.drivers.build_opener", return_value=opener):
            try:
                driver = self.driver(cls)
                if tool:
                    driver.tool_turn(role="worker", prompt="synthetic", history=[], tools=[])
                else:
                    driver.complete(role="worker", prompt="synthetic", context={})
            except DriverUnavailable as caught:
                failure = caught
                rendered = "".join(traceback.format_exception(caught))
            else:
                self.fail("Expected transport failure")
        opener.open.assert_called_once()
        request = opener.open.call_args.args[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(opener.open.call_args.kwargs, {"timeout": 7})
        self.assertNotIn("tools", json.loads(request.data))
        key_header = "X-api-key" if cls is AnthropicMessagesDriver else "Authorization"
        self.assertIn(SECRET, request.get_header(key_header))
        self.assertEqual(body.tell(), 0)
        if read:
            response.read.assert_called_once()
        for private in (SECRET, PRIVATE_BODY, "private.invalid"):
            self.assertNotIn(private, str(failure))
            self.assertNotIn(private, repr(failure))
            self.assertNotIn(private, repr(vars(failure)))
            self.assertNotIn(private, rendered)
        self.assertIsNone(failure.__cause__)
        self.assertTrue(failure.__suppress_context__)
        return failure

    def assert_delay(self, failure, status, expected):
        self.assertIs(type(failure), TextRequestRejected if status == 429 else TextTransportFailure)
        if status == 429:
            self.assertEqual(failure.status_code, 429)
        self.assertEqual(failure.retry_after_seconds, expected)
        if expected is not None:
            self.assertIs(type(failure.retry_after_seconds), float)
            self.assertTrue(math.isfinite(failure.retry_after_seconds))
            self.assertGreaterEqual(failure.retry_after_seconds, 0)

    def test_original_constructors_remain_compatible(self):
        for args in ((), ("lost response",), ("lost response", 7)):
            failure = TextTransportFailure(*args)
            self.assertEqual(failure.args, args)
            self.assertIsNone(failure.retry_after_seconds)
        failure = TextRequestRejected("refused", status_code=429)
        self.assertEqual(failure.args, ("refused",))
        self.assertEqual(failure.status_code, 429)
        self.assertIsNone(failure.retry_after_seconds)

    def test_constructor_metadata_is_sanitized_without_changing_message(self):
        for cls in (TextTransportFailure, TextRequestRejected):
            options = {"status_code": 429} if cls is TextRequestRejected else {}
            for value, expected in ((0, 0.0), (1.5, 1.5), (3600, 3600.0),
                                    (None, None), (True, None), (-1, None),
                                    (float("nan"), None), (float("inf"), None),
                                    (float("-inf"), None), (-10**1000, None),
                                    (2**54, float(MAX_RETRY_AFTER_SECONDS)),
                                    (10**1000, float(MAX_RETRY_AFTER_SECONDS)),
                                    (1.7976931348623157e308, float(MAX_RETRY_AFTER_SECONDS)),
                                    (PRIVATE_URL, None), (PrivateValue(), None)):
                with self.subTest(cls=cls.__name__, value_type=type(value).__name__):
                    failure = cls("safe", retry_after_seconds=value, **options)
                    self.assertEqual(failure.retry_after_seconds, expected)
                    self.assertEqual(str(failure), "safe")
                    self.assertEqual(failure.args, ("safe",))

    def test_integer_headers_through_both_transports_and_all_statuses(self):
        cases = (("0", 0.0), ("007", 7.0), (" \t120\t ", 120.0),
                 ("3600", 3600.0), ("86400", 86400.0),
                 ("9007199254740991", float(2**53 - 1)))
        for cls in DRIVERS:
            for status in STATUSES:
                for value, expected in cases:
                    with self.subTest(cls=cls.__name__, status=status, value=value):
                        self.assert_delay(self.invoke(cls, headers(value), status=status), status, expected)

    def test_huge_legal_delays_saturate_above_host_automatic_wait_limit(self):
        cases = (str(MAX_RETRY_AFTER_SECONDS + 1), str(2**54), "9" * 128)
        for cls in DRIVERS:
            for status in STATUSES:
                for read in (False, True):
                    for value in cases:
                        with self.subTest(cls=cls.__name__, status=status, read=read, value=value):
                            failure = self.invoke(cls, headers(value), status=status, read=read)
                            self.assert_delay(failure, status, float(MAX_RETRY_AFTER_SECONDS))
                            self.assertGreater(failure.retry_after_seconds, 120)

    def test_http_dates_use_utc_and_preserve_long_delays_and_past_zero(self):
        future = NOW + timedelta(seconds=3600)
        cases = ((format_datetime(future, usegmt=True), 3600.0),
                 ("Thursday, 15-Oct-26 01:00:00 GMT", 3600.0),
                 ("Thu Oct 15 01:00:00 2026", 3600.0),
                 ("Sun Nov  1 00:00:00 2026", 17 * 86400.0),
                 (format_datetime(NOW, usegmt=True), 0.0),
                 (format_datetime(NOW - timedelta(seconds=1), usegmt=True), 0.0),
                 ("Mon, 01 Jan 1900 00:00:00 GMT", 0.0),
                 ("Fri, 31 Dec 9999 23:59:59 GMT",
                  (datetime(9999, 12, 31, 23, 59, 59, tzinfo=timezone.utc) - NOW).total_seconds()))
        for cls in DRIVERS:
            for status in STATUSES:
                for value, expected in cases:
                    with self.subTest(cls=cls.__name__, status=status, value=value):
                        self.assert_delay(self.invoke(cls, headers(value), status=status), status, expected)

    def test_invalid_values_are_ignored_without_leaks_or_reclassification(self):
        date = format_datetime(NOW, usegmt=True)
        invalid = ("", " ", "NaN", "nan", "Infinity", "inf", "-1", "+1", "1.0", "1e3",
                   "1_000", "0x10", "\u0661", "\uff11", "1, 2", "1\r\nRetry-After: 0", "1\n", "\x001",
                   "9" * 10000, "0" * 129, "untrusted text", PRIVATE_URL,
                   date + " " + PRIVATE_URL, date + " (" + SECRET + ")",
                   "Thu, 15 Oct 2026 01:00:00 +0000", "Thu, 15 Oct 2026 01:00:00 PST",
                   "Thu, 15 Oct 2026 01:00:00", "Thu, 15 Oct 2026 24:00:00 GMT",
                   "Thu, 30 Feb 2026 01:00:00 GMT", "2026-10-15T01:00:00Z")
        for cls in DRIVERS:
            for status in STATUSES:
                for index, value in enumerate(invalid):
                    with self.subTest(cls=cls.__name__, status=status, index=index):
                        self.assert_delay(self.invoke(cls, {"Retry-After": value}, status=status), status, None)

    def test_missing_duplicate_wrong_type_and_broken_headers_preserve_http_classification(self):
        duplicate = headers("1")
        duplicate["Retry-After"] = "3600"
        errors = [ValueError(PRIVATE_URL), TypeError(SECRET), OverflowError(PRIVATE_BODY),
                  OSError(PRIVATE_URL), RuntimeError(SECRET)]
        cases = [None, {}, Message(), duplicate, {"Retry-After": 30}, {"Retry-After": True},
                 {"Retry-After": b"30"}, {"Retry-After": ["30"]}, {"Retry-After": PrivateValue()},
                 BrokenHeaderProperty(), *[BrokenHeaders(error) for error in errors],
                 *[BrokenMultiHeaders(error) for error in errors]]
        for cls in DRIVERS:
            for status in STATUSES:
                for index, value in enumerate(cases):
                    with self.subTest(cls=cls.__name__, status=status, index=index):
                        self.assert_delay(self.invoke(cls, value, status=status), status, None)

    def test_date_parser_failure_cannot_replace_http_classification(self):
        for cls in DRIVERS:
            for status in STATUSES:
                for error in (ValueError(SECRET), OverflowError(PRIVATE_URL), RuntimeError(PRIVATE_BODY)):
                    with self.subTest(cls=cls.__name__, status=status, error=type(error).__name__), patch(
                        "cater.drivers.parsedate_to_datetime", side_effect=error
                    ):
                        self.assert_delay(self.invoke(cls, headers(format_datetime(NOW, usegmt=True)),
                                                      status=status), status, None)

    def test_http_error_during_response_read_retains_delay(self):
        for cls in DRIVERS:
            for status in STATUSES:
                with self.subTest(cls=cls.__name__, status=status):
                    self.assert_delay(self.invoke(cls, headers("3600"), status=status, read=True), status, 3600.0)

    def test_network_failure_has_no_delay(self):
        for cls in DRIVERS:
            for read in (False, True):
                for error in (TimeoutError(SECRET), URLError(PRIVATE_URL), OSError(PRIVATE_BODY)):
                    with self.subTest(cls=cls.__name__, read=read, error=type(error).__name__):
                        failure = self.invoke(cls, None, read=read, error=error)
                        self.assertIs(type(failure), TextTransportFailure)
                        self.assertIsNone(failure.retry_after_seconds)

    def test_other_http_statuses_and_tool_calls_keep_existing_semantics(self):
        for cls in DRIVERS:
            for tool, statuses in ((False, (301, 400, 401, 403, 404, 409, 501)), (True, STATUSES)):
                for status in statuses:
                    with self.subTest(cls=cls.__name__, tool=tool, status=status), patch(
                        "cater.drivers._retry_after_seconds", side_effect=AssertionError("Unexpected header access")
                    ), patch("cater.anthropic._retry_after_seconds", side_effect=AssertionError("Unexpected header access")):
                        failure = self.invoke(cls, BrokenHeaders(RuntimeError(SECRET)), status=status, tool=tool)
                        expected = ToolRequestRejected if tool and status == 429 else DriverUnavailable
                        self.assertIs(type(failure), expected)
                        self.assertFalse(hasattr(failure, "retry_after_seconds"))
                        if expected is ToolRequestRejected:
                            self.assertEqual(failure.status_code, 429)


if __name__ == "__main__":
    unittest.main()
