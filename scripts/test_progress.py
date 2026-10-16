"""Bounded, best-effort CI evidence; never controls test execution."""

from __future__ import annotations

import json
import sys
import threading
import time

STALL_INTERVAL_SECONDS = 120.0
CLOSE_BUDGET_SECONDS = 1.0
NOTICE_LIMIT = 8
STACK_LIMIT = 8
WALK_LIMIT = 64
TEXT_LIMIT = 250

_FIXTURES = {
    ("suite.py", "_handleModuleFixture"): "module_setup",
    ("suite.py", "_handleModuleTearDown"): "module_teardown",
    ("suite.py", "_handleClassSetUp"): "class_setup",
    ("suite.py", "_tearDownPreviousClass"): "class_teardown",
    ("case.py", "doModuleCleanups"): "module_cleanup",
    ("case.py", "doClassCleanups"): "class_cleanup",
}


def sample_main_stack(ident: int) -> tuple[str | None, list[dict]]:
    """Copy metadata only, dropping live frames before returning or raising."""
    frames = frame = None
    stack = []
    fixture = None
    try:
        frames = sys._current_frames()
        frame = frames.get(ident)
        frames.clear()
        for _ in range(WALK_LIMIT):
            if frame is None:
                break
            code = frame.f_code
            filename = code.co_filename.replace("\\", "/").rsplit("/", 1)[-1]
            function = code.co_name
            if fixture is None:
                fixture = _FIXTURES.get((filename, function))
            if len(stack) < STACK_LIMIT:
                stack.append({"file": filename[:TEXT_LIMIT], "function": function[:TEXT_LIMIT],
                              "line": frame.f_lineno})
            frame = frame.f_back
        return fixture, stack
    finally:
        frame = frames = None


class ProgressWatchdog:
    def __init__(self, stream, escape, *, interval: float | None = None):
        self.interval = STALL_INTERVAL_SECONDS if interval is None else interval
        self._stream = stream
        self._escape = escape
        self._ident = threading.main_thread().ident
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = None
        self._origin = time.monotonic()
        self._state = ("discovery", None, self._origin)
        self._completed_cases = 0
        self._selected_total = None

    def start(self) -> None:
        try:
            self._thread = threading.Thread(target=self._watch, name="test-progress-watchdog", daemon=True)
            self._thread.start()
        except Exception:
            self._thread = None
            self._stop.set()

    def boundary(self, phase: str, case: str | None = None) -> None:
        with self._lock:
            self._state = (phase, case, time.monotonic())

    def start_case(self, test) -> None:
        try:
            self.boundary("case", test.id()[:TEXT_LIMIT])
        except Exception:
            self.boundary("case")

    def select(self, total: int) -> None:
        with self._lock:
            self._selected_total = total
            self._state = ("fixture_or_suite", None, time.monotonic())

    def stop_case(self) -> None:
        with self._lock:
            self._completed_cases += 1
            self._state = ("fixture_or_suite", None, time.monotonic())

    def _watch(self) -> None:
        try:
            for _ in range(NOTICE_LIMIT):
                while not self._stop.wait(self.interval):
                    with self._lock:
                        phase, case, changed = self._state
                        completed, total = self._completed_cases, self._selected_total
                    now = time.monotonic()
                    fixture, stack = sample_main_stack(self._ident)
                    with self._lock:
                        if self._state != (phase, case, changed):
                            continue
                    if self._stop.is_set():
                        return
                    payload = {"phase": fixture if phase == "fixture_or_suite" and fixture else phase,
                               "case": case, "elapsed_seconds": round(max(0.0, now - self._origin), 3),
                               "since_progress_seconds": round(max(0.0, now - changed), 3),
                               "stalled": now - changed >= self.interval,
                               "completed_cases": completed, "selected_total": total,
                               "stack": stack}
                    message = self._escape(json.dumps(payload, ensure_ascii=True, separators=(",", ":")))
                    stream = self._stream
                    if self._stop.is_set() or stream is None:
                        return
                    stream.write("\n::notice title=Test progress::" + message + "\n")
                    # A blocked write can outlive close's budget. Never flush it after stop.
                    if self._stop.is_set():
                        return
                    stream.flush()
                    break
                else:
                    return
        except Exception:
            # Diagnostic failures must not replace the suite's outcome or leak details.
            return

    def close(self) -> None:
        try:
            self._stop.set()
            self._stream = None
            if self._thread is not None:
                self._thread.join(timeout=CLOSE_BUDGET_SECONDS)
        finally:
            self._stream = None
