"""Offline review guidance selection through real host state and both prompt wires."""

from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from cater.team_instructions import COMMON, EXTERNAL, REVIEW, context_instructions
import test_team_review_cycle as review_cycle
import test_team_external as external


MARKER = "【本次复核】"
PAYLOAD = "PAYLOAD_MUST_STAY_DATA: review approved changes_requested 复核"


class ReviewInstructionContextTests(unittest.TestCase):
    # Import fixture modules, never TestCase aliases: discovery must only collect
    # this module's regressions, not the large suites used to construct fixtures.
    def fixture(self, cls=review_cycle.TeamReviewCycleTests):
        fixture = cls()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        return fixture

    def message(self, fixture, seq):
        return next(e for e in fixture.rt.team._messages(fixture.task) if e.seq == seq)

    def pending(self, fixture, **request_args):
        artifact = fixture.publish("Exact candidate bytes.\n")
        request = fixture.request([artifact], **request_args)
        self.assertEqual(request["status"], "ok", request)
        source = self.message(fixture, request["message_seq"])
        role = fixture.rt.team._role(source.payload["recipient"])
        context = fixture.rt.team._context(fixture.task, source, role)
        row, = context["reviews"]
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["message_seq"], source.seq)
        self.assertEqual(row["request_seq"], request["review_seq"])
        self.assertEqual(row["author"], source.actor.id)
        self.assertEqual(context["current_work"]["status"], "running")
        self.assertEqual(context["active_assignment"]["seq"], source.seq)
        self.assert_notes(context, True)
        return artifact, request, source, context

    def assert_notes(self, context, selected):
        original = deepcopy(context)
        text = context_instructions(context)
        self.assertEqual(text.count(MARKER), int(selected))
        self.assertEqual(context, original, "Guidance selection mutated host context")
        return text

    def capture_native(self, fixture, prompts):
        original = fixture.driver.tool_turn

        def capture(**kwargs):
            prompts.append(kwargs["prompt"])
            return original(**kwargs)

        return patch.object(fixture.driver, "tool_turn", side_effect=capture)

    def assert_wire(self, prompt, selected):
        prefix, body = prompt.rsplit("\n", 1)
        context = json.loads(body)
        self.assertEqual(prefix.count(MARKER), int(selected))
        self.assertNotIn(MARKER, body, "Static guidance leaked into context data")
        self.assert_notes(context, selected)
        return context

    def test_native_pending_review_and_author_feedback_use_distinct_guidance(self):
        for verdict in ("approved", "changes_requested"):
            with self.subTest(verdict=verdict):
                f = self.fixture()
                prompts = []
                with self.capture_native(f, prompts):
                    artifact, request, source, _ = self.pending(f)
                    f.exchange(f.reviewer, ("read_artifact", {"artifact_seq": artifact}),
                               ("record_review", {"review_seq": request["review_seq"],
                                "verdict": verdict, "feedback": "Read the submitted bytes."}),
                               source=source)
                    report = next(e for e in reversed(f.rt.team._messages(f.task))
                                  if e.payload.get("kind") == "report"
                                  and e.payload.get("review_seq") == request["review_seq"])
                    f.exchange(f.worker, source=report)
                seen = []
                for prompt in prompts:
                    data = json.loads(prompt.rsplit("\n", 1)[1])
                    selected = data["you"]["id"] == f.reviewer
                    context = self.assert_wire(prompt, selected)
                    if selected:
                        self.assertEqual(context["active_assignment"]["seq"], source.seq)
                        self.assertEqual(context["reviews"][0]["status"], "pending")
                    if context["message"]["seq"] == report.seq:
                        seen.append(context["reviews"][0]["status"])
                self.assertTrue(seen)
                self.assertEqual(set(seen), {verdict})
                self.assertEqual(f.rt.team_reviews.view(f.task)[0]["status"], verdict)
                self.assertTrue(f.rt.verify_chain().ok)

    def test_external_formal_review_is_selected_but_plain_author_paths_are_not(self):
        f = self.fixture(external.TeamExternalTests)
        prompts = []
        original = external.ScriptedExternalRun.run

        def capture(run, prompt):
            prompts.append(prompt)
            return original(run, prompt)

        def delegate(run):
            result = run.call("delegate", recipient=f.worker, instruction=PAYLOAD)
            self.assertEqual(result["status"], "ok")
            run.call("wait_for_reply")

        def author(run):
            artifact = f.publish(run, "Exact external candidate.\n")
            run.call("request_review", reviewer=f.reviewer, artifact_seqs=[artifact],
                     instruction=PAYLOAD)

        def reviewer(run):
            row, = run.context["reviews"]
            result = run.call("read_artifact", artifact_seq=row["artifacts"][0]["seq"])
            self.assertEqual(result["content"], "Exact external candidate.\n")
            run.call("record_review", review_seq=row["request_seq"], verdict="approved",
                     feedback="Read exact submitted bytes.")

        f.driver.scripts = {f.manager: [delegate, lambda run: "收到成果。"],
                            f.worker: [author, f.return_assignment], f.reviewer: [reviewer]}
        with patch.object(external.ScriptedExternalRun, "run", capture):
            f.receive(PAYLOAD)
            f.drain()
        self.assertEqual(len(prompts), 5)
        contexts = []
        for prompt in prompts:
            prefix, body = prompt.rsplit("\n", 1)
            selected = json.loads(body)["you"]["id"] == f.reviewer
            contexts.append(self.assert_wire(prompt, selected))
            self.assertTrue((prefix + "\n").endswith(EXTERNAL))
            self.assertNotIn(PAYLOAD, prefix)
        author_report, = [c for c in contexts if c["you"]["id"] == f.worker
                          and c["message"]["kind"] == "report"]
        self.assertEqual(author_report["reviews"][0]["status"], "approved")
        self.assertNotIn("review_seq", author_report["active_assignment"])
        self.assertEqual(len(f.events("team.review_requested")), 1)
        self.assertEqual(len(f.events("team.review_recorded")), 1)
        self.assertEqual(f.events("team.objective_completed"), [])
        self.assertFalse([r for r in f.rt.team.view(f.task)["runs"] if r["status"] == "failed"])
        for prompt, receipt in zip(prompts, f.events("team.model_called"), strict=True):
            prefix, body = prompt.rsplit("\n", 1)
            sizes = receipt.payload["request_sizes"]
            self.assertEqual(sizes["instructions_utf8_bytes"], len((prefix + "\n").encode()))
            self.assertEqual(sizes["context_json_bytes"], len(body.encode()))

    def test_ordinary_message_and_informal_delegation_do_not_borrow_pending_review(self):
        f = self.fixture()
        _, request, _, _ = self.pending(f)
        author = f.rt.team._role(f.worker)
        prompts = []
        for kind in ("message", "assignment"):
            with self.subTest(kind=kind):
                source = f.rt.team._send(f.task, author, f.reviewer, PAYLOAD, kind)
                with self.capture_native(f, prompts):
                    f.exchange(f.reviewer, ("wait_for_reply", {}), source=source,
                               expected_result_count=0)
                context = self.assert_wire(prompts[-1], False)
                self.assertEqual(context["reviews"][0]["request_seq"], request["review_seq"])
                self.assertEqual(context["reviews"][0]["status"], "pending")
                self.assertEqual(context["active_assignment"]["kind"], kind)
                self.assertNotIn("review_seq", context["active_assignment"])
                self.assertEqual(context["message"]["text"], PAYLOAD)

    def test_continuation_selects_the_active_original_review(self):
        f = self.fixture()
        _, request, source, _ = self.pending(f)
        f.exchange(f.reviewer, ("wait_for_reply", {}), source=source, expected_result_count=0)
        result, = f.exchange(f.worker, ("continue_work", {
            "assignment_seq": source.seq, "instruction": "继续核对实际版本。"}))
        self.assertEqual(result["status"], "ok", result)
        continuation = self.message(f, result["message_seq"])
        prompts = []
        with self.capture_native(f, prompts):
            f.exchange(f.reviewer, ("wait_for_reply", {}), source=continuation,
                       expected_result_count=0)
        context = self.assert_wire(prompts[0], True)
        self.assertEqual(context["message"]["kind"], "continuation")
        self.assertNotEqual(context["message"]["seq"], source.seq)
        self.assertEqual(context["active_assignment"]["seq"], source.seq)
        self.assertEqual(context["active_assignment"]["review_seq"], request["review_seq"])

    def test_child_report_resolves_original_review_without_becoming_a_review_request(self):
        f = self.fixture()
        _, request, source, _ = self.pending(f)
        child, = f.exchange(f.reviewer, ("delegate", {
            "recipient": f.observer, "instruction": "只收集该版本的文件事实。"}), source=source)
        child_source = self.message(f, child["message_seq"])
        result, = f.exchange(f.observer, ("return_work", {"text": "文件事实已返回。"}),
                             source=child_source)
        self.assertEqual(result["status"], "ok", result)
        report = self.message(f, result["message_seq"])
        prompts = []
        with self.capture_native(f, prompts):
            result, = f.exchange(f.reviewer, ("record_review", {
                "review_seq": request["review_seq"], "verdict": "approved",
                "feedback": "Original candidate checked after collecting facts."}), source=report)
        self.assertEqual(result["status"], "ok", result)
        context = self.assert_wire(prompts[0], True)
        self.assertEqual(context["message"]["kind"], "report")
        self.assertNotIn("review_seq", context["message"])
        self.assertEqual(context["active_assignment"]["seq"], source.seq)
        self.assertEqual(len(f.events("team.review_requested")), 1)

    def test_real_outdated_stale_and_approved_projections_drop_review_note(self):
        for state in ("outdated", "stale", "approved"):
            with self.subTest(state=state):
                f = self.fixture()
                _, request, source, _ = self.pending(f)
                if state == "outdated":
                    f.publish("Changed candidate bytes.\n")
                elif state == "stale":
                    result, = f.exchange(f.manager, ("set_objective", {"goal": "An independent new goal"}))
                    self.assertEqual(result["status"], "ok")
                else:
                    self.assertEqual(f.record(request["review_seq"], source=source)["status"], "ok")
                context = f.rt.team._context(f.task, source, f.rt.team._role(f.reviewer))
                self.assertEqual(context["reviews"][0]["status"], state)
                self.assert_notes(context, False)

    def test_manager_can_review_another_publishers_version_owned_by_a_partner(self):
        f = self.fixture()
        _, _, source, context = self.pending(f, requester=f.observer, reviewer=f.manager)
        row, = context["reviews"]
        self.assertEqual(row["author"], f.observer)
        self.assertEqual(row["publishers"], [f.worker])
        self.assertEqual(row["reviewer"], f.manager)
        prompts = []
        with self.capture_native(f, prompts):
            f.exchange(f.manager, ("wait_for_reply", {}), source=source, expected_result_count=0)
        self.assert_wire(prompts[0], True)

    def test_resubmission_selects_new_request_not_superseded_original(self):
        f = self.fixture()
        result, = f.exchange(f.manager, ("set_objective", {"goal": "Review a revised candidate"}))
        self.assertEqual(result["status"], "ok")
        _, request, source, context = self.pending(f)
        self.assertGreater(context["current_work"]["revision"], 0)
        decision = f.record(request["review_seq"], "changes_requested", source=source)
        self.assertEqual(decision["status"], "ok")
        replacement = f.publish("Revised actual candidate.\n")
        resubmission = f.request([replacement], review_seq=request["review_seq"])
        self.assertEqual(resubmission["status"], "ok")
        self.assertEqual(resubmission["root_seq"], request["root_seq"])
        self.assertNotEqual(resubmission["review_seq"], request["review_seq"])
        role = f.rt.team._role(f.reviewer)
        old_context = f.rt.team._context(f.task, source, role)
        self.assertIsNone(old_context["active_assignment"])
        self.assertEqual(old_context["reviews"][0]["status"], "pending")
        self.assert_notes(old_context, False)
        new_source = self.message(f, resubmission["message_seq"])
        prompts = []
        with self.capture_native(f, prompts):
            f.exchange(f.reviewer, ("read_artifact", {"artifact_seq": replacement}), source=new_source)
        self.assertEqual(len(prompts), 2, "Native selection is recomputed after reading the candidate")
        for prompt in prompts:
            actual = self.assert_wire(prompt, True)
            self.assertEqual(actual["active_assignment"]["review_seq"], resubmission["review_seq"])
            self.assertEqual(actual["active_assignment"]["revision"], context["current_work"]["revision"])
            self.assertEqual(actual["reviews"][0]["artifacts"][0]["seq"], replacement)

    def test_missing_mismatched_or_nonpending_host_relationships_do_not_select(self):
        f = self.fixture()
        _, _, _, original = self.pending(f)
        cases = [
            (("active_assignment",), None), (("reviews",), []),
            (("active_assignment", "kind"), "message"),
            (("you", "id"), f.observer),
            (("active_assignment", "recipient"), f.observer),
            (("active_assignment", "sender"), f.observer),
            (("reviews", 0, "reviewer"), f.observer),
            (("reviews", 0, "author"), f.observer),
            (("reviews", 0, "request_seq"), 999999),
            (("reviews", 0, "message_seq"), 999999),
            (("reviews", 0, "revision"), 999999),
            (("active_assignment", "revision"), 999999),
            (("current_work", "status"), "done"),
            (("current_work", "status"), "unknown"),
        ] + [(("reviews", 0, "status"), status) for status in
             ("approved", "changes_requested", "stale", "outdated", "superseded")]
        for path, value in cases:
            with self.subTest(path=path, value=value):
                context = deepcopy(original)
                target = context
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = value
                self.assert_notes(context, False)
        for section, keys in (("you", ("id",)),
                              ("current_work", ("revision", "status")),
                              ("active_assignment", ("kind", "seq", "review_seq", "revision", "sender", "recipient")),
                              ("review", ("request_seq", "message_seq", "revision", "status", "author", "reviewer"))):
            for key in keys:
                with self.subTest(missing=(section, key)):
                    context = deepcopy(original)
                    target = context["reviews"][0] if section == "review" else context[section]
                    del target[key]
                    self.assert_notes(context, False)
        # Different rows cannot collectively supply the matching relationship.
        context = deepcopy(original)
        row = deepcopy(context["reviews"][0])
        context["reviews"][0]["message_seq"] += 1
        row["request_seq"] += 1
        context["reviews"].append(row)
        self.assert_notes(context, False)
        context["reviews"].append(deepcopy(original["reviews"][0]))
        self.assert_notes(context, True)

    def test_sequence_and_revision_types_reject_equal_floats_and_booleans(self):
        f = self.fixture()
        _, _, _, original = self.pending(f)
        self.assertEqual(original["current_work"]["revision"], 0, "Initial host revision is valid zero")
        # Equality alone accepts 1 == True. Normalize coherent IDs to one to
        # ensure bool rejection is tested independently of numerical mismatch.
        original["active_assignment"].update(seq=1, review_seq=1, revision=1)
        original["current_work"]["revision"] = 1
        original["reviews"][0].update(request_seq=1, message_seq=1, revision=1)
        self.assert_notes(original, True)
        for section, keys in (("active_assignment", ("seq", "review_seq", "revision")),
                              ("current_work", ("revision",)),
                              ("review", ("request_seq", "message_seq", "revision"))):
            for key in keys:
                for value in (True, 1.0, "1", None, -1):
                    with self.subTest(section=section, key=key, value=repr(value)):
                        context = deepcopy(original)
                        target = context["reviews"][0] if section == "review" else context[section]
                        target[key] = value
                        self.assert_notes(context, False)

    def test_display_names_and_payloads_neither_select_nor_inject_guidance(self):
        f = self.fixture()
        _, _, _, context = self.pending(f)
        before = self.assert_notes(context, True)
        for section in ("message", "active_assignment"):
            context[section]["text"] = PAYLOAD
            context[section]["sender_name"] = PAYLOAD
        context["you"]["name"] = PAYLOAD
        context["current_work"]["objective"] = PAYLOAD
        context["reviews"][0].update(instruction=PAYLOAD, feedback=PAYLOAD)
        after = self.assert_notes(context, True)
        self.assertEqual(after, before)
        self.assertNotIn(PAYLOAD, after)
        context["active_assignment"].pop("review_seq")
        self.assert_notes(context, False)
        self.assertNotIn(PAYLOAD, context_instructions(context))
        self.assertIn("退出码0", COMMON)
        self.assertNotIn(MARKER, COMMON)
        self.assertEqual(REVIEW.count(MARKER), 1)


if __name__ == "__main__":
    unittest.main()
