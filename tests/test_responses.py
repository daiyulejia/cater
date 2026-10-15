from __future__ import annotations

import json
import unittest

from cater.responses import load_response_object
from cater.runtime import Runtime
from cater.drivers import DriverResponse
from test_work import WorkDriver, item


class ResponseObjectTests(unittest.TestCase):
    def test_transport_wrappers_preserve_the_exact_decoded_object_and_artifact_body(self):
        body = '# Example\n```js\nconsole.log("hi");\n```\nLiteral \\n remains literal.'
        expected = {"text": "Ready", "deliverables": [{"content": body}]}
        raw = json.dumps(expected, ensure_ascii=False)
        for wrapped in (raw, " \n" + raw + "\t", "\ufeff" + raw,
                        "```json\n" + raw + "\n```", "```JSON\r\n" + raw + "\r\n```",
                        "```\n" + raw + "\n```", raw + "\n```", raw + "\r\n```\n"):
            with self.subTest(wrapped=wrapped[:35]):
                self.assertEqual(load_response_object(wrapped), expected)

    def test_ambiguous_partial_non_object_and_duplicate_responses_are_rejected(self):
        for raw in ('prefix {"action":"resume_task"}', '{"a":1} suffix', '{} {}', '[]', 'null',
                    '"{}"', '{"a":1,"a":2}', '{"nested":{"a":1,"a":2}}', '{"x":NaN}',
                    '{"x":Infinity}', '```json\n{}', '```python\n{}\n```', '{}\n```\nextra',
                    '```json\n{}\n```\n{}', '{"content":"unfinished\n```', None):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                load_response_object(raw)

    def test_fenced_work_and_review_results_reach_the_dependent_worker(self):
        class WrappedDriver(WorkDriver):
            def complete(self, **kwargs):
                response = super().complete(**kwargs)
                return DriverResponse(response.text + "\n```", response.tokens, response.meta)

        driver = WrappedDriver()
        with Runtime(":memory:", drivers={name: driver for name in ("product", "design", "dev", "test")}) as rt:
            task = rt.create_task("Preserve results through transport formatting")
            rt.plan_work(task, [item("first"), item("second", deps=["first"])])
            rt.collaborate(task)
            self.assertTrue(rt.work.view(task)["stage_accepted"])
            self.assertEqual(len([c for c in driver.calls if c.get("phase") != "review"]), 2)
            next_call = next(c for c in driver.calls if c.get("work_item_id") == "second" and c.get("phase") != "review")
            self.assertEqual(next_call["accepted_inputs"]["first"][0]["content"], "assert 2 + 2 == 4\n")
            self.assertFalse(list(rt.log.iter_events(task_id=task, event_types=("work.failed",))))

    def test_invalid_work_response_is_reported_without_publishing_raw_protocol_as_chat(self):
        class InvalidDriver(WorkDriver):
            def complete(self, **kwargs):
                return DriverResponse('{"deliverables":[secret-protocol-data', 7, {"token_usage": "reported"})

        with Runtime(":memory:", drivers={"dev": InvalidDriver()}) as rt:
            task = rt.create_task("Invalid response is not an empty successful contribution")
            rt.plan_work(task, [item("first"), item("second", deps=["first"])])
            rt.collaborate(task)
            failed = list(rt.log.iter_events(task_id=task, event_types=("work.failed",)))
            self.assertIn("回应格式", failed[0].payload["reason"])
            messages = list(rt.log.iter_events(task_id=task, event_types=("msg.said",)))
            self.assertEqual(messages[0].payload["phase"], "response_format_error")
            self.assertNotIn("secret-protocol-data", messages[0].payload["text"])
            self.assertFalse(list(rt.log.iter_events(task_id=task, event_types=("deliverable.published",))))
            self.assertEqual(rt.work.view(task)["items"][1]["status"], "blocked")


if __name__ == "__main__":
    unittest.main()
