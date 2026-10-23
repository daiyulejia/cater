from __future__ import annotations

import io
import json
import os
import traceback
import unittest
from copy import deepcopy
from unittest.mock import Mock, patch

from cater import anthropic, drivers, native_transport
from cater.drivers import (DriverProtocolError, DriverUnavailable, ToolRequestRejected,
                           tool_failure_diagnostics, tool_protocol_error_usage)
from cater.tool_protocol import ToolTurn


CLASSES = (drivers.OpenAICompatibleDriver, anthropic.AnthropicMessagesDriver)
TOOLS = [{"name": "lookup", "description": "Read a fixture", "input_schema": {"type": "object"}}]
SIGNED = {"type": "thinking", "thinking": "PRIVATE_THOUGHT", "signature": "PRIVATE_SIGNATURE"}
HISTORY = [
    {"role": "assistant", "content": "prior", "tool_calls": [
        {"id": "old", "name": "lookup", "arguments": {"key": "previous"}}],
     "provider_content": [SIGNED, {"type": "text", "text": "prior"},
                          {"type": "tool_use", "id": "old", "name": "lookup", "input": {"key": "previous"}}]},
    {"role": "tool", "tool_call_id": "old", "content": "fixture result", "is_error": True},
]


def response(cls):
    if issubclass(cls, anthropic.AnthropicMessagesDriver):
        return {"type": "message", "stop_reason": "tool_use", "content": [
            SIGNED, {"type": "redacted_thinking", "data": "PRIVATE_REDACTED"},
            {"type": "text", "text": "next"},
            {"type": "tool_use", "id": "new", "name": "lookup", "input": {"key": "current"}}],
            "usage": {"input_tokens": 11, "output_tokens": 3, "cache_read_input_tokens": 7,
                      "cache_creation_input_tokens": 2, "private": "PRIVATE_USAGE"}}
    return {"choices": [{"finish_reason": "tool_calls", "message": {"content": "next", "tool_calls": [
        {"id": "new", "type": "function", "function": {"name": "lookup", "arguments": '{"key":"current"}'}}]}}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14,
                  "prompt_tokens_details": {"cached_tokens": 7, "private": "PRIVATE_USAGE"},
                  "private": "PRIVATE_USAGE"}}


def drain(iterator):
    yielded = []
    while True:
        try:
            yielded.append(next(iterator))
        except StopIteration as done:
            return yielded, done.value


class NativeDriverIterationTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {"CATER_ITERATION_TEST_KEY": "fixture-secret"}))
        self.enterContext(patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("Real HTTP forbidden")))

    def driver(self, cls, **options):
        return cls("iteration-test", endpoint="https://provider.test/v1/fixture", model="fixture-model",
                   api_key_env="CATER_ITERATION_TEST_KEY", timeout=17, **options)

    def module(self, cls):
        return anthropic if issubclass(cls, anthropic.AnthropicMessagesDriver) else drivers

    def arguments(self):
        return {"role": "reviewer", "prompt": "fixture prompt", "history": deepcopy(HISTORY), "tools": deepcopy(TOOLS)}

    def transport(self, data):
        def request(**kwargs):
            yield "pending"
            if kwargs["check_current"] is not None:
                kwargs["check_current"]()
            yield "ready"
            return deepcopy(data)
        return request

    def test_delegation_preserves_complete_request_and_protocol_result(self):
        variants = [(CLASSES[0], {}), (CLASSES[0], {"max_tokens": 123, "temperature": 0.25}),
                    (CLASSES[0], {"max_completion_tokens": 321}),
                    (CLASSES[1], {"max_tokens": 456, "version": "fixture-version"}),
                    (CLASSES[1], {"thinking": "disabled"})]
        for cls, options in variants:
            with self.subTest(cls=cls.__name__, options=options):
                driver, module = self.driver(cls, **options), self.module(cls)
                arguments, check = self.arguments(), Mock()
                with patch.object(module, "_tool_api_request", return_value=deepcopy(response(cls))) as sync:
                    expected = driver.tool_turn(**arguments)
                sync.assert_called_once()
                expected_request = dict(sync.call_args.kwargs)
                self.assertIs(expected_request.pop("opener"), module._DEFAULT_API_OPENER)
                expected_request["check_current"] = check
                with patch.object(native_transport, "iter_request", side_effect=self.transport(response(cls))) as request, \
                        patch.object(module, "_tool_api_request", side_effect=AssertionError("Duplicate request")) as sync:
                    iterator = driver.iter_tool_turn(**arguments, check_current=check)
                    request.assert_not_called()
                    self.assertEqual(next(iterator), "pending")
                    check.assert_not_called()
                    self.assertEqual(drain(iterator), (["ready"], expected))
                    request.assert_called_once_with(**expected_request)
                    check.assert_called_once_with()
                    sync.assert_not_called()
                self.assertEqual(arguments, self.arguments())
                self.assertNotIn("PRIVATE", repr(expected))
                if cls is CLASSES[1]:
                    self.assertEqual(expected.message["provider_content"], response(cls)["content"])
                    self.assertEqual(expected.tokens, 23)
                    self.assertEqual(expected_request["body"]["messages"][1]["content"][0], SIGNED)
                else:
                    self.assertEqual(expected.tokens, 14)
                self.assertNotIn("mode", expected_request["body"])

    def test_each_module_response_byte_limit_is_forwarded(self):
        for cls, limit in ((CLASSES[0], 137), (CLASSES[1], 251)):
            with self.subTest(cls=cls.__name__), patch.object(self.module(cls), "MAX_API_RESPONSE_BYTES", limit), \
                    patch.object(native_transport, "iter_request", side_effect=self.transport(response(cls))) as request:
                self.assertEqual(drain(self.driver(cls).iter_tool_turn(**self.arguments()))[0], ["pending", "ready"])
                self.assertEqual(request.call_args.kwargs["byte_limit"], limit)
                self.assertIsNone(request.call_args.kwargs["check_current"])
                request.assert_called_once()

    def test_protocol_failures_preserve_sanitized_response_usage(self):
        for cls in CLASSES:
            data = {**response(cls), "type": "PRIVATE_INVALID", "choices": []}
            with self.subTest(cls=cls.__name__):
                driver = self.driver(cls)
                with patch.object(self.module(cls), "_tool_api_request", return_value=data):
                    with self.assertRaises(DriverProtocolError) as sync:
                        driver.tool_turn(**self.arguments())
                with patch.object(native_transport, "iter_request", side_effect=self.transport(data)) as request:
                    with self.assertRaises(DriverProtocolError) as native:
                        drain(driver.iter_tool_turn(**self.arguments()))
                request.assert_called_once()
                self.assertEqual(str(native.exception), str(sync.exception))
                self.assertEqual(vars(native.exception), vars(sync.exception))
                self.assertEqual(tool_protocol_error_usage(native.exception), tool_protocol_error_usage(sync.exception))
                self.assertNotIn("PRIVATE", str(native.exception) + repr(vars(native.exception)))
                self.assertIsNone(native.exception.__cause__)
                self.assertEqual(native.exception.__suppress_context__, sync.exception.__suppress_context__)
                self.assertNotIn("PRIVATE", "".join(traceback.format_exception(native.exception)))

    def test_cleanup_failure_marks_turn_without_losing_usage_or_mutating_parsed_result(self):
        for cls in CLASSES:
            for contained in (False, True):
                for failed in (False, True):
                    data = native_transport.NativeResponse(response(cls))
                    data.cleanup_failed = failed
                    if contained:
                        if cls is CLASSES[1]:
                            data["stop_reason"] = "max_tokens"
                        else:
                            data["choices"][0]["finish_reason"] = "length"
                    driver = self.driver(cls)
                    with patch.object(self.module(cls), "_tool_api_request", return_value=data):
                        parsed = driver.tool_turn(**self.arguments())
                    self.assertNotIn("native_cleanup_failed", parsed.meta)
                    with self.subTest(cls=cls.__name__, contained=contained, failed=failed), \
                            patch.object(native_transport, "iter_request", side_effect=self.transport(data)) as request, \
                            patch.object(driver, "_tool_result", return_value=parsed) as parser:
                        yielded, result = drain(driver.iter_tool_turn(**self.arguments()))
                    self.assertEqual(yielded, ["pending", "ready"])
                    request.assert_called_once()
                    parser.assert_called_once()
                    self.assertEqual(result.tokens, 23 if cls is CLASSES[1] else 14)
                    self.assertEqual(result.text, parsed.text)
                    self.assertIs(result.calls, parsed.calls)
                    self.assertIs(result.message, parsed.message)
                    self.assertEqual(result.meta, {**parsed.meta, **({"native_cleanup_failed": True} if failed else {})})
                    self.assertNotIn("native_cleanup_failed", parsed.meta)
                    if failed:
                        self.assertIsNot(result, parsed)
                    else:
                        self.assertIs(result, parsed)

    def test_cleanup_failure_preserves_protocol_error_identity_and_response_usage(self):
        for cls in CLASSES:
            data = native_transport.NativeResponse({**response(cls), "type": "PRIVATE_INVALID", "choices": []})
            data.cleanup_failed = True
            driver = self.driver(cls)
            with patch.object(self.module(cls), "_tool_api_request", return_value=data):
                with self.assertRaises(DriverProtocolError) as sync:
                    driver.tool_turn(**self.arguments())
            self.assertNotIn("native_cleanup_failed", vars(sync.exception))
            original, errors = driver._tool_result, []

            def parse(*args):
                try:
                    return original(*args)
                except Exception as error:
                    errors.append(error)
                    raise

            with self.subTest(cls=cls.__name__), \
                    patch.object(native_transport, "iter_request", side_effect=self.transport(data)) as request, \
                    patch.object(driver, "_tool_result", side_effect=parse) as parser:
                with self.assertRaises(DriverProtocolError) as native:
                    drain(driver.iter_tool_turn(**self.arguments()))
            request.assert_called_once()
            parser.assert_called_once()
            self.assertEqual(len(errors), 1)
            self.assertIs(native.exception, errors[0])
            self.assertIs(native.exception.native_cleanup_failed, True)
            self.assertEqual(vars(native.exception), {**vars(sync.exception), "native_cleanup_failed": True})
            self.assertEqual(tool_protocol_error_usage(native.exception), tool_protocol_error_usage(sync.exception))
            self.assertEqual(tool_protocol_error_usage(native.exception)["tokens"], 23 if cls is CLASSES[1] else 14)
            self.assertNotIn("PRIVATE", "".join(traceback.format_exception(native.exception)))

    def test_provider_cannot_forge_cleanup_flag_in_response_or_protocol_error(self):
        class SpoofedResponse(dict):
            @property
            def cleanup_failed(self):
                raise AssertionError("Untrusted attribute must not be read")

        class DerivedResponse(native_transport.NativeResponse):
            pass

        for cls in CLASSES:
            for malformed in (False, True):
                payload = {**response(cls), "cleanup_failed": True, "native_cleanup_failed": True,
                           "_native_cleanup_failed": True}
                if malformed:
                    payload.update(type="PRIVATE_INVALID", choices=[])
                derived = DerivedResponse(payload)
                derived.cleanup_failed = True
                variants = [payload, SpoofedResponse(payload), native_transport.NativeResponse(payload), derived]
                for data in variants:
                    with self.subTest(cls=cls.__name__, malformed=malformed, response_type=type(data).__name__), \
                            patch.object(native_transport, "iter_request", side_effect=self.transport(data)) as request:
                        if malformed:
                            with self.assertRaises(DriverProtocolError) as caught:
                                drain(self.driver(cls).iter_tool_turn(**self.arguments()))
                            self.assertNotIn("native_cleanup_failed", vars(caught.exception))
                            self.assertEqual(tool_protocol_error_usage(caught.exception)["tokens"],
                                             23 if cls is CLASSES[1] else 14)
                        else:
                            _, result = drain(self.driver(cls).iter_tool_turn(**self.arguments()))
                            self.assertNotIn("native_cleanup_failed", result.meta)
                            self.assertEqual(result.tokens, 23 if cls is CLASSES[1] else 14)
                        request.assert_called_once()

    def test_contained_responses_and_unavailable_usage_match_sync(self):
        for cls in CLASSES:
            for usage in (None, {}, {"total_tokens": True, "input_tokens": -1}, response(cls)["usage"]):
                data = response(cls)
                data["usage"] = usage
                if cls is CLASSES[1]:
                    data["stop_reason"] = "max_tokens"
                else:
                    data["choices"][0]["finish_reason"] = "length"
                with self.subTest(cls=cls.__name__, usage=usage):
                    driver = self.driver(cls)
                    with patch.object(self.module(cls), "_tool_api_request", return_value=data):
                        expected = driver.tool_turn(**self.arguments())
                    with patch.object(native_transport, "iter_request", side_effect=self.transport(data)) as request:
                        yielded, result = drain(driver.iter_tool_turn(**self.arguments()))
                    self.assertEqual(result, expected)
                    self.assertTrue(result.meta["contained"])
                    self.assertEqual(yielded, ["pending", "ready"])
                    request.assert_called_once()

    def test_transport_error_identity_and_safe_fields_survive_without_retry(self):
        for cls in CLASSES:
            for error in (DriverUnavailable("network"), ToolRequestRejected("rejected", status_code=429),
                          DriverProtocolError("invalid response")):
                error.failure_kind = "http_error" if isinstance(error, ToolRequestRejected) else "network_error"
                error.transport_phase = "response_read"
                fields, diagnostics = vars(error).copy(), tool_failure_diagnostics(error)

                def fail(**kwargs):
                    yield "pending"
                    raise error

                with self.subTest(cls=cls.__name__, error=type(error).__name__), \
                        patch.object(native_transport, "iter_request", side_effect=fail) as request, \
                        patch.object(self.module(cls), "_tool_api_request") as sync:
                    with self.assertRaises(type(error)) as caught:
                        drain(self.driver(cls).iter_tool_turn(**self.arguments()))
                    self.assertIs(caught.exception, error)
                    self.assertEqual(vars(error), fields)
                    self.assertEqual(tool_failure_diagnostics(error), diagnostics)
                    request.assert_called_once()
                    sync.assert_not_called()

    def test_check_current_error_is_owned_by_transport_and_keeps_identity(self):
        for cls in CLASSES:
            error = RuntimeError("superseded")
            check = Mock(side_effect=error)
            with self.subTest(cls=cls.__name__), \
                    patch.object(native_transport, "iter_request", side_effect=self.transport(response(cls))) as request:
                iterator = self.driver(cls).iter_tool_turn(**self.arguments(), check_current=check)
                self.assertEqual(next(iterator), "pending")
                check.assert_not_called()
                with self.assertRaises(RuntimeError) as caught:
                    next(iterator)
                self.assertIs(caught.exception, error)
                request.assert_called_once()
                check.assert_called_once_with()

    def test_yield_from_forwards_send_throw_and_close(self):
        for cls in CLASSES:
            closed = Mock()

            def cooperative(**kwargs):
                try:
                    value = yield "pending"
                    yield value
                    return response(cls)
                finally:
                    closed()

            with self.subTest(cls=cls.__name__), patch.object(native_transport, "iter_request", side_effect=cooperative):
                iterator = self.driver(cls).iter_tool_turn(**self.arguments())
                self.assertEqual(next(iterator), "pending")
                self.assertEqual(iterator.send("resumed"), "resumed")
                iterator.close()
                closed.assert_called_once_with()
                error = RuntimeError("cancelled")
                iterator = self.driver(cls).iter_tool_turn(**self.arguments())
                next(iterator)
                with self.assertRaises(RuntimeError) as caught:
                    iterator.throw(error)
                self.assertIs(caught.exception, error)
                self.assertEqual(closed.call_count, 2)

    def test_subclass_and_instance_billing_guards_cannot_be_bypassed(self):
        for cls in CLASSES:
            for override in ("subclass", "instance", "class"):
                error = DriverUnavailable("billing guard")
                guard = Mock(side_effect=error)
                driver = self.driver(cls)
                if override == "subclass":
                    class Guarded(cls):
                        def tool_turn(self, **kwargs):
                            return guard(**kwargs)
                    driver = self.driver(Guarded)
                with self.subTest(cls=cls.__name__, override=override), \
                        patch.object(native_transport, "iter_request") as request, \
                        patch.object(driver, "_tool_input", side_effect=AssertionError("Guard must run first")):
                    if override == "instance":
                        target = patch.object(driver, "tool_turn", guard)
                    elif override == "class":
                        def class_guard(self, **kwargs):
                            return guard(**kwargs)
                        target = patch.object(cls, "tool_turn", class_guard)
                    else:
                        target = patch.object(driver, "name", driver.name)
                    with target:
                        with self.assertRaises(DriverUnavailable) as caught:
                            drain(driver.iter_tool_turn(**self.arguments()))
                    self.assertIs(caught.exception, error)
                    guard.assert_called_once_with(**self.arguments())
                    request.assert_not_called()

    def test_class_replacement_returns_exact_sync_result_once(self):
        for cls in CLASSES:
            expected = ToolTurn("guarded", [], 9, {"token_usage": "reported"})
            guard = Mock(return_value=expected)

            def guarded(self, **kwargs):
                return guard(**kwargs)

            module = self.module(cls)
            original = module._ANTHROPIC_TOOL_TURN if cls is CLASSES[1] else module._OPENAI_TOOL_TURN
            self.assertIs(original, cls.tool_turn)
            with self.subTest(cls=cls.__name__), patch.object(cls, "tool_turn", guarded), \
                    patch.object(native_transport, "iter_request") as request:
                yielded, result = drain(self.driver(cls).iter_tool_turn(**self.arguments()))
                self.assertEqual(yielded, [])
                self.assertIs(result, expected)
                guard.assert_called_once_with(**self.arguments())
                request.assert_not_called()
                saved = module._ANTHROPIC_TOOL_TURN if cls is CLASSES[1] else module._OPENAI_TOOL_TURN
                self.assertIs(saved, original)

    def test_successful_guard_wraps_original_sync_call_exactly_once(self):
        for cls in CLASSES:
            for override in ("subclass", "instance"):
                guard = Mock()
                driver = self.driver(cls)
                original = driver.tool_turn
                if override == "subclass":
                    class Guarded(cls):
                        def tool_turn(self, **kwargs):
                            guard(**kwargs)
                            return super().tool_turn(**kwargs)
                    driver = self.driver(Guarded)
                else:
                    def checked(**kwargs):
                        guard(**kwargs)
                        return original(**kwargs)
                    driver.tool_turn = checked
                with self.subTest(cls=cls.__name__, override=override), \
                        patch.object(native_transport, "iter_request") as request, \
                        patch.object(self.module(cls), "_tool_api_request", return_value=response(cls)) as opened:
                    yielded, result = drain(driver.iter_tool_turn(**self.arguments()))
                    self.assertEqual(yielded, [])
                    self.assertIsInstance(result, ToolTurn)
                    guard.assert_called_once_with(**self.arguments())
                    opened.assert_called_once()
                    request.assert_not_called()

    def test_mocked_opener_stays_sync_and_is_called_once(self):
        for cls in CLASSES:
            check = Mock()
            with self.subTest(cls=cls.__name__), patch.object(native_transport, "iter_request") as request, \
                    patch.object(self.module(cls), "_open_api_request", return_value=io.BytesIO(
                        json.dumps(response(cls)).encode())) as opened:
                yielded, result = drain(self.driver(cls).iter_tool_turn(**self.arguments(), check_current=check))
                self.assertEqual(yielded, [])
                self.assertEqual(result.text, "next")
                opened.assert_called_once()
                check.assert_called_once_with()
                request.assert_not_called()

    def test_fallback_check_failure_preserves_identity_and_prevents_request(self):
        for cls in CLASSES:
            error = RuntimeError("superseded")
            check = Mock(side_effect=error)
            with self.subTest(cls=cls.__name__), patch.object(native_transport, "iter_request") as request, \
                    patch.object(self.module(cls), "_open_api_request") as opened:
                with self.assertRaises(RuntimeError) as caught:
                    drain(self.driver(cls).iter_tool_turn(**self.arguments(), check_current=check))
                self.assertIs(caught.exception, error)
                check.assert_called_once_with()
                request.assert_not_called()
                opened.assert_not_called()

    def test_invalid_history_and_missing_credentials_keep_sync_errors(self):
        for cls in CLASSES:
            for invalid_history in (False, True):
                arguments = self.arguments()
                if invalid_history:
                    arguments["history"] = [{}]
                with self.subTest(cls=cls.__name__, invalid_history=invalid_history), \
                        patch.dict(os.environ, {}, clear=True), patch.object(native_transport, "iter_request") as request:
                    driver = self.driver(cls)
                    with self.assertRaises((DriverProtocolError, DriverUnavailable)) as sync:
                        driver.tool_turn(**arguments)
                    with self.assertRaises(type(sync.exception)) as native:
                        drain(driver.iter_tool_turn(**arguments))
                    self.assertEqual(str(native.exception), str(sync.exception))
                    self.assertEqual(vars(native.exception), vars(sync.exception))
                    request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
