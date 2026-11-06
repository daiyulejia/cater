"""Bounded discovery and exact, read-only access to this team's customer inputs."""

from bisect import bisect_left, bisect_right
from collections import defaultdict

from .events import Actor
from .tool_protocol import json_utf8

INDEX_SIZE = 8
MAX_PAGE = 50
TEXT_CHARS = 4000
TEXT_JSON_BYTES = 8000
PREVIEW_UTF8_BYTES = 96
PREVIEW_JSON_BYTES = 128
INDEX_JSON_BYTES = 8000
NOTE = ("这些是本群顾客输入的来源索引，不判断哪些是要求、撤销、追问或仍然有效；"
        "目标变更前的消息可能是背景，也可能是变更来源，须结合原文和目标判断。"
        "预览可能完整也可能为节选，看preview_truncated；仅用于发现和按需核对，不能直接推为当前要求。"
        "preview_ref指向同轮字段或按seq和part对应的entries项。"
        "acceptance_source的event_seq、event_hash及count可用于逐项读取验收来源；count=0表示该来源为空。"
        "来源记录只证明输入存在，不证明它已被处理或完整理解；被中断的处理不等于已理解。"
        "触发来源关联只标识原输入，不证明理解正确；旧式命令instruction正文仅按需读取，goal/acceptance可有预览，均不自动判定为有效要求。"
        "读取不会执行旧操作。")


class CustomerSourceIndexError(ValueError):
    """Expected unusable source-index data or a bounded-index failure."""


def _customer_message(event):
    return (event.type == "team.message_received" and event.actor == Actor.human()
            and event.payload.get("kind") == "message" and isinstance(event.payload.get("text"), str))


def _trusted_legacy_command(event):
    # Only direct Runtime.command shapes: conversation/clarification receipts
    # and unfamiliar producers must not be reclassified as original commands.
    allowed = {"instruction", "intent", "next_action", "goal", "acceptance", "mentions",
               "mentioned_workers", "roster_mode", "requested_strategy"}
    value = event.payload.get("instruction")
    intent, action = event.payload.get("intent"), event.payload.get("next_action")
    acceptance = event.payload.get("acceptance")
    direction_fields = {"goal", "acceptance", "mentions", "mentioned_workers", "roster_mode", "requested_strategy"}
    # 08b0507 wrote instruction and optional goal/acceptance; e518271 added
    # mentions; d14c288 added next_action and optional direction metadata.
    original = not ({"intent", "next_action"} & event.payload.keys()) and not (set(event.payload) - {"instruction", "goal", "acceptance", "mentions"})
    pre_intent = "intent" not in event.payload and action in ("run", "battle")
    historical = original or pre_intent
    shape_ok = (historical or intent == "direction" and action in ("run", "battle")
                or intent == "status" and action == "none" and not (direction_fields & event.payload.keys())
                or intent == "resume" and action == "run" and not (direction_fields & event.payload.keys()))
    # At and after e518271 mentions are derived from text; before it the
    # minimal 08b0507 payload could carry non-string instruction values.
    instruction_ok = ("instruction" in event.payload and
                      (isinstance(value, str) and (historical or bool(value.strip()))
                       or original and "mentions" not in event.payload))
    # A blank instruction cannot contain a mention or request governance.
    blank_ok = (not isinstance(value, str) or bool(value.strip())
                or "mentions" not in event.payload and "mentioned_workers" not in event.payload and action != "battle")
    # Direction producers emit strategy iff battle, and roster iff workers.
    action_ok = ("next_action" not in event.payload or intent in ("status", "resume")
                 or (action == "battle") == (event.payload.get("requested_strategy") == "cabinet_review"))
    workers, mentions = event.payload.get("mentioned_workers"), event.payload.get("mentions")
    roster_ok = ("roster_mode" in event.payload) == ("mentioned_workers" in event.payload)
    if isinstance(workers, list) and isinstance(mentions, list):
        order = iter(mentions)
        workers_in_order = all(any(item == worker for item in order) for worker in workers)
    else:
        workers_in_order = workers is None
    return (event.type == "human.commanded" and event.actor == Actor.human()
            and not set(event.payload) - allowed and shape_ok and instruction_ok and blank_ok and action_ok and roster_ok and workers_in_order
            and ("goal" not in event.payload or isinstance(event.payload["goal"], str)
                 and (historical or bool(event.payload["goal"].strip())))
            and ("acceptance" not in event.payload or isinstance(acceptance, list) and all(isinstance(item, str) for item in acceptance))
            and all(key not in event.payload or isinstance(event.payload[key], list) and bool(event.payload[key])
                    and all(isinstance(item, str) for item in event.payload[key]) for key in ("mentions", "mentioned_workers"))
            and ("roster_mode" not in event.payload or event.payload["roster_mode"] in ("replace", "remove", "append"))
            and ("requested_strategy" not in event.payload or event.payload["requested_strategy"] == "cabinet_review"))


