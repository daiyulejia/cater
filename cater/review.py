"""Self-review projection for Cater workspaces and tasks."""

from __future__ import annotations

from typing import Any

from .events import ChainReport, Event
from .fold import TaskSnapshot


LENSES = [
    ("product", "Product"),
    ("design", "Design"),
    ("testing", "Testing"),
    ("ui", "UI"),
    ("logic", "Logic"),
    ("reliability", "Reliability"),
    ("adoption", "Adoption"),
]


def build_review(
    *,
    tasks: list[TaskSnapshot],
    events: list[Event],
    profiles: dict[str, dict[str, Any]],
    chain: ChainReport,
    scope: str = "workspace",
) -> dict[str, Any]:
    lenses = [
        _product_lens(tasks),
        _design_lens(tasks, events),
        _testing_lens(tasks, chain),
        _ui_lens(tasks),
        _logic_lens(tasks),
        _reliability_lens(tasks),
        _adoption_lens(tasks, profiles),
    ]
    next_actions = [lens["action"] for lens in lenses if lens["status"] != "ok"]
    return {
        "scope": scope,
        "health": "attention" if next_actions else "steady",
        "summary": _summary(tasks, events, chain, next_actions),
        "lenses": lenses,
        "next_actions": next_actions,
    }


def _summary(
    tasks: list[TaskSnapshot],
    events: list[Event],
    chain: ChainReport,
    next_actions: list[str],
) -> str:
    if not tasks:
        return "No tasks yet; create a small task and run one collaboration round."
    if not chain.ok:
        return f"Audit chain needs attention at seq={chain.broken_at}: {chain.reason}."
    if next_actions:
        return f"{len(next_actions)} review lens(es) found actionable follow-up."
    return f"{len(tasks)} task(s), {len(events)} event(s), no immediate review blockers."


def _lens(lens_id: str, status: str, finding: str, action: str) -> dict[str, str]:
    label = dict(LENSES)[lens_id]
    return {"id": lens_id, "label": label, "status": status, "finding": finding, "action": action}


def _product_lens(tasks: list[TaskSnapshot]) -> dict[str, str]:
    if not tasks:
        return _lens("product", "needs_attention", "No user-visible task exists.", "Create a small task with acceptance criteria.")
    stale = [task for task in tasks if task.status in {"pending", "running"} and not task.messages]
    if stale:
        return _lens("product", "needs_attention", "Some tasks have not received agent work.", "Run a collaboration round for the pending task.")
    rework = [task for task in tasks if task.status == "rework"]
    if rework:
        return _lens("product", "watch", "A task is in rework after autonomous governance.", "Send a command or run another collaboration round.")
    return _lens("product", "ok", "Tasks have visible agent progress.", "Keep the next user action obvious.")


def _design_lens(tasks: list[TaskSnapshot], events: list[Event]) -> dict[str, str]:
    if not events:
        return _lens("design", "needs_attention", "There is no replayable process yet.", "Create a task so the process can be inspected.")
    missing_brief = [task.id for task in tasks if task.messages and not task.brief]
    if missing_brief:
        return _lens("design", "watch", "Some agent work lacks a generated brief.", "Run or export the task to preserve a readable process summary.")
    return _lens("design", "ok", "The process has timeline and brief data.", "Keep replay views compact and scannable.")


def _testing_lens(tasks: list[TaskSnapshot], chain: ChainReport) -> dict[str, str]:
    if not chain.ok:
        return _lens("testing", "needs_attention", chain.describe(), "Investigate the first broken event before trusting projections.")
    if any(task.driver_failures for task in tasks):
        return _lens("testing", "watch", "Driver failures were contained during execution.", "Add or inspect driver failure tests for this path.")
    return _lens("testing", "ok", chain.describe(), "Keep using scripts/check.py before commits.")


def _ui_lens(tasks: list[TaskSnapshot]) -> dict[str, str]:
    escalated = [task for task in tasks if task.status == "escalated"]
    if escalated:
        return _lens("ui", "needs_attention", "A task is waiting for rare human input.", "Make the required human command or override explicit.")
    if any(task.commands for task in tasks):
        return _lens("ui", "ok", "Human commands are recorded as normal events.", "Keep command entry lightweight and reversible.")
    return _lens("ui", "watch", "No human command has been recorded yet.", "Verify the command surface is obvious in CLI/Web.")


def _logic_lens(tasks: list[TaskSnapshot]) -> dict[str, str]:
    governed = [task for task in tasks if task.cabinet]
    skipped = [
        task
        for task in tasks
        if isinstance(task.brief, dict)
        and isinstance(task.brief.get("governance"), dict)
        and task.brief["governance"].get("status") == "not_needed"
    ]
    if governed and skipped:
        return _lens("logic", "ok", "Both normal collaboration and governance paths are represented.", "Keep triggers configurable and explainable.")
    if governed:
        return _lens("logic", "watch", "Only governance-heavy paths are represented.", "Add or run a low-risk collaboration task that skips cabinet.")
    if skipped:
        return _lens("logic", "watch", "Only non-governed collaboration is represented.", "Force one governance round to verify the escalation path.")
    return _lens("logic", "needs_attention", "No collaboration or governance path has completed.", "Run a task before evaluating the organization logic.")


def _reliability_lens(tasks: list[TaskSnapshot]) -> dict[str, str]:
    failures = sum(len(task.driver_failures) for task in tasks)
    if failures:
        return _lens("reliability", "watch", f"{failures} contained driver failure(s) recorded.", "Review circuit breaker thresholds and recovery controls.")
    return _lens("reliability", "ok", "No driver failures are recorded.", "Keep containment events visible in replay.")


def _adoption_lens(tasks: list[TaskSnapshot], profiles: dict[str, dict[str, Any]]) -> dict[str, str]:
    if not profiles:
        return _lens("adoption", "needs_attention", "No agent profiles exist yet.", "Run the demo or first collaboration round before presenting the project.")
    if len(profiles) < 2:
        return _lens("adoption", "watch", "Only one active agent profile is visible.", "Use at least two roles to demonstrate team behavior.")
    if not tasks:
        return _lens("adoption", "needs_attention", "Profiles exist without task context.", "Export a replay bundle for contributors.")
    return _lens("adoption", "ok", "Multiple agent profiles are visible.", "Keep README examples runnable with zero services.")
