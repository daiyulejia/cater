from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from cater.events import Actor
from cater.questionnaires import normalize_answers, normalize_questions
from cater.runtime import Runtime
from cater.team import HOST
from cater.work import WorkConflict
from test_simple_team import TeamDriver, turn


def proposal():
    return {"title": "了解你的职业方向", "questions": [
        {"id": "stage", "question": "目前的阶段？", "selection": "single", "options": [
            {"id": "working", "label": "在职", "recommended": True}, {"id": "student", "label": "在读"}]},
        {"id": "goals", "question": "想解决哪些问题？", "selection": "multiple", "options": [
            {"id": "change", "label": "转行"}, {"id": "plan", "label": "行动计划", "recommended": True}]},
        {"id": "limits", "question": "其他限制？", "selection": "single", "options": []}]}


def answers():
    return [
        {"question_id": "stage", "option_ids": ["working"], "text": "移动端工程师", "skipped": False},
        {"question_id": "goals", "option_ids": ["change", "plan"], "text": "", "skipped": False},
        {"question_id": "limits", "option_ids": [], "text": "", "skipped": True}]


class QuestionnaireTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        config = json.loads(Path("cater/packs/default_roles.json").read_text(encoding="utf-8"))
        config["organization"]["leader"]["driver"] = "team_test"
        for role in config["roles"]:
            role["driver"] = "team_test"
        roles = root / "roles.json"
        roles.write_text(json.dumps(config), encoding="utf-8")
        self.driver = TeamDriver()
        self.rt = Runtime(root / "test.db", roles_path=roles, drivers={"team_test": self.driver})
        self.addCleanup(self.rt.close)
        self.task = self.rt.create_task("职业规划")
        self.manager = self.rt.organization.leader.id
        self.worker = self.rt.worker_roles[0].id
        self.rt.team.enable(self.task)

    def events(self, kind):
        return list(self.rt.log.iter_events(task_id=self.task, event_types=(kind,)))

    def ask(self):
        self.driver.steps[self.manager] = [turn("了解几件事。", ("request_questions", proposal()))]
        self.rt.team.receive(self.task, "帮我做职业规划", request_id="first-message")
        self.rt.collaborate(self.task)
        return self.rt.questionnaires.view(self.task)[-1]

    def reply(self, question, **changes):
        return self.rt.questionnaires.respond(self.task, **{
            "question_seq": question["seq"], "answers": answers(), "request_id": "batch-response-001", **changes})

    def test_tool_stops_for_customer_without_extra_call_then_resumes_once(self):
        question = self.ask()
        self.assertEqual(question["status"], "pending")
        self.assertEqual(len(self.driver.calls), 1)
        self.assertEqual(self.rt.team.view(self.task)["runs"][-1]["status"], "waiting")
        message, created = self.reply(question)
        self.assertTrue(created)
        self.assertEqual(message.actor, Actor.human())
        self.assertEqual(message.payload["recipient"], self.manager)
        self.assertIn("在职；移动端工程师", message.payload["text"])
        self.assertIn("转行；行动计划", message.payload["text"])
        self.assertIn("不代表同意或授权", message.payload["text"])
        self.assertEqual(self.rt.questionnaires.view(self.task)[-1]["response"]["answers"], answers())
        self.assertEqual(self.reply(question), (message, False))
        self.driver.steps[self.manager] = [turn("按你的背景继续整理规划，不再重新提问。")]
        self.rt.collaborate(self.task)
        self.rt.collaborate(self.task)
        self.assertEqual(len(self.driver.calls), 2)
        self.assertEqual(self.driver.calls[-1]["context"]["message"]["questionnaire"]["answers"], answers())
        self.assertTrue(self.rt.verify_chain().ok)

    def test_same_request_cannot_change_answer_or_collide_with_chat(self):
        question = self.ask()
        self.reply(question)
        changed = answers()
        changed[0]["text"] = "改答案"
        with self.assertRaises(WorkConflict):
            self.reply(question, answers=changed)
        self.rt.team.receive(self.task, "普通消息", request_id="existing-chat")
        with self.assertRaises(WorkConflict):
            self.reply(question, request_id="existing-chat")

    def test_arbitrary_chat_supersedes_pending_form_without_parsing_keywords(self):
        question = self.ask()
        self.rt.team.receive(self.task, "换一下，我直接说我的背景", request_id="free-text-answer")
        self.assertEqual(self.rt.questionnaires.view(self.task)[-1]["status"], "stale")
        before = self.rt.log.head_hash()
        with self.assertRaises(WorkConflict):
            self.reply(question)
        self.assertEqual(self.rt.log.head_hash(), before)

    def test_objective_change_or_replacement_stales_but_answered_stays_answered(self):
        question = self.ask()
        original = self.events("team.questions_requested")[-1]
        self.rt.log.emit(type="team.questions_requested", actor=HOST, task_id=self.task, payload=original.payload)
        self.assertEqual([q["status"] for q in self.rt.questionnaires.view(self.task)], ["stale", "pending"])
        replacement = self.rt.questionnaires.view(self.task)[-1]
        self.reply(replacement)
        self.rt.log.emit(type="team.objective_updated", actor=HOST, task_id=self.task, payload={"goal": "新目标"})
        self.assertEqual([q["status"] for q in self.rt.questionnaires.view(self.task)], ["stale", "answered"])
        self.assertNotEqual(question["seq"], replacement["seq"])

    def test_paused_response_does_not_write_and_same_answer_can_retry_after_resume(self):
        question = self.ask()
        self.rt.execution_control.change(self.task, "pause", request_id="pause-batch")
        before = self.rt.log.head_hash()
        with self.assertRaises(WorkConflict):
            self.reply(question)
        self.assertEqual(self.rt.log.head_hash(), before)
        self.rt.execution_control.change(self.task, "resume", request_id="resume-batch")
        self.assertTrue(self.reply(question)[1])

    def worker_asks(self):
        self.driver.steps = {
            self.manager: [turn("请接手", ("delegate", {"recipient": self.worker, "instruction": "根据顾客背景整理规划"})),
                           turn("等待伙伴")],
            self.worker: [turn("补充几个信息", ("request_questions", proposal()))]}
        self.rt.team.receive(self.task, "职业规划", request_id="assign-career")
        self.rt.collaborate(self.task)
        self.assertEqual(len(self.driver.calls), 3)
        return self.rt.questionnaires.view(self.task)[-1]

    def test_worker_question_answer_keeps_original_assignment_and_reports_to_manager(self):
        question = self.worker_asks()
        self.assertEqual(question["role_id"], self.worker)
        message, _ = self.reply(question)
        self.assertEqual(message.payload["recipient"], self.worker)
        self.driver.steps[self.worker] = [turn("规划草稿已整理，交回店长。")]
        self.driver.steps[self.manager] = [turn("我来给你汇总。")]
        self.rt.collaborate(self.task)
        worker_context = self.driver.calls[-2]["context"]
        self.assertIsNotNone(worker_context["active_assignment"])
        self.assertEqual(worker_context["active_assignment"]["text"], "根据顾客背景整理规划")
        reports = [e for e in self.events("team.message_received") if e.payload["kind"] == "report"]
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0].payload["reply_to"], worker_context["active_assignment"]["seq"])
        self.assertTrue(self.rt.verify_chain().ok)

    def test_queued_answer_does_not_restart_old_work_after_objective_change(self):
        question = self.worker_asks()
        self.reply(question)
        self.rt.log.emit(type="team.objective_updated", task_id=self.task, actor=HOST,
                         payload={"goal": "改为翻译文档"})
        before = len(self.driver.calls)
        self.driver.steps[self.worker] = [turn("不应被调用", ("publish_artifact", {
            "filename": "old.md", "content": "过期规划", "summary": "旧任务", "format": "markdown"}))]
        self.rt.collaborate(self.task)
        self.assertEqual(len(self.driver.calls), before)
        self.assertFalse(self.rt.task(self.task).deliverables)
        self.assertEqual(self.rt.team.view(self.task)["runs"][-1]["status"], "interrupted")

    def test_new_text_after_batch_keeps_unfinished_assignment_instead_of_false_report(self):
        question = self.worker_asks()
        self.reply(question)
        name = self.rt._role_by_id(self.worker).name
        message = self.rt.team.receive(self.task, f"@{name} 还要考虑我的家庭安排", request_id="new-after-answer")
        self.driver.steps[self.worker] = [turn("把家庭安排纳入同一份规划，现交回草稿。")]
        self.driver.steps[self.manager] = [turn("收到更新的规划。")]
        self.rt.collaborate(self.task)
        context = next(c["context"] for c in self.driver.calls if c["context"]["message"]["seq"] == message.seq)
        self.assertIsNotNone(context["active_assignment"])
        self.assertEqual(len(context["open_assignments"]), 1)
        reports = [e for e in self.events("team.message_received") if e.payload["kind"] == "report"]
        self.assertEqual(len(reports), 1)
        self.assertIn("家庭安排", reports[0].payload["text"])
        self.assertNotIn("收到顾客的新消息", reports[0].payload["text"])

    def test_two_members_questions_can_both_be_answered_without_invalidating_each_other(self):
        other = next(r.id for r in self.rt.worker_roles if r.id != self.worker)
        self.driver.steps = {
            self.manager: [turn("", ("delegate", {"recipient": self.worker, "instruction": "了解背景"}),
                               ("delegate", {"recipient": other, "instruction": "了解偏好"})), turn("等待补充")],
            self.worker: [turn("", ("request_questions", proposal()))],
            other: [turn("", ("request_questions", proposal()))]}
        self.rt.team.receive(self.task, "规划", request_id="parallel-questions")
        self.rt.collaborate(self.task)
        first, second = self.rt.questionnaires.view(self.task)
        self.reply(first)
        self.assertEqual(self.rt.questionnaires.view(self.task)[1]["status"], "pending")
        self.reply(second, request_id="second-batch-response")
        self.assertEqual([q["status"] for q in self.rt.questionnaires.view(self.task)], ["answered", "answered"])

    def test_pause_between_tool_start_and_request_does_not_publish_question(self):
        emit = self.rt.log.emit

        def pause_after_tool_start(**kwargs):
            event = emit(**kwargs)
            if event.type == "team.tool_started" and event.payload["name"] == "request_questions":
                self.rt.execution_control.change(self.task, "pause", request_id="pause-while-asking")
            return event

        self.rt.log.emit = pause_after_tool_start
        self.driver.steps[self.manager] = [turn("", ("request_questions", proposal()))]
        self.rt.team.receive(self.task, "规划", request_id="race-question-start")
        self.rt.collaborate(self.task)
        self.assertEqual(self.rt.questionnaires.view(self.task), [])
        self.assertEqual(self.rt.team.view(self.task)["runs"][-1]["status"], "interrupted")

    def test_request_validates_nested_fields_and_no_default_answer(self):
        normalized = normalize_questions(**proposal())
        self.assertTrue(normalized["questions"][0]["options"][0]["recommended"])
        self.assertNotIn("answer", normalized)
        variants = []
        for modify in (
            lambda p: p["questions"][0].update(selection="checkbox"),
            lambda p: p["questions"][0]["options"][0].update(recommended="yes"),
            lambda p: p["questions"][0]["options"][0].update(id="bad id"),
            lambda p: p["questions"][1].update(id="stage"),
            lambda p: p["questions"][0].update(options=[{"id": "x", "label": "重复"}] * 2),
            lambda p: p["questions"][0].update(extra="ignored?"),
        ):
            value = proposal()
            modify(value)
            variants.append(value)
        for value in variants:
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_questions(**value)

    def test_invalid_partial_duplicate_mixed_skip_and_foreign_answers_are_atomic(self):
        question = self.ask()
        variants = [[], answers()[:-1], answers() + answers()[:1], {"answers": answers()}]
        for field, value in (("option_ids", ["student", "working"]), ("option_ids", ["unknown"]),
                             ("option_ids", ["working", "working"]), ("option_ids", [{}]),
                             ("skipped", True), ("skipped", 1), ("text", "x" * 2001),
                             ("question_id", "missing")):
            a = answers()
            a[0][field] = value
            variants.append(a)
        empty = answers()
        empty[0].update(option_ids=[], text="")
        variants.append(empty)
        for value in variants:
            before = self.rt.log.head_hash()
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.reply(question, answers=value)
            self.assertEqual(self.rt.log.head_hash(), before)
        with self.assertRaises(ValueError):
            self.reply(question, question_seq=True)

    def test_custom_only_all_skip_and_order_canonicalization(self):
        questions = normalize_questions(**proposal())["questions"]
        a = answers()
        a[0].update(option_ids=[], text="只用我写的")
        a[1]["option_ids"].reverse()
        normalized = normalize_answers(questions, list(reversed(a)))
        self.assertEqual(normalized[0]["text"], "只用我写的")
        self.assertEqual(normalized[1]["option_ids"], ["change", "plan"])
        a = [{"question_id": q["id"], "option_ids": [], "text": "", "skipped": True} for q in questions]
        self.assertEqual(normalize_answers(questions, a), a)

    def test_unauthenticated_question_event_is_not_a_customer_form(self):
        self.rt.log.emit(type="team.questions_requested", actor=Actor.agent(self.worker, "worker"),
                         task_id=self.task, payload={**proposal(), "role_id": self.manager})
        self.assertEqual(self.rt.questionnaires.view(self.task), [])


if __name__ == "__main__":
    unittest.main()
