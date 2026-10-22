"""Single-use Qoder ACP runs with host-thread MCP tools.

An end_turn is only a transport result, never a delivery receipt. Token budgets
are host metadata; this adapter neither estimates nor claims to enforce usage.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import math
import os
import sys
import threading
import time

from .acp import AcpPromptResult, AcpProtocolError, AcpSession, AcpTimeout
from .mcp_host import McpToolBridge
from .tool_protocol import json_utf8


class ExternalYield(Exception):
    """The host has successfully waited/delivered and wants this run to stop."""


class ExternalRunLimit(AcpTimeout):
    """An explicit local execution budget expired, not a remote verdict."""

    def __init__(self, limit):
        if limit not in {"idle", "total"}:
            raise ValueError("Unknown external execution limit")
        self.limit = limit
        super().__init__("External execution idle limit" if limit == "idle" else "External execution total limit")


class _ProgressBudget:
    """Checked between synchronous callbacks; not a tool preemption mechanism."""

    def __init__(self, idle, total):
        self.idle, self.total = idle, total
        self.started = self.progress_at = time.monotonic()
        self.activity_at = self.started
        self._tool_updates = {}
        self.seen = set()

    def expired(self):
        now = time.monotonic()
        idle_deadline = max(self.progress_at, self.activity_at) + self.idle
        total_deadline = self.started + self.total
        if now >= min(idle_deadline, total_deadline):
            return ExternalRunLimit("total" if total_deadline <= idle_deadline else "idle")
        return None

    def check(self):
        error = self.expired()
        if error is not None:
            raise error

    def observed(self, update):
        """Transport activity is not useful work or acceptance; total never moves."""
        if self.expired() is not None:
            return False
        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk":
            content = update.get("content")
            if (not isinstance(content, dict) or content.get("type") != "text"
                    or not isinstance(content.get("text"), str) or not content["text"].strip()):
                return False
        elif kind in {"tool_call", "tool_call_update"}:
            if not isinstance(update.get("toolCallId"), str) or not update["toolCallId"]:
                return False
            details = {key: update[key] for key in ("toolCallId", "rawInput", "content", "status") if key in update}
            if len(details) < 2:
                return False
            try:
                fingerprint = hashlib.sha256(json_utf8(details, sort_keys=True, allow_nan=False)).digest()
            except (TypeError, ValueError):
                return False
            identity = hashlib.sha256(json_utf8(update["toolCallId"])).digest()
            if fingerprint == self._tool_updates.get(identity):
                return False
            # Retain per-call state without allowing a notification flood to grow memory.
            if identity not in self._tool_updates and len(self._tool_updates) >= 256:
                return False
            self._tool_updates[identity] = fingerprint
        else:
            return False
        return self.activity()

    def activity(self):
        if self.expired() is not None:
            return False
        self.activity_at = time.monotonic()
        return True

    def settled(self, call, result):
        if self.expired() is not None:
            return False
        if not isinstance(result, dict):
            return False
        # A completed failing command is debugging evidence, not idle time or acceptance.
        command_failed = (call.name == "workspace_run" and result.get("status") == "error"
                          and result.get("outcome") == "failed" and result.get("complete") is True
                          and result.get("timed_out") is False and type(result.get("exit_code")) is int
                          and result["exit_code"] != 0 and isinstance(result.get("error"), dict)
                          and result["error"].get("code") == "failed")
        if not command_failed and (result.get("status") != "ok" or result.get("complete") is False
                or result.get("outcome") in ("failed", "timeout", "effects_unknown")):
            return False
        key = hashlib.sha256(json_utf8([call.name, call.arguments], sort_keys=True, allow_nan=False)).digest()
        if key in self.seen:
            return False
        self.seen.add(key)
        self.progress_at = time.monotonic()
        return True


@dataclass(frozen=True)
class ExternalAgentState:
    phase: str = "created"
    prompt_attempted: bool = False
    cleanup_complete: bool = False
    session_id: str | None = None
    stop_reason: str | None = None
    text: str = ""
    cleanup_failures: tuple[BaseException, ...] = ()
    cancel_failed: bool = False
    cancel_failures: tuple[BaseException, ...] = ()
    observations: tuple[tuple[str, int], ...] = ()
    failure_phase: str | None = None


class QoderAcpDriver:
    execution_profile = "qoder-managed-v1"

    def __init__(self, name, executable, model, timeout=120, token_budget=None, max_run_seconds=None):
        try:
            executable = os.fspath(executable)
        except TypeError:
            raise ValueError("Invalid Qoder executable") from None
        for label, value in (("name", name), ("executable", executable), ("model", model)):
            if not isinstance(value, str) or not value.strip() or "\0" in value:
                raise ValueError(f"Invalid Qoder {label}")
        if (type(timeout) not in {int, float} or not 0 < timeout <= 3600
                or not math.isfinite(timeout)):
            raise ValueError("Qoder timeout must be between zero and one hour")
        if token_budget is not None and (type(token_budget) is not int or token_budget < 0):
            raise ValueError("Token budget must be a non-negative integer")
        if max_run_seconds is not None and (type(max_run_seconds) not in {int, float}
                or not timeout <= max_run_seconds <= 3600 or not math.isfinite(max_run_seconds)):
            raise ValueError("max_run_seconds must be finite, at least timeout, and at most one hour")
        self.name, self.model = name, model
        self.token_budget, self.timeout = token_budget, timeout
        self.max_run_seconds = max_run_seconds
        self._command = (
            executable, "--acp", "--model", model,
            "--max-model-request-retries", "0", "--tools", "", "--strict-mcp-config",
            "--setting-sources", "local", "--permission-mode", "default",
        )
        self._closed = False

    @property
    def command(self):
        return list(self._command)

    @property
    def closed(self) -> bool:
        """Configuration lifetime, observed by the host's service check."""
        return self._closed

    def agent_run(self, *, cwd, tools, execute, check_current, on_update):
        if self._closed:
            raise RuntimeError("External driver is closed")
        return ExternalAgentRun(
            self, cwd=cwd, tools=tools, execute=execute,
            check_current=check_current, on_update=on_update,
        )

    def complete(self, *args, **kwargs):
        raise NotImplementedError("Qoder ACP requires agent_run; plain text completion is unsupported")

    def close(self):
        """Disable new runs; existing runs own their resources independently."""
        self._closed = True


