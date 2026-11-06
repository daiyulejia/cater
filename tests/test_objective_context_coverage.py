"""Offline source, review, recovery and external delivery coverage."""

import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from cater.acp import AcpSession
from cater.external_agent import QoderAcpDriver
from cater.team_context import model_context
from cater.tool_protocol import ToolCall, ToolTurn
from test_objective_context_dedup import GOAL
import test_simple_team as simple
import test_team_external as external
import test_team_review_cycle as review


class ObjectiveContextCoverageTests(unittest.TestCase):
    def fixture(self, kind):
        fixture = kind()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def assert_reference(self, context, expected):
        self.assertIsNone(context["work_checkpoint"]["objective"])
        self.assertEqual(context["work_checkpoint"]["objective_context_ref"], "current_work.objective")
        self.assertEqual(context["current_work"]["objective"], expected)
        self.assertEqual(context["work_checkpoint"]["scope"]["revision"], context["current_work"]["revision"])

    def test_nonempty_customer_sources_and_independent_acceptance_read_through(self):
        f = self.fixture(simple.SimpleTeamTests)
        acceptance = ["第一项：完整原文 🐾 e\u0301，不能丢尾部。", "第二项：单独验收，不合并。"]
        task = f.rt.create_task(GOAL, acceptance=acceptance)
        f.rt.team.enable(task)
        foreign_task = f.rt.create_task("另一任务", acceptance=["不属于本任务的验收项"])
        foreign, = f.rt.log.iter_events(task_id=foreign_task, event_types=("task.created",))
        message = f.rt.team.receive(task, "额外顾客来源 🐾", request_id="dedup-source-readthrough")
        created, = f.rt.log.iter_events(task_id=task, event_types=("task.created",))
        raw, _, _, _, basis = f.rt.team._model_input(task, message, f.rt.organization.leader, run_id="source-fixture")
        before, events = copy.deepcopy(raw), f.rt.log.read(task_id=task)
        baseline, projected = model_context(raw), model_context(raw, basis=basis)
        self.assert_reference(projected, GOAL.strip())
        self.assertIn("customer_input_sources", raw)
        index = projected["customer_input_sources"]
        self.assertTrue(index["entries"])
        self.assertEqual(index, baseline["customer_input_sources"])
        self.assertEqual(index, raw["customer_input_sources"])
        self.assertEqual(index["acceptance_source"], {"status": "initial_acceptance", "event_seq": created.seq,
            "event_hash": created.hash, "count": 2, "readable": True})
        acceptance_entries = [entry for entry in index["entries"] if entry["source_type"] == "task_acceptance"]
        self.assertEqual({row["acceptance_index"] for row in acceptance_entries}, {0, 1})
        self.assertTrue(all((row["seq"], row["hash"]) == (created.seq, created.hash) for row in acceptance_entries))
        self.assertTrue(any((row["seq"], row["hash"]) == (message.seq, message.hash) for row in index["entries"]))
        self.assertNotIn(foreign.seq, [row["seq"] for row in index["entries"]])
        goal_entry, = [row for row in index["entries"] if row["source_type"] == "task_goal"]
        self.assertEqual(goal_entry["preview_ref"], "current_work.objective")
        self.assertEqual((goal_entry["seq"], goal_entry["hash"]), (created.seq, created.hash))

        def read(arguments):
            return f.rt.team._execute(task, f.rt.organization.leader, "read_messages", arguments, None, None, None, None)

        for position, expected in enumerate(acceptance):
            first = read({"scope": "customer_message", "message_seq": created.seq, "expected_hash": created.hash,
                          "acceptance_index": position, "offset": 0, "length": 5})
            self.assertEqual(first["status"], "ok")
            self.assertTrue(first["has_more"])
            last = read({"scope": "customer_message", "message_seq": created.seq, "expected_hash": created.hash,
                         "acceptance_index": position, "offset": first["next_offset"]})
            self.assertEqual(first["text"] + last["text"], expected)
            self.assertFalse(last["has_more"])
        actual_message = read({"scope": "customer_message", "message_seq": message.seq, "expected_hash": message.hash})
        self.assertEqual(actual_message["text"], message.payload["text"])
        original_goal = read({"scope": "customer_message", "message_seq": created.seq, "expected_hash": created.hash})
        self.assertEqual(original_goal["text"], GOAL.strip())
        with self.assertRaises(ValueError):
            read({"scope": "customer_message", "message_seq": created.seq,
                  "expected_hash": "0" * 64, "acceptance_index": 0})
        with self.assertRaises(ValueError):
            read({"scope": "customer_message", "message_seq": foreign.seq,
                  "expected_hash": foreign.hash, "acceptance_index": 0})
        self.assertEqual(raw, before)
        self.assertEqual(f.rt.log.read(task_id=task), events)
        self.assertTrue(f.rt.verify_chain().ok)

    def test_review_feedback_and_goal_reference_both_round_trip(self):
        f = self.fixture(review.TeamReviewCycleTests)
        task = f.rt.create_task(GOAL, acceptance=["独立验收项 🐾"])
        f.rt.team.enable(task)
        version = f.publish("Actual submitted text\n", author=f.manager, task_id=task)
        request = f.request([version], requester=f.manager, reviewer=f.worker, task_id=task)
        source = next(e for e in f.rt.team._messages(task) if e.seq == request["message_seq"])
        feedback = "独立复核的修改说明 🐾 e\u0301；保留完整依据。\n" * 35
        result = f.record(request["review_seq"], "changes_requested", reviewer=f.worker,
                          feedback=feedback, task_id=task, source=source)
        self.assertEqual(result["status"], "ok")
        report = next(e for e in reversed(f.rt.team._messages(task)) if e.payload.get("kind") == "report"
                      and e.payload.get("review_seq") == request["review_seq"])
        raw, _, _, _, basis = f.rt.team._model_input(task, report, f.rt.organization.leader, run_id="review-fixture")
        events = f.rt.log.read(task_id=task)
        baseline, projected = model_context(raw), model_context(raw, basis=basis)
        self.assert_reference(projected, GOAL.strip())
        self.assertTrue(projected["customer_input_sources"]["entries"])
        self.assertEqual(projected["customer_input_sources"], baseline["customer_input_sources"])
        row, = [r for r in projected["reviews"] if r["request_seq"] == request["review_seq"]]
        self.assertEqual(row["status"], "changes_requested")
        self.assertIsNone(row["feedback"])
        ref = row["feedback_context_ref"]
        self.assertEqual(ref["field"], "message.text")
        decision = next(e for e in events if e.type == "team.review_recorded" and e.payload["review_seq"] == request["review_seq"])
        self.assertEqual(decision.payload["feedback"], feedback.strip())
        self.assertEqual(projected["message"]["text"][ref["start"]:ref["end"]], decision.payload["feedback"])
        self.assertEqual(f.rt.log.read(task_id=task), events)
        self.assertTrue(f.rt.verify_chain().ok)

    def test_goal_change_failure_and_recovery_reassemble_current_snapshot(self):
        f = self.fixture(simple.SimpleTeamTests)
        task = f.rt.create_task(GOAL, acceptance=["原始验收项"])
        f.rt.team.enable(task)
        new_goal = "修订目标 🐾：" + GOAL.strip()

        def failure(context, history, tools):
            self.assert_reference(context, new_goal)
            self.assertTrue(context["customer_input_sources"]["entries"])
            raise RuntimeError("synthetic driver failure")

        f.driver.steps[f.manager] = [simple.turn("", ("set_objective", {"goal": new_goal})), failure]
        direction = f.rt.team.receive(task, "修改目标并处理", request_id="dedup-transition-failure")
        f.rt.collaborate(task)
        failed = [e for e in f.rt.log.read(task_id=task) if e.type == "team.run_finished" and e.payload["status"] == "failed"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(f.rt.team.view(task)["objective"], new_goal)
        source = f.rt.team.receive(task, "检查仍需处理的工作", request_id="dedup-recovery-input")
        claimed = f.rt.team._claim(task)
        self.assertEqual(claimed[0].seq, source.seq)
        notices = f.rt.team.recovery.prepare(task)
        self.assertEqual(len(notices), 1)
        self.assertTrue(f.rt.team.recovery.is_message(notices[0]))
        seen = []

        def resumed(context, history, tools):
            self.assertEqual(context["message"]["kind"], "recovery")
            self.assert_reference(context, new_goal)
            self.assertTrue(context["customer_input_sources"]["entries"])
            self.assertEqual(context["customer_input_sources"]["acceptance_source"]["count"], 1)
            origin = context["customer_input_sources"]["objective_source"]
            self.assertEqual(origin["status"], "linked_customer_input")
            self.assertEqual((origin["message_seq"], origin["message_hash"]), (direction.seq, direction.hash))
            self.assertEqual(history, [])
            seen.append(True)
            return simple.turn("保留原目标，不重放之前的操作。")

        f.driver.steps[f.manager] = [resumed]
        f.rt.collaborate(task)
        self.assertEqual(seen, [True])
        updates = [e for e in f.rt.log.read(task_id=task) if e.type == "team.objective_updated"]
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0].payload["goal"], new_goal)
        self.assertTrue(f.rt.verify_chain().ok)

    def test_external_goal_change_then_offline_acp_receives_reference(self):
        f = self.fixture(external.TeamExternalTests)
        task = f.rt.create_task(GOAL, acceptance=["独立验收项"])
        f.rt.team.enable(task)
        changed_goal = "外部改目标 🐾：" + GOAL.strip()
        seen = []

        def change(run):
            context = run.context
            self.assert_reference(context, GOAL.strip())
            result = run.call("set_objective", goal=changed_goal)
            self.assertTrue(result["scope_changed"])
            seen.append(True)
            return "目标已更新。"

        f.driver.scripts = {f.manager: [change]}
        f.rt.team.receive(task, "更新目标", request_id="external-dedup-transition")
        f.rt.collaborate(task)
        self.assertEqual(seen, [True])
        self.assertEqual(f.driver.errors, [])
        self.assertEqual(f.rt.team.view(task)["objective"], changed_goal)

        driver = QoderAcpDriver("external_fixture", "never-launched", "ExactModel", timeout=30,
                                max_run_seconds=120, tool_surface="catalog")
        self.addCleanup(driver.close)
        f.rt.drivers["external_fixture"] = driver
        peer = Path(__file__).parent / "fixtures" / "external_agent_peer.py"

        class OfflineSession(AcpSession):
            def __init__(self, command, **kwargs):
                super().__init__([sys.executable, "-u", str(peer.resolve()), "text", str(f.root)], **kwargs)

        f.rt.team.receive(task, "读取更新后的目标", request_id="external-dedup-followup")
        with patch("cater.external_agent.AcpSession", OfflineSession):
            f.rt.collaborate(task)
        wire = [json.loads(line) for line in (f.root / "external-wire.jsonl").read_text(encoding="utf-8").splitlines()]
        prompts = [item for item in wire if item.get("method") == "session/prompt"]
        self.assertEqual(len(prompts), 1)
        text = prompts[0]["params"]["prompt"][0]["text"]
        delivered = json.loads(text.rsplit("\n", 1)[1])
        self.assert_reference(delivered, changed_goal)
        self.assertTrue(delivered["customer_input_sources"]["entries"])
        self.assertEqual(delivered["customer_input_sources"]["acceptance_source"]["count"], 1)
        finishes = list(f.rt.log.iter_events(task_id=task, event_types=("team.external_run_finished",)))
        self.assertEqual(finishes[-1].payload["outcome"], "returned")
        self.assertTrue(finishes[-1].payload["prompt_attempted"])
        self.assertTrue(finishes[-1].payload["cleanup_complete"])
        self.assertTrue(f.rt.verify_chain().ok)
