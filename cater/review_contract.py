"""Response shape and citation diagnostics, separate from quality judgments."""

from __future__ import annotations

from copy import deepcopy
import json
from importlib.resources import files


SCHEMA = json.loads(files("cater.packs").joinpath("review-response.schema.json").read_text(encoding="utf-8"))
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
    if not receipt or receipt.get("check_kind") != "browser":
        return []
    report = receipt.get("browser_report")
    if not isinstance(report, dict):
        return []
    values = [report.get("error")]
    steps = report.get("steps", [])
    if isinstance(steps, list):
        for step in steps:
            if isinstance(step, dict):
                values.extend((step.get("error"), step.get("actual")))
    # Cite observed output, not a plan label or an assertion's expected value.
    return list(dict.fromkeys(value for value in values if isinstance(value, str) and value.strip()))


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
    if seq is None and receipt and receipt.get("check_kind") == "browser":
        if type(row.get("receipt_seq")) is not int or row["receipt_seq"] != receipt.get("seq"):
            return None, "invalid_receipt", row
        if not isinstance(quote, str) or not quote.strip() or len(quote) > 2000:
            return None, "missing_quote", row
        if not any(quote in observed for observed in browser_observations(receipt)):
            return None, "quote_not_found", row
        return {"status": status, "reason": reason, "quote": quote, "artifact_seq": None}, None, row
    if type(seq) is not int or seq not in artifacts:
        return None, "invalid_artifact", row
    if not isinstance(quote, str) or not quote.strip() or len(quote) > 2000:
        return None, "missing_quote", row
    if quote not in artifacts[seq]["content"]:
        return None, "quote_not_found", row
    return {"status": status, "reason": reason, "quote": quote, "artifact_seq": seq}, None, row
