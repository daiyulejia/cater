"""Reproducible local query benchmark; timings are evidence, not performance promises."""

from __future__ import annotations

import argparse
import gc
import json
import sys
import time
import tracemalloc
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cater.events import Actor
from cater.runtime import Runtime
from cater.usage import valid_token_count


def measure(query):
    gc.collect()
    tracemalloc.start()
    started = time.perf_counter()
    tokens = query()
    elapsed = time.perf_counter() - started
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {"tokens": tokens, "elapsed_ms": round(elapsed * 1000, 3), "peak_python_bytes": peak}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=int, default=128)
    args = parser.parse_args()
    if not 1 <= args.artifacts <= 4096:
        parser.error("--artifacts must be between 1 and 4096")
    with Runtime(":memory:") as rt:
        for _ in range(args.artifacts):
            rt.log.emit(actor=Actor.agent("product", "worker"), type="deliverable.published", task_id="fixture", payload={"content": "x" * 65536})
        for _ in range(10):
            rt.log.emit(actor=Actor.agent("product", "worker"), type="msg.said", task_id="fixture", payload={"driver": "product", "tokens": 5, "token_usage": "reported"})
        baseline = measure(lambda: sum(event.payload["tokens"] for event in rt.log.read() if event.type in {"msg.said", "msg.challenge"} and event.payload.get("driver") == "product" and event.payload.get("token_usage") == "reported" and valid_token_count(event.payload.get("tokens"))))
        selected = measure(lambda: rt._reported_driver_tokens("product"))
        if baseline["tokens"] != selected["tokens"]:
            raise AssertionError("query totals differ")
        print(json.dumps({"artifacts": args.artifacts, "artifact_bytes_each": 65536, "baseline_full_read": baseline, "selected_stream": selected}, indent=2))


if __name__ == "__main__":
    main()
