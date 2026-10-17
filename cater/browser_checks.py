"""Optional browser checks: model-selected tests, host-owned execution and receipts."""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from pathlib import Path

from .artifacts import safe_filename
from .collaboration import text_advice_available
from .events import Actor
from .usage import valid_token_count
from .verification import validate_check
from .work import WorkConflict
from .review_contract import receipt_context


PLAN_FILE = ".cater-browser-check.json"
MAX_BROWSER_STEPS = 128
ACTION_FIELDS = {
    "click": {"selector": 500}, "press": {"key": 50},
    "click_at": {"selector": 500, "x": None, "y": None},
    "fill": {"selector": 500, "value": 2000}, "wait": {"ms": None},
    "assert_visible": {"selector": 500}, "assert_text": {"selector": 500, "text": 2000},
    "evaluate": {"expression": 4000}, "assert_js": {"expression": 4000},
}


def validate_filenames(filenames: list[str]) -> None:
    if (not filenames or len(filenames) > 128
            or any(not isinstance(f, str) or safe_filename(f) != f or f.startswith(".") or re.search(r"[\x7f%#]", f)
                   or re.match(r"(?i)^(?:COM|LPT)[¹²³](?:\.|$)", f) for f in filenames)
            or len({unicodedata.normalize("NFC", f).lower() for f in filenames}) != len(filenames)):
        raise ValueError("浏览器检查只支持名称安全且不重复的提交文件。")


def normalize_plan(raw: object, filenames: list[str]) -> dict:
    if not isinstance(raw, dict) or set(raw) != {"entrypoint", "steps"}:
        raise ValueError("浏览器检查须包含 entrypoint 和 steps。")
    entry = raw["entrypoint"]
    if not isinstance(entry, str) or entry not in filenames or not entry.lower().endswith((".html", ".htm")):
        raise ValueError("检查入口必须是本次提交的 HTML 文件。")
    validate_filenames(filenames)
    steps = raw["steps"]
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_BROWSER_STEPS:
        observed = len(steps) if isinstance(steps, list) else type(steps).__name__
        raise ValueError(f"浏览器检查须包含 1 至 {MAX_BROWSER_STEPS} 个具体步骤，实际收到：{observed}。")
    for index, step in enumerate(steps, 1):
        if not isinstance(step, dict) or not isinstance(step.get("action"), str) or step["action"] not in ACTION_FIELDS:
            raise ValueError("未知浏览器动作。")
        fields = ACTION_FIELDS[step["action"]]
        expected = {"action", "label", *fields}
        optional = {"observe"} if step["action"] == "assert_js" else set()
        if not expected <= set(step) or set(step) - expected - optional:
            missing = sorted(expected - set(step))
            extra = sorted(set(step) - expected - optional)
            raise ValueError(f"浏览器动作字段不符合接口：第 {index} 步 {step['action']}，"
                             f"缺少 {missing}，多出 {extra}；必需 {sorted(expected)}，可选 {sorted(optional)}。")
        if (not isinstance(step["label"], str) or not step["label"].strip() or len(step["label"]) > 100
                or re.search(r"[\x00-\x1f\x7f]", step["label"])):
            raise ValueError("每步检查需要简短说明。")
        for key, limit in {**fields, **({"observe": 4000} if "observe" in step else {})}.items():
            value = step[key]
            if key == "ms":
                if type(value) is not int or not 0 <= value <= 2000:
                    raise ValueError("单步等待不能超过 2 秒。")
            elif key in {"x", "y"}:
                if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 16384:
                    raise ValueError("点击坐标必须是元素内有限的非负 CSS 像素坐标。")
            elif (not isinstance(value, str) or len(value) > limit
                  or (key != "value" and not value.strip() and not (key == "key" and value == " "))):
                raise ValueError("浏览器动作参数为空或过长。")
    if not any(s["action"].startswith("assert_") for s in steps):
        raise ValueError("检查必须包含可失败的行为断言，打开页面不算通过。")
    result = {**raw, "files": filenames}
    if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > 65536:
        raise ValueError("浏览器检查计划过大。")
    return result


