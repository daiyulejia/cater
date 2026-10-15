"""Versioned work items and acceptance gates over the append-only event log."""

from __future__ import annotations

import hashlib
import json
import math
import time
import uuid
from typing import Any, Callable

from .events import Actor, Event, EventLog, HeadChanged
from .directions import changes_direction
from .rules import RuleBook
from .review_contract import ISSUES, assessment


class WorkConflict(ValueError):
    pass


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _text(value: object, label: str, limit: int = 12000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"invalid {label}")
    return value.strip()


def obligation_id(item_id: str, criterion_id: str) -> str:
    return "check_" + digest([item_id, criterion_id])


def check_obligations(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"id": c.get("obligation_id") or obligation_id(i["id"], c["id"]),
             "work_item_id": i["id"], "criterion_id": c["id"], "text": c["text"],
             "subject": c.get("subject") or i["objective"], "status": "active"}
            for i in items for c in i["criteria"] if c["kind"] == "local_check"]


def preserve_check_obligations(items: list[dict[str, Any]], contract: dict[str, Any]) -> list[dict[str, Any]]:
    incoming = check_obligations(items)
    if len({c["id"] for c in incoming}) != len(incoming):
        raise ValueError("同一检查义务不能绑定多个对象或标准。")
    by_id = {c["id"]: c for c in incoming}
    current = contract.get("obligations", [])
    for required in current:
        replacement = by_id.get(required["id"])
        if required["status"] == "retired":
            if replacement or any((r["work_item_id"], r["criterion_id"]) ==
                                  (required["work_item_id"], required["criterion_id"]) for r in incoming):
                raise ValueError("已停用检查不能在规划中静默恢复，需要显式修订契约。")
        elif replacement != required:
            raise ValueError("重新规划不能删改、合并或转移必需实际检查及其对象，需要显式修订检查契约。")
    # Existing retired definitions stay in the registry, not in executable criteria.
    return [*current, *(c for c in incoming if c["id"] not in {r["id"] for r in current})]


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
        objective = _text(raw.get("objective"), "work objective")
        for criterion, original in zip(normalized, criteria):
            if criterion["kind"] == "local_check":
                check_id = original.get("obligation_id")
                subject = original.get("subject")
                criterion["obligation_id"] = _text(obligation_id(item_id, criterion["id"]) if check_id is None else check_id, "check obligation id", 100)
                criterion["subject"] = _text(objective if subject is None else subject, "check subject")
            elif original.get("obligation_id") is not None or original.get("subject") is not None:
                raise ValueError("review criterion cannot impersonate a local check obligation")
        result.append({"id": item_id, "owner": owner, "reviewer": reviewer,
                       "objective": objective,
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
    def __init__(self, log: EventLog, rules: RuleBook, organization_fingerprint: str | None = None):
        self.log, self.rules = log, rules
        self.organization_fingerprint = organization_fingerprint

    def check_contract(self, task_id: str) -> dict[str, Any]:
        events = list(self.log.iter_events(task_id=task_id, event_types=("work.plan_created", "checks.contract_revised")))
        for event in reversed(events):
            if event.type == "checks.contract_revised" and event.actor == Actor.human():
                return {"revision": event.seq, "obligations": event.payload["obligations"]}
            if event.type == "work.plan_created" and event.actor == Actor.system("work_scheduler"):
                obligations = (event.payload["check_contract"]["obligations"] if "check_contract" in event.payload
                               else check_obligations(event.payload["items"]))
                return {"revision": event.seq, "obligations": obligations}
        return {"revision": None, "obligations": []}

    def revise_checks(self, task_id: str, changes: object, *, reason: str, expected_revision: int | None) -> Event:
        from .delivery import _review_state

        reason = _text(reason, "check revision reason", 4000)
        if (not isinstance(changes, list) or not 1 <= len(changes) <= 128
                or (expected_revision is not None and type(expected_revision) is not int)):
            raise ValueError("invalid check contract revision")

        def build():
            contract = self.check_contract(task_id)
            if contract["revision"] != expected_revision:
                raise WorkConflict("检查契约已经更新，请重新核对当前版本。")
            if any(i.get("in_flight") for i in self.view(task_id)["items"]):
                raise WorkConflict("在途工作或检查需要先核对，不能修订检查义务。")
            # Recovery folding also verifies the human/system evidence it cites.
            events = self.log.read(task_id=task_id)
            if _review_state(events)[2]:
                raise WorkConflict("整体复核尚未核对结束，不能修订检查义务。")
            for started in events:
                if started.type == "delivery.check_started" and started.actor == Actor.system("local_verifier"):
                    ended = any(e.type == "verification.completed" and e.actor == started.actor and e.seq > started.seq
                        and e.payload.get("check_attempt_seq") == started.seq
                        and e.payload.get("delivery_digest") == started.payload["delivery_digest"]
                        and e.payload.get("criterion_id") == started.payload["criterion_id"] for e in events)
                    if not ended:
                        raise WorkConflict("整体检查尚未核对结束，不能修订检查义务。")
            rows = {r["id"]: dict(r) for r in contract["obligations"]}
            seen = set()
            for change in changes:
                if not isinstance(change, dict):
                    raise ValueError("check change must be an object")
                row_id = change.get("id")
                if not isinstance(row_id, str) or row_id not in rows or row_id in seen:
                    raise ValueError("check obligation is missing or duplicated")
                seen.add(row_id)
                action = change.get("action")
                fields = set(change) - {"id", "action"}
                if action in {"retire", "restore"}:
                    if fields:
                        raise ValueError("retire or restore cannot also replace a definition")
                    rows[row_id]["status"] = "retired" if action == "retire" else "active"
                elif action == "update" and fields and fields <= {"work_item_id", "criterion_id", "text", "subject"}:
                    for field in fields:
                        rows[row_id][field] = _text(change[field], "check " + field,
                                                    100 if field == "criterion_id" else 200 if field == "work_item_id" else 2000 if field == "text" else 12000)
                else:
                    raise ValueError("unknown or incomplete check change")
            active = [r for r in rows.values() if r["status"] == "active"]
            if len({(r["work_item_id"], r["criterion_id"]) for r in active}) != len(active):
                raise ValueError("active check obligations cannot share a binding")
            return "checks.contract_revised", {"base_revision": expected_revision, "changes": changes,
                                               "obligations": list(rows.values()), "reason": reason}, Actor.human()
        return self._write(task_id, build)

    def scope(self, task_id: str) -> dict[str, Any]:
        events = list(self.log.iter_events(task_id=task_id, event_types=("task.created", "human.commanded", "human.decided", "plan.proposed", "plan.amended", "planning.requested")))
        directions = [e for e in events if e.type == "task.created"
                      or (e.type == "human.commanded" and changes_direction(e.payload))
                      or (e.type == "human.decided" and e.payload.get("resolution") == "rework")]
        if not directions:
            raise KeyError(task_id)
        plans = [e.seq for e in events if e.type in {"plan.proposed", "plan.amended", "planning.requested"}]
        scope = {"direction_seq": directions[-1].seq, "planning_seq": plans[-1] if plans else None,
                 "rules": self.rules.current_snapshot(task_id)["fingerprint"]}
        if self.organization_fingerprint is not None:
            scope["organization"] = self.organization_fingerprint
        revisions = [e.seq for e in self.log.iter_events(task_id=task_id, event_types=("checks.contract_revised",))
                     if e.actor == Actor.human()]
        if revisions:
            scope["checks_revision"] = revisions[-1]
        return scope

    def directions(self, task_id: str, through_seq: int) -> list[dict[str, Any]]:
        return [{"seq": e.seq, **({"instruction": e.payload.get("reason", "顾客要求返工")}
                 if e.type == "human.decided" else
                 {key: e.payload[key] for key in ("instruction", "goal", "acceptance") if key in e.payload})}
                for e in self.log.iter_events(task_id=task_id, event_types=("human.commanded", "human.decided"))
                if e.seq <= through_seq and ((e.type == "human.commanded" and changes_direction(e.payload))
                    or (e.type == "human.decided" and e.payload.get("resolution") == "rework"))]

    def _write(self, task_id: str, build: Callable[[], tuple[str, dict[str, Any], Actor]]) -> Event:
        for _ in range(8):
            head = self.log.head_hash()
            kind, payload, actor = build()
            try:
                return self.log.emit(type=kind, task_id=task_id, actor=actor, payload=payload, expected_head=head)
            except HeadChanged:
                continue
        raise WorkConflict("work state changed; retry from the latest state")

    def plan(self, task_id: str, items: list[dict[str, Any]], *, role_ids: set[str], source_seq: int,
             expected_scope: dict[str, Any] | None = None, expected_plan_seq: int | None = None,
             planning: dict[str, Any] | None = None) -> Event:
        normalized = validate_items(items, role_ids)

        def build():
            current = self.view(task_id)
            if any(item.get("in_flight") for item in current["items"]):
                raise WorkConflict("running work must be reconciled before replacing the plan")
            scope = self.scope(task_id)
            if expected_scope is not None and (scope != expected_scope or current["plan_seq"] != expected_plan_seq):
                raise WorkConflict("planning inputs changed before installation")
            obligations = preserve_check_obligations(normalized, self.check_contract(task_id))
            if len(obligations) > 4096:
                raise ValueError("check contract capacity exceeded; split the task")
            return "work.plan_created", {"items": normalized, "source_seq": source_seq, "scope": scope,
                                         "check_contract": {"obligations": obligations},
                                         "planning": planning,
                                         "customer_directions": self.directions(task_id, scope["direction_seq"])}, Actor.system("work_scheduler")
        return self._write(task_id, build)

    def view(self, task_id: str) -> dict[str, Any]:
        events = self.log.read(task_id=task_id)
        plan = next((event for event in reversed(events) if event.type == "work.plan_created"), None)
        if plan is None:
            return {"plan_seq": None, "source_seq": None, "items": [], "stage_accepted": False}
        items = {item["id"]: {**item, "status": "planned", "attempt_id": None, "submission": None,
                               "checks": [], "reason": ""} for item in plan.payload["items"]}
        for candidate in (plan.payload.get("planning") or {}).get("reuse", []):
            if candidate["work_item_id"] in items:
                items[candidate["work_item_id"]]["reuse_candidate"] = candidate
        histories, review_histories = {}, {}

        def review_history(item):
            if item["id"] not in review_histories:
                review_histories[item["id"]] = self._review_history(events, plan.seq, item["id"])
            return review_histories[item["id"]]

        def history(item):
            submission = item["submission"]
            key = submission["seq"]
            if key not in histories:
                histories[key] = self._check_history(events, item)
            return histories[key]

        def invalidate_dependents(item_id):
            affected = {item_id}
            for _ in items:
                affected.update(i["id"] for i in items.values() if set(i["depends_on"]) & affected)
            for target in affected - {item_id}:
                dependent = items[target]
                uncertain = dependent["status"] in {"running", "uncertain"}
                checking = dependent["submission"] is not None and any(
                    e.seq not in state["receipts"] for state in history(dependent)["criteria"].values()
                    for e in state["starts"])
                reviewing = bool(review_history(dependent)["pending"])
                dependent.update(status="uncertain" if uncertain else "stale",
                                 submission=dependent["submission"] if checking or reviewing else None, checks=[],
                                 dependency_check_stale=checking or reviewing,
                                 dependency_uncertain=uncertain,
                                 reason="前置成果重新检查，执行副作用须先核对。" if uncertain else "前置成果重新检查，需要重新验收。")

        for event in events:
            p = event.payload
            if event.seq <= plan.seq or (event.type != "verification.completed" and p.get("plan_seq") != plan.seq):
                continue
            item_id = p.get("work_item_id")
            item = items.get(item_id) if isinstance(item_id, str) else None
            if item is None:
                continue
            if event.type == "work.claimed":
                item.update(status="running", attempt_id=p["attempt_id"], lease_until=p["lease_until"],
                            inputs=p["inputs"], submission=None, checks=[], reason="", dependency_uncertain=False,
                            dependency_check_stale=False)
            elif (event.type == "work.submitted" and p.get("attempt_id") == item["attempt_id"]
                  and not item.get("dependency_uncertain") and not item.get("dependency_check_stale")):
                item.update(status="submitted", submission={"seq": event.seq, **p}, reason="")
            elif (event.type == "work.check_started" and item["submission"]
                  and event.seq in history(item)["valid_starts"]):
                item.update(status="submitted", checks=[], reason="本地检查已启动，需要等待本波次全部回执并重新验收。")
                invalidate_dependents(item["id"])
            elif (event.type == "verification.completed" and item["submission"]
                  and event.seq in history(item)["valid_receipts"] and item["status"] == "accepted"):
                item.update(status="submitted", checks=[], reason="本地检查证据已更新，需要重新验收。")
                invalidate_dependents(item["id"])
            elif (event.type == "work.reviewed" and p.get("submission_seq") == (item["submission"] or {}).get("seq")
                  and not item.get("dependency_uncertain") and not item.get("dependency_check_stale")):
                item.update(status=p["status"], checks=p["checks"], reason=p["reason"], review_seq=event.seq)
            elif event.type == "work.review_deferred" and p.get("submission_seq") == (item["submission"] or {}).get("seq"):
                item.update(reason=p["reason"])
            elif (event.type in {"work.review_finished", "work.review_reconciled"}
                  and p.get("submission_seq") == (item["submission"] or {}).get("seq")):
                lifecycle = review_history(item)
                if event.seq in lifecycle["terminal_seqs"] and (
                        event.type == "work.review_reconciled" or p["status"] in {"stale", "recoverable"}):
                    item.update(status="submitted", checks=[], reason=p["reason"])
            elif event.type == "work.invalidated":
                for target in p["affected"]:
                    if target in items:
                        items[target].update(status="rework" if target == item["id"] else "stale",
                                             reason=p["reason"], submission=None, checks=[])
            elif event.type == "work.failed" and p.get("attempt_id") == item["attempt_id"]:
                item.update(status="uncertain" if p["uncertain"] else "rework", reason=p["reason"],
                            dependency_uncertain=bool(p["uncertain"] and item.get("dependency_uncertain")))
        scope_current = plan.payload["scope"] == self.scope(task_id)
        for item in items.values():
            reviews = review_history(item)
            pending_reviews = reviews["pending"]
            item["execution_in_flight"] = item["status"] in {"running", "uncertain"}
            item["check_in_flight"] = False
            item["check_uncertain"] = False
            item["check_lifecycle"] = {}
            if item["submission"]:
                states = history(item)["criteria"]
                for criterion_id, state in states.items():
                    pending = [e for e in state["starts"] if e.seq not in state["receipts"]]
                    expired = [e for e in pending if e.payload["lease_until"] <= time.time()]
                    item["check_in_flight"] |= bool(pending)
                    item["check_uncertain"] |= bool(expired)
                    item["check_lifecycle"][criterion_id] = {
                        "check_wave": state["wave"], "attempt_seqs": [e.seq for e in state["starts"]],
                        "pending_seqs": [e.seq for e in pending], "uncertain_seqs": [e.seq for e in expired],
                        "receipt_seqs": [e.seq for e in state["receipts"].values()],
                        "failed": (any(not self._receipt_passed(e) for e in state["receipts"].values())
                                   if state["starts"] else bool(state["legacy"]
                                   and not self._receipt_passed(state["legacy"][-1]))),
                    }
                local = self._selected_checks(states)
                if item["status"] == "accepted" and any(
                        c["kind"] == "local_check" and (c["id"] not in local or not self._receipt_passed(local[c["id"]]))
                        for c in item["criteria"]):
                    item.update(status="submitted", checks=[], reason="当前本地检查尚未全部通过，旧验收不能沿用。")
                if item["check_in_flight"]:
                    item.update(status="uncertain" if item["check_uncertain"] else "submitted",
                                reason="本地检查期限已过，结果不明；不会自动重放。" if item["check_uncertain"]
                                else "本地检查尚未全部结束，不能放行。")
                elif item.get("dependency_check_stale") and not pending_reviews:
                    item.update(status="stale", submission=None, checks=[], dependency_check_stale=False,
                                reason="前置成果重新检查，旧提交已失效。")
            item["review_in_flight"] = bool(pending_reviews)
            item["review_uncertain"] = any(
                e.payload["lease_until"] <= time.time() or e.seq in reviews["finishes"] for e in pending_reviews)
            latest = reviews["starts"][-1] if reviews["starts"] else None
            item["review_lifecycle"] = {}
            if latest:
                finish = reviews["finishes"].get(latest.seq)
                reconciled = reviews["reconciled"].get(latest.seq)
                item["review_lifecycle"] = {
                    "attempt_seq": latest.seq, "submission_seq": latest.payload["submission_seq"],
                    "lease_until": latest.payload["lease_until"],
                    "status": ("reconciled" if reconciled else finish.payload["status"] if finish else
                               "uncertain" if latest.payload["lease_until"] <= time.time() else "reviewing"),
                    "finish_seq": finish.seq if finish else None,
                    "recovery": finish.payload.get("recovery") if finish else None,
                    "reconciled_seq": reconciled.seq if reconciled else None,
                    "pending_seqs": [e.seq for e in pending_reviews],
                    "reason": (reconciled.payload["reason"] if reconciled else finish.payload.get("reason", "") if finish
                               else "等待原调用返回；超时后需要先核对，不能自动重放。"),
                }
            if pending_reviews:
                item.update(status="uncertain" if item["review_uncertain"] or not item["submission"] else "submitted",
                            reason="阶段复核结果不明；核对调用已停止及其影响后才能恢复，不会按超时自动重放。"
                            if item["review_uncertain"] else "阶段复核调用尚未结束，正在等待同一版本的结论。")
            item["in_flight"] = item["execution_in_flight"] or item["check_in_flight"] or item["review_in_flight"]
            if item["status"] == "running" and item["lease_until"] < time.time():
                item.update(status="uncertain", reason="执行期限已过，结果不明；不会自动重放。")
            if not scope_current and item["status"] != "uncertain":
                item.update(status="stale", reason="顾客方向、店规或组织配置已变更，需要重新核对计划。")
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
                "planning": plan.payload.get("planning"), "check_contract": self.check_contract(task_id),
                "scope_current": scope_current, "items": list(items.values()),
                "customer_directions": (plan.payload["customer_directions"] if "customer_directions" in plan.payload
                                        else self.directions(task_id, plan.payload["scope"]["direction_seq"])),
                "stage_accepted": all(i["status"] == "accepted" for i in items.values())}

    def defer_review(self, task_id: str, item_id: str, submission_seq: int, reason: str, *, response_repair: bool = False,
                     review_attempt_seq: int | None = None) -> Event:
        def build():
            view, item = self._item(task_id, item_id)
            self._validate_review_owner(task_id, item, review_attempt_seq)
            if item["status"] != "submitted" or item["submission"]["seq"] != submission_seq:
                raise WorkConflict("review deferral must target the current submission")
            if response_repair and any(e.payload.get("response_repair") and e.payload.get("submission_seq") == submission_seq
                    for e in self.log.iter_events(task_id=task_id, event_types=("work.review_deferred",))):
                raise WorkConflict("这份成果的复核回复已自动纠正过一次，不再自动重复调用。")
            return "work.review_deferred", {"plan_seq": view["plan_seq"], "work_item_id": item_id,
                                            "response_repair": response_repair,
                                            "submission_seq": submission_seq, "reason": _text(reason, "reason", 2000)}, Actor.system("work_scheduler")
        return self._write(task_id, build)

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
        from .artifacts import content_metrics

        ids = set(submission["artifact_seqs"])
        return [{**event.payload, "seq": event.seq,
                 "content_metrics": content_metrics(event.payload["content"])
                 if isinstance(event.payload.get("content"), str) else {}} for event in self.log.iter_events(
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

    def resubmit_candidate(self, task_id: str, claim: Event) -> Event | None:
        """Reuse bytes, never previous acceptance or local verification receipts."""
        for _ in range(8):
            head = self.log.head_hash()
            view, item = self._item(task_id, claim.payload["work_item_id"])
            if (item["status"] != "running" or view["plan_seq"] != claim.payload["plan_seq"]
                    or item["attempt_id"] != claim.payload["attempt_id"]):
                raise WorkConflict("reuse claim is expired or stale")
            candidate = item.get("reuse_candidate")
            if not candidate:
                return None
            events = self.log.read(task_id=task_id)
            claims = [e for e in events if e.type == "work.claimed" and e.payload["plan_seq"] == view["plan_seq"]
                      and e.payload["work_item_id"] == item["id"]]
            # A rejected candidate must not be submitted again on every retry.
            if len(claims) != 1:
                return None
            old = next((e for e in events if e.seq == candidate["submission_seq"] and e.type == "work.submitted"), None)
            if old is None or old.payload["work_item_id"] != item["id"] or old.seq >= claim.seq:
                return None
            submissions = {e.payload["digest"]: e.payload for e in events if e.type == "work.submitted"}
            artifacts = {e.seq: e for e in events if e.type == "deliverable.published"}

            def signature(submission):
                if submission is None or any(seq not in artifacts for seq in submission["artifact_seqs"]):
                    return None
                return [(submission["hashes"][str(seq)], artifacts[seq].payload.get("filename"),
                         artifacts[seq].payload.get("format"), artifacts[seq].payload.get("kind"))
                        for seq in submission["artifact_seqs"]]

            old_inputs = old.payload["inputs"]
            if set(old_inputs) != set(item["inputs"]):
                return None
            for dep, previous in old_inputs.items():
                prior, current = signature(submissions.get(previous)), signature(submissions.get(item["inputs"][dep]))
                if prior is None or current is None or prior != current:
                    return None
            for seq in old.payload["artifact_seqs"]:
                artifact = artifacts.get(seq)
                if (artifact is None or artifact.actor.id != item["owner"]
                        or artifact.payload.get("work_item_id") != item["id"]
                        or not isinstance(artifact.payload.get("content"), str)
                        or hashlib.sha256(artifact.payload["content"].encode("utf-8")).hexdigest()
                        != old.payload["hashes"].get(str(seq))):
                    return None
            if not old.payload["artifact_seqs"]:
                return None
            payload = {"plan_seq": view["plan_seq"], "work_item_id": item["id"],
                       "attempt_id": item["attempt_id"], "artifact_seqs": old.payload["artifact_seqs"],
                       "hashes": old.payload["hashes"], "inputs": item["inputs"], "scope": view["scope"],
                       "reused_from": old.seq}
            payload["digest"] = digest(payload)
            try:
                return self.log.emit(type="work.submitted", task_id=task_id, actor=Actor.system("work_scheduler"),
                                     payload=payload, expected_head=head)
            except HeadChanged:
                continue
        raise WorkConflict("work state changed during candidate reuse")

    def fail(self, task_id: str, claim: Event, reason: str, *, uncertain: bool) -> Event:
        def build():
            view, item = self._item(task_id, claim.payload["work_item_id"])
            if (view["plan_seq"] != claim.payload["plan_seq"] or item["attempt_id"] != claim.payload["attempt_id"]
                    or not item.get("execution_in_flight")):
                raise WorkConflict("cannot fail a different attempt")
            return "work.failed", {"plan_seq": view["plan_seq"], "work_item_id": item["id"],
                                   "attempt_id": item["attempt_id"], "reason": reason, "uncertain": uncertain}, Actor.system("work_scheduler")
        return self._write(task_id, build)

    @staticmethod
    def _receipt_passed(receipt: Event) -> bool:
        p = receipt.payload
        return (p.get("outcome") == "passed" and type(p.get("exit_code")) is int and p["exit_code"] == 0
                and p.get("artifacts_unchanged") is True
                and all(isinstance(p.get(s), dict) and p[s].get("complete") is True for s in ("stdout", "stderr")))

    @staticmethod
    def _check_binding(event: Event, item: dict[str, Any], *, started: bool = False) -> bool:
        submission, p = item.get("submission"), event.payload
        if (not submission or event.actor != Actor.system("local_verifier") or event.seq <= submission["seq"]
                or p.get("work_item_id") != item["id"] or p.get("submission_digest") != submission["digest"]):
            return False
        for field, expected in (("plan_seq", submission["plan_seq"]), ("submission_seq", submission["seq"])):
            if (started or field in p) and (type(p.get(field)) is not int or p[field] != expected):
                return False
        return (not started or p.get("scope") == submission["scope"]) and (
            "scope" not in p or p["scope"] == submission["scope"])

    def _check_history(self, events: list[Event], item: dict[str, Any]) -> dict[str, Any]:
        states = {c["id"]: {"wave": None, "starts": [], "receipts": {}, "legacy": []}
                  for c in item["criteria"] if c["kind"] == "local_check"}
        valid_starts, valid_receipts, attempt_ids = set(), set(), set()
        for event in events:
            if event.type not in {"work.check_started", "verification.completed"}:
                continue
            p = event.payload
            if not self._check_binding(event, item, started=event.type == "work.check_started"):
                continue
            criterion_id = p.get("criterion_id")
            if not isinstance(criterion_id, str) or criterion_id not in states:
                continue
            state = states[criterion_id]
            if event.type == "work.check_started":
                attempt, wave, lease = p.get("check_attempt"), p.get("check_wave"), p.get("lease_until")
                supersedes, legacy = p.get("supersedes"), p.get("supersedes_receipt_seqs")
                if (not isinstance(attempt, str) or not attempt or attempt in attempt_ids
                        or not isinstance(wave, str) or not wave or type(lease) not in (int, float)
                        or not math.isfinite(lease) or not isinstance(supersedes, list)
                        or not isinstance(legacy, list) or any(type(seq) is not int for seq in supersedes + legacy)):
                    continue
                pending = any(e.seq not in state["receipts"] for e in state["starts"])
                if pending:
                    if wave != state["wave"] or supersedes or legacy:
                        continue
                else:
                    if (wave == state["wave"] or supersedes != [e.seq for e in state["starts"]]
                            or legacy != [e.seq for e in state["legacy"]]):
                        continue
                    state.update(wave=wave, starts=[], receipts={}, legacy=[])
                state["starts"].append(event)
                attempt_ids.add(attempt)
                valid_starts.add(event.seq)
            elif "check_attempt_seq" in p:
                seq = p["check_attempt_seq"]
                if type(seq) is not int or seq in state["receipts"]:
                    continue
                start = next((e for e in state["starts"] if e.seq == seq), None)
                if (start is None or event.seq <= start.seq or type(p.get("submission_seq")) is not int
                        or p["submission_seq"] != item["submission"]["seq"]
                        or ("check_attempt" in p and p["check_attempt"] != start.payload["check_attempt"])):
                    continue
                state["receipts"][seq] = event
                valid_receipts.add(event.seq)
            elif state["wave"] is None:
                # Legacy receipts are accepted only until this criterion adopts starts.
                state["legacy"].append(event)
                valid_receipts.add(event.seq)
        return {"criteria": states, "valid_starts": valid_starts, "valid_receipts": valid_receipts}

    def _selected_checks(self, states: dict[str, Any]) -> dict[str, Event]:
        selected = {}
        for criterion_id, state in states.items():
            if any(e.seq not in state["receipts"] for e in state["starts"]):
                continue
            receipts = list(state["receipts"].values())
            if receipts:
                failures = [e for e in receipts if not self._receipt_passed(e)]
                selected[criterion_id] = min(failures, key=lambda e: e.seq) if failures else max(receipts, key=lambda e: e.seq)
            elif state["legacy"]:
                selected[criterion_id] = state["legacy"][-1]
        return selected

    def checks(self, task_id: str, item: dict[str, Any]) -> dict[str, Event]:
        view = self.view(task_id)
        current = next((i for i in view["items"] if i["id"] == item["id"]), None)
        if (not view.get("scope_current") or current is None or not current["submission"]
                or current.get("dependency_check_stale") or current["submission"] != item.get("submission")):
            return {}
        return self._selected_checks(self._check_history(self.log.read(task_id=task_id), item)["criteria"])

    def start_check(self, task_id: str, item_id: str, criterion_id: str, *, lease_seconds: float = 600,
                    expected_submission_seq: int | None = None) -> Event:
        """Join overlapping checks; supersede a wave only after every attempt ended."""
        if type(lease_seconds) not in (int, float) or not math.isfinite(lease_seconds) or not 0 < lease_seconds <= 3600:
            raise ValueError("check lease must be between 0 and 3600 seconds")

        def build():
            view, item = self._item(task_id, item_id)
            if expected_submission_seq is not None and (
                    type(expected_submission_seq) is not int or not item["submission"]
                    or item["submission"]["seq"] != expected_submission_seq):
                raise WorkConflict("check target submission changed")
            if (not view.get("scope_current") or not item["submission"]
                    or item["status"] not in {"submitted", "accepted", "ready"}
                    or item.get("execution_in_flight") or item.get("check_uncertain")
                    or item.get("review_in_flight")
                    or item.get("dependency_check_stale")
                    or any(i["status"] != "accepted" for i in view["items"] if i["id"] in item["depends_on"])):
                raise WorkConflict("checking requires a current submission with reconciled effects and accepted inputs")
            if not any(c["id"] == criterion_id and c["kind"] == "local_check" for c in item["criteria"]):
                raise ValueError("criterion must identify a local_check in this work item")
            state = self._check_history(self.log.read(task_id=task_id), item)["criteria"][criterion_id]
            pending = any(e.seq not in state["receipts"] for e in state["starts"])
            submission = item["submission"]
            return "work.check_started", {
                "plan_seq": view["plan_seq"], "scope": view["scope"], "work_item_id": item_id,
                "criterion_id": criterion_id, "submission_seq": submission["seq"],
                "submission_digest": submission["digest"], "check_attempt": uuid.uuid4().hex,
                "check_wave": state["wave"] if pending else uuid.uuid4().hex,
                "lease_until": time.time() + lease_seconds,
                "supersedes": [] if pending else [e.seq for e in state["starts"]],
                "supersedes_receipt_seqs": [] if pending else [e.seq for e in state["legacy"]],
            }, Actor.system("local_verifier")

        return self._write(task_id, build)

    @staticmethod
    def _review_history(events: list[Event], plan_seq: int, item_id: str) -> dict[str, Any]:
        starts, finishes, reconciled, terminal_seqs = {}, {}, {}, set()
        plan = next((e for e in events if e.seq == plan_seq and e.type == "work.plan_created"), None)
        contract = next((i for i in plan.payload["items"] if i["id"] == item_id), None) if plan else None
        submissions = {e.seq: e for e in events if e.type == "work.submitted"}
        binding = ("plan_seq", "work_item_id", "submission_seq", "submission_digest", "scope", "check_seqs")
        for event in events:
            p = event.payload
            if not contract or p.get("plan_seq") != plan_seq or p.get("work_item_id") != item_id:
                continue
            if event.type == "work.review_started":
                submission_seq = p.get("submission_seq")
                submission = submissions.get(submission_seq) if type(submission_seq) is int else None
                lease, checks = p.get("lease_until"), p.get("check_seqs")
                if (not submission or event.seq <= submission.seq
                        or submission.payload.get("plan_seq") != plan_seq
                        or submission.payload.get("work_item_id") != item_id
                        or p.get("submission_digest") != submission.payload.get("digest")
                        or p.get("scope") != submission.payload.get("scope")
                        or not contract["reviewer"] or contract["reviewer"] == contract["owner"]
                        or event.actor != Actor.agent(contract["reviewer"], "reviewer")
                        or type(lease) not in (int, float) or not math.isfinite(lease)
                        or not isinstance(checks, dict)
                        or any(not isinstance(k, str) or type(v) is not int for k, v in checks.items())):
                    continue
                starts[event.seq] = event
            elif event.type in {"work.review_finished", "work.review_reconciled"}:
                seq = p.get("attempt_seq")
                start = starts.get(seq) if type(seq) is int else None
                if (not start or event.seq <= start.seq
                        or any(p.get(k) != start.payload.get(k) for k in binding)):
                    continue
                if event.type == "work.review_finished":
                    if (event.actor != start.actor or seq in finishes or seq in reconciled
                            or p.get("status") not in {"completed", "not_invoked", "uncertain", "stale", "invalid_response", "recoverable"}
                            or (p["status"] == "recoverable" and p.get("recovery") not in {
                                "text_request_rejected", "text_transport_failure"})):
                        continue
                    finishes[seq] = event
                else:
                    if (event.actor != Actor.human() or seq in reconciled
                            or (seq in finishes and finishes[seq].payload["status"] != "uncertain")
                            or p.get("invocation_stopped") is not True or not p.get("reason")):
                        continue
                    reconciled[seq] = event
                terminal_seqs.add(event.seq)
        return {"starts": list(starts.values()), "finishes": finishes, "reconciled": reconciled,
                "terminal_seqs": terminal_seqs,
                "pending": [e for seq, e in starts.items() if seq not in reconciled
                            and (seq not in finishes or finishes[seq].payload["status"] == "uncertain")]}

    def start_review(self, task_id: str, item_id: str, *, reviewer: str, submission_seq: int,
                     expected_check_seqs: dict[str, int], expected_review_seq: int | None = None,
                     expected_review_attempt_seq: int | None = None, lease_seconds: float = 600) -> Event:
        """Reserve the exact prompt inputs before invoking a reviewer, using event-head CAS."""
        if type(lease_seconds) not in (int, float) or not math.isfinite(lease_seconds) or not 0 < lease_seconds <= 3600:
            raise ValueError("review lease must be between 0 and 3600 seconds")

        def build():
            view, item = self._item(task_id, item_id)
            if not reviewer or reviewer != item["reviewer"] or reviewer == item["owner"]:
                raise ValueError("only the assigned independent reviewer may review")
            if (not view.get("scope_current") or item["status"] != "submitted" or item["in_flight"]
                    or type(submission_seq) is not int or item["submission"]["seq"] != submission_seq
                    or item.get("review_seq") != expected_review_seq
                    or item["review_lifecycle"].get("attempt_seq") != expected_review_attempt_seq
                    or any(i["status"] != "accepted" for i in view["items"] if i["id"] in item["depends_on"])):
                raise WorkConflict("复核已被认领、输入已变化或调用尚未核对结束。")
            checks = {k: v.seq for k, v in self.checks(task_id, item).items()}
            if checks != expected_check_seqs:
                raise WorkConflict("复核的检查证据已经变化，请重新读取当前版本。")
            return "work.review_started", {
                "plan_seq": view["plan_seq"], "work_item_id": item_id, "scope": view["scope"],
                "submission_seq": submission_seq, "submission_digest": item["submission"]["digest"],
                "check_seqs": checks, "base_review_seq": expected_review_seq,
                "lease_until": time.time() + lease_seconds,
            }, Actor.agent(reviewer, "reviewer")
        return self._write(task_id, build)

    def _review_attempt(self, task_id: str, attempt_seq: int) -> tuple[Event, dict[str, Any]]:
        events = self.log.read(task_id=task_id)
        started = next((e for e in events if type(attempt_seq) is int and e.seq == attempt_seq
                        and e.type == "work.review_started"), None)
        if started is None:
            raise WorkConflict("复核认领不存在或不属于当前任务。")
        state = self._review_history(events, started.payload["plan_seq"], started.payload["work_item_id"])
        if started not in state["starts"]:
            raise WorkConflict("复核认领的身份或版本绑定无效。")
        return started, state

    def _review_inputs_current(self, task_id: str, started: Event) -> bool:
        view, item = self._item(task_id, started.payload["work_item_id"])
        p, submission = started.payload, item["submission"]
        return bool(view.get("scope_current") and view["plan_seq"] == p["plan_seq"] and submission
                    and submission["seq"] == p["submission_seq"] and submission["digest"] == p["submission_digest"]
                    and self.scope(task_id) == p["scope"] and not item["check_in_flight"]
                    and not item.get("dependency_check_stale") and not item.get("dependency_uncertain")
                    and all(i["status"] == "accepted" for i in view["items"] if i["id"] in item["depends_on"])
                    and {k: v.seq for k, v in self.checks(task_id, item).items()} == p["check_seqs"])

    def _validate_review_owner(self, task_id: str, item: dict[str, Any], attempt_seq: int | None) -> None:
        if attempt_seq is None:
            if item.get("review_in_flight"):
                raise WorkConflict("阶段复核仍在途或结果不明，人工验收前需要先核对调用。")
            return
        started, state = self._review_attempt(task_id, attempt_seq)
        if (started.payload["work_item_id"] != item["id"] or started not in state["pending"]
                or attempt_seq in state["finishes"] or not self._review_inputs_current(task_id, started)):
            raise WorkConflict("阶段复核认领已结束或证据版本已失效。")

    def finish_review(self, task_id: str, attempt_seq: int, *, status: str, reason: str,
                      recovery: str | None = None) -> Event:
        if status not in {"completed", "not_invoked", "uncertain", "stale", "invalid_response", "recoverable"}:
            raise ValueError("invalid review completion status")
        if ((status == "recoverable" and recovery not in {"text_request_rejected", "text_transport_failure"})
                or (status != "recoverable" and recovery is not None)):
            raise ValueError("recoverable review requires a classified gateway recovery reason")
        reason = _text(reason, "review completion reason", 2000)

        def build():
            started, state = self._review_attempt(task_id, attempt_seq)
            if attempt_seq in state["finishes"] or attempt_seq in state["reconciled"]:
                raise WorkConflict("复核认领已经结束，迟到响应不能覆盖既有状态。")
            result = status
            # Unknown remote effects remain locked even when their inputs are stale.
            try:
                current = self._review_inputs_current(task_id, started)
            except KeyError:
                current = False
            if result != "uncertain" and not current:
                result = "stale"
            elif result == "completed" and not any(
                    e.type == "work.reviewed" and e.actor == started.actor
                    and e.payload.get("review_attempt_seq") == attempt_seq
                    for e in self.log.read(task_id=task_id)):
                result = "invalid_response"
            return "work.review_finished", {**started.payload, "attempt_seq": attempt_seq,
                **({"recovery": recovery} if recovery is not None else {}),
                "status": result, "reason": "复核期间输入版本发生变化，旧结论不再放行。" if result == "stale" else reason}, started.actor
        return self._write(task_id, build)

    def reconcile_review(self, task_id: str, attempt_seq: int, *, reason: str, invocation_stopped: bool) -> Event:
        """Trusted operator attestation, not cancellation: reconcile effects before retrying.

        A lease expiry or a resume command alone never authorizes another call.
        Discard any partial judgments and fence all late writes from this attempt.
        """
        if invocation_stopped is not True:
            raise ValueError("confirm the invocation has stopped and its effects were reconciled")
        reason = _text(reason, "review reconciliation evidence", 2000)

        def build():
            started, state = self._review_attempt(task_id, attempt_seq)
            if started not in state["pending"]:
                raise WorkConflict("这次复核没有待核对的调用。")
            return "work.review_reconciled", {**started.payload, "attempt_seq": attempt_seq,
                "invocation_stopped": True, "reason": reason}, Actor.human()
        return self._write(task_id, build)

    def review(self, task_id: str, item_id: str, *, reviewer: str, submission_seq: int,
               results: object, review_attempt_seq: int | None = None) -> Event:
        def build():
            view, item = self._item(task_id, item_id)
            if reviewer != item["reviewer"] or reviewer == item["owner"]:
                raise ValueError("only the assigned independent reviewer may review")
            self._validate_review_owner(task_id, item, review_attempt_seq)
            if (item["status"] not in ({"submitted", "uncertain"} if review_attempt_seq is not None else {"submitted"})
                    or not item["submission"] or item["submission"]["seq"] != submission_seq):
                raise WorkConflict("review must target the current submitted artifact")
            artifacts = {a["seq"]: a for a in self.artifacts(task_id, item["submission"])}
            raw_results = results if isinstance(results, list) else []
            checks = []
            local = self.checks(task_id, item)
            for criterion in item["criteria"]:
                check = {"id": criterion["id"], "kind": criterion["kind"], "status": "unknown",
                         "reason": "缺少可核对的验收依据。", "source": "runtime_check"}
                receipt = local.get(criterion["id"]) if criterion["kind"] == "local_check" else None
                judgment, issue, row = assessment(raw_results, criterion["id"], artifacts,
                                                  {**receipt.payload, "seq": receipt.seq} if receipt else None)
                if issue:
                    check.update(reason=ISSUES[issue], response_issue=issue)
                if criterion["kind"] == "local_check":
                    if receipt:
                        passed = self._receipt_passed(receipt)
                        check.update(execution_status="passed" if passed else "failed", receipt_seq=receipt.seq,
                                     reason=(ISSUES[issue] if issue else "命令已通过，但尚未独立核对它是否足以验证这项标准。") if passed
                                     else "实际检查未通过，成员评价不能覆盖执行结果。")
                        if not passed:
                            check.pop("response_issue", None)
                            if receipt.payload.get("outcome") == "unavailable":
                                check.update(status="unknown", execution_status="unavailable",
                                             reason="检查工具未产生有效执行结论，需要恢复工具或补充检查；不据此判定成果错误。")
                            elif receipt.payload.get("check_kind") == "browser":
                                check.update(status="unknown", reason="浏览器检查失败，尚未独立判定是成果问题还是测试方案问题。")
                                if (judgment and judgment["status"] in {"failed", "unknown"}
                                        and type(row.get("receipt_seq")) is int and row["receipt_seq"] == receipt.seq):
                                    check.update(judgment, source="model_assessment")
                                else:
                                    if issue:
                                        check.update(response_issue=issue)
                                    elif judgment and (type(row.get("receipt_seq")) is not int or row["receipt_seq"] != receipt.seq):
                                        check.update(response_issue="invalid_receipt")
                                    report = receipt.payload.get("browser_report")
                                    if isinstance(report, dict):
                                        steps = report.get("steps", [])
                                        failed_step = next((s for s in steps if isinstance(s, dict) and s.get("status") == "failed"), None) if isinstance(steps, list) else None
                                        detail = (str(failed_step.get("label", "")) + "：" + str(failed_step.get("error", ""))
                                                  if failed_step else str(report.get("error", "")))
                                        if detail:
                                            check["reason"] += "实际失败：" + detail[:1200]
                            else:
                                check["status"] = "failed"
                        # A successful process is necessary, not sufficient evidence
                        # that the command actually tests this acceptance criterion.
                        elif judgment and type(row.get("receipt_seq")) is int and row["receipt_seq"] == receipt.seq:
                            check.update(judgment, source="model_assessment")
                        elif judgment and judgment["status"] == "unknown":
                            check.update(judgment, source="model_assessment")
                        elif judgment:
                            check.update(reason=ISSUES["invalid_receipt"], response_issue="invalid_receipt")
                    else:
                        check.pop("response_issue", None)
                        check["reason"] = "尚未取得本次成果的完整实际检查回执，不能用成员声明代替。"
                elif judgment:
                    check.update(judgment, source="model_assessment")
                checks.append(check)
            status = "accepted" if all(c["status"] == "passed" for c in checks) else (
                "rework" if any(c["status"] == "failed" for c in checks) else "submitted")
            reason = "阶段标准已通过；不代表整件任务已经交付。" if status == "accepted" else (
                "阶段验收未通过，需要返工。" if status == "rework" else "证据不足，保留待验收状态。")
            return "work.reviewed", {"plan_seq": view["plan_seq"], "work_item_id": item_id,
                                     "submission_seq": submission_seq, "status": status,
                                     **({"review_attempt_seq": review_attempt_seq} if review_attempt_seq is not None else {}),
                                     "checks": checks, "reason": reason}, Actor.agent(reviewer, "reviewer")
        return self._write(task_id, build)

    def rework(self, task_id: str, item_id: str, reason: str, *, expected_plan_seq: int | None = None,
               expected_scope: dict[str, Any] | None = None) -> Event:
        reason = _text(reason, "rework reason", 2000)

        def build():
            view, item = self._item(task_id, item_id)
            if ((expected_plan_seq is not None and view["plan_seq"] != expected_plan_seq)
                    or (expected_scope is not None and self.scope(task_id) != expected_scope)):
                raise WorkConflict("rework target plan or scope changed")
            affected = {item_id}
            for _ in view["items"]:
                affected.update(i["id"] for i in view["items"] if set(i["depends_on"]) & affected)
            if any(i.get("in_flight") for i in view["items"] if i["id"] in affected):
                raise WorkConflict("in-flight effects must be reconciled before rework")
            return "work.invalidated", {"plan_seq": view["plan_seq"], "work_item_id": item["id"],
                                        "affected": sorted(affected), "reason": reason}, Actor.system("work_scheduler")
        return self._write(task_id, build)
