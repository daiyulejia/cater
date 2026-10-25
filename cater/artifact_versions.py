"""Explicit namespaced copies of exact published versions, never workspace reads."""
from __future__ import annotations

import sqlite3

from .archive_members import read_members
from .workspace_archive import ArchiveError, _paths, portable_path


MAX_FILES = 128
MAX_MEMBER_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024

_INPUT_ERRORS = {
    "invalid_version_selection": ("请选择一至128个明确版本及其挂载目录。", ("artifact_versions",)),
    "invalid_version_entry": ("每个版本需要正整数artifact_seq和单层directory。",
                              ("artifact_versions[].artifact_seq", "artifact_versions[].directory")),
    "invalid_version_directory": ("directory需要单层便携目录，不能是受保护名称、点路径或非法路径。",
                                  ("artifact_versions[].directory",)),
    "duplicate_artifact_sequence": ("同一成果序号不能重复选择。", ("artifact_versions[].artifact_seq",)),
    "version_directories_conflict": (
        "各版本的directory重复或存在大小写、Unicode等价冲突。比较不同版本请使用各自独立的目录；"
        "若只检查当前多个已发布文件，可改用artifact_seqs一起选择。", ("artifact_versions[].directory",)),
    "artifact_selectors_conflict": ("artifact_versions与artifact_seqs不能同时提供。"
        "检查当前多个文件选artifact_seqs；历史版本对照选artifact_versions并为各版本指定独立directory。",
        ("artifact_versions", "artifact_seqs")),
    "artifact_version_budget": ("版本副本总计限128文件、单文件16MiB、总32MiB。", ("artifact_versions",)),
    "invalid_version_paths": ("挂载后的完整成员路径无效或冲突；请核对所选版本的文件清单及目录层级，"
                              "不要用节选或未发布文件替代。", ("artifact_versions",)),
}


class VersionInputError(ValueError):
    """Fixed input diagnostics; exception text and user values never reach replies."""

    def __init__(self, reason):
        self.reason = reason
        super().__init__(_INPUT_ERRORS[reason][0])

    def result(self):
        message, fields = _INPUT_ERRORS[self.reason]
        return {"status": "error", "code": "invalid_artifact_check", "complete": True,
                "outcome": "not_started", "reason": self.reason, "fields": list(fields), "message": message}


def schema():
    return {"type": "array", "minItems": 1, "maxItems": MAX_FILES,
        "description": "可选：与artifact_seqs互斥。按本群确切成果序号并列检查，可选历史同名版本；directory是各自独立的单层目录，例如before与after。检查当前多个已发布文件可用artifact_seqs放入同一副本，不把多个版本共用directory。原文件路径在该目录内保持不变，不继承旧批准或执行权限。",
        "items": {"type": "object", "additionalProperties": False,
            "required": ["artifact_seq", "directory"], "properties": {
                "artifact_seq": {"type": "integer", "minimum": 1},
                "directory": {"type": "string", "minLength": 1, "maxLength": 64,
                    "description": "独立的单层便携目录，不含斜杠或点路径；与其他目录不可存在大小写或Unicode等价冲突。"}}}}


def references(team, task_id, selections):
    """Validate selection/mount identity without opening archive blobs."""
    if not isinstance(selections, list) or not 1 <= len(selections) <= MAX_FILES:
        raise VersionInputError("invalid_version_selection")
    seqs, directories = [], []
    for row in selections:
        if (not isinstance(row, dict) or set(row) != {'artifact_seq', 'directory'}
                or type(row['artifact_seq']) is not int or row['artifact_seq'] < 1
                or not isinstance(row['directory'], str) or not 1 <= len(row['directory']) <= 64
                or '/' in row['directory'] or '\\' in row['directory']):
            raise VersionInputError("invalid_version_entry")
        try:
            portable_path(row['directory'] + '/__mount__.txt')
        except ArchiveError:
            raise VersionInputError("invalid_version_directory") from None
        seqs.append(row['artifact_seq'])
        directories.append(row['directory'])
    if len(set(seqs)) != len(seqs):
        raise VersionInputError("duplicate_artifact_sequence")
    try:
        _paths([directory + '/__mount__.txt' for directory in directories])
    except ArchiveError:
        raise VersionInputError("version_directories_conflict") from None
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
        raise VersionInputError("artifact_version_budget")
    try:
        _paths([f['path'] for f in layout])
    except ArchiveError:
        raise VersionInputError("invalid_version_paths") from None
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
