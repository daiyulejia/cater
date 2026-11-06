"""Conservative reconciliation when workspace or team ownership is uncertain."""
import multiprocessing
import os
from contextlib import nullcontext, redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cater.activity_owner import activity_owner
from cater.events import Actor
from cater.runtime import Runtime
from cater.work import WorkConflict
import test_workspace_integration as integration


def hold_owner_with_temp(database, root, temp, ready, release, result):
    os.environ["TEMP"] = os.environ["TMP"] = temp
    tempfile.tempdir = None
    with Runtime(database) as rt:
        with rt.workspaces._owner(root) as owned:
            result.put(owned)
            ready.set()
            release.wait(10)


class MissingWorkspaceReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = integration.WorkspaceIntegrationTests("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_missing_root_preserves_pending_and_uncertain_claim(self):
        f = self.fixture
        claim, context = f.prepare_call()
        started = f.start_unknown(context)
        f.rt.work.fail(f.task, claim, "outcome unknown", uncertain=True)
        other = f.directory / "other"; other.mkdir()
        f.root.rmdir()
        self.assertEqual([e.seq for e in f.rt.workspaces.pending(root=str(other), effects_only=True)], [started.seq])
        with self.assertRaises(ValueError):
            f.rt.workspaces.reconcile(f.task, started.seq, reason="reviewed")
        with self.assertRaisesRegex(WorkConflict, "工作目录不存在"):
            f.rt.workspaces.reconcile(f.task, started.seq, reason="checked", invocation_stopped=True)
        with self.assertRaisesRegex(WorkConflict, "工作目录不存在"):
            f.rt.workspaces.reconcile_work(f.task, context["attempt_id"], reason="checked", invocation_stopped=True)
        from cater.cli import main
        output = io.StringIO()
        with patch("cater.cli.open_runtime", return_value=nullcontext(f.rt)), redirect_stdout(output):
            rc = main(["workspace", f.task, "--reconcile", str(started.seq), "--reason", "checked", "--invocation-stopped"])
        self.assertEqual(rc, 1)
        self.assertIn("工作目录不存在", output.getvalue())
        self.assertEqual([e.seq for e in f.rt.workspaces.pending(task_id=f.task)], [started.seq])
        self.assertEqual(f.events("workspace.tool_reconciled"), [])
        self.assertEqual(f.events("workspace.work_reconciled"), [])
        self.assertTrue(f.rt.work.view(f.task)["items"][0]["execution_in_flight"])
        self.assertEqual(f.driver.tool_requests, [])
        self.assertEqual(f.executor.calls, [])

    def test_other_temp_owner_alive_cannot_reconcile_missing_root(self):
        f = self.fixture
        _, context = f.prepare_call()
        started = f.start_unknown(context)
        temp_a = f.directory / "temp-a"; temp_a.mkdir()
        temp_b = f.directory / "temp-b"; temp_b.mkdir()
        ctx = multiprocessing.get_context("spawn")
        ready, release = ctx.Event(), ctx.Event()
        result = ctx.Queue()
        proc = ctx.Process(target=hold_owner_with_temp,
            args=(str(f.directory / "other.db"), str(f.root), str(temp_a), ready, release, result))
        saved_tempdir = tempfile.tempdir
        try:
            proc.start(); self.assertTrue(ready.wait(8)); self.assertTrue(result.get(timeout=2))
            f.root.rmdir()
            with patch.dict(os.environ, {"TEMP": str(temp_b), "TMP": str(temp_b)}):
                tempfile.tempdir = None
                with self.assertRaisesRegex(WorkConflict, "工作目录不存在"):
                    f.rt.workspaces.reconcile(f.task, started.seq, reason="checked", invocation_stopped=True)
                self.assertTrue(proc.is_alive())
                self.assertEqual([e.seq for e in f.rt.workspaces.pending(task_id=f.task)], [started.seq])
                self.assertEqual(f.events("workspace.tool_reconciled"), [])
        finally:
            tempfile.tempdir = saved_tempdir
            release.set()
            if proc.is_alive(): proc.join(8)
            if proc.is_alive(): proc.terminate(); proc.join(5)
            result.close()
        self.assertEqual(proc.exitcode, 0)

    def test_team_start_requires_simple_team_owner_even_when_root_exists(self):
        f = self.fixture
        f.bind()
        event = f.rt.log.emit(type="team.tool_started", task_id=f.task, actor=Actor.system("team"),
            payload={"name": "workspace_write", "root": str(f.root), "call_id": "fixture", "run_id": "fixture"})
        with activity_owner(f.rt.log.path, f.task, "simple-team") as team_owned:
            self.assertTrue(team_owned)
            with self.assertRaises(WorkConflict):
                f.rt.workspaces.reconcile(f.task, event.seq, reason="checked", invocation_stopped=True)
            self.assertEqual([e.seq for e in f.rt.workspaces.pending(task_id=f.task)], [event.seq])
        # A nonblocking failed attempt must not retain a lock or deadlock retries.
        receipt = f.rt.workspaces.reconcile(f.task, event.seq, reason="checked actual effects", invocation_stopped=True)
        self.assertEqual(receipt.actor, Actor.human())
        self.assertEqual(receipt.payload["outcome"], "effects_reviewed_no_replay")
        self.assertEqual(f.rt.workspaces.pending(task_id=f.task), [])
        self.assertEqual(f.executor.calls, [])

    def test_root_lost_before_commit_does_not_clear_pending(self):
        f = self.fixture
        _, context = f.prepare_call()
        started = f.start_unknown(context)
        real = f.rt.workspaces._reconcile_root_present
        calls = 0
        def disappears(root):
            nonlocal calls
            calls += 1
            if calls >= 3:
                raise WorkConflict("工作目录不存在；保留待核对状态。")
            return real(root)
        with patch.object(f.rt.workspaces, "_reconcile_root_present", side_effect=disappears):
            with self.assertRaisesRegex(WorkConflict, "工作目录不存在"):
                f.rt.workspaces.reconcile(f.task, started.seq, reason="checked", invocation_stopped=True)
        self.assertEqual(calls, 3)
        self.assertEqual([e.seq for e in f.rt.workspaces.pending(task_id=f.task)], [started.seq])
        self.assertEqual(f.events("workspace.tool_reconciled"), [])

    def test_recreated_directory_before_commit_preserves_pending(self):
        f = self.fixture
        _, context = f.prepare_call()
        started = f.start_unknown(context)
        real = f.rt.workspaces._reconcile_root_present
        calls = 0
        def replaced(root):
            nonlocal calls
            calls += 1
            if calls == 3:
                f.root.rmdir()
                f.root.mkdir()
            return real(root)
        with patch.object(f.rt.workspaces, "_reconcile_root_present", side_effect=replaced):
            with self.assertRaisesRegex(WorkConflict, "工作目录身份已变化"):
                f.rt.workspaces.reconcile(f.task, started.seq, reason="checked", invocation_stopped=True)
        self.assertEqual([e.seq for e in f.rt.workspaces.pending(task_id=f.task)], [started.seq])
        self.assertEqual(f.events("workspace.tool_reconciled"), [])


if __name__ == "__main__":
    unittest.main()
