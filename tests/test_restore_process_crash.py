"""Real OS kill/wait acceptance; every fresh fixture is retained as evidence.

Run with unittest discovery (-s tests). No private QA directory is required.
The package used by the parent is also the only package root given to workers.
"""

from __future__ import annotations

from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

import cater
from cater.processes import worker_python
from cater.events import Actor
from cater.workspace_tools import WorkspaceExecutor
from cater.work import WorkConflict
from support import restore_crash_worker as harness


class RestoreProcessCrashTests(unittest.TestCase):
    def check_stage(self, stage):
        root = Path(tempfile.mkdtemp(prefix="cater-restore-crash-")).resolve()
        print(f"\ncrash evidence: {root}", flush=True)
        package_root = Path(cater.__file__).resolve().parent.parent
        process = None
        report = {"stage": stage, "root": str(root), "package_root": str(package_root)}
        try:
            data = harness.prepare(root, package_root)
            launch_env = {}
            if os.name == "nt":
                import ctypes

                windows_directory = ctypes.create_unicode_buffer(32768)
                size = ctypes.windll.kernel32.GetWindowsDirectoryW(windows_directory, len(windows_directory))
                self.assertTrue(0 < size < len(windows_directory))
                launch_env["SystemRoot"] = windows_directory.value
            # All parent setup guards and Runtime have exited before Popen.
            with (root / "stdout.log").open("wb") as stdout, (root / "stderr.log").open("wb") as stderr:
                process = subprocess.Popen([
                    worker_python(), "-B", "-I", "-S", str(Path(harness.__file__).resolve()),
                    str(root), str(package_root), stage], cwd=root, env=launch_env, stdin=subprocess.DEVNULL,
                    stdout=stdout, stderr=stderr,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                deadline = time.monotonic() + 30
                while True:
                    self.assertIsNone(process.poll(), (root / "stderr.log").read_text(errors="replace"))
                    try:
                        marker = json.loads((root / "marker.json").read_text(encoding="utf-8"))
                        break
                    except (FileNotFoundError, json.JSONDecodeError):
                        if time.monotonic() >= deadline:
                            self.fail("marker observation timed out; this does not establish process death")
                        time.sleep(0.02)
                self.assertEqual((marker["pid"], marker["stage"]), (process.pid, stage))
                self.assertIs(marker["dont_write_bytecode"], True)
                self.assertTrue(marker["environment_empty"])
                self.assertEqual(marker["guard_checks"], {"socket": True, "connection": True, "process": True})
                self.assertEqual(Path(marker["package"]).parent, package_root / "cater")
                report["marker"] = marker
                driver = harness.Driver()
                with harness.offline(root), harness.runtime(root, driver) as rt:
                    target = data["target"]
                    events = lambda kind: [e for e in rt.log.read(task_id=target) if e.type == kind]
                    run, = events("team.run_started")
                    report["run_payload"] = run.payload
                    report["current_scope"] = rt.team.recovery.scope(target)
                    start, = events("team.tool_started")
                    self.assertEqual(start.payload["name"], harness.TOOL)
                    self.assertEqual(start.payload["run_id"], run.payload["run_id"])
                    self.assertEqual(run.payload["recovery_scope"], rt.team.recovery.scope(target))
                    self.assertEqual(start.payload["arguments"], data["arguments"])
                    self.assertLess(run.payload["message_seq"], run.seq)
                    self.assertLess(run.seq, start.seq)
                    self.assertFalse(events("team.run_finished"))
                    receipted = stage == "after_receipt_before_run_finished"
                    self.assertEqual(len(events("team.tool_finished")), int(receipted))
                    if receipted:
                        receipt, = events("team.tool_finished")
                        self.assertEqual(receipt.payload["started_seq"], start.seq)
                        self.assertEqual(receipt.payload["result"]["outcome"], "written")
                        self.assertEqual(receipt.payload["result"]["restored_file"]["sha256"],
                                         hashlib.sha256(harness.BODY).hexdigest())
                    before_events = rt.log.read(task_id=target)
                    self.assertEqual(rt.collaborate(target).get("consumer_owned"), False)
                    self.assertEqual(rt.team.recovery.prepare(target), [])
                    self.assertEqual(rt.log.read(task_id=target), before_events)
                    self.assertEqual(driver.calls, [])
                    if not receipted:
                        with self.assertRaises(WorkConflict):
                            rt.workspaces.reconcile(target, start.seq, reason="Live negative control",
                                                    invocation_stopped=True)
                    with rt.workspaces._owner(str(root / "target")) as owned:
                        self.assertEqual(owned, receipted)
                    self.assertIsNone(process.poll())
                    process.kill()
                    report["exit_code"] = process.wait(timeout=15)
                    self.assertIsNotNone(process.returncode)
                    self.assertNotEqual(process.returncode, 0)
                    actual = harness.disk(root / "target")
                    self.assertEqual(actual, marker["disk"])
                    report["after_kill_disk"] = actual
                    staging = [name for name in actual if name.startswith(".workspace-tmp-")]
                    self.assertEqual(len(staging), int(stage in ("before_link", "after_link")))
                    self.assertEqual("binary.bin" in actual, stage != "before_link")
                    for row in actual.values():
                        self.assertEqual(row["sha256"], hashlib.sha256(harness.BODY).hexdigest())
                        self.assertEqual(row["bytes"], len(harness.BODY))
                    if stage == "after_link":
                        self.assertEqual(actual[staging[0]]["inode"], actual["binary.bin"]["inode"])
                        self.assertEqual(actual[staging[0]]["device"], actual["binary.bin"]["device"])
                        self.assertGreaterEqual(actual["binary.bin"]["nlink"], 2)
                    with rt.workspaces._owner(str(root / "target")) as owned:
                        self.assertTrue(owned)
                    pending = lambda: [e.seq for e in rt.workspaces.pending(task_id=target)]
                    self.assertEqual(pending(), [] if receipted else [start.seq])
                    recovery = rt.team.recovery.prepare(target)
                    self.assertEqual(len(recovery), 1)
                    retired, = events("team.run_finished")
                    self.assertEqual(retired.payload["status"], "interrupted")
                    self.assertEqual(retired.payload["recovery_of"], run.seq)
                    self.assertEqual(recovery[0].payload["recovery_hash"], run.hash)
                    self.assertEqual(recovery[0].payload["interrupted_seq"], retired.seq)
                    self.assertTrue(rt.team.recovery.is_message(recovery[0]))
                    self.assertEqual([e.seq for e in rt.team.recovery.prepare(target)], [recovery[0].seq])
                    self.assertEqual(len(events("team.run_finished")), 1)
                    with ExitStack() as guards:
                        install = guards.enter_context(patch.object(WorkspaceExecutor, "install_bytes",
                            side_effect=AssertionError("recovery must not replay installation")))
                        link = guards.enter_context(patch("cater.workspace_tools.os.link",
                            side_effect=AssertionError("recovery must not publish")))
                        calls_before_recovery = len(driver.calls)
                        messages_before_recovery = events("team.message_received")
                        rt.collaborate(target)
                        # Prove recovery itself was consumed before any human exchange can mask a missed claim.
                        recovered_run, = [e for e in events("team.run_started") if e.seq > recovery[0].seq]
                        self.assertEqual(recovered_run.payload["message_seq"], recovery[0].seq)
                        self.assertEqual(recovered_run.payload["role"], rt.organization.leader.id)
                        self.assertNotEqual(recovered_run.payload["run_id"], run.payload["run_id"])
                        recovered_finish, = [e for e in events("team.run_finished")
                            if e.payload["run_id"] == recovered_run.payload["run_id"]]
                        self.assertEqual(recovered_finish.payload["message_seq"], recovery[0].seq)
                        self.assertIn(recovered_finish.payload["status"], {"completed", "waiting"})
                        model_call, = [e for e in events("team.model_called")
                            if e.payload["run_id"] == recovered_run.payload["run_id"]]
                        self.assertLess(recovered_run.seq, model_call.seq)
                        self.assertLess(model_call.seq, recovered_finish.seq)
                        self.assertEqual(model_call.payload["driver"], harness.Driver.name)
                        self.assertEqual(len(driver.calls), calls_before_recovery + 1)
                        first_call = driver.calls[calls_before_recovery]
                        self.assertEqual(first_call["history"], [])
                        self.assertEqual(first_call["context"]["message"]["seq"], recovery[0].seq)
                        self.assertEqual(first_call["context"]["message"]["kind"], "recovery")
                        window = first_call["context"]["tool_history_window"]
                        self.assertEqual(window["run_id"], recovered_run.payload["run_id"])
                        self.assertEqual(window["omitted_groups"], 0)
                        self.assertEqual(events("team.message_received"), messages_before_recovery)
                        self.assertEqual(events("team.tool_started"), [start])
                        report["recovery_consumption"] = {
                            "message_seq": recovery[0].seq, "run": recovered_run.payload,
                            "model_event_seq": model_call.seq, "finish": recovered_finish.payload,
                            "first_history": first_call["history"], "tool_history_window": window}
                        self.assertEqual(pending(), [] if receipted else [start.seq])
                        operations = rt.team._pending_operations(target)
                        self.assertEqual([r["started_seq"] for r in operations], [] if receipted else [start.seq])
                        if not receipted:
                            blocked, = harness.exchange(rt, driver, data["shared"],
                                (harness.TOOL, {**data["arguments"], "destination": "blocked.bin"}))
                            self.assertEqual(blocked["outcome"], "not_started")
                            self.assertEqual(blocked["code"], "workspace_pending")
                            blocked, = harness.exchange(rt, driver, target, ("finish_work", {
                                "summary": "Must block", "verification": "Unreviewed effects"}))
                            self.assertEqual(blocked["code"], "operation_pending")
                            self.assertIn(start.seq, blocked["started_seqs"])
                            self.assertFalse(events("team.objective_completed"))
                            with self.assertRaises(ValueError):
                                rt.workspaces.reconcile(target, start.seq, reason="Missing stopped attestation")
                            rt.log.emit(type="workspace.tool_reconciled", actor=Actor.agent(
                                rt.organization.leader.id, "manager"), task_id=target, payload={
                                    "started_seq": start.seq, "started_hash": start.hash,
                                    "call_id": start.payload["call_id"], "invocation_stopped": True})
                            self.assertEqual(pending(), [start.seq])
                            # Protected staging is observable only to the trusted host inspector.
                            if staging:
                                read, = harness.exchange(rt, driver, target,
                                    ("workspace_read", {"path": staging[0]}))
                                self.assertEqual(read["status"], "error")
                            reviewed = rt.workspaces.reconcile(target, start.seq,
                                reason=f"PID {process.pid} killed and waited; exact paths, inode, SHA inspected; staging retained",
                                invocation_stopped=True)
                            self.assertEqual(reviewed.actor, Actor.human())
                            self.assertEqual(reviewed.payload["started_hash"], start.hash)
                            self.assertEqual(pending(), [])
                            self.assertEqual(rt.team._pending_operations(target), [])
                        self.assertEqual(harness.disk(root / "target"), actual)
                        self.assertFalse(events("team.objective_completed"))
                        completed, = harness.exchange(rt, driver, target, ("finish_work", {
                            "summary": "Fixture reviewed", "verification": "Host inspected actual effects",
                            "artifact_seqs": []}))
                        self.assertEqual(completed["status"], "ok")
                        install.assert_not_called()
                        link.assert_not_called()
                    self.assertEqual(len(events("team.objective_completed")), 1)
                    self.assertEqual([e.seq for e in events("team.tool_started")
                        if e.payload["name"] == harness.TOOL], [start.seq])
                    self.assertEqual(harness.disk(root / "target"), actual)
                    self.assertTrue(rt.verify_chain().ok)
                    report["events"] = [{"seq": e.seq, "hash": e.hash, "type": e.type,
                        "actor": {"kind": e.actor.kind, "id": e.actor.id}, "payload": e.payload}
                        for e in rt.log.read(task_id=target)]
                    report["passed"] = True
        finally:
            if process is not None:
                if process.poll() is None:
                    process.kill()
                # A timeout is an observation only; finally always reaps the actual worker.
                process.wait()
                report["final_exit_code"] = process.returncode
            report["final_disk"] = harness.disk(root / "target")
            (root / "evidence.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

    def test_before_link(self):
        self.check_stage("before_link")

    def test_after_link(self):
        self.check_stage("after_link")

    def test_after_install_before_receipt(self):
        self.check_stage("after_install_before_receipt")

    def test_after_receipt_before_run_finished(self):
        self.check_stage("after_receipt_before_run_finished")


if __name__ == "__main__":
    unittest.main()
