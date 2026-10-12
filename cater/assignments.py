"""Assignment helpers for task distribution projections."""

from __future__ import annotations

from typing import Any


def latest_assignments_by_owner(assignments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_owner: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for item in assignments:
        owner = str(item.get("owner") or "")
        if not owner:
            continue
        if owner not in by_owner:
            order.append(owner)
        by_owner[owner] = item
    return [by_owner[owner] for owner in order if owner in by_owner]
