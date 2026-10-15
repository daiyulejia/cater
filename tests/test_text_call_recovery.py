from __future__ import annotations

import io
import json
import os
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import cater.runtime as runtime_module
from cater.anthropic import AnthropicMessagesDriver
from cater.drivers import (CliDriver, DriverResponse, DriverUnavailable, OpenAICompatibleDriver,
                           TextTransportFailure, _text_only_request)
from cater.events import Actor
from cater.runtime import Runtime
from test_work import WorkDriver, item


class CustomTextDriver:
    name = "dev"

    def complete(self, **kwargs):
        raise TextTransportFailure("connection failed")


class AnthropicSubclass(AnthropicMessagesDriver):
    pass


class OpenAISubclass(OpenAICompatibleDriver):
    pass


class DerivedTextTransportFailure(TextTransportFailure):
    pass


class TextCallRecoveryTests(unittest.TestCase):
    def runtime(self, cls=AnthropicMessagesDriver, *, driver=None):
        driver = driver or cls("dev", endpoint="https://fixture.invalid/v1/messages", model="fixture",
                               api_key_env="CATER_RECOVERY_FIXTURE", timeout=90)
        rt = Runtime(":memory:", drivers={"dev": driver, "test": WorkDriver()},
                     driver_attempts=9, circuit_breaker_threshold=10)
        self.addCleanup(rt.close)
        task = rt.create_task("Produce a small text artifact")
        rt.plan_work(task, [item("draft")])
        return rt, task, driver

    def transport(self, cls):
        return "cater.anthropic._open_api_request" if issubclass(cls, AnthropicMessagesDriver) else "cater.drivers._open_api_request"

    def current(self, rt, task):
        return rt.work.view(task)["items"][0]

    def success(self, cls):
        text = json.dumps({"claim": "Draft ready", "text": "Draft ready", "stance": "assert",
                           "deliverables": [{"title": "Draft", "summary": "Small document", "content": "A short draft.",
                                             "format": "markdown", "filename": "draft.md"}]})
        if cls is AnthropicMessagesDriver:
            return {"type": "message", "stop_reason": "end_turn", "content": [{"type": "text", "text": text}],
                    "usage": {"input_tokens": 4, "output_tokens": 6}}
        return {"choices": [{"message": {"content": text}}], "usage": {"total_tokens": 10}}

    @patch.dict(os.environ, {"CATER_RECOVERY_FIXTURE": "synthetic-key"})
    def test_transport_failure_can_resume_but_never_retries_within_one_activity(self):
        for cls in (AnthropicMessagesDriver, OpenAICompatibleDriver):
            for error in (TimeoutError("synthetic"), URLError(TimeoutError("synthetic")),
                          URLError(ConnectionRefusedError("synthetic")),
                          *(HTTPError("https://fixture.invalid", code, "synthetic", {}, None)
                            for code in (408, 500, 502, 503, 504))):
                with self.subTest(cls=cls.__name__, error=type(error).__name__):
                    rt, task, _ = self.runtime(cls)
                    with patch(self.transport(cls), side_effect=error) as opened:
                        rt.collaborate(task)
                        opened.assert_called_once()
                    current = self.current(rt, task)
                    self.assertEqual(current["status"], "ready")
                    self.assertFalse(current["in_flight"])
                    self.assertIsNone(current["submission"])
                    for text in ("本地无工具副作用", "远端是否计费未知", "等待用户继续", "不会自动重试"):
                        self.assertIn(text, current["reason"])
                    claims = list(rt.log.iter_events(task_id=task, event_types=("work.claimed",)))
                    self.assertEqual(len(claims), 1)
                    failed = list(rt.log.iter_events(task_id=task, event_types=("work.failed",)))
                    self.assertFalse(failed[0].payload["uncertain"])
                    self.assertEqual(rt.driver_attempts, 9)
                    rt.command(task, "继续")
                    with patch(self.transport(cls), return_value=io.BytesIO(json.dumps(self.success(cls)).encode())) as opened:
                        rt.collaborate(task)
                        opened.assert_called_once()
                    self.assertTrue(rt.work.view(task)["stage_accepted"])
                    self.assertEqual(len(list(rt.log.iter_events(task_id=task, event_types=("work.claimed",)))), 2)
                    self.assertTrue(rt.verify_chain().ok)

    @patch.dict(os.environ, {"CATER_RECOVERY_FIXTURE": "synthetic-key"})
    def test_response_read_timeout_is_also_a_transport_failure(self):
        for cls in (AnthropicMessagesDriver, OpenAICompatibleDriver):
            with self.subTest(cls=cls.__name__):
                rt, task, _ = self.runtime(cls)
                stream = io.BytesIO()
                with patch.object(stream, "read", side_effect=TimeoutError("synthetic")), \
                        patch(self.transport(cls), return_value=stream) as opened:
                    rt.collaborate(task)
                    opened.assert_called_once()
                self.assertEqual(self.current(rt, task)["status"], "ready")

    @patch.dict(os.environ, {"CATER_RECOVERY_FIXTURE": "synthetic-key"})
    def test_subclasses_custom_and_cli_drivers_remain_uncertain(self):
        for cls in (AnthropicSubclass, OpenAISubclass):
            with self.subTest(cls=cls.__name__):
                rt, task, _ = self.runtime(cls)
                with patch(self.transport(cls), side_effect=TimeoutError("synthetic")) as opened:
                    rt.collaborate(task)
                    opened.assert_called_once()
                self.assertEqual(self.current(rt, task)["status"], "uncertain")
        rt, task, _ = self.runtime(driver=CustomTextDriver())
        rt.collaborate(task)
        self.assertEqual(self.current(rt, task)["status"], "uncertain")
        cli = CliDriver("dev", ["never-launched"])
        rt, task, _ = self.runtime(driver=cli)
        with patch.object(cli, "complete", side_effect=TimeoutError("synthetic")) as called:
            rt.collaborate(task)
            called.assert_called_once()
        self.assertEqual(self.current(rt, task)["status"], "uncertain")

    @patch.dict(os.environ, {"CATER_RECOVERY_FIXTURE": "synthetic-key"})
    def test_http_protocol_and_incomplete_responses_are_not_recovery_signals(self):
        for cls in (AnthropicMessagesDriver, OpenAICompatibleDriver):
            for error in (*(HTTPError("https://fixture.invalid", code, "synthetic", {}, None)
                           for code in (400, 401, 403, 404)),
                          DriverUnavailable("API request timed out"), ValueError("invalid headers")):
                with self.subTest(cls=cls.__name__, error=type(error).__name__):
                    rt, task, _ = self.runtime(cls)
                    with patch(self.transport(cls), side_effect=error):
                        rt.collaborate(task)
                    self.assertEqual(self.current(rt, task)["status"], "uncertain")
            for raw in (b"not-json", b"{}"):
                with self.subTest(cls=cls.__name__, raw=raw):
                    rt, task, _ = self.runtime(cls)
                    with patch(self.transport(cls), return_value=io.BytesIO(raw)):
                        rt.collaborate(task)
                    self.assertEqual(self.current(rt, task)["status"], "uncertain")
        for reason, content in (("max_tokens", [{"type": "text", "text": "truncated"}]),
                                ("tool_use", [{"type": "tool_use", "input": {"command": "not-executed"}}])):
            with self.subTest(reason=reason):
                rt, task, _ = self.runtime()
                data = {"type": "message", "stop_reason": reason, "content": content}
                with patch(self.transport(AnthropicMessagesDriver), return_value=io.BytesIO(json.dumps(data).encode())):
                    rt.collaborate(task)
                self.assertEqual(self.current(rt, task)["status"], "uncertain")

    @patch.dict(os.environ, {"CATER_RECOVERY_FIXTURE": "synthetic-key"})
    def test_existing_artifact_or_check_evidence_prevents_safe_recovery(self):
        for event_type in ("deliverable.published", "work.check_started", "verification.completed"):
            with self.subTest(event_type=event_type):
                rt, task, _ = self.runtime()

                def fail_with_evidence(*args, **kwargs):
                    current = self.current(rt, task)
                    rt.log.emit(type=event_type, task_id=task, actor=Actor.system("local_verifier"),
                                payload={"work_item_id": "draft", "attempt_id": current["attempt_id"]})
                    raise TimeoutError("synthetic")

                with patch(self.transport(AnthropicMessagesDriver), side_effect=fail_with_evidence):
                    rt.collaborate(task)
                self.assertEqual(self.current(rt, task)["status"], "uncertain")

    def test_returned_containment_metadata_and_model_fields_cannot_forge_recovery(self):
        for cls in (AnthropicMessagesDriver, OpenAICompatibleDriver):
            with self.subTest(cls=cls.__name__):
                rt, task, driver = self.runtime(cls)
                response = DriverResponse(json.dumps({"claim": "failed", "safe_to_retry": True,
                                                       "text_transport_failure": True}),
                                          meta={"contained": True, "invocation_started": True,
                                                "_text_transport_failure": runtime_module._TEXT_TRANSPORT_FAILURE})
                with patch.object(driver, "complete", return_value=response):
                    rt.collaborate(task)
                self.assertEqual(self.current(rt, task)["status"], "uncertain")

    @patch.dict(os.environ, {"CATER_RECOVERY_FIXTURE": "synthetic-key"})
    def test_tool_enabled_request_shape_does_not_inherit_text_recovery(self):
        for cls in (AnthropicMessagesDriver, OpenAICompatibleDriver):
            for error in (TimeoutError("synthetic"),
                          HTTPError("https://fixture.invalid", 502, "synthetic", {}, None)):
                with self.subTest(cls=cls.__name__, error=type(error).__name__):
                    rt, task, _ = self.runtime(cls)
                    guard = self.transport(cls).rsplit(".", 1)[0] + "._text_only_request"
                    with patch(guard, return_value=False) as checked, \
                            patch(self.transport(cls), side_effect=error):
                        rt.collaborate(task)
                    checked.assert_called_once()
                    self.assertEqual(self.current(rt, task)["status"], "uncertain")

    def test_request_capability_guard_rejects_tools_history_and_unknown_features(self):
        body = {"model": "fixture", "messages": [{"role": "user", "content": "hello"}]}
        self.assertTrue(_text_only_request(body))
        for field in ("tools", "tool_choice", "functions", "web_search_options", "future_capability"):
            with self.subTest(field=field):
                self.assertFalse(_text_only_request({**body, field: []}))
        for message in ({"role": "tool", "content": "result"},
                        {"role": "assistant", "content": "tool request"},
                        {"role": "user", "content": [{"type": "tool_result"}]},
                        {"role": "user", "content": "hello", "tool_calls": []}):
            with self.subTest(message=message):
                self.assertFalse(_text_only_request({**body, "messages": [message]}))

    @patch.dict(os.environ, {"CATER_RECOVERY_FIXTURE": "synthetic-key"})
    def test_adapters_explicitly_attest_only_text_network_failures(self):
        for cls in (AnthropicMessagesDriver, OpenAICompatibleDriver):
            for error in (TimeoutError("synthetic"), URLError("synthetic"), OSError("synthetic")):
                with self.subTest(cls=cls.__name__, error=type(error).__name__):
                    _, _, driver = self.runtime(cls)
                    with patch(self.transport(cls), side_effect=error), self.assertRaises(TextTransportFailure) as caught:
                        driver.complete(role="worker", prompt="synthetic", context={})
                    self.assertIs(type(caught.exception), TextTransportFailure)

    @patch.dict(os.environ, {"CATER_RECOVERY_FIXTURE": "synthetic-key"})
    def test_exception_subclasses_and_replaced_methods_do_not_qualify(self):
        for cls in (AnthropicMessagesDriver, OpenAICompatibleDriver):
            with self.subTest(cls=cls.__name__):
                rt, task, _ = self.runtime(cls)
                with patch(self.transport(cls), side_effect=DerivedTextTransportFailure("synthetic")):
                    rt.collaborate(task)
                self.assertEqual(self.current(rt, task)["status"], "uncertain")
                rt, task, driver = self.runtime(cls)
                with patch.object(driver, "complete", side_effect=TextTransportFailure("synthetic")):
                    rt.collaborate(task)
                self.assertEqual(self.current(rt, task)["status"], "uncertain")

    def test_gateway_also_clears_returned_marker_outside_work_calls(self):
        rt, task, driver = self.runtime()
        with patch.object(driver, "complete", return_value=DriverResponse("{}", meta={
                "_text_transport_failure": runtime_module._TEXT_TRANSPORT_FAILURE})):
            response = rt._complete_with_containment(task_id=task, task=rt.task(task),
                                                    role=rt._role_by_id("dev"), driver=driver, prompt="synthetic")
        self.assertIsNone(response.meta["_text_transport_failure"])

    @patch.dict(os.environ, {"CATER_RECOVERY_FIXTURE": "synthetic-key"})
    def test_private_sentinel_stays_out_of_events_brief_and_later_model_context(self):
        for cls in (AnthropicMessagesDriver, OpenAICompatibleDriver):
            with self.subTest(cls=cls.__name__):
                rt, task, driver = self.runtime(cls)
                collect = rt._collect_utterances
                earlier = []

                def capture(*args, **kwargs):
                    outputs = collect(*args, **kwargs)
                    earlier.extend(outputs)
                    return outputs

                with patch.object(rt, "_collect_utterances", side_effect=capture), \
                        patch(self.transport(cls), side_effect=TimeoutError("synthetic")):
                    result = rt.collaborate(task)
                self.assertIs(earlier[0]["text_transport_failure"], runtime_module._TEXT_TRANSPORT_FAILURE)
                public = json.dumps({"result": result, "events": [e.payload for e in rt.log.read(task_id=task)]})
                self.assertNotIn("text_transport_failure", public)

                def later_call(**kwargs):
                    self.assertNotIn("text_transport_failure", kwargs["prompt"])
                    self.assertNotIn("text_transport_failure", json.dumps(kwargs["context"]))
                    return DriverResponse('{"claim":"Received","text":"Received","stance":"assert"}')

                with patch.object(driver, "complete", side_effect=later_call) as called:
                    collect(task, rt.task(task), [rt._role_by_id("dev")], {}, earlier=earlier)
                    called.assert_called_once()

    def test_interruption_outside_transport_does_not_become_safe_recovery(self):
        rt, task, _ = self.runtime()
        with patch.object(rt, "_collect_utterances", side_effect=RuntimeError("interrupted")), \
                self.assertRaisesRegex(RuntimeError, "interrupted"):
            rt.collaborate(task)
        self.assertEqual(self.current(rt, task)["status"], "uncertain")


if __name__ == "__main__":
    unittest.main()
