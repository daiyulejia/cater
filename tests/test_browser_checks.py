from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cater.anthropic import AnthropicMessagesDriver
from cater.artifacts import normalize_deliverables
from cater.browser_checks import BrowserChecks, PLAN_FILE, normalize_plan
from cater.drivers import CliDriver
from cater.events import Actor
from cater.runtime import Runtime
from cater.review_contract import assessment, browser_observations
from cater.verification import CheckResult
from cater.work import WorkConflict
from test_work import WorkDriver, item


HTML = '<!doctype html><button id="save">Save</button><p id="result">Ready</p>'
PLAN = {"entrypoint": "index.html", "steps": [
    {"action": "click", "label": "Activate save", "selector": "#save"},
    {"action": "assert_text", "label": "Observe ready state", "selector": "#result", "text": "Ready"},
]}


class BrowserPlanTests(unittest.TestCase):
    def test_receipt_citation_accepts_only_current_observation_not_plan_or_label(self):
        receipt = {"seq": 27, "check_kind": "browser", "browser_report": {
            "error": "Observed page error", "steps": [{"label": "Expected board rendering",
                "actual": "Observed blank canvas", "error": "assert_js: expression did not return exactly true"}]}}
        self.assertEqual(browser_observations(receipt), ["Observed page error",
            "assert_js: expression did not return exactly true", "Observed blank canvas"])
        row = {"id": "behavior", "status": "failed", "reason": "No drawing observed after initialization.",
               "artifact_seq": None, "receipt_seq": 27, "quote": "Observed blank canvas"}
        judgment, issue, _ = assessment([row], "behavior", {}, receipt)
        self.assertEqual(judgment["status"], "failed")
        self.assertIsNone(issue)
        for change, expected in (({"receipt_seq": 26}, "invalid_receipt"),
                                 ({"receipt_seq": True}, "invalid_receipt"),
                                 ({"quote": "Expected board rendering"}, "quote_not_found"),
                                 ({"quote": "invented output"}, "quote_not_found"),
                                 ({"quote": ""}, "missing_quote")):
            with self.subTest(change=change):
                judgment, issue, _ = assessment([{**row, **change}], "behavior", {}, receipt)
                self.assertIsNone(judgment)
                self.assertEqual(issue, expected)
        self.assertEqual(assessment([row], "behavior", {})[1], "invalid_artifact")
        self.assertEqual(assessment([row], "behavior", {}, {**receipt, "check_kind": "command"})[1], "invalid_artifact")

    def test_browser_basenames_reject_hidden_url_metacharacters_and_nfc_collisions(self):
        for name in (".hidden.js", "asset%20.js", "asset#section.js", "asset\x7f.js", "COM\u00b9.txt"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                normalize_plan(PLAN, ["index.html", name])
        with self.assertRaises(ValueError):
            normalize_plan(PLAN, ["index.html", "caf\u00e9.js", "cafe\u0301.js"])

    def test_labels_are_nonblank_control_free_and_not_required_to_be_chinese(self):
        for label in ("", " \t ", "\nAction", "Action\x00", "Action\x7f"):
            plan = {**PLAN, "steps": [{**PLAN["steps"][1], "label": label}]}
            with self.subTest(label=repr(label)), self.assertRaises(ValueError):
                normalize_plan(plan, ["index.html"])
        self.assertEqual(normalize_plan(PLAN, ["index.html"])["steps"][0]["label"], "Activate save")

    def test_plan_is_bound_to_submitted_html_and_has_a_real_assertion(self):
        original = copy.deepcopy(PLAN)
        normalized = normalize_plan(PLAN, ["index.html", "style.css"])
        self.assertEqual(normalized, {**PLAN, "files": ["index.html", "style.css"]})
        self.assertEqual(PLAN, original)
        with self.assertRaises(ValueError):
            normalize_plan({"entrypoint": "index.html", "steps": PLAN["steps"][:1]}, ["index.html"])

    def test_malicious_fields_types_and_unbounded_steps_are_rejected(self):
        invalid = [
            {**PLAN, "command": ["powershell", "-Command", "must-not-run"]},
            {**PLAN, "steps": [{"action": "exec", "label": "Host shell", "command": "whoami"}]},
            {**PLAN, "steps": [{**PLAN["steps"][1], "path": "C:/private.txt"}]},
            {**PLAN, "steps": [{**PLAN["steps"][1], "selector": True}]},
            {**PLAN, "steps": [{**PLAN["steps"][1], "selector": "x" * 501}]},
            {**PLAN, "steps": [{"action": "wait", "label": "Wait", "ms": True}, PLAN["steps"][1]]},
            {**PLAN, "steps": [{"action": "wait", "label": "Wait", "ms": 2001}, PLAN["steps"][1]]},
            {**PLAN, "steps": [{"action": "assert_js", "label": "Expression", "expression": "x" * 4001}]},
            {**PLAN, "steps": PLAN["steps"] * 11},
            {**PLAN, "steps": []},
        ]
        for plan in invalid:
            with self.subTest(plan=str(plan)[:100]), self.assertRaises(ValueError):
                normalize_plan(plan, ["index.html"])

    def test_missing_external_and_escaping_files_are_not_execution_targets(self):
        for entry in ("missing.html", "../outside.html", "C:/outside.html", "https://example.test/index.html", "notes.txt"):
            with self.subTest(entry=entry), self.assertRaises(ValueError):
                normalize_plan({**PLAN, "entrypoint": entry}, ["index.html"])
        for files in (["index.html", "../outside.js"], ["index.html", "INDEX.HTML"],
                      ["index.html", PLAN_FILE], ["index.html", "C:/private.txt"], ["index.html", None]):
            with self.subTest(files=files), self.assertRaises(ValueError):
                normalize_plan(PLAN, files)


class BrowserCheckTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.db = self.root / "browser.db"
        self.config = self.root / "checker.json"
        self.command = [sys.executable, str(self.root / "mock-browser-runner.py")]
        self.config.write_text(json.dumps({"command": self.command, "timeout": 12}), encoding="utf-8")
        self.api = AnthropicMessagesDriver("test", endpoint="https://provider.test/fixture", model="fixture",
                                          api_key_env="CATER_BROWSER_TEST_KEY")
        self.worker = WorkDriver()
        self.drivers = {"dev": self.worker, "product": self.worker, "design": self.worker, "test": self.api}
        self.rt = Runtime(self.db, drivers=self.drivers)
        self.addCleanup(self.rt.close)
        self.task = self.rt.create_task("Check the submitted static HTML interaction")
        self.rt.plan_work(self.task, [item("page", local=True)])
        self.submit()
        self.service = BrowserChecks(self.rt, self.config)
        self.reply = {"status": "ready", "reason": "Exercise the submitted page and assert an observed state.",
                      "plan": copy.deepcopy(PLAN)}
        self.model_hook = None
        self.browser_report = None
        self.exit_code = 0
        self.review_status = None
        self.review_prompts = []
        self.requests, self.executions = [], []
        self.enterContext(patch.dict(os.environ, {"CATER_BROWSER_TEST_KEY": "fixture-only"}))
        self.transport = self.enterContext(patch("cater.anthropic._open_api_request", side_effect=self.model_response))
        self.runner = self.enterContext(patch("cater.runtime.run_check", side_effect=self.execute))

    def submit(self, *, filename="index.html"):
        claim = self.rt.work.claim(self.task, "page")
        artifact = normalize_deliverables([{"title": "Page", "summary": "Unrun submitted HTML",
                    "content": HTML, "format": "code", "filename": filename}])[0]
        published = self.rt.log.emit(type="deliverable.published", task_id=self.task,
            actor=Actor.agent("dev", "worker"), payload={**artifact, "work_item_id": "page",
                "attempt_id": claim.payload["attempt_id"]})
        self.rt.work.submit(self.task, claim, [published.seq])

    def current(self):
        return next(row for row in self.rt.work.view(self.task)["items"] if row["id"] == "page")

    def events(self, kind):
        return list(self.rt.log.iter_events(task_id=self.task, event_types=(kind,)))

    def publish(self, item_id, filename, content, *, accept=False):
        claim = self.rt.work.claim(self.task, item_id)
        body = normalize_deliverables([{"title": item_id, "summary": "Version-bound browser input",
            "filename": filename, "content": content, "format": "code"}])[0]
        artifact = self.rt.log.emit(type="deliverable.published", task_id=self.task, actor=Actor.agent("dev", "worker"),
            payload={**body, "work_item_id": item_id, "attempt_id": claim.payload["attempt_id"]})
        submitted = self.rt.work.submit(self.task, claim, [artifact.seq])
        if accept:
            self.rt.work.review(self.task, item_id, reviewer="design", submission_seq=submitted.seq,
                results=[{"id": "meaning", "status": "passed", "reason": "Read the submitted dependency",
                          "artifact_seq": artifact.seq, "quote": content, "receipt_seq": None}])
        return artifact

    def dependency_fixture(self):
        self.task = self.rt.create_task("Verify a report against its fixed direct HTML input")
        self.rt.plan_work(self.task, [item("ancestor", reviewer="design"),
            item("code", reviewer="design", deps=["ancestor"]), item("unrelated", reviewer="design"),
            item("page", deps=["code"], local=True)])
        self.publish("ancestor", "ancestor.txt", "Transitive input", accept=True)
        code = self.publish("code", "index.html", HTML, accept=True)
        self.publish("unrelated", "other.html", "Unrelated private output", accept=True)
        report = self.publish("page", "report.txt", "Review the accepted HTML behavior.")
        return code, report

    def review_rows(self, receipt, *, status="passed"):
        artifact = self.rt.work.artifacts(self.task, self.current()["submission"])[0]
        return [{"id": c["id"], "status": status, "reason": "The generated test may not establish application behavior.",
                 "artifact_seq": artifact["seq"], "quote": artifact["content"],
                 "receipt_seq": receipt.seq if c["kind"] == "local_check" else None}
                for c in self.current()["criteria"]]

    def model_response(self, request, **kwargs):
        self.requests.append(json.loads(request.data))
        if self.model_hook:
            self.model_hook()
        prompt = self.requests[-1]["messages"][0]["content"]
        reply = self.reply
        if self.review_status is not None and '"phase": "review"' in prompt:
            self.review_prompts.append(prompt)
            artifact = self.rt.work.artifacts(self.task, self.current()["submission"])[0]
            receipt = self.rt.work.checks(self.task, self.current()).get("behavior")
            reply = {"checks": [{"id": c["id"], "status": self.review_status,
                "reason": "The generated test is invalid; its failure does not establish an application defect.",
                "artifact_seq": artifact["seq"], "quote": artifact["content"],
                "receipt_seq": receipt.seq if c["kind"] == "local_check" and receipt else None}
                for c in self.current()["criteria"]]}
        result = {"type": "message", "content": [{"type": "text", "text": json.dumps(reply)}],
                  "stop_reason": "end_turn", "usage": {"input_tokens": 20, "output_tokens": 5}}
        return io.BytesIO(json.dumps(result).encode())

    def execute(self, command, *, cwd, timeout, record_output):
        plan = json.loads((cwd / PLAN_FILE).read_text(encoding="utf-8"))
        self.assertEqual((cwd / "index.html").read_text(encoding="utf-8"), HTML)
        self.executions.append({"command": command, "plan": plan, "cwd": cwd,
                                "timeout": timeout, "record_output": record_output,
                                "files": {name: (cwd / name).read_text(encoding="utf-8") for name in plan["files"]}})
        report = self.browser_report
        if report is None:
            report = {"outcome": "passed", "plan_sha256": hashlib.sha256(json.dumps(plan, ensure_ascii=False).encode("utf-8")).hexdigest(),
                      "steps": [{"index": i, "action": step["action"], "status": "passed",
                                 **({"actual": True} if step["action"].startswith("assert_") else {})}
                                for i, step in enumerate(plan["steps"], 1)], "counts": {"page_errors": 0}}
        text = report if isinstance(report, str) else json.dumps(report)
        streams = {"complete": True, "bytes": 0, "sha256": "0" * 64, "truncated": False, "excerpt": ""}
        stdout = {**streams, "bytes": len(text.encode()), "sha256": hashlib.sha256(text.encode()).hexdigest(), "excerpt": text}
        return CheckResult(payload={"source": "explicit_local_cli", "command": command,
            "outcome": "passed" if self.exit_code == 0 else "failed", "exit_code": self.exit_code,
            "stdout": stdout, "stderr": dict(streams)},
            stdout_preview=text, stderr_preview="")

    def test_disabled_service_does_not_call_model_or_execute(self):
        BrowserChecks(self.rt, None).run(self.task, self.current())
        self.transport.assert_not_called()
        self.runner.assert_not_called()
        self.assertEqual(self.events("verification.planning_started"), [])

    def test_config_requires_absolute_command_and_bounded_timeout(self):
        for config in ({"command": ["python", "runner.py"]}, {"command": self.command, "timeout": 0},
                       {"command": self.command, "timeout": True}, {"command": self.command, "extra": "ignored?"}):
            with self.subTest(config=config):
                self.config.write_text(json.dumps(config), encoding="utf-8")
                with self.assertRaises(ValueError):
                    BrowserChecks(self.rt, self.config)
        self.runner.assert_not_called()
        self.transport.assert_not_called()

    def test_ready_plan_executes_configured_runner_once_and_records_bound_receipt(self):
        submitted = self.current()["submission"]
        self.service.run(self.task, self.current())
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(len(self.executions), 1)
        execution = self.executions[0]
        self.assertEqual(execution["command"], [*self.command, "--plan", PLAN_FILE])
        self.assertEqual(execution["timeout"], 12)
        self.assertTrue(execution["record_output"])
        self.assertEqual(execution["plan"], {**PLAN, "files": ["index.html"]})
        self.assertNotEqual(execution["cwd"], self.root)
        receipt = self.events("verification.completed")[0]
        self.assertEqual(receipt.actor, Actor.system("local_verifier"))
        self.assertEqual(receipt.payload["submission_seq"], submitted["seq"])
        self.assertEqual(receipt.payload["submission_digest"], submitted["digest"])
        self.assertEqual(receipt.payload["criterion_id"], "behavior")
        self.assertEqual(receipt.payload["checked_artifacts"], [{k: a[k] for k in ("seq", "filename", "content_sha256")}
            for a in self.service.artifacts(self.task, self.current())])
        self.assertTrue(receipt.payload["artifacts_unchanged"])
        self.assertTrue(self.rt.work._receipt_passed(receipt))
        self.assertFalse(self.rt.work.view(self.task)["stage_accepted"])
        self.assertEqual(self.current()["status"], "submitted")
        self.assertEqual(self.rt._reported_driver_tokens("test"), 25)
        self.assertEqual(self.rt.task(self.task).tokens, 25)
        self.assertNotIn("tools", self.requests[0])
        self.assertIn("browser_check_plan", json.dumps(self.requests[0]))
        self.service.run(self.task, self.current())
        with Runtime(self.db, drivers=self.drivers, close_drivers_on_close=False) as restarted:
            BrowserChecks(restarted, self.config).run(self.task, restarted.work.view(self.task)["items"][0])
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(len(self.executions), 1)

    def test_invalid_plan_is_charged_but_never_executed(self):
        self.reply["plan"]["steps"] = PLAN["steps"][:1]
        self.service.run(self.task, self.current())
        self.assertEqual(self.events("verification.planned")[-1].payload["status"], "invalid")
        self.runner.assert_not_called()
        self.assertEqual(self.events("verification.completed"), [])
        planned = self.events("verification.planned")
        self.assertEqual([(e.payload["status"], e.payload["correction"]) for e in planned],
                         [("correcting", 0), ("invalid", 1)])
        self.assertEqual([e.payload["tokens"] for e in planned], [25, 25])
        self.assertEqual(len(self.events("verification.planning_started")), 1)
        self.assertEqual(self.rt._reported_driver_tokens("test"), 50)
        self.assertEqual(self.rt.task(self.task).tokens, 50)
        self.service.run(self.task, self.current())
        self.assertEqual(len(self.requests), 2)

    def test_one_invalid_text_proposal_can_be_corrected_and_execute_exactly_once(self):
        valid = copy.deepcopy(self.reply)
        self.reply["plan"]["steps"] = PLAN["steps"][:1]
        invalid = copy.deepcopy(self.reply)
        def correct_second_call():
            self.runner.assert_not_called()
            if len(self.requests) == 2:
                self.assertEqual(self.events("verification.planned")[-1].payload["status"], "correcting")
                self.reply = valid
        self.model_hook = correct_second_call
        self.service.run(self.task, self.current())
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(len(self.executions), 1)
        self.assertEqual(len(self.events("verification.planning_started")), 1)
        planned = self.events("verification.planned")
        self.assertEqual([(e.payload["status"], e.payload["correction"]) for e in planned],
                         [("correcting", 0), ("ready", 1)])
        self.assertEqual(len({event.payload["request_seq"] for event in planned}), 1)
        self.assertEqual([e.payload["tokens"] for e in planned], [25, 25])
        self.assertEqual(self.rt._reported_driver_tokens("test"), 50)
        prompt = self.requests[1]["messages"][0]["content"]
        self.assertIn('"previous_proposal": ' + json.dumps(invalid, ensure_ascii=False), prompt)
        self.assertIn("Your proposal was not executed", prompt)
        self.service.run(self.task, self.current())
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(len(self.executions), 1)

    def test_scope_change_during_text_correction_prevents_execution(self):
        valid = copy.deepcopy(self.reply)
        self.reply["plan"]["steps"] = PLAN["steps"][:1]
        def change_second_call():
            if len(self.requests) == 2:
                self.reply = valid
                self.rt.command(self.task, "Changed during plan correction", goal="Different scope")
        self.model_hook = change_second_call
        self.service.run(self.task, self.current())
        self.assertEqual(len(self.requests), 2)
        self.runner.assert_not_called()
        self.assertEqual([e.payload["status"] for e in self.events("verification.planned")], ["correcting", "stale"])
        self.assertEqual(len(self.events("verification.planning_started")), 1)
        self.assertEqual(self.events("verification.completed"), [])
        self.assertEqual(self.rt._reported_driver_tokens("test"), 50)

    def test_first_plan_usage_can_exhaust_budget_before_the_correction_call(self):
        self.api.token_budget = 25
        self.reply["plan"]["steps"] = PLAN["steps"][:1]
        self.service.run(self.task, self.current())
        self.assertEqual(len(self.requests), 1)
        self.runner.assert_not_called()
        planned = self.events("verification.planned")
        self.assertEqual([(e.payload["status"], e.payload["correction"]) for e in planned],
                         [("correcting", 0), ("unavailable", 1)])
        self.assertEqual([e.payload["tokens"] for e in planned], [25, 0])
        self.assertEqual(self.rt._reported_driver_tokens("test"), 25)
        self.assertEqual(self.rt.task(self.task).tokens, 25)
        self.assertEqual(len(self.events("driver.budget_exhausted")), 1)
        self.assertEqual(len(self.events("verification.planning_started")), 1)
        self.service.run(self.task, self.current())
        self.assertEqual(len(self.requests), 1)

    def test_transport_timeout_never_triggers_a_text_correction(self):
        self.transport.side_effect = TimeoutError("mock transport timeout")
        self.service.run(self.task, self.current())
        self.transport.assert_called_once()
        self.runner.assert_not_called()
        planned = self.events("verification.planned")
        self.assertEqual(len(planned), 1)
        self.assertEqual(planned[0].payload["status"], "unavailable")
        self.assertEqual(planned[0].payload["correction"], 0)

    def test_exit_zero_without_structured_browser_report_is_not_a_pass(self):
        self.browser_report = "printed success but no structured report"
        self.service.run(self.task, self.current())
        receipt = self.events("verification.completed")[-1]
        self.assertEqual(receipt.payload["outcome"], "unavailable")
        self.assertFalse(self.rt.work._receipt_passed(receipt))
        self.assertFalse(self.rt.work.view(self.task)["stage_accepted"])

    def test_browser_report_failure_overrides_zero_process_exit(self):
        self.browser_report = {"outcome": "failed", "reason": "Observed page state did not match the assertion."}
        self.service.run(self.task, self.current())
        receipt = self.events("verification.completed")[-1]
        self.assertEqual(receipt.payload["exit_code"], 0)
        self.assertEqual(receipt.payload["outcome"], "failed")
        self.assertFalse(self.rt.work._receipt_passed(receipt))

    def test_false_success_reports_are_unavailable_not_a_pass(self):
        plan = normalize_plan(PLAN, ["index.html"])
        valid = {"outcome": "passed", "plan_sha256": hashlib.sha256(json.dumps(plan, ensure_ascii=False).encode()).hexdigest(),
                 "steps": [{"index": i, "action": step["action"], "status": "passed",
                            **({"actual": True} if step["action"].startswith("assert_") else {})}
                           for i, step in enumerate(plan["steps"], 1)], "counts": {"page_errors": 0}}
        invalid = [{"outcome": "passed"}, {**valid, "steps": []}, {**valid, "plan_sha256": "wrong-plan"},
                   {**valid, "steps": valid["steps"][:1]}, {**valid, "counts": {"page_errors": 1}},
                   {**valid, "counts": "not-a-report-object"},
                   {**valid, "counts": {"page_errors": False}}, {**valid, "counts": {"page_errors": 0.0}}]
        for field, value in (("index", 0), ("index", True), ("index", 1.0), ("action", "evaluate"), ("status", "failed")):
            steps = copy.deepcopy(valid["steps"])
            steps[0][field] = value
            invalid.append({**valid, "steps": steps})
        for actual in (None, False, 1, "true"):
            steps = copy.deepcopy(valid["steps"])
            if actual is None:
                steps[1].pop("actual")
            else:
                steps[1]["actual"] = actual
            invalid.append({**valid, "steps": steps})
        for report in invalid:
            with self.subTest(report=report):
                self.browser_report = report
                receipt, _ = self.rt.verify_work(self.task, "page", "behavior", [*self.command, "--plan", PLAN_FILE],
                    browser_plan=plan, expected_submission_seq=self.current()["submission"]["seq"])
                self.assertEqual(receipt.payload["outcome"], "unavailable")
                self.assertFalse(self.rt.work._receipt_passed(receipt))

    def test_report_can_check_only_its_accepted_direct_html_dependency(self):
        code, report = self.dependency_fixture()
        current = self.current()
        artifacts = self.service.artifacts(self.task, current)
        self.assertEqual({a["seq"] for a in artifacts}, {code.seq, report.seq})
        self.assertEqual(current["submission"]["inputs"]["code"],
                         next(i for i in self.rt.work.view(self.task)["items"] if i["id"] == "code")["submission"]["digest"])
        self.service.run(self.task, current)
        self.assertEqual(len(self.executions), 1)
        self.assertEqual(self.executions[0]["files"], {"report.txt": "Review the accepted HTML behavior.", "index.html": HTML})
        receipt = self.events("verification.completed")[-1]
        expected = [{k: a.payload[k] if k != "seq" else a.seq for k in ("seq", "filename", "content_sha256")}
                    for a in (report, code)]
        self.assertEqual(receipt.payload["checked_artifacts"], expected)
        self.assertEqual(receipt.payload["browser_plan"]["entrypoint"], "index.html")

    def test_new_dependency_version_cannot_be_used_for_old_report(self):
        self.dependency_fixture()
        old = self.current()
        self.rt.work.rework(self.task, "code", "Replace accepted HTML")
        self.publish("code", "index.html", HTML + "<!-- NEW_VERSION -->", accept=True)
        with self.assertRaises(WorkConflict):
            self.service.artifacts(self.task, old)
        self.service.run(self.task, old)
        self.transport.assert_not_called()
        self.runner.assert_not_called()

    def test_unavailable_receipt_remains_unknown_even_if_review_claims_passed(self):
        self.browser_report, self.exit_code = {"outcome": "unavailable", "reason": "Browser unavailable"}, 2
        self.service.run(self.task, self.current())
        receipt = self.events("verification.completed")[-1]
        review = self.rt.work.review(self.task, "page", reviewer="test", submission_seq=self.current()["submission"]["seq"],
                                    results=self.review_rows(receipt))
        behavior = next(c for c in review.payload["checks"] if c["id"] == "behavior")
        self.assertEqual(behavior["execution_status"], "unavailable")
        self.assertEqual(behavior["status"], "unknown")
        self.assertEqual(review.payload["status"], "submitted")
        self.assertFalse(self.rt.work.view(self.task)["stage_accepted"])

    def test_failed_browser_plan_can_be_unknown_but_never_overridden_with_passed(self):
        self.browser_report = {"outcome": "failed", "reason": "Generated selector did not match the intended control."}
        self.service.run(self.task, self.current())
        receipt = self.events("verification.completed")[-1]
        review = self.rt.work.review(self.task, "page", reviewer="test", submission_seq=self.current()["submission"]["seq"],
                                    results=self.review_rows(receipt, status="unknown"))
        behavior = next(c for c in review.payload["checks"] if c["id"] == "behavior")
        self.assertEqual(behavior["execution_status"], "failed")
        self.assertEqual(behavior["status"], "unknown")
        self.assertEqual(review.payload["status"], "submitted")
        review = self.rt.work.review(self.task, "page", reviewer="test", submission_seq=self.current()["submission"]["seq"],
                                    results=self.review_rows(receipt, status="passed"))
        self.assertEqual(next(c for c in review.payload["checks"] if c["id"] == "behavior")["status"], "unknown")
        self.assertEqual(review.payload["status"], "submitted")
        self.assertFalse(self.rt.work.view(self.task)["stage_accepted"])

    def test_automatic_failed_browser_check_still_gets_independent_review(self):
        self.rt.browser_checks = self.service
        self.browser_report = {"outcome": "failed", "reason": "The generated assertion used the wrong selector."}
        self.review_status = "unknown"
        self.rt.collaborate(self.task)
        self.assertEqual(len(self.executions), 1)
        self.assertEqual(len(self.review_prompts), 1)
        behavior = next(c for c in self.current()["checks"] if c["id"] == "behavior")
        self.assertEqual(behavior["status"], "unknown")
        self.assertEqual(behavior["execution_status"], "failed")
        self.assertEqual(self.current()["status"], "submitted")
        self.assertEqual(self.events("work.invalidated"), [])

    def test_browser_failure_can_be_diagnosed_using_current_observed_receipt(self):
        self.browser_report = {"outcome": "failed", "steps": [
            {"status": "failed", "error": "Observed blank canvas"}]}
        self.service.run(self.task, self.current())
        receipt = self.events("verification.completed")[-1]
        rows = self.review_rows(receipt)
        rows[-1].update(status="failed", artifact_seq=None, quote="Observed blank canvas")
        review = self.rt.work.review(self.task, "page", reviewer="test", submission_seq=self.current()["submission"]["seq"],
                                    results=rows)
        self.assertEqual(review.payload["status"], "rework")
        behavior = next(c for c in review.payload["checks"] if c["id"] == "behavior")
        self.assertEqual(behavior["source"], "model_assessment")
        self.assertEqual(behavior["receipt_seq"], receipt.seq)
        self.assertNotIn("response_issue", behavior)

    def test_receipt_quote_cannot_override_failed_browser_execution(self):
        self.browser_report = {"outcome": "failed", "error": "Observed failure"}
        self.service.run(self.task, self.current())
        receipt = self.events("verification.completed")[-1]
        rows = self.review_rows(receipt)
        rows[-1].update(artifact_seq=None, quote="Observed failure")
        review = self.rt.work.review(self.task, "page", reviewer="test", submission_seq=self.current()["submission"]["seq"],
                                    results=rows)
        self.assertEqual(review.payload["status"], "submitted")
        self.assertEqual(review.payload["checks"][-1]["status"], "unknown")

    def test_failed_explicit_command_keeps_original_failure_gate(self):
        streams = {"complete": True}
        result = CheckResult(payload={"outcome": "failed", "exit_code": 1, "stdout": streams, "stderr": streams},
                             stdout_preview="", stderr_preview="")
        with patch("cater.runtime.run_check", return_value=result):
            self.rt.verify_work(self.task, "page", "behavior", self.command)
        self.rt._review_work_item(self.rt.task(self.task), self.current())
        self.transport.assert_not_called()
        self.assertTrue(self.events("work.invalidated"))
        self.assertEqual(self.current()["status"], "ready")

    def test_runtime_configuration_enables_only_the_explicit_browser_capability(self):
        self.assertFalse(self.rt.browser_checks.capabilities()["automatic_local_checks"])
        with Runtime(self.db, drivers=self.drivers, browser_checker_path=self.config,
                     close_drivers_on_close=False) as configured:
            capabilities = configured.browser_checks.capabilities()
            self.assertTrue(capabilities["automatic_local_checks"])
            self.assertEqual([tool["id"] for tool in capabilities["tools"]], ["browser_check"])
        self.transport.assert_not_called()
        self.runner.assert_not_called()

    def test_unsupported_check_does_not_execute_or_claim_success(self):
        self.reply = {"status": "unsupported", "reason": "Backend service is required.", "plan": None}
        self.service.run(self.task, self.current())
        self.assertEqual(len(self.requests), 1)
        self.runner.assert_not_called()
        self.assertEqual(self.events("verification.planned")[-1].payload["status"], "unsupported")
        self.assertEqual(self.current()["status"], "submitted")
        self.assertEqual(self.events("verification.completed"), [])
        self.assertEqual(len(self.events("verification.planned")), 1)

    def test_missing_entrypoint_never_reaches_runner(self):
        self.reply["plan"]["entrypoint"] = "missing.html"
        self.service.run(self.task, self.current())
        self.runner.assert_not_called()
        self.assertEqual(self.events("verification.planned")[-1].payload["status"], "invalid")

    def test_non_html_submission_does_not_plan_or_execute(self):
        self.rt.work.rework(self.task, "page", "Replace fixture with a text-only submission")
        self.submit(filename="notes.txt")
        self.service.run(self.task, self.current())
        self.transport.assert_not_called()
        self.runner.assert_not_called()
        self.assertEqual(self.events("verification.planning_started"), [])

    def test_no_local_criterion_does_not_plan_or_execute(self):
        self.task = self.rt.create_task("Review-only HTML fixture")
        self.rt.plan_work(self.task, [item("page")])
        self.submit()
        self.service.run(self.task, self.current())
        self.transport.assert_not_called()
        self.runner.assert_not_called()
        self.assertEqual(self.events("verification.planning_started"), [])

    def test_stale_submission_before_planning_cannot_call_model_or_runner(self):
        old = self.current()
        self.rt.command(self.task, "Changed HTML requirements", goal="Different scope")
        self.service.run(self.task, old)
        self.transport.assert_not_called()
        self.runner.assert_not_called()

    def test_scope_change_during_plan_generation_prevents_execution(self):
        self.model_hook = lambda: self.rt.command(self.task, "Changed while planning", goal="Different scope")
        self.service.run(self.task, self.current())
        self.assertEqual(len(self.requests), 1)
        self.runner.assert_not_called()
        self.assertEqual(self.events("verification.planned")[-1].payload["status"], "stale")
        self.assertEqual(self.events("verification.completed"), [])

    def test_missing_current_item_is_a_work_conflict_not_an_unhandled_key_error(self):
        old = self.current()
        with patch.object(self.rt.work, "view", return_value={"items": [], "scope_current": True}):
            with self.assertRaises(WorkConflict):
                self.service._current(self.task, old)

    def test_change_between_plan_and_check_start_is_rejected_before_execution(self):
        verify = self.rt.verify_work
        def change_then_verify(*args, **kwargs):
            self.rt.command(self.task, "Changed before execution", goal="Different scope")
            return verify(*args, **kwargs)
        with patch.object(self.rt, "verify_work", side_effect=change_then_verify):
            self.service.run(self.task, self.current())
        self.runner.assert_not_called()
        self.assertEqual(self.events("verification.completed"), [])

    def test_reentrant_plan_reservation_prevents_duplicate_model_calls(self):
        submitted = self.current()
        self.model_hook = lambda: self.service.run(self.task, submitted)
        self.service.run(self.task, submitted)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(len(self.executions), 1)
        self.assertEqual(len(self.events("verification.planning_started")), 1)

    def test_crash_after_reservation_is_not_replayed_after_restart(self):
        def crash():
            raise KeyboardInterrupt("simulated process interruption")
        self.model_hook = crash
        with self.assertRaises(KeyboardInterrupt):
            self.service.run(self.task, self.current())
        self.assertEqual(len(self.events("verification.planning_started")), 1)
        self.assertEqual(self.events("verification.planned"), [])
        self.model_hook = None
        with Runtime(self.db, drivers=self.drivers, close_drivers_on_close=False) as restarted:
            BrowserChecks(restarted, self.config).run(self.task, restarted.work.view(self.task)["items"][0])
        self.assertEqual(len(self.requests), 1)
        self.runner.assert_not_called()

    def test_exhausted_budget_prevents_plan_call_and_execution(self):
        self.api.token_budget = 0
        self.service.run(self.task, self.current())
        self.transport.assert_not_called()
        self.runner.assert_not_called()
        self.assertEqual(self.rt._reported_driver_tokens("test"), 0)
        self.assertEqual(self.events("verification.planned")[-1].payload["status"], "unavailable")

    def test_cli_reviewer_is_not_started_as_a_browser_plan_model(self):
        cli = CliDriver("test", ["must-not-start"])
        self.addCleanup(cli.close)
        self.rt.drivers["test"] = cli
        with patch.object(cli, "_complete", side_effect=AssertionError("CLI invoked")) as call:
            self.service.run(self.task, self.current())
            call.assert_not_called()
        self.transport.assert_not_called()
        self.runner.assert_not_called()
        self.assertEqual(self.events("verification.planning_started"), [])


if __name__ == "__main__":
    unittest.main()
