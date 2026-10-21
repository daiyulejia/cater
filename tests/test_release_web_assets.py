"""External UI release boundaries, using local fixtures and injected build steps."""

from __future__ import annotations

import hashlib
import io
import json
import os
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts import build_release as release


ROOT = Path(__file__).resolve().parents[1]
PREFIX = "cater/static/workroom/"


def bundle(root: Path, label: str) -> None:
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_bytes(b'<script type="module" src="/assets/main.js"></script>')
    (root / "assets/main.js").write_text(f"export const label = '{label}';\n", encoding="utf-8")
    manifest = {"index.html": {"file": "assets/main.js", "src": "index.html", "isEntry": True}}
    (root / release.MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")


def tree(root: Path) -> dict:
    return {p.relative_to(root).as_posix(): (p.read_bytes() if p.is_file() else None,
            p.stat().st_mtime_ns, p.stat().st_mode) for p in (root, *root.rglob("*"))}


@contextmanager
def directory_alias(target: Path, link: Path):
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)
    try:
        yield link
    finally:
        if os.name == "nt":
            os.rmdir(link)
        else:
            link.unlink()


class ReleaseWebAssetsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="cater-release-web-assets-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "repo"
        self.root.mkdir()
        for name in ("pyproject.toml", "LICENSE"):
            (self.root / name).write_bytes((ROOT / name).read_bytes())
        (self.root / "README.md").write_bytes(b"Release fixture\n")
        (self.root / "cater").mkdir()
        (self.root / "cater/__init__.py").write_bytes(b'__version__ = "0.1.0"\n')
        self.old = self.root / "cater/static/workroom"
        self.ui = self.base / "external-ui"
        bundle(self.old, "old")
        bundle(self.ui, "selected")
        self.output = self.base / "release"
        self.commands = []

    def fake_step(self, label, command, *, cwd, output, timeout):
        self.commands.append((label, command, cwd))
        if label == "toolchain":
            return SimpleNamespace(stdout_preview=json.dumps({"python": "fixture", "pip": "fixture", "setuptools": "fixture"}))
        if label == "build":
            (output / "wheels/cater-0.1.0-py3-none-any.whl").write_bytes(b"fixture")
        if label == "smoke":
            expected = json.loads((output / "expected.json").read_bytes())
            (cwd / "installed-check.json").write_text(json.dumps({"status": "verified",
                "ui_version": expected["ui_version"], "checks": ["fixture_only"], "python": "fixture"}), encoding="utf-8")

    def fake_validation(self, wheel, expected, **kwargs):
        self.validated = dict(expected)
        return {"version": "0.1.0", "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
                "files": {name: hashlib.sha256(body).hexdigest() for name, body in expected.items()}}

    def build(self, step=None):
        with patch.object(release, "_step", side_effect=step or self.fake_step), \
                patch.object(release, "validate_wheel", side_effect=self.fake_validation):
            return release.build_release(self.root, self.output, web_assets=self.ui)

    def reject_before_build(self, *, output=None, ui=None, message=None):
        with patch.object(release, "_step") as step, \
                self.assertRaisesRegex((ValueError, OSError), message or ".+"):
            release.build_release(self.root, output or self.output, web_assets=self.ui if ui is None else ui)
        step.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_default_bytes_and_fingerprint_are_unchanged(self):
        default = release.source_snapshot(self.root)
        self.assertEqual(default, release.source_snapshot(self.root, web_assets=None))
        expected = {name: (self.root / name).read_bytes() for name in (
            "pyproject.toml", "README.md", "LICENSE", "cater/__init__.py",
            PREFIX + "index.html", PREFIX + release.MANIFEST_NAME, PREFIX + "assets/main.js")}
        self.assertEqual(default.files, expected)
        digest = hashlib.sha256()
        for name, body in sorted(expected.items()):
            encoded = name.encode("utf-8")
            digest.update(len(encoded).to_bytes(4, "big") + encoded + len(body).to_bytes(8, "big") + body)
        self.assertEqual(default.fingerprint, digest.hexdigest())
        self.assertEqual(default.ui_version, release.capture_web_assets(self.old).version)

    def test_selected_bytes_include_manifest_and_exclude_all_old_workroom_reads(self):
        (self.old / "stale.py").write_bytes(b"do not include or inspect")
        (self.old / release.MANIFEST_NAME).write_bytes(b"invalid old manifest")
        (self.old / ".vite").mkdir()
        inspect, scandir = release._inspect, os.scandir

        def guarded_inspect(path, *args):
            self.assertFalse(Path(path).is_relative_to(self.old))
            return inspect(path, *args)

        def guarded_scandir(path):
            self.assertFalse(Path(path).is_relative_to(self.old))
            return scandir(path)

        with patch.object(release, "_inspect", side_effect=guarded_inspect), \
                patch("os.scandir", side_effect=guarded_scandir):
            snapshot = release.source_snapshot(self.root, web_assets=self.ui)
        expected = {PREFIX + p.relative_to(self.ui).as_posix(): p.read_bytes()
                    for p in self.ui.rglob("*") if p.is_file()}
        self.assertEqual({n:b for n,b in snapshot.package.items() if n.startswith(PREFIX)}, expected)
        self.assertEqual(snapshot.ui_version, release.capture_web_assets(self.ui).version)

    def test_missing_old_ui_builds_selected_snapshot_without_input_writes(self):
        shutil.rmtree(self.old)
        before = tree(self.root), tree(self.ui)
        snapshot = release.source_snapshot(self.root, web_assets=self.ui)
        report = self.build()
        self.assertEqual(report["status"], "verified")
        self.assertEqual(report["ui_version"], snapshot.ui_version)
        self.assertEqual(report["source_sha256"], snapshot.fingerprint)
        self.assertEqual(report["web_assets"], str(self.ui.absolute()))
        self.assertEqual(self.validated, snapshot.package)
        staged = self.output / "source"
        self.assertEqual({p.relative_to(staged).as_posix(): p.read_bytes() for p in staged.rglob("*") if p.is_file()}, snapshot.files)
        self.assertEqual(before, (tree(self.root), tree(self.ui)))
        self.assertEqual([row[0] for row in self.commands], ["toolchain", "build", "install", "smoke"])

    def test_old_ui_changes_do_not_change_selected_fingerprint(self):
        first = release.source_snapshot(self.root, web_assets=self.ui)
        (self.old / "assets/main.js").write_bytes(b"changed old UI")
        self.assertEqual(release.source_snapshot(self.root, web_assets=self.ui), first)
        (self.ui / "assets/main.js").write_bytes(b"changed selected UI")
        changed = release.source_snapshot(self.root, web_assets=self.ui)
        self.assertNotEqual(changed.fingerprint, first.fingerprint)
        self.assertNotEqual(changed.ui_version, first.ui_version)
        (self.root / "cater/__init__.py").write_bytes(b"# changed backend\n")
        backend = release.source_snapshot(self.root, web_assets=self.ui)
        self.assertNotEqual(backend.fingerprint, changed.fingerprint)
        self.assertEqual(backend.ui_version, changed.ui_version)

    def test_missing_selected_manifest_or_resource_fails_without_fallback(self):
        for name in (release.MANIFEST_NAME, "index.html", "assets/main.js"):
            with self.subTest(name=name):
                path = self.ui / name
                body = path.read_bytes()
                path.unlink()
                try:
                    self.reject_before_build()
                finally:
                    path.write_bytes(body)

    def test_bad_manifests_are_rejected(self):
        for raw in (b"{", b"[]", b'{"x":{},"x":{}}', b'{"x":{"file":"../private.js"}}'):
            with self.subTest(raw=raw):
                (self.ui / release.MANIFEST_NAME).write_bytes(raw)
                self.reject_before_build()

    def test_unregistered_assets_and_root_resources_are_rejected(self):
        for name in ("assets/stale.js", "private.txt"):
            with self.subTest(name=name):
                path = self.ui / name
                path.write_bytes(b"unregistered")
                try:
                    self.reject_before_build(message="unregistered")
                finally:
                    path.unlink()

    def test_manifest_changes_between_read_and_capture_are_rejected(self):
        capture = release.capture_web_assets

        def changed(path):
            manifest = self.ui / release.MANIFEST_NAME
            manifest.write_bytes(manifest.read_bytes() + b" ")
            return capture(path)

        with patch.object(release, "capture_web_assets", side_effect=changed):
            self.reject_before_build(message="manifest changed")

    def test_manifest_or_asset_changes_after_capture_are_rejected(self):
        capture = release.capture_web_assets
        for name in (release.MANIFEST_NAME, "assets/main.js"):
            with self.subTest(name=name):
                def changed(path):
                    result = capture(path)
                    target = self.ui / name
                    target.write_bytes(target.read_bytes() + b" ")
                    return result

                with patch.object(release, "capture_web_assets", side_effect=changed):
                    self.reject_before_build(message="changed")

    def test_ui_change_during_capture_fails_closed(self):
        from cater import web_assets
        read = web_assets._read_asset

        def changed(path, expected):
            body = read(path, expected)
            if path.name == "main.js":
                path.write_bytes(body + b" ")
            return body

        with patch.object(web_assets, "_read_asset", side_effect=changed):
            self.reject_before_build(message="changed")

    def test_final_capture_rejects_late_unregistered_root_file_or_hard_link(self):
        capture = release.capture_web_assets
        for linked in (False, True):
            with self.subTest(linked=linked):
                calls = 0
                target = self.ui / "unregistered.txt"

                def changed(path):
                    nonlocal calls
                    calls += 1
                    if calls == 2:
                        if linked:
                            os.link(self.ui / "assets/main.js", target)
                        else:
                            target.write_bytes(b"not registered")
                    return capture(path)

                try:
                    with patch.object(release, "capture_web_assets", side_effect=changed):
                        self.reject_before_build()
                    self.assertEqual(calls, 2)
                finally:
                    target.unlink(missing_ok=True)

    def test_backend_or_selected_ui_changes_during_build_are_not_accepted(self):
        for name in ("backend", "ui", "manifest", "extra"):
            with self.subTest(name=name):
                self.output = self.base / ("release-" + name)

                def changed(label, *args, **kwargs):
                    result = self.fake_step(label, *args, **kwargs)
                    if label == "smoke":
                        path = {"backend": self.root / "cater/__init__.py", "ui": self.ui / "assets/main.js",
                                "manifest": self.ui / release.MANIFEST_NAME, "extra": self.ui / "assets/extra.js"}[name]
                        path.write_bytes((path.read_bytes() if path.exists() else b"") + b" ")
                    return result

                with self.assertRaises(ValueError):
                    self.build(step=changed)
                report = json.loads((self.output / "release.json").read_bytes())
                self.assertEqual(report["status"], "failed")
                self.assertNotIn("wheel", report)
                self.assertTrue((self.output / "source" / PREFIX / release.MANIFEST_NAME).exists())

    def test_old_ui_changes_during_build_are_irrelevant(self):
        def changed(label, *args, **kwargs):
            result = self.fake_step(label, *args, **kwargs)
            if label == "smoke":
                shutil.rmtree(self.old)
            return result

        self.assertEqual(self.build(step=changed)["status"], "verified")

    def test_output_inside_selected_ui_is_rejected_without_writes(self):
        before = tree(self.ui)
        for output in (self.ui, self.ui / "new-release", self.ui / "assets/nested/release"):
            with self.subTest(output=output):
                self.reject_before_build(output=output, message="selected UI")
        self.assertEqual(tree(self.ui), before)

    def test_ui_parent_traversal_and_missing_root_are_rejected(self):
        for path in (self.ui / ".." / self.ui.name, self.base / "missing"):
            with self.subTest(path=path):
                self.reject_before_build(ui=path)

    def test_existing_output_preserves_evidence(self):
        self.output.mkdir()
        receipt = self.output / "previous.txt"
        receipt.write_bytes(b"preserve")
        before = tree(self.output)
        with patch.object(release, "source_snapshot") as snapshot, patch.object(release, "_step") as step, \
                self.assertRaisesRegex(ValueError, "already exists"):
            release.build_release(self.root, self.output, web_assets=self.ui)
        snapshot.assert_not_called()
        step.assert_not_called()
        self.assertEqual(tree(self.output), before)

    def test_hard_linked_selected_file_is_rejected(self):
        target = self.ui / "assets/main.js"
        other = self.base / "linked.js"
        os.link(target, other)
        self.reject_before_build(message="linked")

    def test_ui_root_and_ancestor_reparse_points_are_rejected(self):
        original = Path.lstat
        for target in (self.ui, self.base):
            with self.subTest(target=target):
                def linked(path, *args, **kwargs):
                    info = original(path, *args, **kwargs)
                    if path == target:
                        return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
                    return info

                with patch.object(Path, "lstat", linked):
                    self.reject_before_build(message="reparse")

    def test_output_alias_inside_ui_and_other_linked_parents_do_not_write(self):
        alias = self.base / "ui-alias"
        before = tree(self.ui)
        with directory_alias(self.ui, alias):
            self.reject_before_build(output=alias / "nested/release", message="selected UI")
        other = self.base / "other"
        other.mkdir()
        with directory_alias(other, alias):
            self.reject_before_build(output=alias / "nested/release", message="reparse")
        self.assertEqual(tree(self.ui), before)
        self.assertEqual(list(other.iterdir()), [])

    def test_real_directory_links_at_selected_root_ancestor_and_assets_are_rejected(self):
        alias = self.base / "ui-alias"
        with directory_alias(self.ui, alias):
            self.reject_before_build(ui=alias, message="reparse")
        with directory_alias(self.base, alias):
            self.reject_before_build(ui=alias / self.ui.name, message="reparse")
        with directory_alias(self.base, self.ui / "assets/linked"):
            self.reject_before_build(message="reparse")

    def test_external_file_and_total_limits_are_enforced(self):
        with patch.object(release, "MAX_MANIFEST_BYTES", 1):
            self.reject_before_build(message="manifest exceeds")
        baseline = release.source_snapshot(self.root)
        with patch.object(release, "MAX_BYTES", sum(map(len, baseline.files.values()))):
            (self.ui / "assets/main.js").write_bytes(b"x" * 4096)
            self.reject_before_build(message="byte limit")
        with patch.object(release, "MAX_FILES", 3):
            self.reject_before_build(message="file count")

    def test_cli_passes_selected_assets_and_output_to_builder(self):
        with patch.object(release, "build_release", return_value={"status": "verified"}) as build, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(release.main(["--source", str(self.root), "--web-assets", str(self.ui),
                "--output", str(self.output), "--timeout", "180"]), 0)
        build.assert_called_once_with(self.root, self.output, timeout=180, web_assets=self.ui)


if __name__ == "__main__":
    unittest.main()
