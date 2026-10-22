"""Frozen offline reply-evidence contract, independent of production implementation.

Sources deliberately use real Human inputs, unlike lookup's SYSTEM default.
Only disposable fixture databases/files and scripted model drivers are used.
"""

from __future__ import annotations

import copy
from contextlib import contextmanager
from dataclasses import replace
import io
import json
import sqlite3
import unittest
from unittest.mock import patch

from cater import team_reply
from cater.events import Actor
from cater.team import HOST, LOCAL_EVENT_CONTRACT, LOCAL_EVENT_TOOLS, PAUSED_TOOLS
from cater.team_read_sources import bound_receipts
from cater.tool_protocol import ToolCall
from cater.verification import _Capture
from cater.work import WorkConflict
from cater.workspace_tools import WorkspaceExecutor
import test_team_external as external
import test_team_supervision as native


TEXT = "The receipt is available for inspection."
MISSING = object()


class ReplyFixture(unittest.TestCase):
    fixture_type = external.TeamExternalTests

    def setUp(self):
        self.f = self.fixture_type()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.rt, self.task = self.f.rt, self.f.task
        self.team, self.log = self.rt.team, self.rt.log
        self.manager, self.worker = self.f.manager, self.f.worker
        self.counter = 0

    def emit(self, kind, payload, *, task=None, actor=HOST):
        return self.log.emit(type=kind, task_id=task or self.task, actor=actor, payload=payload)

    def events(self, kind, task=None):
        return list(self.log.iter_events(task_id=task or self.task, event_types=(kind,)))

    def answers(self, task=None):
        return [e for e in self.events("msg.said", task) if e.payload.get("audience") == "customer"]

    def new_task(self):
        task = self.rt.create_task("Independent frozen reply evidence")
        self.team.enable(task)
        return task

    @staticmethod
    def capture(body):
        capture = _Capture()
        capture.drain(io.BytesIO(body if isinstance(body, bytes) else body.encode("utf-8")))
        return capture.summary(record_output=True)

    def result(self, text="RAW_RECEIPT_BODY", *, failed=False):
        return {"status": "error" if failed else "ok", "outcome": "failed" if failed else "passed",
                "exit_code": 1 if failed else 0, "complete": True, "truncated": False,
                "stdout": self.capture(text), "stderr": self.capture("")}

    def source(self, *, task=None, name="workspace_run", result=None, arguments=None, defect=None):
        task = task or self.task
        self.counter += 1
        role = self.team._role(self.worker)
        if defect == "system_input":
            message = self.emit("team.message_received", {"recipient": role.id,
                "text": "Deliberately invalid SYSTEM source", "kind": "assignment"},
                task=task, actor=Actor.system())
        else:
            message = self.team.receive(task, f"@{role.name} Inspect saved source {self.counter}.")
            self.assertEqual(message.actor, Actor.human())
        revision = self.team.view(task)["revision"]
        run_payload = {"run_id": f"evidence-source-{message.seq}", "role": role.id,
                       "message_seq": message.seq, "revision": revision,
                       "control_seq": self.rt.execution_control.view(task)["seq"]}
        run = self.emit("team.run_started", run_payload, task=task,
                        actor=role_actor(role) if defect == "agent_run" else HOST)
        if defect == "duplicate_run_before":
            self.emit("team.run_started", run_payload, task=task)
        start_payload = {**run_payload, "name": name, "call_id": f"evidence-call-{message.seq}",
                         "arguments": {} if arguments is None else arguments, "root": str(self.f.root)}
        if defect == "wrong_input":
            start_payload["message_seq"] = run.seq
        if defect == "bool_input":
            start_payload["message_seq"] = True
        if defect == "wrong_role":
            start_payload["role"] = self.manager
        if defect == "blank_call":
            start_payload["call_id"] = " "
        start = self.emit("team.tool_started", start_payload, task=task,
                          actor=role_actor(role) if defect == "agent_start" else HOST)

        def close():
            return self.emit("team.run_finished", {**run_payload, "status": "completed",
                             "reason": "Offline source fixture settled."}, task=task)

        if defect == "late":
            close()
        payload = {"started_seq": start.seq, "run_id": run_payload["run_id"], "role": role.id,
                   "name": name, "call_id": start_payload["call_id"],
                   "result": self.result() if result is None else result}
        if defect == "orphan":
            payload["started_seq"] = message.seq
        if defect == "bool_start":
            payload["started_seq"] = True
        if defect == "wrong_call":
            payload["call_id"] = "another-call"
        if defect == "wrong_name":
            payload["name"] = "workspace_read"
        if defect == "wrong_run":
            payload["run_id"] = "another-run"
        finish = self.emit("team.tool_finished", payload, task=task,
                           actor=role_actor(role) if defect == "agent_finish" else HOST)
        if defect != "late":
            close()
        if defect == "duplicate_run_after":
            self.emit("team.run_started", run_payload, task=task)
            close()
        if defect == "agent_run":
            # Consume the Human input without making the forged source run a HOST run.
            consumed = {**run_payload, "run_id": run_payload["run_id"] + "-consumed"}
            self.emit("team.run_started", consumed, task=task)
            self.emit("team.run_finished", {**consumed, "status": "completed"}, task=task)
        return start, finish

    def context(self, task=None, *, recipient=None):
        task = task or self.task
        role = self.team._role(recipient or self.manager)
        source = self.team.receive(task, f"@{role.name} Explain the saved receipts only.")
        claimed, actual_role, run = self.team._claim(task)
        self.assertEqual((claimed, actual_role), (source, role))
        return task, role, source, run

    def dispatch(self, context, evidence=MISSING, *, text=TEXT, arguments=None, on_started=None):
        task, role, source, run = context
        args = {"text": text} if arguments is None else dict(arguments)
        if evidence is not MISSING:
            args["evidence"] = evidence
        self.counter += 1
        return self.team._dispatch_tool(task, source, role, run, run.payload["revision"],
            ToolCall(f"reply-evidence-{self.counter}", "reply_to_customer", args),
            [team_reply.schema()], None, {}, {}, None, on_started=on_started)

    @staticmethod
    def ref(event, quote="RAW_RECEIPT_BODY"):
        return {"source_seq": event.seq, "quote": quote}

    def assert_rejected(self, context, evidence):
        before = self.answers(context[0])
        _start, result = self.dispatch(context, evidence)
        self.assertEqual(result["status"], "error", result)
        self.assertTrue(result.get("message"), result)
        self.assertNotEqual(result.get("outcome"), "effects_unknown", result)
        self.assertEqual(self.answers(context[0]), before, "Invalid evidence sent a customer message")
        return result

    def assert_evidence(self, context, start, result, refs, originals):
        self.assertEqual(result["status"], "ok", result)
        task, role, message, _run = context
        answer, = self.answers(task)
        self.assertEqual(answer.actor, role_actor(role))
        self.assertEqual(answer.payload["text"], TEXT)
        self.assertEqual(answer.payload["reply_to"], message.seq)
        self.assertEqual(answer.payload["reply_evidence_started_seq"], start.seq)
        self.assertNotIn("reply_evidence", answer.payload)
        normalized = result["reply_evidence"]
        self.assertEqual(set(normalized), {"version", "sources", "note"})
        self.assertIs(type(normalized["version"]), int)
        self.assertEqual(normalized["version"], 1)
        self.assertIsInstance(normalized["note"], str)
        self.assertTrue(normalized["note"].strip())
        self.assertLessEqual(len(json.dumps(normalized, ensure_ascii=False).encode("utf-8")), 24000)
        self.assertEqual(len(normalized["sources"]), len(refs))
        for saved, ref, original in zip(normalized["sources"], refs, originals):
            self.assertEqual(saved["source_seq"], ref["source_seq"])
            self.assertEqual(saved["source_hash"], original.hash)
            self.assertEqual(saved["quote"], ref["quote"])
            self.assertEqual(saved["kind"], "tool_receipt")
            self.assertEqual(saved["role"], original.payload["role"])
            self.assertEqual(saved["tool"], original.payload["name"])
            raw = original.payload["result"]
            if raw.get("check_kind") != "browser":
                self.assertEqual(saved["status"], raw["status"])
            expected = {key: raw[key] for key in ("exit_code", "complete", "truncated") if key in raw}
            captures = [raw[key] for key in ("stdout", "stderr") if isinstance(raw.get(key), dict)]
            if any(capture.get("complete") is False for capture in captures):
                expected["complete"] = False
            if (any(capture.get("truncated") is True for capture in captures)
                    or isinstance(raw.get("read_range"), dict) and raw["read_range"].get("content_complete") is False
                    or original.payload["name"] == "workspace_search" and any(
                        match.get("truncated") is True for match in raw.get("matches", []) if isinstance(match, dict))):
                expected["truncated"] = True
            for key, value in expected.items():
                self.assertEqual(saved[key], value)
                self.assertIs(type(saved[key]), type(value))
        receipt, = [e for e in self.events("team.tool_finished", task)
                    if e.payload.get("started_seq") == start.seq]
        self.assertEqual(receipt.actor, HOST)
        self.assertEqual(receipt.payload["result"], result)
        self.assertEqual(start.payload["execution_contract"], LOCAL_EVENT_CONTRACT)
        self.assertLess(start.seq, answer.seq)
        self.assertLess(answer.seq, receipt.seq)
        return answer, normalized


