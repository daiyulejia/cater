"""Read-only native review evidence; no scheduling or authority to resume.

The caller supplies a trusted, complete task event snapshot and expected actor
identities, as for Qoder review evidence. This performs structural and receipt
checks, not cryptographic authentication, log verification or tamper detection.
In particular, seq is not part of the event signing fingerprint. Later admission
must use the host transaction to verify its existing log policy and recheck hash
bindings, scope, all-role customer input, active work and unknown effects.
Local cleanup does not prove descendant or remote-service termination.
"""
from .events import Actor
from .native_response_evidence import validate as validate_response
from .review_return_evidence import _authenticate_review
from .team_recovery import _same_scope

HOST = Actor.system('team')


def authenticate(events, *, task_id, leader_actor, reviewer_actor, run_seq, run_hash):
    """Check a caller-trusted complete snapshot; never grants supervision authority."""
    try:
        return _authenticate_snapshot(events, task_id=task_id, leader_actor=leader_actor,
                                      reviewer_actor=reviewer_actor, run_seq=run_seq, run_hash=run_hash)
    except (TypeError, ValueError, KeyError, AttributeError):
        return None


def _authenticate_snapshot(events, *, task_id, leader_actor, reviewer_actor, run_seq, run_hash):
    """Internal structural check; the caller owns snapshot trust and completeness."""
    chain = _authenticate_review(events, task_id=task_id, leader_actor=leader_actor,
                                 reviewer_actor=reviewer_actor, run_seq=run_seq, run_hash=run_hash)
    if chain is None:
        return None
    events, _, start, refs = chain
    run_id = start.payload['run_id']
    receipts = [e for e in events if e.actor == HOST and e.type == 'team.native_response_completed'
                and (e.payload.get('run_seq') == start.seq or e.payload.get('run_id') == run_id)]
    calls = [e for e in events if e.actor == HOST and e.type == 'team.model_called'
             and e.payload.get('run_id') == run_id]
    finishes = [e for e in events if e.actor == HOST and e.type == 'team.run_finished'
                and e.payload.get('run_id') == run_id]
    starts = [e for e in events if e.actor == HOST and e.type == 'team.run_started'
              and e.payload.get('run_id') == run_id]
    if len(receipts) != 1 or len(calls) != 1 or len(finishes) != 1 or len(starts) != 1:
        return None
    receipt, call, finish = receipts[0], calls[0], finishes[0]
    if (not validate_response(events, task_id=task_id, receipt=receipt)
            or receipt.payload.get('run_seq') != start.seq or receipt.payload.get('run_hash') != start.hash
            or receipt.payload.get('report_seq') != refs['report']['seq']
            or receipt.payload.get('report_hash') != refs['report']['hash']
            or receipt.payload.get('model_call_seq') != call.seq or receipt.payload.get('model_call_hash') != call.hash
            or not _same_scope(receipt.payload.get('scope'), start.payload.get('recovery_scope'))
            or receipt.payload['scope'].get('control_state') != 'running'
            or receipt.payload['scope'].get('done') is not False
            or any(finish.payload.get(k) != start.payload.get(k) for k in ('run_id', 'role', 'message_seq'))
            or finish.payload.get('status') != 'completed' or finish.seq <= receipt.seq
            or finish.payload.get('native_cleanup_failed') is True
            or any(e.actor == HOST and e.type in {'team.tool_started', 'team.external_run_started'}
                   and e.payload.get('run_id') == run_id for e in events)
            or any(e.type == 'msg.said' and e.payload.get('run_id') == run_id
                   and e.payload.get('audience') == 'customer' for e in events)):
        return None
    return {**refs, **{name: {'seq': e.seq, 'hash': e.hash} for name, e in {
        'model_call': call, 'native_response': receipt, 'leader_finish': finish}.items()}}
