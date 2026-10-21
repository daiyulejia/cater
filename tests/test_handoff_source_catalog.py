"""Citation recovery contract; all drivers and source bodies here are local fixtures."""

import copy
import hashlib
import json
import unittest
from unittest.mock import patch

from cater.events import Actor
from cater.team_delivery import HandoffError, HOST
import test_simple_team
import test_team_handoff


CATALOG_KEYS = {
    "available_sources", "available_sources_total", "available_sources_truncated",
    "available_sources_note",
}
ENTRY_KEYS = {
    "source_seq", "kind", "filename", "role", "tool", "status", "exit_code",
    "complete", "truncated", "read_tool", "read_args",
}
_UNSET = object()


class HandoffSourceCatalogTests(unittest.TestCase):
    setUp = test_team_handoff.TeamHandoffTests.setUp
    artifact = test_team_handoff.TeamHandoffTests.artifact
    refs = test_team_handoff.TeamHandoffTests.refs
    handoff = test_team_handoff.TeamHandoffTests.handoff
    normalize = test_team_handoff.TeamHandoffTests.normalize
    receipt = test_team_handoff.TeamHandoffTests.receipt
    finish = test_team_handoff.TeamHandoffTests.finish
    events = test_team_handoff.TeamHandoffTests.events
    run_ok = test_team_handoff.TeamHandoffTests.run_ok

    def review(self, feedback="REVIEW_BODY_SECRET", *, actor=HOST, verdict="approved"):
        request = self.rt.log.emit(type="team.review_requested", task_id=self.task, actor=HOST,
            payload={"root_seq": None, "revision": self.rt.team.view(self.task)["revision"],
                     "author": self.manager, "reviewer": self.worker,
                     "instruction": "Synthetic exact-byte review", "artifacts": self.refs(),
                     "run_id": "catalog-review-fixture"})
        return self.rt.log.emit(type="team.review_recorded", task_id=self.task, actor=actor,
            payload={"root_seq": request.seq, "review_seq": request.seq, "reviewer": self.worker,
                     "verdict": verdict, "feedback": feedback})

    def rejected(self, evidence=None, *, value=_UNSET, artifacts=None):
        if value is _UNSET:
            value = self.handoff(evidence or [{"source_seq": 999999, "quote": "WRONG_QUOTE_SECRET"}])
        original = copy.deepcopy(value)
        before = self.rt.log.read()
        calls = copy.deepcopy(self.driver.calls)
        with self.assertRaises(HandoffError) as caught:
            self.rt.team.delivery.normalize(self.task, value,
                artifacts=self.refs() if artifacts is None else artifacts,
                revision=self.rt.team.view(self.task)["revision"])
        self.assertEqual(value, original)
        self.assertEqual(self.rt.log.read(), before)
        self.assertEqual(self.driver.calls, calls)
        return caught.exception

    def catalog(self, error):
        catalog = error.source_catalog
        self.assertEqual(set(catalog), CATALOG_KEYS)
        rows = catalog["available_sources"]
        self.assertIs(type(catalog["available_sources_total"]), int)
        self.assertIs(type(catalog["available_sources_truncated"]), bool)
        self.assertEqual(catalog["available_sources_truncated"], catalog["available_sources_total"] > len(rows))
        self.assertLessEqual(len(rows), 12)
        self.assertTrue(catalog["available_sources_note"].strip())
        self.assertLessEqual(len(catalog["available_sources_note"]), 1000)
        for row in rows:
            self.assertLessEqual(set(row), ENTRY_KEYS)
            self.assertTrue({"source_seq", "kind", "read_tool", "read_args"} <= set(row))
            self.assertIs(type(row["source_seq"]), int)
            self.assertGreater(row["source_seq"], 0)
            for key, limit in (("filename", 256), ("role", 128), ("tool", 64), ("status", 64)):
                if key in row:
                    self.assertIsInstance(row[key], str)
                    self.assertTrue(0 < len(row[key]) <= limit)
            for key in ("complete", "truncated"):
                if key in row:
                    self.assertIs(type(row[key]), bool)
            if "exit_code" in row:
                self.assertIs(type(row["exit_code"]), int)
            if row["kind"] == "artifact":
                self.assertEqual((row["read_tool"], row["read_args"]),
                    ("read_artifact", {"artifact_seq": row["source_seq"]}))
            elif row["kind"] == "peer_review":
                decision = next(e for e in self.rt.log.read(task_id=self.task) if e.seq == row["source_seq"])
                self.assertEqual((row["read_tool"], row["read_args"]),
                    ("read_checkpoint", {"review_seq": decision.payload["review_seq"]}))
            else:
                self.assertEqual(row["kind"], "tool_receipt")
                self.assertEqual((row["read_tool"], row["read_args"]),
                    ("read_receipts", {"before_seq": row["source_seq"] + 1, "limit": 1}))
        return catalog

    def test_default_error_catalog_is_empty_and_independent(self):
        first, second = HandoffError("one"), HandoffError("two")
        self.assertEqual(first.source_catalog, {})
        self.assertEqual(second.source_catalog, {})
        first.source_catalog["local"] = True
        self.assertEqual(second.source_catalog, {})

    def test_empty_catalog_has_zero_total_and_false_truncation(self):
        catalog = self.catalog(self.rejected())
        self.assertEqual(catalog["available_sources"], [])
        self.assertEqual(catalog["available_sources_total"], 0)
        self.assertIs(catalog["available_sources_truncated"], False)

    def test_unavailable_and_quote_mismatch_share_precise_catalog(self):
        artifact = self.artifact("ARTIFACT_BODY_SECRET")
        missing = self.rejected()
        mismatch = self.rejected([{"source_seq": artifact.seq, "quote": "WRONG_QUOTE_SECRET"}])
        self.assertEqual(missing.diagnostics[0]["reason"], "source_unavailable")
        self.assertEqual(mismatch.diagnostics[0]["reason"], "quote_mismatch")
        self.assertEqual(self.catalog(missing), self.catalog(mismatch))
        self.assertEqual([r["source_seq"] for r in missing.source_catalog["available_sources"]], [artifact.seq])
        for secret in ("ARTIFACT_BODY_SECRET", "WRONG_QUOTE_SECRET", artifact.hash,
                       artifact.payload["content_sha256"]):
            self.assertNotIn(secret, json.dumps(missing.source_catalog))

    def test_group_priority_then_descending_sequence_is_not_global_recency(self):
        artifacts = [self.artifact("A", filename=f"part-{i}.txt") for i in range(3)]
        reviews = [self.review() for _ in range(2)]
        receipts = [self.receipt() for _ in range(3)]
        catalog = self.catalog(self.rejected())
        self.assertEqual([r["source_seq"] for r in catalog["available_sources"]],
            [e.seq for group in (artifacts, reviews, receipts) for e in reversed(group)])
        self.assertEqual(catalog["available_sources_total"], 8)

    def test_limit_boundaries_and_large_catalog_preserve_exact_total(self):
        artifacts = []
        for count in (0, 1, 11, 12, 13, 129):
            while len(artifacts) < count:
                artifacts.append(self.artifact("synthetic body", filename=f"part-{len(artifacts)}.txt"))
            with self.subTest(count=count):
                catalog = self.catalog(self.rejected())
                self.assertEqual(catalog["available_sources_total"], count)
                self.assertEqual(catalog["available_sources_truncated"], count > 12)
                self.assertEqual([r["source_seq"] for r in catalog["available_sources"]],
                                 [e.seq for e in reversed(artifacts)][:12])
                self.assertLess(len(json.dumps(catalog)), 12000)

    def test_twelve_row_cut_spans_groups_in_priority_order(self):
        artifacts = [self.artifact("A", filename=f"part-{i}.txt") for i in range(8)]
        reviews = [self.review() for _ in range(3)]
        receipts = [self.receipt() for _ in range(4)]
        catalog = self.catalog(self.rejected())
        self.assertEqual(catalog["available_sources_total"], 15)
        self.assertTrue(catalog["available_sources_truncated"])
        self.assertEqual([r["source_seq"] for r in catalog["available_sources"]],
            [e.seq for e in reversed(artifacts)] + [e.seq for e in reversed(reviews)] + [receipts[-1].seq])

    def test_exact_selected_sources_exclude_foreign_unselected_and_forged_artifacts(self):
        old = self.artifact("UNSELECTED_BODY_SECRET", filename="same.txt")
        current = self.artifact("CURRENT_BODY_SECRET", filename="same.txt")
        omitted = self.artifact("OMITTED_BODY_SECRET", filename="omitted.txt")
        foreign = self.artifact("FOREIGN_BODY_SECRET", task=self.rt.create_task("FOREIGN_TASK_SECRET"),
                                filename="FOREIGN_FILENAME_SECRET.txt")
        forged = self.artifact("FORGED_BODY_SECRET", filename="forged.txt", actor=Actor.system("untrusted"))
        chosen = [r for r in self.refs() if r["seq"] == current.seq]
        error = self.rejected([{"source_seq": e.seq, "quote": "body"}
                               for e in (old, omitted, foreign, forged)], artifacts=chosen)
        self.assertEqual([r["source_seq"] for r in self.catalog(error)["available_sources"]], [current.seq])
        self.assertEqual([d["reason"] for d in error.diagnostics], ["source_unavailable"] * 4)
        for secret in ("UNSELECTED_BODY_SECRET", "OMITTED_BODY_SECRET", "FOREIGN_BODY_SECRET",
                       "FOREIGN_TASK_SECRET", "FOREIGN_FILENAME_SECRET", "FORGED_BODY_SECRET"):
            self.assertNotIn(secret, json.dumps(error.source_catalog))

    def test_binary_zip_run_start_and_record_review_outer_receipt_are_excluded(self):
        artifact = self.artifact("actual source")
        archive = self.rt.log.emit(type="deliverable.published", task_id=self.task,
            actor=Actor.agent(self.manager, self.rt.organization.leader.role),
            payload={"filename": "project.zip", "title": "project.zip", "format": "archive",
                     "content_bytes": 123, "content_sha256": "a" * 64, "summary": "Binary fixture"})
        review = self.review()
        outer = self.receipt(name="record_review", result={"status": "ok",
            "decision_seq": review.seq, "content": "OUTER_RECEIPT_BODY_SECRET"})
        run = self.events("team.run_started")[-1]
        error = self.rejected([{"source_seq": e.seq, "quote": "wrong"} for e in (archive, outer, run)])
        self.assertEqual([d["reason"] for d in error.diagnostics], ["source_unavailable"] * 3)
        self.assertEqual([r["source_seq"] for r in self.catalog(error)["available_sources"]],
                         [artifact.seq, review.seq])

    def test_old_objective_receipt_stale_review_and_forged_host_are_excluded(self):
        self.artifact("old source")
        review = self.review()
        old = self.receipt()
        self.rt.log.emit(type="team.objective_updated", task_id=self.task, actor=HOST,
                         payload={"goal": "Synthetic new objective"})
        artifact = self.artifact("current source")
        forged = self.receipt(actor=Actor.system("not-team"))
        agent = self.receipt(actor=Actor.agent(self.worker, "untrusted"))
        error = self.rejected([{"source_seq": e.seq, "quote": "wrong"} for e in (review, old, forged, agent)])
        self.assertEqual([d["reason"] for d in error.diagnostics], ["source_unavailable"] * 4)
        self.assertEqual([r["source_seq"] for r in self.catalog(error)["available_sources"]], [artifact.seq])

    def test_outdated_review_and_empty_text_parts_are_excluded(self):
        self.artifact("old source")
        outdated = self.review()
        artifact = self.artifact("current replacement")
        empty_review = self.review(" \n\t")
        empty = self.receipt(result={"status": "ok", "stdout": "", "stderr": {"excerpt": " \n"},
                                     "raw": "RAW_SECRET", "private": "PRIVATE_SECRET"})
        blank = " \n\t"
        empty_artifact = self.rt.log.emit(type="deliverable.published", task_id=self.task,
            actor=Actor.agent(self.manager, self.rt.organization.leader.role),
            payload={"filename": "blank.txt", "content": blank,
                     "content_sha256": hashlib.sha256(blank.encode()).hexdigest()})
        catalog = self.catalog(self.rejected())
        self.assertEqual([r["source_seq"] for r in catalog["available_sources"]], [artifact.seq])
        self.assertTrue({outdated.seq, empty_review.seq, empty.seq, empty_artifact.seq}.isdisjoint(
            r["source_seq"] for r in catalog["available_sources"]))

    def test_receipt_text_shapes_and_error_metadata_remain_truthful(self):
        receipts = [
            self.receipt(result={"status": "error", "exit_code": 2, "complete": False,
                "truncated": True, "stderr": {"excerpt": "ERROR_BODY_SECRET", "utf8_valid": False},
                "path": "PATH_SECRET", "argv": ["ARGV_SECRET"], "private": "PRIVATE_SECRET"}),
            self.receipt(result={"status": "ok", "stdout": {"preview": "HEAD_BODY_SECRET",
                "preview_mode": "head_tail", "tail_excerpt": "TAIL_BODY_SECRET", "truncated": True,
                "complete": False}}),
            self.receipt(result={"status": "error", "error": {"message": "ERROR_MESSAGE_SECRET"}}),
            self.receipt(result={"status": "ok", "content": "CONTENT_BODY_SECRET"}),
        ]
        catalog = self.catalog(self.rejected())
        self.assertEqual([r["source_seq"] for r in catalog["available_sources"]], [e.seq for e in reversed(receipts)])
        row = catalog["available_sources"][-1]
        self.assertEqual({k: row[k] for k in ("status", "exit_code", "complete", "truncated")},
                         {"status": "error", "exit_code": 2, "complete": False, "truncated": True})
        text = json.dumps(catalog)
        for secret in ("ERROR_BODY_SECRET", "HEAD_BODY_SECRET", "TAIL_BODY_SECRET", "ERROR_MESSAGE_SECRET",
                       "CONTENT_BODY_SECRET", "PATH_SECRET", "ARGV_SECRET", "PRIVATE_SECRET", "output_utf8_valid"):
            self.assertNotIn(secret, text)

    def test_catalog_uses_one_existing_source_snapshot_and_filters_metadata(self):
        artifact = self.artifact("BODY_SECRET")
        receipt = self.receipt()
        original = self.rt.team.delivery._sources(self.task, self.refs(), 0)
        poisoned = copy.deepcopy(original)
        metadata = poisoned[receipt.seq][2]
        metadata.update(filename="f" * 10000, role="r" * 10000, tool="workspace_" + "t" * 10000,
            status="STATUS_SECRET" * 1000, exit_code=10 ** 100, complete="COMPLETE_SECRET",
            truncated=1, body="BODY_SECRET", quote="QUOTE_SECRET", hash="HASH_SECRET", path="PATH_SECRET",
            argv=["ARGV_SECRET"], raw="RAW_SECRET", private="PRIVATE_SECRET")
        snapshot = copy.deepcopy(poisoned)
        with patch.object(self.rt.team.delivery, "_sources", return_value=poisoned) as sources:
            catalog = self.catalog(self.rejected())
        sources.assert_called_once_with(self.task, self.refs(), 0)
        self.assertEqual([r["source_seq"] for r in catalog["available_sources"]], [artifact.seq, receipt.seq])
        self.assertEqual(poisoned, snapshot)
        for secret in ("BODY_SECRET", "QUOTE_SECRET", "HASH_SECRET", "PATH_SECRET", "ARGV_SECRET",
                       "RAW_SECRET", "PRIVATE_SECRET", "STATUS_SECRET", "COMPLETE_SECRET"):
            self.assertNotIn(secret, json.dumps(catalog))
        self.assertLess(len(json.dumps(catalog)), 12000)

    def test_schema_errors_never_construct_catalog_even_after_bad_citation(self):
        source = self.artifact("source")
        bad_ref = {"source_seq": 999999, "quote": "wrong"}
        values = [None, {}, {**self.handoff(), "private": "PRIVATE_SECRET"},
                  {**self.handoff(), "why": ""}, {**self.handoff(), "evidence": "bad"},
                  self.handoff([bad_ref] * 17),
                  {**self.handoff(), "impact": ["x" * 1001]}]
        for ref in (None, {}, {"source_seq": True, "quote": "x"}, {"source_seq": 1.0, "quote": "x"},
                    {"source_seq": -1, "quote": "x"}, {"source_seq": source.seq, "quote": ""},
                    {"source_seq": source.seq, "quote": "x" * 2001},
                    {"source_seq": source.seq, "quote": "x", "extra": "PRIVATE_SECRET"}):
            values.append(self.handoff([bad_ref, ref]))
        for value in values:
            with self.subTest(value=value):
                error = self.rejected(value=value)
                self.assertEqual(error.diagnostics, [])
                self.assertEqual(error.source_catalog, {})
        oversized = {**self.handoff(), "changed": ["a" * 1000] * 12, "impact": ["b" * 1000] * 12}
        self.assertEqual(self.rejected(value=oversized).source_catalog, {})

    def test_no_handoff_or_no_evidence_finish_never_enumerates_sources(self):
        self.artifact("source")
        with patch.object(self.rt.team.delivery, "_sources", side_effect=AssertionError("unexpected source scan")) as sources:
            self.assertEqual(self.finish()["status"], "ok")
            self.rt.log.emit(type="team.objective_updated", task_id=self.task, actor=HOST,
                             payload={"goal": "Synthetic explanation-only followup"})
            note = self.handoff()
            del note["evidence"]
            self.assertEqual(self.finish(note)["status"], "ok")
            self.assertEqual(self.normalize(self.handoff())["evidence"], [])
        sources.assert_not_called()
        for event in self.events("team.objective_completed"):
            self.assertTrue(CATALOG_KEYS.isdisjoint(event.payload))
            if "handoff" in event.payload:
                self.assertEqual(event.payload["handoff"]["version"], 1)
                self.assertTrue(CATALOG_KEYS.isdisjoint(event.payload["handoff"]))

    def test_schema_dispatch_receipt_has_no_catalog_or_completion(self):
        self.artifact("source")
        result = self.finish({**self.handoff(), "why": ""})
        self.assertEqual((result["status"], result["code"]), ("error", "invalid_handoff"))
        self.assertTrue(CATALOG_KEYS.isdisjoint(result))
        self.assertEqual(self.events("team.objective_completed"), [])

    def test_unknown_operation_gate_still_precedes_citation_catalog(self):
        artifact = self.artifact("source")
        self.receipt(result={"status": "error", "complete": False, "stdout": "unfinished"})
        with patch.object(self.rt.team.delivery, "normalize", side_effect=AssertionError("gate bypass")) as normalize:
            result = self.finish(self.handoff([{"source_seq": artifact.seq, "quote": "wrong"}]))
        normalize.assert_not_called()
        self.assertEqual((result["status"], result["code"]), ("error", "operation_pending"))
        self.assertTrue(CATALOG_KEYS.isdisjoint(result))
        self.assertEqual(self.events("team.objective_completed"), [])

    def test_successful_normalization_keeps_v1_evidence_and_no_catalog(self):
        artifact = self.artifact("SOURCE_BODY")
        receipt = self.receipt()
        review = self.review("REVIEW_BODY")
        refs = [{"source_seq": e.seq, "quote": text} for e, text in
                ((artifact, "SOURCE_BODY"), (review, "REVIEW_BODY"), (receipt, "Ran 5 tests"))]
        before = self.rt.log.read()
        with patch.object(self.rt.team.delivery, "_sources", wraps=self.rt.team.delivery._sources) as sources:
            result = self.normalize(self.handoff([*refs, refs[0]]))
        sources.assert_called_once()
        self.assertEqual(result["version"], 1)
        self.assertEqual([r["source_seq"] for r in result["evidence"]], [r["source_seq"] for r in refs])
        self.assertEqual([r["source_hash"] for r in result["evidence"]], [artifact.hash, review.hash, receipt.hash])
        self.assertTrue(CATALOG_KEYS.isdisjoint(result))
        self.assertEqual(self.rt.log.read(), before)
        self.assertEqual(self.driver.calls, [])

    def test_fixed_driver_full_dispatch_reads_each_catalog_source_then_corrects_same_run(self):
        """Real Runtime/tool dispatch; scripted driver, no provider or real task replay."""
        artifact = self.artifact("ARTIFACT_BODY_SECRET")
        review = self.review("REVIEW_BODY_SECRET")
        receipt = self.receipt(result={"status": "error", "exit_code": 2, "complete": True,
            "truncated": True, "stdout": "TOOL_BODY_SECRET"})
        unsupported = self.receipt(name="record_review")
        self.driver.steps[self.manager] = [test_simple_team.turn("Synthetic fixture report received")
                                           for _ in range(2)]
        self.rt.collaborate(self.task)
        self.assertEqual(self.driver.steps[self.manager], [])
        fixture_calls = len(self.driver.calls)
        fixture_tokens = self.rt.task(self.task).tokens
        fixture_driver_tokens = self.rt._reported_driver_tokens("team_test")
        selected = self.refs()
        arguments = {"summary": "Synthetic source delivery", "verification": "Fixture observations only",
                     "artifact_seqs": [artifact.seq]}
        expected = {artifact.seq: "ARTIFACT_BODY_SECRET", review.seq: "REVIEW_BODY_SECRET",
                    receipt.seq: "TOOL_BODY_SECRET"}
        observed, reads = [], []

        def read_next(context, history, tools):
            self.assertEqual(history[-1]["role"], "tool")
            result = json.loads(history[-1]["content"])
            if not observed:
                self.assertTrue(history[-1]["is_error"])
                self.assertEqual((result["status"], result["code"]), ("error", "invalid_handoff"))
                self.assertNotIn("source_catalog", result)
                self.assertEqual([d["reason"] for d in result["diagnostics"]],
                                 ["source_unavailable", "quote_mismatch"])
                self.assertEqual(result.keys() & CATALOG_KEYS, CATALOG_KEYS)
                observed.append(result)
                rows = result["available_sources"]
                self.assertEqual([r["source_seq"] for r in rows], list(expected))
                self.assertEqual(result["available_sources_total"], 3)
                self.assertFalse(result["available_sources_truncated"])
            else:
                row = observed[0]["available_sources"][len(reads)]
                self.assertEqual(result["status"], "ok")
                if row["kind"] == "artifact":
                    self.assertEqual((result["seq"], result["content"]), (artifact.seq, expected[artifact.seq]))
                elif row["kind"] == "peer_review":
                    self.assertEqual((result["review"]["decision_seq"], result["review"]["feedback"]),
                                     (review.seq, expected[review.seq]))
                else:
                    returned, = result["receipts"]
                    self.assertEqual((returned["seq"], returned["result"]["stdout"]),
                                     (receipt.seq, expected[receipt.seq]))
                reads.append({"source_seq": row["source_seq"], "quote": expected[row["source_seq"]]})
            self.assertEqual(self.events("team.objective_completed"), [])
            self.assertEqual(self.refs(), selected)
            if len(reads) < 3:
                row = observed[0]["available_sources"][len(reads)]
                self.assertIn(row["read_tool"], {t["name"] for t in tools})
                return test_simple_team.turn("", (row["read_tool"], row["read_args"]))
            return test_simple_team.turn("", ("finish_work", {**arguments, "handoff": self.handoff(reads)}))

        before = self.rt.log.read(task_id=self.task)
        self.driver.steps[self.manager] = [
            test_simple_team.turn("", ("finish_work", {**arguments, "handoff": self.handoff([
                {"source_seq": unsupported.seq, "quote": "wrong"},
                {"source_seq": artifact.seq, "quote": "WRONG_QUOTE_SECRET"}])})),
            read_next, read_next, read_next, read_next,
        ]
        self.rt.team.receive(self.task, "Synthetic citation recovery; no real provider", request_id="catalog-recovery")
        self.rt.collaborate(self.task)
        events = self.rt.log.read(task_id=self.task)[len(before):]
        finished = [e for e in events if e.type == "team.tool_finished"]
        self.assertFalse([e.payload for e in events if e.type == "team.run_finished"
                          and e.payload["status"] == "failed"], repr([e.payload for e in finished]))
        self.assertEqual([e.payload["name"] for e in finished],
                         ["finish_work", "read_artifact", "read_checkpoint", "read_receipts", "finish_work"])
        self.assertEqual(finished[0].payload["result"], observed[0])
        self.assertEqual(finished[-1].payload["result"]["status"], "ok")
        self.assertTrue(CATALOG_KEYS.isdisjoint(finished[-1].payload["result"]))
        run, = [e for e in events if e.type == "team.run_started"]
        self.assertTrue(all(e.actor == HOST and e.payload["run_id"] == run.payload["run_id"] for e in finished))
        completed, = self.events("team.objective_completed")
        self.assertEqual((completed.actor, completed.payload["run_id"]), (HOST, run.payload["run_id"]))
        self.assertGreater(completed.seq, finished[-2].seq)
        self.assertEqual(completed.payload["artifacts"], selected)
        self.assertEqual(completed.payload["handoff"]["version"], 1)
        self.assertEqual([{k: r[k] for k in ("source_seq", "quote")} for r in completed.payload["handoff"]["evidence"]], reads)
        self.assertEqual([r["source_hash"] for r in completed.payload["handoff"]["evidence"]],
                         [artifact.hash, review.hash, receipt.hash])
        self.assertTrue(CATALOG_KEYS.isdisjoint(completed.payload["handoff"]))
        tool_evidence = completed.payload["handoff"]["evidence"][-1]
        self.assertEqual({k: tool_evidence[k] for k in ("status", "exit_code", "complete", "truncated")},
                         {"status": "error", "exit_code": 2, "complete": True, "truncated": True})
        self.assertEqual(self.rt.team.view(self.task)["status"], "done")
        self.assertEqual(len(self.driver.calls) - fixture_calls, 5)
        self.assertEqual(self.rt.task(self.task).tokens - fixture_tokens, 35)
        self.assertEqual(self.rt._reported_driver_tokens("team_test") - fixture_driver_tokens, 35)
        self.assertEqual(self.driver.steps[self.manager], [])
        self.assertFalse([e for e in events if e.type in {"team.review_requested", "team.review_recorded", "deliverable.published"}])
        self.assertTrue(self.rt.verify_chain().ok)
        self._qa_trace = {"driver": "scripted team_test; NOT a real Qwen/provider replay",
            "driver_turns": len(self.driver.calls) - fixture_calls, "reported_fixture_tokens": 35,
            "setup_driver_turns": fixture_calls,
            "run_id": run.payload["run_id"], "failed_receipt": observed[0],
            "read_tool_calls": [{"name": e.payload["name"], "seq": e.seq} for e in finished[1:-1]],
            "corrected_evidence": reads, "completion_seq": completed.seq,
            "events": [{"seq": e.seq, "type": e.type} for e in events]}


if __name__ == "__main__":
    unittest.main()
