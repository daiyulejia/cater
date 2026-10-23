"""Real HTTP/activity recovery with disposable ledgers and offline scripted drivers.

These deterministic scheduling checks are not real-model acceptance tests.
"""

from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from cater.activity_owner import activity_owner
from cater.events import Actor
from cater.runtime import Runtime
from cater.team import HOST
from cater.team_recovery import TeamRecovery
from cater.web import ActivityRunner, make_handler
from test_team_resume_admission import _Scenario


class WebResumeDrainTests(unittest.TestCase):
    @contextmanager
    def scenario(self):
        # The original scenario context writes under old QA; use an isolated root.
        with tempfile.TemporaryDirectory(prefix="resume-drain-") as directory:
            s = _Scenario(self, Path(directory), external=True)
            with patch("cater.web.ActivityRunner", return_value=s.runner):
                handler = make_handler(s.db, runtime_factory=s.factory)
            with patch.object(s.runner, "_start"), patch.object(handler, "log_message"):
                server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
                s.base = f"http://127.0.0.1:{server.server_port}"
                thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
                thread.start()
                try:
                    yield s
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(10)
                    self.assertFalse(thread.is_alive())

    def notices(self, s):
        return [e for e in s.events() if e.type == "team.message_received" and e.actor == HOST
                and e.payload.get("trigger") == "control_resume"]

    def prepare_review(self, s, *, pending_progress=True, questionnaire=False):
        with s.factory() as rt:
            worker, reviewer = [r.id for r in rt.worker_roles[:2]]
        state = {"worker": worker, "reviewer": reviewer}

        def manager(run):
            state["parent"] = run.call("delegate", recipient=worker, instruction="Prepare an independently reviewed result.")["message_seq"]
            run.call("wait_for_reply")

        def author(run):
            artifact = run.call("publish_artifact", filename="result.txt", content="Existing result bytes\n",
                                summary="Offline fixture", format="text")
            state["artifact"] = artifact["artifact_seq"]
            run.call("request_review", reviewer=reviewer, artifact_seqs=[state["artifact"]],
                     instruction="Check the current published artifact.")

        def review(run):
            state["review_assignment"] = run.context["active_assignment"]["seq"]
            if pending_progress:
                s.control("pause")
                run.call("read_artifact", artifact_seq=state["artifact"])
                self.fail("A paused review cannot dispatch another tool")
            run.call("send_message", recipient=worker, text="Review remains open.", notify_only=True)
            run.call("wait_for_reply")

        def author_progress(run):
            self.assertEqual(run.context["message"]["kind"], "progress")
            self.assertEqual(run.context["active_assignment"]["seq"], state["parent"])
            run.call("send_message", recipient=s.manager, text="Review still needs follow-up.", notify_only=True)
            if questionnaire:
                run.call("request_questions", title="Need customer input", questions=[{
                    "id": "detail", "question": "Which detail should be checked?", "selection": "single", "options": [],
                }])
            run.call("wait_for_reply")

        def supervise(run):
            self.assertEqual(run.context["message"].get("trigger"), "control_resume")
            self.assertEqual(run.context["reviews"][0]["status"], "pending")
            run.call("wait_for_reply")

        s.driver.scripts = {s.manager: [manager, supervise], worker: [author, author_progress], reviewer: [review]}
        state["human"] = s.send()
        s.drain()
        self.assertEqual(s.driver.errors, [])
        with s.factory() as rt:
            row, = rt.team_reviews.view(s.task)
            state["review_seq"] = row["request_seq"]
            self.assertEqual(row["status"], "pending")
            self.assertEqual(rt.team._pending_operations(s.task), [])
            self.assertEqual(rt.team.external_runs.pending(s.task), [])
            self.assertEqual(rt.team.view(s.task)["pending_count"], int(pending_progress))
            if pending_progress:
                progress, = [e for e in rt.team._messages(s.task) if e.payload["kind"] == "progress"]
                state["progress"] = progress
                self.assertEqual(progress.payload["reply_to"], state["review_assignment"])
        if not pending_progress:
            s.control("pause")
        return state

    @contextmanager
    def observe(self, s, *, after_first=None, contended_call=None):
        trace = {"resume": [], "drains": []}
        resume, collaborate = TeamRecovery.resume, Runtime.collaborate

        def checked_resume(recovery, task, seq):
            result = resume(recovery, task, seq)
            trace["resume"].append((seq, result))
            return result

        def checked_collaborate(rt, task, **kwargs):
            index = len(trace["drains"]) + 1
            if index == contended_call:
                with activity_owner(rt.log.path, task, "simple-team") as owned:
                    self.assertTrue(owned)
                    result = collaborate(rt, task, **kwargs)
            else:
                result = collaborate(rt, task, **kwargs)
            trace["drains"].append((kwargs, result))
            if index == 1 and after_first:
                after_first(rt)
            return result

        with patch.object(TeamRecovery, "resume", checked_resume), patch.object(Runtime, "collaborate", checked_collaborate):
            yield trace

    def assert_review_untouched(self, s, state, prefix):
        s.assert_preserved(prefix)
        self.assertEqual(s.driver.errors, [])
        with s.factory() as rt:
            review, = rt.team_reviews.view(s.task)
            self.assertEqual((review["request_seq"], review["status"]), (state["review_seq"], "pending"))
            self.assertEqual(rt.team.view(s.task)["status"], "running")
            artifact, = rt.task(s.task).deliverables
            self.assertEqual(artifact["seq"], state["artifact"])
            self.assertEqual(Path(artifact["local_path"]).read_text(encoding="utf-8"), "Existing result bytes\n")
        events = s.events()
        self.assertFalse([e for e in events if e.type in {"team.review_recorded", "team.objective_completed"}])
        self.assertFalse([e for e in events if e.type == "team.message_received" and e.payload["kind"] == "continuation"])
        self.assertEqual(len([e for e in events if e.type == "team.run_started"
                             and e.payload["message_seq"] == state["review_assignment"]]), 1)
        self.assertEqual(len([e for e in events if e.type == "team.tool_started"
                             and e.payload["name"] == "publish_artifact"]), 1)

    def test_http_resume_drains_progress_then_supervises_once_without_replaying_review(self):
        with self.scenario() as s:
            state = self.prepare_review(s)
            prefix = s.events()
            before = len(s.contexts())
            s.control("resume")
            activity = s.runner.status(s.task)
            with self.observe(s) as trace:
                s.drain()
            notices = self.notices(s)
            self.assertEqual(len(notices), 1, "Drained agent progress stranded the still-pending review without resume supervision")
            notice, = notices
            self.assertEqual(notice.payload["resume_seq"], activity["resume_seq"])
            self.assertEqual(notice.payload["recipient"], s.manager)
            self.assertEqual([seq for seq, _ in trace["resume"]], [activity["resume_seq"]] * 2)
            self.assertIsNone(trace["resume"][0][1])
            self.assertEqual(trace["resume"][1][1], notice)
            self.assertEqual(len(trace["drains"]), 2)
            self.assertTrue(all(options == {"expected_scope": activity["expected_scope"]}
                                for options, _ in trace["drains"]))
            self.assertEqual([c["message"]["seq"] for c in s.contexts()[before:]], [state["progress"].seq, notice.seq])
            self.assertEqual(s.runner.status(s.task)["state"], "completed")
            self.assert_review_untouched(s, state, prefix)
            self.assertEqual([e for e in s.events() if e.type == "team.message_received" and e.actor == Actor.human()], [state["human"]])
            s.control("resume")
            s.drain()
            self.assertEqual(self.notices(s), [notice])
            self.assertEqual(len(s.contexts()), before + 2)

    def test_supervision_available_at_entry_is_not_supplemented_or_replayed(self):
        for persisted in (False, True):
            with self.subTest(persisted=persisted), self.scenario() as s:
                state = self.prepare_review(s, pending_progress=False)
                prefix = s.events()
                before = len(s.contexts())
                s.control("resume")
                activity = s.runner.status(s.task)
                if persisted:
                    with s.factory() as rt:
                        self.assertIsNotNone(rt.team.recovery.resume(s.task, activity["resume_seq"]))
                with self.observe(s) as trace:
                    s.drain()
                notice, = self.notices(s)
                self.assertEqual(trace["resume"], [(activity["resume_seq"], notice)])
                self.assertEqual(len(trace["drains"]), 1)
                self.assertEqual([c["message"]["seq"] for c in s.contexts()[before:]], [notice.seq])
                self.assert_review_untouched(s, state, prefix)
                with self.observe(s) as replay:
                    s.runner._run(s.task, activity["id"], "run")
                    s.control("resume")
                    s.drain()
                self.assertEqual(replay, {"resume": [], "drains": []})
                self.assertEqual(self.notices(s), [notice])

    def test_ordinary_run_does_not_turn_drained_progress_into_resume_supervision(self):
        with self.scenario() as s:
            state = self.prepare_review(s)
            prefix = s.events()
            before = len(s.contexts())
            with s.factory() as rt:
                rt.execution_control.change(s.task, "resume", request_id="direct-control-without-web-resume-activity")
            activity = s.runner.submit(s.task, "run")
            self.assertNotIn("resume_seq", activity)
            with self.observe(s) as trace:
                s.drain()
            self.assertEqual(trace["resume"], [])
            self.assertEqual(len(trace["drains"]), 1)
            self.assertEqual(self.notices(s), [])
            self.assertEqual([c["message"]["seq"] for c in s.contexts()[before:]], [state["progress"].seq])
            self.assertEqual(s.runner.status(s.task)["state"], "completed")
            self.assert_review_untouched(s, state, prefix)

    def test_empty_or_completed_work_does_not_get_a_second_collaboration(self):
        for complete_in_drain in (False, True):
            with self.subTest(complete_in_drain=complete_in_drain), self.scenario() as s:
                if complete_in_drain:
                    with s.factory() as rt:
                        source = rt.team.receive(s.task, "Complete this bounded fixture", request_id="resume-drain-finish")
                    s.driver.scripts[s.manager] = [lambda run: run.call(
                        "finish_work", summary="Fixture complete", verification="Scripted fixture only")]
                s.control("pause")
                s.control("resume")
                activity = s.runner.status(s.task)
                with self.observe(s) as trace:
                    s.drain()
                self.assertEqual(trace["resume"], [(activity["resume_seq"], None)])
                self.assertEqual(len(trace["drains"]), 1)
                self.assertEqual(self.notices(s), [])
                self.assertEqual(s.driver.errors, [])
                self.assertEqual([c["message"]["seq"] for c in s.contexts()], [source.seq] if complete_in_drain else [])
                self.assertEqual(s.runner.status(s.task)["state"], "completed")
                with s.factory() as rt:
                    self.assertEqual(rt.team.view(s.task)["status"], "done" if complete_in_drain else "running")
                    self.assertEqual(rt.team.recovery.assignments(s.task), [])

    def add_unknown_effect(self, s, rt, state):
        # Reconstruct a separate crashed operation; never execute its command.
        source = rt.team._send(s.task, rt.organization.leader, state["reviewer"],
                               "Persisted operation evidence", "notification")
        binding = {"run_id": "resume-drain-unknown-effect", "role": state["reviewer"],
                   "message_seq": source.seq, "revision": rt.team.view(s.task)["revision"],
                   "control_seq": rt.execution_control.view(s.task)["seq"]}
        rt.log.emit(type="team.run_started", actor=HOST, task_id=s.task, payload=binding)
        start = rt.log.emit(type="team.tool_started", actor=HOST, task_id=s.task, payload={
            **binding, "name": "workspace_run", "call_id": "resume-drain-unknown-command",
            "arguments": {"command": "fixture-command-never-executed"},
        })
        rt.log.emit(type="team.run_finished", actor=HOST, task_id=s.task,
                    payload={**binding, "status": "interrupted", "reason": "Fixture effect requires reconciliation"})
        state["unknown"] = start.seq
        pending, = rt.team._pending_operations(s.task)
        self.assertEqual((pending["seq"], pending["outcome"]), (start.seq, "unknown"))

    def test_post_drain_resume_rechecks_scope_customer_pause_questions_and_unknown_effects(self):
        for guard in ("scope", "new_customer", "pause", "questionnaire", "unknown_effect"):
            with self.subTest(guard=guard), self.scenario() as s:
                state = self.prepare_review(s, questionnaire=guard == "questionnaire")
                prefix = s.events()
                before = len(s.contexts())
                s.control("resume")
                activity = s.runner.status(s.task)

                def change_after_drain(rt):
                    self.assertEqual(rt.team.view(s.task)["pending_count"], 0)
                    if guard == "scope":
                        rt.rulebook.record("Inspect only under this new task rule.", scope="task", task_id=s.task)
                    elif guard == "new_customer":
                        state["new_customer"] = rt.team.receive(s.task, "A newer customer direction",
                                                               request_id="resume-drain-new-customer")
                    elif guard == "pause":
                        rt.execution_control.change(s.task, "pause", request_id="resume-drain-paused-after-drain")
                    elif guard == "questionnaire":
                        self.assertEqual([q["status"] for q in rt.questionnaires.view(s.task)], ["pending"])
                    else:
                        self.add_unknown_effect(s, rt, state)

                with self.observe(s, after_first=change_after_drain) as trace:
                    s.drain()
                self.assertEqual(trace["resume"], [(activity["resume_seq"], None)] * 2)
                self.assertEqual(len(trace["drains"]), 1)
                self.assertEqual(self.notices(s), [])
                self.assertEqual([c["message"]["seq"] for c in s.contexts()[before:]], [state["progress"].seq])
                self.assert_review_untouched(s, state, prefix)
                with s.factory() as rt:
                    if guard == "unknown_effect":
                        pending, = rt.team._pending_operations(s.task)
                        self.assertEqual((pending["seq"], pending["outcome"]), (state["unknown"], "unknown"))
                        self.assertFalse([e for e in s.events() if e.type == "team.tool_finished"
                                          and e.payload["name"] == "workspace_run"])
                    elif guard == "new_customer":
                        self.assertEqual(rt.team.view(s.task)["pending_count"], 1)
                    elif guard == "pause":
                        self.assertEqual(rt.execution_control.view(s.task)["state"], "paused")
                        self.assertEqual(s.runner.status(s.task)["state"], "failed")

    def test_consumer_contention_preserves_activity_before_either_drain(self):
        for contended_call in (1, 2):
            with self.subTest(contended_call=contended_call), self.scenario() as s:
                state = self.prepare_review(s)
                prefix = s.events()
                before = len(s.contexts())
                s.control("resume")
                activity = s.runner.status(s.task)
                with self.observe(s, contended_call=contended_call) as trace:
                    s.drain()
                self.assertEqual(len(trace["resume"]), contended_call)
                self.assertEqual(len(trace["drains"]), contended_call)
                self.assertIs(trace["drains"][-1][1]["consumer_owned"], False)
                self.assertEqual(len(self.notices(s)), contended_call - 1)
                self.assertEqual([c["message"]["seq"] for c in s.contexts()[before:]],
                                 [state["progress"].seq] if contended_call == 2 else [])
                self.assertEqual(s.runner.status(s.task)["state"], "observing")
                with s.factory() as rt:
                    self.assertIsNone(s.runner._activity_terminal(rt, s.task, activity["id"]))
                    self.assertEqual(rt.team.view(s.task)["pending_count"], 1)
                self.assert_review_untouched(s, state, prefix)


if __name__ == "__main__":
    unittest.main()
