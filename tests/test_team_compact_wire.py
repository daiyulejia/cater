"""Compact model envelopes through real Runtime and loopback HTTP adapters."""

import ast
from contextlib import contextmanager
from copy import deepcopy
import inspect
import json
import unittest
from unittest.mock import patch

from cater.artifacts import normalize_deliverables
from cater.events import Actor
from cater.runtime import Runtime
import cater.team as team_module
import cater.team_external as external_module
from cater.team_context import HISTORY_BYTES, model_context
import test_protocol_usage as protocol
import test_team_external as external


LITERAL = (
    'PRIVATE_WIRE_LITERAL \u4e2d\u6587  a, b: c\n'
    'C:\\Project Files\\\u6d4b\u8bd5\\source file.py\r\n'
    'def render(value):\n'
    '    text = "a, b: c"\n'
    '    return {"text": text, "items": [1, 2]}  # keep  two spaces\n'
    '\tquoted "value", backslash \\, trailing  spaces  \nEND'
)
VISIBLE = 'Model says: { "a": 1, "b": 2 }\n  \u4fdd\u7559\u6b63\u6587\u7f29\u8fdb\tC:\\a b\\file.py'


def declared_string(module, name, **bindings):
    # Derive the complete declared instructions independently of prompt assembly.
    nodes = [node.value for node in ast.walk(ast.parse(inspect.getsource(module)))
             if isinstance(node, ast.Assign)
             and any(isinstance(target, ast.Name) and target.id == name for target in node.targets)]
    if len(nodes) != 1:
        raise AssertionError(f"Expected one declaration of {name}")
    return eval(compile(ast.Expression(nodes[0]), module.__file__, "eval"),
                {"__builtins__": {}}, bindings)


def final_response(cls):
    data = protocol.response(cls)
    if cls is protocol.AnthropicMessagesDriver:
        data["content"] = [{"type": "text", "text": VISIBLE}]
    else:
        data["choices"][0]["message"]["content"] = VISIBLE
    return data


def read_response(cls, name, arguments):
    data = protocol.response(cls, tool=True)
    if cls is protocol.AnthropicMessagesDriver:
        data["content"] = [
            {"type": "thinking", "thinking": 'PRIVATE_PROVIDER_THOUGHT { "a": 1 }\n  \u4e2d\u6587',
             "signature": "PRIVATE_PROVIDER_SIGNATURE  with spaces\nopaque=="},
            {"type": "redacted_thinking", "data": "PRIVATE_PROVIDER_REDACTED=="},
            {"type": "text", "text": VISIBLE},
            {"type": "tool_use", "id": "read-1", "name": name, "input": deepcopy(arguments)},
        ]
    else:
        message = data["choices"][0]["message"]
        message["content"] = VISIBLE
        message["tool_calls"][0]["function"] = {
            "name": name, "arguments": json.dumps(arguments, ensure_ascii=False, indent=2)}
    return data


