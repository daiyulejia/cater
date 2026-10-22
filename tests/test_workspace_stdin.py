"""Independent stdin contract regression: real children, disposable data only."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from cater import workspace_tools
from cater.workspace_tools import WorkspaceExecutor, WorkspacePolicy


NOTE = (
    "\u914d\u7f6e\u7ed9\u547d\u4ee4\u7684\u8f93\u5165\uff0c"
    "\u4e0d\u8bc1\u660e\u76ee\u6807\u7a0b\u5e8f\u5df2\u8bfb\u53d6\u6216\u6267\u884c\u3002"
)
READ_BYTES = (
    "import hashlib,json,sys; data=sys.stdin.buffer.read(); "
    "print(json.dumps({'hex':data.hex(),'bytes':len(data),"
    "'sha256':hashlib.sha256(data).hexdigest(),"
    "'eof':sys.stdin.buffer.read(1)==b''}))"
)
LIMIT_INPUT = "\u4e2d" * 21845 + "\0"


def stdin_metadata(text):
    body = text.encode("utf-8")
    return {"encoding": "utf-8", "bytes": len(body),
            "sha256": hashlib.sha256(body).hexdigest(), "note": NOTE}


class WorkspaceStdinTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="cater-stdin-regression-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "workspace"
        self.root.mkdir()
        self.original_cwd = Path.cwd()

    def tearDown(self):
        self.assertEqual(Path.cwd(), self.original_cwd)

    def executor(self, script=READ_BYTES, *, prefix=None, mode="execute"):
        self.prefix = prefix or [sys.executable, "-I", "-B", "-X", "utf8", "-c", script]
        return WorkspaceExecutor(WorkspacePolicy.from_dict({
            "root": str(self.root), "mode": mode, "timeout": 8,
            "commands": {"probe": self.prefix} if mode == "execute" else {},
        }))

    def run_input(self, text, script=READ_BYTES, **arguments):
        executor = self.executor(script)
        before = self.tree()
        result = executor.execute("workspace_run", {"command": "probe", "stdin": text, **arguments})
        self.assertEqual(self.tree(), before, "stdin must not create or modify workspace files")
        return result

    def tree(self):
        return {str(path.relative_to(self.root)): (None if path.is_dir() else path.read_bytes())
                for path in self.root.rglob("*")}

    def assert_success(self, result):
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual((result["outcome"], result["exit_code"]), ("passed", 0))
        self.assertIs(result["complete"], True)
        self.assertIs(result["timed_out"], False)

    def assert_capture(self, capture, body):
        self.assertEqual(capture["bytes"], len(body))
        self.assertEqual(capture["sha256"], hashlib.sha256(body).hexdigest())
        self.assertIs(capture["complete"], True)
        self.assertIs(capture["utf8_valid"], True)

    def test_schema_declares_optional_nonnullable_string(self):
        schema = next(row["input_schema"] for row in self.executor().schemas()
                      if row["name"] == "workspace_run")
        self.assertIn("stdin", schema["properties"])
        self.assertEqual(schema["properties"]["stdin"]["type"], "string")
        self.assertNotIn("stdin", schema["required"])
        self.assertFalse(schema["additionalProperties"])

    def test_raw_utf8_nul_crlf_no_bom_or_added_newline_and_eof(self):
        text = "private-stdin-body:\u4e2d\U0001f642\0\r\nlast"
        result = self.run_input(text)
        self.assert_success(result)
        body = text.encode("utf-8")
        self.assertEqual(json.loads(result["stdout"]["excerpt"]), {
            "hex": body.hex(), "bytes": len(body),
            "sha256": hashlib.sha256(body).hexdigest(), "eof": True,
        })
        self.assertEqual(result["stdin"], stdin_metadata(text))
        self.assertNotIn(text, json.dumps(result, ensure_ascii=False))
        self.assertEqual(result["argv"], self.prefix)

    def test_explicit_empty_is_distinct_from_omission_and_delivers_eof(self):
        executor = self.executor()
        omitted = executor.execute("workspace_run", {"command": "probe"})
        explicit = executor.execute("workspace_run", {"command": "probe", "stdin": ""})
        self.assert_success(omitted)
        self.assert_success(explicit)
        self.assertNotIn("stdin", omitted)
        self.assertEqual(explicit["stdin"], stdin_metadata(""))
        self.assertEqual(omitted["stdout"], explicit["stdout"])
        self.assertTrue(json.loads(explicit["stdout"]["excerpt"])["eof"])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_omitted_stdin_preserves_devnull_and_adds_only_host_launch_context(self):
        result = self.executor().execute("workspace_run", {"command": "probe"})
        self.assert_success(result)
        self.assertEqual(set(result), {
            "status", "command", "cwd", "argv", "outcome", "exit_code", "timed_out",
            "duration_ms", "stdout", "stderr", "bytes", "truncated", "complete",
            "process_scope", "output", "execution_context",
        })
        self.assertEqual(result["execution_context"], {"version": 1, "invocation": "direct_argv",
            "environment": "minimal_with_allowlist", "path_policy": "os_default",
            "platform": sys.platform if sys.platform in {"win32", "linux", "darwin"} else "other"})
        self.assertEqual(json.loads(result["stdout"]["excerpt"])["hex"], "")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_exact_65536_utf8_bytes_are_accepted(self):
        script = "import hashlib,sys; d=sys.stdin.buffer.read(); print(len(d),hashlib.sha256(d).hexdigest())"
        self.assertEqual(len(LIMIT_INPUT.encode("utf-8")), 65536)
        result = self.run_input(LIMIT_INPUT, script)
        self.assert_success(result)
        metadata = stdin_metadata(LIMIT_INPUT)
        self.assertEqual(result["stdin"], metadata)
        self.assertEqual(result["stdout"]["excerpt"].strip(), f"65536 {metadata['sha256']}")

    def test_invalid_stdin_never_dispatches_or_launches(self):
        executor = self.executor()
        cases = [None, True, 3, 1.5, b"bytes", ["text"], {"text": "value"},
                 "\ud800", "\udfff", "x" * 65537, "\u4e2d" * 21845 + "ab"]
        for index, value in enumerate(cases):
            with self.subTest(case=index), \
                    patch.object(executor, "_run", side_effect=AssertionError("invalid input dispatched")) as run, \
                    patch.object(workspace_tools.subprocess, "Popen", side_effect=AssertionError("invalid input launched")) as launch:
                result = executor.execute("workspace_run", {"command": "probe", "stdin": value})
                self.assertEqual(result["status"], "error", result)
                self.assertEqual(result["error"]["code"], "invalid_arguments", result)
                self.assertEqual(result["outcome"], "not_started")
                self.assertIs(result["complete"], True)
                self.assertIn("stdin", result["invalid_fields"])
                self.assertNotIn("stdin", result)
                run.assert_not_called()
                launch.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_stdin_does_not_relax_alias_args_or_timeout_preflight(self):
        executor = self.executor()
        cases = [
            {"command": sys.executable}, {"args": [False]}, {"args": ["\ud800"]},
            {"args": ["x" * 8193]}, {"timeout": None}, {"timeout": True}, {"timeout": 9},
        ]
        for changes in cases:
            with self.subTest(changes=changes), patch.object(executor, "_run") as run:
                result = executor.execute("workspace_run", {"command": "probe", "stdin": "valid", **changes})
                self.assertEqual(result["error"]["code"], "invalid_arguments", result)
                self.assertEqual(result["outcome"], "not_started")
                run.assert_not_called()

    def test_execute_permission_remains_required(self):
        for mode in ("read_only", "edit"):
            with self.subTest(mode=mode):
                executor = self.executor(mode=mode)
                with patch.object(executor, "_run") as run:
                    result = executor.execute("workspace_run", {"command": "probe", "stdin": "valid"})
                    self.assertEqual(result["error"]["code"], "tool_denied")
                    run.assert_not_called()

    def test_cwd_literal_arguments_and_fixed_prefix_are_preserved(self):
        (self.root / "nested").mkdir()
        (self.root / "nested" / "keep.bin").write_bytes(b"\x00\xff")
        script = (
            "import json,pathlib,sys; print(json.dumps({'cwd':str(pathlib.Path.cwd()),"
            "'args':sys.argv[1:],'stdin':sys.stdin.buffer.read().hex(),"
            "'isolated':sys.flags.isolated}))"
        )
        args = ["with spaces", "a;b", "$(literal)", "--not-a-host-option"]
        result = self.run_input("abc\0", script, cwd="nested", args=args)
        self.assert_success(result)
        observed = json.loads(result["stdout"]["excerpt"])
        self.assertTrue(os.path.samefile(observed["cwd"], self.root / "nested"))
        self.assertEqual(observed["args"], args)
        self.assertEqual(observed["stdin"], "61626300")
        self.assertEqual(observed["isolated"], 1)
        self.assertEqual(result["cwd"], "nested")
        self.assertEqual(result["argv"], self.prefix + args)

    def test_missing_or_outside_cwd_does_not_launch(self):
        executor = self.executor()
        for cwd in ("missing", "../outside"):
            with self.subTest(cwd=cwd), patch.object(workspace_tools.subprocess, "Popen") as launch:
                result = executor.execute("workspace_run", {"command": "probe", "stdin": "valid", "cwd": cwd})
                self.assertEqual(result["outcome"], "not_started", result)
                self.assertIs(result["complete"], True)
                self.assertEqual(result["invalid_fields"], ["cwd"])
                launch.assert_not_called()

    def test_large_input_and_large_output_before_read_do_not_deadlock(self):
        out = b"O" * (256 * 1024)
        err = b"E" * (192 * 1024)
        script = (
            "import hashlib,sys; "
            "sys.stdout.buffer.write(b'O'*(256*1024)); sys.stdout.buffer.flush(); "
            "sys.stderr.buffer.write(b'E'*(192*1024)); sys.stderr.buffer.flush(); "
            "d=sys.stdin.buffer.read(); "
            "sys.stdout.buffer.write(('\\nINPUT '+str(len(d))+' '+hashlib.sha256(d).hexdigest()+'\\n').encode()); "
            "sys.stdout.buffer.flush()"
        )
        result = self.run_input(LIMIT_INPUT, script)
        self.assert_success(result)
        suffix = f"\nINPUT 65536 {stdin_metadata(LIMIT_INPUT)['sha256']}\n".encode()
        self.assert_capture(result["stdout"], out + suffix)
        self.assert_capture(result["stderr"], err)
        self.assertEqual(result["bytes"], len(out + suffix) + len(err))
        self.assertTrue(result["truncated"])
        self.assertEqual(result["stdout"]["preview_mode"], "head_tail")
        self.assertTrue(result["stdout"]["tail_excerpt"].endswith(suffix.decode()))

    def test_ignored_input_has_metadata_but_no_output_or_execution_evidence(self):
        script = "import sys; sys.stdout.buffer.write(b'fixed-only\\n')"
        executor = self.executor(script)
        text = "raise RuntimeError('PRIVATE-UNEXECUTED-STDIN')"
        plain = executor.execute("workspace_run", {"command": "probe"})
        result = executor.execute("workspace_run", {"command": "probe", "stdin": text})
        self.assert_success(result)
        self.assertEqual(result["stdin"], stdin_metadata(text))
        self.assertEqual(result["stdout"], plain["stdout"])
        self.assertEqual(result["stderr"], plain["stderr"])
        self.assertEqual(result["bytes"], len(b"fixed-only\n"))
        self.assertEqual(result["output"], "fixed-only\n")
        self.assertNotIn("PRIVATE-UNEXECUTED-STDIN", json.dumps(result))
        self.assertEqual(set(result) - set(plain), {"stdin"})
        self.assertEqual(list(self.root.iterdir()), [])

    def test_explicit_python_stdin_mode_executes_without_injected_argv(self):
        prefix = [sys.executable, "-I", "-B", "-X", "utf8"]
        executor = self.executor(prefix=prefix)
        text = "import json,sys\nprint(json.dumps([sys.flags.isolated,sys.argv]))\n"
        args = ["-", "literal arg"]
        result = executor.execute("workspace_run", {"command": "probe", "args": args, "stdin": text})
        self.assert_success(result)
        self.assertEqual(result["argv"], prefix + args)
        self.assertEqual(json.loads(result["stdout"]["excerpt"]), [1, ["-", "literal arg"]])
        self.assertEqual(result["stdin"], stdin_metadata(text))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_nonzero_preserves_output_and_input_metadata(self):
        script = (
            "import sys; d=sys.stdin.buffer.read(); "
            "sys.stdout.buffer.write(b'out:'+d); sys.stderr.buffer.write(b'failed\\n'); sys.exit(7)"
        )
        text = "input\0end"
        result = self.run_input(text, script)
        self.assertEqual(result["status"], "error", result)
        self.assertEqual((result["outcome"], result["exit_code"]), ("failed", 7))
        self.assertFalse(result["timed_out"])
        self.assert_capture(result["stdout"], b"out:" + text.encode())
        self.assert_capture(result["stderr"], b"failed\n")
        self.assertEqual(result["stdin"], stdin_metadata(text))

    def test_timeout_preserves_partial_output_and_is_not_success(self):
        script = (
            "import sys,time; sys.stdin.buffer.read(); "
            "sys.stdout.buffer.write(b'read-before-timeout\\n'); sys.stdout.buffer.flush(); time.sleep(5)"
        )
        started = time.monotonic()
        result = self.run_input("bounded", script, timeout=1.5)
        self.assertEqual(result["status"], "error", result)
        self.assertEqual(result["outcome"], "timeout")
        self.assertIs(result["timed_out"], True)
        self.assertIsNone(result["exit_code"])
        self.assertIn("read-before-timeout", result["stdout"]["excerpt"])
        self.assertEqual(result["stdin"], stdin_metadata("bounded"))
        self.assertLess(time.monotonic() - started, 5)

    def test_launch_error_is_not_success_and_keeps_configured_input_metadata(self):
        executor = self.executor(prefix=[str(self.base / "missing-program.exe")])
        text = "configured-but-not-read"
        result = executor.execute("workspace_run", {"command": "probe", "stdin": text})
        self.assertEqual(result["status"], "error", result)
        self.assertEqual(result["outcome"], "launch_error")
        self.assertIsNone(result["exit_code"])
        self.assertEqual(result["stdin"], stdin_metadata(text))
        self.assertEqual(result["bytes"], 0)
        self.assertNotIn(text, json.dumps(result))
        self.assertEqual(list(self.root.iterdir()), [])


class WorkspaceStdinWorkerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="cater-stdin-worker-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.target = self.base / "target"
        self.target.mkdir()
        self.result_path = self.base / "result.json"

    def command(self, script=READ_BYTES):
        return [sys.executable, "-I", "-B", "-X", "utf8", "-c", script]

    def worker(self, payload):
        worker = Path(workspace_tools.__file__).with_name("_verification_worker.py")
        return subprocess.run(
            [sys.executable, "-I", "-B", "-X", "utf8", str(worker), str(self.result_path)],
            input=json.dumps(payload, ensure_ascii=True).encode("utf-8"),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=self.target, timeout=8, check=False,
        )

    def test_legacy_argv_array_still_delivers_devnull(self):
        completed = self.worker(self.command())
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(self.result_path.read_text(encoding="utf-8")), {"exit_code": 0})
        observed = json.loads(completed.stdout)
        self.assertEqual((observed["hex"], observed["bytes"], observed["eof"]), ("", 0, True))
        self.assertEqual(list(self.target.iterdir()), [])

    def test_object_protocol_passes_raw_utf8_and_eof(self):
        text = "worker:\u4e2d\U0001f642\0\r\nend"
        completed = self.worker({"argv": self.command(), "stdin_utf8": text})
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(self.result_path.read_text(encoding="utf-8")), {"exit_code": 0})
        observed = json.loads(completed.stdout)
        self.assertEqual(observed["hex"], text.encode().hex())
        self.assertTrue(observed["eof"])
        self.assertEqual(list(self.target.iterdir()), [])

    def test_object_protocol_empty_input_is_valid(self):
        completed = self.worker({"argv": self.command(), "stdin_utf8": ""})
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["hex"], "")
        self.assertEqual(json.loads(self.result_path.read_text(encoding="utf-8")), {"exit_code": 0})

    def test_object_protocol_is_strict_and_never_launches_malformed_targets(self):
        script = "from pathlib import Path; Path('unexpected-launch').write_bytes(b'started')"
        argv = self.command(script)
        malformed = [
            {"argv": argv}, {"argv": argv, "stdin_utf8": None},
            {"argv": argv, "stdin_utf8": 3},
            {"argv": argv, "stdin_utf8": "", "extra": True},
            {"argv": argv, "stdin_utf8": "\ud800"},
            {"argv": " ".join(argv), "stdin_utf8": ""},
        ]
        for index, payload in enumerate(malformed):
            with self.subTest(case=index):
                completed = self.worker(payload)
                self.assertFalse((self.target / "unexpected-launch").exists())
                if self.result_path.exists():
                    self.assertIsNone(json.loads(self.result_path.read_text(encoding="utf-8"))["exit_code"])
                    self.result_path.unlink()
                else:
                    self.assertNotEqual(completed.returncode, 0)


class WorkspaceStdinArtifactTests(unittest.TestCase):
    def setUp(self):
        # Import the module, never bind its TestCase class into this module's discovery.
        import test_team_artifact_check as fixture
        self.fixture = fixture.TeamArtifactCheckTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_published_copy_and_stdin_compose_without_changing_live_workspace(self):
        fixture = self.fixture
        (fixture.root / "package/data.bin").write_bytes(b"unpublished-live-change")
        before = {str(p.relative_to(fixture.root)): p.read_bytes()
                  for p in fixture.root.rglob("*") if p.is_file()}
        text = "copy-input\0\u4e2d"
        script = (
            "from pathlib import Path; import hashlib,sys; "
            "assert Path('data.bin').read_bytes()==b'\\x00\\xff'; "
            "d=sys.stdin.buffer.read(); print('published-copy',hashlib.sha256(d).hexdigest())"
        )
        revision = fixture.f.rt.team.view(fixture.f.task)["revision"]
        result = fixture.check(stdin=text, args=["-c", script])
        self.assertEqual(result["outcome"], "passed", result)
        self.assertEqual(result["stdin"], stdin_metadata(text))
        self.assertEqual(result["stdout"]["excerpt"].strip(),
                         "published-copy " + stdin_metadata(text)["sha256"])
        self.assertTrue(result["artifact_check_valid"])
        self.assertTrue(result["artifacts_unchanged"])
        self.assertTrue(result["scope_current"])
        self.assertEqual(result["checked_artifacts"][0]["seq"], fixture.seq)
        self.assertEqual(result["environment"], "temporary_artifact_copy_not_sandbox")
        self.assertEqual({str(p.relative_to(fixture.root)): p.read_bytes()
                          for p in fixture.root.rglob("*") if p.is_file()}, before)
        self.assertEqual(fixture.f.rt.team.view(fixture.f.task)["revision"], revision)
        self.assertEqual(fixture.f.rt.team_reviews.view(fixture.f.task), [])
        self.assertEqual(fixture.f.rt.team._pending_operations(fixture.f.task), [])
        started = [e for e in fixture.f.rt.log.read(task_id=fixture.f.task)
                   if e.type == "team.tool_started" and e.payload["name"] == "workspace_run"]
        self.assertEqual(started[-1].payload["arguments"]["stdin"], text)
        self.assertNotIn(text, json.dumps(result, ensure_ascii=False))
        self.assertTrue(fixture.f.rt.verify_chain().ok)

    def test_stdin_cannot_bypass_stale_artifact_selection(self):
        fixture = self.fixture
        newer = fixture.archive()
        self.assertNotEqual(newer, fixture.seq)
        with patch.object(WorkspaceExecutor, "_run", side_effect=AssertionError("stale selection launched")) as run:
            result = fixture.check(stdin="must-not-execute")
        self.assertEqual(result["status"], "error", result)
        self.assertEqual(result["outcome"], "not_started")
        self.assertEqual(result["code"], "invalid_artifact_check")
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
