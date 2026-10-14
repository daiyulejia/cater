"""Command-line interface for Cater."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from . import __version__
from .events import Actor
from .ladder import decision_ladder
from .messages import message_relation
from .path import organization_path
from .runtime import Runtime
from .verification import run_check
from .web import event_to_dict, serve, task_to_dict

DEFAULT_DB = Path.cwd() / ".cater" / "cater.db"


def _configure_utf8_output(*streams: Any) -> None:
    """Prefer one predictable encoding for the Chinese-first command surface."""
    for stream in streams:
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8")
            except (OSError, ValueError):
                pass

DIRECT_DEMO_DRIVERS = {
    "drivers": [
        {
            "kind": "scripted",
            "name": "product",
            "tokens_per_call": 80,
            "script": [
                '{"stance":"assert","option_id":"A","claim":"保持任务小而可检查","evidence":["低风险工作直接指派即可"],"confidence":0.88,"text":"这个任务可以走轻量直派路径。","deliverables":[{"title":"轻量路径判断","summary":"当前任务边界清晰，无需启动内阁治理。","kind":"brief"}]}'
            ],
        },
        {
            "kind": "scripted",
            "name": "dev",
            "tokens_per_call": 80,
            "script": [
                '{"stance":"assert","option_id":"A","claim":"实现最小有用路径","evidence":["任务有明确负责人和有限的验收检查"],"confidence":0.9,"text":"我可以直接执行并汇报结果。","deliverables":[{"title":"执行承诺","summary":"将按已分配目标交付最小可用实现。","kind":"plan"}]}'
            ],
        },
        {
            "kind": "scripted",
            "name": "test",
            "tokens_per_call": 80,
            "script": [
                '{"stance":"assert","option_id":"A","claim":"可回放的冒烟检查已足够","evidence":["当前没有冲突需求或高风险行为"],"confidence":0.86,"text":"本轮不需要升级到治理层。","deliverables":[{"title":"验证结论","summary":"以可回放冒烟检查作为本轮验收证据。","kind":"checklist"}]}'
            ],
        },
    ]
}


def open_runtime(args: argparse.Namespace) -> Runtime:
    db = Path(args.db or DEFAULT_DB)
    db.parent.mkdir(parents=True, exist_ok=True)
    return Runtime(
        db,
        drivers_path=args.drivers,
        roles_path=args.roles,
        driver_attempts=args.driver_attempts,
        circuit_breaker_threshold=args.circuit_breaker_threshold,
        audit_key=resolve_audit_key(args),
        audit_key_id=args.audit_key_id,
        audit_keys=resolve_audit_keys(args),
    )


def resolve_audit_key(args: argparse.Namespace) -> str | None:
    if args.audit_key:
        return args.audit_key
    if args.audit_key_file:
        return Path(args.audit_key_file).read_text(encoding="utf-8").strip()
    return os.environ.get("CATER_AUDIT_KEY")


def resolve_audit_keys(args: argparse.Namespace) -> dict[str, str] | None:
    if not args.audit_keyring_file:
        return None
    try:
        data = json.loads(Path(args.audit_keyring_file).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("audit keyring file must contain a JSON object") from exc
    if not isinstance(data, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in data.items()):
        raise ValueError("audit keyring file must map key ids to string keys")
    return data


def resolve_web_access_token(args: argparse.Namespace) -> str | None:
    if args.access_token:
        return args.access_token
    if args.access_token_file:
        return Path(args.access_token_file).read_text(encoding="utf-8").strip()
    return os.environ.get("CATER_WEB_ACCESS_TOKEN")


def cmd_new(args: argparse.Namespace) -> int:
    with open_runtime(args) as rt:
        if args.run and args.battle:
            print("choose either --run for normal collaboration or --battle for forced governance")
            return 1
        if args.assign_to and args.assign_to not in {role.id for role in rt.worker_roles}:
            print(_unknown_role_message(args.assign_to, rt))
            return 1
        task_id = rt.create_task(args.goal, args.accept or [])
        print(f"created {task_id}")
        if args.dispatch:
            rt.dispatch(task_id)
            print("mission dispatched")
        elif args.assign_to:
            intake = rt.intake(
                task_id,
                requested_route="direct",
                preferred_owner=args.assign_to,
                preferred_objective=args.assign_objective,
                preferred_acceptance=args.assign_accept,
            )
            print(f"leader route: {intake['route']['route']}")
            if intake["route"]["route"] == "direct":
                print(f"assigned to {args.assign_to}")
        elif args.run or args.battle:
            intake = rt.intake(task_id)
            print(f"leader route: {intake['route']['route']}")
        if args.run or args.battle:
            result = rt.battle(task_id) if args.battle else rt.collaborate(task_id)
            task = rt.task(task_id)
            print(f"round complete: {task.status}, brief event #{result['brief_event']}")
            print(result["brief"]["dispute"])
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    with open_runtime(args) as rt:
        task = rt.task(args.task_id)
        if task is None:
            raise KeyError(args.task_id)
        if task.route is None and not task.assignments:
            intake = rt.intake(args.task_id)
            print(f"leader route: {intake['route']['route']}")
        result = rt.collaborate(args.task_id)
        task = rt.task(args.task_id)
        print(f"round complete: {task.status}, brief event #{result['brief_event']}")
        print(json.dumps(result["brief"], ensure_ascii=False, indent=2))
    return 0


def cmd_battle(args: argparse.Namespace) -> int:
    with open_runtime(args) as rt:
        task = rt.task(args.task_id)
        if task is None:
            raise KeyError(args.task_id)
        if task.route is None and not task.assignments:
            intake = rt.intake(args.task_id)
            print(f"leader route: {intake['route']['route']}")
        result = rt.battle(args.task_id)
        task = rt.task(args.task_id)
        print(f"governance round complete: {task.status}, brief event #{result['brief_event']}")
        print(json.dumps(result["brief"], ensure_ascii=False, indent=2))
    return 0


def cmd_dispatch(args: argparse.Namespace) -> int:
    with open_runtime(args) as rt:
        result = rt.dispatch(args.task_id)
        print(f"规划已排队: {args.task_id}" if "route" in result else f"mission dispatched for {args.task_id}")
        print(json.dumps(result.get("plan", result), ensure_ascii=False, indent=2))
    return 0


def cmd_assign(args: argparse.Namespace) -> int:
    with open_runtime(args) as rt:
        try:
            task = rt.task(args.task_id)
            if task is None:
                raise KeyError(args.task_id)
            if task.route is None and not task.assignments:
                intake = rt.intake(
                    args.task_id,
                    requested_route="direct",
                    preferred_owner=args.owner,
                    preferred_objective=args.objective,
                    preferred_acceptance=args.acceptance,
                )
                route = intake["route"]["route"]
                if route != "direct":
                    print(f"leader route: {route}")
                    return 0
                task = rt.task(args.task_id)
                assert task is not None
                result = {"assignment": task.assignments[-1]}
            else:
                result = rt.assign(
                    args.task_id,
                    args.owner,
                    objective=args.objective,
                    acceptance=args.acceptance,
                )
        except ValueError:
            print(_unknown_role_message(args.owner, rt))
            return 1
        print(f"assigned {args.task_id} to {args.owner}")
        print(json.dumps(result["assignment"], ensure_ascii=False, indent=2))
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    with open_runtime(args) as rt:
        if args.task_id:
            task = rt.task(args.task_id)
            if task is None:
                print(f"not found: {args.task_id}")
                return 1
            print(f"{task.id} [{task.status}] {task.goal}")
            print(f"messages={len(task.messages)} tokens={task.tokens}")
            if task.assignments:
                print(f"assignments={len(task.assignments)}")
            if task.work:
                print("工作项（阶段通过不等于整体完成）：")
                for item in task.work["items"]:
                    print(f"  - {item['id']} [{item['status']}] {item['owner']} -> {item['reviewer'] or '待安排复核'}")
                    if item["reason"]:
                        print(f"    {item['reason']}")
            if task.driver_failures:
                print(f"driver_failures={len(task.driver_failures)}")
            if task.cabinet:
                print(
                    "cabinet="
                    f"{task.cabinet['status']} "
                    f"threshold={task.cabinet['approval_threshold']} "
                    f"recommendation={task.cabinet['recommendation']}"
                )
            if task.proposal:
                print(f"proposal={task.proposal['option_id']} reason={task.proposal['reason']}")
            if task.brief:
                print(f"brief: {task.brief['dispute']}")
            if task.messages:
                print("recent messages:")
                for message in task.messages[-5:]:
                    print("  - " + _message_summary(message))
            ladder = decision_ladder(rt.log.read(task_id=args.task_id))
            print("ladder:")
            for step in ladder:
                print(f"  - {step['label']}: {step['status']} ({step['detail']})")
            return 0
        for task in rt.tasks():
            print(f"{task.id} [{task.status}] {task.goal}")
    return 0


def cmd_events(args: argparse.Namespace) -> int:
    with open_runtime(args) as rt:
        for event in rt.log.read(task_id=args.task_id):
            text = event.payload.get("text") or event.payload.get("goal") or event.payload.get("reason") or ""
            relation = message_relation(event.payload)
            suffix = f" [{relation}]" if relation else ""
            print(f"#{event.seq:<3} {event.type:<20} {event.actor.id:<12} {str(text)[:80]}{suffix}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    command = list(args.command_args)
    if command and command[0] == "--":
        command.pop(0)
    with open_runtime(args) as rt:
        if rt.task(args.task_id) is None:
            raise KeyError(args.task_id)
        if args.delivery:
            if args.work_item or args.for_event is not None or not args.criterion:
                raise ValueError("--delivery requires --criterion and cannot combine with --work-item or --for-event")
            event, result = rt.delivery.verify(args.task_id, args.criterion, command,
                                               timeout=args.timeout, record_output=args.record_output)
            print(f"recorded delivery check #{event.seq}: {event.payload['outcome']}")
            print("Executed in a temporary delivery copy, not an OS sandbox.")
            for preview in (result.stdout_preview, result.stderr_preview):
                if preview:
                    print(preview)
            return 0 if event.payload["outcome"] == "passed" and event.payload["artifacts_unchanged"] else 1
        if args.work_item is not None:
            if not args.criterion or args.for_event is not None:
                raise ValueError("--work-item requires --criterion and cannot combine with --for-event")
            event, result = rt.verify_work(args.task_id, args.work_item, args.criterion, command,
                                           timeout=args.timeout, record_output=args.record_output)
            print(f"recorded work check #{event.seq}: {event.payload['outcome']}")
            print("Executed in a temporary artifact copy, not an OS sandbox.")
            for preview in (result.stdout_preview, result.stderr_preview):
                if preview:
                    print(preview)
            return 0 if event.payload["outcome"] == "passed" and event.payload["artifacts_unchanged"] else 1
        if args.criterion:
            raise ValueError("--criterion requires --work-item or --delivery")
        if args.for_event is not None:
            target = next(
                (event for event in rt.log.read(task_id=args.task_id) if event.seq == args.for_event),
                None,
            )
            if target is None or target.type not in {"diagnosis.recorded", "handoff.published"}:
                raise ValueError("--for-event must identify a diagnosis or handoff in this task")
        result = run_check(
            command, cwd=Path(args.cwd), timeout=args.timeout,
            record_output=args.record_output,
        )
        payload = dict(result.payload)
        if args.for_event is not None:
            payload["for_event"] = args.for_event
        event = rt.log.emit(
            actor=Actor.system("local_verifier"), type="verification.completed",
            task_id=args.task_id, payload=payload,
        )
        print(f"recorded check #{event.seq}: {payload['outcome']} (exit={payload['exit_code']})")
        for label, preview in (("stdout", result.stdout_preview), ("stderr", result.stderr_preview)):
            if preview:
                print(f"{label} preview (up to 8192 bytes):\n{preview}")
        return 0 if payload["outcome"] == "passed" else 1


def cmd_export(args: argparse.Namespace) -> int:
    with open_runtime(args) as rt:
        events = rt.log.read(task_id=args.task_id)
        if args.format == "jsonl":
            for event in events:
                print(json.dumps(event_to_dict(event), ensure_ascii=False, sort_keys=True))
            return 0

        tasks = [task for task in rt.tasks() if args.task_id is None or task.id == args.task_id]
        report = rt.verify_chain()
        body = {
            "chain": {
                "ok": report.ok,
                "checked": report.checked,
                "signed": report.signed,
                "head_hash": report.head_hash,
                "reason": report.reason,
            },
            "tasks": [task_to_dict(task) for task in tasks],
            "events": [event_to_dict(event) for event in events],
            "profiles": rt.profiles(),
        }
        print(json.dumps(body, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    with open_runtime(args) as rt:
        review = rt.review(args.task_id, record=args.record)
        if args.next:
            steps = review.get("next_steps") or []
            if not steps:
                print("no immediate action")
                return 0
            step = steps[0]
            print(
                f"{step.get('priority', 'medium')}/{step.get('label', step.get('source', 'review'))}: "
                f"{step.get('action')}"
            )
            return 0
        if args.plain:
            print(f"health: {review['health']}")
            print(f"summary: {review['summary']}")
            print("lenses:")
            for lens in review.get("lenses", []):
                print(f"  - {lens['label']}: {lens['status']} - {lens['finding']}")
                if lens.get("action"):
                    print(f"    next: {lens['action']}")
            actions = review.get("next_actions", [])
            print("next actions:")
            if not actions:
                print("  - none")
            steps = review.get("next_steps") or []
            if steps:
                for step in steps:
                    print(
                        "  - "
                        f"[{step.get('priority', 'medium')}/{step.get('label', step.get('source', 'review'))}] "
                        f"{step.get('action')}"
                    )
            else:
                for action in actions:
                    print(f"  - {action}")
            return 0
        print(json.dumps(review, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_inbox(args: argparse.Namespace) -> int:
    with open_runtime(args) as rt:
        items = rt.inbox()
        if not items:
            print("inbox empty")
            return 0
        for item in items:
            print(f"{item['task_id']} {item['kind']}: {item['title']}")
    return 0


def cmd_profiles(args: argparse.Namespace) -> int:
    with open_runtime(args) as rt:
        profiles = rt.profiles()
        if not profiles:
            print("no agent activity")
            return 0
        for profile in profiles.values():
            print(
                f"{profile['id']:<12} role={profile['role']:<12} "
                f"messages={profile['messages']} supports={profile['supports']} "
                f"challenges={profile['challenges']} failures={profile['driver_failures']}"
            )
            if profile["last_claim"]:
                print(f"  last: {profile['last_claim']}")
    return 0


def cmd_command(args: argparse.Namespace) -> int:
    with open_runtime(args) as rt:
        acceptance = args.accept if args.accept is not None else None
        event = rt.command(args.task_id, args.instruction, goal=args.goal, acceptance=acceptance)
        print(f"command recorded at event #{event.seq}")
    return 0


def cmd_decide(args: argparse.Namespace) -> int:
    with open_runtime(args) as rt:
        event = rt.decide(args.task_id, args.resolution, args.reason)
        print(f"manual override recorded at event #{event.seq}")
    return 0


def cmd_chain(args: argparse.Namespace) -> int:
    with open_runtime(args) as rt:
        report = rt.verify_chain()
        if args.json:
            print(
                json.dumps(
                    {
                        "ok": report.ok,
                        "checked": report.checked,
                        "signed": report.signed,
                        "head_hash": report.head_hash,
                        "broken_at": report.broken_at,
                        "reason": report.reason,
                    },
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0 if report.ok else 1
        print(report.describe())
        return 0 if report.ok else 1


def cmd_drivers(args: argparse.Namespace) -> int:
    with open_runtime(args) as rt:
        for role in rt.roles:
            driver = rt.drivers[role.driver]
            print(f"{role.id:<12} role={role.role:<12} driver={role.driver:<12} {type(driver).__name__}")
    return 0


def cmd_policy(args: argparse.Namespace) -> int:
    with open_runtime(args) as rt:
        print("governance:")
        print(f"  trigger_on_challenge={rt.governance_policy.trigger_on_challenge}")
        print(f"  trigger_on_option_split={rt.governance_policy.trigger_on_option_split}")
        print(f"  low_confidence_threshold={rt.governance_policy.low_confidence_threshold}")
        print("cabinet:")
        print(f"  approval_threshold={rt.cabinet_policy.approval_threshold}")
        print(f"  quorum={rt.cabinet_policy.quorum}")
        print(f"  veto_roles={','.join(rt.cabinet_policy.veto_roles) or '-'}")
        print("reliability:")
        print(f"  driver_attempts={rt.driver_attempts}")
        print(f"  circuit_breaker_threshold={rt.circuit_breaker_threshold}")
        print("roles:")
        for role in rt.roles:
            print(f"  {role.id}: role={role.role} driver={role.driver}")
    return 0


def cmd_path(args: argparse.Namespace) -> int:
    with open_runtime(args) as rt:
        task = rt.task(args.task_id)
        if task is None:
            raise KeyError(args.task_id)
        events = rt.log.read(task_id=args.task_id)
        path = organization_path(task, events)
        if args.json:
            print(json.dumps(path, ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        for step in path:
            print(f"{step['label']:<14} {step['status']:<18} {step['detail']}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    with open_runtime(args) as rt:
        data = _doctor_report(rt, args, probe=bool(args.probe))
        if args.json:
            print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))
            return 0 if data["ok"] else 1
        print(f"cater={data['cater']}")
        print(f"database={data['database']}")
        print(f"roles={data['roles']}")
        print(f"drivers={data['drivers']}")
        print(f"chain={data['chain']['summary']}")
        print(f"driver_attempts={data['reliability']['driver_attempts']}")
        print(f"circuit_breaker_threshold={data['reliability']['circuit_breaker_threshold']}")
        for role in data["role_checks"]:
            status = role["status"]
            if role["probe"] is not None:
                status += f", {'alive' if role['probe'] else 'unavailable'}"
            elif args.probe:
                status += ", no probe"
            print(f"role {role['id']}: driver={role['driver']} {status}")
        return 0 if data["ok"] else 1


def _doctor_report(rt: Runtime, args: argparse.Namespace, *, probe: bool) -> dict[str, Any]:
    report = rt.verify_chain()
    role_checks = []
    ok = report.ok
    roles = list(rt.roles)
    if rt.organization.leader.driver is not None:
        roles.insert(0, rt.organization.leader)
    for role in roles:
        driver = rt.drivers.get(role.driver)
        status = "configured" if driver is not None else "missing"
        ok = ok and driver is not None
        probe_result = None
        if probe and driver is not None:
            probe_fn = getattr(driver, "probe", None)
            if callable(probe_fn):
                probe_result = bool(probe_fn())
                ok = ok and probe_result
        role_checks.append(
            {
                "id": role.id,
                "role": role.role,
                "driver": role.driver,
                "driver_type": type(driver).__name__ if driver is not None else None,
                "status": status,
                "probe": probe_result,
            }
        )
    return {
        "ok": ok,
        "cater": __version__,
        "database": str(Path(args.db or DEFAULT_DB)),
        "roles": len(roles),
        "drivers": len(rt.drivers),
        "chain": {
            "ok": report.ok,
            "checked": report.checked,
            "signed": report.signed,
            "head_hash": report.head_hash,
            "broken_at": report.broken_at,
            "reason": report.reason,
            "summary": report.describe(),
        },
        "reliability": {
            "driver_attempts": rt.driver_attempts,
            "circuit_breaker_threshold": rt.circuit_breaker_threshold,
        },
        "role_checks": role_checks,
    }


def cmd_probe_driver(args: argparse.Namespace) -> int:
    with open_runtime(args) as rt:
        driver = rt.drivers[args.name]
        probe = getattr(driver, "probe", None)
        if callable(probe):
            ok = bool(probe())
            print(f"{args.name}: {'alive' if ok else 'unavailable'}")
            return 0 if ok else 1
        response = driver.complete(role="probe", prompt="ping", context={"probe": True})
        print(f"{args.name}: complete ok, tokens={response.tokens}, text={response.text[:60]}")
    return 0


def _unknown_role_message(role_id: str, rt: Runtime) -> str:
    known = ", ".join(role.id for role in rt.worker_roles)
    return f"unknown role {role_id!r}; expected one of: {known} (execution roles)"


def _message_summary(message: dict) -> str:
    parts = [
        f"#{message.get('seq')}",
        f"{message.get('actor', 'agent')}/{message.get('stance', '')}",
    ]
    if message.get("target"):
        parts.append(f"-> {message['target']}")
    if message.get("reply_to"):
        parts.append(f"reply_to #{message['reply_to']}")
    claim = str(message.get("claim") or "")
    if claim:
        parts.append(claim[:80])
    return " ".join(parts)


def cmd_demo(args: argparse.Namespace) -> int:
    if args.direct:
        return cmd_demo_direct(args)
    with open_runtime(args) as rt:
        task_id = rt.create_task(
            "完成一场可回放的 Cater 协作演示，证明团队能够自主协作并形成决策",
            ["至少三位伙伴参与", "内阁策略记录下一步行动"],
        )
        rt.dispatch(task_id)
        result = rt.battle(task_id)
        print(f"任务：{task_id}")
        print("任务分发：已完成")
        print(f"本轮摘要：{result['brief']['dispute']}")
        print("最近协作：")
        task = rt.task(task_id)
        for message in task.messages[-5:]:
            print("  - " + _message_summary(message))
        if task.decision:
            print(f"\n团队决策：{task.decision['resolution']} ({task.decision['reason']})")
        else:
            print("\n阶段状态：成果尚待独立验收，演示不代表真实任务完成。")
        print("\n需要顾客确认：")
        inbox = rt.inbox()
        if not inbox:
            print("- 暂无")
        for item in inbox:
            print(f"- {item['task_id']}: {item['title']}")
        print("\n接下来可以：")
        print(f"- cater show {task_id}")
        print(f"- cater events {task_id}")
        print(f"- cater review {task_id} --plain")
        print("- cater chain --json")
        print("- cater web")
        print(f"- open http://127.0.0.1:8765/api/tasks/{task_id} after starting the web UI")
    return 0


def cmd_demo_direct(args: argparse.Namespace) -> int:
    db = Path(args.db or DEFAULT_DB)
    db.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        drivers_path = Path(tmp) / "direct-demo-drivers.json"
        drivers_path.write_text(json.dumps(DIRECT_DEMO_DRIVERS), encoding="utf-8")
        with Runtime(
            db,
            drivers_path=drivers_path,
            roles_path=None,
            driver_attempts=args.driver_attempts,
            circuit_breaker_threshold=args.circuit_breaker_threshold,
            audit_key=resolve_audit_key(args),
            audit_key_id=args.audit_key_id,
            audit_keys=resolve_audit_keys(args),
        ) as rt:
            task_id = rt.create_task(
                "完成一项低风险直派任务，不需要额外治理仪式",
                ["直接负责人清晰可见", "除非出现风险，治理层保持空闲"],
            )
            rt.assign(task_id, "dev", objective="完成最小但有用的直派执行路径")
            result = rt.collaborate(task_id)
            task = rt.task(task_id)
        print(f"任务：{task_id}")
        print("协作路径：执行猫咪直接协作")
        print("已交给：米寇")
        print(f"本轮摘要：{result['brief']['dispute']}")
        print(f"治理状态：{result['brief']['governance']['status']}")
        print("最近协作：")
        for message in task.messages[-5:]:
            print("  - " + _message_summary(message))
        print("\n接下来可以：")
        print(f"- cater show {task_id}")
        print(f"- cater review {task_id} --plain")
        print("- cater web")
    return 0


def cmd_web(args: argparse.Namespace) -> int:
    db = Path(args.db or DEFAULT_DB)
    db.parent.mkdir(parents=True, exist_ok=True)
    serve(
        db,
        host=args.host,
        port=args.port,
        drivers_path=args.drivers,
        roles_path=args.roles,
        driver_attempts=args.driver_attempts,
        circuit_breaker_threshold=args.circuit_breaker_threshold,
        audit_key=resolve_audit_key(args),
        audit_key_id=args.audit_key_id,
        audit_keys=resolve_audit_keys(args),
        allow_remote=args.allow_remote,
        access_token=resolve_web_access_token(args),
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cater")
    parser.add_argument("--version", action="version", version=f"cater {__version__}")
    parser.add_argument("--db", help="SQLite database path")
    parser.add_argument("--drivers", help="driver config JSON")
    parser.add_argument("--roles", help="role config JSON")
    parser.add_argument("--driver-attempts", type=int, default=2, help="driver retry attempts before containment")
    parser.add_argument("--circuit-breaker-threshold", type=int, default=2, help="failures before a driver circuit opens")
    parser.add_argument("--audit-key", help="HMAC audit key")
    parser.add_argument("--audit-key-file", help="file containing HMAC audit key")
    parser.add_argument("--audit-key-id", default="local", help="audit key identifier")
    parser.add_argument("--audit-keyring-file", help="JSON file mapping historical audit key ids to HMAC keys")
    sub = parser.add_subparsers(dest="command", required=True)

    p_new = sub.add_parser("new", help="create a task")
    p_new.add_argument("goal")
    p_new.add_argument("--accept", action="append")
    p_new.add_argument("--dispatch", action="store_true", help="dispatch mission before running")
    p_new.add_argument("--assign-to", help="directly assign the task to one worker role")
    p_new.add_argument("--assign-objective", help="objective for --assign-to")
    p_new.add_argument("--assign-accept", help="acceptance check for --assign-to")
    p_new.add_argument("--run", action="store_true", help="run one collaboration round immediately")
    p_new.add_argument("--battle", action="store_true", help="run one forced governance round immediately")
    p_new.set_defaults(func=cmd_new)

    p_run = sub.add_parser("run", help="run one collaboration round")
    p_run.add_argument("task_id")
    p_run.set_defaults(func=cmd_run)

    p_battle = sub.add_parser("battle", help="force one governance round")
    p_battle.add_argument("task_id")
    p_battle.set_defaults(func=cmd_battle)

    p_dispatch = sub.add_parser("dispatch", help="turn a task into assigned execution lanes")
    p_dispatch.add_argument("task_id")
    p_dispatch.set_defaults(func=cmd_dispatch)

    p_assign = sub.add_parser("assign", help="directly assign a task to one worker role")
    p_assign.add_argument("task_id")
    p_assign.add_argument("owner")
    p_assign.add_argument("--objective")
    p_assign.add_argument("--acceptance")
    p_assign.set_defaults(func=cmd_assign)

    p_show = sub.add_parser("show", help="show tasks")
    p_show.add_argument("task_id", nargs="?")
    p_show.set_defaults(func=cmd_show)

    p_events = sub.add_parser("events", help="show event log")
    p_events.add_argument("task_id", nargs="?")
    p_events.set_defaults(func=cmd_events)

    p_verify = sub.add_parser("verify", help="run an explicit local check and record observed result")
    p_verify.add_argument("task_id")
    p_verify.add_argument("--cwd", default=".", help="working directory for the check")
    p_verify.add_argument("--timeout", type=float, default=60, help="seconds, maximum 600")
    p_verify.add_argument("--for-event", type=int, help="link to a diagnosis or handoff event")
    p_verify.add_argument("--work-item", help="check exact submitted inline artifacts in a temporary copy (not a sandbox)")
    p_verify.add_argument("--delivery", action="store_true", help="check all current delivery files in a temporary copy")
    p_verify.add_argument("--criterion", help="local_check criterion id for --work-item or --delivery")
    p_verify.add_argument("--record-output", action="store_true", help="persist bounded output excerpts; may expose secrets")
    p_verify.set_defaults(func=cmd_verify, command_args=[])

    p_export = sub.add_parser("export", help="export replay data as JSON or JSONL")
    p_export.add_argument("task_id", nargs="?")
    p_export.add_argument("--format", choices=["json", "jsonl"], default="json")
    p_export.set_defaults(func=cmd_export)

    p_review = sub.add_parser("review", help="review work from multiple product angles")
    p_review.add_argument("task_id", nargs="?")
    p_review.add_argument("--record", action="store_true", help="append review.generated to the event log")
    p_review.add_argument("--plain", action="store_true", help="print a human-readable review instead of JSON")
    p_review.add_argument("--next", action="store_true", help="print only the highest-priority next step")
    p_review.set_defaults(func=cmd_review)

    sub.add_parser("inbox", help="show exceptional human input requests").set_defaults(func=cmd_inbox)
    sub.add_parser("profiles", help="show projected agent profiles").set_defaults(func=cmd_profiles)

    p_command = sub.add_parser("command", help="record a human command or goal update")
    p_command.add_argument("task_id")
    p_command.add_argument("instruction")
    p_command.add_argument("--goal")
    p_command.add_argument("--accept", action="append")
    p_command.set_defaults(func=cmd_command)

    p_decide = sub.add_parser("decide", help="record a manual human override")
    p_decide.add_argument("task_id")
    p_decide.add_argument("--resolution", choices=["done", "rework"], required=True)
    p_decide.add_argument("--reason", required=True)
    p_decide.set_defaults(func=cmd_decide)

    p_chain = sub.add_parser("chain", help="verify event hash chain")
    p_chain.add_argument("--json", action="store_true", help="print machine-readable chain status")
    p_chain.set_defaults(func=cmd_chain)
    sub.add_parser("drivers", help="show roles and configured drivers").set_defaults(func=cmd_drivers)
    sub.add_parser("policy", help="show organization governance policy").set_defaults(func=cmd_policy)
    p_path = sub.add_parser("path", help="show a task's organization path")
    p_path.add_argument("task_id")
    p_path.add_argument("--json", action="store_true", help="print machine-readable organization path")
    p_path.set_defaults(func=cmd_path)

    p_doctor = sub.add_parser("doctor", help="check runtime wiring and local event chain")
    p_doctor.add_argument("--probe", action="store_true", help="actively probe drivers that support a probe method")
    p_doctor.add_argument("--json", action="store_true", help="print machine-readable doctor status")
    p_doctor.set_defaults(func=cmd_doctor)

    p_probe = sub.add_parser("probe-driver", help="probe a configured driver")
    p_probe.add_argument("name")
    p_probe.set_defaults(func=cmd_probe_driver)

    p_demo = sub.add_parser("demo", help="run a deterministic demo")
    p_demo.add_argument("--direct", action="store_true", help="show lightweight direct collaboration without governance")
    p_demo.set_defaults(func=cmd_demo)

    p_web = sub.add_parser("web", help="run local replay web UI")
    p_web.add_argument("--host", default="127.0.0.1")
    p_web.add_argument("--port", type=int, default=8765)
    p_web.add_argument(
        "--allow-remote",
        action="store_true",
        help="allow a non-loopback bind; requires an access token and TLS boundary",
    )
    p_web.add_argument("--access-token", help="access token for a remote Cater web control plane")
    p_web.add_argument("--access-token-file", help="file containing the Cater web access token")
    p_web.set_defaults(func=cmd_web)
    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_utf8_output(sys.stdout, sys.stderr)
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--" in argv:
        boundary = argv.index("--")
        prefix = argv[:boundary]
        args = build_parser().parse_args(prefix)
        if args.command == "verify":
            args.command_args = argv[boundary + 1:]
        else:
            args = build_parser().parse_args(argv)
    else:
        args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyError as exc:
        missing = exc.args[0] if exc.args else "requested item"
        print(f"error: not found: {missing}")
        return 1
    except ValueError as exc:
        print(f"error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
