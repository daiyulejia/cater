"""Bounded team status summaries through isolated, in-memory HTTP handlers."""

from __future__ import annotations

import copy
import unittest
from unittest.mock import patch

from cater.events import EventLog
from cater.fold import TaskSnapshot
from cater.team import TeamService
from cater.tool_protocol import ToolCall, ToolTurn
from cater.web import _task_summary
import test_team_web


class TeamStatusSummaryTests(unittest.TestCase):
    setUp = test_team_web.TeamWebTests.setUp
    factory = test_team_web.TeamWebTests.factory
    request = test_team_web.TeamWebTests.request
    create = test_team_web.TeamWebTests.create
    events = test_team_web.TeamWebTests.events
    run_current = test_team_web.TeamWebTests.run_current

    def assert_summary_matches(self, task):
        detail = self.request(f"/api/tasks/{task}")
        row = next(row for row in self.request("/api/task-summaries")["tasks"] if row["id"] == task)
        self.assertEqual(row["team"], detail["summary"]["team"])
        self.assertEqual(row["activity"], detail["summary"]["activity"])
        self.assertLessEqual(len(row["team"]["runs"]), 2)
        for run in row["team"]["runs"]:
            self.assertEqual(set(run), {"message_seq", "role", "status", "revision"})
        return detail, row

    def test_handler_activity_completion_preserves_role_outcome(self):
        for outcome in ("failed", "waiting", "completed"):
            with self.subTest(outcome=outcome):
                created = self.create(request_id=f"status-{outcome}")
                task = created["task"]["id"]
                self.assertEqual(created["activity"]["state"], "queued")
                _, queued = self.assert_summary_matches(task)
                self.assertEqual(queued["activity"]["state"], "queued")
                self.assertEqual(queued["team"]["runs"], [])
                if outcome == "waiting":
                    call = ToolCall("fixture-wait", "wait_for_reply", {})
                    self.driver.turns = [ToolTurn("", [call], tokens=1, message={
                        "role": "assistant", "content": "", "tool_calls": [
                            {"id": call.id, "name": call.name, "arguments": call.arguments}]
                    })]
                if outcome == "failed":
                    with patch.object(self.driver, "tool_turn", side_effect=RuntimeError("private fixture failure")):
                        self.run_current(task)
                else:
                    self.run_current(task)
                detail, row = self.assert_summary_matches(task)
                self.assertEqual(row["activity"]["state"], "completed")
                self.assertFalse(row["activity"]["active"])
                self.assertEqual([r["status"] for r in row["team"]["runs"]], [outcome])
                self.assertNotEqual(row["team"]["status"], "done")
                self.assertEqual(detail["team"]["runs"][0]["status"], outcome)
                self.assertIn("reason", detail["team"]["runs"][0])
                self.assertIn("run_id", detail["team"]["runs"][0])
                kinds = [e.type for e in self.events(task)]
                self.assertIn("activity.queued", kinds)
                self.assertIn("activity.completed", kinds)
                self.assertNotIn("activity.failed", kinds)

    def test_stable_handler_summary_cache_does_not_read_history(self):
        task = self.create()["task"]["id"]
        self.run_current(task)
        first = self.request("/api/task-summaries")
        with patch.object(EventLog, "read", side_effect=AssertionError("history read")), \
                patch.object(EventLog, "iter_events", side_effect=AssertionError("history iteration")), \
                patch.object(EventLog, "_row_to_event", side_effect=AssertionError("history decoding")), \
                patch.object(TeamService, "view", side_effect=AssertionError("team fold")):
            self.assertEqual(self.request("/api/task-summaries"), first)
            self.assertEqual(self.request("/api/task-summaries"), first)

    def test_selection_is_bounded_ordered_and_does_not_mutate_full_view(self):
        cases = (
            ([], []),
            ([{"revision": 6, "status": "failed"}], []),
            ([{"revision": 6, "status": "running"}], []),
            ([{"revision": 6, "status": "started"}], []),
            ([{"revision": 7, "status": "completed"}, {"revision": 7, "status": "failed"}], [1]),
            ([{"revision": 7, "status": "running"}], [0]),
            ([{"revision": 7, "status": "started"}], [0]),
            ([{"revision": 7, "status": "running"}, {"revision": 7, "status": "failed"}], [0, 1]),
            ([{"revision": 7, "status": "failed"}, {"revision": 6, "status": "started"}], [0]),
            ([{"revision": 7, "status": "running"}, {"revision": 7, "status": "completed"},
              {"revision": 6, "status": "running"}], [0, 1]),
            ([{"status": "started"}, {"revision": 7, "status": "completed"},
              {"revision": 6, "status": "started"}], [0, 1]),
            ([{"revision": 6, "status": "running"}, {"revision": 7, "status": "started"},
              {"revision": 7, "status": "waiting"}, {"revision": 6, "status": "failed"}], [1, 2]),
            ([{"revision": 7, "status": "failed"}, {"status": "waiting"},
              {"revision": 6, "status": "failed"}], [1]),
            ([{"revision": 7, "status": "completed"}] * 100 +
             [{"revision": 7, "status": "failed"}], [100]),
        )
        for runs, selected in cases:
            with self.subTest(runs=runs):
                full = {"enabled": True, "objective": "Fixture", "revision": 7,
                        "status": "running", "pending_count": 0, "runs": [
                            {"message_seq": i, "role": "dev", "run_id": f"private-{i}",
                             "reason": "PRIVATE_REASON", "text": "PRIVATE_TEXT", **run}
                            for i, run in enumerate(runs)]}
                before = copy.deepcopy(full)
                row = _task_summary(TaskSnapshot("fixture"), version=1, latest_seq=1,
                                    conversation_count=0, team=full)
                self.assertEqual(row["team"]["runs"], [
                    {key: full["runs"][i][key] for key in ("message_seq", "role", "status", "revision")
                     if key in full["runs"][i]} for i in selected])
                self.assertEqual(full, before)
                for run in row["team"]["runs"]:
                    self.assertIsNot(run, full["runs"][run["message_seq"]])
                self.assertLessEqual(len(row["team"]["runs"]), 2)

    def test_disabled_team_keeps_legacy_summary_shape(self):
        for team in (None, {"enabled": False, "runs": [{"status": "failed"}]}):
            row = _task_summary(TaskSnapshot("fixture"), version=1, latest_seq=1,
                                conversation_count=0, team=team)
            self.assertNotIn("team", row)


if __name__ == "__main__":
    unittest.main()
