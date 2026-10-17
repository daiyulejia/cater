"""Explicit, bounded workspace ZIP snapshots using only the standard library.

The caller owns authorization, binding and current-state checks. Advisory locks
and observed-mutation checks do not replace an OS sandbox or exclude hostile
external writers. File contents are opaque bytes, never extracted or executed.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import stat
import unicodedata
import zipfile

from . import workspace_tools as tools
from .workspace_tools import WorkspaceExecutor

MAX_ARCHIVE_BYTES = 65 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_MEMBER_BYTES = 32 * 1024 * 1024
MAX_FILES = 256


class ArchiveError(ValueError):
    """A snapshot cannot be safely captured or its manifest is invalid."""


def portable_path(value: object) -> str:
    """Accept a portable canonical relative path without rewriting its spelling."""
    if (not isinstance(value, str) or not value or "\\" in value
            or any(part in ("", ".", "..") for part in value.split("/"))):
        raise ArchiveError("A canonical relative file path is required.")
    try:
        parts = tools._parts(value)
        if any(unicodedata.normalize("NFKC", part) in ("", ".", "..") for part in parts):
            raise ValueError()
    except (ValueError, UnicodeError):
        raise ArchiveError("Path is nonportable or protected.") from None
    return value


def _paths(paths: list[str]) -> None:
    # Retain component spellings to reject directory aliases as well as leaves.
    tree = {}
    for path in paths:
        node = tree
        for part in portable_path(path).split("/"):
            if None in node:
                raise ArchiveError("File paths have a prefix collision.")
            key = unicodedata.normalize("NFKC", part).casefold()
            if key not in node:
                node[key] = (part, {})
            spelling, child = node[key]
            if spelling != part:
                raise ArchiveError("File paths have a portable alias collision.")
            node = child
        if node:
            raise ArchiveError("File paths are duplicated or have a prefix collision.")
        node[None] = True


def validate_manifest(manifest: object) -> None:
    """Validate the complete version-one schema, limits and portable namespace."""
    if (not isinstance(manifest, dict)
            or set(manifest) != {"version", "files", "total_bytes"}
            or type(manifest["version"]) is not int or manifest["version"] != 1):
        raise ArchiveError("Invalid archive manifest version or fields.")
    files = manifest["files"]
    total = manifest["total_bytes"]
    if not isinstance(files, list) or not 1 <= len(files) <= MAX_FILES:
        raise ArchiveError("Manifest file count is outside the limit.")
    if type(total) is not int or not 0 <= total <= MAX_TOTAL_BYTES:
        raise ArchiveError("Manifest total bytes is outside the limit.")
    paths, measured = [], 0
    for entry in files:
        if (not isinstance(entry, dict)
                or set(entry) != {"path", "bytes", "sha256", "executable"}):
            raise ArchiveError("Invalid manifest file fields.")
        paths.append(portable_path(entry["path"]))
        size, digest = entry["bytes"], entry["sha256"]
        if type(size) is not int or not 0 <= size <= MAX_MEMBER_BYTES:
            raise ArchiveError("Manifest member bytes is outside the limit.")
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-fA-F]{64}", digest) is None:
            raise ArchiveError("Invalid manifest SHA-256 digest.")
        if type(entry["executable"]) is not bool:
            raise ArchiveError("Invalid manifest executable flag.")
        measured += size
    _paths(paths)
    if paths != sorted(paths):
        raise ArchiveError("Manifest file paths must be sorted.")
    if measured != total:
        raise ArchiveError("Manifest total bytes does not match its files.")


def _state(info: os.stat_result) -> tuple:
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or getattr(info, "st_file_attributes", 0) & 0x400):
        raise ArchiveError("Snapshot requires regular files without links.")
    return (tools._signature(info), info.st_mode, info.st_nlink,
            getattr(info, "st_file_attributes", 0))


class _ArchiveBuffer(io.BytesIO):
    def write(self, body: bytes) -> int:
        if self.tell() + len(body) > MAX_ARCHIVE_BYTES:
            raise ArchiveError("ZIP exceeds the archive byte limit.")
        return super().write(body)


def create_snapshot(workspace: WorkspaceExecutor, paths: list[str]) -> tuple[bytes, dict]:
    """Capture only the selected regular files, rejecting observed changes."""
    if not isinstance(paths, list) or not 1 <= len(paths) <= MAX_FILES:
        raise ArchiveError("Select between one and the maximum number of files.")
    paths = list(paths)
    _paths(paths)
    paths.sort()
    manifest = {"version": 1, "files": [], "total_bytes": 0}
    observed = []
    try:
        with tools._write_lock(workspace.policy.root), _ArchiveBuffer() as buffer:
            with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
                for name in paths:
                    path = workspace._path(name)
                    before = _state(tools._inspect(path, False))
                    with os.fdopen(tools._open_file(path, os.O_RDONLY), "rb") as stream:
                        opened = os.fstat(stream.fileno())
                        if before != _state(opened):
                            raise ArchiveError("File changed before snapshot read.")
                        remaining = min(MAX_MEMBER_BYTES,
                                        MAX_TOTAL_BYTES - manifest["total_bytes"])
                        if opened.st_size > remaining:
                            raise ArchiveError("Snapshot exceeds a member or total byte limit.")
                        body = stream.read(remaining + 1)
                        after = _state(os.fstat(stream.fileno()))
                    if len(body) > remaining:
                        raise ArchiveError("Snapshot exceeds a member or total byte limit.")
                    if (before != after or after != _state(tools._inspect(workspace._path(name), False))
                            or len(body) != opened.st_size):
                        raise ArchiveError("File changed during snapshot read.")
                    executable = bool(opened.st_mode & 0o111)
                    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                    info.create_system = 3
                    info.external_attr = (stat.S_IFREG | (0o755 if executable else 0o644)) << 16
                    archive.writestr(info, body)
                    manifest["files"].append({"path": name, "bytes": len(body),
                        "sha256": hashlib.sha256(body).hexdigest(), "executable": executable})
                    manifest["total_bytes"] += len(body)
                    observed.append((name, after))
            validate_manifest(manifest)
            for name, expected in observed:
                if _state(tools._inspect(workspace._path(name), False)) != expected:
                    raise ArchiveError("File changed before snapshot completion.")
            return buffer.getvalue(), manifest
    except ArchiveError:
        raise
    except tools._Denied as exc:
        message = {"unsafe_path": "Snapshot source contains a link or special file.",
                   "protected_path": "Snapshot source path is protected.",
                   "conflict": "Snapshot source changed during capture.",
                   "busy": "Workspace snapshot lock is busy."}.get(
                       exc.code, "Snapshot source path is unavailable or unsafe.")
        raise ArchiveError(message) from None
    except (OSError, ValueError, TypeError, OverflowError):
        # Host errors may contain absolute paths or sensitive source details.
        raise ArchiveError("Snapshot capture failed while accessing files or building ZIP.") from None
