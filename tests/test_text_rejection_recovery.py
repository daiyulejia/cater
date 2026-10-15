from __future__ import annotations

import io
import json
import os
import socket
import subprocess
import traceback
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import cater.runtime as runtime_module
from cater.anthropic import AnthropicMessagesDriver
from cater.availability import team_availability
from cater.drivers import (CliDriver, DriverResponse, DriverUnavailable,
                           OpenAICompatibleDriver, TextRequestRejected)
from cater.events import Actor
from cater.runtime import DriverHealth, Runtime
from test_work import WorkDriver, item


class DerivedAnthropic(AnthropicMessagesDriver):
    pass


class DerivedOpenAI(OpenAICompatibleDriver):
    pass


class DerivedRejection(TextRequestRejected):
    pass


class CustomRejectionDriver:
    name = "dev"

    def complete(self, **kwargs):
        raise TextRequestRejected("dev API request failed with HTTP 429", status_code=429)


class TextRejectionRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {"CATER_REJECTION_FIXTURE": "synthetic-key"}))
        self.enterContext(patch.object(socket, "socket", side_effect=AssertionError("network forbidden")))
        self.enterContext(patch.object(subprocess, "Popen", side_effect=AssertionError("process forbidden")))

    def runtime(self, cls=AnthropicMessagesDriver, *, driver=None, predecessor=False):
        driver = driver or cls("dev", endpoint="https://fixture.invalid/v1/messages", model="fixture",
                               api_key_env="CATER_REJECTION_FIXTURE")
        rt = Runtime(":memory:", drivers={"dev": driver, "test": WorkDriver(), "design": WorkDriver()},
                     driver_attempts=9)
        self.addCleanup(rt.close)
        task = rt.create_task("Produce a small document")
        items = ([item("input", owner="design"), item("draft", deps=["input"])] if predecessor else
                 [item("draft")])
        rt.plan_work(task, items)
        return rt, task, driver

    def transport(self, cls):
        return ("cater.anthropic._open_api_request" if issubclass(cls, AnthropicMessagesDriver) else
                "cater.drivers._open_api_request")

    def rejection(self, status=429):
        return HTTPError("https://fixture.invalid/?private=PRIVATE_URL", status, "PRIVATE_REASON", {},
                         io.BytesIO(b"PRIVATE_BODY"))

    def current(self, rt, task):
        return next(row for row in rt.work.view(task)["items"] if row["id"] == "draft")

    def success(self, cls):
        text = json.dumps({"claim": "Draft ready", "text": "Draft ready", "stance": "assert",
                           "deliverables": [{"title": "Draft", "summary": "Small document", "content": "A short draft.",
                                             "format": "markdown", "filename": "draft.md"}]})
        raw = ({"type": "message", "stop_reason": "end_turn", "content": [{"type": "text", "text": text}],
                "usage": {"input_tokens": 4, "output_tokens": 6}} if cls is AnthropicMessagesDriver else
               {"choices": [{"message": {"content": text}}], "usage": {"total_tokens": 10}})
        return io.BytesIO(json.dumps(raw).encode())

    def test_original_text_adapters_raise_distinct_sanitized_429_rejection(self):
        for cls in (AnthropicMessagesDriver, OpenAICompatibleDriver):
            with self.subTest(cls=cls.__name__):
                _, _, driver = self.runtime(cls)
                with patch(self.transport(cls), side_effect=self.rejection()) as opened:
                    with self.assertRaises(TextRequestRejected) as caught:
                        driver.complete(role="worker", prompt="fixture", context={})
                opened.assert_called_once()
                self.assertIs(type(caught.exception), TextRequestRejected)
                self.assertEqual(caught.exception.status_code, 429)
                self.assertEqual(str(caught.exception), "dev API request failed with HTTP 429")
                self.assertNotIn("PRIVATE", "".join(traceback.format_exception(caught.exception)))

    def test_429_keeps_reason_and_resumes_only_in_next_explicit_activity(self):
        for cls in (AnthropicMessagesDriver, OpenAICompatibleDriver):
            with self.subTest(cls=cls.__name__):
                rt, task, _ = self.runtime(cls)
                with patch(self.transport(cls), side_effect=self.rejection()) as opened:
                    rt.collaborate(task)
                    opened.assert_called_once()
                current = self.current(rt, task)
                self.assertEqual(current["status"], "ready")
                self.assertFalse(current["in_flight"])
                self.assertIsNone(current["submission"])
                for text in ("HTTP 429", "本地无工具副作用", "远端是否计费未知", "等待用户继续", "不会自动重试"):
                    self.assertIn(text, current["reason"])
                before = list(rt.log.iter_events(task_id=task, event_types=("work.failed",)))
                self.assertEqual(len(before), 1)
                self.assertFalse(before[0].payload["uncertain"])
                self.assertEqual(len(list(rt.log.iter_events(task_id=task, event_types=("work.claimed",)))), 1)
                failure = list(rt.log.iter_events(task_id=task, event_types=("driver.failed",)))[0]
                self.assertEqual(failure.payload["error_type"], "TextRequestRejected")
                self.assertIn("HTTP 429", failure.payload["message"])
                rt.command(task, "继续")
                with patch(self.transport(cls), return_value=self.success(cls)) as opened:
                    rt.collaborate(task)
                    opened.assert_called_once()
                self.assertTrue(rt.work.view(task)["stage_accepted"])
                self.assertEqual(list(rt.log.iter_events(task_id=task, event_types=("work.failed",))), before)
                self.assertTrue(rt.verify_chain().ok)

    def test_repeated_explicit_429_attempts_do_not_open_circuit(self):
        for cls in (AnthropicMessagesDriver, OpenAICompatibleDriver):
            with self.subTest(cls=cls.__name__):
                rt, task, _ = self.runtime(cls)
                for attempt in range(3):
                    if attempt:
                        rt.command(task, "继续")
                    with patch(self.transport(cls), side_effect=self.rejection()) as opened:
                        rt.collaborate(task)
                        opened.assert_called_once()
                    self.assertEqual(self.current(rt, task)["status"], "ready")
                    self.assertFalse(rt._is_circuit_open("dev"))
                rt.command(task, "继续")
                with patch(self.transport(cls), return_value=self.success(cls)) as opened:
                    rt.collaborate(task)
                    opened.assert_called_once()
                self.assertTrue(rt.work.view(task)["stage_accepted"])

    def test_gateway_does_not_retry_429_even_without_work_context(self):
        for cls in (AnthropicMessagesDriver, OpenAICompatibleDriver):
            with self.subTest(cls=cls.__name__):
                rt, task, driver = self.runtime(cls)
                with patch(self.transport(cls), side_effect=self.rejection()) as opened:
                    response = rt._complete_with_containment(task_id=task, task=rt.task(task),
                        role=rt._role_by_id("dev"), driver=driver, prompt="fixture")
                    opened.assert_called_once()
                self.assertTrue(response.meta["contained"])
                self.assertIs(response.meta["_text_request_rejected"], runtime_module._TEXT_REQUEST_REJECTED)

    def test_accepted_predecessor_is_preserved_during_rejection_and_resume(self):
        rt, task, _ = self.runtime(predecessor=True)
        with patch(self.transport(AnthropicMessagesDriver), side_effect=self.rejection()):
            rt.collaborate(task)
        predecessor = rt.work.view(task)["items"][0]
        self.assertEqual(predecessor["status"], "accepted")
        self.assertEqual(self.current(rt, task)["status"], "ready")
        rt.command(task, "继续")
        with patch(self.transport(AnthropicMessagesDriver), return_value=self.success(AnthropicMessagesDriver)):
            rt.collaborate(task)
        self.assertEqual(rt.work.view(task)["items"][0], predecessor)
        self.assertTrue(rt.work.view(task)["stage_accepted"])

    def test_other_http_statuses_keep_uncertain_classification(self):
        for cls in (AnthropicMessagesDriver, OpenAICompatibleDriver):
            for status in (400, 401, 403, 408, 409, 422, 500, 503, 504):
                with self.subTest(cls=cls.__name__, status=status):
                    rt, task, _ = self.runtime(cls)
                    with patch(self.transport(cls), side_effect=self.rejection(status)) as opened:
                        rt.collaborate(task)
                        opened.assert_called_once()
                    self.assertEqual(self.current(rt, task)["status"], "uncertain")

    def test_tool_enabled_requests_cannot_get_rejection_recovery(self):
        for cls in (AnthropicMessagesDriver, OpenAICompatibleDriver):
            with self.subTest(cls=cls.__name__):
                rt, task, _ = self.runtime(cls)
                guard = self.transport(cls).rsplit(".", 1)[0] + "._text_only_request"
                with patch(guard, return_value=False) as checked, \
                        patch(self.transport(cls), side_effect=self.rejection()) as opened:
                    rt.collaborate(task)
                    checked.assert_called_once()
                    opened.assert_called_once()
                self.assertEqual(self.current(rt, task)["status"], "uncertain")

    def test_subclasses_and_custom_drivers_cannot_attest_safe_rejection(self):
        for cls in (DerivedAnthropic, DerivedOpenAI):
            with self.subTest(cls=cls.__name__):
                rt, task, _ = self.runtime(cls)
                with patch(self.transport(cls), side_effect=self.rejection()) as opened:
                    rt.collaborate(task)
                    opened.assert_called_once()
                self.assertEqual(self.current(rt, task)["status"], "uncertain")
        rt, task, _ = self.runtime(driver=CustomRejectionDriver())
        rt.collaborate(task)
        self.assertEqual(self.current(rt, task)["status"], "uncertain")

    def test_replaced_methods_and_exception_subclasses_cannot_qualify(self):
        for cls in (AnthropicMessagesDriver, OpenAICompatibleDriver):
            with self.subTest(cls=cls.__name__):
                rt, task, driver = self.runtime(cls)
                with patch.object(driver, "complete", side_effect=TextRequestRejected("HTTP 429", status_code=429)) as called:
                    rt.collaborate(task)
                    called.assert_called_once()
                self.assertEqual(self.current(rt, task)["status"], "uncertain")
                for error in (DerivedRejection("HTTP 429", status_code=429),
                              TextRequestRejected("HTTP 403", status_code=403)):
                    rt, task, _ = self.runtime(cls)
                    with patch(self.transport(cls), side_effect=error):
                        rt.collaborate(task)
                    self.assertEqual(self.current(rt, task)["status"], "uncertain")

    def test_cli_unknown_is_not_cleared_by_http_text_or_later_text_adapter(self):
        cli = CliDriver("dev", ["never-launched"])
        rt, task, _ = self.runtime(driver=cli)
        with patch.object(cli, "complete", side_effect=TextRequestRejected("HTTP 429", status_code=429)) as called:
            rt.collaborate(task)
            called.assert_called_once()
        before = list(rt.log.iter_events(task_id=task, event_types=("work.failed",)))
        self.assertEqual(self.current(rt, task)["status"], "uncertain")
        replacement = AnthropicMessagesDriver("dev", endpoint="https://fixture.invalid/v1/messages", model="fixture",
                                             api_key_env="CATER_REJECTION_FIXTURE")
        rt.drivers["dev"] = replacement
        rt.command(task, "继续")
        with patch(self.transport(AnthropicMessagesDriver), side_effect=self.rejection()) as opened:
            rt.collaborate(task)
            opened.assert_not_called()
        self.assertEqual(self.current(rt, task)["status"], "uncertain")
        self.assertEqual(list(rt.log.iter_events(task_id=task, event_types=("work.failed",))), before)

    def test_arbitrary_429_error_text_does_not_authorize_recovery(self):
        for cls in (AnthropicMessagesDriver, OpenAICompatibleDriver):
            with self.subTest(cls=cls.__name__):
                rt, task, _ = self.runtime(cls)
                with patch(self.transport(cls), side_effect=DriverUnavailable("API request failed with HTTP 429")):
                    rt.collaborate(task)
                self.assertEqual(self.current(rt, task)["status"], "uncertain")

    def test_existing_artifact_or_check_evidence_prevents_recovery(self):
        for cls in (AnthropicMessagesDriver, OpenAICompatibleDriver):
            for event_type in ("deliverable.published", "work.check_started", "verification.completed"):
                with self.subTest(cls=cls.__name__, event_type=event_type):
                    rt, task, _ = self.runtime(cls)
                    def reject_with_evidence(*args, **kwargs):
                        current = self.current(rt, task)
                        rt.log.emit(type=event_type, task_id=task, actor=Actor.system("local_verifier"),
                                    payload={"work_item_id": "draft", "attempt_id": current["attempt_id"]})
                        raise self.rejection()
                    with patch(self.transport(cls), side_effect=reject_with_evidence):
                        rt.collaborate(task)
                    self.assertEqual(self.current(rt, task)["status"], "uncertain")

    def test_driver_metadata_and_model_fields_cannot_forge_rejection(self):
        for cls in (AnthropicMessagesDriver, OpenAICompatibleDriver):
            with self.subTest(cls=cls.__name__):
                rt, task, driver = self.runtime(cls)
                response = DriverResponse(json.dumps({"claim": "HTTP 429", "safe_to_retry": True,
                                                     "text_request_rejected": True}),
                    meta={"contained": True, "invocation_started": True,
                          "_text_request_rejected": runtime_module._TEXT_REQUEST_REJECTED})
                with patch.object(driver, "complete", return_value=response):
                    rt.collaborate(task)
                self.assertEqual(self.current(rt, task)["status"], "uncertain")
                with patch.object(driver, "complete", return_value=response):
                    returned = rt._complete_with_containment(task_id=task, task=rt.task(task),
                        role=rt._role_by_id("dev"), driver=driver, prompt="fixture")
                self.assertIsNone(returned.meta["_text_request_rejected"])

    def test_private_marker_and_raw_http_details_stay_out_of_public_records(self):
        rt, task, _ = self.runtime()
        collect = rt._collect_utterances
        outputs = []
        def capture(*args, **kwargs):
            rows = collect(*args, **kwargs)
            outputs.extend(rows)
            return rows
        with patch.object(rt, "_collect_utterances", side_effect=capture), \
                patch(self.transport(AnthropicMessagesDriver), side_effect=self.rejection()):
            result = rt.collaborate(task)
        self.assertIs(outputs[0]["text_request_rejected"], runtime_module._TEXT_REQUEST_REJECTED)
        public = json.dumps({"result": result, "events": [e.payload for e in rt.log.read(task_id=task)]})
        self.assertNotIn("text_request_rejected", public)
        self.assertNotIn("PRIVATE", public)
        def later_call(**kwargs):
            self.assertNotIn("text_request_rejected", kwargs["prompt"])
            self.assertNotIn("text_request_rejected", json.dumps(kwargs["context"]))
            return DriverResponse('{"claim":"Received","text":"Received","stance":"assert"}')
        with patch.object(rt.drivers["dev"], "complete", side_effect=later_call) as called:
            collect(task, rt.task(task), [rt._role_by_id("dev")], {}, earlier=outputs)
            called.assert_called_once()

    def test_rejection_updates_availability_without_retaining_success(self):
        for cls in (AnthropicMessagesDriver, OpenAICompatibleDriver):
            with self.subTest(cls=cls.__name__):
                rt, task, _ = self.runtime(cls)
                rt._driver_health.record_success("dev")
                before = next(row for row in team_availability(rt)["members"] if row["role_id"] == "dev")
                self.assertEqual(before["status"], "available")
                with patch(self.transport(cls), side_effect=self.rejection()):
                    rt.collaborate(task)
                self.assertEqual(rt._driver_health.recent_state("dev"), "failed")
                after = next(row for row in team_availability(rt)["members"] if row["role_id"] == "dev")
                self.assertEqual((after["status"], after["reason"]), ("unverified", "recent_failure"))
                self.assertFalse(team_availability(rt)["all_available"])

    def test_record_rejection_refreshes_observation_without_changing_counter(self):
        health = DriverHealth()
        health.record_success("dev")
        with patch("cater.runtime.time.monotonic", return_value=1000):
            health.record_rejection("dev")
        with patch("cater.runtime.time.monotonic", return_value=1001):
            self.assertEqual(health.recent_state("dev"), "failed")
        self.assertFalse(health.is_circuit_open("dev", 1))
        self.assertEqual(health.record_failure("dev"), 1)
        health.record_rejection("dev")
        self.assertTrue(health.is_circuit_open("dev", 1))
        self.assertFalse(health.is_circuit_open("dev", 2))
        self.assertEqual(health.record_failure("dev"), 2)

    def test_rejection_does_not_clear_preexisting_failure_counter(self):
        rt, task, _ = self.runtime()
        rt._driver_health.record_failure("dev")
        with patch(self.transport(AnthropicMessagesDriver), side_effect=self.rejection()):
            rt.collaborate(task)
        self.assertTrue(rt._driver_health.is_circuit_open("dev", 1))
        self.assertFalse(rt._is_circuit_open("dev"))
        self.assertEqual(rt._driver_health.record_failure("dev"), 2)
        self.assertTrue(rt._is_circuit_open("dev"))


if __name__ == "__main__":
    unittest.main()
