"""Bounded, replayable organization memory derived from published outcomes."""

from __future__ import annotations

from typing import Any

from .events import Event


def shared_deliverable_memory(
    events: list[Event],
    *,
    exclude_task_id: str | None = None,
    limit: int = 3,
) -> list[dict[str, Any]]:
    """Return recent cross-task outcomes without introducing hidden state.

    Only explicitly published deliverables are carried forward.  Messages,
    prompts, and model-private reasoning are intentionally not treated as
    organization memory.
    """
    if type(limit) is not int or limit < 0:
        raise ValueError("memory limit must be a non-negative integer")
    if limit == 0:
        return []
    items: list[dict[str, Any]] = []
    for event in events:
        if event.type != "deliverable.published" or event.task_id == exclude_task_id:
            continue
        title = str(event.payload.get("title") or "").strip()
        summary = str(event.payload.get("summary") or "").strip()
        if not title or not summary:
            continue
        items.append(
            {
                "seq": event.seq,
                "task_id": event.task_id,
                "actor": event.actor.id,
                "title": title[:160],
                "summary": summary[:320],
                "kind": str(event.payload.get("kind") or "")[:80],
            }
        )
    return items[-limit:]
