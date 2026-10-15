"""Pure progress descriptions of work.view and delivery.view snapshots."""

from __future__ import annotations


_LOCAL_LIMIT = "实际检查需可信本机执行及当前完整回执，不能拿模型 API 当执行工具。"
_NO_REPLAY = "结果不明时须先核对已有执行及副作用，不能自动重放。"
_UNNAMED = "未命名角色"


def work_checkpoint(work: dict, delivery: dict, names: dict) -> dict:
    """Describe recorded progress without reading clocks, invoking tools or mutating views.

    names maps role IDs to display names. IDs and verbatim reasons stay in details;
    summary uses only structured states and display names. started is historical
    evidence, not liveness. produced/artifacts count retained submissions, not
    deleted history. All detail rows survive the two-blocker summary limit.
    ready with a retained reason or attempt is pending attention, not a first run.
    """
    items = work.get("items") or []
    by_id = {i["id"]: i for i in items}

    def name(role_id):
        value = names.get(role_id)
        return value if isinstance(value, str) and value.strip() else _UNNAMED

    def people(role_ids):
        values = list(dict.fromkeys(name(r) for r in role_ids))
        return "、".join(values[:3]) + (f"等{len(values)}位" if len(values) > 3 else "")

    counts = dict.fromkeys(("total", "started", "not_started", "produced", "artifacts", "running",
                            "submitted", "accepted", "blocked", "ready", "rework", "stale", "uncertain",
                            "planned", "checking", "waiting_review", "waiting_local_check"), 0)
    counts["total"] = len(items)
    details, blockers, artifact_seqs = [], [], set()
    local_required = False

    def record(item, state, reason, action, actor=None, *, scope="work"):
        row = {"work_item_id": item.get("id"), "objective": item.get("objective", ""), "scope": scope,
               "owner": item.get("owner"), "owner_name": name(item.get("owner")),
               "reviewer": item.get("reviewer"), "reviewer_name": name(item.get("reviewer")),
               "status": item.get("status"), "state": state, "reason": reason,
               "source_reason": item.get("reason", ""), "next_actor": actor,
               "next_actor_name": name(actor), "next_action": action,
               "depends_on": list(item.get("depends_on") or []),
               "blocked_by": [d for d in item.get("depends_on", [])
                              if d not in by_id or by_id[d].get("status") != "accepted"],
               "checks": [dict(c) for c in item.get("checks") or []]}
        details.append(row)
        if state not in {"running", "stage_accepted", "accepted"}:
            blockers.append(row)

    for item in items:
        status = item.get("status", "planned")
        counts[status] = counts.get(status, 0) + 1
        submission = item.get("submission")
        started = bool(item.get("attempt_id") or submission or item.get("review_seq")
                       or status in {"running", "uncertain", "submitted", "accepted"})
        counts["started"] += int(started)
        counts["not_started"] += int(not started)
        if submission:
            counts["produced"] += 1
            artifact_seqs.update(submission.get("artifact_seqs") or [])
        owner, reviewer = item.get("owner"), item.get("reviewer")
        reason = item.get("reason") or ""
        counts["checking"] += int(bool(item.get("check_in_flight")))
        local_required |= bool(item.get("check_in_flight") or item.get("check_uncertain"))

        if status == "uncertain" or item.get("check_uncertain"):
            record(item, "uncertain", reason or "已有执行或检查结果不明。",
                   "核对已有尝试、回执和副作用，再决定如何继续", owner)
            if status != "uncertain":
                counts["uncertain"] += 1
        elif status == "running":
            record(item, "running", reason or "成果执行在途。", "核对本次执行结果并提交成果", owner)
        elif status == "submitted":
            checks = item.get("checks") or []
            checked = {c["id"]: c for c in checks}
            lifecycle = item.get("check_lifecycle") or {}
            local = [c for c in item.get("criteria", []) if c.get("kind") == "local_check"]
            missing = [c for c in local if not (
                type(checked.get(c["id"], {}).get("receipt_seq")) is int
                or lifecycle.get(c["id"], {}).get("receipt_seqs"))]
            if item.get("check_in_flight"):
                local_required = True
                record(item, "waiting_check", reason or "本机检查已启动，等待完整回执。",
                       "核对已有检查的完整回执后再复核", reviewer or owner)
            elif missing:
                local_required = True
                counts["waiting_local_check"] += 1
                record(item, "waiting_local_check", "缺少当前完整实际检查回执："
                       + "；".join(c.get("text", "") for c in missing),
                       "安排可信本机检查并补齐回执，再交独立复核", owner)
            issues = [c for c in checks if c.get("response_issue")]
            if issues:
                record(item, "review_response_issue", "；".join(c.get("reason", "") for c in issues),
                       "更正复核回复格式、正文引用或回执关联，再给出结论", reviewer)
            if not missing and not item.get("check_in_flight"):
                counts["waiting_review"] += 1
                if not issues or any(c.get("status") != "passed" and not c.get("response_issue") for c in checks):
                    record(item, "waiting_review", reason or "复核结论尚未确认。",
                           "核对当前成果和证据，明确通过、补证或返工" if reviewer else "配置独立复核人并核对当前成果",
                           reviewer)
        else:
            actions = {"accepted": ("stage_accepted", "参与整体交付核对", reviewer),
                       "blocked": ("blocked", "在前置成果通过验收后接手后续工作", owner),
                       "ready": ("ready", "接手本次执行并提交成果", owner),
                       "planned": ("planned", "接手计划中的工作并提交成果", owner),
                       "rework": ("rework", "按返工依据修正并重新提交", owner),
                       "stale": ("stale", "核对最新要求、依赖及已有执行，再更新计划或成果", owner)}
            state, action, actor = actions.get(status, ("unknown", "核对已有工作记录后决定下一步", owner))
            if status == "ready" and (reason or started):
                state, action = "ready_pending", "核对保留依据及已有记录，必要时补正后提交"
            record(item, state, reason, action, actor)

    counts["artifacts"] = len(artifact_seqs)
    stages = bool(items) and all(i.get("status") == "accepted" for i in items)
    stale = work.get("scope_current") is False
    if stale or (stages and work.get("stage_accepted") is False):
        record({}, "stale", "当前计划已失效或阶段结论需重新核对。", "核对最新要求及计划", scope="plan")
    elif stages:
        status = delivery.get("status", "unprepared")
        reviewer = delivery.get("reviewer")
        local = delivery.get("local_checks") or []
        local_required = any(c.get("status") != "passed" for c in local) or any(
            not any(r.get("id") == c.get("id") and r.get("status") == "passed" for r in local)
            for c in (delivery.get("manifest") or {}).get("criteria", []) if c.get("kind") == "local_check")
        if status == "uncertain" or delivery.get("check_uncertain"):
            state, action = "delivery_uncertain", "核对已有整体检查或复核结果后继续"
        elif status == "stale":
            state, action = "delivery_stale", "核对版本变化并重新准备整体交付"
        elif status == "checking" or delivery.get("check_in_flight"):
            local_required = True
            state, action = "delivery_checking", "核对已有整体检查的完整回执后复核"
        elif status == "reviewing":
            state, action = "delivery_reviewing", "核对已有整体复核调用的结果"
        elif status == "accepted" and not local_required:
            state, action = "accepted", "查收当前交付包"
        elif status == "rework":
            state, action = "delivery_rework", "核对整体返工依据并安排修正"
        elif local_required:
            state, action = "delivery_local_check", "安排可信本机整体检查并补齐回执，再进行独立复核"
        else:
            state, action = "delivery_pending", ("核对全部成果及要求，完成整体验收" if reviewer else
                                                "准备交付并配置独立整体复核人")
        record(delivery, state, delivery.get("reason", ""), action, reviewer, scope="delivery")
    if not items:
        record({}, "unplanned", "尚无工作计划或执行记录。", "核对要求并安排工作", scope="plan")

    priority = ("uncertain", "delivery_uncertain", "stale", "delivery_stale", "running", "delivery_checking",
                "delivery_reviewing", "rework", "delivery_rework", "ready_pending", "waiting_check", "waiting_local_check",
                "delivery_local_check", "review_response_issue", "waiting_review", "ready", "delivery_pending",
                "blocked", "planned", "unknown", "unplanned", "accepted")
    states = {r["state"] for r in details}
    state = next((s for s in priority if s in states), "unknown")
    started = bool(counts["started"])
    producers = people(i.get("owner") for i in items if i.get("submission"))
    progress = (f"已开始；{producers}已提交 {counts['produced']} 项成果（{counts['artifacts']} 份文件）"
                if counts["produced"] else "已开始，尚无当前提交成果" if started else "尚无成果执行启动记录")
    activity = f"当前 {counts['running']} 项成果执行在途" if counts["running"] else "目前没有已确认正在运行的成果执行"
    headline = f"{progress}，阶段通过 {counts['accepted']}/{counts['total']} 项；{activity}。"
    phrases = {
        "uncertain": "的执行或检查结果不明，需先核对",
        "delivery_uncertain": "的整体检查或复核结果不明，需先核对",
        "stale": "的计划或成果已失效，需重新核对",
        "delivery_stale": "的旧整体交付已失效，需重新准备",
        "rework": "负责的成果明确需要返工", "delivery_rework": "的整体复核明确要求返工",
        "waiting_check": "的本机检查已启动，正在等待完整回执",
        "waiting_local_check": "的已提交成果缺少完整实际检查回执",
        "delivery_local_check": "的整体交付仍需当前版本的实际行为检查",
        "review_response_issue": "的复核回复存在格式或引用错误，尚未确认通过；这不表示成果本身被否决",
        "waiting_review": "的复核结论尚未确认，已提交成果仍待复核",
        "ready": "可接手，但本次执行尚未开始", "planned": "的计划工作尚无启动记录",
        "ready_pending": "的工作待核对或补正，当前未在执行",
        "blocked": "的后续工作等待前置成果验收",
        "delivery_pending": "仍需准备或完成整体验收，全部阶段通过不等于整体交付通过",
        "delivery_checking": "的整体检查已启动，等待完整回执",
        "delivery_reviewing": "的整体复核调用在途，等待结果",
        "unknown": "的工作状态需核对", "unplanned": "需先安排工作计划",
    }
    selected = []
    for kind in priority:
        rows = [r for r in blockers if r["state"] == kind]
        if rows:
            selected.append(rows)
        if len(selected) == 2:
            break
    summary = [headline]
    for rows in selected:
        kind = rows[0]["state"]
        role_key = "reviewer" if kind in {"review_response_issue", "waiting_review"} else "owner"
        if rows[0]["scope"] == "delivery":
            role_key = "reviewer"
        text = people(r.get(role_key) for r in rows) + phrases[kind]
        if kind == "blocked" and all(not by_id[r["work_item_id"]].get("attempt_id")
                                     and not by_id[r["work_item_id"]].get("submission") for r in rows):
            text += "，尚未开始"
        summary.append(text + "。")
    if state == "accepted":
        summary.append("整体验收已通过，可查收当前交付包。")
    elif selected:
        row = selected[0][0]
        summary.append(f"下一步：{row['next_actor_name']}{row['next_action']}。")
    elif state == "running":
        summary.append("下一步：" + people(i.get("owner") for i in items if i.get("status") == "running")
                       + "核对本次执行结果并提交成果。")
    if local_required:
        summary.append(_LOCAL_LIMIT)
    if states & {"uncertain", "delivery_uncertain"}:
        summary.append(_NO_REPLAY)
    return {"summary": "\n".join(summary), "headline": headline, "state": state, "started": started,
            "counts": counts, "blockers": blockers, "details": details}
