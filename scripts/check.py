"""Run Cater's local verification suite with the current interpreter."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    if sys.version_info < (3, 11):
        print("Cater requires Python 3.11 or newer.", file=sys.stderr)
        return 1
    commands = [
        [sys.executable, "-u", str(Path(__file__).with_name("run_tests.py")), *([] if argv is None else argv)],
    ]
    for command in commands:
        print("+ " + " ".join(command), file=sys.stderr, flush=True)
        completed = subprocess.run(command)
        if completed.returncode != 0:
            return completed.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
