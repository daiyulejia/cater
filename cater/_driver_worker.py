"""Attach the NDJSON driver's process scope before permitting it to start."""

import json
import os
import subprocess
import sys


def main() -> int:
    # No target or request exists until the parent attaches this worker to its scope.
    command = json.loads(sys.stdin.buffer.readline().decode("utf-8"))
    if not isinstance(command, list) or not command or not all(isinstance(item, str) for item in command):
        return 1
    sys.stdout.write('{"ready":true}\n')
    sys.stdout.flush()
    try:
        # Python CLI adapters otherwise use the Windows locale for inherited
        # pipes, even though the NDJSON transport contract is UTF-8.
        child = subprocess.Popen(command, shell=False, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        return child.wait()
    except OSError:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
