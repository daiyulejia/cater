from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from cater.availability import team_availability
from cater.drivers import OpenAICompatibleDriver
from cater.events import Actor
from cater.runtime import Runtime


class AvailabilityTests(unittest.TestCase):
    def api(self, name="product"):
        return OpenAICompatibleDriver(name, endpoint="https://fixture.test/v1/chat/completions", model="fixture", api_key_env="CATER_AVAILABILITY_KEY")

    def member(self, rt, id="product"):
        return next(item for item in team_availability(rt)["members"] if item["role_id"] == id)

    def test_demo_is_not_real_all_available_and_leader_is_local_policy(self):
        with Runtime(":memory:") as rt:
            result = team_availability(rt)
            self.assertFalse(result["all_available"])
            self.assertEqual(len(result["members"]), len({item["role_id"] for item in result["members"]}))
            self.assertEqual(self.member(rt)["status"], "demo")
            self.assertEqual(self.member(rt, rt.organization.leader.id)["reason"], "local_policy")

    def test_missing_key_is_on_leave_without_probing_or_exposing_secrets(self):
        driver = self.api()
        with Runtime(":memory:", drivers={"product": driver}) as rt, patch.dict("cater.availability.os.environ", {}, clear=True), patch.object(driver, "complete") as complete:
            self.assertEqual(self.member(rt)["reason"], "missing_key")
            complete.assert_not_called()
        with Runtime(":memory:", drivers={"product": driver}) as rt, patch.dict("cater.availability.os.environ", {"CATER_AVAILABILITY_KEY": "never-expose-this"}):
            result = team_availability(rt)
            self.assertEqual(self.member(rt)["status"], "unverified")
            self.assertNotIn("never-expose-this", json.dumps(result))
            self.assertNotIn("fixture.test", json.dumps(result))
            self.assertNotIn("CATER_AVAILABILITY_KEY", json.dumps(result))

    def test_success_failure_and_expiry_are_shared_by_driver_roles(self):
        with Runtime(":memory:", drivers={"product": self.api()}) as rt, patch.dict("cater.availability.os.environ", {"CATER_AVAILABILITY_KEY": "fixture"}):
            with patch("cater.runtime.time.monotonic", return_value=10):
                rt._driver_health.record_success("product")
                self.assertEqual(self.member(rt)["reason"], "recent_success")
                self.assertEqual(self.member(rt, "cabinet_product")["status"], "available")
                rt._driver_health.record_failure("product")
                self.assertEqual(self.member(rt)["reason"], "recent_failure")
                self.assertEqual(self.member(rt)["status"], "unverified")
                self.assertEqual(self.member(rt, "cabinet_product")["status"], "unverified")
                rt._driver_health.record_success("product")
            with patch("cater.runtime.time.monotonic", return_value=311):
                self.assertEqual(self.member(rt)["status"], "unverified")

    def test_budget_and_circuit_take_precedence_over_previous_success(self):
        driver = self.api(); driver.token_budget = 5
        with Runtime(":memory:", drivers={"product": driver}) as rt, patch.dict("cater.availability.os.environ", {"CATER_AVAILABILITY_KEY": "fixture"}):
            rt._driver_health.record_success("product")
            task = rt.create_task("预算状态")
            rt.log.emit(actor=Actor.agent("product", "worker"), type="msg.said", task_id=task, payload={"driver": "product", "tokens": 5, "token_usage": "reported"})
            self.assertEqual(self.member(rt)["reason"], "budget_exhausted")
            driver.token_budget = None
            for _ in range(rt.circuit_breaker_threshold): rt._driver_health.record_failure("product")
            self.assertEqual(self.member(rt)["reason"], "circuit_open")

    def test_old_event_success_does_not_claim_new_server_connection_is_available(self):
        with Runtime(":memory:", drivers={"product": self.api()}) as rt, patch.dict("cater.availability.os.environ", {"CATER_AVAILABILITY_KEY": "fixture"}):
            task = rt.create_task("历史成功")
            rt.log.emit(actor=Actor.agent("product", "worker"), type="msg.said", task_id=task, payload={"driver": "product", "tokens": 1, "token_usage": "reported"})
            self.assertEqual(self.member(rt)["status"], "unverified")

    def test_all_available_requires_recent_success_for_every_real_channel(self):
        drivers = {name: self.api(name) for name in ("product", "design", "dev", "test")}
        with Runtime(":memory:", drivers=drivers) as rt, patch.dict("cater.availability.os.environ", {"CATER_AVAILABILITY_KEY": "fixture"}):
            for name in ("product", "design", "dev"): rt._driver_health.record_success(name)
            self.assertFalse(team_availability(rt)["all_available"])
            rt._driver_health.record_success("test")
            self.assertTrue(team_availability(rt)["all_available"])
            rt._driver_health.record_failure("test")
            self.assertFalse(team_availability(rt)["all_available"])
