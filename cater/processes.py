"""Process lifetime containment, not filesystem or network sandboxing."""

from __future__ import annotations

import os
import signal
import subprocess
import sys


def worker_python() -> str:
    """Use a direct interpreter for isolated stdlib bootstrap workers only."""
    if os.name != "nt":
        return sys.executable
    # A Windows venv redirector may spawn its interpreter before Job assignment.
    base = getattr(sys, "_base_executable", None)
    if isinstance(base, str) and base and os.path.splitdrive(base)[0] and os.path.isabs(base):
        return base
    if sys.prefix != sys.base_prefix:
        raise ValueError("A direct base interpreter is required for Windows workers")
    return sys.executable


class ProcessScope:
    def __init__(self, process: subprocess.Popen):
        self.process = process
        self.job = None
        self.kind = "windows_job" if os.name == "nt" else "posix_process_group"
        if os.name == "nt":
            from ._windows_job import WindowsJob
            self.job = WindowsJob(process.pid)

    def close(self) -> None:
        if self.job is not None:
            self.job.close()
        else:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
