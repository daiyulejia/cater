"""Offline Python contracts for optional assert_js diagnostics, not JS execution."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import socket
import sys
import unittest
from unittest.mock import patch

import cater.browser_checks as checks
import cater.team_browser as team_browser
import test_simple_team
import test_team_browser
from cater.verification import CheckResult
from cater.work import WorkConflict


ASSERT = {"action": "assert_js", "label": "Inspect counter", "expression": "() => false"}
OBSERVE = "() => ({count: document.querySelector('#count').textContent})"
MAIN_ERROR = "assert_js: expression did not return exactly true"
OBSERVED = '{"count":"0","note":"\\n<literal>\\u4e2d"}'
ACTION_ARGUMENTS = {
    "click": {"selector": "#add"},
    "press": {"key": " "},
    "click_at": {"selector": "#add", "x": 0, "y": 1.5},
    "fill": {"selector": "#count", "value": ""},
    "wait": {"ms": 0},
    "assert_visible": {"selector": "#count"},
    "assert_text": {"selector": "#count", "text": "1"},
    "evaluate": {"expression": "() => undefined"},
    "assert_js": {"expression": "() => true"},
}


def raw_plan(*steps):
    return {"entrypoint": "index.html", "steps": list(steps)}


def plan_hash(plan):
    return hashlib.sha256(json.dumps(plan, ensure_ascii=False).encode("utf-8")).hexdigest()


def compact_failure(plan, *, observed=OBSERVED, error=MAIN_ERROR):
    first = {"index": 1, "status": "failed", "actual": False, "error": error}
    if observed is not None:
        first["observed"] = observed
    return {"outcome": "failed", "phase": "execution", "steps_encoding": "plan_index_v1",
            "plan_sha256": plan_hash(plan), "counts": {"page_errors": 0},
            "error": "one or more steps failed or were not run",
            "steps": [first, *({"index": i, "status": "not_run"}
                               for i in range(2, len(plan["steps"]) + 1))]}


def runner_result(report):
    return CheckResult(
        {"outcome": "failed", "exit_code": 1, "duration_ms": 1,
         "process_scope": "fixture", "stdout": {"complete": True, "truncated": False},
         "stderr": {"complete": True, "truncated": False}},
        json.dumps(report, ensure_ascii=False), "")


class BrowserDiagnosticsContractTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(socket, "socket", side_effect=AssertionError("network forbidden")))

    def test_legacy_plan_is_exactly_compatible_for_every_action(self):
        steps = [{"action": action, "label": action, **args}
                 for action, args in ACTION_ARGUMENTS.items()]
        raw = raw_plan(*steps)
        original = copy.deepcopy(raw)
        expected = {**original, "files": ["index.html", "app.js"]}
        normalized = checks.normalize_plan(raw, expected["files"])
        self.assertEqual(normalized, expected)
        self.assertEqual(json.dumps(normalized, ensure_ascii=False), json.dumps(expected, ensure_ascii=False))
        self.assertEqual(raw, original)
        self.assertTrue(all("observe" not in step for step in normalized["steps"]))

    def test_optional_observe_preserves_expression_and_unicode_codepoint_boundary(self):
        for expression in (OBSERVE, "1", "() => undefined", "Promise.resolve({count: 0})",
                           " \n() => 'state'\n ", "x" * 4000, "\U0001f642" * 4000):
            with self.subTest(length=len(expression)):
                raw = raw_plan({**ASSERT, "observe": expression})
                original = copy.deepcopy(raw)
                result = checks.normalize_plan(raw, ["index.html"])
                self.assertEqual(result, {**original, "files": ["index.html"]})
                self.assertEqual(raw, original)

    def test_observe_rejects_non_strings_blank_and_over_limit_values(self):
        for value in (None, False, True, 0, 1.5, {}, [], [OBSERVE], "", " \t\n ",
                      "x" * 4001, "\U0001f642" * 4001):
            with self.subTest(kind=type(value).__name__, length=len(value) if isinstance(value, str) else None):
                with self.assertRaises(ValueError):
                    checks.normalize_plan(raw_plan({**ASSERT, "observe": value}), ["index.html"])

    def test_other_actions_cannot_accept_observe(self):
        for action, args in ACTION_ARGUMENTS.items():
            if action == "assert_js":
                continue
            step = {"action": action, "label": action, **args, "observe": OBSERVE}
            with self.subTest(action=action), self.assertRaises(ValueError):
                checks.normalize_plan(raw_plan(step, ASSERT), ["index.html"])

    def test_unknown_fields_and_missing_required_fields_remain_rejected(self):
        step = {**ASSERT, "observe": OBSERVE}
        for field in ("observed", "observes", "timeout", "mode", "command"):
            with self.subTest(extra=field), self.assertRaises(ValueError):
                checks.normalize_plan(raw_plan({**step, field: "unexpected"}), ["index.html"])
        for field in ("action", "label", "expression"):
            incomplete = {k: v for k, v in step.items() if k != field}
            with self.subTest(missing=field), self.assertRaises(ValueError):
                checks.normalize_plan(raw_plan(incomplete), ["index.html"])
        for field in ("observe", "files", "mode"):
            with self.subTest(plan_extra=field), self.assertRaises(ValueError):
                checks.normalize_plan({**raw_plan(step), field: OBSERVE}, ["index.html"])

    def test_schema_has_exact_required_fields_and_only_one_optional_observe(self):
        schema = team_browser.browser_schema()
        self.assertEqual(schema["name"], "browser_check")
        outer = schema["input_schema"]
        self.assertEqual(outer["required"], ["artifact_seqs", "plan"])
        self.assertEqual(set(outer["properties"]), {"artifact_seqs", "plan", "archive_files"})
        selection = outer["properties"]["archive_files"]
        self.assertEqual((selection["type"], selection["minItems"], selection["maxItems"]), ("array", 1, 128))
        self.assertEqual(set(selection["items"]["properties"]), {"artifact_seq", "paths"})
        self.assertEqual(set(selection["items"]["required"]), {"artifact_seq", "paths"})
        self.assertFalse(selection["items"]["additionalProperties"])
        self.assertIs(outer["additionalProperties"], False)
        plan = outer["properties"]["plan"]
        self.assertEqual(plan["required"], ["entrypoint", "steps"])
        self.assertEqual(set(plan["properties"]), {"entrypoint", "steps"})
        self.assertIs(plan["additionalProperties"], False)
        variants = plan["properties"]["steps"]["items"]["oneOf"]
        by_action = {v["properties"]["action"]["enum"][0]: v for v in variants}
        self.assertEqual(len(variants), len(ACTION_ARGUMENTS))
        self.assertEqual(set(by_action), set(ACTION_ARGUMENTS))
        for action, args in ACTION_ARGUMENTS.items():
            with self.subTest(action=action):
                variant = by_action[action]
                required = ["action", "label", *args]
                self.assertEqual(variant["required"], required)
                self.assertIs(variant["additionalProperties"], False)
                self.assertEqual(set(variant["properties"]),
                                 set(required) | ({"observe"} if action == "assert_js" else set()))
        observe = by_action["assert_js"]["properties"]["observe"]
        self.assertEqual({k: observe[k] for k in ("type", "minLength", "maxLength")},
                         {"type": "string", "minLength": 1, "maxLength": 4000})

    def test_compact_expansion_keeps_false_observed_error_and_unexecuted_tail(self):
        plan = checks.normalize_plan(raw_plan({**ASSERT, "observe": OBSERVE},
                                             {"action": "wait", "label": "Later", "ms": 0}), ["index.html"])
        for observed in (OBSERVED, "true", "\u72b6\u6001\U0001f642" * 400):
            report = compact_failure(plan, observed=observed)
            original = copy.deepcopy(report)
            with self.subTest(observed_length=len(observed)):
                expanded = checks.expand_browser_report(report, plan)
                self.assertEqual(expanded, {**original, "steps": [
                    {**row, "action": step["action"], "label": step["label"]}
                    for row, step in zip(original["steps"], plan["steps"])]})
                row = expanded["steps"][0]
                self.assertIs(row["actual"], False)
                self.assertEqual(row["observed"], observed)
                self.assertEqual(row["error"], MAIN_ERROR)
                self.assertEqual(expanded["outcome"], "failed")
                self.assertEqual(expanded["steps"][1]["status"], "not_run")
                self.assertNotIn("actual", expanded["steps"][1])
                self.assertNotIn("observe", row)
                self.assertNotIn("expression", row)
                self.assertEqual(report, original)
                receipt = checks.browser_receipt(runner_result(report), plan)
                self.assertEqual(receipt["outcome"], "failed")
                self.assertEqual(receipt["browser_report"], expanded)

    def test_compact_hash_binds_optional_expression_including_legacy_absence(self):
        plan = checks.normalize_plan(raw_plan({**ASSERT, "observe": OBSERVE}), ["index.html"])
        report = compact_failure(plan)
        alternatives = [raw_plan(ASSERT), raw_plan({**ASSERT, "observe": "() => 'different'"})]
        for raw in alternatives:
            other = checks.normalize_plan(raw, ["index.html"])
            with self.subTest(observe=other["steps"][0].get("observe")):
                self.assertNotEqual(plan_hash(plan), plan_hash(other))
                with self.assertRaises(ValueError):
                    checks.expand_browser_report(report, other)
                self.assertEqual(checks.browser_receipt(runner_result(report), other)["outcome"], "unavailable")
                old_report = compact_failure(other)
                with self.assertRaises(ValueError):
                    checks.expand_browser_report(old_report, plan)

    def test_compact_observed_is_text_and_not_an_extra_report_field(self):
        plan = checks.normalize_plan(raw_plan({**ASSERT, "observe": OBSERVE}), ["index.html"])
        for observed in (None, False, 1, {}, []):
            report = compact_failure(plan)
            report["steps"][0]["observed"] = observed
            with self.subTest(kind=type(observed).__name__), self.assertRaises(ValueError):
                checks.expand_browser_report(report, plan)
        report = compact_failure(plan)
        report["steps"][0]["observe"] = OBSERVE
        with self.assertRaises(ValueError):
            checks.expand_browser_report(report, plan)


class TeamBrowserDiagnosticsTests(unittest.TestCase):
    # Import modules, not TestCase classes: discovery must run only this file's tests.
    artifact = test_team_browser.TeamBrowserTests.artifact
    execute = test_team_browser.TeamBrowserTests.execute
    old_check = test_team_browser.TeamBrowserTests.old_check
    events = test_simple_team.SimpleTeamTests.events
    run_ok = test_simple_team.SimpleTeamTests.run_ok

    def setUp(self):
        self.enterContext(patch.object(socket, "socket", side_effect=AssertionError("network forbidden")))
        test_simple_team.SimpleTeamTests.setUp(self)
        self.paths = []
        self.runner_plan_hash = None
        self.observed = OBSERVED
        self.error = MAIN_ERROR

    def prepare(self):
        self.rt.browser_checks.command = [sys.executable, "not-executed-fixture-runner.py"]
        self.args = {"artifact_seqs": [self.artifact().seq, self.artifact("app.js", test_team_browser.JS).seq],
                     "plan": raw_plan({**ASSERT, "observe": OBSERVE},
                                      {"action": "assert_text", "label": "Later assertion",
                                       "selector": "#count", "text": "1"})}
        return copy.deepcopy(self.args)

    def runner(self, command, *, cwd, **kwargs):
        root = Path(cwd)
        self.paths.append(root)
        self.assertTrue(root.is_dir())
        self.assertFalse(self.rt.log.db.in_transaction)
        self.assertEqual(command, [*self.rt.browser_checks.command, "--plan", checks.PLAN_FILE])
        self.assertEqual({p.name for p in root.iterdir()}, {"index.html", "app.js", checks.PLAN_FILE})
        self.assertEqual((root / "index.html").read_bytes(), test_team_browser.HTML.encode("utf-8"))
        self.assertEqual((root / "app.js").read_bytes(), test_team_browser.JS.encode("utf-8"))
        plan_bytes = (root / checks.PLAN_FILE).read_bytes()
        plan = json.loads(plan_bytes)
        self.assertEqual(plan, {**self.args["plan"], "files": ["index.html", "app.js"]})
        self.assertEqual(kwargs["timeout"], self.rt.browser_checks.timeout)
        self.assertIs(kwargs["record_output"], True)
        report = compact_failure(plan, observed=self.observed, error=self.error)
        self.assertEqual(report["plan_sha256"], hashlib.sha256(plan_bytes).hexdigest())
        self.runner_plan_hash = report["plan_sha256"]
        return runner_result(report)

    def assert_failure(self, result, *, current=True):
        self.assertEqual((result["status"], result["outcome"], result["exit_code"]), ("ok", "failed", 1))
        self.assertIs(result["scope_current"], current)
        self.assertIs(result["artifacts_unchanged"], True)
        self.assertIs(result["complete"], True)
        self.assertEqual(result["browser_plan"], {**self.args["plan"], "files": ["index.html", "app.js"]})
        report = result["browser_report"]
        self.assertEqual(report["outcome"], "failed")
        self.assertEqual(report["plan_sha256"], self.runner_plan_hash)
        self.assertIs(report["steps"][0]["actual"], False)
        self.assertEqual(report["steps"][0]["status"], "failed")
        self.assertEqual(report["steps"][0]["error"], self.error)
        if self.observed is None:
            self.assertNotIn("observed", report["steps"][0])
        else:
            self.assertEqual(report["steps"][0]["observed"], self.observed)
        self.assertEqual(report["steps"][1]["status"], "not_run")
        self.assertTrue(self.paths)
        self.assertTrue(all(not path.exists() for path in self.paths))

    def test_fixed_copy_forwards_observe_and_preserves_failed_diagnostic_receipt(self):
        args = self.prepare()
        original = copy.deepcopy(args)
        before = len(self.rt.log.read(task_id=self.task))
        with patch("cater.team_browser.run_check", side_effect=self.runner) as run:
            result = self.execute(args)
        run.assert_called_once()
        self.assert_failure(result)
        self.assertEqual(args, original)
        self.assertEqual(len(self.rt.log.read(task_id=self.task)), before)
        self.assertEqual(result["checked_artifacts"], [
            {"seq": seq, "filename": filename, "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest()}
            for seq, filename, content in zip(args["artifact_seqs"], ["index.html", "app.js"],
                                             [test_team_browser.HTML, test_team_browser.JS])])
        self.assertNotIn("cwd", result)
        self.assertNotIn("command", result)
        self.assertEqual(self.rt.team.view(self.task)["status"], "running")

    def test_observation_collection_error_cannot_replace_primary_failure(self):
        args = self.prepare()
        self.observed = None
        self.error = MAIN_ERROR + "; \u72b6\u6001\u91c7\u96c6\u672a\u5b8c\u6210: fixture serialization failed"
        with patch("cater.team_browser.run_check", side_effect=self.runner):
            result = self.execute(args)
        self.assert_failure(result)
        self.assertTrue(result["browser_report"]["steps"][0]["error"].startswith(MAIN_ERROR))
        self.assertIn("\u72b6\u6001\u91c7\u96c6\u672a\u5b8c\u6210", result["browser_report"]["steps"][0]["error"])

    def test_observe_does_not_bypass_stale_versions_pause_or_unknown_invocation(self):
        args = self.prepare()
        self.artifact()
        with patch("cater.team_browser.run_check") as run:
            self.assertEqual(self.execute(args)["code"], "invalid_browser_check")
            run.assert_not_called()
        args = self.prepare()
        self.rt.execution_control.change(self.task, "pause", request_id="diagnostics-pause")
        with patch("cater.team_browser.run_check") as run:
            with self.assertRaises(WorkConflict):
                self.execute(args)
            run.assert_not_called()
        self.rt.execution_control.change(self.task, "resume", request_id="diagnostics-resume")
        old = self.old_check()
        with patch("cater.team_browser.run_check") as run:
            blocked = self.execute(args)
            self.assertEqual(blocked["code"], "browser_check_pending")
            self.assertEqual(blocked["started_seqs"], [old.seq])
            run.assert_not_called()

    def test_publication_change_during_run_keeps_only_historical_diagnostics(self):
        args = self.prepare()

        def changed(*a, **kw):
            result = self.runner(*a, **kw)
            self.artifact("app.js", "// newer publication")
            return result

        with patch("cater.team_browser.run_check", side_effect=changed):
            result = self.execute(args)
        self.assert_failure(result, current=False)
        self.assertEqual([a["seq"] for a in result["checked_artifacts"]], args["artifact_seqs"])
        self.assertIn("\u5386\u53f2\u8bb0\u5f55", result["note"])

    def test_native_tool_history_and_read_receipts_keep_complete_observation(self):
        args = self.prepare()
        self.observed = "BEGIN\n<literal>\\n\u72b6\u6001\U0001f642" + "z" * 1100 + "END"
        self.driver.steps[self.manager] = [
            test_simple_team.turn("Inspect fixture", ("browser_check", args)),
            test_simple_team.turn("Read original receipt", ("read_receipts", {"name": "browser_check"})),
            test_simple_team.turn("Failure retained; no approval.")]
        self.rt.team.receive(self.task, "Offline diagnostic fixture", request_id="diagnostics-native-history")
        with patch("cater.team_browser.run_check", side_effect=self.runner) as run:
            self.run_ok()
        run.assert_called_once()
        started = next(e for e in self.events("team.tool_started") if e.payload["name"] == "browser_check")
        finished = next(e for e in self.events("team.tool_finished") if e.payload["name"] == "browser_check")
        original = finished.payload["result"]
        self.assertEqual(started.payload["arguments"], args)
        self.assert_failure(original)
        self.assertEqual(len(self.driver.calls), 3)
        first_history = self.driver.calls[1]["history"]
        self.assertEqual(json.loads(first_history[-1]["content"]), original)
        self.assertEqual(first_history[-2]["tool_calls"][0]["arguments"], args)
        last_history = self.driver.calls[2]["history"]
        self.assertEqual(last_history[:len(first_history)], first_history)
        read_result = json.loads(last_history[-1]["content"])
        self.assertEqual(len(read_result["receipts"]), 1)
        self.assertEqual(read_result["receipts"][0]["result"], original)
        self.assertEqual(read_result["receipts"][0]["seq"], finished.seq)
        for kind in ("team.review_recorded", "team.objective_completed", "verification.completed"):
            self.assertEqual(self.events(kind), [])


if __name__ == "__main__":
    unittest.main()
