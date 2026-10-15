from __future__ import annotations

import copy
import hashlib
import io
import json
import sys
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from cater.anthropic import AnthropicMessagesDriver
from cater.artifacts import normalize_deliverables
from cater.browser_checks import BrowserChecks, PLAN_FILE
from cater.delivery import artifact_capability
from cater.drivers import DriverResponse
from cater.events import Actor
from cater.runtime import Runtime
from cater.verification import CheckResult
from cater.work import WorkConflict, digest
from test_delivery import DeliveryDriver
from test_work import WorkDriver, item


HTML = '<!doctype html><button id="save">Save</button><p id="result">Ready</p>'
PLAN = {"entrypoint": "index.html", "steps": [
    {"action": "click", "label": "Activate save", "selector": "#save"},
    {"action": "assert_text", "label": "Observe result", "selector": "#result", "text": "Ready"},
]}


class ArtifactCapabilityTests(unittest.TestCase):
    def test_file_types_cannot_be_downgraded_by_presentation_format(self):
        for filename in ("main.py", "MAIN.PY", "run.ps1", "app.tsx", "package.json", "Makefile", "unknown.bin"):
            for format in ("text", "markdown", "code"):
                with self.subTest(filename=filename, format=format):
                    self.assertEqual(artifact_capability({"filename": filename, "content": "payload", "format": format}), "manual")
        for filename in ("index.html", "INDEX.HTM", "app.js", "style.css", "image.svg"):
            self.assertEqual(artifact_capability({"filename": filename, "content": "payload", "format": "text"}), "browser")

    def test_raw_executable_content_cannot_hide_in_text_or_missing_names(self):
        for content in ("print('hello')", "#!/bin/sh\necho hello", HTML, "const answer = 42;",
                        "def main():\n    return 42\n", "assert 1 + 1 == 2", "import os\n"):
            for filename in (None, "notes.txt", "notes.md"):
                with self.subTest(content=content, filename=filename):
                    self.assertEqual(artifact_capability({"filename": filename, "content": content, "format": "text"}), "manual")

    def test_documents_and_data_remain_reviewable_without_execution(self):
        for filename, content in (("readme.md", "# Notes\n\nExample:\n```python\nprint('demo')\n```"),
                                  ("script-example.md", "# Notes\n\n```js\nconst count = 3;\n```"),
                                  ("notes.txt", "A plain language report."), ("data.json", '{"count": 3}')):
            self.assertEqual(artifact_capability({"filename": filename, "content": content, "format": "text"}), "document")
        self.assertEqual(artifact_capability({"filename": "notes.txt", "content": "Unidentified code", "format": "code"}), "manual")
        self.assertEqual(artifact_capability({"filename": "server.js", "content": "const fs = require('fs');", "format": "text"}), "manual")


class DeliveryBehaviorCapabilityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.db = self.root / "delivery.db"
        self.roles = self.root / "roles.json"
        config = json.loads(Path("cater/packs/default_roles.json").read_text(encoding="utf-8"))
        config["organization"]["leader"]["driver"] = "final"
        self.roles.write_text(json.dumps(config), encoding="utf-8")
        self.config = self.root / "browser.json"
        self.command = [sys.executable, str(self.root / "configured-runner.py")]
        self.config.write_text(json.dumps({"command": self.command, "timeout": 12}), encoding="utf-8")
        self.worker = WorkDriver()
        self.api = AnthropicMessagesDriver("final", endpoint="https://fixture.invalid", model="fixture", api_key_env="UNUSED_FIXTURE_KEY")
        self.drivers = {**{name: self.worker for name in ("dev", "test", "product", "design")}, "final": self.api}
        self.rt = Runtime(self.db, roles_path=self.roles, drivers=self.drivers, browser_checker_path=self.config)
        self.addCleanup(self.rt.close)
        self.final = DeliveryDriver()
        self.requests, self.executions = [], []
        self.reply = {"status": "ready", "reason": "Check the complete static delivery.", "plan": copy.deepcopy(PLAN)}
        self.plan_hook = None
        self.execute_hook = None
        self.report_hook = None
        self.exit_code = 0
        self.stream_changes = {}
        self.model = self.enterContext(patch.object(self.rt, "_complete_with_containment", side_effect=self.complete))
        self.runner = self.enterContext(patch("cater.delivery.run_check", side_effect=self.execute))

    def prepare(self, groups=None, *, prepare_bundle=True):
        groups = groups or [[("index.html", HTML, "text")]]
        self.task = self.rt.create_task("Deliver and check the complete submitted result")
        self.rt.plan_work(self.task, [item(f"part-{i}") for i in range(len(groups))])
        for i, files in enumerate(groups):
            item_id = f"part-{i}"
            claim = self.rt.work.claim(self.task, item_id)
            artifacts = []
            for filename, content, format in files:
                body = normalize_deliverables([{"title": "Result", "summary": "Submitted bytes", "filename": filename,
                                                "content": content, "format": format}])[0]
                artifacts.append(self.rt.log.emit(type="deliverable.published", task_id=self.task, actor=Actor.agent("dev", "worker"),
                    payload={**body, "work_item_id": item_id, "attempt_id": claim.payload["attempt_id"]}))
            submitted = self.rt.work.submit(self.task, claim, [a.seq for a in artifacts])
            self.rt.work.review(self.task, item_id, reviewer="test", submission_seq=submitted.seq,
                results=[{"id": "meaning", "status": "passed", "reason": "Read this submission", "receipt_seq": None,
                          "artifact_seq": artifacts[0].seq, "quote": artifacts[0].payload["content"]}])
        return (self.rt.delivery.prepare(self.task) if prepare_bundle else
                {"manifest": self.rt.delivery._manifest(self.task), "reviewer": self.rt.organization.leader.id})

    def complete(self, *, role, prompt, work_context, **kwargs):
        if work_context["phase"] == "browser_check_plan":
            self.requests.append(copy.deepcopy(work_context))
            if self.plan_hook:
                self.plan_hook()
            return DriverResponse(json.dumps(self.reply), tokens=3)
        return self.final.complete(role=role, prompt=prompt, context={"work": work_context})

    def execute(self, command, *, cwd, timeout, record_output):
        self.assertEqual(command, [*self.command, "--plan", PLAN_FILE])
        self.assertEqual(timeout, 12)
        self.assertTrue(record_output)
        plan_bytes = (cwd / PLAN_FILE).read_bytes()
        plan = json.loads(plan_bytes)
        files = {name: (cwd / name).read_text(encoding="utf-8") for name in plan["files"]}
        self.executions.append({"plan": plan, "files": files, "cwd": cwd})
        if self.execute_hook:
            self.execute_hook(cwd)
        report = {"outcome": "passed", "plan_sha256": hashlib.sha256(plan_bytes).hexdigest(),
                  "steps": [{"index": i, "action": step["action"], "status": "passed", "actual": True}
                            for i, step in enumerate(plan["steps"], 1)], "counts": {"page_errors": 0}}
        if self.report_hook:
            report = self.report_hook(report)
        output = report if isinstance(report, str) else json.dumps(report)
        stream = {"complete": True, "truncated": False, "bytes": 0, "sha256": hashlib.sha256(b"").hexdigest(), "excerpt": ""}
        return CheckResult(payload={"command": command, "exit_code": self.exit_code,
            "outcome": "passed" if self.exit_code == 0 else "failed",
            "stdout": {**stream, "bytes": len(output.encode()), "sha256": hashlib.sha256(output.encode()).hexdigest(),
                       "excerpt": output, **self.stream_changes}, "stderr": stream}, stdout_preview=output, stderr_preview="")

    def events(self, kind):
        return list(self.rt.log.iter_events(task_id=self.task, event_types=(kind,)))

    def test_all_review_main_py_labelled_text_cannot_complete_or_execute(self):
        for format in ("text", "markdown", "code"):
            with self.subTest(format=format):
                view = self.prepare([[('main.py', "print('hello')", format)]])
                self.rt.collaborate(self.task)
                self.assertTrue(self.rt.work.view(self.task)["stage_accepted"])
                self.assertEqual(self.rt.delivery.view(self.task)["status"], "pending")
                self.assertEqual(view["local_checks"][0]["id"], "integration_behavior")
                self.assertNotEqual(self.rt.task(self.task).status, "done")
                with self.assertRaises(WorkConflict):
                    self.rt.delivery.archive(self.task)
        self.model.assert_not_called()
        self.runner.assert_not_called()

    def test_document_only_completes_without_browser(self):
        self.prepare([[('report.md', "# Report\n\nReadable result.", "markdown")]])
        self.rt.collaborate(self.task)
        self.assertEqual(self.rt.delivery.view(self.task)["status"], "accepted")
        self.assertEqual(self.requests, [])
        self.runner.assert_not_called()

    def test_legacy_accepted_manifest_cannot_keep_bypassed_behavior_gate(self):
        current = self.prepare([[('main.py', 'print(1)', 'text')]], prepare_bundle=False)
        manifest = copy.deepcopy(current['manifest'])
        for file in manifest['files']:
            file.pop('capability')
        manifest['criteria'] = [c for c in manifest['criteria'] if c['kind'] == 'review']
        payload = {'manifest': manifest, 'reviewer': current['reviewer']}
        payload['digest'] = digest(payload)
        self.rt.log.emit(type='delivery.prepared', task_id=self.task, actor=self.rt._leader_actor(), payload=payload)
        self.rt.log.emit(type='delivery.review_finished', task_id=self.task,
            actor=Actor.agent(current['reviewer'], 'delivery_reviewer'), payload={
                'delivery_digest': payload['digest'], 'attempt_seq': 0, 'status': 'accepted', 'check_seqs': {}})
        self.assertEqual(self.rt.delivery.view(self.task)['status'], 'stale')
        with self.assertRaises(WorkConflict):
            self.rt.delivery.archive(self.task)
        self.assertEqual(self.rt.delivery.prepare(self.task)['local_checks'][0]['status'], 'unknown')

    def test_model_unsupported_keeps_behavior_obligation_without_execution(self):
        self.prepare()
        self.reply = {'status': 'unsupported', 'reason': 'Requires a backend outside browser capability.', 'plan': None}
        self.assertEqual(self.rt.delivery.run(self.task)['status'], 'pending')
        self.rt.delivery.run(self.task)
        self.assertEqual(len(self.requests), 1)
        self.runner.assert_not_called()

    def test_valid_execution_still_requires_independent_coverage_review(self):
        self.prepare()
        self.final.transform = lambda raw: {**raw, 'checks': [{**r, 'status': 'unknown'} for r in raw['checks']]}
        delivery = self.rt.delivery.run(self.task)
        self.assertEqual(delivery['local_checks'][0]['status'], 'passed')
        self.assertEqual(delivery['status'], 'needs_evidence')
        with self.assertRaises(WorkConflict):
            self.rt.delivery.archive(self.task)

    def test_html_automatically_checks_whole_bundle_and_exports_identical_layout(self):
        view = self.prepare([[('index.html', HTML, 'text')], [('app.js', "document.title = 'Ready';", 'text')],
                             [('notes.md', '# Notes\n\nReview the delivered page.', 'markdown')]])
        self.rt.collaborate(self.task)
        delivery = self.rt.delivery.view(self.task)
        self.assertEqual(delivery["status"], "accepted")
        self.assertEqual(self.rt.task(self.task).status, "done")
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(len(self.final.calls), 1)
        self.assertEqual(len(self.executions), 1)
        event = self.events("verification.completed")[0]
        self.assertEqual(event.payload["delivery_digest"], view["digest"])
        self.assertEqual(event.payload["criterion_id"], "integration_behavior")
        self.assertEqual(event.payload["checked_artifacts"], view["manifest"]["files"])
        self.assertEqual(self.requests[0]["manifest"], view["manifest"])
        receipt = delivery["local_checks"][0]["receipts"][0]
        self.assertEqual(receipt["browser_plan"], self.executions[0]["plan"])
        self.assertEqual(receipt["browser_report"]["outcome"], "passed")
        self.assertIn("browser_report", self.final.calls[0]["local_checks"]["integration_behavior"])
        with zipfile.ZipFile(io.BytesIO(self.rt.delivery.archive(self.task))) as archive:
            for name, body in self.executions[0]["files"].items():
                self.assertEqual(archive.read(name), body.encode())
            self.assertEqual(json.loads(archive.read("manifest.json"))["local_checks"], delivery["local_checks"])
        self.assertFalse(self.executions[0]["cwd"].exists())
        self.rt.collaborate(self.task)
        self.assertEqual(len(self.executions), 1)
        self.assertTrue(self.rt.verify_chain().ok)

    def test_no_checker_or_non_text_planner_keeps_gate_closed(self):
        self.prepare()
        self.rt.browser_checks = BrowserChecks(self.rt, None)
        self.assertEqual(self.rt.delivery.run(self.task)["status"], "pending")
        self.rt.browser_checks = BrowserChecks(self.rt, self.config)
        self.rt.drivers["final"] = self.final
        self.assertEqual(self.rt.delivery.run(self.task)["status"], "pending")
        self.model.assert_not_called()
        self.runner.assert_not_called()

    def test_mixed_executable_duplicate_reserved_and_unsafe_names_are_unsupported(self):
        variants = [
            [[('index.html', HTML, 'text')], [('main.py', 'print(1)', 'text')]],
            [[('index.html', HTML, 'text')], [('INDEX.HTML', HTML, 'text')]],
            [[('index.html', HTML, 'text'), ('manifest.json', '{}', 'text')]],
            [[('index.html', HTML, 'text'), ('handoff.md', '# Notes', 'text')]],
            [[('index.html', HTML, 'text'), ('unsafe%20.js', 'alert(1)', 'text')]],
            [[('index.html', HTML, 'text'), ('package.json', '{}', 'text')]],
        ]
        for groups in variants:
            with self.subTest(groups=groups):
                self.prepare(groups)
                self.assertEqual(self.rt.delivery.run(self.task)["status"], "pending")
                self.assertEqual(self.events("verification.planned")[-1].payload["status"], "unsupported")
        self.model.assert_not_called()
        self.runner.assert_not_called()

    def test_model_cannot_supply_host_commands_or_unlisted_entrypoints(self):
        for plan in ({**PLAN, "command": ["untrusted"]}, {**PLAN, "entrypoint": "missing.html"},
                     {**PLAN, "steps": [{"action": "exec", "command": "bad"}]},
                     {**PLAN, "steps": PLAN["steps"][:1]}):
            with self.subTest(plan=plan):
                self.prepare()
                self.reply["plan"] = plan
                self.assertEqual(self.rt.delivery.run(self.task)["status"], "pending")
                self.assertEqual(len(self.events("verification.planned")), 2)
                self.rt.delivery.run(self.task)
                self.assertEqual(len(self.events("verification.planning_started")), 1)
        self.runner.assert_not_called()

    def test_failed_or_invalid_reports_never_release_gate_or_replay(self):
        transforms = [lambda r: "not JSON", lambda r: {**r, "plan_sha256": "wrong"},
                      lambda r: {**r, "steps": []}, lambda r: {**r, "counts": {"page_errors": 1}},
                      lambda r: {**r, "steps": [{**s, "actual": False} for s in r["steps"]]},
                      lambda r: {**r, "outcome": "failed"}, lambda r: {**r, "outcome": "unavailable"}]
        for transform in transforms:
            with self.subTest(transform=transform):
                self.prepare()
                self.report_hook = transform
                self.assertEqual(self.rt.delivery.run(self.task)["status"], "pending")
                self.assertEqual(self.rt.delivery.view(self.task)["local_checks"][0]["status"], "failed")
                calls = len(self.executions)
                self.rt.delivery.run(self.task)
                self.assertEqual(len(self.executions), calls)
        self.assertEqual(self.final.calls, [])

    def test_nonzero_exit_and_incomplete_report_override_passed_report(self):
        for exit_code, stream_changes in ((1, {}), (0, {"complete": False}), (0, {"truncated": True})):
            with self.subTest(exit_code=exit_code, stream_changes=stream_changes):
                self.prepare()
                self.exit_code, self.stream_changes = exit_code, stream_changes
                self.assertEqual(self.rt.delivery.run(self.task)["local_checks"][0]["status"], "failed")
        self.assertEqual(self.final.calls, [])

    def test_artifact_mutation_fails_whole_check(self):
        self.prepare()
        self.execute_hook = lambda cwd: (cwd / 'index.html').write_text('changed', encoding='utf-8')
        self.rt.delivery.run(self.task)
        self.assertFalse(self.events("verification.completed")[0].payload["artifacts_unchanged"])
        self.assertEqual(self.rt.delivery.view(self.task)["local_checks"][0]["status"], "failed")
        self.assertEqual(self.final.calls, [])

    def test_direction_change_during_plan_does_not_execute_old_bundle(self):
        self.prepare()
        self.plan_hook = lambda: self.rt.command(self.task, "Change the output requirement")
        self.assertEqual(self.rt.delivery.run(self.task)["status"], "stale")
        self.assertEqual(self.events("verification.planned")[-1].payload["status"], "stale")
        self.runner.assert_not_called()

    def test_direction_change_during_execution_never_accepts_stale_receipt(self):
        self.prepare()
        self.execute_hook = lambda cwd: self.rt.command(self.task, "Change the output requirement")
        self.assertEqual(self.rt.delivery.run(self.task)["status"], "stale")
        self.assertEqual(len(self.events("verification.completed")), 1)
        self.assertEqual(self.final.calls, [])

    def test_pending_or_expired_check_prevents_automatic_planning(self):
        view = self.prepare()
        self.rt.delivery._start_check(self.task, view, "integration_behavior", 1)
        self.assertEqual(self.rt.delivery.run(self.task)["status"], "checking")
        with patch("cater.delivery.time.time", return_value=10**12):
            self.assertEqual(self.rt.delivery.run(self.task)["status"], "uncertain")
        self.model.assert_not_called()
        self.runner.assert_not_called()

    def test_interrupted_planning_survives_restart_without_replay(self):
        self.prepare()
        self.plan_hook = lambda: (_ for _ in ()).throw(RuntimeError("interrupted"))
        with self.assertRaisesRegex(RuntimeError, "interrupted"):
            self.rt.delivery.run(self.task)
        with Runtime(self.db, roles_path=self.roles, drivers=self.drivers, browser_checker_path=self.config) as reopened:
            with patch.object(reopened, "_complete_with_containment") as model:
                self.assertEqual(reopened.delivery.run(self.task)["status"], "pending")
                model.assert_not_called()
        self.runner.assert_not_called()

    def test_concurrent_delivery_runs_plan_and_execute_once(self):
        self.prepare()
        entered, release = threading.Event(), threading.Event()
        errors = []

        def block():
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test did not release planning")

        def run():
            try:
                with Runtime(self.db, roles_path=self.roles, drivers=self.drivers, browser_checker_path=self.config) as runner:
                    with patch.object(runner, "_complete_with_containment", side_effect=self.complete):
                        runner.delivery.run(self.task)
            except Exception as exc:
                errors.append(exc)

        self.plan_hook = block
        thread = threading.Thread(target=run)
        thread.start()
        try:
            self.assertTrue(entered.wait(5))
            with Runtime(self.db, roles_path=self.roles, drivers=self.drivers, browser_checker_path=self.config) as other:
                with patch.object(other, "_complete_with_containment") as model:
                    self.assertEqual(other.delivery.run(self.task)["status"], "pending")
                    model.assert_not_called()
        finally:
            release.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(self.executions), 1)
        self.assertEqual(self.rt.delivery.view(self.task)["status"], "accepted")

    def test_direct_browser_verification_rejects_wrong_command_and_digest(self):
        view = self.prepare()
        with self.assertRaises(ValueError):
            self.rt.delivery.verify(self.task, "integration_behavior", [sys.executable, '-c', 'pass'],
                                    browser_plan=PLAN, expected_digest=view["digest"])
        with self.assertRaises(WorkConflict):
            self.rt.delivery.verify(self.task, "integration_behavior", [*self.command, '--plan', PLAN_FILE],
                                    browser_plan=PLAN, expected_digest='old')
        self.assertEqual(self.events("delivery.check_started"), [])
        self.runner.assert_not_called()

    def test_pause_and_cancel_block_manual_and_browser_checks_before_claim(self):
        for action in ("pause", "cancel"):
            with self.subTest(action=action):
                view = self.prepare()
                self.rt.execution_control.change(self.task, action, request_id=f"{action}-{self.task}")
                for kwargs in ({}, {"browser_plan": PLAN, "expected_digest": view["digest"]}):
                    with self.assertRaises(WorkConflict):
                        self.rt.delivery.verify(self.task, "integration_behavior", [*self.command, '--plan', PLAN_FILE], **kwargs)
                self.assertEqual(self.events("delivery.check_started"), [])
        self.runner.assert_not_called()

    def test_pause_during_file_preparation_records_not_started_receipt(self):
        view = self.prepare()
        write = Path.write_bytes

        def pause_after_write(path, data):
            result = write(path, data)
            if path.name == PLAN_FILE:
                self.rt.execution_control.change(self.task, "pause", request_id=f"copy-pause-{self.task}")
            return result

        with patch.object(Path, "write_bytes", pause_after_write):
            event, _ = self.rt.delivery.verify(self.task, "integration_behavior", [*self.command, '--plan', PLAN_FILE],
                browser_plan=PLAN, expected_digest=view["digest"])
        self.assertIs(event.payload["invocation_started"], False)
        self.assertEqual(event.payload["outcome"], "unavailable")
        self.assertFalse(self.rt.delivery.view(self.task)["check_in_flight"])
        check = self.rt.delivery.view(self.task)["local_checks"][0]
        self.assertEqual(check["status"], "failed")
        self.assertIs(check['receipts'][0]['invocation_started'], False)
        self.assertEqual(check['receipts'][0]['browser_report']['reason'], event.payload['reason'])
        self.runner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
