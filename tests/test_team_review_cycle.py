"""Exact-version review regressions with native fake turns and disposable data."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import unittest

from cater.events import Actor, EVENT_CATEGORIES
from cater.runtime import Runtime
from test_team_supervision import TeamSupervisionTests


HOST = Actor.system("team")


class TeamReviewCycleTests(TeamSupervisionTests):
    def setUp(self):
        super().setUp()
        others = [role.id for role in self.rt.worker_roles if role.id != self.worker]
        self.reviewer, self.observer = others[:2]
        self._exchange_number = 0

    def exchange(self, role_id, *calls, task_id=None, source=None,
                 expected_statuses=("waiting", "completed"), expected_result_count=None):
        """Settle a native turn without consuming unrelated queued assignments."""
        task_id = task_id or self.task
        role = self.rt.team._role(role_id)
        self._exchange_number += 1
        if source is None:
            source = self.rt.team.receive(
                task_id, f"@{role.name} fixture exchange {self._exchange_number}",
                request_id=f"review-exchange-{self._exchange_number}")
            self.assertEqual(source.payload["recipient"], role_id)
        started = self.rt.log.emit(type="team.run_started", actor=HOST, task_id=task_id, payload={
            "message_seq": source.seq, "role": role_id,
            "run_id": f"review-exchange-{self._exchange_number}",
            "revision": self.rt.team.view(task_id)["revision"],
            "control_seq": self.rt.execution_control.view(task_id)["seq"],
        })
        self.driver.steps[role_id] = [
            self.native_turn("", *calls), self.native_turn("", ("wait_for_reply", {})),
        ]
        list(self.rt.team._run_message(task_id, source, role, started))
        finished, = [event for event in self.rt.log.read(task_id=task_id)
                     if event.type == "team.run_finished"
                     and event.payload["run_id"] == started.payload["run_id"]]
        self.assertIn(finished.payload["status"], expected_statuses, finished.payload)
        self.assertEqual(self.protocol_errors, [])
        results = [event.payload["result"] for event in self.rt.log.read(task_id=task_id)
                   if event.type == "team.tool_finished"
                   and event.payload["run_id"] == started.payload["run_id"]
                   and event.payload["name"] != "wait_for_reply"]
        self.assertEqual(len(results), len(calls) if expected_result_count is None else expected_result_count)
        return results

    def publish(self, content="version one\n", *, author=None, filename="result.txt", task_id=None):
        result, = self.exchange(author or self.worker, ("publish_artifact", {
            "filename": filename, "content": content, "summary": "Fixture publication", "format": "text",
        }), task_id=task_id)
        self.assertEqual(result["status"], "ok")
        return result["artifact_seq"]

    def request(self, artifacts, *, requester=None, reviewer=None, review_seq=None, task_id=None):
        arguments = {"reviewer": reviewer or self.reviewer, "artifact_seqs": artifacts,
                     "instruction": "Read the actual file and check its behavior."}
        if review_seq is not None:
            arguments["review_seq"] = review_seq
        result, = self.exchange(requester or self.worker, ("request_review", arguments), task_id=task_id)
        return result

    def record(self, review_seq, verdict="approved", *, reviewer=None, feedback="Fixture body checked.",
               task_id=None, source=None):
        result, = self.exchange(reviewer or self.reviewer, ("record_review", {
            "review_seq": review_seq, "verdict": verdict, "feedback": feedback,
        }), task_id=task_id, source=source)
        return result

    def context(self, *, task_id=None, role_id=None):
        task_id = task_id or self.task
        role = self.rt.team._role(role_id or self.manager)
        source = next(event for event in reversed(self.rt.team._messages(task_id))
                      if event.payload["recipient"] == role.id)
        return self.rt.team._context(task_id, source, role)

    def finish(self):
        result, = self.exchange(self.manager, ("finish_work", {
            "summary": "Fixture delivered", "verification": "Fake native driver; no real model",
        }))
        return result

    def acknowledge_latest_review(self):
        row, = self.rt.team_reviews.view(self.task)
        report = next(event for event in reversed(self.rt.team._messages(self.task))
                      if event.payload.get('kind') == 'report' and event.payload.get('review_seq') == row['request_seq'])
        self.exchange(row['author'], source=report)

    def assert_blocked(self, status, *, code="review_pending"):
        revision = self.rt.team.view(self.task)["revision"]
        completed = self.events("team.objective_completed")
        result = self.finish()
        self.assertEqual((result["status"], result["code"]), ("error", code))
        if code == "review_pending":
            self.assertIn(status, [row["status"] for row in result["reviews"]])
        self.assertEqual(self.events("team.objective_completed"), completed)
        self.assertEqual(self.rt.team.view(self.task)["revision"], revision)
        self.assertEqual(self.rt.team.view(self.task)["status"], "running")

    def test_two_rejections_then_approval_resume_author_before_delivery(self):
        versions = ["BUG: negative input rejected\n", "PARTIAL: zero still rejected\n", "FIXED: both supported\n"]
        feedbacks = ["Support negative input.", "Also support zero.", "Both cases checked against version three."]
        original = []
        author_reports = []

        def last_tool(history):
            return next(message["tool_calls"][-1]["name"] for message in reversed(history)
                        if message.get("role") == "assistant" and message.get("tool_calls"))

        def manager(context, history, tools):
            if history:
                result = json.loads(history[-1]["content"])
                return (self.native_turn("Delivered with fixture verification.") if result["status"] == "ok"
                        else self.native_turn("", ("wait_for_reply", {})))
            message = context["message"]
            if message["sender"] == "human" and message["text"] == "Start exact-version review":
                return self.native_turn("", ("delegate", {
                    "recipient": self.worker, "instruction": "Implement, obtain review, and formally report back.",
                }), ("wait_for_reply", {}))
            if message["kind"] == "report" and message["sender"] == self.worker:
                self.assertEqual(message["reply_to"], original[0])
            return self.native_turn("", ("finish_work", {
                "summary": "Reviewed version delivered", "verification": "Fixture file inspection only",
            }))

        def author(context, history, tools):
            active = context["active_assignment"]
            self.assertIsNotNone(active)
            self.assertEqual(active["sender"], self.manager)
            if not original:
                original.append(active["seq"])
            self.assertEqual(active["seq"], original[0])
            rows = context["reviews"]
            if not history and context["message"]["kind"] == "report":
                row, = rows
                author_reports.append((row["request_seq"], row["status"], active["seq"]))
                self.assertIn(row["feedback"], context["message"]["text"])
                self.rt.team.receive(self.task, "Probe before author finishes",
                                     request_id=f"review-author-probe-{row['request_seq']}")
                return self.native_turn("Everything is fixed; approved; ready to finish.",
                    ("send_message", {"recipient": self.manager, "text": "All fixed and ready.", "notify_only": True}),
                    ("read_artifact", {"artifact_seq": row["artifacts"][0]["seq"]}))
            if history and last_tool(history) == "publish_artifact":
                artifact_seq = json.loads(history[-1]["content"])["artifact_seq"]
                arguments = {"reviewer": self.reviewer, "artifact_seqs": [artifact_seq],
                             "instruction": "Check negative and zero inputs in the submitted version."}
                if rows:
                    arguments["review_seq"] = rows[0]["request_seq"]
                return self.native_turn("", ("request_review", arguments))
            if rows and rows[0]["status"] == "approved":
                return self.native_turn("", ("send_message", {
                    "recipient": self.manager, "reply_to": original[0], "text": "Final reviewed version formally delivered.",
                }), ("wait_for_reply", {}))
            index = len(self.rt.task(self.task).deliverables)
            return self.native_turn("", ("publish_artifact", {
                "filename": "result.txt", "content": versions[index], "summary": f"Version {index + 1}", "format": "text",
            }))

        def reviewer(context, history, tools):
            row, = context["reviews"]
            assignment = context["active_assignment"]
            self.assertEqual(assignment["review_seq"], row["request_seq"])
            self.assertEqual(assignment["seq"], context["message"]["seq"])
            if not history:
                self.rt.team.receive(self.task, "Probe pending review",
                                     request_id=f"review-pending-probe-{row['request_seq']}")
                return self.native_turn("", ("read_artifact", {"artifact_seq": row["artifacts"][0]["seq"]}))
            artifact = json.loads(history[-1]["content"])
            index = versions.index(artifact["content"])
            self.assertEqual(artifact["content_sha256"], hashlib.sha256(versions[index].encode()).hexdigest())
            return self.native_turn("", ("record_review", {
                "review_seq": row["request_seq"], "verdict": "approved" if index == 2 else "changes_requested",
                "feedback": feedbacks[index],
            }))

        self.driver.steps = {self.manager: [manager] * 24, self.worker: [author] * 24,
                             self.reviewer: [reviewer] * 12}
        self.rt.team.receive(self.task, "Start exact-version review", request_id="review-full-cycle")
        self.run_team()
        requests = self.events("team.review_requested")
        decisions = self.events("team.review_recorded")
        self.assertEqual(len(requests), 3)
        self.assertEqual([event.payload["verdict"] for event in decisions],
                         ["changes_requested", "changes_requested", "approved"])
        self.assertEqual([status for _, status, _ in author_reports],
                         ["changes_requested", "changes_requested", "approved"])
        self.assertEqual([seq for seq, _, _ in author_reports], [event.seq for event in requests])
        request_results = self.results("request_review")
        self.assertEqual([result["review_seq"] for result in request_results], [event.seq for event in requests])
        self.assertTrue(all(result["status"] == "ok" and result["root_seq"] == requests[0].seq
                            for result in request_results))
        messages = self.events("team.message_received")
        for request, result, decision in zip(requests, request_results, decisions):
            self.assertEqual(request.actor, HOST)
            self.assertEqual(decision.actor, HOST)
            assignment, = [event for event in messages if event.seq == result["message_seq"]]
            self.assertEqual(assignment.payload["kind"], "assignment")
            self.assertEqual(assignment.payload["review_seq"], request.seq)
            self.assertEqual(assignment.payload["parent_assignment_seq"], original[0])
            reports = [event for event in messages if event.actor.id == self.reviewer
                       and event.payload["kind"] == "report" and event.payload["reply_to"] == assignment.seq]
            self.assertEqual(len(reports), 1, "record_review must not trigger a second automatic report")
            self.assertEqual(reports[0].payload["recipient"], self.worker)
            self.assertIn(decision.payload["feedback"], reports[0].payload["text"])
        record_results = self.results("record_review")
        self.assertEqual([(result["status"], result["review_seq"], result["verdict"]) for result in record_results],
                         [("ok", request.seq, decision.payload["verdict"])
                          for request, decision in zip(requests, decisions)])
        finish_results = self.results("finish_work")
        self.assertEqual([result.get("code") for result in finish_results],
                         ["review_pending", "review_pending", "review_pending", "review_pending",
                          "review_pending", "author_pending", None])
        self.assertEqual([result["reviews"][0]["status"] for result in finish_results if "reviews" in result],
                         ["pending", "changes_requested", "pending", "changes_requested", "pending"])
        parent_reports = [event for event in messages if event.actor.id == self.worker
                          and event.payload["kind"] == "report" and event.payload["reply_to"] == original[0]]
        self.assertEqual(len(parent_reports), 1)
        completed, = self.events("team.objective_completed")
        self.assertLess(decisions[-1].seq, parent_reports[0].seq)
        self.assertLess(parent_reports[0].seq, completed.seq)
        runs = self.rt.team.view(self.task)["runs"]
        worker_runs = [row for row in runs if row["role"] == self.worker]
        self.assertTrue(all(row["status"] == "waiting" for row in worker_runs[:-1]))
        self.assertEqual(worker_runs[-1]["status"], "completed")
        self.assertTrue(all(row["status"] == "waiting" for row in runs if row["role"] == self.reviewer))
        self.assertEqual(self.rt.team.view(self.task)["status"], "done")
        self.assertEqual(self.rt.team.view(self.task)["pending_count"], 0)
        self.assert_one_run_per_role()

    def test_pending_approval_rejects_new_hash_without_recording_decision(self):
        old = self.publish()
        request = self.request([old])
        self.assertEqual(request["status"], "ok")
        self.assert_blocked("pending")
        new = self.publish("version two\n")
        result = self.record(request["review_seq"])
        self.assertEqual(result["status"], "error")
        self.assertEqual(self.events("team.review_recorded"), [])
        self.assert_blocked("outdated")
        replacement = self.request([new], review_seq=request["review_seq"])
        self.assertEqual(replacement["status"], "ok")
        self.assertNotEqual(replacement["review_seq"], request["review_seq"])
        self.assertEqual(replacement["root_seq"], request["root_seq"])
        self.assertEqual(self.record(replacement["review_seq"])["status"], "ok")

    def test_approved_hash_does_not_cover_later_publication(self):
        artifact = self.publish()
        request = self.request([artifact])
        self.assertEqual(self.record(request["review_seq"])["status"], "ok")
        same = self.publish()
        self.assertNotEqual(same, artifact)
        self.assertEqual(self.context(role_id=self.worker)["reviews"][0]["status"], "approved")
        new = self.publish("new unreviewed bytes\n")
        self.assertEqual(self.context(role_id=self.worker)["reviews"][0]["status"], "outdated")
        self.assert_blocked("outdated")
        self.assertEqual(self.request([artifact], review_seq=request["review_seq"])["status"], "error")
        self.assertEqual(self.request([new])["status"], "error", "A fresh root must not abandon the open correction")
        replacement = self.request([new], review_seq=request["review_seq"])
        self.assertEqual(replacement["status"], "ok")
        self.assertEqual(self.record(replacement["review_seq"])["status"], "ok")
        self.acknowledge_latest_review()
        self.assertEqual(self.finish()["status"], "ok")

    def test_only_author_requests_and_only_named_reviewer_records(self):
        artifact = self.publish()
        for requester, reviewer in [(self.observer, self.reviewer), (self.manager, self.reviewer),
                                    (self.worker, self.worker)]:
            with self.subTest(requester=requester, reviewer=reviewer):
                self.assertEqual(self.request([artifact], requester=requester, reviewer=reviewer)["status"], "error")
        self.assertEqual(self.events("team.review_requested"), [])
        request = self.request([artifact])
        self.assertEqual(request["status"], "ok")
        for role_id in (self.worker, self.manager, self.observer):
            with self.subTest(reviewer=role_id):
                self.assertEqual(self.record(request["review_seq"], reviewer=role_id)["status"], "error")
        self.assertEqual(self.events("team.review_recorded"), [])
        self.assertEqual(self.record(request["review_seq"])["status"], "ok")
        manager_artifact = self.publish(author=self.manager, filename="manager.txt")
        manager_request = self.request([manager_artifact], requester=self.manager)
        self.assertEqual(manager_request["status"], "ok", "The manager may review-submit their own publication")

    def test_resubmission_rejects_old_sequence_changed_author_or_changed_reviewer(self):
        artifact = self.publish()
        first = self.request([artifact])
        self.assertEqual(self.record(first["review_seq"], "changes_requested")["status"], "ok")
        revised = self.publish("revision two\n")
        second = self.request([revised], review_seq=first["review_seq"])
        self.assertEqual(second["status"], "ok")
        self.assertEqual(self.record(first["review_seq"])["status"], "error")
        self.assertEqual(self.request([revised], review_seq=first["review_seq"])["status"], "error")
        self.assertEqual(self.request([revised], reviewer=self.observer, review_seq=second["review_seq"])["status"], "error")
        self.assertEqual(self.request([revised], requester=self.manager, review_seq=second["review_seq"])["status"], "error")
        self.assertEqual(self.request([revised], requester=self.observer, review_seq=second["review_seq"])["status"], "error")
        other = self.publish("another author's revision\n", author=self.observer)
        self.assertEqual(self.request([other], requester=self.observer, review_seq=second["review_seq"])["status"], "error")
        self.assertEqual(len(self.events("team.review_requested")), 2)
        self.assertEqual(len(self.events("team.review_recorded")), 1)
        current = self.request([self.publish("author's own revision three\n")], review_seq=second["review_seq"])
        self.assertEqual(current["status"], "ok")
        self.assertEqual(current["root_seq"], first["root_seq"])
        self.assertEqual(self.record(current["review_seq"])["status"], "ok")
        self.assertEqual(self.record(current["review_seq"])["status"], "error", "Decisions must not be recorded twice")

    def test_cross_group_artifacts_and_review_sequences_do_not_disclose_private_content(self):
        other = self.rt.create_task("PRIVATE_REVIEW_GOAL_SENTINEL")
        self.rt.team.enable(other)
        private = self.publish("PRIVATE_ARTIFACT_BODY_SENTINEL", filename="private-sentinel.txt", task_id=other)
        foreign = self.request([private], task_id=other)
        self.assertEqual(foreign["status"], "ok")
        self.assertEqual(self.record(foreign["review_seq"], "changes_requested", task_id=other,
                                     feedback="PRIVATE_REVIEW_FEEDBACK_SENTINEL")["status"], "ok")
        local = self.publish()
        before_requests = self.events("team.review_requested")
        before_decisions = self.events("team.review_recorded")
        call_offset = len(self.driver.calls)
        results = [self.request([private]), self.request([local, private]),
                   self.request([local], review_seq=foreign["review_seq"]),
                   self.record(foreign["review_seq"])]
        self.assertTrue(all(result["status"] == "error" for result in results))
        self.assertEqual(self.events("team.review_requested"), before_requests)
        self.assertEqual(self.events("team.review_recorded"), before_decisions)
        visible = json.dumps({"results": results, "calls": self.driver.calls[call_offset:]})
        for sentinel in ("PRIVATE_REVIEW_GOAL_SENTINEL", "PRIVATE_ARTIFACT_BODY_SENTINEL",
                         "private-sentinel.txt", "PRIVATE_REVIEW_FEEDBACK_SENTINEL"):
            self.assertNotIn(sentinel, visible)
        self.assertEqual(self.context(role_id=self.reviewer)["reviews"], [])

    def test_artifact_contract_rejects_invalid_refs_and_mixed_authors(self):
        artifact = self.publish()
        other = self.publish(author=self.observer, filename="other.txt")
        old = self.publish("old body", filename="changed.txt")
        latest = self.publish("latest body", filename="changed.txt")
        for refs in ([], [artifact] * 21, [artifact, artifact], [True], [str(artifact)], [0], [-1],
                     [99999999], "not-an-array", [artifact, other], [old], [old, latest]):
            with self.subTest(refs=refs):
                self.assertEqual(self.request(refs)["status"], "error")
        self.assertEqual(self.events("team.review_requested"), [])
        self.assertEqual(self.request([artifact], reviewer="missing-role")["status"], "error")
        result, = self.exchange(self.worker, ("request_review", {
            "reviewer": self.reviewer, "artifact_seqs": [artifact], "instruction": "   ",
        }))
        self.assertEqual(result["status"], "error")

    def test_twenty_real_artifacts_are_hash_bound_in_context_and_persist_after_reopen(self):
        refs = []
        for index in range(20):
            content = f"actual file {index}\n"
            seq = self.publish(content, filename=f"file-{index}.txt")
            refs.append({"seq": seq, "filename": f"file-{index}.txt",
                         "content_sha256": hashlib.sha256(content.encode()).hexdigest()})
        extra = self.publish("twenty-first file\n", filename="extra.txt")
        self.assertEqual(self.request([ref["seq"] for ref in refs] + [extra])["status"], "error")
        self.assertEqual(self.events("team.review_requested"), [])
        request = self.request([ref["seq"] for ref in refs])
        self.assertEqual(request["status"], "ok")
        self.assertEqual(self.record(request["review_seq"], "changes_requested", feedback="Fix the boundary case.")["status"], "ok")
        context = self.context(role_id=self.reviewer)
        row, = context["reviews"]
        self.assertEqual({key: row[key] for key in ("root_seq", "request_seq", "author", "reviewer", "artifacts", "status", "feedback", "revision")}, {
            "root_seq": request["root_seq"], "request_seq": request["review_seq"], "author": self.worker,
            "reviewer": self.reviewer, "artifacts": refs, "status": "changes_requested",
            "feedback": "Fix the boundary case.", "revision": self.rt.team.view(self.task)["revision"],
        })
        for artifact in self.rt.task(self.task).deliverables:
            self.assertEqual(hashlib.sha256(Path(artifact["local_path"]).read_bytes()).hexdigest(), artifact["content_sha256"])
        with Runtime(self.root / "test.db", roles_path=self.roles, drivers={"team_test": self.driver}) as reopened:
            source = next(event for event in reversed(reopened.team._messages(self.task))
                          if event.payload["recipient"] == self.reviewer)
            self.assertEqual(reopened.team._context(self.task, source, reopened.team._role(self.reviewer))["reviews"], context["reviews"])
            self.assertTrue(reopened.verify_chain().ok)
        for kind in ("team.review_requested", "team.review_recorded"):
            event, = self.events(kind)
            self.assertEqual(event.actor, HOST)
            self.assertEqual(EVENT_CATEGORIES[kind], "review")

    def test_native_tools_expose_the_versioned_review_contract(self):
        self.publish()
        tools = {tool["name"]: tool["input_schema"] for tool in self.driver.calls[-1]["tools"]}
        request = tools["request_review"]
        self.assertEqual(set(request["required"]), {"reviewer", "instruction"})
        self.assertIn("artifact_seqs", request["properties"])
        self.assertIn("workspace_file", request["properties"])
        self.assertEqual(set(request["properties"]["workspace_file"]["required"]), {"path", "format"})
        self.assertEqual(set(request["properties"]), {"reviewer", "artifact_seqs", "workspace_file", "instruction", "review_seq"})
        self.assertFalse(request["additionalProperties"])
        props = request["properties"]
        self.assertEqual(props["reviewer"]["type"], "string")
        self.assertEqual(props["instruction"]["type"], "string")
        self.assertEqual(props["review_seq"]["type"], "integer")
        self.assertEqual((props["artifact_seqs"]["type"], props["artifact_seqs"]["minItems"],
                          props["artifact_seqs"]["maxItems"]), ("array", 1, 20))
        self.assertEqual(props["artifact_seqs"]["items"]["type"], "integer")
        record = tools["record_review"]
        self.assertEqual(set(record["required"]), {"review_seq", "verdict", "feedback"})
        self.assertEqual(set(record["properties"]), {"review_seq", "verdict", "feedback"})
        self.assertFalse(record["additionalProperties"])
        self.assertEqual(record["properties"]["review_seq"]["type"], "integer")
        self.assertEqual(record["properties"]["feedback"]["type"], "string")
        self.assertEqual(set(record["properties"]["verdict"]["enum"]), {"changes_requested", "approved"})

    def test_new_objective_marks_old_reviews_stale_and_does_not_block_delivery(self):
        request = self.request([self.publish()])
        self.assertEqual(request["status"], "ok")
        result, = self.exchange(self.manager, ("set_objective", {"goal": "A separate text-only objective"}))
        self.assertEqual(result["status"], "ok")
        row, = self.context(role_id=self.manager)["reviews"]
        self.assertEqual(row["status"], "stale")
        self.assertEqual(self.record(request["review_seq"])["status"], "error")
        self.assertEqual(self.request([self.rt.task(self.task).deliverables[0]["seq"]], review_seq=request["review_seq"])["status"], "error")
        self.assertEqual(self.finish()["status"], "ok")

    def test_notification_does_not_block_but_busy_non_author_must_return(self):
        request = self.request([self.publish()])
        self.assertEqual(self.record(request["review_seq"])["status"], "ok")
        self.acknowledge_latest_review()
        notice, = self.exchange(self.observer, ("send_message", {
            "recipient": self.manager, "text": "FYI only: another unrelated activity.", "notify_only": True,
        }))
        self.assertEqual(notice["status"], "ok")
        source = self.rt.team._send(self.task, self.rt.organization.leader, self.observer,
                                    "Unrelated non-author task", "assignment")
        run = self.rt.log.emit(type="team.run_started", actor=HOST, task_id=self.task, payload={
            "message_seq": source.seq, "role": self.observer, "run_id": "unrelated-busy-role",
            "revision": self.rt.team.view(self.task)["revision"],
            "control_seq": self.rt.execution_control.view(self.task)["seq"],
        })
        self.assertEqual(self.finish()["code"], "work_pending")
        self.assertEqual(self.rt.team.view(self.task)["status"], "running")
        self.rt.log.emit(type="team.run_finished", actor=HOST, task_id=self.task,
                         payload={**run.payload, "status": "completed"})
        self.rt.team._send(self.task, self.rt.team._role(self.observer), self.manager,
                           "Analysis returned; no additional files.", "report", reply_to=source.seq)
        self.assertEqual(self.finish()["status"], "ok")
        self.assertEqual(self.rt.team.view(self.task)["status"], "done")

    def test_simple_text_task_with_review_words_needs_no_explicit_review(self):
        self.driver.steps = {self.manager: [
            self.native_turn("", ("delegate", {"recipient": self.worker, "instruction": "Explain changes_requested and approved in plain text."}),
                             ("wait_for_reply", {})),
            self.native_turn("", ("finish_work", {"summary": "Explanation delivered", "verification": "Text only; no files requested"})),
            self.native_turn("The explanation is complete."),
        ], self.worker: [self.native_turn("Review, fix, changes_requested, approved: these are just words in this answer.")]}
        self.rt.team.receive(self.task, "Explain review vocabulary without creating files", request_id="review-simple-answer")
        self.run_team()
        self.assertEqual(self.events("team.review_requested"), [])
        self.assertEqual(self.events("team.review_recorded"), [])
        self.assertEqual(self.rt.task(self.task).deliverables, [])
        self.assertEqual(self.rt.team.view(self.task)["status"], "done")
        self.assertEqual(self.results("finish_work")[0]["status"], "ok")


def load_tests(loader, tests, pattern):
    # Reuse supervision helpers without collecting the inherited regression suite.
    return unittest.TestSuite(TeamReviewCycleTests(name)
        for name in TeamReviewCycleTests.__dict__ if name.startswith("test_"))


if __name__ == "__main__":
    unittest.main()
