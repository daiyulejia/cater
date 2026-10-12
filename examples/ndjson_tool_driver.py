"""Example NDJSON driver for wrapping an external tool process.

This file is intentionally dependency-free. Before replacing build_response with
Codex CLI, an MCP client, a local skill runner, LangGraph, or any other backend,
inspect that tool's real contract and failure behavior. Do not integrate by
guessing from names alone.
"""

from __future__ import annotations

import json
import sys
from typing import Any


def main() -> int:
    for line in sys.stdin:
        if not line.strip():
            continue
        request = json.loads(line)
        response = handle(request)
        print(json.dumps(response, ensure_ascii=False), flush=True)
    return 0


def handle(request: dict[str, Any]) -> dict[str, Any]:
    request_id = request.get("id")
    kind = request.get("type")
    if kind == "ping":
        return {"id": request_id, "ok": True, "pong": True}
    if kind != "complete":
        return {"id": request_id, "ok": False, "error": f"unsupported request type: {kind}"}

    role = str(request.get("role") or "agent")
    prompt = str(request.get("prompt") or "")
    context = request.get("context") if isinstance(request.get("context"), dict) else {}
    payload = build_response(role=role, prompt=prompt, context=context)
    return {
        "id": request_id,
        "ok": True,
        "text": json.dumps(payload, ensure_ascii=False),
        "tokens": estimate_tokens(prompt, payload),
        "meta": {"adapter": "examples.ndjson_tool_driver"},
    }


def build_response(*, role: str, prompt: str, context: dict[str, Any]) -> dict[str, Any]:
    goal = str(context.get("goal") or "the task")
    # Real adapters may add target/reply_to when they are intentionally
    # continuing or correcting a visible prior event from the prompt.
    return {
        "stance": "assert",
        "option_id": "external-tool",
        "claim": f"{role} can call an external tool for {goal}",
        "evidence": ["NDJSON driver boundary is alive", f"prompt_chars={len(prompt)}"],
        "confidence": 0.7,
        "text": "Replace build_response with an MCP, skill, CLI, or API call.",
    }


def estimate_tokens(prompt: str, payload: dict[str, Any]) -> int:
    text = prompt + json.dumps(payload, ensure_ascii=False)
    return max(1, len(text) // 4)


if __name__ == "__main__":
    raise SystemExit(main())
