"""Objective references retain exact snapshot and customer-source boundaries."""

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cater.runtime import Runtime
from cater.team_context import model_context, model_json
from cater.tool_protocol import ToolCall, ToolTurn


DIGEST = "a" * 64
GOAL = '顾客目标 🐾 e\u0301 "原文" \\路径\r\n' * 100


def context(goal=GOAL, *, revision=3, status="running"):
    return {
        "you": {"id": "test"}, "members": [],
        "current_work": {"enabled": True, "objective": goal, "revision": revision,
                         "status": status, "pending_count": 0, "checkpoint": "状态", "runs": []},
        "work_checkpoint": {"as_of_seq": 12, "as_of_hash": DIGEST,
                            "scope": {"revision": revision, "done": status == "done"},
                            "objective": goal, "open_work_count": 0},
        "message": {"seq": 10, "sender": "human", "sender_kind": "human", "text": goal},
        "latest_customer_message": {"seq": 9, "sender": "human", "sender_kind": "human", "text": goal},
        "customer_input_sources": {"status": "available", "complete": True,
            "entries": [{"source_id": "first", "seq": 9}, {"source_id": "second", "seq": 10}]},
        "active_assignment": None, "open_assignments": [], "outgoing_work": [],
        "notifications": [], "conversation": [], "artifacts": [], "reviews": [],
    }


def basis():
    return {"as_of_seq": 12, "as_of_hash": DIGEST}


