"""Long Windows publication paths survive review, delivery and database reopen."""

import io
import os
from pathlib import Path
import shutil
import unittest
import zipfile

from cater.artifact_store import _extended_windows_path
from cater.runtime import Runtime
import test_team_review_cycle as reviews


@unittest.skipUnless(os.name == "nt", "Windows namespace regression")
class TeamLongPathTests(unittest.TestCase):
    def test_workspace_publication_review_download_and_reopen(self):
        self._delivery_cycle(archive=False)

    def test_workspace_archive_review_download_and_reopen(self):
        self._delivery_cycle(archive=True)

    def _delivery_cycle(self, *, archive):
        f = reviews.TeamReviewCycleTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        project = f.bind_workspace()
        root = f.root / ("artifact-storage-" + "x" * 90)
        f.rt._artifact_root = root
        self.addCleanup(shutil.rmtree, _extended_windows_path(root), True)
        filename = "CONTRIBUTING_CHECKLIST.md"
        body = "# Verified delivery\n\nRun the tests before publishing.\n".encode()
        (project / filename).write_bytes(body)
        tool, arguments = "publish_workspace_file", {
            "path": filename, "summary": "Complete workspace document", "format": "markdown",
        }
        if archive:
            tool, arguments = "publish_workspace_archive", {
                "paths": [filename], "filename": "project.zip", "summary": "Frozen project",
            }
        result, = f.exchange(f.worker, (tool, arguments))
        self.assertEqual(result["status"], "ok", result)
        artifact, = f.rt.task(f.task).deliverables
        saved = Path(artifact["local_path"])
        self.assertGreater(len(str(saved)), 260)
        expected = saved.read_bytes() if archive else body
        if archive:
            with zipfile.ZipFile(io.BytesIO(expected)) as snapshot:
                self.assertEqual(snapshot.read(filename), body)
        self.assertEqual(saved.read_bytes(), expected)
        self.assertEqual(f.rt.team._pending_operations(f.task), [])
        read_args = {"artifact_seq": artifact["seq"]}
        if archive:
            read_args["path"] = filename
        read, = f.exchange(f.reviewer, ("read_artifact", read_args))
        self.assertEqual(read["content"].encode(), body)
        self.assertEqual(f.request([artifact["seq"]])["status"], "ok")
        row, = f.rt.team_reviews.view(f.task)
        self.assertEqual(f.record(row["request_seq"])["status"], "ok")
        f.acknowledge_latest_review()
        self.assertEqual(f.finish()["status"], "ok")
        (project / filename).write_bytes(b"Unapproved later edit")
        with zipfile.ZipFile(io.BytesIO(f.rt.team.archive(f.task))) as downloaded:
            self.assertEqual(downloaded.read(artifact["filename"]), expected)
        with Runtime(f.root / "test.db", roles_path=f.roles,
                     drivers={"team_test": f.driver}) as reopened:
            restored, = reopened.task(f.task).deliverables
            self.assertEqual(Path(restored["local_path"]).read_bytes(), expected)
            with zipfile.ZipFile(io.BytesIO(reopened.team.archive(f.task))) as downloaded:
                self.assertEqual(downloaded.read(artifact["filename"]), expected)
            self.assertEqual(reopened.team._pending_operations(f.task), [])
            self.assertTrue(reopened.verify_chain().ok)
