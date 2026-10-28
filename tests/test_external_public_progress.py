"""Offline regressions for durable public chunks and their reply boundaries.

Scripts set only the adapter's accepted public ``state.text``. Filtering private
ACP thoughts is covered by test_acp and test_external_tool_observations; putting
thoughts into this already-public field would bypass that adapter contract.
"""

from contextlib import contextmanager
from dataclasses import replace
import json
import unittest
from unittest.mock import patch

from cater.acp import AcpTimeout
from cater.events import Actor, EventLog
from cater.external_scratch import LaunchDirectory
from cater.runtime import Runtime
from cater.team import HOST
import test_team_external as external


class ExternalPublicProgressTests(unittest.TestCase):
    def setUp(self):
        # Composition deliberately avoids discovering the fixture's tests again.
        self.f = external.TeamExternalTests()
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()
        self.rt, self.task = self.f.rt, self.f.task
        self.manager, self.worker = self.f.manager, self.f.worker
        self.assertIsNone(self.rt.team.external.progress)
        process = self.enterContext(patch("subprocess.Popen", side_effect=AssertionError(
            "Public-progress regressions must never launch an external process")))
        self.addCleanup(process.assert_not_called)

    @staticmethod
    def chunk(run, text):
        run.state = replace(run.state, text=run.state.text + text)

    def events(self, kind):
        return self.f.events(kind)

    def said(self, kind=None, *, role=None):
        return [e for e in self.events("msg.said")
                if (kind is None or e.payload.get("response_kind") == kind)
                and (role is None or e.actor.id == role)]

    def run_one(self, script, *, source=None, role=None):
        if source is None:
            source = self.f.receive("Inspect and explain the current facts.", role=role)
        self.f.driver.scripts[source.payload["recipient"]] = [script]
        claimed = self.rt.team._claim(self.task)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed[0], source)
        list(self.rt.team._run_message(self.task, *claimed))
        self.assertEqual(self.f.driver.errors, [])
        self.assertTrue(all(not scripts for scripts in self.f.driver.scripts.values()))
        self.assertTrue(self.rt.verify_chain().ok)
        self.f.assert_receipt_chain()
        return source

    def assert_progress(self, texts, *, role=None):
        rows = self.said("progress", role=role)
        self.assertEqual([e.payload["text"] for e in rows], texts)
        starts = {e.payload["run_id"]: e for e in self.events("team.run_started")}
        for event in rows:
            p = event.payload
            start = starts[p["run_id"]]
            self.assertEqual(event.actor, Actor.agent(start.payload["role"],
                                                     self.rt.team._role(start.payload["role"]).role))
            self.assertEqual(p["phase"], "team")
            self.assertEqual(p["audience"], "team")
            self.assertEqual(p["reply_to"], start.payload["message_seq"])
            revision = max((e.seq for e in self.rt.log.read(task_id=self.task)
                            if e.seq < event.seq and e.actor == HOST
                            and e.type in {"team.objective_updated", "team.objective_completed"}), default=0)
            self.assertEqual(p["revision"], revision)
            span = p["public_text_span"]
            attempt = next(e for e in self.events("team.external_run_started")
                           if e.seq == span["external_started_seq"])
            self.assertEqual(span["version"], 1)
            self.assertEqual(attempt.payload["run_id"], p["run_id"])
            self.assertGreaterEqual(span["start"], 0)
            self.assertEqual(span["end"] - span["start"], len(p["text"]))
            self.assertNotIn("delivery_seq", p)
            self.assertLess(start.seq, event.seq)
        return rows

    def assert_no_verdict(self):
        self.assertEqual(self.events("team.objective_completed"), [])
        self.assertEqual(self.events("team.review_recorded"), [])

    def test_incremental_chunks_commit_before_each_tool_without_observer(self):
        chunks = ["已定位入口。\n", "参数有两条来源。\n", "还需核对边界。\n"]
        other = EventLog(self.rt.log.path)
        self.addCleanup(other.close)
        dispatch = self.rt.team._dispatch_tool
        observed = []

        def inspect_committed(*args, **kwargs):
            # A separate connection proves the chunk is committed, not just
            # visible inside the execution connection's pending transaction.
            self.assertFalse(self.rt.log.db.in_transaction)
            rows = list(other.iter_events(task_id=self.task, event_types=("msg.said",)))
            observed.append([e.payload["text"] for e in rows])
            return dispatch(*args, **kwargs)

        def script(run):
            for text in chunks:
                self.chunk(run, text)
                self.assertEqual(run.call("read_messages", limit=1)["status"], "ok")
            return run.state.text  # ACP normal return contains all public chunks.

        with patch.object(self.rt.team, "_dispatch_tool", side_effect=inspect_committed):
            self.run_one(script)
        rows = self.assert_progress(chunks)
        self.assertEqual(observed, [chunks[:1], chunks[:2], chunks])
        for row, start in zip(rows, self.events("team.tool_started")):
            self.assertLess(row.seq, start.seq)
        self.assertEqual(self.said("reply"), [])
        self.assert_no_verdict()

    def test_repeated_and_empty_deltas_do_not_repeat_or_create_messages(self):
        def script(run):
            run.call("read_messages", limit=1)  # Empty cumulative text.
            self.chunk(run, "A useful public finding.")
            run.call("read_messages", limit=1)
            run.call("read_messages", limit=1)  # Identical cumulative text.
            self.chunk(run, " \n\t")
            run.call("read_messages", limit=1)  # Whitespace-only increment.
            self.chunk(run, "A second finding.")
            run.call("read_messages", limit=1)
            return run.state.text

        self.run_one(script)
        first, second = self.assert_progress(["A useful public finding.", "A second finding."])
        self.assertEqual(first.payload["public_text_span"]["start"], 0)
        self.assertEqual(second.payload["public_text_span"]["start"], len("A useful public finding. \n\t"))
        self.assertEqual(len(self.events("team.tool_finished")), 5)
        self.assertEqual(self.said("reply"), [])

    def test_failed_tool_keeps_progress_and_accepts_corrected_tool(self):
        def script(run):
            self.chunk(run, "Reading the requested receipt.\n")
            result = run.call("read_messages", limit=0)
            self.assertEqual(result["status"], "error")
            self.assertFalse(run.revoked)
            self.chunk(run, "Corrected the invalid page size.\n")
            self.assertEqual(run.call("read_messages", limit=1)["status"], "ok")
            return run.state.text

        self.run_one(script)
        rows = self.assert_progress(["Reading the requested receipt.\n",
                                     "Corrected the invalid page size.\n"])
        receipts = self.events("team.tool_finished")
        self.assertLess(rows[0].seq, receipts[0].seq)
        self.assertLess(receipts[0].seq, rows[1].seq)
        self.assertEqual(self.said("reply"), [])
        self.assertEqual(self.events("team.run_finished")[-1].payload["status"], "completed")

    def test_chunks_after_adopting_objective_use_the_new_revision(self):
        def script(run):
            self.chunk(run, "Clarified the requested goal.\n")
            self.assertEqual(run.call("set_objective", goal="Inspect the exact requested version.")["status"], "ok")
            self.chunk(run, "Inspecting within the adopted goal.\n")
            run.call("read_messages", limit=1)
            return run.state.text

        self.run_one(script)
        before, after = self.assert_progress(["Clarified the requested goal.\n",
                                              "Inspecting within the adopted goal.\n"])
        objective, = self.events("team.objective_updated")
        self.assertEqual(before.payload["revision"], 0)
        self.assertEqual(after.payload["revision"], objective.seq)
        self.assertEqual(before.payload["run_id"], after.payload["run_id"])
        self.assertEqual(before.payload["public_text_span"]["end"], after.payload["public_text_span"]["start"])
        self.assertEqual(self.said("reply"), [])

    def test_no_tool_answer_remains_one_customer_reply(self):
        def script(run):
            self.chunk(run, "The direct answer is 42.")
            return run.state.text

        source = self.run_one(script)
        self.assert_progress([])
        reply, = self.said()
        self.assertEqual(reply.payload["text"], "The direct answer is 42.")
        self.assertEqual((reply.payload["response_kind"], reply.payload["audience"],
                          reply.payload["recipient"], reply.payload["reply_to"]),
                         ("reply", "customer", "human", source.seq))
        self.assertEqual(self.events("team.tool_started"), [])
        self.assert_no_verdict()

    def test_normal_end_turn_replies_only_with_new_tail_after_tools(self):
        def script(run):
            self.chunk(run, "Checking the original input.\n")
            run.call("read_messages", limit=1)
            self.chunk(run, "The answer from that input is 42.")
            return run.state.text

        source = self.run_one(script)
        self.assert_progress(["Checking the original input.\n"])
        reply, = self.said("reply")
        self.assertEqual(reply.payload["text"], "The answer from that input is 42.")
        self.assertEqual(reply.payload["reply_to"], source.seq)
        self.assertEqual(reply.payload["audience"], "customer")
        self.assert_no_verdict()

    def test_wait_after_tools_does_not_republish_cumulative_text_as_reply(self):
        def script(run):
            self.chunk(run, "Inspected the inputs.\n")
            run.call("read_messages", limit=1)
            self.chunk(run, "Waiting for the missing evidence.")
            run.call("wait_for_reply")
            self.fail("Successful wait must yield")

        self.run_one(script)
        self.assert_progress(["Inspected the inputs.\n", "Waiting for the missing evidence."])
        self.assertEqual(self.said("reply"), [])
        self.assertEqual(self.events("team.run_finished")[-1].payload["status"], "waiting")
        self.assert_no_verdict()

    def test_first_wait_for_customer_without_assignment_keeps_legacy_plain_answer(self):
        def script(run):
            self.assertIsNone(run.context["active_assignment"])
            self.chunk(run, "Here is the requested explanation.")
            run.call("wait_for_reply")

        source = self.run_one(script)
        self.assert_progress([])
        reply, = self.said("reply")
        self.assertEqual(reply.payload["text"], "Here is the requested explanation.")
        self.assertEqual((reply.payload["audience"], reply.payload["reply_to"]),
                         ("customer", source.seq))

    def test_first_wait_on_assignment_is_progress_not_legacy_answer(self):
        source = self.rt.team._send(self.task, self.rt.team._role(self.manager), self.worker,
                                    "Inspect and return this assignment.", "assignment")

        def script(run):
            self.assertIsNotNone(run.context["active_assignment"])
            self.chunk(run, "Found an unverified edge case; waiting.")
            run.call("wait_for_reply")

        self.run_one(script, source=source)
        self.assert_progress(["Found an unverified edge case; waiting."], role=self.worker)
        self.assertEqual(self.said("reply"), [])
        self.assertFalse([e for e in self.events("team.message_received")
                          if e.payload["kind"] == "report"])
        assignment, = self.rt.team.recovery.assignments(self.task)
        self.assertEqual((assignment["seq"], assignment["status"]), (source.seq, "waiting"))
        self.assert_no_verdict()

    def test_first_wait_for_human_with_active_assignment_is_still_progress(self):
        assignment = self.rt.team._send(self.task, self.rt.team._role(self.manager), self.worker,
                                        "Inspect and formally return this assignment.", "assignment")

        def ask(run):
            run.call("request_questions", title="Clarify the delegated scope", questions=[{
                "id": "scope", "question": "Which scope?", "selection": "single",
                "options": [{"id": "original", "label": "Original scope"}]}])

        self.run_one(ask, source=assignment)
        source = self.f.receive("Use the original scope.", role=self.worker)

        def script(run):
            self.assertEqual(run.context["message"]["sender"], "human")
            self.assertEqual(run.context["active_assignment"]["seq"], assignment.seq)
            self.chunk(run, "The original assignment still needs verification.")
            run.call("wait_for_reply")

        self.run_one(script, source=source)
        self.assert_progress(["The original assignment still needs verification."], role=self.worker)
        self.assertEqual(self.said("reply"), [])
        self.assertFalse([e for e in self.events("team.message_received")
                          if e.payload["kind"] == "report"])
        self.assert_no_verdict()

    def test_first_wait_without_customer_source_is_progress(self):
        source = self.rt.team._send(self.task, self.rt.team._role(self.worker), self.manager,
                                    "An unsolicited progress note without a customer consultation.", "progress")

        def script(run):
            self.assertIsNone(run.context["active_assignment"])
            self.chunk(run, "Team-only observation before waiting.")
            run.call("wait_for_reply")

        self.run_one(script, source=source)
        self.assert_progress(["Team-only observation before waiting."])
        self.assertEqual(self.said("reply"), [])

    def test_formal_return_preserves_progress_and_only_one_assignment_report(self):
        def worker(run):
            self.chunk(run, "Located the requested artifact.\n")
            self.f.publish(run)
            self.chunk(run, "The saved bytes are ready for the owner.\n")
            self.f.return_assignment(run)

        def owner(run):
            self.assertEqual(run.context["message"]["kind"], "report")
            run.call("wait_for_reply")

        self.f.driver.scripts = {self.manager: [self.f.delegate_wait, owner], self.worker: [worker]}
        self.f.receive()
        self.f.drain()
        rows = self.assert_progress(["Located the requested artifact.\n",
                                     "The saved bytes are ready for the owner.\n"], role=self.worker)
        self.assertEqual(self.said("reply", role=self.worker), [])
        report, = [e for e in self.events("team.message_received")
                   if e.actor.id == self.worker and e.payload["kind"] == "report"]
        self.assertEqual(report.payload["text"], "The exact published version is ready.")
        self.assertEqual(report.payload["reply_to"], rows[0].payload["reply_to"])
        self.assertLess(rows[-1].seq, report.seq)
        self.assert_no_verdict()

    def test_explicit_customer_reply_is_not_duplicated_by_cumulative_progress(self):
        def script(run):
            self.chunk(run, "Checked the evidence; this is still progress.")
            run.call("reply_to_customer", text="Only this is the customer answer.")

        self.run_one(script)
        self.assert_progress(["Checked the evidence; this is still progress."])
        reply, = self.said("reply")
        self.assertEqual(reply.payload["text"], "Only this is the customer answer.")
        receipt, = self.f.results("reply_to_customer")
        self.assertEqual(receipt["message_seq"], reply.seq)
        self.assert_no_verdict()

    def test_finish_work_emits_only_its_summary_as_delivery_reply(self):
        def script(run):
            self.chunk(run, "Writing the requested file.\n")
            self.f.publish(run)
            self.chunk(run, "Checked the saved version.\n")
            self.f.deliver(run)

        self.run_one(script)
        self.assert_progress(["Writing the requested file.\n", "Checked the saved version.\n"])
        reply, = self.said("reply")
        completed, = self.events("team.objective_completed")
        self.assertEqual(reply.payload["text"], "Offline fixture delivered")
        self.assertEqual(reply.payload["delivery_seq"], completed.seq)
        self.assertEqual(reply.payload["audience"], "customer")
        self.assertEqual(self.events("team.review_recorded"), [])

    def assert_failure_visible_to_next_owner(self, error):
        before, tail = "Found a missing boundary check.\n", "The zero-length case is also affected."
        contexts = []
        model_input = self.rt.team._model_input

        def capture_input(*args, **kwargs):
            result = model_input(*args, **kwargs)
            contexts.append((args[2].id, result[0]))
            return result

        def worker(run):
            self.chunk(run, before)
            run.call("read_messages", limit=1)
            self.chunk(run, tail)
            raise error("PRIVATE_TRANSPORT_DETAIL")

        def owner(run):
            self.assertEqual(run.context["message"]["kind"], "progress")
            progress = self.assert_progress([before, tail], role=self.worker)
            rows = {row["seq"]: row for row in run.context["conversation"]}
            raw = {row["seq"]: row for row in contexts[-1][1]["conversation"]}
            self.assertEqual(contexts[-1][0], self.manager)
            for event in progress:
                for history in (rows, raw):
                    self.assertEqual(history[event.seq]["text"], event.payload["text"])
                    for key in ("response_kind", "audience", "revision", "run_id", "reply_to"):
                        self.assertEqual(history[event.seq][key], event.payload[key])
                    self.assertEqual(history[event.seq]["sender"], self.worker)
                    self.assertNotIn("public_text_span", history[event.seq])
            self.assertEqual(run.context["reviews"], [])
            run.call("wait_for_reply")

        self.f.driver.scripts = {self.manager: [self.f.delegate_wait, owner], self.worker: [worker]}
        self.f.receive()
        with patch.object(self.rt.team, "_model_input", side_effect=capture_input):
            self.f.drain()
        self.assertEqual(len(self.f.driver.runs), 3)
        self.assertEqual(self.said("reply", role=self.worker), [])
        self.assertFalse([e for e in self.events("team.message_received")
                          if e.actor.id == self.worker and e.payload["kind"] == "report"])
        failed, = [e for e in self.events("team.run_finished") if e.payload["role"] == self.worker]
        self.assertEqual(failed.payload["status"], "failed")
        self.assertEqual(failed.payload["error_type"], error.__name__)
        if error is AcpTimeout:
            self.assertEqual(failed.payload["external_failure_code"], "deadline_exceeded")
        self.assertNotIn("PRIVATE_TRANSPORT_DETAIL", json.dumps(self.rt.log.read(task_id=self.task), default=str))
        self.assertEqual(self.rt.team.external_runs.pending(self.task), [])
        self.assert_no_verdict()

    def test_timeout_tail_and_provenance_reach_next_actual_owner_model_input(self):
        self.assert_failure_visible_to_next_owner(AcpTimeout)

    def test_exception_tail_and_provenance_reach_next_actual_owner_model_input(self):
        self.assert_failure_visible_to_next_owner(RuntimeError)

    def test_failure_without_tools_keeps_public_tail_as_progress(self):
        def script(run):
            self.chunk(run, "Partial public discovery before the first tool.")
            raise AcpTimeout("PRIVATE_TIMEOUT")

        self.run_one(script)
        self.assert_progress(["Partial public discovery before the first tool."])
        self.assertEqual(self.events("team.tool_started"), [])
        self.assertEqual(self.said("reply"), [])
        self.assert_no_verdict()

    def test_unconfirmed_cleanup_preserves_public_tail_and_primary_timeout(self):
        def script(run):
            self.chunk(run, "A finding remains useful despite unknown cleanup.")
            run.cleanup_complete = False
            raise AcpTimeout("PRIVATE_PRIMARY_TIMEOUT")

        self.run_one(script)
        self.assert_progress(["A finding remains useful despite unknown cleanup."])
        finished, = self.events("team.run_finished")
        self.assertEqual(finished.payload["status"], "interrupted")
        self.assertEqual(finished.payload["error_type"], "AcpTimeout")
        self.assertEqual(finished.payload["external_failure_code"], "deadline_exceeded")
        pending, = self.rt.team.external_runs.pending(self.task)
        external_finish, = self.events("team.external_run_finished")
        self.assertFalse(external_finish.payload["cleanup_complete"])
        self.assertEqual(pending["started_seq"], external_finish.payload["started_seq"])
        self.assertEqual(pending["outcome"], "unknown")
        self.assertEqual(self.said("reply"), [])
        self.assert_no_verdict()

    def assert_scope_race(self, change, *, at_boundary=False, fail=False):
        config_path = None
        if change == "workspace_permissions":
            project = self.f.root / "project"
            project.mkdir()
            config_path = self.f.root / "workspaces.json"
            config = {"workspaces": [{"id": "project", "title": "Offline project",
                                       "root": str(project), "mode": "edit"}]}
            config_path.write_text(json.dumps(config), encoding="utf-8")
            self.rt.workspaces.config_path = config_path
            self.rt.workspaces.bind(self.task, "project", expected_revision=None)
            self.assertTrue(self.rt.workspaces.view(self.task)["available"])
        other = Runtime(self.rt.log.path, roles_path=self.f.roles,
                        drivers={self.f.driver.name: self.f.driver}, workspaces_path=config_path)
        self.addCleanup(other.close)
        armed, injected = False, []
        atomic, close = self.rt.log.atomic, LaunchDirectory.close

        def change_scope():
            self.assertFalse(self.rt.log.db.in_transaction)
            if change == "pause":
                other.execution_control.change(self.task, "pause", request_id="public-progress-pause")
            elif change == "message":
                other.team.receive(self.task, "New customer scope takes priority.")
            elif change == "objective":
                other.log.emit(type="team.objective_updated", actor=HOST, task_id=self.task,
                               payload={"goal": "A new independently adopted objective."})
            elif change == "workspace_permissions":
                config["workspaces"][0]["mode"] = "read_only"
                config_path.write_text(json.dumps(config), encoding="utf-8")
            else:
                self.fail(f"Unknown scope change: {change}")
            injected.append(change)

        @contextmanager
        def race_before_write():
            nonlocal armed
            if armed:
                armed = False
                change_scope()
            with atomic():
                yield

        def cleanup_then_arm(directory, **kwargs):
            nonlocal armed
            result = close(directory, **kwargs)
            if not at_boundary:
                # The normal-result check already passed. Inject only after
                # cleanup, just before the public tail's write transaction.
                armed = True
            return result

        def script(run):
            nonlocal armed
            self.chunk(run, "Previously committed discovery.\n")
            run.call("read_messages", limit=1)
            self.chunk(run, "LATE_STALE_TAIL_MUST_NOT_APPEAR")
            if at_boundary:
                armed = True
                run.call("read_messages", limit=1)
            elif fail:
                raise AcpTimeout("PRIVATE_OLD_SCOPE_TIMEOUT")
            return run.state.text

        with patch.object(self.rt.log, "atomic", side_effect=race_before_write), \
                patch.object(LaunchDirectory, "close", autospec=True, side_effect=cleanup_then_arm):
            self.run_one(script)
        self.assertEqual(injected, [change])
        self.assert_progress(["Previously committed discovery.\n"])
        self.assertEqual(self.said("reply"), [])
        self.assertEqual(len(self.events("team.tool_started")), 1)
        self.assertNotIn("LATE_STALE_TAIL_MUST_NOT_APPEAR",
                         json.dumps(self.rt.log.read(task_id=self.task), default=str))
        self.assertEqual(self.events("team.run_finished")[-1].payload["status"], "interrupted")
        self.assert_no_verdict()

    def test_pause_between_boundary_check_and_progress_transaction_rejects_late_chunk(self):
        self.assert_scope_race("pause", at_boundary=True)

    def test_new_customer_message_at_final_reply_transaction_rejects_late_tail(self):
        self.assert_scope_race("message")

    def test_new_objective_at_failure_tail_transaction_keeps_only_saved_progress(self):
        self.assert_scope_race("objective", fail=True)

    def test_workspace_permission_change_at_failure_tail_rejects_stale_progress(self):
        self.assert_scope_race("workspace_permissions", fail=True)
        self.assertFalse(self.rt.workspaces.view(self.task)["available"])


if __name__ == "__main__":
    unittest.main()
