"""Review revisions through native host tools, disposable Runtime and real queues.

No production data, services, remote models, clock waits, or product patches.
The fixture owns setup/native tool execution; this suite is deliberately not a
subclass of its test case, so discovery runs only these twelve regressions.
"""

from contextlib import nullcontext
import hashlib
import io
import json
import unittest
from unittest.mock import patch
import zipfile

from cater.team import HOST
from cater.web import ActivityRunner
import test_team_review_cycle as cycle


class ReviewRevisionsTests(unittest.TestCase):
    def fixture(self):
        f = cycle.TeamReviewCycleTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        return f

    def setUp(self):
        self.f = self.fixture()

    def message(self, f, seq):
        return next(e for e in f.rt.log.read(task_id=f.task) if e.seq == seq)

    def ok(self, result):
        self.assertEqual(result.get("status"), "ok", result)
        return result

    def refs(self, f, seqs):
        artifacts = {a["seq"]: a for a in f.rt.task(f.task).deliverables}
        return [{k: artifacts[seq][k] for k in ("seq", "filename", "content_sha256")}
                for seq in seqs]

    def row(self, f, request):
        return next(r for r in f.rt.team_reviews.view(f.task)
                    if r["request_seq"] == request["review_seq"])

    def revise(self, f, previous, seqs, **kwargs):
        result = self.ok(f.request(seqs, review_seq=previous["review_seq"], **kwargs))
        self.assertNotEqual(result["review_seq"], previous["review_seq"])
        self.assertEqual(result["root_seq"], previous["root_seq"])
        row = self.row(f, result)
        self.assertEqual(row["status"], "pending", "A revision needs a new independent decision")
        self.assertEqual(row["artifacts"], self.refs(f, seqs))
        return result

    def approve(self, f, request):
        result = self.ok(f.record(request["review_seq"]))
        f.exchange(f.worker, source=self.message(f, result["message_seq"]))
        return result

    def finish(self, f, seqs=None):
        args = {"summary": "Reviewed complete set", "verification": "Disposable native fixture"}
        if seqs is not None:
            args["artifact_seqs"] = seqs
        result, = f.exchange(f.manager, ("finish_work", args))
        return result

    def effects(self, f, task_id=None):
        return [e for e in f.rt.log.read(task_id=task_id or f.task) if e.type in {
            "deliverable.published", "team.review_requested", "team.review_recorded",
            "team.objective_completed"} or e.type == "team.message_received" and e.actor.kind == "agent"]

    def assert_not_started(self, result, reason):
        self.assertEqual(result.get("status"), "error", result)
        self.assertIs(result.get("complete"), True, result)
        self.assertEqual(result.get("outcome"), "not_started", result)
        self.assertIn(reason, result.get("code", ""), result)
        self.assertTrue(result.get("message"), result)

    def assert_visible_refs(self, visible, refs):
        # Both structured context and host-authored assignment text are public
        # reviewer inputs. Do not prescribe a new history/previous_refs field.
        rendered = json.dumps(visible, ensure_ascii=False)
        for ref in refs:
            self.assertRegex(rendered, rf"(?<!\d){ref['seq']}(?!\d)")
            self.assertIn(ref["filename"], rendered)
            self.assertIn(ref["content_sha256"], rendered)

    def assert_delivery(self, f, seqs):
        completed, = f.events("team.objective_completed")
        expected = self.refs(f, seqs)
        self.assertCountEqual(completed.payload["artifacts"], expected)
        with zipfile.ZipFile(io.BytesIO(f.rt.team.archive(f.task))) as archive:
            manifest = json.loads(archive.read("__cater__/manifest.json"))
            self.assertCountEqual(manifest["files"], [
                {"seq": r["seq"], "filename": r["filename"], "sha256": r["content_sha256"]}
                for r in expected])
            self.assertEqual({n for n in archive.namelist() if not n.startswith("__cater__/")},
                             {r["filename"] for r in expected})
            for ref in expected:
                self.assertEqual(hashlib.sha256(archive.read(ref["filename"])).hexdigest(),
                                 ref["content_sha256"])

    def queued(self, f, text):
        """Run only the host queue's scheduled consumers, synchronously.

        This replaces thread scheduling, not queueing or Runtime execution. A
        missing wake-up must fail; there is no direct collaborate fallback.
        """
        scheduled = []

        class DeferredThread:
            def __init__(self, *, target, args=(), kwargs=None, **options):
                self.target, self.args, self.kwargs = target, args, kwargs or {}

            def start(self):
                scheduled.append(self)

        runner = ActivityRunner(lambda: nullcontext(f.rt))
        with patch("cater.web.threading.Thread", DeferredThread):
            source = runner.receive_team(f.rt, f.task, text)
            runner.continue_team(f.task, source)
            self.assertTrue(scheduled, "Host did not schedule the recorded customer input")
            consumed = 0
            while scheduled:
                consumed += 1
                self.assertLessEqual(consumed, 12, "Host queue did not settle")
                thread = scheduled.pop(0)
                thread.target(*thread.args, **thread.kwargs)
        self.assertFalse(runner.status(f.task)["active"], runner.status(f.task))
        self.assertEqual(f.protocol_errors, [])
        self.assertFalse(f.events("activity.failed"))
        self.assertFalse([r for r in f.rt.team.view(f.task)["runs"] if r["status"] == "failed"])
        self.assertTrue(f.rt.verify_chain().ok)

    def test_pending_rename_exposes_both_exact_sets_and_retires_old_assignment(self):
        f = self.f
        old = f.publish("initial package\n", filename="cater_api_client_fix.zip")
        first = self.ok(f.request([old]))
        original = self.message(f, first["review_seq"])
        new = f.publish("revised package\n", filename="v2.zip")
        second = self.revise(f, first, [new])
        self.assertEqual(self.message(f, first["review_seq"]), original)
        visible = {"context": f.context(role_id=f.reviewer),
                   "assignment": self.message(f, second["message_seq"]).payload}
        self.assert_visible_refs(visible, self.refs(f, [old, new]))
        rows = {r["seq"]: r for r in f.rt.team.recovery.assignments(f.task)}
        self.assertNotIn(first["message_seq"], rows)
        self.assertIn(second["message_seq"], rows)
        before = self.effects(f)
        self.assertEqual(f.record(first["review_seq"])["status"], "error")
        self.assertEqual(self.effects(f), before)
        self.assertEqual(f.events("team.review_recorded"), [])
        self.assertEqual(self.finish(f).get("code"), "review_pending")
        self.approve(f, second)
        self.ok(self.finish(f))
        self.assert_delivery(f, [new])

    def test_changes_requested_allows_add_split_merge_and_carries_complete_refs(self):
        f = self.f
        old = f.publish("combined original\n", filename="bundle.txt")
        first = self.ok(f.request([old]))
        self.ok(f.record(first["review_seq"], "changes_requested", feedback="Split implementation and add usage."))
        split = [f.publish("part A\n", filename="a.txt"),
                 f.publish("part B\n", filename="b.txt"),
                 f.publish("usage\n", filename="usage.md")]
        second = self.revise(f, first, split)
        self.assert_visible_refs({"context": f.context(role_id=f.reviewer),
                                  "message": self.message(f, second["message_seq"]).payload},
                                 self.refs(f, [old] + split))
        self.ok(f.record(second["review_seq"], "changes_requested", feedback="Merge the runtime; retain usage."))
        merged = f.publish("merged corrected runtime\n", filename="runtime.txt")
        third = self.revise(f, second, [merged, split[-1]])
        self.assertEqual(self.finish(f).get("code"), "review_pending")
        self.approve(f, third)
        self.ok(self.finish(f))
        self.assert_delivery(f, [merged, split[-1]])

    def test_old_approved_revision_never_approves_renamed_or_added_files(self):
        f = self.f
        old = f.publish("original\n", filename="approved-v1.txt")
        first = self.ok(f.request([old]))
        decision = self.approve(f, first)
        historical = self.message(f, decision["decision_seq"])
        new = [f.publish("original\n", filename="approved-v2.txt"),
               f.publish("new config\n", filename="config.txt")]
        second = self.revise(f, first, new)
        self.assertEqual(self.message(f, decision["decision_seq"]), historical)
        self.assertEqual(len(f.events("team.review_recorded")), 1)
        self.assertEqual(self.finish(f).get("code"), "review_pending")
        self.assertEqual(f.record(first["review_seq"])["status"], "error")
        self.assertEqual(self.row(f, second)["status"], "pending")
        self.approve(f, second)
        self.assertEqual([e.payload["review_seq"] for e in f.events("team.review_recorded")],
                         [first["review_seq"], second["review_seq"]])
        self.ok(self.finish(f))
        self.assert_delivery(f, new)

    def test_two_revisions_queue_delivers_only_latest_complete_set(self):
        f = self.f
        # Every callback is reached by the real ActivityRunner/Runtime consumer.
        # Waiting for a missing reviewer/author wake-up cannot be hidden by exchange.
        parent = []
        reports = []

        def manager(context, history, tools):
            message = context["message"]
            if message["sender"] == "human":
                return f.native_turn("", ("delegate", {
                    "recipient": f.worker, "instruction": "Publish, revise twice after review, and return."}),
                    ("wait_for_reply", {}))
            if message["kind"] == "report":
                self.assertEqual(message["reply_to"], parent[0])
                return f.native_turn("", ("finish_work", {
                    "summary": "Latest reviewed set", "verification": "Fixture bytes inspected"}))
            return f.native_turn("", ("wait_for_reply", {}))

        def author(context, history, tools):
            active = context["active_assignment"]
            self.assertIsNotNone(active)
            if not parent:
                parent.append(active["seq"])
            self.assertEqual(active["seq"], parent[0])
            rows = context["reviews"]
            if not history and context["message"]["kind"] == "report":
                row, = rows
                reports.append(row["status"])
                if row["status"] == "approved":
                    return f.native_turn("", ("send_message", {
                        "recipient": f.manager, "reply_to": parent[0], "text": "Approved final set returned."}),
                        ("wait_for_reply", {}))
            if history:
                replies = [m for m in history if m.get("role") == "tool"]
                published = [json.loads(m["content"])["artifact_seq"] for m in replies
                             if "artifact_seq" in json.loads(m["content"])]
                if published:
                    args = {"reviewer": f.reviewer, "artifact_seqs": published,
                            "instruction": "Compare the whole old and new sets against the original objective."}
                    if rows:
                        args["review_seq"] = rows[0]["request_seq"]
                    return f.native_turn("", ("request_review", args))
            index = len(f.events("team.review_requested"))
            files = [("draft.txt", "draft" )] if index == 0 else (
                [("part-a.txt", "A"), ("part-b.txt", "B")] if index == 1 else
                [("final.txt", "merged"), ("readme.md", "usage")])
            return f.native_turn("", *[("publish_artifact", {
                "filename": name, "content": body, "summary": "Revision fixture", "format": "text"})
                for name, body in files])

        def reviewer(context, history, tools):
            row, = context["reviews"]
            if not history:
                return f.native_turn("", *[("read_artifact", {"artifact_seq": ref["seq"]})
                                           for ref in row["artifacts"]])
            for reply in (json.loads(m["content"]) for m in history if m.get("role") == "tool"):
                self.ok(reply)
                self.assertEqual(hashlib.sha256(reply["content"].encode()).hexdigest(), reply["content_sha256"])
            index = len(f.events("team.review_recorded"))
            return f.native_turn("", ("record_review", {
                "review_seq": row["request_seq"], "verdict": "approved" if index == 2 else "changes_requested",
                "feedback": ["Split the runtime", "Merge and add usage", "Checked final complete set"][index]}))

        f.driver.steps = {f.manager: [manager] * 24, f.worker: [author] * 24, f.reviewer: [reviewer] * 12}
        self.queued(f, "Start the revision regression")
        self.assertEqual(reports, ["changes_requested", "changes_requested", "approved"])
        requests = f.events("team.review_requested")
        self.assertEqual(len(requests), 3)
        self.assertEqual({r["root_seq"] for r in f.rt.team_reviews.view(f.task)}, {requests[0].seq})
        self.assertEqual(len(f.events("team.review_recorded")), 3)
        self.assertEqual(len(f.results("finish_work")), 1)
        self.ok(f.results("finish_work")[0])
        self.assertEqual(f.rt.team.view(f.task)["pending_count"], 0)
        self.assert_delivery(f, [r["seq"] for r in requests[-1].payload["artifacts"]])
        f.assert_one_run_per_role()

    def test_explicit_superseded_selection_rejected_history_stays_readable(self):
        f = self.f
        old = f.publish("history body\n", filename="old.txt")
        first = self.ok(f.request([old]))
        self.approve(f, first)
        new = f.publish("current body\n", filename="current.txt")
        second = self.revise(f, first, [new])
        self.approve(f, second)
        for selection in ([old], [old, new]):
            with self.subTest(selection=selection):
                before = self.effects(f)
                result = self.finish(f, selection)
                self.assertEqual(result["status"], "error", result)
                self.assertTrue(result.get("code"), result)
                self.assertEqual(self.effects(f), before)
        before = self.effects(f)
        read, = f.exchange(f.reviewer, ("read_artifact", {"artifact_seq": old}))
        self.ok(read)
        self.assertEqual(read["content"], "history body\n")
        self.assertEqual(read["content_sha256"], self.refs(f, [old])[0]["content_sha256"])
        self.assertEqual(self.effects(f), before)
        history, = f.exchange(f.reviewer, ("read_checkpoint", {"review_seq": first["review_seq"]}))
        self.ok(history)
        self.assertEqual(history["review"]["status"], "superseded")
        self.assertEqual(history["review"]["current_request_seq"], second["review_seq"])
        self.assertEqual(history["review"]["artifacts"], self.refs(f, [old]))
        self.assertEqual(self.effects(f), before, "Historical reads must not reopen or approve work")
        current = f.rt.team_reviews.view(f.task)
        self.assertFalse(any(r["request_seq"] == first["review_seq"] and r["status"] == "approved" for r in current))
        visible = {"context": f.context(role_id=f.reviewer),
                   "assignment": self.message(f, second["message_seq"]).payload}
        self.assert_visible_refs(visible, self.refs(f, [old, new]))
        self.assertEqual(f.record(first["review_seq"])["status"], "error")
        self.assertEqual(f.request([new], review_seq=first["review_seq"])["status"], "error")
        self.ok(self.finish(f, [new]))
        self.assert_delivery(f, [new])

    def test_other_current_root_keeps_shared_old_ref_in_default_delivery(self):
        f = self.f
        old = f.publish("shared dependency\n", filename="shared.txt")
        first = self.ok(f.request([old]))
        self.ok(f.record(first["review_seq"], "changes_requested"))
        new = f.publish("replacement\n", filename="replacement.txt")
        second = self.revise(f, first, [new])
        # Once the first root no longer selects old, another root may review it.
        other = self.ok(f.request([old], reviewer=f.observer))
        self.assertNotEqual(other["root_seq"], first["root_seq"])
        self.approve(f, second)
        self.assertEqual(self.finish(f).get("code"), "review_pending")
        decision = self.ok(f.record(other["review_seq"], reviewer=f.observer,
                                    source=self.message(f, other["message_seq"])))
        f.exchange(f.worker, source=self.message(f, decision["message_seq"]))
        self.ok(self.finish(f))
        self.assert_delivery(f, [old, new])

    def test_superseded_review_queue_drops_old_work_but_keeps_ordinary_delegate(self):
        f = self.f
        old = f.publish("old\n")
        first = self.ok(f.request([old]))
        ordinary, = f.exchange(f.manager, ("delegate", {
            "recipient": f.observer, "instruction": "Independent ordinary analysis must still return."}))
        self.ok(ordinary)
        new = f.publish("new\n", filename="renamed.txt")
        second = self.revise(f, first, [new])
        before = len(f.driver.calls)

        def review(context, history, tools):
            self.assertEqual(context["message"]["seq"], second["message_seq"])
            return f.native_turn("", ("record_review", {
                "review_seq": second["review_seq"], "verdict": "approved", "feedback": "Latest bytes checked"}))

        f.driver.steps = {f.manager: [f.native_turn("", ("wait_for_reply", {}))] * 8,
                          f.worker: [f.native_turn("", ("wait_for_reply", {}))] * 8,
                          f.reviewer: [review], f.observer: [f.native_turn("", ("wait_for_reply", {}))]}
        self.queued(f, "Drain current work once")
        seen = [c["context"]["message"]["seq"] for c in f.driver.calls[before:]]
        self.assertNotIn(first["message_seq"], seen)
        self.assertEqual(seen.count(second["message_seq"]), 1)
        self.assertEqual(seen.count(ordinary["message_seq"]), 1)
        assignments = f.rt.team.recovery.assignments(f.task)
        self.assertNotIn(first["message_seq"], {r["seq"] for r in assignments})
        self.assertIn(ordinary["message_seq"], {r["seq"] for r in assignments})

    def test_actor_errors_are_specific_structured_and_do_not_change_chain(self):
        f = self.f
        old = f.publish("author's file\n")
        first = self.ok(f.request([old]))
        new = f.publish("author's revision\n")
        other = f.publish("different author\n", author=f.observer, filename="other.txt")
        attempts = [("author", lambda: f.request([new], requester=f.manager, review_seq=first["review_seq"]), f.worker),
                    ("author", lambda: f.request([other], requester=f.observer, review_seq=first["review_seq"]), f.worker),
                    ("reviewer", lambda: f.request([new], reviewer=f.observer, review_seq=first["review_seq"]), f.reviewer),
                    ("reviewer", lambda: f.record(first["review_seq"], reviewer=f.observer), f.reviewer)]
        for reason, action, expected in attempts:
            with self.subTest(reason=reason, expected=expected):
                before = self.effects(f)
                result = action()
                self.assert_not_started(result, reason)
                # Expected identity must be machine-readable,
                # without prescribing names for newly added diagnostic fields.
                structured = {k: v for k, v in result.items() if k != "message"}
                self.assertIn(expected, json.dumps(structured))
                self.assertEqual(self.effects(f), before)

    def test_goal_pause_and_new_customer_input_stop_inflight_revision(self):
        for guard in ("goal", "pause", "newmsg"):
            with self.subTest(guard=guard):
                f = self.fixture()
                old = f.publish("old\n")
                first = self.ok(f.request([old]))
                new = f.publish("revised\n", filename="new.txt")
                before = len(f.events("team.review_requested"))

                def interrupted(context, history, tools):
                    if guard == "goal":
                        result, = f.exchange(f.manager, ("set_objective", {"goal": "Separate new objective"}))
                        self.ok(result)
                    elif guard == "pause":
                        f.rt.execution_control.change(f.task, "pause", request_id="revision-pause")
                    else:
                        role = next(r for r in f.rt.worker_roles if r.id == f.worker)
                        f.rt.team.receive(f.task, f"@{role.name} Stop; first read this newer instruction")
                    return f.native_turn("", ("request_review", {
                        "review_seq": first["review_seq"], "reviewer": f.reviewer,
                        "artifact_seqs": [new], "instruction": "An interrupted submission must not start"}))

                source = f.rt.team.receive(f.task, f"@{f.rt.team._role(f.worker).name} Begin revision")
                f.driver.steps[f.worker] = [interrupted]
                run = self.unknown_run(f, source)
                list(f.rt.team._run_message(f.task, source, f.rt.team._role(f.worker), run))
                finished, = [e for e in f.events("team.run_finished")
                              if e.payload["run_id"] == run.payload["run_id"]]
                self.assertEqual(finished.payload["status"], "interrupted", finished.payload)
                self.assertEqual(f.protocol_errors, [])
                self.assertEqual(len(f.events("team.review_requested")), before)
                self.assertEqual(f.events("team.review_recorded"), [])
                self.assertEqual(f.events("team.objective_completed"), [])
                if guard == "goal":
                    self.assertEqual(self.row(f, first)["status"], "stale")
                    effects = self.effects(f)
                    historical, = f.exchange(f.reviewer, ("read_checkpoint", {"review_seq": first["review_seq"]}))
                    self.ok(historical)
                    self.assertEqual(historical["review"]["status"], "stale")
                    self.assertEqual(self.effects(f), effects)
                    self.assertEqual(f.request([new], review_seq=first["review_seq"])["status"], "error")
                if guard == "pause":
                    calls = len(f.driver.calls)
                    f.rt.team.run(f.task)
                    self.assertEqual(len(f.driver.calls), calls)

    def unknown_run(self, f, source):
        return f.rt.log.emit(type="team.run_started", actor=HOST, task_id=f.task, payload={
            "message_seq": source.seq, "role": source.payload["recipient"],
            "run_id": f"unknown-revision-{source.seq}", "revision": f.rt.team.view(f.task)["revision"],
            "control_seq": f.rt.execution_control.view(f.task)["seq"]})

    def test_unknown_workspace_effect_survives_revision_and_blocks_delivery(self):
        f = self.f
        old = f.publish("old\n")
        first = self.ok(f.request([old]))
        root = f.bind_workspace()
        run = self.unknown_run(f, self.message(f, first["message_seq"]))
        unknown = f.rt.log.emit(type="team.tool_started", actor=HOST, task_id=f.task, payload={
            **run.payload, "name": "workspace_write", "call_id": "unknown-write",
            "root": str(root), "arguments": {"path": "effect.txt", "expected_sha256": None}})
        f.rt.log.emit(type="team.run_finished", actor=HOST, task_id=f.task,
                      payload={**run.payload, "status": "interrupted", "reason": "Disposable lost receipt"})
        new = f.publish("new\n", filename="renamed.txt")
        second = self.revise(f, first, [new])
        self.approve(f, second)
        self.assertIn(unknown.seq, {e.seq for e in f.rt.workspaces.pending(task_id=f.task)})
        result = self.finish(f)
        self.assertEqual(result.get("code"), "operation_pending", result)
        self.assertIn(unknown.seq, result["started_seqs"])
        self.assertEqual(f.events("team.objective_completed"), [])

    def test_unknown_external_effect_is_not_erased_with_superseded_review(self):
        f = self.f
        old = f.publish("old\n")
        first = self.ok(f.request([old]))
        run = self.unknown_run(f, self.message(f, first["message_seq"]))
        # Create the revision first: unknown external effects conservatively stop
        # subsequent mutating host calls, so inject the lost receipt afterwards.
        new = f.publish("new\n", filename="renamed.txt")
        second = self.revise(f, first, [new])
        self.approve(f, second)
        external = f.rt.team.external_runs.begin(f.task, f.rt.team._role(f.reviewer), run,
            basis={}, binding=f.rt.workspaces.view(f.task), tool_names=[],
            driver_fingerprint="disposable-review-fixture")
        f.rt.team.external_runs.finish(external, phase="prompt", prompt_attempted=True,
                                       cleanup_complete=False, result="failed")
        f.rt.log.emit(type="team.run_finished", actor=HOST, task_id=f.task,
                      payload={**run.payload, "status": "interrupted", "reason": "Disposable uncertain effect"})
        self.assertIn(external.seq, {r["started_seq"] for r in f.rt.team.external_runs.pending(f.task)})
        # Exercise native tool admission. It may interrupt before dispatch; both
        # paths must leave the effect pending and must never complete delivery.
        source = f.rt.team.receive(f.task, "Try delivery with an unknown external effect")
        before = len(f.events("team.tool_finished"))
        f.driver.steps[f.manager] = [f.native_turn("", ("finish_work", {
            "summary": "Must not finish", "verification": "Fixture only"}))]
        f.rt.team.run(f.task)
        self.assertEqual(f.protocol_errors, [])
        self.assertFalse([r for r in f.rt.team.view(f.task)["runs"] if r["status"] == "failed"])
        self.assertEqual(f.events("team.objective_completed"), [])
        self.assertIn(external.seq, {r["started_seq"] for r in f.rt.team.external_runs.pending(f.task)})
        receipts = [e.payload["result"] for e in f.events("team.tool_finished")[before:]
                    if e.payload["name"] == "finish_work"]
        self.assertTrue(all(r["status"] == "error" for r in receipts))
        runs = [r for r in f.rt.team.view(f.task)["runs"] if r["message_seq"] == source.seq]
        self.assertTrue(runs or f.rt.team.view(f.task)["pending_count"], "Input silently disappeared")

    def test_crossgroup_refs_and_requests_reject_without_disclosing_foreign_data(self):
        f = self.f
        other = f.rt.create_task("PRIVATE_REVISION_GOAL")
        f.rt.team.enable(other)
        private = f.publish("PRIVATE_REVISION_BODY", filename="private-revision.txt", task_id=other)
        foreign = self.ok(f.request([private], task_id=other))
        self.ok(f.record(foreign["review_seq"], "changes_requested", task_id=other,
                         feedback="PRIVATE_REVISION_FEEDBACK"))
        local = f.publish("local\n")
        first = self.ok(f.request([local]))
        new = f.publish("local revised\n", filename="local-new.txt")
        before = self.effects(f)
        other_before = self.effects(f, other)
        calls = len(f.driver.calls)
        results = [f.request([private], review_seq=first["review_seq"]),
                   f.request([new, private], review_seq=first["review_seq"]),
                   f.request([new], review_seq=foreign["review_seq"]),
                   f.record(foreign["review_seq"])]
        self.assertTrue(all(r["status"] == "error" for r in results), results)
        self.assertEqual(self.effects(f), before)
        self.assertEqual(self.effects(f, other), other_before)
        visible = json.dumps({"results": results, "calls": f.driver.calls[calls:]})
        for secret in ("PRIVATE_REVISION_GOAL", "PRIVATE_REVISION_BODY", "PRIVATE_REVISION_FEEDBACK",
                       "private-revision.txt", other):
            self.assertNotIn(secret, visible)


if __name__ == "__main__":
    unittest.main()