def _legacy_instruction(event):
    value = event.payload.get("instruction")
    return value if _trusted_legacy_command(event) and isinstance(value, str) else None


def _instruction_part(event):
    items = event.payload.get("acceptance")
    return len(items) + 1 if isinstance(items, list) else 1


def _source_text(event, part=0):
    if part == 0 and _customer_message(event):
        return event.payload["text"]
    if event.type == "human.commanded" and not _trusted_legacy_command(event):
        return None
    if event.type in {"task.created", "human.commanded"} and event.actor == Actor.human():
        goal = event.payload.get("goal")
        if part == 0 and isinstance(goal, str):
            return event.payload["goal"]
        items = event.payload.get("acceptance")
        if event.type == "human.commanded" and part > 0 and part == _instruction_part(event):
            return _legacy_instruction(event)
        if (part > 0 and isinstance(items, list) and part <= len(items) and isinstance(items[part - 1], str)
                and (event.type == "task.created" or all(isinstance(item, str) for item in items))):
            return items[part - 1]
    return None


def _source_parts(event):
    if event.type == "human.commanded" and not _trusted_legacy_command(event):
        return
    if _source_text(event) is not None:
        yield event, 0
    if (event.type in {"task.created", "human.commanded"} and event.actor == Actor.human()
            and isinstance(event.payload.get("acceptance"), list)):
        items = event.payload["acceptance"]
        if event.type == "task.created" or all(isinstance(item, str) for item in items):
            for number, value in enumerate(items):
                if isinstance(value, str):
                    yield event, number + 1
    if _legacy_instruction(event) is not None:
        yield event, _instruction_part(event)


def _source_type(event, part):
    if event.type == "human.commanded":
        if part > 0 and part == _instruction_part(event) and _legacy_instruction(event) is not None:
            return "legacy_command_instruction"
        return "legacy_command_acceptance" if part else "legacy_command_goal"
    return "task_acceptance" if part else "task_goal" if event.type == "task.created" else "message"


def _positive(value):
    return type(value) is int and value > 0


def _preview(text):
    # Preserve whole short texts; never substitute an invalid Unicode character.
    result, size = [], 0
    for char in text:
        try:
            part = char.encode("utf-8")
        except UnicodeEncodeError:
            break
        if size + len(part) > PREVIEW_UTF8_BYTES:
            break
        candidate = "".join(result) + char
        if len(json_utf8({"preview": candidate})) > PREVIEW_JSON_BYTES:
            break
        result.append(char)
        size += len(part)
    value = "".join(result)
    return {"preview": value, "preview_truncated": len(value) < len(text)}


