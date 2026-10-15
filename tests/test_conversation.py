from __future__ import annotations

import copy
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from cater.conversation import ConversationService, SYSTEM
from cater.drivers import DriverResponse
from cater.events import Actor
from cater.explanations import ExplanationService
from cater.runtime import Runtime
from cater.work import WorkConflict


class DialogueDriver:
    name = "dialogue_test"

    def __init__(self):
        self.calls = []
        self.output = None
        self.callback = None

    def complete(self, *, role, prompt, context):
        self.calls.append(copy.deepcopy({"role": role, "prompt": prompt, "context": context}))
        if self.callback:
            self.callback()
        if isinstance(self.output, Exception):
            raise self.output
        text = self.output if isinstance(self.output, str) else json.dumps(self.output, ensure_ascii=False)
        return DriverResponse(text, 17, {"token_usage": "reported"})


class ConversationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        config = json.loads((Path(__file__).resolve().parents[1] / "cater/packs/default_roles.json").read_text(encoding="utf-8"))
        config["organization"]["leader"]["driver"] = "dialogue_test"
        roles = root / "roles.json"
        roles.write_text(json.dumps(config), encoding="utf-8")
        self.driver = DialogueDriver()
        self.db, self.roles = root / "test.db", roles
        self.rt = Runtime(self.db, roles_path=roles, drivers={"dialogue_test": self.driver})
        self.addCleanup(self.rt.close)
        self.task = self.rt.create_task("制作五子棋", ["保留网页成果"])
        self.service = ConversationService(self.rt)
        self.scope = self.rt.work.scope(self.task)
        self.suggest("discuss")

    def suggest(self, action, quote=None, **fields):
        self.driver.output = {"reply": "我理解了你的意思。", "action": action,
                              "task_id": self.task if action not in {"discuss", "new_task"} else None,
                              "goal": None, "acceptance": None, "quote": quote, **fields}

    def events(self, kind):
        return [e for e in self.rt.log.read(task_id=self.task) if e.type == kind]

    def resolve(self, text="继续了吗", *, continue_work=False):
        received = self.service.receive(self.task, text, continue_work=continue_work)
        return received, self.service.resolve_next(self.task)

    def assert_no_actions(self):
        for kind in ("human.commanded", "question.asked", "planning.requested", "activity.queued", "msg.said"):
            self.assertEqual(self.events(kind), [], kind)
        self.assertEqual(self.rt.work.scope(self.task), self.scope)

    def test_receive_only_persists_and_pending_contract(self):
        received = self.service.receive(self.task, "  继续了吗  ", continue_work=True)
        self.assertEqual(received.actor, Actor.human())
        self.assertEqual(received.payload, {"instruction": "  继续了吗  ", "continue_work": True, "scope": self.scope})
        self.assertEqual(self.service.pending(self.task), [received.seq])
        self.assertEqual(self.service.view(self.task)[0]["status"], "pending")
        self.assertEqual(self.driver.calls, [])
        self.assert_no_actions()

    def test_status_uses_observed_records_not_invented_model_progress(self):
        self.suggest("status_task", "继需了吗", reply="已经开发完了，正在部署。")
        received, result = self.resolve("继需了吗", continue_work=True)
        self.assertEqual(result["action"], "status_task")
        self.assertEqual(result["reply_source"], "event_projection")
        self.assertIn("尚未进入分项执行", result["reply"])
        self.assertNotIn("正在部署", result["reply"])
        self.assertEqual(result["next_action"], "none")
        self.assertEqual(self.events("task.progress_reported")[-1].payload["command_seq"], received.seq)
        self.assert_no_actions()

    def test_receive_rejects_invalid_input_without_events(self):
        for text in ("", "  ", None, 12, "长" * 12001):
            with self.subTest(text=str(text)[:20]), self.assertRaises(ValueError):
                self.service.receive(self.task, text)
        with self.assertRaises(ValueError):
            self.service.receive(self.task, "继续", continue_work="true")
        with self.assertRaises(KeyError):
            self.service.receive("missing", "继续")
        self.assertEqual(self.events("conversation.received"), [])

    def test_discussion_status_is_read_only_even_with_continue_permission(self):
        received, result = self.resolve(continue_work=True)
        self.assertEqual(result["conversation_seq"], received.seq)
        self.assertEqual(result["status"], "finished")
        self.assertEqual(result["action"], "discuss")
        self.assertEqual(result["next_action"], "none")
        self.assertEqual(result["reply"], self.driver.output["reply"])
        self.assert_no_actions()
        self.assertEqual(self.service.pending(self.task), [])
        self.assertIsNone(self.service.resolve_next(self.task))
        self.assertEqual(len(self.driver.calls), 1)
        finished = self.events("conversation.finished")[0]
        self.assertEqual(finished.actor, SYSTEM)
        self.assertEqual(finished.payload["tokens"], 17)
        self.assertEqual(finished.payload["driver"], "dialogue_test")
        self.assertEqual(finished.payload["speaker_id"], self.rt.organization.leader.id)
        self.assertEqual(finished.payload["token_usage"], "reported")
        self.assertEqual(self.rt.task(self.task).tokens, 17)

    def test_resume_preserves_scope_and_requires_continue_permission(self):
        for allowed in (False, True):
            self.suggest("resume_task", "继序做吧")
            received, result = self.resolve("继序做吧", continue_work=allowed)
            command = self.events("human.commanded")[-1]
            self.assertEqual(command.payload["intent"], "resume")
            self.assertEqual(command.payload["instruction"], "继序做吧")
            self.assertEqual(command.payload["conversation_seq"], received.seq)
            self.assertEqual(result["command_seq"], command.seq)
            self.assertEqual(result["next_action"], "run" if allowed else "none")
            self.assertEqual(self.rt.work.scope(self.task), self.scope)
        self.assertEqual(self.events("activity.queued"), [])
        self.assertEqual(self.events("planning.requested"), [])

    def test_mixed_modification_preserves_full_original_not_model_goal(self):
        text = "  继续了吗，顺便增家悔棋，但不要动原棋盘  "
        self.suggest("update_task", "顺便增家悔棋", goal="模型重写的目标", acceptance=["模型附加验收"])
        received, result = self.resolve(text, continue_work=True)
        command = self.events("human.commanded")[0]
        self.assertEqual(command.payload["instruction"], text)
        self.assertEqual(command.payload["model_suggestion"], self.driver.output)
        self.assertEqual(command.payload["intent"], "direction")
        self.assertNotIn("goal", command.payload)
        self.assertNotIn("acceptance", command.payload)
        self.assertEqual(self.rt.task(self.task).goal, "制作五子棋")
        self.assertEqual(self.rt.task(self.task).acceptance, ["保留网页成果"])
        self.assertEqual(result["command_seq"], command.seq)
        self.assertEqual(result["conversation_seq"], received.seq)
        self.assertEqual(result["next_action"], "run")
        self.assertEqual(self.rt.work.scope(self.task)["direction_seq"], command.seq)
        self.assertEqual(self.events("planning.requested"), [])

    def test_update_without_continue_adopts_but_never_queues(self):
        self.suggest("update_task", "加上悔棋")
        _, result = self.resolve("加上悔棋")
        self.assertEqual(result["next_action"], "none")
        self.assertEqual(len(self.events("human.commanded")), 1)
        self.assertEqual(self.events("activity.queued"), [])

    def test_explain_only_enqueues_existing_question_path_once(self):
        self.suggest("explain_task", "我怎么运行")
        with patch.object(self.rt.questions, "answer_next", side_effect=AssertionError("Must not answer inline")):
            received, result = self.resolve("我怎么运行", continue_work=True)
        question = self.events("question.asked")[0]
        self.assertEqual(question.payload["request_id"], f"conversation_{received.seq}")
        self.assertEqual(question.payload["question"], "我怎么运行")
        self.assertEqual(result["question_seq"], question.seq)
        self.assertEqual(result["next_action"], "explain")
        self.assertEqual(self.rt.work.scope(self.task), self.scope)
        self.assertEqual(self.events("human.commanded"), [])
        self.assertEqual(self.events("question.started"), [])
        self.assertIsNone(self.service.resolve_next(self.task))
        self.assertEqual(len(self.driver.calls), 1)

    def test_new_task_is_not_created_and_old_task_is_unchanged(self):
        self.suggest("new_task", "另外做个日历", goal="日历", acceptance=[])
        _, result = self.resolve("另外做个日历", continue_work=True)
        self.assertEqual(result["next_action"], "none")
        self.assertIn("尚未创建", result["reply"])
        self.assertEqual(len(list(self.rt.log.iter_events(event_types=("task.created",)))), 1)
        self.assert_no_actions()

    def test_invalid_response_and_transport_failure_never_fall_back_to_direction(self):
        valid = {"reply": "可以", "action": "resume_task", "task_id": self.task,
                 "goal": None, "acceptance": None, "quote": "继续"}
        invalid = ["not json", "x" * 65537, {}, [],
                   {**valid, "quote": "历史授权"}, {**valid, "task_id": "other"},
                   {**valid, "goal": "夹带修改"}, {**valid, "tool_calls": ["run"]},
                   {**valid, "action": "execute"}, RuntimeError("secret-token")]
        for raw in invalid:
            with self.subTest(response=str(raw)[:80]):
                self.driver.output = raw
                before = len(self.driver.calls)
                _, result = self.resolve("继续", continue_work=True)
                self.assertEqual(result["status"], "clarification")
                self.assertEqual(result["next_action"], "none")
                self.assertNotIn("secret-token", result["reply"])
                self.assertEqual(len(self.driver.calls), before + 1)
                self.assert_no_actions()

    def test_scope_changed_before_resolution_skips_model(self):
        self.service.receive(self.task, "继续")
        self.rt.command(self.task, "增加计时", goal="新的明确目标")
        before = len(self.events("human.commanded"))
        result = self.service.resolve_next(self.task)
        self.assertEqual(result["status"], "outdated")
        self.assertEqual(result["next_action"], "none")
        self.assertEqual(len(self.events("human.commanded")), before)
        self.assertEqual(self.driver.calls, [])

    def test_scope_changed_during_call_discards_update(self):
        self.suggest("update_task", "加上悔棋")
        self.driver.callback = lambda: self.rt.command(self.task, "修改验收", acceptance=["最新要求"])
        _, result = self.resolve("加上悔棋", continue_work=True)
        self.assertEqual(result["status"], "outdated")
        self.assertEqual(result["next_action"], "none")
        self.assertEqual(len(self.events("human.commanded")), 1)
        self.assertNotIn("conversation_seq", self.events("human.commanded")[0].payload)

    def test_newer_message_during_call_waits_without_discarding_previous(self):
        self.suggest("resume_task", "继续")
        self.driver.callback = lambda: self.service.receive(self.task, "等等，先说一下进度")
        received, result = self.resolve("继续", continue_work=True)
        self.assertEqual(result["status"], "finished")
        self.assertEqual(result["next_action"], "run")
        self.assertEqual(self.events("human.commanded")[0].payload["conversation_seq"], received.seq)
        self.driver.callback = None
        self.suggest("discuss")
        newer = self.service.resolve_next(self.task)
        self.assertGreater(newer["conversation_seq"], received.seq)
        self.assertEqual(len(self.driver.calls), 2)

    def test_fifo_change_then_progress_preserves_first_requirement_and_rebases(self):
        first = self.service.receive(self.task, "增加悔棋", continue_work=True)
        second = self.service.receive(self.task, "还要多久")
        self.suggest("update_task", "增加悔棋")
        changed = self.service.resolve_next(self.task)
        self.assertEqual(changed["conversation_seq"], first.seq)
        self.assertEqual(changed["status"], "finished")
        self.suggest("discuss")
        current = self.service.resolve_next(self.task)
        self.assertEqual(current["conversation_seq"], second.seq)
        self.assertEqual(current["status"], "finished")
        command = self.events("human.commanded")[0]
        self.assertEqual(command.payload["instruction"], "增加悔棋")
        self.assertEqual(len(self.events("human.commanded")), 1)
        self.assertEqual(len(self.driver.calls), 2)
        self.assertEqual(self.events("conversation.received")[1], second)
        self.assertEqual(second.payload["scope"], self.scope)
        self.assertEqual(self.events("conversation.started")[1].payload["scope"]["direction_seq"], command.seq)
        self.assertEqual(current["scope_before"]["direction_seq"], command.seq)
        self.assertEqual(self.driver.calls[1]["context"]["work"]["scope"], current["scope_before"])

    def test_command_cas_retries_for_new_pending_message_without_dropping_first(self):
        self.suggest("resume_task", "继续")
        emit_many = self.rt.log.emit_many
        raced = False

        def competing_emit(entries, **kwargs):
            nonlocal raced
            if entries[0]["type"] == "human.commanded" and not raced:
                raced = True
                self.service.receive(self.task, "再加个颜色选项")
            return emit_many(entries, **kwargs)

        with patch.object(self.rt.log, "emit_many", side_effect=competing_emit):
            _, result = self.resolve("继续", continue_work=True)
        self.assertTrue(raced)
        self.assertEqual(result["status"], "finished")
        self.assertEqual(len(self.events("human.commanded")), 1)
        self.assertEqual(len(self.events("conversation.finished")), 1)
        self.assertEqual(len(self.driver.calls), 1)
        self.assertEqual(len(self.service.pending(self.task)), 1)

    def test_started_lease_expires_to_uncertain_without_replay(self):
        received = self.service.receive(self.task, "继续")
        self.rt.log.emit(type="conversation.started", task_id=self.task, actor=SYSTEM,
                         payload={"conversation_seq": received.seq, "lease_until": time.time() + 600})
        self.assertEqual(self.service.view(self.task)[0]["status"], "answering")
        with patch("cater.conversation.time.time", return_value=time.time() + 601):
            self.assertEqual(self.service.view(self.task)[0]["status"], "uncertain")
            self.assertEqual(self.service.pending(self.task), [])
            self.assertIsNone(self.service.resolve_next(self.task))
        self.assertEqual(self.driver.calls, [])

    def test_failed_second_insert_rolls_back_command_and_finish_without_replay(self):
        self.suggest("update_task", "增加悔棋")
        self.service.receive(self.task, "增加悔棋", continue_work=True)
        self.rt.log.db.execute("""CREATE TEMP TRIGGER fail_conversation_finish BEFORE INSERT ON events
            WHEN NEW.type = 'conversation.finished'
            BEGIN SELECT RAISE(ABORT, 'simulated finish failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.service.resolve_next(self.task)
        self.rt.log.db.execute("DROP TRIGGER fail_conversation_finish")
        self.assertIsNone(ConversationService(self.rt).resolve_next(self.task))
        self.assertEqual(len(self.driver.calls), 1)
        self.assertEqual(self.events("human.commanded"), [])
        self.assertEqual(self.events("conversation.finished"), [])
        self.assertEqual(self.rt.work.scope(self.task), self.scope)
        self.assertTrue(self.rt.verify_chain().ok)

    def test_existing_applied_command_is_not_duplicated(self):
        self.suggest("resume_task", "继续")
        received = self.service.receive(self.task, "继续", continue_work=True)
        self.driver.callback = lambda: self.rt.log.emit(type="human.commanded", task_id=self.task, actor=Actor.human(),
            payload={"conversation_seq": received.seq, "instruction": "继续", "intent": "resume", "next_action": "run"})
        result = self.service.resolve_next(self.task)
        self.assertEqual(result["next_action"], "none")
        self.assertEqual(len(self.events("human.commanded")), 1)
        self.assertEqual(len(self.driver.calls), 1)

    def test_request_has_bounded_task_local_history_full_goals_and_prompt_data(self):
        other = self.rt.create_task("另一任务秘密目标", ["秘密验收"])
        self.rt.log.emit(type="msg.said", task_id=other, actor=Actor.agent("dev", "worker"), payload={"text": "不可跨任务"})
        for index in range(15):
            self.rt.log.emit(type="msg.said", task_id=self.task, actor=Actor.agent("dev", "worker"), payload={"text": f"历史-{index}"})
        self.rt.log.emit(type="deliverable.published", task_id=self.task, actor=Actor.agent("dev", "worker"),
                         payload={"title": "五子棋", "summary": "未执行的网页", "filename": "gomoku.html", "content": "不应包含大段成果正文"})
        self.rt.rulebook.record("不要假称部署成功", scope="task", task_id=self.task)
        text = "@dev 我怎末运行"
        self.resolve(text)
        call = self.driver.calls[0]
        context = call["context"]
        conversation = context["work"]["conversation"]
        self.assertEqual(context["context_scope"], "request")
        self.assertEqual(context["conversation"], conversation)
        self.assertEqual(conversation["message"], text)
        self.assertEqual(len(conversation["history"]), 12)
        self.assertEqual(conversation["history"][0]["text"], "历史-3")
        self.assertEqual(len(conversation["tasks"]), 1)
        candidate = conversation["tasks"][0]
        self.assertEqual(candidate["id"], self.task)
        self.assertNotIn("task_id", candidate)
        roles = {r["id"]: r for r in candidate["roles"]}
        for role in (self.rt.organization.leader, *self.rt.roles):
            self.assertEqual(roles[role.id]["name"], role.name)
            self.assertEqual(roles[role.id]["role"], role.role[:500])
        self.assertEqual(candidate["goal"], "制作五子棋")
        self.assertEqual(candidate["acceptance"], ["保留网页成果"])
        self.assertEqual(candidate["artifacts"][0]["filename"], "gomoku.html")
        for required in (text, "gomoku.html", "未执行的网页", "不要假称部署成功", "@仅代表关注", "错别字", "模糊", "不要输出内部推理"):
            self.assertIn(required, call["prompt"])
        for forbidden in ("另一任务秘密目标", "秘密验收", "不可跨任务", "不应包含大段成果正文"):
            self.assertNotIn(forbidden, call["prompt"])
        self.assertIn("本轮理解结果经系统核对后会用于消息分流", call["prompt"])
        self.assertNotIn("本轮只理解与回应，不会采用建议或启动工作", call["prompt"])
        self.assertIn("不得以discuss绕过成果来源核对", call["prompt"])
        self.assertIn("不得声称正在开发", call["prompt"])

    def test_oversize_context_fails_closed_without_truncating_goals(self):
        self.rt.command(self.task, "明确目标", goal="目" * 12000, acceptance=["验" * 2000] * 32)
        self.resolve()
        self.assertEqual(self.driver.calls, [])
        self.assertEqual(self.events("conversation.finished")[0].payload["status"], "clarification")

    def test_only_service_actor_and_matching_attempt_finish_queue(self):
        received = self.service.receive(self.task, "继续了吗")
        self.rt.log.emit(type="conversation.started", task_id=self.task, actor=Actor.agent("dev", "worker"),
                         payload={"conversation_seq": received.seq, "lease_until": time.time() + 600})
        self.rt.log.emit(type="conversation.finished", task_id=self.task, actor=SYSTEM,
                         payload={"conversation_seq": received.seq, "attempt_seq": 999, "status": "finished"})
        self.assertEqual(self.service.pending(self.task), [received.seq])
        self.service.resolve_next(self.task)
        self.assertEqual(self.service.view(self.task)[0]["status"], "finished")

    def test_resolver_does_not_call_execution_or_planning_methods(self):
        self.suggest("resume_task", "继续")
        with patch.object(self.rt, "command", side_effect=AssertionError("Do not reclassify")), \
             patch.object(self.rt, "collaborate", side_effect=AssertionError("No hidden execution")), \
             patch.object(self.rt, "battle", side_effect=AssertionError("No hidden meetings")), \
             patch.object(self.rt.questions, "answer_next", side_effect=AssertionError("No hidden answers")):
            _, result = self.resolve("继续", continue_work=True)
        self.assertEqual(result["next_action"], "run")
        self.assertEqual(self.events("msg.said"), [])
        self.assertEqual(self.events("activity.queued"), [])

    def test_collaboration_advice_is_in_history_without_claiming_execution(self):
        advice = self.rt.log.emit(type="collaboration.finished", actor=Actor.system("collaboration"), task_id=self.task,
            payload={"status": "answered", "speaker_id": "test", "reply": "建议先测边界，尚未执行测试。"})
        self.resolve("那你们刚才建议怎么做？")
        history = self.driver.calls[0]["context"]["conversation"]["history"]
        self.assertEqual(history[-1]["seq"], advice.seq)
        self.assertEqual(history[-1]["actor"], "test")
        self.assertIn("尚未执行测试", history[-1]["text"])

    def test_progress_reports_are_in_recent_history(self):
        progress = self.rt.report_progress(self.task, command_seq=0)
        self.resolve()
        history = self.driver.calls[0]["context"]["conversation"]["history"]
        self.assertEqual(history[-1]["seq"], progress.seq)
        self.assertEqual(history[-1]["text"], progress.payload["text"])
        self.assertIn(progress.payload["text"], self.driver.calls[0]["prompt"])

    def test_explain_scope_race_between_check_and_ask_is_rejected(self):
        self.suggest("explain_task", "我怎么运行")
        ask = ExplanationService.ask

        def race(service, *args, **kwargs):
            self.rt.command(self.task, "改任务", goal="另一个明确目标")
            return ask(service, *args, **kwargs)

        with patch.object(ExplanationService, "ask", new=race):
            _, result = self.resolve("我怎么运行")
        self.assertEqual(result["status"], "outdated")
        self.assertEqual(result["next_action"], "none")
        self.assertEqual(self.events("question.asked"), [])

    def test_explain_cas_retry_accepts_new_pending_message_in_fifo_order(self):
        self.suggest("explain_task", "我怎么运行")
        emit = self.rt.log.emit
        raced = False

        def competing_emit(**kwargs):
            nonlocal raced
            if kwargs["type"] == "question.asked" and not raced:
                raced = True
                self.service.receive(self.task, "另外增加悔棋")
            return emit(**kwargs)

        with patch.object(self.rt.log, "emit", side_effect=competing_emit):
            _, result = self.resolve("我怎么运行")
        self.assertTrue(raced)
        self.assertEqual(result["status"], "finished")
        self.assertEqual(result["next_action"], "explain")
        self.assertEqual(len(self.events("question.asked")), 1)
        self.assertEqual(len(self.service.pending(self.task)), 1)

    def test_failed_call_still_records_usage_once(self):
        self.driver.output = "bad json"
        _, result = self.resolve()
        self.assertEqual(result["status"], "clarification")
        self.assertEqual(result["tokens"], 17)
        self.assertEqual(self.rt._reported_driver_tokens("dialogue_test"), 17)
        self.assertIsNone(self.service.resolve_next(self.task))
        self.assertEqual(self.rt._reported_driver_tokens("dialogue_test"), 17)

    def test_parallel_workers_claim_only_one_model_call(self):
        entered, release = threading.Event(), threading.Event()
        errors = []

        def blocking_call():
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test worker timed out")

        def worker():
            try:
                with Runtime(self.db, roles_path=self.roles, drivers={"dialogue_test": self.driver}) as rt:
                    ConversationService(rt).resolve_next(self.task)
            except Exception as exc:
                errors.append(exc)

        self.driver.callback = blocking_call
        self.service.receive(self.task, "继续了吗")
        second = self.service.receive(self.task, "另外改色")
        thread = threading.Thread(target=worker)
        thread.start()
        try:
            self.assertTrue(entered.wait(5))
            self.assertEqual(self.service.view(self.task)[0]["status"], "answering")
            self.assertEqual(self.service.view(self.task)[1]["status"], "pending")
            self.assertEqual(self.service.pending(self.task), [])
            self.assertIsNone(self.service.resolve_next(self.task))
        finally:
            release.set()
            thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(self.driver.calls), 1)
        self.assertEqual(len(self.events("conversation.started")), 1)
        self.assertEqual(len(self.events("conversation.finished")), 1)
        self.assertEqual(self.service.pending(self.task), [second.seq])

    def test_active_work_and_artifacts_are_unchanged_by_discussion(self):
        self.rt.plan_work(self.task, [{"id": "board", "owner": "dev", "reviewer": "test",
            "objective": "完成五子棋", "depends_on": [],
            "criteria": [{"id": "review", "kind": "review", "text": "核对棋盘"}]}])
        self.rt.work.claim(self.task, "board")
        self.rt.log.emit(type="deliverable.published", task_id=self.task, actor=Actor.agent("dev", "worker"),
                         payload={"title": "草稿", "filename": "gomoku.html", "summary": "尚未检查", "content": "<html></html>"})
        before = copy.deepcopy(self.rt.work.view(self.task))
        artifacts = copy.deepcopy(self.rt.task(self.task).deliverables)
        scope = self.rt.work.scope(self.task)
        _, result = self.resolve("还在做吗", continue_work=True)
        self.assertEqual(result["next_action"], "none")
        self.assertEqual(self.rt.work.scope(self.task), scope)
        self.assertEqual(self.rt.work.view(self.task), before)
        self.assertEqual(self.rt.task(self.task).deliverables, artifacts)
        self.assertEqual(self.events("planning.requested"), [])
        self.assertEqual(self.events("activity.queued"), [])

    def test_invalid_response_does_not_consume_following_message(self):
        self.driver.output = "invalid"
        self.driver.callback = lambda: self.service.receive(self.task, "新的消息")
        _, result = self.resolve()
        self.assertEqual(result["status"], "clarification")
        self.assertEqual(result["error_code"], "invalid_response")
        self.assertEqual(len(self.service.pending(self.task)), 1)
        self.assertEqual(result["tokens"], 17)
        self.assert_no_actions()

    def test_fifo_resume_then_update_keeps_both_commands(self):
        first = self.service.receive(self.task, "继续", continue_work=True)
        second = self.service.receive(self.task, "改成红色", continue_work=False)
        self.suggest("resume_task", "继续")
        resumed = self.service.resolve_next(self.task)
        self.suggest("update_task", "改成红色")
        updated = self.service.resolve_next(self.task)
        commands = self.events("human.commanded")
        self.assertEqual([e.payload["conversation_seq"] for e in commands], [first.seq, second.seq])
        self.assertEqual([e.payload["intent"] for e in commands], ["resume", "direction"])
        self.assertEqual(resumed["next_action"], "run")
        self.assertEqual(updated["next_action"], "none")
        self.assertEqual(len(self.driver.calls), 2)

    def test_fifo_multiple_independent_updates_rebase_all_prior_commands(self):
        messages = [self.service.receive(self.task, text) for text in ("增加悔棋", "增加计时", "改成红色")]
        for received in messages:
            self.suggest("update_task", received.payload["instruction"])
            result = self.service.resolve_next(self.task)
            self.assertEqual(result["status"], "finished")
            self.assertEqual(result["conversation_seq"], received.seq)
        self.assertEqual([e.payload["instruction"] for e in self.events("human.commanded")],
                         [e.payload["instruction"] for e in messages])
        self.assertTrue(all(e.payload["scope"] == self.scope for e in self.events("conversation.received")))
        self.assertEqual(len(self.driver.calls), 3)

    def test_rebase_rejects_external_direction_even_when_later_chat_command_masks_it(self):
        first = self.service.receive(self.task, "增加悔棋")
        self.service.receive(self.task, "改成红色")
        self.rt.command(self.task, "外部明确修改", goal="另一目标")
        self.rt.log.emit(type="human.commanded", task_id=self.task, actor=Actor.human(), payload={
            "conversation_seq": first.seq, "instruction": "增加悔棋", "intent": "direction"})
        for _ in range(2):
            result = self.service.resolve_next(self.task)
            self.assertEqual(result["status"], "outdated")
        self.assertEqual(len(self.events("human.commanded")), 2)
        self.assertEqual(self.driver.calls, [])

    def test_rebase_rejects_rule_or_planning_changes(self):
        for change in ("rules", "planning"):
            with self.subTest(change=change):
                first = self.service.receive(self.task, "增加悔棋")
                self.service.receive(self.task, "改成红色")
                self.suggest("update_task", "增加悔棋")
                result = self.service.resolve_next(self.task)
                self.assertEqual(result["conversation_seq"], first.seq)
                if change == "rules":
                    self.rt.rulebook.record("必须保持原有布局", scope="task", task_id=self.task)
                else:
                    self.rt.log.emit(type="planning.requested", task_id=self.task, actor=Actor.human(), payload={})
                calls = len(self.driver.calls)
                commands = len(self.events("human.commanded"))
                stale = self.service.resolve_next(self.task)
                self.assertEqual(stale["status"], "outdated")
                self.assertTrue(self.events("conversation.started")[-1].payload["scope_conflict"])
                self.assertEqual(len(self.driver.calls), calls)
                self.assertEqual(len(self.events("human.commanded")), commands)

    def test_rebase_never_adopts_a_later_received_messages_command(self):
        first = self.service.receive(self.task, "增加悔棋")
        later = self.service.receive(self.task, "改成红色")
        self.rt.log.emit(type="human.commanded", task_id=self.task, actor=Actor.human(), payload={
            "conversation_seq": later.seq, "instruction": "改成红色", "intent": "direction"})
        result = self.service.resolve_next(self.task)
        self.assertEqual(result["conversation_seq"], first.seq)
        self.assertEqual(result["status"], "outdated")
        self.assertEqual(self.driver.calls, [])
        self.assertEqual(len(self.events("human.commanded")), 1)

    def test_command_batch_cas_rechecks_external_scope_change(self):
        self.suggest("update_task", "增加悔棋")
        emit_many = self.rt.log.emit_many
        raced = False

        def competing_emit(entries, **kwargs):
            nonlocal raced
            if entries[0]["type"] == "human.commanded" and not raced:
                raced = True
                self.rt.command(self.task, "外部更新", acceptance=["新的验收"])
            return emit_many(entries, **kwargs)

        with patch.object(self.rt.log, "emit_many", side_effect=competing_emit):
            _, result = self.resolve("增加悔棋", continue_work=True)
        self.assertEqual(result["status"], "outdated")
        self.assertEqual(result["next_action"], "none")
        self.assertEqual(len(self.events("human.commanded")), 1)
        self.assertNotIn("conversation_seq", self.events("human.commanded")[0].payload)
        self.assertEqual(self.rt._reported_driver_tokens("dialogue_test"), 17)

    def test_atomic_finish_has_recovery_contract_and_no_predicted_command_seq(self):
        for action, text, allowed in (("update_task", "增加悔棋", True), ("resume_task", "继续", False)):
            self.suggest(action, text)
            before = self.rt.work.scope(self.task)
            received, result = self.resolve(text, continue_work=allowed)
            command = self.events("human.commanded")[-1]
            finish = self.events("conversation.finished")[-1]
            self.assertEqual(finish.seq, command.seq + 1)
            self.assertEqual(finish.prev_hash, command.hash)
            self.assertEqual(finish.payload["scope_before"], before)
            self.assertEqual(finish.payload["conversation_seq"], received.seq)
            self.assertEqual(finish.payload["intent"], command.payload["intent"])
            self.assertEqual(finish.payload["action"], action)
            self.assertEqual(finish.payload["next_action"], "run" if allowed else "none")
            self.assertNotIn("command_seq", finish.payload)
            self.assertEqual(result["command_seq"], command.seq)
            self.assertEqual(result["finish_seq"], finish.seq)
            self.assertEqual(finish.payload["tokens"], 17)
        self.assertEqual(self.rt.task(self.task).tokens, 34)
        self.assertEqual(self.rt._reported_driver_tokens("dialogue_test"), 34)

    def test_committed_batch_with_lost_ack_is_durable_and_not_replayed(self):
        self.suggest("update_task", "增加悔棋")
        received = self.service.receive(self.task, "增加悔棋", continue_work=True)
        emit_many = self.rt.log.emit_many

        def lose_ack(entries, **kwargs):
            result = emit_many(entries, **kwargs)
            if entries[0]["type"] == "human.commanded":
                raise OSError("lost acknowledgment after commit")
            return result

        with patch.object(self.rt.log, "emit_many", side_effect=lose_ack), self.assertRaises(OSError):
            self.service.resolve_next(self.task)
        with Runtime(self.db, roles_path=self.roles, drivers={"dialogue_test": self.driver}) as restarted:
            service = ConversationService(restarted)
            self.assertIsNone(service.resolve_next(self.task))
            receipt = service.view(self.task)[0]
            self.assertEqual(receipt["status"], "finished")
            self.assertEqual(receipt["next_action"], "run")
            self.assertEqual(receipt["scope_before"], self.scope)
            commands = [e for e in restarted.log.iter_events(task_id=self.task, event_types=("human.commanded",))
                        if e.payload.get("conversation_seq") == received.seq]
            self.assertEqual(len(commands), 1)
            self.assertEqual(restarted.task(self.task).tokens, 17)
        self.assertEqual(len(self.driver.calls), 1)

    def test_expired_started_is_not_replayed_but_allows_next_pending(self):
        first = self.service.receive(self.task, "之前的问题")
        second = self.service.receive(self.task, "后来的问题")
        self.rt.log.emit(type="conversation.started", task_id=self.task, actor=SYSTEM,
                         payload={"conversation_seq": first.seq, "scope": self.scope, "lease_until": time.time() - 1})
        self.assertEqual(self.service.pending(self.task), [second.seq])
        result = self.service.resolve_next(self.task)
        self.assertEqual(result["conversation_seq"], second.seq)
        self.assertEqual(self.service.view(self.task)[0]["status"], "uncertain")
        self.assertEqual(len(self.driver.calls), 1)

    def test_request_id_retry_returns_original_event_even_after_processing(self):
        first = self.service.receive(self.task, "增加悔棋", continue_work=True, request_id="request_123")
        self.suggest("update_task", "增加悔棋")
        self.service.resolve_next(self.task)
        duplicate = self.service.receive(self.task, "增加悔棋", continue_work=True, request_id="request_123")
        self.assertEqual(duplicate, first)
        self.assertEqual(len(self.events("conversation.received")), 1)
        self.assertIsNone(self.service.resolve_next(self.task))
        self.assertEqual(len(self.driver.calls), 1)

    def test_request_id_validation_and_conflicting_reuse(self):
        for request_id in ("short", "x" * 129, "bad key!", [], 123):
            with self.subTest(request_id=request_id), self.assertRaises(ValueError):
                self.service.receive(self.task, "消息", request_id=request_id)
        self.service.receive(self.task, "消息", request_id="request_123")
        other = self.rt.create_task("另一任务", [])
        for task, text, allowed in ((self.task, "不同消息", False), (self.task, "消息", True), (other, "消息", False)):
            with self.subTest(task=task, text=text, allowed=allowed), self.assertRaises(WorkConflict):
                self.service.receive(task, text, continue_work=allowed, request_id="request_123")
        self.assertEqual(len(list(self.rt.log.iter_events(event_types=("conversation.received",)))), 1)
        self.service.receive(self.task, "无id兼容")
        self.service.receive(self.task, "无id兼容")
        self.assertEqual(len(self.events("conversation.received")), 3)

    def test_request_id_cas_retry_finds_concurrent_receipt(self):
        emit = self.rt.log.emit
        raced = False
        competing = []

        def competing_emit(**kwargs):
            nonlocal raced
            if kwargs["type"] == "conversation.received" and not raced:
                raced = True
                competing.append(self.service.receive(self.task, "同一消息", request_id="request_123"))
            return emit(**kwargs)

        with patch.object(self.rt.log, "emit", side_effect=competing_emit):
            received = self.service.receive(self.task, "同一消息", request_id="request_123")
        self.assertEqual(received, competing[0])
        self.assertEqual(len(self.events("conversation.received")), 1)

    def test_explain_uses_existing_unique_mention_recipient_without_execution(self):
        self.suggest("explain_task", "怎么打开")
        _, result = self.resolve("@米寇 怎么打开", continue_work=True)
        self.assertEqual(result["next_action"], "explain")
        self.assertEqual(self.events("question.asked")[0].payload["role_id"], "dev")
        self.assertEqual(self.events("human.commanded"), [])
        self.assertEqual(self.events("activity.queued"), [])
        self.assertEqual(self.rt.work.scope(self.task), self.scope)


if __name__ == "__main__":
    unittest.main()
