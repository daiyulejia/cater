"""Native-team checks of fixed published files, using the optional trusted runner."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
import unicodedata

from .browser_checks import ACTION_FIELDS, MAX_BROWSER_STEPS, PLAN_FILE, browser_receipt, normalize_plan, validate_browser_paths
from .events import Actor
from .verification import output_metadata, run_check
from .work import WorkConflict


class BrowserInputError(ValueError):
    """A bounded, public diagnostic before any browser process is started."""


BROWSER_CONTRACT = {"version": 1, "kind": "published_copy_check", "receipt": "process_cleanup"}


def browser_schema():
    variants = []
    for action, fields in ACTION_FIELDS.items():
        properties = {"action": {"type": "string", "enum": [action]},
                      "label": {"type": "string", "maxLength": 100}}
        for key, limit in fields.items():
            properties[key] = ({"type": "integer", "minimum": 0, "maximum": 2000} if key == "ms"
                else {"type": "number", "minimum": 0, "maximum": 16384} if key in {"x", "y"}
                else {"type": "string", "maxLength": limit})
        if action == "assert_js":
            properties["observe"] = {"type": "string", "minLength": 1, "maxLength": 4000,
                "description": "可选：仅断言失败后在页面内采集诊断值（最多1200字符/1秒），不改写断言结论；不要修改页面来制造结果。"}
        variants.append({"type": "object", "additionalProperties": False,
                         "properties": properties, "required": [key for key in properties if key != "observe"]})
    return {"name": "browser_check",
        "description": "实际检查本群已发布静态网页的固定副本。artifact_seqs列HTML及全部本地依赖，或完整工程ZIP。ZIP默认读全部成员，可用archive_files明确选择其中页面依赖；最多128文件、单16MiB、总32MiB。支持目录和二进制资源，plan入口填实际HTML相对路径，至少一个assert_*。不访问任意网址、外网、登录态或工作目录，不启动后端。evaluate仅页面内测试准备，不可用自设状态冒充功能。outcome才是实际结论，status=ok仅表示拿到回执；失败先判断实现还是检查有误，修复后发布新版再检。",
        "input_schema": {"type": "object", "additionalProperties": False,
            "required": ["artifact_seqs", "plan"], "properties": {
                "artifact_seqs": {"type": "array", "minItems": 1, "maxItems": 128,
                    "items": {"type": "integer", "minimum": 1}},
                "archive_files": {"type": "array", "minItems": 1, "maxItems": 128,
                    "description": "可选：只检查所选ZIP中明确列出的成员，不表示覆盖未选文件。未列出的ZIP仍检查全部成员。",
                    "items": {"type": "object", "additionalProperties": False,
                        "required": ["artifact_seq", "paths"], "properties": {
                            "artifact_seq": {"type": "integer", "minimum": 1},
                            "paths": {"type": "array", "minItems": 1, "maxItems": 128,
                                      "items": {"type": "string", "maxLength": 2048}}}}},
                "plan": {"type": "object", "additionalProperties": False, "required": ["entrypoint", "steps"],
                    "properties": {"entrypoint": {"type": "string", "maxLength": 2048},
                        "steps": {"type": "array", "minItems": 1, "maxItems": MAX_BROWSER_STEPS,
                            "items": {"oneOf": variants}}}}}}}


def browser_environment():
    env = {"PATH": os.defpath, "TEMP": tempfile.gettempdir(), "TMP": tempfile.gettempdir()}
    for key in ("SystemRoot", "WINDIR", "HOME", "USERPROFILE", "LOCALAPPDATA", "XDG_CACHE_HOME",
                "PLAYWRIGHT_BROWSERS_PATH"):
        if key in os.environ:
            env[key] = os.environ[key]
    return env


class TeamBrowser:
    def __init__(self, team):
        self.team, self.rt = team, team.rt

    def _files(self, task_id, seqs, *, historical=False):
        if type(historical) is not bool:
            raise BrowserInputError("版本选择模式无效。")
        if (not isinstance(seqs, list) or not 1 <= len(seqs) <= 128
                or any(type(seq) is not int or seq < 1 for seq in seqs) or len(set(seqs)) != len(seqs)):
            raise BrowserInputError("请选择一至128份不重复的本群已发布文件。")
        members = {r.id for r in [self.rt.organization.leader, *self.rt.roles]}
        files = [e for e in self.rt.log.read(task_id=task_id) if e.type == "deliverable.published"
                 and e.actor.kind == "agent" and e.actor.id in members and isinstance(e.payload.get("filename"), str)]
        by_seq = {e.seq: e for e in files}
        latest = {e.payload["filename"]: e.seq for e in files}
        aliases = {}
        for name in latest:
            aliases.setdefault(unicodedata.normalize("NFC", name).casefold(), set()).add(name)
        selected = []
        for seq in seqs:
            event = by_seq.get(seq)
            if event is None:
                raise BrowserInputError("文件必须由本群已登记成员发布；请核对成果序号与来源。")
            if not historical and latest.get(event.payload["filename"]) != seq:
                raise BrowserInputError("文件必须是本群当前已发布版本；请读取最新成果序号后重试。")
            p = event.payload
            if not historical and len(aliases[unicodedata.normalize("NFC", p["filename"]).casefold()]) != 1:
                raise BrowserInputError("本群存在大小写或Unicode等价的不同文件名，无法确定同一解压目录的版本；请改用无歧义文件名。")
            if p.get("format") == "archive":
                from .workspace_archive import validate_manifest
                import re

                validate_manifest(p.get("archive"))
                if (type(p.get("content_bytes")) is not int or not 0 < p["content_bytes"] <= 65 * 1024 * 1024
                        or not isinstance(p.get("content_sha256"), str)
                        or re.fullmatch(r"[0-9a-f]{64}", p["content_sha256"]) is None):
                    raise BrowserInputError("工程归档缺少完整的大小或内容摘要。")
                selected.append({"seq": seq, **{k: p[k] for k in
                    ("filename", "content_sha256", "content_bytes", "format", "archive")}})
                continue
            content = p.get("content")
            if (not isinstance(content, str) or len(content.encode("utf-8")) > 16 * 1024 * 1024
                    or hashlib.sha256(content.encode("utf-8")).hexdigest() != p.get("content_sha256")):
                raise BrowserInputError("已发布文件缺少完整正文或可信的内容哈希。")
            selected.append({"seq": seq, **{k: p[k] for k in ("filename", "content", "content_sha256")}})
        if sum(len(a["content"].encode("utf-8")) for a in selected if "content" in a) > 32 * 1024 * 1024:
            raise BrowserInputError("检查文件总计超过32MiB。")
        return selected

    def _inputs(self, task_id, arguments, *, browser_paths=True):
        from .archive_members import read_members

        sources = self._files(task_id, arguments["artifact_seqs"])
        archives = {a["seq"]: a for a in sources if a.get("format") == "archive"}
        filters = {}
        if "archive_files" in arguments:
            selections = arguments["archive_files"]
            if not isinstance(selections, list) or not 1 <= len(selections) <= 128:
                raise BrowserInputError("archive_files需要明确的工程成员选择。")
            for selection in selections:
                if (not isinstance(selection, dict) or set(selection) != {"artifact_seq", "paths"}
                        or type(selection["artifact_seq"]) is not int or selection["artifact_seq"] not in archives
                        or selection["artifact_seq"] in filters):
                    raise BrowserInputError("只能为已选工程ZIP指定一次成员选择。")
                paths = selection["paths"]
                validate_browser_paths(paths)
                filters[selection["artifact_seq"]] = paths
        selected, total = [], 0
        for source in sources:
            if source["seq"] in archives:
                entries = source["archive"]["files"]
                paths = filters.get(source["seq"])
                if paths is not None:
                    by_path = {entry["path"]: entry for entry in entries}
                    if any(path not in by_path for path in paths):
                        raise BrowserInputError("所选工程成员不在该版本的文件清单中。")
                    entries = [entry for entry in entries if entry["path"] in paths]
                for entry in entries:
                    selected.append({"artifact_seq": source["seq"], **entry})
                    total += entry["bytes"]
            else:
                body = source["content"].encode("utf-8")
                selected.append({"artifact_seq": source["seq"], "path": source["filename"],
                    "bytes": len(body), "sha256": source["content_sha256"], "executable": False})
                total += len(body)
        if len(selected) > 128 or total > 32 * 1024 * 1024 or any(a["bytes"] > 16 * 1024 * 1024 for a in selected):
            raise BrowserInputError("浏览器副本限128文件、单文件16MiB、总32MiB；请用archive_files选择实际页面依赖，不发布或检查节选文件。")
        relative = bool(archives)
        if not browser_paths:
            from .workspace_archive import _paths

            _paths([a["path"] for a in selected])
        elif relative:
            validate_browser_paths([a["path"] for a in selected])
        files = []
        for source in sources:
            if source["seq"] in archives:
                files.extend({"artifact_seq": source["seq"], **entry, "sha256": entry["sha256"].lower()}
                    for entry in read_members(self.rt.log, source, filters.get(source["seq"])))
            else:
                files.append({"artifact_seq": source["seq"], "path": source["filename"],
                    "body": source["content"].encode("utf-8"), "bytes": len(source["content"].encode("utf-8")),
                    "sha256": source["content_sha256"], "executable": False})
        return sources, files, relative

    def execute(self, task_id, arguments, *, current, started_seq=None):
        if not self.rt.browser_checks.command:
            return {"status": "error", "outcome": "unavailable", "message": "尚未配置实际浏览器检查器。"}
        try:
            if (not isinstance(arguments, dict) or not {"artifact_seqs", "plan"} <= set(arguments)
                    or set(arguments) - {"artifact_seqs", "plan", "archive_files"}):
                raise BrowserInputError("浏览器检查只接受已发布文件序号和检查计划。")
            sources, files, relative = self._inputs(task_id, arguments)
            plan = normalize_plan(arguments["plan"], [a["path"] for a in files], relative_paths=relative)
        except (BrowserInputError, ValueError) as exc:
            return {"status": "error", "code": "invalid_browser_check", "message": str(exc)}
        scope = self.team.recovery.scope(task_id)
        current()
        if scope["control_state"] != "running":
            raise WorkConflict("当前执行已暂停或取消，未启动浏览器检查。")
        unknown = [r for r in self.team._pending_operations(task_id)
                   if r["name"] == "browser_check" and r["started_seq"] != started_seq]
        if unknown:
            return {"status": "error", "code": "browser_check_pending", "started_seqs": [r["started_seq"] for r in unknown],
                "message": "旧浏览器调用仍未结算，不能重复启动。先读取已有回执；结果未知时由可信本地操作者确认旧执行停止并核对，不以重跑代替核对。"}
        with tempfile.TemporaryDirectory(prefix="cater-team-browser-") as tmp:
            from .artifact_store import _directories, _write

            root = Path(tmp)
            for artifact in files:
                target = root / artifact["path"]
                _directories(target.parent, create=True)
                _write(target, artifact["body"])
            _write(root / PLAN_FILE, json.dumps(plan, ensure_ascii=False).encode("utf-8"))
            result = run_check([*self.rt.browser_checks.command, "--plan", PLAN_FILE], cwd=root,
                timeout=self.rt.browser_checks.timeout, record_output=True, env=browser_environment())
            try:
                from .artifact_store import _directories, _inspect, _read
                for a in files:
                    path = root / a["path"]
                    _directories(path.parent)
                    _inspect(path, False)
                    if hashlib.sha256(_read(path, 16 * 1024 * 1024)).hexdigest() != a["sha256"]:
                        raise ValueError("copy changed")
                unchanged = True
            except (OSError, ValueError):
                unchanged = False
        payload = browser_receipt(result, plan)
        if result.payload["outcome"] in {"timeout", "launch_error"}:
            payload["outcome"] = result.payload["outcome"]
        if not unchanged:
            payload.update(outcome="unavailable", browser_report={"outcome": "unavailable",
                "reason": "检查副本在执行期间发生变化，不能将其当作原发布版本的检查。"})
        same_scope = self.team.recovery.scope(task_id) == scope
        try:
            current()
            same_scope &= self._inputs(task_id, arguments) == (sources, files, relative)
        except (WorkConflict, ValueError, KeyError, OSError):
            same_scope = False
        return {"status": "ok" if payload["outcome"] in {"passed", "failed"} else "error", **payload,
            "scope_current": same_scope, "artifacts_unchanged": unchanged,
            "complete": bool(not result.payload.get("cleanup_error") and result.payload["stdout"].get("complete") is True
                and result.payload["stderr"].get("complete") is True),
            "invocation_stopped": bool(result.payload.get("process_scope") in {"windows_job", "posix_process_group"}
                and not result.payload.get("cleanup_error") and result.payload["stdout"].get("complete") is True
                and result.payload["stderr"].get("complete") is True),
            "checked_artifacts": [{k: a[k] for k in ("seq", "filename", "content_sha256")} for a in sources],
            **({"checked_files": [{k: a[k] for k in ("artifact_seq", "path", "bytes", "sha256")} for a in files],
                "archive_files": arguments.get("archive_files", [])} if relative else {}),
            **{k: result.payload[k] for k in ("exit_code", "duration_ms", "process_scope", "stdout", "stderr")},
            "stdout": output_metadata(result.payload["stdout"]),
            "note": ("仅检查固定已发布文件副本；截图只保留元数据，原临时文件已清理。"
                     + ("已校验ZIP摘要及全部成员元数据；浏览器只检查列出的成员，不代表覆盖工程内其他文件。" if relative else "")
                     + ("目标、控制或文件版本已变化，本回执仅作历史记录。" if not same_scope else ""
                        "回执不等于业务覆盖完整或伙伴验收通过。"))}
