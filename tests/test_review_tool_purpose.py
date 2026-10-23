"""Tool affordances distinguish saving, consulting, reviewing and delivering.

Offline wiring tests, not evidence of real-model tool selection.
"""

import unittest

from cater.workspace_tools import WorkspaceExecutor
import test_simple_team as native
import test_team_review_cycle as reviews


class ReviewToolPurposeTests(unittest.TestCase):
    def fixture(self, cls):
        f = cls()
        f.setUp()
        self.addCleanup(f.doCleanups)
        return f

    def test_saving_each_candidate_is_not_review_or_delivery(self):
        f = self.fixture(reviews.TeamReviewCycleTests)
        f.bind_workspace()
        workspace = WorkspaceExecutor(f.rt.workspaces.config['supervision']['policy'])
        schemas = {tool["name"]: tool for tool in f.rt.team._schemas(
            f.rt.team._role(f.manager), workspace)}
        for name in ("publish_artifact", "publish_workspace_file", "publish_workspace_archive"):
            with self.subTest(name=name):
                description = schemas[name]["description"]
                for meaning in ("artifact_seq", "request_review", "finish_work", "最终交付"):
                    self.assertIn(meaning, description)
        review = schemas["request_review"]["description"]
        for meaning in ("作者", "店长本人是作者", "保存待复核版本", "不必再delegate", "record_review"):
            self.assertIn(meaning, review)
        self.assertNotIn("复核委托", schemas["delegate"]["description"])
        self.assertIn("不是版本批准", schemas["delegate"]["description"])

    def test_author_can_save_a_candidate_without_finishing_or_starting_a_review(self):
        f = self.fixture(reviews.TeamReviewCycleTests)
        seq = f.publish(author=f.manager)
        self.assertGreater(seq, 0)
        self.assertEqual(f.events("team.objective_completed"), [])
        self.assertEqual(f.events("team.review_requested"), [])
        self.assertEqual(f.events("team.review_recorded"), [])
        self.assertNotEqual(f.rt.team.view(f.task)["status"], "done")

    def test_manager_author_uses_existing_exact_version_review(self):
        f = self.fixture(reviews.TeamReviewCycleTests)
        seq = f.publish(author=f.manager)
        request = f.request([seq], requester=f.manager)
        self.assertEqual(request["status"], "ok", request)
        decision = f.record(request["review_seq"])
        self.assertEqual(decision["status"], "ok", decision)
        row, = f.rt.team_reviews.view(f.task)
        self.assertEqual(row["author"], f.manager)
        self.assertEqual(row["reviewer"], f.reviewer)
        self.assertEqual(row["artifacts"][0]["seq"], seq)
        self.assertEqual(row["status"], "approved")
        self.assertEqual(f.events("team.objective_completed"), [])
        self.assertTrue(f.rt.verify_chain().ok)

    def test_ordinary_consultation_is_not_automatically_rewritten_as_review(self):
        f = self.fixture(native.SimpleTeamTests)
        f.driver.steps = {
            f.manager: [native.turn("", ("delegate", {
                "recipient": f.worker, "instruction": "Discuss two alternatives, no file or acceptance needed."})),
                native.turn("", ("wait_for_reply", {})), native.turn("Thanks, that is enough for this discussion.")],
            f.worker: [native.turn("Both alternatives are feasible; choose based on scope.")],
        }
        f.rt.team.receive(f.task, "Discuss possible approaches, do not start work.")
        f.run_ok()
        self.assertEqual(f.events("deliverable.published"), [])
        self.assertEqual(f.events("team.review_requested"), [])
        self.assertEqual(f.events("team.review_recorded"), [])
        self.assertEqual(f.events("team.objective_completed"), [])
        self.assertEqual(f.rt.team.view(f.task)["pending_count"], 0)
        self.assertTrue(f.rt.verify_chain().ok)


if __name__ == "__main__":
    unittest.main()
