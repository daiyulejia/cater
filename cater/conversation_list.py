"""Durable list removal, preserving audit history and existing project files."""
from .events import Actor, HeadChanged
from .work import WorkConflict


class ConversationList:
    def __init__(self, runtime):
        self.rt = runtime

    def removed(self):
        return {event.task_id: event.seq for event in self.rt.log.iter_events(
            event_types=("task.removed_from_list",)) if event.actor == Actor.human() and event.task_id}

    def is_removed(self, task_id):
        return any(event.actor == Actor.human() for event in self.rt.log.iter_events(
            task_id=task_id, event_types=("task.removed_from_list",)))

    def remove(self, task_id, *, request_id):
        if (not isinstance(request_id, str) or not 8 <= len(request_id) <= 128
                or not request_id.isascii() or not all(c.isalnum() or c in "-_" for c in request_id)):
            raise ValueError("需要有效的删除请求标识。")
        for _ in range(8):
            head = self.rt.log.head_hash()
            existing = None
            for event in self.rt.log.iter_events(event_types=("task.removed_from_list", "task.execution_controlled")):
                if event.actor != Actor.human():
                    continue
                if event.payload.get("request_id") == request_id:
                    if event.task_id != task_id or (event.type == "task.execution_controlled"
                            and event.payload.get("action") != "cancel"):
                        raise WorkConflict("同一删除请求不能更换对话或动作。")
                    if event.type == "task.removed_from_list":
                        return event
                    existing = event
            if existing is not None:
                raise WorkConflict("请求标识已用于执行控制，请使用新的删除请求。")
            if self.rt.task(task_id) is None:
                raise KeyError(task_id)
            prior = self.removed().get(task_id)
            # Cancel and remove are atomic: a hidden task cannot keep launching work.
            entries = ([self.rt.execution_control.entry(task_id, "cancel", request_id=request_id)]
                       if prior is None else [])
            entries.append({"type": "task.removed_from_list", "task_id": task_id, "actor": Actor.human(),
                "payload": {"request_id": request_id, "retention": "local_audit_and_files",
                            **({"previous_removal_seq": prior} if prior is not None else {})}})
            try:
                return self.rt.log.emit_many(entries, expected_head=head)[-1]
            except HeadChanged:
                continue
        raise WorkConflict("对话记录正在更新，请沿用原请求标识重试。")
