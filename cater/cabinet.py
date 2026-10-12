"""Cabinet vote tallying.

The cabinet layer turns agent positions into a small decision record. It does
not call models or mutate runtime state; it is pure logic that can be replayed
from stored messages.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping


@dataclass(frozen=True)
class CabinetPolicy:
    approval_threshold: int | None = None
    quorum: int | None = None
    veto_roles: frozenset[str] = field(default_factory=frozenset)

    @staticmethod
    def from_dict(data: Mapping[str, Any] | None) -> "CabinetPolicy":
        if not data:
            return CabinetPolicy()
        threshold = data.get("approval_threshold")
        quorum = data.get("quorum")
        veto_roles = data.get("veto_roles") or []
        if not isinstance(veto_roles, list):
            raise ValueError("cabinet.veto_roles must be a list")
        return CabinetPolicy(
            approval_threshold=int(threshold) if threshold is not None else None,
            quorum=int(quorum) if quorum is not None else None,
            veto_roles=frozenset(str(role) for role in veto_roles),
        )


def tally_votes(
    positions: Iterable[Mapping[str, Any]],
    *,
    electorate_size: int | None = None,
    approval_threshold: int | None = None,
    quorum: int | None = None,
    veto_roles: Iterable[str] | None = None,
) -> dict[str, Any]:
    rows = [dict(item) for item in positions]
    size = electorate_size if electorate_size is not None else len(rows)
    threshold = approval_threshold if approval_threshold is not None else majority_threshold(size)
    quorum_size = quorum if quorum is not None else 1
    veto_set = {str(role) for role in (veto_roles or [])}
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
    participation = len(votes)
    top_option = ordered[0]["id"] if ordered else None
    vetoed_by = sorted(
        vote["actor"] for vote in votes if vote["actor"] in veto_set and vote["vote"] == "concern"
    )
    recommendation = next(
        (item["id"] for item in ordered if item["support"] >= threshold and item["concern"] == 0),
        None,
    )
    if participation < quorum_size:
        status = "needs_human"
        reason = f"Quorum {quorum_size} not met; only {participation} vote(s) recorded."
        recommendation = None
    elif vetoed_by:
        status = "vetoed"
        reason = f"Veto role(s) raised concern: {', '.join(vetoed_by)}."
        recommendation = None
    elif recommendation is None:
        status = "needs_supreme" if top_option is not None else "needs_human"
        reason = (
            f"No option reached approval threshold {threshold}; supreme decision should pick from ranked options."
            if top_option is not None
            else f"No option reached approval threshold {threshold} and no option was available."
        )
    else:
        status = "recommended"
        reason = f"Option {recommendation} reached approval threshold {threshold}."

    return {
        "electorate_size": size,
        "approval_threshold": threshold,
        "quorum": quorum_size,
        "veto_roles": sorted(veto_set),
        "vetoed_by": vetoed_by,
        "top_option": top_option,
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