class ObjectiveContextDedupTests(unittest.TestCase):
    def test_exact_unicode_body_round_trips_with_source_and_status_unchanged(self):
        raw = context()
        snapshot = copy.deepcopy(raw)
        baseline = model_context(raw)
        projected = model_context(raw, basis=basis())
        cp, work = projected["work_checkpoint"], projected["current_work"]
        self.assertIsNone(cp["objective"])
        self.assertEqual(cp["objective_context_ref"], "current_work.objective")
        self.assertEqual(work["objective"], GOAL)
        restored = {**cp, "objective": work["objective"]}
        restored.pop("objective_context_ref")
        self.assertEqual(restored, baseline["work_checkpoint"])
        self.assertEqual(projected["current_work"], baseline["current_work"])
        self.assertEqual(projected["customer_input_sources"], baseline["customer_input_sources"])
        self.assertEqual(projected["message"], baseline["message"])
        self.assertEqual(projected["latest_customer_message"], baseline["latest_customer_message"])
        self.assertEqual(raw, snapshot)
        self.assertIn("objective_context_ref", projected["context_window"]["objective_reference_note"])
        self.assertLess(len(model_json(projected).encode("utf-8")), len(model_json(baseline).encode("utf-8")))
        self.assertEqual(json.loads(model_json(projected))["current_work"]["objective"], GOAL)

    def test_new_goal_revision_and_completed_state(self):
        for revision, status in ((0, "running"), (5, "running"), (8, "done")):
            with self.subTest(revision=revision, status=status):
                projected = model_context(context(revision=revision, status=status), basis=basis())
                self.assertEqual(projected["work_checkpoint"]["objective_context_ref"], "current_work.objective")
                self.assertEqual(projected["current_work"]["revision"], revision)
                self.assertEqual(projected["current_work"]["status"], status)
                self.assertEqual(projected["work_checkpoint"]["scope"], {"revision": revision, "done": status == "done"})

    def test_short_empty_or_unproved_inputs_remain_inline(self):
        for goal in ("", "短", "🐾"):
            with self.subTest(goal=goal):
                raw = context(goal)
                self.assertEqual(model_context(raw, basis=basis()), model_context(raw))
        invalid = [
            (lambda c, b: b.update(as_of_seq=11)),
            (lambda c, b: b.update(as_of_hash="b" * 64)),
            (lambda c, b: b.update(as_of_seq=True)),
            (lambda c, b: c["work_checkpoint"].update(as_of_seq=13)),
            (lambda c, b: c["work_checkpoint"]["scope"].update(revision=4)),
            (lambda c, b: c["work_checkpoint"]["scope"].update(revision=True)),
            (lambda c, b: c["work_checkpoint"]["scope"].update(done=True)),
            (lambda c, b: c["work_checkpoint"].update(objective=GOAL + "x")),
            (lambda c, b: c["work_checkpoint"].update(objective_truncated=True)),
            (lambda c, b: c["work_checkpoint"].update(objective_context_ref="other")),
            (lambda c, b: c["current_work"].update(objective_truncated=True)),
            (lambda c, b: c["current_work"].update(revision=13)),
            (lambda c, b: c["current_work"].update(status="unknown")),
        ]
        for mutate in invalid:
            raw, proof = context(), basis()
            mutate(raw, proof)
            with self.subTest(mutation=invalid.index(mutate)):
                self.assertEqual(model_context(raw, basis=proof), model_context(raw))
        self.assertEqual(model_context(context(), basis=None), model_context(context()))

    def test_host_snapshot_and_event_history_round_trip(self):
        class Fake:
            name = "fake"
            tool_loading = "all"

            def tool_turn(self, **_kwargs):
                raise AssertionError("No model invocation is expected")

        with tempfile.TemporaryDirectory(prefix="objective-context-") as temporary:
            roles = json.loads((Path(__file__).resolve().parents[1] / "cater/packs/default_roles.json").read_text(encoding="utf-8"))
            roles["organization"]["leader"]["driver"] = "fake"
            for role in roles["roles"]:
                role["driver"] = "fake"
            roles_path = Path(temporary) / "roles.json"
            roles_path.write_text(json.dumps(roles), encoding="utf-8")
            with Runtime(":memory:", drivers={"fake": Fake()}, roles_path=roles_path) as rt:
                task = rt.create_task(GOAL)
                rt.team.enable(task)
                message = rt.team.receive(task, GOAL, request_id="objective-dedup-fixture")
                raw, _, _, _, proof = rt.team._model_input(task, message, rt.organization.leader, run_id="fixture")
                before = copy.deepcopy(raw)
                events = rt.log.read(task)
                projected = model_context(raw, basis=proof)
                self.assertEqual(projected["work_checkpoint"]["objective_context_ref"], "current_work.objective")
                self.assertEqual(projected["current_work"]["objective"], GOAL.strip())
                self.assertIn("customer_input_sources", raw)
                self.assertTrue(raw["customer_input_sources"].get("entries"))
                self.assertEqual(projected["customer_input_sources"], raw["customer_input_sources"])
                self.assertEqual(rt.log.read(task), events)
                self.assertEqual(raw, before)
                self.assertTrue(rt.verify_chain().ok)
                # An intervening event in another task prevents a provable
                # equality of the global and task watermarks; retain both.
                rt.create_task("Other task")
                raw2, _, _, _, proof2 = rt.team._model_input(task, message, rt.organization.leader, run_id="fixture")
                self.assertNotEqual(raw2["work_checkpoint"]["as_of_seq"], proof2["as_of_seq"])
                self.assertNotIn("objective_context_ref", model_context(raw2, basis=proof2)["work_checkpoint"])

    def test_native_assembly_supplies_the_host_snapshot(self):
        class Fake:
            name = "fake"
            tool_loading = "all"

            def __init__(self):
                self.contexts = []

            def tool_turn(self, *, role, prompt, history, tools):
                self.contexts.append(json.loads(prompt.rsplit("\n", 1)[1]))
                return ToolTurn("已收到。", [], 0, {"token_usage": "estimated"},
                                {"role": "assistant", "content": "已收到。", "tool_calls": []})

        fake = Fake()
        with tempfile.TemporaryDirectory(prefix="objective-assembly-") as temporary:
            roles = json.loads((Path(__file__).resolve().parents[1] / "cater/packs/default_roles.json").read_text(encoding="utf-8"))
            roles["organization"]["leader"]["driver"] = "fake"
            for role in roles["roles"]:
                role["driver"] = "fake"
            roles_path = Path(temporary) / "roles.json"
            roles_path.write_text(json.dumps(roles), encoding="utf-8")
            with Runtime(":memory:", drivers={"fake": fake}, roles_path=roles_path) as rt:
                task = rt.create_task(GOAL)
                rt.team.enable(task)
                rt.team.receive(task, GOAL, request_id="objective-assembly-fixture")
                rt.collaborate(task)
                self.assertEqual(len(fake.contexts), 1)
                actual = fake.contexts[0]
                self.assertEqual(actual["work_checkpoint"]["objective_context_ref"], "current_work.objective")
                self.assertEqual(actual["current_work"]["objective"], GOAL.strip())
                self.assertTrue(rt.verify_chain().ok)

    def test_real_goal_transition_keeps_new_revision_and_original_customer_source(self):
        new_goal = "新的交付目标：" + GOAL.strip()

        class Fake:
            name = "fake"
            tool_loading = "all"

            def __init__(self):
                self.contexts = []

            def tool_turn(self, *, role, prompt, history, tools):
                self.contexts.append(json.loads(prompt.rsplit("\n", 1)[1]))
                if len(self.contexts) == 1:
                    call = ToolCall("set-goal-1", "set_objective", {"goal": new_goal})
                    return ToolTurn("", [call], 0, {"token_usage": "estimated"},
                                    {"role": "assistant", "content": "", "tool_calls": [
                                        {"id": call.id, "name": call.name, "arguments": call.arguments}]})
                return ToolTurn("已更新目标。", [], 0, {"token_usage": "estimated"},
                                {"role": "assistant", "content": "已更新目标。", "tool_calls": []})

        fake = Fake()
        with tempfile.TemporaryDirectory(prefix="objective-transition-") as temporary:
            roles = json.loads((Path(__file__).resolve().parents[1] / "cater/packs/default_roles.json").read_text(encoding="utf-8"))
            roles["organization"]["leader"]["driver"] = "fake"
            for role in roles["roles"]:
                role["driver"] = "fake"
            roles_path = Path(temporary) / "roles.json"
            roles_path.write_text(json.dumps(roles), encoding="utf-8")
            with Runtime(":memory:", drivers={"fake": fake}, roles_path=roles_path) as rt:
                task = rt.create_task(GOAL)
                rt.team.enable(task)
                rt.team.receive(task, GOAL, request_id="objective-transition-fixture")
                rt.collaborate(task)
                self.assertEqual(len(fake.contexts), 2)
                initial, changed = fake.contexts
                self.assertEqual(initial["current_work"]["objective"], GOAL.strip())
                self.assertEqual(changed["current_work"]["objective"], new_goal)
                self.assertGreater(changed["current_work"]["revision"], initial["current_work"]["revision"])
                self.assertEqual(changed["work_checkpoint"]["scope"]["revision"], changed["current_work"]["revision"])
                self.assertEqual(changed["work_checkpoint"]["objective_context_ref"], "current_work.objective")
                self.assertEqual(changed["message"]["text"], GOAL)
                self.assertEqual(rt.task(task).goal, new_goal)
                self.assertTrue(rt.verify_chain().ok)

    def test_external_assembly_references_only_matching_snapshot(self):
        from test_team_external import TeamExternalTests

        for mismatch in (False, True):
            with self.subTest(mismatch=mismatch):
                fixture = TeamExternalTests()
                fixture.setUp()
                self.addCleanup(fixture.doCleanups)
                task = fixture.rt.create_task(GOAL)
                fixture.rt.team.enable(task)
                seen, captured = [], []
                customer_message = "新的顾客补充：保留完整要求。" * 100
                model_input = fixture.rt.team._model_input

                def snapshot(*args, **kwargs):
                    raw, workspace, binding, tools, proof = model_input(*args, **kwargs)
                    captured.append(copy.deepcopy(raw))
                    if mismatch:
                        proof = {**proof, "as_of_hash": "f" * 64}
                    return raw, workspace, binding, tools, proof

                def reply(run):
                    actual = run.context
                    baseline = model_context(captured[-1])
                    checkpoint = actual["work_checkpoint"]
                    self.assertEqual(actual["current_work"]["objective"], GOAL.strip())
                    if mismatch:
                        self.assertEqual(checkpoint, baseline["work_checkpoint"])
                        self.assertNotIn("objective_context_ref", checkpoint)
                    else:
                        self.assertIsNone(checkpoint["objective"])
                        self.assertEqual(checkpoint["objective_context_ref"], "current_work.objective")
                        restored = {**checkpoint, "objective": actual["current_work"]["objective"]}
                        restored.pop("objective_context_ref")
                        self.assertEqual(restored, baseline["work_checkpoint"])
                    for key in ("message", "latest_customer_message", "customer_input_sources"):
                        self.assertEqual(actual.get(key), baseline.get(key))
                    self.assertEqual(actual["message"]["text"], customer_message)
                    self.assertTrue(actual["customer_input_sources"]["entries"])
                    seen.append(True)
                    return "已收到。"

                fixture.driver.scripts = {fixture.manager: [reply]}
                fixture.rt.team.receive(task, customer_message, request_id="external-objective-fixture")
                with patch.object(fixture.rt.team, "_model_input", side_effect=snapshot):
                    fixture.rt.collaborate(task)
                self.assertEqual(seen, [True])
                self.assertEqual(fixture.driver.errors, [])
                self.assertTrue(fixture.rt.verify_chain().ok)


if __name__ == "__main__":
    unittest.main()
