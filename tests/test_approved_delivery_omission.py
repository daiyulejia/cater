"""Native selection correction with disposable real publications, not a model benchmark."""
import copy
from dataclasses import replace
import io
import unittest
import zipfile

from cater.events import Actor
from cater.team_delivery import TeamDelivery, omission_reason
import test_team_review_cycle as reviews


FIELD = "omitted_approved_artifacts_reason"
UNSET = object()


class ApprovedDeliveryOmissionTests(unittest.TestCase):
    def setUp(self):
        self.f = reviews.TeamReviewCycleTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def approve(self, *, names=("result.txt",)):
        f = self.f
        seqs = [f.publish("Actual fixture bytes\n", author=f.manager, filename=name) for name in names]
        request = f.request(seqs, requester=f.manager)
        self.assertEqual(request["status"], "ok", request)
        self.assertEqual(f.record(request["review_seq"])["status"], "ok")
        f.acknowledge_latest_review()
        return seqs

    def finish(self, seqs=UNSET, reason=UNSET, **extra):
        args = {"summary": "Fixture result", "verification": "Deterministic role, no model-quality claim", **extra}
        if seqs is not UNSET:
            args["artifact_seqs"] = seqs
        if reason is not UNSET:
            args[FIELD] = reason
        result, = self.f.exchange(self.f.manager, ("finish_work", args))
        return result

    def test_empty_approved_selection_is_correctable_without_finishing_or_attaching(self):
        seq, = self.approve()
        before = copy.deepcopy(self.f.rt.team.view(self.f.task))
        result = self.finish([])
        self.assertEqual((result["status"], result["code"]), ("error", "approved_artifacts_omitted"))
        self.assertEqual([a["seq"] for a in result["approved_artifacts"]], [seq])
        self.assertIn("草稿", result["message"])
        self.assertIn("不会自动加文件", result["message"])
        self.assertEqual(self.f.events("team.objective_completed"), [])
        self.assertEqual(self.f.rt.team.view(self.f.task)["revision"], before["revision"])
        self.assertEqual(self.f.rt.team.view(self.f.task)["status"], "running")

    def test_unrelated_selection_does_not_silently_discard_all_approved_files(self):
        approved, = self.approve()
        other = self.f.publish("Unreviewed attachment", author=self.f.manager, filename="notes.txt")
        result = self.finish([other])
        self.assertEqual(result["code"], "approved_artifacts_omitted")
        self.assertEqual([a["seq"] for a in result["approved_artifacts"]], [approved])
        self.assertEqual(self.f.events("team.objective_completed"), [])

    def test_same_input_can_correct_selection_and_download_exact_approved_bytes(self):
        seq, = self.approve()
        f = self.f
        def correct(context, history, tools):
            import json
            self.assertEqual(json.loads(history[-1]["content"])["code"], "approved_artifacts_omitted")
            self.assertEqual(f.events("team.objective_completed"), [])
            return f.native_turn("", ("finish_work", {"summary": "Exact artifact selected", "verification": "Fixture only", "artifact_seqs": [seq]}))
        f.driver.steps[f.manager] = [f.native_turn("", ("finish_work", {"summary": "Empty placeholder", "verification": "Fixture only", "artifact_seqs": []})), correct]
        source = f.rt.team.receive(f.task, "Return the reviewed file", request_id="omission-same-input")
        f.run_team()
        completed, = f.events("team.objective_completed")
        self.assertEqual([a["seq"] for a in completed.payload["artifacts"]], [seq])
        self.assertNotIn(FIELD, completed.payload)
        calls = [e for e in f.events("team.tool_started") if e.payload["name"] == "finish_work"]
        self.assertEqual([e.payload["message_seq"] for e in calls], [source.seq, source.seq])
        self.assertEqual([e.payload['status'] for e in f.events('team.run_finished')
                          if e.payload['message_seq'] == source.seq], ['completed'])
        with zipfile.ZipFile(io.BytesIO(f.rt.team.archive(f.task, expected_delivery_seq=completed.seq))) as archive:
            self.assertEqual(archive.read("result.txt"), b"Actual fixture bytes\n")
        self.assertTrue(f.rt.verify_chain().ok)

    def test_deliberate_omission_preserves_privacy_and_is_frozen_readable_without_handoff(self):
        self.approve()
        reason = "只交付本次文字结论；已复核文件作为内部材料，不附给顾客。"
        self.assertEqual(self.finish([], " \n" + reason + "\n")["status"], "ok")
        completed, = self.f.events("team.objective_completed")
        self.assertEqual(completed.payload["artifacts"], [])
        self.assertEqual(completed.payload[FIELD], reason)
        saved = copy.deepcopy(self.f.rt.team.delivery.read(self.f.task, {}))
        self.assertEqual(saved["delivery"][FIELD], reason)
        self.assertIn(reason, TeamDelivery.markdown(completed))
        self.assertIn("未附已复核成果", TeamDelivery.markdown(completed))
        self.f.exchange(self.f.manager, ("set_objective", {"goal": "A separate goal"}))
        read = self.f.rt.team.delivery.read(self.f.task, {"delivery_seq": completed.seq})
        self.assertEqual(read["delivery"], saved["delivery"])
        self.assertTrue(read["historical"])
        with self.assertRaises(ValueError):
            self.f.rt.team.archive(self.f.task, expected_delivery_seq=completed.seq)

    def test_missing_blank_nontext_and_oversized_reasons_cannot_complete(self):
        self.approve()
        for reason in (None, True, 1, {}, [], "", " \t\n", "\ufeff", "\u0085", "\x1c", " \ufeff\u0085\x1c\n", "x" * 1001):
            with self.subTest(reason_type=type(reason).__name__):
                self.assertEqual(self.finish([], reason)["status"], "error")
                self.assertEqual(self.f.events("team.objective_completed"), [])
        self.assertEqual(self.finish([], "有意不附文件")["status"], "ok")

    def test_omission_can_keep_unrelated_attachment_without_automatically_adding_approved_file(self):
        approved, = self.approve()
        other = self.f.publish("Only this note is intended", author=self.f.manager, filename="note.txt")
        self.assertEqual(self.finish([other], "复核材料不作为附件，仅附说明")["status"], "ok")
        completed, = self.f.events("team.objective_completed")
        self.assertEqual([a["seq"] for a in completed.payload["artifacts"]], [other])
        self.assertNotIn(approved, [a["seq"] for a in completed.payload["artifacts"]])
        with zipfile.ZipFile(io.BytesIO(self.f.rt.team.archive(self.f.task, expected_delivery_seq=completed.seq))) as archive:
            self.assertNotIn("result.txt", archive.namelist())
            self.assertIn("复核材料不作为附件", archive.read("__cater__/交付说明.md").decode('utf-8'))

    def test_no_approved_or_only_stale_approved_files_keep_text_only_contract(self):
        self.f.publish(author=self.f.manager)
        self.assertEqual(self.finish([])["status"], "ok")
        completed, = self.f.events("team.objective_completed")
        self.assertNotIn(FIELD, completed.payload)
        self.f.exchange(self.f.manager, ("set_objective", {"goal": "Second work"}))
        self.approve(names=("second.txt",))
        self.f.exchange(self.f.manager, ("set_objective", {"goal": "Text discussion, not old files"}))
        self.assertTrue(all(r['status'] == 'stale' for r in self.f.rt.team_reviews.view(self.f.task)))
        self.assertEqual(self.finish([])["status"], "ok")
        self.assertEqual(self.f.events("team.objective_completed")[-1].payload["artifacts"], [])

    def test_selected_approved_subset_is_allowed_and_unused_reason_is_not_recorded(self):
        first, _second = self.approve(names=("first.txt", "second.txt"))
        self.assertEqual(self.finish([first], "Unused reason")["status"], "ok")
        completed, = self.f.events("team.objective_completed")
        self.assertEqual([a['seq'] for a in completed.payload['artifacts']], [first])
        self.assertNotIn(FIELD, completed.payload)

    def test_reason_never_overrides_exact_sequence_or_pending_review(self):
        original, = self.approve()
        identical = self.f.publish("Actual fixture bytes\n", author=self.f.manager)
        rejected = self.finish([identical], "This is not a version waiver")
        self.assertEqual(rejected['code'], 'delivery_review_mismatch')
        next_review = self.f.request([identical], requester=self.f.manager,
                                     review_seq=self.f.rt.team_reviews.view(self.f.task)[0]['request_seq'])
        self.assertEqual(next_review['status'], 'ok')
        self.assertEqual(self.finish([], "Not a review waiver")['code'], 'review_pending')
        self.assertEqual(self.f.events('team.objective_completed'), [])
        self.assertNotEqual(original, identical)

    def test_reason_export_with_handoff_and_invalid_legacy_reason_never_fabricate_a_note(self):
        self.approve()
        self.assertEqual(self.finish([], "明确只给结论", handoff={"why": "只分享摘要", "reproduce": [], "limits": []})['status'], 'ok')
        event, = self.f.events('team.objective_completed')
        self.assertIn("明确只给结论", TeamDelivery.markdown(event))
        self.assertIn("只分享摘要", TeamDelivery.markdown(event))
        for value in (None, [], {}, " ", "x" * 1001):
            shadow = replace(event, payload={"summary": "Legacy", FIELD: value})
            self.assertIsNone(TeamDelivery.markdown(shadow))
        forged = replace(event, actor=Actor.agent(self.f.manager, self.f.rt.organization.leader.role),
                         payload={FIELD: "Not a host omission record"})
        self.assertIsNone(TeamDelivery.markdown(forged))

    def test_schema_is_optional_and_reason_validation_is_bounded_utf8_text(self):
        self.f.publish(author=self.f.manager)
        tool = next(t for t in self.f.driver.calls[-1]['tools'] if t['name'] == 'finish_work')
        schema = tool['input_schema']
        self.assertNotIn(FIELD, schema['required'])
        self.assertEqual(schema['properties'][FIELD]['maxLength'], 1000)
        self.assertEqual(schema['properties'][FIELD]['type'], 'string')
        self.assertIsNone(omission_reason('\ud800'))
        self.assertIsNone(omission_reason('\udfff'))
        self.assertIsNone(omission_reason('reason\0'))
        self.assertEqual(omission_reason(' \ufeff\u0085\x1c理由\x1f\ufeff '), '理由')
        self.assertEqual(omission_reason('\U0001f319' * 1000), '\U0001f319' * 1000)
        self.assertIsNone(omission_reason('\U0001f319' * 1001))


if __name__ == '__main__':
    unittest.main()
