"""Version-bound public handoff notes, distinct from execution proof."""

from __future__ import annotations

import hashlib
import json
import re

from .events import Actor
from .review_contract import browser_observations


HOST = Actor.system("team")


def omission_reason(value):
    """Validate a recorded choice explanation, not the truth of that explanation."""
    if not isinstance(value, str) or len(value) > 1000 or "\0" in value:
        return None
    # Include the BOM stripped by JavaScript, alongside Python's Unicode whitespace.
    reason = re.sub(r"^[\s\ufeff]+|[\s\ufeff]+$", "", value)
    if not reason:
        return None
    try:
        value.encode("utf-8")
    except UnicodeError:
        return None
    return reason


def _completion_request(arguments):
    if not isinstance(arguments, dict) or any(not isinstance(arguments.get(key), str)
            or not arguments[key].strip() or len(arguments[key]) > 32000 for key in ("summary", "verification")):
        return None
    request = dict(arguments)
    for key in ("summary", "verification"):
        if isinstance(request.get(key), str):
            request[key] = request[key].strip()
    if "artifact_seqs" in request:
        seqs = request["artifact_seqs"]
        if (not isinstance(seqs, list) or len(seqs) > 128
                or any(type(seq) is not int or seq <= 0 for seq in seqs)
                or len(set(seqs)) != len(seqs)):
            return None
        request["artifact_seqs"] = sorted(seqs)
    if "omitted_approved_artifacts_reason" in request:
        reason = omission_reason(request["omitted_approved_artifacts_reason"])
        if reason is None:
            return None
        request["omitted_approved_artifacts_reason"] = reason
    try:
        # Canonical JSON distinguishes bool from int, unlike Python container equality.
        return json.dumps(request, sort_keys=True, ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError):
        return None


class HandoffError(ValueError):
    """Controlled public diagnostics; never echo arbitrary source payloads."""

    def __init__(self, message, *, diagnostics=None, source_catalog=None):
        super().__init__(message)
        self.diagnostics = list(diagnostics) if diagnostics is not None else []
        self.source_catalog = dict(source_catalog) if source_catalog is not None else {}