def expand_browser_report(report: dict, plan: dict) -> dict:
    """Restore only plan metadata; outcomes remain the runner's observations."""
    if "steps_encoding" not in report:
        return report
    rows = report.get("steps")
    if (report["steps_encoding"] != "plan_index_v1" or not isinstance(rows, list)
            or len(rows) != len(plan["steps"])
            or report.get("plan_sha256") != hashlib.sha256(
                json.dumps(plan, ensure_ascii=False).encode("utf-8")).hexdigest()):
        raise ValueError("invalid compact browser report")
    restored = []
    for index, (row, step) in enumerate(zip(rows, plan["steps"]), 1):
        if (not isinstance(row, dict) or set(row) - {"index", "status", "actual", "error", "observed"}
                or type(row.get("index")) is not int or row["index"] != index
                or row.get("status") not in {"passed", "failed", "not_run"}
                or "actual" in row and type(row["actual"]) is not bool
                or "observed" in row and not isinstance(row["observed"], str)):
            raise ValueError("invalid compact browser observation")
        restored.append({**row, "action": step["action"], "label": step["label"]})
    return {**report, "steps": restored}


def browser_receipt(result, plan: dict) -> dict:
    """Validate the configured runner's attestation, not just its exit status."""
    payload = {"browser_plan": plan, "check_kind": "browser",
               "environment": "isolated_browser_context_not_host_sandbox", "outcome": result.payload["outcome"]}
    if result.payload.get("invocation_started") is False:
        return {**payload, "outcome": "unavailable", "browser_report": {
            "outcome": "unavailable", "reason": result.payload["reason"]}}
    try:
        report = json.loads(result.stdout_preview)
        stdout = result.payload.get("stdout", {})
        if (not isinstance(report, dict) or report.get("outcome") not in ("passed", "failed", "unavailable")
                or stdout.get("complete") is not True or stdout.get("truncated")):
            raise ValueError("incomplete browser report")
        report = expand_browser_report(report, plan)
        expected_hash = hashlib.sha256(json.dumps(plan, ensure_ascii=False).encode("utf-8")).hexdigest()
        if report["outcome"] == "passed":
            steps, counts = report.get("steps"), report.get("counts")
            if (report.get("plan_sha256") != expected_hash or not isinstance(steps, list)
                    or len(steps) != len(plan["steps"]) or not isinstance(counts, dict)
                    or type(counts.get("page_errors")) is not int or counts["page_errors"] != 0
                    or any(not isinstance(row, dict) or row.get("status") != "passed"
                           or type(row.get("index")) is not int or row["index"] != index + 1
                           or row.get("action") != step["action"]
                           or (step["action"].startswith("assert_") and row.get("actual") is not True)
                           for index, (row, step) in enumerate(zip(steps, plan["steps"])))):
                raise ValueError("browser report does not attest the planned steps")
        payload["browser_report"] = report
        if ((report["outcome"] == "unavailable" and result.payload.get("exit_code") == 2)
                or report.get("phase") == "validation"):
            payload["outcome"] = "unavailable"
        elif report["outcome"] != "passed" and payload["outcome"] == "passed":
            payload["outcome"] = "failed"
    except (ValueError, TypeError):
        payload.update(outcome="unavailable", browser_report={"outcome": "unavailable",
                       "reason": "浏览器执行器未返回完整有效的结构化回执，不能判定成果正确或错误。"})
    return payload


