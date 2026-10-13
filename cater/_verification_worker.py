"""Wait for the parent to establish process containment before running a check."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def main() -> None:
    command = json.loads(sys.stdin.buffer.read())
    try:
        completed = subprocess.run(command, stdin=subprocess.DEVNULL, shell=False, check=False)
        result = {"exit_code": completed.returncode}
    except OSError as exc:
        result = {"exit_code": None, "launch_error": str(exc)}
    Path(sys.argv[1]).write_text(json.dumps(result), encoding="utf-8")


if __name__ == "__main__":
    main()
