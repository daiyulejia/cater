from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from cater import workspace_tools as tools
from cater.workspace_tools import WorkspaceExecutor, WorkspacePolicy


def _concurrent_write(config, expected, content, start, results):
    executor = WorkspaceExecutor(WorkspacePolicy.from_dict(config))
    start.wait(10)
    results.put(executor.execute("workspace_write", {
        "path": "shared.txt", "content": content, "expected_sha256": expected,
    }))


class WorkspaceToolsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "workspace"
        self.root.mkdir()
        self.executor = self.make_executor()

    def make_executor(self, mode="edit", **kwargs):
        return WorkspaceExecutor(WorkspacePolicy.from_dict({"root": str(self.root), "mode": mode, **kwargs}))

    def run_executor(self, **kwargs):
        return self.make_executor("execute", commands={"python": [sys.executable, "-I", "-c"]}, **kwargs)

    def write(self, content="new", expected=None, path="sample.txt", executor=None):
        return (executor or self.executor).execute("workspace_write", {
            "path": path, "content": content, "expected_sha256": expected,
        })

    def assert_error(self, result, code=None):
        self.assertEqual(result["status"], "error", result)
        self.assertIsInstance(result["error"]["message"], str)
        if code is not None:
            self.assertEqual(result["error"]["code"], code, result)
        json.dumps(result)

    def test_policy_normalization_fingerprint_and_detachment(self):
        config = {"root": str(self.root / "."), "mode": "execute",
                  "commands": {"z": [sys.executable], "a": [sys.executable, "-I"]},
                  "env_allowlist": ["VISIBLE", "VISIBLE", "LANG"]}
        policy = WorkspacePolicy.from_dict(config)
        self.assertRegex(policy.fingerprint, r"^[a-f0-9]{64}$")
        self.assertEqual(policy.as_dict()["timeout"], 60)
        self.assertEqual(policy.as_dict()["env_allowlist"], ["LANG", "VISIBLE"])
        restored = WorkspacePolicy.from_dict(json.loads(json.dumps(policy.as_dict())))
        self.assertEqual(restored.fingerprint, policy.fingerprint)
        config["commands"]["a"].append("changed")
        exported = policy.as_dict()
        exported["commands"]["a"].append("also changed")
        self.assertEqual(policy.commands["a"], (sys.executable, "-I"))
        self.assertEqual(policy.fingerprint, restored.fingerprint)
        other = WorkspacePolicy.from_dict({**policy.as_dict(), "timeout": 2})
        self.assertNotEqual(policy.fingerprint, other.fingerprint)

    def test_invalid_policies(self):
        valid = {"root": str(self.root), "mode": "read_only"}
        changes = [
            {"root": "."}, {"root": str(self.base / "missing")},
            {"root": str(self.root / "..")}, {"mode": "admin"},
            {"commands": {"python": [sys.executable]}}, {"commands": []},
            {"timeout": True}, {"timeout": 0}, {"timeout": 601}, {"timeout": float("nan")},
            {"timeout": float("inf")}, {"env_allowlist": "PATH"},
            {"env_allowlist": ["OPENAI_API_KEY"]}, {"env_allowlist": ["APIKEY"]},
            {"env_allowlist": ["ACCESS_TOKEN"]}, {"env_allowlist": ["bad=name"]}, {"id": "not-stripped"},
        ]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                WorkspacePolicy.from_dict({**valid, **change})
        for commands in ({"x": "python"}, {"x": []}, {"bad/alias": ["python"]}, {"x": [""]}, {"x": ["a\0b"]}):
            with self.subTest(commands=commands), self.assertRaises(ValueError):
                WorkspacePolicy.from_dict({**valid, "mode": "execute", "commands": commands})

    def test_schemas_and_mode_gates(self):
        expected = {"workspace_list", "workspace_read", "workspace_search"}
        for mode in ("read_only", "edit", "execute"):
            executor = self.make_executor(mode)
            schemas = executor.schemas()
            names = {item["name"] for item in schemas}
            self.assertEqual(names, expected | ({"workspace_write"} if mode != "read_only" else set())
                             | ({"workspace_run"} if mode == "execute" else set()))
            for schema in schemas:
                self.assertEqual(set(schema), {"name", "description", "input_schema"})
                self.assertFalse(schema["input_schema"]["additionalProperties"])
            json.dumps(schemas)
        read_only = self.make_executor("read_only")
        self.assert_error(self.write(executor=read_only), "tool_denied")
        self.assert_error(read_only.execute("workspace_run", {"command": "python"}), "tool_denied")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_read_real_hash_bytes_and_byte_window(self):
        body = "a\r\n\u4e2d\u6587\nlast".encode("utf-8")
        (self.root / "sample.txt").write_bytes(body)
        result = self.executor.execute("workspace_read", {"path": "sample.txt"})
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["content"], body.decode())
        self.assertEqual(result["bytes"], len(body))
        self.assertEqual(result["sha256"], hashlib.sha256(body).hexdigest())
        self.assertFalse(result["truncated"])
        window = self.executor.execute("workspace_read", {"path": "sample.txt", "offset": 3, "limit": 3})
        self.assertEqual(window["content"], "\u4e2d")
        self.assertEqual(window["returned_bytes"], 3)
        self.assertTrue(window["truncated"])
        self.assertEqual(window["sha256"], result["sha256"])

    def test_schema_explains_bound_root_and_existing_file_updates(self):
        schemas = {tool['name']: tool for tool in self.run_executor().schemas()}
        for name in ('workspace_list', 'workspace_read', 'workspace_search', 'workspace_write', 'workspace_run'):
            field = 'cwd' if name == 'workspace_run' else 'path'
            description = schemas[name]['input_schema']['properties'][field]['description']
            self.assertIn('bound workspace root', description)
            self.assertIn('do not prepend', description)
        update = schemas['workspace_write']
        self.assertIn('workspace_read', update['description'])
        self.assertIn('sha256', update['input_schema']['properties']['expected_sha256']['description'])

    def test_null_update_error_explains_recovery_without_changing_the_file(self):
        self.write('original')
        rejected = self.write('accidental overwrite')
        self.assert_error(rejected, 'conflict')
        self.assertIn('workspace_read', rejected['error']['message'])
        self.assertIn('sha256', rejected['error']['message'])
        self.assertEqual((self.root / 'sample.txt').read_text(), 'original')
        current = self.executor.execute('workspace_read', {'path': 'sample.txt'})
        recovered = self.write('deliberate update', current['sha256'])
        self.assertEqual(recovered['status'], 'ok')
        self.assertEqual((self.root / 'sample.txt').read_text(), 'deliberate update')

    def test_write_new_nested_utf8_and_cas_existing(self):
        first = self.write("\u4e2d\u6587\r\n", path="new/sub/sample.txt")
        self.assertEqual(first["status"], "ok", first)
        self.assertTrue(first["created"])
        self.assertEqual(first["bytes"], 8)
        self.assertEqual((self.root / "new/sub/sample.txt").read_bytes(), "\u4e2d\u6587\r\n".encode())
        self.assert_error(self.write("overwrite", path="new/sub/sample.txt"), "conflict")
        second = self.write("updated", first["sha256"], "new/sub/sample.txt")
        self.assertEqual(second["status"], "ok")
        self.assertFalse(second["created"])
        self.assertEqual((self.root / "new/sub/sample.txt").read_bytes(), b"updated")
        self.assert_error(self.write("stale", first["sha256"], "new/sub/sample.txt"), "conflict")
        self.assertEqual((self.root / "new/sub/sample.txt").read_bytes(), b"updated")
        self.assertFalse(any("lock" in p.name or "workspace-tmp" in p.name for p in self.root.rglob("*")))

    def test_read_65536_capture_contract_and_full_hash_on_truncated_preview(self):
        target = self.root / "capture.txt"
        for body in (b"x" * 65536, b"x" * 65537, "\u4e2d".encode() * 22000):
            with self.subTest(size=len(body)):
                target.write_bytes(body)
                result = self.executor.execute("workspace_read", {"path": "capture.txt", "limit": 65536})
                self.assertEqual(result["status"], "ok", result)
                self.assertLessEqual(len(result["content"].encode("utf-8")), 65536)
                self.assertEqual(result["returned_bytes"], len(result["content"].encode("utf-8")))
                self.assertEqual(result["bytes"], len(body))
                self.assertEqual(result["sha256"], hashlib.sha256(body).hexdigest())
                self.assertEqual(result["truncated"], len(body) > 65536)
                if not result["truncated"]:
                    self.assertEqual(result["content"].encode("utf-8"), body)

    def test_missing_expected_hash_and_invalid_request_make_no_directories(self):
        for arguments in (
            {"path": "new/sub/file.txt", "content": "x"},
            {"path": "new/sub/file.txt", "content": "x", "expected_sha256": "invalid"},
            {"path": "new/sub/file.txt", "content": ["x"], "expected_sha256": None},
            {"path": "new/sub/file.txt", "content": "x", "expected_sha256": None, "batch": []},
            {"path": "new/sub/file.txt", "content": "x" * (tools.MAX_FILE_BYTES + 1), "expected_sha256": None},
        ):
            self.assert_error(self.executor.execute("workspace_write", arguments))
            self.assertEqual(list(self.root.iterdir()), [])
        self.assert_error(self.write("x", "0" * 64), "conflict")

    def test_external_modification_during_staging_rejects_cas(self):
        first = self.write("original")
        fsync = os.fsync

        def modify(fd):
            fsync(fd)
            (self.root / "sample.txt").write_bytes(b"concurrent external edit")

        with patch.object(tools.os, "fsync", side_effect=modify):
            result = self.write("proposed", first["sha256"])
        self.assert_error(result, "conflict")
        self.assertEqual((self.root / "sample.txt").read_bytes(), b"concurrent external edit")
        self.assertEqual([p.name for p in self.root.iterdir()], ["sample.txt"])

    def test_creation_cannot_overwrite_arrival_even_after_final_check(self):
        link = os.link

        def arrive(source, target, **kwargs):
            Path(target).write_bytes(b"concurrent arrival")
            return link(source, target, **kwargs)

        with patch.object(tools.os, "link", side_effect=arrive):
            self.assert_error(self.write())
        self.assertEqual((self.root / "sample.txt").read_bytes(), b"concurrent arrival")

    def test_external_modification_during_permission_copy_rejects_cas(self):
        first = self.write("original")
        chmod = os.chmod

        def modify(path, mode):
            chmod(path, mode)
            (self.root / "sample.txt").write_bytes(b"concurrent permission-time edit")

        with patch.object(tools.os, "chmod", side_effect=modify):
            result = self.write("proposed", first["sha256"])
        self.assert_error(result, "conflict")
        self.assertEqual((self.root / "sample.txt").read_bytes(), b"concurrent permission-time edit")

    def test_replace_failure_preserves_file_and_redacts_error(self):
        first = self.write("original")
        with patch.object(tools.os, "replace", side_effect=PermissionError("SECRET-OS-MESSAGE /private/host")):
            result = self.write("proposed", first["sha256"])
        self.assert_error(result)
        self.assertNotIn("SECRET-OS-MESSAGE", json.dumps(result))
        self.assertEqual((self.root / "sample.txt").read_bytes(), b"original")
        self.assertEqual([p.name for p in self.root.iterdir()], ["sample.txt"])

    def test_thread_cas_has_one_winner(self):
        first = self.write("original", path="shared.txt")
        barrier = threading.Barrier(4)

        def update(number):
            barrier.wait(10)
            return self.write(str(number), first["sha256"], "shared.txt")

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(update, range(4)))
        winners = [r for r in results if r["status"] == "ok"]
        self.assertEqual(len(winners), 1, results)
        self.assertEqual(hashlib.sha256((self.root / "shared.txt").read_bytes()).hexdigest(), winners[0]["sha256"])

    def test_cross_process_create_and_update_cas_have_one_winner(self):
        context = multiprocessing.get_context("spawn")
        for expected in (None, hashlib.sha256(b"baseline").hexdigest()):
            with self.subTest(expected=expected):
                target = self.root / "shared.txt"
                if target.exists():
                    target.unlink()
                if expected is not None:
                    target.write_bytes(b"baseline")
                start, results = context.Event(), context.Queue()
                processes = [context.Process(target=_concurrent_write, args=(
                    self.executor.policy.as_dict(), expected, f"writer-{i}", start, results,
                )) for i in range(3)]
                try:
                    for process in processes:
                        process.start()
                    start.set()
                    received = [results.get(timeout=20) for _ in processes]
                    for process in processes:
                        process.join(10)
                        self.assertEqual(process.exitcode, 0)
                    winners = [r for r in received if r["status"] == "ok"]
                    self.assertEqual(len(winners), 1, received)
                    self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), winners[0]["sha256"])
                finally:
                    for process in processes:
                        if process.is_alive():
                            process.terminate()
                            process.join(5)
                    results.close()

    def test_escaping_portable_and_windows_alias_paths_are_rejected(self):
        outside = self.base / "outside.txt"
        outside.write_bytes(b"private")
        paths = ["../outside.txt", "a/../outside.txt", "a\\..\\outside.txt", str(outside),
                 "/outside.txt", "C:outside.txt", "C:\\outside.txt", "\\\\host\\share\\file", "file:stream",
                 "NUL", "CON.txt", "COM1", "LPT9.txt", "COM\u00b9.txt", "a. ", "a.", "a\0b", "a\nb",
                 "a/\uff0e\uff0e/b", "a\uff0fb", "ENV~1", "a/\ud800"]
        for path in paths:
            with self.subTest(path=repr(path)):
                self.assert_error(self.executor.execute("workspace_read", {"path": path}))
                self.assert_error(self.write(path=path))
        self.assertEqual(outside.read_bytes(), b"private")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_protected_paths_are_hidden_and_rejected_by_every_path_tool(self):
        for name in (".env", ".ENV.production", "project.env", ".git", ".ssh", ".aws", ".azure",
                     ".gnupg", ".kube", ".npmrc", ".netrc", "id_ed25519", "service.pem", "secrets.json"):
            (self.root / name).write_bytes(b"secret-needle")
            for tool in ("workspace_read", "workspace_list", "workspace_search"):
                arguments = {"path": name, **({"query": "needle"} if tool == "workspace_search" else {})}
                self.assert_error(self.executor.execute(tool, arguments), "protected_path")
            self.assert_error(self.write(path=name), "protected_path")
        self.assertEqual(self.executor.execute("workspace_list", {})["entries"], [])
        self.assertEqual(self.executor.execute("workspace_search", {"query": "needle"})["matches"], [])
        (self.root / ".env-dir").mkdir()
        self.assert_error(self.run_executor().execute("workspace_run", {"command": "python", "cwd": ".env-dir"}))

    def test_binary_special_and_oversized_files_are_bounded(self):
        for name, body in (("nul.bin", b"hello\0world"), ("invalid.bin", b"\xff\xfe"),
                           ("large.txt", b"x" * (tools.MAX_FILE_BYTES + 1))):
            (self.root / name).write_bytes(body)
            self.assert_error(self.executor.execute("workspace_read", {"path": name}))
        result = self.executor.execute("workspace_search", {"query": "hello"})
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["matches"], [])
        self.assert_error(self.write("nul\0content"), "binary_file")
        if os.name != "nt":
            os.mkfifo(self.root / "fifo")
            self.assert_error(self.executor.execute("workspace_read", {"path": "fifo"}), "unsafe_path")

    def test_search_is_literal_skips_vendor_and_reports_file_hashes(self):
        body = b"first a.*b literal\nother aZZb\nlast a.*b\n"
        (self.root / "source.txt").write_bytes(body)
        for name in ("vendor", "node_modules", ".venv", ".git"):
            (self.root / name).mkdir()
            (self.root / name / "hidden.txt").write_bytes(b"a.*b")
        (self.root / "binary.dat").write_bytes(b"a.*b\0")
        result = self.executor.execute("workspace_search", {"query": "a.*b"})
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual([m["line"] for m in result["matches"]], [1, 3])
        self.assertTrue(all(m["path"] == "source.txt" and m["sha256"] == hashlib.sha256(body).hexdigest()
                            and m["bytes"] == len(body) for m in result["matches"]))
        limited = self.executor.execute("workspace_search", {"query": "a.*b", "limit": 1})
        self.assertEqual(len(limited["matches"]), 1)
        self.assertTrue(limited["truncated"])
        self.assert_error(self.executor.execute("workspace_search", {"query": "a.*b", "path": "vendor"}))

    def test_list_read_and_search_output_limits(self):
        (self.root / "long.txt").write_bytes(b"x" * 40000)
        result = self.executor.execute("workspace_read", {"path": "long.txt"})
        self.assertEqual(result["returned_bytes"], tools.DEFAULT_READ_BYTES)
        self.assertTrue(result["truncated"])
        for number in range(tools.MAX_ENTRIES + 2):
            (self.root / f"f{number}.txt").write_bytes(b"x\n" * 10)
        result = self.executor.execute("workspace_list", {})
        self.assertEqual(len(result["entries"]), tools.MAX_ENTRIES)
        self.assertTrue(result["truncated"])
        result = self.executor.execute("workspace_search", {"query": "x", "limit": 200})
        self.assertLessEqual(len(json.dumps(result).encode()), tools.MAX_RESULT_BYTES + 1000)
        self.assertTrue(result["truncated"])

    def test_search_budget_exhaustion_without_matches(self):
        (self.root / "sub").mkdir()
        (self.root / "sub" / "data.txt").write_bytes(b"unmatched")
        with patch.object(tools, "MAX_VISITED", 2):
            result = self.executor.execute("workspace_search", {"query": "missing"})
        self.assertEqual(result["status"], "ok", result)
        self.assertTrue(result["truncated"])

    def test_regular_path_and_permission_errors_do_not_escape_or_leak(self):
        self.assert_error(self.executor.execute("workspace_read", {"path": "missing"}))
        with patch.object(tools, "_read_file", side_effect=PermissionError("PRIVATE_TOKEN=value")):
            result = self.executor.execute("workspace_read", {"path": "something"})
        self.assert_error(result)
        self.assertNotIn("PRIVATE", json.dumps(result))
        for name, arguments in (("unknown", {}), ("workspace_read", {}),
                                ("workspace_read", {"path": "x", "limit": True}),
                                ("workspace_read", {"path": "x", "offset": -1}),
                                ("workspace_search", {"query": ""}),
                                ("workspace_list", {"paths": []}), ("workspace_read", [])):
            self.assert_error(self.executor.execute(name, arguments))

    def test_hardlinks_rejected_for_read_write_search_and_listing(self):
        outside = self.base / "outside.txt"
        outside.write_bytes(b"private-needle")
        os.link(outside, self.root / "linked.txt")
        self.assert_error(self.executor.execute("workspace_read", {"path": "linked.txt"}), "unsafe_path")
        self.assert_error(self.write("change", hashlib.sha256(outside.read_bytes()).hexdigest(), "linked.txt"), "unsafe_path")
        self.assertEqual(self.executor.execute("workspace_list", {})["entries"], [])
        self.assertEqual(self.executor.execute("workspace_search", {"query": "needle"})["matches"], [])
        self.assertEqual(outside.read_bytes(), b"private-needle")

    def test_symlinks_and_linked_root_ancestors_are_rejected(self):
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "private.txt").write_bytes(b"needle")
        link = self.root / "link"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"symlink privilege unavailable: {exc}")
        self.assert_error(self.executor.execute("workspace_read", {"path": "link/private.txt"}), "unsafe_path")
        self.assert_error(self.write(path="link/new/file.txt"), "unsafe_path")
        self.assertEqual(self.executor.execute("workspace_search", {"query": "needle"})["matches"], [])
        with self.assertRaises(ValueError):
            WorkspacePolicy.from_dict({"root": str(link)})
        nested = outside / "nested"
        nested.mkdir()
        with self.assertRaises(ValueError):
            WorkspacePolicy.from_dict({"root": str(link / "nested")})
        self.assertFalse((outside / "new").exists())

    def test_reparse_attribute_rejected_without_symlink_privileges(self):
        fake = SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_file_attributes=0x400)
        with patch.object(Path, "lstat", return_value=fake):
            self.assert_error(self.executor.execute("workspace_list", {}), "unsafe_path")

    def test_symlink_mode_rejected_without_symlink_privileges(self):
        fake = SimpleNamespace(st_mode=stat.S_IFLNK | 0o700, st_file_attributes=0)
        with patch.object(Path, "lstat", return_value=fake):
            self.assert_error(self.executor.execute("workspace_list", {}), "unsafe_path")

    @unittest.skipUnless(os.name == "nt", "Windows junction test")
    def test_real_windows_junction_and_root_reparse_are_rejected(self):
        import _winapi
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "private.txt").write_bytes(b"needle")
        junction = self.root / "junction"
        _winapi.CreateJunction(str(outside), str(junction))
        self.assert_error(self.executor.execute("workspace_read", {"path": "junction/private.txt"}), "unsafe_path")
        self.assert_error(self.write(path="junction/new/file.txt"), "unsafe_path")
        self.assert_error(self.run_executor().execute("workspace_run", {"command": "python", "cwd": "junction"}))
        self.assertEqual(self.executor.execute("workspace_search", {"query": "needle"})["matches"], [])
        with self.assertRaises(ValueError):
            WorkspacePolicy.from_dict({"root": str(junction)})
        self.assertFalse((outside / "new").exists())

    @unittest.skipUnless(os.name == "nt", "Windows junction test")
    def test_executor_rechecks_root_after_policy_creation(self):
        import _winapi
        outside = self.base / "outside"
        outside.mkdir()
        self.root.rmdir()
        _winapi.CreateJunction(str(outside), str(self.root))
        self.assert_error(self.executor.execute("workspace_list", {}), "unsafe_path")
        self.assert_error(self.write(path="new/escape.txt"), "unsafe_path")
        self.assertEqual(list(outside.iterdir()), [])

    def test_run_minimal_environment_and_explicit_nonsecret_allowlist(self):
        executor = self.run_executor(env_allowlist=["WORKSPACE_VISIBLE"])
        code = "import os,json; print(json.dumps(dict(os.environ)))"
        with patch.dict(os.environ, {"OPENAI_API_KEY": "never-inherit", "APIKEY": "never-inherit",
                                     "OTHER_PRIVATE": "never-inherit", "PYTHONPATH": "never-inherit",
                                     "WORKSPACE_VISIBLE": "yes"}):
            result = executor.execute("workspace_run", {"command": "python", "args": [code]})
        self.assertEqual(result["status"], "ok", result)
        env = json.loads(result["stdout"]["excerpt"])
        self.assertEqual(env["WORKSPACE_VISIBLE"], "yes")
        self.assertNotIn("never-inherit", result["output"])
        for key in ("OPENAI_API_KEY", "APIKEY", "OTHER_PRIVATE", "PYTHONPATH"):
            self.assertNotIn(key, env)

    def test_run_exact_arguments_cwd_stream_hashes_and_exit_observation(self):
        executor = self.run_executor()
        (self.root / "sub").mkdir()
        code = "import os,sys,json; print(json.dumps([os.getcwd(),sys.argv[1:]])); sys.stderr.write('diagnostic')"
        result = executor.execute("workspace_run", {"command": "python", "args": [code, "a b", ";echo nope"], "cwd": "sub"})
        self.assertEqual(result["status"], "ok", result)
        actual_cwd, actual_args = json.loads(result["stdout"]["excerpt"])
        self.assertEqual(Path(actual_cwd).resolve(), (self.root / "sub").resolve())
        self.assertEqual(actual_args, ["a b", ";echo nope"])
        self.assertEqual(result["stderr"]["excerpt"], "diagnostic")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["outcome"], "passed")
        self.assertNotIn("accepted", json.dumps(result))
        for stream in ("stdout", "stderr"):
            self.assertEqual(result[stream]["sha256"], hashlib.sha256(result[stream]["excerpt"].encode()).hexdigest())
            self.assertEqual(result[stream]["bytes"], len(result[stream]["excerpt"].encode()))
        failed = executor.execute("workspace_run", {"command": "python", "args": [
            "import sys; print('failed output'); sys.stderr.write('failure diagnostic'); sys.exit(7)",
        ]})
        self.assert_error(failed)
        self.assertEqual(failed["exit_code"], 7)
        self.assertEqual(failed["outcome"], "failed")
        self.assertIsInstance(failed["stdout"], dict)
        self.assertIsInstance(failed["stderr"], dict)
        self.assertIn("failed output", failed["stdout"]["excerpt"])
        self.assertEqual(failed["stderr"]["excerpt"], "failure diagnostic")

    def test_run_output_truncation_keeps_real_digest_and_byte_count(self):
        body = b"\xff" * 30000
        result = self.run_executor().execute("workspace_run", {
            "command": "python", "args": ["import sys; sys.stdout.buffer.write(b'\\xff'*30000)"],
        })
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["stdout"]["bytes"], len(body))
        self.assertEqual(result["stdout"]["sha256"], hashlib.sha256(body).hexdigest())
        self.assertLessEqual(len(result["stdout"]["excerpt"].encode()), 8192)
        self.assertTrue(result["truncated"])
        self.assertTrue(result["complete"])

    def test_unknown_alias_invalid_run_arguments_and_timeout_ceiling(self):
        executor = self.run_executor(timeout=2)
        for args in ({"command": "unconfigured"}, {"command": "python", "args": "code"},
                     {"command": "python", "args": ["a\0b"]}, {"command": "python", "args": [42]},
                     {"command": "python", "cwd": "../"}, {"command": "python", "timeout": 3},
                     {"command": "python", "args": None}, {"command": "python", "timeout": None},
                     {"command": "python", "timeout": True}):
            with self.subTest(args=args), patch.object(tools.subprocess, "Popen", side_effect=AssertionError("must not launch")):
                self.assert_error(executor.execute("workspace_run", args))

    def test_attach_failure_and_missing_program_are_generic_errors(self):
        marker = self.root / "must-not-run"
        with patch.object(tools, "ProcessScope", side_effect=OSError("SECRET-ATTACH-ERROR")):
            result = self.run_executor().execute("workspace_run", {
                "command": "python", "args": [f"from pathlib import Path; Path({str(marker)!r}).touch()"],
            })
        self.assert_error(result)
        self.assertNotIn("SECRET-ATTACH", json.dumps(result))
        self.assertFalse(marker.exists())
        executor = self.make_executor("execute", commands={"missing": [str(self.base / "private-missing-program")]})
        result = executor.execute("workspace_run", {"command": "missing"})
        self.assert_error(result)
        self.assertEqual(result["outcome"], "launch_error")
        self.assertNotIn("private-missing-program", json.dumps(result))

    def test_timeout_kills_descendants_and_closes_capture(self):
        child = ("from pathlib import Path; import time; "
                 f"Path({str(self.root / 'ready')!r}).touch(); time.sleep(1.5); "
                 f"Path({str(self.root / 'leaked')!r}).touch()")
        parent = ("import subprocess,sys,time; "
                  f"subprocess.Popen([sys.executable,'-I','-c',{child!r}]); time.sleep(10)")
        result = self.run_executor(timeout=3).execute("workspace_run", {
            "command": "python", "args": [parent], "timeout": 0.8,
        })
        self.assert_error(result)
        self.assertEqual(result["outcome"], "timeout", result)
        self.assertTrue(result["timed_out"])
        self.assertTrue((self.root / "ready").exists(), "fixture child must have started")
        self.assertIsNone(result["exit_code"])
        self.assertTrue(result["complete"])
        time.sleep(1.6)
        self.assertFalse((self.root / "leaked").exists())


if __name__ == "__main__":
    unittest.main()
