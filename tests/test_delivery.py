from __future__ import annotations

import copy
import io
import json
import sys
import subprocess
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from cater.delivery import DeliveryService
from cater.drivers import DriverResponse
from cater.events import Actor
from cater.runtime import Runtime
from cater.work import WorkConflict
from test_work import WorkDriver, item
from test_runtime import start_test_web


class DeliveryDriver:
    def __init__(self):
        self.calls = []
        self.after = None
        self.transform = None

    def complete(self, *, role, prompt, context):
        work = context["work"]
        self.calls.append(copy.deepcopy(work))
        if self.after:
            self.after(work)
        artifact = work["artifacts"][0]
        def row(row_id):
            return {"id": row_id, "status": "passed", "reason": "核对了这次提交的实际正文。",
                    "evidence": [{"artifact_seq": artifact["seq"], "quote": artifact["content"]}],
                    "superseded_by": None, "source_quote": None}
        raw = {"requirements": [row(s["id"]) for s in work["manifest"]["sources"]],
               "checks": [row(c["id"]) for c in work["manifest"]["criteria"] if c["kind"] == "review"],
               "handoff": {"summary": "已整理当前版本正文。", "why": "先独立验收，再核对组合与要求。",
                           "reproduce": ["打开交付清单，逐条核对正文引用。"], "limits": ["引用不证明程序已经运行。"]}}
        if self.transform:
            raw = self.transform(raw)
        return DriverResponse(text=json.dumps(raw), tokens=13, meta={"token_usage": "reported"})


