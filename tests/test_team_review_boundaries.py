"""Review boundary regressions with native fake turns and disposable data."""

from __future__ import annotations

import json
import unittest

import test_team_review_cycle as review_cycle


class TeamReviewBoundaryTests(review_cycle.TeamReviewCycleTests):
    def _message(self, seq):
        return next(event for event in self.rt.team._messages(self.task) if event.seq == seq)

    def _domain_effects(self):
        kinds = ("deliverable.published", "team.review_requested", "team.review_recorded",
                 "team.objective_completed")
        return ([event for event in self.rt.log.read(task_id=self.task) if event.type in kinds],
                [event for event in self.rt.team._messages(self.task) if event.actor.kind == "agent"])

    def _assert_resubmission_has_no_effect(self, artifacts, review_seq):
        before = self._domain_effects()
        reviews = self.rt.team_reviews.view(self.task)
        result = self.request(artifacts, review_seq=review_seq)
        self.assertEqual(result["status"], "error", result)
        self.assertEqual(self._domain_effects(), before)
        self.assertEqual(self.rt.team_reviews.view(self.task), reviews)

    def _assert_finish_defers_author_cleanup(self, source, artifact):
        revision = self.rt.team.view(self.task)["revision"]

        def author_read(context, history, tools):
            self.assertEqual(context["message"]["seq"], source.seq)
            self.assertIsNone(context["active_assignment"])
            self.rt.team.receive(self.task, "Try delivery while the author reads before cleanup",
                                 request_id="boundary-finish-during-read")
            return self.native_turn("I will clean the leftover item and return the final version.",
                                    ("read_artifact", {"artifact_seq": artifact}))

        def manager_finish(context, history, tools):
            run, = [row for row in self.rt.team.view(self.task)["runs"]
                    if row["message_seq"] == source.seq]
            self.assertEqual(run["status"], "running")
            self.assertTrue(any(event.payload["name"] == "read_artifact"
                                and event.payload["result"].get("seq") == artifact
                                for event in self.events("team.tool_finished")))
            return self.native_turn("", ("finish_work", {
                "summary": "Attempted early delivery", "verification": "Fixture only",
            }))

        def author_cleanup(context, history, tools):
            self.assertEqual(json.loads(history[-1]["content"])["seq"], artifact)
            return self.native_turn("", ("publish_artifact", {
                "filename": "result.txt", "content": "After cleanup\n",
                "summary": "Clean final output", "format": "text",
            }))

        self.driver.steps[self.worker] = [author_read, author_cleanup,
                                          self.native_turn("Cleanup is finished.")]
        self.driver.steps[self.manager] = [manager_finish,
                                           self.native_turn("", ("wait_for_reply", {}))]
        self.run_team()
        result, = self.results("finish_work")
        self.assertEqual((result["status"], result.get("code")), ("error", "author_pending"))
        self.assertIn({"seq": source.seq, "author": self.worker}, result["assignments"])
        run, = [row for row in self.rt.team.view(self.task)["runs"] if row["message_seq"] == source.seq]
        self.assertEqual(run["status"], "completed")
        self.assertEqual(self.rt.task(self.task).deliverables[-1]["content"], "After cleanup\n")
        self.assertEqual(self.events("team.objective_completed"), [])
        self.assertEqual(self.rt.team.view(self.task)["revision"], revision)
        self.assertEqual(self.finish()["status"], "ok")

    def test_finish_waits_for_author_running_on_direct_customer_input(self):
        artifact = self.publish("Before cleanup\n")
        worker = self.rt.team._role(self.worker)
        source = self.rt.team.receive(self.task, f"@{worker.name} Check and clean your output",
                                      request_id="boundary-author-direct-input")
        self.assertEqual(source.payload["recipient"], self.worker)
        self.assertEqual(self.rt.team_reviews.view(self.task), [])
        self._assert_finish_defers_author_cleanup(source, artifact)

    def test_finish_waits_for_unclaimed_customer_input_to_author(self):
        artifact = self.publish("Before cleanup\n")
        worker = self.rt.team._role(self.worker)
        source = self.rt.team.receive(self.task, f"@{worker.name} Inspect your output before delivery",
                                      request_id="boundary-author-queued-input")
        self.assertEqual(source.payload["recipient"], self.worker)
        revision = self.rt.team.view(self.task)["revision"]
        result = self.finish()
        self.assertEqual((result["status"], result.get("code")), ("error", "author_pending"))
        self.assertIn({"seq": source.seq, "author": self.worker}, result["assignments"])
        self.assertFalse(any(row["message_seq"] == source.seq for row in self.rt.team.view(self.task)["runs"]))
        self.assertEqual(self.events("team.objective_completed"), [])
        self.assertEqual(self.rt.team.view(self.task)["revision"], revision)

        self.driver.steps[self.worker] = [
            self.native_turn("", ("read_artifact", {"artifact_seq": artifact})),
            self.native_turn("The queued customer request has been handled."),
        ]
        self.run_team()
        run, = [row for row in self.rt.team.view(self.task)["runs"] if row["message_seq"] == source.seq]
        self.assertEqual(run["status"], "completed")
        self.assertEqual(self.finish()["status"], "ok")

    def test_peer_report_cleanup_read_blocks_finish_after_original_assignment_returned(self):
        manager = self.rt.team._role(self.manager)
        worker = self.rt.team._role(self.worker)
        parent = self.rt.team._send(self.task, manager, self.worker, "Implement and return", "assignment")
        published, returned = self.exchange(self.worker,
            ("publish_artifact", {"filename": "result.txt", "content": "Before cleanup\n",
                                  "summary": "Initial output", "format": "text"}),
            ("send_message", {"recipient": self.manager, "reply_to": parent.seq,
                              "text": "Initial implementation returned"}), source=parent)
        artifact = published["artifact_seq"]
        self.exchange(self.manager, ("read_artifact", {"artifact_seq": artifact}),
                      source=self._message(returned["message_seq"]))
        child = self.rt.team._send(self.task, worker, self.reviewer, "Inspect cleanup details", "assignment",
                                   parent_assignment_seq=parent.seq)
        feedback, = self.exchange(self.reviewer, ("send_message", {
            "recipient": self.worker, "reply_to": child.seq,
            "text": "Please clean the leftover item before final delivery",
        }), source=child)
        self.assertEqual(self.rt.team_reviews.view(self.task), [])
        self._assert_finish_defers_author_cleanup(self._message(feedback["message_seq"]), artifact)

    def test_old_review_report_is_retired_before_model_after_goal_change_and_pause_resume(self):
        request = self.request([self.publish("Old goal contents\n")])
        self.assertEqual(request["status"], "ok")
        rejected = self.record(request["review_seq"], "changes_requested",
                               source=self._message(request["message_seq"]))
        self.assertEqual(rejected["status"], "ok")
        report = self._message(rejected["message_seq"])
        adopted, = self.exchange(self.manager, ("set_objective", {
            "goal": "A separate discussion-only objective",
        }))
        self.assertEqual(adopted["status"], "ok")
        self.assertEqual(self.rt.team_reviews.view(self.task)[0]["status"], "stale")
        before = self._domain_effects()
        calls = len(self.driver.calls)
        self.rt.execution_control.change(self.task, "pause", request_id="boundary-stale-pause")
        self.rt.team.run(self.task)
        self.assertEqual(len(self.driver.calls), calls)
        self.assertFalse(any(row["message_seq"] == report.seq for row in self.rt.team.view(self.task)["runs"]))

        self.rt.execution_control.change(self.task, "resume", request_id="boundary-stale-resume")
        self.driver.steps[self.worker] = [
            self.native_turn("", ("publish_artifact", {
                "filename": "obsolete-fix.txt", "content": "Old correction must not execute",
                "summary": "Obsolete correction", "format": "text",
            })), self.native_turn("", ("wait_for_reply", {})),
        ]
        self.run_team()
        self.assertEqual(len(self.driver.calls), calls, "An obsolete report reached the fake model")
        self.assertEqual(self._domain_effects(), before)
        self.assertEqual(self.rt.team_reviews.view(self.task)[0]["status"], "stale")
        self.rt.team.run(self.task)
        self.assertEqual(len(self.driver.calls), calls)
        self.assertEqual(self._domain_effects(), before)

    def test_changes_requested_rejects_old_artifact_ids_without_domain_events(self):
        artifact = self.publish()
        request = self.request([artifact])
        self.assertEqual(self.record(request["review_seq"], "changes_requested")["status"], "ok")
        self._assert_resubmission_has_no_effect([artifact], request["review_seq"])
        self._assert_resubmission_has_no_effect([artifact], request["review_seq"])
        self.assertEqual(self.rt.team_reviews.view(self.task)[0]["status"], "changes_requested")

    def test_pending_resubmission_requires_publication_after_request(self):
        artifact = self.publish()
        request = self.request([artifact])
        self._assert_resubmission_has_no_effect([artifact], request["review_seq"])
        republished = self.publish()
        self.assertGreater(republished, request["review_seq"])
        replacement = self.request([republished], review_seq=request["review_seq"])
        self.assertEqual(replacement["status"], "ok")
        self.assertEqual(replacement["root_seq"], request["root_seq"])
        self.assertEqual(self.events("team.review_recorded"), [])

    def test_publication_before_rejection_does_not_satisfy_decision_boundary(self):
        artifact = self.publish()
        request = self.request([artifact])
        before_decision = self.publish()
        self.assertGreater(before_decision, request["review_seq"])
        self.assertEqual(self.record(request["review_seq"], "changes_requested")["status"], "ok")
        row, = self.rt.team_reviews.view(self.task)
        self.assertLess(before_decision, row["decision_seq"])
        self._assert_resubmission_has_no_effect([before_decision], request["review_seq"])

    def test_identical_bytes_republished_after_feedback_can_be_reconsidered(self):
        artifact = self.publish()
        request = self.request([artifact])
        self.assertEqual(self.record(request["review_seq"], "changes_requested")["status"], "ok")
        row, = self.rt.team_reviews.view(self.task)
        republished = self.publish()
        self.assertGreater(republished, row["decision_seq"])
        artifacts = {item["seq"]: item for item in self.rt.task(self.task).deliverables}
        self.assertEqual(artifacts[artifact]["content_sha256"], artifacts[republished]["content_sha256"])
        replacement = self.request([republished], review_seq=request["review_seq"])
        self.assertEqual(replacement["status"], "ok")
        self.assertEqual(replacement["root_seq"], request["root_seq"])
        row, = self.rt.team_reviews.view(self.task)
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["artifacts"][0]["seq"], republished)
        self.assertEqual(len(self.events("team.review_recorded")), 1)
        self.assertEqual(self.record(replacement["review_seq"])["status"], "ok")
        self.assertEqual(self.rt.team_reviews.view(self.task)[0]["status"], "approved")

    def test_multi_file_resubmission_accepts_only_one_republished_file(self):
        first = self.publish("First file\n", filename="first.txt")
        second = self.publish("Second file\n", filename="second.txt")
        request = self.request([first, second])
        self.assertEqual(self.record(request["review_seq"], "changes_requested")["status"], "ok")
        row, = self.rt.team_reviews.view(self.task)
        republished = self.publish("First file\n", filename="first.txt")
        self.assertGreater(republished, row["decision_seq"])
        self.assertLess(second, row["decision_seq"])
        replacement = self.request([republished, second], review_seq=request["review_seq"])
        self.assertEqual(replacement["status"], "ok")
        self.assertEqual(replacement["root_seq"], request["root_seq"])
        row, = self.rt.team_reviews.view(self.task)
        self.assertEqual({item["seq"] for item in row["artifacts"]}, {republished, second})
        self.assertEqual(self.record(replacement["review_seq"])["status"], "ok")

    def test_unrelated_new_publication_cannot_make_old_selected_ids_a_new_submission(self):
        artifact = self.publish()
        request = self.request([artifact])
        self.assertEqual(self.record(request["review_seq"], "changes_requested")["status"], "ok")
        self.publish("Unrelated publication\n", filename="unrelated.txt")
        self._assert_resubmission_has_no_effect([artifact], request["review_seq"])


def load_tests(loader, tests, pattern):
    # Import the fixture module without exposing its test class to discovery.
    return unittest.TestSuite(TeamReviewBoundaryTests(name)
        for name in TeamReviewBoundaryTests.__dict__ if name.startswith("test_"))


if __name__ == "__main__":
    unittest.main()
