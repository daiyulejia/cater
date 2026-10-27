"""Fresh supervision after a lost consumer, reconstructed from durable facts."""

from __future__ import annotations

import json

from .activity_owner import activity_owner
from .events import Actor
from .execution_control import paused_predecessor
from .team_pending_display import display_pending, pending_counts
from .work import WorkConflict


HOST = Actor.system("team")
RECOVERY_CONTRACT = {"version": 1, "kind": "fresh_supervision"}
_UNSPECIFIED_RECIPIENT = object()


def _same_scope(left, right):
    return (type(left) is dict and type(right) is dict
            and json.dumps(left, sort_keys=True) == json.dumps(right, sort_keys=True))


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
        if p.get("trigger") == "control_resume":
            return self._resume_message(event, events, by_seq=by_seq)
        if type(p.get("recovery_of")) is not int or type(p.get("interrupted_seq")) is not int:
            return False
        boundary = p.get("trigger") == "turn_boundary"
        if "trigger" in p and not boundary:
            return False
        events = events if events is not None else self.rt.log.read(task_id=event.task_id)
        by_seq = by_seq if by_seq is not None else {e.seq: e for e in events}
        start, finish = by_seq.get(p["recovery_of"]), by_seq.get(p["interrupted_seq"])
        source = by_seq.get(start.payload.get("message_seq")) if start else None
        scope = (finish.payload.get("recovery_scope") if boundary else start.payload.get("recovery_scope")) if start and finish else None
        return bool(start and finish and source and source.seq < start.seq < finish.seq < event.seq
            and start.actor == finish.actor == HOST and start.type == "team.run_started"
            and finish.type == "team.run_finished" and finish.payload.get("status") == "interrupted"
            and start.payload.get("recovery_contract") == RECOVERY_CONTRACT
            and type(start.payload["recovery_contract"].get("version")) is int
            and source.type == "team.message_received" and source.actor.kind in {"human", "agent"}
            and source.payload.get("kind") != "recovery"
            and source.payload.get("recipient") == start.payload.get("role")
            and start.hash == p.get("recovery_hash") and finish.payload.get("recovery_of") == start.seq
            and ((finish.payload.get("interruption_kind") == "turn_boundary") == boundary)
            and all(finish.payload.get(k) == start.payload.get(k) for k in ("message_seq", "role", "run_id"))
            and isinstance(start.payload.get("run_id"), str) and start.payload["run_id"].strip()
            and _same_scope(p.get("scope"), scope)
            and type(p.get("revision")) is int and p["revision"] == scope.get("revision")
            and not any(e.seq < event.seq and e.actor == HOST and e.type == "team.message_received"
                        and e.payload.get("kind") == "recovery" and e.payload.get("recovery_of") == start.seq
                        and e.payload.get("recovery_hash") == start.hash for e in events))

    def current(self, task_id, message):
        events = self.rt.log.read(task_id=task_id)
        if not self.is_message(message, events):
            return False
        if self.budget_boundary(message, events) and self.team._pending_operations(task_id):
            return False
        scope = self.scope(task_id, events=events)
        return (scope["control_state"] == "running" and not scope["done"]
                and _same_scope(message.payload.get("scope"), scope)
                and not self.superseded(task_id, message, events=events))

    @staticmethod
    def budget_boundary(message, events):
        return (message.payload.get("trigger") == "turn_boundary"
                and any(event.seq == message.payload.get("interrupted_seq") and event.actor == HOST
                        and event.type == "team.run_finished"
                        and event.payload.get("boundary_reason") == "execution_budget" for event in events))

    def unhandled_inputs(self, task_id, events):
        return self._unhandled_customer_inputs(task_id, events) | self._unhandled_budget_supervision(task_id, events)

    def _unhandled_budget_supervision(self, task_id, events):
        candidates = [e for e in events if e.type == "team.message_received"
                      and e.payload.get("kind") == "recovery" and self.budget_boundary(e, events)]
        if not candidates:
            return set()
        starts, finishes, entered = {}, {}, set()
        for event in events:
            p = event.payload
            if (event.actor != HOST or not isinstance(p.get("run_id"), str)
                    or not p["run_id"].strip()):
                continue
            if event.type in {"team.external_run_started", "team.model_called", "team.tool_started"}:
                entered.add(p["run_id"])
            if type(p.get("message_seq")) is not int or not isinstance(p.get("role"), str):
                continue
            if event.actor == HOST and event.type == "team.run_started":
                starts.setdefault(p.get("message_seq"), []).append(event)
            elif event.actor == HOST and event.type == "team.run_finished":
                finishes[(p.get("run_id"), p.get("message_seq"), p.get("role"))] = event
        scope = self.scope(task_id, events=events)
        unhandled = set()
        for source in candidates:
            if not self.current(task_id, source):
                continue
            attempts = starts.get(source.seq, [])
            for start in attempts:
                p = start.payload
                finish = finishes.get((p.get("run_id"), p.get("message_seq"), p.get("role")))
                if (finish is None or not source.seq < start.seq < finish.seq
                        or type(p.get("message_seq")) is not int or p["message_seq"] != source.seq
                        or p.get("role") != source.payload["recipient"]
                        or not isinstance(p.get("run_id"), str) or not p["run_id"].strip()
                        or p["run_id"] in entered or finish.payload.get("status") != "interrupted"
                        or finish.payload.get("admission_not_started") is not True
                        or not _same_scope(p.get("recovery_contract"), RECOVERY_CONTRACT)
                        or not _same_scope(p.get("recovery_scope"), scope)
                        or type(p.get("revision")) is not int or p["revision"] != scope["revision"]
                        or p.get("control_seq") != scope["control_seq"]):
                    break
            else:
                if attempts:
                    unhandled.add(source.seq)
        return unhandled

    def _unhandled_customer_inputs(self, task_id, events):
        """A control transition must not consume a customer input before admission."""
        control = next((e for e in reversed(events)
                        if e.type == "task.execution_controlled" and e.actor == Actor.human()), None)
        if control is None or control.payload.get("state") != "running" or control.payload.get("action") != "resume":
            return set()
        controls = {e.seq: e for e in events if e.type == "task.execution_controlled" and e.actor == Actor.human()}
        transition = control
        while True:
            previous = controls.get(transition.payload.get("previous_seq"))
            if previous is None or previous.seq >= transition.seq:
                return set()
            if previous.payload.get("state") == "paused":
                break
            if previous.payload.get("state") != "running" or previous.payload.get("action") != "resume":
                return set()
            transition = previous
        starts, finishes, entered, latest = {}, {}, set(), {}
        for event in events:
            p = event.payload
            if event.actor == HOST and event.type == "team.run_started":
                starts.setdefault(p.get("message_seq"), []).append(event)
            elif event.actor == HOST and event.type == "team.run_finished":
                finishes[(p.get("run_id"), p.get("message_seq"), p.get("role"))] = event
            elif event.actor == HOST and event.type in {"team.external_run_started", "team.model_called", "team.tool_started"}:
                entered.add(p.get("run_id"))
            if event.type == "team.message_received" and event.actor == Actor.human():
                latest[p.get("recipient")] = event.seq
        if not starts or not latest:
            return set()
        scope = self.scope(task_id, events=events)
        if scope["control_state"] != "running" or scope["control_seq"] != control.seq:
            return set()
        members = {r.id: r for r in [self.rt.organization.leader, *self.rt.roles]}
        unhandled = set()
        for source in events:
            p = source.payload
            if (source.type != "team.message_received" or source.actor != Actor.human()
                    or p.get("kind") != "message" or latest.get(p.get("recipient")) != source.seq
                    or p.get("recipient") not in members or source.seq <= scope["revision"]):
                continue
            attempts = starts.get(source.seq, [])
            if not attempts or attempts[-1].payload.get("control_seq") == scope["control_seq"]:
                continue
            if isinstance(p.get("questionnaire"), dict) and p.get("revision") != scope["revision"]:
                continue
            role = members[p["recipient"]]
            # Older built-in ACP runs durably begin BEFORE even creating a client.
            # Absence of a model receipt is not equivalent proof for old native APIs.
            from .external_agent import QoderAcpDriver
            legacy_external = type(self.rt.drivers.get(role.driver)) is QoderAcpDriver
            eligible = True
            for start in attempts:
                sp = start.payload
                key = (sp.get("run_id"), sp.get("message_seq"), sp.get("role"))
                finish = finishes.get(key)
                expected = sp.get("recovery_scope")
                contract = sp.get("recovery_contract")
                if (finish is None or not source.seq < start.seq < finish.seq
                        or type(sp.get("message_seq")) is not int or sp["message_seq"] != source.seq
                        or type(sp.get("revision")) is not int
                        or sp.get("role") != p["recipient"] or not isinstance(sp.get("run_id"), str)
                        or not sp["run_id"].strip() or sp["run_id"] in entered
                        or finish.payload.get("status") != "interrupted"
                        or contract != RECOVERY_CONTRACT or type(contract.get("version")) is not int
                        or type(expected) is not dict or expected.get("control_seq") != sp.get("control_seq")
                        or expected.get("revision") != sp.get("revision")
                        or not (finish.payload.get("admission_not_started") is True
                                or "admission_not_started" not in finish.payload and legacy_external)):
                    eligible = False
                    break
                expected = {**expected, "control_seq": scope["control_seq"], "control_state": scope["control_state"]}
                if not _same_scope(expected, scope):
                    eligible = False
                    break
            if eligible:
                unhandled.add(source.seq)
        return unhandled

    def superseded(self, task_id, message, *, events=None):
        events = events if events is not None else self.rt.log.read(task_id=task_id)
        if message.payload.get("trigger") == "control_resume":
            return any(e.actor == Actor.human() and e.type == "team.message_received"
                       and e.seq > message.payload["resume_seq"] for e in events)
        original = message.payload.get("recovery_of")
        return (message.payload.get("trigger") == "turn_boundary"
                and type(original) is int
                and any(e.actor == Actor.human() and e.type == "team.message_received"
                        and e.seq > original for e in events))

    def _resume_message(self, message, events=None, *, by_seq=None):
        events = events if events is not None else self.rt.log.read(task_id=message.task_id)
        by_seq = by_seq if by_seq is not None else {event.seq: event for event in events}
        p = message.payload
        if type(p.get("resume_seq")) is not int or type(p.get("scope")) is not dict:
            return False
        control, scope = by_seq.get(p["resume_seq"]), p["scope"]
        if (control is None or control.actor != Actor.human() or control.type != "task.execution_controlled"
                or control.payload.get("action") != "resume" or control.payload.get("state") != "running"
                or not control.seq < message.seq or p.get("resume_hash") != control.hash):
            return False
        previous = paused_predecessor(control, events)
        binding = control.payload.get("scope")
        if (previous is None or previous.actor != Actor.human() or previous.type != "task.execution_controlled"
                or previous.payload.get("state") != "paused" or not previous.seq < control.seq
                or type(binding) is not dict or type(binding.get("workspace", {})) is not dict):
            return False
        objectives = [event for event in events if event.actor == HOST and event.seq < message.seq
                      and event.type in {"team.objective_updated", "team.objective_completed"}]
        before_resume = [event for event in objectives if event.seq < control.seq]
        workspace = binding.get("workspace", {})
        return (type(p.get("revision")) is int and type(scope.get("revision")) is int
            and p["revision"] == scope["revision"]
            and scope.get("revision") == (objectives[-1].seq if objectives else 0)
            and scope["revision"] == (before_resume[-1].seq if before_resume else 0)
            and not (objectives and objectives[-1].type == "team.objective_completed")
            and type(scope.get("control_seq")) is int and scope["control_seq"] == control.seq
            and scope.get("control_state") == "running" and scope.get("done") is False
            and scope.get("execution_fingerprint") == binding.get("organization")
            and scope.get("rule_fingerprint") == binding.get("rules")
            and scope.get("workspace_revision") == workspace.get("revision")
            and scope.get("workspace_fingerprint") == workspace.get("fingerprint")
            and not any(event.seq < message.seq and event.actor == HOST and event.type == "team.message_received"
                        and event.payload.get("kind") == "recovery" and event.payload.get("trigger") == "control_resume"
                        and event.payload.get("resume_seq") == control.seq for event in events))

    def resume(self, task_id, resume_seq):
        """Translate an explicit resume into fresh supervision when the inbox is empty."""
        if type(resume_seq) is not int or not self.team.enabled(task_id):
            return None
        with activity_owner(self.rt.log.path, task_id, "simple-team") as owned:
            if not owned:
                return None
            with self.rt.log.atomic():
                events = self.rt.log.read(task_id=task_id)
                control = next((event for event in events if event.seq == resume_seq), None)
                if (control is None or control.actor != Actor.human() or control.type != "task.execution_controlled"
                        or control.payload.get("action") != "resume" or control.payload.get("state") != "running"):
                    return None
                previous = paused_predecessor(control, events)
                if (previous is None or previous.actor != Actor.human() or previous.type != "task.execution_controlled"
                        or previous.payload.get("state") != "paused"):
                    return None
                existing = next((event for event in events if event.actor == HOST and event.type == "team.message_received"
                    and event.payload.get("kind") == "recovery" and event.payload.get("trigger") == "control_resume"
                    and event.payload.get("resume_seq") == resume_seq), None)
                if existing is not None:
                    return existing if self.is_message(existing, events) else None
                scope = self.scope(task_id, events=events)
                previous_objective = next((event.seq for event in reversed(events)
                    if event.seq < resume_seq and event.actor == HOST
                    and event.type in {"team.objective_updated", "team.objective_completed"}), 0)
                if (scope["control_state"] != "running" or scope["control_seq"] != resume_seq or scope["done"]
                        or scope["revision"] != previous_objective
                        or not any(event.seq < resume_seq and event.actor == HOST
                                   and event.type == "team.run_started" for event in events)
                        or control.payload.get("scope") != self.rt.work.scope(task_id)
                        or self.rt.conversation_list.is_removed(task_id) or self.team._pending_operations(task_id)
                        or self.team.view(task_id)["pending_count"]
                        or any(row["status"] == "pending" for row in self.rt.questionnaires.view(task_id))
                        or any(event.seq > resume_seq and event.actor == Actor.human()
                               and event.type == "team.message_received" for event in events)):
                    return None
                return self.rt.log.emit(type="team.message_received", task_id=task_id, actor=HOST,
                    payload={"kind": "recovery", "trigger": "control_resume", "recipient": self.rt.organization.leader.id,
                        "resume_seq": control.seq, "resume_hash": control.hash, "revision": scope["revision"], "scope": scope,
                        "text": "顾客已恢复执行。请核对当前目标、已有文件、成果和操作回执，继续未交付的工作并跟进必要交接、复核与最终交付；沿用原目标，不重做已经确认的操作。"})

    def run_scope(self, task_id, start, *, observed=None, events=None):
        events = events if events is not None else self.rt.log.read(task_id=task_id)
        expected = observed if observed is not None else start.payload.get("recovery_scope")
        if type(expected) is not dict or type(expected.get("revision")) is not int:
            return None
        expected = dict(expected)
        cutoff = max(start.seq, expected["revision"])
        for event in events:
            if (event.seq <= cutoff or event.actor != HOST
                    or event.type not in {"team.objective_updated", "team.objective_completed"}):
                continue
            if event.payload.get("run_id") != start.payload.get("run_id"):
                return None
            expected.update(revision=event.seq, done=event.type == "team.objective_completed")
        scope = self.scope(task_id, events=events)
        return scope if _same_scope(scope, expected) else None

    def boundary_scope(self, task_id, source, start, observed):
        """Only a durable root input may hand off one bounded supervision turn."""
        contract = start.payload.get("recovery_contract")
        if (source.actor.kind not in {"human", "agent"} or source.payload.get("kind") == "recovery"
                or source.payload.get("recipient") != start.payload.get("role")
                or contract != RECOVERY_CONTRACT or type(contract.get("version")) is not int):
            return None
        events = self.rt.log.read(task_id=task_id)
        if any(e.actor == Actor.human() and e.type == "team.message_received" and e.seq > start.seq for e in events):
            return None
        scope = self.run_scope(task_id, start, observed=observed, events=events) if observed is not None else None
        return scope if scope and scope["control_state"] == "running" and not scope["done"] else None

    def _notify(self, task_id, start, finish, scope, *, trigger=None):
        role = self.team._role(start.payload["role"])
        cause = "本轮到达分段检查点" if trigger == "turn_boundary" else "上次执行进程中断"
        return self.rt.log.emit(type="team.message_received", task_id=task_id, actor=HOST, payload={
            "kind": "recovery", "recipient": self.rt.organization.leader.id,
            "revision": scope["revision"], "scope": scope,
            "recovery_of": start.seq, "recovery_hash": start.hash, "interrupted_seq": finish.seq,
            **({"trigger": trigger} if trigger is not None else {}),
            "text": (f"{role.name}处理消息 #{start.payload['message_seq']} 时{cause}。"
                     "已有成果和操作记录已保留，请店长核对当前目标、成果和回执后安排剩余工作；"
                     "这是新的监督输入，不是重放旧请求、旧操作或宣布工作完成。")})

    def retire(self, task_id):
        """Caller owns simple-team; closing and notification commit together."""
        from .team_reply import committed_reply

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
                answer = committed_reply(self.team, start, events)
                if answer is not None:
                    self.rt.log.emit(type="team.run_finished", task_id=task_id, actor=HOST,
                        payload={**{key: p.get(key) for key in ("message_seq", "role", "run_id", "revision")},
                                 "status": "completed", "recovery_of": start.seq,
                                 "recovered_customer_reply_seq": answer.seq,
                                 "reason": "顾客答复及工具回执已原子保存，未重复回答；进程及工具影响仍按各自记录核对。"})
                    continue
                eligible = (source is not None and source.seq < start.seq and source.actor.kind in {"human", "agent"}
                    and source.payload.get("kind") != "recovery"
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
                    self._notify(task_id, start, terminal, scope)

    def prepare(self, task_id):
        if self.rt.log.db.in_transaction:
            raise WorkConflict("恢复准备不能处于外层事务中；通知必须先持久化，再启动模型。")
        with activity_owner(self.rt.log.path, task_id, "simple-team") as owned:
            if not owned or not self.team.enabled(task_id):
                return []
            self.retire(task_id)
            events = self.rt.log.read(task_id=task_id)
            started = {e.payload.get("message_seq") for e in events if e.actor == HOST and e.type == "team.run_started"}
            unhandled = self.unhandled_inputs(task_id, events)
            return [e for e in events if e.seq in unhandled
                    or e.seq not in started and self.is_message(e, events) and self.current(task_id, e)]

    def assignments(self, task_id, *, events=None):
        from .team import _reported_messages

        events = events if events is not None else self.rt.log.read(task_id=task_id)
        messages = self.team._messages(task_id, events=events)
        reported = _reported_messages(messages)
        superseded = self.team._inactive_messages(task_id, events=events)
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
        delegated = set()
        for e in messages:
            parent = by_seq.get(e.payload.get("parent_assignment_seq"))
            if (parent is not None and parent.seq < e.seq and e.actor.kind == "agent"
                    and e.payload.get("kind") in {"assignment", "message"}
                    and parent.payload.get("kind") in {"assignment", "message"}
                    and parent.payload.get("recipient") == e.actor.id
                    and parent.payload.get("revision") == e.payload.get("revision") == revision):
                delegated.add(parent.seq)
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
                    or source.payload.get("revision") != revision or source.seq in reported
                    or source.seq in superseded):
                continue
            latest = continuations.get(source.seq, source)
            run = runs.get(latest.seq, {})
            rows.append({"seq": source.seq, "sender": source.actor.id, "recipient": source.payload["recipient"],
                         "text": source.payload["text"], "latest_message_seq": latest.seq,
                         "requires_return": source.seq in interrupted_roots,
                         "has_delegated_work": source.seq in delegated,
                         "status": run.get("status", "queued"), "reason": run.get("reason", ""),
                         "run_id": run.get("run_id")})
        return rows

    def checkpoint(self, task_id):
        return self._capture(task_id)[0]

    def _capture(self, task_id, *, tool_start=None, role_id=None, pending_view=None):
        # Keep all database projections in one short read snapshot, including
        # shared rules/workspaces, without taking over a caller's transaction.
        db = self.rt.log.db
        owned = not db.in_transaction
        if owned:
            db.execute("BEGIN")
        try:
            events = self.rt.log.read(task_id=task_id)
            assignments = self.assignments(task_id, events=events)
            from .team_context import _preview

            withdrawals = self.team.work_control.view(task_id, events=events)
            objectives = [e for e in events if e.actor == HOST and e.type == "team.objective_updated"]
            objective = objectives[-1].payload["goal"] if objectives else self.rt.task(task_id).goal
            pending = (display_pending(self.team, task_id, tool_start=tool_start, role_id=role_id)
                       if pending_view is None else pending_view)
            checkpoint = {"as_of_seq": events[-1].seq if events else 0,
                "as_of_hash": events[-1].hash if events else None,
                "scope": self.scope(task_id, events=events), "objective": objective,
                "open_work": [{k: v for k, v in row.items() if k != "text"} for row in assignments[-20:]],
                "open_work_count": len(assignments), "open_work_truncated": len(assignments) > 20,
                "withdrawals": [_preview(row, "reason", 512) for row in withdrawals[-5:]],
                "withdrawals_truncated": len(withdrawals) > 5,
                **pending_counts(pending),
                "note": "从持久事件重建的当前事实，不是恢复旧模型历史、重放操作或宣告验收的授权。"}
            return checkpoint, assignments
        finally:
            if owned:
                db.rollback()

    def read(self, task_id, arguments, *, tool_start=None, role_id=None):
        if "review_seq" in arguments:
            seq = arguments["review_seq"]
            if set(arguments) != {"review_seq"} or type(seq) is not int or seq < 1:
                raise ValueError("复核原文读取只接受正整数review_seq，不能与分页混用。")
            row = self.rt.team_reviews.read(task_id, seq)
            return {"status": "ok", "review": row,
                    **({"scope_reason": "objective_changed",
                        "scope_note": "stale表示该记录所属目标与当前目标不同，不表示它被同一复核的下一轮修订替代；历史反馈不证明当前目标或当前文件已通过。"}
                       if row["status"] == "stale" else
                       {"scope_reason": "review_superseded",
                        "scope_note": "这是已被同链新提交替代的历史记录；historical_status仅描述当时结论，不能作为当前成果的批准。请使用current_request_seq读取当前提交。"}
                       if row["status"] == "superseded" else {})}
        after, limit = arguments.get("after_seq", 0), arguments.get("limit", 20)
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("检查点读取需要非负序号和1至50条分页大小。")
        checkpoint, assignments = self._capture(task_id, tool_start=tool_start, role_id=role_id)
        rows = [r for r in assignments if r["seq"] > after]
        page = rows[:limit]
        return {"status": "ok", "checkpoint": checkpoint, "assignments": page,
                "has_more": len(rows) > limit, "next_seq": page[-1]["seq"] if page else after}

    def continuation(self, task_id, role, recipient, seq):
        return self.continuation_source(task_id, role, seq, recipient=recipient)

    def continuation_source(self, task_id, role, seq, *, recipient=_UNSPECIFIED_RECIPIENT):
        """Resolve an explicit outgoing job, never infer one from conversation text."""
        if type(seq) is not int or seq < 1:
            raise ValueError("接续需要本群原委托的有效序号。")
        source = next((r for r in self.assignments(task_id) if r["seq"] == seq), None)
        if (source is None or source["sender"] != role.id
                or recipient is not _UNSPECIFIED_RECIPIENT and source["recipient"] != recipient):
            raise WorkConflict("只能由原发件人接续当前未交还的原委托，不能重开旧目标或代替其他伙伴派活。")
        if source["status"] in {"queued", "running"} or any(
                r["role"] == source["recipient"] and r["status"] == "running" for r in self.team.view(task_id)["runs"]):
            raise WorkConflict("原接收者已有待处理或在途工作，不重复投递接续。")
        return source

    def reconcile_publication(self, task_id, started_seq, *, reason, invocation_stopped=False):
        return self._reconcile_tool(task_id, started_seq, reason=reason, invocation_stopped=invocation_stopped,
                                    names={"publish_artifact", "publish_workspace_file", "publish_workspace_archive"})

    def reconcile_browser_check(self, task_id, started_seq, *, reason, invocation_stopped=False):
        return self._reconcile_tool(task_id, started_seq, reason=reason, invocation_stopped=invocation_stopped,
                                    names={"browser_check"})

    def reconcile_mcp_call(self, task_id, started_seq, *, reason, invocation_stopped=False):
        return self._reconcile_tool(task_id, started_seq, reason=reason, invocation_stopped=invocation_stopped,
                                    names={"call_mcp_tool"})

    def _reconcile_tool(self, task_id, started_seq, *, reason, invocation_stopped, names):
        """Trusted local operator only; models receive no self-unlock tool."""
        if (type(started_seq) is not int or started_seq < 1 or invocation_stopped is not True
                or not isinstance(reason, str) or not reason.strip() or len(reason) > 4000):
            raise ValueError("需要工具调用序号、确认原执行已停止，并说明实际核对的成果、进程与文件影响。")
        with activity_owner(self.rt.log.path, task_id, "simple-team") as owned:
            if not owned:
                raise WorkConflict("原生团队仍有执行者，不能关闭工具操作的未知状态。")
            with self.rt.log.atomic():
                pending = next((r for r in self.team._pending_operations(task_id) if r["started_seq"] == started_seq
                                and r["name"] in names and r["run_ended"]), None)
                if pending is None:
                    raise WorkConflict("该工具调用不是本群已结束运行中待核对的操作。")
                start = next(e for e in self.rt.log.read(task_id=task_id) if e.seq == started_seq)
                return self.rt.log.emit(type="team.tool_reconciled", task_id=task_id, actor=Actor.human(), payload={
                    "started_seq": start.seq, "started_hash": start.hash, "call_id": start.payload["call_id"],
                    **{k: start.payload[k] for k in ("run_id", "role", "name")},
                    "reason": reason.strip(), "invocation_stopped": True, "outcome": "effects_reviewed_no_replay"})
