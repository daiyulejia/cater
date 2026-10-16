"""Publication preflight rejections and the durable publication boundary."""

import hashlib
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import zipfile

from cater.artifacts import MAX_CONTENT_BYTES
from cater.workspace_tools import WorkspaceExecutor
from cater.workspaces import WorkspaceService
import test_simple_team


class PublicationPreflightTests(unittest.TestCase):
    setUp = test_simple_team.SimpleTeamTests.setUp
    events = test_simple_team.SimpleTeamTests.events
    run_ok = test_simple_team.SimpleTeamTests.run_ok

    def bind_workspace(self):
        self.project = self.root / "project"
        self.project.mkdir()
        config = self.root / "workspaces.json"
        config.write_text(json.dumps({"workspaces": [{
            "id": "project", "title": "Project", "root": str(self.project), "mode": "read_only",
        }]}), encoding="utf-8")
        self.rt.workspaces = WorkspaceService(self.rt, config)
        self.rt.workspaces.bind(self.task, "project", expected_revision=None)

    def artifact(self, **changes):
        return {"filename": "result.txt", "content": "Actual source\n", "summary": "Fixture",
                "format": "text", **changes}

    def run_steps(self, steps):
        def unique_step(step):
            def respond(context, history, tools):
                turn = step(context, history, tools) if callable(step) else step
                # Large sources can evict history; real calls must retain distinct IDs.
                for index, call in enumerate(turn.calls):
                    identity = f"publication-{len(self.driver.calls)}-{index}"
                    turn.calls[index] = test_simple_team.ToolCall(identity, call.name, call.arguments)
                    turn.message["tool_calls"][index]["id"] = identity
                return turn
            return respond

        self.driver.steps[self.manager] = [unique_step(step) for step in
                                           [*steps, test_simple_team.turn("Checked.")]]
        self.rt.team.receive(self.task, "Check publication preflight")
        self.run_ok()
        return [e.payload["result"] for e in self.events("team.tool_finished")]

    def assert_rejected(self, result, code):
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["code"], code)
        self.assertIs(result["complete"], True)
        self.assertEqual(result["outcome"], "not_started")

    def assert_no_publication(self):
        self.assertEqual(self.events("deliverable.published"), [])
        self.assertEqual(self.events("team.artifact_saved"), [])
        self.assertEqual(self.rt.task(self.task).deliverables, [])
        self.assertEqual(list(self.rt._artifact_root.rglob("*")), [])
        self.assertEqual(self.rt.team._pending_operations(self.task), [])
        self.assertEqual(self.rt.workspaces.pending(task_id=self.task), [])
        if hasattr(self, "project"):
            self.assertEqual(self.rt.workspaces.pending(root=str(self.project)), [])

    def assert_delivered_bytes(self, filename, body):
        published, = self.events("deliverable.published")
        saved, = self.events("team.artifact_saved")
        completed, = self.events("team.objective_completed")
        artifact, = self.rt.task(self.task).deliverables
        self.assertEqual(artifact["seq"], published.seq)
        self.assertEqual(saved.payload["artifact_seq"], published.seq)
        self.assertEqual(artifact["filename"], filename)
        self.assertEqual(artifact["content"].encode("utf-8"), body)
        self.assertEqual(Path(artifact["local_path"]).read_bytes(), body)
        self.assertEqual(self.rt.team.view(self.task)["status"], "done")
        self.assertEqual(self.rt.team._pending_operations(self.task), [])
        self.assertEqual(self.rt.workspaces.pending(task_id=self.task), [])
        with zipfile.ZipFile(io.BytesIO(self.rt.team.archive(
                self.task, expected_delivery_seq=completed.seq))) as archive:
            self.assertEqual(archive.read(filename), body)
            manifest = json.loads(archive.read("__cater__/manifest.json"))
            self.assertEqual(manifest["files"], [{"filename": filename, "seq": published.seq,
                                                 "sha256": hashlib.sha256(body).hexdigest()}])

    def finish_turn(self):
        return test_simple_team.turn("", ("finish_work", {
            "summary": "Fixture delivered", "verification": "Checked source bytes only",
        }))

    def test_invalid_content_format_summary_and_filename_have_no_effects(self):
        cases = [
            {"content": ""}, {"content": " \r\n\t"}, {"content": None}, {"content": 7},
            {"format": "html"}, {"format": ["text"]},
            {"summary": ""}, {"filename": ""}, {"filename": "../result.txt"},
            {"filename": "CON.txt"}, {"filename": "x" * 181},
            {"filename": "\u4e2d" * 59 + "a.txt"},
        ]
        with patch("cater.artifact_store.materialize_team_artifact") as materialize:
            results = self.run_steps([test_simple_team.turn("", ("publish_artifact", self.artifact(**case)))
                                      for case in cases])
        self.assertEqual(len(results), len(cases))
        for case, result in zip(cases, results):
            with self.subTest(case=case):
                self.assert_rejected(result, "invalid_artifact")
        materialize.assert_not_called()
        self.assert_no_publication()

    def test_strict_portable_names_are_rejected_before_domain_event(self):
        # These pass safe_filename but fail the store's NFKC/control/device checks.
        filenames = ["\uff23\uff2f\uff2e.txt", "\uff0fresult.txt", "COM\u00b9.txt",
                     "CONIN$.txt", "CONOUT$.txt", "hidden\u200b.txt", "delete\x7f.txt"]
        with patch("cater.artifact_store.materialize_team_artifact") as materialize:
            results = self.run_steps([test_simple_team.turn("", ("publish_artifact", self.artifact(filename=name)))
                                      for name in filenames])
        self.assertEqual(len(results), len(filenames))
        for filename, result in zip(filenames, results):
            with self.subTest(filename=filename):
                self.assert_rejected(result, "invalid_artifact_filename")
        materialize.assert_not_called()
        self.assert_no_publication()

    def test_bad_publish_corrected_in_same_run_finishes_and_downloads_exact_bytes(self):
        content = "  \u771f\u5b9e\u6b63\u6587\r\nkeep trailing spaces  \n"
        arguments = self.artifact(content=content)

        def corrected(context, history, tools):
            result = self.events("team.tool_finished")[-1].payload["result"]
            self.assert_rejected(result, "invalid_artifact")
            self.assert_no_publication()
            self.assertTrue(history[-1]["is_error"])
            self.assertEqual(json.loads(history[-1]["content"]), result)
            return test_simple_team.turn("", ("publish_artifact", arguments))

        results = self.run_steps([test_simple_team.turn("", ("publish_artifact", {
            **arguments, "format": "html"})), corrected, self.finish_turn()])
        self.assertEqual([r["status"] for r in results], ["error", "ok", "ok"])
        starts = self.events("team.tool_started")
        self.assertEqual(len({e.payload["run_id"] for e in starts}), 1)
        self.assertEqual([e.payload["arguments"]["filename"] for e in starts[:2]], ["result.txt"] * 2)
        self.assert_delivered_bytes("result.txt", content.encode("utf-8"))

    def test_inline_64kib_limit_counts_utf8_bytes_and_accepts_unicode_name(self):
        content = "\u4e2d" * 21845 + "x"
        filename = "\u4ea4\u4ed8.txt"
        self.assertEqual(len(content.encode("utf-8")), 65536)
        self.assertEqual(MAX_CONTENT_BYTES, 65536)

        def corrected(context, history, tools):
            result = self.events("team.tool_finished")[-1].payload["result"]
            self.assert_rejected(result, "artifact_too_large")
            self.assertEqual(result["max_content_bytes"], 65536)
            self.assert_no_publication()
            return test_simple_team.turn("", ("publish_artifact", self.artifact(
                filename=filename, content=content)))

        results = self.run_steps([test_simple_team.turn("", ("publish_artifact", self.artifact(
            filename=filename, content=content + "x"))), corrected, self.finish_turn()])
        self.assertEqual([r["status"] for r in results], ["error", "ok", "ok"])
        self.assert_delivered_bytes(filename, content.encode("utf-8"))

    def test_workspace_missing_binary_outside_and_protected_sources_do_not_publish(self):
        self.bind_workspace()
        (self.project / "binary.txt").write_bytes(b"\xff\xfeinvalid utf8")
        (self.project / "nul.txt").write_bytes(b"before\x00after")
        outside = self.root / "outside.txt"
        outside.write_bytes(b"Outside fixture")
        (self.project / ".env").write_bytes(b"SECRET=fixture")
        paths = ["missing.txt", "binary.txt", "nul.txt", "../outside.txt", str(outside), ".env"]
        before = {p: p.read_bytes() for p in self.project.iterdir()}
        with patch("cater.artifact_store.materialize_team_artifact") as materialize:
            results = self.run_steps([test_simple_team.turn("", ("publish_workspace_file", {
                "path": path, "summary": "Fixture", "format": "text"})) for path in paths])
        self.assertEqual(len(results), len(paths))
        for path, result in zip(paths, results):
            with self.subTest(path=path):
                self.assert_rejected(result, "source_file_unavailable")
        materialize.assert_not_called()
        self.assert_no_publication()
        self.assertEqual({p: p.read_bytes() for p in self.project.iterdir()}, before)
        self.assertEqual(outside.read_bytes(), b"Outside fixture")

    def test_workspace_symlink_source_does_not_publish_when_supported(self):
        self.bind_workspace()
        outside = self.root / "outside.txt"
        outside.write_bytes(b"Outside fixture")
        link = self.project / "linked.txt"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlink creation unavailable: {exc}")
        with patch("cater.artifact_store.materialize_team_artifact") as materialize:
            result, = self.run_steps([test_simple_team.turn("", ("publish_workspace_file", {
                "path": "linked.txt", "summary": "Fixture", "format": "text"}))])
        self.assert_rejected(result, "source_file_unavailable")
        materialize.assert_not_called()
        self.assert_no_publication()
        self.assertTrue(link.is_symlink())
        self.assertEqual(outside.read_bytes(), b"Outside fixture")

    def test_workspace_64kib_boundary_corrected_same_file_finishes_and_downloads(self):
        self.bind_workspace()
        source = self.project / "result.txt"
        body = ("\u4e2d" * 21845 + "x").encode("utf-8")
        self.assertEqual(len(body), 65536)
        # The read window bisects the final UTF-8 codepoint in the oversized source.
        oversized = b"x" * 65534 + "\u4e2d".encode("utf-8")
        self.assertEqual(len(oversized), 65537)
        source.write_bytes(oversized)
        arguments = {"path": "result.txt", "summary": "Fixture", "format": "text"}

        def corrected(context, history, tools):
            result = self.events("team.tool_finished")[-1].payload["result"]
            self.assert_rejected(result, "source_file_too_large")
            self.assertEqual(result["max_content_bytes"], 65536)
            self.assert_no_publication()
            source.write_bytes(body)
            return test_simple_team.turn("", ("publish_workspace_file", arguments))

        results = self.run_steps([test_simple_team.turn("", ("publish_workspace_file", arguments)),
                                  corrected, self.finish_turn()])
        self.assertEqual([r["status"] for r in results], ["error", "ok", "ok"])
        self.assertEqual(source.read_bytes(), body)
        self.assert_delivered_bytes("result.txt", body)

    def test_workspace_content_and_portable_name_checks_precede_publication(self):
        self.bind_workspace()
        files = [("empty.txt", b"", "text", "invalid_artifact"),
                 ("result.txt", b"Actual source", "html", "invalid_artifact"),
                 ("hidden\u200b.txt", b"Actual source", "text", "source_file_unavailable"),
                 ("\u4e2d" * 61 + ".txt", b"Actual source", "text", "invalid_artifact")]
        for filename, body, _, _ in files:
            (self.project / filename).write_bytes(body)
        with patch("cater.artifact_store.materialize_team_artifact") as materialize:
            results = self.run_steps([test_simple_team.turn("", ("publish_workspace_file", {
                "path": filename, "summary": "Fixture", "format": format}))
                for filename, _, format, _ in files])
        self.assertEqual(len(results), len(files))
        for (filename, _, _, code), result in zip(files, results):
            with self.subTest(filename=filename):
                self.assert_rejected(result, code)
        materialize.assert_not_called()
        self.assert_no_publication()

    def test_returned_read_errors_and_incomplete_reads_are_not_started(self):
        self.bind_workspace()
        sources = [
            {"status": "error", "error": {"code": "operation_failed"}},
            {"status": "ok", "path": "result.txt", "content": "Excerpt", "bytes": 20, "truncated": True},
            {"status": "error", "bytes": 65537, "error": {"code": "too_large"}},
        ]
        with patch.object(WorkspaceExecutor, "execute", side_effect=sources) as read, \
                patch("cater.artifact_store.materialize_team_artifact") as materialize:
            results = self.run_steps([test_simple_team.turn("", ("publish_workspace_file", {
                "path": "result.txt", "summary": "Fixture", "format": "text"})) for _ in sources])
        self.assertEqual(len(results), len(sources))
        for result, code in zip(results, ["source_file_unavailable", "source_file_unavailable", "source_file_too_large"]):
            self.assert_rejected(result, code)
        self.assertEqual(read.call_count, len(sources))
        for call in read.call_args_list:
            self.assertEqual(call.args, ("workspace_read", {"path": "result.txt", "limit": 65536}))
        materialize.assert_not_called()
        self.assert_no_publication()

    def test_source_read_exception_remains_unknown_and_blocks_delivery(self):
        self.bind_workspace()
        with patch.object(WorkspaceExecutor, "execute", side_effect=OSError("Private read failure")), \
                patch("cater.artifact_store.materialize_team_artifact") as materialize:
            first, finish = self.run_steps([test_simple_team.turn("", ("publish_workspace_file", {
                "path": "result.txt", "summary": "Fixture", "format": "text"})), self.finish_turn()])
        self.assertEqual(first["status"], "error")
        self.assertIs(first["complete"], False)
        self.assertEqual(first["outcome"], "effects_unknown")
        self.assertNotIn("Private read failure", json.dumps(first))
        self.assertEqual(finish["code"], "operation_pending")
        pending, = self.rt.team._pending_operations(self.task)
        self.assertEqual(pending["outcome"], "unknown")
        self.assertFalse(pending["replay_authorized"])
        self.assertEqual(self.events("deliverable.published"), [])
        self.assertEqual(self.events("team.artifact_saved"), [])
        self.assertEqual(self.events("team.objective_completed"), [])
        materialize.assert_not_called()

    def test_materialize_failure_keeps_event_and_blocks_same_file_republish_and_finish(self):
        self.bind_workspace()
        arguments = self.artifact()
        (self.project / "result.txt").write_bytes(arguments["content"].encode("utf-8"))

        def fail_after_domain_event(*args):
            self.assertEqual(len(self.events("deliverable.published")), 1)
            self.assertFalse(self.rt.log.db.in_transaction)
            raise OSError("Private disk failure")

        with patch("cater.artifact_store.materialize_team_artifact", side_effect=fail_after_domain_event) as materialize:
            first, retry, workspace_retry, finish = self.run_steps([
                test_simple_team.turn("", ("publish_artifact", arguments)),
                test_simple_team.turn("", ("publish_artifact", self.artifact(content="Must not replace"))),
                test_simple_team.turn("", ("publish_workspace_file", {
                    "path": "result.txt", "summary": "Fixture", "format": "text"})), self.finish_turn()])
        self.assertEqual(first["status"], "error")
        self.assertIs(first["complete"], False)
        self.assertEqual(first["outcome"], "effects_unknown")
        self.assertNotIn("Private disk failure", json.dumps(first))
        for result in (retry, workspace_retry):
            self.assertEqual(result["status"], "error")
            self.assertNotEqual(result.get("outcome"), "not_started")
        self.assertEqual(finish["code"], "operation_pending")
        materialize.assert_called_once()
        published, = self.events("deliverable.published")
        self.assertEqual(published.payload["content"], arguments["content"])
        artifact, = self.rt.task(self.task).deliverables
        self.assertEqual(artifact["seq"], published.seq)
        self.assertEqual(artifact["content"], arguments["content"])
        self.assertEqual(self.events("team.artifact_saved"), [])
        self.assertEqual(self.events("team.objective_completed"), [])
        self.assertEqual(list(self.rt._artifact_root.rglob("*")), [])
        start = self.events("team.tool_started")[0]
        pending = self.rt.team._pending_operations(self.task)
        original, = [row for row in pending if row["started_seq"] == start.seq]
        self.assertEqual(original["outcome"], "unknown")
        self.assertFalse(original["replay_authorized"])
        self.assertIn("finished_seq", original)
        self.assertIn(start.seq, finish["started_seqs"])
        self.assertEqual(self.rt.workspaces.pending(task_id=self.task), [])
        self.assertEqual(self.rt.team.view(self.task)["status"], "running")


if __name__ == "__main__":
    unittest.main()
