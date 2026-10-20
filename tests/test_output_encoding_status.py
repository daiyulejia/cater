"""Offline full-stream UTF-8 diagnostics, independent of lossy preview windows."""

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

from cater.verification import PREVIEW_LIMIT, _Capture, output_metadata, run_check
from cater.workspace_tools import WorkspaceExecutor, WorkspacePolicy


class OutputEncodingStatusTests(unittest.TestCase):
    def capture(self, chunks, *, broken=False):
        capture = _Capture()
        snapshots = []

        class Stream:
            closed = False

            def __init__(self):
                self.chunks = iter(chunks)

            def read(self, size):
                snapshots.append(capture.summary(record_output=False))
                self_state = capture._utf8_decoder.getstate()[0]
                self_test.assertLessEqual(len(self_state), 3)
                self_test.assertLessEqual(len(capture.preview), PREVIEW_LIMIT)
                self_test.assertLessEqual(len(capture.tail), PREVIEW_LIMIT // 2)
                try:
                    return next(self.chunks)
                except StopIteration:
                    if broken:
                        raise OSError("fixture read failure")
                    return b""

            def close(self):
                self.closed = True

        self_test = self
        stream = Stream()
        if broken:
            with self.assertRaises(OSError):
                capture.drain(stream)
        else:
            capture.drain(stream)
        self.assertTrue(stream.closed)
        body = b"".join(chunks)
        summary = capture.summary(record_output=True)
        self.assertEqual(summary["bytes"], len(body))
        self.assertEqual(summary["sha256"], hashlib.sha256(body).hexdigest())
        self.assertEqual(summary["complete"], not broken)
        self.assertEqual(capture.summary(record_output=True), summary)
        return summary, snapshots

    def test_valid_empty_ascii_literal_replacement_and_split_multibyte(self):
        for body in (b"", b"ascii", "\ufffd\u4e2d\U0001f600".encode()):
            for size in (1, 2, 3, 4096):
                with self.subTest(body=body, size=size):
                    result, snapshots = self.capture([body[i:i + size] for i in range(0, len(body), size)])
                    self.assertIs(result["utf8_valid"], True)
                    self.assertTrue(all(s["utf8_valid"] is None for s in snapshots))

    def test_invalid_sequences_and_incomplete_final_codepoint(self):
        for body in (b"\xff", b"\xc0\xaf", b"\xed\xa0\x80", b"\xf4\x90\x80\x80",
                     b"\x80", b"\xe4A", b"\xe4\xb8", "\u4e2d\u6587".encode("cp936")):
            with self.subTest(body=body):
                result, _ = self.capture([bytes([byte]) for byte in body])
                self.assertIs(result["utf8_valid"], False)

    def test_incomplete_capture_is_unknown_unless_already_proven_invalid(self):
        for chunks, expected in (([], None), ([b"ascii"], None), ([b"\xe4\xb8"], None),
                                 ([b"\xff", b"ascii"], False)):
            with self.subTest(chunks=chunks):
                result, _ = self.capture(chunks, broken=True)
                self.assertIs(result["utf8_valid"], expected)

    def test_invalid_middle_survives_discarding_both_preview_windows(self):
        result, snapshots = self.capture([b"H" * 4096] * 4 + [b"\xff"] + [b"T" * 4096] * 100)
        self.assertIs(result["utf8_valid"], False)
        self.assertTrue(result["truncated"])
        self.assertNotIn("\ufffd", result["excerpt"] + result["tail_excerpt"])
        self.assertIs(snapshots[-1]["utf8_valid"], False)

    def test_preview_boundary_replacement_does_not_imply_invalid_stream(self):
        body = b"x" * (PREVIEW_LIMIT - 1) + "\u4e2d".encode() + b"y" * PREVIEW_LIMIT
        result, _ = self.capture([body[i:i + 4096] for i in range(0, len(body), 4096)])
        self.assertTrue(result["truncated"])
        self.assertIs(result["utf8_valid"], True)

    def test_metadata_retains_validity_without_output_or_encoding_guess(self):
        for valid in (True, False, None):
            value = dict(bytes=4, sha256="a" * 64, complete=False, truncated=False, utf8_valid=valid)
            self.assertEqual(output_metadata({**value, "excerpt": "PRIVATE", "tail_excerpt": "PRIVATE",
                                              "encoding_guess": "cp936"}), value)
        self.assertEqual(output_metadata({"complete": False}), {"complete": False})

    def test_real_subprocess_and_workspace_streams_remain_independent(self):
        invalid = "\u4e2d\u6587".encode("cp936")
        valid = "\ufffd\u4e2d\U0001f600".encode()
        for workspace in (False, True):
            for stdout, stderr in ((invalid, valid), (valid, invalid)):
                with self.subTest(workspace=workspace, stdout=stdout), tempfile.TemporaryDirectory() as tmp:
                    script = (f"import sys; sys.stdout.buffer.write({stdout!r}); "
                              f"sys.stderr.buffer.write({stderr!r})")
                    root = Path(tmp)
                    if workspace:
                        policy = WorkspacePolicy(root=root, mode="execute", commands={"python": (sys.executable, "-c")})
                        result = WorkspaceExecutor(policy).execute("workspace_run", {"command": "python", "args": [script]})
                        self.assertEqual(result["status"], "ok")
                    else:
                        result = run_check([sys.executable, "-c", script], cwd=root, record_output=True).payload
                    self.assertEqual(result["outcome"], "passed")
                    self.assertEqual(result["exit_code"], 0)
                    for name, body in (("stdout", stdout), ("stderr", stderr)):
                        output = result[name]
                        self.assertIs(output["utf8_valid"], body == valid)
                        self.assertTrue(output["complete"])
                        self.assertEqual(output["bytes"], len(body))
                        self.assertEqual(output["sha256"], hashlib.sha256(body).hexdigest())
                        self.assertEqual(output["excerpt"], body.decode("utf-8", errors="replace"))


if __name__ == "__main__":
    unittest.main()
