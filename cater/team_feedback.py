"""Settle fully presented child feedback with an explicit upstream return.

This acknowledges queue delivery, never correctness or review approval. Old
returns without a host receipt remain unchanged; later feedback stays pending.
"""

from .events import Actor


HOST = Actor.system("team")
RETURN_TOOLS = frozenset({"return_work", "send_message"})


def _lookup(events, seq):
    return events.get(seq) if type(seq) is int else None


def _parent(events, report, start):
    from .team import _assignment_report

    parent = _lookup(events, report.payload.get("reply_to")) if report else None
    if (parent is not None and parent.type == report.type == "team.message_received"
            and parent.actor.kind == report.actor.kind == "agent"
            and parent.task_id == report.task_id == start.task_id
            and parent.seq < start.seq < report.seq
            and report.actor.id == start.payload.get("role")
            and type(start.payload.get("revision")) is int
            and parent.payload.get("revision") == report.payload.get("revision") == start.payload["revision"]
            and _assignment_report(parent, report)):
        return parent
    return None


def _child_report(events, feedback, parent, before):
    from .team import _assignment_report

    child = _lookup(events, feedback.payload.get("reply_to")) if feedback else None
    return bool(child is not None and child.type == feedback.type == "team.message_received"
        and parent.seq < child.seq < feedback.seq < before
        and parent.task_id == child.task_id == feedback.task_id
        and child.actor.kind == feedback.actor.kind == "agent"
        and child.actor.id == parent.payload["recipient"]
        and child.payload.get("parent_assignment_seq") == parent.seq
        and child.payload.get("revision") == feedback.payload.get("revision") == parent.payload["revision"]
        and _assignment_report(child, feedback))


def presented_feedback(team, task_id, start, result, context):
    """Called inside the new return's transaction, with the actual model input."""
    if (start.payload.get("name") not in RETURN_TOOLS or result.get("status") != "ok"
            or result.get("already_reported") or not isinstance(context, dict)):
        return []
    events = team.rt.log.read(task_id=task_id)
    by_seq = {e.seq: e for e in events}
    report = _lookup(by_seq, result.get("message_seq"))
    parent = _parent(by_seq, report, start)
    if parent is None:
        return []
    claimed = {e.payload["message_seq"] for e in events
               if e.actor == HOST and e.type == "team.run_started"
               and type(e.payload.get("message_seq")) is int}
    inactive = team._inactive_messages(task_id, events=events)
    reviews = team.rt.team_reviews.view(task_id)
    consumed = set()
    for row in context.get("conversation", []):
        feedback = _lookup(by_seq, row.get("seq")) if isinstance(row, dict) else None
        if (feedback is None or feedback.seq in claimed or feedback.seq in inactive
                or not _child_report(by_seq, feedback, parent, start.seq)
                or row.get("text_truncated") or row.get("text_reference")
                or row.get("sender_kind") != "agent" or row.get("sender") != feedback.actor.id
                or any(row.get(key) != feedback.payload.get(key)
                       for key in ("text", "recipient", "kind", "reply_to"))):
            continue
        if "review_seq" in feedback.payload and not any(
                r["request_seq"] == feedback.payload["review_seq"]
                and r["status"] in {"approved", "changes_requested"}
                and r.get("decision_seq", feedback.seq) < feedback.seq for r in reviews):
            continue
        consumed.add(feedback.seq)
    return sorted(consumed)


def consumed_feedback(events):
    """Project committed host receipts; subsequent review changes cannot unsee input."""
    by_seq = {e.seq: e for e in events}
    consumed, claimed, runs = set(), set(), {}
    for event in events:
        if event.actor != HOST:
            continue
        p = event.payload
        if event.type == "team.run_started":
            if type(p.get("message_seq")) is int:
                claimed.add(p["message_seq"])
            if isinstance(p.get("run_id"), str) and p["run_id"]:
                runs[p["run_id"]] = event
        if (event.type != "team.tool_finished" or not isinstance(p.get("consumed_input_seqs"), list)
                or not isinstance(p.get("name"), str) or p["name"] not in RETURN_TOOLS
                or not isinstance(p.get("run_id"), str)):
            continue
        seqs, result = p.get("consumed_input_seqs"), p.get("result")
        start = _lookup(by_seq, p.get("started_seq"))
        run = runs.get(p.get("run_id"))
        if (not isinstance(seqs, list) or not isinstance(result, dict)
                or result.get("status") != "ok" or result.get("already_reported")
                or start is None or run is None or start.actor != HOST
                or start.type != "team.tool_started" or not run.seq < start.seq < event.seq
                or start.task_id != event.task_id or run.task_id != event.task_id
                or any(p.get(key) != start.payload.get(key) for key in ("name", "role", "run_id"))
                or any(run.payload.get(key) != start.payload.get(key)
                       for key in ("role", "message_seq", "revision"))):
            continue
        report = _lookup(by_seq, result.get("message_seq"))
        parent = _parent(by_seq, report, start)
        if parent is None or report.seq >= event.seq:
            continue
        for seq in seqs:
            feedback = _lookup(by_seq, seq)
            if (feedback is not None and seq not in claimed
                    and _child_report(by_seq, feedback, parent, start.seq)):
                consumed.add(seq)
    return consumed
