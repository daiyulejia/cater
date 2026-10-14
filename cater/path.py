"""Organization path projection for task replay."""

from __future__ import annotations

from typing import Any

from .assignments import latest_assignments_by_owner
from .events import Event
from .fold import TaskSnapshot


def organization_path(task: TaskSnapshot, events: list[Event]) -> list[dict[str, Any]]:
    """Summarize how a task moved through Cater's organization."""
    return [
        _distribution_step(task),
        _collaboration_step(task),
        _governance_step(task),
        _decision_step(task, events),
    ]


def _distribution_step(task: TaskSnapshot) -> dict[str, Any]:
    active = latest_assignments_by_owner(task.assignments)
    modes = {str(item.get("mode") or "dispatch") for item in active}
    if not active:
        return {
            "id": "distribution",
            "label": "Distribution",
            "status": "not_set",
            "detail": "No explicit assignment path is recorded yet.",
        }
    if modes == {"direct"}:
        owner = ", ".join(str(item.get("owner")) for item in active if item.get("owner"))
        return {
            "id": "distribution",
            "label": "Distribution",
            "status": "direct",
            "detail": f"Direct worker assignment to {owner}.",
        }
    if task.mission or task.plan:
        source = (task.plan or {}).get("source")
        if source not in {"leader", "cabinet"}:
            source = (task.mission or {}).get("source")
        if source == "leader":
            return {
                "id": "distribution",
                "label": "Distribution",
                "status": "leader_dispatch",
                "detail": f"Leader dispatch produced {len(active)} active lane(s).",
            }
        if source == "cabinet":
            return {
                "id": "distribution",
                "label": "Distribution",
                "status": "cabinet_dispatch",
                "detail": f"Cabinet dispatch produced {len(active)} active lane(s).",
            }
    return {
        "id": "distribution",
        "label": "Distribution",
        "status": "assigned",
        "detail": f"{len(active)} active assignment(s) are recorded.",
    }


def _collaboration_step(task: TaskSnapshot) -> dict[str, Any]:
    if not task.messages:
        return {
            "id": "collaboration",
            "label": "Collaboration",
            "status": "pending",
            "detail": "Workers have not produced task messages yet.",
        }
    challenges = sum(1 for item in task.messages if item.get("type") == "msg.challenge")
    return {
        "id": "collaboration",
        "label": "Collaboration",
        "status": "active",
        "detail": f"{len(task.messages)} message(s), {challenges} challenge(s), {task.tokens} token(s).",
    }


def _governance_step(task: TaskSnapshot) -> dict[str, Any]:
    if task.cabinet:
        return {
            "id": "governance",
            "label": "Governance",
            "status": str(task.cabinet.get("status", "recorded")),
            "detail": str(task.cabinet.get("reason", "Cabinet vote recorded.")),
        }
    brief = task.brief or {}
    governance = brief.get("governance", {}) if isinstance(brief, dict) else {}
    status = governance.get("status")
    if status:
        return {
            "id": "governance",
            "label": "Governance",
            "status": str(status),
            "detail": str(governance.get("reason", "Governance status recorded.")),
        }
    return {
        "id": "governance",
        "label": "Governance",
        "status": "pending",
        "detail": "No governance signal has been recorded yet.",
    }


def _decision_step(task: TaskSnapshot, events: list[Event]) -> dict[str, Any]:
    if task.decision:
        source = str(task.decision.get("source") or "human")
        resolution = str(task.decision.get("resolution") or task.status)
        return {
            "id": "decision",
            "label": "Decision",
            "status": resolution,
            "detail": f"{source} decided: {task.decision.get('reason', '')}",
        }
    if any(event.type == "escalation.triggered" for event in events):
        return {
            "id": "decision",
            "label": "Decision",
            "status": "needs_human",
            "detail": "The organization could not safely decide this case.",
        }
    return {
        "id": "decision",
        "label": "Decision",
        "status": task.status,
        "detail": "No final decision recorded yet.",
    }
