"""Bounded command identity for an already authenticated receipt/start pair.

The caller must validate the pair with TeamReadSources._chain first. This is
metadata, not evidence that requested arguments were actually executed. Page
budgeting belongs to the parent receipt index.
"""

from __future__ import annotations

import json

from .events import Actor


_HOST = Actor.system("team")
_REQUEST_FIELDS = (("$.arguments.command", 128), ("$.arguments.args", 512),
                   ("$.arguments.cwd", 256))
_ARGV_FIELDS = (("$.result.argv", 512),)
_TREE_GUARD = (
    "NOT EXISTS (SELECT 1 FROM json_tree(payload_json) WHERE typeof(key) = 'text' "
    "GROUP BY parent, CAST(key AS BLOB) HAVING count(*) > 1 "
    "OR instr(CAST(key AS BLOB), x'00') > 0)"
)


def _project(sources, task_id, event, fields, identity, *, request=False):
    """Return only bounded cells; SQL NULL retains its separate JSON type."""
    conditions = ["task_id = ?", "seq = ?", "type = ?", "hash = ?",
                  f"CASE WHEN ({sources._guard()}) THEN ({_TREE_GUARD}) ELSE 0 END",
                  sources._guard().replace("payload_json", "actor_json")]
    parameters = [task_id, event.seq, event.type, event.hash]
    for key, value in _HOST.to_dict().items():
        conditions.append(f"json_extract(actor_json, '$.{key}') = ?")
        parameters.append(value)
    conditions.append("(SELECT count(*) FROM json_each(actor_json)) = 3")
    for path, value in identity.items():
        # Paths are module-authored constants, never caller-provided SQL.
        if value is None:
            conditions.append(f"json_type(payload_json, '{path}') IS NULL")
        else:
            kind = "integer" if type(value) is int else "text"
            conditions.extend([f"json_type(payload_json, '{path}') = '{kind}'",
                               f"json_extract(payload_json, '{path}') = ?"])
            parameters.append(value)
    columns = sources._columns(fields)
    if request:
        columns += ", json_type(payload_json, '$.arguments.stdin') IS NOT NULL AS stdin_recorded"
    cursor = sources.rt.log.db.execute(
        "SELECT " + columns + " FROM events WHERE " + " AND ".join(conditions), parameters)
    try:
        return cursor.fetchone()
    finally:
        cursor.close()


def _value(row, index, expected):
    """None means unavailable, never a substituted recorded JSON null."""
    if row[f"k{index}"] != expected or not isinstance(row[f"v{index}"], bytes):
        return None
    try:
        text = row[f"v{index}"].decode("utf-8")
        if expected == "text":
            return text
        value = json.loads(text)
        if type(value) is not list or any(type(item) is not str for item in value):
            return None
        for item in value:
            item.encode("utf-8")  # Escaped lone surrogates survive json.loads.
        return value
    except (ValueError, UnicodeError):
        return None


def command_identity(sources, task_id, event, start) -> dict | None:
    """Project a chain-validated workspace_run, rechecking stored task/identity.

    Missing fields are omitted. Present but invalid/oversized request fields
    are named as unavailable; argv_available alone identifies usable argv.
    No stdin, output body, or whole event is decoded into Python. Short inline
    arguments stay exact; oversized argument arrays are explicitly unavailable.
    """
    if start is None or event is None:
        return None
    for item, kind in ((start, "team.tool_started"), (event, "team.tool_finished")):
        if (item.task_id != task_id or item.type != kind or item.actor != _HOST
                or type(item.seq) is not int or not 0 < item.seq < 2 ** 63
                or not isinstance(item.payload, dict)
                or item.payload.get("name") != "workspace_run"):
            return None
    s, p = start.payload, event.payload
    if (start.seq >= event.seq or type(p.get("started_seq")) is not int
            or p["started_seq"] != start.seq or type(s.get("message_seq")) is not int
            or any(type(s.get(key)) is not str or not s[key].strip()
                   for key in ("run_id", "role", "call_id"))
            or any(p.get(key) != s[key] for key in ("run_id", "role"))
            or "call_id" in p and p["call_id"] != s["call_id"]
            or not isinstance(p.get("result"), dict)
            or p["result"].get("status") not in ("ok", "error")):
        return None
    start_identity = {f"$.{key}": s[key] for key in
                      ("name", "run_id", "role", "call_id", "message_seq")}
    finish_identity = {f"$.{key}": p[key] for key in
                       ("name", "run_id", "role", "started_seq")}
    finish_identity.update({"$.call_id": p.get("call_id"),
                            "$.result.status": p["result"]["status"]})
    try:
        request_row = _project(sources, task_id, start, _REQUEST_FIELDS, start_identity, request=True)
        if request_row is None:
            return None
        argv_row = _project(sources, task_id, event, _ARGV_FIELDS, finish_identity)
        if argv_row is None:
            return None
    except UnicodeError:
        return None
    request, unavailable = {}, []
    for index, key in enumerate(("command", "args", "cwd")):
        if request_row[f"k{index}"] is None:
            continue
        value = _value(request_row, index, "array" if key == "args" else "text")
        if value is None:
            unavailable.append(key)
        else:
            request[key] = value
    argv = _value(argv_row, 0, "array")
    return {"request": request, "request_fields_unavailable": unavailable,
            "stdin_recorded": bool(request_row["stdin_recorded"]),
            "argv": argv, "argv_available": argv is not None,
            "request_hash": start.hash,
            "request_read_args": {"section": "request", "receipt_seq": event.seq}}
