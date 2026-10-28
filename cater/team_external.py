"""External Agent execution using the same host tools and team receipts."""

from __future__ import annotations

import hashlib
import sqlite3
import sys
import threading
import time

from .events import Actor
from .acp import _exhaust, failure_code, rejection_details
from .team_context import model_context, model_json
from .tool_protocol import json_utf8
from .usage import valid_token_count
from .work import WorkConflict
from .external_scratch import LaunchDirectory


HOST = Actor.system("team")


def driver_fingerprint(driver):
    data = {"implementation": type(driver).__module__ + "." + type(driver).__qualname__}
    for key in ("command", "model", "execution_profile", "timeout", "max_run_seconds"):
        data[key] = getattr(driver, key, None)
    if getattr(driver, "tool_surface", "all") != "all":
        data["tool_surface"] = driver.tool_surface
    return hashlib.sha256(json_utf8(data, sort_keys=True, allow_nan=False)).hexdigest()


def _recent_tool_progress(state, timeout, calls):
    facts = dict(state.observations)
    progress, last, end = (facts.get(key) for key in ("progress_receipts", "last_progress_ms", "failure_ms"))
    return (type(timeout) in {int, float} and 0 < timeout <= 3600
            and all(type(value) is int for value in (progress, last, end))
            and 0 < progress <= calls and 0 <= last <= end and end - last <= timeout * 1000)


