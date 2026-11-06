"""Customer input discovery and paging with a local deterministic driver."""

import json
import sys
import unittest
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cater.acp import AcpSession
from cater.events import Actor
from cater.external_agent import QoderAcpDriver
from cater.team import HOST, PAUSED_TOOLS
from cater.team_context import CONVERSATION_BYTES, json_bytes, model_context, model_json
from cater.team_customer_sources import (CustomerSourceIndexError, INDEX_JSON_BYTES, INDEX_SIZE, PREVIEW_JSON_BYTES, PREVIEW_UTF8_BYTES,
                                         TEXT_JSON_BYTES, _preview, index as raw_source_index)
from cater.tool_protocol import json_utf8
from cater.tool_protocol import ToolCall
import test_simple_team as fixture
import test_team_external as external_fixture


def source_index(events, **kwargs):
    return raw_source_index(events, member_ids={"store_manager", "manager"}, **kwargs)


def assert_invalid_attempt_chain(test, events, source):
    starts = [e for e in events if e.type == "team.tool_started" and e.payload.get("name") == "set_objective"]
    bad = next(e for e in starts if "unexpected" in e.payload.get("arguments", {}))
    receipt = next(e for e in events if e.type == "team.tool_finished" and e.payload.get("started_seq") == bad.seq)
    result = receipt.payload["result"]
    success = next(e for e in events if e.type == "team.tool_finished" and e.payload.get("started_seq") == starts[-1].seq)
    test.assertEqual((result["status"], result["code"], result["complete"], result["outcome"]),
                     ("error", "invalid_arguments", True, "not_started"))
    test.assertEqual(source_index(events)["objective_source"]["message_seq"], source.seq)
    with_call_id = [replace(e, payload={**e.payload, "call_id": bad.payload["call_id"]}) if e.seq == receipt.seq else e for e in events]
    test.assertEqual(source_index(with_call_id)["objective_source"]["message_seq"], source.seq)
    variants = [
        [e for e in events if e.seq != receipt.seq],
        [replace(e, actor=Actor.agent("team", "team")) if e.seq == receipt.seq else e for e in events],
        [replace(e, payload={**e.payload, "run_id": "forged"}) if e.seq == receipt.seq else e for e in events],
        [replace(e, payload={**e.payload, "call_id": "different"}) if e.seq == receipt.seq else e for e in events],
        [replace(e, payload={k: v for k, v in e.payload.items() if k != "call_id"}) if e.seq == bad.seq else e for e in events],
        [replace(e, payload={**e.payload, "result": {**result, "outcome": "effects_unknown"}}) if e.seq == receipt.seq else e for e in events],
        [replace(e, payload={**e.payload, "result": {**result, "complete": False}}) if e.seq == receipt.seq else e for e in events],
        [replace(e, payload={**e.payload, "result": {**result, "surprise_effect": True}}) if e.seq == receipt.seq else e for e in events],
        [replace(e, payload={**e.payload, "result": {**e.payload["result"], "outcome": "effects_unknown"}}) if e.seq == success.seq else e for e in events],
        [replace(e, payload={**e.payload, "arguments": {"goal": e.payload["arguments"]["goal"]}}) if e.seq == bad.seq else e for e in events],
        [*events, replace(receipt, seq=events[-1].seq + 1)],
    ]
    for altered in variants:
        test.assertEqual(source_index(altered)["objective_source"]["status"], "unknown")


def assert_noop_attempt_chain(test, events, source):
    receipts = [e for e in events if e.type == "team.tool_finished" and e.payload.get("name") == "set_objective"]
    noop = next(e for e in receipts if e.payload["result"].get("scope_changed") is False)
    start = next(e for e in events if e.seq == noop.payload["started_seq"])
    original = noop.payload["result"]
    changed = next(e for e in receipts if e.payload["result"].get("scope_changed") is True)
    changed_start = next(e for e in events if e.seq == changed.payload["started_seq"])
    test.assertEqual(source_index(events)["objective_source"]["message_seq"], source.seq)
    with_call_id = [replace(e, payload={**e.payload, "call_id": start.payload["call_id"]}) if e.seq == noop.seq else e for e in events]
    test.assertEqual(source_index(with_call_id)["objective_source"]["message_seq"], source.seq)
    maximum_id = [replace(e, payload={**e.payload, "call_id": "x" * 256}) if e.seq == start.seq else e for e in events]
    test.assertEqual(source_index(maximum_id)["objective_source"]["message_seq"], source.seq)
    changed_with_id = [replace(e, payload={**e.payload, "call_id": changed_start.payload["call_id"]}) if e.seq == changed.seq else e for e in events]
    test.assertEqual(source_index(changed_with_id)["objective_source"]["message_seq"], source.seq)
    variants = [
        [e for e in events if e.seq != noop.seq],
        [replace(e, actor=Actor.agent("team", "team")) if e.seq == noop.seq else e for e in events],
        [replace(e, payload={**e.payload, "result": {**original, "goal": "different"}}) if e.seq == noop.seq else e for e in events],
        [replace(e, payload={**e.payload, "result": {**original, "revision": True}}) if e.seq == noop.seq else e for e in events],
        [replace(e, payload={**e.payload, "result": {**original, "scope_changed": True}}) if e.seq == noop.seq else e for e in events],
        [replace(e, payload={**e.payload, "result": {**original, "outcome": "effects_unknown"}}) if e.seq == noop.seq else e for e in events],
        [replace(e, payload={**e.payload, "arguments": {"goal": "different"}}) if e.seq == start.seq else e for e in events],
        [replace(e, payload={**e.payload, "call_id": "different"}) if e.seq == noop.seq else e for e in events],
        [replace(e, payload={**e.payload, "call_id": None}) if e.seq == noop.seq else e for e in events],
        [replace(e, payload={**e.payload, "call_id": "different"}) if e.seq == changed.seq else e for e in events],
        *[[replace(e, payload={**e.payload, "call_id": bad_id}) if e.seq == start.seq else e for e in events]
          for bad_id in (None, "", "  ", "x" * 257, 7)],
        [replace(e, payload={k: v for k, v in e.payload.items() if k != "call_id"}) if e.seq == start.seq else e for e in events],
        [replace(e, payload={k: v for k, v in e.payload.items() if k != "call_id"}) if e.seq == changed_start.seq else e for e in events],
        [*events, replace(noop, seq=events[-1].seq + 1)],
    ]
    for altered in variants:
        test.assertEqual(source_index(altered)["objective_source"]["status"], "unknown")
    with test.assertRaises(CustomerSourceIndexError):
        source_index([events[1], events[0], *events[2:]])


