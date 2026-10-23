"""Question answers resume work; later customer chat must not return that work."""

import unittest

from cater.team import HOST, _reported_messages
from test_questionnaires import QuestionnaireTests
from test_simple_team import turn


class TeamQuestionContextBoundaryTests(QuestionnaireTests):
    def original_assignment(self):
        return next(event for event in self.events("team.message_received")
                    if event.payload.get("kind") == "assignment")

    def call_for(self, message):
        calls = [call for call in self.driver.calls
                 if call["context"]["message"]["seq"] == message.seq]
        self.assertEqual(len(calls), 1)
        return calls[0]

    def assert_healthy(self):
        self.assertFalse([run for run in self.rt.team.view(self.task)["runs"]
                          if run["status"] == "failed"])
        self.assertTrue(self.rt.verify_chain().ok)

    def consume_answer_and_wait(self, message):
        self.driver.steps[self.worker] = [turn("", ("wait_for_reply", {}))]
        self.driver.steps[self.manager] = [turn("", ("wait_for_reply", {}))]
        self.rt.collaborate(self.task)
        call = self.call_for(message)
        assignment = self.original_assignment()
        self.assertEqual(call["context"]["active_assignment"]["seq"], assignment.seq)
        self.assertNotIn("reply_to_customer", {tool["name"] for tool in call["tools"]})
        self.assertNotIn(assignment.seq, _reported_messages(self.rt.team._messages(self.task)))
        run = next(run for run in self.rt.team.view(self.task)["runs"]
                   if run["message_seq"] == message.seq)
        self.assertEqual(run["status"], "waiting")
        self.assert_healthy()

    def assert_later_chat_is_independent(self, *, old_work_open=True):
        assignment = self.original_assignment()
        message = self.rt.team.receive(
            self.task, f"@{self.worker} What does the recommended option mean?",
            request_id="ordinary-question-after-answer")
        before = len(self.driver.calls)
        self.driver.steps[self.worker] = [turn("It is the suggested option; this only explains the label.")]
        self.driver.steps[self.manager] = [turn("", ("wait_for_reply", {}))]
        self.rt.collaborate(self.task)
        call = self.call_for(message)
        reports = [event for event in self.events("team.message_received")
                   if event.payload.get("kind") == "report"
                   and event.payload.get("reply_to") == assignment.seq]
        self.assertFalse(reports, "An ordinary explanation falsely returned the unfinished assignment")
        self.assertIsNone(call["context"]["active_assignment"])
        self.assertIn("reply_to_customer", {tool["name"] for tool in call["tools"]})
        self.assertNotIn(assignment.seq, _reported_messages(self.rt.team._messages(self.task)))
        self.assertEqual(assignment.seq in {row["seq"] for row in call["context"]["open_assignments"]},
                         old_work_open)
        replies = [event for event in self.events("msg.said")
                   if event.payload.get("reply_to") == message.seq
                   and event.payload.get("audience") == "customer"]
        self.assertEqual(len(replies), 1)
        self.assertEqual(len(self.driver.calls) - before, 1)
        self.assert_healthy()

    def test_processed_questionnaire_answer_does_not_bind_later_ordinary_question(self):
        question = self.worker_asks()
        message, _ = self.reply(question)
        self.consume_answer_and_wait(message)
        self.assertEqual(self.rt.questionnaires.view(self.task)[-1]["status"], "answered")
        self.assert_later_chat_is_independent()

    def test_consumed_free_text_answer_does_not_bind_later_ordinary_question(self):
        self.worker_asks()
        message = self.rt.team.receive(
            self.task, f"@{self.worker} I work full time and need a family-friendly plan.",
            request_id="free-text-answer-to-worker")
        self.assertNotIn("questionnaire", message.payload)
        self.consume_answer_and_wait(message)
        self.assertEqual(self.rt.questionnaires.view(self.task)[-1]["status"], "stale")
        self.assert_later_chat_is_independent()

    def test_explicit_questionnaire_answer_keeps_original_assignment(self):
        question = self.worker_asks()
        message, _ = self.reply(question)
        self.assertEqual(message.payload["questionnaire"]["question_seq"], question["seq"])
        self.consume_answer_and_wait(message)

    def test_queued_answer_superseded_by_supplement_keeps_original_assignment(self):
        question = self.worker_asks()
        answer, _ = self.reply(question)
        supplement = self.rt.team.receive(
            self.task, f"@{self.worker} Also include my family schedule in the same plan.",
            request_id="supplement-before-answer-consumed")
        self.consume_answer_and_wait(supplement)
        self.assertFalse([call for call in self.driver.calls
                          if call["context"]["message"]["seq"] == answer.seq])
        run = next(run for run in self.rt.team.view(self.task)["runs"]
                   if run["message_seq"] == answer.seq)
        self.assertEqual(run["status"], "interrupted")
        self.assertFalse([event for event in self.events("team.message_received")
                          if event.payload.get("kind") == "report"])

    def test_new_objective_does_not_bind_old_question_assignment(self):
        question = self.worker_asks()
        message, _ = self.reply(question)
        self.consume_answer_and_wait(message)
        self.rt.log.emit(type="team.objective_updated", task_id=self.task, actor=HOST,
                         payload={"goal": "Translate a document instead of career planning"})
        self.assert_later_chat_is_independent(old_work_open=False)


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(TeamQuestionContextBoundaryTests(name)
                              for name in TeamQuestionContextBoundaryTests.__dict__
                              if name.startswith("test_"))


if __name__ == "__main__":
    unittest.main()
