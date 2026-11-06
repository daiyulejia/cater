"""Token telemetry must distinguish reported, estimated and unavailable usage."""

from typing import Any


def valid_token_count(value: Any) -> bool:
    return type(value) is int and value >= 0


OPENAI_USAGE_CONFLICT = "total_below_reported_components"


def openai_usage_conflict(value: Any) -> bool:
    """Only compare independently valid standard fields, never cache details."""
    if not isinstance(value, dict) or not valid_token_count(value.get("total_tokens")):
        return False
    parts = [value[k] for k in ("prompt_tokens", "completion_tokens")
             if valid_token_count(value.get(k))]
    return bool(parts) and value["total_tokens"] < sum(parts)


def usage_conflict_diagnostics(meta: Any) -> dict:
    """Allowlist and recheck evidence before adding it to an existing receipt."""
    if not isinstance(meta, dict) or meta.get("mode") != "openai_compatible":
        return {}
    if meta.get("token_usage") != "unavailable" or meta.get("usage_issue") != OPENAI_USAGE_CONFLICT:
        return {}
    usage = meta.get("usage")
    if not openai_usage_conflict(usage):
        return {}
    return {"usage_issue": OPENAI_USAGE_CONFLICT,
            "usage": {k: usage[k] for k in ("prompt_tokens", "completion_tokens", "total_tokens")
                      if valid_token_count(usage.get(k))}}


def usage_source(tokens: Any, source: Any) -> str:
    if valid_token_count(tokens) and isinstance(source, str) and source in {"reported", "estimated"}:
        return source
    return "unavailable"
