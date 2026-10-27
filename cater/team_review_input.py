"""Explicit review handoffs with immutable file or archive inputs.

Read workspace bytes before the event transaction. Candidate, review request
and receipt then commit together; no materialized artifact files are written.
"""

from contextlib import contextmanager
import hashlib

from .artifacts import MAX_CONTENT_BYTES, normalize_deliverables
from .artifact_blobs import put_blob
from .artifact_store import _name
from .events import Actor
from .work import WorkConflict
from .workspace_archive import ArchiveError, MAX_FILES, create_snapshot


class ReviewInputError(ValueError):
    """Correctable source validation with no durable review mutation."""

    def __init__(self, message):
        super().__init__(message)


def fields():
    return {
        "artifact_seqs": {"type": "array", "minItems": 1, "maxItems": 20,
            "items": {"type": "integer", "minimum": 1},
            "description": "已发布的本轮完整成果集合；与workspace_file、workspace_archive三选一。"},
        "workspace_file": {"type": "object", "additionalProperties": False,
            "description": "未发布的单份UTF-8文件：保存确切候选并交复核，不改源文件、不代表最终交付；与artifact_seqs、workspace_archive三选一。",
            "properties": {"path": {"type": "string", "description": "当前工作目录的相对文件路径。"},
                "format": {"type": "string", "enum": ["code", "markdown", "text"]}},
            "required": ["path", "format"]},
        "workspace_archive": {"type": "object", "additionalProperties": False,
            "description": "明确文件集合保存为ZIP候选并发起复核；与artifact_seqs、workspace_file三选一。ZIP可直接通过read_artifact(path)读取成员、workspace_run(artifact_seqs)执行成果副本。",
            "properties": {"filename": {"type": "string", "description": "便携文件名，以.zip结尾。"},
                "paths": {"type": "array", "minItems": 1, "maxItems": MAX_FILES,
                    "items": {"type": "string"},
                    "description": "明确的相对文件路径，不填目录、不扫描仓库；最多256个，单个32MiB、总64MiB。"}},
            "required": ["filename", "paths"]},
        "review_seq": {"type": "integer", "minimum": 1,
            "description": "同一目标内修订时填写当前复核request_seq，由原送审负责人交回原复核者；可直接选伙伴已发布的新版，不重复发布，本轮需提交完整成果集合，旧批准不覆盖新版。首次提交省略，不填委托或消息编号；已采用新目标且旧复核为stale时，省略旧review_seq，按当前目标另行提交。"},
    }


def arguments(name, values):
    if name == "delegate":
        if "review" not in values:
            return None
        source = values["review"]
        if not isinstance(source, dict) or set(source) - set(fields()):
            raise ReviewInputError("review需要对象，只填写artifact_seqs、workspace_file或workspace_archive，以及可选的review_seq。")
        result = {**source, "reviewer": values["recipient"], "instruction": values["instruction"]}
    elif name == "request_review":
        result = dict(values)
    else:
        return None
    if sum(key in result for key in ("artifact_seqs", "workspace_file", "workspace_archive")) != 1:
        raise ReviewInputError("复核输入请在artifact_seqs、workspace_file和workspace_archive中选择一种，不同时填写，也不同时省略。")
    return result


def _scope(team, task_id, binding):
    current = team.rt.workspaces.view(task_id)
    if not current.get("available") or any(current.get(k) != binding.get(k)
            for k in ("revision", "fingerprint", "workspace_id", "root")):
        raise WorkConflict("复核文件的工作目录或权限已变化，未保存候选。")