class ExternalAgentRun:
    """A lazy single-use run. Drive run()/iter_run() on the host runtime thread.

    check_current may raise a host control exception or return False to stop.
    on_update receives observations only; notifications never execute tools.
    State snapshots remain readable after any failure, including cleanup errors.
    The host supplies a fresh temporary cwd; workspace access belongs to execute.
    cleanup_complete describes local resources, not remote inference or billing.
    """

    def __init__(self, driver, *, cwd, tools, execute, check_current, on_update):
        if not callable(execute) or not callable(check_current):
            raise ValueError("Host executor and current-scope check must be callable")
        if on_update is not None and not callable(on_update):
            raise ValueError("Update observer must be callable")
        self._command, self._model = driver.command, driver.model
        self._timeout, self._cwd = driver.timeout, cwd
        self._max_run_seconds = driver.max_run_seconds
        self._budget = None
        self._tools = deepcopy(tools)
        self._execute, self._check_current, self._on_update = execute, check_current, on_update
        self._session = self._bridge = None
        self._phase = "created"
        self._prompt_attempted = False
        self._cleanup_complete = False
        self._session_id = self._stop_reason = None
        self._text = ""
        self._cleanup_failures = []
        self._cancel_failures = []
        self._tools_revoked = False
        self._failure = None
        self._stopping = False
        self._start_lock = threading.Lock()
        self._started_at = None
        self._observations = {}
        self._failure_phase = None
        self._cooperative = False
        self._deferred_yield = None

    @property
    def state(self):
        return ExternalAgentState(
            self._phase, self._prompt_attempted, self._cleanup_complete,
            self._session_id, self._stop_reason, self._text, tuple(self._cleanup_failures),
            bool(self._cancel_failures), tuple(self._cancel_failures),
            tuple(self._observations.items()), self._failure_phase,
        )

    def _mark(self, name, *, latest=False):
        if self._started_at is not None and (latest or name not in self._observations):
            self._observations[name] = max(0, int((time.monotonic() - self._started_at) * 1000))

    def revoke_tools(self):
        """Withdraw tool authority without cancelling or losing the current receipt.

        Call on the run thread after a successful host wait/return/finish. The
        next service check may then stop the run. Repeated calls are harmless;
        revocation failures are recorded for cleanup, not raised into execute.
        """
        self._tools_revoked = True
        if self._bridge is not None:
            self._cleanup(self._bridge.revoke)

    def _cleanup(self, action):
        try:
            action()
        except BaseException as exc:
            self._cleanup_complete = False
            if not any(exc is item for item in self._cleanup_failures):
                self._cleanup_failures.append(exc)

    def _stop(self, error, *, phase=None):
        if (self._budget is not None and isinstance(error, AcpTimeout)
                and not isinstance(error, ExternalRunLimit) and error.phase == "response"):
            error = self._budget.expired() or error
        if self._failure is None:
            self._failure = error
            self._failure_phase = phase or self._phase
            self._mark("failure_ms")
        if self._stopping:
            return
        self._stopping = True
        self.revoke_tools()
        if self._session is not None:
            try:
                self._session.cancel()
            except BaseException as exc:
                self._cancel_failures.append(exc)

    def _has_pending_output(self):
        return self._cooperative and getattr(self._bridge, "has_pending_output", False) is True

    def _current(self, *, allow_pending=False):
        if self._failure is not None:
            raise self._failure
        if self._deferred_yield is not None and not (allow_pending and self._has_pending_output()):
            raise self._deferred_yield
        try:
            if self._check_current() is False:
                raise ExternalYield("External run is no longer current")
        except ExternalYield as exc:
            if not (allow_pending and self._has_pending_output()):
                raise
            # A settled tool receipt may still be only partly on the wire.
            # Withdraw authority now; cancel/close only after its output drains.
            if self._deferred_yield is None:
                self._deferred_yield = exc
            self.revoke_tools()

    def _service(self):
        try:
            self._current(allow_pending=self._cooperative)
            if self._budget is not None:
                self._budget.check()
            if self._cooperative:
                self._bridge.poll(timeout=0.001)
            else:
                self._bridge.pump(0.01)
            if self._budget is not None:
                self._budget.check()
        except BaseException as exc:
            self._stop(exc)
            raise

    def _call(self, call):
        dispatched = False
        try:
            self._current()
            if self._tools_revoked:
                raise ExternalYield("External tools have been revoked")
            if self._budget is not None:
                self._budget.check()
            self._mark("first_tool_ms")
            dispatched = True
            self._observations["tool_calls"] = self._observations.get("tool_calls", 0) + 1
            started = time.monotonic()
            try:
                result = self._execute(call)
                if self._budget is not None and self._budget.settled(call, result):
                    self._observations["progress_receipts"] = len(self._budget.seen)
                    self._mark("last_progress_ms", latest=True)
                return result
            finally:
                self._observations["tool_elapsed_ms"] = self._observations.get("tool_elapsed_ms", 0) + max(
                    0, int((time.monotonic() - started) * 1000))
                self._mark("last_tool_end_ms", latest=True)
        except BaseException as exc:
            # Stop before McpToolBridge performs its own exception cleanup.
            self._stop(exc, phase="tool_execution" if dispatched else None)
            raise

    def _update(self, update):
        try:
            self._current(allow_pending=self._cooperative)
            # ACP already discards private thought chunks. Do not persist any
            # update content or arbitrary client-provided category as telemetry.
            if self._phase == "prompting" and update.get("sessionUpdate") in {
                "agent_message_chunk", "tool_call", "tool_call_update", "plan",
            }:
                self._mark("first_public_update_ms")
                self._mark("last_public_update_ms", latest=True)
                self._observations["public_updates"] = self._observations.get("public_updates", 0) + 1
                if self._budget is not None and self._budget.observed(update):
                    self._mark("last_activity_ms", latest=True)
            if self._phase == "prompting" and update.get("sessionUpdate") == "agent_message_chunk":
                content = update.get("content", {})
                if content.get("type") == "text":
                    self._text += content["text"]
            if self._on_update is not None:
                self._on_update(update)
        except BaseException as exc:
            # ACP wraps observer exceptions; retain the original host signal.
            self._stop(exc)
            raise

    def _activity(self):
        try:
            self._current(allow_pending=self._cooperative)
            if self._phase == "prompting" and self._budget is not None and self._budget.activity():
                self._mark("last_activity_ms", latest=True)
        except BaseException as exc:
            self._stop(exc)
            raise

    def _request(self, method, params):
        try:
            self._current(allow_pending=self._cooperative)
        except BaseException as exc:
            self._stop(exc)
            raise
        denied = {"outcome": {"outcome": "cancelled"}}
        if method != "session/request_permission":
            raise ValueError("Client method not supported")
        if (self._tools_revoked or self._phase != "prompting"
                or params.get("sessionId") != self._session.session_id):
            return denied
        tool = params.get("toolCall")
        meta = tool.get("_meta") if isinstance(tool, dict) else None
        qoder = meta.get("qoder") if isinstance(meta, dict) else None
        name = qoder.get("toolName") if isinstance(qoder, dict) else None
        allowed = {f"mcp__cater__{tool['name']}" for tool in self._tools}
        options = params.get("options")
        if not isinstance(name, str) or name not in allowed or not isinstance(options, list):
            return denied
        for option in options:
            if (isinstance(option, dict) and option.get("kind") == "allow_once"
                    and isinstance(option.get("optionId"), str) and option["optionId"]):
                return {"outcome": {"outcome": "selected", "optionId": option["optionId"]}}
        return denied

    def _verify(self, result):
        if self._session.agent_info.get("name") != "qoder-cli-cn":
            raise AcpProtocolError("Unexpected Qoder agent identity")
        models = result.get("models")
        if not isinstance(models, dict):
            raise AcpProtocolError("Missing Qoder model identity")
        current, available = models.get("currentModelId"), models.get("availableModels")
        if not isinstance(current, str) or not current or not isinstance(available, list):
            raise AcpProtocolError("Invalid Qoder model identity")
        selected = [item for item in available if isinstance(item, dict)
                    and item.get("modelId") == current]
        if len(selected) != 1 or selected[0].get("name") != self._model:
            raise AcpProtocolError("Unexpected Qoder model")

    def run(self, prompt) -> AcpPromptResult:
        """Drain the shared lifecycle using the synchronous session entry points."""
        iterator = self._iter_run(prompt, cooperative=False)
        while True:
            try:
                next(iterator)
            except StopIteration as done:
                return done.value

    def iter_run(self, prompt):
        """Yield None at transport opportunities; return the original prompt result.

        Custom sessions without both iterator entry points use open/prompt once,
        synchronously. They do not acquire a cooperative scheduling guarantee.
        Closing a started iterator synchronously revokes and cleans up the run.
        """
        return (yield from self._iter_run(prompt, cooperative=True))

    def _session_operation(self, name, *args, **kwargs):
        if self._cooperative:
            return (yield from getattr(self._session, "iter_" + name)(*args, **kwargs))
        return getattr(self._session, name)(*args, **kwargs)

    def _iter_run(self, prompt, *, cooperative):
        with self._start_lock:
            if self._phase != "created":
                raise RuntimeError("External runs are single-use; prompts cannot be replayed")
            self._phase = "opening"
            self._started_at = time.monotonic()
        try:
            if not isinstance(prompt, str) or not prompt.strip():
                raise ValueError("External prompt must be nonempty text")
            self._current()
            self._bridge = McpToolBridge(self._tools, self._call, timeout=self._timeout)
            self._session = AcpSession(
                self._command, cwd=self._cwd, timeout=self._timeout, on_request=self._request,
                on_activity=self._activity,
            )
            self._cooperative = cooperative and all(
                callable(getattr(self._session, name, None)) for name in ("iter_open", "iter_prompt")
            )
            original_close = self._session.close

            def observed_close():
                # ACP abort closes before returning its error. Revoke/cancel
                # first, and retain both the original error and cleanup evidence.
                error = sys.exception()
                if error is not None and self._phase in {"opening", "prompting"}:
                    self._stop(error)
                try:
                    original_close()
                except BaseException as exc:
                    if not any(exc is item for item in self._cleanup_failures):
                        self._cleanup_failures.append(exc)
                    raise

            self._session.close = observed_close
            opened = yield from self._session_operation("open",
                mcp_servers=[self._bridge.server_config("cater")],
                client_capabilities={}, on_update=self._update, service=self._service,
            )
            self._session_id = self._session.session_id
            self._verify(opened)
            self._mark("handshake_ms")
            self._current()
            if not self._tools_revoked:
                self._bridge.activate()
            self._phase = "prompting"
            self._prompt_attempted = True
            self._mark("prompt_attempt_ms")
            if self._max_run_seconds is None:
                result = yield from self._session_operation(
                    "prompt", prompt, on_update=self._update, service=self._service)
            else:
                self._budget = _ProgressBudget(self._timeout, self._max_run_seconds)
                result = yield from self._session_operation(
                    "prompt", prompt, on_update=self._update, service=self._service,
                    timeout=self._max_run_seconds)
            self._mark("response_ms")
            self._text, self._stop_reason = result.text, result.stop_reason
            while self._has_pending_output():
                self._service()
                yield None
            self._current()
            self._phase = "finished"
            return result
        except BaseException as exc:
            self._stop(exc)
            self._phase = "yielded" if isinstance(self._failure, ExternalYield) else "failed"
            raise self._failure
        finally:
            if self._session is not None:
                self._session_id = self._session.session_id
            if self._bridge is not None:
                self._cleanup(self._bridge.revoke)
            if self._session is not None:
                self._cleanup(self._session.close)
            if self._bridge is not None:
                self._cleanup(self._bridge.close)
            self._cleanup_complete = not self._cleanup_failures
            if self._failure is None and self._cleanup_failures:
                self._failure_phase = "cleanup"
                self._mark("failure_ms")
            self._mark("settled_ms")
            if self._failure is None and self._cleanup_failures:
                self._phase = "failed"
                raise self._cleanup_failures[0]
