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
    def __init__(self, log: EventLog, rules: RuleBook):
        self.log, self.rules = log, rules

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
            events = list(self.log.iter_events(task_id=task_id, event_types=(
                "delivery.check_started", "verification.completed", "delivery.review_started", "delivery.review_finished")))
            for started in events:
                if started.type == "delivery.check_started" and started.actor == Actor.system("local_verifier"):
                    ended = any(e.type == "verification.completed" and e.actor == started.actor and e.seq > started.seq
                        and e.payload.get("check_attempt_seq") == started.seq
                        and e.payload.get("delivery_digest") == started.payload["delivery_digest"]
                        and e.payload.get("criterion_id") == started.payload["criterion_id"] for e in events)
                    if not ended:
                        raise WorkConflict("整体检查尚未核对结束，不能修订检查义务。")
                elif started.type == "delivery.review_started" and started.actor.role == "delivery_reviewer":
                    ended = next((e for e in events if e.type == "delivery.review_finished" and e.actor == started.actor
                        and e.seq > started.seq and e.payload.get("attempt_seq") == started.seq
                        and e.payload.get("delivery_digest") == started.payload["delivery_digest"]), None)
                    if ended is None or ended.payload.get("status") == "uncertain":
                        raise WorkConflict("整体复核尚未核对结束，不能修订检查义务。")
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
        histories = {}

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
                dependent.update(status="uncertain" if uncertain else "stale",
                                 submission=dependent["submission"] if checking else None, checks=[],
                                 dependency_check_stale=checking,
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
                elif item.get("dependency_check_stale"):
                    item.update(status="stale", submission=None, checks=[], dependency_check_stale=False,
                                reason="前置成果重新检查，旧提交已失效。")
            item["in_flight"] = item["execution_in_flight"] or item["check_in_flight"]
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
                "planning": plan.payload.get("planning"), "check_contract": self.check_contract(task_id),
                "scope_current": scope_current, "items": list(items.values()),
                "customer_directions": (plan.payload["customer_directions"] if "customer_directions" in plan.payload
                                        else self.directions(task_id, plan.payload["scope"]["direction_seq"])),
                "stage_accepted": all(i["status"] == "accepted" for i in items.values())}

    def defer_review(self, task_id: str, item_id: str, submission_seq: int, reason: str) -> Event:
        def build():
            view, item = self._item(task_id, item_id)
            if item["status"] != "submitted" or item["submission"]["seq"] != submission_seq:
                raise WorkConflict("review deferral must target the current submission")
            return "work.review_deferred", {"plan_seq": view["plan_seq"], "work_item_id": item_id,
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
                matches = [r for r in raw_results if isinstance(r, dict) and r.get("id") == criterion["id"]]
                assessment = None
                if len(matches) == 1:
                    r = matches[0]
                    status, quote, seq = r.get("status"), r.get("quote"), r.get("artifact_seq")
                    reason = r.get("reason")
                    if (status in ("passed", "failed", "unknown") and isinstance(reason, str) and reason.strip()
                            and len(reason) <= 2000 and type(seq) is int and seq in artifacts
                            and isinstance(quote, str) and quote.strip() and len(quote) <= 2000
                            and quote in artifacts[seq]["content"]):
                        assessment = {"status": status, "reason": reason, "quote": quote, "artifact_seq": seq}
                if criterion["kind"] == "local_check":
                    receipt = local.get(criterion["id"])
                    if receipt:
                        passed = self._receipt_passed(receipt)
                        check.update(execution_status="passed" if passed else "failed", receipt_seq=receipt.seq,
                                     reason="命令已通过，但尚未独立核对它是否足以验证这项标准。" if passed
                                     else "实际检查未通过，成员评价不能覆盖执行结果。")
                        if not passed:
                            check["status"] = "failed"
                        # A successful process is necessary, not sufficient evidence
                        # that the command actually tests this acceptance criterion.
                        elif (assessment and type(matches[0].get("receipt_seq")) is int
                              and matches[0]["receipt_seq"] == receipt.seq):
                            check.update(assessment)
                elif assessment:
                    check.update(assessment)
                checks.append(check)
            status = "accepted" if all(c["status"] == "passed" for c in checks) else (
                "rework" if any(c["status"] == "failed" for c in checks) else "submitted")
            reason = "阶段标准已通过；不代表整件任务已经交付。" if status == "accepted" else (
                "阶段验收未通过，需要返工。" if status == "rework" else "证据不足，保留待验收状态。")
            return "work.reviewed", {"plan_seq": view["plan_seq"], "work_item_id": item_id,
                                     "submission_seq": submission_seq, "status": status,
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
