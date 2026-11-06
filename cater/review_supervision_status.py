"""Read-only, explicitly requested intent diagnostics; never execution authority."""

import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from .events import Actor
from .directions import changes_direction
from .review_supervision_intent import CONTRACT, HOST, TYPE


def _digest(value):
    return type(value) is str and re.fullmatch(r'[0-9a-f]{64}', value) is not None


def _intent_structure(payload, start, events):
    """Check recorded shapes and references only; never authenticate history."""
    scope, evidence, inputs = payload.get('scope'), payload.get('evidence'), payload.get('customer_inputs')
    protocol = payload.get('protocol')
    scope_keys = {'revision', 'control_seq', 'control_state', 'workspace_revision', 'workspace_fingerprint',
                  'workspace_available', 'execution_fingerprint', 'rule_fingerprint', 'done', 'work_direction_seq'}
    if (type(scope) is not dict or set(scope) != scope_keys
            or type(scope.get('revision')) is not int or scope['revision'] < 0
            or scope.get('control_state') != 'running' or scope.get('done') is not False
            or type(scope.get('workspace_available')) is not bool
            or type(scope.get('work_direction_seq')) is not int or scope['work_direction_seq'] < 1
            or any(scope[k] is not None and (type(scope[k]) is not int or scope[k] < 1)
                   for k in ('control_seq', 'workspace_revision'))
            or any(scope[k] is not None and not _digest(scope[k])
                   for k in ('workspace_fingerprint', 'execution_fingerprint', 'rule_fingerprint'))
            or {k: v for k, v in scope.items() if k != 'work_direction_seq'} != start['payload'].get('recovery_scope')
            or scope['revision'] != start['payload'].get('revision')
            or protocol not in ('qoder_acp', 'openai_compatible', 'anthropic')
            or type(evidence) is not dict or type(inputs) is not list):
        return False
    required = {'review_request': 'team.review_requested', 'review_assignment': 'team.message_received',
                'reviewer_run': 'team.run_started', 'reviewer_input': 'team.message_received',
                'review_tool_start': 'team.tool_started', 'review_decision': 'team.review_recorded',
                'report': 'team.message_received', 'review_tool_finish': 'team.tool_finished',
                'leader_run': 'team.run_started', 'leader_finish': 'team.run_finished'}
    required.update({'external_start': 'team.external_run_started', 'external_finish': 'team.external_run_finished'}
                    if protocol == 'qoder_acp' else
                    {'model_call': 'team.model_called', 'native_response': 'team.native_response_completed'})
    if set(evidence) != set(required):
        return False
    for key, event_type in required.items():
        ref = evidence[key]
        if (type(ref) is not dict or set(ref) != {'seq', 'hash'} or type(ref['seq']) is not int
                or not _digest(ref['hash'])):
            return False
        event = events.get(ref['seq'])
        if event is None or event['type'] != event_type or event['hash'] != ref['hash']:
            return False
    for ref in inputs:
        if (type(ref) is not dict or set(ref) != {'seq', 'hash', 'recipient'}
                or type(ref['seq']) is not int or not _digest(ref['hash'])):
            return False
        source = events.get(ref['seq'])
        if (source is None or source['type'] != 'team.message_received'
                or source['actor'] != Actor.human().to_dict() or source['hash'] != ref['hash']
                or source['payload'].get('recipient') != ref['recipient']):
            return False
    return True


