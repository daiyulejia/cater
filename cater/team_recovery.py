"""Fresh supervision after a lost consumer, reconstructed from durable facts."""

from __future__ import annotations

from .activity_owner import activity_owner
from .events import Actor
from .work import WorkConflict


HOST = Actor.system("team")
RECOVERY_CONTRACT = {"version": 1, "kind": "fresh_supervision"}


class TeamRecovery:
    def __init__(self, team):
        self.team, self.rt = team, team.rt

    def scope(self, task_id, *, events=None):
        events = events if events is not None else self.rt.log.read(task_id=task_id)
        objectives = [e for e in events if e.actor == HOST
                      and e.type in {"team.objective_updated", "team.objective_completed"}]
        workspace = self.rt.workspaces.view(task_id)
        control = self.rt.execution_control.view(task_id)
        return {"revision": objectives[-1].seq if objectives else 0,
                "control_seq": control["seq"], "control_state": control["state"],
                "workspace_revision": workspace.get("revision"),
                "workspace_fingerprint": workspace.get("fingerprint"),
                "workspace_available": workspace["available"],
                "execution_fingerprint": self.rt.work.organization_fingerprint,
                "rule_fingerprint": self.rt.rulebook.current_snapshot(task_id)["fingerprint"],
                "done": bool(objectives and objectives[-1].type == "team.objective_completed")}

    def is_message(self, event, events=None, *, by_seq=None):
        if (event.actor != HOST or event.type != "team.message_received"
                or event.payload.get("kind") != "recovery"
                or event.payload.get("recipient") != self.rt.organization.leader.id):
            return False
        p = event.payload
        if type(p.get("recovery_of")) is not int or type(p.get("interrupted_seq")) is not int:
            return False
        events = events if events is not None else self.rt.log.read(task_id=event.task_id)
        by_seq = by_seq if by_seq is not None else {e.seq: e for e in events}
        start, finish = by_seq.get(p["recovery_of"]), by_seq.get(p["interrupted_seq"])
        source = by_seq.get(start.payload.get("message_seq")) if start else None
        return bool(start and finish and source and source.seq < start.seq < finish.seq < event.seq
            and start.actor == finish.actor == HOST and start.type == "team.run_started"
            and finish.type == "team.run_finished" and finish.payload.get("status") == "interrupted"
            and start.payload.get("recovery_contract") == RECOVERY_CONTRACT
            and type(start.payload["recovery_contract"].get("version")) is int
            and source.type == "team.message_received" and source.actor.kind in {"human", "agent"}
            and source.payload.get("recipient") == start.payload.get("role")
            and start.hash == p.get("recovery_hash") and finish.payload.get("recovery_of") == start.seq
            and all(finish.payload.get(k) == start.payload.get(k) for k in ("message_seq", "role", "run_id"))
            and isinstance(start.payload.get("run_id"), str) and start.payload["run_id"].strip()
            and p.get("scope") == start.payload.get("recovery_scope")
            and p.get("revision") == start.payload.get("revision")
            and not any(e.seq < event.seq and e.actor == HOST and e.type == "team.message_received"
                        and e.payload.get("kind") == "recovery" and e.payload.get("recovery_of") == start.seq
                        and e.payload.get("recovery_hash") == start.hash for e in events))

    def current(self, task_id, message):
        scope = self.scope(task_id)
        return (scope["control_state"] == "running" and not scope["done"]
                and message.payload.get("scope") == scope)

    def retire(self, task_id):
        """Caller owns simple-team; closing and notification commit together."""
        with self.rt.log.atomic():
            events = self.rt.log.read(task_id=task_id)
            finished = {(e.payload.get("run_id"), e.payload.get("message_seq"), e.payload.get("role"))
                        for e in events if e.actor == HOST and e.type == "team.run_finished"}
            sources = {e.seq: e for e in events if e.type == "team.message_received"}
            members = {r.id: r.name for r in [self.rt.organization.leader, *self.rt.roles]}
            scope = self.scope(task_id)
            for start in events:
                if (start.actor != HOST or start.type != "team.run_started" or
                        tuple(start.payload.get(k) for k in ("run_id", "message_seq", "role")) in finished):
                    continue
                p = start.payload
                source = sources.get(p.get("message_seq"))
                eligible = (source is not None and source.seq < start.seq and source.actor.kind in {"human", "agent"}
                    and p.get("role") in members
                    and source.payload.get("recipient") == p.get("role")
                    and p.get("recovery_contract") == RECOVERY_CONTRACT
                    and type(p["recovery_contract"].get("version")) is int
                    and p.get("recovery_scope") == scope and p.get("revision") == scope["revision"]
                    and scope["control_state"] == "running" and not scope["done"]
                    and not any(e.actor == Actor.human() and e.seq > start.seq for e in sources.values()))
                terminal = self.rt.log.emit(type="team.run_finished", task_id=task_id, actor=HOST,
                    payload={**{key: p.get(key) for key in ("message_seq", "role", "run_id", "revision")},
                             "status": "interrupted", "recovery_of": start.seq,
                             "reason": "上次执行进程已结束，保留已有操作记录，未重放旧模型请求。"})
                if eligible:
                    self.rt.log.emit(type="team.message_received", task_id=task_id, actor=HOST, payload={
                        "kind": "recovery", "recipient": self.rt.organization.leader.id,
                        "revision": scope["revision"], "scope": scope,
                        "recovery_of": start.seq, "recovery_hash": start.hash, "interrupted_seq": terminal.seq,
                        "text": (f"{members[p['role']]}上次处理消息 #{p['message_seq']} 时执行进程中断。"
                                 "已有成果和操作记录已保留，请店长核对现状后安排未完成的工作。")})

    def prepare(self, task_id):
        if self.rt.log.db.in_transaction:
            raise WorkConflict("恢复准备不能处于外层事务中；通知必须先持久化，再启动模型。")
        with activity_owner(self.rt.log.path, task_id, "simple-team") as owned:
            if not owned or not self.team.enabled(task_id):
                return []
            self.retire(task_id)
            events = self.rt.log.read(task_id=task_id)
            started = {e.payload.get("message_seq") for e in events if e.actor == HOST and e.type == "team.run_started"}
            return [e for e in events if e.seq not in started and self.is_message(e, events) and self.current(task_id, e)]

    def assignments(self, task_id, *, events=None):
        from .team import _reported_messages

        events = events if events is not None else self.rt.log.read(task_id=task_id)
        messages = self.team._messages(task_id, events=events)
        reported = _reported_messages(messages)
        revision = max((e.seq for e in events if e.actor == HOST
                        and e.type in {"team.objective_updated", "team.objective_completed"}), default=0)
        runs, continuations, interrupted_roots = {}, {}, set()
        for e in events:
            if e.actor == HOST and e.type == "team.run_started":
                runs[e.payload.get("message_seq")] = {**e.payload, "status": "running"}
            elif e.actor == HOST and e.type == "team.run_finished":
                run = runs.get(e.payload.get("message_seq"))
                if run and all(run.get(k) == e.payload.get(k) for k in ("role", "run_id")):
                    run.update(e.payload)
        by_seq = {e.seq: e for e in messages}
        for e in messages:
            source = by_seq.get(e.payload.get("continue_assignment_seq"))
            if (e.payload.get("kind") == "continuation" and source and source.seq < e.seq
                    and e.actor == source.actor and e.payload.get("recipient") == source.payload.get("recipient")
                    and e.payload.get("revision") == revision):
                continuations[source.seq] = e
            if runs.get(e.seq, {}).get("status") in {"failed", "interrupted"}:
                interrupted_roots.add(source.seq if source and e.payload.get("kind") == "continuation" else e.seq)
        rows = []
        for source in messages:
            if (source.actor.kind != "agent" or source.payload.get("kind") not in {"assignment", "message"}
                    or source.payload.get("revision") != revision or source.seq in reported):
                continue
            latest = continuations.get(source.seq, source)
            run = runs.get(latest.seq, {})
            rows.append({"seq": source.seq, "sender": source.actor.id, "recipient": source.payload["recipient"],
                         "text": source.payload["text"], "latest_message_seq": latest.seq,
                         "requires_return": source.seq in interrupted_roots,
                         "status": run.get("status", "queued"), "reason": run.get("reason", ""),
                         "run_id": run.get("run_id")})
        return rows

    def checkpoint(self, task_id):
        events = self.rt.log.read(task_id=task_id)
        assignments = self.assignments(task_id, events=events)
        objectives = [e for e in events if e.actor == HOST and e.type == "team.objective_updated"]
        objective = objectives[-1].payload["goal"] if objectives else self.rt.task(task_id).goal
        return {"as_of_seq": events[-1].seq if events else 0,
                "as_of_hash": events[-1].hash if events else None,
                "scope": self.scope(task_id, events=events), "objective": objective,
                "open_work": [{k: v for k, v in row.items() if k != "text"} for row in assignments[-20:]],
                "open_work_count": len(assignments), "open_work_truncated": len(assignments) > 20,
                "pending_operation_count": len(self.team._pending_operations(task_id)),
                "note": "从持久事件重建的当前事实，不是恢复旧模型历史、重放操作或宣告验收的授权。"}

    def read(self, task_id, arguments):
        after, limit = arguments.get("after_seq", 0), arguments.get("limit", 20)
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("检查点读取需要非负序号和1至50条分页大小。")
        rows = [r for r in self.assignments(task_id) if r["seq"] > after]
        page = rows[:limit]
        return {"status": "ok", "checkpoint": self.checkpoint(task_id), "assignments": page,
                "has_more": len(rows) > limit, "next_seq": page[-1]["seq"] if page else after}

    def continuation(self, task_id, role, recipient, seq):
        if type(seq) is not int or seq < 1:
            raise ValueError("接续需要本群原委托的有效序号。")
        source = next((r for r in self.assignments(task_id) if r["seq"] == seq), None)
        if source is None or source["sender"] != role.id or source["recipient"] != recipient:
            raise WorkConflict("只能由原发件人接续当前未交还的原委托，不能重开旧目标或代替其他伙伴派活。")
        if source["status"] in {"queued", "running"} or any(
                r["role"] == recipient and r["status"] == "running" for r in self.team.view(task_id)["runs"]):
            raise WorkConflict("原接收者已有待处理或在途工作，不重复投递接续。")
        return source

    def reconcile_publication(self, task_id, started_seq, *, reason, invocation_stopped=False):
        """Trusted local operator only; models receive no self-unlock tool."""
        if (type(started_seq) is not int or started_seq < 1 or invocation_stopped is not True
                or not isinstance(reason, str) or not reason.strip() or len(reason) > 4000):
            raise ValueError("需要发布调用序号、确认原执行已停止，并说明实际核对的成果与文件影响。")
        with activity_owner(self.rt.log.path, task_id, "simple-team") as owned:
            if not owned:
                raise WorkConflict("原生团队仍有执行者，不能关闭发布操作的未知状态。")
            with self.rt.log.atomic():
                pending = next((r for r in self.team._pending_operations(task_id) if r["started_seq"] == started_seq
                                and r["name"] in {"publish_artifact", "publish_workspace_file"} and r["run_ended"]), None)
                if pending is None:
                    raise WorkConflict("该发布调用不是本群已结束运行中待核对的操作。")
                start = next(e for e in self.rt.log.read(task_id=task_id) if e.seq == started_seq)
                return self.rt.log.emit(type="team.tool_reconciled", task_id=task_id, actor=Actor.human(), payload={
                    "started_seq": start.seq, "started_hash": start.hash, "call_id": start.payload["call_id"],
                    **{k: start.payload[k] for k in ("run_id", "role", "name")},
                    "reason": reason.strip(), "invocation_stopped": True, "outcome": "effects_reviewed_no_replay"})
