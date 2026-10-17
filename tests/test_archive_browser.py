"""Published ZIP inputs and version-bound browser receipts, with a local stub."""

import copy
import hashlib
import json
import unittest
from unittest.mock import patch

from cater.browser_checks import normalize_plan, validate_browser_paths
import test_team_review_cycle as reviews
from test_team_browser import result_for


PLAN = {"entrypoint": "web/index.html", "steps": [
    {"action": "assert_text", "label": "Check result", "selector": "#result", "text": "3"}]}


class ArchiveBrowserTests(unittest.TestCase):
    def setUp(self):
        self.f = reviews.TeamReviewCycleTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.root = self.f.bind_workspace()
        self.files = {"web/index.html": b'<p id="result">3</p><script src="js/app.js"></script>',
            "web/js/app.js": b"globalThis.ready=true;", "assets/image.png": b"\x89PNG\x00\xff",
            "empty.txt": b""}
        for name, body in self.files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
        self.f.rt.browser_checks.command = ["trusted-fixture"]
        self.seq = self.publish()

    def publish(self, filename="project.zip", paths=None):
        response, = self.f.exchange(self.f.worker, ("publish_workspace_archive", {
            "filename": filename, "summary": "Actual fixture files", "paths": paths or list(self.files)}))
        self.assertEqual(response["status"], "ok", response)
        return response["artifact_seq"]

    def args(self, **changes):
        return {"artifact_seqs": [self.seq], "plan": copy.deepcopy(PLAN), **changes}

    def execute(self, arguments=None, **kwargs):
        return self.f.rt.team.browser.execute(self.f.task, arguments or self.args(), current=kwargs.get("current", lambda: None))

    def runner(self, command, *, cwd, **kwargs):
        plan = json.loads((cwd / ".cater-browser-check.json").read_text(encoding="utf-8"))
        self.assertEqual(plan["files_layout"], "relative_paths_v1")
        for name in plan["files"]:
            self.assertEqual((cwd / name).read_bytes(), self.files[name])
        self.assertNotIn("CATER_INTERN_API_KEY", kwargs["env"])
        return result_for(plan)

    def test_directory_binary_and_empty_copy_bind_to_zip_not_member_events(self):
        with patch("cater.team_browser.run_check", side_effect=self.runner):
            result = self.execute()
        self.assertEqual(result["outcome"], "passed", result)
        self.assertTrue(result["scope_current"])
        self.assertTrue(result["artifacts_unchanged"])
        self.assertEqual([a["seq"] for a in result["checked_artifacts"]], [self.seq])
        self.assertEqual(result["checked_artifacts"][0]["filename"], "project.zip")
        self.assertEqual({a["path"] for a in result["checked_files"]}, set(self.files))
        for file in result["checked_files"]:
            self.assertEqual(file["artifact_seq"], self.seq)
            self.assertEqual(file["sha256"], hashlib.sha256(self.files[file["path"]]).hexdigest())
        self.assertNotIn("globalThis.ready", json.dumps(result))

    def test_subset_only_copies_explicit_members_and_preserves_zip_reference(self):
        paths = ["web/index.html", "web/js/app.js"]
        args = self.args(archive_files=[{"artifact_seq": self.seq, "paths": paths}])
        def run(*a, **kw):
            self.assertFalse((kw["cwd"] / "assets").exists())
            self.assertFalse((kw["cwd"] / "empty.txt").exists())
            return self.runner(*a, **kw)
        with patch("cater.team_browser.run_check", side_effect=run):
            result = self.execute(args)
        self.assertEqual(result["outcome"], "passed")
        self.assertEqual({r["path"] for r in result["checked_files"]}, set(paths))
        self.assertEqual(result["archive_files"], args["archive_files"])

    def test_bad_filters_do_not_launch_or_read_arbitrary_sources(self):
        invalid = (None, [], {}, [True], [{"artifact_seq": True, "paths": ["web/index.html"]}],
            [{"artifact_seq": self.seq + 999, "paths": ["web/index.html"]}],
            [{"artifact_seq": self.seq, "paths": ["missing.html"]}],
            [{"artifact_seq": self.seq, "paths": ["../outside"]}],
            [{"artifact_seq": self.seq, "paths": ["web/index.html"]}] * 2)
        for selection in invalid:
            with self.subTest(selection=selection), patch("cater.team_browser.run_check") as run:
                result = self.execute(self.args(archive_files=selection))
                self.assertEqual(result["status"], "error")
                run.assert_not_called()

    def test_two_archives_and_plain_files_mix_but_collisions_never_override(self):
        other = self.publish("dependency.zip", ["empty.txt"])
        plain = self.f.publish("external text", filename="notes.txt")
        args = self.args(artifact_seqs=[self.seq, other, plain],
            archive_files=[{"artifact_seq": self.seq, "paths": ["web/index.html", "web/js/app.js"]}])
        def run(command, *, cwd, **kw):
            plan = json.loads((cwd / ".cater-browser-check.json").read_text(encoding="utf-8"))
            self.assertEqual((cwd / "notes.txt").read_text(), "external text")
            self.assertEqual((cwd / "empty.txt").read_bytes(), b"")
            return result_for(plan)
        with patch("cater.team_browser.run_check", side_effect=run):
            result = self.execute(args)
        self.assertEqual(result["outcome"], "passed")
        self.assertEqual(len(result["checked_artifacts"]), 3)
        with patch("cater.team_browser.run_check") as run:
            self.assertEqual(self.execute(self.args(artifact_seqs=[self.seq, other]))["status"], "error")
            run.assert_not_called()

    def test_workspace_changed_after_publish_does_not_change_checked_bytes(self):
        (self.root / "web/js/app.js").write_bytes(b"unpublished mutation")
        with patch("cater.team_browser.run_check", side_effect=self.runner):
            result = self.execute()
        self.assertEqual(result["outcome"], "passed")
        self.assertTrue(result["scope_current"])

    def test_copy_mutation_is_not_a_valid_attestation(self):
        def run(*args, **kw):
            result = self.runner(*args, **kw)
            (kw["cwd"] / "web/js/app.js").write_bytes(b"modified")
            return result
        with patch("cater.team_browser.run_check", side_effect=run):
            result = self.execute()
        self.assertEqual(result["outcome"], "unavailable")
        self.assertFalse(result["artifacts_unchanged"])

    def test_new_publication_marks_old_check_historical_without_switching_inputs(self):
        def run(*a, **kw):
            result = self.runner(*a, **kw)
            self.publish()
            return result
        with patch("cater.team_browser.run_check", side_effect=run):
            result = self.execute()
        self.assertEqual(result["outcome"], "passed")
        self.assertFalse(result["scope_current"])
        self.assertEqual(result["checked_artifacts"][0]["seq"], self.seq)

    def test_relative_plan_preserves_legacy_and_rejects_unsafe_names(self):
        old = normalize_plan({"entrypoint": "index.html", "steps": PLAN["steps"]}, ["index.html"])
        self.assertNotIn("files_layout", old)
        new = normalize_plan(PLAN, ["web/index.html"], relative_paths=True)
        self.assertEqual(new["files_layout"], "relative_paths_v1")
        for paths in (["../index.html"], ["C:/index.html"], ["a\\b"], [".hidden/index.html"],
            ["a%20.js"], ["a#b.js"], ["\uff0ehidden/a.js"], ["a\uff05.js"],
            ["CON/x"], ["d/a", "D/b"], ["d", "d/a"], ["ss/a", "\u00df/b"]):
            with self.subTest(paths=paths), self.assertRaises(ValueError):
                validate_browser_paths(paths)


if __name__ == "__main__":
    unittest.main()
