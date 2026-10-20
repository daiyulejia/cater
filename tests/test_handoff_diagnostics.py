"""Bounded citation diagnostics preserve the existing handoff trust boundary."""

import copy
import json
import unittest

from cater.events import Actor
from cater.team_delivery import HandoffError, HOST
import test_simple_team
import test_team_handoff


class HandoffDiagnosticsTests(unittest.TestCase):
    setUp = test_team_handoff.TeamHandoffTests.setUp
    artifact = test_team_handoff.TeamHandoffTests.artifact
    refs = test_team_handoff.TeamHandoffTests.refs
    handoff = test_team_handoff.TeamHandoffTests.handoff
    normalize = test_team_handoff.TeamHandoffTests.normalize
    receipt = test_team_handoff.TeamHandoffTests.receipt

    def rejected(self, evidence):
        before = self.rt.log.read()
        value = self.handoff(evidence)
        original = copy.deepcopy(value)
        with self.assertRaises(HandoffError) as caught:
            self.normalize(value)
        self.assertEqual(value, original)
        self.assertEqual(self.rt.log.read(), before)
        self.assertEqual(self.driver.calls, [])
        return caught.exception

    def review(self):
        request = self.rt.log.emit(type="team.review_requested", task_id=self.task, actor=HOST,
            payload={"root_seq": None, "revision": 0, "author": self.manager, "reviewer": self.worker,
                     "instruction": "Inspect selected bytes", "artifacts": self.refs(), "run_id": "review-fixture"})
        return self.rt.log.emit(type="team.review_recorded", task_id=self.task, actor=HOST,
            payload={"root_seq": request.seq, "review_seq": request.seq, "reviewer": self.worker,
                     "verdict": "approved", "feedback": "Reviewed exact source; no integration run"})

    def test_valid_sources_and_deduplication_are_unchanged(self):
        artifact = self.artifact("selected source")
        receipt = self.receipt()
        review = self.review()
        refs = [{"source_seq": e.seq, "quote": quote} for e, quote in (
            (artifact, "selected source"), (receipt, "Ran 5 tests"), (review, "Reviewed exact source"))]
        normalized = self.normalize(self.handoff([*refs, refs[0]]))
        self.assertEqual(normalized["version"], 1)
        expected = [
            {**refs[0], "source_hash": artifact.hash, "kind": "artifact", "role": self.manager,
             "filename": artifact.payload["filename"], "content_sha256": artifact.payload["content_sha256"]},
            {**refs[1], "source_hash": receipt.hash, "kind": "tool_receipt", "role": self.worker,
             "tool": "workspace_run", "status": "ok", "exit_code": 0},
            {**refs[2], "source_hash": review.hash, "kind": "peer_review", "role": self.worker,
             "status": "approved"},
        ]
        self.assertEqual(normalized["evidence"], expected)
        self.assertEqual(HandoffError("existing message").diagnostics, [])

    def test_finish_dispatch_returns_diagnostics_and_same_run_can_correct(self):
        artifact = self.artifact("selected source")
        selected = self.refs()
        arguments = {"summary": "Selected source delivered", "verification": "Source inspection only",
                     "artifact_seqs": [artifact.seq]}
        invalid = self.handoff([
            {"source_seq": 999999, "quote": "unavailable"},
            {"source_seq": artifact.seq, "quote": "wrong quote"},
        ])
        corrected = self.handoff([{"source_seq": artifact.seq, "quote": "selected source"}])
        observed = []

        def repair(context, history, tools):
            response = history[-1]
            self.assertEqual(response["role"], "tool")
            self.assertTrue(response["is_error"])
            receipt = json.loads(response["content"])
            self.assertEqual((receipt["status"], receipt["code"]), ("error", "invalid_handoff"))
            self.assertEqual([(d["evidence_index"], d["source_seq"], d["reason"])
                              for d in receipt["diagnostics"]],
                             [(0, 999999, "source_unavailable"), (1, artifact.seq, "quote_mismatch")])
            self.assertTrue(all(d["guidance"] for d in receipt["diagnostics"]))
            self.assertEqual(list(self.rt.log.iter_events(task_id=self.task,
                             event_types=("team.objective_completed",))), [])
            self.assertEqual(self.refs(), selected)
            observed.append(receipt)
            return test_simple_team.turn("", ("finish_work", {**arguments, "handoff": corrected}))

        self.driver.steps[self.manager] = [
            test_simple_team.turn("", ("finish_work", {**arguments, "handoff": invalid})),
            repair,
        ]
        self.rt.team.receive(self.task, "Deliver the selected source with exact citations")
        self.rt.collaborate(self.task)
        events = self.rt.log.read(task_id=self.task)
        receipts = [e for e in events if e.type == "team.tool_finished"
                    and e.payload["name"] == "finish_work"]
        self.assertEqual(len(receipts), 2)
        self.assertEqual(observed, [receipts[0].payload["result"]])
        self.assertEqual(receipts[1].payload["result"]["status"], "ok")
        self.assertTrue(all(e.actor == HOST for e in receipts))
        run, = [e for e in events if e.type == "team.run_started"]
        self.assertTrue(all(e.payload["run_id"] == run.payload["run_id"] for e in receipts))
        completed, = [e for e in events if e.type == "team.objective_completed"]
        self.assertEqual(completed.actor, HOST)
        self.assertEqual(completed.payload["run_id"], run.payload["run_id"])
        self.assertLess(receipts[0].seq, completed.seq)
        self.assertEqual(completed.payload["artifacts"], selected)
        evidence, = completed.payload["handoff"]["evidence"]
        self.assertEqual((evidence["source_seq"], evidence["source_hash"], evidence["quote"]),
                         (artifact.seq, artifact.hash, "selected source"))
        self.assertEqual([e.seq for e in events if e.type == "deliverable.published"], [artifact.seq])
        self.assertEqual(self.rt.team.view(self.task)["status"], "done")
        self.assertEqual(len(self.driver.calls), 2)
        self.assertEqual(self.driver.steps[self.manager], [])
        self.assertTrue(self.rt.verify_chain().ok)

    def test_all_errors_report_original_indexes_in_input_order(self):
        artifact = self.artifact("CURRENT_SOURCE_SECRET")
        receipt = self.receipt()
        unsupported = self.receipt(name="record_review")
        refs = [
            {"source_seq": artifact.seq, "quote": "CURRENT_SOURCE_SECRET"},
            {"source_seq": unsupported.seq, "quote": "Ran 5 tests"},
            {"source_seq": receipt.seq, "quote": "Ran 5 tests"},
            {"source_seq": artifact.seq, "quote": "WRONG_QUOTE_SECRET"},
        ]
        error = self.rejected(refs)
        self.assertEqual(str(error), "交付引用与当前成果、工具回执或版本复核原文不符，请读取实际来源后修正。")
        self.assertEqual([(d["evidence_index"], d["source_seq"], d["reason"]) for d in error.diagnostics],
                         [(1, unsupported.seq, "source_unavailable"), (3, artifact.seq, "quote_mismatch")])
        self.assertEqual(error.diagnostics, self.rejected(refs).diagnostics)
        public = json.dumps({"message": str(error), "diagnostics": error.diagnostics}, ensure_ascii=False)
        for secret in ("CURRENT_SOURCE_SECRET", "WRONG_QUOTE_SECRET", "Ran 5 tests"):
            self.assertNotIn(secret, public)
        for d in error.diagnostics:
            self.assertEqual(set(d), {"evidence_index", "source_seq", "reason", "guidance"})
            self.assertLessEqual(len(d["guidance"]), 160)
        guidance = error.diagnostics[0]["guidance"]
        for term in ("选中成果", "当前目标", "decision_seq", "feedback", "record_review", "旧目标"):
            self.assertIn(term, guidance)

    def test_followup_pattern_reports_both_unsupported_review_receipt_and_old_goal(self):
        old = self.receipt()
        self.rt.log.emit(type="team.objective_updated", task_id=self.task, actor=HOST,
                         payload={"goal": "New objective"})
        document = self.artifact("current document")
        revision = self.rt.team.view(self.task)["revision"]
        # Helpers emit revision-zero receipts; construct a fully bound current one.
        message = self.rt.team.receive(self.task, "Current check")
        claimed_message, role, run = self.rt.team._claim(self.task)
        self.assertEqual(claimed_message.seq, message.seq)
        start = self.rt.log.emit(type="team.tool_started", task_id=self.task, actor=HOST,
            payload={"message_seq": message.seq, "run_id": run.payload["run_id"], "role": role.id,
                     "revision": revision, "name": "workspace_run", "call_id": "current-check"})
        stdout = self.rt.log.emit(type="team.tool_finished", task_id=self.task, actor=HOST,
            payload={"started_seq": start.seq, "run_id": run.payload["run_id"], "role": role.id,
                     "name": "workspace_run", "result": {"status": "ok", "stdout": "current stdout"}})
        unsupported = self.receipt(name="record_review")
        error = self.rejected([
            {"source_seq": document.seq, "quote": "current document"},
            {"source_seq": stdout.seq, "quote": "current stdout"},
            {"source_seq": unsupported.seq, "quote": "Ran 5 tests"},
            {"source_seq": old.seq, "quote": "Ran 5 tests"},
        ])
        self.assertEqual([(d["evidence_index"], d["source_seq"], d["reason"]) for d in error.diagnostics],
                         [(2, unsupported.seq, "source_unavailable"), (3, old.seq, "source_unavailable")])

    def test_foreign_missing_and_unselected_sources_do_not_disclose_contents(self):
        old = self.artifact("UNSELECTED_SECRET")
        self.artifact("selected replacement")
        other = self.rt.create_task("FOREIGN_TASK_SECRET")
        foreign = self.artifact("FOREIGN_CONTENT_SECRET", task=other, filename="FOREIGN_FILE_SECRET.py")
        error = self.rejected([{"source_seq": e.seq, "quote": "PRIVATE_QUOTE_SECRET"} for e in (old, foreign)]
                              + [{"source_seq": 999999, "quote": "PRIVATE_QUOTE_SECRET"}])
        self.assertEqual([d["reason"] for d in error.diagnostics], ["source_unavailable"] * 3)
        self.assertEqual(len({d["guidance"] for d in error.diagnostics}), 1)
        public = json.dumps(error.diagnostics)
        for secret in ("UNSELECTED_SECRET", "FOREIGN_TASK_SECRET", "FOREIGN_CONTENT_SECRET",
                       "FOREIGN_FILE_SECRET", "PRIVATE_QUOTE_SECRET"):
            self.assertNotIn(secret, public)

    def test_all_sixteen_errors_are_bounded_and_deterministic(self):
        refs = [{"source_seq": 999999, "quote": "missing"}] * 16
        error = self.rejected(refs)
        self.assertEqual(len(error.diagnostics), 16)
        self.assertEqual([d["evidence_index"] for d in error.diagnostics], list(range(16)))
        self.assertEqual(error.diagnostics, self.rejected(refs).diagnostics)
        self.assertEqual(self.rejected([*refs, refs[0]]).diagnostics, [])

    def test_malformed_refs_keep_existing_rejection(self):
        for ref in (None, {}, {"source_seq": True, "quote": "x"},
                    {"source_seq": 1.0, "quote": "x"}, {"source_seq": -1, "quote": "x"},
                    {"source_seq": 1, "quote": ""}, {"source_seq": 1, "quote": "x" * 2001},
                    {"source_seq": 1, "quote": "x", "extra": "PRIVATE"}):
            with self.subTest(ref=ref):
                self.assertEqual(self.rejected([ref]).diagnostics, [])

    def test_forged_actor_and_outdated_review_remain_unavailable(self):
        self.artifact()
        review = self.review()
        self.artifact("replacement invalidates old review")
        forged = self.receipt(actor=Actor.agent(self.worker, "untrusted"))
        error = self.rejected([
            {"source_seq": review.seq, "quote": "Reviewed exact source"},
            {"source_seq": forged.seq, "quote": "Ran 5 tests"},
        ])
        self.assertEqual([d["reason"] for d in error.diagnostics], ["source_unavailable"] * 2)


if __name__ == "__main__":
    unittest.main()
