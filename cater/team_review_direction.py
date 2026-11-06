"""Derive work-direction bindings for formal review evidence."""
from bisect import bisect_right
import sqlite3

from .directions import changes_direction
from .events import Actor


def projection_events(log, task_id, *, before_seq=None, objectives=None):
    """Read only provenance events; never deserialize published artifact bodies."""
    import json
    from .events import Event
    types = ('task.created', 'human.commanded', 'human.decided', 'task.execution_controlled',
             'team.message_received', 'team.run_started', 'team.run_finished', 'team.review_requested',
             'team.review_recorded', 'team.tool_started', 'team.tool_finished', 'team.external_run_started',
             'team.external_run_finished', 'team.native_response_completed', 'team.model_called', 'msg.said')
    sql = 'SELECT * FROM events WHERE task_id=? AND type IN (' + ','.join('?' for _ in types) + ')'
    params = [task_id, *types]
    if before_seq is not None:
        sql += ' AND seq < ?'
        params.append(before_seq)
    events = [log._row_to_event(row) for row in log.db.execute(sql, params)]
    if objectives is None:
        objectives = log.iter_events(task_id=task_id, before_seq=before_seq,
                                     event_types=('team.objective_updated', 'team.objective_completed'))
    events.extend(objectives)
    sql = ("SELECT seq,id,ts,task_id,actor_json,type,category,prev_hash,hash,key_id,signature, "
           "json_extract(payload_json,'$.filename') AS filename, "
           "json_extract(payload_json,'$.content_sha256') AS content_sha256 FROM events "
           "WHERE task_id=? AND type='deliverable.published'")
    params = [task_id]
    if before_seq is not None:
        sql += ' AND seq < ?'
        params.append(before_seq)
    artifact_events = []
    try:
        for row in log.db.execute(sql, params):
            artifact_events.append(Event(seq=row['seq'], id=row['id'], ts=row['ts'], task_id=row['task_id'],
                actor=Actor(**json.loads(row['actor_json'])), type=row['type'], category=row['category'],
                payload={'filename': row['filename'], 'content_sha256': row['content_sha256']},
                prev_hash=row['prev_hash'], hash=row['hash'], key_id=row['key_id'], signature=row['signature']))
    except sqlite3.OperationalError as exc:
        if 'json_extract' not in str(exc).lower() or not any(
                text in str(exc).lower() for text in ('no such function', 'not authorized')):
            raise
        # JSON extensions are optional for ordinary collaboration. This path
        # is limited to the current task, never project metadata discovery.
        artifact_events = list(log.iter_events(task_id=task_id, before_seq=before_seq,
                                               event_types=('deliverable.published',)))
    events.extend(artifact_events)
    return sorted(events, key=lambda e: e.seq)


def _recorded_agent_source(source, events):
    """Require a host-paired tool receipt, not an agent label alone."""
    from .team import LOCAL_EVENT_CONTRACT
    from .team_reply import _recorded_arguments
    by_seq = {e.seq: e for e in events}
    for receipt in events:
        p = receipt.payload
        result = p.get('result')
        if (receipt.actor != Actor.system('team') or receipt.type != 'team.tool_finished'
                or p.get('name') not in {'delegate', 'send_message', 'return_work', 'record_review', 'continue_work'}
                or type(result) is not dict or result.get('status') != 'ok'
                or result.get('message_seq') != source.seq or p.get('role') != source.actor.id):
            continue
        start = by_seq.get(p.get('started_seq'))
        if (start is None or start.actor != Actor.system('team') or start.type != 'team.tool_started'
                or start.payload.get('execution_contract') != LOCAL_EVENT_CONTRACT
                or any(start.payload.get(k) != p.get(k) for k in ('name', 'role', 'run_id'))
                or not start.seq < source.seq < receipt.seq):
            continue
        args = _recorded_arguments(start.payload.get('arguments'))
        if type(args) is not dict:
            continue
        runs = [e for e in events if e.actor == Actor.system('team') and e.type == 'team.run_started'
                and all(e.payload.get(k) == start.payload.get(k) for k in ('run_id', 'role', 'message_seq'))]
        if (len(runs) != 1 or not runs[0].seq < start.seq
                or any(e.actor == Actor.system('team') and e.type == 'team.run_finished'
                       and all(e.payload.get(k) == runs[0].payload.get(k) for k in ('run_id', 'role', 'message_seq'))
                       and e.seq < receipt.seq for e in events)):
            continue
        if p['name'] == 'record_review':
            from .review_report_text import SUPPORTED_VERSIONS, review_report_text
            decision = by_seq.get(result.get('decision_seq'))
            if (source.payload.get('kind') != 'report' or result.get('review_seq') != source.payload.get('review_seq')
                    or decision is None or decision.actor != Actor.system('team') or decision.type != 'team.review_recorded'
                    or not start.seq < decision.seq < source.seq or decision.payload.get('reviewer') != source.actor.id
                    or decision.payload.get('review_seq') != result.get('review_seq')
                    or type(result.get('review_seq')) is not int
                    or result.get('verdict') not in ('approved', 'changes_requested')
                    or decision.payload.get('verdict') != result.get('verdict')
                    or args.get('review_seq') != result.get('review_seq') or args.get('verdict') != result.get('verdict')
                    or type(args.get('feedback')) is not str or args['feedback'].strip() != decision.payload.get('feedback')
                    or not any(source.payload.get('text') == review_report_text(result['verdict'], decision.payload['feedback'],
                                   result['review_seq'], version=version) for version in SUPPORTED_VERSIONS)):
                continue
        elif p['name'] == 'delegate':
            if (args.get('recipient') != source.payload.get('recipient') or type(args.get('instruction')) is not str
                    or args['instruction'].strip() != source.payload.get('text')):
                continue
        elif p['name'] == 'continue_work':
            parent = by_seq.get(args.get('assignment_seq'))
            if (parent is None or source.payload.get('kind') != 'continuation'
                    or source.payload.get('continue_assignment_seq') != parent.seq
                    or parent.actor != source.actor or parent.payload.get('recipient') != source.payload.get('recipient')
                    or type(args.get('instruction')) is not str
                    or args['instruction'].strip() != source.payload.get('text')):
                continue
        elif (type(args.get('text')) is not str or args['text'].strip() != source.payload.get('text')
              or p['name'] == 'send_message' and args.get('recipient') != source.payload.get('recipient')):
            continue
        return True
    return False


