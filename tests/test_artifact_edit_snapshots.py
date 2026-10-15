import copy
import hashlib
import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from cater.artifact_edits import prepare_snapshot
from cater.browser_checks import PLAN_FILE, normalize_plan
from cater.drivers import DriverResponse
from cater.runtime import Runtime
from cater.verification import CheckResult
from test_work import WorkDriver, item


HTML = '<!doctype html><link rel="stylesheet" href="style.css"><p id="result">Old</p>'
CSS = 'body { color: red; }'
PLAN = {"entrypoint": "index.html", "steps": [
    {"action": "assert_text", "label": "Observe revision", "selector": "#result", "text": "New"}]}


def file(name, content):
    return {"title": name, "summary": "Fixture source", "filename": name, "format": "code", "content": content}


def edit(source, old="Old", new="New"):
    return {"artifact_seq": source["seq"], "old_text": old, "new_text": new}


class SnapshotDriver(WorkDriver):
    def __init__(self):
        super().__init__()
        self.author = lambda work: {"deliverables": [file("index.html", HTML), file("style.css", CSS)]}

    def complete(self, *, role, prompt, context):
        work = context["work"]
        if work.get("phase") == "delivery":
            return DriverResponse(json.dumps({"decision": "approve", "reason": "Inspected the current bundle.",
                "handoff": {"summary": "Revised page", "why": "Retained exact checked sources.",
                            "reproduce": ["Open index.html"], "limits": ["Fixture checks only"]}}), 1, {})
        if work.get("phase") == "review":
            return super().complete(role=role, prompt=prompt, context=context)
        self.calls.append(copy.deepcopy(work))
        return DriverResponse(json.dumps({"text": "Submitted fixture snapshot", **self.author(work)}), 1, {})


class ArtifactEditSnapshotTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        config = json.loads(Path("cater/packs/default_roles.json").read_text(encoding="utf-8"))
        config["organization"]["leader"]["driver"] = "final"
        roles = self.root / "roles.json"
        roles.write_text(json.dumps(config), encoding="utf-8")
        self.driver = SnapshotDriver()
        self.rt = Runtime(self.root / "snapshot.db", roles_path=roles,
            drivers={key: self.driver for key in ("dev", "design", "test", "product", "final")})
        self.addCleanup(self.rt.close)
        self.task = self.rt.create_task("Deliver the exact revised static page")

    def rows(self):
        return {i["id"]: i for i in self.rt.work.view(self.task)["items"]}

    def artifacts(self, identity):
        return self.rt.work.artifacts(self.task, self.rows()[identity]["submission"])

    def events(self, kind="deliverable.published"):
        return list(self.rt.log.iter_events(task_id=self.task, event_types=(kind,)))

    def previous_source(self, work, name="style.css"):
        return next(a for a in work["previous_attempt"]["artifacts"] if a["filename"] == name)

    def retry(self, extra=()):
        self.rt.work.rework(self.task, "page", "Correct the source color")
        self.driver.author = lambda work: {"artifact_edits": [edit(self.previous_source(work), "red", "green")],
                                           "deliverables": list(extra)}
        self.rt.collaborate(self.task)

    def test_own_multifile_retry_preserves_unchanged_html_with_host_provenance(self):
        self.rt.plan_work(self.task, [item("page")])
        self.rt.collaborate(self.task)
        original = {a["filename"]: a for a in self.artifacts("page")}
        self.retry()
        revised = {a["filename"]: a for a in self.artifacts("page")}
        self.assertEqual(set(revised), {"index.html", "style.css"})
        self.assertEqual(revised["index.html"]["content"], HTML)
        self.assertEqual(revised["style.css"]["content"], CSS.replace("red", "green"))
        for name, artifact in revised.items():
            self.assertEqual(artifact["revision_source"]["artifact_seq"], original[name]["seq"])
            self.assertEqual(artifact["revision_source"]["content_sha256"], original[name]["content_sha256"])
        self.assertEqual(len(self.events()), 4)
        self.assertTrue(self.rt.work.view(self.task)["stage_accepted"])
        self.assertEqual(set(self.rt.delivery._files(self.task, self.rt.delivery._manifest(self.task))),
                         {"index.html", "style.css"})
        self.assertTrue(self.rt.verify_chain().ok)

    def test_explicit_file_can_replace_inherited_file(self):
        self.rt.plan_work(self.task, [item("page")])
        self.rt.collaborate(self.task)
        replacement = HTML.replace("Old", "Explicit")
        self.retry([file("INDEX.HTML", replacement)])
        revised = {a["filename"]: a["content"] for a in self.artifacts("page")}
        self.assertEqual(revised, {"INDEX.HTML": replacement, "style.css": CSS.replace("red", "green")})

    def test_explicit_file_cannot_override_patched_file(self):
        self.rt.plan_work(self.task, [item("page")])
        self.rt.collaborate(self.task)
        count = len(self.events())
        self.retry([file("STYLE.CSS", "body {}")])
        self.assertEqual(len(self.events()), count)
        self.assertFalse(self.rt.work.view(self.task)["stage_accepted"])

    def test_total_including_inherited_files_is_rejected_without_truncation(self):
        self.driver.author = lambda work: {"deliverables": [file("index.html", HTML), file("style.css", CSS),
            *[file(f"asset-{i}.js", f"const value{i} = 1;") for i in range(6)]]}
        self.rt.plan_work(self.task, [item("page")])
        self.rt.collaborate(self.task)
        self.assertEqual(len(self.artifacts("page")), 8)
        self.retry([file("extra.js", "const extra = 1;")])
        self.assertEqual(len(self.events()), 8)
        self.assertEqual(len(self.events("work.submitted")), 1)
        failures = [e for e in self.rt.log.read(task_id=self.task)
                    if e.payload.get("phase") == "response_format_error"]
        self.assertIn("8-file limit", failures[-1].payload["text"])
        self.assertFalse(self.rt.work.view(self.task)["stage_accepted"])

    def test_exactly_eight_files_are_preserved_on_retry(self):
        self.driver.author = lambda work: {"deliverables": [file("index.html", HTML), file("style.css", CSS),
            *[file(f"asset-{i}.js", f"const value{i} = 1;") for i in range(6)]]}
        self.rt.plan_work(self.task, [item("page")])
        self.rt.collaborate(self.task)
        self.retry()
        self.assertEqual(len(self.artifacts("page")), 8)
        self.assertEqual(len(self.events()), 16)

    def downstream(self, *, local=False, forged=False):
        def author(work):
            if work["work_item_id"] == "base":
                return {"deliverables": [file("index.html", HTML), file("style.css", CSS)]}
            source = work["accepted_inputs"]["base"][0]
            if forged:
                submission = self.rows()["base"]["submission"]
                event = next(e for e in self.events() if e.seq == source["seq"])
                proof = {"artifact_seq": source["seq"], "content_sha256": source["content_sha256"],
                         "event_hash": event.hash, "submission_seq": submission["seq"],
                         "submission_digest": submission["digest"], "relation": "accepted_input"}
                return {"deliverables": [{**file("index.html", HTML.replace("Old", "New")),
                                          "revision_source": proof, "metadata": {"revision_source": proof}}]}
            return {"artifact_edits": [edit(source)]}
        self.driver.author = author
        self.rt.plan_work(self.task, [item("base"), item("repair", owner="design", deps=["base"], local=local)])
        self.rt.collaborate(self.task)

    def execute(self, command, *, cwd, **kwargs):
        files = {str(p.relative_to(cwd)).replace("\\", "/"): p.read_text(encoding="utf-8")
                 for p in cwd.rglob("*") if p.is_file() and p.name != PLAN_FILE}
        self.assertEqual(files, {"index.html": HTML.replace("Old", "New"), "style.css": CSS})
        if (cwd / PLAN_FILE).exists():
            plan = json.loads((cwd / PLAN_FILE).read_text(encoding="utf-8"))
            report = {"outcome": "passed", "plan_sha256": hashlib.sha256(
                json.dumps(plan, ensure_ascii=False).encode()).hexdigest(), "counts": {"page_errors": 0},
                "steps": [{"index": i, "action": step["action"], "status": "passed", "actual": True}
                          for i, step in enumerate(plan["steps"], 1)]}
            output = json.dumps(report)
        else:
            output = "checked exact bundle"
        summary = {"complete": True, "truncated": False}
        return CheckResult({"command": command, "outcome": "passed", "exit_code": 0,
                            "stdout": summary, "stderr": summary}, output, "")

    def test_downstream_patch_checks_and_archives_only_new_html_and_keeps_audit_source(self):
        self.downstream(local=True)
        original = self.artifacts("base")[0]
        revised = self.artifacts("repair")[0]
        checked = self.rt.browser_checks.artifacts(self.task, self.rows()["repair"])
        self.assertEqual({a["seq"] for a in checked}, {revised["seq"], self.artifacts("base")[1]["seq"]})
        with patch("cater.runtime.run_check", side_effect=self.execute):
            receipt, _ = self.rt.verify_work(self.task, "repair", "behavior", [sys.executable, "fixture"],
                                             browser_plan=normalize_plan(PLAN, [a["filename"] for a in checked]))
        self.assertEqual(receipt.payload["outcome"], "passed")
        self.assertNotIn(original["seq"], [a["seq"] for a in receipt.payload["checked_artifacts"]])
        self.rt.collaborate(self.task)
        manifest = self.rt.delivery._manifest(self.task)
        self.assertEqual({f["path"] for f in manifest["files"]}, {"index.html", "style.css"})
        self.assertNotIn(original["seq"], [f["artifact_seq"] for f in manifest["files"]])
        self.assertIn(original["seq"], manifest["items"][0]["submission"]["artifact_seqs"])
        with patch("cater.delivery.run_check", side_effect=self.execute):
            self.rt.delivery.verify(self.task, "integration_behavior", [sys.executable, "fixture"])
        self.assertEqual(self.rt.delivery.run(self.task)["status"], "accepted")
        with zipfile.ZipFile(io.BytesIO(self.rt.delivery.archive(self.task))) as archive:
            self.assertEqual(archive.read("index.html").decode(), HTML.replace("Old", "New"))
            self.assertEqual(archive.read("style.css").decode(), CSS)
            self.assertEqual(set(archive.namelist()), {"index.html", "style.css", "manifest.json", "handoff.md"})
        self.assertEqual(self.artifacts("base")[0]["content"], HTML)
        self.assertTrue(self.rt.verify_chain().ok)

    def test_model_forged_provenance_cannot_hide_accepted_source(self):
        self.downstream(forged=True)
        self.assertNotIn("revision_source", self.artifacts("repair")[0])
        artifacts = self.rt.browser_checks.artifacts(self.task, self.rows()["repair"])
        self.assertEqual(sum(a["filename"] == "index.html" for a in artifacts), 2)
        with self.assertRaises(ValueError):
            normalize_plan(PLAN, [a["filename"] for a in artifacts])
        manifest = self.rt.delivery._manifest(self.task)
        files = self.rt.delivery._files(self.task, manifest)
        self.assertEqual(files["work-1/index.html"], HTML)
        self.assertEqual(files["work-2/index.html"], HTML.replace("Old", "New"))

    def test_independent_sibling_revisions_remain_conflicting(self):
        def author(work):
            if work["work_item_id"] == "base":
                return {"deliverables": [file("index.html", HTML)]}
            if work["work_item_id"] == "join":
                return {"deliverables": [file("notes.txt", "Inspect both branches")]}
            return {"artifact_edits": [edit(work["accepted_inputs"]["base"][0], new=work["work_item_id"])]}
        self.driver.author = author
        self.rt.plan_work(self.task, [item("base"), item("left", deps=["base"]), item("right", deps=["base"]),
                                     item("join", deps=["left", "right"])])
        self.rt.collaborate(self.task)
        artifacts = self.rt.browser_checks.artifacts(self.task, self.rows()["join"])
        self.assertEqual({a["content"] for a in artifacts if a["filename"] == "index.html"},
                         {HTML.replace("Old", "left"), HTML.replace("Old", "right")})
        with self.assertRaises(ValueError):
            normalize_plan(PLAN, [a["filename"] for a in artifacts])
        files = self.rt.delivery._files(self.task, self.rt.delivery._manifest(self.task))
        self.assertEqual(sum(path.endswith("index.html") for path in files), 2)

    def test_sequential_revisions_remove_transitive_ancestors(self):
        self.driver.author = lambda work: ({"deliverables": [file("index.html", HTML)]}
            if work["work_item_id"] == "base" else {"artifact_edits": [
                edit(work["accepted_inputs"]["base" if work["work_item_id"] == "first" else "first"][0],
                     "Old" if work["work_item_id"] == "first" else "New",
                     "New" if work["work_item_id"] == "first" else "Final")]})
        self.rt.plan_work(self.task, [item("base"), item("first", deps=["base"]), item("second", deps=["first"])])
        self.rt.collaborate(self.task)
        selected = self.rt.browser_checks.artifacts(self.task, self.rows()["second"])
        self.assertEqual([a["content"] for a in selected], [HTML.replace("Old", "Final")])
        self.assertEqual(self.rt.delivery._files(self.task, self.rt.delivery._manifest(self.task)),
                         {"index.html": HTML.replace("Old", "Final")})

    def test_downstream_retry_retains_previous_html_and_patches_current_input_css(self):
        self.downstream()
        self.rt.work.rework(self.task, "repair", "Correct the stylesheet too")
        self.driver.author = lambda work: {"artifact_edits": [edit(
            next(a for a in work["accepted_inputs"]["base"] if a["filename"] == "style.css"), "red", "green")]}
        self.rt.collaborate(self.task)
        selected = self.rt.browser_checks.artifacts(self.task, self.rows()["repair"])
        expected = {"index.html": HTML.replace("Old", "New"), "style.css": CSS.replace("red", "green")}
        self.assertEqual({a["filename"]: a["content"] for a in selected}, expected)
        self.assertEqual(len(selected), 2)
        self.assertEqual(self.rt.delivery._files(self.task, self.rt.delivery._manifest(self.task)), expected)
        self.assertEqual(len(self.events()), 5)

    def test_host_reads_real_source_body_instead_of_context_metadata(self):
        self.downstream()
        self.rt.work.rework(self.task, "repair", "Patch the actual input")
        claim = self.rt.work.claim(self.task, "repair")
        source = self.artifacts("base")[0]
        invented = {**source, "content": "Invented source body"}
        invented["content_sha256"] = hashlib.sha256(invented["content"].encode()).hexdigest()
        context = {**claim.payload, "accepted_inputs": {"base": [invented]}}
        with self.assertRaises(ValueError):
            prepare_snapshot(self.rt, self.task, "design", [edit(invented, "Invented", "New")], [], context)
        snapshot, proof = prepare_snapshot(self.rt, self.task, "design", [edit(source)], [], context)
        self.assertEqual(snapshot[0]["content"], HTML.replace("Old", "New"))
        self.assertEqual(proof["index.html"]["content_sha256"], source["content_sha256"])

    def test_host_rejects_context_pointing_to_an_unrelated_or_foreign_artifact(self):
        self.downstream()
        unrelated = self.artifacts("repair")[0]
        self.rt.work.rework(self.task, "repair", "Patch the actual input")
        claim = self.rt.work.claim(self.task, "repair")
        source = self.artifacts("base")[0]
        context = {**claim.payload, "accepted_inputs": {"base": [{**source, "seq": unrelated["seq"]}]}}
        with self.assertRaises(ValueError):
            prepare_snapshot(self.rt, self.task, "design", [edit(context["accepted_inputs"]["base"][0])], [], context)
        foreign = self.rt.create_task("Separate task")
        self.rt.plan_work(foreign, [item("foreign")])
        self.driver.author = lambda work: {"deliverables": [file("index.html", HTML)]}
        self.rt.collaborate(foreign)
        row = self.rt.work.view(foreign)["items"][0]
        artifact = self.rt.work.artifacts(foreign, row["submission"])[0]
        context["accepted_inputs"] = {"base": [artifact]}
        with self.assertRaises(ValueError):
            prepare_snapshot(self.rt, self.task, "design", [edit(artifact)], [], context)
