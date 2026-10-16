"""Native tools select fixed versions; browser outcomes are observations, not approval."""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from cater.artifacts import normalize_deliverables
from cater.events import Actor
from cater.team import HOST
from cater.team_browser import BROWSER_CONTRACT, browser_environment, browser_schema
from cater.verification import CheckResult
from cater.work import WorkConflict
import test_simple_team
import test_team_handoff


HTML = '<!doctype html><button id="add">加一</button><p id="count">0</p><script src="app.js"></script>'
JS = "document.querySelector('#add').onclick=()=>{document.querySelector('#count').textContent='1'}"
PLAN = {"entrypoint": "index.html", "steps": [
    {"action": "click", "label": "按下加一", "selector": "#add"},
    {"action": "assert_text", "label": "核对实际计数", "selector": "#count", "text": "1"}]}


def result_for(plan, *, outcome="passed", exit_code=0, report_change=None, payload_change=None):
    report = {"outcome": outcome, "phase": "execution", "plan_sha256": hashlib.sha256(
        json.dumps(plan, ensure_ascii=False).encode()).hexdigest(), "counts": {"page_errors": 0},
        "steps": [{"index": i, "action": s["action"], "label": s["label"], "status": "passed",
                   **({"actual": True} if s["action"].startswith("assert_") else {})}
                  for i, s in enumerate(plan["steps"], 1)], **(report_change or {})}
    payload = {"outcome": "passed" if exit_code == 0 else "failed", "exit_code": exit_code,
        "duration_ms": 1, "process_scope": "fixture", "stdout": {"complete": True, "truncated": False},
        "stderr": {"complete": True, "truncated": False, "excerpt": ""}, **(payload_change or {})}
    return CheckResult(payload, json.dumps(report, ensure_ascii=False), "")


