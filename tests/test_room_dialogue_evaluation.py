from __future__ import annotations

import copy
import io
import json
import os
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from cater.drivers import (CliDriver, DriverProtocolError, DriverResponse, DriverUnavailable,
                           GuidedDriver, ScriptedDriver)
from cater.room_dialogue import build_request
from scripts import evaluate_room_dialogue as evaluation


def decision_for(case, *, action=None, task_id=None, reply="理解了，这里只提供建议。"):
    action = action or case["expected"]["actions"][0]
    target = task_id if task_id is not None else case["expected"]["task_id"]
    return {"reply": reply, "action": action, "task_id": target,
            "goal": "整理独立交付的活动文案" if action == "new_task" else None,
            "acceptance": [] if action == "new_task" else None,
            "quote": case["message"] if action != "discuss" else None}


def response_for(case, *, tokens=7, meta=None, **changes):
    return DriverResponse(json.dumps(decision_for(case, **changes), ensure_ascii=False), tokens,
                          {"token_usage": "reported"} if meta is None else meta)


class FakeDriver:
    """No transport, filesystem, runtime or task access."""

    def __init__(self, outcomes, budget=10000):
        self.name = "synthetic"
        self.token_budget = budget
        self.outcomes = list(outcomes)
        self.calls = []

    def complete(self, *, role, prompt, context):
        self.calls.append({"role": role, "prompt": prompt, "context": context})
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class RoomDialogueEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.cases = json.loads(evaluation.FIXTURES.read_text(encoding="utf-8"))

    def driver(self, cases=None, **kwargs):
        return FakeDriver([response_for(case) for case in (self.cases if cases is None else cases)], **kwargs)

    def test_semantic_routing_fixtures_keep_mixed_message_regressions(self):
        cases = json.loads(evaluation.FIXTURES.with_name("conversation_routing_cases.json").read_text(encoding="utf-8"))
        self.assertEqual(len(evaluation.validate_cases(cases)), 8)
        expectations = {case["id"]: case["expected"]["actions"] for case in cases}
        self.assertEqual(expectations["mixed_reverse"], ["update_task"])
        self.assertEqual(expectations["status_negated_change"], ["status_task"])

    def run_cli(self, driver, directory, *extra):
        output = Path(directory) / "report.json"
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(evaluation, "load_drivers", return_value={"synthetic": driver}) as load:
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = evaluation.main(["--config", str(Path(directory) / "config.json"),
                                        "--driver", "synthetic", "--output", str(output), *extra])
        report = json.loads(output.read_text(encoding="utf-8")) if output.exists() else None
        return code, report, stdout.getvalue() + stderr.getvalue(), load

    def test_all_ten_fixtures_and_each_expected_action_can_match(self):
        self.assertEqual(len(self.cases), 10)
        report = evaluation.evaluate(self.driver(), self.cases, limit=10)
        self.assertTrue(report["ok"])
        self.assertEqual(report["summary"]["matched"], 10)
        self.assertEqual(report["summary"]["reported_tokens"], 70)
        self.assertEqual({row["decision"]["action"] for row in report["cases"]},
                         {"discuss", "new_task", "update_task", "explain_task", "resume_task"})

    def test_default_limit_is_three_in_cli_and_evaluation(self):
        driver = self.driver()
        report = evaluation.evaluate(driver, self.cases)
        self.assertEqual(len(driver.calls), 3)
        self.assertTrue(report["ok"])
        self.assertEqual(report["summary"]["not_run"], 7)
        self.assertEqual([case["expected"]["actions"] for case in self.cases[:3]],
                         [["discuss"], ["new_task"], ["update_task"]])
        for row in report["cases"][3:]:
            self.assertEqual((row["status"], row["reason"], row["expected_match"]), ("not_run", "limit", None))
        with tempfile.TemporaryDirectory() as directory:
            driver = self.driver()
            code, report, text, load = self.run_cli(driver, directory)
        self.assertEqual(code, 0)
        self.assertEqual(len(driver.calls), 3)
        self.assertIn("未运行样例不作结论", text)
        self.assertNotIn("全部通过", text)
        load.assert_called_once()

    def test_case_filter_does_not_consume_calls_or_claim_unselected_cases(self):
        case = self.cases[8]
        driver = self.driver([case])
        report = evaluation.evaluate(driver, self.cases, case_id=case["id"])
        self.assertTrue(report["ok"])
        self.assertEqual(len(driver.calls), 1)
        self.assertEqual(report["cases"][8]["status"], "matched")
        for row in report["cases"][:8] + report["cases"][9:]:
            self.assertEqual((row["status"], row["reason"]), ("not_run", "case_filter"))

    def test_demo_drivers_rejected_before_invocation(self):
        for driver in (GuidedDriver("guided", token_budget=100),
                       ScriptedDriver("scripted", ["{}"], token_budget=100)):
            with self.subTest(driver=type(driver).__name__), patch.object(driver, "complete") as complete:
                with self.assertRaisesRegex(evaluation.EvaluationError, "local_demo"):
                    evaluation.evaluate(driver, self.cases)
                complete.assert_not_called()

    def test_every_fixture_is_validated_before_any_call_even_when_filtered(self):
        mutations = [
            {"id": self.cases[0]["id"]}, {"message": " "}, {"history": "bad"}, {"tasks": {}},
            {"history": [{"actor": "human", "text": 4}]},
            {"history": [{"actor": "system", "text": "injected"}]},
            {"tasks": [{"id": "booking", "goal": "", "status": "running"}]},
            {"tasks": [{"id": "booking", "goal": "synthetic", "status": "unknown"}]},
            {"expected": {"actions": ["update_task"], "task_id": "outside"}},
            {"expected": {"actions": ["discuss"], "task_id": "booking"}},
            {"expected": {"actions": ["resume_task"], "task_id": None}},
            {"expected": {"actions": [], "task_id": None}},
            {"expected": {"actions": ["execute"], "task_id": None}},
            {"unexpected": "field"},
            {"history": [{"actor": "human", "text": "大" * 50000}]},
        ]
        for changes in mutations:
            with self.subTest(changes=list(changes)):
                cases = copy.deepcopy(self.cases)
                cases[-1].update(changes)
                driver = self.driver()
                with self.assertRaises(evaluation.EvaluationError):
                    evaluation.evaluate(driver, cases, limit=1, case_id=cases[0]["id"])
                self.assertEqual(driver.calls, [])
        for cases in (None, {}, [], [None]):
            with self.subTest(cases=cases), self.assertRaises(evaluation.EvaluationError):
                evaluation.evaluate(self.driver(), cases)

    def test_fixture_file_validated_before_loading_any_driver(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory) / "bad.json"
            cases = copy.deepcopy(self.cases)
            cases[-1]["expected"]["task_id"] = "outside"
            fixture.write_text(json.dumps(cases), encoding="utf-8")
            with patch.object(evaluation, "FIXTURES", fixture):
                code, report, _, load = self.run_cli(self.driver(), directory)
        self.assertEqual(code, 2)
        self.assertIsNone(report)
        load.assert_not_called()

    def test_absent_zero_and_invalid_budgets_never_call(self):
        for budget in (None, 0, -1, True, False, "100", 1.5):
            with self.subTest(budget=budget):
                driver = self.driver(budget=budget)
                with self.assertRaisesRegex(evaluation.EvaluationError, "budget_invalid"):
                    evaluation.evaluate(driver, self.cases)
                self.assertEqual(driver.calls, [])
        driver = self.driver()
        del driver.token_budget
        with self.assertRaises(evaluation.EvaluationError):
            evaluation.evaluate(driver, self.cases)
        self.assertEqual(driver.calls, [])

    def test_budget_stops_after_reported_usage_including_invalid_output(self):
        driver = FakeDriver([DriverResponse("invalid JSON", 10, {"token_usage": "reported"})], budget=10)
        report = evaluation.evaluate(driver, self.cases)
        self.assertFalse(report["ok"])
        self.assertEqual(report["summary"]["reported_tokens"], 10)
        self.assertEqual(len(driver.calls), 1)
        self.assertEqual(report["cases"][0]["status"], "parse_error")
        self.assertEqual(report["cases"][1]["reason"], "budget_exhausted")

    def test_unknown_and_estimated_usage_are_not_zero_and_stop_further_calls(self):
        for tokens, meta in ((0, {}), (None, {"token_usage": "reported"}),
                             (-1, {"token_usage": "reported"}), (True, {"token_usage": "reported"}),
                             (3.5, {"token_usage": "reported"}), (7, {"token_usage": "estimated"})):
            with self.subTest(tokens=tokens, meta=meta):
                driver = FakeDriver([response_for(self.cases[0], tokens=tokens, meta=meta)])
                report = evaluation.evaluate(driver, self.cases)
                self.assertFalse(report["ok"])
                self.assertIsNone(report["summary"]["reported_tokens"])
                self.assertFalse(report["summary"]["usage_complete"])
                self.assertEqual(report["summary"]["unknown_usage_calls"], 1)
                self.assertIsNone(report["cases"][0]["reported_tokens"])
                self.assertEqual(report["cases"][1]["reason"], "usage_unknown")
                self.assertEqual(len(driver.calls), 1)

    def test_partial_usage_aggregate_is_explicit_and_reported_zero_is_valid(self):
        driver = FakeDriver([response_for(self.cases[0], tokens=0),
                             response_for(self.cases[1], tokens=0, meta={})])
        report = evaluation.evaluate(driver, self.cases)
        self.assertEqual(report["summary"]["reported_tokens"], 0)
        self.assertEqual(report["summary"]["reported_calls"], 1)
        self.assertEqual(report["summary"]["unknown_usage_calls"], 1)
        self.assertEqual(len(driver.calls), 2)
        self.assertIsNone(report["cases"][1]["reported_tokens"])

    def test_parse_schema_and_mismatch_are_distinct_and_continue_without_retry(self):
        driver = FakeDriver([
            DriverResponse("not json", 3, {"token_usage": "reported"}),
            DriverResponse('{"action":"discuss"}', 4, {"token_usage": "reported"}),
            DriverResponse(json.dumps(decision_for(self.cases[0])), 5, {"token_usage": "reported"}),
            response_for(self.cases[3], tokens=6),
        ])
        report = evaluation.evaluate(driver, self.cases, limit=4)
        self.assertEqual([r["status"] for r in report["cases"][:4]],
                         ["parse_error", "schema_error", "mismatch", "matched"])
        self.assertEqual(len(driver.calls), 4)
        self.assertEqual(report["summary"]["reported_tokens"], 18)
        self.assertEqual(report["summary"]["schema_valid"], 2)
        self.assertFalse(report["ok"])
        self.assertGreaterEqual(report["summary"]["wall_ms"], 0)
        self.assertGreaterEqual(report["summary"]["call_wall_ms"], 0)

    def test_correct_action_wrong_candidate_is_mismatch_outside_candidate_is_schema_failure(self):
        case = self.cases[7]
        for target, status in (("booking", "mismatch"), ("outside", "schema_error")):
            with self.subTest(target=target):
                report = evaluation.evaluate(FakeDriver([response_for(case, task_id=target)]), [case])
                self.assertEqual(report["cases"][0]["status"], status)
                self.assertFalse(report["ok"])

    def test_contained_response_counts_usage_but_never_validates_a_decision(self):
        contained = response_for(self.cases[0], tokens=11,
                                 meta={"contained": True, "invocation_started": True, "token_usage": "reported",
                                       "stop_reason": "max_tokens", "usage": {"input_tokens": 4, "output_tokens": 7}})
        driver = FakeDriver([contained, response_for(self.cases[1])])
        with patch.object(evaluation, "validate_response", wraps=evaluation.validate_response) as validate:
            report = evaluation.evaluate(driver, self.cases, limit=2)
        self.assertEqual(validate.call_count, 1)
        self.assertEqual(report["cases"][0]["status"], "provider_incomplete")
        self.assertIsNone(report["cases"][0]["schema_valid"])
        self.assertIsNone(report["cases"][0]["expected_match"])
        self.assertNotIn("decision", report["cases"][0])
        self.assertEqual(report["cases"][0]["provider_usage"], {"input_tokens": 4, "output_tokens": 7})
        self.assertEqual(report["cases"][0]["stop_reason"], "max_tokens")
        self.assertTrue(report["cases"][0]["invocation_started"])
        self.assertEqual(report["summary"]["reported_tokens"], 18)
        self.assertFalse(report["ok"])

    def test_diagnostic_usage_fields_and_stop_reason_are_allowlisted(self):
        usage = {"input_tokens": 1, "output_tokens": 2, "cache_creation_input_tokens": 3,
                 "cache_read_input_tokens": 0, "prompt_tokens": True, "completion_tokens": -1,
                 "total_tokens": "private", "private": "sk-private"}
        driver = FakeDriver([response_for(self.cases[0], tokens=6,
                            meta={"usage": usage, "token_usage": "reported", "stop_reason": "https://private.invalid",
                                  "invocation_started": "sk-private"})])
        report = evaluation.evaluate(driver, self.cases, limit=1)
        row = report["cases"][0]
        self.assertEqual(row["provider_usage"], {"input_tokens": 1, "output_tokens": 2,
                                              "cache_creation_input_tokens": 3, "cache_read_input_tokens": 0})
        self.assertEqual(row["stop_reason"], "unknown")
        self.assertIsNone(row["invocation_started"])
        self.assertNotIn("private", json.dumps(report))

    def test_provider_exceptions_stop_all_remaining_calls_and_are_sanitized(self):
        secret = "sk-synthetic-private https://private.invalid/api Authorization: Bearer private"
        exceptions = [DriverUnavailable(secret), DriverProtocolError(secret), TimeoutError(secret),
                      RuntimeError(secret), type("SecretEndpointName", (Exception,), {})(secret)]
        for error in exceptions:
            with self.subTest(error=type(error).__name__):
                driver = FakeDriver([error])
                report = evaluation.evaluate(driver, self.cases, limit=10)
                self.assertEqual(len(driver.calls), 1)
                self.assertEqual(report["cases"][0]["status"], "provider_error")
                self.assertEqual(report["summary"]["not_run"], 9)
                self.assertTrue(all(row["reason"] == "provider_error" for row in report["cases"][1:]))
                self.assertFalse(report["ok"])
                self.assertIsNone(report["summary"]["reported_tokens"])
                self.assertNotIn("private", json.dumps(report))
                self.assertNotIn("SecretEndpointName", json.dumps(report))

    def test_real_cli_driver_internal_restart_is_disabled(self):
        driver = CliDriver("synthetic", ["unused"], restart_limit=4, token_budget=100)
        with patch.object(CliDriver, "_send_once", side_effect=DriverUnavailable("private")) as send:
            with patch.object(CliDriver, "_restart"):
                report = evaluation.evaluate(driver, self.cases)
        send.assert_called_once()
        self.assertEqual(report["summary"]["provider_error"], 1)

    def test_requests_are_build_request_envelopes_with_synthetic_data_only_and_no_task_writes(self):
        original = copy.deepcopy(self.cases)
        driver = self.driver()
        with patch("cater.runtime.Runtime", side_effect=AssertionError("runtime access")):
            with patch("sqlite3.connect", side_effect=AssertionError("database access")):
                with patch.object(Path, "read_text", side_effect=AssertionError("workspace read")):
                    with patch.object(Path, "write_text", side_effect=AssertionError("task write")):
                        report = evaluation.evaluate(driver, self.cases, limit=10)
        self.assertTrue(report["ok"])
        self.assertEqual(original, self.cases)
        for request, case in zip(driver.calls, self.cases):
            prompt, context = build_request(message=case["message"], history=case["history"], tasks=case["tasks"])
            self.assertEqual(request, {"role": "store_manager", "prompt": prompt, "context": context})
            self.assertTrue(request["context"]["conversation"]["read_only"])
            self.assertNotIn("task_id", request["context"])
            self.assertNotIn(str(evaluation.ROOT), prompt)
        driver.calls[0]["context"]["conversation"]["tasks"].append({"id": "mutation"})
        self.assertEqual(original, self.cases)

    def test_reports_and_cli_omit_provider_configuration_response_prose_and_exception_text(self):
        secret = "sk-test-private"
        driver = FakeDriver([response_for(self.cases[0], reply=secret,
                                         meta={"token_usage": "reported", "endpoint": "https://private.invalid", "key": secret}),
                             DriverUnavailable(secret + " https://private.invalid")])
        driver.name = secret
        driver.endpoint = "https://private.invalid"
        with tempfile.TemporaryDirectory() as directory:
            code, report, text, _ = self.run_cli(driver, directory)
        self.assertEqual(code, 1)
        self.assertNotIn(secret, json.dumps(report) + text)
        self.assertNotIn("private.invalid", json.dumps(report) + text)
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(evaluation, "load_drivers", side_effect=ValueError(secret + driver.endpoint)):
                stdout, stderr = io.StringIO(), io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    code = evaluation.main(["--config", "unused", "--driver", "unused",
                                            "--output", str(Path(directory) / "error.json")])
        self.assertEqual(code, 2)
        self.assertNotIn(secret, stdout.getvalue() + stderr.getvalue())
        self.assertNotIn(driver.endpoint, stderr.getvalue())

    def test_failures_never_return_cli_success(self):
        for response in (DriverResponse("bad", 1, {"token_usage": "reported"}),
                         DriverResponse("{}", 1, {"token_usage": "reported"}),
                         response_for(self.cases[2]), DriverUnavailable("safe synthetic failure")):
            with self.subTest(response=type(response).__name__), tempfile.TemporaryDirectory() as directory:
                code, report, _, _ = self.run_cli(FakeDriver([response]), directory, "--limit", "1")
                self.assertEqual(code, 1)
                self.assertFalse(report["ok"])

    def test_invalid_options_and_output_locations_fail_before_calls(self):
        for options in (("--limit", "0"), ("--limit", "-1"), ("--case", "absent")):
            with self.subTest(options=options), tempfile.TemporaryDirectory() as directory:
                driver = self.driver()
                code, report, _, load = self.run_cli(driver, directory, *options)
                self.assertEqual(code, 2)
                self.assertIsNone(report)
                self.assertEqual(driver.calls, [])
                load.assert_not_called()
        for path in (evaluation.ROOT / "tracked-report.json", evaluation.REPORT_DIR / ".." / "report.json",
                     evaluation.REPORT_DIR / "report.txt"):
            with self.subTest(path=path), self.assertRaises(evaluation.EvaluationError):
                evaluation.output_path(str(path))
        for limit in (True, 0, -1):
            with self.assertRaises(evaluation.EvaluationError):
                evaluation.evaluate(self.driver(), self.cases, limit=limit)

    def test_default_report_directory_is_ignored_and_existing_outputs_are_not_overwritten(self):
        output = evaluation.output_path(None)
        self.assertEqual(output.parent, evaluation.ROOT / ".cater" / "evaluations")
        self.assertIn(".cater/", (evaluation.ROOT / ".gitignore").read_text(encoding="utf-8").splitlines())
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "report.json"
            target.write_text("existing", encoding="utf-8")
            driver = self.driver()
            code, _, _, _ = self.run_cli_without_read(driver, directory)
            self.assertEqual(code, 2)
            self.assertEqual(target.read_text(encoding="utf-8"), "existing")
            self.assertEqual(driver.calls, [])

    def run_cli_without_read(self, driver, directory):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(evaluation, "load_drivers", return_value={"synthetic": driver}) as load:
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = evaluation.main(["--config", "unused", "--driver", "synthetic",
                                        "--output", str(Path(directory) / "report.json")])
        return code, None, stdout.getvalue() + stderr.getvalue(), load

    def test_local_http_openai_driver_request_usage_and_transport_failure(self):
        requests = []
        case = self.cases[0]

        class Handler(BaseHTTPRequestHandler):
            status = 200

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append({"body": body, "authorized": self.headers.get("Authorization") == "Bearer synthetic-key"})
                payload = {"choices": [{"message": {"content": json.dumps(decision_for(case), ensure_ascii=False)}}],
                           "usage": {"prompt_tokens": 9, "completion_tokens": 4, "total_tokens": 13}}
                data = json.dumps(payload).encode("utf-8")
                self.send_response(self.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            endpoint = f"http://127.0.0.1:{server.server_port}/v1/chat/completions"
            with tempfile.TemporaryDirectory() as directory:
                config = Path(directory) / "config.json"
                config.write_text(json.dumps({"drivers": [{"kind": "openai_compatible", "name": "local_http",
                                  "endpoint": endpoint, "model": "synthetic-model", "api_key_env": "CATER_EVAL_TEST_KEY",
                                  "timeout": 2, "token_budget": 1000}]}), encoding="utf-8")
                for status in (200, 401):
                    Handler.status = status
                    stdout, stderr = io.StringIO(), io.StringIO()
                    output = Path(directory) / f"report-{status}.json"
                    with patch.dict(os.environ, {"CATER_EVAL_TEST_KEY": "synthetic-key", "NO_PROXY": "127.0.0.1,localhost"}):
                        with redirect_stdout(stdout), redirect_stderr(stderr):
                            code = evaluation.main(["--config", str(config), "--driver", "local_http",
                                                    "--limit", "1" if status == 200 else "3", "--output", str(output)])
                    report = json.loads(output.read_text(encoding="utf-8"))
                    self.assertEqual(code, 0 if status == 200 else 1)
                    self.assertEqual(report["summary"]["calls"], 1)
                    self.assertNotIn("synthetic-key", json.dumps(report) + stdout.getvalue() + stderr.getvalue())
                    self.assertNotIn(endpoint, json.dumps(report) + stdout.getvalue() + stderr.getvalue())
                    if status == 200:
                        self.assertEqual(report["summary"]["reported_tokens"], 13)
                        self.assertTrue(report["summary"]["usage_complete"])
                        self.assertEqual(report["cases"][0]["status"], "matched")
                    else:
                        self.assertEqual(report["cases"][0]["status"], "provider_error")
                        self.assertEqual(report["cases"][1]["reason"], "provider_error")
            self.assertEqual(len(requests), 2)
            self.assertTrue(all(request["authorized"] for request in requests))
            body = requests[0]["body"]
            self.assertEqual(body["model"], "synthetic-model")
            envelope = json.loads(body["messages"][1]["content"].rsplit("\n", 1)[1])
            self.assertEqual(envelope["message"], case["message"])
            self.assertEqual(envelope["tasks"], case["tasks"])
            self.assertEqual(envelope["history"], case["history"])
            self.assertTrue(envelope["read_only"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
        self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
