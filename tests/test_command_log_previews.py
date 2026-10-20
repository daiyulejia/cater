"""Bounded command evidence keeps terminal diagnostics without replaying commands."""

import hashlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

from cater.events import Actor, EventLog
from cater.review_contract import execution_observations, receipt_context
from cater.verification import CheckResult, PREVIEW_LIMIT, _Capture, output_metadata, run_check
from cater.workspace_tools import WorkspaceExecutor, WorkspacePolicy
import test_team_handoff
import test_team_browser
from unittest.mock import patch


class CommandLogPreviewTests(unittest.TestCase):
    def capture(self, body):
        capture = _Capture()
        capture.drain(io.BytesIO(body))
        return capture

    def assert_bounded(self, output, body):
        self.assertEqual(output["bytes"], len(body))
        self.assertEqual(output["sha256"], hashlib.sha256(body).hexdigest())
        self.assertTrue(output["truncated"])
        self.assertEqual(output["preview_mode"], "head_tail")
        self.assertLessEqual(len(output["excerpt"].encode("utf-8")), PREVIEW_LIMIT // 2)
        self.assertLessEqual(len(output["tail_excerpt"].encode("utf-8")), PREVIEW_LIMIT // 2)

    def test_short_empty_and_boundary_outputs_keep_old_receipt_shape(self):
        for body in (b"", b"one\ntwo\n", "\u4e2d\u6587\n".encode(), b"\xff" * 100, b"x" * PREVIEW_LIMIT):
            with self.subTest(size=len(body)):
                capture = self.capture(body)
                self.assertEqual(capture.summary(record_output=True), {
                    "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest(),
                    "utf8_valid": b"\xff" not in body,
                    "complete": True, "truncated": False, "excerpt": body.decode("utf-8", errors="replace")})
                self.assertEqual(capture.preview_text(), body.decode("utf-8", errors="replace"))

    def test_first_truncated_byte_is_not_fabricated_between_distinct_segments(self):
        body = b"H" * (PREVIEW_LIMIT // 2) + b"|" + b"T" * (PREVIEW_LIMIT // 2)
        output = self.capture(body).summary(record_output=True)
        self.assert_bounded(output, body)
        self.assertEqual(output["excerpt"], "H" * (PREVIEW_LIMIT // 2))
        self.assertEqual(output["tail_excerpt"], "T" * (PREVIEW_LIMIT // 2))
        self.assertNotIn("|", output["excerpt"] + output["tail_excerpt"])

    def test_long_output_retains_real_head_and_end_without_inventing_middle(self):
        body = b"HEAD\n" + b"x" * 30000 + b"\nFINAL BUILD ERROR\n"
        capture = self.capture(body)
        output = capture.summary(record_output=True)
        self.assert_bounded(output, body)
        self.assertEqual(output["excerpt"], body[:PREVIEW_LIMIT // 2].decode())
        self.assertEqual(output["tail_excerpt"], body[-PREVIEW_LIMIT // 2:].decode())
        self.assertNotIn("FINAL BUILD ERROR", capture.preview_text())
        self.assertEqual(capture.preview_text(), body[:PREVIEW_LIMIT].decode())
        self.assertEqual(capture.summary(record_output=True), output)
        self.assertTrue(output["complete"])

    def test_record_output_false_never_persists_either_private_segment(self):
        body = b"PRIVATE_HEAD" + b"x" * 30000 + b"PRIVATE_END"
        output = self.capture(body).summary(record_output=False)
        self.assertEqual(set(output), {"bytes", "sha256", "complete", "truncated", "utf8_valid"})
        self.assertNotIn("PRIVATE_", str(output))

    def test_every_chunk_size_keeps_the_same_rolling_tail_and_digest(self):
        body = b"H" + b"z" * PREVIEW_LIMIT + b"LAST"
        for size in (1, 3, 4096, 8193, len(body)):
            class Chunks:
                def __init__(self):
                    self.position = 0

                def read(self, _):
                    part = body[self.position:self.position + size]
                    self.position += len(part)
                    return part

                def close(self):
                    pass

            with self.subTest(size=size):
                output_capture = _Capture()
                output_capture.drain(Chunks())
                output = output_capture.summary(record_output=True)
                self.assert_bounded(output, body)
                self.assertEqual(output["tail_excerpt"], body[-PREVIEW_LIMIT // 2:].decode())

    def test_multibyte_and_invalid_utf8_both_preserve_the_end_within_budget(self):
        for body in (("\u732b" * 5000 + "\nFINAL ERROR\n").encode(),
                     b"\xff" * 6000 + b"\nFINAL ERROR\n", b"\xff" * 30000 + b"\nFINAL ERROR\n"):
            with self.subTest(size=len(body)):
                output = self.capture(body).summary(record_output=True)
                self.assert_bounded(output, body)
                self.assertTrue(output["tail_excerpt"].endswith("\nFINAL ERROR\n"))
                self.assertTrue(output["complete"])

    def test_failed_capture_tail_is_only_last_collected_output_not_completed_log(self):
        body = b"H" + b"x" * 10000 + b"LAST COLLECTED"

        class Broken(io.BytesIO):
            def read(self, size=-1):
                if self.tell() == len(body):
                    raise OSError("fixture stream failure")
                return super().read(size)

        capture = _Capture()
        stream = Broken(body)
        with self.assertRaises(OSError):
            capture.drain(stream)
        self.assertTrue(stream.closed)
        output = capture.summary(record_output=True)
        self.assert_bounded(output, body)
        self.assertFalse(output["complete"])
        self.assertTrue(output["tail_excerpt"].endswith("LAST COLLECTED"))

    def test_real_failed_commands_keep_both_terminal_diagnostics_and_run_once(self):
        stdout = b"OUT_HEAD\n" + b"x" * 10000 + b"\nFINAL BUILD ERROR\n"
        stderr = b"ERR_HEAD\n" + b"y" * 10000 + b"\nFINAL DETAILS\n"
        script = ("from pathlib import Path; import sys; "
                  "p=Path('runs'); p.write_text(str(int(p.read_text())+1) if p.exists() else '1'); "
                  "sys.stdout.buffer.write(b'OUT_HEAD\\n'+b'x'*10000+b'\\nFINAL BUILD ERROR\\n'); "
                  "sys.stderr.buffer.write(b'ERR_HEAD\\n'+b'y'*10000+b'\\nFINAL DETAILS\\n'); sys.exit(3)")
        for workspace in (False, True):
            with self.subTest(workspace=workspace), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                if workspace:
                    policy = WorkspacePolicy(root=root, mode="execute", commands={"python": (sys.executable, "-c")})
                    result = WorkspaceExecutor(policy).execute("workspace_run", {"command": "python", "args": [script]})
                    self.assertEqual(result["status"], "error")
                else:
                    result = run_check([sys.executable, "-c", script], cwd=root, record_output=True).payload
                self.assertEqual(result["outcome"], "failed")
                self.assertEqual(result["exit_code"], 3)
                self.assertEqual((root / "runs").read_text(), "1")
                for field, body, marker in (("stdout", stdout, "FINAL BUILD ERROR"), ("stderr", stderr, "FINAL DETAILS")):
                    self.assert_bounded(result[field], body)
                    self.assertIn(marker, result[field]["tail_excerpt"])
                    self.assertTrue(result[field]["complete"])

    def test_metadata_whitelist_cannot_leak_current_or_future_output_fields(self):
        value = {"bytes": 123, "sha256": "a" * 64, "truncated": True, "complete": True,
                 "excerpt": "PRIVATE_HEAD", "tail_excerpt": "PRIVATE_TAIL", "preview_mode": "head_tail",
                 "preview": "PRIVATE_PREVIEW", "future_text": "PRIVATE_FUTURE"}
        self.assertEqual(output_metadata(value), {key: value[key] for key in ("bytes", "sha256", "truncated", "complete")})
        self.assertNotIn("PRIVATE_", json.dumps(output_metadata(value)))
        self.assertEqual(value["tail_excerpt"], "PRIVATE_TAIL")

    def test_legacy_command_and_workspace_observations_keep_segments_separate(self):
        result = {"outcome": "failed", "exit_code": 3, "command": ["fixture"],
            "stdout": {"excerpt": "HEAD", "tail_excerpt": "TAIL", "preview_mode": "head_tail"},
            "stderr": {"excerpt": "ERR", "tail_excerpt": "UNMARKED"}}
        for receipt in (result, {"check_kind": "workspace", "suite": [{"id": "test", "result": result}]}):
            observed = execution_observations(receipt)
            self.assertEqual(observed[-3:], ["HEAD", "TAIL", "ERR"])
            self.assertNotIn("UNMARKED", observed)
            self.assertFalse(any("HEADTAIL" in part or "TAILERR" in part for part in observed))

    def test_parsed_browser_context_redacts_body_fields_without_rewriting_events(self):
        log = EventLog(":memory:")
        self.addCleanup(log.close)
        stdout = {"complete": True, "truncated": False, "bytes": 3, "sha256": "a" * 64,
                  "excerpt": "PRIVATE_HEAD", "tail_excerpt": "PRIVATE_TAIL", "preview": "PRIVATE_PREVIEW"}
        payload = {"check_kind": "browser", "browser_report": {"outcome": "passed"}, "stdout": stdout}
        event = log.emit(type="verification.completed", actor=Actor.system("local_verifier"), payload=payload)
        view = receipt_context(event)
        self.assertEqual(view["stdout"], output_metadata(stdout))
        self.assertEqual(log.read()[0].payload, payload)


class TailHandoffEvidenceTests(unittest.TestCase):
    setUp = test_team_handoff.TeamHandoffTests.setUp
    artifact = test_team_handoff.TeamHandoffTests.artifact
    refs = test_team_handoff.TeamHandoffTests.refs
    receipt = test_team_handoff.TeamHandoffTests.receipt
    handoff = test_team_handoff.TeamHandoffTests.handoff
    normalize = test_team_handoff.TeamHandoffTests.normalize

    def test_tail_is_independent_evidence_and_does_not_convert_failure_into_pass(self):
        receipt = self.receipt(result={"status": "error", "exit_code": 3, "complete": True,
            "stdout": {"excerpt": "HEAD END", "tail_excerpt": "TAIL FINAL ERROR", "preview_mode": "head_tail",
                       "complete": True, "truncated": True}})
        value = self.normalize(self.handoff([{"source_seq": receipt.seq, "quote": "FINAL ERROR"}]))
        ref, = value["evidence"]
        self.assertEqual(ref["status"], "error")
        self.assertEqual(ref["exit_code"], 3)
        self.assertTrue(ref["complete"])
        self.assertTrue(ref["truncated"])
        for invented in ("HEAD ENDTAIL", "HEAD END\nTAIL", "unrecorded middle text"):
            with self.subTest(invented=invented), self.assertRaises(ValueError):
                self.normalize(self.handoff([{"source_seq": receipt.seq, "quote": invented}]))

    def test_tail_from_wrong_tool_run_or_foreign_group_is_not_accepted(self):
        other = self.rt.create_task("Foreign fixture")
        for kwargs in ({"changes": {"run_id": "wrong-run"}}, {"task": other}):
            receipt = self.receipt(result={"status": "ok", "exit_code": 0,
                "stdout": {"excerpt": "head", "tail_excerpt": "PRIVATE FINAL", "preview_mode": "head_tail"}}, **kwargs)
            with self.assertRaises(ValueError):
                self.normalize(self.handoff([{"source_seq": receipt.seq, "quote": "PRIVATE FINAL"}]))

    def test_unmarked_tail_and_quotes_spanning_two_streams_are_not_sources(self):
        receipt = self.receipt(result={"status": "ok", "exit_code": 0,
            "stdout": {"excerpt": "HEAD", "tail_excerpt": "UNMARKED"},
            "stderr": {"excerpt": "OTHER STREAM"}})
        for quote in ("UNMARKED", "HEADOTHER STREAM", "HEAD\nOTHER STREAM"):
            with self.subTest(quote=quote), self.assertRaises(ValueError):
                self.normalize(self.handoff([{"source_seq": receipt.seq, "quote": quote}]))


class BrowserOutputRedactionTests(unittest.TestCase):
    setUp = test_team_browser.TeamBrowserTests.setUp
    artifact = test_team_browser.TeamBrowserTests.artifact
    prepare = test_team_browser.TeamBrowserTests.prepare
    execute = test_team_browser.TeamBrowserTests.execute

    def test_unavailable_large_browser_json_never_exposes_raw_tail(self):
        args = self.prepare()
        body = json.dumps({"padding": "x" * 20000, "private": "RAW_JSON_TAIL_MARKER"}).encode()
        capture = _Capture()
        capture.drain(io.BytesIO(body))
        stdout = capture.summary(record_output=True)
        self.assertIn("RAW_JSON_TAIL_MARKER", stdout["tail_excerpt"])
        result = CheckResult({"outcome": "passed", "exit_code": 0, "duration_ms": 1,
            "process_scope": "fixture", "stdout": stdout, "stderr": {"complete": True, "truncated": False}},
            capture.preview_text(), "")
        with patch("cater.team_browser.run_check", return_value=result):
            receipt = self.execute(args)
        self.assertEqual(receipt["outcome"], "unavailable")
        self.assertEqual(receipt["stdout"], output_metadata(stdout))
        self.assertNotIn("RAW_JSON_TAIL_MARKER", json.dumps(receipt))


if __name__ == "__main__":
    unittest.main()
