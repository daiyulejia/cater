import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import urlopen

from cater.anthropic import AnthropicMessagesDriver
from cater.drivers import DriverProtocolError, DriverResponse, OpenAICompatibleDriver
from cater.runtime import Runtime
from cater.work import WorkConflict
from test_runtime import start_test_web


class ExternalFailurePrivacyTests(unittest.TestCase):
    def test_text_metadata_subclass_key_is_not_silently_dropped(self):
        class Key(str):
            armed = False
            def __hash__(self):
                if Key.armed:
                    raise AssertionError('unexpected key hash')
                return str.__hash__(self)
        class Driver:
            def complete(self, **kwargs):
                Key.armed = False
                meta = {Key('contained'): True}
                Key.armed = True
                return DriverResponse('must not be used', 100, meta)
        driver = Driver()
        with Runtime(drivers={'product': driver}, circuit_breaker_threshold=1) as rt:
            task = rt.create_task('Text metadata key')
            role = rt._role_by_id('product')
            response = rt._complete_with_containment(task_id=task, task=rt.task(task), role=role, driver=driver, prompt='test')
            failure, = rt.log.iter_events(task_id=task, event_types=('driver.failed',))
            self.assertTrue(response.meta['contained'])
            self.assertNotIn('must not be used', response.text)
            self.assertTrue(failure.payload['circuit_open'])

    def test_replaced_class_attribute_dispatch_is_not_trusted(self):
        from cater.drivers import trusted_driver_method
        driver = OpenAICompatibleDriver('product', endpoint='https://example.test', model='fixture', api_key_env='TEST_PRIVACY_KEY')
        original = OpenAICompatibleDriver.complete
        self.assertTrue(trusted_driver_method(driver, 'complete', original))
        def fake_complete(**kwargs):
            raise RuntimeError('private fixture')
        def dispatch(instance, name):
            if name == 'complete':
                return fake_complete
            return object.__getattribute__(instance, name)
        with patch.object(OpenAICompatibleDriver, '__getattribute__', dispatch), Runtime(
                drivers={'product': driver}, circuit_breaker_threshold=1, text_retry=True) as rt:
            self.assertFalse(trusted_driver_method(driver, 'complete', original))
            task = rt.create_task('Dynamic dispatch')
            role = rt._role_by_id('product')
            response = rt._complete_with_containment(task_id=task, task=rt.task(task), role=role, driver=driver, prompt='test')
            failure, = rt.log.iter_events(task_id=task, event_types=('driver.failed',))
            self.assertTrue(failure.payload['circuit_open'])
            self.assertTrue(response.meta['contained'])
            self.assertNotIn('private fixture', response.text + json.dumps(failure.payload))

    def test_custom_response_fields_are_read_once(self):
        class ChangingResponse(DriverResponse):
            def __getattribute__(self, name):
                if name in ('text', 'tokens'):
                    counts = object.__getattribute__(self, 'reads')
                    counts[name] = counts.get(name, 0) + 1
                    if counts[name] > 1:
                        raise WorkConflict('private fixture')
                return super().__getattribute__(name)
        class Driver:
            def complete(self, **kwargs):
                result = ChangingResponse('valid', 1, {'token_usage': 'reported'})
                object.__setattr__(result, 'reads', {})
                self.response = result
                return result
        driver = Driver()
        with Runtime(drivers={'product': driver}, circuit_breaker_threshold=1) as rt:
            task = rt.create_task('Response snapshots')
            role = rt._role_by_id('product')
            response = rt._complete_with_containment(task_id=task, task=rt.task(task), role=role, driver=driver, prompt='test')
            self.assertEqual((response.text, response.tokens), ('valid', 1))
            self.assertEqual(driver.response.reads, {'text': 1, 'tokens': 1})
            self.assertFalse(list(rt.log.iter_events(task_id=task, event_types=('driver.failed',))))

    def test_native_driver_replaced_callable_identity_cannot_escape(self):
        class Callable:
            @property
            def __func__(self):
                raise RuntimeError('private fixture')
            def __call__(self, **kwargs):
                raise RuntimeError('private fixture')
        for cls in (OpenAICompatibleDriver, AnthropicMessagesDriver):
            with self.subTest(driver=cls.__name__):
                driver = cls('product', endpoint='https://example.test', model='fixture', api_key_env='TEST_PRIVACY_KEY', token_budget=100)
                driver.complete = Callable()
                with Runtime(drivers={'product': driver}, circuit_breaker_threshold=1, text_retry=True) as rt:
                    task = rt.create_task('Identity boundary')
                    role = rt._role_by_id('product')
                    response = rt._complete_with_containment(task_id=task, task=rt.task(task), role=role, driver=driver, prompt='test')
                    failure, = rt.log.iter_events(task_id=task, event_types=('driver.failed',))
                    self.assertTrue(failure.payload['circuit_open'])
                    self.assertTrue(response.meta['contained'])
                    self.assertNotIn('private fixture', response.text + json.dumps(failure.payload))
                    self.assertFalse(list(rt.log.iter_events(task_id=task, event_types=('driver.retry_scheduled',))))

    def test_custom_response_with_non_json_metadata_keeps_output(self):
        class Driver:
            def complete(self, **kwargs):
                from datetime import datetime
                return DriverResponse('valid output', 1, {'token_usage': 'reported', 'observed_at': datetime(2026, 1, 1)})
        driver = Driver()
        with Runtime(drivers={'product': driver}) as rt:
            task = rt.create_task('Metadata compatibility')
            role = rt._role_by_id('product')
            response = rt._complete_with_containment(task_id=task, task=rt.task(task), role=role, driver=driver, prompt='test')
            self.assertEqual(response.text, 'valid output')
            self.assertEqual(response.meta['token_usage'], 'reported')
            self.assertFalse(list(rt.log.iter_events(task_id=task, event_types=('driver.failed',))))

    def test_budget_property_attribute_error_is_failure(self):
        class Driver:
            @property
            def token_budget(self):
                raise AttributeError('private fixture')
            def complete(self, **kwargs):
                raise AssertionError('must not invoke')
        driver = Driver()
        with Runtime(drivers={'product': driver}, circuit_breaker_threshold=1) as rt:
            task = rt.create_task('Budget property')
            role = rt._role_by_id('product')
            response = rt._complete_with_containment(task_id=task, task=rt.task(task), role=role, driver=driver, prompt='test')
            failure, = rt.log.iter_events(task_id=task, event_types=('driver.failed',))
            self.assertTrue(failure.payload['circuit_open'])
            self.assertFalse(response.meta['invocation_started'])

    def test_budget_attribute_failure_counts_toward_circuit(self):
        class Driver:
            @property
            def token_budget(self):
                raise WorkConflict('private fixture')
            def complete(self, **kwargs):
                raise AssertionError('must not invoke')
        driver = Driver()
        with Runtime(drivers={'product': driver}, circuit_breaker_threshold=1) as rt:
            task = rt.create_task('Budget attribute boundary')
            role = rt._role_by_id('product')
            response = rt._complete_with_containment(task_id=task, task=rt.task(task), role=role, driver=driver, prompt='test')
            failure, = rt.log.iter_events(task_id=task, event_types=('driver.failed',))
            self.assertTrue(failure.payload['circuit_open'])
            self.assertFalse(response.meta['invocation_started'])
            self.assertNotIn('private fixture', response.text + json.dumps(failure.payload))

    def test_hostile_exception_attributes_cannot_escape_failure_record(self):
        class HostileError(Exception):
            def __getattribute__(self, name):
                if name == '__dict__':
                    raise RuntimeError('private fixture')
                return super().__getattribute__(name)
        class Driver:
            def complete(self, **kwargs):
                raise HostileError('private fixture')
        driver = Driver()
        with Runtime(drivers={'product': driver}, circuit_breaker_threshold=1) as rt:
            task = rt.create_task('Exception boundary')
            role = rt._role_by_id('product')
            response = rt._complete_with_containment(task_id=task, task=rt.task(task), role=role, driver=driver, prompt='test')
            failure, = rt.log.iter_events(task_id=task, event_types=('driver.failed',))
            self.assertTrue(failure.payload['circuit_open'])
            self.assertNotIn('private fixture', response.text + json.dumps(failure.payload))

    def test_custom_protocol_error_cannot_inject_usage_objects(self):
        class HostileUsage(dict):
            def get(self, *args):
                raise WorkConflict('private fixture')
        class Driver:
            def complete(self, **kwargs):
                error = DriverProtocolError('private fixture')
                error._tool_response_usage = HostileUsage()
                raise error
        driver = Driver()
        with Runtime(drivers={'product': driver}, circuit_breaker_threshold=1) as rt:
            task = rt.create_task('Protocol error boundary')
            role = rt._role_by_id('product')
            response = rt._complete_with_containment(task_id=task, task=rt.task(task), role=role, driver=driver, prompt='test')
            failure, = rt.log.iter_events(task_id=task, event_types=('driver.failed',))
            self.assertTrue(failure.payload['circuit_open'])
            self.assertNotIn('private fixture', response.text + json.dumps(failure.payload))

    def test_custom_response_subclass_and_bad_property_circuit(self):
        class NormalResponse(DriverResponse):
            pass
        class HostileResponse(DriverResponse):
            def __getattribute__(self, name):
                if name == 'text':
                    raise WorkConflict('private fixture')
                return super().__getattribute__(name)
        class Driver:
            response = NormalResponse('valid', 3, {'token_usage': 'reported'})
            def complete(self, **kwargs):
                return self.response
        driver = Driver()
        with Runtime(drivers={'product': driver}, circuit_breaker_threshold=2) as rt:
            task = rt.create_task('Response boundary')
            role = rt._role_by_id('product')
            def invoke():
                return rt._complete_with_containment(task_id=task, task=rt.task(task), role=role, driver=driver, prompt='test')
            self.assertEqual(invoke().text, 'valid')
            driver.response = HostileResponse('ignored', 0, {})
            for _ in range(2):
                self.assertTrue(invoke().meta['contained'])
            self.assertTrue(rt._is_circuit_open('product'))
            failures = list(rt.log.iter_events(task_id=task, event_types=('driver.failed',)))
            self.assertEqual(len(failures), 2)
            self.assertNotIn('private fixture', json.dumps([e.payload for e in failures]))

    def test_native_http_status_is_retained_without_remote_text(self):
        for status in (429, 502, 403):
            with self.subTest(status=status):
                self._check_native_http_status(status)

    def _check_native_http_status(self, status):
        driver = OpenAICompatibleDriver('product', endpoint='https://example.test', model='fixture', api_key_env='TEST_PRIVACY_KEY')
        with Runtime(drivers={'product': driver}) as rt, patch.dict('os.environ', {'TEST_PRIVACY_KEY': 'fixture'}), patch(
                'cater.drivers._open_api_request', side_effect=HTTPError('https://private.test/token', status, 'secret remote body', {}, None)):
            task = rt.create_task('Rate limit')
            role = rt._role_by_id('product')
            reply = rt._complete_with_containment(task_id=task, task=rt.task(task), role=role, driver=driver, prompt='test')
            failure, = rt.log.iter_events(task_id=task, event_types=('driver.failed',))
            self.assertIn(f'HTTP {status}', failure.payload['message'])
            self.assertIn(f'HTTP {status}', reply.text)
            self.assertNotIn('private.test', json.dumps(failure.payload) + reply.text)
            self.assertNotIn('secret remote body', json.dumps(failure.payload) + reply.text)

    def test_gateway_event_reply_and_api_do_not_expose_exception(self):
        secret = 'C:/private/path TOKEN-secret remote-body'
        class Driver:
            calls = 0
            def complete(self, **kwargs):
                self.calls += 1
                raise RuntimeError(secret)
        driver = Driver()
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / 'events.db')
            with Runtime(db, drivers={'product': driver}, circuit_breaker_threshold=1) as rt:
                task = rt.create_task('Privacy regression')
                role = rt._role_by_id('product')
                reply = rt._complete_with_containment(task_id=task, task=rt.task(task), role=role, driver=driver, prompt='test')
                self.assertNotIn(secret, reply.text)
                self.assertIn('Driver invocation failed', reply.text)
                failure, = rt.log.iter_events(task_id=task, event_types=('driver.failed',))
                self.assertEqual(failure.payload['error_type'], 'ExternalDriverError')
                self.assertNotIn(secret, json.dumps(failure.payload))
                self.assertTrue(failure.payload['circuit_open'])
                rt._complete_with_containment(task_id=task, task=rt.task(task), role=role, driver=driver, prompt='again')
                self.assertEqual(driver.calls, 1)
                self.assertEqual([e.payload['attempt'] for e in rt.log.iter_events(task_id=task, event_types=('driver.failed',))], [1, 0])
            base = start_test_web(self, db)
            with urlopen(f'{base}/api/tasks/{task}', timeout=5) as response:
                body = response.read().decode('utf-8')
            self.assertNotIn(secret, body)
            self.assertIn('Driver invocation failed', body)


if __name__ == '__main__':
    unittest.main()
