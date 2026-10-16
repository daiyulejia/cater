"""Response shape and citation diagnostics, separate from quality judgments."""

from __future__ import annotations

from copy import deepcopy
import json
from importlib.resources import files

from .verification import output_metadata


SCHEMA = json.loads(files("cater.packs").joinpath("review-response.schema.json").read_text(encoding="utf-8"))


def receipt_context(event) -> dict:
    """Avoid sending the same parsed browser report twice; retain the immutable receipt link."""
    value = {**event.payload, "seq": event.seq, "event_hash": event.hash}
    if (value.get("check_kind") == "browser" and isinstance(value.get("browser_report"), dict)
            and value["browser_report"].get("outcome") in {"passed", "failed"}
            and value.get("stdout", {}).get("complete") is True
            and value.get("stdout", {}).get("truncated") is False):
        value["stdout"] = output_metadata(value["stdout"])
    return value


ISSUES = {
    "missing_result": "复核回复缺少这项标准的结果，需要复核人补全回复。",
    "duplicate_result": "复核回复重复填写同一项标准，需要复核人整理回复。",
    "invalid_status": "复核回复的结论格式不符合约定，需要复核人重新表达。",
    "missing_reason": "复核回复没有写清判断依据，需要复核人补充说明。",
    "invalid_artifact": "复核回复引用的成果编号不属于本次提交，需要复核人更正引用。",
    "missing_quote": "复核回复未引用实际正文，需要复核人补上原文依据。",
    "quote_not_found": "复核回复的引文与实际正文不一致，需要复核人改用原文。",
    "invalid_receipt": "复核回复未关联当前检查回执，需要复核人核对检查记录。",
}


def response_schema(context: dict) -> dict:
    schema = deepcopy(SCHEMA)
    fields = schema["properties"]["checks"]["items"]["properties"]
    fields["id"]["enum"] = [c["id"] for c in context["contract"]["criteria"]]
    fields["artifact_seq"]["enum"] = [None, *(a["seq"] for a in context["artifacts"])]
    fields["receipt_seq"]["enum"] = [None, *(c["seq"] for c in context["local_checks"].values())]
    return schema


def browser_observations(receipt: dict | None) -> list[str]:
    if not isinstance(receipt, dict) or receipt.get("check_kind") != "browser":
        return []
    report = receipt.get("browser_report")
    if not isinstance(report, dict):
        return []
    values = [report.get("error")]
    steps = report.get("steps", [])
    if isinstance(steps, list):
        for step in steps:
            if isinstance(step, dict):
                values.append(step.get("error"))
                if step.get("status") != "passed":
                    values.append(step.get("actual"))
    # Cite observed output, not a plan label or an assertion's expected value.
    observed = [value for value in values if isinstance(value, str) and value.strip()]
    if (isinstance(steps, list) and receipt.get("outcome") != "unavailable"
            and report.get("outcome") in ("passed", "failed")
            and report.get("phase", "execution") == "execution" and report.get("truncated", False) is False):
        # Import lazily: browser_checks also uses the work ledger's review contract.
        from .browser_checks import ACTION_FIELDS

        for index, step in enumerate(steps, 1):
            if (not isinstance(step, dict) or type(step.get("index")) is not int or step["index"] != index
                    or not isinstance(step.get("action"), str) or step["action"] not in ACTION_FIELDS
                    or step.get("status") not in ("passed", "failed")):
                break
            assertion = step["action"].startswith("assert_")
            if ("actual" in step and (not assertion or type(step["actual"]) is not bool)
                    or assertion and step["status"] == "passed" and step.get("actual") is not True
                    or step["status"] == "passed" and step.get("error")
                    or step["status"] == "failed" and not (isinstance(step.get("error"), str) and step["error"].strip())):
                break
            fields = {key: step[key] for key in ("index", "action", "status", "actual") if key in step}
            observed.append(json.dumps(fields, ensure_ascii=False))
            if isinstance(step.get("observed"), str) and step["observed"].strip():
                observed.append(step["observed"])
            # The runner stops at its first failure; later plan rows were not executed.
            if step["status"] == "failed":
                break
    return list(dict.fromkeys(observed))