class TeamBrowserTests(unittest.TestCase):
    setUp = test_simple_team.SimpleTeamTests.setUp
    events = test_simple_team.SimpleTeamTests.events
    run_ok = test_simple_team.SimpleTeamTests.run_ok

    def artifact(self, name="index.html", content=HTML, *, task=None, actor=None):
        payload, = normalize_deliverables([{"filename": name, "content": content, "title": name,
            "summary": "Test fixture source, not a claimed browser result", "format": "code"}])
        return self.rt.log.emit(type="deliverable.published", task_id=task or self.task,
            actor=actor or Actor.agent(self.worker, self.rt.team._role(self.worker).role), payload=payload)

    def prepare(self):
        self.rt.browser_checks.command = [sys.executable, "trusted-runner.py"]
        return {"artifact_seqs": [self.artifact().seq, self.artifact("app.js", JS).seq], "plan": copy.deepcopy(PLAN)}

    def execute(self, args, *, current=lambda: None):
        return self.rt.team.browser.execute(self.task, args, current=current)

    def runner(self, command, *, cwd, **kwargs):
        self.assertFalse(self.rt.log.db.in_transaction)
        self.assertEqual(command[-2:], ["--plan", ".cater-browser-check.json"])
        self.assertEqual(set(p.name for p in cwd.iterdir()), {"index.html", "app.js", ".cater-browser-check.json"})
        self.assertEqual((cwd / "app.js").read_text(encoding="utf-8"), JS)
        self.assertEqual((cwd / "index.html").read_text(encoding="utf-8"), HTML)
        self.assertEqual(kwargs["timeout"], self.rt.browser_checks.timeout)
        self.assertTrue(kwargs["record_output"])
        self.assertNotIn("CATER_INTERN_API_KEY", kwargs["env"])
        return result_for(json.loads((cwd / ".cater-browser-check.json").read_text(encoding="utf-8")))

    def test_only_configured_checker_is_advertised_and_no_workspace_is_required(self):
        role = self.rt.team._role(self.manager)
        self.assertNotIn("browser_check", [s["name"] for s in self.rt.team._schemas(role, None)])
        self.assertEqual(self.execute({})["outcome"], "unavailable")
        self.rt.browser_checks.command = [sys.executable, "trusted.py"]
        self.assertEqual([s for s in self.rt.team._schemas(role, None) if s["name"] == "browser_check"], [browser_schema()])
        schema = browser_schema()["input_schema"]
        self.assertEqual(schema["required"], ["artifact_seqs", "plan"])
        self.assertNotIn("command", schema["properties"])
        self.assertNotIn("url", schema["properties"])

    def test_fixed_copy_real_receipt_and_no_local_publication_or_approval(self):
        args = self.prepare()
        before = len(self.rt.log.read(task_id=self.task))
        paths = []
        def run(*a, **kw):
            paths.append(kw["cwd"])
            return self.runner(*a, **kw)
        with patch("cater.team_browser.run_check", side_effect=run):
            result = self.execute(args)
        self.assertEqual((result["status"], result["outcome"], result["scope_current"]), ("ok", "passed", True))
        self.assertEqual(result["browser_report"]["steps"][1]["actual"], True)
        self.assertEqual(len(result["checked_artifacts"]), 2)
        self.assertTrue(result["artifacts_unchanged"])
        self.assertNotIn("excerpt", result["stdout"])
        self.assertNotIn("cwd", result)
        self.assertNotIn("command", result)
        self.assertFalse(paths[0].exists())
        self.assertEqual(len(self.rt.log.read(task_id=self.task)), before)
        self.assertEqual(self.rt.team.view(self.task)["status"], "running")

    def test_invalid_selections_do_not_launch(self):
        args = self.prepare()
        other = self.rt.create_task("Other task")
        foreign = self.artifact(task=other)
        for seqs in ([], [True], [0], [-1], ["1"], [args["artifact_seqs"][0]] * 2, [foreign.seq], list(range(1, 130))):
            with self.subTest(seqs=seqs[:3]), patch("cater.team_browser.run_check") as run:
                response = self.execute({**args, "artifact_seqs": seqs})
                self.assertEqual(response["code"], "invalid_browser_check")
                run.assert_not_called()

    def test_stale_even_identical_republication_is_not_reused(self):
        args = self.prepare()
        self.artifact()
        with patch("cater.team_browser.run_check") as run:
            self.assertEqual(self.execute(args)["status"], "error")
            run.assert_not_called()

    def test_unselected_alias_publications_are_also_ambiguous(self):
        for original, alias in (("index.html", "INDEX.html"), ("caf\u00e9.js", "cafe\u0301.js")):
            args = self.prepare()
            old = args["artifact_seqs"][0] if original == "index.html" else self.artifact(original, JS).seq
            new = self.artifact(alias, JS).seq
            for seq in (old, new):
                with self.subTest(name=original, seq=seq), patch("cater.team_browser.run_check") as run:
                    self.assertEqual(self.execute({**args, "artifact_seqs": [seq]})["status"], "error")
                    run.assert_not_called()

    def old_check(self, *, result=None, contract=None):
        source = self.rt.team.receive(self.task, "Check fixture", request_id=f"old-browser-input-{len(self.rt.log.read())}")
        run = self.rt.log.emit(type="team.run_started", task_id=self.task, actor=HOST, payload={
            "role": self.manager, "run_id": f"old-browser-{source.seq}", "message_seq": source.seq, "revision": 0, "control_seq": None})
        start = self.rt.log.emit(type="team.tool_started", task_id=self.task, actor=HOST, payload={
            **run.payload, "name": "browser_check", "call_id": f"old-check-{source.seq}", "arguments": {},
            **({"execution_contract": contract} if contract is not None else {})})
        if result is not None:
            self.rt.log.emit(type="team.tool_finished", task_id=self.task, actor=HOST, payload={
                "started_seq": start.seq, "run_id": run.payload["run_id"], "role": self.manager, "name": "browser_check", "result": result})
        self.rt.log.emit(type="team.run_finished", task_id=self.task, actor=HOST, payload={**run.payload, "status": "interrupted"})
        return start

    def test_unknown_check_blocks_new_execution_until_trusted_stopped_reconciliation(self):
        args = self.prepare()
        old = self.old_check()
        with patch("cater.team_browser.run_check") as run:
            response = self.execute(args)
            self.assertEqual(response["code"], "browser_check_pending")
            self.assertEqual(response["started_seqs"], [old.seq])
            run.assert_not_called()
        with self.assertRaises(ValueError):
            self.rt.team.recovery.reconcile_browser_check(self.task, old.seq, reason="not confirmed")
        self.rt.team.recovery.reconcile_browser_check(self.task, old.seq, reason="Operator inspected and stopped old browser processes", invocation_stopped=True)
        with patch("cater.team_browser.run_check", side_effect=self.runner):
            self.assertEqual(self.execute(args)["outcome"], "passed")
        self.assertFalse(self.rt.team._pending_operations(self.task))
        self.assertEqual(len(self.events("team.tool_reconciled")), 1)

    def test_timeout_without_contract_and_cleanup_proof_remains_unknown(self):
        self.prepare()
        for contract, result in ((None, {"status": "error", "outcome": "timeout", "invocation_stopped": True}),
            (BROWSER_CONTRACT, {"status": "error", "outcome": "timeout", "invocation_stopped": False}),
            (BROWSER_CONTRACT, {"status": "error", "outcome": "unavailable", "complete": False})):
            old = self.old_check(contract=contract, result=result)
            self.assertIn(old.seq, [r["started_seq"] for r in self.rt.team._pending_operations(self.task)])

    def test_timeout_with_current_contract_and_host_cleanup_is_settled_not_permanent_block(self):
        args = self.prepare()
        self.old_check(contract=BROWSER_CONTRACT, result={"status": "error", "outcome": "timeout", "complete": True,
            "invocation_stopped": True, "process_scope": "windows_job", "stdout": {"complete": True}, "stderr": {"complete": True}})
        self.assertFalse(self.rt.team._pending_operations(self.task))
        with patch("cater.team_browser.run_check", side_effect=self.runner):
            self.assertEqual(self.execute(args)["outcome"], "passed")

    def test_untrusted_publications_and_content_hash_mismatch_rejected(self):
        args = self.prepare()
        forged = self.artifact("forged.html", actor=Actor.system("team"))
        original = self.artifact("bad.html")
        corrupted = self.rt.log.emit(type="deliverable.published", task_id=self.task, actor=original.actor,
            payload={**original.payload, "content": "different source"})
        for seq in (forged.seq, corrupted.seq):
            with self.subTest(seq=seq), patch("cater.team_browser.run_check") as run:
                self.assertEqual(self.execute({**args, "artifact_seqs": [seq]})["status"], "error")
                run.assert_not_called()

    def test_unsafe_duplicate_names_and_missing_assertion_rejected(self):
        args = self.prepare()
        for name in ("INDEX.html", "asset%20.js", ".hidden.js", "caf\u00e9.js"):
            refs = [*args["artifact_seqs"], self.artifact(name, JS).seq]
            if name.startswith("caf"):
                refs.append(self.artifact("cafe\u0301.js", JS).seq)
            with self.subTest(name=name), patch("cater.team_browser.run_check") as run:
                self.assertEqual(self.execute({**args, "artifact_seqs": refs})["status"], "error")
                run.assert_not_called()
        for plan in ({"entrypoint": "../index.html", "steps": PLAN["steps"]},
                     {**PLAN, "steps": PLAN["steps"][:1]}, {**PLAN, "files": ["injected.html"]},
                     {**PLAN, "steps": [{"action": "shell", "label": "bad", "command": "no"}]}):
            with self.subTest(plan=plan), patch("cater.team_browser.run_check") as run:
                self.assertEqual(self.execute({**args, "plan": plan})["status"], "error")
                run.assert_not_called()

    def test_model_cannot_supply_host_command_or_timeout(self):
        args = self.prepare()
        for key in ("command", "timeout", "cwd", "env", "url", "files"):
            with self.subTest(key=key), patch("cater.team_browser.run_check") as run:
                self.assertEqual(self.execute({**args, key: "untrusted"})["status"], "error")
                run.assert_not_called()

    def test_control_pause_and_pending_new_message_stop_before_execution(self):
        args = self.prepare()
        with patch("cater.team_browser.run_check") as run:
            with self.assertRaises(WorkConflict):
                self.execute(args, current=lambda: (_ for _ in ()).throw(WorkConflict("new input")))
            run.assert_not_called()
        self.rt.execution_control.change(self.task, "pause", request_id="browser-pause-fixture")
        with patch("cater.team_browser.run_check") as run, self.assertRaises(WorkConflict):
            self.execute(args)
        run.assert_not_called()

    def test_runtime_clears_environment_injections_and_credentials(self):
        with patch.dict(os.environ, {"CATER_INTERN_API_KEY": "never-copy", "NODE_OPTIONS": "--require evil.js",
             "PYTHONPATH": "evil", "PW_TEST_CONNECT_WS_ENDPOINT": "wss://evil", "HTTPS_PROXY": "evil",
             "PLAYWRIGHT_BROWSERS_PATH": "trusted-cache"}):
            env = browser_environment()
            for key in ("CATER_INTERN_API_KEY", "NODE_OPTIONS", "PYTHONPATH", "PW_TEST_CONNECT_WS_ENDPOINT", "HTTPS_PROXY"):
                self.assertNotIn(key, env)
            self.assertEqual(env["PLAYWRIGHT_BROWSERS_PATH"], "trusted-cache")
            self.assertEqual(env["PATH"], os.defpath)

    def test_failed_assertion_is_a_recorded_failure_not_an_invocation_error_or_success(self):
        args = self.prepare()
        def run(command, *, cwd, **kwargs):
            plan = json.loads((cwd / ".cater-browser-check.json").read_text(encoding="utf-8"))
            return result_for(plan, outcome="failed", exit_code=1, report_change={"error": "Counter still 0"})
        with patch("cater.team_browser.run_check", side_effect=run):
            result = self.execute(args)
        self.assertEqual((result["status"], result["outcome"], result["exit_code"]), ("ok", "failed", 1))
        self.assertEqual(result["browser_report"]["error"], "Counter still 0")

    def test_missing_or_bad_runner_report_not_mistaken_for_success(self):
        args = self.prepare()
        for outcome, exit_code, change in (("unavailable", 2, {}), ("passed", 0, {"plan_sha256": "wrong"}),
                                           ("passed", 0, {"steps": []})):
            def run(command, *, cwd, **kwargs):
                plan = json.loads((cwd / ".cater-browser-check.json").read_text(encoding="utf-8"))
                return result_for(plan, outcome=outcome, exit_code=exit_code, report_change=change)
            with self.subTest(change=change), patch("cater.team_browser.run_check", side_effect=run):
                result = self.execute(args)
                self.assertEqual(result["outcome"], "unavailable")
                self.assertEqual(result["status"], "error")

    def test_timeout_launch_error_and_truncated_capture_not_passed(self):
        args = self.prepare()
        for change, expected in (({"outcome": "timeout"}, "timeout"), ({"outcome": "launch_error"}, "launch_error"),
                                 ({"stdout": {"complete": False, "truncated": True}}, "unavailable")):
            def run(command, *, cwd, **kwargs):
                return result_for(json.loads((cwd / ".cater-browser-check.json").read_text(encoding="utf-8")), payload_change=change)
            with self.subTest(change=change), patch("cater.team_browser.run_check", side_effect=run):
                response = self.execute(args)
                self.assertEqual(response["outcome"], expected)
                self.assertEqual(response["status"], "error")

    def test_copy_mutation_invalidates_attestation(self):
        args = self.prepare()
        def run(*a, **kw):
            result = self.runner(*a, **kw)
            (kw["cwd"] / "app.js").write_text("changed", encoding="utf-8")
            return result
        with patch("cater.team_browser.run_check", side_effect=run):
            result = self.execute(args)
        self.assertFalse(result["artifacts_unchanged"])
        self.assertEqual(result["outcome"], "unavailable")
        self.assertEqual(self.rt.task(self.task).deliverables[-1]["content"], JS)

    def test_target_control_and_publication_changes_keep_but_mark_historical_receipt(self):
        for change in ("objective", "pause", "publication", "new_input"):
            args = self.prepare()
            count = 0
            def current():
                nonlocal count
                count += 1
                if change == "new_input" and count > 1:
                    raise WorkConflict("New customer message")
            def run(*a, **kw):
                result = self.runner(*a, **kw)
                if change == "objective":
                    self.rt.log.emit(type="team.objective_updated", task_id=self.task, actor=HOST, payload={"goal": "New goal"})
                elif change == "pause":
                    self.rt.execution_control.change(self.task, "pause", request_id="browser-pause-during-check")
                elif change == "publication":
                    self.artifact("app.js", "Changed later")
                return result
            with self.subTest(change=change), patch("cater.team_browser.run_check", side_effect=run):
                result = self.execute(args, current=current)
            self.assertEqual(result["outcome"], "passed")
            self.assertFalse(result["scope_current"])
            self.assertIn("历史记录", result["note"])
            if change == "pause":
                self.rt.execution_control.change(self.task, "resume", request_id="browser-resume-next-case")

    def test_native_tool_call_round_trip_and_readable_receipts(self):
        args = self.prepare()
        self.driver.steps[self.manager] = [test_simple_team.turn("我先检查实际网页。", ("browser_check", args)),
            test_simple_team.turn("这次点击与计数断言通过；没有声称覆盖所有业务。", ("read_receipts", {"name": "browser_check"})),
            test_simple_team.turn("已保留检查记录，不自动认定交付。")]
        self.rt.team.receive(self.task, "请实际检查网页", request_id="browser-tool-roundtrip")
        with patch("cater.team_browser.run_check", side_effect=self.runner):
            self.run_ok()
        receipt = next(e for e in self.events("team.tool_finished") if e.payload["name"] == "browser_check")
        self.assertEqual(receipt.payload["result"]["outcome"], "passed")
        self.assertTrue(receipt.payload["result"]["scope_current"])
        self.assertEqual(self.driver.calls[-1]["history"][-1]["role"], "tool")
        self.assertIn("browser_report", self.driver.calls[-1]["history"][-1]["content"])
        self.assertEqual(self.events("team.review_recorded"), [])
        self.assertEqual(self.events("team.objective_completed"), [])

    def test_real_subprocess_uses_restricted_env_and_fixed_inputs(self):
        args = self.prepare()
        runner = self.root / "fixture_runner.py"
        runner.write_text("import os,json,hashlib\nfrom pathlib import Path\n"
            "assert 'CATER_INTERN_API_KEY' not in os.environ\n"
            "assert Path('app.js').read_text(encoding='utf-8').startswith('document.querySelector')\n"
            "p=json.loads(Path('.cater-browser-check.json').read_text(encoding='utf-8'))\n"
            "print(json.dumps({'outcome':'passed','phase':'execution','counts':{'page_errors':0},"
            "'plan_sha256':hashlib.sha256(json.dumps(p,ensure_ascii=False).encode()).hexdigest(),"
            "'steps':[dict(index=i,action=s['action'],label=s['label'],status='passed',"
            "**({'actual':True} if s['action'].startswith('assert_') else {})) for i,s in enumerate(p['steps'],1)]}))\n", encoding="utf-8")
        self.rt.browser_checks.command = [sys.executable, str(runner)]
        with patch.dict(os.environ, {"CATER_INTERN_API_KEY": "not-forwarded"}):
            result = self.execute(args)
        self.assertEqual(result["outcome"], "passed", result)
        self.assertEqual(result["exit_code"], 0)
        self.assertTrue(result["stdout"]["complete"])
        self.assertNotIn("not-forwarded", json.dumps(result))


