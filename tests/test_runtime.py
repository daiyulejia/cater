from __future__ import annotations

import io
import sys
import tempfile
import unittest
from pathlib import Path

from cater.cli import main
from cater.drivers import CliDriver
from cater.runtime import Runtime


class RuntimeTests(unittest.TestCase):
    def test_battle_escalates_to_human_inbox_and_decision_closes_it(self):
        with Runtime(":memory:") as rt:
            task_id = rt.create_task("Build Cater seed", ["agents respond"])
            result = rt.battle(task_id)

            self.assertEqual(rt.task(task_id).status, "escalated")
            self.assertIn("positions", result["brief"])
            self.assertEqual(len(rt.inbox()), 1)

            rt.decide(task_id, "done", "Ship the smallest loop first")
            self.assertEqual(rt.task(task_id).status, "done")
            self.assertEqual(rt.inbox(), [])

    def test_event_hash_chain_verifies(self):
        with Runtime(":memory:") as rt:
            task_id = rt.create_task("Check chain")
            rt.battle(task_id)
            report = rt.verify_chain()
            self.assertTrue(report.ok, report.describe())
            self.assertEqual(report.checked, rt.log.count())

    def test_task_state_is_folded_from_events(self):
        with Runtime(":memory:") as rt:
            task_id = rt.create_task("Fold me")
            rt.battle(task_id)
            first = rt.task(task_id)
            again = rt.task(task_id)
            self.assertEqual(first.status, again.status)
            self.assertEqual(len(first.messages), 3)

    def test_runtime_accepts_external_role_and_driver_configs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            roles = root / "roles.json"
            drivers = root / "drivers.json"
            roles.write_text(
                '[{"id":"fixture","name":"Fixture","role":"tester","driver":"fixture"}]',
                encoding="utf-8",
            )
            drivers.write_text(
                '{"drivers":[{"kind":"scripted","name":"fixture","script":["{\\"stance\\":\\"assert\\",\\"option_id\\":\\"X\\",\\"claim\\":\\"configured\\",\\"evidence\\":[\\"config\\"],\\"text\\":\\"configured\\"}"]}]}',
                encoding="utf-8",
            )
            with Runtime(":memory:", roles_path=roles, drivers_path=drivers) as rt:
                task_id = rt.create_task("Configured")
                rt.battle(task_id)
                self.assertEqual(len(rt.task(task_id).messages), 1)
                self.assertEqual(rt.task(task_id).messages[0]["claim"], "configured")


class DriverTests(unittest.TestCase):
    def fixture_command(self) -> list[str]:
        return [sys.executable, str(Path(__file__).resolve().parent / "fixtures" / "ndjson_driver.py")]

    def test_cli_driver_probe_and_complete(self):
        driver = CliDriver("fixture", self.fixture_command(), timeout=5)
        self.addCleanup(driver.close)
        self.assertTrue(driver.probe())
        response = driver.complete(role="tester", prompt="ping", context={})
        self.assertEqual(response.tokens, 11)
        self.assertIn("Fixture driver responded", response.text)

    def test_cli_driver_recovers_after_child_crash(self):
        driver = CliDriver("fixture", self.fixture_command(), timeout=5, restart_limit=1)
        self.addCleanup(driver.close)
        with self.assertRaises(RuntimeError):
            driver.complete(role="tester", prompt="crash", context={"crash": True})
        response = driver.complete(role="tester", prompt="after", context={})
        self.assertIn("Fixture driver responded", response.text)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "cater.db")
        self.out = io.StringIO()
        self._stdout = sys.stdout
        sys.stdout = self.out

    def tearDown(self):
        sys.stdout = self._stdout
        self.tmp.cleanup()

    def run_cli(self, *args: str) -> tuple[int, str]:
        self.out.seek(0)
        self.out.truncate(0)
        code = main(["--db", self.db, *args])
        return code, self.out.getvalue()

    def test_cli_demo_and_chain(self):
        code, out = self.run_cli("demo")
        self.assertEqual(code, 0)
        self.assertIn("task:", out)
        code, out = self.run_cli("chain")
        self.assertEqual(code, 0)
        self.assertIn("hash chain ok", out)

    def test_cli_new_battle_inbox_decide(self):
        code, out = self.run_cli("new", "Build demo", "--accept", "three roles", "--battle")
        self.assertEqual(code, 0)
        task_id = next(line for line in out.splitlines() if line.startswith("created ")).split()[1]

        code, out = self.run_cli("inbox")
        self.assertEqual(code, 0)
        self.assertIn(task_id, out)

        code, out = self.run_cli("decide", task_id, "--resolution", "done", "--reason", "ok")
        self.assertEqual(code, 0)
        self.assertIn("decision recorded", out)

        code, out = self.run_cli("inbox")
        self.assertEqual(code, 0)
        self.assertIn("inbox empty", out)

    def test_cli_probe_driver_with_external_config(self):
        driver_script = Path(__file__).resolve().parent / "fixtures" / "ndjson_driver.py"
        config = Path(self.tmp.name) / "drivers.json"
        config.write_text(
            '{"drivers":[{"kind":"cli","name":"product","command":["'
            + sys.executable.replace("\\", "\\\\")
            + '","'
            + str(driver_script).replace("\\", "\\\\")
            + '"],"timeout":5}]}',
            encoding="utf-8",
        )
        code = main(["--db", self.db, "--drivers", str(config), "probe-driver", "product"])
        self.assertEqual(code, 0)
        self.assertIn("alive", self.out.getvalue())


if __name__ == "__main__":
    unittest.main()
