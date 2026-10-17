"""One recovery read must not combine facts from different event heads."""

from __future__ import annotations

import unittest
import sqlite3
import threading
from unittest.mock import patch

from cater.runtime import Runtime
from cater.events import EventLog
from cater.team import HOST
import test_simple_team


class CheckpointConsistencyTests(unittest.TestCase):
    def setUp(self):
        test_simple_team.SimpleTeamTests.setUp(self)
        # Allow deterministic second-connection commits inside the reader's snapshot.
        # Production journal mode is not changed by the recovery implementation.
        self.rt.log.db.execute("PRAGMA journal_mode=WAL")

    def other(self):
        runtime = Runtime(self.rt.log.path, roles_path=self.roles,
                          drivers={"team_test": self.driver}, close_drivers_on_close=False)
        self.addCleanup(runtime.close)
        return runtime

    def assignment(self):
        return self.rt.team._send(self.task, self.rt.organization.leader,
                                  self.worker, "核对当前工程并返回结果", "assignment")

    def test_page_and_summary_agree_when_another_connection_returns_work(self):
        source = self.assignment()
        other = self.other()
        assignments = self.rt.team.recovery.assignments
        calls = []

        def return_after_read(*args, **kwargs):
            rows = assignments(*args, **kwargs)
            calls.append(rows)
            if len(calls) == 1:
                other.team._send(self.task, other.team._role(self.worker), self.manager,
                                 "已核对，仍需后续运行检查。", "report", reply_to=source.seq)
            return rows

        with patch.object(self.rt.team.recovery, "assignments", side_effect=return_after_read):
            result = self.rt.team.recovery.read(self.task, {})
        self.assertEqual([row["seq"] for row in result["assignments"]], [source.seq])
        self.assertEqual(result["checkpoint"]["open_work"][0]["seq"], source.seq)
        self.assertEqual(result["checkpoint"]["open_work_count"], 1)
        self.assertFalse(result["has_more"])
        self.assertEqual(self.rt.team.recovery.read(self.task, {})["assignments"], [])
        self.assertEqual(self.driver.calls, [])

    def test_new_goal_during_scope_is_not_attached_to_old_as_of(self):
        self.assignment()
        other = self.other()
        scope = self.rt.team.recovery.scope
        changes = []
        before = self.rt.log.read(task_id=self.task)[-1]

        def change_goal(*args, **kwargs):
            if not changes:
                changes.append(other.log.emit(type="team.objective_updated", task_id=self.task,
                    actor=HOST, payload={"goal": "新目标：只整理运行方法"}))
            return scope(*args, **kwargs)

        with patch.object(self.rt.team.recovery, "scope", side_effect=change_goal):
            result = self.rt.team.recovery.checkpoint(self.task)
        self.assertEqual(result["objective"], "制作 doc 转 PDF 工具")
        self.assertEqual(result["as_of_seq"], before.seq)
        self.assertEqual(result["as_of_hash"], before.hash)
        self.assertEqual(result["scope"]["revision"], 0)
        self.assertEqual(result["open_work_count"], 1)
        fresh = self.rt.team.recovery.checkpoint(self.task)
        self.assertEqual(fresh["objective"], changes[0].payload["goal"])
        self.assertEqual(fresh["scope"]["revision"], changes[0].seq)
        self.assertEqual(fresh["open_work_count"], 0)

    def test_control_change_keeps_one_snapshot_without_resuming_or_writing(self):
        other = self.other()
        scope = self.rt.team.recovery.scope
        calls = []

        def pause_after_scope(*args, **kwargs):
            value = scope(*args, **kwargs)
            calls.append(value)
            if len(calls) == 1:
                other.execution_control.change(self.task, "pause", request_id="pause-during-checkpoint")
            return value

        before = self.rt.log.db.total_changes
        with patch.object(self.rt.team.recovery, "scope", side_effect=pause_after_scope):
            result = self.rt.team.recovery.read(self.task, {})
        self.assertEqual(result["checkpoint"]["scope"]["control_state"], "running")
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.rt.team.recovery.checkpoint(self.task)["scope"]["control_state"], "paused")
        self.assertEqual(self.rt.log.db.total_changes, before)
        self.assertEqual(self.driver.calls, [])

    def test_other_task_append_does_not_interrupt_the_observation(self):
        other = self.other()
        another = other.create_task("独立的其他工作")
        scope = self.rt.team.recovery.scope
        calls = []

        def append_elsewhere(*args, **kwargs):
            value = scope(*args, **kwargs)
            calls.append(value)
            if len(calls) == 1:
                other.log.emit(type="task.progress_reported", task_id=another, actor=HOST,
                               payload={"text": "其他群共享资源发生变化"})
            return value

        task_head = self.rt.log.read(task_id=self.task)[-1]
        with patch.object(self.rt.team.recovery, "scope", side_effect=append_elsewhere):
            result = self.rt.team.recovery.checkpoint(self.task)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["as_of_seq"], task_head.seq)
        self.assertEqual(result["as_of_hash"], task_head.hash)

    def test_concurrent_append_does_not_restart_a_complete_read(self):
        other = self.other()
        scope = self.rt.team.recovery.scope
        calls = []

        def keep_changing(*args, **kwargs):
            value = scope(*args, **kwargs)
            calls.append(value)
            other.log.emit(type="task.progress_reported", task_id=self.task, actor=HOST,
                           payload={"text": "并发更新"})
            return value

        before = self.rt.log.read(task_id=self.task)[-1]
        with patch.object(self.rt.team.recovery, "scope", side_effect=keep_changing):
            result = self.rt.team.recovery.read(self.task, {})
        self.assertEqual(result["checkpoint"]["as_of_hash"], before.hash)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.driver.calls, [])
        self.assertFalse(self.rt.log.db.in_transaction)

    def test_workspace_rule_change_is_not_hidden_by_an_unchanged_task_head(self):
        other = self.other()
        scope = self.rt.team.recovery.scope
        calls = []

        def add_global_rule(*args, **kwargs):
            value = scope(*args, **kwargs)
            calls.append(value)
            if len(calls) == 1:
                other.rulebook.record("保留已确认成果，先核对后续做。", scope="workspace")
            return value

        task_head = self.rt.log.read(task_id=self.task)[-1]
        with patch.object(self.rt.team.recovery, "scope", side_effect=add_global_rule):
            result = self.rt.team.recovery.checkpoint(self.task)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["rule_fingerprint"], result["scope"]["rule_fingerprint"])
        self.assertNotEqual(result["scope"]["rule_fingerprint"], scope(self.task)["rule_fingerprint"])
        self.assertEqual(result["as_of_hash"], task_head.hash)
        self.assertEqual(self.driver.calls, [])

    def test_pending_operation_count_never_includes_a_start_newer_than_as_of(self):
        source = self.assignment()
        _, _, run = self.rt.team._claim(self.task)
        other = self.other()
        scope = self.rt.team.recovery.scope
        starts = []
        before = self.rt.log.read(task_id=self.task)[-1]

        def start_operation(*args, **kwargs):
            value = scope(*args, **kwargs)
            if not starts:
                starts.append(other.log.emit(type="team.tool_started", task_id=self.task, actor=HOST,
                    payload={**run.payload, "name": "publish_artifact", "call_id": "interleaved-publication",
                             "arguments": {"filename": "guide.md"}}))
            return value

        with patch.object(self.rt.team.recovery, "scope", side_effect=start_operation):
            result = self.rt.team.recovery.read(self.task, {})
        self.assertEqual(result["checkpoint"]["pending_operation_count"], 0)
        self.assertEqual(result["checkpoint"]["as_of_hash"], before.hash)
        fresh = self.rt.team.recovery.checkpoint(self.task)
        self.assertEqual(fresh["pending_operation_count"], 1)
        self.assertEqual(fresh["as_of_seq"], starts[0].seq)
        self.assertEqual(fresh["as_of_hash"], starts[0].hash)
        self.assertEqual(result["assignments"][0]["seq"], source.seq)
        self.assertEqual(result["assignments"][0]["status"], "running")
        self.assertEqual(self.driver.calls, [])

    def test_stable_page_is_derived_once_and_does_not_write(self):
        self.assignment()
        recovery = self.rt.team.recovery
        before = self.rt.log.head_hash(), self.rt.log.db.total_changes
        with patch.object(recovery, "assignments", wraps=recovery.assignments) as assignments:
            result = recovery.read(self.task, {"limit": 1})
        self.assertEqual(assignments.call_count, 1)
        self.assertEqual(result["checkpoint"]["open_work_count"], 1)
        self.assertEqual(result["checkpoint"]["open_work"][0]["seq"], result["assignments"][0]["seq"])
        self.assertEqual((self.rt.log.head_hash(), self.rt.log.db.total_changes), before)
        self.assertFalse(self.rt.log.db.in_transaction)

    def test_capture_uses_only_read_statements_and_releases_its_transaction(self):
        actions = []
        allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION,
                   sqlite3.SQLITE_RECURSIVE, sqlite3.SQLITE_TRANSACTION}

        def authorize(action, first, second, database, source):
            actions.append((action, first))
            return sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY

        self.rt.log.db.set_authorizer(authorize)
        try:
            self.rt.team.recovery.read(self.task, {})
        finally:
            self.rt.log.db.set_authorizer(None)
        self.assertEqual([value for action, value in actions if action == sqlite3.SQLITE_TRANSACTION],
                         ["BEGIN", "ROLLBACK"])
        self.assertFalse(self.rt.log.db.in_transaction)

    def test_outer_transaction_is_not_committed_or_rolled_back_by_read(self):
        before = self.rt.log.head_hash()
        with self.assertRaisesRegex(RuntimeError, "rollback fixture"):
            with self.rt.log.atomic():
                self.assignment()
                result = self.rt.team.recovery.read(self.task, {})
                self.assertEqual(result["checkpoint"]["open_work_count"], 1)
                self.assertTrue(self.rt.log.db.in_transaction)
                raise RuntimeError("rollback fixture")
        self.assertEqual(self.rt.log.head_hash(), before)

    def test_read_failure_is_not_retried_or_replaced_by_a_conflict(self):
        failure = RuntimeError("fixture read failure")
        with patch.object(self.rt.team.recovery, "scope", side_effect=failure) as scope:
            with self.assertRaises(RuntimeError) as caught:
                self.rt.team.recovery.checkpoint(self.task)
        self.assertIs(caught.exception, failure)
        self.assertEqual(scope.call_count, 1)
        self.assertFalse(self.rt.log.db.in_transaction)

    def test_delete_journal_reader_allows_writer_reservation_and_releases_commit(self):
        self.rt.log.db.execute("PRAGMA journal_mode=DELETE")
        ready, begin, reserved, finished = (threading.Event() for _ in range(4))
        errors = []
        path = self.rt.log.path

        def write():
            log = None
            try:
                log = EventLog(path)
                ready.set()
                if not begin.wait(10):
                    raise RuntimeError("reader did not start")
                log.db.execute("BEGIN IMMEDIATE")
                reserved.set()
                log.emit(type="task.progress_reported", task_id=self.task, actor=HOST,
                         payload={"text": "读快照期间提交的更新"})
                log.db.commit()
            except BaseException as exc:
                errors.append(exc)
                reserved.set()
            finally:
                if log is not None:
                    log.close()
                finished.set()

        writer = threading.Thread(target=write)
        writer.start()
        scope = self.rt.team.recovery.scope

        def observe_reserved_writer(*args, **kwargs):
            begin.set()
            self.assertTrue(reserved.wait(10))
            self.assertEqual(errors, [])
            # BEGIN IMMEDIATE succeeded elsewhere: this reader has no reserved write lock.
            self.assertFalse(finished.is_set())
            return scope(*args, **kwargs)

        try:
            self.assertTrue(ready.wait(10))
            before = self.rt.log.read(task_id=self.task)[-1]
            with patch.object(self.rt.team.recovery, "scope", side_effect=observe_reserved_writer):
                result = self.rt.team.recovery.checkpoint(self.task)
            self.assertEqual(result["as_of_hash"], before.hash)
            self.assertFalse(self.rt.log.db.in_transaction)
        finally:
            begin.set()
            writer.join(10)
        self.assertFalse(writer.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(finished.is_set())
        self.assertGreater(self.rt.team.recovery.checkpoint(self.task)["as_of_seq"], before.seq)

    def test_outer_deferred_snapshot_remains_owned_by_the_caller(self):
        other = self.other()
        self.rt.log.db.execute("BEGIN")
        try:
            before = self.rt.team.recovery.checkpoint(self.task)
            other.execution_control.change(self.task, "pause", request_id="pause-outer-read-snapshot")
            result = self.rt.team.recovery.checkpoint(self.task)
            self.assertEqual(result, before)
            self.assertTrue(self.rt.log.db.in_transaction)
        finally:
            self.rt.log.db.rollback()
        self.assertEqual(self.rt.team.recovery.checkpoint(self.task)["scope"]["control_state"], "paused")


if __name__ == "__main__":
    unittest.main()
