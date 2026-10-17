"""Independent public-API regressions against the pre-projection rule fold."""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cater.events import Actor, EventLog
from cater.rules import RULE_EVENTS, RuleBook, RuleConflict


class LegacyRuleBook(RuleBook):
    """Keep the original reads independent of the new projection helpers."""

    def task_projects(self):
        result = {}
        for event in self.log.iter_events(event_types=("task.created", "task.project_bound")):
            if event.task_id:
                result[event.task_id] = event.payload.get("project_id")
        return result

    def task_project(self, task_id):
        projects = self.task_projects()
        if not isinstance(task_id, str) or task_id not in projects:
            raise ValueError("unknown task")
        return projects[task_id]

    def all_rules(self):
        current = {}
        for event in self.log.iter_events(event_types=(*RULE_EVENTS, "task.created")):
            if event.type == "task.created":
                if event.actor == Actor.human():
                    for rule in event.payload.get("inherited_rules", []):
                        current[rule["id"]] = {**rule, "seq": event.seq, "updated_at": event.ts}
            else:
                current[event.payload["id"]] = {
                    **event.payload, "seq": event.seq, "updated_at": event.ts,
                }
        return list(current.values())


class RuleProjectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "projection.db"
        self.log = self.open_log()
        self.book = RuleBook(self.log)
        self.legacy = LegacyRuleBook(self.log)
        self.task = "task-main"
        self.created(self.task)

    def open_log(self):
        log = EventLog(self.path)
        self.addCleanup(log.close)
        return log

    def created(self, task_id, payload=None, actor=None):
        return self.log.emit(type="task.created", actor=actor or Actor.human(),
                             task_id=task_id, payload={} if payload is None else payload)

    def rule(self, rule_id, **changes):
        value = {
            "id": rule_id, "revision": 1, "scope": "task", "target_id": self.task,
            "text": rule_id, "status": "active", "enforcement": "guidance_only",
            "rationale": "inherited intact", "source": {
                "kind": "conversation_snapshot", "rule_id": "parent-rule",
                "event_seq": 1, "extra": {"flags": [True, None, 3.5]},
            },
        }
        value.update(changes)
        return value

    def raw_event(self, payload_json, *, event_type="task.created", task_id=...,
                  actor=None, commit=True):
        # Deliberately invalid history belongs only to this temporary database.
        self.log.db.execute(
            "INSERT INTO events (id, ts, task_id, actor_json, type, category, "
            "payload_json, prev_hash, hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (f"raw-{self.log.count()}", "2026-10-17T00:00:00+00:00",
             self.task if task_id is ... else task_id,
             json.dumps((Actor.human() if actor is None else actor).to_dict()),
             event_type, "lifecycle", payload_json, self.log.head_hash(), "f" * 64),
        )
        if commit:
            self.log.db.commit()

    def assert_json_equal(self, actual, expected):
        # JSON comparison distinguishes bool/int and tolerates historical NaN.
        self.assertEqual(json.dumps(actual, sort_keys=True, ensure_ascii=False),
                         json.dumps(expected, sort_keys=True, ensure_ascii=False))

    def assert_reads_match(self, task_id=None):
        task_id = self.task if task_id is None else task_id
        self.assert_json_equal(self.book.task_projects(), self.legacy.task_projects())
        self.assert_json_equal(self.book.task_project(task_id), self.legacy.task_project(task_id))
        self.assert_json_equal(self.book.all_rules(), self.legacy.all_rules())
        self.assert_json_equal(self.book.view(task_id=task_id), self.legacy.view(task_id=task_id))
        self.assert_json_equal(self.book.current_snapshot(task_id), self.legacy.current_snapshot(task_id))

    def mixed_history(self):
        project = self.book.create_project("Local")["id"]
        other = self.book.create_project("Foreign")["id"]
        self.book.bind_project(self.task, project)
        self.created("foreign", {"project_id": other})
        self.book.record("Workspace", scope="workspace")
        shared = self.book.record("Project", scope="project", project_id=project)
        self.book.record("Foreign project", scope="project", project_id=other)
        inherited = self.rule("inherited", extra={"nested": [False, None, {"a": 1}]})
        self.created("child", {"project_id": project, "inherited_rules": [inherited]})
        self.log.emit(type="rule.updated", actor=Actor.human(), task_id=self.task,
                      payload={**inherited, "text": "Inherited edited", "revision": 2,
                               "previous_seq": 99})
        self.book.update(shared["id"], revision=1, text="Project edited", status="active")
        return project, other

    def test_complete_results_scopes_fingerprint_and_read_only_chain_match_legacy(self):
        project, other = self.mixed_history()
        head, count = self.log.head_hash(), self.log.count()
        for task_id in (self.task, "child", "foreign"):
            with self.subTest(task_id=task_id):
                self.assert_reads_match(task_id)
        for project_id in (project, other, None):
            self.assert_json_equal(self.book.view(project_id=project_id),
                                   self.legacy.view(project_id=project_id))
        self.assertEqual((self.log.head_hash(), self.log.count()), (head, count))
        self.assertTrue(self.log.verify_chain().ok)

    def test_interleaved_inherited_overwrites_keep_first_insertion_order_and_latest_seq(self):
        first = self.rule("first")
        self.log.emit(type="rule.recorded", actor=Actor.human(), payload=first)
        self.created("child", {"inherited_rules": [self.rule("second"),
                                                   {**first, "text": "inherited overwrite"}]})
        self.log.emit(type="rule.recorded", actor=Actor.human(), payload=self.rule("third"))
        latest = self.log.emit(type="rule.updated", actor=Actor.human(),
                               payload=self.rule("second", revision=2, text="latest"))
        last = self.created("next-child", {"inherited_rules": [self.rule("first", revision=3),
                                                                self.rule("fourth")]})
        self.assert_reads_match()
        rules = self.book.all_rules()
        self.assertEqual([rule["id"] for rule in rules], ["first", "second", "third", "fourth"])
        self.assertEqual((rules[0]["seq"], rules[0]["updated_at"]), (last.seq, last.ts))
        self.assertEqual((rules[1]["seq"], rules[1]["updated_at"]), (latest.seq, latest.ts))

    def test_inheritance_requires_exact_human_actor_and_task_created_event(self):
        for index, actor in enumerate((Actor.agent("dev", "developer"), Actor.system(),
                                       Actor.human("different-human"))):
            self.created(f"ignored-{index}", {"inherited_rules": [self.rule(f"ignored-{index}")]}, actor)
        self.log.emit(type="task.project_bound", actor=Actor.human(), task_id=self.task,
                      payload={"inherited_rules": [self.rule("wrong-event")]})
        self.log.emit(type="msg.said", actor=Actor.human(), task_id=self.task,
                      payload={"inherited_rules": [self.rule("chat-only")]})
        self.created("human-child", {"inherited_rules": [self.rule("trusted")]})
        self.assert_reads_match()
        self.assertEqual([r["id"] for r in self.book.all_rules()], ["trusted"])

    def test_project_values_preserve_missing_null_boolean_numeric_and_container_types(self):
        values = [None, False, True, 0, 1, -2, 1.25, "", "null", 'quoted " \\ \n',
                  [], [True, 1, None], {}, {"nested": [False, 0]}]
        self.created("missing", {})
        for index, value in enumerate(values):
            task_id = f"typed-{index}"
            self.created(task_id, {"project_id": value})
            with self.subTest(value=value):
                self.assert_reads_match(task_id)
                self.assertIs(type(self.book.task_project(task_id)), type(value))
                self.log.emit(type="task.project_bound", actor=Actor.human(), task_id=task_id,
                              payload={"project_id": value})
                self.assert_reads_match(task_id)
        self.assertIsNone(self.book.task_project("missing"))
        for invalid in (None, "", 1, True, [], {}, "unknown", "' OR 1=1 --"):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, "unknown task"):
                self.book.task_project(invalid)
        self.created("", {"project_id": "ignored"})
        self.assertNotIn("", self.book.task_projects())

    def test_missing_inherited_rules_is_empty_but_explicit_null_and_scalars_raise(self):
        self.created("missing", {"background": {"inherited_rules": [self.rule("nested-only")]}})
        self.assertEqual(self.book.all_rules(), [])
        for value in (None, False, True, 0, 2.5, [None], [False], {"bad": "rule"}):
            with self.subTest(value=value):
                with self.log.atomic():
                    self.created("bad-inheritance", {"inherited_rules": value})
                    with self.assertRaises((TypeError, KeyError)) as original:
                        self.legacy.all_rules()
                    with self.assertRaises(type(original.exception)):
                        self.book.all_rules()
                    self.log.db.rollback()
        self.assertEqual(self.book.all_rules(), [])

    def test_task_lookup_preserves_foreign_corrupt_history_errors_on_both_paths(self):
        self.created("foreign-large", {"background": "FOREIGN_BODY" * 20000})
        self.raw_event("{broken", task_id="foreign-broken")
        for fallback in (False, True):
            with self.subTest(fallback=fallback):
                with patch.object(self.book, "_json_projection_available", return_value=not fallback):
                    for book in (self.legacy, self.book):
                        with self.assertRaises(json.JSONDecodeError):
                            book.task_projects()
                        with self.assertRaises(json.JSONDecodeError):
                            book.task_project(self.task)

    def test_large_valid_backgrounds_never_enter_python_full_event_or_json_decode(self):
        for index in range(4):
            self.created(f"large-{index}", {"project_id": None,
                         "background": {"body": "FULL_BACKGROUND_MARKER" + "x" * 200000 + "\0\\u0000"},
                         "inherited_rules": [self.rule(f"copy-{index}")]})
        expected_rules = self.legacy.all_rules()
        expected_snapshot = self.legacy.current_snapshot(self.task)
        row_to_event, loads = self.log._row_to_event, json.loads

        def no_task_events(row):
            self.assertNotIn(row["type"], ("task.created", "task.project_bound"))
            return row_to_event(row)

        def no_background_decode(value, *args, **kwargs):
            self.assertNotIn("FULL_BACKGROUND_MARKER", value)
            return loads(value, *args, **kwargs)

        with patch.object(self.log, "_row_to_event", side_effect=no_task_events), \
                patch("cater.rules.json.loads", side_effect=no_background_decode):
            self.assertIsNone(self.book.task_project(self.task))
            self.assertEqual(len(self.book.task_projects()), 5)
            self.assertEqual(self.book.all_rules(), expected_rules)
            self.assertEqual(self.book.view(task_id=self.task)["rules"], expected_rules)
            self.assertEqual(self.book.current_snapshot(self.task), expected_snapshot)
            saved = self.book.snapshot(self.task)
            self.assertEqual({k: v for k, v in saved.items() if k != "seq"}, expected_snapshot)
            self.assertEqual(self.book.snapshot(self.task), saved)
        self.assertTrue(self.log.verify_chain().ok)

    def test_late_project_binding_refreshes_snapshot_and_scope_without_cache(self):
        project = self.book.create_project("Later")["id"]
        project_rule = self.book.record("Only after binding", scope="project", project_id=project)
        before = self.book.snapshot(self.task)
        self.assertIsNone(before["seq"])
        self.book.bind_project(self.task, project)
        self.assert_reads_match()
        after = self.book.snapshot(self.task)
        self.assertNotEqual(after["fingerprint"], before["fingerprint"])
        self.assertEqual([r["id"] for r in after["rules"]], [project_rule["id"]])
        self.assertEqual(self.book.snapshot(self.task), after)
        count = self.log.count()
        with self.assertRaises(RuleConflict):
            self.book.bind_project(self.task, project)
        self.assertEqual(self.log.count(), count)

    def test_second_connection_appends_and_updates_are_visible_to_existing_book(self):
        rule = self.book.record("Before", scope="workspace")
        first = self.book.snapshot(self.task)
        writer = self.open_log()
        other = RuleBook(writer)
        updated = other.update(rule["id"], revision=1, text="After", status="active")
        other.record("Second rule", scope="task", task_id=self.task)
        writer.emit(type="task.created", actor=Actor.human(), task_id="external",
                    payload={"project_id": "external-project"})
        self.assert_reads_match()
        second = self.book.snapshot(self.task)
        self.assertNotEqual(second["fingerprint"], first["fingerprint"])
        self.assertEqual(second["rules"][0]["seq"], updated["seq"])
        self.assertEqual(self.book.task_project("external"), "external-project")
        self.assertEqual(self.book.snapshot(self.task), second)
        self.assertTrue(self.log.verify_chain().ok)

    def test_transaction_rollback_discards_binding_rules_and_applied_snapshot(self):
        project = self.book.create_project("Rolled back")["id"]
        self.book.record("Stable", scope="workspace")
        before = self.book.snapshot(self.task)
        head, count = self.log.head_hash(), self.log.count()
        with self.assertRaisesRegex(RuntimeError, "rollback fixture"):
            with self.log.atomic():
                self.book.bind_project(self.task, project)
                self.book.record("Transient", scope="task", task_id=self.task)
                temporary = self.book.snapshot(self.task)
                self.assertNotEqual(temporary["fingerprint"], before["fingerprint"])
                self.assertEqual(self.book.task_project(self.task), project)
                raise RuntimeError("rollback fixture")
        self.assertEqual((self.log.head_hash(), self.log.count()), (head, count))
        self.assertEqual(self.book.snapshot(self.task), before)
        self.assert_reads_match()
        self.assertTrue(self.log.verify_chain().ok)

    def test_restart_deduplicates_current_snapshot_but_keeps_old_applied_payload(self):
        rule = self.book.record("Old", scope="task", task_id=self.task)
        first = self.book.snapshot(self.task)
        reopened = self.open_log()
        book = RuleBook(reopened)
        count = reopened.count()
        self.assertEqual(book.snapshot(self.task), first)
        self.assertEqual(reopened.count(), count)
        book.update(rule["id"], revision=1, text="New", status="active")
        second = book.snapshot(self.task)
        self.assertNotEqual(second["seq"], first["seq"])
        self.assertNotEqual(second["fingerprint"], first["fingerprint"])
        again = RuleBook(self.open_log())
        self.assertEqual(again.snapshot(self.task), second)
        history = list(reopened.iter_events(task_id=self.task, event_types=("rules.applied",)))
        self.assertEqual([event.payload["rules"][0]["text"] for event in history], ["Old", "New"])
        self.assertEqual(history[0].payload, {k: v for k, v in first.items() if k != "seq"})
        self.assert_reads_match()

    def test_candidate_adoption_retirement_and_provenance_preserve_snapshot_semantics(self):
        proposal = self.log.emit(type="msg.said", actor=Actor.agent("dev", "developer"),
                                 task_id=self.task, payload={"text": "Suggested"})
        candidate = self.book.record("Suggested", scope="task", task_id=self.task,
                                     proposal_event=proposal, rationale="Observed need")
        self.assertEqual(self.book.snapshot(self.task)["rules"], [])
        self.assert_reads_match()
        adopted = self.book.update(candidate["id"], revision=1, text="Suggested", status="active")
        active = self.book.snapshot(self.task)
        self.assertEqual(adopted["source"], candidate["source"])
        self.assertEqual(active["rules"][0]["seq"], adopted["seq"])
        self.book.update(candidate["id"], revision=2, text="Suggested", status="retired")
        retired = self.book.snapshot(self.task)
        self.assertEqual(retired["rules"], [])
        self.assertIsNotNone(retired["seq"])
        self.assertNotEqual(retired["fingerprint"], active["fingerprint"])
        self.assertEqual(self.book.snapshot(self.task), retired)
        self.assert_reads_match()

    def test_inherited_active_capacity_duplicate_rejection_and_retirement_release(self):
        inherited = [self.rule(f"capacity-{index}") for index in range(32)]
        self.created("capacity-child", {"inherited_rules": inherited})
        count = self.log.count()
        with self.assertRaisesRegex(ValueError, "capacity"):
            self.book.record("Overflow", scope="task", task_id=self.task)
        with self.assertRaisesRegex(RuleConflict, "equivalent"):
            self.book.record(inherited[0]["text"], scope="task", task_id=self.task)
        self.assertEqual(self.log.count(), count)
        self.assertEqual(len(self.book.current_snapshot(self.task)["rules"]), 32)
        self.book.update(inherited[0]["id"], revision=1, text=inherited[0]["text"], status="retired")
        self.book.record("Fits after retirement", scope="task", task_id=self.task)
        self.assert_reads_match()
        self.assertEqual(len(self.book.current_snapshot(self.task)["rules"]), 32)

    def test_inherited_character_and_candidate_limits_do_not_truncate_or_adopt(self):
        inherited = [self.rule(f"long-{index}", text=letter * 2000)
                     for index, letter in enumerate("ABC")]
        inherited += [self.rule(f"candidate-{index}", status="candidate") for index in range(64)]
        self.created("full-child", {"inherited_rules": inherited})
        count = self.log.count()
        with self.assertRaisesRegex(ValueError, "capacity"):
            self.book.record("Character overflow", scope="task", task_id=self.task)
        proposal = self.log.emit(type="msg.said", actor=Actor.agent("dev", "developer"),
                                 task_id=self.task, payload={"text": "Candidate overflow"})
        with self.assertRaisesRegex(ValueError, "too many candidate"):
            self.book.record("Candidate overflow", scope="task", task_id=self.task,
                             proposal_event=proposal, rationale="Overflow")
        self.assertEqual(self.log.count(), count + 1)
        self.assert_reads_match()
        snapshot = self.book.snapshot(self.task)
        self.assertEqual(sum(len(r["text"]) for r in snapshot["rules"]), 6000)
        self.assertEqual(len(self.book.all_rules()), 67)

    def test_missing_json_capability_uses_original_fold_for_all_public_reads_and_writes(self):
        self.mixed_history()
        expected = self.legacy.current_snapshot(self.task)
        with patch.object(RuleBook, "_json_projection_available", return_value=False):
            self.assert_reads_match()
            saved = self.book.snapshot(self.task)
            self.assertEqual({k: v for k, v in saved.items() if k != "seq"}, expected)
            updated = self.book.update("inherited", revision=2, text="Fallback edit", status="active")
            self.assertEqual(updated["revision"], 3)
            self.assert_reads_match()
            self.assertNotEqual(self.book.snapshot(self.task)["fingerprint"], saved["fingerprint"])
        self.assert_reads_match()
        self.assertTrue(self.log.verify_chain().ok)

    def test_real_sqlite_authorizer_denied_json_functions_falls_back(self):
        self.mixed_history()
        for denied_function in ("json_valid", "json_type", "->"):
            denied = []

            def authorizer(action, arg1, arg2, database, source):
                if action == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() == denied_function:
                    denied.append(arg2)
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK

            with self.subTest(function=denied_function):
                self.log.db.set_authorizer(authorizer)
                try:
                    self.assert_reads_match()
                    saved = self.book.snapshot(self.task)
                    self.assertEqual(self.book.snapshot(self.task), saved)
                finally:
                    self.log.db.set_authorizer(None)
                self.assertTrue(denied, "the real SQLite function prohibition must be exercised")
        self.assertTrue(self.log.verify_chain().ok)

    def test_non_strict_nan_history_parses_only_that_full_row_without_losing_rules(self):
        self.created("strict", {"background": "STRICT_BACKGROUND_MARKER" * 5000})
        self.created("old-nan", {"project_id": float("nan"), "old_value": float("nan"),
                                  "inherited_rules": [self.rule("nan-rule", old_value=float("nan"))]})
        expected = self.legacy.current_snapshot("old-nan")
        expected_rules = self.legacy.all_rules()
        loads, full_nan_rows = json.loads, []

        def selective_decode(value, *args, **kwargs):
            self.assertNotIn("STRICT_BACKGROUND_MARKER", value)
            if '"inherited_rules"' in value and "NaN" in value:
                full_nan_rows.append(value)
            return loads(value, *args, **kwargs)

        with patch("cater.rules.json.loads", side_effect=selective_decode):
            self.assert_json_equal(self.book.all_rules(), expected_rules)
            self.assert_json_equal(self.book.current_snapshot("old-nan"), expected)
            self.assert_json_equal(self.book.task_project("old-nan"), float("nan"))
        self.assertTrue(full_nan_rows)
        self.assertTrue(self.log.verify_chain().ok)

    def test_malformed_json_and_valid_non_object_payloads_raise_original_errors(self):
        for event_type in ("task.created", "task.project_bound", "rule.recorded", "rule.updated"):
            for payload in ("{broken", "null", "[]", "[1]", "false", "1", '"text"'):
                for fallback in (False, True):
                    with self.subTest(event_type=event_type, payload=payload, fallback=fallback):
                        # A savepoint isolates each malformed row without changing stored events.
                        with self.log.atomic():
                            self.log.db.execute(
                                "INSERT INTO events (id, ts, task_id, actor_json, type, category, "
                                "payload_json, prev_hash, hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                ("bad-row", "bad-time", self.task, json.dumps(Actor.human().to_dict()),
                                 event_type, "policy", payload, self.log.head_hash(), "f" * 64),
                            )
                            methods = ("all_rules",) if event_type in RULE_EVENTS else (
                                ("all_rules", "task_projects", "task_project")
                                if event_type == "task.created" else ("task_projects", "task_project"))
                            for method in methods:
                                args = (self.task,) if method == "task_project" else ()
                                with self.assertRaises((ValueError, TypeError, AttributeError, KeyError)) as original:
                                    getattr(self.legacy, method)(*args)
                                if fallback:
                                    with patch.object(RuleBook, "_json_projection_available", return_value=False):
                                        with self.assertRaises(type(original.exception)):
                                            getattr(self.book, method)(*args)
                                else:
                                    with self.assertRaises(type(original.exception)):
                                        getattr(self.book, method)(*args)
                            self.log.db.rollback()

    def test_real_database_permission_and_closed_connection_errors_are_not_empty_results(self):
        denied = []

        def deny_events(action, arg1, arg2, database, source):
            if action == sqlite3.SQLITE_READ and arg1 == "events":
                denied.append(arg1)
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        self.log.db.set_authorizer(deny_events)
        try:
            for method, args in (("task_projects", ()), ("task_project", (self.task,)),
                                 ("all_rules", ()), ("current_snapshot", (self.task,))):
                with self.subTest(method=method), self.assertRaises(sqlite3.DatabaseError):
                    getattr(self.book, method)(*args)
        finally:
            self.log.db.set_authorizer(None)
        self.assertTrue(denied)
        self.log.close()
        with self.assertRaises(sqlite3.ProgrammingError):
            self.book.all_rules()

    def test_emit_nul_suffixed_keys_do_not_adopt_project_or_inherited_rules(self):
        self.created("nul-only", {"project_id\0x": "unexpected-project",
                                  "inherited_rules\0x": [self.rule("unexpected-rule")]})
        head, count = self.log.head_hash(), self.log.count()
        for fallback in (False, True):
            with self.subTest(fallback=fallback):
                with patch.object(self.book, "_json_projection_available", return_value=not fallback):
                    self.assert_reads_match("nul-only")
                    self.assertIsNone(self.book.task_project("nul-only"))
                    self.assertEqual(self.book.all_rules(), [])
                    self.assertEqual(self.book.current_snapshot("nul-only")["rules"], [])
        self.assertEqual((self.log.head_hash(), self.log.count()), (head, count))
        self.assertTrue(self.log.verify_chain().ok)

    def test_emit_nul_suffixed_keys_do_not_override_exact_keys(self):
        exact = self.rule("exact", target_id="nul-exact")
        self.created("nul-exact", {"project_id": False, "project_id\0x": "wrong-project",
                                   "inherited_rules": [exact],
                                   "inherited_rules\0x": [self.rule("wrong-rule")]})
        for fallback in (False, True):
            with self.subTest(fallback=fallback):
                with patch.object(self.book, "_json_projection_available", return_value=not fallback):
                    self.assert_reads_match("nul-exact")
                    self.assertIs(self.book.task_project("nul-exact"), False)
                    self.assertEqual([r["id"] for r in self.book.all_rules()], ["exact"])
        self.assertTrue(self.log.verify_chain().ok)

    def test_duplicate_project_keys_including_escaped_aliases_preserve_last_value(self):
        for event_type in ("task.created", "task.project_bound"):
            for keys in (('"project_id"', '"project_id"'),
                         ('"project_id"', r'"\u0070roject_id"'),
                         (r'"project\u005fid"', '"project_id"')):
                for last in (None, False, {"last": [True, 0]}):
                    with self.subTest(event_type=event_type, keys=keys, last=last):
                        with self.log.atomic():
                            payload = '{' + keys[0] + ':"first",' + keys[1] + ':' + json.dumps(last) + '}'
                            self.raw_event(payload, event_type=event_type, commit=False)
                            for fallback in (False, True):
                                with patch.object(self.book, "_json_projection_available", return_value=not fallback):
                                    self.assert_reads_match()
                                    self.assert_json_equal(self.book.task_project(self.task), last)
                            self.log.db.rollback()

    def test_duplicate_inherited_keys_including_escaped_aliases_preserve_last_value_or_error(self):
        inherited = [self.rule("last-rule")]
        for keys in (('"inherited_rules"', '"inherited_rules"'),
                     ('"inherited_rules"', r'"\u0069nherited_rules"'),
                     (r'"inherited\u005frules"', '"inherited_rules"')):
            for first, last in (([], inherited), (inherited, []), ([], None)):
                with self.subTest(keys=keys, first=first, last=last):
                    with self.log.atomic():
                        payload = '{' + keys[0] + ':' + json.dumps(first) + ',' + keys[1] + ':' + json.dumps(last) + '}'
                        self.raw_event(payload, commit=False)
                        for fallback in (False, True):
                            with patch.object(self.book, "_json_projection_available", return_value=not fallback):
                                if last is None:
                                    for book in (self.legacy, self.book):
                                        with self.assertRaises(TypeError):
                                            book.all_rules()
                                        with self.assertRaises(TypeError):
                                            book.current_snapshot(self.task)
                                else:
                                    self.assert_reads_match()
                                    self.assertEqual([r["id"] for r in self.book.all_rules()],
                                                     [r["id"] for r in last])
                        self.log.db.rollback()

    def test_nonhuman_nonobject_created_payloads_are_parsed_then_ignored(self):
        self.book.record("Stable workspace rule", scope="workspace")
        for actor in (Actor.system(), Actor.agent("dev", "developer"), Actor.human("other")):
            for payload in ("null", "[]", "[1]", "NaN", "false", "1", '"text"'):
                with self.subTest(actor=actor, payload=payload):
                    with self.log.atomic():
                        self.raw_event(payload, actor=actor, task_id="nonhuman", commit=False)
                        for fallback in (False, True):
                            with patch.object(self.book, "_json_projection_available", return_value=not fallback):
                                self.assert_json_equal(self.book.all_rules(), self.legacy.all_rules())
                                self.assert_json_equal(self.book.view(), self.legacy.view())
                                self.assertEqual(len(self.book.all_rules()), 1)
                        self.log.db.rollback()

    def test_nonhuman_created_malformed_json_still_raises_before_actor_guard(self):
        for actor in (Actor.system(), Actor.agent("dev", "developer"), Actor.human("other")):
            for payload in ("{broken", "[", "NaNx"):
                with self.subTest(actor=actor, payload=payload):
                    with self.log.atomic():
                        self.raw_event(payload, actor=actor, task_id="nonhuman", commit=False)
                        for fallback in (False, True):
                            with patch.object(self.book, "_json_projection_available", return_value=not fallback):
                                for book in (self.legacy, self.book):
                                    with self.assertRaises(json.JSONDecodeError):
                                        book.all_rules()
                                    with self.assertRaises(json.JSONDecodeError):
                                        book.view()
                        self.log.db.rollback()

    def test_falsy_task_ids_skip_field_get_but_preserve_payload_parse_errors(self):
        for task_id in (None, ""):
            for event_type in ("task.created", "task.project_bound"):
                for payload in ("null", "[]", "NaN", "false", "1", "{broken"):
                    with self.subTest(task_id=task_id, event_type=event_type, payload=payload):
                        with self.log.atomic():
                            self.raw_event(payload, task_id=task_id, event_type=event_type,
                                           actor=Actor.system(), commit=False)
                            for fallback in (False, True):
                                with patch.object(self.book, "_json_projection_available", return_value=not fallback):
                                    if payload == "{broken":
                                        for book in (self.legacy, self.book):
                                            with self.assertRaises(json.JSONDecodeError):
                                                book.task_projects()
                                            with self.assertRaises(json.JSONDecodeError):
                                                book.task_project(self.task)
                                    else:
                                        self.assert_reads_match()
                                        self.assertEqual(self.book.task_projects(), {self.task: None})
                            self.log.db.rollback()

    def test_foreign_corrupt_binding_remains_fatal_to_local_current_snapshot(self):
        self.book.record("Existing active rule", scope="workspace")
        for payload in ("{broken", "null", "[]", "NaN"):
            with self.subTest(payload=payload):
                with self.log.atomic():
                    self.raw_event(payload, event_type="task.project_bound",
                                   task_id="foreign", commit=False)
                    count, head = self.log.count(), self.log.head_hash()
                    for fallback in (False, True):
                        with patch.object(self.book, "_json_projection_available", return_value=not fallback):
                            self.assertEqual(self.book.all_rules(), self.legacy.all_rules())
                            for method, args in (("task_project", (self.task,)),
                                                 ("current_snapshot", (self.task,)),
                                                 ("snapshot", (self.task,))):
                                with self.assertRaises((json.JSONDecodeError, AttributeError)) as original:
                                    getattr(self.legacy, method)(*args)
                                with self.assertRaises(type(original.exception)):
                                    getattr(self.book, method)(*args)
                    self.assertEqual((self.log.count(), self.log.head_hash()), (count, head))
                    self.log.db.rollback()

    def test_snapshot_recomputes_after_competing_connection_changes_append_head(self):
        rule = self.book.record("Before race", scope="workspace")
        writer = self.open_log()
        emit, races = self.log.emit, []

        def race(**kwargs):
            if kwargs["type"] == "rules.applied" and not races:
                races.append(RuleBook(writer).update(rule["id"], revision=1,
                                                    text="After race", status="active"))
            return emit(**kwargs)

        with patch.object(self.log, "emit", side_effect=race):
            saved = self.book.snapshot(self.task)
        self.assertEqual(len(races), 1)
        self.assertEqual(saved["rules"][0]["text"], "After race")
        self.assertEqual(saved["rules"][0]["seq"], races[0]["seq"])
        self.assert_json_equal({k: v for k, v in saved.items() if k != "seq"},
                               self.legacy.current_snapshot(self.task))
        self.assertEqual(len(list(self.log.iter_events(event_types=("rules.applied",)))), 1)
        self.assertTrue(self.log.verify_chain().ok)


    def test_unselected_valid_background_integer_is_not_a_rule_decode_gate(self):
        prior = sys.get_int_max_str_digits()
        try:
            sys.set_int_max_str_digits(0)
            self.created("old-large-integer", {"background": {"number": 10 ** 4500}})
            self.assertTrue(self.log.verify_chain().ok)
            sys.set_int_max_str_digits(4300)
            with self.assertRaises(ValueError):
                self.legacy.current_snapshot(self.task)
            self.assertEqual(self.book.current_snapshot(self.task)["rules"], [])
            with self.assertRaises(ValueError):
                self.log.read(task_id="old-large-integer")
            sys.set_int_max_str_digits(0)
            self.created("selected-large-integer", {"project_id": 10 ** 4500})
            sys.set_int_max_str_digits(4300)
            with self.assertRaises(ValueError):
                self.book.task_project(self.task)
        finally:
            sys.set_int_max_str_digits(prior)


if __name__ == "__main__":
    unittest.main()
