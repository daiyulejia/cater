"""Offline response containment and host checkpoint regressions.

Native parsers and loopback HTTP exercise the actual response boundary; scripted
failures exercise host checkpoints without attributing them to a provider.
"""

from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import PropertyMock, patch

from cater.team import HOST
from cater.tool_protocol import (
    ToolTurn, anthropic_turn, contained_response_diagnostics, openai_turn,
)
import test_protocol_usage as protocol
import test_simple_team as simple


PRIVATE = "PRIVATE_RESPONSE_DIAGNOSTICS"
PROTOCOLS = ((protocol.OpenAICompatibleDriver, "openai_compatible", openai_turn),
             (protocol.AnthropicMessagesDriver, "anthropic", anthropic_turn))


def wire_call(cls, name, arguments, call_id="diagnostic-call"):
    if cls is protocol.AnthropicMessagesDriver:
        return {"type": "tool_use", "id": call_id, "name": name, "input": arguments}
    return {"id": call_id, "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments)}}


def set_calls(cls, data, calls):
    if cls is protocol.AnthropicMessagesDriver:
        data.update(stop_reason="tool_use", content=calls)
    else:
        data["choices"][0].update(finish_reason="tool_calls", message={
            "role": "assistant", "content": None, "tool_calls": calls})


def contained_cases(cls):
    """Only variants reachable through the real protocol parser belong here."""
    native = cls is protocol.AnthropicMessagesDriver
    for stop in (("max_tokens", "pause_turn", "refusal", "model_context_window_exceeded", PRIVATE, None)
                 if native else ("length", "content_filter", "function_call", PRIVATE, None)):
        data = protocol.response(cls, tool=True)
        if native:
            data["stop_reason"] = stop
            data["content"].insert(0, {"type": "thinking", "thinking": PRIVATE, "signature": PRIVATE})
        else:
            data["choices"][0]["finish_reason"] = stop
            data["choices"][0]["message"]["content"] = PRIVATE
        yield "incomplete_stop", stop if stop not in (PRIVATE, None) else None, data
    data = protocol.response(cls)
    if native:
        data["content"] = [{"type": "private-block", "text": PRIVATE}]
    else:
        data["choices"][0]["message"]["content"] = [{"text": PRIVATE}]
    yield "invalid_content", "end_turn" if native else "stop", data
    data = protocol.response(cls, tool=True)
    if native:
        data["stop_reason"] = "end_turn"
    else:
        data["choices"][0]["finish_reason"] = "stop"
    yield "inconsistent_stop", "end_turn" if native else "stop", data
    if not native:
        for field in ("refusal", "function_call"):
            data = protocol.response(cls)
            data["choices"][0]["message"][field] = PRIVATE
            yield "unsupported_content", "stop", data
        data = protocol.response(cls, tool=True)
        data["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = PRIVATE
        yield "invalid_tool_calls", "tool_calls", data
    else:
        data = protocol.response(cls, tool=True)
        data["content"][0]["input"] = PRIVATE
        yield "invalid_content", "tool_use", data


class ResponseDiagnosticsTests(unittest.TestCase):
    def session(self, cls, replies):
        return protocol.ProtocolUsageTests().session(cls, replies)

    def run_message(self, fixture):
        fixture.rt.team.receive(fixture.task, "Check this synthetic response", request_id="response-diagnostics")
        fixture.rt.collaborate(fixture.task)
        self.assertTrue(fixture.rt.verify_chain().ok)

    def assert_private_absent(self, fixture):
        self.assertNotIn(PRIVATE, json.dumps([e.payload for e in fixture.rt.log.read(task_id=fixture.task)]))

    def test_helper_is_strict_fresh_allowlist_and_does_not_mutate_input(self):
        for cls, mode, _ in PROTOCOLS:
            field = "stop_reason" if mode == "anthropic" else "finish_reason"
            stop = "max_tokens" if mode == "anthropic" else "length"
            meta = {"contained": True, "reason": "incomplete_stop", "mode": mode, field: stop,
                    "text": PRIVATE, "provider_content": [PRIVATE], "reasoning": PRIVATE,
                    "signature": PRIVATE, "tool_calls": [{"arguments": PRIVATE}], "private": PRIVATE}
            before = deepcopy(meta)
            expected = {"response_status": "contained", "response_reason": "incomplete_stop",
                        "response_protocol": mode, "response_stop": stop}
            self.assertEqual(contained_response_diagnostics(meta), expected)
            result = contained_response_diagnostics(meta)
            result.clear()
            self.assertEqual(contained_response_diagnostics(meta), expected)
            self.assertEqual(meta, before)
        for value in (None, [], PRIVATE, True, 1, {"contained": 1}, {"contained": "true"},
                      {"contained": False}, {}):
            with self.subTest(value=value):
                self.assertEqual(contained_response_diagnostics(value), {})
        for value in (PRIVATE, [], {}, True, 1, None):
            meta = {"contained": True, "reason": value, "mode": value,
                    "finish_reason": value, "stop_reason": value}
            self.assertEqual(contained_response_diagnostics(meta), {"response_status": "contained"})
        for mode, field, wrong in (("anthropic", "stop_reason", "length"),
                                   ("openai_compatible", "finish_reason", "max_tokens")):
            self.assertEqual(contained_response_diagnostics({"contained": True, "mode": mode, field: wrong}),
                             {"response_status": "contained", "response_protocol": mode})

    def test_real_contained_parsers_and_http_events_preserve_usage_without_execution_or_retry(self):
        for cls, mode, parser in PROTOCOLS:
            for reason, stop, data in contained_cases(cls):
                with self.subTest(protocol=mode, reason=reason, stop=stop):
                    data["private"] = PRIVATE
                    data["usage"]["private"] = PRIVATE
                    seen = {"previous-call"}
                    turn = parser(data, driver="synthetic", names={"read_messages"}, seen=seen)
                    self.assertEqual(seen, {"previous-call"})
                    self.assertEqual((turn.text, turn.calls, turn.tokens), ("", [], 14))
                    self.assertIs(turn.meta["contained"], True)
                    expected = {"response_status": "contained", "response_reason": reason,
                                "response_protocol": mode}
                    if stop is not None:
                        expected["response_stop"] = stop
                    if reason == "invalid_tool_calls":
                        expected["response_tool_issue"] = "invalid_arguments_json"
                    elif reason == "invalid_content" and stop == "tool_use":
                        expected["response_tool_issue"] = "arguments_not_object"
                    self.assertEqual(contained_response_diagnostics(turn.meta), expected)
                    self.assertEqual(turn.meta["usage"], protocol.usage(cls))
                    with self.session(cls, [(200, data)]) as (f, _, requests):
                        self.run_message(f)
                        call, = f.events("team.model_called")
                        finish, = f.events("team.run_finished")
                        for row in (call, finish):
                            self.assertEqual({k: v for k, v in row.payload.items()
                                              if k.startswith("response_")},
                                             {**expected, "response_client_tools_executed": False})
                        self.assertEqual((call.payload["tokens"], call.payload["token_usage"]), (14, "reported"))
                        self.assertEqual(call.payload["usage"], protocol.usage(cls))
                        self.assertEqual((finish.payload["status"], finish.payload["error_type"],
                                          finish.payload["failure_stage"]),
                                         ("failed", "ValueError", "response_handling"))
                        self.assertEqual(call.payload["run_id"], finish.payload["run_id"])
                        for phrase in ("先前成果和操作记录保留", "未自动续试", "宿主未执行这次响应"):
                            self.assertIn(phrase, finish.payload["reason"])
                        self.assertEqual(f.events("team.tool_started"), [])
                        self.assertEqual(f.events("team.tool_finished"), [])
                        self.assertEqual(f.events("msg.said"), [])
                        self.assertEqual(f.events("team.objective_completed"), [])
                        self.assertEqual(f.rt.task(f.task).tokens, 14)
                        self.assertEqual(f.rt._reported_driver_tokens("team_test"), 14)
                        f.rt.collaborate(f.task)
                        self.assertEqual(len(requests), 1)
                        self.assertEqual(len(f.events("team.run_finished")), 1)
                        self.assert_private_absent(f)

    def test_contained_usage_missing_invalid_and_explicit_zero_keep_source(self):
        for cls, _, _ in PROTOCOLS:
            for usage, source in ((None, "unavailable"), ({}, "unavailable"),
                                 ({"total_tokens": True, "input_tokens": -1}, "unavailable"),
                                 ({"input_tokens": 0, "output_tokens": 0} if cls is protocol.AnthropicMessagesDriver
                                  else {"total_tokens": 0}, "reported")):
                with self.subTest(cls=cls.__name__, usage=usage):
                    _, _, data = next(contained_cases(cls))
                    data["usage"] = usage
                    with self.session(cls, [(200, data)]) as (f, _, requests):
                        self.run_message(f)
                        call, = f.events("team.model_called")
                        self.assertEqual((call.payload["tokens"], call.payload["token_usage"]), (0, source))
                        self.assertEqual(call.payload["response_status"], "contained")
                        self.assertEqual(f.rt.task(f.task).tokens, 0)
                        self.assertEqual(len(requests), 1)

    def test_committed_actions_survive_next_contained_response_and_old_completion_is_not_success(self):
        for cls, _, _ in PROTOCOLS:
            first = protocol.response(cls)
            set_calls(cls, first, [wire_call(cls, "set_objective", {"goal": "Current synthetic objective"}, "objective-1"),
                wire_call(cls, "publish_artifact", {"filename": "retained.txt", "content": "committed bytes",
                    "summary": "synthetic artifact", "format": "text"}, "publish-1")])
            _, _, second = next(contained_cases(cls))
            with self.subTest(cls=cls.__name__), self.session(cls, [(200, first), (200, second)]) as (f, _, requests):
                old = f.rt.log.emit(type="team.objective_completed", actor=HOST, task_id=f.task,
                                    payload={"summary": "old completion", "verification": "old fixture"})
                self.run_message(f)
                finish, = f.events("team.run_finished")
                self.assertEqual(finish.payload["status"], "failed")
                self.assertIs(finish.payload["response_client_tools_executed"], False)
                self.assertEqual([e.seq for e in f.events("team.objective_completed")], [old.seq])
                self.assertNotEqual(f.rt.team.view(f.task)["status"], "done")
                self.assertNotEqual(f.rt.task(f.task).status, "done")
                artifact, = f.rt.task(f.task).deliverables
                self.assertEqual(Path(artifact["local_path"]).read_text(encoding="utf-8"), "committed bytes")
                self.assertEqual([e.payload["name"] for e in f.events("team.tool_finished")],
                                 ["set_objective", "publish_artifact"])
                self.assertTrue(all(e.payload["result"]["status"] == "ok" for e in f.events("team.tool_finished")))
                self.assertEqual(f.rt.task(f.task).tokens, 28)
                self.assertEqual(f.rt._reported_driver_tokens("team_test"), 28)
                f.rt.collaborate(f.task)
                self.assertEqual(len(requests), 2)
                self.assertEqual(len(f.events("team.tool_started")), 2)
                self.assertEqual(len(f.rt.task(f.task).deliverables), 1)
                self.assert_private_absent(f)

    def test_nonboolean_containment_stops_but_does_not_forge_recognized_diagnostics(self):
        f = simple.SimpleTeamTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        f.driver.steps[f.manager] = [ToolTurn(PRIVATE, [], 7, {
            "contained": 1, "reason": "incomplete_stop", "mode": "openai_compatible", "finish_reason": "length"})]
        self.run_message(f)
        call, = f.events("team.model_called")
        finish, = f.events("team.run_finished")
        self.assertFalse(any(k.startswith("response_") for k in call.payload))
        self.assertNotIn("response_status", finish.payload)
        self.assertIs(finish.payload["response_client_tools_executed"], False)
        self.assertEqual(finish.payload["status"], "failed")
        self.assertEqual(len(f.driver.calls), 1)
        self.assertEqual(f.events("msg.said"), [])
        self.assert_private_absent(f)

    def test_plain_valueerror_uses_host_checkpoint_not_exception_keywords(self):
        class BrokenText:
            def strip(self):
                raise ValueError(PRIVATE + " HTTP 429 timeout length")

        class BrokenResult(dict):
            def get(self, *args):
                raise ValueError(PRIVATE)

        for stage in ("setup", "context_assembly", "model_request", "response_handling",
                      "tool_dispatch", "tool_execution", "tool_result_recording"):
            with self.subTest(stage=stage):
                f = simple.SimpleTeamTests()
                f.setUp()
                try:
                    reply = simple.turn("", ("read_messages", {"limit": 1}))
                    host, target, options = f.rt.team, None, {}
                    if stage == "setup":
                        host, target = type(f.driver), "tool_turn"
                        options = {"new_callable": PropertyMock,
                                   "side_effect": ValueError(PRIVATE + " HTTP 429 timeout length")}
                    elif stage in {"context_assembly", "tool_execution"}:
                        target = {"context_assembly": "_context_snapshot",
                                  "tool_execution": "_invoke_tool"}[stage]
                        options = {"side_effect": ValueError(PRIVATE + " HTTP 429 timeout length")}
                    elif stage == "model_request":
                        reply = ValueError(PRIVATE + " HTTP 429 timeout length")
                    elif stage == "response_handling":
                        reply = ToolTurn(BrokenText(), [], 7, {"token_usage": "reported"})
                    elif stage == "tool_dispatch":
                        reply = simple.turn("", ("unregistered_fixture_tool", {}))
                    else:
                        target, options = "_invoke_tool", {"return_value": BrokenResult()}
                    f.driver.steps[f.manager] = [reply]
                    if target:
                        with patch.object(host, target, **options):
                            self.run_message(f)
                    else:
                        self.run_message(f)
                    finish, = f.events("team.run_finished")
                    self.assertEqual((finish.payload["status"], finish.payload["error_type"],
                                      finish.payload["failure_stage"]), ("failed", "ValueError", stage))
                    for key in ("response_status", "response_reason", "response_client_tools_executed",
                                "failure_kind", "status_code", "transport_phase"):
                        self.assertNotIn(key, finish.payload)
                    self.assertLessEqual(len(f.driver.calls), 1)
                    self.assert_private_absent(f)
                finally:
                    f.doCleanups()


if __name__ == "__main__":
    unittest.main()
