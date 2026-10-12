"""Runtime assembly root for Cater."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .drivers import Driver, ScriptedDriver
from .events import Actor, Event, EventLog
from .fold import TaskSnapshot, fold


DEFAULT_SCRIPT = {
    "product": [
        json.dumps(
            {
                "stance": "assert",
                "option_id": "A",
                "claim": "Ship the smallest user-visible loop first",
                "evidence": ["acceptance criteria matter more than a broad architecture"],
                "confidence": 0.78,
                "text": "I suggest shipping the smallest loop people can inspect and decide on.",
            },
            ensure_ascii=False,
        )
    ],
    "dev": [
        json.dumps(
            {
                "stance": "assert",
                "option_id": "B",
                "claim": "Harden the driver and event boundary first",
                "evidence": ["real CLI integration exposes protocol drift and crashes"],
                "confidence": 0.74,
                "text": "I suggest connecting real drivers before expanding the UI.",
            },
            ensure_ascii=False,
        )
    ],
    "test": [
        json.dumps(
            {
                "stance": "challenge",
                "option_id": "C",
                "claim": "Do not claim collaboration works without replayable evidence",
                "evidence": ["running successfully is not the same as being correct"],
                "confidence": 0.82,
                "text": "I object to declaring consensus before preserving process evidence.",
            },
            ensure_ascii=False,
        )
    ],
}


@dataclass(frozen=True)
class Role:
    id: str
    name: str
    role: str
    driver: str


DEFAULT_ROLES = [
    Role("product", "Product", "product", "product"),
    Role("dev", "Developer", "developer", "dev"),
    Role("test", "Tester", "tester", "test"),
]


class Runtime:
    def __init__(self, db_path: str | Path = ":memory:", drivers: dict[str, Driver] | None = None):
        self.log = EventLog(db_path)
        self.roles = list(DEFAULT_ROLES)
        self.drivers: dict[str, Driver] = drivers or {
            name: ScriptedDriver(name, script)
            for name, script in DEFAULT_SCRIPT.items()
        }

    def close(self) -> None:
        self.log.close()

    def __enter__(self) -> "Runtime":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def create_task(self, goal: str, acceptance: list[str] | None = None) -> str:
        task_id = "task_" + uuid.uuid4().hex[:10]
        self.log.emit(
            actor=Actor.human("human"),
            type="task.created",
            task_id=task_id,
            payload={"goal": goal, "acceptance": list(acceptance or [])},
        )
        self.log.emit(actor=Actor.system("orchestrator"), type="task.started", task_id=task_id)
        return task_id

    def battle(self, task_id: str) -> dict[str, Any]:
        task = self.task(task_id)
        if task is None:
            raise KeyError(task_id)

        utterances = []
        for role in self.roles:
            driver = self.drivers[role.driver]
            prompt = self._prompt(task, role)
            response = driver.complete(
                role=role.role,
                prompt=prompt,
                context={"task_id": task_id, "goal": task.goal},
            )
            parsed = self._parse_response(response.text)
            event_type = "msg.challenge" if parsed.get("stance") == "challenge" else "msg.said"
            payload = {
                "stance": parsed.get("stance", "assert"),
                "option_id": parsed.get("option_id"),
                "claim": parsed.get("claim", ""),
                "evidence": list(parsed.get("evidence", []) or []),
                "confidence": parsed.get("confidence"),
                "text": parsed.get("text", response.text),
                "tokens": response.tokens,
                "driver": role.driver,
            }
            event = self.log.emit(
                actor=Actor.agent(role.id, role.role),
                type=event_type,
                task_id=task_id,
                payload=payload,
            )
            utterances.append({"event": event, "payload": payload, "role": role})

        brief = self._build_brief(task_id, utterances)
        self.log.emit(
            actor=Actor.system("orchestrator"),
            type="escalation.triggered",
            task_id=task_id,
            payload={"reason": "conflicting_options", "options": brief["options"]},
        )
        brief_event = self.log.emit(
            actor=Actor.system("orchestrator"),
            type="brief.generated",
            task_id=task_id,
            payload={"brief": brief},
        )
        return {"task_id": task_id, "brief_event": brief_event.seq, "brief": brief}

    def decide(self, task_id: str, resolution: str, reason: str) -> Event:
        if resolution not in {"done", "rework"}:
            raise ValueError("resolution must be done or rework")
        return self.log.emit(
            actor=Actor.human("human"),
            type="human.decided",
            task_id=task_id,
            payload={"resolution": resolution, "reason": reason},
        )

    def task(self, task_id: str) -> TaskSnapshot | None:
        return fold(self.log.read()).get(task_id)

    def tasks(self) -> list[TaskSnapshot]:
        return list(fold(self.log.read()).values())

    def inbox(self) -> list[dict[str, Any]]:
        items = []
        for task in self.tasks():
            if task.status == "escalated" and task.decision is None:
                items.append(
                    {
                        "task_id": task.id,
                        "kind": "human_decision",
                        "title": task.brief.get("dispute") if task.brief else task.goal,
                        "status": task.status,
                    }
                )
        return items

    def verify_chain(self):
        return self.log.verify_chain()

    def _prompt(self, task: TaskSnapshot, role: Role) -> str:
        return (
            f"You are {role.name}. Goal: {task.goal}\n"
            f"Acceptance: {task.acceptance}\n"
            "Return JSON with stance, option_id, claim, evidence, confidence, text."
        )

    @staticmethod
    def _parse_response(text: str) -> dict[str, Any]:
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return {"stance": "assert", "claim": "", "text": text, "evidence": []}
        return data if isinstance(data, dict) else {"stance": "assert", "text": text}

    @staticmethod
    def _build_brief(task_id: str, utterances: list[dict[str, Any]]) -> dict[str, Any]:
        positions = []
        options: dict[str, dict[str, Any]] = {}
        for item in utterances:
            role: Role = item["role"]
            payload = item["payload"]
            option_id = payload.get("option_id") or role.id
            positions.append(
                {
                    "actor": role.id,
                    "claim": payload.get("claim", ""),
                    "evidence": payload.get("evidence", []),
                    "confidence": payload.get("confidence"),
                    "stance": payload.get("stance"),
                }
            )
            options.setdefault(
                option_id,
                {
                    "id": option_id,
                    "summary": payload.get("claim", ""),
                    "supporters": [],
                    "dissent": [],
                },
            )
            if payload.get("stance") == "challenge":
                options[option_id]["dissent"].append(role.id)
            else:
                options[option_id]["supporters"].append(role.id)

        return {
            "task_id": task_id,
            "dispute": "Agents proposed different next steps; human decision required.",
            "positions": positions,
            "consensus": [],
            "root_cause": "No single option reached consensus in the department battle.",
            "options": list(options.values()),
        }
