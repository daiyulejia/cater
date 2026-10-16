"""Usage sanitization and wire adapters tested with in-memory responses only."""

from __future__ import annotations

import json
import socket
import unittest
from copy import deepcopy
from unittest.mock import patch

from cater.tool_protocol import anthropic_turn, openai_turn, safe_tool_usage


SCALAR_FIELDS = (
    "prompt_tokens", "completion_tokens", "total_tokens",
    "input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens",
    "prompt_cache_hit_tokens", "prompt_cache_miss_tokens",
)
DETAIL_FIELDS = {
    "prompt_tokens_details": ("cached_tokens", "audio_tokens"),
    "completion_tokens_details": (
        "reasoning_tokens", "audio_tokens", "accepted_prediction_tokens", "rejected_prediction_tokens",
    ),
    "cache_creation": ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens"),
}
INVALID_COUNTS = (None, True, False, -1, 1.0, 1.5, float("nan"), float("inf"),
                  "PRIVATE_SECRET", "12", [], {})
MISSING = object()


class ToolUsageDetailsTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(socket, "socket", side_effect=AssertionError("Real network forbidden")))

    def adapt(self, usage=MISSING, *, anthropic=False, stop=None):
        if anthropic:
            data = {"type": "message", "role": "assistant", "stop_reason": stop or "end_turn",
                    "content": [{"type": "text", "text": "done"}]}
            adapter = anthropic_turn
        else:
            data = {"choices": [{"finish_reason": stop or "stop",
                                 "message": {"role": "assistant", "content": "done"}}]}
            adapter = openai_turn
        if usage is not MISSING:
            data["usage"] = usage
        snapshot = deepcopy(data)
        result = adapter(data, driver="fixture", names=set(), seen=set())
        self.assertEqual(data, snapshot)
        return result

    def test_helper_keeps_explicit_counts_and_discards_unknown_metadata(self):
        expected = {name: index for index, name in enumerate(SCALAR_FIELDS)}
        for key, fields in DETAIL_FIELDS.items():
            expected[key] = {name: index for index, name in enumerate(fields)}
        value = deepcopy(expected)
        value.update(secret="PRIVATE_SECRET", unknown_tokens=99,
                     provider_metadata={"total_tokens": 99, "key": "PRIVATE_SECRET"})
        for key in DETAIL_FIELDS:
            value[key].update(secret="PRIVATE_SECRET", unknown_tokens=99,
                              nested={"cached_tokens": 99})
        snapshot = deepcopy(value)
        result = safe_tool_usage(value)
        self.assertEqual(result, expected)
        self.assertEqual(value, snapshot)
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertEqual(safe_tool_usage(result), result)
        result["prompt_tokens_details"]["cached_tokens"] = 999
        self.assertEqual(value, snapshot)

    def test_helper_rejects_invalid_counts_at_every_allowed_path(self):
        for invalid in INVALID_COUNTS:
            for name in SCALAR_FIELDS:
                with self.subTest(name=name, invalid=repr(invalid)):
                    self.assertEqual(safe_tool_usage({name: invalid}), {})
            for key, fields in DETAIL_FIELDS.items():
                for name in fields:
                    with self.subTest(key=key, name=name, invalid=repr(invalid)):
                        self.assertEqual(safe_tool_usage({key: {name: invalid}}), {})

    def test_helper_requires_dictionary_containers_and_drops_empty_details(self):
        for value in (None, True, 7, 1.0, "PRIVATE_SECRET", [], [("total_tokens", 7)]):
            with self.subTest(value=value):
                self.assertEqual(safe_tool_usage(value), {})
                for key in DETAIL_FIELDS:
                    self.assertEqual(safe_tool_usage({key: value}), {})
        self.assertEqual(safe_tool_usage({key: {} for key in DETAIL_FIELDS}), {})
        self.assertEqual(safe_tool_usage({"total_tokens": 0}), {"total_tokens": 0})

    def test_openai_details_and_deepseek_cache_counts_do_not_inflate_total(self):
        usage = {"prompt_tokens": 100, "completion_tokens": 30, "total_tokens": 130,
                 "prompt_tokens_details": {"cached_tokens": 80, "audio_tokens": 0},
                 "completion_tokens_details": {"reasoning_tokens": 20},
                 "prompt_cache_hit_tokens": 80, "prompt_cache_miss_tokens": 20}
        turn = self.adapt(usage)
        self.assertEqual((turn.tokens, turn.meta["token_usage"]), (130, "reported"))
        self.assertEqual(turn.meta["usage"], usage)

    def test_anthropic_cache_details_keep_existing_four_counter_total(self):
        usage = {"input_tokens": 20, "output_tokens": 5,
                 "cache_creation_input_tokens": 3, "cache_read_input_tokens": 7,
                 "cache_creation": {"ephemeral_5m_input_tokens": 1, "ephemeral_1h_input_tokens": 2}}
        turn = self.adapt(usage, anthropic=True)
        self.assertEqual((turn.tokens, turn.meta["token_usage"]), (35, "reported"))
        self.assertEqual(turn.meta["usage"], usage)
        # Even recognized counters from another provider cannot alter this total.
        usage.update(total_tokens=999, prompt_cache_hit_tokens=500,
                     completion_tokens_details={"reasoning_tokens": 4})
        self.assertEqual(self.adapt(usage, anthropic=True).tokens, 35)

    def test_invalid_details_are_omitted_without_invalidating_valid_totals(self):
        for anthropic in (False, True):
            for invalid in INVALID_COUNTS:
                with self.subTest(anthropic=anthropic, invalid=repr(invalid)):
                    base = {"input_tokens": 3, "output_tokens": 4} if anthropic else {"total_tokens": 7}
                    usage = {**base, "prompt_cache_hit_tokens": invalid, "prompt_cache_miss_tokens": invalid,
                             "prompt_tokens_details": {"cached_tokens": invalid, "secret": "PRIVATE_SECRET"},
                             "completion_tokens_details": {"reasoning_tokens": invalid},
                             "unknown_tokens": 99, "secret": "PRIVATE_SECRET"}
                    turn = self.adapt(usage, anthropic=anthropic)
                    self.assertEqual((turn.tokens, turn.meta["token_usage"]), (7, "reported"))
                    self.assertEqual(turn.meta["usage"], base)

    def test_missing_usage_stays_unavailable_and_explicit_zero_is_reported(self):
        for anthropic in (False, True):
            for usage in (MISSING, None, {}, [], "PRIVATE_SECRET"):
                with self.subTest(anthropic=anthropic, usage=usage):
                    turn = self.adapt(usage, anthropic=anthropic)
                    self.assertEqual((turn.tokens, turn.meta["token_usage"]), (0, "unavailable"))
                    self.assertEqual(turn.meta["usage"], {})
            zero = {"input_tokens": 0, "output_tokens": 0} if anthropic else {"total_tokens": 0}
            turn = self.adapt(zero, anthropic=anthropic)
            self.assertEqual((turn.tokens, turn.meta["token_usage"]), (0, "reported"))
            self.assertEqual(turn.meta["usage"], zero)

    def test_details_never_supply_missing_or_invalid_required_totals(self):
        details = {"prompt_tokens_details": {"cached_tokens": 0},
                   "completion_tokens_details": {"reasoning_tokens": 2},
                   "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 4}
        for anthropic in (False, True):
            for invalid in (MISSING, None, True, -1, 1.0, "PRIVATE_SECRET"):
                with self.subTest(anthropic=anthropic, invalid=invalid):
                    usage = deepcopy(details)
                    if anthropic:
                        usage["input_tokens"] = 4
                    if invalid is not MISSING:
                        usage["output_tokens" if anthropic else "total_tokens"] = invalid
                    turn = self.adapt(usage, anthropic=anthropic)
                    self.assertEqual((turn.tokens, turn.meta["token_usage"]), (0, "unavailable"))
                    self.assertEqual(turn.meta["usage"], {**details, **({"input_tokens": 4} if anthropic else {})})

    def test_invalid_legacy_optional_counts_still_mark_total_unavailable(self):
        cases = ((False, {"total_tokens": 7, "prompt_tokens": True}),
                 (False, {"total_tokens": 7, "completion_tokens": 1.0}),
                 (True, {"input_tokens": 3, "output_tokens": 4, "cache_read_input_tokens": None}),
                 (True, {"input_tokens": 3, "output_tokens": 4, "cache_creation_input_tokens": -1}))
        for anthropic, usage in cases:
            with self.subTest(anthropic=anthropic, usage=usage):
                turn = self.adapt(usage, anthropic=anthropic)
                self.assertEqual((turn.tokens, turn.meta["token_usage"]), (0, "unavailable"))
                self.assertEqual(turn.meta["usage"], safe_tool_usage(usage))

    def test_contained_responses_preserve_safe_usage_details(self):
        for anthropic in (False, True):
            usage = {"input_tokens": 3, "output_tokens": 4} if anthropic else {"total_tokens": 7}
            usage.update(prompt_tokens_details={"cached_tokens": 2, "secret": "PRIVATE_SECRET"},
                         secret="PRIVATE_SECRET")
            turn = self.adapt(usage, anthropic=anthropic, stop="max_tokens" if anthropic else "length")
            self.assertTrue(turn.meta["contained"])
            self.assertEqual((turn.tokens, turn.meta["token_usage"]), (7, "reported"))
            self.assertEqual(turn.meta["usage"], safe_tool_usage(usage))
            self.assertNotIn("PRIVATE", json.dumps(turn.meta))


if __name__ == "__main__":
    unittest.main()
