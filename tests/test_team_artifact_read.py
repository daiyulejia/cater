"""Optional exact-version artifact pages, with real native tool receipts."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import unittest
import zipfile

from cater.events import Actor
from cater.artifacts import content_metrics, normalize_deliverables
import test_team_review_cycle


class TeamArtifactReadTests(unittest.TestCase):
    def setUp(self):
        self.f = test_team_review_cycle.TeamReviewCycleTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def read(self, seq, **arguments):
        result, = self.f.exchange(self.f.worker, ("read_artifact", {"artifact_seq": seq, **arguments}))
        return result

    def test_default_preserves_complete_original_response(self):
        content = "月亮\n" * 5000
        seq = self.f.publish(content)
        artifact, = self.f.rt.task(self.f.task).deliverables
        self.assertEqual(self.read(seq), {"status": "ok", **artifact,
                                         "content_metrics": content_metrics(content)})
        self.assertEqual(len(self.read(seq)["content"]), len(content))
        self.assertNotIn("read_range", self.read(seq))

    def test_unicode_pages_reassemble_exact_version_and_full_digest(self):
        content = "月亮\U0001f43e\u0301\r\n\x00" * 900
        seq = self.f.publish(content)
        artifact, = self.f.rt.task(self.f.task).deliverables
        original = next(e for e in self.f.rt.log.read(task_id=self.f.task) if e.seq == seq)
        chunks, offset = [], 0
        while True:
            page = self.read(seq, offset=offset, length=751)
            self.assertEqual(page["content_sha256"], hashlib.sha256(content.encode()).hexdigest())
            self.assertEqual(page["content_bytes"], len(content.encode()))
            self.assertEqual({k: v for k, v in page.items() if k not in {"status", "content", "read_range"}},
                             {**{k: v for k, v in artifact.items() if k != "content"},
                              "content_metrics": content_metrics(content)})
            coverage = page["read_range"]
            chunks.append(page["content"])
            end = offset + len(page["content"])
            self.assertEqual(coverage, {"unit": "unicode_codepoints", "offset": offset,
                "returned_chars": len(page["content"]), "total_chars": len(content),
                "has_more": end < len(content), "next_offset": end if end < len(content) else None,
                "content_complete": False})
            if not coverage["has_more"]:
                break
            offset = coverage["next_offset"]
        self.assertEqual("".join(chunks), content)
        current = next(e for e in self.f.rt.log.read(task_id=self.f.task) if e.seq == seq)
        self.assertEqual(current, original)
        self.assertEqual(Path(artifact["local_path"]).read_bytes(), content.encode())
        self.assertTrue(self.f.rt.verify_chain().ok)

    def test_offset_only_uses_default_page_not_entire_remainder(self):
        content = "A" * 20000
        seq = self.f.publish(content)
        page = self.read(seq, offset=123)
        self.assertEqual(page["content"], content[123:8123])
        self.assertEqual(page["read_range"]["next_offset"], 8123)
        self.assertFalse(page["read_range"]["content_complete"])

    def test_length_only_starts_at_zero_and_maximum_page_is_supported(self):
        seq = self.f.publish("A" * 20000)
        page = self.read(seq, length=16000)
        self.assertEqual(len(page["content"]), 16000)
        self.assertEqual(page["read_range"]["offset"], 0)
        self.assertTrue(page["read_range"]["has_more"])

    def test_first_page_can_cover_entire_small_file_but_is_not_approval(self):
        seq = self.f.publish("small")
        request = self.f.request([seq])
        page = self.read(seq, length=8000)
        self.assertTrue(page["read_range"]["content_complete"])
        self.assertIsNone(page["read_range"]["next_offset"])
        self.assertFalse(page["read_range"]["has_more"])
        self.assertEqual(self.f.rt.team_reviews.view(self.f.task)[0]["status"], "pending")
        self.assertEqual(self.f.rt.team_reviews.view(self.f.task)[0]["request_seq"], request["review_seq"])

    def test_tail_and_eof_do_not_claim_entire_file_was_read(self):
        seq = self.f.publish("0123456789")
        for offset, body in ((8, "89"), (10, "")):
            with self.subTest(offset=offset):
                page = self.read(seq, offset=offset, length=100)
                self.assertEqual(page["content"], body)
                self.assertFalse(page["read_range"]["has_more"])
                self.assertIsNone(page["read_range"]["next_offset"])
                self.assertFalse(page["read_range"]["content_complete"])

    def test_invalid_bounds_and_types_are_recoverable_without_unknown_effects(self):
        seq = self.f.publish("body")
        cases = [{"offset": v} for v in (-1, True, "0", 1.5, None, 5)]
        cases += [{"length": v} for v in (0, -1, 16001, False, "1", 1.5, None)]
        for arguments in cases:
            with self.subTest(arguments=arguments):
                self.assertEqual(self.read(seq, **arguments)["status"], "error")
        self.assertEqual(self.f.rt.team._pending_operations(self.f.task), [])
        self.assertEqual(self.read(seq, offset=0, length=4)["content"], "body")

    def test_invalid_artifact_ids_and_unknown_fields_do_not_alias(self):
        seq = self.f.publish("body")
        for value in (True, -1, 0, "1", 1.0, None):
            with self.subTest(seq=value):
                self.assertEqual(self.read(value, length=1)["status"], "error")
        self.assertEqual(self.read(seq, limit=1)["status"], "error")
        self.assertEqual(self.read(seq + 999999, length=1)["status"], "error")

    def test_metadata_only_legacy_artifact_is_not_silently_empty_body(self):
        event = self.f.rt.log.emit(type="deliverable.published", task_id=self.f.task,
            actor=Actor.agent(self.f.worker, self.f.rt.team._role(self.f.worker).role),
            payload={"title": "Historical reference", "summary": "No saved content", "uri": "https://example.invalid"})
        self.assertNotIn("content", self.read(event.seq))
        self.assertEqual(self.read(event.seq, offset=0)["status"], "error")
        payload, = normalize_deliverables([{"title": "Historical inline body", "summary": "Fixture",
                                           "content": "legacy body", "format": "text"}])
        self.assertNotIn("filename", payload)
        inline = self.f.rt.log.emit(type="deliverable.published", task_id=self.f.task,
            actor=Actor.agent(self.f.worker, self.f.rt.team._role(self.f.worker).role), payload=payload)
        self.assertEqual(self.read(inline.seq)["content"], "legacy body")
        page = self.read(inline.seq, length=6)
        self.assertEqual(page["content"], "legacy")
        self.assertNotIn("filename", page)
        self.assertFalse(page["read_range"]["content_complete"])

    def test_exact_seq_survives_same_name_new_version_and_disk_change(self):
        old = self.f.publish("old version")
        new = self.f.publish("new version")
        latest = self.f.rt.task(self.f.task).deliverables[-1]
        Path(latest["local_path"]).write_bytes(b"disk changed")
        self.assertEqual(self.read(old, offset=4, length=7)["content"], "version")
        self.assertEqual(self.read(new, length=3)["content"], "new")
        self.assertEqual(self.read(old)["content"], "old version")

    def test_other_task_artifact_cannot_be_read_even_with_same_filename(self):
        other = self.f.rt.create_task("Other project")
        self.f.rt.team.enable(other)
        foreign = self.f.publish("private other task", task_id=other)
        self.assertEqual(self.read(foreign, offset=0, length=1)["status"], "error")

    def test_native_transcript_keeps_exact_page_and_original_signed_blocks(self):
        seq = self.f.publish("abcdef" * 3000)
        self.read(seq, offset=4444, length=777)
        last = self.f.driver.calls[-1]
        result = json.loads(last["history"][-1]["content"])
        self.assertEqual(result["content"], ("abcdef" * 3000)[4444:5221])
        self.assertEqual(result["read_range"]["returned_chars"], 777)
        signed = [row for row in last["history"] if "provider_content" in row]
        self.assertTrue(signed)
        self.assertTrue(all(row["provider_content"][0]["type"] == "thinking" for row in signed))
        self.assertEqual(self.f.protocol_errors, [])

    def test_paused_customer_can_read_page_without_resuming_writes(self):
        seq = self.f.publish("available frozen body")
        self.f.rt.execution_control.change(self.f.task, "pause", request_id="pause-paged-read")
        page = self.read(seq, offset=10, length=6)
        self.assertEqual(page["content"], "frozen")
        self.assertEqual(self.f.rt.execution_control.view(self.f.task)["state"], "paused")
        names = {tool["name"] for tool in self.f.driver.calls[-1]["tools"]}
        self.assertNotIn("publish_artifact", names)
        self.assertNotIn("workspace_write", names)
        self.assertNotIn("workspace_run", names)


class PagedReviewCycleTests(unittest.TestCase):
    def test_optional_pages_do_not_change_two_rework_rounds_or_final_version(self):
        f = test_team_review_cycle.TeamReviewCycleTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        original = f.native_turn
        seen = []

        def paged_turn(text="", *calls):
            updated = []
            for name, arguments in calls:
                if name == "read_artifact":
                    arguments = {**arguments, "offset": 0, "length": 16000}
                    seen.append(arguments["artifact_seq"])
                updated.append((name, arguments))
            return original(text, *updated)

        f.native_turn = paged_turn
        f.test_two_rejections_then_approval_resume_author_before_delivery()
        self.assertGreaterEqual(len(seen), 3)
        review, = f.rt.team_reviews.view(f.task)
        latest = f.rt.task(f.task).deliverables[-1]
        self.assertEqual(review["status"], "approved")
        self.assertEqual(review["artifacts"][0]["seq"], latest["seq"])
        self.assertEqual(Path(latest["local_path"]).read_bytes(), latest["content"].encode())
        with zipfile.ZipFile(io.BytesIO(f.rt.team.archive(f.task))) as archive:
            self.assertEqual(archive.read("result.txt"), latest["content"].encode())
        reads = [e.payload["result"] for e in f.events("team.tool_finished") if e.payload["name"] == "read_artifact"]
        self.assertTrue(all(row["read_range"]["content_complete"] for row in reads))
        self.assertEqual(f.protocol_errors, [])


if __name__ == "__main__":
    unittest.main()
