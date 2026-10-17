from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from cater.events import EventLog
from cater.runtime import Runtime
from cater.web import ActivityRunner, make_handler


class NoModelDriver:
    token_budget = None

    def __init__(self):
        self.calls = []

    def complete(self, **request):
        self.calls.append(request)
        raise AssertionError("Unexpected model call")

    tool_turn = complete


class ActivityInstanceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.db = Path(temporary.name) / "activity.db"
        self.driver = NoModelDriver()
        self.addCleanup(lambda: self.assertEqual(self.driver.calls, []))
        with self.factory() as runtime:
            self.task = runtime.create_task("Check activity instance identity")

    def factory(self):
        return Runtime(
            self.db,
            drivers={name: self.driver for name in ("product", "design", "dev", "test")},
            close_drivers_on_close=False,
        )

    def serve(self, runner):
        with patch("cater.web.ActivityRunner", return_value=runner):
            handler = make_handler(self.db, runtime_factory=self.factory)
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()

        def close():
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())

        self.addCleanup(close)
        return server

    def get(self, server, path="/api/task-summaries", etag=None):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        try:
            connection.request("GET", path, headers={"If-None-Match": etag} if etag else {})
            response = connection.getresponse()
            return response.status, response.headers, response.read()
        finally:
            connection.close()

    def summaries(self, server, etag=None):
        status, headers, raw = self.get(server, etag=etag)
        self.assertEqual(status, 200)
        return json.loads(raw)["tasks"], headers["ETag"]

    def assert_identity(self, server, runner, state):
        rows, tag = self.summaries(server)
        status, _, raw = self.get(server, f"/api/tasks/{self.task}")
        self.assertEqual(status, 200)
        detail = json.loads(raw)
        expected = runner.status(self.task)
        self.assertEqual(expected["state"], state)
        for activity in (rows[0]["activity"], detail["activity"], detail["summary"]["activity"]):
            self.assertEqual(activity["instance_id"], runner.instance_id)
            self.assertEqual(activity["revision"], expected["revision"])
            self.assertEqual(activity["state"], state)
        return tag

    def head(self):
        with self.factory() as runtime:
            return runtime.log.head_hash()

    def test_instance_id_is_read_only_uuid4_and_snapshot_stays_pair(self):
        runner = ActivityRunner(self.factory)
        identity = runner.instance_id
        parsed = uuid.UUID(hex=identity)
        self.assertEqual(parsed.version, 4)
        self.assertEqual(parsed.hex, identity)
        with self.assertRaises(AttributeError):
            runner.instance_id = uuid.uuid4().hex
        self.assertEqual(runner.snapshot(), (0, {}))
        status = runner.status(self.task)
        status["instance_id"] = "changed"
        self.assertEqual(runner.status(self.task)["instance_id"], identity)
        self.assertEqual(runner.snapshot(), (0, {}))

    def test_same_instance_etag_is_stable_and_304_reads_no_history(self):
        runner = ActivityRunner(self.factory)
        server = self.serve(runner)
        tag = self.assert_identity(server, runner, "idle")
        with patch.object(Runtime, "tasks", side_effect=AssertionError("Unexpected fold")), \
                patch.object(EventLog, "read", side_effect=AssertionError("Unexpected history read")), \
                patch.object(EventLog, "iter_events", side_effect=AssertionError("Unexpected history scan")), \
                patch.object(EventLog, "_row_to_event", side_effect=AssertionError("Unexpected event decode")):
            self.assertEqual(self.summaries(server)[1], tag)
            for condition in (tag, tag.removeprefix("W/")):
                status, headers, raw = self.get(server, etag=condition)
                self.assertEqual(status, 304)
                self.assertEqual(headers["ETag"], tag)
                self.assertEqual(raw, b"")

    def test_new_instance_same_head_and_revision_returns_200(self):
        first = ActivityRunner(self.factory)
        first_server = self.serve(first)
        head = self.head()
        _, old_tag = self.summaries(first_server)
        second = ActivityRunner(self.factory)
        second_server = self.serve(second)
        self.assertEqual(first.snapshot()[0], second.snapshot()[0])
        self.assertEqual(self.head(), head)
        self.assertNotEqual(first.instance_id, second.instance_id)
        rows, new_tag = self.summaries(second_server, old_tag)
        self.assertNotEqual(old_tag, new_tag)
        self.assertEqual(rows[0]["activity"]["instance_id"], second.instance_id)
        self.assertEqual(self.assert_identity(second_server, second, "idle"), new_tag)
        self.assertEqual(self.get(first_server, etag=old_tag)[0], 304)

    def test_revision_is_per_instance_and_identity_survives_state_changes(self):
        first, second = ActivityRunner(self.factory), ActivityRunner(self.factory)
        first_server, second_server = self.serve(first), self.serve(second)
        identity = first.instance_id
        with patch.object(first, "_start"):
            queued = first.submit(self.task, "run")
        queued_tag = self.assert_identity(first_server, first, "queued")
        _, second_tag = self.summaries(second_server)
        revision, rows = first.snapshot()
        self.assertGreater(revision, 0)
        self.assertEqual(rows[self.task]["instance_id"], identity)
        self.assertEqual(second.snapshot(), (0, {}))
        head = self.head()
        self.assertTrue(first._mark_running(self.task, queued["id"]))
        running_tag = self.assert_identity(first_server, first, "running")
        self.assertNotEqual(queued_tag, running_tag)
        self.assertGreater(first.snapshot()[0], revision)
        self.assertEqual(second.status(self.task)["revision"], 0)
        self.assertEqual(self.get(second_server, etag=second_tag)[0], 304)
        first._finish(self.task, queued["id"], "completed")
        self.assertNotEqual(self.assert_identity(first_server, first, "completed"), running_tag)
        self.assertEqual(first.instance_id, identity)
        self.assertEqual(self.head(), head)
        self.assertEqual(second.snapshot(), (0, {}))


if __name__ == "__main__":
    unittest.main()
