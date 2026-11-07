"""Real localhost audit replay for human reconciliation records."""
from pathlib import Path
import unittest
from urllib.request import urlopen

from cater.events import Actor
from cater.runtime import Runtime
from test_runtime import WebExitStack, preserving_temporary_directory, start_test_web


class ReconciliationAuditHTTPTests(unittest.TestCase):
    def test_replay_keeps_full_human_reason_and_hides_untrusted_fields(self):
        with preserving_temporary_directory() as tmp, WebExitStack() as resources:
            db = Path(tmp) / 'audit.db'
            reason = '检查 <script>alert(1)</script>' + '字' * 3900 + '末'
            with Runtime(db) as rt:
                task = rt.create_task('offline audit')
                rt.log.emit(type='team.tool_reconciled', task_id=task, actor=Actor.human(), payload={
                    'started_seq': 1, 'started_hash': 'a' * 64, 'name': 'call_mcp_tool', 'role': 'dev',
                    'run_id': 'run', 'call_id': 'call', 'invocation_stopped': True,
                    'outcome': 'effects_reviewed_no_replay', 'reason': reason,
                    'work_item_id': 'PRIVATE_WORK', 'arguments': {'secret': 'PRIVATE_ARGUMENT'}, 'extra': 'PRIVATE_EXTRA'})
                rt.log.emit(type='team.tool_reconciled', task_id=task, actor=Actor.agent('fake', 'fake'), payload={
                    'reason': 'PRIVATE_FAKE_REASON', 'arguments': {'secret': 'PRIVATE_FAKE_ARGUMENT'}})
            base = start_test_web(self, str(db), resources=resources, recover_on_startup=False)
            with urlopen(f'{base}/tasks/{task}', timeout=5) as response:
                body = response.read().decode('utf-8')
            self.assertIn('操作核对记录', body)
            self.assertIn('操作者说明，未经独立验证', body)
            self.assertIn('检查 &lt;script&gt;alert(1)&lt;/script&gt;' + '字' * 3900 + '末', body)
            self.assertIn('记录来源待核对', body)
            for secret in ('PRIVATE_WORK', 'PRIVATE_ARGUMENT', 'PRIVATE_EXTRA', 'PRIVATE_FAKE_REASON', 'PRIVATE_FAKE_ARGUMENT'):
                self.assertNotIn(secret, body)


if __name__ == '__main__':
    unittest.main()
