"""Offline coverage for per-round, evidence-bound stage revisions."""

import copy
import io
import json
import os
import socket
import subprocess
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

from cater.anthropic import AnthropicMessagesDriver
from cater.runtime import Runtime
from test_work import WorkDriver, item


class StageRevisionBudgetTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {"CATER_STAGE_REVISION_TEST": "offline"}))
        self.enterContext(patch.object(socket, "socket", side_effect=AssertionError("network forbidden")))
        self.enterContext(patch.object(subprocess, "Popen", side_effect=AssertionError("process forbidden")))
        roles = json.loads((Path(__file__).resolve().parents[1] / "cater/packs/default_roles.json").read_text(encoding="utf-8"))
        roles["organization"]["leader"]["driver"] = "final"
        roles_path = self.root / "roles.json"
        roles_path.write_text(json.dumps(roles), encoding="utf-8")
        self.rt = Runtime(self.root / "stage.db", roles_path=roles_path, drivers={
            name: AnthropicMessagesDriver(name, endpoint="https://fixture.invalid/messages", model="fixture",
                                          api_key_env="CATER_STAGE_REVISION_TEST")
            for name in ("product", "design", "dev", "test", "final")})
        self.addCleanup(self.rt.close)
        self.task = self.rt.create_task("Deliver corrected source and its dependent note")
        self.rt.plan_work(self.task, [item("source"), item("downstream", owner="design", deps=["source"])])
        self.calls = []
        self.productions = Counter()
        self.body = lambda name, n: f"print({n})\n"
        self.failed_ids = lambda name, n: ["meaning"] if name == "source" and n < 3 else []
        self.review_mode = None
        self.failure = None
        self.hook = None
        self.delivery_failed = False
        self.delivery_count = 0
        self.document = False
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
                return {"id": identifier, "status": "failed" if self.delivery_failed else "passed",
                        "reason": "The conclusion is missing" if self.delivery_failed else "Inspected the body",
                        "evidence": [{"artifact_seq": artifact["seq"], "quote": artifact["content"]}],
                        "superseded_by": None, "source_quote": None}

            raw = {"requirements": [row(s["id"]) for s in context["manifest"]["sources"]],
                   "checks": [row(c["id"]) for c in context["manifest"]["criteria"] if c["kind"] == "review"],
                   "handoff": {"summary": "Checked", "why": "Read the source",
                               "reproduce": ["Read the attached artifact"], "limits": ["Offline fixture"]}}
        else:
            name = context["work_item_id"]
            reviewing = context.get("phase") == "review"
            if not reviewing:
                self.productions[name] += 1
            n = self.productions[name]
            if self.failure and self.failure[0] == ("review" if reviewing else "work") and n == self.failure[1]:
                if self.failure[2] == "transport":
                    raise URLError("offline injected transport failure")
                raise RuntimeError("unknown invocation outcome")
            if reviewing:
                artifact = context["artifacts"][0]
                failed = self.failed_ids(name, n)
                raw = {"checks": [{"id": c["id"], "status": "failed" if c["id"] in failed else "passed",
                                   "reason": f"Required correction for {c['id']} at revision {n}",
                                   "artifact_seq": artifact["seq"], "quote": artifact["content"],
                                   "receipt_seq": None} for c in context["contract"]["criteria"]]}
                for check in raw["checks"]:
                    if self.review_mode == "unknown":
                        check.update(status="unknown", artifact_seq=None, quote="")
                    elif self.review_mode == "missing_evidence":
                        check.update(artifact_seq=None, quote="")
                    elif self.review_mode == "bad_quote":
                        check["quote"] = "absent from the actual source"
            else:
                raw = {"stance": "assert", "claim": "Produced source", "text": "Produced source",
                       "deliverables": [{"title": f"Source revision {n}", "summary": f"Attempt {n}",
                                         "filename": f"{name}.md" if self.document else f"{name}.py",
                                         "format": "markdown" if self.document else "code",
                                         "content": f"# Note {name}\n\nRevision {n}." if self.document else self.body(name, n)}]}
        if self.hook:
            self.hook(context)
        return io.BytesIO(json.dumps({"type": "message", "stop_reason": "end_turn",
                                     "content": [{"type": "text", "text": json.dumps(raw)}]}).encode())

    def run_round(self):
        return self.rt.collaborate(self.task, expected_scope=self.rt.work.scope(self.task))

    def events(self, kind):
        return list(self.rt.log.iter_events(task_id=self.task, event_types=(kind,)))

    def repairs(self):
        return [e for e in self.events("work.repair_requested") if e.payload.get("phase") != "delivery"]

    def assert_blocked(self):
        view = self.rt.work.view(self.task)
        self.assertFalse(view["stage_accepted"])
        self.assertEqual(self.productions["downstream"], 0)
        self.assertEqual(self.delivery_count, 0)
        self.assertNotEqual(self.rt.task(self.task).status, "done")

    def test_two_substantive_revisions_pass_and_release_downstream_with_current_evidence(self):
        result = self.run_round()
        self.assertTrue(result["brief"]["work"]["stage_accepted"])
        self.assertEqual(self.productions, {"source": 3, "downstream": 1})
        repairs = self.repairs()
        self.assertEqual([e.payload["revision_attempt"] for e in repairs], [1, 2])
        reviews = {e.seq: e for e in self.events("work.reviewed")}
        for attempt, event in enumerate(repairs, 1):
            payload = event.payload
            self.assertEqual(payload["revision_limit"], 3)
            self.assertEqual(payload["failed_criterion_ids"], ["meaning"])
            self.assertEqual(payload["scope"], self.rt.work.scope(self.task))
            review = reviews[payload["review_seq"]]
            self.assertEqual(payload["submission_seq"], review.payload["submission_seq"])
            check = review.payload["checks"][0]
            self.assertIn(check["reason"], payload["reason"])
            work = [c for c in self.calls if c.get("phase") != "review" and c.get("work_item_id") == "source"][attempt]
            previous = work["previous_attempt"]
            self.assertEqual(previous["artifacts"][0]["content"], f"print({attempt})\n")
            self.assertEqual(payload["content_hashes"], [previous["artifacts"][0]["content_sha256"]])
            self.assertEqual(previous["review_checks"], review.payload["checks"])
        downstream = next(c for c in self.calls if c.get("work_item_id") == "downstream" and c.get("phase") != "review")
        self.assertEqual(downstream["accepted_inputs"]["source"][0]["content"], "print(3)\n")
        claims = self.events("work.claimed")
        passed = next(e for e in reviews.values() if e.payload["work_item_id"] == "source" and e.payload["status"] == "accepted")
        self.assertGreater(claims[-1].seq, passed.seq)
        self.assertTrue(self.rt.verify_chain().ok)

    def test_same_content_and_failed_ids_stop_despite_new_artifact_and_review_metadata(self):
        self.body = lambda name, n: "print('unchanged')\n"
        self.failed_ids = lambda name, n: ["meaning"]
        self.run_round()
        self.assertEqual(self.productions, {"source": 2})
        self.assertEqual(len(self.repairs()), 1)
        artifacts = self.events("deliverable.published")
        self.assertNotEqual(artifacts[0].seq, artifacts[1].seq)
        self.assertNotEqual(artifacts[0].payload["title"], artifacts[1].payload["title"])
        self.assertEqual(artifacts[0].payload["content_sha256"], artifacts[1].payload["content_sha256"])
        reviews = self.events("work.reviewed")
        self.assertNotEqual(reviews[0].payload["checks"][0]["reason"], reviews[1].payload["checks"][0]["reason"])
        self.assert_blocked()

    def test_return_to_an_earlier_content_and_failure_pair_also_stops(self):
        self.body = lambda name, n: f"print({n % 2})\n"
        self.failed_ids = lambda name, n: ["meaning"]
        self.run_round()
        self.assertEqual(self.productions, {"source": 3})
        self.assertEqual(len(self.repairs()), 2)
        self.assert_blocked()

    def test_same_content_with_different_failed_criteria_can_be_revised(self):
        source = item("source")
        source["criteria"].append({"id": "detail", "kind": "review", "text": "Contains the required detail"})
        self.rt.plan_work(self.task, [source, item("downstream", owner="design", deps=["source"])])
        self.body = lambda name, n: "print('fixed source')\n"
        self.failed_ids = lambda name, n: (["meaning"] if n == 1 else ["detail"]) if name == "source" and n < 3 else []
        self.run_round()
        self.assertEqual(self.productions, {"source": 3, "downstream": 1})
        self.assertEqual([e.payload["failed_criterion_ids"] for e in self.repairs()], [["meaning"], ["detail"]])

    def test_changing_content_that_always_fails_has_three_revisions_per_round(self):
        self.failed_ids = lambda name, n: ["meaning"]
        self.run_round()
        self.assertEqual(self.productions, {"source": 4})
        self.assertEqual([e.payload["revision_attempt"] for e in self.repairs()], [1, 2, 3])
        self.assertEqual(len(self.events("work.reviewed")), 4)
        self.assert_blocked()
        self.run_round()
        self.assertEqual(self.productions, {"source": 8})
        self.assertEqual([e.payload["revision_attempt"] for e in self.repairs()], [1, 2, 3, 1, 2, 3])
        self.assert_blocked()

    def test_independent_items_each_get_three_revisions(self):
        self.rt.plan_work(self.task, [item("source"), item("other", owner="design")])
        self.failed_ids = lambda name, n: ["meaning"]
        self.run_round()
        self.assertEqual(self.productions, {"source": 4, "other": 4})
        for name in self.productions:
            self.assertEqual([e.payload["revision_attempt"] for e in self.repairs() if e.payload["work_item_id"] == name], [1, 2, 3])

    def test_unknown_or_invalid_review_evidence_does_not_authorize_source_revision(self):
        for mode in ("unknown", "missing_evidence", "bad_quote"):
            with self.subTest(mode=mode):
                self.review_mode = mode
                self.task = self.rt.create_task("Keep uncertain evidence distinct from a source defect")
                self.rt.plan_work(self.task, [item("source"), item("downstream", owner="design", deps=["source"])])
                self.productions.clear()
                self.run_round()
                self.assertEqual(self.productions, {"source": 1})
                self.assertEqual(self.repairs(), [])
                self.assertTrue(all(c["status"] == "unknown" for c in self.rt.work.view(self.task)["items"][0]["checks"]))
                self.assert_blocked()

    def assert_incomplete_invocation_stops(self, phase, n, failure):
        self.failure = (phase, n, failure)
        self.run_round()
        self.assertEqual(self.productions, {"source": n})
        self.assertEqual(len(self.repairs()), n - 1)
        if phase == "work":
            self.assertEqual(self.events("work.failed")[-1].payload["uncertain"], failure == "uncertain")
        self.assert_blocked()

    def test_initial_work_transport_failure_is_not_retried(self):
        self.assert_incomplete_invocation_stops("work", 1, "transport")

    def test_initial_work_unknown_side_effects_are_not_retried(self):
        self.assert_incomplete_invocation_stops("work", 1, "uncertain")

    def test_revision_transport_failure_is_not_retried(self):
        self.assert_incomplete_invocation_stops("work", 2, "transport")

    def test_revision_unknown_side_effects_are_not_retried(self):
        self.assert_incomplete_invocation_stops("work", 2, "uncertain")

    def test_initial_review_transport_failure_is_not_a_source_defect(self):
        self.assert_incomplete_invocation_stops("review", 1, "transport")

    def test_initial_review_unknown_side_effects_are_not_a_source_defect(self):
        self.assert_incomplete_invocation_stops("review", 1, "uncertain")

    def test_revision_review_transport_failure_stops_further_revisions(self):
        self.assert_incomplete_invocation_stops("review", 2, "transport")

    def test_revision_review_unknown_side_effects_stop_further_revisions(self):
        self.assert_incomplete_invocation_stops("review", 2, "uncertain")

    def test_unknown_adapter_and_replaced_builtin_method_do_not_acquire_text_permission(self):
        driver = WorkDriver()
        driver.status = "failed"
        with patch.dict(self.rt.drivers, {"dev": driver}):
            self.run_round()
        self.assertEqual(self.repairs(), [])
        self.assertEqual(len([c for c in driver.calls if c.get("phase") != "review"]), 1)
        self.assert_blocked()
        self.task = self.rt.create_task("Check the original builtin method identity")
        self.rt.plan_work(self.task, [item("source")])
        worker = self.rt.drivers["dev"]
        with patch.object(worker, "complete", wraps=worker.complete):
            self.run_round()
        self.assertEqual(self.productions, {"source": 1})
        self.assertEqual(self.repairs(), [])

    def test_scope_change_during_review_cannot_authorize_revision(self):
        self.hook = lambda c: self.rt.command(self.task, "Change the required output") if c.get("phase") == "review" else None
        self.run_round()
        self.assertFalse(self.rt.work.view(self.task)["scope_current"])
        self.assertEqual(self.productions, {"source": 1})
        self.assertEqual(self.repairs(), [])
        self.assert_blocked()

    def test_stage_revisions_keep_delivery_rework_exclusion(self):
        self.document = True
        self.delivery_failed = True
        self.run_round()
        self.assertEqual(self.productions, {"source": 3, "downstream": 1})
        self.assertEqual(self.delivery_count, 1)
        self.assertEqual(len(self.events("work.repair_requested")), 2)
        self.assertTrue(all(e.payload.get("phase") != "delivery" for e in self.events("work.repair_requested")))

    def test_delivery_revision_failure_does_not_gain_three_more_stage_attempts(self):
        self.document = True
        self.delivery_failed = True
        self.failed_ids = lambda name, n: ["meaning"] if self.delivery_count else []
        self.run_round()
        self.assertEqual(self.productions, {"source": 2, "downstream": 1})
        self.assertEqual(self.delivery_count, 1)
        self.assertEqual(self.repairs(), [])
        self.assertEqual(len(self.events("work.repair_requested")), 1)
        self.assertEqual(self.events("work.repair_requested")[0].payload["phase"], "delivery")
        self.assertFalse(self.rt.work.view(self.task)["stage_accepted"])


if __name__ == "__main__":
    unittest.main()
