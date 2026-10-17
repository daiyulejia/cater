"""Opt-in actual Chromium: two ZIP reworks, frozen dependencies and delivery."""

import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import struct
import unittest
import zipfile
import zlib

import test_team_review_cycle as reviews


def red_png():
    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">2I5B", 1, 1, 8, 6, 0, 0, 0)) + \
        chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00\xff")) + chunk(b"IEND", b"")


@unittest.skipUnless(os.environ.get("CATER_ARCHIVE_BROWSER_TESTS") == "1", "opt-in actual local Chromium")
class ArchiveBrowserCycleTests(unittest.TestCase):
    def test_two_actual_browser_failures_then_approved_archive_matches_delivery(self):
        f = reviews.TeamReviewCycleTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        root = f.bind_workspace()
        repo = Path(__file__).resolve().parents[1]
        node = os.environ.get("CATER_TEST_NODE") or shutil.which("node")
        self.assertIsNotNone(node, "Node is required for this explicit browser test")
        f.rt.browser_checks.command = [str(Path(node).resolve()), str(repo / "tools/browser-check/runner.mjs")]
        f.rt.browser_checks.timeout = 30
        sources = {"site/index.html": b'<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="../assets/style.css">'
            b'<button id="add">Add</button><output id="count">0</output><img id="pixel" src="../assets/pixel.png">'
            b'<script src="../assets/app.js"></script>',
            "assets/style.css": b"#count { color: rgb(10,20,30) }", "assets/pixel.png": red_png(),
            "notes/unused.txt": b"not a browser dependency"}
        for name, body in sources.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
        script = root / "assets/app.js"
        dependencies = ["site/index.html", "assets/app.js", "assets/style.css", "assets/pixel.png"]
        plan = {"entrypoint": "site/index.html", "steps": [
            {"action": "assert_js", "label": "Actual image bytes and CSS loaded", "expression":
             "(()=>{let i=document.querySelector('#pixel'),c=document.createElement('canvas');c.width=c.height=1;"
             "let x=c.getContext('2d');x.drawImage(i,0,0);let p=x.getImageData(0,0,1,1).data;return "
             "i.complete&&i.naturalWidth===1&&p[0]===255&&p[1]===0&&p[2]===0&&p[3]===255&&"
             "getComputedStyle(document.querySelector('#count')).color==='rgb(10, 20, 30)'})()"},
            {"action": "click", "label": "Click actual button", "selector": "#add"},
            {"action": "assert_text", "label": "Actual increment must be three", "selector": "#count", "text": "3"}]}
        review_seq, versions, observations = None, [], []
        for number in (1, 2, 3):
            expected_js = f"document.querySelector('#add').onclick=()=>document.querySelector('#count').textContent='{number}';".encode()
            script.write_bytes(expected_js)
            published, = f.exchange(f.worker, ("publish_workspace_archive", {"filename": "site.zip",
                "summary": "Actual project iteration", "paths": [*sources, "assets/app.js"]}))
            self.assertEqual(published["status"], "ok", published)
            seq = published["artifact_seq"]
            versions.append(seq)
            requested = f.request([seq], review_seq=review_seq)
            self.assertEqual(requested["status"], "ok", requested)
            review_seq = requested["review_seq"]
            # A browser check must not read this unpublished worktree mutation.
            script.write_bytes(b"throw new Error('UNPUBLISHED_WORKSPACE_SENTINEL')")
            checked, = f.exchange(f.reviewer, ("browser_check", {"artifact_seqs": [seq], "plan": plan,
                "archive_files": [{"artifact_seq": seq, "paths": dependencies}]}))
            self.assertEqual(checked["status"], "ok", checked)
            self.assertEqual(checked["outcome"], "passed" if number == 3 else "failed", checked)
            self.assertTrue(checked["scope_current"])
            self.assertTrue(checked["artifacts_unchanged"])
            self.assertTrue(checked["complete"])
            self.assertTrue(checked["invocation_stopped"])
            self.assertEqual(checked["browser_report"]["counts"]["page_errors"], 0)
            self.assertEqual(checked["browser_report"]["steps"][0]["actual"], True)
            self.assertNotIn("UNPUBLISHED_WORKSPACE_SENTINEL", json.dumps(checked))
            self.assertEqual({r["path"] for r in checked["checked_files"]}, set(dependencies))
            by_path = {r["path"]: r for r in checked["checked_files"]}
            self.assertEqual(by_path["assets/app.js"]["sha256"], hashlib.sha256(expected_js).hexdigest())
            self.assertEqual(by_path["assets/pixel.png"]["sha256"], hashlib.sha256(red_png()).hexdigest())
            receipt = next(e for e in reversed(f.events("team.tool_finished")) if e.payload["name"] == "browser_check")
            observations.append(checked["outcome"])
            self.assertEqual(f.record(review_seq, "approved" if number == 3 else "changes_requested",
                feedback=f"Actual browser observation: {checked['outcome']}, increment fixture {number}")["status"], "ok")
            if number < 3:
                self.assertEqual(f.finish()["code"], "review_pending")
        f.acknowledge_latest_review()
        delivered, = f.exchange(f.manager, ("finish_work", {"summary": "Exact reviewed static project",
            "verification": "Three actual Chromium checks, first two failed then fixed",
            "handoff": {"why": "Correct the observed button result", "changed": ["Increment becomes three"],
                "impact": ["Button output only"], "alternatives": ["No assertion weakened to accept earlier results"],
                "reproduce": ["Open site/index.html with its assets and click Add"],
                "limits": ["Scripted collaborator protocol, not natural model quality; no backend or external network"],
                "evidence": [{"source_seq": receipt.seq, "quote": '"actual": true'}]}}))
        self.assertEqual(delivered["status"], "ok", delivered)
        completed, = f.events("team.objective_completed")
        self.assertEqual([a["seq"] for a in completed.payload["artifacts"]], [versions[-1]])
        self.assertEqual(completed.payload["handoff"]["evidence"][0]["status"], "passed")
        artifact = f.rt.task(f.task).deliverables[-1]
        with zipfile.ZipFile(io.BytesIO(f.rt.team.archive(f.task))) as delivery:
            raw = delivery.read("site.zip")
            self.assertEqual(hashlib.sha256(raw).hexdigest(), artifact["content_sha256"])
            with zipfile.ZipFile(io.BytesIO(raw)) as package:
                self.assertEqual(package.read("assets/app.js"), expected_js)
                self.assertEqual(package.read("assets/pixel.png"), red_png())
                self.assertIn("notes/unused.txt", package.namelist())
        self.assertEqual(observations, ["failed", "failed", "passed"])
        self.assertEqual(f.protocol_errors, [])
        self.assertTrue(f.rt.verify_chain().ok)


if __name__ == "__main__":
    unittest.main()
