"""Native chat questions: one explicit customer response, not an execution gate."""

from __future__ import annotations

from .events import Actor
from .work import WorkConflict


HOST = Actor.system("team")


def _text(value, label, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{label}为空或过长。")
    return value.strip()


def _identifier(value):
    value = _text(value, "问题或选项标识", 64)
    if not value.isascii() or not all(c.isalnum() or c in "_-" for c in value):
        raise ValueError("问题和选项标识只支持字母、数字、下划线和连字符。")
    return value


def normalize_questions(title, questions):
    title = _text(title, "补充信息标题", 200)
    if not isinstance(questions, list) or not 1 <= len(questions) <= 8:
        raise ValueError("每组补充信息需要一至八个问题。")
    result = []
    for q in questions:
        if not isinstance(q, dict) or set(q) != {"id", "question", "selection", "options"}:
            raise ValueError("问题需要标识、正文、单选或多选类型及选项。")
        if q["selection"] not in ("single", "multiple"):
            raise ValueError("选择类型需要为 single 或 multiple。")
        if not isinstance(q["options"], list) or len(q["options"]) > 8:
            raise ValueError("每题最多八个选项，也可以没有选项。")
        options = []
        for o in q["options"]:
            if (not isinstance(o, dict) or not {"id", "label"} <= set(o)
                    or set(o) - {"id", "label", "recommended"}
                    or type(o.get("recommended", False)) is not bool):
                raise ValueError("选项需要标识、文本及可选的推荐标记。")
            options.append({"id": _identifier(o["id"]), "label": _text(o["label"], "选项", 160),
                            "recommended": o.get("recommended", False)})
        if len({o["id"] for o in options}) != len(options) or len({o["label"] for o in options}) != len(options):
            raise ValueError("同一题的选项不能重复。")
        result.append({"id": _identifier(q["id"]), "question": _text(q["question"], "问题", 600),
                       "selection": q["selection"], "options": options})
    if len({q["id"] for q in result}) != len(result):
        raise ValueError("问题标识不能重复。")
    return {"title": title, "questions": result}


def normalize_answers(questions, answers):
    if not isinstance(answers, list) or len(answers) != len(questions):
        raise ValueError("请逐题回答或明确跳过，再整组提交。")
    by_id = {}
    for a in answers:
        if (not isinstance(a, dict) or set(a) != {"question_id", "option_ids", "text", "skipped"}
                or not isinstance(a["question_id"], str) or a["question_id"] in by_id):
            raise ValueError("每题需要独立且不重复的回答。")
        by_id[a["question_id"]] = a
    if set(by_id) != {q["id"] for q in questions}:
        raise ValueError("回答必须对应当前这一组问题。")
    result = []
    for q in questions:
        a = by_id[q["id"]]
        ids = a["option_ids"]
        if (type(a["skipped"]) is not bool or not isinstance(a["text"], str) or len(a["text"]) > 2000
                or not isinstance(ids, list) or any(not isinstance(i, str) for i in ids)
                or len(set(ids)) != len(ids) or not set(ids) <= {o["id"] for o in q["options"]}):
            raise ValueError("选择、自填或跳过内容无效。")
        text = a["text"].strip()
        if a["skipped"]:
            if ids or text:
                raise ValueError("跳过不能同时提交选项或文字。")
        elif not ids and not text:
            raise ValueError("空白回答需要明确选择跳过。")
        elif q["selection"] == "single" and len(ids) > 1:
            raise ValueError("单选题只能选一个选项，可另加文字。")
        result.append({"question_id": q["id"], "option_ids": [o["id"] for o in q["options"] if o["id"] in ids],
                       "text": text, "skipped": a["skipped"]})
    return result


class QuestionnaireService:
    def __init__(self, runtime):
        self.rt = runtime

    def view(self, task_id):
        events = list(self.rt.log.iter_events(task_id=task_id, event_types=(
            "team.questions_requested", "team.message_received", "team.objective_updated", "team.objective_completed")))
        requests = [e for e in events if e.type == "team.questions_requested" and e.actor == HOST]
        responses = {}
        for e in events:
            if e.type == "team.message_received" and e.actor == Actor.human():
                response = e.payload.get("questionnaire")
                if isinstance(response, dict) and type(response.get("question_seq")) is int:
                    responses.setdefault(response["question_seq"], (e, response))
        known = {self.rt.organization.leader.id, *(r.id for r in self.rt.roles)}
        result = []
        for e in requests:
            p = e.payload
            changed = any(other.seq > e.seq and (
                other.type == "team.message_received" and other.actor == Actor.human()
                and (not isinstance(other.payload.get("questionnaire"), dict)
                     or other.payload["questionnaire"].get("question_seq") == e.seq)
                or other.type in {"team.objective_updated", "team.objective_completed"} and other.actor == HOST
            ) for other in events)
            replaced = any(other.seq > e.seq and other.payload["role_id"] == p["role_id"] for other in requests)
            row = {"seq": e.seq, "role_id": p["role_id"], "title": p["title"], "questions": p["questions"],
                   "status": "stale" if changed or replaced or p["role_id"] not in known else "pending"}
            if e.seq in responses:
                response_event, response = responses[e.seq]
                row.update(status="answered", response_seq=response_event.seq, response={"answers": response["answers"]})
            result.append(row)
        return result

    def request(self, task_id, role, proposal, tool_start, assignment=None):
        normalized = normalize_questions(proposal["title"], proposal["questions"])
        with self.rt.log.atomic():
            # The tool's persisted source, not the model, supplies routing and provenance.
            source = next(e for e in self.rt.team._messages(task_id) if e.seq == tool_start.payload["message_seq"])
            if self.rt.execution_control.view(task_id)["state"] != "running":
                raise WorkConflict("执行已暂停或取消，未发送新问题。")
            if source.payload["recipient"] != role.id:
                raise WorkConflict("问题来源与当前角色不一致。")
            if (self.rt.team.view(task_id)["revision"] != tool_start.payload["revision"]
                    or any(e.seq > source.seq and e.actor == Actor.human() and e.payload["recipient"] == role.id
                           for e in self.rt.team._messages(task_id))):
                raise WorkConflict("提问期间收到新方向，请先理解最新对话。")
            event = self.rt.log.emit(type="team.questions_requested", actor=HOST, task_id=task_id,
                payload={**normalized, "role_id": role.id, "run_id": tool_start.payload["run_id"],
                         "revision": tool_start.payload["revision"],
                         "message_seq": source.seq, "assignment_seq": assignment.seq if assignment else None})
        return event

    def respond(self, task_id, *, question_seq, answers, request_id):
        if type(question_seq) is not int or question_seq < 1:
            raise ValueError("补充问题编号无效。")
        request_id = _text(request_id, "请求标识", 128)
        if len(request_id) < 8:
            raise ValueError("请求标识过短。")
        with self.rt.log.atomic():
            question = next((q for q in self.view(task_id) if q["seq"] == question_seq), None)
            if question is None:
                raise ValueError("这组问题不属于当前对话。")
            response = {"question_seq": question_seq, "answers": normalize_answers(question["questions"], answers),
                        "request_id": request_id}
            for event in self.rt.team._messages(task_id):
                if event.actor == Actor.human() and event.payload.get("request_id") == request_id:
                    if event.payload.get("questionnaire") != response:
                        raise WorkConflict("同一请求标识已经用于不同内容。")
                    return event, False
            if question["status"] != "pending":
                raise WorkConflict("这组问题已回答或已有新的对话，请根据最新消息继续。")
            if self.rt.execution_control.view(task_id)["state"] != "running":
                raise WorkConflict("执行已暂停或取消，恢复后再提交；仍可直接聊天。")
            lines = ["关于「" + question["title"] + "」，我的补充："]
            for q, a in zip(question["questions"], response["answers"]):
                parts = [o["label"] for o in q["options"] if o["id"] in a["option_ids"]]
                if a["text"]:
                    parts.append(a["text"])
                lines.append(q["question"] + "：" + ("暂时跳过（信息未知，不代表同意或授权）" if a["skipped"] else "；".join(parts)))
            text = _text("\n".join(lines), "补充内容", 32000)
            event = self.rt.log.emit(type="team.message_received", task_id=task_id, actor=Actor.human(),
                payload={"text": text, "recipient": question["role_id"], "kind": "message",
                         "request_id": request_id, "questionnaire": response,
                         "revision": next(e.payload["revision"] for e in self.rt.log.iter_events(
                             task_id=task_id, event_types=("team.questions_requested",)) if e.seq == question_seq and e.actor == HOST)})
            return event, True
