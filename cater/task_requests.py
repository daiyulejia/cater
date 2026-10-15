"""Durable creation identity shared across lost HTTP responses and restarts."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from .events import Actor, HeadChanged
from .work import WorkConflict

if TYPE_CHECKING:
    from .runtime import Runtime


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
