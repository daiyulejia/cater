"""Agent profile projection from the event stream."""

from __future__ import annotations

from typing import Any

from .events import Event


def agent_profiles(events: list[Event]) -> dict[str, dict[str, Any]]:
    profiles: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.type in {"msg.said", "msg.challenge"}:
            profile = profiles.setdefault(event.actor.id, _empty_profile(event.actor.id, event.actor.role))
            profile["messages"] += 1
            if event.payload.get("stance") == "challenge":
                profile["challenges"] += 1
            else:
                profile["supports"] += 1
            claim = str(event.payload.get("claim") or "")
            if claim:
                profile["last_claim"] = claim
            driver = event.payload.get("driver")
            if driver:
                profile["drivers"].add(str(driver))
        elif event.type == "driver.failed":
            actor = str(event.payload.get("role") or event.payload.get("driver") or "unknown")
            profile = profiles.setdefault(actor, _empty_profile(actor, actor))
            profile["driver_failures"] += 1
            profile["last_failure"] = str(event.payload.get("message") or "")

    return {key: _finalize_profile(value) for key, value in sorted(profiles.items())}


def _empty_profile(actor_id: str, role: str) -> dict[str, Any]:
    return {
        "id": actor_id,
        "role": role,
        "messages": 0,
        "supports": 0,
        "challenges": 0,
        "driver_failures": 0,
        "drivers": set(),
        "last_claim": "",
        "last_failure": "",
    }


def _finalize_profile(profile: dict[str, Any]) -> dict[str, Any]:
    data = dict(profile)
    data["drivers"] = sorted(data["drivers"])
    return data
