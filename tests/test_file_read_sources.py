"""File provenance contracts; stdlib, fake native turns, disposable workspaces only."""

from __future__ import annotations

import copy
import hashlib
import json
import unittest

from cater.events import Actor
from cater.team import HOST
import test_team_review_cycle as reviews
import test_team_receipt_lookup as lookups


class FileReadSourcesTests(unittest.TestCase):
    def setUp(self):
        self.f = reviews.TeamReviewCycleTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.project = self.f.bind_workspace()
        self.f.rt.team.receive(self.f.task, "File source context probe", request_id="file-context")

    def put(self, path, text):
        target = self.project / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode("utf-8"))

    def exchange(self, *calls, role=None, task=None):
        return self.f.exchange(role or self.f.worker, *calls, task_id=task)

    def query(self, arguments=None, *, role=None, task=None):
        result, = self.exchange(("read_receipts", arguments or {}), role=role, task=task)
        return result

    def context(self, *, task=None):
        task = task or self.f.task
        if not any(e.payload["recipient"] == self.f.manager for e in self.f.rt.team._messages(task)):
            self.f.rt.team.receive(task, "File source context probe", request_id=f"file-context-{task}")
        return self.f.context(task_id=task)

    def read(self, path, *, role=None, **window):
        args = {"path": path, **window}
        result, = self.exchange(("workspace_read", args), role=role)
        self.assertEqual(result["status"], "ok", result)
        finish = self.f.events("team.tool_finished")[-2]  # The exchange ends with wait_for_reply.
        self.assertEqual(finish.payload["name"], "workspace_read")
        start, = [e for e in self.f.events("team.tool_started")
                  if e.seq == finish.payload["started_seq"]]
        self.assertEqual(start.payload["arguments"], args)
        self.assertEqual(finish.payload["result"], result)
        return start, finish, result

    def sources(self):
        context = self.context()
        self.assertIn("file_read_sources", context)
        sources = context["file_read_sources"]
        self.assertIsInstance(sources, dict)
        self.assertIsInstance(sources["files"], list)
        return sources

    def file_rows(self, result):
        self.assertEqual(result["status"], "ok", result)
        # The contract specifies metadata, not the response array's spelling.
        key = next((name for name in ("observations", "file_reads", "receipts") if name in result), None)
        self.assertIsNotNone(key, result)
        self.assertIsInstance(result[key], list)
        return result[key]

    def seq(self, row):
        value = row.get("receipt_seq", row.get("seq"))
        self.assertIs(type(value), int)
        self.assertGreater(value, 0)
        return value

    def assert_observation(self, observation, start, finish, result):
        self.assertEqual(self.seq(observation), finish.seq)
        for field in ("path", "sha256", "bytes", "offset", "returned_bytes", "truncated"):
            self.assertEqual(observation[field], result[field], field)
        self.assertEqual(observation["role"], start.payload["role"])
        self.assertEqual(observation["revision"], start.payload["revision"])
        self.assertEqual(observation.get("read_tool", "read_receipts"), "read_receipts")
        self.assertEqual(observation["read_args"], {"receipt_seq": finish.seq})
        self.assertNotIn("content", observation)
        recovered = self.query(observation["read_args"], role=self.f.reviewer)
        self.assertEqual([row["seq"] for row in recovered["receipts"]], [finish.seq])
        self.assertEqual(recovered["receipts"][0]["result"], result)

    def assert_empty_sources(self, *, task=None):
        self.assertNotIn("file_read_sources", self.context(task=task))
        self.assertEqual(self.file_rows(self.query({"section": "file_reads"}, task=task)), [])

    def test_schema_validation_and_section_argument_boundaries(self):
        self.exchange()
        tool, = [t for t in self.f.driver.calls[-1]["tools"] if t["name"] == "read_receipts"]
        schema = tool["input_schema"]
        self.assertEqual(schema["required"], [])
        self.assertEqual(schema["properties"]["receipt_seq"]["type"], "integer")
        self.assertEqual(schema["properties"]["receipt_seq"]["minimum"], 1)
        self.assertIn("file_reads", schema["properties"]["section"]["enum"])
        self.assertEqual(schema["properties"]["file_path"]["type"], "string")
        invalid = [{"receipt_seq": value} for value in
                   (None, True, False, 0, -1, 1.0, "1", [], {})]
        invalid += [{"section": "file_reads", "receipt_seq": 1},
                    {"section": "file_reads", "include_pending": False},
                    {"section": "file_reads", "include_pending": True},
                    {"file_path": "observed.txt"}, {"section": "unknown"}]
        invalid += [{"section": "file_reads", "file_path": value}
                    for value in (None, True, 1, [], {}, "", "  ")]
        results = self.exchange(*(("read_receipts", args) for args in invalid))
        for args, result in zip(invalid, results):
            with self.subTest(arguments=args):
                self.assertEqual(result["status"], "error", result)
                self.assertTrue(result.get("message"), result)

    def test_exact_receipt_is_a_list_and_ands_existing_filters_without_breaking_index(self):
        self.put("exact.txt", "EXACT_RECEIPT_BODY\n")
        start, finish, result = self.read("exact.txt")
        other_start, other_finish, _ = self.read("exact.txt", role=self.f.reviewer)
        filters = {"receipt_seq": finish.seq, "run_id": start.payload["run_id"],
                   "role": self.f.worker, "message_seq": start.payload["message_seq"],
                   "name": "workspace_read", "before_seq": other_finish.seq, "limit": 1}
        exact = self.query(filters)
        lookups.TeamReceiptLookupTests.assert_page(self, exact, [finish])
        self.assertEqual(exact["receipts"][0]["result"], result)
        for field, value in (("run_id", other_start.payload["run_id"]),
                             ("role", self.f.reviewer),
                             ("message_seq", other_start.payload["message_seq"]),
                             ("name", "workspace_list"), ("before_seq", finish.seq)):
            with self.subTest(field=field):
                lookups.TeamReceiptLookupTests.assert_page(self, self.query({**filters, field: value}), [])
        lookups.TeamReceiptLookupTests.assert_page(self, self.query({"receipt_seq": 99999999}), [])
        default = self.query({"name": "workspace_read"})
        lookups.TeamReceiptLookupTests.assert_page(self, default, [finish, other_finish])
        written, = self.exchange(("workspace_write", {"path": "created.txt",
            "content": "INDEX_MUTATION_BODY\n", "expected_sha256": None}))
        self.assertEqual(written["status"], "ok")
        mutation = self.f.events("team.tool_finished")[-2]
        self.assertEqual(self.query({"include_pending": True})["pending_operations"], [])
        context = self.context()
        self.assertIn("receipt_index", context)
        self.assertNotIn(finish.seq, [row["seq"] for row in context["receipt_index"]])
        index, = [row for row in context["receipt_index"] if row["seq"] == mutation.seq]
        for key in ("started_seq", "run_id", "role", "name"):
            self.assertEqual(index[key], mutation.payload[key])
        self.assertEqual(index["status"], "ok")
        self.assertEqual(self.query({"receipt_seq": mutation.seq})["receipts"][0]["result"], written)
        self.assertEqual(self.query({"section": "receipts", "name": "workspace_read"})["receipts"],
                         default["receipts"])
        self.assertNotIn("EXACT_RECEIPT_BODY", json.dumps(context))
        self.assertNotIn("INDEX_MUTATION_BODY", json.dumps(context))

    def test_first_last_are_observed_versions_and_reviewer_reads_original_receipt(self):
        self.put("source.txt", "ORIGINAL_BASELINE_NEVER_OBSERVED\n")
        self.assert_empty_sources()
        self.put("source.txt", "FIRST_OBSERVED_BODY\n")
        first = self.read("source.txt")
        written, = self.exchange(("workspace_write", {"path": "source.txt",
            "content": "LAST_OBSERVED_BODY\n", "expected_sha256": first[2]["sha256"]}))
        self.assertEqual(written["status"], "ok", written)
        last = self.read("source.txt", role=self.f.reviewer)
        self.put("source.txt", "CURRENT_UNOBSERVED_BODY\n")
        row, = self.sources()["files"]
        self.assertEqual(row["path"], "source.txt")
        self.assert_observation(row["first_observed"], *first)
        self.assert_observation(row["last_observed"], *last)
        self.assertNotEqual(first[2]["sha256"], last[2]["sha256"])
        for marker in ("FIRST_OBSERVED_BODY", "LAST_OBSERVED_BODY", "CURRENT_UNOBSERVED_BODY",
                       "ORIGINAL_BASELINE_NEVER_OBSERVED"):
            self.assertNotIn(marker, json.dumps(self.sources()))
        metadata = self.file_rows(self.query({"section": "file_reads", "file_path": "source.txt"}))
        self.assertEqual([self.seq(r) for r in metadata], [first[1].seq, last[1].seq])

    def test_utf8_partial_and_repeated_hash_keep_exact_windows_and_do_not_invent_versions(self):
        text = "\u4f60\u597d\u4e16\u754c\n" * 40
        self.put("unicode.txt", text)
        first = self.read("unicode.txt", offset=1, limit=8)
        last = self.read("unicode.txt", role=self.f.reviewer, offset=6, limit=7)
        self.assertEqual(first[2]["sha256"], hashlib.sha256(text.encode()).hexdigest())
        self.assertTrue(first[2]["truncated"])
        self.assertTrue(last[2]["truncated"])
        self.assertEqual(first[2]["returned_bytes"], len(first[2]["content"].encode()))
        self.assertLess(first[2]["returned_bytes"], first[2]["bytes"])
        row, = self.sources()["files"]
        self.assert_observation(row["first_observed"], *first)
        self.assert_observation(row["last_observed"], *last)
        self.assertEqual(row["first_observed"]["sha256"], row["last_observed"]["sha256"])
        for key in ("version_count", "versions_count"):
            if key in row:
                self.assertEqual(row[key], 1)
        if "read_count" in row:
            self.assertEqual(row["read_count"], 2)
        metadata = self.file_rows(self.query({"section": "file_reads"}))
        self.assertEqual([self.seq(r) for r in metadata], [first[1].seq, last[1].seq])
        self.assertNotIn(text, json.dumps(metadata, ensure_ascii=False))

    def test_file_read_pagination_exact_observed_paths_and_existing_filters(self):
        self.put("a.txt", "PAGING_SOURCE_BODY\n")
        self.put("unobserved.txt", "UNOBSERVED_FILE_BODY\n")
        reads = [self.read("a.txt", role=self.f.worker if i % 2 == 0 else self.f.reviewer)
                 for i in range(5)]
        seen, cursor = [], None
        for size in (2, 2, 1):
            args = {"section": "file_reads", "file_path": "a.txt", "limit": 2}
            if cursor is not None:
                args["before_seq"] = cursor
            page = self.query(args)
            rows = self.file_rows(page)
            self.assertEqual(len(rows), size)
            seqs = [self.seq(row) for row in rows]
            self.assertEqual(seqs, sorted(seqs))
            self.assertEqual(page["has_more"], size == 2)
            self.assertEqual(page["next_before_seq"], seqs[0] if size == 2 else None)
            seen.extend(seqs)
            cursor = page["next_before_seq"]
            self.assertNotIn("PAGING_SOURCE_BODY", json.dumps(page))
        self.assertEqual(sorted(seen), [read[1].seq for read in reads])
        self.assertEqual(len(seen), len(set(seen)))
        start, finish, _ = reads[2]
        args = {"section": "file_reads", "file_path": "a.txt", "role": self.f.worker,
                "name": "workspace_read", "run_id": start.payload["run_id"],
                "message_seq": start.payload["message_seq"], "limit": 1}
        self.assertEqual([self.seq(r) for r in self.file_rows(self.query(args))], [finish.seq])
        for key, value in (("name", "workspace_list"), ("role", self.f.reviewer),
                           ("message_seq", reads[0][0].payload["message_seq"]),
                           ("run_id", reads[0][0].payload["run_id"])):
            with self.subTest(filter=key):
                self.assertEqual(self.file_rows(self.query({**args, key: value})), [])
        for path in ("unobserved.txt", "missing.txt", "A.txt", "./a.txt", "../a.txt"):
            with self.subTest(path=path):
                result = self.query({"section": "file_reads", "file_path": path})
                if result["status"] == "ok":
                    self.assertEqual(self.file_rows(result), [])
                else:
                    self.assertEqual(result["status"], "error")
                self.assertNotIn("UNOBSERVED_FILE_BODY", json.dumps(result))

    def test_context_budget_eight_files_eight_kib_metadata_and_all_receipts_recoverable(self):
        reads = []
        for index in range(11):
            path = f"directory-{index:02d}/" + "source-name-" * 9 + ".txt"
            self.put(path, f"BUDGET_BODY_{index:02d}_" * 200)
            reads.append(self.read(path, limit=17))
        sources = self.sources()
        self.assertGreater(len(sources["files"]), 0)
        self.assertLessEqual(len(sources["files"]), 8)
        self.assertLessEqual(len(json.dumps(sources, ensure_ascii=False).encode("utf-8")), 8192)
        self.assertNotIn("BUDGET_BODY_", json.dumps(sources))
        by_path = {read[2]["path"]: read for read in reads}
        for row in sources["files"]:
            self.assert_observation(row["first_observed"], *by_path[row["path"]])
            self.assert_observation(row["last_observed"], *by_path[row["path"]])
        all_rows = self.file_rows(self.query({"section": "file_reads", "limit": 50}))
        self.assertEqual([self.seq(row) for row in all_rows], [read[1].seq for read in reads])

    def forge(self, template, *, start_changes=None, finish_changes=None,
              start_actor=HOST, finish_actor=HOST, task=None, source_changes=None,
              source_actor=None, run_changes=None, run_actor=HOST):
        """Copy the public event envelope of a real read, then corrupt one binding."""
        start, finish, result = template
        task = task or self.f.task
        source = self.f.rt.log.emit(type="team.message_received", actor=source_actor or Actor.human(),
            task_id=task, payload={"recipient": self.f.worker, "text": "Forgery calibration",
                "kind": "message", "revision": self.f.rt.team.view(task)["revision"],
                **(source_changes or {})})
        run_id = f"forged-run-{source.seq}"
        run = self.f.rt.log.emit(type="team.run_started", actor=run_actor, task_id=task,
            payload={"run_id": run_id, "role": self.f.worker, "message_seq": source.seq,
                     "revision": self.f.rt.team.view(task)["revision"],
                     "control_seq": self.f.rt.execution_control.view(task)["seq"],
                     **(run_changes or {})})
        payload = {**copy.deepcopy(start.payload), "run_id": run_id,
                   "message_seq": source.seq, "call_id": f"forged-call-{source.seq}",
                   **(start_changes or {})}
        fake_start = self.f.rt.log.emit(type="team.tool_started", actor=start_actor,
                                        task_id=task, payload=payload)
        fake_finish = self.f.rt.log.emit(type="team.tool_finished", actor=finish_actor, task_id=task,
            payload={**copy.deepcopy(finish.payload), "started_seq": fake_start.seq,
                     "run_id": payload["run_id"], "role": payload["role"],
                     "call_id": payload["call_id"], **(finish_changes or {})})
        self.f.rt.log.emit(type="team.run_finished", actor=HOST, task_id=task,
            payload={**run.payload, "status": "completed", "reason": "Adversarial fixture"})
        return fake_start, fake_finish, result

    def test_only_exact_host_successful_matching_run_call_role_source_reads_are_sources(self):
        self.put("trusted.txt", "TRUSTED_NATIVE_READ\n")
        valid = self.read("trusted.txt")
        bad_actor = Actor(HOST.id, "forged-role", HOST.kind)
        cases = [{"start_actor": bad_actor}, {"finish_actor": bad_actor},
                 {"run_actor": bad_actor}, {"start_actor": Actor.agent(self.f.worker, "worker")},
                 {"finish_changes": {"started_seq": True}},
                 {"finish_changes": {"started_seq": float(valid[0].seq)}},
                 {"finish_changes": {"call_id": "wrong-call"}},
                 {"finish_changes": {"role": self.f.reviewer}},
                 {"finish_changes": {"run_id": "wrong-run"}},
                 {"finish_changes": {"name": "workspace_search"}},
                 {"finish_changes": {"result": {**valid[2], "status": "error"}}},
                 {"start_changes": {"root": str(self.f.root / "wrong-root")}},
                 {"start_changes": {"message_seq": True}},
                 {"source_changes": {"recipient": self.f.reviewer}},
                 {"source_actor": Actor("human", "wrong-role", "human")},
                 {"run_changes": {"message_seq": 999999}},
                 {"run_changes": {"role": self.f.reviewer}}]
        for changes in cases:
            with self.subTest(changes=changes):
                self.forge(valid, **changes)
        # A mere tool-shaped success without a real start is not a source.
        self.f.rt.log.emit(type="team.tool_finished", actor=HOST, task_id=self.f.task,
            payload={**valid[1].payload, "started_seq": 999999})
        self.exchange(("workspace_read", {"path": "does-not-exist.txt"}),
                      ("workspace_list", {"path": "."}),
                      ("workspace_search", {"query": "TRUSTED_NATIVE_READ"}))
        row, = self.sources()["files"]
        self.assert_observation(row["first_observed"], *valid)
        self.assert_observation(row["last_observed"], *valid)
        self.assertEqual([self.seq(r) for r in self.file_rows(self.query({"section": "file_reads"}))],
                         [valid[1].seq])

    def test_event_order_and_cross_task_start_run_source_links_cannot_create_sources(self):
        self.put("order.txt", "ORDER_VALID_BODY\n")
        valid = self.read("order.txt")
        foreign = self.f.rt.create_task("Private provenance ordering fixture")
        self.f.rt.team.enable(foreign)
        foreign_start, foreign_finish, _ = self.forge(valid, task=foreign)
        self.forge(valid, start_changes={"message_seq": foreign_start.payload["message_seq"]})
        self.forge(valid, finish_changes={"started_seq": foreign_start.seq})
        self.forge(valid, start_changes={"run_id": foreign_start.payload["run_id"]})
        # Finish before the referenced start, with an otherwise matching real run/source.
        next_start_seq = self.f.rt.log.count() + 2
        early = self.f.rt.log.emit(type="team.tool_finished", actor=HOST, task_id=self.f.task,
            payload={**valid[1].payload, "started_seq": next_start_seq})
        late_start = self.f.rt.log.emit(type="team.tool_started", actor=HOST, task_id=self.f.task,
                                       payload=copy.deepcopy(valid[0].payload))
        self.assertEqual(late_start.seq, next_start_seq)
        # The original run is already finished: late tools cannot extend that run.
        late_finish = self.f.rt.log.emit(type="team.tool_finished", actor=HOST, task_id=self.f.task,
            payload={**valid[1].payload, "started_seq": late_start.seq})
        # Tool start before its claimed future run (whose source does exist).
        role = self.f.rt.team._role(self.f.worker)
        source = self.f.rt.team.receive(self.f.task, f"@{role.name} ordering probe", request_id="file-ordering")
        premature = self.f.rt.log.emit(type="team.tool_started", actor=HOST, task_id=self.f.task,
            payload={**valid[0].payload, "run_id": "future-file-run", "message_seq": source.seq})
        run = self.f.rt.log.emit(type="team.run_started", actor=HOST, task_id=self.f.task,
            payload={"run_id": "future-file-run", "role": self.f.worker, "message_seq": source.seq})
        self.f.rt.log.emit(type="team.tool_finished", actor=HOST, task_id=self.f.task,
            payload={**valid[1].payload, "started_seq": premature.seq, "run_id": "future-file-run"})
        self.f.rt.log.emit(type="team.run_finished", actor=HOST, task_id=self.f.task,
                          payload={**run.payload, "status": "completed"})
        rows = self.file_rows(self.query({"section": "file_reads"}))
        self.assertEqual([self.seq(row) for row in rows], [valid[1].seq])
        self.assertNotIn(foreign_finish.seq, [self.seq(row) for row in rows])
        self.assertNotIn(early.seq, [self.seq(row) for row in rows])
        self.assertNotIn(late_finish.seq, [self.seq(row) for row in rows])

    def test_foreign_group_same_root_and_run_id_never_leaks_exact_receipt_or_metadata(self):
        self.put("shared.txt", "LOCAL_FILE_SOURCE\n")
        local = self.read("shared.txt")
        foreign = self.f.rt.create_task("Private source group")
        self.f.rt.team.enable(foreign)
        self.f.rt.workspaces.bind(foreign, "supervision", expected_revision=None)
        self.put("shared.txt", "FOREIGN_FILE_SOURCE_SECRET\n")
        result, = self.exchange(("workspace_read", {"path": "shared.txt"}), task=foreign)
        self.assertEqual(result["status"], "ok")
        private, = [e for e in self.f.rt.log.read(task_id=foreign)
                    if e.type == "team.tool_finished" and e.payload["name"] == "workspace_read"]
        self.forge(local, task=foreign,
                   start_changes={"run_id": local[0].payload["run_id"]})
        responses = [self.query({"receipt_seq": private.seq}),
                     self.query({"section": "file_reads"}),
                     self.query({"section": "file_reads", "file_path": "shared.txt"}),
                     self.query({"receipt_seq": private.seq, "role": self.f.worker})]
        self.assertEqual(responses[0]["receipts"], [])
        self.assertEqual(responses[3]["receipts"], [])
        for response in responses[1:3]:
            self.assertEqual([self.seq(row) for row in self.file_rows(response)], [local[1].seq])
        self.assertNotIn("FOREIGN_FILE_SOURCE_SECRET", json.dumps(responses))
        row, = self.sources()["files"]
        self.assert_observation(row["last_observed"], *local)

    def test_rebinding_same_root_and_configuration_unavailability_hide_old_sources(self):
        self.put("epoch.txt", "OLD_EPOCH_SOURCE\n")
        old = self.read("epoch.txt")
        binding = self.f.rt.workspaces.view(self.f.task)
        self.f.rt.workspaces.bind(self.f.task, "supervision", expected_revision=binding["revision"])
        self.assert_empty_sources()
        new = self.read("epoch.txt", role=self.f.reviewer)
        row, = self.sources()["files"]
        self.assert_observation(row["first_observed"], *new)
        self.assertNotEqual(self.seq(row["first_observed"]), old[1].seq)
        config = self.f.root / "supervision-workspaces.json"
        saved = config.read_bytes()
        config.write_text('{"workspaces": []}', encoding="utf-8")
        self.assertFalse(self.f.rt.workspaces.view(self.f.task)["available"])
        self.assert_empty_sources()
        config.write_bytes(saved)
        self.assertTrue(self.f.rt.workspaces.view(self.f.task)["available"])
        binding = self.f.rt.workspaces.view(self.f.task)
        self.f.rt.workspaces.bind(self.f.task, None, expected_revision=binding["revision"])
        self.assert_empty_sources()

    def test_same_binding_cross_goal_preserves_reads_but_stops_old_native_writes(self):
        self.put("goal.txt", "OLD_GOAL_SOURCE_BODY\n")
        old = self.read("goal.txt")
        binding = self.f.rt.workspaces.view(self.f.task)
        role = self.f.rt.team._role(self.f.worker)
        delegated, = self.exchange(("delegate", {"recipient": role.id,
            "instruction": "Old goal assignment; modify goal.txt using its observed hash."}),
            role=self.f.manager)
        self.assertEqual(delegated["status"], "ok")
        source, = [e for e in self.f.rt.team._messages(self.f.task)
                   if e.seq == delegated["message_seq"]]
        started = self.f.rt.log.emit(type="team.run_started", actor=HOST, task_id=self.f.task,
            payload={"run_id": "file-old-goal-run", "role": role.id, "message_seq": source.seq,
                     "revision": self.f.rt.team.view(self.f.task)["revision"],
                     "control_seq": self.f.rt.execution_control.view(self.f.task)["seq"]})
        adopted, = self.exchange(("set_objective", {"goal": "A new independent objective"}),
                                 role=self.f.manager)
        self.assertEqual(adopted["status"], "ok")
        self.assertEqual(self.f.rt.workspaces.view(self.f.task)["revision"], binding["revision"])
        self.f.driver.steps[role.id] = [self.f.native_turn("", ("workspace_write", {
            "path": "goal.txt", "content": "FORBIDDEN_OLD_GOAL_WRITE",
            "expected_sha256": old[2]["sha256"]}))]
        writes_before = len(self.f.results("workspace_write"))
        list(self.f.rt.team._run_message(self.f.task, source, role, started))
        self.assertEqual(len(self.f.results("workspace_write")), writes_before)
        self.assertEqual((self.project / "goal.txt").read_bytes(), b"OLD_GOAL_SOURCE_BODY\n")
        row, = self.sources()["files"]
        self.assert_observation(row["first_observed"], *old)
        self.assertEqual([self.seq(r) for r in self.file_rows(self.query({"section": "file_reads"}))],
                         [old[1].seq])

    def test_paused_queries_are_read_only_and_frozen_derived_group_has_no_inherited_reads(self):
        self.put("pause.txt", "PAUSED_ORIGINAL_BODY\n")
        observed = self.read("pause.txt")
        child = self.f.rt.derive_task(self.f.task, "Frozen reference only", request_id="file-derive")
        child_id = child["task_id"]
        self.f.rt.team.enable(child_id)
        # Even explicitly binding the same physical root cannot import the parent's observations.
        self.f.rt.workspaces.bind(child_id, "supervision", expected_revision=None)
        self.assert_empty_sources(task=child_id)
        self.assertEqual(self.query({"receipt_seq": observed[1].seq}, task=child_id)["receipts"], [])
        self.f.rt.execution_control.change(self.f.task, "pause", request_id="file-pause")
        self.assertEqual(self.f.rt.execution_control.view(self.f.task)["state"], "paused")
        count = self.f.rt.log.count()
        raw = (self.project / "pause.txt").read_bytes()
        binding = self.f.rt.workspaces.view(self.f.task)
        role = self.f.rt.team._role(self.f.reviewer)
        for args in ({"section": "file_reads"}, {"receipt_seq": observed[1].seq},
                     {"name": "workspace_read", "include_pending": True}):
            with self.subTest(arguments=args):
                result = self.f.rt.team._execute(self.f.task, role, "read_receipts", args,
                                                 None, binding, None, None)
                self.assertEqual(result["status"], "ok", result)
                if args.get("section") == "file_reads":
                    self.assertEqual([self.seq(row) for row in self.file_rows(result)], [observed[1].seq])
                else:
                    self.assertEqual(result["receipts"][0]["result"], observed[2])
        self.assertIn("file_read_sources", self.context())
        self.assertEqual(self.f.rt.log.count(), count)
        self.assertEqual((self.project / "pause.txt").read_bytes(), raw)
        self.assertEqual(self.f.rt.execution_control.view(self.f.task)["state"], "paused")
        self.assertTrue(self.f.rt.verify_chain().ok)


def load_tests(loader, tests, pattern):
    return loader.loadTestsFromTestCase(FileReadSourcesTests)


if __name__ == "__main__":
    unittest.main()