class TeamCompactWireTests(unittest.TestCase):
    # Reuse only the session helper, without inheriting its discovered tests.
    session = protocol.ProtocolUsageTests.session

    @contextmanager
    def capture_native(self, fixture, driver):
        assembled, turns = [], []
        original_input = fixture.rt.team._model_input
        original_turn = driver.tool_turn

        def capture_input(*args, **kwargs):
            result = original_input(*args, **kwargs)
            assembled.append(deepcopy(result[0]))
            return result

        def capture_turn(**kwargs):
            row = {**deepcopy(kwargs), "raw_context": assembled[-1]}
            turns.append(row)
            result = original_turn(**kwargs)
            row["assistant"] = deepcopy(result.message)
            return result

        with patch.object(fixture.rt.team, "_model_input", side_effect=capture_input), patch.object(
                driver, "tool_turn", side_effect=capture_turn):
            yield turns

    def assert_compact(self, text, value):
        self.assertIsInstance(text, str)
        decoded = json.loads(text)
        self.assertEqual(decoded, value)
        # Durable events sort object keys; wire formatting need not adopt that order.
        self.assertEqual(text, json.dumps(decoded, ensure_ascii=False, separators=(",", ":")))
        self.assertLess(len(text.encode("utf-8")),
                        len(json.dumps(decoded, ensure_ascii=False).encode("utf-8")))

    def assert_sizes(self, sizes, prompt, instructions, context_text, **other):
        self.assertEqual(sizes, {
            "prompt_utf8_bytes": len(prompt.encode("utf-8")),
            "instructions_utf8_bytes": len(instructions.encode("utf-8")),
            "context_json_bytes": len(context_text.encode("utf-8")),
            **other,
        })
        self.assertEqual(sizes["instructions_utf8_bytes"] + sizes["context_json_bytes"],
                         sizes["prompt_utf8_bytes"])
        self.assertTrue(all(type(value) is int and value >= 0 for value in sizes.values()))
        self.assertNotIn("PRIVATE", json.dumps(sizes))

    def assert_native_requests(self, fixture, cls, requests, turns):
        receipts = fixture.events("team.model_called")
        self.assertEqual(len(receipts), len(requests))
        self.assertEqual(len(turns), len(requests))
        instructions = declared_string(team_module, "instructions",
                                       role=fixture.rt.team._role(fixture.manager))
        for request, captured, receipt in zip(requests, turns, receipts):
            prompt = request["messages"][0 if cls is protocol.AnthropicMessagesDriver else 1]["content"]
            self.assertEqual(prompt, captured["prompt"])
            self.assertTrue(prompt.startswith(instructions))
            context_text = prompt[len(instructions):]
            history_bytes = len(json.dumps(captured["history"], ensure_ascii=False).encode("utf-8"))
            context = model_context(captured["raw_context"])
            context["tool_history_window"] = {
                "omitted_groups": 0, "retained_json_bytes": history_bytes,
                "soft_budget_bytes": HISTORY_BYTES, "run_id": receipt.payload["run_id"],
            }
            self.assert_compact(context_text, context)
            self.assertEqual(prompt, instructions + json.dumps(context, ensure_ascii=False, separators=(",", ":")))
            self.assertEqual(context["message"]["text"], LITERAL)
            self.assertIn("\u4e2d\u6587", context_text)
            self.assert_sizes(receipt.payload["request_sizes"], prompt, instructions, context_text,
                              history_json_bytes=history_bytes,
                              tools_json_bytes=len(json.dumps(captured["tools"], ensure_ascii=False).encode("utf-8")),
                              history_omitted_groups=0)
            self.assertNotIn("PRIVATE", json.dumps(receipt.payload))

    def receive_and_run(self, fixture):
        message = fixture.rt.team.receive(fixture.task, LITERAL, request_id="compact-wire-synthetic")
        self.assertEqual(message.payload["text"], LITERAL)
        fixture.rt.collaborate(fixture.task)
        self.assertTrue(fixture.rt.verify_chain().ok)
        return message

    def assert_no_replay(self, fixture, requests, count):
        before = fixture.rt.log.read(task_id=fixture.task)
        fixture.rt.collaborate(fixture.task)
        self.assertEqual(fixture.rt.log.read(task_id=fixture.task), before)
        self.assertEqual(len(requests), count)
        self.assertEqual(fixture.events("team.objective_completed"), [])
        self.assertEqual(fixture.events("team.run_finished")[0].payload["status"], "completed")

    def test_native_complete_prompt_and_final_text_are_lossless(self):
        for cls in protocol.CLASSES:
            with self.subTest(protocol=cls.__name__), self.session(cls, [(200, final_response(cls))]) as (f, driver, requests):
                with self.capture_native(f, driver) as turns:
                    self.receive_and_run(f)
                self.assertEqual(len(requests), 1)
                self.assertEqual(turns[0]["history"], [])
                self.assert_native_requests(f, cls, requests, turns)
                said, = f.events("msg.said")
                self.assertEqual(said.payload["text"], VISIBLE)
                self.assertEqual(f.events("team.tool_started"), [])
                self.assertEqual(f.rt.task(f.task).tokens, 14)
                self.assert_no_replay(f, requests, 1)

    def test_native_read_result_is_compact_but_receipt_and_provider_history_stay_exact(self):
        for cls in protocol.CLASSES:
            for name in ("read_messages", "read_artifact"):
                first = read_response(cls, name, {})
                with self.subTest(protocol=cls.__name__, tool=name), self.session(
                        cls, [(200, first), (200, final_response(cls))]) as (run, run_driver, wire):
                    if name == "read_artifact":
                        payload, = normalize_deliverables([{
                            "title": "Synthetic source", "summary": "Synthetic only",
                            "filename": "source file.py", "content": LITERAL, "format": "text"}])
                        artifact = run.rt.log.emit(type="deliverable.published", task_id=run.task,
                                                   actor=Actor.agent(run.manager, run.rt.team._role(run.manager).role),
                                                   payload=payload)
                        arguments = {"artifact_seq": artifact.seq}
                    else:
                        arguments = {"scope": "inbox", "limit": 1}
                    # Fill the queued reply with the sequence assigned by this Runtime before any request.
                    first.update(read_response(cls, name, arguments))
                    with self.capture_native(run, run_driver) as turns:
                        source = self.receive_and_run(run)
                    self.assertEqual(len(wire), 2)
                    self.assert_native_requests(run, cls, wire, turns)
                    started, = run.events("team.tool_started")
                    finished, = run.events("team.tool_finished")
                    self.assertEqual(started.payload["name"], name)
                    self.assertEqual(started.payload["arguments"], arguments)
                    self.assertEqual(finished.payload["started_seq"], started.seq)
                    result = finished.payload["result"]
                    self.assertIsInstance(result, dict)
                    self.assertEqual(result["status"], "ok")
                    if name == "read_artifact":
                        self.assertEqual(result["content"], LITERAL)
                        self.assertEqual(run.rt.task(run.task).deliverables[0]["content"], LITERAL)
                        self.assertEqual(next(e for e in run.rt.log.read(task_id=run.task)
                                              if e.seq == artifact.seq), artifact)
                    else:
                        message, = result["messages"]
                        self.assertEqual(message["text"], LITERAL)
                        self.assertEqual(message["seq"], source.seq)
                        self.assertFalse(result["has_more"])
                    assistant, tool = turns[1]["history"]
                    self.assertEqual(assistant, turns[0]["assistant"])
                    self.assertEqual(assistant["content"], VISIBLE)
                    self.assertEqual(assistant["tool_calls"][0]["arguments"], arguments)
                    self.assertFalse(tool["is_error"])
                    self.assert_compact(tool["content"], result)
                    self.assertEqual(tool["tool_call_id"], "read-1")
                    replay = next(row for row in wire[1]["messages"] if row["role"] == "assistant")
                    if cls is protocol.AnthropicMessagesDriver:
                        self.assertEqual(assistant["provider_content"], first["content"])
                        self.assertEqual(replay["content"], first["content"])
                        self.assertEqual(wire[1]["messages"][-1]["content"], [{
                            "type": "tool_result", "tool_use_id": "read-1",
                            "content": tool["content"], "is_error": False}])
                    else:
                        self.assertEqual(replay["content"], VISIBLE)
                        replay_args = replay["tool_calls"][0]["function"]["arguments"]
                        # Existing normalization uses spaced json_utf8, not the new model formatter.
                        self.assertEqual(replay_args, json.dumps(arguments, ensure_ascii=False))
                        self.assertNotEqual(replay_args, json.dumps(arguments, separators=(",", ":")))
                        self.assertEqual(wire[1]["messages"][-1]["content"], tool["content"])
                    self.assertEqual(run.rt.task(run.task).tokens, 28)
                    with Runtime(run.root / "test.db", roles_path=run.roles,
                                 drivers={"team_test": run_driver}) as reopened:
                        saved, = list(reopened.log.iter_events(task_id=run.task, event_types=("team.tool_finished",)))
                        self.assertEqual(saved, finished)
                        self.assertEqual(saved.payload["result"], json.loads(tool["content"]))
                        self.assertEqual(reopened.task(run.task).tokens, 28)
                        self.assertTrue(reopened.verify_chain().ok)
                    self.assert_no_replay(run, wire, 2)

    def test_native_protocol_error_still_records_exact_compact_request_sizes(self):
        for cls in protocol.CLASSES:
            with self.subTest(protocol=cls.__name__), self.session(cls, [(200, protocol.malformed(cls))]) as (f, driver, requests):
                with self.capture_native(f, driver) as turns:
                    self.receive_and_run(f)
                self.assertEqual(len(requests), 1)
                self.assert_native_requests(f, cls, requests, turns)
                receipt, = f.events("team.model_called")
                self.assertEqual(receipt.payload["response_status"], "protocol_error")
                self.assertEqual(f.events("team.run_finished")[0].payload["status"], "failed")
                self.assertEqual(f.events("team.tool_started"), [])
                self.assertEqual(f.events("msg.said"), [])
                self.assertEqual(f.rt.task(f.task).tokens, 14)

    def test_external_prompt_is_complete_compact_and_metered_without_native_requests(self):
        f = external.TeamExternalTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        f.driver.scripts[f.manager] = [lambda run: VISIBLE]
        prompts, contexts = [], []
        original_input = f.rt.team._model_input
        original_run = external.ScriptedExternalRun.run

        def capture_input(*args, **kwargs):
            result = original_input(*args, **kwargs)
            contexts.append(deepcopy(result[0]))
            return result

        def capture_run(run, prompt):
            prompts.append(prompt)
            return original_run(run, prompt)

        with patch.object(f.rt.team, "_model_input", side_effect=capture_input), patch.object(
                external.ScriptedExternalRun, "run", capture_run), patch.object(
                f.driver, "tool_turn", side_effect=AssertionError("Unexpected native request")) as native:
            self.receive_and_run(f)
        native.assert_not_called()
        self.assertEqual(f.driver.errors, [])
        self.assertEqual(len(f.driver.runs), 1)
        prompt, = prompts
        raw, = contexts
        instructions = declared_string(team_module, "instructions", role=f.rt.team._role(f.manager))
        instructions = declared_string(external_module, "prompt_instructions", instructions=instructions)
        self.assertTrue(prompt.startswith(instructions))
        context_text = prompt[len(instructions):]
        self.assert_compact(context_text, model_context(raw))
        self.assertEqual(json.loads(context_text)["message"]["text"], LITERAL)
        receipt, = f.events("team.model_called")
        self.assert_sizes(receipt.payload["request_sizes"], prompt, instructions, context_text)
        self.assertNotIn("PRIVATE", json.dumps(receipt.payload))
        self.assertEqual(receipt.payload["token_usage"], "unavailable")
        self.assertEqual(f.events("team.tool_started"), [])
        self.assertEqual(f.events("msg.said")[0].payload["text"], VISIBLE)
        self.assert_no_replay(f, prompts, 1)


if __name__ == "__main__":
    unittest.main()
