"""Windows long paths must retain immutable storage checks, not skip them."""
import hashlib
import os
from pathlib import Path
import shutil
import tempfile
import unittest

from cater.artifact_store import ArtifactStoreError, _extended_windows_path, materialize_team_artifact


@unittest.skipUnless(os.name == "nt", "Windows namespace regression")
class TeamArtifactLongPathTests(unittest.TestCase):
    def setUp(self):
        base = Path(tempfile.mkdtemp(prefix="cater-long-artifact-"))
        self.addCleanup(shutil.rmtree, _extended_windows_path(base))
        self.root = base / ("nested-" + "x" * 65)
        body = "Long path content\n"
        self.artifact = {"seq": 123, "filename": "CONTRIBUTING_CHECKLIST.md", "content": body,
                         "content_sha256": hashlib.sha256(body.encode()).hexdigest(), "format": "markdown"}

    def test_long_path_publication_read_and_idempotence(self):
        saved = materialize_team_artifact(self.root, "task", self.artifact)
        self.assertGreater(len(saved), 260)
        self.assertTrue(saved.startswith("\\\\?\\"))
        self.assertEqual(Path(saved).read_text(), self.artifact["content"])
        self.assertEqual(materialize_team_artifact(self.root, "task", self.artifact), saved)

    def test_long_path_modified_file_is_rejected(self):
        saved = materialize_team_artifact(self.root, "task", self.artifact)
        Path(saved).write_text("tampered")
        with self.assertRaises(ArtifactStoreError):
            materialize_team_artifact(self.root, "task", self.artifact)

    def test_parent_traversal_and_device_roots_rejected_before_writes(self):
        for root in (self.root.parent / "link" / ".." / "store",
                     self.root / "link" / ".." / "store",
                     Path("\\\\.\\C:\\store"), Path("\\\\.\\C:\\" + "x" * 160)):
            with self.subTest(root=str(root)), self.assertRaises(ArtifactStoreError):
                materialize_team_artifact(root, "task", self.artifact)
        self.assertEqual(list(self.root.parent.iterdir()), [])

    def test_unc_and_already_extended_path_conversion(self):
        for original, expected in (
            ("C:\\store", "\\\\?\\C:\\store"),
            ("\\\\server\\share\\store", "\\\\?\\UNC\\server\\share\\store"),
            ("\\\\?\\C:\\store", "\\\\?\\C:\\store"),
            ("\\\\?\\UNC\\server\\share\\store", "\\\\?\\UNC\\server\\share\\store"),
        ):
            self.assertEqual(str(_extended_windows_path(Path(original))), expected)

    def test_ambiguous_root_components_rejected_before_creation(self):
        for suffix in (".", " "):
            for padding in ("short", "long" * 30):
                root = self.root.parent / ("store" + suffix) / padding
                with self.subTest(root=str(root)), self.assertRaises(ArtifactStoreError):
                    materialize_team_artifact(root, "task", self.artifact)
        self.assertEqual(list(self.root.parent.iterdir()), [])

    def test_long_path_junction_ancestor_and_version_rejected(self):
        import _winapi

        outside = self.root.parent / "outside"
        outside.mkdir()
        link = self.root.parent / "junction"
        _winapi.CreateJunction(str(outside), str(link))
        self.addCleanup(os.rmdir, link)
        for root in (link / self.root.name, link):
            with self.assertRaises(ArtifactStoreError):
                materialize_team_artifact(root, "task", self.artifact)
        self.assertEqual(list(outside.iterdir()), [])
        saved = Path(materialize_team_artifact(self.root, "task", self.artifact))
        saved.unlink()
        saved.parent.rmdir()
        _winapi.CreateJunction(str(outside), str(saved.parent))
        self.addCleanup(os.rmdir, saved.parent)
        with self.assertRaises(ArtifactStoreError):
            materialize_team_artifact(self.root, "task", self.artifact)
        self.assertEqual(list(outside.iterdir()), [])

    def test_long_path_hard_link_is_rejected(self):
        saved = materialize_team_artifact(self.root, "task", self.artifact)
        os.link(saved, saved + ".link")
        with self.assertRaises(ArtifactStoreError):
            materialize_team_artifact(self.root, "task", self.artifact)

    def test_long_path_file_and_lock_symlinks_rejected(self):
        outside = self.root.parent / "outside.txt"
        outside.write_bytes(b"unchanged")
        probe = self.root.parent / "probe-link"
        try:
            probe.symlink_to(outside)
        except OSError as exc:
            if getattr(exc, "winerror", None) == 1314:
                self.skipTest("Windows symlink privilege unavailable")
            raise
        probe.unlink()
        saved = Path(materialize_team_artifact(self.root, "task", self.artifact))
        for target in (saved, saved.parent.parent / ".publish.lock"):
            original = target.read_bytes()
            target.unlink()
            try:
                for destination in (outside, outside.with_name("absent.txt")):
                    target.symlink_to(destination)
                    try:
                        with self.assertRaises((ArtifactStoreError, OSError)):
                            materialize_team_artifact(self.root, "task", self.artifact)
                        self.assertEqual(outside.read_bytes(), b"unchanged")
                        self.assertFalse(outside.with_name("absent.txt").exists())
                    finally:
                        target.unlink()
            finally:
                target.write_bytes(original)

    def test_short_paths_keep_existing_public_representation(self):
        with tempfile.TemporaryDirectory() as short:
            artifact = {**self.artifact, "filename": "a.txt"}
            saved = materialize_team_artifact(short, "task", artifact)
            self.assertFalse(saved.startswith("\\\\?\\"))

    def test_windows_limit_counts_utf16_units_not_python_characters(self):
        base = Path(tempfile.mkdtemp(prefix="c"))
        self.addCleanup(shutil.rmtree, _extended_windows_path(base))
        artifact = {**self.artifact, "filename": "\U0001f408" * 20 + ".md"}
        suffix = Path("team-" + hashlib.sha256(b"task").hexdigest()) / (
            "123-" + artifact["content_sha256"]) / artifact["filename"]
        padding = 247 - len(str(base / suffix)) - 1
        if padding < 1:
            self.skipTest("Temporary root too long for the UTF-16 boundary fixture")
        root = base / ("x" * padding)
        target = root / suffix
        self.assertEqual(len(str(target)), 247)
        self.assertGreater(len(str(target).encode("utf-16-le")) // 2, 260)
        saved = materialize_team_artifact(root, "task", artifact)
        self.assertTrue(saved.startswith("\\\\?\\"))
        self.assertEqual(Path(saved).read_text(), artifact["content"])
