"""Versioned working requirements, scoped by explicit project and task identity.

These records guide model context. They do not implement a filesystem sandbox.
Only trusted application calls can adopt or change a rule; model suggestions
remain candidates, with their original message as provenance.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Callable

from .events import Actor, Event, EventLog, HeadChanged

RULE_EVENTS = ("rule.recorded", "rule.updated")
MAX_SCOPE_CHARS = 6000
MAX_SCOPE_RULES = 32


class RuleConflict(ValueError):
    pass


def required_text(value: object, label: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
        raise ValueError(f"{label} requires 1 to {limit} characters")
    return value.strip()


class RuleBook:
    def __init__(self, log: EventLog):
        self.log = log

    def _append(self, build: Callable[[], dict[str, Any]]) -> Event:
        for _ in range(5):
            head = self.log.head_hash()
            args = build()
            try:
                return self.log.emit(**args, expected_head=head)
            except HeadChanged:
                continue
        raise RuleConflict("rules changed concurrently; refresh and retry")

    def projects(self) -> list[dict[str, Any]]:
        return [{**event.payload, "seq": event.seq} for event in self.log.iter_events(event_types=("project.created",))]

    def create_project(self, name: str) -> dict[str, Any]:
        name = required_text(name, "project name", 120)
        payload = {"id": "project_" + uuid.uuid4().hex, "name": name}
        event = self.log.emit(actor=Actor.human(), type="project.created", payload=payload)
        return {**payload, "seq": event.seq}

    def require_project(self, project_id: object) -> str:
        if not isinstance(project_id, str) or not any(item["id"] == project_id for item in self.projects()):
            raise ValueError("unknown project")
        return project_id

    def task_projects(self) -> dict[str, str | None]:
        result = {}
        for event in self.log.iter_events(event_types=("task.created", "task.project_bound")):
            if event.task_id:
                result[event.task_id] = event.payload.get("project_id")
        return result

    def task_project(self, task_id: object) -> str | None:
        projects = self.task_projects()
        if not isinstance(task_id, str) or task_id not in projects:
            raise ValueError("unknown task")
        return projects[task_id]

    def bind_project(self, task_id: str, project_id: str) -> Event:
        def build():
            self.require_project(project_id)
            current = self.task_project(task_id)
            if current is not None:
                raise RuleConflict("task already belongs to a project")
            return dict(actor=Actor.human(), type="task.project_bound", task_id=task_id,
                        payload={"project_id": project_id})
        return self._append(build)

    def all_rules(self) -> list[dict[str, Any]]:
        current = {}
        for event in self.log.iter_events(event_types=RULE_EVENTS):
            current[event.payload["id"]] = {**event.payload, "seq": event.seq, "updated_at": event.ts}
        return list(current.values())

    def view(self, *, task_id: str | None = None, project_id: str | None = None) -> dict[str, Any]:
        if task_id is not None:
            bound = self.task_project(task_id)
            if project_id is not None and bound != project_id:
                raise ValueError("project does not match task")
            project_id = bound
        elif project_id is not None:
            self.require_project(project_id)
        rules = [item for item in self.all_rules() if (
            item["scope"] == "workspace"
            or item["scope"] == "project" and project_id is not None and item["target_id"] == project_id
            or item["scope"] == "task" and task_id is not None and item["target_id"] == task_id
        )]
        return {"project_id": project_id, "rules": rules, "enforcement": "guidance_only"}

    def _target(self, scope: str, task_id: str | None, project_id: str | None) -> str | None:
        if task_id is not None:
            bound = self.task_project(task_id)
            if project_id is not None and bound != project_id:
                raise ValueError("project does not match task")
        else:
            bound = None
        if scope == "workspace":
            return None
        if scope == "task" and task_id is not None:
            return task_id
        if scope == "project":
            return self.require_project(bound if task_id is not None else project_id)
        raise ValueError("rule scope requires workspace, project, or an existing task")

    def _check_capacity(self, rule: dict[str, Any]) -> None:
        peers = [item for item in self.all_rules() if item["scope"] == rule["scope"]
                 and item["target_id"] == rule["target_id"] and item["id"] != rule["id"]]
        if rule["status"] != "retired" and any(item["text"] == rule["text"] and item["status"] != "retired" for item in peers):
            raise RuleConflict("an equivalent rule already exists in this scope")
        if rule["status"] == "active":
            active = [item for item in peers if item["status"] == "active"] + [rule]
            if len(active) > MAX_SCOPE_RULES or sum(len(item["text"]) for item in active) > MAX_SCOPE_CHARS:
                raise ValueError("active rules exceed scope context capacity; consolidate first")
        elif rule["status"] == "candidate" and sum(item["status"] == "candidate" for item in peers) >= 64:
            raise ValueError("too many candidate rules in this scope")

    def record(self, text: str, *, scope: str, task_id: str | None = None,
               project_id: str | None = None, proposal_event: Event | None = None,
               rationale: str = "") -> dict[str, Any]:
        text = required_text(text, "rule text", 2000)
        rationale = required_text(rationale, "rule rationale", 1000) if rationale else ""
        rule_id = "rule_" + uuid.uuid4().hex

        def build():
            target = self._target(scope, task_id, project_id)
            if proposal_event is not None:
                if (proposal_event.task_id != task_id or proposal_event.actor.kind != "agent"
                        or proposal_event.type not in {"msg.said", "msg.challenge"}
                        or scope not in {"project", "task"}):
                    raise ValueError("invalid rule proposal source")
                stored = next((event for event in self.log.iter_events(task_id=task_id)
                               if event.seq == proposal_event.seq), None)
                if stored != proposal_event or not rationale:
                    raise ValueError("rule proposal requires a stored message and rationale")
            payload = {
                "id": rule_id, "revision": 1, "scope": scope, "target_id": target,
                "text": text, "status": "candidate" if proposal_event else "active",
                "enforcement": "guidance_only", "rationale": rationale,
                "source": {"kind": "agent_report" if proposal_event else "human",
                           "actor": proposal_event.actor.id if proposal_event else "human",
                           "event_seq": proposal_event.seq if proposal_event else None},
            }
            self._check_capacity(payload)
            return dict(actor=proposal_event.actor if proposal_event else Actor.human(),
                        type="rule.recorded", task_id=task_id, payload=payload)
        event = self._append(build)
        return {**event.payload, "seq": event.seq, "updated_at": event.ts}

    def update(self, rule_id: str, *, revision: int, text: str, status: str,
               task_id: str | None = None) -> dict[str, Any]:
        if type(revision) is not int or revision < 1:
            raise ValueError("rule revision must be a positive integer")
        text = required_text(text, "rule text", 2000)
        if not isinstance(status, str) or status not in {"active", "candidate", "retired"}:
            raise ValueError("invalid rule status")

        def build():
            current = next((item for item in self.all_rules() if item["id"] == rule_id), None)
            if current is None:
                raise ValueError("unknown rule")
            if current["revision"] != revision:
                raise RuleConflict("rule version changed; refresh before editing")
            if task_id is not None and rule_id not in {item["id"] for item in self.view(task_id=task_id)["rules"]}:
                raise ValueError("rule does not apply to task")
            payload = {key: value for key, value in current.items() if key not in {"seq", "updated_at"}}
            payload.update(text=text, status=status, revision=revision + 1,
                           previous_seq=current["seq"], changed_by="human")
            self._check_capacity(payload)
            return dict(actor=Actor.human(), type="rule.updated", task_id=task_id, payload=payload)
        event = self._append(build)
        return {**event.payload, "seq": event.seq, "updated_at": event.ts}

    def current_snapshot(self, task_id: str) -> dict[str, Any]:
        view = self.view(task_id=task_id)
        active = [{key: item[key] for key in ("id", "revision", "scope", "text", "seq")}
                  for item in view["rules"] if item["status"] == "active"]
        payload = {"project_id": view["project_id"], "rules": active, "enforcement": "guidance_only"}
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
        return {**payload, "fingerprint": fingerprint}

    def snapshot(self, task_id: str) -> dict[str, Any]:
        # Save the exact, untruncated requirements seen by a call. Deduplicate
        # across calls until the applicable content, versions or project change.
        for _ in range(5):
            head = self.log.head_hash()
            payload = self.current_snapshot(task_id)
            prior = list(self.log.iter_events(task_id=task_id, event_types=("rules.applied",)))
            if self.log.head_hash() != head:
                continue
            if prior and prior[-1].payload.get("fingerprint") == payload["fingerprint"]:
                return {**prior[-1].payload, "seq": prior[-1].seq}
            if not prior and not payload["rules"]:
                return {**payload, "seq": None}
            try:
                event = self.log.emit(actor=Actor.system("rulebook"), type="rules.applied", task_id=task_id,
                                      payload=payload, expected_head=head)
                return {**event.payload, "seq": event.seq}
            except HeadChanged:
                continue
        raise RuleConflict("rules changed concurrently; retry the collaboration")
