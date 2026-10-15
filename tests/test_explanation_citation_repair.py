"""Offline, transport-mocked regressions for the question #406 citation failure."""

import copy
import io
import json
import os
import socket
import subprocess
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from cater.anthropic import AnthropicMessagesDriver
from cater.drivers import CliDriver, DriverResponse, OpenAICompatibleDriver
import test_named_dialogue_regression as fixtures


class ExplanationCitationRepairTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {"CATER_CITATION_FIXTURE": "synthetic-key"}))
        self.enterContext(patch.object(socket, "socket", side_effect=AssertionError("network forbidden")))
        self.enterContext(patch.object(subprocess, "Popen", side_effect=AssertionError("process forbidden")))

    def prepare(self, cls=AnthropicMessagesDriver, driver=None):
        fixture = fixtures.NamedDialogueRegressionTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        self.rt, self.task = fixture.rt, fixture.task
        self.driver = driver or cls("offline", endpoint="https://fixture.invalid/v1/messages", model="fixture",
                                    api_key_env="CATER_CITATION_FIXTURE")
        self.rt.drivers["offline"] = self.driver
        self.asked = self.rt.questions.ask(self.task, "@三角 你为什么还没开始？", request_id="citation-fixture")
        source = self.asked.payload["basis"]["source_seqs"][0]["seq"]
        self.good = {"status": "explained", "answer": "这是整体任务目标，不能据此确认我已执行。",
                     "limits": ["没有执行记录。"], "evidence": [{"source_seq": source, "quote": "制作五子棋"}]}
        self.bad = copy.deepcopy(self.good)
        self.bad["evidence"][0]["quote"] = "制作一个五子棋游戏"
        self.transport = "cater.anthropic._open_api_request" if cls is AnthropicMessagesDriver else "cater.drivers._open_api_request"
        self.cls = cls

    def wire(self, raw, *, reported=True):
        text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
        if self.cls is AnthropicMessagesDriver:
            body = {"type": "message", "content": [{"type": "text", "text": text}], "stop_reason": "end_turn"}
            usage = {"input_tokens": 7, "output_tokens": 3}
        else:
            body = {"choices": [{"message": {"content": text}, "finish_reason": "stop"}]}
            usage = {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}
        if reported:
            body["usage"] = usage
        return io.BytesIO(json.dumps(body).encode())

    def events(self, kind):
        return [e for e in self.rt.log.read(task_id=self.task) if e.type == kind]

    def run_responses(self, responses):
        with patch(self.transport, side_effect=responses) as opened:
            self.assertTrue(self.rt.questions.answer_next(self.task))
            self.assertFalse(self.rt.questions.answer_next(self.task))
        self.assertEqual(len(self.events("question.started")), 1)
        self.assertEqual(len(self.events("question.finished")), 1)
        self.assertTrue(self.rt.verify_chain().ok)
        return opened.call_count, self.events("question.finished")[0].payload

    def test_one_correction_same_question_sources_and_two_accounted_calls(self):
        for cls in (AnthropicMessagesDriver, OpenAICompatibleDriver):
            with self.subTest(adapter=cls.__name__):
                self.prepare(cls)
                scope = self.rt.work.scope(self.task)
                with patch.object(self.rt, "_complete_with_containment", wraps=self.rt._complete_with_containment) as gateway:
                    calls, finish = self.run_responses([self.wire(self.bad), self.wire(self.good)])
                self.assertEqual(calls, 2)
                self.assertEqual(finish["status"], "answered")
                self.assertEqual(finish["answer"], self.bad["answer"])
                self.assertEqual(finish["limits"], self.bad["limits"])
                first, second = [call.kwargs for call in gateway.call_args_list]
                self.assertEqual(first["work_context"], second["work_context"])
                self.assertEqual(first["rule_snapshot"], second["rule_snapshot"])
                envelope = json.loads(second["prompt"].split("\n", 1)[1])
                self.assertEqual(envelope["work"]["sources"], first["work_context"]["sources"])
                self.assertEqual(envelope["citation_repair"]["previous_response"], self.bad)
                self.assertEqual(envelope["citation_repair"]["errors"][0]["code"], "quote_not_found")
                repair = finish["response_repair"]
                self.assertTrue(repair["invocation_started"])
                receipt = next(e for e in self.events("msg.said") if e.seq == repair["original_usage_seq"])
                self.assertEqual(receipt.payload["speech_source"], "system_receipt")
                self.assertEqual(receipt.payload["attempt_seq"], finish["attempt_seq"])
                self.assertEqual([receipt.payload["tokens"], finish["tokens"]], [10, 10])
                self.assertEqual(self.rt.task(self.task).tokens, 20)
                self.assertEqual(self.rt._reported_driver_tokens("offline"), 20)
                self.assertEqual(self.rt.work.scope(self.task), scope)
                self.assertFalse(self.events("work.claimed") + self.events("deliverable.published") + self.events("human.commanded"))

    def test_unknown_source_can_be_corrected_and_insufficient_stays_insufficient(self):
        self.prepare()
        self.bad["status"] = self.good["status"] = "insufficient"
        self.bad["evidence"][0] = {"source_seq": 99999, "quote": "制作五子棋"}
        calls, finish = self.run_responses([self.wire(self.bad), self.wire(self.good)])
        self.assertEqual((calls, finish["status"]), (2, "insufficient"))
        self.assertEqual(finish["response_repair"]["errors"][0]["code"], "source_not_found")

    def test_uncorrected_or_malformed_second_response_fails_without_third_call(self):
        for response in ("same", "malformed", "missing", "unknown"):
            with self.subTest(response=response):
                self.prepare()
                corrected = copy.deepcopy(self.bad)
                if response == "malformed":
                    corrected = "{"
                elif response == "missing":
                    del corrected["limits"]
                elif response == "unknown":
                    corrected["evidence"][0]["source_seq"] = 99999
                calls, finish = self.run_responses([self.wire(self.bad), self.wire(corrected)])
                self.assertEqual((calls, finish["status"]), (2, "invalid"))
                self.assertEqual(finish["response_repair"]["outcome"], "invalid")
                self.assertEqual(self.rt.task(self.task).tokens, 20)

    def test_correction_cannot_change_answer_status_limits_count_or_valid_citations(self):
        for field in ("answer", "status", "limits", "count", "valid_quote", "extra"):
            with self.subTest(field=field):
                self.prepare()
                self.bad["evidence"].append(copy.deepcopy(self.good["evidence"][0]))
                corrected = copy.deepcopy(self.good)
                corrected["evidence"].append(copy.deepcopy(self.good["evidence"][0]))
                if field == "answer":
                    corrected[field] += " 已完成。"
                elif field == "status":
                    corrected[field] = "insufficient"
                elif field == "limits":
                    corrected[field] = []
                elif field == "count":
                    corrected["evidence"].pop()
                elif field == "valid_quote":
                    corrected["evidence"][1]["quote"] = "五子棋"
                else:
                    corrected["new_claim"] = "new"
                calls, finish = self.run_responses([self.wire(self.bad), self.wire(corrected)])
                self.assertEqual((calls, finish["status"]), (2, "invalid"))

    def test_invalid_non_citation_fields_and_syntax_never_trigger_correction(self):
        for field in ("answer", "status", "limits", "missing", "syntax", "evidence", "quote"):
            with self.subTest(field=field):
                self.prepare()
                raw = copy.deepcopy(self.bad)
                if field in {"answer", "status", "limits"}:
                    raw[field] = None
                elif field == "missing":
                    del raw["limits"]
                elif field == "syntax":
                    raw = "{"
                elif field == "evidence":
                    raw[field] = []
                else:
                    raw["evidence"][0]["quote"] = ""
                calls, finish = self.run_responses([self.wire(raw)])
                self.assertEqual((calls, finish["status"]), (1, "invalid"))
                self.assertNotIn("response_repair", finish)

    def test_cli_custom_subclass_and_replaced_complete_cannot_opt_in(self):
        class Derived(AnthropicMessagesDriver):
            pass

        for driver in (CliDriver("offline", ["never-executed"]), fixtures.OfflineDriver(),
                       Derived("offline", endpoint="https://fixture.invalid", model="fixture", api_key_env="CATER_CITATION_FIXTURE"),
                       AnthropicMessagesDriver("offline", endpoint="https://fixture.invalid", model="fixture", api_key_env="CATER_CITATION_FIXTURE")):
            self.prepare(driver=driver)
            driver.review_response_repair = True
            with patch.object(driver, "complete", return_value=DriverResponse(json.dumps(self.bad), 10,
                               {"token_usage": "reported"})) as complete:
                self.rt.questions.answer_next(self.task)
                self.assertFalse(self.rt.questions.answer_next(self.task))
                complete.assert_called_once()
            self.assertEqual(self.events("question.finished")[0].payload["status"], "invalid")

    def test_disabled_repair_and_successful_first_response_make_one_call(self):
        for valid in (False, True):
            self.prepare()
            self.driver.review_response_repair = False
            calls, finish = self.run_responses([self.wire(self.good if valid else self.bad)])
            self.assertEqual((calls, finish["status"]), (1, "answered" if valid else "invalid"))
            self.assertNotIn("response_repair", finish)

    def test_transport_failures_are_not_retried_and_reported_usage_is_not_lost(self):
        for correction in (False, True):
            for failure in ("timeout", "429"):
                self.prepare()
                error = TimeoutError("fixture") if failure == "timeout" else HTTPError("https://fixture.invalid", 429, "fixture", {}, io.BytesIO())
                responses = ([self.wire(self.bad)] if correction else []) + [error]
                calls, finish = self.run_responses(responses)
                self.assertEqual(calls, 2 if correction else 1)
                self.assertEqual(finish["status"], "uncertain")
                self.assertEqual(finish["token_usage"], "unavailable")
                self.assertEqual(self.rt._reported_driver_tokens("offline"), 10 if correction else 0)

    def test_first_usage_is_visible_to_repair_budget_check(self):
        self.prepare()
        self.driver.token_budget = 10
        calls, finish = self.run_responses([self.wire(self.bad)])
        self.assertEqual((calls, finish["status"]), (1, "blocked"))
        self.assertFalse(finish["response_repair"]["invocation_started"])
        self.assertEqual(self.rt._reported_driver_tokens("offline"), 10)
        self.assertEqual(self.rt.task(self.task).tokens, 10)

    def test_unknown_usage_remains_distinct_from_reported_usage(self):
        for reported in (False, True):
            self.prepare()
            calls, finish = self.run_responses([self.wire(self.bad, reported=reported), self.wire(self.good, reported=not reported)])
            self.assertEqual((calls, finish["status"]), (2, "answered"))
            self.assertEqual(self.rt._reported_driver_tokens("offline"), 10)
            self.assertEqual(self.rt.task(self.task).token_usage, {"reported": 1, "unavailable": 1})

    def test_paused_explanation_is_read_only_and_uses_frozen_sources_after_change(self):
        self.prepare()
        self.rt.execution_control.change(self.task, "pause", request_id="citation-pause")
        self.rt.command(self.task, "改成象棋，之后再执行")
        scope = self.rt.work.scope(self.task)
        calls, finish = self.run_responses([self.wire(self.bad), self.wire(self.good)])
        self.assertEqual((calls, finish["status"]), (2, "answered"))
        self.assertEqual(self.rt.work.scope(self.task), scope)
        self.assertTrue(self.rt.questions.view(self.task)[0]["outdated"])
        self.assertFalse(self.events("work.claimed") + self.events("deliverable.published"))


if __name__ == "__main__":
    unittest.main()
