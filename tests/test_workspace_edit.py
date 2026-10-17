"""Independent regressions for versioned, literal workspace edits."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import unittest
from unittest.mock import patch

from cater import workspace_tools as tools
from cater.workspace_tools import WorkspaceExecutor, WorkspacePolicy
from cater.workspaces import WorkspaceService


def digest(body):
    return hashlib.sha256(body).hexdigest()


class WorkspaceEditTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="cater-edit-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "workspace"
        self.root.mkdir()
        self.executor = self.make_executor()

    def make_executor(self, mode="edit"):
        return WorkspaceExecutor(WorkspacePolicy.from_dict({
            "root": str(self.root), "mode": mode,
        }))

    def seed(self, body=b"prefix OLD suffix", path="sample.txt"):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        return digest(body)

    def arguments(self, **changes):
        return {"path": "sample.txt", "content": "NEW", "old_text": "OLD",
                "expected_sha256": digest(b"prefix OLD suffix"), **changes}

    def edit(self, **changes):
        return self.executor.execute("workspace_write", self.arguments(**changes))

    def assert_error(self, result, code=None):
        self.assertEqual(result["status"], "error", result)
        self.assertIsInstance(result["error"]["message"], str)
        if code is not None:
            self.assertEqual(result["error"]["code"], code, result)
        json.dumps(result).encode("utf-8")

    def assert_success(self, result, before, after, path="sample.txt"):
        self.assertEqual(result, {
            "status": "ok", "path": path, "sha256": digest(after),
            "bytes": len(after), "created": False, "truncated": False,
            "replacements": 1, "before_sha256": digest(before),
        })
        self.assertEqual((self.root / path).read_bytes(), after)
        self.assertFalse(list(self.root.rglob(".workspace-tmp-*")))

    def assert_no_publication(self, arguments, code=None):
        before = {p.relative_to(self.root).as_posix():
                  None if p.is_dir() else p.read_bytes() for p in self.root.rglob("*")}
        with patch.object(tools.tempfile, "mkstemp") as staging, \
                patch.object(self.executor, "_path", wraps=self.executor._path) as resolve:
            result = self.executor.execute("workspace_write", arguments)
        self.assert_error(result, code)
        staging.assert_not_called()
        for call in resolve.call_args_list:
            self.assertFalse(call.kwargs.get("create_parents", False))
        after = {p.relative_to(self.root).as_posix():
                 None if p.is_dir() else p.read_bytes() for p in self.root.rglob("*")}
        self.assertEqual(after, before)
        return result

    def assert_preflight(self, arguments):
        with patch.object(self.executor, "_write") as dispatch:
            result = self.executor.execute("workspace_write", arguments)
        self.assertFalse(dispatch.called, "Invalid arguments reached _write")
        self.assert_error(result, "invalid_arguments")
        self.assertIs(result["complete"], True)
        self.assertEqual(result["outcome"], "not_started")
        self.assertEqual(list(self.root.iterdir()), [])
        return result

    def test_schema_keeps_required_fields_and_optional_old_text(self):
        schema = next(s["input_schema"] for s in self.executor.schemas()
                      if s["name"] == "workspace_write")
        self.assertEqual(set(schema["required"]), {"path", "content", "expected_sha256"})
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["properties"]["old_text"]["type"], "string")
        self.assertEqual(schema["properties"]["old_text"]["minLength"], 1)

    def test_unique_edit_receipt_hash_covers_entire_verified_file(self):
        before = b"prefix OLD suffix"
        self.seed(before)
        self.assert_success(self.edit(), before, b"prefix NEW suffix")
        self.assertNotEqual(digest(before), digest(b"OLD"))

    def test_unicode_replacement_preserves_surrounding_bytes(self):
        before = "\u524d\U0001f600\u65e7\u6587\u540e\u7f00".encode()
        sha = self.seed(before)
        self.assert_success(self.edit(old_text="\u65e7\u6587", content="\u65b0\U0001f680",
                                      expected_sha256=sha), before,
                            "\u524d\U0001f600\u65b0\U0001f680\u540e\u7f00".encode())

    def test_bom_and_crlf_are_preserved_and_bom_can_be_explicitly_removed(self):
        before = b"\xef\xbb\xbfprefix\r\nOLD\r\nsuffix\r\n"
        sha = self.seed(before)
        after = before.replace(b"OLD", b"NEW")
        self.assert_success(self.edit(expected_sha256=sha), before, after)
        self.assert_success(self.edit(old_text="\ufeff", content="", expected_sha256=digest(after)),
                            after, after[3:])

    def test_multiline_crlf_match_is_literal(self):
        before = b"head\r\nOLD\r\nline\r\ntail\n"
        sha = self.seed(before)
        self.assert_success(self.edit(old_text="OLD\r\nline", content="new\r\nlines",
                                      expected_sha256=sha), before,
                            b"head\r\nnew\r\nlines\r\ntail\n")

    def test_empty_replacement_deletes_fragment_or_whole_file_content(self):
        for before, old, after in ((b"prefix OLD suffix", "OLD", b"prefix  suffix"),
                                   (b"OLD", "OLD", b"")):
            with self.subTest(before=before):
                sha = self.seed(before)
                self.assert_success(self.edit(content="", old_text=old, expected_sha256=sha), before, after)

    def test_regex_metacharacters_are_literal_and_replacement_is_not_recursive(self):
        before = b"head [a.*] tail"
        sha = self.seed(before)
        self.assert_success(self.edit(old_text="[a.*]", content="[a.*][a.*]", expected_sha256=sha),
                            before, b"head [a.*][a.*] tail")

    def test_absent_match_does_not_stage_or_create_directories(self):
        self.seed()
        self.assert_no_publication(self.arguments(old_text="absent"), "conflict")

    def test_multiple_disjoint_matches_do_not_stage(self):
        sha = self.seed(b"OLD middle OLD")
        self.assert_no_publication(self.arguments(expected_sha256=sha), "conflict")

    def test_overlapping_matches_are_ambiguous(self):
        for text, old in (("aaa", "aa"), ("ababa", "aba"), ("aaaa", "aaa")):
            with self.subTest(text=text):
                sha = self.seed(text.encode())
                self.assert_no_publication(self.arguments(old_text=old, expected_sha256=sha), "conflict")

    def test_no_unicode_normalization_or_newline_folding(self):
        for before, old in (("caf\u00e9", "cafe\u0301"), ("a\r\nb", "a\nb"),
                             ("a  b", "a b"), ("Old", "OLD")):
            with self.subTest(before=before):
                sha = self.seed(before.encode())
                self.assert_no_publication(self.arguments(old_text=old, expected_sha256=sha), "conflict")

    def test_missing_file_and_missing_parents_cannot_be_created(self):
        for path in ("missing.txt", "missing/parent/file.txt"):
            with self.subTest(path=path):
                self.assert_no_publication(self.arguments(path=path))

    def test_empty_wrong_type_and_invalid_utf8_old_text_never_dispatch(self):
        for old in ("", None, False, 7, [], {}, "\ud800", "\udfff"):
            with self.subTest(kind=type(old).__name__, value=ascii(old)):
                self.assert_preflight(self.arguments(old_text=old, path="missing/parent/file.txt"))

    def test_required_unknown_fields_and_invalid_content_never_dispatch(self):
        cases = []
        for field in ("path", "content", "expected_sha256"):
            arguments = self.arguments()
            del arguments[field]
            cases.append(arguments)
        cases.extend(self.arguments(content=value) for value in (None, False, [], "\ud800"))
        cases.append(self.arguments(unexpected="PRIVATE_VALUE"))
        for arguments in cases:
            with self.subTest(fields=list(arguments)):
                result = self.assert_preflight(arguments)
                self.assertNotIn("PRIVATE_VALUE", json.dumps(result))

    def test_null_or_malformed_edit_hash_never_dispatch(self):
        for value in (None, "", "f" * 63, "g" * 64, "A" * 64, False, 1):
            with self.subTest(value=value):
                self.assert_preflight(self.arguments(expected_sha256=value))

    def test_oversize_old_text_and_replacement_never_dispatch(self):
        for field in ("old_text", "content"):
            for value in ("x" * (tools.MAX_FILE_BYTES + 1),
                          "\u4e2d" * (tools.MAX_FILE_BYTES // 3 + 1)):
                with self.subTest(field=field, bytes=len(value.encode())):
                    self.assert_preflight(self.arguments(**{field: value}))

    def test_exact_one_megabyte_old_text_and_result_are_allowed(self):
        before = b"x" * tools.MAX_FILE_BYTES
        sha = self.seed(before)
        self.assert_success(self.edit(old_text=before.decode(), content="y" * tools.MAX_FILE_BYTES,
                                      expected_sha256=sha), before, b"y" * tools.MAX_FILE_BYTES)

    def test_result_growth_over_byte_limit_and_oversize_source_do_not_stage(self):
        for before in (b"OLD" + b"x" * (tools.MAX_FILE_BYTES - 3),
                       b"OLD" + b"x" * tools.MAX_FILE_BYTES):
            with self.subTest(size=len(before)):
                sha = self.seed(before)
                self.assert_no_publication(self.arguments(content="\u4e2d\u6587", expected_sha256=sha), "too_large")

    def test_read_only_policy_denies_local_edit(self):
        self.seed()
        executor = self.make_executor("read_only")
        with patch.object(executor, "_write") as dispatch:
            result = executor.execute("workspace_write", self.arguments())
        dispatch.assert_not_called()
        self.assert_error(result, "tool_denied")
        self.assertEqual((self.root / "sample.txt").read_bytes(), b"prefix OLD suffix")

    def test_protected_and_escaping_paths_never_stage_or_create(self):
        for path in (".env", ".git/config", "secrets.json", "nested/.ssh/id_rsa",
                     "../outside.txt", str(self.root / "absolute.txt")):
            with self.subTest(path=path):
                self.assert_no_publication(self.arguments(path=path))

    def test_hard_link_target_is_denied_without_modifying_either_name(self):
        sha = self.seed()
        target = self.root / "sample.txt"
        alias = self.root / "alias.txt"
        try:
            os.link(target, alias)
        except OSError as exc:
            self.skipTest(f"Hard links unavailable: {exc}")
        self.assert_no_publication(self.arguments(expected_sha256=sha), "unsafe_path")
        self.assertEqual(alias.read_bytes(), b"prefix OLD suffix")

    def test_invalid_utf8_and_binary_existing_file_are_rejected(self):
        for before in (b"OLD\xff", b"OLD\x00", b"OLD\x01"):
            with self.subTest(before=before):
                sha = self.seed(before)
                self.assert_no_publication(self.arguments(expected_sha256=sha), "binary_file")

    def test_stale_hash_cannot_apply_even_when_old_text_still_matches(self):
        stale = self.seed()
        self.seed(b"external prefix OLD suffix")
        self.assert_no_publication(self.arguments(expected_sha256=stale), "conflict")

    def test_real_thread_competition_between_edit_and_full_write_has_one_winner(self):
        for round_number in range(3):
            with self.subTest(round=round_number):
                before = b"prefix OLD suffix"
                sha = self.seed(before)
                barrier = threading.Barrier(4)
                executors = [self.make_executor() for _ in range(4)]

                def compete(number):
                    arguments = self.arguments(content=f"writer-{number}", expected_sha256=sha)
                    if number == 3:
                        del arguments["old_text"]
                    barrier.wait(timeout=10)
                    return executors[number].execute("workspace_write", arguments)

                with ThreadPoolExecutor(max_workers=4) as pool:
                    results = list(pool.map(compete, range(4)))
                winners = [(i, r) for i, r in enumerate(results) if r["status"] == "ok"]
                self.assertEqual(len(winners), 1, results)
                number, winner = winners[0]
                expected = (f"writer-{number}" if number == 3 else
                            f"prefix writer-{number} suffix").encode()
                self.assertEqual((self.root / "sample.txt").read_bytes(), expected)
                self.assertEqual(winner["sha256"], digest(expected))
                if number != 3:
                    self.assertEqual(winner["before_sha256"], sha)
                    self.assertEqual(winner["replacements"], 1)
                for i, result in enumerate(results):
                    if i != number:
                        self.assert_error(result, "conflict")
                self.assertFalse(list(self.root.rglob(".workspace-tmp-*")))

    def test_external_write_during_fsync_rejects_final_cas_and_cleans_staging(self):
        self.seed()
        fsync = os.fsync

        def external_write(fd):
            fsync(fd)
            (self.root / "sample.txt").write_bytes(b"external OLD update")

        with patch.object(tools.os, "fsync", side_effect=external_write):
            self.assert_error(self.edit(), "conflict")
        self.assertEqual((self.root / "sample.txt").read_bytes(), b"external OLD update")
        self.assertEqual([p.name for p in self.root.iterdir()], ["sample.txt"])

    def test_permissions_preserved_and_replace_failure_cleans_staging(self):
        before = b"prefix OLD suffix"
        self.seed(before)
        target = self.root / "sample.txt"
        target.chmod(0o640)
        self.addCleanup(target.chmod, 0o600)
        original_mode = stat.S_IMODE(target.stat().st_mode)
        self.assert_success(self.edit(), before, b"prefix NEW suffix")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), original_mode)
        self.seed(before)
        with patch.object(tools.os, "replace", side_effect=PermissionError("PRIVATE_HOST_DETAIL")):
            result = self.edit()
        self.assert_error(result)
        self.assertNotIn("PRIVATE_HOST_DETAIL", json.dumps(result))
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual([p.name for p in self.root.iterdir()], ["sample.txt"])

    def test_legacy_full_write_still_creates_updates_and_returns_original_fields(self):
        for content, expected, created in (("\ufeffold\r\n", None, True),
                                            ("whole new\r\n", digest("\ufeffold\r\n".encode()), False)):
            arguments = {"path": "new/parent/file.txt", "content": content, "expected_sha256": expected}
            result = self.executor.execute("workspace_write", arguments)
            body = content.encode()
            self.assertEqual(result, {"status": "ok", "path": arguments["path"],
                                      "sha256": digest(body), "bytes": len(body),
                                      "created": created, "truncated": False})
            self.assertEqual((self.root / arguments["path"]).read_bytes(), body)

    def test_public_arguments_redacts_bodies_and_hashes_only_valid_utf8(self):
        old = "PRIVATE_OLD_\u4e2d\U0001f600\r\n"
        arguments = self.arguments(old_text=old, content="PRIVATE_CONTENT")
        public = WorkspaceService._public_arguments("workspace_write", arguments)
        self.assertEqual(public, {"path": arguments["path"], "expected_sha256": arguments["expected_sha256"],
                                  "old_text_bytes": len(old.encode()), "old_text_sha256": digest(old.encode())})
        self.assertEqual(arguments["old_text"], old)
        self.assertNotIn("PRIVATE_", json.dumps(public))
        for old in ("\ud800", "prefix\udfff"):
            with self.subTest(old=ascii(old)):
                public = WorkspaceService._public_arguments("workspace_write", self.arguments(old_text=old))
                self.assertEqual(public, {"path": "sample.txt", "expected_sha256": arguments["expected_sha256"],
                                          "old_text_encoding": "invalid_utf8"})
                json.dumps(public, ensure_ascii=False).encode("utf-8")
        for old in (None, False, [], {"source": "PRIVATE_OLD"}):
            public = WorkspaceService._public_arguments("workspace_write", self.arguments(old_text=old))
            self.assertEqual(set(public), {"path", "expected_sha256"})
        public = WorkspaceService._public_arguments("workspace_write", {
            "path": "file.txt", "content": "PRIVATE_CONTENT", "expected_sha256": None,
        })
        self.assertEqual(public, {"path": "file.txt", "expected_sha256": None})


if __name__ == "__main__":
    unittest.main()
