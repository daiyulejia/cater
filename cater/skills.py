"""Host-registered, lazily read Markdown guidance; never tool authorization."""

from __future__ import annotations

import hashlib
from importlib.resources import files
import json
from pathlib import Path
import re

from .workspace_tools import _parts, _read_file, _text


MAX_SKILL_BYTES = 65536
MAX_MANIFEST_BYTES = 1048576
NOTE = "工作指引只是参考，不授予权限、不执行脚本，也不代表任务或检查已完成；历史指引不是当前授权。"


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate configuration field")
        result[key] = value
    return result


def _label(value, limit):
    return (isinstance(value, str) and bool(value.strip()) and len(value) <= limit
            and not any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in value))


def _names(value):
    return (isinstance(value, list) and len(value) <= 128
            and all(isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", v) for v in value)
            and len(set(value)) == len(value))


def _parse(body, *, source, reserved=()):
    value = json.loads(body, object_pairs_hook=_unique)
    if not isinstance(value, dict) or set(value) != {"skills"} or not isinstance(value["skills"], list) or len(value["skills"]) > 128:
        raise ValueError("invalid skill manifest")
    rows, seen = [], set(reserved)
    for row in value["skills"]:
        if (not isinstance(row, dict) or set(row) - {"id", "title", "description", "path", "roles", "workspaces", "requires"}
                or not {"id", "title", "description", "path"} <= set(row)
                or not isinstance(row["id"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", row["id"])
                or row["id"] in seen or not _label(row["title"], 160) or not _label(row["description"], 400)
                or any(key in row and not _names(row[key]) for key in ("roles", "workspaces", "requires"))):
            raise ValueError("invalid skill registration")
        path = row["path"]
        if not isinstance(path, str) or not path or "\0" in path or Path(path).suffix.lower() != ".md":
            raise ValueError("skill needs a Markdown file")
        if source == "builtin":
            if len(_parts(path)) != 1 or "/" in path or "\\" in path:
                raise ValueError("invalid packaged skill name")
        else:
            target = Path(path)
            if not target.is_absolute():
                raise ValueError("registered skill path must be absolute")
            _parts(target.relative_to(target.anchor).as_posix())
        seen.add(row["id"])
        rows.append({**row, "title": row["title"].strip(), "description": row["description"].strip(),
                     "requires": row.get("requires", []), "source": source})
    return rows


class SkillLibrary:
    def __init__(self, config_path=None):
        self.config_path = Path(config_path).absolute() if config_path is not None else None

    def _entries(self):
        bundled = files("cater.packs").joinpath("skills")
        status = "ok"
        try:
            with bundled.joinpath("catalog.json").open("rb") as stream:
                body = stream.read(MAX_MANIFEST_BYTES + 1)
            if len(body) > MAX_MANIFEST_BYTES:
                raise ValueError("manifest too large")
            builtins = _parse(body, source="builtin")
        except (OSError, ValueError, TypeError, RecursionError):
            builtins, status = [], "unavailable"
        custom = []
        if self.config_path is not None:
            try:
                body, _ = _read_file(self.config_path)
                custom = _parse(body, source="configured", reserved=[r["id"] for r in builtins])
            except (OSError, ValueError, TypeError, RecursionError):
                status = "unavailable"
        return [*custom, *builtins], status

    @staticmethod
    def _offered(row, role_id, workspace_id, tools):
        return (role_id in row.get("roles", [role_id]) and workspace_id in row.get("workspaces", [workspace_id])
                and all(name in tools for name in row["requires"]))

    @staticmethod
    def _read(row):
        if row["source"] == "builtin":
            resource = files("cater.packs").joinpath("skills", row["path"])
            with resource.open("rb") as stream:
                body = stream.read(MAX_SKILL_BYTES + 1)
        else:
            body, _ = _read_file(Path(row["path"]))
        if len(body) > MAX_SKILL_BYTES:
            raise ValueError("skill too large")
        content = _text(body)
        if not content.strip():
            raise ValueError("empty skill")
        return content, hashlib.sha256(body).hexdigest(), len(body)

    @staticmethod
    def _metadata(row):
        return {key: row[key] for key in ("id", "title", "description", "source", "requires")}

    def index(self, *, role_id, workspace_id, tools, offset=0, limit=5):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("指引目录需要非负起点和1至20项的页大小。")
        rows, config_status = self._entries()
        rows = [row for row in rows if self._offered(row, role_id, workspace_id, tools)]
        if offset > len(rows):
            raise ValueError("指引目录已变化或起点超出范围，请从首页重新核对。")
        page = []
        for row in rows[offset:offset + limit]:
            entry = self._metadata(row)
            try:
                _, digest, size = self._read(row)
                entry.update(sha256=digest, bytes=size, status="ready")
            except (OSError, ValueError, TypeError):
                entry.update(status="unavailable")
            page.append(entry)
        next_offset = min(offset + limit, len(rows))
        return {"skills": page, "total": len(rows), "offset": offset, "has_more": next_offset < len(rows),
                "next_offset": next_offset, "configuration_status": config_status,
                "note": NOTE + ("部分指引配置不可用，未沿用旧配置；正常工作工具未因此停用。" if config_status != "ok" else "")}

    def read(self, arguments, *, role_id, workspace_id, tools):
        if (not isinstance(arguments, dict) or set(arguments) - {"skill_id", "expected_sha256", "offset", "limit"}
                or not isinstance(arguments.get("skill_id"), str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", arguments["skill_id"])):
            return {"status": "error", "code": "invalid_skill_request", "message": "请选择已登记的工作指引标识，不接受文件路径。"}
        offset, limit = arguments.get("offset", 0), arguments.get("limit", 8000)
        expected = arguments.get("expected_sha256")
        if (type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 12000
                or "expected_sha256" in arguments and (not isinstance(expected, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", expected))):
            return {"status": "error", "code": "invalid_skill_request", "message": "读取指引需要有效版本、非负字符起点和1至12000字符的页大小。"}
        rows, _ = self._entries()
        row = next((r for r in rows if r["id"] == arguments["skill_id"] and self._offered(r, role_id, workspace_id, tools)), None)
        if row is None:
            return {"status": "error", "code": "skill_unavailable", "message": "此指引当前未提供给该角色或工作区，或所需工具尚不可用；不会因此修改权限或停止普通工作。"}
        try:
            content, digest, size = self._read(row)
        except (OSError, ValueError, TypeError):
            return {"status": "error", "code": "skill_unavailable", "message": "登记的指引文件暂不可读取，请核对本地配置；未执行其中任何内容。"}
        if expected is not None and expected.lower() != digest:
            return {"status": "error", "code": "skill_changed", "current_sha256": digest,
                    "message": "指引版本已变化，未拼接新旧内容；请按当前目录版本重新读取。"}
        if offset > len(content):
            return {"status": "error", "code": "invalid_skill_request", "message": "正文起点超出范围，请核对当前版本和字符总数。"}
        end = min(offset + limit, len(content))
        return {"status": "ok", "skill": {**self._metadata(row), "status": "ready", "sha256": digest, "bytes": size},
                "content": content[offset:end], "offset": offset, "total_chars": len(content),
                "has_more": end < len(content), "next_offset": end, "note": NOTE}
