"""Install an explicitly selected historical ZIP member into the current workspace."""

from __future__ import annotations

from .archive_members import read_members
from .events import Actor
from .project_history import ProjectHistory
from .workspace_archive import portable_path
from .workspace_tools import _Denied
from .work import WorkConflict


TOOL = "workspace_restore_project_file"


def schema():
    return {"name": TOOL, "description": (
        "把同项目历史交付的一个确切工程ZIP成员恢复到当前已授权工作目录，支持文本、图片等二进制和空文件。"
        "先用read_project_history查交付及清单，保持as_of_seq/delivery_seq/artifact_seq/path；"
        "destination可选，默认原相对路径。已有相同字节只核对，不重写；不同内容不覆盖，可选择新路径或读取后按当前需求编辑。"
        "每次一个文件，逐项核对后继续；恢复不运行代码、不发布成果、不继承旧验收。"),
        "input_schema": {"type": "object", "additionalProperties": False,
            "properties": {
                "as_of_seq": {"type": "integer", "minimum": 0},
                "delivery_seq": {"type": "integer", "minimum": 1},
                "artifact_seq": {"type": "integer", "minimum": 1},
                "path": {"type": "string", "maxLength": 2048},
                "destination": {"type": "string", "maxLength": 2048}},
            "required": ["as_of_seq", "delivery_seq", "artifact_seq", "path"]}}


def public_arguments(arguments):
    if not isinstance(arguments, dict):
        return {}
    result = ProjectHistory.public_arguments({**arguments, "section": "artifact"})
    result.pop("section", None)
    if "destination" in arguments:
        try:
            path = portable_path(arguments["destination"])
            if len(path) <= 2048:
                result["destination"] = path
        except ValueError:
            pass
    return result


def _rejected(code, message):
    return {"status": "error", "code": code, "complete": True,
            "outcome": "not_started", "message": message}


