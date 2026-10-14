from __future__ import annotations

import copy
import argparse
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request, urlopen

from cater.availability import team_availability
from cater.drivers import DriverResponse
from cater.planning import Planner, validate_plan
from cater.runtime import Runtime
from cater.work import WorkConflict
from test_work import WorkDriver, item
from test_runtime import start_test_web


class PlanningDriver:
    def __init__(self):
        self.calls = []
        self.items = [item("implement", local=True)]
        self.reuse = False
        self.after = None
        self.transform = None

    def complete(self, *, role, prompt, context):
        work = context["work"]
        self.calls.append(copy.deepcopy(work))
        if self.after:
            self.after(work)
        payload = {"status": "ready", "summary": "先实现，再检查实际成果；不把提交当作通过。",
                   "items": copy.deepcopy(self.items),
                   "coverage": [{"source_id": s["id"], "item_ids": [i["id"] for i in self.items],
                                 "superseded_by": None, "reason": "按此要求安排工作", "quote": s["text"][:1000]}
                                for s in work["sources"]],
                   "reuse": [{"work_item_id": i["id"], "submission_seq": i["submission"]["seq"], "reason": "候选成果仍可核对"}
                             for i in work["previous_plan"]["items"] if self.reuse and i.get("submission")]}
        if self.transform:
            payload = self.transform(payload)
        return DriverResponse(text=json.dumps(payload), tokens=11, meta={"token_usage": "reported"})


class PlanningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "planning.db"
        config = json.loads(Path("cater/packs/default_roles.json").read_text(encoding="utf-8"))
        config["organization"]["leader"]["driver"] = "planner"
        self.roles = Path(self.temp.name) / "roles.json"
        self.roles.write_text(json.dumps(config), encoding="utf-8")
        self.planner, self.worker = PlanningDriver(), WorkDriver()
        self.drivers = {**{name: self.worker for name in ("product", "dev", "test", "design")}, "planner": self.planner}
        self.rt = Runtime(self.db, roles_path=self.roles, drivers=self.drivers)
        self.addCleanup(self.rt.close)
        self.task = self.rt.create_task("交付可验证的实现", ["保留现有接口"])
        self.rt.intake(self.task, preferred_owner="dev", requested_route="direct")

    def view(self):
        return self.rt.work.view(self.task)

    def test_initial_planning_flows_into_work_and_waits_for_real_checks(self):
        result = self.rt.collaborate(self.task)
        self.assertIn("待", result["brief"]["dispute"])
        self.assertEqual(self.view()["items"][0]["status"], "submitted")
        self.assertEqual(self.planner.calls[0]["request"]["preferred_owner"], "dev")
        self.assertEqual(len(self.view()["planning"]["coverage"]), 2)
        self.assertEqual(self.rt._reported_driver_tokens("planner"), 11)
        self.assertEqual(len(self.worker.calls), 1)
        self.rt.verify_work(self.task, "implement", "behavior", [sys.executable, "check.py"])
        self.rt.collaborate(self.task)
        self.assertTrue(self.view()["stage_accepted"])
        self.assertEqual(len(self.planner.calls), 1)
        self.assertNotEqual(self.rt.task(self.task).status, "done")

    def test_cli_dispatch_reports_pending_planning_without_assuming_legacy_plan_shape(self):
        from cater.cli import cmd_dispatch
        out = io.StringIO()
        with patch("cater.cli.open_runtime", return_value=Runtime(self.db, roles_path=self.roles, drivers=self.drivers)), contextlib.redirect_stdout(out):
            self.assertEqual(cmd_dispatch(argparse.Namespace(task_id=self.task)), 0)
        self.assertIn("规划已排队", out.getvalue())
        self.assertEqual(self.planner.calls, [])

    def test_change_replans_and_reuses_bytes_but_never_old_check_receipts(self):
        self.rt.collaborate(self.task)
        self.rt.verify_work(self.task, "implement", "behavior", [sys.executable, "check.py"])
        self.rt.collaborate(self.task)
        old = self.view()["items"][0]["submission"]
        self.planner.reuse = True
        self.rt.command(self.task, "还要检查兼容性")
        self.rt.collaborate(self.task)
        current = self.view()["items"][0]
        self.assertEqual(current["submission"]["reused_from"], old["seq"])
        self.assertEqual(current["submission"]["artifact_seqs"], old["artifact_seqs"])
        self.assertNotEqual(current["submission"]["digest"], old["digest"])
        self.assertEqual(current["status"], "submitted")
        self.assertEqual(current["checks"], [])
        self.assertEqual(len([c for c in self.worker.calls if c.get("phase") != "review"]), 1)
        self.assertEqual(len(self.planner.calls[-1]["sources"]), 3)
        self.rt.verify_work(self.task, "implement", "behavior", [sys.executable, "check.py"])
        self.rt.collaborate(self.task)
        self.assertTrue(self.view()["stage_accepted"])
        self.assertTrue(self.rt.verify_chain().ok)

    def test_reused_candidate_rejection_executes_owner_on_next_attempt(self):
        self.planner.items = [item("a")]
        self.rt.collaborate(self.task)
        self.planner.reuse = True
        self.rt.command(self.task, "需要再核对一个细节")
        self.worker.status = "failed"
        self.rt.collaborate(self.task)
        self.assertEqual(self.view()["items"][0]["status"], "ready")
        self.worker.status = "passed"
        self.rt.collaborate(self.task)
        self.assertEqual(len([c for c in self.worker.calls if c.get("phase") != "review"]), 2)
        self.assertNotIn("reused_from", self.view()["items"][0]["submission"])

    def test_changed_dependency_artifact_forces_new_execution(self):
        self.planner.items = [item("a"), item("b", deps=["a"])]
        self.rt.collaborate(self.task)
        self.planner.reuse = True
        self.rt.command(self.task, "修改前置结果")
        self.planner.transform = lambda p: {**p, "reuse": [r for r in p["reuse"] if r["work_item_id"] == "b"]}
        original = self.worker.complete

        def changed(**kwargs):
            response = original(**kwargs)
            if kwargs["context"]["work"].get("phase") != "review":
                raw = json.loads(response.text)
                raw["deliverables"][0]["content"] = "assert 3 + 3 == 6\n"
                return DriverResponse(text=json.dumps(raw))
            return response

        with patch.object(self.worker, "complete", side_effect=changed):
            self.rt.collaborate(self.task)
        self.assertEqual(len([c for c in self.worker.calls if c.get("phase") != "review"]), 4)
        self.assertTrue(self.view()["stage_accepted"])

    def test_status_and_resume_do_not_replan_current_work(self):
        self.rt.collaborate(self.task)
        original = self.view()["plan_seq"]
        self.rt.command(self.task, "进度怎么样")
        self.rt.command(self.task, "继续")
        self.rt.collaborate(self.task)
        self.assertEqual(self.view()["plan_seq"], original)
        self.assertEqual(len(self.planner.calls), 1)

    def test_renamed_dependency_with_identical_body_is_not_treated_as_unchanged(self):
        self.planner.items = [item("a"), item("b", deps=["a"])]
        self.rt.collaborate(self.task)
        self.planner.reuse = True
        self.rt.command(self.task, "修改前置文件名")
        self.planner.transform = lambda p: {**p, "reuse": [r for r in p["reuse"] if r["work_item_id"] == "b"]}
        original = self.worker.complete

        def renamed(**kwargs):
            response = original(**kwargs)
            if kwargs["context"]["work"].get("phase") != "review":
                raw = json.loads(response.text)
                raw["deliverables"][0]["filename"] = "renamed.py"
                return DriverResponse(text=json.dumps(raw))
            return response

        with patch.object(self.worker, "complete", side_effect=renamed):
            self.rt.collaborate(self.task)
        self.assertEqual(len([c for c in self.worker.calls if c.get("phase") != "review"]), 4)

    def test_intake_rejects_malformed_preferences_before_recording_planning(self):
        for kwargs in ({"preferred_objective": []}, {"preferred_acceptance": "x" * 2001}):
            count = self.rt.log.count()
            with self.assertRaises(ValueError):
                self.rt.intake(self.task, **kwargs)
            self.assertEqual(self.rt.log.count(), count)

    def test_late_response_cannot_install_after_goal_rule_or_manual_plan_change(self):
        callbacks = [lambda: self.rt.command(self.task, "新要求"),
                     lambda: self.rt.rulebook.record("新店规", scope="task", task_id=self.task),
                     lambda: self.rt.plan_work(self.task, [item("manual")]),
                     lambda: self.rt.intake(self.task)]
        for callback in callbacks:
            with self.subTest(callback=callback):
                self.rt.command(self.task, "下一轮要求")
                self.planner.after = lambda work: callback()
                result = Planner(self.rt).run(self.task)
                self.assertFalse(result["installed"])
                self.assertIn("未下发", result["reason"])
        self.assertEqual(self.worker.calls, [])

    def test_atomic_install_rechecks_when_direction_changes_during_append(self):
        original, changed = self.rt.log.emit, False

        def emit(**kwargs):
            nonlocal changed
            if kwargs["type"] == "work.plan_created" and not changed:
                changed = True
                self.rt.command(self.task, "安装瞬间改变要求")
            return original(**kwargs)

        with patch.object(self.rt.log, "emit", side_effect=emit):
            self.assertFalse(Planner(self.rt).run(self.task)["installed"])
        self.assertIsNone(self.view()["plan_seq"])

    def test_invalid_plans_do_not_execute_or_automatically_burn_retry_budget(self):
        self.planner.transform = lambda p: {**p, "coverage": []}
        self.rt.collaborate(self.task)
        self.rt.collaborate(self.task)
        self.assertEqual(len(self.planner.calls), 1)
        self.assertEqual(self.worker.calls, [])
        self.assertIsNone(self.view()["plan_seq"])
        self.rt.command(self.task, "继续")
        self.planner.transform = None
        self.rt.collaborate(self.task)
        self.assertEqual(len(self.planner.calls), 2)

    def test_uncertain_planner_is_not_automatically_replayed(self):
        with patch.object(self.planner, "complete", side_effect=RuntimeError("response lost")) as call:
            self.rt.collaborate(self.task)
            self.rt.command(self.task, "继续")
            self.rt.collaborate(self.task)
            self.rt.command(self.task, "有新的要求也不能偷偷重试结果不明的调用")
            self.rt.collaborate(self.task)
        self.assertEqual(call.call_count, 1)
        self.assertEqual(self.worker.calls, [])

    def test_unfinished_planning_is_not_replayed_after_restart(self):
        Planner(self.rt)._start(self.task)
        with Runtime(self.db, roles_path=self.roles, drivers=self.drivers) as reopened:
            result = Planner(reopened).run(self.task)
            self.assertFalse(result["installed"])
            self.assertIn("未结束", result["reason"])
        self.assertEqual(self.planner.calls, [])

    def test_concurrent_planning_has_only_one_invocation(self):
        barrier = threading.Barrier(2)
        outcomes, errors = [], []

        def run():
            try:
                with Runtime(self.db, roles_path=self.roles, drivers=self.drivers) as rt:
                    barrier.wait(timeout=5)
                    outcomes.append(Planner(rt).run(self.task))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(sum(o["installed"] for o in outcomes), 1)
        self.assertEqual(len(self.planner.calls), 1)

    def test_inflight_work_blocks_replan_without_extra_model_call(self):
        Planner(self.rt).run(self.task)
        self.rt.work.claim(self.task, "implement")
        self.rt.command(self.task, "新目标")
        self.assertFalse(Planner(self.rt).run(self.task)["installed"])
        self.assertEqual(len(self.planner.calls), 1)

    def test_structural_and_source_guards(self):
        Planner(self.rt).run(self.task)
        context = self.planner.calls[0]
        base = json.loads(self.planner.complete(role="", prompt="", context={"work": context}).text)
        mutations = [lambda p: p["items"][0].update(reviewer="dev"),
                     lambda p: p["items"][0].update(reviewer=None),
                     lambda p: p["items"][0].update(owner="cabinet_product"),
                     lambda p: p["items"][0].update(depends_on=["implement"]),
                     lambda p: p["coverage"][0].update(source_id="invented"),
                     lambda p: p["coverage"][0].update(quote="invented"),
                     lambda p: p["coverage"][0].update(item_ids=[]),
                     lambda p: p["coverage"][0].update(superseded_by=p["coverage"][0]["source_id"], item_ids=[]),
                     lambda p: p.update(reuse=[{"work_item_id": "implement", "submission_seq": 999999, "reason": "不存在"}])]
        for mutate in mutations:
            candidate = copy.deepcopy(base)
            mutate(candidate)
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                validate_plan(candidate, context)

    def test_existing_local_checks_cannot_be_deleted_or_downgraded(self):
        self.rt.collaborate(self.task)
        self.rt.command(self.task, "增加一个细节")
        self.planner.items[0]["criteria"] = self.planner.items[0]["criteria"][:1]
        result = Planner(self.rt).run(self.task)
        self.assertFalse(result["installed"])
        self.assertIn("必需实际检查", result["reason"])
        self.assertFalse(self.view()["scope_current"])

    def test_budget_decline_and_leader_availability_are_truthful(self):
        self.planner.token_budget = 0
        self.rt.collaborate(self.task)
        self.assertEqual(self.planner.calls, [])
        leader = team_availability(self.rt)["members"][0]
        self.assertEqual(leader["reason"], "budget_exhausted")
        self.planner.token_budget = 100
        self.rt.command(self.task, "继续")
        self.rt.collaborate(self.task)
        self.assertTrue(self.view()["items"])

    def test_adapter_uses_planning_contract_without_real_model(self):
        from examples import codex_cli_driver as adapter
        payload = {"status": "blocked", "summary": "缺少一个必要条件", "items": [], "coverage": [], "reuse": []}
        observed = []

        def run(command, **kwargs):
            observed.append(json.loads(Path(command[command.index("--output-schema") + 1]).read_text()))
            Path(command[command.index("--output-last-message") + 1]).write_text(json.dumps(payload))
            return subprocess.CompletedProcess(command, 0)

        with patch.object(adapter.subprocess, "run", side_effect=run):
            result = adapter.handle({"id": "plan", "type": "complete", "context": {"work": {"phase": "planning"}}})
        self.assertTrue(result["ok"])
        self.assertEqual(set(observed[0]["required"]), set(payload))
        self.assertEqual(json.loads(result["text"]), payload)

    def test_web_followup_replans_latest_direction_after_pending_call_returns(self):
        started, release = threading.Event(), threading.Event()

        def runtime_factory():
            return Runtime(self.db, roles_path=self.roles, drivers=self.drivers, close_drivers_on_close=False)

        base = start_test_web(self, str(self.db), runtime_factory=runtime_factory)
        self.addCleanup(release.set)

        def block_once(work):
            if len(self.planner.calls) == 1:
                started.set()
                if not release.wait(5):
                    raise TimeoutError("test planner was not released")

        self.planner.after = block_once

        def post(action, body):
            request = Request(f"{base}/api/tasks/{self.task}/{action}", data=json.dumps(body).encode(),
                              method="POST", headers={"Content-Type": "application/json"})
            with urlopen(request, timeout=3) as response:
                return json.load(response)

        post("run", {"background": True})
        self.assertTrue(started.wait(3))
        queued = post("command", {"instruction": "还必须支持中文错误信息", "continue": True})
        self.assertTrue(queued["activity"]["active"])
        release.set()
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            with urlopen(f"{base}/api/tasks/{self.task}", timeout=2) as response:
                data = json.load(response)
            if not data["activity"]["active"]:
                break
            time.sleep(0.02)
        self.assertFalse(data["activity"]["active"])
        self.assertTrue(data["task"]["work"]["scope_current"])
        self.assertEqual(len(self.planner.calls), 2)
        self.assertIn("还必须支持中文错误信息", [s["text"] for s in self.planner.calls[-1]["sources"]])
        self.assertEqual(len(self.worker.calls), 1)
        finishes = [e["payload"]["status"] for e in data["events"] if e["type"] == "planning.finished"]
        self.assertEqual(finishes, ["stale", "installed"])

    def test_changed_scope_during_reuse_does_not_lock_uninvoked_work(self):
        self.rt.collaborate(self.task)
        self.planner.reuse = True
        self.rt.command(self.task, "新的要求")
        original = self.rt.work.resubmit_candidate

        def reuse(task_id, claim):
            self.rt.command(task_id, "核对候选时又改变目标")
            return original(task_id, claim)

        with patch.object(self.rt.work, "resubmit_candidate", side_effect=reuse):
            self.rt.collaborate(self.task)
        current = self.view()["items"][0]
        self.assertFalse(current["in_flight"])
        self.assertEqual(current["status"], "stale")
        self.assertEqual(len(self.worker.calls), 1)

    def test_large_inputs_are_blocked_without_truncation_or_call(self):
        for index in range(12):
            self.rt.command(self.task, f"{index}:" + "x" * 11900)
        result = Planner(self.rt).run(self.task)
        self.assertFalse(result["installed"])
        self.assertIn("没有截断", result["reason"])
        self.assertEqual(self.planner.calls, [])

    def test_newer_source_may_replace_old_source_but_cannot_claim_completion(self):
        self.rt.command(self.task, "把目标改成整理文档", goal="整理文档")
        self.planner.items = [item("a", owner="product")]

        def replace(payload):
            goal = next(e for e in payload["coverage"] if e["quote"] == "整理文档")
            payload["coverage"][0].update(item_ids=[], superseded_by=goal["source_id"], quote="整理文档")
            return payload

        self.planner.transform = replace
        self.rt.collaborate(self.task)
        self.assertTrue(self.view()["stage_accepted"])
        self.assertNotEqual(self.rt.task(self.task).status, "done")


if __name__ == "__main__":
    unittest.main()
