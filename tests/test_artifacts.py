from __future__ import annotations

import hashlib
import importlib.util
import unittest
from pathlib import Path

from cater.artifacts import MAX_CONTENT_BYTES, normalize_deliverables, safe_filename
from cater.drivers import GuidedDriver, driver_mode
from cater.events import Actor, EventLog
from cater.memory import shared_deliverable_memory


class ArtifactTests(unittest.TestCase):
    def test_content_preserves_exact_bytes_and_legacy_metadata(self):
        content = "  缩进\n<script>仍作为正文</script>\n"
        items = normalize_deliverables([
            {"title": "正文", "summary": "摘要", "content": content, "format": "code", "filename": "result.html"},
            {"title": "旧成果", "summary": "只有摘要", "uri": "reports/old.md"},
        ])
        self.assertEqual(items[0]["content"], content)
        self.assertEqual(items[0]["content_sha256"], hashlib.sha256(content.encode()).hexdigest())
        self.assertEqual(items[0]["content_bytes"], len(content.encode()))
        self.assertNotIn("content", items[1])
        self.assertEqual(items[1]["uri"], "reports/old.md")

    def test_invalid_or_oversized_body_keeps_summary_without_partial_code(self):
        for value, format, reason in (
            ("猫" * (MAX_CONTENT_BYTES // 3 + 1), "code", "too_large"),
            (123, "text", "invalid_content"),
            ("正文", "html", "invalid_format"),
            ("\ud800", "text", "invalid_encoding"),
        ):
            with self.subTest(reason=reason):
                result = normalize_deliverables([{"title": "结果", "summary": "保留摘要", "content": value, "format": format}])[0]
                self.assertEqual(result["content_error"], reason)
                self.assertEqual(result["summary"], "保留摘要")
                self.assertNotIn("content", result)

    def test_deliverables_and_metadata_are_bounded(self):
        result = normalize_deliverables([{"title": "a" * 300, "summary": "b" * 3000}] * 50)
        self.assertEqual(len(result), 8)
        self.assertEqual(len(result[0]["title"]), 240)
        self.assertEqual(len(result[0]["summary"]), 2000)
        self.assertEqual(normalize_deliverables([{"title": {}, "summary": "摘要"}]), [])
        self.assertEqual(normalize_deliverables([{"title": "\ud800", "summary": "摘要"}]), [])

    def test_download_names_cannot_contain_paths_or_windows_device_names(self):
        for name in ("../file.md", "C:\\file.md", "a/b.md", "a\n.md", "NUL.txt", "con", "COM1.py", "a.", "\ud800.md"):
            with self.subTest(name=name):
                self.assertIsNone(safe_filename(name))
        self.assertEqual(safe_filename("猫咖成果.md"), "猫咖成果.md")

    def test_large_body_is_not_forwarded_into_cross_task_memory(self):
        log = EventLog(":memory:")
        try:
            log.emit(actor=Actor.agent("dev", "实现"), type="deliverable.published", task_id="previous", payload={"title": "结果", "summary": "简要说明", "content": "private body"})
            memory = shared_deliverable_memory(log.read())
        finally:
            log.close()
        self.assertNotIn("content", memory[0])
        self.assertNotIn("private body", str(memory))

    def test_local_demo_has_readable_body_without_claiming_real_execution(self):
        driver = GuidedDriver("demo")
        response = driver.complete(role="质量验证", prompt="", context={"goal": "准备发布"})
        import json
        content = normalize_deliverables(json.loads(response.text)["deliverables"])[0]["content"]
        self.assertIn("本地演示样例", content)
        self.assertIn("尚未执行", content)
        self.assertEqual(driver_mode(driver), "local_demo")

    def test_codex_adapter_accepts_new_bodies_and_legacy_deliverables(self):
        path = Path(__file__).resolve().parents[1] / "examples" / "codex_cli_driver.py"
        spec = importlib.util.spec_from_file_location("artifact_codex_adapter", path)
        adapter = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(adapter)
        legacy = {"title": "结果", "summary": "摘要", "kind": None, "uri": None}
        self.assertTrue(adapter._valid_deliverable(legacy))
        self.assertTrue(adapter._valid_deliverable({**legacy, "content": "# 正文", "format": "markdown", "filename": "result.md"}))
        self.assertFalse(adapter._valid_deliverable({**legacy, "content": "猫" * 30000, "format": "markdown"}))
        self.assertIn("content", adapter.RESPONSE_SCHEMA["properties"]["deliverables"]["items"]["required"])
