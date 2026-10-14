from __future__ import annotations

import json
import os
import sys


history = []
for line in sys.stdin:
    request = json.loads(line)
    response = {"id": request["id"], "ok": True}
    if request["type"] == "ping":
        response["pong"] = True
    else:
        if request["context"].get("crash"):
            sys.exit(7)
        history.append(request["prompt"])
        response.update(text=json.dumps({"pid": os.getpid(), "history": history}), tokens=1)
    print(json.dumps(response), flush=True)