def role_actor(role):
    return Actor.agent(role.id, role.role)


class ReplyEvidenceContractTests(ReplyFixture):
    def test_schema_retains_text_and_only_adds_bounded_optional_evidence(self):
        schema = team_reply.schema()["input_schema"]
        self.assertEqual(schema["required"], ["text"])
        self.assertIs(schema["additionalProperties"], False)
        self.assertEqual(set(schema["properties"]), {"text", "evidence"})
        self.assertEqual(schema["properties"]["text"],
                         {"type": "string", "minLength": 1, "maxLength": 32000})
        evidence = schema["properties"]["evidence"]
        self.assertEqual(evidence["type"], "array")
        self.assertEqual(evidence["maxItems"], 8)
        item = evidence["items"]
        self.assertIs(item["additionalProperties"], False)
        self.assertEqual(set(item["required"]), {"source_seq", "quote"})
        self.assertEqual(set(item["properties"]), {"source_seq", "quote"})
        self.assertEqual(item["properties"]["source_seq"]["type"], "integer")
        self.assertEqual(item["properties"]["source_seq"]["minimum"], 1)
        self.assertEqual(item["properties"]["quote"]["type"], "string")
        self.assertEqual(item["properties"]["quote"]["maxLength"], 2000)
        for role_id in (self.manager, self.worker):
            offered = self.team._schemas(self.team._role(role_id), None)
            self.assertEqual([t for t in offered if t["name"] == "reply_to_customer"], [team_reply.schema()])
        self.assertIn("reply_to_customer", LOCAL_EVENT_TOOLS)
        self.assertIn("reply_to_customer", PAUSED_TOOLS)

    def test_fixture_has_human_input_and_strict_original_chain(self):
        _start, finish = self.source()
        before = self.log.read(task_id=self.task)
        self.assertIn(finish.seq, {e.seq for e, _s in bound_receipts(self.team, self.task, before)})
        self.assertEqual(self.log.read(task_id=self.task), before)
        self.assertTrue(self.rt.verify_chain().ok)

    def test_text_only_and_empty_evidence_keep_original_receipt_shape(self):
        for evidence in (MISSING, []):
            with self.subTest(empty=evidence is not MISSING):
                context = self.context(self.new_task())
                start, result = self.dispatch(context, evidence)
                answer, = self.answers(context[0])
                self.assertEqual(result, {"status": "ok", "message_seq": answer.seq,
                                          "reply_to": context[2].seq})
                self.assertNotIn("reply_evidence_started_seq", answer.payload)
                self.assertNotIn("reply_evidence", answer.payload)
                self.assertEqual(team_reply.committed_reply(self.team, context[3],
                                 self.log.read(task_id=context[0])), answer)
                self.assertEqual(start.payload["execution_contract"], LOCAL_EVENT_CONTRACT)

    def test_original_receipt_is_normalized_on_host_and_sources_remain_immutable(self):
        _source_start, original = self.source()
        before = self.log.read(task_id=self.task)
        refs = [self.ref(original)]
        frozen_refs = copy.deepcopy(refs)
        context = self.context()
        start, result = self.dispatch(context, refs)
        self.assert_evidence(context, start, result, refs, [original])
        self.assertEqual(refs, frozen_refs)
        self.assertEqual(self.log.read(task_id=self.task)[:len(before)], before)
        refs[0]["quote"] = "caller mutation"
        result["reply_evidence"]["sources"][0]["quote"] = "consumer mutation"
        saved = self.events("team.tool_finished")[-1].payload["result"]
        self.assertEqual(saved["reply_evidence"]["sources"][0]["quote"], frozen_refs[0]["quote"])

    def test_all_supported_tools_use_actual_receipt_fields(self):
        rows = {
            "workspace_read": ({"status": "ok", "path": "saved.txt", "content": "Saved exact body.",
                                "encoding": "utf-8", "truncated": False}, "Saved exact body."),
            "workspace_write": ({"status": "ok", "path": "written.txt", "sha256": "a" * 64,
                                 "bytes": 17, "created": True, "truncated": False}, "written.txt"),
            "workspace_list": ({"status": "ok", "path": "", "entries": [
                {"path": "listed.txt", "type": "file", "bytes": 17}], "truncated": False}, "listed.txt"),
            "workspace_search": ({"status": "ok", "path": "", "matches": [
                {"path": "saved.txt", "line": 1, "column": 1, "output": "Exact search hit.",
                 "sha256": "b" * 64, "bytes": 17, "truncated": False}],
                 "scanned_bytes": 17, "truncated": False}, "Exact search hit."),
            "workspace_run": (self.result(), "RAW_RECEIPT_BODY"),
            "read_artifact": ({"status": "ok", "artifact_seq": 1, "filename": "artifact.txt",
                               "content": "Exact artifact body."}, "Exact artifact body."),
            "browser_check": ({"status": "ok", "check_kind": "browser", "outcome": "passed",
                "scope_current": True, "artifacts_unchanged": True,
                "browser_report": {"outcome": "passed", "phase": "execution", "truncated": False,
                    "steps": [{"index": 1, "action": "assert_text", "status": "passed", "actual": True,
                               "observed": "Exact visible browser body."}]}}, "Exact visible browser body."),
        }
        for name, (raw, quote) in rows.items():
            with self.subTest(tool=name):
                task = self.new_task()
                _s, original = self.source(task=task, name=name, result=raw)
                context = self.context(task)
                refs = [self.ref(original, quote)]
                start, result = self.dispatch(context, refs)
                self.assert_evidence(context, start, result, refs, [original])

    def test_failed_program_with_ok_output_is_citable_without_upgrading_exit(self):
        raw = self.result("Ran 15 tests\nOK\n", failed=True)
        _s, original = self.source(result=raw)
        context = self.context()
        refs = [self.ref(original, "OK")]
        start, result = self.dispatch(context, refs)
        _answer, normalized = self.assert_evidence(context, start, result, refs, [original])
        saved, = normalized["sources"]
        self.assertEqual(saved["status"], "error")
        self.assertEqual(saved["exit_code"], 1)

    def test_invalid_types_limits_bool_and_extra_fields_are_correctable(self):
        _s, original = self.source()
        context = self.context()
        invalid = [None, True, False, {}, "receipt", 1, [None], [True], [[]], [{}],
                   [{"source_seq": original.seq}], [{"quote": "RAW_RECEIPT_BODY"}],
                   [{**self.ref(original), "status": "ok"}], [self.ref(original)] * 9]
        invalid += [[{"source_seq": value, "quote": "RAW_RECEIPT_BODY"}]
                    for value in (True, False, 0, -1, 1.5, str(original.seq), None, [], {})]
        invalid += [[self.ref(original, quote)] for quote in (None, True, 3, [], {}, "", " \n\t", "x" * 2001)]
        for evidence in invalid:
            with self.subTest(evidence=repr(evidence)[:120]):
                self.assert_rejected(context, evidence)
        refs = [self.ref(original)]
        start, result = self.dispatch(context, refs)
        self.assert_evidence(context, start, result, refs, [original])

    def test_evidence_does_not_make_text_optional_or_relax_text_validation(self):
        _s, original = self.source()
        context = self.context()
        for arguments in ({}, {"text": None}, {"text": True}, {"text": " "}, {"text": "x" * 32001},
                          {"text": TEXT, "unexpected": "field"}):
            with self.subTest(arguments=repr(arguments)[:80]):
                _start, result = self.dispatch(context, [self.ref(original)], arguments=arguments)
                self.assertEqual(result["status"], "error", result)
                self.assertEqual(self.answers(), [])

    def test_false_quote_is_rejected_and_exact_whitespace_is_preserved(self):
        _s, original = self.source(result=self.result("prefix  exact body \n suffix"))
        context = self.context()
        for quote in ("invented body", "EXACT BODY", "prefix exact body", "prefix...suffix"):
            with self.subTest(quote=quote):
                self.assert_rejected(context, [self.ref(original, quote)])
        refs = [self.ref(original, " exact body \n")]
        start, result = self.dispatch(context, refs)
        self.assert_evidence(context, start, result, refs, [original])

    def test_foreign_missing_and_future_sources_do_not_send_or_leak(self):
        foreign_task = self.new_task()
        _s, foreign = self.source(task=foreign_task, result=self.result("FOREIGN_PRIVATE_BODY"))
        context = self.context()
        for seq in (foreign.seq, self.log.count() + 10000):
            with self.subTest(seq=seq):
                result = self.assert_rejected(context, [{"source_seq": seq, "quote": "BODY"}])
                self.assertNotIn("FOREIGN_PRIVATE_BODY", json.dumps(result))

    def test_source_must_precede_reply_start_even_if_it_exists_at_execution(self):
        raw = self.result()
        task = self.task
        context = self.context(task)
        anticipated = self.log.count() + 3
        def append_source():
            source_start = self.emit("team.tool_started", {**context[3].payload, "name": "workspace_run",
                "call_id": "source-after-reply-start", "arguments": {}}, task=task)
            finished = self.emit("team.tool_finished", {"started_seq": source_start.seq,
                "run_id": context[3].payload["run_id"], "role": context[1].id,
                "name": "workspace_run", "result": raw}, task=task)
            self.assertEqual(finished.seq, anticipated)
        _start, result = self.dispatch(context, [{"source_seq": anticipated, "quote": "RAW_RECEIPT_BODY"}],
                                       on_started=append_source)
        self.assertEqual(result["status"], "error", result)
        self.assertEqual(self.answers(task), [])

    def test_orphan_late_duplicate_and_untrusted_chains_are_rejected(self):
        defects = ("orphan", "late", "duplicate_run_before", "duplicate_run_after", "system_input",
                   "agent_run", "agent_start", "agent_finish", "wrong_input", "bool_input", "wrong_role",
                   "blank_call", "bool_start", "wrong_call", "wrong_name", "wrong_run")
        for defect in defects:
            with self.subTest(defect=defect):
                task = self.new_task()
                _s, original = self.source(task=task, defect=defect)
                self.assert_rejected(self.context(task), [self.ref(original)])

    def test_messages_started_events_and_receipt_transcripts_are_not_original_sources(self):
        for name in ("read_receipts", "read_messages", "set_objective", "workspace_invented"):
            with self.subTest(tool=name):
                task = self.new_task()
                start, finish = self.source(task=task, name=name)
                context = self.context(task)
                self.assert_rejected(context, [self.ref(finish)])
                self.assert_rejected(context, [self.ref(start)])
                self.assert_rejected(context, [self.ref(context[2], "saved receipts")])

    def test_cross_stream_and_head_tail_stitching_is_rejected(self):
        raw = self.result("OUT_HEAD")
        raw["stderr"] = self.capture("ERR_TAIL")
        _s, original = self.source(result=raw)
        context = self.context()
        for quote in ("OUT_HEADERR_TAIL", "OUT_HEAD\nERR_TAIL", "OUT_HEAD...ERR_TAIL"):
            self.assert_rejected(context, [self.ref(original, quote)])
        task = self.new_task()
        raw = self.result()
        raw["stdout"] = self.capture("HEAD_ANCHOR" + "x" * 50000 + "TAIL_ANCHOR")
        raw["truncated"] = True
        self.assertEqual(raw["stdout"]["preview_mode"], "head_tail")
        _s, original = self.source(task=task, result=raw)
        context = self.context(task)
        for quote in ("HEAD_ANCHORTAIL_ANCHOR", "HEAD_ANCHOR...TAIL_ANCHOR", "x" * 2000 + "TAIL_ANCHOR"):
            self.assert_rejected(context, [self.ref(original, quote)])
        refs = [self.ref(original, "HEAD_ANCHOR"), self.ref(original, "TAIL_ANCHOR")]
        start, result = self.dispatch(context, refs)
        self.assert_evidence(context, start, result, refs, [original, original])

    def test_utf8_loss_and_truncation_metadata_survive_valid_visible_quote(self):
        raw = self.result()
        raw["stdout"] = self.capture(b"VISIBLE_ASCII\xff\xfe")
        raw["stderr"] = self.capture("stderr head" + "x" * 50000 + "stderr tail")
        raw["truncated"] = True
        _s, original = self.source(result=raw)
        context = self.context()
        refs = [self.ref(original, "VISIBLE_ASCII")]
        start, result = self.dispatch(context, refs)
        _answer, normalized = self.assert_evidence(context, start, result, refs, [original])
        self.assertEqual(normalized["sources"][0]["output_utf8_valid"], {"stdout": False, "stderr": True})

    def test_empty_receipt_has_no_quotable_body_and_outcomes_remain_literal(self):
        _s, empty = self.source(result={"status": "ok", "stdout": "", "stderr": ""})
        context = self.context()
        self.assert_rejected(context, [self.ref(empty, "ok")])
        for outcome in ("passed", "failed", "timeout", "launch_error", "effects_unknown", "not_started"):
            with self.subTest(outcome=outcome):
                task = self.new_task()
                raw = self.result(f"literal {outcome}", failed=outcome != "passed")
                raw["outcome"] = outcome
                _s, original = self.source(task=task, result=raw)
                context = self.context(task)
                refs = [self.ref(original, f"literal {outcome}")]
                start, result = self.dispatch(context, refs)
                _answer, normalized = self.assert_evidence(context, start, result, refs, [original])
                self.assertEqual(normalized["sources"][0]["outcome"], outcome)

    def test_eight_max_length_ascii_quotes_fit_and_ninth_is_rejected(self):
        originals = [self.source(result=self.result(str(i) + "a" * 1999))[1] for i in range(8)]
        context = self.context()
        refs = [self.ref(e, str(i) + "a" * 1999) for i, e in enumerate(originals)]
        self.assert_rejected(context, refs + [refs[0]])
        start, result = self.dispatch(context, refs)
        self.assert_evidence(context, start, result, refs, originals)

    def test_utf8_24000_byte_cap_is_atomic_and_does_not_drop_sources(self):
        quote = "\u4e2d" * 1999
        originals = [self.source(result=self.result(str(i) + quote))[1] for i in range(8)]
        context = self.context()
        refs = [self.ref(e, str(i) + quote) for i, e in enumerate(originals)]
        self.assertGreater(len(json.dumps(refs, ensure_ascii=False).encode("utf-8")), 24000)
        self.assert_rejected(context, refs)
        start, result = self.dispatch(context, refs[:1])
        self.assert_evidence(context, start, result, refs[:1], originals[:1])

    def test_identical_references_deduplicate_without_changing_quote(self):
        _s, original = self.source()
        context = self.context()
        ref = self.ref(original)
        start, result = self.dispatch(context, [ref, copy.deepcopy(ref)])
        self.assert_evidence(context, start, result, [ref], [original])

    def test_same_run_retry_cannot_change_remove_or_add_sources_to_sent_message(self):
        one, two = [self.source()[1] for _ in range(2)]
        context = self.context()
        refs = [self.ref(one)]
        start, result = self.dispatch(context, refs)
        self.assert_evidence(context, start, result, refs, [one])
        before = self.answers()
        _retry, same = self.dispatch(context, refs)
        self.assertEqual(same, result)
        for changed in ([self.ref(two)], [], [self.ref(one), self.ref(two)], MISSING):
            _retry, rejected = self.dispatch(context, changed)
            self.assertEqual(rejected["status"], "error", rejected)
            self.assertEqual(self.answers(), before)

    def test_text_only_message_cannot_gain_sources_on_retry(self):
        _s, original = self.source()
        context = self.context()
        _start, result = self.dispatch(context)
        self.assertEqual(result["status"], "ok", result)
        self.assert_rejected(context, [self.ref(original)])

    def test_new_input_revision_control_and_open_delegation_still_restrict_reply(self):
        for change in ("human", "revision", "control", "delegation"):
            with self.subTest(change=change):
                task = self.new_task()
                _s, original = self.source(task=task)
                recipient = self.worker if change == "delegation" else self.manager
                if change == "delegation":
                    assignment = self.team._send(task, self.team._role(self.manager), self.worker,
                                                 "Return the open assignment to its sender.", "assignment")
                    self.emit("team.questions_requested", {"role_id": self.worker,
                              "assignment_seq": assignment.seq}, task=task)
                context = self.context(task, recipient=recipient)
                args = {"text": TEXT, "evidence": [self.ref(original)]}
                start = self.emit("team.tool_started", {**context[3].payload,
                    "name": "reply_to_customer", "call_id": "restricted-reply", "arguments": args,
                    "execution_contract": dict(LOCAL_EVENT_CONTRACT)}, task=task)
                if change == "human":
                    self.team.receive(task, "A newer customer instruction.")
                elif change == "revision":
                    self.emit("team.objective_updated", {"goal": "New adopted objective."}, task=task)
                elif change == "control":
                    self.rt.execution_control.change(task, "pause", request_id="evidence-restrict-pause")
                    self.rt.execution_control.change(task, "resume", request_id="evidence-restrict-resume")
                before = self.log.read(task_id=task)
                with self.assertRaises((WorkConflict, ValueError)):
                    self.team._execute(task, context[1], "reply_to_customer", args, None, {}, start, None)
                self.assertEqual(self.log.read(task_id=task), before)
                self.assertEqual(self.answers(task), [])

    def test_old_objective_receipt_is_historical_read_only_evidence(self):
        _s, original = self.source()
        self.emit("team.objective_updated", {"goal": "A separately adopted current goal."})
        context = self.context()
        before = copy.deepcopy(self.team.view(self.task))
        refs = [self.ref(original)]
        start, result = self.dispatch(context, refs)
        self.assert_evidence(context, start, result, refs, [original])
        after = self.team.view(self.task)
        self.assertEqual((after["objective"], after["revision"]), (before["objective"], before["revision"]))
        self.assertEqual(self.events("team.objective_completed"), [])
        self.assertEqual(self.rt.task(self.task).deliverables, [])


