"""Deterministic ACP peer with real MCP, never a real model or Qoder account."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def main():
    root = Path(sys.argv[1])
    relay = None

    def send(value):
        print(json.dumps(value), flush=True)

    def call(identifier, method, params):
        relay.stdin.write((json.dumps({"jsonrpc": "2.0", "id": identifier,
                                      "method": method, "params": params}) + "\n").encode())
        relay.stdin.flush()
        return json.loads(relay.stdout.readline())

    def tool(identifier, name, arguments):
        response = call(identifier, "tools/call", {"name": name, "arguments": arguments})
        assert not response["result"].get("isError"), response
        return json.loads(response["result"]["content"][0]["text"])

    try:
        for line in sys.stdin:
            request = json.loads(line)
            method = request.get("method")
            if method == "initialize":
                result = {"protocolVersion": 1, "agentInfo": {"name": "qoder-cli-cn"}}
            elif method == "session/new":
                server = request["params"]["mcpServers"][0]
                env = {key: os.environ[key] for key in ("SystemRoot", "WINDIR", "TEMP", "TMP") if key in os.environ}
                env.update({entry["name"]: entry["value"] for entry in server["env"]})
                relay = subprocess.Popen([server["command"], *server["args"]], cwd=root, env=env,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                call("initialize", "initialize", {"protocolVersion": "2025-06-18"})
                relay.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
                relay.stdin.flush()
                result = {"sessionId": "team-budget-session", "models": {"currentModelId": "chosen",
                    "availableModels": [{"modelId": "chosen", "name": "offline"}]}}
            elif method == "session/prompt":
                text = request["params"]["prompt"][0]["text"]
                context = json.loads(text.rsplit("\n", 1)[-1])
                if context["message"]["kind"] == "recovery":
                    artifact = context["artifacts"][0]["seq"]
                    body = tool("read-back", "read_artifact", {"artifact_seq": artifact})
                    assert body["content"] == "Preserved through the real MCP bridge\n" * 8
                    tool("finish", "finish_work", {"summary": "Read exact saved bytes",
                         "verification": "Offline actual ACP/MCP, not real-model quality", "artifact_seqs": [artifact]})
                else:
                    published = tool("save-once", "publish_artifact", {"filename": "result.txt", "format": "text",
                        "content": "Preserved through the real MCP bridge\n" * 8, "summary": "One committed version"})
                    for offset in range(20):
                        time.sleep(0.3)
                        tool(str(offset), "read_artifact", {"artifact_seq": published["artifact_seq"],
                                                          "offset": offset, "length": 1})
                result = {"stopReason": "end_turn"}
            else:
                continue
            send({"jsonrpc": "2.0", "id": request["id"], "result": result})
    finally:
        if relay is not None:
            relay.terminate()
            relay.wait(timeout=3)
            relay.stdin.close()
            relay.stdout.close()


if __name__ == "__main__":
    main()
