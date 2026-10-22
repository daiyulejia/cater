"""Host launch facts, not shell portability or model-quality certification."""

from contextlib import contextmanager
import json
import os
import sys
import unittest
from unittest.mock import patch

import test_delivery_commands as delivery
import test_workspace_tools as workspace
from cater import delivery_commands


CONTEXT = {"version": 1, "invocation": "direct_argv", "environment": "minimal_with_allowlist",
           "path_policy": "os_default", "platform": sys.platform if sys.platform in {"win32", "linux", "darwin"} else "other"}


@contextmanager
def fixture(cls):
    value = cls()
    value.setUp()
    try:
        yield value
    finally:
        value.doCleanups()


class CommandExecutionContextTests(unittest.TestCase):
    def test_real_child_has_minimal_environment_and_literal_arguments(self):
        with fixture(workspace.WorkspaceToolsTests) as f:
            code = "import os,json,sys;print(json.dumps([os.environ.get('PATH'),os.environ.get('CATER_CONTEXT_SECRET'),sys.argv[1:]]))"
            with patch.dict(os.environ, {"PATH": str(f.base / "terminal-only"), "CATER_CONTEXT_SECRET": "not-for-child"}):
                result = f.run_executor().execute("workspace_run", {"command": "python", "args": [code, "a>b", "$HOME", "&&"]})
            self.assertEqual(result["outcome"], "passed", result)
            self.assertEqual(json.loads(result["stdout"]["excerpt"]), [os.defpath, None, ["a>b", "$HOME", "&&"]])
            self.assertEqual(result["execution_context"], CONTEXT)
            self.assertNotIn("not-for-child", json.dumps(result))

    def test_explicit_path_and_other_allowlisted_variables_are_not_terminal_identity(self):
        with fixture(workspace.WorkspaceToolsTests) as f:
            for allowlist, expected in ((["PATH"], "allowlist_configured"), (["CATER_VISIBLE"], "os_default")):
                with self.subTest(allowlist=allowlist), patch.dict(os.environ, {"PATH": str(f.base / "configured"), "CATER_VISIBLE": "visible"}):
                    result = f.run_executor(env_allowlist=allowlist).execute("workspace_run", {
                        "command": "python", "args": ["import os,json;print(json.dumps([os.environ['PATH'],os.environ.get('CATER_VISIBLE')]))"]})
                self.assertEqual(result["outcome"], "passed", result)
                self.assertEqual(result["execution_context"], {**CONTEXT, "path_policy": expected})
                self.assertEqual(json.loads(result["stdout"]["excerpt"]),
                    [str(f.base / "configured") if "PATH" in allowlist else os.defpath,
                     "visible" if "CATER_VISIBLE" in allowlist else None])
            with patch.dict(os.environ):
                os.environ.pop("PATH", None)
                missing = f.run_executor(env_allowlist=["PATH"]).execute("workspace_run", {"command": "python", "args": ["pass"]})
            self.assertEqual(missing["execution_context"], CONTEXT)

    def test_metadata_describes_configuration_not_exit_success_or_child_output(self):
        with fixture(workspace.WorkspaceToolsTests) as f:
            result = f.run_executor().execute("workspace_run", {"command": "python", "args": [
                "import sys;print('{\"execution_context\":\"inherited_terminal\"}');sys.exit(7)"]})
            self.assertEqual(result["exit_code"], 7)
            self.assertEqual(result["outcome"], "failed")
            self.assertEqual(result["execution_context"], CONTEXT)
            rejected = f.run_executor().execute("workspace_run", {"command": "python", "cwd": "missing", "args": ["pass"]})
            self.assertEqual(rejected["outcome"], "not_started")
            self.assertNotIn("execution_context", rejected)

    def test_actual_native_copy_receipt_freezes_configuration_and_source_without_changing_bytes(self):
        with fixture(delivery.DeliveryCommandTests) as f:
            artifact = f.archive()
            start, finish, result = f.run_command(artifact_seqs=[artifact], cwd="package", args=["probe.py"])
            record, = f.records(finish)
            f.assert_record(record, start, finish, result)
            self.assertEqual(record["execution_context"], CONTEXT)
            self.assertTrue(result["artifacts_unchanged"])
            self.assertEqual(result["environment"], "temporary_artifact_copy_not_sandbox")
            text = "\n".join(delivery_commands.markdown([record]))
            self.assertIn("这里只描述外层启动配置", text)
            self.assertIn("不证明启动成功", text)
            self.assertIn("不是用户终端PATH", text)
            self.assertTrue(f.rt.verify_chain().ok)

    def test_legacy_and_malformed_optional_metadata_never_backfill_environment(self):
        invalid = [None, {}, [], "minimal_with_allowlist", {**CONTEXT, "version": True},
                   {**CONTEXT, "version": 2}, {**CONTEXT, "platform": "windows"},
                   {**CONTEXT, "invocation": "shell"}, {**CONTEXT, "environment": "inherited"},
                   {**CONTEXT, "path_policy": "terminal"}, {**CONTEXT, "env": {"PRIVATE": "hidden"}}]
        invalid += [{key: value for key, value in CONTEXT.items() if key != missing} for missing in CONTEXT]
        with fixture(delivery.DeliveryCommandTests) as f:
            for extra in [{}, *[{"execution_context": value} for value in invalid]]:
                with self.subTest(extra=extra):
                    _, _, _, finish = f.chain(result={"status": "ok", "outcome": "passed", "exit_code": 0, **extra})
                    record, = f.records(finish)
                    self.assertEqual(set(record), delivery.RECORD_KEYS)
                    self.assertEqual(record["source_seq"], finish.seq)
                    self.assertEqual(record["exit_code"], 0)
                    text = "\n".join(delivery_commands.markdown([record]))
                    self.assertIn("启动环境细节未记录", text)
                    self.assertNotIn("精简环境", text)
                    self.assertNotIn("hidden", text)

    def test_allowlisted_context_is_frozen_only_from_native_host_result(self):
        with fixture(delivery.DeliveryCommandTests) as f:
            _, _, _, finish = f.chain(result={"status": "error", "outcome": "launch_error", "exit_code": None,
                "execution_context": {**CONTEXT, "path_policy": "allowlist_configured"}})
            record, = f.records(finish)
            self.assertEqual(record["execution_context"]["path_policy"], "allowlist_configured")
            self.assertIsNone(record["exit_code"])
            self.assertIn("不保证与用户终端一致", "\n".join(delivery_commands.markdown([record])))
            _, _, _, external = f.chain(result={"status": "ok", "execution_context": CONTEXT},
                start_changes={"name": "mcp__pretend_run"}, finish_changes={"name": "mcp__pretend_run"})
            with self.assertRaises(delivery.HandoffError):
                f.records(external)


if __name__ == "__main__":
    unittest.main()
