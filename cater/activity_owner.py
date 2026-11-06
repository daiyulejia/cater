"""Kernel-held activity ownership, shared by workers and crash recovery."""

from contextlib import contextmanager
import errno
import hashlib
import os
from pathlib import Path
import tempfile

from .events import _windows_io_path


@contextmanager
def activity_owner(database: str | os.PathLike[str], task_id: str, activity_id: str):
    """Try ownership without waiting; closing the handle or process releases it.

    Keep lock files in place: unlinking a released lock can split contenders
    across different inodes. No PID probing or expiry can evict a live worker.
    """
    root = (Path(tempfile.gettempdir()) / "cater-memory-activity-locks" if database == ":memory:"
            else Path(str(Path(_windows_io_path(database)).resolve()) + ".activity-locks"))
    Path(_windows_io_path(str(root))).mkdir(parents=True, exist_ok=True)
    identity = hashlib.sha256((task_id + "\0" + activity_id).encode()).hexdigest()
    with Path(_windows_io_path(str(root / identity))).open("a+b") as handle:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            lock = lambda: msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            unlock = lambda: msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            lock = lambda: fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            unlock = lambda: fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        try:
            lock()
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise
            yield False
            return
        try:
            yield True
        finally:
            unlock()
