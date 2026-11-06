"""Windows managed workspace paths and legacy bindings, using disposable data."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from cater.managed_workspaces import MANAGED_ID
from cater.runtime import Runtime


def extended(path):
    value = os.path.abspath(path)
    return Path("\\\\?\\UNC\\" + value[2:] if value.startswith("\\\\") else "\\\\?\\" + value)


@unittest.skipUnless(os.name == "nt", "Windows filesystem regression")
class ManagedWindowsPaths(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="managed-win-path-"))
        self.addCleanup(shutil.rmtree, extended(self.base))
        self.root = self.base / "workspace"
        self.root.mkdir()
        self.db = self.base / "events.db"
        self.config = self.base / "workspaces.json"
        self.configure(self.root)

    def configure(self, root):
        self.config.write_text(json.dumps({"workspaces": [], "managed": {"root": str(root), "mode": "edit"}}),
                               encoding="utf-8")

    def runtime(self, db=None):
        return Runtime(self.db if db is None else db, workspaces_path=self.config)

    def test_connection_through_switched_junction_cannot_authorize_other_database(self):
        import _winapi
        import sqlite3
        from cater.events import Actor, EventLog
        from cater.workspace_tools import WorkspaceExecutor

        a, b = self.base / "database-a", self.base / "database-b"
        a.mkdir(); b.mkdir()
        db_a, db_b = a / "events.db", b / "events.db"
        with self.runtime(db_b) as original:
            task = original.create_task("Database B")
            binding = original.workspaces.bind(task, MANAGED_ID, expected_revision=None)
            work = WorkspaceExecutor(original.workspaces.config[binding["workspace_id"]]["policy"])
            self.assertEqual(work.execute("workspace_write", {"path": "private.txt", "content": "fixture-b",
                                                             "expected_sha256": None})["status"], "ok")
        shutil.copyfile(db_b, db_a)
        log = EventLog(db_a)
        try:
            log.emit(actor=Actor.system(), type="msg.said", task_id=task, payload={"marker": "only-a"})
        finally:
            log.close()
        self.assertFalse(os.path.samefile(db_a, db_b))
        junction = self.base / "database-link"
        _winapi.CreateJunction(str(a), str(junction))
        original_connect = sqlite3.connect
        switched = False
        def switch_after_open(*args, **kwargs):
            nonlocal switched
            connection = original_connect(*args, **kwargs)
            if not switched:
                switched = True
                os.rmdir(junction)
                _winapi.CreateJunction(str(b), str(junction))
            return connection
        try:
            with patch("cater.events.sqlite3.connect", side_effect=switch_after_open):
                with self.runtime(junction / "events.db") as victim:
                    self.assertTrue(switched)
                    self.assertTrue(os.path.samefile(junction / "events.db", db_b))
                    self.assertTrue(any(event.payload.get("marker") == "only-a"
                                        for event in victim.log.read(task_id=task)))
                    self.assertFalse(victim.workspaces.view(task)["available"])
                    self.assertIsNone(victim.workspaces.config.get(binding["workspace_id"]))
                    self.assertIsNone(victim.team.read_sources._scope(task))
                    with self.assertRaisesRegex(ValueError, "数据库连接身份"):
                        victim.workspaces.bind(task, MANAGED_ID, expected_revision=binding["revision"])
            self.assertEqual((Path(binding["root"]) / "private.txt").read_text(), "fixture-b")
        finally:
            os.rmdir(junction)
        with self.runtime(db_b) as recovered:
            self.assertTrue(recovered.workspaces.view(task)["available"])
            work = WorkspaceExecutor(recovered.workspaces.config[binding["workspace_id"]]["policy"])
            self.assertEqual(work.execute("workspace_read", {"path": "private.txt"})["content"], "fixture-b")

    def test_unproven_or_changed_path_disables_managed_without_changing_data(self):
        from cater.workspace_tools import WorkspaceExecutor

        with self.runtime() as rt:
            task = rt.create_task("Stable database")
            binding = rt.workspaces.bind(task, MANAGED_ID, expected_revision=None)
            work = WorkspaceExecutor(rt.workspaces.config[binding["workspace_id"]]["policy"])
            self.assertEqual(work.execute("workspace_write", {"path": "note.txt", "content": "retained",
                                                             "expected_sha256": None})["status"], "ok")
            with patch("cater.managed_workspaces._managed_database_path_state", return_value=None):
                self.assertFalse(rt.workspaces.view(task)["available"])
                self.assertIsNone(rt.workspaces.config.get(binding["workspace_id"]))
                with self.assertRaisesRegex(ValueError, "数据库连接身份"):
                    rt.workspaces.bind(task, MANAGED_ID, expected_revision=binding["revision"])
            self.assertTrue(rt.workspaces.view(task)["available"])
        with self.runtime(extended(self.db)) as rt:
            self.assertEqual(rt.workspaces.view(task)["root"], binding["root"])
            work = WorkspaceExecutor(rt.workspaces.config[binding["workspace_id"]]["policy"])
            self.assertEqual(work.execute("workspace_read", {"path": "note.txt"})["content"], "retained")

    def test_database_replaced_during_open_fails_closed(self):
        import sqlite3
        from cater.events import Actor, EventLog

        with self.runtime() as original:
            task = original.create_task("Original database")
            binding = original.workspaces.bind(task, MANAGED_ID, expected_revision=None)
        other = self.base / "other.db"
        shutil.copyfile(self.db, other)
        other_log = EventLog(other)
        try:
            other_log.emit(actor=Actor.system(), type="msg.said", task_id=task, payload={"marker": "replacement"})
        finally:
            other_log.close()
        saved = self.base / "original.db"
        original_connect = sqlite3.connect
        switched = False
        def replace_before_open(*args, **kwargs):
            nonlocal switched
            if not switched:
                switched = True
                os.replace(self.db, saved)
                shutil.copyfile(other, self.db)
            return original_connect(*args, **kwargs)
        with patch("cater.events.sqlite3.connect", side_effect=replace_before_open):
            with self.runtime() as victim:
                self.assertTrue(switched)
                self.assertTrue(any(event.payload.get("marker") == "replacement"
                                    for event in victim.log.read(task_id=task)))
                self.assertIsNone(victim.workspaces._namespace)
                self.assertFalse(victim.workspaces.view(task)["available"])
                self.assertIsNone(victim.workspaces.config.get(binding["workspace_id"]))
        self.assertTrue(saved.exists())
        self.assertTrue(Path(binding["root"]).is_dir())

    def test_identity_unavailable_during_open_stays_unproven(self):
        from cater import events

        state = events._managed_database_path_state
        calls = 0
        def first_unavailable(path):
            nonlocal calls
            calls += 1
            return None if calls == 1 else state(path)
        with patch.object(events, "_managed_database_path_state", side_effect=first_unavailable):
            with self.runtime() as rt:
                self.assertIsNone(rt.workspaces._namespace)
                task = rt.create_task("Unproven database")
                with self.assertRaisesRegex(ValueError, "数据库连接身份"):
                    rt.workspaces.bind(task, MANAGED_ID, expected_revision=None)

    def legacy_namespace(self, log):
        import hashlib
        identity = "file:" + os.path.normcase(os.path.realpath(log.path))
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    def test_legacy_binding_requires_explicit_rebind_after_reopen(self):
        with patch("cater.workspaces.database_namespace", side_effect=self.legacy_namespace):
            with self.runtime() as rt:
                task = rt.create_task("Legacy binding")
                binding = rt.workspaces.bind(task, MANAGED_ID, expected_revision=None)
                self.assertTrue(binding["available"])
                (Path(binding["root"]) / "legacy.txt").write_text("original", encoding="utf-8")
        with self.runtime(extended(self.db)) as rt:
            self.assertFalse(rt.workspaces.view(task)["available"])
            self.assertIsNone(rt.workspaces.config.get(binding["workspace_id"]))
            rebound = rt.workspaces.bind(task, MANAGED_ID, expected_revision=binding["revision"])
            self.assertTrue(rebound["available"])
            self.assertNotEqual(rebound["root"], binding["root"])
            self.assertTrue(Path(binding["root"]).is_dir())
            self.assertEqual((Path(binding["root"]) / "legacy.txt").read_text(encoding="utf-8"), "original")
            self.assertFalse((Path(rebound["root"]) / "legacy.txt").exists())

    def test_legacy_extended_database_binding_is_unavailable_on_reopen(self):
        with patch("cater.workspaces.database_namespace", side_effect=self.legacy_namespace):
            with self.runtime(extended(self.db)) as rt:
                task = rt.create_task("Extended legacy binding")
                binding = rt.workspaces.bind(task, MANAGED_ID, expected_revision=None)
                self.assertTrue(binding["available"])
        with self.runtime() as rt:
            self.assertFalse(rt.workspaces.view(task)["available"])
            self.assertIsNone(rt.workspaces.config.get(binding["workspace_id"]))

    def test_legacy_extended_workspace_root_does_not_grant_scope(self):
        from dataclasses import replace
        from cater.events import Actor
        from cater.workspaces import _entry
        with patch("cater.workspaces.database_namespace", side_effect=self.legacy_namespace):
            with self.runtime() as rt:
                task = rt.create_task("Legacy extended root")
                binding = rt.workspaces.bind(task, MANAGED_ID, expected_revision=None)
                policy = rt.workspaces.config[binding["workspace_id"]]["policy"]
                old_policy = replace(policy, root=extended(policy.root))
                fingerprint = _entry(binding["workspace_id"], binding["title"], old_policy, [])["fingerprint"]
                event = rt.log.emit(type="workspace.bound", task_id=task, actor=Actor.human(), payload={
                    "workspace_id": binding["workspace_id"], "title": binding["title"], "root": str(old_policy.root),
                    "mode": binding["mode"], "fingerprint": fingerprint, "managed": True,
                    "previous_revision": binding["revision"]})
        with self.runtime(extended(self.db)) as rt:
            reopened = rt.workspaces.view(task)
            self.assertFalse(reopened["available"], reopened)
            self.assertEqual(reopened["revision"], event.seq)
            self.assertEqual(reopened["root"], str(old_policy.root))
            self.assertIsNone(rt.workspaces.fingerprint(task))
            self.assertIsNone(rt.team.read_sources._scope(task))
            self.config.write_text(json.dumps({"workspaces": [], "managed": {"root": str(self.root),
                                                                               "mode": "read_only"}}), encoding="utf-8")
            self.assertFalse(rt.workspaces.view(task)["available"])

    def test_copied_legacy_database_does_not_gain_original_binding(self):
        with patch("cater.workspaces.database_namespace", side_effect=self.legacy_namespace):
            with self.runtime() as rt:
                task = rt.create_task("Legacy copy")
                binding = rt.workspaces.bind(task, MANAGED_ID, expected_revision=None)
                with self.runtime(self.base / "copy.db") as clone:
                    rt.log.db.backup(clone.log.db)
        with self.runtime(self.base / "copy.db") as clone:
            self.assertFalse(clone.workspaces.view(task)["available"])
            self.assertIsNone(clone.workspaces.config.get(binding["workspace_id"]))
            rebound = clone.workspaces.bind(task, MANAGED_ID, expected_revision=binding["revision"])
            self.assertTrue(rebound["available"])
            self.assertNotEqual(rebound["root"], binding["root"])

    def test_deep_managed_directory_is_created_and_reopened(self):
        root = self.base / ("👩‍💻" * 5) / ("a" * 115)
        root.mkdir(parents=True)
        self.configure(root)
        with self.runtime() as rt:
            task = rt.create_task("Deep managed binding")
            binding = rt.workspaces.bind(task, MANAGED_ID, expected_revision=None)
            self.assertTrue(binding["available"])
            self.assertGreater(len(binding["root"].encode("utf-16-le")) // 2, 260)
            self.assertTrue(extended(binding["root"]).is_dir())
            self.assertFalse(binding["root"].startswith("\\\\?\\"))
            from cater.workspace_tools import WorkspaceExecutor
            work = WorkspaceExecutor(rt.workspaces.config[binding["workspace_id"]]["policy"])
            written = work.execute("workspace_write", {"path": "folder/note.txt", "content": "fixture",
                                                        "expected_sha256": None})
            self.assertEqual(written["status"], "ok", written)
            self.assertEqual(work.execute("workspace_read", {"path": "folder/note.txt"})["content"], "fixture")
            self.assertEqual(work.execute("workspace_list", {"path": "folder"})["status"], "ok")
        with self.runtime(extended(self.db)) as rt:
            self.assertEqual(rt.workspaces.view(task), binding)

    def test_deep_managed_namespace_rejects_junction(self):
        import _winapi
        root = self.base / ("x" * 150)
        root.mkdir()
        outside = self.base / "outside"
        outside.mkdir()
        self.configure(root)
        with self.runtime() as rt:
            task = rt.create_task("Reject linked namespace")
            namespace = root / rt.workspaces._namespace
            _winapi.CreateJunction(str(outside), str(extended(namespace)))
            try:
                with self.assertRaises(ValueError):
                    rt.workspaces.bind(task, MANAGED_ID, expected_revision=None)
                self.assertEqual(list(outside.iterdir()), [])
                self.assertIsNone(rt.workspaces.view(task)["revision"])
            finally:
                os.rmdir(extended(namespace))

    def test_legacy_binding_is_unavailable_even_in_insensitive_directory(self):
        with patch("cater.workspaces.database_namespace", side_effect=self.legacy_namespace):
            with self.runtime() as rt:
                task = rt.create_task("Unknown legacy identity")
                binding = rt.workspaces.bind(task, MANAGED_ID, expected_revision=None)
        with self.runtime() as rt:
            self.assertFalse(rt.workspaces.view(task)["available"])
            self.assertIsNone(rt.workspaces.config.get(binding["workspace_id"]))

    def test_case_sensitive_database_roots_are_not_merged(self):
        parent = self.base / "databases"
        parent.mkdir()
        result = subprocess.run(["fsutil.exe", "file", "setCaseSensitiveInfo", str(parent), "enable"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            self.skipTest("filesystem cannot enable case sensitivity")
        first, second = parent / "Data.db", parent / "data.db"
        with self.runtime(first) as a, self.runtime(second) as b:
            self.assertFalse(os.path.samefile(first, second))
            from cater.events import Actor
            for rt in (a, b):
                rt.log.emit(type="task.created", actor=Actor.human(), task_id="task_same",
                            payload={"goal": "fixture", "acceptance": []})
            one = a.workspaces.bind("task_same", MANAGED_ID, expected_revision=None)
            two = b.workspaces.bind("task_same", MANAGED_ID, expected_revision=None)
            self.assertNotEqual(one["root"], two["root"])
        with self.runtime(extended(first)) as a:
            self.assertEqual(a.workspaces.view("task_same"), one)

    def test_legacy_case_collision_fails_closed_for_copied_database(self):
        parent = self.base / "sensitive"
        parent.mkdir()
        result = subprocess.run(["fsutil.exe", "file", "setCaseSensitiveInfo", str(parent), "enable"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            self.skipTest("filesystem cannot enable case sensitivity")
        first, second = parent / "Data.db", parent / "data.db"
        with patch("cater.workspaces.database_namespace", side_effect=self.legacy_namespace):
            with self.runtime(first) as rt:
                task = rt.create_task("Legacy collision")
                binding = rt.workspaces.bind(task, MANAGED_ID, expected_revision=None)
                with self.runtime(second) as clone:
                    rt.log.db.backup(clone.log.db)
        with self.runtime(extended(second)) as clone:
            self.assertFalse(clone.workspaces.view(task)["available"])
            self.assertIsNone(clone.workspaces.config.get(binding["workspace_id"]))
        with self.runtime(first) as original:
            self.assertFalse(original.workspaces.view(task)["available"])

    def test_legacy_collision_in_case_sensitive_ancestor_fails_closed(self):
        parent = self.base / "sensitive-ancestor"
        parent.mkdir()
        result = subprocess.run(["fsutil.exe", "file", "setCaseSensitiveInfo", str(parent), "enable"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            self.skipTest("filesystem cannot enable case sensitivity")
        upper, lower = parent / "Folder", parent / "folder"
        upper.mkdir()
        lower.mkdir()
        with patch("cater.workspaces.database_namespace", side_effect=self.legacy_namespace):
            with self.runtime(upper / "events.db") as rt:
                task = rt.create_task("Legacy ancestor collision")
                binding = rt.workspaces.bind(task, MANAGED_ID, expected_revision=None)
                with self.runtime(lower / "events.db") as clone:
                    rt.log.db.backup(clone.log.db)
        for db in (upper / "events.db", lower / "events.db"):
            with self.runtime(db) as rt:
                self.assertFalse(rt.workspaces.view(task)["available"])
                self.assertIsNone(rt.workspaces.config.get(binding["workspace_id"]))

    def test_legacy_case_sensitive_parent_without_other_files_is_still_unproven(self):
        parent = self.base / "single-sensitive"
        parent.mkdir()
        result = subprocess.run(["fsutil.exe", "file", "setCaseSensitiveInfo", str(parent), "enable"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            self.skipTest("filesystem cannot enable case sensitivity")
        db = parent / "Data.db"
        with patch("cater.workspaces.database_namespace", side_effect=self.legacy_namespace):
            with self.runtime(db) as rt:
                task = rt.create_task("Unproven legacy owner")
                binding = rt.workspaces.bind(task, MANAGED_ID, expected_revision=None)
                self.assertTrue(binding["available"])
        with self.runtime(db) as rt:
            self.assertFalse(rt.workspaces.view(task)["available"])
            self.assertIsNone(rt.workspaces.config.get(binding["workspace_id"]))

    def test_legacy_database_copy_replaced_at_same_path_cannot_inherit(self):
        from contextlib import closing
        import sqlite3
        with patch("cater.workspaces.database_namespace", side_effect=self.legacy_namespace):
            with self.runtime() as rt:
                task = rt.create_task("Legacy replaced database")
                binding = rt.workspaces.bind(task, MANAGED_ID, expected_revision=None)
                with closing(sqlite3.connect(self.base / "copy.db")) as copy:
                    rt.log.db.backup(copy)
        original = self.db.stat()
        copy_info = (self.base / "copy.db").stat()
        self.assertNotEqual((original.st_dev, original.st_ino), (copy_info.st_dev, copy_info.st_ino))
        os.replace(self.base / "copy.db", self.db)
        with self.runtime() as rt:
            self.assertFalse(rt.workspaces.view(task)["available"])
            self.assertIsNone(rt.workspaces.config.get(binding["workspace_id"]))

    def test_new_database_copy_replaced_at_same_path_cannot_inherit(self):
        from contextlib import closing
        import sqlite3
        with self.runtime() as rt:
            task = rt.create_task("New replaced database")
            binding = rt.workspaces.bind(task, MANAGED_ID, expected_revision=None)
            with closing(sqlite3.connect(self.base / "copy.db")) as copy:
                rt.log.db.backup(copy)
        original, copy_info = self.db.stat(), (self.base / "copy.db").stat()
        self.assertNotEqual((original.st_dev, original.st_ino), (copy_info.st_dev, copy_info.st_ino))
        os.replace(self.base / "copy.db", self.db)
        with self.runtime() as rt:
            self.assertFalse(rt.workspaces.view(task)["available"])
            self.assertIsNone(rt.workspaces.config.get(binding["workspace_id"]))

    def test_legacy_copy_after_disabling_case_sensitivity_cannot_inherit(self):
        from contextlib import closing
        import sqlite3
        parent = self.base / "toggle-case"
        parent.mkdir()
        def set_case(value):
            return subprocess.run(["fsutil.exe", "file", "setCaseSensitiveInfo", str(parent), value],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  creationflags=subprocess.CREATE_NO_WINDOW).returncode
        if set_case("enable"):
            self.skipTest("filesystem cannot enable case sensitivity")
        first, copy = parent / "Data.db", parent / "data.db"
        with patch("cater.workspaces.database_namespace", side_effect=self.legacy_namespace):
            with self.runtime(first) as rt:
                task = rt.create_task("Legacy toggle")
                binding = rt.workspaces.bind(task, MANAGED_ID, expected_revision=None)
                with closing(sqlite3.connect(copy)) as target:
                    rt.log.db.backup(target)
        first.unlink()
        if set_case("disable"):
            self.skipTest("filesystem cannot disable case sensitivity")
        self.assertTrue(os.path.samefile(first, copy))
        with self.runtime(first) as rt:
            self.assertFalse(rt.workspaces.view(task)["available"])
            self.assertIsNone(rt.workspaces.config.get(binding["workspace_id"]))
