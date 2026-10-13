from __future__ import annotations

import json
import hashlib
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from cater.verification import run_check


class VerificationTests(unittest.TestCase):
    def tree_command(self, root: Path, *, keep_parent: bool) -> list[str]:
        child = (
            "from pathlib import Path; import time; "
            f"Path({str(root / 'ready')!r}).write_text('ready'); "
            "time.sleep(1.5); "
            f"Path({str(root / 'leaked')!r}).write_text('still running')"
        )
        parent = (
            "import subprocess, sys, time; from pathlib import Path; "
            f"subprocess.Popen([sys.executable, '-c', {child!r}]); "
            f"ready=Path({str(root / 'ready')!r})\n"
            "while not ready.exists(): time.sleep(0.005)\n"
            + ("time.sleep(10)" if keep_parent else "")
        )
        return [sys.executable, "-c", parent]

    def test_finished_command_does_not_leave_pipe_holding_descendants(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = run_check(self.tree_command(root, keep_parent=False), cwd=root, timeout=3)
            self.assertEqual(result.payload["outcome"], "passed")
            self.assertTrue((root / "ready").exists())
            self.assertTrue(result.payload["stdout"]["complete"])
            self.assertTrue(result.payload["stderr"]["complete"])
            self.assertEqual(result.payload["process_scope"], "windows_job" if os.name == "nt" else "posix_process_group")
            time.sleep(1.6)
            self.assertFalse((root / "leaked").exists())

    def test_timeout_cleans_up_descendants_and_finishes_capture(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = run_check(self.tree_command(root, keep_parent=True), cwd=root, timeout=0.8)
            self.assertTrue((root / "ready").exists(), "fixture must have started its child")
            self.assertEqual(result.payload["outcome"], "timeout")
            self.assertIsNone(result.payload["exit_code"])
            self.assertTrue(result.payload["stdout"]["complete"])
            time.sleep(1.6)
            self.assertFalse((root / "leaked").exists())

    def test_scope_attachment_failure_never_runs_requested_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = root / "must-not-run"
            command = [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"]
            with patch("cater.verification._ProcessScope", side_effect=OSError("attachment rejected")):
                result = run_check(command, cwd=root)
            self.assertEqual(result.payload["outcome"], "launch_error")
            self.assertIn("attachment rejected", result.payload["launch_error"])
            self.assertFalse(marker.exists())

    def test_missing_executable_reports_launch_error_from_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = run_check([str(Path(tmp) / "missing-executable")], cwd=Path(tmp))
        self.assertEqual(result.payload["outcome"], "launch_error")
        self.assertIsNone(result.payload["exit_code"])
        self.assertTrue(result.payload["stdout"]["complete"])
        self.assertIn("launch_error", result.payload)
        json.dumps(result.payload)

    def test_invalid_utf8_cannot_expand_excerpt_past_byte_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = run_check(
                [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'\\xff' * 6000)"],
                cwd=Path(tmp), record_output=True,
            )
        output = result.payload["stdout"]
        self.assertEqual(result.payload["outcome"], "passed")
        self.assertEqual(output["bytes"], 6000)
        self.assertEqual(output["sha256"], hashlib.sha256(b"\xff" * 6000).hexdigest())
        self.assertLessEqual(len(output["excerpt"].encode("utf-8")), 8192)
        self.assertTrue(output["complete"])
        self.assertTrue(output["truncated"])
