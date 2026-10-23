"""Native exact edits, actual Python checks and two scripted review reworks."""

import hashlib
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import zipfile

from cater.workspace_tools import WorkspaceExecutor
from cater.workspaces import WorkspaceService
import test_team_review_cycle as reviews


CHECK = ("import hashlib,json,pathlib; p=pathlib.Path('result.py'); raw=p.read_bytes(); ns={}; "
         "exec(compile(raw,'result.py','exec'),ns); value=ns['answer'](); "
         "print(json.dumps({'sha256':hashlib.sha256(raw).hexdigest(),'value':value})); "
         "raise SystemExit(0 if value==3 else 1)")


class WorkspaceEditCycleTests(unittest.TestCase):
    def test_native_batch_edits_record_one_effect_and_actual_final_file(self):
        f = reviews.TeamReviewCycleTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        project = f.root / 'project'
        project.mkdir()
        config = f.root / 'batch-workspaces.json'
        config.write_text(json.dumps({'workspaces': [{'id': 'project', 'title': 'Batch fixture',
            'root': str(project), 'mode': 'execute',
            'commands': {'python': [sys.executable, '-I', '-B']}, 'timeout': 5}]}), encoding='utf-8')
        f.rt.workspaces = WorkspaceService(f.rt, config)
        f.rt.workspaces.bind(f.task, 'project', expected_revision=None)
        original = 'alpha=1\r\nbeta=2\r\nprint(alpha+beta)\r\n'
        written, = f.exchange(f.worker, ('workspace_write', {
            'path': 'real.py', 'content': original, 'expected_sha256': None}))
        edits = [{'old_text': 'beta=2', 'content': 'beta=20'},
                 {'old_text': 'alpha=1', 'content': 'alpha=10'}]
        changed, = f.exchange(f.worker, ('workspace_write', {
            'path': 'real.py', 'edits': edits, 'expected_sha256': written['sha256']}))
        expected = b'alpha=10\r\nbeta=20\r\nprint(alpha+beta)\r\n'
        self.assertEqual(changed['status'], 'ok')
        self.assertEqual(changed['replacements'], 2)
        self.assertEqual(changed['sha256'], hashlib.sha256(expected).hexdigest())
        self.assertEqual((project / 'real.py').read_bytes(), expected)
        checked, = f.exchange(f.worker, ('workspace_run', {'command': 'python', 'args': ['real.py']}))
        self.assertEqual(checked['status'], 'ok')
        self.assertEqual(checked['outcome'], 'passed')
        self.assertIn('30', json.dumps(checked))
        starts = [e for e in f.rt.log.read(task_id=f.task) if e.type == 'team.tool_started'
                  and e.payload['name'] == 'workspace_write']
        self.assertEqual(len(starts), 2)
        arguments = starts[-1].payload['arguments']
        self.assertNotIn('edits', arguments)
        self.assertNotIn('beta=20', json.dumps(arguments))
        self.assertEqual(arguments['edit_count'], 2)
        self.assertEqual(f.rt.team._pending_operations(f.task), [])
        self.assertEqual(f.rt.workspaces.pending(task_id=f.task), [])
        self.assertTrue(f.rt.verify_chain().ok)
        self.assertEqual(f.protocol_errors, [])

    def test_file_removed_after_hash_is_a_prepublication_conflict_not_unknown(self):
        f = reviews.TeamReviewCycleTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        project = f.bind_workspace()
        written, = f.exchange(f.worker, ("workspace_write", {"path": "real.py",
            "content": "def answer():\n    return 1\n", "expected_sha256": None}))
        original = WorkspaceExecutor._check_expected
        removed = []

        def remove_after_check(executor, path, expected):
            result = original(executor, path, expected)
            if not removed:
                path.unlink()
                removed.append(True)
            return result

        with patch.object(WorkspaceExecutor, "_check_expected", new=remove_after_check), \
                patch("cater.workspace_tools.tempfile.mkstemp") as staging:
            result, = f.exchange(f.worker, ("workspace_write", {"path": "real.py", "old_text": "return 1",
                "content": "return 2", "expected_sha256": written["sha256"]}))
        self.assertTrue(removed)
        self.assertEqual(result["error"]["code"], "conflict")
        staging.assert_not_called()
        self.assertFalse((project / "real.py").exists())
        self.assertEqual(f.rt.team._pending_operations(f.task), [])
        self.assertEqual(f.rt.workspaces.pending(task_id=f.task), [])
        recreated, = f.exchange(f.worker, ("workspace_write", {"path": "real.py",
            "content": "def answer():\n    return 2\n", "expected_sha256": None}))
        self.assertEqual(recreated["status"], "ok")
        self.assertTrue(f.rt.verify_chain().ok)
        self.assertEqual(f.protocol_errors, [])

    def test_missing_edit_parent_does_not_lock_native_work_and_next_edit_succeeds(self):
        f = reviews.TeamReviewCycleTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        project = f.bind_workspace()
        written, = f.exchange(f.worker, ("workspace_write", {"path": "real.py",
            "content": "def answer():\n    return 1\n", "expected_sha256": None}))
        args = {"path": "missing/file.py", "old_text": "return 1", "content": "return 2",
                "expected_sha256": written["sha256"]}
        rejected, = f.exchange(f.worker, ("workspace_write", args))
        self.assertEqual(rejected["error"]["code"], "conflict")
        self.assertNotEqual(rejected.get("outcome"), "effects_unknown")
        self.assertEqual(f.rt.team._pending_operations(f.task), [])
        self.assertEqual(f.rt.workspaces.pending(task_id=f.task), [])
        self.assertFalse((project / "missing").exists())
        changed, = f.exchange(f.worker, ("workspace_write", {**args, "path": "real.py"}))
        self.assertEqual(changed["status"], "ok")
        self.assertEqual((project / "real.py").read_bytes(), b"def answer():\n    return 2\n")
        self.assertTrue(f.rt.verify_chain().ok)
        self.assertEqual(f.protocol_errors, [])

    def test_exact_edits_survive_two_reworks_and_deliver_only_approved_bytes(self):
        f = reviews.TeamReviewCycleTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        project = f.root / "project"
        project.mkdir()
        config = f.root / "edit-workspaces.json"
        config.write_text(json.dumps({"workspaces": [{"id": "project", "title": "Disposable edit fixture",
            "root": str(project), "mode": "execute", "commands": {"python": [sys.executable, "-I", "-c"]},
            "timeout": 5}]}), encoding="utf-8")
        f.rt.workspaces = WorkspaceService(f.rt, config)
        f.rt.workspaces.bind(f.task, "project", expected_revision=None)
        prefix = "# unchanged fixture line\r\n" * 800
        initial = prefix + "def answer():\r\n    return 1\r\n"
        written, = f.exchange(f.worker, ("workspace_write", {
            "path": "result.py", "content": initial, "expected_sha256": None}))
        self.assertEqual(written["status"], "ok")
        review_seq, observed, edits = None, [], []
        for expected in (1, 2, 3):
            if expected > 1:
                read, = f.exchange(f.worker, ("workspace_read", {"path": "result.py", "limit": 65536}))
                self.assertFalse(read["truncated"])
                self.assertTrue(read["content"].endswith(f"    return {expected - 1}\r\n"))
                args = {"path": "result.py", "old_text": f"return {expected - 1}",
                        "content": f"return {expected}", "expected_sha256": read["sha256"]}
                changed, = f.exchange(f.worker, ("workspace_write", args))
                self.assertEqual(changed["status"], "ok")
                self.assertEqual(changed["before_sha256"], read["sha256"])
                self.assertEqual(changed["replacements"], 1)
                edits.append(changed)
                full = {"path": "result.py", "content": initial.replace("return 1", f"return {expected}"),
                        "expected_sha256": read["sha256"]}
                self.assertLess(len(json.dumps(args).encode()), len(json.dumps(full).encode()))
            expected_bytes = initial.replace("return 1", f"return {expected}").encode()
            self.assertEqual((project / "result.py").read_bytes(), expected_bytes)
            publication, = f.exchange(f.worker, ("publish_workspace_file", {
                "path": "result.py", "summary": "Actual file for independent fixture review", "format": "code"}))
            self.assertEqual(publication["status"], "ok")
            seq = publication["artifact_seq"]
            request = f.request([seq], review_seq=review_seq)
            self.assertEqual(request["status"], "ok")
            review_seq = request["review_seq"]

            def decide(context, history, tools):
                source, result = [json.loads(row["content"]) for row in history if row["role"] == "tool"]
                self.assertEqual(source["content"].encode(), expected_bytes)
                self.assertEqual(result["status"], "ok" if expected == 3 else "error", result)
                if expected < 3:
                    self.assertEqual(result["error"]["code"], "failed")
                self.assertTrue(result["complete"])
                actual = json.loads(result["stdout"]["excerpt"])
                self.assertEqual(actual, {"sha256": hashlib.sha256(expected_bytes).hexdigest(), "value": expected})
                self.assertEqual(source["content_sha256"], actual["sha256"])
                self.assertEqual(result["exit_code"], 0 if expected == 3 else 1)
                self.assertEqual(result["outcome"], "passed" if expected == 3 else "failed")
                observed.append(actual)
                return f.native_turn("", ("record_review", {"review_seq": review_seq,
                    "verdict": "approved" if expected == 3 else "changes_requested",
                    "feedback": f"Actual checked value {expected}; expected 3."}))

            f.driver.steps[f.reviewer] = [f.native_turn("", ("read_artifact", {"artifact_seq": seq}),
                ("workspace_run", {"command": "python", "args": [CHECK]})), decide]
            claimed = f.rt.team._claim(f.task)
            self.assertEqual(claimed[0].seq, request["message_seq"])
            self.assertEqual(claimed[1].id, f.reviewer)
            list(f.rt.team._run_message(f.task, *claimed))
            self.assertFalse(f.driver.steps[f.reviewer])
            self.assertEqual(f.protocol_errors, [])
            self.assertEqual(f.rt.team_reviews.view(f.task)[0]["status"],
                             "approved" if expected == 3 else "changes_requested")
            f.acknowledge_latest_review()
        self.assertEqual(f.finish()["status"], "ok")
        self.assertEqual(len(edits), 2)
        self.assertEqual([row["value"] for row in observed], [1, 2, 3])
        artifact = f.rt.task(f.task).deliverables[-1]
        self.assertEqual(f.rt.team_reviews.view(f.task)[0]["artifacts"][0]["seq"], artifact["seq"])
        self.assertEqual(Path(artifact["local_path"]).read_bytes(), (project / "result.py").read_bytes())
        with zipfile.ZipFile(io.BytesIO(f.rt.team.archive(f.task))) as archive:
            self.assertEqual(archive.read("result.py"), (project / "result.py").read_bytes())
        self.assertTrue(f.rt.verify_chain().ok)
        self.assertEqual(f.rt.workspaces.pending(task_id=f.task), [])
        self.assertEqual(f.rt.team._pending_operations(f.task), [])
        edits_in_events = [event.payload["arguments"] for event in f.events("team.tool_started")
                          if event.payload["name"] == "workspace_write" and "old_text_bytes" in event.payload["arguments"]]
        self.assertEqual(len(edits_in_events), 2)
        self.assertTrue(all("old_text" not in row and "content" not in row for row in edits_in_events))


if __name__ == "__main__":
    unittest.main()