class DocumentWorkDriver(WorkDriver):
    def complete(self, *, role, prompt, context):
        response = super().complete(role=role, prompt=prompt, context=context)
        work = context["work"]
        if work.get("phase") != "review" and not any(c["kind"] == "local_check" for c in work["contract"]["criteria"]):
            raw = json.loads(response.text)
            raw["deliverables"][0].update(content="# 离线文档成果\n\n保留逐条复查依据。\n", format="markdown", filename="result.md")
            return DriverResponse(json.dumps(raw), response.tokens, response.meta)
        return response


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "delivery.db"
        self.roles = Path(self.tmp.name) / "roles.json"
        config = json.loads(Path("cater/packs/default_roles.json").read_text(encoding="utf-8"))
        config["organization"]["leader"]["driver"] = "final"
        self.roles.write_text(json.dumps(config), encoding="utf-8")
        self.worker, self.final = DocumentWorkDriver(), DeliveryDriver()
        self.drivers = {**{name: self.worker for name in ("product", "design", "dev", "test")}, "final": self.final}
        self.rt = Runtime(self.db, roles_path=self.roles, drivers=self.drivers)
        self.addCleanup(self.rt.close)
        self.task = self.rt.create_task("整理当前任务成果", ["可复查且不冒充真实模型评测"])

    def prepare(self, *, local=False, two=False):
        self.rt.plan_work(self.task, [item("a", local=local)] + ([item("b", deps=["a"])] if two else []))
        self.rt.collaborate(self.task)
        if local:
            self.rt.verify_work(self.task, "a", "behavior", [sys.executable, "check.py"])
            self.rt.collaborate(self.task)
        return self.rt.delivery.view(self.task)

    def test_stage_checks_alone_do_not_complete_a_code_task(self):
        view = self.prepare(local=True)
        self.assertTrue(self.rt.work.view(self.task)["stage_accepted"])
        self.assertEqual(view["status"], "pending")
        self.assertEqual(view["local_checks"][0]["status"], "unknown")
        self.assertNotEqual(self.rt.task(self.task).status, "done")
        self.assertEqual(self.final.calls, [])
        with self.assertRaises(WorkConflict):
            self.rt.delivery.archive(self.task)

    def test_readable_delivery_completes_once_and_exports_exact_selected_content(self):
        view = self.prepare(two=True)
        self.assertEqual(view["status"], "accepted")
        self.assertEqual(self.rt.task(self.task).status, "done")
        self.assertEqual(self.rt.tasks()[0].status, "done")
        self.assertEqual(len(view["requirements"]), 2)
        self.assertEqual(len(self.final.calls), 1)
        self.rt.collaborate(self.task)
        self.rt.command(self.task, "进度怎么样")
        self.assertEqual(len(self.final.calls), 1)
        progress = self.rt.log.read(task_id=self.task)[-1].payload
        self.assertEqual(progress["checkpoint"]["state"], "accepted")
        self.assertIn("整体验收已通过", progress["text"])
        with zipfile.ZipFile(io.BytesIO(self.rt.delivery.archive(self.task))) as archive:
            self.assertEqual(archive.read("work-1/result.md"), "# 离线文档成果\n\n保留逐条复查依据。\n".encode())
            self.assertEqual(archive.read("work-2/result.md"), "# 离线文档成果\n\n保留逐条复查依据。\n".encode())
            manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(manifest["digest"], view["digest"])
            self.assertIn("为什么这样做", archive.read("handoff.md").decode())
            self.assertNotIn("api_key", archive.read("manifest.json").decode())
        with Runtime(self.db, roles_path=self.roles, drivers=self.drivers) as reopened:
            self.assertEqual(reopened.task(self.task).status, "done")
            self.assertEqual(reopened.delivery.view(self.task)["digest"], view["digest"])

    def test_whole_behavior_receipt_is_required_and_bound_to_current_bundle(self):
        view = self.prepare(local=True)
        event, result = self.rt.delivery.verify(self.task, "integration_behavior", [sys.executable, "work-1/check.py"])
        self.assertEqual(result.payload["outcome"], "passed")
        self.assertEqual(event.payload["delivery_digest"], view["digest"])
        self.assertIsInstance(event.payload["check_attempt_seq"], int)
        self.rt.collaborate(self.task)
        self.assertEqual(self.rt.delivery.view(self.task)["status"], "accepted")
        self.assertTrue(self.rt.verify_chain().ok)

    def test_handoff_exports_actual_checks_independently_of_model_instructions(self):
        self.prepare(local=True)
        command = [sys.executable, "-c", "import runpy; runpy.run_path('work-1/check.py'); print('actual-check')"]
        event, result = self.rt.delivery.verify(self.task, "integration_behavior", command, record_output=True)
        self.final.transform = lambda raw: {**raw, "handoff": {**raw["handoff"], "reproduce": ["MODEL_STEP_NOT_EXECUTED"]}}
        self.rt.collaborate(self.task)
        delivery = self.rt.delivery.view(self.task)
        receipt = delivery["local_checks"][0]["receipts"][0]
        self.assertEqual(receipt["command"], command)
        self.assertEqual(receipt["receipt_seq"], event.seq)
        self.assertEqual(receipt["attempt_seq"], event.payload["check_attempt_seq"])
        self.assertEqual(receipt["stdout"], result.payload["stdout"])
        self.assertIn("actual-check", receipt["stdout"]["excerpt"])
        self.assertNotIn("cwd", receipt)
        with zipfile.ZipFile(io.BytesIO(self.rt.delivery.archive(self.task))) as archive:
            exported = json.loads(archive.read("manifest.json"))["local_checks"][0]["receipts"][0]
            self.assertEqual(exported, receipt)
            notes = archive.read("handoff.md").decode("utf-8")
            self.assertIn("MODEL_STEP_NOT_EXECUTED", notes)
            actual = notes.split("## 实际执行的整体检查", 1)[1]
            self.assertNotIn("MODEL_STEP_NOT_EXECUTED", actual)
            self.assertIn(json.dumps(command, ensure_ascii=False, indent=2), actual)
            self.assertIn(receipt["stdout"]["sha256"], actual)
            self.assertIn("原临时目录已清理", actual)
        with Runtime(self.db, roles_path=self.roles, drivers=self.drivers) as reopened:
            self.assertEqual(reopened.delivery.view(self.task)["local_checks"], delivery["local_checks"])

    def test_check_receipts_keep_output_text_opt_in(self):
        self.prepare(local=True)
        command = [sys.executable, "-c", "import runpy; runpy.run_path('work-1/check.py'); print('check-output')"]
        event, _ = self.rt.delivery.verify(self.task, "integration_behavior", command)
        self.rt.collaborate(self.task)
        receipt = self.rt.delivery.view(self.task)["local_checks"][0]["receipts"][0]
        for stream in ("stdout", "stderr"):
            self.assertNotIn("excerpt", receipt[stream])
            self.assertEqual(receipt[stream], event.payload[stream])
        self.assertGreater(receipt["stdout"]["bytes"], 0)
        with zipfile.ZipFile(io.BytesIO(self.rt.delivery.archive(self.task))) as archive:
            exported = json.loads(archive.read("manifest.json"))["local_checks"][0]["receipts"][0]
            self.assertNotIn("excerpt", exported["stdout"])

    def test_current_check_wave_exposes_all_receipts_without_hiding_failures(self):
        self.prepare(local=True)
        old, _ = self.rt.delivery.verify(self.task, "integration_behavior", [sys.executable, "work-1/check.py"])
        current = self.rt.delivery.view(self.task)
        first = self.rt.delivery._start_check(self.task, current, "integration_behavior", 60)
        second = self.rt.delivery._start_check(self.task, current, "integration_behavior", 60)
        base = {"delivery_digest": current["digest"], "criterion_id": "integration_behavior", "artifacts_unchanged": True,
                "stdout": {"complete": True}, "stderr": {"complete": True}}
        failure = self.rt.log.emit(type="verification.completed", actor=Actor.system("local_verifier"), task_id=self.task,
            payload={**base, "check_attempt_seq": first.seq, "outcome": "failed", "exit_code": 1})
        pending = self.rt.delivery.view(self.task)
        self.assertEqual(pending["status"], "checking")
        self.assertEqual([r["receipt_seq"] for r in pending["local_checks"][0]["receipts"]], [failure.seq])
        success = self.rt.log.emit(type="verification.completed", actor=Actor.system("local_verifier"), task_id=self.task,
            payload={**base, "check_attempt_seq": second.seq, "outcome": "passed", "exit_code": 0})
        self.rt.log.emit(type="verification.completed", actor=Actor.system("local_verifier"), task_id=self.task,
            payload={**base, "check_attempt_seq": first.seq, "outcome": "passed", "exit_code": 0})
        check = self.rt.delivery.view(self.task)["local_checks"][0]
        self.assertEqual(check["status"], "failed")
        self.assertEqual([r["receipt_seq"] for r in check["receipts"]], [failure.seq, success.seq])
        self.assertNotIn(old.seq, [r["receipt_seq"] for r in check["receipts"]])
        self.assertNotIn("command", check["receipts"][0])
        replacement, _ = self.rt.delivery.verify(self.task, "integration_behavior", [sys.executable, "work-1/check.py"])
        check = self.rt.delivery.view(self.task)["local_checks"][0]
        self.assertEqual(check["status"], "passed")
        self.assertEqual([r["receipt_seq"] for r in check["receipts"]], [replacement.seq])

    def test_receipt_details_ignore_model_claims_and_wrong_attempt_references(self):
        self.prepare(local=True)
        current = self.rt.delivery.view(self.task)
        started = self.rt.delivery._start_check(self.task, current, "integration_behavior", 60)
        base = {"delivery_digest": current["digest"], "criterion_id": "integration_behavior",
                "check_attempt_seq": started.seq, "outcome": "passed", "exit_code": 0,
                "artifacts_unchanged": True, "stdout": {"complete": True}, "stderr": {"complete": True},
                "command": ["MODEL_CLAIM_NOT_A_CHECK"]}
        for actor, override in ((Actor.agent("dev", "worker"), {}),
                                (Actor.system("local_verifier"), {"delivery_digest": "wrong-digest"}),
                                (Actor.system("local_verifier"), {"criterion_id": "wrong-criterion"}),
                                (Actor.system("local_verifier"), {"check_attempt_seq": 99999})):
            self.rt.log.emit(type="verification.completed", actor=actor, task_id=self.task, payload={**base, **override})
        check = self.rt.delivery.view(self.task)["local_checks"][0]
        self.assertEqual(check["status"], "unknown")
        self.assertEqual(check["receipts"], [])

    def test_document_handoff_does_not_invent_behavior_check_commands(self):
        self.prepare()
        with zipfile.ZipFile(io.BytesIO(self.rt.delivery.archive(self.task))) as archive:
            self.assertEqual(json.loads(archive.read("manifest.json"))["local_checks"], [])
            self.assertIn("没有整体行为检查回执", archive.read("handoff.md").decode("utf-8"))

    def test_code_output_cannot_skip_behavior_gate_when_planning_omits_local_check(self):
        raw_worker = WorkDriver()
        with Runtime(":memory:", roles_path=self.roles, drivers={**{name: raw_worker for name in ("product", "design", "dev", "test")}, "final": self.final}) as rt:
            task = rt.create_task("交付能运行的代码")
            rt.plan_work(task, [item("code-with-missing-check")]); rt.collaborate(task)
            self.assertTrue(rt.work.view(task)["stage_accepted"])
            delivery = rt.delivery.view(task)
            self.assertEqual(delivery["status"], "pending")
            self.assertEqual(delivery["local_checks"][0]["id"], "integration_behavior")
            self.assertEqual(self.final.calls, [])
            self.assertNotEqual(rt.task(task).status, "done")

    def test_missing_requirement_or_fabricated_quote_cannot_accept(self):
        for mutation in (
            lambda raw: {**raw, "requirements": raw["requirements"][:-1]},
            lambda raw: {**raw, "requirements": [{**r, "evidence": [{"artifact_seq": 99999, "quote": "invented"}]} for r in raw["requirements"]]},
            lambda raw: {**raw, "checks": [{**r, "status": "passed", "evidence": []} for r in raw["checks"]]},
        ):
            with self.subTest(mutation=mutation):
                self.final.transform = mutation
                self.rt.plan_work(self.task, [item("a")])
                self.rt.collaborate(self.task)
                self.assertEqual(self.rt.delivery.view(self.task)["status"], "invalid")
                self.assertNotEqual(self.rt.task(self.task).status, "done")

    def test_unknown_semantic_coverage_remains_a_checkpoint(self):
        self.final.transform = lambda raw: {**raw, "requirements": [{**r, "status": "unknown", "evidence": []} for r in raw["requirements"]]}
        view = self.prepare()
        self.assertEqual(view["status"], "needs_evidence")
        calls = len(self.final.calls)
        self.rt.collaborate(self.task)
        self.assertEqual(len(self.final.calls), calls)
        self.rt.command(self.task, "继续")
        self.rt.collaborate(self.task)
        self.assertEqual(len(self.final.calls), calls + 1)

    def test_failed_integration_reworks_only_cited_outputs_and_dependents(self):
        self.final.transform = lambda raw: {**raw, "checks": [{**c, "status": "failed"} for c in raw["checks"]]}
        self.prepare(two=True)
        statuses = {i["id"]: i["status"] for i in self.rt.work.view(self.task)["items"]}
        self.assertEqual(statuses, {"a": "ready", "b": "blocked"})
        self.assertEqual(self.rt.delivery.view(self.task)["status"], "stale")
        self.assertNotEqual(self.rt.task(self.task).status, "done")

    def test_direction_rules_and_replacement_plan_revoke_delivery(self):
        for change in (
            lambda: self.rt.command(self.task, "另加一个要求"),
            lambda: self.rt.rulebook.record("禁止虚构用户经历", scope="task", task_id=self.task),
            lambda: self.rt.plan_work(self.task, [item("a")]),
        ):
            with self.subTest(change=change):
                self.rt.plan_work(self.task, [item("a")])
                self.rt.collaborate(self.task)
                self.assertEqual(self.rt.delivery.view(self.task)["status"], "accepted")
                change()
                self.assertEqual(self.rt.delivery.view(self.task)["status"], "stale")
                self.assertNotEqual(self.rt.task(self.task).status, "done")
                with self.assertRaises(WorkConflict):
                    self.rt.delivery.archive(self.task)

    def test_change_during_review_cannot_accept_old_response(self):
        self.final.after = lambda _: self.rt.command(self.task, "复核中新增的要求")
        self.prepare()
        self.assertEqual(self.rt.delivery.view(self.task)["status"], "stale")
        self.assertNotEqual(self.rt.task(self.task).status, "done")

    def test_acceptance_append_rechecks_a_concurrent_rule_change(self):
        emit = self.rt.log.emit
        changed = False
        def during_append(**kwargs):
            nonlocal changed
            if kwargs["type"] == "delivery.review_finished" and not changed:
                changed = True
                self.rt.rulebook.record("复核提交时新增的店规", scope="task", task_id=self.task)
            return emit(**kwargs)
        with patch.object(self.rt.log, "emit", side_effect=during_append):
            self.prepare()
        self.assertEqual(self.rt.delivery.view(self.task)["status"], "stale")
        self.assertNotEqual(self.rt.task(self.task).status, "done")

    def test_archive_rechecks_head_after_assembling_content(self):
        self.prepare()
        files = self.rt.delivery._files
        changed = False
        def during_read(task_id, manifest):
            nonlocal changed
            result = files(task_id, manifest)
            if not changed:
                changed = True
                self.rt.command(self.task, "下载组包期间新增要求")
            return result
        with patch.object(self.rt.delivery, "_files", side_effect=during_read):
            with self.assertRaises(WorkConflict):
                self.rt.delivery.archive(self.task)

    def test_late_event_from_old_work_plan_cannot_reopen_a_valid_delivery(self):
        self.prepare()
        old = next(e for e in self.rt.log.read(task_id=self.task) if e.type == "work.claimed")
        self.rt.plan_work(self.task, [item("a")]); self.rt.collaborate(self.task)
        self.rt.log.emit(type="work.failed", task_id=self.task, actor=Actor.system("work_scheduler"),
                         payload={**old.payload, "uncertain": True, "reason": "旧版本迟到记录"})
        self.assertEqual(self.rt.task(self.task).status, "done")
        self.assertEqual(self.rt.delivery.view(self.task)["status"], "accepted")

    def test_author_cannot_be_assigned_as_whole_delivery_reviewer(self):
        with Runtime(":memory:", drivers={name: self.worker for name in ("product", "design", "dev", "test")}) as rt:
            task = rt.create_task("No final model configured")
            rt.plan_work(task, [item("a")]); rt.collaborate(task)
            self.assertEqual(rt.delivery.view(task)["status"], "pending")
            with self.assertRaisesRegex(ValueError, "作者"):
                rt.delivery.prepare(task, reviewer="dev")
            self.assertNotEqual(rt.task(task).status, "done")

    def test_old_human_done_does_not_enable_delivery_download(self):
        self.rt.decide(self.task, "done", "Legacy human acknowledgement")
        with self.assertRaises(WorkConflict):
            self.rt.delivery.archive(self.task)

    def test_human_confirmation_cannot_override_incomplete_work(self):
        self.rt.plan_work(self.task, [item("a", local=True)])
        with self.assertRaises(WorkConflict):
            self.rt.decide(self.task, "done", "Cannot skip checks")
        self.rt.collaborate(self.task)
        with self.assertRaises(WorkConflict):
            self.rt.decide(self.task, "done", "Still missing checks")

    def test_human_confirmation_is_bound_and_rechecks_scope_during_append(self):
        view = self.prepare()
        confirmed = self.rt.decide(self.task, "done", "Checked delivery")
        self.assertEqual(confirmed.payload["delivery_digest"], view["digest"])
        emit = self.rt.log.emit
        def changed(**kwargs):
            if kwargs["type"] == "human.decided":
                self.rt.command(self.task, "Do not drop this new requirement")
            return emit(**kwargs)
        with patch.object(self.rt.log, "emit", side_effect=changed):
            with self.assertRaises(WorkConflict):
                self.rt.decide(self.task, "done", "A stale confirmation")

    def test_transport_failure_is_not_replayed_even_after_resume(self):
        with patch.object(self.final, "complete", side_effect=TimeoutError("offline fixture")) as complete:
            self.assertEqual(self.prepare()["status"], "uncertain")
            self.rt.command(self.task, "继续")
            self.rt.collaborate(self.task)
            self.assertEqual(complete.call_count, 1)

    def test_human_rework_revokes_delivery_and_becomes_a_planning_requirement(self):
        view = self.prepare()
        decision = self.rt.decide(self.task, "rework", "交付内容还有遗漏，需要重新梳理")
        self.assertEqual(decision.payload["delivery_digest"], view["digest"])
        self.assertEqual(self.rt.delivery.view(self.task)["status"], "stale")
        self.assertNotEqual(self.rt.task(self.task).status, "done")
        self.assertNotEqual(self.rt.tasks()[0].status, "done")
        with self.assertRaises(WorkConflict):
            self.rt.delivery.archive(self.task)
        from cater.planning import requirement_sources
        self.assertEqual(requirement_sources(self.rt.log.read(task_id=self.task))[-1]["text"], decision.payload["reason"])
        self.assertEqual(self.rt.work.scope(self.task)["direction_seq"], decision.seq)
        self.assertEqual(self.rt.work.directions(self.task, decision.seq)[-1]["instruction"], decision.payload["reason"])

    def test_check_update_during_review_discards_old_verdict_but_allows_fresh_review(self):
        self.prepare(local=True)
        self.rt.delivery.verify(self.task, "integration_behavior", [sys.executable, "work-1/check.py"])
        def updated(_):
            self.final.after = None
            self.rt.delivery.verify(self.task, "integration_behavior", [sys.executable, "work-1/check.py"])
        self.final.after = updated
        self.assertEqual(self.rt.delivery.run(self.task)["status"], "needs_evidence")
        self.assertEqual(self.rt.delivery.run(self.task)["status"], "accepted")
        self.assertEqual(len(self.final.calls), 2)

    def test_pending_check_update_can_finish_after_old_review_and_then_recover(self):
        current = self.prepare(local=True)
        self.rt.delivery.verify(self.task, "integration_behavior", [sys.executable, "work-1/check.py"])
        started, context = self.rt.delivery._start(self.task)
        pending = self.rt.delivery._start_check(self.task, current, "integration_behavior", 60)
        raw = json.loads(self.final.complete(role="fixture", prompt="", context={"work": context}).text)
        self.rt.delivery._finish(self.task, started, context, DeliveryService._verdict(raw, context))
        self.assertEqual(self.rt.delivery.view(self.task)["status"], "checking")
        self.rt.log.emit(type="verification.completed", task_id=self.task, actor=Actor.system("local_verifier"),
            payload={"delivery_digest": current["digest"], "criterion_id": "integration_behavior", "check_attempt_seq": pending.seq,
                     "outcome": "passed", "exit_code": 0, "artifacts_unchanged": True,
                     "stdout": {"complete": True}, "stderr": {"complete": True}})
        self.assertEqual(self.rt.delivery.run(self.task)["status"], "accepted")

    def test_different_digest_does_not_bypass_an_unfinished_review(self):
        self.prepare(local=True)
        self.rt.delivery.verify(self.task, "integration_behavior", [sys.executable, "work-1/check.py"])
        started, context = self.rt.delivery._start(self.task)
        self.rt.plan_work(self.task, [item("a", local=True)])
        self.rt.collaborate(self.task)
        self.rt.verify_work(self.task, "a", "behavior", [sys.executable, "check.py"])
        self.rt.collaborate(self.task)
        self.rt.delivery.verify(self.task, "integration_behavior", [sys.executable, "work-1/check.py"])
        latest = self.rt.delivery.view(self.task)
        self.assertNotEqual(latest["digest"], context["delivery_digest"])
        self.assertEqual(latest["status"], "reviewing")
        self.assertEqual(self.final.calls, [])
        with patch("cater.delivery.time.time", return_value=time.time() + 1000):
            self.assertEqual(self.rt.delivery.run(self.task)["status"], "uncertain")
        self.rt.delivery._finish(self.task, started, context, {"status": "stale", "reason": "旧版本调用已核对返回"})
        self.assertEqual(self.rt.delivery.run(self.task)["status"], "accepted")

    def test_different_digest_does_not_bypass_transport_uncertainty(self):
        with patch.object(self.final, "complete", side_effect=TimeoutError("fixture")):
            self.assertEqual(self.prepare()["status"], "uncertain")
        self.rt.plan_work(self.task, [item("new")])
        self.rt.collaborate(self.task)
        self.assertEqual(self.rt.delivery.view(self.task)["status"], "uncertain")
        self.assertEqual(self.final.calls, [])

    def test_unknown_check_or_wrong_actor_receipt_cannot_pass(self):
        current = self.prepare(local=True)
        started = self.rt.delivery._start_check(self.task, current, "integration_behavior", 60)
        self.rt.log.emit(type="verification.completed", task_id=self.task, actor=Actor.agent("dev", "worker"),
            payload={"delivery_digest": current["digest"], "criterion_id": "integration_behavior", "check_attempt_seq": started.seq,
                     "outcome": "passed", "exit_code": 0, "artifacts_unchanged": True,
                     "stdout": {"complete": True}, "stderr": {"complete": True}})
        self.assertEqual(self.rt.delivery.view(self.task)["local_checks"][0]["status"], "unknown")
        with self.assertRaises(ValueError):
            self.rt.delivery.verify(self.task, "invented", [sys.executable, "-c", "pass"])

    def test_expired_overall_check_requires_reconciliation(self):
        current = self.prepare(local=True)
        self.rt.delivery._start_check(self.task, current, "integration_behavior", 1)
        with patch("cater.delivery.time.time", return_value=time.time() + 1000):
            with self.assertRaises(WorkConflict):
                self.rt.delivery.verify(self.task, "integration_behavior", [sys.executable, "work-1/check.py"])
        self.assertEqual(self.final.calls, [])

    def test_invalid_check_arguments_do_not_leave_ghost_attempts(self):
        self.prepare(local=True)
        for command, timeout in (([], 60), ([sys.executable, None], 60), ([sys.executable], float("nan")), ([sys.executable], True)):
            with self.subTest(command=command, timeout=timeout):
                before = self.rt.log.count()
                with self.assertRaises(ValueError):
                    self.rt.delivery.verify(self.task, "integration_behavior", command, timeout=timeout)
                self.assertEqual(self.rt.log.count(), before)

    def test_runtime_concurrent_work_checks_wait_for_the_whole_wave(self):
        self.rt.plan_work(self.task, [item("a", local=True), item("b", deps=["a"])])
        self.rt.collaborate(self.task)
        entered = {name: threading.Event() for name in ("pass", "fail")}
        release = {name: threading.Event() for name in entered}
        self.addCleanup(lambda: [e.set() for e in release.values()])
        from cater.verification import run_check
        errors = []
        def check(command, **kwargs):
            label = command[-1]
            entered[label].set()
            if not release[label].wait(5):
                raise TimeoutError("fixture check was not released")
            return run_check([sys.executable, "-c", "pass" if label == "pass" else "raise SystemExit(1)"], **kwargs)
        def invoke(label):
            try:
                with Runtime(self.db, roles_path=self.roles, drivers=self.drivers) as rt:
                    rt.verify_work(self.task, "a", "behavior", [sys.executable, label])
            except Exception as exc:
                errors.append(exc)
        with patch("cater.runtime.run_check", side_effect=check):
            first = threading.Thread(target=invoke, args=("pass",)); second = threading.Thread(target=invoke, args=("fail",))
            first.start(); self.assertTrue(entered["pass"].wait(3))
            second.start(); self.assertTrue(entered["fail"].wait(3))
            release["pass"].set(); first.join(5)
            self.rt.collaborate(self.task)
            self.assertEqual(self.rt.work.view(self.task)["items"][0]["status"], "submitted")
            self.assertEqual(len(self.worker.calls), 1)
            release["fail"].set(); second.join(5)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual(errors, [])
        self.rt.collaborate(self.task)
        self.assertEqual({i["id"]: i["status"] for i in self.rt.work.view(self.task)["items"]}, {"a": "ready", "b": "blocked"})
        self.assertEqual(self.final.calls, [])

    def test_artifact_mutation_and_same_item_name_collisions_block_acceptance(self):
        self.prepare(local=True)
        command = [sys.executable, "-c", "from pathlib import Path; Path('work-1/check.py').write_text('changed')"]
        event, _ = self.rt.delivery.verify(self.task, "integration_behavior", command)
        self.assertFalse(event.payload["artifacts_unchanged"])
        self.rt.collaborate(self.task)
        self.assertNotEqual(self.rt.delivery.view(self.task)["status"], "accepted")
        self.assertEqual(self.final.calls, [])

    def test_oversized_review_input_stops_without_calling_or_truncating(self):
        original = self.rt.delivery._manifest
        def oversized(task_id):
            manifest = original(task_id)
            manifest["sources"].append({"id": "oversized", "text": "x" * 140000, "seq": 0})
            return manifest
        with patch.object(self.rt.delivery, "_manifest", side_effect=oversized):
            self.assertEqual(self.prepare()["status"], "blocked")
        self.assertEqual(self.final.calls, [])

    def test_adapter_selects_delivery_schema_without_a_real_model(self):
        from examples import codex_cli_driver as adapter
        payload = {"requirements": [], "checks": [], "handoff": {"summary": "fixture", "why": "fixture", "reproduce": [], "limits": []}}
        observed = []
        def run(command, **kwargs):
            observed.append(json.loads(Path(command[command.index("--output-schema") + 1]).read_text()))
            Path(command[command.index("--output-last-message") + 1]).write_text(json.dumps(payload))
            return subprocess.CompletedProcess(command, 0)
        with patch.object(adapter.subprocess, "run", side_effect=run):
            result = adapter.handle({"id": "delivery", "type": "complete", "context": {"work": {"phase": "delivery"}}})
        self.assertTrue(result["ok"])
        self.assertEqual(set(observed[0]["required"]), set(payload))

    def test_supersession_requires_later_human_source_and_verified_replacement(self):
        self.rt.command(self.task, "新的要求：保留复查能力")
        def transform(raw):
            old, new = raw["requirements"][0], raw["requirements"][-1]
            old.update(status="superseded", evidence=[], superseded_by=new["id"], source_quote="新的要求")
            return raw
        self.final.transform = transform
        self.assertEqual(self.prepare()["status"], "accepted")
        context = self.final.calls[-1]
        raw = json.loads(self.final.complete(role="fixture", prompt="", context={"work": context}).text)
        raw["requirements"][-1].update(status="unknown", evidence=[])
        self.assertEqual(DeliveryService._verdict(raw, context)["status"], "needs_evidence")
        raw["requirements"][0]["superseded_by"] = raw["requirements"][0]["id"]
        with self.assertRaises(ValueError):
            DeliveryService._verdict(raw, context)

    def test_rule_cannot_be_superseded_by_a_user_quote(self):
        self.rt.rulebook.record("不允许编造实际检查", scope="task", task_id=self.task)
        self.rt.command(self.task, "请按新要求处理")
        def transform(raw):
            rule = raw["requirements"][-1]
            rule.update(status="superseded", evidence=[], superseded_by=raw["requirements"][-2]["id"], source_quote="请按新要求")
            return raw
        self.final.transform = transform
        self.assertEqual(self.prepare()["status"], "invalid")

    def test_concurrent_reviews_invoke_only_one_driver(self):
        self.prepare(local=True)
        self.rt.delivery.verify(self.task, "integration_behavior", [sys.executable, "work-1/check.py"])
        entered, release = threading.Event(), threading.Event()
        self.final.after = lambda _: (entered.set(), release.wait(5))
        errors = []
        def run():
            try:
                with Runtime(self.db, roles_path=self.roles, drivers=self.drivers) as rt:
                    rt.delivery.run(self.task)
            except Exception as exc:
                errors.append(exc)
        first = threading.Thread(target=run); first.start()
        self.assertTrue(entered.wait(3))
        self.assertEqual(self.rt.delivery.run(self.task)["status"], "reviewing")
        release.set(); first.join(5)
        self.assertFalse(first.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(self.final.calls), 1)
        self.assertEqual(self.rt.delivery.view(self.task)["status"], "accepted")

    def test_interrupted_review_cannot_be_replayed_after_lease_expiry(self):
        self.prepare(local=True)
        self.rt.delivery.verify(self.task, "integration_behavior", [sys.executable, "work-1/check.py"])
        self.rt.delivery._start(self.task)
        with patch("cater.delivery.time.time", return_value=time.time() + 1000):
            self.assertEqual(self.rt.delivery.run(self.task)["status"], "uncertain")
        self.assertEqual(self.final.calls, [])

    def test_new_overall_check_revokes_old_success_until_all_concurrent_receipts_finish(self):
        self.prepare(local=True)
        self.rt.delivery.verify(self.task, "integration_behavior", [sys.executable, "work-1/check.py"])
        self.rt.collaborate(self.task)
        current = self.rt.delivery.view(self.task)
        first = self.rt.delivery._start_check(self.task, current, "integration_behavior", 60)
        second = self.rt.delivery._start_check(self.task, current, "integration_behavior", 60)
        self.assertEqual(first.payload["wave"], second.payload["wave"])
        self.assertEqual(self.rt.delivery.view(self.task)["status"], "checking")
        base = {"delivery_digest": current["digest"], "criterion_id": "integration_behavior", "artifacts_unchanged": True,
                "stdout": {"complete": True}, "stderr": {"complete": True}}
        self.rt.log.emit(type="verification.completed", actor=Actor.system("local_verifier"), task_id=self.task,
                         payload={**base, "check_attempt_seq": first.seq, "outcome": "failed", "exit_code": 1})
        self.assertEqual(self.rt.delivery.view(self.task)["local_checks"][0]["status"], "unknown")
        self.rt.log.emit(type="verification.completed", actor=Actor.system("local_verifier"), task_id=self.task,
                         payload={**base, "check_attempt_seq": second.seq, "outcome": "passed", "exit_code": 0})
        self.assertEqual(self.rt.delivery.view(self.task)["local_checks"][0]["status"], "failed")
        self.rt.collaborate(self.task)
        self.assertEqual(len(self.final.calls), 1)
        self.rt.delivery.verify(self.task, "integration_behavior", [sys.executable, "work-1/check.py"])
        self.rt.collaborate(self.task)
        self.assertEqual(self.rt.delivery.view(self.task)["status"], "accepted")

    def test_export_api_is_authenticated_and_rejects_stale_bundle(self):
        self.prepare()
        base = start_test_web(self, self.db, roles_path=self.roles, shared_drivers=self.drivers, access_token="fixture-token")
        endpoint = f"{base}/api/tasks/{self.task}/delivery"
        with self.assertRaises(HTTPError) as error:
            urlopen(endpoint)
        self.assertEqual(error.exception.code, 401)
        with urlopen(Request(endpoint, headers={"X-Cater-Access-Token": "fixture-token"})) as response:
            self.assertEqual(response.headers["Content-Type"], "application/zip")
            self.assertTrue(zipfile.is_zipfile(io.BytesIO(response.read())))
        self.rt.command(self.task, "新方向")
        with self.assertRaises(HTTPError) as error:
            urlopen(Request(endpoint, headers={"X-Cater-Access-Token": "fixture-token"}))
        self.assertEqual(error.exception.code, 409)


if __name__ == "__main__":
    unittest.main()
