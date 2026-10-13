from __future__ import annotations

import json
import sys


def send(data):
    print(json.dumps(data), flush=True)


for line in sys.stdin:
    req = json.loads(line)
    rid = req.get("id")
    if req.get("type") == "ping":
        send({"id": rid, "ok": True, "pong": True})
        continue
    if req.get("type") == "complete":
        if (req.get("context") or {}).get("crash"):
            sys.exit(7)
        if (req.get("context") or {}).get("oversized"):
            print("x" * (4 * 1024 * 1024 + 1), flush=True)
            continue
        send(
            {
                "id": rid,
                "ok": True,
                "text": json.dumps(
                    {
                        "stance": "assert",
                        "option_id": "F",
                        "claim": "Fixture driver responded",
                        "evidence": ["ndjson fixture"],
                        "confidence": 0.9,
                        "text": "Fixture driver completed the request.",
                    }
                ),
                "tokens": 11,
                "meta": {"fixture": True},
            }
        )
        continue
    send({"id": rid, "ok": False, "error": "unknown request"})
