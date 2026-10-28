"""Completed-delivery receipts using composed offline fixtures and real ZIPs."""

import copy
import hashlib
import io
import json
from types import SimpleNamespace
import unittest
import zipfile

from cater.events import Actor
from cater.team import HOST
from cater.team_delivery import _recorded_finish_arguments
from cater.workspaces import WorkspaceService
import test_team_external as external
import test_team_review_cycle as reviews


REASON = "omitted_approved_artifacts_reason"


class CompletedDeliveryReceiptTests(unittest.TestCase):
    def setUp(self):
        self.f = self.fixture(reviews.TeamReviewCycleTests)

    def fixture(self, fixture_type):
        fixture = fixture_type()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def arguments(self, **extra):
        return {"summary": "ORIGINAL_DELIVERY_SUMMARY",
                "verification": "Offline fixture inspected; no model invoked", **extra}

    def finish(self, arguments, *, fixture=None):
        f = fixture or self.f
        result, = f.exchange(f.manager, ("finish_work", copy.deepcopy(arguments)))
        return result

    def frozen(self, f=None):
        f = f or self.f
        return [(e.seq, e.hash, copy.deepcopy(e.payload))
                for e in f.events("team.objective_completed")]

    def assert_rejected(self, result, seq):
        self.assertEqual((result["status"], result.get("code"), result.get("delivery_seq")),
                         ("error", "delivery_already_completed", seq), result)
        self.assertIsNot(result.get("already_completed"), True)
        for action in ("read_delivery", "reply_to_customer", "set_objective", "finish_work"):
            self.assertIn(action, result["message"])
        self.assertNotIn("approval_required", result)

    def assert_unchanged(self, before, *, fixture=None):
        f = fixture or self.f
        self.assertEqual(self.frozen(f), before)
        self.assertEqual(f.rt.team.view(f.task)["status"], "done")
        self.assertEqual(f.rt.team.view(f.task)["revision"], before[-1][0])
        self.assertTrue(f.rt.verify_chain().ok)

    def assert_noop_reply(self, f, source, result, summary):
        self.assertEqual(result["status"], "ok", result)
        self.assertIs(result.get("already_completed"), True)
        self.assertIn("没有新增交付", result["note"])
        self.assertIn("没有改写文件或保存新的说明", result["note"])
        replies = [e for e in f.events("msg.said")
                   if e.payload.get("reply_to") == source.seq
                   and e.payload.get("audience") == "customer"]
        reply, = replies
        self.assertEqual(reply.payload["text"], result["note"])
        self.assertNotIn(summary.strip(), reply.payload["text"])
        self.assertNotIn("delivery_seq", reply.payload)
        finished, = [e for e in f.events("team.run_finished")
                     if e.payload["message_seq"] == source.seq]
        self.assertEqual(finished.payload["status"], "completed")

    def zip_contents(self, data):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            return {name: archive.read(name) for name in archive.namelist()}

    def assert_zip_file(self, files, name, content, seq):
        self.assertEqual(files[name], content)
        manifest = json.loads(files["__cater__/manifest.json"])
        row, = [row for row in manifest["files"] if row["filename"] == name]
        self.assertEqual(row["seq"], seq)
        self.assertEqual(row["sha256"], hashlib.sha256(content).hexdigest())

    def test_selection_changes_including_same_hash_are_rejected_and_frozen(self):
        f = self.f
        a = f.publish("first\n", author=f.manager, filename="first.txt")
        b = f.publish("second\n", author=f.manager, filename="second.txt")
        c = f.publish("third\n", author=f.manager, filename="third.txt")
        twin = f.publish("first\n", author=f.manager, filename="first.txt")
        publications = {e.seq: e for e in f.events("deliverable.published")}
        self.assertNotEqual(a, twin)
        self.assertEqual(publications[a].payload["content_sha256"],
                         publications[twin].payload["content_sha256"])
        original = self.arguments(artifact_seqs=[a, b])
        self.assertEqual(self.finish(original)["status"], "ok")
        before = self.frozen()
        seq = before[-1][0]
        old_zip = self.zip_contents(f.rt.team.archive(f.task, expected_delivery_seq=seq))
        for label, selection in (("nonempty_to_empty", []), ("remove", [a]),
                                 ("add", [a, b, c]), ("replace", [a, c]),
                                 ("same_hash_different_seq", [twin, b])):
            with self.subTest(selection=label):
                self.assert_rejected(self.finish({**original, "artifact_seqs": selection}), seq)
                self.assert_unchanged(before)
                self.assertEqual(self.zip_contents(f.rt.team.archive(f.task, expected_delivery_seq=seq)), old_zip)
        g = self.fixture(reviews.TeamReviewCycleTests)
        old = g.publish(author=g.manager)
        empty = self.arguments(artifact_seqs=[])
        self.assertEqual(self.finish(empty, fixture=g)["status"], "ok")
        before = self.frozen(g)
        self.assert_rejected(self.finish({**empty, "artifact_seqs": [old]}, fixture=g), before[-1][0])
        self.assert_unchanged(before, fixture=g)

    def test_complete_parameters_and_selection_presence_must_match_original_call(self):
        f = self.f
        artifact = f.publish(author=f.manager)
        handoff = {"why": "Saved explanation", "changed": ["one", "two"],
                   "reproduce": ["Inspect the file"], "limits": ["Offline only"]}
        original = self.arguments(handoff=handoff)
        self.assertEqual(self.finish(original)["status"], "ok")
        before = self.frozen()
        candidates = [
            {**original, "summary": "A new claim that was never saved"},
            {**original, "verification": "A new verification claim"},
            {**original, "artifact_seqs": [artifact]},
            {key: value for key, value in original.items() if key != "handoff"},
            {**original, "handoff": {**handoff, "why": "New unsaved explanation"}},
            {**original, "handoff": {**handoff, "why": " Saved explanation "}},
            {**original, "handoff": {**handoff, "changed": ["two", "one"]}},
            {**original, "handoff": {**handoff, "alternatives": []}},
            {**original, REASON: "Unused but newly supplied explanation"},
        ]
        for index, candidate in enumerate(candidates):
            with self.subTest(candidate=index):
                self.assert_rejected(self.finish(candidate), before[-1][0])
                self.assert_unchanged(before)

    def test_key_and_selection_reordering_and_outer_text_trim_are_idempotent(self):
        f = self.f
        a = f.publish(author=f.manager, filename="a.txt")
        b = f.publish(author=f.manager, filename="b.txt")
        handoff = {"why": "Explanation", "limits": ["Offline"], "reproduce": []}
        original = self.arguments(artifact_seqs=[a, b], handoff=handoff)
        self.assertEqual(self.finish(original)["status"], "ok")
        before = self.frozen()
        retry = {"handoff": dict(reversed(list(handoff.items()))), "artifact_seqs": [b, a],
                 "verification": " \n" + original["verification"] + "\t",
                 "summary": "\n" + original["summary"] + "  "}
        result = self.finish(retry)
        self.assertEqual(result["status"], "ok", result)
        self.assertIs(result.get("already_completed"), True)
        self.assertEqual(result["delivery_seq"], before[-1][0])
        self.assert_unchanged(before)
        starts = [e for e in f.events("team.tool_started") if e.payload["name"] == "finish_work"]
        self.assertEqual(starts[0].payload["arguments"], original)
        self.assertEqual(starts[-1].payload["arguments"], retry)

    def test_omission_reason_uses_existing_normalization_but_changes_are_rejected(self):
        f = self.f
        artifact = f.publish(author=f.manager)
        request = f.request([artifact], requester=f.manager)
        self.assertEqual(request["status"], "ok")
        self.assertEqual(f.record(request["review_seq"])["status"], "ok")
        f.acknowledge_latest_review()
        original = self.arguments(artifact_seqs=[], **{REASON: " \ufeff\u0085\x1cInternal materials only\x1f "})
        self.assertEqual(self.finish(original)["status"], "ok")
        before = self.frozen()
        self.assertEqual(before[-1][2][REASON], "Internal materials only")
        result = self.finish({**original, REASON: "Internal materials only"})
        self.assertIs(result.get("already_completed"), True)
        for reason in ("A different reason", "", None, True, {}, "\ufeff", "x" * 1001):
            with self.subTest(reason=repr(reason)[:40]):
                self.assert_rejected(self.finish({**original, REASON: reason}), before[-1][0])
                self.assert_unchanged(before)
        without_reason = {key: value for key, value in original.items() if key != REASON}
        self.assert_rejected(self.finish(without_reason), before[-1][0])

    def test_invalid_bool_duplicate_and_foreign_sequences_never_match(self):
        f = self.f
        a = f.publish(author=f.manager)
        foreign_task = f.rt.create_task("Separate group")
        f.rt.team.enable(foreign_task)
        foreign = f.publish(author=f.manager, task_id=foreign_task)
        original = self.arguments(artifact_seqs=[a])
        self.assertEqual(self.finish(original)["status"], "ok")
        before = self.frozen()
        for selection in ([True], [False], [a, a], [foreign], [0], [-1], [float(a)],
                          [str(a)], [a, True], [a] * 129, "bad", None, {}):
            with self.subTest(selection=repr(selection)[:50]):
                self.assert_rejected(self.finish({**original, "artifact_seqs": selection}), before[-1][0])
                self.assert_unchanged(before)
        # Exercise the defensive receipt boundary with shapes a tool call cannot encode.
        for arguments in (None, [], "bad", {}, {"summary": True, "verification": "ok"},
                          {"summary": "ok", "verification": []}):
            with self.subTest(arguments=arguments):
                self.assert_rejected(f.rt.team.delivery.repeat(f.task, arguments, before[-1][0]), before[-1][0])

    def test_legacy_or_unbound_source_never_guesses_success(self):
        cases = ("no_start", "agent_start", "wrong_run", "no_receipt", "error_receipt",
                 "agent_receipt", "wrong_receipt_start", "wrong_receipt_run", "wrong_receipt_name",
                 "receipt_before_completion")
        for case in cases:
            with self.subTest(source=case):
                f = self.fixture(reviews.TeamReviewCycleTests)
                arguments = self.arguments(artifact_seqs=[])
                start = None
                if case != "no_start":
                    start = f.rt.log.emit(type="team.tool_started", task_id=f.task,
                        actor=Actor.agent(f.manager, f.rt.organization.leader.role) if case == "agent_start" else HOST,
                        payload={"run_id": "wrong" if case == "wrong_run" else "legacy-run",
                                 "name": "finish_work", "arguments": arguments})
                receipt = {"run_id": "wrong" if case == "wrong_receipt_run" else "legacy-run",
                           "name": "read_delivery" if case == "wrong_receipt_name" else "finish_work",
                           "started_seq": -1 if case == "wrong_receipt_start" else start.seq if start else -1,
                           "result": {"status": "error" if case == "error_receipt" else "ok"}}
                if case == "receipt_before_completion":
                    f.rt.log.emit(type="team.tool_finished", task_id=f.task, actor=HOST, payload=receipt)
                completed = f.rt.log.emit(type="team.objective_completed", task_id=f.task, actor=HOST,
                    payload={**arguments, "artifacts": [], "run_id": "legacy-run"})
                if case not in {"no_start", "no_receipt", "receipt_before_completion"}:
                    f.rt.log.emit(type="team.tool_finished", task_id=f.task,
                        actor=Actor.agent(f.manager, f.rt.organization.leader.role) if case == "agent_receipt" else HOST,
                        payload=receipt)
                before = self.frozen(f)
                self.assert_rejected(self.finish(arguments, fixture=f), completed.seq)
                self.assert_unchanged(before, fixture=f)

    def test_native_noop_uses_host_note_and_stops_remaining_batch(self):
        f = self.f
        arguments = self.arguments(artifact_seqs=[])
        self.assertEqual(self.finish(arguments)["status"], "ok")
        before = self.frozen()
        f.driver.steps[f.manager] = [f.native_turn("", ("finish_work", arguments),
            ("set_objective", {"goal": "MUST_NOT_EXECUTE"}),
            ("publish_artifact", {"filename": "forbidden.txt", "content": "not saved",
                                  "format": "text", "summary": "Not saved"}))]
        source = f.rt.team.receive(f.task, "Repeat the exact delivery", request_id="native-receipt")
        f.run_team()
        starts = [e for e in f.events("team.tool_started") if e.payload["message_seq"] == source.seq]
        self.assertEqual([e.payload["name"] for e in starts], ["finish_work"])
        result = f.events("team.tool_finished")[-1].payload["result"]
        self.assert_noop_reply(f, source, result, arguments["summary"])
        self.assertEqual(result["delivery_seq"], before[-1][0])
        self.assertEqual(f.events("team.objective_updated"), [])
        self.assertEqual(f.events("deliverable.published"), [])
        self.assert_unchanged(before)

    def test_external_noop_uses_host_note_and_yields_before_next_tool(self):
        f = self.fixture(external.TeamExternalTests)
        arguments = self.arguments(artifact_seqs=[])

        def finish(run):
            run.call("finish_work", **arguments)
            self.fail("Successful finish must yield")

        f.driver.scripts[f.manager] = [finish]
        f.receive("Finish the original objective")
        f.drain()
        before = self.frozen(f)

        def retry(run):
            run.call("finish_work", **arguments)
            run.call("set_objective", goal="MUST_NOT_EXECUTE")
            self.fail("No-op finish must yield before later tools")

        f.driver.scripts[f.manager] = [retry]
        source = f.receive("Repeat exactly")
        f.drain()
        self.assert_noop_reply(f, source, f.results("finish_work")[-1], arguments["summary"])
        self.assertEqual(f.results("finish_work")[-1]["delivery_seq"], before[-1][0])
        self.assertEqual(f.events("team.objective_updated"), [])
        self.assertTrue(f.driver.runs[-1].revoked)
        self.assertEqual(f.driver.runs[-1].state.phase, "yielded")
        self.assertEqual(f.events("team.external_run_finished")[-1].payload["outcome"], "yielded")
        self.assert_unchanged(before, fixture=f)

    def test_plain_followup_reply_preserves_delivery_for_both_drivers(self):
        for kind in ("native", "external"):
            with self.subTest(driver=kind):
                f = self.f if kind == "native" else self.fixture(external.TeamExternalTests)
                arguments = self.arguments(artifact_seqs=[])
                if kind == "native":
                    self.assertEqual(self.finish(arguments)["status"], "ok")
                else:
                    f.driver.scripts[f.manager] = [lambda run: run.call("finish_work", **arguments)]
                    f.receive("Original completion")
                    f.drain()
                before = self.frozen(f)
                text = "The earlier result was checked only by the offline fixture."
                if kind == "native":
                    f.driver.steps[f.manager] = [f.native_turn("", ("reply_to_customer", {"text": text}))]
                    source = f.rt.team.receive(f.task, "Explain the verification scope", request_id="plain-followup")
                    f.run_team()
                else:
                    f.driver.scripts[f.manager] = [lambda run: run.call("reply_to_customer", text=text)]
                    source = f.receive("Explain the verification scope")
                    f.drain()
                reply, = [e for e in f.events("msg.said") if e.payload.get("reply_to") == source.seq
                          and e.payload.get("audience") == "customer"]
                self.assertEqual(reply.payload["text"], text)
                self.assertNotIn("delivery_seq", reply.payload)
                self.assert_unchanged(before, fixture=f)

    def test_native_same_input_corrects_selection_and_freezes_real_old_and_new_zip_bytes(self):
        f = self.f
        old = f.publish("OLD accepted bytes\n", author=f.manager)
        new = f.publish("NEW candidate bytes\n", author=f.manager)
        original = self.arguments(artifact_seqs=[old])
        self.assertEqual(self.finish(original)["status"], "ok")
        before = self.frozen()
        old_seq = before[-1][0]
        old_delivery = copy.deepcopy(f.rt.team.delivery.read(f.task, {"delivery_seq": old_seq})["delivery"])
        old_zip = f.rt.team.archive(f.task, expected_delivery_seq=old_seq)
        changed = {**original, "artifact_seqs": [new], "summary": "NEW_SAVED_SUMMARY"}
        corrected = []

        def correct(context, history, tools):
            self.assert_rejected(json.loads(history[-1]["content"]), old_seq)
            self.assert_unchanged(before)
            corrected.append(context["message"]["seq"])
            return f.native_turn("", ("set_objective", {"goal": "Explicitly deliver the exact existing candidate"}),
                                 ("finish_work", changed))

        f.driver.steps[f.manager] = [f.native_turn("", ("finish_work", changed)), correct]
        source = f.rt.team.receive(f.task, "Deliver the existing candidate instead", request_id="receipt-correction")
        f.run_team()
        self.assertEqual(corrected, [source.seq])
        completed = f.events("team.objective_completed")[-1]
        self.assertGreater(completed.seq, old_seq)
        self.assertEqual(completed.payload["summary"], changed["summary"])
        self.assertEqual([a["seq"] for a in completed.payload["artifacts"]], [new])
        starts = [e for e in f.events("team.tool_started") if e.payload["message_seq"] == source.seq]
        self.assertEqual([e.payload["name"] for e in starts], ["finish_work", "set_objective", "finish_work"])
        self.assertEqual(len({e.payload["run_id"] for e in starts}), 1)
        self.assertEqual(len(f.events("deliverable.published")), 2)
        self.assertEqual(f.events("team.review_requested"), [])
        self.assertEqual(self.frozen()[0], before[0])
        self.assertEqual(f.rt.team.delivery.read(f.task, {"delivery_seq": old_seq})["delivery"], old_delivery)
        self.assert_zip_file(self.zip_contents(old_zip), "result.txt", b"OLD accepted bytes\n", old)
        self.assert_zip_file(self.zip_contents(f.rt.team.archive(f.task, expected_delivery_seq=completed.seq)),
                             "result.txt", b"NEW candidate bytes\n", new)
        reply, = [e for e in f.events("msg.said") if e.payload.get("delivery_seq") == completed.seq]
        self.assertEqual(reply.payload["text"], changed["summary"])
        self.assertTrue(f.rt.verify_chain().ok)

    def test_external_same_input_can_reuse_exact_file_without_new_approval(self):
        f = self.fixture(external.TeamExternalTests)
        selected = []
        arguments = self.arguments()

        def original(run):
            selected.append(f.publish(run, "EXACT reused bytes\n"))
            run.call("finish_work", **{**arguments, "artifact_seqs": selected[:]})
            self.fail("Original finish must yield")

        f.driver.scripts[f.manager] = [original]
        f.receive("Publish and finish")
        f.drain()
        before = self.frozen(f)
        old_seq = before[-1][0]
        old_zip = f.rt.team.archive(f.task, expected_delivery_seq=old_seq)
        changed = {**arguments, "artifact_seqs": selected[:], "summary": "EXTERNAL_NEW_SAVED_SUMMARY",
                   "handoff": {"why": "Reuse the exact unchanged file", "reproduce": [], "limits": ["Offline only"]}}

        def correction(run):
            self.assert_rejected(run.call("finish_work", **changed), old_seq)
            self.assert_unchanged(before, fixture=f)
            self.assertEqual(run.call("set_objective", goal="New explanation using the exact existing file")["status"], "ok")
            run.call("finish_work", **changed)
            self.fail("Corrected delivery must yield")

        f.driver.scripts[f.manager] = [correction]
        source = f.receive("Save a new explanation for that exact file")
        f.drain()
        completed = f.events("team.objective_completed")[-1]
        self.assertEqual(self.frozen(f)[0], before[0])
        self.assertEqual([a["seq"] for a in completed.payload["artifacts"]], selected)
        self.assertEqual(completed.payload["handoff"]["why"], changed["handoff"]["why"])
        self.assertEqual(len(f.events("deliverable.published")), 1)
        self.assertEqual(f.events("team.review_requested"), [])
        starts = [e for e in f.events("team.tool_started") if e.payload["message_seq"] == source.seq]
        self.assertEqual([e.payload["name"] for e in starts], ["finish_work", "set_objective", "finish_work"])
        self.assertEqual(len({e.payload["run_id"] for e in starts}), 1)
        self.assert_zip_file(self.zip_contents(old_zip), "result.txt", b"EXACT reused bytes\n", selected[0])
        self.assert_zip_file(self.zip_contents(f.rt.team.archive(f.task, expected_delivery_seq=completed.seq)),
                             "result.txt", b"EXACT reused bytes\n", selected[0])
        reply, = [e for e in f.events("msg.said") if e.payload.get("delivery_seq") == completed.seq]
        self.assertEqual(reply.payload["text"], changed["summary"])

    def test_omitted_invalid_handoff_still_compares_original_bool_and_int_arguments(self):
        original = self.arguments(artifact_seqs=[], handoff={"why": "Explanation", "reproduce": [], "limits": [True]})
        first = self.finish(original)
        self.assertEqual(first["status"], "ok")
        self.assertEqual(first["handoff_status"], "omitted_invalid")
        before = self.frozen()
        self.assertNotIn("handoff", before[-1][2])
        self.assertIs(self.finish(original).get("already_completed"), True)
        changed = {**original, "handoff": {**original["handoff"], "limits": [1]}}
        self.assert_rejected(self.finish(changed), before[-1][0])
        self.assert_rejected(self.finish(self.arguments(artifact_seqs=[])), before[-1][0])
        self.assert_unchanged(before)

    def test_omitted_references_still_compare_original_arguments_not_clean_body(self):
        original = self.arguments(artifact_seqs=[], handoff={"why": "Explanation", "reproduce": [], "limits": [],
            "evidence": [{"source_seq": True, "quote": "Unverified reference"}]})
        first = self.finish(original)
        self.assertEqual((first["status"], first["handoff_status"]), ("ok", "references_omitted_invalid"))
        before = self.frozen()
        self.assertEqual(before[-1][2]["handoff"]["evidence"], [])
        self.assertIs(self.finish(original).get("already_completed"), True)
        changed = {**original, "handoff": {**original["handoff"],
            "evidence": [{"source_seq": 1, "quote": "Unverified reference"}]}}
        self.assert_rejected(self.finish(changed), before[-1][0])
        clean = {**original, "handoff": {key: value for key, value in original["handoff"].items() if key != "evidence"}}
        self.assert_rejected(self.finish(clean), before[-1][0])
        self.assert_unchanged(before)

    def test_wrapped_surrogate_request_repeats_only_when_original_arguments_match(self):
        original = self.arguments(artifact_seqs=[], handoff={"why": ["first", "second"],
            "reproduce": [], "limits": [], "unknown": "private\ud800"})
        result = self.finish(original)
        self.assertEqual((result["status"], result["handoff_status"]), ("ok", "omitted_invalid"))
        before = self.frozen()
        started, = [e for e in self.f.events("team.tool_started") if e.payload.get("name") == "finish_work"]
        self.assertEqual(started.payload["arguments"]["encoding"], "escaped_json")
        self.assertEqual(_recorded_finish_arguments(started), original)
        self.assertIs(self.finish(original).get("already_completed"), True)
        for changed in ({**original, "handoff": {**original["handoff"], "unknown": "private\udfff"}},
                        {**original, "handoff": {k: v for k, v in original["handoff"].items() if k != "unknown"}},
                        {**original, "handoff": {**original["handoff"], "why": "first\n\nsecond"}}):
            with self.subTest(changed=repr(changed)):
                self.assert_rejected(self.finish(changed), before[-1][0])
        self.assert_unchanged(before)

    def test_wrapper_decoder_requires_trusted_host_and_exact_host_representation(self):
        original = self.arguments(artifact_seqs=[], handoff={"why": "ok", "reproduce": [],
            "limits": [], "unknown": "\ud800"})
        wrapped = WorkspaceService._public_arguments("finish_work", original)
        def event(value, *, actor=HOST, kind="team.tool_started", name="finish_work"):
            return SimpleNamespace(actor=actor, type=kind, payload={"name": name, "arguments": value})
        self.assertEqual(_recorded_finish_arguments(event(wrapped)), original)
        self.assertIsNone(_recorded_finish_arguments(event(wrapped, actor=Actor.human())))
        self.assertIsNone(_recorded_finish_arguments(event(wrapped, kind="team.tool_finished")))
        self.assertIsNone(_recorded_finish_arguments(event(wrapped, name="other")))
        plain = self.arguments(handoff={"why": "ok", "reproduce": [], "limits": [],
            "encoding": "escaped_json", "json": wrapped["json"]})
        self.assertEqual(_recorded_finish_arguments(event(plain)), plain)
        for fake in ({"encoding": "escaped_json", "json": "{"},
                     {"encoding": "escaped_json", "json": "[]"},
                     {"encoding": "escaped_json", "json": json.dumps(self.arguments())},
                     {"encoding": "escaped_json", "json": '{"summary":"a","summary":"b"}'},
                     {"encoding": "escaped_json", "json": wrapped["json"] + " "},
                     WorkspaceService._public_arguments("finish_work", {**original, "unexpected": "x"})):
            with self.subTest(value=repr(fake)[:80]):
                self.assertIsNone(_recorded_finish_arguments(event(fake)))
        # A user-controlled wrapper-shaped object with extra keys stays an ordinary request.
        lookalike = {**wrapped, "summary": "ordinary", "verification": "ordinary"}
        self.assertEqual(_recorded_finish_arguments(event(lookalike)), lookalike)
        # A user-supplied top-level wrapper cannot start a successful finish chain.
        rejected = self.finish(wrapped)
        self.assertEqual(rejected["status"], "error")
        self.assertEqual(rejected["code"], "invalid_arguments")
        self.assertEqual(self.f.events("team.objective_completed"), [])

    def test_accepted_native_delivery_keeps_receipt_after_a_correctable_read_error(self):
        f = self.f
        arguments = self.arguments(artifact_seqs=[])
        f.driver.steps[f.manager] = [f.native_turn("", ("read_artifact", {"artifact_seq": 999999}),
            ("finish_work", arguments), ("set_objective", {"goal": "MUST_NOT_EXECUTE"}))]
        source = f.rt.team.receive(f.task, "Deliver the recorded result", request_id="finish-after-read-error")
        f.run_team()
        completed, = f.events("team.objective_completed")
        results = [e.payload["result"] for e in f.events("team.tool_finished")]
        self.assertEqual([r["status"] for r in results], ["error", "ok"])
        reply, = [e for e in f.events("msg.said") if e.payload.get("delivery_seq") == completed.seq]
        self.assertEqual(reply.payload["text"], completed.payload["summary"])
        finished, = [e for e in f.events("team.run_finished") if e.payload["message_seq"] == source.seq]
        self.assertEqual(finished.payload["status"], "completed")
        self.assertEqual(f.events("team.objective_updated"), [])
        self.assertTrue(f.rt.verify_chain().ok)

    def test_native_first_finish_stops_same_turn_duplicate_and_mutations_with_delivery_marker(self):
        f = self.f
        arguments = self.arguments(artifact_seqs=[])
        f.driver.steps[f.manager] = [f.native_turn("",
            ("finish_work", arguments), ("finish_work", copy.deepcopy(arguments)),
            ("set_objective", {"goal": "MUST_NOT_REOPEN_FIRST_DELIVERY"}),
            ("publish_artifact", {"filename": "forbidden.txt", "content": "Never saved",
                                  "format": "text", "summary": "Never saved"}))]
        source = f.rt.team.receive(f.task, "Finish once despite duplicate tool calls",
                                   request_id="first-finish-duplicate-batch")
        f.run_team()
        starts = [e for e in f.events("team.tool_started") if e.payload["message_seq"] == source.seq]
        self.assertEqual([e.payload["name"] for e in starts], ["finish_work"])
        receipt, = f.events("team.tool_finished")
        self.assertEqual(receipt.payload["started_seq"], starts[0].seq)
        self.assertEqual(receipt.payload["result"]["status"], "ok")
        self.assertIsNot(receipt.payload["result"].get("already_completed"), True)
        completed, = f.events("team.objective_completed")
        self.assertEqual(completed.payload["summary"], arguments["summary"])
        self.assertEqual(completed.payload["verification"], arguments["verification"])
        self.assertEqual(completed.payload["run_id"], starts[0].payload["run_id"])
        reply, = [e for e in f.events("msg.said") if e.payload.get("reply_to") == source.seq
                  and e.payload.get("audience") == "customer"]
        self.assertEqual(reply.payload["text"], completed.payload["summary"])
        self.assertEqual(reply.payload["delivery_seq"], completed.seq)
        self.assertEqual(reply.payload["run_id"], completed.payload["run_id"])
        self.assertEqual(f.events("team.objective_updated"), [])
        self.assertEqual(f.events("deliverable.published"), [])
        self.assertEqual(len(f.driver.calls), 1)
        finished, = f.events("team.run_finished")
        self.assertEqual(finished.payload["status"], "completed")
        self.assert_unchanged(self.frozen())

    def test_oversized_outer_whitespace_is_rejected_before_and_after_completion(self):
        f = self.f
        original = self.arguments(artifact_seqs=[])
        candidates = []
        padding = " " * 32000
        for field in ("summary", "verification"):
            for side, value in (("before", padding + original[field]),
                                ("after", original[field] + padding),
                                ("both", padding + original[field] + padding)):
                candidates.append((field, side, {**original, field: value}))
        for field, side, candidate in candidates:
            with self.subTest(stage="first_finish", field=field, side=side):
                self.assertGreater(len(candidate[field]), 32000)
                self.assertEqual(candidate[field].strip(), original[field])
                result = self.finish(candidate)
                self.assertEqual(result["status"], "error")
                self.assertIsNot(result.get("already_completed"), True)
                self.assertEqual(f.events("team.objective_completed"), [])
                self.assertEqual(f.rt.team.view(f.task)["status"], "running")
        self.assertEqual(self.finish(original)["status"], "ok")
        before = self.frozen()
        for field, side, candidate in candidates:
            with self.subTest(stage="repeat_finish", field=field, side=side):
                self.assert_rejected(self.finish(candidate), before[-1][0])
                self.assert_unchanged(before)

    def test_external_noop_cleanup_failure_keeps_pending_and_warns_without_delivery_marker(self):
        f = self.fixture(external.TeamExternalTests)
        arguments = self.arguments(artifact_seqs=[])
        f.driver.scripts[f.manager] = [lambda run: run.call("finish_work", **arguments)]
        f.receive("Complete before the cleanup-failure probe")
        f.drain()
        before = self.frozen(f)
        self.assertEqual(f.rt.team.external_runs.pending(f.task), [])

        def retry(run):
            run.cleanup_complete = False
            run.call("finish_work", **arguments)
            self.fail("No-op finish must yield even when cleanup is unconfirmed")

        f.driver.scripts[f.manager] = [retry]
        source = f.receive("Repeat without confirming client cleanup")
        f.drain()
        result = f.results("finish_work")[-1]
        self.assertEqual(result["status"], "ok")
        self.assertIs(result.get("already_completed"), True)
        self.assertEqual(result["delivery_seq"], before[-1][0])
        reply, = [e for e in f.events("msg.said") if e.payload.get("reply_to") == source.seq
                  and e.payload.get("audience") == "customer"]
        self.assertTrue(reply.payload["text"].startswith(result["note"]))
        self.assertIn("\u6e05\u7406\u5c1a\u672a\u786e\u8ba4", reply.payload["text"])
        self.assertIn("\u9700\u6838\u5bf9\u8fdb\u7a0b", reply.payload["text"])
        self.assertNotIn(arguments["summary"], reply.payload["text"])
        self.assertNotIn("delivery_seq", reply.payload)
        host, = [e for e in f.events("team.run_finished") if e.payload["message_seq"] == source.seq]
        self.assertEqual(host.payload["status"], "interrupted")
        self.assertEqual(host.payload["failure_stage"], "cleanup")
        settled = f.events("team.external_run_finished")[-1]
        self.assertFalse(settled.payload["cleanup_complete"])
        pending, = f.rt.team.external_runs.pending(f.task)
        self.assertEqual(pending["started_seq"], f.events("team.external_run_started")[-1].seq)
        self.assertEqual(pending["outcome"], "unknown")
        self.assertTrue(pending["run_ended"])
        self.assertFalse(pending["replay_authorized"])
        self.assertTrue(f.driver.runs[-1].revoked)
        self.assertFalse(f.driver.runs[-1].state.cleanup_complete)
        self.assert_unchanged(before, fixture=f)

    def test_native_mixed_objective_and_first_finish_stop_remaining_tools(self):
        f = self.f
        self.assertEqual(self.finish(self.arguments(artifact_seqs=[]))["status"], "ok")
        before = self.frozen()
        arguments = self.arguments(summary="NEW_OBJECTIVE_FROZEN_SUMMARY", artifact_seqs=[])
        goal = "A separate text-only delivery"
        calls_before = len(f.driver.calls)
        f.driver.steps[f.manager] = [f.native_turn("",
            ("set_objective", {"goal": goal}), ("finish_work", arguments),
            ("publish_artifact", {"filename": "late.txt", "content": "Never saved",
                                  "format": "text", "summary": "Never saved"}),
            ("set_objective", {"goal": "MUST_NOT_REOPEN_NEW_DELIVERY"}))]
        source = f.rt.team.receive(f.task, "Adopt and finish new work in the same batch",
                                   request_id="mixed-objective-first-finish")
        f.run_team()
        starts = [e for e in f.events("team.tool_started") if e.payload["message_seq"] == source.seq]
        self.assertEqual([e.payload["name"] for e in starts], ["set_objective", "finish_work"])
        self.assertEqual(len({e.payload["run_id"] for e in starts}), 1)
        receipts = [e for e in f.events("team.tool_finished")
                    if e.payload["started_seq"] in {start.seq for start in starts}]
        self.assertEqual([e.payload["result"]["status"] for e in receipts], ["ok", "ok"])
        self.assertIsNot(receipts[-1].payload["result"].get("already_completed"), True)
        completed = f.events("team.objective_completed")[-1]
        self.assertEqual(len(f.events("team.objective_completed")), 2)
        self.assertEqual(self.frozen()[0], before[0])
        self.assertEqual(completed.payload["summary"], arguments["summary"])
        objective, = f.events("team.objective_updated")
        self.assertEqual(objective.payload["goal"], goal)
        self.assertEqual(completed.payload["objective_seq"], objective.seq)
        reply, = [e for e in f.events("msg.said") if e.payload.get("reply_to") == source.seq
                  and e.payload.get("audience") == "customer"]
        self.assertEqual(reply.payload["text"], arguments["summary"])
        self.assertEqual(reply.payload["delivery_seq"], completed.seq)
        self.assertEqual(f.events("deliverable.published"), [])
        self.assertEqual(f.events("team.review_requested"), [])
        self.assertEqual(len(f.driver.calls), calls_before + 1)
        self.assert_unchanged(self.frozen())


if __name__ == "__main__":
    unittest.main()
