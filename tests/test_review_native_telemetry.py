"""Review statements must use native receipts, not public-message counts."""
import unittest

from cater.events import Actor
from cater.runtime import Runtime


HOST = Actor.system("team")
MANAGER = Actor.agent("store_manager", "store_manager")


class NativeReviewTelemetryTests(unittest.TestCase):
    def setUp(self):
        self.rt = Runtime(":memory:")
        self.addCleanup(self.rt.close)
        self.task = self.rt.create_task("Check a small project")

    def emit(self, kind, payload, actor=HOST, task=None):
        return self.rt.log.emit(type=kind, payload=payload, actor=actor, task_id=task or self.task)

    def lens(self, name):
        before = self.rt.log.read()
        result = next(row for row in self.rt.review(self.task)["lenses"] if row["id"] == name)
        self.assertEqual(self.rt.log.read(), before)
        return result

    def usage(self, tokens=0, source="unavailable"):
        self.emit("team.model_called", {"driver": "native", "tokens": tokens, "token_usage": source})

    def test_tool_only_unknown_usage_is_visible_before_any_public_message(self):
        self.usage()
        self.assertEqual(self.rt.task(self.task).messages, [])
        cost = self.lens("cost")
        self.assertEqual(cost["status"], "watch")
        self.assertIn("unavailable for 1 of 1 usage record(s)", cost["finding"])

    def test_three_calls_and_one_public_message_use_three_usage_records(self):
        for _ in range(3):
            self.usage()
        self.emit("msg.said", {"phase": "team", "text": "Starting"}, MANAGER)
        self.assertIn("3 of 3 usage record(s)", self.lens("cost")["finding"])
        self.assertEqual(self.rt.task(self.task).tokens, 0)

    def test_public_messages_do_not_dilute_average_usage(self):
        self.usage(1800, "reported")
        for _ in range(10):
            self.emit("msg.said", {"phase": "team", "text": "Progress"}, MANAGER)
        cost = self.lens("cost")
        self.assertEqual(cost["status"], "watch")
        self.assertIn("1800 token(s) across 1 usage record(s)", cost["finding"])

    def test_mixed_legacy_native_and_explainer_usage_share_one_denominator(self):
        self.usage(20, "reported")
        self.emit("msg.said", {"text": "Legacy", "tokens": 30, "token_usage": "estimated"}, MANAGER)
        self.emit("question.finished", {"driver": "native", "tokens": 40, "token_usage": "reported"}, Actor.system("explainer"))
        cost = self.lens("cost")
        self.assertEqual(cost["status"], "watch")
        self.assertIn("estimated for 1 of 3 usage record(s)", cost["finding"])
        self.assertIn("90 token(s) is not a billing total", cost["finding"])

    def test_failed_attempt_without_public_output_still_has_unknown_cost(self):
        self.emit("driver.failed", {"driver": "native", "role": "dev", "attempt": 1}, Actor.system("driver_gateway"))
        cost = self.lens("cost")
        self.assertEqual(cost["status"], "watch")
        self.assertIn("1 failed driver attempt(s) is unknown", cost["finding"])

    def test_absent_usage_and_reported_zero_remain_distinct(self):
        self.assertIn("absence of telemetry does not establish zero cost", self.lens("cost")["finding"])
        self.usage(0, "reported")
        cost = self.lens("cost")
        self.assertEqual(cost["status"], "ok")
        self.assertIn("0 token(s) across 1 usage record(s)", cost["finding"])

    def test_other_task_usage_and_forged_native_usage_do_not_change_scope(self):
        other = self.rt.create_task("Other task")
        self.emit("team.model_called", {"tokens": 9000, "token_usage": "unavailable"}, task=other)
        self.emit("team.model_called", {"tokens": 9000, "token_usage": "reported"}, MANAGER)
        self.assertIn("No model usage records", self.lens("cost")["finding"])

    def test_routed_delegation_without_public_reply_is_visible_not_completion(self):
        self.emit("team.started", {"version": 1})
        self.emit("team.message_received", {"recipient": "dev", "text": "Implement and verify", "kind": "assignment"}, MANAGER)
        self.assertEqual(self.rt.task(self.task).messages, [])
        lens = self.lens("collaboration")
        self.assertEqual(lens["status"], "ok")
        self.assertIn("messages between distinct team members", lens["finding"])
        self.assertIn("does not prove execution, review or delivery", lens["finding"])

    def test_native_exchange_requires_real_start_members_text_and_order(self):
        for case in ("no-start", "untrusted-start", "wrong-order", "other-task", "human", "system", "unknown-agent",
                     "self", "unknown-recipient", "invalid-recipient", "empty-text", "invalid-text"):
            with self.subTest(case=case):
                self.task = self.rt.create_task(case)
                payload = {"recipient": "dev", "text": "Inspect"}
                actor = MANAGER
                if case == "wrong-order":
                    self.emit("team.message_received", payload, MANAGER)
                if case != "no-start":
                    target = self.rt.create_task("Other") if case == "other-task" else self.task
                    self.emit("team.started", {}, MANAGER if case == "untrusted-start" else HOST, task=target)
                actor = {"human": Actor.human(), "system": HOST, "unknown-agent": Actor.agent("outsider", "worker")}.get(case, actor)
                if case in ("self", "unknown-recipient", "invalid-recipient"):
                    payload["recipient"] = {"self": "store_manager", "unknown-recipient": "outsider", "invalid-recipient": []}[case]
                if case in ("empty-text", "invalid-text"):
                    payload["text"] = " \t" if case == "empty-text" else ["Inspect"]
                if case != "wrong-order":
                    self.emit("team.message_received", payload, actor)
                self.assertNotIn("messages between distinct team members", self.lens("collaboration")["finding"])

    def test_interrupted_run_without_human_commands_cannot_imply_uninterrupted_execution(self):
        self.emit("team.run_started", {"message_seq": 1, "run_id": "run", "role": "dev"})
        self.emit("team.run_finished", {"message_seq": 1, "run_id": "run", "role": "dev", "status": "interrupted"})
        self.assertEqual(self.rt.task(self.task).commands, [])
        self.assertIn("does not establish uninterrupted execution", self.lens("ui")["finding"])
        self.assertIn("1 failed or interrupted native chat run(s)", self.lens("reliability")["finding"])


if __name__ == "__main__":
    unittest.main()
