from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from cater.clarifications import normalize_question
from cater.drivers import DriverResponse
from cater.runtime import Runtime
from cater.web import task_api_payload
from cater.work import WorkConflict
from test_runtime import start_test_web
from test_work import WorkDriver, item


QUESTION = {"question": "这页主要给哪类人看？", "options": ["初次来访的顾客", "已经使用过的同事"]}


class ClarificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "questions.db"
        self.rt = Runtime(self.db)
        self.addCleanup(self.rt.close)
        self.task = self.rt.create_task("整理欢迎说明")

    def ask(self, task=None):
        task = task or self.task
        return self.rt.clarifications.request(task, "store_manager", QUESTION, scope=self.rt.work.scope(task))

    def answer(self, question, **kwargs):
        return self.rt.clarifications.respond(self.task, question_seq=question.seq, kind="choice",
                                             option_index=0, request_id="answer-one", **kwargs)

    def test_selected_answer_is_one_versioned_direction_and_retry_is_idempotent(self):
        question = self.ask()
        self.assertEqual(task_api_payload(self.rt, self.task)["summary"]["conversation_count"], 2)
        goal = self.rt.task(self.task).goal
        command, created = self.answer(question)
        self.assertTrue(created)
        self.assertEqual(command.payload["intent"], "direction")
        self.assertIn("@店长", command.payload["instruction"])
        self.assertIn(QUESTION["question"], command.payload["instruction"])
        self.assertEqual(command.payload["clarification"]["answer"], QUESTION["options"][0])
        self.assertEqual(self.rt.task(self.task).goal, goal)
        before = self.rt.log.count()
        retried, created = self.answer(question)
        self.assertFalse(created)
        self.assertEqual(retried.seq, command.seq)
        self.assertEqual(self.rt.log.count(), before)
        self.assertEqual(task_api_payload(self.rt, self.task)["summary"]["conversation_count"], 3)
        with Runtime(self.db) as reopened:
            row = reopened.clarifications.view(self.task)[0]
            self.assertEqual(row["status"], "answered")
            self.assertEqual(row["response"]["option_index"], 0)
            self.assertTrue(reopened.verify_chain().ok)

    def test_custom_answer_and_skip_do_not_silently_choose_suggested_option(self):
        for kind in ("custom", "skip"):
            with self.subTest(kind=kind):
                task = self.rt.create_task("新的说明")
                question = self.ask(task)
                command, _ = self.rt.clarifications.respond(task, question_seq=question.seq, kind=kind,
                    request_id=kind, **({"answer": "  主要给研究生同学看  "} if kind == "custom" else {}))
                response = command.payload["clarification"]
                self.assertNotIn("option_index", response)
                if kind == "custom":
                    self.assertEqual(response["answer"], "主要给研究生同学看")
                else:
                    self.assertIn("不代表授权", response["answer"])
                    self.assertNotIn(QUESTION["options"][0], response["answer"])

    def test_stale_direction_rule_or_new_plan_blocks_old_question_without_writing(self):
        for change in (lambda: self.rt.command(self.task, "目标改成安装说明"),
                       lambda: self.rt.rulebook.record("新的要求", scope="task", task_id=self.task),
                       lambda: self.rt.intake(self.task)):
            with self.subTest(change=change):
                question = self.ask()
                change()
                count = self.rt.log.count()
                self.assertEqual(self.rt.clarifications.view(self.task)[-1]["status"], "stale")
                with self.assertRaises(WorkConflict):
                    self.answer(question)
                self.assertEqual(self.rt.log.count(), count)

    def test_invalid_question_or_answer_does_not_write_and_cross_task_answer_is_rejected(self):
        for value in (None, {**QUESTION, "options": ["一个"]}, {**QUESTION, "options": ["重复", " 重复 "]},
                      {**QUESTION, "extra": "permission"}, {**QUESTION, "question": " "}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_question(value)
        question = self.ask()
        baseline = self.rt.log.count()
        invalid = [dict(kind="choice", option_index=True), dict(kind="choice", option_index=20),
                   dict(kind="choice", option_index=0, answer="覆盖选项"), dict(kind="custom", answer=" "),
                   dict(kind="skip", option_index=0), dict(kind=[]), dict(kind="custom", answer="x" * 2001)]
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.rt.clarifications.respond(self.task, question_seq=question.seq, request_id="invalid", **values)
        other = self.rt.create_task("无关对话")
        count = self.rt.log.count()
        with self.assertRaises(ValueError):
            self.rt.clarifications.respond(other, question_seq=question.seq, kind="skip", request_id="cross")
        self.assertEqual(self.rt.log.count(), count)
        self.assertEqual(baseline + 2, count)

    def test_conflicting_retry_and_concurrent_answers_have_one_winner(self):
        question = self.ask()
        barrier = threading.Barrier(2)
        outcomes = []

        def respond(index):
            with Runtime(self.db) as rt:
                barrier.wait()
                try:
                    event, created = rt.clarifications.respond(self.task, question_seq=question.seq,
                        kind="choice", option_index=index, request_id="racing")
                    outcomes.append((event.seq, created))
                except WorkConflict:
                    outcomes.append("conflict")

        threads = [threading.Thread(target=respond, args=(i,)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(outcomes.count("conflict"), 1)
        commands = list(self.rt.log.iter_events(task_id=self.task, event_types=("human.commanded",)))
        self.assertEqual(len(commands), 1)
        self.assertTrue(self.rt.verify_chain().ok)

    def test_compare_and_swap_rechecks_question_before_accepting_late_answer(self):
        question = self.ask()
        original = self.rt.log.emit
        changed = False

        def emit(**kwargs):
            nonlocal changed
            if kwargs["type"] == "human.commanded" and not changed:
                changed = True
                self.rt.command(self.task, "请改成其他方向")
            return original(**kwargs)

        with patch.object(self.rt.log, "emit", side_effect=emit), self.assertRaises(WorkConflict):
            self.answer(question)
        self.assertEqual(self.rt.clarifications.view(self.task)[0]["status"], "stale")

    def test_pending_question_suspends_model_calls_but_plain_chat_can_supersede_it(self):
        question = self.ask()
        with patch.object(self.rt, "_collect_utterances", side_effect=AssertionError("must not execute")):
            result = self.rt.collaborate(self.task)
        self.assertIn("等待顾客补充", result["brief"]["dispute"])
        self.assertEqual(self.rt.clarifications.pending(self.task)["seq"], question.seq)
        self.rt.command(self.task, "不用选项了，请只做简短版本")
        self.assertIsNone(self.rt.clarifications.pending(self.task))


class ClarificationFlowTests(unittest.TestCase):
    def test_planner_question_then_answer_reaches_new_plan_and_execution_context(self):
        from test_planning import PlanningDriver
        with tempfile.TemporaryDirectory() as tmp:
            roles = json.loads(Path("cater/packs/default_roles.json").read_text(encoding="utf-8"))
            roles["organization"]["leader"]["driver"] = "planner"
            path = Path(tmp) / "roles.json"
            path.write_text(json.dumps(roles), encoding="utf-8")
            planner, worker = PlanningDriver(), WorkDriver()
            planner.transform = lambda p: {"status": "needs_input", "summary": "先确认读者，再安排文案。",
                "items": [], "coverage": [], "reuse": [], "clarification": QUESTION}
            with Runtime(Path(tmp) / "flow.db", roles_path=path,
                         drivers={**{r: worker for r in ("dev", "product", "design", "test")}, "planner": planner}) as rt:
                task = rt.create_task("写一段欢迎说明")
                rt.intake(task)
                rt.collaborate(task)
                self.assertEqual(worker.calls, [])
                pending = rt.clarifications.pending(task)
                rt.collaborate(task)
                self.assertEqual(len(planner.calls), 1)
                answer, _ = rt.clarifications.respond(task, question_seq=pending["seq"], kind="custom",
                    answer="给第一次使用的同学看", request_id="flow")
                planner.transform = None
                planner.items = [item("welcome", owner="product")]
                rt.collaborate(task)
                self.assertIn(answer.payload["instruction"], [s["text"] for s in planner.calls[-1]["sources"]])
                self.assertIn(answer.payload["instruction"], [s["instruction"] for s in worker.calls[0]["customer_directions"]])

    def test_execution_question_does_not_submit_or_publish_simultaneous_deliverables(self):
        with tempfile.TemporaryDirectory() as tmp:
            worker = WorkDriver()
            original = worker.complete

            def ask(**kwargs):
                raw = json.loads(original(**kwargs).text)
                return DriverResponse(text=json.dumps({**raw, "clarification": QUESTION}))

            worker.complete = ask
            with Runtime(Path(tmp) / "worker.db", drivers={"dev": worker}) as rt:
                task = rt.create_task("整理说明")
                rt.plan_work(task, [item("draft"), item("independent", owner="product")])
                rt.collaborate(task)
                self.assertIsNotNone(rt.clarifications.pending(task))
                self.assertEqual(len(worker.calls), 1)
                self.assertEqual(list(rt.log.iter_events(task_id=task, event_types=("deliverable.published", "work.submitted"))), [])
                rt.collaborate(task)
                self.assertEqual(len(worker.calls), 1)

    def test_api_persists_answer_and_does_not_queue_it_twice(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "api.db"
            with Runtime(db) as rt:
                task = rt.create_task("欢迎说明")
                question = rt.clarifications.request(task, "store_manager", QUESTION, scope=rt.work.scope(task))
            base = start_test_web(self, str(db))
            body = {"question_seq": question.seq, "kind": "choice", "option_index": 1, "request_id": "api"}

            def post(value):
                with urlopen(Request(f"{base}/api/tasks/{task}/clarifications", data=json.dumps(value).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST"), timeout=4) as response:
                    return json.load(response)

            first = post(body)
            self.assertEqual(first["clarifications"][0]["response"]["option_index"], 1)
            second = post(body)
            self.assertEqual(second["clarifications"][0]["response_seq"], first["clarifications"][0]["response_seq"])
            with self.assertRaises(HTTPError) as error:
                post({**body, "option_index": 0})
            self.assertEqual(error.exception.code, 409)
            with self.assertRaises(HTTPError) as error:
                post({**body, "scope": {}})
            self.assertEqual(error.exception.code, 400)

    def test_api_authentication_and_origin_checks_precede_answer_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "secure.db"
            with Runtime(db) as rt:
                task = rt.create_task("说明")
                question = rt.clarifications.request(task, "store_manager", QUESTION, scope=rt.work.scope(task))
                count = rt.log.count()
            base = start_test_web(self, str(db), access_token="test-only-token")
            body = json.dumps({"question_seq": question.seq, "kind": "skip", "request_id": "auth"}).encode()
            for headers in ({}, {"X-Cater-Access-Token": "test-only-token", "Origin": "https://untrusted.example"}):
                with self.subTest(headers=headers), self.assertRaises(HTTPError) as error:
                    urlopen(Request(f"{base}/api/tasks/{task}/clarifications", data=body,
                                    headers={"Content-Type": "application/json", **headers}, method="POST"), timeout=3)
                self.assertIn(error.exception.code, (401, 403))
            with Runtime(db) as rt:
                self.assertEqual(rt.log.count(), count)
                self.assertEqual(rt.clarifications.pending(task)["seq"], question.seq)
