import json
import tempfile
import unittest
from pathlib import Path

from cater.anthropic import AnthropicMessagesDriver
from cater.runtime import Runtime
from test_work import item


class ExecutionFingerprintTests(unittest.TestCase):
    def test_changed_model_invalidates_old_plan_but_operational_timeout_does_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "tasks.db"
            def driver(model="one", timeout=60):
                return AnthropicMessagesDriver("dev", endpoint="https://fixture.invalid/messages", model=model,
                                               timeout=timeout, api_key_env="CATER_TEST")
            with Runtime(db, drivers={"dev": driver()}) as rt:
                task = rt.create_task("Version the configuration")
                rt.plan_work(task, [item("draft")])
                scope = rt.work.scope(task)
            with Runtime(db, drivers={"dev": driver(timeout=180)}) as rt:
                self.assertEqual(rt.work.scope(task), scope)
                self.assertTrue(rt.work.view(task)["scope_current"])
            with Runtime(db, drivers={"dev": driver(model="two")}) as rt:
                self.assertFalse(rt.work.view(task)["scope_current"])
                self.assertNotEqual(rt.work.scope(task)["organization"], scope["organization"])
                self.assertNotIn("fixture.invalid", json.dumps(rt.log.read()[-1].payload))

    def test_changed_role_charter_does_not_reuse_previous_acceptance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            roles = root / "roles.json"
            config = json.loads((Path(__file__).resolve().parents[1] / "cater/packs/default_roles.json").read_text(encoding="utf-8"))
            roles.write_text(json.dumps(config), encoding="utf-8")
            with Runtime(root / "tasks.db", roles_path=roles) as rt:
                task = rt.create_task("Keep independent responsibilities")
                rt.plan_work(task, [item("draft")])
                old = rt.work.scope(task)
            config["roles"][0]["charter"] = "New review responsibility"
            roles.write_text(json.dumps(config), encoding="utf-8")
            with Runtime(root / "tasks.db", roles_path=roles) as rt:
                self.assertNotEqual(rt.work.scope(task), old)
                self.assertFalse(rt.work.view(task)["scope_current"])


if __name__ == "__main__":
    unittest.main()
