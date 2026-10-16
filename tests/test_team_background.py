"""Frozen background contracts through native model/tool exchanges."""

from __future__ import annotations

import copy
import hashlib
import json
import unittest
from unittest.mock import patch

from cater.artifacts import normalize_deliverables
from cater.conversations import dialogue_background, read_background
from cater.events import Actor
from cater.team import HOST
import test_simple_team

turn = test_simple_team.turn


class TeamBackgroundTests(unittest.TestCase):
    def setUp(self):
        test_simple_team.SimpleTeamTests.setUp(self)

    def publish(self, task, text, filename="guide.txt"):
        payload, = normalize_deliverables([{
            "filename": filename, "title": filename, "summary": "Frozen reference",
            "content": text, "format": "text",
        }])
        return self.rt.log.emit(type="deliverable.published", task_id=task,
                                actor=Actor.agent(self.worker, "worker"), payload=payload)

    def derive(self, source=None, request_id="background-child-001"):
        child = self.rt.derive_task(source or self.task, "Write a new usage guide", request_id=request_id)["task_id"]
        self.rt.team.enable(child)
        return child

    def results(self, task):
        return [e.payload["result"] for e in self.rt.log.read(task_id=task)
                if e.type == "team.tool_finished" and e.payload.get("name") == "read_background"]

    def test_native_model_and_delegate_receive_index_and_can_read_frozen_body(self):
        content = "FROZEN_BODY_ONLY_AFTER_TOOL\n" * 80
        artifact = self.publish(self.task, content)
        customer = self.rt.team.receive(self.task, "SOURCE_CUSTOMER_DETAIL: keep the original API")
        self.rt.log.emit(type="team.objective_updated", task_id=self.task, actor=HOST,
                         payload={"goal": "CURRENT_SOURCE_GOAL"})
        child = self.derive()
        saved = copy.deepcopy(self.rt.conversation_background(child))
        parent_head = self.rt.log.read(task_id=self.task)[-1].hash
        self.rt.team.receive(self.task, "LATER_PARENT_DETAIL must never enter the child")
        self.publish(self.task, "LATER_PARENT_BODY")
        self.rt.log.emit(type="team.objective_updated", task_id=self.task, actor=HOST,
                         payload={"goal": "LATER_PARENT_GOAL"})
        self.assertNotEqual(parent_head, self.rt.log.read(task_id=self.task)[-1].hash)
        source_id = f"{customer.seq}:text:0"
        self.driver.steps = {
            self.manager: [turn("", ("read_background", {"section": "goal"}),
                                ("read_background", {"section": "source", "source_id": source_id}),
                                ("read_background", {"section": "artifact", "artifact_seq": artifact.seq})),
                           turn("", ("delegate", {"recipient": self.worker, "instruction": "Read the frozen guide and summarize its use."}),
                                ("wait_for_reply", {})), turn("Summary received.")],
            self.worker: [turn("", ("read_background", {"section": "artifact", "artifact_seq": artifact.seq})),
                          turn("Frozen guide read; no execution claimed.")],
        }
        self.rt.team.receive(child, "Continue using the captured background")
        self.rt.collaborate(child)
        calls = self.driver.calls
        self.assertEqual({call["context"]["you"]["id"] for call in calls}, {self.manager, self.worker})
        for call in calls:
            context = call["context"]
            self.assertEqual(context["background"]["source_goal_excerpt"], "CURRENT_SOURCE_GOAL")
            self.assertEqual(context["current_work"]["objective"], "Write a new usage guide")
            self.assertIn("read_background", {tool["name"] for tool in call["tools"]})
            self.assertNotIn("explain_task", context["background"]["notice"])
            self.assertNotIn("FROZEN_BODY_ONLY_AFTER_TOOL", json.dumps(context))
            self.assertNotIn("LATER_PARENT", json.dumps(context))
        results = self.results(child)
        self.assertEqual(results[0]["content"], "CURRENT_SOURCE_GOAL")
        self.assertIn("SOURCE_CUSTOMER_DETAIL", results[1]["content"])
        self.assertEqual([r["content"] for r in results[2:]], [content, content])
        self.assertEqual(results[2]["source"]["content_sha256"], hashlib.sha256(content.encode()).hexdigest())
        self.assertEqual(results[2]["source"]["verification"], "unverified")
        self.assertTrue(all(r["historical_only"] for r in results))
        self.assertEqual(self.rt.conversation_background(child), saved)
        self.assertFalse([r for r in self.rt.team.view(child)["runs"] if r["status"] == "failed"])
        self.assertTrue(self.rt.verify_chain().ok)

    def test_snapshot_index_and_body_are_bounded_and_pageable_without_mutation(self):
        snapshot = {"source_task_id": "parent", "source_seq": 100, "source_hash": "anchor",
                    "source_goal": "g" * 2200, "omitted_artifacts": 7,
                    "sources": [{"id": f"source-{i}", "seq": i, "task_id": "parent", "text": "x" * 18000}
                                for i in range(22)],
                    "artifacts": [{"seq": 100, "task_id": "parent", "filename": "app.py", "summary": "Large body",
                                   "content": "z" * 140000, "verification": "unverified"}]}
        before = copy.deepcopy(snapshot)
        index = dialogue_background(snapshot, reader="read_background")
        self.assertEqual(index["omitted_sources"], 10)
        self.assertTrue(index["source_goal_truncated"])
        self.assertNotIn("z" * 100, json.dumps(index))
        page = read_background(snapshot, {"section": "index", "limit": 10})
        self.assertEqual(len(page["entries"]), 10)
        self.assertEqual(page["next_offset"], 10)
        self.assertEqual(page["omitted_artifacts"], 7)
        last = read_background(snapshot, {"section": "index", "offset": 20, "limit": 10})
        self.assertEqual(len(last["entries"]), 3)
        self.assertFalse(last["has_more"])
        self.assertIsNone(last["next_offset"])
        self.assertFalse(last["entries"][-1]["content_included"])
        self.assertNotIn("z" * 100, json.dumps(last))
        first = read_background(snapshot, {"section": "source", "source_id": "source-0", "limit": 16000})
        second = read_background(snapshot, {"section": "source", "source_id": "source-0", "offset": first["next_offset"]})
        self.assertEqual(first["content"] + second["content"], "x" * 18000)
        self.assertFalse(second["has_more"])
        beyond = read_background(snapshot, {"section": "goal", "offset": 3000})
        self.assertEqual(beyond["content"], "")
        self.assertIsNone(beyond["next_offset"])
        self.assertEqual(snapshot, before)

    def test_only_captured_references_can_be_read_no_live_cross_group_lookup(self):
        saved = self.publish(self.task, "CAPTURED")
        child = self.derive()
        late = self.publish(self.task, "NOT_CAPTURED")
        unrelated = self.rt.create_task("OTHER_PROJECT")
        other = self.publish(unrelated, "UNRELATED_SECRET")
        snapshot = self.rt.conversation_background(child)
        for seq in (late.seq, other.seq):
            with self.subTest(seq=seq):
                result = read_background(snapshot, {"section": "artifact", "artifact_seq": seq})
                self.assertEqual(result["status"], "error")
                self.assertNotIn("SECRET", json.dumps(result))
        self.assertEqual(read_background(snapshot, {"section": "artifact", "artifact_seq": saved.seq})["content"], "CAPTURED")
        self.assertEqual(read_background(snapshot, {"section": "source", "source_id": "absent"})["status"], "error")
        with self.assertRaises(ValueError):
            read_background(snapshot, {"section": "artifact", "artifact_seq": other.seq, "task_id": unrelated})

    def test_invalid_selectors_and_pagination_are_rejected(self):
        snapshot = {"source_goal": "old", "sources": [], "artifacts": []}
        bad = [{}, {"section": "all"}, {"section": "goal", "artifact_seq": 1},
               {"section": "source"}, {"section": "source", "source_id": True},
               {"section": "artifact", "artifact_seq": True}, {"section": "artifact", "artifact_seq": 0},
               {"section": "index", "limit": 51}, {"section": "goal", "limit": 16001},
               {"section": "goal", "offset": -1}, {"section": "goal", "offset": False},
               {"section": "index", "limit": None}, {"section": "goal", "limit": 0}]
        for arguments in bad:
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                read_background(snapshot, arguments)
        self.assertEqual(read_background(None, {"section": "goal"})["status"], "error")

    def test_missing_or_ambiguous_body_never_falls_back_to_live_file(self):
        snapshot = {"artifacts": [{"seq": 7, "summary": "not a body"}],
                    "sources": [{"id": "same", "text": "one"}, {"id": "same", "text": "two"}]}
        self.assertEqual(read_background(snapshot, {"section": "artifact", "artifact_seq": 7})["status"], "error")
        self.assertEqual(read_background(snapshot, {"section": "source", "source_id": "same"})["status"], "error")

    def test_ordinary_chat_does_not_advertise_background_reader(self):
        self.driver.steps[self.manager] = [turn("Hello.")]
        self.rt.team.receive(self.task, "Hello")
        self.rt.collaborate(self.task)
        self.assertIsNone(self.driver.calls[0]["context"]["background"])
        self.assertNotIn("read_background", {t["name"] for t in self.driver.calls[0]["tools"]})

    def test_native_paging_error_explains_limit_and_allows_correction(self):
        child = self.derive()
        self.driver.steps[self.manager] = [
            turn("", ("read_background", {"section": "index", "limit": 100})),
            turn("", ("read_background", {"section": "index", "limit": 50})), turn("Reference found."),
        ]
        self.rt.team.receive(child, "Read the reference index")
        self.rt.collaborate(child)
        failed, corrected = self.results(child)
        self.assertEqual(failed["status"], "error")
        self.assertIn("50", failed["message"])
        self.assertEqual(corrected["status"], "ok")
        tool = next(t for t in self.driver.calls[0]["tools"] if t["name"] == "read_background")
        self.assertIn("1-50", tool["input_schema"]["properties"]["limit"]["description"])
        self.assertIn("50", json.dumps(self.driver.calls[1]["history"], ensure_ascii=False))

    def test_paused_derived_chat_can_read_frozen_background_without_execution_tools(self):
        self.publish(self.task, "REFERENCE")
        child = self.derive()
        self.rt.execution_control.change(child, "pause", request_id="background-paused-001")
        self.driver.steps[self.manager] = [turn("", ("read_background", {"section": "goal"})), turn("The original goal was read.")]
        self.rt.team.receive(child, "What was the original goal?")
        self.rt.collaborate(child)
        self.assertEqual(self.results(child)[0]["content"], "制作 doc 转 PDF 工具")
        self.assertEqual({t["name"] for t in self.driver.calls[0]["tools"]},
                         {"read_artifact", "read_receipts", "read_messages", "read_background", "read_checkpoint", "read_delivery", "list_skills", "read_skill", "wait_for_reply"})
        self.assertEqual(self.rt.execution_control.view(child)["state"], "paused")

    def test_nested_derivation_keeps_unique_frozen_sources_not_live_parent_updates(self):
        original = self.rt.team.receive(self.task, "ORIGINAL_DETAIL")
        self.publish(self.task, "ORIGINAL_FILE")
        child = self.derive()
        child_detail = self.rt.team.receive(child, "CHILD_DETAIL")
        self.rt.team.receive(self.task, "LATE_ORIGINAL_DETAIL")
        grandchild = self.derive(child, "background-grandchild-001")
        snapshot = self.rt.conversation_background(grandchild)
        keys = [(s["task_id"], s["id"]) for s in snapshot["sources"]]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertIn((self.task, f"{original.seq}:text:0"), keys)
        self.assertIn((child, f"{child_detail.seq}:text:0"), keys)
        self.assertNotIn("LATE_ORIGINAL_DETAIL", json.dumps(snapshot))
        self.assertNotIn("background", snapshot)
        self.assertEqual(len(snapshot["artifacts"]), 1)

    def test_snapshot_read_uses_no_other_task_event_stream(self):
        child = self.derive()
        snapshot = self.rt.conversation_background(child)
        with patch.object(self.rt.log, "read", side_effect=AssertionError("must not fetch live source")), \
                patch.object(self.rt.log, "iter_events", side_effect=AssertionError("must not fetch live source")):
            self.assertEqual(read_background(snapshot, {"section": "goal"})["status"], "ok")


if __name__ == "__main__":
    unittest.main()
