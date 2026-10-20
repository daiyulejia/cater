"""Private stdio relay. No tools, database, filesystem actions or retries."""

from __future__ import annotations

import json
import os
import socket
import sys


LIMIT = 4 * 1024 * 1024


def main():
    port = int(os.environ.pop("CATER_MCP_PORT"))
    capability = os.environ.pop("CATER_MCP_CAPABILITY")
    timeout = float(os.environ.pop("CATER_MCP_TIMEOUT"))
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as connection:
        with connection.makefile("rb") as incoming:
            def exchange(line):
                connection.sendall(line)
                reply = incoming.readline(LIMIT + 2)
                if len(reply) > LIMIT + 1 or not reply.endswith(b"\n"):
                    raise ValueError("Invalid host frame")
                return json.loads(reply.decode("utf-8"))

            hello = json.dumps({"capability": capability}).encode() + b"\n"
            if exchange(hello) != {"authenticated": True}:
                raise ValueError("Host authentication failed")
            del capability, hello
            while True:
                line = sys.stdin.buffer.readline(LIMIT + 2)
                if not line:
                    return
                if len(line) > LIMIT + 1 or not line.endswith(b"\n"):
                    raise ValueError("Invalid client frame")
                reply = exchange(line)
                if reply.get("response") is not None:
                    try:
                        body = json.dumps(reply["response"], ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"
                    except UnicodeEncodeError:
                        body = json.dumps(reply["response"], ensure_ascii=True, allow_nan=False).encode() + b"\n"
                    if len(body) > LIMIT + 1:
                        raise ValueError("Response exceeds limit")
                    sys.stdout.buffer.write(body)
                    sys.stdout.buffer.flush()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Do not expose local launch secrets or tool arguments on protocol pipes.
        sys.exit(1)
