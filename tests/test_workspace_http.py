"""Creation and workspace authorization share a durable request identity."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from cater.runtime import Runtime
from cater.web import ActivityRunner
from test_runtime import start_test_web


class WorkspaceHTTPTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        (self.base / "project").mkdir()
        self.db = self.base / "tasks.db"
        self.config = self.base / "workspaces.json"
        self.config.write_text(json.dumps({"workspaces": [{"id": "source", "title": "Source", "root": str(self.base / 'project'), "mode": "edit"}]}), encoding="utf-8")
        self.url = start_test_web(self, self.db, workspaces_path=self.config)

    def post(self, path, body):
        with urlopen(Request(self.url + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}), timeout=5) as response:
            return response.status, json.load(response)

    def test_creation_binds_before_activation_and_replay_does_not_rebind(self):
        data = {"goal": "Inspect project", "workspace_id": "source", "request_id": "workspace-creation-1", "run": True, "background": True}
        with patch.object(ActivityRunner, "_start") as start:
            status, first = self.post("/api/tasks", data)
            self.assertEqual(status, 202)
            self.assertEqual(first["workspace"]["workspace_id"], "source")
            task = first["task"]["id"]
            with Runtime(self.db, workspaces_path=self.config) as rt:
                head = rt.log.head_hash()
                events = rt.log.read(task_id=task)
                bound = next(e for e in events if e.type == "workspace.bound")
                activation = next(e for e in events if e.type == "task.activation_requested")
                self.assertLess(bound.seq, activation.seq)
                self.assertEqual(activation.payload["scope"]["workspace"]["revision"], bound.seq)
            self.assertEqual(self.post("/api/tasks", data)[1]["task"]["id"], task)
            with self.assertRaises(HTTPError) as error:
                self.post("/api/tasks", {**data, "workspace_id": None})
            self.assertEqual(error.exception.code, 409)
            with Runtime(self.db, workspaces_path=self.config) as rt:
                self.assertEqual(rt.log.head_hash(), head)
            self.assertEqual(start.call_count, 1)

    def test_invalid_workspace_rolls_back_creation(self):
        for identity in ("not-configured", {}, []):
            with self.assertRaises(HTTPError) as error:
                self.post("/api/tasks", {"goal": "Inspect project", "workspace_id": identity, "request_id": "invalid-workspace-1"})
            self.assertEqual(error.exception.code, 400)
            with Runtime(self.db, workspaces_path=self.config) as rt:
                self.assertEqual(rt.tasks(), [])
                self.assertTrue(rt.verify_chain().ok)

    def test_binding_endpoint_rejects_arbitrary_policy_and_stale_revision(self):
        _, detail = self.post("/api/tasks", {"goal": "Inspect"})
        path = "/api/tasks/" + detail["task"]["id"] + "/workspace"
        with self.assertRaises(HTTPError) as error:
            self.post(path, {"workspace_id": "source", "expected_revision": None, "root": str(self.base)})
        self.assertEqual(error.exception.code, 400)
        _, bound = self.post(path, {"workspace_id": "source", "expected_revision": None})
        with self.assertRaises(HTTPError) as error:
            self.post(path, {"workspace_id": None, "expected_revision": None})
        self.assertEqual(error.exception.code, 409)
        _, unbound = self.post(path, {"workspace_id": None, "expected_revision": bound["workspace"]["revision"]})
        self.assertIsNone(unbound["workspace"]["workspace_id"])


if __name__ == "__main__":
    unittest.main()
