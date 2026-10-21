"""Bootstrap selection and real Windows venv containment, without external tools."""

from contextlib import ExitStack
import importlib.util
import json
import ntpath
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import venv

import cater
from cater import processes
from cater.acp import AcpSession, AcpUnavailable
from cater.drivers import CliDriver, DriverUnavailable
from cater.verification import run_check
from cater.workspace_tools import WorkspaceExecutor, WorkspacePolicy


FIXTURE = Path(__file__).resolve().parent / "fixtures" / "worker_interpreter_peer.py"
PACKAGE_ROOT = Path(cater.__file__).resolve().parent.parent


def windows_runtime(base=None, *, virtual=True):
    return SimpleNamespace(executable=r"C:\fixture-venv\Scripts\python.exe",
                           _base_executable=base, prefix="venv" if virtual else "base",
                           base_prefix="base")


class WorkerInterpreterSelectionTests(unittest.TestCase):
    def select(self, runtime, platform="nt"):
        with patch.object(processes, "sys", runtime), patch.object(
                processes, "os", SimpleNamespace(name=platform, path=ntpath)):
            return processes.worker_python()

    def test_non_windows_keeps_the_configured_interpreter(self):
        for base in (None, "", "relative-python", "/different/base/python"):
            with self.subTest(base=base):
                runtime = windows_runtime(base)
                runtime.executable = "/temporary/venv/bin/python"
                self.assertEqual(self.select(runtime, "posix"), runtime.executable)

    def test_windows_uses_absolute_base_in_and_out_of_a_venv(self):
        for virtual in (False, True):
            for base in (r"C:\base python\python.exe", r"\\server\share\python.exe"):
                with self.subTest(virtual=virtual, base=base):
                    self.assertEqual(self.select(windows_runtime(base, virtual=virtual)), base)

    def test_windows_venv_rejects_missing_or_invalid_base(self):
        for base in (None, "", " ", "python.exe", r"C:python.exe", r"\python.exe", "/python.exe", 1, [], Path("python")):
            with self.subTest(base=base), self.assertRaises(ValueError):
                self.select(windows_runtime(base))
        runtime = windows_runtime()
        del runtime._base_executable
        with self.assertRaises(ValueError):
            self.select(runtime)

    def test_windows_non_venv_falls_back_without_a_usable_base(self):
        for base in (None, "", "python.exe", 1):
            with self.subTest(base=base):
                runtime = windows_runtime(base, virtual=False)
                self.assertEqual(self.select(runtime), runtime.executable)


class WorkerInterpreterFailClosedTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="cater-worker-refusal-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.executor = WorkspaceExecutor(WorkspacePolicy.from_dict({
            "root": str(self.root), "mode": "execute", "commands": {"fixture": [sys.executable]},
        }))
        self.enterContext(patch.object(processes, "sys", windows_runtime(None)))
        self.enterContext(patch.object(processes, "os", SimpleNamespace(name="nt", path=ntpath)))
        self.popen = self.enterContext(patch(
            "subprocess.Popen", side_effect=AssertionError("No bootstrap or configured target may start")))
        self.enterContext(patch("cater.verification._git_state", return_value={"head": None, "dirty": None}))

    def tearDown(self):
        self.popen.assert_not_called()

    def test_acp_refuses_before_starting_any_process(self):
        session = AcpSession(["configured-target-must-not-start"], cwd=self.root)
        self.addCleanup(session.close)
        with self.assertRaises(AcpUnavailable):
            session.open()
        self.assertIsNone(session._proc)

    def test_cli_refuses_before_starting_any_process(self):
        driver = CliDriver("fixture", ["configured-target-must-not-start"], cwd=self.root, restart_limit=0)
        self.addCleanup(driver.close)
        with self.assertRaises(DriverUnavailable):
            driver.complete(role="fixture", prompt="offline", context={})
        self.assertIsNone(driver._proc)

    def test_workspace_refuses_before_starting_any_process(self):
        result = self.executor.execute("workspace_run", {"command": "fixture"})
        self.assertEqual(result["status"], "error", result)
        self.assertEqual(result["outcome"], "launch_error", result)
        self.assertEqual(result["argv"], [])

    def test_verification_refuses_before_starting_any_process(self):
        result = run_check(["configured-target-must-not-start"], cwd=self.root)
        self.assertEqual(result.payload["outcome"], "launch_error")
        self.assertIsNone(result.payload["exit_code"])


