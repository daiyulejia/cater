"""Optional guidance is bounded, scoped and versioned, not executable input."""
import hashlib
from importlib.resources import files
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cater.skills import MAX_SKILL_BYTES, SkillLibrary


class SkillLibraryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.config = self.root / "skills.json"
        self.body = self.root / "SKILL.md"
        self.body.write_bytes("# 本地方法\r\n正文不是默认上下文。\n".encode("utf-8"))
        self.entry = {"id": "local-guide", "title": "本地流程", "description": "供本群阅读的短指引", "path": str(self.body)}
        self.library = SkillLibrary(self.config)
        self.configure([self.entry])
        self.scope = {"role_id": "dev", "workspace_id": "project", "tools": set()}

    def configure(self, rows):
        self.config.write_text(json.dumps({"skills": rows}, ensure_ascii=True), encoding="utf-8")

    def index(self, **kwargs):
        return self.library.index(**{**self.scope, **kwargs})

    def read(self, **kwargs):
        return self.library.read({"skill_id": "local-guide", **kwargs}, **self.scope)

    def test_catalog_is_metadata_only_and_hashes_raw_bytes_without_path(self):
        result = self.index()
        self.assertEqual(result["configuration_status"], "ok")
        entry, = [row for row in result["skills"] if row["id"] == "local-guide"]
        self.assertEqual(entry["sha256"], hashlib.sha256(self.body.read_bytes()).hexdigest())
        self.assertEqual(entry["bytes"], len(self.body.read_bytes()))
        self.assertEqual(entry["source"], "configured")
        for private in (str(self.body), str(self.config), "正文不是默认上下文", "path", "content"):
            self.assertNotIn(private, json.dumps(result, ensure_ascii=False))

    def test_actual_utf8_pages_preserve_original_crlf_and_do_not_execute_markup(self):
        content = "# 标题\r\n<script>neverRun()</script>\nrm -rf nothing\n正文"
        self.body.write_bytes(content.encode("utf-8"))
        first = self.read(offset=0, limit=8)
        self.assertEqual(first["content"], content[:8])
        self.assertTrue(first["has_more"])
        second = self.read(offset=first["next_offset"], limit=12000, expected_sha256=first["skill"]["sha256"])
        self.assertEqual(first["content"] + second["content"], content)
        self.assertFalse(second["has_more"])
        self.assertEqual(first["total_chars"], len(content))
        self.assertEqual(self.body.read_bytes(), content.encode("utf-8"))
        self.assertIn("不执行脚本", first["note"])

    def test_changed_body_cannot_be_stitched_to_old_page(self):
        first = self.read(limit=5)
        self.body.write_text("# 新版本\nNew content", encoding="utf-8")
        changed = self.read(expected_sha256=first["skill"]["sha256"], offset=5)
        self.assertEqual(changed["code"], "skill_changed")
        self.assertNotIn("content", changed)
        self.assertNotEqual(changed["current_sha256"], first["skill"]["sha256"])
        self.assertEqual(self.read()["skill"]["sha256"], changed["current_sha256"])

    def test_role_workspace_and_real_tool_requirements_must_all_match(self):
        self.configure([{**self.entry, "roles": ["dev"], "workspaces": ["project"], "requires": ["workspace_run"]}])
        for change in ({}, {"tools": {"workspace_run"}, "role_id": "other"},
                       {"tools": {"workspace_run"}, "workspace_id": "other"}, {"tools": {"workspace_run"}, "workspace_id": None}):
            scope = {**self.scope, **change}
            self.assertNotIn("local-guide", [r["id"] for r in self.library.index(**scope)["skills"]])
            self.assertEqual(self.library.read({"skill_id": "local-guide"}, **scope)["code"], "skill_unavailable")
        scope = {**self.scope, "tools": {"workspace_run"}}
        self.assertEqual(self.library.index(**scope)["skills"][0]["id"], "local-guide")
        self.assertEqual(self.library.read({"skill_id": "local-guide"}, **scope)["status"], "ok")

    def test_empty_filter_has_no_recipients_but_empty_requirements_need_no_tools(self):
        self.configure([{**self.entry, "roles": []}])
        ids = [row["id"] for row in self.index()["skills"]]
        self.assertNotIn("local-guide", ids)
        self.assertIn("explanatory-delivery", ids)
        self.assertEqual(self.read()["code"], "skill_unavailable")
        self.configure([{**self.entry, "requires": []}])
        self.assertEqual(self.read()["status"], "ok")

    def test_catalog_paging_and_metadata_budget(self):
        builtins = SkillLibrary().index(**self.scope, limit=20)
        self.assertFalse(builtins["has_more"])
        builtin_ids = [row["id"] for row in builtins["skills"]]
        self.assertIn("explanatory-delivery", builtin_ids)
        rows = [{**self.entry, "id": f"guide-{i}"} for i in range(23)]
        self.configure(rows)
        custom_ids = [row["id"] for row in rows]
        expected_ids = custom_ids + builtin_ids
        first = self.index(limit=20)
        self.assertEqual(first["total"], 23 + builtins["total"])
        self.assertEqual(len(first["skills"]), 20)
        self.assertTrue(first["has_more"])
        combined = []
        page = first
        while True:
            self.assertEqual(page["offset"], len(combined))
            self.assertEqual(page["total"], len(expected_ids))
            self.assertLessEqual(len(page["skills"]), 20)
            combined.extend(page["skills"])
            self.assertEqual(page["next_offset"], len(combined))
            self.assertEqual(page["has_more"], len(combined) < len(expected_ids))
            if not page["has_more"]:
                break
            self.assertGreater(page["next_offset"], page["offset"])
            page = self.index(offset=page["next_offset"], limit=20)
        ids = [row["id"] for row in combined]
        self.assertEqual(ids, expected_ids)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual([row["id"] for row in combined if row["source"] == "configured"], custom_ids)
        self.assertEqual([row["id"] for row in combined if row["source"] == "builtin"], builtin_ids)
        self.assertEqual(len(self.index()["skills"]), 5)
        with self.assertRaises(ValueError):
            self.index(offset=999)

    def test_exact_end_is_empty_but_beyond_end_is_an_explicit_error(self):
        end = len(self.body.read_bytes().decode("utf-8"))
        result = self.read(offset=end)
        self.assertEqual(result["content"], "")
        self.assertFalse(result["has_more"])
        self.assertEqual(result["next_offset"], end)
        self.assertEqual(self.read(offset=end + 1)["code"], "invalid_skill_request")
        total = self.index()["total"]
        last = self.index(offset=total)
        self.assertEqual(last["skills"], [])
        self.assertFalse(last["has_more"])
        self.assertEqual(last["next_offset"], total)
        with self.assertRaises(ValueError):
            self.index(offset=total + 1)

    def test_metadata_argument_validation_is_bounded(self):
        for kwargs in ({"offset": True}, {"offset": -1}, {"limit": 0}, {"limit": 21}, {"limit": "1"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.index(**kwargs)
        for kwargs in ({"offset": True}, {"offset": -1}, {"limit": 0}, {"limit": 12001},
                       {"expected_sha256": None}, {"expected_sha256": "wrong"}, {"path": str(self.body)}):
            with self.subTest(kwargs=kwargs):
                self.assertEqual(self.read(**kwargs)["code"], "invalid_skill_request")

    def test_skill_id_is_not_an_arbitrary_path_and_request_cannot_choose_scope(self):
        for value in (str(self.body), "../SKILL.md", "", True, "missing", "x" * 81):
            response = self.library.read({"skill_id": value}, **self.scope)
            self.assertEqual(response["status"], "error")
            self.assertNotIn("content", response)
        for key in ("role_id", "workspace_id", "tools", "path"):
            self.assertEqual(self.read(**{key: "different"})["code"], "invalid_skill_request")

    def test_custom_manifest_failure_never_reuses_cached_entries_or_hides_builtins(self):
        self.assertEqual(self.read()["status"], "ok")
        self.config.write_text("bad-json-config-secret-value", encoding="utf-8")
        result = self.index(tools={"finish_work"})
        self.assertEqual(result["configuration_status"], "unavailable")
        self.assertEqual([r["id"] for r in result["skills"]], ["explanatory-delivery"])
        self.assertEqual(self.read()["code"], "skill_unavailable")
        self.assertNotIn("secret-value", json.dumps(result))
        self.assertNotIn(str(self.config), json.dumps(result))

    def test_removal_or_malformed_changes_do_not_continue_old_custom_source(self):
        self.assertEqual(self.read()["status"], "ok")
        for rows in ([], [{**self.entry, "roles": ["other"]}], [{**self.entry, "path": "relative.md"}]):
            self.configure(rows)
            self.assertEqual(self.read()["status"], "error")
        self.config.unlink()
        self.assertEqual(self.index()["configuration_status"], "unavailable")
        self.assertEqual(self.read()["code"], "skill_unavailable")

    def test_invalid_schema_duplicates_reserved_ids_and_limits_reject_custom_manifest(self):
        for rows in ([self.entry, self.entry], [{**self.entry, "id": "bug-investigation"}],
                     [{**self.entry, "id": "../escape"}], [{**self.entry, "unknown": True}],
                     [{**self.entry, "requires": "workspace_run"}], [{**self.entry, "roles": ["dev", "dev"]}],
                     [{**self.entry, "title": "\0private"}], [{**self.entry, "description": "x" * 401}],
                     [{**self.entry, "title": "\ud800"}], [{**self.entry, "description": "\udfff"}],
                     [{**self.entry, "id": f"g-{i}"} for i in range(129)]):
            with self.subTest(rows=str(rows)[:80]):
                self.configure(rows)
                self.assertEqual(self.index()["configuration_status"], "unavailable")
        self.config.write_text('{"skills":[],"skills":[]}', encoding="utf-8")
        self.assertEqual(self.index()["configuration_status"], "unavailable")

    def test_config_and_markdown_resources_reject_oversized_empty_binary_and_invalid_utf8(self):
        for body in (b"", b" \n", b"\x00binary", b"\xff", b"x" * (MAX_SKILL_BYTES + 1)):
            with self.subTest(size=len(body)):
                self.body.write_bytes(body)
                self.assertEqual(self.index()["skills"][0]["status"], "unavailable")
                self.assertEqual(self.read()["code"], "skill_unavailable")
        self.config.write_bytes(b"x" * 1048577)
        self.assertEqual(self.index()["configuration_status"], "unavailable")

    def test_protected_and_unsafe_registered_paths_are_rejected_without_body_leak(self):
        protected = self.root / "secrets"
        protected.mkdir()
        (protected / "notes.md").write_text("must not read", encoding="utf-8")
        for name in (".env.md", "notes.key", "../SKILL.md", "missing.md", "secrets/notes.md"):
            candidate = self.root / name
            self.configure([{**self.entry, "path": str(candidate)}])
            response = self.read()
            self.assertEqual(response["status"], "error")
            self.assertNotIn(str(candidate), json.dumps(response))
        self.configure([{**self.entry, "path": "relative.md"}])
        self.assertEqual(self.index()["configuration_status"], "unavailable")

    def test_hard_link_and_directory_are_never_read_as_skill_file(self):
        linked = self.root / "linked.md"
        os.link(self.body, linked)
        self.configure([{**self.entry, "path": str(linked)}])
        self.assertEqual(self.read()["code"], "skill_unavailable")
        directory = self.root / "directory.md"
        directory.mkdir()
        self.configure([{**self.entry, "path": str(directory)}])
        self.assertEqual(self.read()["code"], "skill_unavailable")

    def test_builtin_resources_are_complete_and_index_does_not_read_all_bodies_into_prompt(self):
        library = SkillLibrary()
        scope = {**self.scope, "tools": {"workspace_read", "workspace_search", "workspace_run", "browser_check", "request_review", "read_artifact", "finish_work"}}
        index = library.index(**scope)
        self.assertEqual(len(index["skills"]), 5)
        for entry in index["skills"]:
            self.assertEqual(entry["source"], "builtin")
            self.assertEqual(entry["status"], "ready")
            response = library.read({"skill_id": entry["id"], "expected_sha256": entry["sha256"]}, **scope)
            self.assertEqual(response["status"], "ok")
            self.assertTrue(response["content"].startswith("# "))
            self.assertNotIn(response["content"], json.dumps(index, ensure_ascii=False))
        config = files("cater.packs").joinpath("skills").joinpath("catalog.json").read_text(encoding="utf-8")
        self.assertEqual(len(json.loads(config)["skills"]), 5)
        pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('"packs/skills/*.json"', pyproject)
        self.assertIn('"packs/skills/*.md"', pyproject)

    def test_builtin_resources_support_single_child_namespace_joinpath(self):
        root = files("cater.packs")

        class SingleChildRoot:
            def joinpath(self, child):
                return root.joinpath(child)

        scope = {**self.scope, "tools": {"workspace_read", "workspace_search", "workspace_run",
                                        "browser_check", "request_review", "read_artifact", "finish_work"}}
        with patch("cater.skills.files", return_value=SingleChildRoot()):
            library = SkillLibrary()
            index = library.index(**scope)
            self.assertEqual(index["configuration_status"], "ok")
            self.assertEqual(len(index["skills"]), 5)
            for entry in index["skills"]:
                self.assertEqual(entry["status"], "ready")
                result = library.read({"skill_id": entry["id"], "expected_sha256": entry["sha256"]}, **scope)
                self.assertEqual(result["status"], "ok")
                self.assertTrue(result["content"].startswith("# "))


if __name__ == "__main__":
    unittest.main()
