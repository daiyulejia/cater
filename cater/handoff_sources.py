"""Deterministic choices over authorized handoff text, without storing new authority."""

from __future__ import annotations

import hashlib
import json


MAX_QUOTE_BYTES = 512
MAX_PARTS = 4


def choices(sources, revision):
    """Yield bounded original excerpts; IDs are selectors, not approval tokens."""
    for event, parts, metadata in sources.values():
        count = 0
        for index, part in enumerate(parts):
            if not isinstance(part, str):
                continue
            try:
                body = part.encode("utf-8")
            except UnicodeError:
                continue
            quote = body[:MAX_QUOTE_BYTES].decode("utf-8", errors="ignore").strip()
            if not quote:
                continue
            key = json.dumps([revision, event.hash, index, quote], ensure_ascii=True).encode("ascii")
            yield {"evidence_id": "ev1_" + hashlib.sha256(key).hexdigest(),
                   "quote": quote, "quote_truncated": len(body) > MAX_QUOTE_BYTES}, event, metadata
            count += 1
            if count == MAX_PARTS:
                break
