"""Local UI QA with deliberate HTTP latency and an independent demo database."""

from __future__ import annotations

import argparse
import sys
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from cater.runtime import Runtime
from cater.web import make_handler


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8774)
    parser.add_argument("--delay", type=float, default=2)
    parser.add_argument("--db", type=Path, default=ROOT / ".cater" / "qa" / "workroom.db")
    args = parser.parse_args()
    args.db.parent.mkdir(parents=True, exist_ok=True)
    with Runtime(args.db) as rt:
        if not rt.tasks():
            for goal in ("整理猫咖活动网页", "复盘消息重复发送的问题"):
                task_id = rt.create_task(goal)
                rt.intake(task_id)
                rt.collaborate(task_id)
    base = make_handler(args.db, static_dir=ROOT / "cater" / "static" / "workroom")

    class Handler(base):
        def do_GET(self) -> None:
            if self.path.startswith("/api/tasks/"):
                time.sleep(args.delay)
            try:
                super().do_GET()
            except ConnectionError:
                pass

        def do_POST(self) -> None:
            if self.path.endswith("/command"):
                time.sleep(args.delay * 2)
            try:
                super().do_POST()
            except ConnectionError:
                pass

    with ThreadingHTTPServer(("127.0.0.1", args.port), Handler) as server:
        print(f"Delayed QA workroom: http://127.0.0.1:{args.port}/", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
