"""Deterministic protocol regressions, not real-model comprehension or QA.

All model actions are scripted; customer wording never selects fixture behavior.
"""

from copy import deepcopy
import json
import unittest
from unittest.mock import patch

import test_team_external as external
import test_team_review_cycle as reviews


FEEDBACK = "The original conversion still loses tables; preserve the requested layout."
REVIEW_INSTRUCTION = "Check the original conversion and table layout in this file."


class ObjectiveContinuityTests(unittest.TestCase):
    def fixture(self, status):
        f = reviews.TeamReviewCycleTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        artifact = f.publish()
        delegated, = f.exchange(f.manager, ("delegate", {
            "recipient": f.worker, "instruction": "Implement the original conversion and obtain review.",
        }))
        self.assertEqual(delegated["status"], "ok")
        assignment = next(e for e in f.events("team.message_received")
                          if e.seq == delegated["message_seq"])
        requested, = f.exchange(f.worker, ("request_review", {
            "reviewer": f.reviewer, "artifact_seqs": [artifact], "instruction": REVIEW_INSTRUCTION,
        }), source=assignment)
        self.assertEqual(requested["status"], "ok")
        if status != "pending":
            self.assertEqual(f.record(requested["review_seq"], status, feedback=FEEDBACK)["status"], "ok")
        self.assertEqual(f.rt.team_reviews.view(f.task)[0]["status"], status)
        self.assertEqual(f.context(role_id=f.worker)["active_assignment"]["seq"], assignment.seq)
        return f, assignment

    def state(self, f):
        view = f.rt.team.view(f.task)
        return deepcopy({key: view[key] for key in
                         ("objective", "revision", "status", "assignments", "reviews", "withdrawals")})

    def assert_history_preserved(self, f, history):
        self.assertEqual(f.rt.log.read(task_id=f.task)[:len(history)], history)
        self.assertTrue(f.rt.verify_chain().ok)

    def manager_exchange(self, f, wire, source, *calls):
        """Capture the real production prompt; only the model endpoint is fake."""
        prompts = []
        if wire == "native":
            original = f.driver.tool_turn

            def capture(**kwargs):
                prompts.append(kwargs["prompt"])
                return original(**kwargs)

            with patch.object(f.driver, "tool_turn", side_effect=capture):
                results = f.exchange(f.manager, *calls, source=source)
        else:
            driver = external.ScriptedExternalDriver()

            def script(run):
                for name, arguments in calls:
                    run.call(name, **arguments)
                run.call("wait_for_reply")

            driver.scripts[f.manager] = [script]
            original = external.ScriptedExternalRun.run

            def capture(run, prompt):
                prompts.append(prompt)
                return original(run, prompt)

            with patch.dict(f.rt.drivers, {f.driver.name: driver}), \
                    patch.object(external.ScriptedExternalRun, "run", capture):
                results = f.exchange(f.manager, *calls, source=source)
            self.assertEqual(driver.errors, [])
            self.assertEqual(len(driver.runs), 1)
            self.assertEqual(driver.scripts[f.manager], [])
            self.assertEqual(f.rt.team.external_runs.pending(f.task), [])
        self.assertTrue(prompts)
        return results, [json.loads(prompt.rsplit("\n", 1)[1]) for prompt in prompts]

    def check_repair_followup(self, status, wire):
        f, assignment = self.fixture(status)
        before = self.state(f)
        self.assertEqual(before["status"], "running")
        projections = {role: f.context(role_id=role)
                       for role in (f.manager, f.worker, f.reviewer)}
        self.assertIn(assignment.seq, [row["seq"] for row in projections[f.manager]["outgoing_work"]])
        self.assertIn(assignment.seq, [row["seq"] for row in projections[f.worker]["open_assignments"]])
        history = deepcopy(f.rt.log.read(task_id=f.task))
        text = (f"Continue the original request: {before['objective']}. "
                "Repair the table conversion and keep the requested output layout.")
        source = f.rt.team.receive(f.task, text)
        self.assertEqual(self.state(f), before, "Receiving a repair supplement must preserve work scope")

        context = f.rt.team._context(f.task, source, f.rt.team._role(f.manager))
        self.assertEqual(context["message"]["text"], text)
        self.assertEqual(context["reviews"], before["reviews"])
        self.assertEqual(self.state(f), before, "Context construction must preserve work scope")
        results, contexts = self.manager_exchange(f, wire, source, ("finish_work", {
            "summary": "Scripted premature delivery", "verification": "Offline protocol fixture only",
        }))
        result, = results
        self.assertEqual((result["status"], result["code"]), ("error", "review_pending"))
        self.assertEqual(result["reviews"][0]["status"], status)
        for context in contexts:
            for key in ("objective", "revision", "status"):
                self.assertEqual(context["current_work"][key], before[key])
            self.assertEqual(context["message"]["text"], text)
            self.assertEqual(context["reviews"], before["reviews"])
            self.assertEqual(context["reviews"][0]["instruction"], REVIEW_INSTRUCTION)
            if status == "changes_requested":
                self.assertEqual(context["reviews"][0]["feedback"], FEEDBACK)
            self.assertIn(assignment.seq, [row["seq"] for row in context["outgoing_work"]])
        for role, previous in projections.items():
            current = f.context(role_id=role)
            for key in ("open_assignments", "outgoing_work", "reviews"):
                self.assertEqual(current[key], previous[key])
        self.assertEqual(self.state(f), before)
        self.assertEqual(f.events("team.objective_updated"), [])
        self.assertEqual(f.events("team.objective_completed"), [])
        self.assert_history_preserved(f, history)

    def test_native_repair_supplement_preserves_pending_and_changes_requested_work(self):
        for status in ("pending", "changes_requested"):
            with self.subTest(status=status):
                self.check_repair_followup(status, "native")

    def test_external_prompt_preserves_running_goal_and_repair_feedback(self):
        self.check_repair_followup("changes_requested", "external")

    def test_explicit_objective_replacement_retires_old_review_and_assignment(self):
        for status, wire in (("pending", "native"), ("approved", "external")):
            with self.subTest(status=status, wire=wire):
                f, assignment = self.fixture(status)
                before = self.state(f)
                old_review, = before["reviews"]
                self.assertEqual(f.rt.team.recovery.continuation(
                    f.task, f.rt.team._role(f.manager), f.worker, assignment.seq)["seq"], assignment.seq)
                history = deepcopy(f.rt.log.read(task_id=f.task))
                goal = "Deliver a separate inventory export"
                source = f.rt.team.receive(f.task, goal)
                self.assertEqual(self.state(f), before)
                results, _ = self.manager_exchange(f, wire, source, ("set_objective", {"goal": goal}))
                adopted, = results
                updated, = f.events("team.objective_updated")
                self.assertEqual(adopted["status"], "ok")
                self.assertTrue(adopted["scope_changed"])
                self.assertEqual(adopted["work_status"], "running")
                self.assertEqual(adopted["revision"], updated.seq)
                self.assertGreater(updated.seq, before["revision"])
                context = f.context(role_id=f.manager)
                self.assertEqual(context["current_work"]["objective"], goal)
                self.assertEqual(context["current_work"]["revision"], updated.seq)
                self.assertEqual(context["reviews"], [{**old_review, "status": "stale"}])
                self.assertEqual(context["outgoing_work"], [])
                self.assertEqual(f.context(role_id=f.worker)["open_assignments"], [])

                continued, = f.exchange(f.manager, ("send_message", {
                    "recipient": f.worker, "text": "Continue the old assignment",
                    "continue_assignment_seq": assignment.seq,
                }))
                self.assertEqual(continued["status"], "error")
                self.assertFalse([e for e in f.events("team.message_received")
                                  if e.payload.get("kind") == "continuation"])
                calls_before = len(f.driver.calls)
                tools_before = f.events("team.tool_started")
                f.exchange(f.worker, ("publish_artifact", {
                    "filename": "forbidden.txt", "content": "Old assignment must never execute",
                    "summary": "Stale work", "format": "text",
                }), source=assignment, expected_statuses=("interrupted",), expected_result_count=0)
                self.assertEqual(len(f.driver.calls), calls_before)
                self.assertEqual(f.events("team.tool_started"), tools_before)

                # Identical bytes make objective scope, not a changed hash, decisive.
                new_artifact = f.publish()
                decisions = f.events("team.review_recorded")
                self.assertEqual(f.record(old_review["request_seq"])["status"], "error")
                self.assertEqual(f.events("team.review_recorded"), decisions)
                self.assertEqual(f.request([new_artifact], review_seq=old_review["request_seq"])["status"], "error")
                fresh = f.request([new_artifact])
                self.assertEqual(fresh["status"], "ok")
                self.assertNotEqual(fresh["root_seq"], old_review["root_seq"])
                rows = {row["request_seq"]: row for row in f.rt.team_reviews.view(f.task)}
                self.assertEqual(rows[old_review["request_seq"]], {**old_review, "status": "stale"})
                self.assertEqual(rows[fresh["review_seq"]]["status"], "pending")
                self.assertEqual(rows[fresh["review_seq"]]["revision"], updated.seq)
                blocked = f.finish()
                self.assertEqual((blocked["status"], blocked["code"]), ("error", "review_pending"))
                self.assertEqual([row["request_seq"] for row in blocked["reviews"]], [fresh["review_seq"]])
                self.assertEqual(f.events("team.objective_completed"), [])
                self.assertEqual(f.rt.team.view(f.task)["revision"], updated.seq)
                self.assert_history_preserved(f, history)


if __name__ == "__main__":
    unittest.main()
