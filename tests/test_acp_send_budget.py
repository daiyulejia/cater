"""Deterministic in-memory ACP deadlines; no subprocess, socket, or model."""

from pathlib import Path
import queue
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from cater.acp import AcpSession, AcpTimeout
from cater.external_agent import ExternalRunLimit, QoderAcpDriver, _ProgressBudget


class AcpSendBudgetTests(unittest.TestCase):
    def setUp(self):
        self.process = self.enterContext(patch(
            "subprocess.Popen", side_effect=AssertionError("No process may be started")))
        self.bridge = self.enterContext(patch(
            "cater.external_agent.McpToolBridge", side_effect=AssertionError("No bridge may be opened")))

    def tearDown(self):
        self.process.assert_not_called()
        self.bridge.assert_not_called()

    def session(self):
        client = AcpSession(["never-launch"], cwd=Path(__file__).resolve().parent, timeout=2)
        client.session_id = "memory-session"
        self.addCleanup(client.close)
        return client

    def run_with_budget(self):
        driver = QoderAcpDriver("offline", "never-launch", "ExactModel", timeout=2, max_run_seconds=5)
        run = driver.agent_run(cwd=Path(__file__).parent, tools=[], execute=Mock(),
                               check_current=lambda: None, on_update=None)
        run._phase = "prompting"
        run._budget = _ProgressBudget(2, 5)
        return run

    def test_prompt_override_caps_send_without_shortening_or_restarting_response_budget(self):
        for override, send_deadline, response_deadline in ((None, 102, 102), (10, 102, 110), (1, 101, 101)):
            with self.subTest(override=override):
                client = self.session()
                with patch("cater.acp.time.monotonic", return_value=100) as clock:
                    def sent(message, deadline, *, service):
                        self.assertTrue(callable(service))
                        self.assertEqual(message["method"], "session/prompt")
                        self.assertEqual(deadline, send_deadline)
                        clock.return_value = 100.25

                    with patch.object(client, "_send", side_effect=sent) as send, patch.object(
                        client, "_receive", return_value={
                            "jsonrpc": "2.0", "id": "cater-1", "result": {"stopReason": "end_turn"},
                        },
                    ) as receive:
                        result = client.prompt("offline", timeout=override)
                self.assertEqual(result.stop_reason, "end_turn")
                send.assert_called_once()
                self.assertEqual(receive.call_args.args, (response_deadline,))
                receive.assert_called_once()
                self.assertEqual(client.timeout, 2)
                self.assertFalse(client._prompt_active)

    def test_write_lock_timeout_is_input_and_preserves_prompt_uncertainty(self):
        client = self.session()
        lock = Mock()
        lock.acquire.return_value = False
        with patch("cater.acp.time.monotonic", return_value=100), patch.object(client, "_write_lock", lock):
            with self.assertRaises(AcpTimeout) as caught:
                client.prompt("offline", timeout=10)
        self.assertEqual(caught.exception.phase, "input")
        self.assertTrue(caught.exception.prompt_may_have_run)
        lock.acquire.assert_called_once_with(timeout=2)
        lock.release.assert_not_called()
        self.assertFalse(client._prompt_active)

    def test_unfinished_pipe_write_timeout_is_input_without_starting_a_thread(self):
        client = self.session()
        output = Mock()
        output.get.side_effect = queue.Empty
        pipe = Mock()
        with patch("cater.acp.time.monotonic", return_value=100), patch.object(
            client, "_proc", SimpleNamespace(stdin=pipe),
        ), patch("cater.acp.queue.Queue", return_value=output), patch("cater.acp.threading.Thread"):
            with self.assertRaises(AcpTimeout) as caught:
                client._send({"jsonrpc": "2.0", "method": "offline"}, 102)
        self.assertEqual(caught.exception.phase, "input")
        output.get.assert_called_once_with(timeout=2)
        pipe.write.assert_not_called()
        pipe.flush.assert_not_called()

    def test_receive_timeout_marks_response_at_each_wait_boundary(self):
        for boundary in ("already_expired", "after_service", "empty_queue"):
            with self.subTest(boundary=boundary):
                client = self.session()
                frames = Mock()
                frames.get.side_effect = queue.Empty
                with patch("cater.acp.time.monotonic", return_value=100) as clock, patch.object(
                    client, "_frames", frames,
                ):
                    if boundary == "already_expired":
                        deadline, service = 100, None
                    elif boundary == "after_service":
                        deadline = 102
                        service = lambda: setattr(clock, "return_value", 102)
                    else:
                        deadline, service = 102, None
                    with self.assertRaises(AcpTimeout) as caught:
                        client._receive(deadline, service=service)
                self.assertEqual(caught.exception.phase, "response")
                if boundary == "empty_queue":
                    frames.get.assert_called_once_with(timeout=2)
                else:
                    frames.get.assert_not_called()

    def test_cancel_keeps_default_send_window_after_prompt_override(self):
        client = self.session()
        with patch("cater.acp.time.monotonic", return_value=100), patch.object(
            client, "_send",
        ) as send, patch.object(client, "_receive", return_value={
            "jsonrpc": "2.0", "id": "cater-1", "result": {"stopReason": "end_turn"},
        }):
            client.prompt("offline", timeout=10)
            client._prompt_active = True
            self.assertTrue(client.cancel())
        self.assertEqual(send.call_count, 2)
        message, deadline = send.call_args.args
        self.assertEqual(message["method"], "session/cancel")
        self.assertEqual(deadline, 102)
        self.assertEqual(client.timeout, 2)

    def test_legacy_timeout_constructor_retains_optional_uncertainty_flag(self):
        default = AcpTimeout("legacy")
        uncertain = AcpTimeout("legacy", prompt_may_have_run=True)
        explicit_none = AcpTimeout("legacy", phase=None, prompt_may_have_run=True)
        self.assertIsNone(default.phase)
        self.assertFalse(default.prompt_may_have_run)
        for error in (uncertain, explicit_none):
            self.assertIsNone(error.phase)
            self.assertTrue(error.prompt_may_have_run)

    def test_input_and_unclassified_timeouts_are_never_replaced_by_expired_budget(self):
        for phase in ("input", None):
            for now, last_progress in ((1, 0), (3, 0), (5, 4)):
                with self.subTest(phase=phase, now=now, last_progress=last_progress):
                    with patch("cater.external_agent.time.monotonic", return_value=0) as clock:
                        run = self.run_with_budget()
                        run._budget.progress_at = last_progress
                        clock.return_value = now
                        error = AcpTimeout("transport timeout", phase=phase, prompt_may_have_run=True)
                        run._stop(error, phase="prompting")
                    self.assertIs(run._failure, error)
                    self.assertEqual(run._failure.phase, phase)
                    self.assertTrue(run._failure.prompt_may_have_run)
                    self.assertTrue(run._tools_revoked)
                    self.assertEqual(run.state.failure_phase, "prompting")

    def test_response_timeout_can_classify_expired_idle_and_total_budgets(self):
        for now, last_progress, expected in ((3, 0, "idle"), (5, 4, "total")):
            with self.subTest(expected=expected):
                with patch("cater.external_agent.time.monotonic", return_value=0) as clock:
                    run = self.run_with_budget()
                    run._budget.progress_at = last_progress
                    clock.return_value = now
                    error = AcpTimeout("response timeout", phase="response", prompt_may_have_run=True)
                    run._stop(error, phase="prompting")
                self.assertIsInstance(run._failure, ExternalRunLimit)
                self.assertEqual(run._failure.limit, expected)
                self.assertTrue(run._tools_revoked)
                self.assertEqual(run.state.failure_phase, "prompting")

    def test_response_timeout_before_budget_expiry_preserves_original_error(self):
        with patch("cater.external_agent.time.monotonic", return_value=0) as clock:
            run = self.run_with_budget()
            clock.return_value = 1
            error = AcpTimeout("response timeout", phase="response", prompt_may_have_run=True)
            run._stop(error)
        self.assertIs(run._failure, error)
        self.assertTrue(run._failure.prompt_may_have_run)

    def test_timeout_phases_do_not_enable_budget_classification_in_legacy_mode(self):
        for phase in (None, "input", "response"):
            with self.subTest(phase=phase):
                run = self.run_with_budget()
                run._budget = None
                error = AcpTimeout("legacy timeout", phase=phase, prompt_may_have_run=True)
                run._stop(error)
                self.assertIs(run._failure, error)
                self.assertTrue(run._failure.prompt_may_have_run)

    def test_existing_budget_failure_is_not_replaced_by_a_later_timeout(self):
        with patch("cater.external_agent.time.monotonic", return_value=0) as clock:
            run = self.run_with_budget()
            original = ExternalRunLimit("total")
            run._stop(original)
            clock.return_value = 6
            run._stop(AcpTimeout("input timeout", phase="input"))
        self.assertIs(run._failure, original)
        self.assertEqual(run._failure.limit, "total")


if __name__ == "__main__":
    unittest.main()
