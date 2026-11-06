"""Index migration and append admission, using disposable local databases."""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from cater.events import Actor, AuditSigningMismatch, AuditSigningRequired, EventLog


class EventIndexRecoveryTests(unittest.TestCase):
    def test_legacy_table_without_signature_columns_is_migrated(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            source = EventLog(root / "source.db")
            try:
                first = source.emit(type="task.progress_reported", actor=Actor.system("fixture"), payload={})
                columns = ("seq", "id", "ts", "task_id", "actor_json", "type", "category",
                           "payload_json", "prev_hash", "hash")
                old = sqlite3.connect(root / "legacy.db")
                try:
                    old.execute("""CREATE TABLE events (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
                        ts TEXT NOT NULL, task_id TEXT, actor_json TEXT NOT NULL, type TEXT NOT NULL,
                        category TEXT NOT NULL, payload_json TEXT NOT NULL, prev_hash TEXT NOT NULL,
                        hash TEXT NOT NULL)""")
                    old.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?)",
                                tuple(source.db.execute("SELECT " + ",".join(columns) + " FROM events").fetchone()))
                    old.commit()
                finally:
                    old.close()
            finally:
                source.close()
            migrated = EventLog(root / "legacy.db")
            try:
                self.assertTrue(migrated.verify_chain().ok)
                self.assertEqual(migrated.read()[0].hash, first.hash)
                names = {row["name"] for row in migrated.db.execute("PRAGMA index_list(events)")}
                self.assertTrue({"events_signed", "events_signed_key_seq", "events_unsigned_seq"} <= names)
                migrated.emit(type="task.progress_reported", actor=Actor.system("fixture"), payload={})
                self.assertTrue(migrated.verify_chain().ok)
            finally:
                migrated.close()

    def test_missing_live_index_fails_closed_and_reopen_restores_guards(self):
        for index in ("events_signed", "events_signed_key_seq", "events_unsigned_seq"):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as root:
                path = Path(root) / "signed.db"
                signed = EventLog(path, audit_key="fixture-key")
                try:
                    signed.emit(type="task.progress_reported", actor=Actor.system("fixture"), payload={})
                    count = signed.count()
                    signed.db.execute("DROP INDEX " + index)
                    signed.db.commit()
                    with self.assertRaises(sqlite3.OperationalError):
                        signed.emit(type="task.progress_reported", actor=Actor.system("fixture"), payload={})
                    self.assertEqual(signed.count(), count)
                finally:
                    signed.close()
                unsigned = EventLog(path)
                try:
                    with self.assertRaises(AuditSigningRequired):
                        unsigned.emit(type="task.progress_reported", actor=Actor.system("fixture"), payload={})
                    self.assertEqual(unsigned.count(), count)
                finally:
                    unsigned.close()
                wrong = EventLog(path, audit_key="different-fixture-key")
                try:
                    with self.assertRaises(AuditSigningMismatch):
                        wrong.emit(type="task.progress_reported", actor=Actor.system("fixture"), payload={})
                    self.assertEqual(wrong.count(), count)
                finally:
                    wrong.close()
                restored = EventLog(path, audit_key="fixture-key")
                try:
                    restored.emit(type="task.progress_reported", actor=Actor.system("fixture"), payload={})
                    self.assertTrue(restored.verify_chain().ok)
                finally:
                    restored.close()


if __name__ == "__main__":
    unittest.main()
