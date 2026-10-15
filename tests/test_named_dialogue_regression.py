"""Offline regressions for the 389/394/395/397 named-dialogue incident."""

from __future__ import annotations

import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cater.anthropic import AnthropicMessagesDriver
from cater.artifacts import normalize_deliverables
from cater.artifact_store import ArtifactStoreError
from cater.conversation import ConversationService
from cater.drivers import DriverResponse, ScriptedDriver
from cater.events import Actor
from cater.explanations import ExplanationService
from cater.room_dialogue import addressed_role
from cater.runtime import Runtime
from test_work import WorkDriver, item


ROOT = Path(__file__).resolve().parents[1]


class OfflineDriver:
    name = "offline"

    def __init__(self):
        self.calls = []
        self.outputs = []
        self.after = None

    def complete(self, **call):
        self.calls.append(copy.deepcopy(call))
        if self.after:
            self.after()
        output = self.outputs.pop(0)
        if callable(output):
            output = output(call)
        return DriverResponse(json.dumps(output, ensure_ascii=False), 17, {"token_usage": "reported"})


class NamedDialogueRegressionTests(unittest.TestCase):
    def setUp(self):
        (ROOT / ".cater").mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="nd-", dir=ROOT / ".cater")
        self.addCleanup(temporary.cleanup)
        config = json.loads((ROOT / "cater/packs/default_roles.json").read_text(encoding="utf-8"))
        config["organization"]["leader"]["driver"] = "offline"
        for role in config["roles"]:
            role["driver"] = "offline"
        roles = Path(temporary.name) / "roles.json"
        roles.write_text(json.dumps(config), encoding="utf-8")
        self.driver = OfflineDriver()
        self.rt = Runtime(Path(temporary.name) / "offline.db", roles_path=roles,
                          drivers={"offline": self.driver})
        self.addCleanup(self.rt.close)
        self.task = self.rt.create_task("制作五子棋", ["能重新开始"])
        self.service = ConversationService(self.rt)

    def suggest(self, action="discuss", *, quote=None, **fields):
        return {"reply": "先说明我的方案。", "action": action,
                "task_id": None if action in {"discuss", "new_task"} else self.task,
                "goal": None, "acceptance": None, "quote": quote, **fields}

    def resolve(self, message, output=None, *, continue_work=True):
        self.driver.outputs.append(output or self.suggest())
        received = self.service.receive(self.task, message, continue_work=continue_work)
        return received, self.service.resolve_next(self.task)

    def events(self, kind):
        return [event for event in self.rt.log.read(task_id=self.task) if event.type == kind]

    def test_latest_named_message_selects_actual_driver_role_and_public_reply(self):
        self.resolve("@三角 你为什么不做", self.suggest(reply="旧话题：三角等待前置验收。"))
        scope = self.rt.work.scope(self.task)
        _, result = self.resolve("@月亮 你说说你的思路吧",
                                 self.suggest(reply="我的方案是先明确玩法，再把胜负与重开写成可检查的要求。"))
        call = self.driver.calls[-1]
        self.assertEqual(call["context"]["role"]["id"], "product")
        self.assertEqual(call["context"]["conversation"]["speaker"]["id"], "product")
        self.assertEqual(call["context"]["conversation"]["message"], "@月亮 你说说你的思路吧")
        self.assertEqual(result["speaker_id"], "product")
        self.assertIn("我的方案", result["reply"])
        self.assertNotIn("三角", result["reply"])
        self.assertIn("不能继续回答上一条问题", call["prompt"])
        self.assertIn("不索取或输出私有思维链", call["prompt"])
        self.assertEqual(call["context"]["context_scope"], "request")
        self.assertEqual(result["next_action"], "none")
        self.assertEqual(self.rt.work.scope(self.task), scope)
        self.assertEqual(self.events("human.commanded"), [])
        self.assertEqual(self.events("question.asked"), [])
        self.assertIsNone(self.service.resolve_next(self.task))
        self.assertEqual(len(self.driver.calls), 2)

    def test_single_mentions_aliases_unknown_multiple_and_email_boundaries(self):
        cases = [("@月亮 你好", "product"), ("@PRODUCT：你好", "product"),
                 ("＠月亮 你好", "product"), ("@月亮 @product 你好", "product"),
                 ("@店长 你好", "store_manager"), ("@月亮 @三角 聊聊", "store_manager"),
                 ("@月亮@三角 聊聊", "store_manager"), ("@月亮@不存在 聊聊", "store_manager"),
                 ("@不存在 你好", "store_manager"), ("@月亮 @不存在 你好", "store_manager"),
                 ("someone@product.example 你好", "store_manager"),
                 ("@product_extra 你好", "store_manager"), ("月亮 你好", "store_manager")]
        for message, expected in cases:
            with self.subTest(message=message):
                _, result = self.resolve(message)
                self.assertEqual(result["speaker_id"], expected)
                self.assertEqual(self.driver.calls[-1]["context"]["role"]["id"], expected)
                self.assertEqual(result["next_action"], "none")
        roles = [{"id": "one", "name": "同名"}, {"id": "two", "name": "同名"}]
        self.assertIsNone(addressed_role("@同名 你好", roles))

    def test_mention_does_not_override_semantic_change_or_full_original(self):
        message = "@月亮 说说思路，另外目标改为网页版围棋，保留重开按钮"
        received, result = self.resolve(message, self.suggest("update_task", quote="目标改为网页版围棋",
                                                           goal="模型改写的目标"))
        command = self.events("human.commanded")[0]
        self.assertEqual(command.payload["instruction"], message)
        self.assertEqual(command.payload["conversation_seq"], received.seq)
        self.assertEqual(result["action"], "update_task")
        self.assertEqual(result["next_action"], "run")
        self.assertNotIn("goal", command.payload)
        self.assertEqual(self.events("question.asked"), [])
        self.assertEqual(len(self.driver.calls), 1)

    def test_missing_member_model_keeps_manager_routing_and_named_question(self):
        from dataclasses import replace

        self.rt.drivers["demo_member"] = ScriptedDriver("demo_member", [])
        self.rt.roles = [replace(role, driver="demo_member") if role.id == "product" else role
                         for role in self.rt.roles]
        _, result = self.resolve("@月亮 解释实际成果", self.suggest("explain_task", quote="解释实际成果"))
        self.assertEqual(result["speaker_id"], "store_manager")
        speaker = self.driver.calls[-1]["context"]["conversation"]["speaker"]
        self.assertEqual(speaker["requested_role_id"], "product")
        self.assertEqual(self.events("question.asked")[0].payload["role_id"], "product")
        self.rt.questions.answer_next(self.task)
        self.assertEqual(self.rt.questions.view(self.task)[0]["status"], "blocked")
        self.assertEqual(len(self.driver.calls), 1)

    def test_named_controls_keep_semantic_action_and_current_message_quote(self):
        for message, action, expected in (("@月亮 暂停任务", "pause_task", "paused"),
                                          ("@月亮 继续任务", "resume_task", "running"),
                                          ("@月亮 取消任务", "cancel_task", "cancelled")):
            with self.subTest(action=action):
                _, result = self.resolve(message, self.suggest(action, quote=message))
                self.assertEqual(result["action"], action)
                self.assertEqual(self.rt.execution_control.view(self.task)["state"], expected)
        self.assertEqual(self.events("question.asked"), [])

    def test_explanation_time_pause_change_marks_old_answer_historical(self):
        asked = self.rt.questions.ask(self.task, "@月亮 说明当前要求", request_id="control-version-question")
        self.assertNotIn("control_seq", asked.payload["basis"]["anchor"])
        self.rt.execution_control.change(self.task, "pause", request_id="pause-after-question")
        self.driver.outputs.append({"status": "insufficient", "answer": "提问时没有执行记录。",
                                    "evidence": [], "limits": ["仅对应提问时版本。"]})
        self.rt.questions.answer_next(self.task)
        row = self.rt.questions.view(self.task)[0]
        self.assertTrue(row["outdated"])
        self.assertEqual(row["status"], "insufficient")
        self.assertEqual(self.driver.calls[-1]["context"]["work"]["state"]["execution_control"]["state"], "running")
        self.assertEqual(self.rt.execution_control.view(self.task)["state"], "paused")

    def test_mentioned_explanation_waits_for_source_validation_without_early_claims(self):
        message = "@三角 你为什么不做"
        _, result = self.resolve(message, self.suggest("explain_task", quote="你为什么不做",
                                                     reply="我已运行全部测试且验收通过。"))
        self.assertNotIn("已运行", result["reply"])
        question = self.events("question.asked")[0]
        self.assertEqual(question.payload["role_id"], "test")
        self.assertEqual(result["next_action"], "explain")
        self.assertEqual(self.events("question.started"), [])
        self.driver.outputs.append({"status": "insufficient", "answer": "现在没有我的执行记录，不能声称已经验证。",
                                    "evidence": [], "limits": ["尚未执行检查。"]})
        self.rt.questions.answer_next(self.task)
        self.assertEqual(self.rt.questions.view(self.task)[0]["status"], "insufficient")
        self.assertEqual(self.driver.calls[-1]["context"]["role"]["id"], "test")
        self.assertEqual(self.events("human.commanded"), [])
        self.assertFalse(self.rt.questions.answer_next(self.task))
        self.assertEqual(len(self.driver.calls), 2)

    def test_paused_named_chat_and_explanation_do_not_resume_execution(self):
        self.rt.execution_control.change(self.task, "pause", request_id="pause-named-chat")
        control = self.rt.execution_control.view(self.task)
        _, result = self.resolve("@月亮 说说思路")
        self.assertEqual(result["speaker_id"], "product")
        self.assertEqual(result["next_action"], "none")
        self.rt.questions.ask(self.task, "@月亮 你的公开操作依据是什么", request_id="paused-explanation")
        self.driver.outputs.append({"status": "insufficient", "answer": "任务暂停中；我可以说明拟采用的方法。",
                                    "evidence": [], "limits": ["尚未执行。"]})
        self.rt.questions.answer_next(self.task)
        self.assertEqual(self.rt.questions.view(self.task)[0]["status"], "insufficient")
        self.assertEqual(self.driver.calls[-1]["context"]["work"]["state"]["execution_control"]["state"], "paused")
        self.assertEqual(self.rt.execution_control.view(self.task), control)
        self.assertEqual(self.events("human.commanded"), [])

    def test_goal_or_pause_changed_during_call_cannot_apply_named_change(self):
        for change in (lambda: self.rt.command(self.task, "新目标", goal="另一个目标"),
                       lambda: self.rt.execution_control.change(self.task, "pause", request_id="pause-during-named")):
            with self.subTest(change=change):
                self.driver.after = change
                _, result = self.resolve("@月亮 增加悔棋", self.suggest("update_task", quote="增加悔棋"))
                self.assertEqual(result["status"], "outdated")
                self.assertEqual(result["next_action"], "none")
                self.assertFalse(any(e.payload.get("instruction") == "@月亮 增加悔棋"
                                     for e in self.events("human.commanded")))
        self.driver.after = None

    def test_current_unaccepted_review_is_available_as_claim_not_execution_proof(self):
        self.rt.drivers["offline"] = WorkDriver()
        self.rt.plan_work(self.task, [item("implementation", local=True),
                                     item("validation", owner="test", reviewer="cabinet_quality", deps=["implementation"])])
        self.rt.collaborate(self.task)
        work = self.rt.work.view(self.task)
        implementation = next(i for i in work["items"] if i["id"] == "implementation")
        self.assertEqual(implementation["status"], "submitted")
        asked = self.rt.questions.ask(self.task, "@三角 你为什么不做", request_id="blocked-explanation")
        sources = {row["seq"]: row["kind"] for row in asked.payload["basis"]["source_seqs"]}
        self.assertEqual(sources[implementation["review_seq"]], "member_review")
        self.assertNotIn("execution_record", sources.values())
        blocked = next(i for i in asked.payload["basis"]["state"]["items"] if i["id"] == "validation")
        self.assertEqual(blocked["owner"], "test")
        self.assertEqual(blocked["depends_on"], ["implementation"])
        self.assertEqual(blocked["status"], "blocked")

    def test_bad_contract_has_safe_diagnostics_and_never_replays(self):
        self.rt.questions.ask(self.task, "@三角 你为什么不做", request_id="wrong-contract-question")
        self.driver.outputs.append({"reply": "不要泄露这个正文", "private-secret-field": "secret"})
        self.rt.questions.answer_next(self.task)
        row = self.rt.questions.view(self.task)[0]
        self.assertEqual(row["status"], "invalid")
        self.assertIn("answer", row["reason"])
        self.assertIn("额外字段数 2", row["reason"])
        self.assertNotIn("secret", row["reason"])
        self.assertEqual(row["tokens"], 17)
        self.assertFalse(self.rt.questions.answer_next(self.task))
        self.assertEqual(len(self.driver.calls), 1)

    def test_http_wire_contains_speaker_current_question_and_explicit_contract(self):
        self.rt.questions.ask(self.task, "@月亮 你说说你的思路吧", request_id="http-named-question")
        self.rt.drivers["offline"] = AnthropicMessagesDriver("offline", endpoint="https://example.invalid/v1/messages",
                                                           model="offline-fixture", api_key_env="CATER_OFFLINE_FIXTURE_KEY")
        payload = {"status": "insufficient", "answer": "我会先明确玩法范围，再列出可核对的验收要求。",
                   "evidence": [], "limits": ["这是拟采用的方法，尚无执行记录。"]}
        response = {"type": "message", "stop_reason": "end_turn",
                    "content": [{"type": "text", "text": json.dumps(payload)}],
                    "usage": {"input_tokens": 7000, "output_tokens": 476}}
        with patch.dict("os.environ", {"CATER_OFFLINE_FIXTURE_KEY": "offline-only"}), \
                patch("cater.anthropic._open_api_request", return_value=io.BytesIO(json.dumps(response).encode())) as transport:
            self.rt.questions.answer_next(self.task)
        transport.assert_called_once()
        wire = json.loads(transport.call_args.args[0].data)
        prompt = wire["messages"][0]["content"]
        self.assertIn('"name": "月亮"', prompt)
        self.assertIn('"question": "@月亮 你说说你的思路吧"', prompt)
        self.assertIn("exactly status, answer, evidence, limits, not the schema itself", prompt)
        self.assertIn("never private chain-of-thought", prompt)
        self.assertNotIn("tools", wire)
        row = self.rt.questions.view(self.task)[0]
        self.assertEqual(row["status"], "insufficient")
        self.assertEqual(row["speaker_id"], "product")
        self.assertEqual(row["tokens"], 7476)
        self.assertFalse(self.rt.questions.answer_next(self.task))

    def test_contract_repair_never_relaxes_exact_evidence_or_accepts_actions(self):
        source = {"seq": 3, "kind": "member_artifact", "text": "公开的需求说明"}
        valid = {"status": "explained", "answer": "这是需求说明中的公开依据。",
                 "evidence": [{"source_seq": 3, "quote": "公开的需求说明"}], "limits": []}
        self.assertEqual(ExplanationService._validate(valid, [source])["status"], "answered")
        for change in ({"evidence": []}, {"evidence": [{"source_seq": 4, "quote": source["text"]}]},
                       {"evidence": [{"source_seq": 3, "quote": "已经运行通过"}]},
                       {"evidence": [{"source_seq": True, "quote": source["text"]}]},
                       {"action": "resume_task"}, {"reasoning": "private"}, {"deliverables": []}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                ExplanationService._validate({**valid, **change}, [source])

    def submitted_artifact(self):
        self.rt._artifact_root = Path(self.rt.log.path).parent / "a"
        contract = item("requirements", owner="product", reviewer="cabinet_product")
        contract["objective"] = "说明五子棋玩法、胜负条件和重开行为"
        self.rt.plan_work(self.task, [contract])
        claim = self.rt.work.claim(self.task, "requirements")
        payload = normalize_deliverables([{"title": "玩法需求", "summary": "明确玩法范围与验收要求",
            "filename": "requirements.md", "format": "markdown", "content": "黑棋先行；五子连珠获胜；可以重新开始。"}])[0]
        artifact = self.rt.log.emit(type="deliverable.published", task_id=self.task, actor=Actor.agent("product", "worker"),
            payload={**payload, "work_item_id": "requirements", "attempt_id": claim.payload["attempt_id"],
                     "local_path": "C:/untrusted/claimed.md", "materialized": True})
        self.rt.work.submit(self.task, claim, [artifact.seq])
        return artifact

    def test_subject_contract_and_artifact_follow_history_and_current_question_is_last(self):
        artifact = self.submitted_artifact()
        self.resolve("@三角 你为什么不做", self.suggest(reply="三角还在等待前置验收。"))
        self.resolve("@月亮 你说说你的思路吧")
        call = self.driver.calls[-1]
        conversation = call["context"]["conversation"]
        self.assertEqual(list(conversation)[-2:], ["subject", "message"])
        subject = conversation["subject"]
        self.assertEqual(subject["id"], "product")
        self.assertEqual(subject["contracts"][0]["objective"], "说明五子棋玩法、胜负条件和重开行为")
        self.assertTrue(subject["contracts"][0]["criteria"])
        self.assertEqual(subject["artifacts"][0]["seq"], artifact.seq)
        self.assertEqual(subject["artifacts"][0]["summary"], "明确玩法范围与验收要求")
        self.assertEqual(json.loads(call["prompt"].rsplit("\n", 1)[1]), conversation)
        self.assertLess(call["prompt"].index('"history"'), call["prompt"].rindex('"subject"'))
        self.assertLess(call["prompt"].rindex('"subject"'), call["prompt"].rindex('"message"'))
        self.assertNotIn("local_path", json.dumps(subject))

    def test_named_status_starts_with_member_state_and_labels_overall_task(self):
        self.rt.plan_work(self.task, [item("implementation"),
            item("validation", owner="test", reviewer="cabinet_quality", deps=["implementation"])])
        _, result = self.resolve("@三角 你为什么还没有开始？", self.suggest("status_task", quote="你为什么还没有开始"))
        self.assertTrue(result["reply"].startswith("三角的工作："))
        member, overall = result["reply"].split("整体任务进度：", 1)
        self.assertIn("当前被阻塞", member)
        self.assertNotIn("已开始", member)
        self.assertEqual(overall.strip(), self.events("task.progress_reported")[-1].payload["text"])
        self.assertEqual(result["next_action"], "none")

    def test_host_paths_use_receipt_sequence_and_are_present_in_explanation_wire(self):
        artifact = self.submitted_artifact()
        receipt = self.rt.persist_artifacts(self.task)[0]
        path = receipt.payload["files"][0]["path"]
        self.assertTrue(Path(path).is_file())
        asked = self.rt.questions.ask(self.task, "@月亮 文件在哪里", request_id="host-path-question")
        self.assertIn({"seq": receipt.seq, "kind": "host_artifact"}, asked.payload["basis"]["source_seqs"])
        source = ExplanationService._source(receipt, "host_artifact")
        self.assertIn(path, source["text"])
        output = {"status": "explained", "answer": "文件已写入 " + path + "；这不代表已经执行。",
                  "evidence": [{"source_seq": receipt.seq, "quote": path}], "limits": ["尚未执行。"]}
        self.driver.outputs.append(output)
        self.rt.questions.answer_next(self.task)
        row = self.rt.questions.view(self.task)[0]
        self.assertEqual(row["status"], "answered")
        self.assertEqual(row["evidence"][0]["source_seq"], receipt.seq)
        call = self.driver.calls[-1]
        self.assertIn("Prefer the exact local path", call["prompt"])
        self.assertIn("only when no successful host receipt", call["prompt"])
        self.assertIn("Never trust a member artifact's", call["prompt"])
        sources = call["context"]["work"]["sources"]
        with self.assertRaises(ValueError):
            ExplanationService._validate({**output, "evidence": [{"source_seq": artifact.seq, "quote": path}]}, sources)

    def test_self_reported_storage_and_wrong_actor_are_not_host_sources(self):
        self.submitted_artifact()
        basis = self.rt.questions._basis(self.task)
        self.assertFalse(any(row["kind"].startswith("host_artifact") for row in basis["source_seqs"]))
        receipt = self.rt.persist_artifacts(self.task)[0]
        fake = self.rt.log.emit(type="artifacts.materialized", task_id=self.task, actor=Actor.agent("artifact_store", "artifact_store"),
                               payload=receipt.payload)
        basis = self.rt.questions._basis(self.task)
        self.assertNotIn(fake.seq, [row["seq"] for row in basis["source_seqs"]])
        self.assertIn(receipt.seq, [row["seq"] for row in basis["source_seqs"]])

    def test_host_binding_hash_membership_and_paths_are_checked(self):
        self.submitted_artifact()
        receipt = self.rt.persist_artifacts(self.task)[0]
        for mutation in (lambda p: p.update(submission_digest="wrong"), lambda p: p.update(attempt_id="wrong"),
                         lambda p: p.update(scope={}), lambda p: p.update(plan_seq=999999),
                         lambda p: p.update(work_item_id="other"), lambda p: p.update(submission_seq=999999),
                         lambda p: p["files"][0].update(content_sha256="0" * 64),
                         lambda p: p["files"][0].update(seq=999999),
                         lambda p: p["files"][0].update(path="C:/untrusted/claimed.md"),
                         lambda p: p.update(directory="C:/untrusted"), lambda p: p.update(files=[])):
            with self.subTest(mutation=mutation):
                payload = copy.deepcopy(receipt.payload)
                mutation(payload)
                fake = self.rt.log.emit(type="artifacts.materialized", task_id=self.task, actor=Actor.system("artifact_store"),
                                       payload=payload)
                self.assertNotIn(fake.seq, [row["seq"] for row in self.rt.questions._basis(self.task)["source_seqs"]])
        other = self.rt.create_task("另一个任务")
        fake = self.rt.log.emit(type="artifacts.materialized", task_id=other, actor=Actor.system("artifact_store"), payload=receipt.payload)
        self.assertNotIn(fake.seq, [row["seq"] for row in self.rt.questions._basis(self.task)["source_seqs"]])

    def test_failed_host_receipt_supersedes_success_and_marks_frozen_answer_historical(self):
        self.submitted_artifact()
        success = self.rt.persist_artifacts(self.task)[0]
        self.rt.questions.ask(self.task, "文件在哪里", request_id="before-host-failure")
        with patch("cater.artifact_store.materialize", side_effect=ArtifactStoreError("disk unavailable")):
            failure = self.rt.persist_artifacts(self.task)[0]
        self.assertEqual(failure.type, "artifacts.materialization_failed")
        basis = self.rt.questions._basis(self.task)
        self.assertNotIn(success.seq, [row["seq"] for row in basis["source_seqs"]])
        self.assertIn({"seq": failure.seq, "kind": "host_artifact_failure"}, basis["source_seqs"])
        self.assertTrue(self.rt.questions.view(self.task)[0]["outdated"])
        self.driver.outputs.append({"status": "insufficient", "answer": "仅能说明提问时的落盘记录。", "evidence": [], "limits": []})
        self.rt.questions.answer_next(self.task)
        frozen_sources = self.driver.calls[-1]["context"]["work"]["sources"]
        self.assertIn(success.seq, [row["seq"] for row in frozen_sources])
        self.assertNotIn(failure.seq, [row["seq"] for row in frozen_sources])

    def test_task_changes_and_rework_exclude_old_host_receipts(self):
        self.submitted_artifact()
        receipt = self.rt.persist_artifacts(self.task)[0]
        self.rt.work.rework(self.task, "requirements", "新的需求")
        self.assertNotIn(receipt.seq, [row["seq"] for row in self.rt.questions._basis(self.task)["source_seqs"]])
        self.rt.command(self.task, "新目标", goal="不同目标")
        self.assertFalse(any(row["kind"].startswith("host_artifact")
                             for row in self.rt.questions._basis(self.task)["source_seqs"]))


if __name__ == "__main__":
    unittest.main()
