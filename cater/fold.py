"""Pure fold from events to current task snapshots."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from .events import Actor, Event
from .directions import changes_direction
from .usage import usage_source, valid_token_count
from .work import digest


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


def _bound_artifacts(event: Event, task: TaskSnapshot, submissions: dict[int, Event]) -> list[dict[str, Any]]:
    """Validate a host-created work submission against its already-published bodies."""
    payload = event.payload
    seqs, hashes = payload.get("artifact_seqs"), payload.get("hashes")
    if (not isinstance(seqs, list) or not seqs or any(type(seq) is not int or seq <= 0 or seq >= event.seq for seq in seqs)
            or len(set(seqs)) != len(seqs) or not isinstance(hashes, dict) or set(hashes) != {str(seq) for seq in seqs}
            or not isinstance(payload.get("work_item_id"), str) or not payload["work_item_id"]
            or not isinstance(payload.get("attempt_id"), str) or not payload["attempt_id"]):
        return []
    try:
        if payload.get("digest") != digest({key: value for key, value in payload.items() if key != "digest"}):
            return []
    except (TypeError, ValueError, UnicodeError):
        return []
    reused = payload.get("reused_from")
    if reused is not None:
        prior = submissions.get(reused) if type(reused) is int else None
        if (event.actor != Actor.system("work_scheduler") or not prior or prior.task_id != event.task_id
                or prior.payload.get("work_item_id") != payload["work_item_id"]
                or prior.payload.get("artifact_seqs") != seqs or prior.payload.get("hashes") != hashes):
            return []
    elif event.actor.kind != "agent":
        return []
    by_seq = {item["seq"]: item for item in task.deliverables}
    result = []
    for seq in seqs:
        item = by_seq.get(seq)
        if (not item or item.get("work_item_id") != payload["work_item_id"]
                or (reused is None and (item.get("attempt_id") != payload["attempt_id"] or item["actor"] != event.actor.id))
                or not isinstance(item.get("content"), str) or item.get("content_sha256") != hashes[str(seq)]):
            return []
        try:
            if hashlib.sha256(item["content"].encode("utf-8")).hexdigest() != hashes[str(seq)]:
                return []
        except UnicodeError:
            return []
        result.append(item)
    return result


def _absolute_path(value: object) -> PureWindowsPath | PurePosixPath | None:
    if not isinstance(value, str) or not value or any(ord(char) < 32 for char in value):
        return None
    path = PureWindowsPath(value)
    if not path.is_absolute():
        path = PurePosixPath(value)
    return path if path.is_absolute() and ".." not in path.parts else None


def _apply_materialization(event: Event, task: TaskSnapshot, submissions: dict[int, Event],
                           materialization_versions: dict[tuple[str, int], int]) -> None:
    if event.actor != Actor.system("artifact_store"):
        return
    payload = event.payload
    seq = payload.get("submission_seq")
    submission = submissions.get(seq) if type(seq) is int else None
    if (not submission or submission.task_id != event.task_id or submission.seq >= event.seq
            or submission.payload["digest"] != payload.get("submission_digest")):
        return
    artifacts = _bound_artifacts(submission, task, submissions)
    if not artifacts:
        return
    if event.type == "artifacts.materialization_failed":
        reason = payload.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            return
        for artifact in artifacts:
            key = (task.id, artifact["seq"])
            if submission.seq < materialization_versions.get(key, 0):
                continue
            materialization_versions[key] = submission.seq
            artifact.pop("local_path", None)
            artifact["local_save_error"] = reason
        return
    directory, files = _absolute_path(payload.get("directory")), payload.get("files")
    if directory is None or not isinstance(files, list) or len(files) != len(artifacts):
        return
    paths = {}
    for item in files:
        if not isinstance(item, dict) or type(item.get("seq")) is not int:
            return
        seq = item["seq"]
        path = _absolute_path(item.get("path"))
        if (seq in paths or seq not in submission.payload["artifact_seqs"] or path is None
                or type(path) is not type(directory) or path == directory or not path.is_relative_to(directory)
                or item.get("content_sha256") != submission.payload["hashes"][str(seq)]):
            return
        paths[seq] = item["path"]
    if len({_absolute_path(path) for path in paths.values()}) != len(paths):
        return
    for artifact in artifacts:
        key = (task.id, artifact["seq"])
        if submission.seq < materialization_versions.get(key, 0):
            continue
        materialization_versions[key] = submission.seq
        artifact.pop("local_save_error", None)
        artifact["local_path"] = paths[artifact["seq"]]


def fold(events: list[Event]) -> dict[str, TaskSnapshot]:
    tasks: dict[str, TaskSnapshot] = {}
    submissions: dict[int, Event] = {}
    # Reuse shares artifact seqs; only valid receipts advance this private version guard.
    materialization_versions: dict[tuple[str, int], int] = {}
    workspace_usage: dict[int, Event] = {}
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
            clean = {key: value for key, value in payload.items() if key not in {"local_path", "local_save_error"}}
            task.deliverables.append({**clean, "seq": event.seq, "actor": event.actor.id})
        elif event.type in {"artifacts.materialized", "artifacts.materialization_failed"}:
            _apply_materialization(event, task, submissions, materialization_versions)
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
            if event.type == "work.submitted" and _bound_artifacts(event, task, submissions):
                submissions[event.seq] = event
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
            calls = payload.get("usage_recorded_in")
            accounted = (isinstance(calls, list) and bool(calls) and all(type(seq) is int and seq in workspace_usage
                and workspace_usage[seq].task_id == event.task_id
                and workspace_usage[seq].payload.get("attempt_id") == payload.get("attempt_id")
                and workspace_usage[seq].payload.get("role") == event.actor.id
                and workspace_usage[seq].payload.get("driver") == payload.get("driver") for seq in calls))
            if not accounted:
                task.tokens += tokens if valid_token_count(tokens) else 0
                source = usage_source(tokens, payload.get("token_usage"))
                task.token_usage[source] = task.token_usage.get(source, 0) + 1
        elif event.type == "driver.failed":
            task.driver_failures.append({"seq": event.seq, **payload})
        elif ((event.type == "question.finished" and event.actor.id == "explainer")
              or (event.type == "conversation.finished" and event.actor.id == "conversation")
              or (event.type == "collaboration.finished" and event.actor.id == "collaboration")
              or (event.type == "verification.planned" and event.actor.id == "local_verifier")
              or (event.type == "workspace.model_called" and event.actor.id == "workspace_gateway")) and event.actor.kind == "system" and "driver" in payload:
            if event.type == "workspace.model_called":
                workspace_usage[event.seq] = event
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
