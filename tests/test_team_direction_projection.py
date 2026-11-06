"""Persisted human direction must not relabel old team work as current."""

import unittest
from unittest.mock import patch

from cater.runtime import Runtime
from cater.tool_protocol import ToolCall
import test_simple_team as fixtures


class TeamDirectionProjectionTests(unittest.TestCase):
    def fixture(self):
        fixture = fixtures.SimpleTeamTests(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def test_old_assignment_is_not_claimed_as_new_direction(self):
        f = self.fixture()
        team = f.rt.team
        assignment = team._send(f.task, f.rt.organization.leader, f.worker,
                                "Old work", "assignment")
        direction = f.rt.command(f.task, "Change direction", goal="New objective")
        self.assertGreater(direction.seq, assignment.seq)
        self.assertIsNone(team._claim(f.task))
        self.assertIn(assignment, team._messages(f.task))
        self.assertFalse([event for event in f.events("team.run_started")
                          if event.payload.get("message_seq") == assignment.seq])

    def test_new_explicit_goal_replaces_old_team_goal_in_all_work_views(self):
        f = self.fixture()
        team = f.rt.team
        message = team.receive(f.task, "Initial input", request_id="old-direction-input")
        source, role, run = team._claim(f.task)
        context, workspace, binding, tools, _ = team._model_input(
            f.task, source, role, run_id=run.payload["run_id"])
        _, result = team._dispatch_tool(f.task, source, role, run, run.payload["revision"],
            ToolCall("set-old", "set_objective", {"goal": "Old objective"}),
            tools, workspace, binding, context, None)
        self.assertEqual(result["status"], "ok")
        f.rt.command(f.task, "Change direction", goal="New objective")
        view = team.view(f.task)
        new_message = team.receive(f.task, "Next input", request_id="new-direction-input")
        snapshot = team._context(f.task, new_message, role)
        self.assertEqual(f.rt.task(f.task).goal, "New objective")
        self.assertEqual(view["objective"], "New objective")
        self.assertEqual(snapshot["current_work"]["objective"], "New objective")
        self.assertEqual(snapshot["work_checkpoint"]["objective"], "New objective")
        self.assertTrue(any(e.type == "team.objective_updated" and e.payload.get("goal") == "Old objective"
                            for e in f.rt.log.read(task_id=f.task)))

    def test_old_delivery_is_historical_after_explicit_new_direction(self):
        import test_team_review_cycle as review

        f = review.TeamReviewCycleTests(methodName="runTest")
        f.setUp()
        self.addCleanup(f.doCleanups)
        self.assertEqual(f.finish()["status"], "ok")
        completed, = f.events("team.objective_completed")
        f.rt.command(f.task, "Change direction", goal="New objective")
        view = f.rt.team.view(f.task)
        self.assertEqual(view["objective"], "New objective")
        self.assertEqual(view["status"], "running")
        self.assertFalse(f.rt.team.recovery.scope(f.task)["done"])
        self.assertTrue(f.rt.team.delivery.index(f.task)["historical"])
        self.assertTrue(f.rt.team.delivery.read(f.task, {"delivery_seq": completed.seq})["historical"])
        self.assertIn(completed, f.rt.log.read(task_id=f.task))

    def test_observational_command_does_not_retire_unrelated_work(self):
        f = self.fixture()
        team = f.rt.team
        assignment = team._send(f.task, f.rt.organization.leader, f.worker,
                                "Existing work", "assignment")
        before = f.rt.work.scope(f.task)["direction_seq"]
        f.rt.command(f.task, "现在进度如何？")
        self.assertEqual(f.rt.work.scope(f.task)["direction_seq"], before)
        claimed = team._claim(f.task)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed[0].seq, assignment.seq)

    def test_old_assignment_continuation_and_late_report_are_not_new_work(self):
        f = self.fixture()
        team = f.rt.team
        leader, worker = f.rt.organization.leader, team._role(f.worker)
        original = team._send(f.task, leader, worker.id, "Old work", "assignment")
        f.rt.command(f.task, "Change direction", goal="New objective")
        continuation = team._send(f.task, leader, worker.id, "Old continuation", "continuation",
                                  continue_assignment_seq=original.seq)
        report = team._send(f.task, worker, leader.id, "Old report", "report", reply_to=original.seq)
        self.assertIsNone(team._claim(f.task))
        self.assertTrue({original.seq, continuation.seq, report.seq} <= team._inactive_messages(f.task))
        self.assertTrue({original.seq, continuation.seq, report.seq} <= {e.seq for e in team._messages(f.task)})

    def test_current_assignment_continuation_and_report_remain_eligible(self):
        f = self.fixture()
        team = f.rt.team
        leader, worker = f.rt.organization.leader, team._role(f.worker)
        team._send(f.task, leader, worker.id, "Old work", "assignment")
        f.rt.command(f.task, "Change direction", goal="New objective")
        current = team._send(f.task, leader, worker.id, "Current work", "assignment")
        continuation = team._send(f.task, leader, worker.id, "Current continuation", "continuation",
                                  continue_assignment_seq=current.seq)
        self.assertEqual(team.recovery.assignments(f.task)[0]["seq"], current.seq)
        report = team._send(f.task, worker, leader.id, "Current report", "report", reply_to=current.seq)
        inactive = team._inactive_messages(f.task)
        self.assertTrue({current.seq, continuation.seq, report.seq}.isdisjoint(inactive))

    def test_current_direction_review_remains_claimable(self):
        import test_team_review_cycle as review

        f = review.TeamReviewCycleTests(methodName="runTest")
        f.setUp()
        self.addCleanup(f.doCleanups)
        f.rt.command(f.task, "Change direction", goal="New objective")
        artifact = f.publish()
        request = f.request([artifact])
        self.assertEqual(request["status"], "ok")
        row, = f.rt.team_reviews.view(f.task)
        self.assertEqual(row["status"], "pending")
        claimed = f.rt.team._claim(f.task)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed[0].seq, request["message_seq"])

    def test_current_direction_report_is_claimable(self):
        f = self.fixture()
        team = f.rt.team
        leader, worker = f.rt.organization.leader, team._role(f.worker)
        f.rt.command(f.task, "Change direction", goal="New objective")
        assignment = team._send(f.task, leader, worker.id, "Current work", "assignment")
        report = team._send(f.task, worker, leader.id, "Current report", "report", reply_to=assignment.seq)
        claimed = team._claim(f.task)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed[0].seq, report.seq)

    def test_restart_claims_new_assignment_without_relabeling_old_one(self):
        f = self.fixture()
        team = f.rt.team
        old = team._send(f.task, f.rt.organization.leader, f.worker, "Old work", "assignment")
        f.rt.command(f.task, "Change direction", goal="New objective")
        new = team._send(f.task, f.rt.organization.leader, f.worker, "New work", "assignment")
        with Runtime(f.rt.log.path, roles_path=f.roles, drivers={"team_test": fixtures.TeamDriver()}) as reopened:
            claimed = reopened.team._claim(f.task)
            self.assertIsNotNone(claimed)
            self.assertEqual(claimed[0].seq, new.seq)
            self.assertIn(old.seq, reopened.team._inactive_messages(f.task))
            self.assertNotIn(new.seq, reopened.team._inactive_messages(f.task))
            self.assertEqual(reopened.team.view(f.task)["objective"], "New objective")

    def test_current_direction_formal_return_survives_full_tool_chain(self):
        from test_return_work import ReturnWorkTests

        helper = ReturnWorkTests(methodName="runTest")
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        for mode in ("native", "external"):
            with self.subTest(mode=mode):
                f = helper.fixture(mode)
                f.rt.command(f.task, "Change direction", goal="New objective")
                sent, = helper.run_calls(f, helper.human(f), ("delegate", {
                    "recipient": f.worker, "instruction": "Current work"}))
                assignment = helper.message(f, sent["message_seq"])
                returned, = helper.run_calls(f, assignment, ("return_work", {"text": "Current result"}))
                report = helper.message(f, returned["message_seq"])
                self.assertNotIn(report.seq, f.rt.team._inactive_messages(f.task))
                claimed = f.rt.team._claim(f.task)
                self.assertIsNotNone(claimed)
                self.assertEqual(claimed[0].seq, report.seq)

    def test_old_review_is_historical_and_does_not_block_current_direction(self):
        import test_team_review_cycle as review

        f = review.TeamReviewCycleTests(methodName="runTest")
        f.setUp()
        self.addCleanup(f.doCleanups)
        artifact = f.publish()
        request = f.request([artifact])
        self.assertEqual(request["status"], "ok")
        f.rt.command(f.task, "Change direction", goal="New objective")
        row, = f.rt.team_reviews.view(f.task)
        self.assertEqual(row["status"], "stale")
        self.assertEqual(row["request_seq"], request["review_seq"])
        self.assertFalse(f.rt.team_reviews.blockers(f.task))
        self.assertIsNone(f.rt.team._claim(f.task))

    def test_old_artifact_is_not_implicitly_delivered_under_new_direction(self):
        import test_team_review_cycle as review

        f = review.TeamReviewCycleTests(methodName="runTest")
        f.setUp()
        self.addCleanup(f.doCleanups)
        artifact = f.publish(content="old", author=f.worker)
        f.rt.command(f.task, "Change direction", goal="New objective")
        self.assertEqual(f.finish()["status"], "ok")
        completed, = f.events("team.objective_completed")
        self.assertNotIn(artifact, [a["seq"] for a in completed.payload.get("artifacts", [])])
        self.assertTrue(any(a["seq"] == artifact for a in f.rt.task(f.task).deliverables))

    def test_direction_change_keeps_unknown_started_effect_without_replay(self):
        f = self.fixture()
        team = f.rt.team
        assignment = team._send(f.task, f.rt.organization.leader, f.worker, "Old work", "assignment")
        claim = team._claim(f.task)
        self.assertEqual(claim[0].seq, assignment.seq)
        f.driver.steps[f.worker] = [fixtures.turn("", ("publish_artifact", {
            "filename": "fixture.txt", "content": "x", "summary": "x", "format": "text"})),
            fixtures.turn("Done")]
        with patch.object(team, "_execute", side_effect=OSError("synthetic")):
            list(team._run_message(f.task, *claim))
        before = team._pending_operations(f.task)
        self.assertEqual(len(before), 1)
        f.rt.command(f.task, "Change direction", goal="New objective")
        self.assertEqual(team._pending_operations(f.task), before)
        self.assertIsNone(team._claim(f.task))
        self.assertEqual(len([e for e in f.events("team.tool_started")
                              if e.payload.get("name") == "publish_artifact"]), 1)

    def test_old_run_finishing_after_direction_does_not_replace_new_checkpoint(self):
        from cater.events import Actor

        f = self.fixture()
        f.rt.team.receive(f.task, "Old input", request_id="old-progress-input")
        _, _, started = f.rt.team._claim(f.task)
        f.rt.command(f.task, "Change direction", goal="New objective")
        f.rt.log.emit(type="team.run_finished", task_id=f.task, actor=Actor.system("team"),
                      payload={**{k: started.payload[k] for k in ("message_seq", "role", "run_id")},
                               "status": "interrupted", "reason": "OLD_FIXTURE_REASON"})
        view = f.rt.team.view(f.task)
        self.assertEqual(view["objective"], "New objective")
        self.assertNotIn("OLD_FIXTURE_REASON", view["checkpoint"])
        self.assertTrue(any(row["reason"] == "OLD_FIXTURE_REASON" for row in view["runs"]))

    def test_late_historical_delivery_does_not_complete_new_direction(self):
        from cater.events import Actor

        f = self.fixture()
        f.rt.team.receive(f.task, "Old input", request_id="old-delivery-input")
        _, _, started = f.rt.team._claim(f.task)
        f.rt.command(f.task, "Change direction", goal="New objective")
        updated = f.rt.log.emit(type="team.objective_updated", task_id=f.task, actor=Actor.system("team"),
            payload={"run_id": started.payload["run_id"], "goal": "Late old objective"})
        completed = f.rt.log.emit(type="team.objective_completed", task_id=f.task, actor=Actor.system("team"),
            payload={"run_id": started.payload["run_id"], "summary": "Old delivery", "verification": "fixture"})
        view = f.rt.team.view(f.task)
        self.assertEqual((view["objective"], view["status"]), ("New objective", "running"))
        self.assertFalse(f.rt.team.recovery.scope(f.task)["done"])
        self.assertTrue(f.rt.team.delivery.index(f.task)["historical"])
        self.assertIn(updated, f.rt.log.read(task_id=f.task))
        self.assertIn(completed, f.rt.log.read(task_id=f.task))


if __name__ == "__main__":
    unittest.main()
