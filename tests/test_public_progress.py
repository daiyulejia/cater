"""Public observations stay live, bounded and separate from work receipts."""
from contextlib import contextmanager
import http.client
from http.server import ThreadingHTTPServer
import json
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from cater.events import Actor
from cater.public_progress import MAX_RUNS, MAX_TEXT, PublicProgress
from cater.runtime import Runtime
from cater.web import ActivityRunner, make_handler
import test_team_external as fixture


def attempt(seq=3, task='task', role='dev'):
    return SimpleNamespace(task_id=task, actor=Actor.system('team'), type='team.external_run_started', seq=seq,
        payload={'run_started_seq': seq - 1, 'role': role, 'run_id': f'run-{seq}', 'message_seq': 1})


def chunk(text, kind='agent_message_chunk'):
    return {'sessionUpdate': kind, 'content': {'type': 'text', 'text': text}}


class PublicProgressBufferTests(unittest.TestCase):
    def test_only_public_text_is_kept_and_nothing_survives_activity_end(self):
        progress = PublicProgress()
        with progress.capture('task', 'activity') as observe:
            for kind in ('agent_thought_chunk', 'tool_call', 'tool_call_update', 'plan'):
                observe(attempt(), 0, chunk('private-or-tool-data', kind))
            for update in ({}, {'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'image', 'text': 'image'}},
                           chunk('\ud800'), chunk('')):
                observe(attempt(), 0, update)
            self.assertEqual(progress.snapshot('task', 'activity'), [])
            observe(attempt(), 0, chunk('正在核对需求'))
            observe(attempt(), 0, chunk('，再保存文件。'))
            row, = progress.snapshot('task', 'activity')
            self.assertEqual(row['text'], '正在核对需求，再保存文件。')
            self.assertFalse(row['truncated'])
            self.assertNotIn('observed', row)
            self.assertEqual(progress.snapshot('other', 'activity'), [])
            self.assertEqual(progress.snapshot('task', 'other'), [])
        observe(attempt(), 0, chunk('late'))
        self.assertEqual(progress.snapshot('task', 'activity'), [])

    def test_text_and_concurrent_run_bounds_do_not_grow_with_chunks(self):
        progress = PublicProgress()
        with progress.capture('task', 'activity') as observe:
            for seq in range(3, MAX_RUNS + 8):
                observe(attempt(seq), 0, chunk('月' * (MAX_TEXT + 500)))
            rows = progress.snapshot('task', 'activity')
            self.assertEqual(len(rows), MAX_RUNS)
            self.assertTrue(all(row['text'] == '月' * MAX_TEXT and row['truncated'] for row in rows))
            observe(attempt(MAX_RUNS + 7), 0, chunk('尾'))
            self.assertEqual(progress.snapshot('task', 'activity')[-1]['text'], '月' * (MAX_TEXT - 1) + '尾')

    def test_clock_expiry_goal_change_and_run_close(self):
        progress = PublicProgress()
        with patch('cater.public_progress.time.monotonic', return_value=100) as clock:
            with progress.capture('task', 'activity') as observe:
                observe(attempt(), 0, chunk('旧目标'))
                clock.return_value = 102
                self.assertEqual(progress.snapshot('task', 'activity')[0]['age_ms'], 2000)
                observe(attempt(), 9, chunk('新目标'))
                self.assertEqual(progress.snapshot('task', 'activity')[0]['text'], '新目标')
                clock.return_value = 162
                self.assertEqual(progress.snapshot('task', 'activity'), [])
                observe(attempt(), 9, chunk('更新'))
                observe(attempt(), 9, None)
                self.assertEqual(progress.snapshot('task', 'activity'), [])

    def test_late_observer_and_finalizer_cannot_replace_reused_channel(self):
        progress = PublicProgress()
        previous = progress.capture('task', 'activity')
        old = previous.__enter__()
        with progress.capture('task', 'activity') as current:
            current(attempt(), 0, chunk('当前'))
            old(attempt(), 0, chunk('迟到'))
            previous.__exit__(None, None, None)
            self.assertEqual(progress.snapshot('task', 'activity')[0]['text'], '当前')


class PublicRun(fixture.ScriptedExternalRun):
    def __init__(self, *args, on_update, **kwargs):
        super().__init__(*args, on_update=on_update, **kwargs)
        self.on_update = on_update


class PublicDriver(fixture.ScriptedExternalDriver):
    def agent_run(self, **kwargs):
        run = PublicRun(self, **kwargs)
        self.runs.append(run)
        return run


class PublicProgressIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.fx = fixture.TeamExternalTests()
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        self.driver = PublicDriver()
        self.fx.driver = self.driver
        self.fx.rt.drivers[self.driver.name] = self.driver

    def factory(self):
        return Runtime(self.fx.root / 'team.db', roles_path=self.fx.roles,
            drivers={self.driver.name: self.driver}, close_drivers_on_close=False)

    @contextmanager
    def serve(self, runner):
        with patch('cater.web.ActivityRunner', return_value=runner):
            handler = make_handler(self.fx.root / 'team.db', runtime_factory=self.factory, recover_on_startup=False)
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01})
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)
            self.assertFalse(thread.is_alive())

    def get(self, server, path):
        connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
        try:
            connection.request('GET', path)
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            return json.loads(response.read())
        finally:
            connection.close()

    def test_http_shows_public_text_before_role_returns_and_never_persists_it(self):
        observed, release = threading.Event(), threading.Event()
        fx = self.fx
        text = '公开工作说明，仅供当前观察，不是验收结论。'
        def work(run):
            run.on_update(chunk('do-not-project-private', 'agent_thought_chunk'))
            run.on_update(chunk(text))
            observed.set()
            self.assertTrue(release.wait(8))
            run.call('reply_to_customer', text='已核对，答复完成。')
        self.driver.scripts = {fx.manager: [work]}
        fx.receive('请回答一个问题')
        runner = ActivityRunner(self.factory)
        try:
            with self.serve(runner) as server:
                runner.submit(fx.task, 'run')
                self.assertTrue(observed.wait(5))
                detail = self.get(server, f'/api/tasks/{fx.task}')
                self.assertTrue(detail['activity']['active'])
                row, = detail['public_progress']['runs']
                self.assertEqual(row['text'], text)
                self.assertEqual(detail['public_progress']['activity_id'], detail['activity']['id'])
                self.assertEqual(detail['public_progress']['instance_id'], runner.instance_id)
                before = [(event.seq, event.hash) for event in fx.rt.log.read(task_id=fx.task)]
                summary = self.get(server, '/api/task-summaries')
                self.assertNotIn(text, json.dumps(summary, ensure_ascii=False))
                self.assertEqual(summary['tasks'][0]['activity']['id'], detail['activity']['id'])
                self.assertEqual([(event.seq, event.hash) for event in fx.rt.log.read(task_id=fx.task)], before)
                self.assertNotIn(text, json.dumps([event.payload for event in fx.rt.log.read(task_id=fx.task)], ensure_ascii=False))
                self.assertNotIn('do-not-project-private', json.dumps(detail))
                release.set()
                deadline = time.monotonic() + 5
                while runner.status(fx.task)['active'] and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertFalse(runner.status(fx.task)['active'])
                self.assertEqual(self.get(server, f'/api/tasks/{fx.task}')['public_progress']['runs'], [])
                self.assertEqual(self.driver.errors, [])
        finally:
            release.set()

    def test_observer_failure_does_not_turn_a_real_reply_into_failed_work(self):
        def broken(*args):
            raise OSError('optional observation unavailable')
        self.fx.rt.team.external.progress = broken
        def work(run):
            run.on_update(chunk('公开说明'))
            run.call('reply_to_customer', text='仍然只回答一次。')
        self.driver.scripts = {self.fx.manager: [work]}
        self.fx.receive()
        self.fx.drain()
        self.assertEqual(self.driver.errors, [])
        answers = [event for event in self.fx.events('msg.said') if event.payload.get('customer_reply_tool')]
        self.assertEqual(len(answers), 1)
        self.assertEqual(self.fx.events('team.run_finished')[-1].payload['status'], 'completed')


if __name__ == '__main__':
    unittest.main()
