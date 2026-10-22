"""Freeze selected recorded invocations, not model-written reproduction commands."""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3

from .events import Actor
from .artifacts import safe_filename
from .team_read_sources import _unambiguous
from .workspace_archive import _paths, portable_path


HOST = Actor.system('team')
HASH = re.compile(r'[0-9a-f]{64}')
OUTCOMES = {'passed', 'failed', 'timeout', 'launch_error', 'effects_unknown', 'not_started'}
ENVIRONMENTS = {'temporary_artifact_copy_not_sandbox'}
REQUEST_KEYS = {'command', 'args', 'cwd', 'timeout', 'artifact_seqs', 'artifact_versions', 'stdin'}


class CommandRecordError(ValueError):
    """Public, bounded diagnostics for optional references."""


def schema():
    return {'type': 'array', 'maxItems': 4, 'items': {'type': 'integer', 'minimum': 1},
        'description': '可选：至多4份本群当前目标的workspace_run完成回执seq。用read_receipts读取真实seq，不用客户端call id。宿主补齐实际工具请求、原环境参数与结果；失败记录也可引用，不新增执行或验收条件。stdin正文和超长参数不会复制，实际记录与成员建议分开。'}


def _integer(value):
    return type(value) is int and 0 < value < 2 ** 63


def _text(value, limit=1024):
    try:
        return value if isinstance(value, str) and len(value.encode('utf-8')) <= limit else None
    except UnicodeError:
        return None


def _small(value, limit=1024):
    try:
        return value if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')) <= limit else None
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return None


def _request(arguments):
    if not isinstance(arguments, dict):
        return None, False, None
    stdin = None
    if isinstance(arguments.get('stdin'), str):
        try:
            body = arguments['stdin'].encode('utf-8')
            if len(body) <= 65536:
                stdin = {'bytes': len(body), 'sha256': hashlib.sha256(body).hexdigest()}
        except UnicodeError:
            pass
    valid = (isinstance(arguments.get('command'), str)
        and bool(arguments['command'].strip())
        and ('args' not in arguments or isinstance(arguments['args'], list)
             and len(arguments['args']) <= 128 and all(isinstance(v, str) for v in arguments['args']))
        and ('cwd' not in arguments or isinstance(arguments['cwd'], str))
        and ('timeout' not in arguments or type(arguments['timeout']) in (int, float)
             and 0 < arguments['timeout'] <= 86400 and math.isfinite(arguments['timeout']))
        and ('stdin' not in arguments or stdin is not None))
    if 'artifact_seqs' in arguments:
        seqs = arguments['artifact_seqs']
        valid = valid and isinstance(seqs, list) and 0 < len(seqs) <= 128 and all(_integer(v) for v in seqs)
    if 'artifact_versions' in arguments:
        rows = arguments['artifact_versions']
        valid = valid and 'artifact_seqs' not in arguments and isinstance(rows, list) and 0 < len(rows) <= 128 and all(
            isinstance(row, dict) and set(row) == {'artifact_seq', 'directory'} and _integer(row['artifact_seq'])
            and isinstance(row['directory'], str) and bool(row['directory']) for row in rows)
    request = _small({key: value for key, value in arguments.items() if key in REQUEST_KEYS - {'stdin'}}) if valid else None
    return request, request is not None and 'stdin' not in arguments and not set(arguments) - REQUEST_KEYS, stdin


def _output(value):
    if not isinstance(value, (str, dict)):
        return None
    source = value if isinstance(value, dict) else {'excerpt': value}
    result = {}
    for key in ('bytes', 'complete', 'truncated', 'utf8_valid', 'sha256'):
        item = source.get(key)
        if ((key == 'bytes' and type(item) is int and 0 <= item < 2 ** 63)
                or key in {'complete', 'truncated'} and type(item) is bool
                or key == 'utf8_valid' and key in source and (item is None or type(item) is bool)
                or key == 'sha256' and isinstance(item, str) and HASH.fullmatch(item)):
            result[key] = item
    excerpt = source.get('excerpt', source.get('preview'))
    if isinstance(excerpt, str):
        try:
            body = excerpt.encode('utf-8')
            result['excerpt'] = body[:512].decode('utf-8', errors='ignore')
            if len(body) > 512:
                result['truncated'] = True
        except UnicodeError:
            pass
    if source.get('preview_mode') == 'head_tail':
        # Only the saved head is copied; never relabel its suffix as a stream tail.
        result['truncated'] = True
    return result or None


