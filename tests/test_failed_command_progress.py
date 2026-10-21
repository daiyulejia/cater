"""Completed command failures are debugging progress, never test acceptance."""

from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from cater.external_agent import ExternalRunLimit, QoderAcpDriver, _ProgressBudget
from cater.processes import ProcessScope
from cater.tool_protocol import ToolCall
from cater.workspace_tools import WorkspaceExecutor, WorkspacePolicy


class FailedCommandProgressTests(unittest.TestCase):
    def failed(self, **changes):
        return {"status": "error", "outcome": "failed", "complete": True,
                "timed_out": False, "exit_code": 1,
                "error": {"code": "failed", "message": "A real check failed."}, **changes}

    def run_for(self, execute, *, idle=2, total=10):
        driver = QoderAcpDriver("offline", "never-launch", "ExactModel",
                                timeout=idle, max_run_seconds=total)
        run = driver.agent_run(cwd=".", tools=[], execute=execute,
                               check_current=lambda: None, on_update=None)
        run._budget = _ProgressBudget(idle, total)
        run._started_at = 0
        return run

    def executor(self, script):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        return WorkspaceExecutor(WorkspacePolicy.from_dict({
            "root": str(root), "mode": "execute", "timeout": 15,
            "commands": {"check": [sys.executable, "-I", "-B", "-c", script]},
        }))

    def test_known_nonzero_integer_failures_renew_idle(self):
        for code in (1, 2, -9):
            with self.subTest(exit_code=code), patch(
                    "cater.external_agent.time.monotonic", return_value=0) as clock:
                budget = _ProgressBudget(2, 10)
                clock.return_value = 1
                self.assertTrue(budget.settled(ToolCall("one", "workspace_run", {
                    "command": "check"}), self.failed(exit_code=code)))
                self.assertEqual(budget.progress_at, 1)
                self.assertEqual(len(budget.seen), 1)
                clock.return_value = 2.5
                budget.check()
                clock.return_value = 3
                with self.assertRaises(ExternalRunLimit) as caught:
                    budget.check()
                self.assertEqual(caught.exception.limit, "idle")

    def test_real_host_python_red_test_receipt_stays_failed_and_renews_idle(self):
        script = "\n".join([
            "import unittest",
            "class DeliberatelyRed(unittest.TestCase):",
            "    def test_expected_red(self):",
            "        self.assertEqual(1, 2, 'expected host red test')",
            "unittest.main(verbosity=2)",
        ])
        executor = self.executor(script)
        call = ToolCall("real", "workspace_run", {"command": "check"})
        # Keep real process deadlines independent of the deterministic budget clock.
        receipt = executor.execute(call.name, call.arguments)
        self.host_receipt = deepcopy(receipt)
        self.assertEqual(receipt["status"], "error", receipt)
        self.assertEqual(receipt["outcome"], "failed", receipt)
        self.assertIs(type(receipt["exit_code"]), int)
        self.assertEqual(receipt["exit_code"], 1)
        self.assertIs(receipt["complete"], True)
        self.assertIs(receipt["timed_out"], False)
        self.assertEqual(receipt["error"]["code"], "failed")
        self.assertEqual(receipt["argv"], list(executor.policy.commands["check"]))
        self.assertIn("test_expected_red", receipt["stderr"]["excerpt"])
        self.assertIn("Ran 1 test", receipt["output"])
        self.assertIn("FAILED (failures=1)", receipt["output"])
        self.assertTrue(receipt["stdout"]["complete"])
        self.assertTrue(receipt["stderr"]["complete"])
        before = deepcopy(receipt)
        with patch("cater.external_agent.time.monotonic", return_value=0) as clock:
            run = self.run_for(lambda _: receipt)
            clock.return_value = 1
            self.assertIs(run._call(call), receipt)
            self.assertEqual(receipt, before)
            self.assertEqual(dict(run.state.observations).get("progress_receipts"), 1)
            clock.return_value = 2.5
            run._budget.check()

    def test_known_failed_receipt_is_returned_unchanged_with_host_accounting(self):
        receipt = self.failed(output="assertion failed", duration_ms=250,
                              stdout={"complete": True, "excerpt": "red"})
        before = deepcopy(receipt)
        calls = []
        with patch("cater.external_agent.time.monotonic", return_value=0) as clock:
            def execute(call):
                calls.append(call)
                clock.return_value = 1.25
                return receipt

            run = self.run_for(execute)
            call = ToolCall("one", "workspace_run", {"command": "check"})
            clock.return_value = 1
            self.assertIs(run._call(call), receipt)
            self.assertEqual(receipt, before)
            self.assertEqual(calls, [call])
            observations = dict(run.state.observations)
            self.assertEqual(observations["tool_calls"], 1)
            self.assertEqual(observations["tool_elapsed_ms"], 250)
            self.assertEqual(observations.get("progress_receipts"), 1)
            self.assertEqual(observations["last_progress_ms"], 1250)
            self.assertEqual(observations["last_tool_end_ms"], 1250)
            self.assertEqual(run._budget.progress_at, 1.25)
            self.assertIsNone(run._failure)

    def test_near_miss_receipts_never_renew_idle_or_consume_dedup_key(self):
        cases = [("non-dict", "workspace_run", "failed")]
        for name in ("workspace_read", "inspect", "run"):
            cases.append(("tool-" + name, name, self.failed()))
        variants = {
            "status": ("ok", "failed", "unknown", None),
            "outcome": ("passed", "timeout", "not_started", "effects_unknown", "unknown", "launch_error", None),
            "complete": (False, 1, "true", None),
            "timed_out": (True, 0, "false", None),
            "exit_code": (True, False, 0, 1.0, "1", None),
            "error": ({"code": "process_failed"}, {"code": "permission_denied"},
                      {"code": "invalid_arguments"}, {"code": "unknown"}, {}, None, "failed"),
        }
        for field, values in variants.items():
            for value in values:
                cases.append((f"{field}={value!r}", "workspace_run", self.failed(**{field: value})))
            missing = self.failed()
            del missing[field]
            cases.append(("missing-" + field, "workspace_run", missing))
        for label, name, receipt in cases:
            with self.subTest(case=label), patch(
                    "cater.external_agent.time.monotonic", return_value=0) as clock:
                budget = _ProgressBudget(2, 10)
                clock.return_value = 1
                self.assertFalse(budget.settled(ToolCall("bad", name, {"command": "check"}), receipt))
                self.assertEqual(budget.progress_at, 0)
                self.assertEqual(budget.seen, set())
                clock.return_value = 2
                self.assertEqual(budget.expired().limit, "idle")

    def test_duplicate_call_ids_key_order_and_success_failure_share_one_progress_key(self):
        success = {"status": "ok", "outcome": "passed", "complete": True, "exit_code": 0}
        for first, repeat in ((self.failed(), self.failed()),
                              (self.failed(), success), (success, self.failed())):
            with self.subTest(first=first["status"], repeat=repeat["status"]), patch(
                    "cater.external_agent.time.monotonic", return_value=0) as clock:
                budget = _ProgressBudget(2, 10)
                args = {"command": "check", "args": ["one", "two"], "cwd": "."}
                clock.return_value = 1
                self.assertTrue(budget.settled(ToolCall("original", "workspace_run", args), first))
                clock.return_value = 2
                reordered = {"cwd": ".", "args": ["one", "two"], "command": "check"}
                self.assertFalse(budget.settled(ToolCall("new-id", "workspace_run", reordered), repeat))
                self.assertEqual(budget.progress_at, 1)
                self.assertEqual(len(budget.seen), 1)
                clock.return_value = 2.5
                # Arguments, rather than the transport id, distinguish new work.
                self.assertTrue(budget.settled(ToolCall("original", "workspace_run", {
                    **args, "args": ["two", "one"]}), self.failed()))
                self.assertEqual(len(budget.seen), 2)

    def test_distinct_failed_commands_cannot_extend_fixed_total(self):
        with patch("cater.external_agent.time.monotonic", return_value=0) as clock:
            budget = _ProgressBudget(2, 5)
            for index, when in enumerate((1, 2.5, 4)):
                clock.return_value = when
                self.assertTrue(budget.settled(ToolCall(str(index), "workspace_run", {
                    "command": "check", "args": [str(index)]}), self.failed()))
            self.assertEqual(budget.started, 0)
            self.assertEqual(budget.total, 5)
            clock.return_value = 5
            self.assertFalse(budget.settled(ToolCall("late", "workspace_run", {
                "command": "check", "args": ["late"]}), self.failed()))
            self.assertEqual(budget.progress_at, 4)
            self.assertEqual(len(budget.seen), 3)
            with self.assertRaises(ExternalRunLimit) as caught:
                budget.check()
            self.assertEqual(caught.exception.limit, "total")

    def test_late_failed_receipt_is_preserved_without_reviving_or_dispatching_again(self):
        for total, prior, start, end, limit in ((10, 0, 1, 2, "idle"), (3, 1, 2, 3, "total")):
            with self.subTest(limit=limit), patch(
                    "cater.external_agent.time.monotonic", return_value=0) as clock:
                receipt = self.failed()
                before = deepcopy(receipt)
                dispatched = []

                def execute(call):
                    dispatched.append(call)
                    clock.return_value = end
                    return receipt

                run = self.run_for(execute, total=total)
                if prior:
                    clock.return_value = prior
                    self.assertTrue(run._budget.settled(
                        ToolCall("prior", "inspect", {}), {"status": "ok"}))
                seen = set(run._budget.seen)
                clock.return_value = start
                call = ToolCall("late", "workspace_run", {"command": "check"})
                self.assertIs(run._call(call), receipt)
                self.assertEqual(receipt, before)
                self.assertEqual(run._budget.progress_at, prior)
                self.assertEqual(run._budget.seen, seen)
                self.assertNotIn("progress_receipts", dict(run.state.observations))
                self.assertEqual(dict(run.state.observations)["tool_calls"], 1)
                with self.assertRaises(ExternalRunLimit) as caught:
                    run._call(ToolCall("never", "workspace_run", {"command": "check", "args": ["new"]}))
                self.assertEqual(caught.exception.limit, limit)
                self.assertEqual(dispatched, [call])

    def test_real_cleanup_failure_has_process_failed_code_and_cannot_renew_idle(self):
        executor = self.executor("raise SystemExit(1)")
        close = ProcessScope.close

        def close_then_report_failure(scope):
            close(scope)
            raise OSError("injected cleanup failure after actual process cleanup")

        with patch.object(ProcessScope, "close", close_then_report_failure):
            receipt = executor.execute("workspace_run", {"command": "check"})
        self.host_receipt = deepcopy(receipt)
        self.assertEqual(receipt["status"], "error", receipt)
        self.assertEqual(receipt["outcome"], "failed")
        self.assertEqual(receipt["exit_code"], 1)
        self.assertIs(receipt["complete"], True)
        self.assertIs(receipt["timed_out"], False)
        self.assertEqual(receipt["error"]["code"], "process_failed")
        with patch("cater.external_agent.time.monotonic", return_value=0) as clock:
            run = self.run_for(lambda _: receipt)
            clock.return_value = 1
            self.assertIs(run._call(ToolCall("cleanup", "workspace_run", {"command": "check"})), receipt)
            self.assertEqual(run._budget.progress_at, 0)
            self.assertEqual(run._budget.seen, set())
            self.assertNotIn("progress_receipts", dict(run.state.observations))
            clock.return_value = 2
            self.assertEqual(run._budget.expired().limit, "idle")


if __name__ == "__main__":
    unittest.main()
