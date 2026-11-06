"""Signed history must not be downgraded by Web or concurrent writers."""
import http.client
from http.server import ThreadingHTTPServer
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from cater.events import Actor, AuditSigningMismatch, AuditSigningRequired, EventLog
from cater.review_supervision_candidate import SupervisionLogIntegrityError
from cater.runtime import Runtime
from cater.web import ActivityRunner, make_handler


class SignedLogWriteGuardTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / 'guard.db'
        with Runtime(self.path, audit_key='offline-secret') as rt:
            self.task = rt.create_task('Offline guard fixture')

    def factory(self, *, key=None):
        return Runtime(self.path, audit_key=key)

    def events(self):
        with self.factory(key='offline-secret') as rt:
            self.assertTrue(rt.verify_chain().ok)
            return rt.log.read()

    def test_submit_and_http_entry_reject_without_a_write(self):
        before = self.events()
        runner = ActivityRunner(self.factory)
        with patch.object(runner, '_start') as start:
            with self.assertRaises(AuditSigningRequired):
                runner.submit(self.task, 'run')
            start.assert_not_called()
        self.assertEqual(self.events(), before)
        with patch('cater.web.ActivityRunner', return_value=runner):
            handler = make_handler(self.path, runtime_factory=self.factory)
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01})
        thread.start()
        try:
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
            try:
                connection.request('POST', f'/api/tasks/{self.task}/run', body='{}',
                                   headers={'Content-Type': 'application/json'})
                response = connection.getresponse()
                body = response.read().decode('utf-8')
            finally:
                connection.close()
            self.assertIn(response.status, {400, 409})
            self.assertIn('签名', body)
            self.assertNotIn('offline-secret', body)
            self.assertEqual(self.events(), before)
        finally:
            server.shutdown()
            thread.join(5)
            server.server_close()

    def test_real_submit_rejects_wrong_key_without_scheduling_or_appending(self):
        before = self.events()
        runner = ActivityRunner(lambda: self.factory(key='wrong-offline-key'))
        with patch.object(runner, '_start') as start:
            with self.assertRaises(AuditSigningMismatch) as caught:
                runner.submit(self.task, 'run')
            start.assert_not_called()
        self.assertNotIn('wrong-offline-key', str(caught.exception))
        self.assertNotIn('offline-secret', str(caught.exception))
        self.assertEqual(self.events(), before)

    def test_wrong_key_http_error_is_sanitized(self):
        before = self.events()
        factory = lambda: self.factory(key='wrong-offline-key')
        handler = make_handler(self.path, runtime_factory=factory)
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01})
        thread.start()
        try:
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
            try:
                connection.request('POST', f'/api/tasks/{self.task}/run', body='{}',
                                   headers={'Content-Type': 'application/json'})
                response = connection.getresponse()
                body = response.read().decode('utf-8')
            finally:
                connection.close()
            self.assertIn(response.status, {400, 409})
            self.assertIn('审计', body)
            self.assertNotIn('wrong-offline-key', body)
            self.assertNotIn('offline-secret', body)
            self.assertEqual(self.events(), before)
        finally:
            server.shutdown()
            thread.join(5)
            server.server_close()

    def test_failed_activity_and_followup_stay_blocked_without_terminal_or_replay(self):
        with self.factory(key='offline-secret') as rt:
            for identity in ('parent', 'child'):
                rt.log.emit(type='activity.queued', actor=Actor.system('orchestrator'), task_id=self.task,
                            payload={'activity_id': identity, 'action': 'run',
                                     **({'after_activity_id': 'parent'} if identity == 'child' else {})})
        before = self.events()
        runner = ActivityRunner(self.factory)
        runner._activities[self.task] = {'id': 'parent', 'state': 'running', 'action': 'run',
                                        'follow_up': {'id': 'child', 'action': 'run', 'state': 'queued'}}
        with patch.object(runner, '_start') as start:
            runner._fail_activity(self.task, 'parent', 'run', AuditSigningRequired())
            start.assert_not_called()
        status = runner.status(self.task)
        self.assertEqual(status['state'], 'blocked')
        self.assertEqual(status['follow_up']['state'], 'blocked')
        self.assertIn('签名', status['error'])
        self.assertEqual(self.events(), before)
        reopened = ActivityRunner(self.factory)
        with patch.object(reopened, '_start') as start:
            self.assertEqual(reopened.recover_interrupted(), 0)
            start.assert_not_called()
        self.assertEqual(reopened.status(self.task)['state'], 'blocked')
        self.assertEqual(reopened.status(self.task)['follow_up']['state'], 'blocked')
        self.assertEqual(self.events(), before)

    def test_integrity_error_never_appends_terminal_even_with_signing_key(self):
        runner = ActivityRunner(lambda: self.factory(key='offline-secret'))
        runner._activities[self.task] = {'id': 'parent', 'state': 'running', 'action': 'run'}
        before = self.events()
        runner._fail_activity(self.task, 'parent', 'run', SupervisionLogIntegrityError('日志校验失败'))
        self.assertEqual(runner.status(self.task)['state'], 'blocked')
        self.assertEqual(self.events(), before)

    def test_correct_key_recovery_records_failure_and_no_model(self):
        with self.factory(key='offline-secret') as rt:
            rt.log.emit(type='activity.queued', actor=Actor.system('orchestrator'), task_id=self.task,
                        payload={'activity_id': 'old', 'action': 'run'})
        runner = ActivityRunner(lambda: self.factory(key='offline-secret'))
        with patch.object(runner, '_start') as start:
            self.assertEqual(runner.recover_interrupted(), 1)
            start.assert_not_called()
        self.assertEqual(runner.status(self.task)['state'], 'failed')
        self.assertTrue(any(e.type == 'activity.failed' for e in self.events()))

    def test_corrupt_log_on_restart_blocks_without_appending_even_with_key(self):
        with self.factory(key='offline-secret') as rt:
            queued = rt.log.emit(type='activity.queued', actor=Actor.system('orchestrator'), task_id=self.task,
                                 payload={'activity_id': 'old', 'action': 'run'})
            rt.log.db.execute('DROP TRIGGER events_no_update')
            rt.log.db.execute("UPDATE events SET payload_json='{}' WHERE seq=?", (queued.seq,))
            rt.log.db.commit()
            self.assertFalse(rt.verify_chain().ok)
            before = rt.log.count()
        runner = ActivityRunner(lambda: self.factory(key='offline-secret'))
        with patch.object(runner, '_start') as start:
            self.assertEqual(runner.recover_interrupted(), 0)
            start.assert_not_called()
        # The corrupted queued payload has lost its identity, so no in-memory
        # activity can be reconstructed; no recovery or terminal is written.
        with self.factory(key='offline-secret') as rt:
            self.assertEqual(rt.log.count(), before)

    def test_batch_atomic_default_unsigned_and_rotation(self):
        with self.factory(key='offline-secret') as rt:
            before = rt.log.count()
        unsigned = EventLog(self.path)
        self.addCleanup(unsigned.close)
        with self.assertRaises(AuditSigningRequired):
            unsigned.emit_many([{'type': 'task.progress_reported', 'task_id': self.task,
                                 'actor': Actor.system('team'), 'payload': {}} for _ in range(2)])
        self.assertEqual(unsigned.count(), before)
        rotated = EventLog(self.path, audit_key='new-secret', key_id='new',
                           audit_keys={'local': 'offline-secret'})
        self.addCleanup(rotated.close)
        rotated.emit(type='task.progress_reported', actor=Actor.system('team'), task_id=self.task, payload={})
        self.assertTrue(rotated.verify_chain().ok)
        blank = EventLog(Path(self.path.parent) / 'unsigned.db')
        self.addCleanup(blank.close)
        blank.emit(type='task.progress_reported', actor=Actor.system('team'), payload={})
        self.assertTrue(blank.verify_chain().ok)

    def test_rotation_rejects_missing_historical_key_wrong_key_and_reused_identity(self):
        before = self.events()
        for options in ({'audit_key': 'new-secret', 'key_id': 'new', 'audit_keys': {}},
                        {'audit_key': 'new-secret', 'key_id': 'new', 'audit_keys': {'local': 'wrong-old'}},
                        {'audit_key': 'new-secret', 'key_id': 'local', 'audit_keys': {'local': 'offline-secret'}}):
            with self.subTest(option=tuple(options)):
                log = EventLog(self.path, **options)
                try:
                    with self.assertRaises(AuditSigningMismatch):
                        log.emit(type='task.progress_reported', actor=Actor.system('team'), payload={})
                finally:
                    log.close()
                self.assertEqual(self.events(), before)
        rotated = EventLog(self.path, audit_key='new-secret', key_id='new',
                           audit_keys={'local': 'offline-secret'})
        self.addCleanup(rotated.close)
        rotated.emit(type='task.progress_reported', actor=Actor.system('team'), payload={})
        self.assertTrue(rotated.verify_chain().ok)
        new_only = EventLog(self.path, audit_key='new-secret', key_id='new', audit_keys={})
        self.addCleanup(new_only.close)
        with self.assertRaises(AuditSigningMismatch):
            new_only.emit(type='task.progress_reported', actor=Actor.system('team'), payload={})
        self.assertTrue(rotated.verify_chain().ok)
        back = EventLog(self.path, audit_key='offline-secret', key_id='local', audit_keys={'new': 'new-secret'})
        self.addCleanup(back.close)
        back.emit(type='task.progress_reported', actor=Actor.system('team'), payload={})
        self.assertTrue(back.verify_chain().ok)

    def test_invalid_earlier_signature_is_not_hidden_by_later_valid_signature(self):
        with self.factory(key='offline-secret') as rt:
            rt.log.emit(type='task.progress_reported', actor=Actor.system('team'), task_id=self.task, payload={})
            count = rt.log.count()
            first = rt.log.read()[0]
            rt.log.db.execute('DROP TRIGGER events_no_update')
            rt.log.db.execute('UPDATE events SET signature=? WHERE seq=?', ('0' * 64, first.seq))
            rt.log.db.commit()
            with self.assertRaises(AuditSigningMismatch):
                rt.log.emit(type='task.progress_reported', actor=Actor.system('team'), task_id=self.task, payload={})
            self.assertEqual(rt.log.count(), count)

    def test_unsigned_gap_after_signed_history_prevents_further_append(self):
        with self.factory(key='offline-secret') as rt:
            last = rt.log.emit(type='task.progress_reported', actor=Actor.system('team'), task_id=self.task, payload={})
            count = rt.log.count()
            rt.log.db.execute('DROP TRIGGER events_no_update')
            rt.log.db.execute('UPDATE events SET signature=NULL WHERE seq=?', (last.seq,))
            rt.log.db.commit()
            with self.assertRaises(AuditSigningMismatch):
                rt.log.emit(type='task.progress_reported', actor=Actor.system('team'), task_id=self.task, payload={})
            self.assertEqual(rt.log.count(), count)

    def test_nonrepresentative_bad_signature_requires_full_verification(self):
        with self.factory(key='offline-secret') as rt:
            second = rt.log.emit(type='task.progress_reported', actor=Actor.system('team'), task_id=self.task, payload={})
            rt.log.db.execute('DROP TRIGGER events_no_update')
            rt.log.db.execute('UPDATE events SET signature=? WHERE seq=?', ('0' * 64, second.seq))
            rt.log.db.commit()
            self.assertFalse(rt.verify_chain().ok)
            before = rt.log.count()
            # The first sample remains valid: this narrower admission does not
            # certify the other historical signatures.
            rt.log.emit(type='task.progress_reported', actor=Actor.system('team'), task_id=self.task, payload={})
            self.assertEqual(rt.log.count(), before + 1)
            self.assertFalse(rt.verify_chain().ok)

    def test_mutated_active_key_cannot_bypass_verified_keyring(self):
        with self.factory(key='offline-secret') as rt:
            before = rt.log.count()
            rt.log.audit_key = b'wrong-offline-key'
            with self.assertRaises(AuditSigningMismatch):
                rt.log.emit(type='task.progress_reported', actor=Actor.system('team'), payload={})
            self.assertEqual(rt.log.count(), before)

    def test_batch_uses_signer_snapshot_if_key_changes_during_payload_assembly(self):
        with self.factory(key='offline-secret') as rt:
            original = rt.log._fingerprint
            def change_key(**kwargs):
                digest = original(**kwargs)
                rt.log.audit_key = b'wrong-offline-key'
                return digest
            with patch.object(rt.log, '_fingerprint', side_effect=change_key):
                event = rt.log.emit(type='task.progress_reported', actor=Actor.system('team'), payload={})
            self.assertIsNotNone(event.signature)
        self.assertTrue(self.events())

    def test_indexed_selection_scales_with_key_ids_not_event_count(self):
        with self.factory(key='offline-secret') as rt:
            rt.log.emit_many([{'type': 'task.progress_reported', 'actor': Actor.system('team'), 'payload': {}}
                              for _ in range(100)])
            with patch.object(rt.log, '_sign_with', wraps=rt.log._sign_with) as sign:
                rt.log.emit(type='task.progress_reported', actor=Actor.system('team'), payload={})
            self.assertEqual(sign.call_count, 2)  # one representative and one new signature
            plans = [rt.log.db.execute(sql, args).fetchall() for sql, args in (
                ("EXPLAIN QUERY PLAN SELECT key_id, hash, signature FROM events INDEXED BY events_signed_key_seq "
                 "WHERE signature IS NOT NULL AND signature <> '' AND key_id > ? ORDER BY key_id, seq LIMIT 1", ('a',)),
                ("EXPLAIN QUERY PLAN SELECT 1 FROM events INDEXED BY events_unsigned_seq "
                 "WHERE (signature IS NULL OR signature = '') AND seq > ? LIMIT 1", (1,)))]
            self.assertTrue(all('SEARCH' in ' '.join(str(value) for row in plan for value in row) for plan in plans))

    def test_legacy_null_and_empty_key_ids_use_existing_fallback(self):
        for legacy_id in (None, ''):
            with self.subTest(legacy_id=legacy_id):
                path = Path(self.path.parent) / ('legacy-null.db' if legacy_id is None else 'legacy-empty.db')
                log = EventLog(path, audit_key='offline-secret')
                try:
                    first = log.emit(type='task.progress_reported', actor=Actor.system('team'), payload={})
                    log.db.execute('DROP TRIGGER events_no_update')
                    log.db.execute('UPDATE events SET key_id=? WHERE seq=?', (legacy_id, first.seq))
                    log.db.commit()
                    self.assertTrue(log.verify_chain().ok)
                    log.emit(type='task.progress_reported', actor=Actor.system('team'), payload={})
                    self.assertTrue(log.verify_chain().ok)
                    wrong = EventLog(path, audit_key='wrong-offline-key')
                    try:
                        with self.assertRaises(AuditSigningMismatch):
                            wrong.emit(type='task.progress_reported', actor=Actor.system('team'), payload={})
                    finally:
                        wrong.close()
                finally:
                    log.close()

    def test_missing_required_index_fails_closed(self):
        with self.factory(key='offline-secret') as rt:
            before = rt.log.count()
            rt.log.db.execute('DROP INDEX events_signed_key_seq')
            rt.log.db.commit()
            with self.assertRaises(sqlite3.OperationalError):
                rt.log.emit(type='task.progress_reported', actor=Actor.system('team'), payload={})
            self.assertEqual(rt.log.count(), before)

    def test_signed_commit_races_with_unsigned_append(self):
        fresh = Path(self.path.parent) / 'race.db'
        signed = EventLog(fresh, audit_key='offline-secret')
        self.addCleanup(signed.close)
        unsigned = EventLog(fresh)
        self.addCleanup(unsigned.close)
        unsigned.db.execute('PRAGMA busy_timeout=0')
        with signed.atomic():
            signed.emit(type='task.progress_reported', actor=Actor.system('team'), payload={})
            with self.assertRaises(sqlite3.OperationalError) as busy:
                unsigned.db.execute('BEGIN IMMEDIATE')
            self.assertEqual(busy.exception.sqlite_errorcode, sqlite3.SQLITE_BUSY)
        with self.assertRaises(AuditSigningRequired):
            unsigned.emit(type='task.progress_reported', actor=Actor.system('team'), payload={})
        self.assertEqual(signed.count(), 1)
        self.assertTrue(signed.verify_chain().ok)

    def test_unsigned_first_then_signing_keeps_valid_prefix(self):
        fresh = Path(self.path.parent) / 'prefix.db'
        unsigned = EventLog(fresh)
        self.addCleanup(unsigned.close)
        unsigned.emit(type='task.progress_reported', actor=Actor.system('team'), payload={})
        signed = EventLog(fresh, audit_key='offline-secret')
        self.addCleanup(signed.close)
        signed.emit(type='task.progress_reported', actor=Actor.system('team'), payload={})
        self.assertTrue(signed.verify_chain().ok)
        with self.assertRaises(AuditSigningRequired):
            unsigned.emit(type='task.progress_reported', actor=Actor.system('team'), payload={})

    def test_waiting_unsigned_writer_rechecks_after_signed_commit(self):
        fresh = Path(self.path.parent) / 'concurrent.db'
        signed = EventLog(fresh, audit_key='offline-secret')
        self.addCleanup(signed.close)
        ready, attempt = threading.Event(), threading.Event()
        errors = []
        def write_unsigned():
            log = EventLog(fresh)
            try:
                ready.set()
                attempt.wait(5)
                try:
                    log.emit(type='task.progress_reported', actor=Actor.system('team'), payload={})
                except Exception as exc:
                    errors.append(exc)
            finally:
                log.close()
        thread = threading.Thread(target=write_unsigned)
        thread.start()
        self.assertTrue(ready.wait(5))
        with signed.atomic():
            signed.emit(type='task.progress_reported', actor=Actor.system('team'), payload={})
            attempt.set()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], AuditSigningRequired)
        self.assertEqual(signed.count(), 1)
        self.assertTrue(signed.verify_chain().ok)

    def test_waiting_old_key_writer_rechecks_after_rotation(self):
        old = EventLog(self.path, audit_key='offline-secret')
        self.addCleanup(old.close)
        rotate = EventLog(self.path, audit_key='new-secret', key_id='new', audit_keys={'local': 'offline-secret'})
        self.addCleanup(rotate.close)
        before = rotate.count()
        ready, attempt = threading.Event(), threading.Event()
        errors = []
        def write_old():
            log = EventLog(self.path, audit_key='offline-secret')
            try:
                ready.set()
                attempt.wait(5)
                try:
                    log.emit(type='task.progress_reported', actor=Actor.system('team'), payload={})
                except Exception as exc:
                    errors.append(exc)
            finally:
                log.close()
        thread = threading.Thread(target=write_old)
        thread.start()
        self.assertTrue(ready.wait(5))
        with rotate.atomic():
            rotate.emit(type='task.progress_reported', actor=Actor.system('team'), payload={})
            attempt.set()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], AuditSigningMismatch)
        self.assertTrue(rotate.verify_chain().ok)
        self.assertEqual(rotate.count(), before + 1)


if __name__ == '__main__':
    unittest.main()
