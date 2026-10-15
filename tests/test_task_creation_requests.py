import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from cater.runtime import Runtime
from cater.task_requests import create
from cater.web import ActivityRunner
from cater.work import WorkConflict
from test_runtime import start_test_web


class TaskCreationRequestsTests(unittest.TestCase):
    def test_restart_preserves_identity_and_rejects_changed_parameters(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "tasks.db"
            request = {"goal": "Write a plan", "acceptance": ["Reviewed"], "run": True}
            with Runtime(db) as rt:
                task_id, created = create(rt, request, "request-creation-1")
                self.assertTrue(created)
                head = rt.log.head_hash()
            with Runtime(db) as rt:
                self.assertEqual(create(rt, request, "request-creation-1"), (task_id, False))
                with self.assertRaises(WorkConflict):
                    create(rt, {**request, "run": False}, "request-creation-1")
                self.assertEqual(rt.log.head_hash(), head)
                self.assertEqual(len(rt.tasks()), 1)

    def test_concurrent_creation_uses_one_atomic_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "tasks.db"
            with Runtime(db):
                pass
            barrier = threading.Barrier(4)

            def invoke(_):
                with Runtime(db) as rt:
                    barrier.wait(timeout=5)
                    return create(rt, {"goal": "One task"}, "concurrent-creation")

            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(invoke, range(4)))
            self.assertEqual(sum(result[1] for result in results), 1)
            self.assertEqual(len({result[0] for result in results}), 1)
            with Runtime(db) as rt:
                self.assertEqual([e.type for e in rt.log.read()], ["task.created", "task.started"])
                self.assertTrue(rt.verify_chain().ok)

    def test_http_lost_response_replay_does_not_repeat_intake_or_run(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = Path(tmp.name) / "http.db"
        base = start_test_web(self, db)
        payload = {"goal": "Write a note", "request_id": "http-creation-replay", "run": True,
                   "background": True}

        def post(data):
            request = Request(base + "/api/tasks", data=json.dumps(data).encode(),
                              headers={"Content-Type": "application/json"})
            with urlopen(request, timeout=5) as response:
                return response.status, json.load(response)

        with patch.object(ActivityRunner, "_start") as submit:
            first_status, first = post(payload)
            with Runtime(db) as rt:
                head = rt.log.head_hash()
            second_status, second = post(payload)
            self.assertEqual((first_status, second_status), (202, 200))
            self.assertEqual(first["task"]["id"], second["task"]["id"])
            submit.assert_called_once()
            with self.assertRaises(HTTPError) as error:
                post({**payload, "goal": "Changed"})
            self.assertEqual(error.exception.code, 409)
            with Runtime(db) as rt:
                self.assertEqual(rt.log.head_hash(), head)


if __name__ == "__main__":
    unittest.main()
