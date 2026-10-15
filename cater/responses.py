"""Decode model JSON without mistaking presentation fences for task content."""

from __future__ import annotations

import json
import re


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("模型回应包含重复字段。")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("模型回应包含非 JSON 数值。")


def load_response_object(text: str) -> dict:
    if not isinstance(text, str):
        raise ValueError("模型回应不是文本。")
    candidate = text.strip()
    if candidate.startswith("\ufeff"):
        candidate = candidate[1:].lstrip()
    if candidate.startswith("```"):
        match = re.fullmatch(r"```(?:json)?[ \t]*\r?\n([\s\S]*)\r?\n```", candidate, re.IGNORECASE)
        if match is None:
            raise ValueError("模型回应的 JSON 代码围栏不完整。")
        candidate = match[1]
    elif candidate.endswith("\n```"):
        # Some compatible providers omit the opening fence. Only an otherwise
        # complete JSON object is accepted; prose and partial JSON still fail.
        candidate = candidate[:-4].rstrip()
    try:
        value = json.loads(candidate, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
    except RecursionError as exc:
        raise ValueError("模型回应嵌套过深。") from exc
    if not isinstance(value, dict):
        raise ValueError("模型回应必须是单个 JSON 对象。")
    return value
