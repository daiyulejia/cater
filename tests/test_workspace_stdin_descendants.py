"""Real Windows regression for an exiting target with stdin-holding descendants."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from cater import workspace_tools
from cater.workspace_tools import WorkspaceExecutor, WorkspacePolicy


class _ProcessHandles:
    def __init__(self):
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        self.kernel.OpenProcess.restype = ctypes.c_void_p
        self.kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        self.kernel.WaitForSingleObject.restype = ctypes.c_uint32
        self.kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        self.kernel.GetExitCodeProcess.restype = ctypes.c_int
        self.kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        self.kernel.CloseHandle.restype = ctypes.c_int
        self.handles = {}

    def open(self, label, pid):
        handle = self.kernel.OpenProcess(0x100000 | 0x1000, False, pid)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self.handles[label] = handle

    def wait_and_code(self, label):
        handle = self.handles[label]
        if self.kernel.WaitForSingleObject(handle, 5000) != 0:
            raise AssertionError(label + " is not confirmed terminated")
        code = ctypes.c_uint32()
        if not self.kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
            raise ctypes.WinError(ctypes.get_last_error())
        return code.value

    def close(self):
        for handle in self.handles.values():
            self.kernel.CloseHandle(handle)
        self.handles.clear()


@unittest.skipUnless(os.name == "nt", "requires Windows process handles and Job cleanup; POSIX equivalence unverified")
class WorkspaceStdinDescendantTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="cater-stdin-descendants-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "project"
        self.root.mkdir()
        temporary_root = self.base / "tmp"
        temporary_root.mkdir()
        temp_patch = patch.object(tempfile, "tempdir", str(temporary_root))
        temp_patch.start()
        self.addCleanup(temp_patch.stop)

    def executor(self, script):
        return WorkspaceExecutor(WorkspacePolicy.from_dict({
            "root": str(self.root), "mode": "execute", "timeout": 10,
            "commands": {"probe": [sys.executable, "-I", "-B", "-X", "utf8", "-c", script]},
        }))

    def assert_tree_exit(self, exit_code):
        child_ready = self.root / "child-ready"
        tree_ready = self.root / "tree-ready.json"
        observer_ready = self.root / "observer-ready"
        leaked = self.root / "late-write"
        child = (
            "import time; from pathlib import Path; "
            f"Path({str(child_ready)!r}).write_text('ready'); "
            f"release=Path({str(observer_ready)!r})\n"
            "while not release.exists(): time.sleep(.01)\n"
            "time.sleep(3)\n"
            f"Path({str(leaked)!r}).write_text('descendant survived target exit')\n"
            "time.sleep(30)"
        )
        target = (
            "import json,os,subprocess,sys,time; from pathlib import Path; "
            f"child=subprocess.Popen([sys.executable,'-I','-B','-c',{child!r}],"
            "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
            f"ready=Path({str(child_ready)!r})\n"
            "while not ready.exists(): time.sleep(.01)\n"
            f"Path({str(tree_ready)!r}).write_text(json.dumps(dict(target=os.getpid(),child=child.pid)))\n"
            f"release=Path({str(observer_ready)!r})\n"
            "while not release.exists(): time.sleep(.01)\n"
            "os.write(1,b'target stdout\\n'); os.write(2,b'target stderr\\n')\n"
            f"sys.exit({exit_code})"
        )
        handles = _ProcessHandles()
        self.addCleanup(handles.close)
        stop = threading.Event()
        observer_errors = []

        def observe():
            try:
                while not stop.wait(.01):
                    try:
                        pids = json.loads(tree_ready.read_text())
                    except (OSError, ValueError):
                        continue
                    for label in ("target", "child"):
                        handles.open(label, pids[label])
                    # Retain handles before release so PID reuse cannot fake reaping.
                    observer_ready.write_text("handles retained")
                    return
            except Exception as exc:
                observer_errors.append(repr(exc))

        observer = threading.Thread(target=observe, daemon=True)
        observer.start()
        workers = []
        real_popen = subprocess.Popen

        def record_worker(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            workers.append(process)
            return process

        try:
            with patch.object(workspace_tools.subprocess, "Popen", record_worker):
                result = self.executor(target).execute("workspace_run", {
                    "command": "probe", "stdin": "x" * 65536,
                })
        finally:
            stop.set()
            observer.join(timeout=5)
        self.assertFalse(observer.is_alive(), "fixture observer must finish")
        self.assertEqual(observer_errors, [], "fixture observer failed")
        self.assertEqual(set(handles.handles), {"target", "child"}, "fixture must start and observe both processes")
        actual_codes = {label: handles.wait_and_code(label) for label in ("target", "child")}
        self.assertTrue(workers and all(process.poll() is not None for process in workers), "worker must be reaped")
        evidence = json.dumps({"receipt": result, "kernel_exit_codes": actual_codes,
                               "late_write": leaked.exists()}, ensure_ascii=True)
        self.assertEqual((result.get("outcome"), result.get("exit_code")),
                         ("passed" if exit_code == 0 else "failed", exit_code), evidence)
        self.assertEqual(actual_codes["target"], exit_code, evidence)
        self.assertFalse(leaked.exists(), evidence)
        self.assertEqual(result["status"], "ok" if exit_code == 0 else "error", evidence)
        self.assertFalse(result["timed_out"], evidence)
        self.assertTrue(result["complete"], evidence)
        self.assertEqual(result["process_scope"], "windows_job", evidence)
        for name, body in (("stdout", b"target stdout\n"), ("stderr", b"target stderr\n")):
            self.assertEqual(result[name]["bytes"], len(body), evidence)
            self.assertEqual(result[name]["sha256"], hashlib.sha256(body).hexdigest(), evidence)
            self.assertTrue(result[name]["complete"], evidence)
        self.assertEqual(result["stdin"]["bytes"], 65536, evidence)
        self.assertEqual(result["stdin"]["sha256"], hashlib.sha256(b"x" * 65536).hexdigest(), evidence)
        if exit_code:
            self.assertEqual(result["error"]["code"], "failed", evidence)

    def test_zero_exit_reaps_stdin_holding_descendant(self):
        self.assert_tree_exit(0)

    def test_nonzero_exit_reaps_stdin_holding_descendant(self):
        self.assert_tree_exit(7)

    def test_early_exit_without_consuming_input_preserves_output_and_code(self):
        for code in (0, 7):
            with self.subTest(code=code):
                script = f"import os,sys; os.write(1,b'early stdout'); os.write(2,b'early stderr'); sys.exit({code})"
                result = self.executor(script).execute("workspace_run", {
                    "command": "probe", "stdin": "x" * 65536,
                })
                self.assertEqual((result["outcome"], result["exit_code"]),
                                 ("passed" if code == 0 else "failed", code), result)
                self.assertEqual(result["stdout"]["excerpt"], "early stdout", result)
                self.assertEqual(result["stderr"]["excerpt"], "early stderr", result)
                self.assertTrue(result["complete"], result)
                self.assertFalse(result["timed_out"], result)


if __name__ == "__main__":
    unittest.main()
