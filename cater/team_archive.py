"""Exact workspace snapshots; archive bytes never enter model/event JSON bodies."""

from __future__ import annotations

import hashlib

from .artifact_blobs import read_blob, put_blob
from .artifact_store import _name, materialize_team_artifact
from .events import Actor
from .workspace_archive import ArchiveError, create_snapshot, validate_manifest
from .work import WorkConflict


TOOL = "publish_workspace_archive"


def schema():
    return {"name": TOOL, "description": (
        "发布工作目录中明确选择的真实文件为工程ZIP，保留目录、空文件和二进制；"
        "paths填相对文件路径，不填目录，不自动包含整个仓库。先完成实际检查，再发布并交独立复核；"
        "ZIP发布不代表验证通过。最多256个文件，单文件32MiB，总64MiB。"),
        "input_schema": {"type": "object", "additionalProperties": False,
            "properties": {"filename": {"type": "string"}, "summary": {"type": "string"},
                "paths": {"type": "array", "minItems": 1, "maxItems": 256,
                          "items": {"type": "string"}}},
            "required": ["filename", "summary", "paths"]}}


def artifact_bytes(log, artifact):
    if artifact.get("format") == "archive":
        validate_manifest(artifact.get("archive"))
        return read_blob(log, artifact.get("content_sha256"), artifact.get("content_bytes"))
    content = artifact.get("content")
    if not isinstance(content, str):
        raise ValueError("成果没有完整正文。")
    body = content.encode("utf-8")
    if hashlib.sha256(body).hexdigest() != artifact.get("content_sha256"):
        raise ValueError("成果正文与已发布版本不一致。")
    return body


def publish(team, task_id, role, arguments, workspace, binding, tool_start):
    from .team import HOST, _publication_rejected, _text

    try:
        filename = _name(arguments["filename"])
        if not filename.endswith(".zip"):
            raise ValueError("工程成果文件名需要以.zip结尾。")
        summary = _text(arguments["summary"], "摘要")
    except (ValueError, TypeError, KeyError):
        return _publication_rejected("invalid_archive", "请填写便携.zip文件名、非空摘要和明确的相对文件路径。")
    rt = team.rt
    objective_revision = team.view(task_id)["revision"]

    def scope():
        current = rt.workspaces.view(task_id)
        if (workspace is None or not current["available"] or any(current.get(key) != binding.get(key)
                for key in ("revision", "fingerprint", "workspace_id"))):
            raise WorkConflict("工作目录或权限已变化，未发布旧目录工程。")
        if team.view(task_id)["revision"] != objective_revision:
            raise WorkConflict("目标已变化，未发布旧目标工程。")
        team._publication_guard(task_id, filename, tool_start)

    scope()
    try:
        with rt.workspaces._owner(binding["root"]) as owned:
            if not owned or rt.workspaces.pending(root=binding["root"], effects_only=True):
                return _publication_rejected("workspace_pending", "目录仍有执行或影响待核对，未生成工程快照。")
            body, manifest = create_snapshot(workspace, arguments["paths"])
    except (ArchiveError, OSError):
        return _publication_rejected("archive_source_unavailable",
            "未能取得完整、稳定且安全的文件快照；请核对路径、受保护文件、命名冲突、容量和并行修改。未发布部分工程。")
    payload = {"title": filename, "filename": filename, "summary": summary, "format": "archive",
        "content_bytes": len(body), "content_sha256": hashlib.sha256(body).hexdigest(),
        "archive": manifest, "source": "team", "phase": "team"}
    # File access is outside the DB write transaction; blob and event commit together.
    with rt.log.atomic():
        scope()
        run = next(e for e in rt.log.read(task_id=task_id) if e.type == "team.run_started"
                   and e.actor == HOST and e.payload.get("run_id") == tool_start.payload["run_id"])
        team._current(task_id, run, role, run.payload["revision"])
        if team.view(task_id)["status"] == "done":
            raise WorkConflict("当前工作已交付，未追加旧工程快照。")
        put_blob(rt.log, body)
        event = rt.log.emit(type="deliverable.published", task_id=task_id,
            actor=Actor.agent(role.id, role.role), payload=payload)
    result = {"status": "ok", "artifact_seq": event.seq, "filename": filename,
              "content_sha256": payload["content_sha256"], "content_bytes": len(body),
              "file_count": len(manifest["files"])}
    if rt._artifact_root is not None:
        path = materialize_team_artifact(rt._artifact_root, task_id, {**payload, "seq": event.seq}, body=body)
        rt.log.emit(type="team.artifact_saved", task_id=task_id, actor=HOST,
            payload={"artifact_seq": event.seq, "path": path, "content_sha256": payload["content_sha256"]})
        result["local_path"] = path
    return result


def read_member(log, artifact, arguments, *, guide_missing=False):
    from .archive_members import read_members
    from .workspace_archive import portable_path

    if guide_missing:
        manifest = artifact.get("archive")
        validate_manifest(manifest)
    path = portable_path(arguments["path"])
    offset, length = arguments.get("offset", 0), arguments.get("length", 8000)
    if type(offset) is not int or offset < 0 or type(length) is not int or not 1 <= length <= 16000:
        raise ValueError("工程文本分页需要非负offset和1到16000的length，单位为Unicode字符。")
    if guide_missing and path not in {entry["path"] for entry in manifest["files"]}:
        return {"status": "error", "code": "archive_member_not_listed", "complete": True,
                "outcome": "not_started", "artifact_seq": artifact["seq"],
                "message": "所选版本已保存的manifest文件清单中没有这个成员。请仅传artifact_seq调用read_artifact查看文件清单，再用清单中的精确相对路径作为path读取。"}
    member, = read_members(log, artifact, [path], max_files=1,
        max_member_bytes=32 * 1024 * 1024, max_total_bytes=32 * 1024 * 1024)
    body = member.pop("body")
    result = {"status": "ok", "artifact_seq": artifact["seq"], "archive_sha256": artifact["content_sha256"], **member}
    try:
        content = body.decode("utf-8")
        if any(ord(char) < 32 and char not in "\t\r\n\f" for char in content):
            raise UnicodeError()
    except UnicodeError:
        return {**result, "content": None, "content_kind": "binary",
                "note": "仅核对保存的二进制字节与摘要，未解释或执行文件内容。"}
    if offset > len(content):
        raise ValueError("分页起点超出工程文件正文。")
    end = min(len(content), offset + length)
    return {**result, "content_kind": "utf8", "content": content[offset:end], "read_range": {
        "unit": "unicode_codepoints", "offset": offset, "returned_chars": end - offset,
        "total_chars": len(content), "has_more": end < len(content),
        "next_offset": end if end < len(content) else None,
        "content_complete": offset == 0 and end == len(content)}}
