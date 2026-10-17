"""Materialize trusted work submissions into a host-owned, immutable file store.

Call materialize(db_path.parent / "artifacts", task_id, submission,
                 work.artifacts(task_id, submission)).
The caller must obtain submission and artifacts from that task's event log:
WorkLedger.artifacts omits event task_id/actor, so these dictionaries alone cannot
authenticate their database origin. Digests are integrity checks, not signatures.

Only flat, portable filenames are accepted. No code is executed. Versions are
never repaired, overwritten or deleted, including abandoned staging directories.
Advisory OS locks serialize cooperating publishers; lstat, reparse-point checks
and no-follow opens reject observed links. This is NOT an OS sandbox or a defense
against a hostile process racing replacements of ancestor directories. The host
must protect the dedicated root and its ancestors from untrusted writers.
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import unicodedata
import uuid
from typing import Any, Iterator

from .artifacts import MAX_CONTENT_BYTES
from .work import digest


class ArtifactStoreError(ValueError):
    """Invalid input, unsafe filesystem entry, or a conflicting/tampered version."""


def _name(value: object) -> str:
    if not isinstance(value, str):
        raise ArtifactStoreError("filename or task_id must be a string")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError as exc:
        raise ArtifactStoreError("invalid filename encoding") from exc
    normalized = unicodedata.normalize("NFKC", value)
    for name in (value, normalized):
        base = name.split(".")[0].rstrip(" ").upper()
        if (not name or name != name.strip() or name.endswith((".", " "))
                or name in {".", ".."} or re.search(r'[\\/:*?"<>|]', name)
                or any(unicodedata.category(char).startswith("C") for char in name)
                or base in {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
                or re.fullmatch(r"(?:COM|LPT)[1-9]", base)):
            raise ArtifactStoreError(f"unsafe filename or task_id: {value!r}")
    if size > 180:
        raise ArtifactStoreError("filename or task_id exceeds 180 UTF-8 bytes")
    return value


def _positive(value: object) -> bool:
    return type(value) is int and value > 0


def _prepare(task_id: str, submission: dict[str, Any], artifacts: list[dict[str, Any]]) -> tuple[dict, dict[str, bytes]]:
    _name(task_id)
    if not isinstance(submission, dict) or not isinstance(artifacts, list):
        raise ArtifactStoreError("submission must be a dictionary and artifacts a list")
    if submission.get("task_id", task_id) != task_id:
        raise ArtifactStoreError("submission belongs to another task")
    payload = {key: value for key, value in submission.items() if key not in {"seq", "digest", "task_id"}}
    required = {"plan_seq", "work_item_id", "attempt_id", "artifact_seqs", "hashes", "inputs", "scope"}
    if not required <= payload.keys() or payload.keys() - required - {"reused_from"}:
        raise ArtifactStoreError("invalid work submission fields")
    if (not _positive(payload["plan_seq"]) or not isinstance(payload["inputs"], dict)
            or not isinstance(payload["scope"], dict)
            or any(not isinstance(payload[key], str) or not payload[key] for key in ("work_item_id", "attempt_id"))):
        raise ArtifactStoreError("invalid submission ownership")
    try:
        expected_digest = digest(payload)
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ArtifactStoreError("invalid submission encoding") from exc
    if submission.get("digest") != expected_digest:
        raise ArtifactStoreError("submission digest mismatch")
    seqs, hashes = payload["artifact_seqs"], payload["hashes"]
    if (not isinstance(seqs, list) or not seqs or not all(_positive(seq) for seq in seqs)
            or len(set(seqs)) != len(seqs) or not isinstance(hashes, dict)
            or set(hashes) != {str(seq) for seq in seqs}):
        raise ArtifactStoreError("submission requires distinct artifact seqs and exact hashes")
    submitted_seq = submission.get("seq")
    if submitted_seq is not None and (not _positive(submitted_seq) or submitted_seq <= max(seqs)):
        raise ArtifactStoreError("invalid submission event sequence")
    reused = payload.get("reused_from")
    if "reused_from" in payload and (not _positive(reused) or reused <= max(seqs)
                                     or (submitted_seq is not None and reused >= submitted_seq)):
        raise ArtifactStoreError("invalid reuse provenance")
    by_seq: dict[int, dict] = {}
    for artifact in artifacts:
        if not isinstance(artifact, dict) or not _positive(artifact.get("seq")) or artifact["seq"] in by_seq:
            raise ArtifactStoreError("invalid or duplicate artifact sequence")
        by_seq[artifact["seq"]] = artifact
    if set(by_seq) != set(seqs):
        raise ArtifactStoreError("artifacts do not exactly match submission membership")
    entries, bodies, names = [], {}, set()
    for seq in sorted(seqs):
        artifact = by_seq[seq]
        if (artifact.get("task_id", task_id) != task_id or artifact.get("work_item_id") != payload["work_item_id"]
                or not isinstance(artifact.get("attempt_id"), str) or not artifact["attempt_id"]
                or (reused is None and artifact["attempt_id"] != payload["attempt_id"])):
            raise ArtifactStoreError(f"artifact {seq} does not belong to this work submission")
        # A trusted reused submission explicitly binds prior seqs/hashes, not the new attempt_id.
        content = artifact.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ArtifactStoreError(f"artifact {seq} requires inline content")
        try:
            body = content.encode("utf-8")
        except UnicodeError as exc:
            raise ArtifactStoreError(f"artifact {seq} has invalid UTF-8 content") from exc
        sha = hashlib.sha256(body).hexdigest()
        if len(body) > MAX_CONTENT_BYTES or sha != artifact.get("content_sha256") or sha != hashes[str(seq)]:
            raise ArtifactStoreError(f"artifact {seq} content hash or size mismatch")
        if "content_bytes" in artifact and (type(artifact["content_bytes"]) is not int or artifact["content_bytes"] != len(body)):
            raise ArtifactStoreError(f"artifact {seq} content_bytes mismatch")
        extension = "md" if artifact.get("format") == "markdown" else "txt"
        filename = _name(artifact.get("filename", f"artifact-{seq}.{extension}"))
        name_key = unicodedata.normalize("NFKC", unicodedata.normalize("NFKC", filename).casefold())
        if name_key in names:
            raise ArtifactStoreError("case or Unicode filename collision")
        names.add(name_key)
        path = f"files/{filename}"
        bodies[path] = body
        entries.append({"path": path, "seq": seq, "content_sha256": sha, "content_bytes": len(body)})
    version = f"task-{hashlib.sha256(task_id.encode('utf-8')).hexdigest()}/{expected_digest}"
    return {"version": 1, "task_id": task_id, "submission_digest": expected_digest,
            "directory": version, "files": entries}, bodies


def _inspect(path: Path, directory: bool) -> os.stat_result:
    info = path.lstat()
    if (stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
        raise ArtifactStoreError(f"symlink or reparse point is forbidden: {path}")
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise ArtifactStoreError(f"unexpected filesystem entry type: {path}")
    if not directory and info.st_nlink != 1:
        raise ArtifactStoreError(f"hard-linked files are forbidden: {path}")
    return info


def _directories(path: Path, *, create: bool = False) -> None:
    for part in (*reversed(path.parents), path):
        try:
            _inspect(part, True)
        except FileNotFoundError:
            if not create:
                raise
            try:
                part.mkdir()
            except FileExistsError:
                pass
            _inspect(part, True)


def _open(path: Path, flags: int, *, create: bool = False) -> int:
    _directories(path.parent)
    before = None
    if not create:
        before = _inspect(path, False)
    fd = os.open(path, flags | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0), 0o600)
    try:
        after = _inspect(path, False)
        opened = os.fstat(fd)
        if not os.path.samestat(after, opened) or (before is not None and not os.path.samestat(before, opened)):
            raise ArtifactStoreError(f"filesystem entry changed while opening: {path}")
        return fd
    except BaseException:
        os.close(fd)
        raise


@contextmanager
def _lock(path: Path) -> Iterator[None]:
    try:
        fd = _open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, create=True)
    except FileExistsError:
        fd = _open(path, os.O_RDWR)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _write(path: Path, content: bytes) -> None:
    with os.fdopen(_open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, create=True), "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _read(path: Path, limit: int) -> bytes:
    with os.fdopen(_open(path, os.O_RDONLY), "rb") as stream:
        return stream.read(limit + 1)


def _manifest_bytes(manifest: dict) -> bytes:
    return (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _verify(directory: Path, manifest: dict, bodies: dict[str, bytes]) -> None:
    _directories(directory)
    if {entry.name for entry in directory.iterdir()} != {"manifest.json", "files"}:
        raise ArtifactStoreError(f"incomplete or conflicting version directory: {directory}")
    _inspect(directory / "files", True)
    if {entry.name for entry in (directory / "files").iterdir()} != {Path(name).name for name in bodies}:
        raise ArtifactStoreError(f"missing or unexpected artifact files: {directory}")
    expected = _manifest_bytes(manifest)
    if _read(directory / "manifest.json", len(expected)) != expected:
        raise ArtifactStoreError(f"manifest conflict or tampering: {directory}")
    for entry in manifest["files"]:
        body = _read(directory / entry["path"], entry["content_bytes"])
        if len(body) != entry["content_bytes"] or hashlib.sha256(body).hexdigest() != entry["content_sha256"]:
            raise ArtifactStoreError(f"artifact content tampering: {entry['path']}")


def materialize_team_artifact(root: str | os.PathLike[str], task_id: str, artifact: dict, *, body: bytes | None = None) -> str:
    """Save a ledger-derived team artifact without fabricating a legacy work submission."""
    _name(task_id)
    filename = _name(artifact["filename"])
    if artifact.get("format") == "archive":
        from .workspace_archive import MAX_ARCHIVE_BYTES, validate_manifest

        validate_manifest(artifact.get("archive"))
        if type(body) is not bytes or len(body) != artifact.get("content_bytes"):
            raise ArtifactStoreError("invalid archive body")
        content, limit = body, MAX_ARCHIVE_BYTES
    else:
        content, limit = artifact["content"].encode("utf-8"), MAX_CONTENT_BYTES
    sha = hashlib.sha256(content).hexdigest()
    if (not _positive(artifact.get("seq")) or sha != artifact.get("content_sha256")
            or len(content) > limit):
        raise ArtifactStoreError("invalid team artifact")
    base = Path(root).absolute()
    _directories(base, create=True)
    directory = base / ("team-" + hashlib.sha256(task_id.encode()).hexdigest())
    _directories(directory, create=True)
    with _lock(directory / ".publish.lock"):
        version = directory / (str(artifact["seq"]) + "-" + sha)
        _directories(version, create=True)
        path = version / filename
        if path.exists():
            if _read(path, limit) != content:
                raise ArtifactStoreError("saved team artifact was modified")
        else:
            _write(path, content)
        return str(path)


def materialize(root: str | os.PathLike[str], task_id: str, submission: dict[str, Any],
                artifacts: list[dict[str, Any]]) -> dict[str, Any]:
    """Return a deterministic manifest after verifying a complete local version.

    root is a dedicated, host-selected store (typically db_path.parent/artifacts),
    never a model-supplied project directory. manifest['directory'] is relative to
    root; each files[].path is relative to that version. Missing filenames receive
    artifact-<seq>.txt (or .md). Repeats verify exact manifests and file hashes.
    Invalid input/conflicts raise ArtifactStoreError; OS failures are wrapped in
    it with their original cause. KeyboardInterrupt/SystemExit propagate, leaving
    an unpublished .staging-* directory. No automatic deletion is performed.
    File fsync plus same-parent rename gives atomic visibility, not a portable
    guarantee of directory durability through power loss on all filesystems.
    """
    manifest, bodies = _prepare(task_id, submission, artifacts)
    base = Path(root)
    if ".." in base.parts:
        raise ArtifactStoreError("root must not contain parent traversal")
    base = Path(os.path.abspath(base))  # Do not resolve away links before checking them.
    target = base / manifest["directory"]
    try:
        _directories(target.parent, create=True)
        locks = target.parent / ".locks"
        _directories(locks, create=True)
        with _lock(locks / f"{manifest['submission_digest']}.lock"):
            _directories(target.parent)
            if os.path.lexists(target):
                _verify(target, manifest, bodies)
                return manifest
            staging = target.parent / f".staging-{uuid.uuid4().hex}"
            staging.mkdir(mode=0o700)
            (staging / "files").mkdir(mode=0o700)
            for name, body in bodies.items():
                _write(staging / name, body)
            _write(staging / "manifest.json", _manifest_bytes(manifest))
            _verify(staging, manifest, bodies)
            # Cooperating processes hold the same OS lock; never replace an observed entry.
            if os.path.lexists(target):
                _verify(target, manifest, bodies)
            else:
                staging.rename(target)
            _verify(target, manifest, bodies)
            return manifest
    except OSError as exc:
        raise ArtifactStoreError(f"artifact store filesystem failure: {exc}") from exc
