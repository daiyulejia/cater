"""Old-goal review excerpts are reversible and never replace live requirements."""
import copy
import unittest

from cater.events import Actor
from cater.team_context import json_bytes, model_context
import test_team_review_cycle as reviews


class StaleReviewContextTests(unittest.TestCase):
    def setUp(self):
        self.f = reviews.TeamReviewCycleTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def review(self):
        f = self.f
        artifact = f.publish()
        requested = f.request([artifact])
        self.assertEqual(requested["status"], "ok")
        result = f.record(requested["review_seq"], feedback="\u539f\u6587" * 5000)
        self.assertEqual(result["status"], "ok")
        return requested["review_seq"]

    def new_goal(self):
        self.f.rt.log.emit(type="team.objective_updated", task_id=self.f.task,
                           actor=Actor.system("team"), payload={"goal": "Different objective"})

    def context(self):
        f = self.f
        message = f.rt.team.receive(f.task, "Inspect the historical review")
        context, _ = f.rt.team._context_snapshot(f.task, message, f.rt.organization.leader)
        return context

    def test_stale_excerpt_reads_back_exact_record_without_mutating_evidence(self):
        seq = self.review()
        self.new_goal()
        raw = self.context()
        original = copy.deepcopy(raw)
        projected = model_context(raw)
        old, = projected["reviews"]
        self.assertEqual(old["status"], "stale")
        self.assertTrue(old["feedback_truncated"])
        self.assertLessEqual(len(old["feedback"].encode()), 512)
        self.assertEqual(old["feedback_bytes"], 30000)
        self.assertLess(json_bytes(projected["reviews"]), json_bytes(raw["reviews"]) / 10)
        self.assertEqual(raw, original)
        reader = projected["context_window"]["review_reader"]
        f = self.f
        result, = f.exchange(f.manager, (reader["name"], {reader["argument"]: seq}))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["review"], original["reviews"][0])
        self.assertEqual(result["scope_reason"], "objective_changed")
        self.assertIn("不表示", result["scope_note"])
        self.assertEqual(f.rt.team_reviews.view(f.task), original["reviews"])

    def test_all_nonstale_states_preserve_full_requirements_and_feedback(self):
        self.review()
        raw = self.context()
        for status in ("pending", "approved", "changes_requested", "outdated", "unknown"):
            with self.subTest(status=status):
                raw["reviews"][0].update(status=status, instruction="\u8981\u6c42" * 5000)
                projected = model_context(raw)
                self.assertEqual(projected["reviews"], raw["reviews"])
                self.assertNotIn("feedback_truncated", projected["reviews"][0])

    def test_stale_short_records_stay_exact_and_unicode_previews_are_valid(self):
        self.review()
        self.new_goal()
        raw = self.context()
        raw["reviews"][0].update(feedback="Short", instruction="Short")
        self.assertEqual(model_context(raw)["reviews"], raw["reviews"])
        raw["reviews"][0].update(feedback="\U0001f408\u6708" * 500, instruction="\u8981\u6c42" * 500)
        row, = model_context(raw)["reviews"]
        for field in ("feedback", "instruction"):
            self.assertTrue(raw["reviews"][0][field].startswith(row[field]))
            self.assertLessEqual(len(row[field].encode()), 512)
            self.assertTrue(row[field + "_truncated"])

    def test_foreign_unknown_and_mixed_read_selectors_do_not_write_or_disclose(self):
        seq = self.review()
        f = self.f
        foreign = f.rt.create_task("Other group")
        before = f.rt.log.read()
        errors = []
        for task, arguments in ((foreign, {"review_seq": seq}), (f.task, {"review_seq": 999999})):
            with self.assertRaises(ValueError) as caught:
                f.rt.team.recovery.read(task, arguments)
            errors.append(str(caught.exception))
        self.assertEqual(errors[0], errors[1])
        for arguments in ({"review_seq": True}, {"review_seq": 0}, {"review_seq": "1"},
                          {"review_seq": seq, "limit": 1}, {"review_seq": seq, "after_seq": 0}):
            with self.assertRaises(ValueError):
                f.rt.team.recovery.read(f.task, arguments)
        self.assertEqual(f.rt.log.read(), before)

    def test_paused_group_can_read_stale_review_without_reviving_it(self):
        seq = self.review()
        self.new_goal()
        f = self.f
        f.rt.execution_control.change(f.task, "pause", request_id="pause-review-read")
        result, = f.exchange(f.manager, ("read_checkpoint", {"review_seq": seq}))
        self.assertEqual(result["review"]["status"], "stale")
        self.assertEqual(f.rt.execution_control.view(f.task)["state"], "paused")
        self.assertEqual(f.rt.team._pending_operations(f.task), [])
        self.assertTrue(f.rt.verify_chain().ok)

    def test_resubmitted_request_is_not_confused_with_an_old_goal_record(self):
        old = self.review()
        f = self.f
        artifact = f.publish("Revised source\n")
        requested = f.request([artifact], review_seq=old)
        self.assertEqual(requested["status"], "ok")
        with self.assertRaises(ValueError):
            f.rt.team.recovery.read(f.task, {"review_seq": old})
        current = f.rt.team.recovery.read(f.task, {"review_seq": requested["review_seq"]})
        self.assertEqual(current["review"]["status"], "pending")
        self.assertNotIn("scope_reason", current)
        self.assertNotIn("scope_note", current)
