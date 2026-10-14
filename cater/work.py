"""Versioned work items and acceptance gates over the append-only event log."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any, Callable

from .events import Actor, Event, EventLog, HeadChanged
from .rules import RuleBook


class WorkConflict(ValueError):
    pass


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _text(value: object, label: str, limit: int = 12000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"invalid {label}")
    return value.strip()


def validate_items(items: object, role_ids: set[str]) -> list[dict[str, Any]]:
    if not isinstance(items, list) or not 1 <= len(items) <= 64:
        raise ValueError("work plan requires 1 to 64 items")
    result = []
    ids = set()
    for raw in items:
        if not isinstance(raw, dict):
            raise ValueError("work item must be an object")
        item_id = _text(raw.get("id"), "work item id", 200)
        if item_id in ids:
            raise ValueError("duplicate work item id")
        ids.add(item_id)
        owner = raw.get("owner")
        reviewer = raw.get("reviewer")
        if (not isinstance(owner, str) or owner not in role_ids
                or (reviewer is not None and (not isinstance(reviewer, str) or reviewer not in role_ids))):
            raise ValueError("work owner or reviewer is not configured")
        if reviewer == owner:
            raise ValueError("work author cannot review their own item")
        dependencies = raw.get("depends_on", [])
        if (not isinstance(dependencies, list) or any(not isinstance(d, str) for d in dependencies)
                or len(dependencies) != len(set(dependencies))):
            raise ValueError("dependencies must be distinct work item ids")
        criteria = raw.get("criteria")
        if not isinstance(criteria, list) or not 1 <= len(criteria) <= 32:
            raise ValueError("work item requires acceptance criteria")
        normalized = []
        for criterion in criteria:
            if not isinstance(criterion, dict) or criterion.get("kind") not in ("review", "local_check"):
                raise ValueError("unknown acceptance criterion kind")
            normalized.append({"id": _text(criterion.get("id"), "criterion id", 100),
                               "text": _text(criterion.get("text"), "criterion text", 2000),
                               "kind": criterion["kind"]})
        if len({c["id"] for c in normalized}) != len(normalized):
            raise ValueError("duplicate criterion id")
        result.append({"id": item_id, "owner": owner, "reviewer": reviewer,
                       "objective": _text(raw.get("objective"), "work objective"),
                       "depends_on": dependencies, "criteria": normalized})
    by_id = {item["id"]: item for item in result}
    visited, visiting = set(), set()

    def visit(item_id: str) -> None:
        if item_id not in ids:
            raise ValueError("missing work dependency")
        if item_id in visiting:
            raise ValueError("cyclic work dependencies")
        if item_id in visited:
            return
        visiting.add(item_id)
        for dep in by_id[item_id]["depends_on"]:
            visit(dep)
        visiting.remove(item_id)
        visited.add(item_id)

    for item_id in ids:
        visit(item_id)
    return result


class WorkLedger:
    def __init__(self, log: EventLog, rules: RuleBook):
        self.log, self.rules = log, rules

    def scope(self, task_id: str) -> dict[str, Any]:
        events = list(self.log.iter_events(task_id=task_id, event_types=("task.created", "human.commanded", "plan.proposed", "plan.amended")))
        directions = [e for e in events if e.type in {"task.created", "human.commanded"}]
        if not directions:
            raise KeyError(task_id)
        plans = [e.seq for e in events if e.type in {"plan.proposed", "plan.amended"}]
        return {"direction_seq": directions[-1].seq, "planning_seq": plans[-1] if plans else None,
                "rules": self.rules.current_snapshot(task_id)["fingerprint"]}

    def _write(self, task_id: str, build: Callable[[], tuple[str, dict[str, Any], Actor]]) -> Event:
        for _ in range(8):
            head = self.log.head_hash()
            kind, payload, actor = build()
            try:
                return self.log.emit(type=kind, task_id=task_id, actor=actor, payload=payload, expected_head=head)
            except HeadChanged:
                continue
        raise WorkConflict("work state changed; retry from the latest state")

    def plan(self, task_id: str, items: list[dict[str, Any]], *, role_ids: set[str], source_seq: int) -> Event:
        normalized = validate_items(items, role_ids)

        def build():
            current = self.view(task_id)
            if any(item.get("in_flight") for item in current["items"]):
                raise WorkConflict("running work must be reconciled before replacing the plan")
            return "work.plan_created", {"items": normalized, "source_seq": source_seq,
                                         "scope": self.scope(task_id)}, Actor.system("work_scheduler")
        return self._write(task_id, build)

    def view(self, task_id: str) -> dict[str, Any]:
        events = self.log.read(task_id=task_id)
        plan = next((event for event in reversed(events) if event.type == "work.plan_created"), None)
        if plan is None:
            return {"plan_seq": None, "source_seq": None, "items": [], "stage_accepted": False}
        items = {item["id"]: {**item, "status": "planned", "attempt_id": None, "submission": None,
                               "checks": [], "reason": ""} for item in plan.payload["items"]}
        for event in events:
            p = event.payload
            if event.seq <= plan.seq or p.get("plan_seq") != plan.seq:
                continue
            item = items.get(p.get("work_item_id"))
            if item is None:
                continue
            if event.type == "work.claimed":
                item.update(status="running", attempt_id=p["attempt_id"], lease_until=p["lease_until"],
                            inputs=p["inputs"], submission=None, checks=[], reason="")
            elif event.type == "work.submitted" and p.get("attempt_id") == item["attempt_id"]:
                item.update(status="submitted", submission={"seq": event.seq, **p}, reason="")
            elif event.type == "work.reviewed" and p.get("submission_seq") == (item["submission"] or {}).get("seq"):
                item.update(status=p["status"], checks=p["checks"], reason=p["reason"], review_seq=event.seq)
            elif event.type == "work.invalidated":
                for target in p["affected"]:
                    if target in items:
                        items[target].update(status="rework" if target == item["id"] else "stale",
                                             reason=p["reason"], submission=None, checks=[])
            elif event.type == "work.failed" and p.get("attempt_id") == item["attempt_id"]:
                item.update(status="uncertain" if p["uncertain"] else "rework", reason=p["reason"])
        scope_current = plan.payload["scope"] == self.scope(task_id)
        for item in items.values():
            item["in_flight"] = item["status"] in {"running", "uncertain"}
            if item["status"] == "running" and item["lease_until"] < time.time():
                item.update(status="uncertain", reason="执行期限已过，结果不明；不会自动重放。")
            if not scope_current and item["status"] != "uncertain":
                item.update(status="stale", reason="顾客方向或店规已变更，需要重新核对计划。")
        # Resolve readiness in dependency order, independent of JSON list order.
        resolved: set[str] = set()

        def resolve(item_id: str) -> None:
            if item_id in resolved:
                return
            item = items[item_id]
            for dep in item["depends_on"]:
                resolve(dep)
            if scope_current and item["status"] in {"planned", "rework", "stale"}:
                ready = all(items[d]["status"] == "accepted" for d in item["depends_on"])
                item.update(status="ready" if ready else "blocked")
                if not ready:
                    item["reason"] = "等待前置工作验收。"
            resolved.add(item_id)

        for item_id in items:
            resolve(item_id)
        return {"plan_seq": plan.seq, "source_seq": plan.payload["source_seq"], "scope": plan.payload["scope"],
                "scope_current": scope_current, "items": list(items.values()),
                "stage_accepted": all(i["status"] == "accepted" for i in items.values())}

    def _item(self, task_id: str, item_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        view = self.view(task_id)
        item = next((item for item in view["items"] if item["id"] == item_id), None)
        if item is None:
            raise KeyError(item_id)
        return view, item

    def claim(self, task_id: str, item_id: str, *, lease_seconds: float = 600) -> Event:
        if not 0 < lease_seconds <= 3600:
            raise ValueError("lease must be between 0 and 3600 seconds")

        def build():
            view, item = self._item(task_id, item_id)
            if item["status"] != "ready":
                raise WorkConflict("only ready work may be claimed")
            inputs = {i["id"]: i["submission"]["digest"] for i in view["items"] if i["id"] in item["depends_on"]}
            return "work.claimed", {"plan_seq": view["plan_seq"], "work_item_id": item_id,
                                    "attempt_id": uuid.uuid4().hex, "inputs": inputs,
                                    "scope": view["scope"], "lease_until": time.time() + lease_seconds}, Actor.system("work_scheduler")
        return self._write(task_id, build)

    def artifacts(self, task_id: str, submission: dict[str, Any]) -> list[dict[str, Any]]:
        ids = set(submission["artifact_seqs"])
        return [{"seq": event.seq, **event.payload} for event in self.log.iter_events(
            task_id=task_id, event_types=("deliverable.published",)) if event.seq in ids]

    def submit(self, task_id: str, claim: Event, artifact_seqs: list[int]) -> Event:
        def build():
            view, item = self._item(task_id, claim.payload["work_item_id"])
            if (item["status"] != "running" or view["plan_seq"] != claim.payload["plan_seq"]
                    or item["attempt_id"] != claim.payload["attempt_id"]):
                raise WorkConflict("work claim is expired or stale")
            if not artifact_seqs or len(artifact_seqs) != len(set(artifact_seqs)):
                raise ValueError("submission requires distinct inline artifacts")
            by_seq = {e.seq: e for e in self.log.iter_events(task_id=task_id, event_types=("deliverable.published",))}
            hashes = {}
            for seq in artifact_seqs:
                event = by_seq.get(seq)
                if (type(seq) is not int or event is None or event.actor.id != item["owner"]
                        or event.seq <= claim.seq or event.payload.get("attempt_id") != item["attempt_id"]
                        or event.payload.get("work_item_id") != item["id"]):
                    raise ValueError("artifact does not belong to this work attempt")
                content = event.payload.get("content")
                if (not isinstance(content, str) or not content.strip() or
                        hashlib.sha256(content.encode("utf-8")).hexdigest() != event.payload.get("content_sha256")):
                    raise ValueError("submission requires stored content with a valid hash")
                hashes[str(seq)] = event.payload["content_sha256"]
            payload = {"plan_seq": view["plan_seq"], "work_item_id": item["id"],
                       "attempt_id": item["attempt_id"], "artifact_seqs": artifact_seqs, "hashes": hashes,
                       "inputs": item["inputs"], "scope": view["scope"]}
            payload["digest"] = digest(payload)
            return "work.submitted", payload, Actor.agent(item["owner"], "worker")
        return self._write(task_id, build)

    def fail(self, task_id: str, claim: Event, reason: str, *, uncertain: bool) -> Event:
        def build():
            view, item = self._item(task_id, claim.payload["work_item_id"])
            if (view["plan_seq"] != claim.payload["plan_seq"] or item["attempt_id"] != claim.payload["attempt_id"]
                    or not item.get("in_flight")):
                raise WorkConflict("cannot fail a different attempt")
            return "work.failed", {"plan_seq": view["plan_seq"], "work_item_id": item["id"],
                                   "attempt_id": item["attempt_id"], "reason": reason, "uncertain": uncertain}, Actor.system("work_scheduler")
        return self._write(task_id, build)

    def checks(self, task_id: str, item: dict[str, Any]) -> dict[str, Event]:
        submission = item["submission"]
        return {e.payload["criterion_id"]: e for e in self.log.iter_events(task_id=task_id, event_types=("verification.completed",))
                if e.actor == Actor.system("local_verifier") and submission
                and e.payload.get("submission_digest") == submission["digest"]
                and e.payload.get("work_item_id") == item["id"] and e.payload.get("criterion_id")}

    def review(self, task_id: str, item_id: str, *, reviewer: str, submission_seq: int,
               results: object) -> Event:
        def build():
            view, item = self._item(task_id, item_id)
            if reviewer != item["reviewer"] or reviewer == item["owner"]:
                raise ValueError("only the assigned independent reviewer may review")
            if item["status"] != "submitted" or item["submission"]["seq"] != submission_seq:
                raise WorkConflict("review must target the current submitted artifact")
            artifacts = {a["seq"]: a for a in self.artifacts(task_id, item["submission"])}
            raw_results = results if isinstance(results, list) else []
            checks = []
            local = self.checks(task_id, item)
            for criterion in item["criteria"]:
                check = {"id": criterion["id"], "kind": criterion["kind"], "status": "unknown", "reason": "缺少可核对的验收依据。"}
                if criterion["kind"] == "local_check":
                    receipt = local.get(criterion["id"])
                    if receipt:
                        p = receipt.payload
                        passed = (p.get("outcome") == "passed" and type(p.get("exit_code")) is int
                                  and p["exit_code"] == 0 and p.get("artifacts_unchanged") is True
                                  and all(p.get(s, {}).get("complete") is True for s in ("stdout", "stderr")))
                        check.update(status="passed" if passed else "failed", receipt_seq=receipt.seq,
                                     reason="已记录该成果副本的本地检查；覆盖范围仅限所执行命令。")
                else:
                    matches = [r for r in raw_results if isinstance(r, dict) and r.get("id") == criterion["id"]]
                    if len(matches) == 1:
                        r = matches[0]
                        status, quote, seq = r.get("status"), r.get("quote"), r.get("artifact_seq")
                        reason = r.get("reason")
                        if (status in ("passed", "failed", "unknown") and isinstance(reason, str) and reason.strip()
                                and len(reason) <= 2000 and type(seq) is int and seq in artifacts
                                and isinstance(quote, str) and quote.strip() and len(quote) <= 2000
                                and quote in artifacts[seq]["content"]):
                            check.update(status=status, reason=reason, quote=quote, artifact_seq=seq)
                checks.append(check)
            status = "accepted" if all(c["status"] == "passed" for c in checks) else (
                "rework" if any(c["status"] == "failed" for c in checks) else "submitted")
            reason = "阶段标准已通过；不代表整件任务已经交付。" if status == "accepted" else (
                "阶段验收未通过，需要返工。" if status == "rework" else "证据不足，保留待验收状态。")
            return "work.reviewed", {"plan_seq": view["plan_seq"], "work_item_id": item_id,
                                     "submission_seq": submission_seq, "status": status,
                                     "checks": checks, "reason": reason}, Actor.agent(reviewer, "reviewer")
        return self._write(task_id, build)

    def rework(self, task_id: str, item_id: str, reason: str) -> Event:
        reason = _text(reason, "rework reason", 2000)

        def build():
            view, item = self._item(task_id, item_id)
            affected = {item_id}
            for _ in view["items"]:
                affected.update(i["id"] for i in view["items"] if set(i["depends_on"]) & affected)
            if any(i.get("in_flight") for i in view["items"] if i["id"] in affected):
                raise WorkConflict("in-flight effects must be reconciled before rework")
            return "work.invalidated", {"plan_seq": view["plan_seq"], "work_item_id": item["id"],
                                        "affected": sorted(affected), "reason": reason}, Actor.system("work_scheduler")
        return self._write(task_id, build)
