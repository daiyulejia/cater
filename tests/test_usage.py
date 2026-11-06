from __future__ import annotations

import json
import argparse
import io
import traceback
import tempfile
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext, redirect_stdout
import sys
import unittest
from unittest.mock import patch

from cater.drivers import CliDriver, DriverProtocolError, DriverResponse, OpenAICompatibleDriver, native_openai_usage, tool_protocol_error_usage
from cater.cli import cmd_probe_driver
from cater.events import Actor
from cater.runtime import Runtime
from cater.usage import openai_usage_conflict, usage_conflict_diagnostics, usage_source, valid_token_count


class UsageTests(unittest.TestCase):
    def test_budget_decline_keeps_legacy_text_and_identifies_soft_accounting(self):
        driver = OpenAICompatibleDriver("product", endpoint="https://example.test", model="fixture", api_key_env="CATER_USAGE_KEY", token_budget=0)
        with Runtime(":memory:", drivers={"product": driver}) as rt, patch("cater.drivers._open_api_request") as transport:
            task_id = rt.create_task("预算提示")
            role = next(role for role in rt.roles if role.id == "product")
            response = rt._complete_with_containment(task_id=task_id, task=rt.task(task_id), role=role, driver=driver, prompt="fixture")
            text = response.meta["display_reason"]
            self.assertIn("Cater 本地累计 token 预算", text)
            self.assertIn("已记录 0，上限 0", text)
            self.assertIn("仅用于已报告用量", text)
            self.assertIn("不是硬上限", text)
            transport.assert_not_called()

    def test_text_rejects_ambiguous_json_without_exposing_or_counting_any_value(self):
        bodies = (
            b'{"choices":[{"message":{"content":"ok"}}],"usage":{"total_tokens":500,"total_tokens":1}}',
            b'{"choices":[{"message":{"content":"ok"}}],"usage":{"total_tokens":1,"total_tokens":500}}',
            b'{"choices":[{"message":{"content":"ok"}}],"usage":{"total_tokens":500,"total_tokens":500}}',
            b'{"choices":[{"message":{"content":"ok"}}],"usage":{"total_tokens":500,"total_\\u0074okens":1}}',
            b'{"choices":[{"message":{"content":"ok"}}],"usage":{"total_tokens":500},"usage":{"total_tokens":1}}',
            b'{"choices":[{"message":{"content":"ok","content":"SECRET_PROVIDER_CONTENT"}}],"usage":{"total_tokens":1}}',
            b'{"choices":[{"message":{"content":"ok"}}],"usage":{"total_tokens":1},"extra":{"key":1,"key":2}}',
        )
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def read(self, _limit): return raw
        driver = OpenAICompatibleDriver("product", endpoint="https://example.test", model="fixture", api_key_env="CATER_USAGE_KEY")
        with patch.dict("cater.drivers.os.environ", {"CATER_USAGE_KEY": "fixture"}), patch("cater.drivers._open_api_request", return_value=Response()):
            for raw in bodies:
                with self.subTest(body=raw[:30]), self.assertRaises(DriverProtocolError) as raised:
                    driver.complete(role="worker", prompt="fixture", context={})
                self.assertIn("无歧义", str(raised.exception))
                self.assertIn("用量无法确认", str(raised.exception))
                self.assertEqual(tool_protocol_error_usage(raised.exception), {"tokens": 0, "token_usage": "unavailable", "usage": {}})
                self.assertNotIn("SECRET_PROVIDER_CONTENT", "".join(traceback.format_exception(raised.exception)))

    def test_text_valid_json_does_not_treat_string_content_or_separate_objects_as_duplicates(self):
        body = {"choices": [{"message": {"content": '{"same":1,"same":2}'}}],
                "usage": {"total_tokens": 5, "prompt_tokens": 3, "completion_tokens": 2},
                "provider_metadata": [{"same": 1}, {"same": 2}]}
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def read(self, _limit): return json.dumps(body).encode()
        driver = OpenAICompatibleDriver("product", endpoint="https://example.test", model="fixture", api_key_env="CATER_USAGE_KEY")
        with patch.dict("cater.drivers.os.environ", {"CATER_USAGE_KEY": "fixture"}), patch("cater.drivers._open_api_request", return_value=Response()):
            response = driver.complete(role="worker", prompt="fixture", context={})
        self.assertEqual(response.text, '{"same":1,"same":2}')
        self.assertEqual((response.tokens, response.meta["token_usage"]), (5, "reported"))

    def test_text_preserves_existing_number_decoder_for_unrelated_provider_fields(self):
        raw = b'{"choices":[{"message":{"content":"ok"}}],"usage":{"total_tokens":5},"provider_metadata":{"large":1e9999}}'
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def read(self, _limit): return raw
        driver = OpenAICompatibleDriver("product", endpoint="https://example.test", model="fixture", api_key_env="CATER_USAGE_KEY")
        with patch.dict("cater.drivers.os.environ", {"CATER_USAGE_KEY": "fixture"}), patch("cater.drivers._open_api_request", return_value=Response()):
            response = driver.complete(role="worker", prompt="fixture", context={})
        self.assertEqual((response.text, response.tokens, response.meta["token_usage"]), ("ok", 5, "reported"))

    def test_ambiguous_text_response_records_unknown_without_using_output(self):
        raw = b'{"choices":[{"message":{"content":"SECRET_PROVIDER_CONTENT"}}],"usage":{"total_tokens":500,"total_tokens":1}}'
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def read(self, _limit): return raw
        driver = OpenAICompatibleDriver("product", endpoint="https://example.test", model="fixture", api_key_env="CATER_USAGE_KEY", token_budget=100)
        with Runtime(":memory:", drivers={"product": driver}) as rt, patch.dict("cater.drivers.os.environ", {"CATER_USAGE_KEY": "fixture"}), patch("cater.drivers._open_api_request", return_value=Response()) as transport:
            task_id = rt.create_task("重复键不可信")
            role = next(role for role in rt.roles if role.id == "product")
            response = rt._complete_with_containment(task_id=task_id, task=rt.task(task_id), role=role, driver=driver, prompt="fixture")
            self.assertTrue(response.meta["contained"])
            self.assertEqual(response.meta["token_usage"], "unavailable")
            self.assertNotIn("SECRET_PROVIDER_CONTENT", response.text)
            failed, = rt.log.iter_events(task_id=task_id, event_types=("driver.failed",))
            self.assertEqual(failed.payload["error_type"], "DriverProtocolError")
            self.assertIn("usage may be unknown", failed.payload["message"])
            self.assertIn("remote charges may apply", failed.payload["message"])
            self.assertEqual(rt._reported_driver_tokens("product"), 0)
            self.assertEqual(transport.call_count, 1)
            self.assertTrue(rt.verify_chain().ok)

    def test_ambiguous_probe_is_recorded_as_failed_unknown_not_one_of_the_duplicates(self):
        raw = b'{"choices":[{"message":{"content":"SECRET_PROVIDER_CONTENT"}}],"usage":{"total_tokens":500,"total_tokens":1}}'
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def read(self, _limit): return raw
        driver = OpenAICompatibleDriver("product", endpoint="https://example.test", model="fixture", api_key_env="CATER_USAGE_KEY", token_budget=100)
        with Runtime(":memory:", drivers={"product": driver}) as rt, patch("cater.cli.open_runtime", return_value=nullcontext(rt)), patch.dict("cater.drivers.os.environ", {"CATER_USAGE_KEY": "fixture"}), patch("cater.drivers._open_api_request", return_value=Response()) as transport:
            output = io.StringIO()
            with redirect_stdout(output), self.assertRaises(DriverProtocolError) as raised:
                cmd_probe_driver(argparse.Namespace(name="product"))
            event, = rt.log.iter_events(event_types=("driver.probe_called",))
            self.assertEqual((event.payload["status"], event.payload["token_usage"], event.payload["tokens"]), ("failed", "unavailable", 0))
            self.assertEqual(rt._reported_driver_tokens("product"), 0)
            self.assertIn("不是硬上限", output.getvalue())
            self.assertIn("用量无法确认", str(raised.exception))
            self.assertNotIn("SECRET_PROVIDER_CONTENT", json.dumps(event.payload))
            self.assertEqual(transport.call_count, 1)
            self.assertTrue(rt.verify_chain().ok)

    def test_native_provenance_rejects_custom_and_replaced_adapter_methods(self):
        driver = OpenAICompatibleDriver("product", endpoint="https://example.test", model="fixture", api_key_env="CATER_USAGE_KEY")
        self.assertTrue(native_openai_usage(driver, tool=False))
        self.assertTrue(native_openai_usage(driver, tool=True))
        with patch.object(driver, "complete", return_value=DriverResponse("fake")):
            self.assertFalse(native_openai_usage(driver, tool=False))
        with patch.object(driver, "_tool_result", return_value=None):
            self.assertFalse(native_openai_usage(driver, tool=True))
        class Adapter(OpenAICompatibleDriver): pass
        self.assertFalse(native_openai_usage(Adapter("other", endpoint="https://example.test", model="fixture", api_key_env="CATER_USAGE_KEY"), tool=False))

    def test_openai_conflicts_only_follow_valid_canonical_components(self):
        cases = [
            ({"total_tokens": 1, "prompt_tokens": 80001}, True),
            ({"total_tokens": 9, "prompt_tokens": 5, "completion_tokens": 5}, True),
            ({"total_tokens": 0, "completion_tokens": 1, "prompt_tokens": True}, True),
            ({"total_tokens": 10, "prompt_tokens": 5, "completion_tokens": 5}, False),
            ({"total_tokens": 11, "prompt_tokens": 5, "completion_tokens": 5}, False),
            ({"total_tokens": 1, "prompt_tokens": "80001"}, False),
            ({"total_tokens": True, "prompt_tokens": 80001}, False),
            ({"prompt_tokens": 80001}, False),
            ({"total_tokens": 0, "prompt_cache_hit_tokens": 80001}, False),
            ({"total_tokens": 0, "prompt_tokens_details": {"cached_tokens": 80001}}, False),
            ({"total_tokens": 0, "prompt_tokens": 0, "completion_tokens": 0}, False),
        ]
        for usage, expected in cases:
            with self.subTest(usage=usage):
                self.assertEqual(openai_usage_conflict(usage), expected)

    def test_text_and_tool_paths_agree_for_missing_invalid_and_extra_usage(self):
        from cater.tool_protocol import _usage
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def read(self, _limit): return json.dumps(body).encode()
        driver = OpenAICompatibleDriver("product", endpoint="https://example.test", model="fixture", api_key_env="CATER_USAGE_KEY")
        for usage in (None, {}, {"total_tokens": 5}, {"total_tokens": 5, "prompt_tokens": None},
                      {"total_tokens": 5, "completion_tokens": "3"}, {"total_tokens": 5, "unknown": "secret"},
                      {"total_tokens": 5, "prompt_tokens": 4, "completion_tokens": 2},
                      {"total_tokens": 5, "prompt_tokens_details": {"cached_tokens": "invalid"}}):
            body = {"choices": [{"message": {"content": "ok"}}], "usage": usage}
            with self.subTest(usage=usage), patch.dict("cater.drivers.os.environ", {"CATER_USAGE_KEY": "fixture"}), patch("cater.drivers._open_api_request", return_value=Response()):
                response = driver.complete(role="worker", prompt="usage", context={})
                tokens, meta = _usage(body, anthropic=False, driver="product")
                self.assertEqual((response.tokens, response.meta["token_usage"]), (tokens, meta["token_usage"]))

    def test_legacy_reported_conflict_is_visible_but_provider_not_assumed(self):
        class Driver:
            token_budget = 80000
        driver = Driver()
        with Runtime(":memory:") as rt:
            task = rt.create_task("旧用量")
            event = rt.log.emit(type="team.model_called", actor=Actor.system("team"), task_id=task,
                                payload={"driver": "product", "tokens": 1, "token_usage": "reported",
                                         "usage": {"total_tokens": 1, "prompt_tokens": 80001}})
            evidence = rt._budget_usage_evidence({"product"})
            self.assertEqual(evidence, {"product": {"historical_source_unknown": event.seq}})
            self.assertIsNone(rt._budget_usage_uncertainty("product", driver))
            self.assertEqual(rt._reported_driver_tokens("product"), 1)
            self.assertTrue(rt.verify_chain().ok)

    def test_conflict_evidence_is_rechecked_and_only_public_counts_are_saved(self):
        meta = {"mode": "openai_compatible", "token_usage": "unavailable",
                "usage_issue": "total_below_reported_components",
                "usage": {"total_tokens": 1, "prompt_tokens": 80001, "private": "SECRET"}}
        self.assertEqual(usage_conflict_diagnostics(meta), {"usage_issue": "total_below_reported_components",
                         "usage": {"total_tokens": 1, "prompt_tokens": 80001}})
        for replacement in ({"mode": "anthropic"}, {"token_usage": "reported"}, {"usage_issue": "OTHER"},
                            {"usage": {"total_tokens": 1, "prompt_tokens": 1}}):
            self.assertEqual(usage_conflict_diagnostics({**meta, **replacement}), {})

    def test_text_conflict_retains_output_and_persists_explanation_without_inventing_budget(self):
        body = {"choices": [{"message": {"content": json.dumps({"stance": "assert", "text": "fixture result"})}}],
                "usage": {"total_tokens": 1, "prompt_tokens": 80001, "private": "SECRET"}}
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def read(self, _limit): return json.dumps(body).encode()
        driver = OpenAICompatibleDriver("product", endpoint="https://example.test", model="fixture", api_key_env="CATER_USAGE_KEY")
        with patch.dict("cater.drivers.os.environ", {"CATER_USAGE_KEY": "fixture"}), patch("cater.drivers._open_api_request", return_value=Response()):
            response = driver.complete(role="worker", prompt="usage", context={})
        self.assertIn("fixture result", response.text)
        self.assertEqual((response.tokens, response.meta["token_usage"]), (0, "unavailable"))
        self.assertNotIn("SECRET", json.dumps(response.meta))
        with Runtime(":memory:", drivers={"product": driver}) as rt, patch.dict("cater.drivers.os.environ", {"CATER_USAGE_KEY": "fixture"}), patch("cater.drivers._open_api_request", return_value=Response()):
            task = rt.create_task("测试真实用量矛盾")
            rt.collaborate(task)
            messages = [e.payload for e in rt.log.read(task_id=task) if e.type in {"msg.said", "msg.challenge"} and e.payload.get("driver") == "product"]
            self.assertTrue(messages)
            recorded, = [e.payload for e in rt.log.read(task_id=task) if e.type == "driver.usage_inconsistent"]
            self.assertEqual(recorded["usage_issue"], "total_below_reported_components")
            self.assertEqual(recorded["usage"], {"total_tokens": 1, "prompt_tokens": 80001})
            self.assertEqual(rt._reported_driver_tokens("product"), 0)
            self.assertTrue(rt.verify_chain().ok)

    def test_text_conflict_survives_length_containment_and_malformed_response(self):
        body = {"usage": {"total_tokens": 1, "prompt_tokens": 80001, "private": "SECRET"},
                "choices": [{"finish_reason": "length", "message": {"content": "partial"}}]}
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def read(self, _limit): return json.dumps(body).encode()
        driver = OpenAICompatibleDriver("product", endpoint="https://example.test", model="fixture", api_key_env="CATER_USAGE_KEY")
        with patch.dict("cater.drivers.os.environ", {"CATER_USAGE_KEY": "fixture"}), patch("cater.drivers._open_api_request", return_value=Response()):
            response = driver.complete(role="worker", prompt="usage", context={})
            self.assertTrue(response.meta["contained"])
            self.assertEqual(response.meta["token_usage"], "unavailable")
            body["choices"] = []
            with self.assertRaises(DriverProtocolError) as raised:
                driver.complete(role="worker", prompt="usage", context={})
        counts = tool_protocol_error_usage(raised.exception)
        self.assertEqual(counts, {"tokens": 0, "token_usage": "unavailable",
                         "usage_issue": "total_below_reported_components",
                         "usage": {"total_tokens": 1, "prompt_tokens": 80001}})
        with Runtime(":memory:", drivers={"product": driver}) as rt:
            task = rt.create_task("失败响应计数")
            role = next(role for role in rt.roles if role.id == "product")
            event = rt._emit_driver_failure(task_id=task, role=role, attempt=1, exc=raised.exception, circuit_open=False)
            self.assertEqual(event.payload["usage_issue"], "total_below_reported_components")
            self.assertNotIn("SECRET", json.dumps(event.payload))

    def test_budget_uncertainty_only_new_proven_conflict_blocks_its_budgeted_channel(self):
        class Driver:
            token_budget = 100
        with Runtime(":memory:") as rt:
            task = rt.create_task("历史用量兼容")
            driver = Driver()
            for payload in (
                {"driver": "shared", "tokens": 0, "token_usage": "unavailable"},
                {"driver": "shared", "tokens": 0, "token_usage": "unavailable", "usage": {"total_tokens": 1, "prompt_tokens": 99}},
                {"driver": "shared", "token_usage": "unavailable", "usage_issue": "total_below_reported_components", "usage": {"total_tokens": 1, "prompt_tokens": 1}},
            ):
                rt.log.emit(actor=Actor.agent("product", "worker"), type="msg.said", task_id=task, payload=payload)
            rt.log.emit(actor=Actor.agent("product", "worker"), type="driver.usage_inconsistent", task_id=task,
                        payload={"driver": "shared", "usage_issue": "total_below_reported_components",
                                 "usage": {"total_tokens": 1, "prompt_tokens": 99}})
            self.assertIsNone(rt._budget_usage_uncertainty("shared", driver))
            rt.log.emit(actor=Actor.system("driver_gateway"), type="driver.usage_inconsistent", task_id=task,
                        payload={"driver": "shared", "token_usage": "unavailable", "usage_issue": "total_below_reported_components",
                                 "usage": {"total_tokens": 1, "prompt_tokens": 99}})
            self.assertIn("剩余额度", rt._budget_usage_uncertainty("shared", driver))
            self.assertIsNone(rt._budget_usage_uncertainty("different-channel", driver))
            driver.token_budget = None
            self.assertIsNone(rt._budget_usage_uncertainty("shared", driver))

    def test_text_budget_blocks_second_request_and_new_channel_recovers(self):
        body = {"choices": [{"message": {"content": '{"stance":"assert","text":"保留内容"}'}}],
                "usage": {"total_tokens": 1, "prompt_tokens": 80001}}
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def read(self, _limit): return json.dumps(body).encode()
        driver = OpenAICompatibleDriver("product", endpoint="https://example.test", model="fixture", api_key_env="CATER_USAGE_KEY", token_budget=80000)
        with Runtime(":memory:", drivers={"product": driver}) as rt, patch.dict("cater.drivers.os.environ", {"CATER_USAGE_KEY": "fixture"}), patch("cater.drivers._open_api_request", return_value=Response()) as transport:
            task_id = rt.create_task("预算暂停")
            role = next(role for role in rt.roles if role.id == "product")
            args = {"task_id": task_id, "task": rt.task(task_id), "role": role,
                    "driver": driver, "prompt": "fixture"}
            first = rt._complete_with_containment(**args)
            self.assertIn("保留内容", first.text)
            second = rt._complete_with_containment(**args)
            self.assertEqual(transport.call_count, 1)
            self.assertTrue(second.meta["contained"])
            self.assertFalse(second.meta["invocation_started"])
            self.assertIn("剩余额度", second.meta["display_reason"])
            recorded, = [e for e in rt.log.read(task_id=task_id) if e.type == "driver.usage_inconsistent"]
            self.assertTrue(recorded.payload["budget_enforced"])
            self.assertIsNone(rt._budget_usage_uncertainty("new-channel", driver))
            self.assertTrue(rt.verify_chain().ok)

    def test_cli_probe_records_conflict_and_budget_stops_next_probe(self):
        body = {"choices": [{"message": {"content": "ok"}}], "usage": {"total_tokens": 1, "prompt_tokens": 101}}
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def read(self, _limit): return json.dumps(body).encode()
        driver = OpenAICompatibleDriver("product", endpoint="https://example.test", model="fixture", api_key_env="CATER_USAGE_KEY", token_budget=100)
        with Runtime(":memory:", drivers={"product": driver}) as rt, patch("cater.cli.open_runtime", return_value=nullcontext(rt)), patch.dict("cater.drivers.os.environ", {"CATER_USAGE_KEY": "fixture"}), patch("cater.drivers._open_api_request", return_value=Response()) as transport:
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(cmd_probe_driver(argparse.Namespace(name="product")), 1)
                self.assertEqual(cmd_probe_driver(argparse.Namespace(name="product")), 1)
            self.assertEqual(transport.call_count, 1)
            self.assertIn("剩余额度", output.getvalue())
            event, = rt.log.iter_events(event_types=("driver.probe_called",))
            self.assertIsNone(event.task_id)
            self.assertEqual(event.payload["usage_issue"], "total_below_reported_components")
            self.assertTrue(rt.verify_chain().ok)

    def test_cli_untrusted_meta_cannot_create_native_block(self):
        class Driver:
            token_budget = 100
            def complete(self, **_kwargs):
                return DriverResponse(text="ok", tokens=0, meta={"mode": "openai_compatible", "token_usage": "unavailable",
                    "usage_issue": "total_below_reported_components", "usage": {"total_tokens": 1, "prompt_tokens": 101}})
        driver = Driver()
        with Runtime(":memory:", drivers={"product": driver}) as rt, patch("cater.cli.open_runtime", return_value=nullcontext(rt)), redirect_stdout(io.StringIO()):
            self.assertEqual(cmd_probe_driver(argparse.Namespace(name="product")), 0)
            self.assertEqual(list(rt.log.iter_events(event_types=("driver.usage_inconsistent",))), [])
            self.assertIsNone(rt._budget_usage_uncertainty("product", driver))

    def test_cli_probe_zero_budget_never_starts_and_reported_usage_counts_across_runtime(self):
        body = {"choices": [{"message": {"content": "ok"}}], "usage": {"total_tokens": 5, "prompt_tokens": 3, "completion_tokens": 2}}
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def read(self, _limit): return json.dumps(body).encode()
        driver = OpenAICompatibleDriver("product", endpoint="https://example.test", model="fixture", api_key_env="CATER_USAGE_KEY", token_budget=0)
        with tempfile.TemporaryDirectory() as temp, Runtime(Path(temp) / "probe.db", drivers={"product": driver}) as rt, patch("cater.cli.open_runtime", return_value=nullcontext(rt)), patch.dict("cater.drivers.os.environ", {"CATER_USAGE_KEY": "fixture"}), patch("cater.drivers._open_api_request", return_value=Response()) as transport, redirect_stdout(io.StringIO()):
            self.assertEqual(cmd_probe_driver(argparse.Namespace(name="product")), 1)
            self.assertEqual(transport.call_count, 0)
            self.assertEqual(list(rt.log.iter_events(event_types=("driver.probe_called",))), [])
            driver.token_budget = 5
            self.assertEqual(cmd_probe_driver(argparse.Namespace(name="product")), 0)
            task = rt.create_task("探测与协作共享预算")
            role = next(role for role in rt.roles if role.id == "product")
            blocked = rt._complete_with_containment(task_id=task, task=rt.task(task), role=role, driver=driver, prompt="fixture")
            self.assertFalse(blocked.meta["invocation_started"])
            self.assertEqual(cmd_probe_driver(argparse.Namespace(name="product")), 1)
            self.assertEqual(transport.call_count, 1)
            self.assertEqual(rt._reported_driver_tokens("product"), 5)
            with Runtime(Path(temp) / "probe.db", drivers={"product": driver}) as reopened:
                self.assertEqual(reopened._reported_driver_tokens("product"), 5)
                self.assertTrue(reopened.verify_chain().ok)

    def test_cli_unknown_usage_is_disclosed_as_soft_not_free(self):
        body = {"choices": [{"message": {"content": "ok"}}]}
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def read(self, _limit): return json.dumps(body).encode()
        driver = OpenAICompatibleDriver("product", endpoint="https://example.test", model="fixture", api_key_env="CATER_USAGE_KEY", token_budget=5)
        with Runtime(":memory:", drivers={"product": driver}) as rt, patch("cater.cli.open_runtime", return_value=nullcontext(rt)), patch.dict("cater.drivers.os.environ", {"CATER_USAGE_KEY": "fixture"}), patch("cater.drivers._open_api_request", return_value=Response()) as transport:
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(cmd_probe_driver(argparse.Namespace(name="product")), 0)
                self.assertEqual(cmd_probe_driver(argparse.Namespace(name="product")), 0)
            self.assertEqual(transport.call_count, 2)
            self.assertIn("用量未报告", output.getvalue())
            self.assertIn("不是硬上限", output.getvalue())
            self.assertEqual(rt._reported_driver_tokens("product"), 0)

    def test_opaque_probe_is_budget_checked_and_recorded_as_unknown(self):
        class Driver:
            token_budget = 0
            calls = 0
            def probe(self):
                self.calls += 1
                return True
        driver = Driver()
        with Runtime(":memory:", drivers={"product": driver}) as rt, patch("cater.cli.open_runtime", return_value=nullcontext(rt)):
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(cmd_probe_driver(argparse.Namespace(name="product")), 1)
                self.assertEqual(driver.calls, 0)
                driver.token_budget = 5
                self.assertEqual(cmd_probe_driver(argparse.Namespace(name="product")), 0)
            self.assertEqual(driver.calls, 1)
            event, = rt.log.iter_events(event_types=("driver.probe_called",))
            self.assertEqual(event.payload["token_usage"], "unavailable")
            self.assertIn("不是硬上限", output.getvalue())

    def test_cli_probe_malformed_response_keeps_reported_counts(self):
        body = {"choices": [], "usage": {"total_tokens": 5, "prompt_tokens": 3, "completion_tokens": 2}}
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def read(self, _limit): return json.dumps(body).encode()
        driver = OpenAICompatibleDriver("product", endpoint="https://example.test", model="fixture", api_key_env="CATER_USAGE_KEY", token_budget=5)
        with Runtime(":memory:", drivers={"product": driver}) as rt, patch("cater.cli.open_runtime", return_value=nullcontext(rt)), patch.dict("cater.drivers.os.environ", {"CATER_USAGE_KEY": "fixture"}), patch("cater.drivers._open_api_request", return_value=Response()) as transport:
            with self.assertRaises(DriverProtocolError):
                cmd_probe_driver(argparse.Namespace(name="product"))
            with redirect_stdout(io.StringIO()):
                self.assertEqual(cmd_probe_driver(argparse.Namespace(name="product")), 1)
            event, = rt.log.iter_events(event_types=("driver.probe_called",))
            self.assertEqual((event.payload["status"], event.payload["tokens"], event.payload["token_usage"]), ("failed", 5, "reported"))
            self.assertEqual(rt._reported_driver_tokens("product"), 5)
            self.assertEqual(transport.call_count, 1)

    def test_two_runtimes_can_already_be_in_flight_when_conflict_is_recorded(self):
        body = {"choices": [{"message": {"content": "ok"}}], "usage": {"total_tokens": 1, "prompt_tokens": 101}}
        barrier = threading.Barrier(2)
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def read(self, _limit): return json.dumps(body).encode()
        def transport(*_args, **_kwargs):
            barrier.wait(timeout=10)
            return Response()
        def driver():
            return OpenAICompatibleDriver("product", endpoint="https://example.test", model="fixture", api_key_env="CATER_USAGE_KEY", token_budget=100)
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "fixture.db"
            with Runtime(db, drivers={"product": driver()}) as rt:
                task_id = rt.create_task("并发预算边界")
            def invoke():
                native = driver()
                with Runtime(db, drivers={"product": native}) as rt:
                    role = next(role for role in rt.roles if role.id == "product")
                    result = rt._complete_with_containment(task_id=task_id, task=rt.task(task_id), role=role,
                                                           driver=native, prompt="fixture")
                    return result.text
            with patch.dict("cater.drivers.os.environ", {"CATER_USAGE_KEY": "fixture"}), patch("cater.drivers._open_api_request", side_effect=transport), ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _index: invoke(), range(2)))
            self.assertEqual(results, ["ok", "ok"])
            with Runtime(db, drivers={"product": driver()}) as rt:
                self.assertEqual(len(list(rt.log.iter_events(event_types=("driver.usage_inconsistent",)))), 2)
                self.assertTrue(rt._budget_usage_uncertainty("product", rt.drivers["product"]))
                self.assertTrue(rt.verify_chain().ok)
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
