"""Explicit namespaced copies of exact published versions, never workspace reads."""
from __future__ import annotations

import sqlite3

from .archive_members import read_members
from .workspace_archive import _paths, portable_path


MAX_FILES = 128
MAX_MEMBER_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024


def schema():
    return {"type": "array", "minItems": 1, "maxItems": MAX_FILES,
        "description": "可选：与artifact_seqs互斥。按本群确切成果序号并列检查，可选历史同名版本；directory是各自独立的单层目录，例如before与after。原文件路径在该目录内保持不变，不继承旧批准或执行权限。",
        "items": {"type": "object", "additionalProperties": False,
            "required": ["artifact_seq", "directory"], "properties": {
                "artifact_seq": {"type": "integer", "minimum": 1},
                "directory": {"type": "string", "minLength": 1, "maxLength": 64,
                    "description": "独立的单层便携目录，不含斜杠或点路径；与其他目录不可存在大小写或Unicode等价冲突。"}}}}


def references(team, task_id, selections):
    """Validate selection/mount identity without opening archive blobs."""
    if not isinstance(selections, list) or not 1 <= len(selections) <= MAX_FILES:
        raise ValueError("请选择一至128个明确版本及其挂载目录。")
    seqs, directories = [], []
    for row in selections:
        if (not isinstance(row, dict) or set(row) != {'artifact_seq', 'directory'}
                or type(row['artifact_seq']) is not int or row['artifact_seq'] < 1
                or not isinstance(row['directory'], str) or not 1 <= len(row['directory']) <= 64
                or '/' in row['directory'] or '\\' in row['directory']):
            raise ValueError("每个版本需要正整数artifact_seq和单层directory。")
        portable_path(row['directory'] + '/__mount__.txt')
        seqs.append(row['artifact_seq'])
        directories.append(row['directory'])
    if len(set(seqs)) != len(seqs):
        raise ValueError("同一成果序号不能重复选择。")
    _paths([directory + '/__mount__.txt' for directory in directories])
    sources = team.browser._files(task_id, seqs, historical=True)
    return [{**source, 'directory': directory} for source, directory in zip(sources, directories)]


def inputs(team, task_id, selections):
    sources = references(team, task_id, selections)
    layout = []
    for source in sources:
        entries = (source['archive']['files'] if source.get('format') == 'archive' else [{
            'path': source['filename'], 'bytes': len(source['content'].encode('utf-8')),
            'sha256': source['content_sha256'], 'executable': False}])
        for entry in entries:
            layout.append({'artifact_seq': source['seq'], **entry,
                'source_path': entry['path'], 'path': source['directory'] + '/' + entry['path']})
    if (len(layout) > MAX_FILES or sum(f['bytes'] for f in layout) > MAX_TOTAL_BYTES
            or any(f['bytes'] > MAX_MEMBER_BYTES for f in layout)):
        raise ValueError("版本副本总计限128文件、单文件16MiB、总32MiB。")
    _paths([f['path'] for f in layout])
    files = []
    for source in sources:
        if source.get('format') == 'archive':
            entries = read_members(team.rt.log, source, max_files=MAX_FILES,
                max_member_bytes=MAX_MEMBER_BYTES, max_total_bytes=MAX_TOTAL_BYTES)
        else:
            body = source['content'].encode('utf-8')
            entries = [{'path': source['filename'], 'body': body, 'bytes': len(body),
                        'sha256': source['content_sha256'], 'executable': False}]
        for entry in entries:
            files.append({'artifact_seq': source['seq'], **entry, 'sha256': entry['sha256'].lower(),
                'source_path': entry['path'], 'path': source['directory'] + '/' + entry['path']})
    return sources, files


def checked_artifacts(team, task_id, sources):
    """Latest publication is a separate observation, not a permission or approval."""
    members = {r.id for r in [team.rt.organization.leader, *team.rt.roles]}
    try:
        latest = {e.payload['filename']: e.seq for e in team.rt.log.read(task_id=task_id)
            if e.type == 'deliverable.published' and e.actor.kind == 'agent' and e.actor.id in members
            and isinstance(e.payload.get('filename'), str)}
    except (OSError, sqlite3.Error, ValueError):
        # A failed metadata observation must not replace the actual process outcome.
        latest = {}
    return [{**{key: source[key] for key in ('seq', 'filename', 'content_sha256', 'directory')},
        'latest_seq_at_check': latest.get(source['filename']),
        'is_latest_at_check': (latest[source['filename']] == source['seq']
                              if source['filename'] in latest else None)} for source in sources]
