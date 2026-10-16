"""Model-only working views; persistent evidence and public views stay intact."""

from __future__ import annotations

import json

from .tool_protocol import prepare_tool_turn


CONVERSATION_BYTES = 16000
HISTORY_BYTES = 96000


def json_bytes(value):
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def _preview(row, key, limit):
    value = row.get(key)
    if not isinstance(value, str) or len(value.encode("utf-8")) <= limit:
        return row
    return {**row, key: value.encode("utf-8")[:limit].decode("utf-8", errors="ignore"),
            key + "_truncated": True, key + "_bytes": len(value.encode("utf-8"))}


def model_context(context):
    """Keep live requirements exact, and make historical excerpts explicit."""
    result = dict(context)
    work = context["current_work"]
    result["current_work"] = {key: work[key] for key in
        ("enabled", "objective", "revision", "status", "pending_count", "checkpoint")}
    result["current_work"].update(
        runs=[_preview(row, "reason", 512) for index, row in enumerate(work["runs"])
              if row["status"] == "running" or index >= len(work["runs"]) - 8],
        runs_total=len(work["runs"]))
    # These payloads already have authoritative copies elsewhere in this input.
    pinned = {row["seq"] for row in (context["message"], context.get("active_assignment"),
                                     context.get("latest_customer_message")) if row}
    result["open_assignments"] = [
        {**row, "text": None, "context_ref": "message" if row["seq"] == context["message"]["seq"]
         else "active_assignment" if context.get("active_assignment") and row["seq"] == context["active_assignment"]["seq"]
         else "latest_customer_message"} if row["seq"] in pinned else _preview(row, "text", 1536)
        for row in context["open_assignments"]]
    result["outgoing_work"] = [_preview(_preview(row, "text", 1536), "latest_update", 1024)
                               for row in context["outgoing_work"]]
    result["notifications"] = [_preview(row, "text", 1536) for row in context["notifications"]]
    rows = [row for row in context["conversation"] if row["seq"] not in pinned]
    selected, total = [], 2
    for row in reversed(rows):
        preview = _preview(row, "text", 4096)
        cost = json_bytes(preview) + 2
        if total + cost > CONVERSATION_BYTES:
            break
        selected.append(preview)
        total += cost
    result["conversation"] = list(reversed(selected))
    latest = {row["filename"]: row for row in context["artifacts"]}
    result["artifacts"] = [_preview(row, "summary", 512) for row in latest.values()]
    result["context_window"] = {
        "conversation_bytes": json_bytes(result["conversation"]),
        "conversation_budget_bytes": CONVERSATION_BYTES,
        "conversation_excerpt": True,
        "conversation_reader": {"name": "read_messages", "scope": "conversation"},
        "work_reader": "read_checkpoint", "receipt_reader": "read_receipts",
        "artifact_versions_total": len(context["artifacts"]),
        "note": "聊天和工作索引可能是节选，不是完整要求或验收结论。message、active_assignment、最新顾客消息及reviews保留原文；需要细节时读取来源。",
    }
    return result


def history_window(history, receipts, *, tools, budget=HISTORY_BYTES):
    """Evict only complete tool exchanges; keep the newest group byte-for-byte.

    Provider blocks may be signed, so no message inside a retained group is
    rewritten. A single oversized group is a deliberate soft-budget exception.
    """
    original_bytes = json_bytes(history)
    if original_bytes <= budget:
        return history, {"omitted_groups": 0, "retained_json_bytes": original_bytes,
                         "soft_budget_bytes": budget}
    # Validate before eviction, including opaque provider blocks and tool names.
    # Otherwise dropping a bad prefix could turn an invalid exchange into valid.
    prepare_tool_turn(role="collaborator", prompt="", history=history, tools=tools)
    groups, seen, index = [], set(), 0
    while index < len(history):
        start = index
        message = history[index]
        if message.get("role") != "assistant":
            raise ValueError("Tool history must start with an assistant exchange")
        ids = [call["id"] for call in message.get("tool_calls", [])]
        if len(set(ids)) != len(ids) or seen.intersection(ids):
            raise ValueError("Tool call IDs must remain unique across exchanges")
        seen.update(ids)
        pending = set(ids)
        index += 1
        while index < len(history) and history[index].get("role") == "tool":
            call_id = history[index].get("tool_call_id")
            if call_id not in pending:
                raise ValueError("Orphaned or duplicate tool result")
            pending.remove(call_id)
            index += 1
        if pending:
            raise ValueError("Incomplete tool exchange")
        groups.append((start, index, json_bytes(history[start:index])))
    if not groups:
        return history, {"omitted_groups": 0, "retained_json_bytes": json_bytes(history)}
    first, retained = len(groups) - 1, groups[-1][2]
    while first > 0 and retained + groups[first - 1][2] <= budget:
        first -= 1
        retained += groups[first][2]
    offset = groups[first][0]
    omitted = {call["id"] for row in history[:offset] if row["role"] == "assistant"
               for call in row.get("tool_calls", [])}
    refs = [row for row in receipts if row["call_id"] in omitted]
    tail = history[offset:]
    return tail, {"omitted_groups": first, "omitted_messages": offset,
                  "retained_json_bytes": json_bytes(tail), "soft_budget_bytes": budget,
                  "oversized_latest_group": groups[-1][2] > budget,
                  "omitted_receipts": refs[-24:], "omitted_receipts_total": len(refs),
                  "reader": "read_receipts",
                  "note": "较早的完整工具交互未重复发送，原回执仍保存。需要原输出时按本轮run_id读取；不得据此重放操作或推断验收通过。"}
