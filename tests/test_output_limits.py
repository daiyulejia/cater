"""Per-request generation limits are optional, explicit and never retried here."""

import io
import json
import os
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from cater.drivers import (
    OpenAICompatibleDriver,
    TextRequestRejected,
    driver_from_dict,
)


CONFIG = {
    "kind": "openai_compatible",
    "name": "output_limit_fixture",
    "endpoint": "https://example.invalid/v1/chat/completions",
    "model": "fixture",
    "api_key_env": "CATER_OUTPUT_LIMIT_FIXTURE",
}
FIELDS = ("max_tokens", "max_completion_tokens")


class OutputLimitTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {CONFIG["api_key_env"]: "synthetic-only"}))

    def call(self, driver, native):
        if native:
            return driver.tool_turn(role="dev", prompt="fixture", history=[], tools=[])
        return driver.complete(role="dev", prompt="fixture", context={})

    def response(self, finish_reason="stop"):
        return {
            "choices": [{"finish_reason": finish_reason, "message": {
                "role": "assistant", "content": '{"result":"complete-looking"}',
            }}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20},
        }

    def wire_response(self, data):
        return io.BytesIO(json.dumps(data).encode("utf-8"))

    def test_configured_limit_reaches_both_request_paths_without_other_field(self):
        for field in FIELDS:
            for native in (False, True):
                with self.subTest(field=field, native=native):
                    driver = driver_from_dict({**CONFIG, field: 4096})
                    with patch("cater.drivers._open_api_request", return_value=self.wire_response(self.response())) as send:
                        result = self.call(driver, native)
                    self.assertEqual(send.call_count, 1)
                    payload = json.loads(send.call_args.args[0].data)
                    self.assertEqual(payload[field], 4096)
                    self.assertNotIn(next(item for item in FIELDS if item != field), payload)
                    self.assertEqual(result.tokens, 20)

    def test_unconfigured_or_null_limits_preserve_provider_default(self):
        for limits in ({}, dict.fromkeys(FIELDS)):
            for native in (False, True):
                with self.subTest(limits=limits, native=native):
                    driver = driver_from_dict({**CONFIG, **limits})
                    with patch("cater.drivers._open_api_request", return_value=self.wire_response(self.response())) as send:
                        self.call(driver, native)
                    payload = json.loads(send.call_args.args[0].data)
                    self.assertTrue(all(field not in payload for field in FIELDS))

    def test_invalid_limits_fail_locally_in_config_and_constructor(self):
        constructor = {key: value for key, value in CONFIG.items() if key != "kind"}
        for field in FIELDS:
            for value in (True, False, 0, -1, 1.0, "4096", [], {}):
                for direct in (False, True):
                    with self.subTest(field=field, value=value, direct=direct):
                        with patch("cater.drivers._open_api_request") as send:
                            with self.assertRaises(ValueError):
                                if direct:
                                    OpenAICompatibleDriver(**constructor, **{field: value})
                                else:
                                    driver_from_dict({**CONFIG, field: value})
                            send.assert_not_called()

    def test_conflicting_fields_fail_before_request(self):
        with self.assertRaisesRegex(ValueError, "only one"):
            driver_from_dict({**CONFIG, "max_tokens": 10, "max_completion_tokens": 20})
        constructor = {key: value for key, value in CONFIG.items() if key != "kind"}
        with self.assertRaisesRegex(ValueError, "only one"):
            OpenAICompatibleDriver(**constructor, max_tokens=10, max_completion_tokens=20)

    def test_truncated_complete_looking_text_is_not_accepted_or_retried(self):
        for field in FIELDS:
            for native in (False, True):
                with self.subTest(field=field, native=native):
                    driver = driver_from_dict({**CONFIG, field: 8})
                    with patch("cater.drivers._open_api_request", return_value=self.wire_response(self.response("length"))) as send:
                        if native:
                            result = self.call(driver, native)
                            self.assertEqual((result.text, result.calls, result.tokens), ("", [], 20))
                            self.assertIs(result.meta["contained"], True)
                            self.assertEqual(result.meta["reason"], "incomplete_stop")
                        else:
                            result = self.call(driver, native)
                            self.assertNotIn("complete-looking", result.text)
                            self.assertNotIn("deliverables", json.loads(result.text))
                            self.assertEqual(result.tokens, 20)
                            self.assertEqual(result.meta["token_usage"], "reported")
                            self.assertIs(result.meta["contained"], True)
                    self.assertEqual(send.call_count, 1)
                    self.assertEqual(json.loads(send.call_args.args[0].data)[field], 8)

    def test_incomplete_tool_call_remains_contained_and_preserves_usage(self):
        driver = driver_from_dict({**CONFIG, "max_tokens": 8})
        data = self.response("length")
        data["choices"][0]["message"]["tool_calls"] = [{
            "id": "call-1", "type": "function",
            "function": {"name": "read_file", "arguments": "{}"},
        }]
        schema = [{"name": "read_file", "description": "Fixture read",
                   "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}}]
        with patch("cater.drivers._open_api_request", return_value=self.wire_response(data)) as send:
            result = driver.tool_turn(role="dev", prompt="fixture", history=[], tools=schema)
        self.assertEqual(send.call_count, 1)
        self.assertEqual((result.text, result.calls, result.tokens), ("", [], 20))
        self.assertIs(result.meta["contained"], True)
        self.assertEqual(result.meta["usage"], data["usage"])

    def test_limits_do_not_disable_known_text_only_rejection_classification(self):
        for field in FIELDS:
            with self.subTest(field=field):
                driver = driver_from_dict({**CONFIG, field: 100})
                error = HTTPError(CONFIG["endpoint"], 429, "limited", {"Retry-After": "3"}, None)
                with patch("cater.drivers._open_api_request", side_effect=error) as send:
                    with self.assertRaises(TextRequestRejected):
                        self.call(driver, False)
                self.assertEqual(send.call_count, 1)

    def test_truncation_is_counted_without_publication_or_automatic_retry(self):
        from test_automatic_text_retry import AutomaticTextRetryTests

        fixture = AutomaticTextRetryTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        rt, task, driver = fixture.runtime(OpenAICompatibleDriver)
        driver.max_tokens = 8
        driver.token_budget = 20
        data = self.response("length")
        data["choices"][0]["message"]["content"] = json.dumps({
            "text": "PRIVATE_TRUNCATED", "deliverables": [{
                "title": "Untrusted", "summary": "Untrusted", "filename": "note.md",
                "format": "markdown", "content": "PRIVATE_TRUNCATED",
            }],
        })
        with patch("cater.drivers._open_api_request", return_value=self.wire_response(data)) as send:
            rt.collaborate(task)
            blocked = rt._complete_with_containment(
                task_id=task, task=rt.task(task), role=next(role for role in rt.roles if role.id == "dev"),
                driver=driver, prompt="This must not be sent",
            )
        self.assertIs(blocked.meta["invocation_started"], False)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(rt._reported_driver_tokens("dev"), 20)
        self.assertEqual(fixture.events(rt, task, "deliverable.published"), [])
        self.assertEqual(fixture.events(rt, task, "driver.retry_scheduled"), [])
        self.assertTrue(fixture.events(rt, task, "driver.budget_exhausted"))
        self.assertNotIn("PRIVATE_TRUNCATED", json.dumps([e.payload for e in rt.log.read(task_id=task)]))
        self.assertTrue(rt.verify_chain().ok)


if __name__ == "__main__":
    unittest.main()
