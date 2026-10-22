"""Offline delivery layering keeps answers intact and the existing handoff optional."""

import copy
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import zipfile

from cater.team_delivery import TeamDelivery, handoff_schema
import test_simple_team as simple
import test_team_external as external


class DeliveryHierarchyTests(unittest.TestCase):
    def fixture(self, mode):
        fixture = simple.SimpleTeamTests() if mode == "native" else external.TeamExternalTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def handoff(self):
        return {"why": "文件说明了数据保留方式，便于使用者复查；这里只是离线文字样例。",
                "changed": ["逐文件说明：note.txt 保存完整说明，不代表运行了其中的命令。"],
                "impact": ["此样例不修改工程配置。"],
                "alternatives": ["更详细的原因留在展开说明中。"],
                "reproduce": ["建议在解压后的工程根目录运行 python -m unittest discover -s tests -v；本例未执行。"],
                "limits": ["作者的-c探针与复核者的stdin输入应分别归因；此处没有实际探针。",
                           "未实测顾客环境；普通嵌套样例不证明自定义对象或循环结构兼容，也不表示顾客排除它们。"],
                "evidence": []}

    def complete(self, mode, arguments, *, publication=None):
        fixture = self.fixture(mode)

        def with_source():
            value = copy.deepcopy(arguments)
            if publication is not None:
                source, = fixture.events("deliverable.published")
                value["handoff"]["evidence"] = [{"source_seq": source.seq, "quote": publication["content"]}]
            return value

        if mode == "native":
            steps = []
            if publication is not None:
                steps.append(simple.turn("", ("publish_artifact", publication)))
            steps.append(lambda context, history, tools: simple.turn("", ("finish_work", with_source())))
            fixture.driver.steps[fixture.manager] = steps
        else:
            def deliver(run):
                if publication is not None:
                    result = run.call("publish_artifact", **publication)
                    self.assertEqual(result["status"], "ok")
                run.call("finish_work", **with_source())
                self.fail("Successful external finish must yield without another response")

            fixture.driver.scripts[fixture.manager] = [deliver]
        fixture.rt.team.receive(fixture.task, "按当前要求给出答复；需要全文时请保留全文。")
        if mode == "native":
            fixture.run_ok()
            self.assertEqual(len(fixture.driver.calls), 2 if publication is not None else 1)
        else:
            fixture.drain()
            self.assertEqual(len(fixture.driver.runs), 1)
        completion, = fixture.events("team.objective_completed")
        replies = [event.payload["text"] for event in fixture.events("msg.said")
                   if event.payload.get("delivery_seq") == completion.seq]
        self.assertEqual(replies, [arguments["summary"]])
        self.assertEqual(completion.payload["summary"], arguments["summary"])
        self.assertEqual(completion.payload["verification"], arguments["verification"])
        expected_tools = ["publish_artifact", "finish_work"] if publication is not None else ["finish_work"]
        self.assertEqual([event.payload["name"] for event in fixture.events("team.tool_started")], expected_tools)
        return fixture, completion

    def assert_layered_delivery(self, mode):
        summary = "说明文件已可下载，打开note.txt即可阅读。关键限制：这是文字样例，未执行工程命令。"
        arguments = {"summary": summary, "verification": "仅离线文字样例，无模型或工程验证。",
                     "handoff": self.handoff()}
        publication = {"filename": "note.txt", "format": "text", "summary": "完整说明样例",
                       "content": "完整说明：按实际输入方式、执行者、环境和已验证范围核对；有限样例不能推出全兼容。"}
        fixture, completion = self.complete(mode, arguments, publication=publication)
        saved = fixture.rt.team.delivery.read(fixture.task, {"delivery_seq": completion.seq})
        self.assertFalse(saved["historical"])
        self.assertEqual(saved["delivery"]["summary"], summary)
        note = saved["delivery"]["handoff"]
        self.assertEqual(note["version"], 1)
        for key, value in arguments["handoff"].items():
            if key != "evidence":
                self.assertEqual(note[key], value)
        source, = fixture.events("deliverable.published")
        evidence, = note["evidence"]
        self.assertEqual(evidence["source_seq"], source.seq)
        self.assertEqual(evidence["source_hash"], source.hash)
        self.assertEqual(evidence["quote"], publication["content"])
        self.assertEqual(evidence["kind"], "artifact")
        with zipfile.ZipFile(io.BytesIO(fixture.rt.team.archive(fixture.task))) as archive:
            self.assertEqual(archive.read("note.txt").decode("utf-8"), publication["content"])
            explanation = archive.read("__cater__/交付说明.md").decode("utf-8")
            self.assertIn(summary, explanation)
            self.assertIn(note["why"], explanation)
            for key in ("changed", "impact", "alternatives", "reproduce", "limits"):
                for detail in note[key]:
                    self.assertIn(detail, explanation)
            self.assertIn(f"事件 #{source.seq}", explanation)
            self.assertIn(publication["content"], explanation)

    def test_native_summary_and_optional_details_survive_read_and_archive(self):
        self.assert_layered_delivery("native")

    def test_external_summary_and_optional_details_survive_read_and_archive(self):
        self.assert_layered_delivery("external")

    def assert_verbatim_answers(self, mode):
        report = "完整报告\n" + "\n".join(
            f"第{i}项：样例数={i}，单位为条；仅说明该项样例，未验证顾客终端。"
            for i in range(1, 121)) + "\n完整命令：python -m unittest discover -s tests -v\n报告结束。"
        self.assertGreater(len(report), 1000)
        cases = [("consulting", "可以先保留现有方案。", False),
                 ("numbers", "767字节、293码点、209基本汉字；13个方法=交付8+独立5，子用例不另计。", False),
                 ("full-report", report, False), ("full-report-with-handoff", report, True)]
        for name, answer, with_handoff in cases:
            with self.subTest(mode=mode, case=name):
                arguments = {"summary": answer, "verification": "固定离线样例，不是实际测量结果。"}
                if with_handoff:
                    arguments["handoff"] = self.handoff()
                fixture, completion = self.complete(mode, arguments)
                saved = fixture.rt.team.delivery.read(fixture.task, {})["delivery"]
                self.assertEqual(saved["summary"], answer)
                self.assertEqual(saved["artifacts"], [])
                self.assertEqual("handoff" in saved, with_handoff)
                self.assertEqual("handoff" in completion.payload, with_handoff)
                if with_handoff:
                    self.assertIn(answer, TeamDelivery.markdown(completion))
                with self.assertRaisesRegex(ValueError, "尚无可下载的成果"):
                    fixture.rt.team.archive(fixture.task)

    def test_native_text_numbers_and_requested_full_report_are_not_rewritten(self):
        self.assert_verbatim_answers("native")

    def test_external_text_numbers_and_requested_full_report_are_not_rewritten(self):
        self.assert_verbatim_answers("external")

    def test_both_paths_receive_guidance_with_the_same_existing_schema(self):
        schemas = []
        for mode in ("native", "external"):
            with self.subTest(mode=mode):
                fixture = self.fixture(mode)
                prompts = []
                if mode == "native":
                    original = fixture.driver.tool_turn

                    def observed(**kwargs):
                        prompts.append(kwargs["prompt"])
                        return original(**kwargs)

                    observer = patch.object(fixture.driver, "tool_turn", side_effect=observed)
                    fixture.driver.steps[fixture.manager] = [simple.turn("", ("finish_work", {
                        "summary": "建议保留现有文字方案。", "verification": "未进行工程操作。"}))]
                else:
                    original = external.ScriptedExternalRun.run

                    def observed(run, prompt):
                        prompts.append(prompt)
                        return original(run, prompt)

                    observer = patch.object(external.ScriptedExternalRun, "run", new=observed)
                    fixture.driver.scripts[fixture.manager] = [lambda run: run.call(
                        "finish_work", summary="建议保留现有文字方案。", verification="未进行工程操作。")]
                with observer:
                    fixture.rt.team.receive(fixture.task, "给出文字建议")
                    fixture.rt.collaborate(fixture.task)
                self.assertEqual(len(prompts), 1)
                self.assertIn("交付不只说完成", prompts[0])
                self.assertEqual(len(fixture.events("team.objective_completed")), 1)
                tools = fixture.driver.calls[0]["tools"] if mode == "native" else fixture.driver.runs[0].tools
                schema = next(tool for tool in tools if tool["name"] == "finish_work")["input_schema"]
                schemas.append(schema)
                self.assertEqual(schema["required"], ["summary", "verification"])
                self.assertEqual(set(schema["properties"]), {"summary", "verification", "handoff", "artifact_seqs"})
                self.assertEqual(schema["properties"]["handoff"], handoff_schema())
                self.assertEqual(schema["properties"]["summary"], {"type": "string", "description":
                    schema["properties"]["summary"]["description"]})
        self.assertEqual(schemas[0], schemas[1])
        note = handoff_schema()
        self.assertEqual(note["required"], ["why", "reproduce", "limits"])
        self.assertEqual(set(note["properties"]), {"why", "changed", "impact", "alternatives", "reproduce", "limits", "evidence", "command_receipts"})
        self.assertNotIn("command_receipts", note["required"])
        self.assertEqual(note["properties"]["command_receipts"]["type"], "array")
        self.assertEqual(note["properties"]["command_receipts"]["maxItems"], 4)
        self.assertEqual(note["properties"]["command_receipts"]["items"], {"type": "integer", "minimum": 1})
        self.assertEqual(note["properties"]["why"]["maxLength"], 4000)
        self.assertEqual(note["properties"]["reproduce"]["maxItems"], 12)
        self.assertEqual(note["properties"]["reproduce"]["items"]["maxLength"], 1000)
        self.assertEqual(note["properties"]["evidence"]["maxItems"], 16)

    def test_updated_skill_is_read_in_full_without_engineering_tools(self):
        fixture = self.fixture("native")
        fixture.driver.steps[fixture.manager] = [simple.turn("", ("read_skill", {
            "skill_id": "explanatory-delivery"})), simple.turn("按任务解释结果和验证范围。")]
        fixture.rt.team.receive(fixture.task, "只阅读交付讲解指引")
        fixture.run_ok()
        result = json.loads(fixture.driver.calls[-1]["history"][-1]["content"])
        expected = (Path(__file__).resolve().parents[1]
                    / "cater/packs/skills/explanatory-delivery.md").read_text(encoding="utf-8")
        self.assertEqual(result["content"], expected)
        self.assertFalse(result["has_more"])
        self.assertEqual(fixture.events("team.objective_completed"), [])
        self.assertEqual([event.payload["name"] for event in fixture.events("team.tool_started")], ["read_skill"])
        for call in fixture.driver.calls:
            self.assertNotIn("workspace_run", {tool["name"] for tool in call["tools"]})


if __name__ == "__main__":
    unittest.main()
