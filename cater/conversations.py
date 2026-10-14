"""Immutable, bounded background for independently continuing conversations."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from .events import Actor, HeadChanged
from .work import WorkConflict

if TYPE_CHECKING:
    from .runtime import Runtime


MAX_BACKGROUND_BYTES = 262144
MAX_BACKGROUND_SOURCES = 256
MAX_BACKGROUND_ARTIFACTS = 3


def background(runtime: Runtime, task_id: str) -> dict[str, Any] | None:
    created = next(runtime.log.iter_events(task_id=task_id, event_types=("task.created",)), None)
    return created.payload.get("background") if created else None


def _size(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def capture(runtime: Runtime, source_task_id: str) -> dict[str, Any]:
    from .planning import requirement_sources

    task = runtime.task(source_task_id)
    if task is None:
        raise ValueError("来源对话不存在。")
    events = runtime.log.read(task_id=source_task_id)
    prior = background(runtime, source_task_id)
    sources = list((prior or {}).get("sources", []))
    sources.extend({**s, "task_id": source_task_id} for s in requirement_sources(events))
    # Descendants carry original material once, without recursively nesting history.
    sources = list({(s["task_id"], s["id"]): s for s in sources}.values())
    rules = [r for r in runtime.rulebook.view(task_id=source_task_id)["rules"]
             if r["scope"] == "task" and r["status"] == "active"]
    result = {"version": 1, "source_task_id": source_task_id, "source_goal": task.goal,
              "source_seq": events[-1].seq, "source_hash": events[-1].hash,
              "captured_at": datetime.now(timezone.utc).isoformat(), "sources": sources, "artifacts": [],
              "task_rules": rules, "omitted_artifacts": (prior or {}).get("omitted_artifacts", 0)}
    if len(sources) > MAX_BACKGROUND_SOURCES or _size(result) > MAX_BACKGROUND_BYTES:
        raise ValueError("来源背景过大，暂时无法完整继承；请先整理为较短的独立对话。")
    work = runtime.work.view(source_task_id)
    accepted = {seq for item in work["items"] if item["status"] == "accepted"
                for seq in item["submission"]["artifact_seqs"]}
    artifacts = list((prior or {}).get("artifacts", []))
    for event in events:
        if event.type != "deliverable.published":
            continue
        p = event.payload
        if p.get("work_item_id") and event.seq not in accepted:
            result["omitted_artifacts"] += 1
            continue
        artifact = {k: p[k] for k in ("title", "summary", "content", "format", "filename", "content_sha256") if k in p}
        artifact.update(task_id=source_task_id, seq=event.seq, actor=event.actor.id,
                        verification="accepted_at_capture" if event.seq in accepted else "unverified")
        if "content" in artifact:
            artifact["content_sha256"] = hashlib.sha256(artifact["content"].encode("utf-8")).hexdigest()
        artifacts.append(artifact)
    artifacts = list({(a["task_id"], a["seq"]): a for a in artifacts}.values())
    for artifact in reversed(artifacts):
        if len(result["artifacts"]) >= MAX_BACKGROUND_ARTIFACTS or _size(result) + _size(artifact) + 16 > MAX_BACKGROUND_BYTES:
            result["omitted_artifacts"] += 1
        else:
            result["artifacts"].insert(0, artifact)
    return result


def derive(runtime: Runtime, source_task_id: str, goal: str, acceptance: list[str], request_id: str) -> dict[str, Any]:
    import uuid

    if not isinstance(source_task_id, str) or not source_task_id:
        raise ValueError("需要选择来源对话。")
    if not isinstance(request_id, str) or not 8 <= len(request_id) <= 128 or not all(c.isalnum() or c in "-_" for c in request_id):
        raise ValueError("需要有效的创建请求标识。")
    request = {"source_task_id": source_task_id, "goal": goal, "acceptance": acceptance}
    task_id = "task_" + uuid.uuid4().hex[:10]
    for _ in range(8):
        head = runtime.log.head_hash()
        for event in runtime.log.iter_events(event_types=("task.created",)):
            if event.actor == Actor.human() and event.payload.get("creation_request_id") == request_id:
                if event.payload.get("creation_request") != request:
                    raise WorkConflict("同一创建请求不能更换目标或来源，请重新发起。")
                return {"task_id": event.task_id, "created": False}
        snapshot = capture(runtime, source_task_id)
        project_id = runtime.rulebook.task_project(source_task_id)
        inherited_rules = []
        for rule in snapshot["task_rules"]:
            inherited_rules.append({**{k: v for k, v in rule.items() if k not in {"seq", "updated_at"}},
                                    "id": "rule_" + uuid.uuid4().hex, "revision": 1, "target_id": task_id,
                                    "source": {"kind": "inherited", "actor": "human", "task_id": source_task_id,
                                               "rule_id": rule["id"], "event_seq": rule["seq"]}})
        try:
            runtime.log.emit(actor=Actor.human(), type="task.created", task_id=task_id,
                             payload={"goal": goal, "acceptance": acceptance, "project_id": project_id,
                                      "background": snapshot, "inherited_rules": inherited_rules,
                                      "creation_request_id": request_id, "creation_request": request}, expected_head=head)
        except HeadChanged:
            continue
        runtime.log.emit(actor=runtime._leader_actor(), type="task.started", task_id=task_id,
                         payload={"organization_fingerprint": runtime.organization_fingerprint})
        return {"task_id": task_id, "created": True}
    raise WorkConflict("来源对话正在更新，请稍后重试。")
