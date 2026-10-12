"""Cabinet vote tallying.

The cabinet layer turns agent positions into a small decision record. It does
not call models or mutate runtime state; it is pure logic that can be replayed
from stored messages.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping


def tally_votes(
    positions: Iterable[Mapping[str, Any]],
    *,
    electorate_size: int | None = None,
    approval_threshold: int | None = None,
) -> dict[str, Any]:
    rows = [dict(item) for item in positions]
    size = electorate_size if electorate_size is not None else len(rows)
    threshold = approval_threshold if approval_threshold is not None else majority_threshold(size)
    options: dict[str, dict[str, Any]] = {}
    votes = []

    for row in rows:
        actor = str(row.get("actor") or "unknown")
        option_id = str(row.get("option_id") or actor)
        stance = str(row.get("stance") or "assert")
        vote = "concern" if stance == "challenge" else "support"
        claim = str(row.get("claim") or "")

        option = options.setdefault(
            option_id,
            {
                "id": option_id,
                "summary": claim,
                "support": 0,
                "concern": 0,
                "supporters": [],
                "concerns": [],
                "score": 0,
            },
        )
        if not option["summary"] and claim:
            option["summary"] = claim

        if vote == "support":
            option["support"] += 1
            option["supporters"].append(actor)
            option["score"] += 1
        else:
            option["concern"] += 1
            option["concerns"].append(actor)
            option["score"] -= 1

        votes.append(
            {
                "actor": actor,
                "option_id": option_id,
                "vote": vote,
                "stance": stance,
                "weight": 1,
            }
        )

    ordered = sorted(
        options.values(),
        key=lambda item: (-int(item["support"]), int(item["concern"]), str(item["id"])),
    )
    recommendation = next(
        (item["id"] for item in ordered if item["support"] >= threshold and item["concern"] == 0),
        None,
    )
    if recommendation is None:
        status = "needs_human"
        reason = f"No option reached approval threshold {threshold} without open concerns."
    else:
        status = "recommended"
        reason = f"Option {recommendation} reached approval threshold {threshold}."

    return {
        "electorate_size": size,
        "approval_threshold": threshold,
        "status": status,
        "recommendation": recommendation,
        "reason": reason,
        "votes": votes,
        "options": ordered,
    }


def majority_threshold(size: int) -> int:
    if size <= 0:
        return 1
    return size // 2 + 1
