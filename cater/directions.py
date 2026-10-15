"""Conservative chat control recognition, not a semantic intent classifier."""

from __future__ import annotations

import re
from typing import Any, Iterable


# Compose complete usage questions from bounded phrases; never accept an arbitrary
# tail after "how", since it may contain another requirement or an execution request.
_USAGE_OBJECT = (
    r"(?:(?:这个|这份|这款|该|那个|刚才的|你们的|你们做的|生成的|交付的)?"
    r"(?:五子棋(?:小游戏|游戏)?|小游戏|游戏|网页|页面|网站|项目|程序|应用|代码|文件|成果|产物|结果|demo))"
    r"|这个|那个|它|这些|[a-z0-9_-]+\.(?:html?|py|js|exe|md|zip)"
)
_USAGE_OBJECT = rf"(?:{_USAGE_OBJECT})"
_USAGE_ACTION = r"(?:使用|打开|运行|启动|访问|试玩|游玩|用|玩)"
_USAGE_PLACE = r"(?:在本地|本地|在浏览器(?:里|中)?|在电脑上|在手机上|直接)"
_USAGE_QUESTION = re.compile(
    rf"(?:请问(?:一下)?|想问一下|我想问一下|我想知道|请告诉我|能告诉我|可以告诉我|能不能告诉我|麻烦告诉我)?"
    rf"(?:我(?:们)?(?:应该|该|要)?|应该|该|要)?"
    rf"(?:"
    rf"(?:{_USAGE_OBJECT}(?:要|应该|该)?)?(?:{_USAGE_PLACE})?(?:怎么|怎样|如何)"
    rf"(?:才能|才可以|可以|能)?(?:{_USAGE_PLACE})?{_USAGE_ACTION}(?:起来)?(?:{_USAGE_OBJECT})?"
    rf"|(?:{_USAGE_OBJECT})?(?:在(?:哪里|哪儿)|哪里|哪儿){_USAGE_ACTION}(?:{_USAGE_OBJECT})?"
    rf"|(?:{_USAGE_OBJECT}的)?(?:使用|打开|运行|启动)(?:方法|方式|步骤)(?:是什么|有哪些)"
    rf")(?:呢|啊|呀|吗)?"
)


def _usage_question(text: str) -> bool:
    # Commas and newlines are boundaries too: every clause must be read-only.
    text = re.sub(r"^(?:请问(?:一下)?|想问一下|我想问一下)[，,]\s*", "", text)
    clauses = re.split(r"[。！？!?，,；;\r\n]+", text)
    return all(clause.strip() and _USAGE_QUESTION.fullmatch(
        re.sub(r"[ \t]+", "", clause)) for clause in clauses)


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
    if _usage_question(text):
        return "question"
    explanation = re.match(r"^(?:请讲解|讲解|提问)\s*[:：]", text)
    if explanation:
        body = text[explanation.end():].strip()
        # An explicit explanation prefix must not hide changes added to a usage question.
        if any(_usage_question(clause.strip())
               for clause in re.split(r"[。！？!?，,；;\r\n]+|并且|并|然后|顺便|同时|另外", body)):
            return "question" if _usage_question(body) else "direction"
        return "question"
    if text in {"为什么这么改", "为什么这样改", "为什么这样做", "为什么改这里", "怎么验证", "如何验证", "如何复查", "我没看懂", "解释一下", "请解释一下"}:
        return "question"
    # Mixed sentences, unknown phrases, and historical events remain changes.
    return "direction"


def changes_direction(payload: dict[str, Any]) -> bool:
    return ("goal" in payload or "acceptance" in payload
            or payload.get("intent") not in {"status", "resume"})
