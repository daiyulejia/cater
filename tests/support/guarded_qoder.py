"""Portable bounded Qoder adapter for offline supervision tests."""
import copy
import threading
import time

from cater.external_agent import QoderAcpDriver
from cater.work import WorkConflict


ALLOWED = frozenset({'wait_for_reply', 'delegate', 'return_work', 'send_message',
    'read_checkpoint', 'read_messages', 'reply_to_customer', 'workspace_list',
    'workspace_search', 'workspace_read', 'workspace_write', 'publish_workspace_file',
    'publish_artifact', 'request_review', 'record_review', 'finish_work'})


class GuardedQoder(QoderAcpDriver):
    def __init__(self, executable, model, *, max_starts, seconds, timeout):
        super().__init__('offline_guarded', executable, model, timeout=timeout,
                         max_run_seconds=timeout, tool_surface='all')
        self.limit = max_starts
        self.deadline = time.monotonic() + seconds
        self.starts = 0
        self.lock = threading.Lock()

    def check_deadline(self):
        if time.monotonic() >= self.deadline:
            raise WorkConflict('offline shared deadline reached')

    def agent_run(self, *, cwd, tools, execute, check_current, on_update):
        with self.lock:
            self.check_deadline()
            if self.starts >= self.limit:
                raise WorkConflict('offline shared start limit reached')
            remaining = self.deadline - time.monotonic()
            self.starts += 1
            bounded = copy.copy(self)
            bounded.timeout = min(self.timeout, remaining)
            bounded.max_run_seconds = min(self.max_run_seconds, remaining)
        offered = [copy.deepcopy(tool) for tool in tools if tool.get('name') in ALLOWED]

        def checked(*args, **kwargs):
            self.check_deadline()
            return check_current(*args, **kwargs)

        def guarded(call):
            self.check_deadline()
            if call.name not in ALLOWED:
                return {'status': 'error', 'error': 'offline tool denied'}
            return execute(call)

        return super(GuardedQoder, bounded).agent_run(cwd=cwd, tools=offered, execute=guarded,
                                                     check_current=checked, on_update=on_update)
