"""Non-invasive team availability; never probes models or guesses account balances."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from .drivers import OpenAICompatibleDriver, driver_mode
from .anthropic import AnthropicMessagesDriver
from .usage import valid_token_count

if TYPE_CHECKING:
    from .runtime import Runtime


def team_availability(rt: Runtime) -> dict[str, Any]:
    totals: dict[str, int] = {}
    budgeted = {name for name, driver in rt.drivers.items() if valid_token_count(getattr(driver, "token_budget", None))}
    for event in rt.log.iter_events(event_types=("msg.said", "msg.challenge", "question.finished", "conversation.finished", "collaboration.finished", "verification.planned", "workspace.model_called")) if budgeted else ():
        name = event.payload.get("driver")
        if isinstance(name, str) and name in budgeted and event.payload.get("token_usage") == "reported" and valid_token_count(event.payload.get("tokens")):
            totals[name] = totals.get(name, 0) + event.payload["tokens"]
    leader = rt.organization.leader
    members = [] if leader.driver else [{"role_id": leader.id, "status": "available", "reason": "local_policy"}]
    states = {}
    roles = [*([leader] if leader.driver else []), *rt.cabinet_roles, *rt.worker_roles]
    for role in {role.id: role for role in roles}.values():
        if role.driver not in states:
            driver = rt.drivers.get(role.driver)
            budget = getattr(driver, "token_budget", None)
            if driver is None:
                state = ("on_leave", "not_configured")
            elif budget is not None and not valid_token_count(budget):
                state = ("on_leave", "invalid_budget")
            elif valid_token_count(budget) and totals.get(role.driver, 0) >= budget:
                state = ("on_leave", "budget_exhausted")
            elif isinstance(driver, (OpenAICompatibleDriver, AnthropicMessagesDriver)) and not os.environ.get(driver.api_key_env):
                state = ("on_leave", "missing_key")
            elif rt._is_circuit_open(role.driver):
                state = ("on_leave", "circuit_open")
            elif driver_mode(driver) == "local_demo":
                state = ("demo", "local_demo")
            else:
                recent = rt._driver_health.recent_state(role.driver)
                state = ("available", "recent_success") if recent == "succeeded" else ("unverified", "recent_failure") if recent == "failed" else ("unverified", "not_recently_checked")
            states[role.driver] = state
        status, reason = states[role.driver]
        members.append({"role_id": role.id, "status": status, "reason": reason})
    return {"checked_at": datetime.now(timezone.utc).isoformat(), "members": members, "all_available": all(item["status"] == "available" for item in members)}
