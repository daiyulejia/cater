"""Read-only, version-bound project outcomes; never a global chat memory."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3

from .artifacts import safe_filename
from .events import Actor, GENESIS_HASH
from .json_fields import UNAMBIGUOUS_OBJECT_SQL
from .tool_protocol import json_utf8
from .workspace_archive import MAX_ARCHIVE_BYTES, portable_path, validate_manifest


TOOL = "read_project_history"
HOST = Actor.system("team")
NOTE = ("同项目历史交付仅供参考，不是当前目标、执行权限或正确性证明；店长当时的验证说明不等于独立实测。"
        "读取的是持久版本，不是当前磁盘文件。先按当前任务核对，再决定是否复用；衍生群只读取自己的冻结背景。")
_OUTCOMES = ("team.started", "team.run_started", "team.objective_updated", "team.objective_completed")
_SHA = re.compile(r"[a-f0-9]{64}\Z")
MAX_INDEX_BYTES = 16000


def _positive(value):
    return type(value) is int and value > 0


def _text(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 32000:
        return False
    try:
        value.encode("utf-8")
    except UnicodeError:
        return False
    return True


def _excerpt(value):
    raw = value.encode("utf-8")
    return raw[:320].decode("utf-8", errors="ignore"), len(raw) > 320


def _rejected(code="project_history_unavailable"):
    return {"status": "error", "code": code, "complete": True, "outcome": "not_started",
            "message": "无法读取这份项目历史背景；请核对本群项目、目录版本与引用，不查询其他群的任意内容。"}


class _MetadataUnavailable(ValueError):
    pass


class _ScopeChanged(ValueError):
    pass


class ProjectHistory:
    def __init__(self, team):
        self.team, self.rt = team, team.rt

    def _anchor(self, seq=None):
        if seq is not None and (type(seq) is not int or not 0 <= seq < 2 ** 63 - 1):
            raise ValueError("项目背景版本需要非负事件序号。")
        if seq == 0:
            return {"as_of_seq": 0, "as_of_hash": GENESIS_HASH}
        row = (self.rt.log.db.execute("SELECT seq, hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
               if seq is None else self.rt.log.db.execute("SELECT seq, hash FROM events WHERE seq = ?", (seq,)).fetchone())
        if row is None and seq is not None:
            raise ValueError("找不到项目背景版本。")
        return {"as_of_seq": row["seq"] if row else 0, "as_of_hash": row["hash"] if row else GENESIS_HASH}

    def _state(self, seq):
        projects, tasks = {}, {}
        # SQL projects metadata only; frozen bodies never become Python objects.
        # SQLite still parses its stored JSON; this is not a constant-I/O claim.
        fields = {"id": 128, "name": 120, "project_id": 128}
        columns = [f"CASE WHEN json_type(payload_json, '$.{key}') = 'text' "
                   f"AND length(json_extract(payload_json, '$.{key}')) <= {limit} "
                   f"THEN CAST(json_extract(payload_json, '$.{key}') AS BLOB) END AS {key}"
                   for key, limit in fields.items()]
        try:
            ambiguous = self.rt.log.db.execute("SELECT 1 FROM events WHERE seq < ? "
                "AND type IN ('project.created', 'task.created', 'task.project_bound') "
                f"AND NOT ({UNAMBIGUOUS_OBJECT_SQL}) LIMIT 1", (seq + 1,)).fetchone()
            if ambiguous:
                raise _MetadataUnavailable("项目历史元数据存在歧义，未猜测项目范围。")
            cursor = self.rt.log.db.execute("SELECT seq, task_id, actor_json, type, " + ", ".join(columns)
                + ", (COALESCE(json_type(payload_json, '$.background'), 'null') <> 'null' "
                + "OR COALESCE(json_extract(payload_json, '$.context_scope') = 'task', 0)) AS isolated "
                + "FROM events WHERE seq < ? AND type IN ('project.created', 'task.created', 'task.project_bound') "
                + "AND json_valid(payload_json) ORDER BY seq", (seq + 1,))
            try:
                for row in cursor:
                    try:
                        actor = Actor(**json.loads(row["actor_json"]))
                        values = {key: row[key].decode("utf-8") if row[key] is not None else None for key in fields}
                    except (ValueError, TypeError, UnicodeError):
                        continue
                    if actor != Actor.human():
                        continue
                    task_id, kind = row["task_id"], row["type"]
                    if kind == "project.created" and values["id"] and _text(values["name"]):
                        projects.setdefault(values["id"], values["name"])
                    elif kind == "task.created" and task_id and len(task_id) <= 128 and task_id not in tasks:
                        project = values["project_id"]
                        tasks[task_id] = {"created_seq": row["seq"], "project_id": project if project in projects else None,
                                          "isolated": bool(row["isolated"])}
                    elif kind == "task.project_bound" and task_id in tasks and values["project_id"] in projects:
                        tasks[task_id]["project_id"] = values["project_id"]
            finally:
                cursor.close()
        except sqlite3.Error:
            raise _MetadataUnavailable("项目历史元数据查询不可用，未回退读取全部背景正文。") from None
        return tasks

    def _goal(self, created_seq):
        try:
            row = self.rt.log.db.execute("SELECT CASE WHEN json_type(payload_json, '$.goal') = 'text' "
                "AND length(json_extract(payload_json, '$.goal')) <= 32000 "
                "THEN CAST(json_extract(payload_json, '$.goal') AS BLOB) END AS goal FROM events WHERE seq = ? "
                f"AND ({UNAMBIGUOUS_OBJECT_SQL})",
                (created_seq,)).fetchone()
            return row["goal"].decode("utf-8") if row and row["goal"] is not None else None
        except UnicodeError:
            return None
        except sqlite3.Error:
            raise _MetadataUnavailable("项目历史目标查询不可用。") from None

    def available(self, task_id):
        try:
            current = self._state(self._anchor()["as_of_seq"]).get(task_id)
        except _MetadataUnavailable:
            return False
        return bool(current and current["project_id"] and not current["isolated"])

    @staticmethod
    def public_arguments(arguments):
        if not isinstance(arguments, dict):
            return {}
        result = {key: value for key, value in arguments.items()
                if (key == "section" and isinstance(value, str) and value in {"index", "delivery", "artifact"}
                    or key in {"as_of_seq", "before_seq", "limit", "delivery_seq", "artifact_seq", "offset", "length"}
                    and type(value) is int and 0 <= value <= 2 ** 63 - 1)}
        if arguments.get("section") == "artifact" and "path" in arguments:
            try:
                path = portable_path(arguments["path"])
                if len(path) <= 2048:
                    result["path"] = path
            except ValueError:
                pass
        return result

    @staticmethod
    def _refs(value, delivery_seq):
        if not isinstance(value, list) or len(value) > 128:
            return None
        seqs, names = set(), set()
        for ref in value:
            if (not isinstance(ref, dict) or set(ref) != {"seq", "filename", "content_sha256"}
                    or not _positive(ref["seq"]) or ref["seq"] >= delivery_seq
                    or safe_filename(ref["filename"]) != ref["filename"] or ref["filename"] is None
                    or not isinstance(ref["content_sha256"], str) or not _SHA.fullmatch(ref["content_sha256"])
                    or ref["seq"] in seqs or ref["filename"] in names):
                return None
            seqs.add(ref["seq"])
            names.add(ref["filename"])
        return value

    def _latest(self, task_id, goal, seq):
        boundary, objective, started, runs, latest = None, goal, False, {}, None
        for event in self.rt.log.iter_events(task_id=task_id, event_types=_OUTCOMES, before_seq=seq + 1):
            if event.actor != HOST:
                continue
            p = event.payload
            if event.type == "team.started":
                started = type(p.get("version")) is int and p["version"] == 1
                boundary = event.seq
            elif event.type == "team.objective_updated":
                boundary, objective = event.seq, p.get("goal")
            elif event.type == "team.run_started" and isinstance(p.get("run_id"), str):
                runs[p["run_id"]] = event
            elif event.type == "team.objective_completed":
                run = runs.get(p.get("run_id")) if isinstance(p.get("run_id"), str) else None
                refs = self._refs(p.get("artifacts"), event.seq)
                if (not started or run is None or run.seq >= event.seq or refs is None
                        or not isinstance(run.payload.get("role"), str) or not run.payload["role"]
                        or not _positive(run.payload.get("message_seq")) or run.payload["message_seq"] >= run.seq
                        or type(p.get("objective_seq")) is not int or p["objective_seq"] != boundary
                        or not _text(objective) or not _text(p.get("summary")) or not _text(p.get("verification"))):
                    continue
                source_seq = run.payload["message_seq"]
                source = next(self.rt.log.iter_events(task_id=task_id, event_types=("team.message_received",),
                    after_seq=source_seq - 1, before_seq=source_seq + 1), None)
                if source is None or source.payload.get("recipient") != run.payload["role"]:
                    continue
                latest = {"event": event, "objective": objective, "artifacts": refs}
        return latest

    def _catalog(self, task_id, as_of_seq=None, *, expected_project_id=None):
        head = self._anchor()
        current = self._state(head["as_of_seq"])
        target = current.get(task_id)
        if not target or not target["project_id"] or target["isolated"]:
            if expected_project_id is not None:
                raise _ScopeChanged("项目范围已不可用。")
            return None
        if expected_project_id is not None and target["project_id"] != expected_project_id:
            raise _ScopeChanged("项目范围已变化。")
        anchor = head if as_of_seq is None else self._anchor(as_of_seq)
        past = current if anchor == head else self._state(anchor["as_of_seq"])
        original = past.get(task_id)
        if not original or original["project_id"] != target["project_id"] or original["isolated"]:
            raise ValueError("本群项目与历史目录版本不一致。")
        rows = []
        for source, state in past.items():
            now = current.get(source)
            if (source == task_id or state["isolated"] or state["project_id"] != target["project_id"]
                    or now is None or now["isolated"] or now["project_id"] != target["project_id"]):
                continue
            latest = self._latest(source, self._goal(state["created_seq"]), anchor["as_of_seq"])
            if latest:
                rows.append(latest)
        return {**anchor, "project_id": target["project_id"], "rows": sorted(rows, key=lambda row: row["event"].seq, reverse=True)}

    def index(self, task_id, *, as_of_seq=None, before_seq=None, limit=3, expected_project_id=None):
        if type(limit) is not int or not 1 <= limit <= 10 or before_seq is not None and not _positive(before_seq):
            raise ValueError("项目背景目录需要有效起点和1至10项页大小。")
        try:
            catalog = self._catalog(task_id, as_of_seq, expected_project_id=expected_project_id)
        except _MetadataUnavailable:
            return {"available": False, "reason": "metadata_query_unavailable", "historical_only": True, "note": NOTE}
        if catalog is None:
            return {"available": False, "reason": "scope_unavailable", "historical_only": True, "note": NOTE}
        if before_seq is not None and before_seq > catalog["as_of_seq"] + 1:
            raise ValueError("项目背景起点超出目录版本。")
        rows = [row for row in catalog["rows"] if before_seq is None or row["event"].seq < before_seq]
        entries = []
        for row in rows[:limit]:
            event = row["event"]
            objective, goal_cut = _excerpt(row["objective"])
            summary, summary_cut = _excerpt(event.payload["summary"])
            entry = {"task_id": event.task_id, "delivery_seq": event.seq, "delivery_hash": event.hash,
                            "objective_excerpt": objective, "objective_truncated": goal_cut,
                            "summary_excerpt": summary, "summary_truncated": summary_cut,
                            "artifact_count": len(row["artifacts"]), "artifacts": row["artifacts"][:3],
                            "artifacts_truncated": len(row["artifacts"]) > 3, "verification_kind": "manager_declared"}
            if len(json_utf8([*entries, entry])) > MAX_INDEX_BYTES - 2048:
                break
            entries.append(entry)
        if rows and not entries:
            raise ValueError("项目背景元数据过大，无法完整列出本条引用。")
        return {"available": True, **{k: catalog[k] for k in ("project_id", "as_of_seq", "as_of_hash")},
                "entries": entries, "total": len(catalog["rows"]), "before_seq": before_seq,
                "has_more": len(rows) > len(entries), "next_before_seq": entries[-1]["delivery_seq"] if len(rows) > len(entries) else None,
                "historical_only": True, "note": NOTE}

    def read(self, task_id, arguments, *, expected_project_id=None):
        allowed = {"section", "as_of_seq", "before_seq", "limit", "delivery_seq", "artifact_seq", "path", "offset", "length"}
        if (not isinstance(arguments, dict) or set(arguments) - allowed or not isinstance(arguments.get("section"), str)
                or arguments["section"] not in {"index", "delivery", "artifact"}
                or "as_of_seq" in arguments and type(arguments["as_of_seq"]) is not int
                or "before_seq" in arguments and type(arguments["before_seq"]) is not int):
            return _rejected("invalid_project_history_arguments")
        section = arguments["section"]
        keys = {"section", "as_of_seq", "before_seq", "limit"} if section == "index" else {
            "section", "as_of_seq", "delivery_seq", "offset", "length", *({"artifact_seq", "path"} if section == "artifact" else set())}
        if set(arguments) - keys:
            return _rejected("invalid_project_history_arguments")
        try:
            if section == "index":
                result = self.index(task_id, **{k: v for k, v in arguments.items() if k != "section"},
                                    expected_project_id=expected_project_id,
                                    **({"limit": 5} if "limit" not in arguments else {}))
                return {"status": "ok", "complete": True, "section": section, **result} if result["available"] else _rejected()
            offset, length = arguments.get("offset", 0), arguments.get("length", 8000)
            if "path" in arguments and len(portable_path(arguments["path"])) > 2048:
                return _rejected("invalid_project_history_arguments")
            if (not _positive(arguments.get("delivery_seq")) or type(offset) is not int or offset < 0
                    or type(length) is not int or not 1 <= length <= 16000
                    or section == "artifact" and not _positive(arguments.get("artifact_seq"))):
                return _rejected("invalid_project_history_arguments")
            catalog = self._catalog(task_id, arguments.get("as_of_seq"), expected_project_id=expected_project_id)
            if catalog is None:
                return _rejected()
            selected = next((row for row in catalog["rows"] if row["event"].seq == arguments["delivery_seq"]), None)
            if selected is None:
                return _rejected()
            event = selected["event"]
            source = {"project_id": catalog["project_id"], "task_id": event.task_id,
                      "delivery_seq": event.seq, "delivery_hash": event.hash}
            if section == "delivery":
                content = json_utf8({"objective": selected["objective"], "artifacts": selected["artifacts"],
                    **{k: event.payload[k] for k in ("summary", "verification", "handoff") if k in event.payload}},
                    allow_nan=False).decode("utf-8")
            else:
                ref = next((row for row in selected["artifacts"] if row["seq"] == arguments["artifact_seq"]), None)
                if ref is None:
                    return _rejected()
                artifact = next(self.rt.log.iter_events(task_id=event.task_id, event_types=("deliverable.published",),
                                                       after_seq=ref["seq"] - 1, before_seq=ref["seq"] + 1), None)
                if (artifact is None or artifact.actor.kind != "agent" or artifact.payload.get("filename") != ref["filename"]
                        or artifact.payload.get("content_sha256") != ref["content_sha256"]):
                    return _rejected("project_history_content_unavailable")
                source.update(artifact_seq=artifact.seq, content_sha256=ref["content_sha256"], filename=ref["filename"], author=artifact.actor.id)
                if artifact.payload.get("format") == "archive":
                    return self._read_archive(artifact, arguments, catalog, source)
                if "path" in arguments:
                    return _rejected("invalid_project_history_arguments")
                content = artifact.payload.get("content")
                if not isinstance(content, str) or hashlib.sha256(content.encode("utf-8")).hexdigest() != ref["content_sha256"]:
                    return _rejected("project_history_content_unavailable")
            if offset > len(content):
                return _rejected("invalid_project_history_arguments")
            end = min(len(content), offset + length)
            return {"status": "ok", "complete": True, "section": section, "historical_only": True,
                    **{k: catalog[k] for k in ("as_of_seq", "as_of_hash")}, "source": source,
                    "content": content[offset:end], "offset": offset, "total_chars": len(content),
                    "next_offset": end if end < len(content) else None, "has_more": end < len(content), "note": NOTE}
        except _ScopeChanged:
            return _rejected("project_history_scope_changed")
        except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
            return _rejected("invalid_project_history_arguments")

    def _read_archive(self, event, arguments, catalog, source):
        from .team_archive import read_member

        if "path" not in arguments and any(key in arguments for key in ("offset", "length")):
            return _rejected("invalid_project_history_arguments")
        try:
            artifact = {**event.payload, "seq": event.seq}
            if (type(artifact.get("content_bytes")) is not int
                    or not 0 < artifact["content_bytes"] <= MAX_ARCHIVE_BYTES
                    or not isinstance(event.hash, str) or not _SHA.fullmatch(event.hash)):
                return _rejected("project_history_content_unavailable")
            validate_manifest(artifact.get("archive"))
            if "path" in arguments:
                value = read_member(self.rt.log, artifact, {"path": arguments["path"],
                    "offset": arguments.get("offset", 0), "length": arguments.get("length", 8000)})
            else:
                value = {"status": "ok", **{key: artifact[key] for key in
                    ("seq", "filename", "format", "content_sha256", "content_bytes", "archive")}}
            return {"status": "ok", "complete": True, "section": "artifact", "historical_only": True,
                    **{key: catalog[key] for key in ("as_of_seq", "as_of_hash")},
                    "source": {**source, "format": "archive", "event_hash": event.hash},
                    "offset": arguments.get("offset", 0), "archive_read": value, "note": NOTE}
        except Exception:
            # Archive/DB exceptions can contain file paths or bytes; return no partial data.
            return _rejected("project_history_content_unavailable")
