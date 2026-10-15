"""Minimal local web surface for Cater replay."""

from __future__ import annotations

import hashlib
import html
import hmac
import ipaddress
import json
import logging
import mimetypes
import threading
import time
import uuid
from collections import Counter
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs, unquote, urlparse

from .assignments import latest_assignments_by_owner
from .activity_owner import activity_owner
from .availability import team_availability
from .drivers import driver_mode
from .events import Actor as EventActor, Event
from .fold import TaskSnapshot
from .ladder import decision_ladder
from .messages import message_relation
from .path import organization_path
from .review import build_review
from .runtime import DriverHealth, ExecutionScopeChanged, Runtime
from .rules import RuleConflict
from .work import WorkConflict


MAX_REQUEST_BYTES = 1_048_576
MAX_REJECT_DRAIN_BYTES = 65_536
REJECT_DRAIN_TIMEOUT_SECONDS = 0.25
MAX_QUESTION_QUEUE_ATTEMPTS = 3

_LOGGER = logging.getLogger(__name__)


class ActivationSuperseded(WorkConflict):
    pass


class ActivityRunner:
    """Run long collaboration rounds off the request thread, one per task."""

    _BINDING_KEYS = ("conversation_seq", "expected_scope", "finished_seq", "resume_seq", "activation_seq")

    def __init__(self, runtime_factory: Callable[[], Runtime]):
        self._runtime_factory = runtime_factory
        self._lock = threading.RLock()
        self._activities: dict[str, dict[str, Any]] = {}
        self._revision = 0
        self._question_tasks: dict[str, str] = {}
        self._conversation_tasks: dict[str, str] = {}

    def answer_conversations(self, task_id: str) -> None:
        with self._lock:
            if task_id in self._conversation_tasks:
                return
            owner = uuid.uuid4().hex
            self._conversation_tasks[task_id] = owner
        try:
            threading.Thread(target=self._answer_conversations, args=(task_id, owner),
                             name=f"cater-chat-{task_id}", daemon=True).start()
        except Exception:
            with self._lock:
                if self._conversation_tasks.get(task_id) == owner:
                    self._conversation_tasks.pop(task_id, None)
            raise

    def _answer_conversations(self, task_id: str, owner: str) -> None:
        last_message = None
        attempts = read_failures = 0
        try:
            while True:
                try:
                    with self._lock:
                        if self._conversation_tasks.get(task_id) != owner:
                            return
                        wait_seconds = None
                        with self._runtime_factory() as runtime:
                            pending = runtime.conversation.pending(task_id)
                            if not pending:
                                rows = runtime.conversation.view(task_id)
                                if any(row["status"] == "pending" for row in rows):
                                    answering = {row["attempt_seq"] for row in rows if row["status"] == "answering"}
                                    leases = [event.payload["lease_until"] for event in runtime.log.iter_events(
                                        task_id=task_id, event_types=("conversation.started",))
                                        if event.actor == EventActor.system("conversation") and event.seq in answering]
                                    wait_seconds = min(1.0, max(0.01,
                                        max(leases, default=time.time()) - time.time() + 0.001))
                        if not pending and wait_seconds is None:
                            if self._conversation_tasks.get(task_id) == owner:
                                self._conversation_tasks.pop(task_id, None)
                            return
                except Exception:
                    read_failures += 1
                    _LOGGER.exception("Cannot read conversation queue for %s", task_id)
                    if read_failures >= MAX_QUESTION_QUEUE_ATTEMPTS:
                        return
                    continue
                read_failures = 0
                if wait_seconds is not None:
                    # Keep one consumer until later messages can run; never replay the old call.
                    # Wait outside the runner lock so other tasks and sends remain independent.
                    time.sleep(wait_seconds)
                    continue
                if pending[0] != last_message:
                    last_message, attempts = pending[0], 0
                if attempts >= MAX_QUESTION_QUEUE_ATTEMPTS:
                    return
                attempts += 1
                try:
                    with self._runtime_factory() as runtime:
                        result = runtime.conversation.resolve_next(task_id)
                    if result and result.get("next_action") == "explain":
                        self.answer_questions(task_id)
                    elif result and result.get("next_action") == "run":
                        self.dispatch_conversation(task_id, result)
                except Exception:
                    # Started calls remain durable and are never blindly repeated.
                    _LOGGER.exception("Conversation handling failed for %s at %s", task_id, last_message)
        finally:
            with self._lock:
                if self._conversation_tasks.get(task_id) == owner:
                    self._conversation_tasks.pop(task_id, None)

    def answer_questions(self, task_id: str) -> None:
        with self._lock:
            if task_id in self._question_tasks:
                return
            owner = uuid.uuid4().hex
            self._question_tasks[task_id] = owner
        try:
            threading.Thread(target=self._answer_questions, args=(task_id, owner), name=f"cater-explain-{task_id}", daemon=True).start()
        except Exception:
            with self._lock:
                if self._question_tasks.get(task_id) == owner:
                    self._question_tasks.pop(task_id, None)
            raise

    def _answer_questions(self, task_id: str, owner: str) -> None:
        last_question = None
        attempts = read_failures = 0
        try:
            while True:
                # Keep the empty-queue decision atomic with new submissions.
                try:
                    with self._lock:
                        with self._runtime_factory() as runtime:
                            pending = runtime.questions.pending(task_id)
                        if not pending:
                            if self._question_tasks.get(task_id) == owner:
                                self._question_tasks.pop(task_id, None)
                            return
                except Exception:
                    read_failures += 1
                    _LOGGER.exception("Cannot read question queue for %s (%d/%d)",
                                      task_id, read_failures, MAX_QUESTION_QUEUE_ATTEMPTS)
                    if read_failures >= MAX_QUESTION_QUEUE_ATTEMPTS:
                        return
                    continue
                read_failures = 0
                if pending[0] != last_question:
                    last_question, attempts = pending[0], 0
                if attempts >= MAX_QUESTION_QUEUE_ATTEMPTS:
                    _LOGGER.error("Question queue for %s stopped without progress at question %s",
                                  task_id, last_question)
                    return
                attempts += 1
                try:
                    with self._runtime_factory() as runtime:
                        runtime.questions.answer_next(task_id)
                except Exception:
                    # Re-read durable state before proceeding: a started call is
                    # never pending again, even if recording its result failed.
                    _LOGGER.exception("Question queue attempt failed for %s at question %s",
                                      task_id, last_question)
        finally:
            with self._lock:
                if self._question_tasks.get(task_id) == owner:
                    self._question_tasks.pop(task_id, None)

    def snapshot(self) -> tuple[int, dict[str, dict[str, Any]]]:
        with self._lock:
            return self._revision, {task_id: self.status(task_id) for task_id in self._activities}

    def recover_interrupted(self) -> int:
        """Close activities left open by a previous Web process.

        A thread cannot survive a server restart, and replaying its old model
        call could duplicate side effects against stale task context. Record a
        durable, visible failure instead; the customer can then continue from
        the latest command with a freshly assembled round.

        Never-started questions are resumed separately. The return value still
        counts only interrupted execution activities, not question queues.
        """
        pending: dict[tuple[str, str], Event] = {}
        recovered = 0
        with self._lock:
            with self._runtime_factory() as runtime:
                for event in runtime.log.iter_events(event_types=(
                        "activity.queued", "activity.completed", "activity.failed")):
                    activity_id = event.payload.get("activity_id")
                    if (event.actor != EventActor.system("orchestrator") or not event.task_id
                            or not isinstance(activity_id, str) or not activity_id):
                        continue
                    key = (event.task_id, activity_id)
                    if event.type == "activity.queued":
                        pending[key] = event
                    else:
                        pending.pop(key, None)
            # Recovery may be invoked again while this process owns live workers.
            for task_id, activity in self._activities.items():
                if activity["state"] in {"queued", "running"}:
                    with self._runtime_factory() as runtime:
                        control = self._control_failure(runtime, task_id, activity)
                        followup = activity.get("follow_up")
                        followup_control = self._control_failure(runtime, task_id, followup) if followup else None
                    if activity["state"] == "running" or control is None:
                        pending.pop((task_id, activity["id"]), None)
                    if isinstance(followup, dict) and followup_control is None:
                        pending.pop((task_id, followup["id"]), None)
            for (task_id, activity_id), queued in pending.items():
                action = str(queued.payload.get("action") or "run")
                try:
                    result = self._recover_activity(task_id, queued)
                    if result is None:
                        continue
                    terminal, created = result
                except Exception:
                    _LOGGER.exception("Cannot recover activity for %s at %s", task_id, activity_id)
                    continue
                if not created:
                    continue
                recovered += 1
                current = self._activities.get(task_id)
                if current and current["state"] in {"queued", "running"} and current["id"] != activity_id:
                    if (current.get("follow_up") or {}).get("id") == activity_id:
                        current.pop("follow_up")
                        self._revision += 1
                    continue
                self._activities[task_id] = {
                    "id": activity_id,
                    "action": action,
                    "state": "failed",
                    "active": False,
                    "error": terminal.payload["message"],
                    "finished_at": terminal.ts,
                }
                self._revision += 1
        self._recover_pending_questions()
        with self._runtime_factory() as runtime:
            queued = {(event.task_id, event.payload.get("conversation_seq"))
                      for event in runtime.log.iter_events(event_types=("activity.queued",))
                      if event.actor == EventActor.system("orchestrator")}
            undispatched = [event for event in runtime.log.iter_events(event_types=("conversation.finished",))
                            if event.actor == EventActor.system("conversation")
                            and event.payload.get("next_action") == "run"
                            and (event.task_id, event.payload.get("conversation_seq")) not in queued]
            claimed_resumes = {(event.task_id, event.payload.get("resume_seq")) for event in runtime.log.iter_events(
                event_types=("activity.queued",)) if event.actor == EventActor.system("orchestrator")}
            resumes = [event for event in runtime.log.iter_events(event_types=("task.execution_controlled",))
                       if event.actor == EventActor.human() and event.payload.get("action") == "resume"
                       and "conversation_seq" not in event.payload
                       and (event.task_id, event.seq) not in claimed_resumes]
            task_ids = {event.task_id for event in runtime.log.iter_events(event_types=("conversation.received",))
                        if event.task_id and event.actor == EventActor.human()}
            activations = list(runtime.log.iter_events(event_types=("task.activation_requested",)))
        for event in undispatched:
            try:
                self.dispatch_conversation(event.task_id, {**event.payload, "finish_seq": event.seq})
            except Exception:
                _LOGGER.exception("Cannot recover conversation dispatch for %s at %s", event.task_id, event.seq)
        for event in resumes:
            try:
                self.dispatch_control(event.task_id, event)
            except Exception:
                _LOGGER.exception("Cannot recover control dispatch for %s at %s", event.task_id, event.seq)
        for event in activations:
            try:
                self.dispatch_activation(event.task_id, event)
            except Exception:
                _LOGGER.exception("Cannot recover task activation for %s at %s", event.task_id, event.seq)
        for task_id in sorted(task_ids):
            self.answer_conversations(task_id)
        return recovered

    def _recover_pending_questions(self) -> None:
        """Wake pending question queues without replaying started/uncertain calls.

        Discover work from the log, not the previous process's thread registry.
        A failure in one task must not prevent recovery of other task queues.
        """
        with self._runtime_factory() as runtime:
            task_ids = {event.task_id for event in runtime.log.iter_events(event_types=("question.asked",))
                        if event.task_id and event.actor == EventActor.human()}
            pending_tasks = []
            for task_id in sorted(task_ids):
                try:
                    if runtime.questions.pending(task_id):
                        pending_tasks.append(task_id)
                except Exception:
                    _LOGGER.exception("Cannot recover question queue for %s", task_id)
        for task_id in pending_tasks:
            try:
                self.answer_questions(task_id)
            except Exception:
                _LOGGER.exception("Cannot start recovered question queue for %s", task_id)

    def status(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            activity = dict(self._activities.get(task_id, {}))
            if isinstance(activity.get("follow_up"), dict):
                activity["follow_up"] = dict(activity["follow_up"])
            revision = self._revision
        if not activity:
            return {"state": "idle", "active": False, "revision": revision}
        activity["active"] = activity["state"] in {"queued", "running"}
        activity["revision"] = revision
        return activity

    def submit(self, task_id: str, action: str) -> dict[str, Any]:
        return self._submit(task_id, action, queue_after_current=False)

    def dispatch_activation(self, task_id: str, intent: Event, *, background: bool = True) -> dict[str, Any]:
        from .task_requests import activation

        with self._runtime_factory() as runtime:
            current = activation(runtime, task_id)
            if current is None or current != intent:
                return self.status(task_id)
            if any(e.actor == EventActor.system("orchestrator") and e.payload.get("activation_seq") == intent.seq
                   for e in runtime.log.iter_events(task_id=task_id, event_types=("activity.queued",))):
                return self.status(task_id)
        try:
            return self._submit(task_id, intent.payload["action"], queue_after_current=False,
                binding={"activation_seq": intent.seq, "expected_scope": intent.payload["scope"]},
                run_inline=not background)
        except WorkConflict:
            with self._runtime_factory() as runtime:
                if any(e.actor == EventActor.system("orchestrator") and e.payload.get("activation_seq") == intent.seq
                       for e in runtime.log.iter_events(task_id=task_id, event_types=("activity.queued",))):
                    return self.status(task_id)
            raise

    def _recover_activity(self, task_id: str, queued: Event) -> tuple[Event, bool] | None:
        activity_id = queued.payload["activity_id"]
        with self._runtime_factory() as runtime:
            with ExitStack() as ownership:
                predecessor = queued.payload.get("after_activity_id")
                # A live parent owns its queued follow-up until _start reserves
                # the child's lock. Hold both locks while deciding recovery.
                if isinstance(predecessor, str) and predecessor:
                    if not ownership.enter_context(activity_owner(runtime.log.path, task_id, predecessor)):
                        return None
                owned = ownership.enter_context(activity_owner(runtime.log.path, task_id, activity_id))
                if not owned:
                    return None
                # Re-read after ownership: a worker may have finished since the scan.
                if self._activity_terminal(runtime, task_id, activity_id) is not None:
                    return None
                if not self._restore_unstarted_activation(task_id, queued):
                    failure = self._control_failure(runtime, task_id, {**queued.payload, "queued_seq": queued.seq})
                    return self._record_failure(task_id, activity_id, str(queued.payload.get("action") or "run"),
                        **(failure or {"error_type": "InterruptedActivity",
                            "message": "Cater 服务在这轮协作结束前重启；请按最新方向重新启动。"}))
        # The worker acquires the same lock before inspecting or executing this queue.
        self._start(task_id, self._activities[task_id])
        return None

    def _restore_unstarted_activation(self, task_id: str, queued: Event) -> bool:
        """Only the versioned activation protocol proves no execution has started."""
        from .task_requests import activation

        if "activation_seq" not in queued.payload:
            return False
        try:
            with self._runtime_factory() as runtime:
                intent = activation(runtime, task_id)
                events = runtime.log.read(task_id=task_id)
                if (intent is None or intent.seq != queued.payload["activation_seq"]
                        or queued.payload.get("action") != intent.payload["action"]
                        or queued.payload.get("expected_scope") != intent.payload["scope"]
                        or any(e.type == "activity.started" and e.actor == EventActor.system("orchestrator")
                               and e.payload.get("activity_id") == queued.payload["activity_id"] for e in events)):
                    return False
                binding = {**queued.payload, "queued_seq": queued.seq}
                if (self._control_failure(runtime, task_id, binding)
                        or runtime.work.scope(task_id) != intent.payload["scope"]):
                    return False
            activity = {"id": queued.payload["activity_id"], **queued.payload,
                        "queued_seq": queued.seq, "queued_at": queued.ts, "state": "queued"}
            self._activities[task_id] = activity
            self._revision += 1
            return True
        except Exception:
            _LOGGER.exception("Cannot resume never-started task activation for %s", task_id)
            return False

    @staticmethod
    def _claim_activation_start(runtime: Runtime, task_id: str, binding: dict[str, Any]) -> bool:
        from .task_requests import activation

        with runtime.log.atomic():
            intent = activation(runtime, task_id)
            queued = next((event for event in runtime.log.iter_events(task_id=task_id, event_types=("activity.queued",))
                           if event.seq == binding.get("queued_seq")
                           and event.actor == EventActor.system("orchestrator")), None)
            if (intent is None or intent.seq != binding.get("activation_seq")
                    or intent.payload["action"] != binding.get("action")
                    or intent.payload["scope"] != binding.get("expected_scope") or queued is None
                    or any(queued.payload.get(key) != binding.get(key)
                           for key in ("activity_id", "activation_seq", "action", "expected_scope"))):
                raise WorkConflict("首次执行请求的来源不匹配，未启动执行。")
            if any(e.actor == EventActor.system("orchestrator")
                   and e.payload.get("activity_id") == binding["activity_id"]
                   for e in runtime.log.iter_events(task_id=task_id,
                       event_types=("activity.started", "activity.completed", "activity.failed"))):
                return False
            superseded = ActivityRunner._activation_failure(runtime, task_id, binding)
            if superseded:
                raise ActivationSuperseded(superseded["message"])
            if ActivityRunner._control_failure(runtime, task_id, binding):
                raise ExecutionScopeChanged("首次执行前任务已暂停或取消。")
            if runtime.work.scope(task_id) != intent.payload["scope"]:
                raise ExecutionScopeChanged("首次执行授权的任务范围已变化。")
            runtime.log.emit(type="activity.started", task_id=task_id, actor=EventActor.system("orchestrator"),
                payload={"activity_id": binding["activity_id"], "activation_seq": intent.seq,
                         "queued_seq": binding["queued_seq"], "action": intent.payload["action"]})
            return True

    @staticmethod
    def _activation_failure(runtime: Runtime, task_id: str, binding: dict[str, Any]) -> dict[str, str] | None:
        if "activation_seq" not in binding:
            return None
        advanced = any(event.actor.kind != "human" and not (
            event.type == "activity.queued" and event.payload.get("activity_id") == binding.get("activity_id"))
            for event in runtime.log.iter_events(task_id=task_id, after_seq=binding["activation_seq"],
                event_types=("activity.queued", "planning.started", "work.claimed", "agent.working")))
        if advanced:
            return {"error_type": "ActivationSuperseded",
                    "message": "该任务已有后续执行安排，初次启动请求不再重复执行。"}
        return None

    def dispatch_conversation(self, task_id: str, result: dict[str, Any]) -> dict[str, Any]:
        """Adopt only durable execution authorization, once per conversation."""
        seq = result.get("conversation_seq")
        if type(seq) is not int:
            return self.status(task_id)
        with self._lock:
            with self._runtime_factory() as runtime:
                events = runtime.log.read(task_id=task_id)
                finished = next((event for event in events if event.type == "conversation.finished"
                    and event.actor == EventActor.system("conversation")
                    and event.payload.get("conversation_seq") == seq), None)
                if finished is None or (result.get("finish_seq") is not None and result["finish_seq"] != finished.seq):
                    return self.status(task_id)
                payload = finished.payload
                if (payload.get("status") != "finished" or payload.get("next_action") != "run"
                        or payload.get("action") not in {"update_task", "resume_task"}
                        or not isinstance(payload.get("scope_before"), dict) or not payload["scope_before"]):
                    return self.status(task_id)
                command = next((event for event in events if event.type == "human.commanded"
                    and event.actor == EventActor.human() and event.payload.get("conversation_seq") == seq), None)
                intent = "direction" if payload["action"] == "update_task" else "resume"
                if (command is None or command.payload.get("intent") != intent
                        or (payload.get("command_seq") is not None and payload["command_seq"] != command.seq)):
                    return self.status(task_id)
                if any(event.type == "activity.queued" and event.actor == EventActor.system("orchestrator")
                       and event.payload.get("conversation_seq") == seq for event in events):
                    return self.status(task_id)
                expected_scope = dict(payload["scope_before"])
                if intent == "direction":
                    expected_scope["direction_seq"] = command.seq
                binding = {"conversation_seq": seq, "expected_scope": expected_scope, "finished_seq": finished.seq}
            try:
                return self._submit(task_id, "run", queue_after_current=True, binding=binding)
            except WorkConflict:
                # Another runner may have durably queued the same authorization.
                return self.status(task_id)

    def continue_after_current(self, task_id: str, action: str) -> dict[str, Any]:
        """Queue one follow-up round without racing the active one.

        A human direction is already durable in the event log when this method
        runs.  Keeping the current round intact avoids force-killing a local
        CLI process, while the queued follow-up makes the new direction visible
        to a freshly assembled runtime context.
        """
        return self._submit(task_id, action, queue_after_current=True)

    def dispatch_control(self, task_id: str, event: Event) -> dict[str, Any]:
        """Claim a durable direct pause-to-resume transition, including retries.

        The route must call this for every resume response, even changed=False.
        Chat control is dispatched through its conversation.finished instead.
        """
        with self._lock:
            with self._runtime_factory() as runtime:
                events = runtime.log.read(task_id=task_id)
                control = next((row for row in events if row.seq == event.seq
                    and row.type == "task.execution_controlled" and row.actor == EventActor.human()), None)
                if (control is None or control.payload.get("action") != "resume"
                        or control.payload.get("state") != "running" or "conversation_seq" in control.payload):
                    return self.status(task_id)
                previous = next((row for row in events if row.seq == control.payload.get("previous_seq")
                    and row.type == "task.execution_controlled" and row.actor == EventActor.human()), None)
                if previous is None or previous.seq >= control.seq or previous.payload.get("state") != "paused":
                    return self.status(task_id)
                if any(row.type == "activity.queued" and row.actor == EventActor.system("orchestrator")
                       and row.payload.get("resume_seq") == control.seq for row in events):
                    return self.status(task_id)
                scope = control.payload.get("scope")
                binding = {"resume_seq": control.seq,
                           "expected_scope": dict(scope) if isinstance(scope, dict) else None}
            try:
                return self._submit(task_id, "run", queue_after_current=True, binding=binding)
            except WorkConflict:
                # Suppress only a duplicate claim, not a failed queue repair/write.
                with self._runtime_factory() as runtime:
                    if any(row.actor == EventActor.system("orchestrator")
                           and row.payload.get("resume_seq") == control.seq for row in runtime.log.iter_events(
                               task_id=task_id, event_types=("activity.queued",))):
                        return self.status(task_id)
                raise

    def _submit(self, task_id: str, action: str, *, queue_after_current: bool,
                binding: dict[str, Any] | None = None, run_inline: bool = False) -> dict[str, Any]:
        with self._lock:
            current = self._activities.get(task_id)
            with self._runtime_factory() as runtime:
                control = self._control_failure(runtime, task_id, binding)
                if control is None and binding and any(key in binding for key in ("resume_seq", "activation_seq")):
                    expected_scope = binding.get("expected_scope")
                    if not isinstance(expected_scope, dict) or not expected_scope:
                        control = {"error_type": "MissingExecutionScope",
                                   "message": "旧恢复请求未记录授权范围，本轮未执行；请按当前方向重新明确授权。"}
                    elif expected_scope != runtime.work.scope(task_id):
                        control = {"error_type": "ExecutionScopeChanged",
                                   "message": "执行请求授权的任务方向已变化，本轮未执行；请确认最新方向后重新授权。"}
                if control is None and binding and "activation_seq" in binding:
                    control = self._activation_failure(runtime, task_id, binding)
                queued_control = (self._control_failure(runtime, task_id, current)
                                  if current and current["state"] == "queued" else None)
                followup_control = (self._control_failure(runtime, task_id, current["follow_up"])
                                    if current and current.get("follow_up") else None)
            if followup_control and not self._close_follow_up(task_id, current, followup_control):
                raise WorkConflict("排队请求的终态尚未写入，请先恢复记录后再继续。")
            if queued_control:
                # _mark_running takes this same lock. Only a never-started local
                # worker can be retired here; an in-flight call must return first.
                self._record_failure(task_id, current["id"], current["action"],
                    **queued_control, execution_started=False)
                self._finish(task_id, current["id"], "failed", error=queued_control["message"])
            if current and current["state"] not in {"queued", "running"} and current.get("follow_up"):
                # A failed terminal write must be repaired before replacing this queue.
                self._finish(task_id, current["id"], current["state"], error=current.get("error"))
                if current.get("follow_up"):
                    raise WorkConflict("排队请求的终态尚未写入，请先恢复记录后再继续。")
            if control:
                rejected = {"id": "activity_" + uuid.uuid4().hex[:10], "action": action,
                            "queued_at": _now_iso(), **(binding or {})}
                self._record_queued(task_id, rejected)
                terminal, _ = self._record_failure(task_id, rejected["id"], action,
                    **control, execution_started=False)
                if not current or current["state"] not in {"queued", "running"}:
                    self._activities[task_id] = {**rejected, "state": "failed", "error": control["message"],
                                                 "finished_at": terminal.ts}
                self._revision += 1
                return self.status(task_id)
            if current and current["state"] in {"queued", "running"}:
                if queue_after_current:
                    queued = current.get("follow_up")
                    if not queued:
                        queued = {
                            "id": "activity_" + uuid.uuid4().hex[:10],
                            "action": action,
                            "queued_at": _now_iso(),
                            **(binding or {}),
                        }
                        current["follow_up"] = queued
                        try:
                            self._record_queued(task_id, queued, after_activity_id=current["id"])
                            self._revision += 1
                        except Exception:
                            current.pop("follow_up", None)
                            raise
                    elif (queued["action"] != action or
                          {key: queued[key] for key in self._BINDING_KEYS if key in queued}
                          != (binding or {})):
                        revised = {key: value for key, value in queued.items()
                                   if key not in self._BINDING_KEYS}
                        revised.update(action=action, revised_at=_now_iso(), **(binding or {}))
                        self._record_queued(task_id, revised, after_activity_id=current["id"],
                                            replaces_action=queued["action"])
                        current["follow_up"] = revised
                        self._revision += 1
                return self.status(task_id)
            activity = {
                "id": "activity_" + uuid.uuid4().hex[:10],
                "action": action,
                "state": "queued",
                "queued_at": _now_iso(),
                **(binding or {}),
            }
            # Publish to the in-memory queue only after the durable claim succeeds.
            self._record_queued(task_id, activity)
            self._activities[task_id] = activity
            self._revision += 1
            if activity["state"] == "failed":
                return self.status(task_id)

        if run_inline:
            self._run(task_id, activity["id"], action)
        else:
            self._start(task_id, activity)
        return self.status(task_id)

    def _record_queued(
        self,
        task_id: str,
        activity: dict[str, Any],
        *,
        after_activity_id: str | None = None,
        replaces_action: str | None = None,
    ) -> None:
        with self._runtime_factory() as runtime:
            if runtime.task(task_id) is None:
                raise KeyError(task_id)
            payload: dict[str, Any] = {"activity_id": activity["id"], "action": activity["action"]}
            if after_activity_id:
                payload["after_activity_id"] = after_activity_id
            if replaces_action:
                payload["replaces_action"] = replaces_action
            payload.update({key: activity[key] for key in self._BINDING_KEYS
                            if key in activity})
            source_key = next((key for key in ("conversation_seq", "resume_seq", "activation_seq") if key in payload), None)
            if source_key is not None:
                def claim():
                    if any(event.actor == EventActor.system("orchestrator")
                           and event.payload.get(source_key) == payload[source_key]
                           for event in runtime.log.iter_events(task_id=task_id, event_types=("activity.queued",))):
                        raise WorkConflict("这条消息已排队，不重复执行。")
                    return "activity.queued", payload, EventActor.system("orchestrator")
                if source_key == "activation_seq":
                    with runtime.log.atomic():
                        rejection = self._activation_failure(runtime, task_id, payload)
                        activity["queued_seq"] = runtime.work._write(task_id, claim).seq
                        if rejection:
                            terminal, _ = self._record_terminal(runtime, task_id, activity["id"],
                                activity["action"], "activity.failed", **rejection, execution_started=False)
                            activity.update(state="failed", error=rejection["message"], finished_at=terminal.ts)
                else:
                    activity["queued_seq"] = runtime.work._write(task_id, claim).seq
                return
            activity["queued_seq"] = runtime.log.emit(
                actor=EventActor.system("orchestrator"),
                type="activity.queued",
                task_id=task_id,
                payload=payload,
            ).seq

    def _start(self, task_id: str, activity: dict[str, Any]) -> None:
        ownership = ExitStack()
        try:
            with self._runtime_factory() as runtime:
                database = runtime.log.path
            # Reserve before starting the thread: when called by _finish, the
            # parent's lock is still held. The worker closes this stack.
            owned = ownership.enter_context(activity_owner(database, task_id, activity["id"]))
            thread = threading.Thread(
                target=self._run,
                args=(task_id, activity["id"], activity["action"]),
                kwargs={"_ownership": (ownership, owned)},
                name=f"cater-{activity['action']}-{task_id}",
                daemon=True,
            )
            thread.start()
        except BaseException as exc:
            ownership.close()
            if not isinstance(exc, Exception):
                raise
            try:
                with self._runtime_factory() as runtime:
                    with activity_owner(runtime.log.path, task_id, activity["id"]) as owned:
                        if owned:
                            self._fail_activity(task_id, activity["id"], activity["action"], exc)
            except Exception:
                _LOGGER.exception("Cannot record thread start failure for %s", task_id)
            finally:
                with self._lock:
                    current = self._activities.get(task_id)
                    if current and current["id"] == activity["id"] and current["state"] == "queued":
                        current["state"] = "observing"
                        self._revision += 1
            raise

    def _run(self, task_id: str, activity_id: str, action: str,
             *, _ownership: tuple[ExitStack, bool] | None = None) -> None:
        owned = False
        try:
            with ExitStack() as ownership:
                if _ownership is not None:
                    ownership.enter_context(_ownership[0])
                    owned = _ownership[1]
                else:
                    with self._runtime_factory() as runtime:
                        database = runtime.log.path
                    owned = ownership.enter_context(activity_owner(database, task_id, activity_id))
                if owned:
                    self._run_owned(task_id, activity_id, action)
        finally:
            # Initialization failure or BaseException must not hide an ownerless
            # queue from recovery. A contending worker cannot retire a live one.
            with self._lock:
                current = self._activities.get(task_id)
                if (current and current["id"] == activity_id
                        and (current["state"] == "queued" or (owned and current["state"] == "running"))):
                    current["state"] = "observing"
                    self._revision += 1

    def _run_owned(self, task_id: str, activity_id: str, action: str) -> None:
        control = None
        try:
            with self._runtime_factory() as runtime:
                if any(event.actor == EventActor.system("orchestrator")
                       and event.payload.get("activity_id") == activity_id
                       for event in runtime.log.iter_events(task_id=task_id,
                           event_types=("activity.completed", "activity.failed"))):
                    return
                queued = [event for event in runtime.log.iter_events(task_id=task_id, event_types=("activity.queued",))
                          if event.actor == EventActor.system("orchestrator")
                          and event.payload.get("activity_id") == activity_id]
                binding = {**queued[-1].payload, "queued_seq": queued[-1].seq} if queued else {}
                control = self._control_failure(runtime, task_id, binding)
                if control:
                    raise ExecutionScopeChanged(control["message"])
                self._check_reconciliation(runtime, task_id, binding)
                if "expected_scope" in binding and runtime.work.scope(task_id) != binding["expected_scope"]:
                    raise ExecutionScopeChanged("这条消息授权的任务方向已变化，本轮未开始执行；请确认最新方向后继续。")
                if not self._mark_running(task_id, activity_id):
                    return
                if "activation_seq" in binding and not self._claim_activation_start(runtime, task_id, binding):
                    self._finish(task_id, activity_id, "observing")
                    return
                if action == "run":
                    if "expected_scope" in binding:
                        runtime.collaborate(task_id, expected_scope=binding["expected_scope"])
                    else:
                        runtime.collaborate(task_id)
                elif action == "battle":
                    if "expected_scope" in binding:
                        runtime.battle(task_id, expected_scope=binding["expected_scope"])
                    else:
                        runtime.battle(task_id)
                else:
                    raise ValueError(f"unsupported background action {action!r}")
                control = self._control_failure(runtime, task_id)
                if control:
                    raise ExecutionScopeChanged(control["message"])
                # A normal return can still mean planning/review stopped at an unknown call.
                self._check_reconciliation(runtime, task_id, binding, check_activities=False)
                terminal, _ = self._record_terminal(runtime, task_id, activity_id, action, "activity.completed")
        except Exception as exc:
            self._fail_activity(task_id, activity_id, action, exc, control=control)
            return
        self._finish(task_id, activity_id, "completed" if terminal.type == "activity.completed" else "failed",
                     error=terminal.payload.get("message"))

    @staticmethod
    def _control_failure(runtime: Runtime, task_id: str, binding: dict[str, Any] | None = None) -> dict[str, Any] | None:
        control = runtime.execution_control.view(task_id)
        authorization_seq = (binding or {}).get("resume_seq",
            (binding or {}).get("conversation_seq", (binding or {}).get("activation_seq", (binding or {}).get("queued_seq"))))
        if control["state"] == "running" and authorization_seq is not None:
            if binding and "conversation_seq" in binding and "finished_seq" in binding:
                # FIFO chat may adopt pause and resume after both messages arrived.
                # The validated command marks when this message acquired permission.
                command = next((event for event in runtime.log.iter_events(task_id=task_id,
                    event_types=("human.commanded",)) if event.actor == EventActor.human()
                    and event.payload.get("conversation_seq") == binding["conversation_seq"]
                    and event.seq < binding["finished_seq"]), None)
                if command:
                    authorization_seq = command.seq
            # Resume authorizes new work; it does not revive requests queued before pause.
            stopped = next((event for event in runtime.log.iter_events(task_id=task_id,
                event_types=("task.execution_controlled",), after_seq=authorization_seq)
                if event.actor == EventActor.human() and event.payload.get("state") in {"paused", "cancelled"}), None)
            if stopped:
                control = {"state": stopped.payload["state"], "seq": stopped.seq, "reason": stopped.payload["reason"]}
        if control["state"] == "running":
            return None
        return {"error_type": "ExecutionPaused" if control["state"] == "paused" else "ExecutionCancelled",
                "message": control["reason"], "control_seq": control["seq"], "execution_state": control["state"]}

    @staticmethod
    def _check_reconciliation(runtime: Runtime, task_id: str, binding: dict[str, Any] | None = None,
                              *, check_activities: bool = True) -> None:
        from .delivery import DeliveryService, _review_state
        from .planning import Planner, _attempt_state

        # Both folds authenticate recovery evidence; review starts also need the
        # prepared bundle. A filtered event stream would change their meaning.
        events = runtime.log.read(task_id=task_id)
        blocked = []
        pending = {}
        authorization_seq = (binding or {}).get("resume_seq",
            (binding or {}).get("conversation_seq", (binding or {}).get("queued_seq")))
        _, planning_finishes, _, planning_pending = _attempt_state(events)
        _, review_finishes, review_pending = _review_state(events)
        if planning_pending:
            Planner(runtime).recover(task_id)
        if review_pending:
            DeliveryService(runtime).recover(task_id)
        # Closing a safe expired text claim is not permission to replay it in
        # this activity. The caller must authorize again after reconciliation.
        blocked.extend(event.seq for event in (*planning_pending, *review_pending))
        blocked.extend(event.seq for event in (*planning_finishes.values(), *review_finishes.values())
            if authorization_seq is not None and event.payload.get("status") == "interrupted"
            and event.seq > authorization_seq)
        for started in events:
            if started.actor == EventActor.system("orchestrator"):
                activity_id = started.payload.get("activity_id")
                if started.type == "activity.queued":
                    pending[activity_id] = started
                    if (check_activities and binding and "resume_seq" in binding
                            and binding["resume_seq"] < started.seq < binding.get("queued_seq", 0)
                            and "resume_seq" not in started.payload and "conversation_seq" not in started.payload):
                        # Legacy /control queues have no source key. They may
                        # already have executed this resume; never infer a replay.
                        blocked.append(started.seq)
                elif started.type in {"activity.completed", "activity.failed"}:
                    pending.pop(activity_id, None)
            if (check_activities and binding and started.type == "activity.failed"
                    and started.actor == EventActor.system("orchestrator")
                    and authorization_seq is not None and started.seq > authorization_seq
                    and started.payload.get("activity_id") != binding.get("activity_id")
                    and started.payload.get("error_type") != "ExecutionScopeChanged"
                    and not (started.payload.get("execution_started") is False
                             and started.payload.get("error_type") in {"ExecutionPaused", "ExecutionCancelled"}
                             and type(started.payload.get("control_seq")) is int
                             and started.payload["control_seq"] < authorization_seq)):
                # A message sent before the failure is not a fresh recovery instruction.
                blocked.append(started.seq)
        if check_activities and binding:
            blocked.extend(event.seq for activity_id, event in pending.items()
                if activity_id != binding.get("activity_id")
                and event.payload.get("after_activity_id") != binding.get("activity_id"))
        if blocked:
            references = ", ".join(f"#{seq}" for seq in blocked)
            raise WorkConflict(f"此前活动失败或规划/整体复核结果尚未核对（{references}）；本轮停止自动接续，请先核对后重新授权。")

    def _record_failure(self, task_id: str, activity_id: str, action: str, **details: Any) -> tuple[Event, bool]:
        """Close a queue claim once, retaining the latest authorization and predecessor."""
        with self._runtime_factory() as runtime:
            return self._record_terminal(runtime, task_id, activity_id, action, "activity.failed", **details)

    @staticmethod
    def _activity_terminal(runtime: Runtime, task_id: str, activity_id: str) -> Event | None:
        return next((event for event in runtime.log.iter_events(task_id=task_id,
            event_types=("activity.completed", "activity.failed"))
            if event.actor == EventActor.system("orchestrator")
            and event.payload.get("activity_id") == activity_id), None)

    @staticmethod
    def _record_terminal(runtime: Runtime, task_id: str, activity_id: str, action: str,
                         kind: str, **details: Any) -> tuple[Event, bool]:
        with runtime.log.atomic():
            existing = ActivityRunner._activity_terminal(runtime, task_id, activity_id)
            if existing is not None:
                return existing, False
            queued = [event for event in runtime.log.iter_events(task_id=task_id, event_types=("activity.queued",))
                      if event.actor == EventActor.system("orchestrator")
                      and event.payload.get("activity_id") == activity_id]
            payload = {**queued[-1].payload, "queued_seq": queued[-1].seq} if queued else {}
            payload.update(activity_id=activity_id, action=action, **details)
            return runtime.log.emit(type=kind, task_id=task_id, actor=EventActor.system("orchestrator"),
                                    payload=payload), True

    def _fail_activity(self, task_id: str, activity_id: str, action: str, exc: Exception,
                       *, control: dict[str, Any] | None = None) -> None:
        recorded = False
        try:
            if control is None:
                with self._runtime_factory() as runtime:
                    control = self._control_failure(runtime, task_id)
            details = ({**control, "cause_error_type": type(exc).__name__, "cause_message": str(exc)}
                       if control else {"error_type": type(exc).__name__, "message": str(exc)})
            terminal, _ = self._record_failure(task_id, activity_id, action,
                **details)
            recorded = terminal.type == "activity.failed" and terminal.payload.get("error_type") == "ExecutionScopeChanged"
        except Exception:
            _LOGGER.exception("Cannot record activity failure for %s at %s", task_id, activity_id)
        self._finish(task_id, activity_id, "failed", error=str(exc),
                     follow_after_failure=recorded and isinstance(exc, ExecutionScopeChanged))

    def _close_follow_up(self, task_id: str, activity: dict[str, Any], failure: dict[str, Any]) -> bool:
        queued = activity["follow_up"]
        try:
            self._record_failure(task_id, queued["id"], queued["action"], **failure,
                after_activity_id=activity["id"], execution_started=False)
        except Exception:
            queued.update(state="blocked", error=failure["message"])
            _LOGGER.exception("Cannot close follow-up for %s at %s", task_id, queued["id"])
            closed = False
        else:
            activity.pop("follow_up")
            closed = True
        self._revision += 1
        return closed

    def _mark_running(self, task_id: str, activity_id: str) -> bool:
        with self._lock:
            activity = self._activities.get(task_id)
            if activity and activity["id"] == activity_id and activity["state"] == "queued":
                activity["state"] = "running"
                activity["started_at"] = _now_iso()
                self._revision += 1
                return True
            return bool(activity and activity["id"] == activity_id and activity["state"] == "running")

    def _finish(self, task_id: str, activity_id: str, state: str, *, error: str | None = None,
                follow_after_failure: bool = False) -> None:
        follow_up: dict[str, Any] | None = None
        with self._lock:
            activity = self._activities.get(task_id)
            if not activity or activity["id"] != activity_id:
                return
            activity["state"] = state
            activity["finished_at"] = _now_iso()
            if error:
                activity["error"] = error
            queued = activity.get("follow_up")
            if isinstance(queued, dict):
                try:
                    with self._runtime_factory() as runtime:
                        control = self._control_failure(runtime, task_id, queued)
                except Exception:
                    queued.update(state="blocked", error="无法核对执行控制状态，排队请求尚未执行。")
                    self._revision += 1
                    _LOGGER.exception("Cannot check follow-up control for %s at %s", task_id, queued["id"])
                    return
                if control:
                    self._close_follow_up(task_id, activity, control)
                    return
            if (state == "completed" or (state == "failed" and follow_after_failure)) and isinstance(queued, dict):
                follow_up = {**queued, "state": "queued"}
                activity.pop("follow_up")
                self._activities[task_id] = follow_up
            elif isinstance(queued, dict):
                message = "前一轮活动失败或结果不明，排队请求未执行；请先核对后按最新方向重新授权。"
                self._close_follow_up(task_id, activity,
                    {"error_type": "PredecessorActivityFailed", "message": message})
            self._revision += 1
        if follow_up is not None:
            self._start(task_id, follow_up)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _driver_mode(driver: Any) -> str:
    """Classify drivers without exposing commands, endpoints, or secrets."""
    return driver_mode(driver)


def serve(
    db_path: str | Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    drivers_path: str | Path | None = None,
    roles_path: str | Path | None = None,
    browser_checker_path: str | Path | None = None,
    driver_attempts: int = 2,
    circuit_breaker_threshold: int = 2,
    audit_key: str | bytes | None = None,
    audit_key_id: str = "local",
    audit_keys: Mapping[str, str | bytes] | None = None,
    allow_remote: bool = False,
    access_token: str | None = None,
) -> None:
    if access_token is not None and not access_token.strip():
        raise ValueError("web access token must not be empty")
    is_loopback = _is_loopback_host(host)
    if not allow_remote and not is_loopback:
        raise ValueError(
            "refusing to expose the unauthenticated Cater control plane beyond localhost; "
            "use --allow-remote with an access token behind a TLS network boundary"
        )
    if not is_loopback and not access_token:
        raise ValueError(
            "refusing to expose the Cater control plane beyond localhost without an access token; "
            "set CATER_WEB_ACCESS_TOKEN or use --access-token-file"
        )
    # Each request needs its own thread-bound SQLite connection.  CLI drivers
    # are process resources, so the server owns one pool for its full lifetime.
    driver_owner = Runtime(":memory:", drivers_path=drivers_path, roles_path=roles_path)
    driver_health = DriverHealth()
    handler = make_handler(
        db_path,
        drivers_path=drivers_path,
        roles_path=roles_path,
        browser_checker_path=browser_checker_path,
        driver_attempts=driver_attempts,
        circuit_breaker_threshold=circuit_breaker_threshold,
        audit_key=audit_key,
        audit_key_id=audit_key_id,
        audit_keys=audit_keys,
        static_dir=_workroom_dist(),
        shared_drivers=driver_owner.drivers,
        shared_driver_health=driver_health,
        access_token=access_token,
    )
    server = ThreadingHTTPServer((host, port), handler)
    print(f"Cater web listening on http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        driver_owner.close()


def _is_loopback_host(host: str) -> bool:
    normalized = host.strip().lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _workroom_dist() -> Path | None:
    """Return a development build first, then the workroom bundled with Cater."""
    package_root = Path(__file__).resolve().parents[1]
    candidates = (
        package_root / "web" / "dist",
        Path(__file__).resolve().parent / "static" / "workroom",
    )
    return next((candidate for candidate in candidates if (candidate / "index.html").is_file()), None)


def make_handler(
    db_path: str | Path,
    *,
    drivers_path: str | Path | None = None,
    roles_path: str | Path | None = None,
    browser_checker_path: str | Path | None = None,
    driver_attempts: int = 2,
    circuit_breaker_threshold: int = 2,
    audit_key: str | bytes | None = None,
    audit_key_id: str = "local",
    audit_keys: Mapping[str, str | bytes] | None = None,
    static_dir: str | Path | None = None,
    runtime_factory: Callable[[], Runtime] | None = None,
    shared_drivers: dict[str, Any] | None = None,
    shared_driver_health: DriverHealth | None = None,
    access_token: str | None = None,
):
    if access_token is not None and not access_token.strip():
        raise ValueError("web access token must not be empty")
    static_root = Path(static_dir).resolve() if static_dir is not None else None

    def build_runtime() -> Runtime:
        if runtime_factory is not None:
            return runtime_factory()
        return Runtime(
            db_path,
            drivers_path=drivers_path,
            roles_path=roles_path,
            browser_checker_path=browser_checker_path,
            driver_attempts=driver_attempts,
            circuit_breaker_threshold=circuit_breaker_threshold,
            audit_key=audit_key,
            audit_key_id=audit_key_id,
            audit_keys=audit_keys,
            drivers=shared_drivers,
            close_drivers_on_close=shared_drivers is None,
            driver_health=shared_driver_health,
        )

    activities = ActivityRunner(build_runtime)
    activities.recover_interrupted()
    summaries_lock = threading.Lock()
    summaries_head: str | None = None
    summaries_base: list[dict[str, Any]] = []
    summaries_lease_version = 0.0
    summaries_deadline = float("inf")

    class CaterHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if not self._has_valid_request_host():
                return self._forbidden("untrusted Host header")
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            if static_root is not None and (path == "/" or path.startswith("/assets/") or path == "/favicon.svg"):
                static_path = static_root / "index.html" if path == "/" else static_root / unquote(path.lstrip("/"))
                try:
                    static_path.resolve().relative_to(static_root)
                except ValueError:
                    return self._not_found()
                if static_path.is_file():
                    return self._send_static(static_path)
                return self._not_found()
            if self._requires_access_token(path) and not self._has_valid_access_token():
                return self._unauthorized()
            if path == "/":
                return self._send_html(render_index(self._runtime()))
            if path == "/api/task-summaries":
                return self._send_task_summaries()
            if path == "/api/rooms" or path.startswith("/api/rooms/"):
                return self._get_rooms(path, parsed.query)
            if path == "/api/tasks":
                with self._runtime() as rt:
                    return self._send_json(
                        [
                            {**task_to_dict(task, include_content=False), "activity": activities.status(task.id)}
                            for task in rt.tasks()
                        ]
                    )
            if path == "/api/projects":
                with self._runtime() as rt:
                    return self._send_json(rt.rulebook.projects())
            if path == "/api/rules":
                query = parse_qs(parsed.query)
                with self._runtime() as rt:
                    try:
                        state = rt.rulebook.view(
                            task_id=query.get("task_id", [None])[0],
                            project_id=query.get("project_id", [None])[0],
                        )
                    except ValueError as exc:
                        return self._bad_request(str(exc))
                    return self._send_json(state)
            if path == "/api/organization":
                with self._runtime() as rt:
                    return self._send_json(
                        [
                            {
                                "id": role.id,
                                "name": role.name,
                                "role": role.role,
                                "cabinet_member": role.id in rt.organization.cabinet_members,
                            }
                            for role in rt.worker_roles
                        ]
                    )
            if path == "/api/organization/profile":
                with self._runtime() as rt:
                    worker_drivers = [
                        {
                            "role_id": role.id,
                            "name": role.name,
                            "driver": role.driver,
                            "mode": _driver_mode(rt.drivers[role.driver]),
                        }
                        for role in rt.worker_roles
                    ]
                    modes = {item["mode"] for item in worker_drivers}
                    leader_driver = rt.organization.leader.driver
                    if leader_driver is not None:
                        modes.add(_driver_mode(rt.drivers[leader_driver]))
                    return self._send_json(
                        {
                            "leader": _role_to_dict(rt.organization.leader),
                            "cabinet": [_role_to_dict(role) for role in rt.cabinet_roles],
                            "workers": [_role_to_dict(role) for role in rt.worker_roles],
                            "mode": (
                                "local_demo"
                                if modes <= {"local_demo"}
                                else "external_driver" if modes <= {"external"} else "mixed_driver"
                            ),
                            "worker_drivers": worker_drivers,
                            "planning_mode": "local_rules" if leader_driver is None else _driver_mode(rt.drivers[leader_driver]),
                            "availability": team_availability(rt),
                        }
                    )
            if path == "/api/chain":
                with self._runtime() as rt:
                    return self._send_json(chain_to_dict(rt.verify_chain()))
            if path.startswith("/api/tasks/"):
                task_id = path.split("/", 3)[3]
                if task_id.endswith("/delivery"):
                    task_id = task_id.removesuffix("/delivery")
                    with self._runtime() as rt:
                        if rt.task(task_id) is None:
                            return self._not_found()
                        try:
                            raw = rt.delivery.archive(task_id)
                        except ValueError as exc:
                            return self._conflict(str(exc))
                        self.send_response(HTTPStatus.OK)
                        self.send_header("Content-Type", "application/zip")
                        self.send_header("Content-Disposition", 'attachment; filename="cater-delivery.zip"')
                        self.send_header("Cache-Control", "no-store")
                        self.send_header("Content-Length", str(len(raw)))
                        self.end_headers()
                        self.wfile.write(raw)
                        return
                with self._runtime() as rt:
                    task = rt.task(task_id)
                    if task is None:
                        return self._not_found()
                    return self._send_json(
                        task_api_payload(rt, task_id, activity=activities.status(task_id))
                    )
            if path.startswith("/tasks/"):
                task_id = path.split("/", 2)[2]
                query = parse_qs(parsed.query)
                timeline_category = query.get("category", [""])[0].strip() or None
                with self._runtime() as rt:
                    task = rt.task(task_id)
                    if task is None:
                        return self._not_found()
                    events = rt.log.read(task_id=task_id)
                    return self._send_html(
                        render_task(
                            task,
                            events,
                            rt.review(task_id),
                            roles=rt.roles,
                            timeline_category=timeline_category,
                        )
                    )
            return self._not_found()

        def do_POST(self) -> None:
            if not self._has_valid_request_host():
                return self._forbidden("untrusted Host header")
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/")
            if self._requires_access_token(path) and not self._has_valid_access_token():
                return self._unauthorized()
            if not self._is_same_origin_write():
                return self._forbidden("cross-origin writes are not allowed")
            if path == "/api/rooms" or path.startswith("/api/rooms/"):
                return self._post_rooms(path)
            if path in {"/api/projects", "/api/rules"} or path.startswith("/api/rules/"):
                data = self._read_json()
                if data is None:
                    return
                with self._runtime() as rt:
                    try:
                        if path == "/api/projects":
                            result = rt.rulebook.create_project(data.get("name"))
                        elif data.get("enforcement", "guidance_only") != "guidance_only":
                            return self._bad_request("rule enforcement is not available; only guidance can be recorded")
                        elif path == "/api/rules":
                            result = rt.rulebook.record(
                                data.get("text"), scope=data.get("scope"),
                                task_id=data.get("task_id"), project_id=data.get("project_id"),
                            )
                        else:
                            result = rt.rulebook.update(
                                path.split("/")[-1], revision=data.get("revision"),
                                text=data.get("text"), status=data.get("status"), task_id=data.get("task_id"),
                            )
                    except RuleConflict as exc:
                        return self._conflict(str(exc))
                    except ValueError as exc:
                        return self._bad_request(str(exc))
                    return self._send_json(result, status=HTTPStatus.CREATED if path in {"/api/projects", "/api/rules"} else HTTPStatus.OK)
            if path == "/api/tasks":
                data = self._read_json()
                if data is None:
                    return
                goal = str(data.get("goal", "")).strip()
                if not goal:
                    return self._bad_request("goal is required")
                run_now = _truthy(data.get("run"))
                force_battle = _truthy(data.get("battle"))
                if run_now and force_battle:
                    return self._bad_request("choose either run for normal collaboration or battle for forced governance")
                background = _truthy(data.get("background"))
                assign_owner = str(data.get("assign_to", "")).strip()
                requested_route = _optional_text(data.get("requested_route"))
                acceptance = _acceptance_lines(data.get("acceptance"))
                with self._runtime() as rt:
                    if not assign_owner and requested_route is None:
                        mentioned_workers = rt.mentioned_workers(goal)
                        if len(mentioned_workers) == 1:
                            assign_owner = mentioned_workers[0]
                            requested_route = "direct"
                    elif assign_owner and requested_route is None:
                        # API callers that name a worker are expressing the
                        # same lightweight preference as an @ mention.  Intake
                        # still records the leader's check before direct work.
                        requested_route = "direct"
                    if assign_owner and assign_owner not in {role.id for role in rt.worker_roles}:
                        return self._bad_request(f"unknown role {assign_owner!r}")
                    if requested_route is not None and requested_route not in {"auto", "direct"}:
                        return self._bad_request("requested_route must be auto or direct")
                    try:
                        from .task_requests import prepare
                        task_id, created, activation = prepare(rt, {
                            "goal": goal, "acceptance": acceptance, "project_id": data.get("project_id"),
                            "requested_route": requested_route, "assign_to": assign_owner,
                            "assign_objective": _optional_text(data.get("assign_objective")),
                            "assign_acceptance": _optional_text(data.get("assign_acceptance")),
                            "run": run_now, "battle": force_battle, "background": background,
                            "dispatch": _truthy(data.get("dispatch")),
                        }, data.get("request_id"))
                    except WorkConflict as exc:
                        return self._conflict(str(exc))
                    except ValueError as exc:
                        return self._bad_request(str(exc))
                background_action = "run" if run_now else "battle" if force_battle else None
                if activation is not None:
                    activities.dispatch_activation(task_id, activation, background=background)
                return self._send_json(
                    self._task_payload(task_id, activities),
                    status=(HTTPStatus.OK if not created else
                            HTTPStatus.ACCEPTED if background and background_action else HTTPStatus.CREATED),
                )
            if path.startswith("/api/tasks/"):
                parts = path.split("/")
                if len(parts) != 5:
                    return self._not_found()
                task_id = parts[3]
                action = parts[4]
                data = self._read_json()
                if data is None:
                    return
                background = _truthy(data.get("background"))
                background_action: str | None = None
                if action in {"run", "battle"} and not background and activities.status(task_id)["active"]:
                    return self._conflict("a collaboration activity is already running for this task")
                with self._runtime() as rt:
                    if rt.task(task_id) is None:
                        return self._not_found()
                    try:
                        if action == "control":
                            if set(data) - {"action", "request_id"}:
                                return self._bad_request("执行控制只接收动作和请求标识。")
                            event, _ = rt.execution_control.change(task_id, data.get("action"), request_id=data.get("request_id"))
                            if data["action"] == "resume":
                                activities.dispatch_control(task_id, event)
                            return self._send_json(self._task_payload(task_id, activities))
                        elif action == "questions":
                            if set(data) - {"question", "request_id", "role_id"}:
                                return self._bad_request("提问只接收问题、请求标识与已配置的伙伴。")
                            rt.questions.ask(task_id, data.get("question"), request_id=data.get("request_id"), role_id=data.get("role_id"))
                            activities.answer_questions(task_id)
                            return self._send_json(self._task_payload(task_id, activities), status=HTTPStatus.ACCEPTED)
                        elif action == "clarifications":
                            if set(data) - {"question_seq", "kind", "request_id", "option_index", "answer"}:
                                return self._bad_request("回复只接收问题编号、选择或自填内容和请求标识。")
                            _, created = rt.clarifications.respond(task_id,
                                question_seq=data.get("question_seq"), kind=data.get("kind"), request_id=data.get("request_id"),
                                option_index=data.get("option_index"), answer=data.get("answer"))
                            if created:
                                background_action = "run"
                        elif action == "derive":
                            if set(data) - {"goal", "acceptance", "request_id", "run", "background"}:
                                return self._bad_request("来源背景与项目由服务器确定，不能由请求替换。")
                            result = rt.derive_task(task_id, data.get("goal"),
                                                    _acceptance_lines(data.get("acceptance")),
                                                    request_id=data.get("request_id"))
                            child_id = result["task_id"]
                            if result["created"]:
                                mentioned = rt.mentioned_workers(data["goal"])
                                owner = mentioned[0] if len(mentioned) == 1 else None
                                rt.intake(child_id, requested_route="direct" if owner else "auto", preferred_owner=owner)
                                if _truthy(data.get("run")):
                                    activities.submit(child_id, "run")
                            return self._send_json(self._task_payload(child_id, activities),
                                                   status=HTTPStatus.CREATED if result["created"] else HTTPStatus.OK)
                        elif action == "project":
                            if activities.status(task_id)["active"]:
                                return self._conflict("cannot bind a project while collaboration is active")
                            rt.rulebook.bind_project(task_id, data.get("project_id"))
                        elif action == "run":
                            if background:
                                background_action = "run"
                            else:
                                rt.collaborate(task_id)
                        elif action == "battle":
                            if background:
                                background_action = "battle"
                            else:
                                rt.battle(task_id)
                        elif action == "dispatch":
                            rt.dispatch(task_id)
                        elif action == "assign":
                            owner = str(data.get("owner", "")).strip()
                            if not owner:
                                return self._bad_request("owner is required")
                            rt.assign(
                                task_id,
                                owner,
                                objective=_optional_text(data.get("objective")),
                                acceptance=_optional_text(data.get("acceptance")),
                            )
                        elif action == "command":
                            instruction = _optional_text(data.get("instruction")) or "Updated from Cater API"
                            command = rt.chat(
                                task_id,
                                instruction,
                                continue_work=_truthy(data.get("continue")),
                                request_id=data.get("request_id"),
                                goal=_optional_text(data.get("goal")),
                                acceptance=(
                                    _acceptance_lines(data.get("acceptance"))
                                    if "acceptance" in data
                                    else None
                                ),
                            )
                            if command.type == "conversation.received":
                                activities.answer_conversations(task_id)
                                return self._send_json(self._task_payload(task_id, activities), status=HTTPStatus.ACCEPTED)
                            elif command.type == "question.asked":
                                activities.answer_questions(task_id)
                            elif _truthy(data.get("continue")) and command.payload.get("next_action") != "none":
                                background_action = str(command.payload.get("next_action") or "run")
                        elif action == "decide":
                            rt.decide(
                                task_id,
                                str(data.get("resolution", "done")),
                                _optional_text(data.get("reason")) or "Decided from Cater API",
                            )
                        else:
                            return self._not_found()
                    except (RuleConflict, WorkConflict) as exc:
                        return self._conflict(str(exc))
                    except ValueError as exc:
                        return self._bad_request(str(exc))
                if background_action:
                    if action in {"command", "clarifications"}:
                        activities.continue_after_current(task_id, background_action)
                    else:
                        activities.submit(task_id, background_action)
                return self._send_json(
                    self._task_payload(task_id, activities),
                    status=HTTPStatus.ACCEPTED if background_action else HTTPStatus.OK,
                )
            if path == "/tasks":
                data = self._read_form()
                goal = data.get("goal", [""])[0].strip()
                if not goal:
                    self.send_response(HTTPStatus.SEE_OTHER)
                    self.send_header("Location", "/")
                    self.end_headers()
                    return
                acceptance = [
                    line.strip()
                    for line in data.get("acceptance", [""])[0].splitlines()
                    if line.strip()
                ]
                with self._runtime() as rt:
                    try:
                        task_id = rt.create_task(goal, acceptance)
                    except ValueError as exc:
                        return self._bad_request(str(exc))
                    if data.get("dispatch", [""])[0] == "on":
                        rt.dispatch(task_id)
                    assign_owner = data.get("assign_owner", [""])[0].strip()
                    if assign_owner:
                        try:
                            rt.assign(task_id, assign_owner)
                        except ValueError as exc:
                            return self._bad_request(str(exc))
                    if data.get("run_now", data.get("battle", [""]))[0] == "on":
                        rt.collaborate(task_id)
                self._redirect(f"/tasks/{task_id}")
                return
            if path.startswith("/tasks/") and path.endswith("/run"):
                task_id = path.split("/")[2]
                with self._runtime() as rt:
                    if rt.task(task_id) is None:
                        return self._not_found()
                    rt.collaborate(task_id)
                self._redirect(f"/tasks/{task_id}")
                return
            if path.startswith("/tasks/") and path.endswith("/battle"):
                task_id = path.split("/")[2]
                with self._runtime() as rt:
                    if rt.task(task_id) is None:
                        return self._not_found()
                    rt.battle(task_id)
                self._redirect(f"/tasks/{task_id}")
                return
            if path.startswith("/tasks/") and path.endswith("/dispatch"):
                task_id = path.split("/")[2]
                with self._runtime() as rt:
                    if rt.task(task_id) is None:
                        return self._not_found()
                    rt.dispatch(task_id)
                self._redirect(f"/tasks/{task_id}")
                return
            if path.startswith("/tasks/") and path.endswith("/assign"):
                task_id = path.split("/")[2]
                data = self._read_form()
                owner = data.get("owner", [""])[0].strip()
                objective = data.get("objective", [""])[0].strip() or None
                acceptance = data.get("acceptance", [""])[0].strip() or None
                if not owner:
                    self._redirect(f"/tasks/{task_id}")
                    return
                with self._runtime() as rt:
                    if rt.task(task_id) is None:
                        return self._not_found()
                    try:
                        rt.assign(task_id, owner, objective=objective, acceptance=acceptance)
                    except ValueError as exc:
                        return self._bad_request(str(exc))
                self._redirect(f"/tasks/{task_id}")
                return
            if path.startswith("/tasks/") and path.endswith("/command"):
                task_id = path.split("/")[2]
                data = self._read_form()
                instruction = data.get("instruction", [""])[0].strip()
                goal = data.get("goal", [""])[0].strip() or None
                acceptance_raw = data.get("acceptance", [""])[0]
                acceptance = [line.strip() for line in acceptance_raw.splitlines() if line.strip()]
                with self._runtime() as rt:
                    if rt.task(task_id) is None:
                        return self._not_found()
                    try:
                        command = rt.chat(
                            task_id,
                            instruction or "Updated from Cater web",
                            continue_work=data.get("run_now", [""])[0] == "on",
                            request_id=data.get("request_id", [None])[0],
                            goal=goal,
                            acceptance=acceptance if acceptance_raw.strip() else None,
                        )
                    except ValueError as exc:
                        return self._bad_request(str(exc))
                    if command.type == "conversation.received":
                        activities.answer_conversations(task_id)
                    elif command.type == "question.asked":
                        activities.answer_questions(task_id)
                    elif data.get("run_now", [""])[0] == "on" and command.payload.get("next_action") != "none":
                        if command.payload.get("next_action") == "battle":
                            rt.battle(task_id)
                        else:
                            rt.collaborate(task_id)
                self._redirect(f"/tasks/{task_id}")
                return
            if path.startswith("/tasks/") and path.endswith("/decide"):
                task_id = path.split("/")[2]
                data = self._read_form()
                resolution = data.get("resolution", ["done"])[0]
                reason = data.get("reason", [""])[0].strip() or "Decided from Cater web"
                with self._runtime() as rt:
                    if rt.task(task_id) is None:
                        return self._not_found()
                    try:
                        rt.decide(task_id, resolution, reason)
                    except ValueError as exc:
                        return self._bad_request(str(exc))
                self._redirect(f"/tasks/{task_id}")
                return
            return self._not_found()

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _runtime(self) -> Runtime:
            return build_runtime()

        def _get_rooms(self, path: str, raw_query: str) -> None:
            parts = path.split("/")
            if path != "/api/rooms" and not (
                len(parts) in {4, 5} and parts[3] and (len(parts) == 4 or parts[4] == "events")
            ):
                return self._not_found()
            with self._runtime() as rt:
                try:
                    if path == "/api/rooms":
                        return self._send_json(rt.rooms.list())
                    room = rt.rooms.get(parts[3])
                    if room is None:
                        return self._not_found()
                    if len(parts) == 4:
                        return self._send_json(room)
                    query = parse_qs(raw_query, keep_blank_values=True)
                    if set(query) - {"after_seq", "membership_seq", "limit"}:
                        return self._bad_request("群聊消息不支持该查询参数。")
                    if any(len(values) != 1 for values in query.values()):
                        return self._bad_request("群聊消息的查询参数不能重复。")
                    try:
                        after_seq = int(query.get("after_seq", ["0"])[0])
                        membership_seq = int(query["membership_seq"][0]) if "membership_seq" in query else None
                        limit = int(query.get("limit", ["100"])[0])
                    except ValueError:
                        return self._bad_request("消息游标、任务归属版本与分页数量必须是整数。")
                    result = rt.rooms.timeline(parts[3], after_seq=after_seq,
                                               membership_seq=membership_seq, limit=limit)
                    return self._send_json({**result, "events": [event_to_dict(event) for event in result["events"]]})
                except WorkConflict as exc:
                    return self._conflict(str(exc))
                except ValueError as exc:
                    return self._bad_request(str(exc))

        def _post_rooms(self, path: str) -> None:
            parts = path.split("/")
            if path == "/api/rooms":
                action = "create"
            elif path == "/api/rooms/import-legacy":
                action = "import-legacy"
            elif len(parts) == 5 and parts[3] and parts[4] in {"messages", "tasks"}:
                action = parts[4]
            else:
                return self._not_found()
            data = self._read_json()
            if data is None:
                return
            allowed = {
                "create": {"title", "request_id"},
                "import-legacy": set(),
                "messages": {"text", "request_id", "task_id", "reply_to"},
                "tasks": ({"task_id"} if "task_id" in data else
                          {"message_id", "goal", "acceptance", "project_id", "request_id"}),
            }[action]
            with self._runtime() as rt:
                try:
                    if action in {"messages", "tasks"} and rt.rooms.get(parts[3]) is None:
                        return self._not_found()
                    if set(data) - allowed:
                        return self._bad_request("群聊请求包含不支持的字段。")
                    if action == "create":
                        result = rt.rooms.create(data.get("title"), request_id=data.get("request_id"))
                    elif action == "import-legacy":
                        return self._send_json(rt.rooms.import_legacy())
                    elif action == "messages":
                        result = rt.rooms.receive(parts[3], data.get("text"), request_id=data.get("request_id"),
                                                  task_id=data.get("task_id"), reply_to=data.get("reply_to"))
                        result = {**result, "message": event_to_dict(result["message"])}
                    elif "task_id" in data:
                        result = rt.rooms.attach(parts[3], data.get("task_id"))
                    else:
                        result = rt.rooms.start_task(parts[3], message_id=data.get("message_id"),
                                                     goal=data.get("goal"), acceptance=data.get("acceptance"),
                                                     project_id=data.get("project_id"), request_id=data.get("request_id"))
                except WorkConflict as exc:
                    return self._conflict(str(exc))
                except ValueError as exc:
                    return self._bad_request(str(exc))
                return self._send_json(result, status=HTTPStatus.CREATED if result["created"] else HTTPStatus.OK)

        def _send_task_summaries(self) -> None:
            nonlocal summaries_head, summaries_base, summaries_lease_version, summaries_deadline
            # Cache only small rows. Stable requests read the indexed event head
            # and activity state, never task history or delivery manifests.
            with summaries_lock, self._runtime() as rt, _read_snapshot(rt):
                activity_revision, activity_rows = activities.snapshot()
                head = rt.log.head_hash()
                now = time.time()
                lease_version, deadline = summaries_lease_version, summaries_deadline
                if summaries_head != head or now > deadline:
                    # Lease expiry changes work/delivery views even without an append.
                    row = rt.log.db.execute("""
                        SELECT MAX(CASE WHEN lease < ? THEN lease END) AS expired,
                               MIN(CASE WHEN lease >= ? THEN lease END) AS upcoming
                        FROM (SELECT json_extract(payload_json, '$.lease_until') AS lease FROM events
                              WHERE type IN ('work.claimed', 'work.check_started', 'work.review_started', 'planning.started',
                                             'delivery.review_started', 'delivery.check_started'))
                    """, (now, now)).fetchone()
                    lease_version, deadline = row["expired"] or 0.0, row["upcoming"] or float("inf")
                # evaluated_at may differ across handler lifetimes; validate
                # semantic row state rather than byte-identical JSON.
                etag = 'W/"' + hashlib.sha256(
                    f"summaries-v1:{head}:{activity_revision}:{lease_version}".encode()
                ).hexdigest() + '"'
                supplied = self.headers.get("If-None-Match", "")
                if any(tag.strip().removeprefix("W/") in {etag.removeprefix("W/"), "*"}
                       for tag in supplied.split(",")):
                    self.send_response(HTTPStatus.NOT_MODIFIED)
                    self.send_header("ETag", etag)
                    self.send_header("Cache-Control", "private, no-cache")
                    self.end_headers()
                    return
                if summaries_head != head or summaries_lease_version != lease_version:
                    version = _event_version(rt)
                    stats: dict[str, Counter] = {}
                    latest: dict[str, int] = {}
                    # Extract only the receipt discriminator, not message/body text.
                    for row in rt.log.db.execute("""
                        SELECT task_id, type,
                            CASE WHEN type = 'msg.said' AND json_extract(payload_json, '$.phase')
                                IN ('planning', 'delivery_receipt', 'clarification_receipt', 'collaboration_request')
                                THEN json_extract(payload_json, '$.phase')
                                WHEN type = 'human.commanded' AND json_extract(payload_json, '$.conversation_seq') IS NOT NULL
                                THEN 'conversation_receipt'
                                WHEN type = 'conversation.finished' AND json_extract(payload_json, '$.action') = 'status_task'
                                AND json_extract(payload_json, '$.progress_seq') IS NOT NULL
                                THEN 'status_receipt' END AS phase,
                            COUNT(*) AS n, MAX(seq) AS latest
                        FROM events WHERE task_id IS NOT NULL GROUP BY task_id, type, phase
                    """):
                        stats.setdefault(row["task_id"], Counter())[(row["type"], row["phase"])] = row["n"]
                        latest[row["task_id"]] = max(latest.get(row["task_id"], 0), row["latest"])
                    rows = [
                        _task_summary(task, version=version, latest_seq=latest[task.id],
                                      execution_control=rt.execution_control.view(task.id),
                                      conversation_count=_conversation_count(stats[task.id]))
                        for task in rt.tasks()
                    ]
                    summaries_base, summaries_head = rows, head
                    summaries_lease_version, summaries_deadline = lease_version, deadline
                rows = [
                    _summary_activity(row, activity_rows.get(row["id"],
                                      {"state": "idle", "active": False, "revision": activity_revision}))
                    for row in summaries_base
                ]
            self._send_json({"tasks": rows}, etag=etag)

        def _task_payload(self, task_id: str, runner: ActivityRunner) -> dict[str, Any]:
            with self._runtime() as runtime:
                return task_api_payload(runtime, task_id, activity=runner.status(task_id))

        def _send_html(self, body: str) -> None:
            raw = body.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _send_json(self, body: Any, *, status: HTTPStatus = HTTPStatus.OK, etag: str | None = None) -> None:
            raw = json.dumps(body, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "private, no-cache" if etag else "no-store")
            if etag:
                self.send_header("ETag", etag)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _send_static(self, path: Path) -> None:
            raw = path.read_bytes()
            content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            # The entry document chooses the current hashed bundle, so it must
            # revalidate after an upgrade. Immutable bundle assets can remain
            # aggressively cached without trapping the UI on an old release.
            if path.name == "index.html":
                self.send_header("Cache-Control", "no-cache")
            elif "/assets/" in path.as_posix():
                self.send_header("Cache-Control", "public, max-age=31536000, immutable")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _not_found(self) -> None:
            raw = b"not found"
            self.send_response(HTTPStatus.NOT_FOUND)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _bad_request(self, message: str) -> None:
            raw = message.encode("utf-8")
            self.send_response(HTTPStatus.BAD_REQUEST)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _conflict(self, message: str) -> None:
            raw = message.encode("utf-8")
            self.send_response(HTTPStatus.CONFLICT)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _forbidden(self, message: str) -> None:
            raw = message.encode("utf-8")
            self.send_response(HTTPStatus.FORBIDDEN)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _unauthorized(self) -> None:
            raw = b"access token is required"
            self.send_response(HTTPStatus.UNAUTHORIZED)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("WWW-Authenticate", 'Bearer realm="Cater"')
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _requires_access_token(self, path: str) -> bool:
            # Static files deliberately stay readable so a remote browser can
            # load the client.  All task data and every mutation remain behind
            # the token; the client forwards it only from a URL fragment.  If
            # no bundled client exists, ``/`` is the legacy server-rendered
            # index and includes task data, so it must be protected as well.
            return access_token is not None and (
                path.startswith("/api/")
                or path.startswith("/tasks")
                or (path == "/" and static_root is None)
            )

        def _has_valid_request_host(self) -> bool:
            # A loopback bind alone does not prevent DNS rebinding. Remote
            # names are usable only with the explicitly authenticated server.
            hosts = self.headers.get_all("Host", [])
            if len(hosts) != 1:
                return False
            host = hosts[0]
            if not host or any(char.isspace() for char in host):
                return False
            try:
                parsed = urlparse("//" + host)
                parsed.port
                if (not parsed.hostname or parsed.username is not None or parsed.password is not None
                        or parsed.path or parsed.query or parsed.fragment):
                    return False
                return access_token is not None or _is_loopback_host(parsed.hostname)
            except ValueError:
                return False

        def _has_valid_access_token(self) -> bool:
            if access_token is None:
                return True
            supplied = self.headers.get("X-Cater-Access-Token", "")
            authorization = self.headers.get("Authorization", "")
            if not supplied and authorization.startswith("Bearer "):
                supplied = authorization.removeprefix("Bearer ")
            return hmac.compare_digest(supplied, access_token)

        def _is_same_origin_write(self) -> bool:
            """Reject browser cross-site form/fetch writes without breaking local scripts.

            Non-browser callers commonly omit Origin, while browsers attach it
            to cross-site writes.  An absent Origin therefore remains allowed
            for the local API and CLI, but an asserted Origin must use HTTP(S)
            and match the host serving Cater.  HTTPS is accepted here because
            a TLS reverse proxy terminates encryption before forwarding to this
            deliberately minimal local HTTP server.
            """
            origin = self.headers.get("Origin")
            if not origin:
                return True
            parsed = urlparse(origin)
            return parsed.scheme in {"http", "https"} and parsed.netloc == self.headers.get("Host", "")

        def end_headers(self) -> None:
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
                "form-action 'self'; connect-src 'self'; img-src 'self' data:; "
                "object-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'",
            )
            super().end_headers()

        def _read_form(self) -> dict[str, list[str]]:
            raw = self._read_body()
            if raw is None:
                return {}
            return parse_qs(raw)

        def _read_json(self) -> dict[str, Any] | None:
            raw = self._read_body()
            if raw is None:
                return None
            try:
                data = json.loads(raw or "{}")
            except json.JSONDecodeError:
                self._bad_request("invalid JSON body")
                return None
            if not isinstance(data, dict):
                self._bad_request("JSON body must be an object")
                return None
            return data

        def _read_body(self) -> str | None:
            raw_length = self.headers.get("Content-Length", "0") or "0"
            try:
                length = int(raw_length)
            except ValueError:
                self._bad_request("invalid Content-Length")
                return None
            if length < 0 or length > MAX_REQUEST_BYTES:
                self._discard_near_limit_rejected_body(length)
                self.close_connection = True
                self._bad_request(f"request body exceeds {MAX_REQUEST_BYTES} byte limit")
                return None
            try:
                return self.rfile.read(length).decode("utf-8")
            except UnicodeDecodeError:
                self._bad_request("request body must be UTF-8")
                return None

        def _discard_near_limit_rejected_body(self, length: int) -> None:
            """Let ordinary clients finish a just-over-limit upload before replying.

            Closing a Windows socket while its client is still sending can turn a
            deliberate 400 into a connection-reset error.  Only drain a tiny
            overage, under a short timeout; a clearly large or slow upload is
            still rejected without consuming unbounded request data.
            """
            if length > MAX_REQUEST_BYTES + MAX_REJECT_DRAIN_BYTES:
                return
            previous_timeout = self.connection.gettimeout()
            try:
                self.connection.settimeout(REJECT_DRAIN_TIMEOUT_SECONDS)
                self.rfile.read(length)
            except OSError:
                pass
            finally:
                self.connection.settimeout(previous_timeout)

        def _redirect(self, location: str) -> None:
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", location)
            self.end_headers()

    return CaterHandler


def render_index(rt: Runtime) -> str:
    with rt:
        tasks = rt.tasks()
        inbox = {item["task_id"] for item in rt.inbox()}
        profiles = rt.profiles()
        chain = rt.verify_chain()
        roles = list(rt.roles)
        cabinet_policy = rt.cabinet_policy
        governance_policy = rt.governance_policy
        review = build_review(
            tasks=tasks,
            events=rt.log.read(),
            profiles=profiles,
            chain=chain,
            scope="workspace",
        )
    rows = "\n".join(_task_row(task, task.id in inbox) for task in tasks)
    if not rows:
        rows = '<tr><td colspan="5" class="empty">No tasks yet. Run <code>cater demo</code>.</td></tr>'
    role_options = _role_options(roles, include_blank=True)
    return page(
        "Cater",
        f"""
        <section class="topbar">
          <div>
            <p class="eyebrow">Cater Replay</p>
            <h1>Autonomous agent workbench</h1>
          </div>
          <span class="chain">{escape(chain.describe())}</span>
        </section>
        <section class="grid">
          <article class="panel">
            <div class="panel-head">
              <h2>New Task</h2>
              <span>create</span>
            </div>
            <form method="post" action="/tasks">
              <label>Goal
                <textarea name="goal" rows="3" required></textarea>
              </label>
              <label>Acceptance
                <textarea name="acceptance" rows="4"></textarea>
              </label>
              <label>Direct worker
                <select name="assign_owner">{role_options}</select>
              </label>
              <label class="checkline">
                <input type="checkbox" name="dispatch">
                <span>Dispatch mission first</span>
              </label>
              <label class="checkline">
                <input type="checkbox" name="run_now" checked>
                <span>Run collaboration round now</span>
              </label>
              <button type="submit">Create task</button>
            </form>
          </article>
          <article class="panel">
            <div class="panel-head">
              <h2>Human Input</h2>
              <span>{len(inbox)} pending</span>
            </div>
            {_inbox_list(tasks, inbox)}
          </article>
        </section>
        <section class="panel">
          <div class="panel-head">
            <h2>Agent Profiles</h2>
            <span>{len(profiles)} active</span>
          </div>
          {_profiles_table(profiles)}
        </section>
        <section class="panel">
          <div class="panel-head">
            <h2>Organization Policy</h2>
            <span>{len(roles)} roles</span>
          </div>
          {_policy_view(roles, cabinet_policy, governance_policy, rt.driver_attempts, rt.circuit_breaker_threshold)}
        </section>
        {_api_panel()}
        {_review_panel(review)}
        <section class="panel">
          <div class="panel-head">
            <h2>Tasks</h2>
            <span>{len(tasks)} total</span>
          </div>
          <table>
            <thead>
              <tr><th>Task</th><th>Status</th><th>Cabinet</th><th>Messages</th><th>Decision</th></tr>
            </thead>
            <tbody>{rows}</tbody>
          </table>
        </section>
        """,
    )


def _api_panel() -> str:
    return """
    <section class="panel api-panel">
      <div class="panel-head">
        <h2>Local API</h2>
        <span>machine access</span>
      </div>
      <div class="api-grid">
        <code>GET /api/tasks</code>
        <code>POST /api/tasks</code>
        <code>GET /api/chain</code>
        <code>POST /api/tasks/&lt;id&gt;/run</code>
      </div>
      <p class="muted"><code>python examples/api_client.py</code></p>
    </section>
    """


def render_task(
    task: TaskSnapshot,
    events: list[Event],
    review: dict[str, Any] | None = None,
    *,
    roles: list[Any] | None = None,
    timeline_category: str | None = None,
) -> str:
    message_cards = "\n".join(_message_card(item) for item in task.messages)
    timeline_events = _filtered_timeline_events(events, timeline_category)
    timeline = "\n".join(_event_row(event) for event in timeline_events)
    if not timeline:
        timeline = '<li class="empty-event">No events match this filter.</li>'
    filters = _timeline_filters(task.id, events, timeline_category)
    ladder = "\n".join(_ladder_step(step) for step in decision_ladder(events))
    path = "\n".join(_path_step(step) for step in organization_path(task, events))
    governance = _governance_panel(task)
    decision = _decision_panel(task)
    distribution = _distribution_panel(task, roles or [])
    command = _command_panel(task)
    failures = _failure_panel(task)
    review = review or build_review(
        tasks=[task],
        events=events,
        profiles={},
        chain=_local_chain_placeholder(),
        scope=task.id,
    )
    return page(
        f"Cater / {task.id}",
        f"""
        <section class="topbar">
          <div>
            <p class="eyebrow"><a href="/">Cater Replay</a> / {escape(task.id)}</p>
            <h1>{escape(task.goal)}</h1>
          </div>
          <span class="status">{escape(task.status)}</span>
        </section>
        <section class="grid">
          {governance}
          {decision}
        </section>
        <section class="panel">
          <div class="panel-head"><h2>Organization Path</h2><span>task route</span></div>
          <ol class="path-list">{path}</ol>
        </section>
        {_acceptance_panel(task)}
        {distribution}
        <section class="panel">
          <div class="panel-head"><h2>Decision Ladder</h2><span>autonomy path</span></div>
          <ol class="ladder">{ladder}</ol>
        </section>
        {command}
        {_review_panel(review)}
        {failures}
        <section class="panel">
          <div class="panel-head"><h2>Messages</h2><span>{len(task.messages)} entries</span></div>
          <div class="messages">{message_cards}</div>
        </section>
        <section class="panel">
          <div class="panel-head"><h2>Timeline</h2><span>{len(timeline_events)} / {len(events)} events</span></div>
          {filters}
          <ol class="timeline">{timeline}</ol>
        </section>
        """,
    )


def _acceptance_panel(task: TaskSnapshot) -> str:
    if task.acceptance:
        items = "\n".join(f"<li>{escape(item)}</li>" for item in task.acceptance)
    else:
        items = '<li class="muted">No explicit acceptance criteria yet</li>'
    return f"""
    <section class="panel">
      <div class="panel-head"><h2>Acceptance</h2><span>{len(task.acceptance)} criteria</span></div>
      <ul class="acceptance-list">{items}</ul>
    </section>
    """


def page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <style>{CSS}</style>
</head>
<body>
  <main>{body}</main>
</body>
</html>
"""


def task_to_dict(task: TaskSnapshot, *, include_content: bool = True) -> dict[str, Any]:
    return {
        "id": task.id,
        "project_id": task.project_id,
        "goal": task.goal,
        "acceptance": task.acceptance,
        "status": task.status,
        "messages": task.messages,
        "driver_failures": task.driver_failures,
        "budget_exhaustions": task.budget_exhaustions,
        "discipline_flags": task.discipline_flags,
        "cabinet": task.cabinet,
        "proposal": task.proposal,
        "brief": task.brief,
        "decision": task.decision,
        "commands": task.commands,
        "reviews": task.reviews,
        "interpretation": task.interpretation,
        "route": task.route,
        "mission": task.mission,
        "plan": task.plan,
        "assignments": task.assignments,
        "deliverables": task.deliverables if include_content else [
            {key: value for key, value in item.items() if key != "content"}
            for item in task.deliverables
        ],
        "diagnoses": task.diagnoses,
        "handoffs": task.handoffs,
        "verifications": task.verifications,
        "work": task.work,
        "tokens": task.tokens,
        "token_usage": task.token_usage,
    }


def _role_to_dict(role: Any) -> dict[str, str]:
    return {
        "id": str(role.id),
        "name": str(role.name),
        "role": str(role.role),
        "charter": str(getattr(role, "charter", "")),
    }


@contextmanager
def _read_snapshot(rt: Runtime):
    owned = not rt.log.db.in_transaction
    if owned:
        rt.log.db.execute("BEGIN")
    try:
        yield
    finally:
        if owned:
            rt.log.db.rollback()


def _event_version(rt: Runtime) -> int:
    return rt.log.db.execute("SELECT COALESCE(MAX(seq), 0) FROM events").fetchone()[0]


# Count conversational records, not inline membership notices. Keep receipt
# suppression in sync with App.conversationFor when adding message event types.
_CHAT_TYPES = {
    "clarification.requested",
    "question.asked", "question.finished",
    "conversation.received", "conversation.finished",
    "collaboration.requested", "collaboration.finished",
    "verification.planning_started", "verification.planned",
    "task.created", "rule.recorded", "rule.updated", "activity.failed", "human.commanded",
    "task.progress_reported", "planning.started", "planning.finished", "delivery.review_started",
    "delivery.review_finished", "task.interpreted", "cabinet.convened",
    "task.decomposed", "plan.amended", "msg.said", "msg.challenge", "diagnosis.recorded",
    "deliverable.published", "handoff.published", "verification.completed", "cabinet.voted",
    "agent.decided", "supreme.decided", "agent.discipline_flagged", "driver.budget_exhausted", "driver.failed",
}


def _conversation_count(counts: Counter) -> int:
    count = sum(n for (kind, phase), n in counts.items() if kind in _CHAT_TYPES
                and not (kind == "msg.said" and phase in {"planning", "delivery_receipt", "clarification_receipt", "collaboration_request"})
                and not (kind == "human.commanded" and phase == "conversation_receipt")
                and not (kind == "conversation.finished" and phase == "status_receipt"))
    if not (counts[("activity.completed", None)] or counts[("activity.failed", None)]):
        count += counts[("activity.queued", None)]
    if not (counts[("agent.decided", None)] or counts[("supreme.decided", None)]):
        count += counts[("brief.generated", None)]
    return count


def _task_status_label(task: TaskSnapshot) -> str:
    work = task.work or {}
    if work.get("items"):
        statuses = {item["status"] for item in work["items"]}
        delivery_status = (work.get("delivery") or {}).get("status")
        if "uncertain" in statuses:
            return "结果待核实"
        if not work.get("scope_current"):
            return "需要重新核对"
        if delivery_status == "accepted":
            return "已交付"
        if delivery_status == "uncertain":
            return "复核结果待确认"
        if work.get("stage_accepted"):
            return "阶段通过，待整体验收"
        return "等待验收" if "submitted" in statuses else "待补充成果"
    if task.status == "running" and task.brief:
        return "等待下一轮"
    return {"pending": "待开始", "running": "进行中", "rework": "需返工", "decided": "已定下",
            "done": "已完成", "escalated": "待处理"}.get(task.status, task.status)


def _task_summary(task: TaskSnapshot, *, version: int, latest_seq: int, conversation_count: int,
                  execution_control: dict[str, Any] | None = None) -> dict[str, Any]:
    control = execution_control or {"state": "running", "seq": None, "reason": ""}
    return {"id": task.id, "goal": task.goal, "project_id": task.project_id, "status": task.status,
            "execution_control": control,
            "status_label": {"paused": "已暂停", "cancelled": "已取消"}.get(control["state"], _task_status_label(task)),
            "conversation_count": conversation_count,
            "latest_seq": latest_seq, "version": version, "evaluated_at": time.time()}


def _summary_activity(row: dict[str, Any], activity: dict[str, Any]) -> dict[str, Any]:
    state = {key: activity[key] for key in ("state", "active", "revision") if key in activity}
    if activity.get("follow_up"):
        state["follow_up"] = {"action": activity["follow_up"]["action"]}
    label = row["status_label"]
    if activity.get("active") and row.get("execution_control", {}).get("state", "running") == "running":
        label = ("已收下新方向，当前轮结束后继续" if state.get("follow_up", {}).get("action") == "run"
                 else "协作已排队" if state["state"] == "queued" else "正在协作")
    return {**row, "activity": state, "status_label": label}


def task_api_payload(
    rt: Runtime,
    task_id: str,
    *,
    activity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    with _read_snapshot(rt):
        return _task_api_payload(rt, task_id, activity=activity)


def _task_api_payload(rt: Runtime, task_id: str, *, activity: dict[str, Any] | None) -> dict[str, Any]:
    version = _event_version(rt)
    task = rt.task(task_id)
    if task is None:
        raise KeyError(task_id)
    events = rt.log.read(task_id=task_id)
    payload = {
        "summary": _summary_activity(
            _task_summary(task, version=version, latest_seq=events[-1].seq if events else 0,
                          execution_control=rt.execution_control.view(task_id),
                          conversation_count=_conversation_count(Counter(
                              (e.type, e.payload.get("phase") if e.type == "msg.said"
                               and e.payload.get("phase") in ("planning", "delivery_receipt", "clarification_receipt", "collaboration_request")
                               else "conversation_receipt" if e.type == "human.commanded" and e.payload.get("conversation_seq")
                               else "status_receipt" if e.type == "conversation.finished" and e.payload.get("action") == "status_task"
                               and e.payload.get("progress_seq") else None)
                              for e in events))), activity or {"state": "idle", "active": False}),
        "task": task_to_dict(task),
        "execution_control": rt.execution_control.view(task_id),
        "events": [event_to_dict(e) for e in events],
        "organization_memory": rt.shared_memory(exclude_task_id=task_id),
        "background": rt.conversation_background(task_id),
        "questions": rt.questions.view(task_id),
        "conversation": rt.conversation.view(task_id),
        "clarifications": rt.clarifications.view(task_id),
        "decision_ladder": decision_ladder(events),
        "organization_path": organization_path(task, events),
        "review": rt.review(task_id),
    }
    if activity is not None:
        payload["activity"] = activity
    return payload


def chain_to_dict(report: Any) -> dict[str, Any]:
    return {
        "ok": report.ok,
        "checked": report.checked,
        "signed": report.signed,
        "head_hash": report.head_hash,
        "broken_at": report.broken_at,
        "reason": report.reason,
        "summary": report.describe(),
    }


def _acceptance_lines(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [line.strip() for line in str(value).splitlines() if line.strip()]


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _profiles_table(profiles: dict[str, dict[str, Any]]) -> str:
    if not profiles:
        return '<p class="muted">No agent activity yet</p>'
    rows = "\n".join(
        f"""
        <tr>
          <td>{escape(profile['id'])}<small>{escape(profile['role'])}</small></td>
          <td>{escape(profile['messages'])}</td>
          <td>{escape(profile['supports'])}</td>
          <td>{escape(profile['challenges'])}</td>
          <td>{escape(profile['driver_failures'])}</td>
          <td>{escape(profile['last_claim'])}</td>
        </tr>
        """
        for profile in profiles.values()
    )
    return f"""
    <table>
      <thead>
        <tr><th>Agent</th><th>Messages</th><th>Support</th><th>Challenge</th><th>Failures</th><th>Last Claim</th></tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>
    """


def _role_options(roles: list[Any], *, include_blank: bool = False) -> str:
    options = ['<option value="">none</option>'] if include_blank else []
    options.extend(
        f'<option value="{escape(role.id)}">{escape(role.id)} / {escape(role.role)}</option>'
        for role in roles
    )
    return "\n".join(options)


def _policy_view(
    roles: list[Any],
    cabinet_policy: Any,
    governance_policy: Any,
    driver_attempts: int,
    circuit_breaker_threshold: int,
) -> str:
    role_rows = "\n".join(
        f"<li><strong>{escape(role.id)}</strong><span>{escape(role.role)} / {escape(role.driver)}</span></li>"
        for role in roles
    )
    return f"""
    <div class="policy-grid">
      <div>
        <h3>Governance</h3>
        <dl class="policy-list">
          <div><dt>challenge</dt><dd>{escape(governance_policy.trigger_on_challenge)}</dd></div>
          <div><dt>option split</dt><dd>{escape(governance_policy.trigger_on_option_split)}</dd></div>
          <div><dt>low confidence</dt><dd>{escape(governance_policy.low_confidence_threshold)}</dd></div>
        </dl>
      </div>
      <div>
        <h3>Cabinet</h3>
        <dl class="policy-list">
          <div><dt>threshold</dt><dd>{escape(cabinet_policy.approval_threshold or "majority")}</dd></div>
          <div><dt>quorum</dt><dd>{escape(cabinet_policy.quorum or "-")}</dd></div>
          <div><dt>veto roles</dt><dd>{escape(", ".join(cabinet_policy.veto_roles) or "-")}</dd></div>
        </dl>
      </div>
      <div>
        <h3>Reliability</h3>
        <dl class="policy-list">
          <div><dt>driver attempts</dt><dd>{escape(driver_attempts)}</dd></div>
          <div><dt>circuit breaker</dt><dd>{escape(circuit_breaker_threshold)}</dd></div>
        </dl>
      </div>
      <div>
        <h3>Roles</h3>
        <ul class="policy-roles">{role_rows}</ul>
      </div>
    </div>
    """


def _review_panel(review: dict[str, Any]) -> str:
    lenses = "\n".join(
        f"""
        <li>
          <strong>{escape(item.get("label"))}</strong>
          <span>{escape(item.get("status"))}</span>
          <p>{escape(item.get("finding"))}</p>
          <small>{escape(item.get("action"))}</small>
        </li>
        """
        for item in review.get("lenses", [])
    )
    steps = review.get("next_steps") or []
    actions = review.get("next_actions", [])
    if steps:
        action_rows = "\n".join(
            f"<li><strong>{escape(item.get('label', item.get('source', 'Review')))}</strong>"
            f"<span>{escape(item.get('priority', 'medium'))}</span>"
            f"{escape(item.get('action'))}</li>"
            for item in steps[:3]
        )
        next_block = f'<ul class="next-actions">{action_rows}</ul>'
    elif actions:
        action_rows = "\n".join(f"<li>{escape(item)}</li>" for item in actions[:3])
        next_block = f'<ul class="next-actions">{action_rows}</ul>'
    else:
        next_block = '<p class="muted">No immediate action</p>'
    return f"""
    <section class="panel">
      <div class="panel-head"><h2>Self Review</h2><span>{escape(review.get("health"))}</span></div>
      <p class="muted">{escape(review.get("summary"))}</p>
      {next_block}
      <ol class="review-list">{lenses}</ol>
    </section>
    """


def _local_chain_placeholder() -> Any:
    class LocalChain:
        ok = True
        checked = 0
        signed = 0
        head_hash = ""
        reason = ""
        broken_at = None

        def describe(self) -> str:
            return "task-local review"

    return LocalChain()


def event_to_dict(event: Event) -> dict[str, Any]:
    return {
        "seq": event.seq,
        "id": event.id,
        "ts": event.ts,
        "task_id": event.task_id,
        "actor": event.actor.to_dict(),
        "type": event.type,
        "category": event.category,
        "payload": event.payload,
        "prev_hash": event.prev_hash,
        "hash": event.hash,
        "key_id": event.key_id,
        "signature": event.signature,
    }


def _task_row(task: TaskSnapshot, needs_decision: bool) -> str:
    cabinet = task.cabinet or {}
    governance = (task.brief or {}).get("governance", {}) if isinstance(task.brief, dict) else {}
    governance_status = cabinet.get("status") or governance.get("status", "-")
    decision = "input needed" if needs_decision else (task.decision or {}).get("resolution", "-")
    return f"""
    <tr>
      <td><a href="/tasks/{escape(task.id)}">{escape(task.goal)}</a><small>{escape(task.id)}</small></td>
      <td><span class="pill">{escape(task.status)}</span></td>
      <td>{escape(governance_status)}</td>
      <td>{len(task.messages)}</td>
      <td>{escape(decision)}</td>
    </tr>
    """


def _message_card(item: dict[str, Any]) -> str:
    evidence = ", ".join(str(value) for value in item.get("evidence", []))
    link_text = message_relation(item)
    link_row = f"<small>{escape(link_text)}</small>" if link_text else ""
    return f"""
    <article class="message">
      <header><strong>{escape(item.get("actor", "agent"))}</strong><span>{escape(item.get("stance", ""))}</span></header>
      <p>{escape(item.get("claim", ""))}</p>
      {link_row}
      <small>{escape(evidence)}</small>
    </article>
    """


def _governance_panel(task: TaskSnapshot) -> str:
    brief = task.brief or {}
    governance = brief.get("governance", {}) if isinstance(brief, dict) else {}
    if task.cabinet:
        cabinet = task.cabinet
        options = "\n".join(_option_card(item) for item in cabinet.get("options", []))
        trigger = governance.get("reason", "")
        return f"""
        <article class="panel">
          <div class="panel-head"><h2>Cabinet</h2><span>{escape(cabinet.get("status", "pending"))}</span></div>
          <dl class="metrics">
            <div><dt>threshold</dt><dd>{escape(cabinet.get("approval_threshold", "-"))}</dd></div>
            <div><dt>recommendation</dt><dd>{escape(cabinet.get("recommendation", "none"))}</dd></div>
            <div><dt>votes</dt><dd>{escape(len(cabinet.get("votes", [])))}</dd></div>
          </dl>
          <p class="muted">{escape(trigger)}</p>
          <p class="muted">{escape(cabinet.get("reason", ""))}</p>
          <div class="options">{options}</div>
        </article>
        """

    options = "\n".join(_option_card(item) for item in brief.get("options", [])) if isinstance(brief, dict) else ""
    status = governance.get("status") or "pending"
    reason = governance.get("reason") or "No governance record yet."
    return f"""
    <article class="panel">
      <div class="panel-head"><h2>Governance</h2><span>{escape(status)}</span></div>
      <p class="muted">{escape(reason)}</p>
      <div class="options">{options}</div>
    </article>
    """


def _option_card(item: dict[str, Any]) -> str:
    supporters = ", ".join(item.get("supporters", [])) or "none"
    concerns = ", ".join(item.get("concerns", [])) or "none"
    return f"""
    <article class="option">
      <strong>{escape(item.get("id", ""))}: {escape(item.get("summary", ""))}</strong>
      <span>support {escape(item.get("support", 0))} / concern {escape(item.get("concern", 0))}</span>
      <small>supporters: {escape(supporters)} | concerns: {escape(concerns)}</small>
    </article>
    """


def _event_row(event: Event) -> str:
    text = (
        event.payload.get("claim")
        or event.payload.get("reason")
        or event.payload.get("objective")
        or event.payload.get("goal")
        or event.payload.get("strategy")
        or ""
    )
    relation = message_relation(event.payload)
    relation_row = f"<small>{escape(relation)}</small>" if relation else ""
    payload_json = json.dumps(event.payload, ensure_ascii=False, indent=2, sort_keys=True)
    return f"""
    <li>
      <span>#{event.seq}</span>
      <strong>{escape(event.type)}</strong>
      <em>{escape(event.actor.id)} / {escape(event.category)}</em>
      <div>
        <p>{escape(text)}{relation_row}</p>
        <details class="payload-json">
          <summary>payload</summary>
          <pre>{escape(payload_json)}</pre>
        </details>
      </div>
    </li>
    """


def _filtered_timeline_events(events: list[Event], category: str | None) -> list[Event]:
    if not category:
        return events
    return [event for event in events if event.category == category]


def _timeline_filters(task_id: str, events: list[Event], current: str | None) -> str:
    counts: dict[str, int] = {}
    for event in events:
        counts[event.category] = counts.get(event.category, 0) + 1
    links = [
        _timeline_filter_link(task_id, "all", len(events), None, current is None),
    ]
    for category in sorted(counts):
        links.append(
            _timeline_filter_link(
                task_id,
                category,
                counts[category],
                category,
                current == category,
            )
        )
    return '<nav class="filter-row" aria-label="Timeline filters">' + "\n".join(links) + "</nav>"


def _timeline_filter_link(
    task_id: str,
    label: str,
    count: int,
    category: str | None,
    active: bool,
) -> str:
    href = f"/tasks/{escape(task_id)}"
    if category:
        href += f"?category={escape(category)}"
    active_class = " active" if active else ""
    return f'<a class="filter-pill{active_class}" href="{href}">{escape(label)} <span>{count}</span></a>'


def _ladder_step(step: dict[str, Any]) -> str:
    return f"""
    <li class="ladder-step {escape(step.get("status"))}">
      <strong>{escape(step.get("label"))}</strong>
      <span>{escape(step.get("status"))}</span>
      <p>{escape(step.get("detail"))}</p>
    </li>
    """


def _path_step(step: dict[str, Any]) -> str:
    return f"""
    <li>
      <strong>{escape(step.get("label"))}</strong>
      <span>{escape(step.get("status"))}</span>
      <p>{escape(step.get("detail"))}</p>
    </li>
    """


def _distribution_panel(task: TaskSnapshot, roles: list[Any]) -> str:
    if roles:
        owner_control = f"""
        <select name="owner">
          {_role_options(roles, include_blank=False)}
        </select>
        """
    else:
        owner_control = '<input name="owner" placeholder="product, design, dev, test">'
    direct_form = f"""
    <form method="post" action="/tasks/{escape(task.id)}/assign" class="compact-form">
      <label>Direct worker role
        {owner_control}
      </label>
      <label>Objective
        <textarea name="objective" rows="2"></textarea>
      </label>
      <label>Acceptance
        <textarea name="acceptance" rows="2"></textarea>
      </label>
      <button class="secondary" type="submit">Assign directly</button>
    </form>
    """
    if not task.assignments:
        return f"""
        <section class="panel">
          <div class="panel-head"><h2>Task Distribution</h2><span>not dispatched</span></div>
          <p class="muted">Use cabinet dispatch for complex work, or directly assign simple work to one worker role.</p>
          <form method="post" action="/tasks/{escape(task.id)}/dispatch">
            <button type="submit">Dispatch mission</button>
          </form>
          {direct_form}
        </section>
        """
    visible_assignments = latest_assignments_by_owner(task.assignments)
    rows = "\n".join(
        f"""
        <li>
          <strong>{escape(item.get("owner"))}</strong>
          <span>{escape(item.get("lane"))}</span>
          <p>{escape(item.get("objective"))}</p>
          <small>{escape(item.get("deliverable") or item.get("acceptance"))}</small>
          <small>{escape(_depends_on_text(item))}</small>
        </li>
        """
        for item in visible_assignments
    )
    plan_reason = ""
    if task.plan:
        plan_reason = f'<p class="muted">{escape(task.plan.get("reason", ""))}</p>'
    return f"""
    <section class="panel">
      <div class="panel-head"><h2>Task Distribution</h2><span>{len(visible_assignments)} active / {len(task.assignments)} total</span></div>
      {plan_reason}
      <ol class="assignment-list">{rows}</ol>
      {direct_form}
    </section>
    """


def _depends_on_text(item: dict[str, Any]) -> str:
    deps = item.get("depends_on") or []
    if not deps:
        return "depends on: none"
    return "depends on: " + ", ".join(str(dep) for dep in deps)


def _decision_panel(task: TaskSnapshot) -> str:
    if task.decision:
        return f"""
        <article class="panel">
          <div class="panel-head"><h2>Agent Decision</h2><span>{escape(task.decision.get("resolution"))}</span></div>
          <p>{escape(task.decision.get("reason", ""))}</p>
        </article>
        """
    proposal = ""
    if task.proposal:
        proposal = f"""
        <div class="proposal">
          <strong>Proposed option {escape(task.proposal.get("option_id"))}</strong>
          <p>{escape(task.proposal.get("reason", ""))}</p>
        </div>
        """
    if task.status != "escalated":
        return f"""
        <article class="panel">
          <div class="panel-head"><h2>Agent Decision</h2><span>{escape(task.status)}</span></div>
          <p class="muted">Agents are expected to continue autonomously unless you send a command.</p>
          <div class="button-row">
            <form method="post" action="/tasks/{escape(task.id)}/dispatch">
              <button class="secondary" type="submit">Dispatch mission</button>
            </form>
            <form method="post" action="/tasks/{escape(task.id)}/run">
              <button type="submit">Run next round</button>
            </form>
            <form method="post" action="/tasks/{escape(task.id)}/battle">
              <button class="secondary" type="submit">Force governance</button>
            </form>
          </div>
        </article>
        """
    return f"""
    <article class="panel">
      <div class="panel-head"><h2>Human Input</h2><span>exception</span></div>
      {proposal}
      <form method="post" action="/tasks/{escape(task.id)}/decide">
        <label>Resolution
          <select name="resolution">
            <option value="done">done</option>
            <option value="rework">rework</option>
          </select>
        </label>
        <label>Reason
          <textarea name="reason" rows="4" placeholder="Why is this the right call?"></textarea>
        </label>
        <button type="submit">Record override</button>
      </form>
    </article>
    """


def _command_panel(task: TaskSnapshot) -> str:
    history = ""
    if task.commands:
        rows = "\n".join(
            f"<li><strong>#{escape(item.get('seq'))}</strong> {escape(item.get('instruction'))}</li>"
            for item in task.commands[-5:]
        )
        history = f'<ul class="inbox-list">{rows}</ul>'
    return f"""
    <section class="panel">
      <div class="panel-head"><h2>Human Command</h2><span>optional</span></div>
      <form method="post" action="/tasks/{escape(task.id)}/command">
        <label>Instruction
          <textarea name="instruction" rows="3" placeholder="Change direction, add a constraint, or override the goal."></textarea>
        </label>
        <label>New goal
          <textarea name="goal" rows="2"></textarea>
        </label>
        <label>Acceptance
          <textarea name="acceptance" rows="3"></textarea>
        </label>
        <button type="submit">Send command</button>
      </form>
      {history}
    </section>
    """


def _failure_panel(task: TaskSnapshot) -> str:
    if not task.driver_failures:
        return ""
    rows = "\n".join(
        f"""
        <li>
          <strong>{escape(item.get("driver"))}</strong>
          <span>{escape(item.get("error_type"))}</span>
          <p>{escape(item.get("message"))}</p>
        </li>
        """
        for item in task.driver_failures
    )
    return f"""
    <section class="panel">
      <div class="panel-head"><h2>Driver Failures</h2><span>{len(task.driver_failures)} contained</span></div>
      <ol class="failure-list">{rows}</ol>
    </section>
    """


def _inbox_list(tasks: list[TaskSnapshot], inbox: set[str]) -> str:
    pending = [task for task in tasks if task.id in inbox]
    if not pending:
        return '<p class="muted">Inbox empty</p>'
    rows = "\n".join(
        f'<li><a href="/tasks/{escape(task.id)}">{escape(task.goal)}</a></li>' for task in pending
    )
    return f'<ul class="inbox-list">{rows}</ul>'


def escape(value: Any) -> str:
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


CSS = """
:root {
  color-scheme: light;
  --bg: #f6f7f9;
  --panel: #ffffff;
  --ink: #1e242c;
  --muted: #687282;
  --line: #d8dee8;
  --accent: #216869;
  --accent-weak: #e8f2f1;
  --warn: #8f4f00;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--ink);
  font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}
main {
  width: min(1120px, calc(100vw - 32px));
  margin: 0 auto;
  padding: 28px 0 48px;
}
a { color: var(--accent); text-decoration: none; }
h1, h2, p { margin: 0; }
h3 { margin: 0 0 10px; font-size: 14px; }
h1 { font-size: 28px; line-height: 1.15; font-weight: 720; letter-spacing: 0; }
h2 { font-size: 16px; }
.topbar {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 20px;
  margin-bottom: 20px;
}
.eyebrow {
  color: var(--muted);
  font-size: 13px;
  margin-bottom: 8px;
}
.chain, .status, .pill {
  border: 1px solid var(--line);
  background: var(--panel);
  border-radius: 999px;
  padding: 7px 10px;
  color: var(--muted);
  font-size: 13px;
  white-space: nowrap;
}
.panel {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 18px;
  margin-bottom: 16px;
}
.panel-head {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  align-items: center;
  margin-bottom: 14px;
}
.panel-head span, .muted, small {
  color: var(--muted);
  font-size: 13px;
}
.grid {
  display: grid;
  grid-template-columns: minmax(0, 1.35fr) minmax(280px, .65fr);
  gap: 16px;
}
table {
  border-collapse: collapse;
  width: 100%;
}
th, td {
  border-top: 1px solid var(--line);
  padding: 12px 8px;
  text-align: left;
  vertical-align: top;
}
th {
  color: var(--muted);
  font-size: 12px;
  text-transform: uppercase;
}
td small {
  display: block;
  margin-top: 4px;
}
.empty {
  color: var(--muted);
  text-align: center;
}
.metrics {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 10px;
  margin: 0 0 14px;
}
.metrics div {
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 10px;
}
.policy-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 14px;
}
.policy-list {
  margin: 0;
}
.policy-list div {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  border-top: 1px solid var(--line);
  padding: 8px 0;
}
.api-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 10px;
}
.api-grid code {
  border: 1px solid var(--line);
  border-radius: 8px;
  display: block;
  padding: 10px;
  white-space: normal;
}
.policy-roles {
  list-style: none;
  padding: 0;
  margin: 0;
}
.policy-roles li {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  border-top: 1px solid var(--line);
  padding: 8px 0;
}
.policy-roles span {
  color: var(--muted);
  font-size: 13px;
}
.review-list, .assignment-list, .acceptance-list, .path-list {
  list-style: none;
  padding: 0;
  margin: 14px 0 0;
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
}
.next-actions {
  margin: 12px 0 0;
  padding-left: 18px;
  color: var(--muted);
  font-size: 13px;
}
.next-actions li + li {
  margin-top: 4px;
}
.next-actions strong, .next-actions span {
  display: inline-block;
  margin-right: 8px;
}
.next-actions span {
  color: var(--accent);
}
.path-list {
  grid-template-columns: repeat(4, minmax(0, 1fr));
}
.review-list li, .assignment-list li, .acceptance-list li, .path-list li {
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 10px;
  background: #fbfcfd;
}
.review-list strong, .review-list span, .assignment-list strong, .assignment-list span, .path-list strong, .path-list span {
  display: block;
}
.review-list span, .assignment-list span, .path-list span {
  color: var(--accent);
  font-size: 13px;
  margin-top: 4px;
}
.review-list p, .assignment-list p, .path-list p {
  margin-top: 8px;
}
.assignment-list small {
  color: var(--muted);
  display: block;
  margin-top: 8px;
}
dt {
  color: var(--muted);
  font-size: 12px;
}
dd {
  margin: 4px 0 0;
  font-weight: 700;
}
.messages, .options {
  display: grid;
  gap: 10px;
}
.message, .option {
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 12px;
  background: #fbfcfd;
}
.message header {
  display: flex;
  justify-content: space-between;
  margin-bottom: 8px;
}
.message header span, .option span {
  color: var(--warn);
  font-size: 13px;
}
.message p, .option strong {
  display: block;
  margin-bottom: 8px;
}
.failure-list {
  list-style: none;
  padding: 0;
  margin: 0;
}
.inbox-list {
  list-style: none;
  padding: 0;
  margin: 0;
  display: grid;
  gap: 8px;
}
.inbox-list li {
  border-top: 1px solid var(--line);
  padding-top: 8px;
}
.failure-list li {
  border-top: 1px solid var(--line);
  padding: 10px 0;
}
.failure-list strong {
  display: inline-block;
  min-width: 120px;
}
.failure-list span {
  color: var(--warn);
  font-size: 13px;
}
.failure-list p {
  color: var(--muted);
  margin-top: 4px;
}
.proposal {
  border: 1px solid var(--line);
  background: var(--accent-weak);
  border-radius: 8px;
  padding: 10px;
  margin-bottom: 12px;
}
.proposal p {
  color: var(--muted);
  margin-top: 6px;
}
.timeline {
  list-style: none;
  padding: 0;
  margin: 0;
}
.filter-row {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 12px;
}
.filter-pill {
  border: 1px solid var(--line);
  border-radius: 999px;
  color: var(--muted);
  font-size: 13px;
  padding: 6px 9px;
}
.filter-pill.active {
  background: var(--accent-weak);
  color: var(--accent);
}
.filter-pill span {
  margin-left: 4px;
}
.ladder {
  list-style: none;
  padding: 0;
  margin: 0;
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 10px;
}
.ladder-step {
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 10px;
  background: #fbfcfd;
}
.ladder-step strong, .ladder-step span {
  display: block;
}
.ladder-step span {
  color: var(--accent);
  font-size: 13px;
  margin-top: 4px;
}
.ladder-step p {
  color: var(--muted);
  margin-top: 8px;
  font-size: 13px;
}
.timeline li {
  display: grid;
  grid-template-columns: 56px 180px 120px minmax(0, 1fr);
  gap: 12px;
  padding: 10px 0;
  border-top: 1px solid var(--line);
}
.timeline .empty-event {
  display: block;
  color: var(--muted);
}
.timeline span, .timeline em {
  color: var(--muted);
  font-style: normal;
}
.payload-json {
  margin-top: 6px;
}
.payload-json summary {
  color: var(--accent);
  cursor: pointer;
  font-size: 13px;
}
.payload-json pre {
  margin: 8px 0 0;
  max-height: 220px;
  overflow: auto;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: #f6f7f9;
  padding: 10px;
  white-space: pre-wrap;
  word-break: break-word;
}
form {
  display: grid;
  gap: 12px;
}
label {
  display: grid;
  gap: 6px;
  color: var(--muted);
  font-size: 13px;
}
label.checkline {
  display: flex;
  align-items: center;
  color: var(--ink);
}
input[type="checkbox"] {
  width: 16px;
  height: 16px;
}
select, textarea, input[name="owner"] {
  width: 100%;
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 10px;
  font: inherit;
  color: var(--ink);
  background: #fff;
}
button {
  border: 0;
  border-radius: 8px;
  padding: 10px 12px;
  background: var(--accent);
  color: #fff;
  font-weight: 700;
  cursor: pointer;
}
.button-row {
  display: flex;
  gap: 10px;
  align-items: center;
}
.button-row form {
  display: block;
}
button.secondary {
  background: #eef3f3;
  color: var(--accent);
  border: 1px solid var(--line);
}
.compact-form {
  border-top: 1px solid var(--line);
  margin-top: 14px;
  padding-top: 14px;
}
@media (max-width: 760px) {
  main { width: min(100vw - 20px, 1120px); padding-top: 18px; }
  .topbar, .grid { display: block; }
  .chain, .status { display: inline-block; margin-top: 12px; }
  table, thead, tbody, tr, th, td { display: block; }
  thead { display: none; }
  tr { border-top: 1px solid var(--line); padding: 10px 0; }
  td { border: 0; padding: 6px 0; }
  .timeline li { grid-template-columns: 48px minmax(0, 1fr); }
  .timeline strong, .timeline em, .timeline li > div { grid-column: 2; }
  .ladder { grid-template-columns: 1fr; }
  .policy-grid { grid-template-columns: 1fr; }
  .api-grid { grid-template-columns: 1fr; }
  .review-list, .assignment-list, .acceptance-list, .path-list { grid-template-columns: 1fr; }
}
"""
