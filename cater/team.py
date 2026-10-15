"""Message-driven role execution. The manager delegates; the host delivers and records."""

from __future__ import annotations

import io
import json
import uuid
import zipfile
from dataclasses import asdict

from .artifacts import normalize_deliverables
from .activity_owner import activity_owner
from .events import Actor
from .room_dialogue import addressed_role
from .usage import valid_token_count
from .work import WorkConflict


HOST = Actor.system("team")
MAX_ACTOR_TURNS = 32


def _text(value, label="内容", limit=32000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{label}为空或过长。")
    return value.strip()


def _tool(name, description, properties, required):
    return {"name": name, "description": description, "input_schema": {
        "type": "object", "properties": properties, "required": required, "additionalProperties": False}}


class TeamService:
    def __init__(self, runtime):
        self.rt = runtime

    def available(self):
        leader = self.rt.organization.leader
        return leader.driver is not None and callable(getattr(self.rt.drivers[leader.driver], "tool_turn", None))

    def _role(self, role_id):
        if role_id == self.rt.organization.leader.id:
            return self.rt.organization.leader
        return self.rt._role_by_id(role_id)

    def enabled(self, task_id):
        return any(e.actor == HOST for e in self.rt.log.iter_events(task_id=task_id, event_types=("team.started",)))

    def enable(self, task_id):
        with self.rt.log.atomic():
            if self.rt.task(task_id) is None:
                raise KeyError(task_id)
            if not self.available():
                raise ValueError("店长的模型通道需要支持原生工具调用。")
            if not self.enabled(task_id):
                self.rt.log.emit(type="team.started", actor=HOST, task_id=task_id, payload={"version": 1})

    def receive(self, task_id, text, *, request_id=None):
        _text(text)
        if request_id is not None and (not isinstance(request_id, str) or not 8 <= len(request_id) <= 128):
            raise ValueError("消息请求标识无效。")
        with self.rt.log.atomic():
            if not self.enabled(task_id):
                raise WorkConflict("本群尚未启用消息协作。")
            if request_id is not None:
                for e in self.rt.log.iter_events(task_id=task_id, event_types=("team.message_received",)):
                    if e.actor == Actor.human() and e.payload.get("request_id") == request_id:
                        if e.payload["text"] != text:
                            raise WorkConflict("同一发送请求不能更换内容。")
                        return e
            members = [self.rt.organization.leader, *self.rt.roles]
            recipient = addressed_role(text, [asdict(r) for r in members]) or members[0].id
            return self.rt.log.emit(type="team.message_received", actor=Actor.human(), task_id=task_id,
                payload={"text": text, "recipient": recipient, "kind": "message", "request_id": request_id})

    def _messages(self, task_id):
        return [e for e in self.rt.log.iter_events(task_id=task_id, event_types=("team.message_received",))
                if e.actor == Actor.human() or e.actor.id in {r.id for r in [self.rt.organization.leader, *self.rt.roles]}]

    def view(self, task_id):
        events = self.rt.log.read(task_id=task_id)
        starts, finishes = {}, {}
        objective = self.rt.task(task_id).goal
        revision, status, checkpoint = 0, "running", "等待店长理解消息。"
        assignments = []
        for e in events:
            if e.type == "team.message_received" and e.payload.get("kind") == "assignment":
                assignments.append({"seq": e.seq, "sender": e.actor.id, **e.payload})
            if e.actor != HOST:
                continue
            if e.type == "team.objective_updated":
                objective, revision, status = e.payload["goal"], e.seq, "running"
                checkpoint = "当前目标：" + objective
            elif e.type == "team.objective_completed":
                status, checkpoint, revision = "done", e.payload["summary"], e.seq
            elif e.type == "team.run_started":
                starts[e.payload["message_seq"]] = e
            elif e.type == "team.run_finished":
                finishes[e.payload["message_seq"]] = e
                if e.payload["status"] in {"failed", "interrupted"}:
                    checkpoint = e.payload.get("reason", "本轮未完成，已有记录保留。")
        messages = self._messages(task_id)
        pending = [e for e in messages if e.seq not in starts]
        runs = [{"message_seq": seq, "role": e.payload["role"], "revision": e.payload.get("revision", 0),
                 "reason": finishes[seq].payload.get("reason", "") if seq in finishes else "",
                 "status": finishes[seq].payload["status"] if seq in finishes else "running"}
                for seq, e in starts.items()]
        if status != "done":
            active = [self._role(r["role"]).name for r in runs if r["status"] == "running"]
            if active:
                checkpoint = "、".join(active) + "正在处理消息和工作。"
            elif pending:
                checkpoint = f"已收到 {len(pending)} 条待处理消息。"
            elif runs and runs[-1]["status"] not in {"failed", "interrupted"}:
                checkpoint = "本轮消息已处理；是否交付以店长的成果说明为准。"
        return {"enabled": self.enabled(task_id), "objective": objective, "revision": revision, "status": status,
                "pending_count": len(pending), "runs": runs, "assignments": assignments, "checkpoint": checkpoint}

    def _send(self, task_id, sender, recipient, text, kind, *, reply_to=None, parent_assignment_seq=None):
        self._role(recipient)
        if recipient == sender.id:
            raise ValueError("请向另一位伙伴投递消息。")
        return self.rt.log.emit(type="team.message_received", task_id=task_id,
            actor=Actor.agent(sender.id, sender.role), payload={"recipient": recipient, "text": _text(text),
                "kind": kind, "reply_to": reply_to, "revision": self.view(task_id)["revision"],
                "parent_assignment_seq": parent_assignment_seq})

    def _claim(self, task_id):
        with self.rt.log.atomic():
            events = self.rt.log.read(task_id=task_id)
            starts = {e.payload["message_seq"]: e for e in events if e.type == "team.run_started" and e.actor == HOST}
            finished = {e.payload["message_seq"] for e in events if e.type == "team.run_finished" and e.actor == HOST}
            busy = {e.payload["role"] for seq, e in starts.items() if seq not in finished}
            running = self.rt.execution_control.view(task_id)["state"] == "running"
            pending = [e for e in self._messages(task_id) if e.seq not in starts
                       and e.payload["recipient"] not in busy
                       and (running or e.payload["kind"] != "assignment")]
            if not pending:
                return None
            # A new customer message is read before the next delegated job.
            message = min(pending, key=lambda e: (e.actor.kind != "human", e.seq))
            role = self._role(message.payload["recipient"])
            started = self.rt.log.emit(type="team.run_started", task_id=task_id, actor=HOST,
                payload={"message_seq": message.seq, "role": role.id, "run_id": uuid.uuid4().hex,
                         "revision": self.view(task_id)["revision"],
                         "control_seq": self.rt.execution_control.view(task_id)["seq"]})
            return message, role, started

    def _schemas(self, role, workspace):
        string = {"type": "string"}
        members = [self.rt.organization.leader, *self.rt.roles]
        recipient = {"type": "string", "enum": [r.id for r in members if r.id != role.id]}
        tools = [
            _tool("wait_for_reply", "已经安排好工作或正在等信息时结束本轮，安静等待下一条消息，不必重复发言。", {}, []),
            _tool("delegate", "向伙伴交代一份工作或复核委托。对方执行后会自动回报你。用清晰文本说明目标、背景、输入和交付期待。",
                  {"recipient": recipient, "instruction": string}, ["recipient", "instruction"]),
            _tool("send_message", "向伙伴提问、补充信息或沟通。是真实消息投递，不强制变成工作步骤。",
                  {"recipient": recipient, "text": string}, ["recipient", "text"]),
            _tool("read_artifact", "读取本群已发布成果的完整正文。", {"artifact_seq": {"type": "integer"}}, ["artifact_seq"]),
            _tool("read_receipts", "读取本群真实工具执行回执（含失败和输出），自主检查伙伴的工作，不凭聊天自述判定测试通过。",
                  {"run_id": string, "limit": {"type": "integer", "minimum": 1, "maximum": 50}}, []),
            _tool("publish_artifact", "保存完整文本成果，供顾客打开下载；如已在工作目录写文件，可用workspace_read读取真实内容后发布。",
                  {"filename": string, "content": string, "summary": string,
                   "format": {"type": "string", "enum": ["code", "markdown", "text"]}},
                  ["filename", "content", "summary", "format"]),
        ]
        if role.id == self.rt.organization.leader.id:
            tools += [
                _tool("set_objective", "采用顾客当前目标或更换实现方向。记录成功后再安排相应工作；普通追问不用更换目标。",
                      {"goal": string}, ["goal"]),
                _tool("finish_work", "店长确认当前工作可以交付，说明实际验证与尚未验证的范围；不等同于宿主证明正确。",
                      {"summary": string, "verification": string}, ["summary", "verification"]),
            ]
        if workspace is not None:
            tools += workspace.schemas()
            tools.append(_tool("publish_workspace_file", "直接读取工作目录中已写好的真实UTF-8文件并发布，不必重复复述整份源码。",
                {"path": string, "summary": string, "format": {"type": "string", "enum": ["code", "markdown", "text"]}},
                ["path", "summary", "format"]))
        return tools

    def _waiting_assignment(self, task_id, message, role):
        if message.payload["kind"] == "assignment":
            return message
        if message.payload["kind"] != "report":
            return None
        messages = {e.seq: e for e in self._messages(task_id)}
        child = messages.get(message.payload.get("reply_to"))
        parent = messages.get(child.payload.get("parent_assignment_seq")) if child else None
        if (child and parent and child.actor.id == role.id and parent.payload.get("kind") == "assignment"
                and parent.payload.get("recipient") == role.id
                and parent.payload.get("revision") == self.view(task_id)["revision"]
                and not any(e.payload.get("kind") == "report" and e.actor.id == role.id
                            and e.payload.get("reply_to") == parent.seq for e in messages.values())):
            return parent
        return None

    def _context(self, task_id, message, role):
        events = self.rt.log.read(task_id=task_id)
        history = []
        for e in events:
            if e.type == "team.message_received" or e.type in {"msg.said", "msg.challenge"}:
                text = e.payload.get("text")
                if text:
                    history.append({"seq": e.seq, "sender": e.actor.id, "text": text,
                        "recipient": e.payload.get("recipient"), "kind": e.payload.get("kind")})
            elif e.type in {"conversation.received", "conversation.finished"}:
                text = e.payload.get("instruction") or e.payload.get("reply")
                if text:
                    history.append({"seq": e.seq, "sender": e.actor.id, "text": text})
        task = self.rt.task(task_id)
        assignment = self._waiting_assignment(task_id, message, role)
        return {"you": asdict(role), "members": [asdict(r) for r in [self.rt.organization.leader, *self.rt.roles]],
            "active_assignment": {"seq": assignment.seq, "sender": assignment.actor.id, **assignment.payload} if assignment else None,
            "current_work": self.view(task_id), "message": {"seq": message.seq, "sender": message.actor.id, **message.payload},
            "conversation": history[-60:], "artifacts": [{k: a.get(k) for k in
                ("seq", "filename", "summary", "content_sha256", "local_path")} for a in task.deliverables],
            "workspace": self.rt.workspaces.view(task_id), "rules": self.rt.rulebook.snapshot(task_id)["rules"]}

    def _current(self, task_id, started, role, revision):
        control = self.rt.execution_control.view(task_id)
        if control["seq"] != started.payload["control_seq"]:
            raise WorkConflict("执行已暂停或控制发生变化；已有成果与回执保留。")
        if any(e.actor == Actor.human() and e.seq > started.payload["message_seq"]
               and e.payload["recipient"] == role.id for e in self._messages(task_id)):
            raise WorkConflict("收到顾客的新消息，先理解新的上下文；已有操作未重放。")
        if role.id != self.rt.organization.leader.id:
            view = self.view(task_id)
            if view["revision"] != revision:
                raise WorkConflict("店长已调整或结束当前工作，旧委托不再启动新的操作。")

    def _execute(self, task_id, role, name, arguments, workspace, binding, tool_start, assignment):
        if self.view(task_id)["status"] == "done" and name in {
                "delegate", "publish_artifact", "publish_workspace_file", "workspace_write", "workspace_run"}:
            raise WorkConflict("当前工作已交付；如需继续修改，请先由店长采用新的工作目标。")
        if name == "wait_for_reply":
            return {"status": "ok", "waiting": True}
        if name == "delegate":
            e = self._send(task_id, role, arguments["recipient"], arguments["instruction"], "assignment",
                           parent_assignment_seq=assignment.seq if assignment else None)
            return {"status": "ok", "message_seq": e.seq, "recipient": e.payload["recipient"]}
        if name == "send_message":
            e = self._send(task_id, role, arguments["recipient"], arguments["text"], "message")
            return {"status": "ok", "message_seq": e.seq}
        if name == "set_objective":
            goal = _text(arguments["goal"], "目标", 12000)
            current_work = self.view(task_id)
            if current_work["objective"] != goal or current_work["status"] == "done":
                self.rt.log.emit(type="team.objective_updated", task_id=task_id, actor=HOST, payload={"goal": goal})
            return {"status": "ok", "goal": goal}
        if name == "finish_work":
            payload = {"summary": _text(arguments["summary"]), "verification": _text(arguments["verification"])}
            self.rt.log.emit(type="team.objective_completed", task_id=task_id, actor=HOST, payload=payload)
            return {"status": "ok", "note": "已记录店长交付结论，实际验证范围按原文保留。"}
        if name == "read_artifact":
            artifact = next((a for a in self.rt.task(task_id).deliverables if a["seq"] == arguments["artifact_seq"]), None)
            if artifact is None:
                raise ValueError("找不到本群成果。")
            return {"status": "ok", **artifact}
        if name == "read_receipts":
            limit = arguments.get("limit", 20)
            if type(limit) is not int or not 1 <= limit <= 50:
                raise ValueError("回执数量应为1到50。")
            receipts = [{"seq": e.seq, **e.payload} for e in self.rt.log.iter_events(
                task_id=task_id, event_types=("team.tool_finished",)) if e.actor == HOST
                and e.payload.get("name") != "read_receipts"
                and (not arguments.get("run_id") or e.payload.get("run_id") == arguments["run_id"])]
            return {"status": "ok", "receipts": receipts[-limit:], "has_more": len(receipts) > limit}
        if name == "publish_workspace_file":
            current = self.rt.workspaces.view(task_id)
            if workspace is None or not current["available"] or current["fingerprint"] != binding["fingerprint"] or current["revision"] != binding["revision"]:
                raise WorkConflict("当前工作目录或读取权限已变化。")
            source = workspace.execute("workspace_read", {"path": arguments["path"], "limit": 65536})
            if source.get("status") != "ok" or source.get("truncated"):
                raise ValueError("没有读到完整UTF-8文件，不能发布部分源码。")
            filename = source["path"].rsplit("/", 1)[-1]
            return self._execute(task_id, role, "publish_artifact", {"filename": filename, "content": source["content"],
                "summary": arguments["summary"], "format": arguments["format"]}, workspace, binding, tool_start, assignment)
        if name == "publish_artifact":
            payload, = normalize_deliverables([{**arguments, "title": arguments["filename"]}])
            if not payload.get("content_sha256") or "filename" not in payload:
                raise ValueError("需要有效文件名和完整成果正文。")
            e = self.rt.log.emit(type="deliverable.published", actor=Actor.agent(role.id, role.role), task_id=task_id,
                                 payload={**payload, "source": "team", "phase": "team"})
            result = {"status": "ok", "artifact_seq": e.seq, "filename": payload["filename"]}
            if self.rt._artifact_root is not None:
                from .artifact_store import materialize_team_artifact
                path = materialize_team_artifact(self.rt._artifact_root, task_id, {**payload, "seq": e.seq})
                self.rt.log.emit(type="team.artifact_saved", task_id=task_id, actor=HOST,
                    payload={"artifact_seq": e.seq, "path": path, "content_sha256": payload["content_sha256"]})
                result["local_path"] = path
            return result
        if workspace is not None:
            current = self.rt.workspaces.view(task_id)
            if not current["available"] or current["revision"] != binding["revision"] or current["fingerprint"] != binding["fingerprint"]:
                raise WorkConflict("工作目录或权限已变化，未执行旧工具请求。")
            with self.rt.workspaces._owner(binding["root"]) as owned:
                pending = [e for e in self.rt.workspaces.pending(root=binding["root"]) if e.seq != tool_start.seq]
                if not owned or pending and name in {"workspace_write", "workspace_run"}:
                    raise WorkConflict("工作目录有未结束或待核对操作，不能重复执行。")
                return workspace.execute(name, arguments)
        raise ValueError("工具尚未接入。")

    def _run_message(self, task_id, message, role, started):
        status, reason, final = "completed", "", ""
        revision = self.view(task_id)["revision"]
        assignment = self._waiting_assignment(task_id, message, role)
        try:
            if message.payload["kind"] == "assignment" and (message.payload.get("revision") != revision
                    or self.view(task_id)["status"] == "done"):
                raise WorkConflict("这份委托已被新的目标替代，不再执行旧方案。")
            driver = self.rt.drivers[role.driver]
            if not callable(getattr(driver, "tool_turn", None)):
                raise ValueError("这位伙伴的模型通道尚不支持工具执行。")
            binding = self.rt.workspaces.view(task_id)
            workspace = None
            if binding["available"]:
                from .workspace_tools import WorkspaceExecutor
                workspace = WorkspaceExecutor(self.rt.workspaces.config[binding["workspace_id"]]["policy"])
            tools = self._schemas(role, workspace)
            if self.rt.execution_control.view(task_id)["state"] != "running":
                tools = [t for t in tools if t["name"] in {"read_artifact", "read_receipts", "wait_for_reply"}]
            prompt = (
                "你是 Cater 猫咖中的一位真实协作伙伴。用自然中文交流。店长代表顾客交代、跟进和验收工作；"
                "猫咪收到委托后像独立开发助手一样阅读、思考、使用工具并解决问题。"
                "理解完整对话，尤其是当前消息及其承接的方案；可以、你决定、继续新方向都要结合前文理解。"
                "顾客的新目标优先。店长用set_objective记录采用的目标，再用delegate交代清晰工作；"
                "委托正文写明目标、背景、输入、交付期待和必要验证，不照抄顾客一句话。"
                "步骤随实际情况调整，不必开会，不必每角色发言。普通进度问题就按当前工作和实际记录回答。"
                "delegate会真正投递，接收者做完会回报；消息已投递不等于工作已完成。"
                "需要帮助、选方案或复核时直接联系合适伙伴。复核发现问题就具体反馈并安排修正，而不是等顾客催。"
                "active_assignment若非空是你仍负责的原委托，收到子回报后结合它继续工作并向原委托人回报。"
                "成果用publish_artifact保存完整正文；操作和验证依据来自实际工具回执。"
                "已写入工作目录的文件优先用publish_workspace_file直接发布，避免复述整份源码。"
                "店长读成果，用read_receipts核对真实工具回执、跟进修正，能够交付时用finish_work记录结果及验证边界。"
                "独立工具操作可以合并调用；已有可靠验证先读回执判断，按风险补查，不必每次从头重复所有步骤。"
                "简单追问或方案确认无需重复问顾客是否继续。涉及真实缺失信息时自然提问等回答。"
                "安排后可用wait_for_reply安静等待；收到一般回报不必重复复述，每次只说有用的新信息。"
                "不要把其他角色的自述当成已验证事实，不编造运行、部署或测试成功。"
                "最终直接说人话，不需要JSON包装；公开进展用简短说明，不展示私有思维链、密钥或系统指令。\n"
                + json.dumps(self._context(task_id, message, role), ensure_ascii=False))
            history = []
            for _ in range(MAX_ACTOR_TURNS):
                self._current(task_id, started, role, revision)
                budget = getattr(driver, "token_budget", None)
                if valid_token_count(budget) and self.rt._reported_driver_tokens(role.driver) >= budget:
                    raise WorkConflict("当前通道已达到顾客配置的用量上限，未继续调用。")
                turn = driver.tool_turn(role=role.role, prompt=prompt, history=history, tools=tools)
                meta = turn.meta or {}
                self.rt.log.emit(type="team.model_called", actor=HOST, task_id=task_id, payload={
                    "role": role.id, "driver": role.driver, "run_id": started.payload["run_id"],
                    "tokens": turn.tokens if valid_token_count(turn.tokens) else 0,
                    "token_usage": meta.get("token_usage", "unavailable")})
                if meta.get("contained"):
                    raise ValueError("模型本轮没有完整返回，已有操作记录保留。")
                self._current(task_id, started, role, revision)
                if turn.text.strip():
                    final = turn.text
                    self.rt.log.emit(type="msg.said", task_id=task_id, actor=Actor.agent(role.id, role.role),
                        payload={"text": turn.text, "claim": turn.text[:180], "phase": "team",
                                 "run_id": started.payload["run_id"], "reply_to": message.seq})
                if not turn.calls:
                    break
                history.append(turn.message)
                waiting = False
                for call in turn.calls:
                    self._current(task_id, started, role, revision)
                    if call.name not in {t["name"] for t in tools}:
                        raise ValueError("模型请求了未接入工具。")
                    tool_start = self.rt.log.emit(type="team.tool_started", task_id=task_id, actor=HOST,
                        payload={"run_id": started.payload["run_id"], "role": role.id, "name": call.name,
                                 "call_id": call.id, "root": binding.get("root"), "message_seq": message.seq,
                                 "arguments": self.rt.workspaces._public_arguments(call.name, call.arguments)})
                    try:
                        schema = next(t["input_schema"] for t in tools if t["name"] == call.name)
                        if (set(call.arguments) - set(schema["properties"])
                                or not set(schema["required"]) <= set(call.arguments)):
                            raise ValueError("工具参数需要符合已提供的字段。")
                        result = self._execute(task_id, role, call.name, call.arguments, workspace, binding, tool_start, assignment)
                    except WorkConflict as exc:
                        result = {"status": "error", "message": str(exc)}
                    except (ValueError, KeyError, TypeError, OSError):
                        result = {"status": "error", "message": "操作未完成，请核对工具参数、权限与已有记录后修正。"}
                    self.rt.log.emit(type="team.tool_finished", task_id=task_id, actor=HOST,
                        payload={"started_seq": tool_start.seq, "run_id": started.payload["run_id"],
                                 "role": role.id, "name": call.name, "result": result})
                    history.append({"role": "tool", "tool_call_id": call.id, "is_error": result.get("status") != "ok",
                                    "content": json.dumps(result, ensure_ascii=False)})
                    if call.name in {"set_objective", "finish_work"}:
                        revision = self.view(task_id)["revision"]
                    if call.name == "wait_for_reply":
                        waiting = True
                if waiting:
                    status = "waiting"
                    break
                yield
            else:
                status, reason = "interrupted", "这轮操作较长，已保留成果与回执，交回店长检查后继续安排。"
        except WorkConflict as exc:
            status, reason = "interrupted", str(exc)
        except Exception:
            status, reason = "failed", f"{role.name}的本轮调用未完成，已有成果和操作记录保留；需要核对接入或执行记录。"
        self.rt.log.emit(type="team.run_finished", task_id=task_id, actor=HOST,
            payload={"message_seq": message.seq, "role": role.id, "run_id": started.payload["run_id"],
                     "status": status, "reason": reason})
        if status == "waiting" and assignment is not None:
            artifacts = [a for a in self.rt.task(task_id).deliverables if a["seq"] > started.seq and a.get("actor") == role.id]
            published = "；".join(f"{a.get('filename', a.get('title', '成果'))}（成果 #{a['seq']}）" for a in artifacts)
            text = ("本轮已保存：" + published + "。" if published else "")
            text += (final or "我已处理这轮消息，现在等待后续信息或安排。")
            self._send(task_id, role, assignment.actor.id, text, "progress", reply_to=assignment.seq)
        if status != "waiting" and (assignment is not None or (message.actor == Actor.human()
                and role.id != self.rt.organization.leader.id)):
            recipient = assignment.actor.id if assignment else self.rt.organization.leader.id
            self._send(task_id, role, recipient, reason or final or "委托已处理，请查看本轮成果和工具记录。", "report",
                       reply_to=assignment.seq if assignment else message.seq)

    def run(self, task_id):
        with activity_owner(self.rt.log.path, task_id, "simple-team") as owned:
            if not owned:
                return {"task_id": task_id, "team": self.view(task_id)}
            self._retire_interrupted(task_id)
            return self._drain(task_id)

    def _retire_interrupted(self, task_id):
        # The kernel lock proves no live team consumer owns these prior runs.
        for row in self.view(task_id)["runs"]:
            if row["status"] == "running":
                self.rt.log.emit(type="team.run_finished", task_id=task_id, actor=HOST,
                    payload={**row, "status": "interrupted",
                             "reason": "上次执行进程已结束，保留已有操作记录，未重放旧模型请求。"})

    def _drain(self, task_id):
        active = []
        while True:
            preempt = next((item for item in active if any(e.actor == Actor.human()
                and e.seq > item[0].seq and e.payload["recipient"] == item[1].id
                for e in self._messages(task_id))), None)
            if preempt is not None:
                active.remove(preempt)
                try:
                    next(preempt[2])
                    active.append(preempt)
                except StopIteration:
                    pass
                continue
            claimed = self._claim(task_id)
            if claimed is not None:
                actor_run = self._run_message(task_id, *claimed)
                if claimed[0].actor == Actor.human():
                    active.insert(0, (claimed[0], claimed[1], actor_run))
                else:
                    active.append((claimed[0], claimed[1], actor_run))
            if not active:
                break
            entry = active.pop(0)
            actor_run = entry[2]
            try:
                next(actor_run)
                active.append(entry)
            except StopIteration:
                pass
        return {"task_id": task_id, "team": self.view(task_id)}

    def archive(self, task_id):
        files = {a["filename"]: a for a in self.rt.task(task_id).deliverables
                 if a.get("filename") and isinstance(a.get("content"), str)}
        if not files:
            raise ValueError("尚无可下载的成果。")
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, artifact in files.items():
                archive.writestr(name, artifact["content"].encode("utf-8"))
            archive.writestr("__cater__/manifest.json", json.dumps({"objective": self.view(task_id)["objective"],
                "files": [{"filename": name, "seq": a["seq"], "sha256": a["content_sha256"]} for name, a in files.items()]},
                ensure_ascii=False, indent=2))
        return output.getvalue()
