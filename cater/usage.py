"""Token telemetry must distinguish reported, estimated and unavailable usage."""

from typing import Any


def valid_token_count(value: Any) -> bool:
    return type(value) is int and value >= 0


def usage_source(tokens: Any, source: Any) -> str:
    if valid_token_count(tokens) and isinstance(source, str) and source in {"reported", "estimated"}:
        return source
    return "unavailable"
