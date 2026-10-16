"""Model failure diagnostics remain readable, bounded, and non-replaying."""
import json
import unittest

from cater.drivers import DriverUnavailable
import test_simple_team


class TeamFailureDiagnosticsTests(unittest.TestCase):
    setUp = test_simple_team.SimpleTeamTests.setUp
    events = test_simple_team.SimpleTeamTests.events

    def fail(self, fields):
        error = DriverUnavailable("DO-NOT-PERSIST provider message, credential, or response body")
        error.__dict__.update(fields)
        before = len(self.driver.calls)
        self.driver.steps[self.manager] = [error]
        self.rt.team.receive(self.task, "Continue the same work", request_id=f"diagnose-{before}")
        self.rt.collaborate(self.task)
        self.assertEqual(len(self.driver.calls), before + 1)
        self.assertEqual(self.events("team.tool_started"), [])
        self.assertEqual(self.events("team.objective_completed"), [])
        run = self.events("team.run_finished")[-1]
        self.assertEqual(run.payload["status"], "failed")
        self.assertNotIn("DO-NOT-PERSIST", json.dumps([e.payload for e in self.rt.log.read(task_id=self.task)]))
        self.assertTrue(self.rt.verify_chain().ok)
        return run.payload

    def test_http_codes_are_retained_without_inventing_balance_or_automatic_retry(self):
        for code in (400, 401, 402, 413, 429, 500, 503):
            with self.subTest(code=code):
                result = self.fail({"failure_kind": "http_error", "status_code": code, "transport_phase": "request"})
                self.assertEqual(result["status_code"], code)
                self.assertIn(f"HTTP {code}", result["reason"])
                self.assertNotIn("余额", result["reason"])
                self.assertEqual(result["failure_kind"], "http_error")

    def test_network_and_timeout_distinguish_phase_without_claiming_no_remote_effect(self):
        for kind in ("network_error", "timeout"):
            for phase in ("request", "response_read"):
                with self.subTest(kind=kind, phase=phase):
                    result = self.fail({"failure_kind": kind, "transport_phase": phase})
                    self.assertEqual(result["failure_kind"], kind)
                    self.assertEqual(result["transport_phase"], phase)
                    self.assertIn("计费尚不确定", result["reason"])
                    if phase == "response_read":
                        self.assertIn("接收响应时", result["reason"])

    def test_credentials_and_invalid_request_are_actionable_without_private_details(self):
        for kind, label in (("missing_credentials", "未读到该通道的凭据"), ("invalid_request", "配置未通过检查")):
            with self.subTest(kind=kind):
                result = self.fail({"failure_kind": kind})
                self.assertEqual(result["failure_kind"], kind)
                self.assertIn(label, result["reason"])

    def test_unknown_failure_does_not_get_guessed_metadata(self):
        result = self.fail({"failure_kind": "private-provider-body", "transport_phase": "credential", "status_code": True,
                            "headers": {"Authorization": "DO-NOT-PERSIST"}})
        self.assertNotIn("failure_kind", result)
        self.assertNotIn("transport_phase", result)
        self.assertNotIn("status_code", result)
        self.assertNotIn("headers", result)
