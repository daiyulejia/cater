"""Ephemeral public client text, scoped to live host activities and receipts."""

from contextlib import contextmanager
import threading
import time

from .events import Actor


MAX_TEXT = 4000
MAX_RUNS = 16
FRESH_SECONDS = 60
HOST = Actor.system("team")
IDENTITY = ("role", "run_id", "message_seq")


class PublicProgress:
    def __init__(self):
        self._lock = threading.Lock()
        self._channels = {}

    @contextmanager
    def capture(self, task_id, activity_id):
        key, rows = (task_id, activity_id), {}
        with self._lock:
            self._channels[key] = rows

        def observe(attempt, revision, update):
            if attempt.task_id != task_id or attempt.actor != HOST or attempt.type != "team.external_run_started":
                return
            with self._lock:
                if self._channels.get(key) is not rows:
                    return
                if update is None:
                    rows.pop(attempt.seq, None)
                    return
                if (not isinstance(update, dict) or update.get("sessionUpdate") != "agent_message_chunk"
                        or not isinstance(update.get("content"), dict) or update["content"].get("type") != "text"):
                    return
                text = update["content"].get("text")
                if not isinstance(text, str) or not text or type(revision) is not int or revision < 0:
                    return
                text = text[-MAX_TEXT:]
                try:
                    text.encode("utf-8")
                except UnicodeEncodeError:
                    return
                previous = rows.get(attempt.seq)
                # A goal change must not carry the previous goal's prose forward.
                previous = previous if previous and previous["revision"] == revision else None
                merged = (previous["text"] if previous else "") + text
                rows[attempt.seq] = {"external_started_seq": attempt.seq,
                    "run_started_seq": attempt.payload["run_started_seq"],
                    **{name: attempt.payload[name] for name in IDENTITY}, "revision": revision,
                    "text": merged[-MAX_TEXT:], "truncated": bool(previous and previous["truncated"]
                        or len(update["content"]["text"]) > MAX_TEXT or len(merged) > MAX_TEXT),
                    "observed": time.monotonic()}
                while len(rows) > MAX_RUNS:
                    rows.pop(next(iter(rows)))

        try:
            yield observe
        finally:
            with self._lock:
                if self._channels.get(key) is rows:
                    self._channels.pop(key, None)
                rows.clear()

    def snapshot(self, task_id, activity_id):
        with self._lock:
            now = time.monotonic()
            return [{**{key: value for key, value in row.items() if key != "observed"},
                     "age_ms": max(0, int((now - row["observed"]) * 1000))}
                    for row in self._channels.get((task_id, activity_id), {}).values()
                    if now - row["observed"] < FRESH_SECONDS]


def visible_progress(rows, *, events, team, activity, control, current):
    """Recheck the live text against the same immutable detail snapshot."""
    if not team or activity.get("state") != "running" or not activity.get("active") or control["state"] != "running":
        return []
    by_seq = {event.seq: event for event in events}
    hosts = [event for event in events if event.actor == HOST]
    result = []
    for row in rows:
        if row["revision"] != team["revision"]:
            continue
        start, attempt = by_seq.get(row["run_started_seq"]), by_seq.get(row["external_started_seq"])
        if (not start or not attempt or start.actor != HOST or attempt.actor != HOST
                or start.type != "team.run_started" or attempt.type != "team.external_run_started"
                or start.payload.get("control_seq") != control["seq"]
                or attempt.payload.get("run_started_seq") != start.seq
                or any(any(event.payload.get(key) != row[key] for key in IDENTITY) for event in (start, attempt))):
            continue
        if any(event.seq > attempt.seq and (
            event.type == "team.run_finished" and event.payload.get("run_id") == row["run_id"]
            or event.type == "team.external_run_finished" and event.payload.get("started_seq") == attempt.seq)
               for event in hosts):
            continue
        try:
            # Reuse execution admission, including run-aware recovery and withdrawal.
            current(start, row)
        except Exception:
            continue
        result.append(row)
    return result
