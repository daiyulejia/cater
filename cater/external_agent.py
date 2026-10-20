"""Single-use Qoder ACP runs with host-thread MCP tools.

An end_turn is only a transport result, never a delivery receipt. Token budgets
are host metadata; this adapter neither estimates nor claims to enforce usage.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import math
import os
import sys
import threading

from .acp import AcpPromptResult, AcpProtocolError, AcpSession
from .mcp_host import McpToolBridge


class ExternalYield(Exception):
    """The host has successfully waited/delivered and wants this run to stop."""


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


class QoderAcpDriver:
    execution_profile = "qoder-managed-v1"

    def __init__(self, name, executable, model, timeout=120, token_budget=None):
        try:
            executable = os.fspath(executable)
        except TypeError:
            raise ValueError("Invalid Qoder executable") from None
        for label, value in (("name", name), ("executable", executable), ("model", model)):
            if not isinstance(value, str) or not value.strip() or "\0" in value:
                raise ValueError(f"Invalid Qoder {label}")
        if (type(timeout) not in {int, float} or not math.isfinite(timeout)
                or not 0 < timeout <= 3600):
            raise ValueError("Qoder timeout must be between zero and one hour")
        if token_budget is not None and (type(token_budget) is not int or token_budget < 0):
            raise ValueError("Token budget must be a non-negative integer")
        self.name, self.model = name, model
        self.token_budget, self.timeout = token_budget, timeout
        self._command = (
            executable, "--acp", "--model", model, "--max-output-tokens", "2048",
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
    """A lazy single-use run. Call run() on the host runtime thread.

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

    @property
    def state(self):
        return ExternalAgentState(
            self._phase, self._prompt_attempted, self._cleanup_complete,
            self._session_id, self._stop_reason, self._text, tuple(self._cleanup_failures),
            bool(self._cancel_failures), tuple(self._cancel_failures),
        )

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

    def _stop(self, error):
        if self._failure is None:
            self._failure = error
        if self._stopping:
            return
        self._stopping = True
        self.revoke_tools()
        if self._session is not None:
            try:
                self._session.cancel()
            except BaseException as exc:
                self._cancel_failures.append(exc)

    def _current(self):
        if self._failure is not None:
            raise self._failure
        if self._check_current() is False:
            raise ExternalYield("External run is no longer current")

    def _service(self):
        try:
            self._current()
            self._bridge.pump(0.01)
        except BaseException as exc:
            self._stop(exc)
            raise

    def _call(self, call):
        try:
            self._current()
            if self._tools_revoked:
                raise ExternalYield("External tools have been revoked")
            return self._execute(call)
        except BaseException as exc:
            # Stop before McpToolBridge performs its own exception cleanup.
            self._stop(exc)
            raise

    def _update(self, update):
        try:
            self._current()
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

    def _request(self, method, params):
        try:
            self._current()
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
        with self._start_lock:
            if self._phase != "created":
                raise RuntimeError("External runs are single-use; prompts cannot be replayed")
            self._phase = "opening"
        try:
            if not isinstance(prompt, str) or not prompt.strip():
                raise ValueError("External prompt must be nonempty text")
            self._current()
            self._bridge = McpToolBridge(self._tools, self._call, timeout=self._timeout)
            self._session = AcpSession(
                self._command, cwd=self._cwd, timeout=self._timeout, on_request=self._request,
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
            opened = self._session.open(
                mcp_servers=[self._bridge.server_config("cater")],
                client_capabilities={}, on_update=self._update, service=self._service,
            )
            self._session_id = self._session.session_id
            self._verify(opened)
            self._current()
            if not self._tools_revoked:
                self._bridge.activate()
            self._phase = "prompting"
            self._prompt_attempted = True
            result = self._session.prompt(prompt, on_update=self._update, service=self._service)
            self._text, self._stop_reason = result.text, result.stop_reason
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
                self._phase = "failed"
                raise self._cleanup_failures[0]