def _artifacts(value):
    if not isinstance(value, list) or len(value) > 8:
        return None
    result = []
    for row in value:
        if (not isinstance(row, dict) or not _integer(row.get('seq')) or not _text(row.get('filename'), 512)
                or safe_filename(row['filename']) != row['filename']
                or not isinstance(row.get('content_sha256'), str) or not HASH.fullmatch(row['content_sha256'])
                or 'directory' in row and not _text(row['directory'], 256)):
            return None
        result.append({key: row[key] for key in ('seq', 'filename', 'content_sha256', 'directory') if key in row})
    directories = [row['directory'] for row in result if 'directory' in row]
    try:
        for directory in directories:
            if len(directory) > 64 or '/' in directory or '\\' in directory:
                return None
            portable_path(directory + '/__mount__.txt')
        _paths([directory + '/__mount__.txt' for directory in directories])
    except ValueError:
        return None
    return result if len({row['seq'] for row in result}) == len(result) else None


def _argv(value):
    return _small(value) if isinstance(value, list) and all(isinstance(v, str) for v in value) else None


def _event(team, task_id, seq, kind):
    row = team.rt.log.db.execute('SELECT * FROM events WHERE task_id = ? AND seq = ? AND type = ?',
                                (task_id, seq, kind)).fetchone()
    if row is None:
        raise CommandRecordError('引用不是本群有效的命令完成记录。')
    json.loads(row['payload_json'], object_pairs_hook=_unambiguous)
    json.loads(row['actor_json'], object_pairs_hook=_unambiguous)
    event = team.rt.log._row_to_event(row)
    if (event.actor != HOST or not isinstance(event.payload, dict)
            or not isinstance(event.hash, str) or not HASH.fullmatch(event.hash)):
        raise CommandRecordError('命令记录来源无法核对。')
    return event


