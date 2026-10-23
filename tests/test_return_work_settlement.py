"""Return receipts and restart safety using scripted drivers, not real models."""

from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from cater.runtime import Runtime
from cater.team import HOST, LOCAL_EVENT_CONTRACT, LOCAL_EVENT_TOOLS
from cater.tool_protocol import ToolCall
import test_return_work as returns
import test_simple_team as simple


class ReturnWorkSettlementTests(returns.ReturnWorkFixtures, unittest.TestCase):
    def test_receipt_failure_rolls_back_visible_report_then_same_turn_retry_succeeds(self):
        for mode in ("native", "external"):
            with self.subTest(mode=mode):
                f = self.fixture(mode)
                original = self.assign(f)
                emit = f.rt.log.emit
                failed = []

                def fail_first_receipt(**kwargs):
                    payload = kwargs.get("payload", {})
                    if kwargs["type"] == "team.tool_finished" and payload.get("name") == "return_work":
                        self.assertTrue(f.rt.log.db.in_transaction if payload["result"]["status"] == "ok"
                                        else not f.rt.log.db.in_transaction)
                        if not failed:
                            report, = self.reports(f, original)
                            self.assertIs(report.payload["visible"], True)
                            failed.append(report)
                            raise OSError("Injected receipt failure after report emission")
                        if payload["result"]["status"] == "error":
                            self.assertEqual(self.reports(f, original), [])
                            self.assertIn(original.seq, {r["seq"] for r in f.rt.team.recovery.assignments(f.task)})
                    return emit(**kwargs)

                with patch.object(f.rt.log, "emit", side_effect=fail_first_receipt):
                    rejected, accepted = self.run_calls(f, original, returns.RETURN, returns.RETURN,
                                                       statuses=("completed",))
                self.assertEqual(len(failed), 1)
                self.assertEqual(rejected["status"], "error", rejected)
                self.assert_return(f, original, accepted)
                self.assertEqual(f.rt.team._pending_operations(f.task), [])
                starts = [e for e in f.events("team.tool_started") if e.payload["name"] == "return_work"]
                self.assertEqual(len(starts), 2)
                self.assertTrue(all(e.payload["execution_contract"] == LOCAL_EVENT_CONTRACT for e in starts))
                self.assertIn("return_work", LOCAL_EVENT_TOOLS)

    def invoke_bound(self, f, original, *, role=None, text=returns.TEXT, name="return_work"):
        """Exercise validation of a captured host assignment through settlement."""
        role = f.rt.team._role(role or f.worker)
        source = self.human(f, role.id)
        run = self.start(f, source)
        arguments = {"text": text}
        if name == "send_message":
            arguments.update(recipient=original.actor.id, reply_to=original.seq)
        call = ToolCall(f"bound-{run.seq}", name, arguments)
        start = f.rt.log.emit(type="team.tool_started", actor=HOST, task_id=f.task, payload={
            **run.payload, "name": call.name, "call_id": call.id, "arguments": call.arguments,
            "execution_contract": dict(LOCAL_EVENT_CONTRACT),
        })
        result = f.rt.team._invoke_tool(f.task, role, run, run.payload["revision"], call,
            {"properties": {key: {} for key in arguments}, "required": list(arguments)},
            None, {}, start, original)
        f.rt.log.emit(type="team.run_finished", actor=HOST, task_id=f.task,
                      payload={**run.payload, "status": "completed"})
        return result

    def test_recorded_review_report_stays_idempotent_even_after_version_changes(self):
        for name in ("return_work", "send_message"):
            with self.subTest(tool=name):
                f = self.fixture()
                requested = f.request([f.publish("Original reviewed bytes\n")])
                self.assertEqual(requested["status"], "ok", requested)
                review = self.message(f, requested["message_seq"])
                recorded = f.record(requested["review_seq"], source=review)
                self.assertEqual(recorded["status"], "ok", recorded)
                report, = self.reports(f, review)
                for changed in (False, True):
                    if changed:
                        f.publish("Different bytes after the recorded decision\n")
                        self.assertEqual(f.rt.team_reviews.view(f.task)[0]["status"], "outdated")
                    repeated = self.invoke_bound(f, review, role=f.reviewer, name=name, text=report.payload["text"])
                    self.assertEqual((repeated["status"], repeated["message_seq"]), ("ok", report.seq))
                    rejected = self.invoke_bound(f, review, role=f.reviewer, name=name, text="Different report text")
                    self.assertEqual((rejected["status"], rejected["code"]),
                                     ("error", "assignment_already_returned"))
                    self.assertEqual(rejected["message_seq"], report.seq)
                    self.assertEqual(self.reports(f, review), [report])
                self.assertEqual(len(f.events("team.review_recorded")), 1)
                self.assertEqual(f.rt.team._pending_operations(f.task), [])
                self.assertTrue(f.rt.verify_chain().ok)

    def test_captured_assignment_reuses_direction_scope_withdrawal_and_duplicate_validation(self):
        for case in ("direction", "revision", "withdrawal", "duplicate"):
            with self.subTest(case=case):
                f = self.fixture()
                original = self.assign(f)
                role = f.worker
                if case == "direction":
                    role = f.reviewer
                    code = "reply_direction_mismatch"
                elif case == "revision":
                    changed, = self.run_calls(f, self.human(f), ("set_objective", {"goal": "New goal"}))
                    self.assertEqual(changed["status"], "ok")
                    code = "reply_scope_changed"
                elif case == "withdrawal":
                    changed, = self.run_calls(f, self.human(f), ("withdraw_work", {
                        "message_seq": original.seq, "reason": "Withdraw before delayed return.",
                    }))
                    self.assertEqual(changed["status"], "ok")
                    code = "reply_work_inactive"
                else:
                    first, = self.run_calls(f, original, returns.RETURN, statuses=("completed",))
                    self.assert_return(f, original, first)
                    repeated = self.invoke_bound(f, original)
                    self.assertEqual((repeated["status"], repeated["message_seq"]), ("ok", first["message_seq"]))
                    code = "assignment_already_returned"
                before = list(self.reports(f, original))
                result = self.invoke_bound(f, original, role=role, text="A changed report")
                self.assertEqual((result["status"], result["code"]), ("error", code))
                self.assertEqual(self.reports(f, original), before)
                self.assertEqual(f.rt.team._pending_operations(f.task), [])
                self.assertTrue(f.rt.verify_chain().ok)

    def test_process_exit_before_receipt_or_after_commit_never_replays_or_double_reports(self):
        code = r'''
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / "tests"))
from cater.runtime import Runtime
import test_simple_team as simple
driver = simple.TeamDriver()
with Runtime(sys.argv[1], roles_path=sys.argv[2], drivers={driver.name: driver}) as rt:
    task, worker, stage, text = sys.argv[3:]
    driver.steps[worker] = [simple.turn("", ("return_work", {"text": text}))]
    if stage == "receipt":
        emit = rt.log.emit
        def crash(**kwargs):
            if kwargs["type"] == "team.tool_finished" and kwargs["payload"]["name"] == "return_work":
                os._exit(37)
            return emit(**kwargs)
        rt.log.emit = crash
    else:
        def crash(*args, **kwargs):
            os._exit(38)
        rt.team._finish_message = crash
    rt.collaborate(task)
'''
        for stage in ("receipt", "run_finish"):
            with self.subTest(stage=stage):
                f = self.fixture()
                original = self.assign(f)
                completed = subprocess.run([
                    sys.executable, "-X", "utf8", "-B", "-c", code, str(f.rt.log.path), str(f.roles),
                    f.task, f.worker, stage, returns.TEXT,
                ], cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=20)
                self.assertEqual(completed.returncode, 37 if stage == "receipt" else 38,
                                 completed.stderr.decode("utf-8", errors="replace"))
                committed = stage == "run_finish"
                self.assertEqual(len(self.reports(f, original)), int(committed))
                driver = simple.TeamDriver()
                with Runtime(f.rt.log.path, roles_path=f.roles, drivers={driver.name: driver}) as reopened:
                    driver.steps = {r.id: [simple.turn("", ("wait_for_reply", {}))] * 3
                                    for r in [reopened.organization.leader, *reopened.worker_roles]}
                    reopened.team.run(f.task)
                    reports = [e for e in reopened.team._messages(f.task)
                               if e.payload["kind"] == "report" and e.payload.get("reply_to") == original.seq]
                    self.assertEqual(len(reports), int(committed))
                    starts = [e for e in reopened.log.read(task_id=f.task)
                              if e.type == "team.tool_started" and e.payload["name"] == "return_work"]
                    self.assertEqual(len(starts), 1, "Restart replayed the interrupted return tool")
                    self.assertEqual(starts[0].payload["execution_contract"], LOCAL_EVENT_CONTRACT)
                    pending = reopened.team._pending_operations(f.task)
                    if committed:
                        self.assertEqual(pending, [])
                    else:
                        operation, = pending
                        self.assertEqual(operation["outcome"], "not_committed")
                        self.assertIs(operation["replay_authorized"], False)
                        self.assertIn(original.seq, {r["seq"] for r in reopened.team.recovery.assignments(f.task)})
                    calls = len(driver.calls)
                    reopened.team.run(f.task)
                    self.assertEqual(len(driver.calls), calls)
                    self.assertEqual(reopened.team.view(f.task)["status"], "running")
                    self.assertTrue(reopened.verify_chain().ok)


if __name__ == "__main__":
    unittest.main()
