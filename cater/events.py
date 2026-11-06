"""Append-only event log.

The event log is Cater's source of truth. Current state, inboxes, and briefs are
views over this log; they are not independent write paths.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
import stat
import uuid
from dataclasses import dataclass
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

GENESIS_HASH = "0" * 64


def _check_extended_syntax(path: str) -> None:
    # SQLite and pathlib can normalize syntax that Win32 extended paths do not.
    parts = path[4:].split("\\")
    if ("/" in path or any(part in {".", ".."} for part in parts)
            or any(not part for part in parts[:-1])):
        raise ValueError("ambiguous extended Windows path")


def _windows_io_path(path: str | os.PathLike[str]) -> str:
    """Use extended syntax for long local paths without changing their meaning."""
    path = os.fspath(path)
    if not isinstance(path, str):
        raise TypeError("path must be a string or a string-valued path-like object")
    if os.name != "nt" or path == ":memory:":
        return path
    if path.startswith("file:"):
        # SQLite may enable URI interpretation globally. Inspect the decoded
        # filename only, without altering URI query, fragment, or native behavior.
        filename = path[5:].split("?", 1)[0].split("#", 1)[0]
        decoded = re.sub(r"%([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), filename)
        start = decoded.replace("/", "\\").find("\\\\?\\")
        if start >= 0:
            _check_extended_syntax(decoded[start:])
        return path
    namespace = path.replace("/", "\\")
    if namespace.startswith("\\\\?\\"):
        _check_extended_syntax(path)
        return path
    if namespace.startswith("\\\\.\\"):
        return path
    absolute = os.path.abspath(path)
    # Win32 limits count UTF-16 code units, including two for non-BMP characters.
    # Preserve lone surrogates as Windows path strings do; leave rejection to I/O.
    # Leave room for SQLite journal/WAL suffixes and for directory operations.
    if len(absolute.encode("utf-16-le", errors="surrogatepass")) // 2 < 240:
        return path
    if absolute.startswith(("\\\\?\\", "\\\\.\\")):
        raise ValueError("cannot safely extend a path relative to a Windows device namespace")
    drive, absolute_tail = os.path.splitdrive(absolute)
    _, original_tail = os.path.splitdrive(path.replace("/", "\\"))
    for tail in (original_tail, absolute_tail):
        for part in tail.split("\\"):
            if part in {"", ".", ".."}:
                continue
            if (part.endswith((" ", ".")) or re.search(r'[<>:"|?*\x00-\x1f]', part)
                    or re.match(r"^(?:CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[1-9¹²³]|LPT[1-9¹²³]) *(?:\.|$)", part, re.I)):
                raise ValueError("ambiguous Windows path cannot be used as a long path")
    if drive.startswith("\\\\"):
        return "\\\\?\\UNC\\" + absolute[2:]
    return "\\\\?\\" + absolute


def _managed_database_path_state(path: str):
    """Observe database and ancestors without resolving links; return None if unproven."""
    if path == ":memory:" or path.startswith("file:"):
        return None
    try:
        target = Path(os.path.abspath(_windows_io_path(path)))
        parents = []
        for parent in reversed(target.parents):
            info = os.lstat(_windows_io_path(str(parent)))
            if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                    or getattr(info, "st_file_attributes", 0) & 0x400
                    or not info.st_dev or not info.st_ino):
                return None
            parents.append((info.st_dev, info.st_ino))
        try:
            info = os.lstat(_windows_io_path(str(target)))
        except FileNotFoundError:
            return (tuple(parents), None)
        if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & 0x400
                or not info.st_dev or not info.st_ino):
            return None
        return (tuple(parents), (info.st_dev, info.st_ino))
    except (OSError, ValueError, TypeError):
        return None

EVENT_CATEGORIES = {
    "team.started": "lifecycle",
    "team.message_received": "dialogue",
    "team.assignment_withdrawn": "lifecycle",
    "team.questions_requested": "dialogue",
    "team.run_started": "lifecycle",
    "team.run_finished": "lifecycle",
    "team.objective_updated": "planning",
    "team.objective_completed": "outcome",
    "team.model_called": "driver",
    "team.native_response_completed": "driver",
    "team.response_correction_requested": "driver",
    "team.external_run_started": "driver",
    "team.external_run_finished": "driver",
    "team.external_run_reconciled": "driver",
    "team.external_directory_created": "driver",
    "team.external_directory_settled": "driver",
    "team.tool_started": "execution",
    "team.tool_finished": "execution",
    "team.tool_reconciled": "execution",
    "team.artifact_saved": "outcome",
    "team.review_requested": "review",
    "team.review_recorded": "review",
    "team.review_followup_requested": "review",
    "team.review_supervision_intended": "review",
    "planning.requested": "planning",
    "planning.started": "planning",
    "planning.finished": "planning",
    "work.plan_created": "planning",
    "work.claimed": "lifecycle",
    "work.lease_renewed": "lifecycle",
    "work.submitted": "outcome",
    "work.reviewed": "review",
    "work.review_started": "review",
    "work.review_finished": "review",
    "work.review_reconciled": "review",
    "work.repair_requested": "lifecycle",
    "work.invalidated": "review",
    "work.failed": "lifecycle",
    "work.review_deferred": "review",
    "delivery.prepared": "outcome",
    "delivery.review_started": "review",
    "delivery.review_finished": "review",
    "delivery.response_recorded": "review",
    "delivery.check_started": "review",
    "work.check_started": "review",
    "checks.contract_revised": "policy",
    "project.created": "planning",
    "task.project_bound": "planning",
    "room.created": "lifecycle",
    "room.task_attached": "planning",
    "room.message_received": "dialogue",
    "rule.recorded": "policy",
    "rule.updated": "policy",
    "rules.applied": "policy",
    "task.created": "lifecycle",
    "task.started": "lifecycle",
    "task.activation_requested": "lifecycle",
    "task.execution_controlled": "lifecycle",
    "task.removed_from_list": "lifecycle",
    "activity.queued": "lifecycle",
    "activity.started": "lifecycle",
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
    "driver.usage_inconsistent": "driver",
    "driver.probe_called": "driver",
    "driver.retry_scheduled": "driver",
    "driver.retry_finished": "driver",
    "driver.budget_exhausted": "driver",
    "agent.working": "dialogue",
    "msg.said": "dialogue",
    "msg.challenge": "dialogue",
    "agent.discipline_flagged": "decision",
    "diagnosis.recorded": "analysis",
    "deliverable.published": "outcome",
    "artifacts.materialized": "outcome",
    "artifacts.materialization_failed": "outcome",
    "workspace.bound": "policy",
    "workspace.tool_started": "execution",
    "workspace.tool_finished": "execution",
    "workspace.tool_reconciled": "execution",
    "workspace.work_reconciled": "execution",
    "workspace.execution_stopped": "execution",
    "workspace.model_called": "driver",
    "driver.invocation_skipped": "driver",
    "driver.model_called": "driver",
    "workspace.files_captured": "outcome",
    "workspace.response_recorded": "driver",
    "handoff.published": "outcome",
    "verification.completed": "review",
    "verification.planning_started": "review",
    "verification.planned": "review",
    "cabinet.voted": "decision",
    "decision.proposed": "decision",
    "agent.decided": "decision",
    "supreme.decided": "decision",
    "escalation.triggered": "decision",
    "brief.generated": "decision",
    "human.commanded": "decision",
    "task.progress_reported": "dialogue",
    "question.asked": "dialogue",
    "question.started": "lifecycle",
    "question.finished": "dialogue",
    "conversation.received": "dialogue",
    "conversation.started": "lifecycle",
    "conversation.finished": "dialogue",
    "collaboration.requested": "dialogue",
    "collaboration.finished": "dialogue",
    "clarification.requested": "dialogue",
    "human.decided": "decision",
    "review.generated": "review",
}


class UnknownEventType(ValueError):
    pass


class HeadChanged(ValueError):
    """A conditional append must be recomputed from the current event head."""


class AuditSigningRequired(ValueError):
    """Appending to signed history requires an active signing key."""

    def __init__(self):
        super().__init__('事件日志已有签名记录，但当前没有签名密钥；未写入，请核对审计配置。')


class AuditSigningMismatch(AuditSigningRequired):
    """The configured keyring cannot authenticate signed history for an append."""

    def __init__(self):
        ValueError.__init__(self, '事件日志签名与当前审计密钥配置不匹配或历史不完整；未写入，请核对审计配置。')


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
        self.path = os.fspath(path) if isinstance(path, os.PathLike) else str(path)
        self.key_id = _normalize_key_id(key_id)
        self.audit_keys = _normalize_audit_keys(audit_keys)
        self.audit_key = _normalize_audit_key(audit_key)
        self._single_audit_key_fallback = self.audit_key is not None and audit_keys is None
        if self.audit_key is not None:
            self.audit_keys[self.key_id] = self.audit_key
        elif self.key_id in self.audit_keys:
            self.audit_key = self.audit_keys[self.key_id]
        io_path = _windows_io_path(self.path)
        if self.path != ":memory:":
            Path(io_path).parent.mkdir(parents=True, exist_ok=True)
        before = _managed_database_path_state(self.path)
        self.db = sqlite3.connect(io_path, timeout=5)
        after = _managed_database_path_state(self.path)
        self._managed_database_path_proof = (after if before is not None and after is not None
            and after[1] is not None and before[0] == after[0]
            and (before[1] is None or before[1] == after[1]) else None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout = 5000")
        self._init_schema()

    def close(self) -> None:
        self.db.close()

    @contextmanager
    def atomic(self):
        """Group local event writes only; never hold this lock across external work."""
        nested = self.db.in_transaction
        savepoint = "events_" + uuid.uuid4().hex
        self.db.execute(f"SAVEPOINT {savepoint}" if nested else "BEGIN IMMEDIATE")
        try:
            yield
            if nested:
                self.db.execute(f"RELEASE SAVEPOINT {savepoint}")
            else:
                self.db.commit()
        except BaseException:
            if nested:
                self.db.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                self.db.execute(f"RELEASE SAVEPOINT {savepoint}")
            else:
                self.db.rollback()
            raise

    def emit(
        self,
        *,
        actor: Actor,
        type: str,
        task_id: str | None = None,
        payload: dict[str, Any] | None = None,
        expected_head: str | None = None,
    ) -> Event:
        return self.emit_many(
            [{"actor": actor, "type": type, "task_id": task_id, "payload": payload}],
            expected_head=expected_head,
        )[0]

    def emit_many(
        self,
        entries: Iterable[dict[str, Any]],
        *,
        expected_head: str | None = None,
    ) -> list[Event]:
        """Append entries atomically, checking the global head once under lock.

        Entries use emit's actor/type/task_id/payload fields. An empty iterable
        returns [] without opening a transaction or checking expected_head.
        """
        entries = list(entries)
        if not entries:
            return []
        for entry in entries:
            if EVENT_CATEGORIES.get(entry["type"]) is None:
                raise UnknownEventType(entry["type"])
        # Multiple Web requests use independent SQLite connections.  Acquiring
        # the write transaction before reading the previous hash prevents two
        # writers from attaching different events to the same chain head.
        with self.atomic():
            signing_key, signing_id = self.require_signing_for_append()
            prev = self.head_hash()
            if expected_head is not None and prev != expected_head:
                raise HeadChanged("event head changed")
            events = []
            for entry in entries:
                actor = entry["actor"]
                type = entry["type"]
                task_id = entry.get("task_id")
                category = EVENT_CATEGORIES[type]
                event_id = uuid.uuid4().hex
                ts = datetime.now(timezone.utc).isoformat(timespec="microseconds")
                payload = dict(entry.get("payload") or {})
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
                signature = self._sign_with(signing_key, fingerprint) if signing_key is not None else None
                key_id = signing_id if signature else None
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
                events.append(Event(
                    int(cur.lastrowid),
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
                ))
                prev = fingerprint
        return events

    def require_signing_for_append(self) -> tuple[bytes | None, str]:
        """Check signing configuration against one historical sample per key ID.

        The caller holds the write transaction. This prevents an inconsistent
        configured key from appending to trusted history and detects an unsigned
        suffix. Samples do not authenticate every historical signature or the
        event hash chain; use verify_chain for complete historical verification.
        """
        if not self.db.in_transaction:
            raise RuntimeError('Signing admission requires a write transaction')
        signing_key, signing_id = self.audit_key, self.key_id
        keys, fallback = dict(self.audit_keys), self._single_audit_key_fallback
        configured = keys.get(signing_id)
        if (signing_key is not None and (not isinstance(signing_key, bytes)
                or not isinstance(configured, bytes) or not hmac.compare_digest(signing_key, configured))):
            raise AuditSigningMismatch()
        first = self.db.execute("SELECT seq FROM events INDEXED BY events_signed "
                                "WHERE signature IS NOT NULL AND signature <> '' ORDER BY seq LIMIT 1").fetchone()
        if first is None:
            return signing_key, signing_id
        if signing_key is None:
            raise AuditSigningRequired()
        if self.db.execute("SELECT 1 FROM events INDEXED BY events_unsigned_seq "
                           "WHERE (signature IS NULL OR signature = '') AND seq > ? LIMIT 1",
                           (first['seq'],)).fetchone():
            raise AuditSigningMismatch()

        def check(row):
            if row is None:
                return
            raw_id = row['key_id']
            if raw_id is not None and type(raw_id) is not str:
                raise AuditSigningMismatch()
            event_key_id = raw_id or signing_id
            key = keys.get(event_key_id)
            if key is None and fallback:
                key = signing_key
            signature, digest = row['signature'], row['hash']
            if (key is None or type(signature) is not str or type(digest) is not str
                    or len(signature) != 64 or not signature.isascii()
                    or re.fullmatch(r'[0-9a-f]{64}', digest) is None
                    or not hmac.compare_digest(signature, self._sign_with(key, digest))):
                raise AuditSigningMismatch()

        where = "signature IS NOT NULL AND signature <> ''"
        columns = 'key_id, hash, signature'
        index = 'events INDEXED BY events_signed_key_seq'
        check(self.db.execute(f'SELECT {columns} FROM {index} WHERE {where} '
                              'AND key_id IS NULL ORDER BY seq LIMIT 1').fetchone())
        row = self.db.execute(f'SELECT {columns} FROM {index} WHERE {where} '
                              'AND key_id IS NOT NULL ORDER BY key_id, seq LIMIT 1').fetchone()
        while row is not None:
            check(row)
            row = self.db.execute(f'SELECT {columns} FROM {index} WHERE {where} '
                                  'AND key_id > ? ORDER BY key_id, seq LIMIT 1', (row['key_id'],)).fetchone()
        return signing_key, signing_id

    def read(self, task_id: str | None = None) -> list[Event]:
        return list(self.iter_events(task_id=task_id))

    def iter_events(
        self, *, task_id: str | None = None, event_types: Iterable[str] | None = None,
        after_seq: int = 0,
        before_seq: int | None = None,
    ) -> Iterator[Event]:
        """Stream selected immutable events without materializing other payloads."""
        if type(after_seq) is not int or after_seq < 0:
            raise ValueError("after_seq must be a non-negative integer")
        if before_seq is not None and (type(before_seq) is not int or before_seq < 1):
            raise ValueError("before_seq must be a positive integer")
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
        if before_seq is not None:
            clauses.append("seq < ?")
            params.append(before_seq)
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
        """Allow unsigned history only before signing starts.

        With verification keys, a nonempty log must contain signatures. This
        intentionally rejects fully unsigned history until a signed event is
        appended, rather than accepting a log whose signatures were stripped.
        """
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
            elif signed:
                return ChainReport(False, checked, event.seq, "missing signature after signed event", prev, signed)
            prev = event.hash
        if checked and self.audit_keys and not signed:
            return ChainReport(False, checked, rows[0]["seq"],
                               "audit key supplied but log contains no signatures", prev, signed)
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
        self.db.execute("CREATE INDEX IF NOT EXISTS events_signed ON events(seq) "
                        "WHERE signature IS NOT NULL AND signature <> ''")
        self.db.execute("CREATE INDEX IF NOT EXISTS events_signed_key_seq ON events(key_id, seq) "
                        "WHERE signature IS NOT NULL AND signature <> ''")
        self.db.execute("CREATE INDEX IF NOT EXISTS events_unsigned_seq ON events(seq) "
                        "WHERE signature IS NULL OR signature = ''")
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
    if not value:
        raise ValueError("audit key must be non-empty")
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
            raise ValueError("audit keyring values must be non-empty")
        keys[_normalize_key_id(key_id)] = normalized
    return keys


def _normalize_key_id(value: str) -> str:
    key_id = value.strip()
    if not key_id:
        raise ValueError("audit key id must not be empty")
    return key_id
