"""Selectable handoff evidence stays bound to actual sources and delivery scope."""

import copy
import json
import unittest

from cater.events import Actor
from cater.mcp_host import McpToolBridge
from cater.team_delivery import HandoffError, HOST
import test_handoff_diagnostics
import test_simple_team
import test_team_handoff


class HandoffSourceSelectionTests(unittest.TestCase):
    setUp = test_team_handoff.TeamHandoffTests.setUp
    artifact = test_team_handoff.TeamHandoffTests.artifact
    refs = test_team_handoff.TeamHandoffTests.refs
    handoff = test_team_handoff.TeamHandoffTests.handoff
    normalize = test_team_handoff.TeamHandoffTests.normalize
    receipt = test_team_handoff.TeamHandoffTests.receipt
    finish = test_team_handoff.TeamHandoffTests.finish
    events = test_team_handoff.TeamHandoffTests.events
    run_ok = test_team_handoff.TeamHandoffTests.run_ok
    review = test_handoff_diagnostics.HandoffDiagnosticsTests.review

    def read(self, **arguments):
        before = self.rt.log.read()
        calls = copy.deepcopy(self.driver.calls)
        result = self.rt.team.delivery.read_sources(self.task, arguments)
        self.assertEqual(self.rt.log.read(), before)
        self.assertEqual(self.driver.calls, calls)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["revision"], self.rt.team.view(self.task)["revision"])
        self.assertIs(type(result["has_more"]), bool)
        self.assertTrue(result["note"].strip())
        self.assertLessEqual(len(result["sources"]), arguments.get("limit", 6))
        for source in result["sources"]:
            self.assertTrue(1 <= len(source["evidence_options"]) <= 4)
            for option in source["evidence_options"]:
                self.assertRegex(option["evidence_id"], r"\Aev1_[0-9a-f]{64}\Z")
                self.assertTrue(option["quote"])
                self.assertEqual(option["quote"], option["quote"].strip())
                self.assertLessEqual(len(option["quote"].encode("utf-8")), 512)
                self.assertIs(type(option["quote_truncated"]), bool)
        return result

    def option(self, event, **arguments):
        row, = self.read(source_seq=event.seq, **arguments)["sources"]
        self.assertEqual(row["source_seq"], event.seq)
        return row["evidence_options"][0]

    def reject(self, evidence, *, artifacts=None):
        value = self.handoff(evidence)
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

    def test_three_source_kinds_select_to_existing_v1_records_and_persist(self):
        artifact = self.artifact("selected source")
        receipt = self.receipt()
        review = self.review()
        events = [artifact, receipt, review]
        options = [self.option(event) for event in events]
        selected = [{"evidence_id": option["evidence_id"]} for option in options]
        legacy = [{"source_seq": event.seq, "quote": option["quote"]}
                  for event, option in zip(events, options)]
        expected = self.normalize(self.handoff(legacy))
        self.assertEqual(self.normalize(self.handoff(selected)), expected)
        self.assertEqual(expected["version"], 1)
        self.assertEqual([ref["kind"] for ref in expected["evidence"]],
                         ["artifact", "tool_receipt", "peer_review"])
        self.assertEqual([ref["source_hash"] for ref in expected["evidence"]],
                         [event.hash for event in events])
        self.assertEqual(self.finish(self.handoff(selected))["status"], "ok")
        completed, = self.events("team.objective_completed")
        self.assertEqual(completed.payload["handoff"], expected)
        self.assertEqual(self.rt.team.delivery.read(self.task, {})["delivery"]["handoff"], expected)
        self.assertNotIn("evidence_id", json.dumps(expected))

    def test_mixed_legacy_and_selected_refs_deduplicate_and_keep_exact_quote_rule(self):
        source = self.artifact("exact original text")
        option = self.option(source)
        old = {"source_seq": source.seq, "quote": option["quote"]}
        new = {"evidence_id": option["evidence_id"]}
        self.assertEqual(self.normalize(self.handoff([old, new, old])), self.normalize(self.handoff([old])))
        self.reject([{"source_seq": source.seq, "quote": "Exact original text"}])
        for ref in ({**new, "quote": option["quote"]}, {**old, **new},
                    {"evidence_id": True}, {"evidence_id": "ev1_bad"}):
            with self.subTest(ref=ref):
                self.reject([ref])

    def test_ids_are_stable_but_forged_and_foreign_ids_reject_without_body_leaks(self):
        source = self.artifact("CURRENT_BODY_SECRET")
        option = self.option(source)
        self.assertEqual(self.option(source), option)
        other = self.rt.create_task("FOREIGN_TASK_SECRET")
        foreign = self.artifact("FOREIGN_BODY_SECRET", task=other)
        foreign_option = self.rt.team.delivery.read_sources(other, {"artifact_seqs": [foreign.seq]})["sources"][0]["evidence_options"][0]
        identity = option["evidence_id"]
        forged = identity[:-1] + ("0" if identity[-1] != "0" else "1")
        for bad in (forged, foreign_option["evidence_id"]):
            with self.subTest(identity=bad):
                error = self.reject([{"evidence_id": bad}])
                public = json.dumps({"message": str(error), "diagnostics": error.diagnostics,
                                     "catalog": error.source_catalog})
                for secret in ("CURRENT_BODY_SECRET", "FOREIGN_BODY_SECRET", "FOREIGN_TASK_SECRET"):
                    self.assertNotIn(secret, public)
        self.assertEqual(self.read(source_seq=foreign.seq)["sources"], [])

    def test_final_artifact_selection_rejects_unselected_and_newer_source(self):
        old = self.artifact("old bytes")
        old_option = self.option(old)
        latest = self.artifact("new bytes")
        latest_option = self.option(latest)
        refs = {event.seq: {key: event.payload[key] for key in ("filename", "content_sha256")}
                for event in (old, latest)}
        for selected, unavailable in ((latest, old_option), (old, latest_option)):
            chosen = [{"seq": selected.seq, **refs[selected.seq]}]
            with self.subTest(selected=selected.seq):
                self.reject([{"evidence_id": unavailable["evidence_id"]}], artifacts=chosen)
        self.assertEqual(self.read()["artifact_seqs"], [latest.seq])
        self.assertEqual(self.read(source_seq=old.seq)["sources"], [])
        self.assertEqual(self.option(old, artifact_seqs=[old.seq]), old_option)

    def test_objective_change_invalidates_ids_even_for_explicitly_reused_artifact(self):
        source = self.artifact("reusable bytes")
        receipt = self.receipt()
        review = self.review()
        old_options = [self.option(event) for event in (source, receipt, review)]
        self.rt.log.emit(type="team.objective_updated", task_id=self.task, actor=HOST,
                         payload={"goal": "New objective"})
        self.assertEqual(self.read()["sources"], [])
        for option in old_options:
            with self.subTest(identity=option["evidence_id"]):
                self.reject([{"evidence_id": option["evidence_id"]}])
        fresh = self.option(source, artifact_seqs=[source.seq])
        self.assertNotEqual(fresh["evidence_id"], old_options[0]["evidence_id"])
        self.assertEqual(self.normalize(self.handoff([{"evidence_id": fresh["evidence_id"]}]))["evidence"][0]["source_seq"], source.seq)

    def test_outdated_review_id_is_not_current_approval(self):
        self.artifact("old reviewed bytes")
        review = self.review()
        option = self.option(review)
        self.artifact("replacement bytes")
        self.assertEqual(self.read(source_seq=review.seq)["sources"], [])
        self.reject([{"evidence_id": option["evidence_id"]}])

    def test_forged_content_hash_and_actor_do_not_offer_options(self):
        source = self.artifact("HASH_BODY_SECRET")
        forged = self.rt.log.emit(type="deliverable.published", task_id=self.task,
            actor=Actor.agent(self.manager, self.rt.organization.leader.role),
            payload={**source.payload, "content_sha256": "0" * 64})
        self.assertEqual(self.read(source_seq=forged.seq)["sources"], [])
        self.reject([{"source_seq": forged.seq, "quote": "HASH_BODY_SECRET"}])
        untrusted = self.artifact("FORGED_BODY_SECRET", filename="untrusted.py", actor=Actor.system("untrusted"))
        self.assertEqual(self.read(source_seq=untrusted.seq)["sources"], [])

    def test_utf8_prefixes_are_exact_bounded_and_round_trip(self):
        for content, truncated in (("  short text  ", False), ("a" * 512, False),
                                   ("a" * 513, True), ("\u4e2d\U0001f642" * 100, True)):
            with self.subTest(bytes=len(content.encode("utf-8"))):
                source = self.artifact(content)
                option = self.option(source)
                self.assertTrue(content.strip().startswith(option["quote"]))
                self.assertNotIn("\ufffd", option["quote"])
                self.assertEqual(option["quote_truncated"], truncated)
                ref, = self.normalize(self.handoff([{"evidence_id": option["evidence_id"]}]))["evidence"]
                self.assertEqual(ref["quote"], option["quote"])

    def test_receipt_options_keep_capture_failure_metadata_and_four_option_cap(self):
        receipt = self.receipt(result={"status": "error", "complete": False, "exit_code": 2,
            "stdout": {"excerpt": "head marker", "preview_mode": "head_tail", "tail_excerpt": "tail marker",
                       "truncated": True, "utf8_valid": False},
            "stderr": {"excerpt": "error head", "preview_mode": "head_tail", "tail_excerpt": "error tail"},
            "content": "content marker", "error": {"message": "failure marker"}})
        row, = self.read(source_seq=receipt.seq)["sources"]
        self.assertEqual((row["status"], row["exit_code"], row["complete"], row["truncated"]),
                         ("error", 2, False, True))
        texts = {"head marker", "tail marker", "error head", "error tail", "content marker", "failure marker"}
        for option in row["evidence_options"]:
            self.assertIn(option["quote"], texts)
            ref, = self.normalize(self.handoff([{"evidence_id": option["evidence_id"]}]))["evidence"]
            self.assertEqual((ref["status"], ref["exit_code"], ref["complete"], ref["truncated"]),
                             ("error", 2, False, True))
            self.assertEqual(ref["output_utf8_valid"], {"stdout": False})
            self.assertNotIn("approved", ref)

    def test_empty_sources_offer_no_id(self):
        receipt = self.receipt(result={"status": "ok", "stdout": " \n\t", "stderr": ""})
        self.assertEqual(self.read(source_seq=receipt.seq)["sources"], [])

    def test_retired_latest_filename_does_not_resurrect_earlier_draft(self):
        earlier = self.artifact("earlier draft", filename="draft.txt")
        retired = self.artifact("reviewed draft", filename="draft.txt")
        option = self.option(retired)
        first_decision = self.review()
        final = self.artifact("renamed final bytes", filename="final.txt")
        selected = [ref for ref in self.refs() if ref["seq"] == final.seq]
        request = self.rt.log.emit(type="team.review_requested", task_id=self.task, actor=HOST,
            payload={"root_seq": first_decision.payload["review_seq"], "revision": 0,
                     "author": self.manager, "reviewer": self.worker,
                     "instruction": "Review renamed final", "artifacts": selected, "run_id": "renamed-review"})
        self.rt.log.emit(type="team.review_recorded", task_id=self.task, actor=HOST,
            payload={"root_seq": first_decision.payload["review_seq"], "review_seq": request.seq,
                     "reviewer": self.worker, "verdict": "approved", "feedback": "Renamed final inspected"})
        self.assertIn(retired.seq, self.rt.team_reviews.superseded_artifacts(self.task))
        page = self.read()
        self.assertEqual(page["artifact_seqs"], [final.seq])
        self.assertTrue({earlier.seq, retired.seq}.isdisjoint(row["source_seq"] for row in page["sources"]))
        with self.assertRaises(HandoffError):
            self.rt.team.delivery.read_sources(self.task, {"artifact_seqs": [retired.seq]})
        self.reject([{"evidence_id": option["evidence_id"]}], artifacts=selected)

    def test_pagination_visits_every_source_once_in_descending_sequence(self):
        artifacts = [self.artifact(f"body {i}", filename=f"part-{i}.txt") for i in range(7)]
        review = self.review()
        receipt = self.receipt()
        expected = sorted([event.seq for event in [*artifacts, review, receipt]], reverse=True)
        self.assertEqual(len(self.read()["sources"]), 6)
        for limit in (1, 6):
            with self.subTest(limit=limit):
                seen, before = [], None
                for _ in range(len(expected) + 1):
                    page = self.read(**({"before_seq": before} if before is not None else {}), limit=limit)
                    seqs = [row["source_seq"] for row in page["sources"]]
                    seen.extend(seqs)
                    if not page["has_more"]:
                        self.assertIsNone(page["next_before_seq"])
                        break
                    self.assertTrue(seqs)
                    self.assertEqual(page["next_before_seq"], seqs[-1])
                    before = page["next_before_seq"]
                self.assertEqual(seen, expected)
        exact = self.read(source_seq=artifacts[0].seq)
        self.assertFalse(exact["has_more"])
        self.assertIsNone(exact["next_before_seq"])

    def test_invalid_read_arguments_fail_without_events_models_or_secret_echo(self):
        source = self.artifact("SOURCE_BODY_SECRET")
        other = self.rt.create_task("Other")
        foreign = self.artifact("FOREIGN_BODY_SECRET", task=other)
        cases = [{"limit": n} for n in (0, 7, True, 1.0)]
        cases += [{field: n} for field in ("source_seq", "before_seq") for n in (0, -1, True, 1.0)]
        cases += [{"source_seq": source.seq, "before_seq": source.seq + 1},
                  {"artifact_seqs": [source.seq, source.seq]}, {"artifact_seqs": [foreign.seq]},
                  {"artifact_seqs": [True]}, {"unknown": "ARGUMENT_BODY_SECRET"}]
        before = self.rt.log.read()
        for arguments in cases:
            with self.subTest(arguments=arguments), self.assertRaises(HandoffError) as caught:
                self.rt.team.delivery.read_sources(self.task, arguments)
            self.assertNotIn("BODY_SECRET", str(caught.exception))
        self.assertEqual(self.rt.log.read(), before)
        self.assertEqual(self.driver.calls, [])

    def test_native_read_tool_records_receipt_and_selected_id_finishes_without_execution(self):
        source = self.artifact("native fixture bytes")
        chosen = {}

        def select(context, history, tools):
            result = json.loads(history[-1]["content"])
            self.assertEqual(result["status"], "ok")
            chosen.update(result["sources"][0]["evidence_options"][0])
            return test_simple_team.turn("", ("finish_work", {
                "summary": "Fixture delivered", "verification": "Source inspection only",
                "handoff": self.handoff([{"evidence_id": chosen["evidence_id"]}])}))

        self.driver.steps[self.manager] = [test_simple_team.turn("", ("read_handoff_sources", {})),
                                           select, test_simple_team.turn("Fixture delivered")]
        self.rt.team.receive(self.task, "Deliver fixture")
        self.run_ok()
        receipts = [event for event in self.events("team.tool_finished") if event.payload["name"] == "read_handoff_sources"]
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0].actor, HOST)
        self.assertEqual(receipts[0].payload["result"]["status"], "ok")
        self.assertEqual([event.payload["name"] for event in self.events("team.tool_started")],
                         ["read_handoff_sources", "finish_work"])
        completed, = self.events("team.objective_completed")
        ref, = completed.payload["handoff"]["evidence"]
        self.assertEqual((ref["source_seq"], ref["source_hash"], ref["quote"]),
                         (source.seq, source.hash, chosen["quote"]))
        self.assertEqual(self.events("team.review_recorded"), [])

    def test_paused_manager_can_read_without_execution_or_worker_permission(self):
        source = self.artifact("paused source")
        self.rt.execution_control.change(self.task, "pause", request_id="selection-pause")
        self.assertEqual(self.option(source)["quote"], "paused source")

        def read(context, history, tools):
            names = {tool["name"] for tool in tools}
            self.assertIn("read_handoff_sources", names)
            self.assertTrue({"finish_work", "set_objective", "workspace_run", "publish_artifact", "record_review"}.isdisjoint(names))
            return test_simple_team.turn("", ("read_handoff_sources", {}))

        self.driver.steps[self.manager] = [read, test_simple_team.turn("Existing source read")]
        self.rt.team.receive(self.task, "Read existing sources")
        self.run_ok()
        receipt, = [event for event in self.events("team.tool_finished") if event.payload["name"] == "read_handoff_sources"]
        self.assertEqual(receipt.payload["result"]["status"], "ok")
        self.assertEqual(self.rt.execution_control.view(self.task)["state"], "paused")
        self.assertEqual(self.events("team.objective_completed"), [])
        self.assertNotIn("read_handoff_sources", {tool["name"] for tool in self.rt.team._schemas(self.rt.team._role(self.worker), None)})

    def test_worker_cannot_invoke_manager_source_selection_tool(self):
        self.artifact("MANAGER_SOURCE_BODY_SECRET")
        self.driver.steps[self.worker] = [test_simple_team.turn("", ("read_handoff_sources", {})),
                                          test_simple_team.turn("Permission boundary observed")]
        self.driver.steps[self.manager] = [test_simple_team.turn("Worker report received")]
        role = self.rt.team._role(self.worker)
        self.rt.team.receive(self.task, f"@{role.name} Check available tools")
        self.rt.collaborate(self.task)
        run, = self.events("team.run_finished")
        self.assertEqual((run.payload["status"], run.payload["failure_stage"]), ("failed", "tool_dispatch"))
        self.assertEqual(self.events("team.tool_started"), [])
        self.assertEqual(self.events("team.tool_finished"), [])
        self.assertNotIn("MANAGER_SOURCE_BODY_SECRET", json.dumps(run.payload))
        self.assertTrue(self.rt.verify_chain().ok)

    def test_mcp_and_native_share_read_fields_and_legacy_or_selected_handoff_schema(self):
        tools = self.rt.team._schemas(self.rt.organization.leader, None)
        native = {tool["name"]: tool["input_schema"] for tool in tools}
        schema = native["read_handoff_sources"]
        self.assertEqual(set(schema["properties"]), {"artifact_seqs", "source_seq", "before_seq", "limit"})
        self.assertEqual((schema["properties"]["limit"]["minimum"], schema["properties"]["limit"]["maximum"]), (1, 6))
        evidence = native["finish_work"]["properties"]["handoff"]["properties"]["evidence"]["items"]
        self.assertEqual({frozenset(branch["required"]) for branch in evidence["oneOf"]},
                         {frozenset({"source_seq", "quote"}), frozenset({"evidence_id"})})
        bridge = McpToolBridge(tools, lambda call: self.fail("Schema discovery executed a tool"))
        self.addCleanup(bridge.close)
        bridge._dispatch({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                          "params": {"protocolVersion": "2025-06-18"}})
        bridge._dispatch({"jsonrpc": "2.0", "method": "notifications/initialized"})
        result = bridge._dispatch({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        exposed = {tool["name"]: tool["inputSchema"] for tool in result["result"]["tools"]}
        for name in ("read_handoff_sources", "finish_work"):
            self.assertEqual(exposed[name], native[name])


if __name__ == "__main__":
    unittest.main()
