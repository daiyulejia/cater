"""Task-local observed file reads, not a filesystem cache or baseline authority."""

from __future__ import annotations

import json
import re
import sqlite3
from types import SimpleNamespace

from .events import Actor
from .json_fields import UNAMBIGUOUS_OBJECT_SQL
from .tool_protocol import json_utf8
from .workspace_tools import _parts, DEFAULT_READ_BYTES, MAX_FILE_BYTES, MAX_READ_BYTES


HOST = Actor.system("team")
MAX_INDEX_BYTES = 8000
RECEIPT_INDEX_NOTE = ("完成回执索引，不含stdin或输出正文，超长参数省略。result_summary保留原操作状态，查询ok不代表操作成功。"
                      "command_identity区分请求与实际argv；缺失字段不代表默认值。已含所需字段可据实回答，详情按需读取，不必逐条展开。"
                      "历史读取不重执行、不证明当前文件或验收通过。"
                      "空列表不证明没有历史或未结算操作，pending仍需单独查询。")
NOTE = ("这里只列当前工作目录绑定期间已记录的成功读取，不是磁盘扫描。"
        "first_observed不等于原始基线，last_observed不等于当前文件；目标变化后仍可能是历史。"
        "读取可能为节选，按read_args读回确切回执再比较；历史不授权写入，也不证明解释正确。")


def invalid_query(message, *fields):
    """Only caller-authored input diagnostics belong here, never exception text."""
    return {"status": "error", "code": "invalid_receipt_query", "complete": True,
            "outcome": "not_started", "message": message, "fields": list(fields)}

_SOURCE_FIELDS = tuple((f"$.{key}", maximum) for key, maximum in {
    "started_seq": 0, "run_id": 128, "role": 128, "name": 128, "call_id": 256,
    "message_seq": 0, "root": 8192, "revision": 0, "recipient": 128, "kind": 128,
    "arguments.path": 8192, "arguments.offset": 0, "arguments.limit": 0,
    "recovery_of": 0, "interrupted_seq": 0, "recovery_hash": 64, "trigger": 128,
    "resume_seq": 0, "resume_hash": 64, "previous_seq": 0, "action": 128, "state": 128,
    "recovery_contract": 4096, "recovery_scope": 4096, "scope": 4096,
    "status": 128, "interruption_kind": 128,
}.items())
_READ_FIELDS = tuple((f"$.{key}", maximum) for key, maximum in {
    "started_seq": 0, "run_id": 128, "role": 128, "name": 128, "call_id": 256,
    "result.status": 128, "result.path": 8192, "result.sha256": 64,
    "result.bytes": 0, "result.offset": 0, "result.returned_bytes": 0,
    "result.encoding": 32, "result.truncated": 0,
}.items())
_BINDING_FIELDS = tuple((f"$.{key}", maximum) for key, maximum in {
    "workspace_id": 128, "root": 8192, "fingerprint": 256,
}.items())
_RECEIPT_INDEX_FIELDS = tuple((f"$.{key}", maximum) for key, maximum in {
    "started_seq": 0, "run_id": 128, "role": 128, "name": 128, "call_id": 256,
    "result.status": 128, "result.outcome": 128, "result.code": 128,
    "result.exit_code": 0, "result.complete": 0, "result.truncated": 0,
    "result.timed_out": 0, "result.command": 128, "result.duration_ms": 0,
    "result.bytes": 0,
}.items())


def file_path(value):
    parts = _parts(value)
    if not parts:
        raise ValueError("文件来源需要具体的相对文件路径。")
    return "/".join(parts)


def _integer(value, lower, upper):
    return type(value) is int and lower <= value <= upper


def _unambiguous(pairs):
    result = {}
    for key, value in pairs:
        if key in result or "\0" in key:
            raise ValueError("Ambiguous recorded object")
        result[key] = value
    return result


