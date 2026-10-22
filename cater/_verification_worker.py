"""Wait for the parent to establish process containment before running a check."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path


def main() -> None:
    command = json.loads(sys.stdin.buffer.read())
    # Existing callers send only argv. Explicit input uses a separate envelope,
    # never a temporary script or an implicit change to the configured command.
    input_bytes = None
    if isinstance(command, dict):
        if (set(command) != {"argv", "stdin_utf8"} or type(command["argv"]) is not list
                or not command["argv"] or any(type(arg) is not str for arg in command["argv"])
                or type(command["stdin_utf8"]) is not str):
            raise ValueError("Invalid command input envelope")
        input_bytes = command["stdin_utf8"].encode("utf-8")
        if len(input_bytes) > 65536:
            raise ValueError("Command input exceeds the byte limit")
        command = command["argv"]
    try:
        if input_bytes is None:
            code = subprocess.run(command, stdin=subprocess.DEVNULL, shell=False, check=False).returncode
        else:
            target = subprocess.Popen(command, stdin=subprocess.PIPE, bufsize=0, shell=False)

            def send_input():
                try:
                    with target.stdin as stream:
                        remaining = memoryview(input_bytes)
                        while remaining:
                            written = stream.write(remaining)
                            if not written:
                                break
                            remaining = remaining[written:]
                except OSError:
                    # A target may ignore input or exit before consuming it.
                    pass

            threading.Thread(target=send_input, daemon=True).start()
            code = target.wait()
        result = {"exit_code": code}
    except OSError as exc:
        result = {"exit_code": None, "launch_error": str(exc)}
    Path(sys.argv[1]).write_text(json.dumps(result), encoding="utf-8")
    if input_bytes is not None:
        # A descendant may keep stdin open after the target exits. Publish the
        # target's status and exit without waiting on that feeder or finalizers;
        # the parent then closes the existing process scope, including descendants.
        os._exit(0)


if __name__ == "__main__":
    main()
