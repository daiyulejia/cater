"""Isolated bootstrap for one HTTP exchange; no runtime or tool execution."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cater.native_transport import worker_main


if __name__ == '__main__':
    worker_main()
