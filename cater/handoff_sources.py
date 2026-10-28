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
        tails = []
        offered = set()
        for index, part in enumerate(parts):
            if not isinstance(part, str):
                continue
            try:
                body = part.encode("utf-8")
            except UnicodeError:
                continue
            quote = body[:MAX_QUOTE_BYTES].decode("utf-8", errors="ignore").strip()
            if len(body) > MAX_QUOTE_BYTES:
                tail = body[-MAX_QUOTE_BYTES:].decode("utf-8", errors="ignore").strip()
                if tail and tail != quote:
                    tails.append((index, tail))
            if not quote:
                continue
            key = json.dumps([revision, event.hash, index, quote], ensure_ascii=True).encode("ascii")
            yield {"evidence_id": "ev1_" + hashlib.sha256(key).hexdigest(),
                   "quote": quote, "quote_truncated": len(body) > MAX_QUOTE_BYTES}, event, metadata
            offered.add(quote)
            count += 1
            if count == MAX_PARTS:
                break
        # Keep all previously offered prefixes and their IDs before adding
        # any suffix, so existing selections remain valid for this scope.
        for index, tail in tails:
            if count >= MAX_PARTS:
                break
            if tail in offered:
                continue
            key = json.dumps([revision, event.hash, index, tail], ensure_ascii=True).encode("ascii")
            yield {"evidence_id": "ev1_" + hashlib.sha256(key).hexdigest(),
                   "quote": tail, "quote_truncated": True, "quote_position": "end"}, event, metadata
            offered.add(tail)
            count += 1
