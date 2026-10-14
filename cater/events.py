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
from typing import Any, Iterable, Iterator, Mapping

GENESIS_HASH = "0" * 64

EVENT_CATEGORIES = {
    "planning.requested": "planning",
    "planning.started": "planning",
    "planning.finished": "planning",
    "work.plan_created": "planning",
    "work.claimed": "lifecycle",
    "work.submitted": "outcome",
    "work.reviewed": "review",
    "work.invalidated": "review",
    "work.failed": "lifecycle",
    "work.review_deferred": "review",
    "project.created": "planning",
    "task.project_bound": "planning",
    "rule.recorded": "policy",
    "rule.updated": "policy",
    "rules.applied": "policy",
    "task.created": "lifecycle",
    "task.started": "lifecycle",
    "activity.queued": "lifecycle",
    "activity.completed": "lifecycle",
    "activity.failed": "lifecycle",
    "task.interpreted": "planning",
    "task.routed": "planning",
    "cabinet.convened": "planning",
    "mission.created": "planning",
    "task.decomposed": "planning",
    "subtask.assigned": "planning",
    "plan.proposed": "planning",
    "plan.amended": "planning",
    "driver.failed": "driver",
    "driver.budget_exhausted": "driver",
    "agent.working": "dialogue",
    "msg.said": "dialogue",
    "msg.challenge": "dialogue",
    "agent.discipline_flagged": "decision",
    "diagnosis.recorded": "analysis",
    "deliverable.published": "outcome",
    "handoff.published": "outcome",
    "verification.completed": "review",
    "cabinet.voted": "decision",
    "decision.proposed": "decision",
    "agent.decided": "decision",
    "supreme.decided": "decision",
    "escalation.triggered": "decision",
    "brief.generated": "decision",
    "human.commanded": "decision",
    "task.progress_reported": "dialogue",
    "human.decided": "decision",
    "review.generated": "review",
}


class UnknownEventType(ValueError):
    pass


class HeadChanged(ValueError):
    """A conditional append must be recomputed from the current event head."""


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
        audit_keys: Mapping[str, str | bytes] | None = None,
    ):
        self.path = str(path)
        self.key_id = _normalize_key_id(key_id)
        self.audit_keys = _normalize_audit_keys(audit_keys)
        self.audit_key = _normalize_audit_key(audit_key)
        self._single_audit_key_fallback = self.audit_key is not None and audit_keys is None
        if self.audit_key is not None:
            self.audit_keys[self.key_id] = self.audit_key
        elif self.key_id in self.audit_keys:
            self.audit_key = self.audit_keys[self.key_id]
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout = 5000")
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
        expected_head: str | None = None,
    ) -> Event:
        category = EVENT_CATEGORIES.get(type)
        if category is None:
            raise UnknownEventType(type)
        # Multiple Web requests use independent SQLite connections.  Acquiring
        # the write transaction before reading the previous hash prevents two
        # writers from attaching different events to the same chain head.
        self.db.execute("BEGIN IMMEDIATE")
        try:
            prev = self.head_hash()
            if expected_head is not None and prev != expected_head:
                raise HeadChanged("event head changed")
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
        except Exception:
            self.db.rollback()
            raise
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
        return list(self.iter_events(task_id=task_id))

    def iter_events(
        self, *, task_id: str | None = None, event_types: Iterable[str] | None = None,
        after_seq: int = 0,
    ) -> Iterator[Event]:
        """Stream selected immutable events without materializing other payloads."""
        if type(after_seq) is not int or after_seq < 0:
            raise ValueError("after_seq must be a non-negative integer")
        if isinstance(event_types, str):
            raise ValueError("event_types must be an iterable of names, not a string")
        types = tuple(dict.fromkeys(event_types)) if event_types is not None else None
        if types is not None:
            for event_type in types:
                if event_type not in EVENT_CATEGORIES:
                    raise UnknownEventType(event_type)
            if not types:
                return
        sql = "SELECT * FROM events"
        clauses = ["seq > ?"]
        params: list[Any] = [after_seq]
        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(task_id)
        if types is not None:
            clauses.append(f"type IN ({','.join('?' for _ in types)})")
            params.extend(types)
        sql += " WHERE " + " AND ".join(clauses) + " ORDER BY seq"
        cursor = self.db.execute(sql, params)
        try:
            for row in cursor:
                yield self._row_to_event(row)
        finally:
            cursor.close()

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
                event_key_id = event.key_id or self.key_id
                signing_key = self.audit_keys.get(event_key_id)
                if signing_key is None and self._single_audit_key_fallback:
                    signing_key = self.audit_key
                if signing_key is None:
                    return ChainReport(
                        False,
                        checked,
                        event.seq,
                        f"signature present but no audit key supplied for key_id={event_key_id!r}",
                        prev,
                        signed,
                    )
                expected_signature = self._sign_with(signing_key, event.hash)
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
            CREATE INDEX IF NOT EXISTS events_type_seq ON events(type, seq);
            CREATE INDEX IF NOT EXISTS events_task_seq ON events(task_id, seq);
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
        return self._sign_with(self.audit_key, fingerprint)

    @staticmethod
    def _sign_with(key: bytes, fingerprint: str) -> str:
        return hmac.new(key, fingerprint.encode("utf-8"), hashlib.sha256).hexdigest()

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


def _normalize_audit_keys(values: Mapping[str, str | bytes] | None) -> dict[str, bytes]:
    if values is None:
        return {}
    keys: dict[str, bytes] = {}
    for key_id, value in values.items():
        normalized = _normalize_audit_key(value)
        if normalized is None:
            raise ValueError("audit keyring values must not be null")
        keys[_normalize_key_id(key_id)] = normalized
    return keys


def _normalize_key_id(value: str) -> str:
    key_id = value.strip()
    if not key_id:
        raise ValueError("audit key id must not be empty")
    return key_id
