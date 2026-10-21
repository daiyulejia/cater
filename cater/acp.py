"""One external Agent session over ACP stdio, without executing its tools.

Callbacks run synchronously and must provide their own bounded execution.
ProcessScope manages lifetime, not filesystem/network isolation. Notifications
are external observations, not Cater tool receipts or delivery approval.
"""

from __future__ import annotations

import math
import os
import queue
import subprocess
import threading
import time
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .processes import ProcessScope, worker_python
from .tool_protocol import json_utf8, strict_json


MAX_FRAME_BYTES = 4 * 1024 * 1024
MAX_PENDING_UPDATES = 128
_STOP_REASONS = {"end_turn", "max_tokens", "max_turn_requests", "refusal", "cancelled"}


class AcpError(RuntimeError):
    """Transport failure; never replay a prompt with an unknown outcome."""

    def __init__(self, message: str, *, prompt_may_have_run: bool = False):
        super().__init__(message)
        self.prompt_may_have_run = prompt_may_have_run


class AcpProtocolError(AcpError):
    pass


class AcpUnavailable(AcpError):
    pass


class AcpTimeout(AcpUnavailable):
    """The local transport deadline elapsed, not proof of remote cancellation."""

    def __init__(self, message, *, prompt_may_have_run=False, phase=None):
        if phase not in (None, "input", "response"):
            raise ValueError("Invalid ACP timeout phase")
        super().__init__(message, prompt_may_have_run=prompt_may_have_run)
        self.phase = phase


class AcpProcessExited(AcpUnavailable):
    pass


class AcpRequestRejected(AcpUnavailable):
    """The peer rejected an RPC; its private error text is intentionally omitted."""

    def __init__(self, message, *, prompt_may_have_run=False, rpc_code=None, rpc_method=None):
        super().__init__(message, prompt_may_have_run=prompt_may_have_run)
        self.rpc_code = rpc_code if type(rpc_code) is int and -(2**31) <= rpc_code < 2**31 else None
        self.rpc_method = rpc_method if type(rpc_method) is str and rpc_method in (
            "initialize", "session/new", "session/prompt") else None


def rejection_details(error):
    """Keep bounded RPC facts only, never server text or an inferred cause."""
    if not isinstance(error, AcpRequestRejected):
        return {}
    fields = {}
    # Diagnostics must not replace the original failure, including in driver subclasses.
    try:
        code = getattr(error, "rpc_code", None)
    except Exception:
        code = None
    try:
        method = getattr(error, "rpc_method", None)
    except Exception:
        method = None
    if type(code) is int and -(2**31) <= code < 2**31:
        fields["external_rpc_code"] = code
    if type(method) is str and method in ("initialize", "session/new", "session/prompt"):
        fields["external_rpc_method"] = method
    return fields


def failure_code(error):
    """Return only host-owned classifications, never exception/server text."""
    for kind, code in ((AcpTimeout, "deadline_exceeded"),
                       (AcpProcessExited, "process_exited"),
                       (AcpRequestRejected, "request_rejected"),
                       (AcpProtocolError, "protocol_error"),
                       (AcpUnavailable, "transport_unavailable")):
        if isinstance(error, kind):
            return code
    return "external_error"


@dataclass(frozen=True)
class AcpPromptResult:
    text: str
    stop_reason: str


