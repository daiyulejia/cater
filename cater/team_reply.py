"""A customer answer is a message, not a task completion or review verdict."""

from .events import Actor
from .work import WorkConflict
from .reply_evidence import ReplyEvidenceError, evidence_schema, normalize, same
from .tool_protocol import strict_json


TOOL = "reply_to_customer"
OMITTED_WARNING = "可选答复引用未通过校验，未保存；答复正文已发送，不代表内容已验证。"


def _recorded_arguments(value):
    # Decode the host's audit-only UTF-8 wrapper, never execute its contents.
    if (isinstance(value, dict) and set(value) == {"encoding", "json"}
            and value["encoding"] == "escaped_json" and isinstance(value["json"], str)):
        try:
            return strict_json(value["json"])
        except (ValueError, TypeError, RecursionError):
            return None
    return value


def _message_input(team, task_id, message, events, by_seq):
    """Resolve the input of a host-paired ordinary message, never a reply hint."""
    from .team import HOST, LOCAL_EVENT_CONTRACT

    for receipt in events:
        r = receipt.payload
        result = r.get("result")
        if (receipt.type != "team.tool_finished" or receipt.actor != HOST
                or not isinstance(result, dict) or result.get("status") != "ok"
                or type(result.get("message_seq")) is not int or result["message_seq"] != message.seq
                or type(r.get("started_seq")) is not int):
            continue
        tool = by_seq.get(r["started_seq"])
        if (tool is None or tool.type != "team.tool_started" or tool.actor != HOST
                or not tool.seq < message.seq < receipt.seq
                or r.get("name") not in {"delegate", "send_message", "return_work"}
                or any(r.get(key) != tool.payload.get(key) for key in ("name", "role", "run_id"))
                or r.get("role") != message.actor.id
                or type(tool.payload.get("revision")) is not int
                or tool.payload["revision"] != message.payload.get("revision")):
            continue
        contract = tool.payload.get("execution_contract")
        if (not isinstance(contract, dict) or contract != LOCAL_EVENT_CONTRACT
                or type(contract.get("version")) is not int):
            continue
        args = _recorded_arguments(tool.payload.get("arguments"))
        if not isinstance(args, dict):
            continue
        name, kind = r["name"], message.payload.get("kind")
        text = args.get("instruction" if name == "delegate" else "text")
        if not isinstance(text, str) or text.strip() != message.payload.get("text"):
            continue
        if name == "delegate":
            if kind != "assignment" or "review" in args or args.get("recipient") != message.payload.get("recipient"):
                continue
        elif name == "return_work":
            if kind != "report":
                continue
        else:
            if (args.get("notify_only", False) is not False or "continue_assignment_seq" in args
                    or args.get("recipient") != message.payload.get("recipient")):
                continue
            if kind == "report":
                if type(args.get("reply_to")) is not int or args["reply_to"] != message.payload.get("reply_to"):
                    continue
            elif kind != "message" or "reply_to" in args:
                continue
        runs = [e for e in events if e.type == "team.run_started" and e.actor == HOST
                and e.payload.get("run_id") == r.get("run_id")]
        source_seq = tool.payload.get("message_seq")
        source = by_seq.get(source_seq) if type(source_seq) is int else None
        if (len(runs) == 1 and source is not None and source.type == "team.message_received"
                and source.payload.get("recipient") == message.actor.id
                and source.seq < runs[0].seq < tool.seq
                and all(runs[0].payload.get(key) == tool.payload.get(key)
                        for key in ("role", "run_id", "message_seq", "revision"))
                and not any(e.type == "team.run_finished" and e.actor == HOST and e.seq < receipt.seq
                            and all(e.payload.get(key) == runs[0].payload.get(key)
                                    for key in ("role", "run_id", "message_seq")) for e in events)):
            if name == "return_work":
                try:
                    role = team._role(message.actor.id)
                except (KeyError, ValueError):
                    continue
                assignment = team._waiting_assignment(task_id, source, role,
                    events=[e for e in events if e.seq < tool.seq])
                if assignment is None or assignment.seq != message.payload.get("reply_to"):
                    continue
            return source
    return None


