"""Independent command receipt contract, with native dispatch and real Python.

Execution examples use the existing deterministic Team driver and disposable
workspaces. Synthetic events below exercise attribution and projection only;
they are never offered as evidence that an example process actually ran.
"""

from __future__ import annotations

from contextlib import contextmanager
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest.mock import patch

from cater.events import Actor
from cater.team import HOST
from cater.team_delivery import HandoffError
from cater.workspace_tools import WorkspaceExecutor
from cater.workspaces import WorkspaceService
import test_team_review_cycle as reviews


PROBE = """import hashlib, json, sys
from pathlib import Path
print(json.dumps({'cwd': str(Path.cwd()),
    'data': Path('data.txt').read_text(encoding='utf-8'),
    'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    'args': sys.argv[1:]}, sort_keys=True))
"""
RECORD_KEYS = {
    "version", "source_seq", "source_hash", "started_seq", "started_hash",
    "role", "tool", "request", "request_complete", "stdin", "argv",
    "authorized_root", "cwd", "environment", "status", "outcome",
    "exit_code", "complete", "scope_current", "artifacts_unchanged",
    "artifact_check_valid", "selected_versions_current", "stdout", "stderr",
    "checked_artifacts", "delivery_artifact_seqs",
}
KNOWN_REQUEST_KEYS = {"command", "args", "cwd", "timeout", "artifact_seqs", "artifact_versions"}
UNSET = object()
EXECUTION_EVIDENCE = None