def bound_receipts(team, task_id, events):
    """Join host receipts to their actual input/run, without creating authority."""
    events = [e for e in events if e.task_id == task_id]
    by_seq = {e.seq: e for e in events}
    members = {r.id for r in [team.rt.organization.leader, *team.rt.roles]}
    messages = {e.seq: e for e in team._messages(task_id, events=events)}
    runs, endings = {}, {}
    for event in events:
        p = event.payload
        if (event.actor != HOST or event.type not in {"team.run_started", "team.run_finished"}
                or not isinstance(p.get("run_id"), str) or not p["run_id"].strip()
                or not isinstance(p.get("role"), str) or type(p.get("message_seq")) is not int):
            continue
        key = (p["run_id"], p["role"], p["message_seq"])
        (runs if event.type == "team.run_started" else endings).setdefault(key, []).append(event)
    for event in events:
        p = event.payload
        if event.actor != HOST or event.type != "team.tool_finished" or type(p.get("started_seq")) is not int:
            continue
        start = by_seq.get(p["started_seq"])
        if start is None or start.actor != HOST or start.type != "team.tool_started" or start.seq >= event.seq:
            continue
        s = start.payload
        if (not isinstance(s.get("role"), str) or s["role"] not in members
                or not isinstance(s.get("run_id"), str) or not 1 <= len(s["run_id"]) <= 128
                or type(s.get("message_seq")) is not int or s["message_seq"] < 1
                or not isinstance(s.get("name"), str) or not s["name"]
                or not isinstance(s.get("call_id"), str) or not 1 <= len(s["call_id"]) <= 256
                or not s["call_id"].strip()
                or any(p.get(k) != s.get(k) for k in ("name", "run_id", "role"))
                or "call_id" in p and p["call_id"] != s["call_id"]):
            continue
        key = (s["run_id"], s["role"], s["message_seq"])
        candidates = runs.get(key, [])
        run = candidates[0] if len(candidates) == 1 and candidates[0].seq < start.seq else None
        message = messages.get(s["message_seq"])
        if (message is None or run is None or not message.seq < run.seq
                or message.payload.get("recipient") != s["role"]
                or any(e.seq < event.seq for e in endings.get(key, []))
                or not isinstance(p.get("result"), dict) or p["result"].get("status") not in ("ok", "error")):
            continue
        yield event, start


