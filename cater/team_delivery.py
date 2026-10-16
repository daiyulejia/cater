"""Version-bound public handoff notes, distinct from execution proof."""

from __future__ import annotations

import hashlib
import json

from .events import Actor


HOST = Actor.system("team")


class HandoffError(ValueError):
    """Controlled public diagnostics; never echo arbitrary source payloads."""


def _text(value, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise HandoffError("交付说明需要非空、有界的文字。")
    return value.strip()


def _strings(value, *, count=12):
    if not isinstance(value, list) or len(value) > count:
        raise HandoffError("交付说明列表过长或类型不正确。")
    return [_text(item, 1000) for item in value]


def handoff_schema():
    strings = {"type": "array", "maxItems": 12, "items": {"type": "string", "maxLength": 1000}}
    return {"type": "object", "additionalProperties": False, "required": ["why", "reproduce", "limits"],
        "description": "可选的对客原因、复查与边界说明，不是私有思维。保存的完整说明（含宿主补充引用信息）总计不超过24KB UTF-8。quote从文件正文、回执stdout/stderr/content或当前复核反馈逐字摘录。",
        "properties": {"why": {"type": "string", "maxLength": 4000},
            **{key: strings for key in ("changed", "impact", "alternatives", "reproduce", "limits")},
            "evidence": {"type": "array", "maxItems": 16, "items": {"type": "object", "additionalProperties": False,
                "required": ["source_seq", "quote"], "properties": {
                    "source_seq": {"type": "integer", "minimum": 1},
                    "quote": {"type": "string", "maxLength": 2000}}}}}}


def _receipt_text(result):
    parts = []
    for key in ("content", "stdout", "stderr"):
        value = result.get(key)
        if isinstance(value, dict):
            value = value.get("excerpt", value.get("preview"))
        if isinstance(value, str):
            parts.append(value)
    error = result.get("error")
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        parts.append(error["message"])
    return parts


class TeamDelivery:
    def __init__(self, team):
        self.team, self.rt = team, team.rt

    def _sources(self, task_id, artifacts, revision):
        events = self.rt.log.read(task_id=task_id)
        by_seq = {e.seq: e for e in events}
        sources = {}
        members = {r.id for r in [self.rt.organization.leader, *self.rt.roles]}
        for ref in artifacts:
            event = by_seq.get(ref["seq"])
            if (event and event.type == "deliverable.published" and event.actor.kind == "agent" and event.actor.id in members
                    and isinstance(event.payload.get("content"), str) and event.payload.get("filename") == ref["filename"]
                    and event.payload.get("content_sha256") == ref["content_sha256"]
                    and hashlib.sha256(event.payload["content"].encode("utf-8")).hexdigest() == ref["content_sha256"]):
                sources[event.seq] = (event, [event.payload["content"]], {"kind": "artifact",
                    "filename": ref["filename"], "content_sha256": ref["content_sha256"], "role": event.actor.id})
        messages = {e.seq: e for e in self.team._messages(task_id, events=events)}
        runs = {e.payload["run_id"]: e for e in events if e.type == "team.run_started" and e.actor == HOST
                and isinstance(e.payload.get("run_id"), str)}
        for event in events:
            if event.type != "team.tool_finished" or event.actor != HOST:
                continue
            p = event.payload
            start = by_seq.get(p.get("started_seq")) if type(p.get("started_seq")) is int else None
            if not start or start.type != "team.tool_started" or start.actor != HOST or start.seq >= event.seq:
                continue
            s = start.payload
            name = s.get("name")
            run = runs.get(s.get("run_id")) if isinstance(s.get("run_id"), str) else None
            message = messages.get(s.get("message_seq")) if type(s.get("message_seq")) is int else None
            result = p.get("result")
            if (not isinstance(name, str) or not name.startswith("workspace_") or run is None or message is None
                    or not message.seq < run.seq < start.seq or s.get("role") not in members or message.payload.get("recipient") != s.get("role")
                    or run.payload.get("role") != s.get("role") or run.payload.get("message_seq") != message.seq
                    or s.get("revision") != revision or type(s.get("revision")) is not int
                    or not isinstance(s.get("call_id"), str) or not s["call_id"].strip()
                    or any(p.get(k) != s.get(k) for k in ("run_id", "role", "name"))
                    or "call_id" in p and p["call_id"] != s["call_id"]
                    or not isinstance(result, dict) or result.get("status") not in {"ok", "error"}):
                continue
            metadata = {"kind": "tool_receipt", "role": s["role"], "tool": name, "status": result["status"]}
            if type(result.get("exit_code")) is int:
                metadata["exit_code"] = result["exit_code"]
            if type(result.get("complete")) is bool:
                metadata["complete"] = result["complete"]
            if any(isinstance(result.get(key), dict) and result[key].get("complete") is False for key in ("stdout", "stderr")):
                metadata["complete"] = False
            if type(result.get("truncated")) is bool:
                metadata["truncated"] = result["truncated"]
            if any(isinstance(result.get(key), dict) and result[key].get("truncated") is True for key in ("stdout", "stderr")):
                metadata["truncated"] = True
            sources[event.seq] = (event, _receipt_text(result), metadata)
        for review in self.rt.team_reviews.view(task_id):
            event = by_seq.get(review.get("decision_seq"))
            if (review["status"] not in {"stale", "outdated"} and event and event.actor == HOST
                    and event.type == "team.review_recorded" and event.payload.get("reviewer") == review["reviewer"]
                    and event.payload.get("review_seq") == review["request_seq"]):
                sources[event.seq] = (event, [event.payload.get("feedback", "")], {
                    "kind": "peer_review", "role": review["reviewer"], "status": review["status"]})
        return sources

    def normalize(self, task_id, value, *, artifacts, revision):
        allowed = {"why", "changed", "impact", "alternatives", "reproduce", "limits", "evidence"}
        if (not isinstance(value, dict) or set(value) - allowed
                or not {"why", "reproduce", "limits"} <= set(value)):
            raise HandoffError("交付说明需要原因、复查步骤与未覆盖范围，不能包含私有思考或其他未定义字段。")
        result = {"version": 1, "why": _text(value["why"], 4000),
                  **{key: _strings(value.get(key, [])) for key in ("changed", "impact", "alternatives", "reproduce", "limits")}}
        evidence = value.get("evidence", [])
        if not isinstance(evidence, list) or len(evidence) > 16:
            raise HandoffError("交付引用需要至多16项来源。")
        sources = self._sources(task_id, artifacts, revision) if evidence else {}
        normalized = []
        seen = set()
        for ref in evidence:
            if (not isinstance(ref, dict) or set(ref) != {"source_seq", "quote"}
                    or type(ref["source_seq"]) is not int or ref["source_seq"] < 1):
                raise HandoffError("交付引用需要本群有效事件序号与原文摘录。")
            quote = _text(ref["quote"], 2000)
            source = sources.get(ref["source_seq"])
            if source is None or not any(quote in part for part in source[1]):
                raise HandoffError("交付引用与当前成果、工具回执或版本复核原文不符，请读取实际来源后修正。")
            if (ref["source_seq"], quote) in seen:
                continue
            seen.add((ref["source_seq"], quote))
            normalized.append({"source_seq": source[0].seq, "source_hash": source[0].hash, "quote": quote, **source[2]})
        result["evidence"] = normalized
        if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > 24000:
            raise HandoffError("交付说明超过24KB，请保留关键原因、步骤、边界及必要引用。")
        return result

    def index(self, task_id):
        event = next((e for e in reversed(self.rt.log.read(task_id=task_id))
                      if e.type == "team.objective_completed" and e.actor == HOST), None)
        if event is None:
            return None
        summary = event.payload.get("summary")
        return {"seq": event.seq, "summary": summary[:1000] if isinstance(summary, str) else "",
                "has_handoff": isinstance(event.payload.get("handoff"), dict),
                "historical": self.team.view(task_id)["revision"] != event.seq}

    @staticmethod
    def markdown(event):
        note = event.payload.get("handoff")
        if (not isinstance(note, dict) or type(note.get("version")) is not int or note["version"] != 1
                or not isinstance(note.get("why"), str) or not note["why"].strip()
                or any(not isinstance(note.get(k), list) or not all(isinstance(v, str) for v in note[k])
                       for k in ("changed", "impact", "alternatives", "reproduce", "limits"))):
            return None
        lines = ["# 交付说明", "", f"对应交付事件 #{event.seq}", "", str(event.payload.get("summary", "")), "",
                 "## 当时记录的验证范围", "", str(event.payload.get("verification", "")), "",
                 "## 为什么这样做", "", note["why"], ""]
        for key, title in (("changed", "改动"), ("impact", "影响"), ("alternatives", "取舍与其他考虑"),
                           ("reproduce", "运行与复查"), ("limits", "未验证范围")):
            if note[key]:
                lines += ["## " + title, ""]
                lines += [f"{i + 1}. {value}" if key == "reproduce" else "- " + value for i, value in enumerate(note[key])]
                lines.append("")
        lines += ["## 说明边界", "", "这是该次交付保存的模型说明；运行步骤不会因此执行，原因推断不等于独立证明。",
                  "历史检查不证明当前文件未变化；文件版本请核对同目录的 manifest.json。", ""]
        evidence = note.get("evidence", [])
        if isinstance(evidence, list):
            labels = {"artifact": "成果原文", "tool_receipt": "历史工具回执", "peer_review": "伙伴复核意见"}
            statuses = {"ok": "正常返回", "error": "返回错误", "approved": "伙伴同意", "changes_requested": "伙伴要求修改"}
            for ref in evidence:
                if (not isinstance(ref, dict) or not isinstance(ref.get("kind"), str)
                        or ref["kind"] not in labels or not isinstance(ref.get("quote"), str)):
                    continue
                lines += [f"### {labels[ref['kind']]} · 事件 #{ref.get('source_seq', '')}", ""]
                if isinstance(ref.get("status"), str):
                    lines += ["记录状态：" + statuses.get(ref["status"], ref["status"]), ""]
                if type(ref.get("exit_code")) is int:
                    lines += [f"退出码：{ref['exit_code']}", ""]
                if type(ref.get("complete")) is bool:
                    lines += ["回执采集完整。" if ref["complete"] else "回执采集未完成。", ""]
                lines += ["> " + line for line in ref["quote"].splitlines()]
                lines += ["", "来源事件哈希：" + str(ref.get("source_hash", "")), ""]
                if ref.get("truncated") is True:
                    lines += ["回执正文仅保留部分内容。", ""]
        return "\n".join(lines)

    def read(self, task_id, arguments):
        seq = arguments.get("delivery_seq")
        if "delivery_seq" in arguments and (type(seq) is not int or seq < 1):
            raise ValueError("交付读取需要本群有效交付序号。")
        event = next((e for e in reversed(self.rt.log.read(task_id=task_id))
                      if e.type == "team.objective_completed" and e.actor == HOST and (seq is None or e.seq == seq)), None)
        if event is None:
            raise ValueError("找不到本群这次交付记录。")
        return {"status": "ok", "delivery": {"seq": event.seq,
                **{k: event.payload[k] for k in ("summary", "verification", "artifacts", "handoff") if k in event.payload}},
                "historical": self.team.view(task_id)["revision"] != event.seq,
                "note": "保存的版本说明；步骤未因此执行，引用摘录匹配不证明判断正确或当前文件未变化。"}