class BrowserHandoffTests(unittest.TestCase):
    setUp = test_simple_team.SimpleTeamTests.setUp
    artifact = test_team_handoff.TeamHandoffTests.artifact
    refs = test_team_handoff.TeamHandoffTests.refs
    handoff = test_team_handoff.TeamHandoffTests.handoff
    normalize = test_team_handoff.TeamHandoffTests.normalize
    receipt = test_team_handoff.TeamHandoffTests.receipt

    def browser_receipt(self, *, changes=None):
        artifact = self.artifact(filename="index.html", content=HTML)
        source = {"seq": artifact.seq, "filename": "index.html", "content_sha256": artifact.payload["content_sha256"]}
        return self.receipt(name="browser_check", result={"status": "ok", "check_kind": "browser", "outcome": "passed",
            "scope_current": True, "artifacts_unchanged": True, "checked_artifacts": [source], "exit_code": 0,
            "browser_report": {"outcome": "passed", "phase": "execution", "steps": [
                {"index": 1, "action": "assert_js", "status": "passed", "actual": True, "label": "Plan expectation, not observed"}]},
            **(changes or {})})

    def test_current_browser_observation_citable_without_plan_expectation(self):
        receipt = self.browser_receipt()
        quote = '{"index": 1, "action": "assert_js", "status": "passed", "actual": true}'
        normalized = self.normalize(self.handoff([{"source_seq": receipt.seq, "quote": quote}]))
        self.assertEqual(normalized["evidence"][0]["status"], "passed")
        for quote in ("Plan expectation, not observed", "Some invented actual result"):
            with self.subTest(quote=quote), self.assertRaises(ValueError):
                self.normalize(self.handoff([{"source_seq": receipt.seq, "quote": quote}]))

    def test_valid_runner_text_observation_is_citable_not_only_boolean(self):
        receipt = self.browser_receipt(changes={"browser_report": {"outcome": "passed", "phase": "execution", "steps": [
            {"index": 1, "action": "assert_text", "status": "passed", "actual": True, "observed": "Counter is 1", "label": "expected-text"}]}})
        result = self.normalize(self.handoff([{"source_seq": receipt.seq, "quote": "Counter is 1"}]))
        self.assertEqual(result["evidence"][0]["quote"], "Counter is 1")
        with self.assertRaises(ValueError):
            self.normalize(self.handoff([{"source_seq": receipt.seq, "quote": "expected-text"}]))

    def test_alias_added_after_check_invalidates_browser_citation(self):
        receipt = self.browser_receipt()
        self.artifact(filename="INDEX.html", content=HTML)
        with self.assertRaises(ValueError):
            self.normalize(self.handoff([{"source_seq": receipt.seq, "quote": '"actual": true'}]))

    def test_stale_scope_copy_or_artifact_not_in_delivery_not_citable(self):
        for change in ({"scope_current": False}, {"artifacts_unchanged": False}, {"checked_artifacts": []},
                       {"checked_artifacts": [{"seq": 1, "filename": "index.html", "content_sha256": "wrong"}]}):
            receipt = self.browser_receipt(changes=change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.normalize(self.handoff([{"source_seq": receipt.seq, "quote": '"actual": true'}]))

    def test_failed_browser_observation_preserves_failed_state(self):
        receipt = self.browser_receipt(changes={"outcome": "failed", "exit_code": 1,
            "browser_report": {"outcome": "failed", "phase": "execution", "error": "Observed counter stayed zero"}})
        normalized = self.normalize(self.handoff([{"source_seq": receipt.seq, "quote": "Observed counter stayed zero"}]))
        self.assertEqual(normalized["evidence"][0]["status"], "failed")
        self.assertEqual(normalized["evidence"][0]["exit_code"], 1)


if __name__ == "__main__":
    unittest.main()