def _recovery_source(source, run, events):
    """Reconstruct a claimed host recovery from its earlier evidence."""
    from .team_recovery import RECOVERY_CONTRACT, _same_scope, recovery_message_valid
    p, r = source.payload, run.payload
    if (source.actor != Actor.system('team') or p.get('kind') != 'recovery'
            or p.get('recipient') != r.get('role') or r.get('recovery_contract') != RECOVERY_CONTRACT
            or type(r.get('recovery_contract', {}).get('version')) is not int
            or sum(e.actor == Actor.system('team') and e.type == 'team.run_started'
                   and e.seq <= run.seq and e.payload.get('message_seq') == source.seq for e in events) != 1
            or type(p.get('scope')) is not dict or not _same_scope(r.get('recovery_scope'), p['scope'])
            or r.get('revision') != p.get('revision')
            or r.get('control_seq') != p['scope'].get('control_seq')):
        return False
    before = [e for e in events if e.seq <= source.seq]
    if p.get('trigger') != 'review_return_candidate':
        try:
            if not recovery_message_valid(source, before, leader_id=r.get('role')):
                return False
            if p.get('trigger') != 'control_resume':
                by_seq = {e.seq: e for e in before}
                original = by_seq.get(p.get('recovery_of'))
                origin = by_seq.get(original.payload.get('message_seq')) if original else None
                if (origin is None or not (origin.actor == Actor.human() and origin.payload.get('kind') == 'message'
                        or origin.actor.kind == 'agent' and _recorded_agent_source(origin, before))):
                    return False
            return True
        except (TypeError, ValueError, KeyError, AttributeError):
            return False
    from .review_supervision_candidate import _review_evidence, _payload
    from .supervision_barrier import KIND
    by_seq = {e.seq: e for e in before}
    original = by_seq.get(p.get('original_run_seq'))
    if (original is None or original.hash != p.get('original_run_hash')
            or original.actor != Actor.system('team') or original.type != 'team.run_started'
            or original.payload.get('role') != r.get('role')
            or not _same_scope(r.get('recovery_scope'), p.get('scope'))
            or r.get('revision') != p.get('revision')
            or r.get('control_seq') != p.get('scope', {}).get('control_seq')
            or r.get('customer_input_barrier') != {'version': 1, 'kind': KIND,
                                                  'source_seq': source.seq, 'source_hash': source.hash}
            or sum(e.actor == Actor.system('team') and e.type == 'team.message_received'
                   and e.payload.get('trigger') == 'review_return_candidate'
                   and e.payload.get('original_run_seq') == p.get('original_run_seq') for e in before) != 1):
        return False
    report = by_seq.get(original.payload.get('message_seq'))
    assignment = by_seq.get(report.payload.get('reply_to')) if report else None
    if (report is None or report.actor.kind != 'agent' or assignment is None
            or assignment.actor.kind != 'agent' or assignment.actor.id != r.get('role')):
        return False
    try:
        prior = [e for e in before if e.seq < source.seq]
        evidence = _review_evidence(prior, task_id=source.task_id, leader_actor=assignment.actor,
                                    reviewer_actor=report.actor, run_seq=original.seq, run_hash=original.hash)
        direction = direction_at(prior, original.seq)
        scope = original.payload.get('recovery_scope')
        request = by_seq.get(evidence.get('review_request', {}).get('seq')) if evidence else None
        if (evidence is None or type(scope) is not dict or direction is None or request is None
                or direction_at(prior, request.seq) != direction or direction_at(prior, source.seq) != direction
                or scope.get('control_state') != 'running' or scope.get('done') is not False
                or scope.get('revision') != original.payload.get('revision')
                or any(e.actor == Actor.human() and e.type == 'team.message_received' and e.seq > original.seq for e in prior)
                or any(e.payload.get('run_id') == original.payload.get('run_id')
                       and (e.actor == Actor.system('team') and e.type == 'team.tool_started'
                            or e.type == 'msg.said' and e.payload.get('audience') == 'customer') for e in prior)):
            return False
        if 'work_direction_seq' in p.get('scope', {}):
            scope = {**scope, 'work_direction_seq': direction}
        inputs = p.get('customer_inputs')
        if inputs is not None and (type(inputs) is not list or any(type(x) is not dict or
                (entry := by_seq.get(x.get('seq'))) is None or entry.actor != Actor.human()
                or entry.type != 'team.message_received' or entry.hash != x.get('hash')
                or entry.payload.get('recipient') != x.get('recipient') for x in inputs)):
            return False
        intervening = [e.seq for e in prior if report.seq < e.seq < original.seq
                       and e.actor == Actor.human() and e.type == 'team.message_received']
        if intervening != [x['seq'] for x in (inputs or [])]:
            return False
        return p == _payload(r['role'], original.seq, original.hash, scope, evidence, inputs)
    except (TypeError, ValueError, KeyError, AttributeError):
        return False