def customer_source(team, task_id, message, role, *, events=None, before_seq=None):
    """Let the addressed role answer its current, host-paired consultation."""
    from .team import HOST, _assignment_report

    events = [e for e in (team.rt.log.read(task_id=task_id) if events is None else events)
              if e.task_id == task_id and (before_seq is None or e.seq < before_seq)]
    by_seq = {e.seq: e for e in events}
    if (message is None or type(message.seq) is not int or by_seq.get(message.seq) != message
            or message.type != "team.message_received" or message.payload.get("recipient") != role.id):
        return None
    if message.actor == Actor.human() and message.payload.get("kind") == "message":
        return message
    objective = next((e for e in reversed(events) if e.actor == HOST
                      and e.type in {"team.objective_updated", "team.objective_completed"}), None)
    revision = objective.seq if objective is not None else 0
    current = message
    while current is not None and current.actor.kind == "agent":
        seq = current.payload.get("reply_to")
        request = by_seq.get(seq) if type(seq) is int else None
        if (request is None or request.actor != Actor.agent(role.id, role.role)
                or request.type != "team.message_received" or request.seq >= current.seq
                or type(request.payload.get("review_seq")) is int
                or not _assignment_report(request, current)
                or type(current.payload.get("revision")) is not int
                or request.payload.get("revision") != current.payload["revision"]
                or current.payload["revision"] != revision
                or _message_input(team, task_id, current, events, by_seq) is None):
            return None
        current = _message_input(team, task_id, request, events, by_seq)
        if current is not None and current.payload.get("recipient") != role.id:
            return None
    if (current is not None and current.actor == Actor.human() and current.payload.get("kind") == "message"
            and current.seq > revision
            and not any(e.type == "team.message_received" and e.actor == Actor.human()
                        and e.payload.get("recipient") == role.id and e.seq > current.seq for e in events)):
        return current
    return None


def _omitted(result):
    return (result.get("reply_evidence_status") == "omitted_invalid"
            and result.get("reply_evidence_warning") == OMITTED_WARNING
            and "reply_evidence" not in result)


def _optional_evidence(team, task_id, refs, *, before_seq, events=None):
    try:
        return normalize(team, task_id, refs, before_seq=before_seq, events=events), {}
    except ReplyEvidenceError:
        # Discard the entire optional attachment, never promote partial or false quotes.
        return None, {"reply_evidence_status": "omitted_invalid", "reply_evidence_warning": OMITTED_WARNING}


