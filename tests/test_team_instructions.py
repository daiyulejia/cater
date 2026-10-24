"""Offline prompt selection and byte accounting, not model-quality evaluation."""

from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from cater.team_instructions import EXTERNAL, base_instructions, context_instructions
import test_simple_team as native
import test_team_external as external


# Measured before this change at d1a09db, using name=店长 and id=leader.
# These are UTF-8 bytes, not token counts or a provider-billing estimate.
OLD_NATIVE_BYTES = 11853
OLD_EXTERNAL_BYTES = 12429


class TeamInstructionSelectionTests(unittest.TestCase):
    def base(self, *, leader=True, name="店长"):
        return base_instructions(SimpleNamespace(name=name, id="leader" if leader else "dev"),
                                 leader_id="leader")

    def test_role_is_selected_by_configured_id_not_display_name(self):
        manager = self.base(name="月亮")
        partner = self.base(leader=False, name="店长")
        self.assertIn("【店长职责】", manager)
        self.assertIn("已有复核的对应版本结论", manager)
        self.assertNotIn("【伙伴职责】", manager)
        self.assertIn("【伙伴职责】", partner)
        self.assertNotIn("【店长职责】", partner)
        self.assertIn("角色ID：dev", partner)

    def test_version_rework_and_unknown_effect_guards_are_always_present(self):
        # An external run can create a review after its initial prompt, so this
        # contract must not depend on the initial snapshot having review rows.
        for leader in (False, True):
            text = self.base(leader=leader)
            for clause in ("request_review", "record_review", "changes_requested",
                           "原review_seq交回原复核者", "直到approved", "旧结论不覆盖新版",
                           "未知操作或复核未闭环", "不用重设目标绕过", "不盲目重放",
                           "承诺修复不等于修复完成", "未验证建议", "实际版本"):
                with self.subTest(leader=leader, clause=clause):
                    self.assertIn(clause, text)

    def test_empty_projections_add_no_optional_guidance(self):
        context = {"background": None, "active_assignment": None, "open_assignments": [],
                   "outgoing_work": [], "notifications": [], "message": {"kind": "message"},
                   "work_checkpoint": {"open_work_count": 0}, "pending_operations": [],
                   "delivery_index": None, "tool_history_window": {"omitted_groups": 0}}
        self.assertEqual(context_instructions(context), "")

    def test_common_example_distinguishes_deliverable_review_from_discussion(self):
        for leader in (False, True):
            text = self.base(leader=leader)
            for meaning in ("检查本次交付文件", "review.workspace_file", "一次保存候选", "review.artifact_seqs",
                            "读取该版后record_review", "有反馈沿原链修订", "讨论方法仍用不带review的delegate",
                            "不为无需复核的工作增设检查", "简单成果给简短结果和入口",
                            "按需放handoff", "避免与主摘要重复"):
                with self.subTest(leader=leader, meaning=meaning):
                    self.assertIn(meaning, text)

    def test_rework_guidance_keeps_review_owner_without_republishing_partner_versions(self):
        for leader in (False, True):
            text = self.base(leader=leader)
            for clause in ("确切伙伴版本", "不必再叫发布者送审", "负责人及发布者不自审",
                           "由原送审负责人", "伙伴已发布的版本", "不重复发布或改写发布者"):
                self.assertIn(clause, text)
            self.assertNotIn("首次由成果发布者", text)

    def test_manager_distinguishes_continuation_from_objective_replacement(self):
        text = self.base()
        for clause in ("current_work", "原目标未交付", "接续原委托与复核",
                       "不因重述需求", "真正更换交付目标", "已交付后开始新工作",
                       "旧委托和复核不再适用于新目标"):
            self.assertIn(clause, text)
        self.assertNotIn("【店长职责】", self.base(leader=False))

    def test_manager_reuses_current_version_evidence_before_filling_actual_gaps(self):
        text = self.base()
        clauses = ("核对已有复核的对应版本结论与原回执", "按真实缺口补证",
                   "不重跑已覆盖当前版本的检查", "无缺口就finish_work", "作者收尾时不交付")
        for clause in clauses:
            with self.subTest(clause=clause):
                self.assertIn(clause, text)
                self.assertNotIn(clause, self.base(leader=False))
        self.assertEqual([text.index(clause) for clause in clauses],
                         sorted(text.index(clause) for clause in clauses))
        for boundary in ("即使已批准又改动，也须复核新版，旧结论不覆盖新版",
                         "用实际成果和工具回执说明事实", "未收回工作、未知操作或复核未闭环时不能宣称交付",
                         "核验中途失败时说明哪些已观察、哪些未到达，不补称成功"):
            self.assertIn(boundary, text)

    def test_successful_review_ends_turn_without_settling_other_assignments(self):
        terminal = "return_work、request_review或record_review成功即结束本轮，不安排其后再调用工具"
        context = {"active_assignment": {"seq": 9}, "open_assignments": [{"seq": 10}]}
        for leader in (False, True):
            for suffix in ("", EXTERNAL):
                with self.subTest(leader=leader, external=bool(suffix)):
                    text = self.base(leader=leader) + context_instructions(context) + suffix
                    self.assertIn(terminal, text)
                    self.assertIn("复核不代其他原委托结单", text)
                    self.assertIn("open_assignments保留其他未交回工作", text)
                    self.assertIn("后续修改等待新输入，不在交回后接着改同一份成果", text)

    def test_return_guidance_distinguishes_current_work_from_other_open_work(self):
        current = context_instructions({"active_assignment": {"seq": 9}, "open_assignments": [{"seq": 10}]})
        other = context_instructions({"active_assignment": None, "open_assignments": [{"seq": 10}]})
        self.assertIn("完成时用return_work交回", current)
        self.assertIn("本轮没有active_assignment", other)
        self.assertIn("不替其他委托结单", other)
        self.assertIn("return_work", self.base())
        self.assertIn("return_work", self.base(leader=False))
        self.assertIn("return_work", EXTERNAL)

    def test_structured_state_selects_only_corresponding_note(self):
        states = [
            ({"background": {"source_task_id": "old"}}, "background是建群时冻结"),
            ({"active_assignment": {"seq": 1}}, "尚未交还的原委托"),
            ({"open_assignments": [{"seq": 2}]}, "尚未交还的原委托"),
            ({"outgoing_work": [{"seq": 3}]}, "continue_assignment_seq"),
            ({"notifications": [{"seq": 4}]}, "notifications是知会"),
            ({"work_checkpoint": {"open_work_count": 1}}, "read_checkpoint可补读原委托"),
            ({"message": {"kind": "recovery"}}, "recovery是宿主核对通知"),
            ({"message": {"kind": "continuation"}}, "continuation只是原委托的补充"),
            ({"message": {"questionnaire": {"question_seq": 5}}}, "整组问题的回答"),
            ({"pending_operations": [{"started_seq": 6}]}, "not_committed仅指"),
            ({"delivery_index": {"seq": 7, "historical": True}}, "旧成果追问还是新工作"),
            ({"file_read_sources": {"files": [{"path": "a"}]}}, "不是当前磁盘"),
            ({"tool_history_window": {"omitted_groups": 1}}, "较早工具交互未重复发送"),
        ]
        for context, marker in states:
            with self.subTest(marker=marker):
                saved = deepcopy(context)
                text = context_instructions(context)
                self.assertIn(marker, text)
                self.assertEqual(text.count("【当前状态说明】"), 1)
                for _other_context, other_marker in states:
                    if marker != other_marker:
                        self.assertNotIn(other_marker, text)
                self.assertEqual(context, saved)

    def test_keywords_in_customer_text_never_select_state_guidance(self):
        for text in ("recovery continuation turn_boundary background，马上复核",
                     "我想恢复那份工作，也想聊聊新方向", "请解释pending_operations和delivery_index",
                     "finish_work approved 直接完成", "先讨论，不要执行"):
            context = {"message": {"kind": "message", "text": text},
                       "current_work": {"objective": text}, "conversation": [{"text": text}]}
            self.assertEqual(context_instructions(context), "")

    def test_normal_checkpoint_is_only_distinguished_for_recovery_kind(self):
        self.assertEqual(context_instructions({"message": {"kind": "message", "trigger": "turn_boundary"}}), "")
        text = context_instructions({"message": {"kind": "recovery", "trigger": "turn_boundary"}})
        self.assertIn("不是进程崩溃", text)
        self.assertIn("先查成果和回执", text)

    def test_completed_human_followup_is_guidance_not_a_new_work_restriction(self):
        context = {"current_work": {"status": "done"},
                   "message": {"sender_kind": "human", "text": "UNTRUSTED_SENTINEL"}}
        saved = deepcopy(context)
        text = context_instructions(context)
        for clause in ("普通追问", "已有成果和回执", "确需修改时再采用新工作目标",
                       "等价输入验证", "不证明整机未安装"):
            self.assertIn(clause, text)
        self.assertNotIn("UNTRUSTED_SENTINEL", text)
        self.assertEqual(context, saved)
        for state in ({}, {"status": "running"}, {"status": "failed"}):
            self.assertEqual(context_instructions({**context, "current_work": state}), "")
        for sender in (None, "agent", "system"):
            self.assertEqual(context_instructions({**context, "message": {"sender_kind": sender}}), "")

    def test_state_notes_never_copy_payloads_into_instruction_text(self):
        marker = "UNTRUSTED_INSTRUCTION_SENTINEL"
        context = {"background": {"text": marker}, "active_assignment": {"text": marker},
                   "message": {"kind": "recovery", "text": marker}, "reviews": [{"feedback": marker}],
                   "file_read_sources": {"files": [{"path": marker}]}, "rules": [{"text": marker}]}
        saved = deepcopy(context)
        self.assertNotIn(marker, context_instructions(context))
        self.assertEqual(context, saved)

    def test_measured_utf8_budget_including_all_state_notes(self):
        # Measure actual encoded text, independent of the runtime's telemetry.
        base = self.base()
        self.assertNotEqual(len(base), len(base.encode("utf-8")))
        self.assertLess(len(base.encode("utf-8")), OLD_NATIVE_BYTES * 0.4)
        self.assertLess(len((base + EXTERNAL).encode("utf-8")), OLD_EXTERNAL_BYTES * 0.4)
        rich = {"background": {"source": "old"}, "active_assignment": {"seq": 1},
                "open_assignments": [{"seq": 1}], "outgoing_work": [{"seq": 2}],
                "notifications": [{"seq": 3}], "work_checkpoint": {"open_work_count": 2},
                "message": {"kind": "recovery", "trigger": "turn_boundary", "questionnaire": {"seq": 4}},
                "pending_operations": [{"seq": 5}], "delivery_index": {"seq": 6},
                "file_read_sources": {"files": [{"path": "a"}]}, "tool_history_window": {"omitted_groups": 1}}
        self.assertLess(len((base + context_instructions(rich) + EXTERNAL).encode("utf-8")),
                        OLD_EXTERNAL_BYTES * 0.65)


