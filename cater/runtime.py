"""Runtime assembly root for Cater."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from .cabinet import CabinetPolicy, tally_votes
from .drivers import Driver, DriverResponse, load_drivers
from .events import Actor, Event, EventLog
from .fold import TaskSnapshot, fold


@dataclass(frozen=True)
class Role:
    id: str
    name: str
    role: str
    driver: str


@dataclass(frozen=True)
class RuntimeConfig:
    roles: list[Role]
    cabinet_policy: CabinetPolicy


class Runtime:
    def __init__(
        self,
        db_path: str | Path = ":memory:",
        *,
        drivers: dict[str, Driver] | None = None,
        drivers_path: str | Path | None = None,
        roles_path: str | Path | None = None,
        driver_attempts: int = 2,
        circuit_breaker_threshold: int = 2,
        audit_key: str | bytes | None = None,
        audit_key_id: str = "local",
    ):
        self.log = EventLog(db_path, audit_key=audit_key, key_id=audit_key_id)
        self.driver_attempts = max(1, driver_attempts)
        self.circuit_breaker_threshold = max(1, circuit_breaker_threshold)
        self._driver_failures: dict[str, int] = {}
        config = load_runtime_config(roles_path)
        self.roles = config.roles
        self.cabinet_policy = config.cabinet_policy
        configured_drivers = load_drivers(_pack_path("default_drivers.json"))
        if drivers_path is not None:
            configured_drivers.update(load_drivers(drivers_path))
        if drivers is not None:
            configured_drivers.update(drivers)
        self.drivers = configured_drivers
        missing = sorted({role.driver for role in self.roles} - set(self.drivers))
        if missing:
            raise ValueError(f"missing driver(s) for roles: {', '.join(missing)}")

    def close(self) -> None:
        for driver in self.drivers.values():
            close = getattr(driver, "close", None)
            if callable(close):
                close()
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
            response = self._complete_with_containment(
                task_id=task_id,
                task=task,
                role=role,
                driver=driver,
                prompt=prompt,
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

        brief = self._build_brief(task_id, utterances, self.cabinet_policy)
        self.log.emit(
            actor=Actor.system("cabinet"),
            type="cabinet.voted",
            task_id=task_id,
            payload={"tally": brief["cabinet"]},
        )
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

    def _complete_with_containment(
        self,
        *,
        task_id: str,
        task: TaskSnapshot,
        role: Role,
        driver: Driver,
        prompt: str,
    ) -> DriverResponse:
        if self._is_circuit_open(role.driver):
            self._emit_driver_failure(
                task_id=task_id,
                role=role,
                attempt=0,
                exc=RuntimeError("circuit breaker is open"),
                circuit_open=True,
            )
            return self._contained_driver_failure(role, "circuit breaker is open")

        last_error: Exception | None = None
        context = {"task_id": task_id, "goal": task.goal}
        for attempt in range(1, self.driver_attempts + 1):
            try:
                response = driver.complete(role=role.role, prompt=prompt, context=context)
                self._driver_failures[role.driver] = 0
                return response
            except Exception as exc:
                last_error = exc
                self._driver_failures[role.driver] = self._driver_failures.get(role.driver, 0) + 1
                self._emit_driver_failure(
                    task_id=task_id,
                    role=role,
                    attempt=attempt,
                    exc=exc,
                    circuit_open=self._is_circuit_open(role.driver),
                )
        assert last_error is not None
        return self._contained_driver_failure(role, str(last_error))

    def _emit_driver_failure(
        self,
        *,
        task_id: str,
        role: Role,
        attempt: int,
        exc: Exception,
        circuit_open: bool,
    ) -> None:
        self.log.emit(
            actor=Actor.system("driver_gateway"),
            type="driver.failed",
            task_id=task_id,
            payload={
                "role": role.id,
                "driver": role.driver,
                "attempt": attempt,
                "error_type": type(exc).__name__,
                "message": str(exc),
                "circuit_open": circuit_open,
                "contained": True,
            },
        )

    def _is_circuit_open(self, driver_name: str) -> bool:
        return self._driver_failures.get(driver_name, 0) >= self.circuit_breaker_threshold

    @staticmethod
    def _contained_driver_failure(role: Role, reason: str) -> DriverResponse:
        text = json.dumps(
            {
                "stance": "challenge",
                "option_id": f"driver_failure:{role.id}",
                "claim": f"{role.name} driver failed; human review required",
                "evidence": [reason],
                "confidence": 1.0,
                "text": f"{role.name} could not produce a normal response: {reason}",
            },
            ensure_ascii=False,
        )
        return DriverResponse(text=text, tokens=0, meta={"driver": role.driver, "contained": True})

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
    def _build_brief(
        task_id: str,
        utterances: list[dict[str, Any]],
        cabinet_policy: CabinetPolicy,
    ) -> dict[str, Any]:
        positions = []
        for item in utterances:
            role: Role = item["role"]
            payload = item["payload"]
            positions.append(
                {
                    "actor": role.id,
                    "option_id": payload.get("option_id") or role.id,
                    "claim": payload.get("claim", ""),
                    "evidence": payload.get("evidence", []),
                    "confidence": payload.get("confidence"),
                    "stance": payload.get("stance"),
                }
            )
        cabinet = tally_votes(
            positions,
            electorate_size=len(positions),
            approval_threshold=cabinet_policy.approval_threshold,
            quorum=cabinet_policy.quorum,
            veto_roles=cabinet_policy.veto_roles,
        )

        return {
            "task_id": task_id,
            "dispute": "Agents proposed different next steps; human decision required.",
            "positions": positions,
            "consensus": [cabinet["recommendation"]] if cabinet["recommendation"] else [],
            "root_cause": cabinet["reason"],
            "cabinet": cabinet,
            "options": cabinet["options"],
        }


def load_runtime_config(path: str | Path | None = None) -> RuntimeConfig:
    source = Path(path) if path is not None else _pack_path("default_roles.json")
    raw = json.loads(source.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        roles_raw = raw.get("roles")
        cabinet_raw = raw.get("cabinet")
    else:
        roles_raw = raw
        cabinet_raw = None
    if not isinstance(roles_raw, list):
        raise ValueError("roles config must be a list or an object with roles")
    roles = []
    for item in roles_raw:
        if not isinstance(item, dict):
            raise ValueError("each role must be an object")
        roles.append(
            Role(
                id=str(item["id"]),
                name=str(item.get("name") or item["id"]),
                role=str(item.get("role") or item["id"]),
                driver=str(item.get("driver") or item["id"]),
            )
        )
    return RuntimeConfig(roles=roles, cabinet_policy=CabinetPolicy.from_dict(cabinet_raw))


def load_roles(path: str | Path | None = None) -> list[Role]:
    return load_runtime_config(path).roles


def _pack_path(name: str) -> Path:
    return Path(str(resources.files("cater").joinpath("packs", name)))
