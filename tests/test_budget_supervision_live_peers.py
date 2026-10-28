"""Deterministic live-peer supervision regressions; no model or subprocess."""
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from cater.events import Actor
from cater.external_agent import QoderAcpDriver
from cater.team import HOST
from cater.tool_protocol import ToolCall
from cater.work import WorkConflict
import test_external_budget_checkpoint as budget_fixture
from test_team_cooperative import CooperativeDriver


class BudgetSupervisionLivePeerTests(unittest.TestCase):
    def setUp(self):
        self.fixture = budget_fixture.BudgetCheckpointTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.f = self.fixture.f
        self.rt, self.task = self.f.rt, self.f.task
        self.process = self.enterContext(patch(
            "subprocess.Popen", side_effect=AssertionError("No processes allowed")))
        self.addCleanup(self.process.assert_not_called)
        _, self.notice = self.fixture.stage()
        self.supervisor = self.rt.team._claim(self.task)
        self.assertEqual(self.supervisor[0], self.notice)
        self.assignment = self.rt.team._send(
            self.task, self.rt.organization.leader, self.f.worker, "Existing child work.", "assignment")
        self.f.driver.scripts[self.f.worker] = [lambda run: run.call("wait_for_reply")]
        initial_child = self.rt.team._claim(self.task)
        self.assertEqual(initial_child[0], self.assignment)
        list(self.rt.team._run_message(self.task, *initial_child))
        self.coop = CooperativeDriver()
        self.enterContext(patch.object(self.fixture.qoder, "agent_run", side_effect=self.coop.agent_run))
        self.iterators = []
        self.observed = {}
        self.rt.team.external.begin_scheduler(self.task)
        self.addCleanup(self.close_runs)

    def close_runs(self):
        try:
            for iterator in reversed(self.iterators):
                iterator.close()
        finally:
            self.rt.team.external.end_scheduler()
        self.assertEqual(self.rt.team.external._live, {})
        if hasattr(self.rt.team.external, "_live_scope_checks"):
            self.assertEqual(self.rt.team.external._live_scope_checks, {})

    def start_pair(self, *, message=False, child_write=False, separate_driver=False):
        def supervise(run):
            self.observed["continue"] = run.call(
                "continue_work", assignment_seq=self.assignment.seq, instruction="Continue the original work.")
            self.assertEqual(self.observed["continue"]["status"], "ok")
            yield None
            run.state = replace(run.state, text="Accepted supervisor progress while child is live.")
            if message:
                self.observed["message"] = run.call(
                    "send_message", recipient=self.f.reviewer, text="Check the later evidence.")
            run.call("wait_for_reply")
            self.fail("Wait must yield")

        def child(run):
            self.assertEqual(run.context["message"]["kind"], "continuation")
            yield None
            if child_write:
                self.observed["child_write"] = run.call(
                    "publish_artifact", filename="child.txt", content="Saved once.",
                    summary="Offline child evidence", format="text")
            run.call("wait_for_reply")

        self.coop.scripts = {self.f.manager: [supervise], self.f.worker: [child]}
        self.parent_it = self.rt.team._run_message(self.task, *self.supervisor)
        self.iterators.append(self.parent_it)
        next(self.parent_it)
        self.continued = self.rt.team._claim(self.task)
        self.assertEqual(self.continued[0].seq, self.observed["continue"]["message_seq"])
        self.child_it = self.rt.team._run_message(self.task, *self.continued)
        self.iterators.append(self.child_it)
        if separate_driver:
            self.peer_driver = QoderAcpDriver(self.f.worker, "offline-never-launch", "offline", timeout=2)
            with patch.object(self.peer_driver, "agent_run", side_effect=self.coop.agent_run), \
                    patch.dict(self.rt.drivers, {self.continued[1].driver: self.peer_driver}):
                next(self.child_it)
        else:
            next(self.child_it)
        self.parent_run, self.child_run = self.coop.runs
        rows = self.rt.team._pending_operations(self.task)
        self.child_row, = [row for row in rows if row["role"] == self.f.worker]
        self.parent_row, = [row for row in rows if row["role"] == self.f.manager]
        self.assertEqual(self.child_row["outcome"], "unsettled")
        self.assertFalse(self.child_row["run_ended"])
        self.assertTrue(self.rt.team.external.owns(self.task, self.child_row))

    def parent_finish(self):
        return next(event for event in self.f.events("team.run_finished")
                    if event.payload["run_id"] == self.supervisor[2].payload["run_id"])

    def parent_tools(self):
        return [event.payload["name"] for event in self.f.events("team.tool_started")
                if event.payload["run_id"] == self.supervisor[2].payload["run_id"]]

    def assert_blocked(self):
        list(self.parent_it)
        finished = self.parent_finish()
        self.assertEqual(finished.payload["status"], "interrupted", finished.payload)
        self.assertNotIn("wait_for_reply", self.parent_tools())
        self.assertNotIn("message", self.observed)
        self.assertFalse([event for event in self.f.events("msg.said")
                          if event.payload.get("run_id") == self.supervisor[2].payload["run_id"]])
        self.assertEqual(self.f.events("team.objective_completed"), [])
        self.assertTrue(self.rt.verify_chain().ok)
        return finished

    def test_live_child_allows_supervisor_wait_and_preserves_public_tail(self):
        # Reusable reproduction: fails on the frozen baseline, passes on candidate.
        self.start_pair()
        self.assertTrue(self.rt.team.external.owns(self.task, self.parent_row))
        list(self.parent_it)
        self.assertEqual(self.parent_finish().payload["status"], "waiting")
        self.assertEqual(self.parent_tools(), ["continue_work", "wait_for_reply"])
        public, = [e for e in self.f.events("msg.said")
                   if e.payload.get("run_id") == self.supervisor[2].payload["run_id"]]
        self.assertEqual(public.payload["text"], "Accepted supervisor progress while child is live.")
        self.assertEqual(public.payload["response_kind"], "progress")
        self.assertEqual(public.payload["audience"], "team")
        self.assertTrue(self.rt.team.external.owns(self.task, self.child_row))
        list(self.child_it)
        self.assertEqual(self.rt.team._pending_operations(self.task), [])

    def test_live_child_allows_supervisor_message_then_wait(self):
        self.start_pair(message=True)
        list(self.parent_it)
        self.assertEqual(self.parent_finish().payload["status"], "waiting")
        self.assertEqual(self.observed["message"]["status"], "ok")
        self.assertEqual(self.parent_tools(), ["continue_work", "send_message", "wait_for_reply"])

    def test_child_write_is_not_blocked_by_live_supervisor(self):
        self.start_pair(child_write=True)
        list(self.child_it)
        self.assertIn("child_write", self.observed, "Live supervisor blocked the child before dispatch")
        self.assertEqual(self.observed["child_write"]["status"], "ok")
        self.assertEqual(len(self.f.results("publish_artifact")), 1)
        list(self.parent_it)
        self.assertEqual(self.parent_finish().payload["status"], "waiting")

    def test_new_unknown_workspace_effect_still_blocks(self):
        self.start_pair(message=True)
        unknown = self.fixture.unknown(self.supervisor[2])
        self.assertTrue(any(row["started_seq"] == unknown.seq
                            for row in self.rt.team._pending_operations(self.task)))
        self.assert_blocked()
        self.assertTrue(any(row["started_seq"] == unknown.seq
                            for row in self.rt.team._pending_operations(self.task)))

    def test_display_flags_and_observe_only_cannot_release_unknown_effect(self):
        self.start_pair(message=True)
        self.fixture.unknown(self.supervisor[2])
        rows = self.rt.team._pending_operations(self.task)
        decorated = [{**row, "live": True, "requires_review": False, "pending_live": True}
                     if row["name"] == "workspace_run" else row for row in rows]
        self.assertFalse(self.rt.team.external.owns(self.task, self.parent_row, observe_only=True))
        with patch.object(self.rt.team, "_pending_operations", return_value=decorated):
            self.assert_blocked()

    def test_peer_cleanup_failure_is_not_treated_as_live(self):
        self.start_pair()
        self.child_run.cleanup_complete = False
        list(self.child_it)
        pending, = [row for row in self.rt.team.external_runs.pending(self.task)
                    if row["role"] == self.f.worker]
        self.assertEqual(pending["outcome"], "unknown")
        self.assertTrue(pending["run_ended"])
        self.assert_blocked()
        self.assertTrue(any(row["started_seq"] == pending["started_seq"]
                            for row in self.rt.team.external_runs.pending(self.task)))

    def test_live_phase_with_recorded_cleanup_error_is_not_allowed(self):
        self.start_pair()
        self.child_run.state = replace(self.child_run.state, cleanup_failures=(OSError("cleanup unknown"),))
        self.assert_blocked()

    def test_terminal_peer_before_adapter_cleanup_is_not_allowed(self):
        self.start_pair()
        result = self.child_run.execute(
            ToolCall("terminal", "wait_for_reply", {}))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(self.child_run.state.phase, "prompting")
        self.assert_blocked()

    def test_finished_receipt_cannot_be_disguised_by_stale_unsettled_row(self):
        self.start_pair()
        attempt = self.rt.team.external._live[self.child_row["started_seq"]][0]
        self.rt.team.external_runs.finish(attempt, phase="failed", prompt_attempted=True,
                                          cleanup_complete=False, result="failed")
        stale = [self.parent_row, self.child_row]
        with patch.object(self.rt.team, "_pending_operations", return_value=stale):
            self.assert_blocked()

    def test_forged_same_id_wrong_identity_does_not_borrow_live_handle(self):
        self.start_pair()
        forged = {**self.child_row, "run_id": "different-run"}
        with patch.object(self.rt.team, "_pending_operations", return_value=[self.parent_row, forged]):
            self.assert_blocked()

    def test_numeric_equal_but_wrong_type_identity_is_not_allowed(self):
        self.start_pair()
        forged = {**self.child_row, "message_seq": float(self.child_row["message_seq"])}
        with patch.object(self.rt.team, "_pending_operations", return_value=[self.parent_row, forged]):
            self.assert_blocked()

    def test_same_id_wrong_pending_name_does_not_borrow_live_handle(self):
        self.start_pair()
        forged = {**self.child_row, "name": "workspace_run"}
        with patch.object(self.rt.team, "_pending_operations", return_value=[self.parent_row, forged]):
            self.assert_blocked()

    def test_same_identity_new_start_without_live_handle_is_not_allowed(self):
        self.start_pair()
        attempt = self.rt.team.external._live[self.child_row["started_seq"]][0]
        self.rt.log.emit(type="team.external_run_started", actor=HOST, task_id=self.task,
                         payload=dict(attempt.payload))
        self.assert_blocked()

    def test_forged_live_entry_receipt_hash_is_not_allowed(self):
        self.start_pair()
        seq = self.child_row["started_seq"]
        entry = self.rt.team.external._live[seq]
        self.rt.team.external._live[seq] = (replace(entry[0], hash="forged"), *entry[1:])
        try:
            self.assert_blocked()
        finally:
            self.rt.team.external._live[seq] = entry

    def test_stale_peer_driver_configuration_still_blocks(self):
        self.start_pair(separate_driver=True)
        self.peer_driver.model = "changed-model"
        self.assert_blocked()

    def test_closing_peer_driver_still_blocks(self):
        self.start_pair(separate_driver=True)
        self.peer_driver.close()
        self.assert_blocked()

    def test_pause_still_blocks(self):
        self.start_pair()
        self.rt.execution_control.change(self.task, "pause", request_id="candidate-pause")
        self.assert_blocked()

    def test_replaced_objective_still_blocks(self):
        self.start_pair()
        self.rt.log.emit(type="team.objective_updated", actor=HOST, task_id=self.task,
                         payload={"goal": "Changed outside the running scope"})
        self.assert_blocked()

    def test_new_customer_input_still_blocks(self):
        self.start_pair()
        self.f.receive("Replace the supervisor's input.")
        self.assert_blocked()

    def test_missing_scheduler_owner_still_blocks_peer_exemption(self):
        self.start_pair()
        self.rt.team.external.end_scheduler()
        self.assert_blocked()

    def test_missing_scope_registration_cannot_use_display_owns(self):
        self.start_pair()
        if not hasattr(self.rt.team.external, "_live_scope_checks"):
            self.skipTest("Candidate-private registration guard")
        self.rt.team.external._live_scope_checks.pop(self.child_row["started_seq"])
        with patch.object(self.rt.team.external, "owns", return_value=True):
            self.assert_blocked()

    def test_replaced_handle_cannot_reuse_scope_registration(self):
        self.start_pair()
        if not hasattr(self.rt.team.external, "_live_scope_checks"):
            self.skipTest("Candidate-private registration guard")
        seq = self.child_row["started_seq"]
        entry = self.rt.team.external._live[seq]
        self.rt.team.external._live[seq] = (entry[0], SimpleNamespace(state=entry[1].state), entry[2])
        try:
            self.assert_blocked()
        finally:
            self.rt.team.external._live[seq] = entry

    def test_scope_validation_does_not_call_owns_recursively(self):
        self.start_pair()
        with patch.object(self.rt.team.external, "owns", side_effect=AssertionError("recursive owns")):
            self.parent_run.check_current(force=True)
        list(self.parent_it)
        self.assertEqual(self.parent_finish().payload["status"], "waiting")


