"""Native readability uses trusted current delivery records, not legacy briefs."""

import unittest

from cater.events import Actor
from cater.runtime import Runtime


HOST = Actor.system("team")
WORKER = Actor.agent("dev", "developer")
DELIVERY = {"summary": "Note delivered", "verification": "Text inspected; execution not tested"}


class NativeReviewReadabilityTests(unittest.TestCase):
    def setUp(self):
        self.rt = Runtime(":memory:")
        self.addCleanup(self.rt.close)

    def emit(self, task, event_type, payload=None, actor=HOST):
        return self.rt.log.emit(type=event_type, task_id=task, actor=actor, payload=payload or {})

    def start(self):
        task = self.rt.create_task("Prepare a note")
        self.emit(task, "team.started", {"version": 1})
        return task

    def route(self, task):
        source = self.emit(task, "team.message_received",
                           {"recipient": "dev", "text": "Prepare the note", "kind": "message"}, Actor.human())
        self.emit(task, "msg.said", {"text": "Inspecting the note", "phase": "team", "reply_to": source.seq}, WORKER)
        return source

    def complete(self, task, payload=None, actor=HOST):
        return self.emit(task, "team.objective_completed", DELIVERY if payload is None else payload, actor)

    def design(self, task=None):
        review = self.rt.review(task)
        lens = next(row for row in review["lenses"] if row["id"] == "design")
        if lens["status"] != "ok" and self.rt.tasks():
            self.assertIn(lens["action"], review["next_actions"])
        return lens

    def test_native_delivery_without_brief_is_readable_and_review_is_read_only(self):
        task = self.start()
        self.route(task)
        self.complete(task, {**DELIVERY, "artifacts": [], "run_id": "manager-run"})
        before = self.rt.log.read()
        lens = self.design(task)
        self.assertEqual(lens["status"], "ok")
        self.assertIn("routed chat timelines", lens["finding"])
        self.assertIn("current-objective delivery summary and verification text", lens["finding"])
        self.assertNotIn("brief", lens["finding"])
        self.assertIn("does not prove model correctness or user understanding", lens["action"])
        self.assertIsNone(self.rt.task(task).brief)
        self.assertEqual(self.rt.log.read(), before)

    def test_minimal_historical_contract_needs_no_artifacts_run_id_or_r07_fields(self):
        task = self.start()
        self.route(task)
        self.complete(task)
        self.assertEqual(self.design(task)["status"], "ok")

    def test_member_routing_without_legacy_agent_output_is_still_actual_process_evidence(self):
        task = self.start()
        self.emit(task, "team.message_received", {"recipient": "test", "text": "Review the note"}, WORKER)
        self.complete(task)
        self.assertEqual(self.rt.task(task).messages, [])
        self.assertEqual(self.design(task)["status"], "ok")

    def test_late_run_retirement_keeps_delivery_and_history_readable(self):
        task = self.start()
        source = self.route(task)
        self.emit(task, "team.run_started", {"message_seq": source.seq, "role": "dev", "run_id": "old-run"})
        delivery = self.complete(task)
        self.emit(task, "team.run_finished", {"message_seq": source.seq, "role": "dev", "run_id": "old-run",
                                               "status": "interrupted", "reason": "Retired after delivery"})
        before = self.rt.log.read(task_id=task)
        self.assertEqual(self.design(task)["status"], "ok")
        self.assertIn(delivery, before)
        self.assertEqual(self.rt.log.read(task_id=task), before)

    def test_native_task_does_not_hide_missing_legacy_brief_in_workspace(self):
        task = self.start()
        self.route(task)
        self.complete(task)
        legacy = self.rt.create_task("Legacy execution")
        self.emit(legacy, "msg.said", {"text": "Working", "phase": "execution"}, WORKER)
        lens = self.design()
        self.assertEqual(lens["status"], "watch")
        self.assertIn("Some agent work lacks a generated brief.", lens["finding"])
        self.assertIn("1 native task(s)", lens["finding"])
        self.assertEqual(self.design(task)["status"], "ok")
        self.emit(legacy, "brief.generated", {"brief": {"summary": "Legacy summary"}})
        self.assertEqual(self.design()["status"], "ok")

    def test_legacy_execution_in_same_task_still_needs_its_brief(self):
        task = self.start()
        self.emit(task, "msg.said", {"text": "Legacy work", "phase": "execution"}, WORKER)
        self.route(task)
        self.complete(task)
        self.assertIn("lacks a generated brief", self.design(task)["finding"])

    def test_legacy_results_and_empty_workspace_keep_original_design_behavior(self):
        self.assertEqual(self.design()["status"], "needs_attention")
        task = self.rt.create_task("Legacy work")
        self.emit(task, "msg.said", {"text": "Working"}, WORKER)
        lens = self.design(task)
        self.assertEqual(lens["status"], "watch")
        self.assertEqual(lens["finding"], "Some agent work lacks a generated brief.")
        self.emit(task, "brief.generated", {"brief": {"summary": "Summary"}})
        lens = self.design(task)
        self.assertEqual(lens["status"], "ok")
        self.assertEqual(lens["finding"], "The process has timeline and brief data.")

    def test_new_objective_cannot_reuse_old_completion_even_with_same_goal(self):
        for goal in ("A different note", "Prepare a note"):
            with self.subTest(goal=goal):
                task = self.start()
                self.route(task)
                old = self.complete(task)
                self.emit(task, "team.objective_updated", {"goal": goal})
                before = self.rt.log.read(task_id=task)
                lens = self.design(task)
                self.assertEqual(lens["status"], "watch")
                self.assertIn("current native objective has no trusted completed outcome", lens["finding"])
                self.assertIn("1 earlier completion record(s) remain historical", lens["finding"])
                self.assertIn(old, before)
                self.assertEqual(self.rt.log.read(task_id=task), before)
                self.complete(task)
                lens = self.design(task)
                self.assertEqual(lens["status"], "ok")
                self.assertIn("1 earlier completion record(s) remain historical", lens["finding"])

    def test_in_progress_native_chat_is_visible_but_not_completed(self):
        task = self.start()
        self.route(task)
        lens = self.design(task)
        self.assertEqual(lens["status"], "watch")
        self.assertIn("no trusted completed outcome", lens["finding"])
        self.assertNotIn("brief", lens["finding"])

    def test_latest_malformed_completion_cannot_fall_back_to_old_good_summary_or_brief(self):
        task = self.start()
        self.route(task)
        self.complete(task)
        self.emit(task, "brief.generated", {"brief": {"summary": "Unrelated legacy summary"}})
        for field in ("summary", "verification"):
            for value in (None, "", " \n\t", 42, False, [], {"text": "not a string"}):
                with self.subTest(field=field, value=value):
                    self.complete(task, {**DELIVERY, field: value})
                    lens = self.design(task)
                    self.assertEqual(lens["status"], "watch")
                    self.assertIn("lacks non-empty text for " + field, lens["finding"])
            payload = dict(DELIVERY)
            payload.pop(field)
            self.complete(task, payload)
            self.assertIn("lacks non-empty text for " + field, self.design(task)["finding"])
        self.complete(task, {})
        self.assertIn("summary, verification", self.design(task)["finding"])
        self.complete(task)
        self.assertEqual(self.design(task)["status"], "ok")

    def test_untrusted_completion_cannot_complete_current_objective(self):
        for actor in (WORKER, Actor.agent("team", "team"), Actor.human(), Actor.system("other")):
            with self.subTest(actor=actor):
                task = self.start()
                self.route(task)
                self.complete(task)
                self.emit(task, "team.objective_updated", {"goal": "New objective"})
                self.complete(task, actor=actor)
                lens = self.design(task)
                self.assertEqual(lens["status"], "watch")
                self.assertIn("no trusted completed outcome", lens["finding"])
                self.assertIn("remain historical", lens["finding"])

    def test_untrusted_update_or_completion_cannot_erase_trusted_delivery(self):
        task = self.start()
        self.route(task)
        self.complete(task)
        self.emit(task, "team.objective_updated", {"goal": "Forged objective"}, WORKER)
        self.complete(task, {"summary": ""}, WORKER)
        self.assertEqual(self.design(task)["status"], "ok")

    def test_start_only_cannot_claim_readable_native_completion(self):
        task = self.start()
        self.assertEqual(self.design(task)["status"], "watch")
        self.complete(task)
        lens = self.design(task)
        self.assertEqual(lens["status"], "watch")
        self.assertIn("no valid routed native message", lens["finding"])
        self.assertNotIn("have routed chat timelines", lens["finding"])

    def test_routing_evidence_requires_trusted_start_sender_recipient_text_and_order(self):
        for case in ("no-start", "untrusted-start", "message-before-start", "message-after-delivery",
                     "other-task", "system-sender", "unknown-agent", "unknown-human",
                     "missing-recipient", "unknown-recipient", "invalid-recipient",
                     "missing-text", "empty-text", "invalid-text"):
            with self.subTest(case=case):
                task = self.rt.create_task("Check native routing")
                payload = {"recipient": "dev", "text": "Inspect the note", "kind": "message"}
                if case == "message-before-start":
                    self.emit(task, "team.message_received", payload, Actor.human())
                if case != "no-start":
                    self.emit(task, "team.started", {"version": 1}, WORKER if case == "untrusted-start" else HOST)
                if case == "message-after-delivery":
                    self.complete(task)
                if case == "missing-recipient":
                    payload.pop("recipient")
                elif case == "unknown-recipient":
                    payload["recipient"] = "outsider"
                elif case == "invalid-recipient":
                    payload["recipient"] = ["dev"]
                elif case == "missing-text":
                    payload.pop("text")
                elif case == "empty-text":
                    payload["text"] = " \n\t"
                elif case == "invalid-text":
                    payload["text"] = {"text": "not text"}
                actor = {"system-sender": HOST, "unknown-agent": Actor.agent("outsider", "developer"),
                         "unknown-human": Actor.human("outsider")}.get(case, Actor.human())
                target = self.rt.create_task("Another task") if case == "other-task" else task
                if case != "message-before-start":
                    self.emit(target, "team.message_received", payload, actor)
                self.emit(task, "msg.said", {"text": "An unbound response", "phase": "team"}, WORKER)
                if case != "message-after-delivery":
                    self.complete(task)
                lens = self.design(task)
                self.assertEqual(lens["status"], "watch")
                self.assertIn("no valid routed native message", lens["finding"])

    def test_other_task_completion_cannot_supply_missing_native_delivery(self):
        first, second = self.start(), self.start()
        self.route(first)
        self.route(second)
        self.complete(first)
        lens = self.design()
        self.assertEqual(lens["status"], "watch")
        self.assertIn("Task " + second + ":", lens["finding"])
        self.assertIn("1 native task(s)", lens["finding"])
        self.assertNotIn("lacks a generated brief", lens["finding"])


if __name__ == "__main__":
    unittest.main()
