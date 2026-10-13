from __future__ import annotations

import io
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import unittest
from unittest.mock import patch
from pathlib import Path
from http.server import ThreadingHTTPServer
from urllib.parse import urlencode
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from cater.cabinet import majority_threshold, tally_votes
from cater import __version__, web as web_module
from cater import cli as cli_module
from cater.cli import main
from cater.drivers import CliDriver, DriverProtocolError, DriverResponse, DriverUnavailable, OpenAICompatibleDriver, load_drivers
from cater.ladder import decision_ladder
from cater.messages import message_relation
from cater.profiles import agent_profiles
from cater.runtime import DriverHealth, Runtime
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


class DeliverableCaptureDriver:
    name = "deliverable-memory"

    def __init__(self):
        self.prompts: list[str] = []

    def complete(self, *, role: str, prompt: str, context: dict) -> DriverResponse:
        self.prompts.append(prompt)
        return DriverResponse(
            text=json.dumps(
                {
                    "stance": "assert",
                    "option_id": "A",
                    "claim": "发布可复用成果",
                    "evidence": ["已形成可回放交付物"],
                    "text": "我已留下可供下次任务参考的成果。",
                    "deliverables": [{"title": "组织记忆样例", "summary": "这是一个已发布成果。"}],
                },
                ensure_ascii=False,
            ),
            tokens=3,
        )


class StaticResponseDriver:
    def __init__(self, text: str, tokens: int = 5, meta: dict | None = None):
        self.text = text
        self.tokens = tokens
        self.meta = meta
        self.calls = 0

    def complete(self, *, role: str, prompt: str, context: dict) -> DriverResponse:
        self.calls += 1
        return DriverResponse(text=self.text, tokens=self.tokens, meta=self.meta)


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
            match = re.search(r"\(#(\d+)\)", prompt)
            text = {
                "stance": "assert",
                "option_id": "shared-plan",
                "claim": "Continue the plan from planner",
                "evidence": ["uses prior event"],
                "confidence": 0.8,
                "text": "builder output",
                "target": "alpha",
                "reply_to": int(match.group(1)) if match else None,
            }
        return DriverResponse(text=json.dumps(text), tokens=4)


class BlockingDriver:
    name = "blocking"

    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()

    def complete(self, *, role: str, prompt: str, context: dict) -> DriverResponse:
        self.started.set()
        if not self.release.wait(timeout=3):
            raise RuntimeError("test driver was not released")
        return DriverResponse(
            text='{"stance":"assert","option_id":"A","claim":"background output","text":"background output"}',
            tokens=1,
        )