def _input_state(events, anchor):
    """Follow fold's order, then the team's last-host-goal override, at each tool start."""
    unknown = {"valid": False, "folded_goal": None, "folded_origin": None,
               "acceptance_source": {"status": "unknown"}, "goals_at_start": {}}
    if any(type(event.seq) is not int or event.seq < 1 or (i and event.seq <= events[i - 1].seq)
           for i, event in enumerate(events)):
        return unknown
    creations = [event for event in events if event.seq <= anchor.seq and event.type == "task.created"]
    if (len(creations) != 1 or creations[0].actor != Actor.human()
            or not isinstance(creations[0].payload.get("goal"), str) or not creations[0].payload["goal"].strip()):
        return unknown
    folded_goal, folded_origin, team_goal = None, None, None
    from .team_review_direction import effective_objectives
    effective = effective_objectives([e for e in events if e.seq <= anchor.seq])
    acceptance = {"status": "unknown"}
    goals_at_start = {}
    for event in events:
        if event.seq > anchor.seq:
            break
        payload = event.payload
        if event.type == "task.created":
            value = payload.get("goal", "")
            folded_goal = value if (event.actor == Actor.human() and "goal" in payload
                                    and isinstance(value, str) and bool(value.strip())) else None
            folded_origin = event if folded_goal is not None else None
            raw_items = payload.get("acceptance", [])
            items = raw_items if isinstance(raw_items, list) else []
            acceptance = ({"status": "initial_acceptance", "event_seq": event.seq, "event_hash": event.hash,
                           "count": len(items), "readable": True}
                          if event.actor == Actor.human() and (raw_items is None or isinstance(raw_items, list))
                          and all(isinstance(item, str) for item in items) else {"status": "unknown"})
        elif event.type == "human.commanded":
            # fold applies goal only when truthy, but applies acceptance whenever present.
            from .directions import changes_direction
            if changes_direction(payload):
                team_goal = None
            trusted = _trusted_legacy_command(event)
            if payload.get("goal"):
                value = payload["goal"]
                folded_goal = value if trusted else None
                folded_origin = event if folded_goal is not None else None
            if "acceptance" in payload:
                raw_items = payload.get("acceptance")
                items = raw_items if isinstance(raw_items, list) else []
                acceptance = ({"status": "legacy_command_acceptance", "event_seq": event.seq,
                               "event_hash": event.hash, "count": len(items), "readable": True}
                              if trusted else {"status": "unknown"})
        elif event.type == "team.objective_updated" and event.seq in effective:
            team_goal = event
        elif event.type == "team.tool_started" and event.actor == Actor.system("team") and payload.get("name") == "set_objective":
            value = team_goal.payload.get("goal") if team_goal else folded_goal
            goals_at_start[event.seq] = value if isinstance(value, str) else None
    return {"valid": True, "folded_goal": folded_goal, "folded_origin": folded_origin,
            "acceptance_source": acceptance, "goals_at_start": goals_at_start}


