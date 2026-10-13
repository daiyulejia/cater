"""Small zero-dependency Cater Web API client example."""

from __future__ import annotations

import argparse
import json
from urllib.request import Request, urlopen


def post_json(base_url: str, path: str, payload: dict) -> dict:
    request = Request(
        base_url.rstrip("/") + path,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def get_json(base_url: str, path: str) -> dict:
    with urlopen(base_url.rstrip("/") + path, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Create and run a Cater task through the Web API.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--goal", default="Create a replayable task through the Cater API")
    args = parser.parse_args()

    created = post_json(
        args.base_url,
        "/api/tasks",
        {
            "goal": args.goal,
            "acceptance": ["task can be inspected through the API"],
            "assign_to": "dev",
            "assign_objective": "Execute the smallest useful API-driven path",
        },
    )
    task_id = created["task"]["id"]

    ran = post_json(args.base_url, f"/api/tasks/{task_id}/run", {})
    chain = get_json(args.base_url, "/api/chain")

    print(f"task={task_id}")
    print(f"status={ran['task']['status']}")
    print(f"messages={len(ran['task']['messages'])}")
    print(f"chain_ok={chain['ok']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
