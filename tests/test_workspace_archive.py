from __future__ import annotations

import copy
from contextlib import contextmanager, nullcontext
import gzip
import hashlib
import io
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from cater import workspace_archive as archive
from cater import workspace_tools as tools


class WorkspaceArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "workspace"
        self.root.mkdir()
        self.workspace = tools.WorkspaceExecutor(tools.WorkspacePolicy.from_dict({
            "root": str(self.root), "mode": "read_only"}))

    def write(self, name, body=b"data"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        return path

    def manifest(self, paths=("a",), size=0):
        return {"version": 1, "files": [{"path": name, "bytes": size,
            "sha256": "a" * 64, "executable": False} for name in paths],
            "total_bytes": len(paths) * size}

    def test_exported_limits(self):
        self.assertEqual(archive.MAX_ARCHIVE_BYTES, 65 * 1024 * 1024)
        self.assertEqual(archive.MAX_TOTAL_BYTES, 64 * 1024 * 1024)
        self.assertEqual(archive.MAX_MEMBER_BYTES, 32 * 1024 * 1024)
        self.assertEqual(archive.MAX_FILES, 256)
        self.assertTrue(issubclass(archive.ArchiveError, ValueError))

    def test_zip_bytes_hashes_modes_empty_binary_utf8_and_determinism(self):
        bodies = {"src/run.sh": b"#!/bin/sh\nexit 0\n", "empty": b"",
                  "binary.bin": bytes(range(256)) * 300,
                  "text/\u4f60\u597d.txt": "\u4f60\u597d\n".encode("utf-8")}
        for name, body in bodies.items():
            self.write(name, body)
        self.write("unselected.txt", b"must not be included")
        run = self.root / "src/run.sh"
        if os.name != "nt":
            run.chmod(0o751)
        selected = list(reversed(sorted(bodies)))
        with patch.object(self.workspace, "execute", side_effect=AssertionError("executed")):
            body, manifest = archive.create_snapshot(self.workspace, selected)
        self.assertEqual(selected, list(reversed(sorted(bodies))))
        archive.validate_manifest(manifest)
        self.assertEqual(manifest["total_bytes"], sum(map(len, bodies.values())))
        with zipfile.ZipFile(io.BytesIO(body)) as result:
            self.assertEqual(result.namelist(), sorted(bodies))
            self.assertIsNone(result.testzip())
            for info, entry in zip(result.infolist(), manifest["files"]):
                with self.subTest(name=info.filename):
                    expected = bodies[info.filename]
                    self.assertEqual(result.read(info), expected)
                    self.assertEqual(entry["bytes"], len(expected))
                    self.assertEqual(entry["sha256"], hashlib.sha256(expected).hexdigest())
                    executable = bool((self.root / info.filename).stat().st_mode & 0o111)
                    self.assertIs(entry["executable"], executable)
                    self.assertEqual(info.external_attr >> 16,
                                     stat.S_IFREG | (0o755 if executable else 0o644))
                    self.assertEqual(info.date_time, (1980, 1, 1, 0, 0, 0))
                    self.assertEqual(info.create_system, 3)
                    self.assertEqual(info.compress_type, zipfile.ZIP_STORED)
                    self.assertEqual(info.extra, b"")
                    self.assertEqual(info.comment, b"")
        self.assertEqual((body, manifest), archive.create_snapshot(self.workspace, sorted(bodies)))

    def test_executable_mode_mapping_with_platform_independent_stat(self):
        self.write("run")
        def executable(info):
            values = {field: getattr(info, field) for field in (
                "st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_nlink")}
            return SimpleNamespace(**values, st_mode=stat.S_IFREG | 0o711)

        inspect, fstat = tools._inspect, os.fstat
        with patch.object(tools, "_write_lock", return_value=nullcontext()), \
                patch.object(tools, "_inspect", side_effect=lambda p, d=None:
                             executable(inspect(p, d)) if d is False else inspect(p, d)), \
                patch.object(os, "fstat", side_effect=lambda fd: executable(fstat(fd))):
            body, manifest = archive.create_snapshot(self.workspace, ["run"])
        self.assertTrue(manifest["files"][0]["executable"])
        with zipfile.ZipFile(io.BytesIO(body)) as result:
            self.assertEqual(result.getinfo("run").external_attr >> 16, stat.S_IFREG | 0o755)

    def test_portable_paths(self):
        for name in ("file.txt", "src/a-b_2.py", "text/\u4f60\u597d.txt", "\u00e9.txt"):
            self.assertEqual(archive.portable_path(name), name)
        invalid = [None, True, 1, b"a", "", ".", "..", "./a", "a/./b", "a/../b",
                   "a//b", "a/", "/a", "a\\b", "C:a", "C:/a", "//server/share/a",
                   "a:b", "a?b", "a*b", 'a"b', "a<b", "a>b", "a|b", "a\x00b",
                   "a\x1fb", "a\x7fb", "a\u200bb", "a\ud800", "a.", "a ", " a",
                   "CON", "con.txt", "NUL", "AUX", "PRN", "CONIN$", "CONOUT$.txt",
                   "COM1.txt", "lpt9", "COM\u00b9.txt", "a~1.txt", "\uff0e", "\uff0e\uff0e/a",
                   "a\uff0fb", "a\uff3cb", "a\uff1ab", "a" * 241, "a/" * 33 + "b",
                   "a/" * 1024 + "b"]
        for name in invalid:
            with self.subTest(name=repr(name)), self.assertRaises(archive.ArchiveError):
                archive.portable_path(name)

    def test_protected_components_rejected_before_access(self):
        names = [".env", "src/.env.local", ".git/config", "a/.ssh/id_rsa",
                 "credentials.json", "a/secret.pem", "a/SECRETS/x",
                 "a/\uff0eenv", ".workspace-tmp-forbidden/file"]
        with patch.object(self.workspace, "_path", side_effect=AssertionError("accessed")), \
                patch.object(tools, "_write_lock", side_effect=AssertionError("locked")):
            for name in names:
                with self.subTest(name=name), self.assertRaises(archive.ArchiveError):
                    archive.create_snapshot(self.workspace, [name])

    def test_alias_and_prefix_collisions_before_access(self):
        cases = [("a", "a"), ("a", "A"), ("a", "\uff41"),
                 ("\u00e9", "e\u0301"), ("ss", "\u00df"),
                 ("a", "a/b"), ("a/b", "a"), ("A/x", "a/y"), ("a", "A/b")]
        with patch.object(self.workspace, "_path", side_effect=AssertionError("accessed")):
            for paths in cases:
                with self.subTest(paths=paths), self.assertRaises(archive.ArchiveError):
                    archive.create_snapshot(self.workspace, list(paths))
                with self.assertRaises(archive.ArchiveError):
                    archive.validate_manifest(self.manifest(sorted(paths)))

    def test_manifest_strict_schema_types_digests_totals_and_order(self):
        good = self.manifest(("a", "b"), 1)
        archive.validate_manifest(good)
        bad = [None, [], {}, {**good, "version": True}, {**good, "version": 2},
               {**good, "extra": 1}, {**good, "files": ()}, {**good, "files": []},
               {**good, "total_bytes": True}, {**good, "total_bytes": -1},
               {**good, "total_bytes": 3}, self.manifest(("b", "a"))]
        for field, values in {"bytes": [True, -1, 1.0, "1"],
                              "sha256": [None, "g" * 64, "a" * 63, "a" * 64 + "\n"],
                              "executable": [0, 1, "false", None],
                              "path": ["../a", ".env", 1]}.items():
            for value in values:
                changed = copy.deepcopy(good)
                changed["files"][0][field] = value
                bad.append(changed)
        bad.append({**good, "files": [None]})
        changed = copy.deepcopy(good)
        del changed["files"][0]["path"]
        bad.append(changed)
        changed = copy.deepcopy(good)
        changed["files"][0]["extra"] = 1
        bad.append(changed)
        for value in bad:
            with self.subTest(value=value), self.assertRaises(archive.ArchiveError):
                archive.validate_manifest(value)

    def test_limits_with_small_patched_bounds(self):
        self.write("a", b"1234")
        self.write("b", b"5678")
        cases = [("MAX_FILES", 1, ["a", "b"]), ("MAX_MEMBER_BYTES", 3, ["a"]),
                 ("MAX_TOTAL_BYTES", 7, ["a", "b"]), ("MAX_ARCHIVE_BYTES", 100, ["a"])]
        for constant, limit, paths in cases:
            with self.subTest(constant=constant), patch.object(archive, constant, limit), \
                    self.assertRaises(archive.ArchiveError):
                archive.create_snapshot(self.workspace, paths)
        with patch.object(archive, "MAX_MEMBER_BYTES", 4), patch.object(archive, "MAX_TOTAL_BYTES", 8):
            archive.create_snapshot(self.workspace, ["a", "b"])
        for constant, limit, manifest in [
                ("MAX_FILES", 1, self.manifest(("a", "b"))),
                ("MAX_MEMBER_BYTES", 3, self.manifest(size=4)),
                ("MAX_TOTAL_BYTES", 7, self.manifest(("a", "b"), 4))]:
            with self.subTest(constant=constant), patch.object(archive, constant, limit), \
                    self.assertRaises(archive.ArchiveError):
                archive.validate_manifest(manifest)
        for paths in ([], None, "a", ("a",), [1]):
            with self.subTest(paths=paths), self.assertRaises(archive.ArchiveError):
                archive.create_snapshot(self.workspace, paths)

    def test_directory_missing_hardlink_and_symlink(self):
        source = self.write("source")
        for name in ("missing", ".", "folder"):
            (self.root / "folder").mkdir(exist_ok=True)
            with self.subTest(name=name), self.assertRaises(archive.ArchiveError):
                archive.create_snapshot(self.workspace, [name])
        try:
            os.link(source, self.root / "hard")
        except OSError:
            pass
        else:
            for name in ("source", "hard"):
                with self.assertRaises(archive.ArchiveError):
                    archive.create_snapshot(self.workspace, [name])
        try:
            (self.root / "link").symlink_to(source)
            (self.root / "dirlink").symlink_to(self.root / "folder", target_is_directory=True)
        except OSError:
            pass
        else:
            for name in ("link", "dirlink/file"):
                with self.assertRaises(archive.ArchiveError):
                    archive.create_snapshot(self.workspace, [name])

    def test_special_and_reparse_stat_rejection(self):
        for mode, attributes, links in [(stat.S_IFIFO, 0, 1), (stat.S_IFSOCK, 0, 1),
                                         (stat.S_IFREG, 0x400, 1), (stat.S_IFREG, 0, 2)]:
            info = SimpleNamespace(st_mode=mode, st_file_attributes=attributes, st_nlink=links)
            with self.subTest(mode=mode, attributes=attributes, links=links), \
                    self.assertRaises(archive.ArchiveError):
                archive._state(info)

    def test_mutation_before_open_during_read_and_after_zip(self):
        path = self.write("a", b"before")
        original_open = tools._open_file

        def changed_open(target, flags, **kwargs):
            if target.name == "a":
                target.write_bytes(b"changed before opening")
            return original_open(target, flags, **kwargs)

        with patch.object(tools, "_open_file", side_effect=changed_open), \
                self.assertRaisesRegex(archive.ArchiveError, "changed"):
            archive.create_snapshot(self.workspace, ["a"])
        original_fdopen = os.fdopen

        class MutatingReader:
            def __init__(self, fd, mode):
                self.stream = original_fdopen(fd, mode)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.stream.close()

            def fileno(self):
                return self.stream.fileno()

            def read(self, size):
                self_test.assertLessEqual(size, archive.MAX_MEMBER_BYTES + 1)
                data = self.stream.read(size)
                path.write_bytes(b"changed during reading with a different size")
                return data

        self_test = self
        with patch.object(os, "fdopen", side_effect=MutatingReader), \
                self.assertRaisesRegex(archive.ArchiveError, "changed"):
            archive.create_snapshot(self.workspace, ["a"])
        self.write("b")
        original_write = zipfile.ZipFile.writestr

        def changed_write(result, info, body):
            original_write(result, info, body)
            if info.filename == "b":
                path.write_bytes(b"changed after first file was archived")

        with patch.object(zipfile.ZipFile, "writestr", new=changed_write), \
                self.assertRaisesRegex(archive.ArchiveError, "changed"):
            archive.create_snapshot(self.workspace, ["a", "b"])

    def test_gzip_is_opaque_and_no_extraction_or_commands(self):
        compressed = gzip.compress(b"z" * 200000)
        self.write("input.gz", compressed)
        with patch.object(gzip, "decompress", side_effect=AssertionError("decompressed")), \
                patch.object(zipfile.ZipFile, "extractall", side_effect=AssertionError("extracted")), \
                patch.object(self.workspace, "execute", side_effect=AssertionError("executed")):
            body, manifest = archive.create_snapshot(self.workspace, ["input.gz"])
        with zipfile.ZipFile(io.BytesIO(body)) as result:
            self.assertEqual(result.read("input.gz"), compressed)
        self.assertEqual(manifest["total_bytes"], len(compressed))

    def test_read_is_bounded_even_when_source_grows(self):
        path = self.write("a", b"1234")
        original_fdopen = os.fdopen
        requested = []

        @contextmanager
        def growing_reader(fd, mode):
            with original_fdopen(fd, mode) as stream:
                def read(size):
                    requested.append(size)
                    path.write_bytes(b"x" * 100)
                    return stream.read(size)

                yield SimpleNamespace(fileno=stream.fileno, read=read)

        with patch.object(archive, "MAX_MEMBER_BYTES", 4), \
                patch.object(archive, "MAX_TOTAL_BYTES", 4), \
                patch.object(os, "fdopen", side_effect=growing_reader), \
                self.assertRaisesRegex(archive.ArchiveError, "byte limit"):
            archive.create_snapshot(self.workspace, ["a"])
        self.assertEqual(requested, [5])

    def test_capture_errors_are_wrapped_without_host_details(self):
        for error in (OSError("sensitive-host-path credential-value"),
                      tools._Denied("conflict", "sensitive-host-path credential-value"),
                      ValueError("sensitive-host-path credential-value")):
            with self.subTest(error=type(error)), \
                    patch.object(self.workspace, "_path", side_effect=error), \
                    self.assertRaises(archive.ArchiveError) as caught:
                archive.create_snapshot(self.workspace, ["a"])
            self.assertNotIn("sensitive", str(caught.exception))
            self.assertNotIn("credential", str(caught.exception))
            self.assertTrue(caught.exception.__suppress_context__)


if __name__ == "__main__":
    unittest.main()
