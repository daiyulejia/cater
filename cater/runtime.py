"""Runtime assembly root for Cater."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from .assignments import latest_assignments_by_owner
from .cabinet import CabinetPolicy, tally_votes
from .drivers import Driver, DriverResponse, load_drivers
from .events import Actor, Event, EventLog
from .fold import TaskSnapshot, fold
from .profiles import agent_profiles
from .review import build_review


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
    governance_policy: "GovernancePolicy"


@dataclass(frozen=True)
class GovernancePolicy:
    trigger_on_challenge: bool = True
    trigger_on_option_split: bool = True
    low_confidence_threshold: float | None = 0.5

    @staticmethod
    def from_dict(data: dict[str, Any] | None) -> "GovernancePolicy":
        if data is None:
            return GovernancePolicy()
        if not isinstance(data, dict):
            raise ValueError("governance config must be an object")
        threshold = data.get("low_confidence_threshold", 0.5)
        if threshold is not None:
            threshold = float(threshold)
            if threshold < 0 or threshold > 1:
                raise ValueError("governance.low_confidence_threshold must be between 0 and 1")
        return GovernancePolicy(
            trigger_on_challenge=bool(data.get("trigger_on_challenge", True)),
            trigger_on_option_split=bool(data.get("trigger_on_option_split", True)),
            low_confidence_threshold=threshold,
        )


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
        self.governance_policy = config.governance_policy
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

    def collaborate(self, task_id: str) -> dict[str, Any]:
        return self._run_round(task_id, force_governance=False)

    def battle(self, task_id: str) -> dict[str, Any]:
        return self._run_round(task_id, force_governance=True)

    def dispatch(self, task_id: str) -> dict[str, Any]:
        task = self.task(task_id)
        if task is None:
            raise KeyError(task_id)

        mission = {
            "goal": task.goal,
            "acceptance": list(task.acceptance),
            "source": "orchestrator",
            "intent": "Turn the human command into executable organizational work.",
        }
        self.log.emit(
            actor=Actor.system("orchestrator"),
            type="mission.created",
            task_id=task_id,
            payload=mission,
        )

        subtasks = self._decompose_task(task)
        decomposition = {
            "source": "cabinet",
            "strategy": "cabinet divides mission into role-owned execution lanes",
            "subtasks": subtasks,
        }
        self.log.emit(
            actor=Actor.system("cabinet"),
            type="task.decomposed",
            task_id=task_id,
            payload=decomposition,
        )
        for subtask in subtasks:
            self.log.emit(
                actor=Actor.system("cabinet"),
                type="subtask.assigned",
                task_id=task_id,
                payload=subtask,
            )

        plan = {
            "source": "cabinet",
            "mission": mission,
            "subtasks": subtasks,
            "flow": ["orchestrator", "cabinet", "workers", "review"],
            "next_action": "run",
            "reason": "Top layer framed the mission; cabinet assigned executable work to the worker layer.",
        }
        self.log.emit(
            actor=Actor.system("cabinet"),
            type="plan.proposed",
            task_id=task_id,
            payload=plan,
        )
        return {"task_id": task_id, "mission": mission, "plan": plan}

    def assign(
        self,
        task_id: str,
        owner: str,
        *,
        objective: str | None = None,
        acceptance: str | None = None,
    ) -> dict[str, Any]:
        task = self.task(task_id)
        if task is None:
            raise KeyError(task_id)
        role = self._role_by_id(owner)
        assignment = {
            "id": f"{task.id}:{role.id}:direct",
            "owner": role.id,
            "role": role.role,
            "lane": self._role_lane(role.role),
            "objective": objective or f"{role.name} directly owns execution for: {task.goal}",
            "acceptance": acceptance or (task.acceptance[0] if task.acceptance else "complete the requested work"),
            "depends_on": [],
            "status": "assigned",
            "source": "human",
            "mode": "direct",
        }
        self.log.emit(
            actor=Actor.human("human"),
            type="subtask.assigned",
            task_id=task_id,
            payload=assignment,
        )
        plan = {
            "source": "human",
            "mission": {"goal": task.goal, "acceptance": list(task.acceptance), "source": "human"},
            "subtasks": [assignment],
            "flow": ["human", "worker", "review"],
            "next_action": "run",
            "reason": "Human assigned this task directly to the worker layer.",
        }
        self.log.emit(
            actor=Actor.human("human"),
            type="plan.proposed",
            task_id=task_id,
            payload=plan,
        )
        return {"task_id": task_id, "assignment": assignment, "plan": plan}

    def _run_round(self, task_id: str, *, force_governance: bool) -> dict[str, Any]:
        task = self.task(task_id)
        if task is None:
            raise KeyError(task_id)

        profiles = self.profiles()
        utterances = []
        role_ids = {role.id for role in self.roles}
        for role in self.roles:
            driver = self.drivers[role.driver]
            prompt = self._prompt(task, role, profiles.get(role.id), utterances)
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
            visible_refs = {
                int(item["seq"])
                for item in task.messages
                if isinstance(item.get("seq"), int)
            }
            visible_refs.update(
                event.seq
                for item in utterances
                for event in [item.get("event")]
                if isinstance(getattr(event, "seq", None), int)
            )
            target = self._optional_target(parsed.get("target"), role_ids)
            reply_to = self._optional_reply_to(parsed.get("reply_to"), visible_refs)
            if target is not None:
                payload["target"] = target
            if reply_to is not None:
                payload["reply_to"] = reply_to
            event = self.log.emit(
                actor=Actor.agent(role.id, role.role),
                type=event_type,
                task_id=task_id,
                payload=payload,
            )
            utterances.append({"event": event, "payload": payload, "role": role})

        governance = self._governance_trigger(utterances, force_governance=force_governance)
        if governance["status"] == "not_needed":
            brief = self._build_collaboration_brief(task_id, utterances, governance)
            brief_event = self.log.emit(
                actor=Actor.system("orchestrator"),
                type="brief.generated",
                task_id=task_id,
                payload={"brief": brief, "reason": "collaboration_continues", "options": brief["options"]},
            )
            return {"task_id": task_id, "brief_event": brief_event.seq, "brief": brief}

        brief = self._build_brief(task_id, utterances, self.cabinet_policy, governance)
        self.log.emit(
            actor=Actor.system("cabinet"),
            type="cabinet.voted",
            task_id=task_id,
            payload={"tally": brief["cabinet"]},
        )
        if brief["cabinet"]["recommendation"]:
            self.log.emit(
                actor=Actor.system("cabinet"),
                type="decision.proposed",
                task_id=task_id,
                payload={
                    "option_id": brief["cabinet"]["recommendation"],
                    "reason": brief["cabinet"]["reason"],
                    "source": "cabinet",
                    "status": "accepted_by_cabinet",
                },
            )
            self.log.emit(
                actor=Actor.system("cabinet"),
                type="agent.decided",
                task_id=task_id,
                payload={
                    "resolution": "accepted",
                    "option_id": brief["cabinet"]["recommendation"],
                    "reason": brief["cabinet"]["reason"],
                    "source": "cabinet",
                },
            )
        elif brief["cabinet"]["status"] == "vetoed":
            self.log.emit(
                actor=Actor.system("cabinet"),
                type="agent.decided",
                task_id=task_id,
                payload={
                    "resolution": "rework",
                    "option_id": None,
                    "reason": brief["cabinet"]["reason"],
                    "source": "cabinet",
                },
            )
        elif brief["cabinet"]["status"] == "needs_supreme":
            self.log.emit(
                actor=Actor.system("supreme"),
                type="supreme.decided",
                task_id=task_id,
                payload={
                    "resolution": "rework",
                    "option_id": brief["cabinet"]["top_option"],
                    "reason": brief["cabinet"]["reason"],
                    "source": "supreme",
                },
            )
        self.log.emit(
            actor=Actor.system("orchestrator"),
            type="brief.generated"
            if brief["cabinet"]["status"] in {"recommended", "vetoed", "needs_supreme"}
            else "escalation.triggered",
            task_id=task_id,
            payload={
                "brief": brief,
                "reason": "agent_decided"
                if brief["cabinet"]["status"] in {"recommended", "vetoed", "needs_supreme"}
                else "needs_human_input",
                "options": brief["options"],
            },
        )
        if brief["cabinet"]["status"] in {"recommended", "vetoed", "needs_supreme"}:
            brief_event = self.log.read(task_id=task_id)[-1]
        else:
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

    def command(
        self,
        task_id: str,
        instruction: str,
        *,
        goal: str | None = None,
        acceptance: list[str] | None = None,
    ) -> Event:
        if self.task(task_id) is None:
            raise KeyError(task_id)
        payload: dict[str, Any] = {"instruction": instruction}
        if goal is not None:
            payload["goal"] = goal
        if acceptance is not None:
            payload["acceptance"] = list(acceptance)
        return self.log.emit(
            actor=Actor.human("human"),
            type="human.commanded",
            task_id=task_id,
            payload=payload,
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

    def profiles(self) -> dict[str, dict[str, Any]]:
        return agent_profiles(self.log.read())

    def review(self, task_id: str | None = None, *, record: bool = False) -> dict[str, Any]:
        events = self.log.read(task_id=task_id)
        tasks = self.tasks() if task_id is None else ([self.task(task_id)] if self.task(task_id) else [])
        tasks = [task for task in tasks if task is not None]
        review = build_review(
            tasks=tasks,
            events=events,
            profiles=self.profiles(),
            chain=self.verify_chain(),
            scope=task_id or "workspace",
        )
        if record:
            self.log.emit(
                actor=Actor.system("reviewer"),
                type="review.generated",
                task_id=task_id,
                payload={"review": review},
            )
        return review

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
                "claim": f"{role.name} driver failed; autonomous decision needs caution",
                "evidence": [reason],
                "confidence": 1.0,
                "text": f"{role.name} could not produce a normal response: {reason}",
            },
            ensure_ascii=False,
        )
        return DriverResponse(text=text, tokens=0, meta={"driver": role.driver, "contained": True})

    def _prompt(
        self,
        task: TaskSnapshot,
        role: Role,
        profile: dict[str, Any] | None = None,
        round_outputs: list[dict[str, Any]] | None = None,
    ) -> str:
        memory = self._profile_memory(profile)
        task_memory = self._task_memory(task, role.id)
        round_memory = self._round_memory(round_outputs or [])
        return (
            f"You are {role.name}. Work as one member of an autonomous agent organization.\n"
            f"Goal: {task.goal}\n"
            f"Acceptance: {task.acceptance}\n"
            "Collaboration policy: produce the most useful next contribution first; "
            "use stance=challenge only for material disagreement, risk, or missing evidence; "
            "do not force a debate when normal collaboration is enough.\n"
            "Agent-to-agent policy: when you are continuing, correcting, or handing off another "
            "agent's output, include target=<role id> and reply_to=<event seq> in the JSON.\n"
            f"{memory}\n"
            f"{task_memory}\n"
            f"{round_memory}\n"
            "Return JSON with stance, option_id, claim, evidence, confidence, text, "
            "and optional target, reply_to."
        )

    @staticmethod
    def _profile_memory(profile: dict[str, Any] | None) -> str:
        if not profile:
            return "Profile memory: no prior activity for this role."
        parts = [
            f"messages={profile.get('messages', 0)}",
            f"supports={profile.get('supports', 0)}",
            f"challenges={profile.get('challenges', 0)}",
            f"driver_failures={profile.get('driver_failures', 0)}",
        ]
        drivers = profile.get("drivers") or []
        if drivers:
            parts.append("drivers=" + ",".join(str(item) for item in drivers))
        last_claim = str(profile.get("last_claim") or "")
        if last_claim:
            parts.append(f"last_claim={last_claim[:240]}")
        last_failure = str(profile.get("last_failure") or "")
        if last_failure:
            parts.append(f"last_failure={last_failure[:160]}")
        return "Profile memory: " + "; ".join(parts) + "."

    @staticmethod
    def _task_memory(task: TaskSnapshot, role_id: str | None = None) -> str:
        parts = []
        if task.assignments:
            latest_assignments = latest_assignments_by_owner(task.assignments)
            owned = [
                item for item in latest_assignments
                if role_id is not None and str(item.get("owner") or "") == role_id
            ]
            if owned:
                current = owned[-1]
                parts.append(
                    "your_assignment="
                    + str(current.get("objective") or "")[:180]
                    + "; acceptance="
                    + str(current.get("acceptance") or "")[:120]
                )
            assignments = []
            for item in latest_assignments:
                owner = str(item.get("owner") or "")
                objective = str(item.get("objective") or "")
                if owner and objective:
                    assignments.append(f"{owner}: {objective[:160]}")
            if assignments:
                parts.append("assigned_work=" + " | ".join(assignments))
        if task.commands:
            commands = []
            for item in task.commands[-3:]:
                instruction = str(item.get("instruction") or "")
                goal = str(item.get("goal") or "")
                text = instruction
                if goal:
                    text += f" goal={goal}"
                if text:
                    commands.append(text[:180])
            if commands:
                parts.append("recent_commands=" + " | ".join(commands))
        if task.messages:
            messages = []
            for item in task.messages[-6:]:
                actor = str(item.get("actor") or "agent")
                stance = str(item.get("stance") or "")
                claim = str(item.get("claim") or "")
                seq = item.get("seq")
                if claim:
                    suffix = f" (#{seq})" if seq is not None else ""
                    messages.append(f"{actor}/{stance}: {claim[:180]}{suffix}")
            if messages:
                parts.append("recent_agent_outputs=" + " | ".join(messages))
        if not parts:
            return "Task memory: no prior collaboration on this task."
        return "Task memory: " + "; ".join(parts) + "."

    def _role_by_id(self, role_id: str) -> Role:
        for role in self.roles:
            if role.id == role_id:
                return role
        known = ", ".join(role.id for role in self.roles)
        raise ValueError(f"unknown role {role_id!r}; expected one of: {known}")

    def _decompose_task(self, task: TaskSnapshot) -> list[dict[str, Any]]:
        criteria = list(task.acceptance) or ["produce a useful, verifiable next increment"]
        subtasks = []
        prior_by_lane: dict[str, list[str]] = {}
        for index, role in enumerate(self.roles):
            criterion = criteria[index % len(criteria)]
            lane = self._role_lane(role.role)
            subtask_id = f"{task.id}:{role.id}:{index + 1}"
            depends_on = self._lane_dependencies(lane, prior_by_lane)
            subtasks.append(
                {
                    "id": subtask_id,
                    "owner": role.id,
                    "role": role.role,
                    "lane": lane,
                    "objective": f"{role.name} owns the {lane} lane for: {task.goal}",
                    "acceptance": criterion,
                    "deliverable": self._lane_deliverable(lane),
                    "depends_on": depends_on,
                    "status": "assigned",
                }
            )
            prior_by_lane.setdefault(lane, []).append(subtask_id)
        return subtasks

    @staticmethod
    def _role_lane(role: str) -> str:
        normalized = role.lower()
        if "product" in normalized:
            return "scope and user value"
        if "dev" in normalized or "engineer" in normalized:
            return "implementation"
        if "test" in normalized or "qa" in normalized:
            return "verification and risk"
        if "design" in normalized or "ui" in normalized:
            return "interaction design"
        return "execution"

    @staticmethod
    def _lane_deliverable(lane: str) -> str:
        if lane == "scope and user value":
            return "goal framing, user value, and acceptance clarification"
        if lane == "implementation":
            return "working implementation plan or code-ready change"
        if lane == "verification and risk":
            return "risk review, test strategy, and release confidence"
        if lane == "interaction design":
            return "usable interaction flow and interface constraints"
        return "role-owned execution output"

    @staticmethod
    def _lane_dependencies(lane: str, prior_by_lane: dict[str, list[str]]) -> list[str]:
        if lane == "scope and user value":
            return []
        if lane == "implementation":
            return list(prior_by_lane.get("scope and user value", []))
        if lane == "interaction design":
            return list(prior_by_lane.get("scope and user value", []))
        if lane == "verification and risk":
            deps = []
            for prior_lane, ids in prior_by_lane.items():
                if prior_lane != "verification and risk":
                    deps.extend(ids)
            return deps
        return [ids[-1] for ids in prior_by_lane.values() if ids]

    @staticmethod
    def _round_memory(utterances: list[dict[str, Any]]) -> str:
        if not utterances:
            return "Current round memory: no earlier outputs in this round."
        items = []
        for item in utterances[-6:]:
            role: Role = item["role"]
            payload = item["payload"]
            stance = str(payload.get("stance") or "")
            claim = str(payload.get("claim") or "")
            event = item.get("event")
            seq = getattr(event, "seq", None)
            if claim:
                suffix = f" (#{seq})" if seq is not None else ""
                items.append(f"{role.id}/{stance}: {claim[:180]}{suffix}")
        if not items:
            return "Current round memory: earlier outputs had no claims."
        return "Current round memory: " + " | ".join(items) + "."

    @staticmethod
    def _optional_target(value: Any, role_ids: set[str]) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text if text in role_ids else None

    @staticmethod
    def _optional_reply_to(value: Any, visible_refs: set[int]) -> int | None:
        if value is None or value == "":
            return None
        try:
            seq = int(value)
        except (TypeError, ValueError):
            return None
        return seq if seq in visible_refs else None

    @staticmethod
    def _parse_response(text: str) -> dict[str, Any]:
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return {"stance": "assert", "claim": "", "text": text, "evidence": []}
        return data if isinstance(data, dict) else {"stance": "assert", "text": text}

    def _governance_trigger(
        self,
        utterances: list[dict[str, Any]],
        *,
        force_governance: bool,
    ) -> dict[str, Any]:
        if force_governance:
            return {
                "status": "forced",
                "reason": "governance round forced by caller",
                "signals": ["forced"],
            }

        signals = []
        option_ids = set()
        for item in utterances:
            role: Role = item["role"]
            payload = item["payload"]
            if self.governance_policy.trigger_on_challenge and payload.get("stance") == "challenge":
                signals.append(f"challenge:{role.id}")
            option_id = payload.get("option_id")
            if option_id:
                option_ids.add(str(option_id))
            confidence = payload.get("confidence")
            threshold = self.governance_policy.low_confidence_threshold
            if isinstance(confidence, (int, float)) and threshold is not None and confidence < threshold:
                signals.append(f"low_confidence:{role.id}={confidence}")
        if self.governance_policy.trigger_on_option_split and len(option_ids) > 1:
            signals.append("option_split:" + ",".join(sorted(option_ids)))
        if signals:
            return {
                "status": "triggered",
                "reason": "governance triggered by " + "; ".join(signals),
                "signals": signals,
            }
        return {
            "status": "not_needed",
            "reason": "normal collaboration round",
            "signals": [],
        }

    @staticmethod
    def _build_collaboration_brief(
        task_id: str,
        utterances: list[dict[str, Any]],
        governance: dict[str, Any],
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
                    "target": payload.get("target"),
                    "reply_to": payload.get("reply_to"),
                }
            )
        options = [
            {
                "id": item["option_id"],
                "summary": item["claim"],
                "support": 1,
                "concern": 0,
                "supporters": [item["actor"]],
                "concerns": [],
            }
            for item in positions
        ]
        return {
            "task_id": task_id,
            "dispute": "Agents collaborated without material disagreement; governance was not needed.",
            "positions": positions,
            "consensus": [positions[0]["option_id"]] if positions else [],
            "root_cause": "No challenge, option split, or low-confidence signal was observed.",
            "cabinet": None,
            "governance": governance,
            "options": options,
        }

    @staticmethod
    def _build_brief(
        task_id: str,
        utterances: list[dict[str, Any]],
        cabinet_policy: CabinetPolicy,
        governance: dict[str, Any],
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
                    "target": payload.get("target"),
                    "reply_to": payload.get("reply_to"),
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
            "dispute": "Agents exchanged contributions; governance selected the next action when needed.",
            "positions": positions,
            "consensus": [cabinet["recommendation"]] if cabinet["recommendation"] else [],
            "root_cause": cabinet["reason"],
            "cabinet": cabinet,
            "governance": governance,
            "options": cabinet["options"],
        }


def load_runtime_config(path: str | Path | None = None) -> RuntimeConfig:
    source = Path(path) if path is not None else _pack_path("default_roles.json")
    raw = json.loads(source.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        roles_raw = raw.get("roles")
        cabinet_raw = raw.get("cabinet")
        governance_raw = raw.get("governance")
    else:
        roles_raw = raw
        cabinet_raw = None
        governance_raw = None
    if not isinstance(roles_raw, list):
        raise ValueError("roles config must be a list or an object with roles")
    roles = []
    for item in roles_raw:
        if not isinstance(item, dict):
            raise ValueError("each role must be an object")
        role_id = item.get("id")
        if not isinstance(role_id, str) or not role_id.strip():
            raise ValueError("each role requires a non-empty string id")
        role_id = role_id.strip()
        roles.append(
            Role(
                id=role_id,
                name=str(item.get("name") or role_id),
                role=str(item.get("role") or role_id),
                driver=str(item.get("driver") or role_id),
            )
        )
    return RuntimeConfig(
        roles=roles,
        cabinet_policy=CabinetPolicy.from_dict(cabinet_raw),
        governance_policy=GovernancePolicy.from_dict(governance_raw),
    )


def load_roles(path: str | Path | None = None) -> list[Role]:
    return load_runtime_config(path).roles


def _pack_path(name: str) -> Path:
    return Path(str(resources.files("cater").joinpath("packs", name)))