def committed_reply(team, start, events):
    """Recover only a paired, atomic host receipt, not a role's completion claim."""
    from .team import HOST, LOCAL_EVENT_CONTRACT

    by_seq = {event.seq: event for event in events}
    p = start.payload
    source_seq = p.get("message_seq")
    incoming = by_seq.get(source_seq) if type(source_seq) is int else None
    if (by_seq.get(start.seq) != start or start.actor != HOST or start.type != "team.run_started"
            or incoming is None or incoming.type != "team.message_received"
            or incoming.payload.get("recipient") != p.get("role")
            or not incoming.seq < start.seq or not isinstance(p.get("run_id"), str) or not p["run_id"]):
        return None
    try:
        role = team._role(p["role"])
    except (KeyError, ValueError):
        return None
    for receipt in events:
        r = receipt.payload
        tool_seq = r.get("started_seq")
        tool = by_seq.get(tool_seq) if type(tool_seq) is int else None
        result = r.get("result")
        if (receipt.actor != HOST or receipt.type != "team.tool_finished" or r.get("name") != TOOL
                or tool is None or tool.actor != HOST or tool.type != "team.tool_started"
                or not start.seq < tool.seq < receipt.seq or tool.payload.get("name") != TOOL
                or any(r.get(key) != p.get(key) or tool.payload.get(key) != p.get(key)
                       for key in ("run_id", "role"))
                or type(tool.payload.get("message_seq")) is not int or tool.payload["message_seq"] != incoming.seq
                or not isinstance(result, dict) or result.get("status") != "ok"):
            continue
        source = customer_source(team, start.task_id, incoming, role, events=events, before_seq=tool.seq)
        if source is None:
            continue
        contract = tool.payload.get("execution_contract")
        args = _recorded_arguments(tool.payload.get("arguments"))
        answer_seq = result.get("message_seq")
        answer = by_seq.get(answer_seq) if type(answer_seq) is int else None
        if (not isinstance(contract, dict) or contract != LOCAL_EVENT_CONTRACT
                or type(contract.get("version")) is not int
                or not isinstance(args, dict) or set(args) - {"text", "evidence"} or "text" not in args
                or not isinstance(args["text"], str) or not args["text"].strip() or len(args["text"]) > 32000
                or type(result.get("reply_to")) is not int or result["reply_to"] != source.seq
                or answer is None or not tool.seq < answer.seq < receipt.seq
                or answer.type != "msg.said" or answer.actor != Actor.agent(role.id, role.role)):
            continue
        marker = answer.payload.get("reply_evidence_started_seq")
        if "reply_evidence_status" in result or "reply_evidence_warning" in result:
            # Omission is a historical host decision, not a current source check.
            if not (_omitted(result) and "evidence" in args and type(marker) is int and marker == tool.seq):
                continue
        else:
            try:
                evidence = normalize(team, start.task_id, args.get("evidence", []), before_seq=tool.seq, events=events)
            except (ValueError, TypeError, OSError):
                continue
            if (not same(result.get("reply_evidence"), evidence)
                    or (not (type(marker) is int and marker == tool.seq) if evidence is not None
                        else "reply_evidence_started_seq" in answer.payload)):
                continue
        a = answer.payload
        if (a.get("phase") == "team" and a.get("run_id") == p["run_id"] and a.get("text") == args["text"].strip()
                and type(a.get("reply_to")) is int and a["reply_to"] == source.seq
                and a.get("customer_reply_tool") == TOOL and a.get("audience") == "customer"
                and a.get("recipient") == "human" and a.get("response_kind") == "reply"):
            return answer
    return None


def schema():
    return {"name": TOOL, "description": (
        "核对后直接回答本轮顾客问题，发送给顾客并结束本轮。text只讲本次所需的结论、关键原因和必要操作；要求简短时不重述全流程，要求细节时完整回答。区分实际观察、源码推导和未验证建议，保留执行者与环境范围；失败实验未取得的输出不能写成实测，不混入之前的过程前言。"
        "伙伴咨询交回后，可由原顾客接收者答复原问题。只发消息，不代表整个任务完成或复核通过；正式交付仍用finish_work，伙伴交还委托仍用send_message。"
        "若只是解释或核验旧交付，无需重设目标或发布新报告；需要可查原文时可选填evidence，顾客默认折叠查看。"),
        "input_schema": {"type": "object", "additionalProperties": False,
            "properties": {"text": {"type": "string", "minLength": 1, "maxLength": 32000},
                           "evidence": evidence_schema()},
            "required": ["text"]}}


