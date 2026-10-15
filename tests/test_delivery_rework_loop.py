"""Offline integration coverage for bounded, evidence-bound delivery revisions."""

import copy
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

from cater.anthropic import AnthropicMessagesDriver
from cater.events import Actor
from cater.runtime import Runtime
from test_delivery import DocumentWorkDriver
from test_work import item


class DeliveryReworkLoopTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1] / ".cater"
        root.mkdir(exist_ok=True)
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(dir=root, prefix="f09-")))
        self.enterContext(patch.dict(os.environ, {"CATER_F09_TEST": "offline-fixture"}))
        self.enterContext(patch.object(socket, "socket", side_effect=AssertionError("network forbidden")))
        self.real_popen = subprocess.Popen
        self.enterContext(patch.object(subprocess, "Popen", side_effect=AssertionError("process forbidden")))
        config = json.loads(Path("cater/packs/default_roles.json").read_text(encoding="utf-8"))
        config["organization"]["leader"]["driver"] = "final"
        roles = self.root / "roles.json"
        roles.write_text(json.dumps(config), encoding="utf-8")
        self.drivers = {name: AnthropicMessagesDriver(
            name, endpoint="https://fixture.invalid/messages", model="fixture", api_key_env="CATER_F09_TEST")
            for name in ("product", "design", "dev", "test", "final")}
        self.rt = Runtime(self.root / "test.db", roles_path=roles, drivers=self.drivers)
        self.addCleanup(self.rt.close)
        self.task = self.rt.create_task("Deliver a consistent note")
        self.plan = [item("a"), item("b", owner="design", deps=["a"]), item("c", owner="product", deps=["b"]),
                     item("untouched", owner="product")]
        self.rt.plan_work(self.task, self.plan)
        self.calls = []
        self.productions = Counter()
        self.delivery_count = 0
        self.always_fail = False
        self.delivery_mode = None
        self.stage_failure = None
        self.revision_failure = None
        self.after_delivery = None
        self.after_work = None
        complete = self.rt._complete_with_containment

        def capture(**kwargs):
            self.context = copy.deepcopy(kwargs["work_context"])
            self.calls.append(self.context)
            return complete(**kwargs)

        self.enterContext(patch.object(self.rt, "_complete_with_containment", side_effect=capture))
        self.enterContext(patch("cater.anthropic._open_api_request", side_effect=self.respond))

    def respond(self, request, **kwargs):
        context = self.context
        if "manifest" in context:
            self.delivery_count += 1
            artifact = context["artifacts"][0]

            def row(identifier):
                return {"id": identifier, "status": "passed", "reason": "Inspected the attached body",
                        "evidence": [{"artifact_seq": artifact["seq"], "quote": artifact["content"]}],
                        "superseded_by": None, "source_quote": None}

            raw = {"requirements": [row(s["id"]) for s in context["manifest"]["sources"]],
                   "checks": [row(c["id"]) for c in context["manifest"]["criteria"] if c["kind"] == "review"],
                   "handoff": {"summary": "Reviewed note", "why": "Compared the actual bodies",
                               "reproduce": ["Read the attached note"], "limits": ["Document only"]}}
            failed = raw["requirements"][0]
            if self.delivery_count == 1 or self.always_fail:
                failed.update(status="failed", reason="The note omits the agreed conclusion")
            if self.delivery_mode == "unknown":
                failed.update(status="unknown", evidence=[])
            elif self.delivery_mode == "missing_evidence":
                failed["evidence"] = []
            elif self.delivery_mode == "bad_quote":
                failed["evidence"][0]["quote"] = "This text does not exist"
            elif self.delivery_mode == "mixed_unknown":
                raw["checks"][0].update(status="unknown", evidence=[])
            elif self.delivery_mode == "multiple_roots":
                other = next(a for a in context["artifacts"] if a["work_item_id"] == "b")
                failed["evidence"].append({"artifact_seq": other["seq"], "quote": other["content"]})
            if self.after_delivery:
                self.after_delivery(context)
            if self.delivery_mode == "invalid":
                raw = "not JSON"
        elif context.get("phase") == "review":
            artifact = context["artifacts"][0]
            status = "passed"
            if self.stage_failure:
                status = self.stage_failure(context)
            raw = {"checks": [{"id": c["id"], "status": status, "reason": "Inspected the exact body",
                               "artifact_seq": artifact["seq"], "quote": artifact["content"],
                               "receipt_seq": context["local_checks"].get(c["id"], {}).get("seq")}
                              for c in context["contract"]["criteria"]]}
        else:
            name = context["work_item_id"]
            self.productions[name] += 1
            if "delivery_rework" in context and self.revision_failure == "transport":
                raise URLError("offline injected failure")
            if "delivery_rework" in context and self.revision_failure == "uncertain":
                raise RuntimeError("unknown invocation outcome")
            raw = {"stance": "assert", "claim": "Note produced", "text": "Note produced",
                   "deliverables": [{"title": name, "summary": "A checked note", "filename": "note.md",
                                     "format": "markdown", "content": f"# Note {name}\n\nRevision {self.productions[name]}."}]}
            if "delivery_rework" in context and self.revision_failure == "invalid":
                raw = "not JSON"
            if "delivery_rework" in context and self.revision_failure == "empty":
                raw["deliverables"] = []
            if self.after_work:
                self.after_work(context)
        text = raw if isinstance(raw, str) else json.dumps(raw)
        return io.BytesIO(json.dumps({"type": "message", "content": [{"type": "text", "text": text}],
                                     "stop_reason": "end_turn"}).encode())

    def events(self, kind):
        return list(self.rt.log.iter_events(task_id=self.task, event_types=(kind,)))

    def delivery_repairs(self):
        return [e for e in self.events("work.repair_requested") if e.payload.get("phase") == "delivery"]

    def run_round(self):
        return self.rt.collaborate(self.task, expected_scope=self.rt.work.scope(self.task))

    def test_rework_repairs_dependency_closure_then_independently_accepts_new_bundle(self):
        result = self.run_round()
        self.assertEqual(result["brief"]["work"]["delivery"]["status"], "accepted")
        self.assertEqual(self.productions, {"a": 2, "b": 2, "c": 2, "untouched": 1})
        self.assertEqual(self.delivery_count, 2)
        self.assertEqual(len(self.delivery_repairs()), 1)
        self.assertEqual([e.payload["status"] for e in self.events("delivery.review_finished")], ["rework", "accepted"])
        repaired = [c for c in self.calls if "delivery_rework" in c]
        self.assertEqual([c["work_item_id"] for c in repaired], ["a", "b", "c"])
        for context in repaired:
            self.assertIn("Revision 1", context["previous_attempt"]["artifacts"][0]["content"])
            self.assertEqual(context["delivery_rework"]["requirements"][0]["reason"],
                             "The note omits the agreed conclusion")
        self.assertIn("Revision 2", repaired[1]["accepted_inputs"]["a"][0]["content"])
        self.assertIn("Revision 2", repaired[2]["accepted_inputs"]["b"][0]["content"])
        reviews = self.events("work.reviewed")
        submissions = self.events("work.submitted")
        self.assertEqual(len(reviews), len(submissions))
        self.assertEqual({e.payload["submission_seq"] for e in reviews}, {e.seq for e in submissions})
        for review in reviews:
            self.assertNotEqual(review.actor.id, next(i["owner"] for i in self.plan if i["id"] == review.payload["work_item_id"]))
        prepared = self.events("delivery.prepared")
        self.assertNotEqual(prepared[0].payload["digest"], prepared[1].payload["digest"])
        self.assertTrue(self.rt.verify_chain().ok)

    def test_overlapping_affected_roots_do_not_duplicate_dependency_execution(self):
        self.delivery_mode = "multiple_roots"
        self.run_round()
        self.assertEqual(self.productions, {"a": 2, "b": 2, "c": 2, "untouched": 1})
        self.assertEqual(self.delivery_repairs()[0].payload["roots"], ["a", "b"])

    def test_repeated_delivery_failure_exhausts_round_without_recursion_or_false_pass(self):
        self.always_fail = True
        self.run_round()
        self.assertEqual(self.delivery_count, 2)
        self.assertEqual(self.productions, {"a": 2, "b": 2, "c": 2, "untouched": 1})
        self.assertEqual(len(self.delivery_repairs()), 1)
        self.assertEqual([e.payload["status"] for e in self.events("delivery.review_finished")], ["rework", "rework"])
        self.assertNotEqual(self.rt.task(self.task).status, "done")

    def test_stage_and_delivery_share_each_items_single_revision_budget(self):
        self.stage_failure = lambda c: "failed" if c["work_item_id"] == "a" and self.productions["a"] == 1 else "passed"
        self.run_round()
        self.assertEqual(self.productions, {"a": 2, "b": 1, "c": 1, "untouched": 1})
        self.assertEqual(self.delivery_count, 1)
        self.assertEqual(len(self.events("work.repair_requested")), 1)
        self.assertEqual(self.delivery_repairs(), [])

    def test_failed_stage_review_after_delivery_revision_cannot_trigger_third_production(self):
        self.stage_failure = lambda c: "failed" if self.productions["a"] == 2 else "passed"
        self.run_round()
        self.assertEqual(self.productions, {"a": 2, "b": 1, "c": 1, "untouched": 1})
        self.assertEqual(self.delivery_count, 1)
        self.assertEqual(len(self.events("work.repair_requested")), 1)
        self.assertFalse(self.rt.work.view(self.task)["stage_accepted"])

    def test_missing_unknown_or_invalid_evidence_never_authorizes_repair(self):
        for mode in ("unknown", "missing_evidence", "bad_quote", "invalid", "mixed_unknown"):
            with self.subTest(mode=mode):
                self.delivery_mode = mode
                self.delivery_count = 0
                self.task = self.rt.create_task("Deliver a consistent note")
                self.rt.plan_work(self.task, [item("a")])
                self.run_round()
                self.assertEqual(self.delivery_repairs(), [])
                self.assertEqual(len(self.events("work.claimed")), 1)
                self.assertNotEqual(self.rt.task(self.task).status, "done")

    def test_unknown_adapters_cannot_enter_automatic_rework(self):
        for name in ("dev", "design", "test", "final"):
            with self.subTest(driver=name):
                original = self.rt.drivers[name]
                if name == "final":
                    from test_delivery import DeliveryDriver
                    unknown = DeliveryDriver()
                    unknown.transform = lambda raw: {**raw, "requirements": [
                        {**r, "status": "failed"} for r in raw["requirements"]]}
                else:
                    unknown = DocumentWorkDriver()
                self.rt.drivers[name] = unknown
                self.task = self.rt.create_task("Deliver a consistent note")
                self.rt.plan_work(self.task, self.plan)
                self.delivery_count = 0
                self.run_round()
                self.assertEqual(self.delivery_repairs(), [])
                self.assertEqual(len(self.events("work.claimed")), 4)
                self.rt.drivers[name] = original

    def test_replaced_builtin_method_does_not_inherit_text_only_permission(self):
        worker = self.rt.drivers["dev"]
        with patch.object(worker, "complete", wraps=worker.complete):
            self.run_round()
        self.assertEqual(self.delivery_repairs(), [])
        self.assertEqual(len(self.events("work.claimed")), 4)

    def test_pause_and_cancel_during_delivery_prevent_new_work(self):
        for action in ("pause", "cancel"):
            with self.subTest(action=action):
                self.task = self.rt.create_task("Deliver a consistent note")
                self.rt.plan_work(self.task, [item("a")])
                self.delivery_count = 0
                self.after_delivery = lambda c: self.rt.execution_control.change(self.task, action, request_id="f09-" + action)
                self.run_round()
                self.assertEqual(len(self.events("work.claimed")), 1)
                self.assertEqual(self.delivery_repairs(), [])

    def test_direction_or_plan_change_during_delivery_cannot_reuse_old_authorization(self):
        for change in ("direction", "plan"):
            with self.subTest(change=change):
                self.task = self.rt.create_task("Deliver a consistent note")
                self.rt.plan_work(self.task, [item("a")])
                self.delivery_count = 0
                self.after_delivery = (lambda c: self.rt.command(self.task, "Change the required conclusion")) if change == "direction" else (
                    lambda c: self.rt.plan_work(self.task, [item("a")]))
                self.run_round()
                self.assertEqual(len(self.events("work.claimed")), 1)
                self.assertEqual(self.delivery_repairs(), [])
                self.assertNotEqual(self.rt.task(self.task).status, "done")

    def test_revision_failure_is_not_retried_and_does_not_release_dependents(self):
        for failure in ("transport", "uncertain", "invalid", "empty"):
            with self.subTest(failure=failure):
                self.task = self.rt.create_task("Deliver a consistent note")
                self.rt.plan_work(self.task, self.plan)
                self.delivery_count = 0
                self.revision_failure = failure
                self.run_round()
                self.assertEqual(len(self.events("work.claimed")), 5)
                self.assertEqual(self.delivery_count, 1)
                self.assertFalse(self.rt.work.view(self.task)["stage_accepted"])
                self.assertEqual(self.events("work.failed")[-1].payload["uncertain"], failure == "uncertain")

    def test_pause_after_first_revision_does_not_run_review_or_dependents(self):
        self.after_work = lambda c: self.rt.execution_control.change(self.task, "pause", request_id="f09-mid-repair") if "delivery_rework" in c else None
        self.run_round()
        self.assertEqual(len(self.events("work.claimed")), 5)
        self.assertEqual(len(self.events("work.reviewed")), 4)
        self.assertEqual(self.delivery_count, 1)

    def test_concurrent_inflight_claim_after_invalidation_blocks_rework_wave(self):
        run = self.rt.delivery.run

        def concurrent(task):
            result = run(task)
            self.rt.work.claim(task, "a")
            return result

        with patch.object(self.rt.delivery, "run", side_effect=concurrent):
            self.run_round()
        self.assertEqual(self.delivery_repairs(), [])
        self.assertEqual(self.productions, {"a": 1, "b": 1, "c": 1, "untouched": 1})

    def test_rework_claim_cannot_adopt_replacement_plan_even_with_same_scope(self):
        claim = self.rt.work.claim

        def replace(task_id, item_id):
            if self.delivery_repairs():
                self.rt.plan_work(task_id, [item("a", owner="product")])
            return claim(task_id, item_id)

        with patch.object(self.rt.work, "claim", side_effect=replace):
            self.run_round()
        self.assertEqual(self.productions, {"a": 1, "b": 1, "c": 1, "untouched": 1})
        self.assertEqual(self.events("work.failed")[-1].payload["uncertain"], False)
        self.assertFalse(self.rt.work.view(self.task)["stage_accepted"])

    def test_scope_change_during_repair_authorization_is_rechecked_atomically(self):
        emit = self.rt.log.emit
        changed = False

        def change(**kwargs):
            nonlocal changed
            if kwargs["type"] == "work.repair_requested" and kwargs["payload"].get("phase") == "delivery" and not changed:
                changed = True
                self.rt.command(self.task, "Change the required conclusion")
            return emit(**kwargs)

        with patch.object(self.rt.log, "emit", side_effect=change):
            self.run_round()
        self.assertTrue(changed)
        self.assertEqual(self.delivery_repairs(), [])
        self.assertEqual(len(self.events("work.claimed")), 4)

    def test_actual_check_failure_or_missing_new_receipt_cannot_be_overridden_by_model(self):
        for failure in ("stage", "whole", "missing_new_receipt"):
            with self.subTest(failure=failure):
                self.task = self.rt.create_task("Deliver a consistent note")
                self.rt.plan_work(self.task, [item("a", local=True), item("b", deps=["a"])])
                self.delivery_count = 0
                self.productions.clear()

                def stage(task_id, candidate):
                    if candidate["id"] != "a":
                        return
                    revised = self.productions["a"] == 2
                    if revised and failure == "missing_new_receipt":
                        return
                    self.rt.verify_work(task_id, "a", "behavior", [sys.executable, "-c",
                        "raise SystemExit(1)" if revised and failure == "stage" else "raise SystemExit(0)"])

                def whole(task_id, view):
                    self.rt.delivery.verify(task_id, "integration_behavior", [sys.executable, "-c",
                        "raise SystemExit(1)" if failure == "whole" else "raise SystemExit(0)"])

                with patch.object(subprocess, "Popen", self.real_popen), patch.object(
                        self.rt.browser_checks, "run", side_effect=stage), patch.object(
                        self.rt.browser_checks, "run_delivery", side_effect=whole):
                    self.run_round()
                self.assertNotEqual(self.rt.task(self.task).status, "done")
                self.assertEqual(self.productions, {"a": 1 if failure == "whole" else 2, "b": 1})
                self.assertEqual(self.delivery_count, 0 if failure == "whole" else 1)
                receipts = self.events("verification.completed")
                if failure != "missing_new_receipt":
                    self.assertEqual(receipts[-1].payload["exit_code"], 1)
                    self.assertEqual(receipts[-1].payload["outcome"], "failed")
                else:
                    current = self.rt.work.view(self.task)["items"][0]
                    self.assertEqual(self.rt.work.checks(self.task, current), {})


if __name__ == "__main__":
    unittest.main()
