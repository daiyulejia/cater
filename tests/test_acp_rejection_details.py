"""Offline rejection diagnostics through real ACP peers and team receipts."""

from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from cater.acp import (
    AcpError, AcpProtocolError, AcpRequestRejected, AcpSession,
    AcpTimeout, AcpUnavailable, failure_code, rejection_details,
)
from cater.runtime import Runtime
from cater.team import HOST
from test_team_external import ScriptedExternalDriver


PEER = Path(__file__).resolve().parent / "fixtures" / "acp_peer.py"
METHODS = ("initialize", "session/new", "session/prompt")
PRIVATE = ("PRIVATE_REJECTION_MESSAGE", "PRIVATE_REJECTION_DATA",
           "PRIVATE_THOUGHT_MUST_NOT_ESCAPE")
VALID_CODES = (("known", -32000), ("method-not-found", -32601),
               ("unknown", 123456789), ("zero", 0),
               ("minimum", -(2**31)), ("maximum", 2**31 - 1))


class IntegerSubclass(int):
    pass


class StringSubclass(str):
    pass


class EqualityTrap:
    def __eq__(self, other):
        raise AssertionError("Diagnostics must not compare untrusted objects")


INVALID_CODES = (None, True, False, -32000.0, float("nan"), float("inf"),
                 -(2**31) - 1, 2**31, 2**1000, -(2**1000), "-32000",
                 [], {}, IntegerSubclass(-32000), EqualityTrap())
INVALID_METHODS = (None, True, 1, [], {}, EqualityTrap(),
                   StringSubclass("initialize"), StringSubclass("session/new"),
                   StringSubclass("session/prompt"), "", " initialize", "session/prompt ",
                   "session/cancel", "session/request_permission", "remote/private")


class AcpRejectionDetailsTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.counter = 0

    def directory(self):
        self.counter += 1
        root = self.root / str(self.counter)
        root.mkdir()
        return root

    def assert_private_absent(self, value):
        for secret in PRIVATE:
            self.assertNotIn(secret, value)

    def assert_classification(self, error, *, uncertain):
        self.assertIsInstance(error, AcpUnavailable)
        self.assertEqual(failure_code(error), "request_rejected")
        self.assertIs(error.prompt_may_have_run, uncertain)

    def test_legacy_constructor_and_missing_attributes(self):
        error = AcpRequestRejected("Legacy rejection")
        self.assertEqual(error.args, ("Legacy rejection",))
        self.assertEqual(str(error), "Legacy rejection")
        self.assertIsNone(error.rpc_code)
        self.assertIsNone(error.rpc_method)
        self.assertEqual(rejection_details(error), {})
        self.assert_classification(error, uncertain=False)
        error = AcpRequestRejected("Legacy rejection", prompt_may_have_run=True)
        del error.rpc_code
        del error.rpc_method
        self.assertEqual(rejection_details(error), {})
        self.assert_classification(error, uncertain=True)
        with self.assertRaises(TypeError):
            AcpRequestRejected("Keyword-only contract", True, -32000, "initialize")

    def test_constructor_accepts_only_exact_safe_types_and_signed32_codes(self):
        for _, code in VALID_CODES:
            for method in METHODS:
                with self.subTest(code=code, method=method):
                    error = AcpRequestRejected("Rejected", prompt_may_have_run=True,
                                               rpc_code=code, rpc_method=method)
                    self.assertIs(type(error.rpc_code), int)
                    self.assertIs(type(error.rpc_method), str)
                    self.assertEqual(rejection_details(error), {
                        "external_rpc_code": code, "external_rpc_method": method})
                    self.assert_classification(error, uncertain=True)
        for code in INVALID_CODES:
            with self.subTest(invalid_code=repr(code)):
                error = AcpRequestRejected("Rejected", rpc_code=code, rpc_method="initialize")
                self.assertIsNone(error.rpc_code)
                self.assertEqual(rejection_details(error), {"external_rpc_method": "initialize"})
                self.assert_classification(error, uncertain=False)
        for method in INVALID_METHODS:
            with self.subTest(invalid_method=repr(method)):
                error = AcpRequestRejected("Rejected", rpc_code=0, rpc_method=method)
                self.assertIsNone(error.rpc_method)
                self.assertEqual(rejection_details(error), {"external_rpc_code": 0})
                self.assert_classification(error, uncertain=False)

    def test_helper_revalidates_mutated_attributes_independently(self):
        error = AcpRequestRejected("Rejected", prompt_may_have_run=True,
                                   rpc_code=-32000, rpc_method="session/prompt")
        for code in INVALID_CODES:
            with self.subTest(mutated_code=repr(code)):
                error.rpc_code = code
                self.assertEqual(rejection_details(error), {"external_rpc_method": "session/prompt"})
                self.assert_classification(error, uncertain=True)
        error.rpc_code = -32601
        for method in INVALID_METHODS:
            with self.subTest(mutated_method=repr(method)):
                error.rpc_method = method
                self.assertEqual(rejection_details(error), {"external_rpc_code": -32601})
                self.assert_classification(error, uncertain=True)
        error.rpc_method = "session/new"
        details = rejection_details(error)
        details["external_rpc_code"] = 99
        self.assertEqual(rejection_details(error), {
            "external_rpc_code": -32601, "external_rpc_method": "session/new"})
        del error.rpc_code
        self.assertEqual(rejection_details(error), {"external_rpc_method": "session/new"})
        del error.rpc_method
        self.assertEqual(rejection_details(error), {})

    def test_helper_rejects_lookalike_exceptions_without_reading_properties(self):
        for kind, expected in ((RuntimeError, "external_error"), (AcpError, "external_error"),
                               (AcpUnavailable, "transport_unavailable"),
                               (AcpProtocolError, "protocol_error"), (AcpTimeout, "deadline_exceeded")):
            with self.subTest(kind=kind.__name__):
                error = kind(PRIVATE[0])
                error.rpc_code, error.rpc_method = -32000, "session/prompt"
                self.assertEqual(rejection_details(error), {})
                self.assertEqual(failure_code(error), expected)

        class FakeRejection(RuntimeError):
            @property
            def rpc_code(self):
                raise AssertionError("A non-rejection's attributes must not be inspected")

            rpc_method = rpc_code

        self.assertEqual(rejection_details(FakeRejection()), {})
        self.assertEqual(rejection_details(None), {})

    def test_raising_diagnostic_attributes_cannot_replace_original_rejection(self):
        class RaisingRejection(AcpRequestRejected):
            def __getattribute__(self, name):
                if name in object.__getattribute__(self, "broken_fields"):
                    raise ValueError(PRIVATE[1])
                return super().__getattribute__(name)

        for broken, expected in (
                (("rpc_code",), {"external_rpc_method": "session/prompt"}),
                (("rpc_method",), {"external_rpc_code": -32000}),
                (("rpc_code", "rpc_method"), {})):
            with self.subTest(broken=broken):
                error = RaisingRejection(PRIVATE[0], prompt_may_have_run=True,
                                         rpc_code=-32000, rpc_method="session/prompt")
                error.broken_fields = broken
                self.assertEqual(rejection_details(error), expected)
                self.assert_classification(error, uncertain=True)
                self.runtime_failure(error, expected)

    def peer_error(self, method, case, expected_type):
        root = self.directory()
        client = AcpSession([sys.executable, "-u", str(PEER),
                             f"rejection-details:{method}:{case}", str(root)], cwd=root, timeout=5)
        self.addCleanup(client.close)
        updates = []
        try:
            with self.assertRaises(expected_type) as caught:
                client.open(on_update=updates.append)
                if method != "session/prompt":
                    self.fail("The peer must reject before any prompt")
                client.prompt("One offline attempt", on_update=updates.append)
            error = caught.exception
            self.assertIs(type(error), expected_type)
            self.assertIs(error.prompt_may_have_run, method == "session/prompt")
            self.assertEqual(updates, [])
            self.assert_private_absent(repr(error) + str(vars(error)) + repr(rejection_details(error)))
            self.assertEqual(error.args, ("ACP server rejected the request",)
                             if expected_type is AcpRequestRejected else ("Invalid ACP error response",))
            self.assertFalse(client.cancel())
            with self.assertRaises(AcpUnavailable):
                client.open()
            with self.assertRaises(AcpUnavailable):
                client.prompt("Must never replay")
        finally:
            client.close()
        rows = [json.loads(line) for line in (root / "wire.jsonl").read_text(encoding="utf-8").splitlines()]
        requests = [row for row in rows if row.get("jsonrpc") == "2.0" and "method" in row]
        self.assertEqual([row["method"] for row in requests], list(METHODS[:METHODS.index(method) + 1]))
        self.assertEqual(len({row["id"] for row in requests}), len(requests))
        self.assertEqual(sum(row.get("event") == "started" for row in rows), 1)
        response, = [row["response"] for row in rows if row.get("event") == "rejection-sent"]
        self.assertEqual(response["id"], requests[-1]["id"])
        if case in {name for name, _ in VALID_CODES} or case in {
                "positive-overflow", "negative-overflow", "huge"}:
            remote = response["error"]
            self.assertNotEqual(remote["rpc_method"], method)
            for secret in PRIVATE:
                self.assertIn(secret, json.dumps(remote))
        return error

    def test_real_peer_retains_host_method_and_known_unknown_boundary_codes(self):
        for method in METHODS:
            for case, code in VALID_CODES:
                with self.subTest(method=method, case=case):
                    error = self.peer_error(method, case, AcpRequestRejected)
                    self.assertEqual(rejection_details(error), {
                        "external_rpc_code": code, "external_rpc_method": method})
                    self.assert_classification(error, uncertain=method == "session/prompt")

    def test_real_peer_oversized_integers_drop_diagnostics_without_reclassifying(self):
        for method in METHODS:
            for case in ("positive-overflow", "negative-overflow", "huge"):
                with self.subTest(method=method, case=case):
                    error = self.peer_error(method, case, AcpRequestRejected)
                    self.assertIsNone(error.rpc_code)
                    self.assertEqual(rejection_details(error), {"external_rpc_method": method})
                    self.assert_classification(error, uncertain=method == "session/prompt")

    def test_real_peer_malformed_errors_still_have_protocol_classification(self):
        for method in METHODS:
            for case in ("true", "false", "float", "string", "missing-code",
                         "missing-message", "non-string-message", "null-error"):
                with self.subTest(method=method, case=case):
                    error = self.peer_error(method, case, AcpProtocolError)
                    self.assertEqual(failure_code(error), "protocol_error")
                    self.assertEqual(rejection_details(error), {})

    def runtime_failure(self, error, expected_details, *, method="session/prompt", cleanup=True):
        root = self.directory()
        config = json.loads((Path(__file__).resolve().parents[1]
                             / "cater/packs/default_roles.json").read_text(encoding="utf-8"))
        driver = ScriptedExternalDriver()
        config["organization"]["leader"]["driver"] = driver.name
        for role in config["roles"]:
            role["driver"] = driver.name
        roles = root / "roles.json"
        roles.write_text(json.dumps(config), encoding="utf-8")
        rt = Runtime(root / "team.db", roles_path=roles, drivers={driver.name: driver})
        self.addCleanup(rt.close)
        task = rt.create_task("Offline rejection receipt regression")
        rt.team.enable(task)
        attempted = method == "session/prompt"
        phase = "prompting" if attempted else "opening"
        observations = {"failure_ms": 30, "tool_calls": int(attempted)}

        def fail_once(run):
            if attempted:
                receipt = run.call("publish_artifact", filename="retained.txt", content="Retained once\n",
                                   summary="Offline fixture", format="text")
                self.assertEqual(receipt["status"], "ok", receipt)
            run.state = replace(run.state, prompt_attempted=attempted, failure_phase=phase,
                                observations=tuple(observations.items()),
                                session_id="offline-rejection" if attempted else None)
            run.cleanup_complete = cleanup
            raise error

        driver.scripts = {rt.organization.leader.id: [fail_once]}
        # The scripted driver must exercise the host branch without starting any CLI.
        with patch("subprocess.Popen", side_effect=AssertionError("Unexpected external process")) as process:
            rt.team.receive(task, "Fail exactly once")
            rt.collaborate(task)
            snapshot = rt.log.read(task_id=task)
            rt.collaborate(task)
            self.assertEqual(rt.log.read(task_id=task), snapshot)
            process.assert_not_called()
        self.assertEqual(driver.errors, [])
        self.assertEqual(driver.scripts[rt.organization.leader.id], [])
        self.assertEqual(len(driver.runs), 1)
        events = rt.log.read(task_id=task)

        def of_type(kind):
            return [event for event in events if event.type == kind]

        started, = of_type("team.run_started")
        external_start, = of_type("team.external_run_started")
        external_finish, = of_type("team.external_run_finished")
        finished, = of_type("team.run_finished")
        self.assertEqual(external_start.payload["run_started_seq"], started.seq)
        self.assertEqual(external_start.payload["run_started_hash"], started.hash)
        self.assertEqual(external_finish.payload["started_seq"], external_start.seq)
        self.assertEqual(external_finish.payload["started_hash"], external_start.hash)
        self.assertLess(started.seq, external_start.seq)
        self.assertLess(external_start.seq, external_finish.seq)
        self.assertLess(external_finish.seq, finished.seq)
        for event in (external_start, external_finish, finished):
            self.assertEqual(event.actor, HOST)
            for key in ("role", "run_id", "message_seq"):
                self.assertEqual(event.payload[key], started.payload[key])
        self.assertEqual(external_finish.payload["outcome"], "unknown" if attempted else "not_started")
        self.assertEqual(external_finish.payload["result"], "failed")
        self.assertIs(external_finish.payload["prompt_attempted"], attempted)
        self.assertIs(external_finish.payload["cleanup_complete"], cleanup)
        self.assertEqual(len(rt.team.external_runs.pending(task)), int(not cleanup))
        self.assertEqual(finished.payload["failure_stage"], phase)
        self.assertEqual(finished.payload["external_observations"], observations)
        if cleanup:
            self.assertEqual(finished.payload["status"], "failed")
            self.assertEqual(finished.payload["external_failure_code"], failure_code(error))
            self.assertEqual(finished.payload["error_type"], type(error).__name__)
            self.assertEqual(set(finished.payload), {
                "message_seq", "role", "run_id", "status", "reason", "error_type",
                "external_failure_code", "external_observations", "failure_stage", *expected_details})
        else:
            self.assertEqual(finished.payload["status"], "interrupted")
        self.assertEqual({key: value for key, value in finished.payload.items()
                          if key.startswith("external_rpc_")}, expected_details)
        calls = of_type("team.model_called")
        self.assertEqual(len(calls), int(attempted))
        if calls:
            self.assertEqual(calls[0].payload["external_observations"], observations)
        tools, receipts = of_type("team.tool_started"), of_type("team.tool_finished")
        self.assertEqual(len(tools), int(attempted))
        self.assertEqual(len(receipts), len(tools))
        self.assertEqual(len(driver.runs[0].receipts), len(tools))
        if attempted:
            tool, receipt = tools[0], receipts[0]
            self.assertEqual(tool.actor, HOST)
            self.assertEqual(receipt.actor, HOST)
            self.assertEqual(tool.payload["name"], "publish_artifact")
            self.assertEqual(tool.payload["external_started_seq"], external_start.seq)
            self.assertEqual(receipt.payload["started_seq"], tool.seq)
            self.assertEqual(receipt.payload["result"], driver.runs[0].receipts[0][1])
            self.assertLess(tool.seq, receipt.seq)
            self.assertLess(receipt.seq, external_finish.seq)
            artifact, = rt.task(task).deliverables
            self.assertEqual(Path(artifact["local_path"]).read_text(encoding="utf-8"), "Retained once\n")
        for kind in ("team.review_requested", "team.review_recorded", "team.objective_completed"):
            self.assertEqual(of_type(kind), [])
        self.assertEqual(rt.team_reviews.view(task), [])
        self.assertNotEqual(rt.team.view(task)["status"], "done")
        self.assert_private_absent(repr(events))
        self.assertTrue(rt.verify_chain().ok)
        return finished

    def test_runtime_persists_safe_details_from_each_real_peer_stage(self):
        for method in METHODS:
            for case, code in (("known", -32000), ("unknown", 123456789), ("huge", None)):
                with self.subTest(method=method, case=case):
                    error = self.peer_error(method, case, AcpRequestRejected)
                    expected = {"external_rpc_method": method}
                    if code is not None:
                        expected["external_rpc_code"] = code
                    self.runtime_failure(error, expected, method=method)
                    self.assert_classification(error, uncertain=method == "session/prompt")

    def test_runtime_malformed_peer_errors_have_no_rejection_diagnostics(self):
        for method in METHODS:
            with self.subTest(method=method):
                error = self.peer_error(method, "true", AcpProtocolError)
                self.runtime_failure(error, {}, method=method)

    def test_runtime_filters_legacy_mutated_and_lookalike_exceptions(self):
        legacy = AcpRequestRejected(PRIVATE[0], prompt_may_have_run=True)
        missing = AcpRequestRejected(PRIVATE[0], prompt_may_have_run=True)
        del missing.rpc_code
        del missing.rpc_method
        mutated = AcpRequestRejected(PRIVATE[0], prompt_may_have_run=True,
                                     rpc_code=-32000, rpc_method="session/prompt")
        mutated.rpc_code, mutated.rpc_method = True, StringSubclass("session/prompt")
        code_only = AcpRequestRejected(PRIVATE[0], prompt_may_have_run=True)
        code_only.rpc_code, code_only.rpc_method = 0, {"secret": PRIVATE[1]}
        method_only = AcpRequestRejected(PRIVATE[0], prompt_may_have_run=True)
        method_only.rpc_code, method_only.rpc_method = 2**1000, "session/prompt"
        fake = RuntimeError(PRIVATE[0])
        fake.rpc_code, fake.rpc_method = -32000, "session/prompt"
        timeout = AcpTimeout(PRIVATE[0], prompt_may_have_run=True)
        timeout.rpc_code, timeout.rpc_method = -32000, "session/prompt"
        for name, error, expected in (
                ("legacy", legacy, {}), ("missing", missing, {}), ("mutated", mutated, {}),
                ("code-only", code_only, {"external_rpc_code": 0}),
                ("method-only", method_only, {"external_rpc_method": "session/prompt"}),
                ("fake", fake, {}), ("timeout", timeout, {})):
            with self.subTest(name=name):
                error.data = {"secret": PRIVATE[1], "thought": PRIVATE[2]}
                error.external_rpc_code = "UNTRUSTED_DIRECT_FIELD"
                self.runtime_failure(error, expected)

    def test_runtime_rejection_cannot_confirm_failed_cleanup(self):
        error = AcpRequestRejected(PRIVATE[0], prompt_may_have_run=True,
                                   rpc_code=-32000, rpc_method="session/prompt")
        self.runtime_failure(error, {"external_rpc_code": -32000,
                                    "external_rpc_method": "session/prompt"}, cleanup=False)
        self.assert_classification(error, uncertain=True)

    def test_cleanup_failure_keeps_primary_code_without_claiming_execution_stopped(self):
        for kind, code in ((AcpProtocolError, "protocol_error"), (AcpTimeout, "deadline_exceeded"),
                           (RuntimeError, "external_error")):
            with self.subTest(kind=kind.__name__):
                error = kind(PRIVATE[0])
                last = self.runtime_failure(error, {}, cleanup=False)
                self.assertEqual(last.payload["external_failure_code"], code)
                self.assertEqual(last.payload["error_type"], kind.__name__)
                self.assertEqual(last.payload["status"], "interrupted")


if __name__ == "__main__":
    unittest.main()
