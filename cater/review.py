"""Self-review projection for Cater workspaces and tasks."""

from __future__ import annotations

from typing import Any

from .assignments import latest_assignments_by_owner
from .events import Actor, ChainReport, Event
from .fold import TaskSnapshot


LENSES = [
    ("product", "Product"),
    ("design", "Design"),
    ("distribution", "Distribution"),
    ("collaboration", "Collaboration"),
    ("cost", "Cost"),
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
    member_ids: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    native_tasks = _native_chat_tasks(events, member_ids)
    lenses = [
        _product_lens(tasks),
        _design_lens(tasks, events, member_ids),
        _distribution_lens(tasks, native_tasks),
        _collaboration_lens(tasks, _native_chat_tasks(events, member_ids, agent_exchange=True)),
        _cost_lens(tasks),
        _testing_lens(tasks, chain),
        _ui_lens(tasks),
        _logic_lens(tasks, native_tasks),
        _discipline_lens(tasks),
        _reliability_lens(tasks, events),
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
    steps.sort(key=lambda step: 0 if step["priority"] == "high" else 1)
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


def _design_lens(
    tasks: list[TaskSnapshot], events: list[Event], member_ids: frozenset[str] = frozenset(),
) -> dict[str, str]:
    if not events:
        return _lens("design", "needs_attention", "There is no replayable process yet.", "Create a task so the process can be inspected.")
    native_tasks = _native_chat_tasks(events, member_ids)
    by_task: dict[str, list[Event]] = {}
    for event in events:
        if event.task_id:
            by_task.setdefault(event.task_id, []).append(event)
    missing_brief = [
        task.id for task in tasks if task.messages and not task.brief
        and (task.id not in native_tasks or any(message.get("phase") != "team" for message in task.messages))
    ]
    findings, actions = [], []
    if missing_brief:
        findings.append("Some agent work lacks a generated brief.")
        actions.append("Run or export the task to preserve a readable process summary.")
    native_issues, readable, historical = [], 0, 0
    for task in tasks:
        timeline = by_task.get(task.id, [])
        if not any(event.type in {"team.started", "team.objective_updated", "team.objective_completed"}
                   for event in timeline):
            continue
        # Completion belongs to the latest trusted objective, even if its goal text is unchanged.
        boundary = max((event.seq for event in timeline if event.actor == Actor.system("team")
                        and event.type in {"team.started", "team.objective_updated"}), default=0)
        completions = [event for event in timeline
                       if event.type == "team.objective_completed" and event.actor == Actor.system("team")]
        historical += sum(event.seq <= boundary for event in completions)
        completion = next((event for event in reversed(completions) if event.seq > boundary), None)
        # Reuse native routing trust rules, also requiring readable text before the delivery.
        process = [event for event in timeline if (completion is None or event.seq < completion.seq)
                   and (event.type != "team.message_received"
                        or isinstance(event.payload.get("text"), str) and event.payload["text"].strip())]
        issues = []
        if task.id not in _native_chat_tasks(process, member_ids):
            issues.append("no valid routed native message supports this process or delivery")
        if completion is None or task.status != "done":
            issues.append("the current native objective has no trusted completed outcome")
        if completion is not None:
            missing = [key for key in ("summary", "verification")
                       if not isinstance(completion.payload.get(key), str) or not completion.payload[key].strip()]
            if missing:
                issues.append("the current native completion lacks non-empty text for " + ", ".join(missing))
        if issues:
            native_issues.append(f"Task {task.id}: " + "; ".join(issues) + ".")
        else:
            readable += 1
    if native_issues:
        findings.extend(native_issues)
        actions.append("Inspect the current native objective's routed messages and trusted completion; preserve a readable summary and actual verification scope when work finishes.")
    if readable:
        findings.append(f"{readable} native task(s) have routed chat timelines and trusted current-objective delivery summary and verification text.")
    if historical:
        findings.append(f"{historical} earlier completion record(s) remain historical; they do not complete the current objective.")
    if native_issues or readable:
        actions.append("Inspect the recorded verification scope; process readability does not prove model correctness or user understanding.")
    if findings:
        return _lens("design", "watch" if missing_brief or native_issues else "ok", " ".join(findings), " ".join(actions))
    return _lens("design", "ok", "The process has timeline and brief data.", "Keep replay views compact and scannable.")


def _native_chat_tasks(
    events: list[Event], member_ids: frozenset[str], *, agent_exchange: bool = False,
) -> frozenset[str]:
    started, routed = set(), set()
    for event in events:
        if not event.task_id:
            continue
        if event.type == "team.started" and event.actor == Actor.system("team"):
            started.add(event.task_id)
        elif (event.task_id in started and event.type == "team.message_received"
              and (event.actor == Actor.human() or event.actor.kind == "agent" and event.actor.id in member_ids)
              and isinstance(event.payload.get("recipient"), str) and event.payload["recipient"] in member_ids):
            if agent_exchange and (
                event.actor.kind != "agent" or event.actor.id == event.payload["recipient"]
                or not isinstance(event.payload.get("text"), str) or not event.payload["text"].strip()
            ):
                continue
            routed.add(event.task_id)
    return frozenset(routed)


def _distribution_lens(tasks: list[TaskSnapshot], native_tasks: frozenset[str] = frozenset()) -> dict[str, str]:
    if not tasks:
        return _lens("distribution", "needs_attention", "No task distribution pattern exists yet.", "Create one small task before evaluating distribution weight.")
    planned = [task for task in tasks if task.work and task.work.get("items")]
    if any(task.work.get("scope_current") is False for task in planned):
        return _lens("distribution", "watch", "Some work-item plans no longer match the current requirements or rules.",
                     "Reconcile in-flight work and update the affected plan before releasing further work.")
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
        if any(message.get("phase") != "planning" for message in task.messages)
        and not task.assignments and not (task.work and task.work.get("items")) and task.id not in native_tasks
    ]
    overloaded = [
        task for task in tasks
        if task.status == "done" and len([
            item for item in latest_assignments_by_owner(task.assignments)
            if item.get("status", "assigned") == "assigned"
        ]) > max(3, len(task.messages) + 2)
    ]
    if overloaded:
        return _lens("distribution", "watch", "Some completed tasks may be carrying more active assignments than useful output.", "Review whether dispatch is adding ceremony, extra agents, or token cost instead of clarity.")
    if unassigned_active:
        return _lens("distribution", "watch", "Some executed tasks have no visible assignment path.", "Use direct assign or dispatch when ownership would clarify the work.")
    if planned:
        return _lens("distribution", "ok", "Work items record explicit ownership, review responsibility and dependencies.",
                     "Use current work-item acceptance records; extra dispatch or more agents are not required for coverage.")
    if direct and full:
        return _lens("distribution", "ok", "Both direct worker assignment and cabinet dispatch are represented.", "Keep lightweight and full distribution paths available; periodically prune redundant layers.")
    if direct:
        return _lens("distribution", "ok", "Only direct worker assignment is represented.", "Keep the lightweight path when ownership is clear; use dispatch only when task dependencies justify it.")
    if full:
        has_dependencies = any(
            item.get("depends_on")
            for task in full
            for item in task.assignments
        )
        if has_dependencies:
            return _lens("distribution", "ok", "Only full cabinet dispatch is represented, with visible dependency structure.", "Keep dispatch proportional to the dependencies in the actual task.")
        return _lens("distribution", "watch", "Only full cabinet dispatch is represented, but no dependency structure is visible.", "Review whether dispatch is adding real sequencing or only ceremony.")
    if any(task.assignments for task in tasks):
        return _lens("distribution", "ok", "Leader or command assignments record visible ownership.", "Keep the task's current ownership clear; do not require cabinet dispatch merely for coverage.")
    if any(task.id in native_tasks for task in tasks):
        return _lens("distribution", "ok", "Native chat messages record explicit recipients without legacy assignment records.",
                     "Inspect the actual message, handoff and result; extra dispatch is not required for ownership coverage.")
    return _lens("distribution", "watch", "Tasks exist but no distribution path is visible.", "Choose direct assignment for simple work or dispatch for complex work.")


def _collaboration_lens(
    tasks: list[TaskSnapshot], native_exchanges: frozenset[str] = frozenset(),
) -> dict[str, str]:
    multi_agent = [
        task for task in tasks
        if not (task.work and task.work.get("items"))
        and len({message.get("actor") for message in task.messages if message.get("phase") != "review"}) > 1
    ]
    if not multi_agent:
        if any(task.work and task.work.get("items") for task in tasks):
            return _lens("collaboration", "ok", "Work-item handoffs are bound to accepted input versions; waiting dependencies do not need chat replies.",
                         "Inspect the linked artifacts and acceptance records, not the number of messages.")
        if any(task.id in native_exchanges for task in tasks):
            return _lens("collaboration", "ok", "Native chat records messages between distinct team members; routing alone does not prove execution, review or delivery.",
                         "Inspect the recipient's actual work and return receipts; public narration is not required for a handoff.")
        return _lens("collaboration", "ok", "No multi-agent exchange is visible; that alone is not a collaboration defect.", "Use additional roles only when the task needs collaboration or independent checking.")
    linked = [
        task for task in multi_agent
        if any(message.get("target") or message.get("reply_to") for message in task.messages)
    ]
    if linked:
        return _lens("collaboration", "ok", "At least one worker exchange uses explicit reply metadata.", "Keep replies purposeful; do not turn ordinary work into unnecessary chatter.")
    dependent = [
        task
        for task in multi_agent
        if any(assignment.get("depends_on") for assignment in task.assignments)
    ]
    if dependent:
        return _lens(
            "collaboration",
            "watch",
            "Dependent multi-agent work has no explicit worker-to-worker reply.",
            "Use target/reply_to when one agent continues, corrects, or hands off another agent's work.",
        )
    return _lens(
        "collaboration",
        "ok",
        "Multiple agents contributed independently; no worker handoff reply was needed.",
        "Keep replies purposeful; parallel work does not need performative chatter.",
    )


def _cost_lens(tasks: list[TaskSnapshot]) -> dict[str, str]:
    tokens = sum(task.tokens for task in tasks)
    # Native tool turns and legacy outputs share usage records, not chat-message counts.
    sources = {source: sum(task.token_usage.get(source, 0) for task in tasks) for source in ("reported", "estimated", "unavailable")}
    records = sum(sources.values())
    unavailable = sources["unavailable"]
    estimated = sources["estimated"]
    if unavailable:
        return _lens(
            "cost",
            "watch",
            f"Token usage is unavailable for {unavailable} of {records} usage record(s); the recorded {tokens} token(s) is incomplete.",
            "Treat cost as unknown until the connected driver reports verified usage telemetry.",
        )
    if estimated:
        return _lens(
            "cost",
            "watch",
            f"Token usage is estimated for {estimated} of {records} usage record(s); {tokens} token(s) is not a billing total.",
            "Use provider-reported usage before making cost decisions from this workspace.",
        )
    failed_attempts = sum(
        1 for task in tasks for failure in task.driver_failures
        if type(failure.get("attempt")) is int and failure["attempt"] > 0
    )
    if failed_attempts:
        return _lens(
            "cost", "watch",
            f"Recorded outputs report {tokens} token(s), but usage for {failed_attempts} failed driver attempt(s) is unknown.",
            "Reconcile provider usage for failed attempts before treating recorded output tokens as a complete cost total.",
        )
    if not records:
        return _lens("cost", "ok", "No model usage records are available yet; absence of telemetry does not establish zero cost.",
                     "Measure provider usage independently of visible chat messages.")
    average = tokens / records
    if tokens >= 4000 or average >= 1500:
        return _lens("cost", "watch", f"Recorded model usage totals {tokens} token(s) across {records} usage record(s).", "Review whether fewer roles, direct assignment, or a smaller prompt would preserve quality with less cost.")
    return _lens("cost", "ok", f"Recorded model usage totals {tokens} token(s) across {records} usage record(s).", "Keep monitoring token cost as driver prompts and memory grow.")


def _testing_lens(tasks: list[TaskSnapshot], chain: ChainReport) -> dict[str, str]:
    if not chain.ok:
        return _lens("testing", "needs_attention", chain.describe(), "Investigate the first broken event before trusting projections.")
    if any(task.driver_failures for task in tasks):
        return _lens("testing", "watch", "Driver failures were contained during execution.", "Add or inspect driver failure tests for this path.")
    pending = [item for task in tasks for item in (task.work or {}).get("items", []) if item["status"] != "accepted"]
    if pending:
        return _lens("testing", "watch", f"{len(pending)} work item(s) have not passed current stage acceptance; event-chain validity is not delivery proof.",
                     "Inspect missing artifacts, reviewer evidence, and required local checks before releasing downstream work.")
    return _lens("testing", "ok", chain.describe(), "Keep using scripts/check.py before commits.")


def _ui_lens(tasks: list[TaskSnapshot]) -> dict[str, str]:
    escalated = [task for task in tasks if task.status == "escalated"]
    if escalated:
        return _lens("ui", "needs_attention", "A task is waiting for rare human input.", "Make the required human command or override explicit.")
    if any(task.commands for task in tasks):
        return _lens("ui", "ok", "Human commands are recorded as normal events.", "Keep command entry lightweight and reversible.")
    return _lens(
        "ui",
        "ok",
        "No human command events are recorded; this does not establish uninterrupted execution.",
        "Keep command entry discoverable without making it the default workflow.",
    )


def _logic_lens(tasks: list[TaskSnapshot], native_tasks: frozenset[str] = frozenset()) -> dict[str, str]:
    governed = [task for task in tasks if task.cabinet]
    skipped = [
        task
        for task in tasks
        if isinstance(task.brief, dict)
        and isinstance(task.brief.get("governance"), dict)
        and task.brief["governance"].get("status") == "not_needed"
    ]
    completed_chat = any(task.id in native_tasks and task.status == "done" for task in tasks)
    if governed and (skipped or completed_chat):
        return _lens("logic", "ok", "Both normal collaboration and governance paths are represented.", "Keep triggers configurable and explainable.")
    if governed:
        return _lens("logic", "watch", "Only governance-heavy paths are represented; trigger proportionality needs review.", "Inspect the recorded risk signals and whether each escalation was necessary; do not manufacture another task for path coverage.")
    if skipped:
        return _lens("logic", "ok", "Normal collaboration completed without requiring governance.", "Escalate when actual disagreement or risk justifies it; validate escalation behavior in tests, not by forcing a customer task.")
    if completed_chat:
        return _lens("logic", "ok", "A task with routed native chat has a recorded completion without requiring legacy governance.",
                     "Inspect the delivery's actual verification scope; completion is not independent proof of correctness.")
    if any(task.id in native_tasks for task in tasks):
        return _lens("logic", "ok", "Native chat has routed messages; no completed outcome is asserted here.",
                     "Observe progress and actual results; do not force governance merely because a task is still in progress.")
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


def _reliability_lens(tasks: list[TaskSnapshot], events: list[Event]) -> dict[str, str]:
    uncertain_work = sum(item.get("status") == "uncertain" or bool(item.get("check_uncertain"))
                         for task in tasks for item in (task.work or {}).get("items", []))
    uncertain_delivery = sum(((task.work or {}).get("delivery") or {}).get("status") == "uncertain" for task in tasks)
    latest_planning = {}
    for event in events:
        if event.type == "planning.finished":
            attempt = event.payload.get("attempt_seq")
            order = attempt if type(attempt) is int else event.seq
            if order >= latest_planning.get(event.task_id, (0, None))[0]:
                latest_planning[event.task_id] = (order, event.payload.get("status"))
    uncertain_planning = sum(status == "uncertain" for _, status in latest_planning.values())
    if uncertain_work or uncertain_delivery or uncertain_planning:
        return _lens("reliability", "needs_attention",
                     f"Unresolved results: {uncertain_work} work/check item(s), {uncertain_delivery} delivery review(s), "
                     f"{uncertain_planning} planning call(s). Missing driver.failed events do not establish recovery.",
                     "Inspect the recorded attempt and reconcile its outcome before replaying; do not infer success from a valid audit chain.")
    # Native chat failures use run receipts, not legacy driver.failed events.
    task_ids = {task.id for task in tasks}
    team_starts, team_finishes = {}, {}
    for event in events:
        if event.task_id not in task_ids or event.actor != Actor.system("team"):
            continue
        key = (event.task_id, event.payload.get("message_seq"))
        if event.type == "team.run_started":
            team_starts[key] = event
        elif event.type == "team.run_finished":
            start = team_starts.get(key)
            if (start and start.seq < event.seq and start.payload.get("role") == event.payload.get("role")
                    and ("run_id" not in event.payload or event.payload["run_id"] == start.payload.get("run_id"))):
                team_finishes[key] = event
    native_failures = sum(event.payload.get("status") in {"failed", "interrupted"}
                          for event in team_finishes.values())
    failures = sum(len(task.driver_failures) for task in tasks)
    if native_failures:
        return _lens("reliability", "watch",
                     f"{native_failures} failed or interrupted native chat run(s), {failures} legacy driver failure(s) recorded. "
                     "These are historical receipts, not proof that current work is blocked or recovered.",
                     "Inspect the affected run, later handoffs and real tool results before replaying; do not automatically repeat unknown effects.")
    if failures:
        return _lens("reliability", "watch", f"{failures} contained driver failure(s) recorded.", "Review circuit breaker thresholds and recovery controls.")
    if any(task.messages for task in tasks):
        return _lens("reliability", "ok", "No driver failures are recorded.", "When adding third-party tools, verify contracts and failure modes before trusting names.")
    return _lens("reliability", "ok", "No driver failures are recorded.", "Keep containment events visible in replay.")


def _adoption_lens(tasks: list[TaskSnapshot], profiles: dict[str, dict[str, Any]]) -> dict[str, str]:
    if not profiles:
        return _lens("adoption", "needs_attention", "No agent profiles exist yet.", "Run the demo or first collaboration round before presenting the project.")
    if len(profiles) < 2:
        return _lens("adoption", "ok", "One active agent profile is visible.", "Choose team size for the actual work, not to manufacture multi-agent coverage.")
    if not tasks:
        return _lens("adoption", "needs_attention", "Profiles exist without task context.", "Export a replay bundle for contributors.")
    return _lens("adoption", "ok", "Multiple agent profiles are visible.", "Keep README examples runnable with zero services.")
