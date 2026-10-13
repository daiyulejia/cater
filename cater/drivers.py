"""Agent driver abstractions."""

from __future__ import annotations

import json
import queue
import subprocess
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True)
class DriverResponse:
    text: str
    tokens: int = 0
    meta: dict[str, Any] | None = None


class Driver(Protocol):
    name: str

    def complete(self, *, role: str, prompt: str, context: dict[str, Any]) -> DriverResponse:
        ...


class DriverError(RuntimeError):
    """Base class for driver failures."""


class DriverProtocolError(DriverError):
    """The driver responded with a malformed payload."""


class DriverUnavailable(DriverError):
    """The driver process or endpoint is unavailable."""


class ScriptedDriver:
    def __init__(self, name: str, script: list[str], tokens_per_call: int = 200):
        self.name = name
        self.script = list(script)
        self.tokens_per_call = tokens_per_call
        self.cursor = 0

    def complete(self, *, role: str, prompt: str, context: dict[str, Any]) -> DriverResponse:
        if not self.script:
            text = f'{{"stance":"assert","option_id":"A","claim":"{role} agrees","evidence":["script"],"text":"{role} agrees"}}'
        else:
            text = self.script[self.cursor % len(self.script)]
            self.cursor += 1
        return DriverResponse(text=text, tokens=self.tokens_per_call, meta={"driver": self.name})


class CliDriver:
    """Long-lived NDJSON CLI driver.

    The child process receives one JSON request per line and responds with one
    JSON object per line. This lets Cater talk to wrappers around Codex CLI,
    Claude Code, Gemini CLI, or any other local model runner without hardwiring
    those tools into the core runtime.
    """

    def __init__(
        self,
        name: str,
        command: list[str],
        *,
        cwd: str | Path | None = None,
        timeout: float = 120.0,
        restart_limit: int = 1,
    ):
        self.name = name
        self.command = list(command)
        self.cwd = str(cwd) if cwd is not None else None
        self.timeout = timeout
        self.restart_limit = restart_limit
        self._proc: subprocess.Popen[str] | None = None
        self._lock = threading.Lock()

    def complete(self, *, role: str, prompt: str, context: dict[str, Any]) -> DriverResponse:
        data = self._request(
            {"type": "complete", "role": role, "prompt": prompt, "context": context}
        )
        text = data.get("text")
        if not isinstance(text, str):
            raise DriverProtocolError(f"{self.name} response must include string text")
        tokens = data.get("tokens", 0)
        if not isinstance(tokens, int):
            raise DriverProtocolError(f"{self.name} response tokens must be an integer")
        meta = data.get("meta") or {}
        if not isinstance(meta, dict):
            raise DriverProtocolError(f"{self.name} response meta must be an object")
        return DriverResponse(text=text, tokens=tokens, meta={**meta, "driver": self.name})

    def probe(self) -> bool:
        try:
            return self._request({"type": "ping"}).get("pong") is True
        except DriverError:
            return False

    def close(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        for stream in (proc.stdin, proc.stdout):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()

    def _request(self, payload: dict[str, Any]) -> dict[str, Any]:
        last_error: DriverError | None = None
        for attempt in range(self.restart_limit + 1):
            with self._lock:
                request_payload = dict(payload)
                request_payload["id"] = uuid.uuid4().hex
                try:
                    return self._send_once(request_payload)
                except DriverError as exc:
                    last_error = exc
                    self._restart()
            if attempt >= self.restart_limit:
                break
        assert last_error is not None
        raise last_error

    def _send_once(self, payload: dict[str, Any]) -> dict[str, Any]:
        proc = self._ensure_started()
        assert proc.stdin is not None
        try:
            proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise DriverUnavailable(f"{self.name} stdin is not writable") from exc

        line = self._readline(proc)
        if line == "":
            raise DriverUnavailable(f"{self.name} exited before responding")
        try:
            data = json.loads(line)
        except json.JSONDecodeError as exc:
            raise DriverProtocolError(f"{self.name} output is not valid NDJSON") from exc
        if not isinstance(data, dict):
            raise DriverProtocolError(f"{self.name} output must be a JSON object")
        if data.get("id") != payload.get("id"):
            raise DriverProtocolError(f"{self.name} response id mismatch")
        if data.get("ok") is False:
            raise DriverUnavailable(str(data.get("error") or f"{self.name} returned an error"))
        if data.get("ok") is not True:
            raise DriverProtocolError(f"{self.name} response must include ok=true/false")
        return data

    def _ensure_started(self) -> subprocess.Popen[str]:
        if self._proc is not None and self._proc.poll() is None:
            return self._proc
        self._proc = subprocess.Popen(
            self.command,
            cwd=self.cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        return self._proc

    def _restart(self) -> None:
        self.close()

    def _readline(self, proc: subprocess.Popen[str]) -> str:
        assert proc.stdout is not None
        out: queue.Queue[str] = queue.Queue(maxsize=1)

        def reader() -> None:
            try:
                out.put(proc.stdout.readline())
            except ValueError:
                out.put("")

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        try:
            return out.get(timeout=self.timeout)
        except queue.Empty as exc:
            raise DriverUnavailable(f"{self.name} did not respond within {self.timeout}s") from exc


def driver_from_dict(data: dict[str, Any]) -> Driver:
    kind = data.get("kind")
    name = data.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError("driver config requires a non-empty name")
    if kind == "scripted":
        script = data.get("script") or []
        if not isinstance(script, list) or not all(isinstance(item, str) for item in script):
            raise ValueError(f"scripted driver {name!r} requires a string array script")
        tokens_per_call = int(data.get("tokens_per_call", 200))
        if tokens_per_call < 0:
            raise ValueError(f"scripted driver {name!r} tokens_per_call must be non-negative")
        return ScriptedDriver(
            name,
            list(script),
            tokens_per_call=tokens_per_call,
        )
    if kind == "cli":
        command = data.get("command")
        if not isinstance(command, list) or not all(isinstance(item, str) for item in command):
            raise ValueError(f"CLI driver {name!r} requires a string array command")
        return CliDriver(
            name,
            command,
            cwd=data.get("cwd"),
            timeout=float(data.get("timeout", 120.0)),
            restart_limit=int(data.get("restart_limit", 1)),
        )
    raise ValueError(f"unknown driver kind: {kind!r}")


def load_drivers(path: str | Path) -> dict[str, Driver]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    items = raw.get("drivers") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        raise ValueError("driver config must be a list or an object with drivers")
    drivers = [driver_from_dict(item) for item in items]
    return {driver.name: driver for driver in drivers}
