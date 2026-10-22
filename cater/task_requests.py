"""Durable creation identity shared across lost HTTP responses and restarts."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from .events import Actor, HeadChanged
from .work import WorkConflict

if TYPE_CHECKING:
    from .runtime import Runtime


def prepare(runtime: Runtime, request: dict, request_id: str | None, *, default_team: bool = True):
    """Commit creation, local intake and the execution intent together.

    Intake records local routing or requests later model planning. No driver is
    called while the event transaction holds the database write lock.
    default_team selects execution for ordinary Web requests without changing identity.
    """
    if request.get("run") and request.get("battle"):
        raise ValueError("普通协作与强制治理不能同时启动。")
    if request.get("collaboration") not in (None, "team"):
        raise ValueError("collaboration must be team")
    with runtime.log.atomic():
        task_id, created = create(runtime, request, uuid.uuid4().hex if request_id is None else request_id)
        if not created:
            return task_id, False, activation(runtime, task_id)
        if request.get("workspace_id") is not None:
            runtime.workspaces.bind(task_id, request["workspace_id"], expected_revision=None)
        elif "workspace_id" not in request:
            runtime.workspaces.bind_default(task_id)
        team = getattr(runtime, "team", None)
        # Select execution only after identity lookup; defaults are not request identity.
        if (not request.get("battle") and (request.get("collaboration") == "team" or default_team)
                and team is not None and team.available()):
            source = next(runtime.log.iter_events(task_id=task_id, event_types=("task.created",)))
            team.enable(task_id)
            team.receive(task_id, request["goal"], request_id=f"team-creation-{source.seq}")
        elif request.get("requested_route") is not None or not request.get("dispatch"):
            runtime.intake(task_id, requested_route=request.get("requested_route") or "auto",
                           preferred_owner=request.get("assign_to") or None,
                           preferred_objective=request.get("assign_objective"),
                           preferred_acceptance=request.get("assign_acceptance"))
        else:
            runtime.dispatch(task_id)
        action = "run" if request.get("run") else "battle" if request.get("battle") else None
        intent = None
        if action:
            source = next(runtime.log.iter_events(task_id=task_id, event_types=("task.created",)))
            intent = runtime.log.emit(type="task.activation_requested", task_id=task_id,
                actor=Actor.system("task_activation"), payload={"creation_seq": source.seq,
                    "creation_hash": source.hash, "action": action, "scope": runtime.work.scope(task_id),
                    "protocol": 1})
        return task_id, True, intent


def activation(runtime: Runtime, task_id: str):
    events = runtime.log.read(task_id=task_id)
    source = next((e for e in events if e.type == "task.created" and e.actor == Actor.human()), None)
    if source is None:
        return None
    request = source.payload.get("creation_request")
    if not isinstance(request, dict):
        return None
    action = "run" if request.get("run") is True else "battle" if request.get("battle") is True else None
    if action is None:
        return None
    return next((e for e in events if e.type == "task.activation_requested"
                 and e.actor == Actor.system("task_activation") and e.seq > source.seq
                 and e.payload.get("protocol") == 1 and e.payload.get("creation_seq") == source.seq
                 and e.payload.get("creation_hash") == source.hash and e.payload.get("action") == action
                 and isinstance(e.payload.get("scope"), dict) and e.payload["scope"]), None)


def create(runtime: Runtime, request: dict, request_id: str | None) -> tuple[str, bool]:
    from .runtime import _acceptance_items, _required_text, MAX_TASK_GOAL_CHARS

    goal = _required_text(request.get("goal"), "goal", MAX_TASK_GOAL_CHARS)
    acceptance = _acceptance_items(request.get("acceptance"))
    if request_id is None:
        return runtime.create_task(goal, acceptance, project_id=request.get("project_id")), True
    if (not isinstance(request_id, str) or not 8 <= len(request_id) <= 128
            or not all(c.isalnum() or c in "-_" for c in request_id)):
        raise ValueError("需要有效的创建请求标识。")
    normalized = {**request, "goal": goal, "acceptance": acceptance}
    project_id = request.get("project_id")
    task_id = "task_" + uuid.uuid4().hex[:10]
    for _ in range(8):
        head = runtime.log.head_hash()
        for event in runtime.log.iter_events(event_types=("task.created",)):
            if event.actor == Actor.human() and event.payload.get("creation_request_id") == request_id:
                if event.payload.get("creation_request") != normalized:
                    raise WorkConflict("同一创建请求不能更换目标或执行选项，请重新发起。")
                return event.task_id, False
        if project_id is not None:
            runtime.rulebook.require_project(project_id)
        try:
            runtime.log.emit_many([
                {"actor": Actor.human(), "type": "task.created", "task_id": task_id,
                 "payload": {"goal": goal, "acceptance": acceptance,
                             **({"project_id": project_id} if project_id else {}),
                             "creation_request_id": request_id, "creation_request": normalized}},
                {"actor": runtime._leader_actor(), "type": "task.started", "task_id": task_id,
                 "payload": {"organization_fingerprint": runtime.organization_fingerprint}},
            ], expected_head=head)
            return task_id, True
        except HeadChanged:
            continue
    raise WorkConflict("创建请求正在更新，请使用原请求标识重试。")