def _goal_origin(events, goal, anchor, state=None, member_ids=None):
    # EventLog provides strictly increasing task events; do not apply binary
    # searches to a broken or ambiguous chain.
    if any(type(event.seq) is not int or event.seq < 1 or (i and event.seq <= events[i - 1].seq)
           for i, event in enumerate(events)):
        return {"status": "unknown", **({"objective_update_seq": goal.seq} if goal is not None else {})}
    state = state if state is not None else _input_state(events, anchor)
    if not state["valid"]:
        return {"status": "unknown", **({"objective_update_seq": goal.seq} if goal is not None else {})}
    if goal is None:
        origin = state["folded_origin"]
        if origin is None:
            return {"status": "unknown"}
        if origin.type == "human.commanded":
            return {"status": "legacy_command_goal", "event_seq": origin.seq, "event_hash": origin.hash,
                    "readable": True}
        return {"status": "initial_goal", "event_seq": origin.seq, "event_hash": origin.hash}
    unknown = {"status": "unknown", "objective_update_seq": goal.seq}
    run_id = goal.payload.get("run_id")
    if not isinstance(run_id, str) or not 1 <= len(run_id) <= 128 or not run_id.strip():
        return unknown
    hosts = [e for e in events if e.seq <= anchor.seq and e.actor == Actor.system("team")]
    runs_by_id, starts_by_id, receipts_by_start = defaultdict(list), defaultdict(list), defaultdict(list)
    external_by_id, run_ends_by_id, external_ends_by_start = defaultdict(list), defaultdict(list), defaultdict(list)
    transitions = []
    from .team_review_direction import effective_objectives
    effective = effective_objectives([e for e in events if e.seq <= anchor.seq])
    for event in hosts:
        payload, kind = event.payload, event.type
        identity, started = payload.get("run_id"), payload.get("started_seq")
        if event.seq in effective:
            transitions.append(event)
        if isinstance(identity, str):
            if kind == "team.run_started":
                runs_by_id[identity].append(event)
            elif kind == "team.tool_started" and payload.get("name") == "set_objective":
                starts_by_id[identity].append(event)
            elif kind == "team.external_run_started":
                external_by_id[identity].append(event)
            elif kind == "team.run_finished":
                run_ends_by_id[identity].append(event.seq)
        if type(started) is int:
            if kind == "team.tool_finished":
                receipts_by_start[started].append(event)
            elif kind == "team.external_run_finished":
                external_ends_by_start[started].append(event.seq)
    runs = runs_by_id[run_id]
    if len(runs) != 1:
        return unknown
    run = runs[0]
    seq, role = run.payload.get("message_seq"), run.payload.get("role")
    source = next((e for e in events if type(seq) is int and e.seq == seq and _customer_message(e)), None)
    if (source is None or not isinstance(role, str) or member_ids is None or role not in member_ids
            or type(run.payload.get("revision")) is not int
            or source.payload.get("recipient") != role or not source.seq < run.seq < goal.seq):
        return unknown
    contract = {"version": 1, "kind": "atomic_local_events", "receipt": "same_transaction"}
    prior = next((e for e in reversed(transitions) if e.seq < run.seq), None)
    revision = prior.seq if prior else 0
    if run.payload["revision"] != revision:
        return unknown
    work_status = "done" if prior and prior.type == "team.objective_completed" else "running"
    cursor = run.seq
    changes = [e for e in transitions if run.seq < e.seq <= goal.seq]
    run_starts = starts_by_id[run_id]
    start_seqs = [e.seq for e in run_starts]
    external_starts = external_by_id[run_id]
    external_seqs = [e.seq for e in external_starts]
    run_end_seqs = run_ends_by_id[run_id]
    for position, change in enumerate(changes):
        if change.type != "team.objective_updated" or change.payload.get("run_id") != run_id:
            return unknown
        starts = run_starts[bisect_right(start_seqs, cursor):bisect_left(start_seqs, change.seq)]
        if not starts:
            return unknown
        end = changes[position + 1].seq if position + 1 < len(changes) else anchor.seq + 1
        last_receipt = cursor
        for position_in_change, start in enumerate(starts):
            args = start.payload.get("arguments")
            if (start.seq <= last_receipt or start.payload.get("role") != role
                    or type(start.payload.get("message_seq")) is not int or start.payload["message_seq"] != source.seq
                    or type(start.payload.get("revision")) is not int or start.payload["revision"] != revision
                    or not isinstance(start.payload.get("call_id"), str)
                    or not 1 <= len(start.payload["call_id"]) <= 256 or not start.payload["call_id"].strip()
                    or start.payload.get("execution_contract") != contract
                    or type(start.payload.get("execution_contract", {}).get("version")) is not int
                    or not isinstance(args, dict)):
                return unknown
            # External sessions additionally need their own host-anchored start.
            external_seq = start.payload.get("external_started_seq")
            external_events = external_starts[bisect_right(external_seqs, run.seq):bisect_left(external_seqs, start.seq)]
            external = None
            if external_seq is not None or external_events:
                from .external_runs import EXTERNAL_RUN_CONTRACT

                external = next((e for e in external_events if type(external_seq) is int and e.seq == external_seq), None)
                if (len(external_events) != 1 or external is None
                        or type(external.payload.get("run_started_seq")) is not int
                        or external.payload["run_started_seq"] != run.seq
                        or external.payload.get("run_started_hash") != run.hash
                        or type(external.payload.get("message_seq")) is not int
                        or any(external.payload.get(k) != run.payload.get(k) for k in ("run_id", "role", "message_seq"))
                        or external.payload.get("execution_contract") != EXTERNAL_RUN_CONTRACT
                        or type(external.payload.get("execution_contract", {}).get("version")) is not int):
                    return unknown
            receipts = receipts_by_start[start.seq]
            if len(receipts) != 1:
                return unknown
            receipt = receipts[0]
            result = receipt.payload.get("result")
            if (receipt.seq <= start.seq or not isinstance(result, dict)
                    or any(receipt.payload.get(k) != start.payload.get(k) for k in ("run_id", "role", "name"))
                    or "call_id" in receipt.payload and receipt.payload["call_id"] != start.payload["call_id"]
                    or bisect_right(run_end_seqs, run.seq) < bisect_left(run_end_seqs, receipt.seq)
                    or external is not None and bisect_right(external_ends_by_start[external.seq], external.seq)
                       < bisect_left(external_ends_by_start[external.seq], receipt.seq)):
                return unknown
            if position_in_change < len(starts) - 1:
                current_goal = state["goals_at_start"].get(start.seq)
                invalid = ((set(args) - {"goal"} or "goal" not in args)
                    and set(result) == {"status", "code", "complete", "outcome", "message", "allowed_fields", "required_fields"}
                    and result.get("status") == "error" and result.get("code") == "invalid_arguments"
                    and result.get("complete") is True and result.get("outcome") == "not_started"
                    and result.get("allowed_fields") == ["goal"] and result.get("required_fields") == ["goal"]
                    and isinstance(result.get("message"), str) and bool(result["message"]))
                unchanged = (set(args) == {"goal"} and isinstance(args.get("goal"), str)
                    and len(args["goal"]) <= 12000 and bool(args["goal"].strip())
                    and isinstance(current_goal, str) and args["goal"].strip() == current_goal
                    and work_status == "running" and set(result) == {"status", "goal", "revision", "work_status", "scope_changed"}
                    and result.get("status") == "ok" and result.get("goal") == current_goal
                    and type(result.get("revision")) is int and result["revision"] == revision
                    and result.get("work_status") == "running" and result.get("scope_changed") is False)
                if not receipt.seq < starts[position_in_change + 1].seq or not (invalid or unchanged):
                    return unknown
            elif (set(args) != {"goal"} or not isinstance(args.get("goal"), str)
                  or args["goal"].strip() != change.payload.get("goal") or not change.seq < receipt.seq < end
                  or result.get("status") != "ok" or type(result.get("revision")) is not int
                  or result["revision"] != change.seq or result.get("scope_changed") is not True
                  or result.get("goal") != change.payload.get("goal") or result.get("work_status") != "running"
                  or set(result) - {"status", "goal", "revision", "work_status", "scope_changed", "continuation_note"}):
                return unknown
            last_receipt = receipt.seq
        revision, cursor, work_status = change.seq, last_receipt, "running"
    if not changes or changes[-1].seq != goal.seq:
        return unknown
    return {"status": "linked_customer_input", "objective_update_seq": goal.seq,
            "message_seq": source.seq, "message_hash": source.hash}


