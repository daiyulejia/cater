"""Explicit local command checks; model output is never executable input here."""

from __future__ import annotations

import hashlib
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

PREVIEW_LIMIT = 8192


@dataclass
class _Capture:
    digest: object = field(default_factory=hashlib.sha256)
    size: int = 0
    preview: bytearray = field(default_factory=bytearray)

    def drain(self, stream: BinaryIO) -> None:
        try:
            while chunk := stream.read(4096):
                self.digest.update(chunk)
                self.size += len(chunk)
                remaining = PREVIEW_LIMIT - len(self.preview)
                if remaining > 0:
                    self.preview.extend(chunk[:remaining])
        finally:
            stream.close()

    def summary(self, *, record_output: bool) -> dict[str, object]:
        result: dict[str, object] = {
            "bytes": self.size,
            "sha256": self.digest.hexdigest(),
            "truncated": self.size > PREVIEW_LIMIT,
        }
        if record_output:
            result["excerpt"] = self.preview.decode("utf-8", errors="replace")
        return result


@dataclass(frozen=True)
class CheckResult:
    payload: dict[str, object]
    stdout_preview: str
    stderr_preview: str


def _git_state(cwd: Path) -> dict[str, object]:
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=cwd, capture_output=True,
            text=True, timeout=3, check=False,
        )
        if head.returncode != 0:
            return {"head": None, "dirty": None}
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=normal"],
            cwd=cwd, capture_output=True, text=True, timeout=3, check=False,
        )
        return {
            "head": head.stdout.strip(),
            "dirty": bool(status.stdout) if status.returncode == 0 else None,
        }
    except (OSError, subprocess.TimeoutExpired):
        return {"head": None, "dirty": None}


def run_check(
    command: list[str], *, cwd: Path, timeout: float = 60,
    record_output: bool = False,
) -> CheckResult:
    if not command or not command[0]:
        raise ValueError("verification command is required after --")
    if not cwd.is_dir():
        raise ValueError(f"verification directory does not exist: {cwd}")
    if not 0 < timeout <= 600:
        raise ValueError("verification timeout must be between 0 and 600 seconds")

    cwd = cwd.resolve()
    started = time.monotonic()
    stdout = _Capture()
    stderr = _Capture()
    timed_out = False
    launch_error: str | None = None
    exit_code: int | None = None
    try:
        process = subprocess.Popen(
            command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            shell=False,
        )
    except OSError as exc:
        launch_error = str(exc)
    else:
        assert process.stdout is not None and process.stderr is not None
        readers = [
            threading.Thread(target=stdout.drain, args=(process.stdout,)),
            threading.Thread(target=stderr.drain, args=(process.stderr,)),
        ]
        for reader in readers:
            reader.start()
        try:
            exit_code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
            process.wait()
        finally:
            for reader in readers:
                reader.join()

    outcome = "timeout" if timed_out else "launch_error" if launch_error else "passed" if exit_code == 0 else "failed"
    payload: dict[str, object] = {
        "source": "explicit_local_cli",
        "command": command,
        "cwd": str(cwd),
        "outcome": outcome,
        "exit_code": exit_code,
        "duration_ms": round((time.monotonic() - started) * 1000),
        "stdout": stdout.summary(record_output=record_output),
        "stderr": stderr.summary(record_output=record_output),
        "git": _git_state(cwd),
    }
    if launch_error:
        payload["launch_error"] = launch_error
    return CheckResult(
        payload=payload,
        stdout_preview=stdout.preview.decode("utf-8", errors="replace"),
        stderr_preview=stderr.preview.decode("utf-8", errors="replace"),
    )
