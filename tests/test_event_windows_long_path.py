import hashlib
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from urllib.parse import quote
from unittest.mock import patch

from cater.activity_owner import activity_owner
from cater.events import Actor, EventLog, _windows_io_path
from cater.workspace_tools import WorkspacePolicy


class ActivityPathLikeTests(unittest.TestCase):
    def test_short_pathlike_and_string_contend(self):
        with tempfile.TemporaryDirectory(prefix="cater-activity-pathlike-") as root:
            database = Path(root) / "events.db"
            with activity_owner(database, "task", "activity") as held:
                self.assertTrue(held)
                with activity_owner(str(database), "task", "activity") as contender:
                    self.assertFalse(contender)
            with activity_owner(str(database), "task", "activity") as held:
                self.assertTrue(held)
                with activity_owner(database, "task", "activity") as contender:
                    self.assertFalse(contender)


class EventUriTests(unittest.TestCase):
    def test_sqlite_memory_uri_retains_native_behavior(self):
        script = ("import sqlite3; from cater.events import EventLog, Actor; "
                  "uri='file:cater-path-test?mode=memory&cache=shared'; "
                  "a=EventLog(uri); a.emit(actor=Actor.system(), type='task.created'); "
                  "b=EventLog(uri); raw=sqlite3.connect(uri); "
                  "assert b.db.execute('pragma database_list').fetchall()[0][2] == "
                  "raw.execute('pragma database_list').fetchall()[0][2]; "
                  "assert len(b.read()) == 1; raw.close(); b.close(); a.close()")
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
        with tempfile.TemporaryDirectory(prefix="cater-event-uri-") as root:
            subprocess.run([sys.executable, "-c", script], cwd=root, env=env, check=True)

    def test_sqlite_file_uri_query_and_escaping(self):
        script = ("import sqlite3; from cater.events import EventLog, Actor; "
                  "a=EventLog('test # name.db'); a.emit(actor=Actor.system(), type='task.created'); a.close(); "
                  "uri='file:test%20%23%20name.db?mode=rw'; b=EventLog(uri); "
                  "raw=sqlite3.connect(uri); "
                  "assert b.path == uri and b.db.execute('pragma database_list').fetchall()[0][2] == "
                  "raw.execute('pragma database_list').fetchall()[0][2]; "
                  "assert len(b.read()) == raw.execute('select count(*) from events').fetchone()[0]; "
                  "raw.close(); b.close()")
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
        with tempfile.TemporaryDirectory(prefix="cater-event-uri-") as root:
            subprocess.run([sys.executable, "-c", script], cwd=root, env=env, check=True)

    def test_runtime_pathlike_uri_retains_native_sqlite_behavior(self):
        script = """\
import sqlite3
from cater.runtime import Runtime
class UriPath:
    calls = 0
    def __fspath__(self):
        self.calls += 1
        return 'file:cater-uri-pathlike?mode=memory&cache=shared'
    def __str__(self):
        raise AssertionError('unexpected string conversion')
path = UriPath()
with Runtime(path) as runtime:
    raw = sqlite3.connect(runtime.log.path)
    try:
        assert path.calls == 1
        assert runtime.log.db.execute('pragma database_list').fetchall()[0][2] == raw.execute('pragma database_list').fetchall()[0][2]
    finally:
        raw.close()
"""
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
        with tempfile.TemporaryDirectory(prefix="cater-event-uri-") as root:
            subprocess.run([sys.executable, "-c", script], cwd=root, env=env, check=True)


