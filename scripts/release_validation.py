"""Read-only validation of the deliberately narrow Cater pure-Python wheel format."""

from __future__ import annotations

import base64
import configparser
import csv
import hashlib
import io
import re
import stat
import unicodedata
import zipfile
import zlib
from email import policy
from email.parser import BytesParser
from pathlib import Path


MAX_FILES = 2048
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_FILE_BYTES = 16 * 1024 * 1024
_CHUNK_BYTES = 64 * 1024
_VERSION = re.compile(
    r"[0-9]+(?:\.[0-9]+)*(?:(?:a|b|rc)[0-9]+)?"
    r"(?:\.post[0-9]+)?(?:\.dev[0-9]+)?(?:\+[a-z0-9]+(?:\.[a-z0-9]+)*)?",
    re.ASCII,
)
_RESERVED = re.compile(r"(?:con|prn|aux|nul|com[0-9]|lpt[0-9])(?:\..*)?", re.I)


class ReleaseValidationError(ValueError):
    """The wheel does not match the expected Cater release."""


def _safe_path(path: str, *, directory: bool = False) -> list[str]:
    # Check both spellings so compatibility characters cannot hide separators,
    # device names, traversal, or Windows extraction aliases.
    if directory and path.endswith("/"):
        path = path[:-1]
    for spelling in (path, unicodedata.normalize("NFKC", path)):
        parts = spelling.split("/")
        if (
            not spelling
            or any(part in ("", ".", "..") for part in parts)
            or any(char in spelling for char in '\\:<>"|?*')
            or any(unicodedata.category(char).startswith("C") for char in spelling)
            or any(part.endswith((".", " ")) or _RESERVED.fullmatch(part) for part in parts)
        ):
            raise ReleaseValidationError("Unsafe wheel member path")
    return path.split("/")


def _path_alias(path: str) -> str:
    return unicodedata.normalize("NFKC", unicodedata.normalize("NFKC", path).casefold())


def _headers(data: bytes, label: str):
    message = BytesParser(policy=policy.default).parsebytes(data, headersonly=True)
    if message.defects or any(value.defects for _, value in message.items()):
        raise ReleaseValidationError(f"Malformed {label} headers")
    return message


def _single_header(message, field: str, expected: str, label: str) -> None:
    values = message.get_all(field, [])
    if len(values) != 1 or str(values[0]) != expected:
        raise ReleaseValidationError(f"{label} must contain one matching {field}")


def _validate_metadata(contents: dict[str, bytes], prefix: str, name: str,
                       version: str, license_bytes: bytes) -> None:
    metadata = _headers(contents[prefix + "METADATA"], "METADATA")
    for field, value in (
        ("Name", name), ("Version", version), ("Requires-Python", ">=3.11"),
        ("License-Expression", "MIT"), ("License-File", "LICENSE"),
    ):
        _single_header(metadata, field, value, "METADATA")
    if metadata.get_all("Requires-Dist", []):
        raise ReleaseValidationError("METADATA must not declare Requires-Dist")
    if contents[prefix + "licenses/LICENSE"] != license_bytes:
        raise ReleaseValidationError("License content differs from expected LICENSE")

    wheel = _headers(contents[prefix + "WHEEL"], "WHEEL")
    for field, value in (
        ("Wheel-Version", "1.0"), ("Root-Is-Purelib", "true"), ("Tag", "py3-none-any"),
    ):
        _single_header(wheel, field, value, "WHEEL")

    entries = configparser.ConfigParser(
        interpolation=None, strict=True, delimiters=("=",),
        empty_lines_in_values=False, default_section="\0",
    )
    entries.optionxform = str
    entries.read_string(contents[prefix + "entry_points.txt"].decode("utf-8"))
    if (
        entries.defaults()
        or entries.sections() != ["console_scripts"]
        or dict(entries["console_scripts"]) != {"cater": "cater.cli:main"}
    ):
        raise ReleaseValidationError("entry_points.txt must contain only cater = cater.cli:main")
    top_level = contents.get(prefix + "top_level.txt")
    if top_level is not None and top_level.decode("utf-8").strip() != "cater":
        raise ReleaseValidationError("top_level.txt must name only cater")


