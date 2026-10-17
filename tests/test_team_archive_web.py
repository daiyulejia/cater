"""Loopback HTTP archive authorization and byte identity, with no model API."""

import hashlib
import json
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from cater.runtime import Runtime
from cater.team_archive import artifact_bytes
from test_runtime import start_test_web
import test_team_review_cycle as reviews


class TeamArchiveWebTests(unittest.TestCase):
    def setUp(self):
        self.f = reviews.TeamReviewCycleTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        project = self.f.bind_workspace()
        (project / "result.txt").write_bytes(b"archive HTTP fixture")
        result, = self.f.exchange(self.f.worker, ("publish_workspace_archive", {
            "filename": "project.zip", "summary": "Real bytes", "paths": ["result.txt"]}))
        self.assertEqual(result["status"], "ok")
        self.seq = result["artifact_seq"]
        self.artifact, = self.f.rt.task(self.f.task).deliverables
        self.expected = artifact_bytes(self.f.rt.log, self.artifact)
        self.other = self.f.rt.create_task("Other task must not see the archive")
        self.base = start_test_web(self, str(self.f.root / "test.db"),
            access_token="fixture-only-access", runtime_factory=lambda: Runtime(self.f.root / "test.db",
                roles_path=self.f.roles, drivers={"team_test": self.f.driver}, close_drivers_on_close=False))

    def get(self, suffix, *, task=None, token="fixture-only-access", status=200):
        request = Request(f"{self.base}/api/tasks/{task or self.f.task}/artifacts/{suffix}",
            headers={"X-Cater-Access-Token": token} if token else {})
        if status != 200:
            with self.assertRaises(HTTPError) as caught:
                urlopen(request, timeout=5)
            with caught.exception as response:
                self.assertEqual(response.code, status)
                body = response.read()
                self.assertNotIn(b"archive HTTP fixture", body)
            return
        with urlopen(request, timeout=5) as response:
            self.assertEqual(response.headers.get_content_type(), "application/zip")
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            body = response.read()
            self.assertEqual(int(response.headers["Content-Length"]), len(body))
            return body

    def test_requires_token_and_task_scoped_sequence_not_just_a_known_hash(self):
        self.get(self.seq, token="", status=401)
        self.get(self.seq, token="wrong", status=401)
        self.get(self.seq, task=self.other, status=404)
        self.get(self.artifact["content_sha256"], status=400)
        self.assertEqual(self.get(self.seq), self.expected)
        self.assertEqual(hashlib.sha256(self.expected).hexdigest(), self.artifact["content_sha256"])

    def test_missing_or_modified_blob_fails_without_falling_back_to_disk(self):
        db = self.f.rt.log.db
        with self.f.rt.log.atomic():
            db.execute("DROP TRIGGER cater_artifact_blobs_no_update")
            db.execute("UPDATE artifact_blobs SET content = ?", (b"tampered",))
        self.get(self.seq, status=409)
        self.assertTrue(self.f.rt.verify_chain().ok)

    def test_bad_sequence_and_plain_text_are_not_archive_downloads(self):
        for seq in ("0", "-1", "1/else", "1.0", "9" * 30):
            with self.subTest(seq=seq):
                self.get(seq, status=400)
        plain = self.f.publish("not a zip")
        self.get(plain, status=404)
        self.get(99999999, status=404)


if __name__ == "__main__":
    unittest.main()