class CustomerInputSourceTests(unittest.TestCase):
    def test_many_customer_messages_show_omissions_and_paginate_original_sources(self):
        from cater.team_customer_sources import index, read
        messages = [self.rt.team.receive(self.task, f'Customer input {i}', request_id=f'visibility-{i}') for i in range(75)]
        self.rt.log.emit(type='team.run_started', task_id=self.task, actor=HOST,
            payload={'message_seq': messages[0].seq, 'role': self.manager, 'run_id': 'interrupted-input', 'revision': 0})
        self.rt.log.emit(type='team.run_finished', task_id=self.task, actor=HOST,
            payload={'message_seq': messages[0].seq, 'role': self.manager, 'run_id': 'interrupted-input', 'status': 'interrupted'})
        context = self.context(messages[-1], self.manager)
        window = context['context_window']
        self.assertGreater(window['customer_messages_omitted_count'], 0)
        self.assertIn('不应声称已完整理解', window['omission_note'])
        sources = context['customer_input_sources']
        self.assertTrue(sources['has_more'])
        self.assertIn('被中断的处理不等于已理解', sources['note'])
        events = self.rt.log.read(task_id=self.task)
        page = sources
        found = []
        while True:
            found.extend(page['entries'])
            if not page['has_more']:
                break
            page = index(events, task_id=self.task, member_ids={r.id for r in [self.rt.organization.leader, *self.rt.roles]},
                         as_of_seq=sources['as_of_seq'], as_of_hash=sources['as_of_hash'],
                         before_seq=page['next_before_seq'], before_part=page['next_before_part'])
        original = next(row for row in found if row['seq'] == messages[0].seq)
        body = read(events, message_seq=original['seq'], expected_hash=original['hash'])
        self.assertEqual(body['text'], messages[0].payload['text'])

    def setUp(self):
        fixture.SimpleTeamTests.setUp(self)

    def context(self, message, role):
        return model_context(self.rt.team._context(self.task, message, self.rt.team._role(role)))

    def call(self, arguments, role=None):
        return self.rt.team._execute(self.task, self.rt.team._role(role or self.manager),
                                     "read_messages", arguments, None, None, None, None)

    def test_customer_message_discovery_and_receipt_guidance(self):
        source = self.rt.team.receive(self.task, "Customer source", request_id="discovery-guidance")
        receipt = self.rt.log.emit(type="team.tool_finished", task_id=self.task, actor=HOST,
                                   payload={"name": "workspace_read", "result": {"text": "receipt secret"}})
        schema = next(t for t in self.rt.team._schemas(self.rt.team._role(self.manager), None)
                      if t["name"] == "read_messages")
        props = schema["input_schema"]["properties"]
        for field in ("message_seq", "expected_hash", "acceptance_index", "source_field"):
            self.assertIn("customer_message", props[field]["description"])
        for args in ({"message_seq": receipt.seq}, {"expected_hash": source.hash},
                     {"message_seq": receipt.seq, "expected_hash": receipt.hash},
                     {"message_seq": source.seq, "expected_hash": "wrong"}):
            with self.subTest(args=args), self.assertRaises(ValueError) as error:
                self.call({"scope": "customer_message", **args})
            self.assertIn("customer_index", str(error.exception))
            self.assertIn("read_receipts", str(error.exception))
            self.assertIn("receipt_seq", str(error.exception))
            self.assertNotIn("receipt secret", str(error.exception))
        self.assertEqual(self.call({"scope": "customer_message", "message_seq": source.seq,
                                    "expected_hash": source.hash})["text"], "Customer source")
        self.assertEqual(self.driver.calls, [])

    def test_native_tool_history_contains_fixed_customer_guidance(self):
        source = self.rt.team.receive(self.task, "PRIVATE_CUSTOMER_TEXT", request_id="native-guidance")
        receipt = self.rt.log.emit(type="team.tool_finished", task_id=self.task, actor=HOST,
                                   payload={"name": "workspace_read", "result": {"text": "PRIVATE_RECEIPT_TEXT"}})
        queries = [{"scope": "customer_message", "message_seq": receipt.seq},
                   {"scope": "customer_message", "message_seq": receipt.seq, "expected_hash": receipt.hash},
                   {"scope": "customer_message", "message_seq": source.seq, "expected_hash": "PRIVATE_HASH"},
                   {"scope": "customer_message", "message_seq": source.seq, "unexpected": "PRIVATE_ARGUMENT"}]
        self.driver.steps[self.manager] = [fixture.turn("", *(("read_messages", args) for args in queries)),
                                          fixture.turn("Done")]
        self.rt.collaborate(self.task)
        replies = [json.loads(row["content"]) for row in self.driver.calls[-1]["history"] if row.get("role") == "tool"]
        self.assertEqual(len(replies), len(queries))
        for result in replies:
            self.assertEqual(result["status"], "error")
            self.assertIn("customer_index", result["message"])
            self.assertIn("read_receipts", result["message"])
            self.assertIn("receipt_seq", result["message"])
            for secret in ("PRIVATE_CUSTOMER_TEXT", "PRIVATE_RECEIPT_TEXT", "PRIVATE_HASH", "PRIVATE_ARGUMENT"):
                self.assertNotIn(secret, json.dumps(result, ensure_ascii=False))

    def test_unavailable_index_preserves_original_conversation_and_live_context(self):
        self.rt.team.receive(self.task, "Keep this earlier customer input", request_id="source-degrade-earlier")
        for i in range(70):
            self.rt.log.emit(type="msg.said", task_id=self.task, actor=Actor.agent(self.worker, "dev"),
                             payload={"text": f"Progress {i}: " + "x" * 400})
        message = self.rt.team.receive(self.task, "Current question", request_id="source-degrade-current")
        raw = self.rt.team._context(self.task, message, self.rt.team._role(self.manager))
        original = model_context({key: value for key, value in raw.items() if key != "customer_input_sources"})
        with patch("cater.team_customer_sources.INDEX_JSON_BYTES", 20):
            degraded = self.context(message, self.manager)
        marker = degraded["customer_input_sources"]
        self.assertEqual((marker["status"], marker["complete"]), ("unavailable", False))
        self.assertIn("完整性未知", marker["note"])
        self.assertIn("未验证", degraded["objective_source_note"])
        self.assertNotIn("entries", marker)
        self.assertNotIn("has_more", marker)
        self.assertEqual(degraded["conversation"], original["conversation"])
        self.assertEqual(degraded["context_window"]["conversation_budget_bytes"], CONVERSATION_BYTES)
        self.assertEqual(degraded["context_window"], original["context_window"])
        for field in ("message", "latest_customer_message", "current_work", "open_assignments", "reviews"):
            self.assertEqual(degraded[field], original[field])

    def test_invalid_index_history_degrades_but_unexpected_errors_propagate(self):
        message = self.rt.team.receive(self.task, "Continue", request_id="source-degrade-invalid")
        events = self.rt.log.read(task_id=self.task)
        malformed = [replace(e, payload=[]) if e.seq == events[-1].seq else e for e in events]
        with self.assertRaises(CustomerSourceIndexError):
            source_index(malformed)
        def index_invalid_history(_events, **kwargs):
            return raw_source_index(malformed, **kwargs)
        with patch("cater.team_customer_sources.index", side_effect=index_invalid_history):
            result = self.context(message, self.manager)
        self.assertEqual(result["customer_input_sources"]["status"], "unavailable")
        self.assertFalse(result["customer_input_sources"]["complete"])
        self.assertEqual(result["message"]["text"], message.payload["text"])
        for error in (ValueError("unexpected"), RuntimeError("bug"), MemoryError("resource")):
            with self.subTest(error=type(error).__name__):
                with patch("cater.team_customer_sources.index", side_effect=error):
                    with self.assertRaises(type(error)) as raised:
                        self.context(message, self.manager)
                self.assertIs(raised.exception, error)

    def test_discovery_survives_new_question_and_eviction_for_both_roles(self):
        initial = self.rt.team.receive(self.task, "Original direction", request_id="source-original-input")
        addition = self.rt.team.receive(self.task, "Also preserve the original example", request_id="source-addition-input")
        for i in range(70):
            self.rt.log.emit(type="msg.said", task_id=self.task, actor=Actor.agent(self.worker, "dev"),
                             payload={"text": f"Progress {i}: " + "x" * 400})
        question = self.rt.team.receive(self.task, "How is it going?", request_id="source-progress-input")
        worker_message = self.rt.team._send(self.task, self.rt.team._role(self.manager), self.worker,
                                            "Continue the assigned work", "assignment")
        for role, message in ((self.manager, question), (self.worker, worker_message)):
            projected = self.context(message, role)
            self.assertEqual(projected["latest_customer_message"]["seq"], question.seq)
            self.assertNotIn(addition.seq, [row["seq"] for row in projected["conversation"]])
            sources = projected["customer_input_sources"]
            created = next(e for e in self.rt.log.read(task_id=self.task) if e.type == "task.created")
            self.assertEqual([row["seq"] for row in sources["entries"]], [question.seq, addition.seq, initial.seq, created.seq])
            self.assertIn("不判断", sources["note"])
            self.assertEqual(sources["entries"][1]["hash"], addition.hash)
            self.assertFalse(sources["entries"][1]["preview_truncated"])
            self.assertEqual(sources["entries"][1]["preview"], addition.payload["text"])
            self.assertIn(sources["entries"][0]["preview_ref"], ("message.text", "latest_customer_message.text"))
            self.assertEqual(sources["entries"][-1]["preview_ref"], "current_work.objective")
            read = self.call({"scope": "customer_message", "message_seq": addition.seq,
                              "expected_hash": addition.hash}, role)
            self.assertEqual(read["text"], addition.payload["text"])
            self.assertFalse(read["has_more"])
            self.assertEqual(self.call({"scope": "customer_message", "message_seq": created.seq,
                                        "expected_hash": created.hash}, role)["text"], created.payload["goal"])
        self.assertEqual(self.driver.calls, [])

    def test_bounded_index_pagination_snapshot_goal_switch_and_withdrawal(self):
        before = self.rt.team.receive(self.task, "Initial scope", request_id="source-before-switch")
        pivot = self.rt.team.receive(self.task, "Change the direction", request_id="source-pivot-message")
        goal = self.rt.log.emit(type="team.objective_updated", task_id=self.task, actor=HOST,
                                payload={"goal": "New direction"})
        change = self.rt.team.receive(self.task, "Include the extra export", request_id="source-new-change")
        withdraw = self.rt.team.receive(self.task, "Actually remove that export", request_id="source-withdrawal")
        for i in range(12):
            self.rt.team.receive(self.task, f"Question {i}", request_id=f"source-question-{i:02d}")
        message = self.rt.team.receive(self.task, "Current question", request_id="source-current-question")
        index = self.context(message, self.manager)["customer_input_sources"]
        self.assertEqual(index["objective_update_seq"], goal.seq)
        self.assertEqual(index["objective_source"], {"status": "unknown", "objective_update_seq": goal.seq})
        self.assertEqual(len(index["entries"]), INDEX_SIZE)
        self.assertTrue(index["has_more"])
        self.assertLess(json_bytes(index), INDEX_JSON_BYTES)
        late = self.rt.team.receive(self.task, "Later input", request_id="source-later-input")
        entries = list(index["entries"])
        cursor, part = index["next_before_seq"], index["next_before_part"]
        while cursor is not None:
            page = self.call({"scope": "customer_index", "as_of_seq": index["as_of_seq"], "as_of_hash": index["as_of_hash"],
                              "before_seq": cursor, "before_part": part, "limit": 3})
            entries.extend(page["entries"])
            cursor, part = page["next_before_seq"], page["next_before_part"]
        seqs = [entry["seq"] for entry in entries]
        self.assertEqual(len(seqs), len(set(seqs)))
        self.assertEqual(seqs, sorted(seqs, reverse=True))
        for event in (before, pivot, change, withdraw, message):
            self.assertIn(event.seq, seqs)
        self.assertNotIn(late.seq, seqs)
        self.assertEqual(self.call({"scope": "customer_message", "message_seq": withdraw.seq,
                                   "expected_hash": withdraw.hash})["text"], withdraw.payload["text"])

    def test_long_unicode_and_escaped_text_reconstructs_under_wire_budget(self):
        text = ('🐾汉"\\\n' * 4500) + "END"
        source = self.rt.team.receive(self.task, text, request_id="source-long-unicode")
        chunks, offset = [], 0
        while True:
            result = self.call({"scope": "customer_message", "message_seq": source.seq,
                                "expected_hash": source.hash, "offset": offset, "length": 4000})
            self.assertLessEqual(len(json_utf8({"text": result["text"]})), TEXT_JSON_BYTES)
            self.assertEqual(result["offset"], offset)
            chunks.append(result["text"])
            if not result["has_more"]:
                self.assertIsNone(result["next_offset"])
                break
            self.assertGreater(result["next_offset"], offset)
            offset = result["next_offset"]
        self.assertEqual("".join(chunks), text)

    def test_goal_change_anchors_only_a_receipted_host_run_customer_input(self):
        source = self.rt.team.receive(self.task, "Please change the task", request_id="source-real-goal-input")
        self.driver.steps[self.manager] = [fixture.turn("", ("set_objective", {"goal": "New objective"})),
                                          fixture.turn("Updated")]
        self.rt.collaborate(self.task)
        events = self.rt.log.read(task_id=self.task)
        goal, = [e for e in events if e.type == "team.objective_updated"]
        result = source_index(events)
        origin = result["objective_source"]
        self.assertEqual({key: origin[key] for key in ("status", "objective_update_seq", "message_seq", "message_hash")},
                         {"status": "linked_customer_input", "objective_update_seq": goal.seq,
                          "message_seq": source.seq, "message_hash": source.hash})
        self.assertEqual(origin["preview_ref"], f"entries[seq={source.seq},part=0]")
        before_receipt = source_index([e for e in events if e.seq <= goal.seq])
        self.assertEqual(before_receipt["objective_source"], {"status": "unknown", "objective_update_seq": goal.seq})
        run = next(e for e in events if e.type == "team.run_started" and e.payload.get("message_seq") == source.seq)
        receipt = next(e for e in events if e.type == "team.tool_finished" and e.payload.get("name") == "set_objective")
        for altered in (replace(source, actor=Actor.agent("human", "human")),
                        replace(run, actor=Actor.agent("team", "team")),
                        replace(receipt, actor=Actor.agent("team", "team"))):
            altered_events = [altered if e.seq == altered.seq else e for e in events]
            self.assertEqual(source_index(altered_events)["objective_source"],
                             {"status": "unknown", "objective_update_seq": goal.seq})
        for i in range(12):
            self.rt.team.receive(self.task, f"Another question {i}", request_id=f"source-after-goal-{i:02d}")
        current = source_index(self.rt.log.read(task_id=self.task))
        self.assertNotIn(source.seq, [entry["seq"] for entry in current["entries"]])
        self.assertEqual(current["objective_source"]["message_seq"], source.seq)
        self.assertFalse(current["objective_source"]["preview_truncated"])
        self.assertEqual(source.payload["text"], current["objective_source"]["preview"])
        self.assertEqual(self.call({"scope": "customer_message", "message_seq": source.seq,
                                   "expected_hash": source.hash})["text"], source.payload["text"])

    def test_preview_budget_escaping_unicode_and_page_boundaries(self):
        sources = []
        for i in range(55):
            text = f"{i:02d}:" + ('🐾汉"\\\n\x01' * 200)
            sources.append(self.rt.team.receive(self.task, text, request_id=f"source-preview-{i:03d}"))
        events = self.rt.log.read(task_id=self.task)
        initial = source_index(events, limit=50)
        entries, page = [], initial
        while True:
            self.assertLessEqual(len(json_utf8(page)), INDEX_JSON_BYTES)
            entries.extend(page["entries"])
            for row in page["entries"]:
                event = next(e for e in events if e.seq == row["seq"])
                text = (event.payload["acceptance"][row["acceptance_index"]] if row["source_type"] == "task_acceptance"
                        else event.payload["goal"] if event.type == "task.created" else event.payload["text"])
                self.assertTrue(text.startswith(row["preview"]))
                self.assertEqual(row["preview_truncated"], len(row["preview"]) < len(text))
                self.assertLessEqual(len(row["preview"].encode("utf-8")), PREVIEW_UTF8_BYTES)
                self.assertLessEqual(len(json_utf8({"preview": row["preview"]})), PREVIEW_JSON_BYTES)
            if not page["has_more"]:
                break
            page = self.call({"scope": "customer_index", "as_of_seq": initial["as_of_seq"],
                              "as_of_hash": initial["as_of_hash"], "before_seq": page["next_before_seq"],
                              "before_part": page["next_before_part"], "limit": 50})
        identities = [(row["seq"], row["part"]) for row in entries]
        self.assertEqual(len(identities), len(set(identities)))
        self.assertTrue({(event.seq, 0) for event in sources} <= set(identities))
        self.assertEqual(_preview("ab\ud800end"), {"preview": "ab", "preview_truncated": True})

    def test_preview_preserves_short_inputs_and_only_truncates_at_actual_limits(self):
        for text in ("", "好", "不要", "🐾", "🐾猫👍", "否\n定"):
            with self.subTest(text=text):
                self.assertEqual(_preview(text), {"preview": text, "preview_truncated": False})
        for text in ("a" * 96, "猫" * 32, "🐾" * 24):
            with self.subTest(text=text):
                self.assertEqual(len(text.encode("utf-8")), PREVIEW_UTF8_BYTES)
                self.assertEqual(_preview(text), {"preview": text, "preview_truncated": False})
                self.assertEqual(_preview(text + "🐾"), {"preview": text, "preview_truncated": True})
        escaped = "\x01" * 18
        self.assertLessEqual(len(json_utf8({"preview": escaped})), PREVIEW_JSON_BYTES)
        self.assertGreater(len(json_utf8({"preview": escaped + "\x01"})), PREVIEW_JSON_BYTES)
        self.assertEqual(_preview(escaped), {"preview": escaped, "preview_truncated": False})
        self.assertEqual(_preview(escaped + "\x01"), {"preview": escaped, "preview_truncated": True})
        self.assertEqual(_preview("\ud800"), {"preview": "", "preview_truncated": True})

    def test_short_customer_inputs_and_current_reference_survive_projection(self):
        messages = [self.rt.team.receive(self.task, value, request_id=f"source-short-input-{i}")
                    for i, value in enumerate(("好", "不要", "🐾"))]
        current = self.rt.team.receive(self.task, "还有多久", request_id="source-short-current")
        context = self.context(current, self.manager)
        sources = context["customer_input_sources"]
        self.assertIn("不能直接推为当前要求", sources["note"])
        self.assertIn("可能完整也可能为节选", sources["note"])
        for message in messages:
            entry = next(row for row in sources["entries"] if row["seq"] == message.seq)
            self.assertEqual((entry["preview"], entry["preview_truncated"]), (message.payload["text"], False))
        current_entry = next(row for row in sources["entries"] if row["seq"] == current.seq)
        self.assertEqual(current_entry["preview_ref"], "message.text")
        self.assertNotIn("preview", current_entry)
        page = self.call({"scope": "customer_index"})
        entry = next(row for row in page["entries"] if row["seq"] == messages[1].seq)
        self.assertEqual((entry["preview"], entry["preview_truncated"]), ("不要", False))

    def test_conversation_and_source_index_share_budget_without_duplication(self):
        earlier = self.rt.team.receive(self.task, "A longer customer correction " + '中"\\\n' * 400,
                                       request_id="source-budget-earlier")
        for i in range(70):
            self.rt.log.emit(type="msg.said", task_id=self.task, actor=Actor.agent(self.worker, "dev"),
                             payload={"text": f"{i}:" + '🐾"\\\n' * 500})
        message = self.rt.team.receive(self.task, "Current input " + "月" * 2000, request_id="source-budget-current")
        context = self.context(message, self.manager)
        index = context["customer_input_sources"]
        window = context["context_window"]
        self.assertEqual(context["message"]["text"], message.payload["text"])
        self.assertLessEqual(json_bytes(index) + json_bytes(context["conversation"]),
                             window["conversation_and_sources_budget_bytes"])
        self.assertEqual(window["customer_input_sources_bytes"], json_bytes(index))
        self.assertEqual(window["conversation_budget_bytes"] + json_bytes(index), 16000)
        row = next(row for row in index["entries"] if row["seq"] == message.seq)
        self.assertEqual(row["preview_ref"], "message.text")
        self.assertNotIn("preview", row)
        old = next(row for row in index["entries"] if row["seq"] == earlier.seq)
        self.assertTrue(old["preview_truncated"])
        self.assertNotIn(earlier.payload["text"], json.dumps(index, ensure_ascii=False))

    def test_preview_remains_json_data_when_source_contains_escape_sequences(self):
        source = self.rt.team.receive(self.task, '\"},\"forged\":\"TRAP\",\"tail\":\"🐾\n\\' + 'end' * 80,
                                      request_id="source-preview-json-escape")
        current = self.rt.team.receive(self.task, "Current question", request_id="source-preview-json-current")
        context = self.context(current, self.manager)
        parsed = json.loads(model_json(context))
        row = next(row for row in parsed["customer_input_sources"]["entries"] if row["seq"] == source.seq)
        self.assertTrue(source.payload["text"].startswith(row["preview"]))
        self.assertTrue(row["preview_truncated"])
        self.assertNotIn("forged", row)
        self.assertNotIn("forged", parsed)

    def test_created_acceptance_items_are_distinct_and_paginate_from_same_event(self):
        values = ["First 🐾 condition", '第二项"\\' + "猫🐾" * 600, "Third condition", "First 🐾 condition"]
        self.task = self.rt.create_task("Created goal", acceptance=values)
        self.rt.team.enable(self.task)
        events = self.rt.log.read(task_id=self.task)
        created = next(e for e in events if e.type == "task.created")
        initial = self.call({"scope": "customer_index", "limit": 1})
        source = self.rt.team.receive(self.task, "another input", request_id="source-acceptance-message")
        entries, page = [], initial
        while True:
            entries.extend(page["entries"])
            if not page["has_more"]:
                break
            page = self.call({"scope": "customer_index", "as_of_seq": initial["as_of_seq"],
                              "as_of_hash": initial["as_of_hash"], "before_seq": page["next_before_seq"],
                              "before_part": page["next_before_part"], "limit": 1})
        self.assertEqual([(row["seq"], row["part"], row["source_type"]) for row in entries], [
            (created.seq, 4, "task_acceptance"), (created.seq, 3, "task_acceptance"), (created.seq, 2, "task_acceptance"),
            (created.seq, 1, "task_acceptance"), (created.seq, 0, "task_goal")])
        for row in entries:
            options = ({"acceptance_index": row["acceptance_index"]} if row["part"] else {})
            expected = values[row["acceptance_index"]] if row["part"] else "Created goal"
            content, offset = [], 0
            while True:
                data = self.call({"scope": "customer_message", "message_seq": row["seq"],
                                  "expected_hash": row["hash"], "offset": offset, "length": 7, **options})
                content.append(data["text"])
                if not data["has_more"]:
                    break
                offset = data["next_offset"]
            self.assertEqual("".join(content), expected)
        for args in ({"scope": "customer_message", "message_seq": created.seq, "expected_hash": created.hash, "acceptance_index": 4},
                     {"scope": "customer_message", "message_seq": created.seq, "expected_hash": created.hash, "acceptance_index": True},
                     {"scope": "customer_message", "message_seq": source.seq, "expected_hash": source.hash, "acceptance_index": 0},
                     {"scope": "customer_index", "before_part": 1}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.call(args)
        other = self.rt.create_task("Foreign goal", acceptance=["Foreign acceptance"])
        foreign = next(self.rt.log.iter_events(task_id=other, event_types=("task.created",)))
        with self.assertRaises(ValueError):
            self.call({"scope": "customer_message", "message_seq": foreign.seq,
                       "expected_hash": foreign.hash, "acceptance_index": 0})

    def test_two_native_goal_changes_require_complete_receipt_chain(self):
        source = self.rt.team.receive(self.task, "Update our direction", request_id="source-native-two-goals")
        self.driver.steps[self.manager] = [fixture.turn("", ("set_objective", {"goal": "First objective"}),
            ("set_objective", {"goal": "Second objective"})), fixture.turn("Updated")]
        self.rt.collaborate(self.task)
        events = self.rt.log.read(task_id=self.task)
        goals = [e for e in events if e.type == "team.objective_updated"]
        starts = [e for e in events if e.type == "team.tool_started" and e.payload.get("name") == "set_objective"]
        self.assertEqual(len(goals), 2)
        self.assertEqual(starts[1].payload["revision"], goals[0].seq)
        self.assertEqual(source_index(events)["objective_source"]["message_seq"], source.seq)
        receipt = next(e for e in events if e.type == "team.tool_finished" and e.payload.get("started_seq") == starts[0].seq)
        variants = [
            [e for e in events if e.seq != receipt.seq],
            [replace(e, actor=Actor.agent("team", "team")) if e.seq == receipt.seq else e for e in events],
            [replace(e, payload={**e.payload, "revision": 0}) if e.seq == starts[1].seq else e for e in events],
            [replace(e, payload={**e.payload, "message_seq": float(source.seq)}) if e.seq == starts[1].seq else e for e in events],
            [replace(e, payload={**e.payload, "result": {**e.payload["result"], "revision": 0}}) if e.seq == receipt.seq else e for e in events],
        ]
        for altered in variants:
            self.assertEqual(source_index(altered)["objective_source"]["status"], "unknown")

    def test_native_invalid_goal_attempt_then_success_has_proven_source(self):
        source = self.rt.team.receive(self.task, "Change the direction", request_id="source-native-invalid-goal")
        self.driver.steps[self.manager] = [fixture.turn("", ("set_objective", {"goal": "First objective"}),
            ("set_objective", {"goal": "Second objective", "unexpected": "x"}),
            ("set_objective", {"goal": "Second objective"})), fixture.turn("Updated")]
        self.rt.collaborate(self.task)
        events = self.rt.log.read(task_id=self.task)
        self.assertEqual(len([e for e in events if e.type == "team.objective_updated"]), 2)
        assert_invalid_attempt_chain(self, events, source)
        self.assertTrue(self.rt.verify_chain().ok)

    def test_native_invalid_first_goal_then_success_has_proven_source(self):
        source = self.rt.team.receive(self.task, "Change the direction", request_id="source-native-first-invalid")
        self.driver.steps[self.manager] = [fixture.turn("", ("set_objective", {"goal": "New goal", "unexpected": "x"}),
            ("set_objective", {"goal": "New goal"})), fixture.turn("Updated")]
        self.rt.collaborate(self.task)
        events = self.rt.log.read(task_id=self.task)
        self.assertEqual(len([e for e in events if e.type == "team.objective_updated"]), 1)
        assert_invalid_attempt_chain(self, events, source)

    def test_redacted_invalid_arguments_cannot_prove_prevalidation_failure(self):
        self.rt.team.receive(self.task, "Change the direction", request_id="source-redacted-invalid")
        self.driver.steps[self.manager] = [fixture.turn("", ("set_objective", {"goal": "New goal", "content": "private"}),
            ("set_objective", {"goal": "New goal"})), fixture.turn("Updated")]
        self.rt.collaborate(self.task)
        events = self.rt.log.read(task_id=self.task)
        starts = [e for e in events if e.type == "team.tool_started" and e.payload.get("name") == "set_objective"]
        self.assertEqual(starts[0].payload["arguments"], {"goal": "New goal"})
        self.assertEqual(source_index(events)["objective_source"]["status"], "unknown")

    def test_native_unchanged_then_changed_goal_keeps_source(self):
        source = self.rt.team.receive(self.task, "Change direction", request_id="source-native-unchanged")
        goal = self.rt.team.view(self.task)["objective"]
        self.driver.steps[self.manager] = [fixture.turn("", ("set_objective", {"goal": goal}),
            ("set_objective", {"goal": "New goal"})), fixture.turn("Updated")]
        self.rt.collaborate(self.task)
        events = self.rt.log.read(task_id=self.task)
        assert_noop_attempt_chain(self, events, source)
        self.assertEqual(raw_source_index(events)["objective_source"]["status"], "unknown")
        altered = []
        for event in events:
            if event.seq == source.seq:
                altered.append(replace(event, payload={**event.payload, "recipient": "outsider"}))
            elif event.type in {"team.run_started", "team.tool_started", "team.tool_finished"}:
                altered.append(replace(event, payload={**event.payload, "role": "outsider"}))
            else:
                altered.append(event)
        self.assertEqual(source_index(altered)["objective_source"]["status"], "unknown")

    def test_legacy_goal_and_acceptance_are_anchored_without_exposing_instruction(self):
        self.task = self.rt.create_task("Original goal", acceptance=["Original criterion"])
        command = self.rt.command(self.task, "PRIVATE_INSTRUCTION_ONLY", goal="Legacy goal",
                                  acceptance=["Revised criterion", "Another criterion"])
        self.rt.team.enable(self.task)
        source = self.rt.team.receive(self.task, "Proceed", request_id="source-legacy-native")
        self.assertEqual(self.rt.team.view(self.task)["objective"], "Legacy goal")
        events = self.rt.log.read(task_id=self.task)
        created = next(e for e in events if e.type == "task.created")
        earlier = source_index(events, as_of_seq=created.seq, as_of_hash=created.hash)
        self.assertEqual(earlier["objective_source"]["status"], "initial_goal")
        self.assertEqual(earlier["acceptance_source"]["status"], "initial_acceptance")
        context = self.context(source, self.manager)
        index = context["customer_input_sources"]
        self.assertEqual(index["objective_source"], {"status": "legacy_command_goal", "event_seq": command.seq,
            "event_hash": command.hash, "readable": True, "preview_ref": f"entries[seq={command.seq},part=0]"})
        self.assertEqual(index["acceptance_source"], {"status": "legacy_command_acceptance", "event_seq": command.seq,
            "event_hash": command.hash, "count": 2, "readable": True})
        self.assertNotIn("PRIVATE_INSTRUCTION_ONLY", json.dumps(index))
        self.assertIn("instruction正文仅按需读取，goal/acceptance可有预览，均不自动判定为有效要求", index["note"])
        self.assertEqual([row["source_type"] for row in index["entries"] if row["seq"] == command.seq],
                         ["legacy_command_instruction", "legacy_command_acceptance", "legacy_command_acceptance", "legacy_command_goal"])
        for role in (self.manager, self.worker):
            self.assertEqual(self.call({"scope": "customer_message", "message_seq": command.seq,
                                        "expected_hash": command.hash}, role)["text"], "Legacy goal")
            self.assertEqual(self.call({"scope": "customer_message", "message_seq": command.seq,
                                        "expected_hash": command.hash, "acceptance_index": 0}, role)["text"], "Revised criterion")
            self.assertEqual(self.call({"scope": "customer_message", "message_seq": command.seq,
                                        "expected_hash": command.hash, "source_field": "instruction"}, role)["text"],
                             "PRIVATE_INSTRUCTION_ONLY")
        self.driver.steps[self.manager] = [fixture.turn("", ("set_objective", {"goal": "Legacy goal"}),
            ("set_objective", {"goal": "New goal"})), fixture.turn("Updated")]
        self.rt.collaborate(self.task)
        events = self.rt.log.read(task_id=self.task)
        assert_noop_attempt_chain(self, events, source)
        for actor in (Actor.agent("human", "human"), Actor(id="other", role="human", kind="human"), Actor.human("other")):
            altered = [replace(e, actor=actor) if e.seq == command.seq else e for e in events]
            index = source_index(altered)
            self.assertEqual(index["objective_source"]["status"], "unknown")
            self.assertEqual(index["acceptance_source"]["status"], "unknown")
        untrusted_creation = [replace(e, actor=Actor.agent("human", "human")) if e.seq == created.seq else e for e in events]
        untrusted_index = source_index(untrusted_creation)
        self.assertEqual(untrusted_index["objective_source"]["status"], "unknown")
        self.assertEqual(untrusted_index["acceptance_source"]["status"], "unknown")

    def test_acceptance_only_legacy_command_keeps_initial_goal_source(self):
        self.task = self.rt.create_task("Original goal", acceptance=["Original criterion"])
        created = next(self.rt.log.iter_events(task_id=self.task, event_types=("task.created",)))
        command = self.rt.command(self.task, "Update acceptance", acceptance=["New criterion"])
        self.rt.team.enable(self.task)
        source = self.rt.team.receive(self.task, "Continue", request_id="source-legacy-acceptance")
        index = self.context(source, self.manager)["customer_input_sources"]
        self.assertEqual(index["objective_source"]["event_seq"], created.seq)
        self.assertEqual(index["objective_source"]["status"], "initial_goal")
        self.assertEqual(index["acceptance_source"]["event_seq"], command.seq)
        self.assertEqual(index["acceptance_source"]["status"], "legacy_command_acceptance")
        self.assertTrue(index["acceptance_source"]["readable"])
        self.assertEqual(self.rt.task(self.task).acceptance, ["New criterion"])
        self.driver.steps[self.manager] = [fixture.turn("", ("set_objective", {"goal": "Original goal"}),
            ("set_objective", {"goal": "New goal"})), fixture.turn("Updated")]
        self.rt.collaborate(self.task)
        assert_noop_attempt_chain(self, self.rt.log.read(task_id=self.task), source)

    def test_legacy_sources_page_by_event_and_part_with_snapshot_and_identity_checks(self):
        self.task = self.rt.create_task("Original", acceptance=["Original criterion"])
        values = ["第一项🐾" * 150, '否定 "\\ end', "第三项"]
        command = self.rt.command(self.task, "INSTRUCTION_NOT_SOURCE", goal="旧目标🐾", acceptance=values)
        self.rt.team.enable(self.task)
        before = self.call({"scope": "customer_index", "limit": 1})
        later = self.rt.command(self.task, "Change later", goal="New goal", acceptance=["New criterion"])
        entries, page = [], before
        while True:
            entries.extend(page["entries"])
            if not page["has_more"]:
                break
            page = self.call({"scope": "customer_index", "as_of_seq": before["as_of_seq"],
                              "as_of_hash": before["as_of_hash"], "before_seq": page["next_before_seq"],
                              "before_part": page["next_before_part"], "limit": 1})
        old = [row for row in entries if row["seq"] == command.seq]
        self.assertEqual([(row["part"], row["source_type"]) for row in old],
                         [(4, "legacy_command_instruction"), (3, "legacy_command_acceptance"), (2, "legacy_command_acceptance"),
                          (1, "legacy_command_acceptance"), (0, "legacy_command_goal")])
        self.assertNotIn(later.seq, [row["seq"] for row in entries])
        self.assertNotIn("INSTRUCTION_NOT_SOURCE", json.dumps(entries))
        for row in old:
            expected = ("INSTRUCTION_NOT_SOURCE" if row["source_type"] == "legacy_command_instruction" else
                        values[row["acceptance_index"]] if row["part"] else "旧目标🐾")
            options = ({"source_field": "instruction"} if row["source_type"] == "legacy_command_instruction" else
                       {"acceptance_index": row["acceptance_index"]} if row["part"] else {})
            chunks, offset = [], 0
            while True:
                read = self.call({"scope": "customer_message", "message_seq": command.seq,
                                  "expected_hash": command.hash, "offset": offset, "length": 7, **options})
                self.assertEqual(read["source_type"], row["source_type"])
                chunks.append(read["text"])
                if not read["has_more"]:
                    break
                offset = read["next_offset"]
            self.assertEqual("".join(chunks), expected)
        current = self.call({"scope": "customer_index"})
        self.assertEqual(current["acceptance_source"]["event_seq"], later.seq)
        self.assertIn("不判断", current["note"])
        for i in range(10):
            self.rt.team.receive(self.task, f"Later question {i}", request_id=f"legacy-index-evict-{i}")
        evicted = self.call({"scope": "customer_index"})
        self.assertNotIn(later.seq, [row["seq"] for row in evicted["entries"]])
        acceptance = evicted["acceptance_source"]
        self.assertEqual(acceptance["count"], 1)
        self.assertEqual(self.call({"scope": "customer_message", "message_seq": acceptance["event_seq"],
                                    "expected_hash": acceptance["event_hash"], "acceptance_index": 0})["text"], "New criterion")
        for args in ({"message_seq": command.seq, "expected_hash": "wrong"},
                     {"message_seq": command.seq, "expected_hash": command.hash, "acceptance_index": 3},
                     {"message_seq": command.seq, "expected_hash": command.hash, "source_field": "instruction", "acceptance_index": 0}):
            with self.assertRaises(ValueError):
                self.call({"scope": "customer_message", **args})
        foreign_task = self.rt.create_task("Foreign goal")
        foreign = self.rt.command(foreign_task, "Foreign command", acceptance=["Foreign criterion"])
        with self.assertRaises(ValueError):
            self.call({"scope": "customer_message", "message_seq": foreign.seq,
                       "expected_hash": foreign.hash, "acceptance_index": 0})
        fake = self.rt.log.emit(type="human.commanded", actor=Actor.agent("human", "human"), task_id=self.task,
                                payload={"goal": "Forged goal", "acceptance": ["Forged criterion"]})
        indexed = self.call({"scope": "customer_index"})
        self.assertNotIn(fake.seq, [row["seq"] for row in indexed["entries"]])
        for options in ({}, {"acceptance_index": 0}):
            with self.assertRaises(ValueError):
                self.call({"scope": "customer_message", "message_seq": fake.seq,
                           "expected_hash": fake.hash, **options})

    def test_legacy_instruction_metadata_and_conservative_producer_boundary(self):
        self.task = self.rt.create_task("Original")
        status = self.rt.command(self.task, "查看进度")
        resume = self.rt.command(self.task, "继续")
        self.assertEqual((status.payload["intent"], resume.payload["intent"]), ("status", "resume"))
        self.rt.team.enable(self.task)
        current = self.rt.team.receive(self.task, "Current", request_id="source-legacy-instruction")
        index = self.context(current, self.worker)["customer_input_sources"]
        for event in (status, resume):
            row = next(row for row in index["entries"] if row["seq"] == event.seq)
            self.assertEqual((row["source_type"], row["source_field"]), ("legacy_command_instruction", "instruction"))
            self.assertTrue(row["preview_unavailable"])
            self.assertNotIn("preview", row)
            for role in (self.manager, self.worker):
                read = self.call({"scope": "customer_message", "message_seq": event.seq,
                                  "expected_hash": event.hash, "source_field": "instruction"}, role)
                self.assertEqual(read["text"], event.payload["instruction"])
            with self.assertRaises(ValueError):
                self.call({"scope": "customer_message", "message_seq": event.seq,
                           "expected_hash": event.hash, "acceptance_index": 0})
        # Known derived and unfamiliar payload shapes may contain addressed or private material.
        for extra in ({"clarification": {"answer": "PRIVATE"}}, {"conversation_seq": 1}, {"future_private": "PRIVATE"}):
            event = self.rt.log.emit(type="human.commanded", actor=Actor.human(), task_id=self.task,
                payload={"instruction": "PRIVATE_DERIVED", "intent": "direction", "next_action": "run", **extra})
            page = self.call({"scope": "customer_index"})
            self.assertNotIn(event.seq, [row["seq"] for row in page["entries"]])
            with self.assertRaises(ValueError):
                self.call({"scope": "customer_message", "message_seq": event.seq,
                           "expected_hash": event.hash, "source_field": "instruction"})

    def test_persisted_derived_or_unknown_commands_do_not_expose_any_source_fields(self):
        for number, extra in enumerate(({"clarification": {"answer": "PRIVATE"}},
                                        {"conversation_seq": 123}, {"future_private": "PRIVATE"})):
            with self.subTest(extra=extra):
                self.task = self.rt.create_task("Original", acceptance=["Original criterion"])
                event = self.rt.log.emit(type="human.commanded", actor=Actor.human(), task_id=self.task,
                    payload={"instruction": "PRIVATE_INSTRUCTION", "intent": "direction", "next_action": "run",
                             "goal": "PRIVATE_GOAL", "acceptance": ["PRIVATE_CRITERION", "Other"], **extra})
                self.rt.team.enable(self.task)
                message = self.rt.team.receive(self.task, "Continue", request_id=f"legacy-bad-shape-{number}")
                index = self.context(message, self.manager)["customer_input_sources"]
                self.assertEqual(index["objective_source"], {"status": "unknown"})
                self.assertEqual(index["acceptance_source"], {"status": "unknown"})
                self.assertNotIn("PRIVATE", json.dumps(index))
                first = self.call({"scope": "customer_index", "limit": 1})
                page = first
                while True:
                    self.assertNotIn(event.seq, [row["seq"] for row in page["entries"]])
                    if not page["has_more"]:
                        break
                    page = self.call({"scope": "customer_index", "as_of_seq": first["as_of_seq"],
                        "as_of_hash": first["as_of_hash"], "before_seq": page["next_before_seq"],
                        "before_part": page["next_before_part"], "limit": 1})
                arguments = [{"scope": "customer_message", "message_seq": event.seq, "expected_hash": event.hash, **options}
                             for options in ({}, {"acceptance_index": 0}, {"acceptance_index": 1},
                                             {"source_field": "instruction"})]
                for role in (self.manager, self.worker):
                    for args in arguments:
                        with self.assertRaises(ValueError):
                            self.call(args, role)
                self.driver.steps[self.manager] = [fixture.turn("", *(('read_messages', args) for args in arguments)),
                                                   fixture.turn("Done")]
                self.rt.collaborate(self.task)
                receipts = [e.payload["result"] for e in self.rt.log.iter_events(task_id=self.task,
                    event_types=("team.tool_finished",)) if e.payload.get("name") == "read_messages"]
                self.assertEqual([r["status"] for r in receipts], ["error"] * len(arguments))
                self.assertTrue(self.rt.verify_chain().ok)

    def test_untrusted_partial_command_only_invalidates_the_changed_source(self):
        for number, changes in enumerate(({"goal": "UNTRUSTED_GOAL"}, {"acceptance": ["UNTRUSTED_ITEM"]})):
            with self.subTest(changes=changes):
                self.task = self.rt.create_task("Original", acceptance=["Original criterion"])
                event = self.rt.log.emit(type="human.commanded", actor=Actor.human(), task_id=self.task,
                    payload={"instruction": "Unfamiliar", "intent": "direction", "next_action": "run",
                             "unknown_field": True, **changes})
                self.rt.team.enable(self.task)
                index = self.call({"scope": "customer_index"})
                self.assertEqual(index["objective_source"]["status"], "unknown" if "goal" in changes else "initial_goal")
                self.assertEqual(index["acceptance_source"]["status"], "unknown" if "acceptance" in changes else "initial_acceptance")
                self.assertNotIn(event.seq, [row["seq"] for row in index["entries"]])
                self.assertNotIn("UNTRUSTED", json.dumps(index))

    def test_real_direct_command_with_roster_and_strategy_remains_readable(self):
        self.task = self.rt.create_task("Original")
        command = self.rt.command(self.task, f"只让 @{self.worker} 一起审查", goal="Revised goal", acceptance=["Revised item"])
        self.assertEqual(command.payload.get("requested_strategy"), "cabinet_review")
        self.assertIn(self.worker, command.payload.get("mentioned_workers", []))
        self.assertIn(command.payload.get("roster_mode"), ("replace", "remove", "append"))
        self.rt.team.enable(self.task)
        index = self.call({"scope": "customer_index"})
        self.assertEqual(index["objective_source"]["status"], "legacy_command_goal")
        self.assertEqual(index["acceptance_source"]["status"], "legacy_command_acceptance")
        for options, expected in (({}, "Revised goal"), ({"acceptance_index": 0}, "Revised item"),
                                  ({"source_field": "instruction"}, command.payload["instruction"])):
            self.assertEqual(self.call({"scope": "customer_message", "message_seq": command.seq,
                                        "expected_hash": command.hash, **options})["text"], expected)

    def test_persisted_historical_direct_shapes_keep_sources_through_noop_then_change(self):
        for number, (instruction, extra) in enumerate((("Historical direction", {}),
            ("Historical direction", {"mentions": ["retired-role"]}),
            ("", {"next_action": "run"}),
            ("Historical direction", {"next_action": "battle", "requested_strategy": "cabinet_review",
                                      "mentions": [self.worker], "mentioned_workers": [self.worker], "roster_mode": "append"}))):
            with self.subTest(extra=extra):
                self.task = self.rt.create_task("Original", acceptance=["Original item"])
                command = self.rt.log.emit(type="human.commanded", actor=Actor.human(), task_id=self.task,
                    payload={"instruction": instruction, "goal": "Historical goal",
                             "acceptance": ["一🐾", "Second"], **extra})
                self.rt.team.enable(self.task)
                source = self.rt.team.receive(self.task, "Change direction", request_id=f"old-command-{number}")
                context = self.context(source, self.manager)
                index = context["customer_input_sources"]
                self.assertEqual(index["objective_source"]["event_seq"], command.seq)
                self.assertEqual(index["acceptance_source"]["event_seq"], command.seq)
                self.assertNotIn("objective_source_note", context)
                for options, expected in (({}, "Historical goal"), ({"acceptance_index": 0}, "一🐾"),
                                          ({"acceptance_index": 1}, "Second"),
                                          ({"source_field": "instruction"}, instruction)):
                    self.assertEqual(self.call({"scope": "customer_message", "message_seq": command.seq,
                                                "expected_hash": command.hash, **options})["text"], expected)
                self.driver.steps[self.manager] = [fixture.turn("", ("set_objective", {"goal": "Historical goal"}),
                    ("set_objective", {"goal": "New goal"})), fixture.turn("Updated")]
                self.rt.collaborate(self.task)
                assert_noop_attempt_chain(self, self.rt.log.read(task_id=self.task), source)
                self.assertTrue(self.rt.verify_chain().ok)

    def test_historical_nontext_or_empty_instruction_does_not_hide_goal_or_acceptance(self):
        cases = [("", {}), (None, {}), (42, {}), ({"opaque": "not original text"}, {}),
                 ("", {"next_action": "run"})]
        for number, (instruction, extra) in enumerate(cases):
            with self.subTest(instruction=instruction, extra=extra):
                self.task = self.rt.create_task("Original", acceptance=["Original item"])
                event = self.rt.log.emit(type="human.commanded", actor=Actor.human(), task_id=self.task,
                    payload={"instruction": instruction, "goal": "Historical goal", "acceptance": ["一🐾", "Second"], **extra})
                self.rt.team.enable(self.task)
                index = self.call({"scope": "customer_index", "limit": 1})
                self.assertEqual(index["objective_source"]["event_seq"], event.seq)
                self.assertEqual(index["acceptance_source"]["event_seq"], event.seq)
                entries, page = [], index
                while True:
                    entries.extend(page["entries"])
                    if not page["has_more"]:
                        break
                    page = self.call({"scope": "customer_index", "as_of_seq": index["as_of_seq"],
                        "as_of_hash": index["as_of_hash"], "before_seq": page["next_before_seq"],
                        "before_part": page["next_before_part"], "limit": 1})
                types = [row["source_type"] for row in entries if row["seq"] == event.seq]
                self.assertIn("legacy_command_goal", types)
                self.assertEqual(types.count("legacy_command_acceptance"), 2)
                self.assertEqual("legacy_command_instruction" in types, isinstance(instruction, str))
                for role in (self.manager, self.worker):
                    for options, expected in (({}, "Historical goal"), ({"acceptance_index": 0}, "一🐾"),
                                              ({"acceptance_index": 1}, "Second")):
                        self.assertEqual(self.call({"scope": "customer_message", "message_seq": event.seq,
                                                    "expected_hash": event.hash, **options}, role)["text"], expected)
                    args = {"scope": "customer_message", "message_seq": event.seq,
                            "expected_hash": event.hash, "source_field": "instruction"}
                    if isinstance(instruction, str):
                        self.assertEqual(self.call(args, role)["text"], instruction)
                    else:
                        with self.assertRaises(ValueError):
                            self.call(args, role)

    def test_mixed_legacy_field_types_remain_unknown(self):
        for changes in ({"acceptance": ["Valid", 7]}, {"mentions": ["retired-role", 7]},
                        {"goal": ["Not text"]}, {"mentioned_workers": ["old-worker", None], "next_action": "run"}):
            with self.subTest(changes=changes):
                self.task = self.rt.create_task("Original", acceptance=["Original item"])
                event = self.rt.log.emit(type="human.commanded", actor=Actor.human(), task_id=self.task,
                    payload={"instruction": "", "goal": "Historical goal", "acceptance": ["Valid"], **changes})
                self.rt.team.enable(self.task)
                index = self.call({"scope": "customer_index"})
                self.assertEqual(index["objective_source"]["status"], "unknown")
                self.assertEqual(index["acceptance_source"]["status"], "unknown")
                self.assertNotIn(event.seq, [row["seq"] for row in index["entries"]])
                for options in ({}, {"acceptance_index": 0}, {"source_field": "instruction"}):
                    with self.assertRaises(ValueError):
                        self.call({"scope": "customer_message", "message_seq": event.seq,
                                   "expected_hash": event.hash, **options})

    def test_persisted_impossible_producer_combinations_are_unknown(self):
        cases = [
            {"instruction": None, "next_action": "run"},
            {"instruction": 42, "mentions": ["retired-role"]},
            {"instruction": " ", "mentions": ["retired-role"]},
            {"instruction": "", "next_action": "battle", "requested_strategy": "cabinet_review"},
            {"instruction": "text", "next_action": "run", "requested_strategy": "cabinet_review"},
            {"instruction": "text", "next_action": "battle"},
            {"instruction": "text", "next_action": "run", "roster_mode": "append"},
            {"instruction": "text", "next_action": "run", "mentions": ["old"], "mentioned_workers": ["old"]},
            {"instruction": "text", "next_action": "run", "mentions": ["old"],
             "mentioned_workers": ["other"], "roster_mode": "append"},
            {"instruction": "text", "next_action": "run", "mentions": ["first", "second"],
             "mentioned_workers": ["second", "first"], "roster_mode": "append"},
        ]
        for number, fields in enumerate(cases):
            with self.subTest(fields=fields):
                self.task = self.rt.create_task("Original", acceptance=["Original item"])
                event = self.rt.log.emit(type="human.commanded", actor=Actor.human(), task_id=self.task,
                    payload={"goal": "PRIVATE_GOAL", "acceptance": ["PRIVATE_ITEM"], **fields})
                self.rt.team.enable(self.task)
                message = self.rt.team.receive(self.task, "Continue", request_id=f"impossible-old-{number}")
                context = self.context(message, self.manager)
                index = context["customer_input_sources"]
                self.assertEqual(index["objective_source"], {"status": "unknown"})
                self.assertEqual(index["acceptance_source"], {"status": "unknown"})
                self.assertNotIn("PRIVATE", json.dumps(index))
                self.assertIn("未验证", context["objective_source_note"])
                self.assertNotIn(event.seq, [row["seq"] for row in index["entries"]])
                for role in (self.manager, self.worker):
                    for options in ({}, {"acceptance_index": 0}, {"source_field": "instruction"}):
                        with self.assertRaises(ValueError):
                            self.call({"scope": "customer_message", "message_seq": event.seq,
                                       "expected_hash": event.hash, **options}, role)
                if number == 0:
                    args = {"scope": "customer_message", "message_seq": event.seq,
                            "expected_hash": event.hash, "acceptance_index": 0}
                    self.driver.steps[self.manager] = [fixture.turn("", ("read_messages", args)), fixture.turn("Done")]
                    self.rt.collaborate(self.task)
                    receipt = next(e for e in self.rt.log.iter_events(task_id=self.task, event_types=("team.tool_finished",))
                                   if e.payload.get("name") == "read_messages")
                    self.assertEqual(receipt.payload["result"]["status"], "error")
                self.assertTrue(self.rt.verify_chain().ok)

    def test_unvalidated_objective_projection_is_explicit_in_model_context(self):
        self.task = self.rt.create_task("Original")
        self.rt.log.emit(type="human.commanded", actor=Actor.human(), task_id=self.task,
            payload={"instruction": "Derived", "intent": "direction", "next_action": "run",
                     "goal": "Unverified projection", "clarification": {"answer": "x"}})
        self.rt.team.enable(self.task)
        message = self.rt.team.receive(self.task, "Continue", request_id="unknown-projection-note")
        context = self.context(message, self.manager)
        self.assertEqual(context["current_work"]["objective"], "Unverified projection")
        self.assertEqual(context["customer_input_sources"]["objective_source"]["status"], "unknown")
        self.assertIn("未验证", context["objective_source_note"])
        self.assertIn("current_work.objective", context["objective_source_note"])

    def test_oldest_empty_instruction_and_whitespace_goal_follow_historical_fold(self):
        self.task = self.rt.create_task("Original", acceptance=["Old"])
        event = self.rt.log.emit(type="human.commanded", actor=Actor.human(), task_id=self.task,
                                 payload={"instruction": "", "goal": "  ", "acceptance": []})
        self.rt.team.enable(self.task)
        index = self.call({"scope": "customer_index"})
        self.assertEqual(self.rt.team.view(self.task)["objective"], "  ")
        self.assertEqual(index["objective_source"]["event_seq"], event.seq)
        self.assertEqual(index["acceptance_source"]["count"], 0)
        self.assertEqual(self.call({"scope": "customer_message", "message_seq": event.seq,
                                    "expected_hash": event.hash})["text"], "  ")
        self.assertEqual(self.call({"scope": "customer_message", "message_seq": event.seq,
                                    "expected_hash": event.hash, "source_field": "instruction"})["text"], "")

    def test_old_shape_excludes_derived_unknown_and_invalid_metadata(self):
        for number, extra in enumerate(({"clarification": {}}, {"conversation_seq": 1}, {"future_private": True},
                                        {"next_action": None}, {"intent": None}, {"next_action": "unknown"})):
            with self.subTest(extra=extra):
                self.task = self.rt.create_task("Original", acceptance=["Original item"])
                event = self.rt.log.emit(type="human.commanded", actor=Actor.human(), task_id=self.task,
                    payload={"instruction": "old", "goal": "UNTRUSTED", "acceptance": ["UNTRUSTED"], **extra})
                self.rt.team.enable(self.task)
                index = self.call({"scope": "customer_index"})
                self.assertEqual(index["objective_source"], {"status": "unknown"})
                self.assertEqual(index["acceptance_source"], {"status": "unknown"})
                self.assertNotIn(event.seq, [row["seq"] for row in index["entries"]])
                for options in ({}, {"acceptance_index": 0}, {"source_field": "instruction"}):
                    with self.assertRaises(ValueError):
                        self.call({"scope": "customer_message", "message_seq": event.seq,
                                   "expected_hash": event.hash, **options})

    def test_legacy_command_clearing_acceptance_is_not_reported_as_initial(self):
        self.task = self.rt.create_task("Original goal", acceptance=["Original criterion"])
        command = self.rt.command(self.task, "Clear acceptance", acceptance=[])
        self.rt.team.enable(self.task)
        source = self.rt.team.receive(self.task, "Continue", request_id="source-legacy-acceptance-clear")
        index = self.context(source, self.manager)["customer_input_sources"]
        self.assertEqual(self.rt.task(self.task).acceptance, [])
        self.assertEqual(index["acceptance_source"], {"status": "legacy_command_acceptance", "event_seq": command.seq,
                                                       "event_hash": command.hash, "count": 0, "readable": True})

    def test_legacy_and_untrusted_command_follow_fold_and_team_view_order(self):
        self.task = self.rt.create_task("Original goal", acceptance=["Original criterion"])
        command = self.rt.command(self.task, "Switch", goal="Legacy goal", acceptance=["Trusted criterion"])
        self.rt.team.enable(self.task)
        source = self.rt.team.receive(self.task, "Proceed", request_id="source-legacy-order")
        self.driver.steps[self.manager] = [fixture.turn("", ("set_objective", {"goal": "Team goal"})), fixture.turn("Updated")]
        self.rt.collaborate(self.task)
        latest = self.rt.command(self.task, "Another scope", goal="Later legacy goal", acceptance=["Later criterion"])
        self.assertEqual(self.rt.task(self.task).goal, "Later legacy goal")
        self.assertEqual(self.rt.team.view(self.task)["objective"], "Later legacy goal")
        index = source_index(self.rt.log.read(task_id=self.task))
        self.assertEqual(index["objective_source"]["event_seq"], latest.seq)
        self.assertEqual(index["acceptance_source"]["event_seq"], latest.seq)
        self.assertEqual(index["acceptance_source"]["status"], "legacy_command_acceptance")
        # A fake command influences fold, but must never become a trusted source.
        self.task = self.rt.create_task("Another goal", acceptance=["Valid criterion"])
        self.rt.log.emit(type="human.commanded", actor=Actor.agent("human", "human"), task_id=self.task,
                         payload={"instruction": "fake", "goal": "Fake goal", "acceptance": ["Fake criterion"]})
        self.rt.team.enable(self.task)
        fake_source = self.rt.team.receive(self.task, "Proceed", request_id="source-fake-legacy")
        self.assertEqual(self.rt.team.view(self.task)["objective"], "Another goal")
        fake_index = self.context(fake_source, self.manager)["customer_input_sources"]
        self.assertEqual(fake_index["objective_source"], {"status": "unknown"})
        self.assertEqual(fake_index["acceptance_source"], {"status": "unknown"})
        self.driver.steps[self.manager] = [fixture.turn("", ("set_objective", {"goal": "Fake goal"}),
            ("set_objective", {"goal": "Final goal"})), fixture.turn("Updated")]
        self.rt.collaborate(self.task)
        self.assertEqual(source_index(self.rt.log.read(task_id=self.task))["objective_source"]["status"], "unknown")

    def test_malformed_human_command_does_not_certify_goal_or_acceptance(self):
        self.task = self.rt.create_task("Original goal", acceptance=["Original criterion"])
        command = self.rt.log.emit(type="human.commanded", actor=Actor.human(), task_id=self.task,
                                   payload={"instruction": "bad", "goal": ["non-text"], "acceptance": ""})
        self.rt.team.enable(self.task)
        source = self.rt.team.receive(self.task, "Proceed", request_id="source-malformed-legacy")
        self.assertEqual(self.rt.team.view(self.task)["objective"], "['non-text']")
        index = self.context(source, self.manager)["customer_input_sources"]
        self.assertEqual(index["objective_source"], {"status": "unknown"})
        self.assertEqual(index["acceptance_source"], {"status": "unknown"})
        self.assertNotIn(command.seq, [row["seq"] for row in index["entries"]])

    def test_unchanged_and_rejected_attempts_after_an_earlier_change(self):
        source = self.rt.team.receive(self.task, "Change direction", request_id="source-mixed-attempts")
        self.driver.steps[self.manager] = [fixture.turn("", ("set_objective", {"goal": "First goal"}),
            ("set_objective", {"goal": "First goal"}),
            ("set_objective", {"goal": "New goal", "unexpected": "x"}),
            ("set_objective", {"goal": "New goal"})), fixture.turn("Updated")]
        self.rt.collaborate(self.task)
        events = self.rt.log.read(task_id=self.task)
        assert_noop_attempt_chain(self, events, source)
        assert_invalid_attempt_chain(self, events, source)

    def test_large_chain_keeps_noop_order_and_rejects_a_corrupted_middle_receipt(self):
        events = []
        def add(kind, actor, payload):
            event = SimpleNamespace(seq=len(events) + 1, type=kind, actor=actor, payload=payload, hash="a" * 64)
            events.append(event)
            return event
        add("task.created", Actor.human(), {"goal": "initial", "acceptance": []})
        source = add("team.message_received", Actor.human(), {"kind": "message", "text": "new", "recipient": "manager"})
        add("team.run_started", HOST, {"run_id": "r", "role": "manager", "message_seq": source.seq, "revision": 0})
        for _ in range(1000):
            add("team.notice", HOST, {})
        contract = {"version": 1, "kind": "atomic_local_events", "receipt": "same_transaction"}
        noop_receipts = []
        for _ in range(128):
            start = add("team.tool_started", HOST, {"name": "set_objective", "run_id": "r", "role": "manager",
                "message_seq": source.seq, "revision": 0, "call_id": f"call-{len(events)}", "execution_contract": contract, "arguments": {"goal": "initial"}})
            noop_receipts.append(add("team.tool_finished", HOST, {"name": "set_objective", "run_id": "r", "role": "manager",
                "started_seq": start.seq, "result": {"status": "ok", "goal": "initial", "revision": 0,
                                                      "work_status": "running", "scope_changed": False}}))
        start = add("team.tool_started", HOST, {"name": "set_objective", "run_id": "r", "role": "manager",
            "message_seq": source.seq, "revision": 0, "call_id": "last-call", "execution_contract": contract, "arguments": {"goal": "new"}})
        goal = add("team.objective_updated", HOST, {"goal": "new", "run_id": "r"})
        add("team.tool_finished", HOST, {"name": "set_objective", "run_id": "r", "role": "manager", "started_seq": start.seq,
            "result": {"status": "ok", "goal": "new", "revision": goal.seq, "work_status": "running", "scope_changed": True}})
        self.assertEqual(source_index(events)["objective_source"]["message_seq"], source.seq)
        altered = deepcopy(events)
        altered[noop_receipts[64].seq - 1].payload["result"]["revision"] = 7
        self.assertEqual(source_index(altered)["objective_source"]["status"], "unknown")

    def test_trusted_identity_task_and_anchor_validation(self):
        own = self.rt.team.receive(self.task, "Trusted input", request_id="source-trusted-identity")
        fake = self.rt.log.emit(type="team.message_received", task_id=self.task,
             actor=Actor.agent("human", "human"), payload={"kind": "message", "text": "Not customer"})
        malformed = self.rt.log.emit(type="team.message_received", task_id=self.task,
             actor=Actor.human(), payload={"kind": "notification", "text": "Not input"})
        other_task = self.rt.create_task("Other task")
        other = self.rt.log.emit(type="team.message_received", task_id=other_task, actor=Actor.human(),
                                 payload={"kind": "message", "text": "Other task private"})
        with self.assertRaises(ValueError):
            raw_source_index([*self.rt.log.read(task_id=self.task), other], task_id=self.task,
                             member_ids={self.manager, self.worker})
        page = self.call({"scope": "customer_index"})
        created = next(e for e in self.rt.log.read(task_id=self.task) if e.type == "task.created")
        self.assertEqual([entry["seq"] for entry in page["entries"]], [own.seq, created.seq])
        for source in (fake, malformed, other):
            with self.subTest(seq=source.seq), self.assertRaises(ValueError):
                self.call({"scope": "customer_message", "message_seq": source.seq, "expected_hash": source.hash})
        with self.assertRaises(ValueError):
            self.call({"scope": "customer_message", "message_seq": own.seq, "expected_hash": "wrong"})
        for args in ({"scope": "customer_index", "as_of_seq": other.seq},
                     {"scope": "customer_index", "as_of_seq": page["as_of_seq"], "as_of_hash": "wrong"},
                     {"scope": "customer_index", "before_seq": True},
                     {"scope": "customer_message", "message_seq": own.seq, "expected_hash": own.hash, "offset": True},
                     {"scope": "customer_message", "message_seq": own.seq, "expected_hash": own.hash, "length": 4001},
                     {"scope": "customer_message", "message_seq": own.seq, "expected_hash": own.hash, "after_seq": 0}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.call(args)

    def test_real_tool_dispatch_and_legacy_reader(self):
        source = self.rt.team.receive(self.task, "Recorded customer input", request_id="source-tool-dispatch")
        self.driver.steps[self.manager] = [
            fixture.turn("", ("read_messages", {"scope": "customer_message", "message_seq": source.seq,
                                       "expected_hash": source.hash, "length": 7})),
            fixture.turn("", ("read_messages", {"scope": "conversation", "after_seq": source.seq - 1, "limit": 1})),
            fixture.turn("Done"),
        ]
        self.rt.collaborate(self.task)
        receipts = [e.payload["result"] for e in self.rt.log.iter_events(task_id=self.task,
            event_types=("team.tool_finished",)) if e.payload.get("name") == "read_messages"]
        self.assertEqual(receipts[0]["text"], source.payload["text"][:7])
        self.assertEqual(receipts[0]["next_offset"], 7)
        self.assertEqual(receipts[1]["messages"][0]["text"], source.payload["text"])
        self.assertTrue(self.rt.verify_chain().ok)
        for role in (self.manager, self.worker):
            schema = next(t for t in self.rt.team._schemas(self.rt.team._role(role), None) if t["name"] == "read_messages")
            self.assertIn("customer_message", schema["input_schema"]["properties"]["scope"]["enum"])
            self.assertIn("可能完整也可能为节选", schema["description"])
            self.assertIn("preview_truncated", schema["description"])
        self.assertIn("read_messages", PAUSED_TOOLS)

class ExternalGoalSourceTests(unittest.TestCase):
    def setUp(self):
        external_fixture.TeamExternalTests.setUp(self)

    def test_external_historical_direct_shapes_support_noop_then_change(self):
        for number, extra in enumerate(({}, {"next_action": "run"})):
            with self.subTest(extra=extra):
                self.task = self.rt.create_task("Original", acceptance=["Original item"])
                command = self.rt.log.emit(type="human.commanded", actor=Actor.human(), task_id=self.task,
                    payload={"instruction": "Historical", "goal": "Historical goal", "acceptance": ["Item"], **extra})
                self.rt.team.enable(self.task)
                source = self.rt.team.receive(self.task, "Change direction", request_id=f"external-old-{number}")
                def script(run):
                    index = run.call("read_messages", scope="customer_index")
                    self.assertEqual(index["objective_source"]["event_seq"], command.seq)
                    item = run.call("read_messages", scope="customer_message", message_seq=command.seq,
                                    expected_hash=command.hash, acceptance_index=0)
                    self.assertEqual(item["text"], "Item")
                    self.assertIs(run.call("set_objective", goal="Historical goal")["scope_changed"], False)
                    self.assertIs(run.call("set_objective", goal="New goal")["scope_changed"], True)
                    return "Updated"
                self.driver.scripts[self.manager] = [script]
                self.rt.collaborate(self.task)
                self.assertEqual(self.driver.errors, [])
                assert_noop_attempt_chain(self, self.rt.log.read(task_id=self.task), source)
                self.assertTrue(self.rt.verify_chain().ok)

    def test_two_external_goal_changes_use_the_same_host_validated_chain(self):
        source = self.rt.team.receive(self.task, "Change the direction", request_id="source-external-two-goals")
        def script(run):
            self.assertEqual(run.call("set_objective", goal="First objective")["status"], "ok")
            self.assertEqual(run.call("set_objective", goal="Second objective")["status"], "ok")
            return "Updated"
        self.driver.scripts[self.manager] = [script]
        self.rt.collaborate(self.task)
        self.assertEqual(self.driver.errors, [])
        events = self.rt.log.read(task_id=self.task)
        goals = [e for e in events if e.type == "team.objective_updated"]
        starts = [e for e in events if e.type == "team.tool_started" and e.payload.get("name") == "set_objective"]
        self.assertEqual(len(goals), 2)
        self.assertEqual(starts[1].payload["revision"], goals[0].seq)
        self.assertEqual(source_index(events)["objective_source"]["message_seq"], source.seq)
        ext_start = next(e for e in events if e.type == "team.external_run_started")
        receipt = next(e for e in events if e.type == "team.tool_finished" and e.payload.get("started_seq") == starts[0].seq)
        variants = [
            [e for e in events if e.seq != receipt.seq],
            [replace(e, actor=Actor.agent("team", "team")) if e.seq == ext_start.seq else e for e in events],
            [replace(e, payload={**e.payload, "run_started_hash": "wrong"}) if e.seq == ext_start.seq else e for e in events],
            [replace(e, payload={**e.payload, "external_started_seq": True}) if e.seq == starts[1].seq else e for e in events],
        ]
        for altered in variants:
            self.assertEqual(source_index(altered)["objective_source"]["status"], "unknown")
        self.assertTrue(self.rt.verify_chain().ok)

    def test_external_invalid_goal_attempt_then_success_has_proven_source(self):
        source = self.rt.team.receive(self.task, "Change the direction", request_id="source-external-invalid-goal")
        def script(run):
            self.assertEqual(run.call("set_objective", goal="First objective")["status"], "ok")
            bad = run.call("set_objective", goal="Second objective", unexpected="x")
            self.assertEqual((bad["status"], bad["outcome"]), ("error", "not_started"))
            self.assertEqual(run.call("set_objective", goal="Second objective")["status"], "ok")
            return "Updated"
        self.driver.scripts[self.manager] = [script]
        self.rt.collaborate(self.task)
        self.assertEqual(self.driver.errors, [])
        events = self.rt.log.read(task_id=self.task)
        self.assertEqual(len([e for e in events if e.type == "team.objective_updated"]), 2)
        assert_invalid_attempt_chain(self, events, source)
        self.assertTrue(self.rt.verify_chain().ok)

    def test_external_invalid_first_goal_then_success_has_proven_source(self):
        source = self.rt.team.receive(self.task, "Change the direction", request_id="source-external-first-invalid")
        def script(run):
            bad = run.call("set_objective", goal="New goal", unexpected="x")
            self.assertEqual((bad["status"], bad["outcome"]), ("error", "not_started"))
            self.assertEqual(run.call("set_objective", goal="New goal")["status"], "ok")
            return "Updated"
        self.driver.scripts[self.manager] = [script]
        self.rt.collaborate(self.task)
        self.assertEqual(self.driver.errors, [])
        events = self.rt.log.read(task_id=self.task)
        self.assertEqual(len([e for e in events if e.type == "team.objective_updated"]), 1)
        assert_invalid_attempt_chain(self, events, source)

    def test_external_unchanged_then_changed_goal_keeps_source(self):
        source = self.rt.team.receive(self.task, "Change direction", request_id="source-external-unchanged")
        goal = self.rt.team.view(self.task)["objective"]
        def script(run):
            self.assertIs(run.call("set_objective", goal=goal)["scope_changed"], False)
            self.assertIs(run.call("set_objective", goal="New goal")["scope_changed"], True)
            return "Updated"
        self.driver.scripts[self.manager] = [script]
        self.rt.collaborate(self.task)
        self.assertEqual(self.driver.errors, [])
        assert_noop_attempt_chain(self, self.rt.log.read(task_id=self.task), source)

    def test_external_legacy_goal_unchanged_then_changed_keeps_source(self):
        self.task = self.rt.create_task("Original goal", acceptance=["Original criterion"])
        command = self.rt.command(self.task, "Use revised scope", goal="Legacy goal", acceptance=["Revised criterion"])
        self.rt.team.enable(self.task)
        source = self.rt.team.receive(self.task, "Proceed", request_id="source-legacy-external")
        initial = source_index(self.rt.log.read(task_id=self.task))
        self.assertEqual(initial["objective_source"]["event_seq"], command.seq)
        self.assertEqual(initial["objective_source"]["status"], "legacy_command_goal")
        self.assertEqual(initial["acceptance_source"]["status"], "legacy_command_acceptance")
        def script(run):
            index = run.call("read_messages", scope="customer_index")
            self.assertEqual(index["objective_source"]["event_seq"], command.seq)
            self.assertIn(command.seq, [row["seq"] for row in index["entries"]])
            item = run.call("read_messages", scope="customer_message", message_seq=command.seq,
                            expected_hash=command.hash, acceptance_index=0)
            self.assertEqual((item["status"], item["text"]), ("ok", "Revised criterion"))
            instruction = run.call("read_messages", scope="customer_message", message_seq=command.seq,
                                   expected_hash=command.hash, source_field="instruction")
            self.assertEqual((instruction["status"], instruction["text"]), ("ok", "Use revised scope"))
            self.assertIs(run.call("set_objective", goal="Legacy goal")["scope_changed"], False)
            self.assertIs(run.call("set_objective", goal="New goal")["scope_changed"], True)
            return "Updated"
        self.driver.scripts[self.manager] = [script]
        self.rt.collaborate(self.task)
        self.assertEqual(self.driver.errors, [])
        assert_noop_attempt_chain(self, self.rt.log.read(task_id=self.task), source)

    def test_external_catalog_describes_and_invokes_customer_source_readers(self):
        self.task = self.rt.create_task("Catalog goal", acceptance=["Acceptance 🐾 item", "Second item"])
        command = self.rt.command(self.task, "Catalog old instruction")
        self.rt.team.enable(self.task)
        created = next(self.rt.log.iter_events(task_id=self.task, event_types=("task.created",)))
        customer = self.rt.team.receive(self.task, "Customer direction", request_id="source-catalog-customer")
        foreign_task = self.rt.create_task("Foreign goal", acceptance=["Foreign item"])
        foreign = next(self.rt.log.iter_events(task_id=foreign_task, event_types=("task.created",)))
        catalog_driver = QoderAcpDriver("offline-catalog", "never-launched", "offline", timeout=180,
                                        max_run_seconds=600, tool_surface="catalog")
        self.addCleanup(catalog_driver.close)

        def script(host):
            adapter = catalog_driver.agent_run(cwd=self.root, tools=host.tools, execute=host.execute,
                                               check_current=host.check_current, on_update=None)
            self.assertEqual([tool["name"] for tool in adapter._wire_tools], ["describe_tools", "invoke_tool"])
            listing = adapter._catalog_call(ToolCall("list", "describe_tools", {}))
            self.assertIn("read_messages", [item["name"] for item in listing["tools"]])
            definition = adapter._catalog_call(ToolCall("describe", "describe_tools", {"names": ["read_messages"]}))
            scopes = definition["definitions"][0]["input_schema"]["properties"]["scope"]["enum"]
            self.assertIn("customer_index", scopes)
            self.assertIn("customer_message", scopes)
            self.assertEqual(definition["definitions"][0]["input_schema"]["properties"]["source_field"]["enum"], ["instruction"])

            def invoke(identifier, arguments):
                return adapter._catalog_call(ToolCall(identifier, "invoke_tool", {"name": "read_messages", "arguments": arguments}))

            first = invoke("index", {"scope": "customer_index", "limit": 1})
            self.assertEqual(first["status"], "ok")
            self.assertTrue(first["has_more"])
            receipt = list(self.rt.log.iter_events(task_id=self.task, event_types=("team.tool_finished",)))[-1]
            for identifier, arguments in (("missing-hash", {"message_seq": receipt.seq}),
                                          ("receipt", {"message_seq": receipt.seq, "expected_hash": receipt.hash}),
                                          ("wrong-hash", {"message_seq": customer.seq, "expected_hash": "PRIVATE_HASH"})):
                rejected = invoke(identifier, {"scope": "customer_message", **arguments})
                self.assertEqual(rejected["status"], "error")
                self.assertIn("customer_index", rejected["message"])
                self.assertIn("read_receipts", rejected["message"])
                self.assertNotIn("PRIVATE_HASH", json.dumps(rejected))
            page = invoke("next", {"scope": "customer_index", "as_of_seq": first["as_of_seq"],
                "as_of_hash": first["as_of_hash"], "before_seq": first["next_before_seq"],
                "before_part": first["next_before_part"], "limit": 1})
            self.assertEqual((page["entries"][0]["seq"], page["entries"][0]["source_field"]), (command.seq, "instruction"))
            old_instruction = invoke("instruction", {"scope": "customer_message", "message_seq": command.seq,
                "expected_hash": command.hash, "source_field": "instruction"})
            self.assertEqual(old_instruction["text"], "Catalog old instruction")
            page = invoke("next-created", {"scope": "customer_index", "as_of_seq": first["as_of_seq"],
                "as_of_hash": first["as_of_hash"], "before_seq": page["next_before_seq"],
                "before_part": page["next_before_part"], "limit": 1})
            self.assertEqual((page["entries"][0]["seq"], page["entries"][0]["acceptance_index"]), (created.seq, 1))
            result = invoke("read", {"scope": "customer_message", "message_seq": created.seq,
                "expected_hash": created.hash, "acceptance_index": 0, "offset": 0, "length": 5})
            self.assertEqual(result["text"], "Accep")
            self.assertTrue(result["has_more"])
            result = invoke("read-next", {"scope": "customer_message", "message_seq": created.seq,
                "expected_hash": created.hash, "acceptance_index": 0, "offset": result["next_offset"]})
            self.assertEqual("Accep" + result["text"], "Acceptance 🐾 item")
            message = invoke("message", {"scope": "customer_message", "message_seq": customer.seq,
                "expected_hash": customer.hash})
            self.assertEqual(message["text"], customer.payload["text"])
            denied = invoke("foreign", {"scope": "customer_message", "message_seq": foreign.seq,
                "expected_hash": foreign.hash, "acceptance_index": 0})
            self.assertEqual(denied["status"], "error")
            return "Finished offline catalog check"

        self.driver.scripts[self.manager] = [script]
        self.rt.collaborate(self.task)
        self.assertEqual(self.driver.errors, [])
        receipts = [e for e in self.rt.log.iter_events(task_id=self.task, event_types=("team.tool_finished",))
                    if e.payload.get("name") == "read_messages"]
        self.assertEqual(len(receipts), 11)
        self.assertTrue(self.rt.verify_chain().ok)


class ExternalWireCatalogTests(unittest.TestCase):
    def setUp(self):
        external_fixture.TeamExternalTests.setUp(self)

    def test_offline_acp_catalog_describes_and_reads_acceptance_sources(self):
        self.task = self.rt.create_task("Catalog goal", acceptance=["验收🐾完整项"])
        self.rt.team.enable(self.task)
        created = next(self.rt.log.iter_events(task_id=self.task, event_types=("task.created",)))
        self.rt.team.receive(self.task, "Read customer sources", request_id="source-wire-catalog")
        driver = QoderAcpDriver("external_fixture", "never-launched", "ExactModel", timeout=30,
                                max_run_seconds=120, tool_surface="catalog")
        self.rt.drivers["external_fixture"] = driver
        root = self.root
        peer = Path(__file__).parent / "fixtures" / "external_agent_peer.py"

        class OfflineSession(AcpSession):
            def __init__(self, command, **kwargs):
                super().__init__([sys.executable, "-u", str(peer.resolve()), "customer_sources_catalog", str(root)], **kwargs)

        with patch("cater.external_agent.AcpSession", OfflineSession):
            self.rt.collaborate(self.task)
        receipts = [e.payload["result"] for e in self.rt.log.iter_events(task_id=self.task,
                    event_types=("team.tool_finished",)) if e.payload.get("name") == "read_messages"]
        self.assertEqual(len(receipts), 5)
        self.assertEqual([item["status"] for item in receipts], ["ok", "error", "error", "ok", "ok"])
        self.assertEqual((receipts[3]["entries"][0]["seq"], receipts[3]["entries"][0]["acceptance_index"]), (created.seq, 0))
        self.assertEqual(receipts[4]["text"], "验收🐾")
        external = [e for e in self.rt.log.iter_events(task_id=self.task, event_types=("team.external_run_finished",))]
        self.assertEqual(len(external), 1)
        self.assertTrue(external[0].payload["cleanup_complete"])
        self.assertTrue(self.rt.verify_chain().ok)


if __name__ == "__main__":
    unittest.main()