def _validate_record(data: bytes, record_path: str,
                     hashes: dict[str, str], sizes: dict[str, int]) -> None:
    seen: set[str] = set()
    reader = csv.reader(io.StringIO(data.decode("utf-8"), newline=""), strict=True)
    for row in reader:
        if len(row) != 3:
            raise ReleaseValidationError("RECORD rows must have exactly three columns")
        path, digest, size = row
        if path in seen:
            raise ReleaseValidationError("RECORD contains a duplicate path")
        if path not in hashes:
            raise ReleaseValidationError("RECORD names a file absent from the wheel")
        seen.add(path)
        if path == record_path:
            if digest or size:
                raise ReleaseValidationError("RECORD must leave its own hash and size empty")
            continue
        encoded = base64.urlsafe_b64encode(bytes.fromhex(hashes[path])).rstrip(b"=").decode("ascii")
        if digest != "sha256=" + encoded:
            raise ReleaseValidationError("RECORD sha256 differs from actual content")
        if not re.fullmatch(r"[0-9]+", size, re.ASCII) or size != str(sizes[path]):
            raise ReleaseValidationError("RECORD size differs from actual content")
    if seen != set(hashes):
        raise ReleaseValidationError("RECORD does not cover every wheel file")


def _read_member(archive: zipfile.ZipFile, member: zipfile.ZipInfo) -> bytes:
    remaining = min(member.file_size, MAX_FILE_BYTES)
    chunks = []
    with archive.open(member, "r") as source:
        while True:
            chunk = source.read(min(_CHUNK_BYTES, remaining + 1))
            if not chunk:
                break
            if len(chunk) > remaining:
                raise ReleaseValidationError("Wheel member exceeds declared or allowed size")
            remaining -= len(chunk)
            chunks.append(chunk)
    result = b"".join(chunks)
    if len(result) != member.file_size:
        raise ReleaseValidationError("Wheel member size differs from its ZIP declaration")
    return result


