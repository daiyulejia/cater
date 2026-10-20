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
            raise WorkConflict("这次复核已被新的提交或目标替代，请读取reviews中的当前记录。")
        return row

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
            if len(authors) != 1:
                raise ValueError("每份复核由一位作者负责；多位作者可分别提交。")
            author = next(iter(authors))
            self.rt.team._role(author)
            if role.id != author or reviewer.id == author:
                raise ValueError("由成果作者本人提交，另一位伙伴独立复核；店长可委托作者提交，不能自己验收自己。")
            if len({a["filename"] for a in selected}) != len(selected) or any(
                    self._latest(artifacts).get(a["filename"]) != a["content_sha256"] for a in selected):
                raise WorkConflict("请提交当前最新版本，不混用同名成果的旧版。")
            previous = self._find(task_id, arguments["review_seq"]) if "review_seq" in arguments else None
            overlapping = [r for r in self.view(task_id) if r["status"] != "stale" and r["author"] == author
                           and {a["filename"] for a in r["artifacts"]} & {a["filename"] for a in selected}]
            if previous is None and overlapping:
                raise WorkConflict("这份成果已有复核记录；请用review_seq承接原修复闭环，不另开一份覆盖它。")
            if previous and (previous["author"] != author or previous["reviewer"] != reviewer.id
                    or {a["filename"] for a in previous["artifacts"]} != {a["filename"] for a in selected}):
                raise WorkConflict("修复后交回原复核者，提交原复核范围的全部新版成果。")
            if previous and not any(a["seq"] > previous.get("decision_seq", previous["request_seq"]) for a in selected):
                raise WorkConflict("尚未发布本轮反馈后的新提交；请先发布当前实际成果，再交回复核，不沿用旧提交序号。")
            refs = [{k: a[k] for k in ("seq", "filename", "content_sha256")} for a in selected]
            request = self.rt.log.emit(type="team.review_requested", actor=HOST, task_id=task_id, payload={
                "root_seq": previous["root_seq"] if previous else None, "author": author, "reviewer": reviewer.id,
                "artifacts": refs, "revision": tool_start.payload["revision"], "instruction": instruction,
                "run_id": tool_start.payload["run_id"], "requested_by": role.id})
            text = (f"请复核这次提交（review_seq={request.seq}）：\n{instruction}\n"
                    + "\n".join(f"成果 #{a['seq']}：{a['filename']}，SHA256 {a['content_sha256']}" for a in refs)
                    + "\n读取实际成果并按风险验证，用record_review记录changes_requested或approved及具体反馈；不要只在聊天里宣布通过。")
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
                raise ValueError("只有本次指定的独立复核者可以登记验收。")
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

    def _request_message(self, task_id, row):
        return next((e for e in self.rt.team._messages(task_id) if e.seq > row["request_seq"]
                     and e.actor.id == row["author"] and e.payload.get("recipient") == row["reviewer"]
                     and e.payload.get("kind") == "assignment" and e.payload.get("review_seq") == row["request_seq"]), None)

    def blockers(self, task_id):
        return [r for r in self.view(task_id) if r["status"] in {"pending", "changes_requested", "outdated"}]
