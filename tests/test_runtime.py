from __future__ import annotations

import io
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from http.server import ThreadingHTTPServer
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from cater.cabinet import majority_threshold, tally_votes
from cater.cli import main
from cater.drivers import CliDriver, DriverResponse
from cater.ladder import decision_ladder
from cater.profiles import agent_profiles
from cater.runtime import Runtime
from cater.web import make_handler, render_task


class AlwaysFailDriver:
    name = "product"

    def __init__(self):
        self.calls = 0

    def complete(self, *, role: str, prompt: str, context: dict) -> DriverResponse:
        self.calls += 1
        raise RuntimeError("model process exploded")


class RuntimeTests(unittest.TestCase):
    def test_battle_records_agent_decision_and_command_updates_goal(self):
        with Runtime(":memory:") as rt:
            task_id = rt.create_task("Build Cater seed", ["agents respond"])
            result = rt.battle(task_id)
            task = rt.task(task_id)

            self.assertEqual(task.status, "rework")
            self.assertEqual(task.decision["resolution"], "rework")
            self.assertIn("positions", result["brief"])
            self.assertEqual(rt.inbox(), [])

            rt.command(
                task_id,
                "Narrow the scope and try again",
                goal="Build the smallest autonomous loop",
                acceptance=["agents decide without human approval"],
            )
            task = rt.task(task_id)
            self.assertEqual(task.status, "running")
            self.assertEqual(task.goal, "Build the smallest autonomous loop")
            self.assertEqual(task.acceptance, ["agents decide without human approval"])
            self.assertEqual(task.commands[-1]["instruction"], "Narrow the scope and try again")

    def test_event_hash_chain_verifies(self):
        with Runtime(":memory:") as rt:
            task_id = rt.create_task("Check chain")
            rt.battle(task_id)
            report = rt.verify_chain()
            self.assertTrue(report.ok, report.describe())
            self.assertEqual(report.checked, rt.log.count())

    def test_hmac_audit_signature_verifies_and_rejects_wrong_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "audit.db")
            with Runtime(db, audit_key="secret", audit_key_id="test-key") as rt:
                task_id = rt.create_task("Sign me")
                rt.battle(task_id)
                signed_event = rt.log.read(task_id=task_id)[0]
                self.assertEqual(signed_event.key_id, "test-key")
                self.assertIsNotNone(signed_event.signature)
                report = rt.verify_chain()
                self.assertTrue(report.ok, report.describe())
                self.assertEqual(report.signed, rt.log.count())

            with Runtime(db, audit_key="wrong") as rt:
                report = rt.verify_chain()
                self.assertFalse(report.ok)
                self.assertEqual(report.reason, "signature mismatch")

    def test_task_state_is_folded_from_events(self):
        with Runtime(":memory:") as rt:
            task_id = rt.create_task("Fold me")
            rt.battle(task_id)
            first = rt.task(task_id)
            again = rt.task(task_id)
            self.assertEqual(first.status, again.status)
            self.assertEqual(len(first.messages), 3)

    def test_agent_profiles_are_projected_from_events(self):
        with Runtime(":memory:") as rt:
            task_id = rt.create_task("Profile agents")
            rt.battle(task_id)
            profiles = rt.profiles()
            from_events = agent_profiles(rt.log.read())

            self.assertEqual(profiles, from_events)
            self.assertEqual(profiles["product"]["supports"], 1)
            self.assertEqual(profiles["test"]["challenges"], 1)
            self.assertIn("Ship the smallest", profiles["product"]["last_claim"])

    def test_battle_records_cabinet_vote_tally(self):
        with Runtime(":memory:") as rt:
            task_id = rt.create_task("Tally me")
            result = rt.battle(task_id)
            task = rt.task(task_id)
            event_types = [event.type for event in rt.log.read(task_id=task_id)]

            self.assertIn("cabinet.voted", event_types)
            self.assertIn("agent.decided", event_types)
            self.assertEqual(task.cabinet["approval_threshold"], 2)
            self.assertEqual(task.cabinet["quorum"], 3)
            self.assertEqual(task.cabinet["status"], "vetoed")
            self.assertEqual(task.cabinet["vetoed_by"], ["test"])
            self.assertEqual(len(task.cabinet["votes"]), 3)
            self.assertEqual(result["brief"]["cabinet"], task.cabinet)

    def test_runtime_accepts_external_role_and_driver_configs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            roles = root / "roles.json"
            drivers = root / "drivers.json"
            roles.write_text(
                '{"cabinet":{"approval_threshold":1,"quorum":1},"roles":[{"id":"fixture","name":"Fixture","role":"tester","driver":"fixture"}]}',
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
                self.assertEqual(rt.task(task_id).cabinet["status"], "recommended")
                self.assertEqual(rt.task(task_id).status, "decided")

    def test_supreme_decides_when_cabinet_has_no_consensus(self):
        with tempfile.TemporaryDirectory() as tmp:
            roles = Path(tmp) / "roles.json"
            roles.write_text(
                """
                {
                  "cabinet": {"approval_threshold": 3, "quorum": 3, "veto_roles": []},
                  "roles": [
                    {"id":"product","name":"Product","role":"product","driver":"product"},
                    {"id":"dev","name":"Developer","role":"developer","driver":"dev"},
                    {"id":"test","name":"Tester","role":"tester","driver":"test"}
                  ]
                }
                """,
                encoding="utf-8",
            )
            with Runtime(":memory:", roles_path=roles) as rt:
                task_id = rt.create_task("Need supreme fallback")
                rt.battle(task_id)
                task = rt.task(task_id)
                event_types = [event.type for event in rt.log.read(task_id=task_id)]

                self.assertEqual(task.cabinet["status"], "needs_supreme")
                self.assertEqual(task.decision["source"], "supreme")
                self.assertEqual(task.status, "rework")
                self.assertIn("supreme.decided", event_types)
                self.assertEqual(rt.inbox(), [])

    def test_driver_failure_is_contained_and_opens_circuit(self):
        driver = AlwaysFailDriver()
        with Runtime(
            ":memory:",
            drivers={"product": driver},
            driver_attempts=2,
            circuit_breaker_threshold=2,
        ) as rt:
            task_id = rt.create_task("Contain failures")
            rt.battle(task_id)
            task = rt.task(task_id)

            self.assertEqual(driver.calls, 2)
            self.assertEqual(len(task.driver_failures), 2)
            self.assertIn("model process exploded", task.driver_failures[0]["message"])
            self.assertTrue(any("driver failed" in msg["claim"] for msg in task.messages))
            self.assertIn("driver.failed", [event.type for event in rt.log.read(task_id=task_id)])

            rt.battle(task_id)
            task = rt.task(task_id)
            self.assertEqual(driver.calls, 2)
            self.assertTrue(any(item["attempt"] == 0 for item in task.driver_failures))


class CabinetTests(unittest.TestCase):
    def test_majority_threshold(self):
        self.assertEqual(majority_threshold(0), 1)
        self.assertEqual(majority_threshold(1), 1)
        self.assertEqual(majority_threshold(2), 2)
        self.assertEqual(majority_threshold(3), 2)
        self.assertEqual(majority_threshold(4), 3)

    def test_tally_votes_recommends_clear_majority(self):
        tally = tally_votes(
            [
                {"actor": "product", "option_id": "A", "stance": "assert", "claim": "ship"},
                {"actor": "dev", "option_id": "A", "stance": "assert", "claim": "ship"},
                {"actor": "test", "option_id": "B", "stance": "challenge", "claim": "risk"},
            ]
        )
        self.assertEqual(tally["recommendation"], "A")
        self.assertEqual(tally["status"], "recommended")
        self.assertEqual(tally["options"][0]["id"], "A")

    def test_tally_votes_applies_quorum_and_veto_roles(self):
        low_quorum = tally_votes(
            [{"actor": "product", "option_id": "A", "stance": "assert"}],
            quorum=2,
        )
        self.assertEqual(low_quorum["status"], "needs_human")
        self.assertIn("Quorum", low_quorum["reason"])

        vetoed = tally_votes(
            [
                {"actor": "product", "option_id": "A", "stance": "assert"},
                {"actor": "test", "option_id": "A", "stance": "challenge"},
            ],
            approval_threshold=1,
            quorum=2,
            veto_roles=["test"],
        )
        self.assertEqual(vetoed["status"], "vetoed")
        self.assertEqual(vetoed["vetoed_by"], ["test"])

    def test_tally_votes_routes_unresolved_cabinet_to_supreme(self):
        tally = tally_votes(
            [
                {"actor": "product", "option_id": "A", "stance": "assert"},
                {"actor": "dev", "option_id": "B", "stance": "assert"},
                {"actor": "test", "option_id": "C", "stance": "challenge"},
            ],
            approval_threshold=3,
            quorum=3,
        )
        self.assertEqual(tally["status"], "needs_supreme")
        self.assertEqual(tally["top_option"], "A")


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

    def test_cli_chain_reports_signed_events(self):
        code = main(["--db", self.db, "--audit-key", "secret", "demo"])
        self.assertEqual(code, 0)
        self.out.seek(0)
        self.out.truncate(0)
        code = main(["--db", self.db, "--audit-key", "secret", "chain"])
        self.assertEqual(code, 0)
        self.assertIn("signed", self.out.getvalue())

    def test_cli_new_battle_inbox_decide(self):
        code, out = self.run_cli("new", "Build demo", "--accept", "three roles", "--battle")
        self.assertEqual(code, 0)
        task_id = next(line for line in out.splitlines() if line.startswith("created ")).split()[1]

        code, out = self.run_cli("show", task_id)
        self.assertEqual(code, 0)
        self.assertIn("cabinet=vetoed", out)
        self.assertIn("ladder:", out)
        self.assertIn("Cabinet: vetoed", out)

        code, out = self.run_cli("inbox")
        self.assertEqual(code, 0)
        self.assertIn("inbox empty", out)

        code, out = self.run_cli(
            "command",
            task_id,
            "Change the goal",
            "--goal",
            "Build an autonomous loop",
            "--accept",
            "no human approval required",
        )
        self.assertEqual(code, 0)
        self.assertIn("command recorded", out)

        code, out = self.run_cli("decide", task_id, "--resolution", "done", "--reason", "ok")
        self.assertEqual(code, 0)
        self.assertIn("manual override recorded", out)

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

    def test_cli_profiles(self):
        code, _ = self.run_cli("demo")
        self.assertEqual(code, 0)
        code, out = self.run_cli("profiles")
        self.assertEqual(code, 0)
        self.assertIn("product", out)
        self.assertIn("challenges=1", out)


class WebTests(unittest.TestCase):
    def test_render_task_includes_cabinet_timeline_and_decision_form(self):
        with Runtime(":memory:") as rt:
            task_id = rt.create_task("Render me")
            rt.battle(task_id)
            events = rt.log.read(task_id=task_id)
            html = render_task(rt.task(task_id), events)
            ladder = decision_ladder(events)

        self.assertIn("Cabinet", html)
        self.assertIn("Decision Ladder", html)
        self.assertIn("Timeline", html)
        self.assertIn("Human Command", html)
        self.assertIn("cabinet.voted", html)
        self.assertIn("agent.decided", html)
        self.assertEqual([step["id"] for step in ladder], ["battle", "cabinet", "supreme", "human"])
        self.assertEqual(ladder[1]["status"], "vetoed")

    def test_web_api_and_decision_post(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "web.db")
            with Runtime(db) as rt:
                task_id = rt.create_task("Serve me")
                rt.battle(task_id)

            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(db))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            self.addCleanup(server.server_close)
            self.addCleanup(thread.join, 1)
            self.addCleanup(server.shutdown)

            with urlopen(f"{base}/api/tasks/{task_id}", timeout=5) as response:
                data = response.read().decode("utf-8")
            self.assertIn('"cabinet"', data)
            self.assertIn("cabinet.voted", data)
            self.assertIn('"decision_ladder"', data)

            body = urlencode({"resolution": "done", "reason": "Looks good"}).encode("utf-8")
            request = Request(
                f"{base}/tasks/{task_id}/decide",
                data=body,
                method="POST",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            with urlopen(request, timeout=5) as response:
                page = response.read().decode("utf-8")
            self.assertIn("Looks good", page)

            with Runtime(db) as rt:
                self.assertEqual(rt.task(task_id).status, "done")

    def test_web_can_create_task_and_run_battle(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "web-create.db")
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(db))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            self.addCleanup(server.server_close)
            self.addCleanup(thread.join, 1)
            self.addCleanup(server.shutdown)

            body = urlencode(
                {
                    "goal": "Create from web",
                    "acceptance": "battle runs\ninbox updates",
                    "battle": "on",
                }
            ).encode("utf-8")
            request = Request(
                f"{base}/tasks",
                data=body,
                method="POST",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            with urlopen(request, timeout=5) as response:
                page = response.read().decode("utf-8")

            self.assertIn("Create from web", page)
            self.assertIn("Cabinet", page)
            with Runtime(db) as rt:
                tasks = rt.tasks()
                self.assertEqual(len(tasks), 1)
                self.assertEqual(tasks[0].status, "rework")
                self.assertEqual(tasks[0].acceptance, ["battle runs", "inbox updates"])

            with urlopen(f"{base}/", timeout=5) as response:
                index = response.read().decode("utf-8")
            self.assertIn("Agent Profiles", index)
            self.assertIn("Create from web", index)


if __name__ == "__main__":
    unittest.main()
