from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

from cater.drivers import CliDriver
from cater.events import Actor
from cater.runtime import Runtime
from cater.work import WorkConflict
from test_conversation_snapshots import RecordingDriver
from test_delivery import DeliveryDriver, DocumentWorkDriver
from test_work import WorkDriver, item


class RoomTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "rooms.db"
        self.rt = Runtime(self.db, audit_key="test-key")
        self.addCleanup(self.rt.close)
        self.room = self.rt.rooms.create("讨论室", request_id="create-room-001")["room"]["id"]

    def message(self, text="先聊聊，不执行", *, key="message-request-001", room=None, **kwargs):
        return self.rt.rooms.receive(room or self.room, text, request_id=key, **kwargs)["message"]

    def task(self, goal="独立工作", *, key="start-request-001", message=None, room=None, project=None):
        message = message or self.message()
        return self.rt.rooms.start_task(room or self.room, message_id=message.id, goal=goal,
                                        acceptance=["可检查的结果"], project_id=project,
                                        request_id=key)["task_id"]

    def test_group_and_messages_never_create_phantom_tasks(self):
        original = "  @店长 聊聊系统架构，先别做\n保留原文  "
        message = self.message(original)
        self.assertIsNone(message.task_id)
        self.assertEqual(message.payload["text"], original)
        self.assertEqual(message.actor, Actor.human())
        self.assertEqual(self.rt.tasks(), [])
        count = self.rt.log.count()
        self.assertEqual(self.rt.rooms.get(self.room)["task_ids"], [])
        self.assertEqual(self.rt.rooms.timeline(self.room)["events"], [message])
        self.assertEqual(self.rt.log.count(), count)
        self.assertTrue(self.rt.verify_chain().ok)

    def test_receive_and_create_are_idempotent_across_restart(self):
        message = self.message()
        count = self.rt.log.count()
        with Runtime(self.db, audit_key="test-key") as reopened:
            room = reopened.rooms.create("讨论室", request_id="create-room-001")
            self.assertFalse(room["created"])
            self.assertEqual(room["room"]["id"], self.room)
            replay = reopened.rooms.receive(self.room, message.payload["text"], request_id="message-request-001")
            self.assertFalse(replay["created"])
            self.assertEqual(replay["message"], message)
            with self.assertRaises(WorkConflict):
                reopened.rooms.receive(self.room, "changed", request_id="message-request-001")
            with self.assertRaises(WorkConflict):
                reopened.rooms.create("renamed", request_id="create-room-001")
            self.assertEqual(reopened.log.count(), count)

    def test_two_tasks_and_chat_do_not_mutate_each_others_scope(self):
        first = self.task()
        self.rt.plan_work(first, [item("first")])
        before = self.rt.log.read(task_id=first)
        scope = self.rt.work.scope(first)
        second_message = self.message("另外做一张海报", key="message-request-002")
        second = self.task("海报", key="start-request-002", message=second_message)
        for index, text in enumerate(("为什么这么改", "暂停", "@米寇 继续", "改成另一种风格", "先聊天")):
            self.message(text, key=f"chat-message-{index}", task_id=first)
        self.assertEqual(self.rt.work.scope(first), scope)
        self.assertEqual(self.rt.log.read(task_id=first), before)
        self.assertEqual(self.rt.rooms.get(self.room)["task_ids"], [first, second])
        self.assertEqual(self.rt.task(second).goal, "海报")
        self.assertEqual(self.rt.task(second).status, "pending")
        self.assertEqual(self.rt.task(second).assignments, [])
        self.assertEqual(self.rt.task(second).messages, [])

    def test_task_creation_and_membership_are_atomic_on_storage_failure(self):
        message = self.message()
        count = self.rt.log.count()
        self.rt.log.db.execute("CREATE TEMP TRIGGER reject_membership BEFORE INSERT ON events "
                               "WHEN NEW.type = 'room.task_attached' BEGIN "
                               "SELECT RAISE(ABORT, 'simulated failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.task(message=message)
        self.assertEqual(self.rt.log.count(), count)
        self.assertEqual(self.rt.tasks(), [])
        self.assertEqual(self.rt.rooms.get(self.room)["task_ids"], [])
        self.rt.log.db.execute("DROP TRIGGER reject_membership")
        created = self.task(message=message)
        self.assertEqual(self.rt.rooms.get(self.room)["task_ids"], [created])
        self.assertTrue(self.rt.verify_chain().ok)

    def test_creation_retry_and_conflicting_reuse_do_not_duplicate_work(self):
        message = self.message()
        task = self.task(message=message)
        count = self.rt.log.count()
        with Runtime(self.db, audit_key="test-key") as reopened:
            body = {"message_id": message.id, "goal": "独立工作", "acceptance": ["可检查的结果"],
                    "request_id": "start-request-001"}
            self.assertEqual(reopened.rooms.start_task(self.room, **body), {"task_id": task, "created": False})
            for field, value in (("goal", "changed"), ("acceptance", []), ("message_id", "unknown")):
                with self.subTest(field=field), self.assertRaises(WorkConflict):
                    reopened.rooms.start_task(self.room, **{**body, field: value})
            self.assertEqual(reopened.log.count(), count)

    def test_racing_duplicate_requests_produce_one_message_and_one_task(self):
        barrier = Barrier(2)

        def send():
            with Runtime(self.db, audit_key="test-key") as rt:
                barrier.wait(timeout=5)
                message = rt.rooms.receive(self.room, "work", request_id="racing-message-001")["message"]
                return rt.rooms.start_task(self.room, message_id=message.id, goal="work", request_id="racing-task-001")

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(send) for _ in range(2)]
            results = [f.result(timeout=15) for f in futures]
        self.assertEqual(len({r["task_id"] for r in results}), 1)
        self.assertEqual(sum(r["created"] for r in results), 1)
        self.assertEqual(len(list(self.rt.log.iter_events(event_types=("room.message_received",)))), 1)
        self.assertEqual(len(self.rt.tasks()), 1)
        self.assertTrue(self.rt.verify_chain().ok)

    def test_attach_rejects_multiple_groups_without_changing_task(self):
        task = self.rt.create_task("Legacy")
        original = self.rt.log.read(task_id=task)
        self.assertTrue(self.rt.rooms.attach(self.room, task)["created"])
        self.assertFalse(self.rt.rooms.attach(self.room, task)["created"])
        other = self.rt.rooms.create("Other", request_id="create-room-002")["room"]["id"]
        with self.assertRaises(WorkConflict):
            self.rt.rooms.attach(other, task)
        self.assertEqual(self.rt.log.read(task_id=task), original)
        self.assertFalse(self.rt.task_context_isolated(task))

    def test_message_and_reply_membership_are_checked_without_scope_effects(self):
        task = self.task()
        other = self.rt.rooms.create("Other", request_id="create-room-002")["room"]["id"]
        foreign = self.message(room=other, key="foreign-message-001")
        own = self.message(task_id=task, key="own-message-001")
        second = self.task("Second", key="start-request-002")
        count = self.rt.log.count()
        for kwargs in ({"reply_to": foreign.seq}, {"reply_to": True}, {"reply_to": 99999},
                       {"task_id": "unknown"}, {"task_id": []}, {"reply_to": own.seq, "task_id": second}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.message(key="invalid-message", **kwargs)
        with self.assertRaises(ValueError):
            self.task(message=foreign, key="invalid-source-task")
        self.assertEqual(self.rt.log.count(), count)
        reply = self.message(key="valid-reply-001", reply_to=own.seq, task_id=task)
        self.assertEqual(reply.payload["reply_to"], own.seq)

    def test_timeline_is_paginated_chronological_and_does_not_leak_other_groups(self):
        task = self.task()
        other = self.rt.rooms.create("Other", request_id="create-room-002")["room"]["id"]
        self.message("SECRET", room=other, key="foreign-message-001")
        self.rt.log.emit(actor=Actor.agent("dev", "worker"), type="msg.said", task_id=task,
                         payload={"text": "result"})
        last = self.message("After work", key="last-message-001")
        events = []
        cursor, membership = 0, None
        while True:
            page = self.rt.rooms.timeline(self.room, after_seq=cursor, membership_seq=membership, limit=2)
            events.extend(page["events"])
            cursor, membership = page["next_after_seq"], page["membership_seq"]
            if not page["has_more"]:
                break
        self.assertEqual(events[-1], last)
        self.assertEqual([e.seq for e in events], sorted({e.seq for e in events}))
        self.assertEqual(len(events), 4)
        self.assertNotIn("SECRET", repr(events))
        count = self.rt.log.count()
        empty = self.rt.rooms.timeline(self.room, after_seq=cursor, membership_seq=membership)
        self.assertEqual(empty["events"], [])
        self.assertEqual(empty["next_after_seq"], cursor)
        self.assertEqual(self.rt.log.count(), count)

    def test_new_membership_invalidates_cursor_so_older_history_cannot_be_missed(self):
        legacy = self.rt.create_task("Older than the cursor")
        self.message()
        page = self.rt.rooms.timeline(self.room)
        self.rt.rooms.attach(self.room, legacy)
        with self.assertRaises(WorkConflict):
            self.rt.rooms.timeline(self.room, after_seq=page["next_after_seq"], membership_seq=page["membership_seq"])
        self.assertTrue(any(e.task_id == legacy for e in self.rt.rooms.timeline(self.room)["events"]))

    def test_import_is_explicit_restartable_and_preserves_historical_hashes(self):
        old = [self.rt.create_task(f"Old {i}") for i in range(2)]
        original = self.rt.log.read()
        self.rt.rooms.list()
        self.assertEqual(self.rt.log.read(), original)
        self.assertEqual(self.rt.rooms.import_legacy(), {"created": 2})
        count = self.rt.log.count()
        with Runtime(self.db, audit_key="test-key") as reopened:
            self.assertEqual(reopened.rooms.import_legacy(), {"created": 0})
            self.assertEqual(reopened.log.count(), count)
            self.assertEqual(reopened.log.read()[:len(original)], original)
            self.assertEqual(sorted(t for r in reopened.rooms.list() for t in r["task_ids"]), sorted(old))
            self.assertTrue(reopened.verify_chain().ok)

    def test_unrelated_event_between_read_and_write_is_retried(self):
        message = self.message()
        emit_many = self.rt.log.emit_many
        calls = []

        def race(entries, **kwargs):
            if not calls:
                calls.append(True)
                self.rt.log.emit(actor=Actor.system(), type="review.generated", payload={"unrelated": True})
            return emit_many(entries, **kwargs)

        with patch.object(self.rt.log, "emit_many", side_effect=race):
            task = self.task(message=message)
        self.assertEqual(len(self.rt.tasks()), 1)
        self.assertEqual(self.rt.rooms.get(self.room)["task_ids"], [task])
        self.assertTrue(self.rt.verify_chain().ok)

    def test_starting_new_work_during_an_active_call_does_not_discard_its_result(self):
        first = self.task("FIRST_GOAL")
        self.rt.plan_work(first, [item("first-work")])
        scope = self.rt.work.scope(first)
        worker = WorkDriver()
        self.rt.drivers.update({name: worker for name in ("product", "design", "dev", "test")})
        created = []

        def during_call(work):
            if created:
                return
            message = self.message("另外开始另一项工作", key="during-call-message")
            created.append(self.task("SECOND_GOAL", key="during-call-task", message=message))
            self.message("先聊聊需求", key="during-call-discuss", task_id=first)

        worker.after = during_call
        self.rt.collaborate(first)
        self.assertEqual(len(worker.calls), 2)
        self.assertEqual(self.rt.work.scope(first), scope)
        self.assertTrue(self.rt.work.view(first)["stage_accepted"])
        self.assertEqual(self.rt.work.view(created[0])["items"], [])
        self.assertEqual(self.rt.task(first).goal, "FIRST_GOAL")
        self.assertEqual(self.rt.task(created[0]).goal, "SECOND_GOAL")

    def test_completed_delivery_survives_later_discussion_and_another_task(self):
        config = json.loads(Path("cater/packs/default_roles.json").read_text(encoding="utf-8"))
        config["organization"]["leader"]["driver"] = "final"
        roles = Path(self.tmp.name) / "delivery-roles.json"
        roles.write_text(json.dumps(config), encoding="utf-8")
        worker, final = DocumentWorkDriver(), DeliveryDriver()
        drivers = {**{name: worker for name in ("product", "design", "dev", "test")}, "final": final}
        first = self.task("完成一份说明")
        with Runtime(self.db, roles_path=roles, drivers=drivers, audit_key="test-key") as rt:
            rt.plan_work(first, [item("document")])
            rt.collaborate(first)
            delivery = rt.delivery.view(first)
            self.assertEqual(delivery["status"], "accepted")
            self.assertEqual(rt.task(first).status, "done")
            original = rt.log.read(task_id=first)
            self.message("后面再讨论一下别的方向", key="after-delivery-message", task_id=first)
            self.task("另一个目标", key="after-delivery-task")
            self.assertEqual(rt.task(first).status, "done")
            self.assertEqual(rt.delivery.view(first), delivery)
            self.assertEqual(rt.log.read(task_id=first), original)
            self.assertEqual(len(final.calls), 1)

    def test_import_failure_keeps_completed_groups_and_no_half_import(self):
        first, second = [self.rt.create_task(f"Old {i}") for i in range(2)]
        self.rt.log.db.execute(
            "CREATE TEMP TRIGGER reject_import BEFORE INSERT ON events "
            "WHEN NEW.type = 'room.task_attached' AND "
            f"json_extract(NEW.payload_json, '$.task_id') = '{second}' "
            "BEGIN SELECT RAISE(ABORT, 'simulated failure'); END"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.rt.rooms.import_legacy()
        imported = [r for r in self.rt.rooms.list() if r["task_ids"]]
        self.assertEqual([r["task_ids"] for r in imported], [[first]])
        self.assertEqual(len(self.rt.rooms.list()), 2)
        self.rt.log.db.execute("DROP TRIGGER reject_import")
        with Runtime(self.db, audit_key="test-key") as reopened:
            self.assertEqual(reopened.rooms.import_legacy(), {"created": 1})
            self.assertEqual(reopened.rooms.import_legacy(), {"created": 0})
            self.assertEqual(len(reopened.rooms.list()), 3)
            self.assertTrue(reopened.verify_chain().ok)

    def test_membership_and_task_history_are_read_in_one_snapshot(self):
        first = self.task()
        second = self.rt.create_task("Not attached at read time")
        original_require = self.rt.rooms._require
        appended = []

        def concurrent_attach(room_id):
            room = original_require(room_id)
            if not appended:
                appended.append(True)
                # WAL permits another connection to commit while this reader
                # retains its old membership and corresponding event snapshot.
                with Runtime(self.db, audit_key="test-key") as other:
                    other.rooms.attach(self.room, second)
            return room

        self.rt.log.db.execute("PRAGMA journal_mode = WAL")
        with patch.object(self.rt.rooms, "_require", side_effect=concurrent_attach):
            page = self.rt.rooms.timeline(self.room)
        self.assertEqual({e.task_id for e in page["events"] if e.task_id}, {first})
        with self.assertRaises(WorkConflict):
            self.rt.rooms.timeline(self.room, after_seq=page["next_after_seq"], membership_seq=page["membership_seq"])

    def test_message_queries_do_not_deserialize_other_groups_or_large_artifacts(self):
        other = self.rt.rooms.create("Other", request_id="create-room-002")["room"]["id"]
        foreign = self.message("foreign" * 4000, room=other, key="foreign-message-001")
        unrelated = self.rt.create_task("Unrelated")
        artifact = self.rt.log.emit(actor=Actor.agent("dev", "worker"), type="deliverable.published",
                                    task_id=unrelated, payload={"content": "x" * 100000})
        convert = self.rt.log._row_to_event

        def selected_only(row):
            self.assertNotIn(row["seq"], {foreign.seq, artifact.seq})
            return convert(row)

        with patch.object(self.rt.log, "_row_to_event", side_effect=selected_only):
            message = self.message()
            self.assertEqual(self.message(), message)
            self.assertEqual(self.rt.rooms.timeline(self.room)["events"], [message])

    def test_invalid_values_do_not_write(self):
        count = self.rt.log.count()
        for value in (None, [], "", " ", "x" * 32001):
            with self.subTest(value=str(value)[:30]), self.assertRaises(ValueError):
                self.message(value)
        for key in (None, [], "short", "bad/request", "中文标识不是请求"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.message(key=key)
        for args in ({"after_seq": True}, {"after_seq": -1}, {"after_seq": 1},
                     {"limit": 0}, {"limit": 201}, {"membership_seq": False},
                     {"after_seq": 2 ** 63}, {"membership_seq": 2 ** 63}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.rt.rooms.timeline(self.room, **args)
        self.assertEqual(self.rt.log.count(), count)

    def test_room_tasks_keep_same_project_memories_and_profiles_separate_in_both_directions(self):
        for project in (None, self.rt.rulebook.create_project("Shared")["id"]):
            with self.subTest(project=project):
                first = self.task("FIRST_GOAL", key=f"first-{project}-request", project=project)
                second = self.task("SECOND_GOAL", key=f"second-{project}-request", project=project)
                legacy = self.rt.create_task("LEGACY_GOAL", project_id=project)
                for task, marker in ((first, "FIRST_PRIVATE"), (second, "SECOND_PRIVATE"), (legacy, "LEGACY_PRIVATE")):
                    self.rt.log.emit(actor=Actor.agent("dev", "worker"), type="msg.said", task_id=task,
                                     payload={"claim": marker, "text": marker})
                    self.rt.log.emit(actor=Actor.agent("dev", "worker"), type="deliverable.published", task_id=task,
                                     payload={"title": marker, "summary": marker})
                for task in (first, second):
                    self.assertEqual(self.rt.shared_memory(exclude_task_id=task), [])
                    self.assertEqual({e.task_id for e in self.rt._project_events(task, ("msg.said",))}, {task})
                self.assertFalse(any(m["task_id"] in {first, second} for m in self.rt.shared_memory(exclude_task_id=legacy)))
                self.assertFalse(any(e.task_id in {first, second} for e in self.rt._project_events(legacy, ("msg.said",))))
                recorder = RecordingDriver(WorkDriver())
                self.rt.drivers.update({name: recorder for name in ("product", "design", "dev", "test")})
                self.rt.plan_work(first, [item("first-work")])
                self.rt.collaborate(first)
                self.assertEqual(len(recorder.calls), 2)
                for call in recorder.calls:
                    self.assertEqual(call["context"]["context_scope"], "task")
                    self.assertNotIn("SECOND_PRIVATE", json.dumps(call))
                    self.assertNotIn("LEGACY_PRIVATE", json.dumps(call))
                    self.assertNotIn("SECOND_GOAL", json.dumps(call))

    def test_room_cli_calls_do_not_reuse_process_memory(self):
        program = (
            "import json,sys\n"
            "seen=[]\n"
            "for line in sys.stdin:\n"
            " r=json.loads(line); seen.append(r['context']['task_id'])\n"
            " print(json.dumps({'id':r['id'],'ok':True,'text':json.dumps(seen)}),flush=True)\n"
        )
        driver = CliDriver("stateful", [sys.executable, "-u", "-c", program], timeout=5)
        self.addCleanup(driver.close)
        for task in ("a", "b", "a"):
            response = driver.complete(role="worker", prompt="work", context={"task_id": task, "context_scope": "task"})
            self.assertEqual(json.loads(response.text), [task])
            self.assertIsNone(driver._proc)
