"""Read-only pending presentation. Never use these rows to grant execution."""

from copy import deepcopy
import json

from .events import Actor, Event


HOST = Actor.system("team")
_IDENTITY = ("role", "run_id", "message_seq")


def _current_query_seq(team, task_id, tool_start, role_id):
    if (not isinstance(tool_start, Event) or type(tool_start.seq) is not int or tool_start.seq <= 0
            or tool_start.task_id != task_id or tool_start.actor != HOST
            or tool_start.type != "team.tool_started"):
        return None
    p = tool_start.payload
    if (p.get("name") != "read_checkpoint" or not isinstance(role_id, str) or p.get("role") != role_id
            or type(p.get("message_seq")) is not int or not 0 < p["message_seq"] < tool_start.seq
            or any(not isinstance(p.get(k), str) or not p[k].strip() for k in ("run_id", "call_id"))):
        return None
    events = team.rt.log.read(task_id=task_id)
    stored = next((e for e in events if e.seq == tool_start.seq), None)
    if (stored != tool_start or json.dumps(stored.payload, sort_keys=True) != json.dumps(p, sort_keys=True)):
        return None
    message = next((e for e in events if e.seq == p["message_seq"]
                    and e.type == "team.message_received" and e.payload.get("recipient") == role_id), None)

    def same_run(event):
        return all(type(event.payload.get(k)) is type(p[k]) and event.payload.get(k) == p[k]
                   for k in _IDENTITY)

    runs = [e for e in events if e.actor == HOST and e.type == "team.run_started"
            and p["message_seq"] < e.seq < tool_start.seq and same_run(e)]
    if message is None or len(runs) != 1:
        return None
    if any(e.actor == HOST and e.seq > runs[0].seq and same_run(e)
           and (e.type == "team.run_finished"
                or e.type == "team.tool_started" and e.seq > tool_start.seq
                or e.type in {"team.tool_finished", "team.tool_reconciled"}
                and e.payload.get("started_seq") == tool_start.seq) for e in events):
        return None
    return tool_start.seq


def pending_counts(rows):
    live = sum(row["scheduler_owned_live"] is True for row in rows)
    return {"pending_operation_count": len(rows), "pending_live_count": live,
            "pending_review_count": len(rows) - live}


def display_pending(team, task_id, *, tool_start=None, role_id=None):
    """Keep all unconfirmed effects; exclude only the authenticated current query."""
    with team._context_read():
        excluded = _current_query_seq(team, task_id, tool_start, role_id)
        rows = deepcopy(team._pending_operations(task_id))
        result = []
        for row in rows:
            if (row.get("started_seq") == excluded and row.get("name") == "read_checkpoint"
                    and row.get("run_ended") is False and "finished_seq" not in row):
                continue
            row["scheduler_owned_live"] = (row.get("name") == "external_agent"
                and team.external.owns(task_id, row, observe_only=True))
            result.append(row)
        return result