@unittest.skipUnless(os.name == "nt", "requires Windows filesystem and locking")
class WindowsEventPathTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cater-event-path-"))
        self.addCleanup(shutil.rmtree, "\\\\?\\" + str(self.root))

    def deep_path(self, minimum):
        root = self.root
        while len(str(root / "events.db")) < minimum:
            root /= "segment_" + "x" * 33
        return root / "events # 测试.db"

    def astral_path(self, segments):
        root = self.root
        for index in range(segments):
            root /= str(index) + "😀" * 35
        return root / "events.db"

    def test_astral_long_database_uses_utf16_length(self):
        path = self.astral_path(3)
        normal = str(path)
        self.assertLess(len(normal), 240)
        self.assertGreater(len(normal.encode("utf-16-le")) // 2, 260)
        Path("\\\\?\\" + str(path.parent)).mkdir(parents=True)
        log = EventLog(normal)
        try:
            self.assertEqual(log.path, normal)
            log.emit(actor=Actor.system(), type="task.created")
            self.assertTrue(log.verify_chain().ok)
        finally:
            log.close()
        alias = EventLog("\\\\?\\" + normal)
        try:
            self.assertEqual(len(alias.read()), 1)
        finally:
            alias.close()

    def test_astral_long_activity_lock_uses_utf16_length(self):
        path = self.astral_path(2)
        normal = str(path)
        lock = normal + ".activity-locks\\" + "a" * 64
        self.assertLess(len(lock), 240)
        self.assertGreater(len(lock.encode("utf-16-le")) // 2, 260)
        Path("\\\\?\\" + str(path.parent)).mkdir(parents=True)
        extended = "\\\\?\\" + normal
        with activity_owner(path, "task", "activity") as held:
            self.assertTrue(held)
            with activity_owner(extended, "task", "activity") as contender:
                self.assertFalse(contender)

    def test_long_database_aliases_wal_and_signed_chain(self):
        path = self.deep_path(290)
        normal = str(path)
        extended = "\\\\?\\" + normal
        self.assertGreater(len(normal), 260)
        log = EventLog(normal, audit_key="test")
        try:
            self.assertEqual(log.path, normal)
            self.assertEqual(log.db.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower(), "wal")
            first = log.emit(actor=Actor.system(), type="task.created")
            alias = EventLog(extended, audit_key="test")
            try:
                self.assertEqual(alias.read(), [first])
                alias.emit(actor=Actor.system(), type="msg.said", payload={"text": "hello"})
                self.assertTrue(log.verify_chain().ok)
                self.assertEqual(len(log.read()), 2)
            finally:
                alias.close()
        finally:
            log.close()
        reopened = EventLog(normal, audit_key="test")
        try:
            self.assertEqual(reopened.verify_chain().signed, 2)
        finally:
            reopened.close()
        with activity_owner(path, "task", "activity") as held:
            self.assertTrue(held)
            with activity_owner(extended, "task", "activity") as contender:
                self.assertFalse(contender)

    def test_long_lock_matches_old_location_and_alias_across_processes(self):
        db = self.deep_path(190)
        self.assertLess(len(str(db)), 240)
        self.assertGreater(len(str(db)) + len(".activity-locks\\") + 64, 260)
        Path(_windows_io_path(str(db.parent))).mkdir(parents=True, exist_ok=True)
        extended = "\\\\?\\" + str(db)
        identity = hashlib.sha256(b"task\0activity").hexdigest()
        legacy = Path(extended + ".activity-locks") / identity
        with activity_owner(str(db), "task", "activity") as held:
            self.assertTrue(held)
            self.assertTrue(legacy.exists())
            script = ("import sys; from cater.activity_owner import activity_owner; "
                      "ctx=activity_owner(sys.argv[1], 'task', 'activity'); "
                      "owned=ctx.__enter__(); print(int(owned)); ctx.__exit__(None,None,None)")
            result = subprocess.run([sys.executable, "-c", script, extended], capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout.strip(), "0")
        with activity_owner(extended, "task", "activity") as held:
            self.assertTrue(held)
            with activity_owner(str(db), "task", "activity") as contender:
                self.assertFalse(contender)
        # Simulate an old worker using the original lock file and byte lock.
        import msvcrt
        with legacy.open("a+b") as handle:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            try:
                with activity_owner(str(db), "task", "activity") as contender:
                    self.assertFalse(contender)
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

    def test_relative_long_database_path(self):
        path = self.deep_path(290)
        relative = os.path.relpath(path, self.root)
        script = ("import sys; from cater.events import EventLog, Actor; "
                  "log=EventLog(sys.argv[1]); log.emit(actor=Actor.system(), type='task.created'); "
                  "assert log.path == sys.argv[1] and log.verify_chain().ok; log.close()")
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
        subprocess.run([sys.executable, "-c", script, relative], cwd=self.root, env=env, check=True)
        log = EventLog(path)
        try:
            self.assertEqual(len(log.read()), 1)
        finally:
            log.close()

    def test_wal_reopens_after_process_exit(self):
        path = self.deep_path(290)
        script = ("import os, sys; from cater.events import EventLog, Actor; "
                  "log=EventLog(sys.argv[1]); log.db.execute('PRAGMA journal_mode=WAL'); "
                  "log.emit(actor=Actor.system(), type='task.created'); os._exit(0)")
        subprocess.run([sys.executable, "-c", script, str(path)], check=True)
        log = EventLog(path)
        try:
            self.assertEqual(len(log.read()), 1)
            self.assertTrue(log.verify_chain().ok)
        finally:
            log.close()

    def test_long_ambiguous_paths_are_rejected(self):
        path = str(self.deep_path(280).parent / "bad. " / "events.db")
        with self.assertRaises(ValueError):
            EventLog(path)
        with self.assertRaises(ValueError):
            with activity_owner(path, "task", "activity"):
                pass
        self.assertEqual(_windows_io_path("file:example?mode=memory"), "file:example?mode=memory")
        self.assertEqual(_windows_io_path(":memory:"), ":memory:")

    def test_runtime_and_event_log_reject_raw_extended_syntax_before_open(self):
        from cater.runtime import Runtime

        target = self.root / "target"
        target.mkdir()
        (self.root / "other").mkdir()
        db = target / "events.db"
        log = EventLog(db)
        try:
            log.emit(actor=Actor.system(), type="task.created", payload={"fixture": True})
        finally:
            log.close()
        before = db.read_bytes()
        prefix = "\\\\?\\" + str(self.root)
        paths = (prefix + "\\.\\target\\events.db", prefix + "\\other\\..\\target\\events.db",
                 prefix + "/target/events.db", prefix + "\\\\target\\events.db")
        for raw in paths:
            for factory in (EventLog, Runtime):
                with self.subTest(path=raw, factory=factory.__name__):
                    with self.assertRaises(ValueError):
                        instance = factory(raw)
                        instance.close()
                    self.assertEqual(db.read_bytes(), before)
        check = EventLog(db)
        try:
            self.assertTrue(check.verify_chain().ok)
            self.assertEqual(len(check.read()), 1)
        finally:
            check.close()

    def test_runtime_pathlike_is_captured_once(self):
        from cater.runtime import Runtime

        first = self.root / "first" / "events.db"
        second = self.root / "second" / "events.db"
        class ChangingPath:
            calls = 0
            def __fspath__(self):
                self.calls += 1
                return str(first if self.calls == 1 else second)
            def __str__(self):
                return os.fspath(self)
        path = ChangingPath()
        with Runtime(path) as runtime:
            self.assertEqual(path.calls, 1)
            self.assertEqual(runtime.log.path, str(first))
            self.assertEqual(runtime._artifact_root, first.parent / "artifacts")
        self.assertFalse(second.exists())

    def test_raw_pathlike_is_checked_before_sqlite_connect(self):
        from cater.runtime import Runtime

        raw = "\\\\?\\" + str(self.root) + "\\.\\other.db"
        class RawPath:
            calls = 0
            def __fspath__(self):
                self.calls += 1
                return raw
            def __str__(self):
                raise AssertionError("use the path-like value, not its representation")
        for factory in (EventLog, Runtime):
            path = RawPath()
            with self.subTest(factory=factory.__name__), \
                    patch("cater.events.sqlite3.connect", side_effect=AssertionError("database was opened")):
                with self.assertRaises(ValueError):
                    factory(path)
                self.assertEqual(path.calls, 1)
        self.assertFalse((self.root / "other.db").exists())

    def test_encoded_uri_cannot_hide_invalid_extended_path(self):
        from cater.runtime import Runtime

        db = self.root / "uri.db"
        log = EventLog(db)
        try:
            log.emit(actor=Actor.system(), type="task.created")
        finally:
            log.close()
        raw = "\\\\?\\" + str(self.root) + "\\.\\uri.db"
        encoded = quote(raw, safe="")
        native_connect = sqlite3.connect
        for uri in ("file:" + encoded, "file:///" + encoded,
                    "file:" + encoded.replace(".", "%2E")):
            # SQLite builds with URI support enabled can open the normalized target.
            native = native_connect(uri, uri=True)
            try:
                self.assertEqual(native.execute("select count(*) from events").fetchone()[0], 1)
            finally:
                native.close()
            before = db.read_bytes()
            for factory in (EventLog, Runtime):
                with self.subTest(uri=uri, factory=factory.__name__), \
                        patch("cater.events.sqlite3.connect", side_effect=AssertionError("database was opened")):
                    with self.assertRaises(ValueError):
                        factory(uri)
                    self.assertEqual(db.read_bytes(), before)

        valid_uri = "file:" + quote("\\\\?\\" + str(db), safe="")
        def connect_with_uri(*args, **kwargs):
            return native_connect(*args, uri=True, **kwargs)
        with patch("cater.events.sqlite3.connect", side_effect=connect_with_uri):
            log = EventLog(valid_uri)
            try:
                self.assertEqual(log.path, valid_uri)
                self.assertEqual(len(log.read()), 1)
            finally:
                log.close()

    def test_valid_extended_pathlike_keeps_database_and_user_path(self):
        from cater.runtime import Runtime

        raw = "\\\\?\\" + str(self.root / "valid.db")
        class RawPath:
            def __fspath__(self):
                return raw
            def __str__(self):
                raise AssertionError("unexpected path-like string conversion")
        with Runtime(RawPath()) as runtime:
            self.assertEqual(runtime.log.path, raw)
            runtime.log.emit(actor=Actor.system(), type="task.created")
            self.assertTrue(runtime.log.verify_chain().ok)
        log = EventLog(self.root / "valid.db")
        try:
            self.assertEqual(len(log.read()), 1)
        finally:
            log.close()

    def test_unc_and_device_namespace_conversion(self):
        tail = "\\".join(["segment" * 10] * 4) + "\\events.db"
        unc = "\\\\server\\share\\" + tail
        self.assertEqual(_windows_io_path(unc), "\\\\?\\UNC\\server\\share\\" + tail)
        self.assertEqual(_windows_io_path(unc.replace("\\", "/")),
                         "\\\\?\\UNC\\server\\share\\" + tail)
        device = "//?/C:/" + tail.replace("\\", "/")
        with self.assertRaises(ValueError):
            _windows_io_path(device)
        for invalid in ("\\\\?\\UNC\\server\\share\\.\\db", "\\\\?\\UNC\\server\\share/db"):
            with self.assertRaises(ValueError):
                _windows_io_path(invalid)
        uri = "file:example/./db?mode=memory&cache=shared"
        self.assertEqual(_windows_io_path(uri), uri)
        uri_with_query = "file:ordinary.db?ignored=%5C%5C%3F%5CC%3A%5C.%5Cexample"
        self.assertEqual(_windows_io_path(uri_with_query), uri_with_query)
        for component in ("CON .db", "CONIN$", "name?", "name:stream", "name. "):
            with self.subTest(component=component), self.assertRaises(ValueError):
                _windows_io_path(unc + "\\" + component)

    def test_utf16_threshold_and_lone_surrogate(self):
        prefix = str(self.root) + "\\"
        used = len(prefix.encode("utf-16-le", errors="surrogatepass")) // 2
        component = "😀" * 35
        # Use legal-sized components while reaching each side of the threshold.
        base = prefix + component + "\\" + component + "\\"
        remaining = 239 - used - 142
        short = base + "x" * remaining
        self.assertEqual(len(short.encode("utf-16-le")) // 2, 239)
        self.assertEqual(_windows_io_path(short), short)
        long = short + "\ud800"
        self.assertEqual(len(long.encode("utf-16-le", errors="surrogatepass")) // 2, 240)
        self.assertEqual(_windows_io_path(long), "\\\\?\\" + long)

    def test_sqlite_surrogate_and_nul_behavior(self):
        long_parent = self.astral_path(3).parent
        Path("\\\\?\\" + str(long_parent)).mkdir(parents=True)
        for parent in (self.root, long_parent):
            with self.subTest(parent=parent):
                path = str(parent / "bad\ud800.db")
                log = EventLog(path)
                try:
                    self.assertEqual(log.path, path)
                    self.assertTrue(log.verify_chain().ok)
                finally:
                    log.close()
                with self.assertRaises(ValueError):
                    EventLog(str(parent / "bad\0.db"))

    def test_junction_alias_locks_and_workspace_link_guard(self):
        path = self.deep_path(290)
        log = EventLog(path)
        log.close()
        junction = self.root / "junction"
        subprocess.run(["cmd", "/c", "mklink", "/J", str(junction),
                        _windows_io_path(str(path.parent))], capture_output=True, check=True)
        alias = junction / path.name
        self.assertTrue(os.path.samefile(_windows_io_path(str(path)), str(alias)))
        with activity_owner(str(path), "task", "activity") as held:
            self.assertTrue(held)
            with activity_owner(str(alias), "task", "activity") as contender:
                self.assertFalse(contender)
        for root in (str(junction), "\\\\?\\" + str(junction)):
            with self.subTest(root=root), self.assertRaises(ValueError):
                WorkspacePolicy.from_dict({"root": root, "mode": "read_only"})
