"""Bounded tools for a trusted host workspace, using only the standard library.

This is not an OS sandbox. Allowed programs run on the host with their normal
permissions; aliases authorize programs, not their arguments or effects. Hosts
must protect roots and ancestors from hostile replacement. Atomic publication
and advisory root locks provide CAS between cooperating executors; portable
stdlib APIs cannot atomically compare and replace against arbitrary external
writers. Observed external changes are rejected, including a final prepublish
recheck. Results are observations, never task acceptance.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import errno
import hashlib
import json
import math
import ntpath
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import threading
import time
from types import MappingProxyType
from typing import Iterator, Mapping
import unicodedata

from .processes import ProcessScope
from .verification import _Capture

MAX_FILE_BYTES = 1024 * 1024
DEFAULT_READ_BYTES = 16000
MAX_READ_BYTES = 65536
MAX_ENTRIES = 200
MAX_MATCHES = 200
MAX_VISITED = 2000
MAX_SEARCH_BYTES = 8 * MAX_FILE_BYTES
MAX_RESULT_BYTES = 64000
_PROTECTED = {
    ".git", ".ssh", ".aws", ".azure", ".gnupg", ".kube", ".docker",
    ".config", ".codex", ".pki", ".terraform", ".pgpass", ".my.cnf",
    ".boto", ".s3cfg", ".vault-token", "auth.json", "terraform.tfstate",
    ".netrc", "_netrc", ".npmrc", ".pypirc", ".gitconfig", ".git-credentials",
    ".bash_history", ".zsh_history", ".python_history", "credentials",
    "credentials.json", "secrets", "secrets.json", "secrets.yaml", "secrets.yml",
    "gcloud", "application_default_credentials.json", "id_rsa", "id_dsa",
    "id_ecdsa", "id_ed25519",
}
_VENDOR = {"vendor", "node_modules", ".venv", "venv", "__pycache__", ".tox"}
_SECRET_ENV = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH|COOKIE", re.I)


class _Denied(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _deny(code: str, message: str) -> None:
    raise _Denied(code, message)


def _protected(part: str) -> bool:
    name = unicodedata.normalize("NFKC", part).casefold()
    return (name in _PROTECTED or name.startswith((".env", ".workspace-tmp-"))
            or name.endswith((".env", ".pem", ".key", ".p12", ".pfx", ".keystore"))
            or ".env." in name)


def _inspect(path: Path, directory: bool | None = None) -> os.stat_result:
    info = path.lstat()
    if (stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & 0x400):
        _deny("unsafe_path", "Links and reparse points are forbidden.")
    is_dir = stat.S_ISDIR(info.st_mode)
    if not is_dir and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
        _deny("unsafe_path", "Special files and hard links are forbidden.")
    if directory is not None and directory != is_dir:
        _deny("wrong_type", "Unexpected filesystem entry type.")
    return info


def _directories(path: Path) -> None:
    for ancestor in (*reversed(path.parents), path):
        _inspect(ancestor, True)


def _timeout(value: object) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 600:
        raise ValueError("timeout must be greater than zero and at most 600 seconds")
    return float(value)


@dataclass(frozen=True)
class WorkspacePolicy:
    root: Path
    mode: str
    commands: Mapping[str, tuple[str, ...]]
    env_allowlist: tuple[str, ...] = ()
    timeout: float = 60

    @classmethod
    def from_dict(cls, data: dict) -> WorkspacePolicy:
        if not isinstance(data, dict) or data.keys() - {"root", "mode", "commands", "env_allowlist", "timeout"}:
            raise ValueError("invalid workspace policy fields")
        root_value = data.get("root")
        if not isinstance(root_value, str) or not root_value or "\0" in root_value:
            raise ValueError("root must name an absolute existing directory")
        root = Path(root_value)
        if not root.is_absolute() or ".." in root.parts:
            raise ValueError("root must name an absolute existing directory")
        if any(_protected(part) for part in root.parts):
            raise ValueError("protected workspace root")
        try:
            _directories(root)
        except (OSError, ValueError):
            raise ValueError("root must be an existing directory without links") from None
        # Canonicalize only after checking links, including Windows short names.
        root = Path(os.path.realpath(root))
        if any(_protected(part) for part in root.parts):
            raise ValueError("protected workspace root")
        _directories(root)
        mode = data.get("mode", "read_only")
        if mode not in ("read_only", "edit", "execute"):
            raise ValueError("invalid workspace mode")
        commands = data.get("commands", {})
        if not isinstance(commands, dict) or len(commands) > 100 or (commands and mode != "execute"):
            raise ValueError("commands require execute mode")
        normalized = {}
        for alias, argv in commands.items():
            if (not isinstance(alias, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", alias)
                    or not isinstance(argv, list) or not argv or len(argv) > 100
                    or any(not isinstance(arg, str) or "\0" in arg or len(arg) > 8192 for arg in argv)
                    or not argv[0] or sum(len(arg.encode("utf-8")) for arg in argv) > 32000):
                raise ValueError("invalid command alias or argv prefix")
            normalized[alias] = tuple(argv)
        env = data.get("env_allowlist", [])
        if (not isinstance(env, list) or len(env) > 100
                or any(not isinstance(key, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", key)
                       or _SECRET_ENV.search(key) for key in env)):
            raise ValueError("invalid or secret environment variable allowlist")
        return cls(root, mode, MappingProxyType(normalized), tuple(sorted(set(env))),
                   _timeout(data.get("timeout", 60)))

    def as_dict(self) -> dict:
        return {"root": str(self.root), "mode": self.mode,
                "commands": {key: list(self.commands[key]) for key in sorted(self.commands)},
                "env_allowlist": list(self.env_allowlist), "timeout": self.timeout}

    @property
    def fingerprint(self) -> str:
        encoded = json.dumps(self.as_dict(), sort_keys=True, ensure_ascii=True,
                             separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def _parts(value: object) -> tuple[str, ...]:
    if not isinstance(value, str) or not value or len(value) > 2048:
        _deny("invalid_path", "A bounded relative path is required.")
    if ntpath.splitdrive(value)[0] or value.startswith(("/", "\\")):
        _deny("invalid_path", "Absolute paths are forbidden.")
    parts = value.replace("\\", "/").split("/")
    result = []
    for part in parts:
        if part in ("", "."):
            continue
        normalized = unicodedata.normalize("NFKC", part)
        for candidate in (part, normalized):
            base = candidate.split(".")[0].rstrip(" ").upper()
            if (candidate == ".." or candidate.endswith((".", " "))
                    or candidate != candidate.strip() or re.search(r'[\\/:*?"<>|]', candidate)
                    or any(unicodedata.category(c).startswith("C") for c in candidate)
                    or base in {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
                    or re.fullmatch(r"(?:COM|LPT)[1-9]", base)
                    or re.search(r"~[0-9]", candidate)):
                _deny("invalid_path", "Unsafe path component.")
        if _protected(part):
            _deny("protected_path", "Protected paths are forbidden.")
        if len(part.encode("utf-8")) > 240:
            _deny("invalid_path", "Path component is too long.")
        result.append(part)
    if len(result) > 32:
        _deny("invalid_path", "Path is too deep.")
    return tuple(result)


def _signature(info: os.stat_result) -> tuple:
    # Windows fstat/lstat can expose change time versus birth time as ctime.
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns if os.name != "nt" else None)


def _open_file(path: Path, flags: int, *, create: bool = False) -> int:
    _directories(path.parent)
    before = None if create else _inspect(path, False)
    fd = os.open(path, flags | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0), 0o600)
    try:
        after, opened = _inspect(path, False), os.fstat(fd)
        _directories(path.parent)
        if not os.path.samestat(after, opened) or (before is not None and not os.path.samestat(before, opened)):
            _deny("conflict", "File changed during access.")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read_file(path: Path) -> tuple[bytes, tuple]:
    with os.fdopen(_open_file(path, os.O_RDONLY), "rb") as stream:
        before = os.fstat(stream.fileno())
        if before.st_size > MAX_FILE_BYTES:
            _deny("too_large", "File exceeds the size limit.")
        body = stream.read(MAX_FILE_BYTES + 1)
        after = os.fstat(stream.fileno())
    if len(body) > MAX_FILE_BYTES:
        _deny("too_large", "File exceeds the size limit.")
    if _signature(before) != _signature(after) or _signature(after) != _signature(_inspect(path, False)):
        _deny("conflict", "File changed during access.")
    return body, _signature(after)


def _text(body: bytes) -> str:
    try:
        value = body.decode("utf-8")
    except UnicodeError:
        _deny("binary_file", "Only UTF-8 text files are supported.")
    if any(ord(c) < 32 and c not in "\t\r\n\f" for c in value):
        _deny("binary_file", "Binary files are forbidden.")
    return value


def _integer(value: object, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        _deny("invalid_arguments", "Numeric argument is outside its allowed range.")
    return value


@contextmanager
def _write_lock(root: Path) -> Iterator[None]:
    # Never unlink: all contenders must keep locking the same inode.
    lock_root = Path(tempfile.gettempdir()) / "cater-workspace-locks"
    lock_root.mkdir(mode=0o700, exist_ok=True)
    _directories(lock_root)
    key = hashlib.sha256(os.path.normcase(str(root)).encode("utf-8")).hexdigest()
    path = lock_root / key
    try:
        fd = _open_file(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, create=True)
    except FileExistsError:
        fd = _open_file(path, os.O_RDWR)
    try:
        if os.name == "nt":
            import msvcrt
            lock = lambda: msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            unlock = lambda: msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            lock = lambda: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            unlock = lambda: fcntl.flock(fd, fcntl.LOCK_UN)
        deadline = time.monotonic() + 5
        while True:
            try:
                lock()
                break
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise
                if time.monotonic() >= deadline:
                    _deny("busy", "Workspace write lock is busy.")
                time.sleep(0.01)
        try:
            yield
        finally:
            unlock()
    finally:
        os.close(fd)


class WorkspaceExecutor:
    def __init__(self, policy: WorkspacePolicy):
        # Revalidate and detach configuration even when callers construct a policy directly.
        self.policy = WorkspacePolicy.from_dict(policy.as_dict())

    def schemas(self) -> list[dict]:
        string = {"type": "string"}
        relative = {"type": "string", "description":
            "Relative to the bound workspace root. Use '.' for that root; do not prepend its title or directory name."}
        path = {**relative, "default": "."}
        definitions = [
            ("workspace_list", "List safe workspace entries (bounded).", {"path": path}, []),
            ("workspace_read", "Read UTF-8 text using byte offsets; sha256 covers the whole file.",
             {"path": relative, "offset": {"type": "integer", "minimum": 0, "default": 0},
              "limit": {"type": "integer", "minimum": 1, "maximum": MAX_READ_BYTES, "default": DEFAULT_READ_BYTES}}, ["path"]),
            ("workspace_search", "Search literal UTF-8 text, skipping binary, vendor and protected paths.",
             {"query": string, "path": path,
              "limit": {"type": "integer", "minimum": 1, "maximum": MAX_MATCHES, "default": 50}}, ["query"]),
        ]
        if self.policy.mode in {"edit", "execute"}:
            definitions.append(("workspace_write", "Atomically write UTF-8 text. To update an existing file, first workspace_read it and pass that result's sha256 as expected_sha256. Null is ONLY for creating a file that does not exist. On conflict, read again and reconcile before retrying; never blindly overwrite.",
                                {"path": relative, "content": string, "expected_sha256": {
                                    "type": ["string", "null"], "description":
                                    "Copy sha256 from the latest workspace_read of this file; null only if the file does not exist."}},
                                ["path", "content", "expected_sha256"]))
        if self.policy.mode == "execute":
            definitions.append(("workspace_run", "Run a trusted host program alias (not an OS sandbox or task acceptance).",
                                {"command": {"type": "string", "enum": sorted(self.policy.commands)},
                                 "args": {"type": "array", "items": string, "default": []}, "cwd": path,
                                 "timeout": {"type": "number", "exclusiveMinimum": 0, "maximum": self.policy.timeout,
                                             "default": self.policy.timeout}}, ["command"]))
        return [{"name": name, "description": description,
                 "input_schema": {"type": "object", "properties": properties,
                                  "required": required, "additionalProperties": False}}
                for name, description, properties, required in definitions]

    def execute(self, name: str, arguments: dict) -> dict:
        try:
            schemas = {item["name"]: item["input_schema"] for item in self.schemas()}
            if not isinstance(name, str) or name not in schemas:
                _deny("tool_denied", "Unknown or unavailable workspace tool.")
            schema = schemas[name]
            if (not isinstance(arguments, dict) or arguments.keys() - schema["properties"].keys()
                    or not set(schema["required"]) <= arguments.keys()):
                _deny("invalid_arguments", "Invalid workspace tool arguments.")
            if name == "workspace_run" and any(arguments.get(key, 0) is None for key in ("args", "timeout")):
                _deny("invalid_arguments", "Command args and timeout cannot be null.")
            result = getattr(self, "_" + name.removeprefix("workspace_"))(**arguments)
            return {"status": "ok", **result}
        except _Denied as exc:
            return {"status": "error", "error": {"code": exc.code, "message": str(exc)}}
        except (OSError, ValueError, TypeError, OverflowError):
            # OS messages can contain host paths, command arguments or credentials.
            return {"status": "error", "error": {"code": "operation_failed", "message": "Workspace operation failed."}}

    def _path(self, value: object, *, create_parents: bool = False) -> Path:
        parts = _parts(value)
        _directories(self.policy.root)
        current = self.policy.root
        for part in parts[:-1]:
            current = current / part
            if create_parents:
                try:
                    current.mkdir(mode=0o700)
                except FileExistsError:
                    pass
            _inspect(current, True)
        return self.policy.root.joinpath(*parts)

    def _relative(self, path: Path) -> str:
        return path.relative_to(self.policy.root).as_posix()

    def _list(self, path: str = ".") -> dict:
        target = self._path(path)
        _inspect(target, True)
        entries, visited, used, truncated = [], 0, 0, False
        with os.scandir(target) as iterator:
            for entry in iterator:
                visited += 1
                if visited > MAX_VISITED or len(entries) >= MAX_ENTRIES:
                    truncated = True
                    break
                try:
                    child = self._path(self._relative(target / entry.name))
                    info = _inspect(child)
                except (_Denied, OSError, UnicodeError):
                    continue
                item = {"path": self._relative(child), "type": "directory" if stat.S_ISDIR(info.st_mode) else "file",
                        "bytes": info.st_size if stat.S_ISREG(info.st_mode) else None}
                used += len(json.dumps(item, ensure_ascii=True).encode())
                if used > MAX_RESULT_BYTES:
                    truncated = True
                    break
                entries.append(item)
        entries.sort(key=lambda item: item["path"])
        return {"path": self._relative(target), "entries": entries, "truncated": truncated}

    def _read(self, path: str, offset: int = 0, limit: int = DEFAULT_READ_BYTES) -> dict:
        offset = _integer(offset, 0, MAX_FILE_BYTES)
        limit = _integer(limit, 1, MAX_READ_BYTES)
        target = self._path(path)
        body, _ = _read_file(target)
        _text(body)
        excerpt = body[offset:offset + limit]
        # A byte window can bisect UTF-8. Never expand it with replacement bytes.
        content = excerpt.decode("utf-8", errors="ignore")
        return {"path": self._relative(target), "sha256": hashlib.sha256(body).hexdigest(),
                "bytes": len(body), "offset": offset, "returned_bytes": len(content.encode("utf-8")),
                "content": content, "encoding": "utf-8",
                "truncated": offset > 0 or len(excerpt) < len(body)
                or len(content.encode("utf-8")) != len(excerpt)}

    def _search(self, query: str, path: str = ".", limit: int = 50) -> dict:
        if not isinstance(query, str) or not query or len(query.encode("utf-8")) > 1024:
            _deny("invalid_arguments", "A nonempty bounded literal query is required.")
        limit = _integer(limit, 1, MAX_MATCHES)
        target = self._path(path)
        _inspect(target)
        if any(part.casefold() in _VENDOR for part in target.relative_to(self.policy.root).parts):
            _deny("search_excluded", "Vendor paths are excluded from search.")
        pending = [target]
        matches, visited, scanned, used = [], 0, 0, 0
        truncated = False
        result_full = False
        deadline = time.monotonic() + 5
        while pending:
            current = pending.pop()
            visited += 1
            if visited > MAX_VISITED or time.monotonic() > deadline:
                truncated = True
                break
            try:
                current = self._path(self._relative(current))
                info = _inspect(current)
                if stat.S_ISDIR(info.st_mode):
                    with os.scandir(current) as iterator:
                        for entry in iterator:
                            visited += 1
                            if len(pending) + visited >= MAX_VISITED:
                                truncated = True
                                break
                            if entry.name.casefold() not in _VENDOR and not _protected(entry.name):
                                pending.append(current / entry.name)
                    continue
                if info.st_size + scanned > MAX_SEARCH_BYTES:
                    truncated = True
                    break
                if info.st_size > MAX_FILE_BYTES:
                    truncated = True
                    continue
                body, _ = _read_file(current)
                scanned += len(body)
                content = _text(body)
            except (_Denied, OSError, UnicodeError):
                continue
            sha = hashlib.sha256(body).hexdigest()
            for number, line in enumerate(content.splitlines(), 1):
                column = line.find(query)
                if column < 0:
                    continue
                snippet = line[max(0, column - 120):column + len(query) + 120]
                item = {"path": self._relative(current), "line": number, "column": column + 1,
                        "output": snippet, "sha256": sha, "bytes": len(body),
                        "truncated": len(snippet) < len(line)}
                size = len(json.dumps(item, ensure_ascii=True).encode())
                if len(matches) >= limit or used + size > MAX_RESULT_BYTES:
                    truncated = True
                    result_full = True
                    break
                matches.append(item)
                used += size
            if result_full:
                break
        return {"path": self._relative(target), "matches": matches, "scanned_bytes": scanned,
                "truncated": truncated}

    def _write(self, path: str, content: str, expected_sha256: str | None) -> dict:
        # Validate the entire request before creating even a parent directory.
        target = self.policy.root.joinpath(*_parts(path))
        _directories(self.policy.root)
        if target == self.policy.root or not isinstance(content, str):
            _deny("invalid_arguments", "A file path and UTF-8 content are required.")
        if len(content) > MAX_FILE_BYTES:
            _deny("too_large", "Content exceeds the file size limit.")
        body = content.encode("utf-8")
        if len(body) > MAX_FILE_BYTES:
            _deny("too_large", "Content exceeds the file size limit.")
        _text(body)
        if expected_sha256 is not None and (not isinstance(expected_sha256, str)
                                           or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)):
            _deny("invalid_arguments", "expected_sha256 must be null or a lowercase SHA-256 digest.")
        with _write_lock(self.policy.root):
            # _path permits missing parents only for a validated creation request.
            target = self._path(path, create_parents=expected_sha256 is None)
            original = self._check_expected(target, expected_sha256)
            fd, temporary = tempfile.mkstemp(prefix=".workspace-tmp-", dir=target.parent)
            staging = Path(temporary)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(body)
                    stream.flush()
                    os.fsync(stream.fileno())
                if original is not None:
                    os.chmod(staging, stat.S_IMODE(_inspect(target, False).st_mode) & 0o777)
                self._path(path)
                current = self._check_expected(target, expected_sha256)
                if current != original:
                    _deny("conflict", "File changed before publication.")
                if original is None:
                    # link is atomic create-if-absent, unlike replace/rename on POSIX.
                    os.link(staging, target, follow_symlinks=False)
                    staging.unlink()
                else:
                    os.replace(staging, target)
            finally:
                if os.path.lexists(staging):
                    staging.unlink()
        return {"path": self._relative(target), "sha256": hashlib.sha256(body).hexdigest(),
                "bytes": len(body), "created": original is None, "truncated": False}

    def _check_expected(self, path: Path, expected: str | None) -> tuple | None:
        try:
            body, signature = _read_file(path)
        except FileNotFoundError:
            if expected is not None:
                _deny("conflict", "Expected file no longer exists.")
            return None
        if expected is None or hashlib.sha256(body).hexdigest() != expected:
            _deny("conflict", "File hash does not match expected_sha256. Read this file again with workspace_read, reconcile any changes, then use its returned sha256; null is only for new files.")
        _text(body)
        return signature

    def _run(self, command: str, args: list[str] | None = None, cwd: str = ".",
             timeout: float | None = None) -> dict:
        if not isinstance(command, str) or command not in self.policy.commands:
            _deny("unknown_command", "Unknown command alias.")
        args = [] if args is None else args
        if (not isinstance(args, list) or len(args) > 100
                or any(not isinstance(arg, str) or "\0" in arg or len(arg.encode("utf-8")) > 8192 for arg in args)
                or sum(len(arg.encode("utf-8")) for arg in args) > 32000):
            _deny("invalid_arguments", "Invalid command argument list.")
        timeout = self.policy.timeout if timeout is None else _timeout(timeout)
        if timeout > self.policy.timeout:
            _deny("invalid_arguments", "Timeout exceeds workspace policy.")
        directory = self._path(cwd)
        _inspect(directory, True)
        env = {"PATH": os.defpath, "TEMP": tempfile.gettempdir(), "TMP": tempfile.gettempdir()}
        if os.name == "nt":
            for key in ("SystemRoot", "WINDIR"):
                if key in os.environ:
                    env[key] = os.environ[key]
        for key in self.policy.env_allowlist:
            if key in os.environ:
                env[key] = os.environ[key]
        argv = [*self.policy.commands[command], *args]
        stdout, stderr = _Capture(), _Capture()
        started = time.monotonic()
        process = scope = None
        readers = []
        exit_code = None
        outcome = "launch_error"
        cleanup_failed = False
        with tempfile.TemporaryDirectory(prefix="cater-workspace-run-") as tmp:
            result_path = Path(tmp) / "result.json"
            try:
                process = subprocess.Popen(
                    [sys.executable, "-I", str(Path(__file__).with_name("_verification_worker.py")), str(result_path)],
                    cwd=directory, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    shell=False, start_new_session=os.name != "nt",
                )
                scope = ProcessScope(process)
                readers = [threading.Thread(target=capture.drain, args=(stream,), daemon=True)
                           for capture, stream in ((stdout, process.stdout), (stderr, process.stderr))]
                for reader in readers:
                    reader.start()
                process.stdin.write(json.dumps(argv).encode("utf-8"))
                process.stdin.close()
                process.wait(timeout=max(0.001, timeout - (time.monotonic() - started)))
                # Worker errors are intentionally not echoed: they may include host secrets.
                with result_path.open("rb") as stream:
                    result = json.loads(stream.read(8193))
                if result.get("launch_error") is None and type(result.get("exit_code")) is int:
                    exit_code = result["exit_code"]
                    outcome = "passed" if exit_code == 0 else "failed"
            except subprocess.TimeoutExpired:
                outcome = "timeout"
            except (OSError, ValueError):
                pass
            finally:
                if process is not None:
                    try:
                        if scope is not None:
                            scope.close()
                        elif process.poll() is None:
                            process.kill()
                    except OSError:
                        cleanup_failed = True
                        if process.poll() is None:
                            process.kill()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        cleanup_failed = True
                        process.kill()
                        process.wait(timeout=5)
                    if process.stdin is not None and not process.stdin.closed:
                        process.stdin.close()
                    if not readers:
                        for stream in (process.stdout, process.stderr):
                            if stream is not None:
                                stream.close()
                deadline = time.monotonic() + 2
                for reader in readers:
                    reader.join(timeout=max(0, deadline - time.monotonic()))
        out_meta, err_meta = stdout.summary(record_output=True), stderr.summary(record_output=True)
        complete = out_meta["complete"] and err_meta["complete"]
        if outcome == "passed" and (not complete or cleanup_failed):
            outcome = "failed"
        ok = outcome == "passed"
        result = {"status": "ok" if ok else "error", "command": command, "cwd": self._relative(directory),
                  "argv": argv if exit_code is not None else [],
                  "outcome": outcome, "exit_code": exit_code, "timed_out": outcome == "timeout",
                  "duration_ms": round((time.monotonic() - started) * 1000),
                  "stdout": out_meta, "stderr": err_meta,
                  "bytes": out_meta["bytes"] + err_meta["bytes"],
                  "truncated": out_meta["truncated"] or err_meta["truncated"], "complete": complete,
                  "process_scope": scope.kind if scope is not None else None}
        result["output"] = out_meta["excerpt"] + err_meta["excerpt"]
        if not ok:
            result["error"] = {"code": outcome if not cleanup_failed and complete else "process_failed",
                               "message": "Workspace command did not complete successfully."}
        return result
