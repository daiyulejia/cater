import unittest

import test_conversation


class ConversationControlTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_conversation.ConversationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_semantic_pause_and_resume_work_through_chat_without_changing_goal(self):
        f = self.fixture
        scope = f.rt.work.scope(f.task)
        f.suggest("pause_task", "先停一会")
        _, result = f.resolve("先停一会")
        self.assertEqual(result["status"], "finished")
        self.assertEqual(result["next_action"], "none")
        self.assertEqual(f.rt.execution_control.view(f.task)["state"], "paused")
        f.suggest("resume_task", "接着做吧")
        _, result = f.resolve("接着做吧", continue_work=True)
        self.assertEqual(result["next_action"], "run")
        self.assertEqual(f.rt.execution_control.view(f.task)["state"], "running")
        self.assertEqual(f.rt.work.scope(f.task), scope)
        self.assertEqual(f.rt.task(f.task).goal, "制作五子棋")

    def test_new_direct_pause_during_model_call_fences_old_resume(self):
        f = self.fixture
        f.suggest("resume_task", "继续做")
        f.driver.callback = lambda: f.rt.execution_control.change(f.task, "pause", request_id="late-button-pause")
        _, result = f.resolve("继续做", continue_work=True)
        self.assertEqual(result["status"], "outdated")
        self.assertEqual(f.rt.execution_control.view(f.task)["state"], "paused")
        self.assertEqual(f.events("human.commanded"), [])

    def test_fifo_pause_then_resume_received_before_processing_are_both_adopted(self):
        f = self.fixture
        f.service.receive(f.task, "暂停吧")
        f.service.receive(f.task, "继续吧", continue_work=True)
        f.suggest("pause_task", "暂停吧")
        self.assertEqual(f.service.resolve_next(f.task)["status"], "finished")
        f.suggest("resume_task", "继续吧")
        self.assertEqual(f.service.resolve_next(f.task)["next_action"], "run")
        self.assertEqual(f.rt.execution_control.view(f.task)["state"], "running")

    def test_cancellation_does_not_delete_messages_and_cannot_resume(self):
        f = self.fixture
        f.suggest("cancel_task", "这项不用做了")
        _, result = f.resolve("这项不用做了")
        self.assertEqual(result["status"], "finished")
        f.suggest("resume_task", "继续")
        _, result = f.resolve("继续", continue_work=True)
        self.assertEqual(result["next_action"], "none")
        self.assertEqual(f.rt.execution_control.view(f.task)["state"], "cancelled")
        self.assertEqual(len(f.events("conversation.received")), 2)


if __name__ == "__main__":
    unittest.main()