class ReplyEvidenceRecoveryTests(ReplyFixture):
    @contextmanager
    def persisted_snapshot(self, events):
        # Exact source lookup reads SQLite; make the supplied proof and its source agree.
        original = self.log.db
        shadow = sqlite3.connect(":memory:")
        shadow.row_factory = sqlite3.Row
        schema = original.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='events'").fetchone()[0]
        shadow.execute(schema)
        cursor = original.execute("SELECT * FROM events")
        shadow.executemany("INSERT INTO events VALUES (" + ",".join("?" for _ in cursor.description) + ")", cursor)
        try:
            for event in events:
                shadow.execute("UPDATE events SET payload_json = ?, actor_json = ? WHERE seq = ?",
                    (json.dumps(event.payload, ensure_ascii=False), json.dumps(event.actor.to_dict()), event.seq))
            shadow.commit()
            self.log.db = shadow
            yield
        finally:
            self.log.db = original
            shadow.close()

    def committed(self, context, events=None):
        before = self.log.read(task_id=context[0])
        result = team_reply.committed_reply(self.team, context[3], before if events is None else events)
        self.assertEqual(self.log.read(task_id=context[0]), before, "Recovery proof lookup wrote events")
        return result

    def candidate(self):
        _s, original = self.source()
        context = self.context()
        refs = [self.ref(original)]
        start, result = self.dispatch(context, refs)
        answer, normalized = self.assert_evidence(context, start, result, refs, [original])
        receipt = self.events("team.tool_finished")[-1]
        return context, start, answer, receipt, original, normalized

    def test_committed_evidence_recovers_once_without_any_model_work(self):
        context, _s, answer, _r, _original, _norm = self.candidate()
        self.assertEqual(self.committed(context), answer)
        with patch.object(self.f.driver, "agent_run", side_effect=AssertionError("No model replay")) as model:
            self.assertEqual(self.team.recovery.prepare(self.task), [])
            after = self.log.read(task_id=self.task)
            self.assertEqual(self.team.recovery.prepare(self.task), [])
            self.assertEqual(self.log.read(task_id=self.task), after)
            model.assert_not_called()
        recovered, = [e for e in self.events("team.run_finished")
                        if e.payload["run_id"] == context[3].payload["run_id"]]
        self.assertEqual(recovered.payload["status"], "completed")
        self.assertEqual(recovered.payload["recovered_customer_reply_seq"], answer.seq)
        self.assertEqual(self.answers(), [answer])
        self.assertEqual(self.team._pending_operations(self.task), [])

    def test_recovery_rejects_tampered_request_message_result_and_source(self):
        context, start, answer, receipt, original, _norm = self.candidate()
        events = self.log.read(task_id=self.task)
        cases = []

        def change(event, payload):
            return [replace(e, payload=payload) if e.seq == event.seq else e for e in events]

        cases.append(("missing_marker", change(answer, {k: v for k, v in answer.payload.items()
                                                       if k != "reply_evidence_started_seq"})))
        for marker in (True, original.seq, receipt.seq):
            cases.append((f"marker_{marker}", change(answer, {**answer.payload, "reply_evidence_started_seq": marker})))
        cases.append(("source_body", change(original, {**original.payload, "result": self.result("OTHER_BODY")})))
        cases.append(("request_quote", change(start, {**start.payload, "arguments": {
            "text": TEXT, "evidence": [self.ref(original, "OTHER_BODY")]}})))
        cases.append(("no_request_evidence", change(start, {**start.payload, "arguments": {"text": TEXT}})))
        for key, value in (("version", True), ("version", 2), ("sources", []), ("note", None)):
            result = copy.deepcopy(receipt.payload["result"])
            result["reply_evidence"][key] = value
            cases.append((f"normalized_{key}_{value}", change(receipt, {**receipt.payload, "result": result})))
        for key, value in (("source_hash", "0" * 64), ("source_seq", True), ("quote", "invented"),
                           ("role", self.manager), ("tool", "workspace_read"), ("status", "error"),
                           ("exit_code", 1), ("complete", False), ("kind", "artifact")):
            result = copy.deepcopy(receipt.payload["result"])
            result["reply_evidence"]["sources"][0][key] = value
            cases.append(("source_" + key, change(receipt, {**receipt.payload, "result": result})))
        contract = {**LOCAL_EVENT_CONTRACT, "receipt": "separate_transaction"}
        cases.append(("contract", change(start, {**start.payload, "execution_contract": contract})))
        cases.append(("nonhost_result", [replace(e, actor=Actor.agent(self.manager, "leader"))
                                         if e.seq == receipt.seq else e for e in events]))
        cases.append(("nonhost_source", [replace(e, actor=Actor.agent(self.worker, "worker"))
                                         if e.seq == original.seq else e for e in events]))
        for label, candidate in cases:
            with self.subTest(defect=label), self.persisted_snapshot(candidate):
                self.assertIsNone(self.committed(context, candidate))
        self.assertEqual(self.committed(context), answer)

    def test_unpaired_answer_and_evidence_marker_cannot_suppress_normal_recovery(self):
        context, _s, answer, receipt, _original, _norm = self.candidate()
        events = self.log.read(task_id=self.task)
        self.assertIsNone(self.committed(context, [e for e in events if e.seq != receipt.seq]))
        self.assertIsNone(self.committed(context, [e for e in events if e.seq != answer.seq]))

    def test_invalid_citation_without_answer_keeps_normal_recovery_and_never_replays_model(self):
        _s, original = self.source()
        context = self.context()
        self.assert_rejected(context, [self.ref(original, "invented quote")])
        self.assertIsNone(self.committed(context))
        with patch.object(self.f.driver, "agent_run", side_effect=AssertionError("No model replay")) as model:
            queued = self.team.recovery.prepare(self.task)
            self.assertEqual(len(queued), 1)
            self.assertEqual(queued[0].payload["recovery_of"], context[3].seq)
            after = self.log.read(task_id=self.task)
            self.assertEqual(self.team.recovery.prepare(self.task), queued)
            self.assertEqual(self.log.read(task_id=self.task), after)
            model.assert_not_called()
        finished = self.events("team.run_finished")[-1]
        self.assertEqual(finished.payload["status"], "interrupted")
        self.assertNotIn("recovered_customer_reply_seq", finished.payload)
        self.assertEqual(self.answers(), [])

    def test_source_that_becomes_orphaned_cannot_suppress_recovery(self):
        context, _s, answer, _receipt, original, _norm = self.candidate()
        events = self.log.read(task_id=self.task)
        orphan = replace(original, payload={**original.payload, "started_seq": context[2].seq})
        candidate = [orphan if e.seq == original.seq else e for e in events]
        with self.persisted_snapshot(candidate):
            self.assertIsNone(self.committed(context, candidate))
        self.assertEqual(self.committed(context), answer)

    def test_text_only_receipt_cannot_smuggle_unrequested_evidence(self):
        context = self.context()
        _start, result = self.dispatch(context)
        self.assertEqual(result["status"], "ok", result)
        events = self.log.read(task_id=self.task)
        receipt = self.events("team.tool_finished")[-1]
        forged = {**receipt.payload, "result": {**receipt.payload["result"], "reply_evidence": {
            "version": 1, "sources": [{"source_seq": 99999, "quote": "forged"}], "note": "untrusted"}}}
        self.assertIsNone(self.committed(context, [replace(e, payload=forged)
                          if e.seq == receipt.seq else e for e in events]))

    def test_atomic_receipt_failure_rolls_back_message_and_retry_commits_once(self):
        _s, original = self.source()
        context = self.context()
        emit = self.log.emit
        observed = []

        def fail_receipt(**kwargs):
            if (kwargs["type"] == "team.tool_finished" and kwargs["payload"]["name"] == "reply_to_customer"
                    and kwargs["payload"]["result"].get("status") == "ok"):
                self.assertTrue(self.log.db.in_transaction)
                observed.extend(self.answers())
                raise OSError("Frozen offline host receipt failure")
            return emit(**kwargs)

        with patch.object(self.log, "emit", side_effect=fail_receipt):
            _start, failed = self.dispatch(context, [self.ref(original)])
        self.assertEqual(failed["status"], "error", failed)
        self.assertEqual(len(observed), 1)
        self.assertEqual(self.answers(), [])
        self.assertIsNone(self.committed(context))
        self.assertEqual([e.payload["status"] for e in self.events("team.run_finished")], ["completed"])
        start, result = self.dispatch(context, [self.ref(original)])
        answer, _norm = self.assert_evidence(context, start, result, [self.ref(original)], [original])
        self.assertEqual(self.committed(context), answer)


