"""Coordinator contract regressions with native fake turns and disposable data.

Compose existing fixtures without inheriting or collecting their test methods.
Review verdicts below are controlled protocol evidence, not live-model QA.
"""

import copy
import hashlib
from pathlib import Path
import unittest

from cater.events import Actor
from cater.runtime import Runtime
from cater.skills import SkillLibrary
from cater.team_archive import schema as archive_schema
import test_review_resubmission_owner as owners
import test_team_review_cycle as cycle


class ReviewCoordinatorTests(unittest.TestCase):
    def fixture(self):
        flow = owners.ReviewResubmissionOwnerTests()
        self.addCleanup(flow.doCleanups)
        flow.setUp()
        return flow

    def submit(self, flow, owner, seqs, *, tool="request_review", reviewer=None, previous=None):
        f = flow.f
        if tool == "request_review":
            return f.request(seqs, requester=owner, reviewer=reviewer,
                             review_seq=previous["review_seq"] if previous else None)
        source = {"artifact_seqs": seqs}
        if previous:
            source["review_seq"] = previous["review_seq"]
        result, = f.exchange(owner, ("delegate", {
            "recipient": reviewer or f.reviewer, "instruction": "Inspect the exact selected files.",
            "review": source}))
        return result

    def assert_request(self, flow, result, owner, seqs, publishers):
        f = flow.f
        flow.ok(result)
        request = flow.event(result["review_seq"])
        self.assertEqual(request.actor, cycle.HOST)
        self.assertEqual({k: request.payload[k] for k in ("owner", "author", "requested_by")},
                         dict.fromkeys(("owner", "author", "requested_by"), owner))
        self.assertEqual(request.payload["publishers"], sorted(set(publishers)))
        self.assertEqual(request.payload["artifacts"], flow.refs(*seqs))
        self.assertEqual(request.payload["reviewer"], f.reviewer)
        assignment = flow.event(result["message_seq"])
        self.assertEqual(assignment.actor.id, owner)
        self.assertEqual((assignment.payload["kind"], assignment.payload["recipient"],
                          assignment.payload["review_seq"]),
                         ("assignment", f.reviewer, request.seq))
        row = next(r for r in f.rt.team_reviews.view(f.task) if r["request_seq"] == request.seq)
        self.assertEqual(f.rt.team_reviews._request_message(f.task, row), assignment)
        self.assertTrue(f.rt.verify_chain().ok)
        return request

    def inspect_and_decide(self, flow, request, owner, seqs, verdict, feedback):
        f = flow.f
        calls = [("read_artifact", {"artifact_seq": seq}) for seq in seqs]
        calls.append(("record_review", {"review_seq": request["review_seq"],
                                       "verdict": verdict, "feedback": feedback}))
        results = f.exchange(f.reviewer, *calls, source=flow.event(request["message_seq"]))
        artifacts = {a["seq"]: a for a in f.rt.task(f.task).deliverables}
        for seq, result in zip(seqs, results[:-1]):
            flow.ok(result)
            self.assertEqual(result["content"], artifacts[seq]["content"])
            self.assertEqual(result["content_sha256"],
                             hashlib.sha256(result["content"].encode("utf-8")).hexdigest())
        decision = flow.ok(results[-1])
        reports = [e for e in f.rt.team._messages(f.task)
                   if e.payload.get("kind") == "report"
                   and e.payload.get("review_seq") == request["review_seq"]]
        report, = reports
        self.assertEqual(report.seq, decision["message_seq"])
        self.assertEqual(report.actor.id, f.reviewer)
        self.assertEqual((report.payload["recipient"], report.payload["reply_to"]),
                         (owner, request["message_seq"]))
        self.assertIn(feedback, report.payload["text"])
        f.exchange(owner, source=report)
        return decision

    def test_current_members_submit_partner_versions_through_both_tools_without_republishing(self):
        for tool in ("request_review", "delegate"):
            for owner_kind in ("manager", "observer", "worker"):
                with self.subTest(tool=tool, owner=owner_kind):
                    flow = self.fixture()
                    f = flow.f
                    artifact = f.publish("Partner's actual version.\n")
                    publications = f.events("deliverable.published")
                    result = self.submit(flow, getattr(f, owner_kind), [artifact], tool=tool)
                    self.assert_request(flow, result, getattr(f, owner_kind), [artifact], [f.worker])
                    self.assertEqual(f.events("deliverable.published"), publications)
                    self.assertEqual(f.events("team.review_recorded"), [])
                    self.assertEqual(f.events("team.objective_completed"), [])
                    self.assertTrue(result["waiting_for_review"])

    def test_multiple_publishers_are_sorted_deduplicated_and_only_cover_selected_actors(self):
        for tool in ("request_review", "delegate"):
            with self.subTest(tool=tool):
                flow = self.fixture()
                f = flow.f
                worker = f.publish(author=f.worker, filename="worker.txt")
                manager = f.publish(author=f.manager, filename="manager.txt")
                another = f.publish(author=f.worker, filename="another.txt")
                f.publish(author=f.reviewer, filename="unselected.txt")
                publications = f.events("deliverable.published")
                selected = [another, manager, worker]
                result = self.submit(flow, f.observer, selected, tool=tool)
                self.assert_request(flow, result, f.observer, selected, [f.worker, f.manager])
                self.assertEqual(f.events("deliverable.published"), publications)

    def test_owner_and_every_selected_publisher_are_rejected_as_reviewer(self):
        flow = self.fixture()
        f = flow.f
        worker = f.publish(author=f.worker, filename="worker.txt")
        manager = f.publish(author=f.manager, filename="manager.txt")
        for tool in ("request_review", "delegate"):
            for reviewer in (f.observer, f.worker, f.manager):
                with self.subTest(tool=tool, reviewer=reviewer):
                    flow.reject(lambda: self.submit(flow, f.observer, [worker, manager],
                                                   tool=tool, reviewer=reviewer),
                                code="reviewer_mismatch")
            for owner, reviewer in ((f.manager, f.manager), (f.manager, f.worker), (f.worker, f.worker)):
                with self.subTest(tool=tool, owner=owner, reviewer=reviewer, single=True):
                    flow.reject(lambda: self.submit(flow, owner, [worker], tool=tool, reviewer=reviewer),
                                code="reviewer_mismatch")
        self.assertEqual(f.events("team.review_requested"), [])

    def test_two_simulated_reworks_keep_owner_chain_messages_and_exact_final_zip(self):
        flow = self.fixture()
        f = flow.f
        previous = None
        snapshots = []
        rounds = []
        for index, verdict in enumerate(("changes_requested", "changes_requested", "approved")):
            with self.subTest(round=index + 1):
                implementation = f.publish(f"Implementation round {index + 1}\n", author=f.worker,
                                           filename=f"implementation-{index + 1}.txt")
                notes = f.publish(f"Usage round {index + 1}\n", author=f.observer,
                                 filename=f"usage-{index + 1}.txt")
                seqs = [implementation, notes]
                publications = f.events("deliverable.published")
                result = self.submit(flow, f.manager, seqs,
                                     tool="delegate" if index == 1 else "request_review", previous=previous)
                request = self.assert_request(flow, result, f.manager, seqs, [f.worker, f.observer])
                self.assertEqual(f.events("deliverable.published"), publications)
                self.assertEqual(f.finish().get("code"), "review_pending")
                if previous:
                    self.assertEqual(result["root_seq"], previous["root_seq"])
                    self.assertEqual(request.payload["previous_review_seq"], previous["review_seq"])
                    self.assertEqual(request.payload["previous_artifacts"], flow.refs(*rounds[-1]))
                    flow.reject(lambda: self.submit(flow, f.worker, seqs, previous=result),
                                code="review_author_mismatch", expected=("author", f.manager))
                    flow.reject(lambda: self.submit(flow, f.manager, seqs, previous=previous))
                for signer in (f.manager, f.worker, f.observer):
                    flow.reject(lambda: f.record(result["review_seq"], reviewer=signer),
                                code="reviewer_mismatch", expected=("reviewer", f.reviewer))
                self.inspect_and_decide(flow, result, f.manager, seqs, verdict,
                                        f"Simulated fixture inspection, round {index + 1}: {verdict}.")
                if previous:
                    flow.reject(lambda: f.record(previous["review_seq"]))
                snapshots.append(copy.deepcopy(f.rt.log.read()))
                rounds.append(seqs)
                previous = result
        for snapshot in snapshots:
            self.assertEqual(f.rt.log.read()[:len(snapshot)], snapshot)
        self.assertEqual([e.payload["verdict"] for e in f.events("team.review_recorded")],
                         ["changes_requested", "changes_requested", "approved"])
        self.assertEqual(f.rt.team_reviews.superseded_artifacts(f.task), set(rounds[0] + rounds[1]))
        flow.ok(f.finish())
        flow.assert_delivery(*rounds[-1])
        self.assertEqual(f.rt.team.view(f.task)["status"], "done")
        self.assertEqual(f.rt.team.view(f.task)["pending_count"], 0)
        f.assert_one_run_per_role()
        completed, = f.events("team.objective_completed")
        self.assertLess(f.events("team.review_recorded")[-1].seq, completed.seq)
        frozen = copy.deepcopy(f.rt.log.read())
        with Runtime(f.root / "test.db", roles_path=f.roles, drivers={"team_test": f.driver}) as reopened:
            self.assertEqual(reopened.log.read(), frozen)
            self.assertEqual(reopened.team_reviews.view(f.task), f.rt.team_reviews.view(f.task))
            historical = reopened.team_reviews.read(f.task, f.events("team.review_requested")[0].seq)
            self.assertEqual(historical["artifacts"], flow.refs(*rounds[0]))
            self.assertEqual(historical["historical_status"], "changes_requested")
            self.assertTrue(reopened.verify_chain().ok)
        self.assertEqual(f.rt.log.read(), frozen)
        self.assertEqual(len(f.events("deliverable.published")), 6)

    def test_approved_partner_waiting_without_formal_return_cannot_be_delivered(self):
        flow = self.fixture()
        f = flow.f
        delegated, = f.exchange(f.manager, ("delegate", {
            "recipient": f.worker, "instruction": "Publish the file and formally return this assignment."}))
        flow.ok(delegated)
        parent = flow.event(delegated["message_seq"])
        published, = f.exchange(f.worker, ("publish_artifact", {
            "filename": "result.txt", "content": "Published, but assignment still waiting.\n",
            "summary": "Waiting publisher fixture", "format": "text"}), source=parent)
        artifact = flow.ok(published)["artifact_seq"]
        requested = self.submit(flow, f.observer, [artifact])
        self.assert_request(flow, requested, f.observer, [artifact], [f.worker])
        self.inspect_and_decide(flow, requested, f.observer, [artifact], "approved", "Fixture bytes inspected.")
        assignment, = [r for r in f.rt.team.recovery.assignments(f.task) if r["seq"] == parent.seq]
        self.assertEqual(assignment["status"], "waiting")
        self.assertFalse(assignment["requires_return"])
        revision = f.rt.team.view(f.task)["revision"]
        blocked = flow.reject(f.finish)
        self.assertEqual(blocked.get("code"), "author_pending", blocked)
        self.assertIn({"seq": parent.seq, "author": f.worker}, blocked["assignments"])
        self.assertEqual(f.rt.team.view(f.task)["revision"], revision)
        self.assertEqual(f.rt.team.view(f.task)["status"], "running")
        notice, = f.exchange(f.worker, ("send_message", {
            "recipient": f.manager, "text": "Ready for delivery.", "notify_only": True}))
        flow.ok(notice)
        self.assertEqual(flow.reject(f.finish).get("code"), "author_pending")
        returned, = f.exchange(f.worker, ("send_message", {
            "recipient": f.manager, "reply_to": parent.seq, "text": "Final reviewed file formally returned."}))
        flow.ok(returned)
        f.exchange(f.manager, source=flow.event(returned["message_seq"]))
        flow.ok(f.finish())
        flow.assert_delivery(artifact)

    def test_legacy_author_is_chain_owner_without_rewriting_old_events(self):
        flow = self.fixture()
        f = flow.f
        artifact = f.publish(author=f.worker)
        legacy = f.rt.log.emit(type="team.review_requested", actor=cycle.HOST, task_id=f.task, payload={
            "root_seq": None, "author": f.observer, "requested_by": f.observer,
            "reviewer": f.reviewer, "artifacts": flow.refs(artifact),
            "revision": f.rt.team.view(f.task)["revision"], "instruction": "Legacy independent inspection.",
            "run_id": "legacy-coordinator"})
        assignment = f.rt.team._send(f.task, f.rt.team._role(f.observer), f.reviewer,
                                    "Inspect the legacy file.", "assignment", review_seq=legacy.seq)
        first = {"review_seq": legacy.seq, "root_seq": legacy.seq, "message_seq": assignment.seq}
        self.inspect_and_decide(flow, first, f.observer, [artifact], "changes_requested", "Revise fixture bytes.")
        frozen = copy.deepcopy(f.rt.log.read())
        revised = f.publish("Partner revision of legacy chain.\n", author=f.worker)
        for contender in (f.manager, f.worker, f.reviewer):
            flow.reject(lambda: self.submit(flow, contender, [revised], previous=first),
                        code="review_author_mismatch", expected=("author", f.observer))
        flow.reject(lambda: self.submit(flow, f.observer, [revised], previous=first, reviewer=f.manager),
                    code="reviewer_mismatch", expected=("reviewer", f.reviewer))
        second = self.submit(flow, f.observer, [revised], previous=first)
        self.assert_request(flow, second, f.observer, [revised], [f.worker])
        self.assertEqual(second["root_seq"], legacy.seq)
        self.assertEqual(f.rt.log.read()[:len(frozen)], frozen)
        self.assertNotIn("owner", flow.event(legacy.seq).payload)
        self.assertNotIn("publishers", flow.event(legacy.seq).payload)
        self.inspect_and_decide(flow, second, f.observer, [revised], "approved", "Legacy revision checked.")
        flow.ok(f.finish())
        flow.assert_delivery(revised)

    def test_replacing_publisher_keeps_original_work_pending_until_report_or_withdrawal_after_reopen(self):
        for resolution in ("report", "withdraw"):
            with self.subTest(resolution=resolution):
                flow = self.fixture()
                f = flow.f
                delegated, = f.exchange(f.manager, ("delegate", {
                    "recipient": f.worker, "instruction": "Publish a candidate and formally return this assignment."}))
                parent = flow.event(flow.ok(delegated)["message_seq"])
                published, = f.exchange(f.worker, ("publish_artifact", {
                    "filename": "result.txt", "content": "Original publisher's candidate.\n",
                    "summary": "Original work still waiting", "format": "text"}), source=parent)
                original = flow.ok(published)["artifact_seq"]
                self.assertEqual([e.actor.id for e in f.events("deliverable.published")], [f.worker])
                first = self.submit(flow, f.manager, [original])
                self.assert_request(flow, first, f.manager, [original], [f.worker])
                self.inspect_and_decide(flow, first, f.manager, [original], "changes_requested", "Revise the candidate.")
                replacement = f.publish("Owner's independently reviewed replacement.\n", author=f.manager)
                second = self.submit(flow, f.manager, [replacement], previous=first)
                self.assert_request(flow, second, f.manager, [replacement], [f.manager])
                self.assertEqual(second["root_seq"], first["root_seq"])
                self.inspect_and_decide(flow, second, f.manager, [replacement], "approved", "Replacement bytes inspected.")
                head, = f.rt.team_reviews.view(f.task)
                self.assertEqual((head["status"], head["artifacts"], head["publishers"]),
                                 ("approved", flow.refs(replacement), [f.manager]))
                self.assertEqual(f.rt.team_reviews.superseded_artifacts(f.task), {original})
                requests = copy.deepcopy(f.events("team.review_requested"))
                revision = f.rt.team.view(f.task)["revision"]

                def assert_original_work_blocks_delivery():
                    assignments = f.rt.team.recovery.assignments(f.task)
                    row, = [r for r in assignments if r["seq"] == parent.seq]
                    self.assertEqual(row["status"], "waiting")
                    self.assertFalse(row["requires_return"])
                    self.assertFalse(row["has_delegated_work"])
                    self.assertFalse([r for r in assignments
                                      if r["status"] in {"running", "queued"} or r["requires_return"]])
                    self.assertFalse([r for r in f.rt.team.view(f.task)["runs"] if r["status"] == "running"])
                    blocked = flow.reject(f.finish)
                    self.assertEqual(blocked.get("code"), "author_pending", blocked)
                    self.assertEqual(blocked["assignments"], [{"seq": parent.seq, "author": f.worker}])
                    self.assertEqual(f.rt.team.view(f.task)["revision"], revision)

                assert_original_work_blocks_delivery()
                frozen = copy.deepcopy(f.rt.log.read())
                previous_runtime = f.rt
                with Runtime(f.root / "test.db", roles_path=f.roles, drivers={"team_test": f.driver}) as reopened:
                    f.rt = reopened
                    try:
                        self.assertEqual(f.rt.log.read(), frozen)
                        assert_original_work_blocks_delivery()
                        if resolution == "report":
                            returned, = f.exchange(f.worker, ("send_message", {
                                "recipient": f.manager, "reply_to": parent.seq,
                                "text": "Original work and remaining responsibility formally returned."}))
                            report = flow.event(flow.ok(returned)["message_seq"])
                            self.assertEqual(report.actor.id, f.worker)
                            self.assertEqual((report.payload["kind"], report.payload["reply_to"]),
                                             ("report", parent.seq))
                            f.exchange(f.manager, source=report)
                            self.assertEqual(f.events("team.assignment_withdrawn"), [])
                        else:
                            withdrawn, = f.exchange(f.manager, ("withdraw_work", {
                                "message_seq": parent.seq,
                                "reason": "Owner's approved replacement makes the original assignment unnecessary."}))
                            self.assertTrue(flow.ok(withdrawn)["withdrawn"])
                            self.assertIn(parent.seq, f.rt.team.work_control.inactive(f.task))
                            self.assertFalse([e for e in f.rt.team._messages(f.task)
                                              if e.payload.get("kind") == "report"
                                              and e.payload.get("reply_to") == parent.seq])
                        self.assertNotIn(parent.seq, {r["seq"] for r in f.rt.team.recovery.assignments(f.task)})
                        flow.ok(f.finish())
                        flow.assert_delivery(replacement)
                        self.assertEqual(f.rt.team.view(f.task)["status"], "done")
                        self.assertEqual(f.events("team.review_requested"), requests)
                        self.assertEqual(f.rt.log.read()[:len(frozen)], frozen)
                        self.assertEqual([e.actor.id for e in f.events("deliverable.published")],
                                         [f.worker, f.manager])
                    finally:
                        f.rt = previous_runtime

    def test_legacy_partner_revision_waits_for_each_publisher_to_return_before_delivery(self):
        flow = self.fixture()
        f = flow.f

        def legacy_request(seqs, previous=None):
            payload = {
                "root_seq": previous["root_seq"] if previous else None,
                "author": f.manager, "requested_by": f.manager, "reviewer": f.reviewer,
                "artifacts": flow.refs(*seqs), "revision": f.rt.team.view(f.task)["revision"],
                "instruction": "Inspect this exact legacy submission.", "run_id": "legacy-publisher-return",
            }
            if previous:
                payload.update(previous_review_seq=previous["review_seq"],
                               previous_artifacts=flow.event(previous["review_seq"]).payload["artifacts"])
            request = f.rt.log.emit(type="team.review_requested", actor=cycle.HOST,
                                    task_id=f.task, payload=payload)
            message = f.rt.team._send(f.task, f.rt.team._role(f.manager), f.reviewer,
                                      "Inspect the exact legacy submission.", "assignment",
                                      visible=True, review_seq=request.seq)
            return {"review_seq": request.seq, "root_seq": previous["root_seq"] if previous else request.seq,
                    "message_seq": message.seq}

        draft = f.publish("Owner's original draft.\n", author=f.manager, filename="draft.txt")
        first = legacy_request([draft])
        self.inspect_and_decide(flow, first, f.manager, [draft], "changes_requested", "Split and correct the draft.")
        parents = {}
        corrected = []
        for publisher, filename in ((f.worker, "implementation.txt"), (f.observer, "usage.txt")):
            delegated, = f.exchange(f.manager, ("delegate", {
                "recipient": publisher, "instruction": "Correct your part and formally return this assignment."}))
            parent = flow.event(flow.ok(delegated)["message_seq"])
            parents[publisher] = parent
            published, = f.exchange(publisher, ("publish_artifact", {
                "filename": filename, "content": f"Corrected {filename}\n",
                "summary": "Legacy partner correction", "format": "text"}), source=parent)
            corrected.append(flow.ok(published)["artifact_seq"])
        second = legacy_request(corrected, previous=first)
        self.assertEqual(second["root_seq"], first["root_seq"])
        self.inspect_and_decide(flow, second, f.manager, corrected, "approved", "Corrected fixture bytes inspected.")
        head, = f.rt.team_reviews.view(f.task)
        self.assertEqual((head["author"], head["status"], head["artifacts"]),
                         (f.manager, "approved", flow.refs(*corrected)))
        self.assertNotIn("owner", head)
        self.assertNotIn("publishers", head)
        requests = copy.deepcopy(f.events("team.review_requested"))
        for request in requests:
            self.assertNotIn("owner", request.payload)
            self.assertNotIn("publishers", request.payload)
        frozen = copy.deepcopy(f.rt.log.read())
        revision = f.rt.team.view(f.task)["revision"]
        remaining = dict(parents)
        for publisher, parent in parents.items():
            assignments = {r["seq"]: r for r in f.rt.team.recovery.assignments(f.task)}
            for waiting in remaining.values():
                row = assignments[waiting.seq]
                self.assertEqual(row["status"], "waiting")
                self.assertFalse(row["has_delegated_work"])
                self.assertFalse(row["requires_return"])
            self.assertFalse([r for r in assignments.values()
                              if r["status"] in {"running", "queued"} or r["requires_return"]])
            self.assertFalse([r for r in f.rt.team.view(f.task)["runs"] if r["status"] == "running"])
            blocked = flow.reject(f.finish)
            self.assertEqual(blocked.get("code"), "author_pending", blocked)
            self.assertEqual({(r["seq"], r["author"]) for r in blocked["assignments"]},
                             {(p.seq, actor) for actor, p in remaining.items()})
            self.assertEqual(f.rt.team.view(f.task)["revision"], revision)
            returned, = f.exchange(publisher, ("send_message", {
                "recipient": f.manager, "reply_to": parent.seq, "text": "Corrected reviewed part formally returned."}))
            report = flow.event(flow.ok(returned)["message_seq"])
            self.assertEqual(report.actor.id, publisher)
            self.assertEqual((report.payload["kind"], report.payload["reply_to"]), ("report", parent.seq))
            f.exchange(f.manager, source=report)
            del remaining[publisher]
        self.assertEqual(f.rt.team_reviews.superseded_artifacts(f.task), {draft})
        flow.ok(f.finish())
        flow.assert_delivery(*corrected)
        self.assertEqual(f.events("team.review_requested"), requests)
        self.assertEqual(f.rt.log.read()[:len(frozen)], frozen)
        self.assertEqual(len(f.events("deliverable.published")), 3)

    def test_loaded_review_skill_and_archive_description_explain_coordinator_contract(self):
        library = SkillLibrary()
        scope = {"role_id": "store_manager", "workspace_id": None,
                 "tools": {"request_review", "read_artifact"}}
        index = library.index(**scope)
        entry, = [s for s in index["skills"] if s["id"] == "versioned-review"]
        response = library.read({"skill_id": entry["id"], "expected_sha256": entry["sha256"]}, **scope)
        self.assertEqual(response["status"], "ok", response)
        self.assertFalse(response["has_more"])
        self.assertEqual(response["skill"]["source"], "builtin")
        content = response["content"]
        for term in ("request_review", "owner", "author", "publishers", "record_review", "artifact_seqs",
                     "\u9001\u5ba1\u8d1f\u8d23\u4eba", "\u591a\u4f4d\u4f19\u4f34", "\u4e0d\u5fc5\u91cd\u65b0\u53d1\u5e03",
                     "\u539freview_seq", "\u540c\u4e00\u590d\u6838\u8005"):
            with self.subTest(term=term):
                self.assertIn(term, content)
        self.assertIn("\u8d1f\u8d23\u4eba\u53ca\u672c\u7248\u4efb\u4f55\u53d1\u5e03\u8005"
                      "\u90fd\u4e0d\u80fd\u81ea\u884c\u590d\u6838", content)
        obsolete = "\u9996\u6b21\u7531\u6210\u679c\u53d1\u5e03\u8005"
        self.assertNotIn(obsolete, content)
        description = archive_schema()["description"]
        for term in ("request_review", "finish_work", "\u9001\u5ba1\u8d1f\u8d23\u4eba",
                     "ZIP\u53d1\u5e03\u4e0d\u662f\u6700\u7ec8\u4ea4\u4ed8", "\u4e0d\u4ee3\u8868\u9a8c\u8bc1\u901a\u8fc7"):
            self.assertIn(term, description)
        self.assertNotIn(obsolete, description)

    def test_mixed_selection_still_rejects_unknown_publishers(self):
        flow = self.fixture()
        f = flow.f
        worker = f.publish(author=f.worker, filename="worker.txt")
        manager = f.publish(author=f.manager, filename="manager.txt")
        invalid = f.rt.log.emit(type="deliverable.published", task_id=f.task,
            actor=Actor.agent("unknown-partner", "developer"),
            payload={**flow.event(worker).payload, "filename": "unknown.txt"})
        for tool in ("request_review", "delegate"):
            with self.subTest(tool=tool):
                flow.reject(lambda: self.submit(flow, f.observer, [worker, manager, invalid.seq], tool=tool))
        self.assertEqual(f.events("team.review_requested"), [])
        valid = self.submit(flow, f.observer, [worker, manager])
        self.assert_request(flow, valid, f.observer, [worker, manager], [f.worker, f.manager])

    def test_saving_and_ready_messages_do_not_record_approval_or_delivery(self):
        flow = self.fixture()
        f = flow.f
        f.rt._artifact_root = f.root / "saved-artifacts"
        artifact = f.publish("Saved bytes are not a delivery.\n")
        saved, = f.events("team.artifact_saved")
        self.assertEqual(Path(saved.payload["path"]).read_bytes(), b"Saved bytes are not a delivery.\n")
        self.assertEqual(saved.payload["artifact_seq"], artifact)
        self.assertEqual(f.events("team.objective_completed"), [])
        request = self.submit(flow, f.manager, [artifact])
        self.assert_request(flow, request, f.manager, [artifact], [f.worker])
        notice, = f.exchange(f.worker, ("send_message", {
            "recipient": f.manager, "text": "Saved and approved; ready to deliver.", "notify_only": True}))
        flow.ok(notice)
        self.assertEqual(f.events("team.review_recorded"), [])
        self.assertEqual(f.events("team.objective_completed"), [])
        self.assertEqual(flow.reject(f.finish).get("code"), "review_pending")
        self.inspect_and_decide(flow, request, f.manager, [artifact], "approved", "Controlled fixture byte check.")
        self.assertEqual(f.events("team.objective_completed"), [])
        flow.ok(f.finish())
        flow.assert_delivery(artifact)
        self.assertEqual(f.events("team.artifact_saved"), [saved])

    def test_owner_and_publishers_remain_host_facts_not_model_arguments(self):
        flow = self.fixture()
        f = flow.f
        artifact = f.publish()
        schemas = {t["name"]: t["input_schema"] for t in f.driver.calls[-1]["tools"]}
        for key in ("owner", "author", "requested_by", "publishers"):
            self.assertNotIn(key, schemas["request_review"]["properties"])
            self.assertNotIn(key, schemas["delegate"]["properties"]["review"]["properties"])
            for tool in ("request_review", "delegate"):
                with self.subTest(tool=tool, injected=key):
                    source = {"artifact_seqs": [artifact], key: [f.manager] if key == "publishers" else f.worker}
                    arguments = ({"reviewer": f.reviewer, "instruction": "Forged attribution.", **source}
                                 if tool == "request_review" else
                                 {"recipient": f.reviewer, "instruction": "Forged attribution.", "review": source})
                    flow.reject(lambda: f.exchange(f.manager, (tool, arguments))[0])
        self.assertEqual(f.events("team.review_requested"), [])


if __name__ == "__main__":
    unittest.main()
