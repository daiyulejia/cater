"""Persistent cooperative execution controls; existing calls are not forcibly killed."""

from __future__ import annotations

from .events import Actor, HeadChanged
from .work import WorkConflict


class ExecutionControl:
    def __init__(self, runtime):
        self.rt = runtime

    def view(self, task_id):
        events = list(self.rt.log.iter_events(task_id=task_id, event_types=("task.execution_controlled",)))
        event = next((e for e in reversed(events) if e.actor == Actor.human()), None)
        return ({"state": event.payload["state"], "seq": event.seq, "reason": event.payload["reason"]}
                if event else {"state": "running", "seq": None, "reason": ""})

    def entry(self, task_id, action, *, request_id):
        states = {"pause": "paused", "resume": "running", "cancel": "cancelled"}
        reasons = {"pause": "任务已暂停，不再启动下一步；已经发出的调用可能仍会返回，结果会保留。",
                   "resume": "暂停已解除，将从当前有效检查点继续；结果不明的调用仍需核对。",
                   "cancel": "任务已取消，不再启动后续工作；已有成果和在途调用的返回记录会保留。"}
        previous = self.view(task_id)
        if previous["state"] == "cancelled" and action != "cancel":
            raise WorkConflict("这项任务已经取消，需要另开任务继续新的工作。")
        return {"type": "task.execution_controlled", "task_id": task_id, "actor": Actor.human(),
                "payload": {"action": action, "state": states[action], "reason": reasons[action],
                            "scope": self.rt.work.scope(task_id),
                            "previous_seq": previous["seq"], "request_id": request_id}}

    def change(self, task_id, action, *, request_id):
        if not isinstance(action, str) or action not in {"pause", "resume", "cancel"}:
            raise ValueError("执行控制只支持暂停、恢复或取消。")
        if (not isinstance(request_id, str) or not 8 <= len(request_id) <= 128
                or not all(c.isalnum() or c in "-_" for c in request_id)):
            raise ValueError("需要有效的执行控制请求标识。")
        for _ in range(8):
            head = self.rt.log.head_hash()
            for event in self.rt.log.iter_events(event_types=("task.execution_controlled",)):
                if event.actor == Actor.human() and event.payload.get("request_id") == request_id:
                    if event.task_id != task_id or event.payload["action"] != action:
                        raise WorkConflict("同一控制请求不能更换任务或动作。")
                    return event, False
            if not next(self.rt.log.iter_events(task_id=task_id, event_types=("task.created",)), None):
                raise KeyError(task_id)
            previous = self.view(task_id)
            entry = self.entry(task_id, action, request_id=request_id)
            try:
                event = self.rt.log.emit(**entry, expected_head=head)
                return event, previous["state"] != event.payload["state"]
            except HeadChanged:
                continue
        raise WorkConflict("执行状态正在变化，请使用原请求标识重试。")