def execution_observations(receipt: dict | None) -> list[str]:
    if not isinstance(receipt, dict) or receipt.get("check_kind") == "browser":
        return browser_observations(receipt)
    if receipt.get("check_kind") != "workspace":
        if not isinstance(receipt.get("command"), list) or receipt.get("outcome") not in {"passed", "failed", "timeout", "launch_error"}:
            return []
        values = [json.dumps({k: receipt[k] for k in ("command", "outcome", "exit_code") if k in receipt}, ensure_ascii=False)]
        for stream in ("stdout", "stderr"):
            capture = receipt.get(stream)
            if isinstance(capture, dict) and isinstance(capture.get("excerpt"), str) and capture["excerpt"].strip():
                values.append(capture["excerpt"])
            if (isinstance(capture, dict) and capture.get("preview_mode") == "head_tail"
                    and isinstance(capture.get("tail_excerpt"), str) and capture["tail_excerpt"].strip()):
                values.append(capture["tail_excerpt"])
        return values
    rows = receipt.get("suite")
    if not isinstance(rows, list) or not rows:
        return []
    observations = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("result"), dict):
            return []
        result = row["result"]
        observations.append(json.dumps({"id": row.get("id"), "command_alias": row.get("command_alias"),
            "exit_code": result.get("exit_code"), "outcome": result.get("outcome")}, ensure_ascii=False))
        for stream in ("stdout", "stderr"):
            capture = result.get(stream)
            if isinstance(capture, dict) and isinstance(capture.get("excerpt"), str) and capture["excerpt"].strip():
                observations.append(capture["excerpt"])
            if (isinstance(capture, dict) and capture.get("preview_mode") == "head_tail"
                    and isinstance(capture.get("tail_excerpt"), str) and capture["tail_excerpt"].strip()):
                observations.append(capture["tail_excerpt"])
    return observations


def _receipt_judgment(row: dict, receipt: dict, quote: str) -> dict:
    judgment = {"status": row["status"], "reason": row["reason"], "quote": quote, "artifact_seq": None}
    report = receipt.get("browser_report")
    if (row.get("next_action") == "revise_check" and row["status"] == "unknown"
            and receipt.get("check_kind") == "browser" and receipt.get("outcome") in {"passed", "failed"}
            and isinstance(report, dict) and report.get("outcome") == receipt["outcome"]
            and type(receipt.get("exit_code")) is int
            and receipt["exit_code"] == (0 if receipt["outcome"] == "passed" else 1)
            and all(isinstance(receipt.get(stream), dict) and receipt[stream].get("complete") is True
                    and not receipt[stream].get("truncated", False) for stream in ("stdout", "stderr"))
            and report.get("phase", "execution") == "execution" and report.get("truncated", False) is False):
        judgment["next_action"] = "revise_check"
    return judgment


def assessment(results: list, criterion_id: str, artifacts: dict,
               receipt: dict | None = None) -> tuple[dict | None, str | None, dict | None]:
    rows = [r for r in results if isinstance(r, dict) and r.get("id") == criterion_id]
    if len(rows) != 1:
        return None, "missing_result" if not rows else "duplicate_result", None
    row = rows[0]
    status, quote, seq, reason = (row.get(k) for k in ("status", "quote", "artifact_seq", "reason"))
    if status not in ("passed", "failed", "unknown"):
        return None, "invalid_status", row
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
        return None, "missing_reason", row
    if status == "unknown" and seq is None and quote in (None, ""):
        return {"status": status, "reason": reason}, None, row
    if seq is None and isinstance(receipt, dict) and receipt.get("check_kind") in {"browser", "workspace"}:
        if (type(row.get("receipt_seq")) is not int or type(receipt.get("seq")) is not int
                or receipt["seq"] <= 0 or row["receipt_seq"] != receipt["seq"]):
            return None, "invalid_receipt", row
        if not isinstance(quote, str) or not quote.strip() or len(quote) > 2000:
            return None, "missing_quote", row
        if not any(quote in observed for observed in execution_observations(receipt)):
            return None, "quote_not_found", row
        return _receipt_judgment(row, receipt, quote), None, row
    if type(seq) is not int or seq not in artifacts:
        return None, "invalid_artifact", row
    if not isinstance(quote, str) or not quote.strip() or len(quote) > 2000:
        return None, "missing_quote", row
    if quote not in artifacts[seq]["content"]:
        return None, "quote_not_found", row
    return {"status": status, "reason": reason, "quote": quote, "artifact_seq": seq}, None, row
