"""Pure fold from events to current task snapshots."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .events import Event
from .directions import changes_direction
from .usage import usage_source, valid_token_count


@dataclass
class TaskSnapshot:
    id: str
    project_id: str | None = None
    goal: str = ""
    acceptance: list[str] = field(default_factory=list)
    status: str = "pending"
    messages: list[dict[str, Any]] = field(default_factory=list)
    driver_failures: list[dict[str, Any]] = field(default_factory=list)
    budget_exhaustions: list[dict[str, Any]] = field(default_factory=list)
    cabinet: dict[str, Any] | None = None
    proposal: dict[str, Any] | None = None
    brief: dict[str, Any] | None = None
    decision: dict[str, Any] | None = None
    commands: list[dict[str, Any]] = field(default_factory=list)
    reviews: list[dict[str, Any]] = field(default_factory=list)
    interpretation: dict[str, Any] | None = None
    route: dict[str, Any] | None = None
    mission: dict[str, Any] | None = None
    plan: dict[str, Any] | None = None
    assignments: list[dict[str, Any]] = field(default_factory=list)
    deliverables: list[dict[str, Any]] = field(default_factory=list)
    diagnoses: list[dict[str, Any]] = field(default_factory=list)
    handoffs: list[dict[str, Any]] = field(default_factory=list)
    verifications: list[dict[str, Any]] = field(default_factory=list)
    work: dict[str, Any] | None = None
    discipline_flags: list[dict[str, Any]] = field(default_factory=list)
    tokens: int = 0
    token_usage: dict[str, int] = field(default_factory=dict)


def fold(events: list[Event]) -> dict[str, TaskSnapshot]:
    tasks: dict[str, TaskSnapshot] = {}
    for event in events:
        if not event.task_id:
            continue
        task = tasks.setdefault(event.task_id, TaskSnapshot(id=event.task_id))
        payload = event.payload
        if event.type == "task.created":
            task.project_id = payload.get("project_id")
            task.goal = str(payload.get("goal", ""))
            task.acceptance = list(payload.get("acceptance", []) or [])
            task.status = "pending"
        elif event.type == "task.project_bound":
            task.project_id = payload.get("project_id")
        elif event.type == "human.commanded":
            task.commands.append({"seq": event.seq, **payload})
            if payload.get("goal"):
                task.goal = str(payload["goal"])
            if "acceptance" in payload:
                task.acceptance = list(payload.get("acceptance") or [])
            if changes_direction(payload):
                task.status = "running"
        elif event.type == "task.started":
            task.status = "running"
        elif event.type == "task.interpreted":
            task.interpretation = {"seq": event.seq, **payload}
        elif event.type == "task.routed":
            task.route = {"seq": event.seq, **payload}
        elif event.type == "planning.requested":
            task.route = {"seq": event.seq, "route": "planning", "reason": "等待店长核对工作计划。"}
        elif event.type == "mission.created":
            task.mission = {"seq": event.seq, **payload}
        elif event.type == "task.decomposed":
            task.plan = {"seq": event.seq, **payload}
        elif event.type == "subtask.assigned":
            task.assignments.append({"seq": event.seq, **payload})
        elif event.type == "deliverable.published":
            task.deliverables.append({"seq": event.seq, **payload, "actor": event.actor.id})
        elif event.type == "diagnosis.recorded":
            task.diagnoses.append({"seq": event.seq, **payload})
        elif event.type == "handoff.published":
            task.handoffs.append({"seq": event.seq, **payload})
        elif event.type == "verification.completed":
            task.verifications.append({"seq": event.seq, **payload})
        elif event.type in {"plan.proposed", "plan.amended"}:
            task.plan = {"seq": event.seq, **payload}
        elif event.type == "work.plan_created":
            task.work = {"plan_seq": event.seq}
            if payload.get("source_seq") == 0:
                task.plan = {"seq": event.seq, "subtasks": payload["items"], "work_contract": True}
            task.status = "running"
        elif event.type in {"work.claimed", "work.submitted", "work.reviewed", "work.invalidated", "work.failed"}:
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
            tokens = payload.get("tokens")
            task.tokens += tokens if valid_token_count(tokens) else 0
            source = usage_source(tokens, payload.get("token_usage"))
            task.token_usage[source] = task.token_usage.get(source, 0) + 1
        elif event.type == "driver.failed":
            task.driver_failures.append({"seq": event.seq, **payload})
        elif ((event.type == "question.finished" and event.actor.id == "explainer")
              or (event.type == "conversation.finished" and event.actor.id == "conversation")) and event.actor.kind == "system" and "driver" in payload:
            tokens = payload.get("tokens")
            task.tokens += tokens if valid_token_count(tokens) else 0
            source = usage_source(tokens, payload.get("token_usage"))
            task.token_usage[source] = task.token_usage.get(source, 0) + 1
        elif event.type == "driver.budget_exhausted":
            task.budget_exhaustions.append({"seq": event.seq, **payload})
        elif event.type == "agent.discipline_flagged":
            task.discipline_flags.append({"seq": event.seq, **payload})
        elif event.type == "escalation.triggered":
            task.status = "escalated"
        elif event.type == "cabinet.voted":
            task.cabinet = payload.get("tally") if isinstance(payload.get("tally"), dict) else payload
        elif event.type == "decision.proposed":
            task.proposal = payload
        elif event.type in {"agent.decided", "supreme.decided"}:
            task.decision = payload
            resolution = payload.get("resolution")
            if resolution == "accepted":
                task.status = "decided"
            elif resolution == "rework":
                task.status = "rework"
            else:
                task.status = "running"
        elif event.type == "brief.generated":
            task.brief = payload.get("brief") if isinstance(payload.get("brief"), dict) else payload
        elif event.type == "human.decided":
            task.decision = payload
            resolution = payload.get("resolution")
            task.status = "done" if resolution == "done" else "running"
        elif event.type == "review.generated":
            review = payload.get("review") if isinstance(payload.get("review"), dict) else payload
            task.reviews.append({"seq": event.seq, **review})
    return tasks
