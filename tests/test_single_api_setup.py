from __future__ import annotations

import json
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

from cater.availability import team_availability
from cater.cli import _doctor_report
from cater.drivers import DriverUnavailable, OpenAICompatibleDriver, driver_mode, load_drivers
from cater.runtime import Runtime, load_runtime_config


EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
ROLES_PATH = EXAMPLES / "single_api_roles.json"
DRIVERS_PATH = EXAMPLES / "single_api_driver_config.json"
CHANNEL = "shared_api"
KEY_ENV = "CATER_SHARED_API_KEY"
CABINET = {"cabinet_product", "cabinet_engineering", "cabinet_quality"}
WORKERS = {"product", "design", "dev", "test"}
MEMBERS = {"store_manager"} | CABINET | WORKERS


class SingleApiSetupTests(unittest.TestCase):
    def setUp(self):
        # Replace the environment object so no real credentials are inspected.
        self.env = self.enterContext(patch("os.environ", {}))
        self.open_request = self.enterContext(patch(
            "cater.drivers._open_api_request",
            side_effect=AssertionError("API requests are forbidden in setup tests"),
        ))
        self.socket = self.enterContext(patch(
            "socket.socket",
            side_effect=AssertionError("Network access is forbidden in setup tests"),
        ))
        self.addCleanup(self.open_request.assert_not_called)
        self.addCleanup(self.socket.assert_not_called)

    def runtime(self):
        return self.enterContext(Runtime(
            ":memory:", roles_path=ROLES_PATH, drivers_path=DRIVERS_PATH,
        ))

    def test_templates_load_one_explicit_shared_api_channel(self):
        drivers = load_drivers(DRIVERS_PATH)
        self.assertEqual(set(drivers), {CHANNEL})
        driver = drivers[CHANNEL]
        self.assertIsInstance(driver, OpenAICompatibleDriver)
        self.assertEqual(driver.name, CHANNEL)
        self.assertEqual(
            driver.endpoint,
            "https://replace-with-provider.example.invalid/v1/chat/completions",
        )
        self.assertEqual(driver.model, "replace-with-model-id")
        self.assertEqual(driver.api_key_env, KEY_ENV)
        raw_driver = json.loads(DRIVERS_PATH.read_text(encoding="utf-8"))["drivers"][0]
        self.assertNotIn("api_key", raw_driver)

        config = load_runtime_config(ROLES_PATH)
        self.assertEqual(config.organization.leader.id, "store_manager")
        self.assertEqual(config.organization.leader.driver, CHANNEL)
        self.assertEqual(config.organization.cabinet_members, CABINET)
        self.assertEqual(config.organization.worker_members, WORKERS)
        self.assertEqual({role.id for role in config.roles}, CABINET | WORKERS)
        self.assertEqual({role.driver for role in config.roles}, {CHANNEL})

    def test_runtime_binds_leader_and_every_member_to_the_same_driver(self):
        rt = self.runtime()
        shared = rt.drivers[CHANNEL]
        self.assertIsInstance(shared, OpenAICompatibleDriver)
        members = [rt.organization.leader, *rt.cabinet_roles, *rt.worker_roles]
        self.assertEqual({role.id for role in members}, MEMBERS)
        for role in members:
            with self.subTest(role=role.id):
                self.assertEqual(role.driver, CHANNEL)
                self.assertIs(rt.drivers[role.driver], shared)

    def test_roles_require_the_external_driver_config(self):
        with self.assertRaisesRegex(ValueError, "missing driver.*shared_api"):
            with Runtime(":memory:", roles_path=ROLES_PATH):
                self.fail("The shared channel must not fall back to demo drivers")

    def test_leader_intake_requests_model_planning_without_an_api_call(self):
        rt = self.runtime()
        with patch.object(rt.drivers[CHANNEL], "complete") as complete:
            task_id = rt.create_task("Check the single API setup")
            result = rt.intake(task_id)
            self.assertEqual(result["route"]["route"], "planning")
            events = rt.log.read(task_id=task_id)
            planning = [event for event in events if event.type == "planning.requested"]
            self.assertEqual(len(planning), 1)
            self.assertEqual(planning[0].actor.id, "store_manager")
            self.assertNotIn("task.interpreted", [event.type for event in events])
            complete.assert_not_called()

    def test_missing_or_empty_key_marks_every_member_on_leave_and_blocks_requests(self):
        rt = self.runtime()
        for value in (None, ""):
            with self.subTest(key=value):
                if value is None:
                    self.env.pop(KEY_ENV, None)
                else:
                    self.env[KEY_ENV] = value
                with patch.object(rt.drivers[CHANNEL], "complete") as complete:
                    result = team_availability(rt)
                    complete.assert_not_called()
                self.assertFalse(result["all_available"])
                self.assertEqual({item["role_id"] for item in result["members"]}, MEMBERS)
                self.assertEqual(len(result["members"]), len(MEMBERS))
                for item in result["members"]:
                    self.assertEqual((item["status"], item["reason"]), ("on_leave", "missing_key"))
                with self.assertRaisesRegex(DriverUnavailable, KEY_ENV):
                    rt.drivers[CHANNEL].complete(
                        role=rt.organization.leader.role, prompt="Return JSON", context={},
                    )

    def test_fixture_key_is_unverified_without_probing_or_exposing_it(self):
        self.env[KEY_ENV] = "offline-fixture-not-a-real-key"
        rt = self.runtime()
        with patch.object(rt.drivers[CHANNEL], "complete") as complete:
            result = team_availability(rt)
            complete.assert_not_called()
        self.assertFalse(result["all_available"])
        self.assertEqual({item["role_id"] for item in result["members"]}, MEMBERS)
        for item in result["members"]:
            self.assertEqual(
                (item["status"], item["reason"]),
                ("unverified", "not_recently_checked"),
            )
        serialized = json.dumps(result)
        self.assertNotIn(self.env[KEY_ENV], serialized)
        self.assertNotIn(KEY_ENV, serialized)
        self.assertNotIn(rt.drivers[CHANNEL].endpoint, serialized)

    def test_default_runtime_still_uses_local_policy_and_demo_drivers(self):
        with Runtime(":memory:") as rt:
            self.assertIsNone(rt.organization.leader.driver)
            self.assertNotIn(CHANNEL, rt.drivers)
            for role in rt.roles:
                self.assertEqual(driver_mode(rt.drivers[role.driver]), "local_demo")

    def test_doctor_lists_the_configured_leader_without_calling_the_api(self):
        rt = self.runtime()
        with patch.object(rt.drivers[CHANNEL], "complete") as complete:
            report = _doctor_report(rt, Namespace(db=":memory:"), probe=True)
            complete.assert_not_called()
        self.assertTrue(report["ok"])
        self.assertEqual(report["roles"], len(MEMBERS))
        self.assertEqual({role["id"] for role in report["role_checks"]}, MEMBERS)
        for role in report["role_checks"]:
            self.assertEqual(role["driver"], CHANNEL)
            self.assertEqual(role["status"], "configured")
            self.assertIsNone(role["probe"])

    def test_doctor_reports_missing_leader_driver_as_unhealthy(self):
        rt = self.runtime()
        del rt.drivers[CHANNEL]
        report = _doctor_report(rt, Namespace(db=":memory:"), probe=False)
        self.assertFalse(report["ok"])
        leader = next(role for role in report["role_checks"] if role["id"] == "store_manager")
        self.assertEqual(leader["status"], "missing")


if __name__ == "__main__":
    unittest.main()