def index(events, *, as_of_seq=None, as_of_hash=None, before_seq=None, before_part=None, limit=INDEX_SIZE,
          preview_refs=None, member_ids=None, task_id=None):
    if task_id is not None and any(getattr(event, "task_id", None) != task_id for event in events):
        raise CustomerSourceIndexError("顾客输入索引只能读取本群事件。")
    if any(type(getattr(event, "seq", None)) is not int or event.seq < 1
           or i and event.seq <= events[i - 1].seq
           or not isinstance(getattr(event, "actor", None), Actor)
           or not isinstance(getattr(event, "payload", None), dict)
           or not isinstance(getattr(event, "hash", None), str)
           for i, event in enumerate(events)):
        raise CustomerSourceIndexError("顾客输入索引的事件记录无效。")
    if type(limit) is not int or not 1 <= limit <= MAX_PAGE:
        raise ValueError("顾客输入索引页大小需要在1至50之间。")
    if as_of_seq is None:
        if as_of_hash is not None:
            raise ValueError("顾客输入索引版本需要序号和指纹。")
        anchor = events[-1] if events else None
    else:
        if not _positive(as_of_seq) or not isinstance(as_of_hash, str):
            raise ValueError("顾客输入索引版本无效。")
        anchor = next((e for e in events if e.seq == as_of_seq), None)
        if anchor is None or anchor.hash != as_of_hash:
            raise ValueError("找不到本群顾客输入索引版本。")
    if anchor is None:
        return None
    if before_seq is not None and (not _positive(before_seq) or before_seq > anchor.seq + 1):
        raise ValueError("顾客输入索引起点无效。")
    if before_part is not None and (before_seq is None or type(before_part) is not int or before_part < 0):
        raise ValueError("顾客输入索引分项起点无效。")
    bound = (before_seq, before_part if before_part is not None else 0) if before_seq is not None else (anchor.seq + 1, 0)
    rows = [(e, part) for e in events if e.seq <= anchor.seq for e, part in _source_parts(e) if (e.seq, part) < bound]
    from .team_review_direction import direction_at, effective_objectives
    snapshot = [e for e in events if e.seq <= anchor.seq]
    effective = effective_objectives(snapshot)
    direction = direction_at(snapshot, anchor.seq)
    goal = next((e for e in reversed(snapshot) if e.seq in effective and e.type == "team.objective_updated"
                 and direction is not None and e.seq > direction), None)
    # An unverified current-direction update is not evidence for the previous
    # goal. Only discard a later update when its old run/source is identifiable.
    latest_update = next((e for e in reversed(snapshot) if e.actor == Actor.system('team')
                          and e.type == 'team.objective_updated'
                          and (direction is None or e.seq > direction)), None)
    if latest_update is not None and latest_update.seq not in effective and (goal is None or latest_update.seq > goal.seq):
        runs = [e for e in snapshot if e.actor == Actor.system('team') and e.type == 'team.run_started'
                and e.payload.get('run_id') == latest_update.payload.get('run_id')
                and e.seq < latest_update.seq]
        old = False
        if len(runs) == 1 and direction is not None:
            from .team_review_direction import _recorded_agent_source
            source = next((e for e in snapshot if e.seq == runs[0].payload.get('message_seq')), None)
            credible = bool(source and source.type == 'team.message_received'
                and source.payload.get('recipient') == runs[0].payload.get('role')
                and (source.actor == Actor.human() and source.payload.get('kind') == 'message'
                     or source.actor.kind == 'agent' and _recorded_agent_source(source,
                         [e for e in snapshot if e.seq < runs[0].seq])))
            old = credible and (runs[0].seq < direction or source.actor.kind == 'agent' and source.seq < direction)
        if not old:
            goal = latest_update
    state = _input_state(events, anchor)
    origin = _goal_origin(events, goal, anchor, state=state, member_ids=member_ids)
    source_seq = origin.get("message_seq", origin.get("event_seq"))
    source = next((e for e in events if e.seq == source_seq and _source_text(e) is not None), None)
    refs = preview_refs or {}
    previews = {}

    def preview_fields(event, part):
        if _source_type(event, part) == "legacy_command_instruction":
            return {"preview_unavailable": True, "preview_note": "旧式原话不自动预览，请按需读取。"}
        key = (event.seq, part)
        if key in refs:
            return {"preview_ref": refs[key]}
        if key not in previews:
            previews[key] = _preview(_source_text(event, part))
        return previews[key]

    def build(page):
        objective_source = dict(origin)
        if source is not None:
            objective_source.update({"preview_ref": f"entries[seq={source.seq},part=0]"}
                                    if any(e.seq == source.seq and part == 0 for e, part in page) else preview_fields(source, 0))
        more = len(rows) > len(page)
        return {"as_of_seq": anchor.seq, "as_of_hash": anchor.hash,
                "objective_update_seq": goal.seq if goal else 0, "objective_source": objective_source,
                "acceptance_source": state["acceptance_source"],
                "entries": [{"seq": e.seq, "hash": e.hash, "part": part,
                             "source_type": _source_type(e, part),
                             **({"source_field": "instruction"} if _source_type(e, part) == "legacy_command_instruction"
                                else {"acceptance_index": part - 1} if part else {}), **preview_fields(e, part)} for e, part in page],
                "has_more": more, "next_before_seq": page[-1][0].seq if more and page else None,
                "next_before_part": page[-1][1] if more and page else None,
                "reader": {"name": "read_messages", "scope": "customer_message",
                           "message_seq": "entries[].seq", "expected_hash": "entries[].hash",
                           "acceptance_index": "entries[].acceptance_index（逐项验收）",
                           "source_field": "entries[].source_field（旧式原话）"},
                "index_reader": {"name": "read_messages", "scope": "customer_index",
                                 "as_of_seq": anchor.seq, "as_of_hash": anchor.hash,
                                 "before_seq": "next_before_seq", "before_part": "next_before_part"}, "note": NOTE}

    page = []
    for row in reversed(rows[-limit:]):
        candidate = [*page, row]
        if len(json_utf8({"status": "ok", **build(candidate)})) > INDEX_JSON_BYTES:
            break
        page = candidate
    result = build(page)
    if len(json_utf8({"status": "ok", **result})) > INDEX_JSON_BYTES or rows and not page:
        raise CustomerSourceIndexError("顾客输入索引超过预算，无法安全分页。")
    return result


