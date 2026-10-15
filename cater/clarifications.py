"""Version-bound member questions answered as explicit customer directions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .events import Actor, HeadChanged
from .work import WorkConflict

if TYPE_CHECKING:
    from .runtime import Runtime


def normalize_question(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"question", "options"}:
        raise ValueError("补充问题需要 question 和 options。")
    question, options = value["question"], value["options"]
    if not isinstance(question, str) or not question.strip() or len(question) > 2000:
        raise ValueError("补充问题为空或过长。")
    if (not isinstance(options, list) or len(options) not in (0, 2, 3, 4)
            or any(not isinstance(o, str) or not o.strip() or len(o) > 200 for o in options)):
        raise ValueError("选项需要为空或包含二至四个简短建议。")
    options = [o.strip() for o in options]
    if len(set(options)) != len(options):
        raise ValueError("补充问题的选项不能重复。")
    return {"question": question.strip(), "options": options}


class ClarificationService:
    def __init__(self, runtime: Runtime):
        self.rt = runtime

    def view(self, task_id: str) -> list[dict[str, Any]]:
        events = list(self.rt.log.iter_events(task_id=task_id,
                      event_types=("clarification.requested", "human.commanded")))
        if not any(e.type == "clarification.requested" for e in events):
            return []
        scope = self.rt.work.scope(task_id)
        responses = {}
        for event in events:
            if event.type == "human.commanded" and event.actor == Actor.human():
                response = event.payload.get("clarification")
                if isinstance(response, dict) and type(response.get("question_seq")) is int:
                    responses.setdefault(response["question_seq"], (event, response))
        result = []
        known = {r.id for r in self.rt.roles} | {self.rt.organization.leader.id}
        for event in events:
            if event.type != "clarification.requested":
                continue
            p = event.payload
            row = {"seq": event.seq, "role_id": p["role_id"], "question": p["question"],
                   "options": p["options"], "scope": p["scope"],
                   "status": "pending" if p["scope"] == scope and p["role_id"] in known else "stale"}
            if event.seq in responses:
                response_event, response = responses[event.seq]
                row.update(status="answered", response_seq=response_event.seq,
                           response={k: response[k] for k in ("kind", "answer", "option_index") if k in response})
            result.append(row)
        return result

    def pending(self, task_id: str) -> dict[str, Any] | None:
        return next((row for row in self.view(task_id) if row["status"] == "pending"), None)

    def request(self, task_id: str, role_id: str, proposal: object, *, scope: dict[str, Any]):
        question = normalize_question(proposal)
        leader = self.rt.organization.leader
        if role_id != leader.id:
            self.rt._role_by_id(role_id)
        actor = self.rt._leader_actor() if role_id == leader.id else Actor.agent(role_id, self.rt._role_by_id(role_id).role)

        def build():
            if self.rt.work.scope(task_id) != scope:
                raise WorkConflict("提问期间要求有变化，这个问题未采用。")
            if self.pending(task_id):
                raise WorkConflict("已有问题等待顾客补充，不重复提问。")
            return "clarification.requested", {**question, "role_id": role_id, "scope": scope}, actor

        return self.rt.work._write(task_id, build)

    def respond(self, task_id: str, *, question_seq: object, kind: object,
                request_id: object, option_index: object = None, answer: object = None):
        if type(question_seq) is not int or question_seq < 1 or kind not in ("choice", "custom", "skip"):
            raise ValueError("问题编号或回复方式无效。")
        if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 200:
            raise ValueError("回复需要有效的请求标识。")
        for _ in range(8):
            head = self.rt.log.head_hash()
            question = next((q for q in self.view(task_id) if q["seq"] == question_seq), None)
            if question is None:
                raise ValueError("这个补充问题不属于当前对话。")
            response = {"question_seq": question_seq, "kind": kind, "request_id": request_id}
            if kind == "choice":
                if (type(option_index) is not int or not 0 <= option_index < len(question["options"])
                        or answer is not None):
                    raise ValueError("请选择这个问题中的一个选项。")
                response.update(option_index=option_index, answer=question["options"][option_index])
            elif kind == "custom":
                if (option_index is not None or not isinstance(answer, str)
                        or not answer.strip() or len(answer) > 2000):
                    raise ValueError("请填写不超过 2000 字的想法。")
                response["answer"] = answer.strip()
            else:
                if option_index is not None or answer is not None:
                    raise ValueError("跳过问题不能同时选择或填写答案。")
                response["answer"] = "暂时跳过，没有确认任何选项；不代表授权执行或认可未说明的假设。"
            for event in self.rt.log.iter_events(task_id=task_id, event_types=("human.commanded",)):
                prior = event.payload.get("clarification") if event.actor == Actor.human() else None
                if isinstance(prior, dict) and prior.get("request_id") == request_id:
                    if prior != response:
                        raise WorkConflict("这次回复请求已用于不同内容，请刷新后重试。")
                    return event, False
            if question["status"] != "pending":
                raise WorkConflict("这个问题已回答或已随新方向失效，请刷新对话。")
            name = (self.rt.organization.leader.name if question["role_id"] == self.rt.organization.leader.id
                    else self.rt._role_by_id(question["role_id"]).name)
            instruction = f"@{name} 关于「{question['question']}」：{response['answer']}"
            try:
                event = self.rt.log.emit(type="human.commanded", task_id=task_id, actor=Actor.human(),
                    payload={"instruction": instruction, "intent": "direction", "next_action": "run",
                             "clarification": response}, expected_head=head)
                return event, True
            except HeadChanged:
                continue
        raise WorkConflict("对话正在更新，请读取最新问题后再回答。")
