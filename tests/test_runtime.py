from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import threading
import tomllib
import unittest
from pathlib import Path
from http.server import ThreadingHTTPServer
from urllib.parse import urlencode
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from cater.cabinet import majority_threshold, tally_votes
from cater import __version__
from cater.cli import main
from cater.drivers import CliDriver, DriverResponse, load_drivers
from cater.ladder import decision_ladder
from cater.messages import message_relation
from cater.profiles import agent_profiles
from cater.runtime import Runtime
from cater.web import make_handler, render_index, render_task


class AlwaysFailDriver:
    name = "product"

    def __init__(self):
        self.calls = 0

    def complete(self, *, role: str, prompt: str, context: dict) -> DriverResponse:
        self.calls += 1
        raise RuntimeError("model process exploded")


class PromptCaptureDriver:
    name = "memory"

    def __init__(self):
        self.calls = 0
        self.prompts: list[str] = []

    def complete(self, *, role: str, prompt: str, context: dict) -> DriverResponse:
        self.calls += 1
        self.prompts.append(prompt)
        text = (
            '{"stance":"assert","option_id":"M","claim":"memory claim '
            + str(self.calls)
            + '","evidence":["captured prompt"],"confidence":0.8,"text":"ok"}'
        )
        return DriverResponse(text=text, tokens=3)


class StaticResponseDriver:
    def __init__(self, text: str):
        self.text = text

    def complete(self, *, role: str, prompt: str, context: dict) -> DriverResponse:
        return DriverResponse(text=self.text, tokens=5)


class ReplyDriver:
    def __init__(self):
        self.prompts: list[str] = []

    def complete(self, *, role: str, prompt: str, context: dict) -> DriverResponse:
        self.prompts.append(prompt)
        if role == "planner":
            text = {
                "stance": "assert",
                "option_id": "shared-plan",
                "claim": "Draft the plan",
                "evidence": ["first worker output"],
                "confidence": 0.8,
                "text": "planner output",
            }
        else:
            text = {
                "stance": "assert",
                "option_id": "shared-plan",
                "claim": "Continue the plan from planner",
                "evidence": ["uses prior event"],
                "confidence": 0.8,
                "text": "builder output",
                "target": "alpha",
                "reply_to": 3,
            }
        return DriverResponse(text=json.dumps(text), tokens=4)


def start_test_web(testcase: unittest.TestCase, db: str) -> str:
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(db))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    testcase.addCleanup(server.server_close)
    testcase.addCleanup(thread.join, 1)
    testcase.addCleanup(server.shutdown)
    return f"http://127.0.0.1:{server.server_port}"


