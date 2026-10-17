"""Offline regressions for the public model_context projection contract."""

from __future__ import annotations

import copy
import json
import unittest

from cater.team_context import model_context


INPUTS = ("message", "active_assignment", "latest_customer_message")
MISSING = object()
LONG = "Exact requirement with trailing space.\n" * 100
UNICODE = ('\u4e2d\u6587\U0001f9ea e\u0301 \"quoted\" \\path\r\n\t\x00 ' * 120)


def wire(value):
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


def row(seq=7, sender="manager", text=LONG, **metadata):
    return {"seq": seq, "sender": sender, "text": text, **metadata}


def context(**updates):
    # Include the complete structural inputs consumed by model_context.
    value = {
        "you": {"id": "worker", "name": "Worker", "role": "developer"},
        "members": [],
        "message": row(101, "human", "Current request", sender_kind="human"),
        "active_assignment": None,
        "latest_customer_message": None,
        "current_work": {
            "enabled": True, "objective": "Offline regression", "revision": 1,
            "status": "active", "pending_count": 1,
            "checkpoint": {"summary": "Work remains open"}, "runs": [],
        },
        "open_assignments": [], "outgoing_work": [], "notifications": [],
        "conversation": [], "artifacts": [], "reviews": [], "rules": [],
        "receipt_index": [], "pending_operations": [], "questionnaires": [],
        "workspace": {"available": False}, "background": None,
    }
    value.update(updates)
    return value


def unreferenced_projection(value):
    # Cost cases have empty historical lists, so removing the optional inputs
    # obtains the public baseline without disabling or calling private helpers.
    baseline = model_context({**value, "active_assignment": None,
                              "latest_customer_message": None})
    for key in INPUTS:
        if key in value:
            baseline[key] = copy.deepcopy(value[key])
        else:
            baseline.pop(key, None)
    return baseline


