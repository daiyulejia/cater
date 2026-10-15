import json
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from cater.runtime import Runtime
from cater.web import ActivityRunner
from test_runtime import start_test_web


class ExecutionControlWebTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name) / "control.db"
        with Runtime(self.db) as rt:
            self.task = rt.create_task("Keep the same task while pausing")
            self.scope = rt.work.scope(self.task)
        self.base = start_test_web(self, self.db)

    def post(self, payload):
        req = Request(self.base + f"/api/tasks/{self.task}/control",
                      data=json.dumps(payload).encode(),
                      headers={"Content-Type": "application/json"})
        with urlopen(req, timeout=5) as response:
            return json.load(response)

    def test_pause_is_visible_persistent_and_does_not_start_execution(self):
        with patch.object(ActivityRunner, "_start") as start:
            response = self.post({"action": "pause", "request_id": "web-pause-1"})
            duplicate = self.post({"action": "pause", "request_id": "web-pause-1"})
            self.assertEqual(response["execution_control"]["state"], "paused")
            self.assertEqual(response["execution_control"], duplicate["execution_control"])
            start.assert_not_called()
        with Runtime(self.db) as rt:
            self.assertEqual(rt.execution_control.view(self.task), response["execution_control"])
            self.assertEqual(rt.work.scope(self.task), self.scope)

    def test_resume_replay_schedules_only_one_activity(self):
        self.post({"action": "pause", "request_id": "web-before-resume"})
        with patch.object(ActivityRunner, "_start") as start:
            response = self.post({"action": "resume", "request_id": "web-resume-1"})
            self.post({"action": "resume", "request_id": "web-resume-1"})
            self.assertEqual(response["execution_control"]["state"], "running")
            start.assert_called_once()

    def test_summary_does_not_hide_pause_behind_in_flight_activity(self):
        self.post({"action": "pause", "request_id": "summary-pause-1"})
        with urlopen(self.base + "/api/task-summaries", timeout=5) as response:
            summary = json.load(response)["tasks"][0]
        self.assertEqual(summary["execution_control"]["state"], "paused")
        self.assertEqual(summary["status_label"], "已暂停")
        from cater.web import task_api_payload
        with Runtime(self.db) as rt:
            payload = task_api_payload(rt, self.task, activity={"state": "running", "active": True})
        self.assertEqual(payload["summary"]["status_label"], "已暂停")
        self.assertTrue(payload["activity"]["active"])

    def test_cancel_rejects_resume_and_invalid_controls_never_write(self):
        self.post({"action": "cancel", "request_id": "web-cancel-1"})
        with Runtime(self.db) as rt:
            head = rt.log.head_hash()
        for payload, status in (({"action": "resume", "request_id": "web-cancel-resume"}, 409),
                                ({"action": "pause", "request_id": "x"}, 400),
                                ({"action": "unknown", "request_id": "web-invalid-1"}, 400),
                                ({"action": ["pause"], "request_id": "web-invalid-3"}, 400),
                                ({"action": "cancel", "request_id": "web-invalid-2", "state": "running"}, 400)):
            with self.subTest(payload=payload):
                with self.assertRaises(HTTPError) as error:
                    self.post(payload)
                self.assertEqual(error.exception.code, status)
        with Runtime(self.db) as rt:
            self.assertEqual(rt.log.head_hash(), head)


if __name__ == "__main__":
    unittest.main()
