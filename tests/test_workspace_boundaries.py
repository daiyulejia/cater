"""Regression contracts for live authorization and exact source archive bytes."""

from __future__ import annotations

import io
import json
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tempfile
import unittest
import zipfile

from cater.drivers import DriverResponse
from cater.events import Actor
from cater.runtime import Runtime
from cater.tool_protocol import ToolCall, ToolTurn
from cater.work import WorkConflict


HOST = Actor.system("workspace_gateway")
SOURCE = "def add(a, b):\r\n    return a + b\r\n"


def work_item(identity, *, local=False):
    criteria = [{"id": "meaning", "kind": "review", "text": "Inspect submitted source"}]
    if local:
        criteria.append({"id": "behavior", "kind": "local_check", "text": "Run source byte checks"})
    return {"id": identity, "owner": "dev", "reviewer": "test", "objective": "Deliver " + identity,
            "depends_on": [], "criteria": criteria}


def tool_turn(*calls, text=""):
    return ToolTurn(text, list(calls), 5, {"token_usage": "reported"},
                    {"role": "assistant", "content": text, "tool_calls": [
                        {"id": call.id, "name": call.name, "arguments": call.arguments} for call in calls]})


def write_turn(identity, path, content="changed\n"):
    return tool_turn(ToolCall(identity, "workspace_write",
                             {"path": path, "content": content, "expected_sha256": None}))


def final_turn(paths=None):
    raw = {"stance": "assert", "claim": "Observed result", "text": "Observed result",
           "deliverables": [] if paths is not None else [
               {"title": "Result", "summary": "Observed result", "content": "Observed result.\n",
                "format": "text", "filename": "result.txt"}]}
    if paths is not None:
        raw["workspace_files"] = paths
    return tool_turn(text=json.dumps(raw))


class BoundaryDriver:
    """Script model turns; leave claims, checks, filesystem and event actors real."""

    def __init__(self):
        self.turns = []
        self.tool_requests = []
        self.after_turn = None

    def tool_turn(self, **request):
        self.tool_requests.append(request)
        if not self.turns:
            raise AssertionError("Unexpected model tool turn")
        response = self.turns.pop(0)
        if self.after_turn is not None:
            self.after_turn(len(self.tool_requests))
        return response

    def complete(self, *, role, prompt, context):
        work = context["work"]
        artifact = work["artifacts"][0]
        if work["phase"] == "review":
            raw = {"checks": [
                {"id": criterion["id"], "status": "passed", "reason": "Inspected submitted bytes",
                 "artifact_seq": artifact["seq"], "quote": artifact["content"],
                 "receipt_seq": work["local_checks"].get(criterion["id"], {}).get("seq")}
                for criterion in work["contract"]["criteria"]]}
        elif work["phase"] == "delivery":
            def evidence_row(identity):
                return {"id": identity, "status": "passed", "reason": "Inspected the submitted bundle",
                        "evidence": [{"artifact_seq": artifact["seq"], "quote": artifact["content"]}],
                        "superseded_by": None, "source_quote": None}

            raw = {"requirements": [evidence_row(source["id"]) for source in work["manifest"]["sources"]],
                   "checks": [evidence_row(c["id"]) for c in work["manifest"]["criteria"] if c["kind"] == "review"],
                   "handoff": {"summary": "Captured the source bundle", "why": "Checked exact submitted bytes",
                               "reproduce": ["Run check_source.py"], "limits": ["Only fixture cases were checked"]}}
        else:
            raise AssertionError("Unexpected text phase: " + str(work.get("phase")))
        return DriverResponse(json.dumps(raw), 7, {"token_usage": "reported"})


class WorkspaceBoundaryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="cater-workspace-boundaries-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "project"
        self.root.mkdir()
        self.config = self.base / "workspaces.json"
        self.driver = BoundaryDriver()

    def write_config(self, *, mode="edit", checks=False):
        row = {"id": "project", "title": "Boundary fixture", "root": str(self.root), "mode": mode}
        if checks:
            row.update(commands={"python": [sys.executable, "-B"]}, checks=[
                {"id": "source_bytes", "title": "Source bytes", "command": "python",
                 "args": ["check_source.py"], "cwd": "."}])
        self.config.write_text(json.dumps({"workspaces": [row]}), encoding="utf-8")

    def open_runtime(self, *, checks=False):
        self.write_config(mode="execute" if checks else "edit", checks=checks)
        self.rt = Runtime(self.base / "tasks.db", workspaces_path=self.config,
                          drivers={name: self.driver for name in ("dev", "test", "product")})
        self.addCleanup(self.rt.close)
        self.task = self.rt.create_task("Preserve the bound source and its authorization")
        self.rt.workspaces.bind(self.task, "project", expected_revision=None)
        bound = self.events("workspace.bound")[-1]
        self.assertEqual(bound.actor, Actor.human())

    def events(self, kind):
        return list(self.rt.log.iter_events(task_id=self.task, event_types=(kind,)))

    def assert_old_round_stops(self, interrupt_turn):
        self.open_runtime()
        self.rt.plan_work(self.task, [work_item("a"), work_item("b")])
        scope = self.rt.work.scope(self.task)
        initial_control = self.rt.execution_control.view(self.task)["seq"]
        self.driver.turns = [write_turn("a-write", "a.txt")]
        if interrupt_turn == 2:
            self.driver.turns.append(final_turn())
        self.driver.turns.extend([write_turn("b-write", "b.txt"), final_turn()])

        def interrupt(index):
            if index == interrupt_turn:
                self.rt.execution_control.change(self.task, "pause", request_id="pause-old-boundary-round")
                self.rt.execution_control.change(self.task, "resume", request_id="resume-new-boundary-round")

        self.driver.after_turn = interrupt
        try:
            self.rt.collaborate(self.task, expected_scope=scope)
        except WorkConflict:
            pass
        controls = self.events("task.execution_controlled")
        self.assertEqual([e.payload["action"] for e in controls], ["pause", "resume"])
        self.assertTrue(all(e.actor == Actor.human() for e in controls))
        self.assertEqual(controls[0].payload["previous_seq"], initial_control)
        self.assertEqual(controls[1].payload["previous_seq"], controls[0].seq)
        self.assertEqual(self.rt.execution_control.view(self.task)["seq"], controls[1].seq)
        self.assertEqual(self.rt.work.scope(self.task), scope)
        claims = self.events("work.claimed")
        self.assertEqual([e.payload["work_item_id"] for e in claims], ["a"],
                         "The old round must not adopt resume authorization to claim B")
        self.assertEqual(claims[0].actor, Actor.system("work_scheduler"))
        starts = self.events("workspace.tool_started")
        self.assertTrue(all(e.actor == HOST and e.seq < controls[0].seq for e in starts))
        self.assertFalse((self.root / "b.txt").exists())
        self.assertEqual(len(self.driver.tool_requests), interrupt_turn)
        if interrupt_turn == 2:
            self.assertEqual((self.root / "a.txt").read_bytes(), b"changed\n")
            self.assertEqual(len(self.events("workspace.tool_finished")), 1)
        else:
            self.assertFalse((self.root / "a.txt").exists())
            self.assertEqual(starts, [])
        self.assertTrue(self.rt.verify_chain().ok)

    def test_pause_resume_during_model_turn_cannot_claim_or_execute_b(self):
        self.assert_old_round_stops(1)

    def test_pause_resume_after_completed_write_cannot_claim_or_execute_b(self):
        self.assert_old_round_stops(2)

    def assert_live_config_revocation(self, change):
        self.open_runtime()
        self.rt.plan_work(self.task, [work_item("source")])
        claim = self.rt.work.claim(self.task, "source")
        item = self.rt.work.view(self.task)["items"][0]
        context = {**claim.payload, "contract": item, "accepted_inputs": {}, "customer_directions": []}
        self.driver.turns = [write_turn("revoked-write", "revoked.txt"), final_turn()]
        changes = []

        def revoke(index):
            if index == 1:
                if change == "read_only":
                    self.write_config(mode="read_only")
                elif change == "invalid_json":
                    self.config.write_text('{"workspaces": [', encoding="utf-8")
                elif change == "invalid_policy":
                    self.write_config(mode="revoked-invalid-mode")
                elif change == "deleted":
                    self.config.unlink()
                else:
                    raise AssertionError(change)
                changes.append(change)

        self.driver.after_turn = revoke
        response = self.rt._complete_with_containment(
            task_id=self.task, task=self.rt.task(self.task), role=self.rt._role_by_id("dev"),
            driver=self.driver, prompt="Inspect the bound source", work_context=context)
        self.assertEqual(changes, [change])
        self.assertFalse((self.root / "revoked.txt").exists(),
                         "An existing Runtime must recheck disk policy before applying model tools")
        self.assertEqual(self.events("workspace.tool_started"), [])
        self.assertEqual(self.events("workspace.tool_finished"), [])
        self.assertEqual(self.events("workspace.files_captured"), [])
        self.assertEqual(len(self.driver.tool_requests), 1)
        self.assertTrue(response.meta.get("contained"))
        self.assertEqual(self.events("work.claimed")[0].payload["attempt_id"], context["attempt_id"])
        self.assertTrue(self.rt.verify_chain().ok)

    def test_live_edit_permission_revoked_to_read_only_blocks_write(self):
        self.assert_live_config_revocation("read_only")

    def test_live_invalid_json_config_blocks_write(self):
        self.assert_live_config_revocation("invalid_json")

    def test_live_invalid_policy_config_blocks_write(self):
        self.assert_live_config_revocation("invalid_policy")

    def test_live_deleted_config_blocks_write(self):
        self.assert_live_config_revocation("deleted")

    def assert_archive_bytes(self, extra_files, *, collision):
        self.open_runtime(checks=True)
        files = {"calculator.py": SOURCE, **extra_files}
        check = ("from pathlib import Path\nfrom calculator import add\n"
                 "assert add(2, 3) == 5\nassert add(-2, 2) == 0\n"
                 + "expected = " + repr(files) + "\n"
                 + "for name, content in expected.items():\n"
                   "    assert Path(name).read_bytes() == content.encode('utf-8'), name\n"
                   "print('boundary sources match')\n")
        files["check_source.py"] = check
        calls = [ToolCall("capture-" + str(index), "workspace_write",
                          {"path": path, "content": content, "expected_sha256": None})
                 for index, (path, content) in enumerate(files.items())]
        self.driver.turns = [tool_turn(*calls), final_turn(list(files))]
        self.rt.plan_work(self.task, [work_item("implementation", local=True)])
        self.rt.collaborate(self.task)
        work = self.rt.work.view(self.task)
        self.assertTrue(work["stage_accepted"], work)
        item = work["items"][0]
        artifacts = self.rt.work.artifacts(self.task, item["submission"])
        self.assertEqual({a["workspace_file"]["path"] for a in artifacts}, set(files))
        stage = self.rt.work.checks(self.task, item)["behavior"]
        self.assertEqual(stage.payload["outcome"], "passed")
        self.assertTrue(stage.payload["artifacts_unchanged"])
        self.rt.delivery.prepare(self.task, reviewer="cabinet_product")
        delivery = self.rt.delivery.run(self.task)
        self.assertEqual(delivery["status"], "accepted", delivery)
        whole = self.rt.delivery._checks(self.task, delivery["digest"])["integration_behavior"]
        self.assertNotEqual(whole.seq, stage.seq)
        self.assertEqual(whole.payload["outcome"], "passed")
        self.assertTrue(whole.payload["artifacts_unchanged"])
        self.assertEqual(whole.payload["check_kind"], "workspace")
        manifest = delivery["manifest"]
        if collision:
            metadata_root = manifest.get("metadata_root")
            self.assertIsInstance(metadata_root, str,
                                  "Conflicting source names require an explicit metadata_root in the manifest")
            self.assertTrue(metadata_root)
            path = PurePosixPath(metadata_root)
            self.assertFalse(path.is_absolute())
            self.assertNotIn("..", path.parts)
            self.assertNotIn("\\", metadata_root)
            self.assertNotIn(":", metadata_root)
            self.assertFalse(any(p.casefold() == metadata_root.casefold()
                                 or p.casefold().startswith(metadata_root.casefold() + "/")
                                 or metadata_root.casefold().startswith(p.casefold() + "/") for p in files))
            if not any(p == ".cater-delivery" or p.startswith(".cater-delivery/") for p in files):
                self.assertEqual(metadata_root, ".cater-delivery")
            prefix = metadata_root + "/"
        else:
            self.assertNotIn("metadata_root", manifest)
            prefix = ""
        output = self.rt.delivery.archive(self.task)
        with zipfile.ZipFile(io.BytesIO(output)) as archive:
            names = archive.namelist()
            self.assertEqual(len(names), len({name.casefold() for name in names}), names)
            self.assertEqual(set(names), {*files, prefix + "manifest.json", prefix + "handoff.md"})
            for name, content in files.items():
                self.assertEqual(archive.read(name), content.encode("utf-8"), name)
            metadata = json.loads(archive.read(prefix + "manifest.json"))
            self.assertEqual(metadata["digest"], delivery["digest"])
            self.assertEqual(metadata["manifest"], manifest)
            self.assertIn("Captured the source bundle", archive.read(prefix + "handoff.md").decode("utf-8"))
            extracted = self.base / "extracted"
            archive.extractall(extracted)
        result = subprocess.run([sys.executable, "-B", "check_source.py"], cwd=extracted,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("boundary sources match", result.stdout)
        self.assertTrue(self.rt.verify_chain().ok)

    def test_accepted_project_manifest_keeps_bytes_and_moves_delivery_metadata(self):
        self.assert_archive_bytes({"manifest.json": '{"project":"expected"}\r\n'}, collision=True)

    def test_accepted_project_handoff_keeps_bytes_and_moves_delivery_metadata(self):
        self.assert_archive_bytes({"handoff.md": "# Project handoff\r\n"}, collision=True)

    def test_metadata_directory_avoids_existing_project_directory(self):
        self.assert_archive_bytes({"manifest.json": '{"project":"expected"}\n',
                                   ".cater-delivery/project.txt": "Project-owned metadata directory\n"}, collision=True)

    def test_archive_without_reserved_names_keeps_legacy_metadata_paths(self):
        self.assert_archive_bytes({}, collision=False)


if __name__ == "__main__":
    unittest.main()
