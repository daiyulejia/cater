"""Command-line interface for Cater."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .runtime import Runtime
from .web import serve

DEFAULT_DB = Path.cwd() / ".cater" / "cater.db"


def open_runtime(args: argparse.Namespace) -> Runtime:
    db = Path(args.db or DEFAULT_DB)
    db.parent.mkdir(parents=True, exist_ok=True)
    return Runtime(db, drivers_path=args.drivers, roles_path=args.roles)


def cmd_new(args: argparse.Namespace) -> int:
    with open_runtime(args) as rt:
        task_id = rt.create_task(args.goal, args.accept or [])
        print(f"created {task_id}")
        if args.battle:
            result = rt.battle(task_id)
            print(f"escalated: brief event #{result['brief_event']}")
            print(result["brief"]["dispute"])
    return 0


def cmd_battle(args: argparse.Namespace) -> int:
    with open_runtime(args) as rt:
        result = rt.battle(args.task_id)
        print(f"escalated: brief event #{result['brief_event']}")
        print(json.dumps(result["brief"], ensure_ascii=False, indent=2))
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
            if task.driver_failures:
                print(f"driver_failures={len(task.driver_failures)}")
            if task.cabinet:
                print(
                    "cabinet="
                    f"{task.cabinet['status']} "
                    f"threshold={task.cabinet['approval_threshold']} "
                    f"recommendation={task.cabinet['recommendation']}"
                )
            if task.brief:
                print(f"brief: {task.brief['dispute']}")
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


def cmd_inbox(args: argparse.Namespace) -> int:
    with open_runtime(args) as rt:
        items = rt.inbox()
        if not items:
            print("inbox empty")
            return 0
        for item in items:
            print(f"{item['task_id']} {item['kind']}: {item['title']}")
    return 0


def cmd_decide(args: argparse.Namespace) -> int:
    with open_runtime(args) as rt:
        event = rt.decide(args.task_id, args.resolution, args.reason)
        print(f"decision recorded at event #{event.seq}")
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
            "Build a first Cater demo that proves agents can disagree and escalate",
            ["three roles respond", "a human decision brief is generated"],
        )
        result = rt.battle(task_id)
        print(f"task: {task_id}")
        print(json.dumps(result["brief"], ensure_ascii=False, indent=2))
        print("\ninbox:")
        for item in rt.inbox():
            print(f"- {item['task_id']}: {item['title']}")
    return 0


def cmd_web(args: argparse.Namespace) -> int:
    db = Path(args.db or DEFAULT_DB)
    db.parent.mkdir(parents=True, exist_ok=True)
    serve(db, host=args.host, port=args.port, drivers_path=args.drivers, roles_path=args.roles)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cater")
    parser.add_argument("--db", help="SQLite database path")
    parser.add_argument("--drivers", help="driver config JSON")
    parser.add_argument("--roles", help="role config JSON")
    sub = parser.add_subparsers(dest="command", required=True)

    p_new = sub.add_parser("new", help="create a task")
    p_new.add_argument("goal")
    p_new.add_argument("--accept", action="append")
    p_new.add_argument("--battle", action="store_true")
    p_new.set_defaults(func=cmd_new)

    p_battle = sub.add_parser("battle", help="run department battle")
    p_battle.add_argument("task_id")
    p_battle.set_defaults(func=cmd_battle)

    p_show = sub.add_parser("show", help="show tasks")
    p_show.add_argument("task_id", nargs="?")
    p_show.set_defaults(func=cmd_show)

    p_events = sub.add_parser("events", help="show event log")
    p_events.add_argument("task_id", nargs="?")
    p_events.set_defaults(func=cmd_events)

    sub.add_parser("inbox", help="show pending human decisions").set_defaults(func=cmd_inbox)

    p_decide = sub.add_parser("decide", help="record human decision")
    p_decide.add_argument("task_id")
    p_decide.add_argument("--resolution", choices=["done", "rework"], required=True)
    p_decide.add_argument("--reason", required=True)
    p_decide.set_defaults(func=cmd_decide)

    sub.add_parser("chain", help="verify event hash chain").set_defaults(func=cmd_chain)
    sub.add_parser("drivers", help="show roles and configured drivers").set_defaults(func=cmd_drivers)

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
