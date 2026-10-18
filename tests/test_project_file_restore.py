"""Real isolated restore chains with a scripted driver, never a real model.

Faults model scope changes and lost receipts, not an actual OS crash. The host
reader test runs only after the inherited no-network/no-process fixture closes.
It interprets fixture data, never restored source code or production content.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import test_project_history_archive as archive_fixtures
from cater import project_restore
from cater.events import Actor
from cater.runtime import Runtime
from cater.team import HOST
from cater.workspace_tools import WorkspaceExecutor
from cater.workspaces import WorkspaceService


TOOL = "workspace_restore_project_file"


class ProjectFileRestoreTests(unittest.TestCase):
    def setUp(self):
        # Module import prevents unittest discovering the reused TestCase twice.
        self.archive = archive_fixtures.ProjectHistoryArchiveTests()
        self.addCleanup(self.archive.doCleanups)
        self.archive.setUp()
        self.f = self.archive.f
        self.target_root = getattr(self, "host_target_root", self.f.root / "restore-target")
        self.target_root.mkdir()
        self.execute_root = self.f.root / "execute-target"
        self.execute_root.mkdir()
        self.registry = {"workspaces": [
            {"id": "snapshot", "title": "Read-only source", "root": str(self.archive.workspace), "mode": "read_only"},
            {"id": "target", "title": "Restore target", "root": str(self.target_root), "mode": "edit"},
            {"id": "execute", "title": "Execute target", "root": str(self.execute_root), "mode": "execute"},
        ]}
        self.save_registry()
        self.f.rt.workspaces = WorkspaceService(self.f.rt, self.archive.config)
        self.bind(self.f.target, "target")

    def save_registry(self):
        self.archive.config.write_text(json.dumps(self.registry), encoding="utf-8")

    def bind(self, task, workspace):
        previous = self.f.rt.workspaces.view(task)["revision"]
        return self.f.rt.workspaces.bind(task, workspace, expected_revision=previous)

    def selection(self, delivery, artifact, path=archive_fixtures.UNICODE_PATH, **changes):
        return {"as_of_seq": self.f.history.index(self.f.target)["as_of_seq"],
                "delivery_seq": delivery.seq, "artifact_seq": artifact.seq, "path": path, **changes}

    def restore(self, arguments, task=None):
        result, = self.f.exchange(task or self.f.target, (TOOL, arguments))
        return result

    def pair(self, task=None):
        task = task or self.f.target
        start = self.f.events(task, "team.tool_started")[-1]
        finish = self.f.events(task, "team.tool_finished")[-1]
        self.assertEqual(start.payload["name"], TOOL)
        self.assertEqual(finish.payload["started_seq"], start.seq)
        return start, finish

    def disk(self, root=None):
        root = root or self.target_root
        return {p.relative_to(root).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
                for p in root.rglob("*") if p.is_file()}

    def rejected(self, result):
        self.assertEqual(result["status"], "error", result)
        self.assertNotIn("source", result)
        self.assertNotIn("content", result)
        encoded = json.dumps(result, ensure_ascii=True)
        for secret in (str(self.f.root), "PRIVATE_EXCEPTION", "Traceback", archive_fixtures.TEXT):
            self.assertNotIn(json.dumps(secret, ensure_ascii=True)[1:-1], encoded)

    def not_started(self, result):
        self.rejected(result)
        self.assertIs(result["complete"], True)
        self.assertEqual(result["outcome"], "not_started")

    def assert_restored(self, result, arguments, body):
        self.assertEqual((result["status"], result["complete"]), ("ok", True), result)
        destination = arguments.get("destination", arguments["path"])
        actual = (self.target_root / destination).read_bytes()
        sha = hashlib.sha256(body).hexdigest()
        self.assertEqual(actual, body)
        self.assertEqual(result["member"]["sha256"], sha)
        self.assertEqual(result["member"]["path"], arguments["path"])
        self.assertEqual(result["member"]["bytes"], len(body))
        receipt = result["restored_file"]
        self.assertEqual((receipt["path"], receipt["sha256"], receipt["bytes"]), (destination, sha, len(body)))
        self.assertIs(type(receipt["executable"]), bool)
        self.assertEqual(receipt["executable_requested"], result["member"]["executable"])
        read = self.f.history.read(self.f.target, {"section": "artifact", **{
            key: arguments[key] for key in ("as_of_seq", "delivery_seq", "artifact_seq")}})
        self.assertEqual(result["source"], read["source"])
        self.assertEqual((result["as_of_seq"], result["as_of_hash"]), (read["as_of_seq"], read["as_of_hash"]))
        self.assertNotIn("body", result["member"])
        self.assertNotIn("content", receipt)
        self.assertNotIn("base64", json.dumps(result))
        return sha

    def test_text_empty_binary_restore_publish_and_finish_are_real(self):
        delivery, artifact = self.archive.delivered()
        source_disk = self.disk(self.archive.workspace)
        for name, body in archive_fixtures.FILES.items():
            with self.subTest(path=name):
                args = self.selection(delivery, artifact, name)
                result = self.restore(args)
                self.assertEqual(result["outcome"], "written")
                self.assert_restored(result, args, body)
                self.assertTrue(result["restored_file"]["created"])
                start, finish = self.pair()
                self.assertEqual(start.payload["project_scope"], self.f.project)
                self.assertEqual(start.payload["root"], str(self.target_root))
                self.assertEqual(start.payload["arguments"], args)
                self.assertEqual(finish.payload["result"], result)
        self.assertEqual(self.disk(self.archive.workspace), source_disk)
        self.assertEqual(self.f.rt.task(self.f.target).deliverables, [])
        self.assertEqual(self.f.events(self.f.target, "team.objective_completed"), [])
        published, = self.f.exchange(self.f.target, ("publish_workspace_archive", {
            "filename": "restored.zip", "summary": "Restored fixture bytes only", "paths": list(archive_fixtures.FILES)}))
        self.assertEqual(published["status"], "ok", published)
        current = next(e for e in self.f.events(self.f.target, "deliverable.published") if e.seq == published["artifact_seq"])
        for row in current.payload["archive"]["files"]:
            self.assertEqual(row["sha256"], hashlib.sha256(archive_fixtures.FILES[row["path"]]).hexdigest())
        completed = self.f.finish(self.f.target, artifacts=[current], verification="Fixture bytes checked; no source program executed")
        self.assertNotEqual(completed.seq, delivery.seq)
        self.assertEqual(self.f.rt.workspaces.pending(task_id=self.f.target), [])
        self.assertTrue(self.f.rt.verify_chain().ok)
        for role in [self.f.rt.organization.leader, *self.f.rt.worker_roles]:
            self.assertEqual(role.driver, "team_test")
            self.assertIs(self.f.rt.drivers[role.driver], self.f.driver)

    def test_reused_fixture_keeps_network_process_and_environment_guards(self):
        self.assertEqual(dict(os.environ), {})
        with self.assertRaisesRegex(AssertionError, "network access"):
            socket.socket()
        with self.assertRaisesRegex(AssertionError, "external process"):
            subprocess.Popen([sys.executable, "-c", "raise AssertionError('must never launch')"])
        self.assertEqual(self.f.driver.calls, [])

    def test_same_retains_mtime_and_conflict_never_overwrites(self):
        delivery, artifact = self.archive.delivered()
        for name, body in archive_fixtures.FILES.items():
            with self.subTest(path=name):
                args = self.selection(delivery, artifact, name)
                self.assertEqual(self.restore(args)["status"], "ok")
                path = self.target_root / name
                os.utime(path, ns=(1_600_000_000_000_000_000,) * 2)
                before = (path.stat().st_mtime_ns, path.stat().st_mode, path.stat().st_ino)
                result = self.restore(args)
                self.assert_restored(result, args, body)
                self.assertEqual(result["outcome"], "already_present")
                self.assertFalse(result["restored_file"]["created"])
                self.assertEqual((path.stat().st_mtime_ns, path.stat().st_mode, path.stat().st_ino), before)
                path.write_bytes(b"Current target content")
                before_disk = self.disk()
                self.not_started(self.restore(args))
                self.assertEqual(self.disk(), before_disk)

    def test_destination_and_execute_root_are_independent(self):
        delivery, artifact = self.archive.delivered()
        self.bind(self.f.target, "execute")
        args = self.selection(delivery, artifact, "binary.bin", destination="assets/restored.bin")
        result = self.restore(args)
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual((self.execute_root / "assets/restored.bin").read_bytes(), archive_fixtures.FILES["binary.bin"])
        self.assertEqual(self.disk(), {})
        self.assertFalse((self.execute_root / "binary.bin").exists())

    def test_registry_checks_fingerprint_does_not_reject_valid_executor_policy(self):
        delivery, artifact = self.archive.delivered()
        row = self.registry["workspaces"][1]
        row.update(mode="execute", commands={"fixture-python": [sys.executable, "-I", "-S", "-c",
            "raise AssertionError('registered check must not execute during restore')"]})
        for label, checks in (("empty", []), ("configured", [{"id": "fixture-check", "title": "Fixture only",
                "command": "fixture-python", "args": [], "cwd": "."}])):
            with self.subTest(checks=label):
                row["checks"] = checks
                self.save_registry()
                binding = self.bind(self.f.target, "target")
                configured = self.f.rt.workspaces.config["target"]
                self.assertEqual(binding["fingerprint"], configured["fingerprint"])
                self.assertNotEqual(binding["fingerprint"], configured["policy"].fingerprint)
                args = self.selection(delivery, artifact, "binary.bin", destination=f"checks-{label}.bin")
                result = self.restore(args)
                self.assert_restored(result, args, archive_fixtures.FILES["binary.bin"])
                self.assertEqual(result["outcome"], "written")
        self.assertFalse(any(e.payload["name"] == "workspace_run"
                             for e in self.f.events(self.f.target, "team.tool_started")))

    def test_fixed_asof_survives_later_source_update_and_runtime_restart(self):
        delivery, artifact = self.archive.delivered()
        args = self.selection(delivery, artifact)
        (self.archive.workspace / archive_fixtures.UNICODE_PATH).write_bytes(b"Later live source")
        self.f.update(delivery.task_id, "Later source version")
        newer = self.archive.publish(delivery.task_id)
        latest = self.f.finish(delivery.task_id, artifacts=[newer])
        for name in archive_fixtures.FILES:
            (self.archive.workspace / name).unlink()
        self.f.rt.close()
        self.f.rt = Runtime(self.f.database, roles_path=self.f.roles, workspaces_path=self.archive.config,
                            drivers={"team_test": self.f.driver})
        self.assert_restored(self.restore(args), args, archive_fixtures.FILES[args["path"]])
        before = self.disk()
        self.not_started(self.restore({**args, "delivery_seq": latest.seq, "artifact_seq": newer.seq,
                                      "destination": "wrong-version.txt"}))
        self.assertEqual(self.disk(), before)
        self.assertTrue(self.f.rt.verify_chain().ok)

    def test_invalid_paths_selectors_and_extra_bytes_never_write(self):
        delivery, artifact = self.archive.delivered()
        args = self.selection(delivery, artifact)
        cases = [{key: value} for key in ("path", "destination") for value in
                 (None, True, "", "../escape", "/absolute", "C:/host", "a\\b", ".env", "a//b", "CON", "x\0y", "\ud800", "x" * 2049)]
        cases += [{key: value} for key in ("as_of_seq", "delivery_seq", "artifact_seq")
                  for value in (None, True, -1, "1", 2 ** 63)]
        cases += [{"path": "missing.txt"}, {"delivery_seq": artifact.seq}, {"artifact_seq": delivery.seq},
                  {"as_of_seq": 0}, {"content": "PRIVATE_EXCEPTION"}, {"body": "AA=="}, {"task_id": delivery.task_id}]
        for changes in cases:
            with self.subTest(changes=repr(changes)):
                self.not_started(self.restore({**args, **changes}))
                self.assertEqual(self.disk(), {})
        for key in args:
            with self.subTest(missing=key):
                self.not_started(self.restore({k: v for k, v in args.items() if k != key}))

    def test_cross_project_private_current_and_unselected_versions_reject(self):
        delivery, artifact = self.archive.delivered()
        other = self.f.rt.rulebook.create_project("Other fixture project")["id"]
        for task in (self.f.task(other), self.f.task(self.f.project, isolated=True), self.f.target):
            with self.subTest(source=task):
                self.bind(task, "snapshot")
                d, a = self.archive.delivered(task)
                self.bind(self.f.target, "target")
                if task == self.f.target:
                    self.f.update(task, "New goal must not restore its own delivery as project history")
                self.not_started(self.restore(self.selection(d, a)))
        source = self.f.task(self.f.project)
        chosen = self.archive.publish(source)
        excluded = self.archive.publish(source)
        selected_delivery = self.f.finish(source, artifacts=[chosen])
        for d, a in ((selected_delivery, excluded), (selected_delivery, artifact), (delivery, chosen)):
            with self.subTest(delivery=d.seq, artifact=a.seq):
                self.not_started(self.restore(self.selection(d, a)))
        plain_delivery, plain = self.f.deliver(self.f.task(self.f.project))
        self.not_started(self.restore(self.selection(plain_delivery, plain, "result.txt")))
        self.assertEqual(self.disk(), {})

    def test_bad_blob_hash_manifest_member_crc_are_safe_native_errors(self):
        def member_hash(body, manifest):
            next(row for row in manifest["files"] if row["path"] == archive_fixtures.UNICODE_PATH)["sha256"] = "f" * 64
            return body, manifest

        def crc(body, manifest):
            with zipfile.ZipFile(io.BytesIO(body)) as archive:
                member = archive.getinfo(archive_fixtures.UNICODE_PATH)
                position = member.header_offset + 30 + len(member.filename.encode()) + len(member.extra)
            damaged = bytearray(body)
            damaged[position] ^= 1
            return bytes(damaged), manifest

        cases = [{"store": False}, {"changes": {"content_sha256": "f" * 64}},
                 {"changes": {"content_bytes": 1}}, {"changes": {"archive": {}}},
                 {"transform": member_hash}, {"transform": crc},
                 {"transform": lambda body, manifest: (b"PRIVATE_EXCEPTION invalid ZIP", manifest)}]
        for case in cases:
            with self.subTest(case=case):
                delivery, artifact = self.archive.adversarial(**case)
                self.not_started(self.restore(self.selection(delivery, artifact)))
                self.assertEqual(self.disk(), {})

    def native_run(self, task, steps):
        """Like the reused exchange, but retain failed/interrupted/hidden-schema runs."""
        self.f.number += 1
        role = self.f.rt.team._role(self.f.manager)
        message = self.f.rt.team.receive(task, f"@{role.name} restore boundary {self.f.number}",
                                         request_id=f"restore-boundary-{self.f.number}")
        run_id = f"restore-boundary-run-{self.f.number}"
        started = self.f.rt.log.emit(type="team.run_started", actor=HOST, task_id=task, payload={
            "message_seq": message.seq, "role": role.id, "run_id": run_id,
            "revision": self.f.rt.team.view(task)["revision"],
            "control_seq": self.f.rt.execution_control.view(task)["seq"]})
        self.f.driver.steps[role.id] = steps
        list(self.f.rt.team._run_message(task, message, role, started))
        return next(e for e in self.f.events(task, "team.run_finished") if e.payload["run_id"] == run_id)

    def test_schema_hidden_for_read_only_unbound_paused_and_nonproject_targets(self):
        delivery, artifact = self.archive.delivered()
        args = self.selection(delivery, artifact)
        for kind in ("read_only", "unbound", "paused", "private"):
            with self.subTest(kind=kind):
                target = self.f.task(self.f.project if kind != "private" else None)
                if kind != "unbound":
                    self.bind(target, "snapshot" if kind == "read_only" else "target")
                if kind == "paused":
                    self.f.rt.execution_control.change(target, "pause", request_id="restore-pause-fixture")
                calls = len(self.f.driver.calls)
                run = self.native_run(target, [archive_fixtures.fixtures.turn("", (TOOL, args))])
                self.assertEqual(run.payload["status"], "failed", run.payload)
                self.assertEqual(self.f.events(target, "team.tool_started"), [])
                self.assertNotIn(TOOL, {t["name"] for t in self.f.driver.calls[calls]["tools"]})
                self.assertEqual(self.disk(), {})

    def test_source_without_supported_native_start_is_not_restorable(self):
        source = self.f.rt.create_task("Unsupported fixture native version", project_id=self.f.project)
        self.f.rt.log.emit(type="team.started", task_id=source, actor=HOST, payload={"version": 2})
        delivery, artifact = self.archive.delivered(source)
        self.not_started(self.restore(self.selection(delivery, artifact)))
        self.assertEqual(self.disk(), {})

    def test_scope_changes_after_member_read_are_rechecked_before_install(self):
        delivery, artifact = self.archive.delivered()
        original = project_restore.read_members
        for change in ("binding", "permission", "project", "source_project", "pause", "customer_input"):
            with self.subTest(change=change):
                target = self.f.task(self.f.project)
                self.bind(target, "target")
                args = self.selection(delivery, artifact)

                def mutate(*a, **kw):
                    members = original(*a, **kw)
                    if change == "binding":
                        binding = self.f.rt.workspaces.view(target)
                        self.f.rt.log.emit(type="workspace.bound", actor=Actor.human(), task_id=target,
                            payload={**{k: binding[k] for k in ("workspace_id", "title", "root", "mode", "fingerprint")},
                                     "previous_revision": binding["revision"]})
                    elif change == "permission":
                        self.registry["workspaces"][1]["mode"] = "read_only"
                        self.save_registry()
                    elif change in {"project", "source_project"}:
                        other = self.f.rt.rulebook.create_project("Changed scope")["id"]
                        self.archive.rebind(target if change == "project" else delivery.task_id, other)
                    elif change == "pause":
                        self.f.rt.execution_control.change(target, "pause", request_id="restore-race-pause")
                    else:
                        role = self.f.rt.team._role(self.f.manager)
                        self.f.rt.team.receive(target, f"@{role.name} changed request", request_id="restore-new-input")
                    return members

                try:
                    calls = len(self.f.driver.calls)
                    with patch.object(project_restore, "read_members", side_effect=mutate) as read:
                        run = self.native_run(target, [archive_fixtures.fixtures.turn("", (TOOL, args)),
                                                       archive_fixtures.fixtures.turn()])
                    result = self.pair(target)[1].payload["result"]
                    interrupted = change in {"binding", "permission", "pause", "customer_input"}
                    self.assertEqual(run.payload["status"], "interrupted" if interrupted else "completed")
                    self.assertEqual(len(self.f.driver.calls), calls + (1 if interrupted else 2))
                    read.assert_called_once()
                    self.not_started(result)
                    self.assertEqual(self.disk(), {})
                finally:
                    self.registry["workspaces"][1]["mode"] = "edit"
                    self.save_registry()
                    self.archive.rebind(delivery.task_id, self.f.project)

    def test_final_primitive_guard_rechecks_target_goal_and_control(self):
        delivery, artifact = self.archive.delivered()
        original = WorkspaceExecutor.install_bytes
        for change in ("objective", "pause"):
            with self.subTest(change=change):
                target = self.f.task(self.f.project)
                self.bind(target, "target")
                args = self.selection(delivery, artifact)
                observed = []

                def install(executor, path, body, *, executable=False, guard=None):
                    def final_guard():
                        observed.append(True)
                        if change == "objective":
                            self.f.rt.log.emit(type="team.objective_updated", task_id=target,
                                actor=HOST, payload={"goal": "Changed at final publication guard"})
                        else:
                            self.f.rt.execution_control.change(target, "pause", request_id="final-guard-pause")
                        guard()
                    return original(executor, path, body, executable=executable, guard=final_guard)

                with patch.object(WorkspaceExecutor, "install_bytes", new=install):
                    run = self.native_run(target, [archive_fixtures.fixtures.turn("", (TOOL, args)),
                                                   archive_fixtures.fixtures.turn()])
                result = self.pair(target)[1].payload["result"]
                self.assertEqual(run.payload["status"], "interrupted" if change == "pause" else "completed")
                self.assertEqual(observed, [True])
                self.not_started(result)
                self.assertEqual(self.disk(), {})

    def test_busy_owner_refuses_before_member_access(self):
        delivery, artifact = self.archive.delivered()
        with self.f.rt.workspaces._owner(str(self.target_root)) as owned:
            self.assertTrue(owned)
            with patch.object(project_restore, "read_members", side_effect=AssertionError("must not read")) as read:
                result = self.restore(self.selection(delivery, artifact))
            read.assert_not_called()
        self.not_started(result)
        self.assertEqual(result["code"], "workspace_pending")
        self.assertEqual(self.disk(), {})

    def unknown_after_install(self, args):
        original = WorkspaceExecutor.install_bytes

        def lose_receipt(executor, *a, **kw):
            original(executor, *a, **kw)
            raise OSError("PRIVATE_EXCEPTION after file publication")

        with patch.object(WorkspaceExecutor, "install_bytes", new=lose_receipt):
            result = self.restore(args)
        self.rejected(result)
        self.assertEqual((result["complete"], result["outcome"]), (False, "effects_unknown"))
        return result

    def test_unknown_blocks_shared_root_restore_and_finish_until_human_reconcile(self):
        delivery, artifact = self.archive.delivered()
        args = self.selection(delivery, artifact)
        self.unknown_after_install(args)
        start, _ = self.pair()
        self.assertEqual((self.target_root / args["path"]).read_bytes(), archive_fixtures.FILES[args["path"]])
        self.assertEqual([e.seq for e in self.f.rt.workspaces.pending(task_id=self.f.target)], [start.seq])
        shared = self.f.task(self.f.project)
        self.bind(shared, "target")
        with patch.object(WorkspaceExecutor, "install_bytes", side_effect=AssertionError("must not replay")):
            self.not_started(self.restore({**args, "destination": "blocked.txt"}, shared))
            blocked, = self.f.exchange(self.f.target, ("finish_work", {"summary": "Must block", "verification": "Unknown restore"}))
        self.assertEqual(blocked["code"], "operation_pending")
        self.assertIn(start.seq, blocked["started_seqs"])
        self.assertEqual(self.f.events(self.f.target, "team.objective_completed"), [])
        with self.assertRaises(ValueError):
            self.f.rt.workspaces.reconcile(self.f.target, start.seq, reason="Missing stopped confirmation")
        # An agent cannot attest that the invocation was stopped and inspected.
        self.f.rt.log.emit(type="workspace.tool_reconciled", actor=Actor.agent(self.f.manager, "manager"),
            task_id=self.f.target, payload={"started_seq": start.seq, "started_hash": start.hash,
                "call_id": start.payload["call_id"], "invocation_stopped": True})
        self.assertTrue(self.f.rt.workspaces.pending(task_id=self.f.target))
        before = self.disk()
        with patch.object(WorkspaceExecutor, "install_bytes", side_effect=AssertionError("reconcile must not replay")):
            receipt = self.f.rt.workspaces.reconcile(self.f.target, start.seq,
                reason="Stopped fixture invocation; independently compared restored bytes and SHA", invocation_stopped=True)
        self.assertEqual(receipt.payload["started_hash"], start.hash)
        self.assertEqual(self.disk(), before)
        self.assertEqual(self.f.rt.workspaces.pending(task_id=self.f.target), [])
        self.assertFalse(any(r["started_seq"] == start.seq for r in self.f.rt.team._pending_operations(self.f.target)))
        self.assertEqual(self.restore(args)["outcome"], "already_present")
        self.f.finish(self.f.target, artifacts=[], verification="Inspected fixture bytes; no program execution")

    def test_real_link_publication_then_error_is_unknown_and_not_replayed(self):
        delivery, artifact = self.archive.delivered()
        args = self.selection(delivery, artifact, "binary.bin")
        original = os.link

        def link_then_fail(*a, **kw):
            original(*a, **kw)
            raise OSError("PRIVATE_EXCEPTION link succeeded before simulated failure")

        with patch("cater.workspace_tools.os.link", side_effect=link_then_fail) as link:
            result = self.restore(args)
        self.assertEqual(link.call_count, 1)
        self.rejected(result)
        self.assertEqual((result["complete"], result["outcome"]), (False, "effects_unknown"))
        self.assertEqual((self.target_root / "binary.bin").read_bytes(), archive_fixtures.FILES["binary.bin"])
        self.assertFalse(list(self.target_root.rglob(".workspace-tmp-*")))
        start, _ = self.pair()
        self.assertEqual([e.seq for e in self.f.rt.workspaces.pending(task_id=self.f.target)], [start.seq])

    def test_missing_finish_receipt_stays_pending_across_restart(self):
        delivery, artifact = self.archive.delivered()
        args = self.selection(delivery, artifact)
        emit = self.f.rt.log.emit

        def lose_finish(**kwargs):
            if kwargs.get("type") == "team.tool_finished" and kwargs.get("payload", {}).get("name") == TOOL:
                raise OSError("PRIVATE_EXCEPTION simulated lost receipt")
            return emit(**kwargs)

        with patch.object(self.f.rt.log, "emit", side_effect=lose_finish):
            run = self.native_run(self.f.target, [archive_fixtures.fixtures.turn("", (TOOL, args))])
        self.assertEqual(run.payload["status"], "failed")
        start = self.f.events(self.f.target, "team.tool_started")[-1]
        self.assertFalse(self.f.events(self.f.target, "team.tool_finished"))
        self.assertEqual((self.target_root / args["path"]).read_bytes(), archive_fixtures.FILES[args["path"]])
        self.f.rt.close()
        self.f.rt = Runtime(self.f.database, roles_path=self.f.roles, workspaces_path=self.archive.config,
                            drivers={"team_test": self.f.driver})
        self.assertEqual([e.seq for e in self.f.rt.workspaces.pending(task_id=self.f.target)], [start.seq])
        blocked, = self.f.exchange(self.f.target, ("finish_work", {"summary": "Blocked", "verification": "Missing receipt"}))
        self.assertEqual(blocked["code"], "operation_pending")
        self.f.rt.workspaces.reconcile(self.f.target, start.seq, reason="Stopped; inspected actual restored fixture bytes", invocation_stopped=True)
        self.assertEqual(self.f.rt.workspaces.pending(task_id=self.f.target), [])
        self.assertTrue(self.f.rt.verify_chain().ok)

    def test_delivered_target_cannot_restore_more_files(self):
        delivery, artifact = self.archive.delivered()
        self.f.finish(self.f.target, artifacts=[])
        before = self.disk()
        result = self.restore(self.selection(delivery, artifact))
        self.assertEqual(result["status"], "error")
        self.assertEqual(self.disk(), before)


class RestoredFixtureHostReaderTests(unittest.TestCase):
    def test_controlled_python_reads_restored_project_after_guarded_runtime_closes(self):
        # Do not unpatch Popen or weaken the inherited fixture while Runtime lives.
        with tempfile.TemporaryDirectory() as directory:
            case = ProjectFileRestoreTests()
            case.host_target_root = Path(directory).resolve() / "restored"
            try:
                case.setUp()
                delivery, artifact = case.archive.delivered()
                for name, body in archive_fixtures.FILES.items():
                    args = case.selection(delivery, artifact, name)
                    case.assert_restored(case.restore(args), args, body)
                expected = {name: hashlib.sha256(body).hexdigest() for name, body in archive_fixtures.FILES.items()}
            finally:
                case.doCleanups()
            # Hard-coded host checker reads data only; -I/-S excludes project imports.
            checker = (
                "import hashlib,json,pathlib,socket,sys; "
                "socket.socket=lambda *a,**k: (_ for _ in ()).throw(AssertionError('network forbidden')); "
                "root=pathlib.Path(sys.argv[1]); expected=json.loads(sys.argv[2]); "
                "actual={name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in expected}; "
                "assert actual==expected,(actual,expected); print(json.dumps(actual,sort_keys=True))"
            )
            completed = subprocess.run([sys.executable, "-I", "-S", "-c", checker,
                str(case.host_target_root), json.dumps(expected)], cwd=directory,
                env={}, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20, check=True)
            self.assertEqual(json.loads(completed.stdout), expected)


if __name__ == "__main__":
    unittest.main()
