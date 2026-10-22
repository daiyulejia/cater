"""Explanation guides stay optional and available without delivery authority."""

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

from cater.runtime import Runtime
from cater.workspaces import WorkspaceService
import test_simple_team as simple


class ExplanationGuidanceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.project = Path(__file__).resolve().parents[1]
        roles = json.loads((self.project / "cater/packs/default_roles.json").read_text(encoding="utf-8"))
        roles["organization"]["leader"]["driver"] = "guidance_test"
        for role in roles["roles"]:
            role["driver"] = "guidance_test"
        roles_path = self.root / "roles.json"
        roles_path.write_text(json.dumps(roles), encoding="utf-8")
        self.driver = simple.TeamDriver()
        self.rt = Runtime(self.root / "test.db", roles_path=roles_path,
                          drivers={"guidance_test": self.driver})
        self.addCleanup(self.rt.close)
        self.task = self.rt.create_task("Explain existing work")
        self.rt.team.enable(self.task)
        self.manager = self.rt.organization.leader.id
        self.worker = self.rt.worker_roles[0].id
        self.guide = (self.project / "cater/packs/skills/explanatory-delivery.md").read_bytes()

    def events(self, kind):
        return list(self.rt.log.iter_events(task_id=self.task, event_types=(kind,)))

    def run_ok(self):
        self.rt.collaborate(self.task)
        self.assertFalse([run for run in self.rt.team.view(self.task)["runs"] if run["status"] == "failed"])
        self.assertTrue(self.rt.verify_chain().ok)

    def assert_guide_read(self):
        receipt, = [event for event in self.events("team.tool_finished")
                    if event.payload["name"] == "read_skill"]
        result = receipt.payload["result"]
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["content"], self.guide.decode("utf-8"))
        self.assertEqual(result["skill"]["sha256"], hashlib.sha256(self.guide).hexdigest())
        self.assertFalse(result["has_more"])

    def test_every_role_can_read_guide_without_workspace_or_execution_tools(self):
        for role in (self.rt.organization.leader, *self.rt.roles):
            with self.subTest(role=role.id):
                scope = {"role_id": role.id, "workspace_id": None, "tools": set()}
                index = self.rt.skills.index(**scope)
                entry, = [row for row in index["skills"] if row["id"] == "explanatory-delivery"]
                self.assertEqual(entry["requires"], [])
                self.assertNotIn("content", entry)
                result = self.rt.skills.read({"skill_id": entry["id"]}, **scope)
                self.assertEqual(result["status"], "ok")
                self.assertEqual(result["content"], self.guide.decode("utf-8"))
        self.assertEqual(self.events("team.tool_started"), [])
        self.assertFalse(self.rt.workspaces.view(self.task)["available"])

    def test_direct_worker_reads_on_demand_then_sends_text_only_reply(self):
        answer = "Source analysis only; the proposed command has not run."
        self.driver.steps[self.worker] = [simple.turn("", ("read_skill", {"skill_id": "explanatory-delivery"})),
                                          simple.turn("", ("reply_to_customer", {"text": answer})),
                                          AssertionError("A reply must not request another turn")]
        role = self.rt.team._role(self.worker)
        self.rt.team.receive(self.task, f"@{role.name} Explain the existing result.")
        self.run_ok()
        self.assert_guide_read()
        first, second = self.driver.calls
        entry, = [row for row in first["context"]["skills_index"]["skills"]
                  if row["id"] == "explanatory-delivery"]
        self.assertNotIn("content", entry)
        self.assertNotIn(self.guide.decode("utf-8"), json.dumps(first["context"], ensure_ascii=False))
        returned, = [row for row in second["history"] if row["role"] == "tool"]
        self.assertEqual(json.loads(returned["content"])["content"], self.guide.decode("utf-8"))
        self.assertEqual(first["tools"], second["tools"])
        self.assertNotIn("finish_work", {tool["name"] for tool in first["tools"]})
        reply, = self.events("msg.said")
        self.assertEqual(reply.payload["text"], answer)
        self.assertEqual(reply.payload["audience"], "customer")
        self.assertEqual(self.events("team.objective_completed"), [])
        self.assertEqual(self.events("team.review_requested"), [])

    def test_delegated_worker_can_read_without_customer_reply_or_finish_tool(self):
        self.driver.steps = {
            self.manager: [simple.turn("", ("delegate", {
                "recipient": self.worker, "instruction": "Explain the source without executing or changing files."})),
                simple.turn("Waiting for the explanation."), simple.turn("The explanation is received.")],
            self.worker: [simple.turn("", ("read_skill", {"skill_id": "explanatory-delivery"})),
                          simple.turn("Only source analysis was requested and performed.")],
        }
        self.rt.team.receive(self.task, "Ask a partner to explain the existing work.")
        self.run_ok()
        self.assert_guide_read()
        calls = [call for call in self.driver.calls if call["context"]["you"]["id"] == self.worker]
        self.assertEqual(len(calls), 2)
        for call in calls:
            self.assertIsNotNone(call["context"]["active_assignment"])
            names = {tool["name"] for tool in call["tools"]}
            self.assertIn("read_skill", names)
            self.assertNotIn("reply_to_customer", names)
            self.assertNotIn("finish_work", names)
        self.assertEqual(self.events("team.objective_completed"), [])
        self.assertEqual(self.events("team.review_requested"), [])

    def test_paused_worker_read_does_not_restore_execution_permissions(self):
        workspace = self.root / "project"
        workspace.mkdir()
        config = self.root / "workspaces.json"
        config.write_text(json.dumps({"workspaces": [{"id": "fixture", "title": "Fixture", "root": str(workspace),
            "mode": "execute", "commands": {"python": [sys.executable, "-B"]}}]}), encoding="utf-8")
        self.rt.workspaces = WorkspaceService(self.rt, config)
        self.rt.workspaces.bind(self.task, "fixture", expected_revision=None)
        role = self.rt.team._role(self.worker)
        self.assertIn("workspace_run", self.rt.team._skill_scope(self.task, role)["tools"])
        self.rt.execution_control.change(self.task, "pause", request_id="guidance-pause")
        self.driver.steps[self.worker] = [simple.turn("", ("read_skill", {"skill_id": "explanatory-delivery"})),
                                          simple.turn("", ("reply_to_customer", {"text": "The work remains paused."}))]
        self.rt.team.receive(self.task, f"@{role.name} Explain while paused.")
        self.run_ok()
        self.assert_guide_read()
        for call in self.driver.calls:
            names = {tool["name"] for tool in call["tools"]}
            self.assertIn("read_skill", names)
            self.assertTrue(names.isdisjoint({"workspace_run", "workspace_write", "delegate", "finish_work", "browser_check"}))
        self.assertEqual(self.rt.execution_control.view(self.task)["state"], "paused")
        self.assertEqual(self.events("workspace.tool_started"), [])
        self.assertEqual(list(workspace.iterdir()), [])

    def test_unread_guide_is_not_required_for_reply_or_delivery(self):
        role = self.rt.team._role(self.worker)
        answer = "Hypothesis: [None] may occur; this is not measured output."
        self.driver.steps[self.worker] = [simple.turn("", ("reply_to_customer", {"text": answer}))]
        self.rt.team.receive(self.task, f"@{role.name} Discuss a hypothesis without running anything.")
        self.run_ok()
        self.assertEqual(self.events("msg.said")[-1].payload["text"], answer)
        self.driver.steps[self.manager] = [simple.turn("", ("finish_work", {
            "summary": "Source explanation delivered.", "verification": "No command was run."}))]
        self.rt.team.receive(self.task, "Deliver the explanation without files.")
        self.run_ok()
        self.assertEqual([event.payload["name"] for event in self.events("team.tool_started")],
                         ["reply_to_customer", "finish_work"])
        delivery, = self.events("team.objective_completed")
        self.assertNotIn("handoff", delivery.payload)
        self.assertEqual(delivery.payload["artifacts"], [])
        self.assertEqual(self.events("team.review_requested"), [])
        reply = next(tool for tool in self.driver.calls[0]["tools"] if tool["name"] == "reply_to_customer")
        finish = next(tool for tool in self.driver.calls[-1]["tools"] if tool["name"] == "finish_work")
        self.assertEqual(reply["input_schema"]["required"], ["text"])
        self.assertEqual(set(reply["input_schema"]["properties"]), {"text", "evidence"})
        self.assertEqual(finish["input_schema"]["required"], ["summary", "verification"])
        self.assertEqual(set(finish["input_schema"]["properties"]),
                         {"summary", "verification", "handoff", "artifact_seqs"})


if __name__ == "__main__":
    unittest.main()
