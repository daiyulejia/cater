"""Durable external invocation receipts; never authorize prompt replay."""

from __future__ import annotations

import json

from .activity_owner import activity_owner
from .events import Actor, Event
from .work import WorkConflict


HOST = Actor.system("team")
EXTERNAL_RUN_CONTRACT = {"version": 1, "kind": "external_agent"}
_IDENTITY = ("role", "run_id", "message_seq")
_BASIS = ("as_of_seq", "as_of_hash", "rule_fingerprint", "rule_snapshot_seq")


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _seq(value):
    return type(value) is int and value > 0


def _same(left, right):
    # Python equality alone accepts True as sequence 1 and contract version 1.
    return json.dumps(left, sort_keys=True) == json.dumps(right, sort_keys=True)


def _identity(payload):
    return (_text(payload.get("role")) and _text(payload.get("run_id"))
            and _seq(payload.get("message_seq")))


def _outcome(prompt_attempted, result):
    return ("not_started" if not prompt_attempted else
            "unknown" if result == "failed" else result)


class ExternalRuns:
    def __init__(self, team):
        self.team, self.rt = team, team.rt

    def _durable(self):
        if self.rt.log.db.in_transaction:
            raise WorkConflict("External invocation receipts require no outer transaction.")

    def _stored(self, event, events):
        if not isinstance(event, Event) or not _seq(event.seq):
            raise ValueError("A persisted event is required.")
        stored = next((e for e in events if e.seq == event.seq), None)
        if (stored is None or stored != event or not _same(stored.payload, event.payload)):
            raise ValueError("Event does not match the persisted receipt.")
        return stored

    def _run_valid(self, run, by_seq):
        p = run.payload
        if run.actor != HOST or run.type != "team.run_started" or not _identity(p):
            return False
        message = by_seq.get(p["message_seq"])
        return bool(message and message.task_id == run.task_id and message.seq < run.seq
                    and message.type == "team.message_received"
                    and message.payload.get("recipient") == p["role"])

    def _start_valid(self, start, by_seq):
        p = start.payload
        if (start.actor != HOST or start.type != "team.external_run_started"
                or not _identity(p) or not _seq(p.get("run_started_seq"))
                or not _same(p.get("execution_contract"), EXTERNAL_RUN_CONTRACT)):
            return False
        run = by_seq.get(p["run_started_seq"])
        return bool(run and run.task_id == start.task_id and run.seq < start.seq
                    and self._run_valid(run, by_seq) and p.get("run_started_hash") == run.hash
                    and all(_same(p.get(k), run.payload.get(k)) for k in _IDENTITY))

    def _bound(self, event, start):
        p = event.payload
        return (event.task_id == start.task_id and event.seq > start.seq
                and _seq(p.get("started_seq")) and p["started_seq"] == start.seq
                and p.get("started_hash") == start.hash
                and all(_same(p.get(k), start.payload.get(k)) for k in _IDENTITY))

    def _finished(self, event, start):
        p = event.payload
        return (event.type == "team.external_run_finished" and event.actor == HOST
                and self._bound(event, start) and type(p.get("cleanup_complete")) is bool
                and type(p.get("prompt_attempted")) is bool and _text(p.get("phase"))
                and p.get("result") in ("returned", "yielded", "failed")
                and p.get("outcome") == _outcome(p["prompt_attempted"], p["result"]))

    def _ended(self, start, events, *, before=None):
        return any(e.type == "team.run_finished" and e.actor == HOST
                   and e.task_id == start.task_id and e.seq > start.seq
                   and (before is None or e.seq < before)
                   and _identity(e.payload)
                   and all(_same(e.payload.get(k), start.payload.get(k)) for k in _IDENTITY)
                   and e.payload.get("status") in ("completed", "waiting", "failed", "interrupted")
                   for e in events)

    def _reconciled(self, event, start, events):
        p = event.payload
        return (event.type == "team.external_run_reconciled" and event.actor == Actor.human()
                and self._bound(event, start) and p.get("invocation_stopped") is True
                and _text(p.get("note")) and p.get("outcome") == "effects_reviewed_no_replay"
                and self._ended(start, events, before=event.seq))

    @staticmethod
    def _reference(start):
        return {"started_seq": start.seq, "started_hash": start.hash,
                **{k: start.payload[k] for k in _IDENTITY}}

    def begin(self, task_id, role, run_start, *, basis, binding, tool_names,
              driver_fingerprint) -> Event:
        self._durable()
        if not isinstance(basis, dict) or not isinstance(binding, dict):
            raise ValueError("Context basis and workspace binding must be mappings.")
        if isinstance(tool_names, str):
            raise ValueError("Tool names must be a collection of names.")
        names = list(tool_names)
        if not all(_text(name) for name in names) or not _text(driver_fingerprint):
            raise ValueError("Tool names and driver fingerprint must be nonempty text.")
        # Copy only public provenance, never launch environments or credentials.
        payload = {
            "execution_contract": dict(EXTERNAL_RUN_CONTRACT),
            "context_basis": {k: basis[k] for k in _BASIS if k in basis},
            "workspace_id": binding.get("workspace_id"),
            "workspace_revision": binding.get("revision"),
            "workspace_fingerprint": binding.get("fingerprint"),
            "root": binding.get("root"),
            "tool_names": sorted(set(names)), "driver_fingerprint": driver_fingerprint,
        }
        with self.rt.log.atomic():
            events = self.rt.log.read(task_id=task_id)
            run = self._stored(run_start, events)
            if (run.task_id != task_id or not self._run_valid(run, {e.seq: e for e in events})
                    or getattr(role, "id", None) != run.payload["role"]
                    or self._ended(run, events)):
                raise ValueError("Expected this task's active host run and matching role/message.")
            if any(e.type == "team.external_run_started" and e.actor == HOST
                   and e.payload.get("run_started_seq") == run.seq for e in events):
                raise WorkConflict("本次运行已有外部请求记录，不自动重放；请核对原记录后使用新的监督输入。")
            return self.rt.log.emit(type="team.external_run_started", task_id=task_id, actor=HOST,
                payload={**payload, **{k: run.payload[k] for k in _IDENTITY},
                         "run_started_seq": run.seq, "run_started_hash": run.hash})

    def finish(self, start, *, phase, prompt_attempted, cleanup_complete,
               stop_reason=None, session_id=None, result="returned") -> Event:
        self._durable()
        if (not _text(phase) or type(prompt_attempted) is not bool
                or type(cleanup_complete) is not bool or result not in ("returned", "yielded", "failed")
                or any(v is not None and not isinstance(v, str) for v in (stop_reason, session_id))):
            raise ValueError("Invalid external invocation result.")
        with self.rt.log.atomic():
            events = self.rt.log.read(task_id=start.task_id)
            start = self._stored(start, events)
            if not self._start_valid(start, {e.seq: e for e in events}):
                raise ValueError("Invalid external invocation start.")
            payload = {**self._reference(start), "phase": phase, "prompt_attempted": prompt_attempted,
                       "cleanup_complete": cleanup_complete, "stop_reason": stop_reason,
                       "session_id": session_id, "result": result,
                       "outcome": _outcome(prompt_attempted, result)}
            previous = next((e for e in events if self._finished(e, start)), None)
            if previous:
                if not _same(previous.payload, payload):
                    raise WorkConflict("An existing finish receipt cannot be changed.")
                return previous
            if any(self._reconciled(e, start, events) for e in events):
                raise WorkConflict("Invocation already reconciled.")
            return self.rt.log.emit(type="team.external_run_finished", task_id=start.task_id,
                                    actor=HOST, payload=payload)

    def pending(self, task_id) -> list[dict]:
        if not next(self.rt.log.iter_events(task_id=task_id,
                    event_types=("team.external_run_started",)), None):
            return []
        events = self.rt.log.read(task_id=task_id)
        by_seq = {e.seq: e for e in events}
        rows = []
        for start in events:
            if start.type != "team.external_run_started" or start.actor != HOST:
                continue
            valid = self._start_valid(start, by_seq)
            finished = next((e for e in events if valid and self._finished(e, start)), None)
            if (finished and finished.payload["cleanup_complete"] is True
                    or valid and any(self._reconciled(e, start, events) for e in events)):
                continue
            ended = valid and self._ended(start, events)
            rows.append({"seq": start.seq, "started_seq": start.seq, "name": "external_agent",
                         **{k: start.payload.get(k) for k in _IDENTITY},
                         "outcome": "unknown" if ended or finished or not valid else "unsettled",
                         "run_ended": bool(ended), "replay_authorized": False})
        return rows

    def reconcile(self, task_id, started_seq, *, expected_hash, invocation_stopped, note,
                  retire_host_run=False) -> Event:
        """Trusted host/human API only; deliberately absent from model tool schemas."""
        self._durable()
        if type(retire_host_run) is not bool:
            raise ValueError("retire_host_run must be a boolean.")
        if (not _seq(started_seq) or not _text(expected_hash) or invocation_stopped is not True
                or not _text(note)):
            raise ValueError("Require start identity, hash, stopped invocation, and nonempty note.")
        with activity_owner(self.rt.log.path, task_id, "simple-team") as owned:
            if not owned:
                raise WorkConflict("Team execution still owns this task.")
            with self.rt.log.atomic():
                events = self.rt.log.read(task_id=task_id)
                by_seq = {e.seq: e for e in events}
                start = by_seq.get(started_seq)
                if start is None or start.hash != expected_hash or not self._start_valid(start, by_seq):
                    raise WorkConflict("Require this task's external start and a finished host run.")
                ended = self._ended(start, events)
                if not ended and not retire_host_run:
                    raise WorkConflict("Require this task's external start and a finished host run.")
                payload = {**self._reference(start), "invocation_stopped": True, "note": note.strip(),
                           "outcome": "effects_reviewed_no_replay"}
                previous = next((e for e in events if self._reconciled(e, start, events)), None)
                if previous:
                    if not _same(previous.payload, payload):
                        raise WorkConflict("Reconciliation evidence cannot be changed.")
                    return previous
                if any(self._finished(e, start) and e.payload["cleanup_complete"] is True for e in events):
                    raise WorkConflict("Invocation cleanup is already confirmed.")
                if not ended:
                    run = by_seq[start.payload["run_started_seq"]]
                    self.rt.log.emit(type="team.run_finished", task_id=task_id, actor=HOST,
                        payload={**{k: run.payload[k] for k in _IDENTITY},
                                 **({"revision": run.payload["revision"]} if "revision" in run.payload else {}),
                                 "status": "interrupted", "recovery_of": run.seq,
                                 "reason": note.strip()})
                return self.rt.log.emit(type="team.external_run_reconciled", task_id=task_id,
                                        actor=Actor.human(), payload=payload)
