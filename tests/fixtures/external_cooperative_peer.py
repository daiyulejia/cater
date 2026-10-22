"""Offline ACP peer with an explicit prompt gate and a real MCP connection."""

import json
from pathlib import Path
import socket
import sys
import time


def main():
    root = Path(sys.argv[1])
    session_id = root.name
    connection = stream = None
    config = None

    def record(value):
        with (root / "wire.jsonl").open("a", encoding="utf-8") as target:
            target.write(json.dumps(value) + "\n")

    def send(value):
        sys.stdout.buffer.write(json.dumps(value).encode() + b"\n")
        sys.stdout.buffer.flush()

    def rpc(value):
        connection.sendall(json.dumps(value).encode() + b"\n")
        response = json.loads(stream.readline())
        record({"mcp": response})
        return response

    try:
        for line in sys.stdin.buffer:
            request = json.loads(line)
            record(request)
            method = request.get("method")
            if method == "initialize":
                result = {"protocolVersion": 1, "agentInfo": {"name": "qoder-cli-cn"}}
            elif method == "session/new":
                config = request["params"]["mcpServers"][0]
                result = {"sessionId": session_id, "models": {
                    "currentModelId": "chosen", "availableModels": [
                        {"modelId": "chosen", "name": "ExactModel"}]}}
            elif method == "session/prompt":
                (root / "prompt-received").touch()
                while not (root / "release").exists():
                    time.sleep(0.002)
                env = {item["name"]: item["value"] for item in config["env"]}
                connection = socket.create_connection(("127.0.0.1", int(env["CATER_MCP_PORT"])), timeout=5)
                stream = connection.makefile("rb")
                rpc({"capability": env["CATER_MCP_CAPABILITY"]})
                rpc({"jsonrpc": "2.0", "id": "init", "method": "initialize",
                     "params": {"protocolVersion": "2025-06-18"}})
                rpc({"jsonrpc": "2.0", "method": "notifications/initialized"})
                for index in (1, 2):
                    rpc({"jsonrpc": "2.0", "id": index, "method": "tools/call",
                         "params": {"name": "save", "arguments": {"value": index}}})
                send({"jsonrpc": "2.0", "method": "session/update", "params": {
                    "sessionId": session_id, "update": {"sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": session_id}}}})
                result = {"stopReason": "end_turn"}
            else:
                continue
            send({"jsonrpc": "2.0", "id": request["id"], "result": result})
    finally:
        if stream is not None:
            stream.close()
        if connection is not None:
            connection.close()


if __name__ == "__main__":
    main()
