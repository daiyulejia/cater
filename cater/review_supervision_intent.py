"""Persist an inert, one-shot intent during the live drain's finish transaction.

This record is not a candidate, a claim, or authority to invoke a driver.
There is deliberately no startup discovery or recovery consumer here.
"""

import threading

from .events import Actor
from .team_recovery import _same_scope


HOST = Actor.system("team")
TYPE = "team.review_supervision_intended"
CONTRACT = {"version": 1, "kind": "finished_review_supervision"}


def record(team, task_id, message, role, started, finished):
    """Record only the currently owned drain step, never reconstruct old intents."""
    if (not team.rt.log.db.in_transaction or not team._owns_drain(task_id)
            or not team.external.scheduling(task_id)
            or getattr(team, "_drain_step", None) != (task_id, started.seq, started.hash, threading.get_ident())
            or role.id != team.rt.organization.leader.id or message.actor.kind != "agent"
            or message.payload.get("kind") != "report" or type(message.payload.get("review_seq")) is not int
            or finished.actor != HOST or finished.type != "team.run_finished"
            or finished.payload.get("status") != "completed"
            or any(finished.payload.get(k) != started.payload.get(k) for k in ("run_id", "role", "message_seq"))):
        return None

    from .external_agent import QoderAcpDriver
    from .native_response_evidence import trusted_protocol
    from .review_supervision_candidate import _review_evidence, require_verified_log, SupervisionLogIntegrityError

    driver = team.rt.drivers.get(role.driver)
    protocol = trusted_protocol(driver)
    is_qoder = isinstance(driver, QoderAcpDriver)
    if not is_qoder and protocol is None:
        return None
    try:
        require_verified_log(team.rt.log)
    except SupervisionLogIntegrityError:
        return None
    events = team.rt.log.read(task_id=task_id)
    if (started.task_id != task_id or message.task_id != task_id or finished.task_id != task_id
            or started.actor != HOST or started.type != "team.run_started"
            or started.payload.get("message_seq") != message.seq or started.payload.get("role") != role.id
            or not any(e == message for e in events) or not any(e == started for e in events)
            or not any(e == finished for e in events)
            or not message.seq < started.seq < finished.seq
            or sum(e.type == "team.run_finished" and e.actor == HOST
                   and all(e.payload.get(k) == started.payload.get(k) for k in ("run_id", "role", "message_seq"))
                   for e in events) != 1
            or any(e.type == TYPE and e.actor == HOST and e.payload.get("run_seq") == started.seq for e in events)
            or any(e.type == "team.message_received" and e.actor == Actor.human() and e.seq > started.seq for e in events)
            or any(e.payload.get("run_id") == started.payload.get("run_id")
                   and (e.type == "team.tool_started" and e.actor == HOST
                        or e.type == "msg.said" and e.payload.get("audience") == "customer") for e in events)):
        return None
    scope = team.recovery.scope(task_id, events=events)
    if (scope.get("control_state") != "running" or scope.get("done") is not False
            or not _same_scope(scope, started.payload.get("recovery_scope"))
            or started.payload.get("revision") != scope.get("revision")
            or team.rt.conversation_list.is_removed(task_id)):
        return None
    try:
        reviewer = team._role(message.actor.id)
    except (KeyError, ValueError):
        return None
    evidence = _review_evidence(events, task_id=task_id, leader_actor=Actor.agent(role.id, role.role),
        reviewer_actor=Actor.agent(reviewer.id, reviewer.role), run_seq=started.seq, run_hash=started.hash)
    if evidence is None or evidence.get("leader_finish") != {"seq": finished.seq, "hash": finished.hash}:
        return None
    from .team_review_direction import current_matches, direction_at, evidence_direction, report_customer_sources
    direction = evidence_direction(events, evidence)
    if direction is None or direction != direction_at(events, started.seq) or not current_matches(team, task_id, events, evidence):
        return None
    customer_inputs = report_customer_sources(team, task_id, message, before_seq=started.seq, events=events)
    if customer_inputs is None:
        return None
    if is_qoder:
        if "external_finish" not in evidence:
            return None
        protocol = "qoder_acp"
    else:
        native = evidence.get("native_response", {})
        response = next((e for e in events if e.seq == native.get("seq") and e.hash == native.get("hash")), None)
        if response is None or response.payload.get("protocol") != protocol:
            return None
    return team.rt.log.emit(type=TYPE, task_id=task_id, actor=HOST, payload={
        "contract": dict(CONTRACT), "run_seq": started.seq, "run_hash": started.hash,
        "finish_seq": finished.seq, "finish_hash": finished.hash,
        "report_seq": message.seq, "report_hash": message.hash,
        "scope": {**scope, "work_direction_seq": direction}, "protocol": protocol, "evidence": evidence,
        "customer_inputs": customer_inputs,
    })