def direction_at(events, through):
    """Mirror the work ledger's direction projection, rejecting untrusted actors."""
    directions = []
    for event in events:
        if event.seq > through:
            break
        if (event.type == "task.created"
                or event.type == "human.commanded" and changes_direction(event.payload)
                or event.type == "human.decided" and event.payload.get("resolution") == "rework"):
            if event.actor != Actor.human():
                return None
            directions.append(event.seq)
    return directions[-1] if directions else None


def objective_projection(events, fallback, *, effective_events=None):
    """Replay explicit direction and team objective events in commit order."""
    objective, revision, done = fallback, 0, False
    host = Actor.system("team")
    current_direction, effective_seq = None, None
    directions = []
    by_seq, runs = {}, {}
    for event in events:
        p = event.payload
        by_seq[event.seq] = event
        if event.actor == Actor.human():
            if event.type == "task.created":
                objective = p.get("goal", objective)
                current_direction = event.seq
                directions.append(event.seq)
            elif event.type == "human.commanded" and changes_direction(p):
                if p.get("goal"):
                    objective = str(p["goal"])
                done = False
                current_direction, effective_seq = event.seq, None
                directions.append(event.seq)
            elif event.type == "human.decided" and p.get("resolution") == "rework":
                done = False
                current_direction, effective_seq = event.seq, None
                directions.append(event.seq)
        elif event.actor == host and event.type == "team.run_started":
            if isinstance(p.get("run_id"), str) and p["run_id"]:
                source = by_seq.get(p.get("message_seq"))
                anchor = by_seq.get(current_direction)
                invalid_anchor = (('work_direction_seq' in p or 'work_direction_hash' in p)
                    and (type(p.get('work_direction_seq')) is not int or p['work_direction_seq'] != current_direction
                         or anchor is None or p.get('work_direction_hash') != anchor.hash))
                if (invalid_anchor or source is None or source.type != "team.message_received"
                        or source.payload.get("recipient") != p.get("role")):
                    run_direction = None
                elif source.actor == host and _recovery_source(source, event, events):
                    run_direction = direction_at(events, source.seq)
                elif source.actor == Actor.human() and source.payload.get('kind') == 'message':
                    run_direction = current_direction
                elif source.actor.kind == "agent":
                    if (source.payload.get('kind') not in {'assignment', 'message', 'report', 'continuation'}
                            or not _recorded_agent_source(source, [e for e in events if e.seq < event.seq])):
                        run_direction = None
                    else:
                        index = bisect_right(directions, source.seq)
                        run_direction = directions[index - 1] if index else None
                else:
                    run_direction = None
                runs.setdefault(p["run_id"], []).append(run_direction)
        elif event.actor == host and event.type in {"team.objective_updated", "team.objective_completed"}:
            candidates = runs.get(p.get("run_id"), []) if isinstance(p.get("run_id"), str) else []
            # Old records without a run can be replayed before any direction
            # switch; after a switch only a unique attributable run suffices.
            if (len(candidates) == 1 and candidates[0] != current_direction
                    or len(candidates) != 1 and len(directions) > 1):
                continue
            revision = event.seq
            if effective_events is not None:
                effective_events.add(event.seq)
            if event.type == "team.objective_updated":
                objective, done = p["goal"], False
            else:
                done = True
            effective_seq = event.seq
    return objective, revision, done, effective_seq


