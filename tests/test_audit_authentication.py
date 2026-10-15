from __future__ import annotations

import json
import io
import unittest
from contextlib import redirect_stdout

from cater.cli import main
from cater.events import Actor, EventLog
from cater.runtime import Runtime


class AuditAuthenticationTests(unittest.TestCase):
    def log(self, **options):
        log = EventLog(":memory:", **options)
        self.addCleanup(log.close)
        return log

    def reopen(self, source, **options):
        reopened = self.log(**options)
        source.db.backup(reopened.db)
        return reopened

    def emit(self, log, outcome="failed"):
        return log.emit(actor=Actor.system("local_verifier"), type="verification.completed",
                        task_id="audit-fixture", payload={"outcome": outcome})

    def rewrite(self, log, event, *, signature=None, payload=None):
        # Simulate a database-file writer in memory, bypassing SQLite's trigger.
        payload = event.payload if payload is None else payload
        fingerprint = log._fingerprint(event_id=event.id, ts=event.ts, task_id=event.task_id,
            actor=event.actor, type=event.type, category=event.category,
            payload=payload, prev_hash=event.prev_hash)
        log.db.execute("DROP TRIGGER IF EXISTS events_no_update")
        log.db.execute("UPDATE events SET payload_json=?, hash=?, signature=?, key_id=NULL WHERE seq=?",
                       (json.dumps(payload), fingerprint, signature, event.seq))
        log.db.commit()

    def test_tampered_rehashed_unsigned_suffix_cannot_follow_signed_prefix(self):
        log = self.log(audit_key="fixture-secret")
        prefix = self.emit(log)
        tail = self.emit(log)
        self.rewrite(log, tail, payload={"outcome": "passed"})
        before = log.read()

        report = log.verify_chain()

        self.assertFalse(report.ok)
        self.assertEqual(report.reason, "missing signature after signed event")
        self.assertEqual((report.checked, report.broken_at, report.signed), (2, tail.seq, 1))
        self.assertEqual(log.read(), before)
        self.assertEqual(log.read()[0], prefix)
        self.assertEqual(log.read()[-1].payload["outcome"], "passed")

    def test_stripping_every_signature_cannot_downgrade_keyed_verification(self):
        for missing in (None, ""):
            with self.subTest(signature=missing):
                log = self.log(audit_key="fixture-secret")
                events = [self.emit(log), self.emit(log)]
                for event in events:
                    self.rewrite(log, event, signature=missing)
                report = log.verify_chain()
                self.assertFalse(report.ok)
                self.assertEqual(report.reason, "audit key supplied but log contains no signatures")
                self.assertEqual((report.checked, report.broken_at, report.signed), (2, events[0].seq, 0))

    def test_hash_only_log_without_keys_remains_valid(self):
        log = self.log()
        self.emit(log)
        self.emit(log)
        report = log.verify_chain()
        self.assertTrue(report.ok, report.reason)
        self.assertEqual((report.checked, report.signed), (2, 0))
        self.assertTrue(all(event.signature is None for event in log.read()))

    def test_unsigned_log_with_verification_keys_requires_authentication(self):
        source = self.log()
        self.emit(source)
        for options in ({"audit_key": "fixture-secret"},
                        {"audit_keys": {"historical": "fixture-secret"}}):
            with self.subTest(options=tuple(options)):
                log = self.reopen(source, **options)
                report = log.verify_chain()
                self.assertFalse(report.ok)
                self.assertEqual(report.reason, "audit key supplied but log contains no signatures")
                self.assertEqual(report.signed, 0)

    def test_unsigned_prefix_can_be_upgraded_by_appending_signed_events(self):
        source = self.log()
        prefix = [self.emit(source), self.emit(source)]
        log = self.reopen(source, audit_key="fixture-secret", key_id="first-key")
        self.assertFalse(log.verify_chain().ok)
        signed = self.emit(log)

        report = log.verify_chain()

        self.assertTrue(report.ok, report.reason)
        self.assertEqual((report.checked, report.signed), (3, 1))
        self.assertEqual(log.read()[:2], prefix)
        self.assertEqual(signed.prev_hash, prefix[-1].hash)

    def test_empty_signature_prefix_does_not_start_signed_history(self):
        source = self.log()
        prefix = self.emit(source)
        self.rewrite(source, prefix, signature="")
        self.assertTrue(source.verify_chain().ok)
        log = self.reopen(source, audit_key="fixture-secret")
        self.emit(log)
        report = log.verify_chain()
        self.assertTrue(report.ok, report.reason)
        self.assertEqual(report.signed, 1)

    def test_empty_signature_after_signing_is_a_downgrade(self):
        log = self.log(audit_key="fixture-secret")
        self.emit(log)
        tail = self.emit(log)
        self.rewrite(log, tail, signature="")
        report = log.verify_chain()
        self.assertFalse(report.ok)
        self.assertEqual(report.reason, "missing signature after signed event")
        self.assertEqual(report.broken_at, tail.seq)

    def test_later_signature_cannot_hide_an_unsigned_gap(self):
        log = self.log(audit_key="fixture-secret")
        self.emit(log)
        gap = self.emit(log)
        self.emit(log)
        self.rewrite(log, gap)
        report = log.verify_chain()
        self.assertFalse(report.ok)
        self.assertEqual(report.reason, "missing signature after signed event")
        self.assertEqual(report.broken_at, gap.seq)

    def test_appending_without_key_after_signing_is_rejected_when_verified(self):
        source = self.log(audit_key="fixture-secret")
        self.emit(source)
        unsigned_writer = self.reopen(source)
        tail = self.emit(unsigned_writer)
        log = self.reopen(unsigned_writer, audit_key="fixture-secret")
        report = log.verify_chain()
        self.assertFalse(report.ok)
        self.assertEqual(report.reason, "missing signature after signed event")
        self.assertEqual(report.broken_at, tail.seq)

    def test_rotation_keeps_historical_events_and_keyring_verification(self):
        source = self.log(audit_key="old-secret", key_id="old")
        prefix = self.emit(source)
        log = self.reopen(source, audit_key="new-secret", key_id="new",
                          audit_keys={"old": "old-secret"})
        tail = self.emit(log)
        report = log.verify_chain()
        self.assertTrue(report.ok, report.reason)
        self.assertEqual(report.signed, 2)
        self.assertEqual(log.read()[0], prefix)
        self.assertEqual((prefix.key_id, tail.key_id), ("old", "new"))

        verifier = self.reopen(log, audit_keys={"old": "old-secret", "new": "new-secret"})
        self.assertIsNone(verifier.audit_key)
        self.assertTrue(verifier.verify_chain().ok)
        self.assertEqual(verifier.read(), log.read())

    def test_wrong_single_key_fails_with_signature_mismatch(self):
        source = self.log(audit_key="fixture-secret", key_id="historical")
        event = self.emit(source)
        log = self.reopen(source, audit_key="wrong-secret")
        report = log.verify_chain()
        self.assertFalse(report.ok)
        self.assertEqual(report.reason, "signature mismatch")
        self.assertEqual(report.broken_at, event.seq)

    def test_rotation_requires_correct_historical_key(self):
        source = self.log(audit_key="old-secret", key_id="old")
        event = self.emit(source)
        for keys, reason in (({}, "no audit key supplied for key_id='old'"),
                             ({"old": "wrong-secret"}, "signature mismatch")):
            with self.subTest(keys=tuple(keys)):
                log = self.reopen(source, audit_key="new-secret", key_id="new", audit_keys=keys)
                report = log.verify_chain()
                self.assertFalse(report.ok)
                self.assertIn(reason, report.reason)
                self.assertEqual(report.broken_at, event.seq)

    def test_signed_log_without_verification_key_still_fails(self):
        source = self.log(audit_key="fixture-secret", key_id="fixture")
        self.emit(source)
        report = self.reopen(source).verify_chain()
        self.assertFalse(report.ok)
        self.assertIn("no audit key supplied for key_id='fixture'", report.reason)

    def test_empty_log_with_keys_is_valid(self):
        for options in ({"audit_key": "fixture-secret"},
                        {"audit_keys": {"old": "fixture-secret"}}):
            with self.subTest(options=tuple(options)):
                report = self.log(**options).verify_chain()
                self.assertTrue(report.ok, report.reason)
                self.assertEqual((report.checked, report.signed), (0, 0))

    def test_none_single_key_does_not_enable_signing_or_authentication(self):
        log = self.log(audit_key=None)
        event = self.emit(log)
        self.assertIsNone(log.audit_key)
        self.assertEqual(log.audit_keys, {})
        self.assertFalse(log._single_audit_key_fallback)
        self.assertIsNone(event.signature)
        self.assertIsNone(event.key_id)
        self.assertTrue(log.verify_chain().ok)

    def test_explicit_empty_single_key_is_rejected_by_event_log_and_runtime(self):
        for factory in (EventLog, Runtime):
            for key in ("", b""):
                for options in ({}, {"audit_keys": {"local": "fixture-secret"}}):
                    with self.subTest(factory=factory.__name__, key=key, keyring=bool(options)):
                        with self.assertRaisesRegex(ValueError, "audit key must be non-empty"):
                            factory(":memory:", audit_key=key, **options)

    def test_explicit_empty_cli_audit_key_cannot_silently_disable_signing(self):
        output = io.StringIO()
        with redirect_stdout(output):
            result = main(["--db", ":memory:", "--audit-key", "", "chain"])
        self.assertEqual(result, 1)
        self.assertIn("audit key must be non-empty", output.getvalue())

    def test_empty_keyring_values_are_rejected(self):
        for key in (None, "", b""):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "must be non-empty"):
                self.log(audit_keys={"empty": key})

    def test_no_key_does_not_authenticate_signatures_from_an_empty_secret(self):
        source = self.log()
        event = self.emit(source)
        self.rewrite(source, event, signature=source._sign_with(b"", event.hash))
        log = self.reopen(source, audit_key=None)
        report = log.verify_chain()
        self.assertFalse(report.ok)
        self.assertIn("no audit key supplied", report.reason)


if __name__ == "__main__":
    unittest.main()
