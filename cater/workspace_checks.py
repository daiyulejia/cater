"""Configured checks of submitted source copies, independent of work-time claims."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import tempfile
import time

from .events import Actor
from .work import WorkConflict


def captured_path(rt, task_id, artifact):
    source = artifact.get("workspace_file")
    if not isinstance(source, dict):
        return None
    capture = next((e for e in rt.log.iter_events(task_id=task_id, event_types=("workspace.files_captured",))
                    if e.seq == source.get("capture_seq") and e.actor == Actor.system("workspace_gateway")), None)
    fields = {k: source.get(k) for k in ("path", "filename", "content_sha256")}
    if (capture is None or fields not in capture.payload.get("files", [])
            or capture.payload.get("attempt_id") != artifact.get("attempt_id")
            or fields["filename"] != artifact.get("filename") or fields["content_sha256"] != artifact.get("content_sha256")):
        raise WorkConflict("本地源码缺少有效的宿主读取记录。")
    path = fields["path"]
    if (not isinstance(path, str) or "\\" in path or ":" in path
            or PurePosixPath(path).is_absolute() or any(p in {"", ".", ".."} for p in path.split("/"))):
        raise WorkConflict("本地源码相对路径无效。")
    return path


class WorkspaceChecks:
    def __init__(self, runtime):
        self.rt = runtime

    def _config(self, task_id):
        binding = self.rt.workspaces.view(task_id)
        row = self.rt.workspaces.config.get(binding.get("workspace_id"))
        return row if binding["available"] and not binding["pending"] and row and row["checks"] else None

    def _files(self, task_id, items):
        files, seen = {}, {}
        for item in items:
            for artifact in self.rt.work.artifacts(task_id, item["submission"]):
                path = captured_path(self.rt, task_id, artifact)
                if path is None:
                    continue
                canonical = path.casefold()
                if canonical in seen and (seen[canonical] != path or files[path] != artifact["content"]):
                    raise WorkConflict("提交包含不同版本的同路径源码，需要明确最终版本后再检查。")
                seen[canonical], files[path] = path, artifact["content"]
        if not files:
            raise WorkConflict("没有宿主读取的源码快照，不能把当前目录冒充已提交版本。")
        return files

    def _execute(self, row, files, before_each=None):
        from .workspace_tools import WorkspaceExecutor, WorkspacePolicy

        checks = []
        deadline = time.monotonic() + 600
        with tempfile.TemporaryDirectory(prefix="cater-source-check-") as tmp:
            root = Path(tmp)
            for path, content in files.items():
                target = root / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content.encode("utf-8"))
            config = {**row["policy"].as_dict(), "root": str(root)}
            executor = WorkspaceExecutor(WorkspacePolicy.from_dict(config))
            for check in row["checks"]:
                try:
                    if before_each is not None:
                        before_each()
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise WorkConflict("本次检查已达到总时限，不启动下一条命令。")
                except WorkConflict as exc:
                    checks.append({"id": check["id"], "title": check["title"], "command_alias": check["command"],
                                   "args": check["args"], "result": {"status": "error", "outcome": "unavailable",
                                   "invocation_started": False, "reason": str(exc)}})
                    break
                result = executor.execute("workspace_run", {"command": check["command"], "args": check["args"], "cwd": ".",
                    "timeout": min(config.get("timeout", 60), remaining)})
                checks.append({"id": check["id"], "title": check["title"], "command_alias": check["command"],
                               "args": check["args"], "result": result})
            try:
                unchanged = all(not (root / path).is_symlink() and (root / path).is_file()
                                and (root / path).read_bytes() == content.encode("utf-8") for path, content in files.items())
            except OSError:
                unchanged = False
        # Exit zero is recorded only for this suite. Independent review still judges relevance.
        passed = bool(checks) and all(c["result"].get("status") == "ok" and c["result"].get("exit_code") == 0
                                      and c["result"].get("outcome") == "passed" for c in checks)
        raw = json.dumps(checks, ensure_ascii=False)
        encoded = raw.encode("utf-8")
        complete = len(encoded) <= 60000
        stdout = {"bytes": len(encoded), "sha256": hashlib.sha256(encoded).hexdigest(), "complete": complete,
                  "truncated": not complete, "excerpt": encoded[:60000].decode("utf-8", errors="ignore")}
        empty = {"bytes": 0, "sha256": hashlib.sha256(b"").hexdigest(), "complete": True, "truncated": False, "excerpt": ""}
        return {"source": "configured_workspace_checks", "check_kind": "workspace", "suite": checks,
                "command": ["configured_checks", *(c["id"] for c in row["checks"])],
                "outcome": ("unavailable" if any(c["result"].get("outcome") == "unavailable" for c in checks)
                            else "passed" if passed and complete else "failed"), "exit_code": 0 if passed and complete else 1,
                "stdout": stdout, "stderr": empty, "artifacts_unchanged": unchanged,
                "environment": "temporary_source_copy_not_sandbox",
                "checked_files": [{"path": p, "content_sha256": hashlib.sha256(c.encode("utf-8")).hexdigest()} for p, c in files.items()]}

    def run(self, task_id, item):
        if self.rt.log.db.in_transaction:
            raise WorkConflict("检查开始记录必须在执行前提交，不能从外层事务执行检查。")
        row = self._config(task_id)
        if row is None or not item.get("submission"):
            return
        view = self.rt.work.view(task_id)
        if not view.get("scope_current") or item.get("check_in_flight") or item.get("check_uncertain"):
            return
        by_id = {i["id"]: i for i in view["items"]}
        required, pending = set(), list(item["depends_on"])
        while pending:
            identity = pending.pop()
            if identity in required:
                continue
            dependency = by_id.get(identity)
            if dependency is None or dependency["status"] != "accepted" or not dependency.get("submission"):
                return
            required.add(identity)
            pending.extend(dependency["depends_on"])
        deps = [i for i in view["items"] if i["id"] in required]
        try:
            files = self._files(task_id, [*deps, item])
        except WorkConflict:
            return
        for criterion in item["criteria"]:
            if criterion["kind"] != "local_check" or criterion["id"] in self.rt.work.checks(task_id, item):
                continue
            with self.rt.workspaces._owner(row["policy"].as_dict()["root"]) as owned:
                if not owned:
                    return
                with self.rt.log.atomic():
                    self.rt._check_execution_authorization(task_id)
                    if self.rt.execution_control.view(task_id)["state"] != "running" or not self._config(task_id):
                        return
                    _, current = self.rt.work._item(task_id, item["id"])
                    if current.get("check_in_flight") or criterion["id"] in self.rt.work.checks(task_id, current):
                        continue
                    started = self.rt.work.start_check(task_id, item["id"], criterion["id"],
                        lease_seconds=min(600, len(row["checks"]) * row["policy"].as_dict().get("timeout", 60)) + 30,
                        expected_submission_seq=item["submission"]["seq"])
                    control_seq = self.rt.execution_control.view(task_id)["seq"]
                result = self._execute(row, files, lambda: self._authorized(task_id, started.payload["scope"], control_seq))
                self.rt.log.emit(type="verification.completed", task_id=task_id, actor=Actor.system("local_verifier"),
                    payload={**result, **{k: started.payload[k] for k in
                        ("plan_seq", "scope", "work_item_id", "criterion_id", "submission_seq", "submission_digest", "check_attempt")},
                        "check_attempt_seq": started.seq})

    def run_delivery(self, task_id, view):
        if self.rt.log.db.in_transaction:
            raise WorkConflict("检查开始记录必须在执行前提交，不能从外层事务执行检查。")
        row = self._config(task_id)
        if (row is None or view.get("status") != "pending" or not view.get("manifest")
                or self.rt.delivery._checks(task_id, view["digest"]) or view.get("check_in_flight")):
            return
        if not any(c["id"] == "integration_behavior" for c in view["manifest"]["criteria"]):
            return
        try:
            files = self._files(task_id, view["manifest"]["items"])
        except WorkConflict:
            return
        with self.rt.workspaces._owner(row["policy"].as_dict()["root"]) as owned:
            if not owned:
                return
            with self.rt.log.atomic():
                self.rt._check_execution_authorization(task_id)
                if self.rt.execution_control.view(task_id)["state"] != "running" or not self._config(task_id):
                    return
                started = self.rt.delivery._start_check(task_id, view, "integration_behavior",
                    min(600, len(row["checks"]) * row["policy"].as_dict().get("timeout", 60)), automatic=True)
                control_seq = self.rt.execution_control.view(task_id)["seq"]
            result = self._execute(row, files, lambda: self._authorized(task_id, view["manifest"]["scope"], control_seq))
            self.rt.log.emit(type="verification.completed", task_id=task_id, actor=Actor.system("local_verifier"),
                payload={**result, "delivery_digest": view["digest"], "criterion_id": "integration_behavior",
                         "check_attempt_seq": started.seq})

    def _authorized(self, task_id, scope, control_seq):
        self.rt._check_execution_authorization(task_id)
        control = self.rt.execution_control.view(task_id)
        if (control["state"] != "running" or control["seq"] != control_seq
                or self.rt.work.scope(task_id) != scope or not self._config(task_id)):
            raise WorkConflict("要求、权限或执行控制已变化，不再启动下一条检查命令。")
