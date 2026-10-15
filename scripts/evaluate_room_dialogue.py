"""Read-only, synthetic room-dialogue evaluation; no runtime or task writes."""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cater.drivers import (CliDriver, Driver, DriverProtocolError, DriverUnavailable,
                           driver_mode, load_drivers)
from cater.room_dialogue import ACTIONS, build_request, validate_response
from cater.usage import usage_source, valid_token_count


FIXTURES = ROOT / "tests" / "fixtures" / "room_dialogue_cases.json"
REPORT_DIR = ROOT / ".cater" / "evaluations"
TARGET_ACTIONS = {"update_task", "explain_task", "resume_task", "status_task"}
USAGE_FIELDS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens",
                "prompt_tokens", "completion_tokens", "total_tokens")
STOP_REASONS = {"end_turn", "stop_sequence", "max_tokens", "tool_use", "pause_turn", "refusal",
                "model_context_window_exceeded", "stop", "length", "content_filter", "tool_calls"}
IDENTIFIER = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
LABELS = {
    "matched": "预期匹配", "mismatch": "预期不匹配", "parse_error": "JSON 解析失败",
    "schema_error": "结构验证失败", "provider_error": "提供方异常，已停止",
    "provider_incomplete": "提供方输出不完整", "not_run": "未运行",
}
REASONS = {
    "case_filter": "未选中", "limit": "调用上限", "provider_error": "提供方异常后停止",
    "budget_exhausted": "已报告用量达到预算", "usage_unknown": "用量不可确认，停止后续调用",
}


class EvaluationError(ValueError):
    """Only fixed, non-sensitive error codes may reach CLI diagnostics."""


def safe_exception_type(exc: Exception) -> str:
    for cls in (DriverUnavailable, DriverProtocolError, json.JSONDecodeError,
                TimeoutError, OSError, UnicodeError, TypeError, ValueError):
        if isinstance(exc, cls):
            return cls.__name__
    return "Exception"


def validate_cases(cases: Any) -> list[tuple[str, dict[str, Any]]]:
    """Validate every case, including unselected cases, before any invocation."""
    if not isinstance(cases, list) or not cases:
        raise EvaluationError("fixtures_invalid")
    seen: set[str] = set()
    requests = []
    for case in cases:
        if not isinstance(case, dict) or set(case) != {"id", "message", "history", "tasks", "expected"}:
            raise EvaluationError("fixtures_invalid")
        case_id = case["id"]
        if not isinstance(case_id, str) or not IDENTIFIER.fullmatch(case_id) or case_id in seen:
            raise EvaluationError("fixtures_invalid")
        seen.add(case_id)
        if not isinstance(case["history"], list) or not isinstance(case["tasks"], list):
            raise EvaluationError("fixtures_invalid")
        for turn in case["history"]:
            if (not isinstance(turn, dict) or set(turn) != {"actor", "text"}
                    or turn["actor"] not in ("human", "store_manager")
                    or not isinstance(turn["text"], str) or not turn["text"].strip()):
                raise EvaluationError("fixtures_invalid")
        task_ids: set[str] = set()
        for task in case["tasks"]:
            if (not isinstance(task, dict) or set(task) != {"id", "goal", "status"}
                    or not isinstance(task["id"], str) or not IDENTIFIER.fullmatch(task["id"])
                    or task["id"] in task_ids or not isinstance(task["goal"], str)
                    or not task["goal"].strip() or task["status"] not in ("pending", "running", "paused", "done")):
                raise EvaluationError("fixtures_invalid")
            task_ids.add(task["id"])
        expected = case["expected"]
        if (not isinstance(expected, dict) or set(expected) != {"actions", "task_id"}
                or not isinstance(expected["actions"], list) or not expected["actions"]
                or any(not isinstance(a, str) or a not in ACTIONS for a in expected["actions"])):
            raise EvaluationError("fixtures_invalid")
        target = expected["task_id"]
        if target is not None and (not isinstance(target, str) or target not in task_ids):
            raise EvaluationError("fixtures_invalid")
        if any((action in TARGET_ACTIONS) != (target is not None) for action in expected["actions"]):
            raise EvaluationError("fixtures_invalid")
        try:
            requests.append(build_request(message=case["message"], history=copy.deepcopy(case["history"]),
                                          tasks=copy.deepcopy(case["tasks"])))
        except (ValueError, TypeError) as exc:
            raise EvaluationError("fixtures_invalid") from exc
    return requests


