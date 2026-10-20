"""Offline Qoder-shaped ACP peer that launches the real Cater MCP relay."""

import json
import os
from pathlib import Path
import subprocess
import sys
import uuid


def main():
    mode, root = sys.argv[1:3]
    root = Path(root)
    session_id = str(uuid.uuid4())
    relay = None

    def record(value):
        with (root / "external-wire.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(value) + "\n")

    def receive():
        line = sys.stdin.buffer.readline()
        if not line:
            raise EOFError
        value = json.loads(line)
        record(value)
        return value

    def send(value):
        sys.stdout.buffer.write(json.dumps(value).encode() + b"\n")
        sys.stdout.buffer.flush()

    def result(request, value):
        send({"jsonrpc": "2.0", "id": request["id"], "result": value})

    def rpc(identifier, method, params):
        relay.stdin.write(json.dumps({
            "jsonrpc": "2.0", "id": identifier, "method": method, "params": params,
        }).encode() + b"\n")
        relay.stdin.flush()
        response = json.loads(relay.stdout.readline())
        record({"relay": response})
        return response

    def permission(identifier, name, kind="allow_once"):
        send({"jsonrpc": "2.0", "id": identifier, "method": "session/request_permission",
              "params": {"sessionId": session_id,
                         "toolCall": {"title": "mcp__cater__save", "_meta": {
                             "qoder": {"toolName": name}}},
                         "options": [{"kind": kind, "optionId": "yes"}]}})
        receive()

    record({"event": "start"})
    try:
        while True:
            request = receive()
            method = request.get("method")
            if method == "initialize":
                result(request, {"protocolVersion": 1, "agentInfo": {
                    "name": "impostor" if mode == "identity" else "qoder-cli-cn"}})
            elif method == "session/new":
                config = request["params"]["mcpServers"][0]
                env = {key: os.environ[key] for key in ("SystemRoot", "WINDIR", "TEMP", "TMP")
                       if key in os.environ}
                env.update({item["name"]: item["value"] for item in config["env"]})
                relay = subprocess.Popen(
                    [config["command"], *config["args"]], env=env, cwd=root,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                )
                rpc("init", "initialize", {"protocolVersion": "2025-06-18"})
                relay.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
                relay.stdin.flush()
                rpc("early", "tools/call", {"name": "save"})
                models = {"currentModelId": "chosen", "availableModels": [
                    {"modelId": "chosen", "name": "Wrong" if mode == "model" else "ExactModel"}]}
                if mode == "duplicate":
                    models["availableModels"] *= 2
                if mode == "missing":
                    models = {}
                result(request, {"sessionId": session_id, "models": models})
            elif method == "session/prompt":
                if mode == "timeout":
                    continue
                if mode == "disconnect":
                    return
                if mode == "reject":
                    send({"jsonrpc": "2.0", "id": request["id"],
                          "error": {"code": -32000, "message": "Try paid fallback or login"}})
                    continue
                send({"jsonrpc": "2.0", "method": "session/update", "params": {
                    "sessionId": session_id, "update": {"sessionUpdate": "agent_thought_chunk",
                    "content": {"type": "text", "text": "PRIVATE_THOUGHT"}}}})
                permission("allowed", "mcp__cater__save")
                permission("unknown", "mcp__cater__other")
                permission("prefix", "mcp__cater__save_extra")
                permission("builtin", "write_file")
                permission("always", "mcp__cater__save", "allow_always")
                permission("malformed", {"name": "mcp__cater__save"})
                send({"jsonrpc": "2.0", "id": "fs", "method": "fs/write_text_file",
                      "params": {"sessionId": session_id, "path": str(root / "forbidden"),
                                 "content": "must not write"}})
                receive()
                send({"jsonrpc": "2.0", "method": "fs/write_text_file",
                      "params": {"sessionId": session_id, "path": str(root / "forbidden"),
                                 "content": "must not write"}})
                send({"jsonrpc": "2.0", "method": "session/update", "params": {
                    "sessionId": session_id, "update": {"sessionUpdate": "tool_call",
                    "rawInput": {"path": str(root / "forbidden"), "content": "must not write"}}}})
                send({"jsonrpc": "2.0", "method": "session/update", "params": {
                    "sessionId": session_id, "update": {"sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "observed"}}}})
                # Wait until the host has consumed preceding ACP observations.
                permission("barrier", "mcp__cater__save")
                if mode != "text":
                    rpc("one", "tools/call", {"name": "save", "arguments": {"value": 1}})
                    rpc("two", "tools/call", {"name": "save", "arguments": {"value": 2}})
                result(request, {"stopReason": "end_turn"})
            elif method == "session/cancel":
                record({"event": "cancel"})
    except EOFError:
        pass
    finally:
        if relay is not None:
            relay.terminate()
            relay.wait(timeout=3)
            relay.stdin.close()
            relay.stdout.close()


if __name__ == "__main__":
    main()