class ReplyEvidenceExternalTests(ReplyFixture):
    def test_external_invalid_quote_then_valid_reply_yields_and_skips_later_calls(self):
        _s, original = self.source()
        refs = [self.ref(original)]

        def script(run):
            invalid = run.call("reply_to_customer", text=TEXT, evidence=[self.ref(original, "false quote")])
            self.assertEqual(invalid["status"], "error", invalid)
            self.assertEqual(self.answers(), [])
            self.assertFalse(run.revoked)
            run.call("reply_to_customer", text=TEXT, evidence=refs)
            self.fail("Successful evidence reply did not yield immediately")

        self.f.driver.scripts = {self.manager: [script]}
        message = self.f.receive("Explain from the original source.")
        self.rt.collaborate(self.task)
        self.assertEqual(self.f.driver.errors, [])
        self.assertEqual(self.f.driver.scripts[self.manager], [])
        self.assertTrue(self.rt.verify_chain().ok)
        self.assertEqual(len(self.f.driver.runs), 1)
        invocation, = self.f.driver.runs
        self.assertEqual(invocation.state.phase, "yielded")
        self.assertTrue(invocation.revoked)
        host_external, = self.events("team.external_run_started")
        for start in self.events("team.tool_started"):
            if start.payload["name"] == "reply_to_customer":
                self.assertEqual(start.payload["external_started_seq"], host_external.seq)
                self.assertLess(host_external.seq, start.seq)
                for key in ("run_id", "role", "message_seq"):
                    self.assertEqual(start.payload[key], host_external.payload[key])
        self.assertEqual([r["status"] for r in self.f.results("reply_to_customer")], ["error", "ok"])
        answer, = self.answers()
        self.assertEqual(answer.payload["reply_to"], message.seq)
        self.assertEqual(self.events("team.run_finished")[-1].payload["status"], "completed")
        self.assertEqual(self.events("team.objective_completed"), [])

    def test_external_crash_after_settlement_recovers_and_keeps_cleanup_unknown(self):
        _s, original = self.source(result=self.result("OK", failed=True))

        def script(run):
            run.cleanup_complete = False
            run.call("reply_to_customer", text=TEXT, evidence=[self.ref(original, "OK")])
            self.fail("Reply did not yield")

        self.f.driver.scripts = {self.manager: [script]}
        self.f.receive("Explain the failed command's saved output.")
        with patch.object(self.team, "_finish_message", side_effect=SystemExit("settled before crash")):
            with self.assertRaises(SystemExit):
                self.rt.collaborate(self.task)
        self.assertEqual(self.f.driver.errors, [])
        answer, = self.answers()
        pending, = self.team.external_runs.pending(self.task)
        self.assertEqual(pending["outcome"], "unknown")
        self.assertFalse(pending["replay_authorized"])
        with patch.object(self.f.driver, "agent_run", side_effect=AssertionError("No duplicate model")) as model:
            self.assertEqual(self.team.recovery.prepare(self.task), [])
            after = self.log.read(task_id=self.task)
            self.assertEqual(self.team.recovery.prepare(self.task), [])
            self.assertEqual(self.log.read(task_id=self.task), after)
            model.assert_not_called()
        held, = self.team.external_runs.pending(self.task)
        self.assertEqual(held["outcome"], "unknown")
        self.assertFalse(held["replay_authorized"])
        self.assertTrue(held["run_ended"])
        self.assertEqual(self.answers(), [answer])
        self.assertEqual(self.events("team.run_finished")[-1].payload["recovered_customer_reply_seq"], answer.seq)