def evaluate(driver: Driver, cases: list[dict[str, Any]], *, limit: int = 3,
             case_id: str | None = None) -> dict[str, Any]:
    requests = validate_cases(cases)
    if type(limit) is not int or limit <= 0:
        raise EvaluationError("limit_invalid")
    if case_id is not None and case_id not in {case["id"] for case in cases}:
        raise EvaluationError("case_unknown")
    if driver_mode(driver) == "local_demo":
        raise EvaluationError("local_demo")
    budget = getattr(driver, "token_budget", None)
    if not valid_token_count(budget) or budget == 0:
        raise EvaluationError("budget_invalid")
    # CliDriver otherwise retries transport errors internally by default.
    if isinstance(driver, CliDriver):
        driver.restart_limit = 0

    results = []
    calls, reported, reported_calls = 0, 0, 0
    stop_reason = None
    started = time.perf_counter()
    for case, (prompt, context) in zip(cases, requests):
        row = {"id": case["id"], "expected": copy.deepcopy(case["expected"]),
               "status": "not_run", "schema_valid": None, "expected_match": None,
               "token_usage": "not_invoked", "reported_tokens": None, "wall_ms": None,
               "provider_usage": None, "stop_reason": None, "contained": None, "invocation_started": None}
        results.append(row)
        reason = ("case_filter" if case_id is not None and case["id"] != case_id
                  else stop_reason or ("limit" if calls >= limit else None))
        if reason:
            row["reason"] = reason
            continue
        calls += 1
        call_started = time.perf_counter()
        try:
            response = driver.complete(role="store_manager", prompt=prompt, context=copy.deepcopy(context))
        except Exception as exc:
            row.update(status="provider_error", error_type=safe_exception_type(exc), token_usage="unavailable")
            stop_reason = "provider_error"
        else:
            meta = response.meta if isinstance(response.meta, dict) else {}
            usage = meta.get("usage")
            if isinstance(usage, dict):
                row["provider_usage"] = {key: usage[key] for key in USAGE_FIELDS
                                         if key in usage and valid_token_count(usage[key])}
            stop = meta.get("stop_reason")
            row["stop_reason"] = stop if isinstance(stop, str) and stop in STOP_REASONS else (
                "unknown" if stop is not None else None)
            row["contained"] = bool(meta.get("contained"))
            if type(meta.get("invocation_started")) is bool:
                row["invocation_started"] = meta["invocation_started"]
            source = usage_source(response.tokens, meta.get("token_usage"))
            row["token_usage"] = source
            if source == "reported":
                row["reported_tokens"] = response.tokens
                reported += response.tokens
                reported_calls += 1
                if reported >= budget:
                    stop_reason = "budget_exhausted"
            else:
                # Estimates and missing telemetry cannot establish remaining budget.
                stop_reason = "usage_unknown"
            if meta.get("contained"):
                row["status"] = "provider_incomplete"
            else:
                try:
                    raw = json.loads(response.text)
                except (ValueError, TypeError) as exc:
                    row.update(status="parse_error", error_type=safe_exception_type(exc))
                else:
                    try:
                        decision = validate_response(raw, message=case["message"],
                                                     task_ids={task["id"] for task in case["tasks"]})
                    except (ValueError, TypeError) as exc:
                        row.update(status="schema_error", schema_valid=False, error_type=safe_exception_type(exc))
                    else:
                        matches = (decision["action"] in case["expected"]["actions"]
                                   and decision["task_id"] == case["expected"]["task_id"])
                        row.update(status="matched" if matches else "mismatch", schema_valid=True,
                                   expected_match=matches,
                                   decision={"action": decision["action"], "task_id": decision["task_id"]})
        row["wall_ms"] = round((time.perf_counter() - call_started) * 1000, 3)

    counts = Counter(row["status"] for row in results)
    blocked = any(row.get("reason") in {"provider_error", "budget_exhausted", "usage_unknown"} for row in results)
    return {
        "version": 1, "synthetic_only": True, "read_only": True,
        "scope": "仅比较动作和任务对象；结构通过不代表回应质量通过；未运行样例不作结论。",
        "budget_scope": "仅本次运行的已报告用量；请求结束后检查，不能保证单次请求不超预算。",
        "limit": limit, "token_budget": budget, "stop_reason": stop_reason,
        "ok": calls > 0 and counts["matched"] == calls and not blocked and reported_calls == calls,
        "summary": {
            "total": len(cases), "calls": calls, "not_run": counts["not_run"],
            "matched": counts["matched"], "mismatch": counts["mismatch"],
            "schema_valid": sum(row["schema_valid"] is True for row in results),
            "parse_error": counts["parse_error"], "schema_error": counts["schema_error"],
            "provider_error": counts["provider_error"], "provider_incomplete": counts["provider_incomplete"],
            "reported_tokens": reported if reported_calls else None, "reported_calls": reported_calls,
            "usage_complete": reported_calls == calls, "unknown_usage_calls": calls - reported_calls,
            "wall_ms": round((time.perf_counter() - started) * 1000, 3),
            "call_wall_ms": round(sum(row["wall_ms"] or 0 for row in results), 3),
        },
        "cases": results,
    }