def _text(value, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise HandoffError("交付说明需要非空、有界的文字。")
    return value.strip()


def _strings(value, *, count=12):
    if not isinstance(value, list) or len(value) > count:
        raise HandoffError("交付说明列表过长或类型不正确。")
    return [_text(item, 1000) for item in value]


def handoff_schema():
    from .delivery_commands import schema as command_schema

    strings = {"type": "array", "maxItems": 12, "items": {"type": "string", "maxLength": 1000}}
    return {"type": "object", "additionalProperties": False, "required": ["why", "reproduce", "limits"],
        "description": "可选的对客原因、长细节与证据，在此折叠保存；关键结论留主摘要，不是私有思维。完整说明（含宿主补充引用信息）总计不超过24KB UTF-8。quote从文件正文、回执stdout/stderr/content、浏览器实际观测或当前复核反馈逐字摘录，不引用计划标签作为执行证据。",
        "properties": {"why": {"type": "string", "maxLength": 4000},
            **{key: strings for key in ("changed", "impact", "alternatives", "reproduce", "limits")},
            "reproduce": {**strings, "description": "成员建议的使用和复查步骤：给出适用目录、完整命令及环境，区分已运行与建议，不凭说明声称执行。Cater工具JSON字段不是终端参数，客户端临时目录不是工程；实际命令可选用command_receipts引用宿主记录，不必重新编写。"},
            "command_receipts": command_schema(),
            "evidence": {"type": "array", "maxItems": 16, "items": {"type": "object", "additionalProperties": False,
                "required": ["source_seq", "quote"], "properties": {
                    "source_seq": {"type": "integer", "minimum": 1,
                        "description": "成果用artifact seq，工作区/浏览器检查用工具回执seq；伙伴复核用record_review返回或reviews中的decision_seq，不是review_seq/request_seq、message_seq或record_review工具回执seq。"},
                    "quote": {"type": "string", "maxLength": 2000}}}}}}


def _receipt_text(result):
    parts = []
    for key in ("content", "stdout", "stderr"):
        value = result.get(key)
        if isinstance(value, dict):
            segments = [value.get("excerpt", value.get("preview"))]
            if value.get("preview_mode") == "head_tail":
                segments.append(value.get("tail_excerpt"))
            parts.extend(part for part in segments if isinstance(part, str))
            continue
        if isinstance(value, str):
            parts.append(value)
    error = result.get("error")
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        parts.append(error["message"])
    return parts


def receipt_metadata(result, *, role, name):
    """Recorded execution/capture fields, independent of any quoted conclusion."""
    metadata = {"kind": "tool_receipt", "role": role, "tool": name, "status": result["status"]}
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
    encoding = {key: result[key]["utf8_valid"] for key in ("stdout", "stderr")
                if isinstance(result.get(key), dict) and "utf8_valid" in result[key]
                and (type(result[key]["utf8_valid"]) is bool or result[key]["utf8_valid"] is None)}
    if encoding:
        metadata["output_utf8_valid"] = encoding
    return metadata


class TeamDelivery:
    def __init__(self, team):
        self.team, self.rt = team, team.rt

    def repeat(self, task_id, arguments, delivery_seq):
        events = self.rt.log.read(task_id=task_id)
        completed = next((e for e in events if e.seq == delivery_seq
            and e.actor == HOST and e.type == "team.objective_completed"), None)
        original = next((e for e in reversed(events) if completed is not None
            and e.seq < completed.seq and e.actor == HOST and e.type == "team.tool_started"
            and e.payload.get("name") == "finish_work" and completed.payload.get("run_id")
            and e.payload.get("run_id") == completed.payload["run_id"]), None)
        receipted = original is not None and any(e.actor == HOST and e.type == "team.tool_finished"
            and e.seq > completed.seq and e.payload.get("started_seq") == original.seq
            and e.payload.get("run_id") == completed.payload["run_id"] and e.payload.get("name") == "finish_work"
            and isinstance(e.payload.get("result"), dict) and e.payload["result"].get("status") == "ok" for e in events)
        requested = _completion_request(arguments)
        if receipted and requested is not None and requested == _completion_request(original.payload.get("arguments")):
            return {"status": "ok", "delivery_seq": delivery_seq, "already_completed": True,
                "note": f"这份相同请求已有交付记录 #{delivery_seq}；本次没有新增交付，也没有改写文件或保存新的说明。"}
        return {"status": "error", "code": "delivery_already_completed", "delivery_seq": delivery_seq,
            "message": "当前目标已有冻结的交付记录，本次不同参数未保存，文件清单和说明均未改写，不是补交成功。"
                "请用read_delivery核对原记录；普通追问用reply_to_customer答复。确需补交、修改或重新交付时，"
                "先用set_objective明确这次新工作，再按实际版本finish_work；可显式沿用本群未改动的成果，不必重做源码或无故重新开会。"}

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
            if (not isinstance(name, str) or not (name.startswith("workspace_") or name == "browser_check") or run is None or message is None
                    or not message.seq < run.seq < start.seq or s.get("role") not in members or message.payload.get("recipient") != s.get("role")
                    or run.payload.get("role") != s.get("role") or run.payload.get("message_seq") != message.seq
                    or s.get("revision") != revision or type(s.get("revision")) is not int
                    or not isinstance(s.get("call_id"), str) or not s["call_id"].strip()
                    or any(p.get(k) != s.get(k) for k in ("run_id", "role", "name"))
                    or "call_id" in p and p["call_id"] != s["call_id"]
                    or not isinstance(result, dict) or result.get("status") not in {"ok", "error"}):
                continue
            metadata = receipt_metadata(result, role=s["role"], name=name)
            if name == "browser_check":
                checked = result.get("checked_artifacts")
                if (result.get("scope_current") is not True or result.get("artifacts_unchanged") is not True
                        or result.get("check_kind") != "browser" or result.get("outcome") not in {"passed", "failed"}
                        or not isinstance(checked, list) or not checked
                        or any(not isinstance(a, dict) or a not in artifacts for a in checked)):
                    continue
                from .team_browser import BrowserInputError
                try:
                    self.team.browser._files(task_id, [a["seq"] for a in checked])
                except BrowserInputError:
                    continue
                metadata["status"] = result["outcome"]
                report = result.get("browser_report")
                metadata["truncated"] = bool(metadata.get("truncated") or isinstance(report, dict) and report.get("truncated"))
                sources[event.seq] = (event, browser_observations(result), metadata)
            else:
                sources[event.seq] = (event, _receipt_text(result), metadata)
        for review in self.rt.team_reviews.view(task_id):
            event = by_seq.get(review.get("decision_seq"))
            if (review["status"] not in {"stale", "outdated"} and event and event.actor == HOST
                    and event.type == "team.review_recorded" and event.payload.get("reviewer") == review["reviewer"]
                    and event.payload.get("review_seq") == review["request_seq"]):
                sources[event.seq] = (event, [event.payload.get("feedback", "")], {
                    "kind": "peer_review", "role": review["reviewer"], "status": review["status"]})
        return sources

    @staticmethod
    def _source_catalog(sources):
        ranks = {"artifact": 0, "peer_review": 1, "tool_receipt": 2}
        usable = [source for source in sources.values() if source[2].get("kind") in ranks
                  and any(isinstance(part, str) and part.strip() for part in source[1])]
        usable.sort(key=lambda source: (ranks[source[2]["kind"]], -source[0].seq))
        entries = []
        for event, _parts, metadata in usable[:12]:
            entry = {"source_seq": event.seq, "kind": metadata["kind"]}
            for key, limit in (("filename", 256), ("role", 128), ("tool", 64)):
                value = metadata.get(key)
                if isinstance(value, str) and 0 < len(value) <= limit:
                    entry[key] = value
            status = metadata.get("status")
            if isinstance(status, str) and status in {"ok", "error", "passed", "failed", "approved", "changes_requested"}:
                entry["status"] = status
            for key in ("complete", "truncated"):
                if type(metadata.get(key)) is bool:
                    entry[key] = metadata[key]
            code = metadata.get("exit_code")
            if type(code) is int and -(2 ** 31) <= code < 2 ** 32:
                entry["exit_code"] = code
            if entry["kind"] == "artifact":
                entry.update(read_tool="read_artifact", read_args={"artifact_seq": event.seq})
            elif entry["kind"] == "peer_review":
                entry.update(read_tool="read_checkpoint", read_args={"review_seq": event.payload["review_seq"]})
            else:
                entry.update(read_tool="read_receipts", read_args={"before_seq": event.seq + 1, "limit": 1})
            entries.append(entry)
        return {"available_sources": entries, "available_sources_total": len(usable),
                "available_sources_truncated": len(usable) > len(entries),
                "available_sources_note": "这是本次选定版本与当前目标下可摘录的来源索引，不是检查通过或结论正确的证明。"
                    "请按 read_tool/read_args 读取原文，再选择必要引文；未列全时仍可核对已有的其他合法来源。"}

    def normalize(self, task_id, value, *, artifacts, revision):
        allowed = {"why", "changed", "impact", "alternatives", "reproduce", "limits", "evidence", "command_receipts"}
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
        diagnostics = []
        seen = set()
        for evidence_index, ref in enumerate(evidence):
            if (not isinstance(ref, dict) or set(ref) != {"source_seq", "quote"}
                    or type(ref["source_seq"]) is not int or ref["source_seq"] < 1):
                raise HandoffError("交付引用需要本群有效事件序号与原文摘录。")
            quote = _text(ref["quote"], 2000)
            source = sources.get(ref["source_seq"])
            if source is None or not any(quote in part for part in source[1]):
                diagnostics.append({
                    "evidence_index": evidence_index, "source_seq": ref["source_seq"],
                    "reason": "source_unavailable" if source is None else "quote_mismatch",
                    "guidance": (
                        "请读取本次选中成果、当前目标的工作区或浏览器工具回执，或当前复核 decision_seq 的 feedback；"
                        "不要引用 record_review 工具回执或旧目标回执。" if source is None else
                        "请重新读取该来源的正文、回执 stdout/stderr/content 或复核 feedback，逐字摘录可见原文；不要改写或拼接。"),
                })
                continue
            if (ref["source_seq"], quote) in seen:
                continue
            seen.add((ref["source_seq"], quote))
            normalized.append({"source_seq": source[0].seq, "source_hash": source[0].hash, "quote": quote, **source[2]})
        if diagnostics:
            raise HandoffError("交付引用与当前成果、工具回执或版本复核原文不符，请读取实际来源后修正。",
                               diagnostics=diagnostics, source_catalog=self._source_catalog(sources))
        result["evidence"] = normalized
        if "command_receipts" in value:
            from .delivery_commands import normalize as command_records

            try:
                result["command_receipts"] = command_records(self.team, task_id, value["command_receipts"],
                    revision=revision, artifacts=artifacts)
            except ValueError as error:
                raise HandoffError(str(error) + " 可修正或移除可选command_receipts继续交付；未重跑原命令。") from None
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
        reason = (omission_reason(event.payload.get("omitted_approved_artifacts_reason"))
                  if event.actor == HOST and event.type == "team.objective_completed" else None)
        omission = (["## 未附已复核成果的原因", "", *["    " + line for line in reason.splitlines()], "",
                     "这是店长记录的交付选择说明，不表示已附上这些成果。", ""] if reason else [])
        if (not isinstance(note, dict) or type(note.get("version")) is not int or note["version"] != 1
                or not isinstance(note.get("why"), str) or not note["why"].strip()
                or any(not isinstance(note.get(k), list) or not all(isinstance(v, str) for v in note[k])
                       for k in ("changed", "impact", "alternatives", "reproduce", "limits"))):
            return ("\n".join(["# 交付说明", "", f"对应交付事件 #{event.seq}", "",
                str(event.payload.get("summary", "")), "", "## 当时记录的验证范围", "",
                str(event.payload.get("verification", "")), "", *omission]) if omission else None)
        lines = ["# 交付说明", "", f"对应交付事件 #{event.seq}", "", str(event.payload.get("summary", "")), "",
                 "## 当时记录的验证范围", "", str(event.payload.get("verification", "")), "",
                 "## 为什么这样做", "", note["why"], ""]
        lines += omission
        for key, title in (("changed", "改动"), ("impact", "影响"), ("alternatives", "取舍与其他考虑"),
                           ("reproduce", "成员建议的运行与复查"), ("limits", "未验证范围")):
            if note[key]:
                lines += ["## " + title, ""]
                lines += [f"{i + 1}. {value}" if key == "reproduce" else "- " + value for i, value in enumerate(note[key])]
                lines.append("")
        if isinstance(note.get('command_receipts'), list):
            from .delivery_commands import markdown as command_markdown

            lines += command_markdown(note['command_receipts'], artifacts=event.payload.get('artifacts'))
        lines += ["## 说明边界", "", "这是该次交付保存的模型说明；运行步骤不会因此执行，原因推断不等于独立证明。",
                  "历史检查不证明当前文件未变化；文件版本请核对同目录的 manifest.json。", ""]
        evidence = note.get("evidence", [])
        if isinstance(evidence, list):
            labels = {"artifact": "成果原文", "tool_receipt": "历史工具回执", "peer_review": "伙伴复核意见"}
            statuses = {"ok": "正常返回", "error": "返回错误", "passed": "检查通过", "failed": "检查失败", "approved": "伙伴同意", "changes_requested": "伙伴要求修改"}
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
                encoding = ref.get("output_utf8_valid")
                if isinstance(encoding, dict):
                    for stream, label in (("stdout", "标准输出"), ("stderr", "错误输出")):
                        if stream not in encoding:
                            continue
                        if encoding[stream] is False:
                            lines += [f"{label}包含无效 UTF-8 字节，显示可能有信息损失；不表示本段引文本身损坏，也不判定命令失败。原始编码未确定。", ""]
                        elif encoding[stream] is None:
                            lines += [f"{label}的 UTF-8 有效性尚未确定。", ""]
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
                **{k: event.payload[k] for k in ("summary", "verification", "artifacts", "handoff",
                    "omitted_approved_artifacts_reason") if k in event.payload}},
                "historical": self.team.view(task_id)["revision"] != event.seq,
                "note": "保存的版本说明；步骤未因此执行，引用摘录匹配不证明判断正确或当前文件未变化。"}