def start_test_web(
    testcase: unittest.TestCase,
    db: str,
    *,
    return_server: bool = False,
    **handler_options: object,
) -> str | tuple[str, ThreadingHTTPServer, threading.Thread]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(db, **handler_options))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    testcase.addCleanup(server.server_close)
    testcase.addCleanup(thread.join, 1)
    testcase.addCleanup(server.shutdown)
    base = f"http://127.0.0.1:{server.server_port}"
    return (base, server, thread) if return_server else base


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

            self.assertEqual(task.status, "decided")
            self.assertEqual(task.decision["resolution"], "accepted")
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
            self.assertEqual(len(first.messages), 7)

    def test_task_start_records_a_stable_organization_fingerprint(self):
        with Runtime(":memory:") as rt:
            task_id = rt.create_task("Fingerprint the organization")
            event = next(event for event in rt.log.read(task_id=task_id) if event.type == "task.started")
            default_fingerprint = event.payload["organization_fingerprint"]

        with tempfile.TemporaryDirectory() as tmp:
            roles = Path(tmp) / "roles.json"
            source = Path(__file__).resolve().parents[1] / "cater" / "packs" / "default_roles.json"
            config = json.loads(source.read_text(encoding="utf-8"))
            config["roles"][0]["voice"] = "改过的表达方式"
            roles.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
            with Runtime(":memory:", roles_path=roles) as rt:
                task_id = rt.create_task("Fingerprint the changed organization")
                event = next(event for event in rt.log.read(task_id=task_id) if event.type == "task.started")
                changed_fingerprint = event.payload["organization_fingerprint"]

        self.assertRegex(default_fingerprint, r"^[0-9a-f]{64}$")
        self.assertNotEqual(default_fingerprint, changed_fingerprint)

    def test_agent_profiles_are_projected_from_events(self):
        with Runtime(":memory:") as rt:
            task_id = rt.create_task("Profile agents")
            rt.battle(task_id)
            profiles = rt.profiles()
            from_events = agent_profiles(rt.log.read())

            self.assertEqual(profiles, from_events)
            self.assertEqual(profiles["product"]["supports"], 1)
            self.assertEqual(profiles["test"]["challenges"], 0)
            self.assertEqual(profiles["test"]["supports"], 1)
            self.assertIn("用户价值", profiles["product"]["last_claim"])

    def test_default_demo_drivers_publish_replayable_deliverables(self):
        with Runtime(":memory:") as rt:
            task_id = rt.create_task("验证默认成果")
            rt.collaborate(task_id)
            task = rt.task(task_id)
            events = rt.log.read(task_id=task_id)
            outcomes = [event for event in events if event.type == "deliverable.published"]
            working = [event for event in events if event.type == "agent.working"]

        self.assertEqual(len(outcomes), 4)
        self.assertEqual([event.actor.id for event in working], ["product", "design", "dev", "test"])
        self.assertIn("用户路径", working[0].payload["summary"])
        self.assertEqual(
            [item["title"] for item in task.deliverables],
            ["月亮的任务收束", "罐头的交互草图", "米寇的实现路径", "三角的验证清单"],
        )
        self.assertTrue(all(item["message_seq"] for item in task.deliverables))

    def test_guided_demo_keeps_a_task_focus_and_links_peer_outputs(self):
        with Runtime(":memory:") as rt:
            task_id = rt.create_task("做一个能预约、查看菜单并提交建议的网页")
            rt.collaborate(task_id)
            messages = rt.task(task_id).messages

        self.assertIn("预约、菜单浏览与意见反馈", messages[0]["text"])
        self.assertNotIn("实现线", messages[1]["text"])
        self.assertEqual(messages[1]["target"], "product")
        self.assertEqual(messages[1]["reply_to"], messages[0]["seq"])

    def test_agent_can_publish_structured_deliverables(self):
        response = json.dumps(
            {
                "stance": "assert",
                "option_id": "result",
                "claim": "成果已完成",
                "evidence": ["可回放事件"],
                "confidence": 0.9,
                "text": "我已发布可复用成果。",
                "deliverables": [
                    {
                        "title": "验收报告",
                        "summary": "核心流程已经验证通过。",
                        "kind": "report",
                        "uri": "reports/acceptance.md",
                    },
                    {"title": "", "summary": "缺少标题的项目不应发布。"},
                ],
            },
            ensure_ascii=False,
        )
        with tempfile.TemporaryDirectory() as tmp:
            roles = Path(tmp) / "roles.json"
            roles.write_text(
                '{"roles":[{"id":"worker","name":"执行者","role":"执行","driver":"result"}]}',
                encoding="utf-8",
            )
            with Runtime(
                ":memory:",
                roles_path=roles,
                drivers={"result": StaticResponseDriver(response)},
            ) as rt:
                task_id = rt.create_task("发布成果")
                rt.collaborate(task_id)
                task = rt.task(task_id)
                published = [event for event in rt.log.read(task_id=task_id) if event.type == "deliverable.published"]

        self.assertEqual(len(published), 1)
        self.assertEqual(task.deliverables[0]["title"], "验收报告")
        self.assertEqual(task.deliverables[0]["uri"], "reports/acceptance.md")
        self.assertEqual(task.deliverables[0]["message_seq"], task.messages[0]["seq"])

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

    def test_published_outcomes_become_bounded_shared_memory_for_later_tasks(self):
        driver = DeliverableCaptureDriver()
        with tempfile.TemporaryDirectory() as tmp:
            roles = Path(tmp) / "roles.json"
            roles.write_text(
                '{"roles":[{"id":"memory","name":"Memory","role":"memory","driver":"memory"}]}',
                encoding="utf-8",
            )
            with Runtime(":memory:", roles_path=roles, drivers={"memory": driver}) as rt:
                first = rt.create_task("First task")
                rt.collaborate(first)
                second = rt.create_task("Second task")
                rt.collaborate(second)
                memory = rt.shared_memory(exclude_task_id=second)

        self.assertEqual(len(memory), 1)
        self.assertEqual(memory[0]["title"], "组织记忆样例")
        self.assertIn("Organization memory: #", driver.prompts[1])
        self.assertIn("组织记忆样例", driver.prompts[1])
        self.assertNotIn("First task", driver.prompts[1])

    def test_discipline_flags_unsupported_challenge_and_reply_without_turning_it_into_a_veto(self):
        response = json.dumps(
            {
                "stance": "challenge",
                "option_id": "A",
                "claim": "这里需要额外核验",
                "evidence": [],
                "text": "请先补充可以复查的证据。",
                "target": "missing-role",
                "reply_to": 999,
            },
            ensure_ascii=False,
        )
        with tempfile.TemporaryDirectory() as tmp:
            roles = Path(tmp) / "roles.json"
            roles.write_text(
                '{"roles":[{"id":"worker","name":"执行者","role":"worker","driver":"static"}]}',
                encoding="utf-8",
            )
            with Runtime(":memory:", roles_path=roles, drivers={"static": StaticResponseDriver(response)}) as rt:
                task_id = rt.create_task("纪律检查")
                rt.collaborate(task_id)
                task = rt.task(task_id)
                events = rt.log.read(task_id=task_id)
                review = rt.review(task_id)

        self.assertEqual(task.messages[0]["type"], "msg.said")
        self.assertIsNone(task.messages[0]["option_id"])
        self.assertNotIn("target", task.messages[0])
        self.assertNotIn("reply_to", task.messages[0])
        self.assertIn("challenge_without_evidence", task.messages[0]["discipline"]["issues"])
        self.assertIn("invalid_target", task.discipline_flags[0]["issues"])
        self.assertIn("agent.discipline_flagged", [event.type for event in events])
        self.assertTrue(any(item["id"] == "discipline" and item["status"] == "watch" for item in review["lenses"]))

    def test_reported_token_budget_is_shared_by_roles_using_the_same_driver(self):
        driver = StaticResponseDriver(
            '{"stance":"assert","option_id":"A","claim":"正常输出","evidence":["可核验"],"text":"正常输出"}',
            tokens=5,
        )
        driver.token_budget = 5
        with tempfile.TemporaryDirectory() as tmp:
            roles = Path(tmp) / "roles.json"
            roles.write_text(
                '{"organization":{"leader":{"id":"lead","name":"Lead","role":"lead"},"cabinet_members":["alpha","beta"],"worker_members":["alpha","beta"]},"roles":[{"id":"alpha","name":"Alpha","role":"alpha","driver":"shared"},{"id":"beta","name":"Beta","role":"beta","driver":"shared"}]}',
                encoding="utf-8",
            )
            with Runtime(":memory:", roles_path=roles, drivers={"shared": driver}) as rt:
                task_id = rt.create_task("共享预算")
                rt.collaborate(task_id)
                task = rt.task(task_id)
                events = rt.log.read(task_id=task_id)

        self.assertEqual(driver.calls, 1)
        self.assertEqual(task.tokens, 5)
        self.assertEqual(len(task.budget_exhaustions), 1)
        self.assertEqual(task.budget_exhaustions[0]["role"], "beta")
        self.assertEqual(task.budget_exhaustions[0]["used_tokens"], 5)
        self.assertIn("driver.budget_exhausted", [event.type for event in events])

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
        self.assertIn("recent_customer_directions_oldest_to_newest=#9: Use prior output", driver.prompts[2])
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
        self.assertIn("User-facing response", driver.prompts[0])
        self.assertIn("Voice guidance", driver.prompts[0])
        match = re.search(r"alpha/assert: Draft the plan \(#(\d+)\)", driver.prompts[1])
        self.assertIsNotNone(match)
        prior_seq = int(match.group(1))
        self.assertEqual(task.messages[1]["target"], "alpha")
        self.assertEqual(task.messages[1]["reply_to"], prior_seq)
        self.assertEqual(result["brief"]["positions"][1]["target"], "alpha")
        self.assertEqual(result["brief"]["positions"][1]["reply_to"], prior_seq)
        self.assertIn(f"reply_to #{prior_seq}", html)

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
                self.assertEqual(task.assignments[1]["lane"], "实现")
                self.assertEqual(task.assignments[1]["depends_on"], [task.assignments[0]["id"]])
                self.assertIn("deliverable", task.assignments[0])
                html = render_task(task, rt.log.read(task_id=task_id))
                self.assertIn("可执行的实现方案或可交付代码改动", html)
                self.assertIn("depends on:", html)

                rt.collaborate(task_id)

        self.assertIn("assigned_work=product:", driver.prompts[0])
        self.assertIn("dev:", driver.prompts[0])

    def test_dispatch_dependencies_follow_execution_lanes(self):
        with Runtime(":memory:") as rt:
            task_id = rt.create_task("Plan dependencies")
            rt.dispatch(task_id)
            task = rt.task(task_id)

        product, design, dev, test = task.assignments
        self.assertEqual(product["depends_on"], [])
        self.assertEqual(design["depends_on"], [product["id"]])
        self.assertEqual(dev["depends_on"], [product["id"]])
        self.assertEqual(test["depends_on"], [product["id"], design["id"], dev["id"]])
        self.assertEqual(test["deliverable"], "风险审查、测试策略与发布信心")

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
                task = rt.task(task_id)

                self.assertEqual([message["actor"] for message in task.messages], ["dev"])

        self.assertEqual(len(driver.prompts), 1)
        self.assertIn("your_assignment=Implement the smallest useful path", driver.prompts[0])
        self.assertIn("assigned_work=dev:", driver.prompts[0])

    def test_display_name_mention_can_prefer_a_single_worker(self):
        with Runtime(":memory:") as rt:
            mentions = rt.mentioned_workers("@月亮 帮我整理一个短页面文案")
            design_mentions = rt.mentioned_workers("@罐头 帮我看看这个页面怎么更好用")
            task_id = rt.create_task("@月亮 帮我整理一个短页面文案")
            result = rt.intake(task_id, requested_route="direct", preferred_owner=mentions[0])
            task = rt.task(task_id)

        self.assertEqual(mentions, ["product"])
        self.assertEqual(design_mentions, ["design"])
        self.assertEqual(result["route"]["route"], "direct")
        self.assertEqual(task.assignments[0]["owner"], "product")
        self.assertIn("做到什么算完成", task.interpretation["summary"])

    def test_in_chat_direction_amends_the_next_round_roster_and_strategy(self):
        with Runtime(":memory:") as rt:
            task_id = rt.create_task("整理一个页面说明")
            rt.assign(task_id, "product")

            command = rt.command(task_id, "请 @罐头 加入，并请内阁一起审查交互取舍")
            task = rt.task(task_id)
            plan_events = [event for event in rt.log.read(task_id=task_id) if event.type == "plan.amended"]

            self.assertEqual(command.payload["mentioned_workers"], ["design"])
            self.assertEqual(command.payload["next_action"], "battle")
            self.assertEqual([role.id for role in rt._roles_for_round(task)], ["product", "design"])
            self.assertEqual(task.plan["participants"], ["product", "design"])
            self.assertEqual(task.plan["next_action"], "battle")
            self.assertEqual(plan_events[-1].payload["added"], ["design"])

    def test_in_chat_direction_can_replace_the_next_round_roster(self):
        with Runtime(":memory:") as rt:
            task_id = rt.create_task("做一个网页任务入口")
            rt.intake(task_id)

            rt.command(task_id, "这一步只让 @罐头 先处理界面，其他伙伴先不用参与")
            task = rt.task(task_id)

            self.assertEqual([role.id for role in rt._roles_for_round(task)], ["design"])
            self.assertEqual(task.plan["participants"], ["design"])
            latest = {item["owner"]: item for item in task.assignments}
            self.assertEqual(latest["product"]["status"], "deferred")
            self.assertEqual(latest["design"]["status"], "assigned")

    def test_task_memory_excludes_deferred_work_and_orders_customer_directions(self):
        with Runtime(":memory:") as rt:
            task_id = rt.create_task("做一个网页任务入口")
            rt.intake(task_id)
            rt.command(task_id, "这一步只让 @罐头 先处理界面")
            rt.command(task_id, "目标改为：先把输入框做到可用")
            memory = rt._task_memory(rt.task(task_id), "design")

        self.assertIn("assigned_work=design:", memory)
        self.assertNotIn("product:", memory)
        self.assertIn("recent_customer_directions_oldest_to_newest=", memory)
        self.assertIn("latest explicit direction overrides", memory)

    def test_explicit_goal_rewrite_updates_the_task_and_next_plan(self):
        with Runtime(":memory:") as rt:
            task_id = rt.create_task("先做一个完整网页")
            rt.intake(task_id)

            rt.command(task_id, "需求改为：只完成可用的任务输入框")
            task = rt.task(task_id)

            self.assertEqual(task.goal, "只完成可用的任务输入框")
            self.assertIn("任务输入框", task.interpretation["objective"])
            self.assertEqual(task.plan["source"], "human_command")

    def test_goal_rewrite_keeps_an_adequate_existing_roster_until_replanning_is_explicit(self):
        with Runtime(":memory:") as rt:
            task_id = rt.create_task("做一个网页任务入口")
            rt.intake(task_id)
            before = [role.id for role in rt._roles_for_round(rt.task(task_id))]

            rt.command(task_id, "目标改为：把现有入口文案再简洁一点")
            task = rt.task(task_id)

            self.assertEqual([role.id for role in rt._roles_for_round(task)], before)
            self.assertFalse(task.plan["replanned"])

    def test_intake_records_task_understanding_before_lightweight_collaboration(self):
        with Runtime(":memory:") as rt:
            task_id = rt.create_task("为团队做一个网页任务入口", ["用户能发起并查看任务", "过程可回放"])
            result = rt.intake(task_id)
            task = rt.task(task_id)
            event_types = [event.type for event in rt.log.read(task_id=task_id)]

        self.assertEqual(result["route"]["route"], "collaborate")
        self.assertEqual(task.interpretation["complexity"], "standard")
        self.assertIn("交互路径", task.interpretation["objective"])
        self.assertIn("团队做一个网页任务入口", task.interpretation["objective"])
        self.assertIn("团队做一个网页任务入口", task.interpretation["summary"])
        self.assertEqual(task.route["route"], "collaborate")
        self.assertEqual(task.mission["goal"], task.interpretation["objective"])
        self.assertIn("task.interpreted", event_types)
        self.assertIn("task.routed", event_types)
        self.assertIn("task.decomposed", event_types)
        self.assertEqual(len(task.assignments), 4)

    def test_leader_intake_selects_smallest_useful_worker_set(self):
        with Runtime(":memory:") as rt:
            content_task_id = rt.create_task("帮我整理一页活动说明")
            rt.intake(content_task_id)
            rt.collaborate(content_task_id)
            content_task = rt.task(content_task_id)

            screen_task_id = rt.create_task("做一个网页任务入口", ["能发起任务", "过程可回放"])
            rt.intake(screen_task_id)
            screen_task = rt.task(screen_task_id)

        self.assertEqual([item["owner"] for item in content_task.assignments], ["product"])
        self.assertEqual([message["actor"] for message in content_task.messages], ["product"])
        self.assertEqual(screen_task.plan["participants"], ["product", "design", "dev", "test"])

    def test_explicit_cabinet_dispatch_keeps_full_default_team(self):
        with Runtime(":memory:") as rt:
            task_id = rt.create_task("整理一页活动说明")
            rt.dispatch(task_id)
            task = rt.task(task_id)

        self.assertEqual(task.plan["participants"], ["product", "design", "dev", "test"])

    def test_intake_can_override_direct_preference_and_convene_cabinet(self):
        with Runtime(":memory:") as rt:
            task_id = rt.create_task("重构多 Agent 协作系统并接入安全的外部 CLI", ["可回放", "可验证", "可恢复"])
            result = rt.intake(task_id, requested_route="direct", preferred_owner="dev")
            task = rt.task(task_id)
            events = rt.log.read(task_id=task_id)

        self.assertEqual(result["route"]["route"], "cabinet")
        self.assertEqual(task.route["route"], "cabinet")
        self.assertEqual(task.plan["source"], "cabinet")
        self.assertIn("cabinet.convened", [event.type for event in events])
        self.assertEqual(task.plan["members"], ["cabinet_product", "cabinet_engineering", "cabinet_quality"])
        self.assertIn("内阁", task.route["reason"])

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

                self.assertEqual(task.status, "decided")
                self.assertEqual(task.decision["source"], "leader")
                self.assertIn("agent.decided", event_types)
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
            self.assertEqual(task.cabinet["status"], "recommended")
            self.assertEqual(task.cabinet["vetoed_by"], [])
            self.assertEqual(len(task.cabinet["votes"]), 3)
            self.assertEqual(result["brief"]["cabinet"], task.cabinet)
            self.assertEqual(result["brief"]["governance"]["status"], "forced")

    def test_default_collaboration_skips_governance_when_roles_converge(self):
        with Runtime(":memory:") as rt:
            task_id = rt.create_task("Default collaboration")
            result = rt.collaborate(task_id)
            task = rt.task(task_id)
            governance = result["brief"]["governance"]

            self.assertEqual(governance["status"], "not_needed")
            self.assertEqual(governance["signals"], [])
            self.assertEqual(task.status, "decided")
            self.assertEqual(task.decision["source"], "leader")

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

            roles.write_text(
                '{"roles":[{"id":"dev","name":"Dev","role":"developer","driver":"dev","voice":7}]}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "voice must be a string"):
                Runtime(":memory:", roles_path=roles)

            roles.write_text('{"roles":[]}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "at least one role"):
                Runtime(":memory:", roles_path=roles)

    def test_explicit_organization_keeps_leadership_cabinet_and_workers_distinct(self):
        with tempfile.TemporaryDirectory() as tmp:
            roles = Path(tmp) / "roles.json"
            roles.write_text(
                json.dumps(
                    {
                        "cabinet": {"approval_threshold": 2, "quorum": 2, "veto_roles": []},
                        "organization": {
                            "leader": {"id": "chief", "name": "总负责人", "role": "最高决策者"},
                            "cabinet_members": ["product", "dev"],
                            "worker_members": ["product", "dev", "test"],
                        },
                        "roles": [
                            {"id": "product", "name": "产品", "role": "产品负责人", "driver": "product"},
                            {"id": "dev", "name": "开发", "role": "开发工程师", "driver": "dev"},
                            {"id": "test", "name": "测试", "role": "测试工程师", "driver": "test"},
                            {"id": "observer", "name": "观察", "role": "观察员", "driver": "product"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with Runtime(":memory:", roles_path=roles) as rt:
                self.assertEqual(rt.organization.leader.id, "chief")
                self.assertEqual([role.id for role in rt.cabinet_roles], ["product", "dev"])
                self.assertEqual([role.id for role in rt.worker_roles], ["product", "dev", "test"])
                task_id = rt.create_task("Respect organization seats")
                with self.assertRaisesRegex(ValueError, "not an execution role"):
                    rt.assign(task_id, "observer")
                rt.battle(task_id)
                task = rt.task(task_id)

        self.assertEqual(len(task.cabinet["votes"]), 2)
        self.assertEqual(task.decision["source"], "cabinet")

    def test_default_cat_cafe_keeps_cabinet_seats_separate_from_execution_cats(self):
        with Runtime(":memory:") as rt:
            self.assertTrue(rt.organization.cabinet_members.isdisjoint(rt.organization.worker_members))
            self.assertEqual([role.name for role in rt.worker_roles], ["月亮", "罐头", "米寇", "三角"])
            self.assertEqual([role.name for role in rt.cabinet_roles], ["白露", "阿程", "知夏"])
            self.assertTrue(all(role.charter for role in rt.roles))
            self.assertTrue(all(role.voice for role in rt.roles))
            task_id = rt.create_task("Check separate organization layers")
            rt.battle(task_id)
            task = rt.task(task_id)

        actors = [message["actor"] for message in task.messages]
        self.assertEqual(actors[:4], ["product", "design", "dev", "test"])
        self.assertEqual(set(actors[4:]), {"cabinet_product", "cabinet_engineering", "cabinet_quality"})
        self.assertEqual({vote["actor"] for vote in task.cabinet["votes"]}, {
            "cabinet_product", "cabinet_engineering", "cabinet_quality",
        })

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
            drivers = {
                "product": StaticResponseDriver('{"stance":"assert","option_id":"A","claim":"产品方案","text":"产品方案"}'),
                "dev": StaticResponseDriver('{"stance":"assert","option_id":"B","claim":"开发方案","text":"开发方案"}'),
                "test": StaticResponseDriver('{"stance":"assert","option_id":"C","claim":"测试方案","text":"测试方案"}'),
            }
            with Runtime(":memory:", roles_path=roles, drivers=drivers) as rt:
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
            self.assertEqual(len(task.driver_failures), 3)
            self.assertIn("model process exploded", task.driver_failures[0]["message"])
            self.assertTrue(any("driver failed" in msg["claim"] for msg in task.messages))
            self.assertIn("driver.failed", [event.type for event in rt.log.read(task_id=task_id)])

            rt.battle(task_id)
            task = rt.task(task_id)
            self.assertEqual(driver.calls, 2)
            self.assertTrue(any(item["attempt"] == 0 for item in task.driver_failures))

    def test_driver_health_can_keep_a_circuit_open_across_runtime_instances(self):
        driver = AlwaysFailDriver()
        health = DriverHealth()
        with Runtime(
            ":memory:",
            drivers={"product": driver},
            driver_attempts=1,
            circuit_breaker_threshold=1,
            driver_health=health,
        ) as first:
            task_id = first.create_task("Open a shared circuit")
            first.battle(task_id)
        self.assertEqual(driver.calls, 1)

        with Runtime(
            ":memory:",
            drivers={"product": driver},
            driver_attempts=1,
            circuit_breaker_threshold=1,
            driver_health=health,
        ) as second:
            task_id = second.create_task("Keep the shared circuit open")
            second.battle(task_id)
            task = second.task(task_id)
        self.assertEqual(driver.calls, 1)
        assert task is not None
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
        flat_driver = StaticResponseDriver(
            '{"stance":"assert","option_id":"A","claim":"independent","text":"independent"}'
        )
        with tempfile.TemporaryDirectory() as tmp:
            roles = Path(tmp) / "flat-roles.json"
            roles.write_text(
                """
                {
                  "governance": {"trigger_on_option_split": false},
                  "cabinet": {"approval_threshold": 2, "quorum": 2, "veto_roles": []},
                  "roles": [
                    {"id":"alpha","name":"Alpha","role":"planner","driver":"flat"},
                    {"id":"beta","name":"Beta","role":"builder","driver":"flat"}
                  ]
                }
                """,
                encoding="utf-8",
            )
            with Runtime(":memory:", roles_path=roles, drivers={"flat": flat_driver}) as rt:
                task_id = rt.create_task("Flat exchange")
                rt.collaborate(task_id)
                review = rt.review(task_id)
                findings = [item["finding"] for item in review["lenses"]]

        self.assertTrue(any("no explicit worker-to-worker reply" in finding for finding in findings))

        with Runtime(":memory:") as rt:
            task_id = rt.create_task("Guided exchange")
            rt.collaborate(task_id)
            review = rt.review(task_id)
            findings = [item["finding"] for item in review["lenses"]]

        self.assertTrue(any("uses explicit reply metadata" in finding for finding in findings))

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

    def test_review_flags_high_token_cost(self):
        driver = StaticResponseDriver(
            '{"stance":"assert","option_id":"A","claim":"Expensive output","evidence":[],"confidence":0.8,"text":"ok"}',
            tokens=5000,
        )
        with tempfile.TemporaryDirectory() as tmp:
            roles = Path(tmp) / "roles.json"
            roles.write_text(
                """
                {
                  "cabinet": {"approval_threshold": 1, "quorum": 1, "veto_roles": []},
                  "roles": [
                    {"id":"cost","name":"Cost","role":"planner","driver":"static"}
                  ]
                }
                """,
                encoding="utf-8",
            )
            with Runtime(":memory:", roles_path=roles, drivers={"static": driver}) as rt:
                task_id = rt.create_task("Watch cost")
                rt.collaborate(task_id)
                review = rt.review(task_id)
                findings = [item["finding"] for item in review["lenses"]]

        self.assertTrue(any("5000 token" in finding for finding in findings))

    def test_review_does_not_treat_unreported_token_usage_as_free(self):
        driver = StaticResponseDriver(
            '{"stance":"assert","option_id":"A","claim":"External output","evidence":[],"confidence":0.8,"text":"ok"}',
            tokens=0,
            meta={"token_usage": "unavailable"},
        )
        with tempfile.TemporaryDirectory() as tmp:
            roles = Path(tmp) / "roles.json"
            roles.write_text(
                '''{"cabinet":{"approval_threshold":1,"quorum":1,"veto_roles":[]},"roles":[{"id":"cost","name":"Cost","role":"planner","driver":"static"}]}''',
                encoding="utf-8",
            )
            with Runtime(":memory:", roles_path=roles, drivers={"static": driver}) as rt:
                task_id = rt.create_task("Watch unknown cost")
                rt.collaborate(task_id)
                task = rt.task(task_id)
                review = rt.review(task_id)

        self.assertEqual(task.token_usage, {"unavailable": 1})
        self.assertTrue(any("usage is unavailable" in item["finding"] for item in review["lenses"]))


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

    def test_cli_driver_rejects_oversized_response_and_recovers(self):
        driver = CliDriver("fixture", self.fixture_command(), timeout=5, restart_limit=0)
        self.addCleanup(driver.close)
        with self.assertRaisesRegex(DriverProtocolError, "response exceeds"):
            driver.complete(role="tester", prompt="large", context={"oversized": True})
        response = driver.complete(role="tester", prompt="after", context={})
        self.assertIn("Fixture driver responded", response.text)

    def test_cli_driver_rejects_oversized_request_before_starting_child(self):
        driver = CliDriver("fixture", self.fixture_command(), timeout=5, restart_limit=0)
        self.addCleanup(driver.close)
        with self.assertRaisesRegex(DriverProtocolError, "request exceeds"):
            driver.complete(role="tester", prompt="x" * (4 * 1024 * 1024 + 1), context={})
        self.assertIsNone(driver._proc)

    def test_runtime_can_leave_shared_driver_open(self):
        driver = CliDriver("fixture", self.fixture_command(), timeout=5)
        self.addCleanup(driver.close)
        with Runtime(":memory:", drivers={"product": driver}, close_drivers_on_close=False):
            self.assertTrue(driver.probe())
        self.assertIsNotNone(driver._proc)
        assert driver._proc is not None
        self.assertIsNone(driver._proc.poll())

    def test_openai_compatible_driver_uses_environment_key_and_reports_provider_usage(self):
        captured: dict[str, object] = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit=-1):
                return json.dumps(
                    {
                        "choices": [{"message": {"content": '{"stance":"assert","text":"完成"}'}}],
                        "usage": {"total_tokens": 37},
                    }
                ).encode("utf-8")

        def fake_open(request, *, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return FakeResponse()

        driver = OpenAICompatibleDriver(
            "worker-api",
            endpoint="https://models.example.test/v1/chat/completions",
            model="test-model",
            api_key_env="CATER_TEST_API_KEY",
            timeout=9,
            temperature=0.2,
        )
        with patch.dict("cater.drivers.os.environ", {"CATER_TEST_API_KEY": "secret-value"}, clear=False), patch(
            "cater.drivers.urlopen", side_effect=fake_open
        ):
            response = driver.complete(role="worker", prompt="Return JSON", context={})

        request = captured["request"]
        assert isinstance(request, Request)
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(request.get_header("Authorization"), "Bearer secret-value")
        self.assertEqual(body["model"], "test-model")
        self.assertEqual(body["temperature"], 0.2)
        self.assertEqual(response.tokens, 37)
        self.assertEqual(response.meta["token_usage"], "reported")
        self.assertIn("完成", response.text)
        self.assertEqual(captured["timeout"], 9)

    def test_openai_compatible_driver_rejects_oversized_api_response(self):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, limit=-1):
                return b"x" * limit

        driver = OpenAICompatibleDriver(
            "worker-api",
            endpoint="https://models.example.test/v1/chat/completions",
            model="test-model",
            api_key_env="CATER_TEST_API_KEY",
        )
        with patch.dict("cater.drivers.os.environ", {"CATER_TEST_API_KEY": "secret-value"}, clear=False), patch(
            "cater.drivers.urlopen", return_value=FakeResponse()
        ):
            with self.assertRaisesRegex(DriverProtocolError, "response exceeds"):
                driver.complete(role="worker", prompt="Return JSON", context={})

    def test_openai_compatible_driver_does_not_send_a_request_without_its_key(self):
        driver = OpenAICompatibleDriver(
            "worker-api",
            endpoint="https://models.example.test/v1/chat/completions",
            model="test-model",
            api_key_env="CATER_MISSING_KEY",
        )
        with patch.dict("cater.drivers.os.environ", {}, clear=True), patch("cater.drivers.urlopen") as open_request:
            with self.assertRaisesRegex(DriverUnavailable, "CATER_MISSING_KEY"):
                driver.complete(role="worker", prompt="Return JSON", context={})
        open_request.assert_not_called()

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

    def test_openai_compatible_driver_config_rejects_incomplete_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            drivers = Path(tmp) / "drivers.json"
            drivers.write_text(
                '{"drivers":[{"kind":"openai_compatible","name":"bad","endpoint":"ftp://example","model":"x","api_key_env":"KEY"}]}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "requires an HTTPS endpoint"):
                load_drivers(drivers)

    def test_openai_compatible_driver_config_requires_https_except_local_development(self):
        with tempfile.TemporaryDirectory() as tmp:
            drivers = Path(tmp) / "drivers.json"
            drivers.write_text(
                '{"drivers":[{"kind":"openai_compatible","name":"unsafe","endpoint":"http://models.example.test/v1","model":"x","api_key_env":"KEY"}]}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "requires an HTTPS endpoint"):
                load_drivers(drivers)

            drivers.write_text(
                '{"drivers":[{"kind":"openai_compatible","name":"local","endpoint":"http://127.0.0.1:11434/v1/chat/completions","model":"x","api_key_env":"KEY"}]}',
                encoding="utf-8",
            )
            self.assertIn("local", load_drivers(drivers))
            drivers.write_text(
                '{"drivers":[{"kind":"openai_compatible","name":"bad","endpoint":"https://example","model":"x"}]}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "api_key_env"):
                load_drivers(drivers)
            drivers.write_text(
                '{"drivers":[{"kind":"guided","name":"bad","token_budget":1.5}]}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "token_budget must be a non-negative integer"):
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

    def test_example_tool_driver_can_reply_to_visible_worker_output(self):
        script = Path(__file__).resolve().parents[1] / "examples" / "ndjson_tool_driver.py"
        driver = CliDriver("tool", [sys.executable, str(script)], timeout=5)
        self.addCleanup(driver.close)

        response = driver.complete(
            role="builder",
            prompt="Current round memory: alpha/assert: Draft the plan (#12).",
            context={"goal": "continue the plan"},
        )
        payload = json.loads(response.text)

        self.assertEqual(payload["target"], "alpha")
        self.assertEqual(payload["reply_to"], 12)
        self.assertIn("continues alpha", payload["claim"])

    def test_codex_cli_adapter_answers_ping_without_running_a_model(self):
        script = Path(__file__).resolve().parents[1] / "examples" / "codex_cli_driver.py"
        driver = CliDriver("codex", [sys.executable, str(script)], timeout=5)
        self.addCleanup(driver.close)

        self.assertTrue(driver.probe())

    def test_codex_cli_adapter_uses_read_only_structured_invocation(self):
        script = Path(__file__).resolve().parents[1] / "examples" / "codex_cli_driver.py"
        spec = importlib.util.spec_from_file_location("test_codex_cli_driver", script)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        expected = {
            "stance": "assert",
            "option_id": "A",
            "claim": "A concise contribution",
            "evidence": ["visible task context"],
            "confidence": 0.8,
            "text": "我会先交付一个可复查的最小结果。",
            "target": None,
            "reply_to": None,
            "deliverables": [],
        }

        def fake_run(command, **_kwargs):
            output = Path(command[command.index("--output-last-message") + 1])
            output.write_text(json.dumps(expected, ensure_ascii=False), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0)

        with patch.object(module.subprocess, "run", side_effect=fake_run) as run:
            response = module.handle(
                {"id": "req-1", "type": "complete", "role": "developer", "prompt": "Build a page", "context": {}}
            )

        self.assertTrue(response["ok"])
        self.assertEqual(json.loads(response["text"]), expected)
        command = run.call_args.args[0]
        self.assertIn("--sandbox", command)
        self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
        self.assertIn("--output-schema", command)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", command)

    def test_codex_cli_adapter_accepts_a_quoted_windows_executable_path(self):
        script = Path(__file__).resolve().parents[1] / "examples" / "codex_cli_driver.py"
        spec = importlib.util.spec_from_file_location("test_codex_cli_command", script)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        with patch.dict(module.os.environ, {"CATER_CODEX_COMMAND": '"C:\\Program Files\\Codex\\codex.exe" --profile local'}):
            command = module._codex_command()

        self.assertEqual(command, ["C:\\Program Files\\Codex\\codex.exe", "--profile", "local"])

    def test_codex_cli_adapter_preserves_an_unquoted_existing_windows_path(self):
        script = Path(__file__).resolve().parents[1] / "examples" / "codex_cli_driver.py"
        spec = importlib.util.spec_from_file_location("test_codex_cli_path", script)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "codex.exe"
            executable.write_text("fixture", encoding="utf-8")
            with patch.dict(module.os.environ, {"CATER_CODEX_COMMAND": str(executable)}):
                command = module._codex_command()

        self.assertEqual(command, [str(executable)])

    def test_example_api_client_drives_web_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "api-client.db")
            base = start_test_web(self, db, access_token="api-client-token")

            script = Path(__file__).resolve().parents[1] / "examples" / "api_client.py"
            result = subprocess.run(
                [sys.executable, str(script), "--base-url", base, "--goal", "API example task"],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
                env={**os.environ, "CATER_WEB_ACCESS_TOKEN": "api-client-token"},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("task=task_", result.stdout)
            self.assertIn("messages=1", result.stdout)
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
        self.assertIn("任务：", out)
        self.assertIn("任务分发：已完成", out)
        self.assertIn("本轮摘要：", out)
        self.assertIn("最近协作：", out)
        self.assertIn("接下来可以：", out)
        self.assertIn("cater show", out)
        self.assertIn("cater review", out)
        self.assertIn("cater chain --json", out)
        self.assertIn("/api/tasks/", out)
        task_id = next(line for line in out.splitlines() if line.startswith("任务：")).split("：", 1)[1]
        with Runtime(self.db) as rt:
            task = rt.task(task_id)
        assert task is not None
        self.assertIn("可回放的 Cater 协作演示", task.goal)
        self.assertEqual(task.acceptance, ["至少三位伙伴参与", "内阁策略记录下一步行动"])
        code, out = self.run_cli("chain")
        self.assertEqual(code, 0)
        self.assertIn("hash chain ok", out)

    def test_cli_direct_demo_shows_lightweight_path(self):
        code, out = self.run_cli("demo", "--direct")
        self.assertEqual(code, 0)
        self.assertIn("协作路径：执行猫咪直接协作", out)
        self.assertIn("已交给：米寇", out)
        self.assertIn("治理状态：not_needed", out)
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
        self.assertIn("已交给：米寇", self.out.getvalue())

    def test_cli_version(self):
        with self.assertRaises(SystemExit) as raised:
            main(["--version"])
        self.assertEqual(raised.exception.code, 0)
        self.assertIn("cater 0.1.0", self.out.getvalue())

    def test_cli_configures_utf8_when_output_stream_supports_it(self):
        class ReconfigurableStream:
            def __init__(self):
                self.calls: list[dict[str, str]] = []

            def reconfigure(self, **kwargs: str) -> None:
                self.calls.append(kwargs)

        stdout = ReconfigurableStream()
        stderr = ReconfigurableStream()
        cli_module._configure_utf8_output(stdout, stderr)
        self.assertEqual(stdout.calls, [{"encoding": "utf-8"}])
        self.assertEqual(stderr.calls, [{"encoding": "utf-8"}])

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
        self.assertNotIn("cabinet=", out)
        self.assertIn("assignments=4", out)
        self.assertIn("ladder:", out)
        self.assertIn("Cabinet: skipped", out)

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

    def test_cli_new_run_uses_the_same_leader_intake_as_the_web(self):
        code, out = self.run_cli("new", "做一个网页任务入口", "--accept", "能发起任务", "--accept", "过程可回放", "--run")
        self.assertEqual(code, 0)
        self.assertIn("leader route: collaborate", out)
        task_id = next(line for line in out.splitlines() if line.startswith("created ")).split()[1]

        with Runtime(self.db) as rt:
            task = rt.task(task_id)

        self.assertIsNotNone(task)
        assert task is not None
        self.assertTrue(task.interpretation)
        self.assertEqual(task.route["route"], "collaborate")
        self.assertEqual([item["owner"] for item in task.assignments], ["product", "design", "dev", "test"])

    def test_cli_new_complex_run_convenes_cabinet_before_execution(self):
        code, out = self.run_cli(
            "new",
            "重构多 Agent 协作系统并接入安全的外部 CLI",
            "--accept",
            "可回放",
            "--accept",
            "可验证",
            "--accept",
            "可恢复",
            "--run",
        )
        self.assertEqual(code, 0)
        self.assertIn("leader route: cabinet", out)
        task_id = next(line for line in out.splitlines() if line.startswith("created ")).split()[1]

        with Runtime(self.db) as rt:
            task = rt.task(task_id)

        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task.plan["source"], "cabinet")
        self.assertEqual(task.route["route"], "cabinet")

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
        self.assertIn("challenges=0", out)

    def test_cli_policy(self):
        code, out = self.run_cli("policy")
        self.assertEqual(code, 0)
        self.assertIn("governance:", out)
        self.assertIn("trigger_on_challenge=True", out)
        self.assertIn("cabinet:", out)
        self.assertIn("veto_roles=cabinet_quality", out)
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
        self.assertEqual(data["roles"], 7)
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

    def test_cli_reports_missing_task_without_traceback(self):
        code, out = self.run_cli("path", "missing-task")

        self.assertEqual(code, 1)
        self.assertIn("error: not found: missing-task", out)
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
        self.assertIn("assignments=4", out)

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

    def test_cli_assign_lets_leader_upgrade_an_unplanned_complex_task(self):
        code, out = self.run_cli(
            "new",
            "重构多 Agent 协作系统并接入安全的外部 CLI",
            "--accept",
            "可回放",
            "--accept",
            "可验证",
            "--accept",
            "可恢复",
        )
        self.assertEqual(code, 0)
        task_id = next(line for line in out.splitlines() if line.startswith("created ")).split()[1]

        code, out = self.run_cli("assign", task_id, "dev", "--objective", "只做最小实现")
        self.assertEqual(code, 0)
        self.assertIn("leader route: cabinet", out)

        with Runtime(self.db) as rt:
            task = rt.task(task_id)

        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task.route["route"], "cabinet")
        self.assertEqual(task.plan["source"], "cabinet")

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
        self.assertIn("expected one of: product, design, dev, test", out)

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
                                            "reply_to": 4,
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
        self.assertRegex(out, r"\[target alpha \| reply_to #\d+\]")

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

    def test_web_refuses_non_loopback_bind_without_explicit_opt_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "remote-bind.db")
            with self.assertRaisesRegex(ValueError, "unauthenticated Cater control plane"):
                web_module.serve(db, host="0.0.0.0", port=0)
            with self.assertRaisesRegex(ValueError, "without an access token"):
                web_module.serve(db, host="0.0.0.0", port=0, allow_remote=True)
        self.assertTrue(web_module._is_loopback_host("127.0.0.1"))
        self.assertTrue(web_module._is_loopback_host("::1"))
        self.assertTrue(web_module._is_loopback_host("localhost"))
        self.assertFalse(web_module._is_loopback_host("192.168.1.15"))

    def test_web_access_token_protects_task_data_and_mutations(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "token-protected.db")
            static_dir = Path(__file__).resolve().parents[1] / "cater" / "static" / "workroom"
            base = start_test_web(self, db, access_token="test-access-token", static_dir=static_dir)

            with self.assertRaises(HTTPError) as raised:
                urlopen(f"{base}/api/tasks", timeout=5)
            self.assertEqual(raised.exception.code, 401)
            self.assertEqual(raised.exception.headers["WWW-Authenticate"], 'Bearer realm="Cater"')

            wrong = Request(f"{base}/api/tasks", headers={"X-Cater-Access-Token": "wrong"})
            with self.assertRaises(HTTPError) as raised:
                urlopen(wrong, timeout=5)
            self.assertEqual(raised.exception.code, 401)

            create = Request(
                f"{base}/api/tasks",
                data=json.dumps({"goal": "protected task"}).encode("utf-8"),
                method="POST",
                headers={"Content-Type": "application/json", "X-Cater-Access-Token": "test-access-token"},
            )
            with urlopen(create, timeout=5) as response:
                self.assertEqual(response.status, 201)

            with urlopen(f"{base}/", timeout=5) as response:
                self.assertEqual(response.status, 200)

            legacy_base = start_test_web(self, db, access_token="test-access-token", static_dir=None)
            with self.assertRaises(HTTPError) as raised:
                urlopen(f"{legacy_base}/", timeout=5)
            self.assertEqual(raised.exception.code, 401)

            legacy_index = Request(
                f"{legacy_base}/",
                headers={"X-Cater-Access-Token": "test-access-token"},
            )
            with urlopen(legacy_index, timeout=5) as response:
                self.assertEqual(response.status, 200)

    def test_web_api_rejects_an_oversized_request_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "large-body.db")
            base = start_test_web(self, db)
            body = b"x" * (web_module.MAX_REQUEST_BYTES + 1)
            request = Request(
                f"{base}/api/tasks",
                data=body,
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with self.assertRaises(HTTPError) as raised:
                urlopen(request, timeout=5)
            self.assertEqual(raised.exception.code, 400)
            self.assertIn("request body exceeds", raised.exception.read().decode("utf-8"))

    def test_web_reuses_a_shared_cli_driver_across_requests(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "shared-driver.db")
            driver = CliDriver(
                "product",
                [sys.executable, str(Path(__file__).resolve().parent / "fixtures" / "ndjson_driver.py")],
                timeout=5,
            )
            self.addCleanup(driver.close)
            base = start_test_web(self, db, shared_drivers={"product": driver})
            request = Request(
                f"{base}/api/tasks",
                data=json.dumps({"goal": "Use the shared CLI driver", "run": True}).encode("utf-8"),
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urlopen(request, timeout=5) as response:
                self.assertEqual(response.status, 201)
            self.assertIsNotNone(driver._proc)
            assert driver._proc is not None
            pid = driver._proc.pid
            with urlopen(f"{base}/api/tasks", timeout=5) as response:
                self.assertEqual(response.status, 200)
            self.assertIsNotNone(driver._proc)
            assert driver._proc is not None
            self.assertEqual(driver._proc.pid, pid)

    def test_web_rejects_cross_origin_writes_and_sets_browser_security_headers(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "origin.db")
            base = start_test_web(self, db)
            request = Request(
                f"{base}/api/tasks",
                data=json.dumps({"goal": "cross-origin task"}).encode("utf-8"),
                method="POST",
                headers={"Content-Type": "application/json", "Origin": "https://evil.example"},
            )
            with self.assertRaises(HTTPError) as raised:
                urlopen(request, timeout=5)
            self.assertEqual(raised.exception.code, 403)
            self.assertEqual(raised.exception.headers["X-Frame-Options"], "DENY")
            self.assertIn("default-src 'self'", raised.exception.headers["Content-Security-Policy"])

            proxied_https = Request(
                f"{base}/api/tasks",
                data=json.dumps({"goal": "HTTPS reverse proxy task"}).encode("utf-8"),
                method="POST",
                headers={"Content-Type": "application/json", "Origin": base.replace("http://", "https://", 1)},
            )
            with urlopen(proxied_https, timeout=5) as response:
                self.assertEqual(response.status, 201)

            with urlopen(f"{base}/api/tasks", timeout=5) as response:
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
    def test_web_marks_interrupted_background_activity_after_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "web-recovery.db")
            with Runtime(db) as rt:
                task_id = rt.create_task("Recover an interrupted activity")
                rt.log.emit(
                    actor=web_module.EventActor.system("orchestrator"),
                    type="activity.queued",
                    task_id=task_id,
                    payload={"activity_id": "activity_old", "action": "run"},
                )

            base = start_test_web(self, db)
            with urlopen(f"{base}/api/tasks/{task_id}", timeout=2) as response:
                payload = json.loads(response.read().decode("utf-8"))

            self.assertEqual(payload["activity"]["state"], "failed")
            self.assertIn("服务在这轮协作结束前重启", payload["activity"]["error"])
            failures = [event for event in payload["events"] if event["type"] == "activity.failed"]
            self.assertEqual(failures[-1]["payload"]["error_type"], "InterruptedActivity")

    def test_web_can_run_collaboration_in_background_without_duplicate_rounds(self):
        driver = BlockingDriver()
        self.addCleanup(driver.release.set)
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "web-background.db")
            roles = Path(tmp) / "roles.json"
            roles.write_text(
                json.dumps(
                    {
                        "governance": {"trigger_on_option_split": False},
                        "cabinet": {"approval_threshold": 1, "quorum": 1, "veto_roles": []},
                        "roles": [{"id": "worker", "name": "执行", "role": "worker", "driver": "blocking"}],
                    }
                ),
                encoding="utf-8",
            )

            def runtime_factory() -> Runtime:
                return Runtime(db, roles_path=roles, drivers={"blocking": driver})

            base, server, thread = start_test_web(
                self,
                db,
                runtime_factory=runtime_factory,
                return_server=True,
            )
            request = Request(
                f"{base}/api/tasks",
                data=json.dumps({"goal": "Run in the background", "run": True, "background": True}).encode("utf-8"),
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urlopen(request, timeout=2) as response:
                self.assertEqual(response.status, 202)
                queued = json.loads(response.read().decode("utf-8"))

            task_id = queued["task"]["id"]
            self.assertTrue(queued["activity"]["active"])
            self.assertTrue(driver.started.wait(timeout=1))

            duplicate = Request(
                f"{base}/api/tasks/{task_id}/run",
                data=json.dumps({"background": True}).encode("utf-8"),
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urlopen(duplicate, timeout=2) as response:
                self.assertEqual(response.status, 202)
                repeated = json.loads(response.read().decode("utf-8"))
            self.assertEqual(repeated["activity"]["id"], queued["activity"]["id"])
            self.assertEqual(
                sum(event["type"] == "activity.queued" for event in repeated["events"]),
                1,
            )

            driver.release.set()
            deadline = time.monotonic() + 3
            completed = repeated
            while time.monotonic() < deadline:
                with urlopen(f"{base}/api/tasks/{task_id}", timeout=2) as response:
                    completed = json.loads(response.read().decode("utf-8"))
                if not completed["activity"]["active"]:
                    break
                time.sleep(0.03)

            self.assertEqual(completed["activity"]["state"], "completed")
            self.assertIn("activity.completed", [event["type"] for event in completed["events"]])
            self.assertEqual(len(completed["task"]["messages"]), 1)
            server.shutdown()
            thread.join(timeout=1)
            server.server_close()

    def test_web_queues_a_follow_up_round_after_a_human_direction(self):
        driver = BlockingDriver()
        self.addCleanup(driver.release.set)
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "web-follow-up.db")
            roles = Path(tmp) / "roles.json"
            roles.write_text(
                json.dumps(
                    {
                        "governance": {"trigger_on_option_split": False},
                        "cabinet": {"approval_threshold": 1, "quorum": 1, "veto_roles": []},
                        "roles": [{"id": "worker", "name": "执行", "role": "worker", "driver": "blocking"}],
                    }
                ),
                encoding="utf-8",
            )

            def runtime_factory() -> Runtime:
                return Runtime(db, roles_path=roles, drivers={"blocking": driver})

            base, server, thread = start_test_web(
                self,
                db,
                runtime_factory=runtime_factory,
                return_server=True,
            )
            create = Request(
                f"{base}/api/tasks",
                data=json.dumps({"goal": "Work, then change direction", "run": True, "background": True}).encode("utf-8"),
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urlopen(create, timeout=2) as response:
                task_id = json.loads(response.read().decode("utf-8"))["task"]["id"]
            self.assertTrue(driver.started.wait(timeout=1))

            command = Request(
                f"{base}/api/tasks/{task_id}/command",
                data=json.dumps({"instruction": "请内阁一起审查更新后的方向", "continue": True}).encode("utf-8"),
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urlopen(command, timeout=2) as response:
                queued = json.loads(response.read().decode("utf-8"))

            self.assertTrue(queued["activity"]["active"])
            self.assertEqual(queued["activity"]["follow_up"]["action"], "battle")
            self.assertIn("activity.queued", [event["type"] for event in queued["events"]])
            self.assertIn("human.commanded", [event["type"] for event in queued["events"]])
            self.assertIn("plan.amended", [event["type"] for event in queued["events"]])

            driver.release.set()
            deadline = time.monotonic() + 3
            completed = queued
            while time.monotonic() < deadline:
                with urlopen(f"{base}/api/tasks/{task_id}", timeout=2) as response:
                    completed = json.loads(response.read().decode("utf-8"))
                if not completed["activity"]["active"]:
                    break
                time.sleep(0.03)

            self.assertEqual(completed["activity"]["state"], "completed")
            self.assertGreaterEqual(len(completed["task"]["messages"]), 2)
            server.shutdown()
            thread.join(timeout=1)
            server.server_close()

    def test_web_uses_the_latest_queued_strategy_after_multiple_directions(self):
        driver = BlockingDriver()
        self.addCleanup(driver.release.set)
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "web-latest-direction.db")
            roles = Path(tmp) / "roles.json"
            roles.write_text(
                json.dumps(
                    {
                        "governance": {"trigger_on_option_split": False},
                        "cabinet": {"approval_threshold": 1, "quorum": 1, "veto_roles": []},
                        "roles": [{"id": "worker", "name": "执行", "role": "worker", "driver": "blocking"}],
                    }
                ),
                encoding="utf-8",
            )

            def runtime_factory() -> Runtime:
                return Runtime(db, roles_path=roles, drivers={"blocking": driver})

            base, server, thread = start_test_web(self, db, runtime_factory=runtime_factory, return_server=True)
            create = Request(
                f"{base}/api/tasks",
                data=json.dumps({"goal": "Work while receiving new directions", "run": True, "background": True}).encode("utf-8"),
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urlopen(create, timeout=2) as response:
                task_id = json.loads(response.read().decode("utf-8"))["task"]["id"]
            self.assertTrue(driver.started.wait(timeout=1))

            for instruction in ("先继续正常推进", "请内阁一起审查最新取舍"):
                command = Request(
                    f"{base}/api/tasks/{task_id}/command",
                    data=json.dumps({"instruction": instruction, "continue": True}).encode("utf-8"),
                    method="POST",
                    headers={"Content-Type": "application/json"},
                )
                with urlopen(command, timeout=2) as response:
                    queued = json.loads(response.read().decode("utf-8"))

            self.assertEqual(queued["activity"]["follow_up"]["action"], "battle")
            replacements = [
                event for event in queued["events"]
                if event["type"] == "activity.queued" and event["payload"].get("replaces_action") == "run"
            ]
            self.assertEqual(replacements[-1]["payload"]["action"], "battle")

            driver.release.set()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                with urlopen(f"{base}/api/tasks/{task_id}", timeout=2) as response:
                    completed = json.loads(response.read().decode("utf-8"))
                if not completed["activity"]["active"]:
                    break
                time.sleep(0.03)
            self.assertEqual(completed["activity"]["state"], "completed")
            server.shutdown()
            thread.join(timeout=1)
            server.server_close()

    def test_bundled_workroom_includes_chinese_shell_and_static_assets(self):
        root = Path(web_module.__file__).resolve().parent / "static" / "workroom"
        shell = (root / "index.html").read_text(encoding="utf-8")

        self.assertIn('lang="zh-CN"', shell)
        self.assertIn("Cater 自治工作台", shell)
        self.assertTrue((root / "favicon.svg").is_file())
        bundles = list((root / "assets").glob("*.js"))
        self.assertTrue(bundles)
        self.assertTrue(list((root / "assets").glob("*.css")))
        bundle_text = "\n".join(bundle.read_text(encoding="utf-8") for bundle in bundles)
        self.assertIn("对话筛选", bundle_text)
        self.assertIn("和店长聊聊", bundle_text)
        self.assertNotIn("向猫咖下单", bundle_text)

    def test_web_serves_optional_built_workroom_from_same_origin(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "dist"
            assets = root / "assets"
            assets.mkdir(parents=True)
            (root / "index.html").write_text("<main>workroom</main>", encoding="utf-8")
            (assets / "workroom.js").write_text("console.log('workroom')", encoding="utf-8")
            db = str(Path(tmp) / "web-static.db")
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(db, static_dir=root))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(server.server_close)
            self.addCleanup(thread.join, 1)
            self.addCleanup(server.shutdown)
            base = f"http://127.0.0.1:{server.server_port}"

            with urlopen(f"{base}/", timeout=5) as response:
                self.assertEqual(response.read().decode("utf-8"), "<main>workroom</main>")
                self.assertIn("text/html", response.headers["Content-Type"])
                self.assertEqual(response.headers["Cache-Control"], "no-cache")
            with urlopen(f"{base}/assets/workroom.js", timeout=5) as response:
                self.assertIn("console.log", response.read().decode("utf-8"))
                self.assertEqual(response.headers["Cache-Control"], "public, max-age=31536000, immutable")
            with urlopen(f"{base}/api/tasks", timeout=5) as response:
                self.assertEqual(json.loads(response.read().decode("utf-8")), [])

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

        self.assertRegex(html, r"target alpha \| reply_to #\d+")

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
        self.assertIn("由顾客明确要求启动治理轮次", html)
        self.assertIn("cabinet.voted", html)
        self.assertIn("agent.decided", html)
        self.assertIn("payload-json", html)
        self.assertIn("human / lifecycle", html)
        self.assertIn("filter-pill active", html)
        self.assertEqual([step["id"] for step in ladder], ["battle", "cabinet", "supreme", "human"])
        self.assertEqual(ladder[1]["status"], "recommended")

    def test_render_task_can_filter_timeline_by_category(self):
        with Runtime(":memory:") as rt:
            task_id = rt.create_task("Filter timeline")
            rt.battle(task_id)
            events = rt.log.read(task_id=task_id)
            html = render_task(rt.task(task_id), events, timeline_category="dialogue")

        dialogue_events = [event for event in events if event.category == "dialogue"]
        self.assertIn("dialogue", html)
        self.assertIn(f"{len(dialogue_events)} / {len(events)} events", html)
        self.assertIn("msg.said", html)
        self.assertNotIn("task.created", html)

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
            self.assertIn('<option value="dev">dev / 工程实现猫</option>', page)

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

    def test_web_exposes_configured_cat_cafe_identity_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "web-organization.db")
            base = start_test_web(self, db)
            with urlopen(f"{base}/api/organization/profile", timeout=5) as response:
                profile = json.loads(response.read().decode("utf-8"))

        self.assertEqual(profile["leader"]["name"], "店长")
        self.assertEqual([member["name"] for member in profile["workers"]], ["月亮", "罐头", "米寇", "三角"])
        self.assertEqual(len(profile["cabinet"]), 3)
        self.assertTrue(profile["workers"][0]["charter"])
        self.assertEqual(profile["mode"], "local_demo")
        self.assertTrue(all(item["mode"] == "local_demo" for item in profile["worker_drivers"]))

    def test_web_marks_a_partially_connected_team_as_mixed(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "web-mixed-drivers.db")
            drivers = Path(tmp) / "drivers.json"
            drivers.write_text(
                json.dumps(
                    {
                        "drivers": [
                            {"kind": "cli", "name": "product", "command": [sys.executable, "-c", "pass"]}
                        ]
                    }
                ),
                encoding="utf-8",
            )
            base = start_test_web(self, db, drivers_path=drivers)
            with urlopen(f"{base}/api/organization/profile", timeout=5) as response:
                profile = json.loads(response.read().decode("utf-8"))

        by_role = {item["role_id"]: item["mode"] for item in profile["worker_drivers"]}
        self.assertEqual(profile["mode"], "mixed_driver")
        self.assertEqual(by_role["product"], "external")
        self.assertEqual(by_role["design"], "local_demo")

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

    def test_web_api_uses_a_single_display_name_mention_as_a_lightweight_preference(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "web-mention.db")
            base = start_test_web(self, db)
            request = Request(
                f"{base}/api/tasks",
                data=json.dumps({"goal": "@月亮 帮我整理首页文案"}).encode("utf-8"),
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urlopen(request, timeout=5) as response:
                data = json.loads(response.read().decode("utf-8"))

        self.assertEqual(data["task"]["route"]["route"], "direct")
        self.assertEqual(data["task"]["assignments"][0]["owner"], "product")

    def test_web_api_direct_preference_preserves_custom_assignment_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "web-direct-preference.db")
            base = start_test_web(self, db)
            request = Request(
                f"{base}/api/tasks",
                data=json.dumps(
                    {
                        "goal": "给接口补一条最小路径",
                        "assign_to": "dev",
                        "assign_objective": "只实现健康检查接口",
                        "assign_acceptance": "接口返回正常状态",
                    }
                ).encode("utf-8"),
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urlopen(request, timeout=5) as response:
                data = json.loads(response.read().decode("utf-8"))

        assignment = data["task"]["assignments"][0]
        self.assertEqual(data["task"]["route"]["route"], "direct")
        self.assertEqual(assignment["objective"], "只实现健康检查接口")
        self.assertEqual(assignment["acceptance"], "接口返回正常状态")

    def test_web_api_runs_every_new_conversation_through_leader_intake(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "web-default-intake.db")
            base = start_test_web(self, db)
            request = Request(
                f"{base}/api/tasks",
                data=json.dumps({"goal": "帮我整理一页活动说明"}).encode("utf-8"),
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urlopen(request, timeout=5) as response:
                data = json.loads(response.read().decode("utf-8"))

        self.assertEqual(data["task"]["route"]["route"], "collaborate")
        self.assertTrue(data["task"]["interpretation"])
        self.assertTrue(data["task"]["assignments"])

    def test_web_api_intake_exposes_interpretation_and_can_upgrade_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "web-api-intake.db")
            base = start_test_web(self, db)
            body = json.dumps(
                {
                    "goal": "重构多 Agent 协作系统并接入外部 CLI",
                    "acceptance": ["可回放", "可恢复", "可验证"],
                    "requested_route": "direct",
                    "assign_to": "dev",
                }
            ).encode("utf-8")
            request = Request(
                f"{base}/api/tasks",
                data=body,
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urlopen(request, timeout=5) as response:
                data = json.loads(response.read().decode("utf-8"))

        self.assertEqual(data["task"]["route"]["route"], "cabinet")
        self.assertIn("协作", data["task"]["interpretation"]["objective"])
        self.assertIn("cabinet.convened", [event["type"] for event in data["events"]])

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
                self.assertEqual(tasks[0].status, "decided")
                self.assertEqual(tasks[0].decision["source"], "leader")
                self.assertEqual(tasks[0].acceptance, ["battle runs", "inbox updates"])
                self.assertEqual(len(tasks[0].assignments), 4)

            with urlopen(f"{base}/", timeout=5) as response:
                index = response.read().decode("utf-8")
            self.assertIn("Agent Profiles", index)
            self.assertIn("Organization Policy", index)
            self.assertIn("Reliability", index)
            self.assertIn("Self Review", index)
            self.assertIn("Create from web", index)
            self.assertIn('name="run_now"', index)
            self.assertNotIn('name="battle" checked', index)

    def test_web_command_can_request_the_next_collaboration_round(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "web-command-round.db")
            with Runtime(db) as rt:
                task_id = rt.create_task("Continue this conversation")

            base = start_test_web(self, db)
            body = urlencode({"instruction": "@dev propose the smallest next step", "run_now": "on"}).encode("utf-8")
            request = Request(
                f"{base}/tasks/{task_id}/command",
                data=body,
                method="POST",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            with urlopen(request, timeout=5) as response:
                page = response.read().decode("utf-8")

            self.assertIn("@dev propose the smallest next step", page)
            with Runtime(db) as rt:
                task = rt.task(task_id)
                self.assertEqual(task.commands[-1]["instruction"], "@dev propose the smallest next step")
                self.assertEqual(task.commands[-1]["mentions"], ["dev"])
                self.assertEqual(len(task.messages), 1)
                self.assertEqual(task.messages[0]["actor"], "dev")


if __name__ == "__main__":
    unittest.main()

