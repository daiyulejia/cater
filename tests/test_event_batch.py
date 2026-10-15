from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from cater.events import Actor, EventLog, GENESIS_HASH, HeadChanged, UnknownEventType


class EventBatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "events.db"
        self.log = EventLog(self.path, audit_key="batch-secret", key_id="batch-key")
        self.addCleanup(self.log.close)

    def entry(self, **overrides):
        return {"actor": Actor.system("batch-test"), "type": "msg.said", **overrides}

    def assert_unchanged(self, before):
        self.assertEqual(self.log.read(), before)
        self.assertEqual(self.log.head_hash(), before[-1].hash if before else GENESIS_HASH)
        self.assertFalse(self.log.db.in_transaction)
        report = self.log.verify_chain()
        self.assertTrue(report.ok, report.describe())

    def test_batch_is_contiguous_signed_and_persisted_with_one_commit(self):
        first = self.log.emit(**self.entry(task_id="earlier"))
        entries = [
            self.entry(type="room.created", payload={"room_id": "room-a"}),
            self.entry(type="room.task_attached", task_id="task-a", payload={"room_id": "room-a"}),
            self.entry(type="room.message_received", task_id="task-b", payload={"text": "hello"}),
        ]
        statements = []
        self.log.db.set_trace_callback(statements.append)
        try:
            events = self.log.emit_many(iter(entries), expected_head=first.hash)
        finally:
            self.log.db.set_trace_callback(None)

        self.assertEqual([event.seq for event in events], [2, 3, 4])
        self.assertEqual([event.type for event in events], [entry["type"] for entry in entries])
        self.assertEqual([event.category for event in events], ["lifecycle", "planning", "dialogue"])
        self.assertEqual([event.task_id for event in events], [None, "task-a", "task-b"])
        self.assertEqual([event.prev_hash for event in events], [first.hash, events[0].hash, events[1].hash])
        self.assertEqual([event.key_id for event in events], ["batch-key"] * 3)
        self.assertTrue(all(event.signature for event in events))
        self.assertEqual(statements.count("BEGIN IMMEDIATE"), 1)
        self.assertEqual(statements.count("COMMIT"), 1)
        self.assertFalse(self.log.db.in_transaction)
        reopened = EventLog(self.path, audit_key="batch-secret", key_id="batch-key")
        try:
            self.assertEqual(reopened.read(), [first, *events])
            self.assertEqual(reopened.head_hash(), events[-1].hash)
            report = reopened.verify_chain()
            self.assertTrue(report.ok, report.describe())
            self.assertEqual(report.signed, 4)
        finally:
            reopened.close()

    def test_hash_signature_and_stored_json_keep_existing_format(self):
        actor = Actor.agent("writer", "\u5f00\u53d1")
        payload = {"z": [1, {"b": False, "a": None}], "a": "\u623f\u95f4"}
        entry = self.entry(actor=actor, task_id="task-a", payload=payload)
        with patch("cater.events.uuid.uuid4") as uuid4, patch("cater.events.datetime") as clock:
            uuid4.return_value.hex = "0123456789abcdef0123456789abcdef"
            clock.now.return_value.isoformat.return_value = "2026-10-15T01:02:03.123456+00:00"
            event = self.log.emit_many([entry], expected_head=GENESIS_HASH)[0]

        body = {
            "id": event.id, "ts": event.ts, "task_id": "task-a", "actor": actor.to_dict(),
            "type": "msg.said", "category": "dialogue", "payload": payload, "prev_hash": GENESIS_HASH,
        }
        raw = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        fingerprint = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        signature = hmac.new(b"batch-secret", fingerprint.encode("utf-8"), hashlib.sha256).hexdigest()
        self.assertEqual(event.hash, fingerprint)
        self.assertEqual(event.signature, signature)
        row = self.log.db.execute("SELECT actor_json, payload_json FROM events").fetchone()
        self.assertEqual(row["actor_json"], json.dumps(actor.to_dict(), ensure_ascii=False, sort_keys=True))
        self.assertEqual(row["payload_json"], json.dumps(payload, ensure_ascii=False, sort_keys=True))
        self.assertEqual(self.log.read(), [event])

    def test_emit_delegates_and_preserves_defaults_and_payload_copy(self):
        with patch.object(self.log, "emit_many", wraps=self.log.emit_many) as emit_many:
            event = self.log.emit(actor=Actor.human(), type="task.created", expected_head=GENESIS_HASH)
        emit_many.assert_called_once_with(
            [{"actor": Actor.human(), "type": "task.created", "task_id": None, "payload": None}],
            expected_head=GENESIS_HASH,
        )
        self.assertEqual(event.payload, {})
        self.assertIsNone(event.task_id)
        payload = {"text": "original"}
        events = self.log.emit_many([self.entry(), self.entry(payload=None), self.entry(payload=payload)])
        payload["text"] = "changed"
        self.assertEqual([item.payload for item in events], [{}, {}, {"text": "original"}])
        self.assertEqual(self.log.read(), [event, *events])

    def test_unsigned_batches_keep_null_signature_fields(self):
        log = EventLog()
        try:
            events = log.emit_many([self.entry(), self.entry()], expected_head=GENESIS_HASH)
            self.assertEqual([event.key_id for event in events], [None, None])
            self.assertEqual([event.signature for event in events], [None, None])
            self.assertEqual(events[0].prev_hash, GENESIS_HASH)
            self.assertEqual(events[1].prev_hash, events[0].hash)
            report = log.verify_chain()
            self.assertTrue(report.ok, report.describe())
            self.assertEqual(report.signed, 0)
        finally:
            log.close()

    def test_unknown_type_anywhere_leaves_no_partial_batch(self):
        first = self.log.emit(**self.entry())
        for index in range(3):
            with self.subTest(index=index):
                entries = [self.entry() for _ in range(3)]
                entries[index]["type"] = "not.registered"
                with self.assertRaisesRegex(UnknownEventType, "not.registered"):
                    self.log.emit_many(entries, expected_head=first.hash)
                self.assert_unchanged([first])

    def test_serialization_failure_rolls_back_prior_entries_and_allows_retry(self):
        first = self.log.emit(**self.entry())
        circular = {}
        circular["self"] = circular
        for payload, error in (({"bad": object()}, TypeError), (circular, ValueError)):
            with self.subTest(error=error):
                with self.assertRaises(error):
                    self.log.emit_many([self.entry(), self.entry(payload=payload)], expected_head=first.hash)
                self.assert_unchanged([first])
        events = self.log.emit_many([self.entry(), self.entry()], expected_head=first.hash)
        self.assertEqual([event.seq for event in events], [2, 3])
        self.assertEqual(events[0].prev_hash, first.hash)
        self.assertTrue(self.log.verify_chain().ok)

    def test_insert_failure_rolls_back_entire_batch(self):
        first = self.log.emit(**self.entry())
        with patch("cater.events.uuid.uuid4") as uuid4:
            uuid4.return_value.hex = "duplicate-event-id"
            with self.assertRaises(sqlite3.IntegrityError):
                self.log.emit_many([self.entry(), self.entry()], expected_head=first.hash)
        self.assert_unchanged([first])
        event = self.log.emit(**self.entry(), expected_head=first.hash)
        self.assertEqual(event.seq, 2)

    def test_interrupted_batch_rolls_back_and_releases_transaction(self):
        first = self.log.emit(**self.entry())
        fingerprint = self.log._fingerprint

        def interrupt_second(**kwargs):
            if kwargs["payload"].get("interrupt"):
                raise KeyboardInterrupt
            return fingerprint(**kwargs)

        with patch.object(self.log, "_fingerprint", side_effect=interrupt_second):
            with self.assertRaises(KeyboardInterrupt):
                self.log.emit_many([self.entry(), self.entry(payload={"interrupt": True})])
        self.assert_unchanged([first])
        self.assertEqual(self.log.emit(**self.entry()).seq, 2)

    def test_failing_input_iterable_does_not_write(self):
        def entries():
            yield self.entry()
            raise RuntimeError("input failed")

        with self.assertRaisesRegex(RuntimeError, "input failed"):
            self.log.emit_many(entries())
        self.assert_unchanged([])

    def test_expected_head_is_global_and_stale_batch_writes_nothing(self):
        first = self.log.emit(**self.entry(task_id="task-a"))
        other = EventLog(self.path, audit_key="batch-secret", key_id="batch-key")
        try:
            concurrent = other.emit(**self.entry(task_id="task-b"))
        finally:
            other.close()
        with self.assertRaises(HeadChanged):
            self.log.emit_many([self.entry(task_id="task-a"), self.entry()], expected_head=first.hash)
        self.assert_unchanged([first, concurrent])
        with self.assertRaises(HeadChanged):
            self.log.emit(**self.entry(), expected_head=first.hash)
        self.assert_unchanged([first, concurrent])

    def test_empty_batch_is_a_noop_even_with_stale_expected_head(self):
        for populated in (False, True):
            with self.subTest(populated=populated):
                if populated:
                    self.log.emit(**self.entry())
                before = self.log.read()
                statements = []
                self.log.db.set_trace_callback(statements.append)
                try:
                    self.assertEqual(self.log.emit_many([]), [])
                    self.assertEqual(self.log.emit_many(iter(()), expected_head="stale"), [])
                finally:
                    self.log.db.set_trace_callback(None)
                self.assertEqual(statements, [])
                self.assert_unchanged(before)

    def test_concurrent_connections_keep_batches_contiguous(self):
        barrier = threading.Barrier(3)

        def write(writer):
            log = EventLog(self.path, audit_key="batch-secret", key_id="batch-key")
            try:
                result = []
                for batch in range(4):
                    barrier.wait(timeout=5)
                    entries = [self.entry(payload={"writer": writer, "batch": batch, "item": item})
                               for item in range(3)]
                    result.append(log.emit_many(entries))
                return result
            finally:
                log.close()

        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(write, writer) for writer in range(3)]
            batches = [batch for future in futures for batch in future.result(timeout=20)]
        events = self.log.read()
        self.assertEqual(len(events), 36)
        for batch in batches:
            self.assertEqual([event.seq for event in batch], list(range(batch[0].seq, batch[0].seq + 3)))
            self.assertEqual(events[batch[0].seq - 1:batch[-1].seq], batch)
        report = self.log.verify_chain()
        self.assertTrue(report.ok, report.describe())
        self.assertEqual(report.signed, 36)

    def test_concurrent_batches_with_same_expected_head_have_one_winner(self):
        barrier = threading.Barrier(2)

        def write(writer):
            log = EventLog(self.path, audit_key="batch-secret", key_id="batch-key")
            try:
                head = log.head_hash()
                barrier.wait(timeout=5)
                try:
                    return log.emit_many(
                        [self.entry(payload={"writer": writer, "item": item}) for item in range(3)],
                        expected_head=head,
                    )
                except HeadChanged:
                    self.assertFalse(log.db.in_transaction)
                    return None
            finally:
                log.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(write, writer) for writer in range(2)]
            results = [future.result(timeout=10) for future in futures]
        winners = [result for result in results if result is not None]
        self.assertEqual(len(winners), 1)
        self.assertEqual(self.log.read(), winners[0])
        self.assertEqual(self.log.count(), 3)
        self.assertTrue(self.log.verify_chain().ok)
