"""Public contact and internal role communication have distinct recipients."""
import unittest

from test_simple_team import SimpleTeamTests, turn


class TeamAudienceTests(SimpleTeamTests):
    def test_peer_output_is_internal_and_only_manager_delivery_is_official(self):
        self.driver.steps = {
            self.manager: [turn("I will coordinate this.", ("delegate", {
                "recipient": self.worker, "instruction": "Check the result and return it to me"})),
                turn("Please clarify the input if needed.", ("wait_for_reply", {})),
                turn("I received the stage result.", ("finish_work", {
                    "summary": "Final delivery", "verification": "Fixture only"})),
                turn("This is the final delivery for the customer.")],
            self.worker: [turn("The stage result is ready for the manager.")],
        }
        self.rt.team.receive(self.task, "Check and deliver", request_id="audience-delivery")
        self.run_ok()
        said = self.events("msg.said")
        worker, = [e for e in said if e.actor.id == self.worker]
        self.assertEqual(worker.payload["audience"], "team")
        self.assertEqual(worker.payload["recipient"], self.manager)
        self.assertNotIn("delivery_seq", worker.payload)
        first, = [e for e in said if e.payload["text"] == "I will coordinate this."]
        self.assertEqual(first.payload["audience"], "customer")
        self.assertEqual(first.payload["response_kind"], "progress")
        question, = [e for e in said if e.payload["text"] == "Please clarify the input if needed."]
        self.assertEqual(question.payload["response_kind"], "reply")
        completed, = self.events("team.objective_completed")
        official, = [e for e in said if "delivery_seq" in e.payload]
        self.assertEqual(official.actor.id, self.manager)
        self.assertEqual(official.payload["delivery_seq"], completed.seq)
        self.assertEqual(official.payload["run_id"], completed.payload["run_id"])
        self.assertEqual(official.payload["audience"], "customer")
        self.assertEqual(official.payload["recipient"], "human")
        self.assertEqual(official.payload["response_kind"], "reply")

    def test_progress_question_after_done_is_not_another_official_delivery(self):
        self.driver.steps[self.manager] = [turn("", ("finish_work", {
            "summary": "Original delivery", "verification": "Fixture only"})), turn("Delivered.")]
        self.rt.team.receive(self.task, "Finish", request_id="audience-finish")
        self.run_ok()
        self.driver.steps[self.manager] = [turn("The same delivery is already available.")]
        self.rt.team.receive(self.task, "Is it finished?", request_id="audience-progress")
        self.run_ok()
        latest = self.events("msg.said")[-1]
        self.assertEqual(latest.payload["audience"], "customer")
        self.assertNotIn("delivery_seq", latest.payload)
        self.assertEqual(len([e for e in self.events("msg.said") if "delivery_seq" in e.payload]), 1)

    def test_reopening_work_in_same_run_does_not_label_new_work_as_delivered(self):
        self.driver.steps[self.manager] = [turn("",
            ("finish_work", {"summary": "First delivery", "verification": "Fixture only"}),
            ("set_objective", {"goal": "Next objective"})), turn("The next objective is not delivered yet.")]
        self.rt.team.receive(self.task, "Finish one thing then adopt the next", request_id="audience-reopen")
        self.run_ok()
        latest = self.events("msg.said")[-1]
        self.assertNotIn("delivery_seq", latest.payload)
        self.assertEqual(self.rt.team.view(self.task)["status"], "running")

    def test_old_goal_interruption_does_not_bounce_obsolete_callbacks(self):
        self.driver.steps[self.manager] = [turn("", ("delegate", {
            "recipient": self.worker, "instruction": "Old implementation"})),
            turn("", ("set_objective", {"goal": "Replacement implementation"})), turn("New goal adopted.")]
        self.rt.team.receive(self.task, "Start the original work", request_id="audience-stale")
        self.run_ok()
        self.assertFalse([e for e in self.events("team.message_received") if e.payload.get("kind") in {"progress", "report"}])

    def test_repeated_finish_without_new_work_does_not_create_another_delivery(self):
        self.driver.steps[self.manager] = [turn("", ("finish_work", {
            "summary": "Delivered", "verification": "Fixture only"})), turn("Delivered.")]
        self.rt.team.receive(self.task, "Finish", request_id="audience-finish-once")
        self.run_ok()
        revision = self.rt.team.view(self.task)["revision"]
        self.driver.steps[self.manager] = [turn("", ("finish_work", {
            "summary": "Still delivered", "verification": "No new work"})), turn("The original delivery is still available.")]
        self.rt.team.receive(self.task, "Is it done?", request_id="audience-redundant-finish")
        self.run_ok()
        self.assertEqual(len(self.events("team.objective_completed")), 1)
        self.assertEqual(self.rt.team.view(self.task)["revision"], revision)
        self.assertNotIn("delivery_seq", self.events("msg.said")[-1].payload)

    def test_exact_explicit_result_does_not_trigger_identical_automatic_update(self):
        self.driver.steps = {
            self.manager: [turn("", ("delegate", {"recipient": self.worker, "instruction": "Check the result"})),
                           turn("", ("wait_for_reply", {})), turn("Result received.", ("wait_for_reply", {}))],
            self.worker: [turn("", ("send_message", {"recipient": self.manager, "text": "The check is complete."})),
                          turn("The check is complete.")],
        }
        self.rt.team.receive(self.task, "Check", request_id="audience-one-parent-update")
        self.run_ok()
        updates = [e for e in self.events("team.message_received") if e.actor.id == self.worker
                   and e.payload.get("recipient") == self.manager]
        self.assertEqual(len(updates), 1)
        self.assertTrue(updates[0].payload["visible"])

    def test_progress_notice_does_not_swallow_a_later_failed_verification(self):
        self.driver.steps = {
            self.manager: [turn("", ("delegate", {"recipient": self.worker, "instruction": "Check permissions"})),
                turn("", ("wait_for_reply", {})), turn("", ("wait_for_reply", {})), turn("", ("wait_for_reply", {}))],
            self.worker: [turn("", ("send_message", {"recipient": self.manager, "text": "Still checking; progress only."})),
                turn("Final conclusion: permission verification failed; repair is needed.")],
        }
        self.rt.team.receive(self.task, "Check permissions", request_id="audience-progress-then-failure")
        self.run_ok()
        final, = [e for e in self.events("team.message_received") if e.payload.get("kind") == "report"
                   and e.payload.get("recipient") == self.manager]
        self.assertIn("verification failed", final.payload["text"])
        self.assertTrue(any(c["context"]["message"]["seq"] == final.seq for c in self.driver.calls))


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(TeamAudienceTests(name) for name in TeamAudienceTests.__dict__ if name.startswith("test_"))
