"""Offline cwd regressions: scripted native turns, real local subprocesses."""

from __future__ import annotations

import copy
from contextlib import contextmanager
import errno
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from cater.runtime import Runtime
from cater.team import HOST
from cater.tool_protocol import ToolCall, ToolTurn
from cater.workspace_tools import WorkspaceExecutor
import cater.workspace_tools as workspace_tools
from test_simple_team import TeamDriver


class _TeamFixture:
    """Compose disposable runtime resources without inheriting existing tests."""

    def __init__(self, case):
        self.case = case
        temporary = tempfile.TemporaryDirectory(prefix="cater-cwd-regression-")
        case.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.root = self.home / "project"
        self.root.mkdir()
        self.db = self.home / "events.db"
        self.config = self.home / "workspaces.json"
        self.config.write_text(json.dumps({"workspaces": [{
            "id": "project", "title": "Cwd fixture", "root": str(self.root),
            "mode": "execute", "commands": {"python": [sys.executable, "-I", "-B"]},
            "timeout": 5,
        }]}), encoding="utf-8")
        roles = json.loads((Path(__file__).resolve().parents[1]
                            / "cater/packs/default_roles.json").read_text(encoding="utf-8"))
        roles["organization"]["leader"]["driver"] = "team_test"
        for role in roles["roles"]:
            role["driver"] = "team_test"
        self.roles = self.home / "roles.json"
        self.roles.write_text(json.dumps(roles), encoding="utf-8")
        self.driver = TeamDriver()
        self.rt = self.reopen()
        self.task = self.rt.create_task("Offline command cwd fixture")
        self.manager = self.rt.organization.leader.id
        self.rt.team.enable(self.task)
        self.rt.workspaces.bind(self.task, "project", expected_revision=None)
        self.number = 0
        self.files = {
            "package/check.py": (
                b"from pathlib import Path\nimport sys\n"
                b"assert Path('data.bin').read_bytes() == b'\\x00\\xff'\n"
                b"Path('executed.txt').write_bytes(b'command really ran')\n"
                b"print('cwd-check-passed')\nprint('cwd-check-stderr', file=sys.stderr)\n"
            ),
            "package/data.bin": b"\x00\xff",
            "package/empty.txt": b"",
        }
        for name, body in self.files.items():
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)

    def reopen(self):
        runtime = Runtime(self.db, roles_path=self.roles, workspaces_path=self.config,
                          drivers={"team_test": self.driver})
        self.case.addCleanup(runtime.close)
        return runtime

    def events(self, kind, runtime=None):
        return list((runtime or self.rt).log.iter_events(task_id=self.task, event_types=(kind,)))

    def turn(self, name=None, arguments=None):
        self.number += 1
        calls = [] if name is None else [ToolCall(f"cwd-{self.number}", name, arguments)]
        return ToolTurn("Observed local fixture output" if not calls else "", calls, 1,
                        {"token_usage": "reported"}, {
                            "role": "assistant", "content": "", "tool_calls": [
                                {"id": c.id, "name": c.name, "arguments": c.arguments} for c in calls],
                        })

    def exchange(self, *turns):
        before = len(self.events("team.tool_finished"))
        self.driver.steps[self.manager] = [*turns, self.turn()]
        self.rt.team.receive(self.task, "Run offline fixture", request_id=f"cwd-request-{self.number}")
        self.rt.team.run(self.task)
        self.case.assertEqual(self.driver.steps[self.manager], [])
        self.case.assertNotIn("failed", [e.payload["status"] for e in self.events("team.run_finished")])
        return self.events("team.tool_finished")[before:]

    def publish_zip(self):
        event, = self.exchange(self.turn("publish_workspace_archive", {
            "filename": "project.zip", "summary": "Unmodified fixture inputs", "paths": list(self.files),
        }))
        self.case.assertEqual(event.payload["result"]["status"], "ok", event.payload)
        seq = event.payload["result"]["artifact_seq"]
        artifact = next(a for a in self.rt.task(self.task).deliverables if a["seq"] == seq)
        return seq, Path(artifact["local_path"])

    def arguments(self, cwd="package", artifact_seq=None):
        arguments = {"command": "python", "args": ["check.py"], "cwd": cwd}
        if artifact_seq is not None:
            arguments["artifact_seqs"] = [artifact_seq]
        return arguments


