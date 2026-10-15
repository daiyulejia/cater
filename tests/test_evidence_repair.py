import io
import json
import os
import unittest
from unittest.mock import patch

from cater.anthropic import AnthropicMessagesDriver
from cater.runtime import Runtime
from test_work import WorkDriver, item


class EvidenceRepairTests(unittest.TestCase):
    def run_case(self, *, always_fail=False, unknown=False, bad_quote=False):
        self.enterContext(patch.dict(os.environ, {"CATER_REPAIR_TEST": "synthetic"}))
        worker = AnthropicMessagesDriver("dev", endpoint="https://fixture.invalid/messages", model="test",
                                        api_key_env="CATER_REPAIR_TEST")
        reviewer = WorkDriver()
        def judge(work):
            if work.get("phase") == "review":
                reviewer.status = "unknown" if unknown else "failed" if (
                    always_fail or work["artifacts"][0]["content"] == "incomplete") else "passed"
                reviewer.quote = "not in the artifact" if bad_quote else None
        reviewer.after = judge
        rt = Runtime(drivers={"dev": worker, "test": reviewer})
        self.addCleanup(rt.close)
        task = rt.create_task("Write a checked note")
        rt.plan_work(task, [item("draft"), item("next", owner="design", deps=["draft"])])
        prompts = []
        def response(request, **kwargs):
            prompts.append(json.loads(request.data)["messages"][0]["content"])
            text = json.dumps({"stance": "assert", "text": "Draft", "claim": "Draft ready",
                "deliverables": [{"title": "Draft", "summary": "A note", "filename": "draft.md",
                                  "format": "markdown", "content": "incomplete" if len(prompts) == 1 else "corrected"}]})
            return io.BytesIO(json.dumps({"type": "message", "content": [{"type": "text", "text": text}],
                                        "stop_reason": "end_turn"}).encode())
        with patch("cater.anthropic._open_api_request", side_effect=response):
            rt.collaborate(task)
        return rt, task, prompts

    def test_failed_review_returns_evidence_to_owner_and_continues_dependencies(self):
        rt, task, prompts = self.run_case()
        self.assertEqual(len(prompts), 2)
        self.assertIn("previous_attempt", prompts[1])
        self.assertIn("incomplete", prompts[1])
        self.assertIn("Inspected the exact body", prompts[1])
        rows = rt.work.view(task)["items"]
        self.assertEqual(rows[0]["status"], "accepted")
        self.assertTrue(rows[1]["attempt_id"])
        repairs = list(rt.log.iter_events(task_id=task, event_types=("work.repair_requested",)))
        self.assertEqual(len(repairs), 1)
        self.assertEqual(len([c for c in rt.log.iter_events(task_id=task, event_types=("work.claimed",))
                              if c.payload["work_item_id"] == "draft"]), 2)

    def test_unresolved_defect_stops_after_one_revision(self):
        rt, task, prompts = self.run_case(always_fail=True)
        self.assertEqual(len(prompts), 2)
        rows = rt.work.view(task)["items"]
        self.assertEqual(rows[0]["status"], "ready")
        self.assertEqual(rows[1]["status"], "blocked")
        self.assertFalse(rows[1]["attempt_id"])

    def test_unknown_or_invalid_review_does_not_repeat_production(self):
        for kwargs in ({"unknown": True}, {"bad_quote": True}):
            with self.subTest(**kwargs):
                rt, task, prompts = self.run_case(**kwargs)
                self.assertEqual(len(prompts), 1)
                self.assertEqual(list(rt.log.iter_events(task_id=task, event_types=("work.repair_requested",))), [])


if __name__ == "__main__":
    unittest.main()
