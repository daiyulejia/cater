"""Owned client launch directories, separate from model and process outcomes."""

from __future__ import annotations

from pathlib import Path
import shutil
import stat
import tempfile

from .events import Actor


HOST = Actor.system("team")

_OS_ERROR_TYPES = (
    (OSError, "OSError"), (PermissionError, "PermissionError"),
    (FileNotFoundError, "FileNotFoundError"), (NotADirectoryError, "NotADirectoryError"),
    (IsADirectoryError, "IsADirectoryError"), (FileExistsError, "FileExistsError"),
    (BlockingIOError, "BlockingIOError"))


def _retained(exc, stage):
    """Expose only fixed host stages and safe fields from exact builtin errors."""
    kind = next((name for cls, name in _OS_ERROR_TYPES if type(exc) is cls), None)
    result = {"status": "retained", "error_type": kind or "OSError", "failure_stage": stage}
    # Use the builtin descriptor, not a potentially overridden attribute.
    descriptor = OSError.__dict__.get("winerror")
    if kind is not None and descriptor is not None:
        value = descriptor.__get__(exc, type(exc))
        if type(value) is int and 0 <= value <= 0xFFFFFFFF:
            result["winerror"] = value
    return result


def _identity(info):
    return {"device": info.st_dev, "inode": info.st_ino}


def _directory(info):
    return (stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode)
            and not getattr(info, "st_file_attributes", 0) & 0x400)


class LaunchDirectory:
    """No finalizer: an unconfirmed client must not lose its cwd during GC."""

    def __init__(self, log, attempt):
        self.log, self.attempt = log, attempt
        self.path = tempfile.mkdtemp(prefix="cater-agent-")
        self.identity = _identity(Path(self.path).lstat())
        self.settlement = None
        self.reference = {"started_seq": attempt.seq, "started_hash": attempt.hash,
                          **{key: attempt.payload[key] for key in ("role", "run_id", "message_seq")}}
        try:
            self.created = log.emit(type="team.external_directory_created", actor=HOST,
                task_id=attempt.task_id,
                payload={**self.reference, "path": self.path, "identity": self.identity})
        except BaseException:
            # No client has been constructed. Do not leak the original error.
            self._remove()
            raise

    def _remove(self):
        stage = "identity_check"
        try:
            path = Path(self.path)
            try:
                info = path.lstat()
            except FileNotFoundError as exc:
                if type(exc) is FileNotFoundError:
                    return {"status": "removed"}
                raise
            if not _directory(info) or _identity(info) != self.identity:
                return {"status": "identity_changed"}
            stage = "remove_tree"
            # Known limitation: a concurrent path replacement after lstat can
            # change the target. The identity check alone does not bind rmtree.
            shutil.rmtree(path)
            return {"status": "removed"}
        except OSError as exc:
            # OS exception text may contain client paths or credentials.
            return _retained(exc, stage)

    def close(self, *, client_stopped):
        if type(client_stopped) is not bool:
            raise ValueError("client_stopped must be a boolean")
        if self.settlement is not None:
            return self.settlement
        result = self._remove() if client_stopped else {"status": "deferred"}
        self.settlement = self.log.emit(type="team.external_directory_settled", actor=HOST,
            task_id=self.attempt.task_id,
            payload={**self.reference, "created_seq": self.created.seq,
                     "created_hash": self.created.hash, "client_stopped": client_stopped, **result})
        return self.settlement


def launch_directories(runs, task_id):
    """Operator diagnostics only; never release unknown effects or replay work."""
    events = runs.rt.log.read(task_id=task_id)
    by_seq = {event.seq: event for event in events}
    rows = []
    for event in events:
        if event.type != "team.external_directory_created" or event.actor != HOST:
            continue
        payload = event.payload
        start_seq = payload.get("started_seq")
        start = by_seq.get(start_seq) if type(start_seq) is int else None
        identity = payload.get("identity")
        if (start is None or not runs._start_valid(start, by_seq) or not runs._bound(event, start)
                or not isinstance(payload.get("path"), str) or not payload["path"]
                or not isinstance(identity, dict) or set(identity) != {"device", "inode"}
                or not all(type(value) is int for value in identity.values())):
            continue
        settled = next((e for e in events
            if e.type == "team.external_directory_settled" and e.actor == HOST
            and e.seq > event.seq and runs._bound(e, start)
            and type(e.payload.get("created_seq")) is int and e.payload["created_seq"] == event.seq
            and e.payload.get("created_hash") == event.hash
            and type(e.payload.get("client_stopped")) is bool
            and (e.payload.get("status") == "deferred" and not e.payload["client_stopped"]
                 or e.payload.get("status") in ("removed", "retained", "identity_changed")
                    and e.payload["client_stopped"])), None)
        settlement = ({key: settled.payload[key] for key in
                       ("status", "client_stopped") if key in settled.payload}
                      if settled else None)
        if settlement and type(settled.payload.get("error_type")) is str:
            error_type = settled.payload["error_type"]
            if error_type in {name for _, name in _OS_ERROR_TYPES}:
                settlement["error_type"] = error_type
        if settlement and settlement["status"] == "retained":
            stage = settled.payload.get("failure_stage")
            winerror = settled.payload.get("winerror")
            if stage in ("identity_check", "remove_tree"):
                settlement["failure_stage"] = stage
            if type(winerror) is int and 0 <= winerror <= 0xFFFFFFFF:
                settlement["winerror"] = winerror
        rows.append({"created_seq": event.seq, **payload,
                     "status": settled.payload["status"] if settled else "unconfirmed",
                     "settlement": settlement})
    return rows