class _UnreadableWorkerResult(io.BytesIO):
    def read(self, *args, **kwargs):
        raise OSError("PRIVATE_RESULT_STREAM_FAILURE")


class WorkspaceCommandCwdTests(unittest.TestCase):
    def assert_not_started(self, result, code="cwd_unavailable"):
        self.assertEqual(result["status"], "error", result)
        self.assertEqual(result.get("error", {}).get("code"), code, result)
        self.assertIs(result.get("complete"), True, result)
        self.assertEqual(result.get("outcome"), "not_started", result)
        self.assertEqual(result.get("invalid_fields"), ["cwd"], result)

    def corrected_run(self, *, artifact_copy=False, bad_cwd="missing",
                      code="cwd_unavailable", message=None, permission_denied=False):
        f = _TeamFixture(self)
        seq, archive = f.publish_zip() if artifact_copy else (None, None)
        archive_bytes = archive.read_bytes() if archive else None
        if artifact_copy:
            (f.root / "package/check.py").write_bytes(b"raise RuntimeError('unpublished source')\n")
            (f.root / "package/data.bin").write_bytes(b"unpublished input")
        live_files = {name: (f.root / name).read_bytes() for name in f.files}
        original_events = f.rt.log.read(task_id=f.task)
        first_args = f.arguments(bad_cwd, seq)
        original_args = copy.deepcopy(first_args)
        real_popen = subprocess.Popen
        real_inspect = workspace_tools._inspect

        def inspect(path, directory=None):
            if permission_denied and path.name == "denied" and directory is True:
                raise PermissionError(errno.EACCES, "PRIVATE_CWD_PERMISSION", str(path))
            return real_inspect(path, directory)

        with patch.object(workspace_tools, "_inspect", side_effect=inspect), \
                patch.object(workspace_tools.subprocess, "Popen", wraps=real_popen) as popen:
            def correct(context, history, tools):
                self.assertEqual(popen.call_count, 0, "Invalid cwd must never reach real Popen")
                self.assertEqual(json.loads(history[-1]["content"]),
                                 f.events("team.tool_finished")[-1].payload["result"])
                self.assertEqual(f.rt.workspaces.pending(task_id=f.task), [])
                self.assertEqual(f.rt.team._pending_operations(f.task), [])
                return f.turn("workspace_run", f.arguments("package", seq))

            receipts = f.exchange(f.turn("workspace_run", first_args), correct)
        self.assertEqual(len(receipts), 2)
        bad, good = [e.payload["result"] for e in receipts]
        # Check continuation before the error shape, so the initial failure shows the lock.
        self.assertEqual(good["status"], "ok", {"first": bad, "second": good, "popen_calls": popen.call_count})
        self.assertEqual(popen.call_count, 1)
        self.assertEqual(good["outcome"], "passed")
        self.assertEqual(good["exit_code"], 0)
        self.assertEqual(good["cwd"], "package")
        self.assertIs(good["complete"], True)
        self.assertIn("cwd-check-passed", good["stdout"]["excerpt"])
        self.assertIn("cwd-check-stderr", good["stderr"]["excerpt"])
        self.assertEqual(good["argv"], [sys.executable, "-I", "-B", "check.py"])
        if code == "cwd_unavailable":
            self.assert_not_started(bad)
            self.assertNotIn(str(f.root), json.dumps(bad))
            self.assertNotIn("PRIVATE_CWD_PERMISSION", json.dumps(bad))
        else:
            self.assertEqual(bad["status"], "error", bad)
            self.assertEqual(bad["error"], {"code": code, "message": message})
            # Existing safety refusals may additionally declare the prelaunch boundary.
            self.assertIn(bad.get("outcome"), (None, "not_started"))
            self.assertIn(bad.get("complete"), (None, True))
        self.assertEqual(first_args, original_args)
        self.assertEqual([e.payload["run_id"] for e in receipts], [receipts[0].payload["run_id"]] * 2)
        starts = {e.seq: e for e in f.events("team.tool_started")}
        self.assertEqual(starts[receipts[0].payload["started_seq"]].payload["arguments"], original_args)
        for receipt in receipts:
            start = starts[receipt.payload["started_seq"]]
            self.assertEqual(start.actor, HOST)
            self.assertEqual(receipt.actor, HOST)
            self.assertLess(start.seq, receipt.seq)
            for key in ("run_id", "role", "name"):
                self.assertEqual(start.payload[key], receipt.payload[key])
        for name, body in live_files.items():
            self.assertEqual((f.root / name).read_bytes(), body)
        self.assertEqual(f.rt.log.read(task_id=f.task)[:len(original_events)], original_events)
        if archive:
            self.assertEqual(archive.read_bytes(), archive_bytes)
            with zipfile.ZipFile(io.BytesIO(archive_bytes)) as zipped:
                for name, body in f.files.items():
                    self.assertEqual(zipped.read(name), body)
            self.assertFalse((f.root / "package/executed.txt").exists())
            self.assertNotEqual(Path(popen.call_args.kwargs["cwd"]).parent, f.root)
            self.assertFalse(Path(popen.call_args.kwargs["cwd"]).exists())
            for result in (bad, good):
                self.assertEqual(result["environment"], "temporary_artifact_copy_not_sandbox")
                self.assertTrue(result["artifacts_unchanged"])
                self.assertTrue(result["artifact_check_valid"])
                self.assertEqual(result["checked_artifacts"][0]["seq"], seq)
                self.assertEqual({row["path"]: row["sha256"] for row in result["checked_files"]},
                                 {name: hashlib.sha256(body).hexdigest() for name, body in f.files.items()})
        else:
            self.assertEqual(Path(popen.call_args.kwargs["cwd"]), f.root / "package")
            self.assertEqual((f.root / "package/executed.txt").read_bytes(), b"command really ran")
        final_history = f.driver.calls[-1]["history"]
        self.assertEqual([json.loads(row["content"]) for row in final_history if row["role"] == "tool"],
                         [bad, good])
        expected_receipts = f.events("team.tool_finished")
        self.assertEqual(f.rt.workspaces.pending(task_id=f.task), [])
        self.assertEqual(f.rt.team._pending_operations(f.task), [])
        self.assertTrue(f.rt.verify_chain().ok)
        f.rt.close()
        reopened = f.reopen()
        self.assertEqual(reopened.workspaces.pending(task_id=f.task), [])
        self.assertEqual(reopened.team._pending_operations(f.task), [])
        self.assertEqual(f.events("team.tool_finished", reopened), expected_receipts)
        self.assertTrue(reopened.verify_chain().ok)

    def test_native_missing_cwd_can_be_corrected_in_same_run(self):
        self.corrected_run()

    def test_published_zip_missing_cwd_can_be_corrected_in_same_run(self):
        self.corrected_run(artifact_copy=True)

    def test_missing_parent_and_workspace_title_prefix_are_correctable(self):
        for artifact_copy in (False, True):
            for cwd in ("missing/child", "project/package"):
                with self.subTest(artifact_copy=artifact_copy, cwd=cwd):
                    self.corrected_run(artifact_copy=artifact_copy, bad_cwd=cwd)

    def test_permission_denied_at_cwd_inspection_is_correctable_without_launch(self):
        for artifact_copy in (False, True):
            for cwd in ("denied", "denied/child"):
                with self.subTest(artifact_copy=artifact_copy, cwd=cwd):
                    self.corrected_run(artifact_copy=artifact_copy, bad_cwd=cwd, permission_denied=True)

    def test_safety_refusals_keep_original_code_and_message_without_launch(self):
        cases = [
            (".git", "protected_path", "Protected paths are forbidden."),
            ("package/.env", "protected_path", "Protected paths are forbidden."),
            ("../outside", "invalid_path", "Unsafe path component."),
            ("/outside", "invalid_path", "Absolute paths are forbidden."),
            ("C:\\outside", "invalid_path", "Absolute paths are forbidden."),
            ("package/check.py", "wrong_type", "Unexpected filesystem entry type."),
            ("package/check.py/child", "wrong_type", "Unexpected filesystem entry type."),
        ]
        for artifact_copy in (False, True):
            for cwd, code, message in cases:
                with self.subTest(artifact_copy=artifact_copy, cwd=cwd):
                    self.corrected_run(artifact_copy=artifact_copy, bad_cwd=cwd, code=code, message=message)

    def test_root_cwd_and_explicit_dot_keep_relative_semantics(self):
        f = _TeamFixture(self)
        executor = WorkspaceExecutor(f.rt.workspaces.config["project"]["policy"])
        for arguments in ({"command": "python", "args": ["-c", "print('root-cwd')"]},
                          {"command": "python", "args": ["-c", "print('root-cwd')"], "cwd": "."}):
            with self.subTest(arguments=arguments):
                result = executor.execute("workspace_run", arguments)
                self.assertEqual(result["status"], "ok", result)
                self.assertEqual(result["cwd"], ".")
                self.assertIn("root-cwd", result["stdout"]["excerpt"])

    def test_oserror_outside_cwd_boundary_keeps_generic_error(self):
        f = _TeamFixture(self)
        executor = WorkspaceExecutor(f.rt.workspaces.config["project"]["policy"])

        def unavailable_temp():
            raise OSError("PRIVATE_ENV_FAILURE")

        with patch.object(workspace_tools, "tempfile", SimpleNamespace(gettempdir=unavailable_temp)), \
                patch.object(workspace_tools.subprocess, "Popen", wraps=subprocess.Popen) as popen:
            result = executor.execute("workspace_run", f.arguments())
        popen.assert_not_called()
        self.assertEqual(result["error"]["code"], "operation_failed", result)
        self.assertNotEqual(result.get("outcome"), "not_started")
        self.assertNotIn("PRIVATE_ENV_FAILURE", json.dumps(result))
        read = executor.execute("workspace_read", {"path": "missing.txt"})
        self.assertEqual(read["error"]["code"], "operation_failed", read)
        self.assertNotEqual(read.get("outcome"), "not_started")

    def test_popen_oserror_is_not_reclassified_as_cwd_unavailable(self):
        f = _TeamFixture(self)
        with patch.object(workspace_tools.subprocess, "Popen",
                          side_effect=OSError("PRIVATE_POPEN_FAILURE")) as popen:
            event, = f.exchange(f.turn("workspace_run", f.arguments()))
        popen.assert_called_once()
        result = event.payload["result"]
        self.assertEqual(result["status"], "error", result)
        self.assertNotEqual(result.get("outcome"), "not_started")
        self.assertNotEqual(result["error"]["code"], "cwd_unavailable")
        self.assertEqual(result["outcome"], "launch_error")
        self.assertEqual(result["error"]["code"], "process_failed")
        self.assertIs(result["complete"], False)
        self.assertNotIn("PRIVATE_POPEN_FAILURE", json.dumps(result))
        self.assertFalse((f.root / "package/executed.txt").exists())
        original = next(e for e in f.events("team.tool_started")
                        if e.seq == event.payload["started_seq"])
        self.assert_only_original_pending(f, original)

    def test_result_read_oserror_after_real_execution_is_not_a_cwd_rejection(self):
        for artifact_copy in (False, True):
            with self.subTest(artifact_copy=artifact_copy):
                self.untrusted_worker_result(PermissionError("PRIVATE_RESULT_READ_FAILURE"), artifact_copy)

    def untrusted_worker_result(self, replacement, artifact_copy=False, worker_returncode=None,
                                expected_outcome="effects_unknown"):
        f = _TeamFixture(self)
        seq, archive = f.publish_zip() if artifact_copy else (None, None)
        archive_bytes = archive.read_bytes() if archive else None
        real_open = Path.open
        real_popen = subprocess.Popen
        processes = []
        observed_markers = []

        def start_process(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            processes.append(process)
            return process

        def replace_result(path, mode="r", *args, **kwargs):
            if path.name != "result.json" or not path.parent.name.startswith("cater-workspace-run-") or mode != "rb":
                return real_open(path, mode, *args, **kwargs)
            marker = Path(popen.call_args.kwargs["cwd"]) / "executed.txt"
            observed_markers.append(marker.read_bytes())
            if worker_returncode is not None:
                processes[0].returncode = worker_returncode
            if isinstance(replacement, OSError):
                raise replacement
            if replacement is None:
                path.unlink()
                return real_open(path, mode, *args, **kwargs)
            if isinstance(replacement, io.BytesIO):
                return replacement
            return io.BytesIO(replacement)

        with patch.object(Path, "open", replace_result), \
                patch.object(workspace_tools.subprocess, "Popen", side_effect=start_process) as popen:
            event, = f.exchange(f.turn("workspace_run", f.arguments(artifact_seq=seq)))
        self.assertEqual(popen.call_count, 1)
        self.assertEqual(observed_markers, [b"command really ran"])
        result = event.payload["result"]
        original = next(e for e in f.events("team.tool_started")
                        if e.seq == event.payload["started_seq"])
        # A missing pending entry would authorize repeating the already observed write.
        self.assert_only_original_pending(f, original, artifact_seq=seq)
        self.assertEqual(result["status"], "error", result)
        self.assertEqual(result["outcome"], expected_outcome, result)
        self.assertEqual(result["error"]["code"], "process_failed", result)
        self.assertIsNone(result["exit_code"])
        self.assertEqual(result["argv"], [])
        self.assertFalse(result["timed_out"])
        self.assertIs(result["complete"], True, "Finished output capture is distinct from trusted execution status")
        self.assertNotIn("PRIVATE_", json.dumps(result))
        for name, expected in (("stdout", "cwd-check-passed"), ("stderr", "cwd-check-stderr")):
            stream = result[name]
            self.assertIn(expected, stream["excerpt"])
            self.assertIs(stream["complete"], True)
            self.assertIs(stream["utf8_valid"], True)
            self.assertFalse(stream["truncated"])
            captured = stream["excerpt"].encode("utf-8")
            self.assertEqual(stream["sha256"], hashlib.sha256(captured).hexdigest())
            self.assertEqual(stream["bytes"], len(captured))
        for name, body in f.files.items():
            self.assertEqual((f.root / name).read_bytes(), body)
        if archive:
            self.assertEqual(archive.read_bytes(), archive_bytes)
            self.assertFalse((f.root / "package/executed.txt").exists())
            self.assertTrue(result["artifact_check_valid"])
        else:
            self.assertEqual((f.root / "package/executed.txt").read_bytes(), b"command really ran")

    def test_missing_worker_result_after_real_write_stays_pending(self):
        for artifact_copy in (False, True):
            with self.subTest(artifact_copy=artifact_copy):
                self.untrusted_worker_result(None, artifact_copy)

    def test_worker_result_stream_oserror_after_real_write_stays_pending(self):
        for artifact_copy in (False, True):
            with self.subTest(artifact_copy=artifact_copy):
                self.untrusted_worker_result(_UnreadableWorkerResult(), artifact_copy)

    def test_malformed_worker_json_after_real_write_stays_pending(self):
        for artifact_copy in (False, True):
            for body in (b"", b"{", b'{"exit_code":', b"\xff", b"PRIVATE_INVALID_JSON"):
                with self.subTest(artifact_copy=artifact_copy, body=body):
                    self.untrusted_worker_result(body, artifact_copy)

    def test_duplicate_keys_and_nonfinite_worker_json_after_real_write_stay_pending(self):
        bodies = [b'{"exit_code":7,"exit_code":0}', b'{"exit_code":NaN}',
                  b'{"exit_code":Infinity}', b'{"exit_code":-Infinity}']
        for artifact_copy in (False, True):
            for body in bodies:
                with self.subTest(artifact_copy=artifact_copy, body=body):
                    self.untrusted_worker_result(body, artifact_copy)

    def test_oversized_worker_result_after_real_write_stays_pending(self):
        body = b'{"exit_code":0}'.ljust(8193, b" ")
        for artifact_copy in (False, True):
            with self.subTest(artifact_copy=artifact_copy):
                self.untrusted_worker_result(body, artifact_copy)

    def test_nonzero_worker_exit_cannot_certify_a_success_receipt(self):
        for artifact_copy in (False, True):
            with self.subTest(artifact_copy=artifact_copy):
                self.untrusted_worker_result(b'{"exit_code":0}', artifact_copy, worker_returncode=1)

    def test_untrusted_worker_shapes_after_real_write_stay_pending(self):
        values = [{}, [], None, True, 0, "PRIVATE_WRONG_SHAPE"]
        values += [{"exit_code": value} for value in (None, True, False, "0", 0.0, [], {})]
        values += [
            {"launch_error": "PRIVATE_MISSING_EXIT_CODE"},
            {"exit_code": 0, "extra": "PRIVATE_UNEXPECTED_FIELD"},
            {"exit_code": 0, "launch_error": None},
            {"exit_code": 0, "launch_error": "PRIVATE_CONTRADICTORY_RESULT"},
            *[{"exit_code": None, "launch_error": value} for value in (None, False, 1, [])],
        ]
        for artifact_copy in (False, True):
            for value in values:
                with self.subTest(artifact_copy=artifact_copy, value=value):
                    self.untrusted_worker_result(json.dumps(value).encode("utf-8"), artifact_copy)

    def test_worker_launch_error_after_real_write_keeps_diagnostic_and_pending(self):
        body = b'{"exit_code":null,"launch_error":"PRIVATE_POST_RUN_ERROR"}'
        for artifact_copy in (False, True):
            with self.subTest(artifact_copy=artifact_copy):
                self.untrusted_worker_result(body, artifact_copy, expected_outcome="launch_error")

    def test_explicit_worker_launch_error_keeps_diagnostic_and_pending(self):
        for artifact_copy in (False, True):
            with self.subTest(artifact_copy=artifact_copy):
                f = _TeamFixture(self)
                seq = f.publish_zip()[0] if artifact_copy else None
                config = json.loads(f.config.read_text(encoding="utf-8"))
                config["workspaces"][0]["commands"]["missing"] = [str(f.home / "PRIVATE_MISSING_BINARY")]
                f.config.write_text(json.dumps(config), encoding="utf-8")
                revision = f.rt.workspaces.view(f.task)["revision"]
                f.rt.workspaces.bind(f.task, "project", expected_revision=revision)
                missing = {**f.arguments(artifact_seq=seq), "command": "missing", "args": []}
                with patch.object(workspace_tools.subprocess, "Popen", wraps=subprocess.Popen) as popen:
                    first, second = f.exchange(f.turn("workspace_run", missing),
                                               f.turn("workspace_run", f.arguments(artifact_seq=seq)))
                self.assertEqual(popen.call_count, 1)
                result = first.payload["result"]
                self.assertEqual((result["status"], result["outcome"]), ("error", "launch_error"))
                self.assertEqual(result["error"]["code"], "process_failed")
                self.assertIsNone(result["exit_code"])
                self.assertIs(result["complete"], True)
                self.assertNotIn("PRIVATE_MISSING_BINARY", json.dumps(result))
                self.assertEqual(second.payload["result"]["status"], "error", second.payload)
                self.assertFalse((f.root / "package/executed.txt").exists())
                original = next(e for e in f.events("team.tool_started")
                                if e.seq == first.payload["started_seq"])
                self.assert_only_original_pending(f, original, artifact_seq=seq)

    def test_nonzero_exit_keeps_output_and_failed_outcome(self):
        for artifact_copy in (False, True):
            with self.subTest(artifact_copy=artifact_copy):
                f = _TeamFixture(self)
                seq = f.publish_zip()[0] if artifact_copy else None
                arguments = f.arguments(artifact_seq=seq)
                arguments["args"] = ["-c", "import sys; print('real-failure'); print('real-stderr', file=sys.stderr); sys.exit(7)"]
                with patch.object(workspace_tools.subprocess, "Popen", wraps=subprocess.Popen) as popen:
                    event, = f.exchange(f.turn("workspace_run", arguments))
                self.assertEqual(popen.call_count, 1)
                result = event.payload["result"]
                self.assertEqual((result["status"], result["outcome"], result["exit_code"]), ("error", "failed", 7))
                self.assertEqual(result["error"]["code"], "failed")
                self.assertIs(result["complete"], True)
                self.assertIn("real-failure", result["stdout"]["excerpt"])
                self.assertIn("real-stderr", result["stderr"]["excerpt"])
                self.assertEqual(f.rt.team._pending_operations(f.task), [])

    def assert_only_original_pending(self, f, original, artifact_seq=None):
        expected_receipts = f.events("team.tool_finished")
        f.rt.close()
        f.rt = f.reopen()
        self.assertEqual(f.events("team.tool_finished"), expected_receipts)
        self.assertEqual([e.seq for e in f.rt.workspaces.pending(task_id=f.task)], [original.seq])
        self.assertEqual([row["started_seq"] for row in f.rt.team._pending_operations(f.task)], [original.seq])
        self.assertTrue(f.rt.verify_chain().ok)
        with patch.object(workspace_tools.subprocess, "Popen", wraps=subprocess.Popen) as popen:
            blocked = f.exchange(f.turn("workspace_run", f.arguments("missing", artifact_seq)),
                                 f.turn("workspace_run", f.arguments("package", artifact_seq)))
        popen.assert_not_called()
        for event in blocked:
            self.assertEqual(event.payload["result"]["status"], "error")
        self.assertEqual([row["started_seq"] for row in f.rt.team._pending_operations(f.task)], [original.seq])

    def test_real_timeout_cannot_be_cleared_by_changing_cwd(self):
        for artifact_copy in (False, True):
            with self.subTest(artifact_copy=artifact_copy):
                f = _TeamFixture(self)
                seq = f.publish_zip()[0] if artifact_copy else None
                arguments = f.arguments(artifact_seq=seq)
                arguments.update(args=["-c", "import time; print('real-timeout-started', flush=True); time.sleep(30)"], timeout=1)
                with patch.object(workspace_tools.subprocess, "Popen", wraps=subprocess.Popen) as popen:
                    events = f.exchange(f.turn("workspace_run", arguments),
                                        f.turn("workspace_run", f.arguments("missing", seq)),
                                        f.turn("workspace_run", f.arguments("package", seq)))
                self.assertEqual(popen.call_count, 1)
                result = events[0].payload["result"]
                self.assertEqual((result["status"], result["outcome"]), ("error", "timeout"))
                self.assertTrue(result["timed_out"])
                self.assertIn("real-timeout-started", result["stdout"]["excerpt"])
                for event in events[1:]:
                    self.assertEqual(event.payload["result"]["status"], "error")
                    self.assertNotEqual(event.payload["result"].get("outcome"), "not_started")
                original = next(e for e in f.events("team.tool_started")
                                if e.seq == events[0].payload["started_seq"])
                self.assert_only_original_pending(f, original)

    def test_real_execution_then_cleanup_oserror_stays_unknown_after_cwd_correction(self):
        for artifact_copy in (False, True):
            with self.subTest(artifact_copy=artifact_copy):
                f = _TeamFixture(self)
                seq, archive = f.publish_zip() if artifact_copy else (None, None)
                saved_archive = archive.read_bytes() if archive else None
                real_temporary = tempfile.TemporaryDirectory

                @contextmanager
                def cleanup_failure(*args, **kwargs):
                    with real_temporary(*args, **kwargs) as path:
                        yield path
                    raise OSError("PRIVATE_POST_EXECUTION_CLEANUP")

                temporary_api = SimpleNamespace(gettempdir=tempfile.gettempdir, TemporaryDirectory=cleanup_failure)
                with patch.object(workspace_tools, "tempfile", temporary_api), \
                        patch.object(workspace_tools.subprocess, "Popen", wraps=subprocess.Popen) as popen:
                    events = f.exchange(f.turn("workspace_run", f.arguments("missing", seq)),
                                        f.turn("workspace_run", f.arguments("package", seq)),
                                        f.turn("workspace_run", f.arguments("missing", seq)),
                                        f.turn("workspace_run", f.arguments("package", seq)),
                                        f.turn("finish_work", {"summary": "Must stay blocked", "verification": "Unknown effects"}))
                self.assertEqual(popen.call_count, 1)
                self.assert_not_started(events[0].payload["result"])
                result = events[1].payload["result"]
                self.assertEqual(result["outcome"], "effects_unknown", result)
                self.assertIs(result["complete"], False)
                self.assertNotIn("PRIVATE_POST_EXECUTION_CLEANUP", json.dumps(result))
                if archive:
                    self.assertEqual(archive.read_bytes(), saved_archive)
                else:
                    self.assertEqual((f.root / "package/executed.txt").read_bytes(), b"command really ran")
                for event in events[2:4]:
                    self.assertEqual(event.payload["result"]["status"], "error")
                    self.assertNotEqual(event.payload["result"].get("outcome"), "not_started")
                self.assertEqual(events[4].payload["result"]["code"], "operation_pending")
                self.assertEqual(f.events("team.objective_completed"), [])
                original = next(e for e in f.events("team.tool_started")
                                if e.seq == events[1].payload["started_seq"])
                self.assert_only_original_pending(f, original)


if __name__ == "__main__":
    unittest.main()