class TeamReadSources:
    def __init__(self, team):
        self.team, self.rt = team, team.rt

    def exact(self, task_id, arguments):
        """Decode the requested body only; legacy list queries remain separate."""
        seq = arguments["receipt_seq"]
        if seq >= 2 ** 63:
            return []
        row = self.rt.log.db.execute("SELECT * FROM events WHERE seq = ? AND task_id = ? "
            "AND type = 'team.tool_finished'", (seq, task_id)).fetchone()
        if row is None:
            return []
        try:
            json.loads(row["payload_json"], object_pairs_hook=_unambiguous)
            json.loads(row["actor_json"], object_pairs_hook=_unambiguous)
            event = self.rt.log._row_to_event(row)
        except (ValueError, TypeError, UnicodeError):
            return []
        p = event.payload
        if (event.actor != HOST or not isinstance(p, dict) or p.get("name") == "read_receipts"
                or any(key in arguments and p.get(key) != arguments[key] for key in ("run_id", "role", "name"))
                or "before_seq" in arguments and event.seq >= arguments["before_seq"]):
            return []
        start = self._chain(task_id, event)
        if start is None or "message_seq" in arguments and start.payload.get("message_seq") != arguments["message_seq"]:
            return []
        result = {**p, "seq": event.seq, "message_seq": start.payload["message_seq"]}
        result.pop("observations", None)
        if p.get("name") == "workspace_run":
            from .receipt_observations import receipt_observations

            if observations := receipt_observations(p.get("result")):
                result["observations"] = [{"source_seq": event.seq, "source_hash": event.hash, **value}
                                          for value in observations]
        return [result]

    def receipts_index(self, task_id, arguments, record_read):
        """Discover bounded metadata; fetch a chosen body only through exact()."""
        conditions, parameters = ["json_extract(payload_json, '$.name') != 'read_receipts'",
            # exact() rejects ambiguous keys at every depth. Keep discovery
            # consistent without decoding scripts or outputs into Python.
            "NOT EXISTS (SELECT 1 FROM json_tree(payload_json) WHERE typeof(key) = 'text' "
            "GROUP BY parent, CAST(key AS BLOB) HAVING count(*) > 1 "
            "OR instr(CAST(key AS BLOB), x'00') > 0)"], []
        for key in ("run_id", "role", "name"):
            if key in arguments:
                conditions.append(f"json_extract(payload_json, '$.{key}') = ?")
                parameters.append(arguments[key])
        if "before_seq" in arguments:
            conditions.append("seq < ?")
            parameters.append(min(arguments["before_seq"], 2 ** 63 - 1))
        response = {"status": "ok", "section": "index", "receipts": [], "has_more": False,
                    "next_before_seq": None, "record_read": record_read, "note": RECEIPT_INDEX_NOTE}
        limit = arguments.get("limit", 20)
        try:
            with self.team._context_read():
                if "run_id" in arguments and not any(event.actor == HOST for event in self._metadata(
                        task_id, kind="team.run_started", where="json_extract(payload_json, '$.run_id') = ?",
                        parameters=(arguments["run_id"],))):
                    return {"status": "error", "message": "找不到本群的这个运行ID；角色名请用role筛选，委托序号请用message_seq，真实运行ID见receipt_index。"}
                for event in self._metadata(task_id, kind="team.tool_finished",
                        where=" AND ".join(conditions), parameters=parameters, fields=_RECEIPT_INDEX_FIELDS,
                        optional_fields={path for path, _ in _RECEIPT_INDEX_FIELDS
                                         if path.startswith("$.result.") and path != "$.result.status"}):
                    result = event.payload.get("result", {})
                    if type(result.get("status")) is not str:
                        continue
                    start = self._chain(task_id, event)
                    if start is None or ("message_seq" in arguments
                            and start.payload["message_seq"] != arguments["message_seq"]):
                        continue
                    summary = {}
                    for key, value in result.items():
                        expected = (bool if key in {"complete", "truncated", "timed_out"} else
                                    int if key in {"exit_code", "duration_ms", "bytes"} else str)
                        if type(value) is expected or key == "exit_code" and value is None:
                            summary[key] = value
                    row = {"seq": event.seq, "source_hash": event.hash,
                           **{key: start.payload[key] for key in ("run_id", "message_seq", "role", "name")},
                           "started_seq": start.seq, "result_summary": summary,
                           "read_args": {"receipt_seq": event.seq}}
                    if start.payload["name"] == "workspace_run":
                        from .team_command_index import command_identity

                        if command := command_identity(self, task_id, event, start):
                            row["command_identity"] = command
                    # Candidates arrive newest first; only advance past returned records.
                    candidate = {**response, "receipts": [row, *response["receipts"]],
                                 "has_more": True, "next_before_seq": event.seq}
                    terminal = {**candidate, "has_more": False, "next_before_seq": None}
                    if (len(response["receipts"]) >= limit
                            or max(len(json_utf8(candidate)), len(json_utf8(terminal))) > MAX_INDEX_BYTES):
                        if not response["receipts"]:
                            return {"status": "error", "code": "receipt_index_record_too_large",
                                    "message": "这份回执元数据无法放入索引页，请按receipt_seq读取。",
                                    "read_args": {"receipt_seq": event.seq}, "record_read": record_read}
                        response.update(has_more=True, next_before_seq=response["receipts"][0]["seq"])
                        break
                    response["receipts"].insert(0, row)
            return response
        except (sqlite3.Error, ValueError, TypeError, UnicodeError):
            return {"status": "error", "code": "receipt_index_unavailable", "record_read": record_read,
                    "message": "回执索引暂不可用，未回退加载全部正文；可按已知receipt_seq精读。"}

    @staticmethod
    def _guard(path=None):
        if path is None:
            return UNAMBIGUOUS_OBJECT_SQL
        # Apply the same duplicate/NUL-key semantics inside candidate objects.
        return UNAMBIGUOUS_OBJECT_SQL.replace("payload_json", f"json_extract(payload_json, '{path}')")

    @staticmethod
    def _columns(fields):
        columns = []
        for index, (path, maximum) in enumerate(fields):
            value = f"json_extract(payload_json, '{path}')"
            kind = f"json_type(payload_json, '{path}')"
            columns.extend([f"{kind} AS k{index}",
                f"CASE WHEN {kind} IN ('integer', 'real', 'true', 'false') THEN {value} "
                f"WHEN {kind} IN ('text', 'object', 'array') AND length(CAST({value} AS BLOB)) <= {maximum} "
                f"THEN CAST({value} AS BLOB) END AS v{index}"])
        return ", ".join(columns)

    @staticmethod
    def _values(row, fields, *, optional_fields=()):
        payload = {}
        for index, (path, _) in enumerate(fields):
            value, kind = row[f"v{index}"], row[f"k{index}"]
            if path in optional_fields and value is None and kind != "null":
                # SQL NULL for an oversized/unsupported value is not JSON null.
                continue
            if kind in {"true", "false"}:
                value = kind == "true"
            elif isinstance(value, bytes):
                try:
                    value = value.decode("utf-8")
                except UnicodeError:
                    if path in optional_fields:
                        continue
                    raise
                if kind in {"object", "array"}:
                    value = json.loads(value)
            target = payload
            parts = path[2:].split(".")
            for part in parts[:-1]:
                target = target.setdefault(part, {})
            if kind is not None:
                target[parts[-1]] = value
        return payload

    def _metadata(self, task_id, *, seq=None, kind=None, where="", parameters=(), limit=None, fields=None,
                  optional_fields=()):
        fields = fields or _SOURCE_FIELDS
        conditions, args = ["task_id = ?", f"({self._guard()})"], [task_id]
        if seq is not None:
            if type(seq) is not int or not 0 < seq < 2 ** 63:
                return
            conditions.append("seq = ?")
            args.append(seq)
        if kind is not None:
            conditions.append("type = ?")
            args.append(kind)
        if where:
            conditions.append(where)
            args.extend(parameters)
        sql = "SELECT seq, task_id, type, actor_json, hash, " + self._columns(fields)
        sql += " FROM events WHERE " + " AND ".join(conditions) + " ORDER BY seq DESC"
        if limit is not None:
            sql += " LIMIT ?"
            args.append(limit)
        cursor = self.rt.log.db.execute(sql, args)
        try:
            for row in cursor:
                try:
                    actor = Actor(**json.loads(row["actor_json"], object_pairs_hook=_unambiguous))
                    yield SimpleNamespace(seq=row["seq"], task_id=row["task_id"], type=row["type"],
                        actor=actor, hash=row["hash"], payload=self._values(row, fields, optional_fields=optional_fields))
                except (ValueError, TypeError, UnicodeError):
                    continue
        finally:
            cursor.close()

    def _one(self, task_id, seq):
        return next(self._metadata(task_id, seq=seq), None)

    def _chain(self, task_id, event):
        p = event.payload
        if event.actor != HOST or event.type != "team.tool_finished" or type(p.get("started_seq")) is not int:
            return None
        start = self._one(task_id, p["started_seq"])
        if start is None or start.type != "team.tool_started" or start.actor != HOST or start.seq >= event.seq:
            return None
        s = start.payload
        members = {r.id for r in [self.rt.organization.leader, *self.rt.roles]}
        if (not isinstance(s.get("role"), str) or s["role"] not in members or type(s.get("message_seq")) is not int
                or not isinstance(s.get("run_id"), str) or not s["run_id"].strip()
                or not isinstance(s.get("call_id"), str) or not s["call_id"].strip()
                or not isinstance(s.get("name"), str) or not s["name"]
                or any(p.get(k) != s.get(k) for k in ("name", "run_id", "role"))
                or "call_id" in p and p["call_id"] != s["call_id"]
                or not isinstance(p.get("result"), dict) or not isinstance(p["result"].get("status"), str)
                or p["result"]["status"] not in {"ok", "error"}):
            return None
        match = ("json_type(payload_json, '$.message_seq') = 'integer' "
                 "AND json_extract(payload_json, '$.message_seq') = ? "
                 "AND json_extract(payload_json, '$.run_id') = ? "
                 "AND json_extract(payload_json, '$.role') = ?")
        args = (s["message_seq"], s["run_id"], s["role"])
        runs = []
        for candidate in self._metadata(task_id, kind="team.run_started", where=match, parameters=args):
            if candidate.actor == HOST:
                runs.append(candidate)
                if len(runs) == 2:
                    break
        if len(runs) != 1 or runs[0].seq >= start.seq:
            return None
        if any(r.actor == HOST and r.seq < event.seq for r in self._metadata(
                task_id, kind="team.run_finished", where=match, parameters=args)):
            return None
        message = self._one(task_id, s["message_seq"])
        if (message is None or message.type != "team.message_received" or message.seq >= runs[0].seq
                or message.payload.get("recipient") != s["role"]):
            return None
        if message.payload.get("kind") == "recovery" and message.payload.get("trigger") == "control_resume":
            proof = self._resume_proof(task_id, message)
            if proof is None or not self.team.recovery.is_message(message, proof):
                return None
        elif message.payload.get("kind") == "recovery":
            # Recovery proofs are targeted too; no whole conversation fallback.
            recovery_start = self._one(task_id, message.payload.get("recovery_of"))
            recovery_end = self._one(task_id, message.payload.get("interrupted_seq"))
            source = self._one(task_id, recovery_start.payload.get("message_seq")) if recovery_start else None
            proof = [e for e in (source, recovery_start, recovery_end, message) if e is not None]
            duplicates = self._metadata(task_id, kind="team.message_received",
                where="seq < ? AND json_extract(payload_json, '$.kind') = 'recovery' "
                "AND json_extract(payload_json, '$.recovery_of') = ? "
                "AND json_extract(payload_json, '$.recovery_hash') = ?",
                parameters=(message.seq, message.payload.get("recovery_of"), message.payload.get("recovery_hash")))
            if any(e.actor == HOST for e in duplicates) or not self.team.recovery.is_message(message, proof):
                return None
        elif not (message.actor == Actor.human() or message.actor.kind == "agent" and message.actor.id in members):
            return None
        return start

    def _resume_proof(self, task_id, message):
        # Read the control lineage and its objective boundaries, not conversation bodies.
        proof = [message]
        control = self._one(task_id, message.payload.get("resume_seq"))
        if control is None:
            return proof
        proof.append(control)
        previous = control
        while (previous.actor == Actor.human() and previous.type == "task.execution_controlled"
                and previous.payload.get("action") == "resume" and previous.payload.get("state") == "running"):
            candidate = self._one(task_id, previous.payload.get("previous_seq"))
            if candidate is None or candidate.seq >= previous.seq:
                break
            proof.append(candidate)
            previous = candidate
        try:
            for cutoff in (control.seq, message.seq):
                objective = self._proof_candidate(task_id, cutoff,
                    "type IN ('team.objective_updated', 'team.objective_completed')")
                if objective is not None:
                    proof.append(objective)
            duplicate = self._proof_candidate(task_id, message.seq,
                "type = 'team.message_received' AND (NOT (" + self._guard() + ") OR ("
                "json_extract(payload_json, '$.kind') = 'recovery' "
                "AND json_extract(payload_json, '$.trigger') = 'control_resume' "
                "AND json_extract(payload_json, '$.resume_seq') = ?))", (control.seq,))
            if duplicate is not None:
                proof.append(duplicate)
        except (ValueError, TypeError, UnicodeError, sqlite3.Error):
            return None
        return sorted(proof, key=lambda event: event.seq)

    def _proof_candidate(self, task_id, before, condition, parameters=()):
        # An unreadable veto is not absence: do not skip it and accept an older boundary.
        cursor = self.rt.log.db.execute("SELECT seq, actor_json FROM events WHERE task_id = ? "
            "AND seq < ? AND (" + condition + ") ORDER BY seq DESC", (task_id, before, *parameters))
        try:
            for row in cursor:
                actor = Actor(**json.loads(row["actor_json"], object_pairs_hook=_unambiguous))
                if actor != HOST:
                    continue
                event = self._one(task_id, row["seq"])
                if event is None:
                    raise ValueError("Recovery proof metadata unavailable")
                return event
        finally:
            cursor.close()
        return None

    def _scope(self, task_id):
        self.rt.workspaces._refresh()
        latest = None
        cursor = self.rt.log.db.execute("SELECT seq, actor_json FROM events WHERE task_id = ? "
                                       "AND type = 'workspace.bound' ORDER BY seq DESC", (task_id,))
        try:
            for row in cursor:
                if Actor(**json.loads(row["actor_json"])) == Actor.human():
                    latest = row["seq"]
                    break
        finally:
            cursor.close()
        # A malformed newest binding cannot fall back to an older epoch.
        binding = next(self._metadata(task_id, seq=latest, fields=_BINDING_FIELDS), None) if latest else None
        if binding is None:
            return None
        p = binding.payload
        configured = self.rt.workspaces.config.get(p.get("workspace_id"))
        if not configured or configured["fingerprint"] != p.get("fingerprint"):
            return None
        return {"binding_seq": binding.seq, "workspace_id": p["workspace_id"],
                "fingerprint": p["fingerprint"], "root": p.get("root")}

    @staticmethod
    def _public_scope(scope):
        return {key: value for key, value in scope.items() if key != "root"} if scope else None

    def _observations(self, task_id, scope):
        if scope is None:
            return
        # SQLite checks byte lengths without transferring source bodies to Python.
        # This still scans/parses stored candidate JSON; it is not constant-I/O.
        checks = (f"({self._guard('$.result')}) AND seq > ? "
            "AND json_extract(payload_json, '$.name') = 'workspace_read' "
            "AND json_type(payload_json, '$.result.content') = 'text' "
            "AND length(CAST(json_extract(payload_json, '$.result.content') AS BLOB)) <= ? "
            "AND length(CAST(json_extract(payload_json, '$.result.content') AS BLOB)) "
            "= json_extract(payload_json, '$.result.returned_bytes')")
        for event in self._metadata(task_id, kind="team.tool_finished", where=checks,
                parameters=(scope["binding_seq"], MAX_READ_BYTES), fields=_READ_FIELDS):
            start = self._chain(task_id, event)
            if start is None:
                continue
            p = event.payload
            s = start.payload
            if s["name"] != "workspace_read" or s.get("root") != scope["root"] or start.seq <= scope["binding_seq"]:
                continue
            # Ambiguous nested argument keys must not acquire SQLite's first-key semantics.
            if self.rt.log.db.execute("SELECT 1 FROM events WHERE seq = ? AND "
                    f"({self._guard('$.arguments')})", (start.seq,)).fetchone() is None:
                continue
            result, arguments = p.get("result"), s.get("arguments")
            if not isinstance(result, dict) or result.get("status") != "ok" or not isinstance(arguments, dict):
                continue
            try:
                path = file_path(result.get("path"))
                if path != file_path(arguments.get("path")):
                    continue
            except (ValueError, KeyError, TypeError, AttributeError, UnicodeError):
                continue
            size, offset, returned = result.get("bytes"), result.get("offset"), result.get("returned_bytes")
            limit = arguments.get("limit", DEFAULT_READ_BYTES)
            sha = result.get("sha256")
            if (not isinstance(sha, str) or re.fullmatch(r"[a-f0-9]{64}", sha) is None
                    or result.get("encoding") != "utf-8" or type(result.get("truncated")) is not bool
                    or not _integer(size, 0, MAX_FILE_BYTES) or not _integer(offset, 0, MAX_FILE_BYTES)
                    or not _integer(limit, 1, MAX_READ_BYTES)
                    or not _integer(arguments.get("offset", 0), 0, MAX_FILE_BYTES)
                    or arguments.get("offset", 0) != offset
                    or not _integer(returned, 0, limit)
                    or returned > max(0, size - offset)):
                continue
            complete = not result["truncated"] and offset == 0 and returned == size
            if not result["truncated"] and not complete:
                continue
            row = {"seq": event.seq, "started_seq": start.seq, "run_id": s["run_id"],
                   "message_seq": s["message_seq"], "role": s["role"], "path": path,
                   "sha256": sha, "bytes": size, "offset": offset, "returned_bytes": returned,
                   "truncated": result["truncated"], "content_complete": complete,
                   "read_tool": "read_receipts", "read_args": {"receipt_seq": event.seq}}
            if type(s.get("revision")) is int:
                row["revision"] = s["revision"]
            yield row

    def index(self, task_id, *, workspace=None, events=None):
        try:
            scope = self._scope(task_id)
            if workspace is not None and (not workspace.get("available") or scope is None
                    or any(workspace.get(key) != scope.get(other) for key, other in
                           (("revision", "binding_seq"), ("root", "root"), ("fingerprint", "fingerprint")))):
                return None
            groups, omitted = {}, False
            for row in self._observations(task_id, scope):
                path = row["path"]
                if path in groups:
                    entry = groups[path]
                    entry["first_observed"] = row
                    entry["read_count"] += 1
                elif len(groups) < 8:
                    entry = {"path": path, "read_count": 1, "first_observed": row, "last_observed": row,
                        "read_tool": "read_receipts", "read_args": {"section": "file_reads", "file_path": path}}
                    if len(json_utf8({"workspace_binding": self._public_scope(scope), "files": [entry],
                                      "truncated": True, "note": NOTE})) <= MAX_INDEX_BYTES:
                        groups[path] = entry
                    else:
                        omitted = True
                else:
                    omitted = True
            if not groups or self._scope(task_id) != scope:
                return None
            result = {"workspace_binding": self._public_scope(scope), "files": [], "truncated": omitted, "note": NOTE}
            for entry in groups.values():
                candidate = {**result, "files": [*result["files"], entry]}
                if len(json_utf8(candidate)) > MAX_INDEX_BYTES:
                    result["truncated"] = True
                else:
                    result["files"].append(entry)
            return result
        except (sqlite3.Error, ValueError, TypeError, UnicodeError):
            return None

    def read(self, task_id, arguments):
        limit = arguments.get("limit", 20)
        path = None
        if "file_path" in arguments:
            try:
                file_path(arguments["file_path"])
            except ValueError:
                return invalid_query("file_path需要具体的工作目录相对文件路径，不使用绝对路径、父级路径或目录。", "file_path")
            path = arguments["file_path"]
        try:
            with self.team._context_read():
                scope = self._scope(task_id)
                rows = []
                for row in self._observations(task_id, scope):
                    if ((path is None or row["path"] == path)
                            and ("name" not in arguments or arguments["name"] == "workspace_read")
                            and all(key not in arguments or row.get(key) == arguments[key]
                                    for key in ("run_id", "role", "message_seq"))
                            and ("before_seq" not in arguments or row["seq"] < arguments["before_seq"])):
                        rows.append(row)
                        if len(rows) > limit:
                            break
            if scope != self._scope(task_id):
                return {"status": "error", "code": "file_read_scope_changed", "message": "目录绑定已变化，请重新查询来源。"}
            page = list(reversed(rows[:limit]))
            query_state = "scope_unavailable" if scope is None else "matched_records" if page else "no_matching_records"
            scope_note = ("当前工作目录绑定范围不可查询，空列表不表示没有历史读取，也不证明发生过解绑或权限变更。"
                          if scope is None else "当前筛选条件下没有匹配记录，不证明文件从未读过。" if not page else "")
            return {"status": "ok", "observations": page, "workspace_binding": self._public_scope(scope),
                    "scope_available": scope is not None, "query_state": query_state, "has_more": len(rows) > limit,
                    "next_before_seq": page[0]["seq"] if len(rows) > limit else None, "note": scope_note + NOTE}
        except (sqlite3.Error, ValueError, TypeError, UnicodeError):
            return {"status": "error", "code": "file_read_metadata_unavailable",
                    "message": "文件读取元数据暂不可用，未回退加载整段历史正文。"}
