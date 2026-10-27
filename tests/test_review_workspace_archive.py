"""Offline native review handoffs with real files, ZIP bytes and transactions."""
from contextlib import ExitStack
import hashlib
import io
import itertools
import json
import os
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from cater import team_review_input as inputs
from cater import workspace_archive as archives
from cater.artifact_blobs import put_blob, read_blob
from cater.events import Actor
from cater.tool_protocol import ToolCall
from cater.work import WorkConflict
from cater.workspaces import WorkspaceService
from cater.workspace_tools import WorkspaceExecutor
import test_review_handoff as handoffs


class ReviewWorkspaceArchiveTests(unittest.TestCase):
    fixture = handoffs.ReviewHandoffTests.fixture
    arguments = handoffs.ReviewHandoffTests.arguments
    exchange = handoffs.ReviewHandoffTests.exchange
    assert_waiting = handoffs.ReviewHandoffTests.assert_waiting
    assert_source_rejected = handoffs.ReviewHandoffTests.assert_source_rejected

    def write(self, f, files=None):
        files = files or {"src/main.py": b"print('snapshot version')\r\n",
                          "src/empty.txt": b"", "assets/data.bin": bytes(range(256)),
                          "docs/nested/readme.md": "工程快照\n".encode()}
        for name, body in files.items():
            path = f.project / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
        return files

    def source(self, paths=("src/main.py",), filename="project.zip", **extra):
        return {"workspace_archive": {"filename": filename, "paths": list(paths)}, **extra}

    def state(self, f):
        schema = f.rt.log.db.execute("SELECT type,name,sql FROM sqlite_master WHERE name LIKE '%artifact_blobs%' ORDER BY name").fetchall()
        blobs = f.rt.log.db.execute("SELECT sha256,content FROM artifact_blobs ORDER BY sha256").fetchall() if schema else []
        return (f.events("deliverable.published"), f.events("team.review_requested"),
                f.events("team.artifact_saved"), f.rt.task(f.task).deliverables,
                [e for e in f.events("team.message_received") if e.payload.get("kind") in {"assignment", "notification", "report"}],
                schema, blobs)

    def unchanged(self, f, before):
        self.assertEqual(self.state(f), before)
        self.assertTrue(f.rt.verify_chain().ok)

    def policy(self, f, mode):
        config = f.root / "supervision-workspaces.json"
        config.write_text(json.dumps({"workspaces": [{"id": "supervision", "title": "Archive review fixture",
            "root": str(f.project), "mode": mode,
            "commands": {"python": [sys.executable, "-I", "-B", "-X", "utf8"]} if mode == "execute" else {}}]}), encoding="utf-8")
        f.rt.workspaces = WorkspaceService(f.rt, config)
        revision = f.rt.workspaces.view(f.task)["revision"]
        f.rt.workspaces.bind(f.task, "supervision", expected_revision=revision)

    def test_multifile_snapshot_readonly_or_edit_is_exact_and_immutable(self):
        for tool, mode in itertools.product(("request_review", "delegate"), ("read_only", "edit")):
            with self.subTest(tool=tool, mode=mode):
                f = self.fixture(workspace=True)
                files = self.write(f)
                (f.project / "unselected.txt").write_bytes(b"never included")
                self.policy(f, mode)
                f.rt._artifact_root = f.root / "must-not-materialize"
                binding = f.rt.workspaces.view(f.task)
                result = self.exchange(f, tool, self.source(reversed(list(files))))
                request, receipt = self.assert_waiting(f, tool, result)
                artifact, = f.rt.task(f.task).deliverables
                body = read_blob(f.rt.log, artifact["content_sha256"], artifact["content_bytes"])
                with zipfile.ZipFile(io.BytesIO(body)) as zipped:
                    self.assertEqual(zipped.namelist(), sorted(files))
                    self.assertEqual({name: zipped.read(name) for name in zipped.namelist()}, files)
                manifest = artifact["archive"]
                self.assertEqual(manifest["total_bytes"], sum(map(len, files.values())))
                for entry in manifest["files"]:
                    self.assertEqual(entry["sha256"], hashlib.sha256(files[entry["path"]]).hexdigest())
                    self.assertEqual(entry["bytes"], len(files[entry["path"]]))
                self.assertEqual(request.payload["owner"], f.worker)
                self.assertEqual(request.payload["publishers"], [f.worker])
                self.assertEqual(request.payload["artifacts"], [{"seq": artifact["seq"],
                    "filename": "project.zip", "content_sha256": hashlib.sha256(body).hexdigest()}])
                provenance = {"paths": sorted(files), "sha256": hashlib.sha256(body).hexdigest(),
                    "bytes": len(body), "workspace_id": binding["workspace_id"],
                    "workspace_revision": binding["revision"], "workspace_fingerprint": binding["fingerprint"],
                    "started_seq": receipt.payload["started_seq"]}
                self.assertEqual(result["workspace_snapshot"], provenance)
                self.assertEqual(f.events("deliverable.published")[0].payload["review_workspace_source"], provenance)
                self.assertEqual(result["artifact_seqs"], [artifact["seq"]])
                self.assertLess(artifact["seq"], request.seq)
                self.assertLess(request.seq, receipt.seq)
                self.assertFalse(f.rt._artifact_root.exists())
                self.assertEqual(f.events("team.artifact_saved"), [])
                self.assertNotIn("snapshot version", json.dumps([e.payload for e in f.rt.log.read(task_id=f.task)]))
                (f.project / "src/main.py").write_bytes(b"later source")
                for path in ("src/main.py", "src/empty.txt", "assets/data.bin"):
                    read, = f.exchange(f.reviewer, ("read_artifact", {"artifact_seq": artifact["seq"], "path": path}))
                    self.assertEqual(read["status"], "ok", read)
                    self.assertEqual(read["sha256"], hashlib.sha256(files[path]).hexdigest())
                    self.assertEqual(read["content"], None if path.endswith(".bin") else files[path].decode())
                self.assertEqual(f.rt.team_reviews.view(f.task)[0]["status"], "pending")

    def test_reads_once_outside_transaction_owner_held_through_receipt(self):
        for tool in ("request_review", "delegate"):
            with self.subTest(tool=tool):
                f = self.fixture(workspace=True)
                self.write(f)
                seen = []
                snapshot, blob, emit = inputs.create_snapshot, inputs.put_blob, f.rt.log.emit
                def observe(stage, transaction):
                    self.assertEqual(f.rt.log.db.in_transaction, transaction)
                    self.assertEqual(threading.get_ident(), owner_thread)
                    with f.rt.workspaces._owner(str(f.project)) as owned:
                        self.assertFalse(owned, stage)
                    seen.append(stage)
                def capture(*args):
                    observe("capture", False)
                    return snapshot(*args)
                def store(*args):
                    observe("blob", True)
                    return blob(*args)
                def event(*args, **kwargs):
                    if kwargs["type"] in {"deliverable.published", "team.review_requested"}:
                        observe(kwargs["type"], True)
                    if kwargs["type"] == "team.tool_finished" and kwargs["payload"].get("name") == tool:
                        observe("receipt", True)
                    return emit(*args, **kwargs)
                owner_thread = threading.get_ident()
                with patch.object(inputs, "create_snapshot", side_effect=capture), patch.object(inputs, "put_blob", side_effect=store), patch.object(f.rt.log, "emit", side_effect=event):
                    result = self.exchange(f, tool, self.source())
                self.assert_waiting(f, tool, result)
                self.assertEqual(seen, ["capture", "blob", "deliverable.published", "team.review_requested", "receipt"])
                with f.rt.workspaces._owner(str(f.project)) as owned:
                    self.assertTrue(owned)

    def test_artifact_copy_execution_uses_zip_not_changed_workspace(self):
        for tool in ("request_review", "delegate"):
            with self.subTest(tool=tool):
                f = self.fixture(workspace=True)
                files = self.write(f, {"pkg/check.py": b"from pathlib import Path\nassert Path('empty').read_bytes() == b''\nassert Path('data.bin').read_bytes() == b'\\x00\\xff'\nprint('archive copy passed')\n",
                                       "pkg/empty": b"", "pkg/data.bin": b"\x00\xff"})
                self.policy(f, "execute")
                result = self.exchange(f, tool, self.source(files))
                self.assert_waiting(f, tool, result)
                (f.project / "pkg/check.py").write_bytes(b"raise RuntimeError('unpublished source')")
                checked, = f.exchange(f.reviewer, ("workspace_run", {"command": "python", "args": ["check.py"],
                    "cwd": "pkg", "artifact_seqs": result["artifact_seqs"]}))
                self.assertEqual(checked["outcome"], "passed", checked)
                self.assertEqual(checked["stdout"]["excerpt"].strip(), "archive copy passed")
                self.assertTrue(checked["artifacts_unchanged"])
                self.assertTrue(checked["scope_current"])
                self.assertTrue(checked["artifact_check_valid"])
                self.assertEqual(checked["checked_artifacts"][0]["seq"], result["artifact_seqs"][0])
                self.assertEqual(f.rt.team_reviews.view(f.task)[0]["status"], "pending")
                self.assertEqual(f.events("team.review_recorded"), [])

    def test_revision_chain_old_approval_cannot_sign_or_deliver_new_version(self):
        for tool in ("request_review", "delegate"):
            with self.subTest(tool=tool):
                f = self.fixture(workspace=True)
                self.write(f)
                first = self.exchange(f, tool, self.source())
                self.assertEqual(f.record(first["review_seq"])["status"], "ok")
                (f.project / "src/main.py").write_bytes(b"print('version two')\n")
                second = self.exchange(f, tool, self.source(review_seq=first["review_seq"]))
                request, _ = self.assert_waiting(f, tool, second)
                self.assertEqual(second["root_seq"], first["root_seq"])
                self.assertEqual(request.payload["previous_review_seq"], first["review_seq"])
                self.assertEqual(request.payload["previous_artifacts"][0]["seq"], first["artifact_seqs"][0])
                self.assertEqual(f.rt.team_reviews.view(f.task)[0]["status"], "pending")
                self.assertEqual(f.record(first["review_seq"])["status"], "error")
                self.assertEqual(f.finish()["code"], "review_pending")
                self.assertEqual(f.record(second["review_seq"])["status"], "ok")
                f.acknowledge_latest_review()
                refused, = f.exchange(f.manager, ("finish_work", {"artifact_seqs": first["artifact_seqs"],
                    "summary": "old archive", "verification": "offline"}))
                self.assertEqual(refused["status"], "error")
                self.assertEqual(f.finish()["status"], "ok")
                with zipfile.ZipFile(io.BytesIO(f.rt.team.archive(f.task))) as delivery:
                    with zipfile.ZipFile(io.BytesIO(delivery.read("project.zip"))) as archive:
                        self.assertEqual(archive.read("src/main.py"), b"print('version two')\n")

    def test_three_sources_are_exclusive_by_presence_including_null(self):
        sources = [{"artifact_seqs": [1]}, {"workspace_file": {"path": "src/main.py", "format": "code"}}, self.source()]
        invalid = [{}, {"workspace_archive": None}, {"artifact_seqs": None, **self.source()},
                   {"workspace_file": None, **self.source()}]
        invalid += [dict(item for source in combo for item in source.items())
                    for count in (2, 3) for combo in itertools.combinations(sources, count)]
        for tool, source in itertools.product(("request_review", "delegate"), invalid):
            with self.subTest(tool=tool, source=source):
                f = self.fixture(workspace=True)
                self.write(f)
                before = self.state(f)
                with patch.object(inputs, "create_snapshot", side_effect=AssertionError("must reject before read")):
                    result = self.exchange(f, tool, source)
                self.assert_source_rejected(f, result)
                self.unchanged(f, before)

    def test_invalid_names_paths_fields_and_file_count_do_not_publish(self):
        specs = [None, {}, {"filename": "x.zip"}, {"paths": ["src/main.py"]},
                 {"filename": "x.zip", "paths": ["src/main.py"], "extra": True}]
        specs += [{"filename": name, "paths": ["src/main.py"]}
                  for name in (None, "../x.zip", "x.txt", "CON.zip", "ＣＯＮ.zip", "x\u200b.zip", "x.ZIP")]
        specs += [{"filename": "x.zip", "paths": paths} for paths in (
            None, "src/main.py", [], [True], ["missing"], ["src"], ["../outside"], ["/absolute"],
            [".env"], [".git/config"], ["src/main.py"] * 2, ["src/main.py", "SRC/main.py"],
            ["src\\main.py"], ["src/main.py"] * 257)]
        for tool, spec in itertools.product(("request_review", "delegate"), specs):
            with self.subTest(tool=tool, spec=spec):
                f = self.fixture(workspace=True)
                self.write(f)
                before = self.state(f)
                result = self.exchange(f, tool, {"workspace_archive": spec})
                self.assert_source_rejected(f, result)
                self.unchanged(f, before)

    def test_real_files_exceed_member_total_and_zip_limits(self):
        # Lower only byte constants: exercise real reads/ZIP writes without large allocations.
        for tool, limit in itertools.product(("request_review", "delegate"),
                ("MAX_MEMBER_BYTES", "MAX_TOTAL_BYTES", "MAX_ARCHIVE_BYTES")):
            with self.subTest(tool=tool, limit=limit):
                f = self.fixture(workspace=True)
                files = self.write(f, {"one": b"12345", "two": b"67890"})
                before = self.state(f)
                with patch.object(archives, limit, 4 if limit == "MAX_MEMBER_BYTES" else 9):
                    result = self.exchange(f, tool, self.source(files))
                self.assert_source_rejected(f, result)
                self.unchanged(f, before)

    def test_hardlinked_source_is_rejected(self):
        f = self.fixture(workspace=True)
        self.write(f)
        os.link(f.project / "src/main.py", f.project / "linked.py")
        before = self.state(f)
        result = self.exchange(f, "request_review", self.source(["linked.py"]))
        self.assert_source_rejected(f, result)
        self.unchanged(f, before)

    def test_missing_root_self_review_busy_unknown_and_done_reject_before_read(self):
        for tool, kind in itertools.product(("request_review", "delegate"), ("root", "self", "busy", "unknown", "done")):
            with self.subTest(tool=tool, kind=kind):
                f = self.fixture(workspace=kind != "root")
                if kind != "root":
                    self.write(f)
                if kind == "unknown":
                    f.orphan_write(f.project)
                if kind == "done":
                    self.assertEqual(f.finish()["status"], "ok")
                before = self.state(f)
                with ExitStack() as stack:
                    if kind == "busy":
                        self.assertTrue(stack.enter_context(f.rt.workspaces._owner(str(f.project))))
                    stack.enter_context(patch.object(inputs, "create_snapshot", side_effect=AssertionError("must not read")))
                    result = self.exchange(f, tool, self.source(), reviewer=f.worker if kind == "self" else None)
                self.assertEqual(result["status"], "error", result)
                self.unchanged(f, before)

    def test_scope_target_pause_or_customer_change_after_capture_rolls_back(self):
        for tool, kind in itertools.product(("request_review", "delegate"), ("binding", "goal", "pause", "customer")):
            with self.subTest(tool=tool, kind=kind):
                f = self.fixture(workspace=True)
                self.write(f)
                before = self.state(f)
                original = inputs.create_snapshot
                def change(*args):
                    result = original(*args)
                    if kind == "binding":
                        binding = f.rt.workspaces.view(f.task)
                        f.rt.workspaces.bind(f.task, None, expected_revision=binding["revision"])
                    elif kind == "goal":
                        f.rt.log.emit(type="team.objective_updated", actor=Actor.system("team"), task_id=f.task,
                                      payload={"goal": "New objective during capture"})
                    elif kind == "pause":
                        f.rt.execution_control.change(f.task, "pause", request_id="pause-capture")
                    else:
                        role = f.rt.team._role(f.worker)
                        f.rt.team.receive(f.task, "@" + role.name + " Changed requirement")
                    return result
                with patch.object(inputs, "create_snapshot", side_effect=change):
                    result, = f.exchange(f.worker, (tool, self.arguments(f, tool, self.source())),
                                          expected_statuses=("waiting", "completed", "interrupted"))
                self.assertEqual(result["status"], "error", result)
                self.unchanged(f, before)

    def test_wrong_revision_owner_reviewer_or_old_goal_rolls_back_blob(self):
        for tool, kind in itertools.product(("request_review", "delegate"), ("owner", "reviewer", "goal")):
            with self.subTest(tool=tool, kind=kind):
                f = self.fixture(workspace=True)
                self.write(f)
                first = self.exchange(f, tool, self.source())
                self.assertEqual(f.record(first["review_seq"], "changes_requested")["status"], "ok")
                (f.project / "src/main.py").write_bytes(b"new version\n")
                if kind == "goal":
                    f.rt.log.emit(type="team.objective_updated", actor=Actor.system("team"), task_id=f.task,
                                  payload={"goal": "New objective before revision"})
                before = self.state(f)
                result = self.exchange(f, tool, self.source(review_seq=first["review_seq"]),
                    author=f.manager if kind == "owner" else f.worker,
                    reviewer=f.observer if kind == "reviewer" else f.reviewer)
                self.assertEqual(result["status"], "error", result)
                if kind in {"owner", "reviewer"}:
                    self.assertEqual(result["code"], "review_author_mismatch" if kind == "owner" else "reviewer_mismatch")
                self.unchanged(f, before)
                if kind == "goal":
                    current = self.exchange(f, tool, self.source())
                    self.assert_waiting(f, tool, current)
                    self.assertNotEqual(current["root_seq"], first["root_seq"])

    def test_failures_after_each_write_rollback_blob_events_messages_and_receipt(self):
        for tool, stage, existing in itertools.product(("request_review", "delegate"),
                ("blob", "publication", "review", "assignment", "receipt", "returned_error"), (False, True)):
            with self.subTest(tool=tool, stage=stage, existing=existing):
                f = self.fixture(workspace=True)
                self.write(f)
                if existing:
                    put_blob(f.rt.log, b"previous blob remains")
                before = self.state(f)
                original_blob, original_emit, original_request = inputs.put_blob, f.rt.log.emit, f.rt.team_reviews.request
                injected = []
                def fail():
                    self.assertTrue(f.rt.log.db.in_transaction)
                    self.assertGreater(len(f.rt.log.db.execute("SELECT sha256 FROM artifact_blobs").fetchall()), int(existing))
                    injected.append(stage)
                    raise ValueError("offline injection after write")
                def blob(*args):
                    result = original_blob(*args)
                    if stage == "blob":
                        fail()
                    return result
                def emit(*args, **kwargs):
                    event = original_emit(*args, **kwargs)
                    kind, payload = kwargs["type"], kwargs["payload"]
                    if ((stage == "publication" and kind == "deliverable.published")
                        or (stage == "review" and kind == "team.review_requested")
                        or (stage == "assignment" and kind == "team.message_received" and payload.get("kind") == "assignment")
                        or (stage == "receipt" and kind == "team.tool_finished" and payload.get("name") == tool and payload["result"].get("status") == "ok")):
                        fail()
                    return event
                def request(*args):
                    result = original_request(*args)
                    if stage == "returned_error":
                        self.assertEqual(result["status"], "ok")
                        injected.append(stage)
                        return {"status": "error", "code": "injected_rejection"}
                    return result
                with patch.object(inputs, "put_blob", side_effect=blob), patch.object(f.rt.log, "emit", side_effect=emit), patch.object(f.rt.team_reviews, "request", side_effect=request):
                    result = self.exchange(f, tool, self.source())
                self.assertEqual(result["status"], "error", result)
                self.assertEqual(injected, [stage])
                self.unchanged(f, before)
                receipts = [e for e in f.events("team.tool_finished") if e.payload["name"] == tool]
                self.assertEqual(len(receipts), 1)
                self.assertEqual(receipts[0].payload["result"]["status"], "error")
                self.assertEqual(f.rt.team._pending_operations(f.task), [])
                with f.rt.workspaces._owner(str(f.project)) as owned:
                    self.assertTrue(owned)

    def test_prepare_guards_paused_scope_and_existing_transaction(self):
        for kind in ("pause", "transaction"):
            with self.subTest(kind=kind):
                f = self.fixture(workspace=True)
                self.write(f)
                role = f.rt.team._role(f.worker)
                source = f.rt.team.receive(f.task, "@" + role.name + " Review these files")
                binding = f.rt.workspaces.view(f.task)
                start = SimpleNamespace(seq=source.seq, payload={"revision": f.rt.team.view(f.task)["revision"], "message_seq": source.seq})
                call = ToolCall("guard", "request_review", self.arguments(f, "request_review", self.source()))
                workspace = WorkspaceExecutor(f.rt.workspaces.config["supervision"]["policy"])
                if kind == "pause":
                    f.rt.execution_control.change(f.task, "pause", request_id="paused-before-capture")
                before = self.state(f)
                with ExitStack() as stack:
                    if kind == "transaction":
                        stack.enter_context(f.rt.log.atomic())
                    stack.enter_context(patch.object(inputs, "create_snapshot", side_effect=AssertionError("must not read")))
                    with self.assertRaises(WorkConflict):
                        with inputs.prepare(f.rt.team, f.task, role, call, workspace, binding, start):
                            self.fail("must not yield")
                self.unchanged(f, before)


if __name__ == "__main__":
    unittest.main()