def read(events, *, message_seq, expected_hash, acceptance_index=None, source_field=None, offset=0, length=TEXT_CHARS):
    if (not _positive(message_seq) or not isinstance(expected_hash, str)
            or acceptance_index is not None and (type(acceptance_index) is not int or acceptance_index < 0)
            or source_field not in (None, "instruction") or source_field is not None and acceptance_index is not None
            or type(offset) is not int or offset < 0 or type(length) is not int or not 1 <= length <= TEXT_CHARS):
        raise ValueError("顾客输入读取需要有效来源、指纹和分页范围。")
    event = next((e for e in events if e.seq == message_seq), None)
    part = (_instruction_part(event) if source_field == "instruction" and event is not None
            else acceptance_index + 1 if acceptance_index is not None else 0)
    if (event is None or source_field == "instruction" and _legacy_instruction(event) is None
            or acceptance_index is not None and _source_type(event, part) == "legacy_command_instruction"
            or _source_text(event, part) is None):
        raise ValueError("找不到本群匹配指纹的顾客输入。请用read_messages的customer_index核对同一条目的seq、hash及来源字段；工具完成回执请用read_receipts的receipt_seq读取。")
    if event is None or event.hash != expected_hash:
        raise ValueError("找不到本群匹配指纹的顾客输入。请用read_messages的customer_index核对同一条目的seq、hash及来源字段；工具完成回执请用read_receipts的receipt_seq读取。")
    text = _source_text(event, part)
    if offset > len(text):
        raise ValueError("顾客输入读取位置超出正文。")
    low, high = offset, min(len(text), offset + length)
    while low < high:
        mid = (low + high + 1) // 2
        if len(json_utf8({"text": text[offset:mid]})) <= TEXT_JSON_BYTES:
            low = mid
        else:
            high = mid - 1
    return {"status": "ok", "message_seq": event.seq, "message_hash": event.hash,
            "source_type": _source_type(event, part),
            **({"source_field": "instruction"} if source_field else
               {"acceptance_index": acceptance_index} if acceptance_index is not None else {}),
            "text": text[offset:low], "offset": offset, "total_chars": len(text),
            "has_more": low < len(text), "next_offset": low if low < len(text) else None,
            "note": NOTE}
