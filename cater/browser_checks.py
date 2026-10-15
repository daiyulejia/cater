"""Optional browser checks: model-selected tests, host-owned execution and receipts."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from pathlib import Path

from .artifacts import safe_filename
from .collaboration import text_advice_available
from .events import Actor
from .usage import valid_token_count
from .verification import validate_check
from .work import WorkConflict


PLAN_FILE = ".cater-browser-check.json"
ACTION_FIELDS = {
    "click": {"selector": 500}, "press": {"key": 50},
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
    if not isinstance(steps, list) or not 1 <= len(steps) <= 20:
        raise ValueError("浏览器检查须包含 1 至 20 个具体步骤。")
    for step in steps:
        if not isinstance(step, dict) or not isinstance(step.get("action"), str) or step["action"] not in ACTION_FIELDS:
            raise ValueError("未知浏览器动作。")
        fields = ACTION_FIELDS[step["action"]]
        if set(step) != {"action", "label", *fields}:
            raise ValueError("浏览器动作字段不符合接口。")
        if (not isinstance(step["label"], str) or not step["label"].strip() or len(step["label"]) > 100
                or re.search(r"[\x00-\x1f\x7f]", step["label"])):
            raise ValueError("每步检查需要简短说明。")
        for key, limit in fields.items():
            value = step[key]
            if key == "ms":
                if type(value) is not int or not 0 <= value <= 2000:
                    raise ValueError("单步等待不能超过 2 秒。")
            elif (not isinstance(value, str) or len(value) > limit
                  or (key != "value" and not value.strip() and not (key == "key" and value == " "))):
                raise ValueError("浏览器动作参数为空或过长。")
    if not any(s["action"].startswith("assert_") for s in steps):
        raise ValueError("检查必须包含可失败的行为断言，打开页面不算通过。")
    result = {**raw, "files": filenames}
    if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > 65536:
        raise ValueError("浏览器检查计划过大。")
    return result


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
        artifacts = self.rt.work.artifacts(task_id, item["submission"])
        for dependency in self.rt.work.view(task_id)["items"]:
            expected = item["submission"].get("inputs", {}).get(dependency["id"])
            if expected is None:
                continue
            if dependency["status"] != "accepted" or (dependency.get("submission") or {}).get("digest") != expected:
                raise WorkConflict("检查依赖成果已变化，不能检查新的代码却沿用旧提交。")
            artifacts.extend(self.rt.work.artifacts(task_id, dependency["submission"]))
        return artifacts

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
                # A durable reservation prevents duplicate plans/calls after re-entry or restart.
                if any(e.type == "verification.planning_started" and all(e.payload.get(k) == v for k, v in binding.items())
                       for e in self.rt.log.iter_events(task_id=task_id)):
                    raise WorkConflict("本次成果已安排浏览器检查，不自动重复调用。")
                return "verification.planning_started", binding, Actor.system("local_verifier")

            try:
                started = self.rt.work._write(task_id, start)
            except WorkConflict:
                continue
            context = {**binding, "phase": "browser_check_plan", "criterion": criterion,
                       "available_entrypoints": [f for f in filenames if isinstance(f, str) and f.lower().endswith((".html", ".htm"))],
                       "artifacts": artifacts,
                       "customer_directions": self.rt.work.directions(task_id, binding["scope"]["direction_seq"])}
            result = self._plan(task_id, started, context, filenames, role, lambda: self._current(task_id, item))
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

    def _plan(self, task_id, started, context, filenames, role, current) -> dict:
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
            if any(e.type == "verification.planning_started" and e.actor == Actor.system("local_verifier")
                   and all(e.payload.get(k) == v for k, v in binding.items())
                   for e in self.rt.log.iter_events(task_id=task_id)):
                raise WorkConflict("本次整包已安排浏览器检查，不自动重复调用。")
            return "verification.planning_started", binding, Actor.system("local_verifier")

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
        result = self._plan(task_id, started, context, filenames, role, lambda: self._current_delivery(task_id, delivery))
        if result["status"] != "ready":
            return
        try:
            self._current_delivery(task_id, delivery)
            self.rt.delivery.verify(task_id, "integration_behavior", [*self.command, "--plan", PLAN_FILE],
                timeout=self.timeout, record_output=True, browser_plan=result["plan"], expected_digest=delivery["digest"])
        except (ValueError, WorkConflict) as exc:
            self.rt.log.emit(type="verification.planned", actor=Actor.system("local_verifier"), task_id=task_id,
                payload={**binding, "request_seq": started.seq, "status": "unavailable", "reason": str(exc)[:1000]})