def inspect_task(database, task_id):
    """Read one task in one SQLite snapshot, without opening a runtime or writing.

    This is a structural projection of recorded facts, not log verification,
    candidate authentication, proof of delivery or permission to resume.
    """
    if type(task_id) is not str or not task_id.strip():
        raise ValueError('task_id is required')
    uri = Path(database).resolve().as_uri() + '?mode=ro'
    with closing(sqlite3.connect(uri, uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN')
        rows = db.execute('SELECT seq, task_id, actor_json, type, payload_json, hash FROM events '
                          'WHERE task_id = ? ORDER BY seq', (task_id,)).fetchall()
    events = []
    for row in rows:
        actor = json.loads(row['actor_json'])
        payload = json.loads(row['payload_json'])
        if type(actor) is not dict or type(payload) is not dict:
            raise ValueError('invalid event record')
        events.append({'seq': row['seq'], 'task_id': row['task_id'], 'actor': actor,
                       'type': row['type'], 'payload': payload, 'hash': row['hash']})
    if not any(e['type'] == 'task.created' and e['actor'] == Actor.human().to_dict() for e in events):
        raise ValueError('task not found or task record invalid')
    by_seq = {e['seq']: e for e in events}
    intents = [e for e in events if e['type'] == TYPE]
    results = []
    for event in intents:
        p = event['payload']
        # The database is not authenticated here; even a digest-shaped value
        # can be forged and must not be echoed as if it were a trusted hash.
        row = {'intent_seq': event['seq'], 'intent_hash': None,
               'state': 'record_unverified',
               'later_customer_input': any(e['seq'] > event['seq'] and e['type'] == 'team.message_received'
                   and e['actor'] == Actor.human().to_dict() for e in events),
               'later_control_change': any(e['seq'] > event['seq'] and e['type'] == 'task.execution_controlled'
                   and e['actor'] == Actor.human().to_dict() for e in events),
               'later_direction_change': any(e['seq'] > event['seq'] and e['actor'] == Actor.human().to_dict()
                   and (e['type'] == 'human.commanded' and changes_direction(e['payload'])
                        or e['type'] == 'human.decided' and e['payload'].get('resolution') == 'rework')
                   for e in events)}
        start = by_seq.get(p.get('run_seq')) if type(p.get('run_seq')) is int else None
        finish = by_seq.get(p.get('finish_seq')) if type(p.get('finish_seq')) is int else None
        report = by_seq.get(p.get('report_seq')) if type(p.get('report_seq')) is int else None
        evidence = p.get('evidence')
        valid = (_digest(event['hash']) and event['actor'] == HOST.to_dict() and p.get('contract') == CONTRACT
                 and type(p.get('contract', {}).get('version')) is int
                 and start is not None and finish is not None and report is not None
                 and _intent_structure(p, start, by_seq)
                 and start['type'] == 'team.run_started' and start['actor'] == HOST.to_dict()
                 and finish['type'] == 'team.run_finished' and finish['actor'] == HOST.to_dict()
                 and finish['payload'].get('status') == 'completed'
                 and report['type'] == 'team.message_received' and report['actor'].get('kind') == 'agent'
                 and report['payload'].get('kind') == 'report' and type(report['payload'].get('review_seq')) is int
                 and report['seq'] < start['seq'] < finish['seq'] < event['seq']
                 and start['payload'].get('message_seq') == report['seq']
                 and all(finish['payload'].get(k) == start['payload'].get(k) for k in ('run_id', 'role', 'message_seq'))
                 and all(p.get(k + '_hash') == item['hash'] for k, item in
                         (('run', start), ('finish', finish), ('report', report)))
                 and type(evidence) is dict
                 and all(evidence.get(k) == {'seq': item['seq'], 'hash': item['hash']}
                         for k, item in (('leader_run', start), ('leader_finish', finish), ('report', report)))
                 and sum(e['type'] == TYPE and e['actor'] == HOST.to_dict()
                         and e['payload'].get('run_seq') == start['seq'] for e in intents) == 1)
        if not valid:
            results.append(row)
            continue
        candidates = [e for e in events if e['type'] == 'team.message_received'
                      and e['payload'].get('trigger') == 'review_return_candidate'
                      and e['payload'].get('original_run_seq') == start['seq']]
        if not candidates:
            row['state'] = 'awaiting_continuation'
        elif len(candidates) == 1:
            candidate = candidates[0]
            cp = candidate['payload']
            if (_digest(candidate['hash']) and candidate['seq'] > event['seq'] and candidate['actor'] == HOST.to_dict()
                    and cp.get('original_run_hash') == start['hash'] and cp.get('evidence') == evidence
                    and cp.get('scope') == p.get('scope') and cp.get('customer_inputs') == p.get('customer_inputs')
                    and cp.get('kind') == 'recovery' and cp.get('recipient') == start['payload'].get('role')):
                row['candidate_seq'] = candidate['seq']
                starts = [e for e in events if e['type'] == 'team.run_started'
                          and e['payload'].get('message_seq') == candidate['seq']]
                if not starts:
                    row['state'] = 'candidate_unclaimed'
                elif len(starts) == 1:
                    claimed = starts[0]
                    sp = claimed['payload']
                    barrier = {'version': 1, 'kind': 'all_customer_messages', 'source_seq': candidate['seq'],
                               'source_hash': candidate['hash']}
                    if (claimed['seq'] > candidate['seq'] and claimed['actor'] == HOST.to_dict()
                            and sp.get('role') == cp.get('recipient') and sp.get('customer_input_barrier') == barrier
                            and type(sp.get('customer_input_barrier', {}).get('version')) is int
                            and type(sp.get('run_id')) is str and sp['run_id']):
                        row['supervision_run_seq'] = claimed['seq']
                        ends = [e for e in events if e['seq'] > claimed['seq'] and e['type'] == 'team.run_finished'
                                and e['actor'] == HOST.to_dict() and all(e['payload'].get(k) == sp.get(k)
                                    for k in ('run_id', 'role', 'message_seq'))]
                        if not ends:
                            row['state'] = 'run_started_no_finish_record'
                        elif len(ends) == 1 and ends[0]['payload'].get('status') in {'completed', 'waiting', 'interrupted', 'failed'}:
                            row['state'] = 'run_finished_recorded'
                            row['run_status'] = ends[0]['payload']['status']
        results.append(row)
    return {'task_id': task_id, 'intents': results,
            'note': ('只读事件投影；awaiting_continuation表示未见绑定候选，candidate_unclaimed表示候选尚无领取记录；'
                     'run_started_no_finish_record不证明进程仍在运行；run_finished_recorded仅是监督轮的记录，不代表任务完成。'
                     'record_unverified表示相关记录无法核对。仅检查结构，未验证日志、哈希真实性或授权，'
                     '不证明任务交付或会自动恢复，也不会启动或重放任何操作。')}
