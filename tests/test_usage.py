from __future__ import annotations

import json
import sys
import unittest
from unittest.mock import patch

from cater.drivers import CliDriver, DriverProtocolError, DriverResponse, OpenAICompatibleDriver
from cater.events import Actor
from cater.runtime import Runtime
from cater.usage import usage_source, valid_token_count


class UsageTests(unittest.TestCase):
    def cli_response(self, payload):
        script = (
            "import sys, json\n"
            "for line in sys.stdin:\n"
            " r=json.loads(line)\n"
            f" p=json.loads({json.dumps(json.dumps(payload))})\n"
            " p['id']=r['id']\n"
            " print(json.dumps(p),flush=True)\n"
        )
        driver = CliDriver("usage-fixture", [sys.executable, "-c", script], timeout=3, restart_limit=0)
        try:
            return driver.complete(role="worker", prompt="usage", context={})
        finally:
            driver.close()

    def test_cli_missing_usage_is_unknown_even_when_metadata_claims_reported(self):
        response = self.cli_response({"ok": True, "text": "done", "meta": {"token_usage": "reported"}})
        self.assertEqual(response.tokens, 0)
        self.assertEqual(response.meta["token_usage"], "unavailable")

    def test_cli_usage_requires_non_negative_integer_not_boolean(self):
        for tokens in (True, -1, "3", 1.5):
            with self.subTest(tokens=tokens):
                with self.assertRaisesRegex(DriverProtocolError, "non-negative integer"):
                    self.cli_response({"ok": True, "text": "done", "tokens": tokens})
        self.assertEqual(self.cli_response({"ok": True, "text": "done", "tokens": 0}).meta["token_usage"], "reported")
        self.assertEqual(self.cli_response({"ok": True, "text": "done", "tokens": 3, "meta": {"token_usage": "estimated"}}).meta["token_usage"], "estimated")

    def test_api_invalid_usage_preserves_response_but_marks_usage_unknown(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def read(self, _limit): return json.dumps(body).encode()

        driver = OpenAICompatibleDriver("api", endpoint="https://example.test/v1/chat/completions", model="fixture", api_key_env="CATER_USAGE_KEY")
        for tokens in (True, -1, "3", 1.5, None, [], 0, 37):
            with self.subTest(tokens=tokens):
                body = {"choices": [{"message": {"content": "done"}}], "usage": {"total_tokens": tokens}}
                with patch.dict("cater.drivers.os.environ", {"CATER_USAGE_KEY": "fixture"}), patch("cater.drivers._open_api_request", return_value=Response()):
                    response = driver.complete(role="worker", prompt="usage", context={})
                self.assertEqual(response.text, "done")
                self.assertEqual(response.meta["token_usage"], "reported" if valid_token_count(tokens) else "unavailable")
                self.assertEqual(response.tokens, tokens if valid_token_count(tokens) else 0)

    def test_runtime_does_not_infer_telemetry_source_or_accept_invalid_counts(self):
        for tokens, source in ((3, None), (-10, "reported"), (True, "reported"), (3, []), (3, "unknown")):
            with self.subTest(tokens=tokens, source=source):
                self.assertEqual(Runtime._token_usage(DriverResponse(text="ok", tokens=tokens, meta={"token_usage": source})), "unavailable")
        self.assertEqual(usage_source(0, "reported"), "reported")
        self.assertEqual(usage_source(4, "estimated"), "estimated")

    def test_historical_bad_counts_do_not_crash_projection_or_reduce_budget(self):
        with Runtime(":memory:") as rt:
            task_id = rt.create_task("用量账本回归")
            for tokens, source in ((5, "reported"), (-100, "reported"), (True, "reported"), ("garbage", "reported"), (7, None), (2, [])):
                rt.log.emit(actor=Actor.agent("product", "worker"), type="msg.said", task_id=task_id, payload={"text": "记录", "driver": "shared", "tokens": tokens, "token_usage": source})
            task = rt.task(task_id)
            self.assertEqual(rt._reported_driver_tokens("shared"), 5)
            self.assertEqual(task.tokens, 14)
            self.assertEqual(task.token_usage, {"reported": 1, "unavailable": 5})
            lens = next(item for item in rt.review(task_id)["lenses"] if "Token usage is unavailable" in item["finding"])
            self.assertEqual(lens["status"], "watch")

    def test_invalid_custom_driver_telemetry_does_not_discard_useful_output(self):
        class Driver:
            name = "product"
            def complete(self, **_kwargs):
                return DriverResponse(text=json.dumps({"stance": "assert", "option_id": "demo-shared", "claim": "保留正常产出", "evidence": ["检查正文"], "confidence": 0.9, "text": "保留正常产出"}), tokens=-100, meta={"token_usage": "reported"})

        with Runtime(":memory:", drivers={"product": Driver()}) as rt:
            task_id = rt.create_task("自定义驱动统计回归")
            rt.collaborate(task_id)
            messages = [event.payload for event in rt.log.read(task_id=task_id) if event.type in {"msg.said", "msg.challenge"} and event.payload.get("driver") == "product"]
            self.assertTrue(messages)
            self.assertTrue(all(item["tokens"] == 0 and item["token_usage"] == "unavailable" for item in messages))
            self.assertTrue(any(item["text"] == "保留正常产出" for item in messages))

    def test_failed_attempt_cost_is_not_hidden_by_successful_reported_output(self):
        with Runtime(":memory:") as rt:
            task_id = rt.create_task("失败重试成本")
            rt.log.emit(actor=Actor.agent("product", "worker"), type="msg.said", task_id=task_id, payload={"text": "结果", "driver": "product", "tokens": 5, "token_usage": "reported"})
            rt.log.emit(actor=Actor.system("driver_gateway"), type="driver.failed", task_id=task_id, payload={"driver": "product", "role": "product", "attempt": 1, "contained": True})
            lens = next(item for item in rt.review(task_id)["lenses"] if "failed driver attempt" in item["finding"])
            self.assertEqual(lens["status"], "watch")
            self.assertIn("failed driver attempt", lens["finding"])