@contextmanager
def prepare(team, task_id, role, call, workspace, binding, tool_start):
    values = arguments(call.name, call.arguments)
    if values is None or "artifact_seqs" in values:
        yield None
        return
    archive = "workspace_archive" in values
    spec = values["workspace_archive" if archive else "workspace_file"]
    if archive:
        try:
            if not isinstance(spec, dict) or set(spec) != {"filename", "paths"}:
                raise ValueError()
            filename = _name(spec["filename"])
            if not filename.endswith(".zip"):
                raise ValueError()
        except (ValueError, TypeError, KeyError):
            raise ReviewInputError("workspace_archive只填写便携.zip文件名filename和明确的相对文件路径paths。") from None
    elif (not isinstance(spec, dict) or set(spec) != {"path", "format"}
            or not isinstance(spec["path"], str) or not spec["path"].strip()
            or not isinstance(spec["format"], str)
            or spec["format"] not in {"code", "markdown", "text"}):
        raise ReviewInputError("workspace_file只填写相对path和format（code、markdown或text）。")
    reviewer = team._role(values["reviewer"])
    if reviewer.id == role.id:
        raise ReviewInputError("作者不能自行复核，请选择另一位伙伴。")
    if (not isinstance(values["instruction"], str) or not values["instruction"].strip()
            or len(values["instruction"]) > 16000):
        raise ReviewInputError("复核需要一至16000字符的非空说明。")
    if workspace is None or not binding.get("root") or team.rt.log.db.in_transaction:
        raise WorkConflict("需要有效工作目录，且不能在事件事务中读取复核文件。")
    team.rt.team_reviews._guard(task_id, role, tool_start)
    with team.rt.workspaces._owner(binding["root"]) as owned:
        if not owned or any(e.seq != tool_start.seq for e in team.rt.workspaces.pending(
                root=binding["root"], effects_only=True)):
            raise WorkConflict("目录有执行或影响待核对，未取得复核快照。")
        _scope(team, task_id, binding)
        if archive:
            try:
                body, manifest = create_snapshot(workspace, spec["paths"])
            except (ArchiveError, OSError):
                raise ReviewInputError("未能取得完整、稳定且安全的工程快照；请核对明确文件路径、受保护文件、命名冲突、容量和并行修改。未保存候选。") from None
            digest = hashlib.sha256(body).hexdigest()
            payload = {"title": filename, "filename": filename, "format": "archive",
                "summary": "待独立复核的工作目录工程快照。", "content_bytes": len(body),
                "content_sha256": digest, "archive": manifest}
            provenance = {"paths": [entry["path"] for entry in manifest["files"]],
                "sha256": digest, "bytes": len(body), "workspace_id": binding["workspace_id"],
                "workspace_revision": binding["revision"], "workspace_fingerprint": binding["fingerprint"],
                "started_seq": tool_start.seq}
            yield {"arguments": values, "payload": payload, "provenance": provenance, "body": body}
            return
        source = workspace.execute("workspace_read", {"path": spec["path"], "limit": MAX_CONTENT_BYTES})
        if (source.get("status") != "ok" or source.get("truncated") is not False
                or not isinstance(source.get("content"), str)):
            raise ReviewInputError("未读到完整UTF-8文件，未保存候选。请核对相对路径、读取权限和64KiB上限；完整文件集合可直接用workspace_archive提交复核。")
        body = source["content"].encode("utf-8")
        if (not body or len(body) > MAX_CONTENT_BYTES or source.get("bytes") != len(body)
                or source.get("returned_bytes") != len(body) or source.get("offset") != 0
                or source.get("sha256") != hashlib.sha256(body).hexdigest()):
            raise ReviewInputError("文件超过64KiB或读取内容与完整字节不一致，未保存候选；不要用节选验收，可用workspace_archive提交完整文件集合。")
        filename = source["path"].rsplit("/", 1)[-1]
        payload, = normalize_deliverables([{"title": filename, "filename": filename,
            "content": source["content"], "format": spec["format"], "summary": "待独立复核的工作目录文件快照。"}])
        if not payload.get("filename") or not payload.get("content_sha256"):
            raise ReviewInputError("复核候选不符合完整文本成果约定，请核对非空内容与便携文件名。")
        provenance = {"path": source["path"], "sha256": source["sha256"], "bytes": len(body),
            "workspace_id": binding["workspace_id"], "workspace_revision": binding["revision"],
            "workspace_fingerprint": binding["fingerprint"], "started_seq": tool_start.seq}
        yield {"arguments": values, "payload": payload, "provenance": provenance}


def request(team, task_id, role, name, values, binding, tool_start, assignment, prepared):
    selected = arguments(name, values)
    if selected is None:
        raise ValueError("未提供复核交接。")
    if "artifact_seqs" in selected:
        return team.rt.team_reviews.request(task_id, role, selected, tool_start, assignment)
    if prepared is None or prepared["arguments"] != selected:
        raise WorkConflict("缺少本次工具调用取得的复核快照。")
    if not team.rt.log.db.in_transaction:
        raise WorkConflict("复核快照需要与请求和回执在同一事件事务中提交。")
    _scope(team, task_id, binding)
    payload = prepared["payload"]
    team._publication_guard(task_id, payload["filename"], tool_start)
    if "workspace_archive" in selected:
        put_blob(team.rt.log, prepared["body"])
    event = team.rt.log.emit(type="deliverable.published", task_id=task_id,
        actor=Actor.agent(role.id, role.role), payload={**payload, "source": "team", "phase": "team",
            "review_workspace_source": prepared["provenance"]})
    selected = {k: v for k, v in selected.items() if k not in {"workspace_file", "workspace_archive"}}
    selected["artifact_seqs"] = [event.seq]
    result = team.rt.team_reviews.request(task_id, role, selected, tool_start, assignment)
    # The caller's local-event transaction rolls back this candidate on rejection.
    return {**result, **({"artifact_seqs": [event.seq], "workspace_snapshot": prepared["provenance"]}
                        if result.get("status") == "ok" else {})}
