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
    ("discipline", "Discipline"),
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
        _distribution_lens(tasks),
        _collaboration_lens(tasks),
        _cost_lens(tasks),
        _testing_lens(tasks, chain),
        _ui_lens(tasks),
        _logic_lens(tasks),
        _discipline_lens(tasks),
        _reliability_lens(tasks),
        _adoption_lens(tasks, profiles),
    ]
    next_steps = _next_steps(tasks, lenses)
    next_actions = [step["action"] for step in next_steps]
    return {
        "scope": scope,
        "health": "attention" if next_actions else "steady",
        "summary": _summary(tasks, events, chain, next_actions),
        "lenses": lenses,
        "next_steps": next_steps,
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


def _next_actions(tasks: list[TaskSnapshot], lenses: list[dict[str, str]]) -> list[str]:
    return [step["action"] for step in _next_steps(tasks, lenses)]


def _next_steps(tasks: list[TaskSnapshot], lenses: list[dict[str, str]]) -> list[dict[str, str]]:
    if not tasks:
        return [
            {
                "source": "product",
                "label": "Product",
                "status": "needs_attention",
                "priority": "high",
                "action": "Run cater demo to create the first replayable task.",
            }
        ]
    steps = []
    seen = set()
    for lens in lenses:
        if lens["status"] == "ok":
            continue
        action = lens["action"]
        if action not in seen:
            steps.append(
                {
                    "source": lens["id"],
                    "label": lens["label"],
                    "status": lens["status"],
                    "priority": "high" if lens["status"] == "needs_attention" else "medium",
                    "action": action,
                }
            )
            seen.add(action)
    return steps


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


def _distribution_lens(tasks: list[TaskSnapshot]) -> dict[str, str]:
    if not tasks:
        return _lens("design", "needs_attention", "No task distribution pattern exists yet.", "Create one small task before evaluating distribution weight.")
    direct = [
        task for task in tasks
        if any(item.get("mode") == "direct" for item in task.assignments)
    ]
    full = [
        task for task in tasks
        if task.plan and task.plan.get("source") == "cabinet"
    ]
    unassigned_active = [
        task for task in tasks
        if task.messages and not task.assignments
    ]
    overloaded = [
        task for task in tasks
        if len(task.assignments) > max(
            3,
            len({str(item.get("owner")) for item in task.assignments if item.get("owner")}) + 1,
            len(task.messages) + 2,
        )
    ]
    if overloaded:
        return _lens("design", "watch", "Some tasks may be carrying more assignment structure than useful output.", "Review whether dispatch is adding ceremony, extra agents, or token cost instead of clarity.")
    if unassigned_active:
        return _lens("design", "watch", "Some executed tasks have no visible assignment path.", "Use direct assign or dispatch when ownership would clarify the work.")
    if direct and full:
        return _lens("design", "ok", "Both direct worker assignment and cabinet dispatch are represented.", "Keep lightweight and full distribution paths available; periodically prune redundant layers.")
    if direct:
        return _lens("design", "watch", "Only direct worker assignment is represented.", "Run one cabinet dispatch on a complex task to verify upper-layer oversight.")
    if full:
        has_dependencies = any(
            item.get("depends_on")
            for task in full
            for item in task.assignments
        )
        if has_dependencies:
            return _lens("design", "watch", "Only full cabinet dispatch is represented, with visible dependency structure.", "Use direct assignment for a small task to avoid over-governance and unnecessary token spend.")
        return _lens("design", "watch", "Only full cabinet dispatch is represented, but no dependency structure is visible.", "Review whether dispatch is adding real sequencing or only ceremony.")
    return _lens("design", "watch", "Tasks exist but no distribution path is visible.", "Choose direct assignment for simple work or dispatch for complex work.")


def _collaboration_lens(tasks: list[TaskSnapshot]) -> dict[str, str]:
    multi_agent = [
        task for task in tasks
        if len({message.get("actor") for message in task.messages}) > 1
    ]
    if not multi_agent:
        return _lens("logic", "watch", "No multi-agent exchange is visible yet.", "Run at least two roles before judging third-layer collaboration quality.")
    linked = [
        task for task in multi_agent
        if any(message.get("target") or message.get("reply_to") for message in task.messages)
    ]
    if linked:
        return _lens("logic", "ok", "At least one worker exchange uses explicit reply metadata.", "Keep replies purposeful; do not turn ordinary work into unnecessary chatter.")
    return _lens("logic", "watch", "Multi-agent output exists but no explicit worker-to-worker reply is visible.", "Use target/reply_to when one agent continues, corrects, or hands off another agent's work.")


def _cost_lens(tasks: list[TaskSnapshot]) -> dict[str, str]:
    messages = sum(len(task.messages) for task in tasks)
    tokens = sum(task.tokens for task in tasks)
    if not messages:
        return _lens("design", "ok", "No token-bearing agent output is recorded yet.", "Measure token cost once real driver output exists.")
    sources = {source: sum(task.token_usage.get(source, 0) for task in tasks) for source in ("reported", "estimated", "unavailable")}
    unavailable = sources["unavailable"]
    estimated = sources["estimated"]
    if unavailable:
        return _lens(
            "design",
            "watch",
            f"Token usage is unavailable for {unavailable} of {messages} message(s); the recorded {tokens} token(s) is incomplete.",
            "Treat cost as unknown until the connected driver reports verified usage telemetry.",
        )
    if estimated:
        return _lens(
            "design",
            "watch",
            f"Token usage is estimated for {estimated} of {messages} message(s); {tokens} token(s) is not a billing total.",
            "Use provider-reported usage before making cost decisions from this workspace.",
        )
    average = tokens / messages
    if tokens >= 4000 or average >= 1500:
        return _lens("design", "watch", f"Agent output used {tokens} token(s) across {messages} message(s).", "Review whether fewer roles, direct assignment, or a smaller prompt would preserve quality with less cost.")
    return _lens("design", "ok", f"Agent output cost is currently {tokens} token(s) across {messages} message(s).", "Keep monitoring token cost as driver prompts and memory grow.")


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


def _discipline_lens(tasks: list[TaskSnapshot]) -> dict[str, str]:
    flags = [flag for task in tasks for flag in task.discipline_flags]
    if flags:
        return _lens(
            "discipline",
            "watch",
            f"{len(flags)} agent contribution(s) violated the visible collaboration protocol.",
            "Inspect discipline flags and tighten the affected role prompt or driver adapter.",
        )
    return _lens(
        "discipline",
        "ok",
        "No visible collaboration protocol violations are recorded.",
        "Keep contribution, evidence, and reply-link rules explicit for new drivers.",
    )


def _reliability_lens(tasks: list[TaskSnapshot]) -> dict[str, str]:
    failures = sum(len(task.driver_failures) for task in tasks)
    if failures:
        return _lens("reliability", "watch", f"{failures} contained driver failure(s) recorded.", "Review circuit breaker thresholds and recovery controls.")
    if any(task.messages for task in tasks):
        return _lens("reliability", "ok", "No driver failures are recorded.", "When adding third-party tools, verify contracts and failure modes before trusting names.")
    return _lens("reliability", "ok", "No driver failures are recorded.", "Keep containment events visible in replay.")


def _adoption_lens(tasks: list[TaskSnapshot], profiles: dict[str, dict[str, Any]]) -> dict[str, str]:
    if not profiles:
        return _lens("adoption", "needs_attention", "No agent profiles exist yet.", "Run the demo or first collaboration round before presenting the project.")
    if len(profiles) < 2:
        return _lens("adoption", "watch", "Only one active agent profile is visible.", "Use at least two roles to demonstrate team behavior.")
    if not tasks:
        return _lens("adoption", "needs_attention", "Profiles exist without task context.", "Export a replay bundle for contributors.")
    return _lens("adoption", "ok", "Multiple agent profiles are visible.", "Keep README examples runnable with zero services.")
