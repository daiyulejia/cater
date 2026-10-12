"""Pure fold from events to current task snapshots."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .events import Event


@dataclass
class TaskSnapshot:
    id: str
    goal: str = ""
    acceptance: list[str] = field(default_factory=list)
    status: str = "pending"
    messages: list[dict[str, Any]] = field(default_factory=list)
    cabinet: dict[str, Any] | None = None
    brief: dict[str, Any] | None = None
    decision: dict[str, Any] | None = None
    tokens: int = 0


def fold(events: list[Event]) -> dict[str, TaskSnapshot]:
    tasks: dict[str, TaskSnapshot] = {}
    for event in events:
        if not event.task_id:
            continue
        task = tasks.setdefault(event.task_id, TaskSnapshot(id=event.task_id))
        payload = event.payload
        if event.type == "task.created":
            task.goal = str(payload.get("goal", ""))
            task.acceptance = list(payload.get("acceptance", []) or [])
            task.status = "pending"
        elif event.type == "task.started":
            task.status = "running"
        elif event.type in {"msg.said", "msg.challenge"}:
            task.messages.append(
                {
                    "seq": event.seq,
                    "actor": event.actor.id,
                    "role": event.actor.role,
                    "type": event.type,
                    **payload,
                }
            )
            task.tokens += int(payload.get("tokens") or 0)
        elif event.type == "escalation.triggered":
            task.status = "escalated"
        elif event.type == "cabinet.voted":
            task.cabinet = payload.get("tally") if isinstance(payload.get("tally"), dict) else payload
        elif event.type == "brief.generated":
            task.brief = payload.get("brief") if isinstance(payload.get("brief"), dict) else payload
        elif event.type == "human.decided":
            task.decision = payload
            resolution = payload.get("resolution")
            task.status = "done" if resolution == "done" else "running"
    return tasks
