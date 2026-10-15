from __future__ import annotations

import io
import json
import tempfile
import unittest
from email.message import Message
from http.client import HTTPResponse
from pathlib import Path
from unittest.mock import patch

from cater.events import Actor, EventLog
from cater.runtime import Runtime
from cater.web import ActivityRunner, make_handler, task_api_payload


class MemorySocket:
    def __init__(self, raw):
        self.raw = raw

    def makefile(self, *args):
        return io.BytesIO(self.raw)


class TaskSummaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "summaries.db"
        self.factory = lambda: Runtime(self.db)
        self.runner = ActivityRunner(self.factory)
        with patch("cater.web.ActivityRunner", return_value=self.runner):
            self.handler = make_handler(self.db, access_token="fixture-token", runtime_factory=self.factory)

    def get(self, etag=None, token="fixture-token", path="/api/task-summaries"):
        handler = self.handler.__new__(self.handler)
        handler.path = path
        handler.command = "GET"
        handler.request_version = "HTTP/1.1"
        handler.requestline = f"GET {path} HTTP/1.1"
        handler.headers = Message()
        if token is not None:
            handler.headers["X-Cater-Access-Token"] = token
        if etag is not None:
            handler.headers["If-None-Match"] = etag
        handler.wfile = io.BytesIO()
        handler.do_GET()
        reply = HTTPResponse(MemorySocket(handler.wfile.getvalue()))
        reply.begin()
        return reply.status, reply.headers, reply.read()

    def rows(self, etag=None):
        status, headers, raw = self.get(etag)
        self.assertEqual(status, 200)
        return json.loads(raw)["tasks"], headers["ETag"]

    def test_empty_then_other_window_create_and_revalidate_without_history(self):
        rows, empty_tag = self.rows()
        self.assertEqual(rows, [])
        with self.factory() as rt:
            task = rt.create_task("other window")
        rows, tag = self.rows(empty_tag)
        self.assertNotEqual(tag, empty_tag)
        self.assertEqual(rows[0]["id"], task)
        self.assertEqual(rows[0]["conversation_count"], 1)
        with patch.object(Runtime, "tasks", side_effect=AssertionError("fold on 304")), \
                patch.object(EventLog, "_row_to_event", side_effect=AssertionError("history on 304")):
            for condition in (tag, f'"other", {tag.removeprefix("W/")}', "*"):
                status, headers, raw = self.get(condition)
                self.assertEqual(status, 304)
                self.assertEqual(headers["ETag"], tag)
                self.assertEqual(raw, b"")
                self.assertEqual(headers["Cache-Control"], "private, no-cache")
            # An unconditional second window also reuses the small row cache.
            self.assertEqual(self.rows()[0], rows)

    def test_semantic_receipts_do_not_duplicate_visible_message_counts(self):
        with self.factory() as rt:
            task = rt.create_task("状态查询")
            received = rt.log.emit(type="conversation.received", actor=Actor.human(), task_id=task,
                                   payload={"instruction": "继需了吗"})
            progress = rt.report_progress(task, command_seq=received.seq)
            rt.log.emit(type="conversation.finished", actor=Actor.system("conversation"), task_id=task,
                        payload={"conversation_seq": received.seq, "action": "status_task", "progress_seq": progress.seq})
            second = rt.log.emit(type="conversation.received", actor=Actor.human(), task_id=task,
                                 payload={"instruction": "继续"})
            rt.log.emit(type="human.commanded", actor=Actor.human(), task_id=task,
                        payload={"conversation_seq": second.seq, "instruction": "继续", "intent": "resume"})
            rt.log.emit(type="conversation.finished", actor=Actor.system("conversation"), task_id=task,
                        payload={"conversation_seq": second.seq, "action": "resume_task"})
            self.assertEqual(task_api_payload(rt, task)["summary"]["conversation_count"], 5)
        self.assertEqual(self.rows()[0][0]["conversation_count"], 5)

    def test_access_token_is_checked_before_etag_or_database(self):
        _, tag = self.rows()
        with patch.object(self.handler, "_runtime", side_effect=AssertionError("unauthorized read")):
            for token in (None, "wrong-token"):
                status, headers, raw = self.get(tag, token=token)
                self.assertEqual(status, 401)
                self.assertNotIn("ETag", headers)
                self.assertEqual(raw, b"access token is required")

    def test_counts_visible_human_and_organization_records_but_not_receipts(self):
        with self.factory() as rt:
            task = rt.create_task("conversation")
            visible = ["human.commanded", "task.progress_reported", "planning.started", "planning.finished",
                       "delivery.review_started", "delivery.review_finished", "task.interpreted",
                       "cabinet.convened", "task.decomposed", "plan.amended", "msg.said", "msg.challenge",
                       "rule.recorded", "rule.updated", "diagnosis.recorded", "deliverable.published",
                       "handoff.published", "verification.completed", "cabinet.voted",
                       "agent.discipline_flagged", "driver.budget_exhausted", "driver.failed"]
            for kind in visible:
                rt.log.emit(actor=Actor.system(), task_id=task, type=kind, payload={"content": "PRIVATE_BODY"})
            for phase in ("planning", "delivery_receipt"):
                rt.log.emit(actor=Actor.system(), task_id=task, type="msg.said", payload={"phase": phase})
            for kind in ("task.routed", "mission.created", "subtask.assigned", "agent.working", "plan.proposed",
                         "review.generated", "activity.completed"):
                rt.log.emit(actor=Actor.system(), task_id=task, type=kind)
            expected = 1 + len(visible)
            self.assertEqual(task_api_payload(rt, task)["summary"]["conversation_count"], expected)
        rows, _ = self.rows()
        self.assertEqual(rows[0]["conversation_count"], expected)
        self.assertEqual(set(rows[0]), {"id", "goal", "project_id", "status", "status_label",
                                      "conversation_count", "latest_seq", "version", "evaluated_at", "activity"})
        self.assertNotIn("PRIVATE_BODY", json.dumps(rows))

    def test_queue_and_brief_suppression_matches_chat_and_can_reduce_count(self):
        with self.factory() as rt:
            task = rt.create_task("conditional bubbles")
            for kind in ("activity.queued", "brief.generated"):
                rt.log.emit(actor=Actor.system(), task_id=task, type=kind)
        rows, tag = self.rows()
        self.assertEqual(rows[0]["conversation_count"], 3)
        with self.factory() as rt:
            rt.log.emit(actor=Actor.system(), task_id=task, type="agent.decided")
            rt.log.emit(actor=Actor.system(), task_id=task, type="activity.completed")
        rows, tag = self.rows(tag)
        self.assertEqual(rows[0]["conversation_count"], 2)
        with self.factory() as rt:
            rt.log.emit(actor=Actor.system(), task_id=task, type="activity.failed", payload={"message": "failed"})
        self.assertEqual(self.rows(tag)[0][0]["conversation_count"], 3)

    def test_activity_only_changes_etag_and_does_not_refold(self):
        with self.factory() as rt:
            task = rt.create_task("background")
        with patch.object(self.runner, "_start"):
            queued = self.runner.submit(task, "run")
        rows, tag = self.rows()
        self.assertEqual(rows[0]["status_label"], "协作已排队")
        self.runner._mark_running(task, queued["id"])
        with patch.object(Runtime, "tasks", side_effect=AssertionError("activity refold")):
            rows, running_tag = self.rows(tag)
        self.assertNotEqual(tag, running_tag)
        self.assertEqual(rows[0]["status_label"], "正在协作")
        with patch.object(self.runner, "_start"):
            self.runner.continue_after_current(task, "run")
            snapshot = self.runner.snapshot()[1]
            self.runner.continue_after_current(task, "battle")
        self.assertEqual(snapshot[task]["follow_up"]["action"], "run")
        rows, tag = self.rows(running_tag)
        self.assertEqual(rows[0]["activity"]["follow_up"]["action"], "battle")
        self.runner._finish(task, queued["id"], "failed", error="PRIVATE_ERROR")
        rows, _ = self.rows(tag)
        self.assertFalse(rows[0]["activity"]["active"])
        self.assertNotIn("PRIVATE_ERROR", json.dumps(rows))

    def test_real_delivery_completion_and_requirement_change_revoke_list_status(self):
        from test_delivery import DeliveryDriver, DocumentWorkDriver
        from test_work import item

        config = json.loads(Path("cater/packs/default_roles.json").read_text(encoding="utf-8"))
        config["organization"]["leader"]["driver"] = "final"
        roles = Path(self.tmp.name) / "roles.json"
        roles.write_text(json.dumps(config), encoding="utf-8")
        worker, final = DocumentWorkDriver(), DeliveryDriver()
        drivers = {**{name: worker for name in ("product", "design", "dev", "test")}, "final": final}
        self.factory = lambda: Runtime(self.db, roles_path=roles, drivers=drivers)
        self.handler = make_handler(self.db, runtime_factory=self.factory, access_token="fixture-token")
        with self.factory() as rt:
            task = rt.create_task("deliver a document")
            rt.plan_work(task, [item("a")])
            rt.collaborate(task)
        rows, tag = self.rows()
        self.assertEqual(rows[0]["status"], "done")
        self.assertEqual(rows[0]["status_label"], "已交付")
        self.assertNotIn("manifest", json.dumps(rows))
        with self.factory() as rt:
            rt.command(task, "add another requirement", goal="revised document")
            expected = task_api_payload(rt, task)["summary"]
        rows, new_tag = self.rows(tag)
        self.assertNotEqual(tag, new_tag)
        self.assertEqual(rows[0]["status"], "running")
        self.assertEqual(rows[0]["goal"], "revised document")
        self.assertEqual(rows[0]["status_label"], "需要重新核对")
        self.assertEqual(rows[0]["conversation_count"], expected["conversation_count"])
        self.assertEqual(rows[0]["version"], expected["version"])

    def test_lease_expiry_invalidates_etag_without_a_new_event(self):
        from test_work import item

        with self.factory() as rt:
            task = rt.create_task("lease")
            rt.plan_work(task, [item("a")])
            with patch("cater.work.time.time", return_value=100):
                rt.work.claim(task, "a", lease_seconds=10)
        with patch("cater.web.time.time", return_value=105):
            rows, tag = self.rows()
            version = rows[0]["version"]
            self.assertEqual(rows[0]["status_label"], "待补充成果")
            self.assertEqual(self.get(tag)[0], 304)
        with patch("cater.web.time.time", return_value=111):
            rows, new_tag = self.rows(tag)
            self.assertNotEqual(tag, new_tag)
            self.assertEqual(rows[0]["version"], version)
            self.assertEqual(rows[0]["status_label"], "结果待核实")
            self.assertEqual(self.get(new_tag)[0], 304)


if __name__ == "__main__":
    unittest.main()