def restore(team, task_id, role, arguments, workspace, binding, tool_start):
    rt = team.rt
    try:
        required = {"as_of_seq", "delivery_seq", "artifact_seq", "path"}
        if (not isinstance(arguments, dict) or not required <= arguments.keys()
                or set(arguments) - required - {"destination"}
                or any(type(arguments[key]) is not int or not minimum <= arguments[key] < 2 ** 63 - 1
                       for key, minimum in (("as_of_seq", 0), ("delivery_seq", 1), ("artifact_seq", 1)))):
            raise ValueError()
        path = portable_path(arguments["path"])
        destination = portable_path(arguments.get("destination", path))
        if len(path) > 2048 or len(destination) > 2048:
            raise ValueError()
    except (ValueError, TypeError, KeyError):
        return _rejected("invalid_restore_arguments", "请提供确切目录版本、交付和工程文件引用，以及清单中的相对路径；未写入文件。")
    expected = tool_start.payload.get("project_scope")
    if not isinstance(expected, str) or not expected:
        return _rejected("project_history_scope_changed", "原项目范围不可用，未恢复历史文件。")
    if workspace is None or workspace.policy.mode not in {"edit", "execute"}:
        return _rejected("workspace_read_only", "当前目录没有编辑权限，未恢复文件；历史读取不会授予写入权限。")
    selection = {"section": "artifact", **{key: arguments[key] for key in
                 ("as_of_seq", "delivery_seq", "artifact_seq")}}
    try:
        selected = team.project_history.read(task_id, selection, expected_project_id=expected)
    except Exception:
        return _rejected("restore_source_unavailable", "无法核对历史交付范围，未恢复文件。")
    if selected.get("status") != "ok":
        return selected
    source = selected.get("source", {})
    if source.get("format") != "archive" or "archive_read" not in selected:
        return _rejected("restore_archive_required", "这份引用不是可恢复的历史工程ZIP；普通文本可按当前目标读取并编辑，未写入文件。")
    run = next((e for e in rt.log.iter_events(task_id=task_id, event_types=("team.run_started",))
        if e.payload.get("run_id") == tool_start.payload.get("run_id")
        and e.payload.get("role") == role.id and e.actor == Actor.system("team")), None)
    if run is None:
        return _rejected("restore_scope_changed", "无法核对当前运行范围，未恢复文件。")

    def guard():
        current = rt.workspaces.view(task_id)
        if (not current["available"] or any(current.get(key) != binding.get(key)
                for key in ("revision", "fingerprint", "workspace_id", "root", "mode"))
                or str(workspace.policy.root) != binding.get("root")
                or workspace.policy.fingerprint != rt.workspaces.config[current["workspace_id"]]["policy"].fingerprint):
            raise WorkConflict("workspace changed")
        objective = team.view(task_id)
        if (objective["status"] == "done" or objective["revision"] != tool_start.payload.get("revision")
                or rt.execution_control.view(task_id)["state"] != "running"):
            raise WorkConflict("execution scope changed")
        team._current(task_id, run, role, tool_start.payload["revision"])
        try:
            fresh = team.project_history.read(task_id, selection, expected_project_id=expected)
        except Exception:
            raise WorkConflict("historical source unavailable") from None
        if fresh.get("status") != "ok" or fresh.get("source") != source:
            raise WorkConflict("historical source scope changed")

    attempted = False
    try:
        with rt.workspaces._owner(binding["root"]) as owned:
            if not owned or any(e.seq != tool_start.seq for e in rt.workspaces.pending(root=binding["root"], effects_only=True)):
                return _rejected("workspace_pending", "目录有执行中或影响待核对的操作，未恢复文件；请先核对原操作。")
            guard()
            event = next(rt.log.iter_events(task_id=source["task_id"], event_types=("deliverable.published",),
                after_seq=source["artifact_seq"] - 1, before_seq=source["artifact_seq"] + 1), None)
            if (event is None or event.hash != source["event_hash"] or event.actor.kind != "agent"
                    or event.actor.id != source["author"] or event.payload.get("filename") != source["filename"]
                    or event.payload.get("content_sha256") != source["content_sha256"]):
                return _rejected("restore_source_unavailable", "这份确切历史工程版本不可读，未改读其他版本或磁盘文件。")
            try:
                member, = read_members(rt.log, {**event.payload, "seq": event.seq}, [path],
                    max_files=1, max_member_bytes=32 * 1024 * 1024, max_total_bytes=32 * 1024 * 1024)
            except Exception:
                return _rejected("restore_source_unavailable", "所选历史文件缺失、损坏或校验不一致，未写入文件。")
            try:
                attempted = True
                restored = workspace.install_bytes(destination, member["body"], executable=member["executable"], guard=guard)
            except _Denied as exc:
                return _rejected(exc.code, "未恢复文件：请核对当前编辑权限、相对路径及已有文件；不同内容不会被覆盖。")
            if (restored.get("path") != destination or restored.get("sha256") != member["sha256"]
                    or type(restored.get("bytes")) is not int or restored["bytes"] != member["bytes"]
                    or any(type(restored.get(key)) is not bool for key in
                           ("created", "already_present", "executable", "executable_requested"))
                    or restored["created"] == restored["already_present"]
                    or restored["executable_requested"] != member["executable"]):
                raise OSError("installation receipt mismatch")
            return {"status": "ok", "complete": True,
                "outcome": "already_present" if restored["already_present"] else "written",
                **{key: selected[key] for key in ("as_of_seq", "as_of_hash")}, "source": source,
                "member": {key: member[key] for key in ("path", "sha256", "bytes", "executable")},
                "restored_file": restored,
                "note": "仅恢复或核对当前文件，不执行、不发布、不继承历史验收；继续按本任务目标检查与复核。"}
    except WorkConflict:
        return _rejected("restore_scope_changed", "目标、控制、目录或项目范围已变化，未按旧要求恢复文件。")
    except (OSError, ValueError, TypeError, KeyError, OverflowError):
        if not attempted:
            return _rejected("restore_source_unavailable", "无法取得完整来源或目标目录，未开始恢复文件。")
        return {"status": "error", "code": "restore_result_unknown", "complete": False,
                "outcome": "effects_unknown", "message": "恢复操作的实际影响尚未核对；可能已写入文件，请先核对原文件与回执，不要盲目重放。"}
