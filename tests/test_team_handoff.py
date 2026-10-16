"""Native public handoffs bind explanations to actual delivered versions."""

from __future__ import annotations

import copy
import io
import json
from pathlib import Path
import unittest
import zipfile

from cater.artifacts import normalize_deliverables
from cater.events import Actor
from cater.team import HOST
from cater.workspaces import WorkspaceService
from cater.verification import _Capture
import test_simple_team


class TeamHandoffTests(unittest.TestCase):
    setUp = test_simple_team.SimpleTeamTests.setUp
    events = test_simple_team.SimpleTeamTests.events
    run_ok = test_simple_team.SimpleTeamTests.run_ok

    def artifact(self, content="def total(values):\n    return sum(values)\n", *, task=None, filename="total.py", actor=None):
        payload, = normalize_deliverables([{"filename": filename, "title": filename, "format": "code",
            "summary": "Actual fixture source, no execution claimed", "content": content}])
        return self.rt.log.emit(type="deliverable.published", task_id=task or self.task,
                                actor=actor or Actor.agent(self.manager, self.rt.organization.leader.role), payload=payload)

    def refs(self):
        return list({a["filename"]: {k: a[k] for k in ("seq", "filename", "content_sha256")}
                     for a in self.rt.task(self.task).deliverables}.values())

    def handoff(self, evidence=None):
        return {"why": "旧实现过滤了负数；对完整输入求和才能保留负数的贡献。",
                "changed": ["移除负数过滤"], "impact": ["原先依赖过滤行为的调用需要复查"],
                "alternatives": ["没有增加配置开关，避免扩大本次修改范围"],
                "reproduce": ["在项目目录运行 python -m unittest -v test_total"],
                "limits": ["此说明本身不会执行命令，也不证明测试已经通过。"], "evidence": evidence or []}

    def normalize(self, value):
        return self.rt.team.delivery.normalize(self.task, value, artifacts=self.refs(), revision=self.rt.team.view(self.task)["revision"])

    def finish(self, handoff=None):
        arguments = {"summary": "求和实现与复查说明已准备好", "verification": "仅核对源码正文，未声称运行测试"}
        if handoff is not None:
            arguments["handoff"] = handoff
        self.driver.steps[self.manager] = [test_simple_team.turn("", ("finish_work", arguments)),
                                          test_simple_team.turn("请按保存的复查步骤核实，尚未运行测试。")]
        self.rt.team.receive(self.task, "按实际材料交付", request_id=f"handoff-finish-{len(self.driver.calls)}")
        self.run_ok()
        return [e.payload["result"] for e in self.events("team.tool_finished") if e.payload["name"] == "finish_work"][-1]

    def receipt(self, *, name="workspace_run", result=None, task=None, changes=None, actor=HOST):
        task = task or self.task
        message = self.rt.log.emit(type="team.message_received", task_id=task, actor=Actor.agent(self.manager, self.rt.organization.leader.role),
            payload={"recipient": self.worker, "kind": "assignment", "revision": 0, "text": "Inspect fixture bytes"})
        run = self.rt.log.emit(type="team.run_started", task_id=task, actor=HOST,
            payload={"run_id": f"handoff-fixture-{message.seq}", "message_seq": message.seq, "role": self.worker,
                     "revision": 0, "control_seq": None})
        start = self.rt.log.emit(type="team.tool_started", task_id=task, actor=HOST, payload={
            "run_id": run.payload["run_id"], "role": self.worker, "message_seq": message.seq, "revision": 0,
            "name": name, "call_id": f"fixture-call-{run.seq}", "arguments": {"command": "python"}})
        finish = self.rt.log.emit(type="team.tool_finished", task_id=task, actor=actor, payload={
            "run_id": run.payload["run_id"], "role": self.worker, "name": name, "started_seq": start.seq,
            "result": result or {"status": "ok", "exit_code": 0, "stderr": {"excerpt": "Ran 5 tests\nOK"}}, **(changes or {})})
        self.rt.log.emit(type="team.run_finished", task_id=task, actor=HOST,
            payload={**run.payload, "status": "completed", "reason": "Fixture closed"})
        self.rt.team._send(task, self.rt.team._role(self.worker), self.manager, "Fixture checked", "report", reply_to=message.seq)
        return finish

    def test_optional_handoff_preserves_old_finish_contract(self):
        self.artifact()
        result = self.finish()
        self.assertEqual(result["status"], "ok")
        event, = self.events("team.objective_completed")
        self.assertNotIn("handoff", event.payload)

    def test_artifact_quote_is_exact_and_bound_to_delivered_version(self):
        source = self.artifact()
        value = self.handoff([{"source_seq": source.seq, "quote": "return sum(values)"}])
        result = self.finish(value)
        self.assertEqual(result["status"], "ok")
        event, = self.events("team.objective_completed")
        note = event.payload["handoff"]
        self.assertEqual(note["version"], 1)
        ref, = note["evidence"]
        self.assertEqual(ref["kind"], "artifact")
        self.assertEqual(ref["source_hash"], source.hash)
        self.assertEqual(ref["content_sha256"], event.payload["artifacts"][0]["content_sha256"])
        self.assertEqual(ref["filename"], "total.py")
        self.assertEqual(self.rt.team.delivery.read(self.task, {})["delivery"]["handoff"], note)
        self.assertFalse(self.rt.team.delivery.read(self.task, {})["historical"])

    def test_native_tools_publish_real_workspace_bytes_with_an_explanatory_delivery(self):
        project = self.root / "project"
        project.mkdir()
        config = self.root / "workspaces.json"
        config.write_text(json.dumps({"workspaces": [{"id": "project", "title": "Project", "root": str(project), "mode": "edit"}]}), encoding="utf-8")
        self.rt.workspaces = WorkspaceService(self.rt, config)
        self.rt.workspaces.bind(self.task, "project", expected_revision=None)
        source = "def total(values):\n    return sum(values)\n"
        def deliver(context, history, tools):
            artifact, = context["artifacts"]
            return test_simple_team.turn("源码已保存，附上原因与复查范围。", ("finish_work", {
                "summary": "求和源码已保存", "verification": "实际写入并读取源码，未执行测试命令",
                "handoff": self.handoff([{"source_seq": artifact["seq"], "quote": "return sum(values)"}])}))
        self.driver.steps[self.manager] = [test_simple_team.turn("", ("workspace_write", {"path": "total.py", "content": source, "expected_sha256": None})),
            test_simple_team.turn("", ("publish_workspace_file", {"path": "total.py", "summary": "实际源码", "format": "code"})),
            deliver, test_simple_team.turn("源码已准备好，测试尚未运行，可展开复查说明。")]
        self.rt.team.receive(self.task, "修复求和并说明原因与复查方法", request_id="actual-workspace-handoff")
        self.run_ok()
        self.assertEqual((project / "total.py").read_text(encoding="utf-8"), source)
        artifact, = self.rt.task(self.task).deliverables
        self.assertEqual(Path(artifact["local_path"]).read_text(encoding="utf-8"), source)
        completion, = self.events("team.objective_completed")
        self.assertEqual(completion.payload["handoff"]["evidence"][0]["content_sha256"], artifact["content_sha256"])
        self.assertFalse([e for e in self.events("team.tool_started") if e.payload["name"] == "workspace_run"])

    def test_wrong_quote_is_error_without_partial_completion(self):
        source = self.artifact()
        result = self.finish(self.handoff([{"source_seq": source.seq, "quote": "all tests passed"}]))
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["code"], "invalid_handoff")
        self.assertIn("原文不符", result["message"])
        self.assertEqual(self.events("team.objective_completed"), [])
        self.assertEqual(self.rt.team.view(self.task)["status"], "running")

    def test_latest_same_filename_only_and_foreign_group_are_not_citation_sources(self):
        old = self.artifact("# old source\n", filename="note.md")
        self.artifact("# current source\n", filename="note.md")
        other = self.rt.create_task("Foreign group")
        foreign = self.artifact("Private other group text", task=other)
        for event, quote in ((old, "old source"), (foreign, "Private other group text")):
            with self.subTest(seq=event.seq), self.assertRaises(ValueError):
                self.normalize(self.handoff([{"source_seq": event.seq, "quote": quote}]))

    def test_forged_artifact_hash_or_system_author_cannot_be_cited_as_delivered_bytes(self):
        source = self.artifact(actor=Actor.system("untrusted"))
        with self.assertRaises(ValueError):
            self.normalize(self.handoff([{"source_seq": source.seq, "quote": "return sum(values)"}]))
        self.rt.log.emit(type="deliverable.published", task_id=self.task, actor=Actor.agent(self.manager, self.rt.organization.leader.role), payload={
            **source.payload, "content_sha256": "0" * 64})
        latest = self.events("deliverable.published")[-1]
        with self.assertRaises(ValueError):
            self.normalize(self.handoff([{"source_seq": latest.seq, "quote": "return sum(values)"}]))

    def test_real_bound_receipt_remains_historical_execution_not_artifact_approval(self):
        receipt = self.receipt()
        self.artifact()
        value = self.normalize(self.handoff([{"source_seq": receipt.seq, "quote": "Ran 5 tests\nOK"}]))
        ref, = value["evidence"]
        self.assertEqual(ref["kind"], "tool_receipt")
        self.assertEqual(ref["exit_code"], 0)
        self.assertEqual(ref["tool"], "workspace_run")
        self.assertEqual(ref["status"], "ok")
        self.assertNotIn("approved", ref)

    def test_failed_receipt_and_incomplete_output_do_not_turn_into_success(self):
        receipt = self.receipt(result={"status": "error", "complete": False, "exit_code": 2,
                                       "stderr": {"excerpt": "Missing browser environment"}})
        ref, = self.normalize(self.handoff([{"source_seq": receipt.seq, "quote": "Missing browser environment"}]))["evidence"]
        self.assertEqual(ref["status"], "error")
        self.assertEqual(ref["exit_code"], 2)
        self.assertFalse(ref["complete"])

    def test_completed_capture_preserves_truncated_preview_separately(self):
        capture = _Capture()
        capture.drain(io.BytesIO(b"visible-marker\n" + b"x" * 30000))
        output = capture.summary(record_output=True)
        self.assertTrue(output["complete"])
        self.assertTrue(output["truncated"])
        receipt = self.receipt(result={"status": "ok", "exit_code": 0, "complete": True, "stdout": output})
        ref, = self.normalize(self.handoff([{"source_seq": receipt.seq, "quote": "visible-marker"}]))["evidence"]
        self.assertTrue(ref["complete"])
        self.assertTrue(ref["truncated"])

    def test_global_size_diagnostic_reaches_model_and_does_not_commit_completion(self):
        value = {**self.handoff(), "changed": ["a" * 1000] * 12, "impact": ["b" * 1000] * 12}
        result = self.finish(value)
        self.assertEqual(result["code"], "invalid_handoff")
        self.assertIn("24KB", result["message"])
        self.assertEqual(self.events("team.objective_completed"), [])
        schema = next(t for t in self.rt.team._schemas(self.rt.organization.leader, None) if t["name"] == "finish_work")
        self.assertIn("24KB", schema["input_schema"]["properties"]["handoff"]["description"])

    def test_untrusted_and_mismatched_receipts_are_not_sources(self):
        cases = [(Actor.system("other"), {}), (HOST, {"run_id": "other-run"}),
                 (HOST, {"role": self.manager}), (HOST, {"name": "workspace_write"}),
                 (HOST, {"started_seq": True}), (HOST, {"call_id": "wrong-call"})]
        for actor, changes in cases:
            with self.subTest(actor=actor, changes=changes):
                receipt = self.receipt(actor=actor, changes=changes)
                with self.assertRaises(ValueError):
                    self.normalize(self.handoff([{"source_seq": receipt.seq, "quote": "Ran 5 tests"}]))

    def test_receipt_from_previous_objective_is_not_current_execution_evidence(self):
        receipt = self.receipt()
        self.rt.log.emit(type="team.objective_updated", task_id=self.task, actor=HOST, payload={"goal": "New implementation"})
        with self.assertRaises(ValueError):
            self.normalize(self.handoff([{"source_seq": receipt.seq, "quote": "Ran 5 tests"}]))

    def test_only_current_peer_review_feedback_can_be_cited_and_is_not_execution(self):
        artifact = self.artifact()
        request = self.rt.log.emit(type="team.review_requested", task_id=self.task, actor=HOST, payload={
            "root_seq": None, "revision": 0, "author": self.manager, "reviewer": self.worker,
            "instruction": "Inspect exact source", "artifacts": self.refs(), "run_id": "fixture-review"})
        decision = self.rt.log.emit(type="team.review_recorded", task_id=self.task, actor=HOST, payload={
            "root_seq": request.seq, "review_seq": request.seq, "reviewer": self.worker,
            "verdict": "approved", "feedback": "Read this source; did not run integration tests"})
        ref, = self.normalize(self.handoff([{"source_seq": decision.seq, "quote": "did not run integration tests"}]))["evidence"]
        self.assertEqual(ref["kind"], "peer_review")
        self.assertNotIn("exit_code", ref)
        self.artifact("Changed after review", filename=artifact.payload["filename"])
        with self.assertRaises(ValueError):
            self.normalize(self.handoff([{"source_seq": decision.seq, "quote": "did not run integration tests"}]))

    def test_shape_size_and_undeclared_private_reasoning_are_rejected(self):
        cases = [None, [], {}, {**self.handoff(), "why": ""}, {**self.handoff(), "why": "x" * 4001},
                 {**self.handoff(), "changed": "Not a list"}, {**self.handoff(), "limits": [""]},
                 {**self.handoff(), "reproduce": ["Step"] * 13}, {**self.handoff(), "thinking": "private"},
                 {**self.handoff(), "evidence": [{"source_seq": True, "quote": "Text"}]},
                 {**self.handoff(), "evidence": [{"source_seq": 1, "quote": "Text", "kind": "tool_receipt"}]},
                 {**self.handoff(), "changed": ["中" * 1000] * 12}]
        for value in cases:
            with self.subTest(value_type=type(value).__name__), self.assertRaises(ValueError):
                self.normalize(value)

    def test_duplicate_quotes_are_not_duplicated(self):
        source = self.artifact()
        ref = {"source_seq": source.seq, "quote": "return sum(values)"}
        self.assertEqual(len(self.normalize(self.handoff([ref, ref]))["evidence"]), 1)

    def test_read_delivery_is_version_frozen_after_goal_or_file_changes(self):
        source = self.artifact()
        self.finish(self.handoff([{"source_seq": source.seq, "quote": "return sum(values)"}]))
        saved = copy.deepcopy(self.rt.team.delivery.read(self.task, {})["delivery"])
        self.rt.log.emit(type="team.objective_updated", task_id=self.task, actor=HOST, payload={"goal": "Other implementation"})
        self.artifact("Different current bytes")
        read = self.rt.team.delivery.read(self.task, {"delivery_seq": saved["seq"]})
        self.assertEqual(read["delivery"], saved)
        self.assertTrue(read["historical"])
        self.assertTrue(self.rt.team.delivery.index(self.task)["historical"])
        self.assertEqual(self.rt.team.delivery.index(self.task)["seq"], saved["seq"])

    def test_duplicate_finish_does_not_rewrite_existing_handoff(self):
        self.artifact()
        self.finish(self.handoff())
        original = copy.deepcopy(self.events("team.objective_completed")[0].payload)
        self.finish({**self.handoff(), "why": "Do not replace a saved explanation"})
        self.assertEqual([e.payload for e in self.events("team.objective_completed")], [original])

    def test_download_keeps_explanation_with_frozen_files_without_changing_source(self):
        artifact = self.artifact()
        value = self.handoff([{"source_seq": artifact.seq, "quote": "return sum(values)"}])
        self.finish(value)
        with zipfile.ZipFile(io.BytesIO(self.rt.team.archive(self.task))) as archive:
            self.assertEqual(archive.read("total.py").decode(), artifact.payload["content"])
            note = archive.read("__cater__/交付说明.md").decode()
            self.assertIn(value["why"], note)
            self.assertIn(value["reproduce"][0], note)
            self.assertIn(artifact.hash, note)
            self.assertNotIn("\"source_seq\"", note)
        self.rt.log.emit(type="team.objective_updated", task_id=self.task, actor=HOST, payload={"goal": "A new task"})
        with zipfile.ZipFile(io.BytesIO(self.rt.team.archive(self.task))) as archive:
            self.assertNotIn("__cater__/交付说明.md", archive.namelist())

    def test_download_does_not_hide_failure_when_quote_is_from_a_successful_subcheck(self):
        receipt = self.receipt(result={"status": "error", "exit_code": 2, "complete": True,
            "stdout": {"excerpt": "SMOKE PASSED", "complete": True, "truncated": True},
            "stderr": {"excerpt": "INTEGRATION FAILED", "complete": True}})
        self.artifact()
        result = self.finish(self.handoff([{"source_seq": receipt.seq, "quote": "SMOKE PASSED"}]))
        self.assertEqual(result["status"], "ok")
        with zipfile.ZipFile(io.BytesIO(self.rt.team.archive(self.task))) as archive:
            note = archive.read("__cater__/交付说明.md").decode()
            self.assertIn("SMOKE PASSED", note)
            self.assertIn("记录状态：返回错误", note)
            self.assertIn("退出码：2", note)
            self.assertIn("回执采集完整", note)
            self.assertIn("正文仅保留部分内容", note)

    def test_post_delivery_question_reads_saved_explanation_without_redelegation(self):
        self.artifact()
        self.finish(self.handoff())
        delivered = self.events("team.objective_completed")[0]
        for i in range(70):
            self.rt.log.emit(type="msg.said", task_id=self.task, actor=self.rt._leader_actor(), payload={"text": f"Later discussion {i}"})
        def read(context, history, tools):
            self.assertEqual(context["delivery_index"]["seq"], delivered.seq)
            self.assertTrue(context["delivery_index"]["has_handoff"])
            self.assertFalse(any("旧实现过滤了负数" in row["text"] for row in context["conversation"]))
            return test_simple_team.turn("先读原交付。", ("read_delivery", {}))
        def explain(context, history, tools):
            result = json.loads(history[-1]["content"])
            self.assertEqual(result["delivery"]["handoff"]["why"], self.handoff()["why"])
            return test_simple_team.turn("在项目目录执行保存的测试命令；原记录没有实际运行测试。")
        self.driver.steps[self.manager] = [read, explain]
        self.rt.team.receive(self.task, "为什么这样改，怎么运行", request_id="why-and-how-after-delivery")
        self.run_ok()
        self.assertEqual(len(self.events("team.objective_completed")), 1)
        self.assertEqual(self.events("team.objective_updated"), [])
        self.assertFalse([e for e in self.events("team.message_received") if e.payload.get("kind") == "assignment"])

    def test_read_rejects_missing_foreign_and_invalid_ids(self):
        with self.assertRaises(ValueError):
            self.rt.team.delivery.read(self.task, {})
        self.artifact()
        self.finish()
        seq = self.events("team.objective_completed")[0].seq
        other = self.rt.create_task("Other group")
        for task, arguments in ((other, {"delivery_seq": seq}), (self.task, {"delivery_seq": True}),
                                (self.task, {"delivery_seq": 0}), (self.task, {"delivery_seq": seq + 9999})):
            with self.subTest(task=task, arguments=arguments), self.assertRaises(ValueError):
                self.rt.team.delivery.read(task, arguments)


if __name__ == "__main__":
    unittest.main()