class ContextReferenceTests(unittest.TestCase):
    def assert_exact_input(self, projected, key, original):
        actual = projected[key]
        if "context_ref" not in actual:
            self.assertEqual(actual, original)
            return actual["text"]
        self.assertIsNone(actual["text"])
        target_key = actual["context_ref"]
        self.assertIn(target_key, ("message", "active_assignment"))
        self.assertNotEqual(target_key, key)
        target = projected[target_key]
        self.assertNotIn("context_ref", target)
        self.assertNotIn("text_truncated", target)
        self.assertIsInstance(target["text"], str)
        self.assertEqual(target["seq"], original["seq"])
        self.assertEqual(target["sender"], original["sender"])
        self.assertEqual(target["text"], original["text"])
        self.assertEqual({k: v for k, v in actual.items() if k != "context_ref"},
                         {**original, "text": None})
        self.assertLess(len(wire(actual)), len(wire(original)))
        return target["text"]

    def assert_no_alias(self, source, candidate):
        value = context(message=source, active_assignment=candidate)
        projected = model_context(value)
        self.assertEqual(projected["message"], source)
        self.assertEqual(projected["active_assignment"], candidate)

    def test_active_assignment_references_complete_message_and_keeps_unique_metadata(self):
        source = row(kind="assignment", recipient="worker", sender_name="Manager")
        assignment = row(kind="assignment", recipient="worker", reply_to=3,
                         revision=1, evidence={"receipts": [11, 12]})
        projected = model_context(context(message=source, active_assignment=assignment))
        self.assertEqual(projected["message"], source)
        self.assertEqual(projected["active_assignment"]["context_ref"], "message")
        self.assert_exact_input(projected, "active_assignment", assignment)

    def test_latest_customer_references_message_and_preserves_customer_metadata(self):
        source = row(9, "human", UNICODE, sender_kind="human", recipient="manager")
        latest = {**source, "sender_name": "Customer", "event_type": "msg.said"}
        projected = model_context(context(message=source, latest_customer_message=latest))
        self.assertEqual(projected["message"], source)
        self.assertEqual(projected["latest_customer_message"]["context_ref"], "message")
        self.assert_exact_input(projected, "latest_customer_message", latest)

    def test_three_equal_sources_reference_message_directly_without_chaining(self):
        source = row()
        value = context(message=source, active_assignment={**source, "reply_to": 2},
                        latest_customer_message={**source, "sender_name": "Same source"})
        projected = model_context(value)
        self.assertEqual(projected["message"], source)
        for key in INPUTS[1:]:
            self.assertEqual(projected[key]["context_ref"], "message")
            self.assert_exact_input(projected, key, value[key])

    def test_latest_can_reference_a_complete_active_assignment_in_this_input(self):
        assignment = row(19, "human", UNICODE)
        latest = {**assignment, "sender_kind": "human"}
        projected = model_context(context(active_assignment=assignment,
                                          latest_customer_message=latest))
        self.assertEqual(projected["active_assignment"], assignment)
        self.assertEqual(projected["latest_customer_message"]["context_ref"], "active_assignment")
        self.assert_exact_input(projected, "latest_customer_message", latest)

    def test_current_original_assignment_and_latest_survive_multiple_combinations(self):
        customer = row(30, "human", "New direction\n" + UNICODE, sender_kind="human")
        assignment = row(20, "manager", "Original assignment\n" + LONG, kind="assignment")
        report = row(40, "child", "Child result\n" + UNICODE, kind="report", reply_to=20)
        for message, active, latest in (
            (report, assignment, customer),
            (assignment, copy.deepcopy(assignment), customer),
            (customer, assignment, copy.deepcopy(customer)),
            (report, None, customer),
        ):
            with self.subTest(message_seq=message["seq"], has_active=active is not None):
                value = context(message=message, active_assignment=active,
                                latest_customer_message=latest)
                projected = json.loads(wire(model_context(value)))
                self.assertEqual(projected["message"], message)
                for key in INPUTS[1:]:
                    if value[key] is None:
                        self.assertIsNone(projected[key])
                    else:
                        self.assertEqual(self.assert_exact_input(projected, key, value[key]),
                                         value[key]["text"])

    def test_equal_text_from_different_events_is_never_merged(self):
        for key in INPUTS[1:]:
            with self.subTest(key=key):
                value = context(message=row(7), **{key: row(8)})
                projected = model_context(value)
                self.assertEqual(projected[key], value[key])

    def test_sequence_requires_positive_integer_not_bool_on_both_sides(self):
        for invalid in (True, False, 0, -1, 7.0, "7", None, MISSING):
            for side in ("source", "candidate", "both"):
                with self.subTest(seq=repr(invalid), side=side):
                    source, candidate = row(), row()
                    for target in ([source, candidate] if side == "both" else
                                   [source if side == "source" else candidate]):
                        if invalid is MISSING:
                            target.pop("seq")
                        else:
                            target["seq"] = invalid
                    self.assert_no_alias(source, candidate)

    def test_same_sequence_and_text_from_different_senders_or_roles_stay_full(self):
        for sender, kind in (("worker", "agent"), ("human", "human"), ("reviewer", "agent")):
            with self.subTest(sender=sender):
                self.assert_no_alias(row(sender_kind="agent"), row(sender=sender, sender_kind=kind))
        self.assert_no_alias(row(sender_kind="agent"), row(sender_kind="human"))

    def test_sender_requires_nonempty_string_on_both_sides(self):
        for invalid in ("", None, False, 7, MISSING):
            for side in ("source", "candidate", "both"):
                with self.subTest(sender=repr(invalid), side=side):
                    source, candidate = row(), row()
                    for target in ([source, candidate] if side == "both" else
                                   [source if side == "source" else candidate]):
                        if invalid is MISSING:
                            target.pop("sender")
                        else:
                            target["sender"] = invalid
                    self.assert_no_alias(source, candidate)

    def test_text_must_match_exactly_without_normalization_or_prefix_guessing(self):
        for left, right in ((LONG, LONG + " "), (LONG, LONG[:-1]),
                            (LONG + "\r\n", LONG + "\n"), (LONG, LONG.lower()),
                            (LONG + "\u00e9", LONG + "e\u0301"),
                            (LONG + "\u4e00", LONG + "\u4e8c")):
            with self.subTest(tail=repr(right[-8:])):
                self.assert_no_alias(row(text=left), row(text=right))

    def test_none_empty_or_missing_text_is_not_an_alias(self):
        for text in (None, "", MISSING):
            for side in ("source", "candidate", "both"):
                with self.subTest(text=repr(text), side=side):
                    source, candidate = row(), row()
                    for target in ([source, candidate] if side == "both" else
                                   [source if side == "source" else candidate]):
                        if text is MISSING:
                            target.pop("text")
                        else:
                            target["text"] = text
                    self.assert_no_alias(source, candidate)

    def test_optional_input_rows_can_be_none_or_absent(self):
        for key in INPUTS[1:]:
            for absent in (False, True):
                with self.subTest(key=key, absent=absent):
                    value = context()
                    if absent:
                        value.pop(key)
                    projected = model_context(value)
                    self.assertEqual(projected["message"], value["message"])
                    if absent:
                        self.assertNotIn(key, projected)
                    else:
                        self.assertIsNone(projected[key])

    def test_existing_reference_candidates_are_untouched_even_when_they_have_text(self):
        for key in INPUTS[1:]:
            for ref in (None, "", key, "message", "previous_cycle.message"):
                for text in (LONG, None):
                    with self.subTest(key=key, ref=ref, has_text=text is not None):
                        candidate = row(text=text, context_ref=ref, revision=1)
                        projected = model_context(context(message=row(), **{key: candidate}))
                        self.assertEqual(projected[key], candidate)

    def test_existing_references_are_not_targets_and_cannot_form_new_cycles(self):
        for text in (None, LONG):
            with self.subTest(existing_text=text is not None):
                source = row(text=text, context_ref="active_assignment")
                self.assert_no_alias(source, row())
                active = row(context_ref="latest_customer_message")
                latest = row()
                projected = model_context(context(active_assignment=active,
                                                  latest_customer_message=latest))
                self.assertEqual(projected["active_assignment"], active)
                self.assertEqual(projected["latest_customer_message"], latest)
                cycle = context(message=row(), active_assignment=row(text=None, context_ref="latest_customer_message"),
                                latest_customer_message=row(text=None, context_ref="active_assignment"))
                result = model_context(cycle)
                for key in INPUTS:
                    self.assertEqual(result[key], cycle[key])

    def test_incomplete_targets_and_any_truncation_marker_cannot_be_reused(self):
        for text in (None, MISSING):
            with self.subTest(incomplete=repr(text)):
                source = row()
                if text is MISSING:
                    source.pop("text")
                else:
                    source["text"] = text
                projected = model_context(context(active_assignment=source, latest_customer_message=row()))
                self.assertEqual(projected["latest_customer_message"], row())
        for marker in (True, False, None):
            for side in ("source", "candidate", "both"):
                with self.subTest(marker=marker, side=side):
                    source, candidate = row(), row()
                    for target in ([source, candidate] if side == "both" else
                                   [source if side == "source" else candidate]):
                        target["text_truncated"] = marker
                    self.assert_no_alias(source, candidate)
                    projected = model_context(context(message=source, open_assignments=[candidate],
                                                      conversation=[candidate]))
                    self.assertNotIn("context_ref", projected["open_assignments"][0])
                    self.assertIsInstance(projected["open_assignments"][0]["text"], str)
                    self.assertEqual(projected["conversation"], [candidate])

    def test_separate_calls_do_not_reuse_previous_round_or_cycle_bodies(self):
        first = context(message=row(), active_assignment=row())
        first["current_work"]["revision"] = 1
        prior = copy.deepcopy(model_context(first))
        self.assertEqual(prior["active_assignment"]["context_ref"], "message")
        for revision in (1, 2):
            with self.subTest(revision=revision):
                value = context(message=row(text="Changed current body " + LONG), active_assignment=row())
                value["current_work"]["revision"] = revision
                projected = model_context(value)
                self.assertEqual(projected["active_assignment"], value["active_assignment"])
                self.assertEqual(projected["message"], value["message"])
        self.assertEqual(model_context(first), prior)

    def test_short_bodies_do_not_add_reference_or_explanation_overhead(self):
        for text in ("", "a", "hi", "short text", "\u6708"):
            with self.subTest(text=text):
                value = context(message=row(text=text), active_assignment=row(text=text),
                                latest_customer_message=row(text=text))
                projected = model_context(value)
                self.assertEqual(projected, unreferenced_projection(value))

    def test_row_savings_are_rejected_when_explanation_makes_total_json_larger(self):
        large = model_context(context(message=row(), active_assignment=row()))
        reference_note = large["context_window"]["note"]
        counterexample = None
        for size in range(1, 400):
            value = context(message=row(text="x" * size), active_assignment=row(text="x" * size))
            baseline = unreferenced_projection(value)
            proposed = copy.deepcopy(baseline)
            proposed["active_assignment"] = {**value["active_assignment"], "text": None, "context_ref": "message"}
            proposed["context_window"]["note"] = reference_note
            if (len(wire(proposed["active_assignment"])) < len(wire(value["active_assignment"]))
                    and len(wire(proposed)) > len(wire(baseline))):
                counterexample = (value, baseline)
                break
        self.assertIsNotNone(counterexample, "Need an actual row-saving, total-growing JSON counterexample")
        value, baseline = counterexample
        self.assertEqual(model_context(value), baseline)

    def test_unicode_reference_saves_real_serialized_utf8_rows_and_total_with_note(self):
        value = context(message=row(text=UNICODE), active_assignment=row(text=UNICODE),
                        latest_customer_message=row(text=UNICODE, sender_name="\u987e\u5ba2"))
        projected = model_context(value)
        baseline = unreferenced_projection(value)
        self.assertGreater(len(wire(value["message"])), len(json.dumps(value["message"], ensure_ascii=False)))
        self.assertNotEqual(projected["context_window"]["note"], baseline["context_window"]["note"])
        self.assertIn("context_ref", projected["context_window"]["note"])
        self.assertIn("text", projected["context_window"]["note"])
        self.assertIn("\u672c\u8f6e", projected["context_window"]["note"])
        self.assertIn("\u4e00\u8df3", projected["context_window"]["note"])
        self.assertLess(len(wire(projected)), len(wire(baseline)))
        for key in INPUTS[1:]:
            self.assertEqual(self.assert_exact_input(projected, key, value[key]), UNICODE)
        self.assertEqual(json.loads(wire(projected))["message"]["text"], UNICODE)
        self.assertNotIn("body_refs", projected)

    def test_original_context_remains_deeply_equal_after_all_projection_paths(self):
        value = context(message=row(), active_assignment=row(evidence={"items": [1, {"status": "open"}]}),
                        open_assignments=[row(), row(8, text=UNICODE)],
                        conversation=[row(9, text=UNICODE)],
                        outgoing_work=[row(10, latest_update=LONG)], notifications=[row(11)],
                        artifacts=[{"seq": 1, "filename": "result.txt", "summary": LONG}],
                        reviews=[{"status": "pending", "feedback": UNICODE, "artifacts": [{"seq": 1}]}],
                        rules=[{"text": UNICODE, "metadata": {"enabled": True}}])
        value["current_work"]["runs"] = [{"status": "running", "reason": LONG, "run_id": "offline"}]
        saved = copy.deepcopy(value)
        saved_wire = wire(value)
        projected = model_context(value)
        self.assertEqual(value, saved)
        self.assertEqual(wire(value), saved_wire)
        self.assertIsNot(projected, value)
        self.assert_exact_input(projected, "active_assignment", saved["active_assignment"])
        self.assertEqual(model_context(value), projected)
        self.assertEqual(value, saved)

    def test_open_assignments_match_exact_source_and_never_point_to_wrong_content(self):
        source = row(recipient="worker", kind="assignment")
        active = {**source, "reply_to": 2}
        matches = [row(recipient="worker", kind="assignment", status="queued")]
        collisions = [row(text="Different requirement " + UNICODE), row(sender="other"), row(8),
                      row(True), row(recipient="other"), row(kind="notification"),
                      row(context_ref="previous_cycle.message"), row(text_truncated=False)]
        value = context(message=source, active_assignment=active, open_assignments=matches + collisions)
        projected = model_context(value)
        match = projected["open_assignments"][0]
        self.assertEqual(match["context_ref"], "message")
        self.assertIsNone(match["text"])
        self.assertEqual(match["status"], "queued")
        self.assertEqual(projected[match["context_ref"]]["text"], matches[0]["text"])
        self.assertNotIn("context_ref", projected[match["context_ref"]])
        for original, actual in zip(collisions, projected["open_assignments"][1:], strict=True):
            with self.subTest(original_keys=sorted(original), sender=original["sender"]):
                self.assertEqual(actual.get("context_ref"), original.get("context_ref"))
                expected = original["text"].encode("utf-8")[:1536].decode("utf-8", errors="ignore")
                self.assertEqual(actual["text"], expected)
                self.assertTrue(actual["text_truncated"])
                self.assertEqual(actual["text_bytes"], len(original["text"].encode("utf-8")))
                for key in original.keys() - {"text", "text_truncated"}:
                    self.assertEqual(actual[key], original[key])

    def test_conversation_only_removes_exact_complete_source_duplicates(self):
        source = row(text="Preserve conversation evidence. " * 5, recipient="worker", kind="assignment")
        same = {**source, "sender_name": "Manager"}
        retained = [{**source, "seq": 8}, {**source, "sender": "reviewer"},
                    {**source, "text": source["text"] + "!"}, {**source, "seq": True},
                    {**source, "recipient": "someone_else"}, {**source, "kind": "report"},
                    {**source, "text_truncated": False}, {**source, "context_ref": "message"}]
        value = context(message=source, conversation=[retained[0], same, *retained[1:]])
        projected = model_context(value)
        self.assertEqual(projected["conversation"], retained)
        self.assertEqual(projected["context_window"]["conversation_bytes"], len(wire(retained)))
        for marked_source in ({**source, "context_ref": "active_assignment"},
                              {**source, "text_truncated": False}):
            with self.subTest(source_keys=sorted(marked_source)):
                result = model_context(context(message=marked_source, conversation=[same]))
                self.assertEqual(result["conversation"], [same])

    def test_reviews_rules_and_other_full_evidence_are_not_compacted_with_inputs(self):
        evidence = {
            "reviews": [{"request_seq": 22, "status": "pending", "feedback": UNICODE * 3,
                         "artifacts": [{"seq": 20, "sha256": "offline-example"}]}],
            "rules": [{"text": LONG * 3, "revision": 2, "status": "active"}],
            "work_checkpoint": {"pending": ["must complete"], "summary": UNICODE},
            "questionnaires": [{"questions": [{"text": UNICODE}], "status": "open"}],
        }
        value = context(message=row(), active_assignment=row(), **evidence)
        projected = model_context(value)
        self.assertIn("context_ref", projected["active_assignment"])
        for key, original in evidence.items():
            with self.subTest(key=key):
                self.assertEqual(projected[key], original)
                self.assertEqual(wire(projected[key]), wire(original))

    def test_shared_metadata_requires_identical_type_and_value_in_all_views(self):
        for field, left, right in (
            ("recipient", "worker", "reviewer"), ("kind", "assignment", "report"),
            ("revision", 1, True), ("revision", 1, 1.0), ("revision", 1, "1"),
            ("reply_to", 3, 4), ("sender_kind", "agent", "human"),
            ("sender_name", "Manager", "Different name"), ("parent_assignment_seq", None, 3),
            ("tags", ["required"], ["optional"]), ("evidence", {"status": "open"}, {"status": "done"}),
        ):
            with self.subTest(field=field, left=left, right=right):
                source = row(**{field: left})
                candidate = row(**{field: right})
                self.assert_no_alias(source, candidate)
                latest = model_context(context(message=source, latest_customer_message=candidate))
                self.assertEqual(latest["latest_customer_message"], candidate)
                active = model_context(context(active_assignment=source, latest_customer_message=candidate))
                self.assertEqual(active["latest_customer_message"], candidate)
                projected = model_context(context(message=source, open_assignments=[candidate],
                                                  conversation=[candidate]))
                self.assertNotIn("context_ref", projected["open_assignments"][0])
                self.assertEqual(projected["conversation"], [candidate])

    def test_partial_metadata_bridge_does_not_hide_conflicts_or_create_reference_chains(self):
        message = row(meta={"version": "A"})
        active = row()
        conflicting = row(meta={"version": "B"}, open_only={"required": ["keep me"]})
        value = context(message=message, active_assignment=active,
                        open_assignments=[conflicting], conversation=[conflicting])
        projected = model_context(value)
        self.assert_exact_input(projected, "active_assignment", active)
        opened = projected["open_assignments"][0]
        self.assertNotIn("context_ref", opened)
        self.assertEqual(opened["meta"], conflicting["meta"])
        self.assertEqual(opened["open_only"], conflicting["open_only"])
        self.assertEqual(opened["text"], LONG.encode("utf-8")[:1536].decode("utf-8"))
        self.assertTrue(opened["text_truncated"])
        self.assertEqual(projected["conversation"], [conflicting])
        # Check conflicts against original views, including later views. A view
        # without the disputed key may still use a valid complete one-hop source.
        for field, left, right in (("meta", {"version": "A"}, {"version": "B"}),
                                   ("recipient", "worker", "reviewer"),
                                   ("revision", 1, True)):
            for omitted in INPUTS:
                with self.subTest(field=field, omitted=omitted):
                    values = iter((left, right))
                    rows = {key: row() if key == omitted else row(**{field: next(values)})
                            for key in INPUTS}
                    rows["latest_customer_message"]["latest_only"] = "keep latest"
                    value = context(**rows)
                    projected = model_context(value)
                    self.assertEqual(projected["message"], value["message"])
                    for key in INPUTS[1:]:
                        if key == omitted:
                            self.assert_exact_input(projected, key, value[key])
                        else:
                            self.assertEqual(projected[key], value[key])


if __name__ == "__main__":
    unittest.main()
