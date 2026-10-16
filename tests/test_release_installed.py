"""Installed-smoke guards and real loopback HTTP checks, without model calls."""

from __future__ import annotations

import builtins
from contextlib import contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.request

from scripts import check_installed as smoke


@contextmanager
def local_server(body: bytes):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        if thread.is_alive():
            raise AssertionError("Fixture HTTP server did not stop")


class InstalledSmokeProxyTests(unittest.TestCase):
    def test_explicit_opener_bypasses_available_system_proxy_for_all_smoke_paths(self):
        with local_server(b"installed-resource") as (origin, origin_requests), \
                local_server(b"proxy-response") as (proxy, proxy_requests):
            # This models Windows registry proxy discovery without reading or
            # changing system settings. Both possible destinations are loopback.
            with patch("urllib.request.getproxies", return_value={"http": proxy}) as discovery, \
                    patch("urllib.request.proxy_bypass", return_value=False):
                ambient = urllib.request.build_opener()
                with ambient.open(origin + "/control", timeout=2) as response:
                    self.assertEqual(response.read(), b"proxy-response")
                self.assertEqual(proxy_requests, [origin + "/control"])
                self.assertEqual(origin_requests, [])
                discovery.assert_called_once_with()
                proxy_requests.clear()
                discovery.reset_mock()

                # A pre-existing global opener must not affect smoke requests.
                with patch("urllib.request._opener", ambient):
                    opener = smoke._smoke_opener()
                    paths = ["/", "/assets/main.js", "/api/chain", "/api/tasks/task_fixture"]
                    for path in paths:
                        with self.subTest(path=path), opener.open(origin + path, timeout=2) as response:
                            self.assertEqual(response.status, 200)
                            self.assertEqual(response.read(), b"installed-resource")
                self.assertEqual(origin_requests, paths)
                self.assertEqual(proxy_requests, [])
                discovery.assert_not_called()


class InstalledSmokeGuardTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.installed = self.root / "installed"
        package = self.installed / "cater"
        package.mkdir(parents=True)
        self.bodies = {
            "cater/__init__.py": b"raise AssertionError('fixture must not be imported')\n",
            "cater/cli.py": b"raise AssertionError('fixture CLI must not be imported')\n",
        }
        for name, body in self.bodies.items():
            (self.installed / name).write_bytes(body)
        self.expected = {"files": {name: hashlib.sha256(body).hexdigest() for name, body in self.bodies.items()}}
        self.output = self.root / "installed-check.json"

    def assert_rejected_before_import(self, message, *, isolated=True, no_site=True, installed=None, expected=None):
        process = SimpleNamespace(flags=SimpleNamespace(isolated=isolated, no_site=no_site), path=list(sys.path))
        original_path = list(sys.path)
        original_import = builtins.__import__
        imports = []
        previous_report = self.output.read_bytes() if self.output.exists() else None

        def guarded_import(name, *args, **kwargs):
            if name == "cater" or name.startswith("cater."):
                imports.append(name)
                raise AssertionError("Cater import occurred before rejecting invalid installation")
            return original_import(name, *args, **kwargs)

        with patch.object(smoke, "sys", process), patch("builtins.__import__", side_effect=guarded_import):
            with self.assertRaisesRegex(ValueError, message):
                smoke.check_installed(
                    self.installed if installed is None else installed,
                    self.expected if expected is None else expected,
                    self.output,
                )
        self.assertEqual(imports, [])
        self.assertEqual(process.path, original_path)
        self.assertEqual(sys.path, original_path)
        if previous_report is None:
            self.assertFalse(self.output.exists())
        else:
            self.assertEqual(self.output.read_bytes(), previous_report)
        self.assertFalse(self.output.with_suffix(".db").exists())

    def test_both_isolated_and_no_site_flags_are_required_before_reading_installation(self):
        for isolated, no_site in ((False, False), (False, True), (True, False)):
            with self.subTest(isolated=isolated, no_site=no_site):
                self.assert_rejected_before_import(
                    "require python -I -S", isolated=isolated, no_site=no_site,
                    installed=self.root / "missing-installation",
                )

    def test_existing_report_is_preserved_before_reading_installation(self):
        self.output.write_bytes(b"previous verification evidence\n")
        self.assert_rejected_before_import("overwrite", installed=self.root / "missing-installation")

    def test_working_directory_must_be_outside_installation(self):
        with patch.object(Path, "cwd", return_value=self.installed / "cater"):
            self.assert_rejected_before_import("outside the installation")

    def test_missing_installed_file_is_rejected_before_import(self):
        (self.installed / "cater/cli.py").unlink()
        self.assert_rejected_before_import("file set differs")

    def test_extra_installed_file_is_rejected_before_import(self):
        (self.installed / "cater/stale-resource.json").write_bytes(b"{}\n")
        self.assert_rejected_before_import("file set differs")

    def test_empty_expected_inventory_is_rejected_before_import(self):
        self.assert_rejected_before_import("file set differs", expected={"files": {}})

    def test_missing_package_is_rejected_without_source_fallback(self):
        empty_installation = self.root / "empty-installation"
        empty_installation.mkdir()
        self.assert_rejected_before_import("file set differs", installed=empty_installation)

    def test_modified_installed_bytes_are_rejected_before_import(self):
        (self.installed / "cater/cli.py").write_bytes(b"changed installed content\n")
        self.assert_rejected_before_import("Installed bytes differ")


if __name__ == "__main__":
    unittest.main()