class ReplyEvidenceNativeTests(ReplyFixture):
    fixture_type = native.TeamSupervisionTests

    def workspace(self):
        self.f.bind_workspace()
        binding = self.rt.workspaces.view(self.task)
        return WorkspaceExecutor(self.rt.workspaces.config[binding["workspace_id"]]["policy"])

    def test_actual_executor_write_list_and_search_fields_are_separate_sources(self):
        executor = self.workspace()
        write_args = {"path": "saved/first.txt", "content": "ACTUAL_SEARCH_BODY\n", "expected_sha256": None}
        written = executor.execute("workspace_write", write_args)
        self.assertEqual(written["status"], "ok", written)
        second = executor.execute("workspace_write", {**write_args, "path": "saved/second.txt"})
        self.assertEqual(second["status"], "ok", second)
        listed = executor.execute("workspace_list", {"path": "saved"})
        searched = executor.execute("workspace_search", {"path": "saved", "query": "ACTUAL_SEARCH_BODY"})
        self.assertEqual(listed["status"], "ok", listed)
        self.assertEqual(searched["status"], "ok", searched)
        first_entry, second_entry = listed["entries"]
        first_match = searched["matches"][0]
        checks = [
            ("workspace_write", written, write_args, [written["path"], written["sha256"]],
             [write_args["content"], written["path"] + written["sha256"]]),
            ("workspace_list", listed, {"path": "saved"}, [listed["path"], first_entry["path"], second_entry["path"]],
             [first_entry["path"] + second_entry["path"]]),
            ("workspace_search", searched, {"path": "saved", "query": "ACTUAL_SEARCH_BODY"},
             [first_match["path"], first_match["output"], first_match["sha256"]],
             [first_match["path"] + first_match["output"], first_match["output"] + first_match["sha256"]]),
        ]
        for name, raw, arguments, quotes, invalid in checks:
            with self.subTest(tool=name):
                task = self.new_task()
                _s, original = self.source(task=task, name=name, result=raw, arguments=arguments)
                context = self.context(task)
                for quote in invalid:
                    self.assert_rejected(context, [self.ref(original, quote)])
                refs = [self.ref(original, quote) for quote in quotes]
                start, result = self.dispatch(context, refs)
                self.assert_evidence(context, start, result, refs, [original] * len(refs))

    def test_actual_search_query_and_unreturned_middle_are_not_observations(self):
        executor = self.workspace()
        written = executor.execute("workspace_write", {"path": "long.txt", "content":
            "HEAD_ONLY" + "a" * 400 + "ACTUAL_HIT" + "b" * 400 + "UNRETURNED_MIDDLE_END",
            "expected_sha256": None})
        self.assertEqual(written["status"], "ok", written)
        searched = executor.execute("workspace_search", {"query": "ACTUAL_HIT"})
        self.assertEqual(searched["status"], "ok", searched)
        self.assertIs(searched["truncated"], False)
        match, = searched["matches"]
        self.assertIs(match["truncated"], True)
        _s, original = self.source(name="workspace_search", result=searched, arguments={"query": "ACTUAL_HIT"})
        context = self.context()
        self.assert_rejected(context, [self.ref(original, "UNRETURNED_MIDDLE_END")])
        refs = [self.ref(original, match["output"])]
        start, result = self.dispatch(context, refs)
        _answer, normalized = self.assert_evidence(context, start, result, refs, [original])
        self.assertIs(normalized["sources"][0]["truncated"], True)
        task = self.new_task()
        query = "QUERY_WAS_NOT_OBSERVED"
        empty = executor.execute("workspace_search", {"query": query})
        self.assertEqual(empty["status"], "ok", empty)
        self.assertEqual(empty["matches"], [])
        _s, original = self.source(task=task, name="workspace_search", result=empty, arguments={"query": query})
        self.assert_rejected(self.context(task), [self.ref(original, query)])

    def test_actual_read_artifact_partial_and_tail_pages_remain_truncated(self):
        for offset in (0, 9):
            with self.subTest(offset=offset):
                task = self.new_task()
                context = self.context(task)
                _task, role, message, run = context

                def tool(name, arguments):
                    self.counter += 1
                    return self.team._dispatch_tool(task, message, role, run, run.payload["revision"],
                        ToolCall(f"actual-artifact-{self.counter}", name, arguments),
                        self.team._schemas(role, None), None, {}, {}, None)

                _start, published = tool("publish_artifact", {"filename": "paged.txt", "format": "text",
                    "summary": "Actual paged artifact fixture", "content": "HEAD_BODYTAIL_BODY"})
                self.assertEqual(published["status"], "ok", published)
                read_start, read_result = tool("read_artifact", {"artifact_seq": published["artifact_seq"],
                                                               "offset": offset, "length": 9})
                self.assertEqual(read_result["status"], "ok", read_result)
                self.assertIs(read_result["read_range"]["content_complete"], False)
                original, = [e for e in self.events("team.tool_finished", task)
                             if e.payload["started_seq"] == read_start.seq]
                self.assert_rejected(context, [self.ref(original, "HEAD_BODYTAIL_BODY")])
                refs = [self.ref(original, read_result["content"])]
                start, result = self.dispatch(context, refs)
                _answer, normalized = self.assert_evidence(context, start, result, refs, [original])
                self.assertIs(normalized["sources"][0]["truncated"], True)

    def test_native_evidence_reply_preserves_internal_progress_and_stops_model(self):
        _s, original = self.source()
        refs = [self.ref(original)]
        self.f.driver.steps[self.manager] = [
            self.f.native_turn("Checking exact receipts.", ("reply_to_customer", {"text": TEXT, "evidence": refs}),
                               ("publish_artifact", {"filename": "must-not-exist.txt", "content": "late",
                                                     "summary": "late", "format": "text"})),
            AssertionError("Successful evidence reply requested another model turn")]
        message = self.team.receive(self.task, "Explain only; keep the current goal.")
        self.f.run_team()
        self.assertEqual(len(self.f.driver.calls), 1)
        self.assertEqual(len(self.f.driver.steps[self.manager]), 1)
        customer, = self.answers()
        start, = [e for e in self.events("team.tool_started") if e.payload["name"] == "reply_to_customer"]
        result = self.f.results("reply_to_customer")[-1]
        run = next(e for e in self.events("team.run_started") if e.payload["message_seq"] == message.seq)
        self.assert_evidence((self.task, self.team._role(self.manager), message, run), start, result, refs, [original])
        progress, = [e for e in self.events("msg.said") if e.payload.get("response_kind") == "progress"]
        self.assertEqual(progress.payload["audience"], "team")
        self.assertNotIn("reply_evidence_started_seq", progress.payload)
        self.assertLess(progress.seq, start.seq)
        self.assertEqual(customer.payload["text"], TEXT)
        self.assertEqual(self.rt.task(self.task).deliverables, [])

    def test_native_invalid_evidence_can_be_corrected_in_same_exchange(self):
        _s, original = self.source()

        def correct(context, history, tools):
            result = json.loads(history[-1]["content"])
            self.assertEqual(result["status"], "error", result)
            self.assertEqual(self.answers(), [])
            return self.f.native_turn("", ("reply_to_customer", {"text": TEXT, "evidence": [self.ref(original)]}))

        self.f.driver.steps[self.manager] = [
            self.f.native_turn("", ("reply_to_customer", {"text": TEXT, "evidence": [self.ref(original, "invented")]})),
            correct, AssertionError("No model acknowledgment after corrected reply")]
        self.team.receive(self.task, "Correct the citation, then answer.")
        self.f.run_team()
        self.assertEqual(len(self.f.driver.calls), 2)
        self.assertGreater(self.f.validated_continuations, 0)
        self.assertEqual([r["status"] for r in self.f.results("reply_to_customer")], ["error", "ok"])
        self.assertEqual(len(self.answers()), 1)

    def test_rebound_and_paused_historical_receipts_do_not_authorize_writes(self):
        self.f.bind_workspace()
        _s, original = self.source()
        binding = self.rt.workspaces.view(self.task)
        self.rt.workspaces.bind(self.task, None, expected_revision=binding["revision"])
        self.rt.execution_control.change(self.task, "pause", request_id="evidence-history-pause")
        context = self.context()
        _context, _workspace, _binding, tools, _basis = self.team._model_input(
            self.task, context[2], context[1], run_id=context[3].payload["run_id"])
        self.assertIn("reply_to_customer", {t["name"] for t in tools})
        self.assertNotIn("workspace_write", {t["name"] for t in tools})
        before = self.log.read(task_id=self.task)
        refs = [self.ref(original)]
        start, result = self.dispatch(context, refs)
        self.assert_evidence(context, start, result, refs, [original])
        self.assertEqual(self.log.read(task_id=self.task)[:len(before)], before)
        self.assertEqual(self.rt.execution_control.view(self.task)["state"], "paused")
        self.assertFalse(self.rt.workspaces.view(self.task)["available"])
        self.assertEqual(self.events("workspace.tool_started"), [])


if __name__ == "__main__":
    unittest.main()
