"""Bounded inline deliverables; resource references never read local files."""

from __future__ import annotations

import hashlib
import re
from typing import Any

MAX_CONTENT_BYTES = 65_536
MAX_DELIVERABLES = 8
FORMATS = {"markdown", "text", "code"}


def safe_filename(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    name = value.strip()
    try:
        encoded = name.encode("utf-8")
    except UnicodeEncodeError:
        return None
    if not name or len(encoded) > 180:
        return None
    if re.search(r'[\x00-\x1f\\/:*?"<>|]', name) or name.endswith(".") or name in {".", ".."}:
        return None
    if re.fullmatch(r"CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]", name.split(".")[0], re.IGNORECASE):
        return None
    return name


def normalize_deliverables(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result = []
    for item in value[:MAX_DELIVERABLES]:
        if not isinstance(item, dict):
            continue
        title, summary = item.get("title"), item.get("summary")
        if not isinstance(title, str) or not title.strip() or not isinstance(summary, str) or not summary.strip():
            continue
        try:
            title.encode("utf-8")
            summary.encode("utf-8")
        except UnicodeEncodeError:
            continue
        payload: dict[str, Any] = {"title": title.strip()[:240], "summary": summary.strip()[:2000]}
        for key, limit in (("kind", 80), ("uri", 2000)):
            if isinstance(item.get(key), str) and item[key].strip():
                try:
                    item[key].encode("utf-8")
                except UnicodeEncodeError:
                    continue
                payload[key] = item[key].strip()[:limit]
        content = item.get("content")
        if content is not None:
            format = item.get("format") or "text"
            if not isinstance(content, str) or not content.strip():
                payload["content_error"] = "invalid_content"
            elif not isinstance(format, str) or format not in FORMATS:
                payload["content_error"] = "invalid_format"
            else:
                try:
                    encoded = content.encode("utf-8")
                except UnicodeEncodeError:
                    payload["content_error"] = "invalid_encoding"
                else:
                    if len(encoded) > MAX_CONTENT_BYTES:
                        payload["content_error"] = "too_large"
                    else:
                        payload.update(content=content, format=format, content_bytes=len(encoded), content_sha256=hashlib.sha256(encoded).hexdigest())
                        filename = safe_filename(item.get("filename"))
                        if filename:
                            payload["filename"] = filename
        result.append(payload)
    return result
