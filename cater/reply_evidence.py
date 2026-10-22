"""Optional, task-local quotes for customer replies, not claim certification."""

from __future__ import annotations

import json
import sqlite3
from contextlib import nullcontext

from .review_contract import browser_observations
from .team_delivery import _receipt_text, receipt_metadata
from .tool_protocol import json_utf8


SUPPORTED = frozenset({"workspace_read", "workspace_write", "workspace_run", "workspace_list",
                       "workspace_search", "browser_check", "read_artifact"})
NOTE = ("这是答复引用的历史原文与工具记录，不认证观点、测试身份、覆盖率或成果正确性。"
        "摘录中的通过不代表整个命令成功；以记录的状态、退出码和完整性分别核对。")
MAX_BYTES = 24000


class ReplyEvidenceError(ValueError):
    def __init__(self, reason, index=None, seq=None):
        super().__init__("答复引用未核对成功；请用read_receipts的receipt_seq读取原回执，逐字摘录单个已保存片段后修正。")
        self.reason, self.index = reason, index
        self.seq = seq if type(seq) is int and 0 < seq < 2 ** 63 else None

    def result(self):
        return {"status": "error", "code": "invalid_reply_evidence", "complete": True,
                "outcome": "not_started", "message": str(self),
                "diagnostics": [{"reason": self.reason,
                    **({"evidence_index": self.index} if self.index is not None else {}),
                    **({"source_seq": self.seq} if self.seq is not None else {})}]}


def evidence_schema():
    return {"type": "array", "maxItems": 8,
        "description": "可选的原回执逐字引文，供顾客展开核对，不认证答复观点；不强制每次引用。source_seq是本群工具完成回执seq，不是started_seq或委托号；MCP结果的cater_receipt提供确切编号。先核对原文，状态/退出码由宿主自动附上，不拼成正文quote。错误/非零退出也能引用，不能将片段OK当整条命令成功。",
        "items": {"type": "object", "additionalProperties": False, "required": ["source_seq", "quote"],
            "properties": {"source_seq": {"type": "integer", "minimum": 1},
                "quote": {"type": "string", "minLength": 1, "maxLength": 2000}}}}


def same(left, right):
    """JSON type-sensitive comparison (True must not stand in for event 1)."""
    try:
        return json.dumps(left, sort_keys=True) == json.dumps(right, sort_keys=True)
    except (ValueError, TypeError, UnicodeError):
        return False


def _parts(name, result):
    if name == "browser_check":
        return browser_observations(result)
    parts = _receipt_text(result)
    if result.get("status") == "error" and isinstance(result.get("message"), str):
        parts.append(result["message"])
    if name.startswith("workspace_") and name != "workspace_run":
        parts.extend(result[key] for key in ("path", "sha256") if isinstance(result.get(key), str))
    if name in {"workspace_list", "workspace_search"}:
        entries = result.get("entries" if name == "workspace_list" else "matches")
        if isinstance(entries, list):
            for entry in entries[:256]:
                if isinstance(entry, dict):
                    parts.extend(entry[key] for key in ("path", "output", "sha256")
                                 if isinstance(entry.get(key), str))
    return parts


def normalize(team, task_id, refs, *, before_seq, events=None):
    if type(refs) is not list or len(refs) > 8:
        raise ReplyEvidenceError("invalid_references")
    sources, seen = [], set()
    for index, ref in enumerate(refs):
        if (type(ref) is not dict or set(ref) != {"source_seq", "quote"}
                or type(ref["source_seq"]) is not int or not 0 < ref["source_seq"] < before_seq
                or ref["source_seq"] >= 2 ** 63 or type(ref["quote"]) is not str
                or not ref["quote"].strip() or len(ref["quote"]) > 2000):
            raise ReplyEvidenceError("invalid_reference", index)
    # Recovery uses its supplied immutable event snapshot, never a newer query.
    authenticated = {}
    if events is not None and refs:
        from .team_read_sources import bound_receipts

        wanted = {ref["source_seq"] for ref in refs}
        authenticated = {event.seq: (event, start) for event, start in bound_receipts(team, task_id, events)
                         if event.seq in wanted}
    with team._context_read() if events is None else nullcontext():
        for index, ref in enumerate(refs):
            seq, quote = ref["source_seq"], ref["quote"]
            try:
                quote.encode("utf-8")
                if events is None:
                    rows = team.read_sources.exact(task_id, {"receipt_seq": seq})
                    event = team.rt.log.db.execute("SELECT hash FROM events WHERE seq = ? AND task_id = ?",
                                                   (seq, task_id)).fetchone()
                    source_hash = event["hash"] if event else None
                else:
                    source = authenticated.get(seq)
                    rows = [{**source[0].payload, "seq": seq}] if source else []
                    source_hash = source[0].hash if source else None
            except (ValueError, TypeError, UnicodeError, sqlite3.Error):
                raise ReplyEvidenceError("source_unavailable", index, seq) from None
            if len(rows) != 1 or source_hash is None or rows[0].get("name") not in SUPPORTED:
                raise ReplyEvidenceError("source_unavailable", index, seq)
            row, result = rows[0], rows[0]["result"]
            parts = _parts(row["name"], result)
            if not any(quote in part for part in parts):
                raise ReplyEvidenceError("quote_mismatch", index, seq)
            if (seq, quote) in seen:
                continue
            seen.add((seq, quote))
            metadata = receipt_metadata(result, role=row["role"], name=row["name"])
            if (isinstance(result.get("read_range"), dict) and result["read_range"].get("content_complete") is False
                    or row["name"] == "workspace_search" and isinstance(result.get("matches"), list)
                    and any(isinstance(match, dict) and match.get("truncated") is True for match in result["matches"][:256])):
                metadata["truncated"] = True
            outcome = result.get("outcome")
            if isinstance(outcome, str) and outcome in {"passed", "failed", "timeout", "launch_error", "effects_unknown", "not_started"}:
                metadata["outcome"] = outcome
            sources.append({"source_seq": seq, "source_hash": source_hash, "quote": quote, **metadata})
    if not sources:
        return None
    value = {"version": 1, "sources": sources, "note": NOTE}
    if len(json_utf8(value)) > MAX_BYTES:
        raise ReplyEvidenceError("evidence_too_large")
    return value
