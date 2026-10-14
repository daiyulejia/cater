from __future__ import annotations

import copy
import hashlib
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from cater.artifacts import normalize_deliverables
from cater.events import Actor
from cater.planning import Planner
from cater.profiles import agent_profiles
from cater.runtime import Runtime
from cater.work import WorkConflict
from test_planning import PlanningDriver
from test_runtime import start_test_web
from test_work import WorkDriver, item
from test_delivery import DeliveryDriver, DocumentWorkDriver


class RecordingDriver:
    def __init__(self, delegate):
        self.delegate = delegate
        self.calls = []

    def complete(self, *, role, prompt, context):
        self.calls.append(copy.deepcopy({"role": role, "prompt": prompt, "context": context}))
        return self.delegate.complete(role=role, prompt=prompt, context=context)


class ConversationSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "conversations.db"
        self.worker = WorkDriver()
        self.recorder = RecordingDriver(self.worker)
        self.drivers = {name: self.recorder for name in ("product", "design", "dev", "test")}
        self.rt = Runtime(self.db, drivers=self.drivers)
        self.addCleanup(self.rt.close)
        self.project = self.rt.rulebook.create_project("Shared project")["id"]
        self.source = self.rt.create_task("SOURCE_GOAL", ["SOURCE_ACCEPTANCE"], project_id=self.project)

    def derive(self, *, source=None, goal="CHILD_GOAL", acceptance=None, request_id="derive-request-001"):
        return self.rt.derive_task(source or self.source, goal, acceptance, request_id=request_id)["task_id"]

    def publish(self, task_id, content, **binding):
        artifact = normalize_deliverables([{"title": content.splitlines()[0], "summary": "Reference result",
                                           "content": content, "format": "text"}])[0]
        return self.rt.log.emit(type="deliverable.published", actor=Actor.agent("dev", "worker"),
                                task_id=task_id, payload={**artifact, **binding})

    def say(self, task_id, text, *, actor="dev"):
        return self.rt.log.emit(type="msg.said", actor=Actor.agent(actor, "worker"), task_id=task_id,
                                payload={"text": text, "claim": text, "stance": "assert"})

    def test_creation_captures_now_and_resets_state_plan_and_acceptance(self):
        rule = self.rt.rulebook.record("SOURCE_TASK_RULE", scope="task", task_id=self.source)
        self.rt.plan_work(self.source, [item("source-implementation", local=True)])
        self.rt.collaborate(self.source)
        self.rt.verify_work(self.source, "source-implementation", "behavior", [sys.executable, "check.py"])
        self.rt.collaborate(self.source)
        self.assertTrue(self.rt.work.view(self.source)["stage_accepted"])
        self.assertTrue(self.rt.task(self.source).verifications)
        head = self.rt.log.read(task_id=self.source)[-1]
        before = datetime.now(timezone.utc)
        child = self.derive()
        after = datetime.now(timezone.utc)

        snapshot = self.rt.conversation_background(child)
        self.assertIsNone(self.rt.conversation_background(self.source))
        self.assertEqual(snapshot["source_task_id"], self.source)
        self.assertEqual(snapshot["source_goal"], "SOURCE_GOAL")
        self.assertEqual((snapshot["source_seq"], snapshot["source_hash"]), (head.seq, head.hash))
        captured = datetime.fromisoformat(snapshot["captured_at"])
        self.assertIsNotNone(captured.tzinfo)
        self.assertLessEqual(before, captured)
        self.assertLessEqual(captured, after)
        self.assertEqual(snapshot["omitted_artifacts"], 0)
        self.assertEqual([r["id"] for r in snapshot["task_rules"]], [rule["id"]])
        self.assertEqual(len(snapshot["artifacts"]), 1)

        task = self.rt.task(child)
        self.assertEqual((task.goal, task.acceptance, task.status), ("CHILD_GOAL", [], "running"))
        self.assertEqual(task.project_id, self.project)
        self.assertIsNone(task.plan)
        self.assertIsNone(task.decision)
        self.assertEqual(task.assignments, [])
        self.assertEqual(task.messages, [])
        self.assertEqual(task.deliverables, [])
        self.assertEqual(task.verifications, [])
        self.assertEqual(self.rt.work.check_contract(child)["obligations"], [])
        self.assertEqual(self.rt.work.view(child)["items"], [])
        self.assertFalse(self.rt.work.view(child)["stage_accepted"])
        created = self.rt.log.read(task_id=child)[0]
        self.assertEqual(created.type, "task.created")
        self.assertEqual(created.payload["background"], snapshot)
        self.assertEqual(len(created.payload["inherited_rules"]), 1)
        returned = self.rt.conversation_background(child)
        returned["sources"].clear()
        returned["artifacts"][0]["content"] = "Caller mutation"
        self.assertEqual(self.rt.conversation_background(child), snapshot)
        explicit = self.derive(acceptance=["CHILD_ONLY_CHECK"], request_id="explicit-acceptance")
        self.assertEqual(self.rt.task(explicit).acceptance, ["CHILD_ONLY_CHECK"])
        self.assertTrue(self.rt.verify_chain().ok)

    def test_background_and_rules_survive_interruption_after_creation_event(self):
        self.rt.rulebook.record("ATOMIC_RULE", scope="task", task_id=self.source)
        self.publish(self.source, "ATOMIC_BODY")
        emit = self.rt.log.emit

        def interrupted(**kwargs):
            if kwargs["type"] == "task.started":
                raise RuntimeError("Simulated interruption after task.created")
            return emit(**kwargs)

        with patch.object(self.rt.log, "emit", side_effect=interrupted):
            with self.assertRaisesRegex(RuntimeError, "Simulated interruption"):
                self.derive(request_id="atomic-request-001")
        creations = [e for e in self.rt.log.iter_events(event_types=("task.created",))
                     if e.payload.get("creation_request_id") == "atomic-request-001"]
        self.assertEqual(len(creations), 1)
        child = creations[0].task_id
        self.assertEqual([e.type for e in self.rt.log.read(task_id=child)], ["task.created"])
        self.rt.close()
        with Runtime(self.db) as reopened:
            snapshot = reopened.conversation_background(child)
            self.assertEqual(snapshot["artifacts"][0]["content"], "ATOMIC_BODY")
            rules = reopened.rulebook.view(task_id=child)["rules"]
            self.assertEqual([r["text"] for r in rules], ["ATOMIC_RULE"])
            self.assertEqual(rules[0]["target_id"], child)
            count = reopened.log.count()
            self.assertEqual(reopened.derive_task(self.source, "CHILD_GOAL", request_id="atomic-request-001"),
                             {"task_id": child, "created": False})
            self.assertEqual(reopened.log.count(), count)
            self.assertTrue(reopened.verify_chain().ok)

    def test_source_change_during_append_recaptures_consistent_background(self):
        emit = self.rt.log.emit
        raced = []

        def change_source(**kwargs):
            if kwargs["type"] == "task.created" and not raced:
                raced.append(True)
                self.rt.command(self.source, "RACING_DIRECTION", goal="RACING_GOAL")
                self.rt.rulebook.record("RACING_RULE", scope="task", task_id=self.source)
            return emit(**kwargs)

        with patch.object(self.rt.log, "emit", side_effect=change_source):
            child = self.derive()
        self.assertEqual(raced, [True])
        snapshot = self.rt.conversation_background(child)
        source_head = self.rt.log.read(task_id=self.source)[-1]
        self.assertEqual(snapshot["source_goal"], "RACING_GOAL")
        self.assertEqual((snapshot["source_seq"], snapshot["source_hash"]), (source_head.seq, source_head.hash))
        self.assertIn("RACING_DIRECTION", [s["text"] for s in snapshot["sources"]])
        self.assertEqual([r["text"] for r in self.rt.rulebook.current_snapshot(child)["rules"]], ["RACING_RULE"])
        self.assertEqual(len(list(self.rt.log.iter_events(task_id=child, event_types=("task.created",)))), 1)
        self.assertTrue(self.rt.verify_chain().ok)

    def test_request_id_is_durable_and_conflicting_reuse_never_writes(self):
        child = self.derive()
        snapshot = self.rt.conversation_background(child)
        self.rt.command(self.source, "LATER_DIRECTION", goal="LATER_GOAL")
        other = self.rt.create_task("Other source", project_id=self.project)
        count = self.rt.log.count()
        with Runtime(self.db) as reopened:
            self.assertEqual(reopened.derive_task(self.source, "CHILD_GOAL", request_id="derive-request-001"),
                             {"task_id": child, "created": False})
            self.assertEqual(reopened.conversation_background(child), snapshot)
            for source, goal, acceptance in ((self.source, "Different goal", None),
                                              (other, "CHILD_GOAL", None),
                                              (self.source, "CHILD_GOAL", ["Different acceptance"])):
                with self.subTest(source=source, goal=goal, acceptance=acceptance):
                    with self.assertRaises(WorkConflict):
                        reopened.derive_task(source, goal, acceptance, request_id="derive-request-001")
                    self.assertEqual(reopened.log.count(), count)

    def test_oversized_requirements_are_rejected_without_partial_child(self):
        from cater.conversations import MAX_BACKGROUND_SOURCES

        for index in range(MAX_BACKGROUND_SOURCES):
            self.rt.log.emit(type="human.commanded", task_id=self.source, actor=Actor.human(),
                             payload={"instruction": f"requirement-{index}"})
        count = self.rt.log.count()
        with self.assertRaisesRegex(ValueError, "来源背景过大"):
            self.derive()
        self.assertEqual(self.rt.log.count(), count)
        self.assertEqual(len(self.rt.tasks()), 1)

    def test_background_byte_capacity_keeps_full_requirements_or_rejects(self):
        for index in range(12):
            self.rt.log.emit(type="human.commanded", task_id=self.source, actor=Actor.human(),
                             payload={"instruction": str(index) + "界" * 10000})
        count = self.rt.log.count()
        with self.assertRaisesRegex(ValueError, "来源背景过大"):
            self.derive()
        self.assertEqual(self.rt.log.count(), count)

    def test_parent_change_during_final_review_does_not_revoke_child_delivery(self):
        self.publish(self.source, "FROZEN_REFERENCE")
        parent_rule = self.rt.rulebook.record("KEEP_INTERFACE", scope="task", task_id=self.source)
        child = self.derive()
        snapshot = self.rt.conversation_background(child)
        config = json.loads(Path("cater/packs/default_roles.json").read_text(encoding="utf-8"))
        config["organization"]["leader"]["driver"] = "final"
        roles = Path(self.tmp.name) / "delivery-roles.json"
        roles.write_text(json.dumps(config), encoding="utf-8")
        final = DeliveryDriver()
        recorded = RecordingDriver(final)
        worker = DocumentWorkDriver()
        drivers = {**{name: worker for name in ("product", "dev", "test", "design")}, "final": recorded}
        with Runtime(self.db, roles_path=roles, drivers=drivers) as rt:
            def change_parent(_):
                self.rt.command(self.source, "PARENT_CHANGED_DURING_REVIEW", goal="LATE_PARENT_GOAL")
                self.rt.rulebook.update(parent_rule["id"], revision=1, text="LATE_PARENT_RULE", status="active")
            final.after = change_parent
            rt.plan_work(child, [item("child-document")])
            rt.collaborate(child)
            delivery = rt.delivery.view(child)
            self.assertEqual(delivery["status"], "accepted")
            self.assertEqual(rt.task(child).status, "done")
            self.assertEqual(delivery["manifest"]["background"]["source_seq"], snapshot["source_seq"])
            self.assertEqual(len(recorded.calls), 1)
            call = recorded.calls[0]
            self.assertEqual(call["context"]["work"]["phase"], "delivery")
            self.assertEqual(call["context"]["background"], snapshot)
            self.assertIn("FROZEN_REFERENCE", call["prompt"])
            self.assertNotIn("LATE_PARENT_", call["prompt"])
            self.assertEqual({s["text"] for s in delivery["manifest"]["sources"]}, {"CHILD_GOAL", "KEEP_INTERFACE"})
            self.rt.command(self.source, "ANOTHER_PARENT_UPDATE")
            self.assertEqual(rt.delivery.view(child), delivery)
            self.assertTrue(rt.delivery.archive(child))
            self.assertTrue(rt.verify_chain().ok)

    def test_competing_request_between_capture_and_append_creates_one_child(self):
        emit = self.rt.log.emit
        winner = []

        def competing_create(**kwargs):
            if kwargs["type"] == "task.created" and not winner:
                with Runtime(self.db) as competing:
                    winner.append(competing.derive_task(self.source, "CHILD_GOAL", request_id="competing-request"))
            return emit(**kwargs)

        with patch.object(self.rt.log, "emit", side_effect=competing_create):
            result = self.rt.derive_task(self.source, "CHILD_GOAL", request_id="competing-request")
        self.assertTrue(winner[0]["created"])
        self.assertEqual(result, {"task_id": winner[0]["task_id"], "created": False})
        self.assertEqual(len(self.rt.tasks()), 2)
        self.assertEqual(len(list(self.rt.log.iter_events(task_id=result["task_id"], event_types=("task.started",)))), 1)
        self.assertTrue(self.rt.verify_chain().ok)

    def test_parent_changes_do_not_leak_into_memory_profiles_scope_or_execution(self):
        self.publish(self.source, "FROZEN_BODY")
        self.say(self.source, "PARENT_CHAT_BEFORE")
        child = self.derive()
        own = self.say(child, "CHILD_LOCAL_CLAIM", actor="design")
        self.rt.plan_work(child, [item("child-work")])
        snapshot = self.rt.conversation_background(child)
        memory = self.rt.shared_memory(exclude_task_id=child)
        scope = self.rt.work.scope(child)
        self.assertEqual([(a["task_id"], a["title"]) for a in memory], [(self.source, "FROZEN_BODY")])
        sibling = self.rt.create_task("SIBLING_GOAL", project_id=self.project)

        mutations = [lambda: self.say(self.source, "PARENT_CHAT_AFTER"),
                     lambda: self.rt.command(self.source, "PARENT_DIRECTION_AFTER", goal="PARENT_GOAL_AFTER"),
                     lambda: self.rt.decide(self.source, "rework", "PARENT_REWORK_AFTER"),
                     lambda: self.publish(self.source, "PARENT_ARTIFACT_AFTER"),
                     lambda: self.say(sibling, "SIBLING_CHAT"),
                     lambda: self.publish(sibling, "SIBLING_ARTIFACT")]
        for index, mutate in enumerate(mutations):
            with self.subTest(mutation=index):
                mutate()
                self.assertEqual(self.rt.conversation_background(child), snapshot)
                self.assertEqual(self.rt.shared_memory(exclude_task_id=child), memory)
                self.assertEqual(self.rt.work.scope(child), scope)
                self.assertTrue(self.rt.work.view(child)["scope_current"])
        profile_types = ("msg.said", "msg.challenge", "driver.failed")
        events = self.rt._project_events(child, profile_types)
        self.assertEqual([e.seq for e in events], [own.seq])
        profiles = agent_profiles(events)
        self.assertEqual(set(profiles), {"design"})
        self.assertEqual(profiles["design"]["last_claim"], "CHILD_LOCAL_CLAIM")
        self.rt.close()
        with Runtime(self.db, drivers=self.drivers) as reopened:
            self.assertEqual(reopened.conversation_background(child), snapshot)
            self.assertEqual(reopened.shared_memory(exclude_task_id=child), memory)
            self.assertEqual(reopened.work.scope(child), scope)
            self.assertEqual(agent_profiles(reopened._project_events(child, profile_types)), profiles)
            with patch("cater.runtime.agent_profiles", wraps=agent_profiles) as projection:
                reopened.collaborate(child)
            self.assertTrue(projection.called)
            for call in projection.call_args_list:
                self.assertTrue(all(e.task_id == child for e in call.args[0]))
            self.assertTrue(reopened.work.view(child)["stage_accepted"])
            self.assertTrue(reopened.verify_chain().ok)
        self.assertEqual(len(self.recorder.calls), 2)
        for call in self.recorder.calls:
            self.assertEqual(call["context"]["background"], snapshot)
            self.assertIn("FROZEN_BODY", call["prompt"])
            self.assertIn("CHILD_GOAL", call["prompt"])
            self.assertNotIn("PARENT_", json.dumps(call))
            self.assertNotIn("SIBLING_", json.dumps(call))

    def test_child_and_descendant_updates_do_not_enter_original_group_context(self):
        for project_id in (self.project, None):
            with self.subTest(project_id=project_id):
                source = self.rt.create_task("ORIGINAL_GROUP", project_id=project_id)
                regular = self.rt.create_task("Ordinary shared group", project_id=project_id)
                self.say(source, "SOURCE_OWN_CLAIM")
                ordinary = self.publish(regular, "ORDINARY_SHARED_REFERENCE")
                self.say(regular, "ORDINARY_SHARED_CLAIM", actor="design")
                self.rt.plan_work(source, [item("original-work")])
                scope = self.rt.work.scope(source)
                child = self.derive(source=source, request_id=f"reverse-child-{project_id}")
                self.say(child, "CHILD_PRIVATE_CLAIM")
                self.publish(child, "CHILD_PRIVATE_BODY")
                descendant = self.derive(source=child, request_id=f"reverse-descendant-{project_id}")
                self.say(descendant, "DESCENDANT_PRIVATE_CLAIM", actor="design")
                self.publish(descendant, "DESCENDANT_PRIVATE_BODY")

                memory = self.rt.shared_memory(exclude_task_id=source)
                self.assertIn(ordinary.seq, [entry["seq"] for entry in memory])
                self.assertTrue(all(entry["task_id"] not in {child, descendant} for entry in memory))
                profiles = agent_profiles(self.rt._project_events(source, ("msg.said",)))
                self.assertEqual(profiles["dev"]["last_claim"], "SOURCE_OWN_CLAIM")
                self.assertEqual(profiles["design"]["last_claim"], "ORDINARY_SHARED_CLAIM")
                self.assertEqual(self.rt.work.scope(source), scope)
                self.recorder.calls.clear()
                self.rt.collaborate(source)
                self.assertTrue(self.recorder.calls)
                self.assertTrue(any("ORDINARY_SHARED_REFERENCE" in call["prompt"]
                                    for call in self.recorder.calls))
                for call in self.recorder.calls:
                    self.assertNotIn("CHILD_PRIVATE", json.dumps(call))
                    self.assertNotIn("DESCENDANT_PRIVATE", json.dumps(call))
                with Runtime(self.db) as reopened:
                    self.assertTrue(all(entry["task_id"] not in {child, descendant}
                                        for entry in reopened.shared_memory(exclude_task_id=source)))
                self.assertTrue(self.rt.verify_chain().ok)

    def test_task_rules_are_independent_but_workspace_and_project_rules_stay_live(self):
        book = self.rt.rulebook
        shared = [book.record("WORKSPACE_RULE", scope="workspace"),
                  book.record("PROJECT_RULE", scope="project", project_id=self.project)]
        parent = book.record("PARENT_RULE", scope="task", task_id=self.source)
        retired = book.record("RETIRED_RULE", scope="task", task_id=self.source)
        book.update(retired["id"], revision=1, text=retired["text"], status="retired")
        proposal = self.say(self.source, "Suggested rule")
        book.record("CANDIDATE_RULE", scope="task", task_id=self.source,
                    proposal_event=proposal, rationale="Unadopted suggestion")
        child = self.derive()
        snapshot = self.rt.conversation_background(child)
        copied = [r for r in book.view(task_id=child)["rules"] if r["scope"] == "task"]
        self.assertEqual(len(copied), 1)
        copied = copied[0]
        self.assertNotEqual(copied["id"], parent["id"])
        self.assertEqual((copied["target_id"], copied["revision"], copied["text"]), (child, 1, "PARENT_RULE"))
        self.assertEqual(copied["source"]["rule_id"], parent["id"])
        self.rt.plan_work(child, [item("child-work")])
        scope = self.rt.work.scope(child)
        book.update(parent["id"], revision=1, text="PARENT_EDIT", status="active")
        book.record("PARENT_NEW_RULE", scope="task", task_id=self.source)
        self.assertEqual(self.rt.work.scope(child), scope)
        book.update(copied["id"], revision=1, text="CHILD_EDIT", status="active", task_id=child)
        self.assertNotEqual(self.rt.work.scope(child), scope)
        self.assertFalse(self.rt.work.view(child)["scope_current"])
        self.assertIn("PARENT_EDIT", [r["text"] for r in book.current_snapshot(self.source)["rules"]])
        self.assertNotIn("CHILD_EDIT", [r["text"] for r in book.current_snapshot(self.source)["rules"]])
        for rule in shared:
            with self.subTest(scope=rule["scope"]):
                self.rt.plan_work(child, [item("child-work")])
                scope = self.rt.work.scope(child)
                book.update(rule["id"], revision=1, text=rule["text"] + "_UPDATED", status="active")
                self.assertNotEqual(self.rt.work.scope(child), scope)
                self.assertFalse(self.rt.work.view(child)["scope_current"])
        self.rt.plan_work(child, [item("child-work")])
        self.rt.collaborate(child)
        expected = {"CHILD_EDIT", "WORKSPACE_RULE_UPDATED", "PROJECT_RULE_UPDATED"}
        self.assertTrue(self.rt.work.view(child)["stage_accepted"])
        for call in self.recorder.calls:
            self.assertEqual({r["text"] for r in call["context"]["rule_snapshot"]["rules"]}, expected)
            for text in expected:
                self.assertIn(text, call["prompt"])
            self.assertNotIn("PARENT_EDIT", call["prompt"])
        self.assertEqual(self.rt.conversation_background(child), snapshot)
        with Runtime(self.db) as reopened:
            self.assertEqual({r["text"] for r in reopened.rulebook.current_snapshot(child)["rules"]}, expected)
            self.assertEqual(reopened.conversation_background(child), snapshot)

    def test_descendant_sources_are_flat_deduplicated_requirements_not_chat(self):
        self.rt.command(self.source, "SOURCE_DIRECTION", goal="SOURCE_REVISED", acceptance=["REVISED_CHECK"])
        self.rt.decide(self.source, "rework", "SOURCE_REWORK_REASON")
        self.rt.command(self.source, "status")
        self.rt.command(self.source, "resume")
        self.say(self.source, "CHAT_ONLY_MARKER " * 1000)
        self.publish(self.source, "ANCESTOR_ARTIFACT")
        child = self.derive()
        self.rt.command(child, "CHILD_DIRECTION")
        grandchild = self.derive(source=child, goal="GRANDCHILD_GOAL", request_id="grandchild-request")
        descendant = self.derive(source=grandchild, goal="DESCENDANT_GOAL", request_id="descendant-request")
        snapshot = self.rt.conversation_background(descendant)
        sources = snapshot["sources"]
        expected = {"SOURCE_GOAL", "SOURCE_ACCEPTANCE", "SOURCE_DIRECTION", "SOURCE_REVISED", "REVISED_CHECK",
                    "SOURCE_REWORK_REASON", "CHILD_GOAL", "CHILD_DIRECTION", "GRANDCHILD_GOAL"}
        self.assertEqual({s["text"] for s in sources}, expected)
        self.assertEqual(len(sources), len(expected))
        self.assertEqual(len({(s["task_id"], s["id"]) for s in sources}), len(sources))
        self.assertEqual({s["task_id"] for s in sources}, {self.source, child, grandchild})
        for source in sources:
            event = next(e for e in self.rt.log.read(task_id=source["task_id"]) if e.seq == source["seq"])
            self.assertIn(source["text"], json.dumps(event.payload))
        self.assertNotIn("CHAT_ONLY_MARKER", json.dumps(snapshot))
        self.assertNotIn("background", snapshot)
        self.assertEqual([a["content"] for a in snapshot["artifacts"]], ["ANCESTOR_ARTIFACT"])
        self.assertEqual(snapshot["omitted_artifacts"], 0)

    def test_artifact_cap_keeps_accepted_bodies_and_excludes_work_drafts(self):
        names = ["accepted-0", "accepted-1", "accepted-2", "accepted-3", "rejected", "pending", "unsubmitted"]
        self.rt.plan_work(self.source, [item(name) for name in names])
        accepted = []
        for name in names:
            claim = self.rt.work.claim(self.source, name)
            event = self.publish(self.source, name + " BODY\n", work_item_id=name,
                                 attempt_id=claim.payload["attempt_id"])
            if name == "unsubmitted":
                continue
            submission = self.rt.work.submit(self.source, claim, [event.seq])
            if name == "pending":
                continue
            self.rt.work.review(self.source, name, reviewer="test", submission_seq=submission.seq,
                                results=[{"id": "meaning", "status": "failed" if name == "rejected" else "passed",
                                          "reason": "Inspected the actual body", "artifact_seq": event.seq,
                                          "quote": event.payload["content"]}])
            if name.startswith("accepted"):
                accepted.append(event)
        statuses = {i["id"]: i["status"] for i in self.rt.work.view(self.source)["items"]}
        self.assertTrue(all(statuses[name] == "accepted" for name in names[:4]))
        self.assertEqual(statuses["pending"], "submitted")
        self.assertNotEqual(statuses["rejected"], "accepted")
        child = self.derive()
        snapshot = self.rt.conversation_background(child)
        self.assertEqual(len(snapshot["artifacts"]), 3)
        self.assertEqual(snapshot["omitted_artifacts"], 4)
        self.assertTrue({a["seq"] for a in snapshot["artifacts"]} <= {e.seq for e in accepted})
        self.assertEqual(len({a["seq"] for a in snapshot["artifacts"]}), 3)
        for artifact in snapshot["artifacts"]:
            event = next(e for e in accepted if e.seq == artifact["seq"])
            self.assertEqual(artifact["content"], event.payload["content"])
            self.assertEqual(artifact["content_sha256"], hashlib.sha256(event.payload["content"].encode()).hexdigest())
            self.assertEqual(artifact["verification"], "accepted_at_capture")
        for excluded in names[4:]:
            self.assertNotIn(excluded + " BODY", json.dumps(snapshot))
        self.assertEqual({a["seq"] for a in self.rt.shared_memory(exclude_task_id=child)},
                         {a["seq"] for a in snapshot["artifacts"]})

    def test_planner_worker_and_reviewer_receive_background_without_inheriting_requirements(self):
        self.publish(self.source, "REFERENCE_BODY")
        self.rt.plan_work(self.source, [item("parent-only-item")])
        child = self.derive(acceptance=["CHILD_CHECK"])
        snapshot = self.rt.conversation_background(child)
        config = json.loads((Path(__file__).resolve().parents[1] / "cater/packs/default_roles.json").read_text(encoding="utf-8"))
        config["organization"]["leader"]["driver"] = "planner"
        roles = Path(self.tmp.name) / "roles.json"
        roles.write_text(json.dumps(config), encoding="utf-8")
        planner = PlanningDriver()
        planner.items = [item("child-only-item")]
        recording_planner = RecordingDriver(planner)
        with Runtime(self.db, roles_path=roles, drivers={**self.drivers, "planner": recording_planner}) as runtime:
            runtime.intake(child)
            self.rt.command(self.source, "LATE_PARENT_DIRECTION", goal="LATE_PARENT_GOAL")
            self.assertTrue(Planner(runtime).run(child)["installed"])
        self.rt.collaborate(child)
        self.assertTrue(self.rt.work.view(child)["stage_accepted"])
        self.assertEqual([i["id"] for i in self.rt.work.view(child)["items"]], ["child-only-item"])
        self.assertEqual(len(recording_planner.calls), 1)
        planning_context = recording_planner.calls[0]["context"]["work"]
        self.assertEqual({s["text"] for s in planning_context["sources"]}, {"CHILD_GOAL", "CHILD_CHECK"})
        self.assertEqual(planning_context["previous_plan"]["items"], [])
        self.assertEqual(len(self.recorder.calls), 2)
        self.assertEqual(self.recorder.calls[-1]["context"]["work"]["phase"], "review")
        for call in recording_planner.calls + self.recorder.calls:
            self.assertEqual(call["context"]["background"], snapshot)
            self.assertEqual(call["context"]["acceptance"], ["CHILD_CHECK"])
            for marker in ("REFERENCE_BODY", "SOURCE_GOAL", "CHILD_GOAL"):
                self.assertIn(marker, call["prompt"])
            self.assertNotIn("LATE_PARENT_", json.dumps(call))
            self.assertNotIn("parent-only-item", json.dumps(call))


class ConversationSnapshotApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "web.db"
        self.driver = WorkDriver()
        with Runtime(self.db) as rt:
            self.source = rt.create_task("HTTP_SOURCE", ["HTTP_PARENT_ACCEPTANCE"])
            self.other = rt.create_task("HTTP_OTHER_SOURCE")
            rt.rulebook.record("HTTP_PARENT_RULE", scope="task", task_id=self.source)
        self.base = start_test_web(self, str(self.db),
                                   shared_drivers={name: self.driver for name in ("product", "design", "dev", "test")})

    def request(self, path, data=None):
        request = Request(self.base + path, data=None if data is None else json.dumps(data).encode("utf-8"),
                          headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())

    def test_derive_returns_task_detail_and_retry_never_repeats_intake(self):
        path = f"/api/tasks/{self.source}/derive"
        body = {"goal": "HTTP_CHILD", "request_id": "http-derive-request", "run": False}
        status, detail = self.request(path, body)
        self.assertEqual(status, 201)
        self.assertTrue({"task", "summary", "events", "background", "organization_memory", "activity",
                         "review", "decision_ladder", "organization_path"} <= detail.keys())
        child = detail["task"]["id"]
        self.assertNotEqual(child, self.source)
        self.assertEqual(detail["summary"]["id"], child)
        self.assertEqual(detail["task"]["goal"], "HTTP_CHILD")
        self.assertEqual(detail["task"]["acceptance"], [])
        self.assertEqual(detail["background"]["source_task_id"], self.source)
        self.assertTrue(all(event["task_id"] == child for event in detail["events"]))
        self.assertEqual(sum(e["type"] == "task.interpreted" for e in detail["events"]), 1)
        self.assertFalse(detail["activity"]["active"])
        self.assertEqual(self.driver.calls, [])
        with Runtime(self.db) as rt:
            rt.command(self.source, "HTTP_LATE_DIRECTION", goal="HTTP_LATE_GOAL")
            count = rt.log.count()
        status, repeated = self.request(path, body)
        self.assertEqual(status, 200)
        self.assertEqual(repeated["task"]["id"], child)
        self.assertEqual(repeated["events"], detail["events"])
        self.assertEqual(repeated["background"], detail["background"])
        self.assertEqual(self.driver.calls, [])
        status, fetched = self.request(f"/api/tasks/{child}")
        self.assertEqual(status, 200)
        self.assertEqual(fetched["background"], detail["background"])
        self.assertEqual(fetched["task"], repeated["task"])
        with Runtime(self.db) as rt:
            self.assertEqual(rt.log.count(), count)
            self.assertEqual(len(rt.tasks()), 3)
            self.assertEqual([r["text"] for r in rt.rulebook.view(task_id=child)["rules"]], ["HTTP_PARENT_RULE"])
            self.assertTrue(rt.verify_chain().ok)

    def test_reusing_request_for_different_goal_source_or_acceptance_returns_409(self):
        body = {"goal": "HTTP_CHILD", "request_id": "http-conflict-request", "run": False}
        _, detail = self.request(f"/api/tasks/{self.source}/derive", body)
        with Runtime(self.db) as rt:
            count = rt.log.count()
        for source, change in ((self.source, {"goal": "OTHER_GOAL"}), (self.other, {}),
                               (self.source, {"acceptance": ["NEW_CHECK"]})):
            with self.subTest(source=source, change=change):
                with self.assertRaises(HTTPError) as caught:
                    self.request(f"/api/tasks/{source}/derive", {**body, **change})
                with caught.exception as error:
                    self.assertEqual(error.code, 409)
                    self.assertIn("同一创建请求", error.read().decode("utf-8"))
                with Runtime(self.db) as rt:
                    self.assertEqual(rt.log.count(), count)
                    self.assertEqual(rt.conversation_background(detail["task"]["id"]), detail["background"])

    def test_invalid_requests_and_client_supplied_provenance_create_nothing(self):
        base = {"goal": "HTTP_CHILD", "request_id": "http-invalid-request", "run": False}
        invalid = [{**base, "goal": " "}, {**base, "goal": []}, {"goal": "HTTP_CHILD", "run": False},
                   {**base, "request_id": "bad id"}, {**base, "source_task_id": self.other},
                   {**base, "project_id": "client-project"}, {**base, "sources": []}]
        with Runtime(self.db) as rt:
            count = rt.log.count()
        for body in invalid:
            with self.subTest(body=body):
                with self.assertRaises(HTTPError) as caught:
                    self.request(f"/api/tasks/{self.source}/derive", body)
                with caught.exception as error:
                    self.assertEqual(error.code, 400)
                with Runtime(self.db) as rt:
                    self.assertEqual(rt.log.count(), count)
        with self.assertRaises(HTTPError) as caught:
            self.request("/api/tasks/task_missing/derive", base)
        with caught.exception as error:
            self.assertEqual(error.code, 404)
        with Runtime(self.db) as rt:
            self.assertEqual(rt.log.count(), count)
        self.assertEqual(self.driver.calls, [])


if __name__ == "__main__":
    unittest.main()
