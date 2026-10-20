"""Local ACP peer; all activity stays inside the test's temporary directory."""

from __future__ import annotations

import json
from pathlib import Path
import socket
import subprocess
import sys
import time


SESSION_ID = "fixture-session"
THOUGHT = "PRIVATE_THOUGHT_MUST_NOT_ESCAPE"


def send(value):
    wire = json.dumps(value, ensure_ascii=False).encode("utf-8") + b"\n"
    sys.stdout.buffer.write(wire)
    sys.stdout.buffer.flush()


def result(request, value):
    send({"jsonrpc": "2.0", "id": request["id"], "result": value})


def update(value, session_id=SESSION_ID):
    send({"jsonrpc": "2.0", "method": "session/update",
          "params": {"sessionId": session_id, "update": value}})


def message(text, kind="agent_message_chunk", session_id=SESSION_ID):
    update({"sessionUpdate": kind, "content": {"type": "text", "text": text}},
           session_id)


def wait_for(path):
    deadline = time.monotonic() + 20
    while not path.exists():
        if time.monotonic() > deadline:
            raise RuntimeError("fixture synchronization timed out")
        time.sleep(0.01)


def publish_port(root, port):
    pending = root / "child-port.pending"
    pending.write_text(str(port))
    pending.replace(root / "child-port")


def child(root):
    # A live socket observes descendant lifetime without PID reuse races.
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        publish_port(root, listener.getsockname()[1])
        while True:
            connection, _ = listener.accept()
            connection.close()


def main():
    mode, root_arg = sys.argv[1:3]
    root = Path(root_arg)
    if mode == "--child":
        child(root)
        return

    journal = root / "wire.jsonl"

    def record(value):
        with journal.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(value, ensure_ascii=False) + "\n")

    def receive():
        line = sys.stdin.buffer.readline()
        if not line:
            raise EOFError
        value = json.loads(line)
        record(value)
        return value

    record({"event": "started"})
    pending = None
    prompt_count = 0
    while True:
        try:
            request = receive()
        except EOFError:
            return
        method = request.get("method")
        if method == "initialize":
            if mode == "initialize-timeout":
                wait_for(root / "release")
            if mode == "initialize-disconnect":
                return
            result(request, {
                "protocolVersion": 1,
                "agentCapabilities": {},
                "agentInfo": {"name": "local-acp-fixture", "version": "1"},
            })
        elif method == "session/new":
            if mode == "new-timeout":
                wait_for(root / "release")
            if mode in {"early-update", "early-wrong-session"}:
                early_sid = "other-session" if mode == "early-wrong-session" else SESSION_ID
                message(THOUGHT, "agent_thought_chunk", session_id=early_sid)
                message("early:", session_id=early_sid)
            if mode == "tree":
                subprocess.Popen([sys.executable, "-u", __file__, "--child", str(root)])
                wait_for(root / "child-port")
            result(request, {"sessionId": SESSION_ID})
        elif method == "session/prompt":
            prompt_count += 1
            if mode == "turn-race" and prompt_count == 1:
                message("ready")
                wait_for(root / "release-a")
                result(request, {"stopReason": "end_turn"})
                continue
            if mode == "thought-request":
                send({"jsonrpc": "2.0", "id": "thought-request",
                      "method": "session/update",
                      "params": {"sessionId": SESSION_ID, "update": {
                          "sessionUpdate": "agent_thought_chunk",
                          "content": {"type": "text", "text": THOUGHT}}}})
                result(request, {"stopReason": "end_turn"})
                continue
            if mode == "permission-cancel":
                send({"jsonrpc": "2.0", "id": "permission-1",
                      "method": "session/request_permission",
                      "params": {"sessionId": SESSION_ID,
                                 "toolCall": {"toolCallId": "tool-1", "title": "fixture"},
                                 "options": [{"optionId": "allow", "name": "Allow",
                                              "kind": "allow_once"}]}})
                while True:
                    reply = receive()
                    if reply.get("id") == "permission-1":
                        break
                result(request, {"stopReason": "cancelled"})
                continue
            if mode == "disconnect":
                return
            if mode == "reject":
                send({"jsonrpc": "2.0", "id": request["id"],
                      "error": {"code": -32000, "message": "PRIVATE_CREDENTIAL"}})
                continue
            if mode.startswith("bad-error:"):
                errors = [None, {}, "PRIVATE_CREDENTIAL", {"code": True, "message": "PRIVATE_CREDENTIAL"},
                          {"code": -32000, "message": {"secret": "PRIVATE_CREDENTIAL"}}]
                send({"jsonrpc": "2.0", "id": request["id"],
                      "error": errors[int(mode.split(":")[1])]})
                continue
            if mode == "timeout":
                wait_for(root / "release")
                continue
            if mode in {"hold", "cancel", "turn-race"}:
                pending = request
                message("ready")
                continue
            if mode == "wrong-id":
                send({"jsonrpc": "2.0", "id": "not-a-client-id",
                      "result": {"stopReason": "end_turn"}})
                continue
            if mode == "wrong-session":
                message("must not escape", session_id="other-session")
                result(request, {"stopReason": "end_turn"})
                continue
            if mode.startswith("bad-frame:"):
                kind = mode.split(":", 1)[1]
                request_id = json.dumps(request["id"]).encode("ascii")
                prefix = b'{"jsonrpc":"2.0","id":' + request_id
                frames = {
                    "json": b"not json\n",
                    "utf8": b"\xff\n",
                    "array": b"[]\n",
                    "duplicate": prefix + b',"id":' + request_id
                        + b',"result":{"stopReason":"end_turn"}}\n',
                    "nan": prefix + b',"result":{"stopReason":"end_turn","x":NaN}}\n',
                    "infinity": prefix + b',"result":{"stopReason":"end_turn","x":Infinity}}\n',
                    "oversized": b"x" * (4 * 1024 * 1024 + 1) + b"\n",
                }
                sys.stdout.buffer.write(frames[kind])
                sys.stdout.buffer.flush()
                continue
            if mode in {"permission", "callback", "callback-error"}:
                send({
                    "jsonrpc": "2.0", "id": "permission-1",
                    "method": "session/request_permission",
                    "params": {
                        "sessionId": SESSION_ID,
                        "toolCall": {"toolCallId": "tool-1", "title": "fixture only"},
                        "options": [{"optionId": "allow", "name": "Allow",
                                     "kind": "allow_once"}],
                    },
                })
                receive()
                send({"jsonrpc": "2.0", "id": "unknown-1",
                      "method": "fixture/unsupported",
                      "params": {"sessionId": SESSION_ID, "value": 7}})
                receive()
            if mode == "tool":
                update({"sessionUpdate": "tool_call", "toolCallId": "tool-1",
                        "title": "fixture only", "kind": "execute", "status": "pending",
                        "rawInput": {"command": [sys.executable, "-c",
                            "from pathlib import Path; "
                            f"Path({str(root / 'must-not-execute')!r}).touch()"]}})
            message(THOUGHT, "agent_thought_chunk")
            message("\u4f60\u597d\uff0c")
            message("\u4e16\u754c")
            message(THOUGHT, "agent_thought_chunk")
            result(request, {"stopReason": "end_turn"})
        elif method == "session/cancel":
            record({"event": "cancel", "notification": "id" not in request})
            message("cancel-observed")
            wait_for(root / "release")
            if pending is not None:
                result(pending, {"stopReason": "cancelled"})
                pending = None
        elif "id" in request:
            send({"jsonrpc": "2.0", "id": request["id"],
                  "error": {"code": -32601, "message": "Unknown fixture method"}})


if __name__ == "__main__":
    main()
