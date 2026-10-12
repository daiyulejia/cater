"""Command-line interface for Cater."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .ladder import decision_ladder
from .runtime import Runtime
from .web import event_to_dict, serve, task_to_dict

DEFAULT_DB = Path.cwd() / ".cater" / "cater.db"


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
    )


def resolve_audit_key(args: argparse.Namespace) -> str | None:
    if args.audit_key:
        return args.audit_key
    if args.audit_key_file:
        return Path(args.audit_key_file).read_text(encoding="utf-8").strip()
    return os.environ.get("CATER_AUDIT_KEY")


def cmd_new(args: argparse.Namespace) -> int:
    with open_runtime(args) as rt:
        task_id = rt.create_task(args.goal, args.accept or [])
        print(f"created {task_id}")
        if args.dispatch:
            rt.dispatch(task_id)
            print("mission dispatched")
        if args.assign_to:
            rt.assign(
                task_id,
                args.assign_to,
                objective=args.assign_objective,
                acceptance=args.assign_accept,
            )
            print(f"assigned to {args.assign_to}")
        if args.run or args.battle:
            result = rt.battle(task_id) if args.battle else rt.collaborate(task_id)
            task = rt.task(task_id)
            print(f"round complete: {task.status}, brief event #{result['brief_event']}")
            print(result["brief"]["dispute"])
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    with open_runtime(args) as rt:
        result = rt.collaborate(args.task_id)
        task = rt.task(args.task_id)
        print(f"round complete: {task.status}, brief event #{result['brief_event']}")
        print(json.dumps(result["brief"], ensure_ascii=False, indent=2))
    return 0


def cmd_battle(args: argparse.Namespace) -> int:
    with open_runtime(args) as rt:
        result = rt.battle(args.task_id)
        task = rt.task(args.task_id)
        print(f"governance round complete: {task.status}, brief event #{result['brief_event']}")
        print(json.dumps(result["brief"], ensure_ascii=False, indent=2))
    return 0


def cmd_dispatch(args: argparse.Namespace) -> int:
    with open_runtime(args) as rt:
        result = rt.dispatch(args.task_id)
        print(f"mission dispatched for {args.task_id}")
        print(json.dumps(result["plan"], ensure_ascii=False, indent=2))
    return 0


def cmd_assign(args: argparse.Namespace) -> int:
    with open_runtime(args) as rt:
        result = rt.assign(
            args.task_id,
            args.owner,
            objective=args.objective,
            acceptance=args.acceptance,
        )
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
            print(f"#{event.seq:<3} {event.type:<20} {event.actor.id:<12} {str(text)[:80]}")
    return 0


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


def cmd_demo(args: argparse.Namespace) -> int:
    with open_runtime(args) as rt:
        task_id = rt.create_task(
            "Build a first Cater demo that proves agents can collaborate and decide autonomously",
            ["three roles contribute", "cabinet policy records the next action"],
        )
        rt.dispatch(task_id)
        result = rt.collaborate(task_id)
        print(f"task: {task_id}")
        print("mission: dispatched")
        print(json.dumps(result["brief"], ensure_ascii=False, indent=2))
        task = rt.task(task_id)
        print(f"\nagent decision: {task.decision['resolution']} ({task.decision['reason']})")
        print("\nhuman inbox:")
        inbox = rt.inbox()
        if not inbox:
            print("- empty")
        for item in inbox:
            print(f"- {item['task_id']}: {item['title']}")
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
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cater")
    parser.add_argument("--db", help="SQLite database path")
    parser.add_argument("--drivers", help="driver config JSON")
    parser.add_argument("--roles", help="role config JSON")
    parser.add_argument("--driver-attempts", type=int, default=2, help="driver retry attempts before containment")
    parser.add_argument("--circuit-breaker-threshold", type=int, default=2, help="failures before a driver circuit opens")
    parser.add_argument("--audit-key", help="HMAC audit key")
    parser.add_argument("--audit-key-file", help="file containing HMAC audit key")
    parser.add_argument("--audit-key-id", default="local", help="audit key identifier")
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

    p_export = sub.add_parser("export", help="export replay data as JSON or JSONL")
    p_export.add_argument("task_id", nargs="?")
    p_export.add_argument("--format", choices=["json", "jsonl"], default="json")
    p_export.set_defaults(func=cmd_export)

    p_review = sub.add_parser("review", help="review work from multiple product angles")
    p_review.add_argument("task_id", nargs="?")
    p_review.add_argument("--record", action="store_true", help="append review.generated to the event log")
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

    sub.add_parser("chain", help="verify event hash chain").set_defaults(func=cmd_chain)
    sub.add_parser("drivers", help="show roles and configured drivers").set_defaults(func=cmd_drivers)
    sub.add_parser("policy", help="show organization governance policy").set_defaults(func=cmd_policy)

    p_probe = sub.add_parser("probe-driver", help="probe a configured driver")
    p_probe.add_argument("name")
    p_probe.set_defaults(func=cmd_probe_driver)

    sub.add_parser("demo", help="run a deterministic demo").set_defaults(func=cmd_demo)

    p_web = sub.add_parser("web", help="run local replay web UI")
    p_web.add_argument("--host", default="127.0.0.1")
    p_web.add_argument("--port", type=int, default=8765)
    p_web.set_defaults(func=cmd_web)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
