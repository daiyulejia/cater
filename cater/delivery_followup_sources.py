"""Bound historical sources for a delivery's followup, without new execution.

Pure projection: delivery is the caller's current HOST completion Event, and
bound_sources contains (finished, started) Event pairs from bound_receipts.
The caller owns current-delivery/followup eligibility and receipt authentication.
No I/O, event changes, execution claims or inference from test names occur here.
"""

from __future__ import annotations

from collections.abc import Iterable

from .events import Actor, Event
from .receipt_observations import _captured_text
from .tool_protocol import json_utf8


MAX_BYTES = 4096
MAX_SOURCE_BYTES = 2048
_HOST = Actor.system("team")
_SCOPE = ("run_id", "message_seq", "root", "revision")
_NOTE = ("Selected historical output and file reads, not runtime file versions or "
         "proof of assertion reachability, coverage or causality. Read omitted bodies by receipt_seq.")


def delivery_followup_sources(
    delivery: Event,
    bound_sources: Iterable[tuple[Event, Event]],
    *,
    objective_revision: int,
    max_bytes: int = MAX_BYTES,
) -> dict | None:
    """Return at most one referenced failed command and two earlier file reads.

    All sources must belong to this delivery's task and objective. File reads
    must share the command's run/message/root/revision and finish before its
    start. Select newest eligible sources, never infer a baseline or cause.
    Each source has read_args and body_included; complete bodies use parts with
    field/literal keys, otherwise omission_reason explains the absent body.
    The whole result, including pointers and note, fits max_bytes (1..4096),
    measured with json_utf8. Return None when no command or anchor pointer fits.
    """
    if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_BYTES:
        raise ValueError("max_bytes must be an integer from 1 to 4096")
    if delivery is None or delivery.actor != _HOST or delivery.type != "team.objective_completed":
        return None
    if type(objective_revision) is not int or not 0 <= objective_revision < delivery.seq:
        raise ValueError("objective_revision must be an integer before delivery")
    handoff = delivery.payload.get("handoff")
    boundary = delivery.payload.get("objective_seq")
    if (type(handoff) is not dict or type(handoff.get("version")) is not int
            or handoff["version"] != 1 or type(boundary) is not int
            or not 0 <= boundary < delivery.seq):
        return None
    refs = _references(handoff, delivery.seq)
    sources = {event.seq: (event, start) for event, start in bound_sources
               if event.task_id == delivery.task_id and start.task_id == delivery.task_id
               and boundary < start.seq < event.seq < delivery.seq
               and type(start.payload.get("revision")) is int
               and start.payload["revision"] == objective_revision
               and refs.get(event.seq) == event.hash}
    commands = [pair for pair in sources.values()
                if pair[0].payload["name"] == "workspace_run" and _failed(pair[0].payload["result"])]
    if not commands:
        return None
    command, start = max(commands, key=lambda pair: pair[0].seq)
    scope = tuple(start.payload.get(key) for key in _SCOPE)
    files = []
    if (isinstance(scope[0], str) and scope[0] and type(scope[1]) is int
            and isinstance(scope[2], str) and scope[2]):
        files = sorted((pair for pair in sources.values()
                        if pair[0].payload["name"] == "workspace_read"
                        and pair[0].payload["result"].get("status") == "ok"
                        and pair[0].seq < start.seq
                        and tuple(pair[1].payload.get(key) for key in _SCOPE) == scope),
                       key=lambda pair: pair[0].seq, reverse=True)
    view = {"delivery_seq": delivery.seq, "sources": [],
            "sources_truncated": len(files) > 2, "note": _NOTE}
    for event, _ in [(command, start), *files[:2]]:
        row = _source(event)
        if len(json_utf8({**view, "sources": [*view["sources"], row]})) > max_bytes:
            row = {key: value for key, value in row.items() if key != "parts"}
            row.update(body_included=False, omission_reason="context_budget")
        if len(json_utf8({**view, "sources": [*view["sources"], row]})) <= max_bytes:
            view["sources"].append(row)
        elif event is command:
            return None
        else:
            view["sources_truncated"] = True
    return view


def _references(handoff, before_seq):
    refs = {}
    for key, limit in (("command_receipts", 4), ("evidence", 16)):
        values = handoff.get(key, [])
        if type(values) is not list or len(values) > limit:
            continue
        for value in values:
            if type(value) is not dict:
                continue
            seq, fingerprint = value.get("source_seq"), value.get("source_hash")
            if type(seq) is int and 0 < seq < before_seq and isinstance(fingerprint, str):
                # Conflicting hashes must not let list order choose the source.
                refs[seq] = fingerprint if seq not in refs else (
                    fingerprint if refs[seq] == fingerprint else None)
    return refs


def _failed(result):
    if (result.get("timed_out", False) is not False
            or result.get("outcome") not in (None, "passed", "failed")
            or any(result.get(key) is not None for key in ("exception", "launch_error", "cleanup_error"))):
        return False
    return (result.get("status") == "error" or result.get("outcome") == "failed"
            or type(result.get("exit_code")) is int and result["exit_code"] != 0)


def _parts(result, tool):
    if tool == "workspace_run":
        if result.get("complete") is not True or result.get("truncated") is not False:
            return None
        streams = [(key, _captured_text(result.get(key))) for key in ("stdout", "stderr")]
        if any(text is None for _, text in streams):
            return None
        return [{"field": key + ".excerpt", "literal": text} for key, text in streams]
    content, size = result.get("content"), result.get("bytes")
    if (type(content) is not str or type(size) is not int or size < 0
            or type(result.get("offset")) is not int or result["offset"] != 0
            or type(result.get("returned_bytes")) is not int or result["returned_bytes"] != size
            or result.get("truncated") is not False or result.get("encoding") != "utf-8"):
        return None
    try:
        if len(content.encode("utf-8")) != size:
            return None
    except UnicodeError:
        return None
    return [{"field": "content", "literal": content}]


def _source(event):
    payload, result = event.payload, event.payload["result"]
    tool = payload["name"]
    row = {"source_seq": event.seq, "source_hash": event.hash, "tool": tool,
           "role": payload["role"], "status": result["status"],
           "read_tool": "read_receipts", "read_args": {"receipt_seq": event.seq},
           "body_included": False}
    if tool == "workspace_run":
        if isinstance(result.get("outcome"), str):
            row["outcome"] = result["outcome"]
        if type(result.get("exit_code")) is int:
            row["exit_code"] = result["exit_code"]
        for key in ("complete", "truncated"):
            if type(result.get(key)) is bool:
                row[key] = result[key]
    elif isinstance(result.get("path"), str):
        row["path"] = result["path"]
    parts = _parts(result, tool)
    if parts is None:
        row["omission_reason"] = "incomplete_capture"
    elif sum(len(part["literal"].encode("utf-8")) for part in parts) > MAX_SOURCE_BYTES:
        row["omission_reason"] = "source_budget"
    else:
        row.update(body_included=True, parts=parts)
    return row
