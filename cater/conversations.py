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


def dialogue_background(snapshot: dict[str, Any], *, reader: str = "explain_task") -> dict[str, Any]:
    """A bounded routing index, never a replacement for the frozen sources."""
    sources = snapshot.get("sources", [])
    artifacts = snapshot.get("artifacts", [])
    selected = sources[-12:]
    return {
        **{key: snapshot.get(key) for key in ("version", "source_task_id", "source_seq", "source_hash", "captured_at")},
        "representation": "routing_index",
        "source_goal_excerpt": str(snapshot.get("source_goal", ""))[:1000],
        "source_goal_truncated": len(str(snapshot.get("source_goal", ""))) > 1000,
        "sources": [{**{key: source.get(key) for key in ("id", "seq", "task_id")},
                     "excerpt": str(source.get("text", ""))[:500],
                     "excerpt_truncated": len(str(source.get("text", ""))) > 500}
                    for source in selected],
        "omitted_sources": len(sources) - len(selected),
        "artifacts": [{**{key: artifact.get(key) for key in ("task_id", "seq", "content_sha256", "verification")},
                       "title": str(artifact.get("title", ""))[:300],
                       "filename": str(artifact.get("filename", ""))[:300],
                       "summary_excerpt": str(artifact.get("summary", ""))[:500],
                       "content_included": False} for artifact in artifacts[:3]],
        "omitted_artifacts": snapshot.get("omitted_artifacts", 0) + max(0, len(artifacts) - 3),
        "notice": "这是群聊分流索引，历史原文与成果正文未完整加载。不可据此声称检查过成果；"
                  f"涉及具体来源或成果正文时使用 {reader} 读取保留的快照。"
                  "完整快照没有修改，历史要求不自动成为当前任务要求。",
    }


def read_background(snapshot: dict[str, Any] | None, arguments: dict[str, Any]) -> dict[str, Any]:
    """Read only the calling conversation's frozen references, never live peers."""
    if snapshot is None:
        return {"status": "error", "message": "本群没有继承的背景快照；请读取本群消息或成果。"}
    if (not isinstance(arguments, dict) or set(arguments) - {"section", "source_id", "artifact_seq", "offset", "limit"}
            or arguments.get("section") not in {"index", "goal", "source", "artifact"}):
        raise ValueError("需要有效的背景读取范围。")
    section = arguments["section"]
    selectors = set(arguments) & {"source_id", "artifact_seq"}
    expected = {"source_id"} if section == "source" else {"artifact_seq"} if section == "artifact" else set()
    if selectors != expected:
        raise ValueError("来源原文需要source_id，成果正文需要artifact_seq，索引与目标不接受其他选择器。")
    offset, limit = arguments.get("offset", 0), arguments.get("limit", 20 if section == "index" else 8000)
    maximum = 50 if section == "index" else 16000
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= maximum:
        raise ValueError("背景分页范围无效；索引每页最多50项，正文每页最多16000字符。")
    anchor = {key: snapshot.get(key) for key in ("source_task_id", "source_seq", "source_hash", "captured_at")}
    result = {"status": "ok", "snapshot": anchor, "historical_only": True, "section": section, "offset": offset}
    if section == "index":
        rows = [{"section": "source", **{k: s.get(k) for k in ("id", "seq", "task_id")},
                 "excerpt": str(s.get("text", ""))[:300], "excerpt_truncated": len(str(s.get("text", ""))) > 300}
                for s in snapshot.get("sources", [])]
        rows.extend({"section": "artifact", **{k: a.get(k) for k in
                    ("seq", "task_id", "filename", "content_sha256", "verification")},
                     "summary_excerpt": str(a.get("summary", ""))[:300], "content_included": False}
                    for a in snapshot.get("artifacts", []))
        result.update(entries=rows[offset:offset + limit], total=len(rows), omitted_artifacts=snapshot.get("omitted_artifacts", 0))
        size = len(rows)
    else:
        if section == "goal":
            content = str(snapshot.get("source_goal", ""))
        else:
            key, selector = ("id", "source_id") if section == "source" else ("seq", "artifact_seq")
            value = arguments[selector]
            if (section == "source" and (not isinstance(value, str) or not value)
                    or section == "artifact" and (type(value) is not int or value < 1)):
                raise ValueError("背景引用无效。")
            matches = [row for row in snapshot.get("sources" if section == "source" else "artifacts", [])
                       if row.get(key) == value]
            if len(matches) != 1:
                return {"status": "error", "message": "这条引用不在本群冻结背景中；请用section=index核对，不能查询其他群的实时材料。"}
            row = matches[0]
            content_key = "text" if section == "source" else "content"
            if not isinstance(row.get(content_key), str):
                return {"status": "error", "message": "快照未保存这份正文，不能把摘要当作完整文件；请说明缺失或向顾客索取。"}
            content = row[content_key]
            result["source"] = {k: row[k] for k in
                ("id", "seq", "task_id", "filename", "format", "content_sha256", "verification") if k in row}
        result.update(content=content[offset:offset + limit], total_chars=len(content))
        size = len(content)
    result.update(has_more=offset + limit < size, next_offset=offset + limit if offset + limit < size else None)
    return result


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
    if runtime.team.enabled(source_task_id):
        sources.extend({"id": f"{event.seq}:text:0", "seq": event.seq, "task_id": source_task_id,
                        "text": event.payload["text"]}
                       for event in events if event.type == "team.message_received" and event.actor == Actor.human())
    # Descendants carry original material once, without recursively nesting history.
    sources = list({(s["task_id"], s["id"]): s for s in sources}.values())
    rules = [r for r in runtime.rulebook.view(task_id=source_task_id)["rules"]
             if r["scope"] == "task" and r["status"] == "active"]
    source_goal = runtime.team.view(source_task_id)["objective"] if runtime.team.enabled(source_task_id) else task.goal
    result = {"version": 1, "source_task_id": source_task_id, "source_goal": source_goal,
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
