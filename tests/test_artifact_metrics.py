import tempfile
import unittest
from pathlib import Path

from cater.artifacts import content_metrics, normalize_deliverables
from cater.events import Actor
from cater.runtime import Runtime
from test_work import item


class ArtifactMetricsTests(unittest.TestCase):
    def test_counts_have_explicit_units_and_do_not_strip_markdown(self):
        text = "# 中文\n A\t文\u3000\U00020000\U0001f31c"
        metrics = content_metrics(text)
        self.assertEqual(metrics["basis"], "raw_content_including_markdown")
        self.assertEqual(metrics["utf8_bytes"], len(text.encode("utf-8")))
        self.assertEqual(metrics["codepoints"], 12)
        self.assertEqual(metrics["non_whitespace_codepoints"], 7)
        self.assertEqual(metrics["cjk_ideographs"], 4)
        self.assertEqual(content_metrics("")["codepoints"], 0)

    def test_model_counts_cannot_override_host_facts_or_mutate_stored_artifact(self):
        with tempfile.TemporaryDirectory() as tmp, Runtime(Path(tmp) / "facts.db") as rt:
            task = rt.create_task("A short note")
            rt.plan_work(task, [item("a")])
            claim = rt.work.claim(task, "a")
            artifact = normalize_deliverables([{"title": "Note", "summary": "Evidence", "content": "文" * 128}])[0]
            event = rt.log.emit(type="deliverable.published", actor=Actor.agent("dev", "worker"), task_id=task,
                payload={**artifact, "work_item_id": "a", "attempt_id": claim.payload["attempt_id"],
                         "content_metrics": {"cjk_ideographs": 93}})
            submitted = rt.work.submit(task, claim, [event.seq])
            head = rt.log.head_hash()
            current = rt.work.artifacts(task, submitted.payload)[0]
            self.assertEqual(current["content_metrics"]["cjk_ideographs"], 128)
            self.assertEqual(current["content_sha256"], artifact["content_sha256"])
            self.assertEqual(rt.log.head_hash(), head)
            self.assertEqual(next(rt.log.iter_events(task_id=task, event_types=("deliverable.published",))).payload,
                             event.payload)


if __name__ == "__main__":
    unittest.main()
