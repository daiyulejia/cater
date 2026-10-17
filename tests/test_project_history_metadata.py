"""Project metadata queries avoid unrelated bodies and bind the native scope."""

from __future__ import annotations

import json
import sqlite3
import unittest
from unittest.mock import patch

from cater.events import Actor
from cater.project_history import TOOL
from cater.web import task_api_payload
import test_team_project_history
import test_simple_team


class ProjectMetadataTests(unittest.TestCase):
    setUp = test_team_project_history.TeamProjectHistoryTests.setUp
    exchange = test_team_project_history.TeamProjectHistoryTests.exchange
    events = test_team_project_history.TeamProjectHistoryTests.events
    publication = test_team_project_history.TeamProjectHistoryTests.publication
    complete = test_team_project_history.TeamProjectHistoryTests.complete

    def test_foreign_frozen_body_and_foreign_goal_are_not_python_metadata(self):
        other = self.rt.rulebook.create_project("另一个项目")["id"]
        foreign = self.rt.create_task("PRIVATE_FOREIGN_GOAL", project_id=other)
        self.rt.team.enable(foreign)
        self.exchange(("publish_artifact", {"filename": "private.txt", "content": "PRIVATE_FROZEN_BODY" * 5000,
            "summary": "PRIVATE_FROZEN_SUMMARY", "format": "text"}), task=foreign)
        self.rt.derive_task(foreign, "冻结私有背景", request_id="foreign-frozen-metadata")
        self.publication(self.source)
        self.complete(self.source)
        original = self.rt.log._row_to_event
        seen, sql = [], []
        def selected(row):
            self.assertNotEqual(row["task_id"], foreign)
            self.assertNotIn(row["type"], {"task.created", "task.project_bound", "project.created", "deliverable.published"})
            seen.append(row["type"])
            return original(row)
        self.rt.log.db.set_trace_callback(sql.append)
        self.addCleanup(self.rt.log.db.set_trace_callback, None)
        with patch.object(self.rt.log, "_row_to_event", side_effect=selected):
            state = self.rt.team.project_history._state(self.rt.log.count())
            index = self.rt.team.project_history.index(self.task)
        self.assertEqual(len(index["entries"]), 1)
        self.assertNotIn("PRIVATE_", json.dumps(state))
        self.assertNotIn("PRIVATE_", json.dumps(index))
        metadata = [query for query in sql if " AS isolated " in query]
        self.assertTrue(metadata)
        self.assertTrue(all("$.goal" not in query and "SELECT *" not in query for query in metadata))
        self.assertIn("team.objective_completed", seen)

    def test_missing_sqlite_json_fails_history_closed_but_regular_chat_still_runs(self):
        def deny_json(action, first, second, database, trigger):
            if action == sqlite3.SQLITE_FUNCTION and str(second).startswith("json_"):
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        self.rt.log.db.set_authorizer(deny_json)
        self.addCleanup(self.rt.log.db.set_authorizer, None)
        result = self.rt.team.project_history.index(self.task)
        self.assertFalse(result["available"])
        self.assertEqual(result["reason"], "metadata_query_unavailable")
        self.assertFalse(self.rt.team.project_history.available(self.task))
        self.assertEqual(self.rt.team.project_history.read(self.task, {"section": "index"})["outcome"], "not_started")
        self.exchange()
        context = self.driver.calls[-1]["context"]
        self.assertEqual(context["project_history"]["reason"], "metadata_query_unavailable")
        self.assertNotIn(TOOL, {tool["name"] for tool in self.driver.calls[-1]["tools"]})
        self.assertEqual(self.events("team.run_finished")[-1].payload["status"], "completed")

    def test_model_context_project_is_checked_before_reading_another_scope(self):
        other = self.rt.rulebook.create_project("另一个项目")["id"]
        foreign = self.rt.create_task("另一个项目的来源", project_id=other)
        self.rt.team.enable(foreign)
        self.publication(foreign, "PRIVATE_OTHER_PROJECT")
        self.complete(foreign)
        def change(context, history, tools):
            self.assertEqual(context["project_history"]["project_id"], self.project)
            self.rt.log.emit(type="task.project_bound", actor=Actor.human(), task_id=self.task, payload={"project_id": other})
            return test_simple_team.turn("", (TOOL, {"section": "index"}))
        result, = self.exchange(step=change)
        self.assertEqual((result["code"], result["outcome"]), ("project_history_scope_changed", "not_started"))
        self.assertNotIn("PRIVATE_", json.dumps(result))
        start = self.events("team.tool_started")[-1]
        self.assertEqual(start.payload["project_scope"], self.project)

    def test_scope_change_between_host_guard_and_catalog_cannot_read_new_project(self):
        other = self.rt.rulebook.create_project("另一个项目")["id"]
        original = self.rt.team.project_history.read
        def changed(task, arguments, **kwargs):
            self.rt.log.emit(type="task.project_bound", actor=Actor.human(), task_id=task, payload={"project_id": other})
            return original(task, arguments, **kwargs)
        with patch.object(self.rt.team.project_history, "read", side_effect=changed):
            result, = self.exchange((TOOL, {"section": "index"}))
        self.assertEqual(result["code"], "project_history_scope_changed")
        self.assertNotIn("entries", result)
        self.assertNotIn("source", result)

    def test_native_detail_suppresses_legacy_memory_but_keeps_frozen_background(self):
        self.publication(self.source)
        self.complete(self.source)
        child = self.rt.derive_task(self.source, "独立冻结分支", request_id="history-native-api-child")["task_id"]
        self.rt.team.enable(child)
        with patch.object(self.rt, "shared_memory", side_effect=AssertionError("legacy memory in native detail")):
            self.assertEqual(task_api_payload(self.rt, self.task)["organization_memory"], [])
            detail = task_api_payload(self.rt, child)
        self.assertEqual(detail["organization_memory"], [])
        self.assertEqual(detail["background"]["source_task_id"], self.source)
        legacy = self.rt.create_task("旧演示入口", project_id=self.project)
        with patch.object(self.rt, "shared_memory", return_value=[{"seq": 1}]) as old:
            self.assertEqual(task_api_payload(self.rt, legacy)["organization_memory"], [{"seq": 1}])
            old.assert_called_once_with(exclude_task_id=legacy)

    def test_nul_suffixed_keys_cannot_place_unbound_task_in_project_history(self):
        self.publication(self.source)
        self.complete(self.source)
        self.assertTrue(self.rt.team.project_history.index(self.task)["available"])
        special = "unbound-special-key"
        self.rt.log.emit(type="task.created", actor=Actor.human(), task_id=special,
                         payload={"goal": "未关联项目", "project_id\0alias": self.project})
        self.assertIsNone(self.rt.rulebook.task_project(special))
        for task in (special, self.task):
            with self.subTest(task=task):
                result = self.rt.team.project_history.index(task)
                self.assertFalse(result["available"])
                self.assertEqual(result["reason"], "metadata_query_unavailable")
                self.assertNotIn("entries", result)
        self.assertTrue(self.rt.verify_chain().ok)

    def test_ambiguous_later_binding_does_not_fall_back_to_previous_project_scope(self):
        other = self.rt.rulebook.create_project("另一个项目")["id"]
        self.rt.log.emit(type="task.project_bound", actor=Actor.human(), task_id=self.task,
                         payload={"project_id": other, "project_id\0alias": self.project})
        self.assertEqual(self.rt.rulebook.task_project(self.task), other)
        result = self.rt.team.project_history.read(self.task, {"section": "index"},
                                                   expected_project_id=self.project)
        self.assertEqual(result["outcome"], "not_started")
        self.assertNotIn("entries", result)
        self.assertNotIn("source", result)
        self.assertTrue(self.rt.verify_chain().ok)

    def test_raw_duplicate_metadata_keys_are_unavailable_not_first_value_wins(self):
        # Only this temporary ledger models imported or corrupted raw JSON.
        self.rt.log.db.execute("INSERT INTO events "
            "(id, ts, task_id, actor_json, type, category, payload_json, prev_hash, hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("raw-duplicate-id", "2026-10-17T00:00:00+00:00", "raw-duplicate",
             json.dumps(Actor.human().to_dict()), "task.created", "lifecycle",
             '{"goal":"历史样本","project_id":"first","project\\u005fid":"last"}',
             self.rt.log.head_hash(), "f" * 64))
        self.rt.log.db.commit()
        self.assertEqual(self.rt.rulebook.task_project("raw-duplicate"), "last")
        result = self.rt.team.project_history.index(self.task)
        self.assertFalse(result["available"])
        self.assertEqual(result["reason"], "metadata_query_unavailable")
        self.assertNotIn("entries", result)


if __name__ == "__main__":
    unittest.main()