def reply(team, task_id, role, arguments, tool_start, assignment):
    from .team import HOST, _text

    text = _text(arguments["text"])
    events = team.rt.log.read(task_id=task_id)
    by_seq = {event.seq: event for event in events}
    stored = by_seq.get(tool_start.seq)
    incoming = by_seq.get(tool_start.payload.get("message_seq"))
    source = customer_source(team, task_id, incoming, role, events=events)
    run = next((e for e in events if e.type == "team.run_started" and e.actor == HOST
                and e.payload.get("run_id") == tool_start.payload.get("run_id")), None)
    if (stored != tool_start or tool_start.actor != HOST or tool_start.type != "team.tool_started"
            or tool_start.payload.get("name") != TOOL or tool_start.payload.get("role") != role.id
            or source is None or incoming is None
            or run is None or not incoming.seq < run.seq < tool_start.seq
            or any(run.payload.get(key) != tool_start.payload.get(key)
                   for key in ("run_id", "role", "message_seq"))
            or team.view(task_id)["revision"] != tool_start.payload.get("revision")
            or any(e.type == "team.run_finished" and e.actor == HOST
                   and e.payload.get("run_id") == run.payload["run_id"] for e in events)
            or assignment is not None or team._waiting_assignment(task_id, incoming, role) is not None):
        raise WorkConflict("此入口只回答本轮顾客输入或其已核对来源的咨询回报；已有委托仍向原委托人交还，未代替任何人宣布完成。")
    team._current(task_id, run, role, tool_start.payload["revision"])
    refs = arguments.get("evidence", [])
    previous = next((e for e in events if e.type == "msg.said"
        and e.actor == Actor.agent(role.id, role.role) and e.payload.get("run_id") == run.payload["run_id"]
        and e.payload.get("customer_reply_tool") == TOOL), None)
    if previous:
        if previous.payload.get("text") != text:
            raise WorkConflict("本轮顾客答复已发送；未用另一份文字覆盖已发送消息。")
        old_start_seq = previous.payload.get("reply_evidence_started_seq")
        old_start = by_seq.get(old_start_seq) if type(old_start_seq) is int else None
        if "reply_evidence_started_seq" in previous.payload and (
                old_start is None or old_start.actor != HOST or old_start.type != "team.tool_started"
                or old_start.payload.get("name") != TOOL or old_start.seq >= previous.seq
                or any(old_start.payload.get(key) != tool_start.payload.get(key)
                       for key in ("run_id", "role", "message_seq"))):
            raise WorkConflict("本轮已发答复的原引用关联无效，未覆盖既有消息。")
        if old_start and committed_reply(team, run, events) != previous:
            raise WorkConflict("本轮已发答复的原回执暂无法核对，未重复发送。")
        try:
            old_args = _recorded_arguments(old_start.payload.get("arguments")) if old_start else {}
            if not isinstance(old_args, dict):
                raise ValueError("Invalid original reply arguments")
            old_refs = old_args.get("evidence", [])
            if old_start:
                old_result = next(e.payload["result"] for e in events
                    if e.actor == HOST and e.type == "team.tool_finished" and e.payload.get("started_seq") == old_start.seq
                    and e.payload.get("name") == TOOL and e.payload.get("run_id") == run.payload["run_id"]
                    and e.payload.get("role") == role.id and isinstance(e.payload.get("result"), dict)
                    and e.payload["result"].get("message_seq") == previous.seq and e.payload["result"].get("status") == "ok")
                if _omitted(old_result):
                    if not same(old_refs, refs):
                        raise WorkConflict("本轮顾客答复已发送；未用另一组引用覆盖已发送消息。")
                    return dict(old_result)
            else:
                old_result = {"status": "ok", "message_seq": previous.seq, "reply_to": source.seq}
        except (ValueError, TypeError, StopIteration):
            raise WorkConflict("本轮已发答复的原引用暂无法核对，未覆盖既有消息。") from None
        evidence, omitted = _optional_evidence(team, task_id, refs, before_seq=tool_start.seq)
        if omitted or not same(old_result.get("reply_evidence"), evidence):
            raise WorkConflict("本轮顾客答复已发送；未用另一组引用覆盖已发送消息。")
        return dict(old_result)
    else:
        evidence, omitted = _optional_evidence(team, task_id, refs, before_seq=tool_start.seq)
        event = team.rt.log.emit(type="msg.said", task_id=task_id, actor=Actor.agent(role.id, role.role),
            payload={"text": text, "claim": text[:180], "phase": "team", "run_id": run.payload["run_id"],
                     "reply_to": source.seq, "response_kind": "reply", "audience": "customer",
                     "recipient": "human", "customer_reply_tool": TOOL,
                     **({"reply_evidence_started_seq": tool_start.seq} if evidence is not None or omitted else {})})
    return {"status": "ok", "message_seq": event.seq, "reply_to": source.seq,
            **({"reply_evidence": evidence} if evidence is not None else {}), **omitted}
