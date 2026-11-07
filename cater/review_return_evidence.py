"""Read-only evidence for a formal review return consumed by an external leader.

The caller supplies a trusted, complete task event snapshot. This does not
verify the event log's hash chain, establish reviewer process cleanup, authorize
recovery, or schedule any work. Future admission must check reviewer cleanup
and pending or unknown operations separately.
"""

from .events import Actor
from .external_runs import ExternalRuns
from .team import LOCAL_EVENT_CONTRACT, _assignment_report


HOST = Actor.system('team')


def _reviewer_input(events, by_seq, assignment, reviewer_run, leader_actor, revision):
    """Resolve one actual continuation to its original formal assignment."""
    from .team_reply import _recorded_arguments

    seq = reviewer_run.payload.get('message_seq')
    if type(seq) is not int:
        return None
    source = by_seq.get(seq)
    if source == assignment:
        return source
    if (source is None or source.type != 'team.message_received' or source.actor != leader_actor
            or source.payload.get('kind') != 'continuation'
            or type(source.payload.get('continue_assignment_seq')) is not int
            or source.payload['continue_assignment_seq'] != assignment.seq
            or source.payload.get('recipient') != assignment.payload.get('recipient')
            or type(source.payload.get('revision')) is not int or source.payload['revision'] != revision
            or source.payload.get('reply_to') is not None or source.payload.get('visible') is not True
            or not assignment.seq < source.seq < reviewer_run.seq):
        return None
    receipts = [e for e in events if e.actor == HOST and e.type == 'team.tool_finished'
                and e.payload.get('name') in {'continue_work', 'send_message'}
                and isinstance(e.payload.get('result'), dict)
                and type(e.payload['result'].get('message_seq')) is int
                and e.payload['result']['message_seq'] == source.seq
                and e.payload['result'].get('status') == 'ok']
    if len(receipts) != 1:
        return None
    receipt = receipts[0]
    tool = by_seq.get(receipt.payload.get('started_seq'))
    if (tool is None or tool.actor != HOST or tool.type != 'team.tool_started'
            or tool.payload.get('execution_contract') != LOCAL_EVENT_CONTRACT
            or type(tool.payload['execution_contract'].get('version')) is not int
            or tool.payload.get('role') != leader_actor.id
            or type(tool.payload.get('revision')) is not int or tool.payload['revision'] != revision
            or any(tool.payload.get(k) != receipt.payload.get(k) for k in ('name', 'role', 'run_id'))
            or not assignment.seq < tool.seq < source.seq < receipt.seq < reviewer_run.seq):
        return None
    send_runs = [e for e in events if e.actor == HOST and e.type == 'team.run_started'
                 and e.payload.get('run_id') == tool.payload.get('run_id')]
    if len(send_runs) != 1:
        return None
    send_run = send_runs[0]
    send_source = by_seq.get(send_run.payload.get('message_seq'))
    if (send_source is None or send_source.type != 'team.message_received'
            or send_source.payload.get('recipient') != leader_actor.id
            or type(send_run.payload.get('revision')) is not int or send_run.payload['revision'] != revision
            or any(send_run.payload.get(k) != tool.payload.get(k) for k in ('run_id', 'role', 'message_seq'))
            or not send_source.seq < send_run.seq < tool.seq):
        return None
    args = _recorded_arguments(tool.payload.get('arguments'))
    if not isinstance(args, dict):
        return None
    if tool.payload['name'] == 'continue_work':
        if (type(args.get('assignment_seq')) is not int or args['assignment_seq'] != assignment.seq
                or not isinstance(args.get('instruction'), str)
                or args['instruction'].strip() != source.payload.get('text')):
            return None
    elif (type(args.get('continue_assignment_seq')) is not int
          or args['continue_assignment_seq'] != assignment.seq
          or args.get('recipient') != source.payload['recipient'] or not isinstance(args.get('text'), str)
          or args['text'].strip() != source.payload.get('text')):
        return None
    return source


def authenticate(events, *, task_id, leader_actor, reviewer_actor, run_seq, run_hash):
    """Return references or None; caller provides trusted expected agent identities."""
    try:
        return _authenticate(events, task_id=task_id, leader_actor=leader_actor, reviewer_actor=reviewer_actor,
                             run_seq=run_seq, run_hash=run_hash)
    except (TypeError, ValueError, KeyError, AttributeError):
        # Malformed historical payloads are not evidence.
        return None


