from __future__ import annotations

import hashlib
import io
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from cater.artifact_blobs import BlobError, MAX_BLOB_BYTES, put_blob, read_blob
from cater.events import Actor, EventLog, UnknownEventType


class ArtifactBlobTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "events.db"
        self.log = EventLog(self.path)
        self.addCleanup(self.log.close)
        self.body = b"private-project-content\x00\xff"
        self.sha = hashlib.sha256(self.body).hexdigest()

    def schema(self):
        return [tuple(row) for row in self.log.db.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
        )]

    def seed(self, content):
        # Simulate legacy/damaged storage without disabling production guards.
        self.log.db.execute(
            "CREATE TABLE artifact_blobs(sha256 TEXT PRIMARY KEY, content BLOB NOT NULL)"
        )
        self.log.db.execute(
            "INSERT INTO artifact_blobs VALUES (?, ?)", (self.sha, content)
        )
        self.log.db.commit()

    def test_put_read_and_reopen_project_zip_outside_events(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("project/main.py", self.body)
        body = buffer.getvalue()
        sha = put_blob(self.log, body)
        self.assertEqual(sha, hashlib.sha256(body).hexdigest())
        self.assertEqual(read_blob(self.log, sha, len(body)), body)
        self.assertEqual(self.log.count(), 0)
        self.assertFalse(self.log.db.in_transaction)
        reopened = EventLog(self.path)
        try:
            self.assertEqual(read_blob(reopened, sha, len(body)), body)
            self.assertEqual(reopened.count(), 0)
        finally:
            reopened.close()

    def test_duplicate_is_verified_and_does_not_rewrite_content(self):
        self.assertEqual(put_blob(self.log, self.body), self.sha)
        changes = self.log.db.total_changes
        self.assertEqual(put_blob(self.log, self.body), self.sha)
        self.assertEqual(self.log.db.total_changes, changes)
        self.assertEqual(self.log.db.execute(
            "SELECT COUNT(*) FROM artifact_blobs"
        ).fetchone()[0], 1)
        self.assertEqual(read_blob(self.log, self.sha, len(self.body)), self.body)

    def test_read_is_query_only_and_works_with_query_only_connection(self):
        put_blob(self.log, self.body)
        schema = self.schema()
        changes = self.log.db.total_changes
        statements = []
        self.log.db.execute("PRAGMA query_only = ON")
        self.log.db.set_trace_callback(statements.append)
        try:
            self.assertEqual(read_blob(self.log, self.sha, len(self.body)), self.body)
        finally:
            self.log.db.set_trace_callback(None)
            self.log.db.execute("PRAGMA query_only = OFF")
        self.assertTrue(statements)
        self.assertTrue(all(sql.lstrip().upper().startswith("SELECT ") for sql in statements))
        self.assertEqual(self.schema(), schema)
        self.assertEqual(self.log.db.total_changes, changes)
        self.assertFalse(self.log.db.in_transaction)

    def test_schema_absent_does_not_write(self):
        schema = self.schema()
        changes = self.log.db.total_changes
        before = self.path.read_bytes()
        statements = []
        self.log.db.execute("PRAGMA query_only = ON")
        self.log.db.set_trace_callback(statements.append)
        try:
            with self.assertRaisesRegex(BlobError, "schema is absent"):
                read_blob(self.log, self.sha, len(self.body))
        finally:
            self.log.db.set_trace_callback(None)
            self.log.db.execute("PRAGMA query_only = OFF")
        self.assertTrue(all(sql.lstrip().upper().startswith("SELECT ") for sql in statements))
        self.assertEqual(self.schema(), schema)
        self.assertEqual(self.log.db.total_changes, changes)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(self.log.db.in_transaction)

    def test_missing_blob_and_wrong_expected_length(self):
        put_blob(self.log, self.body)
        with self.assertRaisesRegex(BlobError, "not found"):
            read_blob(self.log, "0" * 64, 1)
        for size in (len(self.body) - 1, len(self.body) + 1):
            with self.subTest(size=size), self.assertRaisesRegex(BlobError, "integrity"):
                read_blob(self.log, self.sha, size)

    def test_corrupt_hash_rejected_by_read_and_put_without_replacement(self):
        damaged = b"x" * len(self.body)
        self.seed(damaged)
        schema = self.schema()
        with self.assertRaisesRegex(BlobError, "integrity"):
            read_blob(self.log, self.sha, len(self.body))
        with self.assertRaisesRegex(BlobError, "integrity"):
            put_blob(self.log, self.body)
        self.assertEqual(self.schema(), schema)
        self.assertEqual(self.log.db.execute(
            "SELECT content FROM artifact_blobs"
        ).fetchone()[0], damaged)
        self.assertFalse(self.log.db.in_transaction)

    def test_non_blob_and_wrong_length_storage_rejected(self):
        for content in (self.body.decode("latin1"), 123, 1.5, b"short"):
            with self.subTest(content_type=type(content)):
                log = EventLog()
                try:
                    log.db.execute(
                        "CREATE TABLE artifact_blobs(sha256 TEXT PRIMARY KEY, content BLOB NOT NULL)"
                    )
                    log.db.execute("INSERT INTO artifact_blobs VALUES (?, ?)", (self.sha, content))
                    log.db.commit()
                    with self.assertRaises(BlobError):
                        read_blob(log, self.sha, len(self.body))
                    with self.assertRaises(BlobError):
                        put_blob(log, self.body)
                    self.assertEqual(log.db.execute(
                        "SELECT content FROM artifact_blobs"
                    ).fetchone()[0], content)
                finally:
                    log.close()

    def test_simulated_hash_collision_still_checks_exact_bytes(self):
        self.seed(self.body)
        with patch("cater.artifact_blobs.hashlib.sha256") as digest:
            digest.return_value.hexdigest.return_value = self.sha
            with self.assertRaisesRegex(BlobError, "conflicts"):
                put_blob(self.log, b"x" * len(self.body))
        self.assertEqual(read_blob(self.log, self.sha, len(self.body)), self.body)

    def test_outer_rollback_removes_lazy_schema_and_event(self):
        before = self.schema()
        with self.assertRaisesRegex(RuntimeError, "cancel"):
            with self.log.atomic():
                sha = put_blob(self.log, self.body)
                self.log.emit(actor=Actor.system(), type="deliverable.published",
                              payload={"sha256": sha, "content_bytes": len(self.body)})
                self.assertEqual(read_blob(self.log, sha, len(self.body)), self.body)
                raise RuntimeError("cancel")
        self.assertEqual(self.schema(), before)
        self.assertEqual(self.log.count(), 0)
        self.assertFalse(self.log.db.in_transaction)

    def test_failed_publication_rolls_back_new_blob_keeps_previous_blob(self):
        old_sha = put_blob(self.log, b"previous")
        with self.assertRaises(UnknownEventType):
            with self.log.atomic():
                put_blob(self.log, self.body)
                self.log.emit(actor=Actor.system(), type="invalid.publication")
        with self.assertRaisesRegex(BlobError, "not found"):
            read_blob(self.log, self.sha, len(self.body))
        self.assertEqual(read_blob(self.log, old_sha, len(b"previous")), b"previous")
        self.assertEqual(self.log.count(), 0)
        self.assertFalse(self.log.db.in_transaction)

    def test_successful_publication_commits_only_reference_in_event(self):
        with self.log.atomic():
            sha = put_blob(self.log, self.body)
            event = self.log.emit(
                actor=Actor.system(), type="deliverable.published",
                payload={"sha256": sha, "content_bytes": len(self.body)},
            )
        self.assertEqual(self.log.read(), [event])
        self.assertEqual(event.payload, {"sha256": self.sha, "content_bytes": len(self.body)})
        self.assertTrue(self.log.verify_chain().ok)
        self.assertEqual(read_blob(self.log, sha, len(self.body)), self.body)

    def test_failed_nested_put_preserves_outer_transaction(self):
        self.seed(b"damaged")
        with self.log.atomic():
            event = self.log.emit(actor=Actor.system(), type="msg.said", payload={"text": "kept"})
            with self.assertRaises(BlobError):
                put_blob(self.log, self.body)
            self.assertTrue(self.log.db.in_transaction)
            good_sha = put_blob(self.log, b"good")
        self.assertEqual(self.log.read(), [event])
        self.assertEqual(read_blob(self.log, good_sha, 4), b"good")

    def test_immutable_triggers_block_update_delete_and_replace(self):
        put_blob(self.log, self.body)
        triggers = {row[0] for row in self.log.db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'artifact_blobs'"
        )}
        self.assertTrue({"cater_artifact_blobs_no_update", "cater_artifact_blobs_no_delete"} <= triggers)
        statements = [
            ("UPDATE artifact_blobs SET content = ? WHERE sha256 = ?", (b"bad", self.sha)),
            ("UPDATE artifact_blobs SET sha256 = ? WHERE sha256 = ?", ("0" * 64, self.sha)),
            ("DELETE FROM artifact_blobs WHERE sha256 = ?", (self.sha,)),
            ("INSERT OR REPLACE INTO artifact_blobs VALUES (?, ?)", (self.sha, b"bad")),
        ]
        for sql, params in statements:
            with self.subTest(sql=sql):
                with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                    with self.log.atomic():
                        self.log.db.execute(sql, params)
                self.assertEqual(read_blob(self.log, self.sha, len(self.body)), self.body)

    def test_size_limit_accepts_exact_boundary_and_rejects_overflow(self):
        self.assertEqual(MAX_BLOB_BYTES, 65 * 1024 * 1024)
        body = b"x" * MAX_BLOB_BYTES
        sha = put_blob(self.log, body)
        self.assertEqual(read_blob(self.log, sha, MAX_BLOB_BYTES), body)
        with self.assertRaisesRegex(BlobError, "maximum size"):
            put_blob(self.log, body + b"x")
        with self.assertRaises(BlobError):
            read_blob(self.log, sha, MAX_BLOB_BYTES + 1)
        self.assertEqual(self.log.db.execute(
            "SELECT COUNT(*) FROM artifact_blobs"
        ).fetchone()[0], 1)

    def test_stored_oversize_content_rejected(self):
        body = b"x" * (MAX_BLOB_BYTES + 1)
        self.seed(body)
        with self.assertRaisesRegex(BlobError, "integrity"):
            read_blob(self.log, self.sha, len(self.body))

    def test_strict_body_type_rejected_before_any_sql(self):
        class BytesSubclass(bytes):
            pass

        statements = []
        self.log.db.set_trace_callback(statements.append)
        try:
            for body in (None, "private-project-content", bytearray(self.body),
                         memoryview(self.body), BytesSubclass(self.body), 1, True):
                with self.subTest(body_type=type(body)), self.assertRaises(BlobError):
                    put_blob(self.log, body)
        finally:
            self.log.db.set_trace_callback(None)
        self.assertEqual(statements, [])

    def test_strict_hash_and_expected_bytes_rejected_before_any_sql(self):
        class IntSubclass(int):
            pass

        invalid_hashes = (None, 1, b"a" * 64, "A" * 64, "g" * 64,
                          "a" * 63, "a" * 65, self.sha + "\n", " " + self.sha)
        invalid_sizes = (None, True, False, 0, -1, 1.0, "1", IntSubclass(1), MAX_BLOB_BYTES + 1)
        statements = []
        self.log.db.set_trace_callback(statements.append)
        try:
            for sha in invalid_hashes:
                with self.subTest(sha=sha), self.assertRaises(BlobError):
                    read_blob(self.log, sha, len(self.body))
            for size in invalid_sizes:
                with self.subTest(size=size), self.assertRaises(BlobError):
                    read_blob(self.log, self.sha, size)
        finally:
            self.log.db.set_trace_callback(None)
        self.assertEqual(statements, [])

    def test_empty_bytes_can_be_stored_but_read_requires_positive_size(self):
        sha = put_blob(self.log, b"")
        self.assertEqual(sha, hashlib.sha256(b"").hexdigest())
        self.assertEqual(put_blob(self.log, b""), sha)
        with self.assertRaises(BlobError):
            read_blob(self.log, sha, 0)

    def test_oversized_or_nonblob_stored_row_is_filtered_before_python_receives_content(self):
        digest = hashlib.sha256(b"x").hexdigest()
        for damaged in (b"x" * 64, "x" * 64):
            with self.subTest(kind=type(damaged).__name__):
                log, received = EventLog(), []
                try:
                    log.db.execute("CREATE TABLE artifact_blobs(sha256 TEXT PRIMARY KEY, content BLOB NOT NULL)")
                    log.db.execute("INSERT INTO artifact_blobs VALUES (?, ?)", (digest, damaged))
                    log.db.commit()
                    def inspect_row(cursor, row):
                        received.extend(len(value) for value in row if type(value) in (bytes, str) and len(value) > 4)
                        return row
                    log.db.row_factory = inspect_row
                    with patch("cater.artifact_blobs.MAX_BLOB_BYTES", 4):
                        with self.assertRaises(BlobError):
                            read_blob(log, digest, 1)
                        with self.assertRaises(BlobError):
                            put_blob(log, b"x")
                    self.assertEqual(received, [])
                finally:
                    log.close()

    def test_errors_do_not_echo_content_or_database_error_details(self):
        self.assertTrue(issubclass(BlobError, ValueError))
        self.seed(self.body.decode("latin1"))
        for operation in (lambda: put_blob(self.log, self.body),
                          lambda: read_blob(self.log, self.sha, len(self.body))):
            with self.assertRaises(BlobError) as caught:
                operation()
            self.assertNotIn("private-project-content", str(caught.exception))
        self.log.db.execute(
            "CREATE TRIGGER fail_blob_read_write BEFORE INSERT ON artifact_blobs "
            "BEGIN SELECT RAISE(ABORT, 'private-project-content'); END"
        )
        with self.assertRaisesRegex(BlobError, "storage write failed") as caught:
            put_blob(self.log, b"new content")
        self.assertNotIn("private-project-content", str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)


if __name__ == "__main__":
    unittest.main()
