"""A known native API refusal never erases or replays local tool effects."""

import io
import json
import os
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from cater.anthropic import AnthropicMessagesDriver
from cater.runtime import _TEXT_REQUEST_REJECTED, _WORKSPACE_REQUEST_REJECTED
from cater.tool_protocol import ToolCall
from cater.work import WorkConflict
import test_workspace_integration as integration


class WorkspaceRejectionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = integration.WorkspaceIntegrationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.native = AnthropicMessagesDriver("dev", endpoint="https://example.invalid/v1/messages",
            model="test", api_key_env="CATER_FIXTURE_API_KEY")
        self.fixture.rt.drivers["dev"] = self.native

    @staticmethod
    def rejected():
        return HTTPError("https://example.invalid/v1/messages", 429, "refused", {}, None)

    def test_native_refusal_before_tools_is_resumable_without_text_only_marker(self):
        _, context = self.fixture.prepare_call()
        with patch.dict(os.environ, {"CATER_FIXTURE_API_KEY": "not-a-real-key"}), patch(
            "cater.anthropic._open_api_request", side_effect=self.rejected()) as http:
            response = self.fixture.gateway(context)
        self.assertIs(response.meta.get("_workspace_request_rejected"), _WORKSPACE_REQUEST_REJECTED)
        self.assertIsNot(response.meta.get("_text_request_rejected"), _TEXT_REQUEST_REJECTED)
        self.assertEqual(http.call_count, 1)
        self.assertFalse(self.fixture.rt._is_circuit_open("dev"))
        self.assertEqual(self.fixture.events("workspace.tool_started"), [])

    def test_refusal_after_completed_write_preserves_receipt_and_stops_this_round(self):
        self.fixture.bind()
        self.fixture.plan()
        reply = {"type": "message", "role": "assistant", "stop_reason": "tool_use",
                 "content": [{"type": "tool_use", "id": "write-1", "name": "workspace_write", "input": {
                     "path": "new.txt", "content": "New body", "expected_sha256": None}}],
                 "usage": {"input_tokens": 4, "output_tokens": 3}}
        with patch.dict(os.environ, {"CATER_FIXTURE_API_KEY": "not-a-real-key"}), patch(
            "cater.anthropic._open_api_request", side_effect=[io.BytesIO(json.dumps(reply).encode()), self.rejected()]) as http:
            self.fixture.rt.collaborate(self.fixture.task)
        item = self.fixture.rt.work.view(self.fixture.task)["items"][0]
        self.assertEqual(item["status"], "ready")
        self.assertIn("先读取实际文件", item["reason"])
        self.assertEqual(http.call_count, 2)
        self.assertEqual(len(self.fixture.events("workspace.tool_finished")), 1)
        self.assertEqual(len(self.fixture.executor.calls), 1)
        self.assertEqual(self.fixture.rt._reported_driver_tokens("dev"), 7)
        self.assertTrue(self.fixture.rt.verify_chain().ok)

    def test_native_final_format_correction_never_replays_a_tool(self):
        _, context = self.fixture.prepare_call()
        first = {"type": "message", "role": "assistant", "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "I have inspected the source; no changes were needed."}],
            "usage": {"input_tokens": 4, "output_tokens": 3}}
        final = integration.final_text()
        second = {**first, "content": [{"type": "text", "text": final}]}
        with patch.dict(os.environ, {"CATER_FIXTURE_API_KEY": "not-a-real-key"}), patch(
            "cater.anthropic._open_api_request", side_effect=[io.BytesIO(json.dumps(first).encode()), io.BytesIO(json.dumps(second).encode())]) as http:
            response = self.fixture.gateway(context)
        self.assertFalse(response.meta.get("contained"))
        self.assertEqual(http.call_count, 2)
        self.assertEqual(self.fixture.executor.calls, [])
        self.assertEqual(len(self.fixture.events("workspace.response_recorded")), 1)
        self.assertEqual(self.fixture.rt._reported_driver_tokens("dev"), 14)

    def test_second_bad_final_format_is_not_corrected_again(self):
        _, context = self.fixture.prepare_call()
        bad = {"type": "message", "role": "assistant", "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "Still natural prose, not a submission."}],
            "usage": {"input_tokens": 4, "output_tokens": 3}}
        with patch.dict(os.environ, {"CATER_FIXTURE_API_KEY": "not-a-real-key"}), patch(
            "cater.anthropic._open_api_request", side_effect=[io.BytesIO(json.dumps(bad).encode()), io.BytesIO(json.dumps(bad).encode())]) as http:
            response = self.fixture.gateway(context)
        self.assertTrue(response.meta.get("contained"))
        self.assertEqual(http.call_count, 2)
        self.assertEqual(self.fixture.rt._reported_driver_tokens("dev"), 14)

    def test_unknown_work_can_be_reconciled_without_replaying_finished_tools(self):
        claim, context = self.fixture.prepare_call()
        self.fixture.rt.work.fail(self.fixture.task, claim, "Model output lost", uncertain=True)
        service = self.fixture.rt.workspaces
        with self.assertRaises(ValueError):
            service.reconcile_work(self.fixture.task, context["attempt_id"], reason="Inspected")
        service.reconcile_work(self.fixture.task, context["attempt_id"], reason="Inspected files and stopped execution", invocation_stopped=True)
        self.assertEqual(self.fixture.rt.work.view(self.fixture.task)["items"][0]["status"], "ready")
        self.assertEqual(self.fixture.executor.calls, [])
        with self.assertRaises(WorkConflict):
            service.reconcile_work(self.fixture.task, context["attempt_id"], reason="Old attempt", invocation_stopped=True)

    def test_live_tool_progress_renews_a_near_expiry_claim(self):
        _, context = self.fixture.prepare_call()
        self.fixture.rt.drivers["dev"] = self.fixture.driver
        clock = [time.time()]
        self.fixture.driver.turns = [integration.turn(ToolCall("read-1", "workspace_read", {"path": "a.txt"})),
                                     integration.turn(text=integration.final_text())]
        self.fixture.driver.after_turn = lambda count: clock.__setitem__(0, clock[0] + 500) if count == 1 else None
        with patch("time.time", side_effect=lambda: clock[0]):
            response = self.fixture.gateway(context)
        self.assertFalse(response.meta.get("contained"))
        self.assertEqual(len(self.fixture.events("work.lease_renewed")), 1)

    def test_expired_or_changed_attempt_cannot_be_renewed(self):
        _, context = self.fixture.prepare_call()
        with self.assertRaises(WorkConflict):
            self.fixture.rt.work.renew_claim(self.fixture.task, context["work_item_id"], "another-attempt")
        with patch("time.time", return_value=time.time() + 1000):
            with self.assertRaises(WorkConflict):
                self.fixture.rt.work.renew_claim(self.fixture.task, context["work_item_id"], context["attempt_id"])


if __name__ == "__main__":
    unittest.main()