def _authenticate_review(events, *, task_id, leader_actor, reviewer_actor, run_seq, run_hash):
    """Common review/report chain; transport completion is checked by callers."""
    if (not isinstance(task_id, str) or not task_id or not isinstance(leader_actor, Actor)
            or not isinstance(reviewer_actor, Actor) or leader_actor.kind != 'agent' or reviewer_actor.kind != 'agent'
            or not leader_actor.id or not reviewer_actor.id or leader_actor.id == reviewer_actor.id
            or type(run_seq) is not int or run_seq <= 0
            or not isinstance(run_hash, str) or not run_hash):
        return None
    leader_id = leader_actor.id
    events = [e for e in events if e.task_id == task_id]
    by_seq = {e.seq: e for e in events}
    if len(by_seq) != len(events):
        return None
    start = by_seq.get(run_seq)
    if (start is None or start.actor != HOST or start.type != 'team.run_started'
            or start.hash != run_hash or start.payload.get('role') != leader_id
            or not isinstance(start.payload.get('run_id'), str) or not start.payload['run_id'].strip()
            or type(start.payload.get('message_seq')) is not int
            or type(start.payload.get('revision')) is not int):
        return None
    report = by_seq.get(start.payload['message_seq'])
    if (report is None or report.type != 'team.message_received' or report.actor != reviewer_actor
            or report.payload.get('kind') != 'report' or report.payload.get('recipient') != leader_id
            or type(report.payload.get('review_seq')) is not int
            or type(report.payload.get('reply_to')) is not int
            or type(report.payload.get('revision')) is not int
            or report.payload.get('revision') != start.payload['revision'] or report.seq >= start.seq):
        return None
    request = by_seq.get(report.payload['review_seq'])
    assignment = by_seq.get(report.payload.get('reply_to'))
    if (request is None or request.actor != HOST or request.type != 'team.review_requested'
            or request.payload.get('author') != leader_id or type(request.payload.get('revision')) is not int
            or request.payload['revision'] != start.payload['revision']
            or request.payload.get('root_seq') is not None and type(request.payload['root_seq']) is not int
            or assignment is None or assignment.type != 'team.message_received'
            or assignment.actor != leader_actor
            or type(assignment.payload.get('revision')) is not int
            or assignment.payload.get('revision') != start.payload['revision']
            or assignment.payload.get('recipient') != request.payload.get('reviewer')
            or type(assignment.payload.get('review_seq')) is not int
            or assignment.payload.get('review_seq') != request.seq
            or not _assignment_report(assignment, report)
            or not request.seq < assignment.seq < report.seq):
        return None
    decisions = [e for e in events if e.actor == HOST and e.type == 'team.review_recorded'
                 and e.payload.get('review_seq') == request.seq and e.seq < report.seq]
    if len(decisions) != 1:
        return None
    decision = decisions[0]
    if (decision.payload.get('reviewer') != report.actor.id
            or report.actor.id == leader_id
            or type(decision.payload.get('review_seq')) is not int
            or type(decision.payload.get('root_seq')) is not int
            or decision.payload.get('root_seq') != (request.payload.get('root_seq') or request.seq)
            or decision.payload.get('verdict') not in {'approved', 'changes_requested'}):
        return None
    feedback = decision.payload.get('feedback')
    if not isinstance(feedback, str) or not feedback.strip() or feedback != feedback.strip():
        return None
    from .review_report_text import SUPPORTED_VERSIONS, review_report_text

    if (report.payload.get('visible') is not True or not any(
            report.payload.get('text') == review_report_text(decision.payload['verdict'], feedback, request.seq, version=version)
            for version in SUPPORTED_VERSIONS)):
        return None
    receipts = [e for e in events if e.actor == HOST and e.type == 'team.tool_finished'
                and e.payload.get('name') == 'record_review' and isinstance(e.payload.get('result'), dict)
                and e.payload['result'].get('message_seq') == report.seq]
    if len(receipts) != 1:
        return None
    receipt = receipts[0]
    result = receipt.payload['result']
    tool = by_seq.get(receipt.payload.get('started_seq'))
    from .team_reply import _recorded_arguments
    args = _recorded_arguments(tool.payload.get('arguments')) if tool is not None else None
    reviewer_runs = [e for e in events if e.actor == HOST and e.type == 'team.run_started'
                     and e.payload.get('run_id') == decision.payload.get('run_id')]
    if len(reviewer_runs) != 1:
        return None
    reviewer_run = reviewer_runs[0]
    reviewer_input = _reviewer_input(events, by_seq, assignment, reviewer_run, leader_actor, start.payload['revision'])
    if (tool is None or tool.actor != HOST or tool.type != 'team.tool_started'
            or tool.payload.get('name') != 'record_review'
            or tool.payload.get('execution_contract') != LOCAL_EVENT_CONTRACT
            or type(tool.payload['execution_contract'].get('version')) is not int
            or reviewer_input is None
            or reviewer_run.payload.get('role') != report.actor.id
            or type(reviewer_run.payload.get('revision')) is not int
            or reviewer_run.payload['revision'] != start.payload['revision']
            or type(tool.payload.get('revision')) is not int
            or tool.payload.get('message_seq') != reviewer_input.seq or tool.payload.get('revision') != start.payload['revision']
            or any(tool.payload.get(k) != receipt.payload.get(k) for k in ('run_id', 'role', 'name'))
            or tool.payload.get('role') != report.actor.id
            or tool.payload.get('run_id') != reviewer_run.payload.get('run_id')
            or not isinstance(args, dict) or type(args.get('review_seq')) is not int
            or args['review_seq'] != request.seq or args.get('verdict') != decision.payload['verdict']
            or not isinstance(args.get('feedback'), str) or args['feedback'].strip() != decision.payload.get('feedback')
            or not assignment.seq <= reviewer_input.seq < reviewer_run.seq < tool.seq < decision.seq < report.seq < receipt.seq < start.seq
            or result.get('status') != 'ok' or type(result.get('review_seq')) is not int or result['review_seq'] != request.seq
            or type(result.get('decision_seq')) is not int or result['decision_seq'] != decision.seq
            or type(result.get('message_seq')) is not int or result['message_seq'] != report.seq
            or result.get('verdict') != decision.payload['verdict']):
        return None
    # Mirror the current-review projection: latest request for the root and
    # exact latest published content for every reviewed filename.
    root = request.payload.get('root_seq') or request.seq
    requests = [e for e in events if e.actor == HOST and e.type == 'team.review_requested'
                and (e.payload.get('root_seq') or e.seq) == root]
    from .team_review_direction import effective_objectives
    effective = effective_objectives(events)
    objectives = [e for e in events if e.seq in effective]
    current_revision = max((e.seq for e in objectives), default=0)
    if (not requests or max(e.seq for e in requests) != request.seq or current_revision != start.payload['revision']):
        return None
    artifacts = request.payload.get('artifacts')
    if not isinstance(artifacts, list) or not artifacts:
        return None
    for ref in artifacts:
        if not isinstance(ref, dict) or type(ref.get('seq')) is not int or not isinstance(ref.get('filename'), str) or not isinstance(ref.get('content_sha256'), str):
            return None
        versions = [e for e in events if e.type == 'deliverable.published' and e.payload.get('filename') == ref['filename']]
        artifact = by_seq.get(ref['seq'])
        if (artifact not in versions or artifact.seq >= request.seq or not versions
                or artifact.actor.kind != 'agent' or artifact.actor.id == report.actor.id
                or artifact.payload.get('content_sha256') != ref['content_sha256']
                or max(versions, key=lambda e: e.seq).payload.get('content_sha256') != ref['content_sha256']):
            return None
    refs = {name: {'seq': event.seq, 'hash': event.hash} for name, event in {
        'review_request': request, 'review_assignment': assignment, 'reviewer_run': reviewer_run,
        'reviewer_input': reviewer_input, 'review_tool_start': tool, 'review_decision': decision,
        'report': report, 'review_tool_finish': receipt, 'leader_run': start}.items()}
    return events, by_seq, start, refs