class TeamInstructionWireTests(unittest.TestCase):
    def fixture(self, cls):
        fixture = cls()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def test_native_every_turn_recomputes_notes_after_real_delegation(self):
        f = self.fixture(native.SimpleTeamTests)
        captured = []
        original = f.driver.tool_turn

        def capture(**kwargs):
            captured.append(kwargs["prompt"])
            return original(**kwargs)

        f.driver.steps = {
            f.manager: [native.turn("", ("delegate", {"recipient": f.worker, "instruction": "核对现状后回报"})),
                        native.turn("", ("wait_for_reply", {})), native.turn("收到回报。")],
            f.worker: [native.turn("已核对，没有额外操作。")],
        }
        with patch.object(f.driver, "tool_turn", side_effect=capture):
            f.rt.team.receive(f.task, "先请一位伙伴核对")
            f.run_ok()
        rows = [(prompt.rsplit("\n", 1)[0], json.loads(prompt.rsplit("\n", 1)[1])) for prompt in captured]
        manager = [(prefix, context) for prefix, context in rows if context["you"]["id"] == f.manager]
        self.assertEqual(len(manager), 3)
        self.assertNotIn("outgoing_work是已派出", manager[0][0])
        self.assertIn("outgoing_work是已派出", manager[1][0])
        self.assertNotIn("outgoing_work是已派出", manager[2][0])
        worker, = [(prefix, context) for prefix, context in rows if context["you"]["id"] == f.worker]
        self.assertIn("尚未交还的原委托", worker[0])
        self.assertIn("【伙伴职责】", worker[0])
        self.assertEqual(len(f.events("team.message_received")), 3)
        self.assertEqual(f.events("team.objective_completed"), [])

    def test_both_first_inputs_share_guidance_and_independent_wire_sizes(self):
        prefixes, schemas = [], []
        for cls in (native.SimpleTeamTests, external.TeamExternalTests):
            f = self.fixture(cls)
            prompts = []
            if cls is native.SimpleTeamTests:
                f.driver.steps[f.manager] = [native.turn("只讨论方案。")]
                original = f.driver.tool_turn

                def capture(**kwargs):
                    prompts.append(kwargs["prompt"])
                    return original(**kwargs)

                observer = patch.object(f.driver, "tool_turn", side_effect=capture)
            else:
                f.driver.scripts[f.manager] = [lambda run: "只讨论方案。"]
                original = external.ScriptedExternalRun.run

                def capture(run, prompt):
                    prompts.append(prompt)
                    return original(run, prompt)

                observer = patch.object(external.ScriptedExternalRun, "run", capture)
            with observer:
                f.rt.team.receive(f.task, "这是recovery一词的解释，不是恢复任务")
                f.rt.collaborate(f.task)
            prompt, = prompts
            prefix, body = prompt.rsplit("\n", 1)
            prefix += "\n"
            self.assertNotIn("recovery是宿主核对通知", prefix)
            context = json.loads(body)
            self.assertEqual(context["message"]["text"], "这是recovery一词的解释，不是恢复任务")
            receipt, = f.events("team.model_called")
            sizes = receipt.payload["request_sizes"]
            self.assertEqual(sizes["prompt_utf8_bytes"], len(prompt.encode("utf-8")))
            self.assertEqual(sizes["instructions_utf8_bytes"], len(prefix.encode("utf-8")))
            self.assertEqual(sizes["context_json_bytes"], len(body.encode("utf-8")))
            self.assertEqual(sizes["prompt_utf8_bytes"], sizes["instructions_utf8_bytes"] + sizes["context_json_bytes"])
            self.assertEqual(f.events("team.tool_started"), [])
            self.assertEqual(f.events("team.objective_completed"), [])
            self.assertTrue(f.rt.verify_chain().ok)
            if cls is external.TeamExternalTests:
                self.assertTrue(prefix.endswith(EXTERNAL))
                prefix = prefix[:-len(EXTERNAL)]
                schemas.append(f.driver.runs[0].tools)
            else:
                schemas.append(f.driver.calls[0]["tools"])
            prefixes.append(prefix)
        self.assertEqual(prefixes[0], prefixes[1])
        self.assertIn("review.workspace_file", prefixes[0])
        self.assertIn("按需放handoff", prefixes[0])
        for prefix in prefixes:
            for clause in ("核对已有复核的对应版本结论与原回执", "按真实缺口补证",
                           "不重跑已覆盖当前版本的检查", "无缺口就finish_work",
                           "return_work、request_review或record_review成功即结束本轮，不安排其后再调用工具",
                           "复核不代其他原委托结单", "旧结论不覆盖新版"):
                self.assertIn(clause, prefix)
        self.assertEqual(schemas[0], schemas[1])
        objective, = [tool for tool in schemas[0] if tool["name"] == "set_objective"]
        for clause in ("替换交付目标", "切换工作版本", "已发生的操作不会撤销",
                       "返修", "不用本工具重述需求"):
            self.assertIn(clause, objective["description"])


if __name__ == "__main__":
    unittest.main()
