"""Conservative chat control recognition, not a semantic intent classifier."""

from __future__ import annotations

import re
from typing import Any, Iterable


def command_intent(instruction: str, aliases: Iterable[str] = ()) -> str:
    text = instruction.strip()
    # Only a leading, known addressee is presentation, not a scheduling edit.
    for alias in sorted(set(aliases), key=len, reverse=True):
        match = re.match(rf"^@{re.escape(alias)}(?![\w-])[\s,，:：]*", text, re.IGNORECASE)
        if match:
            text = text[match.end():]
            break
    text = text.strip(" \t\r\n。.!！?？").casefold()
    if text in {
        "进度", "查看进度", "进度怎么样", "现在进度怎么样", "目前进度怎么样", "做到哪了",
        "现在做到哪了", "做完了吗", "现在做完了吗", "任务进度", "任务进度怎么样", "汇报进度",
        "请汇报进度", "status", "progress", "what is the status", "what's the status",
    }:
        return "status"
    if text in {"继续", "继续吧", "请继续", "继续执行", "继续推进", "继续任务", "接着做", "接着做吧", "continue", "resume"}:
        return "resume"
    # Mixed sentences, unknown phrases, and historical events remain changes.
    return "direction"


def changes_direction(payload: dict[str, Any]) -> bool:
    return ("goal" in payload or "acceptance" in payload
            or payload.get("intent") not in {"status", "resume"})
