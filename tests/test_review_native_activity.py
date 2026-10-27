"""Silent native tool turns are activity, not delivery or legacy profiles."""
import unittest

from cater.events import Actor
from cater.runtime import Runtime


HOST = Actor.system("team")


class NativeActivityReviewTests(unittest.TestCase):
    def setUp(self):
        self.rt = Runtime(":memory:")
        self.addCleanup(self.rt.close)
        self.task = self.rt.create_task("Inspect an existing project")

    def emit(self, kind, payload, *, task=None, actor=HOST):
        return self.rt.log.emit(type=kind, actor=actor, task_id=task or self.task, payload=payload)

    def start(self, *, task=None, actor=HOST, **fields):
        return self.emit("team.run_started", {
            "run_id": "native-run", "role": "store_manager", "message_seq": 1, **fields,
        }, task=task, actor=actor)

    def call(self, *, task=None, actor=HOST, **fields):
        return self.emit("team.model_called", {
            "run_id": "native-run", "role": "store_manager", "driver": "native",
            "tokens": 1200, "token_usage": "reported", **fields,
        }, task=task, actor=actor)

    def review(self, *, workspace=False):
        before = self.rt.log.read()
        result = self.rt.review(None if workspace else self.task)
        self.assertEqual(self.rt.log.read(), before, "Review must not start or record work")
        return {row["id"]: row for row in result["lenses"]}

    def test_tool_only_activity_is_not_misreported_as_work_never_started(self):
        self.start()
        for _ in range(3):
            self.call()
        self.assertEqual(self.rt.task(self.task).messages, [])
        lens = self.review()["product"]
        self.assertEqual(lens["status"], "watch")
        self.assertIn("1 active task(s)", lens["finding"])
        self.assertIn("recorded native model activity", lens["finding"])
        self.assertIn("historical", lens["finding"])
        self.assertIn("not current execution or delivery", lens["finding"])
        self.assertNotIn("have not received agent work", lens["finding"])
        self.assertIn("Do not automatically replay", lens["action"])
        self.assertEqual(self.rt.task(self.task).tokens, 3600)

    def test_missing_legacy_profiles_does_not_require_running_a_demo(self):
        self.start()
        self.call()
        self.assertEqual(self.rt.profiles(), {})
        lens = self.review()["adoption"]
        self.assertEqual(lens["status"], "watch")
        self.assertIn("No message/failure-derived agent profiles", lens["finding"])
        self.assertIn("native model activity", lens["finding"])
        self.assertNotIn("Run the demo", lens["action"])
        self.assertIn("not require extra agents", lens["action"])
        self.assertEqual(self.rt.profiles(), {})

    def test_receipt_does_not_assert_success_after_failure_or_unknown_usage(self):
        self.start()
        self.call(tokens=0, token_usage="unavailable", response_status="protocol_error")
        self.emit("team.run_finished", {
            "run_id": "native-run", "role": "store_manager", "message_seq": 1,
            "status": "failed", "failure_stage": "response_handling",
        })
        lenses = self.review()
        self.assertEqual(lenses["product"]["status"], "watch")
        self.assertIn("not current execution or delivery", lenses["product"]["finding"])
        self.assertIn("unavailable", lenses["cost"]["finding"])
        self.assertIn("1 failed or interrupted", lenses["reliability"]["finding"])

    def test_old_activity_after_objective_change_is_explicitly_historical(self):
        self.start()
        self.call()
        self.emit("team.objective_updated", {"goal": "A different project"})
        lens = self.review()["product"]
        self.assertIn("historical", lens["finding"])
        self.assertIn("current objective", lens["action"])
        self.assertIn("not current execution or delivery", lens["finding"])
        self.assertEqual(self.rt.task(self.task).goal, "A different project")

    def test_native_activity_does_not_hide_another_unobserved_task(self):
        self.start()
        self.call()
        other = self.rt.create_task("No work yet")
        lens = self.review(workspace=True)["product"]
        self.assertEqual(lens["status"], "needs_attention")
        self.assertIn("1 active task(s) without public messages have recorded native model activity", lens["finding"])
        self.assertIn("1 active task(s) have neither public messages nor linked native model receipts", lens["finding"])
        self.assertIn(self.task, lens["finding"])
        self.assertIn(other, lens["finding"])

    def test_activity_in_other_task_does_not_cover_selected_task(self):
        other = self.rt.create_task("Another group")
        self.start(task=other)
        self.call(task=other)
        lenses = self.review()
        self.assertNotIn("recorded native model activity", lenses["product"]["finding"])
        self.assertEqual(lenses["product"]["status"], "needs_attention")
        self.assertNotIn("native model activity", lenses["adoption"]["finding"])

    def test_missing_receipts_are_not_proof_of_zero_or_unstarted_work(self):
        self.start()
        lenses = self.review()
        lens = lenses["product"]
        self.assertEqual(lens["status"], "needs_attention")
        self.assertIn("absence is not proof that no work occurred", lens["finding"])
        self.assertIn("pending operations", lens["action"])
        self.assertNotIn("Run a collaboration round", lens["action"])
        self.assertNotIn("Run the demo", lenses["adoption"]["action"])
        self.assertIn("does not establish an unused model", lenses["adoption"]["action"])

    def test_only_trusted_scoped_ordered_run_and_call_pairs_count(self):
        cases = ("no-start", "forged-start", "forged-call", "wrong-order", "other-task", "run-mismatch",
                 "role-mismatch", "blank-role", "empty-run", "blank-run", "list-run", "list-role", "invalid-message",
                 "boolean-message", "missing-driver", "blank-driver", "invalid-driver", "system-impostor")
        for case in cases:
            with self.subTest(case=case):
                self.task = self.rt.create_task(case)
                fields = {"run_id": []} if case == "list-run" else {"role": []} if case == "list-role" else {}
                if case == "empty-run":
                    fields["run_id"] = ""
                if case == "blank-run":
                    fields["run_id"] = " \t"
                if case == "blank-role":
                    fields["role"] = " \t"
                start_fields = {**fields}
                if case in ("invalid-message", "boolean-message"):
                    start_fields["message_seq"] = 0 if case == "invalid-message" else True
                if case == "wrong-order":
                    self.call()
                if case != "no-start":
                    self.start(task=self.rt.create_task("Foreign") if case == "other-task" else None,
                               actor=Actor.agent("team", "team") if case == "forged-start" else HOST, **start_fields)
                if case == "run-mismatch":
                    fields["run_id"] = "different-run"
                if case == "role-mismatch":
                    fields["role"] = "dev"
                if case in ("missing-driver", "invalid-driver"):
                    fields["driver"] = "" if case == "missing-driver" else []
                if case == "blank-driver":
                    fields["driver"] = " \t"
                if case != "wrong-order":
                    actor = Actor.agent("team", "team") if case == "forged-call" else HOST
                    if case == "system-impostor":
                        actor = Actor(id="team", role="other", kind="system")
                    self.call(actor=actor, **fields)
                lenses = self.review()
                self.assertNotIn("recorded native model activity", lenses["product"]["finding"])
                self.assertNotIn("native model activity", lenses["adoption"]["finding"])

    def test_external_usage_receipt_counts_without_claiming_known_tokens(self):
        self.start()
        self.call(external_started_seq=4, tokens=0, token_usage="unavailable")
        lenses = self.review()
        self.assertIn("recorded native model activity", lenses["product"]["finding"])
        self.assertIn("unavailable", lenses["cost"]["finding"])

    def test_visible_legacy_profile_behavior_is_preserved(self):
        self.emit("msg.said", {"text": "A plain answer", "phase": "execution"},
                  actor=Actor.agent("dev", "developer"))
        lenses = self.review()
        self.assertEqual(lenses["product"]["status"], "ok")
        self.assertEqual(lenses["adoption"]["status"], "ok")
        self.assertIn("One agent profile is visible in workspace history", lenses["adoption"]["finding"])

    def test_retired_role_still_has_historical_model_activity(self):
        self.start(role="retired-role")
        self.call(role="retired-role")
        self.assertIn("recorded native model activity", self.review()["product"]["finding"])

    def test_identity_values_are_not_normalized_into_a_false_pair(self):
        for fields in ({"run_id": " native-run"}, {"role": "store_manager "}):
            with self.subTest(fields=fields):
                self.task = self.rt.create_task("Identity mismatch")
                self.start()
                self.call(**fields)
                self.assertNotIn("recorded native model activity", self.review()["product"]["finding"])

    def test_late_old_run_after_goal_change_remains_only_historical(self):
        self.start()
        self.emit("team.objective_updated", {"goal": "Inspect an existing project"})
        self.call()
        lens = self.review()["product"]
        self.assertIn("historical", lens["finding"])
        self.assertIn("not current execution or delivery", lens["finding"])

    def test_workspace_profile_is_not_claimed_as_selected_task_activity(self):
        other = self.rt.create_task("Old group")
        self.emit("msg.said", {"text": "An old answer", "phase": "team"}, task=other,
                  actor=Actor.agent("dev", "developer"))
        lenses = self.review()
        self.assertIn("workspace history", lenses["adoption"]["finding"])
        self.assertNotIn("active agent", lenses["adoption"]["finding"])
        self.assertEqual(lenses["product"]["status"], "needs_attention")

    def test_full_event_list_is_intersected_with_selected_tasks(self):
        from cater.review import build_review

        other = self.rt.create_task("Not selected")
        self.start(task=other)
        self.call(task=other)
        result = build_review(tasks=[self.rt.task(self.task)], events=self.rt.log.read(),
                              profiles={}, chain=self.rt.verify_chain())
        lenses = {row["id"]: row for row in result["lenses"]}
        self.assertNotIn("recorded native model activity", lenses["product"]["finding"])
        self.assertNotIn("native model activity", lenses["adoption"]["finding"])

    def test_completed_task_is_not_reopened_by_silent_history(self):
        self.start()
        self.call()
        self.emit("team.objective_completed", {"summary": "A text result", "verification": "Text checked"})
        self.assertEqual(self.review()["product"]["status"], "ok")
        self.assertEqual(self.rt.task(self.task).status, "done")

    def test_fresh_workspace_keeps_first_task_guidance(self):
        with Runtime(":memory:") as rt:
            lenses = {row["id"]: row for row in rt.review()["lenses"]}
            self.assertIn("No user-visible task", lenses["product"]["finding"])
            self.assertEqual(lenses["product"]["status"], "needs_attention")


if __name__ == "__main__":
    unittest.main()
