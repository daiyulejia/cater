import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from cater.artifact_edits import revise_artifacts
from cater.drivers import DriverResponse
from cater.runtime import Runtime
from test_work import WorkDriver, item


class ArtifactEditTests(unittest.TestCase):
    def setUp(self):
        self.source = {"seq": 10, "filename": "game.html", "format": "code", "title": "Game",
                       "content": "const value = 1;\nrender(value);\n"}
        self.source["content_sha256"] = hashlib.sha256(self.source["content"].encode()).hexdigest()
        self.context = {"accepted_inputs": {"code": [self.source]}}
        self.edit = {"artifact_seq": 10, "expected_sha256": self.source["content_sha256"],
                     "old_text": "const value = 1;", "new_text": "const value = 2;"}

    def test_exact_revision_produces_complete_file_without_mutating_input(self):
        original = copy.deepcopy(self.context)
        result = revise_artifacts([self.edit], self.context)
        self.assertEqual(result[0]["content"], "const value = 2;\nrender(value);\n")
        self.assertEqual(result[0]["filename"], "game.html")
        self.assertEqual(self.context, original)

    def test_prior_own_submission_is_available_but_unrelated_source_is_not(self):
        context = {"previous_attempt": {"artifacts": [self.source]}}
        self.assertEqual(revise_artifacts([self.edit], context)[0]["filename"], "game.html")
        with self.assertRaises(ValueError):
            revise_artifacts([self.edit], {})

    def test_host_binds_immutable_hash_without_requiring_the_model_to_transcribe_it(self):
        edit = {k: v for k, v in self.edit.items() if k != "expected_sha256"}
        self.assertIn("value = 2", revise_artifacts([edit], self.context)[0]["content"])

    def test_wrong_version_missing_and_ambiguous_text_never_guess_a_patch(self):
        for change in ({"expected_sha256": "old"}, {"artifact_seq": 99}, {"artifact_seq": True},
                       {"old_text": "absent"}, {"old_text": ""}, {"old_text": "value"},
                       {"extra": "command"}, {"new_text": "x" * 65537}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                revise_artifacts([{**self.edit, **change}], self.context)
        with self.assertRaises(ValueError):
            revise_artifacts([self.edit], {**self.context, "phase": "review"})

    def test_multiple_changes_to_one_file_are_sequential_and_all_or_nothing(self):
        second = {**self.edit, "old_text": "render(value);", "new_text": "render(value + 1);"}
        result = revise_artifacts([self.edit, second], self.context)
        self.assertEqual(len(result), 1)
        self.assertIn("render(value + 1)", result[0]["content"])
        with self.assertRaises(ValueError):
            revise_artifacts([self.edit, {**second, "old_text": "missing"}], self.context)
        self.assertEqual(self.source["content"], "const value = 1;\nrender(value);\n")


class ArtifactEditPipelineTests(unittest.TestCase):
    def test_repair_publishes_complete_new_version_and_rechecks_instead_of_large_rewrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            driver = WorkDriver()
            driver.status = "failed"
            with Runtime(Path(tmp) / "edits.db", drivers={r: driver for r in ("product", "dev", "design", "test")}) as rt:
                task = rt.create_task("Repair the delivered code")
                rt.plan_work(task, [item("code")])
                rt.collaborate(task)
                original = next(rt.log.iter_events(task_id=task, event_types=("deliverable.published",)))
                complete = driver.complete

                def respond(*, role, prompt, context):
                    work = context["work"]
                    if work.get("previous_attempt") and work.get("phase") != "review":
                        source = work["previous_attempt"]["artifacts"][0]
                        return DriverResponse(json.dumps({"text": "Fixed the assertion with a small revision.",
                            "artifact_edits": [{"artifact_seq": source["seq"],
                                "expected_sha256": source["content_sha256"],
                                "old_text": "2 + 2 == 4", "new_text": "3 + 3 == 6"}]}), 2, {})
                    return complete(role=role, prompt=prompt, context=context)

                driver.complete, driver.status = respond, "passed"
                rt.collaborate(task)
                artifacts = list(rt.log.iter_events(task_id=task, event_types=("deliverable.published",)))
                self.assertEqual(len(artifacts), 2)
                self.assertEqual(original.payload["content"], "assert 2 + 2 == 4\n")
                self.assertEqual(artifacts[1].payload["content"], "assert 3 + 3 == 6\n")
                self.assertTrue(rt.work.view(task)["stage_accepted"])
                self.assertTrue(rt.verify_chain().ok)
