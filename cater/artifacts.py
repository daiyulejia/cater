"""Bounded inline deliverables; resource references never read local files."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Any

MAX_CONTENT_BYTES = 65_536
MAX_DELIVERABLES = 8
FORMATS = {"markdown", "text", "code"}


def content_metrics(content: str) -> dict[str, Any]:
    """Count raw source, not rendered Markdown, tokens or inferred requirements."""
    return {
        "basis": "raw_content_including_markdown",
        "utf8_bytes": len(content.encode("utf-8")),
        "codepoints": len(content),
        "non_whitespace_codepoints": sum(not char.isspace() for char in content),
        "cjk_ideographs": sum(unicodedata.name(char, "").startswith(
            ("CJK UNIFIED IDEOGRAPH-", "CJK COMPATIBILITY IDEOGRAPH-")) for char in content),
    }


def check_measurements(value: object, artifacts: dict[int, dict[str, Any]], source: str) -> list[dict[str, Any]]:
    """Evaluate declared source-bound comparisons, never infer limits from prose."""
    if not isinstance(value, list) or len(value) > 32:
        raise ValueError("量化检查必须是有界列表。")
    metrics = {"utf8_bytes", "codepoints", "non_whitespace_codepoints", "cjk_ideographs"}
    results = []
    for row in value:
        if not isinstance(row, dict) or set(row) != {"artifact_seq", "metric", "operator", "limit", "source_quote"}:
            raise ValueError("量化检查字段无效，观测值必须由宿主计算。")
        seq, metric, operator, limit, quote = (row[key] for key in ("artifact_seq", "metric", "operator", "limit", "source_quote"))
        if (type(seq) is not int or seq not in artifacts or not isinstance(metric, str) or metric not in metrics
                or not isinstance(operator, str) or operator not in {"le", "ge", "eq"}
                or type(limit) is not int or not 0 <= limit <= 1_000_000_000
                or not isinstance(quote, str) or not quote.strip() or len(quote) > 2000 or quote not in source):
            raise ValueError("量化检查必须绑定当前正文、支持的计量口径和本条要求原文。")
        observed = content_metrics(artifacts[seq]["content"])[metric]
        passed = {"le": observed <= limit, "ge": observed >= limit, "eq": observed == limit}[operator]
        results.append({**row, "observed": observed, "status": "passed" if passed else "failed",
                        "basis": "raw_content_including_markdown", "source": "host_computed"})
    return results


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
