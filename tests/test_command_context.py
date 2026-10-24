"""Historical command context, using disposable Runtime fixtures only.

Synthetic chains test attribution and projection, not actual execution. The
native integration below runs local Python without a model or external API.

Focused Windows invocation (from the repository root):
  & 'C:/Users/dayulejia/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe' -B -m unittest discover -s tests -p test_command_context.py -v
"""

from __future__ import annotations

import copy
from dataclasses import replace
import hashlib
import json
import sys
import unittest
from unittest.mock import patch

from cater import delivery_commands
from cater.events import Actor
from cater.team import HOST
from cater.tool_protocol import json_utf8
from cater.workspace_tools import WorkspaceExecutor
import test_delivery_commands as commands


CONTEXT = {"version": 1, "invocation": "direct_argv",
           "environment": "minimal_with_allowlist", "path_policy": "os_default",
           "platform": sys.platform if sys.platform in {"win32", "linux", "darwin"} else "other"}


class CommandContextTests(unittest.TestCase):
    def setUp(self):
        # Composition keeps the source fixture's regression tests out of discovery.
        self.f = commands.DeliveryCommandTests()
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()
        self.rt, self.task = self.f.rt, self.f.task
        self.team, self.log = self.f.team, self.f.log

    def chain(self, *, result=None, **changes):
        return self.f.chain(result={"status": "ok", "outcome": "passed", "exit_code": 0,
            "argv": ["recorded-python", "-I", "-c", "pass"], "cwd": ".",
            **(result or {})}, **changes)

    @staticmethod
    def row(chain):
        message, run, start, finish = chain
        return {"seq": finish.seq, "started_seq": start.seq,
                "run_id": run.payload["run_id"], "message_seq": message.seq,
                "role": start.payload["role"], "name": start.payload["name"],
                **{key: finish.payload["result"][key] for key in
                   ("status", "outcome", "exit_code") if key in finish.payload["result"]}}

    def index(self, rows, **changes):
        return delivery_commands.index_records(self.team, self.task, rows,
            revision=changes.pop("revision", self.team.view(self.task)["revision"]), **changes)

    def context(self, message):
        context, _rules = self.team._context_snapshot(
            self.task, message, self.rt.organization.leader)
        return context

    def assert_preserved(self, rows, indexed):
        self.assertIsInstance(indexed, list)
        self.assertIsNot(indexed, rows)
        self.assertEqual(len(indexed), len(rows))
        for original, projected in zip(rows, indexed):
            self.assertIsInstance(projected, dict)
            self.assertIsNot(projected, original)
            self.assertEqual({k: v for k, v in projected.items() if k != "command_record"}, original)

    def assert_omitted(self, chain):
        rows = [self.row(chain)]
        indexed = self.index(rows)
        self.assert_preserved(rows, indexed)
        self.assertEqual(indexed, rows)

    def test_exact_source_request_argv_and_optional_launch_context(self):
        arguments = {"command": "python", "args": ["probe.py", "literal space", "a>b", "$HOME", "&&"],
                     "cwd": "package", "timeout": 5,
                     "artifact_versions": [{"artifact_seq": 12, "directory": "input"}]}
        argv = ["C:/original environment/python.exe", "-I", "-B", *arguments["args"]]
        chain = self.chain(arguments=arguments, result={"argv": argv, "cwd": "package", "execution_context": CONTEXT})
        start, finish = chain[2:]
        rows = [{**self.row(chain), "future_metadata": {"items": [1, 2]}}]
        before = copy.deepcopy(rows)
        indexed = self.index(rows)
        self.assert_preserved(rows, indexed)
        record = indexed[0]["command_record"]
        self.assertEqual((record["source_seq"], record["source_hash"]), (finish.seq, finish.hash))
        self.assertEqual((record["started_seq"], record["started_hash"]), (start.seq, start.hash))
        self.assertEqual(record["request"], arguments)
        self.assertIs(record["request_complete"], True)
        self.assertIsNone(record["stdin"])
        self.assertEqual(record["argv"], argv)
        self.assertNotEqual(record["argv"], [arguments["command"], *arguments["args"]])
        self.assertEqual(record["cwd"], "package")
        self.assertEqual(record["execution_context"], CONTEXT)
        self.assertEqual(record["recorded_revision"], self.team.view(self.task)["revision"])
        self.assertEqual(record["read_args"], {"receipt_seq": finish.seq})
        self.assertEqual(rows, before)
        record["argv"].append("projection-only")
        record["request"]["args"].append("projection-only")
        self.assertEqual(self.f.event(start.seq), start)
        self.assertEqual(self.f.event(finish.seq), finish)

    def test_stdin_fingerprint_and_no_stream_env_values_or_exception_body(self):
        body = "PRIVATE_STDIN_BODY_" + "\u4e2d" * 2000
        chain = self.chain(arguments={"command": "python", "args": ["probe.py"], "stdin": body,
                                     "env": {"TOKEN": "PRIVATE_REQUEST_ENV"}, "extra": "PRIVATE_ARGUMENT_EXTRA"},
            result={"stdout": {"excerpt": "PRIVATE_STDOUT", "tail_excerpt": "PRIVATE_TAIL"},
                    "stderr": "PRIVATE_STDERR", "env": {"TOKEN": "PRIVATE_RESULT_ENV"},
                    "error": {"message": "PRIVATE_EXCEPTION"}, "other": "PRIVATE_RESULT_EXTRA"})
        record = self.index([self.row(chain)])[0]["command_record"]
        self.assertEqual(record["request"], {"command": "python", "args": ["probe.py"]})
        self.assertIs(record["request_complete"], False)
        self.assertEqual(record["stdin"], {"bytes": len(body.encode("utf-8")),
            "sha256": hashlib.sha256(body.encode("utf-8")).hexdigest()})
        self.assertNotIn("PRIVATE_", json.dumps(record))
        self.assertNotIn("PRIVATE_", json.dumps(self.context(chain[0])))
        for field in ("stdout", "stderr", "env", "error", "command_line", "shell"):
            self.assertNotIn(field, record)

    def test_environment_and_capture_completeness_keep_normalized_history_semantics(self):
        temporary = "temporary_artifact_copy_not_sandbox"
        cases = [({}, None, None),
                 ({"environment": temporary, "complete": True}, temporary, True),
                 ({"environment": temporary, "complete": False}, temporary, False),
                 ({"environment": "current_workspace", "complete": 1}, None, None),
                 ({"environment": {"TOKEN": "PRIVATE_ENV"}, "complete": "true"}, None, None),
                 ({"complete": True, "stdout": {"complete": False}}, None, False),
                 ({"complete": True, "stderr": {"complete": False}}, None, False)]
        for result, environment, complete in cases:
            with self.subTest(result=result):
                chain = self.chain(result=result)
                record = self.index([self.row(chain)])[0]["command_record"]
                normalized, = self.f.records(chain[3])
                self.assertEqual(record["environment"], environment)
                self.assertIs(record["complete"], complete)
                self.assertEqual(record["environment"], normalized["environment"])
                self.assertIs(record["complete"], normalized["complete"])
                row, = [row for row in self.context(chain[0])["receipt_index"] if row["seq"] == chain[3].seq]
                self.assertEqual(row["command_record"]["environment"], environment)
                self.assertIs(row["command_record"]["complete"], complete)
                self.assertNotIn("PRIVATE_ENV", json.dumps(record))

    def test_legacy_and_invalid_execution_context_are_not_invented(self):
        invalid = [None, {}, [], "shell", {**CONTEXT, "version": True},
                   {**CONTEXT, "invocation": "shell"}, {**CONTEXT, "platform": "windows"},
                   {**CONTEXT, "env": {"TOKEN": "PRIVATE_CONTEXT_ENV"}}]
        for result in [{}, *[{"execution_context": value} for value in invalid]]:
            with self.subTest(result=result):
                record = self.index([self.row(self.chain(result=result))])[0]["command_record"]
                self.assertNotIn("execution_context", record)
                self.assertNotIn("PRIVATE_CONTEXT_ENV", json.dumps(record))

    def test_missing_empty_malformed_and_oversized_argv_are_omitted_whole(self):
        legacy = self.f.chain(result={"status": "ok"})
        self.assert_omitted(legacy)
        for argv in (None, [], "python -c pass", ["python", 3], ["python", None],
                     {"0": "python"}, ["python", "\u4e2d" * 400]):
            with self.subTest(argv=argv):
                self.assert_omitted(self.chain(result={"argv": argv}))

    def test_invalid_or_oversized_request_is_never_rebuilt_from_argv(self):
        for arguments in ({}, {"command": ""}, {"command": "python", "args": "-c pass"},
                          {"command": "python", "args": [1]}, {"command": "python", "timeout": True},
                          {"command": "python", "artifact_seqs": [True]},
                          {"command": "python", "args": ["\u4e2d" * 400]}):
            with self.subTest(arguments=arguments):
                self.assert_omitted(self.chain(arguments=arguments))

    def test_request_and_argv_normalizer_byte_boundaries(self):
        # Exercise each normalizer limit with room for the other context fields.
        for request_size, argv_size in ((1024, 64), (1025, 64), (64, 1024), (64, 1025)):
            arguments = {"command": "python", "args": [""]}
            padding = request_size - len(json_utf8(arguments))
            arguments["args"][0] = "\u4e2d" * (padding // 3) + "x" * (padding % 3)
            padding = argv_size - len(json_utf8([""]))
            argv = ["\u4e2d" * (padding // 3) + "x" * (padding % 3)]
            self.assertEqual(len(json_utf8(arguments)), request_size)
            self.assertEqual(len(json_utf8(argv)), argv_size)
            chain = self.chain(arguments=arguments, result={"argv": argv, "cwd": None})
            with self.subTest(request_size=request_size, argv_size=argv_size):
                indexed = self.index([self.row(chain)])
                if request_size <= 1024 and argv_size <= 1024:
                    record = indexed[0]["command_record"]
                    self.assertEqual(record["request"], arguments)
                    self.assertEqual(record["argv"], argv)
                    self.assertLessEqual(len(json_utf8(record)), 2400)
                else:
                    self.assertNotIn("command_record", indexed[0])

    def test_three_authentication_attempts_preserve_rows_without_filling_failed_slot(self):
        chains = [self.chain() for _ in range(5)]
        invalid = self.chain(actors={"finish": Actor.agent(self.f.f.manager, "Manager")})
        legacy = self.f.chain(result={"status": "ok"})
        other = self.chain(start_changes={"name": "workspace_write"}, finish_changes={"name": "workspace_write"})
        rows = [self.row(chain) for chain in [*chains, invalid, legacy, other]]
        with patch("cater.delivery_commands.normalize", wraps=delivery_commands.normalize) as normalize:
            indexed = self.index(rows)
        self.assertEqual(normalize.call_count, 3)
        self.assert_preserved(rows, indexed)
        self.assertEqual([row["seq"] for row in indexed if "command_record" in row],
                         [chain[3].seq for chain in chains[-2:]])
        self.assertNotIn("command_record", indexed[-3])
        self.assertLessEqual(sum(len(json_utf8(row["command_record"])) for row in indexed
                                 if "command_record" in row), 2400)

    def test_twenty_old_revision_rows_are_filtered_before_normalize(self):
        chains = [self.chain() for _ in range(20)]
        self.f.emit("team.objective_updated", {"goal": "Only current objective commands"})
        rows = [self.row(chain) for chain in chains]
        events = self.log.read(task_id=self.task)
        with patch("cater.delivery_commands.normalize", wraps=delivery_commands.normalize) as normalize:
            indexed = self.index(rows, events=events)
        normalize.assert_not_called()
        self.assert_preserved(rows, indexed)
        self.assertEqual(indexed, rows)

    def test_twenty_missing_argv_rows_are_filtered_before_normalize(self):
        chains = [self.f.chain(result={"status": "ok"}) for _ in range(20)]
        rows = [self.row(chain) for chain in chains]
        events = self.log.read(task_id=self.task)
        with patch("cater.delivery_commands.normalize", wraps=delivery_commands.normalize) as normalize:
            indexed = self.index(rows, events=events)
        normalize.assert_not_called()
        self.assert_preserved(rows, indexed)
        self.assertEqual(indexed, rows)

    def test_twenty_oversize_rows_are_filtered_before_normalize(self):
        cases = {"request": {"arguments": {"command": "python", "args": ["\u4e2d" * 400]}},
                 "argv": {"result": {"argv": ["\u4e2d" * 400]}},
                 "whole_record": {"arguments": {"command": "python", "args": ["a" * 850]},
                                  "result": {"argv": ["b" * 850], "cwd": "c" * 850}}}
        for name, changes in cases.items():
            with self.subTest(size_limit=name):
                chains = [self.chain(**changes) for _ in range(20)]
                rows = [self.row(chain) for chain in chains]
                events = self.log.read(task_id=self.task)
                with patch("cater.delivery_commands.normalize", wraps=delivery_commands.normalize) as normalize:
                    indexed = self.index(rows, events=events)
                normalize.assert_not_called()
                self.assert_preserved(rows, indexed)
                self.assertEqual(indexed, rows)

    def test_twenty_untrusted_chains_use_at_most_three_authentications_and_never_pass(self):
        cases = {"pairing": {"finish_changes": {"call_id": "forged-call"}},
                 "actor": {"actors": {"finish": Actor.agent(self.f.f.manager, "Manager")}},
                 "duplicate_run": {"duplicate_run": "after"}, "duplicate_settlement": {}}
        for name, changes in cases.items():
            with self.subTest(source_failure=name):
                older_valid = self.chain()
                chains = [self.chain(**changes) for _ in range(20)]
                if name == "duplicate_settlement":
                    for chain in chains:
                        self.f.emit("team.tool_finished", chain[3].payload)
                rows = [self.row(chain) for chain in [older_valid, *chains]]
                events = self.log.read(task_id=self.task)
                with patch("cater.delivery_commands.normalize", wraps=delivery_commands.normalize) as normalize:
                    indexed = self.index(rows, events=events)
                self.assertEqual(normalize.call_count, 3)
                self.assertEqual([call.args[2] for call in normalize.call_args_list],
                                 [[chain[3].seq] for chain in reversed(chains[-3:])])
                self.assert_preserved(rows, indexed)
                self.assertEqual(indexed, rows)

    def test_supplied_events_avoid_log_read_and_empty_snapshot_does_not_fall_back(self):
        chain = self.chain()
        rows = [self.row(chain)]
        revision = self.team.view(self.task)["revision"]
        events = self.log.read(task_id=self.task)
        saved = copy.deepcopy(events)
        for snapshot in (events, []):
            with self.subTest(empty=not snapshot), \
                    patch.object(self.log, "read", side_effect=AssertionError("Snapshot already supplied")):
                indexed = delivery_commands.index_records(
                    self.team, self.task, rows, revision=revision, events=snapshot)
            self.assert_preserved(rows, indexed)
            if snapshot:
                self.assertEqual(indexed[0]["command_record"]["source_seq"], chain[3].seq)
            else:
                self.assertEqual(indexed, rows)
        self.assertEqual(events, saved)

    def test_team_passes_its_existing_event_snapshot_to_index_records(self):
        chain = self.chain()
        events = self.log.read(task_id=self.task)
        original = delivery_commands.index_records

        def project(*args, **kwargs):
            self.assertEqual(kwargs["events"], events)
            with patch.object(self.log, "read", side_effect=AssertionError("Do not reread within index_records")):
                return original(*args, **kwargs)

        with patch("cater.delivery_commands.index_records", side_effect=project) as projection:
            context = self.context(chain[0])
        projection.assert_called_once()
        self.assertEqual(context["receipt_index"][0]["command_record"]["source_seq"], chain[3].seq)

    def test_authentication_is_repeated_after_snapshot_changes(self):
        chain = self.chain()
        rows = [self.row(chain)]
        before = self.log.read(task_id=self.task)
        with patch("cater.delivery_commands.normalize", wraps=delivery_commands.normalize) as normalize:
            first = self.index(rows, events=before)
            self.assertEqual(first[0]["command_record"]["source_seq"], chain[3].seq)
            self.assertEqual(normalize.call_count, 1)
            self.f.emit("team.tool_finished", chain[3].payload)
            after = self.log.read(task_id=self.task)
            self.assertNotEqual(before, after)
            second = self.index(rows, events=after)
            self.assertEqual(normalize.call_count, 2)
        self.assertEqual(second, rows)
        self.assert_preserved(rows, second)

    def test_invalid_source_or_start_hash_is_filtered_before_serialization_and_authentication(self):
        chain = self.chain()
        rows = [self.row(chain)]
        events = self.log.read(task_id=self.task)
        for source in chain[2:]:
            for bad_hash in (None, 1, "\u4e2d" * 64, "\ud800"):
                snapshot = [replace(event, hash=bad_hash) if event.seq == source.seq else event for event in events]
                with self.subTest(seq=source.seq, hash=repr(bad_hash)), \
                        patch("cater.delivery_commands.normalize", wraps=delivery_commands.normalize) as normalize:
                    indexed = self.index(rows, events=snapshot)
                normalize.assert_not_called()
                self.assertEqual(indexed, rows)
        self.assertTrue(self.rt.verify_chain().ok)

    def test_total_budget_counts_utf8_bytes_across_records(self):
        chains = [self.chain(result={"argv": ["\u4e2d" * 100], "cwd": "\u4e2d" * 100}) for _ in range(3)]
        rows = [self.row(chain) for chain in chains]
        individual = [self.index([row])[0]["command_record"] for row in rows]
        self.assertGreater(sum(len(json_utf8(record)) for record in individual), 2400)
        self.assertLessEqual(sum(len(json.dumps(record, ensure_ascii=False)) for record in individual), 2400)
        with patch("cater.delivery_commands.normalize", wraps=delivery_commands.normalize) as normalize:
            indexed = self.index(rows)
        self.assertEqual(normalize.call_count, 2)
        selected = [row["command_record"] for row in indexed if "command_record" in row]
        self.assertEqual([record["source_seq"] for record in selected], [chain[3].seq for chain in chains[-2:]])
        self.assertEqual(selected, individual[-2:])
        self.assertLessEqual(sum(len(json_utf8(record)) for record in selected), 2400)
        self.assert_preserved(rows, indexed)

    def test_context_keeps_twenty_pointer_window_and_only_three_recent_records(self):
        chains = [self.chain() for _ in range(22)]
        context = self.context(chains[-1][0])
        rows = context["receipt_index"]
        self.assertIs(context["receipts_truncated"], True)
        self.assertEqual([row["seq"] for row in rows], [chain[3].seq for chain in chains[-20:]])
        self.assertEqual([row["seq"] for row in rows if "command_record" in row],
                         [chain[3].seq for chain in chains[-3:]])
        self.assertEqual([{k: v for k, v in row.items() if k != "command_record"} for row in rows],
                         [self.row(chain) for chain in chains[-20:]])
        self.assertLessEqual(sum(len(json_utf8(row["command_record"])) for row in rows
                                 if "command_record" in row), 2400)

    def test_single_over_budget_record_is_skipped_without_clipping_or_losing_pointer(self):
        older = self.chain()
        large = self.chain(arguments={"command": "python", "args": ["a" * 850]},
                           result={"argv": ["b" * 850], "cwd": "c" * 850})
        self.assert_omitted(large)
        rows = [self.row(older), self.row(large)]
        indexed = self.index(rows)
        self.assertEqual(indexed[1], rows[1])
        self.assertEqual(indexed[0]["command_record"]["source_seq"], older[3].seq)

    def test_wrong_actors_at_each_chain_link_never_gain_command_record(self):
        for stage in ("message", "run", "start", "finish"):
            for actor in (Actor.system("other"), Actor.human(), Actor.agent("removed-member", "Removed")):
                if stage == "message" and actor == Actor.human():
                    continue
                with self.subTest(stage=stage, actor=actor):
                    self.assert_omitted(self.chain(actors={stage: actor}))

    def test_forged_pairing_run_and_revision_links_are_rejected(self):
        cases = [{"finish_changes": {key: value}} for key, value in (
            ("started_seq", True), ("started_seq", 999999), ("started_seq", 1.0),
            ("call_id", "forged-call"), ("run_id", "forged-run"), ("role", self.f.f.worker))]
        cases += [{"start_changes": {key: value}} for key, value in (
            ("message_seq", True), ("message_seq", 999999), ("revision", True),
            ("revision", 999999), ("role", "removed-member"), ("call_id", ""))]
        cases += [{"message_changes": {"recipient": self.f.f.worker}},
                  {"run_changes": {"role": self.f.f.worker}}, {"early_end": True}]
        for changes in cases:
            with self.subTest(changes=changes):
                self.assert_omitted(self.chain(**changes))

    def test_non_workspace_tools_and_invalid_result_status_are_rejected(self):
        for name in ("workspace_write", "mcp__pretend_run", "read_receipts"):
            with self.subTest(name=name):
                self.assert_omitted(self.chain(start_changes={"name": name}, finish_changes={"name": name}))
        for status in (None, True, "passed"):
            with self.subTest(status=status):
                self.assert_omitted(self.chain(result={"status": status}))

    def test_cross_task_receipts_and_foreign_start_binding_are_rejected(self):
        other = self.rt.create_task("Disposable foreign command context")
        self.team.enable(other)
        foreign = self.chain(task=other)
        self.assert_omitted(foreign)
        self.assert_omitted(self.chain(finish_changes={"started_seq": foreign[2].seq}))
        local = self.chain()
        indexed = self.index([self.row(foreign), self.row(local)])
        self.assertNotIn("command_record", indexed[0])
        self.assertEqual(indexed[1]["command_record"]["source_seq"], local[3].seq)

    def test_duplicate_run_and_duplicate_settlement_remain_ambiguous(self):
        for timing in ("before", "after"):
            with self.subTest(timing=timing):
                self.assert_omitted(self.chain(duplicate_run=timing))
        chain = self.chain()
        duplicate = self.f.emit("team.tool_finished", {**chain[3].payload,
            "result": {"status": "error", "argv": ["other-python"]}})
        self.assert_omitted(chain)
        self.assert_omitted((*chain[:3], duplicate))

    def test_duplicate_and_nul_json_keys_reuse_strict_source_validation(self):
        chain = self.chain()
        for event in chain:
            key = "recipient" if event.type == "team.message_received" else "run_id"
            variants = [self.f.duplicate_json(event.payload, key, event.payload[key]),
                        json.dumps({**event.payload, key + "\0shadow": "ambiguous"})]
            for encoded in variants:
                with self.subTest(seq=event.seq, encoded=encoded), self.f.raw_json(event, "payload_json", encoded):
                    self.assert_omitted(chain)
            actor = json.loads(self.log.db.execute("SELECT actor_json FROM events WHERE seq = ?", (event.seq,)).fetchone()[0])
            with self.subTest(actor_seq=event.seq), self.f.raw_json(event, "actor_json",
                    self.f.duplicate_json(actor, "id", actor["id"])):
                self.assert_omitted(chain)

    def test_old_objective_omits_record_and_note_but_keeps_history_pointer(self):
        chain = self.chain()
        self.f.emit("team.objective_updated", {"goal": "A different current objective"})
        self.assert_omitted(chain)
        context = self.context(chain[0])
        row, = [row for row in context["receipt_index"] if row["seq"] == chain[3].seq]
        self.assertEqual(row, self.row(chain))
        self.assertNotIn("command_record_note", context)
        self.assertEqual(self.team.read_sources.exact(self.task, {"receipt_seq": chain[3].seq})[0]["result"],
                         chain[3].payload["result"])
        current = self.chain()
        record = self.index([self.row(current)])[0]["command_record"]
        self.assertEqual(record["recorded_revision"], self.team.view(self.task)["revision"])
        self.assertNotEqual(record["recorded_revision"], 0)

    def test_revision_parameter_is_strict(self):
        chain = self.chain()
        for revision in (None, True, -1, "0", 0.0, 999999):
            with self.subTest(revision=revision):
                self.assertEqual(self.index([self.row(chain)], revision=revision), [self.row(chain)])

    def recovered_chain(self, message):
        return self.chain(run_changes={"message_seq": message.seq})

    def test_authentic_resume_recovery_gains_record_but_forged_and_duplicate_do_not(self):
        self.f.emit("team.objective_updated", {"goal": "Recovery context objective"})
        source = self.team.receive(self.task, "Before pause", request_id="command-context-before-pause")
        claimed, _role, run = self.team._claim(self.task)
        self.assertEqual(claimed.seq, source.seq)
        self.f.emit("team.run_finished", {**run.payload, "status": "completed"})
        self.rt.execution_control.change(self.task, "pause", request_id="command-context-pause")
        resume, _changed = self.rt.execution_control.change(self.task, "resume", request_id="command-context-resume")
        notice = self.team.recovery.resume(self.task, resume.seq)
        self.assertIsNotNone(notice)
        self.assertTrue(self.team.recovery.is_message(notice))
        valid = self.recovered_chain(notice)
        self.assertIn("command_record", self.index([self.row(valid)])[0])
        self.assertIn("command_record_note", self.context(notice))
        forged = self.f.emit("team.message_received", {**notice.payload, "resume_hash": "0" * 64})
        self.assert_omitted(self.recovered_chain(forged))
        duplicate = self.f.emit("team.message_received", notice.payload)
        self.assert_omitted(self.recovered_chain(duplicate))
        self.assertIn("command_record", self.index([self.row(valid)])[0])

    def test_ordinary_interruption_recovery_keeps_source_validation(self):
        self.f.emit("team.objective_updated", {"goal": "Ordinary recovery context"})
        source = self.team.receive(self.task, "Interrupted input", request_id="command-context-interrupted")
        claimed, _role, run = self.team._claim(self.task)
        self.assertEqual(claimed.seq, source.seq)
        notice, = self.team.recovery.prepare(self.task)
        self.assertEqual(notice.payload["recovery_of"], run.seq)
        valid = self.recovered_chain(notice)
        self.assertIn("command_record", self.index([self.row(valid)])[0])
        duplicate = self.f.emit("team.message_received", notice.payload)
        self.assert_omitted(self.recovered_chain(duplicate))

    def test_empty_and_legacy_context_keep_old_field_set_without_note(self):
        message = self.f.emit("team.message_received", {"recipient": self.f.f.manager,
            "kind": "message", "text": "Empty context", "revision": 0}, actor=Actor.human())
        empty = self.context(message)
        self.assertEqual(empty["receipt_index"], [])
        self.assertNotIn("command_record_note", empty)
        self.assertEqual(self.index([]), [])
        legacy = self.f.chain(result={"status": "ok", "outcome": "passed", "exit_code": 0})
        context = self.context(legacy[0])
        self.assertEqual(context["receipt_index"], [self.row(legacy)])
        self.assertEqual(set(context), set(empty))
        self.assertNotIn("command_record_note", context)
        large = self.chain(arguments={"command": "python", "args": ["a" * 850]},
                           result={"argv": ["b" * 850], "cwd": "c" * 850})
        bounded = self.context(large[0])
        self.assertEqual(bounded["receipt_index"], [self.row(legacy), self.row(large)])
        self.assertEqual(set(bounded), set(empty))
        self.assertNotIn("command_record_note", bounded)

    def test_projection_and_snapshot_do_not_execute_emit_review_or_complete(self):
        chains = [self.chain(result=result) for result in (
            {"status": "ok", "outcome": "passed", "exit_code": 0},
            {"status": "error", "outcome": "failed", "exit_code": 7},
            {"status": "error", "outcome": "effects_unknown", "exit_code": None})]
        rows = [self.row(chain) for chain in chains]
        saved_rows = copy.deepcopy(rows)
        saved_events = self.log.read()
        saved_view = copy.deepcopy(self.team.view(self.task))
        saved_pending = copy.deepcopy(self.team._pending_operations(self.task))
        with patch.object(WorkspaceExecutor, "execute", side_effect=AssertionError("Context must not execute")), \
                patch.object(self.log, "emit", side_effect=AssertionError("Context must not persist events")):
            indexed = self.index(rows)
            context = self.context(chains[-1][0])
        self.assert_preserved(rows, indexed)
        self.assertEqual(rows, saved_rows)
        self.assertEqual(context["receipt_index"], indexed)
        note = context["command_record_note"]
        self.assertIsInstance(note, str)
        self.assertTrue(note.strip())
        for meaning in ("\u5386\u53f2", "argv", "\u9a8c\u6536", "\u8986\u76d6", "\u539f\u73af\u5883"):
            self.assertIn(meaning, note)
        for row, chain in zip(context["receipt_index"], chains):
            self.assertEqual(row["status"], chain[3].payload["result"]["status"])
            self.assertEqual(row["outcome"], chain[3].payload["result"]["outcome"])
            self.assertEqual(row["exit_code"], chain[3].payload["result"]["exit_code"])
        self.assertEqual(self.log.read(), saved_events)
        self.assertEqual(self.team.view(self.task), saved_view)
        self.assertEqual(self.team._pending_operations(self.task), saved_pending)
        self.assertEqual(self.f.events("team.objective_completed"), [])
        self.assertEqual(self.f.events("team.review_requested"), [])
        self.assertTrue(self.rt.verify_chain().ok)

    def test_omitted_item_note_and_read_receipts_only_return_completed_result(self):
        marker = "ORIGINAL_REQUEST_ONLY_SENTINEL"
        omitted = self.f.chain(arguments={"command": "python", "args": [marker]},
            result={"status": "ok", "stdout": "SAVED_COMPLETED_OUTPUT"})
        valid = self.chain()
        context = self.context(valid[0])
        row, = [row for row in context["receipt_index"] if row["seq"] == omitted[3].seq]
        self.assertNotIn("command_record", row)
        self.assertNotIn("read_args", row)
        note = context["command_record_note"]
        for meaning in ("seq", "receipt_seq", "read_receipts", "\u5b8c\u6210", "\u4e0d\u542b", "\u8bf7\u6c42"):
            self.assertIn(meaning, note)
        self.assertNotIn("read_args", note)
        self.assertNotIn(marker, json.dumps(context))
        response, = self.f.f.exchange(self.f.f.manager, ("read_receipts", {"receipt_seq": row["seq"]}))
        self.assertEqual(response["status"], "ok")
        receipt, = response["receipts"]
        self.assertEqual(receipt["seq"], omitted[3].seq)
        self.assertEqual(receipt["result"], omitted[3].payload["result"])
        self.assertNotIn("request", receipt)
        self.assertNotIn("arguments", receipt)
        self.assertNotIn(marker, json.dumps(response))
        self.assertFalse(response["record_read"]["reexecutes_operations"])

    def test_native_runtime_context_exact_read_and_old_receipt_pagination(self):
        artifact = self.f.archive()
        args = {"command": "python", "args": ["probe.py", "literal space", "a>b", "$HOME", "&&"],
                "cwd": "package", "artifact_seqs": [artifact], "timeout": 5}
        start, finish, result = self.f.run_command(args)
        self.assertEqual((result["status"], result["outcome"], result["exit_code"]), ("ok", "passed", 0))
        before_receipts = copy.deepcopy(self.team._receipts(self.task))
        before_events = self.log.read()
        context = self.context(self.f.event(start.payload["message_seq"]))
        row, = [row for row in context["receipt_index"] if row["seq"] == finish.seq]
        self.assertEqual(row["command_record"]["argv"], self.f.prefix + args["args"])
        self.assertEqual(row["command_record"]["request"], args)
        self.assertEqual(row["command_record"]["execution_context"], CONTEXT)
        self.assertEqual(row["command_record"]["environment"], "temporary_artifact_copy_not_sandbox")
        self.assertEqual(row["command_record"]["environment"], result["environment"])
        self.assertIs(row["command_record"]["complete"], True)
        self.assertIs(row["command_record"]["complete"], result["complete"])
        self.assertIn("command_record_note", context)
        self.assertEqual(self.log.read(), before_events)
        self.assertEqual(self.team._receipts(self.task), before_receipts)
        observed = []

        def discover(context, history, tools):
            reference, = [r for r in context["receipt_index"] if r["seq"] == finish.seq]
            observed.append(reference["command_record"])
            self.assertEqual(reference["command_record"], row["command_record"])
            return self.f.f.native_turn("", ("read_receipts", reference["command_record"]["read_args"]),
                ("read_receipts", {"name": "workspace_run", "limit": 1}))

        def inspect(context, history, tools):
            replies = [json.loads(message["content"]) for message in history if message.get("role") == "tool"]
            exact, page = replies[-2:]
            for response in (exact, page):
                self.assertEqual(response["status"], "ok")
                self.assertEqual([r["seq"] for r in response["receipts"]], [finish.seq])
                self.assertEqual(response["receipts"][0]["result"], result)
                self.assertNotIn("command_record", response["receipts"][0])
                self.assertFalse(response["has_more"])
                self.assertIsNone(response["next_before_seq"])
                self.assertFalse(response["record_read"]["reexecutes_operations"])
            output = json.loads(exact["receipts"][0]["result"]["stdout"]["excerpt"])
            self.assertEqual(output["args"], args["args"][1:])
            self.assertEqual(output["data"], "published-data")
            return self.f.f.native_turn("Historical command inspected.")

        self.f.f.driver.steps[self.f.f.manager] = [discover, inspect]
        self.team.receive(self.task, "Inspect the original command", request_id="command-context-native-read")
        self.f.f.run_team()
        self.assertEqual(len(observed), 1)
        self.assertGreater(self.f.f.validated_continuations, 0)
        runs = [event for event in self.f.events("team.tool_finished") if event.payload["name"] == "workspace_run"]
        self.assertEqual(runs, [finish])
        self.assertEqual(self.f.events("team.objective_completed"), [])
        self.assertEqual(self.f.events("team.review_requested"), [])
        self.assertTrue(self.rt.verify_chain().ok)


if __name__ == "__main__":
    unittest.main()
