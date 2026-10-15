from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from cater.events import Actor
from cater.rooms import RoomService
from cater.runtime import Runtime
from cater.web import ActivityRunner, MAX_REQUEST_BYTES, event_to_dict, make_handler
from cater.work import WorkConflict
from test_runtime import start_test_web


class RoomsApiTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = str(Path(tmp.name) / "rooms-api.db")
        self.rt = Runtime(self.db)
        self.addCleanup(self.rt.close)
        self.request_number = 0
        # A regression must fail before reaching a model or scheduling work.
        for owner, names in (
            (Runtime, ("intake", "command", "assign", "dispatch", "collaborate", "battle",
                       "_complete_with_containment")),
            (ActivityRunner, ("submit", "continue_after_current", "answer_questions", "_start")),
        ):
            for name in names:
                mock = self.enterContext(patch.object(owner, name, side_effect=AssertionError(name)))
                self.addCleanup(mock.assert_not_called)
        self.base = start_test_web(self, self.db, runtime_factory=lambda: Runtime(self.db))

    def request(self, path, data=None, *, expected=200, raw=None, headers=None, base=None):
        body = json.dumps(data).encode("utf-8") if data is not None else raw
        request = Request((base or self.base) + path, data=body,
                          headers={"Content-Type": "application/json", **(headers or {})},
                          method="POST" if body is not None else "GET")
        try:
            response = urlopen(request, timeout=5)
        except HTTPError as exc:
            response = exc
        with response:
            raw_body = response.read().decode("utf-8")
            self.assertEqual(response.status, expected, f"{request.method} {path}: {raw_body}")
            self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
            if response.headers.get_content_type() == "application/json":
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                return json.loads(raw_body)
            return raw_body

    def request_id(self):
        self.request_number += 1
        return f"request-{self.request_number:08d}"

    def room(self, title="Discussion"):
        return self.request("/api/rooms", {"title": title, "request_id": self.request_id()},
                            expected=201)["room"]["id"]

    def message(self, room_id, text="Discuss the next step", **fields):
        return self.request(f"/api/rooms/{room_id}/messages",
                            {"text": text, "request_id": self.request_id(), **fields},
                            expected=201)["message"]

    def test_create_list_detail_and_idempotency(self):
        self.assertEqual(self.request("/api/rooms"), [])
        data = {"title": "Shared discussion", "request_id": self.request_id()}
        created = self.request("/api/rooms", data, expected=201)
        room = created["room"]
        self.assertIs(created["created"], True)
        self.assertEqual(set(room), {"id", "title", "created_at", "task_ids", "membership_seq"})
        self.assertEqual(room["title"], data["title"])
        self.assertTrue(room["created_at"])
        self.assertEqual((room["task_ids"], room["membership_seq"]), ([], 0))
        before = self.rt.log.read()
        self.assertEqual(self.request("/api/rooms", data), {"room": room, "created": False})
        self.request("/api/rooms", {**data, "title": "Changed title"}, expected=409)
        self.assertEqual(self.request("/api/rooms"), [room])
        self.assertEqual(self.request(f"/api/rooms/{room['id']}"), room)
        self.assertEqual(self.rt.log.read(), before)
        self.assertEqual(self.rt.tasks(), [])

    def test_get_and_handler_construction_never_import_legacy_tasks(self):
        task_id = self.rt.create_task("Historical task", ["Keep its scope"])
        before = self.rt.log.read()
        with patch.object(RoomService, "import_legacy", side_effect=AssertionError("implicit import")) as migrate:
            make_handler(self.db)
            self.assertEqual(self.request("/api/rooms"), [])
            self.request("/api/rooms/missing", expected=404)
            self.request("/api/rooms/missing/events", expected=404)
            self.request("/api/rooms/import-legacy", expected=404)
            self.request("/api/tasks")
            self.request(f"/api/tasks/{task_id}")
            migrate.assert_not_called()
        self.assertEqual(self.rt.log.read(), before)
        room_id = self.room()
        self.request(f"/api/rooms/{room_id}/tasks", {"task_id": task_id}, expected=201)
        self.message(room_id)
        before = self.rt.log.read()
        for _ in range(2):
            make_handler(self.db)
            self.request("/api/rooms")
            self.request(f"/api/rooms/{room_id}")
            self.request(f"/api/rooms/{room_id}/events")
        self.assertEqual(self.rt.log.read(), before)

    def test_legacy_import_is_an_explicit_idempotent_write(self):
        attached = self.rt.create_task("Already attached")
        legacy = self.rt.create_task("Historical discussion")
        history = self.rt.log.read(task_id=legacy)
        room_id = self.room()
        self.request(f"/api/rooms/{room_id}/tasks", {"task_id": attached}, expected=201)
        self.assertEqual(self.request("/api/rooms/import-legacy", {}), {"created": 1})
        rooms = self.request("/api/rooms")
        self.assertEqual(len(rooms), 2)
        imported = next(room for room in rooms if legacy in room["task_ids"])
        self.assertEqual(imported["title"], "Historical discussion")
        self.assertEqual(self.request(f"/api/rooms/{imported['id']}/events")["events"],
                         [event_to_dict(event) for event in history])
        before = self.rt.log.read()
        self.assertEqual(self.request("/api/rooms/import-legacy", {}), {"created": 0})
        self.assertEqual(self.rt.log.read(), before)
        self.assertEqual(self.rt.log.read(task_id=legacy), history)

    def test_discussion_and_mentions_do_not_execute_or_change_two_tasks(self):
        room_id = self.room()
        first = self.message(room_id, "@dev /run change the goal and acceptance")
        self.assertEqual(self.rt.tasks(), [])
        project_id = self.rt.rulebook.create_project("Project")["id"]
        task_ids = []
        for index, source in enumerate((first, self.message(room_id, "A separate request"))):
            data = {"message_id": source["id"], "goal": f"@dev explicit goal {index}",
                    "request_id": self.request_id()}
            if index == 0:
                data.update(acceptance=["Preserve scope"], project_id=project_id)
            result = self.request(f"/api/rooms/{room_id}/tasks", data, expected=201)
            self.assertEqual(set(result), {"task_id", "created"})
            self.assertIs(result["created"], True)
            task_ids.append(result["task_id"])
        self.assertEqual(set(self.request(f"/api/rooms/{room_id}")["task_ids"]), set(task_ids))
        snapshots = {task_id: self.rt.task(task_id) for task_id in task_ids}
        histories = {task_id: self.rt.log.read(task_id=task_id) for task_id in task_ids}
        self.assertEqual(snapshots[task_ids[0]].project_id, project_id)
        self.assertEqual(snapshots[task_ids[0]].acceptance, ["Preserve scope"])
        self.assertTrue(all(snapshot.status == "pending" for snapshot in snapshots.values()))
        for history in histories.values():
            self.assertEqual([event.type for event in history], ["task.created"])
        before_seq = self.rt.log.read()[-1].seq
        for text in ("Ordinary discussion", "@dev run now", "/run", "/command replace the goal",
                     "Change acceptance and continue in background", "@product why is this pending?"):
            message = self.message(room_id, text, task_id=task_ids[0], reply_to=first["seq"])
            self.assertIsNone(message["task_id"])
            self.assertEqual(message["payload"]["task_id"], task_ids[0])
            self.assertEqual(message["payload"]["text"], text)
            self.assertEqual(message["actor"], Actor.human().to_dict())
        self.assertEqual({task.id for task in self.rt.tasks()}, set(task_ids))
        for task_id in task_ids:
            self.assertEqual(self.rt.task(task_id), snapshots[task_id])
            self.assertEqual(self.rt.log.read(task_id=task_id), histories[task_id])
        added = [event for event in self.rt.log.read() if event.seq > before_seq]
        self.assertEqual([event.type for event in added], ["room.message_received"] * 6)
        self.assertTrue(self.rt.verify_chain().ok)

    def test_message_retry_and_changed_content_or_reference_conflict(self):
        room_id = self.room()
        task_id = self.rt.create_task("Referenced task")
        self.request(f"/api/rooms/{room_id}/tasks", {"task_id": task_id}, expected=201)
        source = self.message(room_id)
        data = {"text": "A reply", "request_id": self.request_id(),
                "task_id": task_id, "reply_to": source["seq"]}
        path = f"/api/rooms/{room_id}/messages"
        result = self.request(path, data, expected=201)
        self.assertEqual(result["message"], event_to_dict(self.rt.log.read()[-1]))
        before = self.rt.log.read()
        self.assertEqual(self.request(path, data), {**result, "created": False})
        for changes in ({"text": "Changed"}, {"task_id": None}, {"reply_to": None}):
            with self.subTest(changes=changes):
                self.request(path, {**data, **changes}, expected=409)
        self.assertEqual(self.rt.log.read(), before)

    def test_attach_retry_and_cross_room_conflict(self):
        room_id, other_room = self.room(), self.room()
        task_id = self.rt.create_task("Attach once")
        before_task = self.rt.log.read(task_id=task_id)
        result = self.request(f"/api/rooms/{room_id}/tasks", {"task_id": task_id}, expected=201)
        self.assertEqual(result, {"room_id": room_id, "task_id": task_id, "created": True})
        before = self.rt.log.read()
        self.assertEqual(self.request(f"/api/rooms/{room_id}/tasks", {"task_id": task_id}),
                         {**result, "created": False})
        self.request(f"/api/rooms/{other_room}/tasks", {"task_id": task_id}, expected=409)
        self.request(f"/api/rooms/{other_room}/tasks", {"task_id": "missing"}, expected=400)
        self.assertEqual(self.rt.log.read(), before)
        self.assertEqual(self.rt.log.read(task_id=task_id), before_task)

    def test_explicit_task_retry_and_conflicts(self):
        room_id = self.room()
        source = self.message(room_id)
        another_source = self.message(room_id)
        project = self.rt.rulebook.create_project("Project")["id"]
        other_project = self.rt.rulebook.create_project("Other project")["id"]
        data = {"message_id": source["id"], "goal": "Explicit goal", "acceptance": ["Original criterion"],
                "project_id": project, "request_id": self.request_id()}
        path = f"/api/rooms/{room_id}/tasks"
        result = self.request(path, data, expected=201)
        before = self.rt.log.read()
        self.assertEqual(self.request(path, data), {**result, "created": False})
        for changes in ({"message_id": another_source["id"]}, {"goal": "Changed"},
                        {"acceptance": ["Changed"]}, {"project_id": other_project}):
            with self.subTest(changes=changes):
                self.request(path, {**data, **changes}, expected=409)
        self.assertEqual(self.rt.log.read(), before)
        self.assertEqual([task.id for task in self.rt.tasks()], [result["task_id"]])

    def test_message_and_task_sources_cannot_cross_room_membership(self):
        room_id, other_room = self.room(), self.room()
        own_task = self.rt.create_task("Attached here")
        other_task = self.rt.create_task("Attached elsewhere")
        unowned_task = self.rt.create_task("Unattached")
        for room, task in ((room_id, own_task), (other_room, other_task)):
            self.request(f"/api/rooms/{room}/tasks", {"task_id": task}, expected=201)
        source = self.message(room_id)
        foreign = self.message(other_room)
        own_event = self.rt.log.read(task_id=own_task)[0]
        other_event = self.rt.log.read(task_id=other_task)[0]
        self.message(room_id, "Reply to task history", task_id=own_task, reply_to=own_event.seq)
        self.message(room_id, "Reply to room discussion", reply_to=source["seq"])
        before = self.rt.log.read()
        for fields in ({"task_id": other_task}, {"task_id": unowned_task},
                       {"reply_to": foreign["seq"]}, {"reply_to": other_event.seq},
                       {"reply_to": 999999}, {"reply_to": True}, {"reply_to": source["id"]}):
            with self.subTest(fields=fields):
                self.request(f"/api/rooms/{room_id}/messages",
                             {"text": "Invalid reference", "request_id": self.request_id(), **fields}, expected=400)
        for message_id in (foreign["id"], own_event.id, "missing", source["seq"]):
            with self.subTest(message_id=message_id):
                self.request(f"/api/rooms/{room_id}/tasks",
                             {"message_id": message_id, "goal": "Invalid source", "request_id": self.request_id()},
                             expected=400)
        self.assertEqual(self.rt.log.read(), before)

    def test_timeline_pagination_and_membership_change_require_refresh(self):
        older_task = self.rt.create_task("Older history")
        room_id, other_room = self.room(), self.room()
        foreign = self.message(other_room)
        messages = [self.message(room_id, f"Message {i}") for i in range(3)]
        path = f"/api/rooms/{room_id}/events"
        first = self.request(path + "?limit=2")
        self.assertEqual(first, {"events": messages[:2], "next_after_seq": messages[1]["seq"],
                                 "membership_seq": 0, "has_more": True})
        cursor = first["next_after_seq"]
        self.request(f"{path}?after_seq={cursor}", expected=400)
        self.request(f"{path}?after_seq={cursor}&membership_seq=999999", expected=409)
        last = self.request(f"{path}?after_seq={cursor}&membership_seq=0")
        self.assertEqual(last, {"events": messages[2:], "next_after_seq": messages[-1]["seq"],
                                "membership_seq": 0, "has_more": False})
        empty = self.request(f"{path}?after_seq={last['next_after_seq']}&membership_seq=0")
        self.assertEqual(empty, {**last, "events": []})
        self.request(f"/api/rooms/{room_id}/tasks", {"task_id": older_task}, expected=201)
        self.request(f"{path}?after_seq={cursor}&membership_seq=0", expected=409)
        self.request(path + "?membership_seq=0", expected=409)
        refreshed = self.request(path)
        self.assertGreater(refreshed["membership_seq"], 0)
        expected = [event_to_dict(event) for event in self.rt.log.read(task_id=older_task)] + messages
        self.assertEqual(refreshed["events"], expected)
        self.assertNotIn(foreign["id"], [event["id"] for event in refreshed["events"]])
        self.assertEqual(refreshed["membership_seq"], self.request(f"/api/rooms/{room_id}")["membership_seq"])

    def test_timeline_default_and_maximum_page_sizes(self):
        room_id = self.room()
        for i in range(201):
            self.rt.rooms.receive(room_id, f"Message {i}", request_id=self.request_id())
        path = f"/api/rooms/{room_id}/events"
        first = self.request(path)
        self.assertEqual(len(first["events"]), 100)
        self.assertIs(first["has_more"], True)
        large = self.request(path + "?limit=200")
        self.assertEqual(len(large["events"]), 200)
        self.assertIs(large["has_more"], True)
        last = self.request(f"{path}?limit=200&after_seq={large['next_after_seq']}&membership_seq=0")
        self.assertEqual(len(last["events"]), 1)
        self.assertIs(last["has_more"], False)

    def test_path_and_timeline_parameter_errors_do_not_write(self):
        room_id = self.room()
        before = self.rt.log.read()
        for path in ("/api/rooms/missing", "/api/rooms/missing/events", "/api/rooms//events",
                     f"/api/rooms/{room_id}/messages", f"/api/rooms/{room_id}/events/extra"):
            with self.subTest(path=path):
                self.request(path, expected=404)
        for path in (f"/api/rooms/{room_id}", f"/api/rooms/{room_id}/events",
                     f"/api/rooms/{room_id}/run", f"/api/rooms/{room_id}/tasks/extra", "/api/rooms//messages"):
            with self.subTest(path=path):
                self.request(path, {}, expected=404)
        for action, data in (("messages", {"text": "Hello", "request_id": self.request_id()}),
                             ("tasks", {"task_id": "missing"}),
                             ("tasks", {"message_id": "missing", "goal": "Goal", "request_id": self.request_id()})):
            self.request(f"/api/rooms/missing/{action}", data, expected=404)
        for query in ("after_seq=", "after_seq=-1", "after_seq=abc", "after_seq=1.5", "after_seq=true",
                      "after_seq=1", "membership_seq=", "membership_seq=-1", "membership_seq=no",
                      "limit=", "limit=0", "limit=-1", "limit=201", "limit=1.5", "limit=no",
                      "after_seq=0&after_seq=1", "membership_seq=0&membership_seq=1", "limit=1&limit=2",
                      "task_id=foreign", "task_ids=foreign", "room_id=foreign"):
            with self.subTest(query=query):
                self.request(f"/api/rooms/{room_id}/events?{query}", expected=400)
        self.assertEqual(self.rt.log.read(), before)

    def test_invalid_body_fields_and_membership_injection_do_not_write(self):
        room_id = self.room()
        source = self.message(room_id)
        task_id = self.rt.create_task("Existing task")
        endpoints = (
            ("/api/rooms", {"title": "Room", "request_id": self.request_id()}),
            ("/api/rooms/import-legacy", {}),
            (f"/api/rooms/{room_id}/messages", {"text": "Discuss", "request_id": self.request_id()}),
            (f"/api/rooms/{room_id}/tasks", {"task_id": task_id}),
            (f"/api/rooms/{room_id}/tasks", {"message_id": source["id"], "goal": "Goal",
                                            "request_id": self.request_id()}),
        )
        before = self.rt.log.read()
        for path, data in endpoints:
            for field, value in (("run", True), ("continue", True), ("actor", {"kind": "system"}),
                                 ("background", True), ("dispatch", True), ("battle", True),
                                 ("assign_to", "dev"), ("role_id", "dev"), ("requested_route", "direct"),
                                 ("room_id", "foreign"), ("task_ids", [task_id]), ("membership_seq", 1)):
                with self.subTest(path=path, field=field):
                    self.request(path, {**data, field: value}, expected=400)
        self.request(endpoints[-1][0], {**endpoints[-1][1], "task_id": task_id}, expected=400)
        for path, bodies in (
            ("/api/rooms", ({}, {"title": 12, "request_id": self.request_id()},
                            {"title": "Room", "request_id": "bad"})),
            (f"/api/rooms/{room_id}/messages", ({}, {"text": [], "request_id": self.request_id()},
                                               {"text": " ", "request_id": self.request_id()},
                                               {"text": "Hello", "request_id": self.request_id(), "task_id": []})),
            (f"/api/rooms/{room_id}/tasks", ({}, {"task_id": []}, {"task_id": None},
                                            {**endpoints[-1][1], "acceptance": "not a list"},
                                            {**endpoints[-1][1], "project_id": "missing"})),
        ):
            for body in bodies:
                with self.subTest(path=path, body=body):
                    self.request(path, body, expected=400)
        self.assertEqual(self.rt.log.read(), before)

    def test_json_and_body_limits_apply_to_every_write_endpoint(self):
        room_id = self.room()
        paths = ("/api/rooms", "/api/rooms/import-legacy",
                 f"/api/rooms/{room_id}/messages", f"/api/rooms/{room_id}/tasks")
        before = self.rt.log.read()
        for path in paths:
            for raw in (b"{", b"[]", b"null", b'"text"', b"\xff"):
                with self.subTest(path=path, raw=raw):
                    self.request(path, raw=raw, expected=400)
            self.request(path, raw=b"x" * (MAX_REQUEST_BYTES + 1), expected=400)
        self.assertEqual(self.rt.log.read(), before)

    def test_access_token_and_csrf_protect_all_room_endpoints(self):
        room_id = self.room()
        source = self.message(room_id)
        task_id = self.rt.create_task("Existing task")
        protected = start_test_web(self, self.db, access_token="room-access-token")
        reads = ("/api/rooms", f"/api/rooms/{room_id}", f"/api/rooms/{room_id}/events")
        writes = (
            ("/api/rooms", {"title": "Protected", "request_id": self.request_id()}),
            ("/api/rooms/import-legacy", {}),
            (f"/api/rooms/{room_id}/messages", {"text": "Protected", "request_id": self.request_id()}),
            (f"/api/rooms/{room_id}/tasks", {"task_id": task_id}),
            (f"/api/rooms/{room_id}/tasks", {"message_id": source["id"], "goal": "Protected",
                                            "request_id": self.request_id()}),
        )
        before = self.rt.log.read()
        for headers in ({}, {"X-Cater-Access-Token": "wrong"}):
            for path in reads:
                self.request(path, headers=headers, base=protected, expected=401)
            for path, data in writes:
                self.request(path, data, headers=headers, base=protected, expected=401)
        auth = {"Authorization": "Bearer room-access-token"}
        for path in reads:
            self.request(path, headers=auth, base=protected)
        for origin in ("https://foreign.example", "null"):
            for path, data in writes:
                self.request(path, data, headers={**auth, "Origin": origin}, base=protected, expected=403)
                self.request(path, data, headers={"Origin": origin}, expected=403)
        self.assertEqual(self.rt.log.read(), before)
        self.request("/api/rooms", headers={"X-Cater-Access-Token": "room-access-token"}, base=protected)
        self.request(writes[0][0], writes[0][1], headers={**auth, "Origin": protected},
                     base=protected, expected=201)
        self.request(writes[2][0], writes[2][1], headers=auth, base=protected, expected=201)

    def test_service_value_errors_and_conflicts_map_to_http_statuses(self):
        room_id = self.room()
        for name, path, data in (
            ("list", "/api/rooms", None), ("get", f"/api/rooms/{room_id}", None),
            ("timeline", f"/api/rooms/{room_id}/events", None),
            ("create", "/api/rooms", {"title": "Room", "request_id": self.request_id()}),
            ("import_legacy", "/api/rooms/import-legacy", {}),
            ("receive", f"/api/rooms/{room_id}/messages", {"text": "Hello", "request_id": self.request_id()}),
            ("attach", f"/api/rooms/{room_id}/tasks", {"task_id": "task"}),
            ("start_task", f"/api/rooms/{room_id}/tasks",
             {"message_id": "message", "goal": "Goal", "request_id": self.request_id()}),
        ):
            for error, status in ((ValueError("Invalid input"), 400), (WorkConflict("Stale input"), 409)):
                with self.subTest(service=name, status=status):
                    with patch.object(RoomService, name, side_effect=error) as method:
                        self.assertEqual(self.request(path, data, expected=status), str(error))
                        method.assert_called_once()


if __name__ == "__main__":
    unittest.main()
