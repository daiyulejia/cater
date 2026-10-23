"""Native health observations through loopback HTTP, never remote models."""

from contextlib import contextmanager, nullcontext
import time
import unittest
from unittest.mock import Mock, call, patch

from cater.availability import team_availability
from cater.runtime import Runtime
from cater.team import HOST
from cater.work import WorkConflict
import test_protocol_usage as protocol


def contained(cls, *, truncated=False):
    data = protocol.response(cls, tool=True)
    if cls is protocol.AnthropicMessagesDriver:
        if truncated:
            data["stop_reason"] = "max_tokens"
        else:
            data["content"][0]["input"] = "INVALID_TOOL_ARGUMENTS"
    elif truncated:
        data["choices"][0]["finish_reason"] = "length"
    else:
        data["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = "INVALID_TOOL_ARGUMENTS"
    return data


def rejected_replies(cls):
    return (
        ("http_503", (503, {"error": "synthetic unavailable"})),
        ("invalid_json", (200, b"INVALID_JSON")),
        ("protocol_shape", (200, protocol.malformed(cls))),
        ("tool_format", (200, contained(cls))),
        ("truncation", (200, contained(cls, truncated=True))),
    )


class TeamNativeHealthTests(unittest.TestCase):
    # Reuse only the session, so importing this module does not rerun its tests.
    session = protocol.ProtocolUsageTests.session

    @contextmanager
    def observations(self, fixture):
        health = fixture.rt._driver_health
        trace = Mock()
        with patch.object(health, "record_success", wraps=health.record_success) as success, \
                patch.object(health, "record_rejection", wraps=health.record_rejection) as rejection, \
                patch.object(health, "record_failure", wraps=health.record_failure) as failure:
            trace.attach_mock(success, "record_success")
            trace.attach_mock(rejection, "record_rejection")
            trace.attach_mock(failure, "record_failure")
            yield trace

    def run_message(self, fixture):
        fixture.rt.team.receive(fixture.task, "Check this synthetic fixture input")
        fixture.rt.collaborate(fixture.task)
        self.assertTrue(fixture.rt.verify_chain().ok)

    def start(self, fixture, *, worker=False):
        if worker:
            fixture.rt.team._send(fixture.task, fixture.rt.organization.leader,
                                  fixture.worker, "Check this fixture assignment", "assignment")
        else:
            fixture.rt.team.receive(fixture.task, "Check this synthetic fixture input")
        claimed = fixture.rt.team._claim(fixture.task)
        self.assertIsNotNone(claimed)
        running = fixture.rt.team._run_message(fixture.task, *claimed)
        self.addCleanup(running.close)
        return running

    def change(self, fixture, driver, kind):
        if kind == "pause":
            fixture.rt.execution_control.change(fixture.task, "pause", request_id="native-health-pause")
        elif kind == "customer":
            fixture.rt.team.receive(fixture.task, "Use this new fixture input")
        elif kind == "version":
            fixture.rt.log.emit(type="team.objective_updated", actor=HOST, task_id=fixture.task,
                                payload={"goal": "A new fixture objective"})
        elif kind == "budget":
            driver.token_budget = fixture.rt._reported_driver_tokens("team_test")
        else:
            self.fail("Unknown fixture change: " + kind)

    def assert_members(self, rt, status, reason):
        members = team_availability(rt)["members"]
        ids = {rt.organization.leader.id, *(role.id for role in rt.roles)}
        self.assertEqual({member["role_id"] for member in members}, ids)
        self.assertGreater(len(ids), 1)
        self.assertEqual({(member["status"], member["reason"]) for member in members}, {(status, reason)})
        self.assertEqual(team_availability(rt)["all_available"], status == "available")

    def assert_failure_count(self, health, count):
        if count:
            self.assertTrue(health.is_circuit_open("team_test", count))
        self.assertFalse(health.is_circuit_open("team_test", count + 1))

    def test_native_success_makes_every_role_sharing_the_driver_available(self):
        for cls in protocol.CLASSES:
            with self.subTest(protocol=cls.__name__), self.session(
                    cls, [(200, protocol.response(cls))]) as (f, _, requests):
                self.assert_members(f.rt, "unverified", "not_recently_checked")
                with self.observations(f) as trace:
                    self.run_message(f)
                self.assertEqual(len(requests), 1)
                self.assertEqual(f.events("team.run_finished")[-1].payload["status"], "completed")
                self.assertEqual(f.rt.task(f.task).tokens, 14)
                self.assertEqual(f.rt._reported_driver_tokens("team_test"), 14)
                self.assert_members(f.rt, "available", "recent_success")
                self.assertEqual(trace.mock_calls, [call.record_success("team_test")])
                self.assert_failure_count(f.rt._driver_health, 0)

    def test_rejected_native_response_replaces_previous_success_without_retry(self):
        for cls in protocol.CLASSES:
            for issue, reply in rejected_replies(cls):
                with self.subTest(protocol=cls.__name__, issue=issue), self.session(
                        cls, [(200, protocol.response(cls)), reply]) as (f, _, requests):
                    with self.observations(f) as trace:
                        self.run_message(f)
                        self.run_message(f)
                    self.assertEqual(len(requests), 2)
                    self.assertEqual(f.events("team.run_finished")[-1].payload["status"], "failed")
                    self.assertEqual(f.events("team.tool_started"), [])
                    self.assertEqual(f.events("team.response_correction_requested"), [])
                    self.assert_members(f.rt, "unverified", "recent_failure")
                    self.assertEqual(trace.mock_calls, [call.record_success("team_test"),
                                                       call.record_rejection("team_test")])
                    self.assert_failure_count(f.rt._driver_health, 0)

    def test_rejection_replaces_a_known_recent_success_without_clock_patching(self):
        for cls in protocol.CLASSES:
            for issue, reply in rejected_replies(cls):
                with self.subTest(protocol=cls.__name__, issue=issue), self.session(
                        cls, [reply]) as (f, _, requests):
                    health = f.rt._driver_health
                    health.record_success("team_test")
                    self.assert_members(f.rt, "available", "recent_success")
                    with self.observations(f) as trace:
                        self.run_message(f)
                    self.assertEqual(len(requests), 1)
                    self.assertEqual(trace.mock_calls, [call.record_rejection("team_test")])
                    self.assertEqual(health.recent_state("team_test"), "failed")
                    self.assert_members(f.rt, "unverified", "recent_failure")
                    self.assert_failure_count(health, 0)

    def test_rejection_refreshes_observation_preserves_counter_and_success_clears_it(self):
        for cls in protocol.CLASSES:
            for issue, reply in rejected_replies(cls):
                with self.subTest(protocol=cls.__name__, issue=issue), self.session(
                        cls, [reply, (200, protocol.response(cls))]) as (f, _, requests):
                    health = f.rt._driver_health
                    stale = time.monotonic() - 301
                    with patch("cater.runtime.time", wraps=time) as health_clock:
                        health_clock.monotonic.return_value = stale
                        health.record_failure("team_test")
                    self.assertEqual(health.recent_state("team_test"), "unverified")
                    with self.observations(f) as trace:
                        self.run_message(f)
                        self.assertEqual(len(requests), 1)
                        self.assertEqual(trace.mock_calls, [call.record_rejection("team_test")])
                        self.assert_failure_count(health, 1)
                        self.assert_members(f.rt, "unverified", "recent_failure")
                        self.run_message(f)
                    self.assertEqual(len(requests), 2)
                    self.assertEqual(trace.mock_calls, [call.record_rejection("team_test"),
                                                       call.record_success("team_test")])
                    self.assert_failure_count(health, 0)
                    self.assert_members(f.rt, "available", "recent_success")

    def test_each_complete_tool_turn_records_success_before_host_execution(self):
        for cls in protocol.CLASSES:
            with self.subTest(protocol=cls.__name__), self.session(cls, [
                    (200, protocol.response(cls, tool=True)), (200, protocol.response(cls))]) as (f, _, requests):
                running = self.start(f)
                execute = f.rt.team._execute

                def execute_after_success(*args, **kwargs):
                    self.assertEqual(trace.mock_calls, [call.record_success("team_test")])
                    self.assert_members(f.rt, "available", "recent_success")
                    return execute(*args, **kwargs)

                with self.observations(f) as trace, patch.object(
                        f.rt.team, "_execute", side_effect=execute_after_success) as host:
                    protocol.advance_to_response_boundary(running, f, requests, event_type="team.tool_finished")
                    host.assert_called_once()
                    self.assertEqual(len(requests), 1)
                    self.assertEqual(trace.mock_calls, [call.record_success("team_test")])
                    self.assert_members(f.rt, "available", "recent_success")
                    self.assertEqual(f.events("team.tool_finished")[0].payload["result"]["status"], "ok")
                    list(running)
                self.assertEqual(len(requests), 2)
                self.assertEqual(trace.mock_calls, [call.record_success("team_test")] * 2)
                self.assertEqual(f.rt._reported_driver_tokens("team_test"), 28)
                self.assertEqual(f.events("team.run_finished")[-1].payload["status"], "completed")
                self.assertTrue(f.rt.verify_chain().ok)

    def test_one_format_correction_finishes_available_without_stale_failure(self):
        for cls in protocol.CLASSES:
            with self.subTest(protocol=cls.__name__), self.session(cls, [
                    (200, contained(cls)), (200, protocol.response(cls))],
                    tool_response_corrections=1) as (f, _, requests):
                f.rt._driver_health.record_failure("team_test")
                running = self.start(f)
                with self.observations(f) as trace:
                    protocol.advance_to_response_boundary(running, f, requests,
                                                          event_type="team.response_correction_requested")
                    self.assertEqual(len(requests), 1)
                    self.assertEqual(trace.mock_calls, [call.record_rejection("team_test")])
                    self.assert_members(f.rt, "unverified", "recent_failure")
                    self.assert_failure_count(f.rt._driver_health, 1)
                    self.assertEqual(f.events("team.tool_started"), [])
                    list(running)
                self.assertEqual(len(requests), 2)
                self.assertEqual(trace.mock_calls, [call.record_rejection("team_test"),
                                                   call.record_success("team_test")])
                self.assertEqual(len(f.events("team.response_correction_requested")), 1)
                calls = f.events("team.model_called")
                self.assertEqual(len(calls), 2)
                self.assertEqual(calls[0].payload["response_status"], "contained")
                self.assertEqual(calls[1].payload["correction_of"], calls[0].seq)
                self.assertEqual(f.events("team.run_finished")[-1].payload["status"], "completed")
                self.assertNotIn("response_status", f.events("team.run_finished")[-1].payload)
                self.assertEqual(f.rt._reported_driver_tokens("team_test"), 28)
                self.assert_failure_count(f.rt._driver_health, 0)
                self.assert_members(f.rt, "available", "recent_success")
                self.assertTrue(f.rt.verify_chain().ok)

    def test_failed_correction_records_two_rejections_without_recursive_retry(self):
        for cls in protocol.CLASSES:
            for issue, reply in rejected_replies(cls):
                with self.subTest(protocol=cls.__name__, issue=issue), self.session(cls, [
                        (200, contained(cls)), reply], tool_response_corrections=1) as (f, _, requests):
                    f.rt._driver_health.record_failure("team_test")
                    with self.observations(f) as trace:
                        self.run_message(f)
                    self.assertEqual(len(requests), 2)
                    self.assertEqual(trace.mock_calls, [call.record_rejection("team_test")] * 2)
                    self.assertEqual(len(f.events("team.response_correction_requested")), 1)
                    self.assertEqual(f.events("team.tool_started"), [])
                    self.assertEqual(f.events("team.run_finished")[-1].payload["status"], "failed")
                    self.assert_failure_count(f.rt._driver_health, 1)
                    self.assert_members(f.rt, "unverified", "recent_failure")

    def test_admission_failures_without_native_call_preserve_observation_and_counter(self):
        for cls in protocol.CLASSES:
            for prior in ("unverified", "succeeded", "failed"):
                for change in ("budget", "pause", "customer", "version", "context_error"):
                    with self.subTest(protocol=cls.__name__, prior=prior, change=change), self.session(
                            cls, []) as (f, driver, requests):
                        health = f.rt._driver_health
                        if prior == "succeeded":
                            health.record_success("team_test")
                        elif prior == "failed":
                            health.record_failure("team_test")
                        running = self.start(f, worker=change == "version")
                        if change != "context_error":
                            self.change(f, driver, change)
                        context = (patch.object(f.rt.team, "_model_input", side_effect=OSError("Fixture context failure"))
                                   if change == "context_error" else nullcontext())
                        with self.observations(f) as trace, context:
                            list(running)
                        self.assertEqual(requests, [])
                        self.assertEqual(trace.mock_calls, [])
                        self.assertEqual(health.recent_state("team_test"), prior)
                        self.assert_failure_count(health, int(prior == "failed"))
                        self.assertEqual(f.events("team.model_called"), [])
                        self.assertEqual(f.events("team.tool_started"), [])
                        self.assertEqual(f.rt._reported_driver_tokens("team_test"), 0)
                        expected = "failed" if change == "context_error" else "interrupted"
                        self.assertEqual(f.events("team.run_finished")[-1].payload["status"], expected)
                        self.assertTrue(f.rt.verify_chain().ok)

    def test_interruption_after_a_turn_does_not_invent_another_health_observation(self):
        for cls in protocol.CLASSES:
            for rejected in (False, True):
                for change in ("budget", "pause", "customer", "version"):
                    data = contained(cls) if rejected else protocol.response(cls, tool=True)
                    with self.subTest(protocol=cls.__name__, rejected=rejected, change=change), self.session(
                            cls, [(200, data)], tool_response_corrections=1) as (f, driver, requests):
                        running = self.start(f, worker=change == "version")
                        expected = call.record_rejection("team_test") if rejected else call.record_success("team_test")
                        with self.observations(f) as trace:
                            protocol.advance_to_response_boundary(running, f, requests,
                                event_type="team.response_correction_requested" if rejected else "team.tool_finished")
                            self.assertEqual(len(requests), 1)
                            self.assertEqual(trace.mock_calls, [expected])
                            self.change(f, driver, change)
                            list(running)
                        self.assertEqual(len(requests), 1)
                        self.assertEqual(trace.mock_calls, [expected])
                        self.assertEqual(f.rt._driver_health.recent_state("team_test"), "failed" if rejected else "succeeded")
                        self.assert_failure_count(f.rt._driver_health, 0)
                        self.assertEqual(f.rt._reported_driver_tokens("team_test"), 14)
                        self.assertEqual(f.events("team.run_finished")[-1].payload["status"], "interrupted")
                        self.assertTrue(f.rt.verify_chain().ok)

    def test_in_flight_success_remains_success_when_pause_or_new_input_discards_tools(self):
        for cls in protocol.CLASSES:
            for change in ("pause", "customer", "version"):
                with self.subTest(protocol=cls.__name__, change=change), self.session(
                        cls, [(200, protocol.response(cls, tool=True))]) as (f, driver, requests):
                    f.rt._driver_health.record_failure("team_test")
                    running = self.start(f, worker=change == "version")
                    original = driver.tool_turn

                    def turn_then_change(**kwargs):
                        turn = original(**kwargs)
                        self.change(f, driver, change)
                        return turn

                    with self.observations(f) as trace, patch.object(driver, "tool_turn", side_effect=turn_then_change):
                        list(running)
                    self.assertEqual(len(requests), 1)
                    self.assertEqual(trace.mock_calls, [call.record_success("team_test")])
                    self.assertEqual(f.events("team.tool_started"), [])
                    self.assertEqual(f.events("team.run_finished")[-1].payload["status"], "interrupted")
                    self.assert_failure_count(f.rt._driver_health, 0)
                    self.assert_members(f.rt, "available", "recent_success")
                    self.assertTrue(f.rt.verify_chain().ok)

    def test_host_tool_errors_do_not_reject_successful_native_responses(self):
        for cls in protocol.CLASSES:
            for error in (OSError, RuntimeError):
                with self.subTest(protocol=cls.__name__, error=error.__name__), self.session(cls, [
                        (200, protocol.response(cls, tool=True)), (200, protocol.response(cls))]) as (f, _, requests):
                    running = self.start(f)
                    with self.observations(f) as trace, patch.object(
                            f.rt.team, "_execute", side_effect=error("Synthetic host tool failure")):
                        list(running)
                    count = 2 if error is OSError else 1
                    self.assertEqual(len(requests), count)
                    self.assertEqual(trace.mock_calls, [call.record_success("team_test")] * count)
                    self.assertEqual(len(f.events("team.tool_started")), 1)
                    if error is OSError:
                        self.assertEqual(f.events("team.tool_finished")[0].payload["result"]["status"], "error")
                    self.assertEqual(f.events("team.run_finished")[-1].payload["status"],
                                     "completed" if error is OSError else "failed")
                    self.assertEqual(f.rt._reported_driver_tokens("team_test"), 14 * count)
                    self.assert_failure_count(f.rt._driver_health, 0)
                    self.assert_members(f.rt, "available", "recent_success")
                    self.assertTrue(f.rt.verify_chain().ok)

    def test_model_receipt_storage_error_keeps_the_actual_success_or_rejection(self):
        for cls in protocol.CLASSES:
            for rejected in (False, True):
                data = contained(cls) if rejected else protocol.response(cls)
                with self.subTest(protocol=cls.__name__, rejected=rejected), self.session(
                        cls, [(200, data)]) as (f, _, requests):
                    running = self.start(f)
                    original = f.rt.log.emit

                    def fail_model_receipt(**kwargs):
                        if kwargs["type"] == "team.model_called":
                            raise OSError("Synthetic receipt storage failure")
                        return original(**kwargs)

                    with self.observations(f) as trace, patch.object(f.rt.log, "emit", side_effect=fail_model_receipt):
                        list(running)
                    expected = call.record_rejection("team_test") if rejected else call.record_success("team_test")
                    self.assertEqual(len(requests), 1)
                    self.assertEqual(trace.mock_calls, [expected])
                    self.assert_members(f.rt, "unverified" if rejected else "available",
                                        "recent_failure" if rejected else "recent_success")
                    self.assertEqual(f.events("team.model_called"), [])
                    self.assertEqual(f.events("team.tool_started"), [])
                    self.assertEqual(f.events("team.run_finished")[-1].payload["status"], "failed")
                    self.assert_failure_count(f.rt._driver_health, 0)
                    self.assertTrue(f.rt.verify_chain().ok)

    def test_local_driver_work_conflict_does_not_record_connection_failure(self):
        for cls in protocol.CLASSES:
            with self.subTest(protocol=cls.__name__), self.session(cls, []) as (f, driver, requests):
                f.rt._driver_health.record_failure("team_test")
                with self.observations(f) as trace, patch.object(
                        driver, "tool_turn", side_effect=WorkConflict("Synthetic local admission conflict")) as turn:
                    self.run_message(f)
                turn.assert_called_once()
                self.assertEqual(requests, [])
                self.assertEqual(trace.mock_calls, [])
                self.assertEqual(f.events("team.run_finished")[-1].payload["status"], "interrupted")
                self.assert_failure_count(f.rt._driver_health, 1)
                self.assert_members(f.rt, "unverified", "recent_failure")

    def test_native_observation_is_shared_between_runtimes_and_expires(self):
        for cls in protocol.CLASSES:
            with self.subTest(protocol=cls.__name__), self.session(cls, [
                    (200, protocol.response(cls)), (503, {"error": "synthetic unavailable"})]) as (f, driver, requests):
                with Runtime(":memory:", roles_path=f.roles, drivers={"team_test": driver},
                             driver_health=f.rt._driver_health, close_drivers_on_close=False) as other, \
                        patch("cater.runtime.time", wraps=time) as health_clock:
                    # Replace only Runtime's clock reference; HTTP keeps real time.
                    health_clock.monotonic.return_value = 100
                    self.assert_members(other, "unverified", "not_recently_checked")
                    with self.observations(f) as trace:
                        self.run_message(f)
                        self.assert_members(other, "available", "recent_success")
                        health_clock.monotonic.return_value = 101
                        self.run_message(f)
                    self.assertEqual(len(requests), 2)
                    self.assertEqual(trace.mock_calls, [call.record_success("team_test"),
                                                       call.record_rejection("team_test")])
                    self.assert_members(other, "unverified", "recent_failure")
                    self.assert_members(f.rt, "unverified", "recent_failure")
                    health_clock.monotonic.return_value = 402
                    self.assert_members(other, "unverified", "not_recently_checked")
                    self.assert_members(f.rt, "unverified", "not_recently_checked")


if __name__ == "__main__":
    unittest.main()
