"""Real HTTP/control/queue integration; deterministic external model fixture."""

from copy import deepcopy
from dataclasses import replace
import unittest
from unittest.mock import patch

from cater.events import Actor
from cater.team import HOST
from cater.web import ActivityRunner
import test_team_resume_admission


class ResumeSupervisionTests(unittest.TestCase):
    scenario = test_team_resume_admission.ResumeAdmissionTests.scenario

    def notices(self, scenario):
        return [event for event in scenario.events() if event.type == "team.message_received"
                and event.actor == HOST and event.payload.get("trigger") == "control_resume"]

    def settled(self, scenario):
        scenario.driver.scripts = {scenario.manager: [lambda run: "Existing unfinished work"]}
        scenario.send()
        scenario.drain()

    def test_http_resume_after_confirmed_write_uses_new_input_without_replaying_effect(self):
        with self.scenario(external=True) as s:
            published = []

            def interrupted(run):
                result = run.call("publish_artifact", filename="result.txt", content="Preserved bytes\n",
                                  summary="Existing result", format="text")
                self.assertEqual(result["status"], "ok", result)
                published.append(result["artifact_seq"])
                s.control("pause")
                run.call("read_artifact", artifact_seq=published[0])
                self.fail("Paused execution cannot continue")

            def resumed(run):
                self.assertEqual(run.context["message"]["kind"], "recovery")
                self.assertNotEqual(run.context["message"]["seq"], original.seq)
                content = run.call("read_artifact", artifact_seq=published[0])
                self.assertEqual(content["content"], "Preserved bytes\n")
                run.call("finish_work", summary="Inspected existing result", verification="Exact bytes read back")
                self.fail("Successful delivery must yield")

            s.driver.scripts = {s.manager: [interrupted, resumed]}
            original = s.send()
            s.drain()
            prefix = s.events()
            self.assertEqual(len(s.contexts()), 1)
            self.assertEqual(s.driver.errors, [])
            self.assertEqual([e.payload["status"] for e in prefix if e.type == "team.run_finished"], ["interrupted"],
                             [e.payload for e in prefix if e.type == "team.run_finished"])
            s.control("resume")
            s.drain()
            s.assert_preserved(prefix)
            self.assertEqual(s.driver.errors, [])
            self.assertEqual(len(s.contexts()), 2)
            notice, = self.notices(s)
            self.assertNotEqual(notice.actor, Actor.human())
            with s.factory() as rt:
                self.assertTrue(rt.team.recovery.is_message(notice))
                self.assertEqual(rt.team.view(s.task)["status"], "done")
                self.assertEqual(rt.team.view(s.task)["pending_count"], 0)
                self.assertEqual(rt.team.external_runs.pending(s.task), [])
                self.assertEqual(len(rt.task(s.task).deliverables), 1)
            self.assertEqual([e for e in s.events() if e.type == "team.message_received"
                              and e.actor == Actor.human()], [original])
            self.assertEqual(len([e for e in s.events() if e.type == "team.tool_started"
                                 and e.payload["name"] == "publish_artifact"]), 1)
            s.control("resume")  # Same request id: no second supervision or model call.
            s.drain()
            self.assertEqual(self.notices(s), [notice])
            self.assertEqual(len(s.contexts()), 2)

    def test_persisted_resume_notice_is_consumed_once_after_runner_restart(self):
        with self.scenario(external=True) as s:
            s.driver.scripts = {s.manager: [lambda run: "Existing work is not delivered"]}
            s.send()
            s.drain()
            s.control("pause")
            s.control("resume")
            queued = s.runner.status(s.task)
            with s.factory() as rt:
                notice = rt.team.recovery.resume(s.task, queued["resume_seq"])
                self.assertIsNotNone(notice)
                self.assertEqual(rt.team.recovery.resume(s.task, queued["resume_seq"]), notice)
                self.assertTrue(rt.team.recovery.current(s.task, notice))
            prefix = s.events()
            s.driver.scripts = {s.manager: [lambda run: run.call("wait_for_reply")]}
            replacement = ActivityRunner(s.factory)
            with patch.object(replacement, "_start"):
                replacement.recover_interrupted()
                s.drain(replacement)
                replacement.recover_interrupted()
                s.drain(replacement)
            s.assert_preserved(prefix)
            self.assertEqual(self.notices(s), [notice])
            self.assertEqual(len(s.contexts()), 2)
            self.assertEqual(s.contexts()[-1]["message"]["seq"], notice.seq)
            self.assertEqual(s.driver.errors, [])

    def test_normal_pending_input_is_not_replaced_by_resume_supervision(self):
        with self.scenario(external=True) as s:
            original = s.send()
            s.control("pause")
            s.control("resume")
            s.drain()
            s.assert_answered_once(original)
            self.assertEqual(self.notices(s), [])

    def test_empty_task_resume_does_not_invent_work(self):
        with self.scenario(external=True) as s:
            s.control("pause")
            s.control("resume")
            s.drain()
            self.assertEqual(self.notices(s), [])
            self.assertEqual(s.contexts(), [])

    def test_distinct_resumes_before_queue_runs_use_latest_control_once(self):
        with self.scenario(external=True) as s:
            self.settled(s)
            s.driver.scripts = {s.manager: [lambda run: run.call("wait_for_reply")]}
            prefix = s.events()
            s.control("pause")
            s.control("resume")
            s.control("resume", "test-second-resume-click")
            with s.factory() as rt:
                latest = rt.execution_control.view(s.task)["seq"]
            s.drain()
            s.assert_preserved(prefix)
            notice, = self.notices(s)
            self.assertEqual(notice.payload["resume_seq"], latest)
            self.assertEqual(len(s.contexts()), 2)
            self.assertEqual(s.contexts()[-1]["message"]["seq"], notice.seq)
            self.assertEqual(s.driver.errors, [])

    def test_new_customer_input_supersedes_unclaimed_resume_notice(self):
        with self.scenario(external=True) as s:
            self.settled(s)
            s.driver.scripts = {s.manager: [lambda run: run.call("reply_to_customer", text="New answer")]}
            s.control("pause")
            s.control("resume")
            with s.factory() as rt:
                notice = rt.team.recovery.resume(s.task, s.runner.status(s.task)["resume_seq"])
                self.assertTrue(rt.team.recovery.current(s.task, notice))
            original = s.send("Please answer this instead", "test-new-customer-input")
            with s.factory() as rt:
                self.assertFalse(rt.team.recovery.current(s.task, notice))
            s.drain()
            self.assertEqual([c["message"]["seq"] for c in s.contexts()[1:]], [original.seq])
            self.assertEqual(len([e for e in s.events() if e.type == "msg.said"
                and e.payload.get("reply_to") == original.seq and e.payload.get("audience") == "customer"]), 1)

    def test_no_fresh_input_for_stale_scope_done_questionnaire_or_unknown_operation(self):
        for change in ("scope", "done", "questionnaire", "operation"):
            with self.subTest(change=change), self.scenario(external=True) as s:
                self.settled(s)
                s.control("pause")
                s.control("resume")
                with s.factory() as rt:
                    seq = s.runner.status(s.task)["resume_seq"]
                    if change == "scope":
                        rt.command(s.task, "Different direction")
                    elif change == "done":
                        rt.log.emit(type="team.objective_completed", task_id=s.task, actor=HOST,
                                    payload={"summary": "Already delivered"})
                    target = (patch.object(rt.questionnaires, "view", return_value=[{"status": "pending"}])
                              if change == "questionnaire" else
                              patch.object(rt.team, "_pending_operations", return_value=[{"unknown": True}])
                              if change == "operation" else patch.object(rt.team, "enabled", return_value=True))
                    with target:
                        self.assertIsNone(rt.team.recovery.resume(s.task, seq))
                self.assertEqual(self.notices(s), [])
                self.assertEqual(len(s.contexts()), 1)

    def test_resume_provenance_rejects_malformed_and_duplicate_notifications(self):
        with self.scenario(external=True) as s:
            self.settled(s)
            s.control("pause")
            s.control("resume")
            with s.factory() as rt:
                notice = rt.team.recovery.resume(s.task, s.runner.status(s.task)["resume_seq"])
                events = rt.log.read(task_id=s.task)
                control = next(e for e in events if e.seq == notice.payload["resume_seq"])
                for key, value in (("resume_seq", True), ("resume_seq", []), ("resume_hash", "wrong"),
                                   ("revision", True), ("scope", None)):
                    payload = {**notice.payload, key: value}
                    with self.subTest(key=key, value=value):
                        self.assertFalse(rt.team.recovery.is_message(replace(notice, payload=payload), events))
                for previous in (None, True, [], {}):
                    forged = replace(control, payload={**control.payload, "previous_seq": previous})
                    altered = [forged if e.seq == control.seq else e for e in events]
                    self.assertFalse(rt.team.recovery.is_message(notice, altered))
                scope = deepcopy(notice.payload["scope"])
                scope["revision"] = True
                self.assertFalse(rt.team.recovery.is_message(replace(notice,
                    payload={**notice.payload, "revision": 1, "scope": scope}), events))
                duplicate = rt.log.emit(type=notice.type, task_id=s.task, actor=HOST, payload=notice.payload)
                self.assertFalse(rt.team.recovery.is_message(duplicate))


if __name__ == "__main__":
    unittest.main()
