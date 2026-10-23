"""Offline stall diagnostics: real blocking children and bounded metadata only."""

from __future__ import annotations

from contextlib import ExitStack, redirect_stderr, redirect_stdout
import gc
import io
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import weakref

from scripts import run_tests as runner
from scripts import test_progress as progress

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "::notice title=Test progress::"

# Request real samples only while the child is held at a known blocking point.
# The watchdog still classifies the real main-thread stack and uses its own clock.
STALL_HANDSHAKE = textwrap.dedent('''\
    import json
    requested, observed = threading.Event(), threading.Event()
    expected_phase = None
    original_stdout = sys.stdout

    def stall(phase):
        global expected_phase
        expected_phase = phase
        observed.clear()
        requested.set()
        assert observed.wait(5), "No stalled notice for " + phase

    class Watcher(progress.ProgressWatchdog):
        def __init__(self, *args):
            super().__init__(*args)
            wait = self._stop.wait
            def wait_for_sample(interval):
                while not requested.is_set():
                    if wait(.01):
                        return True
                return wait(interval)
            self._stop.wait = wait_for_sample

    class Sink:
        def write(self, text):
            size = original_stdout.write(text)
            prefix = "::notice title=Test progress::"
            for line in text.splitlines():
                if line.startswith(prefix):
                    item = json.loads(line[len(prefix):].replace("%0D", "\\r").replace("%0A", "\\n").replace("%25", "%"))
                    if item["phase"] == expected_phase and item["stalled"]:
                        original_stdout.flush()
                        requested.clear()
                        observed.set()
            return size

        def flush(self):
            original_stdout.flush()

    runner.ProgressWatchdog = Watcher
    sys.stdout = Sink()
''')


def notices(output):
    return [json.loads(line[len(PREFIX):].replace("%0D", "\r").replace("%0A", "\n").replace("%25", "%"))
            for line in output.splitlines() if line.startswith(PREFIX)]