class ConcurrentBudgetSupervisorTests(unittest.TestCase):
    def arrange_supervisors(self):
        fixture = budget_fixture.BudgetCheckpointTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def exercise_supervisors(self, *, direct_callback_stress=False):
        fixture = self.arrange_supervisors()
        f, rt = fixture.f, fixture.f.rt
        with patch("subprocess.Popen", side_effect=AssertionError("No processes allowed")):
            f.receive("Root manager work.")
            f.receive("Root worker work.", role=f.worker)
            first = rt.team._claim(f.task)
            second = rt.team._claim(f.task)
            self.assertNotEqual(first[1].id, second[1].id)

            def budget(run):
                run.call("read_checkpoint")
                fixture.budget(run)

            f.driver.scripts = {f.manager: [budget], f.worker: [budget]}
            list(rt.team._run_message(f.task, *first))
            list(rt.team._run_message(f.task, *second))
            notices = fixture.notices()
            self.assertEqual(len(notices), 2)
            claimed = rt.team._claim(f.task)
            other = next(n for n in notices if n.seq != claimed[0].seq)
            started = rt.log.emit(type="team.run_started", actor=HOST, task_id=f.task, payload={
                **claimed[2].payload, "message_seq": other.seq, "run_id": "overlap-supervisor"})
            coop = CooperativeDriver()

            def supervise(run):
                yield None
                run.call("wait_for_reply")

            coop.scripts = {f.manager: [supervise, supervise]}
            iterators = []
            rt.team.external.begin_scheduler(f.task)
            try:
                with patch.object(fixture.qoder, "agent_run", side_effect=coop.agent_run):
                    first_it = rt.team._run_message(f.task, *claimed)
                    iterators.append(first_it)
                    next(first_it)
                    if not direct_callback_stress:
                        # Actual admission remains fail-closed: a second
                        # supervisor is not a new request while the first lives.
                        list(rt.team._run_message(f.task, other, claimed[1], started))
                        self.assertEqual(len(coop.runs), 1)
                        finished = next(e for e in f.events("team.run_finished")
                                        if e.payload["run_id"] == "overlap-supervisor")
                        self.assertEqual(finished.payload["status"], "interrupted")
                        self.assertTrue(finished.payload["admission_not_started"])
                        list(first_it)
                        self.assertEqual(rt.team.external_runs.pending(f.task), [])
                        return
                    # Direct internal lifecycle calls deliberately bypass the
                    # admission above solely to exercise two mutual callbacks.
                    # This is not evidence that the scheduler admits both.
                    second_it = rt.team.external.iter_message(
                        f.task, other, claimed[1], started, started.payload["revision"],
                        None, fixture.qoder, "Offline callback-cycle stress.\n")
                    iterators.append(second_it)
                    next(second_it)
                    rows = rt.team.external_runs.pending(f.task)
                    self.assertEqual(len(rows), 2)
                    with patch.object(rt.team, "_pending_operations",
                                      wraps=rt.team._pending_operations) as reads:
                        self.assertTrue(all(rt.team.external.owns(f.task, row) for row in rows))
                        self.assertLessEqual(reads.call_count, 2, "Peer validation recursively traversed pending")
                    fixture.unknown(claimed[2])
                    with patch.object(rt.team, "_pending_operations",
                                      wraps=rt.team._pending_operations) as reads:
                        self.assertFalse(any(rt.team.external.owns(f.task, row) for row in rows))
                        self.assertLessEqual(reads.call_count, 2)
                    for iterator in iterators:
                        list(iterator)
                    self.assertTrue(any(row["name"] == "workspace_run"
                                        for row in rt.team._pending_operations(f.task)))
            finally:
                for iterator in reversed(iterators):
                    iterator.close()
                rt.team.external.end_scheduler()
            self.assertTrue(rt.verify_chain().ok)

    def test_second_supervisor_is_not_admitted_while_first_is_live(self):
        self.exercise_supervisors()

    def test_mutual_supervisor_callbacks_do_not_recurse_or_hide_unknown_effect(self):
        self.exercise_supervisors(direct_callback_stress=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
