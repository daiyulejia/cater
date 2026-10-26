"""Native request failures remain visible even after earlier reported tool turns."""
import unittest

from cater.events import Actor
from cater.runtime import Runtime


HOST = Actor.system("team")


class NativeRequestCostTests(unittest.TestCase):
    def setUp(self):
        self.rt = Runtime(":memory:")
        self.addCleanup(self.rt.close)
        self.task = self.rt.create_task("Inspect a native request")

    def emit(self, kind, payload, *, task=None, actor=HOST):
        return self.rt.log.emit(type=kind, actor=actor, task_id=task or self.task, payload=payload)

    def start(self, **overrides):
        payload = {"run_id": "run", "role": "dev", "message_seq": 5, **overrides}
        return self.emit("team.run_started", payload)

    def finish(self, *, task=None, actor=HOST, **overrides):
        payload = {"run_id": "run", "role": "dev", "message_seq": 5, "status": "failed",
                   "failure_stage": "model_request", "failure_kind": "network_error",
                   "transport_phase": "request", **overrides}
        return self.emit("team.run_finished", payload, task=task, actor=actor)

    def lens(self):
        before = self.rt.log.read()
        lens = next(item for item in self.rt.review(self.task)["lenses"] if item["id"] == "cost")
        self.assertEqual(self.rt.log.read(), before)
        return lens

    def test_failed_second_request_does_not_disappear_behind_first_usage(self):
        self.start()
        self.emit("team.model_called", {"run_id": "run", "role": "dev", "driver": "native",
                                       "tokens": 10227, "token_usage": "reported"})
        self.finish(transport_issue="remote_disconnected")
        result = self.lens()
        self.assertEqual(result["status"], "watch")
        self.assertIn("10227 token(s)", result["finding"])
        self.assertIn("1 native model failure receipt(s) have unverified usage", result["finding"])
        self.assertEqual(self.rt.task(self.task).tokens, 10227)
        self.assertEqual(self.rt.task(self.task).token_usage, {"reported": 1})

    def test_failure_before_any_usage_does_not_look_like_no_model_activity(self):
        self.start()
        self.finish()
        self.assertEqual(self.lens()["status"], "watch")
        self.assertIn("not a complete billing total", self.lens()["finding"])
        self.assertNotIn("No model usage records", self.lens()["finding"])

    def test_unknown_and_estimated_records_remain_disclosed(self):
        self.start()
        for source in ("unavailable", "estimated"):
            self.emit("team.model_called", {"driver": "native", "tokens": 0, "token_usage": source})
        self.finish()
        self.emit("driver.failed", {"driver": "legacy", "attempt": 1}, actor=Actor.system("driver_gateway"))
        self.assertIn("1 unavailable and 1 estimated", self.lens()["finding"])
        self.assertIn("1 failed legacy driver attempt", self.lens()["finding"])

    def test_transport_failure_kinds_and_phases_are_supported(self):
        for kind in ("network_error", "timeout", "http_error"):
            for phase in ("request", "response_read"):
                with self.subTest(kind=kind, phase=phase):
                    self.task = self.rt.create_task("Transport")
                    self.start()
                    self.finish(failure_kind=kind, transport_phase=phase, status="interrupted")
                    self.assertIn("1 native model failure receipt", self.lens()["finding"])

    def test_parent_timeout_without_dispatch_phase_is_uncertain_not_a_billable_claim(self):
        for kind in ("timeout", None):
            with self.subTest(kind=kind):
                self.task = self.rt.create_task("Uncertain transport")
                self.start()
                self.finish(failure_kind=kind, transport_phase=None, error_type="DriverUnavailable")
                result = self.lens()
                self.assertIn("1 native model failure receipt", result["finding"])
                self.assertIn("does not prove a request reached the provider or was billed", result["action"])

    def test_unparseable_response_without_usage_remains_unknown(self):
        self.start()
        self.emit("team.model_called", {"run_id": "run", "role": "dev", "driver": "native", "tokens": 20,
                                       "token_usage": "reported", "context_basis": {"as_of_seq": 1}})
        self.finish(failure_stage="response_handling", failure_kind=None, error_type="DriverProtocolError",
                    context_basis={"as_of_seq": 2})
        self.assertIn("1 native model failure receipt", self.lens()["finding"])

    def test_protocol_response_usage_receipt_is_not_counted_again(self):
        for source in ("reported", "unavailable"):
            with self.subTest(source=source):
                self.task = self.rt.create_task("Malformed response with a usage receipt")
                self.start()
                self.emit("team.model_called", {"run_id": "run", "role": "dev", "driver": "native", "tokens": 20,
                                               "token_usage": source, "context_basis": {"as_of_seq": 2}})
                self.finish(failure_stage="response_handling", failure_kind=None, error_type="DriverProtocolError",
                            context_basis={"as_of_seq": 2})
                self.assertNotIn("native model failure receipt", self.lens()["finding"])
                self.assertEqual(self.rt.task(self.task).token_usage, {source: 1})

    def test_unrelated_or_untrusted_receipt_cannot_account_for_protocol_failure(self):
        for mismatch in ("task", "run", "role", "actor", "order", "basis", "missing-basis"):
            with self.subTest(mismatch=mismatch):
                self.task = self.rt.create_task("Unrelated usage")
                body = {"run_id": "other" if mismatch == "run" else "run",
                        "role": "other" if mismatch == "role" else "dev", "driver": "native", "tokens": 20,
                        "token_usage": "reported", "context_basis": {} if mismatch == "basis" else {"as_of_seq": 2}}
                if mismatch == "missing-basis":
                    body.pop("context_basis")
                if mismatch != "order":
                    self.start()
                self.emit("team.model_called", body, task=self.rt.create_task("Other") if mismatch == "task" else None,
                          actor=Actor.agent("dev", "dev") if mismatch == "actor" else HOST)
                if mismatch == "order":
                    self.start()
                self.finish(failure_stage="response_handling", failure_kind=None, error_type="DriverProtocolError",
                            context_basis=None if mismatch == "missing-basis" else {"as_of_seq": 2})
                self.assertIn("1 native model failure receipt", self.lens()["finding"])

    def test_preflight_local_tool_and_response_failures_are_not_guessed_as_new_requests(self):
        for changes in ({"failure_kind": "missing_credentials"}, {"failure_kind": "invalid_request"},
                        {"failure_kind": "invalid_request", "failure_stage": "response_handling", "error_type": "DriverProtocolError"},
                        {"failure_kind": "missing_credentials", "failure_stage": "response_handling", "error_type": "DriverProtocolError"},
                        {"failure_kind": "unknown"}, {"failure_stage": "response_handling"},
                        {"failure_stage": "tool_execution"},
                        {"admission_not_started": True}, {"status": "completed"}):
            with self.subTest(changes=changes):
                self.task = self.rt.create_task("Other failure")
                self.start()
                self.finish(**changes)
                self.assertNotIn("native model failure receipt", self.lens()["finding"])

    def test_invalid_or_mismatched_identity_cannot_supply_failure_evidence(self):
        for field, value in (("run_id", "different"), ("run_id", []), ("role", "other"),
                             ("role", {}), ("message_seq", 6), ("message_seq", True)):
            with self.subTest(field=field, value=value):
                self.task = self.rt.create_task("Identity")
                self.start()
                self.finish(**{field: value})
                self.assertNotIn("native model failure receipt", self.lens()["finding"])

    def test_matching_start_order_actor_and_scope_are_required(self):
        for case in ("no-start", "late-start", "agent-start", "agent-finish", "other-task"):
            with self.subTest(case=case):
                self.task = self.rt.create_task(case)
                if case not in ("no-start", "late-start"):
                    self.emit("team.run_started", {"run_id": "run", "role": "dev", "message_seq": 5},
                              actor=Actor.agent("dev", "dev") if case == "agent-start" else HOST)
                self.finish(actor=Actor.agent("dev", "dev") if case == "agent-finish" else HOST,
                            task=self.rt.create_task("Other") if case == "other-task" else None)
                if case == "late-start":
                    self.start()
                self.assertNotIn("native model failure receipt", self.lens()["finding"])

    def test_duplicate_terminal_records_do_not_duplicate_unknown_usage(self):
        self.start()
        self.finish()
        self.finish()
        self.assertIn("1 native model failure receipt", self.lens()["finding"])

    def test_later_success_does_not_erase_historical_unknown_usage(self):
        self.start()
        self.finish()
        self.start(run_id="retry")
        self.emit("team.model_called", {"driver": "native", "tokens": 20, "token_usage": "reported"})
        self.finish(run_id="retry", status="completed", failure_kind=None)
        self.assertIn("1 native model failure receipt", self.lens()["finding"])

    def test_multiple_failures_for_same_message_are_not_collapsed_to_latest_run(self):
        for run in ("one", "two"):
            self.start(run_id=run)
            self.finish(run_id=run)
        self.assertIn("2 native model failure receipt", self.lens()["finding"])


class NativeRequestRuntimeCostTests(unittest.TestCase):
    def test_actual_failure_receipt_is_visible_without_a_model_usage_record(self):
        from cater.drivers import DriverUnavailable
        from test_simple_team import SimpleTeamTests

        SimpleTeamTests.setUp(self)
        error = DriverUnavailable("Private provider text must not reach the cost summary")
        error.failure_kind = "network_error"
        error.transport_phase = "request"
        error.transport_issue = "remote_disconnected"
        self.driver.steps[self.manager] = [error]
        self.rt.team.receive(self.task, "Implement and verify the tool", request_id="cost-failure")
        self.rt.collaborate(self.task)
        before = self.rt.log.read()
        cost = next(item for item in self.rt.review(self.task)["lenses"] if item["id"] == "cost")
        self.assertEqual(self.rt.log.read(), before)
        self.assertEqual(len(self.driver.calls), 1)
        self.assertIn("1 native model failure receipt", cost["finding"])
        self.assertNotIn("Private provider text", str(cost))
        self.assertEqual(self.rt.task(self.task).token_usage, {})


if __name__ == "__main__":
    unittest.main()
