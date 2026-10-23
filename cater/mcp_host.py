"""Ephemeral stdio MCP entry to tools executed on the host's calling thread.

The loopback relay is authenticated but is not an OS sandbox. A caller must
pump on its runtime thread and enforce current scope inside every tool call.
Closing prevents new calls; it cannot undo an operation already in progress.
The Agent's ProcessScope owns the relay process; closing this socket alone does
not terminate a relay blocked on client stdin. Shut down both owners.
Executors return structured tool errors. Raised exceptions abort the bridge.
"""

from __future__ import annotations

import hmac
import math
import re
import secrets
import socket
import sys
import threading
import time
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

from .tool_protocol import ToolCall, json_utf8, strict_json


MAX_BYTES = 4 * 1024 * 1024
MAX_REQUESTS = 4096
VERSION = "2025-06-18"


class ToolBridgeError(RuntimeError):
    pass


class _ExecutorAbort(BaseException):
    def __init__(self, error):
        self.error = error


@dataclass
class _PendingResponse:
    data: bytes
    deadline: float
    offset: int = 0


class McpToolBridge:
    def __init__(self, tools: list[dict], execute, *, timeout: float = 30, on_rejection=None):
        if not callable(execute):
            raise ValueError("MCP tool executor must be callable")
        if on_rejection is not None and not callable(on_rejection):
            raise ValueError("MCP rejection observer must be callable")
        if (type(timeout) not in {int, float} or not math.isfinite(timeout)
                or not 0 < timeout <= 3600):
            raise ValueError("MCP timeout must be between zero and one hour")
        if not isinstance(tools, list) or len(tools) > 256:
            raise ValueError("Invalid MCP tool list")
        definitions = {}
        for tool in tools:
            if (not isinstance(tool, dict) or not isinstance(tool.get("name"), str)
                    or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", tool["name"])
                    or tool["name"] in definitions
                    or not isinstance(tool.get("description"), str)
                    or not isinstance(tool.get("input_schema"), dict)):
                raise ValueError("Invalid MCP tool definition")
            definitions[tool["name"]] = {
                "name": tool["name"], "description": tool["description"],
                "inputSchema": deepcopy(tool["input_schema"]),
            }
        self._encode({"tools": list(definitions.values())})
        self._definitions = definitions
        self._execute = execute
        self._on_rejection = on_rejection
        self._owner = threading.get_ident()
        self._timeout = float(timeout)
        self._secret = secrets.token_urlsafe(32)
        self._listener = socket.socket()
        try:
            self._listener.bind(("127.0.0.1", 0))
            self._listener.listen(1)
        except OSError:
            self._listener.close()
            raise
        self._port = self._listener.getsockname()[1]
        self._connection = None
        self._closed = False
        self._authenticated = False
        self._initialized = False
        self._ready = False
        self._buffer = b""
        self._frame_since = None
        self._seen = set()
        self._sequence = 0
        self._pumping = False
        self._active = False
        self._revoked = False
        self._pending_output = None

    def activate(self):
        """Enable business calls only after the owner has persisted its run."""
        if threading.get_ident() != self._owner:
            raise ToolBridgeError("Only the owning runtime may activate tools")
        if self._closed or self._revoked:
            raise ToolBridgeError("MCP tool authority is no longer available")
        self._active = True

    def revoke(self):
        """Stop accepting work without claiming already-started work was undone."""
        self._active = False
        self._revoked = True

    def server_config(self, name: str = "cater") -> dict:
        """Private launch configuration: contains a transient capability secret."""
        if self._closed:
            raise ToolBridgeError("MCP bridge is closed")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
            raise ValueError("Invalid MCP server name")
        return {
            "name": name, "command": sys.executable,
            "args": ["-I", "-S", "-B", str(Path(__file__).with_name("_mcp_bridge_worker.py"))],
            "env": [
                {"name": "CATER_MCP_PORT", "value": str(self._port)},
                {"name": "CATER_MCP_CAPABILITY", "value": self._secret},
                {"name": "CATER_MCP_TIMEOUT", "value": str(self._timeout)},
            ],
        }

    @staticmethod
    def _encode(value):
        try:
            data = json_utf8(value, allow_nan=False)
        except (ValueError, TypeError, RecursionError):
            raise ToolBridgeError("Invalid MCP JSON") from None
        if len(data) > MAX_BYTES:
            raise ToolBridgeError("MCP frame exceeds limit")
        return data + b"\n"

    def _drop_unauthenticated(self):
        if self._connection is not None:
            self._connection.close()
        self._connection = None
        self._buffer = b""
        self._frame_since = None

    def pump(self, timeout: float = 0.1) -> bool:
        """Process at most one frame, with execution on this calling thread.

        Returns whether a frame was handled, not whether work was completed.
        Executor exceptions abort and propagate; structured errors remain results.
        """
        return self._advance(timeout, cooperative=False)

    @property
    def has_pending_output(self) -> bool:
        """Whether an already-handled frame still has response bytes to send."""
        return self._pending_output is not None

    def poll(self, timeout: float = 0.001) -> bool:
        """Handle at most one frame with bounded transport waits on the owner.

        Returns whether a new frame was handled. Pending output alone returns
        False, even when its last bytes were sent; no next frame is read then.
        Inspect has_pending_output before closing a revoked terminal tool's
        bridge. Output has one total deadline, shared with synchronous pump.
        Tool execution and JSON processing remain synchronous, without an OS
        sandbox or a preemption guarantee for the executor.
        """
        return self._advance(timeout, cooperative=True)

    def _flush_output(self, *, cooperative):
        """Advance the saved wire bytes; only the synchronous caller drains."""
        while True:
            pending = self._pending_output
            if pending is None:
                return
            if self._closed:
                raise ToolBridgeError("MCP bridge is closed")
            remaining = pending.deadline - time.monotonic()
            if remaining <= 0:
                raise ToolBridgeError("MCP response timed out")
            self._connection.settimeout(0 if cooperative else remaining)
            try:
                sent = self._connection.send(memoryview(pending.data)[
                    pending.offset:pending.offset + 65536])
            except (socket.timeout, BlockingIOError):
                if self._closed:
                    raise ToolBridgeError("MCP relay disconnected") from None
                if time.monotonic() >= pending.deadline:
                    raise ToolBridgeError("MCP response timed out") from None
                if cooperative:
                    return
                continue
            if self._closed or not sent:
                raise ToolBridgeError("MCP relay disconnected")
            if time.monotonic() >= pending.deadline:
                raise ToolBridgeError("MCP response timed out")
            pending.offset += sent
            if pending.offset == len(pending.data):
                self._pending_output = None
            if cooperative:
                return

    def _advance(self, timeout, *, cooperative):
        if threading.get_ident() != self._owner or self._pumping:
            raise ToolBridgeError("MCP bridge must be pumped by its owning runtime thread")
        if type(timeout) not in {int, float} or not math.isfinite(timeout) or not 0 < timeout <= 1:
            raise ValueError("MCP pump wait must be between zero and one second")
        if self._closed:
            raise ToolBridgeError("MCP bridge is closed")
        self._pumping = True
        try:
            if self.has_pending_output:
                self._flush_output(cooperative=cooperative)
                return False
            wait_deadline = time.monotonic() + timeout
            if self._connection is None:
                self._listener.settimeout(timeout)
                try:
                    self._connection, _ = self._listener.accept()
                except (socket.timeout, BlockingIOError):
                    return False
                self._frame_since = time.monotonic()
            connection = self._connection
            if b"\n" not in self._buffer:
                try:
                    connection.settimeout(max(0, wait_deadline - time.monotonic())
                                          if cooperative else timeout)
                    chunk = connection.recv(min(65536, MAX_BYTES + 2 - len(self._buffer)))
                except (socket.timeout, BlockingIOError):
                    chunk = None
                except OSError:
                    if not self._authenticated:
                        self._drop_unauthenticated()
                        return False
                    raise
                if chunk == b"":
                    if not self._authenticated:
                        self._drop_unauthenticated()
                        return False
                    raise ToolBridgeError("MCP relay disconnected")
                if chunk:
                    if self._frame_since is None:
                        self._frame_since = time.monotonic()
                    self._buffer += chunk
            if b"\n" not in self._buffer:
                if len(self._buffer) > MAX_BYTES:
                    if not self._authenticated:
                        self._drop_unauthenticated()
                        return False
                    raise ToolBridgeError("MCP frame exceeds limit")
                if self._frame_since is not None and time.monotonic() - self._frame_since > self._timeout:
                    if not self._authenticated:
                        self._drop_unauthenticated()
                        return False
                    raise ToolBridgeError("MCP partial frame timed out")
                return False
            frame, self._buffer = self._buffer.split(b"\n", 1)
            self._frame_since = time.monotonic() if self._buffer else None
            if len(frame) > MAX_BYTES:
                if not self._authenticated:
                    self._drop_unauthenticated()
                    return False
                raise ToolBridgeError("MCP frame exceeds limit")
            try:
                value = strict_json(frame.decode("utf-8"))
            except (ValueError, UnicodeError, RecursionError):
                if not self._authenticated:
                    self._drop_unauthenticated()
                    return False
                raise ToolBridgeError("Invalid MCP relay frame") from None
            if not self._authenticated:
                token = value.get("capability") if isinstance(value, dict) else None
                if not isinstance(token, str) or len(token) > 128 or not hmac.compare_digest(
                        token.encode("utf-8", errors="replace"), self._secret.encode()):
                    self._drop_unauthenticated()
                    return False
                self._authenticated = True
                self._listener.close()
                response = {"authenticated": True}
            else:
                response = {"response": self._dispatch(value)}
            if self._closed:
                raise ToolBridgeError("MCP bridge closed during execution")
            data = self._encode(response)
            self._pending_output = _PendingResponse(data, time.monotonic() + self._timeout)
            self._flush_output(cooperative=cooperative)
            return True
        except _ExecutorAbort as failure:
            raise failure.error from None
        except (OSError, ToolBridgeError):
            self.close()
            raise ToolBridgeError("MCP bridge disconnected or rejected an invalid frame") from None
        finally:
            self._pumping = False

    @staticmethod
    def _error(identifier, code, message):
        return {"jsonrpc": "2.0", "id": identifier, "error": {"code": code, "message": message}}

    def _observe_rejection(self, reason):
        if self._authenticated and self._pumping and self._on_rejection is not None:
            try:
                self._on_rejection(reason)
            except BaseException:
                # Diagnostics must not replace the protocol result or abort cleanup.
                pass

    def _dispatch(self, request):
        if (not isinstance(request, dict) or request.get("jsonrpc") != "2.0"
                or not isinstance(request.get("method"), str)
                or not isinstance(request.get("params", {}), dict)
                or "result" in request or "error" in request):
            raise ToolBridgeError("Invalid MCP request envelope")
        method, params = request["method"], request.get("params", {})
        if "id" not in request:
            if method == "notifications/initialized" and self._initialized:
                self._ready = True
            elif method != "notifications/cancelled":
                raise ToolBridgeError("Unsupported MCP notification")
            return None
        identifier = request["id"]
        if (type(identifier) not in {str, int}
                or isinstance(identifier, str) and len(identifier) > 256
                or type(identifier) is int and abs(identifier) > 2**53 - 1):
            raise ToolBridgeError("Invalid MCP request ID")
        identity = (type(identifier), identifier)
        if identity in self._seen or len(self._seen) >= MAX_REQUESTS:
            raise ToolBridgeError("Repeated MCP request ID or request limit")
        self._seen.add(identity)
        if method == "initialize":
            if self._initialized or not isinstance(params.get("protocolVersion"), str):
                return self._error(identifier, -32602, "Invalid initialization")
            self._initialized = True
            result = {"protocolVersion": VERSION, "capabilities": {"tools": {}},
                      "serverInfo": {"name": "cater-host-tools", "version": "0.1"}}
        elif method == "ping":
            result = {}
        elif not self._ready:
            if method == "tools/call":
                self._observe_rejection("not_initialized")
            return self._error(identifier, -32600, "MCP session is not initialized")
        elif method == "tools/list":
            if params.get("cursor") is not None:
                return self._error(identifier, -32602, "Unsupported cursor")
            result = {"tools": list(self._definitions.values())}
        elif method == "tools/call":
            if not self._active or self._revoked or self._closed:
                self._observe_rejection("inactive")
                return self._error(identifier, -32600, "Host tools are not active for this run")
            name, arguments = params.get("name"), params.get("arguments", {})
            if not isinstance(name, str) or name not in self._definitions:
                self._observe_rejection("unknown_tool")
                return self._error(identifier, -32602, "Unknown tool or invalid arguments")
            if not isinstance(arguments, dict):
                self._observe_rejection("arguments_not_object")
                return self._error(identifier, -32602, "Unknown tool or invalid arguments")
            self._sequence += 1
            try:
                value = self._execute(ToolCall(f"mcp-{self._sequence}", name, deepcopy(arguments)))
            except BaseException as exc:
                # Scope/control exceptions must reach the run owner, not invite
                # the Agent to try more tools against the same obsolete scope.
                self.close()
                raise _ExecutorAbort(exc) from None
            try:
                if not isinstance(value, dict):
                    raise ValueError("Invalid tool result")
                body = json_utf8(value, allow_nan=False).decode("utf-8")
                result = {"content": [{"type": "text", "text": body}], "isError": value.get("status") != "ok"}
                from .tool_protocol import RecordedToolResult

                if type(value) is RecordedToolResult:
                    # Keep the original result first; optional provenance cannot
                    # turn an otherwise valid completed operation into an error.
                    try:
                        footer = json_utf8({"cater_receipt": value.receipt_reference()}, allow_nan=False).decode("utf-8")
                        enriched = {**result, "content": [*result["content"], {"type": "text", "text": footer}]}
                        self._encode({"response": {"jsonrpc": "2.0", "id": identifier, "result": enriched}})
                    except Exception:
                        self._encode({"response": {"jsonrpc": "2.0", "id": identifier, "result": result}})
                    else:
                        result = enriched
                else:
                    self._encode({"response": {"jsonrpc": "2.0", "id": identifier, "result": result}})
            except Exception:
                result = {"content": [{"type": "text", "text":
                    "Host tool did not return a valid result. Check host receipts before retrying; effects may have occurred."}],
                    "isError": True}
        else:
            return self._error(identifier, -32601, "Method not supported")
        return {"jsonrpc": "2.0", "id": identifier, "result": result}

    def close(self):
        self.revoke()
        self._closed = True
        self._pending_output = None
        for stream in (self._connection, self._listener):
            if stream is not None:
                try:
                    stream.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                stream.close()
