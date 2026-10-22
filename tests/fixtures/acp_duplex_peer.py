"""Synthetic pipe peer; control socket records facts, never private content."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
import threading


SID = "duplex-synthetic-session"
LIMIT = 20


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"


class Peer:
    def __init__(self, mode, port):
        self.mode = mode
        self.control = socket.create_connection(("127.0.0.1", port), timeout=LIMIT)
        self.control.settimeout(LIMIT)
        self.commands = self.control.makefile("rb")
        self.stdin = sys.stdin.buffer
        self.relay = None

    def event(self, name, **facts):
        self.control.sendall(encode({"event": name, **facts}))

    def gate(self, expected):
        value = json.loads(self.commands.readline())
        assert value == {"command": expected}, (value, expected)

    def send(self, value):
        sys.stdout.buffer.write(encode(value))
        sys.stdout.buffer.flush()

    def receive(self, prefix=b""):
        return json.loads(prefix + self.stdin.readline())

    def result(self, request, value):
        self.send({"jsonrpc": "2.0", "id": request["id"], "result": value})

    def update(self, text="", *, private=False, sid=SID):
        return {"jsonrpc": "2.0", "method": "session/update", "params": {
            "sessionId": sid, "update": {
                "sessionUpdate": "agent_thought_chunk" if private else "agent_message_chunk",
                "content": {"type": "text", "text": "" if private else text}}}}

    def bridge(self, server):
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        env.update({item["name"]: item["value"] for item in server["env"]})
        self.relay = subprocess.Popen(
            [server["command"], *server["args"]], env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        self.event("relay-started", pid=self.relay.pid)

        def exchange(identifier, method, params):
            self.event("mcp-request", method=method)
            self.relay.stdin.write(encode({"jsonrpc": "2.0", "id": identifier,
                                          "method": method, "params": params}))
            self.relay.stdin.flush()
            reply = json.loads(self.relay.stdout.readline())
            assert reply["id"] == identifier and "result" in reply
            if method == "tools/list":
                assert [tool["name"] for tool in reply["result"]["tools"]] == ["memory_echo"]
            if method == "tools/call":
                assert reply["result"]["isError"] is False
            self.event("mcp-response", method=method)

        exchange(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                    "clientInfo": {"name": "synthetic", "version": "1"}})
        self.relay.stdin.write(encode({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}))
        self.relay.stdin.flush()
        exchange(2, "tools/list", {})
        exchange(3, "ping", {})
        exchange(4, "tools/call", {"name": "memory_echo", "arguments": {"value": 7}})
        self.event("mcp-complete-before-drain")

    def run(self):
        self.event("peer-started", pid=os.getpid())
        initialized = self.receive()
        assert initialized["method"] == "initialize"
        self.event("initialize", capabilities=initialized["params"]["clientCapabilities"])
        self.result(initialized, {"protocolVersion": 1, "agentInfo": {"name": "synthetic-duplex"}})
        new = self.receive()
        assert new["method"] == "session/new"
        self.event("session-new", servers=len(new["params"]["mcpServers"]), cwd=new["params"]["cwd"])
        if self.mode == "startup":
            for _ in range(512):
                self.send(self.update(private=True))
            self.send(self.update("startup"))
        self.result(new, {"sessionId": SID})
        self.event("opened")
        self.gate("go")

        # Reading one byte proves a prompt write began; the rest stays blocked.
        prefix = self.stdin.read(1)
        assert prefix == b"{"
        self.event("input-prefix")
        self.gate("prefix-ack")

        if self.mode in {"mcp", "pause"}:
            self.bridge(new["params"]["mcpServers"][0])
        elif self.mode == "private":
            for _ in range(512):
                self.send(self.update(private=True))
            self.event("private-complete", count=512)
        elif self.mode == "public":
            for index in range(384):
                self.send(self.update(str(index)))
                self.send(self.update(private=True))
                if (index + 1) % 32 == 0:
                    self.event("public-batch", count=index + 1)
                    self.gate("public-" + str(index + 1))
            self.event("public-complete", count=384)
        elif self.mode.startswith("deadline-"):
            private = self.mode.endswith("private")
            self.event("deadline-stream-started")
            index = 0
            # IPC grants one frame at a time. No elapsed-time pacing or stdin drain.
            while True:
                self.gate("tick")
                self.send(self.update(str(index), private=private))
                index += 1
                self.event("deadline-frame", count=index)
        elif self.mode.startswith("bad-"):
            packet = self.update(private=self.mode in {"bad-wrong-private", "bad-thought-id"})
            if self.mode == "bad-envelope":
                packet["jsonrpc"] = "1.0"
            elif self.mode in {"bad-wrong-public", "bad-wrong-private"}:
                packet["params"]["sessionId"] = "wrong-session"
            elif self.mode == "bad-shape":
                packet["params"]["update"] = []
            elif self.mode == "bad-thought-id":
                packet["id"] = "must-not-be-a-request"
            self.event("invalid-sent")
            self.send(packet)
            self.gate("never-drain")
        elif self.mode.startswith("invalid-id-"):
            identifier = {"null": None, "bool": True, "unknown": "other-response"}[
                self.mode.removeprefix("invalid-id-")]
            self.event("invalid-sent")
            self.send({"jsonrpc": "2.0", "id": identifier, "result": {"stopReason": "end_turn"}})
            self.gate("never-drain")
        elif self.mode in {"service-failure", "race", "callback-close"}:
            if self.mode == "callback-close":
                self.send(self.update("close-now"))
            self.event("holding-input")
            self.gate("never-drain")

        while True:
            request = self.receive(prefix)
            assert request["method"] == "session/prompt"
            assert request["params"]["sessionId"] == SID
            text = request["params"]["prompt"][0]["text"]
            raw = text.encode("utf-8")
            self.event("prompt", bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
            if self.mode == "mcp":
                self.send({"jsonrpc": "2.0", "id": "owner-request", "method": "fixture/owner",
                           "params": {"sessionId": SID}})
                reply = self.receive()
                assert reply == {"jsonrpc": "2.0", "id": "owner-request", "result": {"ok": True}}
                self.event("owner-request-complete")
            self.send(self.update("ok"))
            self.result(request, {"stopReason": "end_turn"})
            self.event("turn-complete")
            prefix = b""


def main():
    peer = Peer(sys.argv[1], int(sys.argv[2]))
    try:
        peer.run()
    except (BrokenPipeError, ConnectionResetError, EOFError):
        pass
    except BaseException as error:
        try:
            peer.event("peer-error", kind=type(error).__name__)
        except OSError:
            pass
        raise
    finally:
        if peer.relay is not None:
            for stream in (peer.relay.stdin, peer.relay.stdout):
                stream.close()
            if peer.relay.poll() is None:
                peer.relay.terminate()
            peer.relay.wait(timeout=5)
        peer.commands.close()
        peer.control.close()


if __name__ == "__main__":
    main()