def _authenticate(events, *, task_id, leader_actor, reviewer_actor, run_seq, run_hash):
    chain = _authenticate_review(events, task_id=task_id, leader_actor=leader_actor,
                                 reviewer_actor=reviewer_actor, run_seq=run_seq, run_hash=run_hash)
    if chain is None:
        return None
    events, by_seq, start, refs = chain
    externals = [e for e in events if e.actor == HOST and e.type == 'team.external_run_started'
                 and e.payload.get('run_started_seq') == start.seq]
    if len(externals) != 1:
        return None
    external = externals[0]
    # Existing receipt validators are pure and do not need a runtime instance.
    validator = object.__new__(ExternalRuns)
    if not validator._start_valid(external, by_seq):
        return None
    exits = [e for e in events if validator._finished(e, external)]
    finishes = [e for e in events if e.actor == HOST and e.type == 'team.run_finished'
                and all(e.payload.get(k) == start.payload.get(k) for k in ('run_id', 'role', 'message_seq'))]
    if (len(exits) != 1 or len(finishes) != 1):
        return None
    end, finish = exits[0], finishes[0]
    # Versioned Job observations are diagnostics, never additional proof of
    # tree-wide cleanup; malformed or explicitly unconfirmed facts cannot pass.
    if 'job_cleanup_observation' in end.payload:
        from .external_runs import _job_cleanup_observation
        observation = end.payload['job_cleanup_observation']
        if not _job_cleanup_observation(observation) or observation['status'] == 'unconfirmed':
            return None
    if (not external.seq < end.seq < finish.seq or end.payload.get('prompt_attempted') is not True
            or end.payload.get('cleanup_complete') is not True or end.payload.get('outcome') != 'returned'
            or end.payload.get('stop_reason') != 'end_turn' or finish.payload.get('status') != 'completed'):
        return None
    return {**refs, **{name: {'seq': event.seq, 'hash': event.hash} for name, event in {
        'external_start': external, 'external_finish': end, 'leader_finish': finish}.items()}}
