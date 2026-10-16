"""Explicit local command checks; model output is never executable input here."""

from __future__ import annotations

import hashlib
import math
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

from .processes import ProcessScope as _ProcessScope

PREVIEW_LIMIT = 8192


def output_metadata(output: dict) -> dict:
    """Whitelist capture metadata where raw output must not be repeated."""
    return {key: output[key] for key in ("bytes", "sha256", "truncated", "complete") if key in output}


def _decode_preview(value: bytearray, *, limit: int = PREVIEW_LIMIT, tail: bool = False) -> str:
    decoded = value.decode("utf-8", errors="replace")
    encoded = decoded.encode("utf-8")
    return (encoded[-limit:] if tail else encoded[:limit]).decode("utf-8", errors="ignore")


@dataclass
class _Capture:
    digest: object = field(default_factory=hashlib.sha256)
    size: int = 0
    preview: bytearray = field(default_factory=bytearray)
    tail: bytearray = field(default_factory=bytearray)
    complete: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)

    def drain(self, stream: BinaryIO) -> None:
        try:
            while chunk := stream.read(4096):
                with self.lock:
                    self.digest.update(chunk)
                    self.size += len(chunk)
                    remaining = PREVIEW_LIMIT - len(self.preview)
                    if remaining > 0:
                        self.preview.extend(chunk[:remaining])
                    self.tail.extend(chunk)
                    del self.tail[:-(PREVIEW_LIMIT // 2)]
            with self.lock:
                self.complete = True
        finally:
            stream.close()

    def summary(self, *, record_output: bool) -> dict[str, object]:
        with self.lock:
            decoded_size = len(self.preview.decode("utf-8", errors="replace").encode("utf-8"))
            result: dict[str, object] = {
                "bytes": self.size,
                "sha256": self.digest.hexdigest(),
                "truncated": self.size > PREVIEW_LIMIT or decoded_size > PREVIEW_LIMIT,
                "complete": self.complete,
            }
            if record_output:
                if result["truncated"]:
                    # These are separate segments, never a fabricated continuous log.
                    half = PREVIEW_LIMIT // 2
                    head_size = min(half, self.size // 2)
                    tail_size = min(half, self.size - head_size)
                    result.update(excerpt=_decode_preview(self.preview[:head_size], limit=half),
                                  tail_excerpt=_decode_preview(self.tail[-tail_size:], limit=half, tail=True),
                                  preview_mode="head_tail")
                else:
                    result["excerpt"] = _decode_preview(self.preview)
            return result

    def preview_text(self) -> str:
        with self.lock:
            return _decode_preview(self.preview)


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


def validate_check(command: list[str], timeout: float) -> None:
    if not isinstance(command, list) or not command or not isinstance(command[0], str) or not command[0]:
        raise ValueError("verification command is required after --")
    if any(not isinstance(arg, str) or "\0" in arg for arg in command):
        raise ValueError("verification command must contain valid string arguments")
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 600:
        raise ValueError("verification timeout must be between 0 and 600 seconds")


def run_check(
    command: list[str], *, cwd: Path, timeout: float = 60,
    record_output: bool = False, env: dict[str, str] | None = None,
) -> CheckResult:
    validate_check(command, timeout)
    if not cwd.is_dir():
        raise ValueError(f"verification directory does not exist: {cwd}")

    cwd = cwd.resolve()
    started = time.monotonic()
    stdout = _Capture()
    stderr = _Capture()
    timed_out = False
    launch_error: str | None = None
    exit_code: int | None = None
    cleanup_error: str | None = None
    scope = None
    process = None
    readers = []
    with tempfile.TemporaryDirectory(prefix="cater-check-") as tmp:
        status_path = Path(tmp) / "result.json"
        try:
            process = subprocess.Popen(
                [sys.executable, "-I", str(Path(__file__).with_name("_verification_worker.py")), str(status_path)],
                cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                shell=False, start_new_session=os.name != "nt", env=env,
            )
            # The worker waits on stdin, so no target process can escape before attachment.
            scope = _ProcessScope(process)
            assert process.stdin is not None and process.stdout is not None and process.stderr is not None
            readers = [
                threading.Thread(target=stdout.drain, args=(process.stdout,), daemon=True),
                threading.Thread(target=stderr.drain, args=(process.stderr,), daemon=True),
            ]
            for reader in readers:
                reader.start()
            process.stdin.write(json.dumps(command).encode("utf-8"))
            process.stdin.close()
            process.wait(timeout=max(0.001, timeout - (time.monotonic() - started)))
            result = json.loads(status_path.read_text(encoding="utf-8"))
            exit_code = result["exit_code"]
            launch_error = result.get("launch_error")
        except subprocess.TimeoutExpired:
            timed_out = True
        except (OSError, ValueError) as exc:
            launch_error = str(exc)
        finally:
            if process is not None:
                try:
                    if scope is not None:
                        scope.close()
                    elif process.poll() is None:
                        process.kill()
                except OSError as exc:
                    cleanup_error = str(exc)
                    if process.poll() is None:
                        process.kill()
                process.wait(timeout=5)
                if process.stdin is not None and not process.stdin.closed:
                    process.stdin.close()
                if not readers:
                    for stream in (process.stdout, process.stderr):
                        if stream is not None:
                            stream.close()
            drain_deadline = time.monotonic() + 2
            for reader in readers:
                reader.join(timeout=max(0, drain_deadline - time.monotonic()))

    capture_complete = bool(readers) and stdout.complete and stderr.complete
    if readers and not capture_complete and cleanup_error is None:
        cleanup_error = "output streams did not close after process cleanup"

    outcome = "timeout" if timed_out else "launch_error" if launch_error else "passed" if exit_code == 0 and not cleanup_error else "failed"
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
        "process_scope": scope.kind if scope is not None else None,
    }
    if launch_error:
        payload["launch_error"] = launch_error
    if cleanup_error:
        payload["cleanup_error"] = cleanup_error
    return CheckResult(
        payload=payload,
        stdout_preview=stdout.preview_text(),
        stderr_preview=stderr.preview_text(),
    )