def output_path(value: str | None) -> Path:
    if value is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = REPORT_DIR / f"room-dialogue-{stamp}-{uuid.uuid4().hex[:8]}.json"
    else:
        path = Path(value)
    path = path.resolve()
    if path.suffix.lower() != ".json" or (path.is_relative_to(ROOT) and not path.is_relative_to(REPORT_DIR)):
        raise EvaluationError("output_invalid")
    return path


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(description="只读群聊评估，仅发送内置合成样例。")
    cli.add_argument("--config", required=True, type=Path, help="现有驱动配置文件")
    cli.add_argument("--driver", required=True, help="配置中的驱动名称（拒绝 local_demo）")
    cli.add_argument("--limit", type=int, default=3, help="最多调用次数，默认 3；不自动重试")
    cli.add_argument("--case", dest="case_id", help="只评估指定样例 ID")
    cli.add_argument("--output", help="JSON 报告文件，默认 .cater/evaluations/；也可指定仓库外路径")
    return cli


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        target = output_path(args.output)
        if target == args.config.resolve() or target.exists():
            raise EvaluationError("output_exists")
        cases = json.loads(FIXTURES.read_text(encoding="utf-8"))
        validate_cases(cases)
        if args.limit <= 0:
            raise EvaluationError("limit_invalid")
        if args.case_id is not None and args.case_id not in {case["id"] for case in cases}:
            raise EvaluationError("case_unknown")
        drivers = load_drivers(args.config)
        if args.driver not in drivers:
            raise EvaluationError("driver_unknown")
        driver = drivers[args.driver]
        try:
            report = evaluate(driver, cases, limit=args.limit, case_id=args.case_id)
        finally:
            close = getattr(driver, "close", None)
            if callable(close):
                close()
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    except Exception as exc:
        code = str(exc) if type(exc) is EvaluationError else safe_exception_type(exc)
        print(f"评估未启动或报告未完成：{code}。", file=sys.stderr)
        return 2
    for row in report["cases"]:
        suffix = f"（{REASONS[row['reason']]}）" if row.get("reason") else ""
        print(f"{row['id']}: {LABELS[row['status']]}{suffix}")
    summary = report["summary"]
    tokens = summary["reported_tokens"] if summary["reported_tokens"] is not None else "未知"
    print(f"调用 {summary['calls']}；匹配 {summary['matched']}；已报告 token {tokens}；"
          f"用量未知 {summary['unknown_usage_calls']} 次；耗时 {summary['wall_ms']} ms。")
    print(report["scope"])
    # Paths, driver configuration, response prose and provider metadata are not diagnostics.
    print("JSON 报告已写入。")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