def normalize(team, task_id, seqs, *, revision, artifacts):
    if (not isinstance(seqs, list) or len(seqs) > 4 or any(not _integer(seq) for seq in seqs)):
        raise CommandRecordError('command_receipts需要至多4个完成回执的正整数seq。')
    if type(revision) is not int or revision < 0:
        raise CommandRecordError('命令记录需要明确的当前目标版本。')
    records = []
    try:
        for seq in dict.fromkeys(seqs):
            event = _event(team, task_id, seq, 'team.tool_finished')
            meta = team.read_sources._chain(task_id, event)
            if (meta is None or meta.payload.get('name') != 'workspace_run'
                    or type(meta.payload.get('revision')) is not int or meta.payload['revision'] != revision):
                raise CommandRecordError('只可选择本群当前目标中配对有效的workspace_run完成回执。')
            start = _event(team, task_id, meta.seq, 'team.tool_started')
            s, p = start.payload, event.payload
            duplicates = [e for e in team.read_sources._metadata(task_id, kind='team.tool_finished',
                where="json_type(payload_json, '$.started_seq') = 'integer' AND json_extract(payload_json, '$.started_seq') = ?",
                parameters=(start.seq,)) if e.actor == HOST
                and all(e.payload.get(k) == s.get(k) for k in ('run_id', 'role', 'name'))
                and ('call_id' not in e.payload or e.payload['call_id'] == s['call_id'])]
            if len(duplicates) != 1:
                raise CommandRecordError('同一命令存在矛盾或重复完成记录，不能选为确切执行来源。')
            result = p['result']
            request, complete, stdin = _request(s.get('arguments'))
            argv = _argv(result.get('argv'))
            checked = _artifacts(result.get('checked_artifacts'))
            records.append({'version': 1, 'source_seq': event.seq, 'source_hash': event.hash,
                'started_seq': start.seq, 'started_hash': start.hash, 'role': s['role'], 'tool': 'workspace_run',
                'request': request, 'request_complete': complete, 'stdin': stdin, 'argv': argv,
                'authorized_root': _text(s.get('root')), 'cwd': _text(result.get('cwd')),
                'environment': result.get('environment') if isinstance(result.get('environment'), str)
                    and result['environment'] in ENVIRONMENTS else None,
                'status': result['status'], 'outcome': result.get('outcome') if isinstance(result.get('outcome'), str)
                    and result['outcome'] in OUTCOMES else None,
                'exit_code': result.get('exit_code') if type(result.get('exit_code')) is int
                    and -(2 ** 31) <= result['exit_code'] < 2 ** 32 else None,
                **{key: result[key] if type(result.get(key)) is bool else None for key in (
                    'complete', 'scope_current', 'artifacts_unchanged', 'artifact_check_valid', 'selected_versions_current')},
                'stdout': _output(result.get('stdout')), 'stderr': _output(result.get('stderr')),
                'checked_artifacts': checked,
                'delivery_artifact_seqs': [row['seq'] for row in checked or [] if any(
                    all(row[key] == ref[key] for key in ('seq', 'filename', 'content_sha256')) for ref in artifacts)]})
            if any(isinstance(result.get(key), dict) and result[key].get('complete') is False
                   for key in ('stdout', 'stderr')):
                records[-1]['complete'] = False
    except CommandRecordError:
        raise
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError, sqlite3.Error, OSError):
        raise CommandRecordError('无法完整核对所选命令记录，请读取真实回执后修正。') from None
    return records


def _fenced(value, language):
    length = max((len(match.group()) for match in re.finditer(r'`+', value)), default=0)
    fence = '`' * max(3, length + 1)
    return [fence + language, value, fence, '']


def _state(value, yes, no):
    return yes if value is True else no if value is False else '未记录或未确定'


def _delivery_refs(value):
    if not isinstance(value, list) or len(value) > 128:
        return None
    refs = {}
    for row in value:
        if (not isinstance(row, dict) or not _integer(row.get('seq')) or row['seq'] in refs
                or not isinstance(row.get('filename'), str) or safe_filename(row['filename']) != row['filename']
                or not isinstance(row.get('content_sha256'), str) or not HASH.fullmatch(row['content_sha256'])):
            return None
        refs[row['seq']] = row
    return refs


