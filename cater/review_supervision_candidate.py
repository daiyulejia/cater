"""Create an inert, durable candidate; this module never schedules a run."""

from .events import Actor
from .review_return_evidence import authenticate
from .team_recovery import _same_scope
from .work import WorkConflict
import sqlite3


HOST = Actor.system('team')
TRIGGER = 'review_return_candidate'


class SupervisionLogIntegrityError(WorkConflict):
    """The candidate check stopped because the existing log could not be verified."""


def require_verified_log(log):
    """Fail visibly without exposing verifier details, key identifiers or key material."""
    try:
        report = log.verify_chain()
    except (sqlite3.Error, OSError, TypeError, ValueError, UnicodeError):
        raise SupervisionLogIntegrityError('监督未启动：事件日志无法校验，请核对日志和审计配置。') from None
    if not report.ok:
        if report.reason.startswith('signature present but no audit key supplied'):
            raise SupervisionLogIntegrityError('监督未启动：缺少事件日志所需的审计验证密钥，请核对审计配置。')
        raise SupervisionLogIntegrityError('监督未启动：事件日志完整性校验未通过，请核对日志和审计配置。')


def _payload(leader_id, run_seq, run_hash, scope, evidence, customer_inputs=None):
    return {
        'kind': 'recovery', 'trigger': TRIGGER, 'recipient': leader_id,
        'original_run_seq': run_seq, 'original_run_hash': run_hash,
        'revision': scope['revision'], 'scope': scope, 'evidence': evidence,
        **({'customer_inputs': customer_inputs} if customer_inputs is not None else {}),
        'text': '正式复核回报已处理，但店长尚未采取后续动作。请结合顾客任务与已有回执判断下一步。',
    }


def _review_evidence(events, *, task_id, leader_actor, reviewer_actor, run_seq, run_hash):
    """Authenticate either existing Qoder or native evidence from the same snapshot."""
    from .native_review_return_evidence import authenticate as authenticate_native

    args = {'task_id': task_id, 'leader_actor': leader_actor, 'reviewer_actor': reviewer_actor,
            'run_seq': run_seq, 'run_hash': run_hash}
    evidence = authenticate(events, **args)
    return evidence if evidence is not None else authenticate_native(events, **args)


def authenticate_history(team, task_id, candidate, *, events):
    """Validate the immutable candidate history, not current admission or answer authority."""
    try:
        return _authenticate_history(team, task_id, candidate, events=events)
    except (TypeError, ValueError, KeyError, AttributeError):
        return False


def _authenticate_history(team, task_id, candidate, *, events):
    if (candidate.task_id != task_id or candidate.actor != HOST or candidate.type != 'team.message_received'
            or candidate.payload.get('kind') != 'recovery' or candidate.payload.get('trigger') != TRIGGER
            or not any(event == candidate for event in events)):
        return False
    run_seq, run_hash = candidate.payload.get('original_run_seq'), candidate.payload.get('original_run_hash')
    if type(run_seq) is not int or run_seq < 1 or not isinstance(run_hash, str) or not run_hash:
        return False
    duplicates = [e for e in events if e.actor == HOST and e.type == 'team.message_received'
                  and e.payload.get('trigger') == TRIGGER and e.payload.get('original_run_seq') == run_seq]
    if len(duplicates) != 1 or duplicates[0] != candidate:
        return False
    prior = [e for e in events if e.seq < candidate.seq]
    start = next((e for e in prior if e.seq == run_seq), None)
    if start is None or start.hash != run_hash or start.actor != HOST or start.type != 'team.run_started':
        return False
    source = next((e for e in prior if e.seq == start.payload.get('message_seq')), None)
    if source is None or source.actor.kind != 'agent':
        return False
    try:
        leader = team.rt.organization.leader
        reviewer = team._role(source.actor.id)
    except (KeyError, ValueError):
        return False
    evidence = _review_evidence(prior, task_id=task_id, leader_actor=Actor.agent(leader.id, leader.role),
                                reviewer_actor=Actor.agent(reviewer.id, reviewer.role), run_seq=run_seq, run_hash=run_hash)
    scope = start.payload.get('recovery_scope')
    from .team_review_direction import direction_at, evidence_direction, report_customer_sources
    direction = evidence_direction(prior, evidence)
    if (evidence is None or type(scope) is not dict or type(scope.get('revision')) is not int
            or scope.get('revision') != start.payload.get('revision')
            or scope.get('control_state') != 'running' or scope.get('done') is not False
            or evidence['leader_finish']['seq'] >= candidate.seq
            or any(e.actor == Actor.human() and e.type == 'team.message_received' and e.seq > start.seq for e in prior)
            or any(e.payload.get('run_id') == start.payload.get('run_id') and
                   (e.type == 'team.tool_started' and e.actor == HOST
                    or e.type == 'msg.said' and e.payload.get('audience') == 'customer') for e in prior)):
        return False
    if direction is None or direction != direction_at(prior, start.seq) or direction != direction_at(prior, candidate.seq):
        return False
    candidate_scope = candidate.payload.get('scope')
    if type(candidate_scope) is dict and 'work_direction_seq' in candidate_scope:
        scope = {**scope, 'work_direction_seq': direction}
    inputs = report_customer_sources(team, task_id, source, before_seq=start.seq, events=prior)
    if inputs is None or inputs and 'customer_inputs' not in candidate.payload:
        return False
    return candidate.payload == _payload(leader.id, run_seq, run_hash, scope, evidence,
                                         inputs if 'customer_inputs' in candidate.payload else None)


