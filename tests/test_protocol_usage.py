"""Actual loopback adapters through native events; never a remote model."""

from contextlib import ExitStack, contextmanager
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import threading
import traceback
import unittest
from unittest.mock import patch

from cater.anthropic import AnthropicMessagesDriver
from cater.drivers import DriverProtocolError, OpenAICompatibleDriver, tool_protocol_error_usage
from cater.runtime import Runtime
import test_simple_team as simple


CLASSES = (OpenAICompatibleDriver, AnthropicMessagesDriver)
KEY = "CATER_PROTOCOL_USAGE_FIXTURE"


def usage(cls):
    return ({"input_tokens": 5, "output_tokens": 3, "cache_read_input_tokens": 4,
             "cache_creation_input_tokens": 2, "cache_creation": {"ephemeral_5m_input_tokens": 2}}
            if cls is AnthropicMessagesDriver else
            {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14,
             "prompt_tokens_details": {"cached_tokens": 9},
             "completion_tokens_details": {"reasoning_tokens": 1}})


def response(cls, *, tool=False):
    if cls is AnthropicMessagesDriver:
        content = [{"type": "tool_use", "id": "read-1", "name": "read_messages", "input": {"limit": 1}}] if tool else [
            {"type": "text", "text": "已核对当前消息。"}]
        return {"type": "message", "role": "assistant", "stop_reason": "tool_use" if tool else "end_turn",
                "content": content, "usage": usage(cls)}
    message = {"role": "assistant", "content": "已核对当前消息。"}
    if tool:
        message["tool_calls"] = [{"id": "read-1", "type": "function", "function": {
            "name": "read_messages", "arguments": '{"limit":1}'}}]
    return {"choices": [{"finish_reason": "tool_calls" if tool else "stop", "message": message}], "usage": usage(cls)}


def malformed(cls):
    data = response(cls, tool=True)
    if cls is AnthropicMessagesDriver:
        data["role"] = "PRIVATE_PROVIDER_ROLE"
    else:
        data["choices"].append(deepcopy(data["choices"][0]))
    data["private"] = "PRIVATE_PROVIDER_BODY"
    data["usage"]["private"] = "PRIVATE_PROVIDER_USAGE"
    return data