def effective_objectives(events):
    """Return objective event sequences effective at their commit time."""
    effective = set()
    objective_projection(events, "", effective_events=effective)
    return effective


def evidence_direction(events, evidence):
    ref = evidence.get("review_request") if isinstance(evidence, dict) else None
    if not isinstance(ref, dict) or type(ref.get("seq")) is not int:
        return None
    request = next((e for e in events if e.seq == ref["seq"] and e.hash == ref.get("hash")
                    and e.actor == Actor.system("team") and e.type == "team.review_requested"), None)
    return direction_at(events, request.seq) if request is not None else None


def current_matches(team, task_id, events, evidence):
    source = evidence_direction(events, evidence)
    current = direction_at(events, events[-1].seq) if events else None
    return (source is not None and source == current
            and team.rt.work.scope(task_id).get("direction_seq") == current)


def answered_observation(team, task_id, event, *, before_seq, events):
    """Prove a reply was settled, without interpreting the customer's words."""
    from .team_reply import committed_reply

    text = event.payload.get("text")
    if (event.actor != Actor.human() or event.type != "team.message_received"
            or event.payload.get("kind") != "message" or type(text) is not str
            or event.seq >= before_seq):
        return False
    runs = [e for e in events if e.actor == Actor.system("team") and e.type == "team.run_started"
            and e.payload.get("message_seq") == event.seq and e.payload.get("role") == event.payload.get("recipient")
            and event.seq < e.seq < before_seq]
    if len(runs) != 1:
        return False
    run = runs[0]
    endings = [e for e in events if e.actor == Actor.system("team") and e.type == "team.run_finished"
               and all(e.payload.get(k) == run.payload.get(k) for k in ("run_id", "role", "message_seq"))
               and run.seq < e.seq < before_seq]
    if len(endings) != 1 or endings[0].payload.get("status") != "completed":
        return False
    allowed = {"reply_to_customer", "read_messages", "read_artifact", "read_checkpoint", "read_receipts",
               "read_delivery", "read_handoff_sources", "list_skills", "read_skill"}
    if any(e.actor == Actor.system("team") and e.type == "team.tool_started"
           and e.payload.get("run_id") == run.payload.get("run_id") and e.payload.get("name") not in allowed
           for e in events):
        return False
    prior = [e for e in events if e.seq < endings[0].seq]
    if committed_reply(team, run, prior) is not None:
        return True
    # A tool-free direct answer is also an observed reply, but only with an
    # exact run/source binding and no client-tool actions in that run.
    if any(e.actor == Actor.system("team") and e.type == "team.tool_started"
           and e.payload.get("run_id") == run.payload.get("run_id") for e in prior):
        return False
    try:
        role = team._role(run.payload["role"])
    except (KeyError, ValueError):
        return False
    replies = [e for e in prior if e.type == "msg.said" and e.actor == Actor.agent(role.id, role.role)
               and e.seq > run.seq and e.payload.get("run_id") == run.payload.get("run_id")
               and e.payload.get("reply_to") == event.seq and e.payload.get("audience") == "customer"
               and e.payload.get("recipient") == "human" and e.payload.get("response_kind") == "reply"
               and type(e.payload.get("text")) is str and e.payload["text"].strip()]
    return len(replies) == 1


def report_customer_sources(team, task_id, report, *, before_seq, events):
    """Bind every intervening customer input; a receipt does not classify its meaning."""
    rows = []
    for event in events:
        if not report.seq < event.seq < before_seq or event.type != "team.message_received" or event.actor != Actor.human():
            continue
        if not answered_observation(team, task_id, event, before_seq=before_seq, events=events):
            return None
        rows.append({"seq": event.seq, "hash": event.hash, "recipient": event.payload.get("recipient")})
    return rows
