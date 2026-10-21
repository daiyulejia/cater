"""Explicit withdrawal of ordinary work, without manufacturing a return."""

from __future__ import annotations

from .events import Actor


HOST = Actor.system("team")


def linked_inputs(messages, roots, *, include_feedback=True, direct_feedback_roots=None):
    """Follow continuations and feedback resuming roots, never cancel child work.

    Narrowing direct feedback roots never exempts feedback resuming a withdrawn parent.
    """
    messages = sorted(messages, key=lambda event: event.seq)
    by_seq = {event.seq: event for event in messages}
    roots = set(roots)
    direct_feedback_roots = roots if direct_feedback_roots is None else set(direct_feedback_roots) & roots
    continuations = set(roots)
    inactive = set(roots)
    for message in messages:
        p = message.payload
        original = by_seq.get(p.get("continue_assignment_seq"))
        if (p.get("kind") == "continuation" and original is not None
                and original.seq in continuations and original.seq < message.seq
                and message.task_id == original.task_id
                and message.actor.kind == "agent" and message.actor == original.actor
                and all(p.get(key) == original.payload.get(key)
                        for key in ("recipient", "revision"))):
            continuations.add(message.seq)
            inactive.add(message.seq)
        if not include_feedback:
            continue
        original = by_seq.get(p.get("reply_to"))
        parent = by_seq.get(original.payload.get("parent_assignment_seq")) if original else None
        resumes_root = (parent is not None and parent.seq in roots
            and parent.seq < original.seq
            and parent.task_id == original.task_id
            and parent.payload.get("kind") in {"assignment", "message"}
            and original.payload.get("kind") in {"assignment", "message"}
            and parent.payload.get("recipient") == original.actor.id
            and parent.payload.get("revision") == original.payload.get("revision"))
        if (p.get("kind") in {"progress", "report"} and original is not None
                and (original.seq in direct_feedback_roots or resumes_root) and original.seq < message.seq
                and message.task_id == original.task_id
                and message.actor.kind == "agent"
                and message.actor.id == original.payload.get("recipient")
                and p.get("recipient") == original.actor.id
                and p.get("revision") == original.payload.get("revision")):
            inactive.add(message.seq)
    return inactive


def _error(code, message):
    return {"status": "error", "code": code, "message": message,
            "complete": True, "outcome": "not_started"}


