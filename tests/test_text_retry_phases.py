import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError

from cater.anthropic import AnthropicMessagesDriver
from cater.drivers import OpenAICompatibleDriver
from cater.events import EventLog
from cater.planning import Planner
from cater.runtime import Runtime
from test_work import WorkDriver, item
import test_browser_repair_pipeline as browser_fixture
import test_delivery_review_recovery as delivery_fixture
import test_planning_recovery as planning_fixture


class TextRetryPhaseTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch('cater.text_retry.RETRY_DELAY', 0.0))

    def test_planning_and_final_review_keep_one_owner_across_retry(self):
        for phase in ('planning', 'delivery'):
            for adapter in (AnthropicMessagesDriver, OpenAICompatibleDriver):
                with self.subTest(phase=phase, adapter=adapter.__name__):
                    fixture = (planning_fixture.PlanningRecoveryTests() if phase == 'planning' else
                               delivery_fixture.DeliveryReviewRecoveryTests())
                    fixture.setUp()
                    try:
                        rt, task, _ = fixture.runtime(adapter)
                        rt.text_retry = True
                        response = (fixture.success(rt, task, adapter) if phase == 'planning' else fixture.response(rt, task, adapter))
                        clock = SimpleNamespace(time=lambda: fixture.now, monotonic=time.monotonic, sleep=time.sleep)
                        with patch('cater.text_retry.time', clock), patch(fixture.transport(adapter), side_effect=[
                                HTTPError('https://fixture.invalid', 502, 'fixture', {}, None), response]) as opened:
                            result = Planner(rt).run(task) if phase == 'planning' else rt.delivery.run(task)
                        self.assertEqual(opened.call_count, 2)
                        self.assertTrue(result['installed'] if phase == 'planning' else result['status'] == 'accepted')
                        starts = list(rt.log.iter_events(task_id=task, event_types=(
                            'planning.started' if phase == 'planning' else 'delivery.review_started',)))
                        self.assertEqual(len(starts), 1)
                        finished, = list(rt.log.iter_events(task_id=task, event_types=('driver.retry_finished',)))
                        self.assertEqual(finished.payload['status'], 'succeeded')
                        self.assertTrue(rt.verify_chain().ok)
                    finally:
                        fixture.doCleanups()

    def test_browser_test_planning_recovers_then_executes_and_hands_off(self):
        fixture = browser_fixture.BrowserRepairPipelineTests()
        fixture.setUp()
        try:
            fixture.rt.text_retry = True
            failed = False
            def respond(request, **kwargs):
                nonlocal failed
                prompt = json.loads(request.data)['messages'][0]['content']
                if not failed and 'Current check contract (untrusted reference data):' in prompt:
                    failed = True
                    raise HTTPError('https://fixture.invalid', 502, 'fixture', {}, None)
                return fixture.respond(request, **kwargs)
            fixture.transport.side_effect = respond
            fixture.rt.collaborate(fixture.task)
            self.assertTrue(failed)
            self.assertEqual(fixture.rows()['page']['status'], 'accepted')
            self.assertTrue(fixture.calls_for('author:downstream'))
            self.assertEqual(len(fixture.executions), 2)
            started, = fixture.events('driver.retry_scheduled')
            self.assertEqual(started.payload['phase'], 'browser_check_plan')
            self.assertEqual(fixture.events('driver.retry_finished')[-1].payload['status'], 'succeeded')
        finally:
            fixture.doCleanups()

    def test_web_wait_does_not_lock_out_messages_or_pause_on_another_connection(self):
        with tempfile.TemporaryDirectory() as temporary:
            db = Path(temporary) / 'waiting.db'
            api = AnthropicMessagesDriver('dev', endpoint='https://fixture.invalid/messages', model='fixture',
                                          api_key_env='CATER_RETRY_TEST')
            drivers = {'dev': api, 'test': WorkDriver()}
            with Runtime(db, drivers=drivers) as rt:
                task = rt.create_task('A cancellable background task')
                rt.plan_work(task, [item('draft')])
            waiting, errors = threading.Event(), []
            emit = EventLog.emit
            def notice(log, **kwargs):
                event = emit(log, **kwargs)
                if event.type == 'driver.retry_scheduled':
                    waiting.set()
                return event
            def execute():
                try:
                    with Runtime(db, drivers=drivers, text_retry=True) as rt:
                        rt.collaborate(task)
                except Exception as exc:
                    errors.append(repr(exc))
            with patch.dict('os.environ', {'CATER_RETRY_TEST': 'fixture-key'}), \
                    patch('cater.text_retry.RETRY_DELAY', 2.0), patch.object(EventLog, 'emit', notice), \
                    patch('cater.anthropic._open_api_request', side_effect=TimeoutError('fixture')) as opened:
                worker = threading.Thread(target=execute)
                worker.start()
                try:
                    self.assertTrue(waiting.wait(5), errors)
                    with Runtime(db, drivers=drivers) as other:
                        # This separate connection must be able to commit while the worker waits.
                        other.command(task, '进度怎么样？')
                        other.execution_control.change(task, 'pause', request_id='pause-retry-from-another-thread')
                finally:
                    worker.join(10)
                self.assertFalse(worker.is_alive())
                self.assertEqual(errors, [])
                opened.assert_called_once()
            with Runtime(db, drivers=drivers) as rt:
                finished, = list(rt.log.iter_events(task_id=task, event_types=('driver.retry_finished',)))
                self.assertEqual(finished.payload['status'], 'cancelled')
                self.assertTrue(rt.verify_chain().ok)
