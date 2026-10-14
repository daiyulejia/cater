from __future__ import annotations

import copy
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from cater.drivers import DriverResponse, DriverUnavailable
from cater.events import Actor
from cater.runtime import Runtime
from cater.web import ActivityRunner
from cater.work import WorkConflict
from test_delivery import DeliveryDriver, DocumentWorkDriver
from test_runtime import start_test_web
from test_work import item


class ExplainerDriver:
    name = "explain"

    def __init__(self):
        self.calls = []
        self.final = DeliveryDriver()
        self.after = None
        self.transform = None

    def complete(self, *, role, prompt, context):
        work = context["work"]
        if work["phase"] != "explanation":
            return self.final.complete(role=role, prompt=prompt, context=context)
        self.calls.append(copy.deepcopy({"prompt": prompt, "context": context}))
        if self.after:
            self.after(work)
        source = next(s for s in work["sources"] if s["kind"] == "member_artifact")
        raw = {"status": "explained", "answer": "先看这份具体成果，再核对它覆盖的目标；没有外部调用者记录，不能据此保证所有调用方都不受影响。",
               "evidence": [{"source_seq": source["seq"], "quote": source["text"][:50]}],
               "limits": ["尚未读取原始项目的外部调用方。"]}
        if self.transform:
            raw = self.transform(raw)
        return DriverResponse(json.dumps(raw), 17, {"token_usage": "reported"})


class ExplanationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "questions.db"
        self.roles = Path(self.tmp.name) / "roles.json"
        config = json.loads(Path("cater/packs/default_roles.json").read_text(encoding="utf-8"))
        config["organization"]["leader"]["driver"] = "explain"
        self.roles.write_text(json.dumps(config), encoding="utf-8")
        self.driver, self.worker = ExplainerDriver(), DocumentWorkDriver()
        self.drivers = {**{name: self.worker for name in ("dev", "test", "design", "product")}, "explain": self.driver}
        self.rt = Runtime(self.db, roles_path=self.roles, drivers=self.drivers)
        self.addCleanup(self.rt.close)
        self.task = self.rt.create_task("整理一份可复查的说明", ["能说明结论依据"])
        self.rt.plan_work(self.task, [item("document")])
        self.rt.collaborate(self.task)
        self.assertEqual(self.rt.task(self.task).status, "done")

    def ask(self, text="为什么这样处理，哪些影响还没检查？", request_id="question-request-001"):
        return self.rt.questions.ask(self.task, text, request_id=request_id)

    def test_question_and_grounded_answer_preserve_completed_delivery_and_account_usage(self):
        before = self.rt.delivery.view(self.task)
        count = self.rt.task(self.task).tokens
        asked = self.ask()
        self.assertEqual(self.rt.questions.pending(self.task), [asked.seq])
        self.assertTrue(self.rt.questions.answer_next(self.task))
        self.assertFalse(self.rt.questions.answer_next(self.task))
        view = self.rt.questions.view(self.task)[0]
        self.assertEqual(view["status"], "answered")
        self.assertFalse(view["outdated"])
        self.assertIn("外部调用方", view["limits"][0])
        self.assertEqual(self.rt.delivery.view(self.task), before)
        self.assertEqual(self.rt.task(self.task).status, "done")
        self.assertEqual(self.rt.task(self.task).tokens, count + 17)
        self.assertEqual(self.rt._reported_driver_tokens("explain"), 30)
        self.assertEqual(len(self.driver.calls), 1)
        self.assertTrue(self.rt.delivery.archive(self.task))
        self.assertTrue(self.rt.verify_chain().ok)

    def test_request_id_durably_reuses_question_without_resending_or_rebinding(self):
        asked = self.ask()
        self.rt.questions.answer_next(self.task)
        before = self.rt.log.count()
        with Runtime(self.db, roles_path=self.roles, drivers=self.drivers) as reopened:
            self.assertEqual(reopened.questions.ask(self.task, asked.payload["question"], request_id="question-request-001"), asked)
            self.assertFalse(reopened.questions.answer_next(self.task))
            with self.assertRaises(WorkConflict):
                reopened.questions.ask(self.task, "不同问题", request_id="question-request-001")
            self.assertEqual(reopened.log.count(), before)
            self.assertEqual(reopened.questions.view(self.task)[0]["status"], "answered")
        self.assertEqual(len(self.driver.calls), 1)

    def test_fabricated_evidence_or_actions_cannot_become_an_answer_or_change_work(self):
        original = self.rt.delivery.view(self.task)
        mutations = [lambda r: {**r, "evidence": [{"source_seq": 999999, "quote": "invented"}]},
                     lambda r: {**r, "evidence": [{**r["evidence"][0], "quote": "not in the supplied source"}]},
                     lambda r: {**r, "evidence": [{**r["evidence"][0], "quote": "  " + r["evidence"][0]["quote"] + "  "}]},
                     lambda r: {**r, "deliverables": [{"title": "unexpected action"}]},
                     lambda r: {**r, "evidence": []}]
        for index, mutation in enumerate(mutations):
            self.driver.transform = mutation
            self.ask(request_id=f"invalid-question-{index}")
            self.rt.questions.answer_next(self.task)
            self.assertEqual(self.rt.questions.view(self.task)[-1]["status"], "invalid")
            self.assertEqual(self.rt.delivery.view(self.task), original)
            self.assertEqual(self.rt.task(self.task).status, "done")

    def test_question_time_sources_stay_fixed_and_later_changes_mark_answer_historical(self):
        asked = self.ask()
        source_seqs = {s["seq"] for s in asked.payload["basis"]["source_seqs"]}
        self.rt.command(self.task, "NEW_DIRECTION_AFTER_QUESTION", goal="NEW_GOAL_AFTER_QUESTION")
        self.rt.questions.answer_next(self.task)
        call = self.driver.calls[0]
        self.assertNotIn("NEW_DIRECTION_AFTER_QUESTION", json.dumps(call))
        self.assertEqual(call["context"]["goal"], "整理一份可复查的说明")
        self.assertEqual({s["seq"] for s in call["context"]["work"]["sources"]}, source_seqs)
        self.assertTrue(self.rt.questions.view(self.task)[0]["outdated"])
        self.assertEqual(self.rt.task(self.task).goal, "NEW_GOAL_AFTER_QUESTION")
        self.assertEqual(self.rt.delivery.view(self.task)["status"], "stale")
        self.assertEqual(call["context"]["work"]["scope"], asked.payload["basis"]["anchor"]["scope"])

    def test_rework_removes_obsolete_artifacts_reviews_and_prior_answers_from_new_basis(self):
        self.ask()
        self.rt.questions.answer_next(self.task)
        old = self.rt.work.view(self.task)["items"][0]
        invalidated = self.rt.work.rework(self.task, old["id"], "OLD_RESULT_IS_WRONG")
        asked = self.ask("返工原因是什么？", request_id="reworked-question-002")
        basis = asked.payload["basis"]
        sources = {s["seq"] for s in basis["source_seqs"]}
        self.assertNotIn(old["review_seq"], sources)
        self.assertTrue(set(old["submission"]["artifact_seqs"]).isdisjoint(sources))
        self.assertIn(invalidated.seq, sources)
        self.assertEqual(basis["history_seqs"], [])
        self.assertIn("OLD_RESULT_IS_WRONG", json.dumps(basis))
        self.assertTrue(self.rt.questions.view(self.task)[0]["outdated"])

    def test_rules_changed_after_question_block_without_mixing_versions_or_calling_model(self):
        self.ask()
        self.rt.rulebook.record("NEW_RULE_AFTER_QUESTION", scope="task", task_id=self.task)
        self.rt.questions.answer_next(self.task)
        row = self.rt.questions.view(self.task)[0]
        self.assertEqual(row["status"], "blocked")
        self.assertTrue(row["outdated"])
        self.assertIn("店规已变更", row["reason"])
        self.assertEqual(self.driver.calls, [])
        self.assertNotIn("driver", row)

    def test_new_work_check_invalidates_previous_answer_and_replaces_receipt_sources(self):
        task = self.rt.create_task("检查回执变更不能沿用旧解释")
        self.rt.plan_work(task, [item("checkable", local=True)])
        self.rt.collaborate(task)
        first, _ = self.rt.verify_work(task, "checkable", "behavior", [sys.executable, "-c", "pass"])
        self.rt.questions.ask(task, "解释这条检查", request_id="check-question-first")
        self.rt.questions.answer_next(task)
        self.assertFalse(self.rt.questions.view(task)[0]["outdated"])
        second, _ = self.rt.verify_work(task, "checkable", "behavior", [sys.executable, "-c", "raise SystemExit(1)"])
        self.assertTrue(self.rt.questions.view(task)[0]["outdated"])
        asked = self.rt.questions.ask(task, "现在检查失败了吗", request_id="check-question-second")
        seqs = {s["seq"] for s in asked.payload["basis"]["source_seqs"]}
        self.assertNotIn(first.seq, seqs)
        self.assertIn(second.seq, seqs)

    def test_real_direction_during_answer_is_not_overwritten_and_history_is_separate(self):
        self.ask()
        self.rt.questions.answer_next(self.task)
        self.ask("你说的影响边界能举例吗？", request_id="followup-question-002")
        self.driver.after = lambda _: self.rt.command(self.task, "真实的补充要求")
        self.rt.questions.answer_next(self.task)
        history = self.driver.calls[-1]["context"]["work"]["history"]
        self.assertEqual(len(history), 1)
        self.assertIn("外部调用者", history[0]["answer"])
        self.assertTrue(self.rt.questions.view(self.task)[-1]["outdated"])
        self.assertEqual(self.rt.delivery.view(self.task)["status"], "stale")

    def test_insufficient_evidence_is_an_honest_response_without_false_grounding(self):
        self.driver.transform = lambda r: {**r, "status": "insufficient", "answer": "现有记录没有该调用方的代码，无法判断。", "evidence": []}
        self.ask()
        self.rt.questions.answer_next(self.task)
        self.assertEqual(self.rt.questions.view(self.task)[0]["status"], "insufficient")
        self.assertEqual(self.rt.task(self.task).status, "done")

    def test_budget_denial_never_calls_model_or_records_fake_usage(self):
        self.driver.token_budget = 0
        self.ask()
        count = self.rt.task(self.task).tokens
        self.rt.questions.answer_next(self.task)
        question = self.rt.questions.view(self.task)[0]
        self.assertEqual(question["status"], "blocked")
        self.assertNotIn("driver", question)
        self.assertEqual(self.driver.calls, [])
        self.assertEqual(self.rt.task(self.task).tokens, count)

    def test_transport_failure_and_interruption_do_not_automatically_replay(self):
        def fail(_):
            raise DriverUnavailable("unknown outcome")
        self.driver.after = fail
        self.ask()
        self.rt.questions.answer_next(self.task)
        self.assertEqual(self.rt.questions.view(self.task)[0]["status"], "uncertain")
        self.assertFalse(self.rt.questions.answer_next(self.task))
        self.ask(request_id="interrupted-question-002")
        original_emit = self.rt.log.emit
        def interrupted(**kwargs):
            if kwargs["type"] == "question.finished":
                raise RuntimeError("process interrupted after model")
            return original_emit(**kwargs)
        with patch.object(self.rt.log, "emit", side_effect=interrupted):
            with self.assertRaisesRegex(RuntimeError, "process interrupted"):
                self.rt.questions.answer_next(self.task)
        with patch("cater.explanations.time.time", return_value=time.time() + 1000):
            self.assertEqual(self.rt.questions.view(self.task)[-1]["status"], "uncertain")
            self.assertFalse(self.rt.questions.answer_next(self.task))
        self.assertEqual(len(self.driver.calls), 2)

    def test_known_chat_questions_are_not_commands_but_mixed_requirements_still_are(self):
        original = self.rt.work.scope(self.task)
        for text in ("为什么这么改？", "@店长 为什么这样做", "请讲解：我怎样向别人解释这里的取舍？"):
            event = self.rt.command(self.task, text)
            self.assertEqual(event.type, "question.asked")
            self.assertEqual(self.rt.work.scope(self.task), original)
        event = self.rt.command(self.task, "为什么这么改？另外必须补充接口测试")
        self.assertEqual(event.type, "human.commanded")
        self.assertNotEqual(self.rt.work.scope(self.task), original)

    def test_explicit_goal_and_acceptance_remain_directions_even_with_question_text(self):
        event = self.rt.command(self.task, "为什么这么改？", goal="修改后的目标", acceptance=["新的标准"])
        self.assertEqual(event.type, "human.commanded")
        self.assertEqual(self.rt.task(self.task).goal, "修改后的目标")
        self.assertEqual(self.rt.task(self.task).acceptance, ["新的标准"])

    def test_demo_does_not_invent_a_personalized_answer_or_spend_tokens(self):
        with Runtime(":memory:") as demo:
            task = demo.create_task("演示任务")
            demo.questions.ask(task, "这里为什么这么做？", request_id="demo-question-001")
            demo.questions.answer_next(task)
            row = demo.questions.view(task)[0]
            self.assertEqual(row["status"], "blocked")
            self.assertIn("演示模式", row["reason"])
            self.assertNotIn("driver", row)
            self.assertEqual(demo.task(task).tokens, 0)

    def test_oversized_sources_block_without_truncation_or_model_call(self):
        for index in range(3):
            self.rt.log.emit(type="deliverable.published", actor=Actor.agent("dev", "worker"), task_id=self.task,
                             payload={"title": str(index), "summary": "large", "content": "x" * 65536})
        asked = self.ask()
        self.rt.questions.answer_next(self.task)
        row = self.rt.questions.view(self.task)[0]
        self.assertEqual(row["status"], "blocked")
        self.assertIn("没有截断", row["reason"])
        self.assertEqual(len(asked.payload["basis"]["source_seqs"]), len(row["basis"]["source_seqs"]))
        self.assertEqual(self.driver.calls, [])

    def test_child_explanation_never_reads_later_parent_or_other_group_events(self):
        child = self.rt.derive_task(self.task, "另一个工作目标", request_id="explanation-child-create")["task_id"]
        self.rt.plan_work(child, [item("child-document")])
        self.rt.collaborate(child)
        self.rt.command(self.task, "PARENT_PRIVATE_LATER", goal="PARENT_PRIVATE_GOAL")
        other = self.rt.create_task("UNRELATED_PRIVATE_GOAL")
        self.rt.log.emit(type="deliverable.published", actor=Actor.agent("dev", "worker"), task_id=other,
                         payload={"content": "UNRELATED_PRIVATE_BODY"})
        self.rt.questions.ask(child, "新群的产出为什么这样组织？", request_id="child-explanation-001")
        self.rt.questions.answer_next(child)
        call = json.dumps(self.driver.calls[-1], ensure_ascii=False)
        self.assertNotIn("PARENT_PRIVATE", call)
        self.assertNotIn("UNRELATED_PRIVATE", call)
        self.assertEqual(self.rt.questions.view(child)[0]["status"], "answered")

    def test_failed_thread_start_can_retry_pending_question_without_stuck_registration(self):
        self.ask()
        runner = ActivityRunner(lambda: Runtime(self.db, roles_path=self.roles, drivers=self.drivers, close_drivers_on_close=False))
        with patch("cater.web.threading.Thread.start", side_effect=RuntimeError("cannot start")):
            with self.assertRaisesRegex(RuntimeError, "cannot start"):
                runner.answer_questions(self.task)
        self.assertNotIn(self.task, runner._question_tasks)
        self.assertEqual(len(self.rt.questions.pending(self.task)), 1)

    def test_simultaneous_answer_attempts_invoke_only_one_model(self):
        self.ask()
        entered, release = threading.Event(), threading.Event()
        self.driver.after = lambda _: (entered.set(), release.wait(5))
        errors = []
        def run():
            try:
                with Runtime(self.db, roles_path=self.roles, drivers=self.drivers) as rt:
                    rt.questions.answer_next(self.task)
            except Exception as exc:
                errors.append(exc)
        worker = threading.Thread(target=run)
        worker.start()
        self.assertTrue(entered.wait(3))
        try:
            self.assertFalse(self.rt.questions.answer_next(self.task))
        finally:
            release.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(self.driver.calls), 1)

    def test_question_queue_preserves_existing_execution_followup_and_answers_in_order(self):
        factory = lambda: Runtime(self.db, roles_path=self.roles, drivers=self.drivers, close_drivers_on_close=False)
        runner = ActivityRunner(factory)
        runner._activities[self.task] = {"id": "running-work", "state": "running", "action": "run"}
        runner.continue_after_current(self.task, "battle")
        self.ask("第一个问题", request_id="queued-question-001")
        self.ask("第二个问题", request_id="queued-question-002")
        runner.answer_questions(self.task)
        runner.answer_questions(self.task)
        for _ in range(200):
            if all(q["status"] == "answered" for q in self.rt.questions.view(self.task)):
                break
            time.sleep(.01)
        self.assertEqual([c["context"]["work"]["question"] for c in self.driver.calls], ["第一个问题", "第二个问题"])
        self.assertEqual(runner.status(self.task)["follow_up"]["action"], "battle")

    def test_http_question_endpoint_is_authenticated_idempotent_and_preserves_acceptance(self):
        base = start_test_web(self, self.db, roles_path=self.roles, shared_drivers=self.drivers, access_token="fixture-token")
        url = f"{base}/api/tasks/{self.task}/questions"
        data = json.dumps({"question": "如何独立核对这个选择？", "request_id": "http-question-request"}).encode()
        with self.assertRaises(HTTPError) as denied:
            urlopen(Request(url, data=data, headers={"Content-Type": "application/json"}))
        self.assertEqual(denied.exception.code, 401)
        headers = {"Content-Type": "application/json", "X-Cater-Access-Token": "fixture-token"}
        for _ in range(2):
            with urlopen(Request(url, data=data, headers=headers)) as response:
                body = json.loads(response.read())
                self.assertEqual(body["task"]["status"], "done")
                self.assertEqual(len(body["questions"]), 1)
        for _ in range(200):
            if self.rt.questions.view(self.task)[0]["status"] == "answered":
                break
            time.sleep(.01)
        self.assertEqual(len(self.driver.calls), 1)
        with urlopen(Request(base + f"/api/tasks/{self.task}", headers=headers)) as response:
            body = json.loads(response.read())
            self.assertEqual(body["questions"][0]["status"], "answered")
            self.assertEqual(body["task"]["status"], "done")

    def test_http_rejects_client_basis_bad_target_and_conflicting_request(self):
        base = start_test_web(self, self.db, roles_path=self.roles, shared_drivers=self.drivers)
        url = f"{base}/api/tasks/{self.task}/questions"
        headers = {"Content-Type": "application/json"}
        for payload in ({"question": "test", "request_id": "valid-question-id", "basis": {}},
                        {"question": "test", "request_id": "valid-question-id", "role_id": "missing"},
                        {"question": "test", "request_id": None}):
            count = self.rt.log.count()
            with self.assertRaises(HTTPError) as error:
                urlopen(Request(url, data=json.dumps(payload).encode(), headers=headers))
            self.assertEqual(error.exception.code, 400)
            self.assertEqual(self.rt.log.count(), count)
        self.ask(request_id="valid-question-id")
        with self.assertRaises(HTTPError) as error:
            urlopen(Request(url, data=json.dumps({"question": "changed", "request_id": "valid-question-id"}).encode(), headers=headers))
        self.assertEqual(error.exception.code, 409)


if __name__ == "__main__":
    unittest.main()