class RuntimeTests(unittest.TestCase):
    def test_message_relation_formats_optional_reply_metadata(self):
        self.assertEqual(message_relation({}), "")
        self.assertEqual(message_relation({"target": "alpha"}), "target alpha")
        self.assertEqual(
            message_relation({"target": "alpha", "reply_to": 3}),
            "target alpha | reply_to #3",
        )

    def test_package_version_matches_project_metadata(self):
        project = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(project["project"]["version"], __version__)

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

    def test_profile_memory_is_fed_into_later_collaboration_prompts(self):
        driver = PromptCaptureDriver()
        with tempfile.TemporaryDirectory() as tmp:
            roles = Path(tmp) / "roles.json"
            roles.write_text(
                """
                {
                  "cabinet": {"approval_threshold": 1, "quorum": 1, "veto_roles": []},
                  "roles": [
                    {"id":"memory","name":"Memory","role":"memory","driver":"memory"}
                  ]
                }
                """,
                encoding="utf-8",
            )
            with Runtime(":memory:", roles_path=roles, drivers={"memory": driver}) as rt:
                task_id = rt.create_task("Carry memory forward")
                rt.collaborate(task_id)
                rt.command(task_id, "Continue with what you learned")
                rt.collaborate(task_id)

        self.assertEqual(driver.calls, 2)
        self.assertIn("Profile memory: no prior activity", driver.prompts[0])
        self.assertIn("Collaboration policy", driver.prompts[1])
        self.assertIn("messages=1", driver.prompts[1])
        self.assertIn("supports=1", driver.prompts[1])
        self.assertIn("last_claim=memory claim 1", driver.prompts[1])

    def test_task_memory_shares_prior_agent_outputs_between_roles(self):
        driver = PromptCaptureDriver()
        with tempfile.TemporaryDirectory() as tmp:
            roles = Path(tmp) / "roles.json"
            roles.write_text(
                """
                {
                  "governance": {"trigger_on_option_split": false},
                  "cabinet": {"approval_threshold": 2, "quorum": 2, "veto_roles": []},
                  "roles": [
                    {"id":"alpha","name":"Alpha","role":"planner","driver":"memory"},
                    {"id":"beta","name":"Beta","role":"builder","driver":"memory"}
                  ]
                }
                """,
                encoding="utf-8",
            )
            with Runtime(":memory:", roles_path=roles, drivers={"memory": driver}) as rt:
                task_id = rt.create_task("Share work")
                rt.collaborate(task_id)
                rt.command(task_id, "Use prior output")
                rt.collaborate(task_id)

        self.assertEqual(driver.calls, 4)
        self.assertIn("Current round memory: no earlier outputs", driver.prompts[0])
        self.assertIn("Current round memory: alpha/assert: memory claim 1", driver.prompts[1])
        self.assertIn("recent_commands=Use prior output", driver.prompts[2])
        self.assertIn("alpha/assert: memory claim 1", driver.prompts[2])
        self.assertIn("beta/assert: memory claim 2", driver.prompts[2])

    def test_command_updates_acceptance_before_next_agent_prompt(self):
        driver = PromptCaptureDriver()
        with tempfile.TemporaryDirectory() as tmp:
            roles = Path(tmp) / "roles.json"
            roles.write_text(
                """
                {
                  "governance": {"trigger_on_option_split": false},
                  "cabinet": {"approval_threshold": 1, "quorum": 1, "veto_roles": []},
                  "roles": [
                    {"id":"memory","name":"Memory","role":"memory","driver":"memory"}
                  ]
                }
                """,
                encoding="utf-8",
            )
            with Runtime(":memory:", roles_path=roles, drivers={"memory": driver}) as rt:
                task_id = rt.create_task("Original goal", ["old acceptance"])
                rt.command(
                    task_id,
                    "Tighten the target",
                    goal="Updated goal",
                    acceptance=["new acceptance"],
                )
                rt.collaborate(task_id)

        self.assertIn("Goal: Updated goal", driver.prompts[0])
        self.assertIn("Acceptance: ['new acceptance']", driver.prompts[0])
        self.assertNotIn("old acceptance", driver.prompts[0])

    def test_agents_can_address_prior_outputs_by_event_seq(self):
        driver = ReplyDriver()
        with tempfile.TemporaryDirectory() as tmp:
            roles = Path(tmp) / "roles.json"
            roles.write_text(
                """
                {
                  "governance": {"trigger_on_option_split": false},
                  "cabinet": {"approval_threshold": 2, "quorum": 2, "veto_roles": []},
                  "roles": [
                    {"id":"alpha","name":"Alpha","role":"planner","driver":"reply"},
                    {"id":"beta","name":"Beta","role":"builder","driver":"reply"}
                  ]
                }
                """,
                encoding="utf-8",
            )
            with Runtime(":memory:", roles_path=roles, drivers={"reply": driver}) as rt:
                task_id = rt.create_task("Coordinate workers")
                result = rt.collaborate(task_id)
                task = rt.task(task_id)
                html = render_task(task, rt.log.read(task_id=task_id))

        self.assertIn("Agent-to-agent policy", driver.prompts[0])
        self.assertIn("alpha/assert: Draft the plan (#3)", driver.prompts[1])
        self.assertEqual(task.messages[1]["target"], "alpha")
        self.assertEqual(task.messages[1]["reply_to"], 3)
        self.assertEqual(result["brief"]["positions"][1]["target"], "alpha")
        self.assertEqual(result["brief"]["positions"][1]["reply_to"], 3)
        self.assertIn("reply_to #3", html)

    def test_invalid_agent_reply_metadata_is_dropped(self):
        driver = StaticResponseDriver(
            '{"stance":"assert","option_id":"A","claim":"Invalid link",'
            '"evidence":[],"confidence":0.8,"text":"bad refs","target":"ghost","reply_to":999}'
        )
        with tempfile.TemporaryDirectory() as tmp:
            roles = Path(tmp) / "roles.json"
            roles.write_text(
                """
                {
                  "cabinet": {"approval_threshold": 1, "quorum": 1, "veto_roles": []},
                  "roles": [
                    {"id":"alpha","name":"Alpha","role":"planner","driver":"static"}
                  ]
                }
                """,
                encoding="utf-8",
            )
            with Runtime(":memory:", roles_path=roles, drivers={"static": driver}) as rt:
                task_id = rt.create_task("Ignore invalid links")
                rt.collaborate(task_id)
                message = rt.task(task_id).messages[0]

        self.assertNotIn("target", message)
        self.assertNotIn("reply_to", message)

    def test_dispatch_records_pyramid_task_distribution(self):
        driver = PromptCaptureDriver()
        with tempfile.TemporaryDirectory() as tmp:
            roles = Path(tmp) / "roles.json"
            roles.write_text(
                """
                {
                  "cabinet": {"approval_threshold": 1, "quorum": 1, "veto_roles": []},
                  "roles": [
                    {"id":"product","name":"Product","role":"product","driver":"memory"},
                    {"id":"dev","name":"Developer","role":"developer","driver":"memory"}
                  ]
                }
                """,
                encoding="utf-8",
            )
            with Runtime(":memory:", roles_path=roles, drivers={"memory": driver}) as rt:
                task_id = rt.create_task("Build pyramid dispatch", ["plan is visible"])
                result = rt.dispatch(task_id)
                task = rt.task(task_id)
                event_types = [event.type for event in rt.log.read(task_id=task_id)]

                self.assertIn("mission.created", event_types)
                self.assertIn("task.decomposed", event_types)
                self.assertIn("subtask.assigned", event_types)
                self.assertIn("plan.proposed", event_types)
                self.assertEqual(result["plan"]["flow"], ["orchestrator", "cabinet", "workers", "review"])
                self.assertEqual(len(task.assignments), 2)
                self.assertEqual(task.assignments[0]["owner"], "product")
                self.assertEqual(task.assignments[1]["lane"], "implementation")
                self.assertEqual(task.assignments[1]["depends_on"], [task.assignments[0]["id"]])
                self.assertIn("deliverable", task.assignments[0])
                html = render_task(task, rt.log.read(task_id=task_id))
                self.assertIn("working implementation plan or code-ready change", html)
                self.assertIn("depends on:", html)

                rt.collaborate(task_id)

        self.assertIn("assigned_work=product:", driver.prompts[0])
        self.assertIn("dev:", driver.prompts[0])

    def test_dispatch_dependencies_follow_execution_lanes(self):
        with Runtime(":memory:") as rt:
            task_id = rt.create_task("Plan dependencies")
            rt.dispatch(task_id)
            task = rt.task(task_id)

        product, dev, test = task.assignments
        self.assertEqual(product["depends_on"], [])
        self.assertEqual(dev["depends_on"], [product["id"]])
        self.assertEqual(test["depends_on"], [product["id"], dev["id"]])
        self.assertEqual(test["deliverable"], "risk review, test strategy, and release confidence")

    def test_human_can_assign_directly_to_worker_layer(self):
        driver = PromptCaptureDriver()
        with tempfile.TemporaryDirectory() as tmp:
            roles = Path(tmp) / "roles.json"
            roles.write_text(
                """
                {
                  "governance": {"trigger_on_option_split": false},
                  "cabinet": {"approval_threshold": 2, "quorum": 2, "veto_roles": []},
                  "roles": [
                    {"id":"product","name":"Product","role":"product","driver":"memory"},
                    {"id":"dev","name":"Developer","role":"developer","driver":"memory"}
                  ]
                }
                """,
                encoding="utf-8",
            )
            with Runtime(":memory:", roles_path=roles, drivers={"memory": driver}) as rt:
                task_id = rt.create_task("Tiny implementation task")
                result = rt.assign(
                    task_id,
                    "dev",
                    objective="Implement the smallest useful path",
                    acceptance="dev output is enough",
                )
                task = rt.task(task_id)
                self.assertEqual(result["plan"]["flow"], ["human", "worker", "review"])
                self.assertEqual(len(task.assignments), 1)
                self.assertEqual(task.assignments[0]["owner"], "dev")

                rt.collaborate(task_id)

        self.assertNotIn("your_assignment=", driver.prompts[0])
        self.assertIn("your_assignment=Implement the smallest useful path", driver.prompts[1])
        self.assertIn("assigned_work=dev:", driver.prompts[0])

    def test_reassignment_keeps_prompt_and_web_distribution_compact(self):
        driver = PromptCaptureDriver()
        with tempfile.TemporaryDirectory() as tmp:
            roles = Path(tmp) / "roles.json"
            roles.write_text(
                """
                {
                  "governance": {"trigger_on_option_split": false},
                  "cabinet": {"approval_threshold": 1, "quorum": 1, "veto_roles": []},
                  "roles": [
                    {"id":"dev","name":"Developer","role":"developer","driver":"memory"}
                  ]
                }
                """,
                encoding="utf-8",
            )
            with Runtime(":memory:", roles_path=roles, drivers={"memory": driver}) as rt:
                task_id = rt.create_task("Reassign compactly")
                rt.assign(task_id, "dev", objective="Old objective")
                rt.assign(task_id, "dev", objective="New objective")
                task = rt.task(task_id)
                html = render_task(task, rt.log.read(task_id=task_id))
                rt.collaborate(task_id)

        self.assertEqual(len(task.assignments), 2)
        self.assertIn("1 active / 2 total", html)
        self.assertIn("New objective", html)
        self.assertNotIn("Old objective", driver.prompts[0])
        self.assertIn("your_assignment=New objective", driver.prompts[0])

    def test_collaboration_round_skips_governance_without_disagreement(self):
        driver = PromptCaptureDriver()
        with tempfile.TemporaryDirectory() as tmp:
            roles = Path(tmp) / "roles.json"
            roles.write_text(
                """
                {
                  "cabinet": {"approval_threshold": 1, "quorum": 1, "veto_roles": []},
                  "roles": [
                    {"id":"memory","name":"Memory","role":"memory","driver":"memory"}
                  ]
                }
                """,
                encoding="utf-8",
            )
            with Runtime(":memory:", roles_path=roles, drivers={"memory": driver}) as rt:
                task_id = rt.create_task("Collaborate without governance")
                result = rt.collaborate(task_id)
                task = rt.task(task_id)
                events = rt.log.read(task_id=task_id)
                event_types = [event.type for event in events]
                ladder = decision_ladder(events)
                html = render_task(task, events)

                self.assertEqual(task.status, "running")
                self.assertIsNone(task.cabinet)
                self.assertNotIn("cabinet.voted", event_types)
                self.assertEqual(result["brief"]["governance"]["status"], "not_needed")
                self.assertEqual(ladder[1]["status"], "skipped")
                self.assertIn("Governance", html)
                self.assertIn("not_needed", html)

                rt.battle(task_id)
                task = rt.task(task_id)
                self.assertEqual(task.cabinet["status"], "recommended")
                self.assertEqual(task.status, "decided")

    def test_governance_policy_can_ignore_option_split(self):
        drivers = {
            "alpha": StaticResponseDriver(
                '{"stance":"assert","option_id":"A","claim":"Take path A","evidence":[],"confidence":0.9,"text":"A"}'
            ),
            "beta": StaticResponseDriver(
                '{"stance":"assert","option_id":"B","claim":"Take path B","evidence":[],"confidence":0.9,"text":"B"}'
            ),
        }
        with tempfile.TemporaryDirectory() as tmp:
            roles = Path(tmp) / "roles.json"
            roles.write_text(
                """
                {
                  "governance": {
                    "trigger_on_challenge": true,
                    "trigger_on_option_split": false,
                    "low_confidence_threshold": 0.5
                  },
                  "cabinet": {"approval_threshold": 2, "quorum": 2, "veto_roles": []},
                  "roles": [
                    {"id":"alpha","name":"Alpha","role":"planner","driver":"alpha"},
                    {"id":"beta","name":"Beta","role":"builder","driver":"beta"}
                  ]
                }
                """,
                encoding="utf-8",
            )
            with Runtime(":memory:", roles_path=roles, drivers=drivers) as rt:
                task_id = rt.create_task("Allow divergent collaboration")
                result = rt.collaborate(task_id)
                task = rt.task(task_id)

                self.assertEqual(task.status, "running")
                self.assertIsNone(task.cabinet)
                self.assertEqual(len(result["brief"]["options"]), 2)
                self.assertEqual(result["brief"]["governance"]["status"], "not_needed")

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
            self.assertEqual(result["brief"]["governance"]["status"], "forced")

    def test_collaboration_records_governance_trigger_reason(self):
        with Runtime(":memory:") as rt:
            task_id = rt.create_task("Trigger governance")
            result = rt.collaborate(task_id)
            governance = result["brief"]["governance"]

            self.assertEqual(governance["status"], "triggered")
            self.assertIn("challenge:test", governance["signals"])
            self.assertTrue(any(signal.startswith("option_split:") for signal in governance["signals"]))

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

    def test_runtime_reports_invalid_role_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            roles = Path(tmp) / "roles.json"
            roles.write_text('{"roles":[{"name":"Missing id"}]}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "non-empty string id"):
                Runtime(":memory:", roles_path=roles)

            roles.write_text('{"roles":[{"id":"  "}]}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "non-empty string id"):
                Runtime(":memory:", roles_path=roles)

            roles.write_text('{"roles":[{"id":"dev"},{"id":"dev"}]}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate role id: dev"):
                Runtime(":memory:", roles_path=roles)

            roles.write_text('{"roles":[{"id":"dev","driver":"  "}]}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "non-empty string driver"):
                Runtime(":memory:", roles_path=roles)

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

    def test_review_can_be_projected_and_recorded(self):
        with Runtime(":memory:") as rt:
            task_id = rt.create_task("Review me")
            rt.collaborate(task_id)
            review = rt.review(task_id)
            recorded = rt.review(task_id, record=True)
            task = rt.task(task_id)
            events = rt.log.read(task_id=task_id)
            event_types = [event.type for event in events]
            review_event = [event for event in events if event.type == "review.generated"][-1]

            self.assertIn("lenses", review)
            self.assertIn("next_steps", review)
            self.assertEqual(review["scope"], task_id)
            self.assertIn("verify contracts and failure modes", " ".join(item["action"] for item in review["lenses"]))
            self.assertEqual(recorded["scope"], task_id)
            self.assertIn("review.generated", event_types)
            self.assertEqual(task.reviews[-1]["scope"], task_id)
            self.assertEqual(review_event.payload["review"]["next_steps"], recorded["next_steps"])

    def test_review_next_actions_are_usable_for_empty_workspace(self):
        with Runtime(":memory:") as rt:
            review = rt.review()

        self.assertEqual(review["next_actions"], ["Run cater demo to create the first replayable task."])
        self.assertEqual(review["next_steps"][0]["source"], "product")
        self.assertEqual(review["next_steps"][0]["priority"], "high")
        self.assertEqual(review["summary"], "No tasks yet; create a small task and run one collaboration round.")

    def test_review_flags_distribution_weight_and_gaps(self):
        with Runtime(":memory:") as rt:
            task_id = rt.create_task("Review distribution")
            rt.collaborate(task_id)
            review = rt.review(task_id)
            findings = [item["finding"] for item in review["lenses"]]
            self.assertTrue(any("no visible assignment path" in finding for finding in findings))

        with Runtime(":memory:") as rt:
            direct_id = rt.create_task("Direct path")
            rt.assign(direct_id, "dev")
            full_id = rt.create_task("Full path")
            rt.dispatch(full_id)
            review = rt.review()
            findings = [item["finding"] for item in review["lenses"]]
            self.assertTrue(any("direct worker assignment and cabinet dispatch" in finding for finding in findings))

        with Runtime(":memory:") as rt:
            full_id = rt.create_task("Full path with dependencies")
            rt.dispatch(full_id)
            review = rt.review()
            findings = [item["finding"] for item in review["lenses"]]
            self.assertTrue(any("visible dependency structure" in finding for finding in findings))

    def test_review_flags_worker_exchange_quality(self):
        with Runtime(":memory:") as rt:
            task_id = rt.create_task("Flat exchange")
            rt.collaborate(task_id)
            review = rt.review(task_id)
            findings = [item["finding"] for item in review["lenses"]]
            self.assertTrue(any("no explicit worker-to-worker reply" in finding for finding in findings))

        driver = ReplyDriver()
        with tempfile.TemporaryDirectory() as tmp:
            roles = Path(tmp) / "roles.json"
            roles.write_text(
                """
                {
                  "governance": {"trigger_on_option_split": false},
                  "cabinet": {"approval_threshold": 2, "quorum": 2, "veto_roles": []},
                  "roles": [
                    {"id":"alpha","name":"Alpha","role":"planner","driver":"reply"},
                    {"id":"beta","name":"Beta","role":"builder","driver":"reply"}
                  ]
                }
                """,
                encoding="utf-8",
            )
            with Runtime(":memory:", roles_path=roles, drivers={"reply": driver}) as rt:
                task_id = rt.create_task("Linked exchange")
                rt.collaborate(task_id)
                review = rt.review(task_id)
                findings = [item["finding"] for item in review["lenses"]]

        self.assertTrue(any("uses explicit reply metadata" in finding for finding in findings))


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

    def test_scripted_driver_config_rejects_string_script(self):
        with tempfile.TemporaryDirectory() as tmp:
            drivers = Path(tmp) / "drivers.json"
            drivers.write_text(
                '{"drivers":[{"kind":"scripted","name":"bad","script":"not a list"}]}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "string array script"):
                load_drivers(drivers)

    def test_scripted_driver_config_rejects_negative_tokens(self):
        with tempfile.TemporaryDirectory() as tmp:
            drivers = Path(tmp) / "drivers.json"
            drivers.write_text(
                '{"drivers":[{"kind":"scripted","name":"bad","tokens_per_call":-1}]}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "tokens_per_call must be non-negative"):
                load_drivers(drivers)

    def test_driver_config_rejects_non_object_and_duplicate_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            drivers = Path(tmp) / "drivers.json"
            drivers.write_text('{"drivers":["bad"]}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "each driver config must be an object"):
                load_drivers(drivers)

            drivers.write_text(
                """
                {
                  "drivers": [
                    {"kind":"scripted","name":"same","script":[]},
                    {"kind":"scripted","name":"same","script":[]}
                  ]
                }
                """,
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "duplicate driver name: same"):
                load_drivers(drivers)

    def test_cli_driver_config_rejects_invalid_recovery_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            drivers = Path(tmp) / "drivers.json"
            drivers.write_text(
                '{"drivers":[{"kind":"cli","name":"bad","command":["python"],"timeout":0}]}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "timeout must be positive"):
                load_drivers(drivers)

            drivers.write_text(
                '{"drivers":[{"kind":"cli","name":"bad","command":["python"],"restart_limit":-1}]}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "restart_limit must be non-negative"):
                load_drivers(drivers)

    def test_example_tool_driver_speaks_ndjson(self):
        script = Path(__file__).resolve().parents[1] / "examples" / "ndjson_tool_driver.py"
        driver = CliDriver("tool", [sys.executable, str(script)], timeout=5)
        self.addCleanup(driver.close)

        self.assertTrue(driver.probe())
        response = driver.complete(
            role="adapter",
            prompt="Use an external tool",
            context={"goal": "prove the adapter works"},
        )

        self.assertIn("external tool", response.text)
        self.assertGreater(response.tokens, 0)

    def test_example_api_client_drives_web_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "api-client.db")
            base = start_test_web(self, db)

            script = Path(__file__).resolve().parents[1] / "examples" / "api_client.py"
            result = subprocess.run(
                [sys.executable, str(script), "--base-url", base, "--goal", "API example task"],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("task=task_", result.stdout)
            self.assertIn("messages=3", result.stdout)
            self.assertIn("chain_ok=True", result.stdout)


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
        self.assertIn("mission: dispatched", out)
        self.assertIn("brief:", out)
        self.assertIn("recent messages:", out)
        self.assertIn("next:", out)
        self.assertIn("cater show", out)
        self.assertIn("cater review", out)
        self.assertIn("cater chain --json", out)
        self.assertIn("/api/tasks/", out)
        code, out = self.run_cli("chain")
        self.assertEqual(code, 0)
        self.assertIn("hash chain ok", out)

    def test_cli_direct_demo_shows_lightweight_path(self):
        code, out = self.run_cli("demo", "--direct")
        self.assertEqual(code, 0)
        self.assertIn("mode: direct worker collaboration", out)
        self.assertIn("assigned: dev", out)
        self.assertIn("governance: not_needed", out)
        self.assertIn("cater review", out)

    def test_cli_direct_demo_uses_deterministic_default_roles(self):
        roles = Path(self.tmp.name) / "roles.json"
        roles.write_text(
            """
            {
              "roles": [
                {"id":"solo","name":"Solo","role":"solo","driver":"solo"}
              ]
            }
            """,
            encoding="utf-8",
        )
        code = main(["--db", self.db, "--roles", str(roles), "demo", "--direct"])

        self.assertEqual(code, 0)
        self.assertIn("assigned: dev", self.out.getvalue())

    def test_cli_version(self):
        with self.assertRaises(SystemExit) as raised:
            main(["--version"])
        self.assertEqual(raised.exception.code, 0)
        self.assertIn("cater 0.1.0", self.out.getvalue())

    def test_cli_chain_reports_signed_events(self):
        code = main(["--db", self.db, "--audit-key", "secret", "demo"])
        self.assertEqual(code, 0)
        self.out.seek(0)
        self.out.truncate(0)
        code = main(["--db", self.db, "--audit-key", "secret", "chain"])
        self.assertEqual(code, 0)
        self.assertIn("signed", self.out.getvalue())

    def test_cli_chain_json(self):
        code, _ = self.run_cli("demo")
        self.assertEqual(code, 0)

        code, out = self.run_cli("chain", "--json")
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertTrue(report["ok"])
        self.assertGreater(report["checked"], 0)
        self.assertIn("head_hash", report)

    def test_cli_new_battle_inbox_decide(self):
        code, out = self.run_cli("new", "Build demo", "--accept", "three roles", "--dispatch", "--run")
        self.assertEqual(code, 0)
        task_id = next(line for line in out.splitlines() if line.startswith("created ")).split()[1]
        self.assertIn("mission dispatched", out)
        self.assertIn("round complete", out)

        code, out = self.run_cli("show", task_id)
        self.assertEqual(code, 0)
        self.assertIn("cabinet=vetoed", out)
        self.assertIn("assignments=3", out)
        self.assertIn("ladder:", out)
        self.assertIn("Cabinet: vetoed", out)

        code, out = self.run_cli("inbox")
        self.assertEqual(code, 0)
        self.assertIn("inbox empty", out)

        code, out = self.run_cli("run", task_id)
        self.assertEqual(code, 0)
        self.assertIn("round complete", out)

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

    def test_cli_new_rejects_run_and_battle_together(self):
        code, out = self.run_cli("new", "Ambiguous mode", "--run", "--battle")
        self.assertEqual(code, 1)
        self.assertIn("choose either --run", out)

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

    def test_cli_policy(self):
        code, out = self.run_cli("policy")
        self.assertEqual(code, 0)
        self.assertIn("governance:", out)
        self.assertIn("trigger_on_challenge=True", out)
        self.assertIn("cabinet:", out)
        self.assertIn("veto_roles=test", out)
        self.assertIn("reliability:", out)
        self.assertIn("driver_attempts=2", out)

        code = main(
            [
                "--db",
                self.db,
                "--driver-attempts",
                "3",
                "--circuit-breaker-threshold",
                "4",
                "policy",
            ]
        )
        self.assertEqual(code, 0)
        custom_out = self.out.getvalue()
        self.assertIn("driver_attempts=3", custom_out)
        self.assertIn("circuit_breaker_threshold=4", custom_out)

    def test_cli_path_command(self):
        code, out = self.run_cli("new", "Route this task", "--assign-to", "dev", "--run")
        self.assertEqual(code, 0)
        task_id = next(line for line in out.splitlines() if line.startswith("created ")).split()[1]

        code, out = self.run_cli("path", task_id)
        self.assertEqual(code, 0)
        self.assertIn("Distribution", out)
        self.assertIn("direct", out)
        self.assertIn("Collaboration", out)

        code, out = self.run_cli("path", task_id, "--json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data[0]["id"], "distribution")
        self.assertEqual(data[0]["status"], "direct")

    def test_cli_doctor(self):
        code, out = self.run_cli("doctor")
        self.assertEqual(code, 0)
        self.assertIn("cater=0.1.0", out)
        self.assertIn("chain=hash chain ok", out)
        self.assertIn("role product: driver=product configured", out)

        code, out = self.run_cli("doctor", "--json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertTrue(data["ok"])
        self.assertEqual(data["roles"], 3)
        self.assertEqual(data["chain"]["summary"], "hash chain ok, checked 0 event(s)")
        self.assertEqual(data["role_checks"][0]["status"], "configured")

    def test_cli_reports_config_errors_without_traceback(self):
        roles = Path(self.tmp.name) / "bad-roles.json"
        roles.write_text('{"roles":[{"name":"Missing id"}]}', encoding="utf-8")
        code = main(["--db", self.db, "--roles", str(roles), "doctor"])
        out = self.out.getvalue()

        self.assertEqual(code, 1)
        self.assertIn("error: each role requires a non-empty string id", out)
        self.assertNotIn("Traceback", out)

    def test_cli_doctor_can_probe_cli_drivers(self):
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
        code = main(["--db", self.db, "--drivers", str(config), "doctor", "--probe"])
        self.assertEqual(code, 0)
        self.assertIn("role product: driver=product configured, alive", self.out.getvalue())

        self.out.seek(0)
        self.out.truncate(0)
        code = main(["--db", self.db, "--drivers", str(config), "doctor", "--probe", "--json"])
        self.assertEqual(code, 0)
        data = json.loads(self.out.getvalue())
        product = next(item for item in data["role_checks"] if item["id"] == "product")
        self.assertTrue(product["probe"])
        self.assertEqual(product["driver_type"], "CliDriver")

    def test_cli_dispatch_command(self):
        code, out = self.run_cli("new", "Dispatch only")
        self.assertEqual(code, 0)
        task_id = next(line for line in out.splitlines() if line.startswith("created ")).split()[1]

        code, out = self.run_cli("dispatch", task_id)
        self.assertEqual(code, 0)
        self.assertIn("mission dispatched", out)
        self.assertIn('"subtasks"', out)

        code, out = self.run_cli("show", task_id)
        self.assertEqual(code, 0)
        self.assertIn("assignments=3", out)

    def test_cli_direct_assign_command(self):
        code, out = self.run_cli("new", "Assign only")
        self.assertEqual(code, 0)
        task_id = next(line for line in out.splitlines() if line.startswith("created ")).split()[1]

        code, out = self.run_cli("assign", task_id, "dev", "--objective", "Implement it")
        self.assertEqual(code, 0)
        self.assertIn("assigned", out)
        self.assertIn('"owner": "dev"', out)

        code, out = self.run_cli("show", task_id)
        self.assertEqual(code, 0)
        self.assertIn("assignments=1", out)

    def test_cli_direct_assign_reports_unknown_role_without_creating_task(self):
        code, out = self.run_cli("new", "Bad assign", "--assign-to", "nobody")
        self.assertEqual(code, 1)
        self.assertIn("unknown role 'nobody'", out)

        code, out = self.run_cli("show")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "")

        code, out = self.run_cli("new", "Known task")
        self.assertEqual(code, 0)
        task_id = next(line for line in out.splitlines() if line.startswith("created ")).split()[1]
        code, out = self.run_cli("assign", task_id, "nobody")
        self.assertEqual(code, 1)
        self.assertIn("expected one of: product, dev, test", out)

    def test_cli_show_includes_recent_messages(self):
        code, out = self.run_cli("new", "Show message links", "--run")
        self.assertEqual(code, 0)
        task_id = next(line for line in out.splitlines() if line.startswith("created ")).split()[1]

        code, out = self.run_cli("show", task_id)
        self.assertEqual(code, 0)
        self.assertIn("recent messages:", out)
        self.assertIn("product/assert", out)

    def test_cli_events_include_worker_reply_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            roles = Path(tmp) / "roles.json"
            drivers = Path(tmp) / "drivers.json"
            roles.write_text(
                """
                {
                  "governance": {"trigger_on_option_split": false},
                  "cabinet": {"approval_threshold": 2, "quorum": 2, "veto_roles": []},
                  "roles": [
                    {"id":"alpha","name":"Alpha","role":"planner","driver":"reply"},
                    {"id":"beta","name":"Beta","role":"builder","driver":"reply"}
                  ]
                }
                """,
                encoding="utf-8",
            )
            drivers.write_text(
                json.dumps(
                    {
                        "drivers": [
                            {
                                "kind": "scripted",
                                "name": "reply",
                                "script": [
                                    json.dumps(
                                        {
                                            "stance": "assert",
                                            "option_id": "shared-plan",
                                            "claim": "Draft the plan",
                                            "evidence": [],
                                            "confidence": 0.8,
                                            "text": "planner output",
                                        }
                                    ),
                                    json.dumps(
                                        {
                                            "stance": "assert",
                                            "option_id": "shared-plan",
                                            "claim": "Continue the plan",
                                            "evidence": [],
                                            "confidence": 0.8,
                                            "text": "builder output",
                                            "target": "alpha",
                                            "reply_to": 3,
                                        }
                                    ),
                                ],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with Runtime(self.db, roles_path=roles, drivers_path=drivers) as rt:
                task_id = rt.create_task("CLI event links")
                rt.collaborate(task_id)

            self.out.seek(0)
            self.out.truncate(0)
            code = main(["--db", self.db, "--roles", str(roles), "--drivers", str(drivers), "events", task_id])
            out = self.out.getvalue()

        self.assertEqual(code, 0)
        self.assertIn("[target alpha | reply_to #3]", out)

    def test_cli_export_json_and_jsonl(self):
        code, _ = self.run_cli("demo")
        self.assertEqual(code, 0)

        code, out = self.run_cli("export")
        self.assertEqual(code, 0)
        exported = json.loads(out)
        self.assertTrue(exported["chain"]["ok"])
        self.assertEqual(len(exported["tasks"]), 1)
        self.assertGreater(len(exported["events"]), 1)
        self.assertIn("product", exported["profiles"])

        task_id = exported["tasks"][0]["id"]
        code, out = self.run_cli("export", task_id, "--format", "jsonl")
        self.assertEqual(code, 0)
        lines = [json.loads(line) for line in out.splitlines()]
        self.assertGreater(len(lines), 1)
        self.assertTrue(all(item["task_id"] == task_id for item in lines))

    def test_cli_review_and_record(self):
        code, _ = self.run_cli("demo")
        self.assertEqual(code, 0)

        code, out = self.run_cli("review")
        self.assertEqual(code, 0)
        review = json.loads(out)
        self.assertIn("lenses", review)
        self.assertIn("next_actions", review)

        code, out = self.run_cli("review", "--record")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["scope"], "workspace")

        task_id = self.run_cli("show")[1].split()[0]
        code, out = self.run_cli("review", task_id, "--record")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["scope"], task_id)
        code, out = self.run_cli("events", task_id)
        self.assertEqual(code, 0)
        self.assertIn("review.generated", out)
        code, out = self.run_cli("export")
        self.assertEqual(code, 0)
        exported = json.loads(out)
        workspace_reviews = [
            event for event in exported["events"]
            if event["type"] == "review.generated" and event["task_id"] is None
        ]
        self.assertEqual(workspace_reviews[-1]["payload"]["review"]["scope"], "workspace")

    def test_cli_review_plain_output(self):
        code, _ = self.run_cli("demo")
        self.assertEqual(code, 0)

        code, out = self.run_cli("review", "--plain")
        self.assertEqual(code, 0)
        self.assertIn("health:", out)
        self.assertIn("lenses:", out)
        self.assertIn("next actions:", out)
        self.assertIn("[medium/", out)
        self.assertNotIn('"lenses"', out)

    def test_cli_review_next_output(self):
        code, out = self.run_cli("review", "--next")
        self.assertEqual(code, 0)
        self.assertIn("high/Product: Run cater demo", out)

        code, _ = self.run_cli("demo")
        self.assertEqual(code, 0)
        code, out = self.run_cli("review", "--next")
        self.assertEqual(code, 0)
        self.assertIn(":", out)
        self.assertNotIn('"next_steps"', out)


class WebTests(unittest.TestCase):
    def test_render_index_exposes_local_api_panel(self):
        with Runtime(":memory:") as rt:
            html = render_index(rt)

        self.assertIn("Local API", html)
        self.assertIn("GET /api/tasks", html)
        self.assertIn("POST /api/tasks", html)
        self.assertIn("GET /api/chain", html)
        self.assertIn("python examples/api_client.py", html)
        self.assertIn("Run cater demo to create the first replayable task.", html)
        self.assertIn(">high<", html)

    def test_render_task_shows_worker_reply_links_in_messages_and_timeline(self):
        driver = ReplyDriver()
        with tempfile.TemporaryDirectory() as tmp:
            roles = Path(tmp) / "roles.json"
            roles.write_text(
                """
                {
                  "governance": {"trigger_on_option_split": false},
                  "cabinet": {"approval_threshold": 2, "quorum": 2, "veto_roles": []},
                  "roles": [
                    {"id":"alpha","name":"Alpha","role":"planner","driver":"reply"},
                    {"id":"beta","name":"Beta","role":"builder","driver":"reply"}
                  ]
                }
                """,
                encoding="utf-8",
            )
            with Runtime(":memory:", roles_path=roles, drivers={"reply": driver}) as rt:
                task_id = rt.create_task("Render reply links")
                rt.collaborate(task_id)
                html = render_task(rt.task(task_id), rt.log.read(task_id=task_id))

        self.assertIn("target alpha | reply_to #3", html)

    def test_render_task_includes_cabinet_timeline_and_decision_form(self):
        with Runtime(":memory:") as rt:
            task_id = rt.create_task("Render me")
            rt.battle(task_id)
            events = rt.log.read(task_id=task_id)
            html = render_task(rt.task(task_id), events)
            ladder = decision_ladder(events)
            fresh_task_id = rt.create_task("Render controls")
            controls_html = render_task(rt.task(fresh_task_id), rt.log.read(task_id=fresh_task_id))

        self.assertIn("Cabinet", html)
        self.assertIn("Organization Path", html)
        self.assertIn("Decision Ladder", html)
        self.assertIn("Timeline", html)
        self.assertIn("Human Command", html)
        self.assertIn("Self Review", html)
        self.assertIn("next-actions", html)
        self.assertIn("Force governance", controls_html)
        self.assertIn("governance round forced", html)
        self.assertIn("cabinet.voted", html)
        self.assertIn("agent.decided", html)
        self.assertEqual([step["id"] for step in ladder], ["battle", "cabinet", "supreme", "human"])
        self.assertEqual(ladder[1]["status"], "vetoed")

    def test_render_task_shows_current_acceptance(self):
        with Runtime(":memory:") as rt:
            task_id = rt.create_task("Render acceptance", ["ship one path"])
            rt.command(task_id, "Update acceptance", acceptance=["prove replay"])
            html = render_task(rt.task(task_id), rt.log.read(task_id=task_id))

        self.assertIn("Acceptance", html)
        self.assertIn("prove replay", html)
        self.assertNotIn("ship one path</li>", html)

    def test_web_api_and_decision_post(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "web.db")
            with Runtime(db) as rt:
                task_id = rt.create_task("Serve me")
                rt.battle(task_id)

            base = start_test_web(self, db)

            with urlopen(f"{base}/api/tasks/{task_id}", timeout=5) as response:
                data = response.read().decode("utf-8")
            self.assertIn('"cabinet"', data)
            self.assertIn("cabinet.voted", data)
            self.assertIn('"decision_ladder"', data)
            self.assertIn('"organization_path"', data)
            self.assertIn('"review"', data)

            with urlopen(f"{base}/tasks/{task_id}", timeout=5) as response:
                page = response.read().decode("utf-8")
            self.assertIn('<option value="dev">dev / developer</option>', page)

            dispatch_request = Request(
                f"{base}/tasks/{task_id}/dispatch",
                data=b"",
                method="POST",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            with urlopen(dispatch_request, timeout=5) as response:
                page = response.read().decode("utf-8")
            self.assertIn("Task Distribution", page)
            self.assertIn("assigned", page)

            with urlopen(f"{base}/api/tasks/{task_id}", timeout=5) as response:
                data = response.read().decode("utf-8")
            self.assertIn('"assignments"', data)
            self.assertIn('"mission"', data)

            assign_body = urlencode({"owner": "dev", "objective": "Direct web work"}).encode("utf-8")
            assign_request = Request(
                f"{base}/tasks/{task_id}/assign",
                data=assign_body,
                method="POST",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            with urlopen(assign_request, timeout=5) as response:
                page = response.read().decode("utf-8")
            self.assertIn("Direct web work", page)

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

    def test_web_api_chain_reports_audit_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "web-chain.db")
            with Runtime(db) as rt:
                rt.create_task("Expose chain status")

            base = start_test_web(self, db)

            with urlopen(f"{base}/api/chain", timeout=5) as response:
                data = json.loads(response.read().decode("utf-8"))

            self.assertTrue(data["ok"])
            self.assertGreaterEqual(data["checked"], 1)
            self.assertIn("hash chain ok", data["summary"])
            self.assertIn("head_hash", data)

    def test_web_api_can_create_and_run_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "web-api-create.db")
            base = start_test_web(self, db)

            body = json.dumps(
                {
                    "goal": "Create through API",
                    "acceptance": ["task is replayable"],
                    "dispatch": True,
                    "run": True,
                }
            ).encode("utf-8")
            request = Request(
                f"{base}/api/tasks",
                data=body,
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urlopen(request, timeout=5) as response:
                self.assertEqual(response.status, 201)
                data = json.loads(response.read().decode("utf-8"))

            self.assertEqual(data["task"]["goal"], "Create through API")
            self.assertEqual(data["task"]["acceptance"], ["task is replayable"])
            self.assertTrue(data["task"]["mission"])
            self.assertTrue(data["task"]["messages"])
            self.assertIn("review", data)
            self.assertIn("decision_ladder", data)
            self.assertEqual(data["organization_path"][0]["status"], "cabinet_dispatch")

    def test_web_api_rejects_run_and_battle_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "web-api-conflict.db")
            base = start_test_web(self, db)

            body = json.dumps({"goal": "Bad API task", "run": True, "battle": True}).encode("utf-8")
            request = Request(
                f"{base}/api/tasks",
                data=body,
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with self.assertRaises(HTTPError) as raised:
                urlopen(request, timeout=5)

            self.assertEqual(raised.exception.code, 400)
            self.assertIn("choose either run", raised.exception.read().decode("utf-8"))

    def test_web_api_rejects_bad_json_without_creating_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "web-api-bad-json.db")
            base = start_test_web(self, db)

            request = Request(
                f"{base}/api/tasks",
                data=b"{",
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with self.assertRaises(HTTPError) as raised:
                urlopen(request, timeout=5)

            self.assertEqual(raised.exception.code, 400)
            self.assertIn("invalid JSON body", raised.exception.read().decode("utf-8"))
            with Runtime(db) as rt:
                self.assertEqual(rt.tasks(), [])

    def test_web_api_rejects_unknown_assignee_without_creating_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "web-api-bad-assignee.db")
            base = start_test_web(self, db)

            body = json.dumps({"goal": "Bad assignee", "assign_to": "ghost"}).encode("utf-8")
            request = Request(
                f"{base}/api/tasks",
                data=body,
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with self.assertRaises(HTTPError) as raised:
                urlopen(request, timeout=5)

            self.assertEqual(raised.exception.code, 400)
            self.assertIn("unknown role 'ghost'", raised.exception.read().decode("utf-8"))
            with Runtime(db) as rt:
                self.assertEqual(rt.tasks(), [])

    def test_web_api_task_actions_share_runtime_semantics(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "web-api-actions.db")
            with Runtime(db) as rt:
                task_id = rt.create_task("Drive API actions")

            base = start_test_web(self, db)

            assign = Request(
                f"{base}/api/tasks/{task_id}/assign",
                data=json.dumps({"owner": "dev", "objective": "Use API route"}).encode("utf-8"),
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urlopen(assign, timeout=5) as response:
                data = json.loads(response.read().decode("utf-8"))
            self.assertEqual(data["task"]["assignments"][-1]["owner"], "dev")
            self.assertEqual(data["organization_path"][0]["status"], "direct")

            command = Request(
                f"{base}/api/tasks/{task_id}/command",
                data=json.dumps({"instruction": "Narrow goal", "goal": "Drive API actions safely"}).encode("utf-8"),
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urlopen(command, timeout=5) as response:
                data = json.loads(response.read().decode("utf-8"))
            self.assertEqual(data["task"]["goal"], "Drive API actions safely")

            decide = Request(
                f"{base}/api/tasks/{task_id}/decide",
                data=json.dumps({"resolution": "done", "reason": "API accepted"}).encode("utf-8"),
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urlopen(decide, timeout=5) as response:
                data = json.loads(response.read().decode("utf-8"))
            self.assertEqual(data["task"]["status"], "done")
            self.assertEqual(data["task"]["decision"]["reason"], "API accepted")

    def test_web_direct_assign_reports_unknown_role(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "web-bad-assign.db")
            with Runtime(db) as rt:
                task_id = rt.create_task("Serve bad assign")

            base = start_test_web(self, db)

            body = urlencode({"owner": "ghost"}).encode("utf-8")
            request = Request(
                f"{base}/tasks/{task_id}/assign",
                data=body,
                method="POST",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            with self.assertRaises(HTTPError) as raised:
                urlopen(request, timeout=5)

            self.assertEqual(raised.exception.code, 400)
            self.assertIn("unknown role 'ghost'", raised.exception.read().decode("utf-8"))

    def test_web_decide_reports_invalid_resolution(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "web-bad-decision.db")
            with Runtime(db) as rt:
                task_id = rt.create_task("Serve bad decision")

            base = start_test_web(self, db)

            body = urlencode({"resolution": "maybe", "reason": "invalid"}).encode("utf-8")
            request = Request(
                f"{base}/tasks/{task_id}/decide",
                data=body,
                method="POST",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            with self.assertRaises(HTTPError) as raised:
                urlopen(request, timeout=5)

            self.assertEqual(raised.exception.code, 400)
            self.assertIn("resolution must be done or rework", raised.exception.read().decode("utf-8"))

    def test_web_can_create_task_and_run_battle(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "web-create.db")
            base = start_test_web(self, db)

            body = urlencode(
                {
                    "goal": "Create from web",
                    "acceptance": "battle runs\ninbox updates",
                    "dispatch": "on",
                    "run_now": "on",
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
                self.assertEqual(len(tasks[0].assignments), 3)

            with urlopen(f"{base}/", timeout=5) as response:
                index = response.read().decode("utf-8")
            self.assertIn("Agent Profiles", index)
            self.assertIn("Organization Policy", index)
            self.assertIn("Reliability", index)
            self.assertIn("Self Review", index)
            self.assertIn("Create from web", index)
            self.assertIn('name="run_now"', index)
            self.assertNotIn('name="battle" checked', index)


if __name__ == "__main__":
    unittest.main()

