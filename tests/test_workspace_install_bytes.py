from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cater import workspace_tools as tools


class WorkspaceInstallBytesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cater-install-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "workspace"
        self.root.mkdir()
        self.executor = self.make_executor()

    def make_executor(self, mode="edit"):
        return tools.WorkspaceExecutor(tools.WorkspacePolicy.from_dict({
            "root": str(self.root), "mode": mode,
        }))

    def assert_denied(self, code, action):
        with self.assertRaises(tools._Denied) as caught:
            action()
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def assert_receipt(self, receipt, path, body, created, requested=False):
        self.assertEqual(receipt, {
            "path": path, "sha256": hashlib.sha256(body).hexdigest(),
            "bytes": len(body), "created": created, "already_present": not created,
            "executable": os.name != "nt" and bool((self.root / path).stat().st_mode & 0o111),
            "executable_requested": requested,
        })
        self.assertEqual((self.root / path).read_bytes(), body)

    def test_empty_binary_and_nested_install_with_fsync(self):
        for index, body in enumerate((b"", bytes(range(256)) * 20)):
            path = f"nested/{index}/asset.bin"
            with patch.object(tools.os, "fsync", wraps=os.fsync) as sync:
                receipt = self.executor.install_bytes(path, body)
            sync.assert_called_once()
            self.assert_receipt(receipt, path, body, True)
        self.assertFalse(list(self.root.rglob(".workspace-tmp-*")))

    def test_exact_32mib_boundary_and_existing_binary_above_text_limit(self):
        body = b"\x00\xff" * (16 * 1024 * 1024)
        self.assertEqual(len(body), tools.MAX_INSTALL_BYTES)
        receipt = self.executor.install_bytes("large.bin", body)
        self.assert_receipt(receipt, "large.bin", body, True)
        before = (self.root / "large.bin").stat()
        receipt = self.executor.install_bytes("large.bin", body)
        self.assert_receipt(receipt, "large.bin", body, False)
        self.assertEqual(tools._signature(before), tools._signature((self.root / "large.bin").stat()))
        self.assert_denied("too_large", lambda: self.executor.install_bytes("missing/file", body + b"x"))
        self.assertFalse((self.root / "missing").exists())

    def test_invalid_arguments_and_read_only_have_no_effect(self):
        self.assert_denied("tool_denied", lambda: self.make_executor("read_only").install_bytes("new/file", b"x"))
        for body in ("text", bytearray(b"x"), memoryview(b"x"), None, 1):
            with self.subTest(body=type(body)):
                self.assert_denied("invalid_arguments", lambda: self.executor.install_bytes("new/file", body))
        for kwargs in ({"executable": 1}, {"executable": None}, {"guard": False}):
            self.assert_denied("invalid_arguments", lambda: self.executor.install_bytes("new/file", b"x", **kwargs))
        self.assert_denied("invalid_arguments", lambda: self.executor.install_bytes(".", b"x"))
        self.assertEqual(list(self.root.iterdir()), [])
        self.assert_receipt(self.make_executor("execute").install_bytes("ok", b"x"), "ok", b"x", True)

    def test_not_a_model_tool_and_write_limits_unchanged(self):
        self.assertNotIn("workspace_install_bytes", {item["name"] for item in self.executor.schemas()})
        self.assertEqual(self.executor.execute("workspace_install_bytes", {})["error"]["code"], "tool_denied")
        for content in ("x" * (tools.MAX_FILE_BYTES + 1), "x\x00y"):
            result = self.executor.execute("workspace_write", {
                "path": "missing/file", "content": content, "expected_sha256": None,
            })
            self.assertEqual(result["status"], "error")
        self.assertFalse((self.root / "missing").exists())

    def test_read_file_default_limit_stays_one_mib(self):
        target = self.root / "text.txt"
        body = b"x" * tools.MAX_FILE_BYTES
        target.write_bytes(body)
        self.assertEqual(tools._read_file(target)[0], body)
        target.write_bytes(body + b"x")
        self.assert_denied("too_large", lambda: tools._read_file(target))
        self.assertEqual(tools._read_file(target, limit=tools.MAX_INSTALL_BYTES)[0], body + b"x")
        self.assertEqual(self.executor.execute("workspace_read", {"path": "text.txt"})["error"]["code"], "too_large")
        result = self.executor.execute("workspace_write", {
            "path": "text.txt", "content": "replacement",
            "expected_sha256": hashlib.sha256(body + b"x").hexdigest(),
        })
        self.assertEqual(result["error"]["code"], "too_large")
        self.assertEqual(target.read_bytes(), body + b"x")

    def test_existing_same_preserves_identity_mtime_and_mode_with_opposite_request(self):
        target = self.root / "same"
        target.write_bytes(b"\x00\xff")
        for mode, requested in ((0o400, True), (0o700, False)):
            os.chmod(target, mode)
            os.utime(target, ns=(1234567890000000000, 1234567890000000000))
            before = target.stat()
            try:
                with patch.object(tools.os, "chmod", side_effect=AssertionError("must not chmod")), \
                        patch.object(tools.os, "link", side_effect=AssertionError("must not publish")), \
                        patch.object(tools.tempfile, "mkstemp", side_effect=AssertionError("must not stage")):
                    receipt = self.executor.install_bytes("same", b"\x00\xff", executable=requested)
                self.assert_receipt(receipt, "same", b"\x00\xff", False, requested)
                after = target.stat()
                self.assertEqual(tools._signature(before), tools._signature(after))
                self.assertEqual(before.st_mode, after.st_mode)
            finally:
                os.chmod(target, 0o600)

    def test_existing_different_and_directory_are_rejected(self):
        target = self.root / "existing"
        target.write_bytes(b"different")
        before = target.stat()
        self.assert_denied("conflict", lambda: self.executor.install_bytes("existing", b"proposed"))
        self.assertEqual(target.read_bytes(), b"different")
        self.assertEqual(tools._signature(before), tools._signature(target.stat()))
        (self.root / "directory").mkdir()
        self.assert_denied("wrong_type", lambda: self.executor.install_bytes("directory", b""))

    def test_executable_requested_and_actual(self):
        for requested in (False, True):
            path = f"exec-{requested}"
            receipt = self.executor.install_bytes(path, b"program", executable=requested)
            self.assert_receipt(receipt, path, b"program", True, requested)
            self.assertEqual(receipt["executable"], requested and os.name != "nt")

    def test_portable_and_protected_paths(self):
        for path in ("../outside", "/absolute", "C:relative", "C:/absolute", "a/../b",
                     "a\\..\\b", "file:stream", "NUL.txt", "a.", "a\x00b", "ENV~1"):
            with self.subTest(path=path):
                self.assert_denied("invalid_path", lambda: self.executor.install_bytes(path, b"x"))
        for path in (".git/config", ".env", "a/.ssh/key", "secrets.json", "a/service.pem", ".workspace-tmp-test"):
            with self.subTest(path=path):
                self.assert_denied("protected_path", lambda: self.executor.install_bytes(path, b"x"))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_hardlink_is_rejected_even_for_identical_bytes(self):
        outside = self.base / "outside"
        outside.write_bytes(b"x")
        os.link(outside, self.root / "linked")
        self.assert_denied("unsafe_path", lambda: self.executor.install_bytes("linked", b"x"))
        self.assertEqual(outside.read_bytes(), b"x")

    def test_symlink_file_and_parent_are_rejected(self):
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "file").write_bytes(b"x")
        try:
            (self.root / "link").symlink_to(outside, target_is_directory=True)
            (self.root / "file-link").symlink_to(outside / "file")
        except OSError:
            self.skipTest("symlink privilege unavailable")
        for path in ("link/new/file", "file-link"):
            self.assert_denied("unsafe_path", lambda: self.executor.install_bytes(path, b"x"))
        self.assertFalse((outside / "new").exists())

    @unittest.skipUnless(os.name == "nt", "Windows junction")
    def test_junction_parent_and_replaced_root_are_rejected(self):
        import _winapi
        outside = self.base / "outside"
        outside.mkdir()
        _winapi.CreateJunction(str(outside), str(self.root / "junction"))
        self.assert_denied("unsafe_path", lambda: self.executor.install_bytes("junction/new/file", b"x"))
        other = self.base / "other"
        other.mkdir()
        executor = tools.WorkspaceExecutor(tools.WorkspacePolicy.from_dict({"root": str(other), "mode": "edit"}))
        other.rmdir()
        _winapi.CreateJunction(str(outside), str(other))
        self.assert_denied("unsafe_path", lambda: executor.install_bytes("new/file", b"x"))
        self.assertEqual(list(outside.iterdir()), [])

    def test_guard_runs_after_staging_fsync_and_can_deny(self):
        events = []
        sync = os.fsync

        def fsync(fd):
            sync(fd)
            events.append("fsync")

        def guard():
            events.append("guard")
            self.assertEqual(len(list(self.root.glob(".workspace-tmp-*"))), 1)
            raise tools._Denied("scope_changed", "scope changed")

        with patch.object(tools.os, "fsync", side_effect=fsync):
            self.assert_denied("scope_changed", lambda: self.executor.install_bytes("file", b"x", guard=guard))
        self.assertEqual(events, ["fsync", "guard"])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_guard_rechecks_existing_same_and_rejects_changes(self):
        target = self.root / "file"
        target.write_bytes(b"x")

        def guard():
            target.write_bytes(b"changed")

        self.assert_denied("conflict", lambda: self.executor.install_bytes("file", b"x", guard=guard))
        self.assertEqual(target.read_bytes(), b"changed")

    def test_guard_exception_on_existing_same_is_prepublication_denial(self):
        target = self.root / "file"
        target.write_bytes(b"x")
        before = target.stat()

        def guard():
            raise RuntimeError("PRIVATE changed authorization")

        error = self.assert_denied("operation_failed", lambda: self.executor.install_bytes("file", b"x", guard=guard))
        self.assertNotIn("PRIVATE", str(error))
        self.assertEqual(tools._signature(before), tools._signature(target.stat()))
        self.assertEqual(target.read_bytes(), b"x")

    def test_guard_arrival_and_staging_tamper_rejected(self):
        def arrive():
            (self.root / "arrival").write_bytes(b"external")

        self.assert_denied("conflict", lambda: self.executor.install_bytes("arrival", b"x", guard=arrive))
        self.assertEqual((self.root / "arrival").read_bytes(), b"external")

        def tamper():
            next(self.root.glob(".workspace-tmp-*")).write_bytes(b"tampered")

        self.assert_denied("conflict", lambda: self.executor.install_bytes("file", b"x", guard=tamper))
        self.assertFalse((self.root / "file").exists())
        self.assertFalse(list(self.root.glob(".workspace-tmp-*")))

    def test_guard_parent_replacement_rejected(self):
        (self.root / "parent").mkdir()

        def guard():
            (self.root / "parent").rename(self.root / "moved")
            (self.root / "parent").mkdir()

        self.assert_denied("conflict", lambda: self.executor.install_bytes("parent/file", b"x", guard=guard))
        self.assertFalse((self.root / "parent/file").exists())
        self.assertFalse((self.root / "moved/file").exists())

    def test_atomic_link_race_never_overwrites_arrival(self):
        link = os.link

        def arrive(source, target, **kwargs):
            Path(target).write_bytes(b"external")
            link(source, target, **kwargs)

        with patch.object(tools.os, "link", side_effect=arrive):
            self.assert_denied("conflict", lambda: self.executor.install_bytes("file", b"x"))
        self.assertEqual((self.root / "file").read_bytes(), b"external")
        self.assertFalse(list(self.root.glob(".workspace-tmp-*")))

    def test_prepublication_os_failure_is_opaque_denied(self):
        with patch.object(tools.os, "fsync", side_effect=OSError("PRIVATE host details")):
            error = self.assert_denied("operation_failed", lambda: self.executor.install_bytes("file", b"x"))
        self.assertNotIn("PRIVATE", str(error))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_link_exception_after_effect_is_opaque_oserror(self):
        link = os.link

        def publish_then_fail(source, target, **kwargs):
            link(source, target, **kwargs)
            raise tools._Denied("private", "PRIVATE host details")

        with patch.object(tools.os, "link", side_effect=publish_then_fail):
            with self.assertRaises(OSError) as caught:
                self.executor.install_bytes("file", b"x")
        self.assertNotIn("PRIVATE", str(caught.exception))
        self.assertEqual((self.root / "file").read_bytes(), b"x")

    def test_postpublication_target_change_is_unknown(self):
        link = os.link

        def publish_then_change(source, target, **kwargs):
            link(source, target, **kwargs)
            Path(target).write_bytes(b"external edit")

        with patch.object(tools.os, "link", side_effect=publish_then_change):
            with self.assertRaises(OSError):
                self.executor.install_bytes("file", b"x")
        self.assertEqual((self.root / "file").read_bytes(), b"external edit")

    def test_postpublication_cleanup_and_verification_errors_are_unknown(self):
        unlink = Path.unlink
        observe = tools._read_file
        for failure in ("cleanup", "verification"):
            path = f"file-{failure}"

            def fail_cleanup(target, *args, **kwargs):
                unlink(target, *args, **kwargs)
                if target.name.startswith(".workspace-tmp-"):
                    raise tools._Denied("private", "PRIVATE cleanup")

            def fail_verification(target, **kwargs):
                if target.name == path and target.exists():
                    raise tools._Denied("private", "PRIVATE verification")
                return observe(target, **kwargs)

            with patch.object(Path, "unlink", new=fail_cleanup if failure == "cleanup" else unlink), \
                    patch.object(tools, "_read_file", side_effect=fail_verification if failure == "verification" else observe):
                with self.assertRaises(OSError) as caught:
                    self.executor.install_bytes(path, b"x")
            self.assertNotIn("PRIVATE", str(caught.exception))
            self.assertEqual((self.root / path).read_bytes(), b"x")

    def test_lock_exit_failure_after_publication_is_unknown(self):
        lock = tools._write_lock

        @contextmanager
        def failing_unlock(root):
            with lock(root):
                yield
            raise tools._Denied("private", "PRIVATE unlock")

        with patch.object(tools, "_write_lock", failing_unlock):
            with self.assertRaises(OSError) as caught:
                self.executor.install_bytes("file", b"x")
        self.assertNotIn("PRIVATE", str(caught.exception))
        self.assertEqual((self.root / "file").read_bytes(), b"x")

    def test_cooperating_installers_have_one_creator(self):
        def install(index):
            return self.make_executor().install_bytes("shared", b"\x00\xff")

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(install, range(4)))
        self.assertEqual(sum(result["created"] for result in results), 1)
        self.assertEqual(sum(result["already_present"] for result in results), 3)
        self.assertEqual((self.root / "shared").read_bytes(), b"\x00\xff")


if __name__ == "__main__":
    unittest.main()
