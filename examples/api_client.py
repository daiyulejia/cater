"""Small zero-dependency Cater Web API client example."""

from __future__ import annotations

import argparse
import json
import os
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise HTTPError(req.full_url, code, msg, headers, fp)


def _headers(access_token: str | None, *, json_body: bool = False) -> dict[str, str]:
    headers = {"Content-Type": "application/json"} if json_body else {}
    if access_token:
        headers["X-Cater-Access-Token"] = access_token
    return headers


def post_json(base_url: str, path: str, payload: dict, *, access_token: str | None = None) -> dict:
    request = Request(
        base_url.rstrip("/") + path,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers=_headers(access_token, json_body=True),
    )
    with build_opener(_RejectRedirects()).open(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def get_json(base_url: str, path: str, *, access_token: str | None = None) -> dict:
    request = Request(base_url.rstrip("/") + path, headers=_headers(access_token))
    with build_opener(_RejectRedirects()).open(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Create and run a Cater task through the Web API.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--goal", default="Create a replayable task through the Cater API")
    parser.add_argument(
        "--access-token-env",
        default="CATER_WEB_ACCESS_TOKEN",
        help="environment variable containing a Cater web access token; use an empty value to disable",
    )
    args = parser.parse_args()
    access_token = os.environ.get(args.access_token_env) if args.access_token_env else None

    created = post_json(
        args.base_url,
        "/api/tasks",
        {
            "goal": args.goal,
            "acceptance": ["task can be inspected through the API"],
            "assign_to": "dev",
            "assign_objective": "Execute the smallest useful API-driven path",
        },
        access_token=access_token,
    )
    task_id = created["task"]["id"]

    ran = post_json(args.base_url, f"/api/tasks/{task_id}/run", {}, access_token=access_token)
    chain = get_json(args.base_url, "/api/chain", access_token=access_token)

    print(f"task={task_id}")
    print(f"status={ran['task']['status']}")
    print(f"messages={len(ran['task']['messages'])}")
    print(f"chain_ok={chain['ok']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
