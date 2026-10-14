"""Process lifetime containment, not filesystem or network sandboxing."""

from __future__ import annotations

import os
import signal
import subprocess


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
