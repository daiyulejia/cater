"""Bounded, DB-only reads of host v1 ZIP members; never extract or execute.

The caller owns artifact authorization. A parent-agent receipt must distinguish
the verified whole-ZIP digest and all-entry metadata from the selected members'
body/CRC/SHA checks. Unselected bodies are not read or verified. These checks do
not establish that project contents are safe to interpret or execute.
"""

from __future__ import annotations

import hashlib
import io
import stat
import struct
import zipfile

from .artifact_blobs import read_blob
from .workspace_archive import ArchiveError, portable_path, validate_manifest


def _check_directory(archive, blob, files):
    infos = archive.infolist()
    if len(infos) != len(files):
        raise ArchiveError("Archive metadata mismatch.")
    seen, spans = set(), []
    for info, entry in zip(infos, files):
        name = portable_path(info.orig_filename)
        mode = stat.S_IFREG | (0o755 if entry["executable"] else 0o644)
        # Only the host's regular Unix files and stored/deflated encoding.
        allowed_flags = 0x800 | (6 if info.compress_type == zipfile.ZIP_DEFLATED else 0)
        if (name != info.filename or name != entry["path"] or name in seen
                or info.file_size != entry["bytes"] or info.is_dir()
                or info.create_system != 3 or info.external_attr != mode << 16
                or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                or info.flag_bits & ~allowed_flags
                or info.compress_size < 0
                or (info.compress_type == zipfile.ZIP_STORED
                    and info.compress_size != info.file_size)):
            raise ArchiveError("Archive metadata mismatch.")
        seen.add(name)
        # Check local headers for *all* entries without opening their bodies.
        # Host v1 uses seekable ZIPs, so data descriptors/ZIP64 are unnecessary.
        start = info.header_offset
        if start < 0 or start + 30 > archive.start_dir:
            raise ArchiveError("Archive header mismatch.")
        header = struct.unpack_from("<4s5H3I2H", blob, start)
        signature, version, flags, method, time, date, crc, packed, size, nlen, xlen = header
        local_time = ((date >> 9) + 1980, (date >> 5) & 15, date & 31,
                      time >> 11, (time >> 5) & 63, (time & 31) * 2)
        data_start = start + 30 + nlen + xlen
        end = data_start + info.compress_size
        encoded = name.encode("utf-8" if flags & 0x800 else "cp437")
        if (signature != b"PK\x03\x04" or version != info.extract_version
                or flags != info.flag_bits or method != info.compress_type
                or local_time != info.date_time
                or crc != info.CRC or packed != info.compress_size or size != info.file_size
                or blob[start + 30:start + 30 + nlen] != encoded
                or data_start > archive.start_dir or end > archive.start_dir):
            raise ArchiveError("Archive header mismatch.")
        spans.append((start, end))
    spans.sort()
    if any(left[1] > right[0] for left, right in zip(spans, spans[1:])):
        raise ArchiveError("Archive entries overlap.")
    return infos


def read_members(log, artifact, paths=None, *, max_files=128,
                 max_member_bytes=16 * 1024 * 1024,
                 max_total_bytes=32 * 1024 * 1024) -> list[dict]:
    """Return selected opaque bytes in manifest order, within caller budgets.

    Budgets apply to selected uncompressed members and are checked before the
    DB blob read. The full manifest retains its own v1 limits. ZIP metadata is
    checked for every entry, but only selected member bodies are decompressed.
    """
    try:
        if any(type(limit) is not int or limit <= 0
               for limit in (max_files, max_member_bytes, max_total_bytes)):
            raise ArchiveError("Invalid archive read budget.")
        if not isinstance(artifact, dict) or artifact.get("format") != "archive":
            raise ArchiveError("An archive artifact is required.")
        manifest = artifact.get("archive")
        validate_manifest(manifest)
        files = manifest["files"]
        if paths is None:
            selected = {entry["path"] for entry in files}
        else:
            if not isinstance(paths, list) or not paths:
                raise ArchiveError("Invalid archive member selection.")
            selected = {portable_path(path) for path in paths}
            if len(selected) != len(paths) or not selected <= {e["path"] for e in files}:
                raise ArchiveError("Invalid archive member selection.")
        entries = [entry for entry in files if entry["path"] in selected]
        if (len(entries) > max_files
                or any(entry["bytes"] > max_member_bytes for entry in entries)
                or sum(entry["bytes"] for entry in entries) > max_total_bytes):
            raise ArchiveError("Archive selection exceeds read budget.")
        blob = read_blob(log, artifact.get("content_sha256"), artifact.get("content_bytes"))
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            infos = _check_directory(archive, blob, files)
            result = []
            for info, entry in zip(infos, files):
                if entry["path"] not in selected:
                    continue
                with archive.open(info) as stream:
                    body = stream.read(entry["bytes"] + 1)
                if (len(body) != entry["bytes"]
                        or hashlib.sha256(body).hexdigest() != entry["sha256"].lower()):
                    raise ArchiveError("Archive member integrity check failed.")
                result.append({**entry, "body": body})
            return result
    except Exception:
        # ZIP/codec/DB exceptions may embed filenames or content. Never echo them.
        raise ArchiveError("Archive member read failed.") from None
