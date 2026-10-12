"""Append-only event log.

The event log is Cater's source of truth. Current state, inboxes, and briefs are
views over this log; they are not independent write paths.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

GENESIS_HASH = "0" * 64

EVENT_CATEGORIES = {
    "task.created": "lifecycle",
    "task.started": "lifecycle",
    "driver.failed": "driver",
    "msg.said": "dialogue",
    "msg.challenge": "dialogue",
    "cabinet.voted": "decision",
    "decision.proposed": "decision",
    "agent.decided": "decision",
    "supreme.decided": "decision",
    "escalation.triggered": "decision",
    "brief.generated": "decision",
    "human.commanded": "decision",
    "human.decided": "decision",
    "review.generated": "review",
}


class UnknownEventType(ValueError):
    pass


@dataclass(frozen=True)
class Actor:
    id: str
    role: str
    kind: str = "agent"

    @staticmethod
    def system(role: str = "system") -> "Actor":
        return Actor(id=role, role=role, kind="system")

    @staticmethod
    def human(role: str = "human") -> "Actor":
        return Actor(id=role, role=role, kind="human")

    @staticmethod
    def agent(actor_id: str, role: str) -> "Actor":
        return Actor(id=actor_id, role=role, kind="agent")

    def to_dict(self) -> dict[str, str]:
        return {"id": self.id, "role": self.role, "kind": self.kind}


@dataclass(frozen=True)
class Event:
    seq: int
    id: str
    ts: str
    task_id: str | None
    actor: Actor
    type: str
    category: str
    payload: dict[str, Any]
    prev_hash: str
    hash: str
    key_id: str | None = None
    signature: str | None = None


@dataclass(frozen=True)
class ChainReport:
    ok: bool
    checked: int
    broken_at: int | None = None
    reason: str = ""
    head_hash: str = GENESIS_HASH
    signed: int = 0

    def describe(self) -> str:
        if self.ok:
            suffix = f", signed {self.signed} event(s)" if self.signed else ""
            return f"hash chain ok, checked {self.checked} event(s){suffix}"
        return f"hash chain broken at seq={self.broken_at}: {self.reason}"


class EventLog:
    def __init__(
        self,
        path: str | Path = ":memory:",
        *,
        audit_key: str | bytes | None = None,
        key_id: str = "local",
    ):
        self.path = str(path)
        self.audit_key = _normalize_audit_key(audit_key)
        self.key_id = key_id
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self._init_schema()

    def close(self) -> None:
        self.db.close()

    def emit(
        self,
        *,
        actor: Actor,
        type: str,
        task_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> Event:
        category = EVENT_CATEGORIES.get(type)
        if category is None:
            raise UnknownEventType(type)
        prev = self.head_hash()
        event_id = uuid.uuid4().hex
        ts = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        payload = dict(payload or {})
        fingerprint = self._fingerprint(
            event_id=event_id,
            ts=ts,
            task_id=task_id,
            actor=actor,
            type=type,
            category=category,
            payload=payload,
            prev_hash=prev,
        )
        signature = self._sign(fingerprint) if self.audit_key else None
        key_id = self.key_id if signature else None
        cur = self.db.execute(
            """INSERT INTO events
               (id, ts, task_id, actor_json, type, category, payload_json, prev_hash, hash, key_id, signature)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event_id,
                ts,
                task_id,
                json.dumps(actor.to_dict(), ensure_ascii=False, sort_keys=True),
                type,
                category,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                prev,
                fingerprint,
                key_id,
                signature,
            ),
        )
        self.db.commit()
        seq = int(cur.lastrowid)
        return Event(
            seq,
            event_id,
            ts,
            task_id,
            actor,
            type,
            category,
            payload,
            prev,
            fingerprint,
            key_id,
            signature,
        )

    def read(self, task_id: str | None = None) -> list[Event]:
        sql = "SELECT * FROM events"
        params: tuple[Any, ...] = ()
        if task_id is not None:
            sql += " WHERE task_id = ?"
            params = (task_id,)
        sql += " ORDER BY seq"
        return [self._row_to_event(r) for r in self.db.execute(sql, params).fetchall()]

    def count(self) -> int:
        return int(self.db.execute("SELECT COUNT(*) FROM events").fetchone()[0])

    def head_hash(self) -> str:
        row = self.db.execute("SELECT hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        return row["hash"] if row else GENESIS_HASH

    def verify_chain(self) -> ChainReport:
        prev = GENESIS_HASH
        checked = 0
        signed = 0
        rows = self.db.execute("SELECT * FROM events ORDER BY seq").fetchall()
        for row in rows:
            checked += 1
            event = self._row_to_event(row)
            if event.prev_hash != prev:
                return ChainReport(False, checked, event.seq, "prev_hash mismatch", prev)
            expected = self._fingerprint(
                event_id=event.id,
                ts=event.ts,
                task_id=event.task_id,
                actor=event.actor,
                type=event.type,
                category=event.category,
                payload=event.payload,
                prev_hash=event.prev_hash,
            )
            if event.hash != expected:
                return ChainReport(False, checked, event.seq, "payload or metadata changed", prev)
            if event.signature:
                signed += 1
                if self.audit_key is None:
                    return ChainReport(False, checked, event.seq, "signature present but no audit key supplied", prev, signed)
                expected_signature = self._sign(event.hash)
                if not hmac.compare_digest(event.signature, expected_signature):
                    return ChainReport(False, checked, event.seq, "signature mismatch", prev, signed)
            prev = event.hash
        return ChainReport(True, checked, head_hash=prev, signed=signed)

    def _init_schema(self) -> None:
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT NOT NULL UNIQUE,
                ts TEXT NOT NULL,
                task_id TEXT,
                actor_json TEXT NOT NULL,
                type TEXT NOT NULL,
                category TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                prev_hash TEXT NOT NULL,
                hash TEXT NOT NULL,
                key_id TEXT,
                signature TEXT
            );
            CREATE TRIGGER IF NOT EXISTS events_no_update
            BEFORE UPDATE ON events
            BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
            CREATE TRIGGER IF NOT EXISTS events_no_delete
            BEFORE DELETE ON events
            BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
            """
        )
        self._ensure_column("events", "key_id", "TEXT")
        self._ensure_column("events", "signature", "TEXT")
        self.db.commit()

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        rows = self.db.execute(f"PRAGMA table_info({table})").fetchall()
        if column not in {row["name"] for row in rows}:
            self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def _row_to_event(self, row: sqlite3.Row) -> Event:
        actor_data = json.loads(row["actor_json"])
        return Event(
            seq=int(row["seq"]),
            id=row["id"],
            ts=row["ts"],
            task_id=row["task_id"],
            actor=Actor(**actor_data),
            type=row["type"],
            category=row["category"],
            payload=json.loads(row["payload_json"]),
            prev_hash=row["prev_hash"],
            hash=row["hash"],
            key_id=row["key_id"],
            signature=row["signature"],
        )

    def _sign(self, fingerprint: str) -> str:
        assert self.audit_key is not None
        return hmac.new(self.audit_key, fingerprint.encode("utf-8"), hashlib.sha256).hexdigest()

    @staticmethod
    def _fingerprint(
        *,
        event_id: str,
        ts: str,
        task_id: str | None,
        actor: Actor,
        type: str,
        category: str,
        payload: dict[str, Any],
        prev_hash: str,
    ) -> str:
        body = {
            "id": event_id,
            "ts": ts,
            "task_id": task_id,
            "actor": actor.to_dict(),
            "type": type,
            "category": category,
            "payload": payload,
            "prev_hash": prev_hash,
        }
        raw = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _normalize_audit_key(value: str | bytes | None) -> bytes | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value
    return value.encode("utf-8")
