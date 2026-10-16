"""Project history scope and immutable reads over real native completion chains."""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cater.conversations import read_background
from cater.events import Actor
from cater.project_history import ProjectHistory, TOOL
from cater.runtime import Runtime
from cater.team import HOST
from test_simple_team import TeamDriver, turn


class ProjectHistoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.database = self.root / "history.db"
        config = json.loads((Path(__file__).resolve().parents[1] / "cater/packs/default_roles.json").read_text(encoding="utf-8"))
        config["organization"]["leader"]["driver"] = "team_test"
        for role in config["roles"]:
            role["driver"] = "team_test"
        self.roles = self.root / "roles.json"
        self.roles.write_text(json.dumps(config), encoding="utf-8")
        self.enterContext(patch.dict(os.environ, {}, clear=True))
        self.enterContext(patch("socket.socket", side_effect=AssertionError("network access")))
        self.enterContext(patch("subprocess.Popen", side_effect=AssertionError("external process")))
        self.driver = TeamDriver()
        self.rt = Runtime(self.database, roles_path=self.roles, drivers={"team_test": self.driver})
        self.addCleanup(lambda: self.rt.close())
        self.manager = self.rt.organization.leader.id
        self.number = 0
        self.project = self.rt.rulebook.create_project("Project fixture")["id"]
        self.target = self.task(self.project, goal="Target current goal")

    @property
    def history(self):
        return ProjectHistory(self.rt.team)

    def task(self, project=None, *, goal="Source fixture goal", isolated=False, actor=None):
        if isolated or actor is not None:
            self.number += 1
            task = f"task-private-{self.number}"
            payload = {"goal": goal, "acceptance": [], "project_id": project}
            if isolated:
                payload["context_scope"] = "task"
            self.rt.log.emit(type="task.created", actor=actor or Actor.human(), task_id=task, payload=payload)
        else:
            task = self.rt.create_task(goal, project_id=project)
        self.rt.team.enable(task)
        return task

    def events(self, task, kind):
        return list(self.rt.log.iter_events(task_id=task, event_types=(kind,)))

    def head(self):
        return self.rt.log.db.execute("SELECT seq, hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()

    def exchange(self, task, *calls):
        self.number += 1
        role = self.rt.team._role(self.manager)
        message = self.rt.team.receive(task, f"@{role.name} fixture exchange {self.number}",
                                       request_id=f"history-exchange-{self.number}")
        self.assertEqual(message.payload["recipient"], self.manager)
        run_id = f"history-native-{self.number}"
        started = self.rt.log.emit(type="team.run_started", actor=HOST, task_id=task, payload={
            "message_seq": message.seq, "role": self.manager, "run_id": run_id,
            "revision": self.rt.team.view(task)["revision"],
            "control_seq": self.rt.execution_control.view(task)["seq"]})
        self.driver.steps[self.manager] = [turn("", *calls), turn()]
        list(self.rt.team._run_message(task, message, role, started))
        finished, = [event for event in self.events(task, "team.run_finished")
                     if event.payload["run_id"] == run_id]
        self.assertIn(finished.payload["status"], {"completed", "waiting"}, finished.payload)
        results = [event.payload["result"] for event in self.events(task, "team.tool_finished")
                   if event.payload["run_id"] == run_id]
        self.assertEqual(len(results), len(calls))
        return results

    def publish(self, task, content="Frozen source file\n", *, filename="result.txt"):
        result, = self.exchange(task, ("publish_artifact", {"filename": filename, "content": content,
            "summary": "Fixture publication", "format": "text"}))
        self.assertEqual(result["status"], "ok", result)
        artifact, = [event for event in self.events(task, "deliverable.published")
                     if event.seq == result["artifact_seq"]]
        return artifact

    def finish(self, task, *, summary="Source delivered", verification="Manager fixture declaration", artifacts=None):
        arguments = {"summary": summary, "verification": verification}
        if artifacts is not None:
            arguments["artifact_seqs"] = [event.seq for event in artifacts]
        result, = self.exchange(task, ("finish_work", arguments))
        self.assertEqual(result["status"], "ok", result)
        event = self.events(task, "team.objective_completed")[-1]
        boundary = max(event.seq for event in self.rt.log.read(task_id=task)
                       if event.actor == HOST and event.type in {"team.started", "team.objective_updated"})
        self.assertEqual(event.payload["objective_seq"], boundary)
        self.assertIsInstance(event.payload["run_id"], str)
        run, = [row for row in self.events(task, "team.run_started")
                if row.payload["run_id"] == event.payload["run_id"]]
        self.assertLess(run.payload["message_seq"], run.seq)
        self.assertLess(run.seq, event.seq)
        return event

    def deliver(self, task, content="Frozen source file\n", **kwargs):
        artifact = self.publish(task, content)
        return self.finish(task, artifacts=[artifact], **kwargs), artifact

    def update(self, task, goal):
        result, = self.exchange(task, ("set_objective", {"goal": goal}))
        self.assertEqual(result["status"], "ok", result)

    def assert_error(self, result):
        self.assertEqual(result["status"], "error", result)
        self.assertIs(result["complete"], True)
        self.assertEqual(result["outcome"], "not_started")
        self.assertNotIn("content", result)
        self.assertNotIn("source", result)
        self.assertNotIn("entries", result)

    def read(self, delivery, *, task=None, **changes):
        return self.history.read(task or self.target, {"section": "delivery", "delivery_seq": delivery.seq, **changes})

    def read_artifact(self, delivery, artifact, *, task=None, **changes):
        return self.history.read(task or self.target, {"section": "artifact", "delivery_seq": delivery.seq,
                                                      "artifact_seq": artifact.seq, **changes})

    def candidate(self, task, *, changes=None, remove=(), actor=HOST, run_changes=None):
        """Inject adversarial completion data with an otherwise linked run/message."""
        self.number += 1
        role = self.rt.team._role(self.manager)
        message = self.rt.team.receive(task, f"@{role.name} inspect candidate {self.number}",
                                       request_id=f"history-candidate-{self.number}")
        run_id = f"history-candidate-run-{self.number}"
        run_payload = {"run_id": run_id, "message_seq": message.seq, "role": self.manager,
                       "revision": self.rt.team.view(task)["revision"],
                       "control_seq": self.rt.execution_control.view(task)["seq"], **(run_changes or {})}
        self.rt.log.emit(type="team.run_started", actor=HOST, task_id=task, payload=run_payload)
        boundary = max(event.seq for event in self.rt.log.read(task_id=task)
                       if event.actor == HOST and event.type in {"team.started", "team.objective_updated"})
        payload = {"run_id": run_id, "objective_seq": boundary, "artifacts": [],
                   "summary": "Candidate summary", "verification": "Candidate declaration", **(changes or {})}
        for key in remove:
            payload.pop(key, None)
        completed = self.rt.log.emit(type="team.objective_completed", actor=actor, task_id=task, payload=payload)
        self.rt.log.emit(type="team.run_finished", actor=HOST, task_id=task, payload={**run_payload, "status": "completed"})
        return completed

    def test_tool_name_and_project_with_no_history(self):
        self.assertEqual(TOOL, "read_project_history")
        index = self.history.index(self.target)
        self.assertTrue(index["available"])
        self.assertEqual(index["project_id"], self.project)
        self.assertEqual((index["entries"], index["total"], index["has_more"]), ([], 0, False))
        self.assertIsNone(index["next_before_seq"])
        self.assertTrue(index["historical_only"])

    def test_only_explicit_same_project_ordinary_peers_are_shared(self):
        source = self.task(self.project)
        completed, _ = self.deliver(source)
        other_project = self.rt.rulebook.create_project("Other project")["id"]
        for task in (self.target, self.task(), self.task(other_project), self.task(self.project, isolated=True)):
            self.deliver(task)
        index = self.history.index(self.target)
        self.assertEqual([row["delivery_seq"] for row in index["entries"]], [completed.seq])
        self.assertEqual(index["entries"][0]["task_id"], source)

    def test_same_project_names_never_merge_identity(self):
        duplicate = self.rt.rulebook.create_project("Project fixture")["id"]
        self.assertNotEqual(duplicate, self.project)
        outside, _ = self.deliver(self.task(duplicate))
        inside, _ = self.deliver(self.task(self.project))
        self.assertEqual([row["delivery_seq"] for row in self.history.index(self.target)["entries"]], [inside.seq])
        self.assert_error(self.read(outside))

    def test_unbound_isolated_and_unknown_targets_are_unavailable_without_leaks(self):
        source = self.task(self.project)
        completed, artifact = self.deliver(source, "Secret other group content")
        for target in (self.task(), self.task(self.project, isolated=True), "task-not-existing"):
            with self.subTest(target=target):
                index = self.history.index(target)
                self.assertFalse(index["available"])
                self.assertTrue(index.get("reason"), "Unavailable indexes need a reason")
                serialized = json.dumps(index)
                self.assertNotIn(source, serialized)
                self.assertNotIn("Secret other group content", serialized)
                self.assert_error(self.read(completed, task=target))
                self.assert_error(self.read_artifact(completed, artifact, task=target))

    def test_publication_and_claim_without_completed_native_chain_are_not_history(self):
        source = self.task(self.project)
        self.publish(source)
        self.rt.log.emit(type="msg.said", actor=Actor.agent(self.manager, "manager"), task_id=source,
                         payload={"text": "I verified and completed everything", "claim": "All done"})
        self.rt.log.emit(type="team.objective_completed", actor=HOST, task_id=source,
                         payload={"summary": "Incomplete event", "verification": "No run provenance"})
        self.assertEqual(self.history.index(self.target)["entries"], [])

    def test_actor_forged_completion_and_project_metadata_are_not_trusted(self):
        source = self.task(self.project)
        self.candidate(source, actor=Actor.agent(self.manager, "manager"))
        unbound = self.task()
        self.rt.log.emit(type="task.project_bound", actor=Actor.agent("dev", "developer"), task_id=unbound,
                         payload={"project_id": self.project})
        self.deliver(unbound)
        forged_project = "project-untrusted"
        self.rt.log.emit(type="project.created", actor=Actor.agent("dev", "developer"),
                         payload={"id": forged_project, "name": "Untrusted project"})
        forged = self.task(forged_project, actor=Actor.agent("dev", "developer"))
        self.deliver(forged)
        self.assertEqual(self.history.index(self.target)["entries"], [])

    def test_completion_requires_exact_source_message_and_run_recipient(self):
        unrelated = self.task(self.project)
        message = self.rt.team.receive(unrelated, "Foreign source message", request_id="foreign-history-message")
        for changes in ({"message_seq": message.seq}, {"message_seq": 1}, {"message_seq": True},
                        {"role": "not-the-message-recipient"}):
            with self.subTest(changes=changes):
                source = self.task(self.project)
                self.candidate(source, run_changes=changes)
        self.assertEqual(self.history.index(self.target)["entries"], [])

    def test_invalid_latest_candidates_do_not_hide_previous_valid_delivery(self):
        source = self.task(self.project)
        valid, _ = self.deliver(source)
        cases = [({"summary": ""}, ()), ({"verification": []}, ()), ({"objective_seq": 0}, ()),
                 ({"objective_seq": True}, ()), ({"artifacts": {}}, ()), ({}, ("run_id",)),
                 ({}, ("artifacts",)), ({"run_id": "unrelated-run"}, ())]
        for changes, remove in cases:
            with self.subTest(changes=changes, remove=remove):
                self.candidate(source, changes=changes, remove=remove)
                self.assertEqual([row["delivery_seq"] for row in self.history.index(self.target)["entries"]], [valid.seq])

    def test_malformed_artifact_manifests_are_not_trusted_completions(self):
        source = self.task(self.project)
        artifact = self.publish(source)
        ref = {"seq": artifact.seq, "filename": artifact.payload["filename"],
               "content_sha256": artifact.payload["content_sha256"]}
        refs = [None, {}, [None], [{**ref, "seq": True}], [{**ref, "seq": 10 ** 9}],
                [{**ref, "content_sha256": "invalid"}], [{**ref, "filename": "../private.txt"}],
                [{**ref, "local_path": "private-host-path"}], [ref, ref], [ref] * 129]
        for manifest in refs:
            with self.subTest(manifest=manifest):
                self.candidate(source, changes={"artifacts": manifest})
                self.assertEqual(self.history.index(self.target)["entries"], [])

    def test_default_and_tool_pagination_are_bounded_and_descending(self):
        completed = [self.finish(self.task(self.project)) for _ in range(7)]
        first = self.history.index(self.target)
        self.assertEqual(len(first["entries"]), 3)
        self.assertEqual(first["total"], 7)
        self.assertTrue(first["has_more"])
        self.assertEqual(first["next_before_seq"], first["entries"][-1]["delivery_seq"])
        seen = list(first["entries"])
        page = first
        while page["has_more"]:
            page = self.history.index(self.target, as_of_seq=first["as_of_seq"], before_seq=page["next_before_seq"])
            self.assertEqual((page["as_of_seq"], page["as_of_hash"], page["total"]),
                             (first["as_of_seq"], first["as_of_hash"], 7))
            seen.extend(page["entries"])
        self.assertEqual([row["delivery_seq"] for row in seen], [event.seq for event in reversed(completed)])
        self.assertIsNone(page["next_before_seq"])
        tool_index = self.history.read(self.target, {"section": "index"})
        self.assertEqual(tool_index["status"], "ok")
        self.assertEqual(len(tool_index["entries"]), 5)

    def test_index_cursor_is_exclusive_and_old_pages_ignore_new_deliveries(self):
        deliveries = [self.finish(self.task(self.project)) for _ in range(4)]
        first = self.history.index(self.target, limit=2)
        newcomer = self.finish(self.task(self.project))
        next_page = self.history.index(self.target, as_of_seq=first["as_of_seq"],
                                       before_seq=first["next_before_seq"], limit=2)
        self.assertEqual([row["delivery_seq"] for row in next_page["entries"]], [deliveries[1].seq, deliveries[0].seq])
        self.assertEqual(next_page["total"], 4)
        self.assertEqual(self.history.index(self.target)["entries"][0]["delivery_seq"], newcomer.seq)
        self.assertEqual(self.history.index(self.target, before_seq=deliveries[0].seq)["entries"], [])

    def test_latest_delivery_per_source_and_objective_are_frozen_as_of(self):
        source = self.task(self.project, goal="Original objective")
        old, old_file = self.deliver(source, "version one", summary="Old summary")
        anchor = self.history.index(self.target)
        self.update(source, "Replacement objective")
        new, new_file = self.deliver(source, "version two", summary="New summary")
        historical = self.history.index(self.target, as_of_seq=anchor["as_of_seq"])
        self.assertEqual(historical, anchor)
        fresh = self.history.index(self.target)
        self.assertEqual([row["delivery_seq"] for row in fresh["entries"]], [new.seq])
        self.assertEqual(fresh["entries"][0]["objective_excerpt"], "Replacement objective")
        self.assertEqual(json.loads(self.read(old, as_of_seq=anchor["as_of_seq"])["content"])["summary"], "Old summary")
        self.assertEqual(self.read_artifact(old, old_file, as_of_seq=anchor["as_of_seq"])["content"], "version one")
        self.assert_error(self.read_artifact(old, new_file, as_of_seq=anchor["as_of_seq"]))
        self.assert_error(self.read(old))

    def test_new_unfinished_objective_does_not_rewrite_last_delivered_goal(self):
        source = self.task(self.project, goal="Delivered objective")
        delivery = self.finish(source)
        self.update(source, "New unfinished objective")
        row, = self.history.index(self.target)["entries"]
        self.assertEqual(row["delivery_seq"], delivery.seq)
        self.assertEqual(row["objective_excerpt"], "Delivered objective")

    def test_source_joining_project_later_does_not_backfill_old_catalog(self):
        source = self.task()
        delivery = self.finish(source)
        anchor = self.history.index(self.target)
        self.assertEqual(anchor["entries"], [])
        self.rt.rulebook.bind_project(source, self.project)
        self.assertEqual(self.history.index(self.target, as_of_seq=anchor["as_of_seq"])["entries"], [])
        self.assertEqual(self.history.index(self.target)["entries"][0]["delivery_seq"], delivery.seq)

    def test_target_joining_later_rejects_cursor_before_its_project_binding(self):
        source = self.task(self.project)
        delivery = self.finish(source)
        target = self.task()
        prior = self.head()["seq"]
        self.rt.rulebook.bind_project(target, self.project)
        with self.assertRaises(ValueError):
            self.history.index(target, as_of_seq=prior)
        self.assert_error(self.history.read(target, {"section": "index", "as_of_seq": prior}))
        self.assert_error(self.read(delivery, task=target, as_of_seq=prior))
        self.assertTrue(self.history.index(target)["available"])

    def test_actual_event_seq_and_hash_anchor_not_event_count(self):
        delivery = self.finish(self.task(self.project))
        old_head = self.head()["seq"]
        self.rt.log.db.execute("UPDATE sqlite_sequence SET seq = seq + 10 WHERE name = 'events'")
        marker = self.rt.log.emit(type="msg.said", actor=Actor.agent(self.manager, "manager"), task_id=self.target,
                                  payload={"text": "Unrelated marker"})
        index = self.history.index(self.target)
        self.assertEqual((index["as_of_seq"], index["as_of_hash"]), (marker.seq, marker.hash))
        self.assertNotEqual(index["as_of_seq"], self.rt.log.count())
        self.assertTrue(self.rt.verify_chain().ok)
        with self.assertRaises(ValueError):
            self.history.index(self.target, as_of_seq=old_head + 1)
        self.assert_error(self.read(delivery, as_of_seq=old_head + 1))

    def test_invalid_index_argument_types_and_ranges(self):
        head = self.head()["seq"]
        for arguments in ({"limit": 0}, {"limit": 11}, {"limit": True}, {"limit": 1.0},
                          {"before_seq": 0}, {"before_seq": -1}, {"before_seq": True},
                          {"before_seq": head + 2}, {"as_of_seq": -1}, {"as_of_seq": True},
                          {"as_of_seq": "1"}, {"as_of_seq": head + 1}, {"as_of_seq": 2 ** 63}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                self.history.index(self.target, **arguments)
        self.assertEqual(self.history.index(self.target, as_of_seq=None), self.history.index(self.target))

    def test_invalid_reader_shape_selectors_nulls_and_paging_are_rejected(self):
        delivery, artifact = self.deliver(self.task(self.project))
        valid = {"section": "artifact", "delivery_seq": delivery.seq, "artifact_seq": artifact.seq}
        invalid = [None, [], {}, {"section": "unknown"}, {"section": []}, {"section": "delivery"},
                   {**valid, "task_id": self.target}, {**valid, "project_id": self.project},
                   {**valid, "path": str(self.root / "private.txt")}, {**valid, "extra": "private-body"},
                   {**valid, "limit": 1}, {**valid, "before_seq": 1},
                   {"section": "index", "offset": 0}, {"section": "index", "length": 1},
                   {"section": "index", "delivery_seq": delivery.seq},
                   {"section": "delivery", "delivery_seq": delivery.seq, "artifact_seq": artifact.seq}]
        for key, values in {"delivery_seq": [True, 0, -1, "1", None], "artifact_seq": [False, 0, 1.0, None],
                            "offset": [True, -1, "0"], "length": [False, 0, 16001, 1.0],
                            "as_of_seq": [None, True, -1, "1"]}.items():
            invalid.extend({**valid, key: value} for value in values)
        invalid.extend({"section": "index", key: value} for key, value in
                       (("before_seq", None), ("before_seq", True), ("limit", 0), ("limit", True), ("limit", 11)))
        for arguments in invalid:
            with self.subTest(arguments=arguments):
                self.assert_error(self.history.read(self.target, arguments))

    def test_out_of_scope_and_nonexistent_references_have_same_safe_error(self):
        other_project = self.rt.rulebook.create_project("Private project")["id"]
        private, private_file = self.deliver(self.task(other_project), "Private project body")
        absent = self.history.read(self.target, {"section": "delivery", "delivery_seq": self.head()["seq"] + 100})
        outside = self.read(private)
        self.assert_error(outside)
        self.assertEqual(outside, absent)
        self.assert_error(self.read_artifact(private, private_file))
        self.assertNotIn("Private project body", json.dumps(outside))
        self.assertNotIn(private.task_id, json.dumps(outside))

    def test_utf8_excerpts_and_artifact_references_are_bounded(self):
        source = self.task(self.project, goal="\u6c49\U0001f642" * 300)
        files = [self.publish(source, f"Body {i}", filename=f"file-{i}.txt") for i in range(5)]
        delivery = self.finish(source, summary="\u754c\U0001f642" * 300, artifacts=files)
        index = self.history.index(self.target)
        row, = index["entries"]
        for key in ("objective_excerpt", "summary_excerpt"):
            self.assertLessEqual(len(row[key].encode("utf-8")), 320)
            self.assertTrue(row[key])
        self.assertTrue(row["objective_truncated"])
        self.assertTrue(row["summary_truncated"])
        self.assertEqual(row["artifact_count"], 5)
        self.assertEqual(len(row["artifacts"]), 3)
        self.assertTrue(row["artifacts_truncated"])
        self.assertEqual(row["verification_kind"], "manager_declared")
        self.assertEqual(row["delivery_hash"], delivery.hash)
        body = json.loads(self.read(delivery, length=16000)["content"])
        self.assertEqual(len(body["artifacts"]), 5)
        self.assertEqual(self.read_artifact(delivery, files[-1])["content"], "Body 4")
        self.assertNotIn("Body 4", json.dumps(index))

    def test_filename_utf8_bounds_preserve_manifest_and_read_identity(self):
        for size in (129, 159, 180):
            with self.subTest(filename_bytes=size):
                source = self.task(self.project)
                filename = "x" * (size - 4) + ".txt"
                self.assertEqual(len(filename.encode("utf-8")), size)
                # Metadata identity does not depend on Windows' local path-length support.
                with patch.object(self.rt, "_artifact_root", None):
                    artifact = self.publish(source, "Long filename frozen body", filename=filename)
                delivery = self.finish(source, artifacts=[artifact])
                index = self.history.index(self.target)
                row, = [entry for entry in index["entries"] if entry["task_id"] == source]
                expected = {"seq": artifact.seq, "filename": filename,
                            "content_sha256": artifact.payload["content_sha256"]}
                self.assertEqual(row["artifacts"], [expected])
                for entry in index["entries"]:
                    self.assertTrue(all(len(ref["filename"].encode("utf-8")) <= 180 for ref in entry["artifacts"]))
                manifest = json.loads(self.read(delivery)["content"])["artifacts"]
                self.assertEqual(manifest, [expected])
                body = self.read_artifact(delivery, artifact)
                self.assertEqual(body["status"], "ok")
                self.assertEqual(body["content"], "Long filename frozen body")
                self.assertEqual(body["source"]["filename"], filename)
                self.assertEqual(body["source"]["artifact_seq"], artifact.seq)
                self.assertEqual(body["source"]["content_sha256"], expected["content_sha256"])
                self.assertLessEqual(len(json.dumps(index, ensure_ascii=False).encode("utf-8")), 16000)

    def test_full_json_budget_pages_without_dropping_valid_sources(self):
        completed = []
        for number in range(10):
            source = self.task(self.project, goal="\u6c49" * 300)
            calls = [("publish_artifact", {"filename": f"file-{number}-{index}-" + "x" * 100 + ".txt",
                      "content": "Fixture file", "summary": "Publication", "format": "text"}) for index in range(3)]
            with patch.object(self.rt, "_artifact_root", None):
                results = self.exchange(source, *calls)
            self.assertTrue(all(result["status"] == "ok" for result in results), results)
            seqs = {result["artifact_seq"] for result in results}
            artifacts = [event for event in self.events(source, "deliverable.published") if event.seq in seqs]
            completed.append(self.finish(source, artifacts=artifacts, summary="\u754c" * 300))
        first = self.history.index(self.target, limit=10)
        self.assertLess(len(first["entries"]), 10)
        self.assertTrue(first["has_more"])
        page, seen = first, []
        while True:
            self.assertLessEqual(len(json.dumps(page, ensure_ascii=False).encode("utf-8")), 16000)
            self.assertEqual(page["total"], 10)
            seen.extend(row["delivery_seq"] for row in page["entries"])
            if not page["has_more"]:
                break
            page = self.history.index(self.target, as_of_seq=first["as_of_seq"],
                                      before_seq=page["next_before_seq"], limit=10)
        self.assertEqual(seen, [event.seq for event in reversed(completed)])

    def test_delivery_read_is_whitelisted_and_body_pagination_uses_characters(self):
        source = self.task(self.project)
        delivery, artifact = self.deliver(source, "\u6c49\U0001f642abc" * 5000)
        description = self.read(delivery, length=16000)
        self.assertEqual(description["status"], "ok")
        data = json.loads(description["content"])
        self.assertTrue({"summary", "verification", "artifacts"} <= set(data))
        self.assertTrue(set(data) <= {"objective", "summary", "verification", "handoff", "artifacts"})
        self.assertNotIn(str(self.root), description["content"])
        content = artifact.payload["content"]
        chunks, offset = [], 0
        while True:
            result = self.read_artifact(delivery, artifact, offset=offset, length=7000)
            self.assertEqual((result["status"], result["complete"], result["historical_only"]), ("ok", True, True))
            self.assertEqual(result["offset"], offset)
            self.assertEqual(result["total_chars"], len(content))
            self.assertLessEqual(len(result["content"]), 7000)
            self.assertEqual(result["source"]["content_sha256"], hashlib.sha256(content.encode("utf-8")).hexdigest())
            chunks.append(result["content"])
            if not result["has_more"]:
                self.assertIsNone(result["next_offset"])
                break
            self.assertEqual(result["next_offset"], offset + len(result["content"]))
            offset = result["next_offset"]
        self.assertEqual("".join(chunks), content)
        self.assertEqual(len(self.read_artifact(delivery, artifact)["content"]), 8000)
        self.assertEqual(self.read_artifact(delivery, artifact, offset=len(content))["content"], "")
        self.assert_error(self.read_artifact(delivery, artifact, offset=len(content) + 1))

    def test_manifest_membership_and_event_bytes_not_disk_control_artifact_reads(self):
        source = self.task(self.project)
        selected = self.publish(source, "Frozen bytes", filename="selected.txt")
        unpublished_in_delivery = self.publish(source, "Not selected", filename="excluded.txt")
        delivery = self.finish(source, artifacts=[selected])
        local = next(row["local_path"] for row in self.rt.task(source).deliverables if row["seq"] == selected.seq)
        Path(local).write_text("Disk content changed", encoding="utf-8")
        self.assertEqual(self.read_artifact(delivery, selected)["content"], "Frozen bytes")
        Path(local).unlink()
        self.assertEqual(self.read_artifact(delivery, selected)["content"], "Frozen bytes")
        self.assert_error(self.read_artifact(delivery, unpublished_in_delivery))

    def test_read_rejects_frozen_reference_hash_filename_and_task_mismatches(self):
        source = self.task(self.project)
        artifact = self.publish(source, "Actual event body")
        ref = {"seq": artifact.seq, "filename": artifact.payload["filename"],
               "content_sha256": artifact.payload["content_sha256"]}
        foreign = self.publish(self.task(self.project), "Other task body")
        for change in ({"content_sha256": "f" * 64}, {"filename": "different.txt"},
                       {"seq": foreign.seq, "filename": foreign.payload["filename"],
                        "content_sha256": foreign.payload["content_sha256"]}):
            with self.subTest(change=change):
                candidate = self.candidate(source, changes={"artifacts": [{**ref, **change}]})
                result = self.history.read(self.target, {"section": "artifact", "delivery_seq": candidate.seq,
                    "artifact_seq": change.get("seq", artifact.seq)})
                self.assert_error(result)

    def test_artifact_body_digest_and_shape_are_validated_after_manifest_match(self):
        advertised = hashlib.sha256(b"Expected original content").hexdigest()
        for content in ("Changed event body", ["not text"], None):
            with self.subTest(content=content):
                source = self.task(self.project)
                # Adversarial historical publication; delivery still uses a native finish run.
                artifact = self.rt.log.emit(type="deliverable.published", actor=Actor.agent(self.manager, "manager"),
                    task_id=source, payload={"filename": "broken.txt", "title": "broken.txt", "summary": "Fixture",
                        "format": "text", "content": content, "content_sha256": advertised, "source": "team", "phase": "team"})
                completed = self.finish(source, artifacts=[artifact])
                self.assertEqual(self.history.index(self.target)["entries"][0]["delivery_seq"], completed.seq)
                self.assert_error(self.read_artifact(completed, artifact))

    def test_completion_without_version_one_native_start_is_not_shareable(self):
        for version in (True, 0, 2, "1", None):
            with self.subTest(version=version):
                source = self.rt.create_task("Unsupported native start", project_id=self.project)
                self.rt.log.emit(type="team.started", actor=HOST, task_id=source, payload={"version": version})
                self.candidate(source)
                self.assertEqual(self.history.index(self.target)["entries"], [])

    def test_current_project_authorization_is_rechecked_even_with_old_asof(self):
        source = self.task(self.project)
        delivery, artifact = self.deliver(source)
        anchor = self.history.index(self.target)
        other = self.rt.rulebook.create_project("Changed current scope")["id"]
        self.rt.log.emit(type="task.project_bound", actor=Actor.human(), task_id=self.target,
                         payload={"project_id": other})
        self.assert_error(self.read(delivery, as_of_seq=anchor["as_of_seq"]))
        self.assert_error(self.read_artifact(delivery, artifact, as_of_seq=anchor["as_of_seq"]))
        self.assertEqual(self.history.index(self.target)["entries"], [])

    def test_index_never_decodes_artifact_payload_and_read_decodes_only_selected_artifact(self):
        source = self.task(self.project)
        chosen = self.publish(source, "Chosen body", filename="chosen.txt")
        extra = self.publish(source, "Other body" * 1000, filename="extra.txt")
        delivery = self.finish(source, artifacts=[chosen, extra])
        foreign = self.publish(self.task(), "Unrelated private body" * 1000)
        decoded = []
        original = self.rt.log._row_to_event

        def decode(row):
            decoded.append((row["seq"], row["type"]))
            return original(row)

        with patch.object(self.rt.log, "_row_to_event", side_effect=decode):
            self.history.index(self.target)
            self.assertFalse([row for row in decoded if row[1] == "deliverable.published"])
            decoded.clear()
            result = self.read_artifact(delivery, chosen)
            self.assertEqual(result["content"], "Chosen body")
            self.assertEqual([seq for seq, kind in decoded if kind == "deliverable.published"], [chosen.seq])
            self.assertNotIn((foreign.seq, "deliverable.published"), decoded)

    def test_reads_and_index_are_pure_and_do_not_create_model_calls(self):
        delivery, artifact = self.deliver(self.task(self.project))
        before = (self.rt.log.count(), self.rt.log.head_hash(), len(self.driver.calls))
        self.history.index(self.target)
        self.history.read(self.target, {"section": "index"})
        self.read(delivery)
        self.read_artifact(delivery, artifact)
        self.assertEqual((self.rt.log.count(), self.rt.log.head_hash(), len(self.driver.calls)), before)

    def test_public_arguments_keep_only_bounded_fixed_selectors(self):
        arguments = {"section": "artifact", "delivery_seq": 5, "artifact_seq": 3, "offset": 0, "length": 12,
                     "task_id": "private-task", "project_id": "private-project", "path": "/private/path",
                     "content": "Private extra body", "as_of_seq": True, "before_seq": -1, "limit": 2 ** 100}
        self.assertEqual(self.history.public_arguments(arguments),
                         {"section": "artifact", "delivery_seq": 5, "artifact_seq": 3, "offset": 0, "length": 12})
        self.assertEqual(self.history.public_arguments(None), {})

    def test_restart_preserves_anchor_versions_and_artifact_contents(self):
        delivery, artifact = self.deliver(self.task(self.project), "Survives restart")
        index = self.history.index(self.target)
        body = self.read_artifact(delivery, artifact, as_of_seq=index["as_of_seq"])
        self.rt.close()
        self.driver = TeamDriver()
        self.rt = Runtime(self.database, roles_path=self.roles, drivers={"team_test": self.driver})
        self.assertEqual(self.history.index(self.target), index)
        self.assertEqual(self.read_artifact(delivery, artifact, as_of_seq=index["as_of_seq"]), body)
        self.assertEqual(self.driver.calls, [])
        self.assertTrue(self.rt.verify_chain().ok)

    def test_derived_source_and_target_do_not_join_live_history_and_background_stays_frozen(self):
        source = self.task(self.project)
        old, artifact = self.deliver(source, "Frozen inherited version")
        child = self.rt.derive_task(source, "Independent derived goal", request_id="derive-history-fixture")["task_id"]
        self.rt.team.enable(child)
        snapshot = copy.deepcopy(self.rt.conversation_background(child))
        self.assertFalse(self.history.index(child)["available"])
        self.assert_error(self.read(old, task=child))
        self.update(source, "Source future objective")
        self.deliver(source, "Future live version")
        self.deliver(child, "Child isolated delivery")
        self.assertEqual(self.rt.conversation_background(child), snapshot)
        frozen = read_background(self.rt.conversation_background(child), {"section": "artifact", "artifact_seq": artifact.seq})
        self.assertEqual(frozen["content"], "Frozen inherited version")
        self.assertNotIn(child, [entry["task_id"] for entry in self.history.index(self.target)["entries"]])


if __name__ == "__main__":
    unittest.main()
