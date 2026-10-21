"""Run an existing workspace alias against fixed published bytes, not a sandbox."""

from dataclasses import replace
import hashlib
import os
from pathlib import Path
import tempfile

from .artifact_store import _directories, _inspect, _read, _write
from .events import Actor
from .work import WorkConflict
from .workspace_tools import WorkspaceExecutor


def execute(team, task_id, role, arguments, workspace, binding, tool_start):
    # Caller owns the original workspace lock and its unresolved-operation gate.
    scope = team.recovery.scope(task_id)
    started = next(e for e in team.rt.log.read(task_id=task_id)
                   if e.type == "team.run_started" and e.actor == Actor.system("team")
                   and e.payload["run_id"] == tool_start.payload["run_id"])
    sources = None

    def current():
        team._current(task_id, started, role, tool_start.payload["revision"])
        live = team.rt.workspaces.view(task_id)
        if (team.recovery.scope(task_id) != scope or scope["control_state"] != "running"
                or not live["available"] or any(live.get(key) != binding.get(key)
                    for key in ("revision", "fingerprint", "workspace_id"))):
            raise WorkConflict("目标、控制或工作目录权限已变化，未继续旧成果检查。")
        if scope["done"] and not team._post_delivery_command(task_id, role, tool_start):
            raise WorkConflict("顾客输入已变化，未继续旧的交付后检查。")
        if sources is not None:
            try:
                if team.browser._files(task_id, arguments["artifact_seqs"]) != sources:
                    raise ValueError("Published selection changed")
            except ValueError:
                raise WorkConflict("成果版本已变化，未继续旧版本检查。") from None

    current()
    try:
        sources, files, _ = team.browser._inputs(task_id, {"artifact_seqs": arguments["artifact_seqs"]},
                                               browser_paths=False)
    except (ValueError, KeyError, TypeError):
        return {"status": "error", "code": "invalid_artifact_check", "complete": True,
                "outcome": "not_started", "message": "成果版本、完整内容或路径无效，未执行命令；请读取本群最新成果清单，明确选择完整文件或工程ZIP。"}
    command = {key: value for key, value in arguments.items() if key != "artifact_seqs"}
    with tempfile.TemporaryDirectory(prefix="cater-artifact-run-") as tmp:
        root = Path(tmp)
        for entry in files:
            target = root / entry["path"]
            _directories(target.parent, create=True)
            _write(target, entry["body"])
            if entry["executable"]:
                target.chmod(0o755)
        runner = WorkspaceExecutor(replace(workspace.policy, root=root))
        current()
        result = runner.execute("workspace_run", command)
        try:
            for entry in files:
                target = root / entry["path"]
                _directories(target.parent)
                info = _inspect(target, False)
                if hashlib.sha256(_read(target, 16 * 1024 * 1024)).hexdigest() != entry["sha256"]:
                    raise ValueError("Checked copy changed")
                if os.name != "nt" and bool(info.st_mode & 0o111) != entry["executable"]:
                    raise ValueError("Checked executable flag changed")
            unchanged = True
        except (OSError, ValueError):
            unchanged = False
    try:
        current()
        selected, checked, _ = team.browser._inputs(task_id, {"artifact_seqs": arguments["artifact_seqs"]},
                                                   browser_paths=False)
        same_scope = selected == sources and checked == files
    except (WorkConflict, ValueError, KeyError, TypeError, OSError):
        same_scope = False
    return {**result, "environment": "temporary_artifact_copy_not_sandbox",
            "scope_current": same_scope, "artifacts_unchanged": unchanged,
            "artifact_check_valid": same_scope and unchanged,
            "executable_flags_checked": os.name != "nt",
            "checked_artifacts": [{key: source[key] for key in ("seq", "filename", "content_sha256")}
                                  for source in sources],
            "checked_files": [{key: entry[key] for key in ("artifact_seq", "path", "bytes", "sha256", "executable")}
                              for entry in files],
            "note": "仅执行所列发布版本副本；副本变更或范围过期时不能用作原成果当前版本的检查。退出成功不代表业务覆盖完整或伙伴批准。"}
