"""Helpers for displaying agent message relationships."""

from __future__ import annotations

from typing import Any


def message_relation(payload: dict[str, Any]) -> str:
    parts = []
    if payload.get("target"):
        parts.append(f"target {payload['target']}")
    if payload.get("reply_to"):
        parts.append(f"reply_to #{payload['reply_to']}")
    return " | ".join(parts)