class AcpSession:
    """A single-use connection owning exactly one ACP session.

    No login, model fallback, prompt retry, tool execution or persistence is
    implicit. Each role/workspace binding should own a separate instance.
    `end_turn` only ends a turn; it is not evidence of task completion.
    Private thought notifications are discarded before reaching callbacks.
    """

    def __init__(
        self,
        command: list[str],
        *,
        cwd: str | Path,
        timeout: float = 30,
        env: dict[str, str] | None = None,
        on_request: Callable[[str, dict], dict] | None = None,
    ):
        if (not isinstance(command, list) or not command
                or not all(isinstance(arg, str) and "\0" not in arg for arg in command)
                or not command[0].strip()):
            raise ValueError("ACP command must be a nonempty string array")
        if (type(timeout) not in {int, float} or timeout <= 0
                or timeout > threading.TIMEOUT_MAX or not math.isfinite(timeout)):
            raise ValueError("ACP timeout must be finite and positive")
        if env is not None and (not isinstance(env, dict) or not all(
                isinstance(k, str) and k and "=" not in k and "\0" not in k
                and isinstance(v, str) and "\0" not in v for k, v in env.items())):
            raise ValueError("ACP environment must contain strings")
        if on_request is not None and not callable(on_request):
            raise ValueError("ACP request handler must be callable")
        directory = Path(cwd).resolve(strict=True)
        if not directory.is_dir():
            raise ValueError("ACP cwd must be a directory")
        self.command = list(command)
        self.cwd = str(directory)
        self.timeout = float(timeout)
        self.env = None if env is None else dict(env)
        self.on_request = on_request
        self.session_id: str | None = None
        self.agent_info: dict = {}
        self._session_result: dict | None = None
        self._proc: subprocess.Popen | None = None
        self._scope: ProcessScope | None = None
        self._reader: threading.Thread | None = None
        self._writers: list[threading.Thread] = []
        self._frames: queue.Queue = queue.Queue(maxsize=128)
        self._stopped = threading.Event()
        self._state_lock = threading.RLock()
        self._operation_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._turn_lock = threading.RLock()
        self._turn = 0
        self._cancelled = False
        self._close_lock = threading.Lock()
        self._closed = False
        self._cleanup_done = False
        self._prompt_active = False
        self._next_id = 0
        self._pending_updates: list[dict] = []
        self._pending_session_id: str | None = None

    @contextmanager
    def _operation(self):
        if not self._operation_lock.acquire(blocking=False):
            raise AcpUnavailable("ACP session is busy")
        try:
            if self._closed:
                raise AcpUnavailable("ACP session is closed")
            yield
        finally:
            self._operation_lock.release()

    def open(
        self, *, mcp_servers: list[dict] | None = None,
        client_capabilities: dict | None = None,
        on_update: Callable[[dict], None] | None = None,
        service: Callable[[], None] | None = None,
    ) -> dict:
        servers = [] if mcp_servers is None else mcp_servers
        capabilities = {} if client_capabilities is None else client_capabilities
        if not isinstance(servers, list) or not all(isinstance(s, dict) for s in servers):
            raise ValueError("ACP MCP servers must be an object array")
        if not isinstance(capabilities, dict):
            raise ValueError("ACP client capabilities must be an object")
        if on_update is not None and not callable(on_update):
            raise ValueError("ACP update handler must be callable")
        if service is not None and not callable(service):
            raise ValueError("ACP service callback must be callable")
        # Validate before starting any configured executable.
        self._encode({"mcpServers": servers, "clientCapabilities": capabilities})
        with self._operation():
            if self.session_id is not None:
                raise AcpUnavailable("ACP session is already open")
            try:
                self._start(service=service)
                initialized = self._rpc("initialize", {
                    "protocolVersion": 1, "clientCapabilities": deepcopy(capabilities),
                    "clientInfo": {"name": "cater", "version": "0.1"},
                }, service=service)
                if type(initialized.get("protocolVersion")) is not int or initialized["protocolVersion"] != 1:
                    raise AcpProtocolError("ACP protocol version mismatch")
                info = initialized.get("agentInfo", {})
                if not isinstance(info, dict):
                    raise AcpProtocolError("Invalid ACP agent information")
                self.agent_info = deepcopy(info)
                result = self._rpc("session/new", {"cwd": self.cwd, "mcpServers": deepcopy(servers)}, service=service)
                session_id = result.get("sessionId")
                if not isinstance(session_id, str) or not session_id:
                    raise AcpProtocolError("ACP server did not provide a session ID")
                self.session_id = session_id
                if self._pending_session_id not in {None, session_id}:
                    raise AcpProtocolError("ACP startup update belongs to another session")
                for params in self._pending_updates:
                    body = self._validate_update(params)
                    if on_update is not None:
                        self._notify(on_update, body)
                self._pending_updates.clear()
                self._session_result = deepcopy(result)
                return deepcopy(result)
            except AcpError:
                self._abort()
                raise
            except BaseException:
                self._abort()
                raise

    def prompt(self, text: str, *, on_update: Callable[[dict], None] | None = None,
               service: Callable[[], None] | None = None, timeout: float | None = None) -> AcpPromptResult:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("ACP prompt must be nonempty text")
        if on_update is not None and not callable(on_update):
            raise ValueError("ACP update handler must be callable")
        if service is not None and not callable(service):
            raise ValueError("ACP service callback must be callable")
        if timeout is not None and (type(timeout) not in {int, float} or timeout <= 0
                or timeout > threading.TIMEOUT_MAX or not math.isfinite(timeout)):
            raise ValueError("ACP prompt timeout must be finite and positive")
        with self._operation():
            if self.session_id is None:
                raise AcpUnavailable("ACP session is not open")
            params = {"sessionId": self.session_id, "prompt": [{"type": "text", "text": text}]}
            self._encode(params)
            chunks: list[str] = []
            output_bytes = 0

            def update(body: dict):
                nonlocal output_bytes
                if body["sessionUpdate"] == "agent_message_chunk":
                    content = body.get("content")
                    if not isinstance(content, dict):
                        raise AcpProtocolError("Invalid ACP message chunk")
                    if content.get("type") == "text":
                        value = content.get("text")
                        if not isinstance(value, str):
                            raise AcpProtocolError("Invalid ACP message text")
                        output_bytes += len(json_utf8(value))
                        if output_bytes > MAX_FRAME_BYTES:
                            raise AcpProtocolError("ACP message exceeds output limit")
                        chunks.append(value)
                if on_update is not None:
                    self._notify(on_update, body)

            attempted = False
            try:
                with self._state_lock:
                    if self._closed:
                        raise AcpUnavailable("ACP session is closed")
                attempted = True
                result = self._rpc("session/prompt", params, on_update=update, service=service, timeout=timeout)
                reason = result.get("stopReason")
                if not isinstance(reason, str) or reason not in _STOP_REASONS:
                    raise AcpProtocolError("Invalid ACP stop reason")
                return AcpPromptResult("".join(chunks), reason)
            except AcpError as exc:
                exc.prompt_may_have_run = attempted
                self._abort()
                raise
            except BaseException:
                self._abort()
                raise
            finally:
                with self._turn_lock, self._state_lock:
                    self._prompt_active = False

    def cancel(self) -> bool:
        """Request cancellation without claiming that remote work has stopped."""
        with self._state_lock:
            if self._closed or not self._prompt_active or self.session_id is None:
                return False
            turn = self._turn
        try:
            with self._turn_lock:
                with self._state_lock:
                    if self._closed or not self._prompt_active or self._turn != turn:
                        return False
                    self._cancelled = True
                    session_id = self.session_id
                self._send({"jsonrpc": "2.0", "method": "session/cancel",
                            "params": {"sessionId": session_id}}, time.monotonic() + self.timeout)
        except AcpError as exc:
            exc.prompt_may_have_run = True
            self._abort()
            raise
        return True

    def _encode(self, value: dict) -> bytes:
        try:
            data = json_utf8(value, allow_nan=False)
        except (TypeError, ValueError, UnicodeError, RecursionError):
            raise AcpProtocolError("ACP request is not valid JSON") from None
        if len(data) > MAX_FRAME_BYTES:
            raise AcpProtocolError("ACP request exceeds frame limit")
        return data + b"\n"

    def _start(self, *, service=None):
        deadline = time.monotonic() + self.timeout
        try:
            with self._state_lock:
                if self._closed:
                    raise AcpUnavailable("ACP session is closed")
                self._proc = subprocess.Popen(
                    [worker_python(), "-I", str(Path(__file__).with_name("_driver_worker.py"))],
                    cwd=self.cwd, env=self.env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, start_new_session=os.name != "nt",
                )
                self._scope = ProcessScope(self._proc)
                self._reader = threading.Thread(target=self._read_frames, daemon=True)
                self._reader.start()
        except (OSError, ValueError):
            raise AcpUnavailable("ACP process could not start safely") from None
        self._send(self.command, deadline)
        if self._receive(deadline, service=service) != {"ready": True}:
            raise AcpProtocolError("ACP process did not acknowledge startup")

    def _offer(self, item):
        while not self._stopped.is_set():
            try:
                self._frames.put(item, timeout=0.1)
                return
            except queue.Full:
                pass

    def _read_frames(self):
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        try:
            while not self._stopped.is_set():
                frame = proc.stdout.readline(MAX_FRAME_BYTES + 2)
                if not frame:
                    self._offer(AcpProcessExited("ACP process exited before responding"))
                    return
                if len(frame) > MAX_FRAME_BYTES + 1 or not frame.endswith(b"\n"):
                    self._offer(AcpProtocolError("ACP output exceeds frame limit or is incomplete"))
                    return
                self._offer(frame)
        except (OSError, ValueError):
            self._offer(AcpUnavailable("ACP output pipe is unavailable"))

    def _receive(self, deadline: float, *, service=None) -> dict:
        if self._closed:
            raise AcpUnavailable("ACP session is closed")
        while True:
            if time.monotonic() >= deadline:
                raise AcpTimeout("ACP response timed out", phase="response")
            # Host work stays on the runtime thread, even while the Agent waits
            # for a separate MCP tool process. Streaming updates cannot starve it.
            if service is not None:
                service()
            remaining = deadline - time.monotonic()
            if self._closed:
                raise AcpUnavailable("ACP session is closed")
            if remaining <= 0:
                raise AcpTimeout("ACP response timed out", phase="response")
            try:
                frame = self._frames.get(timeout=remaining if service is None else min(0.05, remaining))
                break
            except queue.Empty:
                if service is None:
                    raise AcpTimeout("ACP response timed out", phase="response") from None
        if self._closed:
            raise AcpUnavailable("ACP session is closed")
        if isinstance(frame, AcpError):
            raise frame
        try:
            result = strict_json(frame.decode("utf-8"))
        except (ValueError, UnicodeError, RecursionError):
            raise AcpProtocolError("ACP output is not valid UTF-8 JSON") from None
        if not isinstance(result, dict):
            raise AcpProtocolError("ACP output must be an object")
        return result

    def _send(self, value, deadline: float):
        data = self._encode(value)
        if not self._write_lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
            raise AcpTimeout("ACP input pipe timed out", phase="input")
        try:
            with self._state_lock:
                proc = self._proc
                if self._closed or proc is None or proc.stdin is None:
                    raise AcpUnavailable("ACP input pipe is unavailable")
                output: queue.Queue = queue.Queue(maxsize=1)

                def write():
                    try:
                        proc.stdin.write(data)
                        proc.stdin.flush()
                        output.put(None)
                    except (OSError, ValueError):
                        output.put(AcpUnavailable("ACP input pipe is unavailable"))

                writer = threading.Thread(target=write, daemon=True)
                self._writers.append(writer)
                writer.start()
            try:
                error = output.get(timeout=max(0.0, deadline - time.monotonic()))
            except queue.Empty:
                raise AcpTimeout("ACP input pipe timed out", phase="input") from None
            writer.join(timeout=max(0.0, deadline - time.monotonic()))
            if not writer.is_alive():
                with self._state_lock:
                    self._writers.remove(writer)
            if error is not None:
                raise error
        finally:
            self._write_lock.release()

    def _validate_update(self, params: dict) -> dict:
        if params.get("sessionId") != self.session_id or self.session_id is None:
            raise AcpProtocolError("ACP update belongs to another session")
        update = params.get("update")
        if not isinstance(update, dict) or not isinstance(update.get("sessionUpdate"), str):
            raise AcpProtocolError("Invalid ACP session update")
        return update

    @staticmethod
    def _notify(callback, body):
        try:
            callback(deepcopy(body))
        except Exception:
            raise AcpProtocolError("ACP update handler failed") from None

    def _client_request(self, message: dict, deadline: float):
        params = message.get("params", {})
        if params.get("sessionId") != self.session_id or self.session_id is None:
            raise AcpProtocolError("ACP client request belongs to another session")
        method = message["method"]
        response = {"jsonrpc": "2.0", "id": message["id"]}
        if method == "session/request_permission" and self._cancelled:
            response["result"] = {"outcome": {"outcome": "cancelled"}}
        elif self.on_request is None:
            if method == "session/request_permission":
                response["result"] = {"outcome": {"outcome": "cancelled"}}
            else:
                response["error"] = {"code": -32601, "message": "Client method not supported"}
        else:
            try:
                value = self.on_request(method, deepcopy(params))
                if not isinstance(value, dict):
                    raise ValueError("Invalid handler response")
                self._encode(value)
                response["result"] = value
            except Exception:
                response["error"] = {"code": -32603, "message": "Client request failed"}
        with self._turn_lock:
            if method == "session/request_permission" and self._cancelled:
                response.pop("error", None)
                response["result"] = {"outcome": {"outcome": "cancelled"}}
            self._send(response, deadline)

    def _rpc(self, method: str, params: dict, *, on_update=None, service=None, timeout=None) -> dict:
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        self._next_id += 1
        request_id = "cater-" + str(self._next_id)
        with self._turn_lock:
            if method == "session/prompt":
                with self._state_lock:
                    self._turn += 1
                    self._cancelled = False
                    self._prompt_active = True
            send_deadline = min(deadline, time.monotonic() + self.timeout)
            self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}, send_deadline)
        while True:
            message = self._receive(deadline, service=service)
            if message.get("jsonrpc") != "2.0":
                raise AcpProtocolError("Invalid ACP JSON-RPC envelope")
            if "method" in message:
                if (not isinstance(message["method"], str)
                        or not isinstance(message.get("params", {}), dict)
                        or "result" in message or "error" in message):
                    raise AcpProtocolError("Invalid ACP method envelope")
                if "id" in message:
                    if message["method"] == "session/update":
                        raise AcpProtocolError("ACP session update must be a notification")
                    if type(message["id"]) not in {str, int}:
                        raise AcpProtocolError("Invalid ACP client request ID")
                    self._client_request(message, deadline)
                elif message["method"] == "session/update":
                    body = message.get("params", {})
                    if method == "session/new" and self.session_id is None:
                        update = body.get("update", {})
                        session_id = body.get("sessionId")
                        if (not isinstance(update, dict)
                                or not isinstance(update.get("sessionUpdate"), str)
                                or not isinstance(session_id, str) or not session_id):
                            raise AcpProtocolError("Invalid ACP session update")
                        if self._pending_session_id not in {None, session_id}:
                            raise AcpProtocolError("ACP startup updates have conflicting sessions")
                        self._pending_session_id = session_id
                        if update.get("sessionUpdate") != "agent_thought_chunk":
                            if len(self._pending_updates) >= MAX_PENDING_UPDATES:
                                raise AcpProtocolError("Too many pending ACP updates")
                            self._pending_updates.append(body)
                    else:
                        update = self._validate_update(body)
                        if update["sessionUpdate"] != "agent_thought_chunk" and on_update is not None:
                            on_update(update)
                continue
            if message.get("id") != request_id:
                raise AcpProtocolError("ACP response ID mismatch")
            if ("result" in message) == ("error" in message):
                raise AcpProtocolError("Invalid ACP response envelope")
            if "error" in message:
                # Server messages may contain prompts, paths or credentials.
                error = message["error"]
                if (not isinstance(error, dict) or type(error.get("code")) is not int
                        or not isinstance(error.get("message"), str)):
                    raise AcpProtocolError("Invalid ACP error response")
                raise AcpRequestRejected("ACP server rejected the request", rpc_code=error["code"], rpc_method=method)
            if not isinstance(message["result"], dict):
                raise AcpProtocolError("ACP result must be an object")
            return message["result"]

    def _abort(self):
        try:
            self.close()
        except AcpError:
            pass

    def close(self):
        with self._close_lock:
            self._close()

    def _close(self):
        with self._state_lock:
            if self._cleanup_done:
                return
            self._closed = True
            self._stopped.set()
            proc, scope, reader = self._proc, self._scope, self._reader
            writers = list(self._writers)
        try:
            self._frames.put_nowait(AcpUnavailable("ACP session is closed"))
        except queue.Full:
            pass
        if proc is None:
            self._cleanup_done = True
            return
        cleanup_failed = False
        try:
            if scope is not None:
                scope.close()
            elif proc.poll() is None:
                proc.kill()
        except OSError:
            cleanup_failed = True
            if proc.poll() is None:
                proc.kill()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            cleanup_failed = True
        threads = ([reader] if reader is not None else []) + writers
        for thread in threads:
            thread.join(timeout=2)
        if any(thread.is_alive() for thread in threads):
            raise AcpUnavailable("ACP pipes did not stop after process cleanup")
        for stream in (proc.stdin, proc.stdout):
            if stream is not None:
                try:
                    stream.close()
                except (OSError, ValueError):
                    cleanup_failed = True
        if cleanup_failed:
            raise AcpUnavailable("ACP process cleanup failed")
        self._cleanup_done = True
