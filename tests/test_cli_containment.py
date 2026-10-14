from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from cater.drivers import CliDriver, DriverProtocolError, DriverUnavailable
from cater.processes import ProcessScope


class CliContainmentTests(unittest.TestCase):
    def driver(self, script: str, *, timeout: float = 0.5) -> CliDriver:
        driver = CliDriver("scope-fixture", [sys.executable, "-c", script], timeout=timeout, restart_limit=0)
        self.addCleanup(driver.close)
        return driver

    def test_response_timeout_terminates_before_closing_pipe(self):
        driver = self.driver("import time; time.sleep(4)")
        started = time.monotonic()
        with self.assertRaisesRegex(DriverUnavailable, "did not respond"):
            driver.complete(role="tester", prompt="wait", context={})
        self.assertLess(time.monotonic() - started, 2)
        self.assertIsNone(driver._proc)
        self.assertIsNone(driver._scope)

    def test_request_write_is_also_bounded_by_timeout(self):
        driver = self.driver("import time; time.sleep(4)")
        started = time.monotonic()
        with self.assertRaisesRegex(DriverUnavailable, "did not respond"):
            driver.complete(role="tester", prompt="x" * (2 * 1024 * 1024), context={})
        self.assertLess(time.monotonic() - started, 2)
        self.assertIsNone(driver._proc)

    def test_invalid_utf8_reports_protocol_error_without_waiting_for_timeout(self):
        driver = self.driver("import sys; sys.stdin.readline(); sys.stdout.buffer.write(b'\\xff\\n'); sys.stdout.flush()", timeout=3)
        started = time.monotonic()
        with self.assertRaisesRegex(DriverProtocolError, "UTF-8"):
            driver.complete(role="tester", prompt="decode", context={})
        self.assertLess(time.monotonic() - started, 2)

    def test_scope_attachment_failure_never_launches_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "must-not-run"
            driver = self.driver(f"from pathlib import Path; Path({str(marker)!r}).touch()")
            with patch("cater.drivers.ProcessScope", side_effect=OSError("attachment rejected")):
                with self.assertRaisesRegex(DriverUnavailable, "start safely"):
                    driver.complete(role="tester", prompt="start", context={})
            self.assertFalse(marker.exists())
            self.assertIsNone(driver._proc)

    def test_startup_and_request_share_one_attempt_deadline(self):
        def slow_attach(proc):
            scope = ProcessScope(proc)
            time.sleep(0.3)
            return scope

        script = "import sys, json, time; r=json.loads(sys.stdin.readline()); time.sleep(.3); print(json.dumps({'id':r['id'],'ok':True,'text':'late'}),flush=True)"
        driver = self.driver(script, timeout=0.5)
        with patch("cater.drivers.ProcessScope", side_effect=slow_attach):
            with self.assertRaisesRegex(DriverUnavailable, "did not respond"):
                driver.complete(role="tester", prompt="deadline", context={})

    def test_close_cleans_up_pipe_holding_descendants(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            child = (
                "from pathlib import Path; import time; "
                f"Path({str(root / 'ready')!r}).touch(); "
                "time.sleep(1.5); "
                f"Path({str(root / 'leaked')!r}).touch()"
            )
            script = (
                "import json, subprocess, sys, time; from pathlib import Path\n"
                "request=json.loads(sys.stdin.readline())\n"
                f"subprocess.Popen([sys.executable, '-c', {child!r}])\n"
                f"while not Path({str(root / 'ready')!r}).exists(): time.sleep(.005)\n"
                "print(json.dumps({'id':request['id'],'ok':True,'text':'ready','tokens':0}), flush=True)\n"
                "time.sleep(4)\n"
            )
            driver = self.driver(script, timeout=3)
            response = driver.complete(role="tester", prompt="tree", context={})
            self.assertEqual(response.text, "ready")
            self.assertTrue((root / "ready").exists())
            started = time.monotonic()
            driver.close()
            self.assertLess(time.monotonic() - started, 2)
            time.sleep(1.6)
            self.assertFalse((root / "leaked").exists())

    def test_exited_worker_scope_is_cleaned_before_replacement(self):
        driver = self.driver("import json, sys; r=json.loads(sys.stdin.readline()); print(json.dumps({'id':r['id'],'ok':True,'text':'one'}),flush=True)", timeout=3)
        self.assertEqual(driver.complete(role="tester", prompt="one", context={}).text, "one")
        driver._proc.wait(timeout=2)
        old_scope = driver._scope
        with patch.object(old_scope, "close", wraps=old_scope.close) as close:
            self.assertEqual(driver.complete(role="tester", prompt="two", context={}).text, "one")
            close.assert_called_once()
