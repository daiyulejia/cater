"""Real native process classification in the minimal workspace environment."""

from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import test_delivery_commands as delivery
import test_workspace_tools as workspace
from cater.processes import ProcessScope


POWERSHELL = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
CMD = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/cmd.exe"
WINDOWS_SHELLS = os.name == "nt" and POWERSHELL.is_file() and CMD.is_file()
DEFAULT_PATHEXT = ".COM;.EXE;.BAT;.CMD"


@contextmanager
def fixture(cls):
    value = cls()
    value.setUp()
    try:
        yield value
    finally:
        value.doCleanups()


class MinimalCommandEnvironmentTests(unittest.TestCase):
    def test_platform_default_does_not_inherit_unlisted_extensions_or_secrets(self):
        with fixture(workspace.WorkspaceToolsTests) as f:
            with patch.dict(os.environ, {"PATHEXT": ".EXE;.CUSTOM_PRIVATE", "PRIVATE_RUNTIME_VALUE": "not-for-child"}):
                result = f.run_executor().execute("workspace_run", {"command": "python", "args": [
                    "import json,os;print(json.dumps([os.environ.get('PATHEXT'),os.environ.get('PRIVATE_RUNTIME_VALUE')]))"]})
            self.assertEqual(result["outcome"], "passed", result)
            self.assertEqual(json.loads(result["stdout"]["excerpt"]), [DEFAULT_PATHEXT if os.name == "nt" else None, None])
            self.assertNotIn("CUSTOM_PRIVATE", json.dumps(result))
            self.assertNotIn("not-for-child", json.dumps(result))

    @unittest.skipUnless(os.name == "nt", "Windows case-insensitive environment")
    def test_explicit_case_variants_and_empty_value_override_one_canonical_key(self):
        with fixture(workspace.WorkspaceToolsTests) as f:
            for key in ("PATHEXT", "Pathext", "pathext"):
                for value in (".EXE;.CATER_TEST", ""):
                    with self.subTest(key=key, value=value), patch.dict(os.environ, {"PATHEXT": value}):
                        observed = []
                        original = __import__('subprocess').Popen
                        def capture(*args, **kwargs):
                            observed.append(dict(kwargs["env"]))
                            self.assertIs(kwargs["shell"], False)
                            return original(*args, **kwargs)
                        with patch('cater.workspace_tools.subprocess.Popen', side_effect=capture):
                            result = f.run_executor(env_allowlist=[key]).execute("workspace_run", {
                                "command": "python", "args": ["import os,json;print(json.dumps(os.environ.get('PATHEXT')))"]})
                        self.assertEqual(result["outcome"], "passed", result)
                        self.assertEqual(json.loads(result["stdout"]["excerpt"]), value)
                        self.assertEqual([k for k in observed[0] if k.casefold() == "pathext"], ["PATHEXT"])

    @unittest.skipUnless(os.name == "nt", "Windows base-key case-insensitive environment")
    def test_allowed_base_key_variants_never_duplicate_environment_entries(self):
        with fixture(workspace.WorkspaceToolsTests) as f:
            observed = []
            original = __import__('subprocess').Popen
            def capture(*args, **kwargs):
                observed.append(dict(kwargs["env"]))
                return original(*args, **kwargs)
            with patch.dict(os.environ, {"PATH": str(f.base)}), patch('cater.workspace_tools.subprocess.Popen', side_effect=capture):
                result = f.run_executor(env_allowlist=["SystemRoot", "SYSTEMROOT", "Windir", "Path", "Temp", "Tmp"]).execute(
                    "workspace_run", {"command": "python", "args": ["print('actual-child')"]})
            self.assertEqual(result["outcome"], "passed", result)
            keys = list(observed[0])
            self.assertEqual(len(keys), len({key.casefold() for key in keys}))
            self.assertEqual(result["execution_context"]["path_policy"], "allowlist_configured")

    @unittest.skipUnless(os.name != "nt", "POSIX case-sensitive environment")
    def test_posix_allowlist_keeps_distinct_case_sensitive_keys(self):
        with fixture(workspace.WorkspaceToolsTests) as f:
            with patch.dict(os.environ, {"PATHEXT": "upper", "Pathext": "mixed"}):
                result = f.run_executor(env_allowlist=["PATHEXT", "Pathext"]).execute("workspace_run", {
                    "command": "python", "args": ["import os,json;print(json.dumps([os.environ.get('PATHEXT'),os.environ.get('Pathext')]))"]})
            self.assertEqual(result["outcome"], "passed", result)
            self.assertEqual(json.loads(result["stdout"]["excerpt"]), ["upper", "mixed"])