class TeamExternal:
    def __init__(self, team):
        self.team, self.rt = team, team.rt
        self._scheduler = None
        self._live = {}
        self.progress = None

    def begin_scheduler(self, task_id):
        if self._scheduler is not None or self._live:
            raise WorkConflict("协作调度器仍持有活运行，不能重入执行。")
        self._scheduler = (task_id, threading.get_ident())

    def end_scheduler(self):
        self._scheduler = None

    def scheduling(self, task_id):
        return self._scheduler == (task_id, threading.get_ident())

    def revoke_all(self, task_id):
        errors = []
        for attempt, run, _ in list(self._live.values()):
            if attempt.task_id == task_id:
                try:
                    run.revoke_tools()
                except BaseException as exc:
                    errors.append(exc)
        return errors

    def owns(self, task_id, row, *, observe_only=False):
        """Recognize a live handle, never a reconstructed permission or replay grant."""
        if not self.scheduling(task_id):
            return False
        entry = self._live.get(row.get('started_seq'))
        if entry is None or row.get('outcome') != 'unsettled' or row.get('run_ended') is not False:
            return False
        attempt, run, check = entry
        if (attempt.task_id != task_id or type(row.get('started_seq')) is not int
                or any(type(row.get(key)) is not type(attempt.payload[key])
                       or row[key] != attempt.payload[key] for key in ('role', 'run_id', 'message_seq'))):
            return False
        stored = next((e for e in self.rt.log.read(task_id=task_id) if e.seq == attempt.seq), None)
        if stored != attempt or run.state.phase not in {'opening', 'prompting'}:
            return False
        try:
            if observe_only:
                check(force=True, observe_only=True)
            else:
                check(force=True)
        except Exception:
            return False
        return True

    def run_message(self, task_id, message, role, started, revision, assignment, driver, instructions):
        return _exhaust(self.iter_message(task_id, message, role, started, revision,
                                          assignment, driver, instructions))

    def iter_message(self, task_id, message, role, started, revision, assignment, driver, instructions):
        from .external_agent import ExternalYield, ExternalRunLimit
        from .team_reply import TOOL as REPLY

        def diagnostics(error):
            code = ("execution_budget" if error.limit == "total" else "idle_timeout") if isinstance(
                error, ExternalRunLimit) else failure_code(error)
            return {"error_type": type(error).__name__, "external_failure_code": code,
                    **rejection_details(error)}

        status, reason, final = "completed", "", ""
        failure, awaiting_customer = {}, False
        last_scope, attempt, run = None, None, None
        stop_kind, delivered_text = None, ""
        settled_review_seq = None
        public_chars = 0
        calls = 0
        admission_blocked = False
        last_check = 0.0
        terminal = "failed"
        observer = self.progress
        budget_supervision = (message.payload.get("trigger") == "turn_boundary"
                              and self.team.recovery.budget_boundary(message, self.rt.log.read(task_id=task_id)))

        def observe(update):
            if update is not None and stop_kind is not None:
                return
            if observer is not None and attempt is not None:
                try:
                    observer(attempt, revision, update)
                except Exception:
                    # Optional observation must not change work, cleanup or delivery.
                    pass

        try:
            if self.rt.log.db.in_transaction:
                raise WorkConflict("外部运行必须先提交开始记录，不能处于外层事务中。")
            if self.rt._is_circuit_open(role.driver):
                raise WorkConflict("此通道连续调用失败，暂未继续请求；请检查接入状态。")
            budget = getattr(driver, "token_budget", None)
            if valid_token_count(budget) and self.rt._reported_driver_tokens(role.driver) >= budget:
                raise WorkConflict("当前通道已达到顾客配置的用量上限，未继续调用。")
            raw, workspace, binding, tools, basis = self.team._model_input(
                task_id, message, role, run_id=started.payload["run_id"])
            last_scope = dict(raw["work_checkpoint"]["scope"])
            context = model_context(raw)
            fingerprint = driver_fingerprint(driver)
            rules = basis["rule_fingerprint"]

            def verify_scope(*, observe_only=False, settled_review_seq=None):
                nonlocal admission_blocked
                self.team._current(task_id, started, role, revision,
                    **({"settled_review_seq": settled_review_seq} if settled_review_seq is not None else {}))
                if budget_supervision and any(row.get("started_seq") != (attempt.seq if attempt is not None else None)
                                             for row in self.team._pending_operations(task_id)):
                    if not observe_only:
                        admission_blocked = attempt is None
                    raise WorkConflict("监督接续前出现了未核对操作，先核对实际影响，不启动或继续旧范围工作。")
                if getattr(driver, "closed", False):
                    raise WorkConflict("外部通道正在关闭，本轮停止接收新操作。")
                if self.team._revision(task_id) != revision:
                    raise WorkConflict("当前目标已变化，外部伙伴停止旧方案并保留已有成果。")
                live = self.rt.workspaces.view(task_id)
                if any(live.get(key) != binding.get(key)
                       for key in ("available", "workspace_id", "revision", "fingerprint", "root")):
                    raise WorkConflict("工程目录或权限已变化，未继续旧范围操作。")
                if self.rt.rulebook.current_snapshot(task_id)["fingerprint"] != rules:
                    raise WorkConflict("适用店规已更新，先理解新要求再继续。")
                if driver_fingerprint(driver) != fingerprint:
                    raise WorkConflict("外部模型接入配置已变化，未继续旧会话。")

            def check(force=False, *, observe_only=False):
                nonlocal last_check
                if stop_kind is not None:
                    raise ExternalYield(stop_kind)
                if observe_only:
                    # A display can run in an older read snapshot. Check fully,
                    # but never let it postpone a subsequent live scope check.
                    verify_scope(observe_only=True)
                    return
                now = time.monotonic()
                # Before prompt transmission every check must observe changes
                # made during the handshake, even inside the idle polling window.
                if (not force and run is not None and run.state.prompt_attempted
                        and now - last_check < 0.2):
                    return
                verify_scope()
                last_check = now

            def execute(call):
                nonlocal revision, calls, stop_kind, awaiting_customer, last_scope, delivered_text, public_chars
                nonlocal settled_review_seq
                check(force=True)
                if call.name == REPLY:
                    public = run.state.text
                    if public[public_chars:].strip():
                        self.rt.log.emit(type="msg.said", actor=Actor.agent(role.id, role.role), task_id=task_id,
                            payload={"text": public[public_chars:], "phase": "team", "run_id": started.payload["run_id"],
                                     "reply_to": message.seq, "response_kind": "progress", "audience": "team"})
                    public_chars = len(public)
                _, result = self.team._dispatch_tool(task_id, message, role, started, revision,
                    call, tools, workspace, binding, context, assignment,
                    external_started_seq=attempt.seq)
                calls += 1
                if result.get("status") == "ok":
                    if call.name in {"set_objective", "finish_work"}:
                        revision = self.team.view(task_id)["revision"]
                        last_scope = self.team.recovery.run_scope(task_id, started, observed=last_scope)
                    if call.name == "finish_work":
                        if result.get("already_completed") is True:
                            stop_kind, delivered_text = "delivery_unchanged", result["note"]
                        else:
                            stop_kind = "delivered"
                            completed = next(event for event in reversed(self.rt.log.read(task_id=task_id))
                                if event.type == "team.objective_completed" and event.actor == HOST
                                and event.seq == revision and event.payload.get("run_id") == started.payload["run_id"])
                            delivered_text = completed.payload["summary"]
                    elif call.name == REPLY:
                        stop_kind = "answered"
                        delivered_text = call.arguments["text"].strip()
                    elif (call.name in {"wait_for_reply", "request_questions", "request_review", "record_review"}
                            or call.name == "delegate" and result.get("waiting_for_review") is True):
                        stop_kind = "waiting"
                        awaiting_customer = call.name == "request_questions"
                        if call.name in {"request_review", "delegate"}:
                            settled_review_seq = result.get("review_seq")
                    elif call.name in {"send_message", "return_work"} and assignment is not None and any(
                        event.seq == result.get("message_seq")
                        and event.actor == Actor.agent(role.id, role.role)
                        and event.payload.get("kind") == "report"
                        and event.payload.get("reply_to") == assignment.seq
                        and event.payload.get("recipient") == assignment.actor.id
                        for event in self.team._messages(task_id)):
                        stop_kind = "returned"
                # Revoke before acknowledging a terminal host action. A later
                # model tool request cannot mutate already handed-off work.
                if stop_kind is not None:
                    observe(None)
                    run.revoke_tools()
                return result

            check(force=True)
            attempt = self.team.external_runs.begin(task_id, role, started, basis=basis,
                binding=binding, tool_names=[tool["name"] for tool in tools],
                driver_fingerprint=fingerprint)
            from .team_instructions import EXTERNAL, context_instructions

            prompt_instructions = instructions + context_instructions(context) + EXTERNAL
            context_text = model_json(context)
            prompt = prompt_instructions + context_text
            # Never load a customer's project-local client hooks/settings.
            # Actual workspace operations still use the bound host executor.
            scratch = LaunchDirectory(self.rt.log, attempt)
            try:
                run = driver.agent_run(cwd=scratch.path, tools=tools, execute=execute,
                                       check_current=check, on_update=observe if observer is not None else None)
                try:
                    if self._scheduler == (task_id, threading.get_ident()):
                        self._live[attempt.seq] = (attempt, run, check)
                    iterative = getattr(run, 'iter_run', None)
                    if callable(iterative):
                        result = yield from iterative(prompt)
                    else:
                        result = run.run(prompt)
                    terminal = "returned"
                    check(force=True)
                    final = result.text
                    if result.stop_reason != "end_turn":
                        status, reason = "interrupted", "外部伙伴本轮未正常完成；已有操作保留，请按回执继续。"
                    elif assignment is not None and stop_kind != "returned":
                        status, reason = "interrupted", "外部伙伴本轮回复结束，但尚未正式交回原委托；需根据成果和回执继续交接。"
                    self.rt._driver_health.record_success(role.driver)
                except ExternalYield:
                    if stop_kind not in {"waiting", "delivered", "delivery_unchanged", "returned", "answered"}:
                        raise WorkConflict("外部运行停止，但没有对应的宿主交接记录；保留原委托，不按完成处理。")
                    terminal = "yielded"
                    final = run.state.text
                    self.rt._driver_health.record_success(role.driver)
                    if stop_kind == "waiting":
                        status = "waiting"
                    elif stop_kind in {"delivered", "delivery_unchanged", "answered"}:
                        final = delivered_text
                except Exception as exc:
                    if not isinstance(exc, WorkConflict):
                        # Cleanup may interrupt settlement, but must not erase the primary failure.
                        failure.update(diagnostics(exc))
                    raise
                finally:
                    self._live.pop(attempt.seq, None)
                    primary = sys.exception()
                    revoke_error = None
                    try:
                        revoke = getattr(run, 'revoke_tools', None)
                        if callable(revoke):
                            revoke()
                        elif run.state.cleanup_complete is not True:
                            raise WorkConflict('外部运行没有撤权接口，且尚未确认清理完成。')
                    except BaseException as exc:
                        revoke_error = exc
                        failure['revocation_error'] = type(exc).__name__
                    # The adapter closes the process/bridge before these facts
                    # settle. Missing settlement remains discoverable on restart.
                    state = run.state
                    cleanup_ok = state.cleanup_complete is True and revoke_error is None
                    try:
                        self.team.external_runs.finish(attempt, phase=state.phase,
                            prompt_attempted=state.prompt_attempted,
                            cleanup_complete=cleanup_ok,
                            stop_reason=state.stop_reason, session_id=state.session_id, result=terminal)
                    except BaseException as exc:
                        if primary is None:
                            raise
                        failure['external_settlement_error'] = type(exc).__name__
                    if state.prompt_attempted:
                        try:
                            self.rt.log.emit(type="team.model_called", actor=HOST, task_id=task_id, payload={
                                "role": role.id, "driver": role.driver, "run_id": started.payload["run_id"],
                                "external_started_seq": attempt.seq, "tokens": 0, "token_usage": "unavailable",
                                "external_observations": dict(state.observations),
                                "context_basis": basis, "request_sizes": {
                                    "prompt_utf8_bytes": len(prompt.encode("utf-8")),
                                    "instructions_utf8_bytes": len(prompt_instructions.encode("utf-8")),
                                    "context_json_bytes": len(context_text.encode("utf-8"))}})
                        except BaseException as exc:
                            if primary is None:
                                raise
                            failure['model_receipt_error'] = type(exc).__name__
                    if not cleanup_ok:
                        failure.update(external_observations=dict(state.observations),
                                       failure_stage=state.failure_phase or "cleanup")
                        if primary is None or isinstance(primary, Exception):
                            raise WorkConflict("外部客户端尚未确认清理完成，保留运行记录；请先核对进程，不重放旧工作。")
            finally:
                # A local directory residue is not a failed model turn. It has
                # its own durable receipt and cannot release process/tool holds.
                primary = sys.exception()
                try:
                    scratch.close(client_stopped=run is None or run.state.cleanup_complete is True)
                except (sqlite3.Error, OSError) as exc:
                    # The created receipt stays unconfirmed. Preserve any
                    # primary model/control error, or successful host handoff.
                    failure["launch_directory_settlement_error"] = type(exc).__name__
                except BaseException as exc:
                    if primary is None:
                        raise
                    failure['launch_directory_settlement_error'] = type(exc).__name__
        except WorkConflict as exc:
            status, reason = "interrupted", str(exc)
            if admission_blocked:
                failure["admission_not_started"] = True
            if attempt is None and self.rt.execution_control.view(task_id)["seq"] != started.payload["control_seq"]:
                failure["admission_not_started"] = True
        except Exception as exc:
            details = diagnostics(exc)
            code = details["external_failure_code"]
            detail = {
                "deadline_exceeded": "本地等待时限已到，不代表远端已停止",
                "execution_budget": "本轮已达到配置的总执行预算，未据此认定接入故障",
                "idle_timeout": "等待客户端输出或工具结果已超时，不代表远端已停止",
                "process_exited": "客户端在返回结果前退出",
                "request_rejected": "客户端或服务端拒绝了请求，具体原因尚未确认",
                "protocol_error": "客户端通信格式不符合约定",
                "transport_unavailable": "客户端连接不可用",
            }.get(code, "原因待核查")
            status, reason = "failed", f"{role.name}的外部调用未完成：{detail}；已有成果与工具回执保留，未自动重发请求。"
            failure = {**failure, **details,
                       "external_observations": dict(run.state.observations) if run is not None else {},
                       "failure_stage": (run.state.failure_phase or run.state.phase) if run is not None else "setup"}
            from .external_agent import QoderAcpDriver
            if (type(driver) is QoderAcpDriver and type(exc) is ExternalRunLimit and exc.limit == "total"
                    and stop_kind is None and run is not None and run.state.prompt_attempted is True
                    and run.state.cleanup_complete is True and not run.state.cancel_failed
                    and not any(key in failure for key in ("revocation_error", "external_settlement_error",
                                                          "model_receipt_error", "launch_directory_settlement_error"))
                    and _recent_tool_progress(run.state, driver.timeout, calls)):
                try:
                    check(force=True)
                    scope = (self.team.recovery.run_scope(task_id, started, observed=last_scope)
                             if assignment is not None else
                             self.team.recovery.boundary_scope(task_id, message, started, last_scope))
                    if scope is not None and not self.team._pending_operations(task_id):
                        status = "interrupted"
                        reason = (f"{role.name}本轮达到单次执行预算，已有成果与回执已保存；"
                                  "从新的监督检查点核对并继续剩余工作，不重放旧请求。")
                        failure.update(interruption_kind="turn_boundary", boundary_reason="execution_budget")
                except (WorkConflict, sqlite3.Error, OSError):
                    # A changed control/scope is not a grant to resume stale work.
                    pass
            if code != "execution_budget":
                self.rt._driver_health.record_failure(role.driver)
        finally:
            observe(None)
            if attempt is not None and run is None:
                self.team.external_runs.finish(attempt, phase="setup", prompt_attempted=False,
                                               cleanup_complete=True, result="failed")
        if final.strip() and stop_kind != "answered":
            if stop_kind == "delivered" and run is not None and not run.state.cleanup_complete:
                final += "\n\n已发布成果可下载；外部客户端清理尚未确认，需核对进程，不能将本次交付理解为所有执行均已停止。"
            elif stop_kind == "delivery_unchanged" and run is not None and not run.state.cleanup_complete:
                final += "\n\n外部客户端清理尚未确认，需核对进程；本次没有新增交付，不表示所有执行均已停止。"
            try:
                with self.rt.log.atomic():
                    # Settlement may outlast a pause or a scope change. Check and
                    # publish together; an earlier committed reply stays intact.
                    verify_scope(settled_review_seq=settled_review_seq if status == "waiting" else None)
                    current = self.team.view(task_id)
                    completed = next((event for event in reversed(self.rt.log.read(task_id=task_id))
                                      if event.type == "team.objective_completed" and event.actor == HOST
                                      and event.seq == current["revision"] and event.seq > started.seq
                                      and event.payload.get("run_id") == started.payload["run_id"]), None)
                    delivery = (completed.seq if stop_kind == "delivered" and current["status"] == "done"
                                and role.id == self.rt.organization.leader.id and completed is not None else None)
                    from .team_reply import customer_source

                    reply_source = customer_source(self.team, task_id, message, role) if assignment is None else None
                    customer = message.actor.kind == "human" or delivery is not None or reply_source is not None
                    self.rt.log.emit(type="msg.said", task_id=task_id, actor=Actor.agent(role.id, role.role),
                        payload={"text": final, "claim": final[:180], "phase": "team", "run_id": started.payload["run_id"],
                                 "reply_to": reply_source.seq if reply_source else message.seq, "response_kind": "reply",
                                 "audience": "customer" if customer else "team",
                                 "recipient": "human" if customer else message.actor.id,
                                 **({"delivery_seq": delivery} if delivery is not None else {})})
            except WorkConflict as exc:
                status, reason, final = "interrupted", str(exc), ""
        return {"revision": revision, "status": status, "reason": reason, "final": final,
                "failure": failure, "awaiting_customer": awaiting_customer, "last_scope": last_scope}
