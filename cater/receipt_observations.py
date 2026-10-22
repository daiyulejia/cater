"""Bounded output declarations, never execution, coverage or acceptance proof.

There is no stdlib parser API for plain TextTestRunner output. This small format
parser follows its terminal summary grammar, not command aliases or argv. Any
program can print that grammar; even a genuine runner's count does not identify
the tests or establish what they checked. Receipt authentication belongs to the
caller. No I/O, model calls, log copies or changes to the source result occur.
"""

from __future__ import annotations

import re


_MAX_STREAM_BYTES = 8192
_MAX_SUMMARY_CHARS = 256
_RUN = re.compile(r"Ran (0|[1-9][0-9]{0,8}) (test|tests) in (?:0|[1-9][0-9]{0,8})\.[0-9]{3}s")
_OUTCOME = re.compile(r"(OK|FAILED|NO TESTS RAN)(?: ?\(([^()\n]{1,180})\))?")
_COUNT = re.compile(r"(failures|errors|skipped|expected failures|unexpected successes)=([1-9][0-9]{0,8})")
_COUNT_ORDER = ("failures", "errors", "skipped", "expected failures", "unexpected successes")
_NOTE = ("Output declaration only; TextTestRunner format does not prove program identity, "
         "tests executed, coverage or acceptance. Command status, outcome and exit_code are independent.")


def _captured_text(stream: object) -> str | None:
    if (type(stream) is not dict or stream.get("complete") is not True
            or stream.get("truncated") is not False or stream.get("utf8_valid") is not True
            or "preview_mode" in stream or "tail_excerpt" in stream):
        return None
    text, size = stream.get("excerpt"), stream.get("bytes")
    if (type(text) is not str or len(text) > _MAX_STREAM_BYTES
            or type(size) is not int or not 0 <= size <= _MAX_STREAM_BYTES):
        return None
    try:
        if len(text.encode("utf-8")) != size:
            return None
    except UnicodeError:
        return None
    normalized = text.replace("\r\n", "\n")
    if any(ord(char) < 32 and char not in "\n\t" for char in normalized):
        return None
    return text


def _summary(text: str, stream_name: str) -> dict | None:
    normalized = text.replace("\r\n", "\n")
    lines = normalized.removesuffix("\n").split("\n")
    if len(lines) < 3 or lines[-2] != "":
        return None
    run, outcome = _RUN.fullmatch(lines[-3]), _OUTCOME.fullmatch(lines[-1])
    if run is None or outcome is None:
        return None
    tests = int(run[1])
    if run[2] != ("test" if tests == 1 else "tests"):
        return None
    counts = {}
    previous = -1
    if outcome[2] is not None:
        if outcome[2].startswith(" "):
            return None
        for part in outcome[2].split(","):
            count = _COUNT.fullmatch(part.removeprefix(" "))
            if count is None:
                return None
            position = _COUNT_ORDER.index(count[1])
            if position <= previous:
                return None
            previous = position
            counts[count[1].replace(" ", "_")] = int(count[2])
    failure = any(key in counts for key in ("failures", "errors", "unexpected_successes"))
    if (outcome[1] == "OK" and failure or outcome[1] == "FAILED" and not failure
            or outcome[1] == "NO TESTS RAN" and (tests != 0 or counts)):
        return None
    # Keep the exact summary (including CRLF), never the preceding test/body log.
    start = text.rfind("\nRan ") + 1
    literal = text[start:].removesuffix("\n").removesuffix("\r")
    if len(literal) > _MAX_SUMMARY_CHARS:
        return None
    return {"kind": "unittest_text_summary", "source_stream": stream_name,
            "literal_summary": literal, "reported_tests": tests,
            "reported_outcome": outcome[1], "reported_counts": counts,
            "program_report_note": _NOTE}


def receipt_observations(result: object) -> list[dict]:
    """Return zero or one output declaration from a complete command receipt.

    Require explicit outer and BOTH stream complete/nontruncated flags, valid
    UTF-8, saved full excerpts and matching byte counts (at most 8 KiB per stream).
    Unknown, ambiguous, exceptional or partial capture shapes fail closed. Only
    LF/CRLF terminal Ran/blank/outcome summaries are supported; counts keep their
    reported meaning, with spaces in count keys normalized to underscores.
    """
    if (type(result) is not dict or result.get("complete") is not True
            or result.get("truncated") is not False
            or result.get("timed_out", False) is not False
            or result.get("outcome") in ("timeout", "launch_error", "effects_unknown", "not_started")
            or any(result.get(key) is not None for key in ("exception", "launch_error", "cleanup_error"))):
        return []
    streams = [(name, _captured_text(result.get(name))) for name in ("stdout", "stderr")]
    if any(text is None for _, text in streams):
        return []
    runs = outcomes = 0
    summaries = []
    for name, text in streams:
        lines = text.replace("\r\n", "\n").split("\n")
        runs += sum(line.startswith("Ran ") for line in lines)
        outcomes += sum(_OUTCOME.fullmatch(line) is not None for line in lines)
        summary = _summary(text, name)
        if summary is not None:
            summaries.append(summary)
    if runs != 1 or outcomes != 1 or len(summaries) != 1:
        return []
    summary = summaries[0]
    code = result.get("exit_code")
    command_failed = (type(code) is int and code != 0
                      or result.get("status") in ("error", "failed") or result.get("outcome") == "failed")
    command_passed = (type(code) is int and code == 0
                      or result.get("status") == "ok" or result.get("outcome") == "passed")
    if summary["reported_outcome"] == "OK" and command_failed:
        summary["program_report_note"] += " Output reports OK while command metadata reports failure."
    elif summary["reported_outcome"] == "FAILED" and command_passed and not command_failed:
        summary["program_report_note"] += " Output reports FAILED while command metadata reports success."
    return summaries