class BrowserChecks:
    def __init__(self, runtime, config_path: str | Path | None):
        self.rt = runtime
        self.command = None
        self.timeout = 90
        if config_path is not None:
            config = json.loads(Path(config_path).read_text(encoding="utf-8"))
            if not isinstance(config, dict) or set(config) - {"command", "timeout"}:
                raise ValueError("browser checker config requires command and optional timeout")
            self.command = config.get("command")
            self.timeout = config.get("timeout", 90)
            validate_check(self.command, self.timeout)
            if not Path(self.command[0]).is_absolute():
                raise ValueError("browser checker executable must use an absolute path")

    def capabilities(self) -> dict:
        return {"automatic_local_checks": bool(self.command),
                "tools": ([{"id": "browser_check", "scope": "submitted_static_html",
                            "description": "可检查本次提交的静态 HTML 和浏览器内交互；禁用外网，不执行宿主机脚本。"}]
                          if self.command else []),
                "manual_local_check_entrypoint": "Runtime.verify_work",
                "note": ("已配置可选浏览器检查器，是否可用以实际执行回执为准；不支持后端、包安装或原生应用检查。"
                         if self.command else "尚未配置自动实际检查执行器，文本模型不能代替实际运行。")}

    def _current(self, task_id, item):
        view = self.rt.work.view(task_id)
        current = next((row for row in view["items"] if row["id"] == item["id"]), None)
        if (current is None or not view.get("scope_current") or current["status"] != "submitted"
                or current["submission"] != item["submission"] or current.get("check_in_flight")
                or self.rt.task(task_id).status != "running"):
            raise WorkConflict("浏览器检查对应的成果或要求已变化，未执行旧计划。")
        return current

    def artifacts(self, task_id: str, item: dict) -> list[dict]:
        from .artifact_edits import select_artifact_versions

        return select_artifact_versions(self.rt, task_id,
            [item, *self.rt.work.accepted_dependencies(task_id, item)])

    def _planning_retry(self, task_id: str, binding: dict) -> dict:
        events = self.rt.log.read(task_id=task_id)
        owner = Actor.system("local_verifier")
        starts = [e for e in events if e.type == "verification.planning_started" and e.actor == owner
                  and all(e.payload.get(k) == v for k, v in binding.items())]
        if not starts:
            return {}
        previous = starts[-1]
        finishes = [e for e in events if e.type == "verification.planned" and e.actor == owner
                    and e.payload.get("request_seq") == previous.seq
                    and all(e.payload.get(k) == v for k, v in binding.items())]
        if not finishes or finishes[-1].payload.get("status") not in {"invalid", "unsupported", "unavailable"}:
            raise WorkConflict("检查规划尚未结束或已有可执行计划，不重复调用。")
        if any(e.type in {"work.check_started", "delivery.check_started"} and e.actor == owner
               and e.seq > previous.seq and e.payload.get("criterion_id") == binding["criterion_id"]
               and (e.payload.get("submission_seq") == binding.get("submission_seq")
                    if "submission_seq" in binding else e.payload.get("delivery_digest") == binding["delivery_digest"])
               for e in events):
            raise WorkConflict("已有实际检查调用，需要核对原回执，不能重复启动。")
        authorization = next((e for e in reversed(events) if e.seq > finishes[-1].seq and (
            e.type == "activity.queued" and e.actor == Actor.system("orchestrator")
            and e.payload.get("action") in {"run", "battle"}
            or e.type == "human.commanded" and e.actor == Actor.human()
            and e.payload.get("intent") == "resume" and e.payload.get("next_action") == "run")), None)
        if authorization is None:
            raise WorkConflict("本轮已尝试检查规划，等待新的明确继续请求后再安排。")
        return {"retry_of": previous.seq, "authorization_seq": authorization.seq}

    def _reusable_plan(self, task_id, binding, filenames):
        """Reuse test instructions, never their old verdict or execution receipt."""
        events = self.rt.log.read(task_id=task_id)
        bundles = {e.payload["digest"]: e.payload["manifest"] for e in events if e.type == "delivery.prepared"}

        def matches(event):
            payload = event.payload
            manifest = bundles.get(payload.get("delivery_digest"), {})
            return all(payload.get(k, manifest.get(k)) == binding.get(k)
                       for k in ("work_item_id", "criterion_id", "plan_seq", "scope"))

        previous = next((e for e in reversed(events)
            if e.type == "verification.completed" and e.actor == Actor.system("local_verifier")
            and e.payload.get("check_kind") == "browser"
            and matches(e)), None)
        if previous is None or not self.rt.work._receipt_passed(previous):
            return None
        old = previous.payload.get("browser_plan", {})
        try:
            plan = normalize_plan({k: old[k] for k in ("entrypoint", "steps")}, filenames)
        except (KeyError, ValueError, TypeError):
            return None
        return {"status": "ready", "plan": plan, "reused_plan_receipt_seq": previous.seq,
                "reason": "沿用同一验收目标的可重复检查步骤，在当前成果上重新执行；不沿用旧结论。"}

    def run(self, task_id: str, item: dict) -> None:
        if not self.command or not item.get("reviewer") or item.get("check_in_flight"):
            return
        try:
            artifacts = self.artifacts(task_id, item)
        except WorkConflict:
            return
        filenames = [a.get("filename") for a in artifacts]
        if not any(isinstance(f, str) and f.lower().endswith((".html", ".htm")) for f in filenames):
            return
        role = self.rt._role_by_id(item["reviewer"])
        driver = self.rt.drivers[role.driver]
        if not text_advice_available(driver):
            return
        for criterion in item["criteria"]:
            if criterion["kind"] != "local_check" or criterion["id"] in self.rt.work.checks(task_id, item):
                continue
            binding = {"work_item_id": item["id"], "criterion_id": criterion["id"],
                       "plan_seq": item["submission"]["plan_seq"], "scope": item["submission"]["scope"],
                       "submission_seq": item["submission"]["seq"], "submission_digest": item["submission"]["digest"]}

            def start():
                self._current(task_id, item)
                return "verification.planning_started", {**binding, **self._planning_retry(task_id, binding)}, Actor.system("local_verifier")

            try:
                started = self.rt.work._write(task_id, start)
            except WorkConflict:
                continue
            context = {**binding, "phase": "browser_check_plan", "criterion": criterion,
                       "available_entrypoints": [f for f in filenames if isinstance(f, str) and f.lower().endswith((".html", ".htm"))],
                       "artifacts": artifacts,
                       "customer_directions": self.rt.work.directions(task_id, binding["scope"]["direction_seq"])}
            context["regression_findings"] = [
                {"review_seq": e.seq, "reason": e.payload["reason"], "checks": e.payload["checks"]}
                for e in self.rt.log.iter_events(task_id=task_id, event_types=("work.reviewed",))
                if e.payload.get("work_item_id") == item["id"] and e.payload.get("status") == "rework"
                and e.payload.get("plan_seq") == binding["plan_seq"]][-2:]
            result = self._reusable_plan(task_id, binding, filenames)
            if result is None:
                result = self._plan(task_id, started, context, filenames, role, lambda: self._current(task_id, item))
            else:
                self.rt.log.emit(type="verification.planned", actor=Actor.system("local_verifier"), task_id=task_id,
                    payload={**binding, "request_seq": started.seq, **result})
            if result["status"] != "ready":
                continue
            try:
                self._current(task_id, item)
                self.rt.verify_work(task_id, item["id"], criterion["id"], [*self.command, "--plan", PLAN_FILE],
                    timeout=self.timeout, record_output=True, browser_plan=result["plan"],
                    expected_submission_seq=item["submission"]["seq"])
            except (WorkConflict, ValueError) as exc:
                self.rt.log.emit(type="verification.planned", actor=Actor.system("local_verifier"), task_id=task_id,
                    payload={**binding, "request_seq": started.seq, "status": "unavailable", "reason": str(exc)[:1000]})

    def repair(self, task_id: str, item: dict, *, attempted: set[str]) -> bool:
        """Correct a diagnosed test, not the source or an indeterminate invocation."""
        if not self.command or item.get("in_flight") or item.get("status") != "submitted":
            return False
        role = self.rt._role_by_id(item["reviewer"]) if item.get("reviewer") else None
        if role is None or not text_advice_available(self.rt.drivers[role.driver]):
            return False
        local = self.rt.work.checks(task_id, item)
        for check in item.get("checks", []):
            criterion_id = check["id"]
            receipt = local.get(criterion_id)
            if (criterion_id in attempted or check.get("next_action") != "revise_check"
                    or check.get("status") != "unknown" or check.get("source") != "model_assessment"
                    or check.get("response_issue") or receipt is None or check.get("receipt_seq") != receipt.seq
                    or receipt.payload.get("check_kind") != "browser"
                    or receipt.payload.get("outcome") not in {"passed", "failed"}):
                continue
            report = receipt.payload.get("browser_report", {})
            if (report.get("outcome") != receipt.payload["outcome"]
                    or report.get("phase", "execution") != "execution"
                    or report.get("truncated", False) is not False
                    or receipt.payload.get("exit_code") != (0 if receipt.payload["outcome"] == "passed" else 1)):
                continue
            criterion = next((c for c in item["criteria"] if c["id"] == criterion_id
                              and c["kind"] == "local_check"), None)
            if criterion is None:
                continue
            attempted.add(criterion_id)
            binding = {"work_item_id": item["id"], "criterion_id": criterion_id,
                       "plan_seq": item["submission"]["plan_seq"], "scope": item["submission"]["scope"],
                       "submission_seq": item["submission"]["seq"], "submission_digest": item["submission"]["digest"]}

            def start():
                current = self._current(task_id, item)
                checks = self.rt.work.checks(task_id, current)
                if (current.get("review_seq") != item.get("review_seq")
                        or current.get("in_flight")
                        or check not in current.get("checks", [])
                        or checks.get(criterion_id) != receipt):
                    raise WorkConflict("测试修正依据已变化或已处理，不能重复安排。")
                retry = (self._planning_retry(task_id, binding)
                         if any(e.payload.get("repair_of_receipt_seq") == receipt.seq
                                for e in self.rt.log.iter_events(task_id=task_id,
                                                                event_types=("verification.planning_started",)))
                         else {})
                return "verification.planning_started", {**binding, **retry, "review_seq": item["review_seq"],
                    "repair_of_receipt_seq": receipt.seq, "reason": check["reason"]}, Actor.system("local_verifier")

            try:
                artifacts = self.artifacts(task_id, item)
                started = self.rt.work._write(task_id, start)
            except WorkConflict:
                continue
            filenames = [a.get("filename") for a in artifacts]
            context = {**binding, "phase": "browser_check_plan", "criterion": criterion,
                       "available_entrypoints": [f for f in filenames if isinstance(f, str)
                                                  and f.lower().endswith((".html", ".htm"))],
                       "artifacts": artifacts, "previous_check": receipt_context(receipt),
                       "test_diagnosis": check, "instruction": (
                           "The independent reviewer diagnosed a defective or incomplete test. Correct its stated premise, "
                           "selectors, coordinates or assertions, or add the missing behavior checks against the unchanged "
                           "submitted source. Preserve the full criterion. "
                           "Do not remove failing behavior, edit the application, inject expected answers, or claim success."),
                       "customer_directions": self.rt.work.directions(task_id, binding["scope"]["direction_seq"])}
            result = self._plan(task_id, started, context, filenames, role, lambda: self._current(task_id, item))
            if result["status"] != "ready":
                return False
            if result["plan"] == receipt.payload.get("browser_plan"):
                self.rt.log.emit(type="verification.planned", actor=Actor.system("local_verifier"), task_id=task_id,
                    payload={**binding, "request_seq": started.seq, "status": "invalid",
                             "reason": "修正方案与失败方案相同，未重复执行；需要明确有效的测试修正。"})
                return False
            try:
                self._current(task_id, item)
                self.rt.verify_work(task_id, item["id"], criterion_id, [*self.command, "--plan", PLAN_FILE],
                    timeout=self.timeout, record_output=True, browser_plan=result["plan"],
                    expected_submission_seq=item["submission"]["seq"])
            except (WorkConflict, ValueError) as exc:
                self.rt.log.emit(type="verification.planned", actor=Actor.system("local_verifier"), task_id=task_id,
                    payload={**binding, "request_seq": started.seq, "status": "unavailable", "reason": str(exc)[:1000]})
                return False
            return True
        return False

    def _plan(self, task_id, started, context, filenames, role, current) -> dict:
        context["planning_attempt_seq"] = started.seq
        context["browser_limits"] = {"max_steps": MAX_BROWSER_STEPS, "max_plan_bytes": 65536,
                                     "per_step_wait_ms": 2000, "total_execution_ms": 60000}
        skill = Path(__file__).with_name("packs").joinpath("browser_check.md").read_text(encoding="utf-8")
        prompt = (skill + "\nSelect entrypoint exactly from this allowlist, not from an example: "
                  + json.dumps(context["available_entrypoints"], ensure_ascii=False)
                  + "\nCurrent check contract (untrusted reference data):\n" + json.dumps(context, ensure_ascii=False))
        request_prompt = prompt
        for attempt in range(2):
            result = {"status": "unavailable", "reason": "检查输入过大，没有截断成果后生成检查。"}
            usage, raw = {}, None
            try:
                current()
            except WorkConflict as exc:
                result = {"status": "stale", "reason": str(exc)}
            if result["status"] != "stale" and len(request_prompt) + len(json.dumps(self.rt.conversation_background(task_id), ensure_ascii=False)) <= 131072:
                response = self.rt._complete_with_containment(task_id=task_id, task=self.rt.task(task_id), role=role,
                    driver=self.rt.drivers[role.driver], prompt=request_prompt,
                    rule_snapshot=self.rt.rulebook.snapshot(task_id), work_context=context)
                usage = {"driver": role.driver, "speaker_id": role.id,
                         "tokens": response.tokens if valid_token_count(response.tokens) else 0,
                         "token_usage": self.rt._token_usage(response)}
                result = {"status": "unavailable", "reason": "检查方案调用未完整返回，没有执行浏览器操作。"}
                if not (response.meta or {}).get("contained"):
                    try:
                        raw = self.rt._parse_response(response.text)
                        if (set(raw) != {"status", "reason", "plan"} or raw["status"] not in ("ready", "unsupported")
                                or not isinstance(raw["reason"], str) or not 1 <= len(raw["reason"]) <= 1000):
                            raise ValueError("检查方案回复格式无效。")
                        if raw["status"] == "unsupported":
                            if raw["plan"] is not None:
                                raise ValueError("不支持的检查不能同时提供执行计划。")
                            result = raw
                        else:
                            result = {**raw, "plan": normalize_plan(raw["plan"], filenames)}
                    except (ValueError, TypeError) as exc:
                        result = {"status": "invalid", "reason": str(exc)}
            try:
                current()
            except WorkConflict as exc:
                result = {"status": "stale", "reason": str(exc)}
            correcting = attempt == 0 and result["status"] == "invalid"
            self.rt.log.emit(type="verification.planned", actor=Actor.system("local_verifier"), task_id=task_id,
                payload={**started.payload, "request_seq": started.seq, **result, **usage, "correction": attempt,
                         **({"status": "correcting"} if correcting else {})})
            if not correcting:
                break
            # Only an invalid text proposal is corrected; no execution is replayed.
            context["response_correction"] = {"issue": result["reason"], "previous_proposal": raw}
            request_prompt = (prompt + "\nYour proposal was not executed. Correct the exact validation issue "
                              "once and return the complete JSON proposal. Do not weaken the acceptance criterion.\n"
                              + json.dumps(context["response_correction"], ensure_ascii=False))
        return result

    def _current_delivery(self, task_id, delivery):
        current = self.rt.delivery.view(task_id)
        if (current.get("digest") != delivery["digest"] or current["status"] != "pending"
                or current.get("check_in_flight") or self.rt.task(task_id).status != "running"
                or self.rt.delivery._checks(task_id, delivery["digest"])):
            raise WorkConflict("整体浏览器检查对应的交付或检查状态已变化。")
        return current

    def run_delivery(self, task_id: str, delivery: dict) -> None:
        if not self.command or not delivery.get("reviewer") or delivery["status"] != "pending":
            return
        criteria = [c for c in delivery["manifest"]["criteria"] if c["id"] == "integration_behavior" and c["kind"] == "local_check"]
        if not criteria:
            return
        from .runtime import Role
        leader = self.rt.organization.leader
        role = (Role(leader.id, leader.name, leader.role, leader.driver) if delivery["reviewer"] == leader.id
                else self.rt._role_by_id(delivery["reviewer"]))
        if not text_advice_available(self.rt.drivers.get(role.driver)):
            return
        binding = {"delivery_digest": delivery["digest"], "criterion_id": "integration_behavior",
                   "plan_seq": delivery["manifest"]["plan_seq"], "scope": delivery["manifest"]["scope"]}

        def start():
            self._current_delivery(task_id, delivery)
            return "verification.planning_started", {**binding, **self._planning_retry(task_id, binding)}, Actor.system("local_verifier")

        try:
            started = self.rt.work._write(task_id, start)
        except WorkConflict:
            return
        files = delivery["manifest"]["files"]
        filenames = [f["path"] for f in files]
        try:
            validate_filenames(filenames)
            if any(f["capability"] == "manual" for f in files) or not any(n.lower().endswith((".html", ".htm")) for n in filenames):
                raise ValueError("unsupported bundle")
        except ValueError:
            self.rt.log.emit(type="verification.planned", actor=Actor.system("local_verifier"), task_id=task_id,
                payload={**binding, "request_seq": started.seq, "status": "unsupported",
                         "reason": "整包不属于名称唯一的静态 HTML 交付，受控浏览器不能覆盖其执行要求。"})
            return
        context = {**binding, "phase": "browser_check_plan", "criterion": criteria[0],
                   "manifest": delivery["manifest"],
                   "available_entrypoints": [n for n in filenames if n.lower().endswith((".html", ".htm"))],
                   "artifacts": [a for item in delivery["manifest"]["items"] for a in self.rt.work.artifacts(task_id, item["submission"])],
                   "customer_directions": self.rt.work.directions(task_id, binding["scope"]["direction_seq"])}
        result = self._reusable_plan(task_id, binding, filenames)
        if result is None:
            result = self._plan(task_id, started, context, filenames, role, lambda: self._current_delivery(task_id, delivery))
        else:
            self.rt.log.emit(type="verification.planned", actor=Actor.system("local_verifier"), task_id=task_id,
                payload={**binding, "request_seq": started.seq, **result})
        if result["status"] != "ready":
            return
        try:
            self._current_delivery(task_id, delivery)
            self.rt.delivery.verify(task_id, "integration_behavior", [*self.command, "--plan", PLAN_FILE],
                timeout=self.timeout, record_output=True, browser_plan=result["plan"], expected_digest=delivery["digest"])
        except (ValueError, WorkConflict) as exc:
            self.rt.log.emit(type="verification.planned", actor=Actor.system("local_verifier"), task_id=task_id,
                payload={**binding, "request_seq": started.seq, "status": "unavailable", "reason": str(exc)[:1000]})