@unittest.skipUnless(WINDOWS_SHELLS, "Windows PowerShell and cmd.exe needed for real native pipeline")
class WindowsNativePipelineTests(unittest.TestCase):
    def test_default_environment_really_executes_absolute_native_exe_in_pipeline(self):
        with fixture(workspace.WorkspaceToolsTests) as f:
            executor = f.make_executor("execute", commands={"powershell": [str(POWERSHELL), "-NoProfile", "-NonInteractive", "-Command"]})
            result = executor.execute("workspace_run", {"command": "powershell", "args": [
                f"& '{CMD}' /d /c 'echo 41' | ForEach-Object {{ [int]$_ + 1 }}"]})
            self.assertEqual((result["outcome"], result["exit_code"]), ("passed", 0), result)
            self.assertEqual(result["stdout"]["excerpt"].strip(), "42")
            self.assertEqual(result["stderr"]["bytes"], 0)
            self.assertTrue(result["complete"])
            self.assertEqual(result["process_scope"], "windows_job")
            self.assertEqual(list(f.root.iterdir()), [])

    def test_native_child_failure_does_not_become_success_from_shell_pipeline(self):
        with fixture(workspace.WorkspaceToolsTests) as f:
            executor = f.make_executor("execute", commands={"powershell": [str(POWERSHELL), "-NoProfile", "-NonInteractive", "-Command"]})
            result = executor.execute("workspace_run", {"command": "powershell", "args": [
                f"& '{CMD}' /d /c 'exit 7'; exit $LASTEXITCODE"]})
            self.assertEqual((result["outcome"], result["exit_code"]), ("failed", 7), result)
            self.assertTrue(result["complete"])

    def test_timeout_closes_original_handles_for_powershell_and_its_descendant(self):
        peer = Path(__file__).parent / "fixtures/worker_interpreter_peer.py"
        spec = importlib.util.spec_from_file_location("windows_environment_handles", peer)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        handles = module.ProcessHandles()
        close = ProcessScope.close
        def observe(scope):
            handles.capture_job(scope.job)
            return close(scope)
        try:
            with fixture(workspace.WorkspaceToolsTests) as f:
                executor = f.make_executor("execute", timeout=4, commands={"powershell": [str(POWERSHELL), "-NoProfile", "-NonInteractive", "-Command"]})
                command = (f"& '{CMD}' /d /c 'echo ready' | ForEach-Object {{ $_ }}; "
                    f"Start-Process -FilePath '{POWERSHELL}' -ArgumentList '-NoProfile','-NonInteractive','-Command','Start-Sleep -Seconds 30' -NoNewWindow; "
                    "Start-Sleep -Seconds 30")
                with patch.object(ProcessScope, 'close', observe):
                    result = executor.execute("workspace_run", {"command": "powershell", "args": [command]})
                self.assertEqual(result["outcome"], "timeout", result)
                self.assertIn("ready", result["stdout"]["excerpt"])
                self.assertTrue(result["complete"])
                self.assertGreaterEqual(sum(Path(row["image"]) == POWERSHELL.resolve() for _, row in handles.held.values()), 2)
                handles.wait_all()
        finally:
            self.assertEqual(handles.stop_and_close(), [])

    def test_native_pipeline_works_in_versioned_artifact_copy_without_source_changes(self):
        with fixture(delivery.DeliveryCommandTests) as f:
            artifact = f.archive()
            code = ("import json,subprocess;from pathlib import Path;"
                f"r=subprocess.run([{str(POWERSHELL)!r},'-NoProfile','-NonInteractive','-Command',"
                f"{f'& {str(CMD)!r} /d /c echo 1 | Measure-Object | Select-Object -ExpandProperty Count'!r}],capture_output=True);"
                "print(json.dumps([r.returncode,r.stdout.decode('ascii').strip(),r.stderr.decode('ascii')]));"
                "raise SystemExit(r.returncode)")
            _, _, result = f.run_command(artifact_seqs=[artifact], cwd="package", args=["-c", code])
            self.assertEqual(result["outcome"], "passed", result)
            self.assertEqual(json.loads(result["stdout"]["excerpt"]), [0, "1", ""])
            self.assertTrue(result["artifacts_unchanged"])
            self.assertTrue(result["artifact_check_valid"])
            self.assertEqual(result["environment"], "temporary_artifact_copy_not_sandbox")
            self.assertTrue(f.rt.verify_chain().ok)


if __name__ == "__main__":
    unittest.main()
