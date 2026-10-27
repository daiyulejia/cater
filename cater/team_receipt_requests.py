"""Read a completed command's recorded input, without executing or replaying it."""

from __future__ import annotations

import hashlib
import json

from .team_read_sources import HOST, _unambiguous, invalid_query
from .events import Actor
from .tool_protocol import json_utf8


MAX_SOURCE_BYTES = 256 * 1024
MAX_RESPONSE_BYTES = 8000
NOTE = ("已保存的历史命令输入，不是再次执行、成功或覆盖证明；arguments为排序键的JSON表示，"
        "stdin为原文本。分页可能从行中开始。仅用于本任务核对，公开引用不得包含凭据。")
FIELDS = tuple((f"$.{key}", size) for key, size in {
    "started_seq": 0, "run_id": 128, "role": 128, "name": 128,
    "call_id": 256, "result.status": 128,
}.items())
UNAMBIGUOUS_TREE = ("NOT EXISTS (SELECT 1 FROM json_tree(payload_json) WHERE typeof(key) = 'text' "
    "GROUP BY parent, CAST(key AS BLOB) HAVING count(*) > 1 "
    "OR instr(CAST(key AS BLOB), x'00') > 0)")


def _unavailable():
    return {"status": "error", "code": "receipt_request_unavailable", "complete": True,
            "outcome": "not_started", "message": "该历史请求或指定正文未保存、无法完整解析或超过读取上限；不能据此推断原操作是否执行或成功。"}


def _nonfinite(_):
    raise ValueError("Non-finite recorded value")


def read(sources, task_id, arguments, record_read):
    """Authenticate from bounded metadata before reading the paired start only."""
    response = {"status": "ok", "section": "request", "request": None,
                "record_read": record_read, "note": NOTE}
    seq = arguments["receipt_seq"]
    if seq >= 2 ** 63:
        return response
    event = next(sources._metadata(task_id, seq=seq, kind="team.tool_finished",
        where=UNAMBIGUOUS_TREE, fields=FIELDS), None)
    if (event is None or event.actor != HOST or event.payload.get("name") != "workspace_run"
            or any(key in arguments and event.payload.get(key) != arguments[key]
                   for key in ("run_id", "role", "name"))
            or "before_seq" in arguments and seq >= arguments["before_seq"]):
        return response
    start = sources._chain(task_id, event)
    if start is None:
        # A same-task host start that cannot be parsed is not an empty search.
        # Do not decode its body or expose an unverified request to diagnose it.
        start_seq = event.payload.get("started_seq")
        if type(start_seq) is int and 0 < start_seq < seq:
            bad = sources.rt.log.db.execute("SELECT actor_json FROM events WHERE task_id = ? "
                "AND seq = ? AND type = 'team.tool_started' AND NOT (" + sources._guard() + ") "
                "AND length(CAST(actor_json AS BLOB)) <= 512", (task_id, start_seq)).fetchone()
            if bad is not None:
                try:
                    if Actor(**json.loads(bad["actor_json"], object_pairs_hook=_unambiguous)) == HOST:
                        return _unavailable()
                except (ValueError, TypeError, UnicodeError):
                    pass
        return response
    if ("message_seq" in arguments
            and start.payload.get("message_seq") != arguments["message_seq"]):
        return response
    # Do not materialize the completed command's potentially large output.
    row = sources.rt.log.db.execute("SELECT CASE WHEN length(CAST(payload_json AS BLOB)) <= ? "
        "THEN CAST(payload_json AS BLOB) END AS body FROM events "
        "WHERE task_id = ? AND seq = ? AND type = 'team.tool_started'",
        (MAX_SOURCE_BYTES, task_id, start.seq)).fetchone()
    if row is None or row["body"] is None:
        return _unavailable()
    try:
        stored = json.loads(row["body"].decode("utf-8"), object_pairs_hook=_unambiguous,
                            parse_constant=_nonfinite)
        original = stored.get("arguments")
        if not isinstance(original, dict):
            return _unavailable()
        part = arguments.get("request_part", "arguments")
        # Strict UTF-8 for the entire original: never silently substitute a
        # malformed input or call a sanitized representation the original.
        serialized = json.dumps(original, ensure_ascii=False, sort_keys=True, allow_nan=False)
        serialized.encode("utf-8")
        content = serialized if part == "arguments" else original.get("stdin")
        if not isinstance(content, str):
            return _unavailable()
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return _unavailable()
    offset = arguments.get("offset", 0)
    if offset > len(content):
        return invalid_query("offset超过这份历史正文的码点长度。", "offset")
    identity = {"seq": seq, "source_hash": event.hash, "started_seq": start.seq,
                "request_hash": start.hash,
                **{key: start.payload[key] for key in ("run_id", "message_seq", "role", "name")},
                "part": part, "content_sha256": digest}

    def page(count):
        end = offset + count
        span = {"unit": "unicode_codepoints", "offset": offset, "returned_chars": count,
                "total_chars": len(content), "has_more": end < len(content),
                "next_offset": end if end < len(content) else None,
                "content_complete": offset == 0 and end == len(content)}
        if part == "stdin":
            span.update(first_line=content.count("\n", 0, offset) + 1,
                        starts_mid_line=offset > 0 and content[offset - 1] != "\n")
        return {**response, "request": {**identity, "content": content[offset:end], "read_range": span}}

    maximum = min(arguments.get("length", 4000), len(content) - offset)
    candidate = page(maximum)
    if len(json_utf8(candidate)) <= MAX_RESPONSE_BYTES:
        return candidate
    low, high = 0, maximum
    while low < high:
        middle = (low + high + 1) // 2
        if len(json_utf8(page(middle))) <= MAX_RESPONSE_BYTES:
            low = middle
        else:
            high = middle - 1
    if low == 0:
        return _unavailable()
    return page(low)
