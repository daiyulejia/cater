"""External Agent execution using the same host tools and team receipts."""

from __future__ import annotations

import hashlib
import json
import tempfile
import time

from .events import Actor
from .acp import failure_code
from .team_context import model_context
from .tool_protocol import json_utf8
from .usage import valid_token_count
from .work import WorkConflict


HOST = Actor.system("team")


def driver_fingerprint(driver):
    data = {"implementation": type(driver).__module__ + "." + type(driver).__qualname__}
    for key in ("command", "model", "execution_profile"):
        data[key] = getattr(driver, key, None)
    return hashlib.sha256(json_utf8(data, sort_keys=True, allow_nan=False)).hexdigest()


class TeamExternal:
    def __init__(self, team):
        self.team, self.rt = team, team.rt

    def run_message(self, task_id, message, role, started, revision, assignment, driver, instructions):
        from .external_agent import ExternalYield
        from .team import MAX_ACTOR_TURNS

        status, reason, final = "completed", "", ""
        failure, awaiting_customer = {}, False
        last_scope, attempt, run = None, None, None
        stop_kind, delivered_text = None, ""
        calls = 0
        last_check = 0.0
        terminal = "failed"
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

            def check(force=False):
                nonlocal last_check
                if stop_kind is not None:
                    raise ExternalYield(stop_kind)
                now = time.monotonic()
                # Before prompt transmission every check must observe changes
                # made during the handshake, even inside the idle polling window.
                if (not force and run is not None and run.state.prompt_attempted
                        and now - last_check < 0.2):
                    return
                self.team._current(task_id, started, role, revision)
                if getattr(driver, "closed", False):
                    raise WorkConflict("外部通道正在关闭，本轮停止接收新操作。")
                if self.team.view(task_id)["revision"] != revision:
                    raise WorkConflict("当前目标已变化，外部伙伴停止旧方案并保留已有成果。")
                live = self.rt.workspaces.view(task_id)
                if any(live.get(key) != binding.get(key)
                       for key in ("available", "workspace_id", "revision", "fingerprint", "root")):
                    raise WorkConflict("工程目录或权限已变化，未继续旧范围操作。")
                if self.rt.rulebook.current_snapshot(task_id)["fingerprint"] != rules:
                    raise WorkConflict("适用店规已更新，先理解新要求再继续。")
                if driver_fingerprint(driver) != fingerprint:
                    raise WorkConflict("外部模型接入配置已变化，未继续旧会话。")
                last_check = now

            def execute(call):
                nonlocal revision, calls, stop_kind, awaiting_customer, last_scope, delivered_text
                check(force=True)
                _, result = self.team._dispatch_tool(task_id, message, role, started, revision,
                    call, tools, workspace, binding, context, assignment,
                    external_started_seq=attempt.seq)
                calls += 1
                if result.get("status") == "ok":
                    if call.name in {"set_objective", "finish_work"}:
                        revision = self.team.view(task_id)["revision"]
                        last_scope = self.team.recovery.run_scope(task_id, started, observed=last_scope)
                    if call.name == "finish_work":
                        stop_kind = "delivered"
                        delivered_text = call.arguments["summary"]
                    elif call.name in {"wait_for_reply", "request_questions", "request_review", "record_review"}:
                        stop_kind = "waiting"
                        awaiting_customer = call.name == "request_questions"
                    elif call.name == "send_message" and assignment is not None and any(
                        event.seq == result.get("message_seq")
                        and event.actor == Actor.agent(role.id, role.role)
                        and event.payload.get("kind") == "report"
                        and event.payload.get("reply_to") == assignment.seq
                        and event.payload.get("recipient") == assignment.actor.id
                        for event in self.team._messages(task_id)):
                        stop_kind = "returned"
                if stop_kind is None and calls >= MAX_ACTOR_TURNS:
                    stop_kind = "checkpoint"
                # Revoke before acknowledging a terminal host action. A later
                # model tool request cannot mutate already handed-off work.
                if stop_kind is not None:
                    run.revoke_tools()
                return result

            check(force=True)
            attempt = self.team.external_runs.begin(task_id, role, started, basis=basis,
                binding=binding, tool_names=[tool["name"] for tool in tools],
                driver_fingerprint=fingerprint)
            prompt = (instructions
                + "本轮通过受管外部客户端执行，实际工作只使用Cater提供的工具。"
                  "客户端工作目录是临时启动目录，不是顾客工程；工程路径与权限以workspace及工具回执为准。"
                  "完成原委托时用send_message明确recipient与reply_to正式交回；仅输出结束语不算交还。"
                  "wait_for_reply、提问、复核、正式交还或finish_work成功后，本轮立即让出执行机会，"
                  "不继续修改刚交回的成果；下一份输入会包含最新上下文。"
                  "工具错误先读实际回执，不能因为通信失败就重复执行。\n"
                + json.dumps(context, ensure_ascii=False))
            # Never load a customer's project-local client hooks/settings.
            # Actual workspace operations still use the bound host executor.
            with tempfile.TemporaryDirectory(prefix="cater-agent-") as scratch:
                run = driver.agent_run(cwd=scratch, tools=tools, execute=execute,
                                       check_current=check, on_update=None)
                try:
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
                    if stop_kind not in {"waiting", "checkpoint", "delivered", "returned"}:
                        raise WorkConflict("外部运行停止，但没有对应的宿主交接记录；保留原委托，不按完成处理。")
                    terminal = "yielded"
                    final = run.state.text
                    self.rt._driver_health.record_success(role.driver)
                    if stop_kind == "waiting":
                        status = "waiting"
                    elif stop_kind == "checkpoint":
                        status, reason = "interrupted", "本轮到达工具检查点，已有成果和回执保留，接下来核对并继续剩余工作。"
                        failure["interruption_kind"] = "turn_boundary"
                    elif stop_kind == "delivered":
                        final = delivered_text
                finally:
                    # The adapter closes the process/bridge before these facts
                    # settle. Missing settlement remains discoverable on restart.
                    state = run.state
                    self.team.external_runs.finish(attempt, phase=state.phase,
                        prompt_attempted=state.prompt_attempted, cleanup_complete=state.cleanup_complete,
                        stop_reason=state.stop_reason, session_id=state.session_id, result=terminal)
                    if state.prompt_attempted:
                        self.rt.log.emit(type="team.model_called", actor=HOST, task_id=task_id, payload={
                            "role": role.id, "driver": role.driver, "run_id": started.payload["run_id"],
                            "external_started_seq": attempt.seq, "tokens": 0, "token_usage": "unavailable",
                            "context_basis": basis, "request_sizes": {"prompt_utf8_bytes": len(prompt.encode("utf-8"))}})
                    if not state.cleanup_complete:
                        raise WorkConflict("外部客户端尚未确认清理完成，保留运行记录；请先核对进程，不重放旧工作。")
        except WorkConflict as exc:
            status, reason = "interrupted", str(exc)
        except Exception as exc:
            code = failure_code(exc)
            detail = {
                "deadline_exceeded": "本地等待时限已到，不代表远端已停止",
                "process_exited": "客户端在返回结果前退出",
                "request_rejected": "客户端或服务端拒绝了请求，具体原因尚未确认",
                "protocol_error": "客户端通信格式不符合约定",
                "transport_unavailable": "客户端连接不可用",
            }.get(code, "原因待核查")
            status, reason = "failed", f"{role.name}的外部调用未完成：{detail}；已有成果与工具回执保留，未自动重发请求。"
            failure = {**failure, "error_type": type(exc).__name__,
                       "external_failure_code": code,
                       "failure_stage": run.state.phase if run is not None else "setup"}
            self.rt._driver_health.record_failure(role.driver)
        finally:
            if attempt is not None and run is None:
                self.team.external_runs.finish(attempt, phase="setup", prompt_attempted=False,
                                               cleanup_complete=True, result="failed")
        if final.strip():
            if stop_kind == "delivered" and run is not None and not run.state.cleanup_complete:
                final += "\n\n已发布成果可下载；外部客户端清理尚未确认，需核对进程，不能将本次交付理解为所有执行均已停止。"
            current = self.team.view(task_id)
            delivery = current["revision"] if stop_kind == "delivered" and current["status"] == "done" else None
            customer = message.actor.kind == "human" or delivery is not None
            self.rt.log.emit(type="msg.said", task_id=task_id, actor=Actor.agent(role.id, role.role),
                payload={"text": final, "claim": final[:180], "phase": "team", "run_id": started.payload["run_id"],
                         "reply_to": message.seq, "response_kind": "reply",
                         "audience": "customer" if customer else "team",
                         "recipient": "human" if customer else message.actor.id,
                         **({"delivery_seq": delivery} if delivery is not None else {})})
        return {"revision": revision, "status": status, "reason": reason, "final": final,
                "failure": failure, "awaiting_customer": awaiting_customer, "last_scope": last_scope}
