"""Local fake transport faults through native Team.run and the task API view."""

import json
from pathlib import Path
import unittest

from cater.drivers import DriverUnavailable
from cater.team import HOST
from cater.web import event_to_dict, task_api_payload
import test_simple_team


TRANSPORT_ISSUES = (
    "dns_lookup_failed", "tls_certificate_error", "tls_error",
    "connection_refused", "connection_reset", "broken_pipe",
    "remote_disconnected", "incomplete_response", "http_protocol_error",
)
PRIVATE = "DO-NOT-PERSIST-TRANSPORT-FAULT"


class TeamTransportIssueTests(unittest.TestCase):
    setUp = test_simple_team.SimpleTeamTests.setUp
    events = test_simple_team.SimpleTeamTests.events

    def run_failure(self, fields, *, expected, prefix=()):
        error = DriverUnavailable(PRIVATE)
        error.__dict__.update(fields)
        error.headers = {"private_header": PRIVATE}
        error.provider_body = PRIVATE
        error.endpoint = PRIVATE
        before_calls = len(self.driver.calls)
        before_events = self.rt.log.read(task_id=self.task)
        self.driver.steps[self.manager] = [*prefix, error,
            test_simple_team.turn("Unexpected follow-up", ("read_messages", {}))]
        message = self.rt.team.receive(self.task, "Local fake transport fault; no external diagnosis",
                                       request_id=f"transport-{before_calls}")
        self.rt.team.run(self.task)
        self.assertEqual(len(self.driver.calls), before_calls + len(prefix) + 1)
        self.assertEqual(len(self.driver.steps[self.manager]), 1)
        events = self.rt.log.read(task_id=self.task)
        fresh = events[len(before_events):]
        starts = [e for e in fresh if e.type == "team.run_started"]
        finishes = [e for e in fresh if e.type == "team.run_finished"]
        self.assertEqual(len(starts), 1)
        self.assertEqual(len(finishes), 1)
        start, finish = starts[0], finishes[0]
        self.assertEqual(start.actor, HOST)
        self.assertEqual(finish.actor, HOST)
        self.assertLess(message.seq, start.seq)
        self.assertLess(start.seq, finish.seq)
        for key, value in {"message_seq": message.seq, "role": self.manager,
                           "run_id": start.payload["run_id"]}.items():
            self.assertEqual(start.payload[key], value)
            self.assertEqual(finish.payload[key], value)
        self.assertEqual(finish.payload["status"], "failed")
        self.assertEqual(finish.payload["failure_stage"], "model_request")
        self.assertEqual(finish.payload["error_type"], "DriverUnavailable")
        diagnostics = {key: finish.payload[key] for key in
                       ("failure_kind", "transport_phase", "transport_issue", "status_code")
                       if key in finish.payload}
        self.assertEqual(diagnostics, expected)
        self.assertEqual(set(finish.payload), {
            "message_seq", "role", "run_id", "status", "reason", "error_type", "failure_stage",
            *expected,
        })
        after_failure = [e for e in fresh if e.seq > finish.seq]
        self.assertFalse([e for e in after_failure if e.type in {
            "team.run_started", "team.model_called", "team.tool_started", "team.tool_finished",
            "driver.retry_scheduled", "driver.retry_finished", "team.message_received",
        }])
        self.assertFalse([e for e in fresh if e.type == "team.objective_completed"])
        self.assertFalse([e for e in fresh if e.type in {
            "driver.retry_scheduled", "driver.retry_finished",
        }])
        if not prefix:
            self.assertFalse([e for e in fresh if e.type in {
                "team.model_called", "team.tool_started", "team.tool_finished", "msg.said",
            }])
        detail = task_api_payload(self.rt, self.task)
        self.assertEqual(detail["events"], [event_to_dict(e) for e in events])
        api_finish, = [e for e in detail["events"] if e["seq"] == finish.seq]
        self.assertEqual(api_finish["payload"], finish.payload)
        run, = [r for r in detail["team"]["runs"] if r["run_id"] == finish.payload["run_id"]]
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["message_seq"], message.seq)
        self.assertNotEqual(detail["team"]["status"], "done")
        self.assertNotIn(PRIVATE, json.dumps(detail))
        self.assertTrue(self.rt.verify_chain().ok)
        # An idle scheduler pass must not consume the queued follow-up turn.
        self.rt.team.run(self.task)
        self.assertEqual(len(self.driver.calls), before_calls + len(prefix) + 1)
        self.assertEqual(self.rt.log.read(task_id=self.task), events)
        self.assertEqual(self.rt.log.read(task_id=self.task)[:len(before_events)], before_events)
        self.assertTrue(self.rt.verify_chain().ok)
        return detail, finish

    def test_all_network_categories_survive_both_phases_and_api_projection(self):
        for issue in TRANSPORT_ISSUES:
            for phase in ("request", "response_read"):
                with self.subTest(issue=issue, phase=phase):
                    fields = {"failure_kind": "network_error", "transport_phase": phase,
                              "transport_issue": issue}
                    self.run_failure(fields, expected=fields)

    def test_legacy_network_failure_has_no_invented_category(self):
        for phase in ("request", "response_read"):
            with self.subTest(phase=phase):
                fields = {"failure_kind": "network_error", "transport_phase": phase}
                self.run_failure(fields, expected=fields)

    def test_unknown_or_malformed_network_category_is_omitted(self):
        for issue in (None, "unknown", PRIVATE, "__proto__", "constructor", "DNS_LOOKUP_FAILED",
                      "dns_lookup_failed ", True, 1, {}, [], ["connection_reset"]):
            with self.subTest(issue=issue):
                fields = {"failure_kind": "network_error", "transport_phase": "request"}
                self.run_failure({**fields, "transport_issue": issue}, expected=fields)

    def test_non_network_failures_cannot_adopt_any_network_category(self):
        for kind in ("timeout", "http_error", "missing_credentials", "invalid_request", PRIVATE):
            for issue in TRANSPORT_ISSUES:
                with self.subTest(kind=kind, issue=issue):
                    fields = {"failure_kind": kind, "transport_phase": "response_read",
                              "transport_issue": issue}
                    expected = {} if kind == PRIVATE else {
                        "failure_kind": kind, "transport_phase": "response_read"}
                    if kind == "http_error":
                        fields["status_code"] = expected["status_code"] = 503
                    self.run_failure(fields, expected=expected)

    def test_unregistered_fields_do_not_invent_a_transport_category(self):
        fields = {"failure_kind": "network_error", "transport_phase": "request"}
        self.run_failure({**fields, "connection_issue": "dns_lookup_failed",
                          "transport_error": "tls_certificate_error",
                          "details": {"transport_issue": "connection_reset"}}, expected=fields)
        self.run_failure({"transport_issue": "dns_lookup_failed", "transport_phase": PRIVATE}, expected={})

    def test_transport_failure_preserves_prior_artifact_without_follow_up_tools(self):
        publish = test_simple_team.turn("Local fixture stage record", ("publish_artifact", {
            "filename": "transport-stage.txt", "content": "Saved before the local fake fault.\n",
            "summary": "Local fixture stage record", "format": "text"}))
        fields = {"failure_kind": "network_error", "transport_phase": "response_read",
                  "transport_issue": "connection_reset"}
        detail, finish = self.run_failure(fields, expected=fields, prefix=(publish,))
        artifact, = self.rt.task(self.task).deliverables
        self.assertEqual(Path(artifact["local_path"]).read_text(encoding="utf-8"), artifact["content"])
        self.assertEqual(detail["task"]["deliverables"][0]["content"], artifact["content"])
        tool_start, = self.events("team.tool_started")
        tool_finish, = self.events("team.tool_finished")
        self.assertLess(tool_start.seq, tool_finish.seq)
        self.assertLess(tool_finish.seq, finish.seq)
        self.assertEqual(tool_finish.payload["result"]["status"], "ok")


if __name__ == "__main__":
    unittest.main()
