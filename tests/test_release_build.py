"""Release orchestration boundaries; no model calls or real package installs."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts import build_release as release


ROOT = Path(__file__).resolve().parents[1]


class ReleaseBuildTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "repo"
        self.root.mkdir()
        (self.root / "pyproject.toml").write_bytes((ROOT / "pyproject.toml").read_bytes())
        (self.root / "README.md").write_text("Release fixture\n", encoding="utf-8")
        (self.root / "LICENSE").write_bytes((ROOT / "LICENSE").read_bytes())
        (self.root / "cater").mkdir()
        (self.root / "cater/__init__.py").write_text('__version__ = "0.1.0"\n', encoding="utf-8")
        self.ui = self.root / "cater/static/workroom"
        (self.ui / "assets").mkdir(parents=True)
        (self.ui / "index.html").write_text('<script type="module" src="/assets/main.js"></script><link rel="stylesheet" href="/assets/main.css">', encoding="utf-8")
        (self.ui / "assets/main.js").write_text("export const version = 1\n", encoding="utf-8")
        (self.ui / "assets/main.css").write_text("body { color: green }\n", encoding="utf-8")
        self.manifest = {"index.html": {"file": "assets/main.js", "src": "index.html", "isEntry": True,
                                        "css": ["assets/main.css"]}}
        (self.ui / release.MANIFEST_NAME).write_text(json.dumps(self.manifest), encoding="utf-8")
        self.output = self.base / "release"

    def fake_step(self, label, command, *, cwd, output, timeout):
        self.commands.append((label, command, cwd, timeout))
        if label == "toolchain":
            return SimpleNamespace(stdout_preview=json.dumps({"python": "3.11", "pip": "fixture", "setuptools": "fixture"}))
        if label == "build":
            (output / "wheels/cater-0.1.0-py3-none-any.whl").write_bytes(b"wheel-fixture")
        if label == "smoke":
            expected = json.loads((output / "expected.json").read_text(encoding="utf-8"))
            (cwd / "installed-check.json").write_text(json.dumps({"status": "verified", "ui_version": expected["ui_version"],
                "python": "3.11", "checks": ["fixture_only"]}), encoding="utf-8")
        return None

    def fake_validation(self, wheel, expected, **kwargs):
        return {"name": "cater", "version": "0.1.0", "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
                "files": {name: hashlib.sha256(body).hexdigest() for name, body in expected.items()}}

    def run_fake(self, step=None):
        self.commands = []
        with patch.object(release, "_step", side_effect=step or self.fake_step), \
             patch.object(release, "validate_wheel", side_effect=self.fake_validation):
            return release.build_release(self.root, self.output)

    def test_snapshot_excludes_dirty_root_build_egg_info_and_development_dist(self):
        before = release.source_snapshot(self.root)
        for name in ("build/lib/cater/static/workroom/assets/stale.js", "cater.egg-info/SOURCES.txt",
                     "web/dist/index.html", ".cater/secret.json", "node_modules/private.js"):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("Do not distribute", encoding="utf-8")
        after = release.source_snapshot(self.root)
        self.assertEqual(before.fingerprint, after.fingerprint)
        self.assertEqual(before.files, after.files)
        self.assertTrue(all(name.startswith("cater/") for name in after.package))

    def test_stale_unregistered_asset_or_unknown_root_resource_is_rejected(self):
        for name in ("assets/stale.js", "private.txt"):
            with self.subTest(name=name):
                path = self.ui / name
                path.write_bytes(b"stale")
                with self.assertRaisesRegex(release.ReleaseError, "unregistered"):
                    release.source_snapshot(self.root)
                path.unlink()

    def test_missing_bundle_manifest_does_not_fall_back_to_development_dist(self):
        (self.ui / release.MANIFEST_NAME).unlink()
        development = self.root / "web/dist"
        development.mkdir(parents=True)
        (development / "index.html").write_text("not the package", encoding="utf-8")
        with self.assertRaisesRegex(release.ReleaseError, "manifest"):
            release.source_snapshot(self.root)

    def test_registered_package_data_is_exact_and_working_bytes_are_not_old_head(self):
        folder = self.root / "cater/packs"
        folder.mkdir()
        (folder / "roles.json").write_bytes(b'{"new":true}')
        snapshot = release.source_snapshot(self.root)
        self.assertEqual(snapshot.package["cater/packs/roles.json"], b'{"new":true}')
        self.assertNotIn("README.md", snapshot.package)
        (folder / "roles.json").write_bytes(b'{"new":false}')
        self.assertNotEqual(release.source_snapshot(self.root).fingerprint, snapshot.fingerprint)

    def test_manifest_change_during_capture_is_rejected(self):
        capture = release.capture_web_assets
        def changed(root):
            result = capture(root)
            path = root / release.MANIFEST_NAME
            path.write_bytes(path.read_bytes() + b" ")
            return result
        with patch.object(release, "capture_web_assets", side_effect=changed), \
             self.assertRaisesRegex(release.ReleaseError, "manifest changed"):
            release.source_snapshot(self.root)

    def test_runtime_dependency_or_unsafe_data_pattern_is_rejected(self):
        project = self.root / "pyproject.toml"
        original = project.read_text(encoding="utf-8")
        for changed in (original.replace("dependencies = []", 'dependencies = ["requests"]'),
                        original.replace("packs/*.json", "../../private/*"),
                        original.replace('"cater*"', '"other*"')):
            with self.subTest(changed=changed[-100:]):
                project.write_text(changed, encoding="utf-8")
                with self.assertRaises(ValueError):
                    release.source_snapshot(self.root)

    def test_source_byte_limit_is_checked_before_reading_the_large_member(self):
        path = self.root / "cater/big.py"
        path.write_bytes(b"x" * 4096)
        with patch.object(release, "MAX_BYTES", 3000), patch.object(release, "_read", wraps=release._read) as read:
            with self.assertRaisesRegex(release.ReleaseError, "byte limit"):
                release.source_snapshot(self.root)
            self.assertNotIn(path, [call.args[0] for call in read.call_args_list])

    def test_existing_output_is_preserved_and_no_command_runs(self):
        self.output.mkdir()
        existing = self.output / "previous.whl"
        existing.write_bytes(b"preserve")
        with patch.object(release, "_step") as step, self.assertRaisesRegex(release.ReleaseError, "already exists"):
            release.build_release(self.root, self.output)
        step.assert_not_called()
        self.assertEqual(existing.read_bytes(), b"preserve")

    def test_output_cannot_be_inside_package_or_source_metadata(self):
        for name in ("cater/new-release", ".git/new-release", "web/new-release", "scripts/new-release", "tests/new-release", "."):
            with self.subTest(name=name), self.assertRaises(release.ReleaseError):
                release.build_release(self.root, self.root / name)

    def test_success_stages_exact_files_uses_offline_pip_and_isolated_installed_check(self):
        expected = release.source_snapshot(self.root)
        report = self.run_fake()
        self.assertEqual(report["status"], "verified")
        self.assertEqual(report["source_sha256"], expected.fingerprint)
        self.assertEqual({path.relative_to(self.output / "source").as_posix(): path.read_bytes()
                          for path in (self.output / "source").rglob("*") if path.is_file()}, expected.files)
        self.assertEqual([row[0] for row in self.commands], ["toolchain", "build", "install", "smoke"])
        for label, command, _, _ in self.commands:
            self.assertIn("-I", command)
            if label in {"build", "install"}:
                for option in ("--isolated", "--no-index", "--no-deps"):
                    self.assertIn(option, command)
            if label == "smoke":
                self.assertIn("-S", command)
        self.assertEqual(json.loads((self.output / "release.json").read_text())["status"], "verified")

    def test_failed_build_keeps_evidence_without_creating_accepted_release(self):
        def fail(*args, **kwargs):
            raise release.ReleaseError("injected failure")
        with self.assertRaisesRegex(release.ReleaseError, "injected"):
            self.run_fake(fail)
        report = json.loads((self.output / "release.json").read_text())
        self.assertEqual(report["status"], "failed")
        self.assertTrue((self.output / "source/pyproject.toml").is_file())
        self.assertNotIn("wheel", report)

    def test_source_changed_during_build_is_not_accepted(self):
        def changed(label, *args, **kwargs):
            result = self.fake_step(label, *args, **kwargs)
            if label == "smoke":
                (self.root / "README.md").write_text("New source", encoding="utf-8")
            return result
        with self.assertRaisesRegex(release.ReleaseError, "Source changed"):
            self.run_fake(changed)
        self.assertEqual(json.loads((self.output / "release.json").read_text())["status"], "failed")

    def test_secrets_provider_configuration_and_python_overrides_are_not_inherited(self):
        with patch.dict(os.environ, {"CATER_INTERN_API_KEY": "PRIVATE", "OPENAI_API_KEY": "PRIVATE",
                                    "PIP_INDEX_URL": "PRIVATE", "PIP_CONFIG_FILE": "PRIVATE",
                                    "PIP_REQUIREMENT": "PRIVATE", "PYTHONPATH": "PRIVATE", "SECRET": "PRIVATE"}):
            env = release.build_environment()
        self.assertFalse(any(value == "PRIVATE" for value in env.values()))
        self.assertNotIn("PYTHONPATH", env)
        self.assertNotIn("PIP_REQUIREMENT", env)
        self.assertEqual(env["PIP_CONFIG_FILE"], os.devnull)
        self.assertTrue(any(key.upper() == "PATH" for key in env))

    def test_step_preserves_process_receipt_and_does_not_retry_a_failure(self):
        self.output.mkdir()
        result = SimpleNamespace(payload={"outcome": "timeout", "stdout": {"complete": True}})
        with patch.object(release, "run_check", return_value=result) as check, \
             self.assertRaisesRegex(release.ReleaseError, "build failed"):
            release._step("build", ["python"], cwd=self.root, output=self.output, timeout=0.1)
        self.assertEqual(check.call_count, 1)
        self.assertEqual(json.loads((self.output / "build.json").read_text())["outcome"], "timeout")


if __name__ == "__main__":
    unittest.main()