def validate_wheel(wheel: Path, expected: dict[str, bytes], *, name: str,
                   version: str, license_bytes: bytes) -> dict:
    """Validate bytes without extracting/importing; return only independently measured facts.

    Limits apply to uncompressed files, including metadata. Explicit directories
    are accepted only as ancestors of allowed files (at most 2048 directories).
    Versions use canonical numeric releases with optional a/b/rc, post, dev, local.
    """
    if name != "cater":
        raise ReleaseValidationError("This validator supports only the cater distribution")
    if not isinstance(version, str) or not _VERSION.fullmatch(version):
        raise ReleaseValidationError("Expected a safe static Cater version")
    if not isinstance(license_bytes, bytes):
        raise ReleaseValidationError("Expected LICENSE bytes")
    if not expected:
        raise ReleaseValidationError("Expected Cater package files must not be empty")
    for path, data in expected.items():
        if not isinstance(path, str) or not isinstance(data, bytes):
            raise ReleaseValidationError("Expected package paths must map to bytes")
        parts = _safe_path(path)
        if len(parts) < 2 or parts[0] != "cater":
            raise ReleaseValidationError("Expected package paths must be under cater/")
        if (any(_path_alias(part) == "__pycache__" for part in parts)
                or _path_alias(path).endswith((".pyc", ".pyo"))):
            raise ReleaseValidationError("Expected package files must not contain bytecode")
        if len(data) > MAX_FILE_BYTES:
            raise ReleaseValidationError("Expected package file exceeds size limit")
    if len(expected) > MAX_FILES or sum(map(len, expected.values())) > MAX_TOTAL_BYTES:
        raise ReleaseValidationError("Expected package exceeds wheel limits")

    prefix = f"cater-{version}.dist-info/"
    required = {prefix + suffix for suffix in (
        "METADATA", "WHEEL", "RECORD", "entry_points.txt", "licenses/LICENSE",
    )}
    allowed = set(expected) | required | {prefix + "top_level.txt"}
    allowed_directories = {
        path[:index] for path in allowed for index, char in enumerate(path) if char == "/"
    }
    hashes: dict[str, str] = {}
    sizes: dict[str, int] = {}
    metadata_contents: dict[str, bytes] = {}
    total_bytes = 0
    try:
        # A single handle keeps the reported archive hash tied to the validated file.
        with Path(wheel).open("rb") as source:
            with zipfile.ZipFile(source, "r") as archive:
                members = archive.infolist()
                if len(members) > 2 * MAX_FILES:
                    raise ReleaseValidationError("Wheel contains too many members")
                seen: set[str] = set()
                aliases: dict[str, str] = {}
                directories: set[str] = set()
                regular: list[zipfile.ZipInfo] = []
                declared_bytes = 0
                for member in members:
                    directory = member.is_dir()
                    path = member.filename[:-1] if directory else member.filename
                    if member.orig_filename != member.filename:
                        raise ReleaseValidationError("Unsafe truncated wheel member path")
                    parts = _safe_path(member.filename, directory=directory)
                    if path in seen:
                        raise ReleaseValidationError("Wheel contains a duplicate path")
                    seen.add(path)
                    for index in range(1, len(parts) + 1):
                        ancestor = "/".join(parts[:index])
                        alias = _path_alias(ancestor)
                        if alias in aliases and aliases[alias] != ancestor:
                            raise ReleaseValidationError("Wheel paths have case or Unicode alias conflicts")
                        aliases[alias] = ancestor
                    mode = member.external_attr >> 16
                    kind = stat.S_IFMT(mode)
                    if kind not in (0, stat.S_IFDIR if directory else stat.S_IFREG):
                        raise ReleaseValidationError("Wheel contains a link or special file")
                    if member.external_attr & 0x400 or (member.external_attr & 0x10 and not directory):
                        raise ReleaseValidationError("Wheel contains a Windows reparse point or directory alias")
                    if member.flag_bits & 1:
                        raise ReleaseValidationError("Encrypted wheel members are unsupported")
                    if member.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                        raise ReleaseValidationError("Wheel compression must be stored or deflated")
                    if directory:
                        if member.file_size or path not in allowed_directories:
                            raise ReleaseValidationError("Wheel contains an unexpected directory")
                        directories.add(path)
                    else:
                        if path not in allowed:
                            raise ReleaseValidationError("Wheel contains an unexpected file")
                        if member.file_size > MAX_FILE_BYTES:
                            raise ReleaseValidationError("Wheel member exceeds size limit")
                        declared_bytes += member.file_size
                        if declared_bytes > MAX_TOTAL_BYTES:
                            raise ReleaseValidationError("Wheel exceeds total uncompressed size limit")
                        regular.append(member)
                    if len(regular) > MAX_FILES or len(directories) > MAX_FILES:
                        raise ReleaseValidationError("Wheel contains too many files or directories")
                actual_files = {member.filename for member in regular}
                if not (set(expected) | required) <= actual_files:
                    raise ReleaseValidationError("Wheel is missing expected package files or metadata")
                if actual_files & allowed_directories:
                    raise ReleaseValidationError("Wheel path is both file and directory")
                for member in members:
                    data = _read_member(archive, member)
                    if member.is_dir():
                        continue
                    path = member.filename
                    total_bytes += len(data)
                    if total_bytes > MAX_TOTAL_BYTES:
                        raise ReleaseValidationError("Wheel exceeds total uncompressed size limit")
                    hashes[path] = hashlib.sha256(data).hexdigest()
                    sizes[path] = len(data)
                    if path in expected:
                        if data != expected[path]:
                            raise ReleaseValidationError("Cater package content differs from expected source snapshot")
                    else:
                        metadata_contents[path] = data
                _validate_metadata(metadata_contents, prefix, name, version, license_bytes)
                _validate_record(metadata_contents[prefix + "RECORD"], prefix + "RECORD", hashes, sizes)
            source.seek(0)
            archive_hash = hashlib.file_digest(source, "sha256").hexdigest()
    except ReleaseValidationError:
        raise
    except (OSError, EOFError, ValueError, RuntimeError, NotImplementedError,
            zipfile.BadZipFile, configparser.Error, csv.Error, zlib.error) as error:
        # Parser exceptions may include file contents. Keep them out of diagnostics.
        raise ReleaseValidationError(f"Cannot validate wheel ({type(error).__name__})") from None
    return {
        "name": name,
        "version": version,
        "wheel_sha256": archive_hash,
        "package_files": len(expected),
        "total_bytes": total_bytes,
        "files": {path: hashes[path] for path in sorted(expected)},
    }
