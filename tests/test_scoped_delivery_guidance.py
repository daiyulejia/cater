"""Offline scripted Runtime regressions, not real API or model-quality tests."""

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from cater import team_instructions as guidance
from cater.workspaces import WorkspaceService
import test_simple_team as native
import test_team_external as external


class ScopedDeliveryGuidanceTests(unittest.TestCase):
    def fixture(self, external_mode=False, *, leader_id=None):
        module = external if external_mode else native
        cls = external.TeamExternalTests if external_mode else native.SimpleTeamTests
        f = cls()
        self.addCleanup(f.doCleanups)
        runtime = module.Runtime

        def configured(*args, roles_path, **kwargs):
            config = json.loads(roles_path.read_text(encoding="utf-8"))
            config["organization"]["leader"].update(id=leader_id, name="协调员")
            worker = config["organization"]["worker_members"][0]
            next(r for r in config["roles"] if r["id"] == worker)["name"] = "店长"
            roles_path.write_text(json.dumps(config), encoding="utf-8")
            return runtime(*args, roles_path=roles_path, **kwargs)

        with patch.object(module, "Runtime", side_effect=configured if leader_id else runtime):
            f.setUp()
        return f

    def script(self, f, role, *turns):
        if isinstance(f, native.SimpleTeamTests):
            f.driver.steps[role] = list(turns)
            return

        def execute(run, turn):
            for call in turn.calls:
                run.call(call.name, **call.arguments)
            return turn.text

        f.driver.scripts[role] = [lambda run, turn=t: execute(run, turn) for t in turns]

    def run_input(self, f, text="Explain the current facts.", *, recipient=None):
        rows = []

        def remember(prompt, tools):
            prefix, body = prompt.rsplit("\n", 1)
            rows.append((prefix + "\n", json.loads(body), deepcopy(tools)))

        if isinstance(f, native.SimpleTeamTests):
            original = f.driver.tool_turn

            def capture(**kwargs):
                remember(kwargs["prompt"], kwargs["tools"])
                return original(**kwargs)

            observer = patch.object(f.driver, "tool_turn", side_effect=capture)
        else:
            original = external.ScriptedExternalRun.run

            def capture(run, prompt):
                remember(prompt, run.tools)
                return original(run, prompt)

            observer = patch.object(external.ScriptedExternalRun, "run", capture)
        prefix = f"@{f.rt.team._role(recipient).name} " if recipient else ""
        with observer:
            f.rt.team.receive(f.task, prefix + text)
            if isinstance(f, native.SimpleTeamTests):
                f.run_ok()
                self.assertTrue(all(not steps for steps in f.driver.steps.values()))
            else:
                f.drain()
                self.assertTrue(all(r.revoked and r.state.cleanup_complete for r in f.driver.runs))
                self.assertEqual(f.rt.team.external_runs.pending(f.task), [])
        return rows

    def first_input(self, external_mode=False, *, worker=False, leader_id=None, text="Explain."):
        f = self.fixture(external_mode, leader_id=leader_id)
        role = f.worker if worker else f.manager
        self.script(f, role, native.turn("Offline scripted answer."))
        row, = self.run_input(f, text, recipient=role if worker else None)
        return row

    @staticmethod
    def tool(row, name):
        return next(t for t in row[2] if t["name"] == name)

    def test_runtime_paths_share_guidance_and_tool_wire_schema(self):
        local, remote = self.first_input(), self.first_input(True)
        self.assertTrue(remote[0].endswith(guidance.EXTERNAL))
        self.assertEqual(local[0], remote[0][:-len(guidance.EXTERNAL)])
        self.assertEqual(local[2], remote[2])
        for row in (local, remote):
            self.assertIn(guidance.COMMON, row[0])
            self.assertIn(guidance.MANAGER, row[0])
            reply = self.tool(row, "reply_to_customer")
            for clause in ("要求简短时不重述全流程", "要求细节时完整回答", "保留执行者与环境范围"):
                self.assertIn(clause, reply["description"])

    def test_custom_leader_id_selects_complete_result_responsibility(self):
        for mode in (False, True):
            for leader_id in ("delivery_owner_73", "coordinator-z9"):
                with self.subTest(external=mode, leader_id=leader_id):
                    manager = self.first_input(mode, leader_id=leader_id)
                    worker = self.first_input(mode, worker=True, leader_id=leader_id)
                    self.assertEqual(manager[1]["you"]["id"], leader_id)
                    self.assertIn(guidance.MANAGER, manager[0])
                    for clause in ("委托写清完整交付", "负责人协调复核、返修的职责",
                                   "按需让伙伴直接往返"):
                        self.assertIn(clause, manager[0])
                        self.assertNotIn(clause, worker[0])
                    for prompt in (manager[0], worker[0]):
                        self.assertIn("局部委托按实际范围", prompt)
                    for clause in ("对委托的完整结果负责", "需协作时直接交接并收回反馈",
                                   "完成后交回委托人"):
                        self.assertIn(clause, worker[0])
                        self.assertNotIn(clause, manager[0])
                    self.assertIn("店长（角色ID：", worker[0])
                    self.assertIn(guidance.PARTNER, worker[0])
                    self.assertNotIn("【店长职责】", worker[0])
                    self.assertIn("finish_work", {t["name"] for t in manager[2]})
                    self.assertNotIn("finish_work", {t["name"] for t in worker[2]})

    def test_factual_scope_guidance_reaches_both_runtime_roles(self):
        clauses = ("保留执行者、版本和环境", "个人未做不等于全队未做",
                   "未见记录先核对", "不反推从未发生",
                   "索引和历史不是全文或当前磁盘，需要时读取实际来源",
                   "工具定义是本轮授权，不是整机软件清单",
                   "未配置命令或受限PATH查找未中不证明未安装")
        for mode in (False, True):
            for worker in (False, True):
                with self.subTest(external=mode, worker=worker):
                    prompt, _, _ = self.first_input(mode, worker=worker)
                    for clause in clauses:
                        self.assertIn(clause, prompt)

    def test_brevity_guidance_retains_requested_full_answers(self):
        for mode in (False, True):
            row = self.first_input(mode)
            for clause in ("按本次问题选详略，不复述全部历史", "顾客要全文或细节时保留",
                           "普通聊天不套交付模板", "按需放handoff"):
                self.assertIn(clause, row[0])
            finish = self.tool(row, "finish_work")["input_schema"]
            self.assertIn("顾客要求全文或数字时保留", finish["properties"]["summary"]["description"])
            for field in ("summary", "verification"):
                self.assertNotIn("maxLength", finish["properties"][field])

    def test_bound_workspace_zip_guidance_matches_actual_stdin_execution(self):
        schemas = []
        expected_guide = (Path(__file__).resolve().parents[1]
                          / "cater/packs/skills/engineering-checks.md").read_text(encoding="utf-8")
        note = b"PUBLISHED-NOTE-CONTENT\n"
        script = (
            "import json, sys\n"
            "from pathlib import Path\n"
            "assert Path('note.txt').is_file()\n"
            "assert not Path('bundle.zip').exists()\n"
            "assert not Path('local_check.py').exists()\n"
            "text = Path('note.txt').read_text(encoding='utf-8')\n"
            "assert text == 'PUBLISHED-NOTE-CONTENT\\n'\n"
            "print(json.dumps({'marker': 'ZIP-STDIN-CHECK-OK', 'note': text,\n"
            "                  'cwd': str(Path.cwd()), 'isolated': sys.flags.isolated,\n"
            "                  'no_bytecode': sys.flags.dont_write_bytecode}))\n"
        )
        for mode in (False, True):
            with self.subTest(external=mode):
                f = self.fixture(mode)
                project = f.root / "project"
                project.mkdir()
                (project / "note.txt").write_bytes(note)
                (project / "local_check.py").write_text(script, encoding="utf-8")
                environment = f.root / "environment"
                environment.mkdir()
                env = {key: str(environment) for key in (
                    "TEMP", "TMP", "HOME", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "XDG_CACHE_HOME")}
                env["PYTHONDONTWRITEBYTECODE"] = "1"
                prefix = [sys.executable, "-I", "-B"]
                config = f.root / "workspaces.json"
                config.write_text(json.dumps({"workspaces": [{
                    "id": "project", "title": "Offline ZIP check", "root": str(project),
                    "mode": "execute", "commands": {"python": prefix},
                    "env_allowlist": list(env), "timeout": 5}]}), encoding="utf-8")
                f.rt.workspaces = WorkspaceService(f.rt, config)
                f.rt.workspaces.bind(f.task, "project", expected_revision=None)
                with patch.dict(os.environ, env):
                    self.script(f, f.manager, native.turn("",
                                ("read_skill", {"skill_id": "engineering-checks"}),
                                ("publish_workspace_archive", {"filename": "bundle.zip",
                                    "summary": "Published note only", "paths": ["note.txt"]}),
                                ("reply_to_customer", {"text": "Saved the offline fixture."})))
                    row, = self.run_input(f)
                    tool = self.tool(row, "workspace_run")
                    schemas.append(tool)
                    selection = tool["input_schema"]["properties"]["artifact_seqs"]
                    self.assertEqual({k: v for k, v in selection.items() if k != "description"}, {
                        "type": "array", "minItems": 1, "maxItems": 128,
                        "items": {"type": "integer", "minimum": 1}})
                    for clause in ("ZIP自动解为成员文件，不放入原ZIP本体", "cwd相对此副本",
                                   "每次临时目录独立，不复用上次路径",
                                   "未发布的辅助检查脚本不会复制", "stdin"):
                        self.assertIn(clause, selection["description"])
                    guide, = [e.payload["result"] for e in f.events("team.tool_finished")
                              if e.payload["name"] == "read_skill"]
                    self.assertEqual(guide["status"], "ok", guide)
                    self.assertEqual(guide["content"], expected_guide)
                    self.assertFalse(guide["has_more"])
                    publication, = f.events("deliverable.published")
                    self.assertEqual(publication.payload["filename"], "bundle.zip")
                    self.assertEqual(publication.payload["format"], "archive")
                    (project / "note.txt").write_bytes(b"Unpublished live content\n")
                    before = {p.relative_to(project): p.read_bytes()
                              for p in project.rglob("*") if p.is_file()}
                    state = f.rt.team.view(f.task)
                    arguments = {"command": "python", "args": ["-"], "stdin": script,
                                 "cwd": ".", "timeout": 5, "artifact_seqs": [publication.seq]}
                    self.script(f, f.manager, native.turn("",
                                ("workspace_run", deepcopy(arguments)),
                                ("workspace_run", deepcopy(arguments)),
                                ("reply_to_customer", {"text": "Checked the published note."})))
                    checked_row, = self.run_input(f)
                    self.assertEqual(self.tool(checked_row, "workspace_run"), tool)
                receipts = [e.payload["result"] for e in f.events("team.tool_finished")
                            if e.payload["name"] == "workspace_run"]
                self.assertEqual(len(receipts), 2)
                paths = []
                for result in receipts:
                    self.assertEqual(result["status"], "ok", result)
                    self.assertEqual((result["outcome"], result["exit_code"]), ("passed", 0))
                    self.assertTrue(result["complete"])
                    self.assertEqual(result["argv"], prefix + ["-"])
                    self.assertEqual(result["cwd"], ".")
                    self.assertEqual(result["environment"], "temporary_artifact_copy_not_sandbox")
                    self.assertEqual(result["checked_artifacts"], [{
                        "seq": publication.seq, "filename": "bundle.zip",
                        "content_sha256": publication.payload["content_sha256"]}])
                    member, = result["checked_files"]
                    self.assertEqual(member["artifact_seq"], publication.seq)
                    self.assertEqual(member["path"], "note.txt")
                    self.assertEqual(member["sha256"], hashlib.sha256(note).hexdigest())
                    self.assertEqual(member["bytes"], len(note))
                    for field in ("scope_current", "artifacts_unchanged", "artifact_check_valid"):
                        self.assertIs(result[field], True)
                    output = json.loads(result["stdout"]["excerpt"])
                    self.assertEqual(output["marker"], "ZIP-STDIN-CHECK-OK")
                    self.assertEqual(output["note"], note.decode("utf-8"))
                    self.assertEqual((output["isolated"], output["no_bytecode"]), (1, 1))
                    paths.append(Path(output["cwd"]))
                    self.assertNotEqual(paths[-1], project.resolve())
                    self.assertFalse(paths[-1].exists())
                self.assertNotEqual(paths[0], paths[1])
                self.assertEqual({p.relative_to(project): p.read_bytes()
                                  for p in project.rglob("*") if p.is_file()}, before)
                current = f.rt.team.view(f.task)
                for key in ("objective", "revision", "status"):
                    self.assertEqual(current[key], state[key])
                self.assertEqual(f.rt.team_reviews.view(f.task), [])
                for kind in ("team.review_requested", "team.review_recorded", "team.objective_completed"):
                    self.assertEqual(f.events(kind), [])
                self.assertNotIn("finish_work", [e.payload["name"] for e in f.events("team.tool_started")])
                self.assertEqual(f.rt.workspaces.pending(task_id=f.task), [])
                self.assertTrue(f.rt.verify_chain().ok)
        self.assertEqual(schemas[0], schemas[1])

    def test_explanatory_skill_reads_actual_packaged_scope_guidance(self):
        expected = (Path(__file__).resolve().parents[1]
                    / "cater/packs/skills/explanatory-delivery.md").read_text(encoding="utf-8")
        for mode in (False, True):
            with self.subTest(external=mode):
                f = self.fixture(mode)
                self.script(f, f.manager,
                            native.turn("", ("read_skill", {"skill_id": "explanatory-delivery"}),
                                        ("reply_to_customer", {"text": "Only read the reference."})))
                self.run_input(f)
                receipt, = [e for e in f.events("team.tool_finished") if e.payload["name"] == "read_skill"]
                result = receipt.payload["result"]
                self.assertEqual(result["status"], "ok")
                self.assertEqual(result["content"], expected)
                for clause in ("保留执行者、版本和环境范围", "不表示团队没有检查",
                               "索引没有原输出时按需要读回", "不是整机软件清单",
                               "不能推断宿主未安装", "既有文件不变与新增文件分别说明",
                               "不用部分文件哈希概括整个工程没有变化"):
                    self.assertIn(clause, result["content"])
                self.assertEqual(f.events("team.objective_completed"), [])

    def test_customer_keywords_never_select_state_or_route_tools(self):
        words = ("recovery continuation turn_boundary background pending_operations delivery_index "
                 "tool_catalog active_assignment approved finish_work 简短 全文 打包 独立复核")
        for mode in (False, True):
            plain = self.first_input(mode, text="Hello.")
            quoted = self.first_input(mode, text=words)
            self.assertEqual(plain[0], quoted[0])
            self.assertEqual(plain[2], quoted[2])
            self.assertEqual(quoted[1]["message"]["text"], words)
        context = {"message": {"kind": "message", "sender_kind": "human", "text": words},
                   "current_work": {"status": "running", "objective": words},
                   "conversation": [{"text": words}]}
        self.assertEqual(guidance.context_instructions(context), "")

    def test_state_notes_follow_host_projections_without_copying_payloads(self):
        sentinel = "UNTRUSTED_SCOPE_SENTINEL"
        cases = (
            ({"current_work": {"status": "done"}, "message": {"sender_kind": "human"}},
             "当前目标已交付"),
            ({"message": {"kind": "recovery", "trigger": "turn_boundary"}}, "不是进程崩溃"),
            ({"active_assignment": {"text": sentinel}}, "尚未交还的原委托"),
            ({"pending_operations": [{"text": sentinel}]}, "不证明外部执行或计费未发生"),
            ({"tool_history_window": {"omitted_groups": 1}}, "缺少历史不表示操作未发生"),
        )
        for context, clause in cases:
            with self.subTest(clause=clause):
                context.setdefault("message", {})["text"] = sentinel
                saved = deepcopy(context)
                note = guidance.context_instructions(context)
                self.assertIn(clause, note)
                self.assertNotIn(sentinel, note)
                self.assertEqual(context, saved)
                self.assertEqual(note.count("【当前状态说明】"), 1)
        for sender in ("agent", "system"):
            self.assertEqual(guidance.context_instructions({"current_work": {"status": "done"},
                             "message": {"sender_kind": sender, "text": "当前目标已交付"}}), "")

    def test_call_schemas_keep_existing_fields_and_optional_review(self):
        contracts = {
            "reply_to_customer": (["text"], {"text", "evidence"}),
            "delegate": (["recipient", "instruction"], {"recipient", "instruction", "review"}),
            "return_work": (["text"], {"text"}),
            "set_objective": (["goal"], {"goal"}),
            "finish_work": (["summary", "verification"],
                            {"summary", "verification", "handoff", "artifact_seqs",
                             "omitted_approved_artifacts_reason"}),
        }
        for mode in (False, True):
            row = self.first_input(mode)
            for name, (required, fields) in contracts.items():
                with self.subTest(external=mode, tool=name):
                    schema = self.tool(row, name)["input_schema"]
                    self.assertEqual(schema["required"], required)
                    self.assertEqual(set(schema["properties"]), fields)
                    self.assertFalse(schema["additionalProperties"])
            self.assertEqual(self.tool(row, "reply_to_customer")["input_schema"]["properties"]["text"],
                             {"type": "string", "minLength": 1, "maxLength": 32000})

    def test_short_and_long_replies_end_only_the_turn_and_preserve_body(self):
        for mode in (False, True):
            for text in ("42。", "完整说明。\n" * 5333 + "完。"):
                with self.subTest(external=mode, length=len(text)):
                    f = self.fixture(mode)
                    before = f.rt.team.view(f.task)
                    self.script(f, f.manager, native.turn("",
                                ("reply_to_customer", {"text": text}),
                                ("set_objective", {"goal": "Must not execute after reply"})))
                    self.run_input(f, "请给出完整细节。" if len(text) > 100 else "简短回答。")
                    answer, = [e for e in f.events("msg.said")
                               if e.payload.get("audience") == "customer"]
                    self.assertEqual(answer.payload["text"], text)
                    start, = f.events("team.tool_started")
                    self.assertEqual(start.payload["name"], "reply_to_customer")
                    receipt, = f.events("team.tool_finished")
                    self.assertEqual(receipt.payload["result"]["status"], "ok")
                    current = f.rt.team.view(f.task)
                    for key in ("objective", "revision", "status"):
                        self.assertEqual(current[key], before[key])
                    self.assertEqual(f.events("team.objective_completed"), [])
                    self.assertEqual(f.events("team.review_recorded"), [])
                    finished, = f.events("team.run_finished")
                    self.assertEqual(finished.payload["status"], "completed")
                    if mode:
                        self.assertEqual(f.driver.runs[0].state.phase, "yielded")

    def test_delivery_still_accepts_full_body_without_mandatory_packaging_or_review(self):
        summary = "Requested full explanation.\n" * 400
        for mode in (False, True):
            with self.subTest(external=mode):
                f = self.fixture(mode)
                self.script(f, f.manager, native.turn("", ("finish_work", {
                    "summary": summary, "verification": "Offline scripted host flow only."}),
                    ("set_objective", {"goal": "Must not execute after delivery"})))
                self.run_input(f, "Explain the result in full; no files are needed.")
                completed, = f.events("team.objective_completed")
                self.assertEqual(completed.payload["summary"], summary.strip())
                self.assertEqual(f.rt.team.view(f.task)["status"], "done")
                self.assertEqual(f.rt.task(f.task).deliverables, [])
                self.assertEqual(f.events("team.review_requested"), [])
                self.assertEqual(f.events("team.objective_updated"), [])
                start, = f.events("team.tool_started")
                self.assertEqual(start.payload["name"], "finish_work")
                answer, = [e for e in f.events("msg.said") if e.payload.get("audience") == "customer"]
                self.assertEqual(answer.payload["text"], summary.strip())


if __name__ == "__main__":
    unittest.main()
