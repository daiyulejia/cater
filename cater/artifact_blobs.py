"""Content-addressed bytes in the event database, outside event payloads.

Callers must authorize access through current-group publications, exact
immutable publications in the calling group's frozen background, or explicitly
selected same-project historical delivery references.
A digest is an integrity check, not an access credential.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3

from .events import EventLog


MAX_BLOB_BYTES = 65 * 1024 * 1024


class BlobError(ValueError):
    """Invalid blob input, unavailable storage, or failed integrity check."""


def _validate_content(content: object, sha256: str, expected_bytes: int) -> bytes:
    if (type(content) is not bytes or len(content) > MAX_BLOB_BYTES
            or len(content) != expected_bytes
            or hashlib.sha256(content).hexdigest() != sha256):
        raise BlobError("blob content failed integrity check")
    return content


def put_blob(log: EventLog, body: bytes) -> str:
    """Store bytes atomically; existing content must match exactly.

    Schema creation is lazy and rolls back with the caller's outer transaction.
    """
    if type(body) is not bytes:
        raise BlobError("blob body must be bytes")
    if len(body) > MAX_BLOB_BYTES:
        raise BlobError("blob body exceeds maximum size")
    sha256 = hashlib.sha256(body).hexdigest()
    try:
        with log.atomic():
            # executescript would commit an enclosing publication transaction.
            log.db.execute(
                "CREATE TABLE IF NOT EXISTS artifact_blobs("
                "sha256 TEXT PRIMARY KEY, content BLOB NOT NULL)"
            )
            log.db.execute(
                "CREATE TRIGGER IF NOT EXISTS cater_artifact_blobs_no_update "
                "BEFORE UPDATE ON artifact_blobs "
                "BEGIN SELECT RAISE(ABORT, 'artifact blobs are immutable'); END"
            )
            log.db.execute(
                "CREATE TRIGGER IF NOT EXISTS cater_artifact_blobs_no_delete "
                "BEFORE DELETE ON artifact_blobs "
                "BEGIN SELECT RAISE(ABORT, 'artifact blobs are immutable'); END"
            )
            # REPLACE can bypass DELETE triggers with recursive_triggers off.
            log.db.execute(
                "CREATE TRIGGER IF NOT EXISTS cater_artifact_blobs_no_replace "
                "BEFORE INSERT ON artifact_blobs "
                "WHEN EXISTS (SELECT 1 FROM artifact_blobs WHERE sha256 = NEW.sha256) "
                "BEGIN SELECT RAISE(ABORT, 'artifact blobs are immutable'); END"
            )
            row = log.db.execute(
                "SELECT content FROM artifact_blobs WHERE sha256 = ? "
                "AND typeof(content) = 'blob' AND length(content) = ? AND length(content) <= ?",
                (sha256, len(body), MAX_BLOB_BYTES)
            ).fetchone()
            if row is None:
                log.db.execute(
                    "INSERT INTO artifact_blobs(sha256, content) VALUES (?, ?)",
                    (sha256, body),
                )
            else:
                content = _validate_content(row[0], sha256, len(body))
                if content != body:
                    raise BlobError("blob content conflicts with existing bytes")
    except sqlite3.Error:
        raise BlobError("blob storage write failed") from None
    return sha256


def read_blob(log: EventLog, sha256: str, expected_bytes: int) -> bytes:
    """Read and verify bytes without creating schema or starting a write."""
    if type(sha256) is not str or re.fullmatch(r"[0-9a-f]{64}", sha256) is None:
        raise BlobError("blob sha256 must be 64 lowercase hexadecimal characters")
    if type(expected_bytes) is not int or not 0 < expected_bytes <= MAX_BLOB_BYTES:
        raise BlobError("blob expected_bytes must be a positive integer within the size limit")
    try:
        table = log.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'artifact_blobs'"
        ).fetchone()
        if table is None:
            raise BlobError("blob storage schema is absent")
        row = log.db.execute(
            "SELECT content FROM artifact_blobs WHERE sha256 = ? "
            "AND typeof(content) = 'blob' AND length(content) = ? AND length(content) <= ?",
            (sha256, expected_bytes, MAX_BLOB_BYTES)
        ).fetchone()
        if row is None and log.db.execute(
                "SELECT 1 FROM artifact_blobs WHERE sha256 = ?", (sha256,)).fetchone() is not None:
            raise BlobError("blob content failed integrity check")
    except sqlite3.Error:
        raise BlobError("blob storage read failed") from None
    if row is None:
        raise BlobError("blob not found")
    return _validate_content(row[0], sha256, expected_bytes)