class ProtocolUsageTests(unittest.TestCase):
    @contextmanager
    def session(self, cls, replies):
        fixture = simple.SimpleTeamTests()
        fixture.setUp()
        requests = []
        pending = list(replies)

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                status, data = pending.pop(0) if pending else (500, {"error": "unexpected extra request"})
                raw = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args):
                pass

        try:
            with ExitStack() as resources:
                server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
                server.daemon_threads = False
                resources.callback(server.server_close)
                thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01))
                thread.start()
                resources.callback(thread.join)
                resources.callback(server.shutdown)
                resources.enter_context(patch.dict(os.environ, {KEY: "synthetic-only-key"}))
                driver = cls("team_test", endpoint=f"http://127.0.0.1:{server.server_port}/fixture",
                             model="synthetic-only", api_key_env=KEY, timeout=2)
                fixture.rt.drivers["team_test"] = driver
                yield fixture, driver, requests
        finally:
            fixture.doCleanups()

    def run_message(self, fixture):
        fixture.rt.team.receive(fixture.task, "核对当前要求，不安排额外工作。", request_id="protocol-usage-fixture")
        fixture.rt.collaborate(fixture.task)
        self.assertTrue(fixture.rt.verify_chain().ok)

    def test_outer_protocol_failure_retains_usage_and_stops_all_work(self):
        for cls in CLASSES:
            with self.subTest(cls=cls.__name__), self.session(cls, [(200, malformed(cls))]) as (f, _, requests):
                self.run_message(f)
                calls = f.events("team.model_called")
                self.assertEqual(len(calls), 1)
                call, = calls
                self.assertEqual(call.payload["response_status"], "protocol_error")
                self.assertEqual((call.payload["tokens"], call.payload["token_usage"]), (14, "reported"))
                self.assertEqual(call.payload["usage"], usage(cls))
                self.assertGreater(call.payload["request_sizes"]["prompt_utf8_bytes"], 0)
                self.assertEqual(call.payload["request_sizes"]["history_json_bytes"], 2)
                self.assertEqual(f.rt.task(f.task).tokens, 14)
                self.assertEqual(f.rt._reported_driver_tokens("team_test"), 14)
                with Runtime(f.root / "test.db", roles_path=f.roles, drivers={"team_test": f.rt.drivers["team_test"]}) as reopened:
                    self.assertEqual(reopened.task(f.task).tokens, 14)
                    self.assertEqual(reopened._reported_driver_tokens("team_test"), 14)
                    self.assertTrue(reopened.verify_chain().ok)
                finish, = f.events("team.run_finished")
                self.assertEqual((finish.payload["status"], finish.payload["error_type"]), ("failed", "DriverProtocolError"))
                self.assertEqual(finish.payload["run_id"], call.payload["run_id"])
                self.assertEqual(f.events("team.tool_started"), [])
                self.assertEqual(f.events("team.objective_completed"), [])
                self.assertEqual(f.events("msg.said"), [])
                self.assertEqual(len(requests), 1)
                self.assertNotIn("PRIVATE_PROVIDER", json.dumps([e.payload for e in f.rt.log.read(task_id=f.task)]))

    def test_normal_two_turn_tool_history_preserves_details_and_counts_once(self):
        for cls in CLASSES:
            with self.subTest(cls=cls.__name__), self.session(cls, [(200, response(cls, tool=True)), (200, response(cls))]) as (f, _, requests):
                self.run_message(f)
                calls = f.events("team.model_called")
                self.assertEqual(len(calls), 2)
                self.assertTrue(all(row.payload["usage"] == usage(cls) for row in calls))
                self.assertTrue(all("response_status" not in row.payload for row in calls))
                self.assertGreater(calls[1].payload["request_sizes"]["history_json_bytes"], 2)
                self.assertEqual(f.rt.task(f.task).tokens, 28)
                self.assertEqual(f.rt._reported_driver_tokens("team_test"), 28)
                self.assertEqual(len(f.events("team.tool_finished")), 1)
                self.assertEqual(f.events("team.run_finished")[0].payload["status"], "completed")
                self.assertEqual(len(requests), 2)

    def test_invalid_or_missing_usage_stays_unavailable_not_reported_zero(self):
        for cls in CLASSES:
            for invalid in (None, {}, {"total_tokens": True}, {"input_tokens": -1, "output_tokens": 3}):
                with self.subTest(cls=cls.__name__, invalid=invalid):
                    data = malformed(cls)
                    data["usage"] = invalid
                    with self.session(cls, [(200, data)]) as (f, _, requests):
                        self.run_message(f)
                        call, = f.events("team.model_called")
                        self.assertEqual((call.payload["tokens"], call.payload["token_usage"]), (0, "unavailable"))
                        self.assertEqual(call.payload["response_status"], "protocol_error")
                        self.assertEqual(len(requests), 1)
                        self.assertEqual(f.events("team.tool_started"), [])

    def test_explicit_zero_usage_remains_reported_after_protocol_failure(self):
        for cls in CLASSES:
            data = malformed(cls)
            data["usage"] = {"input_tokens": 0, "output_tokens": 0} if cls is AnthropicMessagesDriver else {"total_tokens": 0}
            with self.subTest(cls=cls.__name__), self.session(cls, [(200, data)]) as (f, _, _):
                self.run_message(f)
                call, = f.events("team.model_called")
                self.assertEqual((call.payload["tokens"], call.payload["token_usage"]), (0, "reported"))

    def test_unambiguous_usage_is_required_before_preserving_protocol_counts(self):
        for cls in CLASSES:
            for status, data in ((200, b'{"usage":{"total_tokens":10},"usage":{"total_tokens":20}}'),
                                 (200, b"PRIVATE_PROVIDER_BAD_JSON"), (200, b"[]"),
                                 (503, {"usage": {"total_tokens": 99}, "error": "PRIVATE_PROVIDER_ERROR"})):
                with self.subTest(cls=cls.__name__, status=status, data=data), self.session(cls, [(status, data)]) as (f, _, requests):
                    self.run_message(f)
                    self.assertEqual(f.events("team.model_called"), [])
                    self.assertEqual(f.events("team.tool_started"), [])
                    self.assertEqual(f.events("team.run_finished")[0].payload["status"], "failed")
                    self.assertEqual(len(requests), 1)

    def test_plain_protocol_error_does_not_forge_a_response_receipt(self):
        with self.session(OpenAICompatibleDriver, []) as (f, driver, requests):
            with patch.object(driver, "tool_turn", side_effect=DriverProtocolError("synthetic pre-response failure")):
                self.run_message(f)
            self.assertEqual(f.events("team.model_called"), [])
            self.assertEqual(requests, [])

    def test_shape_failures_preserve_protocol_exception_and_sanitized_counters(self):
        for cls in CLASSES:
            base = response(cls, tool=True)
            if cls is AnthropicMessagesDriver:
                variants = [{**base, "type": "PRIVATE_PROVIDER_TYPE"}, {**base, "role": "user"}]
            else:
                variants = [{**base, "choices": choices} for choices in (None, [], [None], [base["choices"][0]] * 2)]
                variants += [{**base, "choices": [{"finish_reason": "tool_calls", "message": message}]} for message in (
                    None, {"role": "user", "content": "PRIVATE_PROVIDER_CONTENT"})]
            for data in variants:
                data["usage"] = {**usage(cls), "private": "PRIVATE_PROVIDER_USAGE"}
                with self.subTest(cls=cls.__name__), self.session(cls, [(200, data)]) as (_, driver, requests):
                    with self.assertRaises(DriverProtocolError) as raised:
                        driver.tool_turn(role="worker", prompt="synthetic", history=[], tools=[])
                    self.assertIs(type(raised.exception), DriverProtocolError)
                    saved = tool_protocol_error_usage(raised.exception)
                    self.assertEqual(saved, {"tokens": 14, "token_usage": "reported", "usage": usage(cls)})
                    self.assertNotIn("PRIVATE_PROVIDER", "".join(traceback.format_exception(raised.exception)))
                    self.assertNotIn("PRIVATE_PROVIDER", json.dumps(vars(raised.exception)))
                    self.assertEqual(len(requests), 1)
                    saved["usage"].clear()
                    self.assertEqual(tool_protocol_error_usage(raised.exception)["usage"], usage(cls))

    def test_counter_reader_drops_untrusted_fields_and_does_not_infer_totals(self):
        self.assertIsNone(tool_protocol_error_usage(ValueError("synthetic")))
        self.assertIsNone(tool_protocol_error_usage(DriverProtocolError("synthetic")))
        for tokens in (None, True, -1, 1.5, "PRIVATE_PROVIDER_COUNT"):
            error = DriverProtocolError("synthetic")
            error._tool_response_usage = {"tokens": tokens, "token_usage": "reported",
                "usage": {"total_tokens": 14, "private": "PRIVATE_PROVIDER_USAGE"}}
            self.assertEqual(tool_protocol_error_usage(error), {"tokens": 0, "token_usage": "unavailable",
                                                               "usage": {"total_tokens": 14}})


if __name__ == "__main__":
    unittest.main()