class TeamWorkControl:
    def __init__(self, team):
        self.team, self.rt = team, team.rt

    def _members(self):
        return {role.id for role in [self.rt.organization.leader, *self.rt.roles]}

    def _ordinary(self, source, task_id, *, historical=False):
        members = self._members()
        return (source.task_id == task_id and source.type == "team.message_received"
                and source.actor.kind == "agent" and bool(source.actor.id)
                and source.payload.get("kind") in {"assignment", "message"}
                and isinstance(source.payload.get("recipient"), str) and bool(source.payload["recipient"])
                and (historical or source.actor.id in members and source.payload["recipient"] in members)
                and source.payload["recipient"] != source.actor.id
                and type(source.payload.get("revision")) is int)

    def view(self, task_id, events=None):
        """Project only authentic withdrawals, using the supplied snapshot alone."""
        events = list(events) if events is not None else self.rt.log.read(task_id=task_id)
        events = sorted((e for e in events if e.task_id == task_id), key=lambda e: e.seq)
        messages = {e.seq: e for e in events if e.type == "team.message_received"}
        rows, seen = [], set()
        revision, done = 0, False
        for event in events:
            if event.actor != HOST:
                continue
            if event.type in {"team.objective_updated", "team.objective_completed"}:
                revision = event.seq
                done = event.type == "team.objective_completed"
            if event.type != "team.assignment_withdrawn":
                continue
            p = event.payload
            if type(p.get("message_seq")) is not int:
                continue
            source = messages.get(p["message_seq"])
            if (source is None or not self._ordinary(source, task_id, historical=True) or done
                    or not revision <= source.seq < event.seq
                    or type(p.get("revision")) is not int
                    or p["revision"] != revision or p["revision"] != source.payload["revision"]
                    or p.get("source_hash") != source.hash
                    or not isinstance(p.get("leader_id"), str) or not p["leader_id"]
                    or p.get("requester") not in {source.actor.id, p["leader_id"]}
                    or p.get("sender", source.actor.id) != source.actor.id
                    or p.get("recipient", source.payload["recipient"]) != source.payload["recipient"]
                    or not isinstance(p.get("reason"), str) or not p["reason"].strip()
                    or not isinstance(p.get("run_id"), str) or not p["run_id"].strip()
                    or source.seq in seen):
                continue
            seen.add(source.seq)
            rows.append({**p, "withdrawal_seq": event.seq})
        return rows

    def inactive(self, task_id, events=None, *, started=None):
        events = list(events) if events is not None else self.rt.log.read(task_id=task_id)
        events = [e for e in events if e.task_id == task_id]
        withdrawals = self.view(task_id, events=events)
        roots = {row["message_seq"] for row in withdrawals}
        direct_feedback_roots = set(roots)
        if (started is not None and started.task_id == task_id and started.actor == HOST
                and started.type == "team.run_started"
                and isinstance(started.payload.get("run_id"), str) and started.payload["run_id"].strip()):
            # A sender may retire work while consuming its feedback. Only that
            # existing run survives; roots, continuations and withdrawn parents do not.
            direct_feedback_roots -= {row["message_seq"] for row in withdrawals
                if row["run_id"] == started.payload["run_id"]
                and row["requester"] == started.payload.get("role")
                and row["revision"] == started.payload.get("revision")
                and started.payload["message_seq"] < started.seq < row["withdrawal_seq"]}
        return linked_inputs([e for e in events if e.type == "team.message_received"], roots,
                             direct_feedback_roots=direct_feedback_roots)

    def execute(self, task_id, role, arguments, tool_start):
        """The native tool dispatcher owns the event/receipt transaction."""
        seq, reason = arguments.get("message_seq"), arguments.get("reason")
        if (type(seq) is not int or seq < 1 or not isinstance(reason, str)
                or not reason.strip() or len(reason) > 4000
                or set(arguments) != {"message_seq", "reason"}):
            return _error("invalid_arguments", "请填写本群有效的正整数消息序号 message_seq，以及不超过4000字的具体撤回原因 reason。")
        events = self.rt.log.read(task_id=task_id)
        messages = self.team._messages(task_id, events=events)
        source = next((e for e in messages if e.seq == seq), None)
        if source is None or not self._ordinary(source, task_id):
            return _error("invalid_work", "只能撤回本群伙伴之间的普通委托或请求，不能撤回顾客输入、其他群的消息或非工作消息。")
        if role.id not in {source.actor.id, self.rt.organization.leader.id}:
            return _error("withdrawal_forbidden", "只有原发件人或店长可以撤回这份工作，请向原发件人或店长说明撤回原因。")
        view = self.team.view(task_id)
        if (tool_start.task_id != task_id or tool_start.actor != HOST
                or tool_start.type != "team.tool_started"
                or tool_start.payload.get("role") != role.id
                or tool_start.payload.get("name") != "withdraw_work"
                or not isinstance(tool_start.payload.get("run_id"), str)
                or not tool_start.payload["run_id"].strip()
                or type(tool_start.payload.get("revision")) is not int
                or tool_start.payload["revision"] != view["revision"]
                or source.payload["revision"] != view["revision"]
                or view["status"] != "running"
                or self.rt.execution_control.view(task_id)["state"] != "running"):
            return _error("work_not_current", "工作已暂停、交付或目标已变化，不能用旧的工具请求撤回工作；请先核对当前目标和执行状态。")
        previous = next((row for row in self.view(task_id, events=events) if row["message_seq"] == seq), None)
        if previous:
            return {"status": "ok", "message_seq": seq, "withdrawn": True,
                    "already_withdrawn": True, "withdrawal_seq": previous["withdrawal_seq"]}
        if seq in self.rt.team_reviews.superseded_assignments(task_id, events=events):
            return {"status": "ok", "message_seq": seq, "withdrawn": False, "already_inactive": True}
        if any(e.actor.kind == "agent" and e.seq > seq
               and e.payload.get("kind") == "report" and e.payload.get("reply_to") == seq
               and e.actor.id == source.payload["recipient"]
               and e.payload.get("recipient") == source.actor.id
               and e.payload.get("revision") == source.payload["revision"] for e in messages):
            return {"status": "ok", "message_seq": seq, "withdrawn": False, "already_reported": True}
        # A review_seq tag alone does not identify a formal review delivery.
        for row in self.rt.team_reviews.view(task_id):
            if row.get("revision") != view["revision"] or row.get("status") == "stale":
                continue
            actual = self.rt.team_reviews._request_message(task_id, row)
            if actual is not None and actual.seq == seq:
                return _error("formal_review", "这份工作是当前正式复核的实际委托，不能直接撤回；请沿用当前复核，或由作者发布新版后承接原复核链重新提交。")
        event = self.rt.log.emit(type="team.assignment_withdrawn", task_id=task_id, actor=HOST, payload={
            "message_seq": seq, "source_hash": source.hash, "requester": role.id,
            "sender": source.actor.id, "recipient": source.payload["recipient"],
            "leader_id": self.rt.organization.leader.id,
            "revision": view["revision"], "reason": reason.strip(),
            "run_id": tool_start.payload["run_id"]})
        return {"status": "ok", "message_seq": seq, "withdrawn": True, "withdrawal_seq": event.seq}
