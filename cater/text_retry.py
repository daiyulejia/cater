"""One cancellable retry of a live, tool-free execution request, not crash replay."""

from copy import deepcopy
import hashlib
import hmac
import math
import os
import time

from .events import Actor
from .work import WorkConflict


RETRY_DELAY = 5.0
MAX_RETRY_WAIT = 120.0


class RetryEffectsUncertain(WorkConflict):
    """A local execution or source change invalidates the tool-free recovery claim."""


class TextRetryGuard:
    def __init__(self, runtime, task_id, role, driver, context):
        self.rt, self.task_id, self.role, self.driver = runtime, task_id, role, driver
        self.context = deepcopy(context)
        self.control_seq = runtime.execution_control.view(task_id)["seq"]
        self.head_seq = max((e.seq for e in runtime.log.read(task_id=task_id)), default=0)
        self.configuration = self._configuration()
        self.key_digest = self._key_digest()
        self.started = None
        phase = context.get("phase")
        kind = {"planning": "planning.started", "delivery": "delivery.review_started",
                "review": "work.review_started", "browser_check_plan": "verification.planning_started"}.get(phase)
        if kind:
            identity = context.get("review_attempt_seq") if phase == "review" else context.get("planning_attempt_seq")
            self.started = next((e for e in reversed(runtime.log.read(task_id=task_id))
                if e.type == kind and (identity is None or e.seq == identity)
                and e.payload.get("scope") == context["scope"]
                and (phase != "delivery" or e.payload.get("delivery_digest") == context.get("delivery_digest"))), None)
            if self.started is None:
                raise WorkConflict("缺少本次调用的有效认领，不自动续试。")
            if phase != "browser_check_plan" and self.started.actor.id != role.id:
                raise WorkConflict("调用者不是本次认领负责人，不自动续试。")
        elif phase is not None:
            raise WorkConflict("此类调用尚未启用自动续试。")
        self.current()

    def _configuration(self):
        return {key: getattr(self.driver, key, None) for key in (
            "endpoint", "model", "api_key_env", "max_tokens", "max_completion_tokens",
            "timeout", "version", "thinking", "temperature", "token_budget")}

    def _key_digest(self):
        key = os.environ.get(self.driver.api_key_env, "")
        return hashlib.sha256(key.encode("utf-8", "surrogatepass")).digest()

    def current(self):
        from .runtime import _TEXT_ONLY_CALLS

        rt, task_id, ctx = self.rt, self.task_id, self.context
        rt._check_execution_authorization(task_id)
        if (not rt.text_retry or rt.execution_control.view(task_id)["seq"] != self.control_seq
                or rt.work.scope(task_id) != ctx["scope"]
                or rt.drivers.get(self.role.driver) is not self.driver
                or getattr(self.driver.complete, "__func__", None) is not _TEXT_ONLY_CALLS.get(type(self.driver))
                or self._configuration() != self.configuration
                or not hmac.compare_digest(self._key_digest(), self.key_digest)):
            raise WorkConflict("执行授权、要求或模型配置已变化，已取消旧请求续试。")
        if rt.task(task_id).status != "running":
            raise WorkConflict("任务已不在执行中，已取消旧请求续试。")
        phase = ctx.get("phase")
        if phase is None:
            view, item = rt.work._item(task_id, ctx["work_item_id"])
            if (item["status"] != "running" or item["owner"] != self.role.id
                    or item.get("attempt_id") != ctx.get("attempt_id")
                    or view["plan_seq"] != ctx.get("plan_seq") or item["lease_until"] <= time.time()
                    or item.get("dependency_uncertain") or item.get("dependency_check_stale")):
                raise WorkConflict("工作认领已失效，不自动续试。")
        elif phase == "review":
            _, item = rt.work._item(task_id, ctx["work_item_id"])
            rt.work._validate_review_owner(task_id, item, self.started.seq)
        elif phase == "planning":
            from .planning import Planner
            Planner(rt)._current(task_id, self.started)
        elif phase == "delivery":
            rt.delivery._current_review(task_id, self.started)
        else:
            if any(e.payload.get("request_seq") == self.started.seq and e.payload.get("status") != "correcting"
                   for e in rt.log.iter_events(task_id=task_id, event_types=("verification.planned",))):
                raise WorkConflict("检查规划认领已结束，不自动续试。")
            if "work_item_id" in ctx:
                _, item = rt.work._item(task_id, ctx["work_item_id"])
                rt.browser_checks._current(task_id, item)
                if (item["submission"]["seq"] != ctx["submission_seq"]
                        or item["submission"]["digest"] != ctx["submission_digest"]):
                    raise WorkConflict("待查成果已变化，不自动续试。")
            else:
                delivery = rt.delivery.view(task_id)
                rt.browser_checks._current_delivery(task_id, delivery)
                if delivery["digest"] != ctx["delivery_digest"]:
                    raise WorkConflict("待查交付已变化，不自动续试。")
        effects = ("workspace.tool_started", "workspace.tool_finished", "workspace.tool_reconciled",
                   "workspace.work_reconciled", "workspace.files_captured", "workspace.response_recorded",
                   "deliverable.published", "work.submitted", "work.check_started",
                   "delivery.check_started", "verification.completed")
        if (rt.workspaces.pending(task_id=task_id)
                or any(rt.log.iter_events(task_id=task_id, event_types=effects, after_seq=self.head_seq))):
            raise RetryEffectsUncertain("存在待核对工具结果或新的成果/执行记录，不能当作无副作用请求续试。")

    def wait(self, failure, failure_seq):
        """Return a durable retry claim only while the original owner is still live."""
        suggested = getattr(failure, "retry_after_seconds", None)
        if suggested is not None and (type(suggested) not in (int, float)
                or not math.isfinite(suggested) or suggested < 0 or suggested > MAX_RETRY_WAIT):
            return None
        delay = max(RETRY_DELAY, suggested or 0)
        self.current()
        lease = (self.rt.work._item(self.task_id, self.context["work_item_id"])[1]["lease_until"]
                 if self.context.get("phase") is None else
                 self.started.payload.get("lease_until") if self.started else None)
        if lease is not None and lease - time.time() <= delay + self.driver.timeout:
            return None
        started = self.rt.log.emit(type="driver.retry_scheduled", task_id=self.task_id,
            actor=Actor.system("driver_gateway"), payload={
                "role": self.role.id, "driver": self.role.driver, "failure_seq": failure_seq,
                "retry_index": 1, "retry_limit": 1, "delay_seconds": delay, "not_before": time.time() + delay,
                "phase": self.context.get("phase") or "work", "scope": self.context["scope"],
                **({"activity_id": self.rt._active_activity_id} if getattr(self.rt, "_active_activity_id", None) else {}),
                **({"work_item_id": self.context["work_item_id"]} if "work_item_id" in self.context else {}),
                "reason": "模型请求暂时失败，将等待后自动续试一次；原请求远端是否计费未知。你仍可聊天、暂停或修改方向。"})
        deadline = time.monotonic() + delay
        try:
            while True:
                self.current()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return started
                time.sleep(min(0.2, remaining))
        except Exception:
            self.finish(started, "cancelled", "等待期间执行条件已变化，未发出续试请求；已有成果保留。")
            raise

    def finish(self, started, status, reason):
        self.rt.log.emit(type="driver.retry_finished", task_id=self.task_id, actor=Actor.system("driver_gateway"),
            payload={"request_seq": started.seq, "role": self.role.id, "driver": self.role.driver,
                     "status": status, "reason": reason})
