"""Durable group messages and task membership, independent of task commands."""

from __future__ import annotations

import hashlib
import uuid
from typing import TYPE_CHECKING, Any

from .events import Actor, Event, HeadChanged
from .work import WorkConflict

if TYPE_CHECKING:
    from .runtime import Runtime


ROOM_TYPES = ("room.created", "room.task_attached")
MAX_MESSAGE_CHARS = 32000
MAX_SEQUENCE = (1 << 63) - 1


def _text(value: Any, label: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{label}不能为空，且不能超过 {limit} 个字符。")
    return value


def _request_id(value: Any) -> str:
    if (not isinstance(value, str) or not 8 <= len(value) <= 128
            or not value.isascii() or not all(c.isalnum() or c in "-_" for c in value)):
        raise ValueError("需要有效的请求标识。")
    return value


class RoomService:
    def __init__(self, runtime: Runtime):
        self.runtime = runtime
        self.log = runtime.log

    def list(self) -> list[dict[str, Any]]:
        rooms: dict[str, dict[str, Any]] = {}
        for event in self.log.iter_events(event_types=ROOM_TYPES):
            payload = event.payload
            room_id = payload["room_id"]
            if event.type == "room.created":
                rooms[room_id] = {"id": room_id, "title": payload["title"],
                                  "created_at": event.ts, "task_ids": [], "membership_seq": 0}
            elif room_id in rooms:
                rooms[room_id]["task_ids"].append(payload["task_id"])
                rooms[room_id]["membership_seq"] = event.seq
        return list(rooms.values())

    def get(self, room_id: str) -> dict[str, Any] | None:
        return next((room for room in self.list() if room["id"] == room_id), None)

    def _require(self, room_id: str) -> dict[str, Any]:
        room = self.get(room_id)
        if room is None:
            raise ValueError("群聊不存在。")
        return room

    def create(self, title: str, *, request_id: str) -> dict[str, Any]:
        title = _text(title, "群聊名称", 160).strip()
        request_id = _request_id(request_id)
        room_id = "room_" + uuid.uuid4().hex
        for _ in range(8):
            head = self.log.head_hash()
            for event in self.log.iter_events(event_types=("room.created",)):
                if event.payload.get("request_id") == request_id:
                    if event.payload["title"] != title:
                        raise WorkConflict("同一请求不能创建不同的群聊。")
                    return {"room": self._require(event.payload["room_id"]), "created": False}
            try:
                self.log.emit(actor=Actor.human(), type="room.created",
                              payload={"room_id": room_id, "title": title, "request_id": request_id},
                              expected_head=head)
                return {"room": self._require(room_id), "created": True}
            except HeadChanged:
                continue
        raise WorkConflict("记录正在更新，请使用原请求标识重试。")

    def receive(self, room_id: str, text: str, *, request_id: str,
                task_id: str | None = None, reply_to: int | None = None) -> dict[str, Any]:
        text = _text(text, "消息", MAX_MESSAGE_CHARS)
        request_id = _request_id(request_id)
        if task_id is not None and (not isinstance(task_id, str) or not task_id):
            raise ValueError("任务引用无效。")
        if reply_to is not None and (type(reply_to) is not int or not 1 <= reply_to <= MAX_SEQUENCE):
            raise ValueError("回复引用无效。")
        request = {"room_id": room_id, "text": text, "request_id": request_id,
                   "task_id": task_id, "reply_to": reply_to}
        for _ in range(8):
            head = self.log.head_hash()
            room = self._require(room_id)
            row = self.log.db.execute(
                "SELECT * FROM events WHERE type = 'room.message_received' AND task_id IS NULL "
                "AND json_extract(payload_json, '$.room_id') = ? "
                "AND json_extract(payload_json, '$.request_id') = ? ORDER BY seq LIMIT 1",
                (room_id, request_id),
            ).fetchone()
            if row is not None:
                event = self.log._row_to_event(row)
                if event.payload != request:
                    raise WorkConflict("同一发送请求不能更换消息或引用。")
                return {"message": event, "created": False}
            if task_id is not None and task_id not in room["task_ids"]:
                raise ValueError("只能引用本群中的任务。")
            if reply_to is not None:
                row = self.log.db.execute("SELECT * FROM events WHERE seq = ?", (reply_to,)).fetchone()
                source = self.log._row_to_event(row) if row is not None else None
                if source is None or not self._belongs(source, room):
                    raise ValueError("只能回复本群中的消息或任务记录。")
                source_task = source.task_id or source.payload.get("task_id")
                if task_id is not None and source_task is not None and task_id != source_task:
                    raise ValueError("任务引用与回复对象不一致。")
            try:
                event = self.log.emit(actor=Actor.human(), type="room.message_received",
                                      payload=request, expected_head=head)
                return {"message": event, "created": True}
            except HeadChanged:
                continue
        raise WorkConflict("记录正在更新，请使用原请求标识重试。")

    def attach(self, room_id: str, task_id: str) -> dict[str, Any]:
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("需要有效的任务标识。")
        for _ in range(8):
            head = self.log.head_hash()
            self._require(room_id)
            owner = next((r for r in self.list() if task_id in r["task_ids"]), None)
            if owner is not None:
                if owner["id"] != room_id:
                    raise WorkConflict("任务已属于另一个群聊，不能同时挂入本群。")
                return {"room_id": room_id, "task_id": task_id, "created": False}
            if self.runtime.task(task_id) is None:
                raise ValueError("任务不存在。")
            try:
                self.log.emit(actor=Actor.human(), type="room.task_attached",
                              payload={"room_id": room_id, "task_id": task_id}, expected_head=head)
                return {"room_id": room_id, "task_id": task_id, "created": True}
            except HeadChanged:
                continue
        raise WorkConflict("记录正在更新，请重试。")

    def start_task(self, room_id: str, *, message_id: str, goal: str,
                   request_id: str, acceptance: list[str] | None = None,
                   project_id: str | None = None) -> dict[str, Any]:
        """Explicit adoption only; receiving text never calls this method implicitly."""
        from .runtime import MAX_TASK_GOAL_CHARS, _acceptance_items, _required_text

        request_id = _request_id(request_id)
        goal = _required_text(goal, "goal", MAX_TASK_GOAL_CHARS)
        acceptance = _acceptance_items(acceptance)
        if not isinstance(message_id, str) or not message_id:
            raise ValueError("需要本群的来源消息。")
        if project_id is not None and (not isinstance(project_id, str) or not project_id):
            raise ValueError("项目标识无效。")
        request = {"message_id": message_id, "goal": goal, "acceptance": acceptance, "project_id": project_id}
        task_id = "task_" + uuid.uuid4().hex
        for _ in range(8):
            head = self.log.head_hash()
            self._require(room_id)
            for event in self.log.iter_events(event_types=("room.task_attached",)):
                p = event.payload
                if p["room_id"] == room_id and p.get("creation_request_id") == request_id:
                    if p["creation_request"] != request:
                        raise WorkConflict("同一创建请求不能更换任务目标、项目或来源。")
                    return {"task_id": p["task_id"], "created": False}
            row = self.log.db.execute("SELECT * FROM events WHERE id = ?", (message_id,)).fetchone()
            source = self.log._row_to_event(row) if row is not None else None
            if (source is None or source.type != "room.message_received"
                    or source.payload.get("room_id") != room_id or source.actor != Actor.human()):
                raise ValueError("需要本群的顾客消息作为来源。")
            if project_id is not None:
                self.runtime.rulebook.require_project(project_id)
            try:
                self.log.emit_many([
                    {"actor": Actor.human(), "type": "task.created", "task_id": task_id,
                     "payload": {"goal": goal, "acceptance": acceptance, "project_id": project_id,
                                 "context_scope": "task", "source_message_id": message_id,
                                 "organization_fingerprint": self.runtime.organization_fingerprint}},
                    {"actor": Actor.human(), "type": "room.task_attached",
                     "payload": {"room_id": room_id, "task_id": task_id, "creation_request_id": request_id,
                                 "creation_request": request}},
                ], expected_head=head)
                return {"task_id": task_id, "created": True}
            except HeadChanged:
                continue
        raise WorkConflict("记录正在更新，请使用原请求标识重试。")

    def import_legacy(self) -> dict[str, int]:
        """Explicit, restartable adoption without rewriting historical task events."""
        created = 0
        tasks = list(self.log.iter_events(event_types=("task.created",)))
        for task in tasks:
            for _ in range(8):
                head = self.log.head_hash()
                if any(task.task_id in r["task_ids"] for r in self.list()):
                    break
                room_id = "room_legacy_" + hashlib.sha256(task.task_id.encode()).hexdigest()
                try:
                    self.log.emit_many(self._import_entries(task, room_id), expected_head=head)
                    created += 1
                    break
                except HeadChanged:
                    continue
            else:
                raise WorkConflict("历史记录正在更新，请重试导入；已导入部分不会重复。")
        return {"created": created}

    @staticmethod
    def _import_entries(task: Event, room_id: str) -> list[dict[str, Any]]:
        return [
            {"actor": Actor.system("rooms"), "type": "room.created",
             "payload": {"room_id": room_id, "title": str(task.payload.get("goal") or "历史对话")[:160],
                         "source_task_id": task.task_id}},
            {"actor": Actor.system("rooms"), "type": "room.task_attached",
             "payload": {"room_id": room_id, "task_id": task.task_id}},
        ]

    @staticmethod
    def _belongs(event: Event, room: dict[str, Any]) -> bool:
        return (event.task_id in room["task_ids"] if event.task_id is not None else
                event.type == "room.message_received" and event.payload.get("room_id") == room["id"])

    def timeline(self, room_id: str, *, after_seq: int = 0, membership_seq: int | None = None,
                 limit: int = 100) -> dict[str, Any]:
        if (type(after_seq) is not int or not 0 <= after_seq <= MAX_SEQUENCE
                or type(limit) is not int or not 1 <= limit <= 200):
            raise ValueError("消息游标或分页数量无效。")
        if membership_seq is not None and (type(membership_seq) is not int or not 0 <= membership_seq <= MAX_SEQUENCE):
            raise ValueError("任务归属版本无效。")
        if after_seq and membership_seq is None:
            raise ValueError("继续读取消息需要任务归属版本。")
        owned = not self.log.db.in_transaction
        if owned:
            self.log.db.execute("BEGIN")
        try:
            room = self._require(room_id)
            if membership_seq is not None and membership_seq != room["membership_seq"]:
                raise WorkConflict("群内任务已变化，请从头刷新消息。")
            # Existing task history stays in place; membership changes invalidate
            # cursors so a newly attached older task cannot silently disappear.
            ids = room["task_ids"]
            placeholders = ",".join("?" for _ in ids) or "NULL"
            rows = self.log.db.execute(
                f"SELECT * FROM events WHERE seq > ? AND (task_id IN ({placeholders}) "
                "OR (task_id IS NULL AND type = 'room.message_received' "
                "AND json_extract(payload_json, '$.room_id') = ?)) ORDER BY seq LIMIT ?",
                [after_seq, *ids, room_id, limit + 1],
            )
            events = []
            try:
                for row in rows:
                    event = self.log._row_to_event(row)
                    if self._belongs(event, room):
                        events.append(event)
                        if len(events) > limit:
                            break
            finally:
                rows.close()
            page = events[:limit]
            return {"events": page, "next_after_seq": page[-1].seq if page else after_seq,
                    "membership_seq": room["membership_seq"], "has_more": len(events) > limit}
        finally:
            if owned:
                self.log.db.rollback()