def _candidate_payload(team, task_id, *, run_seq, run_hash, events):
    """Recompute historical evidence and current prerequisites in the caller transaction."""
    start = next((e for e in events if e.seq == run_seq), None)
    if start is None or start.hash != run_hash or start.actor != HOST or start.type != 'team.run_started':
        return None
    source = next((e for e in events if e.seq == start.payload.get('message_seq')), None)
    if source is None or source.actor.kind != 'agent':
        return None
    try:
        leader = team.rt.organization.leader
        reviewer = team._role(source.actor.id)
    except (KeyError, ValueError):
        return None
    evidence = _review_evidence(events, task_id=task_id, leader_actor=Actor.agent(leader.id, leader.role),
                                reviewer_actor=Actor.agent(reviewer.id, reviewer.role), run_seq=run_seq, run_hash=run_hash)
    if evidence is None:
        return None
    from .team_review_direction import current_matches, direction_at, evidence_direction, report_customer_sources
    direction = evidence_direction(events, evidence)
    if (direction is None or direction != direction_at(events, start.seq)
            or not current_matches(team, task_id, events, evidence)):
        return None
    inputs = report_customer_sources(team, task_id, source, before_seq=start.seq, events=events)
    if inputs is None:
        return None
    scope = team.recovery.scope(task_id, events=events)
    if (scope.get('control_state') != 'running' or scope.get('done') is not False
            or not _same_scope(start.payload.get('recovery_scope'), scope)
            or start.payload.get('revision') != scope.get('revision')
            or team.rt.conversation_list.is_removed(task_id)):
        return None
    if any(e.actor == Actor.human() and e.type == 'team.message_received' and e.seq > start.seq for e in events):
        return None
    # A text-only return is not delivery, and a prior customer reply or any
    # host tool action must not be silently reinterpreted as inaction.
    if any(e.payload.get('run_id') == start.payload.get('run_id') and
           (e.type == 'team.tool_started' and e.actor == HOST
            or e.type == 'msg.said' and e.payload.get('audience') == 'customer') for e in events):
        return None
    for active in (e for e in events if e.actor == HOST and e.type == 'team.run_started'):
        identity = tuple(active.payload.get(key) for key in ('run_id', 'role', 'message_seq'))
        if (not isinstance(identity[0], str) or not identity[0].strip()
                or not isinstance(identity[1], str) or not identity[1].strip()
                or type(identity[2]) is not int or identity[2] < 1):
            return None
        if not any(end.actor == HOST and end.type == 'team.run_finished' and end.seq > active.seq
                and end.payload.get('status') in {'completed', 'waiting', 'failed', 'interrupted'}
                and all(type(end.payload.get(key)) is type(value) and end.payload.get(key) == value
                        for key, value in zip(('run_id', 'role', 'message_seq'), identity))
                for end in events):
            return None
    binding = team.rt.workspaces.view(task_id)
    if (binding.get('available') and binding.get('root')
            and team.rt.workspaces.pending(root=binding['root'], effects_only=True)):
        return None
    if (team._pending_operations(task_id) or team.recovery.assignments(task_id)
            or team.view(task_id)['pending_count']
            or any(row['status'] == 'pending' for row in team.rt.questionnaires.view(task_id))):
        return None
    return _payload(leader.id, run_seq, run_hash, {**scope, 'work_direction_seq': direction}, evidence, inputs)


def validate(team, task_id, candidate, *, events):
    """Authenticate the persisted candidate and recheck current prerequisites."""
    if not authenticate_history(team, task_id, candidate, events=events):
        return False
    run_seq = candidate.payload.get('original_run_seq')
    run_hash = candidate.payload.get('original_run_hash')
    if type(run_seq) is not int or run_seq < 1 or not isinstance(run_hash, str) or not run_hash:
        return False
    matching = [event for event in events if event.actor == HOST and event.type == 'team.message_received'
                and event.payload.get('trigger') == TRIGGER and event.payload.get('original_run_seq') == run_seq]
    if len(matching) != 1 or matching[0] != candidate:
        return False
    payload = _candidate_payload(team, task_id, run_seq=run_seq, run_hash=run_hash, events=events)
    if payload is not None and type(candidate.payload.get('scope')) is dict and 'work_direction_seq' not in candidate.payload['scope']:
        # Older candidates did not persist this field; historical evidence is
        # still independently checked against the current direction above.
        payload = {**payload, 'scope': {k: v for k, v in payload['scope'].items() if k != 'work_direction_seq'}}
    if payload is not None and 'customer_inputs' not in candidate.payload and not payload.get('customer_inputs'):
        payload = {k: v for k, v in payload.items() if k != 'customer_inputs'}
    if payload is None or candidate.payload != payload:
        return False
    finish_seq = payload['evidence']['leader_finish']['seq']
    return finish_seq < candidate.seq


def create(team, task_id, *, run_seq, run_hash):
    """Atomically return the unique inert candidate, or None if unsafe.

    The caller must not hold an outer transaction. The existing log policy is
    checked inside the write transaction. A candidate is not recovery authority.
    """
    log = team.rt.log
    if log.db.in_transaction:
        raise WorkConflict('候选创建必须拥有独立写事务。')
    if type(run_seq) is not int or run_seq < 1 or not isinstance(run_hash, str) or not run_hash:
        return None
    with log.atomic():
        require_verified_log(log)
        events = log.read(task_id=task_id)
        payload = _candidate_payload(team, task_id, run_seq=run_seq, run_hash=run_hash, events=events)
        if payload is None:
            return None
        existing = [e for e in events if e.type == 'team.message_received' and e.actor == HOST
                    and e.payload.get('trigger') == TRIGGER and e.payload.get('original_run_seq') == run_seq]
        if existing:
            return existing[0] if len(existing) == 1 and validate(team, task_id, existing[0], events=events) else None
        return log.emit(type='team.message_received', task_id=task_id, actor=HOST, payload=payload)
