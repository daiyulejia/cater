"""Invalid review identities are actionable without authorizing a verdict."""
import unittest

from cater.work import WorkConflict
import test_team_review_cycle as reviews


class ReviewRequestLookupTests(unittest.TestCase):
    def setUp(self):
        self.f = reviews.TeamReviewCycleTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def test_plain_message_is_not_a_review_and_correct_request_can_follow(self):
        f = self.f
        message = f.rt.team.receive(f.task, "Please review the existing document")
        result = f.record(message.seq)
        self.assertEqual(result["status"], "error")
        self.assertIn("本群没有对应的正式复核请求", result["message"])
        self.assertIn("request_review", result["message"])
        self.assertEqual(f.rt.team_reviews.view(f.task), [])
        self.assertFalse(any(e.type == "team.review_recorded" for e in f.rt.log.read(task_id=f.task)))
        artifact = f.publish()
        request = f.request([artifact])
        self.assertEqual(f.record(request["review_seq"])["status"], "ok")

    def test_foreign_review_and_unknown_identity_have_same_non_disclosing_error(self):
        f = self.f
        artifact = f.publish()
        request = f.request([artifact])
        other = f.rt.create_task("Other group")
        messages = []
        for seq in (request["review_seq"], 999999):
            with self.assertRaises(WorkConflict) as caught:
                f.rt.team_reviews._find(other, seq)
            messages.append(str(caught.exception))
        self.assertEqual(messages[0], messages[1])
        self.assertNotIn("result.txt", messages[0])

    def test_superseded_request_is_distinct_from_nonexistent_request(self):
        f = self.f
        request = f.request([f.publish()])
        f.record(request["review_seq"], "changes_requested")
        newer = f.request([f.publish("fixed version")], review_seq=request["review_seq"])
        self.assertEqual(newer["status"], "ok")
        with self.assertRaisesRegex(WorkConflict, "已有后续提交"):
            f.rt.team_reviews._find(f.task, request["review_seq"])
        self.assertEqual(f.rt.team_reviews._find(f.task, newer["review_seq"])["status"], "pending")

    def test_goal_change_preserves_stale_diagnostic(self):
        f = self.f
        request = f.request([f.publish()])
        result, = f.exchange(f.manager, ("set_objective", {"goal": "A new goal"}))
        self.assertEqual(result["status"], "ok")
        with self.assertRaisesRegex(WorkConflict, "目标替代"):
            f.rt.team_reviews._find(f.task, request["review_seq"])

    def test_stale_chain_guides_current_goal_submission_without_changing_old_approval(self):
        f = self.f
        request = f.request([f.publish()])
        decision = f.record(request["review_seq"])
        self.assertEqual(decision["status"], "ok")
        old_decisions = f.events("team.review_recorded")
        f.exchange(f.manager, ("set_objective", {"goal": "Correct the document for a new goal"}))
        revised = f.publish("Actual revised version for the current goal")
        requests = f.events("team.review_requested")
        rejected = f.request([revised], review_seq=request["review_seq"])
        self.assertEqual(rejected["status"], "error")
        self.assertIn("旧目标记录仍可读取", rejected["message"])
        self.assertIn("省略旧review_seq", rejected["message"])
        self.assertIn("由成果作者", rejected["message"])
        self.assertEqual(f.events("team.review_requested"), requests)
        self.assertEqual(f.events("team.review_recorded"), old_decisions)
        current = f.request([revised])
        self.assertEqual(current["status"], "ok")
        self.assertNotEqual(current["root_seq"], request["root_seq"])
        self.assertEqual(f.record(current["review_seq"])["status"], "ok")
        historical = f.rt.team_reviews.read(f.task, request["review_seq"])
        self.assertEqual(historical["status"], "stale")
        self.assertEqual(historical["decision_seq"], decision["decision_seq"])
        self.assertEqual(f.events("team.review_recorded")[0], old_decisions[0])
        self.assertTrue(f.rt.verify_chain().ok)

    def test_native_schemas_explain_author_and_request_identity(self):
        f = self.f
        f.publish()
        tools = {tool["name"]: tool for tool in f.driver.calls[-1]["tools"]}
        self.assertIn("成果发布作者本人", tools["request_review"]["description"])
        self.assertIn("普通delegate", tools["request_review"]["description"])
        request = tools["request_review"]["input_schema"]["properties"]["review_seq"]
        record = tools["record_review"]["input_schema"]["properties"]["review_seq"]
        self.assertIn("首次提交省略", request["description"])
        self.assertIn("同一目标内", request["description"])
        self.assertIn("stale", request["description"])
        self.assertIn("reviews[].request_seq", record["description"])
        self.assertIn("不是委托message_seq", record["description"])


if __name__ == "__main__":
    unittest.main()
