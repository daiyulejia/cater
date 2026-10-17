from __future__ import annotations

import copy
import hashlib
import io
import os
from pathlib import Path
import stat
import struct
import tempfile
import unittest
from unittest.mock import patch
import warnings
import zipfile

from cater import archive_members as members
from cater.artifact_blobs import put_blob
from cater.events import EventLog
from cater.workspace_archive import ArchiveError, create_snapshot
from cater.workspace_tools import WorkspaceExecutor, WorkspacePolicy


class ArchiveMembersTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.log = EventLog(self.root / "events.db")
        self.addCleanup(self.log.close)
        self.bodies = {"a.bin": b"\x00\xff\x80\r\n", "empty": b"", "run": b"exit 0\n"}
        self.blob, self.manifest = self.make_zip()
        self.artifact = self.store(self.blob, self.manifest)

    def make_zip(self, bodies=None, *, compression=zipfile.ZIP_STORED, change=None):
        bodies = self.bodies if bodies is None else bodies
        manifest = {"version": 1, "files": [], "total_bytes": 0}
        buffer = io.BytesIO()
        with warnings.catch_warnings(), zipfile.ZipFile(buffer, "w") as archive:
            warnings.simplefilter("ignore", UserWarning)
            for index, (name, body) in enumerate(sorted(bodies.items())):
                executable = name == "run"
                info = zipfile.ZipInfo(name)
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | (0o755 if executable else 0o644)) << 16
                info.compress_type = compression
                if change:
                    change(info, index)
                archive.writestr(info, body)
                manifest["files"].append({"path": name, "bytes": len(body),
                    "sha256": hashlib.sha256(body).hexdigest(), "executable": executable})
                manifest["total_bytes"] += len(body)
        return buffer.getvalue(), manifest

    def store(self, blob, manifest):
        return {"format": "archive", "archive": manifest, "content_bytes": len(blob),
                "content_sha256": put_blob(self.log, blob)}

    def reject(self, artifact=None, **kwargs):
        with self.assertRaises(ArchiveError) as caught:
            members.read_members(self.log, self.artifact if artifact is None else artifact, **kwargs)
        self.assertIsInstance(caught.exception, ValueError)
        self.assertEqual(str(caught.exception), "Archive member read failed.")
        self.assertTrue(caught.exception.__suppress_context__)

    def test_actual_snapshot_db_blob_roundtrip(self):
        root = self.root / "workspace"
        root.mkdir()
        for name, body in self.bodies.items():
            (root / name).write_bytes(body)
        if os.name != "nt":
            (root / "run").chmod(0o755)
        workspace = WorkspaceExecutor(WorkspacePolicy.from_dict({
            "root": str(root), "mode": "read_only"}))
        blob, manifest = create_snapshot(workspace, list(self.bodies))
        artifact = self.store(blob, manifest)
        changes = self.log.db.total_changes
        with patch.object(zipfile.ZipFile, "extractall", side_effect=AssertionError("extract")), \
                patch.object(zipfile.ZipFile, "extract", side_effect=AssertionError("extract")), \
                patch.object(workspace, "execute", side_effect=AssertionError("execute")):
            result = members.read_members(self.log, artifact)
        self.assertEqual(result, [{**e, "body": self.bodies[e["path"]]} for e in manifest["files"]])
        self.assertEqual(self.log.db.total_changes, changes)
        self.assertEqual(self.log.count(), 0)

    def test_binary_empty_modes_and_selection_manifest_order(self):
        result = members.read_members(self.log, self.artifact, ["run", "a.bin", "empty"])
        self.assertEqual(result, [{**e, "body": self.bodies[e["path"]]}
                                  for e in self.manifest["files"]])
        self.assertTrue(result[-1]["executable"])
        self.assertFalse(result[0]["executable"])
        self.assertEqual(result[1]["body"], b"")
        self.assertEqual(members.read_members(self.log, self.artifact, ["empty"],
                                            max_member_bytes=1, max_total_bytes=1)[0]["body"], b"")

    def test_selection_types_duplicates_missing_and_nonportable_before_blob(self):
        for paths in ([], (), "run", ["a.bin", "a.bin"], ["missing"], ["RUN"],
                      [True], [0], ["../a.bin"], [".env"], ["C:/private"]):
            with self.subTest(paths=paths), patch.object(members, "read_blob") as read:
                self.reject(paths=paths)
                read.assert_not_called()

    def test_strict_positive_budget_types_and_preflight(self):
        for key in ("max_files", "max_member_bytes", "max_total_bytes"):
            for value in (True, False, 0, -1, 1.0, "1", None):
                with self.subTest(key=key, value=value), patch.object(members, "read_blob") as read:
                    self.reject(**{key: value})
                    read.assert_not_called()
        for limits in ({"max_files": 2}, {"max_member_bytes": 5}, {"max_total_bytes": 11}):
            with self.subTest(limits=limits), patch.object(members, "read_blob") as read:
                self.reject(**limits)
                read.assert_not_called()
        self.assertEqual(len(members.read_members(self.log, self.artifact,
                             max_files=3, max_member_bytes=7, max_total_bytes=13)), 3)

    def test_full_manifest_validation_before_blob_even_for_subset(self):
        bad = [None, {}, {**self.artifact, "format": "text"}]
        for field, value in (("version", True), ("total_bytes", 0), ("extra", 1)):
            manifest = copy.deepcopy(self.manifest)
            manifest[field] = value
            bad.append({**self.artifact, "archive": manifest})
        for field, value in (("path", ".env"), ("bytes", True), ("sha256", "bad"),
                             ("executable", 1), ("path", "a.bin/child")):
            manifest = copy.deepcopy(self.manifest)
            manifest["files"][-1][field] = value
            bad.append({**self.artifact, "archive": manifest})
        for artifact in bad:
            with self.subTest(artifact=artifact), patch.object(members, "read_blob") as read:
                # None is also an invalid artifact, not this helper's default.
                with self.assertRaises(ArchiveError):
                    members.read_members(self.log, artifact, ["a.bin"])
                read.assert_not_called()

    def test_blob_missing_bad_hash_length_and_no_disk_fallback(self):
        for changes in ({"content_sha256": "0" * 64}, {"content_bytes": len(self.blob) + 1},
                        {"content_bytes": True}, {"content_sha256": "invalid"}):
            with self.subTest(changes=changes):
                self.reject({**self.artifact, **changes, "local_path": str(self.root / "private.zip")})
        other = EventLog(self.root / "missing.db")
        try:
            with patch("builtins.open", side_effect=AssertionError("disk fallback")):
                with self.assertRaises(ArchiveError):
                    members.read_members(other, self.artifact)
        finally:
            other.close()
        # Damaged legacy storage: no changes to production immutability guards.
        other = EventLog(self.root / "damaged.db")
        try:
            other.db.execute("CREATE TABLE artifact_blobs(sha256 TEXT PRIMARY KEY, content BLOB NOT NULL)")
            other.db.execute("INSERT INTO artifact_blobs VALUES (?, ?)",
                             (self.artifact["content_sha256"], b"x" * len(self.blob)))
            with self.assertRaises(ArchiveError):
                members.read_members(other, self.artifact)
        finally:
            other.close()

    def test_truncated_zip_and_nonzip(self):
        for blob in (self.blob[:-10], self.blob[:40], b"not a ZIP"):
            with self.subTest(size=len(blob)):
                self.reject(self.store(blob, self.manifest))

    def test_crc_sha_and_unselected_body_scope(self):
        damaged = bytearray(self.blob)
        damaged[30 + len("a.bin")] ^= 1
        artifact = self.store(bytes(damaged), self.manifest)
        self.reject(artifact, paths=["a.bin"])
        self.assertEqual(members.read_members(self.log, artifact, ["empty"])[0]["body"], b"")
        manifest = copy.deepcopy(self.manifest)
        manifest["files"][0]["sha256"] = "0" * 64
        self.reject({**self.artifact, "archive": manifest})
        self.assertEqual(len(members.read_members(self.log, {**self.artifact, "archive": manifest},
                                                 ["empty"])), 1)

    def test_all_central_entries_names_order_duplicates_dirs_and_modes(self):
        changes = [lambda i: setattr(i, "filename", "../bad"),
                   lambda i: setattr(i, "filename", ".env"),
                   lambda i: setattr(i, "filename", "a.bin"),
                   lambda i: setattr(i, "filename", "folder/"),
                   lambda i: setattr(i, "create_system", 0),
                   lambda i: setattr(i, "external_attr", (stat.S_IFLNK | 0o777) << 16),
                   lambda i: setattr(i, "external_attr", (stat.S_IFIFO | 0o644) << 16),
                   lambda i: setattr(i, "external_attr", (stat.S_IFREG | 0o600) << 16),
                   lambda i: setattr(i, "external_attr", (stat.S_IFREG | 0o755) << 16),
                   lambda i: setattr(i, "external_attr", (stat.S_IFREG | 0o644) << 16 | 0x10)]
        for change in changes:
            with self.subTest(change=change):
                blob, manifest = self.make_zip(change=lambda info, n: change(info) if n == 1 else None)
                artifact = self.store(blob, manifest)
                with patch.object(zipfile.ZipFile, "open") as opened:
                    self.reject(artifact, paths=["a.bin"])
                    opened.assert_not_called()
        with zipfile.ZipFile(io.BytesIO(self.blob)) as archive:
            infos = archive.infolist()
        central = self.blob.index(b"PK\x01\x02")
        first_len = 46 + len(infos[0].filename)
        second_len = 46 + len(infos[1].filename)
        reordered = (self.blob[:central] + self.blob[central + first_len:central + first_len + second_len]
                     + self.blob[central:central + first_len] + self.blob[central + first_len + second_len:])
        self.reject(self.store(reordered, self.manifest), paths=["run"])
        blob, _ = self.make_zip({"a.bin": self.bodies["a.bin"]})
        self.reject(self.store(blob, self.manifest))

    def test_header_central_size_name_method_crc_flags_and_overlap_mismatch(self):
        central = self.blob.index(b"PK\x01\x02")
        # Mutate actual bytes and republish their correct whole-archive digest.
        for offset, fmt, value in ((central + 24, "I", 4), (central + 20, "I", 4),
                                   (central + 8, "H", 1), (central + 8, "H", 0x40),
                                   (central + 10, "H", 99), (6, "H", 1),
                                   (8, "H", 8), (10, "H", 1), (12, "H", 1),
                                   (14, "I", 0), (18, "I", 4),
                                   (22, "I", 4), (central + 42, "I", 1)):
            with self.subTest(offset=offset, value=value):
                blob = bytearray(self.blob)
                struct.pack_into("<" + fmt, blob, offset, value)
                self.reject(self.store(bytes(blob), self.manifest), paths=["empty"])
        blob = bytearray(self.blob)
        blob[30] = ord("z")
        self.reject(self.store(bytes(blob), self.manifest), paths=["empty"])

    def test_bounded_selected_reads_and_no_unselected_open(self):
        original = zipfile.ZipExtFile.read
        calls = []

        def read(stream, size=-1):
            calls.append((stream.name, size))
            return original(stream, size)

        with patch.object(zipfile.ZipExtFile, "read", new=read):
            members.read_members(self.log, self.artifact, ["run", "empty"])
        self.assertEqual(calls, [("empty", 1), ("run", len(self.bodies["run"]) + 1)])

    def test_unsupported_compression_and_descriptor_flags_before_body_read(self):
        central = self.blob.index(b"PK\x01\x02")
        for method, flags in ((12, 0), (14, 0), (0, 8), (0, 0x2000)):
            blob = bytearray(self.blob)
            struct.pack_into("<HH", blob, 6, flags, method)
            struct.pack_into("<HH", blob, central + 8, flags, method)
            artifact = self.store(bytes(blob), self.manifest)
            with self.subTest(method=method, flags=flags), \
                    patch.object(zipfile.ZipFile, "open") as opened:
                self.reject(artifact, paths=["empty"])
                opened.assert_not_called()

    def test_manifest_hex_digest_spelling_is_preserved(self):
        manifest = copy.deepcopy(self.manifest)
        manifest["files"][0]["sha256"] = manifest["files"][0]["sha256"].upper()
        result = members.read_members(self.log, {**self.artifact, "archive": manifest}, ["a.bin"])
        self.assertEqual(result, [{**manifest["files"][0], "body": self.bodies["a.bin"]}])

    def test_deflated_and_zip_bomb_uncompressed_budget_preflight(self):
        blob, manifest = self.make_zip({"bomb": b"x" * 8192, "small": b"ok"},
                                       compression=zipfile.ZIP_DEFLATED)
        artifact = self.store(blob, manifest)
        self.assertLess(len(blob), 8192)
        with patch.object(members, "read_blob") as read:
            self.reject(artifact, paths=["bomb"], max_member_bytes=128)
            read.assert_not_called()
        self.assertEqual(members.read_members(self.log, artifact, ["small"],
                                              max_member_bytes=2, max_total_bytes=2)[0]["body"], b"ok")
        self.assertEqual(members.read_members(self.log, artifact)[0]["body"], b"x" * 8192)

    def test_errors_are_generic_without_body_paths_or_exception_details(self):
        for error in (OSError("C:/sensitive/password"), ValueError("private body"),
                      RuntimeError("credential"), zipfile.BadZipFile("secret")):
            with self.subTest(error=type(error)), patch.object(members, "read_blob", side_effect=error):
                self.reject()
        with patch.object(zipfile.ZipExtFile, "read", return_value=b"short"):
            self.reject(paths=["run"])


if __name__ == "__main__":
    unittest.main()
