"""Opt-in input barrier for future host-created supervision runs.

This module checks a persisted run's source identity and subsequent customer
messages only. It does not validate the recovery event's legitimacy or its
history, establish scope, or create or authorize supervision. A future
scheduler must independently validate those facts and atomically reject new
customer inputs since the original run before creating this contract.
"""

from .events import Actor
from .work import WorkConflict


HOST = Actor.system("team")
KIND = "all_customer_messages"


def check(task_id, started, role, events):
    """Check the opt-in barrier; source identity is not recovery authorization."""
    if "customer_input_barrier" not in started.payload:
        return
    contract = started.payload["customer_input_barrier"]
    if (type(contract) is not dict or set(contract) != {"version", "kind", "source_seq", "source_hash"}
            or type(contract["version"]) is not int or contract["version"] != 1
            or contract["kind"] != KIND or type(contract["source_seq"]) is not int
            or contract["source_seq"] != started.payload.get("message_seq")
            or not isinstance(contract["source_hash"], str)):
        raise WorkConflict("监督输入屏障无效，未采用本轮动作。")
    source = next((e for e in events if e.seq == contract["source_seq"]), None)
    stored = next((e for e in events if e.seq == started.seq), None)
    if (stored != started or started.actor != HOST or started.type != "team.run_started"
            or started.task_id != task_id or started.payload.get("role") != role.id
            or source is None or source.task_id != task_id or source.actor != HOST
            or source.type != "team.message_received" or source.payload.get("kind") != "recovery"
            or source.payload.get("recipient") != role.id or source.hash != contract["source_hash"]
            or source.seq >= started.seq):
        raise WorkConflict("监督输入屏障来源未通过核对，未采用本轮动作。")
    if any(e.task_id == task_id and e.actor == Actor.human() and e.type == "team.message_received" and e.seq > source.seq
           for e in events):
        raise WorkConflict("顾客已向团队补充新消息，本轮监督让位；已启动操作保留实际回执。")