class ProgressWatchdogTests(unittest.TestCase):
    def test_defaults_and_total_notice_budget(self):
        output = io.StringIO()
        watcher = progress.ProgressWatchdog(output, runner.escape_annotation)
        self.assertEqual(watcher.interval, 120)
        self.assertEqual(progress.CLOSE_BUDGET_SECONDS, 1)
        self.assertEqual(progress.NOTICE_LIMIT, 8)
        watcher._origin = 0
        watcher._state = ("discovery", None, 0)
        with patch.object(watcher._stop, "wait", return_value=False) as wait, \
                patch.object(progress.time, "monotonic", return_value=1000), \
                patch.object(progress, "sample_main_stack", return_value=(None, [])):
            watcher._watch()
        self.assertEqual(len(notices(output.getvalue())), 8)
        self.assertEqual(wait.call_count, 8)
        self.assertTrue(all(item["elapsed_seconds"] == 1000 for item in notices(output.getvalue())))
        watcher.close()

    def test_progress_resets_stall_timer_but_keeps_periodic_checkpoint(self):
        output = io.StringIO()
        watcher = progress.ProgressWatchdog(output, runner.escape_annotation, interval=10)
        with patch.object(progress.time, "monotonic", return_value=100):
            watcher.boundary("case", "current")
            with patch.object(watcher._stop, "wait", side_effect=[False, True]):
                watcher._watch()
        checkpoint, = notices(output.getvalue())
        self.assertFalse(checkpoint["stalled"])
        self.assertEqual(checkpoint["since_progress_seconds"], 0)
        self.assertEqual(checkpoint["completed_cases"], 0)
        self.assertIsNone(checkpoint["selected_total"])
        self.assertEqual(watcher._state, ("case", "current", 100))
        watcher.close()

    def test_json_protocol_escaping_bounds_and_no_extra_commands(self):
        output = io.StringIO()
        watcher = progress.ProgressWatchdog(output, runner.escape_annotation, interval=.01)
        case_id = 'test.%25\r\n::error::"\\\u4e2d,' + 'x' * 400
        watcher.start_case(SimpleNamespace(id=lambda: case_id))
        phase, case, _ = watcher._state
        watcher._state = (phase, case, 0)
        with patch.object(watcher._stop, "wait", side_effect=[False, True]), \
                patch.object(progress, "sample_main_stack", return_value=(None, [])):
            watcher._watch()
        payload, = notices(output.getvalue())
        self.assertEqual(payload["case"], case_id[:250])
        self.assertEqual(payload["phase"], "case")
        self.assertNotIn("\n::error::", output.getvalue())
        self.assertIn("%2525", output.getvalue())
        self.assertTrue(output.getvalue().isascii())
        self.assertLess(len(output.getvalue()), 2000)
        watcher.close()

    def test_stack_is_shallow_basenames_only_and_releases_frames(self):
        class Marker:
            pass

        refs = []

        def nested(depth):
            private_local = Marker()
            refs.append(weakref.ref(private_local))
            if depth:
                return nested(depth - 1)
            return progress.sample_main_stack(threading.main_thread().ident)

        fixture, stack = nested(12)
        gc.collect()
        self.assertIsNone(fixture)
        self.assertEqual(len(stack), 8)
        self.assertTrue(all(ref() is None for ref in refs))
        for entry in stack:
            self.assertEqual(set(entry), {"file", "function", "line"})
            self.assertNotIn("/", entry["file"])
            self.assertNotIn("\\", entry["file"])
            self.assertIsInstance(entry["line"], int)

    def test_sampling_error_releases_frames_and_does_not_leak_locals(self):
        class Marker:
            pass

        def sample():
            private_local = Marker()
            ref = weakref.ref(private_local)
            broken = SimpleNamespace(f_code=SimpleNamespace(co_filename="base.py", co_name="function"))
            frames = {123: broken}
            with patch.object(progress.sys, "_current_frames", return_value=frames):
                with self.assertRaises(AttributeError):
                    progress.sample_main_stack(123)
            self.assertEqual(frames, {})
            return ref

        ref = sample()
        gc.collect()
        self.assertIsNone(ref())

    def test_hostile_stack_metadata_is_bounded_without_reading_source_or_locals(self):
        hostile = '\U0001f600%\r\n::error::' * 300

        class Frame:
            f_code = SimpleNamespace(co_filename="C:\\PRIVATE_PATH\\" + hostile, co_name=hostile)
            f_lineno = 42

            @property
            def f_locals(self):
                raise AssertionError("must not inspect locals")

            @property
            def f_back(self):
                reads.append(1)
                return self

        reads = []
        frames = {123: Frame()}
        with patch.object(progress.sys, "_current_frames", return_value=frames):
            _, stack = progress.sample_main_stack(123)
        self.assertEqual(frames, {})
        self.assertEqual(len(stack), 8)
        self.assertEqual(len(reads), 64)
        self.assertTrue(all(len(item["file"]) == len(item["function"]) == 250 for item in stack))
        output = io.StringIO()
        watcher = progress.ProgressWatchdog(output, runner.escape_annotation, interval=.01)
        watcher._state = ("case", hostile[:250], 0)
        with patch.object(watcher._stop, "wait", side_effect=[False, True]), \
                patch.object(progress, "sample_main_stack", return_value=(None, stack)):
            watcher._watch()
        self.assertEqual(len(notices(output.getvalue())), 1)
        self.assertLess(len(output.getvalue().encode("ascii")), 64000)
        self.assertNotIn("PRIVATE_PATH", output.getvalue())
        watcher.close()

    def test_progress_during_sampling_discards_stale_case_notice(self):
        output = io.StringIO()
        watcher = progress.ProgressWatchdog(output, runner.escape_annotation, interval=.01)
        watcher._state = ("case", "old_case", 0)

        def sample(ident):
            watcher.boundary("case", "new_case")
            return None, []

        with patch.object(watcher._stop, "wait", side_effect=[False, True]), \
                patch.object(progress, "sample_main_stack", side_effect=sample):
            watcher._watch()
        self.assertEqual(output.getvalue(), "")
        watcher.close()

    def test_worker_faults_are_quiet_and_close_joins_and_releases_stream(self):
        for fault in ("sample", "write", "flush"):
            with self.subTest(fault=fault):
                output = io.StringIO()
                watcher = progress.ProgressWatchdog(output, runner.escape_annotation, interval=.001)
                with patch.object(progress, "sample_main_stack", side_effect=(
                        OSError("PRIVATE_SAMPLE") if fault == "sample" else lambda ident: (None, []))):
                    if fault in ("write", "flush"):
                        setattr(output, fault, lambda *args: (_ for _ in ()).throw(OSError("PRIVATE_SINK")))
                    watcher.start()
                    watcher._thread.join(2)
                    self.assertFalse(watcher._thread.is_alive())
                watcher.close()
                self.assertIsNone(watcher._stream)
                self.assertNotIn("PRIVATE", output.getvalue())

    def test_start_failure_and_repeated_close_are_safe(self):
        watcher = progress.ProgressWatchdog(io.StringIO(), runner.escape_annotation)
        with patch.object(progress.threading.Thread, "start", side_effect=RuntimeError("PRIVATE_START")):
            watcher.start()
        watcher.close()
        watcher.close()
        self.assertIsNone(watcher._thread)
        self.assertIsNone(watcher._stream)

    def test_close_interrupts_long_wait_with_no_late_output(self):
        output = io.StringIO()
        watcher = progress.ProgressWatchdog(output, runner.escape_annotation)
        watcher.start()
        watcher.close()
        self.assertFalse(watcher._thread.is_alive())
        self.assertEqual(output.getvalue(), "")

    def test_main_lifecycle_on_returns_exceptions_and_repeated_unit_calls(self):
        for outcome in (0, 1, KeyboardInterrupt(), SystemExit(3), RuntimeError("PRIVATE")):
            with self.subTest(outcome=type(outcome).__name__):
                watchers = []

                def create(*args):
                    watcher = progress.ProgressWatchdog(*args, interval=.01)
                    watchers.append(watcher)
                    return watcher

                def execute(*args):
                    if isinstance(outcome, BaseException):
                        raise outcome
                    return outcome

                with patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}), \
                        patch.object(runner, "ProgressWatchdog", side_effect=create), \
                        patch.object(runner, "run_selected", side_effect=execute), \
                        redirect_stdout(io.StringIO()):
                    for _ in range(2):
                        if isinstance(outcome, BaseException):
                            with self.assertRaises(type(outcome)) as caught:
                                runner.main([])
                            self.assertIs(caught.exception, outcome)
                        else:
                            self.assertEqual(runner.main([]), outcome)
                self.assertEqual(len(watchers), 2)
                self.assertIsNot(watchers[0], watchers[1])
                self.assertTrue(all(not item._thread.is_alive() and item._stream is None for item in watchers))

    def test_local_and_list_modes_never_start_watchdog(self):
        for github, args in (("false", []), ("true", ["--list-tests"])):
            with patch.dict(os.environ, {"GITHUB_ACTIONS": github}), \
                    patch.object(runner, "ProgressWatchdog") as create, \
                    patch.object(runner, "run_selected", return_value=0), redirect_stdout(io.StringIO()):
                self.assertEqual(runner.main(args), 0)
            create.assert_not_called()

    def test_diagnostic_faults_preserve_real_suite_returns_and_exception_identity(self):
        for fault in ("constructor", "event", "start", "select", "boundary", "start_case", "stop_case", "close", "reconfigure"):
            for outcome in (0, 1, RuntimeError("SUITE_ERROR"), KeyboardInterrupt(), SystemExit(7)):
                with self.subTest(fault=fault, outcome=type(outcome).__name__):
                    executed = []

                    class Case(unittest.TestCase):
                        def runTest(self):
                            executed.append("case")
                            if outcome == 1:
                                self.fail("offline failure")

                    class Suite(unittest.TestSuite):
                        def run(self, result, debug=False):
                            super().run(result, debug)
                            if isinstance(outcome, BaseException):
                                raise outcome
                            return result

                    diagnostic_error = OSError("PRIVATE_DIAGNOSTIC_BODY")
                    watcher = SimpleNamespace(**{method: Mock() for method in
                                              ("start", "select", "start_case", "stop_case", "close")})
                    output, diagnostic = io.StringIO(), io.StringIO()
                    if fault == "boundary":
                        watcher = progress.ProgressWatchdog(output, runner.escape_annotation)
                        watcher.start = Mock()
                        watcher.boundary = Mock(side_effect=diagnostic_error)
                    with ExitStack() as contexts:
                        contexts.enter_context(patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}))
                        contexts.enter_context(redirect_stdout(output))
                        contexts.enter_context(redirect_stderr(diagnostic))
                        contexts.enter_context(patch.object(unittest.defaultTestLoader, "discover",
                                                            return_value=Suite([Case()])))
                        if fault == "event":
                            contexts.enter_context(patch.object(progress.threading, "Event", side_effect=diagnostic_error))
                        else:
                            contexts.enter_context(patch.object(runner, "ProgressWatchdog",
                                **({"side_effect": diagnostic_error} if fault == "constructor" else {"return_value": watcher})))
                            if fault == "reconfigure":
                                output.reconfigure = Mock(side_effect=diagnostic_error)
                            elif fault not in ("constructor", "boundary"):
                                getattr(watcher, fault).side_effect = diagnostic_error
                        if isinstance(outcome, BaseException):
                            with self.assertRaises(type(outcome)) as caught:
                                runner.main([])
                            self.assertIs(caught.exception, outcome)
                        else:
                            self.assertEqual(runner.main([]), outcome)
                    self.assertEqual(executed, ["case"])
                    self.assertNotIn("PRIVATE_DIAGNOSTIC_BODY", output.getvalue() + diagnostic.getvalue())

    def test_close_join_error_still_drops_sink_and_sets_stop(self):
        watcher = progress.ProgressWatchdog(io.StringIO(), runner.escape_annotation)
        watcher._thread = SimpleNamespace(join=Mock(side_effect=RuntimeError("PRIVATE_JOIN")))
        runner.progress_call(watcher, "close")
        self.assertTrue(watcher._stop.is_set())
        self.assertIsNone(watcher._stream)
        watcher._thread.join.assert_called_once_with(timeout=1)

    def test_stop_during_serialization_prevents_new_write_or_flush(self):
        output = Mock()
        watcher = progress.ProgressWatchdog(output, lambda value: watcher._stop.set() or value, interval=.01)
        with patch.object(watcher._stop, "wait", return_value=False), \
                patch.object(progress, "sample_main_stack", return_value=(None, [])):
            watcher._watch()
        output.write.assert_not_called()
        output.flush.assert_not_called()
        watcher.close()


class ProgressChildTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "scripts").mkdir()
        (self.root / "tests").mkdir()
        for name in ("run_tests.py", "test_progress.py"):
            shutil.copyfile(ROOT / "scripts" / name, self.root / "scripts" / name)

    def child(self, source, *, args=(), github=True, fault="", after="", synchronize=False):
        (self.root / "tests/test_block.py").write_text(source, encoding="utf-8")
        script = (
            "import sys, time, threading\n"
            "from scripts import run_tests as runner, test_progress as progress\n"
            "progress.STALL_INTERVAL_SECONDS = .03\n"
            + (STALL_HANDSHAKE if synchronize else "")
            + fault + "\n"
            "try:\n"
            "    status = runner.main(sys.argv[1:])\n"
            "except KeyboardInterrupt:\n"
            "    status = 130\n"
            + after + "\n"
            "assert not any(t.name == 'test-progress-watchdog' for t in threading.enumerate())\n"
            "print('COMPLETE', flush=True)\n"
            "time.sleep(.10)\n"
            "raise SystemExit(status)\n"
        )
        allowed = {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "TMPDIR"}
        environment = {key: value for key, value in os.environ.items() if key.upper() in allowed}
        environment.update(PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1",
                           GITHUB_ACTIONS="true" if github else "false", PRIVATE_ENV="ENV_MUST_NOT_APPEAR")
        return subprocess.run([sys.executable, "-X", "utf8", "-B", "-c", script, *args], cwd=self.root,
                              env=environment, capture_output=True, text=True, encoding="utf-8", timeout=15)

    def assert_evidence(self, result, phase, *, status=0, case=None):
        self.assertEqual(result.returncode, status, result.stderr)
        notice_lines = [line for line in result.stdout.splitlines() if line.startswith(PREFIX)]
        evidence = notices("\n".join(notice_lines))
        self.assertGreaterEqual(len(evidence), 1, result.stdout)
        self.assertLessEqual(len(evidence), 8)
        target = [item for item in evidence if item["phase"] == phase]
        self.assertTrue(target, evidence)
        self.assertTrue(all(item["case"] == case for item in target), target)
        self.assertEqual(result.stdout.splitlines()[-1], "COMPLETE")
        ticks = [item["elapsed_seconds"] for item in evidence]
        self.assertEqual(ticks, sorted(ticks))
        for item in evidence:
            self.assertEqual(set(item), {"phase", "case", "elapsed_seconds", "since_progress_seconds", "stack",
                                         "stalled", "completed_cases", "selected_total"})
            self.assertGreaterEqual(item["since_progress_seconds"], 0)
            self.assertIn(item["phase"], {"discovery", "fixture_or_suite", phase})
            if item["phase"] != "case":
                self.assertIsNone(item["case"])
            if item["phase"] == "discovery":
                self.assertIsNone(item["selected_total"])
                self.assertEqual(item["completed_cases"], 0)
            else:
                self.assertEqual(item["selected_total"], 1)
                self.assertLessEqual(item["completed_cases"], item["selected_total"])
            self.assertLessEqual(len(item["stack"]), 8)
            for frame in item["stack"]:
                self.assertEqual(set(frame), {"file", "function", "line"})
            metadata = [item["phase"], item["case"]]
            metadata.extend(value for frame in item["stack"] for value in (frame["file"], frame["function"]))
            for value in metadata:
                if value is not None:
                    self.assertNotIn("/", value)
                    self.assertNotIn("\\", value)
        self.assertTrue(any(item["stalled"] for item in target), target)
        # Ordinary failure annotations retain tracebacks; watchdog notices expose metadata only.
        for text in ("\n".join(notice_lines), json.dumps(evidence, ensure_ascii=False)):
            for private in (str(self.root), "PRIVATE", "ENV_MUST_NOT_APPEAR",
                            "LOCAL_MUST_NOT_APPEAR", "SOURCE_MUST_NOT_APPEAR"):
                self.assertNotIn(private, text)

    def failure_annotation_evidence(self, *, file="test_block.py", function="test_fail"):
        payload = {"phase": "case", "case": "test_block.Case.test_fail", "elapsed_seconds": .04,
                   "since_progress_seconds": .04, "stack": [{"file": file, "function": function, "line": 8}],
                   "stalled": True, "completed_cases": 0, "selected_total": 1}
        detail = ('Traceback (most recent call last):\n'
                  f'  File "{self.root / "tests" / "test_block.py"}", line 8, in test_fail\n'
                  'AssertionError: PRIVATE_FAILURE\n')
        output = io.StringIO()
        result = runner.AnnotatedResult(io.StringIO(), True, 2)
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}), redirect_stdout(output):
            result.annotate(SimpleNamespace(id=lambda: payload["case"]), detail)
        stdout = PREFIX + runner.escape_annotation(json.dumps(payload)) + "\n" + output.getvalue() + "COMPLETE\n"
        return SimpleNamespace(returncode=1, stdout=stdout, stderr="FAILED (failures=1)")

    def test_evidence_distinguishes_failure_tracebacks_from_clean_notices(self):
        for root in (PurePosixPath("/tmp/ci-stall-posix-private"), PureWindowsPath("C:/ci-stall-private")):
            with self.subTest(root=str(root)), patch.object(self, "root", root):
                result = self.failure_annotation_evidence()
                self.assertIn(str(root / "tests" / "test_block.py"), result.stdout)
                self.assertIn("AssertionError: PRIVATE_FAILURE", result.stdout)
                self.assert_evidence(result, "case", status=1, case="test_block.Case.test_fail")

    def test_evidence_checks_discovery_and_requires_the_target_stall(self):
        result = self.failure_annotation_evidence()
        discovery = dict(notices(result.stdout)[0], phase="discovery", case=None,
                         selected_total=None, elapsed_seconds=.03, since_progress_seconds=.03)
        prefix = PREFIX + runner.escape_annotation(json.dumps(discovery)) + "\n"
        result.stdout = prefix + result.stdout
        self.assert_evidence(result, "case", status=1, case="test_block.Case.test_fail")
        for mutation in ("missing_target", "target_not_stalled", "wrong_phase", "discovery_count"):
            with self.subTest(mutation=mutation):
                items = notices(result.stdout)
                if mutation == "missing_target":
                    items = items[:1]
                elif mutation == "target_not_stalled":
                    items[1]["stalled"] = False
                elif mutation == "wrong_phase":
                    items[1]["phase"] = "module_setup"
                else:
                    items[0]["selected_total"] = 1
                invalid = SimpleNamespace(returncode=1, stderr=result.stderr, stdout="".join(
                    PREFIX + runner.escape_annotation(json.dumps(item)) + "\n" for item in items) + "COMPLETE\n")
                with self.assertRaises(AssertionError):
                    self.assert_evidence(invalid, "case", status=1, case="test_block.Case.test_fail")

    def test_evidence_rejects_notice_paths_including_json_escaped_paths(self):
        for path in ("/opt/unrelated/project/test_block.py", "D:\\unrelated\\project\\test_block.py", str(self.root)):
            for field in ("file", "function"):
                for escaped in (False, True):
                    with self.subTest(path=path, field=field, escaped=escaped):
                        result = self.failure_annotation_evidence(**{field: path})
                        if escaped:
                            result.stdout = result.stdout.replace("/", r"\u002f").replace(r"\\", r"\u005c")
                        with self.assertRaises(AssertionError):
                            self.assert_evidence(result, "case", status=1, case="test_block.Case.test_fail")

    def test_evidence_rejects_notice_secrets_including_json_escaped_secrets(self):
        for secret in ("PRIVATE_FAILURE", "ENV_MUST_NOT_APPEAR", "LOCAL_MUST_NOT_APPEAR", "SOURCE_MUST_NOT_APPEAR"):
            for escaped in (False, True):
                with self.subTest(secret=secret, escaped=escaped):
                    result = self.failure_annotation_evidence(function=secret)
                    if escaped:
                        result.stdout = result.stdout.replace(secret, "\\u%04x" % ord(secret[0]) + secret[1:])
                    with self.assertRaises(AssertionError):
                        self.assert_evidence(result, "case", status=1, case="test_block.Case.test_fail")

    def test_real_discovery_import_and_load_tests_stalls(self):
        case = "class Case(unittest.TestCase):\n    def test_ok(self): pass\n"
        for source in (
                "import unittest\nfrom __main__ import stall\nstall('discovery') # SOURCE_MUST_NOT_APPEAR\n" + case,
                "import unittest\nfrom __main__ import stall\n" + case +
                "def load_tests(loader, tests, pattern):\n    stall('discovery')\n    return tests\n"):
            with self.subTest(source=source[:45]):
                result = self.child(source, synchronize=True)
                self.assert_evidence(result, "discovery")

    def test_real_module_class_fixtures_and_cleanups_outside_test_boundaries(self):
        for phase, body in (
            ("module_setup", "def setUpModule(): stall()\n"),
            ("module_teardown", "def tearDownModule(): stall()\n"),
            ("module_cleanup", "def setUpModule(): unittest.addModuleCleanup(stall)\n"),
            ("class_setup", "    @classmethod\n    def setUpClass(cls): stall()\n"),
            ("class_teardown", "    @classmethod\n    def tearDownClass(cls): stall()\n"),
            ("class_cleanup", "    @classmethod\n    def setUpClass(cls): cls.addClassCleanup(stall)\n"),
        ):
            source = ("import unittest\nfrom __main__ import stall as wait_for_notice\n"
                      "wait_for_notice('discovery')\ndef stall():\n"
                      "    private_local = 'LOCAL_MUST_NOT_APPEAR'\n"
                      f"    wait_for_notice({phase!r})\n")
            source += (body if phase.startswith("module") else "")
            source += "class Case(unittest.TestCase):\n    def test_ok(self): pass\n"
            source += (body if phase.startswith("class") else "")
            with self.subTest(phase=phase):
                result = self.child(source, synchronize=True)
                self.assert_evidence(result, phase)
                self.assertEqual([item["phase"] for item in notices(result.stdout) if item["stalled"]],
                                 ["discovery", phase])

    def test_real_case_stall_and_failure_exit_without_replay(self):
        source = ("import unittest\nfrom __main__ import stall\nclass Case(unittest.TestCase):\n"
                  "    def test_fail(self):\n        print('EXECUTED', flush=True)\n"
                  "        private_local = 'LOCAL_MUST_NOT_APPEAR'\n"
                  "        stall('case') # SOURCE_MUST_NOT_APPEAR\n        self.fail('offline failure PRIVATE_FAILURE')\n")
        result = self.child(source, synchronize=True)
        self.assert_evidence(result, "case", status=1, case="test_block.Case.test_fail")
        self.assertEqual(result.stdout.count("EXECUTED"), 1)
        errors = [line for line in result.stdout.splitlines() if line.startswith("::error title=")]
        self.assertEqual(len(errors), 1)
        self.assertTrue(errors[0].startswith("::error title=test_block.Case.test_fail::"))
        self.assertIn("Traceback (most recent call last):", errors[0])
        self.assertIn("test_block.py", errors[0])
        self.assertIn("AssertionError: offline failure PRIVATE_FAILURE", errors[0])
        self.assertIn("FAILED (failures=1)", result.stderr)

    def test_real_keyboard_interrupt_in_discovery_and_case_joins_worker(self):
        for source, phase, case in (
            ("from __main__ import stall\ndef load_tests(loader, tests, pattern):\n"
             "    stall('discovery')\n    raise KeyboardInterrupt()\n", "discovery", None),
            ("import unittest\nfrom __main__ import stall\nclass Case(unittest.TestCase):\n"
             "    def test_interrupt(self):\n        stall('case')\n        raise KeyboardInterrupt()\n",
             "case", "test_block.Case.test_interrupt"),
        ):
            with self.subTest(phase=phase):
                self.assert_evidence(self.child(source, synchronize=True), phase, status=130, case=case)

    def test_real_import_interrupt_keeps_unittest_failed_discovery_semantics(self):
        result = self.child("from __main__ import stall\nstall('discovery')\nraise KeyboardInterrupt()\n",
                            synchronize=True)
        self.assert_evidence(result, "discovery", status=1)
        self.assertIn("no tests executed", result.stderr)

    def test_real_successful_case_and_custom_suite_keep_their_outcomes(self):
        source = ("import unittest\nfrom __main__ import stall\nclass Case(unittest.TestCase):\n"
                  "    def test_ok(self): stall('case')\n")
        self.assert_evidence(self.child(source, synchronize=True), "case", case="test_block.Case.test_ok")
        source = ("import unittest\nfrom __main__ import stall\nclass Case(unittest.TestCase):\n"
                  "    def test_ok(self): pass\n"
                  "class Suite(unittest.TestSuite):\n"
                  "    def run(self, result, debug=False):\n"
                  "        stall('fixture_or_suite')\n        return super().run(result, debug)\n"
                  "def load_tests(loader, tests, pattern):\n"
                  "    return Suite([Case('test_ok')])\n")
        self.assert_evidence(self.child(source, synchronize=True), "fixture_or_suite")

    def test_real_sampling_or_notice_write_fault_cannot_hide_failed_test(self):
        source = ("import time, unittest\nclass Case(unittest.TestCase):\n"
                  "    def test_fail(self):\n        time.sleep(.15)\n        self.fail('offline failure')\n")
        for fault in (
            "def broken(ident): raise OSError('PRIVATE_SAMPLE')\nprogress.sample_main_stack = broken\n",
            "original = sys.stdout\nclass Sink:\n"
            "    def write(self, text):\n"
            "        if '::notice' in text: raise OSError('PRIVATE_WRITE')\n"
            "        return original.write(text)\n"
            "    def flush(self): original.flush()\nsys.stdout = Sink()\n",
        ):
            with self.subTest(fault=fault[:30]):
                result = self.child(source, fault=fault)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("FAILED (failures=1)", result.stderr)
                self.assertNotIn("PRIVATE_", result.stdout + result.stderr)
                self.assertEqual(notices(result.stdout), [])
                self.assertEqual(result.stdout.splitlines()[-1], "COMPLETE")

    def test_real_ci_list_with_slow_discovery_keeps_id_only_stdout(self):
        source = ("import time, unittest\ntime.sleep(.15)\n"
                  "class Case(unittest.TestCase):\n    def test_ok(self): pass\n")
        result = self.child(source, args=["--list-tests"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["test_block.Case.test_ok", "COMPLETE"])
        result = self.child(source, github=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["COMPLETE"])

    def test_real_main_returns_with_blocked_write_then_worker_exits_without_flush(self):
        source = ("import unittest\nfrom __main__ import entered\n"
                  "class Case(unittest.TestCase):\n"
                  "    def test_ok(self): self.assertTrue(entered.wait(3))\n")
        fault = (
            "original = sys.stdout\nentered, release = threading.Event(), threading.Event()\n"
            "class Sink:\n"
            "    writes = 0\n    flushes = 0\n"
            "    def write(self, text):\n"
            "        if threading.current_thread().name == 'test-progress-watchdog':\n"
            "            self.writes += 1\n            entered.set()\n            release.wait()\n"
            "            return len(text)\n"
            "        return original.write(text)\n"
            "    def flush(self):\n"
            "        if threading.current_thread().name == 'test-progress-watchdog': self.flushes += 1\n"
            "        else: original.flush()\n"
            "sink = Sink()\nsys.stdout = sink\nwatchers = []\n"
            "class Watcher(progress.ProgressWatchdog):\n"
            "    def __init__(self, *args):\n        super().__init__(*args)\n        watchers.append(self)\n"
            "runner.ProgressWatchdog = Watcher\nstarted = time.monotonic()\n"
        )
        after = (
            "elapsed = time.monotonic() - started\n"
            "assert status == 0, status\nassert elapsed < 3, elapsed\n"
            "watcher, = watchers\nassert watcher._stream is None\n"
            "assert watcher._thread.daemon and watcher._thread.is_alive()\n"
            "assert sink.writes == 1 and sink.flushes == 0\n"
            "release.set()\nwatcher._thread.join(2)\nassert not watcher._thread.is_alive()\n"
            "time.sleep(.10)\nassert sink.writes == 1 and sink.flushes == 0\n"
            "sys.stdout = original\nprint('BOUNDED_CLOSE', round(elapsed, 3), 'writes=1 flushes=0', flush=True)\n"
        )
        result = self.child(source, fault=fault, after=after)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("BOUNDED_CLOSE", result.stdout)
        self.assertIn("writes=1 flushes=0", result.stdout)
        self.assertIn("Ran 1 test", result.stderr)
        self.assertEqual(result.stdout.splitlines()[-1], "COMPLETE")

    def test_real_continuing_cases_emit_checkpoints_with_completed_counts(self):
        source = ("import time, unittest\nclass Case(unittest.TestCase):\n"
                  "    def runTest(self): time.sleep(.015)\n"
                  "def load_tests(loader, tests, pattern):\n"
                  "    return unittest.TestSuite(Case() for _ in range(40))\n")
        result = self.child(source)
        self.assertEqual(result.returncode, 0, result.stderr)
        evidence = notices(result.stdout)
        self.assertGreaterEqual(len(evidence), 1)
        self.assertLessEqual(len(evidence), 8)
        checkpoints = [item for item in evidence if not item["stalled"] and item["phase"] == "case"]
        self.assertTrue(checkpoints, evidence)
        counts = [item["completed_cases"] for item in checkpoints]
        self.assertEqual(counts, sorted(counts))
        self.assertGreater(counts[-1], counts[0])
        self.assertTrue(all(0 <= item["completed_cases"] <= item["selected_total"] == 40 for item in checkpoints))
        self.assertEqual(result.stdout.splitlines()[-1], "COMPLETE")

    def test_real_short_ci_job_emits_no_checkpoint(self):
        result = self.child("import unittest\nclass Case(unittest.TestCase):\n    def test_ok(self): pass\n",
                            fault="progress.STALL_INTERVAL_SECONDS = 120.0\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["COMPLETE"])

    def test_real_skip_counts_but_fixture_error_is_not_a_completed_case(self):
        source = ("import time, unittest\nclass A(unittest.TestCase):\n"
                  "    @unittest.skip('offline skip')\n    def test_skip(self): pass\n"
                  "class B(unittest.TestCase):\n"
                  "    @classmethod\n    def setUpClass(cls):\n"
                  "        time.sleep(.08)\n        raise RuntimeError('offline fixture')\n"
                  "    def test_not_run(self): pass\n"
                  "class C(unittest.TestCase):\n    def test_after(self): time.sleep(.4)\n")
        result = self.child(source)
        self.assertEqual(result.returncode, 1, result.stderr)
        evidence = [item for item in notices(result.stdout) if item["case"] == "test_block.C.test_after"]
        self.assertTrue(evidence)
        self.assertTrue(all(item["completed_cases"] == 1 and item["selected_total"] == 3 for item in evidence))
        self.assertIn("Ran 2 tests", result.stderr)
        self.assertIn("errors=1, skipped=1", result.stderr)


if __name__ == "__main__":
    unittest.main()
