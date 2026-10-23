"""Versioned peer review commitments for native message collaboration."""

from __future__ import annotations

from .events import Actor
from .work import WorkConflict


HOST = Actor.system("team")


def _positive(value):
    if type(value) is not int or value < 1:
        raise ValueError("需要本群有效的复核或成果序号。")
    return value


def _text(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 16000:
        raise ValueError("复核要求或反馈需要非空的具体文字。")
    return value.strip()


class TeamReviews:
    def __init__(self, runtime):
        self.rt = runtime

    def superseded_assignments(self, task_id, *, events=None):
        """Retire replaced review deliveries and their bound continuation/feedback inputs."""
        events = events if events is not None else self.rt.log.read(task_id=task_id)
        requests = [e for e in events if e.actor == HOST and e.type == "team.review_requested"]
        latest = {e.payload.get("root_seq") or e.seq: e for e in requests}
        messages = self.rt.team._messages(task_id, events=events)
        deliveries = {}
        for message in messages:
            p = message.payload
            if (message.actor.kind == "agent" and p.get("kind") == "assignment"
                    and type(p.get("review_seq")) is int):
                deliveries.setdefault((p["review_seq"], message.actor.id, p.get("recipient"), p.get("revision")), message)
        superseded = set()
        for request in requests:
            previous = request.payload
            current = latest[previous.get("root_seq") or request.seq]
            if (current.seq <= request.seq or any(current.payload.get(key) != previous.get(key)
                    for key in ("author", "reviewer", "revision"))):
                continue
            source = deliveries.get((request.seq, previous["author"], previous["reviewer"], previous["revision"]))
            if source is not None and request.seq < source.seq < current.seq:
                superseded.add(source.seq)
        from .team_work_control import linked_inputs

        return linked_inputs(messages, superseded)

    def _artifacts(self, task_id):
        return {a["seq"]: a for a in self.rt.task(task_id).deliverables if a.get("filename") and a.get("content_sha256")}

    def _latest(self, artifacts):
        return {a["filename"]: a["content_sha256"] for a in artifacts.values()}

    def view(self, task_id):
        events = self.rt.log.read(task_id=task_id)
        revision = max((e.seq for e in events if e.actor == HOST and e.type == "team.objective_updated"), default=0)
        latest = self._latest(self._artifacts(task_id))
        rows = {}
        for e in events:
            if e.actor != HOST:
                continue
            if e.type == "team.review_requested":
                p = e.payload
                root = p.get("root_seq") or e.seq
                rows[root] = {**p, "root_seq": root, "request_seq": e.seq, "status": "pending"}
            elif e.type == "team.review_recorded":
                row = rows.get(e.payload["root_seq"])
                if row and row["request_seq"] == e.payload["review_seq"]:
                    row.update(status=e.payload["verdict"], feedback=e.payload["feedback"], decision_seq=e.seq)
        for row in rows.values():
            message = self._request_message(task_id, row)
            if message is not None:
                row["message_seq"] = message.seq
            if row["revision"] != revision:
                row["status"] = "stale"
            elif any(latest.get(a["filename"]) != a["content_sha256"] for a in row["artifacts"]):
                row["status"] = "outdated"
        return list(rows.values())

    def _find(self, task_id, review_seq):
        _positive(review_seq)
        row = next((r for r in self.view(task_id) if r["request_seq"] == review_seq), None)
        if row is None:
            previous = next((e for e in self.rt.log.iter_events(task_id=task_id,
                event_types=("team.review_requested",)) if e.seq == review_seq and e.actor == HOST), None)
            if previous is None:
                raise WorkConflict("本群没有对应的正式复核请求。review_seq应取request_review返回的review_seq或reviews[].request_seq，"
                                   "不是普通委托、消息、成果或工具回执编号。若reviews为空，请让成果作者先发布实际版本并调用request_review；"
                                   "可先向委托人反馈阅读意见，但这不等于正式批准。")
            raise WorkConflict("这次复核请求已有后续提交，请读取reviews中的当前request_seq，不沿用旧请求编号。")
        if row["status"] == "stale":
            raise WorkConflict("这次复核已被新的提交或目标替代。旧目标记录仍可读取，但不能续签。"
                               "请由成果作者按当前目标决定是否提交待验版本；如需新复核，省略旧review_seq，"
                               "用request_review或delegate.review创建当前目标的复核，不沿用旧目标的批准。")
        return row

    def read(self, task_id, review_seq):
        """Expose historical feedback without making an old request signable again."""
        _positive(review_seq)
        current = self.view(task_id)
        row = next((r for r in current if r["request_seq"] == review_seq), None)
        events = self.rt.log.read(task_id=task_id)
        if row is not None:
            return self._with_predecessor(row, events)
        request = next((e for e in events if e.actor == HOST
                        and e.type == "team.review_requested" and e.seq == review_seq), None)
        if request is None:
            raise ValueError("未找到本群该复核记录；请使用reviews索引中的request_seq。")
        root = request.payload.get("root_seq") or request.seq
        replacement = next((r for r in current if r["root_seq"] == root), None)
        if replacement is None:
            raise ValueError("未找到本群该复核的当前记录。")
        row = {**request.payload, "root_seq": root, "request_seq": request.seq,
               "status": "superseded", "historical_status": "pending",
               "current_request_seq": replacement["request_seq"],
               "current_request_status": replacement["status"],
               "objective_current": replacement["status"] != "stale"}
        for event in events:
            if (event.actor == HOST and event.type == "team.review_recorded"
                    and event.payload["review_seq"] == request.seq
                    and event.payload["root_seq"] == root):
                row.update(historical_status=event.payload["verdict"],
                           feedback=event.payload["feedback"], decision_seq=event.seq)
        message = self._request_message(task_id, row)
        if message is not None:
            row["message_seq"] = message.seq
        return self._with_predecessor(row, events)

    def _with_predecessor(self, row, events):
        if "previous_review_seq" in row:
            return row
        previous = next((event for event in reversed(events) if event.actor == HOST
                         and event.type == "team.review_requested" and event.seq < row["request_seq"]
                         and (event.payload.get("root_seq") or event.seq) == row["root_seq"]
                         and all(event.payload.get(key) == row.get(key)
                                 for key in ("author", "reviewer", "revision"))), None)
        if previous is not None:
            return {**row, "previous_review_seq": previous.seq,
                    "previous_artifacts": previous.payload["artifacts"],
                    "previous_metadata_source": "event_history"}
        return row

    def superseded_artifacts(self, task_id, *, reviews=None):
        """Retire only exact old refs in explicitly continued, currently approved chains."""
        reviews = self.view(task_id) if reviews is None else reviews
        active = [r for r in reviews if r["status"] not in {"stale", "outdated"}]
        retained = {a["seq"] for row in active for a in row["artifacts"]}
        approved = {r["root_seq"]: r for r in active if r["status"] == "approved"}
        retired = set()
        for event in self.rt.log.iter_events(task_id=task_id, event_types=("team.review_requested",)):
            if event.actor != HOST:
                continue
            previous = event.payload
            current = approved.get(previous.get("root_seq") or event.seq)
            if current is not None and event.seq < current["request_seq"] and all(
                    previous.get(key) == current.get(key) for key in ("author", "reviewer", "revision")):
                retired.update(a["seq"] for a in previous["artifacts"])
        return retired - retained

    def _guard(self, task_id, role, tool_start):
        view = self.rt.team.view(task_id)
        if (view["status"] == "done" or view["revision"] != tool_start.payload["revision"]
                or self.rt.execution_control.view(task_id)["state"] != "running"):
            raise WorkConflict("工作已交付、暂停或目标已变化，未更新旧复核。")
        source = next((e for e in self.rt.team._messages(task_id) if e.seq == tool_start.payload["message_seq"]), None)
        if source is None or source.payload["recipient"] != role.id or any(
                e.seq > source.seq and e.actor == Actor.human() and e.payload["recipient"] == role.id
                for e in self.rt.team._messages(task_id)):
            raise WorkConflict("收到新的顾客消息，请先理解新上下文，再更新复核。")

    def request(self, task_id, role, arguments, tool_start, assignment):
        with self.rt.log.atomic():
            self._guard(task_id, role, tool_start)
            reviewer = self.rt.team._role(arguments["reviewer"])
            instruction = _text(arguments["instruction"])
            seqs = arguments["artifact_seqs"]
            if not isinstance(seqs, list) or not 1 <= len(seqs) <= 20:
                raise ValueError("需要一至二十份本次待复核的成果。")
            for seq in seqs:
                _positive(seq)
            if len(set(seqs)) != len(seqs):
                raise ValueError("复核成果不能重复。")
            artifacts = self._artifacts(task_id)
            if any(seq not in artifacts for seq in seqs):
                raise ValueError("找不到本群的完整成果。")
            selected = [artifacts[seq] for seq in seqs]
            authors = {a["actor"] for a in selected}
            previous = self._find(task_id, arguments["review_seq"]) if "review_seq" in arguments else None
            if previous is None and len(authors) != 1:
                raise ValueError("每份复核由一位作者负责；多位作者可分别提交。")
            for publisher in authors:
                self.rt.team._role(publisher)
            # Continuations keep the review owner, independently of who published the files.
            author = previous["author"] if previous else next(iter(authors))
            if role.id != author:
                if previous:
                    return {"status": "error", "code": "review_author_mismatch", "complete": True,
                            "outcome": "not_started", "review_seq": previous["request_seq"],
                            "expected_author": author,
                            "message": f"这条复核链由 {author} 负责，请交回原负责人修订并提交。"}
                return {"status": "error", "code": "review_author_mismatch", "complete": True,
                        "outcome": "not_started", "expected_author": author,
                        "message": f"这些成果由 {author} 发布，请通知作者本人提交复核，不能代提交。"}
            if reviewer.id == author or reviewer.id in authors:
                return {"status": "error", "code": "reviewer_mismatch", "complete": True,
                        "outcome": "not_started",
                        "message": "作者不能复核自己的成果，请选择另一位伙伴独立复核。"}
            if len({a["filename"] for a in selected}) != len(selected) or any(
                    self._latest(artifacts).get(a["filename"]) != a["content_sha256"] for a in selected):
                raise WorkConflict("请提交当前最新版本，不混用同名成果的旧版。")
            overlapping = [r for r in self.view(task_id) if r["status"] != "stale"
                           and {a["filename"] for a in r["artifacts"]} & {a["filename"] for a in selected}]
            if previous is None and overlapping:
                raise WorkConflict("这份成果已有复核记录；请用review_seq承接原修复闭环，不另开一份覆盖它。")
            if previous and previous["reviewer"] != reviewer.id:
                return {"status": "error", "code": "reviewer_mismatch", "complete": True,
                        "outcome": "not_started", "review_seq": previous["request_seq"],
                        "expected_reviewer": previous["reviewer"],
                        "message": f"修订应交回原复核者 {previous['reviewer']}，请保留该reviewer和当前review_seq。"}
            if previous and not any(a["seq"] > previous.get("decision_seq", previous["request_seq"]) for a in selected):
                raise WorkConflict("尚未发布本轮反馈后的新提交；请先发布当前实际成果，再交回复核，不沿用旧提交序号。")
            refs = [{k: a[k] for k in ("seq", "filename", "content_sha256")} for a in selected]
            request = self.rt.log.emit(type="team.review_requested", actor=HOST, task_id=task_id, payload={
                "root_seq": previous["root_seq"] if previous else None, "author": author, "reviewer": reviewer.id,
                "artifacts": refs, "revision": tool_start.payload["revision"], "instruction": instruction,
                **({"previous_review_seq": previous["request_seq"],
                    "previous_artifacts": previous["artifacts"]} if previous else {}),
                "run_id": tool_start.payload["run_id"], "requested_by": role.id})
            text = (f"请复核这次提交（review_seq={request.seq}）：\n{instruction}\n"
                    + "\n".join(f"成果 #{a['seq']}：{a['filename']}，SHA256 {a['content_sha256']}" for a in refs)
                    + "\n读取实际成果并按风险验证，用record_review记录changes_requested或approved及具体反馈；不要只在聊天里宣布通过。")
            if previous:
                text += (f"\n这是复核 #{previous['request_seq']} 的整组修订，旧批准不覆盖当前提交。"
                         "可以合理改名、增加、拆分或合并成果；请对照原目标、历史反馈与新旧实际内容检查有无遗漏，"
                         f"历史反馈可用read_checkpoint(review_seq={previous['request_seq']})读取。\n此前成果：\n"
                         + "\n".join(f"成果 #{a['seq']}：{a['filename']}，SHA256 {a['content_sha256']}"
                                     for a in previous["artifacts"]))
            message = self.rt.team._send(task_id, role, reviewer.id, text, "assignment",
                                         parent_assignment_seq=assignment.seq if assignment else None,
                                         visible=True, review_seq=request.seq)
            if assignment:
                self.rt.team._send(task_id, role, assignment.actor.id,
                    f"已提交成果请{reviewer.name}复核，完成后继续回报原委托。", "notification",
                    parent_assignment_seq=assignment.seq, visible=True)
            return {"status": "ok", "review_seq": request.seq, "root_seq": previous["root_seq"] if previous else request.seq,
                    "message_seq": message.seq, "waiting_for_review": True}

    def record(self, task_id, role, arguments, tool_start):
        with self.rt.log.atomic():
            self._guard(task_id, role, tool_start)
            row = self._find(task_id, arguments["review_seq"])
            verdict, feedback = arguments["verdict"], _text(arguments["feedback"])
            if role.id != row["reviewer"] or role.id == row["author"]:
                return {"status": "error", "code": "reviewer_mismatch", "complete": True,
                        "outcome": "not_started", "review_seq": row["request_seq"],
                        "expected_reviewer": row["reviewer"],
                        "message": (f"本次复核由 {row['reviewer']} 负责，作者或其他伙伴不能代签。"
                                    "请发消息给指定复核者，核对原 review_seq 的实际版本后，由对方登记结论；"
                                    "不用重新发布相同文件或新建复核。")}
            if verdict not in {"changes_requested", "approved"}:
                raise ValueError("复核需要明确通过或具体的修复要求。")
            if row["status"] not in {"pending"}:
                raise WorkConflict("版本已变化或本轮已有结论；请作者提交修正版后重新复核。")
            message = self._request_message(task_id, row)
            if message is None:
                raise WorkConflict("没有找到本次实际投递的复核委托。")
            decision = self.rt.log.emit(type="team.review_recorded", actor=HOST, task_id=task_id, payload={
                "root_seq": row["root_seq"], "review_seq": row["request_seq"], "reviewer": role.id,
                "verdict": verdict, "feedback": feedback, "run_id": tool_start.payload["run_id"]})
            text = ("复核通过。" if verdict == "approved" else "本轮需要修复，尚未验收。") + feedback
            text += ("\n请向原委托人交回这版成果。若继续修改，需将新版再次交回复核。" if verdict == "approved" else
                     f"\n请修改并发布新版后，调用request_review，review_seq={row['request_seq']}，仍交给我复核；不把修复自述当成验收。")
            sent = self.rt.team._send(task_id, role, row["author"], text, "report", reply_to=message.seq,
                                      visible=True, review_seq=row["request_seq"])
            return {"status": "ok", "review_seq": row["request_seq"], "decision_seq": decision.seq,
                    "verdict": verdict, "message_seq": sent.seq}

    def pending_for(self, task_id, role, assignment):
        if assignment is None or type(assignment.payload.get("review_seq")) is not int:
            return None
        if (self.rt.execution_control.view(task_id)["state"] != "running"
                or assignment.seq in self.rt.team._inactive_messages(task_id)):
            return None
        return next((row for row in self.view(task_id)
                     if row["request_seq"] == assignment.payload["review_seq"] and row["status"] == "pending"
                     and row["reviewer"] == role.id and row.get("message_seq") == assignment.seq), None)

    def followup(self, task_id, role, assignment, started, model_call_seq):
        """Offer one recorded chance per exact review, without choosing its verdict."""
        with self.rt.log.atomic():
            self._guard(task_id, role, started)
            row = self.pending_for(task_id, role, assignment)
            if row is None or any(e.actor == HOST and e.payload.get("review_seq") == row["request_seq"]
                    for e in self.rt.log.iter_events(task_id=task_id, event_types=("team.review_followup_requested",))):
                return None
            return self.rt.log.emit(type="team.review_followup_requested", actor=HOST, task_id=task_id, payload={
                "review_seq": row["request_seq"], "message_seq": assignment.seq, "role": role.id,
                "run_id": started.payload["run_id"], "model_call_seq": model_call_seq})

    def _request_message(self, task_id, row):
        return next((e for e in self.rt.team._messages(task_id) if e.seq > row["request_seq"]
                     and e.actor.id == row["author"] and e.payload.get("recipient") == row["reviewer"]
                     and e.payload.get("kind") == "assignment" and e.payload.get("review_seq") == row["request_seq"]), None)

    def blockers(self, task_id):
        return [r for r in self.view(task_id) if r["status"] in {"pending", "changes_requested", "outdated"}]