def markdown(records, *, artifacts=None):
    if not isinstance(records, list) or not records:
        return []
    lines = ['## 实际命令记录', '', '以下为选定历史回执的宿主记录，不是成员建议、完整覆盖或自动批准。不会因此重跑命令。', '']
    if len(records) > 4:
        return lines + ['命令引用记录超过范围，未将其作为确切执行事实展示。', '']
    labels = {'passed': '退出成功', 'failed': '执行失败', 'timeout': '超时', 'launch_error': '启动失败',
              'effects_unknown': '影响待核对', 'not_started': '未开始执行'}
    delivered = _delivery_refs(artifacts)
    for row in records:
        if (not isinstance(row, dict) or type(row.get('version')) is not int or row['version'] != 1
                or not _integer(row.get('source_seq')) or not _integer(row.get('started_seq'))
                or row['started_seq'] >= row['source_seq'] or row.get('tool') != 'workspace_run'
                or not all(isinstance(row.get(key), str) and HASH.fullmatch(row[key]) for key in ('source_hash', 'started_hash'))):
            lines += ['命令引用格式或来源缺失，未将其作为确切执行事实展示。', '']
            continue
        request, complete, _ = _request(row.get('request'))
        outcome = row.get('outcome')
        outcome = outcome if isinstance(outcome, str) else None
        code = row.get('exit_code') if type(row.get('exit_code')) is int else '未记录'
        cwd = _text(row.get('cwd'))
        lines += [f"### 命令回执 #{row['source_seq']}", '', f"记录结果：{labels.get(outcome, '未确定')}；退出码：{code}", '',
            '采集状态：' + _state(row.get('complete'), '记录完整', '未完整结束'), '',
            '任务/权限及确切源字节范围：' + _state(row.get('scope_current'), '回执记录时有效', '回执记录时已失效'), '',
            '执行副本：' + _state(row.get('artifacts_unchanged'), '原输入未改', '原输入已改'), '',
            '确切输入检查：' + _state(row.get('artifact_check_valid'), '记录为有效（不代表业务通过）', '记录为无效'), '',
            '所选版本：' + _state(row.get('selected_versions_current'), '记录时均为最新版', '记录时包含历史版'), '',
            '工具请求（Cater JSON，不是终端命令）：', '']
        lines += _fenced(json.dumps(request, ensure_ascii=False, indent=2), 'json')
        lines += ['请求完整。' if complete and row.get('request_complete') is True
            else '请求未完整包含；stdin正文或超长参数未复制，不能直接视为完整复现步骤。', '',
            '原环境实际程序参数（不承诺可在其他机器直接运行）：', '']
        lines += _fenced(json.dumps(_argv(row.get('argv')), ensure_ascii=False, indent=2), 'json')
        lines += ['工程授权/锁根（不是客户端目录或临时副本执行根）：', '']
        lines += _fenced(json.dumps(_text(row.get('authorized_root')), ensure_ascii=False), 'json')
        lines += ['回执记录的相对cwd（JSON字符串）：', '']
        lines += _fenced(json.dumps(cwd, ensure_ascii=False), 'json')
        lines += [
            '临时成果副本，非OS沙箱；相对cwd不指向上述工程授权根。' if row.get('environment') == 'temporary_artifact_copy_not_sandbox'
                else '执行根没有另行冻结；不依据客户端目录猜测。', '',
            f"来源事件哈希（记录值）：{row['source_hash']}", '']
        checked = _artifacts(row.get('checked_artifacts'))
        matched = row.get('delivery_artifact_seqs')
        matched = matched if isinstance(matched, list) and len(matched) <= 8 and all(_integer(v) for v in matched) else []
        if checked:
            lines += ['已检查版本（不自动继承批准）：', '']
            lines += _fenced(json.dumps(checked, ensure_ascii=False, indent=2), 'json')
            lines += ['原记录声明的关联序号（不单独确认名称或字节指纹相符）：', '']
            lines += _fenced(json.dumps(matched), 'json')
            confirmed = [ref['seq'] for ref in checked if ref['seq'] in matched and delivered is not None
                         and ref['seq'] in delivered and all(ref[key] == delivered[ref['seq']][key]
                             for key in ('seq', 'filename', 'content_sha256'))]
            lines += ['按本次交付记录核对，身份相符的已检查序号：' + (', '.join(map(str, confirmed))
                      if confirmed else '未确认；引用可能缺失或序号、名称、指纹不相符。'), '']
        else:
            lines += ['未保留完整的交付版本关联；普通工作目录命令不证明后来发布的ZIP字节已检查。', '']
        stdin = row.get('stdin')
        if (isinstance(stdin, dict) and type(stdin.get('bytes')) is int and 0 <= stdin['bytes'] <= 65536
                and isinstance(stdin.get('sha256'), str) and HASH.fullmatch(stdin['sha256'])):
            lines += [f"stdin配置：{stdin['bytes']} UTF-8字节，SHA-256 {stdin['sha256']}；不证明程序读取或执行。", '']
        for key in ('stdout', 'stderr'):
            output = _output(row.get(key))
            if isinstance(output, dict) and isinstance(output.get('excerpt'), str):
                lines += [f"{key} 已保存预览：", '']
                lines += _fenced(output['excerpt'], 'text')
                if output.get('truncated') is True:
                    lines += ['输出仅保留节选，不是连续全文或完整末尾。', '']
    return lines