@unittest.skipUnless(os.name == "nt", "Windows venv redirector and Job regression")
class WindowsWorkerInterpreterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        temporary = tempfile.TemporaryDirectory(prefix="cater-worker-venv-")
        cls.addClassCleanup(temporary.cleanup)
        cls.root = Path(temporary.name)
        venv.EnvBuilder(with_pip=False, symlinks=False).create(cls.root / "venv")
        cls.python = cls.root / "venv" / "Scripts" / "python.exe"
        spec = importlib.util.spec_from_file_location("worker_interpreter_fixture", FIXTURE)
        cls.fixture = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.fixture)

    def exercise(self, entry):
        folder = self.root / entry
        folder.mkdir()
        with ExitStack() as stack:
            output = stack.enter_context((folder / "runner.txt").open("w", encoding="utf-8"))
            process = subprocess.Popen([
                processes.worker_python(), "-I", "-B", str(FIXTURE), "guard",
                str(self.python), entry, str(folder), str(PACKAGE_ROOT),
            ], cwd=folder, stdin=subprocess.PIPE, stdout=output, stderr=subprocess.STDOUT)
            job = None
            handles = self.fixture.ProcessHandles()
            try:
                handles.capture_popen(process)
                from cater._windows_job import WindowsJob

                job = WindowsJob(process.pid)
                process.stdin.write(b"start-private-fixture\n")
                process.stdin.close()
                code = process.wait(timeout=60)
                output.flush()
                diagnostic = (folder / "runner.txt").read_text(encoding="utf-8")
                self.assertEqual(code, 0, diagnostic)
                self.assertEqual(handles.pids(job), [], diagnostic)
                report = json.loads((folder / "report.json").read_text(encoding="utf-8"))
                self.assertEqual(report["errors"], [], report)
                self.assertTrue(report["all_original_handles_stopped"], report)
                self.assertEqual(report["forced_terminations"], [], report)
                self.assertEqual(report["remaining_fixture_pids"], [], report)
                self.assertEqual(Path(report["case_python"]), self.python)
                self.assertEqual(Path(report["package_file"]), Path(cater.__file__).resolve())
                self.assertEqual(report["before_assign_descendants"], [], report)
                self.assertTrue(report["all_target_job_members"], report)
                self.assertEqual(report["target_argv"], report["expected_argv"], report)
                self.assertEqual(Path(report["target_python"]), self.python)
                self.assertEqual(Path(report["target_prefix"]), self.python.parent.parent)
                self.assertEqual(Path(report["bootstrap_image"]), Path(report["base_python"]))
                self.assertEqual(report["result"], "end_turn" if entry == "acp" else
                                 "fixture-ready" if entry == "cli" else "timeout", report)
                self.assertTrue(report["pipes_complete"], report)
            finally:
                if job is not None:
                    handles.capture_job(job)
                    job.close()
                handles.stop_and_close()
                process.wait(timeout=5)
                if process.stdin is not None:
                    process.stdin.close()

    def test_acp_uses_direct_worker_and_keeps_configured_target(self):
        self.exercise("acp")

    def test_cli_uses_direct_worker_and_keeps_configured_target(self):
        self.exercise("cli")

    def test_workspace_timeout_cleans_up_the_configured_venv_target(self):
        self.exercise("workspace")

    def test_verification_timeout_cleans_up_the_configured_venv_target(self):
        self.exercise("verification")


if __name__ == "__main__":
    unittest.main()
