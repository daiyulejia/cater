"""Durable finish receipt recovery after an actual host process exit."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from cater.events import Actor
from cater.runtime import Runtime
from cater.team import HOST
from cater.team_recovery import _unsettled_delivery


class DeliveryCrashRecoveryTests(unittest.TestCase):
    def crash(self, mode, *, before=False, objective=False):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        env = dict(os.environ)
        env['PYTHONPATH'] = str(Path.cwd())
        process = subprocess.run([sys.executable, 'tests/support/delivery_crash_worker.py', str(root), mode]
                                 + (['before'] if before else ['objective'] if objective else []),
                                 env=env, capture_output=True, timeout=40)
        self.assertEqual(process.returncode, 74 if objective else 73, process.stderr.decode(errors='replace'))
        task = (root / 'task.txt').read_text(encoding='utf-8')
        return root, task

    def open(self, root):
        class NoCalls:
            def tool_turn(self, **kwargs):
                raise AssertionError('Recovery must not invoke a driver')
        rt = Runtime(root / 'team.db', roles_path=root / 'roles.json', drivers={'offline': NoCalls()})
        self.addCleanup(rt.close)
        return rt

    def test_real_process_exit_and_repeated_restart(self):
        for mode in ('openai', 'anthropic', 'qoder'):
            with self.subTest(mode=mode):
                root, task = self.crash(mode)
                rt = self.open(root)
                events = rt.log.read(task_id=task)
                start, = [e for e in events if e.type == 'team.run_started']
                delivery, = [e for e in events if e.type == 'team.objective_completed']
                receipt, = [e for e in events if e.type == 'team.tool_finished' and e.payload.get('name') == 'finish_work']
                self.assertEqual(receipt.payload['result']['status'], 'ok')
                self.assertFalse([e for e in events if e.type in {'msg.said', 'team.run_finished'}])
                self.assertEqual(_unsettled_delivery(rt.team, task, start, events), delivery)
                self.assertEqual(rt.team.recovery.prepare(task), [])
                events = rt.log.read(task_id=task)
                reply, = [e for e in events if e.type == 'msg.said' and e.payload.get('delivery_seq') == delivery.seq]
                self.assertTrue(reply.payload['text'].startswith(delivery.payload['summary']))
                finished, = [e for e in events if e.type == 'team.run_finished']
                self.assertEqual(finished.payload['recovered_customer_reply_seq'], reply.seq)
                if mode == 'qoder':
                    self.assertIn('清理尚未确认', reply.payload['text'])
                    self.assertTrue(rt.team.external_runs.pending(task))
                self.assertTrue(rt.verify_chain().ok)
                rt.close()
                for _ in range(2):
                    again = self.open(root)
                    before = again.log.read(task_id=task)
                    self.assertEqual(again.team.recovery.prepare(task), [])
                    self.assertEqual(again.log.read(task_id=task), before)
                    again.close()

    def test_cross_role_input_after_delivery_preserves_both(self):
        root, task = self.crash('openai')
        rt = self.open(root)
        worker = rt.worker_roles[0]
        new = rt.team.receive(task, f'@{worker.name} New customer question')
        self.assertEqual(rt.team.recovery.prepare(task), [])
        events = rt.log.read(task_id=task)
        reply, = [e for e in events if e.type == 'msg.said' and e.payload.get('delivery_seq')]
        delivery, = [e for e in events if e.type == 'team.objective_completed']
        self.assertEqual(reply.payload['delivery_seq'], delivery.seq)
        self.assertLess(delivery.seq, new.seq)
        self.assertLess(new.seq, reply.seq)
        finish, = [e for e in events if e.type == 'team.run_finished']
        self.assertEqual(finish.payload['status'], 'completed')
        snapshot = rt.log.read(task_id=task)
        self.assertEqual(rt.team.recovery.prepare(task), [])
        self.assertEqual(rt.log.read(task_id=task), snapshot)
        claimed = rt.team._claim(task, human_only=True)
        self.assertEqual(claimed[0].seq, new.seq)

    def test_input_before_delivery_is_not_recovered_as_old_answer(self):
        root, task = self.crash('openai', before=True)
        rt = self.open(root)
        events = rt.log.read(task_id=task)
        start, = [e for e in events if e.type == 'team.run_started']
        self.assertIsNone(_unsettled_delivery(rt.team, task, start, events))
        self.assertEqual(rt.team.recovery.prepare(task), [])
        self.assertFalse([e for e in rt.log.read(task_id=task) if e.type == 'msg.said'])
        new = [e for e in events if e.type == 'team.message_received' and e.seq > start.seq][0]
        claimed = rt.team._claim(task, human_only=True)
        self.assertEqual(claimed[0].seq, new.seq)

    def test_conflicting_receipts_block_recovery(self):
        root, task = self.crash('openai')
        rt = self.open(root)
        events = rt.log.read(task_id=task)
        start, = [e for e in events if e.type == 'team.run_started']
        receipt, = [e for e in events if e.type == 'team.tool_finished' and e.payload.get('name') == 'finish_work']
        rt.log.emit(type='team.tool_finished', task_id=task, actor=HOST,
                    payload={**receipt.payload, 'result': {'status': 'error'}})
        self.assertIsNone(_unsettled_delivery(rt.team, task, start, rt.log.read(task_id=task)))
        self.assertEqual(rt.team.recovery.prepare(task), [])
        self.assertFalse([e for e in rt.log.read(task_id=task) if e.type == 'msg.said'])
        finish, = [e for e in rt.log.read(task_id=task) if e.type == 'team.run_finished']
        self.assertEqual(finish.payload['status'], 'interrupted')

    def test_objective_change_before_tool_blocks_old_delivery(self):
        root, task = self.crash('openai', objective=True)
        rt = self.open(root)
        rt.team.recovery.prepare(task)
        events = rt.log.read(task_id=task)
        self.assertFalse([e for e in events if e.type == 'team.objective_completed'])
        self.assertFalse([e for e in events if e.type == 'msg.said' and e.payload.get('delivery_seq')])
        self.assertEqual(rt.team.view(task)['objective'], 'Changed goal')

    def test_recovery_reply_and_terminal_roll_back_together(self):
        root, task = self.crash('openai')
        rt = self.open(root)
        emit = rt.log.emit
        def fail(**kwargs):
            if kwargs['type'] == 'team.run_finished':
                raise OSError('injected settlement failure')
            return emit(**kwargs)
        with patch.object(rt.log, 'emit', side_effect=fail):
            with self.assertRaises(OSError):
                rt.team.recovery.prepare(task)
        self.assertFalse([e for e in rt.log.read(task_id=task) if e.type in {'msg.said', 'team.run_finished'}])
        self.assertEqual(rt.team.recovery.prepare(task), [])
        self.assertEqual(len([e for e in rt.log.read(task_id=task) if e.type == 'msg.said']), 1)

    def test_new_objective_and_bad_receipt_do_not_authorize_delivery(self):
        for defect in ('objective', 'receipt_actor', 'receipt_run', 'receipt_status', 'tool_contract', 'summary',
                       'missing_source', 'duplicate_receipt'):
            with self.subTest(defect=defect):
                root, task = self.crash('openai')
                rt = self.open(root)
                events = rt.log.read(task_id=task)
                start, = [e for e in events if e.type == 'team.run_started']
                from dataclasses import replace
                altered = []
                for e in events:
                    if defect == 'missing_source' and e.seq == start.payload['message_seq']:
                        continue
                    if e.type == 'team.tool_finished' and e.payload.get('name') == 'finish_work':
                        payload = dict(e.payload)
                        if defect == 'receipt_run': payload['run_id'] = 'other'
                        if defect == 'receipt_status': payload['result'] = {'status': 'error'}
                        e = replace(e, payload=payload, actor=Actor.human() if defect == 'receipt_actor' else e.actor)
                    if e.type == 'team.tool_started' and e.payload.get('name') == 'finish_work' and defect == 'tool_contract':
                        e = replace(e, payload={**e.payload, 'execution_contract': {'version': 0}})
                    if e.type == 'team.objective_completed' and defect == 'summary':
                        e = replace(e, payload={**e.payload, 'summary': 'Different answer'})
                    altered.append(e)
                    if defect == 'duplicate_receipt' and e.type == 'team.tool_finished' and e.payload.get('name') == 'finish_work':
                        altered.append(replace(e, seq=e.seq + 10000))
                if defect == 'objective':
                    rt.log.emit(type='team.objective_updated', task_id=task, actor=HOST, payload={'goal': 'New goal'})
                    altered = rt.log.read(task_id=task)
                self.assertIsNone(_unsettled_delivery(rt.team, task, start, altered))
                if defect == 'objective':
                    rt.team.recovery.prepare(task)
                    self.assertFalse([e for e in rt.log.read(task_id=task)
                                      if e.type == 'msg.said' and e.payload.get('delivery_seq')])


if __name__ == '__main__':
    unittest.main()
