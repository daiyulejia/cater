from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from cater.drivers import DriverResponse
from cater.ladder import decision_ladder
from cater.path import organization_path
from cater.runtime import Runtime, load_runtime_config
from cater.web import task_api_payload


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "compact_roles.json"


class RecordingDriver:
    def __init__(self, *, option="A", confidence=0.9, stance="assert"):
        self.prompts = []
        self.response = {
            "option_id": option, "confidence": confidence, "stance": stance,
            "claim": "Check the actual result", "text": "Check the actual result",
            "evidence": ["A regression case needs to be checked"],
        }

    def complete(self, *, role, prompt, context):
        self.prompts.append(prompt)
        return DriverResponse(text=json.dumps(self.response), tokens=3)


class CompactOrganizationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.roles_path = Path(self.tmp.name) / "roles.json"
        self.config = {
            "organization": {
                "leader": {"id": "lead", "name": "Lead", "role": "coordinator"},
                "cabinet_members": [],
                "worker_members": ["dev", "test"],
            },
            "roles": [
                {"id": "dev", "name": "Builder", "role": "developer", "driver": "dev"},
                {"id": "test", "name": "Checker", "role": "tester", "driver": "test"},
            ],
        }
        self.write_config()

    def write_config(self):
        self.roles_path.write_text(json.dumps(self.config), encoding="utf-8")

    def runtime(self, **kwargs):
        rt = Runtime(":memory:", roles_path=self.roles_path, **kwargs)
        self.addCleanup(rt.close)
        return rt

    def test_empty_cabinet_is_explicit_and_workers_are_still_required(self):
        self.assertFalse(load_runtime_config(self.roles_path).organization.cabinet_members)
        for field, values in {
            "cabinet_members": [None, "", ["missing"], ["dev", "dev"], [False]],
            "worker_members": [[], None, "", ["missing"]],
        }.items():
            original = self.config["organization"][field]
            for value in values:
                with self.subTest(field=field, value=value):
                    self.config["organization"][field] = value
                    self.write_config()
                    with self.assertRaises(ValueError):
                        load_runtime_config(self.roles_path)
            self.config["organization"][field] = original
        del self.config["organization"]["cabinet_members"]
        self.write_config()
        with self.assertRaises(ValueError):
            load_runtime_config(self.roles_path)

    def test_unused_voting_policy_is_not_silently_discarded(self):
        for policy in ({"quorum": 3}, {"approval_threshold": 2}, {"veto_roles": ["test"]}):
            with self.subTest(policy=policy):
                self.config["cabinet"] = policy
                self.write_config()
                with self.assertRaisesRegex(ValueError, "cabinet policy requires cabinet members"):
                    load_runtime_config(self.roles_path)

    def test_legacy_config_keeps_its_existing_membership(self):
        del self.config["organization"]
        self.write_config()
        organization = load_runtime_config(self.roles_path).organization
        self.assertEqual(organization.cabinet_members, {"dev", "test"})

    def test_complex_intake_and_explicit_dispatch_use_real_leader(self):
        rt = self.runtime()
        task_id = rt.create_task("Refactor architecture", ["Build passes", "Tests pass", "Preview works"])
        intake = rt.intake(task_id, requested_route="direct", preferred_owner="dev")
        self.assertEqual(intake["route"]["route"], "collaborate")
        result = rt.dispatch(task_id)
        task = rt.task(task_id)
        events = rt.log.read(task_id=task_id)
        self.assertEqual(result["plan"]["source"], "leader")
        self.assertEqual(result["plan"]["flow"], ["leader", "workers", "review"])
        self.assertEqual(result["plan"]["participants"], ["dev", "test"])
        self.assertEqual(result["plan"]["members"], [])
        self.assertEqual(organization_path(task, events)[0]["status"], "leader_dispatch")
        self.assertFalse(any(event.type.startswith("cabinet.") for event in events))
        self.assertTrue(all(event.actor.id == "lead" for event in events if event.type == "task.decomposed"))

    def test_small_direct_assignment_calls_only_selected_worker(self):
        builder, checker = RecordingDriver(), RecordingDriver()
        rt = self.runtime(drivers={"dev": builder, "test": checker})
        task_id = rt.create_task("Fix a typo")
        rt.intake(task_id, requested_route="direct", preferred_owner="dev")
        result = rt.collaborate(task_id)
        self.assertEqual(len(builder.prompts), 1)
        self.assertEqual(len(checker.prompts), 0)
        self.assertIsNone(result["brief"]["cabinet"])
        self.assertEqual(rt.task(task_id).status, "running")
        self.assertEqual(rt.task(task_id).deliverables, [])
        self.assertIsNone(rt.task(task_id).decision)
        self.assertIn("尚未提交", rt.task(task_id).work["items"][0]["reason"])

    def test_risk_is_retained_without_fabricating_votes_or_human_escalation(self):
        for risk in ({"option": "B"}, {"stance": "challenge"}, {"confidence": 0.2}):
            with self.subTest(risk=risk):
                builder, checker = RecordingDriver(), RecordingDriver(**risk)
                with Runtime(":memory:", roles_path=self.roles_path, drivers={"dev": builder, "test": checker}) as rt:
                    task_id = rt.create_task("Refactor architecture")
                    # Historical unplanned collaboration still retains its risk policy.
                    result = rt.collaborate(task_id)
                    task = rt.task(task_id)
                    events = rt.log.read(task_id=task_id)
                    self.assertEqual(task.status, "rework")
                    self.assertEqual(task.decision["source"], "leader")
                    self.assertIsNone(task.decision["option_id"])
                    self.assertEqual(result["brief"]["consensus"], [])
                    self.assertIsNone(result["brief"]["cabinet"])
                    self.assertEqual(len(result["brief"]["positions"]), 2)
                    self.assertEqual([len(builder.prompts), len(checker.prompts)], [1, 1])
                    self.assertEqual(result["brief"]["governance"]["handler"], "leader")
                    self.assertFalse(any(event.type.startswith("cabinet.") for event in events))
                    self.assertNotIn("escalation.triggered", [event.type for event in events])
                    self.assertEqual(rt.inbox(), [])
                    self.assertEqual([step["status"] for step in decision_ladder(events)][1:3], ["skipped", "done"])
                    self.assertTrue(rt.verify_chain().ok)
                    rt.collaborate(task_id)
                    self.assertIn("rework_request=", checker.prompts[1])
                    checker.response.update(builder.response)
                    rt.collaborate(task_id)
                    self.assertEqual(rt.task(task_id).status, "decided")

    def test_forced_review_keeps_unknown_work_unaccepted(self):
        rt = self.runtime(drivers={"dev": RecordingDriver(), "test": RecordingDriver()})
        task_id = rt.create_task("Review the work")
        rt.dispatch(task_id)
        rt.command(task_id, "请内阁复核方案")
        self.assertEqual(rt.task(task_id).plan["next_action"], "battle")
        self.assertNotIn("内阁", rt.task(task_id).plan["reason"])
        self.assertEqual(organization_path(rt.task(task_id), rt.log.read(task_id=task_id))[0]["status"], "leader_dispatch")
        rt.battle(task_id)
        self.assertEqual(rt.task(task_id).status, "rework")

    def test_command_only_assignments_do_not_invent_a_cabinet_dispatch(self):
        rt = self.runtime()
        task_id = rt.create_task("Check the result")
        rt.command(task_id, "@test 检查结果")
        path = organization_path(rt.task(task_id), rt.log.read(task_id=task_id))
        self.assertEqual(path[0]["status"], "assigned")

    def test_compact_example_replays_without_the_example_config(self):
        db = Path(self.tmp.name) / "history.db"
        with Runtime(db, roles_path=EXAMPLE) as rt:
            task_id = rt.create_task("Refactor architecture")
            rt.intake(task_id)
            rt.collaborate(task_id)
            # Seed one evidence-backed acceptance; the demo need not finish every stage.
            submitted = next(item for item in rt.work.view(task_id)["items"] if item["status"] == "submitted"
                             and all(c["kind"] == "review" for c in item["criteria"]))
            artifact = rt.work.artifacts(task_id, submitted["submission"])[0]
            accepted = rt.work.review(task_id, submitted["id"], reviewer=submitted["reviewer"],
                submission_seq=submitted["submission"]["seq"], results=[{
                    "id": c["id"], "status": "passed", "reason": "Inspected the stored demo artifact",
                    "artifact_seq": artifact["seq"], "quote": artifact["content"],
                } for c in submitted["criteria"]])
            self.assertEqual(accepted.payload["status"], "accepted")
            before = task_api_payload(rt, task_id)
            self.assertTrue(before["task"]["work"]["scope_current"])
            self.assertEqual(next(item for item in before["task"]["work"]["items"]
                                  if item["id"] == submitted["id"])["status"], "accepted")
            self.assertEqual([role.name for role in rt.worker_roles], ["月亮", "罐头", "米寇", "三角"])
            self.assertFalse(rt.cabinet_roles)
        with Runtime(db) as rt:
            after = task_api_payload(rt, task_id)
            for key in ("events", "decision_ladder", "organization_path"):
                self.assertEqual(before[key], after[key])
            self.assertEqual({k: v for k, v in before["task"].items() if k != "work"},
                             {k: v for k, v in after["task"].items() if k != "work"})
            previous, current = before["task"]["work"], after["task"]["work"]
            self.assertEqual(current["plan_seq"], previous["plan_seq"])
            self.assertEqual(current["scope"], previous["scope"])
            self.assertNotEqual(current["scope"]["organization"], rt.work.scope(task_id)["organization"])
            self.assertFalse(current["scope_current"])
            self.assertFalse(current["stage_accepted"])
            self.assertEqual([item["id"] for item in current["items"]], [item["id"] for item in previous["items"]])
            for prior, item in zip(previous["items"], current["items"]):
                self.assertEqual(item["status"], "stale")
                self.assertEqual(item["submission"], prior["submission"])
                self.assertEqual(item["checks"], prior["checks"])
            self.assertEqual(after["task"]["plan"]["source"], "leader")
            self.assertTrue(rt.verify_chain().ok)


if __name__ == "__main__":
    unittest.main()
