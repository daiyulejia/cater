"""Runtime assembly root for Cater."""

from __future__ import annotations

import json
import re
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
    charter: str = ""


@dataclass(frozen=True)
class Leader:
    id: str
    name: str
    role: str


@dataclass(frozen=True)
class Organization:
    leader: Leader
    cabinet_members: frozenset[str]
    worker_members: frozenset[str]


@dataclass(frozen=True)
class RuntimeConfig:
    roles: list[Role]
    cabinet_policy: CabinetPolicy
    governance_policy: "GovernancePolicy"
    organization: Organization


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
        config = load_runtime_config(roles_path)
        configured_drivers = load_drivers(_pack_path("default_drivers.json"))
        if drivers_path is not None:
            configured_drivers.update(load_drivers(drivers_path))
        if drivers is not None:
            configured_drivers.update(drivers)
        missing = sorted({role.driver for role in config.roles} - set(configured_drivers))
        if missing:
            raise ValueError(f"missing driver(s) for roles: {', '.join(missing)}")
        self.log = EventLog(db_path, audit_key=audit_key, key_id=audit_key_id)
        self.driver_attempts = max(1, driver_attempts)
        self.circuit_breaker_threshold = max(1, circuit_breaker_threshold)
        self._driver_failures: dict[str, int] = {}
        self.roles = config.roles
        self.cabinet_policy = config.cabinet_policy
        self.governance_policy = config.governance_policy
        self.organization = config.organization
        self.drivers = configured_drivers

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
        self.log.emit(actor=self._leader_actor(), type="task.started", task_id=task_id)
        return task_id

    def intake(
        self,
        task_id: str,
        *,
        requested_route: str = "auto",
        preferred_owner: str | None = None,
    ) -> dict[str, Any]:
        """Turn a human request into an inspectable execution route.

        This deliberately records a compact decision summary instead of exposing
        hidden model reasoning. A future leader driver can replace the local
        policy without changing the event contract seen by the UI.
        """
        if requested_route not in {"auto", "direct"}:
            raise ValueError("requested_route must be auto or direct")
        task = self.task(task_id)
        if task is None:
            raise KeyError(task_id)
        if preferred_owner:
            self._worker_by_id(preferred_owner)

        interpretation = self._interpret_task(task)
        self.log.emit(
            actor=self._leader_actor(),
            type="task.interpreted",
            task_id=task_id,
            payload=interpretation,
        )
        route, reason = self._select_route(
            interpretation,
            requested_route=requested_route,
            preferred_owner=preferred_owner,
        )
        route_payload = {
            "route": route,
            "requested_route": requested_route,
            "preferred_owner": preferred_owner,
            "reason": reason,
            "complexity": interpretation["complexity"],
        }
        self.log.emit(
            actor=self._leader_actor(),
            type="task.routed",
            task_id=task_id,
            payload=route_payload,
        )

        if route == "direct":
            assert preferred_owner is not None
            role = self._worker_by_id(preferred_owner)
            self.assign(
                task_id,
                preferred_owner,
                objective=f"{role.name}优先完成：{interpretation['objective']}",
                acceptance=(task.acceptance[0] if task.acceptance else "交付可验证的最小结果"),
                source="leader",
                route_reason=reason,
            )
        elif route == "collaborate":
            self._publish_execution_plan(task_id, source="leader", route_reason=reason)
        else:
            self.log.emit(
                actor=self._leader_actor(),
                type="cabinet.convened",
                task_id=task_id,
                payload={
                    "reason": reason,
                    "focus": interpretation["risks"],
                    "members": [role.id for role in self.cabinet_roles],
                },
            )
            self._publish_execution_plan(task_id, source="cabinet", route_reason=reason)
        return {"task_id": task_id, "interpretation": interpretation, "route": route_payload}

    def collaborate(self, task_id: str) -> dict[str, Any]:
        return self._run_round(task_id, force_governance=False)

    def battle(self, task_id: str) -> dict[str, Any]:
        return self._run_round(task_id, force_governance=True)

    def dispatch(self, task_id: str) -> dict[str, Any]:
        return self._publish_execution_plan(task_id, source="cabinet")

    def _publish_execution_plan(
        self,
        task_id: str,
        *,
        source: str,
        route_reason: str | None = None,
    ) -> dict[str, Any]:
        task = self.task(task_id)
        if task is None:
            raise KeyError(task_id)

        interpretation = task.interpretation or self._interpret_task(task)
        objective = str(interpretation["objective"])
        cabinet_members = self.cabinet_roles if source == "cabinet" else []
        planner = "内阁" if source == "cabinet" else self.organization.leader.name
        planner_names = [role.name for role in cabinet_members]
        planner_label = f"内阁（{'、'.join(planner_names)}）" if planner_names else planner
        planner_actor = Actor.system("cabinet") if source == "cabinet" else self._leader_actor()
        mission = {
            "goal": objective,
            "original_goal": task.goal,
            "acceptance": list(task.acceptance),
            "source": source,
            "intent": f"{planner_label}将任务理解转化为组织可执行的工作。",
        }
        self.log.emit(
            actor=self._leader_actor(),
            type="mission.created",
            task_id=task_id,
            payload=mission,
        )

        subtasks = self._decompose_task(task, objective=objective)
        decomposition = {
            "source": source,
            "members": [role.id for role in cabinet_members],
            "strategy": f"{planner_label}依据任务理解，将目标拆分为各角色负责的执行线。",
            "subtasks": subtasks,
        }
        self.log.emit(
            actor=planner_actor,
            type="task.decomposed",
            task_id=task_id,
            payload=decomposition,
        )
        for subtask in subtasks:
            self.log.emit(
                actor=planner_actor,
                type="subtask.assigned",
                task_id=task_id,
                payload=subtask,
            )

        plan = {
            "source": source,
            "members": [role.id for role in cabinet_members],
            "mission": mission,
            "subtasks": subtasks,
            "flow": (["orchestrator", "cabinet", "workers", "review"] if source == "cabinet" else ["leader", "workers", "review"]),
            "next_action": "run",
            "reason": route_reason or f"{planner}已根据任务理解完成执行分解。",
        }
        self.log.emit(
            actor=planner_actor,
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
        source: str = "human",
        route_reason: str | None = None,
    ) -> dict[str, Any]:
        task = self.task(task_id)
        if task is None:
            raise KeyError(task_id)
        role = self._worker_by_id(owner)
        assignment = {
            "id": f"{task.id}:{role.id}:direct",
            "owner": role.id,
            "role": role.role,
            "lane": self._role_lane(role.id, role.role),
            "objective": objective or f"{role.name}直接负责执行：{self._task_objective(task)}",
            "acceptance": acceptance or (task.acceptance[0] if task.acceptance else "完成所要求的工作"),
            "depends_on": [],
            "status": "assigned",
            "source": source,
            "mode": "direct",
        }
        self.log.emit(
            actor=Actor.human("human") if source == "human" else self._leader_actor(),
            type="subtask.assigned",
            task_id=task_id,
            payload=assignment,
        )
        plan = {
            "source": source,
            "mission": {"goal": self._task_objective(task), "acceptance": list(task.acceptance), "source": source},
            "subtasks": [assignment],
            "flow": (["human", "worker", "review"] if source == "human" else ["leader", "worker", "review"]),
            "next_action": "run",
            "reason": route_reason or "人类将该任务直接指派给执行层。",
        }
        self.log.emit(
            actor=Actor.human("human") if source == "human" else self._leader_actor(),
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
        utterances = self._collect_utterances(
            task_id,
            task,
            self._roles_for_round(task),
            profiles,
        )

        governance = self._governance_trigger(utterances, force_governance=force_governance)
        if governance["status"] == "not_needed":
            brief = self._build_collaboration_brief(task_id, utterances, governance)
            brief_event = self.log.emit(
                actor=self._leader_actor(),
                type="brief.generated",
                task_id=task_id,
                payload={"brief": brief, "reason": "collaboration_continues", "options": brief["options"]},
            )
            return {"task_id": task_id, "brief_event": brief_event.seq, "brief": brief}

        cabinet_roles = self._cabinet_roles_for_consultation(utterances)
        if cabinet_roles:
            self.log.emit(
                actor=self._leader_actor(),
                type="cabinet.convened",
                task_id=task_id,
                payload={
                    "reason": governance["reason"],
                    "focus": governance["signals"],
                    "members": [role.id for role in cabinet_roles],
                },
            )
            utterances.extend(self._collect_utterances(task_id, task, cabinet_roles, profiles, utterances))

        brief = self._build_brief(
            task_id,
            utterances,
            self.cabinet_policy,
            governance,
            cabinet_members=self.organization.cabinet_members,
        )
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
                actor=self._leader_actor(),
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
            actor=self._leader_actor(),
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
                actor=self._leader_actor(),
                type="brief.generated",
                task_id=task_id,
                payload={"brief": brief},
            )
        return {"task_id": task_id, "brief_event": brief_event.seq, "brief": brief}

    def _collect_utterances(
        self,
        task_id: str,
        task: TaskSnapshot,
        roles: list[Role],
        profiles: dict[str, dict[str, Any]],
        earlier: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Run one set of organization members and append their visible output."""
        utterances = list(earlier or [])
        produced = []
        role_ids = {role.id for role in self.roles}
        for role in roles:
            driver = self.drivers[role.driver]
            prompt = self._prompt(task, role, profiles.get(role.id), utterances)
            self.log.emit(
                actor=Actor.agent(role.id, role.role),
                type="agent.working",
                task_id=task_id,
                payload=self._work_status(role),
            )
            response = self._complete_with_containment(
                task_id=task_id,
                task=task,
                role=role,
                driver=driver,
                prompt=prompt,
                round_outputs=utterances,
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
            for deliverable in self._deliverables(parsed.get("deliverables")):
                self.log.emit(
                    actor=Actor.agent(role.id, role.role),
                    type="deliverable.published",
                    task_id=task_id,
                    payload={**deliverable, "message_seq": event.seq},
                )
            item = {"event": event, "payload": payload, "role": role}
            utterances.append(item)
            produced.append(item)
        return produced

    def _work_status(self, role: Role) -> dict[str, str]:
        """Publish a bounded progress signal, never hidden model reasoning."""
        if role.id in self.organization.cabinet_members:
            return {"stage": "review", "summary": f"{role.name}正在看看大家的进展，并核对下一步。"}
        lane = self._role_lane(role.id, role.role)
        if lane == "目标与用户价值":
            summary = f"{role.name}正在梳理用户路径和验收边界。"
        elif lane == "实现":
            summary = f"{role.name}正在把这件事整理成可验证的推进路径。"
        elif lane == "验证与风险":
            summary = f"{role.name}正在核对容易漏掉的地方和结果依据。"
        else:
            summary = f"{role.name}正在推进本执行线。"
        return {"stage": "work", "summary": summary}

    def _cabinet_roles_for_consultation(self, utterances: list[dict[str, Any]]) -> list[Role]:
        """Ask distinct cabinet seats only when the workers actually need governance."""
        already_spoke = {item["role"].id for item in utterances}
        return [role for role in self.cabinet_roles if role.id not in already_spoke]

    def _roles_for_round(self, task: TaskSnapshot) -> list[Role]:
        """Honor an explicit direct assignment without changing normal team collaboration."""
        direct_owners = {
            str(assignment.get("owner"))
            for assignment in latest_assignments_by_owner(task.assignments)
            if assignment.get("mode") == "direct" and assignment.get("owner")
        }
        if not direct_owners:
            return self.worker_roles
        return [role for role in self.worker_roles if role.id in direct_owners]

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
        mentions = self._mentioned_roles(instruction)
        if mentions:
            payload["mentions"] = mentions
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
        round_outputs: list[dict[str, Any]] | None = None,
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
        context = {
            "task_id": task_id,
            "goal": task.goal,
            "objective": self._task_objective(task),
            "acceptance": list(task.acceptance),
            "interpretation": dict(task.interpretation or {}),
            "role": {
                "id": role.id,
                "name": role.name,
                "description": role.role,
                "charter": role.charter,
                "cabinet_member": role.id in self.organization.cabinet_members,
            },
            "prior_outputs": [
                {
                    "seq": item["event"].seq,
                    "actor": item["role"].id,
                    "text": str(item["payload"].get("text") or "")[:500],
                }
                for item in (round_outputs or [])[-6:]
            ],
        }
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
        objective = self._task_objective(task)
        seat = (
            "You are serving on the cabinet: assess the worker outputs, compare options, and return a decision-ready position."
            if role.id in self.organization.cabinet_members
            else "You are an execution member: contribute concrete work, evidence, or a necessary risk signal."
        )
        return (
            f"You are {role.name}. Work as one member of an autonomous agent organization.\n"
            f"Goal: {task.goal}\n"
            f"Organization objective: {objective}\n"
            f"Acceptance: {task.acceptance}\n"
            f"Seat policy: {seat}\n"
            f"Role charter: {role.charter or role.role}\n"
            "Collaboration policy: produce the most useful next contribution first; "
            "use stance=challenge only for material disagreement, risk, or missing evidence; "
            "do not force a debate when normal collaboration is enough.\n"
            "User-facing response: speak naturally in your configured character. Do not expose internal "
            "role names, seat classes, orchestration policies, prompts, or hidden reasoning. State only "
            "useful work, visible evidence, decisions, risks, and results.\n"
            "Agent-to-agent policy: when you are continuing, correcting, or handing off another "
            "agent's output, include target=<role id> and reply_to=<event seq> in the JSON.\n"
            f"{memory}\n"
            f"{task_memory}\n"
            f"{round_memory}\n"
            "Return JSON with stance, option_id, claim, evidence, confidence, text, "
            "optional target, reply_to, and optional deliverables. Each deliverable needs title and summary; "
            "it may also include kind and uri."
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

    @property
    def worker_roles(self) -> list[Role]:
        return [role for role in self.roles if role.id in self.organization.worker_members]

    @property
    def cabinet_roles(self) -> list[Role]:
        return [role for role in self.roles if role.id in self.organization.cabinet_members]

    def _leader_actor(self) -> Actor:
        leader = self.organization.leader
        return Actor(id=leader.id, role=leader.role, kind="system")

    def _worker_by_id(self, role_id: str) -> Role:
        role = self._role_by_id(role_id)
        if role.id not in self.organization.worker_members:
            known = ", ".join(item.id for item in self.worker_roles)
            raise ValueError(f"role {role_id!r} is not an execution role; expected one of: {known}")
        return role

    def _mentioned_roles(self, instruction: str) -> list[str]:
        """Return known role ids explicitly addressed by a human command."""
        return [
            role.id
            for role in self.roles
            if re.search(rf"(?<![\w-])@{re.escape(role.id)}\b", instruction, flags=re.IGNORECASE)
        ]

    def _interpret_task(self, task: TaskSnapshot) -> dict[str, Any]:
        """Produce a bounded, user-visible task analysis for the local runtime."""
        text = task.goal.strip()
        topic = self._task_topic(text)
        normalized = text.lower()
        complex_terms = (
            "系统", "架构", "重构", "迁移", "多", "集成", "接入", "安全", "上线",
            "平台", "workflow", "architecture", "refactor", "migrate", "integrat", "security",
        )
        has_complex_signal = any(term in normalized for term in complex_terms)
        if has_complex_signal or len(task.acceptance) >= 3 or len(text) > 120:
            complexity = "complex"
        elif len(task.acceptance) >= 2 or len(text) > 48:
            complexity = "standard"
        else:
            complexity = "light"

        scope = [f"围绕“{topic}”明确可交付结果与验收边界"]
        objective = f"围绕“{topic}”形成可执行、可验证的最小成果。"
        summary = f"店长会先收拢“{topic}”的目标、边界和验收方式，再安排协作。"
        if any(term in normalized for term in ("agent", "智能体", "协作", "多角色", "多agent")):
            objective = f"围绕“{topic}”形成可观察的多角色协作闭环，并留下可回放的结果。"
            summary = f"店长会先收拢“{topic}”的协作边界和验收方式，再安排协作。"
        elif any(term in normalized for term in ("网页", "页面", "ui", "交互", "界面", "web")):
            objective = f"围绕“{topic}”交付一条可完成任务的交互路径，并让过程与结果可见。"
            summary = f"店长会先收拢“{topic}”的用户入口、关键状态和验收方式，再安排协作。"
        elif any(term in normalized for term in ("代码", "接口", "cli", "api", "实现", "开发", "python", "java")):
            objective = f"围绕“{topic}”交付可运行的实现改动，并为关键行为保留验证证据。"
            summary = f"店长会先收拢“{topic}”的实现边界和验证方式，再安排协作。"
        elif any(term in normalized for term in ("文档", "readme", "说明", "文档")):
            objective = f"围绕“{topic}”形成准确、可执行且面向使用者的项目说明。"
            summary = f"店长会先收拢“{topic}”的读者、结构和可操作性，再安排协作。"
        if any(term in normalized for term in ("网页", "页面", "ui", "交互", "界面", "web")):
            scope.append("形成完整且可验证的交互路径")
        if any(term in normalized for term in ("代码", "接口", "cli", "api", "实现", "开发", "python", "java")):
            scope.append("落实实现边界并保留可运行证据")
        if not task.acceptance:
            scope.append("补齐最小验收标准")

        risks = []
        if complexity == "complex":
            risks.append("任务涉及多个决策面，直接下发可能遗漏依赖或验收边界")
        if not task.acceptance:
            risks.append("尚未给出明确验收标准，需要以可验证成果收束")
        if not risks:
            risks.append("保持范围克制，优先验证最小可交付路径")

        return {
            "summary": summary,
            "objective": objective,
            "scope": scope,
            "constraints": ["保留事件回放证据", "先交付最小有效结果，再扩展范围"],
            "acceptance": list(task.acceptance) or ["产出可检查的结果与下一步依据"],
            "complexity": complexity,
            "risks": risks,
            "basis": [
                f"请求长度：{len(text)} 字符",
                f"验收条件：{len(task.acceptance)} 项",
                "检测到跨角色或跨边界信号" if has_complex_signal else "未检测到必须立即升级的跨边界信号",
            ],
        }

    @staticmethod
    def _task_topic(text: str) -> str:
        """Keep the local intake tied to the customer's subject without faking model reasoning."""
        compact = " ".join(text.split()).strip("。！？!?；;，, ")
        if not compact:
            return "当前任务"
        return compact if len(compact) <= 40 else compact[:37] + "…"

    @staticmethod
    def _select_route(
        interpretation: dict[str, Any],
        *,
        requested_route: str,
        preferred_owner: str | None,
    ) -> tuple[str, str]:
        complexity = str(interpretation["complexity"])
        if complexity == "complex":
            return "cabinet", "任务跨越多个边界或存在明显风险，由内阁先校准拆解与优先级。"
        if requested_route == "direct" and preferred_owner:
            if complexity == "light":
                return "direct", "目标范围较小，保留人类的执行角色偏好并直接交付。"
            return "collaborate", "任务仍需跨角色校准；总控将人类直派偏好改为协作执行。"
        return "collaborate", "任务可先由总控完成轻量分解，再由执行层协作推进。"

    @staticmethod
    def _task_objective(task: TaskSnapshot) -> str:
        if task.interpretation and task.interpretation.get("objective"):
            return str(task.interpretation["objective"])
        return "把需求转化为可执行、可验证的最小成果闭环。"

    def _decompose_task(self, task: TaskSnapshot, *, objective: str | None = None) -> list[dict[str, Any]]:
        objective = objective or self._task_objective(task)
        criteria = list(task.acceptance) or ["产出有价值且可验证的下一步成果"]
        subtasks = []
        prior_by_lane: dict[str, list[str]] = {}
        for index, role in enumerate(self.worker_roles):
            criterion = criteria[index % len(criteria)]
            lane = self._role_lane(role.id, role.role)
            subtask_id = f"{task.id}:{role.id}:{index + 1}"
            depends_on = self._lane_dependencies(lane, prior_by_lane)
            subtasks.append(
                {
                    "id": subtask_id,
                    "owner": role.id,
                    "role": role.role,
                    "lane": lane,
                    "objective": f"{role.name}负责“{lane}”，围绕“{objective}”交付本执行线成果。",
                    "acceptance": criterion,
                    "deliverable": self._lane_deliverable(lane),
                    "depends_on": depends_on,
                    "status": "assigned",
                }
            )
            prior_by_lane.setdefault(lane, []).append(subtask_id)
        return subtasks

    @staticmethod
    def _role_lane(role_id: str, role_description: str) -> str:
        """Map stable role ids and display descriptions to a user-facing work lane."""
        normalized = f"{role_id} {role_description}".lower()
        if "product" in normalized or "产品" in normalized:
            return "目标与用户价值"
        if "test" in normalized or "qa" in normalized or "测试" in normalized or "质量" in normalized:
            return "验证与风险"
        if "design" in normalized or "ui" in normalized or "设计" in normalized:
            return "交互设计"
        if "dev" in normalized or "engineer" in normalized or "开发" in normalized or "工程" in normalized:
            return "实现"
        return "执行"

    @staticmethod
    def _lane_deliverable(lane: str) -> str:
        if lane == "目标与用户价值":
            return "目标界定、用户价值与验收标准澄清"
        if lane == "实现":
            return "可执行的实现方案或可交付代码改动"
        if lane == "验证与风险":
            return "风险审查、测试策略与发布信心"
        if lane == "交互设计":
            return "可用的交互流程与界面约束"
        return "角色负责的执行成果"

    @staticmethod
    def _lane_dependencies(lane: str, prior_by_lane: dict[str, list[str]]) -> list[str]:
        if lane == "目标与用户价值":
            return []
        if lane == "实现":
            return list(prior_by_lane.get("目标与用户价值", []))
        if lane == "交互设计":
            return list(prior_by_lane.get("目标与用户价值", []))
        if lane == "验证与风险":
            deps = []
            for prior_lane, ids in prior_by_lane.items():
                if prior_lane != "验证与风险":
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
    def _deliverables(value: Any) -> list[dict[str, str]]:
        if not isinstance(value, list):
            return []
        published = []
        for item in value:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            summary = str(item.get("summary") or "").strip()
            if not title or not summary:
                continue
            payload = {"title": title, "summary": summary}
            for field in ("kind", "uri"):
                text = str(item.get(field) or "").strip()
                if text:
                    payload[field] = text
            published.append(payload)
        return published

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
        *,
        cabinet_members: frozenset[str],
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
        cabinet_positions = [item for item in positions if item["actor"] in cabinet_members]
        cabinet = tally_votes(
            cabinet_positions,
            electorate_size=len(cabinet_members),
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
        organization_raw = raw.get("organization")
    else:
        roles_raw = raw
        cabinet_raw = None
        governance_raw = None
        organization_raw = None
    if not isinstance(roles_raw, list):
        raise ValueError("roles config must be a list or an object with roles")
    if not roles_raw:
        raise ValueError("roles config requires at least one role")
    roles = []
    seen_role_ids: set[str] = set()
    for item in roles_raw:
        if not isinstance(item, dict):
            raise ValueError("each role must be an object")
        role_id = item.get("id")
        if not isinstance(role_id, str) or not role_id.strip():
            raise ValueError("each role requires a non-empty string id")
        role_id = role_id.strip()
        if role_id in seen_role_ids:
            raise ValueError(f"duplicate role id: {role_id}")
        seen_role_ids.add(role_id)
        role_name = item.get("name", role_id)
        role_description = item.get("role", role_id)
        driver_name = item.get("driver", role_id)
        charter = item.get("charter", "")
        if not isinstance(role_name, str) or not role_name.strip():
            raise ValueError(f"role {role_id!r} requires a non-empty string name")
        if not isinstance(role_description, str) or not role_description.strip():
            raise ValueError(f"role {role_id!r} requires a non-empty string role")
        if not isinstance(driver_name, str) or not driver_name.strip():
            raise ValueError(f"role {role_id!r} requires a non-empty string driver")
        if not isinstance(charter, str):
            raise ValueError(f"role {role_id!r} charter must be a string")
        roles.append(
            Role(
                id=role_id,
                name=role_name.strip(),
                role=role_description.strip(),
                driver=driver_name.strip(),
                charter=charter.strip(),
            )
        )
    return RuntimeConfig(
        roles=roles,
        cabinet_policy=CabinetPolicy.from_dict(cabinet_raw),
        governance_policy=GovernancePolicy.from_dict(governance_raw),
        organization=_load_organization(organization_raw, roles),
    )


def _load_organization(data: Any, roles: list[Role]) -> Organization:
    role_ids = {role.id for role in roles}
    if data is None:
        return Organization(
            leader=Leader(id="supreme", name="最高决策者", role="最高决策者"),
            cabinet_members=frozenset(role_ids),
            worker_members=frozenset(role_ids),
        )
    if not isinstance(data, dict):
        raise ValueError("organization config must be an object")

    leader_raw = data.get("leader")
    if not isinstance(leader_raw, dict):
        raise ValueError("organization.leader must be an object")
    leader_values = {
        key: leader_raw.get(key)
        for key in ("id", "name", "role")
    }
    if not all(isinstance(value, str) and value.strip() for value in leader_values.values()):
        raise ValueError("organization.leader requires non-empty id, name, and role")

    cabinet_members = _organization_members(data.get("cabinet_members"), "cabinet_members", role_ids)
    worker_members = _organization_members(data.get("worker_members"), "worker_members", role_ids)
    return Organization(
        leader=Leader(**{key: value.strip() for key, value in leader_values.items()}),
        cabinet_members=frozenset(cabinet_members),
        worker_members=frozenset(worker_members),
    )


def _organization_members(value: Any, label: str, known_roles: set[str]) -> set[str]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"organization.{label} requires a non-empty role id list")
    members = [item.strip() for item in value if isinstance(item, str) and item.strip()]
    if len(members) != len(value) or len(set(members)) != len(members):
        raise ValueError(f"organization.{label} requires unique non-empty role ids")
    unknown = sorted(set(members) - known_roles)
    if unknown:
        raise ValueError(f"organization.{label} names unknown role(s): {', '.join(unknown)}")
    return set(members)


def load_roles(path: str | Path | None = None) -> list[Role]:
    return load_runtime_config(path).roles


def _pack_path(name: str) -> Path:
    return Path(str(resources.files("cater").joinpath("packs", name)))
