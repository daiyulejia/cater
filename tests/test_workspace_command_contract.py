import json
from pathlib import Path
import sys
import tempfile
import unittest

from cater.workspace_tools import WorkspaceExecutor, WorkspacePolicy


class WorkspaceCommandContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def executor(self, script):
        self.prefix = [sys.executable, "-I", "-c", script]
        return WorkspaceExecutor(WorkspacePolicy.from_dict({
            "root": str(self.root), "mode": "execute", "commands": {"check": self.prefix}, "timeout": 15,
        }))

    def test_schema_explains_prefix_literal_args_and_execution_evidence(self):
        marker = "private-host-prefix-not-for-schema"
        executor = self.executor(f"print({marker!r})")
        schema = next(row for row in executor.schemas() if row["name"] == "workspace_run")
        self.assertNotIn(marker, json.dumps(schema))
        self.assertIn("not the alias name", schema["description"])
        self.assertIn("test names/counts", schema["description"])
        self.assertIn("not an OS sandbox", schema["description"])
        properties = schema["input_schema"]["properties"]
        self.assertEqual(properties["command"]["enum"], ["check"])
        self.assertIn("not an executable path", properties["command"]["description"])
        self.assertIn("accept, reject, or ignore", properties["args"]["description"])

    def test_target_accepting_program_receives_literal_args_after_prefix(self):
        executor = self.executor("import json,sys; print(json.dumps({'selected':sys.argv[1:]}))")
        args = ["test_extra.py", "name with spaces", "a;b", "$(literal)"]
        result = executor.execute("workspace_run", {"command": "check", "args": args})
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["argv"], self.prefix + args)
        self.assertEqual(json.loads(result["stdout"]["excerpt"]), {"selected": args})
        self.assertEqual(list(self.root.iterdir()), [])

    def test_no_argument_program_rejects_target_without_running_it(self):
        executor = self.executor("import argparse; argparse.ArgumentParser().parse_args(); print('default checks')")
        result = executor.execute("workspace_run", {"command": "check", "args": ["test_extra.py"]})
        self.assertEqual(result["status"], "error", result)
        self.assertEqual(result["exit_code"], 2)
        self.assertEqual(result["argv"], self.prefix + ["test_extra.py"])
        self.assertIn("unrecognized arguments", result["stderr"]["excerpt"])
        self.assertNotIn("default checks", result["stdout"]["excerpt"])

    def test_ignored_target_success_is_only_process_success(self):
        executor = self.executor("import json; print(json.dumps({'tests_run':['fixed_only']}))")
        result = executor.execute("workspace_run", {"command": "check", "args": ["test_extra.py"]})
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["outcome"], "passed")
        self.assertEqual(result["argv"], self.prefix + ["test_extra.py"])
        self.assertEqual(json.loads(result["stdout"]["excerpt"]), {"tests_run": ["fixed_only"]})
        self.assertNotIn("test_extra.py", result["output"])


if __name__ == "__main__":
    unittest.main()
