"""Independent contract regressions for atomic, literal workspace batch edits."""

from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
import itertools
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from cater import workspace_tools as tools
from cater.workspace_tools import WorkspaceExecutor, WorkspacePolicy
from cater.workspaces import WorkspaceService


LIMIT = 1024 * 1024
BASELINE = b"head LEFT middle CENTER tail RIGHT\r\n"


def digest(body):
    return hashlib.sha256(body).hexdigest()


class WorkspaceBatchEditTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="cater-batch-edit-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "workspace"
        self.root.mkdir()
        self.executor = self.make_executor()

    def make_executor(self, mode="edit"):
        return WorkspaceExecutor(WorkspacePolicy.from_dict({
            "root": str(self.root), "mode": mode,
        }))

    def seed(self, body=BASELINE, path="sample.txt"):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        return digest(body)

    def arguments(self, **changes):
        return {
            "path": "sample.txt", "expected_sha256": digest(BASELINE),
            "edits": [
                {"old_text": "LEFT", "content": "first and longer"},
                {"old_text": "CENTER", "content": ""},
                {"old_text": "RIGHT", "content": "R"},
            ], **changes,
        }

    def execute(self, **changes):
        return self.executor.execute("workspace_write", self.arguments(**changes))

    def snapshot(self):
        return {p.relative_to(self.root).as_posix():
                None if p.is_dir() else p.read_bytes() for p in self.root.rglob("*")}

    def assert_error(self, result, code=None):
        self.assertEqual(result["status"], "error", result)
        self.assertIsInstance(result["error"]["message"], str)
        if code is not None:
            self.assertEqual(result["error"]["code"], code, result)
        json.dumps(result, ensure_ascii=False).encode("utf-8")

    def assert_success(self, result, before, after, replacements, path="sample.txt"):
        self.assertEqual(result, {
            "status": "ok", "path": path, "sha256": digest(after),
            "bytes": len(after), "created": False, "truncated": False,
            "replacements": replacements, "before_sha256": digest(before),
        })
        self.assertEqual((self.root / path).read_bytes(), after)
        self.assertFalse(list(self.root.rglob(".workspace-tmp-*")))

    def assert_rejected_without_staging(self, arguments, code=None, *, preflight=False):
        before = self.snapshot()
        with patch.object(self.executor, "_write", wraps=self.executor._write) as dispatch, \
                patch.object(self.executor, "_path", wraps=self.executor._path) as resolve, \
                patch.object(tools.tempfile, "mkstemp", wraps=tools.tempfile.mkstemp) as staging, \
                patch.object(tools.os, "replace", wraps=tools.os.replace) as publish:
            result = self.executor.execute("workspace_write", arguments)
        self.assert_error(result, code)
        staging.assert_not_called()
        publish.assert_not_called()
        for call in resolve.call_args_list:
            self.assertFalse(call.kwargs.get("create_parents", False))
        self.assertEqual(self.snapshot(), before)
        if preflight:
            dispatch.assert_not_called()
            resolve.assert_not_called()
            self.assertIs(result["complete"], True)
            self.assertEqual(result["outcome"], "not_started")
        return result

    def assert_preflight(self, arguments):
        return self.assert_rejected_without_staging(
            arguments, "invalid_arguments", preflight=True)

    def test_schema_exposes_bounded_closed_edit_objects_and_preserves_legacy_fields(self):
        schema = next(item["input_schema"] for item in self.executor.schemas()
                      if item["name"] == "workspace_write")
        self.assertEqual(set(schema["required"]), {"path", "expected_sha256"})
        self.assertFalse(schema["additionalProperties"])
        self.assertTrue({"content", "old_text", "edits"} <= schema["properties"].keys())
        edits = schema["properties"]["edits"]
        self.assertEqual(edits["type"], "array")
        self.assertEqual(edits["minItems"], 1)
        self.assertEqual(edits["maxItems"], 32)
        item = edits["items"]
        self.assertEqual(item["type"], "object")
        self.assertEqual(set(item["required"]), {"old_text", "content"})
        self.assertFalse(item["additionalProperties"])
        self.assertEqual(item["properties"]["old_text"]["type"], "string")
        self.assertEqual(item["properties"]["old_text"]["minLength"], 1)
        self.assertEqual(item["properties"]["content"]["type"], "string")

    def test_three_edits_share_one_baseline_and_publish_exactly_once(self):
        self.seed()
        arguments = self.arguments()
        original_arguments = copy.deepcopy(arguments)
        with patch.object(tools.tempfile, "mkstemp", wraps=tools.tempfile.mkstemp) as staging, \
                patch.object(tools.os, "replace", wraps=tools.os.replace) as publish:
            result = self.executor.execute("workspace_write", arguments)
        self.assert_success(result, BASELINE, b"head first and longer middle  tail R\r\n", 3)
        staging.assert_called_once()
        publish.assert_called_once()
        self.assertEqual(arguments, original_arguments)
        self.assertNotEqual(result["before_sha256"], digest(b"LEFTCENTERRIGHT"))

    def test_input_order_including_reverse_does_not_change_original_positions(self):
        fragments = self.arguments()["edits"]
        for order in itertools.permutations(fragments):
            with self.subTest(order=[item["old_text"] for item in order]):
                self.seed()
                self.assert_success(self.execute(edits=list(order)), BASELINE,
                                    b"head first and longer middle  tail R\r\n", 3)

    def test_replacements_match_original_text_and_do_not_chain(self):
        before = b"ALPHA|BETA|GAMMA"
        sha = self.seed(before)
        self.assert_success(self.execute(expected_sha256=sha, edits=[
            {"old_text": "ALPHA", "content": "BETA BETA"},
            {"old_text": "BETA", "content": "GAMMA"},
            {"old_text": "GAMMA", "content": "ALPHA"},
        ]), before, b"BETA BETA|GAMMA|ALPHA", 3)

    def test_text_introduced_by_another_edit_is_not_an_original_match(self):
        self.seed()
        self.assert_rejected_without_staging(self.arguments(edits=[
            {"old_text": "LEFT", "content": "INTRODUCED"},
            {"old_text": "INTRODUCED", "content": "incorrect chained edit"},
        ]), "conflict")

    def test_adjacent_ranges_and_empty_replacements_are_allowed(self):
        before = b"[ABCDEF]"
        for order in (["AB", "CD", "EF"], ["EF", "CD", "AB"]):
            with self.subTest(order=order):
                sha = self.seed(before)
                edits = [{"old_text": old, "content": {"AB": "", "CD": "middle", "EF": "!"}[old]}
                         for old in order]
                self.assert_success(self.execute(expected_sha256=sha, edits=edits),
                                    before, b"[middle!]", 3)

    def test_one_edit_can_delete_the_whole_file(self):
        before = b"only original fragment"
        sha = self.seed(before)
        self.assert_success(self.execute(expected_sha256=sha, edits=[
            {"old_text": before.decode(), "content": ""},
        ]), before, b"", 1)

    def test_literal_metacharacters_and_self_containing_replacement(self):
        before = b"head [a.*] / $1\\tail end"
        sha = self.seed(before)
        self.assert_success(self.execute(expected_sha256=sha, edits=[
            {"old_text": "[a.*]", "content": "[a.*][a.*]"},
            {"old_text": "$1\\tail", "content": "$1\\tail\\1"},
        ]), before, b"head [a.*][a.*] / $1\\tail\\1 end", 2)

    def test_thirty_two_unique_edits_are_allowed(self):
        tokens = [f"<{number:02d}>" for number in range(32)]
        before = "|".join(tokens).encode()
        sha = self.seed(before)
        self.assert_success(self.execute(expected_sha256=sha, edits=[
            {"old_text": token, "content": str(number)}
            for number, token in reversed(list(enumerate(tokens)))
        ]), before, "|".join(str(number) for number in range(32)).encode(), 32)

    def test_overlap_containment_and_duplicate_ranges_reject_whole_batch(self):
        cases = [("ABC", "CDE"), ("ABCDE", "BCD"), ("ABC", "ABC")]
        before = b"prefix ABCDE suffix"
        for left, right in cases:
            for order in ((left, right), (right, left)):
                with self.subTest(order=order):
                    sha = self.seed(before)
                    self.assert_rejected_without_staging(self.arguments(expected_sha256=sha, edits=[
                        {"old_text": old, "content": f"replacement-{number}"}
                        for number, old in enumerate(order)
                    ]), "conflict")

    def test_disjoint_repeated_match_is_ambiguous_even_if_another_edit_removes_one(self):
        before = b"UNIQUE OLD middle OLD"
        sha = self.seed(before)
        self.assert_rejected_without_staging(self.arguments(expected_sha256=sha, edits=[
            {"old_text": "UNIQUE OLD", "content": "removed"},
            {"old_text": "OLD", "content": "new"},
        ]), "conflict")

    def test_self_overlapping_literal_matches_are_ambiguous(self):
        for text, old in (("aaa", "aa"), ("ababa", "aba"), ("aaaa", "aaa")):
            with self.subTest(text=text, old=old):
                sha = self.seed(("UNIQUE " + text).encode())
                self.assert_rejected_without_staging(self.arguments(expected_sha256=sha, edits=[
                    {"old_text": "UNIQUE", "content": "changed"},
                    {"old_text": old, "content": "ambiguous"},
                ]), "conflict")

    def test_missing_match_at_any_position_rejects_without_partial_write(self):
        valid = self.arguments()["edits"]
        for position in range(4):
            with self.subTest(position=position):
                self.seed()
                edits = valid[:position] + [{"old_text": "ABSENT", "content": "never"}] + valid[position:]
                self.assert_rejected_without_staging(self.arguments(edits=edits), "conflict")

    def test_matching_does_not_normalize_unicode_newlines_whitespace_or_case(self):
        for source, old in (("caf\u00e9", "cafe\u0301"), ("a\r\nb", "a\nb"),
                            ("a  b", "a b"), ("Old", "OLD")):
            with self.subTest(source=ascii(source)):
                sha = self.seed(("UNIQUE|" + source).encode())
                self.assert_rejected_without_staging(self.arguments(expected_sha256=sha, edits=[
                    {"old_text": "UNIQUE", "content": "changed"},
                    {"old_text": old, "content": "never"},
                ]), "conflict")

    def test_missing_file_or_parent_never_creates_directories_or_stages(self):
        for path in ("missing.txt", "missing/parent/file.txt"):
            with self.subTest(path=path):
                self.assert_rejected_without_staging(self.arguments(path=path), "conflict")

    def test_edits_must_be_a_list_with_one_to_thirty_two_items(self):
        fragment = {"old_text": "LEFT", "content": "new"}
        for value in (None, False, 1, "edits", {}, (), (fragment,), [], [fragment] * 33):
            with self.subTest(kind=type(value).__name__, count=len(value) if isinstance(value, list) else None):
                self.assert_preflight(self.arguments(path="missing/parent/file.txt", edits=value))

    def test_each_edit_is_a_closed_object_with_both_required_fields(self):
        invalid = [None, False, 1, "literal", [], ("LEFT", "new"), {},
                   {"old_text": "LEFT"}, {"content": "new"},
                   {"old_text": "LEFT", "content": "new", "PRIVATE_EXTRA_KEY": "PRIVATE_VALUE"}]
        for fragment in invalid:
            for position in (0, 1):
                with self.subTest(fragment=ascii(fragment), position=position):
                    edits = [{"old_text": "CENTER", "content": "valid"}]
                    edits.insert(position, fragment)
                    result = self.assert_preflight(self.arguments(path="missing/parent/file.txt", edits=edits))
                    self.assertNotIn("PRIVATE_VALUE", json.dumps(result))

    def test_old_text_must_be_nonempty_string_and_content_must_be_string(self):
        invalid_values = (None, False, True, 0, 1.5, [], {}, b"text")
        for field in ("old_text", "content"):
            values = invalid_values + (("",) if field == "old_text" else ())
            for value in values:
                with self.subTest(field=field, kind=type(value).__name__):
                    edits = [{"old_text": "LEFT", "content": "valid"},
                             {"old_text": "RIGHT", "content": "valid", field: value}]
                    self.assert_preflight(self.arguments(path="missing/parent/file.txt", edits=edits))

    def test_invalid_utf8_in_any_edit_field_is_preflight_even_after_valid_edit(self):
        for field in ("old_text", "content"):
            for value in ("\ud800", "prefix\udfff", "\ud83d\ude80"):
                with self.subTest(field=field, value=ascii(value)):
                    self.assert_preflight(self.arguments(path="missing/parent/file.txt", edits=[
                        {"old_text": "LEFT", "content": "valid"},
                        {"old_text": "RIGHT", "content": "valid", field: value},
                    ]))

    def test_root_argument_structure_required_fields_and_types_are_preflight(self):
        cases = [None, False, [], "arguments"]
        for field in ("path", "expected_sha256", "edits"):
            arguments = self.arguments()
            del arguments[field]
            cases.append(arguments)
        cases.extend(self.arguments(path=value) for value in (None, False, 7, [], {}, "\ud800"))
        cases.append(self.arguments(PRIVATE_EXTRA_KEY={"value": "PRIVATE_VALUE"}))
        for index, arguments in enumerate(cases):
            with self.subTest(case=index):
                result = self.assert_preflight(arguments)
                self.assertNotIn("PRIVATE_VALUE", json.dumps(result))

    def test_edits_cannot_mix_with_root_content_or_old_text_even_when_empty_or_null(self):
        for field in ("content", "old_text"):
            for value in ("", "LEFT", None):
                with self.subTest(field=field, value=value):
                    self.assert_preflight(self.arguments(path="missing/parent/file.txt", **{field: value}))
        self.assert_preflight(self.arguments(content="new", old_text="LEFT"))

    def test_batch_requires_nonnull_lowercase_whole_file_digest(self):
        for value in (None, "", "f" * 63, "f" * 65, "g" * 64, "A" * 64,
                      " " + "f" * 64, "f" * 64 + "\n", False, 1, [], {}):
            with self.subTest(value=ascii(value)):
                self.assert_preflight(self.arguments(path="missing/parent/file.txt", expected_sha256=value))

    def test_combined_old_and_replacement_bytes_over_limit_is_preflight(self):
        # Each field and each edit fits separately; only the batch total exceeds 1 MiB.
        for fill in ("x" * (LIMIT // 2), "\u4e2d" * (LIMIT // 6)):
            edits = [{"old_text": "LEFT", "content": fill},
                     {"old_text": "RIGHT", "content": fill}]
            size = sum(len(edit[key].encode()) for edit in edits for key in ("old_text", "content"))
            with self.subTest(bytes=size):
                self.assertGreater(size, LIMIT)
                self.assert_preflight(self.arguments(path="missing/parent/file.txt", edits=edits))

    def test_combined_budget_includes_old_text_as_well_as_replacements(self):
        for old, content in (("x" * (LIMIT // 2), "y" * (LIMIT // 2 + 1)),
                             ("x" * (LIMIT + 1), ""),
                             ("x", "\U0001f680" * (LIMIT // 4))):
            with self.subTest(old_bytes=len(old.encode()), content_bytes=len(content.encode())):
                self.assert_preflight(self.arguments(path="missing/parent/file.txt", edits=[
                    {"old_text": old, "content": content},
                ]))

    def test_exact_combined_one_megabyte_budget_is_allowed_in_utf8_bytes(self):
        before = b"AB"
        for replacement in ("x" * (LIMIT - 3), "\u4e2d" * ((LIMIT - 3) // 3) + "x"):
            with self.subTest(unicode=replacement.startswith("\u4e2d")):
                sha = self.seed(before)
                edits = [{"old_text": "A", "content": replacement}, {"old_text": "B", "content": "!"}]
                self.assertEqual(sum(len(edit[key].encode()) for edit in edits
                                     for key in ("old_text", "content")), LIMIT)
                self.assert_success(self.execute(expected_sha256=sha, edits=edits),
                                    before, (replacement + "!").encode(), 2)

    def test_exact_one_megabyte_result_is_allowed_after_all_edits(self):
        before = b"A|B" + b"x" * (LIMIT - 3)
        sha = self.seed(before)
        # Applying the first edit on its own would exceed the result limit.
        self.assert_success(self.execute(expected_sha256=sha, edits=[
            {"old_text": "A", "content": "AA"}, {"old_text": "B", "content": ""},
        ]), before, b"AA|" + b"x" * (LIMIT - 3), 2)

    def test_growing_final_result_above_limit_never_stages(self):
        before = b"A|B" + b"x" * (LIMIT - 3)
        sha = self.seed(before)
        self.assert_rejected_without_staging(self.arguments(expected_sha256=sha, edits=[
            {"old_text": "A", "content": "\u4e2d"}, {"old_text": "B", "content": ""},
        ]), "too_large")

    def test_oversize_source_is_rejected_before_staging_even_if_edits_shrink_it(self):
        sha = self.seed(b"A|B" + b"x" * (LIMIT - 2))
        self.assert_rejected_without_staging(self.arguments(expected_sha256=sha, edits=[
            {"old_text": "A", "content": ""}, {"old_text": "B", "content": ""},
        ]), "too_large")

    def test_bom_crlf_and_unicode_are_preserved_with_byte_accurate_receipt(self):
        before = "\ufeff\u524d\U0001f600\r\n\u65e7\u6587\r\nSECOND\r\n\u540e\u7f00\r\n".encode()
        sha = self.seed(before)
        after = "\ufeff\u524d\U0001f600\r\n\u65b0\U0001f680\r\nline1\r\nline2\r\n\u540e\u7f00\r\n".encode()
        self.assert_success(self.execute(expected_sha256=sha, edits=[
            {"old_text": "SECOND\r\n", "content": "line1\r\nline2\r\n"},
            {"old_text": "\u65e7\u6587", "content": "\u65b0\U0001f680"},
        ]), before, after, 2)

    def test_bom_can_be_removed_explicitly_in_a_batch(self):
        before = "\ufeffOLD\r\nEND\r\n".encode()
        sha = self.seed(before)
        self.assert_success(self.execute(expected_sha256=sha, edits=[
            {"old_text": "OLD\r\n", "content": "NEW\r\n"},
            {"old_text": "\ufeff", "content": ""},
        ]), before, b"NEW\r\nEND\r\n", 2)

    def test_binary_or_invalid_utf8_source_cannot_be_partially_edited(self):
        for suffix in (b"\xff", b"\x00", b"\x01"):
            with self.subTest(suffix=suffix):
                sha = self.seed(BASELINE + suffix)
                self.assert_rejected_without_staging(self.arguments(expected_sha256=sha), "binary_file")

    def test_stale_whole_file_hash_rejects_even_when_all_fragments_still_match(self):
        stale = self.seed()
        self.seed(b"external prefix " + BASELINE)
        self.assert_rejected_without_staging(self.arguments(expected_sha256=stale), "conflict")

    def test_read_only_policy_never_dispatches_batch_write(self):
        self.seed()
        executor = self.make_executor("read_only")
        with patch.object(executor, "_write") as dispatch:
            result = executor.execute("workspace_write", self.arguments())
        dispatch.assert_not_called()
        self.assert_error(result, "tool_denied")
        self.assertEqual((self.root / "sample.txt").read_bytes(), BASELINE)

    def test_concurrent_batch_fragment_and_full_writes_have_one_cas_winner(self):
        for batch_only, round_number in itertools.product((True, False), range(3)):
            with self.subTest(batch_only=batch_only, round=round_number):
                sha = self.seed()
                barrier = threading.Barrier(4)
                executors = [self.make_executor() for _ in range(4)]
                arguments = [self.arguments(expected_sha256=sha, edits=[
                    {"old_text": "LEFT", "content": f"left-{number}"},
                    {"old_text": "RIGHT", "content": f"right-{number}"},
                ]) for number in range(4 if batch_only else 2)]
                expected = [f"head left-{number} middle CENTER tail right-{number}\r\n".encode()
                            for number in range(len(arguments))]
                if not batch_only:
                    arguments.extend([
                        {"path": "sample.txt", "expected_sha256": sha, "old_text": "LEFT", "content": "single"},
                        {"path": "sample.txt", "expected_sha256": sha, "content": "full"},
                    ])
                    expected.extend([b"head single middle CENTER tail RIGHT\r\n", b"full"])

                def compete(number):
                    barrier.wait(timeout=10)
                    return executors[number].execute("workspace_write", arguments[number])

                with ThreadPoolExecutor(max_workers=4) as pool:
                    results = list(pool.map(compete, range(4)))
                winners = [number for number, result in enumerate(results) if result["status"] == "ok"]
                self.assertEqual(len(winners), 1, results)
                winner = winners[0]
                self.assertEqual((self.root / "sample.txt").read_bytes(), expected[winner])
                self.assertEqual(results[winner]["sha256"], digest(expected[winner]))
                self.assertEqual(results[winner]["bytes"], len(expected[winner]))
                if "edits" in arguments[winner] or "old_text" in arguments[winner]:
                    self.assertEqual(results[winner]["before_sha256"], sha)
                    self.assertEqual(results[winner]["replacements"],
                                     2 if "edits" in arguments[winner] else 1)
                for number, result in enumerate(results):
                    if number != winner:
                        self.assert_error(result, "conflict")
                self.assertFalse(list(self.root.rglob(".workspace-tmp-*")))

    def test_external_change_after_staging_is_rejected_by_final_hash_check(self):
        self.seed()
        target = self.root / "sample.txt"
        original_stat = target.stat()
        external = BASELINE.replace(b"head", b"host")
        fsync = os.fsync

        def external_write(fd):
            fsync(fd)
            target.write_bytes(external)
            # Preserve size and mtime so the final check must also verify content.
            os.utime(target, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))

        with patch.object(tools.os, "fsync", side_effect=external_write) as sync, \
                patch.object(tools.os, "replace", wraps=tools.os.replace) as publish:
            result = self.execute()
        sync.assert_called_once()
        publish.assert_not_called()
        self.assert_error(result, "conflict")
        self.assertEqual(self.snapshot(), {"sample.txt": external})

    def test_failed_atomic_replace_preserves_original_and_cleans_staging(self):
        self.seed()
        with patch.object(tools.os, "replace", side_effect=PermissionError("PRIVATE_HOST_DETAIL")) as publish:
            result = self.execute()
        publish.assert_called_once()
        self.assert_error(result)
        self.assertNotIn("PRIVATE_HOST_DETAIL", json.dumps(result))
        self.assertEqual(self.snapshot(), {"sample.txt": BASELINE})

    def test_legacy_full_write_create_update_and_single_fragment_remain_supported(self):
        path = "new/parent/file.txt"
        before = b"head OLD tail\r\n"
        result = self.executor.execute("workspace_write", {
            "path": path, "content": before.decode(), "expected_sha256": None,
        })
        self.assertEqual(result, {"status": "ok", "path": path, "sha256": digest(before),
                                  "bytes": len(before), "created": True, "truncated": False})
        self.assertEqual((self.root / path).read_bytes(), before)
        after = b"head NEW tail\r\n"
        result = self.executor.execute("workspace_write", {
            "path": path, "content": "NEW", "old_text": "OLD", "expected_sha256": digest(before),
        })
        self.assert_success(result, before, after, 1, path)
        final = "\ufeffcomplete rewrite\r\n".encode()
        result = self.executor.execute("workspace_write", {
            "path": path, "content": final.decode(), "expected_sha256": digest(after),
        })
        self.assertEqual(result, {"status": "ok", "path": path, "sha256": digest(final),
                                  "bytes": len(final), "created": False, "truncated": False})
        self.assertEqual((self.root / path).read_bytes(), final)

    def assert_public_summary(self, arguments, markers=()):
        original = copy.deepcopy(arguments)
        public = WorkspaceService._public_arguments("workspace_write", arguments)
        self.assertIsInstance(public, dict)
        encoded = json.dumps(public, ensure_ascii=False).encode("utf-8")
        self.assertLessEqual(len(encoded), 32 * 1024, "Audit summary must remain bounded")
        self.assertEqual(public.get("path"), arguments["path"])
        self.assertEqual(public.get("expected_sha256"), arguments["expected_sha256"])
        self.assertTrue(set(public) - {"path", "expected_sha256"}, "Keep bounded batch metadata")
        for marker in markers:
            self.assertNotIn(marker.encode(), encoded)
        self.assertEqual(arguments, original, "Audit redaction must not mutate executor arguments")
        return public

    def test_audit_redacts_all_fragment_bodies_without_binding_summary_field_names(self):
        self.assert_public_summary(self.arguments(edits=[
            {"old_text": "PRIVATE_OLD_\u4e2d\U0001f600\r\n", "content": "PRIVATE_NEW_\u6587"},
            {"old_text": "PRIVATE_SECOND_OLD", "content": "PRIVATE_SECOND_NEW"},
        ]), ("PRIVATE_OLD_", "PRIVATE_NEW_", "PRIVATE_SECOND_OLD", "PRIVATE_SECOND_NEW"))

    def test_audit_drops_unknown_root_and_nested_keys_even_for_rejected_mixed_request(self):
        self.assert_public_summary(self.arguments(
            content="PRIVATE_ROOT_CONTENT", old_text="PRIVATE_ROOT_OLD",
            PRIVATE_ROOT_EXTRA={"secret": "PRIVATE_ROOT_VALUE"}, edits=[
                {"old_text": "PRIVATE_OLD", "content": "PRIVATE_NEW",
                 "PRIVATE_EDIT_EXTRA": {"secret": "PRIVATE_EDIT_VALUE"}},
            ]), ("PRIVATE_ROOT_CONTENT", "PRIVATE_ROOT_OLD", "PRIVATE_ROOT_EXTRA", "PRIVATE_ROOT_VALUE",
                 "PRIVATE_OLD", "PRIVATE_NEW", "PRIVATE_EDIT_EXTRA", "PRIVATE_EDIT_VALUE"))

    def test_audit_invalid_utf8_and_malformed_edits_stay_serializable_and_redacted(self):
        invalid = [None, False, 1, "PRIVATE_RAW_STRING", {"PRIVATE_EXTRA": "PRIVATE_VALUE"},
                   ["PRIVATE_ITEM", None, {"old_text": "PRIVATE_OLD\ud800", "content": "PRIVATE_NEW\udfff",
                                            "PRIVATE_EXTRA": "PRIVATE_VALUE"}],
                   [{"old_text": {"secret": "PRIVATE_VALUE"}, "content": ["PRIVATE_VALUE"]}]]
        for edits in invalid:
            with self.subTest(kind=type(edits).__name__):
                self.assert_public_summary(self.arguments(edits=edits),
                                           ("PRIVATE_RAW_STRING", "PRIVATE_EXTRA", "PRIVATE_VALUE",
                                            "PRIVATE_ITEM", "PRIVATE_OLD", "PRIVATE_NEW"))

    def test_audit_summary_is_bounded_for_large_bodies_and_excess_edit_count(self):
        body = "PRIVATE_LARGE_BODY" + "x" * (128 * 1024)
        for edits in ([{"old_text": body, "content": body}] * 32,
                      [{"old_text": "PRIVATE_OLD", "content": "PRIVATE_NEW"}] * 2048):
            with self.subTest(count=len(edits)):
                self.assert_public_summary(self.arguments(edits=edits),
                                           ("PRIVATE_LARGE_BODY", "PRIVATE_OLD", "PRIVATE_NEW"))


if __name__ == "__main__":
    unittest.main()
