"""Host opt-in task directories; temporary data only, no model or process calls."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import stat
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from cater.cli import build_parser, main
from cater.events import Actor
from cater.managed_workspaces import MANAGED_ID
from cater.runtime import Runtime
from cater.task_requests import prepare
from cater.work import WorkConflict
from cater.workspace_tools import WorkspaceExecutor
from cater.workspaces import load_workspaces
from cater.web import ActivityRunner
from test_runtime import start_test_web


class ManagedWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.root = self.base / "managed"
        self.root.mkdir()
        self.old_root = self.base / "old-project"
        self.old_root.mkdir()
        self.db = self.base / "events.db"
        self.config = self.base / "workspaces.json"
        self.template = {"root": str(self.root), "mode": "execute",
                         "commands": {"python": [sys.executable, "-B"]}}
        self.explicit = {"id": "old", "title": "Old project", "root": str(self.old_root),
                         "mode": "execute", "commands": {"old-check": ["unused-program"]}}
        self.write_config()
        for target in ("subprocess.Popen", "cater.team.TeamService.run", "cater.runtime.Runtime.collaborate"):
            call = self.enterContext(patch(target, side_effect=AssertionError("No external execution")))
            self.addCleanup(call.assert_not_called)

    def write_config(self, *, template=True, entries=None):
        body = {"workspaces": [self.explicit] if entries is None else entries}
        if template is not False:
            body["managed"] = self.template if template is True else template
        self.config.write_text(json.dumps(body), encoding="utf-8")

    def runtime(self, db=None):
        return Runtime(self.db if db is None else db, workspaces_path=self.config)

    def prepare(self, rt, request_id="managed-request-001", **fields):
        # Intake stays local; activation is only recorded, never dispatched.
        return prepare(rt, {"goal": "Build a local file", **fields}, request_id)

    def same_task(self, rt):
        task = "task_same"
        rt.log.emit(type="task.created", actor=Actor.human(), task_id=task,
                    payload={"goal": "Same task ID", "acceptance": []})
        return task

    def test_options_contract_does_not_create_directories(self):
        with self.runtime() as rt:
            option = next(row for row in rt.workspaces.options() if row["id"] == MANAGED_ID)
            self.assertEqual(option, {"id": MANAGED_ID, "title": "\u65b0\u5efa\u72ec\u7acb\u5de5\u4f5c\u76ee\u5f55",
                "root": str(self.root), "mode": "execute", "commands": ["python"], "managed": True})
            self.assertEqual(list(self.root.iterdir()), [])
            task, _, _ = self.prepare(rt)
            bound = rt.workspaces.view(task)
            self.assertTrue(bound["managed"])
            self.assertNotEqual(bound["workspace_id"], MANAGED_ID)
            self.assertEqual({row["id"] for row in rt.workspaces.options()}, {"old", MANAGED_ID})
            self.assertIn(bound["workspace_id"], rt.workspaces.config)
            self.assertIn(bound["workspace_id"], dict(rt.workspaces.config))

    def test_template_is_opt_in_and_requires_explicit_permissions(self):
        for setting in (False, None):
            with self.subTest(setting=setting):
                self.write_config(template=setting)
                with self.runtime() as rt:
                    task, _, _ = self.prepare(rt, request_id="disabled-" + str(setting))
                    self.assertFalse(rt.workspaces.view(task)["available"])
                    self.assertEqual([row["id"] for row in rt.workspaces.options()], ["old"])
        invalid = [{"root": str(self.root)}, {"mode": "edit"}, {}, False,
                   {**self.template, "root": "."}, {**self.template, "root": str(self.base / "missing")},
                   {**self.template, "mode": "edit"}, {**self.template, "extra": True}]
        for value in invalid:
            with self.subTest(value=value):
                self.write_config(template=value, entries=[])
                # False means omission to the fixture, so write this malformed value explicitly.
                if value is False:
                    self.config.write_text(json.dumps({"workspaces": [], "managed": False}), encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_workspaces(self.config)

    def test_reserved_ids_cannot_be_configured_as_static_projects(self):
        for identity in (MANAGED_ID, MANAGED_ID + "task_fake"):
            self.write_config(entries=[{**self.explicit, "id": identity}])
            with self.assertRaises(ValueError):
                load_workspaces(self.config)

    def test_default_binding_precedes_intake_and_activation(self):
        with self.runtime() as rt:
            with patch.object(rt, "intake") as intake:
                def inspect(task_id, **kwargs):
                    self.assertTrue(rt.workspaces.view(task_id)["available"])
                intake.side_effect = inspect
                task, created, intent = self.prepare(rt, run=True, background=True)
            self.assertTrue(created)
            bound = rt.workspaces.view(task)
            self.assertLess(bound["revision"], intent.seq)
            self.assertEqual(intent.payload["scope"]["workspace"]["revision"], bound["revision"])
            policy = rt.workspaces.config[bound["workspace_id"]]["policy"]
            self.assertEqual(set(policy.commands), {"python"})
            self.assertNotEqual(policy.root, self.old_root)
            self.assertEqual(policy.root.parent.parent, self.root)
            self.assertEqual(policy.root.name, task)

    def test_omitted_null_explicit_and_managed_requests_remain_distinct(self):
        with self.runtime() as rt:
            null_task, _, _ = self.prepare(rt, "null-request", workspace_id=None)
            old_task, _, _ = self.prepare(rt, "explicit-request", workspace_id="old")
            new_task, _, _ = self.prepare(rt, "managed-request", workspace_id=MANAGED_ID)
            self.assertIsNone(rt.workspaces.view(null_task)["workspace_id"])
            self.assertEqual(rt.workspaces.view(old_task)["root"], str(self.old_root))
            self.assertTrue(rt.workspaces.view(new_task)["managed"])
            self.assertEqual(len(list(self.root.rglob("task_*"))), 1)
            with self.assertRaises(WorkConflict):
                self.prepare(rt, "managed-request")
            with self.assertRaises(WorkConflict):
                self.prepare(rt, "null-request")

    def test_restart_and_lost_response_reuse_files_without_rebinding(self):
        with self.runtime() as rt:
            task, _, _ = self.prepare(rt)
            binding = rt.workspaces.view(task)
            workspace = WorkspaceExecutor(rt.workspaces.config[binding["workspace_id"]]["policy"])
            result = workspace.execute("workspace_write", {"path": "result.txt", "content": "persistent result",
                                                          "expected_sha256": None})
            self.assertEqual(result["status"], "ok")
            head = rt.log.head_hash()
        with self.runtime() as rt:
            # Direct executor lookup works before anyone calls view().
            workspace = WorkspaceExecutor(rt.workspaces.config[binding["workspace_id"]]["policy"])
            self.assertEqual(workspace.execute("workspace_read", {"path": "result.txt"})["content"], "persistent result")
            self.assertEqual(rt.workspaces.view(task), binding)
            replay, created, _ = self.prepare(rt)
            self.assertEqual(replay, task)
            self.assertFalse(created)
            self.assertEqual(rt.log.head_hash(), head)

    def test_database_and_task_namespaces_are_separate(self):
        roots = []
        for db in (self.db, self.base / "other.db"):
            with self.runtime(db) as rt:
                task = self.same_task(rt)
                roots.append(Path(rt.workspaces.bind(task, MANAGED_ID, expected_revision=None)["root"]))
                other = rt.create_task("Another task")
                other_root = Path(rt.workspaces.bind(other, MANAGED_ID, expected_revision=None)["root"])
                self.assertEqual(other_root.parent, roots[-1].parent)
                self.assertNotEqual(other_root, roots[-1])
        self.assertNotEqual(roots[0], roots[1])
        with self.runtime(self.db.parent / "." / self.db.name) as rt:
            self.assertEqual(Path(rt.workspaces.view("task_same")["root"]), roots[0])
            self.assertTrue(rt.workspaces.view("task_same")["available"])

    def test_independent_memory_databases_do_not_share_directories(self):
        with self.runtime(":memory:") as first, self.runtime(":memory:") as second:
            task1, task2 = self.same_task(first), self.same_task(second)
            a = first.workspaces.bind(task1, MANAGED_ID, expected_revision=None)
            b = second.workspaces.bind(task2, MANAGED_ID, expected_revision=None)
            self.assertNotEqual(a["root"], b["root"])

    def test_copied_database_does_not_gain_access_to_original_directory(self):
        with self.runtime() as rt:
            task, _, _ = self.prepare(rt)
            binding = rt.workspaces.view(task)
            with self.runtime(self.base / "copy.db") as clone:
                rt.log.db.backup(clone.log.db)
                self.assertFalse(clone.workspaces.view(task)["available"])
                self.assertIsNone(clone.workspaces.config.get(binding["workspace_id"]))
                rebound = clone.workspaces.bind(task, MANAGED_ID, expected_revision=binding["revision"])
                self.assertTrue(rebound["available"])
                self.assertNotEqual(rebound["root"], binding["root"])

    def test_concurrent_requests_are_idempotent_and_isolated(self):
        with self.runtime():
            pass
        original_config = self.config.read_bytes()
        for shared in (True, False):
            barrier = threading.Barrier(4)
            def create(index):
                with self.runtime() as rt:
                    barrier.wait(timeout=10)
                    identity = "concurrent-shared" if shared else "concurrent-task-" + str(index)
                    task, created, _ = self.prepare(rt, identity)
                    return task, created, rt.workspaces.view(task)["root"]
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(create, range(4)))
            expected = 1 if shared else 4
            self.assertEqual(sum(row[1] for row in results), expected)
            self.assertEqual(len({row[0] for row in results}), expected)
            self.assertEqual(len({row[2] for row in results}), expected)
        self.assertEqual(self.config.read_bytes(), original_config)
        with self.runtime() as rt:
            self.assertTrue(rt.verify_chain().ok)

    def test_template_changes_revoke_all_lookups_without_a_view_refresh(self):
        moved = self.base / "different-base"
        moved.mkdir()
        changes = [{"root": str(self.root), "mode": "read_only"},
                   {**self.template, "commands": {"other": [sys.executable]}},
                   {**self.template, "commands": {}}, {**self.template, "timeout": 10},
                   {**self.template, "env_allowlist": ["LANG"]},
                   {**self.template, "root": str(moved)}, None, False]
        with self.runtime() as rt:
            task, _, _ = self.prepare(rt)
            binding = rt.workspaces.view(task)
            head = rt.log.head_hash()
            for change in changes:
                with self.subTest(change=change):
                    self.write_config(template=change)
                    self.assertIsNone(rt.workspaces.config.get(binding["workspace_id"]))
                    with self.assertRaises(KeyError):
                        rt.workspaces.config[binding["workspace_id"]]
                    self.assertFalse(rt.workspaces.view(task)["available"])
                    self.assertIsNone(rt.workspaces.fingerprint(task))
            self.config.write_text("invalid-json", encoding="utf-8")
            self.assertIsNone(rt.workspaces.config.get(binding["workspace_id"]))
            self.assertFalse(rt.workspaces.view(task)["available"])
            self.assertEqual(rt.log.head_hash(), head)
            self.assertTrue(Path(binding["root"]).is_dir())

    def test_explicit_rebind_accepts_changed_policy_in_same_directory(self):
        with self.runtime() as rt:
            task, _, _ = self.prepare(rt)
            old = rt.workspaces.view(task)
            self.write_config(template={"root": str(self.root), "mode": "read_only"})
            self.assertFalse(rt.workspaces.view(task)["available"])
            # Replayed creation must not silently authorize the new template.
            self.prepare(rt)
            self.assertEqual(rt.workspaces.view(task)["revision"], old["revision"])
            rebound = rt.workspaces.bind(task, old["workspace_id"], expected_revision=old["revision"])
            self.assertTrue(rebound["available"])
            self.assertEqual(rebound["root"], old["root"])
            names = {t["name"] for t in WorkspaceExecutor(rt.workspaces.config[rebound["workspace_id"]]["policy"]).schemas()}
            self.assertEqual(names, {"workspace_read", "workspace_list", "workspace_search"})

    def test_one_task_cannot_bind_another_tasks_managed_identity(self):
        with self.runtime() as rt:
            task, _, _ = self.prepare(rt)
            binding = rt.workspaces.view(task)
            other = rt.create_task("Unbound task")
            with self.assertRaises(ValueError):
                rt.workspaces.bind(other, binding["workspace_id"], expected_revision=None)
            self.assertFalse(rt.workspaces.view(other)["available"])

    def test_unbind_does_not_delete_or_reacquire_directory_on_replay(self):
        with self.runtime() as rt:
            task, _, _ = self.prepare(rt)
            binding = rt.workspaces.view(task)
            unbound = rt.workspaces.bind(task, None, expected_revision=binding["revision"])
            head = rt.log.head_hash()
            self.assertIsNone(rt.workspaces.config.get(binding["workspace_id"]))
            self.prepare(rt)
            self.assertIsNone(rt.workspaces.view(task)["workspace_id"])
            self.assertTrue(Path(binding["root"]).is_dir())
            self.assertEqual(rt.log.head_hash(), head)
            rebound = rt.workspaces.bind(task, MANAGED_ID, expected_revision=unbound["revision"])
            self.assertEqual(rebound["root"], binding["root"])
            self.assertTrue(rebound["available"])

    def test_disabled_template_keeps_options_and_indexes_readable(self):
        with self.runtime() as rt:
            task, _, _ = self.prepare(rt)
            bound = rt.workspaces.view(task)
            self.write_config(template=None)
            self.assertEqual({row["id"] for row in rt.workspaces.options()}, {"old"})
            self.assertEqual(set(rt.workspaces.config), {"old"})
            self.assertEqual(set(dict(rt.workspaces.config)), {"old"})
            self.assertEqual(len(list(rt.workspaces.config.values())), 1)
            self.assertIsNone(rt.team.read_sources._scope(task))
            self.assertIsNone(rt.team.read_sources.index(task))
            self.assertIsNone(rt.workspace_checks._config(task))
            self.assertNotIn("workspace_run", rt.team._skill_scope(task, rt.organization.leader)["tools"])
            with self.assertRaises(ValueError):
                rt.workspaces.bind(task, MANAGED_ID, expected_revision=bound["revision"])
            # Restoring the exact host policy follows existing fingerprint semantics.
            self.write_config()
            self.assertEqual(rt.workspaces.view(task), bound)

    def test_lookup_scans_only_its_task_and_options_do_not_scan_bindings(self):
        with self.runtime() as rt:
            task, _, _ = self.prepare(rt)
            binding = rt.workspaces.view(task)
            with patch.object(rt.log, "iter_events", wraps=rt.log.iter_events) as read:
                rt.workspaces.options()
                read.assert_not_called()
                self.assertIsNotNone(rt.workspaces.config.get(binding["workspace_id"]))
                self.assertEqual(read.call_count, 1)
                self.assertEqual(read.call_args.kwargs["task_id"], task)
                self.assertEqual(read.call_args.kwargs["event_types"], ("workspace.bound",))

    def test_missing_directory_is_not_recreated_by_reads(self):
        with self.runtime() as rt:
            task, _, _ = self.prepare(rt)
            binding = rt.workspaces.view(task)
            Path(binding["root"]).rmdir()
            self.assertIsNone(rt.workspaces.config.get(binding["workspace_id"]))
            self.assertFalse(rt.workspaces.view(task)["available"])
            rt.workspaces.options()
            self.assertFalse(Path(binding["root"]).exists())

    def test_invalid_configuration_rolls_back_default_creation(self):
        self.write_config(template={"root": str(self.root)})
        with self.runtime() as rt:
            with self.assertRaises(ValueError):
                self.prepare(rt)
            self.assertEqual(rt.tasks(), [])
            self.assertEqual(list(self.root.iterdir()), [])

    def test_failed_creation_rolls_back_binding_and_leaves_no_authorized_row(self):
        with self.runtime() as rt:
            with patch.object(rt, "intake", side_effect=ValueError("intake failed")):
                with self.assertRaisesRegex(ValueError, "intake failed"):
                    self.prepare(rt)
            self.assertEqual(rt.tasks(), [])
            self.assertEqual(set(rt.workspaces.config), {"old"})
            # Empty filesystem residue is harmless and never grants a binding.
            task, created, _ = self.prepare(rt)
            self.assertTrue(created)
            self.assertTrue(rt.workspaces.view(task)["available"])

    def test_stale_binding_revision_does_not_allocate(self):
        with self.runtime() as rt:
            task = rt.create_task("Unbound task")
            with self.assertRaises(WorkConflict):
                rt.workspaces.bind(task, MANAGED_ID, expected_revision=123)
            self.assertEqual(list(self.root.iterdir()), [])

    def test_reparse_or_symlink_namespace_is_rejected_before_task_creation(self):
        for attributes, mode in ((0x400, stat.S_IFDIR | 0o700), (0, stat.S_IFLNK | 0o700)):
            with self.subTest(attributes=attributes):
                with self.runtime() as rt:
                    task = rt.create_task("Reject linked namespace")
                    namespace = self.root / rt.workspaces._namespace
                    namespace.mkdir(exist_ok=True)
                    original = Path.lstat
                    def lstat(path, *args, **kwargs):
                        if path == namespace:
                            return SimpleNamespace(st_mode=mode, st_file_attributes=attributes, st_nlink=1)
                        return original(path, *args, **kwargs)
                    with patch.object(Path, "lstat", lstat):
                        with self.assertRaises(ValueError):
                            rt.workspaces.bind(task, MANAGED_ID, expected_revision=None)
                    self.assertFalse((namespace / task).exists())
                    self.assertIsNone(rt.workspaces.view(task)["revision"])

    def test_team_tool_schema_and_scope_use_managed_policy_without_model_calls(self):
        with self.runtime() as rt:
            task, _, _ = self.prepare(rt)
            with patch.object(rt.team, "available", return_value=True):
                rt.team.enable(task)
            rt.team.receive(task, "Inspect workspace", request_id="schema-input")
            message = list(rt.log.iter_events(task_id=task, event_types=("team.message_received",)))[-1]
            context, workspace, binding, tools, _ = rt.team._model_input(
                task, message, rt.organization.leader, run_id="schema-test")
            self.assertEqual(str(workspace.policy.root), binding["root"])
            self.assertEqual(context["workspace"]["workspace_id"], binding["workspace_id"])
            self.assertTrue({"workspace_read", "workspace_write", "workspace_run", "publish_workspace_file"}
                            <= {tool["name"] for tool in tools})
            self.assertEqual(rt.work.scope(task)["workspace"]["configured"], binding["fingerprint"])
            self.assertEqual(rt.team.read_sources._scope(task)["workspace_id"], binding["workspace_id"])

    def test_cli_default_explicit_none_and_explicit_auto(self):
        # Patch key resolution so these CLI tests never inspect host credentials.
        with patch("cater.cli.resolve_audit_key", return_value=None):
            for flags, managed, explicit in (([], True, False), (["--no-workspace"], False, False),
                    (["--workspace", "old"], False, True), (["--workspace", MANAGED_ID], True, False)):
                with self.subTest(flags=flags):
                    output = io.StringIO()
                    with redirect_stdout(output):
                        result = main(["--db", str(self.db), "--workspaces", str(self.config),
                                       "new", "CLI workspace", *flags])
                    self.assertEqual(result, 0, output.getvalue())
                    task = output.getvalue().strip().split()[-1]
                    with self.runtime() as rt:
                        binding = rt.workspaces.view(task)
                        self.assertEqual(binding.get("managed", False), managed)
                        self.assertEqual(binding["available"], managed or explicit)
                        if explicit:
                            self.assertEqual(binding["workspace_id"], "old")
        with redirect_stdout(io.StringIO()), patch("sys.stderr", new_callable=io.StringIO):
            with self.assertRaises(SystemExit):
                build_parser().parse_args(["new", "Task", "--workspace", "old", "--no-workspace"])

    def test_http_creation_contract_and_replay_without_execution(self):
        url = start_test_web(self, self.db, workspaces_path=self.config)
        with urlopen(url + "/api/workspaces", timeout=5) as response:
            options = json.load(response)
        self.assertTrue(next(row for row in options if row["id"] == MANAGED_ID)["managed"])
        def post(body):
            request = Request(url + "/api/tasks", data=json.dumps(body).encode(),
                              headers={"Content-Type": "application/json"})
            with urlopen(request, timeout=5) as response:
                return json.load(response)
        with patch.object(ActivityRunner, "_start") as start:
            body = {"goal": "HTTP managed creation", "request_id": "http-managed-create", "run": True, "background": True}
            first = post(body)
            self.assertTrue(first["workspace"]["managed"])
            self.assertTrue(first["workspace"]["available"])
            replay = post(body)
            self.assertEqual(replay["task"]["id"], first["task"]["id"])
            self.assertEqual(replay["workspace"], first["workspace"])
            start.assert_called_once()
            plain = post({**body, "request_id": "http-managed-none", "workspace_id": None})
            self.assertIsNone(plain["workspace"]["workspace_id"])
            explicit = post({**body, "request_id": "http-managed-explicit", "workspace_id": MANAGED_ID})
            self.assertTrue(explicit["workspace"]["managed"])
            self.assertNotEqual(explicit["workspace"]["root"], first["workspace"]["root"])

    def test_http_bind_existing_unbound_task_and_preserve_revision_check(self):
        with self.runtime() as rt:
            task = rt.create_task("Existing unbound task")
        url = start_test_web(self, self.db, workspaces_path=self.config)
        def bind(revision):
            request = Request(url + "/api/tasks/" + task + "/workspace",
                data=json.dumps({"workspace_id": MANAGED_ID, "expected_revision": revision}).encode(),
                headers={"Content-Type": "application/json"})
            with urlopen(request, timeout=5) as response:
                return json.load(response)["workspace"]
        first = bind(None)
        self.assertTrue(first["available"])
        self.assertTrue(first["managed"])
        with self.assertRaises(HTTPError) as error:
            bind(None)
        self.assertEqual(error.exception.code, 409)
        rebound = bind(first["revision"])
        self.assertEqual(rebound["root"], first["root"])
        self.assertEqual(rebound["workspace_id"], first["workspace_id"])


if __name__ == "__main__":
    unittest.main()
