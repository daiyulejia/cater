"""Run an existing workspace alias against fixed published bytes, not a sandbox."""

from dataclasses import replace
import hashlib
import os
from pathlib import Path
import sqlite3
import tempfile

from .artifact_store import _directories, _inspect, _read, _write
from .events import Actor
from .work import WorkConflict
from .workspace_tools import WorkspaceExecutor
from . import artifact_versions


def execute(team, task_id, role, arguments, workspace, binding, tool_start):
    # Caller owns the original workspace lock and its unresolved-operation gate.
    scope = team.recovery.scope(task_id)
    started = next(e for e in team.rt.log.read(task_id=task_id)
                   if e.type == "team.run_started" and e.actor == Actor.system("team")
                   and e.payload["run_id"] == tool_start.payload["run_id"])
    sources = None
    versioned = 'artifact_versions' in arguments

    def selected_references():
        if versioned:
            return artifact_versions.references(team, task_id, arguments['artifact_versions'])
        return team.browser._files(task_id, arguments['artifact_seqs'])

    def selected_inputs():
        if versioned:
            return artifact_versions.inputs(team, task_id, arguments['artifact_versions'])
        selected, files, _ = team.browser._inputs(task_id, {'artifact_seqs': arguments['artifact_seqs']},
                                                  browser_paths=False)
        return selected, files

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
                if selected_references() != sources:
                    raise ValueError("Published selection changed")
            except ValueError:
                raise WorkConflict("成果版本已变化，未继续旧版本检查。") from None

    current()
    try:
        if versioned and 'artifact_seqs' in arguments:
            raise ValueError('artifact_versions与artifact_seqs不能同时提供。')
        sources, files = selected_inputs()
    except (ValueError, KeyError, TypeError):
        return {"status": "error", "code": "invalid_artifact_check", "complete": True,
                "outcome": "not_started", "message": ("确切版本或挂载目录无效，未执行命令；artifact_versions与artifact_seqs互斥，目录须独立，不能有大小写、Unicode或路径冲突，并遵守文件预算。"
                    if versioned else "成果版本、完整内容或路径无效，未执行命令；请读取本群最新成果清单，明确选择完整文件或工程ZIP。")}
    command = {key: value for key, value in arguments.items() if key not in {'artifact_seqs', 'artifact_versions'}}
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
        selected, checked = selected_inputs()
        same_scope = selected == sources and checked == files
    except (WorkConflict, ValueError, KeyError, TypeError, OSError, sqlite3.Error):
        same_scope = False
    versions = artifact_versions.checked_artifacts(team, task_id, sources) if versioned else None
    try:
        # Recheck after byte and diagnostic reads; those reads can outlive authorization.
        current()
    except (WorkConflict, ValueError, KeyError, TypeError, OSError, sqlite3.Error):
        same_scope = False
    versions_current = (False if any(row['is_latest_at_check'] is False for row in versions)
                        else True if all(row['is_latest_at_check'] is True for row in versions)
                        else None) if versioned else None
    return {**result, "environment": "temporary_artifact_copy_not_sandbox",
            "scope_current": same_scope, "artifacts_unchanged": unchanged,
            "artifact_check_valid": same_scope and unchanged,
            "executable_flags_checked": os.name != "nt",
            **({'artifact_selection': 'version_directories', 'selected_versions_current': versions_current}
               if versioned else {}),
            "checked_artifacts": versions if versioned else [{key: source[key] for key in ("seq", "filename", "content_sha256")}
                                  for source in sources],
            "checked_files": [{key: entry[key] for key in ('artifact_seq', 'path', 'bytes', 'sha256', 'executable',
                                                           *(['source_path'] if versioned else []))}
                              for entry in files],
            "note": ("仅执行明确挂载的确切版本副本；历史版本可以有有效执行证据，不代表最新版或继承批准。scope_current指当前目标/权限及确切源字节有效，selected_versions_current另记最新发布。退出成功不证明兼容、覆盖完整或独立批准。"
                if versioned else "仅执行所列发布版本副本；副本变更或范围过期时不能用作原成果当前版本的检查。退出成功不代表业务覆盖完整或伙伴批准。")}