class DeliveryCommandTests(unittest.TestCase):
    def setUp(self):
        self.f = reviews.TeamReviewCycleTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.project = self.f.bind_workspace()
        self.rt, self.task = self.f.rt, self.f.task
        self.team, self.log = self.rt.team, self.rt.log
        self.config = self.f.root / "supervision-workspaces.json"
        self.prefix = [sys.executable, "-I", "-B", "-X", "utf8"]
        self.number = 0
        self.policy()

    def policy(self, mode="execute", *, missing_program=False):
        commands = {"python": self.prefix}
        if missing_program:
            commands["missing"] = [str(self.f.root / "absent-command.exe")]
        self.config.write_text(json.dumps({"workspaces": [{
            "id": "supervision", "title": "Independent delivery command fixture",
            "root": str(self.project), "mode": mode, "timeout": 8,
            "commands": commands if mode == "execute" else {},
        }]}), encoding="utf-8")
        self.rt.workspaces = WorkspaceService(self.rt, self.config)
        revision = self.rt.workspaces.view(self.task)["revision"]
        self.rt.workspaces.bind(self.task, "supervision", expected_revision=revision)

    def event(self, seq):
        return next(e for e in self.log.read() if e.seq == seq)

    def events(self, kind):
        return list(self.log.iter_events(task_id=self.task, event_types=(kind,)))

    def archive(self, *, data="published-data", filename="project.zip"):
        files = {"package/probe.py": PROBE, "package/data.txt": data}
        for name, text in files.items():
            target = self.project / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8", newline="\n")
        result, = self.f.exchange(self.f.worker, ("publish_workspace_archive", {
            "filename": filename, "summary": "Complete independent probe", "paths": list(files),
        }))
        self.assertEqual(result["status"], "ok", result)
        return result["artifact_seq"]

    def run_command(self, arguments=None, *, task=None, **changes):
        arguments = {"command": "python", "args": ["-c", "print('actual-process')"],
                     **(arguments or {}), **changes}
        result, = self.f.exchange(self.f.manager, ("workspace_run", arguments), task_id=task)
        finishes = list(self.log.iter_events(task_id=task or self.task, event_types=("team.tool_finished",)))
        finish = next(e for e in reversed(finishes) if e.payload["name"] == "workspace_run")
        start = self.event(finish.payload["started_seq"])
        self.assertEqual(start.payload["arguments"], arguments)
        self.assertEqual(finish.payload["result"], result)
        self.remember_execution(start, finish, result)
        return start, finish, result

    def remember_execution(self, start, finish, result, record=None):
        if EXECUTION_EVIDENCE is None:
            return
        arguments = copy.deepcopy(start.payload["arguments"])
        body = arguments.pop("stdin", None)
        stdin = None if body is None else {"bytes": len(body.encode()), "sha256": hashlib.sha256(body.encode()).hexdigest()}
        entry = {"test": self.id(), "started_seq": start.seq, "started_hash": start.hash,
                 "source_seq": finish.seq, "source_hash": finish.hash, "authorized_root": start.payload.get("root"),
                 "arguments_without_stdin_body": arguments, "configured_stdin_digest": stdin,
                 "actual_result": {key: copy.deepcopy(result[key]) for key in (
                     "status", "outcome", "exit_code", "argv", "cwd", "environment", "complete",
                     "scope_current", "artifacts_unchanged", "artifact_check_valid", "selected_versions_current",
                     "checked_artifacts", "stdout", "stderr", "execution_context") if key in result}}
        if record is not None:
            entry["frozen_record"] = copy.deepcopy(record)
        EXECUTION_EVIDENCE.append(entry)

    def refs(self):
        return list({a["filename"]: {key: a[key] for key in ("seq", "filename", "content_sha256")}
                     for a in self.rt.task(self.task).deliverables}.values())

    def handoff(self, receipts=UNSET):
        value = {"why": "The probe checks the selected bytes; its output does not certify this explanation.",
                 "changed": [], "impact": [], "alternatives": [],
                 "reproduce": ["Member suggestion: modelreproduce --artifact-versions invented-client-path"],
                 "limits": ["No real model or independent approval; process success alone is not coverage."],
                 "evidence": []}
        if receipts is not UNSET:
            value["command_receipts"] = receipts
        return value

    def normalize(self, value, *, artifacts=None):
        return self.team.delivery.normalize(self.task, value,
            artifacts=self.refs() if artifacts is None else artifacts,
            revision=self.team.view(self.task)["revision"])

    def records(self, *receipts, artifacts=None):
        value = self.normalize(self.handoff([e.seq if hasattr(e, "seq") else e for e in receipts]),
                               artifacts=artifacts)
        if EXECUTION_EVIDENCE is not None:
            for record in value["command_receipts"]:
                for entry in EXECUTION_EVIDENCE:
                    if entry["test"] == self.id() and entry["source_seq"] == record["source_seq"]:
                        entry["frozen_record"] = copy.deepcopy(record)
        return value["command_receipts"]

    def finish(self, note=UNSET, *, artifact_seqs=None):
        args = {"summary": "Independent fixture delivery", "verification": "Deterministic model fixture; recorded process only"}
        if note is not UNSET:
            args["handoff"] = note
        if artifact_seqs is not None:
            args["artifact_seqs"] = artifact_seqs
        result, = self.f.exchange(self.f.manager, ("finish_work", args))
        return result

    def assert_record(self, record, start, finish, result):
        expected = RECORD_KEYS | ({"execution_context"} if "execution_context" in result else set())
        self.assertEqual(set(record), expected)
        if "execution_context" in result:
            self.assertEqual(record["execution_context"], result["execution_context"])
        self.assertEqual(record["version"], 1)
        self.assertEqual((record["source_seq"], record["source_hash"]), (finish.seq, finish.hash))
        self.assertEqual((record["started_seq"], record["started_hash"]), (start.seq, start.hash))
        self.assertEqual(record["role"], start.payload["role"])
        self.assertEqual(record["tool"], "workspace_run")
        self.assertEqual(record["status"], result["status"])
        self.assertEqual(record["authorized_root"], start.payload.get("root"))
        for key in ("argv", "cwd", "environment", "outcome", "exit_code", "complete",
                    "scope_current", "artifacts_unchanged", "artifact_check_valid", "selected_versions_current"):
            self.assertEqual(record[key], result.get(key), key)
        for key in ("stdout", "stderr"):
            if key not in result:
                self.assertIsNone(record[key])
                continue
            stream = record[key]
            self.assertIsInstance(stream, dict)
            self.assertLessEqual(len(stream["excerpt"].encode("utf-8")), 512)
            self.assertTrue(result[key]["excerpt"].startswith(stream["excerpt"]))
            for field in ("bytes", "sha256", "complete", "utf8_valid"):
                self.assertEqual(stream[field], result[key].get(field), (key, field))
            self.assertNotIn("tail_excerpt", stream)

    def emit(self, kind, payload, *, task=None, actor=HOST):
        return self.log.emit(type=kind, task_id=task or self.task, actor=actor, payload=payload)

    def chain(self, *, result=None, arguments=None, actors=None, start_changes=None,
              finish_changes=None, run_changes=None, message_changes=None, task=None,
              duplicate_run=None, early_end=False):
        """Adversarial log fixture, never used to claim a real process execution."""
        self.number += 1
        task = task or self.task
        actors = actors or {}
        revision = self.team.view(task)["revision"]
        message = self.emit("team.message_received", {"recipient": self.f.manager,
            "kind": "message", "text": "Independent attribution fixture", "revision": revision,
            **(message_changes or {})}, task=task, actor=actors.get("message", Actor.human()))
        run_payload = {"message_seq": message.seq, "role": self.f.manager,
            "run_id": f"delivery-command-chain-{self.number}", "revision": revision,
            "control_seq": self.rt.execution_control.view(task)["seq"], **(run_changes or {})}
        run = self.emit("team.run_started", run_payload, task=task, actor=actors.get("run", HOST))
        if duplicate_run == "before":
            self.emit("team.run_started", run_payload, task=task)
        start = self.emit("team.tool_started", {**run_payload, "name": "workspace_run",
            "call_id": f"delivery-command-call-{self.number}", "root": str(self.project),
            "arguments": {"command": "python", "args": ["-c", "pass"]} if arguments is None else arguments,
            **(start_changes or {})}, task=task, actor=actors.get("start", HOST))
        end_payload = {**run_payload, "status": "completed", "reason": "Attribution fixture closed"}
        if early_end:
            self.emit("team.run_finished", end_payload, task=task)
        finish = self.emit("team.tool_finished", {"started_seq": start.seq,
            "run_id": run_payload["run_id"], "role": run_payload["role"], "name": "workspace_run",
            "call_id": start.payload["call_id"],
            "result": {"status": "error"} if result is None else result,
            **(finish_changes or {})}, task=task, actor=actors.get("finish", HOST))
        if not early_end:
            self.emit("team.run_finished", end_payload, task=task)
        if duplicate_run == "after":
            self.emit("team.run_started", run_payload, task=task)
        return message, run, start, finish

    @contextmanager
    def raw_json(self, event, field, encoded):
        self.assertIn(field, ("actor_json", "payload_json"))
        # Corrupt only a parser-only shadow, retaining the real append-only log.
        original = self.log.db
        shadow = sqlite3.connect(":memory:")
        shadow.row_factory = sqlite3.Row
        schema = original.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'events'").fetchone()[0]
        shadow.execute(schema)
        cursor = original.execute("SELECT * FROM events")
        placeholders = ",".join("?" for _ in cursor.description)
        shadow.executemany("INSERT INTO events VALUES (" + placeholders + ")", cursor)
        try:
            shadow.execute(f"UPDATE events SET {field} = ? WHERE seq = ?", (encoded, event.seq))
            shadow.commit()
            self.log.db = shadow
            yield
        finally:
            self.log.db = original
            shadow.close()

    @staticmethod
    def duplicate_json(value, key, replacement):
        # Deliberately construct duplicate keys; a dict cannot represent this attack.
        return json.dumps(value)[:-1] + "," + json.dumps(key) + ":" + json.dumps(replacement) + "}"

    def test_schema_declares_optional_at_most_four_positive_integer_receipts(self):
        workspace = WorkspaceExecutor(self.rt.workspaces.config["supervision"]["policy"])
        tool = next(t for t in self.team._schemas(self.rt.organization.leader, workspace) if t["name"] == "finish_work")
        schema = tool["input_schema"]["properties"]["handoff"]
        field = schema["properties"]["command_receipts"]
        self.assertEqual(field["type"], "array")
        self.assertEqual(field["maxItems"], 4)
        self.assertEqual(field["items"]["type"], "integer")
        self.assertEqual(field["items"]["minimum"], 1)
        self.assertNotIn("command_receipts", schema["required"])
        self.assertNotIn("handoff", tool["input_schema"]["required"])

    def test_scheduler_dispatch_freezes_real_published_copy_and_exact_receipt(self):
        seq = self.archive()
        (self.project / "package/data.txt").write_text("unpublished-workspace", encoding="utf-8")
        args = {"command": "python", "args": ["probe.py", "literal space"],
                "cwd": "package", "timeout": 5, "artifact_seqs": [seq]}
        selected = []

        def read_receipt(context, history, tools):
            finish = self.events("team.tool_finished")[-1]
            self.assertEqual(finish.payload["name"], "workspace_run")
            selected.append(finish)
            return self.f.native_turn("", ("read_receipts", {"receipt_seq": finish.seq}))

        def deliver(context, history, tools):
            response = json.loads(history[-1]["content"])
            self.assertEqual(response["receipts"][0]["seq"], selected[0].seq)
            return self.f.native_turn("", ("finish_work", {
                "summary": "Actual published copy probed", "verification": "One actual Python process",
                "artifact_seqs": [seq], "handoff": self.handoff([selected[0].seq]),
            }))

        self.f.driver.steps[self.f.manager] = [self.f.native_turn("", ("workspace_run", args)),
            read_receipt, deliver, self.f.native_turn("Recorded fixture delivery.")]
        self.team.receive(self.task, "Probe and deliver the exact published file", request_id="delivery-command-scheduler")
        self.f.run_team()
        completion, = self.events("team.objective_completed")
        record, = completion.payload["handoff"]["command_receipts"]
        finish = selected[0]
        start = self.event(finish.payload["started_seq"])
        result = finish.payload["result"]
        self.remember_execution(start, finish, result, record)
        self.assert_record(record, start, finish, result)
        self.assertEqual(record["request"], args)
        self.assertIs(record["request_complete"], True)
        self.assertIsNone(record["stdin"])
        self.assertEqual(record["argv"], self.prefix + args["args"])
        observed = json.loads(result["stdout"]["excerpt"])
        execution_cwd = Path(observed["cwd"])
        self.assertEqual(observed["data"], "published-data")
        self.assertEqual(observed["script_sha256"], hashlib.sha256(PROBE.encode()).hexdigest())
        self.assertEqual(observed["args"], ["literal space"])
        self.assertEqual(execution_cwd.name, "package")
        self.assertTrue(execution_cwd.parent.name.startswith("cater-artifact-run-"))
        self.assertNotEqual(execution_cwd.parent, self.project)
        self.assertFalse(execution_cwd.parent.exists(), "The original execution copy must be cleaned up")
        self.assertEqual(record["authorized_root"], str(self.project))
        self.assertEqual(record["cwd"], "package")
        self.assertEqual(record["environment"], "temporary_artifact_copy_not_sandbox")
        self.assertEqual(record["checked_artifacts"], completion.payload["artifacts"])
        self.assertEqual(record["delivery_artifact_seqs"], [seq])
        self.assertNotIn("modelreproduce", json.dumps(record))
        self.assertNotIn("--artifact-versions", json.dumps(record))
        self.assertIn("modelreproduce", completion.payload["handoff"]["reproduce"][0])
        self.assertEqual(self.events("team.review_requested"), [])
        self.assertEqual(self.team._pending_operations(self.task), [])

    def test_ordinary_project_command_does_not_bind_delivery_bytes(self):
        seq = self.archive()
        start, finish, result = self.run_command(args=["probe.py"], cwd="package")
        self.assertEqual(result["outcome"], "passed", result)
        record, = self.records(finish)
        self.assert_record(record, start, finish, result)
        self.assertEqual(Path(json.loads(result["stdout"]["excerpt"])["cwd"]), self.project / "package")
        self.assertIsNone(record["checked_artifacts"])
        self.assertEqual(record["delivery_artifact_seqs"], [])
        self.assertIsNone(record["artifact_check_valid"])
        self.assertNotIn("approved", record)
        self.assertTrue(self.finish(self.handoff([finish.seq]), artifact_seqs=[seq])["status"] == "ok")

    def test_real_nonzero_exit_is_not_converted_to_success_by_a_passing_excerpt(self):
        seq = self.archive()
        start, finish, result = self.run_command(args=["-c",
            "import sys; print('SMOKE PASSED'); print('INTEGRATION FAILED', file=sys.stderr); sys.exit(7)"],
            artifact_seqs=[seq])
        self.assertEqual((result["status"], result["outcome"], result["exit_code"]), ("error", "failed", 7))
        record, = self.records(finish)
        self.assert_record(record, start, finish, result)
        self.assertIn("SMOKE PASSED", record["stdout"]["excerpt"])
        self.assertIn("INTEGRATION FAILED", record["stderr"]["excerpt"])
        self.assertIs(record["artifact_check_valid"], True)
        self.assertEqual(self.finish(self.handoff([finish.seq]), artifact_seqs=[seq])["status"], "ok")
        markdown = self.team.delivery.markdown(self.events("team.objective_completed")[0])
        self.assertIn("INTEGRATION FAILED", markdown)
        self.assertIn("7", markdown)

    def test_real_not_started_and_launch_error_remain_distinct_from_nonzero_exit(self):
        self.policy(missing_program=True)
        observations = []
        for args in ({"command": "unlisted"}, {"cwd": "missing-directory"}, {"command": "missing"}):
            with self.subTest(args=args):
                start, finish, result = self.run_command(**args)
                expected = "launch_error" if args.get("command") == "missing" else "not_started"
                self.assertEqual(result["outcome"], expected, result)
                self.assertIsNone(result.get("exit_code"))
                record, = self.records(finish)
                observations.append(record["outcome"])
                self.assert_record(record, start, finish, result)
                self.assertEqual(record["outcome"], expected)
                self.assertIsNone(record["exit_code"])
                self.assertNotIn("error", record)
        self.assertEqual(observations, ["not_started", "not_started", "launch_error"])

    def test_real_stdin_body_is_digest_only_even_when_long_and_not_consumed(self):
        body = "STDIN_BODY_MUST_NOT_BE_COPIED_" + "\u4e2d" * 18000
        start, finish, result = self.run_command(args=["-c", "print('stdin intentionally ignored')"], stdin=body)
        self.assertEqual(result["outcome"], "passed", result)
        self.assertEqual(start.payload["arguments"]["stdin"], body)
        record, = self.records(finish)
        self.assertIs(record["request_complete"], False)
        self.assertEqual(record["request"], {k: v for k, v in start.payload["arguments"].items() if k in KNOWN_REQUEST_KEYS})
        self.assertEqual(record["stdin"], {"bytes": len(body.encode()), "sha256": hashlib.sha256(body.encode()).hexdigest()})
        self.assertNotIn("STDIN_BODY_MUST_NOT_BE_COPIED", json.dumps(record))
        self.assertNotIn("\u4e2d", json.dumps(record, ensure_ascii=False))
        self.assertNotIn("read", record["stdin"])
        self.assertEqual(record["argv"], result["argv"])

    def test_large_real_request_and_argv_are_omitted_whole_not_truncated(self):
        padding = "\u4e2d" * 500
        args = ["-c", "import sys; print(len(sys.argv[1].encode('utf-8')))", padding]
        _start, finish, result = self.run_command(args=args)
        self.assertEqual(result["outcome"], "passed", result)
        self.assertEqual(result["stdout"]["excerpt"].strip(), "1500")
        record, = self.records(finish)
        self.assertIsNone(record["request"])
        self.assertIsNone(record["argv"])
        self.assertIs(record["request_complete"], False)
        self.assertNotIn(padding, json.dumps(record, ensure_ascii=False))
        self.assertEqual(record["exit_code"], 0)

    def test_real_utf8_preview_is_prefix_only_and_retains_complete_capture_metadata(self):
        args = ["-c", "import sys; sys.stdout.buffer.write(('HEAD:'+'\u4e2d'*9000+'TAIL_ONLY_MARKER').encode('utf-8'))"]
        _start, finish, result = self.run_command(args=args)
        source = result["stdout"]
        self.assertTrue(source["complete"])
        self.assertTrue(source["truncated"])
        self.assertIn("TAIL_ONLY_MARKER", source["tail_excerpt"])
        record, = self.records(finish)
        preview = record["stdout"]
        self.assertEqual(preview["excerpt"], source["excerpt"].encode()[:512].decode("utf-8", errors="ignore"))
        self.assertTrue(preview["truncated"])
        for key in ("bytes", "sha256", "complete", "utf8_valid"):
            self.assertEqual(preview[key], source[key])
        self.assertNotIn("TAIL_ONLY_MARKER", json.dumps(record["stdout"]))
        self.assertNotIn("\ufffd", preview["excerpt"])

    def test_new_preview_clipping_marks_truncated_without_changing_full_capture_truth(self):
        _start, finish, result = self.run_command(args=["-c", "print('x'*800)"])
        self.assertFalse(result["stdout"]["truncated"])
        record, = self.records(finish)
        self.assertEqual(record["stdout"]["excerpt"], "x" * 512)
        self.assertTrue(record["stdout"]["truncated"])
        self.assertTrue(record["stdout"]["complete"])
        self.assertEqual(record["stdout"]["sha256"], result["stdout"]["sha256"])

    def test_real_invalid_utf8_is_not_repaired_into_a_full_valid_log(self):
        _start, finish, result = self.run_command(args=["-c", "import sys; sys.stdout.buffer.write(b'prefix\\xffsuffix')"])
        self.assertFalse(result["stdout"]["utf8_valid"])
        record, = self.records(finish)
        self.assertIs(record["stdout"]["utf8_valid"], False)
        self.assertEqual(record["stdout"]["bytes"], len(b"prefix\xffsuffix"))
        self.assertEqual(record["stdout"]["sha256"], hashlib.sha256(b"prefix\xffsuffix").hexdigest())
        self.assertEqual(record["stdout"]["excerpt"], result["stdout"]["excerpt"])

    def test_real_mutated_copy_keeps_exit_zero_separate_from_invalid_artifact_check(self):
        seq = self.archive()
        _start, finish, result = self.run_command(artifact_seqs=[seq], cwd="package", args=["-c",
            "from pathlib import Path; Path('data.txt').write_text('mutated copy'); print('mutation observed')"])
        self.assertEqual(result["exit_code"], 0)
        self.assertFalse(result["artifacts_unchanged"])
        record, = self.records(finish)
        self.assertEqual(record["exit_code"], 0)
        self.assertFalse(record["artifacts_unchanged"])
        self.assertFalse(record["artifact_check_valid"])
        self.assertTrue(record["scope_current"])
        self.assertEqual((self.project / "package/data.txt").read_text(), "published-data")

    def test_real_timeout_retains_outcome_and_existing_pending_operation_without_replay(self):
        _start, finish, result = self.run_command(timeout=0.2, args=["-c", "import time; time.sleep(3)"])
        self.assertEqual((result["status"], result["outcome"]), ("error", "timeout"), result)
        pending = copy.deepcopy(self.team._pending_operations(self.task))
        self.assertEqual(len(pending), 1)
        with patch.object(WorkspaceExecutor, "execute", side_effect=AssertionError("Timeout evidence cannot authorize replay")):
            record, = self.records(finish)
        self.assertEqual(record["outcome"], "timeout")
        self.assertIsNone(record["exit_code"])
        self.assertEqual(self.team._pending_operations(self.task), pending)

    def test_real_republication_during_execution_preserves_historical_scope_flags(self):
        seq = self.archive()
        original = WorkspaceExecutor.execute
        source = self.event(seq)

        def execute(executor, name, arguments):
            result = original(executor, name, arguments)
            self.emit(source.type, copy.deepcopy(source.payload), actor=source.actor)
            return result

        with patch.object(WorkspaceExecutor, "execute", new=execute):
            _start, finish, result = self.run_command(artifact_seqs=[seq])
        self.assertEqual((result["outcome"], result["exit_code"]), ("passed", 0), result)
        self.assertIs(result["scope_current"], False)
        record, = self.records(finish)
        self.assertIs(record["scope_current"], False)
        self.assertIs(record["artifact_check_valid"], False)
        self.assertIs(record["artifacts_unchanged"], True)
        self.assertEqual(record["checked_artifacts"][0]["seq"], seq)
        self.assertEqual(record["delivery_artifact_seqs"], [])

    def test_unknown_effect_and_incomplete_capture_stay_separate_from_exit_and_scope(self):
        finish = self.chain(result={"status": "error", "outcome": "effects_unknown", "exit_code": None,
            "complete": False, "scope_current": False, "artifacts_unchanged": True,
            "artifact_check_valid": False, "stdout": {"excerpt": "partial", "complete": False, "truncated": True}})[3]
        record, = self.records(finish)
        self.assertEqual(record["outcome"], "effects_unknown")
        self.assertIsNone(record["exit_code"])
        self.assertIs(record["complete"], False)
        self.assertIs(record["scope_current"], False)
        self.assertIs(record["artifacts_unchanged"], True)
        self.assertIs(record["artifact_check_valid"], False)
        self.assertIs(record["stdout"]["complete"], False)
        self.assertIs(record["stdout"]["truncated"], True)

    def test_exact_historical_and_latest_versions_use_actual_json_mounts(self):
        before = self.archive(data="before")
        after = self.archive(data="after")
        versions = [{"artifact_seq": before, "directory": "before"}, {"artifact_seq": after, "directory": "after"}]
        script = ("from pathlib import Path; "
                  "print(Path('before/package/data.txt').read_text()+':'+Path('after/package/data.txt').read_text())")
        start, finish, result = self.run_command(artifact_versions=versions, args=["-c", script], cwd=".")
        self.assertEqual(result["stdout"]["excerpt"].strip(), "before:after")
        self.assertTrue(result["scope_current"])
        self.assertFalse(result["selected_versions_current"])
        record, = self.records(finish)
        self.assert_record(record, start, finish, result)
        self.assertEqual(record["request"]["artifact_versions"], versions)
        self.assertNotIn("--artifact-versions", record["argv"])
        self.assertEqual(record["checked_artifacts"], [
            {k: row[k] for k in ("seq", "filename", "content_sha256", "directory")}
            for row in result["checked_artifacts"]])
        self.assertEqual(record["delivery_artifact_seqs"], [after])
        self.assertTrue(record["artifact_check_valid"])
        self.assertEqual(self.events("team.review_recorded"), [])

    def test_delivery_binding_requires_sequence_name_and_hash_together(self):
        seq = self.archive()
        _start, finish, _result = self.run_command(artifact_seqs=[seq])
        ref, = self.refs()
        for bad in ({**ref, "seq": seq + 1}, {**ref, "filename": "other.zip"},
                    {**ref, "content_sha256": "0" * 64}):
            with self.subTest(changed=bad):
                record, = self.records(finish, artifacts=[bad])
                self.assertEqual(record["delivery_artifact_seqs"], [])
        record, = self.records(finish, artifacts=[ref])
        self.assertEqual(record["delivery_artifact_seqs"], [seq])

    def test_frozen_delivery_survives_new_files_new_receipts_and_new_goal_without_replay(self):
        seq = self.archive()
        _start, finish, _result = self.run_command(artifact_seqs=[seq], cwd="package", args=["probe.py"])
        self.assertEqual(self.finish(self.handoff([finish.seq]), artifact_seqs=[seq])["status"], "ok")
        completion, = self.events("team.objective_completed")
        saved = copy.deepcopy(self.team.delivery.read(self.task, {})["delivery"])
        self.emit("team.objective_updated", {"goal": "A new goal"})
        newer = self.archive(data="new-file-bytes")
        self.run_command(artifact_seqs=[newer], cwd="package", args=["probe.py"])
        before = len(self.events("team.tool_started"))
        with patch.object(WorkspaceExecutor, "execute", side_effect=AssertionError("Reading must not execute commands")):
            response, = self.f.exchange(self.f.manager, ("read_delivery", {"delivery_seq": completion.seq}))
            rendered = self.team.delivery.markdown(completion)
        self.assertEqual(response["delivery"], saved)
        self.assertTrue(response["historical"])
        self.assertEqual([e.payload["name"] for e in self.events("team.tool_started")[before:]], ["read_delivery", "wait_for_reply"])
        self.assertIn("实际命令记录", rendered)
        self.assertIn("成员建议的运行与复查", rendered)
        self.assertIn(finish.hash, rendered)
        self.assertEqual(saved["handoff"]["command_receipts"][0]["checked_artifacts"][0]["seq"], seq)

    def test_duplicate_finish_does_not_replace_frozen_command_selection(self):
        seq = self.archive()
        _s1, first, _r1 = self.run_command()
        _s2, second, _r2 = self.run_command(args=["-c", "print('second process')"])
        self.assertEqual(self.finish(self.handoff([first.seq]), artifact_seqs=[seq])["status"], "ok")
        saved = copy.deepcopy(self.events("team.objective_completed")[0].payload)
        self.finish(self.handoff([second.seq]), artifact_seqs=[seq])
        self.assertEqual([e.payload for e in self.events("team.objective_completed")], [saved])

    def test_markdown_distinguishes_false_unknown_and_true_recorded_observations(self):
        fields = ("complete", "scope_current", "artifacts_unchanged", "artifact_check_valid", "selected_versions_current")
        finish = self.chain(result={"status": "ok", "outcome": "passed", "exit_code": 0,
                                    **{key: True for key in fields}})[3]
        note = self.normalize(self.handoff([finish.seq]))
        completion = self.emit("team.objective_completed", {"summary": "Rendering fixture",
            "verification": "Synthetic rendering states only", "artifacts": [], "handoff": note})
        for field in fields:
            rendered = []
            for value in (True, False, None):
                variant = copy.deepcopy(completion)
                variant.payload["handoff"]["command_receipts"][0][field] = value
                rendered.append(self.team.delivery.markdown(variant))
            with self.subTest(field=field):
                self.assertEqual(len(set(rendered)), 3, f"Markdown hides distinct recorded {field} states")

    def test_markdown_preserves_checked_version_and_delivery_binding_information(self):
        ref = {"seq": 123, "filename": "version.zip", "content_sha256": "a" * 64, "directory": "before"}
        finish = self.chain(result={"status": "error", "checked_artifacts": [ref]})[3]
        note = self.normalize(self.handoff([finish.seq]))
        completion = self.emit("team.objective_completed", {"summary": "Rendering fixture",
            "verification": "Synthetic version rendering only", "artifacts": [], "handoff": note})
        original = self.team.delivery.markdown(completion)
        for field, replacement in (("seq", 456), ("filename", "other.zip"),
                                   ("content_sha256", "b" * 64), ("directory", "after")):
            variant = copy.deepcopy(completion)
            variant.payload["handoff"]["command_receipts"][0]["checked_artifacts"][0][field] = replacement
            with self.subTest(field=field):
                self.assertNotEqual(self.team.delivery.markdown(variant), original,
                                    f"Markdown hides recorded checked_artifacts.{field}")
        variant = copy.deepcopy(completion)
        variant.payload["handoff"]["command_receipts"][0]["delivery_artifact_seqs"] = [123]
        self.assertNotEqual(self.team.delivery.markdown(variant), original,
                            "Markdown hides the relationship to this delivery's bytes")

    def test_invalid_optional_reference_is_omitted_and_does_not_create_approval_gate(self):
        seq = self.archive()
        result = self.finish(self.handoff([999999]), artifact_seqs=[seq])
        self.assertEqual((result["status"], result["handoff_status"]), ("ok", "omitted_invalid"))
        completed, = self.events("team.objective_completed")
        self.assertNotIn("handoff", completed.payload)
        self.assertEqual(completed.payload["handoff_warning"], result["handoff_warning"])
        self.assertEqual(self.events("team.review_requested"), [])
        self.assertEqual(self.finish(self.handoff(), artifact_seqs=[seq])["status"], "ok")
        self.assertEqual(self.events("team.objective_completed"), [completed])

    def test_old_handoff_omits_command_field_and_does_not_read_new_command_sources(self):
        with patch("cater.delivery_commands.normalize", side_effect=AssertionError("Old handoff must not normalize command sources")):
            note = self.normalize(self.handoff())
        self.assertEqual(set(note), {"version", "why", "changed", "impact", "alternatives", "reproduce", "limits", "evidence"})
        seq = self.archive()
        self.assertEqual(self.finish(artifact_seqs=[seq])["status"], "ok")
        completion, = self.events("team.objective_completed")
        self.assertNotIn("handoff", completion.payload)
        self.assertNotIn("handoff", self.team.delivery.read(self.task, {})["delivery"])

    def test_command_permissions_and_pending_contract_are_unchanged(self):
        _start, finish, _result = self.run_command()
        self.assertEqual(self.team._pending_operations(self.task), [])
        self.policy(mode="edit")
        workspace = WorkspaceExecutor(self.rt.workspaces.config["supervision"]["policy"])
        self.assertNotIn("workspace_run", [s["name"] for s in self.team._schemas(self.rt.organization.leader, workspace)])
        with patch.object(WorkspaceExecutor, "execute", side_effect=AssertionError("Receipt normalization must not launch")):
            record, = self.records(finish)
        self.assertEqual(record["source_seq"], finish.seq)
        self.assertEqual(self.team._pending_operations(self.task), [])

    def test_selection_rejects_wrong_types_bool_nonpositive_and_more_than_four(self):
        _m, _r, _s, finish = self.chain()
        invalid = (None, True, finish.seq, str(finish.seq), {}, (), [True], [False], [0], [-1],
                   [1.0], [str(finish.seq)], [{}], [finish.seq] * 5)
        for selection in invalid:
            with self.subTest(selection=selection), self.assertRaises(HandoffError):
                self.normalize(self.handoff(selection))

    def test_empty_selection_and_four_valid_references_with_ordered_deduplication(self):
        first = self.chain()[3]
        second = self.chain()[3]
        third = self.chain()[3]
        fourth = self.chain()[3]
        self.assertEqual(self.normalize(self.handoff([]))["command_receipts"], [])
        self.assertEqual([r["source_seq"] for r in self.records(first, second, third, fourth)],
                         [first.seq, second.seq, third.seq, fourth.seq])
        self.assertEqual([r["source_seq"] for r in self.records(second, first, second, first)], [second.seq, first.seq])

    def test_selector_is_completed_receipt_seq_not_call_id_start_message_or_run_seq(self):
        message, run, start, finish = self.chain()
        for value in (message.seq, run.seq, start.seq, start.payload["call_id"], 999999, 2 ** 63, 10 ** 100):
            with self.subTest(selector=value), self.assertRaises(HandoffError):
                self.records(value)
        self.assertEqual(self.records(finish)[0]["source_seq"], finish.seq)

    def test_unknown_handoff_fields_and_caller_supplied_record_objects_are_rejected(self):
        _m, _r, _s, finish = self.chain()
        for note in ({**self.handoff([finish.seq]), "env": {"token": "private"}},
                     self.handoff([{"source_seq": finish.seq, "status": "ok"}])):
            with self.subTest(note=note), self.assertRaises(HandoffError):
                self.normalize(note)

    def test_non_workspace_run_and_missing_or_invalid_result_status_are_rejected(self):
        cases = ({"start_changes": {"name": "workspace_read"}, "finish_changes": {"name": "workspace_read"}},
                 {"result": {}}, {"result": {"status": True}}, {"result": {"status": "passed"}},
                 {"finish_changes": {"result": "not an object"}})
        for changes in cases:
            finish = self.chain(**changes)[3]
            with self.subTest(changes=changes), self.assertRaises(HandoffError):
                self.records(finish)

    def test_source_actor_must_be_exact_host_at_every_host_chain_link(self):
        for stage in ("run", "start", "finish"):
            for actor in (Actor.system("other"), Actor.agent(self.f.manager, "Manager"), Actor.human()):
                finish = self.chain(actors={stage: actor})[3]
                with self.subTest(stage=stage, actor=actor), self.assertRaises(HandoffError):
                    self.records(finish)
        finish = self.chain(actors={"message": Actor.system("other")})[3]
        with self.assertRaises(HandoffError):
            self.records(finish)

    def test_pairing_mismatches_and_noninteger_source_links_are_rejected(self):
        cases = [{"finish_changes": {key: value}} for key, value in (
            ("run_id", "other-run"), ("role", self.f.worker), ("name", "workspace_write"),
            ("call_id", "other-call"), ("started_seq", True), ("started_seq", 999999),
            ("started_seq", 1.0))]
        cases += [{"start_changes": {key: value}} for key, value in (
            ("message_seq", True), ("message_seq", 999999), ("message_seq", 1.0),
            ("call_id", ""), ("run_id", ""), ("role", "removed-member"), ("revision", True))]
        cases += [{"run_changes": {"role": self.f.worker}},
                  {"message_changes": {"recipient": self.f.worker}},
                  {"run_changes": {"role": "removed-member"}, "message_changes": {"recipient": "removed-member"}}]
        for changes in cases:
            finish = self.chain(**changes)[3]
            with self.subTest(changes=changes), self.assertRaises(HandoffError):
                self.records(finish)

    def test_previous_objective_receipt_cannot_be_selected_for_current_delivery(self):
        finish = self.chain()[3]
        self.emit("team.objective_updated", {"goal": "Different target"})
        with self.assertRaises(HandoffError):
            self.records(finish)

    def test_cross_group_receipt_and_cross_group_start_links_are_rejected(self):
        other = self.rt.create_task("Foreign disposable attribution group")
        self.team.enable(other)
        foreign = self.chain(task=other)[3]
        with self.assertRaises(HandoffError):
            self.records(foreign)
        local = self.chain(finish_changes={"started_seq": foreign.payload["started_seq"]})[3]
        with self.assertRaises(HandoffError):
            self.records(local)

    def test_duplicate_run_identity_before_or_after_settlement_is_ambiguous(self):
        for timing in ("before", "after"):
            finish = self.chain(duplicate_run=timing)[3]
            with self.subTest(timing=timing), self.assertRaises(HandoffError):
                self.records(finish)

    def test_tool_finish_after_run_end_is_not_execution_evidence(self):
        finish = self.chain(early_end=True)[3]
        with self.assertRaises(HandoffError):
            self.records(finish)

    def test_two_finishes_for_one_start_are_rejected_even_when_selection_names_one(self):
        _m, _r, _s, first = self.chain()
        second = self.emit("team.tool_finished", {**first.payload, "result": {"status": "ok", "exit_code": 0}})
        for finish in (first, second):
            with self.subTest(seq=finish.seq), self.assertRaises(HandoffError):
                self.records(finish)

    def test_duplicate_and_nul_payload_keys_at_each_chain_stage_are_rejected(self):
        chain = self.chain()
        for event in chain:
            key = "recipient" if event.type == "team.message_received" else "run_id"
            variants = [self.duplicate_json(event.payload, key, event.payload[key]),
                        json.dumps({**event.payload, key + "\0shadow": "ambiguous"})]
            for encoded in variants:
                with self.subTest(stage=event.type, encoded=encoded), self.raw_json(event, "payload_json", encoded):
                    with self.assertRaises(HandoffError):
                        self.records(chain[3])

    def test_duplicate_actor_keys_at_each_chain_stage_are_rejected(self):
        chain = self.chain()
        for event in chain:
            row = self.log.db.execute("SELECT actor_json FROM events WHERE seq = ?", (event.seq,)).fetchone()
            actor = json.loads(row[0])
            encoded = self.duplicate_json(actor, "id", actor["id"])
            with self.subTest(stage=event.type), self.raw_json(event, "actor_json", encoded):
                with self.assertRaises(HandoffError):
                    self.records(chain[3])

    def test_ambiguous_nested_arguments_results_and_output_are_rejected(self):
        _message, _run, start, finish = self.chain(result={"status": "error", "stdout": {"excerpt": "head"}})
        variants = [(start, "arguments", self.duplicate_json(start.payload["arguments"], "command", "other")),
                    (start, "arguments", json.dumps({**start.payload["arguments"], "cwd\0extra": "."})),
                    (finish, "result", self.duplicate_json(finish.payload["result"], "status", "ok")),
                    (finish, "result", '{"status":"error","stdout":{"excerpt":"first","excerpt":"second"}}')]
        for event, key, nested in variants:
            payload = {k: v for k, v in event.payload.items() if k != key}
            encoded = json.dumps(payload)[:-1] + "," + json.dumps(key) + ":" + nested + "}"
            with self.subTest(stage=event.type, key=key), self.raw_json(event, "payload_json", encoded):
                with self.assertRaises(HandoffError):
                    self.records(finish)

    def test_unknown_fields_environment_and_raw_exception_never_enter_projection(self):
        result = {"status": "error", "exit_code": 3, "outcome": "failed",
                  "argv": [sys.executable, "-c", "pass"], "env": {"token": "ENV_BODY_SENTINEL"},
                  "error": {"message": "RAW_EXCEPTION_SENTINEL"}, "unknown": "UNKNOWN_RESULT_SENTINEL",
                  "stdout": {"excerpt": "actual excerpt", "tail_excerpt": "TAIL_SENTINEL", "extra": "STREAM_EXTRA_SENTINEL"}}
        _m, _r, _s, finish = self.chain(result=result, arguments={"command": "python", "args": ["-c", "pass"],
            "env": {"secret": "ARGUMENT_ENV_SENTINEL"}, "private": "ARGUMENT_EXTRA_SENTINEL"})
        record, = self.records(finish)
        self.assertEqual(set(record), RECORD_KEYS)
        for marker in ("ENV_BODY_SENTINEL", "RAW_EXCEPTION_SENTINEL", "UNKNOWN_RESULT_SENTINEL", "TAIL_SENTINEL",
                       "STREAM_EXTRA_SENTINEL", "ARGUMENT_ENV_SENTINEL", "ARGUMENT_EXTRA_SENTINEL"):
            self.assertNotIn(marker, json.dumps(record))
        self.assertEqual(record["request"], {"command": "python", "args": ["-c", "pass"]})

    def test_missing_execution_observations_are_null_not_backfilled(self):
        _m, _r, _s, finish = self.chain(result={"status": "error"})
        record, = self.records(finish)
        for field in ("argv", "cwd", "environment", "outcome", "exit_code", "complete", "scope_current",
                      "artifacts_unchanged", "artifact_check_valid", "selected_versions_current", "stdin", "stdout", "stderr", "checked_artifacts"):
            self.assertIsNone(record[field], field)
        self.assertEqual(record["delivery_artifact_seqs"], [])

    def test_observation_types_are_strict_and_bool_is_not_an_exit_code(self):
        fields = {"exit_code": True, "complete": 1, "scope_current": "true", "artifacts_unchanged": [],
                  "artifact_check_valid": {}, "selected_versions_current": 0, "outcome": 3,
                  "cwd": [], "environment": False, "argv": ["python", 3]}
        finish = self.chain(result={"status": "error", **fields})[3]
        record, = self.records(finish)
        for field in fields:
            self.assertIsNone(record[field], field)

    def test_oversized_authorized_root_and_recorded_cwd_are_omitted_whole(self):
        long_path = "\u4e2d" * 400
        finish = self.chain(start_changes={"root": long_path}, result={"status": "error", "cwd": long_path})[3]
        record, = self.records(finish)
        self.assertIsNone(record["authorized_root"])
        self.assertIsNone(record["cwd"])
        self.assertNotIn(long_path, json.dumps(record, ensure_ascii=False))

    def test_stream_metadata_does_not_coerce_unknown_or_incorrectly_typed_values(self):
        finish = self.chain(result={"status": "error", "stdout": {"excerpt": "head", "bytes": True,
            "sha256": "not-a-hash", "complete": 1, "truncated": "false", "utf8_valid": 0}})[3]
        record, = self.records(finish)
        self.assertEqual(record["stdout"], {"excerpt": "head"})

    def test_checked_artifacts_are_complete_valid_bounded_references_or_null(self):
        ref = {"seq": 12, "filename": "project.zip", "content_sha256": "a" * 64}
        invalid = ([{**ref, "seq": True}], [{**ref, "content_sha256": "bad"}], [{"seq": 12}],
                   [ref] * 9, "not a list", [{**ref, "filename": "../escape.zip"}],
                   [{**ref, "directory": "../escape"}])
        for checked in invalid:
            finish = self.chain(result={"status": "error", "checked_artifacts": checked})[3]
            with self.subTest(checked=checked):
                record, = self.records(finish)
                self.assertIsNone(record["checked_artifacts"])
                self.assertEqual(record["delivery_artifact_seqs"], [])

    def test_eight_complete_checked_references_keep_mounts_but_drop_unrecognized_metadata(self):
        checked = [{"seq": seq, "filename": f"file-{seq}.zip", "content_sha256": "a" * 64,
                    "directory": f"mount-{seq}", "unknown": "PRIVATE_CHECK_METADATA"} for seq in range(10, 18)]
        finish = self.chain(result={"status": "error", "checked_artifacts": checked})[3]
        record, = self.records(finish, artifacts=[{k: v for k, v in checked[0].items() if k in {"seq", "filename", "content_sha256"}}])
        self.assertEqual(record["checked_artifacts"], [{k: v for k, v in row.items() if k != "unknown"} for row in checked])
        self.assertEqual(record["delivery_artifact_seqs"], [10])
        self.assertNotIn("PRIVATE_CHECK_METADATA", json.dumps(record))

    def test_request_and_argv_utf8_json_budget_is_independent_at_the_boundary(self):
        for request_size, argv_size in ((1024, 1024), (1025, 1024), (1024, 1025)):
            arguments = {"command": "python", "args": [""]}
            padding = request_size - len(json.dumps(arguments, ensure_ascii=False).encode())
            arguments["args"][0] = "\u4e2d" * (padding // 3) + "x" * (padding % 3)
            padding = argv_size - len(json.dumps([""], ensure_ascii=False).encode())
            argv = ["\u4e2d" * (padding // 3) + "x" * (padding % 3)]
            self.assertEqual(len(json.dumps(arguments, ensure_ascii=False).encode()), request_size)
            self.assertEqual(len(json.dumps(argv, ensure_ascii=False).encode()), argv_size)
            finish = self.chain(arguments=arguments, result={"status": "error", "argv": argv})[3]
            with self.subTest(request_size=request_size, argv_size=argv_size):
                record, = self.records(finish)
                self.assertEqual(record["request"], arguments if request_size <= 1024 else None)
                self.assertEqual(record["argv"], argv if argv_size <= 1024 else None)
                self.assertIs(record["request_complete"], request_size <= 1024)

    def test_total_handoff_budget_includes_host_generated_command_records(self):
        finish = self.chain(result={"status": "error", "argv": ["python", "x" * 800],
            "stdout": {"excerpt": "s" * 512}, "stderr": {"excerpt": "e" * 512}})[3]
        note = self.handoff([finish.seq])
        note.update(why="w" * 4000, changed=["c" * 1000] * 12, impact=["i" * 1000] * 6)
        without_commands = {k: v for k, v in note.items() if k != "command_receipts"}
        self.assertLessEqual(len(json.dumps(self.normalize(without_commands), ensure_ascii=False).encode()), 24000)
        with self.assertRaisesRegex(HandoffError, "24KB"):
            self.normalize(note)


def load_tests(loader, tests, pattern):
    return loader.loadTestsFromTestCase(DeliveryCommandTests)


if __name__ == "__main__":
    unittest.main()
