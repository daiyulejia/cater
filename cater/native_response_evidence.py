"""Historical evidence for one response and local worker/pipe cleanup only.

It proves neither the whole run's completion nor that every descendant or
remote operation has stopped. It does not authenticate a formal review,
establish current scope, or authorize supervision. Any future consumer must
independently authenticate the complete history and, in the admission write
transaction, recheck current scope, any-role customer inputs since the original
run, active work and unknown effects before creating or claiming a candidate.
"""
from .events import Actor

HOST = Actor.system('team')
CONTRACT = {'version': 1, 'kind': 'native_response', 'cleanup': 'worker_and_pipes'}


def trusted_protocol(driver):
    """Only unwrapped native adapters and the original cooperative transport qualify."""
    from . import anthropic, drivers, native_transport
    if native_transport.iter_request is not native_transport.NATIVE_ITER_REQUEST:
        return None
    if type(driver) not in {drivers.OpenAICompatibleDriver, anthropic.AnthropicMessagesDriver}:
        return None
    # Even assigning the original bound method on the instance makes both
    # adapters choose their synchronous fallback instead of the owned worker.
    if 'tool_turn' in vars(driver):
        return None
    if type(driver) is drivers.OpenAICompatibleDriver:
        if not drivers.native_openai_usage(driver, tool=True) or drivers._open_api_request is not drivers._DEFAULT_API_OPENER:
            return None
        return 'openai_compatible'
    if type(driver) is anthropic.AnthropicMessagesDriver:
        methods = (('tool_turn', anthropic._ANTHROPIC_TOOL_TURN),
                   ('iter_tool_turn', anthropic._ANTHROPIC_ITER_TOOL_TURN),
                   ('_tool_input', anthropic._ANTHROPIC_TOOL_INPUT),
                   ('_tool_result', anthropic._ANTHROPIC_TOOL_RESULT))
        if (anthropic._open_api_request is not anthropic._DEFAULT_API_OPENER
                or any(not drivers.trusted_driver_method(driver, name, method) for name, method in methods)):
            return None
        return 'anthropic'
    return None


def record(team, task_id, message, role, started, model_call, scope, basis, turn, protocol):
    """Record a clean no-tool response; caller holds a write transaction and rechecks scope.

    The source is bound as a report but is not authenticated as a formal review.
    Future scheduling must independently authenticate its complete history.
    """
    from .tool_protocol import ToolTurn
    meta = turn.meta if type(turn) is ToolTurn else None
    if (protocol not in {'openai_compatible', 'anthropic'} or type(meta) is not dict
            or meta.get('native_cleanup_confirmed') is not True or meta.get('native_cleanup_failed') is True
            or meta.get('incomplete') is not False or meta.get('contained') or turn.calls
            or (meta.get('finish_reason') != 'stop' if protocol == 'openai_compatible'
                else meta.get('stop_reason') != 'end_turn')
            or role.id != team.rt.organization.leader.id or message.actor.kind != 'agent'
            or message.payload.get('kind') != 'report' or type(message.payload.get('review_seq')) is not int
            or message.payload.get('recipient') != role.id or type(scope) is not dict
            or scope.get('revision') != started.payload.get('revision')
            or model_call.payload.get('context_basis') != basis):
        return None
    events = team.rt.log.read(task_id=task_id)
    if not all(any(e == item for e in events) for item in (message, started, model_call)):
        return None
    if (started.actor != HOST or started.type != 'team.run_started' or model_call.actor != HOST
            or model_call.type != 'team.model_called' or started.payload.get('message_seq') != message.seq
            or started.payload.get('role') != role.id or model_call.payload.get('run_id') != started.payload.get('run_id')
            or model_call.payload.get('role') != role.id or not message.seq < started.seq < model_call.seq):
        return None
    return team.rt.log.emit(type='team.native_response_completed', task_id=task_id, actor=HOST, payload={
        'contract': dict(CONTRACT), 'protocol': protocol, 'report_seq': message.seq, 'report_hash': message.hash,
        'run_seq': started.seq, 'run_hash': started.hash, 'run_id': started.payload['run_id'],
        'model_call_seq': model_call.seq, 'model_call_hash': model_call.hash,
        'scope': dict(scope), 'context_basis': dict(basis), 'response': 'normal_no_tools',
        'local_cleanup_confirmed': True})


def validate(events, *, task_id, receipt):
    """Check historical references only, never grant supervision or current-scope authority."""
    try:
        events = [e for e in events if e.task_id == task_id]
        by_seq = {e.seq: e for e in events}
        if len(by_seq) != len(events) or by_seq.get(receipt.seq) != receipt or receipt.actor != HOST or receipt.type != 'team.native_response_completed':
            return False
        p = receipt.payload
        if (p.get('contract') != CONTRACT or type(p.get('contract', {}).get('version')) is not int
                or p.get('protocol') not in {'openai_compatible', 'anthropic'}
                or p.get('response') != 'normal_no_tools' or p.get('local_cleanup_confirmed') is not True
                or type(p.get('scope')) is not dict or type(p.get('context_basis')) is not dict
                or any(type(p.get(k)) is not int or p[k] < 1 for k in ('report_seq', 'run_seq', 'model_call_seq'))
                or any(not isinstance(p.get(k), str) or not p[k] for k in ('report_hash', 'run_hash', 'model_call_hash', 'run_id'))):
            return False
        report, run, call = (by_seq.get(p.get(k)) for k in ('report_seq', 'run_seq', 'model_call_seq'))
        if report is None or run is None or call is None:
            return False
        return bool(report.type == 'team.message_received' and report.actor.kind == 'agent'
            and report.payload.get('kind') == 'report' and type(report.payload.get('review_seq')) is int
            and report.hash == p.get('report_hash') and run.actor == HOST and run.type == 'team.run_started'
            and run.hash == p.get('run_hash') and run.payload.get('message_seq') == report.seq
            and report.payload.get('recipient') == run.payload.get('role')
            and run.payload.get('run_id') == p.get('run_id') and type(run.payload.get('revision')) is int
            and report.payload.get('revision') == run.payload.get('revision')
            and run.payload.get('revision') == p['scope'].get('revision')
            and run.payload.get('control_seq') == p['scope'].get('control_seq')
            and call.actor == HOST and call.type == 'team.model_called' and call.hash == p.get('model_call_hash')
            and call.payload.get('run_id') == p.get('run_id') and call.payload.get('role') == run.payload.get('role')
            and call.payload.get('context_basis') == p['context_basis']
            and report.seq < run.seq < call.seq < receipt.seq)
    except (AttributeError, KeyError, TypeError, ValueError):
        return False
