"""Decision ladder projection."""

from __future__ import annotations

from typing import Any

from .events import Event


def decision_ladder(events: list[Event]) -> list[dict[str, Any]]:
    steps = [
        {
            "id": "battle",
            "label": "Collaboration",
            "status": "pending",
            "detail": "agents have not contributed",
        },
        {"id": "cabinet", "label": "Cabinet", "status": "pending", "detail": "vote not recorded"},
        {"id": "supreme", "label": "Supreme", "status": "skipped", "detail": "not needed"},
        {"id": "human", "label": "Human", "status": "skipped", "detail": "not needed"},
    ]
    for event in events:
        if event.type in {"msg.said", "msg.challenge"}:
            steps[0]["status"] = "done"
            steps[0]["detail"] = "agents exchanged work outputs and positions"
        elif event.type == "cabinet.voted":
            tally = event.payload.get("tally", {})
            steps[1]["status"] = str(tally.get("status", "done"))
            steps[1]["detail"] = str(tally.get("reason", "cabinet vote recorded"))
        elif event.type == "brief.generated":
            brief = event.payload.get("brief", {})
            governance = brief.get("governance", {}) if isinstance(brief, dict) else {}
            if governance.get("cabinet_enabled") is False:
                steps[1]["status"] = "skipped"
                steps[1]["detail"] = "No cabinet seats configured; the leader handled the review request."
            elif governance.get("status") == "not_needed":
                steps[1]["status"] = "skipped"
                steps[1]["detail"] = str(governance.get("reason", "governance not needed"))
        elif event.type == "supreme.decided":
            steps[2]["status"] = "done"
            steps[2]["detail"] = str(event.payload.get("reason", "supreme fallback decided"))
        elif event.type == "agent.decided":
            if event.payload.get("source") == "cabinet":
                steps[2]["detail"] = "not needed; cabinet decided"
        elif event.type == "escalation.triggered":
            steps[3]["status"] = "needed"
            steps[3]["detail"] = str(event.payload.get("reason", "human input requested"))
        elif event.type in {"human.commanded", "human.decided"}:
            steps[3]["status"] = "done"
            steps[3]["detail"] = str(
                event.payload.get("instruction") or event.payload.get("reason") or "human input recorded"
            )
    return steps
