"""Offline review handoffs through native tools and disposable fixture data."""

import hashlib
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from cater.workspace_tools import WorkspaceExecutor
from cater.work import WorkConflict
from cater.team_review_input import prepare
from cater.tool_protocol import ToolCall
import test_team_review_cycle as reviews
import test_team_external as external


class ReviewHandoffTests(unittest.TestCase):
    def fixture(self, *, workspace=False):
        f = reviews.TeamReviewCycleTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        if workspace:
            f.project = f.bind_workspace()
        return f

    def arguments(self, f, tool, source, *, reviewer=None, instruction="Check this version."):
        recipient = {"recipient" if tool == "delegate" else "reviewer": reviewer or f.reviewer}
        return {**recipient, "instruction": instruction,
                **({"review": source} if tool == "delegate" else source)}

    def exchange(self, f, tool, source, *, author=None, **kwargs):
        result, = f.exchange(author or f.worker, (tool, self.arguments(f, tool, source, **kwargs)))
        return result

    def assert_waiting(self, f, tool, result):
        self.assertEqual(result["status"], "ok", result)
        self.assertTrue(result["waiting_for_review"])
        request, = [e for e in f.events("team.review_requested") if e.seq == result["review_seq"]]
        assignment, = [e for e in f.events("team.message_received")
                       if e.seq == result["message_seq"]]
        self.assertEqual(assignment.payload["review_seq"], request.seq)
        self.assertEqual(assignment.payload["recipient"], request.payload["reviewer"])
        self.assertEqual(assignment.actor.id, request.payload["author"])
        receipt = [e for e in f.events("team.tool_finished") if e.payload["name"] == tool][-1]
        finished, = [e for e in f.events("team.run_finished")
                     if e.payload["run_id"] == receipt.payload["run_id"]]
        self.assertEqual(finished.payload["status"], "waiting")
        starts = [e for e in f.events("team.tool_started")
                  if e.payload["run_id"] == receipt.payload["run_id"]]
        self.assertEqual([e.payload["name"] for e in starts], [tool])
        self.assertTrue(f.rt.verify_chain().ok)
        return request, receipt

    def assert_no_publication(self, f, before):
        self.assertEqual(f.events("deliverable.published"), before[0])
        self.assertEqual(f.events("team.review_requested"), before[1])
        self.assertEqual(f.events("team.artifact_saved"), before[2])
        self.assertEqual(f.rt.task(f.task).deliverables, before[3])
        self.assertTrue(f.rt.verify_chain().ok)

    def before(self, f):
        return (f.events("deliverable.published"), f.events("team.review_requested"),
                f.events("team.artifact_saved"), f.rt.task(f.task).deliverables)

    def assert_source_rejected(self, f, result):
        self.assertEqual(result["status"], "error", result)
        self.assertEqual(result["code"], "invalid_review_source")
        self.assertIs(result["complete"], True)
        self.assertEqual(result["outcome"], "not_started")
        self.assertNotIn(str(f.root), repr(result))

    def test_published_artifacts_use_existing_review_flow_without_republication(self):
        for tool in ("delegate", "request_review"):
            with self.subTest(tool=tool):
                f = self.fixture()
                artifact = f.publish(content="Exact existing version.\n")
                published = f.events("deliverable.published")
                result = self.exchange(f, tool, {"artifact_seqs": [artifact]})
                request, _ = self.assert_waiting(f, tool, result)
                self.assertEqual(f.events("deliverable.published"), published)
                self.assertEqual(request.payload["author"], f.worker)
                self.assertEqual(request.payload["reviewer"], f.reviewer)
                self.assertEqual([a["seq"] for a in request.payload["artifacts"]], [artifact])

    def test_workspace_snapshot_one_call_binds_author_reviewer_bytes_and_source(self):
        for tool in ("delegate", "request_review"):
            for format in ("markdown", "code", "text"):
                for manager_author in (False, True):
                    with self.subTest(tool=tool, format=format, manager_author=manager_author):
                        f = self.fixture(workspace=True)
                        author = f.manager if manager_author else f.worker
                        body = "# Snapshot\r\n\u4e2d\u6587 content.\n".encode("utf-8")
                        (f.project / "result.md").write_bytes(body)
                        binding = f.rt.workspaces.view(f.task)
                        artifact_root = f.root / "unused-artifact-root"
                        f.rt._artifact_root = artifact_root
                        result = self.exchange(f, tool, {"workspace_file": {
                            "path": "result.md", "format": format}}, author=author)
                        request, receipt = self.assert_waiting(f, tool, result)
                        published, = f.events("deliverable.published")
                        self.assertEqual(published.actor.id, author)
                        self.assertEqual(request.payload["author"], author)
                        self.assertEqual(request.payload["reviewer"], f.reviewer)
                        digest = hashlib.sha256(body).hexdigest()
                        self.assertEqual(published.payload["content"].encode("utf-8"), body)
                        self.assertEqual(published.payload["content_sha256"], digest)
                        self.assertEqual(published.payload["format"], format)
                        self.assertEqual(request.payload["artifacts"], [{"seq": published.seq,
                            "filename": published.payload["filename"], "content_sha256": digest}])
                        self.assertEqual(published.payload["review_workspace_source"], {
                            "path": "result.md", "sha256": digest, "bytes": len(body),
                            "workspace_id": binding["workspace_id"],
                            "workspace_revision": binding["revision"],
                            "workspace_fingerprint": binding["fingerprint"],
                            "started_seq": receipt.payload["started_seq"]})
                        self.assertLess(published.seq, request.seq)
                        self.assertLess(request.seq, receipt.seq)
                        self.assertFalse(artifact_root.exists())
                        self.assertEqual(f.events("team.artifact_saved"), [])
                        (f.project / "result.md").write_text("Later disk version.\n", encoding="utf-8")
                        read, = f.exchange(f.reviewer, ("read_artifact", {"artifact_seq": published.seq}))
                        self.assertEqual(read["status"], "ok")
                        self.assertEqual(read["content"].encode("utf-8"), body)
                        self.assertEqual(read["content_sha256"], digest)

    def test_plain_delegate_keeps_instruction_and_creates_no_review(self):
        f = self.fixture(workspace=True)
        before = self.before(f)
        instruction = "Discuss request_review and record_review; inspect result.md if useful."
        result, = f.exchange(f.worker, ("delegate", {
            "recipient": f.reviewer, "instruction": instruction}))
        self.assertEqual(result["status"], "ok")
        self.assertNotIn("review_seq", result)
        self.assertNotIn("waiting_for_review", result)
        assignment, = [e for e in f.events("team.message_received")
                       if e.seq == result["message_seq"]]
        self.assertEqual(assignment.payload["text"], instruction)
        self.assertNotIn("review_seq", assignment.payload)
        self.assert_no_publication(f, before)

    def test_existing_artifact_can_be_coordinated_without_changing_publisher(self):
        for tool in ("delegate", "request_review"):
            with self.subTest(tool=tool):
                f = self.fixture()
                artifact = f.publish(author=f.worker)
                published = f.events("deliverable.published")
                saved = f.events("team.artifact_saved")
                result = self.exchange(f, tool, {"artifact_seqs": [artifact]}, author=f.manager)
                request, _ = self.assert_waiting(f, tool, result)
                self.assertEqual(request.payload["owner"], f.manager)
                self.assertEqual(request.payload["author"], f.manager)
                self.assertEqual(request.payload["publishers"], [f.worker])
                self.assertEqual([a["seq"] for a in request.payload["artifacts"]], [artifact])
                self.assertEqual(f.events("deliverable.published"), published)
                self.assertEqual(f.events("team.artifact_saved"), saved)

    def test_two_valid_sources_are_rejected_instead_of_selecting_one(self):
        for tool in ("delegate", "request_review"):
            with self.subTest(tool=tool):
                f = self.fixture(workspace=True)
                artifact = f.publish()
                (f.project / "result.md").write_text("Workspace version.\n", encoding="utf-8")
                before = self.before(f)
                result = self.exchange(f, tool, {"artifact_seqs": [artifact], "workspace_file": {
                    "path": "result.md", "format": "markdown"}})
                self.assert_source_rejected(f, result)
                self.assert_no_publication(f, before)

    def test_self_review_is_rejected_before_workspace_read(self):
        for tool in ("delegate", "request_review"):
            with self.subTest(tool=tool):
                f = self.fixture(workspace=True)
                (f.project / "result.md").write_text("Candidate.\n", encoding="utf-8")
                before = self.before(f)
                original = WorkspaceExecutor.execute
                reads = []

                def observe(executor, name, arguments):
                    if name == "workspace_read":
                        reads.append(arguments)
                    return original(executor, name, arguments)

                with patch.object(WorkspaceExecutor, "execute", observe):
                    result = self.exchange(f, tool, {"workspace_file": {
                        "path": "result.md", "format": "markdown"}}, reviewer=f.worker)
                self.assert_source_rejected(f, result)
                self.assertEqual(reads, [])
                self.assert_no_publication(f, before)

    def test_invalid_sources_and_extra_fields_publish_nothing(self):
        invalid = [
            {}, {"artifact_seqs": []}, {"artifact_seqs": [True]},
            {"artifact_seqs": [0]}, {"artifact_seqs": [99999999]},
            {"artifact_seqs": "not-an-array"}, {"workspace_file": None},
            {"workspace_file": {}}, {"workspace_file": {"path": "result.md"}},
            {"workspace_file": {"format": "markdown"}},
            {"workspace_file": {"path": "result.md", "format": "html"}},
            {"workspace_file": {"path": "result.md", "format": "markdown", "extra": True}},
            {"artifact_seqs": [99999999], "workspace_file": {"path": "result.md", "format": "markdown"}},
            {"workspace_file": {"path": "result.md", "format": "markdown"}, "extra": True},
            {"workspace_file": {"path": "result.md", "format": "markdown"}, "review_seq": True},
        ]
        for tool in ("delegate", "request_review"):
            for index, source in enumerate(invalid):
                with self.subTest(tool=tool, case=index):
                    f = self.fixture(workspace=True)
                    (f.project / "result.md").write_text("Candidate.\n", encoding="utf-8")
                    before = self.before(f)
                    result = self.exchange(f, tool, source)
                    self.assertEqual(result["status"], "error", result)
                    self.assert_no_publication(f, before)
        for invalid_review in (None, False, [], "review"):
            with self.subTest(review=invalid_review):
                f = self.fixture()
                before = self.before(f)
                result, = f.exchange(f.worker, ("delegate", {
                    "recipient": f.reviewer, "instruction": "Check candidate.", "review": invalid_review}))
                self.assertEqual(result["status"], "error", result)
                self.assert_no_publication(f, before)

    def test_missing_top_fields_extra_fields_and_empty_instruction_publish_nothing(self):
        for tool in ("delegate", "request_review"):
            recipient_key = "recipient" if tool == "delegate" else "reviewer"
            for kind in ("recipient", "instruction", "empty_instruction", "extra"):
                with self.subTest(tool=tool, kind=kind):
                    f = self.fixture(workspace=True)
                    (f.project / "result.md").write_text("Candidate.\n", encoding="utf-8")
                    args = self.arguments(f, tool, {"workspace_file": {
                        "path": "result.md", "format": "markdown"}})
                    if kind == "recipient":
                        del args[recipient_key]
                    elif kind == "instruction":
                        del args["instruction"]
                    elif kind == "empty_instruction":
                        args["instruction"] = "   "
                    else:
                        args["extra"] = True
                    before = self.before(f)
                    result, = f.exchange(f.worker, (tool, args))
                    self.assertEqual(result["status"], "error", result)
                    self.assert_no_publication(f, before)

    def test_workspace_source_requires_a_bound_workspace(self):
        for tool in ("delegate", "request_review"):
            with self.subTest(tool=tool):
                f = self.fixture()
                before = self.before(f)
                result = self.exchange(f, tool, {"workspace_file": {
                    "path": "result.md", "format": "markdown"}})
                self.assertEqual(result["status"], "error", result)
                self.assert_no_publication(f, before)

    def test_exact_64k_utf8_bytes_are_accepted_without_truncation(self):
        for tool in ("delegate", "request_review"):
            with self.subTest(tool=tool):
                f = self.fixture(workspace=True)
                body = ("\u4e2d" * 21845 + "x").encode("utf-8")
                self.assertEqual(len(body), 65536)
                (f.project / "result.md").write_bytes(body)
                result = self.exchange(f, tool, {"workspace_file": {
                    "path": "result.md", "format": "markdown"}})
                self.assert_waiting(f, tool, result)
                published, = f.events("deliverable.published")
                self.assertEqual(published.payload["content"].encode("utf-8"), body)
                self.assertEqual(published.payload["content_sha256"], hashlib.sha256(body).hexdigest())

    def test_missing_binary_oversize_and_outside_files_are_rejected(self):
        for tool in ("delegate", "request_review"):
            for kind in ("missing", "binary", "oversize", "parent", "absolute"):
                with self.subTest(tool=tool, kind=kind):
                    f = self.fixture(workspace=True)
                    path = "result.md"
                    if kind == "binary":
                        (f.project / path).write_bytes(b"\xff\x00\xfe")
                    elif kind == "oversize":
                        (f.project / path).write_bytes(b"x" * 65537)
                    elif kind in {"parent", "absolute"}:
                        outside = f.root / "outside.md"
                        outside.write_text("Outside workspace.\n", encoding="utf-8")
                        path = "../outside.md" if kind == "parent" else str(outside)
                    before = self.before(f)
                    result = self.exchange(f, tool, {"workspace_file": {"path": path, "format": "markdown"}})
                    self.assert_source_rejected(f, result)
                    self.assertNotIn(path, result["message"])
                    self.assert_no_publication(f, before)

    def test_truncated_read_is_not_published_as_a_complete_candidate(self):
        for tool in ("delegate", "request_review"):
            with self.subTest(tool=tool):
                f = self.fixture(workspace=True)
                (f.project / "result.md").write_text("Candidate.\n", encoding="utf-8")
                before = self.before(f)
                original = WorkspaceExecutor.execute
                reads = []

                def truncate(executor, name, arguments):
                    result = original(executor, name, arguments)
                    if name == "workspace_read":
                        reads.append(arguments)
                        return {**result, "truncated": True}
                    return result

                with patch.object(WorkspaceExecutor, "execute", truncate):
                    result = self.exchange(f, tool, {"workspace_file": {
                        "path": "result.md", "format": "markdown"}})
                self.assertEqual(len(reads), 1)
                self.assert_source_rejected(f, result)
                self.assert_no_publication(f, before)

    def test_snapshot_read_is_on_owner_thread_outside_log_transaction(self):
        for tool in ("delegate", "request_review"):
            with self.subTest(tool=tool):
                f = self.fixture(workspace=True)
                (f.project / "result.md").write_text("Snapshot.\n", encoding="utf-8")
                owner = threading.get_ident()
                original = WorkspaceExecutor.execute
                observed = []

                def observe(executor, name, arguments):
                    if name == "workspace_read":
                        observed.append((threading.get_ident(), f.rt.log.db.in_transaction))
                    return original(executor, name, arguments)

                with patch.object(WorkspaceExecutor, "execute", observe):
                    result = self.exchange(f, tool, {"workspace_file": {
                        "path": "result.md", "format": "markdown"}})
                self.assert_waiting(f, tool, result)
                self.assertEqual(observed, [(owner, False)])

    def test_failed_request_rolls_back_candidate_request_and_assignment(self):
        for tool in ("delegate", "request_review"):
            for returned_error in (False, True):
                with self.subTest(tool=tool, returned_error=returned_error):
                    f = self.fixture(workspace=True)
                    (f.project / "result.md").write_text("Rollback candidate.\n", encoding="utf-8")
                    before = self.before(f)
                    original = f.rt.team_reviews.request
                    observed = []

                    def reject(*args, **kwargs):
                        result = original(*args, **kwargs)
                        self.assertEqual(result["status"], "ok")
                        observed.append((f.rt.log.db.in_transaction,
                                         len(f.events("deliverable.published")),
                                         len(f.events("team.review_requested"))))
                        if returned_error:
                            return {"status": "error", "code": "offline_injected_rejection"}
                        raise WorkConflict("Offline injected request failure")

                    with patch.object(f.rt.team_reviews, "request", side_effect=reject):
                        result = self.exchange(f, tool, {"workspace_file": {
                            "path": "result.md", "format": "markdown"}})
                    self.assertEqual(observed, [(True, 1, 1)])
                    self.assertEqual(result["status"], "error", result)
                    self.assert_no_publication(f, before)
                    self.assertEqual([e for e in f.events("team.message_received")
                                      if e.payload.get("kind") == "assignment"], [])

    def test_workspace_revision_keeps_existing_review_chain(self):
        for tool in ("delegate", "request_review"):
            with self.subTest(tool=tool):
                f = self.fixture(workspace=True)
                target = f.project / "result.md"
                target.write_text("First version.\n", encoding="utf-8")
                source = {"workspace_file": {"path": "result.md", "format": "markdown"}}
                first = self.exchange(f, tool, source)
                self.assert_waiting(f, tool, first)
                self.assertEqual(f.record(first["review_seq"], "changes_requested")["status"], "ok")
                target.write_text("Revised version.\n", encoding="utf-8")
                second = self.exchange(f, tool, {**source, "review_seq": first["review_seq"]})
                request, _ = self.assert_waiting(f, tool, second)
                self.assertEqual(second["root_seq"], first["root_seq"])
                self.assertEqual(request.payload["previous_review_seq"], first["review_seq"])
                self.assertNotEqual(second["review_seq"], first["review_seq"])
                self.assertEqual(len(f.events("deliverable.published")), 2)

    def test_receipt_failure_rolls_back_candidate_and_review_in_same_transaction(self):
        for tool in ("delegate", "request_review"):
            with self.subTest(tool=tool):
                f = self.fixture(workspace=True)
                (f.project / "result.md").write_text("Atomic candidate.\n", encoding="utf-8")
                before = self.before(f)
                original = f.rt.log.emit
                observed = []

                def fail_receipt(*args, **kwargs):
                    payload = kwargs.get("payload", {})
                    if (kwargs.get("type") == "team.tool_finished" and payload.get("name") == tool
                            and payload.get("result", {}).get("status") == "ok"):
                        observed.append((f.rt.log.db.in_transaction,
                                         len(f.events("deliverable.published")),
                                         len(f.events("team.review_requested"))))
                        raise ValueError("Offline injected receipt failure")
                    return original(*args, **kwargs)

                with patch.object(f.rt.log, "emit", side_effect=fail_receipt):
                    result = self.exchange(f, tool, {"workspace_file": {
                        "path": "result.md", "format": "markdown"}})
                self.assertEqual(observed, [(True, 1, 1)])
                self.assertEqual(result["status"], "error", result)
                self.assert_no_publication(f, before)
                self.assertEqual([e for e in f.events("team.message_received")
                                  if e.payload.get("kind") == "assignment"], [])

    def test_external_typed_delegate_returns_review_receipt_and_yields_same_run(self):
        f = external.TeamExternalTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        def handoff(run):
            published = run.call("publish_artifact", filename="result.md", content="Candidate.\n",
                                 summary="Offline candidate", format="markdown")
            self.assertEqual(published["status"], "ok")
            run.call("delegate", recipient=f.reviewer, instruction="Check this version.",
                     review={"artifact_seqs": [published["artifact_seq"]]})
            return ""

        f.driver.scripts[f.manager] = [handoff]
        message = f.rt.team.receive(f.task, "Prepare a candidate for independent review.")
        # Execute just this input; the reviewer is deliberately not scripted.
        claimed = f.rt.team._claim(f.task)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed[0].seq, message.seq)
        list(f.rt.team._run_message(f.task, *claimed))
        self.assertEqual(len(f.driver.runs), 1)
        self.assertEqual(f.driver.errors, [])
        self.assertEqual([call.name for call, _ in f.driver.runs[0].receipts],
                         ["publish_artifact", "delegate"])
        receipts = [result for call, result in f.driver.runs[0].receipts if call.name == "delegate"]
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["status"], "ok")
        self.assertTrue(receipts[0]["waiting_for_review"])
        request, = f.events("team.review_requested")
        self.assertEqual(receipts[0]["review_seq"], request.seq)
        self.assertEqual(request.payload["author"], f.manager)
        self.assertEqual(request.payload["reviewer"], f.reviewer)
        finished, = f.events("team.run_finished")
        self.assertEqual(finished.payload["status"], "waiting")
        self.assertTrue(f.rt.verify_chain().ok)

    def test_native_review_handoff_stops_before_a_later_write_in_same_batch(self):
        for tool in ("delegate", "request_review"):
            with self.subTest(tool=tool):
                f = self.fixture(workspace=True)
                (f.project / "result.md").write_bytes(b"Original candidate.\n")
                args = self.arguments(f, tool, {"workspace_file": {"path": "result.md", "format": "markdown"}})
                f.driver.steps[f.manager] = [f.native_turn("", (tool, args), ("workspace_write", {
                    "path": "must-not-run.txt", "content": "Unexpected tail mutation", "expected_sha256": None}))]
                message = f.rt.team.receive(f.task, "Check a candidate; no tail execution.")
                claimed = f.rt.team._claim(f.task)
                self.assertEqual(claimed[0].seq, message.seq)
                list(f.rt.team._run_message(f.task, *claimed))
                self.assertEqual([e.payload["name"] for e in f.events("team.tool_started")], [tool])
                self.assertFalse((f.project / "must-not-run.txt").exists())
                self.assertEqual((f.project / "result.md").read_bytes(), b"Original candidate.\n")
                self.assertEqual(len(f.driver.calls), 1)
                self.assertEqual(f.events("team.run_finished")[-1].payload["status"], "waiting")
                self.assertEqual(len(f.events("team.review_requested")), 1)
                self.assertTrue(f.rt.verify_chain().ok)

    def test_live_workspace_owner_prevents_snapshot_read(self):
        f = self.fixture(workspace=True)
        (f.project / "result.md").write_bytes(b"Candidate.\n")
        before = self.before(f)
        with f.rt.workspaces._owner(str(f.project)) as owned:
            self.assertTrue(owned)
            with patch.object(WorkspaceExecutor, "execute", side_effect=AssertionError("Must not read under another owner")):
                result = self.exchange(f, "delegate", {"workspace_file": {"path": "result.md", "format": "markdown"}})
        self.assertEqual(result["status"], "error")
        self.assert_no_publication(f, before)

    def test_completed_goal_rejects_snapshot_before_reading_workspace(self):
        for tool in ("delegate", "request_review"):
            with self.subTest(tool=tool):
                f = self.fixture(workspace=True)
                (f.project / "result.md").write_bytes(b"Unsubmitted later file.\n")
                self.assertEqual(f.finish()["status"], "ok")
                before = self.before(f)
                with patch.object(WorkspaceExecutor, "execute", side_effect=AssertionError("Must reject before read")):
                    result = self.exchange(f, tool, {"workspace_file": {"path": "result.md", "format": "markdown"}})
                self.assertEqual(result["status"], "error")
                self.assert_no_publication(f, before)

    def test_paused_scope_guard_rejects_preparation_before_workspace_read(self):
        f = self.fixture(workspace=True)
        (f.project / "result.md").write_bytes(b"Candidate.\n")
        binding = f.rt.workspaces.view(f.task)
        role = f.rt.team._role(f.worker)
        source = f.rt.team.receive(f.task, "@" + role.name + " Check this candidate.")
        call = ToolCall("paused-read", "delegate", self.arguments(f, "delegate", {
            "workspace_file": {"path": "result.md", "format": "markdown"}}))
        started = SimpleNamespace(seq=source.seq, payload={"revision": f.rt.team.view(f.task)["revision"], "message_seq": source.seq})
        f.rt.execution_control.change(f.task, "pause", request_id="paused-review-source-guard")
        workspace = WorkspaceExecutor(f.rt.workspaces.config["supervision"]["policy"])
        before = self.before(f)
        with patch.object(WorkspaceExecutor, "execute", side_effect=AssertionError("Must reject before read")):
            with self.assertRaisesRegex(WorkConflict, "暂停"):
                with prepare(f.rt.team, f.task, role, call, workspace, binding, started):
                    self.fail("Paused source preparation must not yield")
        self.assert_no_publication(f, before)

    def test_download_unsafe_unicode_names_are_rejected_before_publication(self):
        for tool in ("delegate", "request_review"):
            for name in ("\uff23\uff2f\uff2e.txt", "note\u200b.md"):
                with self.subTest(tool=tool, name=repr(name)):
                    f = self.fixture(workspace=True)
                    (f.project / name).write_bytes(b"Candidate.\n")
                    before = self.before(f)
                    result = self.exchange(f, tool, {"workspace_file": {"path": name, "format": "text"}})
                    self.assertEqual(result["status"], "error")
                    self.assert_no_publication(f, before)

    def test_binding_change_after_read_rejects_snapshot_before_publication(self):
        f = self.fixture(workspace=True)
        (f.project / "result.md").write_bytes(b"Candidate.\n")
        before = self.before(f)
        original_execute, original_view = WorkspaceExecutor.execute, f.rt.workspaces.view
        read_finished = []
        def execute(executor, name, arguments):
            result = original_execute(executor, name, arguments)
            if name == "workspace_read":
                read_finished.append(True)
            return result
        def view(task_id):
            result = original_view(task_id)
            return {**result, "revision": result["revision"] + 1} if read_finished and task_id == f.task else result
        with patch.object(WorkspaceExecutor, "execute", execute), patch.object(f.rt.workspaces, "view", side_effect=view):
            result, = f.exchange(f.worker, ("request_review", self.arguments(f, "request_review", {
                "workspace_file": {"path": "result.md", "format": "markdown"}})), expected_statuses=("interrupted",))
        self.assertEqual(result["status"], "error")
        self.assertEqual(len(read_finished), 1)
        self.assert_no_publication(f, before)

    def test_native_record_review_stops_before_a_later_write_in_same_batch(self):
        f = self.fixture(workspace=True)
        artifact = f.publish()
        request = f.request([artifact])
        role = f.rt.team._role(f.reviewer)
        f.driver.steps[f.reviewer] = [f.native_turn("", ("record_review", {
            "review_seq": request["review_seq"], "verdict": "approved", "feedback": "Offline fixture only."}),
            ("workspace_write", {"path": "must-not-run.txt", "content": "Unexpected tail", "expected_sha256": None}))]
        source = f.rt.team.receive(f.task, "@" + role.name + " Record the reviewed fixture version.")
        claimed = f.rt.team._claim(f.task)
        self.assertEqual(claimed[0].seq, source.seq)
        list(f.rt.team._run_message(f.task, *claimed))
        run_id = claimed[2].payload["run_id"]
        self.assertEqual([e.payload["name"] for e in f.events("team.tool_started")
            if e.payload["run_id"] == run_id], ["record_review"])
        self.assertFalse((f.project / "must-not-run.txt").exists())
        self.assertEqual(f.rt.team_reviews.view(f.task)[0]["status"], "approved")
        self.assertTrue(f.rt.verify_chain().ok)


if __name__ == "__main__":
    unittest.main()
